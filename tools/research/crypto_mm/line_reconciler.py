#!/usr/bin/env python3
"""Per-line fill attribution + budget partition check (expansion gates 2+3).

Every strategy line prefixes its client_order_id with a line tag (gate 1,
``mm_engine.LINE_TAG``).  This tool joins portfolio fills against the lines'
own order receipts and enforces:

  gate 2 - every fill maps to exactly one line (by order_id from receipts,
           falling back to the client_order_id prefix). An unmatched fill is
           an ORPHAN => exit 1. The 372 unattributed fills of 2026-07 are the
           incident this exists to prevent.
  gate 3 - the sum of ``max_loss_micro_usd`` across the lines' budget/control
           documents must not exceed account equity: lines may not silently
           share the same risk dollars.

Read-only: consumes NDJSON receipts + a fills JSON export (from the GET-only
/portfolio/fills capture); never talks to the exchange itself.

Usage:
  line_reconciler.py --fills fills.json \
      --line C15:work/live/crypto_mm_engine/*.ndjson \
      [--line BOX:path/glob.ndjson ...] \
      [--budget C15:control.json ...] [--equity-usd 500]
"""
import argparse, fnmatch, glob, json, sys
from collections import defaultdict


def load_line_orders(pattern):
    """order_id -> client_order_id from a line's engine receipts."""
    oid_map = {}
    for path in sorted(glob.glob(pattern)):
        with open(path) as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                oid = r.get("oid") or r.get("order_id")
                cid = r.get("client_order_id")
                if oid and cid:
                    oid_map[str(oid)] = str(cid)
    return oid_map


def load_fills(path):
    d = json.load(open(path))
    if isinstance(d, dict):
        d = d.get("fills") or d.get("data") or []
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fills", required=True)
    ap.add_argument("--line", action="append", default=[],
                    metavar="TAG:GLOB", help="line tag + receipts NDJSON glob")
    ap.add_argument("--budget", action="append", default=[],
                    metavar="TAG:CONTROL_JSON")
    ap.add_argument("--equity-usd", type=float, default=None)
    a = ap.parse_args()

    lines = {}
    for spec in a.line:
        tag, pat = spec.split(":", 1)
        lines[tag] = load_line_orders(pat)

    per_line = defaultdict(lambda: {"fills": 0, "contracts": 0.0})
    orphans = []
    for f in load_fills(a.fills):
        oid = str(f.get("order_id") or "")
        cnt = float(f.get("count_fp") or f.get("count") or 0)
        owner = None
        for tag, oid_map in lines.items():
            if oid in oid_map:
                owner = tag
                break
        if owner is None:  # fallback: tag prefix on client_order_id if echoed
            cid = str(f.get("client_order_id") or "")
            owner = next((t for t in lines if cid.startswith(t + "-")), None)
        if owner is None:
            orphans.append(f)
        else:
            per_line[owner]["fills"] += 1
            per_line[owner]["contracts"] += cnt

    for tag, s in sorted(per_line.items()):
        print(f"line {tag}: fills={s['fills']} contracts={s['contracts']:.2f}")
    print(f"orphans={len(orphans)}")
    for f in orphans[:5]:
        print("  ORPHAN", f.get("order_id"), f.get("ticker"),
              f.get("created_time"))

    ok = not orphans
    if a.budget and a.equity_usd is not None:
        total = 0
        for spec in a.budget:
            tag, path = spec.split(":", 1)
            doc = json.load(open(path))
            ml = int(doc.get("max_loss_micro_usd") or
                     doc.get("budget", {}).get("max_loss_micro_usd") or 0)
            total += ml
            print(f"budget {tag}: max_loss=${ml/1e6:.2f}")
        print(f"budget total=${total/1e6:.2f} equity=${a.equity_usd:.2f}")
        if total > a.equity_usd * 1e6:
            print("BUDGET PARTITION VIOLATION: sum(max_loss) > equity")
            ok = False

    print("VERDICT:", "CLEAN" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
