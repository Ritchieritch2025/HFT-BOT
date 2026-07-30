#!/usr/bin/env python3
"""Read-only AWS footprint + monthly-cost inventory.

Answers one question: what is still billing while the project is paused?

Credentials come from ~/.kalshi/research_s3.env.sh (exported AWS_* lines) or
the ambient environment. Every call here is a describe/list — nothing mutates.

    python3 tools/ops/cloud_cost_inventory.py [--region us-east-2]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ENV_FILE = Path.home() / ".kalshi" / "research_s3.env.sh"

# us-east-2 on-demand / storage rates, USD. Sourced from AWS public pricing.
HOURS_PER_MONTH = 730.0
GP3_GB_MONTH = 0.08
GP2_GB_MONTH = 0.10
SNAPSHOT_GB_MONTH = 0.05
SNAPSHOT_ARCHIVE_GB_MONTH = 0.0125
IDLE_PUBLIC_IPV4_HOUR = 0.005
S3_STANDARD_GB_MONTH = 0.023
S3_DEEP_ARCHIVE_GB_MONTH = 0.00099
S3_GLACIER_IR_GB_MONTH = 0.004
INSTANCE_HOUR = {
    "r8g.large": 0.10584,
    "r8g.xlarge": 0.21168,
    "r8g.2xlarge": 0.42336,
    "t4g.medium": 0.0336,
}


def load_env() -> None:
    """Export AWS_* assignments from the operator's env file, if present."""
    if not ENV_FILE.exists():
        return
    pat = re.compile(r"^\s*(?:export\s+)?(AWS_[A-Z_]+)=(.*)$")
    for line in ENV_FILE.read_text().splitlines():
        m = pat.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip("'\"")
        os.environ.setdefault(key, val)


