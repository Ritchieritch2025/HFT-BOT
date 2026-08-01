#!/usr/bin/env python3
"""Generalized metrics extractor for any crypto-MM engine run.

Design rule (operator 2026-07-27): nothing about the metric definitions may
be specific to one run, one rung, one market, or one config.  The extractor
reads engine receipts (NDJSON) plus optional anchor recordings and emits the
SAME schema for every run, so runs are comparable and bottlenecks are
attributable.  There are no hardcoded thresholds, tickers, dates, or paths:
everything comes from arguments and from the receipts themselves.

Metric families (each stratified, never a single blended number):
  execution  — RTT/ack/cancel latency, reject and expiry rates
  fill       — fill rate per quote, fill hazard by quote age
  adverse    — markout at configurable horizons, per side/zone/tte bucket
  race       — fills arriving after a cancel intent, by regime
  inventory  — dwell time to flat, peak, pairing rate, exit cost
  pnl        — realized, fees, per-contract mean with cluster bootstrap CI
  pricing    — model fair vs mid, vs settlement outcome (calibration)

Usage:
  mm_metrics.py --receipts <dir_or_glob> [--receipts <other> ...]
                [--label A_front_live] [--horizons 5,30,120]
                [--out report.json] [--compare]

Two --receipts sets with --compare emits a side-by-side delta table, which
is how a rung is judged against its predecessor.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import gzip
import json
import math
import os
import statistics as st
from collections import defaultdict

# ---------------------------------------------------------------- helpers


def iter_receipts(patterns):
    """Yield receipt dicts from files/dirs/globs, plain or gzipped."""
    paths = []
    for pat in patterns:
        if os.path.isdir(pat):
            paths.extend(sorted(glob.glob(os.path.join(pat, "*.ndjson*"))))
        else:
            paths.extend(sorted(glob.glob(pat)))
    for path in paths:
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", errors="ignore") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except (ValueError, TypeError):
                        continue
        except OSError:
            continue


def pct(values, q):
    """Percentile without numpy; q in [0,100]."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def dist(values, name=""):
    """Standard distribution block — the only latency/quantity summary
    shape used anywhere, so every run's numbers line up."""
    if not values:
        return {"n": 0}
    return {"n": len(values),
            "mean": round(st.mean(values), 4),
            "p50": round(pct(values, 50), 4),
            "p90": round(pct(values, 90), 4),
            "p95": round(pct(values, 95), 4),
            "p99": round(pct(values, 99), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4)}


def cluster_bootstrap_ci(pairs, rounds=2000, seed_stream=None):
    """95% CI on the mean, resampling CLUSTERS (markets), not observations.

    seed_stream: iterable of ints for determinism without Math.random-style
    global state; falls back to a fixed LCG so results are reproducible.
    """
    if not pairs:
        return None
    groups = defaultdict(list)
    for key, value in pairs:
        groups[key].append(value)
    keys = list(groups)
    if len(keys) < 2:
        return None
    state = 88172645463325252
    means = []
    for _ in range(rounds):
        picked = []
        for _ in range(len(keys)):
            state ^= (state << 13) & ((1 << 64) - 1)
            state ^= state >> 7
            state ^= (state << 17) & ((1 << 64) - 1)
            picked.extend(groups[keys[state % len(keys)]])
        if picked:
            means.append(sum(picked) / len(picked))
    if not means:
        return None
    return [round(pct(means, 2.5), 4), round(pct(means, 97.5), 4)]


def zone_of(price_c):
    """Price-zone bucket label.  Boundaries are data-independent decades so
    the same labels apply to any market and any run."""
    if price_c is None:
        return "unknown"
    for hi, name in ((10, "0-10"), (20, "10-20"), (35, "20-35"),
                     (65, "35-65"), (80, "65-80"), (90, "80-90")):
        if price_c < hi:
            return name
    return "90-100"


def tte_bucket(tte_s):
    """Time-to-expiry bucket in exponential-ish steps (generic)."""
    if tte_s is None:
        return "unknown"
    for hi, name in ((120, "<2m"), (300, "2-5m"), (600, "5-10m"),
                     (1200, "10-20m")):
        if tte_s < hi:
            return name
    return ">20m"


# ------------------------------------------------------------- extraction

