"""W4: coverage auditor — tools/coverage_audit.py (PLAN_GOLD_DATA_CONTRACT
§2.3 rows V14/V15/V16 + W4). Contract encoded here, written BEFORE the module
exists (TDD):

  - S1 universe reconciliation: EVERY traded market is classified — a market
    whose category is missing from config/market_classes.yaml (or is
    null/empty) defaults to Class B WITH a surfaced warning (V16, matching
    tools/build_classification.py behavior). Traded series absent from the
    live catalog are surfaced, never silently dropped.
  - S2/V14: liquidity tiers reuse the gold sidecar when present, else are
    computed from trades with EXACTLY gold_build.liquidity_tiers semantics
    (top ceil(10%) High, next to ceil(20%) Mid, rest Low; rank by -volume
    then ticker). 100% of High+Mid markets must have L1 coverage; violations
    are LISTED with category + record_class. Day one is REPORT-context: the
    check reports loudly, it does not gate the exit code.
  - S3: every Sports-category market that traded has L1 rows (Sports is
    Class A); missed markets listed.
  - S4: depth-target ranking = traded_volume_e4 x avg_spread_e4 over markets
    whose spread is measurable (has L1 rows with both sides); descending,
    tie-broken by ticker; feeds W6 via work/mm/depth_target_<date>.csv.
  - V15: full-depth observed set vs the DECLARED subscription list.
    Shrinkage (declared but not observed) is an ERROR (exit nonzero), not a
    warning. Extras (observed but never declared — legacy watchlist
    leftovers) are a warning. A missing declared-list file is documented
    ("declared_list_missing"), never invented.
  - Promotions: Class B markets in High/Mid tier are REPORT-ONLY candidates
    (work/mm/promotion_candidates_<date>.csv); Exotics/MVE stay Class B
    regardless (Q7) and are excluded from candidacy (counted, surfaced).
    config/market_classes.yaml is NEVER edited by the tool.
  - lifecycle_check gains ONE appended NON-BLOCKING research stage that only
    ever returns "pass" or "skipped" — it can never turn the lifecycle red.

Anti-fake-green: each check is proven red on its own seeded fixture below
(fake new category / High-tier-without-L1 / sports-missing / depth-set
shrinkage); this whole file was run RED before tools/coverage_audit.py
existed.
"""
import csv
import os
import sys
from collections import namedtuple

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.coverage_audit import (audit, classify_category, compute_tiers,
                                  exit_code, load_class_policy,
                                  read_declared_list, render,
                                  write_depth_targets, write_promotions)

DATE = "2026-07-06"


# --- fixture universe (16 traded markets => hi=ceil(1.6)=2, mid=ceil(3.2)=4:
#     ranks 0-1 High, 2-3 Mid, 4+ Low) ------------------------------------------
def mk(cat, sub, series, vol, n=3):
    return {"category": cat, "subcategory": sub, "series_ticker": series,
            "volume_e4": vol, "n_trades": n}


def traded_universe():
    # n=17 => hi=ceil(1.7)=2 High, mid=ceil(3.4)=4 => ranks 0-1 High, 2-3 Mid.
    t = {
        "KXNBA-COVERED-A":  mk("Sports", "NBA", "KXNBA", 1000_0000),   # High, L1
        "KXNBA-MISSED-A":   mk("Sports", "NBA", "KXNBA", 900_0000),    # High, NO L1
        "KXMVE-COMBO-B":    mk("Exotics", "_none", "KXMVE", 800_0000), # Mid, NO L1, Q7
        "KXENT-HOT-B":      mk("Entertainment", "Awards", "KXENT", 500_0000),  # Mid, NO L1
        "KXQZZ-NEWCAT":     mk("Quantum Fizzbin", "_none", "KXQZZ", 50_0000),  # V16
        "KXGHOST-1":        mk("Entertainment", "_none", "KXGHOST", 40_0000),  # not in catalog
        # warehouse rows carry PATH-SANITIZED categories (verified live
        # 2026-07-06: 'Climate_and_Weather', 'Science_and_Technology'); the
        # classifier must match them to the policy's human names, no warning
        "KXWX-SAN-A":       mk("Climate_and_Weather", "MIA", "KXWX", 35_0000),
        "KXNULLCAT-1":      mk(None, None, "KXNULLCAT", 30_0000),      # null category
    }
    for i in range(9):  # low-volume fillers, all with L1, category Crypto
        t["KXBTC-FILL-%d" % i] = mk("Crypto", "BTC", "KXBTC", 20_0000 - i * 10000)
    return t


