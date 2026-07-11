#!/usr/bin/env python3
"""W4: daily coverage auditor — the scope proof for one warehouse day
(PLAN_GOLD_DATA_CONTRACT §2.3 V14/V15/V16 + W4).

  S1  universe reconciliation: catalog series vs what actually traded/quoted;
      EVERY traded market is classified (new/unlisted/null category => Class B
      default + surfaced warning, V16 — same behavior as build_classification).
  S2  liquidity tiers: reuse the gold sidecar tiering for the day if present,
      else compute from trades with gold_build.liquidity_tiers semantics.
      V14: 100% of High+Mid tier markets have L1 coverage; violations LISTED
      with category + record_class (day one is report-context: loud, non-gating).
  S3  sports completeness: every Sports market that traded has L1 rows
      (Sports is Class A); missed markets listed.
  S4  depth-target list: markets ranked by traded_volume_e4 x avg_spread_e4
      -> work/mm/depth_target_<date>.csv (feeds W6).
  V15 depth-set stability: distinct orderbooks_full markets vs the DECLARED
      subscription list (config/depth_watchlist.txt by default — the firehose
      subscribes NO orderbook_delta, so the declared list is the legacy
      watchlist; a missing file is documented, never invented). Shrinkage
      (declared-but-not-observed) is an ERROR => exit 1. Extras (observed
      leftovers) are a warning.
  Promotions (report-only): Class B markets whose tier is High/Mid ->
      work/mm/promotion_candidates_<date>.csv. Exotics/MVE stay Class B
      regardless (GUARDRAILS Q7) and are excluded (counted). This tool NEVER
      edits config/market_classes.yaml — promotions are operator-reviewed.

Read-only wrt the warehouse (tools/warehouse.py load(), reader lock-retry);
writes only work/mm/*.csv. stdlib + duckdb (via warehouse) only.
"""
import argparse
import csv
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

POLICY_PATH = os.path.join(ROOT, "config", "market_classes.yaml")
DECLARED_DEFAULT = os.path.join(ROOT, "config", "depth_watchlist.txt")
OUT_DIR_DEFAULT = os.path.join(ROOT, "work", "mm")
GOLD_ROOT_DEFAULT = os.path.join(ROOT, "work", "gold")
SPORTS_CATEGORY = "Sports"
LIST_CAP = 25          # stdout listing cap per section (full data -> CSVs)


# --- pure core -------------------------------------------------------------------
def load_class_policy(path):
    """config/market_classes.yaml -> (class_a set, class_b set). Same tiny flat
    2-list reader as build_classification.load_yaml_classes (no pyyaml)."""
    a, b, cur = set(), set(), None
    with open(path) as f:
        for line in f:
            s = line.split("#", 1)[0].rstrip()
            if not s.strip():
                continue
            if s.startswith("class_a_full_l1"):
                cur = a
                continue
            if s.startswith("class_b_trades_only"):
                cur = b
                continue
            m = re.match(r"\s*-\s*(.+?)\s*$", s)
            if m and cur is not None:
                cur.add(m.group(1))
    return a, b


def classify_category(category, a_set, b_set):
    """(record_class, unlisted). V16 safety net: any category not in the policy
    (including null/empty and the '_unclassified' sentinel) => Class B default
    + unlisted=True (warning). The warehouse stores PATH-SANITIZED category
    values (wc.sanitize: 'Climate and Weather' -> 'Climate_and_Weather',
    verified live 2026-07-06) while the policy uses human names — both
    spellings of a listed category classify without a warning."""
    import warehouse_common as wc
    if not category or category == "_unclassified":
        return "B", True
    if category in a_set or wc.sanitize(category) in {wc.sanitize(c) for c in a_set}:
        return "A", False
    if category in b_set or wc.sanitize(category) in {wc.sanitize(c) for c in b_set}:
        return "B", False
    return "B", True


def compute_tiers(volumes):
    """{ticker: volume_e4} -> {ticker: High|Mid|Low}. EXACTLY
    gold_build.liquidity_tiers semantics (parity test-enforced): rank by
    (-volume, ticker); top ceil(10%) High, through ceil(20%) Mid, rest Low."""
    ranked = sorted(volumes.items(), key=lambda kv: (-kv[1], kv[0]))
    n = len(ranked)
    hi, mid = math.ceil(n * 0.10), math.ceil(n * 0.20)
    return {mt: ("High" if r < hi else "Mid" if r < mid else "Low")
            for r, (mt, _) in enumerate(ranked)}


