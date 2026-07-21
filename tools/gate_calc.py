#!/usr/bin/env python3
"""WP-09 Gate Calculator — the three pre-committed gate numbers, computed by
tested code (tests/test_gate_metrics.py), never by gate-day scripts.

Per clean day, over the buy-all-legs bracket-arb signal:

  1. signal_count / day
  2. signal lifespan distribution (p50 / p90 / max seconds)
  3. theoretical net profit table — GATE MODE: REFUSES while
     config/kalshi_facts.yaml fees.verified != true (mechanical,
     mm_research.trade_fee raises FeeNotVerifiedError; GUARDRAILS S2).
     --preview-unverified-fees prints the table under a screaming
     NON-GATE PREVIEW banner using the recorded (unratified) rates.

Output: one markdown report per run -> work/mm/gate_report_<today>.md,
the gate meeting's (H-4) input document.

════════════════════════════════════════════════════════════════════════
DEFINITIONS (pinned by the test fixtures — the contract)

Signal   Buying 1 contract of EVERY leg of a bracket at ask costs
         sum(asks) c and pays exactly 100c at settlement if the legs are
         mutually exclusive AND exhaustive. Gross edge = 100 - sum(asks).
         A signal fires when gross edge >= --min-edge-c (DEFAULT 2.0c:
         sums <= 98c fire, the 99-101c no-arb band is silent). One signal
         = one contiguous EPISODE (appears -> persists across book
         updates -> disappears), never one per tick. Episodes still
         active at end of data close there and are labeled censored.

LOCF     Book state at time T = the leg's most recent row <= T (the
         warehouse's change-only reconstruction rule). A leg's quote is
         usable while its age <= --max-quote-age-s (DEFAULT 3600s — the
         hourly-heartbeat cadence, i.e. the documented upper bound on
         legitimate LOCF lookback; a smaller window would falsely drop
         valid change-only state, a larger one could trust dead data).
         No evaluation happens until EVERY leg has a valid, fresh ask
         (0 < yes_ask_e4 < 10000): partial sums NEVER fire.

Bracket  Preferred source: dims (work/warehouse/dim/latest/markets.csv
universe event_structure == 'bracket', tools/dim_snapshot.py). CHECKED AT
         RUNTIME and reported — as of 2026-07-07 the catalog parquets
         carry no floor_strike/cap_strike columns, so the derivation
         degenerates and yields ZERO 'bracket' rows; the dim is also a
         today-only snapshot capped at 80,000 rows (settled markets from
         prior days are absent). FALLBACK (the effective path, documented
         heuristic): group fact rows by event_ticker; keep events with
         >= --min-legs (default 2) distinct markets, mutually_exclusive
         == true from dim/latest/events.csv, and no KXMVE/mve prefix
         (GUARDRAILS Q7 defense-in-depth). Events absent from the
         (truncated) events dim have UNKNOWN exclusivity and are EXCLUDED
         + counted loudly — a non-exclusive "bracket" sum is meaningless
         and would fabricate signals (D2/S2 fail-closed).

CAVEATS  (printed in every report)
         - Exhaustiveness is NOT verifiable from our data: mutually
           exclusive guarantees at most one leg pays, not at least one.
           Signal counts are an UPPER BOUND on true riskless arbs.
         - Leg universe = markets observed in OUR capture for that
           event-day. A leg that exists but never reached our L1 capture
           would overstate the edge; the full-quote guard covers only
           observed legs. (Dim cross-check impossible today: snapshot is
           today-only + truncated — 4 of 1,964 candidate day-06 events
           matched at all.)
         - No fill model, no queue, no depth: L1 top-of-book only.
           Gross edge assumes 1 contract lifts each ask (Q2: these are
           optimistic/diagnostic numbers; the gate's fee-net table is
           the go/no-go input, and it REFUSES until fees are ratified).

Fees     mm_research.trade_fee ONLY (reads kalshi_facts.yaml at call
         time; a second fee implementation is forbidden). Net profit per
         signal = gross_edge - sum over legs of taker fee at each leg's
         ask price at episode open, 1 contract per leg.

Usage:
  python3 tools/gate_calc.py [--start D] [--end D] [--min-edge-c F]
                             [--max-quote-age-s F] [--min-legs N]
                             [--out PATH] [--preview-unverified-fees]

Read-only on the pipeline (warehouse load() only). stdlib + duckdb/pandas
(research side, same as WP-06). WP-06 wiggle/interval context is CITED
(work/mm/research_*.html), never recomputed here.
"""
import argparse
import datetime
import glob as _glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mm_research as mr  # noqa: E402  (fee fn + pctl live there — reused)

