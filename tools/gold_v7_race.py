"""W3.2 race/consistency report, V7, REPORT-ONLY (PLAN_GOLD_DATA_CONTRACT
§2.2 point 5 + §2.3 V7 row + W3.2) — day-one economic-consistency
measurement over one gold day. NO blocking thresholds.

The measurement: for each TRADE record, does it print at the as-of best?
  taker=yes  =>  trade_yes_price_e4 == ask_px_e4[0]   (lifted the ask)
  taker=no   =>  trade_yes_price_e4 == bid_px_e4[0]   (hit the bid)
The as-of book is the state the trade record itself carries — the state
referenced by (market_id, book_seq); §2.2 item 4 guarantees it is the
PRE-trade book (TRADE < BOOK_DELTA at equal ts_us). races = prints outside
the as-of touch (channel races between the trade feed and the book feed).

TWO populations, measured SEPARATELY and labeled (never pooled):
  covered_book — F_BOOK_COVERED set: the as-of top is the reconstructed
                 full-depth book (day one: ~4 watchlist markets);
  l1_asof      — uncovered (the vast majority): the as-of top is the L1
                 state served in slot 0 by the FSM.

Honesty split (D2): a trade whose as-of book is invalid (F_BOOK_VALID=0,
including book_seq 0 = no state ever emitted) or whose reference side is
empty is UNMEASURABLE — reported separately (book_invalid / side_empty /
bad_taker_side), NEVER counted as a race.

BINDING policy (§2.2 point 5, quoted): "day one is REPORT-ONLY. No
hardcoded blocking threshold (0.1% or otherwise). Race rate is reported by
category, subcategory, market, market class (A/B), and liquidity tier.
After operator approval, category-specific thresholds MAY become blocking."
Accordingly this module contains NO threshold-enforcement code path — the
CLI can only `return 0`; failures are exceptions from the tool itself, never
from a race rate (grep-proven by tests/test_gold_v7_race.py).

PROPOSED thresholds (FOR OPERATOR APPROVAL — not enforced): nearest-rank
p95 of per-market race rates, per population per category (plus _global),
over markets with >= MIN_MEASURABLE measurable trades (below that there is
no statistical support; mirrors V5's MIN_CHECKABLE rationale). Slices whose
race rate exceeds the applicable proposal (market slices use their
category's proposal, falling back to _global; aggregate slices use _global)
are marked unsafe_for_microstructure=true in the report AND in the manifest
verdict field (safety_verdicts.unsafe_for_microstructure) — fill simulation
/ queue studies must refuse them; the day stays valid for other research
(spread/vol calibration). Marked, never failed.

Market class: A/B per category from config/market_classes.yaml (the
category-driven policy; unknown/unlisted category => B, matching the V16
safety-net default). Subcategory is NOT in the markets sidecar (BACKLOG
W3.1 note) — the CLI fetches it from warehouse trades rows (read-only via
load()) with a lock-retry loop (staging is lock-busy in bursts; D6 readers
retry); tests inject it.

Manifest verdict update (V5 pattern, gold_v5_delta.update_manifest_v5):
safety_verdicts.V7 and safety_verdicts.unsafe_for_microstructure are
replaced IN PLACE and nothing else — the certified md5s (manifest["files"])
and the V5 verdict are asserted byte-identical before writing; the write is
tmp+os.replace atomic; GoldDayReader is re-opened afterwards to prove the
certification chain still verifies. The report file itself is NOT added to
manifest["files"].

Exit codes: 0 always (report-only) — nonzero only when the tool itself
errors (exception). stdlib + numpy only; warehouse/duckdb imported lazily
inside fetch_subcategories (CLI path only).
"""
import argparse
import json
import math
import os
import sys
import time
from collections import OrderedDict

import numpy as np

try:
    from tools.gold_dtype import EVENT_TYPE, FLAGS
    from tools.gold_io import GoldDayReader, GoldIOError, day_paths
except ImportError:  # imported as a plain module from tools/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gold_dtype import EVENT_TYPE, FLAGS
    from gold_io import GoldDayReader, GoldIOError, day_paths

MIN_MEASURABLE = 20     # min measurable trades before a market can seed a
                        # proposal or be marked (no statistical support below)
