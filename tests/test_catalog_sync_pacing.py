"""W-A4 contract: catalog_sync's REST loop must be paced and 429-resilient.

Root cause found at cutover step 4 (2026-07-09): on EC2 (same-region,
<1 ms RTT to Kalshi) the sequential pagination loop fires requests ~30x
faster than on the Mac (~30 ms RTT), burns the 600-token read burst, and
dies on HTTP 429 — the Mac never saw this only because its network latency
throttled it for free. The fix is explicit: paced requests + exponential
backoff on 429. These tests drive paced_open with fake opener/sleeper/clock
(offline, no network)."""
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import catalog_sync  # noqa: E402


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "msg", None, None)


def test_paced_open_retries_429_with_backoff_then_succeeds():
    calls = []
    sleeps = []

    def opener(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise _http_error(429)
        return "RESPONSE"

    t = [0.0]

    def clock():
        return t[0]

    def sleeper(s):
        sleeps.append(s)
        t[0] += s

    out = catalog_sync.paced_open("u", timeout=1, min_interval_s=0.05,
                                  opener=opener, sleeper=sleeper, clock=clock)
    assert out == "RESPONSE"
    assert len(calls) == 3
    # exponential backoff after each 429: 1s then 2s (plus any pacing waits)
    backoffs = [s for s in sleeps if s >= 1]
    assert backoffs == [1, 2], backoffs


def test_paced_open_paces_consecutive_requests():
    sleeps = []
    t = [0.0]

    def clock():
        return t[0]

    def sleeper(s):
        sleeps.append(s)
        t[0] += s

    def opener(url, timeout):
        return "OK"

    for _ in range(3):
        catalog_sync.paced_open("u", timeout=1, min_interval_s=0.05,
                                opener=opener, sleeper=sleeper, clock=clock)
    # calls 2 and 3 must each have waited ~min_interval (clock does not
    # advance except via sleeper, so the full interval is slept)
    pacing = [s for s in sleeps if 0 < s <= 0.05]
    assert len(pacing) >= 2, sleeps


def test_paced_open_gives_up_after_max_tries_and_reraises():
    def opener(url, timeout):
        raise _http_error(429)

    tries = []

    def sleeper(s):
        tries.append(s)

    try:
        catalog_sync.paced_open("u", timeout=1, max_tries=3,
                                opener=opener, sleeper=sleeper,
                                clock=lambda: 0.0)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 429


def test_paced_open_does_not_retry_non_429():
    calls = []

    def opener(url, timeout):
        calls.append(1)
        raise _http_error(500)

    try:
        catalog_sync.paced_open("u", timeout=1, opener=opener,
                                sleeper=lambda s: None, clock=lambda: 0.0)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 500
    assert len(calls) == 1


def test_fetch_all_routes_through_paced_open():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "catalog_sync.py")).read()
    live = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("urllib.request.urlopen(url" in ln for ln in live), \
        "fetch_all must not call urlopen directly — use paced_open"
    assert any("paced_open(" in ln and "def " not in ln for ln in live), \
        "paced_open is defined but never used"


def test_fetch_all_cap_stops_and_reports_truncation():
    """W-A5: the page cap must stop the crawl AND report truncated=True (D2 —
    the 80,000 events/markets truncation ran silently-in-a-log for days)."""
    import io
    import json as _json

    class FakeResp(io.StringIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(url, timeout):
        return FakeResp(_json.dumps(
            {"items": [{"n": 1}], "cursor": "more"}))

    orig = catalog_sync.paced_open
    catalog_sync.paced_open = lambda url, timeout=45: opener(url, timeout)
    try:
        items, truncated = catalog_sync.fetch_all("/x", "items", limit=1,
                                                  cap_pages=3)
    finally:
        catalog_sync.paced_open = orig
    assert len(items) == 3 and truncated is True


def test_events_and_markets_crawl_with_raised_cap():
    """The two known-truncating crawls must pass cap_pages > the default 400."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "catalog_sync.py")).read()
    live = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    ev = live[live.index('fetch_all("/events/"'):]
    assert "cap_pages=2000" in ev[:200], "events crawl still default-capped"
    mk = live[live.index('params={"status": "open"}'):]
    assert "cap_pages=2000" in mk[:120], "open-markets crawl still default-capped"


def test_write_parquet_raw_mixed_timestamp_precisions(tmp_path):
    """W-A5 regression (live, 2026-07-10): the 400k-row events crawl mixes
    second- and microsecond-precision timestamps in one column; head-only
    schema sampling inferred the narrow format and crashed at row 363,172.
    write_parquet_raw must survive mixed precisions losslessly."""
    import duckdb
    out = str(tmp_path / "events")
    rows = ([{"t": "2026-01-01T00:00:00Z"}] * 3
            + [{"t": "2025-12-14T12:19:15.983002Z"}])
    n = catalog_sync.write_parquet_raw(rows, out)
    assert n == 4
    got = duckdb.connect().execute(
        "SELECT count(*), count(t) FROM read_parquet('%s/part-00000.parquet')"
        % out).fetchone()
    assert got == (4, 4)