def read_declared_list(path):
    """Declared full-depth subscription list, one ticker per line, # comments.
    Missing file -> None (documented as declared_list_missing, never invented)."""
    if not os.path.isfile(path):
        return None
    out = set()
    with open(path) as f:
        for line in f:
            s = line.split("#", 1)[0].strip()
            if s:
                out.add(s)
    return out


def _q7_excluded(ticker, meta):
    """GUARDRAILS Q7 defense-in-depth: Exotics category or KXMVE prefix."""
    if (meta.get("category") or "") == "Exotics":
        return True
    st = meta.get("series_ticker") or ""
    return ticker.startswith("KXMVE") or st.startswith("KXMVE")


def audit(date, traded, l1_spreads, full_set, catalog_cats, policy, tiers,
          declared):
    """Pure audit over one day.
      traded:       {ticker: {category, subcategory, series_ticker,
                              volume_e4, n_trades}}
      l1_spreads:   {ticker: avg_spread_e4|None} — key present == has L1 rows
      full_set:     set of tickers with orderbooks_full rows
      catalog_cats: {series_ticker: category} from the live catalog
      policy:       (class_a set, class_b set)
      tiers:        {ticker: High|Mid|Low}
      declared:     declared full-depth set, or None if no list exists
    """
    a_set, b_set = policy

    # S1 + V16: total classification, unknown/unlisted surfaced ---------------
    classes, unknown_cat, unlisted_traded = {}, [], set()
    for mt in sorted(traded):
        cat = traded[mt].get("category")
        klass, unlisted = classify_category(cat, a_set, b_set)
        classes[mt] = klass
        if not cat or cat == "_unclassified":
            unknown_cat.append(mt)     # null sentinel = leakage, not a new category
        elif unlisted:
            unlisted_traded.add(cat)
    unlisted_catalog = sorted({c for c in catalog_cats.values()
                               if c and classify_category(c, a_set, b_set)[1]})
    traded_series = {traded[mt].get("series_ticker") for mt in traded}
    unlisted_series = sorted(s for s in traded_series
                             if s and s not in catalog_cats)
    s1 = {"n_traded": len(traded), "n_l1": len(l1_spreads),
          "n_full": len(full_set), "n_catalog_series": len(catalog_cats),
          "n_class_a": sum(1 for k in classes.values() if k == "A"),
          "n_class_b": sum(1 for k in classes.values() if k == "B"),
          "classes": classes, "unknown_category_markets": unknown_cat,
          "unlisted_series": unlisted_series,
          "status": "warn" if (unknown_cat or unlisted_series) else "pass"}
    v16 = {"unlisted_categories_traded": sorted(unlisted_traded),
           "unlisted_categories_catalog": unlisted_catalog,
           "status": "warn" if (unlisted_traded or unlisted_catalog or
                                unknown_cat) else "pass"}

    # S2 / V14: High+Mid => 100% L1 -------------------------------------------
    def row(mt):
        m = traded[mt]
        return {"market_ticker": mt, "category": m.get("category") or "",
                "subcategory": m.get("subcategory") or "",
                "record_class": classes[mt], "tier": tiers.get(mt, "Low"),
                "volume_e4": m["volume_e4"], "n_trades": m["n_trades"]}

    high_mid = [mt for mt in sorted(traded)
                if tiers.get(mt, "Low") in ("High", "Mid")]
    violations = [row(mt) for mt in high_mid if mt not in l1_spreads]
    violations.sort(key=lambda r: (-r["volume_e4"], r["market_ticker"]))
    v14 = {"n_high_mid": len(high_mid), "violations": violations,
           "status": "violations_reported" if violations else "pass"}

    # S3: sports completeness ---------------------------------------------------
    sports = [mt for mt in sorted(traded)
              if (traded[mt].get("category") or "") == SPORTS_CATEGORY]
    missed = [row(mt) for mt in sports if mt not in l1_spreads]
    missed.sort(key=lambda r: (-r["volume_e4"], r["market_ticker"]))
    s3 = {"n_sports_traded": len(sports), "missed": missed,
          "status": "violations_reported" if missed else "pass"}

    # S4: depth targets = volume x spread over measurable markets ---------------
    targets = []
    for mt in sorted(traded):
        spread = l1_spreads.get(mt)
        if spread is None:
            continue
        r = row(mt)
        r["avg_spread_e4"] = spread
        r["score"] = traded[mt]["volume_e4"] * spread
        targets.append(r)
    targets.sort(key=lambda r: (-r["score"], r["market_ticker"]))
    for i, r in enumerate(targets):
        r["rank"] = i + 1
        r["liquidity_tier"] = r.pop("tier")
    s4 = {"targets": targets, "n_unscored": len(traded) - len(targets)}

    # V15: depth-set stability ----------------------------------------------------
    observed = sorted(full_set)
    if declared is None:
        v15 = {"observed": observed, "declared": None, "shrinkage": [],
               "extras": observed, "status": "declared_list_missing"}
    else:
        shrink = sorted(set(declared) - set(full_set))
        extras = sorted(set(full_set) - set(declared))
        v15 = {"observed": observed, "declared": sorted(declared),
               "shrinkage": shrink, "extras": extras,
               "status": ("error" if shrink else
                          "warn" if extras else "pass")}

    # promotions (report-only; Q7 exclusion) ---------------------------------------
    candidates, q7 = [], 0
    for mt in high_mid:
        if classes[mt] != "B":
            continue
        if _q7_excluded(mt, traded[mt]):
            q7 += 1
            continue
        r = row(mt)
        r["avg_spread_e4"] = l1_spreads.get(mt, "")
        r["liquidity_tier"] = r.pop("tier")
        r["reason"] = "tier=%s record_class=B" % r["liquidity_tier"]
        candidates.append(r)
    candidates.sort(key=lambda r: (-r["volume_e4"], r["market_ticker"]))
    promotions = {"candidates": candidates, "q7_excluded_count": q7}

    return {"date": date, "s1": s1, "v16": v16, "v14": v14, "s3": s3,
            "s4": s4, "v15": v15, "promotions": promotions}


