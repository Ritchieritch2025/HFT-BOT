#!/usr/bin/env python3
"""Aggregate stage of the maker-edge pilot (PLAN_RESEARCH_CYCLE_1 S1).

Consumes markout.parquet + dim_segments.parquet; produces the per-layer
primary metric (with per-match block-bootstrap CI), exploratory buckets, the
DQ report, and the interactive HTML (7 charts, vendored ECharts). Called by
maker_edge_pilot.py; kept separate for length. See PRE_REGISTRATION.md.

The primary metric is PRE-REGISTERED (PRE_REGISTRATION.md, frozen before any
computation): vol-weighted half_spread − drift_30 − maker_fee, ¢/contract,
pessimistic fills, pre-match, val split, PER LAYER (maker_fee_class ×
tour_level) — cross-layer merge is forbidden. Everything under "exploratory"
is hypothesis-generating only.
"""
import json
import math
import os
import sys
import zlib

import numpy as np

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
IDENTITY_TOL_C = 0.01        # discipline #11 machine gate
MAKER_RATE = 0.0175          # kalshi_facts params.maker_rate (NON-GATE preview)
PRICE_BANDS = [(1, 10), (10, 30), (30, 50), (50, 70), (70, 90), (90, 99)]


def _seed(*parts):
    """Stable cross-process seed (python's hash() is salted per process, which
    would break the reproducible-fingerprint discipline #8)."""
    return zlib.crc32("|".join(map(str, parts)).encode()) & 0xFFFFFFFF


def _ceil_centicent(x):
    return math.ceil(round(x * 10000.0, 6)) / 10000.0


def maker_fee_preview_cents(p_cents):
    """NON-GATE research preview of the maker fee, ¢/contract, via the
    documented formula ceil_to_centicent(maker_rate·P·(1−P)). P in dollars."""
    P = p_cents / 100.0
    return _ceil_centicent(MAKER_RATE * P * (1.0 - P)) * 100.0


# SQL twin of maker_fee_preview_cents, applied PER FILL in the analysis table
# (pre-registration: the charged layer subtracts the preview as its own column)
FEE_SQL = ("CASE WHEN g.maker_fee_class='charged' THEN "
           "ceil(round(%.6f*(k.p_fill/100.0)*(1.0-k.p_fill/100.0)*10000.0,6))"
           "/10000.0*100.0 ELSE 0.0 END" % MAKER_RATE)


def _boot_ratio_ci(num, den, seed):
    """Per-match block bootstrap (discipline #4) of sum(num)/sum(den) over
    matches. num/den: per-match numpy arrays. Returns [lo, hi] 95% CI."""
    m = len(den)
    if m < 2:
        return [None, None]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, m, size=(NBOOT, m))
    D = den[idx].sum(axis=1)
    ok = D != 0
    if not ok.any():
        return [None, None]
    v = num[idx].sum(axis=1)[ok] / D[ok]
    return [float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))]


