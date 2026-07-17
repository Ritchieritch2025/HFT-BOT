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
     seal_invalidations.ndjson, AND
  4. a separate ``canonical-raw-prune-authority-v1`` file enumerates every
     local byte with its full-SHA-verified, non-null S3 VersionId.

Research publication receipts intentionally carry ``prune_eligible=false``
and are never accepted as prune authority.  Until a dedicated authority is
present, overdue raw is retained and alerted rather than deleted.

Fail-closed planning: all parse, seal, dependency, authority and full-SHA gates
for every day finish before the first unlink. Every retained-overdue file is
listed with its reason in work/live/raw_retention_alert.json (D2: nothing
silently skipped). Exit 0 on success (even with retained files), 1 on error;
an unlink-time filesystem failure reports the exact partial-delete count.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse_common as wc  # noqa: E402

CROSS_DAY_HOURS = 2
PRUNE_AUTHORITY_SCHEMA = "canonical-raw-prune-authority-v1"
PRUNE_AUTHORITY_STATE = "RAW_PRUNE_AUTHORIZED"
PRUNE_AUTHORITY_NAME = "PRUNE_AUTHORITY.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RFQ_NAME_RE = re.compile(
    r"^rfq(?:_receipts)?_\d{2}\.ndjson(?:\.\d+)?$")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_prune_authority(root, day, ddir):
    """Validate one local index produced by a separate S3 durability gate."""
    path = os.path.join(root, "date=%s" % day, PRUNE_AUTHORITY_NAME)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        authority = json.load(handle)
    required = {
        "schema_version", "state", "authority", "date", "complete",
        "prune_eligible", "receipt_set_sha256", "receipt_object", "objects",
    }
    if (not isinstance(authority, dict) or set(authority) != required
            or authority.get("schema_version") != PRUNE_AUTHORITY_SCHEMA
            or authority.get("state") != PRUNE_AUTHORITY_STATE
            or authority.get("authority")
            != "CANONICAL_RAW_PRUNE_CONTROL_PLANE"
            or authority.get("date") != day
            or authority.get("complete") is not True
            or authority.get("prune_eligible") is not True
            or SHA256_RE.fullmatch(
                str(authority.get("receipt_set_sha256") or "")) is None):
        raise RuntimeError("invalid raw prune authority for %s" % day)
    receipt = authority.get("receipt_object")
    if (not isinstance(receipt, dict)
            or set(receipt) != {
                "bucket", "key", "VersionId", "size", "sha256",
                "verification_state"}
            or receipt.get("bucket") != "kalshi-vault-ritcardo"
            or receipt.get("key") != (
                "ec2/control/raw-prune-authority/v1/date=%s/receipt-%s.json" %
                (day, authority["receipt_set_sha256"]))
            or not isinstance(receipt.get("VersionId"), str)
            or not receipt["VersionId"]
            or receipt["VersionId"].lower() == "null"
            or not isinstance(receipt.get("size"), int)
            or isinstance(receipt.get("size"), bool) or receipt["size"] <= 0
            or SHA256_RE.fullmatch(str(receipt.get("sha256") or "")) is None
            or receipt.get("verification_state")
            != "EXACT_VERSION_FULL_SHA256"):
        raise RuntimeError("invalid exact prune receipt binding for %s" % day)
    rows = authority.get("objects")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("empty raw prune authority for %s" % day)
    expected = {}
    for row in rows:
        fields = {
            "local_raw_rel", "bucket", "key", "VersionId", "size",
            "sha256", "verification_state",
        }
        if not isinstance(row, dict) or set(row) != fields:
            raise RuntimeError("malformed prune object for %s" % day)
        rel = row.get("local_raw_rel")
        if (not isinstance(rel, str)
                or not rel.startswith("date=%s/" % day)
                or rel.count("/") != 1
                or row.get("bucket") != "kalshi-vault-ritcardo"
                or row.get("key") != "ec2/raw/" + rel
                or not isinstance(row.get("VersionId"), str)
                or not row["VersionId"]
                or row["VersionId"].lower() == "null"
                or not isinstance(row.get("size"), int)
                or isinstance(row.get("size"), bool) or row["size"] < 0
                or SHA256_RE.fullmatch(str(row.get("sha256") or "")) is None
                or row.get("verification_state")
                != "EXACT_VERSION_FULL_SHA256"
                or rel in expected):
            raise RuntimeError("invalid prune object for %s" % day)
        expected[rel] = row
    actual = {
        "date=%s/%s" % (day, name): os.path.join(ddir, name)
        for name in sorted(os.listdir(ddir))
        if os.path.isfile(os.path.join(ddir, name))
    }
    # Re-runs are monotonic: rows already pruned by an earlier successful
    # pass may be absent, while every still-local byte must remain a member of
    # the original exact authority. A newly appeared/unattested local file is
    # always a hard mismatch.
    extra = set(actual).difference(expected)
    non_rfq_extra = [rel for rel in extra
                     if RFQ_NAME_RE.fullmatch(rel.rsplit("/", 1)[-1]) is None]
    if non_rfq_extra:
        raise RuntimeError("prune authority/local set has new bytes for %s" %
                           day)
    for rel, fpath in actual.items():
        if rel not in expected:
            # Current RFQ is intentionally excluded from byte-attestation
            # receipts under the operator's no-more-audit ruling.  Retain it
            # per object; do not let that deferred family block pruning of
            # independently attested firehose/L2 bytes from the same day.
            continue
        row = expected[rel]
        if (os.path.getsize(fpath) != row["size"]
                or _sha256_file(fpath) != row["sha256"]):
            raise RuntimeError("local raw differs from prune authority: %s" %
                               rel)
    return expected


