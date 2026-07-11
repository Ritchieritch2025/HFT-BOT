#!/usr/bin/env python3
"""W-E1: event index builder (PLAN_EVENT_PACKAGING §3.2/§3.4).

Reads a catalog dir (markets.csv [+ observed.csv + lifecycle.csv]) and writes
work/event_packs/index.parquet — one row per packaging unit (event | market)
with an inferred [win_start, win_end] window. Windows come from OBSERVED
activity + lifecycle, never the scheduled close_time alone (which shifts with
OT/delays). READ-ONLY derived layer; touches nothing under capture/ingest/
export and does NOT extend warehouse.py (W-E3 owns that).

Catalog dir CSVs (all UTC 'YYYY-MM-DD HH:MM:SS' timestamps):
  markets.csv    ticker,event_ticker,series_ticker,category,subcategory,group,
                 open_time,close_time,status[,mve_collection_ticker]
  observed.csv   ticker,ts_utc            (optional; per-tick activity)
  lifecycle.csv  event_ticker,event_type,ts_utc  (optional; Fork A determined/settled)

  python3 tools/event_index.py --catalog-dir tests/fixtures/event_index_catalog
"""
import argparse
import calendar as _cal
import csv
import datetime as _dt
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

US_PER_DAY = 86_400_000_000
US_PER_HOUR = 3_600_000_000
TWO_HOURS_US = 2 * US_PER_HOUR

# §3.4 index schema — column order is the parquet contract.
COLS = ["unit", "unit_key", "category", "subcategory", "group", "event_ticker",
        "markets", "series_ticker", "sched_start_us", "sched_close_us",
        "t_first_seen_us", "t_last_seen_us", "t_determined_us", "t_settled_us",
        "win_start_us", "win_end_us", "window_source", "crossed_day_boundary",
        "status", "catalog_incomplete", "window_divergence",
        "pack_path", "pack_built_at_us", "pack_row_l1", "pack_row_trades"]


def day_index(ts_us):
    return int(ts_us) // US_PER_DAY


def parse_dt_us(s):
    """UTC 'YYYY-MM-DD HH:MM:SS' (or ISO / date-only) -> µs; blank -> None."""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.lower() in ("none", "nan", "null"):
        return None
    s2 = s.replace("T", " ").replace("Z", "").strip()
    for fmt, ln in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            dt = _dt.datetime.strptime(s2[:ln], fmt)
            return int(_cal.timegm(dt.timetuple()) * 1_000_000)
        except ValueError:
            continue
    return None


def load_policy(path):
    """Merge event_packaging.yaml defaults with per-category overrides."""
    import yaml
    with open(path) as f:
        cfg = yaml.safe_load(f)
    defaults = cfg.get("defaults", {})
    cats = cfg.get("categories", {}) or {}

    def for_category(category):
        pol = dict(defaults)
        pol.update(cats.get(category, {}))
        return pol
    return for_category


def _first(mkts, key):
    for m in mkts:
        if m.get(key):
            return m[key]
    return None


def _is_q7(mkts):
    """Q7 defense-in-depth: MVE/combo markets never pack (KXMVE prefix,
    mve_collection_ticker set, or Exotics category)."""
    for m in mkts:
        if m.get("mve"):
            return True
        if str(m.get("ticker", "")).startswith("KXMVE"):
            return True
        if str(m.get("event_ticker", "")).startswith("KXMVE"):
            return True
    return _first(mkts, "category") == "Exotics"