PROPOSAL_PCT = 0.95     # nearest-rank percentile for PROPOSED thresholds
HEADING_FOR_APPROVAL = "FOR OPERATOR APPROVAL — not enforced"
LABEL = "REPORT-ONLY (day one — no blocking thresholds; §2.2 point 5)"
REPORT_NAME = "v7_race_report_%s.json"
CLASSES_YAML = "config/market_classes.yaml"
POP_COVERED, POP_L1 = "covered_book", "l1_asof"
TOP_MARKETS = 30        # printed per-market rows (full detail in the JSON)
_TRADE = EVENT_TYPE["TRADE"]
_UNMEASURABLE = ("book_invalid", "side_empty", "bad_taker_side")
_COUNTS = ("n_trades", "n_measurable", "n_races") + _UNMEASURABLE


# --------------------------------------------------------------- pure pieces

def measure_trades(records):
    """Vectorized per-trade measurement over a gold record array.
    Returns {population: {counter_name: int64 array indexed by market_id}}
    with the _COUNTS counters. taker=yes checks the as-of best ask,
    taker=no the as-of best bid; invalid book / empty reference side /
    unknown taker_side are UNMEASURABLE buckets, never races."""
    idx = np.nonzero(records["event_type"] == _TRADE)[0]
    if idx.size == 0:
        z = np.zeros(0, dtype=np.int64)
        return {p: {c: z for c in _COUNTS} for p in (POP_COVERED, POP_L1)}
    mid = records["market_id"][idx].astype(np.int64)
    flags = records["flags"][idx].astype(np.int64)
    side = records["taker_side"][idx]
    px = records["trade_yes_price_e4"][idx].astype(np.int64)
    bid0 = records["bid_px_e4"][idx, 0].astype(np.int64)
    ask0 = records["ask_px_e4"][idx, 0].astype(np.int64)
    bn = records["bid_nlevels"][idx].astype(np.int64)
    an = records["ask_nlevels"][idx].astype(np.int64)

    covered = (flags & FLAGS["F_BOOK_COVERED"]) != 0
    valid = (flags & FLAGS["F_BOOK_VALID"]) != 0
    is_yes, is_no = side == 1, side == 2
    known_side = is_yes | is_no
    side_present = (is_yes & (an > 0)) | (is_no & (bn > 0))
    measurable = valid & side_present
    ref = np.where(is_yes, ask0, bid0)      # the as-of touch for this taker
    race = measurable & (px != ref)         # print outside the as-of touch

    n = int(mid.max()) + 1
    out = {}
    for pop_name, pop in ((POP_COVERED, covered), (POP_L1, ~covered)):
        out[pop_name] = {
            "n_trades": np.bincount(mid[pop], minlength=n),
            "n_measurable": np.bincount(mid[pop & measurable], minlength=n),
            "n_races": np.bincount(mid[pop & race], minlength=n),
            "book_invalid": np.bincount(mid[pop & ~valid], minlength=n),
            "side_empty": np.bincount(
                mid[pop & valid & known_side & ~side_present], minlength=n),
            "bad_taker_side": np.bincount(
                mid[pop & valid & ~known_side], minlength=n),
        }
    return out


def _rate(races, measurable):
    return round(races / measurable, 6) if measurable else None


def propose_thresholds(per_market, min_measurable=MIN_MEASURABLE,
                       q=PROPOSAL_PCT):
    """PROPOSED thresholds — FOR OPERATOR APPROVAL, not enforced (§2.2
    point 5). Nearest-rank p95 of per-market race rates per category plus
    _global, over markets with >= min_measurable measurable trades.
    Returns {} when no market qualifies (no pool, no proposals — an empty
    pool never invents a threshold)."""
    pool = [e for e in per_market
            if e["n_measurable"] >= min_measurable
            and e["race_rate"] is not None]
    if not pool:
        return {}

    def p_rank(entries):
        rates = sorted(e["race_rate"] for e in entries)
        return {"threshold": rates[max(0, math.ceil(q * len(rates)) - 1)],
                "n_markets_in_pool": len(rates)}

    out = {"_global": p_rank(pool)}
    by_cat = OrderedDict()
    for e in pool:
        by_cat.setdefault(e["category"] or "_none", []).append(e)
    for cat in sorted(by_cat):
        out[cat] = p_rank(by_cat[cat])
    return out


