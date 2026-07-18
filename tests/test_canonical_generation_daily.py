import datetime as dt
import json
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import canonical_generation_daily as daily  # noqa: E402


def _result(date):
    digest = "a" * 64
    return {
        "state": "PRODUCER_GENERATION_WITNESS_READY",
        "date": date,
        "generation_authority": "PRODUCER_COHERENT",
        "catalog_dim_coherence_claim": True,
        "rfq": "OFF",
        "publisher_code_commit": "b" * 40,
        "authorization_sha256": daily.AUTHORIZATION_SHA256,
        "witness_object": {
            "bucket": "kalshi-vault-ritcardo",
            "key": (
                "ec2/control/publication-generations/v1/"
                f"date={date}/witness-{digest}.json"),
            "VersionId": "exact-version-1",
            "size": 123,
            "sha256": digest,
        },
    }


def _credential(tmp_path, monkeypatch, payload):
    parent = tmp_path / "credentials"
    parent.mkdir(mode=0o700)
    path = parent / "publisher.env"
    path.write_text(payload)
    path.chmod(0o600)
    monkeypatch.setattr(daily, "PUBLISHER_ENV_FILE", path)
    return path


def test_publisher_credential_is_parsed_as_data_into_sterile_allowlist(
        tmp_path, monkeypatch):
    path = _credential(
        tmp_path, monkeypatch,
        "export AWS_ACCESS_KEY_ID='access'\n"
        "AWS_SECRET_ACCESS_KEY='secret'\n"
        "KALSHI_TRADING_SECRET='ignored'\n")

    environment = daily.publisher_environment(path)

    assert environment["AWS_ACCESS_KEY_ID"] == "access"
    assert environment["AWS_SECRET_ACCESS_KEY"] == "secret"
    assert "KALSHI_TRADING_SECRET" not in environment
    assert environment["AWS_CONFIG_FILE"] == "/dev/null"
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert environment["AWS_EC2_METADATA_DISABLED"] == "true"
    assert "AWS_PROFILE" not in environment


@pytest.mark.parametrize("line", [
    "AWS_ENDPOINT_URL=https://attacker.invalid\n",
    "AWS_ACCESS_KEY_ID=$(id)\n",
    "AWS_SECRET_ACCESS_KEY=`id`\n",
    "source /tmp/credential\n",
])
def test_publisher_credential_rejects_commands_expansions_and_forbidden_aws(
        tmp_path, monkeypatch, line):
    path = _credential(
        tmp_path, monkeypatch,
        "AWS_ACCESS_KEY_ID=access\nAWS_SECRET_ACCESS_KEY=secret\n" + line)
    with pytest.raises(daily.DailyWitnessError):
        daily.publisher_environment(path)


def test_eligible_dates_scan_sealed_future_manifests_only(
        tmp_path, monkeypatch):
    warehouse = tmp_path / "warehouse"
    manifests = warehouse / ".publication-generations" / "dim"
    seals = warehouse / "seals"
    manifests.mkdir(parents=True)
    seals.mkdir()
    for date in ("2026-07-16", "2026-07-17", "2026-07-18",
                 "2026-07-19"):
        (manifests / f"date={date}.json").write_text("{}\n")
    for date in ("2026-07-17", "2026-07-19"):
        (seals / f"date={date}.json").write_text("{}\n")
    monkeypatch.setattr(daily, "WAREHOUSE_ROOT", warehouse)
    monkeypatch.setattr(daily, "DIM_MANIFEST_ROOT", manifests)

    assert daily.eligible_dates(dt.date(2026, 7, 19)) == ["2026-07-17"]


