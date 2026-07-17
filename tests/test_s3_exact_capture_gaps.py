#!/usr/bin/env python3
"""Offline tests for the exact-VersionId S3 capture-gap backfill scanner."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
try:
    import canonical_receipts as cr  # noqa: E402
    import s3_exact_capture_gaps as exact  # noqa: E402
finally:
    sys.path.remove(str(TOOLS))


DATE = "2026-07-10"
DAY_START = int(dt.datetime(
    2026, 7, 10, tzinfo=dt.timezone.utc).timestamp() * 1_000_000)
DAY_END = DAY_START + 86_400_000_000
AFTER_DAY = DAY_END + 1


def _raw(times, *, final_newline=True):
    rows = [json.dumps({
        "recv_mono_ns": index + 1,
        "recv_wall_ns": timestamp * 1000,
        "source": "Kalshi",
        "channel": "trade",
        "source_ticker": "TEST",
    }, separators=(",", ":")).encode("utf-8") for index, timestamp in enumerate(times)]
    payload = b"\n".join(rows)
    return payload + (b"\n" if final_newline else b"")


def _proof(rel, body):
    return {
        "file": rel,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _fixture():
    bodies = {
        f"date={DATE}/firehose_00.ndjson": _raw([
            DAY_START,
            DAY_START + 120_000_000,
        ]),
        f"date={DATE}/firehose_00.ndjson.1": _raw([
            DAY_START + 30_000_000,
            DAY_START + 60_000_000,
            DAY_START + 90_000_000,
            DAY_END - 1_000_000,
        ], final_newline=False),
        f"date={DATE}/l2_00.ndjson": _raw([DAY_START]),
        f"date={DATE}/rfq_00.ndjson": _raw([DAY_START]),
        "date=2026-07-11/firehose_00.ndjson": _raw([DAY_END]),
    }
    raw_files = [_proof(rel, body) for rel, body in bodies.items()]
    seal = {
        "version": 2,
        "method": "full_v2",
        "status": "SEALED",
        "date": DATE,
        "raw_files": raw_files,
        "archive_file_stats": [],
    }
    selected = [row for row in raw_files
                if row["file"].startswith(f"date={DATE}/firehose_")]
    selected.sort(key=lambda row: row["file"])
    return seal, selected, bodies


class FakeGet:
    def __init__(self, body, version_id, *, response_version=None,
                 response_size=None, chunk_size=17, read_error=None):
        self.body = body
        self.version_id = version_id
        self.response_version = response_version
        self.response_size = response_size
        self.chunk_size = chunk_size
        self.read_error = read_error
        self.offset = 0
        self.metadata = None

    def __enter__(self):
        return self

    def read(self, requested):
        if self.read_error is not None:
            raise self.read_error
        size = min(requested, self.chunk_size)
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.metadata = {
                "VersionId": (self.version_id if self.response_version is None
                              else self.response_version),
                "ContentLength": (len(self.body) if self.response_size is None
                                  else self.response_size),
            }
        return False


class FakeClient:
    def __init__(self, bodies, *, head_version=None, head_size=None,
                 response_version=None, response_size=None, read_error=None):
        self.bodies = dict(bodies)
        self.head_version = head_version
        self.head_size = head_size
        self.response_version = response_version
        self.response_size = response_size
        self.read_error = read_error
        self.calls = []

    @staticmethod
    def _rel(key):
        prefix = f"{exact.SOURCE_PREFIX}/raw/"
        assert key.startswith(prefix)
        return key[len(prefix):]

    def head_current(self, bucket, key):
        self.calls.append(("HEAD", bucket, key, None))
        rel = self._rel(key)
        if rel not in self.bodies:
            raise AssertionError(f"unexpected object {rel}")
        body = self.bodies[rel]
        version = (f"version-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
                   if self.head_version is None else self.head_version)
        return {
            "VersionId": version,
            "ContentLength": (len(body) if self.head_size is None
                              else self.head_size),
        }

    def open_exact(self, bucket, key, version_id, expected_size):
        self.calls.append(("GET", bucket, key, version_id))
        rel = self._rel(key)
        body = self.bodies[rel]
        assert expected_size == (len(body) if self.head_size is None
                                 else self.head_size)
        return FakeGet(
            body, version_id, response_version=self.response_version,
            response_size=self.response_size, read_error=self.read_error)


def _scan(client=None, *, seal=None, selected=None, bodies=None,
          memory_limit_bytes=64 << 20, scratch_parent=None):
    if seal is None or selected is None or bodies is None:
        seal, selected, bodies = _fixture()
    if client is None:
        client = FakeClient(bodies)
    receipt = exact.scan_exact_day(
        date=DATE,
        seal=seal,
        seal_sha256="a" * 64,
        firehose=selected,
        client=client,
        memory_limit_bytes=memory_limit_bytes,
        scratch_parent=scratch_parent,
        now_us=AFTER_DAY,
        generated_at_utc="2026-07-17T00:00:00Z",
    )
    return receipt, client


def test_load_seal_hard_excludes_rfq_l2_and_cross_date(tmp_path):
    seal, selected, _bodies = _fixture()
    path = tmp_path / "seal.json"
    path.write_text(json.dumps(seal), encoding="utf-8")

    loaded, digest, projected = exact.load_sealed_firehose(path, DATE)

    assert loaded == seal
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert projected == selected
    assert [row["file"] for row in projected] == [
        f"date={DATE}/firehose_00.ndjson",
        f"date={DATE}/firehose_00.ndjson.1",
    ]
    assert all("rfq" not in row["file"] and "/l2_" not in row["file"]
               for row in projected)


def test_exact_scan_heads_then_gets_only_sealed_same_date_firehose():
    seal, selected, bodies = _fixture()
    receipt, client = _scan(seal=seal, selected=selected, bodies=bodies)

    expected_calls = []
    for row in selected:
        key = f"{exact.SOURCE_PREFIX}/raw/{row['file']}"
        version = f"version-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
        expected_calls.extend([
            ("HEAD", exact.SOURCE_BUCKET, key, None),
            ("GET", exact.SOURCE_BUCKET, key, version),
        ])
    assert client.calls == expected_calls
    assert receipt["schema_version"] == "capture-gap-scan-receipt-v1"
    assert receipt["files"] == [
        {"file": row["file"], "bytes": row["size"]}
        for row in selected
    ]
    assert receipt["n_files"] == 2
    assert receipt["records"] == 6
    assert receipt["unparsed"] == 0
    assert receipt["unreadable"] is False
    # Global order is 0,30,60,90,120 seconds, then day_end-1 second.  A
    # per-object sequential scan would invent a backward transition here.
    assert receipt["gaps"] == [{
        "start_us": DAY_START + 120_000_000,
        "end_us": DAY_END - 1_000_000,
    }]
    source = receipt["source_attestation"]
    assert source["transport"] == \
        "AWS_CLI_HEAD_CURRENT_THEN_GET_EXACT_VERSION"
    assert source["selection"] == \
        "SAME_DATE_SEALED_FIREHOSE_ONLY_RFQ_L2_EXCLUDED"
    assert source["raw_payload_bytes_written_to_disk"] == 0
    assert len(source["objects"]) == 2
    valid, reason = cr._validate_capture_receipt(receipt, seal, DATE)
    assert (valid, reason) == (True, None)


@pytest.mark.parametrize(
    ("client_kwargs", "code"),
    [
        ({"head_version": "null"}, "VERSIONING_REQUIRED"),
        ({"head_size": 1}, "S3_HEAD_SIZE_MISMATCH"),
        ({"response_version": "other"}, "S3_GET_VERSION_MISMATCH"),
        ({"response_size": 1}, "S3_GET_SIZE_MISMATCH"),
    ],
)
def test_metadata_anomalies_fail_closed(client_kwargs, code):
    seal, selected, bodies = _fixture()
    client = FakeClient(bodies, **client_kwargs)
    with pytest.raises(exact.ExactCaptureError) as caught:
        _scan(client, seal=seal, selected=selected, bodies=bodies)
    assert caught.value.code == code


def test_body_sha_mismatch_fails_closed():
    seal, selected, bodies = _fixture()
    rel = selected[0]["file"]
    original = bodies[rel]
    bodies[rel] = bytes([original[0] ^ 1]) + original[1:]
    with pytest.raises(exact.ExactCaptureError) as caught:
        _scan(seal=seal, selected=selected, bodies=bodies)
    assert caught.value.code == "S3_GET_SHA256_MISMATCH"


def test_caller_cannot_scan_a_subset_of_the_sealed_firehose_set():
    seal, selected, bodies = _fixture()
    with pytest.raises(exact.ExactCaptureError) as caught:
        _scan(seal=seal, selected=selected[:1], bodies=bodies)
    assert caught.value.code == "FIREHOSE_SCOPE_VIOLATION"


@pytest.mark.parametrize("bad", [
    b'{"recv_wall_ns":not-an-int}\n',
    b'{"different_field":1}\n',
    b'\xff\n',
])
def test_any_record_parse_anomaly_fails_closed(bad):
    seal, selected, bodies = _fixture()
    rel = selected[0]["file"]
    bodies[rel] = bad
    replacement = _proof(rel, bad)
    seal["raw_files"] = [
        replacement if row["file"] == rel else row
        for row in seal["raw_files"]
    ]
    selected = exact._project_sealed_firehose(seal, DATE)
    client = FakeClient(bodies)
    with pytest.raises(exact.ExactCaptureError) as caught:
        _scan(client, seal=seal, selected=selected, bodies=bodies)
    assert caught.value.code == "RAW_RECORD_UNPARSEABLE"


def test_stream_exception_fails_closed_without_raw_detail():
    seal, selected, bodies = _fixture()
    client = FakeClient(bodies, read_error=RuntimeError("secret-looking-value"))
    with pytest.raises(exact.ExactCaptureError) as caught:
        _scan(client, seal=seal, selected=selected, bodies=bodies)
    assert caught.value.code == "S3_GET_BODY_FAILED"
    assert "secret-looking-value" not in str(caught.value)


def test_explicit_small_sort_budget_spills_only_timestamps_and_cleans(tmp_path):
    timestamps = [DAY_START + index * 1_000_000 for index in range(20)]
    timestamps.append(DAY_END - 1_000_000)
    body = _raw(list(reversed(timestamps)))
    rel = f"date={DATE}/firehose_00.ndjson"
    seal = {
        "version": 2, "method": "full_v2", "status": "SEALED",
        "date": DATE, "raw_files": [_proof(rel, body)],
        "archive_file_stats": [],
    }
    selected = [_proof(rel, body)]
    bodies = {rel: body}
    # Capacity four timestamps forces several derived int64 runs.
    budget = exact.SORT_FIXED_BYTES + exact.SORT_BYTES_PER_TIMESTAMP * 4

    receipt, _client = _scan(
        seal=seal, selected=selected, bodies=bodies,
        memory_limit_bytes=budget, scratch_parent=tmp_path)

    source = receipt["source_attestation"]
    assert source["sort_record_capacity"] == 4
    assert source["timestamp_spill_bytes"] == len(timestamps) * 8
    assert source["raw_payload_bytes_written_to_disk"] == 0
    assert list(tmp_path.iterdir()) == []
    assert receipt["records"] == len(timestamps)


def test_publish_outputs_replaces_only_target_day_atomically(tmp_path):
    receipt, _client = _scan()
    record = tmp_path / "quality" / "capture_gaps.csv"
    record.parent.mkdir()
    prior = (DAY_START - 3_600_000_000, DAY_START - 3_000_000_000)
    stale = (DAY_START + 1_000_000, DAY_START + 2_000_000)
    record.write_text(
        "start_us,end_us\n%d,%d\n%d,%d\n" % (*prior, *stale),
        encoding="utf-8")

    record_path, receipt_path = exact.publish_outputs(
        receipt, record, record.parent)

    assert record_path == str(record)
    assert receipt_path == str(
        record.parent / f"capture_gap_receipt_{DATE}.json")
    rows = list(csv.DictReader(io.StringIO(record.read_text())))
    assert rows == [
        {"start_us": str(prior[0]), "end_us": str(prior[1])},
        {"start_us": str(receipt["gaps"][0]["start_us"]),
         "end_us": str(receipt["gaps"][0]["end_us"])},
    ]
    persisted = json.loads(Path(receipt_path).read_text())
    assert persisted == receipt
    assert not list(record.parent.glob(".*.pending-*"))


def test_failed_scan_creates_no_outputs(tmp_path):
    seal, selected, bodies = _fixture()
    client = FakeClient(bodies, head_version="null")
    record = tmp_path / "capture_gaps.csv"
    receipt_path = tmp_path / f"capture_gap_receipt_{DATE}.json"

    with pytest.raises(exact.ExactCaptureError):
        _scan(client, seal=seal, selected=selected, bodies=bodies)

    assert not record.exists()
    assert not receipt_path.exists()


class _FakePopen:
    def __init__(self, command, *, body, version, **_kwargs):
        self.command = command
        self.returncode = 0
        self._body = body
        self._version = version
        fd_path = next(part for part in command if part.startswith("/dev/fd/"))
        inherited = int(fd_path.rsplit("/", 1)[1])
        duplicate = os.dup(inherited)
        os.write(duplicate, body)
        os.close(duplicate)

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        assert timeout is not None
        return (json.dumps({
            "VersionId": self._version,
            "ContentLength": len(self._body),
        }).encode(), b"")

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_aws_cli_transport_get_body_uses_pipe_and_exact_version(monkeypatch):
    body = b"object-body-never-written-to-a-path"
    version = "exact/version+id=="
    calls = []

    def fake_popen(command, **kwargs):
        process = _FakePopen(
            command, body=body, version=version, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(exact.subprocess, "Popen", fake_popen)
    stream = exact.AwsCliExactGetStream(
        "aws", {}, exact.SOURCE_BUCKET,
        f"{exact.SOURCE_PREFIX}/raw/date={DATE}/firehose_00.ndjson",
        version, len(body), 30)

    with stream as opened:
        observed = b""
        while True:
            chunk = opened.read(7)
            if not chunk:
                break
            observed += chunk

    assert observed == body
    assert stream.metadata == {
        "VersionId": version,
        "ContentLength": len(body),
    }
    command = calls[0].command
    assert command[:3] == ["aws", "s3api", "get-object"]
    assert command[command.index("--version-id") + 1] == version
    assert any(part.startswith("/dev/fd/") for part in command)
    assert all("put-object" not in part and "delete" not in part
               and "list" not in part for part in command)


def test_head_transport_is_version_resolving_read_only(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps({
            "VersionId": "version-1", "ContentLength": 123,
        }).encode()
        stderr = b""

    def fake_run(command, **_kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr(exact.subprocess, "run", fake_run)
    client = exact.AwsCliExactS3Client("/snap/bin/aws", 30)
    result = client.head_current(
        exact.SOURCE_BUCKET,
        f"{exact.SOURCE_PREFIX}/raw/date={DATE}/firehose_00.ndjson")

    assert result == {"VersionId": "version-1", "ContentLength": 123}
    assert calls[0][:3] == ["/snap/bin/aws", "s3api", "head-object"]
    assert "--version-id" not in calls[0]
    assert all("put-object" not in part and "delete" not in part
               and "list" not in part for part in calls[0])