def _slice_stats(entries):
    """Aggregate per-market entries into one breakdown/totals row."""
    st = {"n_markets": len(entries)}
    for k in ("n_trades", "n_measurable", "n_races", "n_unmeasurable"):
        st[k] = sum(e[k] for e in entries)
    st["unmeasurable"] = {u: sum(e["unmeasurable"][u] for e in entries)
                          for u in _UNMEASURABLE}
    st["race_rate"] = _rate(st["n_races"], st["n_measurable"])
    st["unsafe_for_microstructure"] = False       # marking pass may set it
    return st


def _read_dim(root, date):
    """markets sidecar -> {market_id: {ticker, category, liquidity_tier}}."""
    import csv
    out = {}
    with open(day_paths(root, date)["markets"], newline="") as f:
        for row in csv.DictReader(f):
            out[int(row["market_id"])] = {
                "market_ticker": row["market_ticker"],
                "category": row.get("category", ""),
                "liquidity_tier": row.get("liquidity_tier", "")}
    return out


def _norm_category(c):
    """The sidecar dim carries PATH-SANITIZED category names
    ("Climate_and_Weather"); config/market_classes.yaml carries the live
    Kalshi names ("Climate and Weather"). Normalize both sides before
    class comparison or a class-A category silently reports as B (D2)."""
    return (c or "").replace(" ", "_")


