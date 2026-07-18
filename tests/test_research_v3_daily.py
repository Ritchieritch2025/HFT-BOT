import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "research_v3_daily.py"
SPEC = importlib.util.spec_from_file_location("research_v3_daily", MODULE_PATH)
daily = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = daily
SPEC.loader.exec_module(daily)
TOOLS = str(ROOT / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import canonical_receipts as cr  # noqa: E402
import forward_canonical_receipts as fcr  # noqa: E402
import publication_generation as pg  # noqa: E402
import research_reference as reference  # noqa: E402

DATE = "2026-07-14"
RECEIPT_SHA = "a" * 64
RELEASE_ID = f"{DATE}__v3ref__seal-12345678__pub-abcdef0123456789"
MANIFEST_BYTES = (json.dumps({"release_id": RELEASE_ID}, sort_keys=True)
                  + "\n").encode()
AUTOMATION_AUTHORIZATION = (
    ROOT / "docs" / "plan_releases" / "pipeline"
    / "W-PUB-REF-01C_AUTOMATION_EXECUTION_AUTHORIZATION_2026-07-17.json")


def _mock_secret_stat(monkeypatch, path, *, file_mode, parent_mode):
    def fake_stat(candidate, *, follow_symlinks=False):
        assert follow_symlinks is False
        if Path(candidate) == path:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | file_mode,
                st_uid=0,
                st_gid=0,
                st_size=152,
            )
        assert Path(candidate) == path.parent
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | parent_mode,
            st_uid=0,
            st_gid=0,
        )

    monkeypatch.setattr(daily.os, "stat", fake_stat)


def test_secret_file_accepts_exact_systemd_encrypted_shape(monkeypatch):
    path = Path("/run/credentials/research-v3/publisher.env")
    _mock_secret_stat(
        monkeypatch, path, file_mode=0o440, parent_mode=0o550)

    assert daily._secure_secret_file(path, "publisher") == path


@pytest.mark.parametrize(
    ("file_mode", "parent_mode"),
    [(0o640, 0o550), (0o440, 0o750)],
)
def test_secret_file_rejects_broader_systemd_modes(
        monkeypatch, file_mode, parent_mode):
    path = Path("/run/credentials/research-v3/publisher.env")
    _mock_secret_stat(
        monkeypatch, path, file_mode=file_mode, parent_mode=parent_mode)

    with pytest.raises(daily.GateError) as excinfo:
        daily._secure_secret_file(path, "publisher")

    assert excinfo.value.code == "CREDENTIAL_FILE_INVALID"


def _write_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    path.chmod(mode)
    return path


def test_complete_json_document_parser_accepts_pretty_aws_output_only():
    payload = {"IsTruncated": False, "Contents": [{"Key": "exact"}]}

    assert daily._json_document(
        json.dumps(payload, indent=2), "aws response") == payload
    with pytest.raises(daily.GateError, match="COMMAND_OUTPUT_INVALID"):
        daily._json_document(
            "unexpected diagnostic\n" + json.dumps(payload, indent=2),
            "aws response")


def test_daily_metadata_and_durable_verification_use_max_four_workers():
    assert daily.CANONICAL_VERIFY_WORKERS == cr.MAX_VERIFY_WORKERS == 4
    source = MODULE_PATH.read_text()
    call = '"--workers", str(CANONICAL_VERIFY_WORKERS)'
    first = source.index(call)
    second = source.index(call, first + 1)
    assert source.count(call) == 2
    assert source.index('"shadow-forward", "--date"') < first
    assert first < source.index('"publish-receipt", *common') < second


def _fixture_tree(tmp_path, *, rfq=False, tagged=False):
    live = tmp_path / "work" / "live"
    raw_root = tmp_path / "work" / "raw"
    warehouse_root = tmp_path / "work" / "warehouse"
    quality_dir = tmp_path / "work" / "event_packs"
    fresh_rfq_root = live / "fresh_rfq_research"
    home = tmp_path / "home"
    for path in (live, raw_root, warehouse_root, quality_dir,
                 fresh_rfq_root, home):
        path.mkdir(parents=True, exist_ok=True)
    durable_dir = (live / "canonical_receipts" / "durable"
                   / ("date=" + DATE))
    key = (f"ec2/raw/date={DATE}/rfq_23.ndjson" if rfq else
           f"ec2/warehouse/seals/date={DATE}.json")
    obj = {
        "bucket": daily.DEFAULT_BUCKET,
        "key": key,
        "VersionId": "version-1",
        "source_kind": "raw_rfq" if rfq else "seal",
        "channel": "rfq" if rfq else None,
        "research_candidate": not rfq,
    }
    receipt = {
        "schema_version": "canonical-object-receipt-v1",
        "state": "DURABLE_RECEIPT_VERIFIED",
        "authority": "CANONICAL_CONTROL_PLANE",
        "authoritative": True,
        "s3_published": True,
        "date": DATE,
        "receipt_set_sha256": RECEIPT_SHA,
        "seal": {"date": DATE, "status": "SEALED", "version": 2},
        "families": ([{
            "name": "raw_rfq_deferred",
            "state": "NOT_APPLICABLE",
            "observed_count": 0,
            "reason_code": "RFQ_BRANCH_CLOSED_NO_REPAIR",
        }] if not rfq else []),
        "objects": [obj],
    }
    receipt_path = _write_json(
        durable_dir / f"receipt-{RECEIPT_SHA}.json", receipt)
    raw = receipt_path.read_bytes()
    index = {
        "schema_version": "canonical-durable-receipt-index-v1",
        "state": "DURABLE_RECEIPT_VERIFIED",
        "date": DATE,
        "receipt_set_sha256": RECEIPT_SHA,
        "complete": True,
        "prune_eligible": False,
        "receipt_object": {
            "bucket": daily.DEFAULT_BUCKET,
            "key": (f"ec2/control/canonical-receipts/v1/date={DATE}/"
                    f"receipt-{RECEIPT_SHA}.json"),
            "VersionId": "receipt-version-1",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        },
    }
    index_path = _write_json(
        durable_dir / f"DURABLE-{RECEIPT_SHA}.json", index)

    audit_root = live / "research_v3_audit"
    evidence = _write_json(
        audit_root / ("date=" + DATE) / "policy-evidence.json",
        {"policy": "fixture"})
    evidence_raw = evidence.read_bytes()
    audit = {
        "schema_version": "canonical-eligibility-single-writer-audit-v1",
        "state": "SINGLE_WRITER_VERIFIED",
        "date": DATE,
        "bucket": daily.DEFAULT_BUCKET,
        "receipt_set_sha256": RECEIPT_SHA,
        "tag_key": "research-eligible",
        "dedicated_tagger_principal": daily.DEFAULT_TAGGER_ARN,
        "dedicated_tagger_sts_caller_arn": daily.DEFAULT_TAGGER_ARN,
        "exclusive_exact_version_writer": True,
        "versionless_tagging_denied": True,
        "other_automation_tag_writers_denied": True,
        "audited_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "auditor": "fixture-auditor",
        "policy_evidence_size": len(evidence_raw),
        "policy_evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
    }
    audit_path = _write_json(
        audit_root / ("date=" + DATE)
        / "single-writer-audit.json", audit)
    audit_sha = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    secrets.chmod(0o700)
    tagger_creds = secrets / "tagger.credentials"
    tagger_creds.write_text(
        "[canonical-eligibility-tagger]\n"
        "aws_access_key_id=fixture\naws_secret_access_key=fixture\n")
    tagger_creds.chmod(0o600)
    publisher_env = secrets / "publisher.env"
    publisher_env.write_text(
        "export KALSHI_PRIVATE_KEY_PATH=\"$HOME/.kalshi/private_key.pem\"\n"
        "AWS_ACCESS_KEY_ID=fixture\nAWS_SECRET_ACCESS_KEY=fixture\n")
    publisher_env.chmod(0o600)
    aws = tmp_path / "aws"
    aws.write_text("#!/bin/sh\nexit 1\n")
    aws.chmod(0o700)
    cutover_arm = home / "research_zero_copy_v3_cutover_approved"
    publish_arm = home / "research_publish_approved"
    cutover_arm.write_bytes(AUTOMATION_AUTHORIZATION.read_bytes())
    publish_arm.write_bytes(AUTOMATION_AUTHORIZATION.read_bytes())
    cutover_arm.chmod(0o600)
    publish_arm.chmod(0o600)

    tagged_path = None
    if tagged:
        tagged_path = _make_tagged(live, audit_sha)
    return {
        "live": live,
        "raw_root": raw_root,
        "warehouse_root": warehouse_root,
        "quality_dir": quality_dir,
        "fresh_rfq_root": fresh_rfq_root,
        "home": home,
        "audit_root": audit_root,
        "durable_index": index_path,
        "audit": audit_path,
        "evidence": evidence,
        "tagger_creds": tagger_creds,
        "publisher_env": publisher_env,
        "aws": aws,
        "cutover_arm": cutover_arm,
        "publish_arm": publish_arm,
        "audit_sha": audit_sha,
        "tagged_index": tagged_path,
    }


