"""W-TL1 §4/§5 acceptance — DEMONSTRATED, not described.

The late-arrival scenario: a book update happens at exchange time T but only
reaches us 500ms later; two trades print through our would-be quotes in
between. Backtesting on the exchange clock fills both sides for a riskless
+$0.10 — profit we could NEVER have captured, because we had not yet seen the
book when the trades printed (look-ahead bias). On the recv clock with the
active-quote latency model (order_effective_not_before), the same tape
produces ZERO fills.

Also proves: recv-clock fail-closed on rows missing local_recv_ts_us (exit 3;
--allow-missing-recv drops them loudly; never a silent exchange fallback),
heartbeat rows replay at hour start and never count as missing, and the three
latency parameters come from config (PLACEHOLDER) with CLI/env override.

Fixtures are synthetic and local-only; no network, no orders.
"""
import json
import os
import sys

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ingest  # noqa: E402
import mm_backtest as bt  # noqa: E402
import warehouse as wh  # noqa: E402
import warehouse_common as wc  # noqa: E402

DAY = "2026-07-02"
T = wc.day_start_us(DAY) + 3_600_000_000       # 01:00 UTC, ms-aligned
MS = 1000                                       # us per ms
MT = "KXBTC-25DEC31-B50"

# test latency chain: 1ms processing + 1ms signing + 8ms RTT = 10ms
LAT = ["--processing-chain-p99-us", "1000", "--signing-p99-us", "1000",
       "--signed-post-rtt-p99-us", "8000"]
LAT_US = 10_000


@pytest.fixture(autouse=True)
def _reset_wh_conn():
    if wh._CON is not None:
        try:
            wh._CON.close()
        except Exception:
            pass
    wh._CON = None
    wh._ATTACHED.clear()
    yield


def _env(mono, wall_us, frame):
    return json.dumps({"recv_mono_ns": mono, "recv_wall_ns": wall_us * 1000,
                       "raw": json.dumps(frame)})


def _tick(ts_us, wall_us, bid, ask, mono):
    return _env(mono, wall_us, {"type": "ticker", "msg": {
        "market_ticker": MT, "ts_ms": ts_us // 1000,
        "yes_bid_dollars": "%.4f" % bid, "yes_ask_dollars": "%.4f" % ask,
        "yes_bid_size_fp": "100.00", "yes_ask_size_fp": "100.00",
        "price_dollars": "%.4f" % ((bid + ask) / 2)}})


def _trade(ts_us, wall_us, px, side, tid, mono):
    return _env(mono, wall_us, {"type": "trade", "msg": {
        "market_ticker": MT, "ts_ms": ts_us // 1000, "trade_id": tid,
        "yes_price_dollars": "%.4f" % px, "no_price_dollars": "%.4f" % (1 - px),
        "count_fp": "1.00", "taker_side": side}})


LATE_ARRIVAL_TAPE = [
    # book update: exchange T, but WE only receive it at T+500ms
    _tick(T, T + 500 * MS, 0.40, 0.50, mono=1),
    # two trades print in the blind window, straight through the touch
    _trade(T + 100 * MS, T + 101 * MS, 0.38, "no", "x1", mono=2),
    _trade(T + 200 * MS, T + 201 * MS, 0.55, "yes", "y1", mono=3),
]


def _build_warehouse(tmp_path, lines):
    root = str(tmp_path / "warehouse")
    out = os.path.join(root, "catalog", "series_classified")
    os.makedirs(out)
    con = duckdb.connect()
    con.execute("CREATE TABLE c (series_ticker TEXT, category TEXT, "
                "subcategory TEXT, \"group\" TEXT, record_class TEXT)")
    con.execute("INSERT INTO c VALUES ('KXBTC','Crypto','BTC','BTC','A')")
    con.execute("COPY c TO '%s' (FORMAT PARQUET)"
                % os.path.join(out, "part-00000.parquet").replace("'", "''"))
    scon = duckdb.connect(os.path.join(root, "staging.duckdb"))
    ing = ingest.Ingester(scon, root)
    cap = str(tmp_path / "cap.ndjson")
    with open(cap, "w") as f:
        f.write("\n".join(lines) + "\n")
    ing.process_file(cap)
    scon.close()
    return root


