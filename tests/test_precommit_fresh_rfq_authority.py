"""Focused local transaction tests for fresh-RFQ authority precommit."""

import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "precommit_fresh_rfq_authority.py"
NOW = dt.datetime(2026, 7, 17, 12, 0, 0, tzinfo=dt.timezone.utc)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "precommit_fresh_rfq_authority", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


precommit = load_module()
OWNER_UID = os.geteuid()


def precommit_local(authority_value, output, **kwargs):
    kwargs.setdefault("expected_owner_uid", OWNER_UID)
    return precommit.precommit_authority(authority_value, output, **kwargs)


def sha(label):
    return hashlib.sha256(label.encode()).hexdigest()


def deny_rows():
    return [{
        "key": f"raw_rfq/date=2026-06-{1 + index // 24:02d}/"
               f"rfq_{index % 24:02d}.ndjson.{1000 + index}",
        "size": index + 1,
        "sha256": sha(f"old-{index}"),
    } for index in range(284)]


def authority(
    monkeypatch, *, t0="2026-07-17T13:00:00Z",
    created="2026-07-17T11:59:00Z",
):
    rows = deny_rows()
    fingerprint = precommit.fresh.old_object_set_sha256(rows)
    monkeypatch.setattr(
        precommit.fresh, "OLD_284_OBJECT_SET_SHA256", fingerprint)
    return precommit.fresh.build_fresh_epoch_authority(
        operator_authorization_sha256=
            precommit.fresh.OPERATOR_AUTHORIZATION_SHA256,
        strict_t0_utc=t0,
        deployment_commit="a" * 40,
        generation="fresh-rfq-generation-1",
        created_at_utc=created,
        deny_identities=rows,
    )