FIRST_CLEAN_DAY = "2026-07-06"  # H-3 clean-day clock day 1
OQ1_POINTER = ("fees.verified=false in config/kalshi_facts.yaml — R must "
               "ratify the fee-schedule provenance: WP-05 OPEN QUESTION 1, "
               "docs/DISCOVERY_REPORT_2026-07-07.md (one browser check of "
               "the live kalshi.com fee-schedule PDF)")

REFUSAL_BANNER = """
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!  GATE-MODE NET PROFIT: REFUSED                                     !!
!!  %s
!!  The gate cannot be computed on assumptions (GUARDRAILS S2/Q3).    !!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
""" % OQ1_POINTER

PREVIEW_BANNER = """
########################################################################
##  NON-GATE PREVIEW — UNVERIFIED FEE RATES — NOT A GATE NUMBER      ##
##  Computed with the RECORDED, UNRATIFIED rates from                ##
##  config/kalshi_facts.yaml purely so R can see the SHAPE of the    ##
##  table. NO go/no-go decision may cite these numbers.              ##
########################################################################
"""


# ══════════════════════════════════════════════ signal episodes (tested)

def bracket_signals(df, min_edge_c=2.0, max_age_s=3600.0):
    """Signal episodes for ONE bracket (all L1 rows of one event).

    df columns: ts_utc (us), market_ticker, yes_ask_e4. Legs = distinct
    market_tickers present in df. Returns a list of episode dicts:
      t_open_us, t_close_us, lifespan_s, edge_open_c, edge_max_c,
      n_legs, legs_ask_c_at_open, censored
    Semantics pinned by tests/test_gate_metrics.py (see module docstring).
    """
    import pandas as pd
    legs = df["market_ticker"].unique()
    n_legs = len(legs)
    ts_arr = df["ts_utc"].values
    order = ts_arr.argsort(kind="stable")
    ts_arr = ts_arr[order]
    mt_arr = df["market_ticker"].values[order]
    # float64 coercion: None / pd.NA / masked-int NULLs all become NaN,
    # which the validity check below rejects uniformly (a == a is False).
    ask_arr = pd.to_numeric(df["yes_ask_e4"], errors="coerce") \
                .astype("float64").values[order]

    max_age_us = max_age_s * 1e6
    last_ask = {}   # mt -> ask_e4 (valid only)
    last_ts = {}    # mt -> ts_us of last row (any validity)
    episodes = []
    open_ep = None

    def close(ep, t_us, censored):
        ep["t_close_us"] = int(t_us)
        ep["lifespan_s"] = (t_us - ep["t_open_us"]) / 1e6
        ep["censored"] = censored
        episodes.append(ep)

    for i in range(len(ts_arr)):
        t = ts_arr[i]
        mt = mt_arr[i]
        a = ask_arr[i]
        valid = a is not None and a == a and 0 < a < 10000  # a==a: not NaN
        if valid:
            last_ask[mt] = float(a)
        else:
            last_ask.pop(mt, None)
        last_ts[mt] = t

        fully_quoted = (len(last_ask) == n_legs and
                        all(t - last_ts[m] <= max_age_us for m in last_ask))
        active = False
        edge_c = None
        if fully_quoted:
            edge_c = 100.0 - sum(last_ask.values()) / 100.0
            active = edge_c >= min_edge_c

        if active and open_ep is None:
            open_ep = {
                "t_open_us": int(t),
                "edge_open_c": edge_c,
                "edge_max_c": edge_c,
                "n_legs": n_legs,
                "legs_ask_c_at_open": {m: v / 100.0
                                       for m, v in last_ask.items()},
            }
        elif active and open_ep is not None:
            if edge_c > open_ep["edge_max_c"]:
                open_ep["edge_max_c"] = edge_c
        elif not active and open_ep is not None:
            close(open_ep, t, censored=False)
            open_ep = None

    if open_ep is not None:  # day/data ends with the signal still on
        close(open_ep, ts_arr[-1], censored=True)
    return episodes