def money(x: float) -> str:
    return f"${x:,.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
    ap.add_argument("--bucket", action="append", default=[],
                    help="measure this bucket by name (use when ListBuckets is denied)")
    args = ap.parse_args()

    load_env()
    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        print("boto3 not installed", file=sys.stderr)
        return 2

    region = args.region
    total = 0.0
    denied: list[str] = []

    def probe(label, fn):
        nonlocal denied
        try:
            return fn()
        except Exception as exc:  # ClientError, NoCredentialsError, EndpointError
            denied.append(f"{label}: {type(exc).__name__} {str(exc)[:150]}")
            return None

    print("=" * 72)
    ident = probe("sts:GetCallerIdentity", lambda: boto3.client("sts", region_name=region).get_caller_identity())
    if ident:
        print(f"account {ident['Account']}  arn {ident['Arn']}")
    print(f"region  {region}")
    print("=" * 72)

    ec2 = boto3.client("ec2", region_name=region)

    # ---- instances ----
    res = probe("ec2:DescribeInstances", lambda: ec2.describe_instances())
    if res:
        print("\n## EC2 instances")
        for r in res["Reservations"]:
            for i in r["Instances"]:
                state = i["State"]["Name"]
                if state == "terminated":
                    continue
                itype = i["InstanceType"]
                rate = INSTANCE_HOUR.get(itype, 0.0)
                cost = rate * HOURS_PER_MONTH if state == "running" else 0.0
                total += cost
                name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "-")
                print(f"  {i['InstanceId']}  {itype:14s} {state:10s} {name:24s} "
                      f"compute {money(cost)}/mo")
                if state == "running" and rate:
                    print(f"      ^^ BURNING {money(rate * 24)}/day")

    # ---- volumes ----
    vols = probe("ec2:DescribeVolumes", lambda: ec2.describe_volumes())
    if vols:
        print("\n## EBS volumes")
        for v in vols["Volumes"]:
            gb = v["Size"]
            rate = GP2_GB_MONTH if v["VolumeType"] == "gp2" else GP3_GB_MONTH
            cost = gb * rate
            total += cost
            att = ", ".join(a["InstanceId"] for a in v.get("Attachments", [])) or "UNATTACHED"
            print(f"  {v['VolumeId']}  {gb:5d} GB {v['VolumeType']:5s} {v['State']:10s} "
                  f"{att:22s} {money(cost)}/mo")

    # ---- elastic IPs ----
    addrs = probe("ec2:DescribeAddresses", lambda: ec2.describe_addresses())
    if addrs:
        print("\n## Elastic IPs")
        for a in addrs["Addresses"]:
            cost = IDLE_PUBLIC_IPV4_HOUR * HOURS_PER_MONTH
            total += cost
            print(f"  {a.get('AllocationId','-')}  {a['PublicIp']:16s} "
                  f"assoc={a.get('InstanceId', 'NONE')!s:22s} {money(cost)}/mo")

    # ---- snapshots ----
    snaps = probe("ec2:DescribeSnapshots", lambda: ec2.describe_snapshots(OwnerIds=["self"]))
    if snaps:
        print("\n## EBS snapshots")
        gb = sum(s["VolumeSize"] for s in snaps["Snapshots"])
        cost = gb * SNAPSHOT_GB_MONTH
        total += cost
        print(f"  {len(snaps['Snapshots'])} snapshots, {gb} GB nominal, "
              f"<= {money(cost)}/mo (billed on used blocks, so this is an upper bound)")

    # ---- S3 ----
    buckets = probe("s3:ListBuckets", lambda: boto3.client("s3", region_name=region).list_buckets())
    named = [{"Name": n} for n in args.bucket]
    if buckets or named:
        print("\n## S3 buckets")
        s3 = boto3.client("s3", region_name=region)
        seen: set[str] = set()
        for b in (buckets["Buckets"] if buckets else []) + named:
            name = b["Name"]
            if name in seen:
                continue
            seen.add(name)
            by_class: dict[str, list[int]] = {}
            nbytes = nobj = 0
            noncurrent = 0
            try:
                pag = s3.get_paginator("list_object_versions")
                for page in pag.paginate(Bucket=name):
                    for o in page.get("Versions", []):
                        cls = o.get("StorageClass", "STANDARD")
                        by_class.setdefault(cls, [0, 0])
                        by_class[cls][0] += o["Size"]
                        by_class[cls][1] += 1
                        nbytes += o["Size"]
                        nobj += 1
                        if not o["IsLatest"]:
                            noncurrent += o["Size"]
            except Exception as exc:
                denied.append(f"s3:ListObjectVersions {name}: {str(exc)[:120]}")
                continue
            ver = probe(f"s3:GetBucketVersioning {name}",
                        lambda: s3.get_bucket_versioning(Bucket=name)) or {}
            lc = "none"
            try:
                s3.get_bucket_lifecycle_configuration(Bucket=name)
                lc = "PRESENT"
            except Exception:
                pass
            cost = 0.0
            for cls, (bts, cnt) in sorted(by_class.items()):
                rate = {"STANDARD": S3_STANDARD_GB_MONTH,
                        "GLACIER_IR": S3_GLACIER_IR_GB_MONTH,
                        "DEEP_ARCHIVE": S3_DEEP_ARCHIVE_GB_MONTH}.get(cls, S3_STANDARD_GB_MONTH)
                c = bts / 1e9 * rate
                cost += c
                print(f"  {name}/{cls:14s} {bts/1e9:9.1f} GB {cnt:7d} obj  {money(c)}/mo")
            total += cost
            print(f"    versioning={ver.get('Status','Disabled')} lifecycle={lc} "
                  f"non-current={noncurrent/1e9:.1f} GB  total {money(cost)}/mo")
            if nbytes:
                print(f"    -> if all DEEP_ARCHIVE: "
                      f"{money(nbytes/1e9*S3_DEEP_ARCHIVE_GB_MONTH)}/mo")

    print("\n" + "=" * 72)
    print(f"TOTAL RECURRING (measured surfaces): {money(total)}/mo  = {money(total*12)}/yr")
    print("=" * 72)
    if denied:
        print("\n## no permission / not measured")
        for d in denied:
            print(f"  - {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
