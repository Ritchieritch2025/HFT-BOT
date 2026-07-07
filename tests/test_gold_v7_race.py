"""W3.2: race/consistency report (V7, REPORT-ONLY) — tools/gold_v7_race.py
(PLAN_GOLD_DATA_CONTRACT §2.2 point 5 + §2.3 V7 row + W3.2). Contract encoded
here, written BEFORE the module exists (TDD):

  - Economic consistency per TRADE record: does the print sit at the as-of
    best? taker=yes => trade_yes_price_e4 == ask_px_e4[0]; taker=no =>
    trade_yes_price_e4 == bid_px_e4[0]. The as-of book is the state the
    trade record itself carries (the state referenced by (market_id,
    book_seq) — §2.2 item 4: a trade sees the PRE-trade book).
  - TWO populations, measured SEPARATELY and labeled: covered_book
    (F_BOOK_COVERED — reconstructed full-depth top) vs l1_asof (uncovered;
    the as-of top is the L1 state in slot 0).
  - races = prints outside the as-of touch; race rate broken down per
    category, subcategory, market, market class (A/B, category policy from
    config/market_classes.yaml — injectable), and liquidity tier.
  - Honesty split (D2): a trade whose as-of book is invalid
    (F_BOOK_VALID=0, incl. book_seq 0 = no state ever emitted) or whose
    reference side is empty is UNMEASURABLE — reported separately, NEVER
    counted as a race.
  - BINDING (§2.2 point 5): day one is REPORT-ONLY. NO hardcoded blocking
    threshold, NO threshold-enforcement code path (grep-proven below);
    exits are 0 unless the tool itself errors. PROPOSED thresholds
    (nearest-rank p95 of per-market race rates, per population per
    category, markets with >= MIN_MEASURABLE measurable trades) are listed
    under "FOR OPERATOR APPROVAL — not enforced". Slices whose race rate
    exceeds the proposal are marked unsafe_for_microstructure=true in the
    report AND in the manifest verdict field — marked, never failed.
  - Manifest: safety_verdicts.V7 filled IN PLACE (V5 pattern): certified
    md5s (manifest["files"]) + certified files + the V5 verdict stay
    byte-identical; GoldDayReader must still open (certification intact).
  - Anti-fake-green red proof: the committed v7_inverted_taker fixture has
    every taker_side FLIPPED versus prints that sit exactly at the correct
    as-of touch — the measured race rate flips to 1.0 (the MEASUREMENT
    catches the inversion; NOT a threshold failure: the CLI still exits 0).

Fixture regeneration (deterministic bytes):
    python3 tests/test_gold_v7_race.py
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:                      # __main__ fixture regeneration
    sys.path.insert(0, ROOT)

from tools.gold_dtype import EVENT_TYPE
from tools.gold_io import GoldDayReader, day_paths, write_day
from tools.gold_load import BookMsg, Event, L1Top, Trade
from tools.gold_merge import merge
from tools.gold_v7_race import (HEADING_FOR_APPROVAL, MIN_MEASURABLE,
                                PROPOSAL_PCT, main, measure_day,
                                propose_thresholds, render,
                                update_manifest_v7, write_report)

DEFECT = os.path.join(ROOT, "tests", "fixtures", "gold_defects",
                      "v7_inverted_taker")
DATE = "2026-07-06"
T0 = 1783300000000000
S = 1_000_000                                  # 1 second in µs

SNAP, TRADE = EVENT_TYPE["BOOK_SNAPSHOT"], EVENT_TYPE["TRADE"]
L1 = EVENT_TYPE["L1_TICKER"]
COV, L1M, PRE, INV = "KXV7-COV", "KXV7-L1", "KXV7-PRE", "KXV7-INV"
CLASS_A = {"Sports"}                           # injected category policy
SUBCAT = {COV: "Baseball", L1M: "Sitcoms", PRE: "Baseball", INV: "Baseball"}
DIMS = {COV: ("Sports", "High"), L1M: ("Entertainment", "Low"),
        PRE: ("Sports", "Mid"), INV: ("Sports", "Mid")}


def _snap(yes, no, mt, ts):
    return Event(SNAP, ts, mt, BookMsg(None, None, None, tuple(yes), tuple(no)))


def _l1(mt, ts, bid, bq, ask, aq):
    return Event(L1, ts, mt, L1Top(bid, bq, ask, aq, False))


def _trade(mt, ts, tid, yes_px, side):
    return Event(TRADE, ts, mt, Trade(tid, yes_px, 10000 - yes_px, 10000, side))


def _dim(*mts):
    return {mt: {"category": DIMS.get(mt, ("Sports", "High"))[0],
                 "close_time": "",
                 "liquidity_tier": DIMS.get(mt, ("Sports", "High"))[1]}
            for mt in mts}


def _pop(rep, name):
    return rep["populations"][name]


def _market(pop, mt):
    return next(e for e in pop["per_market"] if e["market_ticker"] == mt)


# ------------------------------------------------------------ day construction

def covered_three_trade_day(root):
    """The W3.2-specified hand-verified day. Book (covered): bid 4500 /
    ask 5000.  Trades, all taker=yes:
      T1 @5000 = at the as-of ask         -> NOT a race
      T2 @4700 = inside the spread        -> RACE
      T3 @4500 = at the BID with taker=yes -> RACE
    => n_measurable=3, n_races=2, race_rate=2/3."""
    book = [_snap([(4500, 100000)], [(5000, 50000)], COV, T0)]
    trades = [_trade(COV, T0 + 1 * S, "T1", 5000, "yes"),
              _trade(COV, T0 + 2 * S, "T2", 4700, "yes"),
              _trade(COV, T0 + 3 * S, "T3", 4500, "yes")]
    records, _ = merge([book, trades])
    write_day(records, _dim(COV), DATE, root)


def two_population_day(root):
    """COV covered (snapshot; 2 trades: one clean at ask, one racing inside)
    + L1M uncovered (L1 top 4000/4200; 3 trades: yes@4200 clean, no@4000
    clean, yes@4100 RACE) + PRE: a trade BEFORE any book state (as-of book
    invalid => unmeasurable, never a race) and a trade against an
    empty-ask L1 state with taker=yes (side empty => unmeasurable)."""
    cov_book = [_snap([(4500, 100000)], [(5000, 50000)], COV, T0)]
    cov_trades = [_trade(COV, T0 + 1 * S, "C1", 5000, "yes"),
                  _trade(COV, T0 + 2 * S, "C2", 4700, "yes")]
    l1_rows = [_l1(L1M, T0, 4000, 50000, 4200, 70000)]
    l1_trades = [_trade(L1M, T0 + 1 * S, "L1", 4200, "yes"),
                 _trade(L1M, T0 + 2 * S, "L2", 4000, "no"),
                 _trade(L1M, T0 + 3 * S, "L3", 4100, "yes")]
    pre_rows = [_l1(PRE, T0 + 2 * S, 3000, 10000, 10000, 0)]  # empty ask side
    pre_trades = [_trade(PRE, T0, "P1", 3000, "yes"),      # before any state
                  _trade(PRE, T0 + 3 * S, "P2", 3100, "yes")]  # ask side empty
    records, _ = merge([cov_book, cov_trades, l1_rows, l1_trades,
                        pre_rows, pre_trades])
    write_day(records, _dim(COV, L1M, PRE), DATE, root)


def twenty_one_market_day(root):
    """21 covered markets, one category (Sports), 20 measurable trades each.
    Markets M00..M19 print exactly at the as-of ask (race_rate 0.0); M20
    prints inside the spread every time (race_rate 1.0). Proposal pool = 21
    rates => nearest-rank p95 = sorted[ceil(.95*21)-1] = sorted[19] = 0.0;
    M20 (1.0 > 0.0) must be MARKED unsafe_for_microstructure — and nothing
    may fail."""
    sources = []
    for m in range(21):
        mt = "KXV7-M%02d" % m
        px = 4700 if m == 20 else 5000
        sources.append([_snap([(4500, 100000)], [(5000, 50000)], mt, T0)])
        sources.append([_trade(mt, T0 + (i + 1) * S, "T%02d-%02d" % (m, i),
                               px, "yes") for i in range(20)])
    records, _ = merge(sources)
    write_day(records, _dim(*("KXV7-M%02d" % m for m in range(21))),
              DATE, root)


# --------------------------------------------------- committed defect fixture

def build_defect_fixture(fixture_root=DEFECT):
    """v7_inverted_taker: book bid 4500 / ask 5000; 25 trades printing
    EXACTLY at the correct as-of touch, but with every taker_side FLIPPED
    (at-ask prints say taker=no, at-bid prints say taker=yes). With correct
    sides the day would measure race_rate 0.0; the inversion flips the
    measured race rate to 1.0 — the measurement itself goes red.
    Deterministic bytes."""
    shutil.rmtree(os.path.join(fixture_root, "date=%s" % DATE),
                  ignore_errors=True)
    book = [_snap([(4500, 100000)], [(5000, 50000)], INV, T0)]
    trades = [_trade(INV, T0 + (i + 1) * S, "TINV%02d" % i,
                     5000 if i % 2 == 0 else 4500,
                     "no" if i % 2 == 0 else "yes")       # FLIPPED sides
              for i in range(25)]
    records, _ = merge([book, trades])
    write_day(records, _dim(INV), DATE, fixture_root)
    return fixture_root


def correct_taker_twin_day(root):
    """Same prints as the fixture with the CORRECT taker sides — race 0.0."""
    book = [_snap([(4500, 100000)], [(5000, 50000)], INV, T0)]
    trades = [_trade(INV, T0 + (i + 1) * S, "TINV%02d" % i,
                     5000 if i % 2 == 0 else 4500,
                     "yes" if i % 2 == 0 else "no")       # correct sides
              for i in range(25)]
    records, _ = merge([book, trades])
    write_day(records, _dim(INV), DATE, root)


# ------------------------------------------------------------ the measurement

def test_hand_computed_three_trades(tmp_path):
    root = str(tmp_path)
    covered_three_trade_day(root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    cov = _pop(rep, "covered_book")
    m = _market(cov, COV)
    assert m["n_trades"] == 3 and m["n_measurable"] == 3
    assert m["n_races"] == 2                        # T2 inside, T3 at bid
    assert m["race_rate"] == round(2 / 3, 6)
    assert cov["totals"]["n_races"] == 2
    assert cov["totals"]["race_rate"] == round(2 / 3, 6)
    assert _pop(rep, "l1_asof")["totals"]["n_trades"] == 0


def test_taker_no_prints_at_bid_not_ask(tmp_path):
    root = str(tmp_path)
    book = [_snap([(4500, 100000)], [(5000, 50000)], COV, T0)]
    trades = [_trade(COV, T0 + 1 * S, "N1", 4500, "no"),   # at bid: clean
              _trade(COV, T0 + 2 * S, "N2", 5000, "no")]   # at ask: RACE
    records, _ = merge([book, trades])
    write_day(records, _dim(COV), DATE, root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    m = _market(_pop(rep, "covered_book"), COV)
    assert m["n_measurable"] == 2 and m["n_races"] == 1
    assert m["race_rate"] == 0.5


def test_populations_separated_and_labeled(tmp_path):
    root = str(tmp_path)
    two_population_day(root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    cov, l1 = _pop(rep, "covered_book"), _pop(rep, "l1_asof")
    # covered_book: only COV's 2 trades (1 race), never L1M's
    assert [e["market_ticker"] for e in cov["per_market"]] == [COV]
    assert cov["totals"] == {
        "n_markets": 1, "n_trades": 2, "n_measurable": 2, "n_races": 1,
        "n_unmeasurable": 0,
        "unmeasurable": {"book_invalid": 0, "side_empty": 0,
                         "bad_taker_side": 0},
        "race_rate": 0.5, "unsafe_for_microstructure": False}
    # l1_asof: L1M measured against the L1 as-of top; PRE all unmeasurable
    lm = _market(l1, L1M)
    assert lm["n_measurable"] == 3 and lm["n_races"] == 1
    assert lm["race_rate"] == round(1 / 3, 6)
    assert lm["market_class"] == "B"                # Entertainment not class A
    assert l1["totals"]["n_trades"] == 5            # 3 L1M + 2 PRE


def test_unmeasurable_never_a_race(tmp_path):
    root = str(tmp_path)
    two_population_day(root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    pre = _market(_pop(rep, "l1_asof"), PRE)
    assert pre["n_trades"] == 2 and pre["n_measurable"] == 0
    assert pre["n_races"] == 0                      # NEVER counted as races
    assert pre["n_unmeasurable"] == 2
    assert pre["unmeasurable"] == {"book_invalid": 1,   # trade before state
                                   "side_empty": 1,     # taker=yes, no ask
                                   "bad_taker_side": 0}
    assert pre["race_rate"] is None                 # no measurable => no rate


def test_breakdowns_category_subcategory_class_tier(tmp_path):
    root = str(tmp_path)
    two_population_day(root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    cov, l1 = _pop(rep, "covered_book"), _pop(rep, "l1_asof")
    m = _market(cov, COV)
    assert m["market_id"] == GoldDayReader(root, DATE).market_id(COV)
    assert m["category"] == "Sports" and m["subcategory"] == "Baseball"
    assert m["market_class"] == "A" and m["liquidity_tier"] == "High"
    assert cov["by_category"]["Sports"]["n_races"] == 1
    assert cov["by_class"]["A"]["n_measurable"] == 2
    assert cov["by_liquidity_tier"]["High"]["race_rate"] == 0.5
    assert l1["by_category"]["Entertainment"]["n_races"] == 1
    assert l1["by_subcategory"]["Sitcoms"]["n_measurable"] == 3
    assert l1["by_class"]["B"]["n_trades"] == 3
    assert l1["by_class"]["A"]["n_trades"] == 2     # PRE is Sports, class A
    assert l1["by_liquidity_tier"]["Low"]["race_rate"] == round(1 / 3, 6)


# --------------------------------------------------------- proposed thresholds

def test_market_class_matches_sanitized_category_names(tmp_path):
    """The sidecar dim carries PATH-SANITIZED category names
    ("Climate_and_Weather") while config/market_classes.yaml carries the
    live Kalshi names ("Climate and Weather") — the class mapping must
    normalize, or a class-A category silently reports as B (D2)."""
    root = str(tmp_path)
    book = [_snap([(4500, 100000)], [(5000, 50000)], COV, T0)]
    trades = [_trade(COV, T0 + 1 * S, "W1", 5000, "yes")]
    records, _ = merge([book, trades])
    dim = {COV: {"category": "Climate_and_Weather", "close_time": "",
                 "liquidity_tier": "High"}}
    write_day(records, dim, DATE, root)
    rep = measure_day(root, DATE, {}, class_a={"Climate and Weather"})
    m = _market(_pop(rep, "covered_book"), COV)
    assert m["market_class"] == "A"
    assert "A" in _pop(rep, "covered_book")["by_class"]


def test_propose_thresholds_nearest_rank_p95_hand_computed():
    """Pure function, hand-computed: category X has 20 qualifying markets
    with rates 0.00..0.19 => p95 = sorted[ceil(.95*20)-1] = sorted[18] =
    0.18; category Y has 5 markets at 0.50 => 0.50; _global pool of 25 =>
    sorted[ceil(.95*25)-1] = sorted[23] = 0.50. A market below
    MIN_MEASURABLE stays OUT of the pool."""
    assert PROPOSAL_PCT == 0.95 and MIN_MEASURABLE == 20
    entries = [{"category": "X", "n_measurable": 50, "race_rate": i / 100}
               for i in range(20)]
    entries += [{"category": "Y", "n_measurable": 50, "race_rate": 0.5}
                for _ in range(5)]
    entries += [{"category": "X", "n_measurable": 5, "race_rate": 1.0}]
    props = propose_thresholds(entries)
    assert props["_global"] == {"threshold": 0.5, "n_markets_in_pool": 25}
    assert props["X"] == {"threshold": 0.18, "n_markets_in_pool": 20}
    assert props["Y"] == {"threshold": 0.5, "n_markets_in_pool": 5}
    assert set(props) == {"_global", "X", "Y"}
    assert propose_thresholds([]) == {}             # no pool, no proposals


def test_unsafe_marking_in_report_and_manifest(tmp_path):
    root = str(tmp_path)
    twenty_one_market_day(root)
    rep = measure_day(root, DATE, {}, class_a=CLASS_A)
    cov = _pop(rep, "covered_book")
    props = rep["proposed_thresholds"]["covered_book"]
    assert props["_global"]["threshold"] == 0.0
    assert props["Sports"]["threshold"] == 0.0
    bad = _market(cov, "KXV7-M20")
    assert bad["race_rate"] == 1.0
    assert bad["unsafe_for_microstructure"] is True
    assert _market(cov, "KXV7-M00")["unsafe_for_microstructure"] is False
    marked = [u for u in rep["unsafe_for_microstructure"]
              if u["slice"] == "market"]
    assert [u["key"] for u in marked] == ["KXV7-M20"]
    assert marked[0]["population"] == "covered_book"
    assert marked[0]["proposed_threshold"] == 0.0
    # the marking flows into the manifest verdict field:
    update_manifest_v7(root, DATE, rep)
    with open(day_paths(root, DATE)["manifest"]) as f:
        man = json.load(f)
    unsafe = man["safety_verdicts"]["unsafe_for_microstructure"]
    assert {"population": "covered_book", "slice": "market",
            "key": "KXV7-M20"}.items() <= unsafe[
                [u["slice"] for u in unsafe].index("market")].items()
    v7 = man["safety_verdicts"]["V7"]
    assert v7["status"] == "measured" and v7["report_only"] is True
    assert v7["n_unsafe_slices"] == len(unsafe) >= 1


def test_min_support_rule_no_marking_no_proposals(tmp_path):
    """3 measurable trades < MIN_MEASURABLE: the market can neither seed a
    proposal nor be marked — its rate is still fully reported (D2)."""
    root = str(tmp_path)
    covered_three_trade_day(root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    assert _market(_pop(rep, "covered_book"), COV)["race_rate"] == round(2 / 3, 6)
    assert rep["proposed_thresholds"]["covered_book"] == {}
    assert rep["unsafe_for_microstructure"] == []


# ----------------------------------------- REPORT-ONLY: no enforcement, ever

def test_report_only_no_threshold_enforcement_code_paths(tmp_path, capsys):
    """Grep-provable (§2.3 V7 + W3.2): no exit-nonzero tied to race rates.
    The module may only `return 0`, and the only sys.exit is
    sys.exit(main()). A day WITH marked-unsafe slices still exits 0."""
    src = open(os.path.join(ROOT, "tools", "gold_v7_race.py")).read()
    assert re.search(r"\breturn\s+[1-9]", src) is None
    assert re.search(r"sys\.exit\((?!main\(\))", src) is None
    root = str(tmp_path)
    twenty_one_market_day(root)
    rc = main(["--date", DATE, "--root", root], fetch=_fake_fetch({}))
    out = capsys.readouterr().out
    assert rc == 0                                   # marked, NEVER failed
    assert "KXV7-M20" in out
    assert HEADING_FOR_APPROVAL == "FOR OPERATOR APPROVAL — not enforced"
    assert HEADING_FOR_APPROVAL in out
    assert "REPORT-ONLY" in out


# ------------------------------------------------------ manifest verdict update

def test_manifest_update_touches_v7_only_never_md5s_or_v5(tmp_path):
    root = str(tmp_path)
    two_population_day(root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    p = day_paths(root, DATE)
    with open(p["manifest"]) as f:
        before = json.load(f)
    import hashlib
    md5s_before = {}
    for k in ("bin", "markets", "trade_ids"):
        with open(p[k], "rb") as f:
            md5s_before[k] = hashlib.md5(f.read()).hexdigest()
    update_manifest_v7(root, DATE, rep)
    with open(p["manifest"]) as f:
        after = json.load(f)
    assert after["files"] == before["files"]            # certified md5s frozen
    for k in ("bin", "markets", "trade_ids"):           # certified FILES frozen
        with open(p[k], "rb") as f:
            assert hashlib.md5(f.read()).hexdigest() == md5s_before[k], k
    assert after["safety_verdicts"]["V5"] == before["safety_verdicts"]["V5"]
    v7 = after["safety_verdicts"]["V7"]
    assert v7["status"] == "measured" and v7["report_only"] is True
    assert v7["populations"]["covered_book"]["race_rate"] == 0.5
    assert v7["proposed_thresholds"]["heading"] == HEADING_FOR_APPROVAL
    assert after["record_count"] == before["record_count"]
    GoldDayReader(root, DATE)                           # certification intact


# --------------------------------------------------- committed defect fixture

def test_defect_fixture_exists_and_is_git_tracked():
    """Fresh-clone safety (G2 precedent)."""
    tracked = set(subprocess.run(
        ["git", "ls-files", "tests/fixtures/gold_defects/v7_inverted_taker"],
        cwd=ROOT, capture_output=True, text=True).stdout.splitlines())
    p = day_paths(DEFECT, DATE)
    for path in (p["bin"], p["markets"], p["trade_ids"], p["manifest"]):
        assert os.path.isfile(path), path
        rel = os.path.relpath(path, ROOT)
        assert rel in tracked, "fixture NOT git-tracked: %s" % rel


def test_inverted_taker_fixture_flips_race_rate_red_proof(tmp_path):
    """The seeded defect: identical prints, taker sides FLIPPED. Correct
    sides measure 0.0; the inversion flips the measured race rate to 1.0 —
    the measurement itself catches the inversion. NOT a threshold failure:
    the CLI on the defect day still exits 0."""
    twin = os.path.join(str(tmp_path), "twin")
    correct_taker_twin_day(twin)
    good = _market(_pop(measure_day(twin, DATE, SUBCAT, class_a=CLASS_A),
                        "covered_book"), INV)
    assert good["n_measurable"] == 25 and good["n_races"] == 0
    assert good["race_rate"] == 0.0
    bad = _market(_pop(measure_day(DEFECT, DATE, SUBCAT, class_a=CLASS_A),
                       "covered_book"), INV)
    assert bad["n_measurable"] == 25 and bad["n_races"] == 25
    assert bad["race_rate"] == 1.0                      # inversion caught
    run_root = os.path.join(str(tmp_path), "run")
    shutil.copytree(os.path.join(DEFECT, "date=%s" % DATE),
                    day_paths(run_root, DATE)["dir"])
    assert main(["--date", DATE, "--root", run_root],
                fetch=_fake_fetch(SUBCAT)) == 0          # report-only, always


# ------------------------------------------------------------------------- CLI

def _fake_fetch(subcat):
    def fetch(date, tickers):
        return ({mt: sc for mt, sc in subcat.items() if mt in tickers},
                {"source": "test-injected", "markets_wanted": len(tickers)})
    return fetch


def test_cli_writes_report_updates_manifest_exit_zero(tmp_path, capsys):
    root = str(tmp_path)
    two_population_day(root)
    rc = main(["--date", DATE, "--root", root], fetch=_fake_fetch(SUBCAT))
    out = capsys.readouterr().out
    assert rc == 0
    assert "V7 MEASURED (REPORT-ONLY)" in out
    assert "covered_book" in out and "l1_asof" in out
    rpath = os.path.join(day_paths(root, DATE)["dir"],
                         "v7_race_report_%s.json" % DATE)
    assert os.path.isfile(rpath)
    with open(rpath) as f:
        rep = json.load(f)
    assert rep["populations"]["covered_book"]["totals"]["race_rate"] == 0.5
    assert rep["populations"]["l1_asof"]["per_market"]  # separately reported
    with open(day_paths(root, DATE)["manifest"]) as f:
        assert json.load(f)["safety_verdicts"]["V7"]["status"] == "measured"


def test_write_report_lands_inside_day_partition(tmp_path):
    root = str(tmp_path)
    covered_three_trade_day(root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    path = write_report(root, DATE, rep)
    assert path == os.path.join(day_paths(root, DATE)["dir"],
                                "v7_race_report_%s.json" % DATE)
    with open(path) as f:
        assert json.load(f)["label"] == rep["label"]


def test_render_required_headings(tmp_path):
    root = str(tmp_path)
    two_population_day(root)
    rep = measure_day(root, DATE, SUBCAT, class_a=CLASS_A)
    text = render(rep)
    assert "REPORT-ONLY" in text
    assert HEADING_FOR_APPROVAL in text
    assert "Unsafe for Microstructure Backtest" in text  # §3 vocabulary
    assert "covered_book" in text and "l1_asof" in text
    assert COV in text and L1M in text


if __name__ == "__main__":
    print("regenerated defect fixture: %s" % build_defect_fixture())