def _seal_state(policy_skip, is_q7, mkts, obs, sched_start, sched_close,
                win_start, win_end, now_us, observed_empty):
    if policy_skip or is_q7:
        return "excluded"
    n = obs.get("n_ticks", 0) or 0
    t_last = obs.get("t_last_us")
    seal_after = obs.get("_seal_after")  # seal_after_close_us, threaded via obs
    min_ticks = obs.get("_min_ticks", 1)
    statuses = [m.get("status") for m in mkts if m.get("status")]
    all_settled = bool(statuses) and all(s == "settled" for s in statuses)
    if observed_empty:
        # nothing captured: future/unknown catalog -> scheduled; past -> partial
        if sched_start is None or now_us < sched_start:
            return "scheduled"
        return "partial"
    if all_settled and n >= min_ticks:
        return "sealed"
    if win_start is not None and win_end is not None and win_start <= now_us <= win_end:
        return "active"
    if (t_last is not None and now_us - seal_after <= t_last <= now_us):
        return "active"
    if sched_close is not None and now_us > sched_close + seal_after and n >= min_ticks:
        return "sealed"
    return "partial"


def infer_index_row(unit, unit_key, mkts, obs, policy, now_us):
    """Steps A-G of §3.2. Returns one §3.4 index row (dict).

    mkts: list of per-market dicts {ticker, event_ticker, series_ticker,
          category, subcategory, group, open_us, close_us, status, mve}.
    obs:  {t_first_us, t_last_us, n_ticks, t_determined_us, t_settled_us}.
    policy: the category policy dict (pre_pad_us, post_pad_us,
            seal_after_close_us, min_ticks_to_seal, unit).
    """
    # Step B — scheduled bounds (reference; never seals alone)
    opens = [m["open_us"] for m in mkts if m.get("open_us") is not None]
    closes = [m["close_us"] for m in mkts if m.get("close_us") is not None]
    sched_start = min(opens) if opens else None
    sched_close = max(closes) if closes else None
    catalog_incomplete = any(m.get("open_us") is None or m.get("close_us") is None
                             for m in mkts)

    # Step C — lifecycle bounds (Fork A; None on Fork B)
    t_determined = obs.get("t_determined_us")
    t_settled = obs.get("t_settled_us")

    # Step D — observed bounds
    n = obs.get("n_ticks", 0) or 0
    t_first = obs.get("t_first_us")
    t_last = obs.get("t_last_us")
    observed_empty = n == 0 or t_first is None

    # Step E — merged window
    pre = policy["pre_pad_us"]
    post = policy["post_pad_us"]
    start_cands = [v for v in (sched_start, t_first) if v is not None]
    win_start = (min(start_cands) - pre) if start_cands else None
    end_cands = [("determined", t_determined), ("settled", t_settled),
                 ("last_seen", t_last), ("scheduled_close", sched_close)]
    end_present = [(nm, v) for nm, v in end_cands if v is not None]
    if end_present:
        nm, mx = max(end_present, key=lambda kv: kv[1])
        win_end = mx + post
        window_source = nm
        # purely observed (no catalog, no lifecycle) -> observed_merged
        if (sched_start is None and sched_close is None
                and t_determined is None and t_settled is None):
            window_source = "observed_merged"
    else:
        win_end, window_source = None, None

    # Step F — divergence (D2: never silent)
    window_divergence = False
    if t_last is not None and sched_close is not None and (t_last - sched_close) > TWO_HOURS_US:
        window_divergence = True
    if t_first is not None and sched_start is not None and (sched_start - t_first) > TWO_HOURS_US:
        window_divergence = True

    # crossed-day flag: observed edges if present, else the padded window
    if not observed_empty:
        crossed = day_index(t_first) != day_index(t_last)
    elif win_start is not None and win_end is not None:
        crossed = day_index(win_start) != day_index(win_end)
    else:
        crossed = False

    # Step G — seal state
    obs2 = dict(obs)
    obs2["_seal_after"] = policy["seal_after_close_us"]
    obs2["_min_ticks"] = policy.get("min_ticks_to_seal", 1)
    status = _seal_state(policy.get("unit") == "skip", _is_q7(mkts), mkts, obs2,
                         sched_start, sched_close, win_start, win_end, now_us,
                         observed_empty)

    return {
        "unit": unit, "unit_key": unit_key,
        "category": _first(mkts, "category"),
        "subcategory": _first(mkts, "subcategory"),
        "group": _first(mkts, "group"),
        "event_ticker": _first(mkts, "event_ticker"),
        "markets": sorted(m["ticker"] for m in mkts),
        "series_ticker": _first(mkts, "series_ticker"),
        "sched_start_us": sched_start, "sched_close_us": sched_close,
        "t_first_seen_us": t_first, "t_last_seen_us": t_last,
        "t_determined_us": t_determined, "t_settled_us": t_settled,
        "win_start_us": win_start, "win_end_us": win_end,
        "window_source": window_source,
        "crossed_day_boundary": crossed, "status": status,
        "catalog_incomplete": catalog_incomplete,
        "window_divergence": window_divergence,
        "pack_path": None, "pack_built_at_us": None,
        "pack_row_l1": None, "pack_row_trades": None,
    }