def l1_spreads():
    s = {"KXNBA-COVERED-A": 200.0, "KXWX-SAN-A": 50.0}
    for i in range(9):
        s["KXBTC-FILL-%d" % i] = 100.0 + i
    return s


def catalog_cats():
    # live catalog: series -> category. KXGHOST deliberately absent (S1).
    # KXWEIRD exists in the catalog with a category the policy doesn't know.
    return {"KXNBA": "Sports", "KXMVE": "Exotics", "KXENT": "Entertainment",
            "KXQZZ": "Quantum Fizzbin", "KXNULLCAT": "Entertainment",
            "KXWX": "Climate and Weather", "KXBTC": "Crypto",
            "KXWEIRD": "Hyperspace Sports"}


def policy():
    return ({"Sports", "Crypto", "Climate and Weather"},
            {"Exotics", "Entertainment"})


def run_fixture_audit(full_set=frozenset(), declared=None):
    t = traded_universe()
    tiers = compute_tiers({k: v["volume_e4"] for k, v in t.items()})
    return audit(DATE, t, l1_spreads(), set(full_set), catalog_cats(),
                 policy(), tiers, declared)


# --- class policy / V16 ---------------------------------------------------------
def test_load_class_policy_real_file():
    a, b = load_class_policy(os.path.join(ROOT, "config", "market_classes.yaml"))
    assert "Sports" in a and "Crypto" in a
    assert "Exotics" in b and "Entertainment" in b
    assert not (a & b)


def test_classify_category_known():
    a, b = policy()
    assert classify_category("Sports", a, b) == ("A", False)
    assert classify_category("Exotics", a, b) == ("B", False)


def test_classify_category_sanitized_storage_forms():
    # The warehouse stores wc.sanitize()d categories; the policy stores human
    # names. Both spellings of a LISTED category classify without a warning.
    a, b = policy()
    assert classify_category("Climate_and_Weather", a, b) == ("A", False)
    assert classify_category("Climate and Weather", a, b) == ("A", False)
    a2, b2 = {"Sports"}, {"Science and Technology"}
    assert classify_category("Science_and_Technology", a2, b2) == ("B", False)
    # the "_unclassified" null sentinel is unknown-category, NOT a new category
    assert classify_category("_unclassified", a, b) == ("B", True)


def test_unclassified_sentinel_counts_as_unknown_not_new_category():
    t = {"KXX-1": mk("_unclassified", None, "KXX", 100)}
    tiers = compute_tiers({"KXX-1": 100})
    rep = audit(DATE, t, {}, set(), {"KXX": "Sports"}, policy(), tiers, None)
    assert rep["s1"]["unknown_category_markets"] == ["KXX-1"]
    assert rep["s1"]["classes"]["KXX-1"] == "B"
    assert "_unclassified" not in rep["v16"]["unlisted_categories_traded"]
    assert rep["v16"]["status"] == "warn"      # leakage is still surfaced


def test_v16_fake_new_category_defaults_class_b_with_warning():
    a, b = policy()
    # the V16 safety net: new/unlisted category => Class B + surfaced warning
    assert classify_category("Quantum Fizzbin", a, b) == ("B", True)
    assert classify_category(None, a, b) == ("B", True)
    assert classify_category("", a, b) == ("B", True)
    rep = run_fixture_audit()
    assert "Quantum Fizzbin" in rep["v16"]["unlisted_categories_traded"]
    assert "Hyperspace Sports" in rep["v16"]["unlisted_categories_catalog"]
    # sanitized spelling of a LISTED category is NOT a new category
    assert "Climate_and_Weather" not in rep["v16"]["unlisted_categories_traded"]
    assert rep["v16"]["status"] == "warn"
    # no unknown-category leakage: every traded market got a class
    assert set(rep["s1"]["classes"]) == set(traded_universe())
    assert rep["s1"]["classes"]["KXQZZ-NEWCAT"] == "B"
    assert rep["s1"]["classes"]["KXNULLCAT-1"] == "B"
    assert "KXNULLCAT-1" in rep["s1"]["unknown_category_markets"]


