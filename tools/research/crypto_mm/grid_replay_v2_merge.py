#!/usr/bin/env python3
"""Merge grid_replay_v2 partition dumps into the final TRAIN/VALIDATE report.

Usage: grid_replay_v2_merge.py <out_dir_with_partial_*.json>
Splits partials by date membership (TRAIN 07-12..19 / VALIDATE 07-20..23),
rebuilds per-arm ArmState aggregates, and emits the same arm report blocks
(mean + cluster-bootstrap CI) as a full serial run would.
"""
import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from grid_replay_v2 import (ArmState, DATES_TRAIN, DATES_VAL, GRID_ARMS,
                            arm_report_block)


def main():
    out_dir = sys.argv[1]
    partials = sorted(glob.glob(os.path.join(out_dir, "partial_*.json")))
    if not partials:
        print("no partials found", file=sys.stderr)
        sys.exit(1)

    shas = set()
    seen_dates = set()
    seg_aggs = {"TRAIN": {a.name: ArmState() for a in GRID_ARMS},
                "VALIDATE": {a.name: ArmState() for a in GRID_ARMS}}
    for pth in partials:
        d = json.load(open(pth))
        shas.add(d.get("script_sha256"))
        dates = d["dates"]
        dup = seen_dates & set(dates)
        if dup:
            print(f"DUPLICATE DATES across partials: {sorted(dup)} — VOID",
                  file=sys.stderr)
            sys.exit(1)
        seen_dates |= set(dates)
        segs = set()
        for dt_ in dates:
            if dt_ in DATES_TRAIN:
                segs.add("TRAIN")
            elif dt_ in DATES_VAL:
                segs.add("VALIDATE")
            else:
                print(f"unknown date {dt_} — VOID", file=sys.stderr)
                sys.exit(1)
        if len(segs) != 1:
            print(f"partial {pth} mixes segments — VOID", file=sys.stderr)
            sys.exit(1)
        seg = segs.pop()
        for name, sd in d["arms"].items():
            st = seg_aggs[seg][name]
            st.placed += sd["placed"]
            st.fills += sd["fills"]
            st.pnl.extend(sd["pnl"])
            st.pairs.extend((mt, p) for mt, p in sd["pairs"])
            st.markets |= set(sd["markets"])
            st.clusters |= set(sd["clusters"])
            st.q_peak = max(st.q_peak, sd["q_peak"])
            st.requote_suppressed += sd["requote_suppressed"]
            st.carried_ct += sd["carried_ct"]
            st.paired_ct += sd["paired_ct"]
            st.flattened_ct += sd["flattened_ct"]
            st.shield_pulls += sd.get("shield_pulls", 0)
            st.harvest_blocks += sd.get("harvest_blocks", 0)
            st.fill_ts.extend(sd.get("fill_ts", []))

    if len(shas) != 1:
        # The recorded sha is hashed from the file ON DISK at partial-write
        # time; replacing the script on disk while already-loaded partitions
        # are still running yields mixed shas even though every process ran
        # identical loaded code.  That exact situation must be attested
        # explicitly; anything else stays VOID.
        if os.environ.get("GRID2_ALLOW_MIXED_SHA") == "1":
            print(f"WARNING mixed on-disk shas {sorted(shas)} — proceeding "
                  "under GRID2_ALLOW_MIXED_SHA=1 (operator attested that all "
                  "partitions were launched from identical loaded code)",
                  file=sys.stderr)
        else:
            print(f"MIXED SCRIPT SHAS {shas} — VOID", file=sys.stderr)
            sys.exit(1)

    missing = (set(DATES_TRAIN) | set(DATES_VAL)) - seen_dates
    report = {"schema_version": "grid-replay-v2-merged",
              "script_sha256": shas.pop(),
              "partials": [os.path.basename(p) for p in partials],
              "missing_dates": sorted(missing)}
    for seg in ("TRAIN", "VALIDATE"):
        report[f"grid_{seg}"] = arm_report_block(seg_aggs[seg])
    out = os.path.join(out_dir, "merged_report.json")
    json.dump(report, open(out, "w"), indent=1)
    print("merged report:", out)
    if missing:
        print("WARNING missing dates:", sorted(missing))
    for seg in ("TRAIN", "VALIDATE"):
        print(f"\n== grid_{seg} ==")
        print(json.dumps(report[f"grid_{seg}"], indent=1))


if __name__ == "__main__":
    main()