def _read_markets(path):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append({
                "ticker": r.get("ticker") or r.get("market_ticker"),
                "event_ticker": r.get("event_ticker"),
                "series_ticker": r.get("series_ticker"),
                "category": r.get("category"),
                "subcategory": r.get("subcategory"),
                "group": r.get("group"),
                "open_us": parse_dt_us(r.get("open_time")),
                "close_us": parse_dt_us(r.get("close_time")),
                "status": (r.get("status") or "").strip() or None,
                "mve": bool((r.get("mve_collection_ticker") or "").strip()),
            })
    return out


def _read_ticks(path):
    ticks = defaultdict(list)
    if not os.path.exists(path):
        return ticks
    with open(path) as f:
        for r in csv.DictReader(f):
            t = r.get("ts_utc")
            us = int(t) if (t and str(t).isdigit()) else parse_dt_us(t)
            if us is not None:
                ticks[r.get("ticker") or r.get("market_ticker")].append(us)
    return ticks


def _read_lifecycle(path):
    lc = defaultdict(dict)
    if not os.path.exists(path):
        return lc
    with open(path) as f:
        for r in csv.DictReader(f):
            t = r.get("ts_utc")
            us = int(t) if (t and str(t).isdigit()) else parse_dt_us(t)
            et = (r.get("event_type") or "").strip()
            ev = r.get("event_ticker")
            if us is None or et not in ("determined", "settled"):
                continue
            key = "t_%s_us" % et
            lc[ev][key] = max(lc[ev].get(key, 0), us)
    return lc


def _obs_for(tickers, event_ticker, ticks, lifecycle):
    allts = [t for tk in tickers for t in ticks.get(tk, [])]
    o = {"n_ticks": len(allts),
         "t_first_us": min(allts) if allts else None,
         "t_last_us": max(allts) if allts else None,
         "t_determined_us": None, "t_settled_us": None}
    o.update(lifecycle.get(event_ticker, {}))
    return o


def _dim_markets(dim_path):
    """Real catalog bounds per (event_ticker, ticker) from dim/latest/markets.csv:
    {(event, ticker): {open_us, close_us, status, mve}}."""
    import duckdb
    if not os.path.exists(dim_path):
        return {}
    rd = ("read_csv('%s', types={'ticker':'VARCHAR','event_ticker':'VARCHAR',"
          "'open_time':'VARCHAR','close_time':'VARCHAR','status':'VARCHAR',"
          "'mve_collection_ticker':'VARCHAR'})" % dim_path.replace("'", "''"))
    out = {}
    for tk, ev, ot, ct, st, mve in duckdb.sql(
            "SELECT ticker, event_ticker, open_time, close_time, status, "
            "mve_collection_ticker FROM %s" % rd).fetchall():
        out[(ev, tk)] = {"open_us": parse_dt_us(ot), "close_us": parse_dt_us(ct),
                         "status": (st or "").strip() or None,
                         "mve": bool((mve or "").strip())}
    return out