def lifespan_stats(episodes):
    """Nearest-rank p50/p90 + max of episode lifespans (seconds)."""
    ls = sorted(e["lifespan_s"] for e in episodes)
    return {"n": len(ls),
            "p50_s": mr.pctl_nearest_rank(ls, 0.50),
            "p90_s": mr.pctl_nearest_rank(ls, 0.90),
            "max_s": ls[-1] if ls else None}


def signal_net_profit(episode, gate_mode=True, facts_path=None):
    """gross - sum of per-leg taker fees (1 contract/leg, at episode-open
    asks), in cents. gate_mode=True REFUSES while fees.verified != true —
    the refusal IS mm_research.trade_fee's (no second guard, no second
    fee formula)."""
    fees_d = sum(mr.trade_fee(ask_c / 100.0, 1, gate_mode=gate_mode,
                              facts_path=facts_path)
                 for ask_c in episode["legs_ask_c_at_open"].values())
    gross_c = episode["edge_open_c"]
    return {"gross_c": gross_c, "fees_c": fees_d * 100.0,
            "net_c": gross_c - fees_d * 100.0}


# ══════════════════════════════════════════════════ bracket universe scan

def load_dims():
    """(me_map, bracket_events, notes): mutually_exclusive by event_ticker,
    events dim-labeled event_structure='bracket' (preferred source, empty
    today — reported, not assumed), and human-readable findings."""
    import duckdb
    import warehouse_common as wc
    dim = os.path.join(wc.load_config()["warehouse_root"], "dim", "latest")
    con = duckdb.connect()
    notes = []
    me_map = {}
    ev_csv = os.path.join(dim, "events.csv")
    if os.path.exists(ev_csv):
        rows = con.execute(
            "SELECT event_ticker, mutually_exclusive FROM read_csv('%s', "
            "header=true)" % ev_csv.replace("'", "''")).fetchall()
        me_map = {r[0]: bool(r[1]) for r in rows}
        notes.append("events dim: %d rows (ME flags)" % len(me_map))
    else:
        notes.append("events dim MISSING — every event has unknown "
                     "exclusivity and will be excluded (fail-closed)")
    bracket_events = set()
    mk_csv = os.path.join(dim, "markets.csv")
    if os.path.exists(mk_csv):
        try:
            rows = con.execute(
                "SELECT DISTINCT event_ticker FROM read_csv('%s', header=true, "
                "types={'event_ticker':'VARCHAR','event_structure':'VARCHAR'}) "
                "WHERE event_structure = 'bracket'"
                % mk_csv.replace("'", "''")).fetchall()
            bracket_events = {r[0] for r in rows}
        except Exception as e:  # dim readable-but-odd: report, fall back
            notes.append("markets dim unreadable (%s) — fallback grouping" % e)
        notes.append("markets dim event_structure='bracket': %d events%s"
                     % (len(bracket_events),
                        "" if bracket_events else
                        " (catalog lacks floor/cap_strike -> derivation "
                        "degenerate; dim is today-only + row-capped) — "
                        "FALLBACK event_ticker grouping + ME heuristic used"))
    else:
        notes.append("markets dim MISSING — fallback event_ticker grouping")
    return me_map, bracket_events, notes


def scan_day(day, me_map, bracket_events, min_edge_c, max_age_s, min_legs):
    """One day's bracket scan through warehouse load() (read-only).
    Returns {day, partial, universe accounting, episodes}."""
    from warehouse import load
    df = load("orderbooks_l1", start=day, end=day,
              columns=["ts_utc", "market_ticker", "event_ticker",
                       "yes_ask_e4"]).df()
    n_rows = len(df)

    # Q7 defense-in-depth: drop MVE/combo legs before any grouping
    mve = (df["event_ticker"].fillna("").str.startswith("KXMVE") |
           df["market_ticker"].fillna("").str.startswith("KXMVE"))
    df = df[~mve]

    acct = {"l1_rows": n_rows, "mve_rows_dropped": int(mve.sum()),
            "events_seen": 0, "excl_single_leg": 0, "excl_me_unknown": 0,
            "excl_not_me": 0, "evaluated": 0, "via_dim_bracket": 0}
    episodes = []
    for ev, g in df.groupby("event_ticker", sort=False):
        acct["events_seen"] += 1
        if g["market_ticker"].nunique() < min_legs:
            acct["excl_single_leg"] += 1
            continue
        if ev in bracket_events:            # preferred: dim-labeled bracket
            acct["via_dim_bracket"] += 1
        elif ev not in me_map:              # unknown exclusivity: fail-closed
            acct["excl_me_unknown"] += 1
            continue
        elif not me_map[ev]:                # known NOT mutually exclusive
            acct["excl_not_me"] += 1
            continue
        acct["evaluated"] += 1
        for e in bracket_signals(g, min_edge_c=min_edge_c,
                                 max_age_s=max_age_s):
            e["event_ticker"] = ev
            e["day"] = day
            episodes.append(e)
    return {"day": day, "acct": acct, "episodes": episodes}


