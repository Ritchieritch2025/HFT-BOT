"""W-A5 ③ / rider (e) D4 test: deploy/alert_notify.sh state machine, offline
(ALERT_DRY=1 prints instead of POSTing; fake HOME with fixture files)."""
import json
import os
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "deploy", "alert_notify.sh")


def _run(home, extra_env=None):
    env = {**os.environ, "HOME": str(home), "ALERT_DRY": "1",
           "FRESH_LIMIT_S": "99999", "DISK_LIMIT_PCT": "101"}
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.update(extra_env or {})
    return subprocess.run(["bash", SCRIPT], env=env,
                          capture_output=True, text=True)


def _mk(home, status):
    live = home / "hft-bot" / "work" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "capture_alert.json").write_text(json.dumps({"status": status}))
    return live


def test_ok_state_is_silent(tmp_path):
    _mk(tmp_path, "ok")
    r = _run(tmp_path)
    assert r.returncode == 0 and "DRY-NOTIFY" not in r.stdout


def test_alert_fires_once_then_suppressed(tmp_path):
    _mk(tmp_path, "gap")
    r1 = _run(tmp_path)
    assert "DRY-NOTIFY" in r1.stdout and "capture:gap" in r1.stdout
    r2 = _run(tmp_path)  # same bad state, within re-alert window: suppressed
    assert "DRY-NOTIFY" not in r2.stdout


def test_recovery_notifies(tmp_path):
    live = _mk(tmp_path, "gap")
    _run(tmp_path)
    (live / "capture_alert.json").write_text(json.dumps({"status": "ok"}))
    r = _run(tmp_path)
    assert "RECOVERED" in r.stdout


def test_disk_threshold_env(tmp_path):
    _mk(tmp_path, "ok")
    r = _run(tmp_path, {"DISK_LIMIT_PCT": "0"})  # any usage trips it
    assert "DRY-NOTIFY" in r.stdout and "disk:" in r.stdout


def test_alerts_log_is_written(tmp_path):
    _mk(tmp_path, "gap")
    _run(tmp_path)
    log = tmp_path / "hft-bot" / "work" / "live" / "alerts.log"
    assert log.exists() and "ALERT" in log.read_text()
