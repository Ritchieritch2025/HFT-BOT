#!/usr/bin/env python3
"""Staged teardown of the paused project's AWS footprint. Dry-run by default.

Stages, cheapest-risk first. Each is opt-in via --stage; nothing runs without
--apply. Every destructive call prints what it will touch first.

    stage eip     release unassociated Elastic IPs            (-$3.65/mo each)
    stage s3      lifecycle: all objects -> DEEP_ARCHIVE,     (-95% of S3 bill)
                  expire non-current versions after 1 day
    stage stop    stop every running instance                 (-compute)
    stage snap    snapshot every volume, tagged, wait for it  (+$0.05/GB-mo)
    stage volume  delete volumes NOT attached to an instance  (-$0.08/GB-mo)
    stage term    terminate stopped instances                 (frees volumes)

Order for the full "keep the data, kill the bill" path:
    s3 -> stop -> snap -> term -> volume -> eip

    python3 tools/ops/cloud_teardown.py --stage s3 --stage eip           # preview
    python3 tools/ops/cloud_teardown.py --stage s3 --stage eip --apply   # do it
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from cloud_cost_inventory import load_env  # noqa: E402

DEEP_ARCHIVE_LIFECYCLE = {
    "Rules": [
        {
            "ID": "paused-project-deep-archive",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Transitions": [{"Days": 0, "StorageClass": "DEEP_ARCHIVE"}],
            "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
        }
    ]
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--stage", action="append", default=[],
                    choices=["eip", "s3", "stop", "snap", "volume", "term"], required=True)
    ap.add_argument("--bucket", action="append", default=["kalshi-vault-ritcardo"])
    ap.add_argument("--apply", action="store_true", help="actually mutate; default is preview")
    args = ap.parse_args()

    load_env()
    import boto3

    ec2 = boto3.client("ec2", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)
    do = args.apply
    tag = "[APPLY]" if do else "[dry-run]"

    def instances(states):
        out = []
        for r in ec2.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": states}]
        )["Reservations"]:
            out.extend(r["Instances"])
        return out

    if "s3" in args.stage:
        print(f"\n== s3 lifecycle -> DEEP_ARCHIVE ==")
        for b in args.bucket:
            print(f"  {tag} put_bucket_lifecycle_configuration({b})")
            print(f"         transition all objects to DEEP_ARCHIVE at day 0")
            print(f"         expire non-current versions after 1 day")
            if do:
                s3.put_bucket_lifecycle_configuration(
                    Bucket=b, LifecycleConfiguration=DEEP_ARCHIVE_LIFECYCLE)
                s3.put_bucket_versioning(
                    Bucket=b, VersioningConfiguration={"Status": "Suspended"})
                print(f"         done; versioning suspended")
        print("  NOTE: transition is asynchronous (hours). Deep Archive has a")
        print("        180-day minimum charge and 12-48h restore latency.")

    if "stop" in args.stage:
        print(f"\n== stop running instances ==")
        ids = [i["InstanceId"] for i in instances(["running"])]
        for i in ids:
            print(f"  {tag} stop_instances({i})")
        if do and ids:
            ec2.stop_instances(InstanceIds=ids)

    if "snap" in args.stage:
        print(f"\n== snapshot every volume ==")
        for v in ec2.describe_volumes()["Volumes"]:
            vid, gb = v["VolumeId"], v["Size"]
            print(f"  {tag} create_snapshot({vid}, {gb} GB) ~= ${gb*0.05:.2f}/mo upper bound")
            if do:
                snap = ec2.create_snapshot(
                    VolumeId=vid,
                    Description=f"paused-project teardown {vid}",
                    TagSpecifications=[{"ResourceType": "snapshot", "Tags": [
                        {"Key": "Name", "Value": f"teardown-{vid}"},
                        {"Key": "Project", "Value": "kalshi-hft-paused"},
                    ]}])
                print(f"         -> {snap['SnapshotId']}")
        if do:
            print("  waiting for snapshots to complete (this can take an hour)...")
            snaps = [s["SnapshotId"] for s in ec2.describe_snapshots(
                OwnerIds=["self"], Filters=[{"Name": "tag:Project",
                                             "Values": ["kalshi-hft-paused"]}])["Snapshots"]]
            while True:
                st = ec2.describe_snapshots(SnapshotIds=snaps)["Snapshots"]
                pend = [(s["SnapshotId"], s["Progress"]) for s in st
                        if s["State"] != "completed"]
                if not pend:
                    print("  all snapshots completed")
                    break
                print(f"  pending: {pend}")
                time.sleep(60)

    if "term" in args.stage:
        print(f"\n== terminate stopped instances ==")
        print("  !! IRREVERSIBLE. Volumes with DeleteOnTermination=false survive.")
        for i in instances(["stopped"]):
            iid = i["InstanceId"]
            keep = [bd["Ebs"]["VolumeId"] for bd in i.get("BlockDeviceMappings", [])
                    if not bd["Ebs"].get("DeleteOnTermination")]
            print(f"  {tag} terminate_instances({iid})  surviving volumes: {keep or 'none'}")
            if do:
                ec2.terminate_instances(InstanceIds=[iid])

    if "volume" in args.stage:
        print(f"\n== delete unattached volumes ==")
        print("  !! IRREVERSIBLE without a snapshot. Run --stage snap first.")
        for v in ec2.describe_volumes(
                Filters=[{"Name": "status", "Values": ["available"]}])["Volumes"]:
            vid, gb = v["VolumeId"], v["Size"]
            snaps = ec2.describe_snapshots(
                OwnerIds=["self"],
                Filters=[{"Name": "volume-id", "Values": [vid]},
                         {"Name": "status", "Values": ["completed"]}])["Snapshots"]
            if not snaps:
                print(f"  SKIP {vid} ({gb} GB): no completed snapshot exists")
                continue
            print(f"  {tag} delete_volume({vid}, {gb} GB) "
                  f"-$" f"{gb*0.08:.2f}/mo  [snapshot {snaps[0]['SnapshotId']} ok]")
            if do:
                ec2.delete_volume(VolumeId=vid)

    if "eip" in args.stage:
        print(f"\n== release Elastic IPs ==")
        for a in ec2.describe_addresses()["Addresses"]:
            if a.get("InstanceId"):
                print(f"  SKIP {a['PublicIp']}: still associated with {a['InstanceId']}")
                continue
            print(f"  {tag} release_address({a['PublicIp']} / "
                  f"{a.get('AllocationId')}) -$3.65/mo")
            if do:
                ec2.release_address(AllocationId=a["AllocationId"])

    if not do:
        print("\nPreview only. Re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