def available_days(end_day):
    """Clean days with data: archived date=* partitions (FINAL) plus any
    staging-only days up to end_day (labeled PARTIAL)."""
    import warehouse_common as wc
    cfg = wc.load_config()
    archived = sorted({m.split("date=")[1].split(os.sep)[0]
                       for m in _glob.glob(os.path.join(
                           cfg["archive_root"], "orderbooks_l1",
                           "*", "*", "date=*"))})
    days, d = [], datetime.date.fromisoformat(FIRST_CLEAN_DAY)
    stop = datetime.date.fromisoformat(end_day)
    while d <= stop:
        days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return days, set(archived)


# ═══════════════════════════════════════════════════════════ report (md)

def _fmt(v, nd=1):
    if v is None:
        return "–"
    return ("%%.%df" % nd) % v if isinstance(v, float) else str(v)


def build_report(results, archived, fees, args, preview):
    today = datetime.datetime.now(datetime.timezone.utc)
    all_eps = [e for r in results for e in r["episodes"]]
    total_days = len(results) or 1
    L = []
    L.append("# WP-09 Gate Report — buy-all-legs bracket arb")
    L.append("")
    L.append("Generated %s by tools/gate_calc.py (tests: "
             "tests/test_gate_metrics.py). Input document for the H-4 gate "
             "meeting: numbers are compared against the pre-committed "
             "thresholds, not renegotiated." % today.strftime("%Y-%m-%d %H:%M UTC"))
    L.append("")
    L.append("## THE THREE GATE NUMBERS")
    L.append("")
    st_all = lifespan_stats(all_eps)
    day_counts = {r["day"]: len(r["episodes"]) for r in results}
    L.append("| # | metric | value |")
    L.append("|---|--------|-------|")
    L.append("| 1 | **signal_count / day** | **%.1f** (total %d over %d "
             "day(s): %s) |" % (len(all_eps) / total_days, len(all_eps),
                                total_days,
                                ", ".join("%s=%d" % kv
                                          for kv in day_counts.items())))
    L.append("| 2 | **lifespan p50** | **%s s** (p90 %s s, max %s s, "
             "n=%d) |" % (_fmt(st_all["p50_s"]), _fmt(st_all["p90_s"]),
                          _fmt(st_all["max_s"]), st_all["n"]))
    if fees.get("verified") is True:
        nets = sorted(signal_net_profit(e)["net_c"] for e in all_eps)
        L.append("| 3 | **net_profit (median/signal)** | **%s c** |"
                 % _fmt(mr.pctl_nearest_rank(nets, 0.5), 2))
    else:
        L.append("| 3 | **net_profit** | **REFUSED** — fees.verified=false; "
                 "see refusal below |")
    L.append("")
    L.append("Parameters: min gross edge %.1fc (signal iff 100 - sum(asks) "
             ">= %.1fc; the 99-101c band is silent by construction), LOCF "
             "max quote age %.0fs (hourly-heartbeat reconstruction bound), "
             "min legs %d, 1 contract per leg at L1 ask." %
             (args.min_edge_c, args.min_edge_c, args.max_quote_age_s,
              args.min_legs))
    L.append("")

    L.append("## 1. Signals per day")
    L.append("")
    L.append("| day | status | L1 rows | events seen | evaluated brackets | "
             "excluded (1-leg / ME-unknown / not-ME) | MVE rows dropped | "
             "**signals** |")
    L.append("|-----|--------|---------|-------------|--------------------|"
             "-----------------------------------------|------------------|"
             "-------------|")
    for r in results:
        a = r["acct"]
        L.append("| %s | %s | %s | %s | %s | %s / %s / %s | %s | **%d** |"
                 % (r["day"],
                    "FINAL (archived)" if r["day"] in archived else "PARTIAL (staging)",
                    "{:,}".format(a["l1_rows"]), "{:,}".format(a["events_seen"]),
                    a["evaluated"], a["excl_single_leg"], a["excl_me_unknown"],
                    a["excl_not_me"], "{:,}".format(a["mve_rows_dropped"]),
                    len(r["episodes"])))
    L.append("")

    L.append("## 2. Lifespan distribution (seconds)")
    L.append("")
    L.append("| day | n | p50 | p90 | max | censored (open at data end) |")
    L.append("|-----|---|-----|-----|-----|------------------------------|")
    for r in results:
        st = lifespan_stats(r["episodes"])
        cens = sum(1 for e in r["episodes"] if e["censored"])
        L.append("| %s | %d | %s | %s | %s | %d |"
                 % (r["day"], st["n"], _fmt(st["p50_s"]), _fmt(st["p90_s"]),
                    _fmt(st["max_s"]), cens))
    st = st_all
    L.append("| **all** | %d | %s | %s | %s | %d |"
             % (st["n"], _fmt(st["p50_s"]), _fmt(st["p90_s"]),
                _fmt(st["max_s"]), sum(1 for e in all_eps if e["censored"])))
    L.append("")
    if all_eps:
        ge = sorted(e["edge_open_c"] for e in all_eps)
        L.append("Gross edge at open (fee-free, diagnostic): p50 %sc, "
                 "p90 %sc, max %sc. Legs per signaling bracket: %s."
                 % (_fmt(mr.pctl_nearest_rank(ge, .5), 2),
                    _fmt(mr.pctl_nearest_rank(ge, .9), 2), _fmt(ge[-1], 2),
                    ", ".join(str(s) for s in sorted(
                        {e["n_legs"] for e in all_eps}))))
        L.append("")

    L.append("## 3. Theoretical net profit")
    L.append("")
    if fees.get("verified") is True:
        L.append("| day | signals | net p50 c | net p90 c | net max c | "
                 "n(net>0) | sum positive net c |")
        L.append("|-----|---------|-----------|-----------|-----------|"
                 "----------|--------------------|")
        for r in results:
            nets = sorted(signal_net_profit(e)["net_c"]
                          for e in r["episodes"])
            pos = [n for n in nets if n > 0]
            L.append("| %s | %d | %s | %s | %s | %d | %s |"
                     % (r["day"], len(nets),
                        _fmt(mr.pctl_nearest_rank(nets, .5), 2),
                        _fmt(mr.pctl_nearest_rank(nets, .9), 2),
                        _fmt(nets[-1] if nets else None, 2), len(pos),
                        _fmt(sum(pos), 2)))
    else:
        L.append("```")
        L.append(REFUSAL_BANNER.strip())
        L.append("```")
        L.append("")
        L.append("Mechanically enforced: gate_calc calls "
                 "mm_research.trade_fee(gate_mode=True), which reads "
                 "fees.verified from config/kalshi_facts.yaml at call time "
                 "and raises FeeNotVerifiedError (tested both directions in "
                 "tests/test_gate_metrics.py). Unblock: %s." % OQ1_POINTER)
        if preview is not None:
            L.append("")
            L.append("```")
            L.append(PREVIEW_BANNER.strip())
            L.append("```")
            L.append("")
            L.append("| day | signals | net p50 c | net p90 c | net max c | "
                     "n(net>0) | sum positive net c |")
            L.append("|-----|---------|-----------|-----------|-----------|"
                     "----------|--------------------|")
            for row in preview:
                L.append("| %s | %d | %s | %s | %s | %d | %s |" % row)
            L.append("")
            L.append("(Preview rates: taker %s — DOCS-ONLY, unratified. "
                     "Every number above is NON-GATE.)"
                     % fees["params"].get("taker_rate"))
    L.append("")

    L.append("## 4. Context — WP-06 wiggle / interval research (cited, "
             "not recomputed)")
    L.append("")
    pages = sorted(_glob.glob(os.path.join(ROOT, "work", "mm",
                                           "research_*.html")))
    if pages:
        for p in pages:
            L.append("- %s (+ metrics CSV alongside)"
                     % os.path.relpath(p, ROOT))
    else:
        L.append("- NO WP-06 research page found under work/mm/ — run "
                 "tools/mm_research.py")
    L.append("")

    L.append("## 5. Method + caveats (binding on interpretation)")
    L.append("")
    L.append("- Bracket universe: dims event_structure='bracket' preferred; "
             "TODAY it is empty (catalog parquets lack floor/cap_strike; "
             "dim snapshot is today-only and row-capped), so the documented "
             "fallback applies: same-event grouping (event_ticker) + "
             "mutually_exclusive=true from dim/latest/events.csv; "
             "ME-unknown events EXCLUDED fail-closed; KXMVE dropped (Q7).")
    L.append("- Mutually exclusive guarantees AT MOST one leg pays; "
             "exhaustiveness (at least one pays) is NOT verifiable from our "
             "data. Signal counts are an UPPER BOUND on true riskless arbs.")
    L.append("- Leg universe = markets observed in OUR capture per "
             "event-day; an uncaptured leg would overstate edges. "
             "Full-quote guard: no evaluation until every observed leg has "
             "a valid ask (0 < ask < 100c) within the LOCF window.")
    L.append("- L1 top-of-book, 1 contract, no depth/queue/fill model — "
             "gross numbers are optimistic/diagnostic (Q2); the fee-net "
             "table is the gate input and refuses until fees are ratified.")
    L.append("- PARTIAL days come from live staging (day incomplete/"
             "unarchived); FINAL days from write-once archives. Episodes "
             "open at data end are censored and counted as such.")
    L.append("")
    return "\n".join(L)