def _sealed(warehouse_root, date):
    # PIPE-W03: single shared definition (also gates the ingest scanner).
    return wc.day_sealed(warehouse_root, date)


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
    ap.add_argument(
        "--prune-authority-root", default=None,
        help="separate canonical raw-prune authority root; missing means retain")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv[1:])
    cfg = wc.load_config()
    raw_root = os.path.abspath(args.raw_root or cfg["raw_root"])
    warehouse_root = args.warehouse_root or cfg["warehouse_root"]
    alert_path = args.alert_path or os.path.join("work", "live",
                                                 "raw_retention_alert.json")
    authority_root = os.path.abspath(
        args.prune_authority_root or os.path.join(
            "work", "live", "canonical_receipts", "prune-authority"))
    today = datetime.datetime.now(datetime.timezone.utc).date()
    cutoff = (today - datetime.timedelta(days=args.retention_days)).isoformat()

    deleted, planned, retained = [], [], []
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
            authority = (None if not day_sealed else
                         _load_prune_authority(authority_root, day, ddir))
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
                rel = "date=%s/%s" % (day, fn)
                if authority is None or rel not in authority:
                    retained.append({
                        "file": fpath,
                        "reason": ("rfq_receipt_deferred"
                                   if authority is not None
                                   and RFQ_NAME_RE.fullmatch(fn)
                                   else "durable_prune_authority_absent"),
                    })
                    continue
                planned.append(fpath)

        # Two-phase safety: every seal/dependency/authority file and every
        # local SHA is validated before the first unlink occurs.
        if not args.dry_run:
            for fpath in planned:
                os.remove(fpath)
                deleted.append(fpath)
            for entry in sorted(os.listdir(raw_root)):
                if not entry.startswith("date="):
                    continue
                ddir = os.path.join(raw_root, entry)
                if os.path.isdir(ddir) and not os.listdir(ddir):
                    os.rmdir(ddir)
    except Exception as e:
        report = {"checked_at_utc": datetime.datetime.now(datetime.timezone.utc)
                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "error": str(e), "deleted": len(deleted),
                  "retained_overdue": retained}
        os.makedirs(os.path.dirname(alert_path), exist_ok=True)
        with open(alert_path, "w") as f:
            json.dump(report, f, indent=2)
        print("PRUNE FAIL-CLOSED (deleted_before_io_error=%d): %s" %
              (len(deleted), e), file=sys.stderr)
        return 1

    report = {"checked_at_utc": datetime.datetime.now(datetime.timezone.utc)
              .strftime("%Y-%m-%dT%H:%M:%SZ"),
              "retention_days": args.retention_days, "cutoff": cutoff,
              "dry_run": args.dry_run, "planned": len(planned),
              "deleted": len(deleted),
              "prune_authority_root": authority_root,
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
