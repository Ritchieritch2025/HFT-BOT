#!/usr/bin/env python3
"""Fail-safe 30-minute idle shutdown guard for PIPE-W09.

An instance is busy when provisioning is active, an SSH TCP/session exists,
or a shutdown inhibitor exists.  Every sensor failure is treated as busy.
The clock is Linux monotonic uptime, so UTC/NTP corrections cannot cause an
early shutdown.  Research jobs detached from SSH must run through ``w09-run``
which installs a systemd shutdown inhibitor for their lifetime.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from typing import Optional


IDLE_SECONDS = 1800
RUN_DIR = "/run/w09-idle"
STATE_DIR = "/var/lib/w09-idle"
IDLE_STATE = os.path.join(RUN_DIR, "idle_since.json")
LOCK_PATH = os.path.join(RUN_DIR, "check.lock")
EVENT_LOG = os.path.join(STATE_DIR, "events.jsonl")
PROVISIONING_MARKER = "/run/w09-idle-provisioning"
DISABLE_MARKER = "/etc/w09-idle.disabled"


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def boot_id() -> str:
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
        return handle.read().strip()


def uptime_seconds() -> float:
    with open("/proc/uptime", encoding="ascii") as handle:
        return float(handle.read().split()[0])


class SensorError(RuntimeError):
    """A named sensor command failed outside its documented empty result."""

    def __init__(self, args, failure_kind: str, returncode=None, errno=None):
        # Log only an allowlisted executable name and numeric status. Sensor
        # output can contain process arguments/session metadata and is never
        # persisted.
        self.sensor = os.path.basename(args[0])
        self.failure_kind = failure_kind
        self.returncode = returncode
        self.errno = errno
        super().__init__(self.sensor)


LAST_SENSOR_ERROR = None


def run_sensor(args, allowed_empty_returncodes=()):
    try:
        result = subprocess.run(
            args, check=False, text=True, capture_output=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        raise SensorError(args, "timeout") from None
    except OSError as exc:
        raise SensorError(args, "os-error", errno=exc.errno) from None
    if result.returncode:
        # procps `ps -u USER ...` returns rc=1 with no output when USER owns no
        # processes.  That is the desired idle result, not a failed sensor.
        # Only a caller that explicitly names the return code may accept it,
        # and any stdout/stderr still fails closed.
        if (result.returncode in allowed_empty_returncodes and
                not result.stdout.strip() and not result.stderr.strip()):
            return ""
        raise SensorError(args, "nonzero-exit",
                          returncode=result.returncode)
    return result.stdout


def busy_reason() -> Optional[str]:
    global LAST_SENSOR_ERROR
    LAST_SENSOR_ERROR = None
    if os.path.exists(DISABLE_MARKER):
        return "disabled"
    if os.path.exists(PROVISIONING_MARKER):
        return "provisioning"
    try:
        sockets = run_sensor([
            "/usr/bin/ss", "-Htn", "state", "established",
            "sport", "=", ":22",
        ])
        if sockets.strip():
            return "ssh-tcp"

        sessions = run_sensor([
            "/usr/bin/loginctl", "list-sessions", "--no-legend",
            "--no-pager",
        ])
        for line in sessions.splitlines():
            fields = line.split()
            if not fields:
                continue
            detail = run_sensor([
                "/usr/bin/loginctl", "show-session", fields[0],
                "--property=Remote", "--property=State", "--no-pager",
            ])
            props = dict(
                item.split("=", 1) for item in detail.splitlines()
                if "=" in item
            )
            if (props.get("Remote") == "yes" and
                    props.get("State") in ("active", "online")):
                return "ssh-session"

        # Any process owned by the research account is work. This protects
        # detached Python/DuckDB/tmux jobs even if somebody forgot w09-run.
        workloads = run_sensor(
            ["/usr/bin/ps", "-u", "ubuntu", "-o", "pid=,comm=,args="],
            allowed_empty_returncodes=(1,),
        )
        if workloads.strip():
            return "user-workload"

        inhibitors = run_sensor([
            "/usr/bin/systemd-inhibit", "--list", "--mode=block",
            "--no-pager", "--no-legend",
        ])
        if any(re.search(r"(^|[:,\s])shutdown([:,\s]|$)", line)
               for line in inhibitors.splitlines()):
            return "shutdown-inhibitor"
    except SensorError as exc:
        LAST_SENSOR_ERROR = {
            "sensor": exc.sensor,
            "failure_kind": exc.failure_kind,
            "returncode": exc.returncode,
        }
        if exc.errno is not None:
            LAST_SENSOR_ERROR["errno"] = exc.errno
        return "sensor-error"
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        LAST_SENSOR_ERROR = {
            "sensor": "python",
            "failure_kind": "parser-error",
            "returncode": None,
        }
        return "sensor-error"
    return None


def append_event(event: dict) -> None:
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    payload = {"utc": utc_now(), "boot_id": boot_id(), **event}
    with open(EVENT_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def remove_idle_state() -> None:
    try:
        os.unlink(IDLE_STATE)
    except FileNotFoundError:
        pass


def write_idle_state(now_uptime: float) -> None:
    tmp = IDLE_STATE + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"boot_id": boot_id(), "uptime": now_uptime}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, IDLE_STATE)


def read_idle_state():
    try:
        with open(IDLE_STATE, encoding="utf-8") as handle:
            state = json.load(handle)
        return state["boot_id"], float(state["uptime"])
    except (FileNotFoundError, KeyError, TypeError, ValueError,
            json.JSONDecodeError):
        return None


def main() -> int:
    os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    with open(LOCK_PATH, "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        reason = busy_reason()
        if reason:
            remove_idle_state()
            event = {"decision": "busy", "reason": reason}
            if reason == "sensor-error" and LAST_SENSOR_ERROR:
                event["sensor_error"] = LAST_SENSOR_ERROR
            append_event(event)
            return 0

        now = uptime_seconds()
        state = read_idle_state()
        if state is None or state[0] != boot_id() or state[1] > now:
            write_idle_state(now)
            append_event({"decision": "idle-start", "uptime": now})
            return 0

        idle_for = now - state[1]
        if idle_for < IDLE_SECONDS:
            return 0

        # Recheck twice so a session arriving at the boundary cancels shutdown.
        for _attempt in range(2):
            reason = busy_reason()
            if reason:
                remove_idle_state()
                append_event({"decision": "shutdown-cancelled",
                              "reason": reason,
                              "idle_for_sec": int(idle_for)})
                return 0
            if _attempt == 0:
                time.sleep(5)

        append_event({
            "decision": "poweroff-requested",
            "reason": "no-ssh-no-inhibitor",
            "idle_since_uptime": state[1],
            "idle_for_sec": int(uptime_seconds() - state[1]),
        })
        result = subprocess.run(
            ["/usr/bin/systemctl", "poweroff"], check=False, timeout=20
        )
        if result.returncode:
            append_event({"decision": "poweroff-failed",
                          "returncode": result.returncode})
            return result.returncode
        return 0


if __name__ == "__main__":
    sys.exit(main())