# ═══════════════════════════════════════════════════════════════════ main

def main(argv):
    import warehouse_common as wc
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=FIRST_CLEAN_DAY)
    ap.add_argument("--end", default=today)
    ap.add_argument("--min-edge-c", dest="min_edge_c", type=float, default=2.0)
    ap.add_argument("--max-quote-age-s", dest="max_quote_age_s", type=float,
                    default=3600.0)
    ap.add_argument("--min-legs", dest="min_legs", type=int, default=2)
    ap.add_argument("--out", default=None)
    ap.add_argument("--preview-unverified-fees", action="store_true",
                    help="print the net-profit table with UNRATIFIED rates "
                         "under a NON-GATE PREVIEW banner (shape only)")
    args = ap.parse_args(argv[1:])

    fees = mr.load_fee_facts()
    print("fees.verified = %s -> gate-mode net profit %s"
          % (fees.get("verified"),
             "ENABLED" if fees.get("verified") is True else "WILL REFUSE"))

    me_map, bracket_events, dim_notes = load_dims()
    for n in dim_notes:
        print("dims: %s" % n)

    days, archived = available_days(args.end)
    days = [d for d in days if d >= args.start]
    results = []
    for d in days:
        try:
            r = scan_day(d, me_map, bracket_events, args.min_edge_c,
                         args.max_quote_age_s, args.min_legs)
        except FileNotFoundError:
            print("%s: no data — skipped" % d)
            continue
        if r["acct"]["l1_rows"] == 0:
            print("%s: 0 L1 rows — skipped" % d)
            continue
        tag = "FINAL" if d in archived else "PARTIAL"
        a = r["acct"]
        print("%s [%s]: %s L1 rows, %d events, %d brackets evaluated "
              "(excl: %d single-leg, %d ME-unknown, %d not-ME), "
              "MVE rows dropped %d -> %d signal(s)"
              % (d, tag, "{:,}".format(a["l1_rows"]), a["events_seen"],
                 a["evaluated"], a["excl_single_leg"], a["excl_me_unknown"],
                 a["excl_not_me"], a["mve_rows_dropped"], len(r["episodes"])))
        results.append(r)

    # gate-mode net profit: attempt -> loud refusal while unverified
    preview = None
    if fees.get("verified") is not True:
        print(REFUSAL_BANNER)
        if args.preview_unverified_fees:
            print(PREVIEW_BANNER)
            preview = []
            for r in results:
                nets = sorted(signal_net_profit(e, gate_mode=False)["net_c"]
                              for e in r["episodes"])
                pos = [n for n in nets if n > 0]
                row = (r["day"], len(nets),
                       _fmt(mr.pctl_nearest_rank(nets, .5), 2),
                       _fmt(mr.pctl_nearest_rank(nets, .9), 2),
                       _fmt(nets[-1] if nets else None, 2), len(pos),
                       _fmt(sum(pos), 2))
                preview.append(row)
                print("  %s: %s signals, net p50 %sc p90 %sc max %sc, "
                      "%s net>0, sum positive %sc  [NON-GATE]" % row)

    report = build_report(results, archived, fees, args, preview)
    out = args.out or os.path.join(wc.ROOT, "work", "mm",
                                   "gate_report_%s.md" % today)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(report)
    print("\nreport: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
