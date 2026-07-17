#!/usr/bin/env python3
"""Stream an S3 L2 day through l2_gap_check without writing production state.

This audit adapter performs only ListObjectsV2 and GetObject calls.  It prints a
canonical summary receipt to stdout and never invokes l2_gap_check's writing
CLI.  The detector and this adapter can be transported to a production host in
memory (for example, as a tar stream read by ``python -c``) so the host remains
read-only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import time

import l2_gap_check


_SHARD_RE = re.compile(r"^(?P<base>l2_\d{2})\.ndjson(?:\.(?P<shard>\d+))?$")


def list_inventory(client, bucket, prefix):
    inventory = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = os.path.basename(key)
            match = _SHARD_RE.match(name)
            if not match:
                continue
            inventory.append({
                "base": match.group("base"),
                "shard": int(match.group("shard") or 0),
                "name": name,
                "file": "s3://%s/%s" % (bucket, key),
                "bytes": int(obj["Size"]),
                "etag": str(obj.get("ETag", "")).strip('"'),
                "ref": key,
            })
    inventory.sort(key=lambda item: (item["base"], item["shard"], item["ref"]))
    return inventory


def manifest_sha256(inventory):
    lines = ["%s\t%d\t%s\n" % (item["ref"], item["bytes"], item["etag"])
             for item in inventory]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def scan_s3(client, date_str, bucket, prefix):
    inventory = list_inventory(client, bucket, prefix)

    def object_lines(item):
        body = client.get_object(Bucket=bucket, Key=item["ref"])["Body"]
        try:
            yield from body.iter_lines(chunk_size=64 * 1024, keepends=False)
        finally:
            body.close()

    raw_root = "s3://%s/%s" % (bucket, prefix.rstrip("/"))
    record = l2_gap_check.scan_inventory(
        date_str, raw_root, inventory, object_lines)
    return record, inventory


def build_summary(record, inventory, args, started, finished):
    omitted = {"per_market"}
    summary = {key: value for key, value in record.items() if key not in omitted}
    summary.update({
        "audit_schema": "l2-gap-s3-readonly-v1",
        "code_commit": args.code_commit,
        "detector_sha256": args.detector_sha256,
        "adapter_sha256": args.adapter_sha256,
        "input_manifest_sha256": manifest_sha256(inventory),
        "inventory_etags": [
            {"file": item["file"], "bytes": item["bytes"], "etag": item["etag"]}
            for item in inventory
        ],
        "per_market_count": len(record["per_market"]),
        "scan_started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scan_finished_utc": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scan_elapsed_seconds": round((finished - started).total_seconds(), 3),
    })
    payload = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    summary["receipt_payload_sha256"] = hashlib.sha256(
        payload.encode("utf-8")).hexdigest()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--detector-sha256", required=True)
    parser.add_argument("--adapter-sha256", required=True)
    args = parser.parse_args(argv)
    dt.date.fromisoformat(args.date)

    # Delayed import keeps the detector's offline/local path dependency-free.
    import boto3

    client = boto3.client("s3")
    started = dt.datetime.now(dt.timezone.utc)
    t0 = time.monotonic()
    record, inventory = scan_s3(client, args.date, args.bucket, args.prefix)
    finished = dt.datetime.now(dt.timezone.utc)
    # Use monotonic duration for the receipt even if the wall clock steps.
    elapsed = time.monotonic() - t0
    summary = build_summary(record, inventory, args, started, finished)
    summary["scan_elapsed_seconds"] = round(elapsed, 3)
    payload = dict(summary)
    payload.pop("receipt_payload_sha256", None)
    summary["receipt_payload_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
