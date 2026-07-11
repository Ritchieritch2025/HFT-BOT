#!/usr/bin/env python3
"""WP-06 Research Notebook — maker-viability metrics on recorded data.

Computes, per market-day over every clean day reachable through
tools/warehouse.py::load() (read-only, the ONLY data access):

  K / z / wiggle  +  reversal_rate  +  inter-update interval p50/p90

and renders ONE static HTML page (tables + inline SVG bars, stdlib-rendered,
no CDN, no new deps) with rankings in BOTH price space and log-odds space
(plan amendment A2), rollups by category/subcategory/group and hour-of-day,
and interval histograms. NO strategy classes, NO simulation, NO fill models
— explicitly out of WP-06 scope.

════════════════════════════════════════════════════════════════════════
METRIC INTERPRETATION — pinned by the plan's hand-computed fixture
(EXECUTION_PLAN WP-06: mids [10,12,10,12] -> K=12, z=2, wiggle=(12-4)/2=4).
Where the prose was ambiguous, THIS is the adopted reading (queued for R's
confirmation in docs/BACKLOG.md, 2026-07-07):

  moves         consecutive NONZERO mid changes within one market-day
                (change-only rows can repeat a mid when only size moved;
                zero moves carry no price information and are dropped)
  K             sum of squared moves — realized quadratic variation.
                Fixture: (+2)^2 + (-2)^2 + (+2)^2 = 12  ✓
  z             net displacement, last mid − first mid. Fixture: 12−10=2 ✓
  wiggle        (K − z²) / 2. Fixture: (12−4)/2 = 4  ✓
                This is the classic mean-reversion-harvest bound: the
                gross profit of a unit contrarian (maker-like) position
                over the path ≈ ½·(realized variance − squared drift).
                Big wiggle = the mid oscillates without going anywhere =
                spread capture can pay; drift (z²) is what makers bleed.
  reversal_rate sign flips between consecutive moves / (n_moves − 1).
                Fixture: 2 flips / 2 pairs = 1.0  ✓. Space-invariant
                (logit is monotonic), so it is emitted once.

  REJECTED reading: "K = max mid excursion in cents; z = # direction
  reversals". It also matches the fixture numerically (max mid = 12,
  reversals = 2) but mixes cents with a squared count — dimensionally
  meaningless — and (K−z²)/2 then has no economic content. The adopted
  reading keeps every term in the same squared-move units and reproduces
  the fixture exactly in both spaces.

  Log-odds space (A2): identical formulas on logit(mid), mid as a
  probability clipped to [0.01, 0.99] — the same transform as
  tools/mm_calibrate.py. Price-space-only screening overweights mid-range
  markets (vol ∝ p(1−p)); divergence between the two rankings is exactly
  what the page surfaces.

  Heartbeats: staging L1 heartbeat rows are is_snapshot=true AND
  price_e4 NULL (scheduled heartbeats carry remembered book state but
  NULL price/volume/oi — docs/warehouse_schema.md). Excluded from ALL
  metrics BEFORE computation; first-observation snapshots (price present)
  are real observations and are kept. Book-validity filter (bid>0,
  ask<100c, ask>bid — one-sided/crossed states carry no mid) applies
  after heartbeat exclusion, same as mm_calibrate.

  Intervals: inter-update intervals per market-day on the rows that
  survive the above; percentiles are nearest-rank (the gold-suite
  convention).

  Fees: NOT part of any metric here (config/kalshi_facts.yaml
  fees.verified is false — WP-05 audit, fail-closed). trade_fee() reads
  the yaml at call time; any gate_mode=True call while verified is not
  true raises FeeNotVerifiedError (GUARDRAILS S2/Q3; WP-07 re-runs the
  rankings once R ratifies the fee provenance). The page shows the fee
  status banner instead of pretending.
════════════════════════════════════════════════════════════════════════

Usage:
  python3 tools/mm_research.py [--start D] [--end D] [--out PATH]
                               [--top N] [--min-updates N]

Outputs: work/mm/research_<date>.html + work/mm/research_metrics_<date>.csv
Read-only on the pipeline; load() retries through the ingest write lock.
"""
import argparse
import csv
import datetime
import glob as _glob
import html as _html
import math
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACTS_PATH = os.path.join(ROOT, "config", "kalshi_facts.yaml")
FIRST_CLEAN_DAY = "2026-07-06"   # H-3 clean-day clock day 1
DAY_US = 86_400_000_000

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════ core metrics

