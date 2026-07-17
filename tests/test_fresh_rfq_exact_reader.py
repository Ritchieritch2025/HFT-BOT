#!/usr/bin/env python3
"""Offline tests for the bounded fresh-RFQ exact-VersionId reader."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fresh_rfq_exact_reader as reader  # noqa: E402


TRANSPORT = "LOCAL_TEST_DOUBLE"


def _identity(name: str, body: bytes, *, version: str | None = None) -> dict:
    return {
        "bucket": reader.SOURCE_BUCKET,
        "key": f"ec2/raw/date=2026-07-17/{name}",
        "version_id": version or f"version-{name}",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


class FakeExactClient:
    """Two-method fixture; it has no list/latest/write API."""

    def __init__(self, rows: list[tuple[dict, bytes]]):
        self.rows = {
            (row["bucket"], row["key"], row["version_id"]): body
            for row, body in rows
        }
        self.ops = []
        self.head_version = None
        self.head_size = None
        self.get_version = None
        self.get_size = None
        self.written_body = None
        self.get_fault = None
        self.head_fault = None
        self.destinations: list[Path] = []

    def head(self, bucket, key, version_id):
        self.ops.append(("head", bucket, key, version_id))
        if self.head_fault is not None:
            raise self.head_fault
        body = self.rows[(bucket, key, version_id)]
        return {
            "VersionId": version_id if self.head_version is None
            else self.head_version,
            "ContentLength": len(body) if self.head_size is None
            else self.head_size,
        }

    def get_exact(self, bucket, key, version_id, destination):
        self.ops.append(("get_exact", bucket, key, version_id))
        path = Path(destination)
        self.destinations.append(path)
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_ISREG(path.stat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        if self.get_fault is not None:
            if callable(self.get_fault):
                self.get_fault(path)
            else:
                raise self.get_fault
        body = self.rows[(bucket, key, version_id)]
        path.write_bytes(body if self.written_body is None else self.written_body)
        return {
            "VersionId": version_id if self.get_version is None
            else self.get_version,
            "ContentLength": len(body) if self.get_size is None
            else self.get_size,
        }


def _consume(session: reader.ExactReadSession, rows: list[dict]) -> list[Path]:
    paths = []
    for row in rows:
        with session.open_exact(row) as opened:
            paths.append(opened.path)
            assert opened.path.read_bytes()
            assert dict(opened.identity) == row
    return paths


def _error_code(exc: pytest.ExceptionInfo) -> str:
    return exc.value.code


def _assert_body_free(value) -> None:
    if isinstance(value, dict):
        assert "body" not in value
        assert "path" not in value
        for child in value.values():
            _assert_body_free(child)
    elif isinstance(value, list):
        for child in value:
            _assert_body_free(child)


def test_complete_set_is_one_at_a_time_private_and_body_free(tmp_path):
    bodies = [b"first-object\n", b"second-object\n"]
    rows = [
        _identity("rfq_00.ndjson", bodies[0], version="pinned-v1"),
        _identity("rfq_01.ndjson", bodies[1], version="pinned-v2"),
    ]
    client = FakeExactClient(list(zip(rows, bodies)))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    session = reader.ExactReadSession(
        list(reversed(rows)), client, transport_kind=TRANSPORT,
        temp_parent=scratch,
    )
    paths = []

    with session as active:
        for row, body in zip(rows, bodies):
            with active.open_exact(copy.deepcopy(row)) as opened:
                paths.append(opened.path)
                assert opened.path.read_bytes() == body
                assert stat.S_IMODE(opened.path.parent.stat().st_mode) == 0o700
                assert stat.S_IMODE(opened.path.stat().st_mode) == 0o600
                assert list(opened.path.parent.iterdir()) == [opened.path]
                assert dict(opened.identity) == row
                with pytest.raises(TypeError):
                    opened.identity["size"] = 0
                with pytest.raises(FrozenInstanceError):
                    opened.path = tmp_path
            assert not paths[-1].exists()
            assert not paths[-1].parent.exists()

    attestation = session.attestation
    assert list(scratch.iterdir()) == []
    assert attestation["schema"] == \
        "fresh-rfq-exact-reader-core-attestation-v1"
    assert attestation["state"] == \
        "ALL_EXPECTED_CALLER_IDENTITIES_BODY_VERIFIED"
    assert attestation["source_objects_exact_get_verified"] is False
    assert attestation["exact_body_identity_verified"] is True
    assert attestation["expected_object_count"] == 2
    assert attestation["verified_object_count"] == 2
    assert attestation["module_head_call_count"] == 2
    assert attestation["module_get_exact_call_count"] == 2
    assert attestation["max_active_object_count"] == 1
    assert attestation["caller_declared_transport_kind"] == TRANSPORT
    assert attestation["transport_attestation_state"] == \
        "CALLER_ADAPTER_UNVERIFIED"
    assert attestation["version_id_argument_supplied_on_all_calls"] is True
    assert attestation["module_list_api_call_count"] == 0
    assert attestation["module_write_api_call_count"] == 0
    assert attestation["aws_transport_verified"] is False
    assert attestation["aws_no_write_verified"] is False
    assert attestation["requires_external_iam_and_operation_audit"] is True
    assert len(attestation["read_ledger"]) == 2
    for expected, ledger in zip(
        sorted(rows, key=lambda row: (row["key"], row["version_id"])),
        attestation["read_ledger"],
    ):
        assert ledger == {
            "bucket": expected["bucket"],
            "key": expected["key"],
            "requested_version_id": expected["version_id"],
            "head_response_version_id": expected["version_id"],
            "head_response_content_length": expected["size"],
            "get_response_version_id": expected["version_id"],
            "get_response_content_length": expected["size"],
            "expected_size": expected["size"],
            "observed_size": expected["size"],
            "expected_sha256": expected["sha256"],
            "observed_sha256": expected["sha256"],
            "verification_state":
                "VERSION_ARGUMENT_HEAD_GET_RESPONSE_AND_FULL_BODY_SHA256_VERIFIED",
        }
    assert attestation["read_ledger_sha256"] == reader.canonical_sha256(
        attestation["read_ledger"]
    )
    assert attestation["ephemeral_temp_directory_mode"] == "0700"
    assert attestation["ephemeral_temp_file_mode"] == "0600"
    assert attestation["ephemeral_temp_deleted_before_return"] is True
    assert attestation["input_bodies_omitted"] is True
    assert attestation["module_durable_data_copy_count"] == 0
    for overclaim in (
        "transport_kind", "transport_kind_source", "head_call_count",
        "get_call_count", "versionless_get_count", "aws_write_call_count",
        "s3_copy_call_count", "data_objects_copied", "aws_write_authorized",
        "versionless_read_performed", "list_performed",
        "module_versionless_get_call_count",
    ):
        assert overclaim not in attestation
    assert attestation["attestation_sha256"] == reader.canonical_sha256({
        key: value for key, value in attestation.items()
        if key != "attestation_sha256"
    })
    _assert_body_free(attestation)
    assert all(op[3] in {"pinned-v1", "pinned-v2"} for op in client.ops)
    assert [op[0] for op in client.ops] == [
        "head", "get_exact", "head", "get_exact",
    ]


def test_input_permutation_produces_same_attestation(tmp_path):
    bodies = [b"a", b"bb", b"ccc"]
    rows = [_identity(f"rfq_0{i}.ndjson", body) for i, body in enumerate(bodies)]
    results = []
    for ordinal, supplied in enumerate((rows, list(reversed(rows)))):
        client = FakeExactClient(list(zip(rows, bodies)))
        session = reader.ExactReadSession(
            copy.deepcopy(supplied), client, transport_kind=TRANSPORT,
            temp_parent=tmp_path / f"scratch-{ordinal}",
        )
        (tmp_path / f"scratch-{ordinal}").mkdir()
        with session as active:
            _consume(active, supplied)
        results.append(session.attestation)
    assert results[0] == results[1]


def test_caller_aws_label_is_explicitly_not_transport_or_iam_proof(tmp_path):
    body = b"fixture-not-aws"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    session = reader.ExactReadSession(
        [row], client, transport_kind="AWS_S3_EXACT_VERSION",
        temp_parent=tmp_path,
    )
    with session as active:
        with active.open_exact(row):
            pass

    attestation = session.attestation
    assert attestation["schema"] == \
        "fresh-rfq-exact-reader-core-attestation-v1"
    assert attestation["caller_declared_transport_kind"] == \
        "AWS_S3_EXACT_VERSION"
    assert attestation["transport_attestation_state"] == \
        "CALLER_ADAPTER_UNVERIFIED"
    assert attestation["source_objects_exact_get_verified"] is False
    assert attestation["aws_transport_verified"] is False
    assert attestation["aws_no_write_verified"] is False
    assert attestation["requires_external_iam_and_operation_audit"] is True


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda row: row.update(bucket="other-bucket"), "SOURCE_BUCKET_MISMATCH"),
        (lambda row: row.update(version_id=None), "VERSION_REQUIRED"),
        (lambda row: row.update(version_id="null"), "VERSION_REQUIRED"),
        (lambda row: row.update(size=True), "OBJECT_SIZE_INVALID"),
        (lambda row: row.update(size=-1), "OBJECT_SIZE_INVALID"),
        (lambda row: row.update(sha256="A" * 64), "OBJECT_SHA256_INVALID"),
        (lambda row: row.update(key="/absolute"), "OBJECT_IDENTITY_INVALID"),
        (lambda row: row.update(key="ec2/raw/../secret"), "OBJECT_IDENTITY_INVALID"),
        (lambda row: row.update(extra="field"), "OBJECT_IDENTITY_FIELDS"),
        (lambda row: row.pop("sha256"), "OBJECT_IDENTITY_FIELDS"),
    ],
)
def test_invalid_expected_identity_fails_before_client_call(mutation, code):
    body = b"object"
    valid = _identity("rfq_00.ndjson", body)
    invalid = copy.deepcopy(valid)
    mutation(invalid)
    client = FakeExactClient([(valid, body)])
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        reader.ExactReadSession([invalid], client, transport_kind=TRANSPORT)
    assert _error_code(error) == code
    assert client.ops == []


def test_empty_duplicate_and_ambiguous_expected_sets_fail_before_io():
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    with pytest.raises(reader.FreshRfqExactReaderError) as empty:
        reader.ExactReadSession([], client, transport_kind=TRANSPORT)
    assert _error_code(empty) == "EXPECTED_SET_INVALID"
    with pytest.raises(reader.FreshRfqExactReaderError) as duplicate:
        reader.ExactReadSession(
            [row, copy.deepcopy(row)], client, transport_kind=TRANSPORT,
        )
    assert _error_code(duplicate) == "EXPECTED_SET_DUPLICATE"
    other_version = {**row, "version_id": "different-version"}
    with pytest.raises(reader.FreshRfqExactReaderError) as ambiguous:
        reader.ExactReadSession(
            [row, other_version], client, transport_kind=TRANSPORT,
        )
    assert _error_code(ambiguous) == "EXPECTED_KEY_AMBIGUOUS"
    assert client.ops == []


@pytest.mark.parametrize("transport", [None, "", "aws", "LOCAL TEST", "_LOCAL"])
def test_transport_kind_must_be_explicit_and_canonical(transport):
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        reader.ExactReadSession(
            [row], client, transport_kind=transport,
        )
    assert _error_code(error) == "TRANSPORT_KIND_INVALID"
    assert client.ops == []


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("head_version", "wrong-version", "RESPONSE_VERSION_MISMATCH"),
        ("head_size", 999, "RESPONSE_SIZE_MISMATCH"),
        ("get_version", "wrong-version", "RESPONSE_VERSION_MISMATCH"),
        ("get_size", 999, "RESPONSE_SIZE_MISMATCH"),
    ],
)
def test_head_and_get_responses_must_match_pinned_identity(
    tmp_path, field, value, code,
):
    body = b"pinned-body"
    row = _identity("rfq_00.ndjson", body, version="immutable-A")
    client = FakeExactClient([(row, body)])
    setattr(client, field, value)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=scratch,
        ) as session:
            with session.open_exact(row):
                pass
    assert _error_code(error) == code
    assert list(scratch.iterdir()) == []
    assert all(op[3] == "immutable-A" for op in client.ops)


@pytest.mark.parametrize(
    ("written", "code"),
    [
        (b"short", "BODY_SIZE_MISMATCH"),
        (b"pinned-bodz", "BODY_SHA256_MISMATCH"),
        (b"pinned-body-extra", "BODY_SIZE_MISMATCH"),
    ],
)
def test_streaming_full_body_size_and_sha_are_required(tmp_path, written, code):
    body = b"pinned-body"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    client.written_body = written
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=scratch,
        ) as session:
            with session.open_exact(row):
                pass
    assert _error_code(error) == code
    assert list(scratch.iterdir()) == []


def test_missing_expected_object_rejects_normal_session_exit(tmp_path):
    bodies = [b"a", b"b"]
    rows = [_identity(f"rfq_0{i}.ndjson", body) for i, body in enumerate(bodies)]
    client = FakeExactClient(list(zip(rows, bodies)))
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        with reader.ExactReadSession(
            rows, client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(rows[0]):
                pass
    assert _error_code(error) == "INCOMPLETE_EXACT_SET"
    assert not client.destinations[0].parent.exists()


def test_unexpected_and_duplicate_reads_are_rejected_and_poison_session(tmp_path):
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    extra_body = b"extra"
    extra = _identity("rfq_01.ndjson", extra_body)
    client = FakeExactClient([(row, body), (extra, extra_body)])

    with pytest.raises(reader.FreshRfqExactReaderError) as unexpected:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(extra):
                pass
    assert _error_code(unexpected) == "UNEXPECTED_OBJECT"
    assert client.ops == []

    client = FakeExactClient([(row, body)])
    with pytest.raises(reader.FreshRfqExactReaderError) as duplicate:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(row):
                pass
            with session.open_exact(row):
                pass
    assert _error_code(duplicate) == "DUPLICATE_READ"
    assert [op[0] for op in client.ops] == ["head", "get_exact"]

    client = FakeExactClient([(row, body), (extra, extra_body)])
    with pytest.raises(reader.FreshRfqExactReaderError) as poisoned:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            try:
                with session.open_exact(extra):
                    pass
            except reader.FreshRfqExactReaderError:
                pass
    assert _error_code(poisoned) == "SESSION_POISONED"


def test_nested_open_is_rejected_and_both_temp_and_session_fail_closed(tmp_path):
    bodies = [b"a", b"b"]
    rows = [_identity(f"rfq_0{i}.ndjson", body) for i, body in enumerate(bodies)]
    client = FakeExactClient(list(zip(rows, bodies)))
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        with reader.ExactReadSession(
            rows, client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(rows[0]):
                with session.open_exact(rows[1]):
                    pass
    assert _error_code(error) == "CONCURRENT_OBJECT_OPEN"
    assert all(not path.parent.exists() for path in client.destinations)


def test_multithread_open_is_rejected_without_blocking_and_poisons_session(
    tmp_path,
):
    bodies = [b"thread-a", b"thread-b"]
    rows = [_identity(f"rfq_0{i}.ndjson", body) for i, body in enumerate(bodies)]
    client = FakeExactClient(list(zip(rows, bodies)))
    entered = threading.Event()
    release = threading.Event()
    worker_errors: list[BaseException] = []

    def consume_first(session):
        try:
            with session.open_exact(rows[0]):
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("fixture release timed out")
        except BaseException as exc:  # captured and asserted in the main thread
            worker_errors.append(exc)

    with pytest.raises(reader.FreshRfqExactReaderError) as poisoned:
        with reader.ExactReadSession(
            rows, client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            worker = threading.Thread(target=consume_first, args=(session,))
            worker.start()
            assert entered.wait(timeout=5)
            with pytest.raises(reader.FreshRfqExactReaderError) as concurrent:
                with session.open_exact(rows[1]):
                    pass
            assert _error_code(concurrent) == "CONCURRENT_OBJECT_OPEN"
            release.set()
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert worker_errors == []

    assert _error_code(poisoned) == "SESSION_POISONED"
    assert all(op[2] != rows[1]["key"] for op in client.ops)
    assert all(not path.parent.exists() for path in client.destinations)


@pytest.mark.parametrize("fault", ["replace", "mode", "extra"])
def test_private_regular_0600_single_file_contract_is_enforced(tmp_path, fault):
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])

    def damage(path: Path):
        if fault == "replace":
            path.unlink()
            path.write_bytes(body)
            os.chmod(path, 0o600)
        elif fault == "mode":
            path.write_bytes(body)
            os.chmod(path, 0o644)
        else:
            (path.parent / "unexpected").write_bytes(b"x")

    client.get_fault = damage
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(row):
                pass
    assert _error_code(error) in {"TEMP_FILE_INVALID", "TEMP_SCOPE_VIOLATION"}
    assert all(not path.parent.exists() for path in client.destinations)


def test_replacement_between_lstat_and_open_is_rejected(tmp_path, monkeypatch):
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    real_open = reader.os.open
    replaced = False

    def replace_on_read(path, flags, *args):
        nonlocal replaced
        candidate = Path(path)
        if (
            not replaced
            and candidate.name == "exact-version.bin"
            and flags & os.O_ACCMODE == os.O_RDONLY
        ):
            replaced = True
            candidate.rename(candidate.with_name("original-inode.bin"))
            candidate.write_bytes(body)
            os.chmod(candidate, 0o600)
        return real_open(path, flags, *args)

    monkeypatch.setattr(reader.os, "open", replace_on_read)
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(row):
                pass
    assert replaced is True
    assert _error_code(error) == "TEMP_FILE_INVALID"
    assert all(not path.parent.exists() for path in client.destinations)


@pytest.mark.parametrize("mutation_stage", ["client", "consumer"])
def test_private_root_mode_is_rechecked_after_get_and_consumer(
    tmp_path, mutation_stage,
):
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    if mutation_stage == "client":
        client.get_fault = lambda path: os.chmod(path.parent, 0o777)

    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(row) as opened:
                if mutation_stage == "consumer":
                    os.chmod(opened.path.parent, 0o777)
    assert _error_code(error) == "TEMP_ROOT_INVALID"
    assert all(not path.parent.exists() for path in client.destinations)


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("chmod", "TEMP_CREATE_FAILED"),
        ("lstat", "TEMP_CREATE_FAILED"),
        ("mode", "TEMP_MODE_INVALID"),
    ],
)
def test_partial_private_temp_root_is_discarded_before_failure(
    tmp_path, monkeypatch, fault, code,
):
    created = []
    real_mkdtemp = reader.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(reader.tempfile, "mkdtemp", tracked_mkdtemp)
    if fault == "chmod":
        monkeypatch.setattr(
            reader.os, "chmod",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("fixture chmod failure")
            ),
        )
    elif fault == "lstat":
        monkeypatch.setattr(
            reader.Path, "lstat",
            lambda _self: (_ for _ in ()).throw(
                OSError("fixture lstat failure")
            ),
        )
    else:
        monkeypatch.setattr(
            reader.Path, "lstat",
            lambda _self: SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o755, st_nlink=2,
            ),
        )

    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        reader._private_temp_root(tmp_path)
    assert _error_code(error) == code
    assert len(created) == 1
    assert not os.path.lexists(created[0])


def test_consumer_mutation_is_detected_and_cleanup_is_unconditional(tmp_path):
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    with pytest.raises(reader.FreshRfqExactReaderError) as error:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(row) as opened:
                with opened.path.open("ab") as handle:
                    handle.write(b"changed")
    assert _error_code(error) == "TEMP_FILE_CHANGED"
    assert all(not path.parent.exists() for path in client.destinations)


def test_client_and_consumer_failures_leave_no_temp_tree(tmp_path):
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    client.get_fault = RuntimeError("fixture get failure")
    with pytest.raises(reader.FreshRfqExactReaderError) as get_error:
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(row):
                pass
    assert _error_code(get_error) == "CLIENT_GET_FAILED"
    assert all(not path.parent.exists() for path in client.destinations)

    class ConsumerFailure(RuntimeError):
        pass

    client = FakeExactClient([(row, body)])
    with pytest.raises(ConsumerFailure):
        with reader.ExactReadSession(
            [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
        ) as session:
            with session.open_exact(row) as opened:
                path = opened.path
                raise ConsumerFailure("parse failed")
    assert not path.parent.exists()


def test_session_exit_cleans_manually_entered_active_object(tmp_path):
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    session = reader.ExactReadSession(
        [row], client, transport_kind=TRANSPORT, temp_parent=tmp_path,
    )
    session.__enter__()
    object_context = session.open_exact(row)
    opened = object_context.__enter__()
    root = opened.path.parent
    assert root.exists()

    with pytest.raises(reader.FreshRfqExactReaderError) as exited:
        session.__exit__(None, None, None)
    assert _error_code(exited) == "SESSION_POISONED"
    assert not root.exists()

    with pytest.raises(reader.FreshRfqExactReaderError) as closed:
        object_context.__exit__(None, None, None)
    assert _error_code(closed) == "TEMP_FILE_CHANGED"
    assert not root.exists()


def test_attestation_is_unavailable_until_complete_and_session_is_single_use():
    body = b"object"
    row = _identity("rfq_00.ndjson", body)
    client = FakeExactClient([(row, body)])
    session = reader.ExactReadSession(
        [row], client, transport_kind=TRANSPORT,
    )
    with pytest.raises(reader.FreshRfqExactReaderError) as unavailable:
        _ = session.attestation
    assert _error_code(unavailable) == "ATTESTATION_NOT_AVAILABLE"
    with session as active:
        with active.open_exact(row):
            pass
    with pytest.raises(reader.FreshRfqExactReaderError) as reenter:
        with session:
            pass
    assert _error_code(reenter) == "SESSION_STATE"


def test_module_has_no_aws_network_process_or_mutation_implementation():
    source = inspect.getsource(reader)
    lowered = source.lower()
    for forbidden in (
        "import boto3", "import botocore", "import subprocess",
        "import urllib", "put_object", "delete_object", "copy_object",
        "put-object", "delete-object", "copy-object", "list_objects",
        "list-object",
    ):
        assert forbidden not in lowered
    assert "self._client.head(" in source
    assert "self._client.get_exact(" in source