@pytest.fixture()
def env_warehouse(tmp_path, monkeypatch):
    root = _build_warehouse(tmp_path, LATE_ARRIVAL_TAPE)
    monkeypatch.setenv("WAREHOUSE_ROOT", root)
    monkeypatch.setenv("STAGING_DB", os.path.join(root, "staging.duckdb"))
    monkeypatch.setenv("ARCHIVE_ROOT", os.path.join(root, "facts"))
    return root


def _run(capsys, *extra):
    rc = bt.main(["mm_backtest", "--date", DAY, "--markets", MT,
                  "--size", "1"] + LAT + list(extra))
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------- §5 the demo

def test_exchange_clock_manufactures_fake_profit(env_warehouse, capsys):
    rc, out = _run(capsys, "--clock", "exchange")
    assert rc == 0
    assert "TOTAL pessimistic: fills=2 pnl=$0.10" in out
    assert "DIAGNOSTIC ONLY" in out          # the banner is not optional
    assert "never go/no-go" in out


def test_recv_clock_with_not_before_produces_no_fill(env_warehouse, capsys):
    rc, out = _run(capsys, "--clock", "recv")
    assert rc == 0
    assert "TOTAL pessimistic: fills=0 pnl=$0.00" in out
    assert "optimistic: fills=0 pnl=$0.00" in out


def test_recv_is_the_default_clock(env_warehouse, capsys):
    rc, out = _run(capsys)
    assert rc == 0
    assert "clock=recv" in out
    assert "fills=0" in out


# --------------------------------------------------- run_market direct proofs

def test_run_market_lookahead_numbers():
    """Same scenario, hand-fed event times — exact numbers both clocks."""
    # exchange clock: quote active T+10ms; trades at T+100/200ms fill both sides
    ex = bt.run_market([(T, 4000, 5000)],
                       [(T + 100 * MS, 3800, "no"), (T + 200 * MS, 5500, "yes")],
                       size=1, max_inv=25, min_spread_e4=200, latency_us=LAT_US)
    assert ex["pessimistic"] == {"fills": 2, "inventory": 0.0, "pnl": 0.10}
    # recv clock: book seen T+500ms -> active T+510ms; trades arrived T+101/201ms
    rv = bt.run_market([(T + 500 * MS, 4000, 5000)],
                       [(T + 101 * MS, 3800, "no"), (T + 201 * MS, 5500, "yes")],
                       size=1, max_inv=25, min_spread_e4=200, latency_us=LAT_US)
    assert rv["pessimistic"] == {"fills": 0, "inventory": 0.0, "pnl": 0.0}
    assert rv["optimistic"]["fills"] == 0


def test_not_before_boundary():
    """A trade exactly AT order_effective_not_before fills; 1us earlier never."""
    quote_t = T
    active = quote_t + LAT_US
    early = bt.run_market([(quote_t, 4000, 5000)], [(active - 1, 3800, "no")],
                          1, 25, 200, LAT_US)
    at = bt.run_market([(quote_t, 4000, 5000)], [(active, 3800, "no")],
                       1, 25, 200, LAT_US)
    assert early["pessimistic"]["fills"] == 0
    assert at["pessimistic"]["fills"] == 1


# ------------------------------------------------------- fail-closed contract