def wiggle_metrics(mids):
    """K / z / wiggle / reversal_rate for one mid series (any space).

    Pinned fixture: [10,12,10,12] -> K=12, z=2, wiggle=4, reversal=1.0.
    See module docstring for the adopted interpretation."""
    mids = [float(m) for m in mids]
    moves = [b - a for a, b in zip(mids, mids[1:]) if b != a]
    k = sum(d * d for d in moves)
    z = (mids[-1] - mids[0]) if mids else 0.0
    rev = None
    if len(moves) >= 2:
        flips = sum(1 for a, b in zip(moves, moves[1:]) if a * b < 0)
        rev = flips / (len(moves) - 1)
    return {"K": k, "z": z, "wiggle": (k - z * z) / 2.0,
            "reversal_rate": rev, "n_moves": len(moves)}


def pctl_nearest_rank(sorted_vals, q):
    """Nearest-rank percentile (gold-suite convention): value at the
    ceil(q*n)-th 1-indexed position of the sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return None
    return sorted_vals[max(1, math.ceil(q * n)) - 1]


def interval_stats(ts_us):
    """Inter-update intervals (seconds) for one market; nearest-rank
    p50/p90. Fixture: 0,1,2,3,4,10s -> p50=1.0, p90=6.0."""
    ts = sorted(int(t) for t in ts_us)
    iv = sorted((b - a) / 1e6 for a, b in zip(ts, ts[1:]))
    return {"n": len(iv),
            "p50_s": pctl_nearest_rank(iv, 0.50),
            "p90_s": pctl_nearest_rank(iv, 0.90)}


def to_logit(mid_cents):
    """logit of the mid as a probability, clipped to [0.01, 0.99]
    (mm_calibrate's transform, A2)."""
    p = min(max(float(mid_cents) / 100.0, 0.01), 0.99)
    return math.log(p / (1.0 - p))


def exclude_heartbeats(df):
    """Drop staging L1 heartbeat rows: is_snapshot=true AND price_e4 NULL.
    First-observation snapshots (price present) are kept — the rule is the
    AND, never is_snapshot alone."""
    hb = df["is_snapshot"].fillna(False).astype(bool) & df["price_e4"].isna()
    return df[~hb]


def market_day_metrics(df):
    """Per (market_ticker, UTC day) metric records, BOTH spaces (A2).

    df: staging/archive-shaped L1 rows (ts_utc µs, yes_bid_e4, yes_ask_e4,
    price_e4, is_snapshot + hierarchy columns). Returns list of dicts."""
    import numpy as np
    import pandas as pd

    df = exclude_heartbeats(df)
    df = df.dropna(subset=["yes_bid_e4", "yes_ask_e4"])
    df = df[(df["yes_bid_e4"] > 0) & (df["yes_ask_e4"] < 10000) &
            (df["yes_ask_e4"] > df["yes_bid_e4"])]
    if df.empty:
        return []
    df = df.copy()
    df["mid_c"] = (df["yes_bid_e4"] + df["yes_ask_e4"]) / 200.0   # cents
    p = np.clip(df["mid_c"].values / 100.0, 0.01, 0.99)
    df["mid_lo"] = np.log(p / (1.0 - p))
    df["day"] = pd.to_datetime(df["ts_utc"], unit="us", utc=True) \
                  .dt.strftime("%Y-%m-%d")

    out = []
    for (mt, day), g in df.groupby(["market_ticker", "day"], sort=False):
        g = g.sort_values("ts_utc")
        wm = wiggle_metrics(g["mid_c"].values)
        wl = wiggle_metrics(g["mid_lo"].values)
        ist = interval_stats(g["ts_utc"].values)
        out.append({
            "market_ticker": mt, "day": day,
            "category": g["category"].iloc[0],
            "subcategory": g["subcategory"].iloc[0],
            "group": g["group"].iloc[0],
            "n_updates": int(len(g)),
            "K_px": wm["K"], "z_px": wm["z"], "wiggle_px": wm["wiggle"],
            "K_lo": wl["K"], "z_lo": wl["z"], "wiggle_lo": wl["wiggle"],
            "reversal_rate": wm["reversal_rate"],   # space-invariant
            "n_moves": wm["n_moves"],
            "interval_p50_s": ist["p50_s"], "interval_p90_s": ist["p90_s"],
        })
    return out


# ══════════════════════════════════════════════════════════════════ fees

class FeeNotVerifiedError(RuntimeError):
    """Gate-mode fee computation refused: kalshi_facts.yaml fees.verified
    is not true (GUARDRAILS S2 fail-closed; WP-05 audit)."""


def load_fee_facts(path=None):
    """fees.* from config/kalshi_facts.yaml — read at call time, never
    cached into code, never hardcoded."""
    import yaml
    with open(path or FACTS_PATH) as f:
        return yaml.safe_load(f)["fees"]


def trade_fee(price_dollars, contracts, rate=None, fee_multiplier=1.0,
              gate_mode=False, facts_path=None):
    """Kalshi trade fee per kalshi_facts.yaml:
    ceil_to_centicent(rate * fee_multiplier * C * P * (1-P)), dollars.

    gate_mode=True is for go/no-go arithmetic: it REFUSES (raises
    FeeNotVerifiedError) unless the yaml says fees.verified: true.
    gate_mode=False is research/diagnosis: computes with the recorded
    (possibly unratified) rate — callers must surface the verified flag."""
    fees = load_fee_facts(facts_path)
    if gate_mode and fees.get("verified") is not True:
        raise FeeNotVerifiedError(
            "fees.verified=%r in %s — gate-mode fee math is forbidden until "
            "R ratifies the fee schedule provenance (WP-05 OPEN QUESTION 1)"
            % (fees.get("verified"), facts_path or FACTS_PATH))
    r = float(rate) if rate is not None else float(fees["params"]["taker_rate"])
    raw = r * fee_multiplier * contracts * price_dollars * (1.0 - price_dollars)
    return math.ceil(round(raw * 10000.0, 6)) / 10000.0


# ═══════════════════════════════════════════════════════════ build (real)

_HIST_EDGES = [(0.0, 0.1, "<0.1s"), (0.1, 1.0, "0.1–1s"), (1.0, 10.0, "1–10s"),
               (10.0, 60.0, "10–60s"), (60.0, 600.0, "1–10m"),
               (600.0, 3600.0, "10–60m"), (3600.0, float("inf"), "≥1h")]


def _hist_bucket(sec):
    for i, (lo, hi, _) in enumerate(_HIST_EDGES):
        if lo <= sec < hi:
            return i
    return len(_HIST_EDGES) - 1


def build_dataset(start, end, archive_only=False):
    """load() L1 for [start, end], compute per-market-day metrics + rollups.
    Pipeline read-only; heavy lifting mirrors mm_calibrate."""
    import numpy as np
    from warehouse import load
    import warehouse_common as wc

    cols = ["ts_utc", "market_ticker", "category", "subcategory", '"group"',
            "yes_bid_e4", "yes_ask_e4", "price_e4", "is_snapshot"]
    df = load("orderbooks_l1", start=start, end=end, columns=cols,
              archive_only=archive_only).df()

    n_raw = len(df)
    df = exclude_heartbeats(df)
    n_hb = n_raw - len(df)
    df = df.dropna(subset=["yes_bid_e4", "yes_ask_e4"])
    df = df[(df["yes_bid_e4"] > 0) & (df["yes_ask_e4"] < 10000) &
            (df["yes_ask_e4"] > df["yes_bid_e4"])]
    n_valid = len(df)
    df = df.copy()
    df["mid_c"] = (df["yes_bid_e4"] + df["yes_ask_e4"]) / 200.0
    p = np.clip(df["mid_c"].values / 100.0, 0.01, 0.99)
    df["mid_lo"] = np.log(p / (1.0 - p))
    day_idx = (df["ts_utc"] // DAY_US).astype("int64")
    df["day"] = [datetime.datetime.fromtimestamp(d * 86400,
                 tz=datetime.timezone.utc).strftime("%Y-%m-%d") for d in day_idx]
    df["hour"] = ((df["ts_utc"] % DAY_US) // 3_600_000_000).astype("int64")

    # Archive sealing proves byte checkpoint + staging/archive parity only;
    # capture completeness remains a separate PIPE-W03 gate.
    cfg = wc.load_config()
    archived = {os.path.basename(m)[5:-5]
                for m in _glob.glob(os.path.join(
                    cfg["warehouse_root"], "seals", "date=*.json"))}

    recs = []
    hour_updates = [0] * 24
    hour_k_px = [0.0] * 24
    hour_k_lo = [0.0] * 24
    hist = [0] * len(_HIST_EDGES)

    for (mt, day), g in df.groupby(["market_ticker", "day"], sort=False):
        g = g.sort_values("ts_utc")
        ts = g["ts_utc"].values
        mids = g["mid_c"].values
        mids_lo = g["mid_lo"].values
        hours = g["hour"].values
        wm = wiggle_metrics(mids)
        wl = wiggle_metrics(mids_lo)
        ist = interval_stats(ts)
        recs.append({
            "market_ticker": mt, "day": day,
            "partial": day not in archived,
            "category": g["category"].iloc[0] or "?",
            "subcategory": g["subcategory"].iloc[0] or "?",
            "group": g["group"].iloc[0] or "?",
            "n_updates": int(len(g)),
            "K_px": wm["K"], "z_px": wm["z"], "wiggle_px": wm["wiggle"],
            "K_lo": wl["K"], "z_lo": wl["z"], "wiggle_lo": wl["wiggle"],
            "reversal_rate": wm["reversal_rate"], "n_moves": wm["n_moves"],
            "interval_p50_s": ist["p50_s"], "interval_p90_s": ist["p90_s"],
        })
        # hour-of-day rollup: updates + K contribution (hour of the later tick)
        for h in hours:
            hour_updates[h] += 1
        d_px = np.diff(mids)
        d_lo = np.diff(mids_lo)
        nz = d_px != 0
        for h, dp, dl in zip(hours[1:][nz], d_px[nz], d_lo[nz]):
            hour_k_px[h] += dp * dp
            hour_k_lo[h] += dl * dl
        for a, b in zip(ts, ts[1:]):
            hist[_hist_bucket((b - a) / 1e6)] += 1

    days = sorted({r["day"] for r in recs})
    return {"records": recs, "days": days, "archived_days": archived,
            "n_raw": n_raw, "n_heartbeats": n_hb, "n_valid": n_valid,
            "hour_updates": hour_updates, "hour_k_px": hour_k_px,
            "hour_k_lo": hour_k_lo, "interval_hist": hist}


def rollup(recs, keyfn):
    agg = defaultdict(lambda: {"markets": set(), "updates": 0,
                               "wiggle_px": 0.0, "wiggle_lo": 0.0,
                               "rev": [], "p50": []})
    for r in recs:
        a = agg[keyfn(r)]
        a["markets"].add(r["market_ticker"])
        a["updates"] += r["n_updates"]
        a["wiggle_px"] += r["wiggle_px"]
        a["wiggle_lo"] += r["wiggle_lo"]
        if r["reversal_rate"] is not None:
            a["rev"].append(r["reversal_rate"])
        if r["interval_p50_s"] is not None:
            a["p50"].append(r["interval_p50_s"])
    rows = []
    for k, a in agg.items():
        rows.append({"key": k, "markets": len(a["markets"]),
                     "updates": a["updates"],
                     "wiggle_px": a["wiggle_px"], "wiggle_lo": a["wiggle_lo"],
                     "mean_reversal": (sum(a["rev"]) / len(a["rev"]))
                     if a["rev"] else None,
                     "median_p50_s": pctl_nearest_rank(sorted(a["p50"]), 0.5)
                     if a["p50"] else None})
    rows.sort(key=lambda r: -r["wiggle_lo"])
    return rows


def rankings(recs, min_updates, top_n):
    """Ranks over the SAME eligible set in both spaces + rank-change list."""
    elig = [r for r in recs if r["n_updates"] >= min_updates]
    by_px = sorted(elig, key=lambda r: -r["wiggle_px"])
    by_lo = sorted(elig, key=lambda r: -r["wiggle_lo"])
    rank_px = {(r["market_ticker"], r["day"]): i + 1 for i, r in enumerate(by_px)}
    rank_lo = {(r["market_ticker"], r["day"]): i + 1 for i, r in enumerate(by_lo)}
    union_keys = [(r["market_ticker"], r["day"]) for r in by_px[:top_n]]
    for r in by_lo[:top_n]:
        k = (r["market_ticker"], r["day"])
        if k not in union_keys:
            union_keys.append(k)
    idx = {(r["market_ticker"], r["day"]): r for r in elig}
    changes = [{"rec": idx[k], "rank_px": rank_px[k], "rank_lo": rank_lo[k],
                "delta": rank_px[k] - rank_lo[k]} for k in union_keys]
    changes.sort(key=lambda c: min(c["rank_lo"], c["rank_px"]))
    return by_px[:top_n], by_lo[:top_n], changes, len(elig)


# ═══════════════════════════════════════════════════════════════════ page

_CSS = """
:root { --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --s1:#2a78d6;
  --s1-track:#cde2fb; --warn-bg:#fdf3e0; --warn-bd:#eda100;
  --border:rgba(11,11,11,0.10); }
@media (prefers-color-scheme: dark) {
  :root { --surface:#1a1a19; --page:#0d0d0d; --ink:#ffffff; --ink2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835; --s1:#3987e5;
    --s1-track:#104281; --warn-bg:#2a2313; --warn-bd:#c98500;
    --border:rgba(255,255,255,0.10); } }
* { box-sizing:border-box; }
body { margin:0; background:var(--page); color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
.wrap { max-width:1180px; margin:0 auto; padding:28px 20px 60px; }
h1 { font-size:22px; margin:0 0 2px; } h2 { font-size:16px; margin:34px 0 8px; }
.sub { color:var(--ink2); margin:0 0 14px; }
.note { background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:12px 14px; margin:12px 0; color:var(--ink2); }
.warn { background:var(--warn-bg); border:1px solid var(--warn-bd);
  border-radius:8px; padding:12px 14px; margin:12px 0; }
.tiles { display:flex; flex-wrap:wrap; gap:12px; margin:18px 0; }
.tile { background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:12px 16px; min-width:150px; }
.tile .lb { color:var(--ink2); font-size:12px; }
.tile .v { font-size:26px; font-weight:600; }
.tile .d { color:var(--muted); font-size:12px; }
.cols { display:flex; flex-wrap:wrap; gap:20px; align-items:flex-start; }
.col { flex:1 1 460px; min-width:0; }
.tblbox { overflow-x:auto; background:var(--surface);
  border:1px solid var(--border); border-radius:8px; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th { text-align:left; color:var(--ink2); font-weight:600; padding:8px 10px;
  border-bottom:1px solid var(--axis); white-space:nowrap; }
td { padding:6px 10px; border-bottom:1px solid var(--grid);
  white-space:nowrap; }
tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.bar { display:inline-block; vertical-align:middle; }
.badge { font-size:11px; border:1px solid var(--warn-bd); color:var(--ink2);
  border-radius:4px; padding:0 5px; margin-left:6px; }
.pos { color:#006300; } .neg { color:#b03030; }
@media (prefers-color-scheme: dark){ .pos{color:#0ca30c;} .neg{color:#e66767;} }
svg text { fill:var(--muted); font:11px system-ui,-apple-system,sans-serif; }
.chart { background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:14px; margin:10px 0; overflow-x:auto; }
.chart h3 { margin:0 0 8px; font-size:13px; color:var(--ink2);
  font-weight:600; }
"""


def _esc(s):
    return _html.escape(str(s))


def _fmt(v, nd=2):
    if v is None:
        return "–"
    if isinstance(v, float):
        if abs(v) >= 1000:
            return "{:,.0f}".format(v)
        return ("%%.%df" % nd) % v
    return "{:,}".format(v)


def _inline_bar(frac, width=110):
    """Tiny in-table magnitude bar (single-hue sequential, track behind)."""
    w = max(0.0, min(1.0, frac)) * width
    return ('<svg class="bar" width="%d" height="10" role="img">'
            '<rect x="0" y="2" width="%d" height="6" rx="3" '
            'fill="var(--s1-track)"/>'
            '<rect x="0" y="2" width="%.1f" height="6" rx="3" '
            'fill="var(--s1)"/></svg>' % (width, width, w))


def _svg_columns(values, labels, title, value_fmt=lambda v: _fmt(v, 0),
                 width=1080, height=180):
    """Single-series column chart: hairline baseline, ≤24px columns with a
    2px gap, max column direct-labeled, <title> hover on every mark."""
    n = len(values)
    if n == 0 or max(values) <= 0:
        return '<div class="chart"><h3>%s</h3><p class="sub">no data</p></div>' % _esc(title)
    pad_l, pad_b, pad_t = 8, 18, 16
    plot_w, plot_h = width - 2 * pad_l, height - pad_b - pad_t
    slot = plot_w / n
    bw = min(24.0, slot - 2)
    vmax = max(values)
    parts = ['<svg width="%d" height="%d" role="img" aria-label="%s">'
             % (width, height, _esc(title))]
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="var(--axis)" '
                 'stroke-width="1"/>' % (pad_l, height - pad_b, width - pad_l,
                                         height - pad_b))
    imax = values.index(vmax)
    for i, v in enumerate(values):
        h = (v / vmax) * plot_h
        x = pad_l + i * slot + (slot - bw) / 2
        y = height - pad_b - h
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                     'rx="3" fill="var(--s1)"><title>%s: %s</title></rect>'
                     % (x, y, bw, max(h, 1.0), _esc(labels[i]),
                        _esc(value_fmt(v))))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle">%s</text>'
                     % (x + bw / 2, height - 5, _esc(labels[i])))
        if i == imax:  # selective direct label: the extreme only
            parts.append('<text x="%.1f" y="%.1f" text-anchor="middle" '
                         'style="fill:var(--ink2);font-weight:600">%s</text>'
                         % (x + bw / 2, max(y - 4, 10), _esc(value_fmt(v))))
    parts.append("</svg>")
    return '<div class="chart"><h3>%s</h3>%s</div>' % (_esc(title), "".join(parts))