def exit_code(report):
    """Only V15 shrinkage is an error day-one; V14/S3/V16 are report-context."""
    return 1 if report["v15"]["status"] == "error" else 0


# --- CSV outputs -------------------------------------------------------------------
DT_COLS = ("rank", "market_ticker", "category", "subcategory", "record_class",
           "liquidity_tier", "volume_e4", "n_trades", "avg_spread_e4", "score")
PC_COLS = ("market_ticker", "category", "subcategory", "record_class",
           "liquidity_tier", "volume_e4", "n_trades", "avg_spread_e4", "reason")


def _write_csv(rows, cols, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cols), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_depth_targets(rows, path):
    _write_csv(rows, DT_COLS, path)


def write_promotions(rows, path):
    _write_csv(rows, PC_COLS, path)


# --- rendering ---------------------------------------------------------------------
def _cap(items, fmt, cap=LIST_CAP):
    lines = [fmt(x) for x in items[:cap]]
    if len(items) > cap:
        lines.append("    ... and %d more (full set in the CSVs)" % (len(items) - cap))
    return lines


def render(report):
    L = []
    s1, v16, v14, s3, s4, v15 = (report[k] for k in
                                 ("s1", "v16", "v14", "s3", "s4", "v15"))
    pro = report["promotions"]
    L.append("== COVERAGE AUDIT %s ==" % report["date"])
    L.append("S1 universe reconciliation [%s]" % s1["status"].upper())
    L.append("  traded=%d  l1=%d  full_depth=%d  catalog_series=%d  "
             "class_A=%d  class_B=%d"
             % (s1["n_traded"], s1["n_l1"], s1["n_full"],
                s1["n_catalog_series"], s1["n_class_a"], s1["n_class_b"]))
    if s1["unknown_category_markets"]:
        L.append("  NULL-category traded markets (Class B default): %d"
                 % len(s1["unknown_category_markets"]))
        L += _cap(s1["unknown_category_markets"], lambda m: "    %s" % m)
    if s1["unlisted_series"]:
        L.append("  traded series NOT in live catalog: %d" % len(s1["unlisted_series"]))
        L += _cap(s1["unlisted_series"], lambda st: "    %s" % st)

    L.append("V16 class-policy safety net [%s]" % v16["status"].upper())
    for key, label in (("unlisted_categories_traded", "traded"),
                       ("unlisted_categories_catalog", "live catalog")):
        if v16[key]:
            L.append("  WARNING: categories in %s missing from "
                     "config/market_classes.yaml (defaulted to Class B): %s"
                     % (label, ", ".join(v16[key])))
    if s1["unknown_category_markets"] and not (
            v16["unlisted_categories_traded"] or
            v16["unlisted_categories_catalog"]):
        L.append("  WARNING: %d traded markets with null/_unclassified "
                 "category (Class B default; listed under S1)"
                 % len(s1["unknown_category_markets"]))

    L.append("S2/V14 liquidity coverage (High+Mid => 100%% L1) [%s]"
             % v14["status"].upper())
    L.append("  high+mid markets=%d  violations=%d"
             % (v14["n_high_mid"], len(v14["violations"])))
    a_viol = [r for r in v14["violations"] if r["record_class"] == "A"]
    b_viol = [r for r in v14["violations"] if r["record_class"] == "B"]
    if a_viol:
        L.append("  CLASS A VIOLATIONS (capture defect — every one listed):")
        L += [("    %(market_ticker)s  tier=%(tier)s  category=%(category)s"
               "  class=%(record_class)s  vol_e4=%(volume_e4)d") % r for r in a_viol]
    if b_viol:
        by_cat = {}
        for r in b_viol:
            by_cat.setdefault(r["category"] or "<null>", []).append(r)
        L.append("  Class B violations by category (policy-expected; "
                 "promotion candidates cover the non-Q7 subset):")
        for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
            L.append("    %-28s %6d markets" % (cat, len(by_cat[cat])))
        L += _cap(b_viol, lambda r: ("    %(market_ticker)s  tier=%(tier)s  "
                                     "category=%(category)s  class=%(record_class)s"
                                     "  vol_e4=%(volume_e4)d") % r)

    L.append("S3 sports completeness [%s]" % s3["status"].upper())
    L.append("  Sports traded=%d  missed L1=%d"
             % (s3["n_sports_traded"], len(s3["missed"])))
    L += _cap(s3["missed"], lambda r: ("    MISSED %(market_ticker)s  "
                                       "tier=%(tier)s  vol_e4=%(volume_e4)d") % r)

    L.append("S4 depth targets (volume x spread): %d scoreable, %d traded "
             "markets unscored (no measurable L1 spread)"
             % (len(s4["targets"]), s4["n_unscored"]))
    L += _cap(s4["targets"][:10],
              lambda r: ("    #%(rank)-3d %(market_ticker)s  "
                         "tier=%(liquidity_tier)s class=%(record_class)s  "
                         "vol_e4=%(volume_e4)d  spread_e4=%(avg_spread_e4).1f") % r,
              cap=10)

    L.append("V15 depth-set stability [%s]" % v15["status"].upper())
    if v15["declared"] is None:
        L.append("  DECLARED LIST MISSING: the firehose subscribes no "
                 "orderbook_delta; no declared watchlist file exists. "
                 "Observed full-depth set (legacy watchlist leftovers): %s"
                 % (", ".join(v15["observed"]) or "<empty>"))
    else:
        L.append("  declared=%d observed=%d" % (len(v15["declared"]),
                                                len(v15["observed"])))
        if v15["shrinkage"]:
            L.append("  ERROR — DEPTH-SET SHRINKAGE (declared but not observed): %s"
                     % ", ".join(v15["shrinkage"]))
        if v15["extras"]:
            L.append("  warning — observed but never declared: %s"
                     % ", ".join(v15["extras"]))

    L.append("Promotion candidates (report-only; operator-reviewed; "
             "Exotics/MVE Q7-excluded=%d): %d" % (pro["q7_excluded_count"],
                                                  len(pro["candidates"])))
    L += _cap(pro["candidates"][:10],
              lambda r: ("    %(market_ticker)s  tier=%(liquidity_tier)s  "
                         "category=%(category)s  vol_e4=%(volume_e4)d") % r,
              cap=10)
    return "\n".join(L)


