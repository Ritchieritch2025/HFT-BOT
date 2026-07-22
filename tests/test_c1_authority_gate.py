from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy" / "w09" / "c1_authority_gate.py"
SPEC = importlib.util.spec_from_file_location("c1_authority_gate", SOURCE)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

NOW = dt.datetime(2026, 7, 22, 16, 0, tzinfo=dt.timezone.utc)
COMMIT = "a" * 40
RELEASE = "c1-real-fill-20260722-01"


def _utc(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _immutable(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_symlink():
        path.chmod(0o644)
    path.write_bytes(raw)
    path.chmod(0o444)


class Bundle:
    def __init__(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.root = tmp_path
        self.uid = os.getuid()
        self.authority_path = tmp_path / "AUTHORITY.json"
        self.arm_path = tmp_path / "EXECUTION_ARM.json"
        self.approval_path = tmp_path / "OPERATOR_APPROVAL.txt"
        self.approval_signature_path = tmp_path / "OPERATOR_APPROVAL.txt.sig"
        self.spec_path = tmp_path / "C1_SPEC.md"
        self.config_path = tmp_path / "C1_CONFIG.json"
        self.runtime_path = tmp_path / "c1-release-commit.txt"
        self.runtime_file_paths: dict[str, Path] = {}
        self.runtime_file_raw: dict[str, bytes] = {}
        self.upstream_paths: dict[str, Path] = {}
        self.upstream_raw: dict[str, bytes] = {}
        ssh_keygen = shutil.which("ssh-keygen")
        assert ssh_keygen is not None
        self.ssh_keygen_path = Path(ssh_keygen).resolve()
        self.ssh_keygen_sha256 = hashlib.sha256(
            self.ssh_keygen_path.read_bytes()
        ).hexdigest()
        self.signing_key = tmp_path / "test-operator-ed25519"
        subprocess.run(
            [
                str(self.ssh_keygen_path),
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(self.signing_key),
            ],
            check=True,
        )
        public_fields = (self.signing_key.with_suffix(".pub")).read_text().split()
        self.allowed_signers_line = (
            f"{gate.OPERATOR_SIGNER_IDENTITY} {public_fields[0]} {public_fields[1]}"
        )
        self.receipt_root = tmp_path / "one-shot"
        self.receipt_root.mkdir(mode=0o750)
        self.receipt_root.chmod(0o750)
        self.manifest_paths: dict[str, Path] = {}
        self.manifest_raw: dict[str, bytes] = {}
        for stage in gate.CHECKPOINT_STAGES:
            path = tmp_path / "checkpoints" / stage / "MANIFEST.json"
            raw = _json_bytes({"schema_version": "test-checkpoint-v1", "stage": stage})
            _immutable(path, raw)
            self.manifest_paths[stage] = path
            self.manifest_raw[stage] = raw
        self.spec_raw = _json_bytes({"schema_version": "c1-test-spec-v1"})
        self.config_raw = _json_bytes({"schema_version": "c1-test-config-v1"})
        _immutable(self.spec_path, self.spec_raw)
        _immutable(self.config_path, self.config_raw)
        _immutable(self.runtime_path, (COMMIT + "\n").encode("ascii"))
        for name in gate.RUNTIME_FILE_PATHS:
            path = tmp_path / "runtime" / name
            raw = (f"# exact test runtime: {name}\n").encode("ascii")
            _immutable(path, raw)
            path.chmod(gate.RUNTIME_FILE_MODES[name])
            self.runtime_file_paths[name] = path
            self.runtime_file_raw[name] = raw
        for name in gate.UPSTREAM_PATHS:
            path = tmp_path / "upstream" / (name + ".json")
            raw = _json_bytes({"schema_version": "test-upstream-v1", "name": name})
            _immutable(path, raw)
            path.chmod(0o640)
            self.upstream_paths[name] = path
            self.upstream_raw[name] = raw
        self.authority = self.make_authority()
        self.arm: dict[str, object] = {}
        self.install(self.authority)

    def _approval_for(self, value: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": gate.APPROVAL_SCHEMA,
            "decision": "APPROVE_ONE_SHOT_OFFLINE_C1",
            "release_id": value["release_id"],
            "run_id": value["authorized_run_id"],
            "arm_nonce": value["authorized_arm_nonce"],
            "authority_issued_at_utc": value["issued_at_utc"],
            "authority_expires_at_utc": value["expires_at_utc"],
            "arm_not_before_utc": value["authorized_arm_not_before_utc"],
            "arm_expires_at_utc": value["authorized_arm_expires_at_utc"],
            "authorized_instance_id": value["authorized_instance_id"],
            "authorized_instance_type": value["authorized_instance_type"],
            "authorized_role": value["authorized_role"],
            "exact_git_commit": value["exact_git_commit"],
            "runtime_file_sha256s": value["runtime_file_sha256s"],
            "upstream_sha256s": value["upstream_sha256s"],
            "spec_sha256": value["spec_sha256"],
            "config_sha256": value["config_sha256"],
            "checkpoint_root": value["checkpoint_root"],
            "checkpoint_manifest_sha256s": value[
                "checkpoint_manifest_sha256s"
            ],
            "eligible_dates": value["eligible_dates"],
            "authorized_read_roots": value["authorized_read_roots"],
            "authorized_write_root": value["authorized_write_root"],
            "max_runtime_seconds": value["max_runtime_seconds"],
            "cost_cap_usd": value["cost_cap_usd"],
            "live_order_permission": value["live_order_permission"],
            "production_mutation": value["production_mutation"],
            "s3_write_permission": value["s3_write_permission"],
            "rfq_included": value["rfq_included"],
        }

    def _sign_approval(self, approval: dict[str, object]) -> tuple[bytes, bytes]:
        raw = gate._canonical_approval_bytes(approval)
        if self.approval_path.exists():
            self.approval_path.chmod(0o600)
        self.approval_path.write_bytes(raw)
        self.approval_path.chmod(0o600)
        if self.approval_signature_path.exists():
            self.approval_signature_path.unlink()
        subprocess.run(
            [
                str(self.ssh_keygen_path),
                "-Y",
                "sign",
                "-f",
                str(self.signing_key),
                "-n",
                gate.OPERATOR_SIGNATURE_NAMESPACE,
                str(self.approval_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        signature = self.approval_signature_path.read_bytes()
        self.approval_path.chmod(0o444)
        self.approval_signature_path.chmod(0o444)
        return raw, signature

    def make_authority(self, **updates: object) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": gate.AUTHORITY_SCHEMA,
            "state": "ACTIVE",
            "release_id": RELEASE,
            "authorized_instance_id": gate.INSTANCE_ID,
            "authorized_instance_type": gate.INSTANCE_TYPE,
            "authorized_role": gate.ROLE,
            "operator_signer_identity": gate.OPERATOR_SIGNER_IDENTITY,
            "operator_signature_namespace": gate.OPERATOR_SIGNATURE_NAMESPACE,
            "operator_public_key_fingerprint": gate.OPERATOR_PUBLIC_KEY_FINGERPRINT,
            "authorized_run_id": "c1-20260722t160000z-real-fill-01",
            "authorized_arm_nonce": "N" * 32,
            "authorized_arm_not_before_utc": _utc(NOW - dt.timedelta(minutes=4)),
            "authorized_arm_expires_at_utc": _utc(NOW + dt.timedelta(hours=1)),
            "exact_git_commit": COMMIT,
            "runtime_file_sha256s": {
                name: hashlib.sha256(raw).hexdigest()
                for name, raw in self.runtime_file_raw.items()
            },
            "upstream_sha256s": {
                name: hashlib.sha256(raw).hexdigest()
                for name, raw in self.upstream_raw.items()
            },
            "spec_path": str(gate.SPEC_PATH),
            "spec_sha256": hashlib.sha256(self.spec_raw).hexdigest(),
            "config_path": str(gate.CONFIG_PATH),
            "config_sha256": hashlib.sha256(self.config_raw).hexdigest(),
            "checkpoint_root": str(gate.CHECKPOINT_ROOT),
            "checkpoint_manifest_sha256s": {
                stage: hashlib.sha256(raw).hexdigest()
                for stage, raw in self.manifest_raw.items()
            },
            "eligible_dates": list(gate.EXACT_ELIGIBLE_DATES),
            "authorized_read_roots": list(gate.READ_ROOTS),
            "authorized_write_root": str(gate.WRITE_ROOT),
            "run_directory_prefix": gate.RUN_PREFIX,
            "max_runtime_seconds": 3600,
            "cost_cap_usd": 1.0,
            "live_order_permission": False,
            "production_mutation": False,
            "s3_write_permission": False,
            "rfq_included": False,
            "issued_at_utc": _utc(NOW - dt.timedelta(minutes=10)),
            "expires_at_utc": _utc(NOW + dt.timedelta(hours=4)),
        }
        value.update(updates)
        approval_raw, signature_raw = self._sign_approval(self._approval_for(value))
        value["operator_authorization_text"] = approval_raw.decode("utf-8")
        value["operator_authorization_sha256"] = hashlib.sha256(
            approval_raw
        ).hexdigest()
        value["operator_approval_signature_sha256"] = hashlib.sha256(
            signature_raw
        ).hexdigest()
        return value

    def make_arm(
        self, authority_raw: bytes, **updates: object
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": gate.ARM_SCHEMA,
            "state": "ARMED",
            "release_id": RELEASE,
            "authority_sha256": hashlib.sha256(authority_raw).hexdigest(),
            "exact_git_commit": COMMIT,
            "run_id": "c1-20260722t160000z-real-fill-01",
            "arm_nonce": "N" * 32,
            "armed_at_utc": _utc(NOW - dt.timedelta(minutes=5)),
            "not_before_utc": _utc(NOW - dt.timedelta(minutes=4)),
            "expires_at_utc": _utc(NOW + dt.timedelta(hours=1)),
        }
        value.update(updates)
        return value

    def install(
        self,
        authority: dict[str, object] | None = None,
        *,
        arm_updates: dict[str, object] | None = None,
    ) -> None:
        self.authority = authority or self.authority
        authority_raw = _json_bytes(self.authority)
        self.arm = self.make_arm(authority_raw, **(arm_updates or {}))
        _immutable(self.authority_path, authority_raw)
        _immutable(self.arm_path, _json_bytes(self.arm))

    def kwargs(self) -> dict[str, object]:
        return {
            "authority_path": self.authority_path,
            "arm_path": self.arm_path,
            "approval_text_path": self.approval_path,
            "approval_signature_path": self.approval_signature_path,
            "spec_path": self.spec_path,
            "config_path": self.config_path,
            "runtime_commit_path": self.runtime_path,
            "runtime_file_paths": self.runtime_file_paths,
            "upstream_paths": self.upstream_paths,
            "checkpoint_manifest_paths": self.manifest_paths,
            "expected_owner_uid": self.uid,
            "checkpoint_owner_uid": self.uid,
            "ssh_keygen_path": self.ssh_keygen_path,
            "expected_ssh_keygen_sha256": self.ssh_keygen_sha256,
            "allowed_signers_line": self.allowed_signers_line,
            "verifier_owner_uid": self.ssh_keygen_path.stat().st_uid,
            "now": NOW,
        }


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    return Bundle(tmp_path)


def test_valid_exact_bundle_binds_all_five_manifests(bundle: Bundle) -> None:
    result, artifacts = gate.validate_bundle(**bundle.kwargs())
    assert result["state"] == "AUTHORIZED"
    assert result["exact_git_commit"] == COMMIT
    assert set(result["runtime_file_sha256s"]) == set(gate.RUNTIME_FILE_PATHS)
    assert set(result["upstream_sha256s"]) == set(gate.UPSTREAM_PATHS)
    assert result["eligible_dates"] == gate.EXACT_ELIGIBLE_DATES
    assert result["run_directory"].startswith("/srv/w09-research/c1-runs/c1-")
    assert result["control_directory"].endswith("/control")
    assert result["work_directory"].endswith("/work")
    assert result["output_directory"].endswith("/work/results")
    assert result["effective_runtime_seconds"] == 3600
    assert set(result["checkpoint_manifest_sha256s"]) == set(gate.CHECKPOINT_STAGES)
    assert len([name for name in artifacts if name.endswith("/MANIFEST.json")]) == 5


def test_operator_or_authority_tamper_is_rejected(bundle: Bundle) -> None:
    tampered = dict(bundle.authority)
    tampered["operator_authorization_text"] = "批准别的任务"
    bundle.install(tampered)
    with pytest.raises(gate.C1AuthorityError, match="operator text differs"):
        gate.validate_bundle(**bundle.kwargs())


def test_signature_tamper_and_valid_signature_with_wrong_scope_are_rejected(
    bundle: Bundle,
) -> None:
    bundle.approval_signature_path.chmod(0o644)
    signature = bytearray(bundle.approval_signature_path.read_bytes())
    signature[len(signature) // 2] ^= 1
    bundle.approval_signature_path.write_bytes(bytes(signature))
    bundle.approval_signature_path.chmod(0o444)
    authority = dict(bundle.authority)
    authority["operator_approval_signature_sha256"] = hashlib.sha256(
        signature
    ).hexdigest()
    bundle.install(authority)
    with pytest.raises(gate.C1AuthorityError, match="signature verification failed"):
        gate.validate_bundle(**bundle.kwargs())

    second = Bundle(bundle.root / "signed-wrong-scope")
    approval = json.loads(second.approval_path.read_bytes())
    approval["max_runtime_seconds"] = 7200
    approval_raw, signature_raw = second._sign_approval(approval)
    authority = dict(second.authority)
    authority["operator_authorization_text"] = approval_raw.decode("utf-8")
    authority["operator_authorization_sha256"] = hashlib.sha256(approval_raw).hexdigest()
    authority["operator_approval_signature_sha256"] = hashlib.sha256(
        signature_raw
    ).hexdigest()
    second.install(authority)
    with pytest.raises(gate.C1AuthorityError, match="signed operator approval mismatch"):
        gate.validate_bundle(**second.kwargs())


def test_verifier_binary_and_offline_identity_are_pinned(bundle: Bundle) -> None:
    kwargs = bundle.kwargs()
    kwargs["expected_ssh_keygen_sha256"] = "0" * 64
    with pytest.raises(gate.C1AuthorityError, match="ssh-keygen verifier SHA"):
        gate.validate_bundle(**kwargs)

    kwargs = bundle.kwargs()
    kwargs["allowed_signers_line"] = (
        "not-the-authorized-identity " + bundle.allowed_signers_line.split(" ", 1)[1]
    )
    with pytest.raises(gate.C1AuthorityError, match="signature verification failed"):
        gate.validate_bundle(**kwargs)


def test_expired_authority_is_rejected(bundle: Bundle) -> None:
    authority = bundle.make_authority(
        issued_at_utc=_utc(NOW - dt.timedelta(hours=2)),
        expires_at_utc=_utc(NOW - dt.timedelta(seconds=1)),
    )
    bundle.install(
        authority,
        arm_updates={
            "armed_at_utc": _utc(NOW - dt.timedelta(hours=1)),
            "not_before_utc": _utc(NOW - dt.timedelta(minutes=30)),
            "expires_at_utc": _utc(NOW - dt.timedelta(seconds=1)),
        },
    )
    with pytest.raises(gate.C1AuthorityError, match="authority is not active"):
        gate.validate_bundle(**bundle.kwargs())


def test_signed_time_window_cannot_be_extended_or_replayed(bundle: Bundle) -> None:
    extended = dict(bundle.authority)
    extended["expires_at_utc"] = _utc(NOW + dt.timedelta(hours=8))
    bundle.install(extended)
    with pytest.raises(
        gate.C1AuthorityError, match="signed operator approval mismatch: authority_expires"
    ):
        gate.validate_bundle(**bundle.kwargs())


@pytest.mark.parametrize(
    "field",
    ["live_order_permission", "production_mutation", "s3_write_permission", "rfq_included"],
)
def test_any_broad_permission_is_rejected(bundle: Bundle, field: str) -> None:
    authority = bundle.make_authority(**{field: True})
    bundle.install(authority)
    with pytest.raises(gate.C1AuthorityError, match="explicit false"):
        gate.validate_bundle(**bundle.kwargs())


def test_extra_read_or_write_scope_is_rejected(bundle: Bundle) -> None:
    authority = bundle.make_authority(
        authorized_read_roots=[*gate.READ_ROOTS, "/home/ubuntu"],
        authorized_write_root="/srv/w09-research",
    )
    bundle.install(authority)
    with pytest.raises(gate.C1AuthorityError, match="field mismatch"):
        gate.validate_bundle(**bundle.kwargs())


@pytest.mark.parametrize("kind", ["spec", "config", "checkpoint", "runtime"])
def test_wrong_bound_sha_or_commit_is_rejected(bundle: Bundle, kind: str) -> None:
    if kind == "spec":
        authority = bundle.make_authority(spec_sha256="0" * 64)
        bundle.install(authority)
        expected = "spec_sha256"
    elif kind == "config":
        authority = bundle.make_authority(config_sha256="0" * 64)
        bundle.install(authority)
        expected = "config_sha256"
    elif kind == "checkpoint":
        hashes = dict(bundle.authority["checkpoint_manifest_sha256s"])
        hashes[gate.CHECKPOINT_STAGES[0]] = "0" * 64
        authority = bundle.make_authority(checkpoint_manifest_sha256s=hashes)
        bundle.install(authority)
        expected = "checkpoint_manifest_sha256s"
    else:
        _immutable(bundle.runtime_path, ("b" * 40 + "\n").encode("ascii"))
        expected = "installed runtime"
    with pytest.raises(gate.C1AuthorityError, match=expected):
        gate.validate_bundle(**bundle.kwargs())


def test_runtime_file_one_byte_and_keyset_tamper_fail_closed(bundle: Bundle) -> None:
    path = bundle.runtime_file_paths["c1_fill_kernel.py"]
    path.chmod(0o644)
    raw = bytearray(path.read_bytes())
    raw[-2] ^= 1
    _immutable(path, bytes(raw))
    with pytest.raises(gate.C1AuthorityError, match="runtime_file_sha256s.c1_fill_kernel"):
        gate.validate_bundle(**bundle.kwargs())

    bundle = Bundle(path.parents[2] / "second")
    missing = dict(bundle.authority["runtime_file_sha256s"])
    missing.pop("c1_checkpoint_reader.py")
    bundle.install(bundle.make_authority(runtime_file_sha256s=missing))
    with pytest.raises(gate.C1AuthorityError, match="missing or extra"):
        gate.validate_bundle(**bundle.kwargs())

    extra = dict(bundle.make_authority()["runtime_file_sha256s"])
    extra["unreviewed.py"] = "0" * 64
    bundle.install(bundle.make_authority(runtime_file_sha256s=extra))
    with pytest.raises(gate.C1AuthorityError, match="missing or extra"):
        gate.validate_bundle(**bundle.kwargs())


@pytest.mark.parametrize("mode, message", [(0o404, "not readable"), (0o464, "writable")])
def test_checkpoint_manifest_group_access_is_fail_closed(
    bundle: Bundle, mode: int, message: str
) -> None:
    path = bundle.manifest_paths[gate.CHECKPOINT_STAGES[0]]
    path.chmod(mode)
    with pytest.raises(gate.C1AuthorityError, match=message):
        gate.validate_bundle(**bundle.kwargs())


def test_fixed_upstream_bytes_keys_and_nofollow_are_fail_closed(bundle: Bundle) -> None:
    path = bundle.upstream_paths["input_manifest"]
    path.chmod(0o640)
    raw = bytearray(path.read_bytes())
    raw[-2] ^= 1
    path.write_bytes(bytes(raw))
    path.chmod(0o640)
    with pytest.raises(gate.C1AuthorityError, match="upstream_sha256s.input_manifest"):
        gate.validate_bundle(**bundle.kwargs())

    second = Bundle(bundle.root / "upstream-second")
    missing = dict(second.authority["upstream_sha256s"])
    missing.pop("input_manifest")
    second.install(second.make_authority(upstream_sha256s=missing))
    with pytest.raises(gate.C1AuthorityError, match="missing or extra"):
        gate.validate_bundle(**second.kwargs())

    third = Bundle(bundle.root / "upstream-third")
    target = third.upstream_paths["fullscope_l2_execution_receipt"]
    real = third.root / "real-upstream.json"
    real.write_bytes(target.read_bytes())
    real.chmod(0o640)
    target.unlink()
    target.symlink_to(real)
    with pytest.raises(gate.C1AuthorityError, match="missing or unsafe"):
        gate.validate_bundle(**third.kwargs())


def test_wrong_mode_owner_and_symlink_are_rejected(bundle: Bundle, tmp_path: Path) -> None:
    bundle.arm_path.chmod(0o644)
    with pytest.raises(gate.C1AuthorityError, match="mode is not exactly 0444"):
        gate.validate_bundle(**bundle.kwargs())
    bundle.arm_path.chmod(0o444)

    wrong_owner = bundle.kwargs()
    wrong_owner["expected_owner_uid"] = bundle.uid + 1
    with pytest.raises(gate.C1AuthorityError, match="wrong owner"):
        gate.validate_bundle(**wrong_owner)

    target = tmp_path / "arm-target.json"
    _immutable(target, bundle.arm_path.read_bytes())
    bundle.arm_path.unlink()
    bundle.arm_path.symlink_to(target)
    with pytest.raises(gate.C1AuthorityError, match="missing or unsafe"):
        gate.validate_bundle(**bundle.kwargs())


def test_external_approval_files_require_root_style_0444_nofollow(
    bundle: Bundle, tmp_path: Path
) -> None:
    bundle.approval_path.chmod(0o644)
    with pytest.raises(gate.C1AuthorityError, match="mode is not exactly 0444"):
        gate.validate_bundle(**bundle.kwargs())
    bundle.approval_path.chmod(0o444)

    target = tmp_path / "approval-signature-target"
    _immutable(target, bundle.approval_signature_path.read_bytes())
    bundle.approval_signature_path.unlink()
    bundle.approval_signature_path.symlink_to(target)
    with pytest.raises(gate.C1AuthorityError, match="missing or unsafe"):
        gate.validate_bundle(**bundle.kwargs())


def test_arm_must_bind_sha_nonce_and_strictly_narrower_window(bundle: Bundle) -> None:
    bundle.install(bundle.authority, arm_updates={"authority_sha256": "0" * 64})
    with pytest.raises(gate.C1AuthorityError, match="authority_sha256"):
        gate.validate_bundle(**bundle.kwargs())

    bundle.install(bundle.authority, arm_updates={"arm_nonce": "short"})
    with pytest.raises(gate.C1AuthorityError, match="nonce"):
        gate.validate_bundle(**bundle.kwargs())

    bundle.install(bundle.authority, arm_updates={"arm_nonce": "X" * 32})
    with pytest.raises(gate.C1AuthorityError, match="signed operator approval"):
        gate.validate_bundle(**bundle.kwargs())

    wide_authority = bundle.make_authority(
        authorized_arm_not_before_utc=bundle.authority["issued_at_utc"],
        authorized_arm_expires_at_utc=bundle.authority["expires_at_utc"],
    )
    bundle.install(
        wide_authority,
        arm_updates={
            "armed_at_utc": wide_authority["issued_at_utc"],
            "not_before_utc": wide_authority["issued_at_utc"],
            "expires_at_utc": wide_authority["expires_at_utc"],
        },
    )
    with pytest.raises(gate.C1AuthorityError, match="strictly narrower"):
        gate.validate_bundle(**bundle.kwargs())


def test_o_excl_consumption_is_one_shot_even_concurrently(bundle: Bundle) -> None:
    kwargs = bundle.kwargs()
    kwargs["receipt_root"] = bundle.receipt_root

    def attempt() -> str:
        try:
            gate.consume_one_shot(**kwargs)
        except gate.C1AuthorityError as exc:
            return str(exc)
        return "PASS"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _n: attempt(), range(2)))
    assert outcomes.count("PASS") == 1
    assert sum("already consumed" in outcome for outcome in outcomes) == 1
    receipts = list(bundle.receipt_root.glob("*.json"))
    assert len(receipts) == 1
    assert receipts[0].stat().st_mode & 0o777 == 0o444
    receipt = json.loads(receipts[0].read_bytes())
    assert receipt["state"] == "CONSUMED_BEFORE_COMPUTE"
    assert receipt["arm_nonce"] == "N" * 32
    assert receipt["operator_authorization_sha256"] == bundle.authority[
        "operator_authorization_sha256"
    ]


def test_signed_scope_cannot_be_replayed_by_reserializing_arm(
    bundle: Bundle,
) -> None:
    kwargs = bundle.kwargs()
    gate.consume_one_shot(**kwargs, receipt_root=bundle.receipt_root)

    # Preserve every externally signed semantic value while changing literal
    # AUTHORITY/ARM bytes and therefore both raw hashes.
    authority_raw = (
        json.dumps(bundle.authority, sort_keys=True, indent=4).encode("utf-8") + b"\n"
    )
    arm = dict(bundle.arm)
    arm["authority_sha256"] = hashlib.sha256(authority_raw).hexdigest()
    bundle.authority_path.chmod(0o644)
    bundle.authority_path.write_bytes(authority_raw)
    bundle.authority_path.chmod(0o444)
    bundle.arm_path.chmod(0o644)
    bundle.arm_path.write_bytes(
        json.dumps(arm, sort_keys=True, indent=4).encode("utf-8") + b"\n"
    )
    bundle.arm_path.chmod(0o444)

    with pytest.raises(gate.C1AuthorityError, match="signed operator approval"):
        gate.consume_one_shot(**kwargs, receipt_root=bundle.receipt_root)
    assert len(list(bundle.receipt_root.glob("*.json"))) == 1


def test_control_receipts_are_root_controlled_canonical_and_fully_bound(
    bundle: Bundle,
) -> None:
    control_root = bundle.root / "control-run"
    kwargs = bundle.kwargs()
    consumed = gate.consume_one_shot(
        **kwargs,
        receipt_root=bundle.receipt_root,
    )
    control_root.mkdir(mode=0o750)
    result = gate.record_control_receipts(
        **kwargs,
        receipt_root=bundle.receipt_root,
        control_receipt_root=control_root,
    )
    authorization = control_root / "CONTROL_AUTHORIZATION_RECEIPT.json"
    consumption = control_root / "ARM_CONSUMPTION_RECEIPT.json"
    for path in (authorization, consumption):
        assert path.stat().st_uid == bundle.uid
        assert path.stat().st_mode & 0o777 == 0o444
        raw = path.read_bytes()
        assert raw.endswith(b"\n")
        assert raw == gate._canonical_json(json.loads(raw))
    auth_value = json.loads(authorization.read_bytes())
    consume_value = json.loads(consumption.read_bytes())
    for field in (
        "authority_sha256",
        "arm_sha256",
        "exact_git_commit",
        "spec_sha256",
        "config_sha256",
        "checkpoint_manifest_sha256s",
        "runtime_file_sha256s",
        "upstream_sha256s",
        "operator_authorization_text",
        "operator_authorization_sha256",
        "operator_approval_signature_sha256",
        "operator_public_key_fingerprint",
        "signed_operator_approval",
        "run_id",
    ):
        assert auth_value[field] == result[field]
        assert consume_value[field] == result[field]
    assert consume_value["consumption_receipt"].endswith(
        result["operator_authorization_sha256"] + ".json"
    )
    assert consumed["arm_sha256"] == result["arm_sha256"]


def test_record_uses_consumed_deadline_across_second_boundary(bundle: Bundle) -> None:
    consume_kwargs = bundle.kwargs()
    consumed = gate.consume_one_shot(
        **consume_kwargs,
        receipt_root=bundle.receipt_root,
    )
    control_root = bundle.root / "cross-second-control"
    control_root.mkdir(mode=0o750)
    record_kwargs = bundle.kwargs()
    record_kwargs["now"] = NOW + dt.timedelta(seconds=2)
    recorded = gate.record_control_receipts(
        **record_kwargs,
        receipt_root=bundle.receipt_root,
        control_receipt_root=control_root,
    )
    assert consumed["effective_runtime_seconds"] == 3600
    assert recorded["consumed_effective_runtime_seconds"] == 3600
    assert recorded["effective_runtime_seconds"] == 3598
    assert recorded["execution_deadline_utc"] == _utc(NOW + dt.timedelta(hours=1))


def test_wrapper_is_fixed_manual_no_sudo_and_consumes_before_compute() -> None:
    wrapper = (ROOT / "deploy" / "w09" / "c1_run_once.sh").read_text()
    gate_source = SOURCE.read_text()
    assert "sudo" not in wrapper
    assert "c1_real_fill_runner.py" in wrapper
    assert '--checkpoint-root "$1"' in wrapper
    assert '--output-dir "$3"' in wrapper
    assert '--config "$4"' in wrapper
    assert 'control_dir="$run_dir/control"' in wrapper
    assert 'work_dir="$run_dir/work"' in wrapper
    assert 'output_dir="$work_dir/results"' in wrapper
    assert "WRITE_ROOT=/srv/w09-research/c1-runs" in wrapper
    assert "--reuid=nobody" in wrapper
    assert "--regid=nogroup" in wrapper
    assert "--groups=1000" in wrapper
    assert "/usr/bin/env -i" in wrapper
    assert "/usr/bin/unshare --mount --net" in wrapper
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in wrapper
    assert "unset PYTHONPATH PYTHONHOME" in wrapper
    assert '"$PYTHON" -I "$GATE" consume' in wrapper
    assert 'v["execution_deadline_utc"]' in wrapper
    assert 'datetime.datetime.now(datetime.timezone.utc)' in wrapper
    assert '"${remaining_seconds}s"' in wrapper
    assert wrapper.index('remaining_seconds="$(') < wrapper.index(
        "exec /usr/bin/setpriv"
    )
    assert '/usr/bin/readlink -f -- "$PYTHON"' in wrapper
    assert "/usr/bin/sha256sum -- /usr/bin/python3.12" in wrapper
    assert 'sys.version.split()[0]=="3.12.3"' in wrapper
    assert 'duckdb.__version__=="1.4.5"' in wrapper
    assert "/usr/bin/mount --make-rprivate /" in wrapper
    assert "/usr/bin/mount -o remount,bind,ro /srv/w09-research" in wrapper
    assert "/usr/bin/mount -t tmpfs -o mode=0700,size=1m tmpfs /mnt" in wrapper
    assert "/usr/bin/mount --bind /mnt/c1-work-alias \"$2\"" in wrapper
    assert '/usr/bin/mount --bind "$2/tmp" /tmp' in wrapper
    assert '/usr/bin/mount --bind "$2/var-tmp" /var/tmp' in wrapper
    assert '/usr/bin/mount --bind "$2/dev-shm" /dev/shm' in wrapper
    assert "-perm /0022" in wrapper
    assert "i-0e53d134dceffe166" in wrapper
    assert "r8g.2xlarge" in wrapper
    assert 'imds_role" = "w09-research-runner"' in wrapper
    assert "/latest/meta-data/iam/security-credentials/" in wrapper
    assert wrapper.index('"$GATE" consume') < wrapper.index(
        '/usr/bin/install -d -m 0750 -o root -g root "$run_dir"'
    )
    assert wrapper.index('"$GATE" record') < wrapper.index("exec /usr/bin/systemd-inhibit")
    assert '/usr/bin/install -d -m 0750 -o root -g root "$control_dir"' in wrapper
    assert '/usr/bin/install -d -m 0750 -o nobody -g nogroup "$work_dir"' in wrapper
    assert '/usr/bin/chmod 0755 "$run_dir"' in wrapper
    assert "/usr/bin/chown nobody:nogroup \"$run_dir\"" not in wrapper
    assert "runner output directory must not exist" in wrapper
    assert "CONTROL_AUTHORIZATION_RECEIPT.json" in gate_source
    assert "ARM_CONSUMPTION_RECEIPT.json" in gate_source
    assert "AUTHORITY.json" not in wrapper
    assert "systemctl start" not in wrapper
