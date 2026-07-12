#!/usr/bin/env python3
"""WP-03 Freshness Monitor — contract tests for tools/freshness.py.

One number: seconds since the newest staging row (max over fact tables'
max(ts_utc)), PLUS the capture lag (seconds since the newest raw file's
mtime under work/raw/date=<today>/). Alert (exit 1, verdict STALE) when
EITHER exceeds the threshold; exit 0 + verdict FRESH otherwise. This is
the permanent alarm for the two 2026-07 staleness incidents: the
rotation-shard glob miss (capture fine / staging 31 min behind) and the
reader-lock crash-loop (same signature) — both are (b)-fresh/(a)-stale,
which is exactly why BOTH lags are measured and reported separately.

Fixtures build a real tiny staging via tools/ingest.py's Ingester (the
production writer), never hand-inserted rows. Time is injected with
--now so lags are deterministic. pytest-native; stdlib + duckdb only.
"""
import datetime
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "freshness.py")
sys.path.insert(0, os.path.join(ROOT, "tools"))

HOUR_US = 3_600_000_000
T0 = 1_783_300_000_000_000  # epoch micros, mid-hour (same anchor as test_ingest)


# ---------------------------------------------------------------- fixtures
def tick(mt, ts_us, bid, ask):
    msg = {"market_ticker": mt, "ts_ms": ts_us // 1000,
           "yes_bid_dollars": "%.4f" % bid, "yes_ask_dollars": "%.4f" % ask,
           "yes_bid_size_fp": "100.00", "yes_ask_size_fp": "100.00",
           "price_dollars": "%.4f" % ((bid + ask) / 2), "volume_fp": "10.00",
           "open_interest_fp": "5.00"}
    return json.dumps({"recv_wall_ns": ts_us * 1000,
                       "raw": json.dumps({"type": "ticker", "msg": msg})})


def trade(mt, ts_us, tid):
    msg = {"market_ticker": mt, "ts_ms": ts_us // 1000, "trade_id": tid,
           "yes_price_dollars": "0.5000", "no_price_dollars": "0.5000",
           "count_fp": "1.00", "taker_side": "yes"}
    return json.dumps({"recv_wall_ns": ts_us * 1000,
                       "raw": json.dumps({"type": "trade", "msg": msg})})


def make_warehouse(tmp):
    """Classification dim so the Ingester records KXBTC as Class A."""
    import duckdb
    wh = os.path.join(tmp, "warehouse")
    out = os.path.join(wh, "catalog", "series_classified")
    os.makedirs(out)
    nd = os.path.join(tmp, "cls.ndjson")
    with open(nd, "w") as f:
        f.write(json.dumps({"series_ticker": "KXBTC", "category": "Crypto",
                            "subcategory": "BTC", "group": "BTC",
                            "record_class": "A"}) + "\n")
    duckdb.connect().execute(
        "COPY (SELECT * FROM read_json_auto('%s', format='newline_delimited')) "
        "TO '%s' (FORMAT PARQUET)" % (nd, os.path.join(out, "part-00000.parquet")))
    return wh


# The newest fact row the fixture staging carries: a TRADE 151s after T0,
# strictly newer than every orderbooks_l1 row (101s) — so a correct tool
# MUST take the max over fact tables, not just orderbooks_l1.
L1_NEWEST_US = T0 + 101 * 1_000_000
STAGING_NEWEST_US = T0 + 151 * 1_000_000


def build_staging(tmp):
    """Real staging built by the production Ingester; returns db path."""
    import duckdb
    import ingest
    wh = make_warehouse(tmp)
    mt = "KXBTC-25DEC31-B50"
    lines = [tick(mt, T0 + i * 1_000_000, 0.40, 0.42) for i in range(100)]
    lines.append(tick(mt, L1_NEWEST_US, 0.41, 0.42))
    lines.append(trade(mt, STAGING_NEWEST_US, "t-1"))
    cap = os.path.join(tmp, "cap.ndjson")
    open(cap, "w").write("\n".join(lines) + "\n")
    db = os.path.join(tmp, "staging.duckdb")
    con = duckdb.connect(db)
    ing = ingest.Ingester(con, wh)
    ing.process_file(cap)
    con.close()  # release the writer lock so the tool's read-only connect works
    return db


