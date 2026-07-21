"""W3.1 δ distribution, V5 (PLAN_GOLD_DATA_CONTRACT §2.3 V5 row + W3.1) —
measure the L1<->book agreement window δ as a DISTRIBUTION, never a scalar.

For COVERED markets only (markets with a full-depth BOOK_SNAPSHOT this day):
the gold .bin carries the reconstructed book as-of every record, so the
reconstructed top-of-book timeline is (ts_us, bid_px_e4[0], ask_px_e4[0]) over
the F_BOOK_VALID BOOK_SNAPSHOT/BOOK_DELTA records. The L1 channel's own view
of those markets is NOT in the .bin (for covered markets the FSM state at an
L1_TICKER record is the full-depth book, and event payloads are not
serialized) — so L1 change rows come from the warehouse `orderbooks_l1` table
(read-only via load(); W3.1 allowed reads: gold file + L1), routed through the
W2.1 gold_load.load_l1 gates (D3 at the boundary; scheduler heartbeats are
excluded and counted — they replay remembered state, they are not channel
observations).

Matching rule (per L1 change row):
  - normalize both sides to L1's empty-side sentinels (no bid => 0, no ask
    => 10000; book arrays store zeros for an empty ask side);
  - scan book records within the FIXED window ±SCAN_BOUND_US around the L1
    row's ts_us;
  - matched: some in-window record's top equals the L1 view; δ = |Δts| to
    the NEAREST such record;
  - never_agree (a REAL mismatch): in-window records exist, none agrees;
  - uncheckable: no book record inside the window at all — no evidence
    either way, reported separately, NEVER counted as a mismatch (D2:
    covered capture ran ~hours; L1 rows outside that window prove nothing).

BINDING (§2.3 V5): widening δ to absorb mismatches is FORBIDDEN. SCAN_BOUND_US
is a code constant — deliberately NOT a CLI flag; changing it is a code change
requiring operator approval. Markets whose mismatch rate stays high at the
global p99 δ go on the surfaced bad-markets list, never into a looser window:
  mismatch_rate_at_global_p99 = (never_agree + matched-with-δ>global-p99)
                                / checkable rows,
  flagged when rate > BAD_MARKET_RATE (0.05 — 5x the ~1% that sits beyond p99
  by construction) AND checkable >= MIN_CHECKABLE (20 — below that there is no
  statistical support to condemn a market; its mismatches are still counted).

Percentiles are nearest-rank on µs integers (deterministic, no interpolation,
no floats in the accounting — ms rendering happens only at the report edge).

Support size is printed prominently (audit G4: day one is ~4 full-depth
markets x ~hours) and the whole report is labeled
"BASELINE SAMPLE (support: N markets)" — day-one δ is NOT global truth.

Manifest verdict update (BACKLOG W2.4 note, binding): the manifest is the
UNHASHED root of trust — safety_verdicts.V5 is replaced IN PLACE and nothing
else: the certified md5s (manifest["files"]) and the certified files
themselves stay byte-identical (asserted before writing; write is
tmp+os.replace atomic; GoldDayReader is re-opened afterwards to prove the
certification chain still verifies). The report file itself is NOT added to
manifest["files"] — verdict updates must not touch the certified set.

Exit codes: 0 measured, no bad markets; 1 bad markets surfaced (loud, never
absorbed); 3 no covered support (measured nothing — recorded as
status="no_support", never a lying green table). stdlib + numpy only;
warehouse/duckdb imported lazily inside fetch_l1_views (CLI path only).
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

SCAN_BOUND_US = 5_000_000     # FIXED search window; no CLI knob (see docstring)
BAD_MARKET_RATE = 0.05        # mismatch rate at global p99 δ that flags a market
MIN_CHECKABLE = 20            # min checkable rows before a market can be flagged
REPORT_NAME = "v5_delta_report_%s.json"
_PCTS = (("p50", 0.50), ("p90", 0.90), ("p99", 0.99))
_SNAP, _DELTA = EVENT_TYPE["BOOK_SNAPSHOT"], EVENT_TYPE["BOOK_DELTA"]
_EMPTY_ASK_E4 = 10000         # L1 empty-side sentinels (gold_load contract)


# --------------------------------------------------------------- pure pieces

def book_timelines(records):
    """Gold records -> ({market_id: (ts, bid_top, ask_top) int64 arrays over
    the VALID book records}, {market_id: n_full_depth_rows}). Covered =
    has a BOOK_SNAPSHOT this day (V10's definition). Tops are normalized to
    the L1 sentinel encoding: empty bid side => 0, empty ask side => 10000."""
    et = records["event_type"]
    is_book = (et == _SNAP) | (et == _DELTA)
    timelines, n_full = {}, {}
    for mid in np.unique(records["market_id"][et == _SNAP]):
        sel = is_book & (records["market_id"] == mid)
        n_full[int(mid)] = int(sel.sum())
        idx = np.nonzero(sel & ((records["flags"] & FLAGS["F_BOOK_VALID"]) != 0))[0]
        ts = records["ts_us"][idx].astype(np.int64)
        bid = np.where(records["bid_nlevels"][idx] > 0,
                       records["bid_px_e4"][idx, 0], 0).astype(np.int64)
        ask = np.where(records["ask_nlevels"][idx] > 0,
                       records["ask_px_e4"][idx, 0], _EMPTY_ASK_E4).astype(np.int64)
        timelines[int(mid)] = (ts, bid, ask)
    return timelines, n_full


def match_views(ts, bid, ask, views, bound_us):
    """One market's valid-book timeline vs its L1 views [(ts_us, bid, ask)].
    Returns (matched_deltas_us list, n_never_agree, n_uncheckable). ts must
    be non-decreasing (per-market gold order is, by V3)."""
    matched, never, uncheckable = [], 0, 0
    for vts, vbid, vask in views:
        lo = int(np.searchsorted(ts, vts - bound_us, "left"))
        hi = int(np.searchsorted(ts, vts + bound_us, "right"))
        if lo == hi:
            uncheckable += 1
            continue
        agree = np.nonzero((bid[lo:hi] == vbid) & (ask[lo:hi] == vask))[0]
        if agree.size == 0:
            never += 1
            continue
        matched.append(int(np.abs(ts[lo + agree] - vts).min()))
    return matched, never, uncheckable


def _pct_us(sorted_us, q):
    """Nearest-rank percentile on a sorted µs int list (deterministic)."""
    return sorted_us[max(0, math.ceil(q * len(sorted_us)) - 1)]


def delta_stats_ms(deltas_us):
    """µs deltas -> {p50, p90, p99, max} in ms (3 decimals: µs precision).
    None when there are no matched pairs — an empty pool never renders a δ."""
    if not deltas_us:
        return None
    s = sorted(deltas_us)
    out = {name: round(_pct_us(s, q) / 1000.0, 3) for name, q in _PCTS}
    out["max"] = round(s[-1] / 1000.0, 3)
    return out


def _slice_stats(entries, p99_us):
    """Aggregate per-market entries into one breakdown row."""
    deltas = [d for e in entries for d in e["_deltas_us"]]
    never = sum(e["n_never_agree"] for e in entries)
    beyond = never + sum(1 for d in deltas if p99_us is not None and d > p99_us)
    checkable = sum(e["n_checkable"] for e in entries)
    return {"n_markets": len(entries),
            "n_l1_rows": sum(e["n_l1_rows"] for e in entries),
            "n_checkable": checkable,
            "n_matched": len(deltas),
            "n_never_agree": never,
            "n_uncheckable": sum(e["n_uncheckable"] for e in entries),
            "n_beyond_global_p99": beyond,
            "mismatch_rate_at_global_p99":
                round(beyond / checkable, 6) if checkable else None,
            "delta_ms": delta_stats_ms(deltas)}


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


# ------------------------------------------------------------ the measurement

def measure_day(root, date, l1_views, subcategories=None, reader=None,
                l1_source=None):
    """Measure V5 for one gold day. l1_views: {market_ticker: [(ts_us,
    yes_bid_e4, yes_ask_e4), ...]} — the L1 channel's view (injected by
    tests; fetched from the warehouse by the CLI). subcategories:
    {market_ticker: subcategory} (the sidecar dim has no subcategory column;
    the warehouse rows do). Returns the report dict."""
    if reader is None:
        reader = GoldDayReader(root, date)      # md5-verified open
    subcategories = subcategories or {}
    timelines, n_full = book_timelines(reader.records)
    dim = _read_dim(root, date)

    per_market = []
    for mid in sorted(timelines):
        ts, bid, ask = timelines[mid]
        meta = dim.get(mid, {})
        mt = meta.get("market_ticker", reader.ticker(mid))
        views = l1_views.get(mt, [])
        matched, never, uncheckable = match_views(ts, bid, ask, views,
                                                  SCAN_BOUND_US)
        per_market.append({
            "market_id": mid,
            "market_ticker": mt,
            "category": meta.get("category", ""),
            "subcategory": subcategories.get(mt, ""),
            "liquidity_tier": meta.get("liquidity_tier", ""),
            "n_l1_rows": len(views),
            "n_full_depth_rows": n_full[mid],
            "n_valid_book_states": int(len(ts)),
            "n_checkable": len(matched) + never,
            "n_matched": len(matched),
            "n_never_agree": never,
            "n_uncheckable": uncheckable,
            "_deltas_us": matched})

    all_deltas = [d for e in per_market for d in e["_deltas_us"]]
    p99_us = _pct_us(sorted(all_deltas), 0.99) if all_deltas else None
    bad_markets = []
    for e in per_market:
        beyond = e["n_never_agree"] + sum(
            1 for d in e["_deltas_us"] if p99_us is not None and d > p99_us)
        e["n_beyond_global_p99"] = beyond
        e["mismatch_rate_at_global_p99"] = (
            round(beyond / e["n_checkable"], 6) if e["n_checkable"] else None)
        e["delta_ms"] = delta_stats_ms(e["_deltas_us"])
        e["bad_market"] = bool(
            e["n_checkable"] >= MIN_CHECKABLE and
            e["mismatch_rate_at_global_p99"] > BAD_MARKET_RATE)
        if e["bad_market"]:
            bad_markets.append({
                "market_ticker": e["market_ticker"],
                "market_id": e["market_id"],
                "mismatch_rate_at_global_p99": e["mismatch_rate_at_global_p99"],
                "n_never_agree": e["n_never_agree"],
                "n_checkable": e["n_checkable"],
                "reason": "mismatch rate stays high at the global p99 δ — "
                          "surfaced, never absorbed into a looser window"})

    def breakdown(key):
        groups = OrderedDict()
        for e in per_market:
            groups.setdefault(e[key] or "_none", []).append(e)
        return {k: _slice_stats(v, p99_us) for k, v in groups.items()}

    book_ts = [t for mid in timelines for t in
               (int(timelines[mid][0][0]), int(timelines[mid][0][-1]))
               if len(timelines[mid][0])]
    support = {
        "n_markets": len(per_market),
        "capture_hours": (round((max(book_ts) - min(book_ts)) / 3.6e9, 2)
                          if book_ts else 0.0),
        "n_l1_rows": sum(e["n_l1_rows"] for e in per_market),
        "n_full_depth_rows": sum(e["n_full_depth_rows"] for e in per_market),
        "n_matched_pairs": len(all_deltas)}
    report = {
        "check": "V5",
        "date": date,
        "root": root,
        "label": "BASELINE SAMPLE (support: %d markets)" % support["n_markets"],
        "baseline_sample": True,
        "scan_bound_ms": SCAN_BOUND_US // 1000,
        "policy": {"widening_delta_forbidden": True,
                   "scan_bound_is_code_constant": True,
                   "bad_market_rate_threshold": BAD_MARKET_RATE,
                   "min_checkable_to_flag": MIN_CHECKABLE},
        "support": support,
        "delta_ms": delta_stats_ms(all_deltas),
        "global_p99_us": p99_us,
        "mismatches": {
            "never_agree": sum(e["n_never_agree"] for e in per_market),
            "beyond_global_p99": sum(e["n_beyond_global_p99"]
                                     for e in per_market),
            "uncheckable_no_book_in_window": sum(e["n_uncheckable"]
                                                 for e in per_market)},
        "bad_markets": bad_markets,
        "per_market": per_market,
        "by_category": breakdown("category"),
        "by_subcategory": breakdown("subcategory"),
        "by_liquidity_tier": breakdown("liquidity_tier")}
    if l1_source is not None:
        report["l1_source"] = l1_source
    for e in per_market:                       # µs arrays stay out of the JSON
        del e["_deltas_us"]
    return report


# ------------------------------------------------------------------ rendering

def _fmt_stats(st):
    if st is None:
        return "p50=-      p90=-      p99=-      max=-"
    return ("p50=%-7.3f p90=%-7.3f p99=%-7.3f max=%-7.3f"
            % (st["p50"], st["p90"], st["p99"], st["max"]))


def render(report):
    sup = report["support"]
    lines = [
        "V5 δ distribution — %s   *** %s ***" % (report["date"], report["label"]),
        "support: n_markets=%d capture_hours=%s n_l1_rows=%d "
        "n_full_depth_rows=%d n_matched_pairs=%d"
        % (sup["n_markets"], sup["capture_hours"], sup["n_l1_rows"],
           sup["n_full_depth_rows"], sup["n_matched_pairs"]),
        "scan bound: %d ms (code constant — widening δ to absorb mismatches "
        "is FORBIDDEN)" % report["scan_bound_ms"],
        "global delta_ms: %s" % _fmt_stats(report["delta_ms"]),
        "mismatches: never_agree=%d beyond_global_p99=%d "
        "uncheckable_no_book_in_window=%d (uncheckable is NOT a mismatch)"
        % (report["mismatches"]["never_agree"],
           report["mismatches"]["beyond_global_p99"],
           report["mismatches"]["uncheckable_no_book_in_window"])]
    hdr = ("%-38s %-6s %-9s %-6s %-8s %-6s %-6s %-8s %s"
           % ("slice", "l1", "checkable", "match", "mismatch", "unchk",
              "tier", "rate@p99", "delta_ms"))
    lines += ["", "per market:", hdr]
    for e in report["per_market"]:
        lines.append("%-38s %-6d %-9d %-6d %-8d %-6d %-6s %-8s %s%s"
                     % (e["market_ticker"], e["n_l1_rows"], e["n_checkable"],
                        e["n_matched"], e["n_never_agree"], e["n_uncheckable"],
                        e["liquidity_tier"],
                        e["mismatch_rate_at_global_p99"]
                        if e["mismatch_rate_at_global_p99"] is not None else "-",
                        _fmt_stats(e["delta_ms"]),
                        "   << BAD MARKET" if e["bad_market"] else ""))
    for title, key in (("by category", "by_category"),
                       ("by subcategory", "by_subcategory"),
                       ("by liquidity tier", "by_liquidity_tier")):
        lines += ["", "%s:" % title]
        for name, st in report[key].items():
            lines.append("%-38s %-6d %-9d %-6d %-8d %-6d %-6s %-8s %s"
                         % (name, st["n_l1_rows"], st["n_checkable"],
                            st["n_matched"], st["n_never_agree"],
                            st["n_uncheckable"], "-",
                            st["mismatch_rate_at_global_p99"]
                            if st["mismatch_rate_at_global_p99"] is not None
                            else "-",
                            _fmt_stats(st["delta_ms"])))
    if report["bad_markets"]:
        lines += ["", "BAD MARKETS SURFACED (%d) — never absorbed into a "
                      "looser window:" % len(report["bad_markets"])]
        for b in report["bad_markets"]:
            lines.append("  %s (id %d): rate@p99=%s never_agree=%d/%d"
                         % (b["market_ticker"], b["market_id"],
                            b["mismatch_rate_at_global_p99"],
                            b["n_never_agree"], b["n_checkable"]))
    else:
        lines += ["", "bad markets: none"]
    return "\n".join(lines)


# ------------------------------------------------------------- report + manifest

def write_report(root, date, report):
    """Report lands INSIDE the day partition (forensics travel with the day;
    it is NOT added to the manifest's certified file set)."""
    path = os.path.join(day_paths(root, date)["dir"], REPORT_NAME % date)
    with open(path, "w") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    return path


def verdict_from_report(report):
    """The day-level V5 safety verdict stored in the manifest."""
    if report["support"]["n_markets"] == 0:
        return {"status": "no_support", "baseline_sample": True,
                "label": report["label"], "support": report["support"],
                "report_file": REPORT_NAME % report["date"]}
    return {"status": "measured",
            "baseline_sample": True,
            "label": report["label"],
            "support": report["support"],
            "delta_ms": report["delta_ms"],
            "scan_bound_ms": report["scan_bound_ms"],
            "mismatches": report["mismatches"],
            "bad_markets": [{"market_ticker": b["market_ticker"],
                             "mismatch_rate_at_global_p99":
                                 b["mismatch_rate_at_global_p99"]}
                            for b in report["bad_markets"]],
            "report_file": REPORT_NAME % report["date"]}


def update_manifest_v5(root, date, report):
    """Fill safety_verdicts.V5 IN PLACE. The certified md5s
    (manifest["files"]) are asserted byte-identical before writing; the write
    is atomic (tmp + os.replace); the strict reader is re-opened afterwards
    to PROVE the certification chain still verifies (module docstring)."""
    path = day_paths(root, date)["manifest"]
    with open(path) as f:
        man = json.load(f)
    if "safety_verdicts" not in man:
        raise GoldIOError("manifest %s has no safety_verdicts block" % path)
    files_before = json.dumps(man.get("files"), sort_keys=True)
    man["safety_verdicts"]["V5"] = verdict_from_report(report)
    if json.dumps(man.get("files"), sort_keys=True) != files_before:
        raise GoldIOError("verdict update would touch certified md5s — "
                          "REFUSED (BACKLOG W2.4 binding note)")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(man, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    GoldDayReader(root, date)          # re-verify every certified md5, loudly
    return path


# ----------------------------------------------------------------- warehouse

_L1_COLS = ("ts_utc", "market_ticker", "yes_bid_e4", "yes_bid_qty_e4",
            "yes_ask_e4", "yes_ask_qty_e4", "is_snapshot",
            "price_e4", "volume_e4", "open_interest_e4", "subcategory")


def fetch_l1_views(date, tickers):
    """L1 change rows for the covered tickers, read-only via warehouse
    load(), through the W2.1 load_l1 gates. Scheduler heartbeats
    (is_snapshot AND NULL price/volume/oi — gold_build composition decision
    1) are excluded and counted: they replay remembered state, they are not
    L1 channel observations. Returns (views, subcategories, summary)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import warehouse                   # lazy: duckdb only on the CLI path
    from gold_load import load_l1
    want = set(tickers)
    rel = warehouse.load("orderbooks_l1", start=date, end=date,
                         columns=list(_L1_COLS))
    rows, subcat, n_hb = [], {}, 0
    while True:
        batch = rel.fetchmany(200_000)
        if not batch:
            break
        for t in batch:
            if t[1] not in want:
                continue
            if t[6] is True and t[7] is None and t[8] is None and t[9] is None:
                n_hb += 1              # scheduler heartbeat — excluded, counted
                continue
            if isinstance(t[10], str):
                subcat.setdefault(t[1], t[10])
            rows.append({"ts_utc": t[0], "market_ticker": t[1],
                         "yes_bid_e4": t[2], "yes_bid_qty_e4": t[3],
                         "yes_ask_e4": t[4], "yes_ask_qty_e4": t[5],
                         "is_snapshot": t[6]})
    events, rep = load_l1(rows, source="orderbooks_l1(v5)")
    views = {}
    for e in events:
        views.setdefault(e.market_ticker, []).append(
            (e.ts_us, e.payload.yes_bid_e4, e.payload.yes_ask_e4))
    summary = rep.summary()
    summary["scheduler_heartbeats_excluded"] = n_hb
    return views, subcat, summary


# ------------------------------------------------------------------------- CLI

def main(argv=None, fetch=fetch_l1_views):
    ap = argparse.ArgumentParser(
        description="W3.1: V5 L1<->book agreement window δ as a distribution "
                    "over one gold day; report + manifest verdict update. The "
                    "scan window is a code constant — widening δ to absorb "
                    "mismatches is forbidden (PLAN_GOLD_DATA_CONTRACT §2.3).")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD partition date")
    ap.add_argument("--root", default="work/gold")
    a = ap.parse_args(argv)

    t0 = time.monotonic()
    reader = GoldDayReader(a.root, a.date)     # md5-verified open (once)
    et = reader.records["event_type"]
    covered = [reader.ticker(int(m))
               for m in np.unique(reader.records["market_id"][et == _SNAP])]
    print("[%6.1fs] gold day open: %d records; covered markets: %d %s"
          % (time.monotonic() - t0, reader.record_count, len(covered),
             covered), flush=True)
    views, subcat, summary = fetch(a.date, covered)
    print("[%6.1fs] L1 views fetched: %s"
          % (time.monotonic() - t0, json.dumps(summary, sort_keys=True)),
          flush=True)
    report = measure_day(a.root, a.date, views, subcat, reader=reader,
                         l1_source=summary)
    print("[%6.1fs] measured" % (time.monotonic() - t0), flush=True)
    print(render(report))
    rpath = write_report(a.root, a.date, report)
    mpath = update_manifest_v5(a.root, a.date, report)
    print("report: %s" % rpath)
    print("manifest V5 verdict updated (certified md5s untouched, reader "
          "re-verified): %s" % mpath)
    if report["support"]["n_markets"] == 0:
        print("V5 NO SUPPORT: no covered markets this day — nothing measured")
        return 3
    print("V5 MEASURED: %s" % report["label"])
    if report["bad_markets"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
