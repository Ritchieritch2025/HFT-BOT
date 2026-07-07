"""W3.1: δ distribution (V5) — tools/gold_v5_delta.py (PLAN_GOLD_DATA_CONTRACT
§2.3 V5 row + W3.1). Contract encoded here, written BEFORE the module exists
(TDD):

  - For COVERED markets only (F_BOOK_COVERED, i.e. markets with a full-depth
    BOOK_SNAPSHOT this day): each L1 change row is matched to the NEAREST
    book record (BOOK_SNAPSHOT/BOOK_DELTA, F_BOOK_VALID) whose reconstructed
    top-of-book (bid_px_e4[0]/ask_px_e4[0], empty-side sentinels normalized
    to L1's 0/10000 encoding) equals the L1 view, scanning within a FIXED
    bound SCAN_BOUND_US. δ = |ts_book − ts_l1| of that nearest agreement.
  - δ is a DISTRIBUTION: p50/p90/p99/max delta_ms (nearest-rank on µs ints)
    + mismatch counts, broken down by market_id, market_ticker, category,
    subcategory, liquidity tier.
  - BINDING (§2.3 V5): widening δ to absorb mismatches is FORBIDDEN. The
    scan bound is a code constant (no CLI knob); a market whose mismatch
    rate stays high at the global p99 δ goes on a surfaced bad-markets
    list, never into a looser window. Red-proven here by the committed
    v5_shifted_book fixture: its book tops are price-shifted +100 E4, so
    book and L1 PERMANENTLY disagree — mismatches must be counted and the
    market listed even at a 100x scan bound.
  - Honesty split (D2): an L1 row with NO book record inside the scan
    window is "uncheckable" (no evidence either way) — reported separately,
    never counted as a mismatch; a row with book records in-window but none
    agreeing is a real mismatch ("never_agree").
  - Support size MUST be printed (audit G4: day one is ~4 markets):
    n_markets, capture_hours, n_l1_rows, n_full_depth_rows, n_matched_pairs,
    with the label "BASELINE SAMPLE (support: N markets)".
  - Manifest verdict update (BACKLOG W2.4 note): safety_verdicts.V5 is
    filled IN PLACE; the certified md5s (manifest["files"]) and every
    certified file stay byte-identical; V7 placeholder untouched;
    GoldDayReader must still open (md5 certification intact).

Fixture regeneration (deterministic bytes):
    python3 tests/test_gold_v5_delta.py
"""
import csv
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:                      # __main__ fixture regeneration
    sys.path.insert(0, ROOT)

from tools.gold_dtype import EVENT_TYPE
from tools.gold_io import GoldDayReader, day_paths, write_day
from tools.gold_load import BookMsg, Event, L1Top
from tools.gold_merge import merge
from tools.gold_v5_delta import (BAD_MARKET_RATE, MIN_CHECKABLE, SCAN_BOUND_US,
                                 main, match_views, measure_day, render,
                                 update_manifest_v5, write_report)

DEFECT = os.path.join(ROOT, "tests", "fixtures", "gold_defects", "v5_shifted_book")
DATE = "2026-07-06"
T0 = 1783300000000000
S = 1_000_000                                  # 1 second in µs
MS = 1_000                                     # 1 millisecond in µs

SNAP, DELTA = EVENT_TYPE["BOOK_SNAPSHOT"], EVENT_TYPE["BOOK_DELTA"]
L1 = EVENT_TYPE["L1_TICKER"]
A, B, C, BAD = "KXV5-A", "KXV5-B", "KXV5-C", "KXV5-BAD"
SUBCAT = {A: "Baseball", B: "Baseball", C: "Soccer", BAD: "Baseball"}


def _snap(yes, no, mt, ts):
    return Event(SNAP, ts, mt, BookMsg(None, None, None, tuple(yes), tuple(no)))


def _delta(side, px, d, mt, ts):
    return Event(DELTA, ts, mt, BookMsg(side, px, d, None, None))


def _l1(mt, ts, bid, bq, ask, aq):
    return Event(L1, ts, mt, L1Top(bid, bq, ask, aq, False))