def _observed_by_market(warehouse, start, end, archive_only=None):
    """Observed activity per (event, market) aggregated in SQL over trades ∪ L1
    (never materializes ticks): {(event, market): {cat, sr, sub, grp, mn, mx, n}}."""
    import warehouse as wh
    out = {}
    for table in ("trades", "orderbooks_l1"):
        try:
            rel = wh.load(table, start=start, end=end, warehouse=warehouse,
                          archive_only=archive_only)
        except FileNotFoundError:
            continue
        # Exclude rows with a NULL identity (dirty data): a NULL event/market
        # would form a bogus unit and later crash the None-vs-str sort (audit
        # Defect-1). Fail-closed: drop + don't fabricate a unit.
        rel = rel.filter("event_ticker IS NOT NULL AND market_ticker IS NOT NULL")
        agg = rel.aggregate(
            "event_ticker, market_ticker, any_value(category) AS cat, "
            "any_value(series_ticker) AS sr, any_value(subcategory) AS sub, "
            'any_value("group") AS grp, min(ts_utc) AS mn, max(ts_utc) AS mx, '
            "count(*) AS n", "event_ticker, market_ticker")
        for ev, mk, cat, sr, sub, grp, mn, mx, n in agg.fetchall():
            k = (ev, mk)
            r = out.get(k)
            if r is None:
                out[k] = {"cat": cat, "sr": sr, "sub": sub, "grp": grp,
                          "mn": mn, "mx": mx, "n": n}
            else:
                r["mn"], r["mx"], r["n"] = min(r["mn"], mn), max(r["mx"], mx), r["n"] + n
    return out


def build_index_from_warehouse(warehouse, start, end, policy_for, now_us, dim_path,
                               archive_only=None):
    """Build the index from the REAL warehouse + dim (not a synthetic catalog dir).
    Units are the events/markets with OBSERVED activity in [start,end]; catalog
    bounds (open/close/status/mve) join from the dim by (event, ticker); category/
    series/group come from the observed rows. Lifecycle is Fork-B (None)."""
    from collections import defaultdict
    obs = _observed_by_market(warehouse, start, end, archive_only)
    dim = _dim_markets(dim_path)
    by_event = defaultdict(list)
    for (ev, mk) in obs:
        by_event[ev].append(mk)

    def mkt_dict(ev, mk):
        o = obs[(ev, mk)]
        d = dim.get((ev, mk), {})
        return {"ticker": mk, "event_ticker": ev, "series_ticker": o["sr"],
                "category": o["cat"], "subcategory": o["sub"], "group": o["grp"],
                "open_us": d.get("open_us"), "close_us": d.get("close_us"),
                "status": d.get("status"), "mve": d.get("mve", False)}

    rows = []
    for ev, mks in by_event.items():
        pol = policy_for(obs[(ev, mks[0])]["cat"])
        if pol.get("unit") == "market":
            for mk in mks:
                o = obs[(ev, mk)]
                oo = {"n_ticks": o["n"], "t_first_us": o["mn"], "t_last_us": o["mx"],
                      "t_determined_us": None, "t_settled_us": None}
                rows.append(infer_index_row("market", mk, [mkt_dict(ev, mk)], oo, pol, now_us))
        else:
            oo = {"n_ticks": sum(obs[(ev, mk)]["n"] for mk in mks),
                  "t_first_us": min(obs[(ev, mk)]["mn"] for mk in mks),
                  "t_last_us": max(obs[(ev, mk)]["mx"] for mk in mks),
                  "t_determined_us": None, "t_settled_us": None}
            rows.append(infer_index_row("event", ev, [mkt_dict(ev, mk) for mk in mks],
                                        oo, pol, now_us))
    rows.sort(key=lambda r: (r["unit"], r["unit_key"]))
    return rows


def build_index(catalog_dir, policy_for, now_us):
    markets = _read_markets(os.path.join(catalog_dir, "markets.csv"))
    ticks = _read_ticks(os.path.join(catalog_dir, "observed.csv"))
    lifecycle = _read_lifecycle(os.path.join(catalog_dir, "lifecycle.csv"))

    by_event = defaultdict(list)
    for m in markets:
        by_event[m["event_ticker"]].append(m)

    rows = []
    for ev, mkts in by_event.items():
        pol = policy_for(mkts[0].get("category"))
        if pol.get("unit") == "market":
            for m in mkts:
                obs = _obs_for([m["ticker"]], ev, ticks, lifecycle)
                rows.append(infer_index_row("market", m["ticker"], [m], obs, pol, now_us))
        else:  # event or skip both group by event_ticker
            obs = _obs_for([m["ticker"] for m in mkts], ev, ticks, lifecycle)
            rows.append(infer_index_row("event", ev, mkts, obs, pol, now_us))
    rows.sort(key=lambda r: (r["unit"], r["unit_key"]))
    return rows


