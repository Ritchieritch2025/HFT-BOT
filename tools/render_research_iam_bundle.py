#!/usr/bin/env python3
"""Materialize the research bucket-policy fragment without unsafe placeholders.

This tool is local-only.  It never calls AWS and deliberately refuses to
overwrite an existing output.  The source fragment remains an audit template;
only a successfully rendered file with the one approved, pathless IAM user
ARN is eligible for an operator's later merge review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = os.path.join(
    ROOT, "docs", "plan_releases", "pipeline",
    "W-PUB-REF-01C_BUCKET_POLICY_MERGE_FRAGMENT.json")
PLACEHOLDER = "TAGGER_ARN"
MAX_POLICY_BYTES = 20 * 1024
APPROVED_TAGGER_ARN = (
    "arn:aws:iam::321572485933:user/canonical-eligibility-tagger")


class RenderError(ValueError):
    pass


def _read_json_nofollow(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RenderError("cannot safely open source policy: %s" % exc)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_POLICY_BYTES:
            raise RenderError("source policy is not a bounded regular file")
        raw = b""
        while len(raw) <= MAX_POLICY_BYTES:
            chunk = os.read(fd, min(65536, MAX_POLICY_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) > MAX_POLICY_BYTES:
            raise RenderError("source policy exceeds the S3 policy limit")
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RenderError("source policy is not valid JSON: %s" % exc)
    return value


def _replace(value, principal_arn, count):
    if isinstance(value, dict):
        return {key: _replace(item, principal_arn, count)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, principal_arn, count) for item in value]
    if value == PLACEHOLDER:
        count[0] += 1
        return principal_arn
    return value


def render(source, output, principal_arn):
    if principal_arn != APPROVED_TAGGER_ARN:
        raise RenderError(
            "tagger ARN must exactly match the approved pathless IAM user")
    policy = _read_json_nofollow(source)
    count = [0]
    rendered = _replace(policy, principal_arn, count)
    if count[0] != 1:
        raise RenderError(
            "source policy must contain TAGGER_ARN exactly once (found %d)" %
            count[0])
    payload = (json.dumps(rendered, sort_keys=True, indent=2,
                          ensure_ascii=True) + "\n").encode("utf-8")
    if PLACEHOLDER.encode() in payload or len(payload) > MAX_POLICY_BYTES:
        raise RenderError("rendered policy failed placeholder/size gate")
    parent = os.path.dirname(os.path.abspath(output))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
             getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(output, flags, 0o600)
    except OSError as exc:
        raise RenderError("refusing unsafe/existing output: %s" % exc)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RenderError("short write while rendering policy")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return {
        "state": "IAM_BUCKET_FRAGMENT_RENDERED_NOT_APPLIED",
        "output": os.path.abspath(output),
        "tagger_principal_arn": principal_arn,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "aws_writes": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tagger-principal-arn", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    args = parser.parse_args(argv)
    try:
        result = render(
            os.path.abspath(args.source), os.path.abspath(args.output),
            args.tagger_principal_arn)
    except RenderError as exc:
        print("REFUSED %s" % exc, file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
