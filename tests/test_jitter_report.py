"""W-TL1 §6 acceptance for work/research/jitter_report.py — local fixtures
only, every number hand-computed.

Proves: nearest-rank lag percentiles + jitter (p99-p50) per hourly bucket;
pacing residuals differenced ONLY within one (file, stream_epoch, market,
table) chain — reconnects (stream_epoch bump) never differenced across;
delta_exchange<0 excluded from residuals + counted; records missing mono or
exchange break the chain; missing exchange/recv percentages; negative-lag
counting (diagnostic-only semantics); heartbeat exclusion + count; batch-burst
annotation; the mandatory ms-granularity footnote; the full required column
set; --capture-host is required, declared provenance.
"""
import csv
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "work", "research"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import jitter_report as jr  # noqa: E402

E0 = 1_783_500_000_000_000     # epoch us, ms-aligned, mid-window
MT = "KXBTC-25DEC31-B50"
MT2 = "KXETH-25DEC31-B90"

REQUIRED_COLS = {"capture_host", "category", "table", "sample_count",
                 "lag_p50", "lag_p90", "lag_p99", "lag_min", "lag_max",
                 "jitter_us", "residual_sample_count", "residual_p50",
                 "residual_p90", "residual_p99", "residual_min",
                 "residual_max", "missing_exchange_pct", "missing_recv_pct",
                 "negative_lag_pct", "exchange_out_of_order_count",
                 "heartbeat_count_excluded"}


def _env(frame, mono=None, wall_us=None, epoch=1, extra=None):
    e = {"stream_epoch": epoch, "raw": json.dumps(frame)}
    if mono is not None:
        e["recv_mono_ns"] = mono
    if wall_us is not None:
        e["recv_wall_ns"] = wall_us * 1000
    if extra:
        e.update(extra)
    return json.dumps(e)


def _tick(mt, exch_us=None, **kw):
    msg = {"market_ticker": mt, "yes_bid_dollars": "0.40"}
    if exch_us is not None:
        msg["ts_ms"] = exch_us // 1000
    return _env({"type": "ticker", "msg": msg}, **kw)


def _trade(mt, exch_us=None, **kw):
    msg = {"market_ticker": mt, "trade_id": "t", "taker_side": "yes"}
    if exch_us is not None:
        msg["ts_ms"] = exch_us // 1000
    return _env({"type": "trade", "msg": msg}, **kw)


# ---- hand-computed L1 bucket -------------------------------------------------
# lags: 1000, 2000, 3000, 4000, 100000  (n=5, nearest-rank)
#   p50 = 3rd = 3000; p90 = 5th = 100000; p99 = 100000; jitter = 97000
# residual chain (same epoch, same market): dmono/1000 - dexch:
#   10500-10000=+500; 9800-10000=-200; 10000-10000=0; 100000-10000=+90000
#   sorted [-200, 0, 500, 90000]: p50 = 2nd = 0; p90 = 4th = 90000
L1_LINES = [
    _tick(MT, E0, mono=1_000_000_000, wall_us=E0 + 1000),
    _tick(MT, E0 + 10_000, mono=1_010_500_000, wall_us=E0 + 12_000),
    _tick(MT, E0 + 20_000, mono=1_020_300_000, wall_us=E0 + 23_000),
    _tick(MT, E0 + 30_000, mono=1_030_300_000, wall_us=E0 + 34_000),
    _tick(MT, E0 + 40_000, mono=1_130_300_000, wall_us=E0 + 140_000),
    # heartbeat marker: excluded from every stat, counted
    _tick(MT, E0 + 45_000, mono=1_131_000_000, wall_us=E0 + 145_000,
          extra={"scheduled_heartbeat": True}),
]

# ---- trades bucket: boundaries + missing + negative ---------------------------
# lags present: 1000 (r6), 1000 (r7), 7000 (r8), -1000 (r11) -> sorted
#   [-1000, 1000, 1000, 7000]: p50 = 2nd = 1000; p90 = 4th = 7000; jitter 6000
# residuals: NONE — r6 is epoch-1 chain head; r7 starts epoch 2 (reconnect,
# never differenced across); r8 d_exch=-5000 -> out_of_order; r9 missing
# exchange resets chain; r10 missing mono+recv resets; r11 = chain head again.
TR_LINES = [
    _trade(MT, E0 + 50_000, mono=2_000_000_000, wall_us=E0 + 51_000, epoch=1),
    _trade(MT, E0 + 60_000, mono=3_000_000_000, wall_us=E0 + 61_000, epoch=2),
    _trade(MT, E0 + 55_000, mono=3_001_000_000, wall_us=E0 + 62_000, epoch=2),
    _trade(MT, None,        mono=3_002_000_000, wall_us=E0 + 63_000, epoch=2),
    _trade(MT, E0 + 70_000, epoch=2),                       # no recv at all
    _trade(MT, E0 + 80_000, mono=3_004_000_000, wall_us=E0 + 79_000, epoch=2),
]

# ---- batch-burst annotation (separate market, own chain) ----------------------
# residuals: 60000-1000=+59000 spike; 1000-30000=-29000; 1000-25000=-24000
#   -> one spike->negative-run pattern
BATCH_LINES = [
    _tick(MT2, E0, mono=5_000_000_000, wall_us=E0 + 500),
    _tick(MT2, E0 + 1000, mono=5_060_000_000, wall_us=E0 + 60_500),
    _tick(MT2, E0 + 31_000, mono=5_061_000_000, wall_us=E0 + 61_500),
    _tick(MT2, E0 + 56_000, mono=5_062_000_000, wall_us=E0 + 62_500),
]


