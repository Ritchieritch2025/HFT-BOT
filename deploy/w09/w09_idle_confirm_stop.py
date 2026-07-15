#!/usr/bin/env python3
"""Bind an operator-observed EC2 ``stopped`` state to one poweroff request."""
import datetime
import json
import os


EVENT_LOG = "/var/lib/w09-idle/events.jsonl"
PROOF = "/etc/w09/idle-control-plane-stop.json"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def main():
    with open(EVENT_LOG, encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle if line.strip()]
    requests = [event for event in events
                if event.get("decision") == "poweroff-requested"]
    if len(requests) != 1:
        raise SystemExit(
            "CONTROL_PLANE_PROOF_GATE: expected one poweroff request"
        )
    request = requests[0]
    failures = [item for item in events
                if item.get("decision") == "poweroff-failed"
                and item.get("boot_id") == request.get("boot_id")
                and item.get("utc", "") >= request.get("utc", "")]
    if failures:
        raise SystemExit(
            "CONTROL_PLANE_PROOF_GATE: poweroff request reported failure"
        )
    with open(BOOT_ID_PATH, encoding="ascii") as handle:
        current_boot = handle.read().strip()
    if current_boot == request.get("boot_id"):
        raise SystemExit(
            "CONTROL_PLANE_PROOF_GATE: same boot; stop/restart not complete"
        )
    payload = {
        "confirmed_at_utc": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "operator_observed_state": "stopped",
        "poweroff_boot_id": request.get("boot_id"),
        "request_utc": request.get("utc"),
        "restart_boot_id": current_boot,
    }
    tmp = PROOF + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, PROOF)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
