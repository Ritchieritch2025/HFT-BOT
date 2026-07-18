"""Offline fail-closed tests for daily fresh-RFQ research admission."""

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import fresh_rfq_daily_eligibility as gate  # noqa: E402
import test_fresh_rfq_receipts as fixture  # noqa: E402


DATE = "2026-07-17"
GENERATED = "2026-07-18T02:00:00Z"


def _write(path, value, *, ndjson=False):
    if ndjson:
        raw = b"".join(
            gate.canonical_bytes(row) + b"\n" for row in value)
    else:
        raw = gate.canonical_bytes(value) + b"\n"
    path.write_bytes(raw)
    return path


def _inputs(tmp_path, monkeypatch, *, overlap=None):
    auth = fixture.authority(monkeypatch, overlap=overlap)
    monkeypatch.setattr(
        gate.fresh, "OLD_284_OBJECT_SET_SHA256",
        auth["old_284_object_set_sha256"])
    envelope = gate.precommit.build_precommit_envelope(
        auth,
        precommitted_at=dt.datetime(
            2026, 7, 16, 23, 50, tzinfo=dt.timezone.utc),
    )
    hours = [
        (dt.datetime(2026, 7, 17, tzinfo=dt.timezone.utc)
         + dt.timedelta(hours=index)).strftime("%Y-%m-%dT%H")
        for index in range(26)
    ]
    segments = [fixture.segment(
        auth, hour,
        subscription_acks=1 if index == 0 else 0,
        subscription_end=True if index == 0 else None,
    ) for index, hour in enumerate(hours)]
    receipts, evidence = fixture.receipts_for_segments(auth, segments)
    hour_set = {
        "schema": gate.HOUR_SET_SCHEMA,
        "date": DATE,
        "receipts": receipts,
    }
    paths = {
        "authority": _write(tmp_path / "authority.json", envelope),
        "source": _write(tmp_path / "source.json", evidence),
        "hours": _write(tmp_path / "hours.json", hour_set),
        "ledger": _write(tmp_path / "ledger.ndjson", segments, ndjson=True),
        "output": tmp_path / "eligible",
    }
    return auth, envelope, segments, evidence, hour_set, paths


def _create(paths, **kwargs):
    return gate.create_package(
        date=DATE,
        authority_path=paths["authority"],
        source_path=paths["source"],
        hour_receipts_path=paths["hours"],
        session_ledger_path=paths["ledger"],
        alert_path=kwargs.get("alert_path"),
        output_root=paths["output"],
        generated_at_utc=GENERATED,
    )


def test_complete_day_creates_atomic_body_free_exact_allowlist(
        tmp_path, monkeypatch):
    _auth, _envelope, _segments, evidence, _hours, paths = _inputs(
        tmp_path, monkeypatch)
    marker_path = _create(paths)
    marker = gate.load_package(marker_path, expected_date=DATE)

    assert marker_path == paths["output"] / f"date={DATE}" / "ELIGIBLE.json"
    assert marker["state"] == gate.STATE
    assert marker["research_ready"] is True
    assert marker["analysis_hour_count"] == 24
    assert marker["watermark_hour_count"] == 2
    assert marker["eligible_objects"] == sorted(
        [{name: row[name] for name in gate.EXACT_FIELDS}
         for row in evidence["analysis_capture_objects"]],
        key=lambda row: (row["key"], row["version_id"]),
    )
    assert len(marker["eligible_objects"]) == 24
    assert len(marker["watermark_objects"]) == 2
    assert all("date=2026-07-18" not in row["key"]
               for row in marker["eligible_objects"])
    assert not ({row["key"] for row in marker["eligible_objects"]}
                & {row["key"] for row in marker["receipt_container_objects"]})
    assert marker["old_lineage_state"] == "DATA_INTEGRITY_BLOCKED"
    assert marker["repair_state"] == "FORBIDDEN"
    assert marker["old_lineage_overlap_count"] == 0
    assert marker["aws_reads"] == marker["aws_writes"] == 0
    assert marker["data_objects_copied"] == 0
    assert not any(paths["output"].glob(".date=*.pending-*"))

    # A timer retry revalidates and reuses the immutable committed package;
    # it does not generate a conflicting marker with a newer wall clock.
    assert _create(paths) == marker_path
    assert gate.load_package(marker_path, expected_date=DATE) == marker


def test_missing_hour_and_mixed_session_fail_closed(tmp_path, monkeypatch):
    _auth, _envelope, segments, _evidence, hour_set, paths = _inputs(
        tmp_path, monkeypatch)
    broken = copy.deepcopy(hour_set)
    broken["receipts"].pop()
    _write(paths["hours"], broken)
    with pytest.raises(gate.EligibilityError, match="HOUR_SET_INVALID"):
        _create(paths)
    assert not paths["output"].exists()

    _write(paths["hours"], hour_set)
    mixed = copy.deepcopy(segments)
    mixed[-1]["child_pid"] += 1
    _write(paths["ledger"], mixed, ndjson=True)
    with pytest.raises(gate.EligibilityError, match="SESSION_LEDGER_MISMATCH"):
        _create(paths)
    assert not paths["output"].exists()


def test_alert_and_old_lineage_overlap_fail_before_commit(tmp_path, monkeypatch):
    _auth, _envelope, _segments, evidence, _hours, paths = _inputs(
        tmp_path, monkeypatch)
    alert = tmp_path / "ALERT.json"
    alert.write_text("{}")
    with pytest.raises(gate.EligibilityError, match="CAPTURE_ALERT_PRESENT"):
        _create(paths, alert_path=alert)
    assert not paths["output"].exists()

    alert.unlink()
    overlap_root = tmp_path / "overlap"
    overlap_root.mkdir()
    overlap = {
        "key": "raw_rfq/old-overlap",
        "size": 100,
        "sha256": fixture.sha("2026-07-17T00-shard-0"),
    }
    with pytest.raises(fixture.fresh.FreshRfqError,
                       match="OLD_LINEAGE_OVERLAP"):
        _inputs(overlap_root, monkeypatch, overlap=overlap)
    assert not (overlap_root / "eligible").exists()


def test_tamper_and_pre_t0_day_fail_closed(tmp_path, monkeypatch):
    _auth, _envelope, _segments, _evidence, _hours, paths = _inputs(
        tmp_path, monkeypatch)
    marker_path = _create(paths)
    source_copy = marker_path.parent / gate.SOURCE_NAME
    source = json.loads(source_copy.read_text())
    source["analysis_capture_objects"][0]["sha256"] = hashlib.sha256(
        b"tamper").hexdigest()
    source_copy.chmod(0o640)
    _write(source_copy, source)
    with pytest.raises(gate.EligibilityError):
        gate.load_package(marker_path, expected_date=DATE)

    other = tmp_path / "pre-t0"
    other.mkdir()
    _auth, _envelope, _segments, _evidence, _hours, paths = _inputs(
        other, monkeypatch)
    with pytest.raises(gate.EligibilityError, match="DATE_MISMATCH|BEFORE_STRICT_T0"):
        gate.create_package(
            date="2026-07-16",
            authority_path=paths["authority"],
            source_path=paths["source"],
            hour_receipts_path=paths["hours"],
            session_ledger_path=paths["ledger"],
            alert_path=None,
            output_root=paths["output"],
            generated_at_utc=GENERATED,
        )