def test_v16_all_listed_is_pass():
    t = {"KXNBA-1": mk("Sports", "NBA", "KXNBA", 100_0000)}
    tiers = compute_tiers({k: v["volume_e4"] for k, v in t.items()})
    rep = audit(DATE, t, {"KXNBA-1": 50.0}, set(), {"KXNBA": "Sports"},
                policy(), tiers, None)
    assert rep["v16"]["status"] == "pass"
    assert rep["v16"]["unlisted_categories_traded"] == []


# --- S1 -------------------------------------------------------------------------
def test_s1_reconciliation_counts_and_unlisted_series():
    rep = run_fixture_audit()
    assert rep["s1"]["n_traded"] == 17
    assert rep["s1"]["n_l1"] == 11
    assert rep["s1"]["unlisted_series"] == ["KXGHOST"]
    # classification is total: A for Sports/Crypto/Weather, B otherwise
    assert rep["s1"]["classes"]["KXNBA-COVERED-A"] == "A"
    assert rep["s1"]["classes"]["KXWX-SAN-A"] == "A"     # sanitized spelling
    assert rep["s1"]["classes"]["KXENT-HOT-B"] == "B"


# --- tiers (must match gold_build semantics exactly) -----------------------------
def test_compute_tiers_hand_case():
    vols = {"M%02d" % i: (10 - i) * 100 for i in range(10)}  # M00 biggest
    tiers = compute_tiers(vols)
    assert tiers["M00"] == "High"          # ceil(10*.1)=1 High
    assert tiers["M01"] == "Mid"           # ceil(10*.2)=2 => rank 1 Mid
    assert all(tiers["M%02d" % i] == "Low" for i in range(2, 10))


def test_compute_tiers_parity_with_gold_build():
    from tools.gold_build import liquidity_tiers
    Payload = namedtuple("Payload", "count_e4")
    Ev = namedtuple("Ev", "market_ticker payload")
    vols = {"A": 500, "B": 400, "C": 400, "D": 100, "E": 50,
            "F": 40, "G": 30, "H": 20, "I": 10, "J": 5, "K": 1}
    evs = [Ev(mt, Payload(v)) for mt, v in vols.items()]
    assert compute_tiers(vols) == liquidity_tiers(evs)


def test_tie_break_matches_gold_build():
    # equal volume => ticker order decides the tier boundary
    tiers = compute_tiers({"ZZZ": 100, "AAA": 100, "MMM": 100,
                           "BBB": 1, "CCC": 1, "DDD": 1,
                           "EEE": 1, "FFF": 1, "GGG": 1, "HHH": 1})
    assert tiers["AAA"] == "High" and tiers["MMM"] == "Mid"
    assert tiers["ZZZ"] == "Low"


# --- V14 ------------------------------------------------------------------------
def test_v14_high_tier_without_l1_listed_with_category_and_class():
    rep = run_fixture_audit()
    v = {r["market_ticker"]: r for r in rep["v14"]["violations"]}
    assert "KXNBA-MISSED-A" in v                     # High tier, no L1
    assert v["KXNBA-MISSED-A"]["category"] == "Sports"
    assert v["KXNBA-MISSED-A"]["record_class"] == "A"
    assert v["KXNBA-MISSED-A"]["tier"] == "High"
    assert "KXENT-HOT-B" in v and v["KXENT-HOT-B"]["record_class"] == "B"
    assert "KXMVE-COMBO-B" in v                      # Q7 exclusion is for
    assert rep["v14"]["status"] == "violations_reported"   # promotions, NOT V14
    assert "KXNBA-COVERED-A" not in v                # covered High is clean