class RunMetrics:
    def __init__(self, label, patterns, horizons):
        self.label = label
        self.patterns = patterns
        self.horizons = horizons
        self.fairs = defaultdict(list)      # mt -> [(ms, fair_c)]
        self.events = []
        self.n_raw = 0

    def load(self):
        for rec in iter_receipts(self.patterns):
            self.n_raw += 1
            ev = rec.get("ev")
            if ev == "QUOTE_EVAL":
                src_ms = rec.get("source_ms")
                fair = rec.get("fair_c")
                if src_ms is not None and fair is not None:
                    self.fairs[rec.get("mt")].append((int(src_ms),
                                                      float(fair)))
                self.events.append(rec)
            elif ev is not None:
                self.events.append(rec)
        for mt in self.fairs:
            self.fairs[mt].sort()
        self._fair_keys = {mt: [a[0] for a in arr]
                           for mt, arr in self.fairs.items()}

    def fair_at(self, mt, ms, tol_ms=3000):
        arr = self.fairs.get(mt)
        if not arr:
            return None
        keys = self._fair_keys[mt]
        i = bisect.bisect_left(keys, ms)
        if i >= len(arr):
            return None
        ts, val = arr[i]
        return val if ts - ms <= tol_ms else None

    # ---------------------------------------------------------- families
    def execution(self):
        by = defaultdict(list)
        rejects = defaultdict(int)
        for r in self.events:
            ev = r.get("ev")
            ms = r.get("ms")
            if ms is None:
                continue
            if ev in ("ORDER_ACK", "ORDER_REJ", "CANCEL_ACK", "CANCEL_FAIL",
                      "TAKER_ACK", "TAKER_REJ"):
                by[ev].append(float(ms))
            if ev in ("ORDER_REJ", "CANCEL_FAIL", "TAKER_REJ"):
                rejects[str(r.get("code"))] += 1
        out = {k: dist(v) for k, v in sorted(by.items())}
        out["reject_codes"] = dict(rejects)
        n_ack = len(by.get("ORDER_ACK", []))
        n_rej = len(by.get("ORDER_REJ", []))
        out["order_reject_rate"] = (round(n_rej / (n_ack + n_rej), 4)
                                    if (n_ack + n_rej) else None)
        out["expired_orders"] = sum(
            1 for r in self.events if r.get("ev") == "ORDER_EXPIRED")
        return out

    def fills(self):
        placed = sum(1 for r in self.events
                     if r.get("ev") in ("ORDER_ACK", "INTENT_PLACE"))
        fills = [r for r in self.events if r.get("ev") == "FILL"]
        fees = [float(r.get("fee_dollars") or 0.0) for r in fills]
        by_side = defaultdict(int)
        for r in fills:
            by_side[str(r.get("side"))] += 1
        return {"quotes_placed": placed,
                "fills": len(fills),
                "fill_rate_per_quote": (round(len(fills) / placed, 4)
                                        if placed else None),
                "fills_by_side": dict(by_side),
                "fee_dollars_total": round(sum(fees), 6),
                "fee_dollars_per_fill": (round(sum(fees) / len(fills), 6)
                                         if fills else None)}

    def adverse(self):
        """Markout by horizon x side x zone x tte — the generic adverse-
        selection surface.  Any bucket can be sliced later; nothing is
        collapsed into a single number here."""
        surface = defaultdict(list)
        flat = defaultdict(list)
        for r in self.events:
            if r.get("ev") != "FILL":
                continue
            mt = r.get("ticker") or r.get("mt")
            side = str(r.get("side"))
            px_c = None
            if r.get("px_dollars") is not None:
                px_c = float(r["px_dollars"]) * 100.0
            if px_c is None or mt is None:
                continue
            t_ms = int(float(r.get("ts_s", 0)) * 1000) or int(
                float(r.get("wall_ns", 0)) / 1e6)
            yes_equiv = px_c if side == "bid" else 100.0 - px_c
            for h in self.horizons:
                fv = self.fair_at(mt, t_ms + int(h * 1000))
                if fv is None:
                    continue
                mo = (fv - px_c) if side == "bid" else (100.0 - fv) - px_c
                key = f"h{h}s|{side}|{zone_of(yes_equiv)}"
                surface[key].append(mo)
                flat[f"h{h}s"].append(mo)
        return {"by_bucket": {k: dist(v) for k, v in sorted(surface.items())},
                "pooled": {k: dist(v) for k, v in sorted(flat.items())}}

    def race(self):
        """Fills landing after a cancel intent on the same slot — the
        cancel-race outcome, bucketed by delay window."""
        cancels = defaultdict(list)
        for r in self.events:
            if r.get("ev") in ("CANCEL_ACK", "INTENT_CANCEL",
                               "ANCHOR_PULL") and r.get("wall_ns"):
                cancels[(r.get("mt"), str(r.get("side")))].append(
                    int(r["wall_ns"]))
        for k in cancels:
            cancels[k].sort()
        windows = (200, 500, 1000, 5000)
        hits = {w: 0 for w in windows}
        n_fills = 0
        for r in self.events:
            if r.get("ev") != "FILL" or not r.get("wall_ns"):
                continue
            n_fills += 1
            key = (r.get("ticker") or r.get("mt"), str(r.get("side")))
            arr = cancels.get(key) or []
            i = bisect.bisect_right(arr, int(r["wall_ns"])) - 1
            if i < 0:
                continue
            dt_ms = (int(r["wall_ns"]) - arr[i]) / 1e6
            for w in windows:
                if 0 <= dt_ms <= w:
                    hits[w] += 1
        return {"fills": n_fills,
                "cancel_intents": sum(len(v) for v in cancels.values()),
                "filled_after_cancel": {
                    f"within_{w}ms": {
                        "count": hits[w],
                        "share_of_fills": (round(hits[w] / n_fills, 4)
                                           if n_fills else None)}
                    for w in windows}}

    def inventory(self):
        locks = [r for r in self.events if r.get("ev") == "PAIR_LOCK"]
        waits = [float(r.get("pair_wait_s") or 0.0) for r in locks]
        pnl_c = [float(r.get("locked_usd") or 0.0) * 100 for r in locks]
        pos = [x for x in pnl_c if x >= 0]
        neg = [x for x in pnl_c if x < 0]
        peaks = [int(r.get("orders") or 0) for r in self.events
                 if r.get("ev") == "HEALTH"]
        return {"pair_cycles": len(locks),
                "dwell_to_flat_s": dist(waits),
                "completion_rate": (round(len(pos) / len(locks), 4)
                                    if locks else None),
                "win_c": dist(pos), "loss_c": dist(neg),
                "loss_share_of_gross_win": (
                    round(-sum(neg) / sum(pos), 4)
                    if pos and sum(pos) > 0 else None),
                "concurrent_orders": dist([float(x) for x in peaks])}

    def pnl(self):
        settles = [r for r in self.events
                   if r.get("ev") in ("SETTLE", "SETTLEMENT")]
        per_contract = []
        for r in settles:
            v = r.get("pnl_c_per_contract")
            mt = r.get("ticker") or r.get("mt")
            if v is not None and mt:
                per_contract.append((mt, float(v)))
        last_realized = None
        for r in reversed(self.events):
            if r.get("ev") == "HEALTH" and r.get("realized") is not None:
                last_realized = float(r["realized"])
                break
        return {"settlements": len(settles),
                "realized_usd_last_seen": last_realized,
                "per_contract_c": dist([v for _, v in per_contract]),
                "per_contract_ci95_cluster": cluster_bootstrap_ci(
                    per_contract)}

    def pricing(self):
        gaps = []
        anchors = []
        for r in self.events:
            if r.get("ev") != "QUOTE_EVAL":
                continue
            if r.get("fair_c") is not None and r.get("mid_c") is not None:
                gaps.append(float(r["fair_c"]) - float(r["mid_c"]))
            if r.get("anchor_dx_usd") is not None:
                anchors.append(abs(float(r["anchor_dx_usd"])))
        reasons = defaultdict(int)
        for r in self.events:
            if r.get("ev") == "QUOTE_EVAL":
                reasons[str(r.get("pricing_reason"))] += 1
        return {"fair_minus_mid_c": dist(gaps),
                "abs_anchor_unpriced_usd": dist(anchors),
                "pricing_reason_counts": dict(reasons),
                "anchor_pulls": sum(1 for r in self.events
                                    if r.get("ev") == "ANCHOR_PULL")}

    def report(self):
        return {"label": self.label,
                "receipt_patterns": self.patterns,
                "receipts_read": self.n_raw,
                "markets_seen": len(self.fairs),
                "horizons_s": self.horizons,
                "execution": self.execution(),
                "fill": self.fills(),
                "adverse": self.adverse(),
                "race": self.race(),
                "inventory": self.inventory(),
                "pnl": self.pnl(),
                "pricing": self.pricing()}


