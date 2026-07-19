"""Independent overlay state must never poison a completed base release."""

import contextlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import fresh_rfq_overlay_release as overlay  # noqa: E402
import test_fresh_rfq_receipts as fresh_fixture  # noqa: E402


DATE = "2026-07-19"


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(overlay.canonical_bytes(value) + b"\n")
    return path


def _inputs(tmp_path):
    base = {
        "schema_version": "research-v3-daily-status-v2",
        "state": "V3_REFERENCE_PUBLISHED", "date": DATE, "rfq": "OFF",
        "manifest_commit_state": "REFERENCE_MANIFEST_COMMITTED",
        "manifest_object": {
            "bucket": "kalshi-vault-ritcardo",
            "key": "research/releases/base/MANIFEST.json",
            "VersionId": "base-version", "size": 10, "sha256": "a" * 64,
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        },
    }
    base_path = _write(tmp_path / "BASE-STATUS.json", base)
    receipt = {
        "schema_version": overlay.SCHEMA, "state": overlay.STATE,
        "date": DATE, "overlay_receipt_sha256": "b" * 64,
    }
    receipt_path = _write(tmp_path / "OVERLAY-RECEIPT.json", receipt)
    return base_path, receipt_path


def test_tagger_failure_turns_only_overlay_off_and_base_stays_successful(tmp_path):
    base, receipt = _inputs(tmp_path)
    before = base.read_bytes()

    def fail(_receipt):
        raise RuntimeError("simulated exact-version PutObjectTagging denial")

    result = overlay.attempt_overlay_tagging(
        base_terminal_path=base, overlay_receipt_path=receipt,
        tagger=fail, status_path=tmp_path / "overlay" / "STATUS.json")
    assert result["state"] == "RFQ_OVERLAY_OFF"
    assert result["reason_code"] == "TAGGER_FAILED:RuntimeError"
    assert result["base_state"] == "V3_REFERENCE_PUBLISHED"
    assert result["base_mutations"] == 0
    assert base.read_bytes() == before
    assert result["base_terminal_file_sha256"] == hashlib.sha256(before).hexdigest()


def test_later_overlay_tag_pass_does_not_rebuild_or_change_base(tmp_path):
    base, receipt = _inputs(tmp_path)
    before = base.read_bytes()
    result = overlay.attempt_overlay_tagging(
        base_terminal_path=base, overlay_receipt_path=receipt,
        tagger=lambda _receipt: {
            "state": "EXACT_VERSION_TAG_READBACK_VERIFIED",
            "rfq": "FRESH_SEALED", "tag_puts": 24,
        }, status_path=tmp_path / "PASS.json")
    assert result["state"] == "RFQ_OVERLAY_TAGGED_READY"
    assert result["base_state"] == "V3_REFERENCE_PUBLISHED"
    assert base.read_bytes() == before


def test_mapping_cache_body_paths_are_opened_one_at_a_time(tmp_path):
    body = b'{"channel":"rfq"}\n'
    path = tmp_path / "exact.ndjson"
    path.write_bytes(body)
    row = {
        "bucket": "kalshi-vault-ritcardo", "key": "ec2/raw/date=x/rfq",
        "version_id": "v1", "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(), "body_path": str(path),
    }
    inputs = {
        "exact_analysis_rfq_objects": [row],
        "orderbooks_l1_objects": [row],
        "orderbooks_full_objects": [row],
        "pre_event_window_ms": 0, "post_event_window_ms": 0,
    }
    reader = overlay._body_path_reader(inputs)
    assert not any(isinstance(value, bytes)
                   for value in vars(reader).values())
    with reader.open_exact(row) as opened:
        assert opened.path == path.absolute()
        assert opened.path.read_bytes() == body