def test_v14_clean_universe_passes():
    t = {"KXNBA-1": mk("Sports", "NBA", "KXNBA", 100), "KXNBA-2": mk("Sports", "NBA", "KXNBA", 50)}
    tiers = compute_tiers({k: v["volume_e4"] for k, v in t.items()})
    rep = audit(DATE, t, {"KXNBA-1": 100.0, "KXNBA-2": 100.0}, set(),
                {"KXNBA": "Sports"}, policy(), tiers, None)
    assert rep["v14"]["violations"] == []
    assert rep["v14"]["status"] == "pass"


# --- S3 -------------------------------------------------------------------------
def test_s3_sports_missing_l1_listed():
    rep = run_fixture_audit()
    missed = [r["market_ticker"] for r in rep["s3"]["missed"]]
    assert missed == ["KXNBA-MISSED-A"]
    assert rep["s3"]["n_sports_traded"] == 2
    assert rep["s3"]["status"] == "violations_reported"


def test_s3_all_sports_covered_passes():
    t = {"KXNBA-1": mk("Sports", "NBA", "KXNBA", 100)}
    tiers = compute_tiers({"KXNBA-1": 100})
    rep = audit(DATE, t, {"KXNBA-1": 10.0}, set(), {"KXNBA": "Sports"},
                policy(), tiers, None)
    assert rep["s3"]["missed"] == [] and rep["s3"]["status"] == "pass"


# --- S4 -------------------------------------------------------------------------
def test_s4_depth_targets_ranked_by_volume_x_spread():
    rep = run_fixture_audit()
    rows = rep["s4"]["targets"]
    # only spread-measurable (L1-covered) markets are scoreable
    assert {r["market_ticker"] for r in rows} == set(l1_spreads())
    assert rows[0]["market_ticker"] == "KXNBA-COVERED-A"
    assert rows[0]["score"] == 1000_0000 * 200.0
    assert rows[0]["rank"] == 1
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert rep["s4"]["n_unscored"] == 17 - len(rows)  # traded w/o measurable spread


# --- V15 ------------------------------------------------------------------------
def test_v15_shrinkage_is_error():
    rep = run_fixture_audit(full_set={"A", "B"}, declared={"A", "B", "C"})
    assert rep["v15"]["shrinkage"] == ["C"]
    assert rep["v15"]["status"] == "error"
    assert exit_code(rep) == 1                       # error semantics, not warning


def test_v15_exact_match_passes():
    rep = run_fixture_audit(full_set={"A", "B"}, declared={"A", "B"})
    assert rep["v15"]["status"] == "pass"
    assert rep["v15"]["shrinkage"] == [] and rep["v15"]["extras"] == []
    assert exit_code(rep) == 0


def test_v15_undeclared_extras_are_warning_not_error():
    rep = run_fixture_audit(full_set={"A", "LEFTOVER"}, declared={"A"})
    assert rep["v15"]["extras"] == ["LEFTOVER"]
    assert rep["v15"]["status"] == "warn"
    assert exit_code(rep) == 0


def test_v15_missing_declared_list_documented_not_invented():
    rep = run_fixture_audit(full_set={"A"}, declared=None)
    assert rep["v15"]["status"] == "declared_list_missing"
    assert rep["v15"]["declared"] is None
    assert rep["v15"]["observed"] == ["A"]
    assert exit_code(rep) == 0


def test_read_declared_list_missing_file_and_parsing(tmp_path):
    assert read_declared_list(str(tmp_path / "nope.txt")) is None
    p = tmp_path / "watch.txt"
    p.write_text("# legacy watchlist\nAAA\n\n  BBB  \n")
    assert read_declared_list(str(p)) == {"AAA", "BBB"}


# --- promotions (report-only; Q7 exclusion) ---------------------------------------
def test_promotion_candidates_class_b_high_mid_only():
    rep = run_fixture_audit()
    cand = {r["market_ticker"]: r for r in rep["promotions"]["candidates"]}
    assert "KXENT-HOT-B" in cand                     # Class B, Mid tier
    assert cand["KXENT-HOT-B"]["liquidity_tier"] == "Mid"
    assert "KXMVE-COMBO-B" not in cand               # Q7: Exotics/MVE never promoted
    assert rep["promotions"]["q7_excluded_count"] == 1
    assert "KXNBA-MISSED-A" not in cand              # already Class A
    assert "KXQZZ-NEWCAT" not in cand                # Low tier