def _rank_table(rows, space, archived):
    hd = ("<tr><th class=num>#</th><th>market · day</th><th>cat / group</th>"
          "<th class=num>wiggle</th><th class=num>K</th><th class=num>z</th>"
          "<th class=num>reversal</th><th class=num>updates</th>"
          "<th class=num>p50 gap</th><th></th></tr>")
    wmax = max((r["wiggle_" + space] for r in rows), default=0) or 1
    body = []
    for i, r in enumerate(rows):
        nd = 1 if space == "px" else 3
        badge = '<span class="badge">PARTIAL</span>' if r["partial"] else ""
        body.append(
            "<tr><td class=num>%d</td><td>%s<br><span style='color:var(--muted)'>%s</span>%s</td>"
            "<td>%s · %s</td><td class=num><b>%s</b></td><td class=num>%s</td>"
            "<td class=num>%s</td><td class=num>%s</td><td class=num>%s</td>"
            "<td class=num>%ss</td><td>%s</td></tr>"
            % (i + 1, _esc(r["market_ticker"]), _esc(r["day"]), badge,
               _esc(r["category"]), _esc(r["group"]),
               _fmt(r["wiggle_" + space], nd), _fmt(r["K_" + space], nd),
               _fmt(r["z_" + space], nd), _fmt(r["reversal_rate"], 2),
               _fmt(r["n_updates"]), _fmt(r["interval_p50_s"], 1),
               _inline_bar(r["wiggle_" + space] / wmax)))
    return '<div class="tblbox"><table>%s%s</table></div>' % (hd, "".join(body))


