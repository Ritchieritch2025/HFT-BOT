#!/usr/bin/env python3
"""Offline tests for the exact-VersionId S3 capture-gap backfill scanner."""

from __future__ import annotations

import csv
import datetime as dt
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

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


def _record_bytes(*rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["start_us", "end_us"])
    writer.writerows(rows)
    return output.getvalue().encode()


def test_publish_outputs_is_receipt_only_and_never_mutates_record(tmp_path):
    receipt, _client = _scan()
    record = tmp_path / "quality" / "capture_gaps.csv"
    record.parent.mkdir()
    prior = (DAY_START - 3_600_000_000, DAY_START - 3_000_000_000)
    exact_gap = (receipt["gaps"][0]["start_us"], receipt["gaps"][0]["end_us"])
    original = _record_bytes(prior, exact_gap)
    record.write_bytes(original)
    before = record.stat()

    record_path, receipt_path, created = exact.publish_outputs(
        receipt, record, record.parent)

    assert record_path == str(record)
    assert receipt_path == str(
        record.parent / f"capture_gap_receipt_{DATE}.json")
    assert created is True
    after = record.stat()
    assert record.read_bytes() == original
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns) == \
           (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            before.st_ctime_ns)
    persisted = json.loads(Path(receipt_path).read_text())
    assert persisted == receipt
    assert not list(record.parent.glob(".*.pending-*"))


def test_zero_gap_receipt_requires_and_accepts_header_only_target_day(tmp_path):
    receipt, _client = _scan()
    receipt["gaps"] = []
    record = tmp_path / "capture_gaps.csv"
    original = _record_bytes()
    record.write_bytes(original)

    _record, receipt_path, created = exact.publish_outputs(
        receipt, record, tmp_path)

    assert created is True
    assert record.read_bytes() == original
    assert json.loads(Path(receipt_path).read_text())["gaps"] == []


def test_record_mismatch_fails_closed_without_any_write(tmp_path):
    receipt, _client = _scan()
    record = tmp_path / "capture_gaps.csv"
    original = _record_bytes((DAY_START + 1, DAY_START + 2))
    record.write_bytes(original)

    with pytest.raises(exact.ExactCaptureError) as caught:
        exact.publish_outputs(receipt, record, tmp_path)

    assert caught.value.code == "GAP_RECORD_MISMATCH"
    assert record.read_bytes() == original
    assert not (tmp_path / f"capture_gap_receipt_{DATE}.json").exists()


def test_existing_semantically_equal_receipt_is_idempotent_no_clobber(tmp_path):
    receipt, _client = _scan()
    gap = (receipt["gaps"][0]["start_us"], receipt["gaps"][0]["end_us"])
    record = tmp_path / "capture_gaps.csv"
    record.write_bytes(_record_bytes(gap))
    _record, receipt_path, created = exact.publish_outputs(receipt, record, tmp_path)
    assert created is True
    target = Path(receipt_path)
    original = target.read_bytes()
    before = target.stat()

    rerun = json.loads(json.dumps(receipt))
    rerun["generated_at_utc"] = "2026-07-17T01:02:03Z"
    rerun["source_attestation"]["sort_memory_limit_bytes"] += 1024
    rerun["source_attestation"]["sort_record_capacity"] += 1
    _record, same_path, created = exact.publish_outputs(rerun, record, tmp_path)

    assert same_path == receipt_path
    assert created is False
    after = target.stat()
    assert target.read_bytes() == original
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == \
           (before.st_dev, before.st_ino, before.st_mtime_ns)


def test_existing_conflicting_or_corrupt_receipt_is_never_overwritten(tmp_path):
    receipt, _client = _scan()
    gap = (receipt["gaps"][0]["start_us"], receipt["gaps"][0]["end_us"])
    record = tmp_path / "capture_gaps.csv"
    record.write_bytes(_record_bytes(gap))
    target = tmp_path / f"capture_gap_receipt_{DATE}.json"
    corrupt = b"{not-json"
    target.write_bytes(corrupt)

    with pytest.raises(exact.ExactCaptureError) as caught:
        exact.publish_outputs(receipt, record, tmp_path)

    assert caught.value.code == "RECEIPT_INVALID"
    assert target.read_bytes() == corrupt
    target.unlink()
    conflicting = json.loads(json.dumps(receipt))
    conflicting["source_attestation"]["seal_sha256"] = "b" * 64
    target.write_text(json.dumps(conflicting))
    conflict_bytes = target.read_bytes()

    with pytest.raises(exact.ExactCaptureError) as caught:
        exact.publish_outputs(receipt, record, tmp_path)

    assert caught.value.code == "RECEIPT_CONFLICT"
    assert target.read_bytes() == conflict_bytes