def _dim(*mts):
    return {mt: {"category": "Sports", "close_time": "2026-07-06T23:00:00Z",
                 "liquidity_tier": "High" if mt != A else "Mid"} for mt in mts}


# ------------------------------------------------------------ day construction

def market_a_events():
    """Covered market with hand-computed offsets. Book: snapshot at T0
    (bid 4500 / ask 5000) then 9 deltas at T0+i*10s each moving the top bid
    to 4500+10i (10 distinct book states). L1: one view per state at
    t_state + (i+1)*10ms => matched deltas exactly 10,20,...,100 ms.
    Nearest-rank percentiles: p50=50, p90=90, p99=100, max=100 ms.
    Plus ONE view 3h later (no book record within the scan bound):
    uncheckable, NOT a mismatch."""
    book = [_snap([(4500, 100000)], [(5000, 50000)], A, T0)]
    for i in range(1, 10):
        book.append(_delta("yes", 4500 + 10 * i, 20000, A, T0 + i * 10 * S))
    views = [(T0 + i * 10 * S + (i + 1) * 10 * MS, 4500 + 10 * i, 5000)
             for i in range(10)]
    views.append((T0 + 3 * 3600 * S, 4590, 5000))          # out of coverage
    return book, views


def market_b_events():
    """Covered market whose L1 PERMANENTLY disagrees: book tops walk
    3000..3024 (snapshot + 24 deltas, 1s apart), the 25 L1 views claim
    3100..3124 — no book state this day ever agrees, at ANY bound.
    All 25 rows are checkable (records inside the scan window) => 25
    never_agree mismatches; 25 >= MIN_CHECKABLE => bad-markets list."""
    book = [_snap([(3000, 10000)], [(6000, 10000)], B, T0)]
    for i in range(1, 25):
        book.append(_delta("yes", 3000 + i, 10000, B, T0 + i * S))
    views = [(T0 + i * S + 5 * MS, 3100 + i, 4000) for i in range(25)]
    return book, views


def good_bad_day(root, mts=(A, B, C)):
    """A (agreeing) + B (disagreeing) + C (L1-only, must be excluded)."""
    sources, views = [], {}
    if A in mts:
        book, v = market_a_events()
        sources.append(book)
        views[A] = v
    if B in mts:
        book, v = market_b_events()
        sources.append(book)
        views[B] = v
    if C in mts:
        sources.append([_l1(C, T0 + i * S, 4000, 5000, 4200, 7000)
                        for i in range(3)])
        views[C] = [(T0 + i * S, 4000, 4200) for i in range(3)]
    records, _ = merge(sources)
    write_day(records, _dim(*mts), DATE, root)
    return views


# --------------------------------------------------- committed defect fixture