# --- warehouse I/O (read-only) --------------------------------------------------
def read_sidecar_tiers(gold_root, date):
    """Gold sidecar liquidity tiers for the day, or None if absent."""
    path = os.path.join(gold_root, "date=%s" % date, "markets_%s.csv" % date)
    if not os.path.isfile(path):
        return None
    tiers = {}
    csv.field_size_limit(16_000_000)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("market_ticker") and r.get("liquidity_tier"):
                tiers[r["market_ticker"]] = r["liquidity_tier"]
    return tiers or None


def gather_day(date):
    """Aggregate a final day without ever opening live staging."""
    import warehouse
    load_kwargs = {"start": date, "end": date, "archive_only": True}
    tr = warehouse.load("trades", **load_kwargs)
    traded = {}
    for mt, cat, sub, st, vol, n in tr.aggregate(
            "market_ticker, min(category), min(subcategory), "
            "min(series_ticker), sum(count_e4), count(*)",
            "market_ticker").fetchall():
        traded[mt] = {"category": cat, "subcategory": sub, "series_ticker": st,
                      "volume_e4": int(vol or 0), "n_trades": int(n)}
    l1 = warehouse.load("orderbooks_l1", **load_kwargs)
    l1_spreads = {}
    for mt, spread in l1.aggregate(
            "market_ticker, avg(CASE WHEN yes_ask_e4 IS NOT NULL AND "
            "yes_bid_e4 IS NOT NULL THEN yes_ask_e4 - yes_bid_e4 END)",
            "market_ticker").fetchall():
        l1_spreads[mt] = float(spread) if spread is not None else None
    try:
        fu = warehouse.load("orderbooks_full", **load_kwargs)
        full_set = {r[0] for r in fu.aggregate("market_ticker",
                                               "market_ticker").fetchall()}
    except FileNotFoundError:
        full_set = set()
    return traded, l1_spreads, full_set