def test_existing_receipt_symlink_is_rejected_without_touching_target(tmp_path):
    receipt, _client = _scan()
    gap = (receipt["gaps"][0]["start_us"], receipt["gaps"][0]["end_us"])
    record = tmp_path / "capture_gaps.csv"
    record.write_bytes(_record_bytes(gap))
    harmless = tmp_path / "harmless"
    harmless.write_bytes(b"do-not-touch")
    target = tmp_path / f"capture_gap_receipt_{DATE}.json"
    target.symlink_to(harmless)

    with pytest.raises(exact.ExactCaptureError) as caught:
        exact.publish_outputs(receipt, record, tmp_path)

    assert caught.value.code == "LOCAL_INPUT_INVALID"
    assert target.is_symlink()
    assert harmless.read_bytes() == b"do-not-touch"


def test_no_clobber_link_failure_leaves_no_receipt_or_pending(tmp_path, monkeypatch):
    receipt, _client = _scan()
    gap = (receipt["gaps"][0]["start_us"], receipt["gaps"][0]["end_us"])
    record = tmp_path / "capture_gaps.csv"
    original = _record_bytes(gap)
    record.write_bytes(original)

    def fail_link(*_args, **_kwargs):
        raise OSError(errno.EIO, "injected")

    monkeypatch.setattr(exact.os, "link", fail_link)
    with pytest.raises(exact.ExactCaptureError) as caught:
        exact.publish_outputs(receipt, record, tmp_path)

    assert caught.value.code == "OUTPUT_COMMIT_FAILED"
    assert record.read_bytes() == original
    assert not (tmp_path / f"capture_gap_receipt_{DATE}.json").exists()
    assert not list(tmp_path.glob(".*.pending-*"))


def test_post_link_fsync_failure_is_recoverable_without_overwrite(
        tmp_path, monkeypatch):
    receipt, _client = _scan()
    gap = (receipt["gaps"][0]["start_us"], receipt["gaps"][0]["end_us"])
    record = tmp_path / "capture_gaps.csv"
    record.write_bytes(_record_bytes(gap))
    target = tmp_path / f"capture_gap_receipt_{DATE}.json"
    original_fsync = exact._fsync_directory

    def fail_fsync(_path):
        exact._fail("OUTPUT_COMMIT_FAILED", "injected fsync")

    monkeypatch.setattr(exact, "_fsync_directory", fail_fsync)
    with pytest.raises(exact.ExactCaptureError) as caught:
        exact.publish_outputs(receipt, record, tmp_path)
    assert caught.value.code == "OUTPUT_COMMIT_FAILED"
    assert json.loads(target.read_text()) == receipt

    monkeypatch.setattr(exact, "_fsync_directory", original_fsync)
    _record, same, created = exact.publish_outputs(receipt, record, tmp_path)
    assert same == str(target)
    assert created is False


