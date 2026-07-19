#!/usr/bin/env python3
"""Cohort-separation and D07 anchor-provenance wiring contracts.

These tests close the MANDATORY deployment blocker I4b recorded in
docs/plan_audits/AUDIT_DEEP03_RFQ_QUALITY_D07_ATTESTATION_2026-07-19.md:
the runner derives the D07 external anchor EXCLUSIVELY from the pinned
root-installed 0444 receipt path, with fail-closed typed provenance
checks, and offers no code path that accepts a caller-constructed anchor
object.  They also prove the fresh-RFQ cohort stays separate from the
historical base cohort and that ineligibility yields an explicit
WAITING/BLOCKED state, never a fabricated empty result.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))
sys.path.insert(0, str(ROOT / "tools"))

import deep03_fullscope_runner as runner  # noqa: E402
import deep03_v3_rfq_bounded as bounded  # noqa: E402
from deep03_v3_common import Deep03InputError  # noqa: E402

fresh = bounded.fresh_receipts


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _anchor_payload() -> dict:
    value = {
        "schema": bounded.D07_EXTERNAL_ANCHOR_SCHEMA,
        "state": "INDEPENDENT_AUDIT_PASS",
        "audit_authority": (
            "docs/plan_audits/AUDIT_DEEP03_RFQ_D07_RUNTIME_PASS "
            "(root-installed 0444 runtime AUTHORITY)"
        ),
        "producer_receipt_sha256s": [_sha("producer")],
        "reader_attestation_sha256s": [_sha("attestation")],
        "partition_receipt_set_sha256s": [_sha("partitions")],
    }
    value["anchor_sha256"] = bounded.canonical_sha256(value)
    return value


def _install_anchor(
    tmp_path: Path,
    monkeypatch,
    *,
    payload: dict | None = None,
    raw: bytes | None = None,
    mode: int = 0o444,
    name: str = "d07-external-anchor.json",
) -> Path:
    path = tmp_path / name
    if raw is None:
        raw = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(raw)
    os.chmod(path, mode)
    monkeypatch.setattr(runner, "D07_EXTERNAL_ANCHOR_INSTALL_PATH", path)
    return path


def _deny_rows() -> list[dict]:
    return [
        {
            "key": (
                f"raw_rfq/date=2026-06-{1 + index // 24:02d}/"
                f"rfq_{index % 24:02d}.ndjson.{1000 + index}"
            ),
            "size": index + 1,
            "sha256": _sha(f"old-{index}"),
        }
        for index in range(284)
    ]


def _fresh_authority(
    monkeypatch,
    *,
    generation: str = runner.FRESH_RFQ_GENERATION,
    t0: str = runner.FRESH_RFQ_STRICT_T0_UTC,
) -> dict:
    rows = _deny_rows()
    monkeypatch.setattr(
        fresh, "OLD_284_OBJECT_SET_SHA256", fresh.old_object_set_sha256(rows)
    )
    return fresh.build_fresh_epoch_authority(
        operator_authorization_sha256=fresh.OPERATOR_AUTHORIZATION_SHA256,
        strict_t0_utc=t0,
        created_at_utc="2026-07-19T14:58:27Z",
        deployment_commit="a" * 40,
        generation=generation,
        deny_identities=rows,
    )


def _authority_file(tmp_path: Path, authority: dict) -> Path:
    path = tmp_path / "fresh-authority.json"
    path.write_text(json.dumps(authority, sort_keys=True), encoding="utf-8")
    return path


def _forbidden_client_factory(_authority):
    raise AssertionError("client_factory must not run before eligibility")


# ---------------------------------------------------------------------------
# Anchor loader: pinned path + fail-closed provenance (blocker I4b)
# ---------------------------------------------------------------------------


def test_anchor_contract_is_root_pinned_and_object_free():
    loader = inspect.signature(runner.load_d07_external_anchor)
    assert loader.parameters["expected_owner_uid"].default == 0
    assert runner.D07_EXTERNAL_ANCHOR_INSTALL_PATH == Path(
        "/etc/w09/deep03/d07-external-anchor.json"
    )
    overlay = inspect.signature(runner.run_fresh_rfq_overlay)
    assert "d07_external_anchor" not in overlay.parameters
    assert (
        overlay.parameters["anchor_path"].default
        == runner.D07_EXTERNAL_ANCHOR_INSTALL_PATH
    )
    assert overlay.parameters["anchor_owner_uid"].default == 0
    anchor_like = [
        name
        for name in overlay.parameters
        if "anchor" in name and name not in {"anchor_path", "anchor_owner_uid"}
    ]
    assert anchor_like == []


def test_anchor_loader_refuses_unpinned_path(tmp_path):
    stray = tmp_path / "anchor.json"
    stray.write_text(json.dumps(_anchor_payload()))
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(stray)
    assert err.value.code == "D07_ANCHOR_PATH"


def test_anchor_loader_accepts_valid_pinned_receipt(tmp_path, monkeypatch):
    payload = _anchor_payload()
    path = _install_anchor(tmp_path, monkeypatch, payload=payload)
    loaded = runner.load_d07_external_anchor(
        path, expected_owner_uid=os.stat(path).st_uid
    )
    assert loaded == payload


def test_anchor_loader_refuses_missing_receipt(tmp_path, monkeypatch):
    path = tmp_path / "d07-external-anchor.json"
    monkeypatch.setattr(runner, "D07_EXTERNAL_ANCHOR_INSTALL_PATH", path)
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(path, expected_owner_uid=os.getuid())
    assert err.value.code == "D07_ANCHOR_NOT_INSTALLED"


def test_anchor_loader_refuses_symlink(tmp_path, monkeypatch):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(_anchor_payload()))
    os.chmod(real, 0o444)
    link = tmp_path / "d07-external-anchor.json"
    link.symlink_to(real)
    monkeypatch.setattr(runner, "D07_EXTERNAL_ANCHOR_INSTALL_PATH", link)
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(link, expected_owner_uid=os.getuid())
    assert err.value.code == "D07_ANCHOR_PROVENANCE"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o440, 0o666])
def test_anchor_loader_requires_mode_exactly_0444(tmp_path, monkeypatch, mode):
    path = _install_anchor(
        tmp_path, monkeypatch, payload=_anchor_payload(), mode=mode
    )
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(path, expected_owner_uid=os.stat(path).st_uid)
    assert err.value.code == "D07_ANCHOR_MODE"


def test_anchor_loader_requires_root_owner_by_default(tmp_path, monkeypatch):
    path = _install_anchor(tmp_path, monkeypatch, payload=_anchor_payload())
    if os.stat(path).st_uid == 0:
        pytest.skip("test environment writes root-owned files")
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(path)
    assert err.value.code == "D07_ANCHOR_OWNER"


def test_anchor_loader_refuses_wrong_owner_via_fstat(tmp_path, monkeypatch):
    path = _install_anchor(tmp_path, monkeypatch, payload=_anchor_payload())
    real_fstat = os.fstat

    def fake_fstat(fd):
        metadata = real_fstat(fd)
        return os.stat_result(
            (
                metadata.st_mode,
                metadata.st_ino,
                metadata.st_dev,
                metadata.st_nlink,
                metadata.st_uid + 1,
                metadata.st_gid,
                metadata.st_size,
                metadata.st_atime,
                metadata.st_mtime,
                metadata.st_ctime,
            )
        )

    monkeypatch.setattr(runner.os, "fstat", fake_fstat)
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(
            path, expected_owner_uid=os.stat(path).st_uid
        )
    assert err.value.code == "D07_ANCHOR_OWNER"


def test_anchor_loader_refuses_directory(tmp_path, monkeypatch):
    path = tmp_path / "d07-external-anchor.json"
    path.mkdir()
    monkeypatch.setattr(runner, "D07_EXTERNAL_ANCHOR_INSTALL_PATH", path)
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(path, expected_owner_uid=os.getuid())
    assert err.value.code == "D07_ANCHOR_PROVENANCE"


def test_anchor_loader_refuses_empty_and_oversize(tmp_path, monkeypatch):
    path = _install_anchor(tmp_path, monkeypatch, raw=b"")
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(path, expected_owner_uid=os.stat(path).st_uid)
    assert err.value.code == "D07_ANCHOR_PROVENANCE"
    monkeypatch.setattr(runner, "D07_EXTERNAL_ANCHOR_MAX_BYTES", 8)
    big = _install_anchor(
        tmp_path, monkeypatch, payload=_anchor_payload(), name="big.json"
    )
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(big, expected_owner_uid=os.stat(big).st_uid)
    assert err.value.code == "D07_ANCHOR_PROVENANCE"


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b"[]", b"3", b'"anchor"'],
)
def test_anchor_loader_refuses_non_object_json(tmp_path, monkeypatch, raw):
    path = _install_anchor(tmp_path, monkeypatch, raw=raw)
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(path, expected_owner_uid=os.stat(path).st_uid)
    assert err.value.code == "D07_ANCHOR_INVALID"


def test_anchor_loader_defers_schema_to_audited_validator(tmp_path, monkeypatch):
    self_declared = _anchor_payload()
    self_declared["state"] = "SELF_DECLARED_PASS"
    self_declared["anchor_sha256"] = bounded.canonical_sha256(
        {k: v for k, v in self_declared.items() if k != "anchor_sha256"}
    )
    path = _install_anchor(tmp_path, monkeypatch, payload=self_declared)
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(path, expected_owner_uid=os.stat(path).st_uid)
    assert err.value.code == "D07_ANCHOR_INVALID"

    tampered = _anchor_payload()
    tampered["producer_receipt_sha256s"] = [_sha("forged")]
    path2 = _install_anchor(
        tmp_path, monkeypatch, payload=tampered, name="tampered.json"
    )
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.load_d07_external_anchor(path2, expected_owner_uid=os.stat(path2).st_uid)
    assert err.value.code == "DIGEST_MISMATCH"


# ---------------------------------------------------------------------------
# Fresh RFQ overlay unit: explicit WAITING/BLOCKED, separate cohorts
# ---------------------------------------------------------------------------


def _overlay_kwargs(tmp_path: Path, authority_path: Path) -> dict:
    overlay_root = tmp_path / "overlays"
    overlay_root.mkdir(exist_ok=True)
    return {
        "overlay_root": overlay_root,
        "fresh_authority_path": authority_path,
        "client_factory": _forbidden_client_factory,
        "checkpoint_root": tmp_path / "checkpoints",
        "receipt_path": tmp_path / "FRESH_RFQ_COHORT_RECEIPT.json",
    }


def test_overlay_waits_explicitly_without_eligibility(tmp_path, monkeypatch):
    authority = _fresh_authority(monkeypatch)
    kwargs = _overlay_kwargs(tmp_path, _authority_file(tmp_path, authority))
    receipt = runner.run_fresh_rfq_overlay(**kwargs)
    assert receipt["state"] == "WAITING_FRESH_RFQ_ELIGIBILITY"
    assert receipt["result_kind"] == "EXPLICIT_NON_RESULT_STATE"
    assert receipt["fabricated_rows"] == 0
    assert receipt["eligible_overlay_count"] == 0
    assert receipt["cohort"] == runner.FRESH_RFQ_COHORT_ID
    assert receipt["base_cohort"] == runner.BASE_COHORT_ID
    assert receipt["historical_input_manifest_rfq_entries"] == 0
    assert (
        receipt["old_damaged_rfq"]
        == "DATA_INTEGRITY_BLOCKED_NO_REPAIR_NO_ANALYSIS"
    )
    on_disk = json.loads(kwargs["receipt_path"].read_text())
    assert on_disk == receipt
    with pytest.raises(Deep03InputError, match="already exists"):
        runner.run_fresh_rfq_overlay(**kwargs)


def test_overlay_blocks_on_invalid_fresh_authority(tmp_path, monkeypatch):
    path = tmp_path / "fresh-authority.json"
    path.write_text("{}")
    kwargs = _overlay_kwargs(tmp_path, path)
    receipt = runner.run_fresh_rfq_overlay(**kwargs)
    assert receipt["state"] == "BLOCKED_FRESH_AUTHORITY"
    assert receipt["result_kind"] == "EXPLICIT_NON_RESULT_STATE"
    assert receipt["fabricated_rows"] == 0


def test_overlay_blocks_on_wrong_generation_or_t0(tmp_path, monkeypatch):
    authority = _fresh_authority(
        monkeypatch, generation="rfq-capture-generation-7"
    )
    kwargs = _overlay_kwargs(tmp_path, _authority_file(tmp_path, authority))
    receipt = runner.run_fresh_rfq_overlay(**kwargs)
    assert receipt["state"] == "BLOCKED_COHORT_PIN"
    assert "rfq-capture-generation-7" in receipt["blocker"]


def _eligible_overlay(tmp_path: Path) -> Path:
    day = tmp_path / "overlays" / "date=2026-07-20"
    day.mkdir(parents=True)
    ready = day / "READY.json"
    ready.write_text("{}")
    return ready


def _stub_report() -> dict:
    return {
        "analysis_dates": ["2026-07-20"],
        "source_binding_sha256": _sha("fresh-rfq-binding"),
        "conservation": {"identity_equation_pass": True},
    }


def test_overlay_passes_only_the_pinned_anchor_to_the_module(
    tmp_path, monkeypatch
):
    payload = _anchor_payload()
    anchor_path = _install_anchor(tmp_path, monkeypatch, payload=payload)
    authority = _fresh_authority(monkeypatch)
    kwargs = _overlay_kwargs(tmp_path, _authority_file(tmp_path, authority))
    ready = _eligible_overlay(tmp_path)
    captured = {}

    def stub(**call):
        captured.update(call)
        return _stub_report()

    monkeypatch.setattr(bounded, "run_bounded_fresh_rfq", stub)
    receipt = runner.run_fresh_rfq_overlay(
        **kwargs,
        anchor_path=anchor_path,
        anchor_owner_uid=os.stat(anchor_path).st_uid,
    )
    assert captured["d07_external_anchor"] == payload
    assert captured["overlay_ready_paths"] == [str(ready)]
    assert captured["fresh_authority"] == authority
    assert receipt["state"] == "FRESH_RFQ_OVERLAY_COMPLETE"
    assert receipt["d07_external_anchor_state"] == (
        "LOADED_FROM_PINNED_ROOT_RECEIPT"
    )
    assert receipt["d07_external_anchor_sha256"] == payload["anchor_sha256"]
    assert receipt["source_binding_sha256"] == _sha("fresh-rfq-binding")
    assert receipt["conservation"] == {"identity_equation_pass": True}
    assert receipt["cohort_join"] == (
        "FORBIDDEN_SEPARATE_SOURCE_BINDINGS_AND_LEDGERS"
    )
    assert receipt["claims"]["base_cohort_claims"] == (
        "NONE_BASE_COHORT_UNTOUCHED"
    )


def test_overlay_without_installed_anchor_blocks_d07_only(
    tmp_path, monkeypatch
):
    missing = tmp_path / "d07-external-anchor.json"
    monkeypatch.setattr(runner, "D07_EXTERNAL_ANCHOR_INSTALL_PATH", missing)
    authority = _fresh_authority(monkeypatch)
    kwargs = _overlay_kwargs(tmp_path, _authority_file(tmp_path, authority))
    _eligible_overlay(tmp_path)
    captured = {}

    def stub(**call):
        captured.update(call)
        return _stub_report()

    monkeypatch.setattr(bounded, "run_bounded_fresh_rfq", stub)
    receipt = runner.run_fresh_rfq_overlay(
        **kwargs, anchor_path=missing, anchor_owner_uid=os.getuid()
    )
    assert captured["d07_external_anchor"] is None
    assert receipt["d07_external_anchor_state"] == (
        "NOT_INSTALLED_D07_BLOCKED_UNANCHORED"
    )
    assert receipt["d07_external_anchor_sha256"] is None


def test_overlay_fails_closed_on_tampered_anchor(tmp_path, monkeypatch):
    anchor_path = _install_anchor(
        tmp_path, monkeypatch, payload=_anchor_payload(), mode=0o644
    )
    authority = _fresh_authority(monkeypatch)
    kwargs = _overlay_kwargs(tmp_path, _authority_file(tmp_path, authority))
    _eligible_overlay(tmp_path)

    def stub(**_call):
        raise AssertionError("module must not run on tampered anchor")

    monkeypatch.setattr(bounded, "run_bounded_fresh_rfq", stub)
    with pytest.raises(bounded.FreshRfqResearchError) as err:
        runner.run_fresh_rfq_overlay(
            **kwargs,
            anchor_path=anchor_path,
            anchor_owner_uid=os.stat(anchor_path).st_uid,
        )
    assert err.value.code == "D07_ANCHOR_MODE"
    assert not kwargs["receipt_path"].exists()


FRESH_RFQ_PINNED_MODULES = (
    "tools/research/deep03_v3_rfq_bounded.py",
    "tools/fresh_rfq_exact_reader.py",
    "tools/fresh_rfq_base_binding.py",
    "tools/fresh_rfq_market_mapping.py",
    "tools/fresh_rfq_receipts.py",
    "tools/fresh_rfq_request_provenance.py",
    "tools/fresh_rfq_universe_provenance.py",
)


def test_rfq_overlay_modules_are_sha_pinned_and_packaged():
    manifest = ROOT / "deploy" / "w09" / "deep03_open_discovery_modules.sha256"
    rows = {}
    for line in manifest.read_text(encoding="ascii").splitlines():
        digest, relative = line.split("  ", 1)
        rows[relative] = digest
    for relative in FRESH_RFQ_PINNED_MODULES:
        assert rows[relative] == hashlib.sha256(
            (ROOT / relative).read_bytes()
        ).hexdigest(), relative
    push = (ROOT / "deploy" / "w09" / "push_and_install.sh").read_text()
    install = (ROOT / "deploy" / "w09" / "install_on_host.sh").read_text()
    for relative in FRESH_RFQ_PINNED_MODULES:
        name = relative.rsplit("/", 1)[1]
        assert name in push, name
        assert name in install, name


def test_base_runner_still_refuses_rfq_contamination():
    """Cohort separation: the historical manifest can never gain RFQ."""
    manifest = {
        "rfq_policy": "FORBIDDEN_AND_ABSENT",
        "objects": [
            {
                "kind": "facts",
                "channel": "rfq_quotes",
                "logical_key": "facts/channel=rfq/date=2026-07-20/x.parquet",
                "row_count": 1,
            }
        ],
        "fact_objects_without_row_count": 0,
        "sealed_fact_rows": 1,
    }
    with pytest.raises(Deep03InputError, match="RFQ object reached"):
        runner._require_base_without_rfq(manifest)
    with pytest.raises(Deep03InputError, match="RFQ-forbidden"):
        runner._require_base_without_rfq({"rfq_policy": "ALLOWED"})
