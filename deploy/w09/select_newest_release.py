#!/usr/bin/env python3
"""Select the newest data-date, version-bound W05 release from CLI cache."""
import argparse
import glob
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    candidates = []
    pattern = os.path.join(args.cache, "releases", "*", "MANIFEST.json")
    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            continue
        rid = manifest.get("release_id") or os.path.basename(
            os.path.dirname(path))
        if (manifest.get("schema_version") !=
                "research-release-manifest-v2"):
            continue
        if ((manifest.get("version_binding") or {}).get("mode") !=
                "VERSION_BOUND"):
            continue
        if not manifest.get("date"):
            continue
        object_bytes = sum(
            int(item.get("size") or 0) for item in manifest.get("objects", [])
        )
        candidates.append((manifest["date"],
                           manifest.get("generated_at_utc") or "",
                           rid, object_bytes, manifest))
    if not candidates:
        raise SystemExit(
            "W09_RELEASE_GATE: no version-bound v2 research release found"
        )
    _date, _generated, rid, object_bytes, manifest = max(candidates)
    result = {
        "release_id": rid,
        "date": manifest["date"],
        "generated_at_utc": manifest.get("generated_at_utc"),
        "evidence_tier": manifest.get("evidence_tier"),
        "object_bytes": object_bytes,
        "version_binding_mode": "VERSION_BOUND",
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(rid)
    return 0


if __name__ == "__main__":
    sys.exit(main())