def test_concurrent_record_replace_is_detected_and_own_receipt_rolled_back(
        tmp_path, monkeypatch):
    receipt, _client = _scan()
    gap = (receipt["gaps"][0]["start_us"], receipt["gaps"][0]["end_us"])
    prior = (DAY_START - 1000, DAY_START - 500)
    later = (DAY_END + 100, DAY_END + 200)
    record = tmp_path / "capture_gaps.csv"
    record.write_bytes(_record_bytes(prior, gap))
    target = tmp_path / f"capture_gap_receipt_{DATE}.json"
    original_create = exact._create_receipt_no_clobber
    linked = threading.Event()
    changed = threading.Event()

    def coordinated_create(*args, **kwargs):
        result = original_create(*args, **kwargs)
        linked.set()
        assert changed.wait(2)
        return result

    def concurrent_writer():
        assert linked.wait(2)
        replacement = tmp_path / "replacement.csv"
        replacement.write_bytes(_record_bytes(prior, gap, later))
        os.replace(replacement, record)
        changed.set()

    monkeypatch.setattr(exact, "_create_receipt_no_clobber", coordinated_create)
    writer = threading.Thread(target=concurrent_writer)
    writer.start()
    with pytest.raises(exact.ExactCaptureError) as caught:
        exact.publish_outputs(receipt, record, tmp_path)
    writer.join(timeout=2)

    assert caught.value.code == "GAP_RECORD_CHANGED"
    assert record.read_bytes() == _record_bytes(prior, gap, later)
    assert not target.exists()


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
        self.kwargs = dict(_kwargs)
        self.returncode = 0
        self._body = body
        self._version = version
        self.communicate_timeout = None
        fd_path = next(part for part in command if part.startswith("/dev/fd/"))
        inherited = int(fd_path.rsplit("/", 1)[1])
        duplicate = os.dup(inherited)
        os.write(duplicate, body)
        os.close(duplicate)

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        assert timeout is not None
        self.communicate_timeout = timeout
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
    assert command[command.index("--cli-connect-timeout") + 1] == "30"
    assert command[command.index("--cli-read-timeout") + 1] == "30"
    assert any(part.startswith("/dev/fd/") for part in command)
    assert all("put-object" not in part and "delete" not in part
               and "list" not in part for part in command)
    assert calls[0].kwargs["start_new_session"] is True
    assert 0 < calls[0].communicate_timeout <= 30


class _StalledPopen:
    def __init__(self, command, **_kwargs):
        self.command = command
        self.returncode = None
        self.terminated = False
        self.killed = False
        fd_path = next(part for part in command if part.startswith("/dev/fd/"))
        inherited = int(fd_path.rsplit("/", 1)[1])
        self.writer = os.dup(inherited)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None
        self.returncode = -15

    def kill(self):
        self.killed = True
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, timeout=None):
        raise AssertionError("stalled process must be stopped before communicate")


def test_exact_get_absolute_deadline_stops_a_stalled_body(monkeypatch):
    processes = []

    def fake_popen(command, **kwargs):
        process = _StalledPopen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(exact.subprocess, "Popen", fake_popen)
    stream = exact.AwsCliExactGetStream(
        "aws", {}, exact.SOURCE_BUCKET,
        f"{exact.SOURCE_PREFIX}/raw/date={DATE}/firehose_00.ndjson",
        "version-1", 1, 0.05)
    started = time.monotonic()

    with pytest.raises(exact.ExactCaptureError) as caught:
        with stream as opened:
            opened.read(1)

    assert caught.value.code == "S3_GET_TIMEOUT"
    assert time.monotonic() - started < 1
    assert processes[0].terminated is True
    assert processes[0].returncode == -15


def test_stop_escalates_process_group_from_term_to_kill(monkeypatch):
    class Process:
        pid = 424242

        def __init__(self):
            self.returncode = None
            self.waits = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.waits += 1
            if self.returncode is None:
                raise subprocess.TimeoutExpired("aws", timeout)
            return self.returncode

    process = Process()
    signals = []

    def fake_killpg(pid, sent):
        signals.append((pid, sent))
        if sent == exact.signal.SIGKILL:
            process.returncode = -9

    read_fd, write_fd = os.pipe()
    stream = exact.AwsCliExactGetStream(
        "aws", {}, exact.SOURCE_BUCKET, "key", "version-1", 1, 1)
    stream.process = process
    stream.body = os.fdopen(read_fd, "rb", buffering=0)
    monkeypatch.setattr(exact.os, "killpg", fake_killpg)

    stream._stop()
    os.close(write_fd)

    assert signals == [
        (process.pid, exact.signal.SIGTERM),
        (process.pid, exact.signal.SIGKILL),
    ]
    assert stream.body is None


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
    assert calls[0][calls[0].index("--cli-connect-timeout") + 1] == "30"
    assert calls[0][calls[0].index("--cli-read-timeout") + 1] == "30"
    assert all("put-object" not in part and "delete" not in part
               and "list" not in part for part in calls[0])
