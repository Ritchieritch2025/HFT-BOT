import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "seal_backlog.py"


def _paths(tmp_path):
    raw = tmp_path / "raw"
    seals = tmp_path / "seals"
    live = tmp_path / "live"
    raw.mkdir()
    seals.mkdir()
    live.mkdir()
    return {
        "raw": raw,
        "seals": seals,
        "state": live / "seal_backlog_state.json",
        "alarm": live / "seal_backlog_alarm.json",
    }


def _raw_day(paths, day, *, firehose=True):
    directory = paths["raw"] / f"date={day}"
    directory.mkdir()
    if firehose:
        (directory / "firehose_00.ndjson").write_text("{}\n")
    return directory


def _seal(paths, day):
    (paths["seals"] / f"date={day}.json").write_text("{}\n")


def _run(paths, command, *extra, now="2026-07-18T12:00:00Z", check=True):
    argv = [
        sys.executable,
        str(TOOL),
        command,
        "--raw-root",
        str(paths["raw"]),
        "--seal-root",
        str(paths["seals"]),
        "--state",
        str(paths["state"]),
        "--alarm",
        str(paths["alarm"]),
        "--not-before",
        "2026-07-10",
        "--lookback-days",
        "35",
        "--now",
        now,
        *extra,
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def _claim(paths, *extra, now="2026-07-18T12:00:00Z"):
    defaults = (
        "--max-per-cycle",
        "1",
        "--max-per-day",
        "4",
        "--lease-seconds",
        "7200",
    )
    return _run(paths, "claim", *(defaults + extra), now=now)


def _record(paths, day, result, *, error=None, now="2026-07-18T12:05:00Z"):
    extra = [
        "--date",
        day,
        "--result",
        result,
        "--retry-base-seconds",
        "3600",
        "--retry-max-seconds",
        "21600",
    ]
    if error is not None:
        extra.extend(("--error", error))
    return _run(paths, "record", *extra, now=now)


def test_claim_only_authorized_completed_unsealed_days_and_oldest_first(tmp_path):
    paths = _paths(tmp_path)
    for day in ("2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12",
                "2026-07-17", "2026-07-18"):
        _raw_day(paths, day)
    _seal(paths, "2026-07-11")

    result = _claim(paths)
    assert result.stdout.splitlines() == ["2026-07-10"]

    state = json.loads(paths["state"].read_text())
    assert "2026-07-09" not in state["days"]  # before explicit authorization
    assert "2026-07-17" not in state["days"]  # yesterday: normal path owns it
    assert "2026-07-18" not in state["days"]  # current UTC date is incomplete
    assert state["days"]["2026-07-10"]["status"] == "CLAIMED"
    assert state["days"]["2026-07-11"]["status"] == "SEALED"
    assert state["days"]["2026-07-12"]["status"] == "MISSING_SEAL"


def test_claim_is_leased_and_bounded_per_cycle_and_utc_day(tmp_path):
    paths = _paths(tmp_path)
    for day in ("2026-07-10", "2026-07-11", "2026-07-12"):
        _raw_day(paths, day)

    limited = (
        "--max-per-cycle",
        "1",
        "--max-per-day",
        "2",
        "--lease-seconds",
        "7200",
    )
    first = _run(paths, "claim", *limited)
    second = _run(paths, "claim", *limited)
    third = _run(paths, "claim", *limited)
    assert first.stdout.splitlines() == ["2026-07-10"]
    assert second.stdout.splitlines() == ["2026-07-11"]
    assert third.stdout == ""

    state = json.loads(paths["state"].read_text())
    assert state["claims_by_utc_day"] == {"2026-07-18": 2}
    assert state["days"]["2026-07-10"]["attempts_total"] == 1
    assert state["days"]["2026-07-12"]["attempts_total"] == 0


def test_failure_error_and_backoff_are_durable(tmp_path):
    paths = _paths(tmp_path)
    _raw_day(paths, "2026-07-10")
    assert _claim(paths, now="2026-07-18T00:00:00Z").stdout.strip() == "2026-07-10"

    _record(
        paths,
        "2026-07-10",
        "failed",
        error="run_seal_chain rc=1; see export.log",
        now="2026-07-18T00:01:00Z",
    )
    state = json.loads(paths["state"].read_text())
    entry = state["days"]["2026-07-10"]
    assert entry["status"] == "RETRY_WAIT"
    assert entry["last_error"] == "run_seal_chain rc=1; see export.log"
    assert entry["next_retry_utc"] == "2026-07-18T01:01:00Z"

    assert _claim(paths, now="2026-07-18T00:59:00Z").stdout == ""
    assert _claim(paths, now="2026-07-18T01:02:00Z").stdout.strip() == "2026-07-10"


def test_newer_success_cannot_clear_older_unresolved_alarm(tmp_path):
    paths = _paths(tmp_path)
    _raw_day(paths, "2026-07-10")
    _raw_day(paths, "2026-07-11")
    batch = _run(
        paths,
        "claim",
        "--max-per-cycle",
        "2",
        "--max-per-day",
        "4",
        "--lease-seconds",
        "7200",
    )
    assert batch.stdout.splitlines() == ["2026-07-10", "2026-07-11"]

    _record(paths, "2026-07-10", "failed", error="older day failed")
    _seal(paths, "2026-07-11")
    _record(paths, "2026-07-11", "succeeded")

    alarm = json.loads(paths["alarm"].read_text())
    assert alarm["status"] == "UNSEALED_BACKLOG"
    assert alarm["unresolved_dates"] == ["2026-07-10"]
    assert alarm["days"][0]["last_error"] == "older day failed"
    state = json.loads(paths["state"].read_text())
    assert state["days"]["2026-07-11"]["status"] == "SEALED"


def test_reported_success_without_exact_seal_fails_closed(tmp_path):
    paths = _paths(tmp_path)
    _raw_day(paths, "2026-07-10")
    _claim(paths)
    _record(paths, "2026-07-10", "succeeded")

    entry = json.loads(paths["state"].read_text())["days"]["2026-07-10"]
    assert entry["status"] == "RETRY_WAIT"
    assert entry["last_error"] == "SUCCESS_REPORTED_WITHOUT_EXACT_SEAL"
    assert paths["alarm"].exists()


def test_raw_disappearance_stays_alarm_visible(tmp_path):
    paths = _paths(tmp_path)
    day_dir = _raw_day(paths, "2026-07-10")
    _claim(paths)
    shutil.rmtree(day_dir)
    _run(paths, "status", now="2026-07-18T15:00:00Z")

    entry = json.loads(paths["state"].read_text())["days"]["2026-07-10"]
    assert entry["status"] == "RAW_MISSING"
    alarm = json.loads(paths["alarm"].read_text())
    assert alarm["unresolved_dates"] == ["2026-07-10"]


def test_corrupt_state_is_never_silently_replaced(tmp_path):
    paths = _paths(tmp_path)
    _raw_day(paths, "2026-07-10")
    paths["state"].write_text("not-json\n")
    before = paths["state"].read_bytes()

    result = _run(
        paths,
        "claim",
        "--max-per-cycle",
        "1",
        "--max-per-day",
        "4",
        "--lease-seconds",
        "7200",
        check=False,
    )
    assert result.returncode == 2
    assert "refusing to replace unreadable state" in result.stderr
    assert paths["state"].read_bytes() == before


def test_supervisor_backlog_wiring_preserves_normal_priority_and_bounds():
    text = (ROOT / "tools" / "pipeline_supervisor.sh").read_text()
    live = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )

    # Audited production defaults: explicit authorization lower bound, one
    # sequential historical date per cycle and four leases per UTC day.
    assert 'SEAL_BACKLOG_NOT_BEFORE_REQUESTED="${SEAL_BACKLOG_NOT_BEFORE:-2026-07-10}"' in live
    assert 'SEAL_BACKLOG_MAX_PER_CYCLE_REQUESTED="${SEAL_BACKLOG_MAX_PER_CYCLE:-1}"' in live
    assert 'SEAL_BACKLOG_MAX_PER_UTC_DAY_REQUESTED="${SEAL_BACKLOG_MAX_PER_UTC_DAY:-4}"' in live
    assert live.index("SEAL_BACKLOG_ENABLED_REQUESTED=") < live.index('source "$CREDS"')
    assert live.index('source "$CREDS"') < live.index(
        'SEAL_BACKLOG_ENABLED="$SEAL_BACKLOG_ENABLED_REQUESTED"'
    )

    wrapper = live[
        live.index("run_normal_and_seal_backlog()") : live.index("\nwhile true; do")
    ]
    assert wrapper.index('run_seal_chain "$normal_date"') \
        < wrapper.index('-f "$SEAL_BACKLOG_DISABLE"') \
        < wrapper.index('normal_day_ready_for_backlog "$normal_date"') \
        < wrapper.index("claim_seal_backlog") \
        < wrapper.index('run_seal_backlog_batch "$backlog_dates"')

    batch = live[
        live.index("run_seal_backlog_batch()") : live.index(
            "run_normal_and_seal_backlog()"
        )
    ]
    assert "for backlog_date in $backlog_dates" in batch
    assert batch.index('run_seal_chain "$backlog_date"') \
        < batch.index('--verify-seal') \
        < batch.index('record_seal_backlog "$backlog_date" "succeeded"')

    main = live[live.index("\nwhile true; do") :]
    launch = 'run_normal_and_seal_backlog "$YESTERDAY" &'
    assert launch in main
    assert main.index(launch) < main.index("./build/ws_shadow")
    assert "SEAL_PID=$!" in main

    service = (ROOT / "deploy" / "kalshi-pipeline.service").read_text()
    for setting in (
        "Environment=SEAL_BACKLOG_ENABLED=1",
        "Environment=SEAL_BACKLOG_NOT_BEFORE=2026-07-10",
        "Environment=SEAL_BACKLOG_LOOKBACK_DAYS=35",
        "Environment=SEAL_BACKLOG_MAX_PER_CYCLE=1",
        "Environment=SEAL_BACKLOG_MAX_PER_UTC_DAY=4",
    ):
        assert setting in service
    assert "seal_backlog_alarm.json" in (ROOT / "tools" / "ec2_health.sh").read_text()
    assert "seal_backlog_alarm.json" in (ROOT / "tools" / "ec2_monitor.sh").read_text()
    assert "seal_backlog_disable" in (ROOT / "tools" / "ec2_health.sh").read_text()
    assert "seal_backlog_disable" in (ROOT / "tools" / "ec2_monitor.sh").read_text()


def test_supervisor_never_clears_a_different_dates_legacy_alarm():
    text = (ROOT / "tools" / "pipeline_supervisor.sh").read_text()
    live = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    chain = live[live.index("run_seal_chain()") : live.index(
        "normal_day_ready_for_backlog()"
    )]
    assert 'rm -f "$SEAL_ALARM"' not in live
    assert chain.count('clear_seal_alarm_for_date "$CHAIN_DATE"') == 2
    clear = live[live.index("clear_seal_alarm_for_date()") : live.index(
        "seal_chain_active()"
    )]
    assert 'alarm.get("date") == day' in clear
    assert "os.unlink(path)" in clear