def make_raw(tmp, now_s, age_s, name="firehose_03.ndjson"):
    """raw_root with date=<utc-day-of-now>/<name> whose mtime is now-age."""
    raw_root = os.path.join(tmp, "raw")
    day = datetime.datetime.fromtimestamp(
        now_s, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    d = os.path.join(raw_root, "date=%s" % day)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    open(p, "w").write("{}\n")
    os.utime(p, (now_s - age_s, now_s - age_s))
    return raw_root


def run_tool(*args):
    return subprocess.run([sys.executable, TOOL] + list(args),
                          capture_output=True, text=True, cwd=ROOT)


def run_json(*args):
    r = run_tool("--json", *args)
    try:
        return r, json.loads(r.stdout)
    except json.JSONDecodeError:
        pytest.fail("--json did not print valid JSON. rc=%d stdout=%r stderr=%r"
                    % (r.returncode, r.stdout, r.stderr))


# ------------------------------------------------------------------ tests
def test_freshness_computation(tmp_path):
    """Known newest ts + injected now ⇒ exact staging lag, max over fact tables."""
    tmp = str(tmp_path)
    db = build_staging(tmp)
    now_s = STAGING_NEWEST_US / 1e6 + 100.0          # exactly 100s after newest row
    raw = make_raw(tmp, now_s, age_s=5.0)
    r, j = run_json("--staging", db, "--raw-root", raw, "--now", repr(now_s))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert j["staging_lag_s"] == pytest.approx(100.0, abs=0.01)
    # a tool that only looked at orderbooks_l1 would report 150s — reject it
    assert j["staging_lag_s"] < 120.0
    assert j["staging_newest_table"] == "trades"
    assert j["staging_newest_us"] == STAGING_NEWEST_US
    assert j["capture_lag_s"] == pytest.approx(5.0, abs=0.01)
    assert j["verdict"] == "FRESH"


def test_alert_threshold_stale(tmp_path):
    """Stale staging fixture ⇒ alert fires: exit 1, verdict STALE, loud."""
    tmp = str(tmp_path)
    db = build_staging(tmp)
    now_s = STAGING_NEWEST_US / 1e6 + 10_000.0       # 10000s > 600s threshold
    raw = make_raw(tmp, now_s, age_s=5.0)            # capture is fine — still alert
    r = run_tool("--staging", db, "--raw-root", raw, "--now", repr(now_s),
                 "--threshold", "600")
    assert r.returncode == 1
    assert "STALE" in r.stdout
    assert "FRESH" not in r.stdout                   # pass_token absent on alert
    _, j = run_json("--staging", db, "--raw-root", raw, "--now", repr(now_s),
                    "--threshold", "600")
    assert j["verdict"] == "STALE"
    assert any("staging" in reason for reason in j["stale_reasons"])


def test_alert_threshold_fresh_is_silent(tmp_path):
    """Fresh fixture ⇒ no alert: exit 0, verdict FRESH."""
    tmp = str(tmp_path)
    db = build_staging(tmp)
    now_s = STAGING_NEWEST_US / 1e6 + 30.0
    raw = make_raw(tmp, now_s, age_s=2.0)
    r = run_tool("--staging", db, "--raw-root", raw, "--now", repr(now_s),
                 "--threshold", "600")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "FRESH" in r.stdout
    assert "STALE" not in r.stdout


def test_custom_threshold_flag(tmp_path):
    """--threshold is honored: the same 100s lag flips FRESH→STALE at 50s."""
    tmp = str(tmp_path)
    db = build_staging(tmp)
    now_s = STAGING_NEWEST_US / 1e6 + 100.0
    raw = make_raw(tmp, now_s, age_s=5.0)
    assert run_tool("--staging", db, "--raw-root", raw, "--now", repr(now_s),
                    "--threshold", "200").returncode == 0
    assert run_tool("--staging", db, "--raw-root", raw, "--now", repr(now_s),
                    "--threshold", "50").returncode == 1


def test_missing_staging_is_stale_and_loud(tmp_path):
    """No staging DB ⇒ STALE (fail-closed, GUARDRAILS S2), never a crash."""
    tmp = str(tmp_path)
    now_s = STAGING_NEWEST_US / 1e6
    raw = make_raw(tmp, now_s, age_s=2.0)
    missing = os.path.join(tmp, "nope", "staging.duckdb")
    r, j = run_json("--staging", missing, "--raw-root", raw, "--now", repr(now_s))
    assert r.returncode == 1
    assert j["verdict"] == "STALE"
    assert j["staging_lag_s"] is None
    assert any("missing" in reason for reason in j["stale_reasons"])
    plain = run_tool("--staging", missing, "--raw-root", raw, "--now", repr(now_s))
    assert plain.returncode == 1
    assert "STALE" in plain.stdout and "missing" in plain.stdout


def test_capture_lag_stale_even_when_staging_fresh(tmp_path):
    """Capture died (old raw mtime) + staging fresh ⇒ STALE on capture lag.

    The mirror image of this week's incidents: EITHER lag alerts."""
    tmp = str(tmp_path)
    db = build_staging(tmp)
    now_s = STAGING_NEWEST_US / 1e6 + 30.0           # staging: 30s, fine
    raw = make_raw(tmp, now_s, age_s=7_200.0)        # capture: 2h dead
    r, j = run_json("--staging", db, "--raw-root", raw, "--now", repr(now_s),
                    "--threshold", "600")
    assert r.returncode == 1
    assert j["verdict"] == "STALE"
    assert j["staging_lag_s"] == pytest.approx(30.0, abs=0.01)
    assert j["capture_lag_s"] == pytest.approx(7_200.0, abs=0.01)
    assert any("capture" in reason for reason in j["stale_reasons"])


def test_missing_raw_day_dir_is_stale(tmp_path):
    """No raw dir for today (or yesterday) ⇒ capture lag unknown ⇒ STALE."""
    tmp = str(tmp_path)
    db = build_staging(tmp)
    now_s = STAGING_NEWEST_US / 1e6 + 30.0
    empty_raw = os.path.join(tmp, "raw_empty")
    os.makedirs(empty_raw)
    r, j = run_json("--staging", db, "--raw-root", empty_raw,
                    "--now", repr(now_s))
    assert r.returncode == 1
    assert j["verdict"] == "STALE"
    assert j["capture_lag_s"] is None
    assert any("capture" in reason for reason in j["stale_reasons"])


def test_rotation_shards_counted_for_capture_lag(tmp_path):
    """firehose_02.ndjson.4 rotation shards ARE capture activity (the exact
    file class the shard-glob incident missed)."""
    tmp = str(tmp_path)
    db = build_staging(tmp)
    now_s = STAGING_NEWEST_US / 1e6 + 30.0
    raw = make_raw(tmp, now_s, age_s=7_200.0)                    # old base file
    make_raw(tmp, now_s, age_s=3.0, name="firehose_02.ndjson.4")  # fresh shard
    r, j = run_json("--staging", db, "--raw-root", raw, "--now", repr(now_s))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert j["capture_lag_s"] == pytest.approx(3.0, abs=0.01)


def test_fresh_rfq_file_cannot_mask_dead_firehose(tmp_path):
    """Independent low-rate channel families never substitute for Layer 1.

    Regression for RFQ rollout: if firehose is two hours stale while a fresh
    rfq_HH file exists, the primary pipeline verdict must remain STALE.
    """
    tmp = str(tmp_path)
    db = build_staging(tmp)
    now_s = STAGING_NEWEST_US / 1e6 + 30.0
    raw = make_raw(tmp, now_s, age_s=7_200.0, name="firehose_03.ndjson")
    make_raw(tmp, now_s, age_s=1.0, name="rfq_03.ndjson")
    r, j = run_json("--staging", db, "--raw-root", raw, "--now", repr(now_s),
                    "--threshold", "600")
    assert r.returncode == 1
    assert j["verdict"] == "STALE"
    assert j["capture_lag_s"] == pytest.approx(7_200.0, abs=0.01)
    assert j["capture_newest_file"].endswith("firehose_03.ndjson")