def read_catalog_categories():
    """Live catalog series -> category (read-only parquet)."""
    import duckdb
    import warehouse_common as wc
    pq = os.path.join(wc.load_config()["warehouse_root"], "catalog", "series",
                      "part-00000.parquet")
    if not os.path.isfile(pq):
        print("WARN: live catalog missing (%s) — S1 catalog reconciliation "
              "degraded to empty catalog" % pq, file=sys.stderr)
        return {}
    con = duckdb.connect()
    rows = con.execute("SELECT ticker, category FROM read_parquet('%s')"
                       % pq.replace("'", "''")).fetchall()
    return {t: c for t, c in rows if t}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD warehouse day")
    ap.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    ap.add_argument("--policy", default=POLICY_PATH)
    ap.add_argument("--declared-list", default=DECLARED_DEFAULT,
                    help="declared full-depth subscription list (one ticker "
                         "per line); missing file is documented, not an error")
    ap.add_argument("--gold-root", default=GOLD_ROOT_DEFAULT)
    a = ap.parse_args(argv)

    policy = load_class_policy(a.policy)
    traded, l1_spreads, full_set = gather_day(a.date)
    if not traded:
        print("COVERAGE AUDIT ERROR: no trades found for %s" % a.date)
        return 1
    catalog = read_catalog_categories()

    sidecar = read_sidecar_tiers(a.gold_root, a.date)
    computed = compute_tiers({mt: m["volume_e4"] for mt, m in traded.items()})
    if sidecar:
        n_missing = sum(1 for mt in traded if mt not in sidecar)
        if n_missing <= 0.01 * len(traded):
            tiers = {mt: sidecar.get(mt, computed[mt]) for mt in traded}
            tier_source = ("gold_sidecar (%d traded markets missing from "
                           "sidecar filled from trades)" % n_missing)
        else:
            tiers, tier_source = computed, (
                "computed_from_trades (sidecar present but missing %d/%d "
                "traded markets)" % (n_missing, len(traded)))
    else:
        tiers, tier_source = computed, "computed_from_trades (no gold sidecar)"

    declared = read_declared_list(a.declared_list)
    report = audit(a.date, traded, l1_spreads, full_set, catalog, policy,
                   tiers, declared)
    print(render(report))
    print("tier source: %s" % tier_source)

    dt_path = os.path.join(a.out_dir, "depth_target_%s.csv" % a.date)
    pc_path = os.path.join(a.out_dir, "promotion_candidates_%s.csv" % a.date)
    write_depth_targets(report["s4"]["targets"], dt_path)
    write_promotions(report["promotions"]["candidates"], pc_path)
    print("depth targets        -> %s (%d rows)" % (dt_path,
                                                    len(report["s4"]["targets"])))
    print("promotion candidates -> %s (%d rows)"
          % (pc_path, len(report["promotions"]["candidates"])))

    rc = exit_code(report)
    print("COVERAGE AUDIT %s" % ("GREEN" if rc == 0 else
                                 "ERROR: depth-set shrinkage (V15)"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