def build_defect_fixture(fixture_root=DEFECT):
    """v5_shifted_book: book tops price-shifted +100 E4 vs the recorded L1
    views — agreement is broken PERMANENTLY (no δ, however wide, absorbs
    it). Deterministic bytes; l1 views committed as a CSV sidecar."""
    shutil.rmtree(os.path.join(fixture_root, "date=%s" % DATE),
                  ignore_errors=True)
    book = [_snap([(4600, 10000)], [(5200, 10000)], BAD, T0)]      # +100 shift
    for i in range(1, 25):
        book.append(_delta("yes", 4600 + i, 10000, BAD, T0 + i * S))
    records, _ = merge([book])
    write_day(records, _dim(BAD), DATE, fixture_root)
    views = [(T0 + i * S + 5 * MS, 4500 + i, 4700) for i in range(25)]
    with open(os.path.join(fixture_root, "l1_views_%s.csv" % DATE), "w",
              newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_us", "market_ticker", "yes_bid_e4", "yes_ask_e4"])
        for ts, bid, ask in views:
            w.writerow([ts, BAD, bid, ask])
    return fixture_root


def load_fixture_views(fixture_root=DEFECT):
    views = {}
    with open(os.path.join(fixture_root, "l1_views_%s.csv" % DATE),
              newline="") as f:
        for r in csv.DictReader(f):
            views.setdefault(r["market_ticker"], []).append(
                (int(r["ts_us"]), int(r["yes_bid_e4"]), int(r["yes_ask_e4"])))
    return views


def _market(report, mt):
    return next(m for m in report["per_market"] if m["market_ticker"] == mt)


# ----------------------------------------------------------- pure match_views

def test_match_views_bound_and_agreement():
    ts = np.array([T0], dtype=np.int64)
    bid = np.array([4500], dtype=np.int64)
    ask = np.array([5000], dtype=np.int64)
    # same top 4s away: matched, δ = 4000 ms
    m, never, unc = match_views(ts, bid, ask, [(T0 + 4 * S, 4500, 5000)],
                                SCAN_BOUND_US)
    assert (m, never, unc) == ([4 * S], 0, 0)
    # same top 6s away: OUTSIDE the fixed 5s scan bound -> uncheckable
    m, never, unc = match_views(ts, bid, ask, [(T0 + 6 * S, 4500, 5000)],
                                SCAN_BOUND_US)
    assert (m, never, unc) == ([], 0, 1)
    # book record in-window but different top -> never_agree mismatch
    m, never, unc = match_views(ts, bid, ask, [(T0 + 4 * S, 4510, 5000)],
                                SCAN_BOUND_US)
    assert (m, never, unc) == ([], 1, 0)


def test_scan_bound_is_fixed_not_a_cli_knob():
    """Widening δ is FORBIDDEN (§2.3 V5): the bound is a code constant and
    the CLI must expose no flag to widen it."""
    assert SCAN_BOUND_US == 5_000_000
    with pytest.raises(SystemExit):
        main(["--help-probe-no-such-flag"])
    rc = subprocess.run(
        [sys.executable, "-m", "tools.gold_v5_delta", "--help"],
        cwd=ROOT, capture_output=True, text=True)
    assert "bound" not in rc.stdout.lower()
    assert "delta-window" not in rc.stdout.lower()


# ------------------------------------------------------- distribution + report

def test_known_offsets_hand_computed_percentiles(tmp_path):
    root = str(tmp_path)
    views = good_bad_day(root, mts=(A,))
    rep = measure_day(root, DATE, views, SUBCAT)
    a = _market(rep, A)
    assert a["n_l1_rows"] == 11
    assert a["n_checkable"] == 10 and a["n_matched"] == 10
    assert a["n_never_agree"] == 0 and a["n_uncheckable"] == 1
    assert a["delta_ms"] == {"p50": 50.0, "p90": 90.0, "p99": 100.0,
                             "max": 100.0}
    assert rep["delta_ms"] == a["delta_ms"]
    assert rep["bad_markets"] == []


def test_disagreeing_market_counted_listed_never_absorbed(tmp_path):
    root = str(tmp_path)
    views = good_bad_day(root)
    rep = measure_day(root, DATE, views, SUBCAT)
    b = _market(rep, B)
    assert b["n_checkable"] == 25 and b["n_matched"] == 0
    assert b["n_never_agree"] == 25                     # mismatches COUNTED
    assert b["mismatch_rate_at_global_p99"] == 1.0
    assert [m["market_ticker"] for m in rep["bad_markets"]] == [B]
    # NOT absorbed: global δ distribution comes from matched pairs only —
    # identical to the A-only day (B adds nothing to the percentile pool).
    assert rep["delta_ms"] == {"p50": 50.0, "p90": 90.0, "p99": 100.0,
                               "max": 100.0}
    assert rep["mismatches"]["never_agree"] == 25
    # and a 100x bound STILL cannot absorb a price-level disagreement:
    book_ts = np.array([T0 + i * S for i in range(25)], dtype=np.int64)
    book_bid = np.array([3000 + i for i in range(25)], dtype=np.int64)
    book_ask = np.full(25, 4000, dtype=np.int64)
    m, never, unc = match_views(book_ts, book_bid, book_ask, views[B],
                                100 * SCAN_BOUND_US)
    assert m == [] and never == 25 and unc == 0


def test_uncovered_market_excluded_from_measurement(tmp_path):
    root = str(tmp_path)
    views = good_bad_day(root)
    rep = measure_day(root, DATE, views, SUBCAT)
    assert C not in [m["market_ticker"] for m in rep["per_market"]]
    assert rep["support"]["n_markets"] == 2            # A and B only


def test_support_size_and_baseline_label(tmp_path):
    root = str(tmp_path)
    views = good_bad_day(root)
    rep = measure_day(root, DATE, views, SUBCAT)
    sup = rep["support"]
    assert sup == {"n_markets": 2, "capture_hours": 0.03, "n_l1_rows": 36,
                   "n_full_depth_rows": 35, "n_matched_pairs": 10}
    assert rep["label"] == "BASELINE SAMPLE (support: 2 markets)"
    text = render(rep)
    assert "BASELINE SAMPLE (support: 2 markets)" in text
    assert "n_l1_rows=36" in text and "n_full_depth_rows=35" in text
    assert "n_matched_pairs=10" in text and "capture_hours=0.03" in text
    assert B in text                                   # bad market surfaced


def test_breakdowns_by_category_subcategory_tier(tmp_path):
    root = str(tmp_path)
    views = good_bad_day(root)
    rep = measure_day(root, DATE, views, SUBCAT)
    a = _market(rep, A)
    assert a["market_id"] == GoldDayReader(root, DATE).market_id(A)
    assert a["category"] == "Sports" and a["subcategory"] == "Baseball"
    assert a["liquidity_tier"] == "Mid"
    assert rep["by_category"]["Sports"]["n_matched"] == 10
    assert rep["by_category"]["Sports"]["n_never_agree"] == 25
    assert rep["by_subcategory"]["Baseball"]["n_matched"] == 10
    assert rep["by_liquidity_tier"]["Mid"]["delta_ms"]["p50"] == 50.0
    assert rep["by_liquidity_tier"]["High"]["n_never_agree"] == 25


# ------------------------------------------------------ manifest verdict update

def test_manifest_update_touches_verdict_only_never_certified_md5s(tmp_path):
    root = str(tmp_path)
    views = good_bad_day(root)
    rep = measure_day(root, DATE, views, SUBCAT)
    p = day_paths(root, DATE)
    with open(p["manifest"]) as f:
        before = json.load(f)
    import hashlib
    md5s_before = {}
    for k in ("bin", "markets", "trade_ids"):
        with open(p[k], "rb") as f:
            md5s_before[k] = hashlib.md5(f.read()).hexdigest()
    update_manifest_v5(root, DATE, rep)
    with open(p["manifest"]) as f:
        after = json.load(f)
    assert after["files"] == before["files"]            # certified md5s frozen
    for k in ("bin", "markets", "trade_ids"):           # certified FILES frozen
        with open(p[k], "rb") as f:
            assert hashlib.md5(f.read()).hexdigest() == md5s_before[k], k
    v5 = after["safety_verdicts"]["V5"]
    assert v5["status"] == "measured" and v5["baseline_sample"] is True
    assert v5["label"] == "BASELINE SAMPLE (support: 2 markets)"
    assert [m["market_ticker"] for m in v5["bad_markets"]] == [B]
    assert after["safety_verdicts"]["V7"] == "pending"  # W3.2's field untouched
    assert after["record_count"] == before["record_count"]
    GoldDayReader(root, DATE)                           # certification intact


# --------------------------------------------------- committed defect fixture

def test_defect_fixture_exists_and_is_git_tracked():
    """Fresh-clone safety (G2 precedent)."""
    tracked = set(subprocess.run(
        ["git", "ls-files", "tests/fixtures/gold_defects/v5_shifted_book"],
        cwd=ROOT, capture_output=True, text=True).stdout.splitlines())
    p = day_paths(DEFECT, DATE)
    paths = [p["bin"], p["markets"], p["trade_ids"], p["manifest"],
             os.path.join(DEFECT, "l1_views_%s.csv" % DATE)]
    for path in paths:
        assert os.path.isfile(path), path
        rel = os.path.relpath(path, ROOT)
        assert rel in tracked, "fixture NOT git-tracked: %s" % rel


def test_shifted_book_fixture_breaks_agreement_red_proof():
    """The seeded defect: book and L1 permanently disagree => every row a
    counted mismatch, market on the bad list, δ pool EMPTY (nothing was
    absorbed into a wider window)."""
    views = load_fixture_views()
    rep = measure_day(DEFECT, DATE, views, SUBCAT)
    bad = _market(rep, BAD)
    assert bad["n_checkable"] == 25 and bad["n_matched"] == 0
    assert bad["n_never_agree"] == 25
    assert [m["market_ticker"] for m in rep["bad_markets"]] == [BAD]
    assert rep["support"]["n_matched_pairs"] == 0
    assert rep["delta_ms"] is None                      # no pairs, no fake δ


def test_bad_market_needs_min_support_but_mismatches_still_counted(tmp_path):
    """A market with fewer than MIN_CHECKABLE rows is not FLAGGED (no
    statistical support) — but its mismatches are still counted (D2)."""
    assert MIN_CHECKABLE == 20 and BAD_MARKET_RATE == 0.05
    root = str(tmp_path)
    book = [_snap([(4600, 10000)], [(5200, 10000)], BAD, T0)]
    records, _ = merge([book])
    write_day(records, _dim(BAD), DATE, root)
    views = {BAD: [(T0 + i * MS, 4500, 4700) for i in range(5)]}
    rep = measure_day(root, DATE, views, SUBCAT)
    assert _market(rep, BAD)["n_never_agree"] == 5
    assert rep["mismatches"]["never_agree"] == 5
    assert rep["bad_markets"] == []                     # unflagged, low support


# ------------------------------------------------------------------------- CLI

def _fake_fetch(views):
    def fetch(date, tickers):
        got = {mt: v for mt, v in views.items() if mt in tickers}
        return got, SUBCAT, {"source": "test-injected", "rows_in":
                             sum(len(v) for v in got.values())}
    return fetch


def test_cli_good_day_exit_zero_report_written_manifest_updated(tmp_path, capsys):
    root = str(tmp_path)
    views = good_bad_day(root, mts=(A, C))
    rc = main(["--date", DATE, "--root", root], fetch=_fake_fetch(views))
    out = capsys.readouterr().out
    assert rc == 0
    assert "BASELINE SAMPLE (support: 1 markets)" in out
    rpath = os.path.join(day_paths(root, DATE)["dir"],
                         "v5_delta_report_%s.json" % DATE)
    assert os.path.isfile(rpath)
    with open(rpath) as f:
        assert json.load(f)["delta_ms"]["p50"] == 50.0
    with open(day_paths(root, DATE)["manifest"]) as f:
        assert json.load(f)["safety_verdicts"]["V5"]["status"] == "measured"


def test_cli_bad_market_surfaced_nonzero_exit(tmp_path, capsys):
    root = str(tmp_path)
    shutil.copytree(os.path.join(DEFECT, "date=%s" % DATE),
                    day_paths(root, DATE)["dir"])
    rc = main(["--date", DATE, "--root", root],
              fetch=_fake_fetch(load_fixture_views()))
    out = capsys.readouterr().out
    assert rc == 1
    assert "BAD MARKETS SURFACED" in out and BAD in out
    with open(day_paths(root, DATE)["manifest"]) as f:
        v5 = json.load(f)["safety_verdicts"]["V5"]
    assert [m["market_ticker"] for m in v5["bad_markets"]] == [BAD]


def test_write_report_lands_inside_day_partition(tmp_path):
    root = str(tmp_path)
    views = good_bad_day(root, mts=(A,))
    rep = measure_day(root, DATE, views, SUBCAT)
    path = write_report(root, DATE, rep)
    assert path == os.path.join(day_paths(root, DATE)["dir"],
                                "v5_delta_report_%s.json" % DATE)
    with open(path) as f:
        assert json.load(f)["label"] == rep["label"]


if __name__ == "__main__":
    print("regenerated defect fixture: %s" % build_defect_fixture())
