#!/usr/bin/env python3
"""Event Intelligence Dashboard — artifact builder.

Builds per-episode, decision-grade JSON artifacts from the LOCAL warehouse
archive (read-only) and renders an offline dashboard:

    sandbox/research/reports/event_intel/
        index.html                 dashboard (offline; file:// or served)
        assets/echarts.min.js      repo-vendored copy (no CDN)
        data/index.json|.js        episode index + channel inventory
        data/episodes/<key>.json|.js   one bounded artifact per episode
        data/heatmaps/<ep>__<mkt>.json|.js   per-market depth-heatmap artifact
            (v2 primary view: time x price resting-depth matrix from REAL
            orderbooks_full snapshot+delta replay, or the honest degraded
            L1 touch-band when no full-book capture exists — never fake depth)

Episode identity (match-level) uses, in priority order:
  1. catalog event relationships (markets -> event_ticker -> catalog titles)
  2. normalized catalog metadata (team-pair sub_titles for consistency checks)
  3. documented deterministic key derivation from the Kalshi ticker convention
     event_ticker = {series}-{YY}{MON}{DD}[{HHMM}]{TEAMS}; the episode key is
     {YY}{MON}{DD}:{TEAMS} (start-time digits stripped) so every series about
     the same match (GAME/SPREAD/TOTAL/SCORE/...) lands in one episode.
Grouping is NEVER by market_ticker alone.

Honesty rules baked into every artifact:
  - duplicate trade_id rows collapse; trade_ids with conflicting bodies are
    EXCLUDED and counted (never silently merged);
  - L1 touch changes are AGGREGATE proxies, never "orders" or actors;
  - descriptive percentiles are full-window (look-ahead) and labeled;
    causal tiers use only strictly-prior-hour cohorts with minimum sizes;
  - score / RFQ channels absent locally -> explicit empty-state contracts;
  - pre-TL1 archive rows carry exchange/coarse ts_utc only (flagged).

stdlib + duckdb only. Read-only against work/; writes only under
sandbox/research/reports/event_intel/.
"""

from __future__ import annotations

import argparse
import array
import base64
import json
import math
import os
import re
import shutil
import sys
from bisect import bisect_left, bisect_right, insort
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKBENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = WORKBENCH_DIR.parents[2]
ECHARTS_PATH = REPO_ROOT / "docs" / "vendor" / "js" / "echarts.min.js"
TEMPLATE_PATH = WORKBENCH_DIR / "intel.html"

INDEX_SCHEMA = "event-intel-index-v1"
EPISODE_SCHEMA = "event-intel-episode-v1"
HEATMAP_SCHEMA = "event-intel-heatmap-v1"

# Depth-heatmap matrix bounds (operator spec: <= ~99 x ~2000 cells/market).
HEATMAP_PRICE_ROWS = 99          # whole-cent rows 1..99
HEATMAP_MAX_BINS = 2000
HEATMAP_MIN_BIN_US = 1_000_000   # never finer than 1 second
HEATMAP_TRADE_CAP = 5000
_UINT32_MAX = 0xFFFFFFFF

SPORTS = ("Soccer", "Baseball", "Tennis", "Basketball")
EPISODES_PER_SPORT = 5
MARKETS_PER_EPISODE = 6
MARKERS_PER_TYPE_PER_MARKET = 400

# Threshold governance (operator spec): a percentile label may only be used
# when the cohort that produced it is at least this large.
THRESHOLD_TIERS = [
    {"label": "p99", "q": 0.99, "min_n": 500},
    {"label": "p99.5", "q": 0.995, "min_n": 1000},
    {"label": "p99.9", "q": 0.999, "min_n": 5000},
]

# Heartbeats are guaranteed hourly per active market; silence beyond this is a
# capture gap, not a quiet market.
GAP_THRESHOLD_US = 65 * 60 * 1_000_000

MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
_EPISODE_RE = re.compile(
    r"^(\d{2})(" + MONTHS + r")(\d{2})(\d{4})?([A-Z][A-Z0-9]{3,})$"
)