def _make_tagged(live, audit_sha):
    digest = "b" * 64
    root = live / "canonical_receipts" / "tagged" / ("date=" + DATE)
    receipt = {
        "schema_version": "canonical-object-receipt-v1",
        "state": "DURABLE_RECEIPT_VERIFIED",
        "authority": "CANONICAL_CONTROL_PLANE",
        "authoritative": True,
        "s3_published": True,
        "date": DATE,
        "prune_eligible": False,
        "receipt_set_sha256": digest,
        "byte_attestation_receipt_set_sha256": RECEIPT_SHA,
        "receipt_phase": "TAGGED_ELIGIBILITY_VERIFIED",
        "eligibility_single_writer_audit_sha256": audit_sha,
        "families": [],
        "objects": [{
            "bucket": daily.DEFAULT_BUCKET,
            "key": f"ec2/warehouse/seals/date={DATE}.json",
            "VersionId": "version-1",
            "source_kind": "seal",
            "research_candidate": True,
        }],
    }
    receipt_path = _write_json(root / f"receipt-{digest}.json", receipt)
    raw = receipt_path.read_bytes()
    return _write_json(root / f"TAGGED-DURABLE-{digest}.json", {
        "schema_version": "canonical-durable-receipt-index-v1",
        "state": "DURABLE_RECEIPT_VERIFIED",
        "date": DATE,
        "receipt_set_sha256": digest,
        "complete": True,
        "completed": True,
        "prune_eligible": False,
        "receipt_phase": "TAGGED_ELIGIBILITY_VERIFIED",
        "byte_attestation_receipt_set_sha256": RECEIPT_SHA,
        "receipt_object_eligibility_tag_state": "TAGGED_VERIFIED",
        "eligibility_single_writer_audit_sha256": audit_sha,
        "receipt_payload_size": len(raw),
        "receipt_payload_sha256": hashlib.sha256(raw).hexdigest(),
        "receipt_object": {
            "bucket": daily.DEFAULT_BUCKET,
            "key": (f"ec2/control/canonical-receipts/v1/date={DATE}/"
                    f"receipt-{digest}.json"),
            "VersionId": "tagged-receipt-version",
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        },
    })