def run(con, args, fp):
    # single-threaded for this stage: parallel aggregation sums floats in
    # nondeterministic order (last-ulp jitter across runs), which would break
    # the same-command-same-data bit-reproducibility of discipline #8. The
    # aggregate stage is seconds-scale; slice/markout keep full parallelism.
    con.execute("SET threads TO 1")
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
    # (1c tick, win_loss, known fee class), label split + phase + per-fill fee.
    drift_cols = ", ".join("k.drift_%d, k.markout_%d, k.markout_chk_%d"
                           % (h, h, h) for h in HORIZONS)
    con.execute("""
      CREATE TEMP TABLE a AS
      SELECT k.market_ticker, k.event_id, k.ts_utc, k.sign, k.count_e4,
             k.p_fill, k.mid_t, k.half_spread_c, k.spread_e4, k.fill_class,
             k.bounce, %s,
             %s AS fee_c,
             g.tour_level, g.maker_fee_class, g.tick_stratum, g.market_kind,
             CASE WHEN k.ts_utc < %d THEN 'train' ELSE 'val' END AS split,
             CASE WHEN o.onset_ts IS NULL OR k.ts_utc < o.onset_ts
                  THEN 'pre_match' ELSE 'in_play' END AS phase
      FROM read_parquet('%s') k
      JOIN read_parquet('%s') g ON g.market_ticker = k.market_ticker
      LEFT JOIN onset o ON o.market_ticker = k.market_ticker
      WHERE k.dq_class='ok' AND g.tick_stratum='1c' AND g.market_kind='win_loss'
        AND g.maker_fee_class IN ('zero','charged')
    """ % (drift_cols, FEE_SQL, train_end_us, m, s))

    # ── IDENTITY SELF-CHECK (discipline #11 machine gate) — per bucket, both
    # |mean(bounce)+mean(drift)−mean(markout)| and the independent recompute.
    worst_gap, identity_table = _identity_gate(con)
    print("  IDENTITY SELF-CHECK: max per-bucket gap = %.6f¢ (tolerance %.2f¢) %s"
          % (worst_gap, IDENTITY_TOL_C,
             "PASS" if worst_gap < IDENTITY_TOL_C else "FAIL"))
    if worst_gap >= IDENTITY_TOL_C:
        print("  REJECT: bounce/drift decomposition identity violated — "
              "the algorithm is wrong (discipline #11). No report written.")
        return 2

    result = {"pre_registration_file": "PRE_REGISTRATION.md", "fingerprint": fp,
              "regime": "slam-week", "grade": "dev-grade",
              "non_gate_banner": "FEES NON-GATE — verified=false research preview "
              "(maker_rate=%.4f); no facts-gated tool touched (OQ-1 unratified)"
              % MAKER_RATE, "primary_horizon_s": PRIMARY_H, "min_n": MIN_N,
              "conclusions_allowed": ["methodology-valid+collect", "methodology-flawed"],
              "machine_gate": {"identity_max_gap_c": worst_gap,
                               "identity_tolerance_c": IDENTITY_TOL_C,
                               "identity_pass": True,
                               "identity_buckets": identity_table},
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
    result["dq"]["staleness_hist"] = _staleness_hist(con, m)
    result["dq"]["fill_class"] = {c: n for c, n in con.execute(
        "SELECT fill_class, count(*) FROM a GROUP BY 1").fetchall()}

    # ── PRIMARY METRIC per layer (fee_class × tour_level), pre-match, val,
    # pessimistic. Per-fill net = half_spread − drift_30 − maker_fee(per fill).
    # Aggregate to per-MATCH first (block bootstrap resamples matches).
    base = ("FROM a WHERE phase='pre_match' AND split='val' "
            "AND fill_class='pessimistic'")
    layers = con.execute(
        "SELECT maker_fee_class, tour_level, count(*) n %s "
        "GROUP BY 1,2 ORDER BY 3 DESC" % base).fetchall()

    for fee_class, tour, n in layers:
        lf = ("%s AND maker_fee_class='%s' AND tour_level='%s'"
              % (base, fee_class, tour))
        rows = con.execute("""
          SELECT event_id,
                 sum(count_e4/10000.0) AS vol,
                 sum((count_e4/10000.0)*half_spread_c) AS vhs,
                 sum((count_e4/10000.0)*drift_%d) AS vdr,
                 sum((count_e4/10000.0)*fee_c) AS vfee,
                 sum((count_e4/10000.0)*p_fill) AS vpx
          %s GROUP BY 1 ORDER BY 1""" % (PRIMARY_H, lf)).fetchall()
        if not rows:
            continue
        vol = np.array([r[1] for r in rows], dtype=float)
        vhs = np.array([r[2] or 0.0 for r in rows], dtype=float)
        vdr = np.array([r[3] or 0.0 for r in rows], dtype=float)
        vfee = np.array([r[4] or 0.0 for r in rows], dtype=float)
        vpx = np.array([r[5] or 0.0 for r in rows], dtype=float)
        V = vol.sum()
        hs, dr, fee = vhs.sum() / V, vdr.sum() / V, vfee.sum() / V
        avgp = vpx.sum() / V
        point = hs - dr - fee
        ci = _boot_ratio_ci(vhs - vdr - vfee, vol, _seed("layer", fee_class, tour))
        auto_collect = n < MIN_N
        result["layers"].append({
            "maker_fee_class": fee_class, "tour_level": tour, "n_pessimistic": n,
            "n_matches": len(rows), "half_spread_c": hs, "drift_30_c": dr,
            "maker_fee_c": fee, "avg_price_c": avgp,
            "net_edge_c": point, "ci95": ci,
            "auto_collect_n_lt_min": auto_collect,
            "primary_metric_interpreted": not auto_collect,
            "conclusion": "methodology-valid+collect",
            "note": ("n<%d ⇒ auto-collect; primary metric NOT interpreted "
                     "(discipline #10)" % MIN_N) if auto_collect else
                    "n≥%d; metric interpretable (dev-grade, regime=slam-week)" % MIN_N,
            "h1_fee_wall_applicable": fee_class == "zero"})

    result["s1_conclusion"] = "methodology-valid+collect"
    result["s1_conclusion_basis"] = (
        "identity self-check PASS (max gap %.6f¢ < %.2f¢); all drops counted; "
        "pre-registered metric computed per layer on val only; Mac-era data ⇒ "
        "dev-grade, regime=slam-week; trade/reject reserved for S5."
        % (worst_gap, IDENTITY_TOL_C))

    # ── exploratory (hypotheses only, discipline #1): all CI = per-match
    # block bootstrap (discipline #4)
    result["exploratory"]["markout_curve"] = _markout_curve(con)
    result["exploratory"]["decomposition"] = _decomposition(con)
    result["exploratory"]["spread_hist"] = _spread_hist(con)
    result["exploratory"]["time_of_day"] = _time_of_day(con)
    result["exploratory"]["per_match_scatter"] = _per_match_scatter(con, base)
    result["exploratory"]["burstiness"] = _burstiness(con)
    result["exploratory"]["burstiness_note"] = (
        "ERRATUM 2026-07-10: trade-gap (inter-trade interval), NOT book-update "
        "interval — computed over trade rows; book-level heartbeat left to S4")

    with open(os.path.join(OUTDIR, "results.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    _write_html(result)
    _print_summary(result)
    return 0


def _identity_gate(con):
    """Per-bucket (fee×tour×horizon) identity gaps over dq-ok rows, both the
    decomposition identity and the independent markout recompute."""
    worst, table = 0.0, []
    for h in HORIZONS:
        rows = con.execute("""
          SELECT maker_fee_class, tour_level, count(markout_%d) AS n,
                 abs(avg(bounce) FILTER (WHERE markout_%d IS NOT NULL)
                     + avg(drift_%d) - avg(markout_%d)) AS g1,
                 abs(avg(markout_%d) - avg(markout_chk_%d)) AS g2
          FROM a GROUP BY 1, 2 ORDER BY 1, 2""" % ((h,) * 6)).fetchall()
        for fee, tour, nn, g1, g2 in rows:
            if not nn:
                continue
            worst = max(worst, g1 or 0.0, g2 or 0.0)
            table.append({"fee": fee, "tour": tour, "h": h, "n": nn,
                          "gap_decomp_c": g1, "gap_recompute_c": g2})
    return worst, table


def _markout_curve(con):
    """Toxicity curve cells: (tour incl. ALL) × phase × price band × horizon,
    zero-fee layer, pessimistic, val. mean + block-bootstrap CI + n."""
    band_case = "CASE %s END" % " ".join(
        "WHEN mid_t >= %d AND mid_t < %d THEN '%d-%dc'" % (lo, hi, lo, hi)
        for lo, hi in PRICE_BANDS)
    sums = ", ".join("sum(markout_%d), count(markout_%d)" % (h, h)
                     for h in HORIZONS)
    rows = con.execute("""
      SELECT tour_level, phase, %s AS band, event_id, %s
      FROM a
      WHERE split='val' AND fill_class='pessimistic' AND maker_fee_class='zero'
        AND (%s) IS NOT NULL
      GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4""" % (band_case, sums, band_case)).fetchall()
    cells = {}
    for r in rows:
        rec = [0.0 if v is None else float(v) for v in r[4:]]
        for t in (r[0], "ALL"):
            cells.setdefault((t, r[1], r[2]), []).append(rec)
    out = []
    for (tour, phase, band), recs in sorted(cells.items()):
        A = np.array(recs, dtype=float)
        for i, h in enumerate(HORIZONS):
            sm, c = A[:, 2 * i], A[:, 2 * i + 1]
            C = c.sum()
            if C == 0:
                continue
            ci = _boot_ratio_ci(sm, c, _seed("curve", tour, phase, band, h))
            out.append({"tour": tour, "phase": phase, "band": band, "h": h,
                        "n": int(C), "mean": float(sm.sum() / C),
                        "lo": ci[0], "hi": ci[1]})
    return out


def _decomposition(con):
    """bounce/drift/markout means (+ CI each) per (tour incl. ALL) × phase ×
    horizon; identity gap carried per cell. Zero-fee, pessimistic, val."""
    cols = []
    for h in HORIZONS:
        cols.append("sum(bounce) FILTER (WHERE markout_%d IS NOT NULL)" % h)
        cols.append("sum(drift_%d)" % h)
        cols.append("sum(markout_%d)" % h)
        cols.append("count(markout_%d)" % h)
    rows = con.execute("""
      SELECT tour_level, phase, event_id, %s
      FROM a WHERE split='val' AND fill_class='pessimistic'
        AND maker_fee_class='zero'
      GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""" % ", ".join(cols)).fetchall()
    cells = {}
    for r in rows:
        rec = [0.0 if v is None else float(v) for v in r[3:]]
        for t in (r[0], "ALL"):
            cells.setdefault((t, r[1]), []).append(rec)
    out = []
    for (tour, phase), recs in sorted(cells.items()):
        A = np.array(recs, dtype=float)
        for i, h in enumerate(HORIZONS):
            sb, sd, sm, c = (A[:, 4 * i + j] for j in range(4))
            C = c.sum()
            if C == 0:
                continue
            entry = {"tour": tour, "phase": phase, "h": h, "n": int(C),
                     "bounce": float(sb.sum() / C), "drift": float(sd.sum() / C),
                     "markout": float(sm.sum() / C)}
            entry["identity_gap"] = abs(entry["bounce"] + entry["drift"]
                                        - entry["markout"])
            for name, arr in (("bounce", sb), ("drift", sd), ("markout", sm)):
                lo, hi = _boot_ratio_ci(arr, c, _seed("dec", tour, phase, h, name))
                entry[name + "_lo"], entry[name + "_hi"] = lo, hi
            out.append(entry)
    return out


def _spread_hist(con):
    rows = con.execute(
        "SELECT tour_level, round(spread_e4/100.0) sc, count(*) n FROM a "
        "WHERE phase='pre_match' GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    out = {}
    for tour, sc, n in rows:
        out.setdefault(tour, []).append({"spread_c": sc, "n": n})
    return out


def _time_of_day(con):
    rows = con.execute(
        "SELECT phase, dayofweek(to_timestamp(ts_utc/1e6)) dow, "
        "hour(to_timestamp(ts_utc/1e6)) hr, count(*) n, "
        "sum(count_e4/10000.0) vol, avg(spread_e4/100.0) spr "
        "FROM a GROUP BY 1,2,3 ORDER BY 1,2,3").fetchall()
    return [{"phase": r[0], "dow": int(r[1]), "hr": int(r[2]), "n": r[3],
             "vol": r[4], "spread_c": r[5]} for r in rows]


def _per_match_scatter(con, base):
    rows = con.execute("""
      SELECT event_id, tour_level, maker_fee_class,
             sum((count_e4/10000.0)*half_spread_c)/nullif(sum(count_e4/10000.0),0) hs,
             sum((count_e4/10000.0)*drift_30)/nullif(sum(count_e4/10000.0),0) dr,
             count(*) n
      %s
      GROUP BY 1,2,3 HAVING count(*) >= 5 ORDER BY 1,2,3""" % base).fetchall()
    return [{"event": r[0], "tour": r[1], "fee": r[2], "half_spread": r[3],
             "drift": r[4], "n": r[5]} for r in rows]


def _burstiness(con):
    """Tennis-only inter-TRADE gap (time between consecutive trades in the
    same market), pre-match vs in-play. ERRATUM 2026-07-10: this reads trade
    rows (the markout parquet), NOT book updates — earlier labels said 盘口/
    book inter-update, which was wrong. The plan's true book-level heartbeat
    distribution needs the L1 book slice and is left to S4."""
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


def _staleness_hist(con, m):
    rows = con.execute("""
      SELECT CASE
        WHEN st < 0.1 THEN '<0.1s' WHEN st < 0.5 THEN '0.1-0.5s'
        WHEN st < 1 THEN '0.5-1s' WHEN st < 2 THEN '1-2s'
        WHEN st < 5 THEN '2-5s' WHEN st < 10 THEN '5-10s'
        WHEN st < 30 THEN '10-30s' ELSE '30-60s' END AS bin, count(*)
      FROM (SELECT (ts_utc - book_ts)/1e6 AS st FROM read_parquet('%s')
            WHERE dq_class='ok') GROUP BY 1""" % m).fetchall()
    order = ["<0.1s", "0.1-0.5s", "0.5-1s", "1-2s", "2-5s", "5-10s",
             "10-30s", "30-60s"]
    d = dict(rows)
    return [{"bin": b, "n": int(d.get(b, 0))} for b in order]


def _print_summary(r):
    print("\n=== MAKER-EDGE PILOT (dev-grade, regime=slam-week) ===")
    print(r["non_gate_banner"])
    print("IDENTITY SELF-CHECK: max gap %.6f¢ < %.2f¢ PASS"
          % (r["machine_gate"]["identity_max_gap_c"],
             r["machine_gate"]["identity_tolerance_c"]))
    print("phase tau (train):", round(r["phase_tau_train"], 1))
    print("DQ:", r["dq"]["classes"], "| fill:", r["dq"]["fill_class"])
    print("\nPRIMARY METRIC per layer (pre-match, val, pessimistic, %ds) — "
          "NO cross-layer merge:" % r["primary_horizon_s"])
    print("%-8s %-11s %8s %7s %9s %9s %8s %10s %-17s %s" %
          ("fee", "tour", "n_pess", "match", "half_sp", "drift30", "fee_c",
           "net_edge", "ci95", "n vs %d" % r["min_n"]))
    for L in r["layers"]:
        ci = L["ci95"]
        ci_txt = ("[%.3f,%.3f]" % (ci[0], ci[1])) if ci[0] is not None else "[n/a]"
        nvs = ("n<%d AUTO-COLLECT" % r["min_n"]) if L["auto_collect_n_lt_min"] \
            else ("n>=%d ok" % r["min_n"])
        print("%-8s %-11s %8d %7d %9.3f %9.3f %8.3f %10.3f %-17s %s" %
              (L["maker_fee_class"], L["tour_level"], L["n_pessimistic"],
               L["n_matches"], L["half_spread_c"], L["drift_30_c"],
               L["maker_fee_c"], L["net_edge_c"], ci_txt, nvs))
    print("\nS1 conclusion: %s (allowed set: %s)"
          % (r["s1_conclusion"], r["conclusions_allowed"]))


# ─────────────────────────────────────────────────────── HTML (ECharts)

def _write_html(r):
    import html_report_maker_edge as H
    H.write(r, OUTDIR, ECHARTS)
