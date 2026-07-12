"""Tests for the Event Intelligence Dashboard builder (event_intel.py).

Fixture provenance: the miniature warehouses written into tmp_path reproduce
the EXACT archive layout and column schemas of work/warehouse/facts (see
docs/warehouse_schema.md); row values are hand-reduced from real archived
2026-07-06 Sports rows (structure, types and edge cases preserved: duplicate
trade ids, conflicting bodies, E4 scaling, snapshot/delta L2 rows). A
real-archive smoke test runs additionally when the local warehouse exists.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "event_intel", HERE / "workbench" / "event_intel.py")
ei = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ei
SPEC.loader.exec_module(ei)

BASE_US = int(datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc).timestamp() * 1_000_000)
TRADE_HEADER = (
    "ts_utc,market_ticker,series_ticker,event_ticker,category,subcategory,"
    "group,trade_id,yes_price_e4,no_price_e4,count_e4,taker_side\n"
)


# ---------------------------------------------------------------------------
# episode identity
# ---------------------------------------------------------------------------

def test_episode_key_groups_all_series_of_one_match():
    assert ei.episode_key("KXWCGAME-26JUL06USABEL") == ("26JUL06:USABEL", None)
    assert ei.episode_key("KXWCADVANCE-26JUL06USABEL") == ("26JUL06:USABEL", None)
    assert ei.episode_key("KXWCTOTAL-26JUL06USABEL") == ("26JUL06:USABEL", None)


def test_episode_key_strips_start_time_digits():
    key, hint = ei.episode_key("KXMLBTOTAL-26JUL052130BOSLAA")
    assert key == "26JUL05:BOSLAA"
    assert hint == "2130"
    # same match, different series, no embedded time -> same episode
    assert ei.episode_key("KXMLBNOTIME-26JUL05BOSLAA")[0] == "26JUL05:BOSLAA"


def test_non_match_tickers_are_not_episodes():
    assert ei.episode_key("KXMENWORLDCUP-26") == (None, None)
    assert ei.episode_key("") == (None, None)
    assert ei.episode_key("NOHYPHEN") == (None, None)


# ---------------------------------------------------------------------------
# fixture warehouse
# ---------------------------------------------------------------------------

def _write_trades(root: Path, rows: list[str], sport: str = "Soccer") -> None:
    d = (root / "work" / "warehouse" / "facts" / "trades" / "category=Sports"
         / f"subcategory={sport}" / "date=2026-07-06")
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / f"trades__Sports__{sport}__2026-07-06.csv.gz", "wt",
                   encoding="utf-8", newline="") as handle:
        handle.write(TRADE_HEADER)
        handle.writelines(rows)


def _write_l1(root: Path, rows: list[tuple], sport: str = "Soccer") -> None:
    import duckdb
    d = (root / "work" / "warehouse" / "facts" / "orderbooks_l1"
         / "category=Sports" / f"subcategory={sport}" / "date=2026-07-06")
    d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("""CREATE TABLE l1 (ts_utc BIGINT, market_ticker VARCHAR,
        event_ticker VARCHAR, subcategory VARCHAR, yes_bid_e4 INTEGER,
        yes_bid_qty_e4 BIGINT, yes_ask_e4 INTEGER, yes_ask_qty_e4 BIGINT,
        price_e4 INTEGER, is_snapshot BOOLEAN)""")
    con.executemany(
        "INSERT INTO l1 VALUES (?,?,?,'%s',?,?,?,?,?,?)" % sport, rows)
    out = d / f"orderbooks_l1__Sports__{sport}__2026-07-06.parquet"
    con.execute(f"COPY l1 TO '{out}' (FORMAT PARQUET)")
    con.close()


def _write_l2(root: Path, rows: list[tuple], sport: str = "Soccer") -> None:
    import duckdb
    d = (root / "work" / "warehouse" / "facts" / "orderbooks_full"
         / "category=Sports" / f"subcategory={sport}" / "date=2026-07-06")
    d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("""CREATE TABLE l2 (ts_utc BIGINT, market_ticker VARCHAR,
        event_ticker VARCHAR, msg_type VARCHAR, side VARCHAR,
        price_e4 INTEGER, delta_e4 BIGINT, yes_levels VARCHAR,
        no_levels VARCHAR)""")
    con.executemany("INSERT INTO l2 VALUES (?,?,?,?,?,?,?,?,?)", rows)
    out = d / f"orderbooks_full__Sports__{sport}__2026-07-06.parquet"
    con.execute(f"COPY l2 TO '{out}' (FORMAT PARQUET)")
    con.close()


EVENT_A = "KXTESTGAME-26JUL06AAABBB"      # head-to-head series
EVENT_B = "KXTESTTOTAL-26JUL06AAABBB"     # total series, SAME match
MKT_A = f"{EVENT_A}-AAA"
MKT_B = f"{EVENT_B}-5"


def _fixture_root(tmp_path: Path) -> Path:
    rows = []

    def tr(us_off, mkt, ev, tid, yes, count, side):
        rows.append(f"{BASE_US + us_off},{mkt},S,{ev},Sports,Soccer,G,{tid},"
                    f"{yes},{10000 - yes},{count},{side}\n")

    # 30 small baseline trades then one large print on market A
    for i in range(30):
        tr(i * 60_000_000, MKT_A, EVENT_A, f"base{i}", 4000, 20000, "yes")
    tr(31 * 60_000_000, MKT_A, EVENT_A, "big1", 4200, 10_000_0000, "no")
    # duplicate rows of one trade id (identical body -> collapse to one)
    tr(32 * 60_000_000, MKT_A, EVENT_A, "dup1", 4100, 30000, "yes")
    tr(32 * 60_000_000, MKT_A, EVENT_A, "dup1", 4100, 30000, "yes")
    # conflicting bodies under one trade id -> EXCLUDED and counted
    tr(33 * 60_000_000, MKT_A, EVENT_A, "conf1", 4100, 30000, "yes")
    tr(33 * 60_000_000, MKT_A, EVENT_A, "conf1", 4100, 99999, "yes")
    # second event ticker of the same match
    for i in range(5):
        tr(i * 60_000_000, MKT_B, EVENT_B, f"tb{i}", 5000, 50000, "no")
    _write_trades(tmp_path, rows)

    l1 = [
        (BASE_US, MKT_A, EVENT_A, 4000, 100_0000, 4200, 200_0000, 4100, True),
        # same-price bid qty increase -> l1_bid_add
        (BASE_US + 60_000_000, MKT_A, EVENT_A, 4000, 900_0000, 4200, 200_0000, 4100, False),
        # same-price ask qty decrease -> l1_ask_drop
        (BASE_US + 120_000_000, MKT_A, EVENT_A, 4000, 900_0000, 4200, 50_0000, 4100, False),
        # >65 min silence -> capture gap, then heartbeat
        (BASE_US + 2 * 3600_000_000 + 120_000_000, MKT_A, EVENT_A,
         4000, 900_0000, 4200, 50_0000, 4100, True),
        (BASE_US, MKT_B, EVENT_B, 3000, 10_0000, 3500, 10_0000, 3200, True),
    ]
    _write_l1(tmp_path, l1)

    l2 = [
        (BASE_US, MKT_A, EVENT_A, "snapshot", None, None, None,
         "[[4000,1000000],[3900,500000]]", "[[5800,2000000]]"),
        (BASE_US + 1_000_000, MKT_A, EVENT_A, "delta", "yes", 4000, 500000, None, None),
        (BASE_US + 2_000_000, MKT_A, EVENT_A, "delta", "yes", 3900, -200000, None, None),
        # depletion: level goes to zero
        (BASE_US + 3_000_000, MKT_A, EVENT_A, "delta", "yes", 3900, -300000, None, None),
    ]
    _write_l2(tmp_path, l2)
    return tmp_path


def _build(tmp_path: Path):
    builder = ei.EventIntelBuilder(
        repo_root=tmp_path,
        out_dir=tmp_path / "out",
        sports=("Soccer",),
        episodes_per_sport=3,
    )
    return builder.build(), builder


# ---------------------------------------------------------------------------
# grouping / dedup / evidence separation
# ---------------------------------------------------------------------------

def test_event_grouping_spans_multiple_event_tickers(tmp_path):
    index, builder = _build(_fixture_root(tmp_path))
    eps = index["sports"][0]["episodes"]
    assert len(eps) == 1, "both event tickers must group into ONE episode"
    ep = json.loads((tmp_path / "out" / "data" / "episodes"
                     / f"{eps[0]['safe_key']}.json").read_text())
    assert ep["key"] == "26JUL06:AAABBB"
    tickers = {e["ticker"] for e in ep["identity"]["events"]}
    assert tickers == {EVENT_A, EVENT_B}
    markets = {m["ticker"] for m in ep["markets"]}
    assert markets == {MKT_A, MKT_B}
    assert "never by market_ticker alone" in ep["identity"]["derivation"]["rule"]


def test_duplicate_ids_collapse_and_conflicts_are_excluded_and_counted(tmp_path):
    index, _ = _build(_fixture_root(tmp_path))
    q = index["trade_quality"]["Soccer"]
    assert q["raw_rows"] == 40  # 30 base + big1 + 2xdup1 + 2xconf1 + 5 tb
    # 30 base + big1 + dup1 + 5 tb = 37 safe unique ids (conf1 excluded)
    assert q["safe_unique_trade_ids"] == 37
    assert q["conflicting_trade_ids_excluded"] == 1


def test_trades_and_l1_proxies_are_distinct_evidence_types(tmp_path):
    _, _ = _build(_fixture_root(tmp_path))
    ep = json.loads(next((_p for _p in
        (Path(str(_f)) for _f in (list((Path(str(tmp_path)) / "out" / "data" /
        "episodes").glob("*.json"))))), None).read_text())
    mk = [m for m in ep["markets"] if m["ticker"] == MKT_A][0]
    types = {m["type"] for m in mk["markers"]}
    assert "trade" in types
    assert "l1_bid_add" in types
    assert "l1_ask_drop" in types
    trade = [m for m in mk["markers"] if m["type"] == "trade"][0]
    add = [m for m in mk["markers"] if m["type"] == "l1_bid_add"][0]
    drop = [m for m in mk["markers"] if m["type"] == "l1_ask_drop"][0]
    assert any("ACTUAL TRADE PRINT" in f for f in trade["flags"])
    assert not any("AGGREGATE L1 PROXY" in f for f in trade["flags"])
    assert any("AGGREGATE L1 PROXY" in f for f in add["flags"])
    # drops carry the cancel-vs-depletion ambiguity flag
    assert any("plausible" in f for f in drop["flags"])


def test_no_marker_ever_claims_actor_identity(tmp_path):
    _, _ = _build(_fixture_root(tmp_path))
    for p in (tmp_path / "out" / "data" / "episodes").glob("*.json"):
        ep = json.loads(p.read_text())
        for m in ep["tape"]:
            if m["type"] == "gap":
                continue
            joined = " ".join(m.get("flags", []))
            assert "NO ACTOR IDENTITY" in joined
            for banned in ("whale", "account", "trader", "person"):
                assert banned not in joined.lower()


def test_large_print_has_descriptive_percentile_and_cash(tmp_path):
    _, _ = _build(_fixture_root(tmp_path))
    ep = json.loads(next(iter((tmp_path / "out" / "data" / "episodes")
                              .glob("*.json"))).read_text())
    mk = [m for m in ep["markets"] if m["ticker"] == MKT_A][0]
    big = [m for m in mk["markers"] if m["id"] == "big1"]
    assert big, "the outlier print must be retained exactly"
    big = big[0]
    assert big["contracts"] == 10000.0
    assert big["d_pct"] == 1.0
    assert big["d_n"] == 32  # 30 base + big1 + dup1 (safe trades on MKT_A)
    # taker no: cash = contracts * no_price
    assert abs(big["cash"] - 10000.0 * 0.58) < 1e-6


# ---------------------------------------------------------------------------
# thresholds: causal cohorts refuse labels on small samples
# ---------------------------------------------------------------------------

def test_small_cohorts_refuse_causal_tiers(tmp_path):
    _, _ = _build(_fixture_root(tmp_path))
    ep = json.loads(next(iter((tmp_path / "out" / "data" / "episodes")
                              .glob("*.json"))).read_text())
    for mk in ep["markets"]:
        for m in mk["markers"]:
            assert m.get("c_tier") is None, \
                "n<500 prior cohort must never earn a causal tier label"
    dist = ep["markets"][0]["dist"]["trade"]
    assert any("cohort below p99 minimum" in n for n in dist["notes"])


def test_causal_annotate_uses_only_strictly_prior_hours():
    hour_us = 3_600_000_000
    src = []
    for i in range(600):  # 600 prior observations in hour 0, sizes 1..600
        src.append((i * 1000, float(i + 1)))
    events = [
        {"t": (hour_us // 1000) + 1, "contracts": 599.0},   # hour 1
        {"t": 1, "contracts": 10_000.0},                    # hour 0: no prior
    ]
    ei.causal_annotate(events, src)
    late, early = events[0], events[1]
    assert early["c_n"] == 0 and early["c_tier"] is None
    assert late["c_n"] == 600
    assert late["c_tier"] == "p99"      # >= p99 of prior cohort, n>=500
    assert late["c_thr"] is not None
    # p99.5 requires n>=1000 -> must NOT be awarded even at max size
    events2 = [{"t": (hour_us // 1000) + 1, "contracts": 1e9}]
    ei.causal_annotate(events2, src)
    assert events2[0]["c_tier"] == "p99"


# ---------------------------------------------------------------------------
# gaps, L2, empty channels
# ---------------------------------------------------------------------------

def test_gap_detection_flags_heartbeat_violations(tmp_path):
    _, _ = _build(_fixture_root(tmp_path))
    ep = json.loads(next(iter((tmp_path / "out" / "data" / "episodes")
                              .glob("*.json"))).read_text())
    mk = [m for m in ep["markets"] if m["ticker"] == MKT_A][0]
    assert len(mk["gaps"]) == 1
    gap_tape = [m for m in ep["tape"] if m["type"] == "gap"]
    assert gap_tape and "DATA GAP / BOOK STATE UNTRUSTED" in gap_tape[0]["flags"][0]


def test_l2_reconstruction_separates_add_remove_depletion(tmp_path):
    _, _ = _build(_fixture_root(tmp_path))
    ep = json.loads(next(iter((tmp_path / "out" / "data" / "episodes")
                              .glob("*.json"))).read_text())
    mk = [m for m in ep["markets"] if m["ticker"] == MKT_A][0]
    l2 = mk["l2"]
    assert l2["available"] is True
    kinds = {m["type"] for m in l2["markers"]}
    assert "l2_depletion" in kinds
    assert l2["depth"], "depth series must exist"
    # snapshot: yes 100+50, after +50 add: 150+30 ... depth in contracts
    assert l2["seq"]["available"] is False
    assert "seq_unavailable" in l2["seq"]["note"]
    other = [m for m in ep["markets"] if m["ticker"] == MKT_B][0]
    assert other["l2"]["available"] is False


def test_missing_channels_render_honest_empty_states(tmp_path):
    index, _ = _build(_fixture_root(tmp_path))
    inv = index["inventory"]
    assert inv["rfq"]["status"] == "UNAVAILABLE"
    assert inv["score_game_state"]["status"] == "UNAVAILABLE"
    ep = json.loads(next(iter((tmp_path / "out" / "data" / "episodes")
                              .glob("*.json"))).read_text())
    assert ep["score"]["available"] is False
    assert ep["score"]["events"] == []
    assert "NOT CAPTURED" in ep["score"]["status"]
    assert ep["rfq"]["available"] is False
    assert ep["rfq"]["events"] == []


def test_score_adapter_never_fabricates_markers(tmp_path):
    root = _fixture_root(tmp_path)
    inputs = tmp_path / "out" / "inputs" / "score_events"
    inputs.mkdir(parents=True)
    # wrong schema -> rejected outright
    (inputs / "26JUL06_AAABBB.json").write_text(json.dumps(
        {"schema_version": "wrong", "events": [{"payload": {"s": 1},
         "provider_ts_utc": "2026-07-06T10:00:00Z"}]}))
    _, _ = _build(root)
    ep = json.loads((tmp_path / "out" / "data" / "episodes"
                     / "26JUL06_AAABBB.json").read_text())
    assert ep["score"]["available"] is False and ep["score"]["events"] == []
    # right schema, but events without a real payload are dropped
    (inputs / "26JUL06_AAABBB.json").write_text(json.dumps(
        {"schema_version": "event-intel-score-input-v1", "provider": "test",
         "events": [
             {"provider_ts_utc": "2026-07-06T10:05:00Z"},          # no payload
             {"payload": {"score": "1-0"},
              "provider_ts_utc": "2026-07-06T10:06:00Z",
              "mapping_confidence": 0.9, "post_hoc": True},
         ]}))
    _, _ = _build(root)
    ep = json.loads((tmp_path / "out" / "data" / "episodes"
                     / "26JUL06_AAABBB.json").read_text())
    assert len(ep["score"]["events"]) == 1
    assert ep["score"]["post_hoc"] is True


def test_distributions_never_ship_a_scalar_alone(tmp_path):
    _, _ = _build(_fixture_root(tmp_path))
    ep = json.loads(next(iter((tmp_path / "out" / "data" / "episodes")
                              .glob("*.json"))).read_text())
    for mk in ep["markets"]:
        for name, d in mk["dist"].items():
            for field in ("n", "p50", "p99", "max", "unit", "population",
                          "provenance", "ecdf", "hist", "notes"):
                assert field in d, f"{name} missing {field}"


# ---------------------------------------------------------------------------
# frontend contract + offline assets
# ---------------------------------------------------------------------------

def test_frontend_is_offline_and_carries_required_controls(tmp_path):
    html = (HERE / "workbench" / "intel.html").read_text(encoding="utf-8")
    for control in ("mode-sel", "thr-sel", "tz-sel", "toggles", "tape-rows",
                    "dist-ecdf", "dist-hist", "console-status",
                    "quality-pane", "def-pane"):
        assert control in html
    for label in ("LOOK-AHEAD / EXPLORATORY",
                  "PRE-TL1 · EXCHANGE-OR-COARSE TIME ONLY",
                  "LARGE-ACTIVITY CANDIDATE — NO ACTOR IDENTITY",
                  "DATA GAP / BOOK STATE UNTRUSTED",
                  "AGGREGATE L1 PROXY"):
        assert label in html, label
    assert "https://" not in html and "http://" not in html
    assert 'src="assets/echarts.min.js"' in html


def test_build_renders_dashboard_and_jsonp_artifacts(tmp_path):
    _, builder = _build(_fixture_root(tmp_path))
    out = builder.out_dir
    assert (out / "index.html").is_file()
    assert (out / "assets" / "echarts.min.js").is_file()
    assert (out / "data" / "index.js").is_file()
    js = (out / "data" / "index.js").read_text()
    assert js.startswith('window.__EI_LOAD__("index",')
    for ep_js in (out / "data" / "episodes").glob("*.js"):
        assert ep_js.read_text().startswith('window.__EI_LOAD__("episode:')


# ---------------------------------------------------------------------------
# real archive smoke (skipped when the local warehouse is absent)
# ---------------------------------------------------------------------------

REAL = ei.REPO_ROOT / "work" / "warehouse" / "facts" / "trades" / "category=Sports"


@pytest.mark.skipif(not REAL.is_dir(), reason="local Sports archive absent")
def test_real_archive_build_smoke(tmp_path):
    builder = ei.EventIntelBuilder(
        out_dir=tmp_path / "out", sports=("Tennis",), episodes_per_sport=1)
    index = builder.build()
    eps = index["sports"][0]["episodes"]
    assert eps, "expected at least one Tennis episode in the local archive"
    ep = json.loads((tmp_path / "out" / "data" / "episodes"
                     / f"{eps[0]['safe_key']}.json").read_text())
    assert ep["markets"] and ep["markets"][0]["price"]
    assert ep["quality"]["timestamp_ladder"] == "PRE-TL1"
    assert index["inventory"]["trades"]["status"] == "REAL"
    assert index["inventory"]["rfq"]["status"] == "UNAVAILABLE"