def test_one_date_failure_does_not_prevent_later_date_completion(
        tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        daily, "publisher_environment",
        lambda _path, **_kwargs: {"AWS": "isolated"})
    monkeypatch.setattr(
        daily, "eligible_dates",
        lambda _today=None: ["2026-07-17", "2026-07-18"])

    def run_date(date, _environment):
        if date == "2026-07-17":
            raise daily.DailyWitnessError("TEST_FAILURE", date)
        return _result(date)

    monkeypatch.setattr(daily, "_run_date", run_date)
    with pytest.raises(
            daily.DailyWitnessError, match="DAILY_PARTIAL_FAILURE") as raised:
        daily.run(credential_file=Path("unused"))

    receipt = json.loads(raised.value.detail)
    assert receipt["failures"][0]["date"] == "2026-07-17"
    assert receipt["completed"][0]["date"] == "2026-07-18"
    marker = daily.STATE_ROOT / "date=2026-07-18.json"
    marker_payload = json.loads(marker.read_text())
    assert marker_payload["state"] == \
        "LOCAL_SCHEDULER_CACHE_NOT_REMOTE_AUTHORITY"
    assert marker_payload["result"] == _result("2026-07-18")
    assert os.stat(marker).st_mode & 0o777 == 0o600


def test_terminal_marker_makes_periodic_retry_local_and_idempotent(
        tmp_path, monkeypatch):
    date = "2026-07-17"
    monkeypatch.setattr(daily, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        daily, "publisher_environment",
        lambda _path, **_kwargs: {"AWS": "isolated"})
    monkeypatch.setattr(daily, "eligible_dates", lambda _today=None: [date])
    daily._write_terminal_marker(date, _result(date))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("terminal date must not launch another child")

    monkeypatch.setattr(daily, "_run_date", forbidden)
    result = daily.run(credential_file=Path("unused"))

    assert result["state"] == "COMPLETE"
    assert result["already_terminal"] == [date]
    assert result["completed"] == []
    assert result["rfq"] == "OFF"
    assert result["copy_operations"] == 0
    assert result["tag_writes"] == 0
    assert result["research_computations"] == 0


def test_recent_terminal_marker_is_exactly_rechecked_after_short_ttl(
        tmp_path, monkeypatch):
    date = "2026-07-17"
    monkeypatch.setattr(daily, "STATE_ROOT", tmp_path / "state")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    marker_path = daily._write_terminal_marker(date, _result(date), now=old)
    marker = daily._read_terminal_marker(date)

    assert marker_path.is_file()
    assert daily._marker_needs_recheck(
        marker, date, dt.date(2026, 7, 20),
        dt.datetime.now(dt.timezone.utc)) is True
    assert marker["state"] == "LOCAL_SCHEDULER_CACHE_NOT_REMOTE_AUTHORITY"


def test_terminal_result_rejects_non_exact_or_rfq_payload():
    payload = _result("2026-07-17")
    assert daily._terminal_result(payload, "2026-07-17") is True
    payload["rfq"] = "ON"
    assert daily._terminal_result(payload, "2026-07-17") is False


def test_legacy_runner_is_bounded_and_uses_same_non_shell_credential_path(
        monkeypatch):
    date = "2026-07-15"
    payload = _result(date)
    payload.update({
        "state": "LEGACY_GENERATION_WITNESS_READY",
        "generation_authority": "LEGACY_MIGRATION_GENERATION",
        "catalog_dim_coherence_claim": False,
        "original_object_puts": 0,
        "research_prefix_writes": 0,
        "copy_operations": 0,
        "tag_writes": 0,
        "history_proof_sha256": "d" * 64,
        "historical_metadata_authority_sha256": "e" * 64,
    })
    monkeypatch.setattr(
        daily, "publisher_environment",
        lambda _path, **_kwargs: {"AWS": "isolated"})
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr(daily.subprocess, "run", run)
    result = daily.run_legacy(date, credential_file=Path("unused"))

    assert result == payload
    assert "migrate-legacy" in observed["command"]
    assert observed["command"][observed["command"].index("--date") + 1] \
        == date
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["env"] == {"AWS": "isolated"}
    invalid = dict(payload)
    invalid["historical_metadata_authority_sha256"] = "not-a-sha"
    assert daily._legacy_result(invalid, date) is False
    invalid.pop("historical_metadata_authority_sha256")
    assert daily._legacy_result(invalid, date) is False
    with pytest.raises(daily.DailyWitnessError, match="LEGACY_DATE_FORBIDDEN"):
        daily.run_legacy("2026-07-14", credential_file=Path("unused"))
