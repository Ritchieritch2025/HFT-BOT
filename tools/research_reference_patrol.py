#!/usr/bin/env python3
"""Read-only daily integrity patrol for downloaded v3 reference manifests.

The patrol never writes AWS, object tags, or research data.  It validates each
local manifest with ``research_reference.validate_manifest`` and then checks
every referenced exact S3 version through an injected read-only reader.

Policy/IAM discovery is intentionally out of scope: callers must supply a
fresh, digest-bound policy-evidence file produced by their authorized control
plane.  Missing, stale, or malformed policy evidence fails closed.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_reference as ref  # noqa: E402
import warehouse_common as wc  # noqa: E402


RESULT_SCHEMA = "research-reference-integrity-patrol-v1"
POLICY_EVIDENCE_SCHEMA = "research-reference-policy-evidence-v1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_POLICY_EVIDENCE_BYTES = 1024 * 1024
MAX_POLICY_EVIDENCE_AGE_SECONDS = 24 * 60 * 60
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
READABLE_STORAGE_CLASSES = frozenset((
    "STANDARD", "REDUCED_REDUNDANCY", "STANDARD_IA", "ONEZONE_IA",
    "INTELLIGENT_TIERING", "GLACIER_IR", "EXPRESS_ONEZONE",
))


class PatrolError(RuntimeError):
    """One stable, non-secret patrol failure."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def alert_path_for_live_dir(live_dir: str) -> str:
    return os.path.join(os.path.abspath(live_dir),
                        "research_reference_patrol", "YELLOW.json")


def default_status_path(alert_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(alert_path)),
                        "PATROL_STATUS.json")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _utc(value: object, label: str) -> datetime.datetime:
    if not isinstance(value, str) or not value:
        raise PatrolError("INVALID_TIMESTAMP", f"{label} is missing")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PatrolError("INVALID_TIMESTAMP", f"{label}: {exc}") from exc
    if parsed.tzinfo is None:
        raise PatrolError("INVALID_TIMESTAMP", f"{label} is not timezone-aware")
    return parsed.astimezone(datetime.timezone.utc)


