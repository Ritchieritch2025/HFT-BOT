#!/usr/bin/env python3
"""Append the audited research fragment to an exported S3 bucket policy.

This tool is local-only and never calls AWS.  It preserves every existing
statement, refuses Sid collisions and unsafe fragments, enforces the S3
20-KiB policy limit, and never overwrites an existing output.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat

import render_research_iam_bundle as renderer


MAX_POLICY_BYTES = 20 * 1024
MANIFEST_PUT_SID = "DenyResearchManifestPutWithoutConditionalCreate"
TAGGER_DENY_SID = "DenyCanonicalVersionTagMutationExceptDedicatedTagger"


class MergeError(ValueError):
    pass


def _read_policy(path, label):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise MergeError("cannot safely open %s: %s" % (label, exc))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_POLICY_BYTES:
            raise MergeError("%s is not a bounded regular file" % label)
        chunks = []
        total = 0
        while total <= MAX_POLICY_BYTES:
            chunk = os.read(fd, min(65536, MAX_POLICY_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_POLICY_BYTES:
            raise MergeError("%s exceeds the S3 policy limit" % label)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise MergeError("%s is not valid JSON: %s" % (label, exc))
    if not isinstance(value, dict):
        raise MergeError("%s root must be an object" % label)
    return value, raw


def _statements(policy, label):
    if policy.get("Version") != "2012-10-17":
        raise MergeError("%s must use Version 2012-10-17" % label)
    statements = policy.get("Statement")
    if not isinstance(statements, list):
        raise MergeError("%s Statement must be an array" % label)
    if any(not isinstance(statement, dict) for statement in statements):
        raise MergeError("%s contains a non-object statement" % label)
    return statements


def _sid_map(statements, label, *, require_sid):
    found = {}
    for statement in statements:
        sid = statement.get("Sid")
        if sid is None and not require_sid:
            continue
        if not isinstance(sid, str) or not sid:
            raise MergeError("%s statement has a missing/invalid Sid" % label)
        if sid in found:
            raise MergeError("%s has duplicate Sid %s" % (label, sid))
        found[sid] = statement
    return found


def _validate_fragment(fragment, statements):
    encoded = json.dumps(fragment, sort_keys=True, separators=(",", ":"))
    if renderer.PLACEHOLDER in encoded:
        raise MergeError("fragment still contains TAGGER_ARN")
    if not statements:
        raise MergeError("fragment has no statements")
    if any(statement.get("Effect") != "Deny" for statement in statements):
        raise MergeError("fragment may contain only explicit Deny statements")
    by_sid = _sid_map(statements, "fragment", require_sid=True)
    manifest = by_sid.get(MANIFEST_PUT_SID)
    if manifest is None:
        raise MergeError("fragment lacks the manifest conditional-create Deny")
    expected_manifest = {
        "Sid": MANIFEST_PUT_SID,
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:PutObject",
        "Resource": (
            "arn:aws:s3:::kalshi-vault-ritcardo/"
            "research/releases/*/MANIFEST.json"),
        "Condition": {"Null": {"s3:if-none-match": "true"}},
    }
    if manifest != expected_manifest:
        raise MergeError("manifest conditional-create Deny differs from contract")
    tagger = by_sid.get(TAGGER_DENY_SID)
    principal = (((tagger or {}).get("Condition") or {}).get("ArnNotEquals")
                 or {}).get("aws:PrincipalArn")
    if principal != renderer.APPROVED_TAGGER_ARN:
        raise MergeError("fragment tagger exception is not the approved user ARN")
    return by_sid


def _payload(policy):
    return (json.dumps(policy, sort_keys=True, indent=2, ensure_ascii=True)
            + "\n").encode("utf-8")


def _write_exclusive(path, payload):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
             getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise MergeError("refusing unsafe/existing output: %s" % exc)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise MergeError("short write while saving merged policy")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def merge(current_path, fragment_path, output_path):
    current, current_raw = _read_policy(current_path, "current policy")
    fragment, fragment_raw = _read_policy(fragment_path, "fragment")
    current_statements = _statements(current, "current policy")
    fragment_statements = _statements(fragment, "fragment")
    current_sids = _sid_map(
        current_statements, "current policy", require_sid=False)
    fragment_sids = _validate_fragment(fragment, fragment_statements)
    collisions = sorted(set(current_sids).intersection(fragment_sids))
    if collisions:
        raise MergeError("Sid collision: %s" % ", ".join(collisions))

    target = copy.deepcopy(current)
    target["Statement"] = (copy.deepcopy(current_statements)
                           + copy.deepcopy(fragment_statements))
    if target["Statement"][:len(current_statements)] != current_statements:
        raise MergeError("existing statements were not preserved exactly")
    payload = _payload(target)
    if len(payload) > MAX_POLICY_BYTES:
        raise MergeError(
            "merged policy is %d bytes; S3 limit is %d" %
            (len(payload), MAX_POLICY_BYTES))
    _write_exclusive(output_path, payload)
    return {
        "state": "FULL_BUCKET_POLICY_MERGED_NOT_APPLIED",
        "output": os.path.abspath(output_path),
        "current_policy_sha256": hashlib.sha256(current_raw).hexdigest(),
        "fragment_sha256": hashlib.sha256(fragment_raw).hexdigest(),
        "target_policy_sha256": hashlib.sha256(payload).hexdigest(),
        "current_statement_count": len(current_statements),
        "appended_statement_count": len(fragment_statements),
        "target_statement_count": len(target["Statement"]),
        "preserved_current_statements": True,
        "bytes": len(payload),
        "aws_writes": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True)
    parser.add_argument("--fragment", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = merge(
            os.path.abspath(args.current), os.path.abspath(args.fragment),
            os.path.abspath(args.output))
    except MergeError as exc:
        print("REFUSED %s" % exc, file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