# --- CSV outputs ------------------------------------------------------------------
def test_csv_writers(tmp_path):
    rep = run_fixture_audit()
    dt = str(tmp_path / ("depth_target_%s.csv" % DATE))
    pc = str(tmp_path / ("promotion_candidates_%s.csv" % DATE))
    write_depth_targets(rep["s4"]["targets"], dt)
    write_promotions(rep["promotions"]["candidates"], pc)
    with open(dt, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows and rows[0]["market_ticker"] == "KXNBA-COVERED-A"
    for col in ("rank", "market_ticker", "category", "subcategory",
                "record_class", "liquidity_tier", "volume_e4", "n_trades",
                "avg_spread_e4", "score"):
        assert col in rows[0]
    with open(pc, newline="") as f:
        prows = list(csv.DictReader(f))
    assert any(r["market_ticker"] == "KXENT-HOT-B" for r in prows)
    for col in ("market_ticker", "category", "subcategory", "liquidity_tier",
                "volume_e4", "n_trades", "avg_spread_e4", "reason"):
        assert col in prows[0]


# --- render honesty ----------------------------------------------------------------
def test_render_surfaces_everything_loudly():
    rep = run_fixture_audit(full_set={"A"}, declared={"A", "GONE"})
    txt = render(rep)
    assert "V14" in txt and "KXNBA-MISSED-A" in txt
    assert "V15" in txt and "GONE" in txt and "SHRINKAGE" in txt.upper()
    assert "V16" in txt and "Quantum Fizzbin" in txt
    assert "S3" in txt and "Sports" in txt
    assert "KXGHOST" in txt                          # unlisted series surfaced


# --- lifecycle stage: appended, NON-BLOCKING by construction -----------------------
def _patched_lc(monkeypatch, tmp_path):
    import tools.lifecycle_check as lc
    monkeypatch.setattr(lc, "WORK", str(tmp_path))
    monkeypatch.setattr(lc, "EVENTS_FILE", str(tmp_path / "lifecycle_events.ndjson"))
    return lc


def test_lifecycle_stage_skipped_when_no_output(monkeypatch, tmp_path):
    lc = _patched_lc(monkeypatch, tmp_path)
    st = lc.coverage_audit_stage()
    assert st["status"] == "skipped"
    assert st["id"] == "coverage_audit"


def test_lifecycle_stage_pass_when_output_exists(monkeypatch, tmp_path):
    lc = _patched_lc(monkeypatch, tmp_path)
    mm = tmp_path / "mm"
    mm.mkdir()
    (mm / "depth_target_2026-07-06.csv").write_text("rank,market_ticker\n1,X\n")
    (mm / "promotion_candidates_2026-07-06.csv").write_text("market_ticker\nY\n")
    st = lc.coverage_audit_stage()
    assert st["status"] == "pass"
    assert "2026-07-06" in st["summary"]


def test_lifecycle_stage_never_reddens(monkeypatch, tmp_path):
    lc = _patched_lc(monkeypatch, tmp_path)
    # empty, partial, and populated states: only pass/skipped ever come back
    assert lc.coverage_audit_stage()["status"] in ("pass", "skipped")
    (tmp_path / "mm").mkdir()
    assert lc.coverage_audit_stage()["status"] in ("pass", "skipped")
    (tmp_path / "mm" / "depth_target_2026-07-06.csv").write_text("bad")
    assert lc.coverage_audit_stage()["status"] in ("pass", "skipped")
    assert lc.coverage_audit_stage()["status"] in lc.VALID_STATUS


def test_lifecycle_stage_registered_and_nonblocking():
    import tools.lifecycle_check as lc
    ids = [sid for sid, _, _ in lc.STAGES]
    assert "coverage_audit" in ids
    sid, label, deps = [s for s in lc.STAGES if s[0] == "coverage_audit"][0]
    assert deps == []                                # nothing downstream depends on it
    # and nothing depends ON it either (it can never block another stage)
    assert all("coverage_audit" not in d for _, _, d in lc.STAGES if _ != "coverage_audit")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