def test_missing_recv_fails_closed(tmp_path, monkeypatch, capsys):
    """A legacy-style row (no recv envelope fields) poisons the recv clock:
    the run must fail closed, and --allow-missing-recv must DROP (not remap)."""
    legacy = json.dumps({"raw": json.dumps({"type": "trade", "msg": {
        "market_ticker": MT, "ts_ms": (T + 300 * MS) // 1000, "trade_id": "leg1",
        "yes_price_dollars": "0.3800", "no_price_dollars": "0.6200",
        "count_fp": "1.00", "taker_side": "no"}})})
    root = _build_warehouse(tmp_path, LATE_ARRIVAL_TAPE + [legacy])
    monkeypatch.setenv("WAREHOUSE_ROOT", root)
    monkeypatch.setenv("STAGING_DB", os.path.join(root, "staging.duckdb"))
    monkeypatch.setenv("ARCHIVE_ROOT", os.path.join(root, "facts"))

    rc = bt.main(["mm_backtest", "--date", DAY, "--markets", MT, "--size", "1"]
                 + LAT + ["--clock", "recv"])
    err = capsys.readouterr()
    assert rc == 3
    assert "FAIL-CLOSED" in err.err
    assert "trades missing 1/3" in err.out

    rc2 = bt.main(["mm_backtest", "--date", DAY, "--markets", MT, "--size", "1"]
                  + LAT + ["--clock", "recv", "--allow-missing-recv"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "fills=0" in out2   # dropped, not remapped to exchange time


# ----------------------------------------------------- event_times unit rules

def test_event_times_heartbeat_and_missing():
    import pandas as pd
    df = pd.DataFrame({
        "ts_utc":           [T, T + 1, T + 2],
        "is_snapshot":      [True, False, False],
        "price_e4":         [None, 4500, 4500],
        "exchange_ts_us":   [None, T + 1, None],
        "local_recv_ts_us": [None, T + 500, None],
    })
    out, hb, miss = bt.event_times(df, "recv", is_l1=True)
    assert hb == 1 and miss == 1                       # row0 heartbeat, row2 missing
    assert out.t_event.iloc[0] == T                    # heartbeat replays at ts_utc
    assert out.t_event.iloc[1] == T + 500              # recv, not exchange
    ex, hb2, miss2 = bt.event_times(df, "exchange", is_l1=True)
    assert miss2 == 0                                  # legacy ts_utc IS exchange time
    assert ex.t_event.iloc[2] == T + 2


def test_legacy_priceless_snapshot_is_not_a_heartbeat():
    """Audit N2: a pre-TL1 FIRST-OBSERVATION snapshot without a price (all-NULL
    ladder but ts_utc off the hour boundary) must stay in the fail-closed
    missing class — never silently replayed at exchange time as a heartbeat."""
    import pandas as pd
    df = pd.DataFrame({
        "ts_utc":           [T + 12_345],       # NOT hour-aligned
        "is_snapshot":      [True],
        "price_e4":         [None],
        "exchange_ts_us":   [None],
        "local_recv_ts_us": [None],
    })
    out, hb, miss = bt.event_times(df, "recv", is_l1=True)
    assert hb == 0 and miss == 1


# ------------------------------------------------------------- config plumbing

def test_latency_config_placeholders_and_override(tmp_path, monkeypatch):
    cfg = bt.load_latency_config()                     # committed file
    assert cfg == {"processing_chain_p99_us": 2000, "signing_p99_us": 5000,
                   "signed_post_rtt_p99_us": 60000}
    p = tmp_path / "lat.yaml"
    p.write_text("processing_chain_p99_us: 111\nsigning_p99_us: 222\n"
                 "signed_post_rtt_p99_us: 333\n")
    assert bt.load_latency_config(str(p)) == {
        "processing_chain_p99_us": 111, "signing_p99_us": 222,
        "signed_post_rtt_p99_us": 333}
    monkeypatch.setenv("SIGNING_P99_US", "999")
    assert bt.load_latency_config(str(p))["signing_p99_us"] == 999
    for line in open(os.path.join(ROOT, "config", "backtest_latency.yaml")):
        if line.strip().startswith(tuple(bt.LATENCY_KEYS)):
            assert "PLACEHOLDER" in line               # honesty marker stays
