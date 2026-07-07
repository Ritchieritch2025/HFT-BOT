#!/usr/bin/env python3
"""depth_probe — OPERATOR-GATED bounded orderbook_delta sizing probe (W6).

Design doc: docs/PLAN_DEPTH_EXPANSION.md (read it before running anything).

What it does (run mode, REFUSED without --operator-approved):
  Launches a SECOND ws_shadow instance (the read-only harness — transmits
  nothing, refuses live mode itself) subscribed to orderbook_delta for the
  top-N markets of the newest work/mm/depth_target_*.csv, for a bounded
  window (default 900 s), capturing to its OWN path under work/probe/ —
  NEVER the production hourly logs and NEVER work/metrics.ndjson (the
  double-writer lesson). work/probe/ is not tailed by the ingester, so
  probe data cannot contaminate the warehouse. After capture it prints
  measured msg rates + bytes/market by liquidity tier from the capture file.

Analysis-only mode (offline, no gate needed — reads an existing file):
  python3 tools/depth_probe.py --analyze <capture.ndjson> [--csv <tiers.csv>]

Safety:
  - REFUSES to run without --operator-approved (exit 2, loud pointer at the
    plan doc). --dry-run prints the exact env/tickers and exits 0 with no
    socket opened.
  - REFUSES KALSHI_MODE=live even with the flag (and ws_shadow refuses live
    again underneath — two independent layers).
  - REFUSES any capture path under work/raw/ (production capture tree).
  - Forces KALSHI_MODE=data_collect and scrubs KALSHI_WS_FIREHOSE for the
    child process. Zero REST tokens spent (no cross-check).

stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN_DOC = "docs/PLAN_DEPTH_EXPANSION.md"
PROBE_DIR = os.path.join("work", "probe")
BOOK_CHANNELS = ("orderbook_delta", "orderbook_snapshot")


def refuse(msg: str) -> int:
    sys.stderr.write(
        "\n"
        "=================================================================\n"
        "  depth_probe REFUSED: %s\n"
        "\n"
        "  This is the OPERATOR-GATED depth-expansion probe from\n"
        "  %s (W6). It opens a SECOND WebSocket\n"
        "  connection to the exchange. It is never run by an agent or a\n"
        "  console button. Read the plan doc, then re-run with\n"
        "  --operator-approved if you (the operator) approve the bounded\n"
        "  15-minute read-only probe.\n"
        "=================================================================\n"
        "\n" % (msg, PLAN_DOC)
    )
    return 2


# --------------------------------------------------------------------------
# tickers / tiers from the depth-target CSV
# --------------------------------------------------------------------------

def newest_depth_target_csv() -> str | None:
    paths = sorted(glob.glob(os.path.join(ROOT, "work", "mm", "depth_target_*.csv")))
    return paths[-1] if paths else None


def load_depth_target(path: str, top_n: int | None = None):
    """Return (tickers_in_rank_order, {ticker: liquidity_tier})."""
    tickers: list[str] = []
    tiers: dict[str, str] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            t = row.get("market_ticker", "").strip()
            if not t:
                continue
            tiers[t] = row.get("liquidity_tier", "?").strip() or "?"
            if top_n is None or len(tickers) < top_n:
                tickers.append(t)
    return tickers, tiers


# --------------------------------------------------------------------------
# analysis (pure; unit-tested in tests/test_depth_probe.py)
# --------------------------------------------------------------------------

def analyze_capture(capture_path: str, tiers: dict[str, str] | None = None,
                    expected_tickers: list[str] | None = None) -> dict:
    """Measure msg rates + bytes/market by tier from a ws_shadow NDJSON capture.

    Capture line format (WsRecorder): {"recv_wall_ns":..., "channel":...,
    "source_ticker":..., "raw":"<verbatim wire frame>", ...}.

    Returns a dict:
      duration_s        span of recv_wall_ns over book messages
      total_msgs        deltas + snapshots
      msgs_per_s        total_msgs / duration_s (0 duration -> 0)
      per_market        {ticker: {tier,deltas,snapshots,capture_bytes,
                                  wire_bytes,msgs_per_s}}
      per_tier          {tier: {markets,msgs,msgs_per_s,capture_bytes,
                                wire_bytes,bytes_per_s,avg_capture_b_per_msg}}
      dead_markets      expected tickers that sent zero book messages
      other_channels    counts of non-book channels seen (subscribed, ticker…)
      parse_errors      undecodable lines (counted, never fatal — D3)
    """
    tiers = tiers or {}
    per_market: dict[str, dict] = {}
    other: dict[str, int] = {}
    parse_errors = 0
    t_min = None
    t_max = None

    with open(capture_path, "rb") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except Exception:
                parse_errors += 1
                continue
            ch = o.get("channel", "?")
            if ch not in BOOK_CHANNELS:
                other[ch] = other.get(ch, 0) + 1
                continue
            tick = o.get("source_ticker", "") or "?"
            wall = o.get("recv_wall_ns")
            if isinstance(wall, (int, float)):
                t_min = wall if t_min is None else min(t_min, wall)
                t_max = wall if t_max is None else max(t_max, wall)
            m = per_market.setdefault(tick, {
                "tier": tiers.get(tick, "?"), "deltas": 0, "snapshots": 0,
                "capture_bytes": 0, "wire_bytes": 0,
            })
            if ch == "orderbook_delta":
                m["deltas"] += 1
            else:
                m["snapshots"] += 1
            m["capture_bytes"] += len(line)
            m["wire_bytes"] += len(o.get("raw", ""))

    duration_s = ((t_max - t_min) / 1e9) if (t_min is not None and t_max is not None and t_max > t_min) else 0.0
    total_msgs = sum(m["deltas"] + m["snapshots"] for m in per_market.values())

    for m in per_market.values():
        n = m["deltas"] + m["snapshots"]
        m["msgs_per_s"] = (n / duration_s) if duration_s > 0 else 0.0

    per_tier: dict[str, dict] = {}
    for m in per_market.values():
        t = per_tier.setdefault(m["tier"], {
            "markets": 0, "msgs": 0, "capture_bytes": 0, "wire_bytes": 0,
        })
        t["markets"] += 1
        t["msgs"] += m["deltas"] + m["snapshots"]
        t["capture_bytes"] += m["capture_bytes"]
        t["wire_bytes"] += m["wire_bytes"]
    for t in per_tier.values():
        t["msgs_per_s"] = (t["msgs"] / duration_s) if duration_s > 0 else 0.0
        t["bytes_per_s"] = (t["capture_bytes"] / duration_s) if duration_s > 0 else 0.0
        t["avg_capture_b_per_msg"] = (t["capture_bytes"] / t["msgs"]) if t["msgs"] else 0.0

    dead = [t for t in (expected_tickers or []) if t not in per_market]

    return {
        "capture_path": capture_path,
        "duration_s": duration_s,
        "total_msgs": total_msgs,
        "msgs_per_s": (total_msgs / duration_s) if duration_s > 0 else 0.0,
        "per_market": per_market,
        "per_tier": per_tier,
        "dead_markets": dead,
        "other_channels": other,
        "parse_errors": parse_errors,
    }


def print_report(r: dict) -> None:
    print("\n=== depth_probe capture report: %s ===" % r["capture_path"])
    print("duration %.1f s | book msgs %d | %.2f msg/s | parse errors %d"
          % (r["duration_s"], r["total_msgs"], r["msgs_per_s"], r["parse_errors"]))
    if r["other_channels"]:
        print("non-book channels: %s" % json.dumps(r["other_channels"], sort_keys=True))

    print("\nby liquidity tier:")
    print("  %-6s %8s %10s %10s %12s %12s %10s"
          % ("tier", "markets", "msgs", "msg/s", "capture_B", "B/s", "B/msg"))
    for tier in sorted(r["per_tier"]):
        t = r["per_tier"][tier]
        print("  %-6s %8d %10d %10.2f %12d %12.1f %10.1f"
              % (tier, t["markets"], t["msgs"], t["msgs_per_s"],
                 t["capture_bytes"], t["bytes_per_s"], t["avg_capture_b_per_msg"]))

    hot = sorted(r["per_market"].items(),
                 key=lambda kv: kv[1]["deltas"] + kv[1]["snapshots"], reverse=True)[:10]
    print("\nhottest markets:")
    print("  %-45s %-5s %8s %6s %10s %8s"
          % ("market", "tier", "deltas", "snaps", "capture_B", "msg/s"))
    for tick, m in hot:
        print("  %-45s %-5s %8d %6d %10d %8.2f"
              % (tick[:45], m["tier"], m["deltas"], m["snapshots"],
                 m["capture_bytes"], m["msgs_per_s"]))

    if r["dead_markets"]:
        print("\nWARNING: %d subscribed market(s) sent ZERO book messages "
              "(settled/dead — stale target list?):" % len(r["dead_markets"]))
        for t in r["dead_markets"]:
            print("  dead: %s" % t)
    print()


# --------------------------------------------------------------------------
# run mode
# --------------------------------------------------------------------------

def run_probe(args) -> int:
    if not args.operator_approved:
        return refuse("--operator-approved flag is missing")
    if os.environ.get("KALSHI_MODE", "").strip().lower() == "live":
        return refuse("KALSHI_MODE=live — this probe is read-only and never "
                      "runs in live mode (data_collect only)")

    csv_path = args.csv or newest_depth_target_csv()
    if not csv_path or not os.path.exists(csv_path):
        sys.stderr.write("depth_probe: no depth-target CSV found (work/mm/depth_target_*.csv)\n")
        return 1
    tickers, tiers = load_depth_target(csv_path, top_n=args.markets)
    if not tickers:
        sys.stderr.write("depth_probe: %s has no market_ticker rows\n" % csv_path)
        return 1

    today = time.strftime("%Y-%m-%d", time.gmtime())
    if today not in os.path.basename(csv_path):
        sys.stderr.write(
            "depth_probe: WARNING: target list %s is not dated today (%s) — "
            "settled markets subscribe cleanly and stream nothing; expect "
            "dead subscriptions and understated rates.\n"
            % (os.path.basename(csv_path), today))

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    capture = args.capture or os.path.join(PROBE_DIR, "depth_probe_%s.ndjson" % ts)
    norm = os.path.normpath(capture)
    # W6-audit F1 fix: compare REAL absolute paths (symlinks resolved), case-
    # folded (APFS is case-insensitive) — the old relative-string prefix check
    # was bypassable with an absolute path, and a capture placed under
    # work/raw/ would be tailed into the warehouse by the ingester's glob.
    # casefold explicitly: os.path.normcase is a no-op on posix/macOS, but APFS
    # is case-insensitive, so work/RAW hits the same inode as work/raw.
    real_cap = os.path.realpath(os.path.abspath(norm)).casefold()
    real_raw = os.path.realpath(os.path.join(ROOT, "work", "raw")).casefold()
    if real_cap == real_raw or real_cap.startswith(real_raw + os.sep):
        return refuse("capture path '%s' resolves inside work/raw/ — the "
                      "production capture tree is off-limits (double-writer "
                      "lesson)" % capture)
    metrics = norm + ".metrics.ndjson"

    env = dict(os.environ)
    env.pop("KALSHI_WS_FIREHOSE", None)          # never all-markets here
    env["KALSHI_MODE"] = "data_collect"          # forced; ws_shadow re-refuses live
    env["KALSHI_WS_CHANNELS"] = "orderbook_delta"
    env["KALSHI_WS_TICKERS"] = ",".join(tickers)
    env["KALSHI_SHADOW_SECONDS"] = str(args.seconds)
    env["KALSHI_SHADOW_CAPTURE"] = norm
    env["KALSHI_SHADOW_METRICS"] = metrics       # own path, never work/metrics.ndjson
    env["KALSHI_SHADOW_XCHECK"] = "0"            # zero REST tokens

    binary = os.path.join(ROOT, "build", "ws_shadow")
    print("depth_probe: %d markets (top of %s), %d s, capture=%s"
          % (len(tickers), os.path.relpath(csv_path, ROOT), args.seconds, norm))

    if args.dry_run:
        print("dry-run: would exec %s with:" % binary)
        for k in ("KALSHI_MODE", "KALSHI_WS_CHANNELS", "KALSHI_SHADOW_SECONDS",
                  "KALSHI_SHADOW_CAPTURE", "KALSHI_SHADOW_METRICS", "KALSHI_SHADOW_XCHECK"):
            print("  %s=%s" % (k, env[k]))
        print("  KALSHI_WS_TICKERS=%s%s" % (",".join(tickers[:5]),
              ",… (%d total)" % len(tickers) if len(tickers) > 5 else ""))
        print("dry-run: no socket opened, exiting 0")
        return 0

    if not os.path.exists(binary):
        sys.stderr.write("depth_probe: %s not built (run make)\n" % binary)
        return 1
    os.makedirs(os.path.join(ROOT, PROBE_DIR), exist_ok=True)

    rc = subprocess.call([binary], env=env, cwd=ROOT)
    print("depth_probe: ws_shadow exited rc=%d" % rc)
    cap_abs = os.path.join(ROOT, norm) if not os.path.isabs(norm) else norm
    if not os.path.exists(cap_abs):
        sys.stderr.write("depth_probe: no capture file produced (%s)\n" % norm)
        return rc or 1
    print_report(analyze_capture(cap_abs, tiers, expected_tickers=tickers))
    print("depth_probe: paste the tier table into %s §2/§3 (PROBE-PENDING marks)." % PLAN_DOC)
    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--operator-approved", action="store_true",
                   help="operator's explicit approval for the bounded probe (see %s)" % PLAN_DOC)
    p.add_argument("--markets", type=int, default=50, help="top-N markets from the target list")
    p.add_argument("--seconds", type=int, default=900, help="probe window (bounded)")
    p.add_argument("--csv", help="depth-target CSV (default: newest work/mm/depth_target_*.csv)")
    p.add_argument("--capture", help="capture path (default: work/probe/depth_probe_<ts>.ndjson)")
    p.add_argument("--dry-run", action="store_true", help="print plan, open no socket")
    p.add_argument("--analyze", metavar="CAPTURE",
                   help="offline: analyze an existing capture file and exit (no gate needed)")
    args = p.parse_args(argv)

    if args.analyze:
        tiers = {}
        csv_path = args.csv or newest_depth_target_csv()
        if csv_path and os.path.exists(csv_path):
            _, tiers = load_depth_target(csv_path)
        if not os.path.exists(args.analyze):
            sys.stderr.write("depth_probe: no such capture: %s\n" % args.analyze)
            return 1
        print_report(analyze_capture(args.analyze, tiers))
        return 0

    return run_probe(args)


if __name__ == "__main__":
    sys.exit(main())