def test_overlay_cache_binds_existing_base_without_mutating_it(
        tmp_path, monkeypatch):
    (authority, receipts, _manifest, manifest_raw, manifest_identity,
     evidence) = fresh_fixture.overlay_inputs(monkeypatch)
    monkeypatch.setattr(
        overlay.fresh, "OLD_284_OBJECT_SET_SHA256",
        authority["old_284_object_set_sha256"])
    date = evidence["analysis_date"]
    package = tmp_path / ("date=" + date)
    package.mkdir()
    eligible_path = _write(package / overlay.gate.ELIGIBLE_NAME, {})
    _write(package / overlay.gate.AUTHORITY_NAME, {})
    _write(package / overlay.gate.SOURCE_NAME, evidence)
    _write(package / overlay.gate.HOURS_NAME, {
        "schema": overlay.gate.HOUR_SET_SCHEMA,
        "date": date, "receipts": receipts,
    })
    analysis = sorted([{
        "bucket": overlay.fresh.SOURCE_BUCKET,
        "key": row["key"], "version_id": row["version_id"],
        "size": row["size"], "sha256": row["sha256"],
    } for receipt in receipts if receipt["segment_hour"][:10] == date
        for row in receipt["rfq_objects"]], key=lambda row: row["key"])
    marker = {
        "eligibility_sha256": "e" * 64,
        "authority_sha256": authority["authority_sha256"],
        "source_evidence_sha256": evidence["evidence_sha256"],
        "eligible_objects": analysis,
    }
    monkeypatch.setattr(
        overlay.gate, "load_package",
        lambda path, *, expected_date: marker)
    monkeypatch.setattr(
        overlay.precommit, "validate_precommit_envelope",
        lambda _value: {"authority": authority})

    base_manifest = tmp_path / "BASE-MANIFEST.json"
    base_manifest.write_bytes(manifest_raw)
    identity_path = _write(tmp_path / "BASE-IDENTITY.json", manifest_identity)
    terminal = {
        "schema_version": "research-v3-daily-status-v2",
        "state": "V3_REFERENCE_PUBLISHED", "date": date, "rfq": "OFF",
        "manifest_commit_state": "REFERENCE_MANIFEST_COMMITTED",
        "manifest_object": {
            "bucket": manifest_identity["bucket"],
            "key": manifest_identity["key"],
            "VersionId": manifest_identity["version_id"],
            "size": manifest_identity["size"],
            "sha256": manifest_identity["sha256"],
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        },
    }
    terminal_path = _write(tmp_path / "BASE-STATUS.json", terminal)
    base_before = terminal_path.read_bytes()

    mapping = fresh_fixture.mapping_provenance_inputs(receipts)
    for family in ("exact_analysis_rfq_objects", "orderbooks_l1_objects",
                   "orderbooks_full_objects"):
        for index, row in enumerate(mapping[family]):
            body = row.pop("body")
            body_path = tmp_path / f"{family}-{index}.bin"
            body_path.write_bytes(body)
            row["body_path"] = str(body_path)
    mapping_path = _write(tmp_path / "MAPPING.json", mapping)
    ready = overlay.create_overlay_cache(
        date=date, eligibility_path=eligible_path,
        base_terminal_path=terminal_path,
        base_manifest_path=base_manifest,
        base_manifest_identity_path=identity_path,
        mapping_inputs_path=mapping_path,
        output_root=tmp_path / "overlays")
    assert ready.name == overlay.READY_NAME
    assert (ready.parent / overlay.MANIFEST_NAME).exists()
    receipt = json.loads((ready.parent / overlay.RECEIPT_NAME).read_text())
    assert receipt["state"] == overlay.STATE
    assert receipt["base_state"] == "V3_REFERENCE_PUBLISHED"
    assert receipt["base_mutations"] == 0
    assert terminal_path.read_bytes() == base_before


