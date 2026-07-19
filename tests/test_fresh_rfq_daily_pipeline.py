"""Durable daily RFQ queue and honest IAM boundary tests."""

import contextlib
import datetime as dt
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))
import fresh_rfq_daily_pipeline as pipeline  # noqa: E402


UTC = dt.timezone.utc


def test_date_queue_never_drops_a_date_after_ninety_days(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline.gate, "PRODUCTION_STRICT_T0_UTC", "2026-01-01T00:00:00Z")
    now = dt.datetime(2026, 5, 2, 3, tzinfo=UTC)
    dates = pipeline.seed_queue(tmp_path, now)
    assert dates[0] == "2026-01-01"
    assert dates[-1] == "2026-05-01"
    assert len(dates) > 90
    entries = pipeline.load_queue(tmp_path)
    assert entries[0][1]["state"] == "PENDING_ELIGIBILITY"
    assert entries[0][1]["date"] == "2026-01-01"


def test_one_quarantined_date_does_not_head_block_later_dates(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline.gate, "PRODUCTION_STRICT_T0_UTC", "2026-07-20T00:00:00Z")
    now = dt.datetime(2026, 7, 23, 3, tzinfo=UTC)
    visited = []

    def process(state, **_kwargs):
        visited.append(state["date"])
        return pipeline._advance(
            state, now=now,
            new_state=("QUARANTINED_DATA" if state["date"] == "2026-07-20"
                       else "WAITING_IAM"),
            resume_stage="IAM",
            next_attempt="9999-12-31T23:59:59Z")

    monkeypatch.setattr(pipeline, "process_date", process)
    result = pipeline.run(
        now=now, transport=object(), state_root=tmp_path,
        base_status_root=tmp_path / "base",
        capability_path=tmp_path / "capability")
    assert visited == ["2026-07-20", "2026-07-21", "2026-07-22"]
    assert result["state_counts"] == {
        "QUARANTINED_DATA": 1, "WAITING_IAM": 2}


def test_pipeline_builds_overlay_then_waits_durably_for_iam(
        tmp_path, monkeypatch):
    now = dt.datetime(2026, 7, 22, 3, tzinfo=UTC)
    date = "2026-07-20"
    state = pipeline._new_state(date, now)
    eligible = tmp_path / "ELIGIBLE.json"
    ready = tmp_path / "READY.json"
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text("{}\n")
    ready.write_text("{}\n")
    terminal = tmp_path / "STATUS.json"
    identity = {
        "bucket": "kalshi-vault-ritcardo",
        "key": "research/releases/r/MANIFEST.json",
        "version_id": "v1", "size": 3, "sha256": "a" * 64,
    }
    monkeypatch.setattr(
        pipeline.producer, "produce",
        lambda supplied, **_kwargs: eligible)
    monkeypatch.setattr(
        pipeline, "discover_base_terminal",
        lambda supplied, root: (terminal, {}, identity))

    class Reader:
        def __init__(self, *_args, **_kwargs):
            self.scratch_root = tmp_path / "scratch"

        @contextlib.contextmanager
        def open_exact(self, supplied):
            assert supplied == identity
            yield SimpleNamespace(path=manifest)

    monkeypatch.setattr(pipeline.producer, "ExactS3Reader", Reader)
    monkeypatch.setattr(
        pipeline.overlay, "create_overlay_cache_from_reader",
        lambda **_kwargs: ready)
    monkeypatch.setattr(pipeline, "_purge_exact_cache", lambda _reader: None)
    monkeypatch.setattr(pipeline, "load_capability", lambda _path: None)
    result = pipeline.process_date(
        state, now=now, transport=object(), state_root=tmp_path,
        base_status_root=tmp_path / "base",
        capability_path=tmp_path / "IAM-CAPABILITY.json")
    assert result["state"] == "WAITING_IAM"
    assert result["resume_stage"] == "IAM"
    assert result["eligibility_path"] == str(eligible)
    assert result["overlay_ready_path"] == str(ready)
    assert result["aws_write_state"] == "NOT_AUTHORIZED"
    persisted = pipeline._validate_state(
        pipeline._read_json(pipeline._queue_path(tmp_path, date)), date)
    assert persisted["overlay_ready_path"] == str(ready)


def test_waiting_iam_state_resumes_to_exact_publication(tmp_path, monkeypatch):
    now = dt.datetime(2026, 7, 22, 4, tzinfo=UTC)
    date = "2026-07-20"
    ready = tmp_path / "READY.json"
    ready.write_text("{}\n")
    eligible = tmp_path / "ELIGIBLE.json"
    state = pipeline._new_state(date, now)
    state = pipeline._advance(
        state, now=now, new_state="WAITING_IAM", resume_stage="IAM",
        eligibility_path=str(eligible), overlay_ready_path=str(ready))
    identity = {
        "bucket": "kalshi-vault-ritcardo",
        "key": "research/releases/r/MANIFEST.json",
        "version_id": "v1", "size": 3, "sha256": "a" * 64,
    }
    monkeypatch.setattr(
        pipeline.gate, "load_package", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        pipeline, "discover_base_terminal",
        lambda supplied, root: (tmp_path / "STATUS.json", {}, identity))
    monkeypatch.setattr(
        pipeline, "load_capability", lambda _path: {"state": "applied"})
    calls = []

    def publish(path, capability):
        calls.append((path, capability))
        return {
            "state": pipeline.PUBLISHED_STATE,
            "date": date,
            "exact_version_tag_readback": True,
            "manifest_exact_version_verified": True,
        }

    result = pipeline.process_date(
        state, now=now, transport=object(), state_root=tmp_path,
        base_status_root=tmp_path / "base",
        capability_path=tmp_path / "IAM-CAPABILITY.json",
        publisher=publish)
    assert result["state"] == "PUBLISHED"
    assert result["resume_stage"] == "COMPLETE"
    assert result["aws_write_state"] == "EXACT_WRITES_VERIFIED"
    assert calls == [(ready, {"state": "applied"})]
