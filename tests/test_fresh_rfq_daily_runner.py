"""The production producer builds products; it is not a marker consumer."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import fresh_rfq_daily_runner as runner  # noqa: E402
import test_fresh_rfq_daily_eligibility as eligibility_fixture  # noqa: E402


def test_runner_rebuilds_all_26_hour_receipts_from_session_ledger(
        tmp_path, monkeypatch):
    auth, _envelope, segments, evidence, expected, _paths = \
        eligibility_fixture._inputs(tmp_path, monkeypatch)
    built = runner.build_hour_set(
        date=eligibility_fixture.DATE, authority=auth,
        source_evidence=evidence, segments=segments)
    assert built == expected
    assert len(built["receipts"]) == 26


def test_session_ledger_is_streamed_and_not_bound_to_whole_file_limit(
        tmp_path, monkeypatch):
    auth, _envelope, segments, _evidence, _expected, _paths = \
        eligibility_fixture._inputs(tmp_path, monkeypatch)
    ledger = tmp_path / "long-lived-ledger.ndjson"
    irrelevant = json.dumps({"type": "old-generation", "padding": "x" * 8192})
    with ledger.open("wb") as handle:
        for _ in range(256):
            handle.write(irrelevant.encode() + b"\n")
        for row in segments:
            handle.write(runner._canonical(row) + b"\n")
    selected = runner._select_segments_path(
        ledger, expected_hours=runner._expected_hours(
            eligibility_fixture.DATE), authority=auth)
    assert selected == segments
    assert "_read_regular(session_ledger" not in (
        ROOT / "tools" / "fresh_rfq_daily_runner.py").read_text()


def test_exact_reader_disk_checkpoint_prevents_repeat_get(tmp_path):
    body = b'{"fresh":"rfq"}\n'
    identity = {
        "bucket": runner.BUCKET,
        "key": "ec2/raw/date=2026-07-20/rfq_00.ndjson",
        "version_id": "exact-v1", "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }

    class Transport:
        def __init__(self):
            self.gets = 0

        def get(self, supplied, destination):
            assert supplied == identity
            self.gets += 1
            destination.write_bytes(body)

    transport = Transport()
    reader = runner.ExactS3Reader(transport, tmp_path / "scratch")
    with reader.open_exact(identity) as opened:
        assert opened.path.read_bytes() == body
    with reader.open_exact(identity) as opened:
        assert opened.path.read_bytes() == body
    assert transport.gets == 1


def test_producer_unit_is_independent_and_aws_transport_has_no_write_api():
    service = (ROOT / "deploy" /
               "kalshi-fresh-rfq-daily-producer.service").read_text()
    timer = (ROOT / "deploy" /
             "kalshi-fresh-rfq-daily-producer.timer").read_text()
    installer = (ROOT / "deploy" /
                 "install_kalshi_fresh_rfq_daily_producer.sh").read_text()
    source = (ROOT / "tools" / "fresh_rfq_daily_runner.py").read_text()
    assert "fresh_rfq_source_evidence" in source
    assert "build_source_evidence_from_reader" in source
    assert "build_hour_set" in source
    assert "build_health_receipt" in source
    assert "put-object" not in source
    assert "put-object-tagging" not in source
    assert "Requires=kalshi-research-v3" not in service
    assert "Requires=kalshi-rfq-capture" not in service
    assert "fresh_rfq_daily_runner.py" in service
    assert "OnCalendar=" in timer
    assert "systemctl enable" in installer
    assert "systemctl start" not in installer
    assert "AUTHORITY_FILE_SHA=2fa1caf" in installer


def test_fixed_authority_and_minimal_iam_delta_are_bound_and_not_applied():
    import fresh_rfq_daily_eligibility as gate

    assert gate.PRODUCTION_STRICT_T0_UTC == "2026-07-20T00:00:00Z"
    assert gate.PRODUCTION_GENERATION == "fresh-rfq-20260720-01"
    assert gate.PRODUCTION_AUTHORITY_SHA256 == \
        "11faaf27e1f7b49689e77e6b58034d83ff23b94ed4d507291cfcff4deee37912"
    assert gate.PRODUCTION_ENVELOPE_SHA256 == \
        "0ef4e0d52911cfdd0b10ffd09ee770e56e5e19f0370d5068951627d6a3eb561e"
    assert gate.PRODUCTION_ENVELOPE_FILE_SHA256 == \
        "2fa1caf792d540e0b7ce4641bb82161f09ce3926a81652a2006468a9649ceaf2"

    path = (ROOT / "docs" / "plan_releases" / "pipeline" /
            "W-RFQ-FRESH-01_MINIMAL_IAM_DELTA_DRAFT_2026-07-18.json")
    delta = json.loads(path.read_text())
    assert delta["state"] == "DRAFT_NOT_APPLIED"
    assert delta["aws_mutations"] == 0
    tagger = delta["tagger_identity_changes"]
    assert "DenyAllRawTagInspectionAndMutationWhileRfqOff" in \
        tagger["remove_statement_sids"]
    dual = next(row for row in tagger["add_statements"]
                if row["Sid"] == "WriteFreshRfqExactVersionDualTags")
    assert dual["Action"] == "s3:PutObjectVersionTagging"
    assert dual["Condition"]["StringEquals"] == {
        "s3:RequestObjectTag/research-eligible": "true",
        "s3:RequestObjectTag/research-channel": "rfq",
    }
    w09 = delta["w09_identity_changes"]
    assert w09["list_bucket_grant_for_raw"] is False
    assert delta["application_enforcement"][
        "w09_reader_must_reject_every_exact_version_not_in_overlay_manifest"]


def test_runner_projects_complete_manual_version_pagination():
    prefix = "ec2/raw/date=2026-07-20/rfq_receipts_02.ndjson"

    class Transport:
        def list_version_pages(self, requested):
            assert requested == prefix
            return [{
                "KeyMarker": None, "VersionIdMarker": None,
                "IsTruncated": True, "NextKeyMarker": prefix,
                "NextVersionIdMarker": "v0",
                "Versions": [{
                    "Key": prefix, "VersionId": "v0", "IsLatest": True,
                    "Size": 10,
                }], "DeleteMarkers": [],
            }, {
                "KeyMarker": prefix, "VersionIdMarker": "v0",
                "IsTruncated": False,
                "Versions": [{
                    "Key": prefix + ".1", "VersionId": "v1",
                    "IsLatest": True, "Size": 11,
                }], "DeleteMarkers": [],
            }]

    latest, pages = runner._latest_family_inventory(
        Transport(), prefix, runner.CONTAINER_RE)
    assert latest == {prefix: "v0", prefix + ".1": "v1"}
    assert len(pages) == 2
    assert pages[0]["is_truncated"] is True
    assert pages[1]["request_version_id_marker"] == "v0"


def test_runner_cli_produces_products_instead_of_consuming_marker_only(
        tmp_path, monkeypatch, capsys):
    marker = tmp_path / "date=2026-07-19" / "ELIGIBLE.json"
    marker.parent.mkdir()
    marker.write_text("{}\n")
    calls = []
    monkeypatch.setattr(runner, "AwsReadOnly", lambda _path: object())
    monkeypatch.setattr(
        runner, "produce",
        lambda date, *, transport: calls.append((date, transport)) or marker)
    monkeypatch.setattr(
        runner.gate, "load_package",
        lambda path, *, expected_date: {
            "eligibility_sha256": "a" * 64, "eligible_objects": [{}],
        })
    assert runner.main(["--date", "2026-07-19"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "FRESH_RFQ_DAILY_PRODUCTS_READY"
    assert result["ready"][0]["date"] == "2026-07-19"
    assert calls and calls[0][0] == "2026-07-19"
