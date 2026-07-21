"""W6: depth_probe contract — analysis function on a fixture capture (known
rates/bytes) + the operator gate (refusal without --operator-approved, live
refusal, work/raw capture-path refusal, dry-run opens no socket).

The probe itself is operator-gated and NEVER run here; run mode is exercised
only through paths that exit before any socket exists (refusals + --dry-run).
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "depth_probe.py")
sys.path.insert(0, os.path.join(ROOT, "tools"))

from depth_probe import analyze_capture, load_depth_target  # noqa: E402


# --------------------------------------------------------------------------
# fixture capture: hand-built, every expected number derivable by hand
# --------------------------------------------------------------------------

T0 = 1_783_307_645_000_000_000  # recv_wall_ns epoch base
RAW_SNAP = '{"type":"orderbook_snapshot","msg":{"x":1}}'
RAW_DELTA = '{"type":"orderbook_delta","msg":{"y":22}}'


def _line(channel, ticker, wall_ns, raw):
    return json.dumps({
        "recv_mono_ns": 1,
        "recv_wall_ns": wall_ns,
        "source": "Kalshi",
        "channel": channel,
        "source_ticker": ticker,
        "stream_epoch": 1,
        "raw": raw,
    }) + "\n"


@pytest.fixture
def fixture_capture(tmp_path):
    """Two live markets over exactly 10.0 s + one subscribed ack + one junk line.

    MKT-HI (High): 1 snapshot + 19 deltas = 20 msgs -> 2.0 msg/s
    MKT-MID (Mid): 5 deltas               =  5 msgs -> 0.5 msg/s
    MKT-DEAD (High): subscribed, zero book messages -> dead
    """
    raw_snap = RAW_SNAP
    raw_delta = RAW_DELTA
    lines = [_line("subscribed", "", T0, '{"type":"subscribed"}')]
    lines.append(_line("orderbook_snapshot", "MKT-HI", T0, raw_snap))
    for i in range(1, 19):  # deltas strictly inside the window
        lines.append(_line("orderbook_delta", "MKT-HI", T0 + i * 500_000_000, raw_delta))
    lines.append(_line("orderbook_delta", "MKT-HI", T0 + 10_000_000_000, raw_delta))  # closes span at 10 s
    for i in range(5):
        lines.append(_line("orderbook_delta", "MKT-MID", T0 + (i + 1) * 1_000_000_000, raw_delta))
    lines.append("this is not json\n")
    path = tmp_path / "depth_probe_fixture.ndjson"
    path.write_text("".join(lines))
    return str(path)


TIERS = {"MKT-HI": "High", "MKT-MID": "Mid", "MKT-DEAD": "High"}
EXPECTED = ["MKT-HI", "MKT-MID", "MKT-DEAD"]


def test_analyze_known_rates_and_bytes(fixture_capture):
    r = analyze_capture(fixture_capture, TIERS, expected_tickers=EXPECTED)

    assert r["duration_s"] == pytest.approx(10.0)
    assert r["total_msgs"] == 25
    assert r["msgs_per_s"] == pytest.approx(2.5)
    assert r["parse_errors"] == 1
    assert r["other_channels"] == {"subscribed": 1}

    hi = r["per_market"]["MKT-HI"]
    assert hi["tier"] == "High"
    assert hi["snapshots"] == 1 and hi["deltas"] == 19
    assert hi["msgs_per_s"] == pytest.approx(2.0)
    assert hi["wire_bytes"] == len(RAW_SNAP) + 19 * len(RAW_DELTA)  # exact

    mid = r["per_market"]["MKT-MID"]
    assert mid["tier"] == "Mid"
    assert mid["snapshots"] == 0 and mid["deltas"] == 5
    assert mid["msgs_per_s"] == pytest.approx(0.5)
    assert mid["wire_bytes"] == 5 * len(RAW_DELTA)

    # capture bytes = exact NDJSON line lengths, accounted per market
    assert hi["capture_bytes"] > hi["wire_bytes"]
    total_tier_bytes = sum(t["capture_bytes"] for t in r["per_tier"].values())
    assert total_tier_bytes == hi["capture_bytes"] + mid["capture_bytes"]

    # tier aggregation: only markets that SENT something are counted per tier
    assert r["per_tier"]["High"]["markets"] == 1
    assert r["per_tier"]["High"]["msgs"] == 20
    assert r["per_tier"]["High"]["msgs_per_s"] == pytest.approx(2.0)
    assert r["per_tier"]["High"]["avg_capture_b_per_msg"] == pytest.approx(
        r["per_tier"]["High"]["capture_bytes"] / 20.0)
    assert r["per_tier"]["Mid"]["bytes_per_s"] == pytest.approx(
        mid["capture_bytes"] / 10.0)

    # dead subscription surfaced loudly (D2), never silently absorbed
    assert r["dead_markets"] == ["MKT-DEAD"]


def test_analyze_empty_capture_is_all_dead(tmp_path):
    path = tmp_path / "empty.ndjson"
    path.write_text("")
    r = analyze_capture(str(path), TIERS, expected_tickers=EXPECTED)
    assert r["total_msgs"] == 0 and r["duration_s"] == 0.0 and r["msgs_per_s"] == 0.0
    assert sorted(r["dead_markets"]) == sorted(EXPECTED)


def test_load_depth_target_top_n(tmp_path):
    csv_path = tmp_path / "depth_target_2099-01-01.csv"
    csv_path.write_text(
        "rank,market_ticker,liquidity_tier\n"
        "1,AAA,High\n2,BBB,Mid\n3,CCC,Low\n")
    tickers, tiers = load_depth_target(str(csv_path), top_n=2)
    assert tickers == ["AAA", "BBB"]                      # top-N respects rank order
    assert tiers == {"AAA": "High", "BBB": "Mid", "CCC": "Low"}  # tiers for ALL rows


# --------------------------------------------------------------------------
# the operator gate (subprocess; every path exits before any socket exists)
# --------------------------------------------------------------------------

def _run(args, env_extra=None, cwd=ROOT):
    env = dict(os.environ)
    env.pop("KALSHI_MODE", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, TOOL] + args, capture_output=True,
                          text=True, env=env, cwd=cwd, timeout=60)


def test_refuses_without_operator_approved():
    p = _run([])
    assert p.returncode == 2
    assert "REFUSED" in p.stderr
    assert "--operator-approved" in p.stderr
    assert "docs/PLAN_DEPTH_EXPANSION.md" in p.stderr


def test_refuses_live_mode_even_with_flag():
    p = _run(["--operator-approved", "--dry-run"], env_extra={"KALSHI_MODE": "live"})
    assert p.returncode == 2
    assert "REFUSED" in p.stderr and "live" in p.stderr


def test_refuses_capture_path_under_work_raw(tmp_path):
    csv_path = tmp_path / "depth_target_2099-01-01.csv"
    csv_path.write_text("rank,market_ticker,liquidity_tier\n1,AAA,High\n")
    p = _run(["--operator-approved", "--dry-run", "--csv", str(csv_path),
              "--capture", "work/raw/date=2099-01-01/sneaky.ndjson"])
    assert p.returncode == 2
    assert "REFUSED" in p.stderr and "work/raw" in p.stderr


def test_refuses_capture_path_under_work_raw_absolute_and_case(tmp_path):
    """W6-audit F1: the guard must hold for ABSOLUTE paths, case variants
    (APFS is case-insensitive), and symlinks — not just the relative spelling."""
    import os
    csv_path = tmp_path / "depth_target_2099-01-01.csv"
    csv_path.write_text("rank,market_ticker,liquidity_tier\n1,AAA,High\n")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cap in [
        os.path.join(root, "work", "raw", "date=2099-01-01", "sneaky.ndjson"),
        os.path.join(root, "work", "RAW", "date=2099-01-01", "sneaky.ndjson"),
    ]:
        p = _run(["--operator-approved", "--dry-run", "--csv", str(csv_path),
                  "--capture", cap])
        assert p.returncode == 2, "guard bypassed for %s" % cap
        assert "REFUSED" in p.stderr and "work/raw" in p.stderr
    # symlink escape: tmp symlink pointing into work/raw
    link = tmp_path / "innocent"
    os.symlink(os.path.join(root, "work", "raw"), link)
    p = _run(["--operator-approved", "--dry-run", "--csv", str(csv_path),
              "--capture", str(link / "date=2099-01-01" / "sneaky.ndjson")])
    assert p.returncode == 2, "guard bypassed via symlink"
    # F1b: a RELATIVE work/raw path invoked from a FOREIGN cwd — the child
    # ws_shadow runs cwd=ROOT, so this resolves into the production tree there
    p = _run(["--operator-approved", "--dry-run", "--csv", str(csv_path),
              "--capture", "work/raw/date=2099-01-01/sneaky.ndjson"],
             cwd=str(tmp_path))
    assert p.returncode == 2, "guard bypassed via relative path from foreign cwd"


def test_dry_run_opens_no_socket_and_warns_on_stale_list(tmp_path):
    csv_path = tmp_path / "depth_target_2000-01-01.csv"  # stale by construction
    csv_path.write_text(
        "rank,market_ticker,liquidity_tier\n1,AAA,High\n2,BBB,Mid\n3,CCC,Low\n")
    cap = tmp_path / "cap.ndjson"
    p = _run(["--operator-approved", "--dry-run", "--markets", "2",
              "--csv", str(csv_path), "--capture", str(cap)])
    assert p.returncode == 0
    assert "dry-run: no socket opened" in p.stdout
    assert "KALSHI_WS_CHANNELS=orderbook_delta" in p.stdout
    assert "KALSHI_MODE=data_collect" in p.stdout
    assert "2 markets" in p.stdout
    assert "not dated today" in p.stderr            # stale-list warning
    assert not cap.exists()                          # nothing captured


def test_analyze_mode_needs_no_gate(fixture_capture):
    p = _run(["--analyze", fixture_capture])
    assert p.returncode == 0
    assert "REFUSED" not in p.stderr
    assert "by liquidity tier" in p.stdout
    assert "2.50 msg/s" in p.stdout or "2.5" in p.stdout
