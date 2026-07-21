#!/usr/bin/env python3
"""Offline state-machine contracts for the root-owned Deep03 one-shot ARM."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "deploy" / "w09" / "deep03_one_shot_arm.py"


def _load():
    spec = importlib.util.spec_from_file_location("deep03_one_shot_arm_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _identity(suffix: str = "a") -> dict[str, str]:
    return {
        "release_id": "D3-W2A-2026-07-19.14",
        "authority_sha256": suffix * 64,
        "arm_sha256": chr(ord(suffix) + 1) * 64,
    }


def _claim_root(tmp_path: Path) -> Path:
    root = tmp_path / "one-shot"
    root.mkdir(mode=0o750)
    return root


def _proc_cgroup(tmp_path: Path, cgroup: str | None = None) -> Path:
    path = tmp_path / "proc-self-cgroup"
    path.write_text("0::%s\n" % (cgroup or "/system.slice/w09-exploratory-autoresearch.service"))
    return path


def test_exact_arm_claims_once_and_is_permanently_consumed(tmp_path):
    arm = _load()
    root = _claim_root(tmp_path)
    identity = _identity()
    invocation = "1" * 32
    proc_cgroup = _proc_cgroup(tmp_path)
    active = arm.claim_one_shot(
        claim_root=root,
        identity=identity,
        invocation_id=invocation,
        expected_owner_uid=os.getuid(),
        proc_cgroup_path=proc_cgroup,
        now=dt.datetime(2026, 7, 18, 14, tzinfo=dt.timezone.utc),
    )
    assert active["state"] == "ACTIVE"
    assert active["service_cgroup"] == arm.SERVICE_CGROUP
    path = root / (identity["arm_sha256"] + ".json")
    assert path.stat().st_mode & 0o222 == 0
    validated, raw = arm.validate_active_claim(
        claim_root=root,
        identity=identity,
        invocation_id=invocation,
        expected_owner_uid=os.getuid(),
        proc_cgroup_path=proc_cgroup,
    )
    assert validated == active
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(arm.OneShotArmError, match="already claimed or consumed"):
        arm.claim_one_shot(
            claim_root=root,
            identity=identity,
            invocation_id=invocation,
            expected_owner_uid=os.getuid(),
            proc_cgroup_path=proc_cgroup,
        )

    consumed = arm.consume_one_shot(
        claim_root=root,
        identity=identity,
        invocation_id=invocation,
        service_result="success",
        exit_code="exited",
        exit_status="0",
        expected_owner_uid=os.getuid(),
        proc_cgroup_path=proc_cgroup,
        now=dt.datetime(2026, 7, 18, 15, tzinfo=dt.timezone.utc),
    )
    assert consumed["state"] == "CONSUMED"
    assert consumed["service_result"] == "success"
    assert json.loads(path.read_text()) == consumed
    with pytest.raises(arm.OneShotArmError, match="field mismatch: state"):
        arm.validate_active_claim(
            claim_root=root,
            identity=identity,
            invocation_id=invocation,
            expected_owner_uid=os.getuid(),
            proc_cgroup_path=proc_cgroup,
        )
    with pytest.raises(arm.OneShotArmError, match="already claimed or consumed"):
        arm.claim_one_shot(
            claim_root=root,
            identity=identity,
            invocation_id=invocation,
            expected_owner_uid=os.getuid(),
            proc_cgroup_path=proc_cgroup,
        )
    # ExecStopPost may be asked to settle the same invocation twice; this is
    # idempotent, but never changes the recorded first terminal outcome.
    assert arm.consume_one_shot(
        claim_root=root,
        identity=identity,
        invocation_id=invocation,
        service_result="failure",
        exit_code="exited",
        exit_status="2",
        expected_owner_uid=os.getuid(),
        proc_cgroup_path=proc_cgroup,
    ) == consumed


def test_atomic_claim_has_one_winner_under_concurrency(tmp_path):
    arm = _load()
    root = _claim_root(tmp_path)
    identity = _identity("c")
    proc_cgroup = _proc_cgroup(tmp_path)

    def attempt(index: int) -> str:
        try:
            arm.claim_one_shot(
                claim_root=root,
                identity=identity,
                invocation_id=("%032x" % (index + 1)),
                expected_owner_uid=os.getuid(),
                proc_cgroup_path=proc_cgroup,
            )
            return "won"
        except arm.OneShotArmError:
            return "refused"

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert outcomes.count("won") == 1
    assert outcomes.count("refused") == 7
    assert len(list(root.glob("*.json"))) == 1


def test_claim_is_bound_to_systemd_invocation_and_fixed_unit(tmp_path):
    arm = _load()
    root = _claim_root(tmp_path)
    identity = _identity("e")
    proc_cgroup = _proc_cgroup(tmp_path)
    arm.claim_one_shot(
        claim_root=root,
        identity=identity,
        invocation_id="a" * 32,
        expected_owner_uid=os.getuid(),
        proc_cgroup_path=proc_cgroup,
    )
    with pytest.raises(arm.OneShotArmError, match="invocation_id"):
        arm.validate_active_claim(
            claim_root=root,
            identity=identity,
            invocation_id="b" * 32,
            expected_owner_uid=os.getuid(),
            proc_cgroup_path=proc_cgroup,
        )
    with pytest.raises(arm.OneShotArmError, match="fixed W09 service"):
        arm.validate_active_claim(
            claim_root=root,
            identity=identity,
            invocation_id="a" * 32,
            service_unit="shell.service",
            expected_owner_uid=os.getuid(),
            proc_cgroup_path=proc_cgroup,
        )


def test_same_invocation_outside_service_cgroup_is_refused(tmp_path):
    arm = _load()
    root = _claim_root(tmp_path)
    identity = _identity("7")
    invocation = "d" * 32
    service_cgroup = _proc_cgroup(tmp_path)
    arm.claim_one_shot(
        claim_root=root,
        identity=identity,
        invocation_id=invocation,
        expected_owner_uid=os.getuid(),
        proc_cgroup_path=service_cgroup,
    )
    attacker_cgroup = _proc_cgroup(
        tmp_path,
        "/user.slice/user-1000.slice/user@1000.service/app.slice/ssh-session.scope",
    )
    with pytest.raises(arm.OneShotArmError, match="outside the fixed W09 service cgroup"):
        arm.validate_active_claim(
            claim_root=root,
            identity=identity,
            invocation_id=invocation,
            expected_owner_uid=os.getuid(),
            proc_cgroup_path=attacker_cgroup,
        )
    with pytest.raises(arm.OneShotArmError, match="outside the fixed W09 service cgroup"):
        arm.consume_one_shot(
            claim_root=root,
            identity=identity,
            invocation_id=invocation,
            service_result="success",
            exit_code="exited",
            exit_status="0",
            expected_owner_uid=os.getuid(),
            proc_cgroup_path=attacker_cgroup,
        )


def test_identity_loader_rejects_mutable_or_cross_bound_arm(tmp_path):
    arm = _load()
    authority_path = tmp_path / "AUTHORITY.json"
    arm_path = tmp_path / "ARM.json"
    authority = {
        "schema_version": arm.AUTHORITY_SCHEMA,
        "state": "ACTIVE",
        "release_id": "D3-W2A-2026-07-19.14",
    }
    authority_path.write_text(json.dumps(authority) + "\n")
    authority_sha = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    arm_path.write_text(json.dumps({
        "schema_version": arm.ARM_SCHEMA,
        "state": "ARMED",
        "release_id": authority["release_id"],
        "authority_sha256": authority_sha,
    }) + "\n")
    authority_path.chmod(0o444)
    arm_path.chmod(0o444)
    identity = arm.load_arm_identity(
        authority_path=authority_path,
        arm_path=arm_path,
        expected_owner_uid=os.getuid(),
    )
    assert identity["authority_sha256"] == authority_sha
    assert identity["arm_sha256"] == hashlib.sha256(arm_path.read_bytes()).hexdigest()

    arm_path.chmod(0o644)
    with pytest.raises(arm.OneShotArmError, match="writable"):
        arm.load_arm_identity(
            authority_path=authority_path,
            arm_path=arm_path,
            expected_owner_uid=os.getuid(),
        )