def build_html(ds, top_px, top_lo, changes, n_elig, args, fees):
    recs = ds["records"]
    days = ds["days"]
    day_tags = ["%s%s" %
                (d, " (ARCHIVE-SEALED / CAPTURE-UNASSESSED)"
                 if d in ds["archived_days"] else " (PARTIAL)")
                for d in days]
    gen = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    fee_note = (
        '<div class="warn"><b>Fee status: fees.verified = %s.</b> '
        'Recorded rates (taker %s / maker %s, %s) are NOT ratified — '
        'gate-mode fee math refuses by construction (FeeNotVerifiedError). '
        'No metric on this page includes fees; WP-07 re-runs the rankings '
        'once R ratifies the schedule.</div>'
        % (_esc(fees.get("verified")), _esc(fees["params"].get("taker_rate")),
           _esc(fees["params"].get("maker_rate")), _esc(fees.get("evidence"))))

    interp = (
        '<div class="note"><b>Interpretation (pinned by the WP-06 fixture; '
        'R to confirm, see docs/BACKLOG.md):</b> moves = nonzero mid changes; '
        'K = Σ(Δmid)² (realized quadratic variation); z = net displacement; '
        '<b>wiggle = (K − z²)/2</b> = the mean-reversion-harvest bound (gross, '
        'fee-free, no fill model — WP-06 has no simulation by design). '
        'Log-odds space uses logit(mid), mid clipped to [0.01, 0.99]. '
        'Heartbeats (is_snapshot ∧ price NULL) and invalid books (one-sided/'
        'crossed) are excluded from all metrics. Intervals: nearest-rank '
        'percentiles. Fixture check: mids [10,12,10,12] → K=12, z=2, '
        'wiggle=4, reversal=1.0.</div>')

    tiles = "".join(
        '<div class="tile"><div class="lb">%s</div><div class="v">%s</div>'
        '<div class="d">%s</div></div>' % t for t in [
            ("Days", len(days), " · ".join(day_tags)),
            ("Market-days", _fmt(len(recs)), "%s ranked (≥%d updates)"
             % (_fmt(n_elig), args.min_updates)),
            ("Markets", _fmt(len({r["market_ticker"] for r in recs})), "with valid L1"),
            ("Updates", _fmt(ds["n_valid"]), "%s heartbeats excluded"
             % _fmt(ds["n_heartbeats"])),
        ])

    # rank-change list (the A2 point)
    chg_rows = []
    for c in changes:
        r = c["rec"]
        d = c["delta"]
        cls, arrow = ("pos", "▲") if d > 0 else (("neg", "▼") if d < 0 else ("", "·"))
        chg_rows.append(
            "<tr><td>%s<br><span style='color:var(--muted)'>%s</span></td>"
            "<td>%s · %s</td><td class=num>%d</td><td class=num>%d</td>"
            "<td class='num %s'>%s %s</td><td class=num>%s</td><td class=num>%s</td></tr>"
            % (_esc(r["market_ticker"]), _esc(r["day"]), _esc(r["category"]),
               _esc(r["group"]), c["rank_px"], c["rank_lo"], cls, arrow,
               ("%+d" % d) if d else "0", _fmt(r["wiggle_px"], 1),
               _fmt(r["wiggle_lo"], 3)))
    chg_tbl = ('<div class="tblbox"><table><tr><th>market · day</th>'
               '<th>cat / group</th><th class=num>rank (price)</th>'
               '<th class=num>rank (log-odds)</th><th class=num>Δ rank</th>'
               '<th class=num>wiggle¢²</th><th class=num>wiggle lo²</th></tr>'
               '%s</table></div>' % "".join(chg_rows))

    def rollup_table(rows, klabel, top=20):
        wmax = max((r["wiggle_lo"] for r in rows), default=0) or 1
        body = "".join(
            "<tr><td>%s</td><td class=num>%s</td><td class=num>%s</td>"
            "<td class=num>%s</td><td class=num>%s</td><td class=num>%s</td>"
            "<td class=num>%ss</td><td>%s</td></tr>"
            % (_esc(r["key"]), _fmt(r["markets"]), _fmt(r["updates"]),
               _fmt(r["wiggle_px"], 0), _fmt(r["wiggle_lo"], 1),
               _fmt(r["mean_reversal"], 2), _fmt(r["median_p50_s"], 1),
               _inline_bar(r["wiggle_lo"] / wmax))
            for r in rows[:top])
        return ('<div class="tblbox"><table><tr><th>%s</th>'
                '<th class=num>markets</th><th class=num>updates</th>'
                '<th class=num>Σwiggle ¢²</th><th class=num>Σwiggle lo²</th>'
                '<th class=num>reversal</th><th class=num>med p50</th>'
                '<th></th></tr>%s</table></div>' % (_esc(klabel), body))

    cat_rows = rollup(recs, lambda r: "%s / %s" % (r["category"], r["subcategory"]))
    grp_rows = rollup(recs, lambda r: "%s (%s)" % (r["group"], r["category"]))

    hours = ["%02d" % h for h in range(24)]
    hour_charts = (
        _svg_columns(ds["hour_updates"], hours,
                     "L1 updates by hour of day (UTC)") +
        _svg_columns([round(v, 2) for v in ds["hour_k_lo"]], hours,
                     "K by hour of day — log-odds space (Σ Δlogit², UTC)",
                     value_fmt=lambda v: _fmt(v, 1)) +
        _svg_columns([round(v, 1) for v in ds["hour_k_px"]], hours,
                     "K by hour of day — price space (Σ Δmid², cents², UTC)",
                     value_fmt=lambda v: _fmt(v, 0)))

    hist_labels = [e[2] for e in _HIST_EDGES]
    hist_chart = _svg_columns(ds["interval_hist"], hist_labels,
                              "Inter-update interval distribution "
                              "(all markets, heartbeats excluded)",
                              width=760)

    # drill-down: top 50 by log-odds wiggle
    drill = sorted([r for r in recs if r["n_updates"] >= args.min_updates],
                   key=lambda r: -r["wiggle_lo"])[:50]
    drill_body = "".join(
        "<tr><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td><td class=num>%s</td>"
        "<td class=num>%s</td><td class=num>%s</td><td class=num>%s</td>"
        "<td class=num>%s</td><td class=num>%s</td><td class=num>%s</td>"
        "<td class=num>%s</td><td class=num>%ss</td><td class=num>%ss</td></tr>"
        % (_esc(r["market_ticker"]), _esc(r["day"]),
           '<span class="badge">P</span>' if r["partial"] else "",
           _esc(r["category"]), _esc(r["group"]), _fmt(r["n_updates"]),
           _fmt(r["K_px"], 1), _fmt(r["z_px"], 1), _fmt(r["wiggle_px"], 1),
           _fmt(r["K_lo"], 3), _fmt(r["z_lo"], 3), _fmt(r["wiggle_lo"], 3),
           _fmt(r["reversal_rate"], 2), _fmt(r["interval_p50_s"], 1),
           _fmt(r["interval_p90_s"], 1)) for r in drill)
    drill_tbl = ('<div class="tblbox"><table><tr><th>market</th><th>day</th>'
                 '<th>category</th><th>group</th><th class=num>upd</th>'
                 '<th class=num>K¢²</th><th class=num>z¢</th>'
                 '<th class=num>wiggle¢²</th><th class=num>K lo²</th>'
                 '<th class=num>z lo</th><th class=num>wiggle lo²</th>'
                 '<th class=num>rev</th><th class=num>p50</th>'
                 '<th class=num>p90</th></tr>%s</table></div>' % drill_body)

    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MM research — K/z² + intervals — %s</title><style>%s</style></head>
