#!/usr/bin/env python3
"""Select a release from CLI cache; copied v2 remains the safe default."""
import argparse
import glob
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", required=True)
    parser.add_argument("--json", action="store_true")
    reference_mode = parser.add_mutually_exclusive_group()
    reference_mode.add_argument(
        "--include-v3-reference", action="store_true",
        help="explicit mixed-mode opt-in: allow CANONICAL_REFERENCE v3 releases",
    )
    reference_mode.add_argument(
        "--require-v3-reference", action="store_true",
        help=("strict canary gate: accept only published "
              "CANONICAL_REFERENCE v3 releases"),
    )
    args = parser.parse_args()
    reference_enabled = (
        args.include_v3_reference or args.require_v3_reference
    )
    candidates = []
    patterns = [
        os.path.join(args.cache, "releases", "*", "MANIFEST.json")]
    if reference_enabled:
        # inventory intentionally caches an unmaterialized v3 manifest under
        # reference_manifests/.  It does not enter releases/ until fetch has
        # completed exact-version verification, so a fresh canary selector
        # must inspect this neutral cache explicitly.
        patterns.append(os.path.join(
            args.cache, "reference_manifests", "*", "MANIFEST.json"))
    paths = sorted({path for pattern in patterns for path in glob.glob(pattern)})
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            continue
        rid = manifest.get("release_id") or os.path.basename(
            os.path.dirname(path))
        is_v2 = (
            not args.require_v3_reference
            and manifest.get("schema_version")
            == "research-release-manifest-v2"
            and (manifest.get("version_binding") or {}).get("mode")
            == "VERSION_BOUND"
        )
        is_v3 = (
            reference_enabled
            and manifest.get("schema")
            == "research-release-manifest-v3-reference"
            and manifest.get("schema_version") == 3
            and manifest.get("storage_mode") == "CANONICAL_REFERENCE"
            and (manifest.get("version_binding") or {}).get("mode")
            == "CANONICAL_REFERENCE"
            and manifest.get("publication_status") == "PUBLISHED"
        )
        if not (is_v2 or is_v3):
            continue
        if not manifest.get("date"):
            continue
        object_bytes = sum(
            int(item.get("size") or 0) for item in manifest.get("objects", [])
        )
        mode = "REFERENCE_V3" if is_v3 else "COPIED_V2"
        candidates.append((manifest["date"],
                           manifest.get("published_at_utc")
                           or manifest.get("generated_at_utc") or "",
                           rid, object_bytes, mode, manifest))
    if not candidates:
        if args.require_v3_reference:
            detail = ("strict gate requires an inventoried, published "
                      "CANONICAL_REFERENCE v3 manifest")
        elif args.include_v3_reference:
            detail = ("mixed gate accepts version-bound v2 or an inventoried, "
                      "published CANONICAL_REFERENCE v3 manifest")
        else:
            detail = "default gate accepts version-bound v2 only"
        raise SystemExit(
            "W09_RELEASE_GATE: no eligible research release found (%s)" % detail
        )
    _date, _generated, rid, object_bytes, mode, manifest = max(candidates)
    result = {
        "release_id": rid,
        "date": manifest["date"],
        "generated_at_utc": (manifest.get("published_at_utc")
                             or manifest.get("generated_at_utc")),
        "evidence_tier": ((manifest.get("evidence") or {}).get("tier")
                          if mode == "REFERENCE_V3"
                          else manifest.get("evidence_tier")),
        "object_bytes": object_bytes,
        "storage_mode": mode,
        "version_binding_mode": (
            "CANONICAL_REFERENCE" if mode == "REFERENCE_V3"
            else "VERSION_BOUND"),
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(rid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