def _make_daily_proof(live):
    payload = b'{"fixture":"precommit"}\n'
    digest = hashlib.sha256(payload).hexdigest()
    path = (live / "canonical_receipts" / "tag-precommit"
            / ("date=" + DATE) / ("PRECOMMIT-" + digest + ".json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _reference_publisher_output(command):
    """Return strict prepare/commit result fixtures for wrapped publishers."""
    rfq_mode = (daily.RFQ_FRESH if "--include-rfq" in command
                else daily.RFQ_OFF)
    if "--prepare-only" in command:
        root = Path(command[command.index("--prepare-output-root") + 1])
        payload = (json.dumps({
            "date": DATE,
            "release_id": RELEASE_ID,
            "state": daily.PREPARED_PLAN_STATE,
        }, sort_keys=True, indent=2) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        plan = root / f"PREPARED-{digest}.json"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_bytes(payload)
        return json.dumps({
            "schema_version": daily.PREPARED_PLAN_SCHEMA,
            "state": daily.PREPARED_PLAN_STATE,
            "date": DATE,
            "release_id": RELEASE_ID,
            "prepared_plan": str(plan.absolute()),
            "prepared_plan_sha256": digest,
            "prepared_plan_size": len(payload),
            "reference_set_sha256": "c" * 64,
            "s3_writes": 0,
            "rfq": rfq_mode,
        }) + "\n"
    if "--prepared-plan" in command:
        plan = Path(command[command.index("--prepared-plan") + 1])
        plan_sha = plan.stem.removeprefix("PREPARED-")
        return json.dumps({
            "schema_version": daily.MANIFEST_COMMIT_RESULT_SCHEMA,
            "state": "REFERENCE_MANIFEST_COMMITTED",
            "release_id": RELEASE_ID,
            "manifest_object": {
                "bucket": daily.DEFAULT_BUCKET,
                "key": f"research/releases/{RELEASE_ID}/MANIFEST.json",
                "VersionId": "manifest-version-1",
                "size": len(MANIFEST_BYTES),
                "sha256": hashlib.sha256(MANIFEST_BYTES).hexdigest(),
                "verification_state": "EXACT_VERSION_FULL_SHA256",
            },
            "data_uploads": 0,
            "rfq": rfq_mode,
            "prepared_plan_sha256": plan_sha,
        }) + "\n"
    return None


def _args(tree, *extra):
    return [
        "--date", DATE,
        "--live-dir", str(tree["live"]),
        "--audit-root", str(tree["audit_root"]),
        "--raw-root", str(tree["raw_root"]),
        "--warehouse-root", str(tree["warehouse_root"]),
        "--quality-dir", str(tree["quality_dir"]),
        "--fresh-rfq-eligibility-root", str(tree["fresh_rfq_root"]),
        "--tagger-credentials-file", str(tree["tagger_creds"]),
        "--publisher-env-file", str(tree["publisher_env"]),
        "--home", str(tree["home"]),
        "--aws-cli", str(tree["aws"]),
        "--python", str(Path(sys.executable).absolute()),
        "--cutover-arm-file", str(tree["cutover_arm"]),
        "--publish-arm-file", str(tree["publish_arm"]),
        *extra,
    ]


def _pin_production(monkeypatch, tree):
    monkeypatch.setattr(daily, "DEFAULT_PRODUCTION_LIVE_DIR", tree["live"])
    monkeypatch.setattr(daily, "DEFAULT_PRODUCTION_RAW_ROOT", tree["raw_root"])
    monkeypatch.setattr(
        daily, "DEFAULT_PRODUCTION_WAREHOUSE_ROOT", tree["warehouse_root"])
    monkeypatch.setattr(
        daily, "DEFAULT_PRODUCTION_QUALITY_DIR", tree["quality_dir"])
    monkeypatch.setattr(daily, "DEFAULT_PRODUCTION_HOME", tree["home"])
    monkeypatch.setattr(
        daily, "DEFAULT_PUBLISHER_ENV_FILE", tree["publisher_env"])
    monkeypatch.setattr(
        daily, "DEFAULT_DURABLE_PUBLISHER_ENV_FILE", tree["publisher_env"])
    monkeypatch.setattr(
        daily, "DEFAULT_TAGGER_CREDENTIAL_FILE", tree["tagger_creds"])
    monkeypatch.setattr(daily, "DEFAULT_CUTOVER_ARM", tree["cutover_arm"])
    monkeypatch.setattr(daily, "DEFAULT_PUBLISH_ARM", tree["publish_arm"])


def test_check_is_local_only_and_reports_rfq_off(tmp_path, monkeypatch, capsys):
    tree = _fixture_tree(tmp_path)

    def forbid(*_args, **_kwargs):
        pytest.fail("--check must not spawn any process or call AWS")

    monkeypatch.setattr(daily.subprocess, "run", forbid)
    assert daily.main(_args(tree, "--check")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "CHECK"
    assert result["rfq"] == "OFF"
    assert result["aws_mutations"] == 0


def test_dry_run_accepts_historical_queue_and_deduplicates_dates(
        tmp_path, monkeypatch, capsys):
    tree = _fixture_tree(tmp_path)
    queue = tmp_path / "dates.queue"
    queue.write_text(f"# backfill\n{DATE}\n{DATE}\n")
    queue.chmod(0o600)
    monkeypatch.setattr(
        daily.subprocess, "run",
        lambda *_a, **_kw: pytest.fail("dry-run spawned a process"))
    argv = _args(tree, "--queue", str(queue), "--dry-run")
    assert daily.main(argv) == 0
    assert capsys.readouterr().out.count('"state": "V3_DAILY_PLAN"') == 1


def test_queue_rejects_flags_instead_of_parsing_them(tmp_path):
    queue = tmp_path / "bad.queue"
    queue.write_text("--include-rfq\n")
    queue.chmod(0o600)
    with pytest.raises(daily.GateError, match="QUEUE_INVALID"):
        daily.load_dates([], queue, False)


def test_authorization_date_scope_blocks_only_pre_2026_07_10():
    failures = []
    dates = daily.load_dates([
        "2026-07-09", "2026-07-10", "2026-07-16", "2026-07-17",
        "2027-01-01",
    ], None, False, discovery_failures=failures)
    assert dates == [
        "2026-07-10", "2026-07-16", "2026-07-17", "2027-01-01"]
    assert failures == [{
        "date": "2026-07-09",
        "code": "AUTHORIZATION_DATE_SCOPE_BLOCKED",
        "detail": (
            "date/queue/scan discovery: automation authorization begins at "
            "2026-07-10; older dates are not authorized"),
    }]


def test_check_reports_pre_authorization_date_but_plans_allowed_date(
        tmp_path, monkeypatch, capsys):
    tree = _fixture_tree(tmp_path)
    monkeypatch.setattr(
        daily.subprocess, "run",
        lambda *_a, **_kw: pytest.fail("check mode spawned a process"))
    assert daily.main(_args(
        tree, "--date", "2026-07-09", "--check")) == 2
    captured = capsys.readouterr()
    assert "AUTHORIZATION_DATE_SCOPE_BLOCKED" in captured.err
    plans = [json.loads(line) for line in captured.out.splitlines() if line]
    assert [row["date"] for row in plans] == [DATE]
    assert plans[0]["mode"] == "CHECK"


def test_unfinished_discovery_never_requeues_pre_authorization_date(tmp_path):
    live = tmp_path / "live"
    old = live / "research_v3_daily" / "date=2026-07-09"
    _write_json(old / "PREPARE_STATUS.json", {"state": "BLOCKED"})
    failures = []
    assert daily.discover_unfinished_dates(live, failures) == []
    assert [(row["date"], row["code"]) for row in failures] == [
        ("2026-07-09", "AUTHORIZATION_DATE_SCOPE_BLOCKED")]


def test_any_rfq_object_refuses_before_subprocess(tmp_path, monkeypatch):
    tree = _fixture_tree(tmp_path, rfq=True)
    monkeypatch.setattr(
        daily.subprocess, "run",
        lambda *_a, **_kw: pytest.fail("RFQ gate was reached too late"))
    assert daily.main(_args(tree, "--dry-run")) == 2


def test_daily_fresh_rfq_gate_is_per_date_and_degrades_only_to_off(
        tmp_path, monkeypatch):
    root = tmp_path / "fresh"
    marker_path = root / ("date=" + DATE) / "ELIGIBLE.json"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text("{}\n")
    args = SimpleNamespace(fresh_rfq_eligibility_root=str(root))
    marker = {"eligibility_sha256": "e" * 64}
    monkeypatch.setattr(
        daily, "_load_fresh_rfq_gate",
        lambda path, date: marker if path == marker_path and date == DATE
        else pytest.fail("wrong daily gate lookup"))

    assert daily._fresh_rfq_gate_for_date(DATE, args) == (
        daily.RFQ_FRESH, marker_path, marker, "STRICT_GATE_PASS")
    assert daily._fresh_rfq_gate_for_date(
        "2026-07-15", args)[0] == daily.RFQ_OFF

    def invalid(*_args):
        raise daily.GateError("RFQ_ELIGIBILITY_INVALID", "tampered")

    monkeypatch.setattr(daily, "_load_fresh_rfq_gate", invalid)
    mode, path, value, reason = daily._fresh_rfq_gate_for_date(DATE, args)
    assert (mode, path, value) == (daily.RFQ_OFF, None, None)
    assert reason.startswith("RFQ_ELIGIBILITY_INVALID:")


def test_execute_fresh_rfq_requires_paired_gate_tag_and_manifest_modes(
        tmp_path, monkeypatch):
    tree = _fixture_tree(tmp_path)
    gate_path = tree["fresh_rfq_root"] / ("date=" + DATE) / "ELIGIBLE.json"
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text("{}\n")
    evidence = tmp_path / "RFQ-ELIGIBILITY.json"
    evidence.write_text("{}\n")
    marker = {"eligibility_sha256": "e" * 64}
    plan = daily.DatePlan(
        DATE, tree["durable_index"],
        tree["durable_index"].with_name(f"receipt-{RECEIPT_SHA}.json"),
        RECEIPT_SHA, tree["audit"], tree["evidence"], None,
        tree["audit_sha"], rfq_mode=daily.RFQ_FRESH,
        fresh_rfq_eligibility=gate_path,
        fresh_rfq_eligibility_sha256=marker["eligibility_sha256"],
        rfq_eligibility_evidence=evidence)
    args = SimpleNamespace(
        python=str(Path(sys.executable).absolute()),
        aws_cli=str(tree["aws"]), live_dir=str(tree["live"]),
        tagger_principal=daily.DEFAULT_TAGGER_ARN,
        dest=daily.DEFAULT_DEST, quality_dir=str(tree["quality_dir"]),
        raw_vault="s3://kalshi-vault-ritcardo/ec2/raw")
    calls = []
    tagged = tmp_path / "TAGGED-DURABLE.json"
    tagged.write_text("{}\n")

    monkeypatch.setattr(
        daily, "_load_fresh_rfq_gate",
        lambda path, date: marker if path == gate_path and date == DATE
        else pytest.fail("fresh gate binding changed"))
    monkeypatch.setattr(daily, "_require_arms", lambda _args: None)
    monkeypatch.setattr(daily, "_verify_seal", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        daily, "_identity", lambda command, expected_arn, **_kw: {
            "Arn": expected_arn, "Account": "321572485933", "UserId": "U"})
    monkeypatch.setattr(daily, "_validate_tagged_index", lambda *_a, **_kw: "f" * 64)

    def fake_run(command, **_kwargs):
        calls.append(command)
        if "--verify-only" in command:
            proof = _make_daily_proof(tree["live"])
            return json.dumps({
                "state": "EXACT_VERSION_TAG_READBACK_VERIFIED",
                "proof": str(proof), "tag_puts": 0,
                "rfq": daily.RFQ_FRESH,
            }) + "\n"
        return json.dumps({
            "index": str(tagged), "rfq": daily.RFQ_FRESH,
        }) + "\n"

    def fake_publisher(command, **_kwargs):
        calls.append(command)
        return _reference_publisher_output(command)

    monkeypatch.setattr(daily, "_run", fake_run)
    monkeypatch.setattr(daily, "_publisher_run", fake_publisher)

    _tagged, result = daily.execute_date(
        plan, args, tag_env={}, publisher_environment={})
    assert result["rfq"] == daily.RFQ_FRESH
    tag = next(command for command in calls
               if "canonical_eligibility_tagger.py" in " ".join(command)
               and "--verify-only" not in command)
    proof = next(command for command in calls if "--verify-only" in command)
    prepare = next(command for command in calls if "--prepare-only" in command)
    commit = next(command for command in calls if "--prepared-plan" in command)
    assert "--include-sealed-rfq" in tag
    assert tag[tag.index("--rfq-eligibility-evidence") + 1] == str(evidence)
    assert "--include-sealed-rfq" in proof
    assert "--include-rfq" in prepare and "--no-rfq" not in prepare
    assert "--include-rfq" in commit and "--no-rfq" not in commit


def test_credential_environments_are_disjoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "publisher-leak")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "trading-leak")
    creds = tmp_path / "tagger.credentials"
    creds.write_text("fixture")
    env = daily.tagger_env(tmp_path, creds, "tagger", "us-east-2")
    assert env["AWS_PROFILE"] == "tagger"
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == str(creds)
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "KALSHI_API_KEY_ID" not in env
    env_file = tmp_path / "publisher.env"
    env_file.write_text(
        'KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi/key.pem"\n'
        "AWS_ACCESS_KEY_ID=publisher\nAWS_SECRET_ACCESS_KEY=secret\n")
    parsed = daily.publisher_env(tmp_path, env_file, "us-east-2")
    assert parsed["AWS_ACCESS_KEY_ID"] == "publisher"
    assert "KALSHI_PRIVATE_KEY_PATH" not in parsed
    command = daily.publisher_command(
        "/snap/bin/aws", daily.DEFAULT_PUBLISHER_ARN, ["aws"])
    assert str(creds) not in command
    assert "actual_arn=" in command[4]


def test_shared_home_aws_config_is_ignored_and_publisher_shell_enforces_it(
        tmp_path):
    home = tmp_path / "shared-home"
    aws_home = home / ".aws"
    aws_home.mkdir(parents=True)
    (aws_home / "config").write_text(
        "[default]\nendpoint_url=http://attacker.invalid\n"
        "credential_process=/bin/false\n")
    (aws_home / "credentials").write_text(
        "[default]\naws_access_key_id=home-injected\n"
        "aws_secret_access_key=home-injected\n")
    publisher_file = tmp_path / "publisher.env"
    publisher_file.write_text(
        "AWS_ACCESS_KEY_ID=publisher\nAWS_SECRET_ACCESS_KEY=secret\n")
    tagger_file = tmp_path / "tagger.credentials"
    tagger_file.write_text("[tagger]\naws_access_key_id=tagger\n")

    publisher = daily.publisher_env(home, publisher_file, "us-east-2")
    tagger = daily.tagger_env(home, tagger_file, "tagger", "us-east-2")
    fixed = {
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_CLI_HISTORY_FILE": "/dev/null",
        "AWS_CLI_HISTORY_ENABLED": "false",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    assert {key: publisher[key] for key in fixed} == fixed
    assert {key: tagger[key] for key in fixed} == fixed
    assert publisher["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert "AWS_PROFILE" not in publisher
    assert tagger["AWS_SHARED_CREDENTIALS_FILE"] == str(tagger_file)
    assert tagger["AWS_PROFILE"] == "tagger"

    fake_aws = tmp_path / "aws"
    fake_aws.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = sts ]; then\n"
        f"  echo {daily.DEFAULT_PUBLISHER_ARN}\n"
        "  exit 0\n"
        "fi\n"
        "[ \"$1\" = probe ] && exit 0\n"
        "exit 1\n")
    fake_aws.chmod(0o700)
    command = daily.publisher_command(
        str(fake_aws), daily.DEFAULT_PUBLISHER_ARN,
        [str(fake_aws), "probe"])
    assert subprocess.run(command, env=publisher, capture_output=True).returncode == 0
    injected = dict(publisher)
    injected["AWS_CONFIG_FILE"] = str(aws_home / "config")
    refused = subprocess.run(command, env=injected, capture_output=True,
                             text=True)
    assert refused.returncode == 78
    assert "profile/shared config is not isolated" in refused.stderr


def test_real_plan_uses_non_rfq_tagger_and_explicit_live_dir(
        tmp_path, monkeypatch):
    tree = _fixture_tree(tmp_path)
    _pin_production(monkeypatch, tree)
    calls = []

    def fake_identity(command, expected_arn, **_kwargs):
        calls.append(command)
        return {"Arn": expected_arn, "Account": "321572485933", "UserId": "U"}

    def fake_run(command, **_kwargs):
        calls.append(command)
        flat = " ".join(command)
        publisher = _reference_publisher_output(command)
        if publisher is not None:
            return publisher
        if "canonical_eligibility_tagger.py" in flat:
            if "--verify-only" in command:
                proof = _make_daily_proof(tree["live"])
                return json.dumps({
                    "state": "EXACT_VERSION_TAG_READBACK_VERIFIED",
                    "proof": str(proof), "tag_puts": 0, "rfq": "OFF",
                }) + "\n"
            tagged = _make_tagged(tree["live"], tree["audit_sha"])
            return json.dumps({
                "index": str(tagged), "rfq": daily.RFQ_OFF,
            }) + "\n"
        return ""

    monkeypatch.setattr(daily, "_identity", fake_identity)
    monkeypatch.setattr(daily, "_run", fake_run)
    monkeypatch.setattr(
        daily, "refresh_policy_patrol_evidence",
        lambda *_a, **_kw: tree["evidence"])
    monkeypatch.setattr(
        daily, "refresh_layered_single_writer_audit",
        lambda *_a, **_kw: (tree["audit"], tree["evidence"]))
    assert daily.main(_args(tree, "--operator-approved")) == 0
    flattened = [" ".join(command) for command in calls]
    tagger = next(row for row in flattened
                  if "canonical_eligibility_tagger.py" in row)
    publishers = [row for row in flattened if "publish-reference" in row]
    assert "include-rfq" not in tagger
    assert "sealed-rfq" not in tagger
    assert len(publishers) == 2
    prepare, commit = publishers
    assert "--prepare-only" in prepare
    assert "--tag-precommit-proof" not in prepare
    assert "--prepared-plan" in commit
    assert "--tag-precommit-proof" in commit
    assert all("--no-rfq" in row for row in publishers)
    assert all(f"--live-dir {tree['live']}" in row for row in publishers)
    assert all("systemctl" not in row for row in flattened)
    status = json.loads((tree["live"] / "research_v3_daily"
                         / ("date=" + DATE)
                         / ("receipt=" + RECEIPT_SHA)
                         / "STATUS.json").read_text())
    assert status["state"] == "V3_REFERENCE_PUBLISHED"
    assert status["schema_version"] == daily.TERMINAL_STATUS_SCHEMA
    assert status["release_id"] == RELEASE_ID
    assert status["manifest_object"]["VersionId"] == "manifest-version-1"
    assert len(status["prepared_plan_sha256"]) == 64
    assert status["rfq"] == "OFF"


def test_durable_only_never_loads_tagger_or_enters_research_publication(
        tmp_path, monkeypatch, capsys):
    tree = _fixture_tree(tmp_path)
    _pin_production(monkeypatch, tree)
    # The full daily unit has a different systemd credential mount.  Durable
    # mode must validate the durable unit path, never the full-unit constant.
    monkeypatch.setattr(
        daily, "DEFAULT_PUBLISHER_ENV_FILE",
        tree["publisher_env"].with_name("wrong-full-unit-publisher.env"))
    tree["tagger_creds"].unlink()

    def forbidden(*_args, **_kwargs):
        pytest.fail("durable-only entered the tagger/research publication path")

    monkeypatch.setattr(daily, "tagger_env", forbidden)
    monkeypatch.setattr(daily, "refresh_policy_patrol_evidence", forbidden)
    monkeypatch.setattr(daily, "refresh_layered_single_writer_audit", forbidden)
    monkeypatch.setattr(daily, "execute_date", forbidden)
    argv = _args(tree, "--operator-approved", "--durable-only")
    marker = argv.index("--tagger-credentials-file")
    del argv[marker:marker + 2]

    assert daily.main(argv) == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["state"] == "DURABLE_RECEIPT_READY"
    assert result["credential_mode"] == "PUBLISHER_ENV_ONLY"
    assert result["tagger_credentials_loaded"] is False
    assert result["tag_writes"] == 0
    assert result["research_manifest_writes"] == 0
    assert result["rfq"] == "OFF"
    status = json.loads((
        tree["live"] / "research_v3_daily" / ("date=" + DATE)
        / "DURABLE_STATUS.json").read_text())
    assert status == result


def test_existing_tagged_index_skips_tagger_but_rechecks_publisher(
        tmp_path, monkeypatch):
    tree = _fixture_tree(tmp_path, tagged=True)
    _pin_production(monkeypatch, tree)
    calls = []
    monkeypatch.setattr(
        daily, "_identity",
        lambda command, expected_arn, **_kwargs: calls.append(command) or {
            "Arn": expected_arn, "Account": "321572485933", "UserId": "U"})
    def fake_run(command, **_kwargs):
        calls.append(command)
        publisher = _reference_publisher_output(command)
        if publisher is not None:
            return publisher
        if "--verify-only" in command:
            proof = _make_daily_proof(tree["live"])
            return json.dumps({
                "state": "EXACT_VERSION_TAG_READBACK_VERIFIED",
                "proof": str(proof), "tag_puts": 0, "rfq": "OFF",
            }) + "\n"
        return ""

    monkeypatch.setattr(daily, "_run", fake_run)
    monkeypatch.setattr(
        daily, "refresh_policy_patrol_evidence",
        lambda *_a, **_kw: tree["evidence"])
    monkeypatch.setattr(
        daily, "refresh_layered_single_writer_audit",
        lambda *_a, **_kw: (tree["audit"], tree["evidence"]))
    assert daily.main(_args(tree, "--operator-approved")) == 0
    flat = "\n".join(" ".join(command) for command in calls)
    canonical = [command for command in calls
                 if "canonical_eligibility_tagger.py" in " ".join(command)]
    assert len(canonical) == 1
    assert "--verify-only" in canonical[0]
    assert "publish-reference" in flat
    prepare_position = next(index for index, command in enumerate(calls)
                            if "--prepare-only" in command)
    proof_position = next(index for index, command in enumerate(calls)
                          if "--verify-only" in command)
    commit_position = next(index for index, command in enumerate(calls)
                           if "--prepared-plan" in command)
    assert prepare_position < proof_position < commit_position
    assert proof_position + 1 == commit_position


def test_over_24h_pending_uses_historical_no_create_recovery(
        tmp_path, monkeypatch):
    tree = _fixture_tree(tmp_path)
    _pin_production(monkeypatch, tree)
    pending_root = (tree["live"] / "canonical_receipts" / "tagged"
                    / ("date=" + DATE))
    pending = _write_json(
        pending_root / ("TAGGED-PENDING-" + "b" * 64 + ".json"), {
            "schema_version": "canonical-tagged-receipt-pending-index-v1",
            "state": "TAGGED_RECEIPT_OBJECT_TAG_PENDING",
            "date": DATE,
            "receipt_set_sha256": "b" * 64,
            "byte_attestation_receipt_set_sha256": RECEIPT_SHA,
            "eligibility_single_writer_audit_sha256": tree["audit_sha"],
            "transaction_created_at_utc": "2000-01-01T00:00:00Z",
        })
    plan = daily.pending_recovery_plan(
        DATE, tree["durable_index"], tree["live"], tree["audit_root"],
        daily.DEFAULT_TAGGER_ARN)
    assert plan is not None
    assert plan.pending_index == pending
    assert plan.historical_existing_only is True

    calls = []
    monkeypatch.setattr(
        daily, "_identity",
        lambda command, expected_arn, **_kwargs: calls.append(command) or {
            "Arn": expected_arn, "Account": "321572485933", "UserId": "U"})

    def fake_run(command, **_kwargs):
        calls.append(command)
        publisher = _reference_publisher_output(command)
        if publisher is not None:
            return publisher
        if "--recover-existing-only" in command:
            tagged = _make_tagged(tree["live"], tree["audit_sha"])
            return json.dumps({
                "index": str(tagged),
                "recovery": "HISTORICAL_EXISTING_REMOTE_ONLY",
                "receipt_puts": 0, "rfq": "OFF",
            }) + "\n"
        if "--verify-only" in command:
            proof = _make_daily_proof(tree["live"])
            return json.dumps({
                "state": "EXACT_VERSION_TAG_READBACK_VERIFIED",
                "proof": str(proof), "tag_puts": 0, "rfq": "OFF",
            }) + "\n"
        return ""

    monkeypatch.setattr(daily, "_run", fake_run)
    monkeypatch.setattr(
        daily, "refresh_policy_patrol_evidence",
        lambda *_a, **_kw: tree["evidence"])
    assert daily.main(_args(tree, "--operator-approved")) == 0
    recovery = next(command for command in calls
                    if "--recover-existing-only" in command)
    assert ["--pending-index", str(pending)] == recovery[
        recovery.index("--pending-index"):recovery.index("--pending-index") + 2]
    assert "--include-sealed-rfq" not in recovery
    assert not any("canonical_receipt_control.py" in " ".join(command)
                   for command in calls)


def test_systemd_job_is_not_coupled_to_capture_or_seal():
    service = (ROOT / "deploy" / "kalshi-research-v3-daily.service").read_text()
    timer = (ROOT / "deploy" / "kalshi-research-v3-daily.timer").read_text()
    unit_block = service.split("[Service]", 1)[0]
    assert "kalshi-pipeline.service" not in "\n".join(
        line for line in unit_block.splitlines() if not line.startswith("#"))
    assert "systemctl" not in service
    assert "--scan-ready-days 35" in service
    assert "--raw-root /home/ubuntu/hft-bot/work/raw" in service
    assert "--warehouse-root /home/ubuntu/hft-bot/work/warehouse" in service
    assert "--publisher-env-file %d/publisher.env" in service
    assert "--aws-cli /snap/aws-cli/current/bin/aws" in service
    assert "--aws-cli /snap/bin/aws" not in service
    assert "Environment=GIT_CONFIG_SYSTEM=/etc/kalshi-research-v3/gitconfig" \
        in service
    assert "Environment=GIT_OPTIONAL_LOCKS=0" in service
    assert "ExecStartPre=/usr/bin/test -r /etc/kalshi-research-v3/gitconfig" \
        in service
    assert "/etc/kalshi-research-v3/gitconfig" in service.split(
        "ReadOnlyPaths=", 1)[1].splitlines()[0]
    assert "LoadCredentialEncrypted=publisher.env:" in service
    assert "LoadCredentialEncrypted=tagger.credentials:" in service
    assert "User=kalshi-research-v3" in service
    assert "SupplementaryGroups=kalshi-publication" in service
    assert "WorkingDirectory=/opt/kalshi-research-v3" in service
    assert "ExecCondition=/usr/bin/test -f /etc/kalshi-research-v3/approvals/" in service
    assert "ReadOnlyPaths=/opt/kalshi-research-v3" in service
    assert "--operator-approved" in service
    assert "Persistent=true" in timer
    assert "OnUnitInactiveSec=30min" in timer
    assert "MemoryMax=" in service
    assert "CPUQuota=" in service
    tmpfiles = (ROOT / "deploy"
                / "kalshi-research-v3-daily.tmpfiles.conf").read_text()
    assert "research_v3_daily" in tmpfiles
    assert "tag-precommit" in tmpfiles
    assert "tag-precommit" in service
    assert "forward-version-bindings" in tmpfiles
    assert "forward-version-bindings" in service
    installer = (ROOT / "deploy"
                 / "install_kalshi_research_v3_daily.sh").read_text()
    assert "setpriv --no-new-privs" in installer
    assert "/snap/aws-cli/current/bin/aws" in installer
    assert "--bounding-set" not in service
    assert "u:$SERVICE_USER:rwX" in installer
    assert "d:u:$SERVICE_USER:rwx" in installer
    assert "for lock_name in catalog dim" in installer
    assert "GIT_OPTIONAL_LOCKS=0" in installer
    assert "safe.directory" not in service
    assert "/work/warehouse/.publication-locks" in tmpfiles
    assert "/work/warehouse/.publication-locks 2770 ubuntu kalshi-publication" in tmpfiles
    assert "/work/warehouse/.publication-locks/catalog.lock 0660 ubuntu kalshi-publication" in tmpfiles
    assert "/work/warehouse/.publication-locks/dim.lock 0660 ubuntu kalshi-publication" in tmpfiles
    assert "WantedBy=multi-user.target" not in service


def test_installer_whitelists_final_durable_pairs_and_derives_arms_from_exact_authority():
    installer = (ROOT / "deploy"
                 / "install_kalshi_research_v3_daily.sh").read_text()
    authorization = (ROOT / "docs" / "plan_releases" / "pipeline"
                     / "W-PUB-REF-01C_AUTOMATION_EXECUTION_AUTHORIZATION_2026-07-17.json")
    raw = authorization.read_bytes()
    payload = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == "1b14001428f2387f3e62c531a8d8ce3dd4f8bd726d6c8b94b4d6c0d893020761"
    assert payload["schema_version"] == \
        "research-v3-automation-execution-authorization-v1"
    assert payload["state"] == \
        "OPERATOR_AUTHORIZED_ISOLATED_AUTOMATIC_EXECUTION"
    assert payload["authorization_scope"]["storage_mode"] == \
        "ZERO_COPY_EXACT_VERSION_REFERENCE"
    assert payload["authorization_scope"]["generation_scope"] == \
        ("canonical original-key generations and their small immutable "
         "witnesses; never a research-prefix data copy")
    allowed_mutations = payload["authorization_scope"][
        "allowed_aws_mutations"]
    assert any("publication-generation witness conditional creates" in item
               for item in allowed_mutations)
    assert any("generation-aware canonical catalog and dated-dim "
               "original-key synchronization" in item
               for item in allowed_mutations)
    assert "ec2/control/publication-generations/v1/" in \
        payload["authorization_scope"]["prefixes"]
    assert "no bulk canonical data copy into the research prefix" in \
        payload["prohibitions"]
    assert payload["rfq_scope"].startswith("OFF;")
    assert "no old damaged RFQ repair or reopening" in payload["prohibitions"]
    assert authorization.name in installer
    assert digest in installer
    assert 'for target_name in cutover-approved publish-approved' in installer
    assert 'install -o root -g root -m 0444 "$IMMUTABLE_AUTHORIZATION"' \
        in installer
    assert "IMPORT_OPERATOR_APPROVALS" not in installer
    assert "operator action remains" not in installer

    assert 'cp -a "$HISTORY_SOURCE"/.' not in installer
    assert "HISTORICAL_RECEIPT_SOURCE" not in installer
    assert ("HISTORY_SOURCE=/home/ubuntu/hft-bot/work/live/"
            "canonical_receipts") in installer
    assert "find -P \"$HISTORY_SOURCE/durable\"" in installer
    assert "-mindepth 2 -maxdepth 2" in installer
    assert "-name 'DURABLE-*.json'" in installer
    assert 'receipt_path="$(dirname -- "$index_path")/receipt-$digest.json"' \
        in installer
    assert "research_v3_daily as d; d._validate_durable_index" in installer
    assert ".verification-tmp" not in installer
    assert "forward-aux" not in installer.split(
        'mkdir -p "$STAGE/history/durable"', 1)[1].split(
            "# Freeze executable bits", 1)[0]


def test_daily_child_environments_preserve_only_fixed_git_read_config():
    env = daily._base_env(Path("/var/lib/kalshi-research-v3"))
    assert env["GIT_CONFIG_SYSTEM"] == "/etc/kalshi-research-v3/gitconfig"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env


def test_operator_arm_must_match_exact_automation_authorization(
        tmp_path, monkeypatch, capsys):
    tree = _fixture_tree(tmp_path)
    _pin_production(monkeypatch, tree)
    tree["publish_arm"].write_text("approved\n")
    assert daily.main(_args(tree, "--operator-approved")) == 2
    assert "does not match the authorized automation execution scope" in \
        capsys.readouterr().err


def test_late_correction_resolver_binds_original_key_without_any_s3_write(
        tmp_path, monkeypatch):
    payload = b"frozen-late-correction"
    expected = (len(payload), hashlib.sha256(payload).hexdigest())
    key = "ec2/warehouse/corrections/date=2026-07-14/late_rows.ndjson"
    row = {
        "bucket": daily.DEFAULT_BUCKET,
        "key": key,
        "logical_source_key":
            "warehouse/corrections/date=2026-07-14/late_rows.ndjson",
        "family": "corrections_at_cutoff",
        "size": expected[0],
        "sha256": expected[1],
    }
    commands = []
    def fake_publisher(command, **_kwargs):
        commands.append(command)
        if "head-object" in command:
            return json.dumps({
                "VersionId": "retained-version-1",
                "ContentLength": expected[0],
                "LastModified": "2026-07-17T12:00:00Z",
            })
        raise AssertionError(command)

    exact = []
    monkeypatch.setattr(daily, "_publisher_run", fake_publisher)
    monkeypatch.setattr(
        daily, "_exact_remote_catalog_bytes",
        lambda observed_key, version_id, _output, **_kwargs:
        exact.append((observed_key, version_id)) or expected)
    binding = daily._resolve_forward_exact_version(
        row, 0, tmp_path / "readback",
        args=SimpleNamespace(aws_cli="/fixture/aws"),
        publisher_environment={}, download_budget={"bytes": 0})

    assert not any("put-object" in command or "copy-object" in command
                   for command in commands)
    assert exact == [(key, "retained-version-1")]
    assert binding["VersionId"] == "retained-version-1"
    assert binding["key"] == key
    assert binding["resolution"] == "CURRENT_EXACT_SHA256_MATCH"
    assert binding["resolver_evidence"]["history_pages"] == 0


def _generation_witness_fixture():
    seal_sha = "1" * 64
    sealed_at = "2026-07-15T02:00:00Z"
    catalog_files = [{
        "relative_path": "catalog/" + rel,
        "size": len(rel.encode()),
        "sha256": hashlib.sha256(rel.encode()).hexdigest(),
    } for rel in cr.CATALOG_REQUIRED]
    catalog = pg.build_manifest("catalog", catalog_files)
    dim_files = [{
        "relative_path": "dim/snapshots/date=%s/%s" % (DATE, name),
        "size": len(name.encode()),
        "sha256": hashlib.sha256(name.encode()).hexdigest(),
    } for name in cr.DIM_REQUIRED]
    dim = pg.build_manifest(
        "dim", dim_files, date=DATE,
        source_catalog_generation_id=catalog["generation_id"])
    objects = []
    for row in catalog["files"] + dim["files"]:
        logical = "warehouse/" + row["relative_path"]
        objects.append({
            "logical_source_key": logical,
            "bucket": daily.DEFAULT_BUCKET,
            "key": daily.DEFAULT_PREFIX + "/" + logical,
            "VersionId": "member-version-1",
            "size": row["size"], "sha256": row["sha256"],
            "LastModified": "2026-07-15T02:01:00Z",
        })
    objects.sort(key=lambda row: row["logical_source_key"])
    seal = {
        "bucket": daily.DEFAULT_BUCKET,
        "key": "%s/warehouse/seals/date=%s.json" % (
            daily.DEFAULT_PREFIX, DATE),
        "VersionId": "seal-version-1", "size": 123,
        "sha256": seal_sha, "LastModified": sealed_at,
    }
    witness = fcr.build_generation_witness_payload(
        DATE, daily.DEFAULT_BUCKET, daily.DEFAULT_PREFIX, seal, catalog,
        dim, objects)
    key, raw = fcr.generation_witness_artifact(witness)
    descriptor = {
        "generation_witness_contract": fcr._generation_witness_contract({
            "sha256": seal_sha, "sealed_at": sealed_at,
        }, DATE, daily.DEFAULT_BUCKET, daily.DEFAULT_PREFIX),
    }
    return descriptor, {"sha256": seal_sha, "size": 123}, witness, key, raw


def test_generation_witness_discovery_is_exact_and_never_lists_history(
        tmp_path, monkeypatch):
    descriptor, seal_binding, witness, key, raw = \
        _generation_witness_fixture()
    commands = []

    def fake_publisher(command, **_kwargs):
        commands.append(command)
        if "list-objects-v2" in command:
            return json.dumps({
                "IsTruncated": False,
                "Contents": [{"Key": key, "Size": len(raw)}],
            }, indent=2)
        if "head-object" in command:
            return json.dumps({
                "VersionId": "witness-version-1",
                "ContentLength": len(raw),
                "LastModified": "2026-07-15T02:02:00Z",
            }, indent=2)
        raise AssertionError(command)

    def fake_exact(observed_key, version_id, output, **_kwargs):
        assert (observed_key, version_id) == (key, "witness-version-1")
        output.write_bytes(raw)
        return len(raw), hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(daily, "_publisher_run", fake_publisher)
    monkeypatch.setattr(daily, "_exact_remote_catalog_bytes", fake_exact)
    observed, envelope = daily._discover_exact_generation_witness(
        DATE, descriptor, seal_binding, tmp_path,
        canonical_receipts=cr, forward_receipts=fcr,
        args=SimpleNamespace(aws_cli="/fixture/aws"),
        publisher_environment={})

    assert observed == witness
    assert envelope["key"] == key
    assert envelope["VersionId"] == "witness-version-1"
    assert envelope["sha256"] == hashlib.sha256(raw).hexdigest()
    assert [command[2] for command in commands] == [
        "list-objects-v2", "head-object"]
    assert not any("list-object-versions" in command
                   or "put-object" in command or "copy-object" in command
                   for command in commands)


@pytest.mark.parametrize("listing", [
    {"IsTruncated": False, "Contents": []},
    {"IsTruncated": True, "Contents": []},
    {"IsTruncated": False, "Contents": [
        {"Key": "first", "Size": 1}, {"Key": "second", "Size": 1}]},
])
def test_generation_witness_discovery_blocks_missing_multiple_or_truncated(
        tmp_path, monkeypatch, listing):
    descriptor, seal_binding, _witness, _key, _raw = \
        _generation_witness_fixture()
    monkeypatch.setattr(
        daily, "_publisher_run", lambda *_a, **_kw: json.dumps(listing))
    monkeypatch.setattr(
        daily, "_exact_remote_catalog_bytes",
        lambda *_a, **_kw: pytest.fail("invalid listing must block before GET"))
    with pytest.raises(daily.GateError, match="exactly one"):
        daily._discover_exact_generation_witness(
            DATE, descriptor, seal_binding, tmp_path,
            canonical_receipts=cr, forward_receipts=fcr,
            args=SimpleNamespace(aws_cli="/fixture/aws"),
            publisher_environment={})


def test_generation_witness_discovery_rejects_noncanonical_wire_bytes(
        tmp_path, monkeypatch):
    descriptor, seal_binding, witness, _key, _raw = \
        _generation_witness_fixture()
    raw = (json.dumps(witness, sort_keys=True, indent=2) + "\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    key = fcr.expected_generation_witness_key(
        daily.DEFAULT_PREFIX, DATE, digest)

    def fake_publisher(command, **_kwargs):
        if "list-objects-v2" in command:
            return json.dumps({
                "IsTruncated": False,
                "Contents": [{"Key": key, "Size": len(raw)}],
            })
        return json.dumps({
            "VersionId": "witness-version-1",
            "ContentLength": len(raw),
            "LastModified": "2026-07-15T02:02:00Z",
        })

    def fake_exact(_key, _version, output, **_kwargs):
        output.write_bytes(raw)
        return len(raw), digest

    monkeypatch.setattr(daily, "_publisher_run", fake_publisher)
    monkeypatch.setattr(daily, "_exact_remote_catalog_bytes", fake_exact)
    with pytest.raises(daily.GateError, match="wire/key form"):
        daily._discover_exact_generation_witness(
            DATE, descriptor, seal_binding, tmp_path,
            canonical_receipts=cr, forward_receipts=fcr,
            args=SimpleNamespace(aws_cli="/fixture/aws"),
            publisher_environment={})


def test_exact_get_response_must_bind_requested_version_and_size(
        tmp_path, monkeypatch):
    output = tmp_path / "exact.bin"
    payload = b"exact-version-bytes"

    def fake_publisher(command, **_kwargs):
        Path(command[-1]).write_bytes(payload)
        return json.dumps({
            "VersionId": "wrong-version",
            "ContentLength": len(payload),
        })

    monkeypatch.setattr(daily, "_publisher_run", fake_publisher)
    with pytest.raises(daily.GateError, match="does not bind"):
        daily._exact_remote_catalog_bytes(
            "ec2/warehouse/catalog/series/part-00000.parquet", "wanted-v1",
            output, args=SimpleNamespace(aws_cli="/fixture/aws"),
            publisher_environment={})


def test_record_attempt_failure_preserves_malformed_status(tmp_path):
    status = (tmp_path / "research_v3_daily" / ("date=" + DATE)
              / ("receipt=" + RECEIPT_SHA) / "STATUS.json")
    status.parent.mkdir(parents=True)
    original = b'{"state":"V3_REFERENCE_PUBLISHED"'
    status.write_bytes(original)
    failure = {
        "schema_version": "research-v3-daily-status-v1",
        "state": "BLOCKED",
        "date": DATE,
        "code": "FIXTURE_FAILURE",
        "detail": "malformed prior status",
        "rfq": "OFF",
        "failed_at_utc": "2026-07-17T00:00:00Z",
    }

    attempt = daily._record_attempt_failure(status, failure)

    assert status.read_bytes() == original
    assert json.loads(attempt.read_text()) == failure
    assert attempt.parent == status.parent / "attempts"


def test_discovery_retries_unauthenticated_terminal_status(tmp_path):
    tree = _fixture_tree(tmp_path)
    status = (tree["live"] / "research_v3_daily" / ("date=" + DATE)
              / ("receipt=" + RECEIPT_SHA) / "STATUS.json")
    _write_json(status, {"state": "V3_REFERENCE_PUBLISHED"})
    failures = []

    assert daily.discover_unfinished_dates(tree["live"], failures) == [DATE]
    assert any(row["code"] == "TERMINAL_STATE_INVALID"
               for row in failures)


def test_discovery_retries_locally_bound_terminal_for_remote_authentication(
        tmp_path):
    tree = _fixture_tree(tmp_path, tagged=True)
    status = (tree["live"] / "research_v3_daily" / ("date=" + DATE)
              / ("receipt=" + RECEIPT_SHA) / "STATUS.json")
    _write_json(status, {
        "schema_version": daily.TERMINAL_STATUS_SCHEMA,
        "state": "V3_REFERENCE_PUBLISHED",
        "date": DATE,
        "durable_receipt_set_sha256": RECEIPT_SHA,
        "tagged_index": str(tree["tagged_index"].absolute()),
        "destination": daily.DEFAULT_DEST,
        "live_dir": str(tree["live"].absolute()),
        "release_id": RELEASE_ID,
        "prepared_plan_sha256": "d" * 64,
        "manifest_commit_state": "REFERENCE_MANIFEST_COMMITTED",
        "manifest_object": {
            "bucket": daily.DEFAULT_BUCKET,
            "key": f"research/releases/{RELEASE_ID}/MANIFEST.json",
            "VersionId": "manifest-version-1",
            "size": len(MANIFEST_BYTES),
            "sha256": hashlib.sha256(MANIFEST_BYTES).hexdigest(),
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        },
        "rfq": "OFF",
        "completed_at_utc": "2026-07-17T00:00:00Z",
    })
    failures = []

    assert daily.discover_unfinished_dates(tree["live"], failures) == [DATE]
    assert failures == []


@pytest.mark.parametrize("tamper", [False, True])
def test_terminal_retry_exact_gets_and_hashes_recorded_manifest(
        tmp_path, monkeypatch, tamper):
    tree = _fixture_tree(tmp_path, tagged=True)
    status_path = (tree["live"] / "research_v3_daily" / ("date=" + DATE)
                   / ("receipt=" + RECEIPT_SHA) / "STATUS.json")
    status = {
        "schema_version": daily.TERMINAL_STATUS_SCHEMA,
        "state": "V3_REFERENCE_PUBLISHED",
        "date": DATE,
        "durable_receipt_set_sha256": RECEIPT_SHA,
        "tagged_index": str(tree["tagged_index"].absolute()),
        "destination": daily.DEFAULT_DEST,
        "live_dir": str(tree["live"].absolute()),
        "release_id": RELEASE_ID,
        "prepared_plan_sha256": "d" * 64,
        "manifest_commit_state": "REFERENCE_MANIFEST_COMMITTED",
        "manifest_object": {
            "bucket": daily.DEFAULT_BUCKET,
            "key": f"research/releases/{RELEASE_ID}/MANIFEST.json",
            "VersionId": "manifest-version-1",
            "size": len(MANIFEST_BYTES),
            "sha256": hashlib.sha256(MANIFEST_BYTES).hexdigest(),
            "verification_state": "EXACT_VERSION_FULL_SHA256",
        },
        "rfq": "OFF",
        "completed_at_utc": "2026-07-17T00:00:00Z",
    }
    _write_json(status_path, status)
    commands = []

    def fake_publisher(command, **_kwargs):
        commands.append(command)
        if "head-object" in command:
            return json.dumps({
                "VersionId": "manifest-version-1",
                "ContentLength": len(MANIFEST_BYTES),
            })
        if "get-object" in command:
            payload = (b"X" + MANIFEST_BYTES[1:]) if tamper else MANIFEST_BYTES
            Path(command[-1]).write_bytes(payload)
            return json.dumps({
                "VersionId": "manifest-version-1",
                "ContentLength": len(payload),
            })
        raise AssertionError(command)

    tagged_index = json.loads(tree["tagged_index"].read_text())
    receipt = tagged_index["receipt_object"]
    monkeypatch.setattr(daily, "_publisher_run", fake_publisher)
    monkeypatch.setattr(reference, "validate_manifest", lambda *_a, **_kw: {
        "date": DATE,
        "release_id": RELEASE_ID,
        "rfq_included": False,
        "canonical_receipt_set_sha256":
            tagged_index["receipt_set_sha256"],
        "receipt_object": {
            "bucket": receipt["bucket"],
            "key": receipt["key"],
            "version_id": receipt["VersionId"],
            "size": receipt["size"],
            "sha256": receipt["sha256"],
        },
    })

    if tamper:
        with pytest.raises(daily.GateError, match="exact GET bytes"):
            daily._verify_terminal_status(
                status, status_path, DATE, RECEIPT_SHA, tree["live"],
                tree["audit_root"], daily.DEFAULT_TAGGER_ARN,
                args=SimpleNamespace(aws_cli="/fixture/aws"),
                publisher_environment={})
    else:
        tagged, binding = daily._verify_terminal_status(
            status, status_path, DATE, RECEIPT_SHA, tree["live"],
            tree["audit_root"], daily.DEFAULT_TAGGER_ARN,
            args=SimpleNamespace(aws_cli="/fixture/aws"),
            publisher_environment={})
        assert tagged == tree["tagged_index"].absolute()
        assert binding == status["manifest_object"]
        assert [command[2] for command in commands] == [
            "head-object", "get-object"]
        assert all("--version-id" in command for command in commands)


def test_verify_seal_routes_only_legacy_dates_to_historical_authority(
        tmp_path, monkeypatch):
    args = SimpleNamespace(
        python=str(Path(sys.executable).absolute()),
        raw_root=str(tmp_path / "raw"),
        warehouse_root=str(tmp_path / "warehouse"),
        quality_dir=str(tmp_path / "event_packs"),
        home=str(tmp_path / "home"),
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return ""

    monkeypatch.setattr(daily, "_run", fake_run)
    daily._verify_seal("2026-07-10", args)
    daily._verify_seal("2026-07-17", args)

    legacy, future = calls
    assert "verify-historical-authority" in legacy[0]
    assert str(ROOT / "tools" / "forward_canonical_receipts.py") \
        in legacy[0]
    assert "--quality-dir" in legacy[0]
    assert "--verify-seal" in future[0]
    assert str(ROOT / "tools" / "export_day.py") in future[0]
    assert "verify-historical-authority" not in future[0]