<body><div class="wrap">
<h1>Maker-viability research — K / z² / wiggle + update intervals</h1>
<p class="sub">WP-06 · generated %s · window %s → %s · data via warehouse
load() (read-only) · both spaces per amendment A2</p>
%s%s
<div class="tiles">%s</div>
<h2>Top %d market-days — price space (wiggle, cents²)</h2>
<div class="cols"><div class="col">%s</div>
<div class="col"><h2 style="margin-top:0">Top %d — log-odds space (wiggle, logit²)</h2>%s</div></div>
<h2>Rank changes between spaces (union of both top-%d lists)</h2>
<p class="sub">▲ = market rises when measured in log-odds — price-space
screening was underweighting it (typically low/high-priced markets);
▼ = mid-range market flattered by price space (vol ∝ p(1−p)).</p>
%s
<h2>Rollup — category / subcategory</h2>%s
<h2>Rollup — group (league / region / asset)</h2>%s
<h2>Hour-of-day rollup</h2>%s
<h2>Interval histograms</h2>%s
<h2>Drill-down — top 50 market-days (log-odds wiggle), all metrics, both spaces</h2>
%s
<p class="sub">Metrics CSV alongside this page carries every market-day row.
No fees, no fills, no simulation in any number above (WP-06 scope).</p>
</div></body></html>""" % (
        _esc(days[-1] if days else ""), _CSS, gen,
        _esc(args.start), _esc(args.end), fee_note, interp, tiles,
        args.top, _rank_table(top_px, "px", ds["archived_days"]),
        args.top, _rank_table(top_lo, "lo", ds["archived_days"]),
        args.top, chg_tbl, rollup_table(cat_rows, "category / subcategory"),
        rollup_table(grp_rows, "group"), hour_charts, hist_chart, drill_tbl)


# ═══════════════════════════════════════════════════════════════════ main

def main(argv):
    import warehouse_common as wc
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default=FIRST_CLEAN_DAY)
    ap.add_argument("--end", default=today)
    ap.add_argument("--out", default=None)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-updates", dest="min_updates", type=int, default=10)
    ap.add_argument("--archive-only", action="store_true",
                    help="never attach live staging (automatic for past ranges)")
    args = ap.parse_args(argv[1:])
    try:
        end_date = datetime.date.fromisoformat(args.end)
    except ValueError:
        ap.error("--end must be YYYY-MM-DD")
    archive_only = (args.archive_only or
                    end_date < datetime.datetime.now(datetime.timezone.utc).date())

    fees = load_fee_facts()
    print("fees.verified = %s (gate-mode fee math %s)"
          % (fees.get("verified"),
             "ENABLED" if fees.get("verified") is True else "REFUSES — research only"))

    ds = build_dataset(args.start, args.end, archive_only=archive_only)
    print("window %s → %s: %s L1 rows, %s heartbeats excluded, %s valid; "
          "%d market-days over days: %s"
          % (args.start, args.end, "{:,}".format(ds["n_raw"]),
             "{:,}".format(ds["n_heartbeats"]), "{:,}".format(ds["n_valid"]),
             len(ds["records"]),
             ", ".join("%s%s" %
                       (d, " (ARCHIVE-SEALED/CAPTURE-UNASSESSED)"
                        if d in ds["archived_days"] else " (PARTIAL)")
                       for d in ds["days"])))

    top_px, top_lo, changes, n_elig = rankings(ds["records"],
                                               args.min_updates, args.top)
    for space, top in (("PRICE space (wiggle, cents^2)", top_px),
                       ("LOG-ODDS space (wiggle, logit^2)", top_lo)):
        print("\nTop %d market-days — %s" % (args.top, space))
        for i, r in enumerate(top):
            key = "wiggle_px" if "PRICE" in space else "wiggle_lo"
            print("  %2d. %-38s %s%s  %-12s wiggle=%10.3f  K=%10.3f  rev=%s  upd=%d"
                  % (i + 1, r["market_ticker"], r["day"],
                     "*" if r["partial"] else " ", r["category"][:12],
                     r[key], r["K_px" if "PRICE" in space else "K_lo"],
                     "%.2f" % r["reversal_rate"] if r["reversal_rate"] is not None else "-",
                     r["n_updates"]))
    print("\nRank changes (union of both top-%d):" % args.top)
    for c in changes:
        r = c["rec"]
        print("  %-38s %s  px#%-4d lo#%-4d delta %+d"
              % (r["market_ticker"], r["day"], c["rank_px"], c["rank_lo"],
                 c["delta"]))

    out = args.out or os.path.join(wc.ROOT, "work", "mm",
                                   "research_%s.html" % today)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(build_html(ds, top_px, top_lo, changes, n_elig, args, fees))
    csv_out = os.path.join(os.path.dirname(out),
                           "research_metrics_%s.csv" % today)
    if ds["records"]:
        with open(csv_out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(ds["records"][0].keys()))
            w.writeheader()
            w.writerows(ds["records"])
    print("\npage: %s\ncsv:  %s" % (out, csv_out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