def _utc_text(value: datetime.datetime) -> str:
    value = value.astimezone(datetime.timezone.utc)
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _json_no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PatrolError("DUPLICATE_JSON_KEY", f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _read_bounded_json(path: str, limit: int, label: str) -> dict:
    """Read one stable regular file without following its final component."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise PatrolError("LOCAL_SAFETY_UNAVAILABLE", "O_NOFOLLOW is required")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PatrolError("LOCAL_READ_FAILED", f"{label}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise PatrolError(
                "LOCAL_READ_FAILED", f"{label} is not a bounded regular file")
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        try:
            path_after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise PatrolError("LOCAL_SOURCE_CHANGED", f"{label}: {exc}") from exc
        fingerprint = lambda item: (  # noqa: E731 - compact immutable tuple
            item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
            item.st_ctime_ns)
        if (total > limit or total != before.st_size
                or fingerprint(before) != fingerprint(after)
                or stat.S_ISLNK(path_after.st_mode)
                or (path_after.st_dev, path_after.st_ino)
                != (after.st_dev, after.st_ino)):
            raise PatrolError("LOCAL_SOURCE_CHANGED", f"{label} changed while read")
    finally:
        os.close(fd)
    try:
        loaded = json.loads(b"".join(chunks), object_pairs_hook=_json_no_duplicates)
    except PatrolError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise PatrolError("INVALID_JSON", f"{label}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PatrolError("INVALID_JSON", f"{label} root is not an object")
    return loaded


def _write_atomic_json(path: str, payload: dict) -> None:
    """Durably replace a small local JSON record in its own directory."""
    target = pathlib.Path(path).absolute()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    raw = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True,
                     allow_nan=False).encode("utf-8") + b"\n"
    fd, temp_path = tempfile.mkstemp(
        prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "wb", closefd=True)
        fd = -1
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = ""
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def validate_policy_evidence(evidence: object, buckets: set[str],
                             now: datetime.datetime) -> dict:
    """Validate externally produced IAM/bucket-policy evidence.

    The patrol does not infer a production role.  The supplied principal and
    policy snapshot are opaque control-plane facts, bound by the self-digest
    and a maximum 24-hour age.
    """
    required = {
        "schema_version", "status", "generated_at_utc", "expires_at_utc",
        "principal_arn", "covered_buckets", "policy_snapshot",
        "evidence_sha256",
    }
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise PatrolError(
            "POLICY_EVIDENCE_INVALID", "policy evidence fields are incomplete")
    if (evidence["schema_version"] != POLICY_EVIDENCE_SCHEMA
            or evidence["status"] != "PASS"):
        raise PatrolError(
            "POLICY_EVIDENCE_INVALID", "policy evidence is not a PASS record")
    principal = evidence["principal_arn"]
    if (not isinstance(principal, str) or not principal.startswith("arn:")
            or any(char.isspace() for char in principal)):
        raise PatrolError(
            "POLICY_EVIDENCE_INVALID", "principal_arn is missing or malformed")
    covered = evidence["covered_buckets"]
    if (not isinstance(covered, list) or not covered
            or len(set(covered)) != len(covered)
            or not all(isinstance(item, str) and item for item in covered)
            or not buckets <= set(covered)):
        raise PatrolError(
            "POLICY_EVIDENCE_SCOPE_MISMATCH",
            "policy evidence does not cover every manifest bucket")
    snapshot = evidence["policy_snapshot"]
    required_policy_digests = {
        "identity_policy_sha256", "bucket_policy_sha256",
        "effective_permissions_sha256",
    }
    if (not isinstance(snapshot, dict)
            or not required_policy_digests <= set(snapshot)
            or any(not isinstance(snapshot.get(key), str)
                   or not SHA256_RE.fullmatch(snapshot[key])
                   for key in required_policy_digests)):
        raise PatrolError(
            "POLICY_EVIDENCE_INVALID",
            "policy_snapshot lacks identity/bucket/effective digests")
    digest = evidence["evidence_sha256"]
    projection = {key: value for key, value in evidence.items()
                  if key != "evidence_sha256"}
    if (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            or digest != canonical_sha256(projection)):
        raise PatrolError(
            "POLICY_EVIDENCE_DIGEST_MISMATCH",
            "policy evidence self-digest does not verify")
    generated = _utc(evidence["generated_at_utc"], "policy generated_at_utc")
    expires = _utc(evidence["expires_at_utc"], "policy expires_at_utc")
    if generated > now or (now - generated).total_seconds() > \
            MAX_POLICY_EVIDENCE_AGE_SECONDS or expires <= now:
        raise PatrolError(
            "POLICY_EVIDENCE_STALE", "policy evidence is stale or expired")
    return {
        "covered": True,
        "principal_arn": principal,
        "generated_at_utc": evidence["generated_at_utc"],
        "expires_at_utc": evidence["expires_at_utc"],
        "evidence_sha256": digest,
    }


def _parse_tags(payload: object, version_id: str, label: str) -> dict[str, str]:
    if (not isinstance(payload, dict)
            or payload.get("VersionId") != version_id
            or not isinstance(payload.get("TagSet"), list)
            or len(payload["TagSet"]) > 10):
        raise PatrolError("TAG_RESPONSE_INVALID", f"{label} tags are malformed")
    tags = {}
    for row in payload["TagSet"]:
        if (not isinstance(row, dict) or set(row) != {"Key", "Value"}
                or not isinstance(row.get("Key"), str) or not row["Key"]
                or not isinstance(row.get("Value"), str)
                or row["Key"] in tags):
            raise PatrolError(
                "TAG_RESPONSE_INVALID", f"{label} tags are malformed")
        tags[row["Key"]] = row["Value"]
    return tags


def _check_object(reader, obj: dict, release_id: str) -> None:
    label = f"{release_id}:{obj['logical_key']}"
    expected_modified = obj.get("source_last_modified_utc")
    if expected_modified is None:
        raise PatrolError(
            "MANIFEST_LAST_MODIFIED_MISSING",
            f"{label} has no source_last_modified_utc binding")
    try:
        head = reader.head(
            obj["source_bucket"], obj["source_key"],
            obj["source_version_id"])
    except Exception as exc:
        raise PatrolError("EXACT_HEAD_FAILED", f"{label}: {exc}") from exc
    if not isinstance(head, dict):
        raise PatrolError("EXACT_HEAD_INVALID", f"{label} HEAD is not an object")
    if head.get("DeleteMarker") not in (None, False):
        raise PatrolError("EXACT_VERSION_DELETE_MARKER", f"{label} is deleted")
    if head.get("VersionId") != obj["source_version_id"]:
        raise PatrolError("VERSION_ID_DRIFT", f"{label} VersionId differs")
    if (not isinstance(head.get("ContentLength"), int)
            or isinstance(head.get("ContentLength"), bool)
            or head["ContentLength"] != obj["size"]):
        raise PatrolError("SIZE_DRIFT", f"{label} ContentLength differs")
    observed_modified = _utc(head.get("LastModified"), f"{label} LastModified")
    if observed_modified != _utc(expected_modified, f"{label} expected LastModified"):
        raise PatrolError("LAST_MODIFIED_DRIFT", f"{label} LastModified differs")
    storage_class = head.get("StorageClass") or "STANDARD"
    if (not isinstance(storage_class, str)
            or storage_class not in READABLE_STORAGE_CLASSES
            or head.get("ArchiveStatus") not in (None, "")):
        raise PatrolError(
            "STORAGE_NOT_IMMEDIATELY_READABLE",
            f"{label} storage class/archive status is not immediately readable")
    try:
        tags_payload = reader.get_tags(
            obj["source_bucket"], obj["source_key"],
            obj["source_version_id"])
    except Exception as exc:
        raise PatrolError("EXACT_TAG_READ_FAILED", f"{label}: {exc}") from exc
    tags = _parse_tags(tags_payload, obj["source_version_id"], label)
    if tags.get("research-eligible") != "true":
        raise PatrolError(
            "ELIGIBILITY_TAG_DRIFT", f"{label} lacks research-eligible=true")
    if (obj.get("kind") == "rfq" or obj.get("channel") == "rfq") \
            and tags.get("research-channel") != "rfq":
        raise PatrolError(
            "RFQ_CHANNEL_TAG_DRIFT", f"{label} lacks research-channel=rfq")


def _failure(code: str, detail: str, **context: Any) -> dict:
    row = {"code": code, "detail": detail}
    row.update({key: value for key, value in context.items()
                if value is not None})
    return row


def run_patrol(manifest_paths, reader, policy_evidence_path: str,
               alert_path: str, status_path: str | None = None,
               now: datetime.datetime | None = None) -> dict:
    """Run one patrol and atomically persist its local result records."""
    if (not isinstance(manifest_paths, (list, tuple)) or not manifest_paths
            or not all(isinstance(path, (str, os.PathLike))
                       for path in manifest_paths)):
        raise PatrolError("MANIFEST_INPUT_INVALID", "at least one manifest is required")
    checked = now or datetime.datetime.now(datetime.timezone.utc)
    if checked.tzinfo is None:
        raise PatrolError("INVALID_TIMESTAMP", "patrol now must be timezone-aware")
    checked = checked.astimezone(datetime.timezone.utc)
    status_path = status_path or default_status_path(alert_path)
    alert_path = os.path.abspath(alert_path)
    status_path = os.path.abspath(status_path)
    if alert_path == status_path:
        raise PatrolError(
            "OUTPUT_PATH_CONFLICT", "status_path must differ from alert_path")
    failures = []
    manifests = []
    release_ids = set()
    for raw_path in manifest_paths:
        path = os.path.abspath(os.fspath(raw_path))
        try:
            raw = _read_bounded_json(path, MAX_MANIFEST_BYTES, "v3 manifest")
            normalized = ref.validate_manifest(raw)
            if normalized["release_id"] in release_ids:
                raise PatrolError(
                    "DUPLICATE_RELEASE", normalized["release_id"])
            release_ids.add(normalized["release_id"])
            manifests.append((path, normalized))
        except (PatrolError, ref.ReferenceManifestError) as exc:
            failures.append(_failure(
                getattr(exc, "code", "MANIFEST_INVALID"), str(exc),
                manifest_path=path))

    buckets = {
        obj["source_bucket"] for _path, manifest in manifests
        for obj in manifest["objects"]
    }
    policy_summary = {"covered": False}
    if not failures:
        try:
            policy_raw = _read_bounded_json(
                os.path.abspath(policy_evidence_path),
                MAX_POLICY_EVIDENCE_BYTES, "policy evidence")
            policy_summary = validate_policy_evidence(
                policy_raw, buckets, checked)
        except PatrolError as exc:
            failures.append(_failure(exc.code, exc.detail,
                                     policy_evidence_path=os.path.abspath(
                                         policy_evidence_path)))

    expected = sum(len(manifest["objects"])
                   for _path, manifest in manifests)
    passed = 0
    if not failures:
        if (reader is None or not hasattr(reader, "head")
                or not hasattr(reader, "get_tags")):
            failures.append(_failure(
                "READER_INVALID", "reader must provide exact head and get_tags"))
        else:
            for path, manifest in manifests:
                for obj in manifest["objects"]:
                    try:
                        _check_object(reader, obj, manifest["release_id"])
                        passed += 1
                    except PatrolError as exc:
                        failures.append(_failure(
                            exc.code, exc.detail, manifest_path=path,
                            release_id=manifest["release_id"],
                            logical_key=obj["logical_key"],
                            bucket=obj["source_bucket"],
                            key=obj["source_key"],
                            version_id=obj["source_version_id"]))

    existing_yellow = os.path.lexists(alert_path)
    failed = bool(failures)
    result = {
        "schema_version": RESULT_SCHEMA,
        "state": "YELLOW" if failed else "PASS",
        "checked_at_utc": _utc_text(checked),
        "freeze_new_v3_publication": failed or existing_yellow,
        "existing_yellow_hold": existing_yellow,
        "manifest_count": len(manifests),
        "release_ids": sorted(release_ids),
        "object_checks_expected": expected,
        "object_checks_passed": passed,
        "policy_evidence": policy_summary,
        "failures": failures,
        "aws_mutations": 0,
        "tag_mutations": 0,
        "data_mutations": 0,
    }
    if failed:
        yellow = dict(result)
        yellow["alert_state"] = "ACTIVE_YELLOW"
        yellow["freeze_new_v3_publication"] = True
        _write_atomic_json(alert_path, yellow)
        result["freeze_new_v3_publication"] = True
    # A PASS updates only the status record.  It never removes or replaces an
    # earlier yellow alert; manual review/clearance is intentionally external.
    _write_atomic_json(status_path, result)
    return result


class AwsCliReadOnlyReader:
    """Minimal exact-version metadata/tag reader; contains no write command."""

    def __init__(self, executable: str = "aws"):
        self.executable = executable

    def _json(self, args):
        try:
            result = subprocess.run(
                [self.executable, "s3api", *args], capture_output=True,
                text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PatrolError("S3_READ_FAILED", str(exc)) from exc
        if result.returncode:
            raise PatrolError(
                "S3_READ_FAILED", (result.stderr or result.stdout or "")[-1000:])
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise PatrolError("S3_READ_FAILED", f"invalid AWS JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise PatrolError("S3_READ_FAILED", "AWS response is not an object")
        return value

    def head(self, bucket, key, version_id):
        return self._json([
            "head-object", "--bucket", bucket, "--key", key,
            "--version-id", version_id, "--output", "json",
        ])

    def get_tags(self, bucket, key, version_id):
        return self._json([
            "get-object-tagging", "--bucket", bucket, "--key", key,
            "--version-id", version_id, "--output", "json",
        ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", action="append", required=True,
                        help="downloaded v3 MANIFEST.json; repeat as needed")
    parser.add_argument("--policy-evidence", required=True,
                        help="fresh external policy-evidence JSON")
    parser.add_argument("--live-dir", required=True,
                        help="active main work/live root shared with publisher")
    parser.add_argument("--status-path")
    parser.add_argument("--aws-cli", default="aws")
    args = parser.parse_args(argv)
    result = run_patrol(
        args.manifest, AwsCliReadOnlyReader(args.aws_cli),
        args.policy_evidence, alert_path_for_live_dir(args.live_dir),
        args.status_path)
    print(json.dumps(result, sort_keys=True))
    if result["state"] != "PASS":
        return 2
    return 3 if result["freeze_new_v3_publication"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
