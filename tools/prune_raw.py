#!/usr/bin/env python3
"""Seal-gated raw pruning (PIPE-R001 W02).

Reconciles two operator rulings:
- 2026-07-10: RAW_RETENTION_DAYS 3->2 (raw is vaulted to S3 hourly; disk math).
- 2026-07-11: day seals are the evidence gate; raw is verified byte-complete
  ONCE at seal time, so LOCAL raw may be pruned AFTER (and only after) the
  covering seals exist.

A receipt-partition day date=R may be deleted only when:
  1. R itself is SEALED (its facts are attested), AND
  2. R's cross-day hours (00/01 feed day R-1's seal) are not needed: R-1 is
     sealed too, or the file is not an hour-00/01 file, AND
  3. no unsealed exchange day lists the file as a late-raw dependency in
     seal_invalidations.ndjson.

Fail-closed: any parse/IO error deletes NOTHING. Every retained-overdue file
is listed with its reason in work/live/raw_retention_alert.json (D2: nothing
silently skipped). Exit 0 on success (even with retained files), 1 on error.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

CROSS_DAY_HOURS = 2


def _sealed(warehouse_root, date):
    path = wc.seal_path(warehouse_root, date)
    if not os.path.isfile(path):
        return False
    try:
        with open(path) as f:
            seal = json.load(f)
    except (OSError, ValueError):
        return False
    return seal.get("status") == "SEALED" and seal.get("version") == 2


def _unsealed_dependencies(warehouse_root, raw_root):
    """Raw paths any UNSEALED exchange day still depends on (never delete)."""
    ledger = os.path.join(warehouse_root, "seal_invalidations.ndjson")
    keep = set()
    if not os.path.isfile(ledger):
        return keep
    with open(ledger, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                raise RuntimeError("corrupt seal_invalidations.ndjson line")
            day = row.get("exchange_date")
            rel = row.get("source_raw_rel")
            if day and rel and not _sealed(warehouse_root, day):
                keep.add(os.path.abspath(os.path.join(raw_root, rel)))
    return keep


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--retention-days", type=int, required=True)
    ap.add_argument("--raw-root", default=None)
    ap.add_argument("--warehouse-root", default=None)
    ap.add_argument("--alert-path", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv[1:])
    cfg = wc.load_config()
    raw_root = os.path.abspath(args.raw_root or cfg["raw_root"])
    warehouse_root = args.warehouse_root or cfg["warehouse_root"]
    alert_path = args.alert_path or os.path.join("work", "live",
                                                 "raw_retention_alert.json")
    today = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = (today - datetime.timedelta(days=args.retention_days)).isoformat()

    deleted, retained = [], []
    try:
        dep_keep = _unsealed_dependencies(warehouse_root, raw_root)
        for entry in sorted(os.listdir(raw_root)):
            if not entry.startswith("date="):
                continue
            day = entry[len("date="):]
            if day > cutoff:
                continue
            ddir = os.path.join(raw_root, entry)
            day_sealed = _sealed(warehouse_root, day)
            prev = (datetime.date.fromisoformat(day)
                    - datetime.timedelta(days=1)).isoformat()
            prev_sealed = _sealed(warehouse_root, prev)
            for fn in sorted(os.listdir(ddir)):
                fpath = os.path.abspath(os.path.join(ddir, fn))
                if not os.path.isfile(fpath):
                    continue
                if not day_sealed:
                    retained.append({"file": fpath, "reason": "day_unsealed"})
                    continue
                hour = None
                stem = fn.split(".ndjson")[0]
                if "_" in stem and stem.rsplit("_", 1)[1].isdigit():
                    hour = int(stem.rsplit("_", 1)[1])
                if (hour is not None and hour < CROSS_DAY_HOURS
                        and not prev_sealed):
                    retained.append({"file": fpath,
                                     "reason": "cross_day_prev_unsealed"})
                    continue
                if fpath in dep_keep:
                    retained.append({"file": fpath,
                                     "reason": "unsealed_dependency"})
                    continue
                if not args.dry_run:
                    os.remove(fpath)
                deleted.append(fpath)
            if not args.dry_run and not os.listdir(ddir):
                os.rmdir(ddir)
    except Exception as e:
        report = {"checked_at_utc": datetime.datetime.now(datetime.timezone.utc)
                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "error": str(e), "deleted": 0, "retained_overdue": []}
        os.makedirs(os.path.dirname(alert_path), exist_ok=True)
        with open(alert_path, "w") as f:
            json.dump(report, f, indent=2)
        print("PRUNE FAIL-CLOSED (nothing deleted): %s" % e, file=sys.stderr)
        return 1

    report = {"checked_at_utc": datetime.datetime.now(datetime.timezone.utc)
              .strftime("%Y-%m-%dT%H:%M:%SZ"),
              "retention_days": args.retention_days, "cutoff": cutoff,
              "dry_run": args.dry_run, "deleted": len(deleted),
              "retained_overdue": retained}
    os.makedirs(os.path.dirname(alert_path), exist_ok=True)
    with open(alert_path, "w") as f:
        json.dump(report, f, indent=2)
    print("PRUNE %s: deleted=%d retained_overdue=%d (reasons in %s)"
          % ("DRY-RUN" if args.dry_run else "PASS", len(deleted), len(retained),
             alert_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
