#!/usr/bin/env python3
"""W-A4 dual-machine capture-completeness diff (sandbox, P8).

Compares two machines' firehose captures over a common wall-clock window,
aligned by MESSAGE CONTENT (not per-connection seq — Kalshi's ws seq counters
are per-subscription, so they are NOT comparable across connections; the
plan's "aligned by ws_seq" is implemented as: seq proves *within*-machine
continuity, content-multiset proves *cross*-machine completeness).

Key = (channel, canonical inner-frame JSON with connection-specific fields
dropped: top-level id/sid/seq). Trades additionally carry a globally unique
trade_id inside msg, making their diff exact. Multiset semantics (a key seen
3x on A and 2x on B counts as 1 missing on B).

Usage:
  python3 sandbox/wa4_capture_diff.py --a macfile1 [macfile2 ...] \
      --b ec2file1 [...] --start-ns N --end-ns N --label-a mac --label-b ec2
Window is [start,end) on the capture envelope's recv_wall_ns (both boxes
chrony/sntp-synced; edges trimmed by the caller to dodge warmup asymmetry).
"""
import argparse
import json
import sys
from collections import Counter


def load(paths, start_ns, end_ns):
    c = Counter()
    total = 0
    skipped = 0
    for p in paths:
        with open(p, "r", errors="replace") as f:
            for line in f:
                try:
                    env = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                ts = env.get("recv_wall_ns")
                ch = env.get("channel", "")
                if ts is None or not (start_ns <= ts < end_ns):
                    continue
                if ch in ("subscribed", "system", ""):
                    continue  # connection-local, legitimately differs
                try:
                    frame = json.loads(env["raw"])
                except (KeyError, json.JSONDecodeError):
                    skipped += 1
                    continue
                frame.pop("id", None)
                frame.pop("sid", None)
                frame.pop("seq", None)
                key = (ch, json.dumps(frame, sort_keys=True))
                c[key] += 1
                total += 1
    return c, total, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", nargs="+", required=True)
    ap.add_argument("--b", nargs="+", required=True)
    ap.add_argument("--start-ns", type=int, required=True)
    ap.add_argument("--end-ns", type=int, required=True)
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    args = ap.parse_args()

    ca, na, ska = load(args.a, args.start_ns, args.end_ns)
    cb, nb, skb = load(args.b, args.start_ns, args.end_ns)

    missing_b = ca - cb   # on A, not on B
    missing_a = cb - ca   # on B, not on A
    mb = sum(missing_b.values())
    ma = sum(missing_a.values())

    def by_channel(cnt):
        out = Counter()
        for (ch, _), n in cnt.items():
            out[ch] += n
        return dict(out)

    print(f"window_ns=[{args.start_ns},{args.end_ns})")
    print(f"{args.label_a}: records={na} skipped={ska} by_channel={by_channel(ca)}")
    print(f"{args.label_b}: records={nb} skipped={skb} by_channel={by_channel(cb)}")
    print(f"missing_on_{args.label_b}(present_on_{args.label_a})={mb} "
          f"by_channel={by_channel(missing_b)}")
    print(f"missing_on_{args.label_a}(present_on_{args.label_b})={ma} "
          f"by_channel={by_channel(missing_a)}")
    denom = max(na, nb)
    if denom:
        print(f"single_machine_miss_rate_{args.label_a}={ma/denom:.6%}")
        print(f"single_machine_miss_rate_{args.label_b}={mb/denom:.6%}")
    for name, miss in ((args.label_b, missing_b), (args.label_a, missing_a)):
        for (ch, frame), n in list(miss.items())[:3]:
            print(f"SAMPLE missing_on_{name} x{n} [{ch}] {frame[:180]}")


if __name__ == "__main__":
    main()