def load_class_a(path=CLASSES_YAML):
    """Class-A category set from the recording policy config (read-only).
    Unknown/unlisted categories are class B (V16 safety-net default)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from build_classification import load_yaml_classes
    class_a, _ = load_yaml_classes(path)
    return set(class_a)


# ------------------------------------------------------------ the measurement

def measure_day(root, date, subcategories=None, reader=None, class_a=None,
                subcat_source=None):
    """Measure V7 for one gold day. subcategories: {market_ticker:
    subcategory} (the sidecar dim has no subcategory column — injected by
    tests, fetched from warehouse trades rows by the CLI). class_a: set of
    class-A categories (CLI loads config/market_classes.yaml). Returns the
    report dict. REPORT-ONLY: measures, proposes, marks — never fails."""
    if reader is None:
        reader = GoldDayReader(root, date)      # md5-verified open
    subcategories = subcategories or {}
    class_a = {_norm_category(c) for c in (class_a or ())}
    dim = _read_dim(root, date)
    counts = measure_trades(reader.records)

    populations, proposals_by_pop, unsafe = {}, {}, []
    traded_ids = set()
    for pop_name in (POP_COVERED, POP_L1):
        c = counts[pop_name]
        per_market = []
        for m in np.nonzero(c["n_trades"])[0]:
            m = int(m)
            traded_ids.add(m)
            meta = dim.get(m, {})
            mt = meta.get("market_ticker", reader.ticker(m))
            cat = meta.get("category", "")
            nt, nm = int(c["n_trades"][m]), int(c["n_measurable"][m])
            nr = int(c["n_races"][m])
            per_market.append({
                "market_id": m,
                "market_ticker": mt,
                "category": cat,
                "subcategory": subcategories.get(mt, ""),
                "market_class": ("A" if _norm_category(cat) in class_a
                                 else "B"),
                "liquidity_tier": meta.get("liquidity_tier", ""),
                "n_trades": nt,
                "n_measurable": nm,
                "n_races": nr,
                "n_unmeasurable": nt - nm,
                "unmeasurable": {u: int(c[u][m]) for u in _UNMEASURABLE},
                "race_rate": _rate(nr, nm),
                "unsafe_for_microstructure": False})
        proposals = propose_thresholds(per_market)
        proposals_by_pop[pop_name] = proposals
        g = proposals.get("_global", {}).get("threshold")

        def mark(entry, slice_type, key, threshold, source):
            """Marking is the ONLY consequence of exceeding a proposal —
            report + manifest field; nothing fails (§2.2 point 5)."""
            if (threshold is None or entry["race_rate"] is None
                    or entry["n_measurable"] < MIN_MEASURABLE
                    or entry["race_rate"] <= threshold):
                return
            entry["unsafe_for_microstructure"] = True
            unsafe.append({"population": pop_name, "slice": slice_type,
                           "key": key, "race_rate": entry["race_rate"],
                           "n_measurable": entry["n_measurable"],
                           "proposed_threshold": threshold,
                           "threshold_source": source})

        for e in per_market:
            cat = e["category"] or "_none"
            if cat in proposals:
                mark(e, "market", e["market_ticker"],
                     proposals[cat]["threshold"], "category:%s" % cat)
            else:
                mark(e, "market", e["market_ticker"], g, "_global")

        def breakdown(key, slice_type):
            groups = OrderedDict()
            for e in per_market:
                groups.setdefault(e[key] or "_none", []).append(e)
            out = OrderedDict()
            for name in sorted(groups):
                st = _slice_stats(groups[name])
                mark(st, slice_type, name, g, "_global")
                out[name] = st
            return out

        totals = _slice_stats(per_market)
        mark(totals, "population", pop_name, g, "_global")
        populations[pop_name] = {
            "totals": totals,
            "per_market": per_market,
            "by_category": breakdown("category", "category"),
            "by_subcategory": breakdown("subcategory", "subcategory"),
            "by_class": breakdown("market_class", "market_class"),
            "by_liquidity_tier": breakdown("liquidity_tier",
                                           "liquidity_tier")}

    n_trades_total = sum(populations[p]["totals"]["n_trades"]
                         for p in populations)
    report = {
        "check": "V7",
        "date": date,
        "root": root,
        "label": LABEL,
        "report_only": True,
        "policy": {
            "report_only": True,
            "blocking_thresholds":
                "NONE — thresholds below are PROPOSALS; category-specific "
                "thresholds activate only after operator approval of this "
                "day-one report (§2.2 point 5)",
            "unsafe_marking":
                "slices above the proposed threshold are marked "
                "unsafe_for_microstructure (fill simulation / queue studies "
                "must refuse them); the day stays valid for other research",
            "min_measurable_to_flag": MIN_MEASURABLE,
            "proposal_percentile": PROPOSAL_PCT},
        "support": {
            "n_records": reader.record_count,
            "n_trade_records": n_trades_total,
            "n_markets_traded": len(traded_ids),
            "populations": {p: {"n_markets":
                                populations[p]["totals"]["n_markets"],
                                "n_trades":
                                populations[p]["totals"]["n_trades"]}
                            for p in populations}},
        "populations": populations,
        "proposed_thresholds": dict({"heading": HEADING_FOR_APPROVAL},
                                    **proposals_by_pop),
        "unsafe_for_microstructure": unsafe}
    if subcat_source is not None:
        report["subcategory_source"] = subcat_source
    return report


# ------------------------------------------------------------------ rendering

def _fmt_row(name, st, mark=""):
    return ("%-40s %-6s %9d %11d %8d %9s  %s%s"
            % (name, st.get("n_markets", ""), st["n_trades"],
               st["n_measurable"], st["n_races"],
               st["race_rate"] if st["race_rate"] is not None else "-",
               "UNSAFE" if st["unsafe_for_microstructure"] else "",
               mark))


def render(report):
    sup = report["support"]
    lines = [
        "V7 economic consistency — %s   *** %s ***"
        % (report["date"], report["label"]),
        "support: n_records=%d n_trade_records=%d n_markets_traded=%d"
        % (sup["n_records"], sup["n_trade_records"], sup["n_markets_traded"]),
        "races = prints outside the as-of touch (taker=yes vs best ask, "
        "taker=no vs best bid); unmeasurable (invalid book / empty side) is "
        "NEVER a race"]
    hdr = ("%-40s %-6s %9s %11s %8s %9s  %s"
           % ("slice", "mkts", "trades", "measurable", "races", "rate", ""))
    for pop_name in (POP_COVERED, POP_L1):
        p = report["populations"][pop_name]
        t = p["totals"]
        lines += ["", "population %s:" % pop_name, hdr,
                  _fmt_row("TOTAL", t),
                  "  unmeasurable=%d (book_invalid=%d side_empty=%d "
                  "bad_taker_side=%d)"
                  % (t["n_unmeasurable"], t["unmeasurable"]["book_invalid"],
                     t["unmeasurable"]["side_empty"],
                     t["unmeasurable"]["bad_taker_side"])]
        for title, key in (("by category", "by_category"),
                           ("by class (A/B)", "by_class"),
                           ("by liquidity tier", "by_liquidity_tier"),
                           ("by subcategory", "by_subcategory")):
            if not p[key]:
                continue
            lines.append(" %s:" % title)
            for name, st in p[key].items():
                lines.append(_fmt_row("  " + name, st))
        top = sorted(p["per_market"], key=lambda e: -e["n_measurable"])
        shown = top[:TOP_MARKETS] + [e for e in top[TOP_MARKETS:]
                                     if e["unsafe_for_microstructure"]]
        if shown:
            lines.append(" per market (top %d by measurable + all marked; "
                         "full detail in the JSON):" % TOP_MARKETS)
            for e in shown:
                lines.append(_fmt_row("  " + e["market_ticker"],
                                      {**e, "n_markets": ""}))
    lines += ["", "PROPOSED THRESHOLDS — %s" % HEADING_FOR_APPROVAL,
              "(nearest-rank p%d of per-market race rates; pool = markets "
              "with >= %d measurable trades; category-specific thresholds "
              "activate ONLY after operator approval — §2.2 point 5)"
              % (int(PROPOSAL_PCT * 100), MIN_MEASURABLE)]
    for pop_name in (POP_COVERED, POP_L1):
        props = report["proposed_thresholds"].get(pop_name, {})
        if not props:
            lines.append("  %s: (no qualifying markets — no proposals)"
                         % pop_name)
            continue
        lines.append("  %s:" % pop_name)
        for name in props:
            lines.append("    %-38s threshold=%-10s pool=%d markets"
                         % (name, props[name]["threshold"],
                            props[name]["n_markets_in_pool"]))
    marked = report["unsafe_for_microstructure"]
    if marked:
        lines += ["", "Unsafe for Microstructure Backtest — %d slice(s) "
                      "marked (report-only, NOTHING fails):" % len(marked)]
        for u in marked:
            lines.append("  %s %s %s: rate=%s > proposed %s (%s), "
                         "n_measurable=%d"
                         % (u["population"], u["slice"], u["key"],
                            u["race_rate"], u["proposed_threshold"],
                            u["threshold_source"], u["n_measurable"]))
    else:
        lines += ["", "Unsafe for Microstructure Backtest: no slices marked"]
    return "\n".join(lines)


# ------------------------------------------------------- report + manifest

def write_report(root, date, report):
    """Report lands INSIDE the day partition (forensics travel with the day;
    it is NOT added to the manifest's certified file set)."""
    path = os.path.join(day_paths(root, date)["dir"], REPORT_NAME % date)
    with open(path, "w") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    return path


def verdict_from_report(report):
    """The day-level V7 safety verdict stored in the manifest."""
    return {"status": ("measured" if report["support"]["n_trade_records"]
                       else "no_trades"),
            "report_only": True,
            "label": report["label"],
            "populations": {p: report["populations"][p]["totals"]
                            for p in report["populations"]},
            "proposed_thresholds": report["proposed_thresholds"],
            "n_unsafe_slices": len(report["unsafe_for_microstructure"]),
            "report_file": REPORT_NAME % report["date"]}


def update_manifest_v7(root, date, report):
    """Fill safety_verdicts.V7 + safety_verdicts.unsafe_for_microstructure
    IN PLACE (V5 pattern). The certified md5s (manifest["files"]) AND the V5
    verdict are asserted byte-identical before writing; the write is atomic
    (tmp + os.replace); the strict reader is re-opened afterwards to PROVE
    the certification chain still verifies (module docstring)."""
    path = day_paths(root, date)["manifest"]
    with open(path) as f:
        man = json.load(f)
    if "safety_verdicts" not in man:
        raise GoldIOError("manifest %s has no safety_verdicts block" % path)
    files_before = json.dumps(man.get("files"), sort_keys=True)
    v5_before = json.dumps(man["safety_verdicts"].get("V5"), sort_keys=True)
    man["safety_verdicts"]["V7"] = verdict_from_report(report)
    man["safety_verdicts"]["unsafe_for_microstructure"] = (
        report["unsafe_for_microstructure"])
    if json.dumps(man.get("files"), sort_keys=True) != files_before:
        raise GoldIOError("verdict update would touch certified md5s — "
                          "REFUSED (BACKLOG W2.4 binding note)")
    if json.dumps(man["safety_verdicts"].get("V5"),
                  sort_keys=True) != v5_before:
        raise GoldIOError("V7 verdict update would touch the V5 verdict — "
                          "REFUSED (W3.2 allowed writes)")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(man, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    GoldDayReader(root, date)          # re-verify every certified md5, loudly
    return path


# ----------------------------------------------------------------- warehouse

def fetch_subcategories(date, tickers, attempts=8, wait_s=15):
    """{market_ticker: subcategory} for the traded tickers, from warehouse
    trades rows (read-only via load(); the markets sidecar has no
    subcategory column — BACKLOG W3.1 note). Staging can be lock-busy in
    bursts (live ingest daemon; BACKLOG W3.1: load()'s internal retry was
    once exhausted) — lock errors are retried here, other errors raise."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import warehouse                   # lazy: duckdb only on the CLI path
    want = set(tickers)
    for attempt in range(attempts):
        try:
            rel = warehouse.load("trades", start=date, end=date,
                                 columns=["market_ticker", "subcategory"])
            subcat = {}
            while True:
                batch = rel.fetchmany(500_000)
                if not batch:
                    break
                for mt, sc in batch:
                    if mt in want and mt not in subcat and isinstance(sc, str):
                        subcat[mt] = sc
            return subcat, {"source": "warehouse trades (read-only)",
                            "markets_wanted": len(want),
                            "markets_resolved": len(subcat),
                            "attempts": attempt + 1}
        except Exception as e:
            if "lock" not in str(e).lower() or attempt == attempts - 1:
                raise
            print("[v7] warehouse lock-busy (attempt %d/%d): %s — retrying "
                  "in %ds" % (attempt + 1, attempts, e, wait_s), flush=True)
            time.sleep(wait_s)


# ------------------------------------------------------------------------- CLI

def main(argv=None, fetch=fetch_subcategories):
    ap = argparse.ArgumentParser(
        description="W3.2: V7 economic-consistency (race) report over one "
                    "gold day — REPORT-ONLY, day one: no blocking "
                    "thresholds; proposed thresholds are printed FOR "
                    "OPERATOR APPROVAL and slices exceeding them are marked "
                    "unsafe_for_microstructure, never failed "
                    "(PLAN_GOLD_DATA_CONTRACT §2.2 point 5).")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD partition date")
    ap.add_argument("--root", default="work/gold")
    a = ap.parse_args(argv)

    t0 = time.monotonic()
    reader = GoldDayReader(a.root, a.date)     # md5-verified open (once)
    recs = reader.records
    traded = [reader.ticker(int(m)) for m in
              np.unique(recs["market_id"][recs["event_type"] == _TRADE])]
    print("[%6.1fs] gold day open: %d records; markets traded: %d"
          % (time.monotonic() - t0, reader.record_count, len(traded)),
          flush=True)
    subcat, summary = fetch(a.date, traded)
    print("[%6.1fs] subcategories fetched: %s"
          % (time.monotonic() - t0, json.dumps(summary, sort_keys=True)),
          flush=True)
    report = measure_day(a.root, a.date, subcat, reader=reader,
                         class_a=load_class_a(), subcat_source=summary)
    print("[%6.1fs] measured" % (time.monotonic() - t0), flush=True)
    print(render(report))
    rpath = write_report(a.root, a.date, report)
    mpath = update_manifest_v7(a.root, a.date, report)
    print("report: %s" % rpath)
    print("manifest V7 verdict + unsafe_for_microstructure updated "
          "(certified md5s and V5 verdict untouched, reader re-verified): %s"
          % mpath)
    print("V7 MEASURED (REPORT-ONLY): trades=%d, %d slice(s) marked Unsafe "
          "for Microstructure Backtest — nothing fails; thresholds are "
          "PROPOSALS awaiting operator approval"
          % (report["support"]["n_trade_records"],
             len(report["unsafe_for_microstructure"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