TRADE_CSV_TYPES = (
    "{'ts_utc':'BIGINT','market_ticker':'VARCHAR','series_ticker':'VARCHAR',"
    "'event_ticker':'VARCHAR','category':'VARCHAR','subcategory':'VARCHAR',"
    "'group':'VARCHAR','trade_id':'VARCHAR','yes_price_e4':'INTEGER',"
    "'no_price_e4':'INTEGER','count_e4':'BIGINT','taker_side':'VARCHAR'}"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(us: int | None) -> str | None:
    if us is None:
        return None
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _num(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    number = float(value)
    return number if math.isfinite(number) else None


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_files(paths: list[Path]) -> str:
    return "[" + ",".join(_sql_str(str(p.resolve())) for p in paths) + "]"


def episode_key(event_ticker: str) -> tuple[str | None, str | None]:
    """Deterministic match-episode key from a Kalshi event ticker.

    Returns (key, start_hint_hhmm). key is None when the ticker is not
    match-shaped (season-long / index events), in which case the event does
    not belong to a match episode.
    Example: KXWCGAME-26JUL06USABEL   -> ("26JUL06:USABEL", None)
             KXMLBTOTAL-26JUL052130BOSLAA -> ("26JUL05:BOSLAA", "2130")
    """
    if not event_ticker or "-" not in event_ticker:
        return None, None
    event_id = event_ticker.split("-", 1)[1]
    match = _EPISODE_RE.match(event_id)
    if not match:
        return None, None
    yy, mon, dd, hhmm, teams = match.groups()
    return f"{yy}{mon}{dd}:{teams}", hhmm


def safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)


def _utc_dates_between(t0_us: int, t1_us: int) -> list[str]:
    """ISO UTC dates spanned by a [t0_us, t1_us] microsecond window."""
    out = []
    day = datetime.fromtimestamp(t0_us / 1e6, tz=timezone.utc).date()
    last = datetime.fromtimestamp(t1_us / 1e6, tz=timezone.utc).date()
    while day <= last:
        out.append(day.isoformat())
        day = day.fromordinal(day.toordinal() + 1)
    return out


# ---------------------------------------------------------------------------
# Channel inventory
# ---------------------------------------------------------------------------

def build_inventory(repo_root: Path, data_root: Path | None = None,
                    tl1_status: str | None = None) -> dict[str, Any]:
    """Channel inventory over the local warehouse or an explicit --data-root
    (PIPE-W05: e.g. the verified research-cache view with facts/, catalog/,
    seals/, raw/). RFQ availability is DATA-DRIVEN: present when rfq raw
    exists under the data root for a date, honest empty state otherwise."""
    base = data_root if data_root is not None \
        else repo_root / "work" / "warehouse"
    facts = base / "facts"
    catalog = base / "catalog"
    seals = base / "seals"
    raw_root = (data_root / "raw") if data_root is not None \
        else repo_root / "work" / "raw"

    def _dates(pattern: str) -> list[str]:
        out = set()
        for p in facts.glob(pattern):
            m = re.search(r"date=(\d{4}-\d{2}-\d{2})", str(p))
            if m:
                out.add(m.group(1))
        return sorted(out)

    trades_dates = _dates("trades/category=Sports/subcategory=*/date=*/*.csv.gz")
    l1_dates = _dates("orderbooks_l1/category=Sports/subcategory=*/date=*/*.parquet")
    l2_files = sorted(facts.glob(
        "orderbooks_full/category=Sports/subcategory=*/date=*/*.parquet"))
    l2_dates = _dates("orderbooks_full/category=Sports/subcategory=*/date=*/*.parquet")
    rfq_raw = sorted(raw_root.glob("date=*/rfq_*.ndjson*")) \
        if raw_root.is_dir() else []
    rfq_dates = sorted({m.group(1) for p in rfq_raw
                        for m in [re.search(r"date=(\d{4}-\d{2}-\d{2})",
                                            str(p))] if m})
    seal_files = sorted(seals.glob("*")) if seals.is_dir() else []

    def chan(status: str, note: str, dates: list[str] | None = None,
             extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"status": status, "note": note, "dates": dates or []}
        if extra:
            payload.update(extra)
        return payload

    return {
        "trades": chan(
            "REAL" if trades_dates else "UNAVAILABLE",
            "archived trade prints (csv.gz); duplicate IDs collapsed, "
            "conflicting bodies excluded", trades_dates),
        "orderbooks_l1": chan(
            "REAL_PROXY" if l1_dates else "UNAVAILABLE",
            "change-only best bid/ask states; AGGREGATE L1 PROXY, "
            "not individual orders", l1_dates),
        "orderbooks_full": chan(
            "REAL" if l2_dates else "UNAVAILABLE",
            "watchlist-only full depth (snapshot+delta); local sample covers "
            "a small market set", l2_dates,
            {"markets_hint": len(l2_files)}),
        "rfq": chan(
            "RAW_PRESENT" if rfq_dates else "UNAVAILABLE",
            "sealed rfq raw present under the data root (verified research "
            "release); per-episode rendering still requires the "
            "event-intel-rfq-input-v1 adapter input"
            if rfq_dates else
            "no rfq raw under this data root for these dates; honest "
            "empty state",
            rfq_dates,
            {"files": len(rfq_raw)}),
        "score_game_state": chan(
            "UNAVAILABLE",
            "no score/game-state payload captured for this archive; "
            "prospective capture contract documented in the artifact"),
        "catalog": chan(
            "REAL" if (catalog / "events" / "part-00000.parquet").is_file()
            else "UNAVAILABLE",
            "series/events/markets dim tables (titles, event identity)"),
        "seals": chan(
            "ABSENT" if not seal_files else "PRESENT",
            "no local day seals for these legacy archive days; gap evidence "
            "is inferred from the hourly-heartbeat contract instead"),
        "timestamp_ladder": chan(
            tl1_status or "PRE-TL1",
            "all four W-TL1 ladder columns present in the archived facts "
            "(receive/decision clocks usable)"
            if tl1_status == "TL1" else
            "mix of TL1 and pre-TL1 days under this data root; per-day "
            "status lives in each release manifest"
            if tl1_status == "MIXED" else
            "archived days predate W-TL1: only ts_utc (exchange-or-coarse "
            "time) exists; no receive/decision clocks"),
    }


# ---------------------------------------------------------------------------
# Distribution helpers
# ---------------------------------------------------------------------------

def _distribution(values: list[float], unit: str, population: str,
                  provenance: str) -> dict[str, Any]:
    """No scalar stands alone: n, p50, p99, max, ECDF and log histogram."""
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    n = len(values)
    if n == 0:
        return {"n": 0, "unit": unit, "population": population,
                "provenance": provenance, "p50": None, "p99": None,
                "max": None, "ecdf": [], "hist": [], "notes": ["empty cohort"]}

    def q(p: float) -> float:
        if n == 1:
            return values[0]
        pos = p * (n - 1)
        lo = math.floor(pos)
        hi = min(lo + 1, n - 1)
        w = pos - lo
        return values[lo] * (1 - w) + values[hi] * w

    ecdf = [[_num(q(i / 40)), round(i / 40, 4)] for i in range(41)]
    lo_v, hi_v = values[0], values[-1]
    hist = []
    if hi_v > lo_v > 0:
        log_lo, log_hi = math.log10(max(lo_v, 1e-4)), math.log10(hi_v)
        nb = 24
        edges = [10 ** (log_lo + (log_hi - log_lo) * i / nb) for i in range(nb + 1)]
        for i in range(nb):
            lo_e, hi_e = edges[i], edges[i + 1]
            cnt = bisect_right(values, hi_e) - bisect_left(values, lo_e if i else -1e18)
            hist.append([_num(lo_e), _num(hi_e), cnt])
    elif hi_v == lo_v:
        hist = [[_num(lo_v), _num(hi_v), n]]
    notes = []
    p50v, p99v = q(0.5), q(0.99)
    if p50v and hi_v / max(p50v, 1e-9) > 50:
        notes.append("long right tail: max is %.0fx the median" % (hi_v / p50v))
    # crude mass-point check: most common value share
    if n >= 20:
        best = 0
        i = 0
        while i < n:
            j = bisect_right(values, values[i])
            best = max(best, j - i)
            i = j
        if best / n >= 0.25:
            notes.append("mass point: %.0f%% of observations share one value"
                         % (100 * best / n))
    if n < 500:
        notes.append("cohort below p99 minimum (n<500): tail labels refused "
                     "in causal mode")
    return {"n": n, "unit": unit, "population": population,
            "provenance": provenance, "p50": _num(p50v), "p99": _num(p99v),
            "max": _num(hi_v), "ecdf": ecdf, "hist": hist, "notes": notes}


# ---------------------------------------------------------------------------
# Causal (no-look-ahead) thresholds: expanding hourly frozen cohorts
# ---------------------------------------------------------------------------

def causal_annotate(events: list[dict[str, Any]], sizes_by_hour_source:
                    list[tuple[int, float]]) -> None:
    """Annotate events with causal cohort info.

    The threshold for an event at time t uses ONLY observations from strictly
    earlier UTC hours (frozen at each hour boundary — no intra-hour leakage,
    no look-ahead). Adds causal_n, causal_tier (highest tier passed with an
    adequate cohort, else None) and causal_thr (that tier's threshold).
    """
    source = sorted(sizes_by_hour_source)
    events_sorted = sorted(range(len(events)), key=lambda i: events[i]["t"])
    acc: list[float] = []
    src_i = 0
    frozen_hour = None
    thr: dict[str, float | None] = {t["label"]: None for t in THRESHOLD_TIERS}

    def freeze() -> None:
        for tier in THRESHOLD_TIERS:
            if len(acc) >= tier["min_n"]:
                pos = tier["q"] * (len(acc) - 1)
                lo = math.floor(pos)
                hi = min(lo + 1, len(acc) - 1)
                w = pos - lo
                thr[tier["label"]] = acc[lo] * (1 - w) + acc[hi] * w
            else:
                thr[tier["label"]] = None

    for idx in events_sorted:
        ev = events[idx]
        hour = (ev["t"] * 1000) // 3_600_000_000  # ms -> hour bucket
        if hour != frozen_hour:
            while src_i < len(source) and \
                    (source[src_i][0] // 3_600_000_000) < hour:
                insort(acc, source[src_i][1])
                src_i += 1
            freeze()
            frozen_hour = hour
        ev["c_n"] = len(acc)
        tier_hit, thr_hit = None, None
        for tier in THRESHOLD_TIERS:
            t_val = thr[tier["label"]]
            if t_val is not None and ev["contracts"] >= t_val:
                tier_hit, thr_hit = tier["label"], t_val
        ev["c_tier"] = tier_hit
        ev["c_thr"] = _num(thr_hit)


# ---------------------------------------------------------------------------
# Score / RFQ input contracts (adapters; render real data only)
# ---------------------------------------------------------------------------

SCORE_INPUT_SCHEMA = "event-intel-score-input-v1"
RFQ_INPUT_SCHEMA = "event-intel-rfq-input-v1"


def load_score_events(inputs_dir: Path, key: str) -> dict[str, Any]:
    """Score/game-state input adapter.

    Contract (one JSON per episode at inputs/score_events/<safe_key>.json):
      {"schema_version": "event-intel-score-input-v1",
       "episode_key": "...", "provider": "...",
       "capture": {"recv_wall_ns": true, "recv_mono_ns": true},
       "events": [{"provider_ts_utc": iso, "recv_wall_ns": int|null,
                   "recv_mono_ns": int|null, "kind": "score|period|clock",
                   "payload": {...raw...}, "mapping_confidence": 0..1,
                   "correction_of": id|null, "post_hoc": bool}]}
    Absent file -> honest empty state. No fabricated markers, ever.
    """
    path = inputs_dir / "score_events" / f"{safe_key(key)}.json"
    if not path.is_file():
        return {
            "status": "SCORE/GAME STATE NOT CAPTURED FOR THIS ARCHIVE",
            "available": False, "events": [],
            "input_contract": SCORE_INPUT_SCHEMA,
            "input_path": str(path.relative_to(inputs_dir.parent)),
            "prospective_capture": {
                "endpoints": [
                    "GET /events?with_milestones=true",
                    "GET /live_data/batch?milestone_ids=...",
                    "GET /live_data/milestone/{milestone_id} (game stats)",
                ],
                "mapping": "milestones.related_event_tickers / "
                           "primary_event_tickers -> episode events",
                "must_record": [
                    "raw payload byte-exact", "recv_wall_ns", "recv_mono_ns",
                    "mapping + confidence", "corrections chain",
                    "gap accounting"],
                "note": "game stats cover several majors (pro baseball, "
                        "basketball/WNBA, soccer, football, hockey); tennis "
                        "is NOT assumed. Post-hoc backfill must be labeled "
                        "POST-HOC.",
            },
        }
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != SCORE_INPUT_SCHEMA:
        return {"status": "SCORE INPUT REJECTED: wrong schema_version",
                "available": False, "events": [],
                "input_contract": SCORE_INPUT_SCHEMA}
    events = []
    for ev in payload.get("events", []):
        if not isinstance(ev, dict) or not ev.get("payload") \
                or not ev.get("provider_ts_utc"):
            continue  # no marker without a real payload
        events.append(ev)
    return {"status": "REAL SCORE EVENTS (%d)" % len(events),
            "available": bool(events), "events": events,
            "provider": payload.get("provider"),
            "post_hoc": any(ev.get("post_hoc") for ev in events),
            "input_contract": SCORE_INPUT_SCHEMA}


def load_rfq_events(inputs_dir: Path, key: str,
                    rfq_raw_dates: frozenset[str] = frozenset(),
                    episode_dates: frozenset[str] = frozenset()
                    ) -> dict[str, Any]:
    """RFQ input adapter (rfq_created/rfq_deleted, requester as static hash
    only). Availability is DATA-DRIVEN (PIPE-W05): when verified research
    rfq raw exists under the data root for the episode's dates, the empty
    state says so; dates without RFQ keep the honest empty state."""
    path = inputs_dir / "rfq" / f"{safe_key(key)}.json"
    if not path.is_file():
        overlap = sorted(set(rfq_raw_dates) & set(episode_dates))
        if overlap:
            status = ("RFQ RAW PRESENT for %s (verified research release) — "
                      "no %s adapter input built; events not rendered"
                      % (", ".join(overlap), RFQ_INPUT_SCHEMA))
        else:
            status = "RFQ NOT CAPTURED FOR THESE DATES (no rfq raw under " \
                     "this data root)"
        return {"status": status,
                "available": False, "events": [],
                "raw_dates_present": overlap,
                "input_contract": RFQ_INPUT_SCHEMA,
                "input_path": str(path.relative_to(inputs_dir.parent))}
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != RFQ_INPUT_SCHEMA:
        return {"status": "RFQ INPUT REJECTED: wrong schema_version",
                "available": False, "events": [],
                "input_contract": RFQ_INPUT_SCHEMA}
    events = [ev for ev in payload.get("events", [])
              if isinstance(ev, dict) and ev.get("kind") in
              ("rfq_created", "rfq_deleted") and ev.get("ts_utc")]
    return {"status": "REAL RFQ EVENTS (%d)" % len(events),
            "available": bool(events), "events": events,
            "input_contract": RFQ_INPUT_SCHEMA}


# ---------------------------------------------------------------------------
# Depth heatmap (v2 primary view) — pure functions, independently testable
# ---------------------------------------------------------------------------

def heatmap_bins(t0_us: int, t1_us: int,
                 max_bins: int = HEATMAP_MAX_BINS,
                 min_bin_us: int = HEATMAP_MIN_BIN_US) -> tuple[int, int]:
    """Adaptive time binning: bin i covers [t0+i*bin, t0+(i+1)*bin) and the
    matrix stays <= max_bins columns. Returns (bin_us, n_bins)."""
    span = max(0, int(t1_us) - int(t0_us))
    bin_us = max(min_bin_us, span // max(1, max_bins - 1) + 1)
    return bin_us, span // bin_us + 1


def _b64_u32(values: list[int]) -> str:
    """Little-endian uint32 array as base64 (typed-array friendly)."""
    arr = array.array("I", values)
    if arr.itemsize != 4:  # pragma: no cover - platform oddity guard
        raise RuntimeError("no 4-byte unsigned array type on this platform")
    if sys.byteorder != "little":  # pragma: no cover
        arr.byteswap()
    return base64.b64encode(arr.tobytes()).decode("ascii")


def build_l2_heatmap(mrows: list) -> dict[str, Any]:
    """Full-book depth matrix from REAL orderbooks_full snapshot+delta rows.

    mrows: rows shaped like _build_l2's fetch —
      (market_ticker, ts_utc, msg_type, side, price_e4, delta_e4,
       yes_levels, no_levels, ws_seq) — already sorted by ts_utc.

    Definition (documented in the artifact and mirrored by the tests):
      - price rows are whole cents 1..99; a level's cent row is
        floor(price_e4/100) — sub-penny levels aggregate into their cent row
        (E4 sub-penny is real; it is BINNED for the heat grid, never rounded
        up). YES-side resting depth maps directly; NO-side resting depth is
        displayed at the equivalent YES ask price (10000 - price_e4).
      - cell value = resting displayed depth (E4 qty) at that price row at
        BIN CLOSE: the book state after replaying every row with
        ts < t0 + (i+1)*bin_us. Delta replay matches the v1 tape rules
        (snapshot resets the book; a level whose qty reaches <= 0 is
        removed; a negative overshoot is clamped and counted, never hidden).
    """
    t0, t1 = int(mrows[0][1]), int(mrows[-1][1])
    bin_us, n_bins = heatmap_bins(t0, t1)
    rows_p = HEATMAP_PRICE_ROWS
    bid = [0] * (n_bins * rows_p)
    ask = [0] * (n_bins * rows_p)
    best_bid_e4 = [0] * n_bins
    best_ask_e4 = [0] * n_bins
    book: dict[str, dict[int, int]] = {"yes": {}, "no": {}}
    anomalies = {"negative_clamps": 0, "out_of_range_price_events": 0,
                 "uint32_clamped_cells": 0}
    level_depth_samples: list[float] = []   # per occupied exact level, per bin
    cur_bin = 0

    def flush_until(idx_end: int) -> None:
        nonlocal cur_bin
        idx_end = min(idx_end, n_bins)
        if idx_end <= cur_bin:
            return
        colb = [0] * rows_p
        cola = [0] * rows_p
        levels: list[float] = []
        bb = 0
        ba = 0
        for price, qty in book["yes"].items():
            cent = price // 100
            if 1 <= cent <= 99:
                colb[cent - 1] += qty
            levels.append(qty / 10000.0)
            if price > bb:
                bb = price
        for price, qty in book["no"].items():
            ask_px = 10000 - price
            cent = ask_px // 100
            if 1 <= cent <= 99:
                cola[cent - 1] += qty
            levels.append(qty / 10000.0)
            if ba == 0 or ask_px < ba:
                ba = ask_px
        for i, v in enumerate(colb):
            if v > _UINT32_MAX:
                colb[i] = _UINT32_MAX
                anomalies["uint32_clamped_cells"] += 1
        for i, v in enumerate(cola):
            if v > _UINT32_MAX:
                cola[i] = _UINT32_MAX
                anomalies["uint32_clamped_cells"] += 1
        for i in range(cur_bin, idx_end):
            base = i * rows_p
            bid[base:base + rows_p] = colb
            ask[base:base + rows_p] = cola
            best_bid_e4[i] = bb
            best_ask_e4[i] = ba
            level_depth_samples.extend(levels)
        cur_bin = idx_end

    for row in mrows:
        ts, msg_type, side, price_e4, delta_e4 = \
            int(row[1]), row[2], row[3], row[4], row[5]
        flush_until(int((ts - t0) // bin_us))
        if msg_type == "snapshot":
            book = {"yes": {}, "no": {}}
            for side_name, levels_json in (("yes", row[6]), ("no", row[7])):
                for price, qty in json.loads(levels_json or "[]"):
                    if qty > 0:
                        book[side_name][int(price)] = int(qty)
                        cent = (int(price) if side_name == "yes"
                                else 10000 - int(price)) // 100
                        if not 1 <= cent <= 99:
                            anomalies["out_of_range_price_events"] += 1
        elif msg_type == "delta" and side in ("yes", "no"):
            price = int(price_e4)
            cent = (price if side == "yes" else 10000 - price) // 100
            if not 1 <= cent <= 99:
                anomalies["out_of_range_price_events"] += 1
            prev = book[side].get(price, 0)
            new = prev + int(delta_e4)
            if new < 0:
                anomalies["negative_clamps"] += 1
            if new <= 0:
                book[side].pop(price, None)
            else:
                book[side][price] = new
    flush_until(n_bins)

    nonzero_bid = [v / 10000.0 for v in bid if v > 0]
    nonzero_ask = [v / 10000.0 for v in ask if v > 0]
    max_cell = max(max(bid, default=0), max(ask, default=0))
    return {
        "kind": "l2_full",
        "t0_us": t0, "t1_us": t1, "bin_us": bin_us, "n_bins": n_bins,
        "price_rows": rows_p,
        "bid_b64": _b64_u32(bid), "ask_b64": _b64_u32(ask),
        "best_bid_e4": best_bid_e4, "best_ask_e4": best_ask_e4,
        "max_cell_e4": max_cell,
        "nonzero_cells": len(nonzero_bid) + len(nonzero_ask),
        "anomalies": anomalies,
        "dist": {
            "heat_cell_depth_bid": _distribution(
                nonzero_bid, "contracts",
                "nonzero BID heat cells (cent row x time bin, bin-close "
                "resting depth)", "orderbooks_full snapshot+delta replay"),
            "heat_cell_depth_ask": _distribution(
                nonzero_ask, "contracts",
                "nonzero ASK heat cells (cent row x time bin, bin-close "
                "resting depth)", "orderbooks_full snapshot+delta replay"),
            "depth_per_level": _distribution(
                level_depth_samples, "contracts",
                "resting depth per occupied EXACT price level (both sides), "
                "sampled at every bin close",
                "orderbooks_full snapshot+delta replay"),
        },
        "definition": {
            "price_binning": "whole-cent rows 1..99; cent = "
                             "floor(price_e4/100) — sub-penny levels are "
                             "REAL and are aggregated into their cent row; "
                             "NO-side depth shown at YES-ask price "
                             "(10000 - price_e4)",
            "sampling": "cell = resting displayed depth at BIN CLOSE "
                        "(book state after all rows with ts < bin end)",
            "replay": "snapshot resets book; delta adds; level removed at "
                      "qty <= 0; negative overshoot clamped AND counted",
            "encoding": "uint32 little-endian E4 quantities, base64, "
                        "row-major [bin][cent-1]",
        },
    }


def build_l1_touch_heatmap(l1_rows: list, t0_us: int, t1_us: int,
                           gaps_ms: list[list[int]]) -> dict[str, Any]:
    """Honest degraded heatmap for markets WITHOUT full-book capture:
    touch-band heat only (best bid / best ask displayed qty), everything
    else transparent. NEVER synthesizes depth away from the touch.

    l1_rows: (ts_utc, yes_bid_e4, yes_bid_qty_e4, yes_ask_e4, yes_ask_qty_e4)
    sorted by ts_utc (rows before t0 seed the carried state). A bin has a
    value only when the last L1 row is within the 65-min heartbeat contract;
    beyond that the bin is empty and the range is a DATA GAP.
    """
    bin_us, n_bins = heatmap_bins(t0_us, t1_us)
    bid_price_e4 = [0] * n_bins
    bid_qty_e4 = [0] * n_bins
    ask_price_e4 = [0] * n_bins
    ask_qty_e4 = [0] * n_bins
    bid_vals: list[float] = []
    ask_vals: list[float] = []
    idx = 0
    last = None
    for i in range(n_bins):
        close = t0_us + (i + 1) * bin_us
        while idx < len(l1_rows) and int(l1_rows[idx][0]) < close:
            last = l1_rows[idx]
            idx += 1
        if last is None or close - int(last[0]) > GAP_THRESHOLD_US:
            continue  # no trusted book state (pre-capture or DATA GAP)
        _, b_px, b_q, a_px, a_q = last
        if b_px and b_px > 0 and b_q and b_q > 0:
            bid_price_e4[i] = int(b_px)
            bid_qty_e4[i] = min(int(b_q), _UINT32_MAX)
            bid_vals.append(b_q / 10000.0)
        if a_px and 0 < a_px < 10000 and a_q and a_q > 0:
            ask_price_e4[i] = int(a_px)
            ask_qty_e4[i] = min(int(a_q), _UINT32_MAX)
            ask_vals.append(a_q / 10000.0)
    return {
        "kind": "l1_touch",
        "t0_us": int(t0_us), "t1_us": int(t1_us),
        "bin_us": bin_us, "n_bins": n_bins,
        "price_rows": HEATMAP_PRICE_ROWS,
        "touch": {
            "bid_price_e4": bid_price_e4, "bid_qty_e4": bid_qty_e4,
            "ask_price_e4": ask_price_e4, "ask_qty_e4": ask_qty_e4,
        },
        "gaps_ms": [[int(a), int(b)] for a, b in (gaps_ms or [])],
        "dist": {
            "touch_depth_bid": _distribution(
                bid_vals, "contracts",
                "displayed qty at best bid per time bin (AGGREGATE L1 "
                "PROXY, not full book)", "orderbooks_l1 change rows (LOCF "
                "within the 65-min heartbeat contract)"),
            "touch_depth_ask": _distribution(
                ask_vals, "contracts",
                "displayed qty at best ask per time bin (AGGREGATE L1 "
                "PROXY, not full book)", "orderbooks_l1 change rows (LOCF "
                "within the 65-min heartbeat contract)"),
        },
        "definition": {
            "degraded": "AGGREGATE L1 PROXY — touch depth only; NOT full "
                        "book. Heat exists ONLY on the best-bid/best-ask "
                        "price rows; no other depth is known, none is drawn.",
            "sampling": "bin value = last L1 state with ts < bin close, "
                        "carried at most 65 min (the heartbeat contract); "
                        "silence beyond that renders as DATA GAP",
        },
    }


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

class EventIntelBuilder:
    def __init__(self, repo_root: Path = REPO_ROOT,
                 out_dir: Path | None = None,
                 sports: tuple[str, ...] = SPORTS,
                 episodes_per_sport: int = EPISODES_PER_SPORT,
                 markets_per_episode: int = MARKETS_PER_EPISODE,
                 data_root: Path | None = None):
        self.repo_root = Path(repo_root)
        # PIPE-W05: an explicit data root (e.g. the verified research-cache
        # view work/research_cache/view with facts/, catalog/, seals/, raw/)
        # replaces the local warehouse; default behavior is unchanged.
        self.data_root = Path(data_root) if data_root is not None else None
        base = self.data_root if self.data_root is not None \
            else self.repo_root / "work" / "warehouse"
        self.out_dir = out_dir or (
            self.repo_root / "sandbox" / "research" / "reports" / "event_intel")
        self.facts = base / "facts"
        self.catalog = base / "catalog"
        self.seals_dir = base / "seals"
        self.raw_root = (self.data_root / "raw") if self.data_root is not None \
            else self.repo_root / "work" / "raw"
        self.sports = sports
        self.episodes_per_sport = episodes_per_sport
        self.markets_per_episode = markets_per_episode
        self.generated_at = _utc_now()
        import duckdb
        self.con = duckdb.connect()
        self.con.execute("SET threads=4")
        # W05 HYGIENE: explicit DuckDB memory limit on every connection
        self.con.execute("SET memory_limit='8GB'")
        self._event_meta: dict[str, tuple[str, str]] | None = None
        self._tl1_status: str | None = None
        self._rfq_dates: frozenset[str] | None = None
        self._evidence: dict[str, Any] | None = None

    # -- file discovery -----------------------------------------------------
    def _trade_files(self, sport: str) -> list[Path]:
        return sorted((self.facts / "trades" / "category=Sports" /
                       f"subcategory={sport}").glob("date=*/*.csv.gz"))

    def _l1_files(self, sport: str) -> list[Path]:
        return sorted((self.facts / "orderbooks_l1" / "category=Sports" /
                       f"subcategory={sport}").glob("date=*/*.parquet"))

    def _l2_files(self) -> list[Path]:
        return sorted((self.facts / "orderbooks_full" / "category=Sports")
                      .glob("subcategory=*/date=*/*.parquet"))

    # -- data-truth detection (PIPE-W05: measured, never asserted) -----------
    LADDER_COLUMNS = ("exchange_ts_us", "recv_wall_ns", "recv_mono_ns",
                      "local_recv_ts_us")

    def tl1_status(self) -> str:
        """TL1 vs PRE-TL1 measured from the archived L1 schemas (metadata
        only). TL1 = all four W-TL1 ladder columns in every sampled file;
        MIXED = both kinds of day under this data root."""
        if self._tl1_status is None:
            kinds = set()
            for sport in self.sports:
                for f in self._l1_files(sport)[:64]:
                    cols = {r[0] for r in self.con.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)",
                        [str(f)]).fetchall()}
                    kinds.add(all(c in cols for c in self.LADDER_COLUMNS))
            self._tl1_status = ("TL1" if kinds == {True}
                                else "MIXED" if kinds == {True, False}
                                else "PRE-TL1")
        return self._tl1_status

    def rfq_raw_dates(self) -> frozenset[str]:
        """UTC dates with rfq raw under the data root (empty locally)."""
        if self._rfq_dates is None:
            found = set()
            if self.raw_root.is_dir():
                for p in self.raw_root.glob("date=*/rfq_*.ndjson*"):
                    m = re.search(r"date=(\d{4}-\d{2}-\d{2})", str(p))
                    if m:
                        found.add(m.group(1))
            self._rfq_dates = frozenset(found)
        return self._rfq_dates

    def evidence_summary(self) -> dict[str, Any]:
        """Evidence tier + archive source — DERIVED from the day seals under
        the data root, never asserted (operator correction order fix 5/7).
        No seals => honest EXPLORATORY_UNSEALED_LEGACY; degraded seals keep
        their downgrade reasons visible."""
        if self._evidence is not None:
            return self._evidence
        seal_files = sorted(self.seals_dir.glob("date=*.json")) \
            if self.seals_dir.is_dir() else []
        source = (str(self.data_root) if self.data_root is not None
                  else "local legacy warehouse (work/warehouse)")
        label = ("RESEARCH DATA ROOT" if self.data_root is not None
                 else "LOCAL ARCHIVE")
        if not seal_files:
            self._evidence = {
                "tier": "EXPLORATORY_UNSEALED_LEGACY",
                "basis": ["no day seals under this data root — evidence "
                          "tier cannot exceed exploratory"],
                "archive_source": source,
                "archive_source_label": label,
                "seals": 0,
            }
            return self._evidence
        reasons: list[str] = []
        n_ok = 0
        for p in seal_files:
            try:
                seal = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                reasons.append("%s unreadable/unparsable" % p.name)
                continue
            if not (seal.get("status") == "SEALED"
                    and seal.get("version") == 2):
                reasons.append("%s is not a SEALED v2 seal" % p.name)
                continue
            n_ok += 1
            if seal.get("method") != "full_v2":
                reasons.append("%s method=%r" % (p.name, seal.get("method")))
            if seal.get("go_no_go_eligible") is not True:
                reasons.append("%s not go_no_go_eligible" % p.name)
        tier = ("SEALED_CONFIRMATION"
                if n_ok == len(seal_files) and not reasons
                else "SEALED_DEGRADED_EVIDENCE")
        self._evidence = {
            "tier": tier,
            "basis": reasons or ["derived from %d SEALED v2 day seal(s)"
                                 % n_ok],
            "archive_source": source,
            "archive_source_label": label,
            "seals": len(seal_files),
        }
        return self._evidence

    # -- catalog ------------------------------------------------------------
    def event_meta(self) -> dict[str, tuple[str, str]]:
        if self._event_meta is not None:
            return self._event_meta
        meta: dict[str, tuple[str, str]] = {}
        events_pq = self.catalog / "events" / "part-00000.parquet"
        if events_pq.is_file():
            rows = self.con.sql(
                "SELECT event_ticker, any_value(title), any_value(sub_title) "
                f"FROM read_parquet({_sql_str(str(events_pq))}) "
                "GROUP BY event_ticker").fetchall()
            meta = {r[0]: (r[1] or "", r[2] or "") for r in rows}
        self._event_meta = meta
        return meta

    def market_meta(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        markets_pq = self.catalog / "markets" / "part-00000.parquet"
        if not markets_pq.is_file() or not tickers:
            return {}
        lst = ",".join(_sql_str(t) for t in tickers)
        rows = self.con.sql(
            "SELECT ticker, any_value(title), any_value(yes_sub_title), "
            "any_value(close_time) "
            f"FROM read_parquet({_sql_str(str(markets_pq))}) "
            f"WHERE ticker IN ({lst}) GROUP BY ticker").fetchall()
        return {r[0]: {"title": r[1], "yes_sub_title": r[2],
                       "close_time": str(r[3]) if r[3] else None}
                for r in rows}

    # -- trades (dedup + conflict exclusion) ---------------------------------
    def load_safe_trades(self, sport: str) -> dict[str, int]:
        """Create temp table safe_trades_<sport> and return quality counts."""
        files = self._trade_files(sport)
        table = f"safe_trades_{sport}"
        self.con.execute(f"DROP TABLE IF EXISTS {table}")
        if not files:
            self.con.execute(
                f"CREATE TEMP TABLE {table} (ts_utc BIGINT, market_ticker "
                "VARCHAR, event_ticker VARCHAR, trade_id VARCHAR, "
                "yes_price_e4 INTEGER, no_price_e4 INTEGER, count_e4 BIGINT, "
                "taker_side VARCHAR)")
            return {"raw_rows": 0, "safe_unique_trade_ids": 0,
                    "conflicting_trade_ids_excluded": 0}
        self.con.execute(f"""
            CREATE TEMP TABLE {table}_bodies AS
            SELECT trade_id,
                   any_value(ts_utc) AS ts_utc,
                   any_value(market_ticker) AS market_ticker,
                   any_value(event_ticker) AS event_ticker,
                   any_value(yes_price_e4) AS yes_price_e4,
                   any_value(no_price_e4) AS no_price_e4,
                   any_value(count_e4) AS count_e4,
                   any_value(taker_side) AS taker_side,
                   COUNT(*)::INTEGER AS raw_rows,
                   COUNT(DISTINCT hash(ts_utc, market_ticker, yes_price_e4,
                       no_price_e4, count_e4, taker_side))::INTEGER AS variants
            FROM read_csv({_sql_files(files)}, columns={TRADE_CSV_TYPES},
                          header=true, compression='gzip')
            GROUP BY trade_id""")
        raw, safe, conflicts, incomplete = self.con.execute(
            f"""SELECT COALESCE(SUM(raw_rows),0),
                       COUNT(*) FILTER (WHERE variants=1 AND ts_utc IS NOT
                           NULL AND count_e4 IS NOT NULL AND count_e4 > 0),
                       COUNT(*) FILTER (WHERE variants>1),
                       COUNT(*) FILTER (WHERE variants=1 AND (ts_utc IS NULL
                           OR count_e4 IS NULL OR count_e4 <= 0))
                FROM {table}_bodies""").fetchone()
        self.con.execute(f"""
            CREATE TEMP TABLE {table} AS
            SELECT ts_utc, market_ticker, event_ticker, trade_id,
                   yes_price_e4, no_price_e4, count_e4, taker_side
            FROM {table}_bodies
            WHERE variants=1 AND ts_utc IS NOT NULL
              AND count_e4 IS NOT NULL AND count_e4 > 0""")
        self.con.execute(f"DROP TABLE {table}_bodies")
        return {"raw_rows": int(raw), "safe_unique_trade_ids": int(safe),
                "conflicting_trade_ids_excluded": int(conflicts),
                "incomplete_rows_excluded": int(incomplete)}

    # -- episode index --------------------------------------------------------
    def build_episode_index(self) -> dict[str, Any]:
        meta = self.event_meta()
        sports_out = []
        self.trade_quality: dict[str, dict[str, int]] = {}
        self.episodes_by_sport: dict[str, list[dict[str, Any]]] = {}
        for sport in self.sports:
            quality = self.load_safe_trades(sport)
            self.trade_quality[sport] = quality
            rows = self.con.sql(f"""
                SELECT event_ticker, COUNT(*) n,
                       SUM(count_e4)/10000.0 vol,
                       COUNT(DISTINCT market_ticker) mkts,
                       MIN(ts_utc) t0, MAX(ts_utc) t1
                FROM safe_trades_{sport} GROUP BY 1""").fetchall()
            groups: dict[str, dict[str, Any]] = {}
            for event_ticker, n, vol, mkts, t0, t1 in rows:
                key, hhmm = episode_key(event_ticker)
                if key is None:
                    continue
                g = groups.setdefault(key, {
                    "key": key, "sport": sport, "events": [], "trades": 0,
                    "volume": 0.0, "markets": 0, "t0": t0, "t1": t1,
                    "start_hint": None})
                title, sub = meta.get(event_ticker, ("", ""))
                g["events"].append({"ticker": event_ticker, "title": title,
                                    "sub_title": sub})
                g["trades"] += int(n)
                g["volume"] += float(vol or 0)
                g["markets"] += int(mkts)
                g["t0"] = min(g["t0"], t0)
                g["t1"] = max(g["t1"], t1)
                if hhmm:
                    g["start_hint"] = g["start_hint"] or hhmm
            for g in groups.values():
                g["title"], g["sub"], g["title_evidence"] = \
                    self._episode_title(g)
            ranked = sorted(groups.values(),
                            key=lambda g: -g["volume"])[: self.episodes_per_sport]
            # Force-include the real-L2 watchlist episode when it has trades.
            if sport == "Baseball":
                for g in groups.values():
                    if g["key"].endswith(":BOSLAA") and g not in ranked:
                        ranked.append(g)
            self.episodes_by_sport[sport] = ranked
            sports_out.append({"sport": sport, "episodes": ranked,
                               "trade_quality": quality})
        return {"sports": sports_out}

    def _episode_title(self, group: dict[str, Any]) -> tuple[str, str, dict]:
        """Priority: catalog title of the head-to-head (GAME/MATCH) event,
        else the most common 'X vs Y' catalog title, else the ticker teams."""
        titled = [(e["ticker"], e["title"], e["sub_title"])
                  for e in group["events"] if e["title"]]
        game_like = [t for t in titled if re.search(
            r"(GAME|MATCH|MATCHUP)-", t[0])]
        vs_titles = [t for t in titled if " vs " in (t[1] or "").lower()
                     or " vs " in (t[1] or "")]
        pick = None
        method = "deterministic-suffix-only"
        if game_like:
            pick, method = game_like[0], "catalog-game-event-title"
        elif vs_titles:
            pick, method = vs_titles[0], "catalog-vs-title"
        elif titled:
            pick, method = titled[0], "catalog-any-title"
        if pick:
            title = re.sub(r":.*$", "", pick[1]).strip()
            sub = pick[2] or ""
        else:
            teams = group["key"].split(":", 1)[1]
            title = teams
            sub = "no catalog title; deterministic ticker-suffix identity"
        # consistency evidence: how many catalog sub_titles agree on team pair
        pairs = defaultdict(int)
        for _, _, sub_t in titled:
            m = re.search(r"([A-Za-z. ]+) (?:vs\.?|at) ([A-Za-z. ]+)", sub_t or "")
            if m:
                pairs[frozenset((m.group(1).strip(), m.group(2).strip()))] += 1
        consistent = max(pairs.values()) / max(1, sum(pairs.values())) \
            if pairs else None
        return title, sub, {
            "method": method,
            "catalog_events_with_titles": len(titled),
            "team_pair_consistency": _num(consistent),
            "rule": "episode = {series}-{YYMONDD}[{HHMM}]{TEAMS} suffix with "
                    "start-time digits stripped; grouped across all series "
                    "of the same match; never by market_ticker alone",
        }

    # -- per-episode artifact --------------------------------------------------
    def build_episode(self, group: dict[str, Any]) -> dict[str, Any]:
        sport = group["sport"]
        key = group["key"]
        event_tickers = [e["ticker"] for e in group["events"]]
        ev_list = ",".join(_sql_str(t) for t in event_tickers)

        market_rows = self.con.sql(f"""
            SELECT market_ticker, event_ticker, COUNT(*) n,
                   SUM(count_e4)/10000.0 vol
            FROM safe_trades_{sport}
            WHERE event_ticker IN ({ev_list})
            GROUP BY 1,2 ORDER BY vol DESC""").fetchall()
        detailed = market_rows[: self.markets_per_episode]
        other = market_rows[self.markets_per_episode:]
        tickers = [r[0] for r in detailed]
        mk_meta = self.market_meta(tickers)
        mk_list = ",".join(_sql_str(t) for t in tickers)

        l1_files = self._l1_files(sport)
        self.con.execute("DROP TABLE IF EXISTS ep_l1")
        if l1_files:
            self.con.execute(f"""
                CREATE TEMP TABLE ep_l1 AS
                SELECT ts_utc, market_ticker, yes_bid_e4, yes_bid_qty_e4,
                       yes_ask_e4, yes_ask_qty_e4, price_e4, is_snapshot
                FROM read_parquet({_sql_files(l1_files)})
                WHERE market_ticker IN ({mk_list})""")
        else:
            self.con.execute(
                "CREATE TEMP TABLE ep_l1 (ts_utc BIGINT, market_ticker "
                "VARCHAR, yes_bid_e4 INTEGER, yes_bid_qty_e4 BIGINT, "
                "yes_ask_e4 INTEGER, yes_ask_qty_e4 BIGINT, price_e4 "
                "INTEGER, is_snapshot BOOLEAN)")
        self.con.execute(f"""
            CREATE OR REPLACE TEMP TABLE ep_trades AS
            SELECT * FROM safe_trades_{sport}
            WHERE market_ticker IN ({mk_list})""")

        markets_payload = []
        tape: list[dict[str, Any]] = []
        gap_total = 0
        for ticker, event_ticker, n_trades, vol in detailed:
            payload, mk_tape, gaps_n = self._build_market(
                ticker, event_ticker, mk_meta.get(ticker, {}))
            payload.update({"n_trades": int(n_trades), "volume": _num(vol)})
            markets_payload.append(payload)
            tape.extend(mk_tape)
            gap_total += gaps_n

        tempo, regimes = self._build_tempo(key)
        l2_info = self._build_l2(tickers)
        for mk in markets_payload:
            if mk["ticker"] in l2_info:
                mk["l2"] = l2_info[mk["ticker"]]
                for marker in mk["l2"]["markers"]:
                    tape.append({**marker, "market": mk["ticker"]})
            else:
                mk["l2"] = {"available": False,
                            "note": "no full-depth capture for this market "
                                    "in the local archive"}
        self._attach_heatmaps(safe_key(key), group, markets_payload)

        tape.sort(key=lambda e: e["t"])
        inputs_dir = self.out_dir / "inputs"
        t0_ms, t1_ms = group["t0"] // 1000, group["t1"] // 1000

        return {
            "schema_version": EPISODE_SCHEMA,
            "generated_at_utc": self.generated_at,
            "key": key, "safe_key": safe_key(key), "sport": sport,
            "title": group["title"], "sub": group["sub"],
            "identity": {
                "episode_key": key,
                "derivation": group["title_evidence"],
                "events": group["events"],
            },
            "window": {"t0_ms": t0_ms, "t1_ms": t1_ms,
                       "t0_utc": _iso(group["t0"]), "t1_utc": _iso(group["t1"]),
                       "basis": "observed archived activity (trades+L1), "
                                "not scheduled times"},
            "time_note": (
                "TL1 — receive/decision ladder columns present in the "
                "archived facts (exchange timestamps stay ms-granular)"
                if self.tl1_status() == "TL1" else
                "MIXED TL1/PRE-TL1 days under this data root — per-day "
                "status in the release manifests; pre-TL1 rows carry "
                "exchange-or-coarse ts_utc only"
                if self.tl1_status() == "MIXED" else
                "PRE-TL1 / EXCHANGE-OR-COARSE TIME ONLY — ts_utc = "
                "COALESCE(exchange_ts, coarse receive); no "
                "recv_wall/recv_mono/decision clocks exist for "
                "these archive days"),
            "markets": markets_payload,
            "other_markets": [{"ticker": r[0], "event_ticker": r[1],
                               "n_trades": int(r[2]), "volume": _num(r[3])}
                              for r in other],
            "tempo": tempo,
            "regimes": regimes,
            "tape": tape,
            "evidence": self.evidence_summary(),
            "score": load_score_events(inputs_dir, key),
            "rfq": load_rfq_events(
                inputs_dir, key, self.rfq_raw_dates(),
                frozenset(_utc_dates_between(group["t0"], group["t1"]))),
            "thresholds": {
                "tiers": THRESHOLD_TIERS,
                "descriptive": "full-window percentile (cume_dist) — "
                               "LOOK-AHEAD / EXPLORATORY only",
                "causal": "expanding hourly frozen cohort: threshold at time "
                          "t uses only strictly-prior-hour observations; "
                          "labels refused when the cohort is too small",
            },
            "quality": {
                "trade_quality": self.trade_quality[sport],
                "l1_rows": int(self.con.sql(
                    "SELECT COUNT(*) FROM ep_l1").fetchone()[0]),
                "capture_gaps": gap_total,
                "gap_rule": "hourly heartbeats are guaranteed per active "
                            "market; >65 min without any L1 row = "
                            "DATA GAP / BOOK STATE UNTRUSTED",
                "seals": ("%d day seal(s) present under the data root"
                          % len(seal_files)
                          if (seal_files := sorted(
                              self.seals_dir.glob("date=*.json"))
                              if self.seals_dir.is_dir() else [])
                          else "no local day seals for these legacy days"),
                "timestamp_ladder": self.tl1_status(),
            },
        }

    def _build_market(self, ticker: str, event_ticker: str,
                      meta: dict[str, Any]) -> tuple[dict, list, int]:
        tk = _sql_str(ticker)
        # Track 1: minute price with intraminute extrema preserved (M4-ish)
        price_rows = self.con.sql(f"""
            WITH valid AS (
                SELECT ts_utc, (ts_utc//60000000)*60 AS minute_s,
                    CASE WHEN yes_bid_e4>0 THEN yes_bid_e4/100.0 END AS bid_c,
                    CASE WHEN yes_ask_e4<10000 AND yes_ask_e4>0
                         THEN yes_ask_e4/100.0 END AS ask_c,
                    CASE WHEN yes_bid_e4>0 AND yes_ask_e4<10000
                          AND yes_ask_e4>=yes_bid_e4
                         THEN (yes_bid_e4+yes_ask_e4)/200.0 END AS mid_c,
                    CASE WHEN yes_bid_e4>0 AND yes_ask_e4<10000
                          AND yes_ask_e4>=yes_bid_e4
                         THEN (yes_ask_e4-yes_bid_e4)/100.0 END AS spread_c,
                    yes_bid_qty_e4/10000.0 AS bid_q,
                    yes_ask_qty_e4/10000.0 AS ask_q,
                    (NOT is_snapshot)::INTEGER AS is_change
                FROM ep_l1 WHERE market_ticker={tk})
            SELECT minute_s*1000,
                   arg_max(bid_c, ts_utc), arg_max(ask_c, ts_utc),
                   MIN(mid_c), MAX(mid_c), arg_max(mid_c, ts_utc),
                   arg_max(spread_c, ts_utc),
                   arg_max(bid_q, ts_utc), arg_max(ask_q, ts_utc),
                   SUM(is_change)::INTEGER
            FROM valid GROUP BY minute_s ORDER BY minute_s""").fetchall()
        price = [[int(r[0]), _num(r[1]), _num(r[2]), _num(r[3]), _num(r[4]),
                  _num(r[5]), _num(r[6]), _num(r[7]), _num(r[8]), int(r[9])]
                 for r in price_rows]

        # Track 2: per-minute signed executed flow + last trade price
        flow_rows = self.con.sql(f"""
            SELECT (ts_utc//60000000)*60000 AS m,
                SUM(CASE WHEN taker_side='yes' THEN count_e4 ELSE 0 END)/10000.0,
                SUM(CASE WHEN taker_side='no' THEN count_e4 ELSE 0 END)/10000.0,
                SUM(CASE WHEN taker_side NOT IN ('yes','no')
                          OR taker_side IS NULL THEN count_e4 ELSE 0 END)/10000.0,
                COUNT(*)::INTEGER,
                arg_max(yes_price_e4, ts_utc)/100.0
            FROM ep_trades WHERE market_ticker={tk}
            GROUP BY 1 ORDER BY 1""").fetchall()
        flow = [[int(r[0]), _num(r[1]), _num(r[2]), _num(r[3]), int(r[4]),
                 _num(r[5])] for r in flow_rows]

        # Large trade prints: full-window descriptive percentile
        trade_rows = self.con.sql(f"""
            SELECT ts_utc, trade_id, count_e4/10000.0 AS contracts,
                   yes_price_e4/100.0, no_price_e4/100.0, taker_side,
                   cume_dist() OVER (ORDER BY count_e4) AS d_pct,
                   COUNT(*) OVER () AS d_n
            FROM ep_trades WHERE market_ticker={tk}
            ORDER BY ts_utc""").fetchall()
        all_sizes = [(r[0], float(r[2])) for r in trade_rows]
        trade_markers = []
        for ts, trade_id, contracts, yes_c, no_c, side, d_pct, d_n in trade_rows:
            if d_pct < 0.99:
                continue
            side = side if side in ("yes", "no") else None
            cash = None
            if side == "yes":
                cash = contracts * yes_c / 100.0
            elif side == "no":
                cash = contracts * no_c / 100.0
            trade_markers.append({
                "t": ts // 1000, "type": "trade", "side": side,
                "price_c": _num(yes_c), "contracts": _num(contracts),
                "cash": _num(cash), "d_pct": _num(d_pct), "d_n": int(d_n),
                "id": trade_id,
                "flags": ["LARGE-ACTIVITY CANDIDATE — NO ACTOR IDENTITY",
                          "ACTUAL TRADE PRINT (not the originating order)"],
            })
        causal_annotate(trade_markers, all_sizes)
        trade_markers.sort(key=lambda e: -(e["contracts"] or 0))
        trade_trunc = max(0, len(trade_markers) - MARKERS_PER_TYPE_PER_MARKET)
        trade_markers = trade_markers[:MARKERS_PER_TYPE_PER_MARKET]
        trade_markers.sort(key=lambda e: e["t"])

        # Track 3 proxies: same-price touch quantity changes (adds & drops)
        touch_rows = self.con.sql(f"""
            WITH tied_states AS (
                SELECT ts_utc, yes_bid_e4, yes_bid_qty_e4, yes_ask_e4,
                       yes_ask_qty_e4, is_snapshot,
                       COUNT(*) OVER (PARTITION BY ts_utc) AS tied
                FROM ep_l1 WHERE market_ticker={tk}),
            ordered AS (
                SELECT *,
                       lag(yes_bid_e4) OVER w AS p_bid,
                       lag(yes_bid_qty_e4) OVER w AS p_bid_q,
                       lag(yes_ask_e4) OVER w AS p_ask,
                       lag(yes_ask_qty_e4) OVER w AS p_ask_q,
                       lag(tied) OVER w AS p_tied
                FROM tied_states
                WINDOW w AS (ORDER BY ts_utc))
            SELECT ts_utc, 'bid' AS side,
                   (yes_bid_qty_e4-p_bid_q)/10000.0 AS delta,
                   yes_bid_e4/100.0 AS price_c
            FROM ordered
            WHERE NOT is_snapshot AND tied=1 AND p_tied=1
              AND yes_bid_e4=p_bid AND yes_bid_qty_e4<>p_bid_q
            UNION ALL
            SELECT ts_utc, 'ask',
                   (yes_ask_qty_e4-p_ask_q)/10000.0,
                   yes_ask_e4/100.0
            FROM ordered
            WHERE NOT is_snapshot AND tied=1 AND p_tied=1
              AND yes_ask_e4=p_ask AND yes_ask_qty_e4<>p_ask_q
            ORDER BY ts_utc""").fetchall()
        trades_by_min: set[int] = {int(r[0]) // 60000 for r in flow_rows}
        by_type: dict[str, list[dict]] = defaultdict(list)
        sizes_by_type: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for ts, side, delta, price_c in touch_rows:
            kind = ("l1_%s_add" % side) if delta > 0 else ("l1_%s_drop" % side)
            size = abs(float(delta))
            sizes_by_type[kind].append((ts, size))
            flags = ["LARGE-ACTIVITY CANDIDATE — NO ACTOR IDENTITY",
                     "AGGREGATE L1 PROXY (may be many orders; not an "
                     "individual order)"]
            if delta < 0:
                coincident = (ts // 60_000_000) in trades_by_min
                flags.append("TRADE-COINCIDENT MINUTE (depletion plausible)"
                             if coincident else
                             "NO COINCIDENT PRINT (cancel/removal plausible)")
            by_type[kind].append({
                "t": ts // 1000, "type": kind, "side": side,
                "price_c": _num(price_c), "contracts": _num(size),
                "cash": None, "id": None, "flags": flags})
        touch_markers = []
        touch_trunc = 0
        touch_dists = {}
        for kind, evs in by_type.items():
            values = [e["contracts"] for e in evs]
            n = len(values)
            svals = sorted(values)
            for e in evs:
                pos = bisect_right(svals, e["contracts"])
                e["d_pct"] = _num(pos / n)
                e["d_n"] = n
            causal_annotate(evs, sizes_by_type[kind])
            keep = [e for e in evs if e["d_pct"] >= 0.99]
            keep.sort(key=lambda e: -(e["contracts"] or 0))
            touch_trunc += max(0, len(keep) - MARKERS_PER_TYPE_PER_MARKET)
            keep = keep[:MARKERS_PER_TYPE_PER_MARKET]
            touch_markers.extend(keep)
            touch_dists[kind] = _distribution(
                values, "contracts",
                f"all same-price touch {kind.split('_')[2]}s in {ticker}",
                "orderbooks_l1 change rows (aggregate L1 proxy)")
        touch_markers.sort(key=lambda e: e["t"])

        # capture gaps (>65min silence violates the heartbeat contract)
        gap_rows = self.con.sql(f"""
            WITH t AS (SELECT ts_utc, lag(ts_utc) OVER (ORDER BY ts_utc) p
                       FROM ep_l1 WHERE market_ticker={tk})
            SELECT p, ts_utc FROM t
            WHERE p IS NOT NULL AND ts_utc-p > {GAP_THRESHOLD_US}
            ORDER BY p""").fetchall()
        gaps = [[int(a // 1000), int(b // 1000)] for a, b in gap_rows]

        markers = trade_markers + touch_markers
        dist = {"trade": _distribution(
            [s for _, s in all_sizes], "contracts",
            f"all deduplicated trade prints in {ticker}",
            "trades csv.gz (dup-collapsed, conflicts excluded)")}
        dist.update(touch_dists)

        tape = [{**m, "market": ticker} for m in markers]
        for a, b in gaps:
            tape.append({"t": a, "type": "gap", "t_end": b, "market": ticker,
                         "side": None, "price_c": None, "contracts": None,
                         "cash": None, "id": None, "d_pct": None, "d_n": None,
                         "c_n": None, "c_tier": None, "c_thr": None,
                         "flags": ["DATA GAP / BOOK STATE UNTRUSTED"]})

        payload = {
            "ticker": ticker, "event_ticker": event_ticker,
            "title": meta.get("title"), "yes_sub_title": meta.get("yes_sub_title"),
            "close_time": meta.get("close_time"),
            "price": price, "flow": flow, "markers": markers,
            "gaps": gaps, "dist": dist,
            "marker_truncation": {"trade": trade_trunc, "touch": touch_trunc,
                                  "cap_per_type": MARKERS_PER_TYPE_PER_MARKET,
                                  "note": "only sub-p99 or beyond-cap rows "
                                          "were dropped; the largest tail is "
                                          "always retained exactly"},
            "l1_note": "AGGREGATE L1 PROXY — best-touch displayed size only; "
                       "changes are not individual orders and carry no "
                       "actor identity",
        }
        return payload, tape, len(gaps)

    # -- tempo + regimes -------------------------------------------------------
    def _build_tempo(self, key: str) -> tuple[list, list]:
        rows = self.con.sql("""
            WITH l1m AS (
                SELECT (ts_utc//60000000)*60000 AS m,
                       COUNT(*) FILTER (WHERE NOT is_snapshot)::INTEGER AS msgs,
                       MAX(CASE WHEN yes_bid_e4>0 AND yes_ask_e4<10000
                                 AND yes_ask_e4>=yes_bid_e4
                            THEN (yes_bid_e4+yes_ask_e4)/200.0 END) mid_hi,
                       MIN(CASE WHEN yes_bid_e4>0 AND yes_ask_e4<10000
                                 AND yes_ask_e4>=yes_bid_e4
                            THEN (yes_bid_e4+yes_ask_e4)/200.0 END) mid_lo,
                       median(CASE WHEN yes_bid_e4>0 AND yes_ask_e4<10000
                                    AND yes_ask_e4>=yes_bid_e4
                            THEN (yes_ask_e4-yes_bid_e4)/100.0 END) spread_med
                FROM ep_l1 GROUP BY 1),
            trm AS (
                SELECT (ts_utc//60000000)*60000 AS m, COUNT(*)::INTEGER AS n,
                       SUM(count_e4)/10000.0 AS vol
                FROM ep_trades GROUP BY 1)
            SELECT COALESCE(l1m.m, trm.m) AS m,
                   COALESCE(msgs,0), COALESCE(trm.n,0),
                   COALESCE(mid_hi-mid_lo, 0), spread_med, COALESCE(vol,0)
            FROM l1m FULL OUTER JOIN trm ON l1m.m=trm.m
            ORDER BY 1""").fetchall()
        tempo = []
        regimes = []
        window: list[tuple[int, int]] = []  # (minute_ms, msgs)
        spread_window: list[tuple[int, float]] = []
        last_shock_end = None
        prev_label = None
        run_start = None

        def flush(label, start, end, comp, rule):
            regimes.append({"t0": start, "t1": end, "label": label,
                            "components": comp, "rule": rule})

        prev_comp, prev_rule = None, None
        for m, msgs, n_tr, move, spread_med, vol in rows:
            m = int(m)
            cutoff = m - 60 * 60000
            window = [(t, v) for t, v in window if t >= cutoff]
            spread_window = [(t, v) for t, v in spread_window if t >= cutoff]
            base = sorted(v for _, v in window)
            if base:
                med = base[len(base) // 2]
                mad = sorted(abs(v - med) for v in base)[len(base) // 2]
            else:
                med, mad = 0, 0
            s_base = sorted(v for _, v in spread_window if v is not None)
            s_med = s_base[len(s_base) // 2] if s_base else None
            burst_thr = max(20.0, med + 4 * (mad * 1.4826 + 1))
            build_thr = med + 2 * (mad * 1.4826 + 1)
            burst_score = (msgs - med) / (mad * 1.4826 + 1)
            comp = {"msgs": int(msgs), "trades": int(n_tr),
                    "mid_move_c": _num(move), "spread_med_c": _num(spread_med),
                    "baseline_med": _num(med), "baseline_mad": _num(mad),
                    "burst_thr": _num(burst_thr), "build_thr": _num(build_thr),
                    "burst_score": _num(round(burst_score, 2)),
                    "baseline_n_min": len(base)}
            if len(base) < 15:
                label, rule = "UNKNOWN", ("trailing baseline under 15 minutes"
                                          " of data — no assignment")
            elif move is not None and move >= 5 and spread_med is not None \
                    and s_med and spread_med >= 2 * s_med:
                label = "DISLOCATION"
                rule = ("mid moved %.1fc within one minute AND median spread "
                        "%.1fc >= 2x trailing median %.1fc"
                        % (move, spread_med, s_med))
                last_shock_end = m
            elif msgs >= burst_thr:
                label = "BURST"
                rule = ("msgs %d >= burst threshold %.1f "
                        "(max(20, med %.1f + 4*1.4826*MAD %.1f + 4))"
                        % (msgs, burst_thr, med, mad))
                last_shock_end = m
            elif msgs >= build_thr and msgs >= 5:
                label = "BUILDING"
                rule = ("msgs %d >= building threshold %.1f "
                        "(med + 2*1.4826*MAD + 2)" % (msgs, build_thr))
            elif last_shock_end is not None and m - last_shock_end <= 10 * 60000:
                label = "RECOVERY"
                rule = ("within 10 minutes after a BURST/DISLOCATION minute "
                        "and message rate back under the building threshold")
            else:
                label, rule = "CALM", ("message rate under building threshold "
                                       "and no recent shock")
            window.append((m, msgs))
            if spread_med is not None:
                spread_window.append((m, float(spread_med)))
            tempo.append([m, int(msgs), int(n_tr), _num(move),
                          _num(spread_med), _num(round(burst_score, 2)),
                          _num(vol)])
            if label != prev_label:
                if prev_label is not None:
                    flush(prev_label, run_start, m, prev_comp, prev_rule)
                prev_label, run_start = label, m
            prev_comp, prev_rule = comp, rule
        if prev_label is not None and rows:
            flush(prev_label, run_start, int(rows[-1][0]) + 60000,
                  prev_comp, prev_rule)
        return tempo, regimes

    # -- real L2 (orderbooks_full) ----------------------------------------------
    def _build_l2(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        self._l2_rows: dict[str, list] = {}
        files = self._l2_files()
        if not files or not tickers:
            return {}
        cols = [r[0] for r in self.con.sql(
            f"DESCRIBE SELECT * FROM read_parquet({_sql_files(files)})").fetchall()]
        has_seq = "ws_seq" in cols
        mk_list = ",".join(_sql_str(t) for t in tickers)
        seq_sel = "ws_seq" if has_seq else "NULL"
        rows = self.con.sql(f"""
            SELECT market_ticker, ts_utc, msg_type, side, price_e4, delta_e4,
                   yes_levels, no_levels, {seq_sel} AS ws_seq
            FROM read_parquet({_sql_files(files)}, union_by_name=true)
            WHERE market_ticker IN ({mk_list})
            ORDER BY market_ticker, ts_utc""").fetchall()
        out: dict[str, dict[str, Any]] = {}
        per_market: dict[str, list] = defaultdict(list)
        for r in rows:
            per_market[r[0]].append(r)
        self._l2_rows = dict(per_market)  # kept for the depth heatmap build
        for ticker, mrows in per_market.items():
            out[ticker] = self._l2_market(ticker, mrows, has_seq)
        return out

    def _l2_market(self, ticker: str, rows: list, has_seq: bool) -> dict[str, Any]:
        book = {"yes": {}, "no": {}}
        depth = []  # [t_ms, yes_top5_qty, no_top5_qty, imbalance, levels_yes, levels_no]
        events = []
        deltas_seen = []
        last_bucket = None
        seq_gaps = 0
        prev_seq = None

        def top5(side: str) -> float:
            lv = sorted(book[side].items(), key=lambda kv: -kv[0])[:5]
            return sum(q for _, q in lv) / 10000.0

        def emit(t_us: int) -> None:
            y, n = top5("yes"), top5("no")
            tot = y + n
            depth.append([t_us // 1000, _num(round(y, 2)), _num(round(n, 2)),
                          _num(round((y - n) / tot, 4)) if tot else None,
                          len(book["yes"]), len(book["no"])])

        for (_, ts, msg_type, side, price_e4, delta_e4,
             yes_levels, no_levels, ws_seq) in rows:
            if has_seq and ws_seq is not None:
                if prev_seq is not None and ws_seq != prev_seq + 1:
                    seq_gaps += 1
                prev_seq = ws_seq
            if msg_type == "snapshot":
                book = {"yes": {}, "no": {}}
                for side_name, levels in (("yes", yes_levels), ("no", no_levels)):
                    for price, qty in json.loads(levels or "[]"):
                        if qty > 0:
                            book[side_name][int(price)] = int(qty)
            elif msg_type == "delta" and side in ("yes", "no"):
                cur = book[side].get(int(price_e4), 0)
                new = cur + int(delta_e4)
                depleted = cur > 0 and new <= 0
                if new <= 0:
                    book[side].pop(int(price_e4), None)
                else:
                    book[side][int(price_e4)] = new
                size = abs(int(delta_e4)) / 10000.0
                deltas_seen.append((ts, size, int(delta_e4) > 0, depleted,
                                    int(price_e4), side))
            bucket = ts // 1_000_000  # 1s
            if bucket != last_bucket:
                emit(ts)
                last_bucket = bucket
        if rows:
            emit(rows[-1][1])

        sizes = [s for _, s, _, _, _, _ in deltas_seen]
        svals = sorted(sizes)
        n = len(svals)
        add_sizes = [(t, s) for t, s, pos, _, _, _ in deltas_seen if pos]
        rem_sizes = [(t, s) for t, s, pos, _, _, _ in deltas_seen if not pos]
        for ts, size, positive, depleted, price_e4, side in deltas_seen:
            pct = bisect_right(svals, size) / n if n else 0
            if pct < 0.995 and not depleted:
                continue
            kind = "l2_add" if positive else (
                "l2_depletion" if depleted else "l2_remove")
            events.append({
                "t": ts // 1000, "type": kind, "side": side,
                "price_c": _num(price_e4 / 100.0), "contracts": _num(size),
                "cash": None, "id": None, "d_pct": _num(pct), "d_n": n,
                "flags": ["LARGE-ACTIVITY CANDIDATE — NO ACTOR IDENTITY",
                          "REAL L2 PRICE-LEVEL UPDATE (a level is not "
                          "necessarily one order)"]})
        causal_annotate([e for e in events if e["type"] == "l2_add"], add_sizes)
        causal_annotate([e for e in events if e["type"] != "l2_add"], rem_sizes)
        events.sort(key=lambda e: -(e["contracts"] or 0))
        trunc = max(0, len(events) - MARKERS_PER_TYPE_PER_MARKET)
        events = sorted(events[:MARKERS_PER_TYPE_PER_MARKET],
                        key=lambda e: e["t"])
        return {
            "available": True,
            "window": {"t0_ms": rows[0][1] // 1000, "t1_ms": rows[-1][1] // 1000,
                       "t0_utc": _iso(rows[0][1]), "t1_utc": _iso(rows[-1][1])},
            "depth": depth,
            "markers": events,
            "marker_truncation": trunc,
            "seq": {"available": has_seq, "gaps": seq_gaps,
                    "note": "pre-W5 archive has no ws_seq column; same-µs "
                            "delta ordering relies on file order "
                            "(seq_unavailable)" if not has_seq else
                            "per-sid ws_seq gap check"},
            "dist": {
                "l2_delta": _distribution(
                    sizes, "contracts",
                    f"all L2 level deltas (|delta|) in {ticker} watchlist window",
                    "orderbooks_full snapshot+delta parquet"),
            },
            "note": "REAL L2 — full-depth watchlist capture; depth = top-5 "
                    "price levels per side, 1s samples",
        }

    # -- depth heatmap artifacts (v2 primary view) --------------------------------
    def _heatmap_trades(self, ticker: str, t0_us: int, t1_us: int) -> dict[str, Any]:
        """Trade prints overlaying the heatmap: dot at (t, yes price), sized
        by contracts, colored by taker side. taker_side is the ONLY taker
        attribution in this archive ('yes'/'no'; deprecated fallback field —
        docs/kalshi_ws_protocol.md I9; the newer taker_outcome_side /
        taker_book_side fields are not in these archived days). NULL renders
        neutral with an explicit 'taker side unknown' — never guessed."""
        rows = self.con.sql(f"""
            SELECT ts_utc, yes_price_e4/100.0, count_e4/10000.0,
                   taker_side, trade_id
            FROM ep_trades WHERE market_ticker={_sql_str(ticker)}
              AND ts_utc BETWEEN {int(t0_us)} AND {int(t1_us)}
            ORDER BY ts_utc""").fetchall()
        truncated = 0
        if len(rows) > HEATMAP_TRADE_CAP:
            truncated = len(rows) - HEATMAP_TRADE_CAP
            rows = sorted(rows, key=lambda r: -(r[2] or 0))[:HEATMAP_TRADE_CAP]
            rows.sort(key=lambda r: r[0])
        return {
            "rows": [[int(r[0]) // 1000, _num(r[1]), _num(r[2]),
                      r[3] if r[3] in ("yes", "no") else None, r[4]]
                     for r in rows],
            "truncated": truncated, "cap": HEATMAP_TRADE_CAP,
            "columns": ["t_ms", "yes_price_c", "contracts",
                        "taker_side", "trade_id"],
            "taker_provenance": "taker_side archive field (deprecated "
                                "fallback per docs/kalshi_ws_protocol.md I9; "
                                "taker_outcome_side/taker_book_side absent "
                                "on these days); null = taker side unknown, "
                                "shown neutral — never guessed",
        }

    def _l1_touch_rows(self, ticker: str) -> list:
        return self.con.sql(f"""
            SELECT ts_utc, yes_bid_e4, yes_bid_qty_e4, yes_ask_e4,
                   yes_ask_qty_e4
            FROM ep_l1 WHERE market_ticker={_sql_str(ticker)}
            ORDER BY ts_utc""").fetchall()

    def _attach_heatmaps(self, ep_safe: str, group: dict[str, Any],
                         markets_payload: list[dict[str, Any]]) -> None:
        """One heatmap artifact per detailed market. Guard: a full-price-grid
        (l2_full) heatmap is ONLY ever built from that market's own
        orderbooks_full rows; markets without full-book capture get the
        degraded touch-band artifact (or an honest 'unavailable')."""
        hm_dir = self.out_dir / "data" / "heatmaps"
        hm_dir.mkdir(parents=True, exist_ok=True)
        l2_rows = getattr(self, "_l2_rows", {})
        for mk in markets_payload:
            ticker = mk["ticker"]
            if l2_rows.get(ticker):
                hm = build_l2_heatmap(l2_rows[ticker])
                hm["seq_note"] = mk["l2"]["seq"]["note"] \
                    if mk.get("l2", {}).get("available") else None
                hm["label"] = ("REAL L2 FULL BOOK — resting displayed depth "
                               "(log color scale)")
            else:
                rows = self._l1_touch_rows(ticker)
                if not rows:
                    mk["heatmap"] = {
                        "kind": "unavailable",
                        "note": "no full-book capture AND no L1 archive rows "
                                "for this market — nothing honest to draw",
                    }
                    continue
                hm = build_l1_touch_heatmap(
                    rows, group["t0"], group["t1"], mk.get("gaps") or [])
                hm["label"] = ("AGGREGATE L1 PROXY — touch depth only; "
                               "NOT full book")
            hm["schema_version"] = HEATMAP_SCHEMA
            hm["generated_at_utc"] = self.generated_at
            hm["market_ticker"] = ticker
            hm["episode_key"] = group["key"]
            hm["trades"] = self._heatmap_trades(
                ticker, hm["t0_us"], hm["t1_us"])
            dist = hm.pop("dist")
            name = f"{ep_safe}__{safe_key(ticker)}"
            _write_json(hm_dir / f"{name}.json", hm)
            _write_jsonp(hm_dir / f"{name}.js", f"heatmap:{name}", hm)
            mk["heatmap"] = {
                "kind": hm["kind"],
                "name": name,
                "src": f"data/heatmaps/{name}.js",
                "bytes": (hm_dir / f"{name}.json").stat().st_size,
                "label": hm["label"],
                "t0_us": hm["t0_us"], "t1_us": hm["t1_us"],
                "bin_us": hm["bin_us"], "n_bins": hm["n_bins"],
                "price_rows": hm["price_rows"],
                "dist": dist,
                "definition": hm["definition"],
            }

    # -- orchestration -----------------------------------------------------------
    def build(self) -> dict[str, Any]:
        inventory = build_inventory(self.repo_root, self.data_root,
                                    self.tl1_status())
        index_groups = self.build_episode_index()
        data_dir = self.out_dir / "data"
        ep_dir = data_dir / "episodes"
        ep_dir.mkdir(parents=True, exist_ok=True)

        episodes_meta = []
        for sport_block in index_groups["sports"]:
            for g in sport_block["episodes"]:
                artifact = self.build_episode(g)
                sk = artifact["safe_key"]
                _write_json(ep_dir / f"{sk}.json", artifact)
                _write_jsonp(ep_dir / f"{sk}.js", f"episode:{sk}", artifact)
                episodes_meta.append({
                    "key": g["key"], "safe_key": sk, "sport": g["sport"],
                    "title": g["title"], "sub": g["sub"],
                    "volume": _num(round(g["volume"], 1)),
                    "trades": g["trades"], "markets": g["markets"],
                    "events": len(g["events"]),
                    "t0_ms": g["t0"] // 1000, "t1_ms": g["t1"] // 1000,
                    "t0_utc": _iso(g["t0"]), "t1_utc": _iso(g["t1"]),
                    "has_l2": any(m.get("l2", {}).get("available")
                                  for m in artifact["markets"]),
                    "bytes": (ep_dir / f"{sk}.json").stat().st_size,
                })

        index_payload = {
            "schema_version": INDEX_SCHEMA,
            "generated_at_utc": self.generated_at,
            "inventory": inventory,
            "evidence": self.evidence_summary(),
            "trade_quality": self.trade_quality,
            "sports": [{"sport": s, "episodes":
                        [e for e in episodes_meta if e["sport"] == s]}
                       for s in self.sports],
            "thresholds": {"tiers": THRESHOLD_TIERS},
            "notes": [
                "All values from real archived local data; no synthetic rows.",
                "Local archive is the dormant Mac mirror (production moved "
                "to EC2 2026-07-09); dates reflect what exists locally.",
            ],
        }
        _write_json(data_dir / "index.json", index_payload)
        _write_jsonp(data_dir / "index.js", "index", index_payload)
        self._render_frontend()
        return index_payload

    def _render_frontend(self) -> None:
        assets = self.out_dir / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ECHARTS_PATH, assets / "echarts.min.js")
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        (self.out_dir / "index.html").write_text(html, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def _write_jsonp(path: Path, name: str, payload: dict[str, Any]) -> None:
    """JSONP-style artifact so the dashboard works over file:// (script tags
    load where fetch() cannot)."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body = body.replace("<", "\\u003c").replace(">", "\\u003e")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text("window.__EI_LOAD__(%s,%s);\n" % (json.dumps(name), body),
                   encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--episodes-per-sport", type=int,
                        default=EPISODES_PER_SPORT)
    parser.add_argument(
        "--data-root", default=None,
        help="explicit data root with facts/, catalog/, seals/, raw/ — e.g. "
             "the PIPE-W05 verified research cache view "
             "(work/research_cache/view built by tools/research_data.py in "
             "the pipeline repo); default = the local warehouse under the "
             "repository root")
    args = parser.parse_args(argv)
    builder = EventIntelBuilder(
        episodes_per_sport=args.episodes_per_sport,
        data_root=Path(args.data_root) if args.data_root else None)
    index = builder.build()
    total = sum(len(s["episodes"]) for s in index["sports"])
    print(json.dumps({
        "generated_at_utc": index["generated_at_utc"],
        "episodes": total,
        "out": str(builder.out_dir),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
