#!/usr/bin/env python3
"""Emit a credential-free proof of exactly one real W09 idle poweroff."""
import json


EVENT_LOG = "/var/lib/w09-idle/events.jsonl"
CONTROL_PLANE_PROOF = "/etc/w09/idle-control-plane-stop.json"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def main():
    with open(EVENT_LOG, encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle if line.strip()]
    poweroffs = [event for event in events
                 if event.get("decision") == "poweroff-requested"]
    if len(poweroffs) != 1:
        raise SystemExit(
            "IDLE_PROOF_GATE: expected exactly one poweroff, got %d" %
            len(poweroffs)
        )
    event = poweroffs[0]
    failures = [item for item in events
                if item.get("decision") == "poweroff-failed"
                and item.get("boot_id") == event.get("boot_id")
                and item.get("utc", "") >= event.get("utc", "")]
    if failures:
        raise SystemExit("IDLE_PROOF_GATE: poweroff request returned failure")
    if int(event.get("idle_for_sec", -1)) < 1800:
        raise SystemExit("IDLE_PROOF_GATE: idle duration below 1800 seconds")
    with open(BOOT_ID_PATH, encoding="ascii") as handle:
        current_boot = handle.read().strip()
    if event.get("boot_id") == current_boot:
        raise SystemExit(
            "IDLE_PROOF_GATE: no subsequent boot after poweroff request"
        )
    try:
        with open(CONTROL_PLANE_PROOF, encoding="utf-8") as handle:
            control = json.load(handle)
    except (OSError, ValueError):
        raise SystemExit(
            "IDLE_PROOF_GATE: external EC2 stopped-state proof missing"
        )
    if (control.get("operator_observed_state") != "stopped" or
            control.get("poweroff_boot_id") != event.get("boot_id") or
            control.get("request_utc") != event.get("utc")):
        raise SystemExit(
            "IDLE_PROOF_GATE: external stopped-state proof does not bind "
            "the poweroff request"
        )
    print(json.dumps({
        "action": "poweroff",
        "event_utc": event.get("utc"),
        "idle_for_sec": event.get("idle_for_sec"),
        "poweroff_boot_id": event.get("boot_id"),
        "current_boot_id": current_boot,
        "reason": event.get("reason"),
        "operator_observed_state": "stopped",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
