"""The production producer builds products; it is not a marker consumer."""

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
    assert "AUTHORITY_FILE_SHA=8de2bef" in installer


def test_fixed_authority_and_minimal_iam_delta_are_bound_and_not_applied():
    import fresh_rfq_daily_eligibility as gate

    assert gate.PRODUCTION_STRICT_T0_UTC == "2026-07-19T00:00:00Z"
    assert gate.PRODUCTION_GENERATION == "fresh-rfq-20260719-01"
    assert gate.PRODUCTION_AUTHORITY_SHA256 == \
        "6540583799317c7f19a57f7d67c639afbd8832186fe55fd735e4f3cbb2cb9160"
    assert gate.PRODUCTION_ENVELOPE_SHA256 == \
        "523543a651870b1611c8320957aa5ed0912da21b64a26ca0ee7bba1a8e47840b"
    assert gate.PRODUCTION_ENVELOPE_FILE_SHA256 == \
        "8de2bef22158879e706f7af3bdd8b0cea13ae7cda9cfd4a98a09ee709c830b9b"

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