def test_success_is_canonical_read_only_and_create_only(tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    output = tmp_path / "authority.json"
    receipt = precommit_local(auth, output, now=NOW)

    raw = output.read_bytes()
    envelope = json.loads(raw)
    assert raw == precommit.fresh.canonical_bytes(envelope) + b"\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o444
    assert precommit.validate_precommit_envelope(envelope) == envelope
    assert envelope["authority"] == auth
    assert envelope["authority_sha256"] == auth["authority_sha256"]
    assert envelope["operator_authorization_sha256"] == \
        precommit.fresh.OPERATOR_AUTHORIZATION_SHA256
    assert envelope["deployment_commit"] == auth["deployment_commit"]
    assert envelope["strict_t0_utc"] == auth["strict_t0_utc"]
    assert envelope["precommitted_at_utc"] == "2026-07-17T12:00:00Z"
    assert envelope["minimum_lead_seconds"] == 300
    assert receipt == {
        "schema": "fresh-rfq-authority-precommit-receipt-v2",
        "output": str(output),
        "authority_sha256": auth["authority_sha256"],
        "operator_authorization_sha256":
            precommit.fresh.OPERATOR_AUTHORIZATION_SHA256,
        "deployment_commit": auth["deployment_commit"],
        "strict_t0_utc": auth["strict_t0_utc"],
        "created_at_utc": auth["created_at_utc"],
        "precommitted_at_utc": envelope["precommitted_at_utc"],
        "minimum_lead_seconds": 300,
        "envelope_sha256": envelope["envelope_sha256"],
        "bytes": len(raw),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
    }

    before = raw
    with pytest.raises(precommit.PrecommitError) as error:
        precommit_local(auth, output, now=NOW)
    assert error.value.code == "OUTPUT_EXISTS"
    assert output.read_bytes() == before


def test_existing_partial_and_symlink_are_never_removed_or_overwritten(
        tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"partial-from-before-call")
    with pytest.raises(precommit.PrecommitError, match="OUTPUT_EXISTS"):
        precommit_local(auth, existing, now=NOW)
    assert existing.read_bytes() == b"partial-from-before-call"

    target = tmp_path / "target.json"
    target.write_bytes(b"target-sentinel")
    link = tmp_path / "authority-link.json"
    link.symlink_to(target)
    with pytest.raises(precommit.PrecommitError, match="OUTPUT_EXISTS"):
        precommit_local(auth, link, now=NOW)
    assert link.is_symlink()
    assert target.read_bytes() == b"target-sentinel"


@pytest.mark.parametrize("case,expected_code", [
    ("bad_schema", "AUTHORITY_SCHEMA"),
    ("past_t0", "T0_LEAD_TOO_SHORT"),
    ("future_created", "AUTHORITY_FROM_FUTURE"),
    ("stale_created", "AUTHORITY_REPLAY_WINDOW"),
    ("short_lead", "T0_LEAD_TOO_SHORT"),
    ("narrow_age_window", "AUTHORITY_REPLAY_WINDOW"),
])
def test_schema_and_execution_time_gates_fail_before_create(
        tmp_path, monkeypatch, case, expected_code):
    if case == "past_t0":
        auth = authority(
            monkeypatch, t0="2026-07-17T11:00:00Z",
            created="2026-07-17T10:59:00Z")
    elif case == "future_created":
        auth = authority(monkeypatch, created="2026-07-17T12:00:01Z")
    elif case == "stale_created":
        auth = authority(monkeypatch, created="2026-07-17T11:49:59Z")
    else:
        auth = authority(monkeypatch)
    kwargs = {}
    if case == "bad_schema":
        auth = dict(auth, schema="wrong")
    elif case == "past_t0":
        kwargs["max_created_age_seconds"] = 7200
    elif case == "short_lead":
        kwargs["min_lead_seconds"] = 3601
    elif case == "narrow_age_window":
        kwargs["max_created_age_seconds"] = 59
    output = tmp_path / f"{case}.json"
    with pytest.raises((precommit.PrecommitError,
                        precommit.fresh.FreshRfqError)) as error:
        precommit_local(auth, output, now=NOW, **kwargs)
    assert error.value.code == expected_code
    assert not output.exists()


def test_short_os_write_then_failure_removes_only_new_partial(
        tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    output = tmp_path / "partial-write.json"
    real_write = precommit.os.write
    calls = 0

    def partial_then_fail(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:max(1, len(data) // 2)])
        raise OSError("injected write failure")

    monkeypatch.setattr(precommit.os, "write", partial_then_fail)
    with pytest.raises(precommit.PrecommitError) as error:
        precommit_local(auth, output, now=NOW)
    assert error.value.code == "OUTPUT_WRITE_FAILED"
    assert calls == 2
    assert not output.exists()


@pytest.mark.parametrize("stage", [
    "after_create", "after_write", "after_file_fsync",
    "after_parent_fsync", "after_readback_open",
    "after_readback_validation",
])
def test_fault_hook_rolls_back_every_transaction_phase(
        tmp_path, monkeypatch, stage):
    auth = authority(monkeypatch)
    output = tmp_path / f"fault-{stage}.json"

    def fail_at(current):
        if current == stage:
            raise RuntimeError(f"injected {stage}")

    with pytest.raises(RuntimeError, match=f"injected {stage}"):
        precommit_local(
            auth, output, now=lambda: NOW, fault_hook=fail_at)
    assert not output.exists()


def test_fixed_fd_readback_rejects_corruption_and_cleans_new_file(
        tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    output = tmp_path / "corrupt-before-readback.json"

    def corrupt(stage):
        if stage == "after_parent_fsync":
            os.chmod(output, 0o644)
            output.write_bytes(b'{"schema":"corrupt"}\n')

    with pytest.raises(precommit.PrecommitError) as error:
        precommit_local(
            auth, output, now=NOW, fault_hook=corrupt)
    assert error.value.code == "READBACK_IDENTITY"
    assert not output.exists()


def test_success_fsyncs_regular_file_then_parent_directory(
        tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    output = tmp_path / "fsync-proof.json"
    real_fsync = precommit.os.fsync
    synced_types = []

    def recording_fsync(fd):
        mode = os.fstat(fd).st_mode
        synced_types.append("directory" if stat.S_ISDIR(mode) else "file")
        return real_fsync(fd)

    monkeypatch.setattr(precommit.os, "fsync", recording_fsync)
    precommit_local(auth, output, now=NOW)
    assert synced_types[:2] == ["file", "directory"]


def test_cli_reads_stdin_or_input_and_stdout_never_contains_deny_set(
        tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    for source_kind in ("stdin", "file"):
        output = tmp_path / f"{source_kind}-authority.json"
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = ["--output", str(output),
                "--expected-owner-uid", str(OWNER_UID)]
        stdin = io.StringIO(json.dumps(auth))
        if source_kind == "file":
            source = tmp_path / "input.json"
            source.write_text(json.dumps(auth))
            argv += ["--input", str(source)]
            stdin = io.StringIO("must-not-be-read")
        rc = precommit.main(
            argv, stdin=stdin, stdout=stdout, stderr=stderr, now=NOW)
        assert rc == 0, stderr.getvalue()
        line = stdout.getvalue()
        small = json.loads(line)
        assert set(small) == {
            "schema", "output", "authority_sha256", "strict_t0_utc",
            "operator_authorization_sha256", "deployment_commit",
            "created_at_utc", "precommitted_at_utc",
            "minimum_lead_seconds", "envelope_sha256", "bytes",
            "file_sha256",
        }
        assert "deny_identities" not in line
        assert "raw_rfq/" not in line
        assert len(line) < 1000
        assert line == json.dumps(
            small, sort_keys=True, separators=(",", ":")) + "\n"


def test_cli_bad_json_is_nonzero_and_has_no_stdout(tmp_path):
    output = tmp_path / "never-created.json"
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = precommit.main(
        ["--output", str(output)], stdin=io.StringIO("{bad"),
        stdout=stdout, stderr=stderr, now=NOW)
    assert rc != 0
    assert stdout.getvalue() == ""
    assert "REFUSED" in stderr.getvalue()
    assert not output.exists()


def test_input_limit_accepts_exactly_one_mib_and_rejects_one_byte_more():
    exact = b" " * (precommit.MAX_INPUT_BYTES - 2) + b"{}"
    assert len(exact) == 1024 * 1024
    assert precommit.load_authority(stdin=io.BytesIO(exact)) == {}

    oversized = exact + b" "
    with pytest.raises(precommit.PrecommitError) as error:
        precommit.load_authority(stdin=io.BytesIO(oversized))
    assert error.value.code == "INPUT_TOO_LARGE"


def test_envelope_limit_is_checked_before_create(tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    envelope = precommit.build_precommit_envelope(
        auth, precommitted_at=NOW)
    payload_size = len(precommit.fresh.canonical_bytes(envelope)) + 1
    monkeypatch.setattr(precommit, "MAX_ENVELOPE_BYTES", payload_size - 1)
    output = tmp_path / "too-large.json"
    with pytest.raises(precommit.PrecommitError) as error:
        precommit_local(auth, output, now=NOW)
    assert error.value.code == "ENVELOPE_TOO_LARGE"
    assert not output.exists()


def test_minimum_lead_floor_and_owner_gate_fail_before_create(
        tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    too_short = tmp_path / "too-short.json"
    with pytest.raises(precommit.PrecommitError) as error:
        precommit_local(auth, too_short, now=NOW, min_lead_seconds=299)
    assert error.value.code == "INVALID_WINDOW"
    assert not too_short.exists()

    wrong_owner = tmp_path / "wrong-owner.json"
    with pytest.raises(precommit.PrecommitError) as error:
        precommit.precommit_authority(
            auth, wrong_owner, now=NOW,
            expected_owner_uid=OWNER_UID + 1)
    assert error.value.code == "OUTPUT_OWNER"
    assert not wrong_owner.exists()


def test_group_world_writable_parent_and_ancestor_symlink_are_rejected(
        tmp_path, monkeypatch):
    auth = authority(monkeypatch)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    try:
        output = unsafe / "authority.json"
        with pytest.raises(precommit.PrecommitError) as error:
            precommit_local(auth, output, now=NOW)
        assert error.value.code == "OUTPUT_PARENT_UNSAFE"
        assert not output.exists()
    finally:
        unsafe.chmod(0o700)

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(real_parent, target_is_directory=True)
    linked_output = ancestor_link / "authority.json"
    with pytest.raises(precommit.PrecommitError) as error:
        precommit_local(auth, linked_output, now=NOW)
    assert error.value.code == "OUTPUT_PARENT_UNSAFE"
    assert not (real_parent / "authority.json").exists()


def test_precommit_never_repairs_owner_or_mode_after_create():
    source = TOOL.read_text()
    assert "os.chown(" not in source
    assert "os.fchown(" not in source
    assert "os.chmod(" not in source
    assert "os.fchmod(" not in source
