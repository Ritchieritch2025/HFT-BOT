#!/usr/bin/env python3
"""Aggregate stage of the maker-edge pilot (PLAN_RESEARCH_CYCLE_1 S1).

Consumes markout.parquet + dim_segments.parquet; produces the per-layer
primary metric (with per-match block-bootstrap CI), exploratory buckets, the
DQ report, and the interactive HTML (7 charts, vendored ECharts). Called by
maker_edge_pilot.py; kept separate for length. See PRE_REGISTRATION.md.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import warehouse_common as wc  # noqa: E402

OUTDIR = os.path.join(wc.ROOT, "work", "research", "maker_edge_pilot")
SEGMENTS = os.path.join(wc.ROOT, "work", "research", "dim_segments.parquet")
MARKOUT = os.path.join(OUTDIR, "markout.parquet")
ECHARTS = os.path.join(wc.ROOT, "docs", "vendor", "js", "echarts.min.js")
HORIZONS = (1, 10, 30, 120)
PRIMARY_H = 30
MIN_N = 200
NBOOT = 1000
MAKER_RATE = 0.0175          # kalshi_facts params.maker_rate (NON-GATE preview)
PRICE_BANDS = [(1, 10), (10, 30), (30, 50), (50, 70), (70, 90), (90, 99)]

# a DETERMINISTIC LCG so the bootstrap is reproducible without Math.random /
# Date (the harness forbids them); seeded from a fixed constant.
class _RNG:
    def __init__(self, seed=0x9E3779B1):
        self.s = seed & 0xFFFFFFFF
    def randint(self, n):
        self.s = (1103515245 * self.s + 12345) & 0x7FFFFFFF
        return self.s % n


def _ceil_centicent(x):
    return math.ceil(round(x * 10000.0, 6)) / 10000.0


def maker_fee_preview_cents(p_cents):
    """NON-GATE research preview of the maker fee, ¢/contract, via the
    documented formula ceil_to_centicent(maker_rate·P·(1−P)). P in dollars."""
    P = p_cents / 100.0
    return _ceil_centicent(MAKER_RATE * P * (1.0 - P)) * 100.0


def _price_band(p):
    for lo, hi in PRICE_BANDS:
        if lo <= p < hi:
            return "%d-%dc" % (lo, hi)
    return "90-99c" if p >= 90 else "1-10c"


def run(con, args, fp):
    m = MARKOUT.replace("'", "''")
    s = SEGMENTS.replace("'", "''")
    train_end_us = wc.day_start_us(args.train_end) + 86_400_000_000  # exclusive

    # ── phase detector (causal, train-frozen). onset = first trade ts where the
    # trailing-120s trade count in that market reaches τ; τ = median of per-market
    # peak trailing-120s counts on TRAIN. pre-match = ts < onset (or no onset).
    con.execute("""
      CREATE TEMP TABLE tw AS
      SELECT market_ticker, ts_utc,
             count(*) OVER (PARTITION BY market_ticker ORDER BY ts_utc
                            RANGE BETWEEN 120000000 PRECEDING AND CURRENT ROW) AS c120
      FROM read_parquet('%s') WHERE dq_class='ok'""" % m)
    tau = con.execute(
        "SELECT median(peak) FROM (SELECT market_ticker, max(c120) peak FROM tw "
        "WHERE ts_utc < %d GROUP BY 1)" % train_end_us).fetchone()[0] or 1e18
    tau = max(2.0, float(tau))
    con.execute("""
      CREATE TEMP TABLE onset AS
      SELECT market_ticker, min(ts_utc) AS onset_ts
      FROM tw WHERE c120 >= %f GROUP BY 1""" % tau)

    # ── analysis table: join segments + phase, keep the pilot main scope
    # (1c tick, win_loss, known fee class), label split + phase + price band.
    drift_cols = ", ".join("k.drift_%d, k.markout_%d" % (h, h) for h in HORIZONS)
    con.execute("""
      CREATE TEMP TABLE a AS
      SELECT k.market_ticker, k.event_id, k.ts_utc, k.sign, k.count_e4,
             k.p_fill, k.mid_t, k.half_spread_c, k.spread_e4, k.fill_class,
             k.bounce, %s,
             g.tour_level, g.maker_fee_class, g.tick_stratum, g.market_kind,
             CASE WHEN k.ts_utc < %d THEN 'train' ELSE 'val' END AS split,
             CASE WHEN o.onset_ts IS NULL OR k.ts_utc < o.onset_ts
                  THEN 'pre_match' ELSE 'in_play' END AS phase
      FROM read_parquet('%s') k
      JOIN read_parquet('%s') g ON g.market_ticker = k.market_ticker
      LEFT JOIN onset o ON o.market_ticker = k.market_ticker
      WHERE k.dq_class='ok' AND g.tick_stratum='1c' AND g.market_kind='win_loss'
        AND g.maker_fee_class IN ('zero','charged')
    """ % (drift_cols, train_end_us, m, s))

    result = {"pre_registration_file": "PRE_REGISTRATION.md", "fingerprint": fp,
              "regime": "slam-week", "grade": "dev-grade",
              "non_gate_banner": "FEES NON-GATE — verified=false research preview "
              "(maker_rate=%.4f); no facts-gated tool touched (OQ-1 unratified)"
              % MAKER_RATE, "primary_horizon_s": PRIMARY_H, "min_n": MIN_N,
              "conclusions_allowed": ["methodology-valid+collect", "methodology-flawed"],
              "phase_tau_train": tau, "layers": [], "dq": {}, "exploratory": {}}

    # ── DQ report (all drops counted) + mid-staleness distribution
    dq_rows = con.execute("SELECT dq_class, count(*) FROM read_parquet('%s') "
                          "GROUP BY 1 ORDER BY 2 DESC" % m).fetchall()
    result["dq"]["classes"] = {c: n for c, n in dq_rows}
    stale = con.execute(
        "SELECT quantile_cont((ts_utc-book_ts)/1e6, [0.5,0.9,0.99,1.0]) "
        "FROM read_parquet('%s') WHERE dq_class='ok'" % m).fetchone()[0]
    result["dq"]["mid_staleness_s"] = {"p50": stale[0], "p90": stale[1],
                                       "p99": stale[2], "max": stale[3]}
    result["dq"]["fill_class"] = {c: n for c, n in con.execute(
        "SELECT fill_class, count(*) FROM a GROUP BY 1").fetchall()}

    # ── PRIMARY METRIC per layer (fee_class × tour_level), pre-match, val,
    # pessimistic. Per-fill net = half_spread − drift_30 − maker_fee.
    # Aggregate to per-MATCH first (block bootstrap resamples matches).
    base = ("FROM a WHERE phase='pre_match' AND split='val' "
            "AND fill_class='pessimistic'")
    layers = con.execute(
        "SELECT maker_fee_class, tour_level, count(*) n %s "
        "GROUP BY 1,2 ORDER BY 3 DESC" % base).fetchall()

    for fee_class, tour, n in layers:
        lf = ("%s AND maker_fee_class='%s' AND tour_level='%s'"
              % (base, fee_class, tour))
        # per-match aggregates: sum(vol), sum(vol*half_spread), sum(vol*drift30)
        fee_expr = ("0.0" if fee_class == "zero" else
                    "0.0")  # maker fee added in python per-fill (formula preview)
        rows = con.execute("""
          SELECT event_id,
                 sum(count_e4/10000.0) AS vol,
                 sum((count_e4/10000.0)*half_spread_c) AS vhs,
                 sum((count_e4/10000.0)*drift_%d) AS vdr,
                 sum((count_e4/10000.0)*p_fill) AS vpx
          %s GROUP BY 1""" % (PRIMARY_H, lf)).fetchall()
        if not rows:
            continue
        # per-match tuples: (vol, v*halfspread, v*drift, v*price)
        M = [(r[1], r[2], r[3], r[4]) for r in rows]
        # maker fee per match (vwap of the preview over that match's fills)
        # approximated at the match's volume-weighted avg price (charged only)
        def net_of(sample):
            V = sum(x[0] for x in sample)
            if V <= 0:
                return None
            hs = sum(x[1] for x in sample) / V
            dr = sum(x[2] for x in sample) / V
            avgp = sum(x[3] for x in sample) / V
            fee = maker_fee_preview_cents(avgp) if fee_class == "charged" else 0.0
            return hs - dr - fee
        point = net_of(M)
        rng = _RNG(0x51ED ^ (hash((fee_class, tour)) & 0xFFFF))
        boots = []
        nb = len(M)
        for _ in range(NBOOT):
            samp = [M[rng.randint(nb)] for _ in range(nb)]
            v = net_of(samp)
            if v is not None:
                boots.append(v)
        boots.sort()
        ci = (boots[int(0.025 * len(boots))], boots[int(0.975 * len(boots))]) \
            if boots else (None, None)
        # components (vwap) for the waterfall
        V = sum(x[0] for x in M)
        hs = sum(x[1] for x in M) / V
        dr = sum(x[2] for x in M) / V
        avgp = sum(x[3] for x in M) / V
        fee = maker_fee_preview_cents(avgp) if fee_class == "charged" else 0.0
        conclusion = "collect_more_data (n<%d)" % MIN_N if n < MIN_N else \
            "methodology-valid+collect"
        result["layers"].append({
            "maker_fee_class": fee_class, "tour_level": tour, "n_pessimistic": n,
            "n_matches": nb, "half_spread_c": hs, "drift_30_c": dr,
            "maker_fee_c": fee, "avg_price_c": avgp,
            "net_edge_c": point, "ci95": [ci[0], ci[1]],
            "conclusion": conclusion,
            "h1_fee_wall_applicable": fee_class == "zero"})

    # ── exploratory (hypotheses only): markout curve by price band, decomposition
    result["exploratory"]["markout_by_horizon_priceband"] = _markout_curve(con, base)
    result["exploratory"]["decomposition"] = _decomposition(con, base)
    result["exploratory"]["spread_hist"] = _spread_hist(con)
    result["exploratory"]["time_of_day"] = _time_of_day(con)
    result["exploratory"]["per_match_scatter"] = _per_match_scatter(con, base)
    result["exploratory"]["burstiness"] = _burstiness(con)

    with open(os.path.join(OUTDIR, "results.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    _write_html(result)
    _print_summary(result)
    return 0


def _markout_curve(con, base):
    out = {}
    for lo, hi in PRICE_BANDS:
        band = "%d-%dc" % (lo, hi)
        pts = []
        for h in HORIZONS:
            r = con.execute(
                "SELECT count(*) n, avg(markout_%d) m, "
                "quantile_cont(markout_%d,[0.25,0.5,0.75]) q "
                "%s AND phase='pre_match' AND fill_class='pessimistic' "
                "AND maker_fee_class='zero' AND mid_t>=%d AND mid_t<%d"
                % (h, h, base, lo, hi)).fetchone()
            if r[0]:
                pts.append({"h": h, "n": r[0], "mean": r[1],
                            "q25": r[2][0], "q50": r[2][1], "q75": r[2][2]})
        if pts:
            out[band] = pts
    return out


def _decomposition(con, base):
    out = []
    for h in HORIZONS:
        r = con.execute(
            "SELECT count(*) n, avg(bounce) b, avg(drift_%d) d, avg(markout_%d) m "
            "%s AND phase='pre_match' AND fill_class='pessimistic' "
            "AND maker_fee_class='zero'" % (h, h, base)).fetchone()
        if r[0]:
            out.append({"h": h, "n": r[0], "bounce": r[1], "drift": r[2],
                        "markout": r[3],
                        "identity_gap": abs((r[1] + r[2]) - r[3])})
    return out


def _spread_hist(con):
    out = {}
    for tour in ("ITF", "Challenger", "ATP", "WTA"):
        rows = con.execute(
            "SELECT round(spread_e4/100.0) sc, count(*) n FROM a "
            "WHERE phase='pre_match' AND tour_level='%s' GROUP BY 1 ORDER BY 1"
            % tour).fetchall()
        if rows:
            out[tour] = [{"spread_c": r[0], "n": r[1]} for r in rows]
    return out


def _time_of_day(con):
    rows = con.execute(
        "SELECT dayofweek(to_timestamp(ts_utc/1e6)) dow, "
        "hour(to_timestamp(ts_utc/1e6)) hr, count(*) n, "
        "sum(count_e4/10000.0) vol, avg(spread_e4/100.0) spr "
        "FROM a WHERE phase='pre_match' GROUP BY 1,2").fetchall()
    return [{"dow": int(r[0]), "hr": int(r[1]), "n": r[2], "vol": r[3],
             "spread_c": r[4]} for r in rows]


def _per_match_scatter(con, base):
    rows = con.execute("""
      SELECT event_id, tour_level, maker_fee_class,
             sum((count_e4/10000.0)*half_spread_c)/nullif(sum(count_e4/10000.0),0) hs,
             sum((count_e4/10000.0)*drift_30)/nullif(sum(count_e4/10000.0),0) dr,
             count(*) n
      %s AND phase='pre_match' AND fill_class='pessimistic'
      GROUP BY 1,2,3 HAVING count(*) >= 5""" % base).fetchall()
    return [{"event": r[0], "tour": r[1], "fee": r[2], "half_spread": r[3],
             "drift": r[4], "n": r[5]} for r in rows]


def _burstiness(con):
    """Tennis-only book-level inter-update interval, pre-match vs in-play."""
    out = {}
    for phase in ("pre_match", "in_play"):
        r = con.execute(
            "SELECT count(*) n, quantile_cont(dt_s,[0.5,0.9,0.99]) q FROM ("
            "SELECT (ts_utc - lag(ts_utc) OVER (PARTITION BY market_ticker "
            "ORDER BY ts_utc))/1e6 AS dt_s FROM a WHERE phase='%s') "
            "WHERE dt_s IS NOT NULL AND dt_s >= 0" % phase).fetchone()
        if r[0]:
            out[phase] = {"n": r[0], "p50_s": r[1][0], "p90_s": r[1][1],
                          "p99_s": r[1][2]}
    return out


def _print_summary(r):
    print("\n=== MAKER-EDGE PILOT (dev-grade, regime=slam-week) ===")
    print(r["non_gate_banner"])
    print("phase tau (train):", round(r["phase_tau_train"], 1))
    print("DQ:", r["dq"]["classes"], "| fill:", r["dq"]["fill_class"])
    print("\nPRIMARY METRIC per layer (pre-match, val, pessimistic, 30s):")
    print("%-8s %-11s %8s %7s %9s %9s %8s %10s %s" %
          ("fee", "tour", "n_pess", "match", "half_sp", "drift30", "fee_c",
           "net_edge", "ci95"))
    for L in r["layers"]:
        ci = L["ci95"]
        print("%-8s %-11s %8d %7d %9.3f %9.3f %8.3f %10.3f [%.3f,%.3f] %s" %
              (L["maker_fee_class"], L["tour_level"], L["n_pessimistic"],
               L["n_matches"], L["half_spread_c"], L["drift_30_c"],
               L["maker_fee_c"], L["net_edge_c"], ci[0], ci[1], L["conclusion"]))


# ─────────────────────────────────────────────────────── HTML (ECharts)

def _write_html(r):
    import html_report_maker_edge as H
    H.write(r, OUTDIR, ECHARTS)