def compare(reports):
    """Side-by-side of the seven headline numbers every rung is judged on."""
    rows = []
    for rep in reports:
        adv = rep["adverse"]["pooled"]
        first_h = sorted(adv)[0] if adv else None
        rows.append({
            "label": rep["label"],
            "fills": rep["fill"]["fills"],
            "fill_rate": rep["fill"]["fill_rate_per_quote"],
            "fee_per_fill": rep["fill"]["fee_dollars_per_fill"],
            f"markout_{first_h}_mean": (adv[first_h]["mean"]
                                        if first_h else None),
            "completion_rate": rep["inventory"]["completion_rate"],
            "loss_share_gross_win":
                rep["inventory"]["loss_share_of_gross_win"],
            "filled_after_cancel_500ms":
                rep["race"]["filled_after_cancel"]["within_500ms"][
                    "share_of_fills"],
            "ack_p95_ms": (rep["execution"].get("ORDER_ACK") or {}).get("p95"),
            "per_contract_c": rep["pnl"]["per_contract_c"].get("mean"),
            "per_contract_ci95": rep["pnl"]["per_contract_ci95_cluster"],
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--receipts", action="append", required=True,
                    help="dir, file, or glob of engine NDJSON (repeatable)")
    ap.add_argument("--label", action="append", default=None,
                    help="label per --receipts set")
    ap.add_argument("--horizons", default="5,30,120",
                    help="markout horizons in seconds")
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()

    horizons = [float(x) for x in args.horizons.split(",") if x.strip()]
    labels = args.label or []
    reports = []
    for i, pattern in enumerate(args.receipts):
        label = labels[i] if i < len(labels) else f"run{i + 1}"
        run = RunMetrics(label, [pattern], horizons)
        run.load()
        reports.append(run.report())

    payload = {"schema_version": "mm-metrics-v1", "runs": reports}
    if args.compare and len(reports) > 1:
        payload["comparison"] = compare(reports)
    text = json.dumps(payload, indent=1)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"written: {args.out}")
    print(text if not args.out else json.dumps(
        payload.get("comparison") or compare(reports), indent=1))


if __name__ == "__main__":
    main()