def write_parquet(rows, out_path):
    import duckdb
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    con = duckdb.connect()
    con.execute("""CREATE TABLE idx (
        unit VARCHAR, unit_key VARCHAR, category VARCHAR, subcategory VARCHAR,
        "group" VARCHAR, event_ticker VARCHAR, markets VARCHAR[],
        series_ticker VARCHAR, sched_start_us BIGINT, sched_close_us BIGINT,
        t_first_seen_us BIGINT, t_last_seen_us BIGINT, t_determined_us BIGINT,
        t_settled_us BIGINT, win_start_us BIGINT, win_end_us BIGINT,
        window_source VARCHAR, crossed_day_boundary BOOLEAN, status VARCHAR,
        catalog_incomplete BOOLEAN, window_divergence BOOLEAN, pack_path VARCHAR,
        pack_built_at_us BIGINT, pack_row_l1 BIGINT, pack_row_trades BIGINT)""")
    if rows:  # executemany([]) raises; an empty build still emits an empty-schema parquet (Defect-2)
        con.executemany(
            "INSERT INTO idx VALUES (%s)" % ",".join("?" * len(COLS)),
            [[r[c] for c in COLS] for r in rows])
    con.execute("COPY idx TO '%s' (FORMAT parquet)" % out_path.replace("'", "''"))
    con.close()


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--catalog-dir",
                     help="synthetic dir with markets.csv [+ observed.csv, lifecycle.csv]")
    src.add_argument("--from-warehouse", action="store_true",
                     help="build from the REAL warehouse + dim/latest (production)")
    ap.add_argument("--warehouse", help="warehouse root (default: config)")
    ap.add_argument("--dim", default="work/warehouse/dim/latest/markets.csv")
    ap.add_argument("--start", help="observed-window start (ISO/date), for --from-warehouse")
    ap.add_argument("--end", help="observed-window end (ISO/date)")
    ap.add_argument("--days", type=int, default=7, help="lookback days if --start omitted")
    ap.add_argument("--policy", default="config/event_packaging.yaml")
    ap.add_argument("--out", default="work/event_packs/index.parquet")
    ap.add_argument("--now", help="UTC 'YYYY-MM-DD HH:MM:SS' for deterministic seal state")
    ap.add_argument("--print", dest="printn", type=int, default=0)
    args = ap.parse_args(argv[1:])

    now_us = parse_dt_us(args.now) if args.now else \
        int(_cal.timegm(_dt.datetime.utcnow().timetuple()) * 1_000_000)
    policy_for = load_policy(args.policy)
    if args.from_warehouse:
        start = args.start or (_dt.datetime.utcnow() - _dt.timedelta(days=args.days)).strftime("%Y-%m-%d")
        rows = build_index_from_warehouse(args.warehouse, start, args.end, policy_for,
                                          now_us, args.dim)
    else:
        rows = build_index(args.catalog_dir, policy_for, now_us)
    write_parquet(rows, args.out)

    from collections import Counter
    st = Counter(r["status"] for r in rows)
    xd = sum(1 for r in rows if r["crossed_day_boundary"])
    print("units=%d  crossed_midnight=%d  status=%s  -> %s" %
          (len(rows), xd, dict(st), args.out))
    for r in rows[:args.printn]:
        print("  %-6s %-28s %-9s src=%-15s cross=%s inc=%s div=%s" % (
            r["unit"], (r["unit_key"] or "")[:28], r["status"],
            r["window_source"], r["crossed_day_boundary"],
            r["catalog_incomplete"], r["window_divergence"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