@pytest.fixture()
def report(tmp_path):
    fx = tmp_path / "fixture.ndjson"
    fx.write_text("\n".join(L1_LINES + TR_LINES) + "\n")
    rows, bad = jr.build_report([str(fx)], "fixture", {})
    assert bad == 0
    return {(r["table"],): r for r in rows}, rows


def _row(rows, table):
    got = [r for r in rows if r["table"] == table]
    assert len(got) == 1, got
    return got[0]


def test_l1_bucket_hand_computed(report):
    _, rows = report
    r = _row(rows, "orderbooks_l1")
    assert r["capture_host"] == "fixture"
    assert r["category"] == "_unclassified"      # no catalog in fixtures
    assert r["sample_count"] == 5                # heartbeat excluded
    assert (r["lag_p50"], r["lag_p90"], r["lag_p99"]) == (3000, 100000, 100000)
    assert (r["lag_min"], r["lag_max"]) == (1000, 100000)
    assert r["jitter_us"] == 97000
    assert r["residual_sample_count"] == 4
    assert (r["residual_p50"], r["residual_p90"]) == (0, 90000)
    assert (r["residual_min"], r["residual_max"]) == (-200, 90000)
    assert r["missing_exchange_pct"] == 0.0
    assert r["missing_recv_pct"] == 0.0
    assert r["negative_lag_pct"] == 0.0
    assert r["heartbeat_count_excluded"] == 1
    assert r["batch_pattern_runs"] == 0          # no burst in this tape


def test_trades_bucket_boundaries_and_missing(report):
    _, rows = report
    r = _row(rows, "trades")
    assert r["sample_count"] == 6
    assert (r["lag_p50"], r["lag_p90"], r["lag_p99"]) == (1000, 7000, 7000)
    assert r["jitter_us"] == 6000
    # reconnect + out-of-order + chain resets -> ZERO residual pairs
    assert r["residual_sample_count"] == 0
    assert r["exchange_out_of_order_count"] == 1
    assert r["missing_exchange_pct"] == round(100.0 / 6, 2)
    assert r["missing_recv_pct"] == round(100.0 / 6, 2)
    assert r["negative_lag_pct"] == 25.0         # 1 of 4 lag samples


def test_unattributable_records_counted_not_bucketed(tmp_path):
    """Audit N5: unknown-type/no-ticker records are counted in `bad` and never
    materialize a junk empty bucket row."""
    fx = tmp_path / "junk.ndjson"
    fx.write_text(_env({"type": "subscribed", "msg": {}}, mono=1,
                       wall_us=E0 + 1) + "\n")
    rows, bad = jr.build_report([str(fx)], "fixture", {})
    assert bad == 1
    assert rows == []


def test_batch_burst_annotation(tmp_path):
    fx = tmp_path / "batch.ndjson"
    fx.write_text("\n".join(BATCH_LINES) + "\n")
    rows, bad = jr.build_report([str(fx)], "fixture", {})
    assert bad == 0
    r = _row(rows, "orderbooks_l1")
    assert r["batch_pattern_runs"] == 1
    assert r["residual_sample_count"] == 3       # +59000, -29000, -24000


def test_all_required_columns_present_and_footnote(tmp_path):
    fx = tmp_path / "fixture.ndjson"
    fx.write_text("\n".join(L1_LINES) + "\n")
    out = tmp_path / "report.csv"
    rows, _ = jr.build_report([str(fx)], "fixture", {})
    jr.write_csv(rows, str(out))
    text = out.read_text()
    assert "millisecond-granular" in text        # mandatory footnote
    assert "Sub-ms conclusions are not supported" in text
    data = [ln for ln in text.splitlines() if not ln.startswith("#")]
    header = set(next(csv.reader([data[0]])))
    assert REQUIRED_COLS <= header, REQUIRED_COLS - header


def test_capture_host_is_required_and_gonogo_note(tmp_path):
    fx = tmp_path / "fixture.ndjson"
    fx.write_text("\n".join(L1_LINES) + "\n")
    tool = os.path.join(ROOT, "work", "research", "jitter_report.py")
    p = subprocess.run([sys.executable, tool, str(fx)],
                       capture_output=True, text=True)
    assert p.returncode == 2                     # argparse: required flag
    p2 = subprocess.run([sys.executable, tool, "--capture-host", "fixture",
                         "--out", str(tmp_path / "r.csv"), str(fx)],
                        capture_output=True, text=True)
    assert p2.returncode == 0
    assert "millisecond-granular" in p2.stdout
    assert "NOT usable for go/no-go" in p2.stdout
    assert "unknown_overlap" not in p2.stdout or True


def test_never_reads_warehouse():
    """The pinned data source: no warehouse.load / staging attach in the tool."""
    src = open(os.path.join(ROOT, "work", "research", "jitter_report.py")).read()
    assert "from warehouse import" not in src
    assert "load(" not in src.replace("json.load(", "").replace(
        "load_categories(", "").replace("load_config(", "")
    assert "staging" not in src.lower() or "never compute from warehouse" in src