def test_streaming_overlay_checkpoints_completed_heavy_stage(
        tmp_path, monkeypatch):
    (authority, receipts, _manifest, manifest_raw, manifest_identity,
     evidence) = fresh_fixture.overlay_inputs(monkeypatch)
    monkeypatch.setattr(
        overlay.fresh, "OLD_284_OBJECT_SET_SHA256",
        authority["old_284_object_set_sha256"])
    date = evidence["analysis_date"]
    package = tmp_path / ("date=" + date)
    package.mkdir()
    eligible_path = _write(package / overlay.gate.ELIGIBLE_NAME, {})
    _write(package / overlay.gate.AUTHORITY_NAME, {})
    _write(package / overlay.gate.SOURCE_NAME, evidence)
    _write(package / overlay.gate.HOURS_NAME, {
        "schema": overlay.gate.HOUR_SET_SCHEMA,
        "date": date, "receipts": receipts,
    })
    analysis = sorted([{
        "bucket": overlay.fresh.SOURCE_BUCKET,
        "key": row["key"], "version_id": row["version_id"],
        "size": row["size"], "sha256": row["sha256"],
    } for receipt in receipts if receipt["segment_hour"][:10] == date
        for row in receipt["rfq_objects"]], key=lambda row: row["key"])
    marker = {
        "eligibility_sha256": "e" * 64,
        "authority_sha256": authority["authority_sha256"],
        "source_evidence_sha256": evidence["evidence_sha256"],
        "eligible_objects": analysis,
    }
    monkeypatch.setattr(
        overlay.gate, "load_package",
        lambda path, *, expected_date: marker)
    monkeypatch.setattr(
        overlay.precommit, "validate_precommit_envelope",
        lambda _value: {"authority": authority})
    manifest_path = tmp_path / "BASE-MANIFEST.json"
    manifest_path.write_bytes(manifest_raw)
    terminal = {
        "schema_version": "research-v3-daily-status-v2",
        "state": "V3_REFERENCE_PUBLISHED", "date": date, "rfq": "OFF",
        "manifest_commit_state": "REFERENCE_MANIFEST_COMMITTED",
        "manifest_object": {
            "bucket": manifest_identity["bucket"],
            "key": manifest_identity["key"],
            "VersionId": manifest_identity["version_id"],
            "size": manifest_identity["size"],
            "sha256": manifest_identity["sha256"],
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        },
    }
    terminal_path = _write(tmp_path / "BASE-STATUS.json", terminal)
    mapping = fresh_fixture.mapping_provenance_inputs(receipts)
    body_by_exact = {}
    for family in ("exact_analysis_rfq_objects", "orderbooks_l1_objects",
                   "orderbooks_full_objects"):
        for row in mapping[family]:
            exact = (row["bucket"], row["key"], row["version_id"])
            body_by_exact[exact] = row["body"]
    opens = []

    @contextlib.contextmanager
    def open_exact(identity):
        exact = (identity["bucket"], identity["key"], identity["version_id"])
        opens.append(exact)
        path = tmp_path / ("reader-" + hashlib.sha256(
            repr(exact).encode()).hexdigest())
        path.write_bytes(body_by_exact[exact])
        yield SimpleNamespace(path=path)

    original_universe = (
        overlay.universe_provenance.build_universe_provenance_from_reader)
    monkeypatch.setattr(
        overlay.universe_provenance,
        "build_universe_provenance_from_reader",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("later stage")))
    kwargs = dict(
        date=date, eligibility_path=eligible_path,
        base_terminal_path=terminal_path,
        base_manifest_path=manifest_path,
        base_manifest_exact_identity=manifest_identity,
        open_exact=open_exact, checkpoint_root=tmp_path / "checkpoints",
        output_root=tmp_path / "streaming-overlays")
    try:
        overlay.create_overlay_cache_from_reader(**kwargs)
    except RuntimeError as exc:
        assert str(exc) == "later stage"
    else:
        raise AssertionError("later-stage failure was swallowed")
    rfq_open_count = len(analysis)
    assert len(opens) == rfq_open_count
    monkeypatch.setattr(
        overlay.universe_provenance,
        "build_universe_provenance_from_reader", original_universe)
    ready = overlay.create_overlay_cache_from_reader(**kwargs)
    assert ready.name == overlay.READY_NAME
    assert len(opens) > rfq_open_count
    assert opens[:rfq_open_count] == [
        (row["bucket"], row["key"], row["version_id"])
        for row in analysis]
    assert not any(exact in set(opens[rfq_open_count:])
                   for exact in opens[:rfq_open_count])
