#!/usr/bin/env python3
"""Lifecycle readiness checker for the Kalshi HFT control system.

This is the ops-side source of truth for readiness. It writes:

  work/lifecycle_status.json
  work/lifecycle_events.ndjson

Safety policy:
  * never runs tools classified as live_order
  * skips real Exchange/API checks unless --allow-network is explicit
  * does not touch the C++ trading hot path
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
WORK = os.path.join(ROOT, "work")
STATUS_FILE = os.path.join(WORK, "lifecycle_status.json")
EVENTS_FILE = os.path.join(WORK, "lifecycle_events.ndjson")
SPEC_STATUS_FILE = os.path.join(WORK, "kalshi_spec_alignment.json")
UPDATES_FILE = os.path.join(WORK, "kalshi_updates.ndjson")

sys.path.insert(0, TOOLS)
import run_tests  # noqa: E402
import feed_readiness  # noqa: E402

VALID_STATUS = {"pass", "fail", "skipped", "not_started", "blocked"}

STAGES = [
    ("kalshi_api_updates", "Kalshi API Updates", []),
    ("api_spec_alignment", "API Spec Alignment", ["kalshi_api_updates"]),
    ("connection_exchange", "Connection / Exchange Evaluation", ["api_spec_alignment"]),
    ("core_tests", "Core Tests", ["connection_exchange"]),
    ("data_pipeline", "Data Pipeline", ["core_tests"]),
    ("strategy_shadow", "Strategy Shadow", ["data_pipeline"]),
    ("live_execution", "Live Execution Gate", ["strategy_shadow"]),
]


def now_ms():
    return int(time.time() * 1000)


def rel(path):
    return os.path.relpath(path, ROOT)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def append_event(event):
    os.makedirs(WORK, exist_ok=True)
    event = dict(event)
    event.setdefault("ts_ms", now_ms())
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def read_last_ndjson(path, limit=500):
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= limit:
                read = min(65536, pos)
                pos -= read
                f.seek(pos)
                data = f.read(read) + data
    except OSError:
        return []
    for line in data.splitlines()[-limit:]:
        try:
            out.append(json.loads(line.decode("utf-8", "replace")))
        except Exception:
            pass
    return out


def stage(stage_id, label, status, summary, checks=None, depends_on=None,
          blocking_reason="", evidence_log="", evidence=None):
    assert status in VALID_STATUS, status
    item = {
        "id": stage_id,
        "label": label,
        "status": status,
        "summary": summary,
        "blocking_reason": blocking_reason,
        "depends_on": depends_on or [],
        "checked_at_ms": now_ms(),
        "evidence_log": evidence_log,
        "evidence": evidence or [],
        "checks": checks or [],
    }
    append_event({"type": "lifecycle_stage", "stage": stage_id,
                  "label": label, "status": status, "summary": summary,
                  "blocking_reason": blocking_reason,
                  "evidence_log": evidence_log})
    return item


def check_record(name, status, detail, duration_ms=0, log=""):
    assert status in VALID_STATUS, status
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "duration_ms": int(duration_ms or 0),
        "log": log,
    }


def run_script(argv):
    start = time.time()
    try:
        proc = subprocess.run(argv, cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)
        out = proc.stdout.decode("utf-8", "replace")
        return proc.returncode, out, int((time.time() - start) * 1000)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if e.stdout else ""
        return 124, out + "\nTIMEOUT\n", int((time.time() - start) * 1000)
    except OSError as e:
        return 126, "ERROR: %s\n" % e, int((time.time() - start) * 1000)


def run_kalshi_updates(allow_network):
    label = "Kalshi API Updates"
    if not allow_network:
        return stage("kalshi_api_updates", label, "skipped",
                     "Public docs watcher skipped because --allow-network is not set.",
                     [check_record("kalshi_update_watcher", "skipped",
                                   "Requires --allow-network; uses public docs only.")],
                     evidence_log=rel(UPDATES_FILE) if os.path.exists(UPDATES_FILE) else "")

    argv = [sys.executable, os.path.join(TOOLS, "kalshi_update_watcher.py"),
            "--allow-network", "--once", "--json"]
    rc, out, dur = run_script(argv)
    parsed = None
    try:
        parsed = json.loads(out)
    except Exception:
        pass
    if rc == 0:
        changes = parsed.get("changes", 0) if isinstance(parsed, dict) else 0
        return stage("kalshi_api_updates", label, "pass",
                     "Public docs watcher completed; %s update events detected." % changes,
                     [check_record("kalshi_update_watcher", "pass",
                                   "Fetched changelog, RSS, OpenAPI, AsyncAPI, and llms.txt.",
                                   dur, rel(UPDATES_FILE))],
                     evidence_log=rel(UPDATES_FILE))
    return stage("kalshi_api_updates", label, "fail",
                 "Public docs watcher failed.",
                 [check_record("kalshi_update_watcher", "fail", out.strip()[-500:], dur)],
                 blocking_reason="Kalshi docs/update status could not be checked.",
                 evidence_log=rel(UPDATES_FILE) if os.path.exists(UPDATES_FILE) else "")


def run_spec_alignment(allow_network):
    label = "API Spec Alignment"
    if allow_network:
        argv = [sys.executable, os.path.join(TOOLS, "kalshi_spec_sync.py"),
                "--allow-network", "--json"]
        rc, out, dur = run_script(argv)
        parsed = None
        try:
            parsed = json.loads(out)
        except Exception:
            parsed = load_json(SPEC_STATUS_FILE, {})
        status = parsed.get("status") if isinstance(parsed, dict) else None
        if status not in VALID_STATUS:
            status = "fail" if rc else "pass"
        drift = len(parsed.get("drift", [])) if isinstance(parsed, dict) else 0
        reason = parsed.get("summary", "") if isinstance(parsed, dict) else out.strip()[-500:]
        return stage("api_spec_alignment", label, status,
                     reason or ("Spec alignment completed with %d drift item(s)." % drift),
                     [check_record("kalshi_spec_sync", status,
                                   "%d drift item(s)." % drift, dur, rel(SPEC_STATUS_FILE))],
                     evidence_log=rel(SPEC_STATUS_FILE),
                     blocking_reason="" if status in ("pass", "skipped") else
                     "API syntax drift or missing spec baseline must be resolved before live execution.")

    saved = load_json(SPEC_STATUS_FILE)
    if saved:
        status = saved.get("status", "not_started")
        if status not in VALID_STATUS:
            status = "not_started"
        return stage("api_spec_alignment", label, status,
                     saved.get("summary", "Using last saved spec-alignment result."),
                     [check_record("kalshi_spec_sync", status,
                                   "Last saved result; rerun with --allow-network to refresh.",
                                   0, rel(SPEC_STATUS_FILE))],
                     evidence_log=rel(SPEC_STATUS_FILE),
                     blocking_reason=saved.get("blocking_reason", ""))
    return stage("api_spec_alignment", label, "skipped",
                 "Spec fetch skipped because --allow-network is not set and no saved result exists.",
                 [check_record("kalshi_spec_sync", "skipped",
                               "No latest Kalshi spec snapshot available locally.")])


def run_connection_exchange(allow_network):
    label = "Connection / Exchange Evaluation"
    network_tools = [("preflight", "Exchange/API availability"),
                     ("account_info", "Account limits and endpoint costs")]
    if not allow_network:
        checks = [check_record(name, "skipped",
                               "Skipped without --allow-network; not counted as failure.")
                  for name, _ in network_tools]
        return stage("connection_exchange", label, "skipped",
                     "Real Exchange/API checks skipped without --allow-network.",
                     checks)

    by_name = {t["name"]: t for t in run_tests.load_registry()}
    checks = []
    failed = False
    for name, desc in network_tools:
        tool = by_name.get(name)
        if not tool:
            failed = True
            checks.append(check_record(name, "fail", "Tool is not registered."))
            continue
        ok, reason = run_tests.may_run(tool, allow_network=True)
        if not ok or tool.get("safety") == "live_order":
            failed = True
            checks.append(check_record(name, "fail", reason))
            continue
        rec = run_tests.run_tool(tool, allow_network=True)
        if rec["status"] != "refused":
            run_tests.record(rec)
        status = "pass" if rec.get("status") == "pass" else "fail"
        failed = failed or status == "fail"
        checks.append(check_record(name, status, desc, rec.get("duration_ms", 0),
                                   rec.get("log", "")))
    return stage("connection_exchange", label, "fail" if failed else "pass",
                 "Read-only Exchange/API checks %s." % ("failed" if failed else "passed"),
                 checks,
                 blocking_reason="Exchange/account checks must pass before live execution."
                 if failed else "")


def core_tools():
    return run_tests.runnable_test_set(run_tests.load_registry())


def run_core_tests(run_tests_now):
    label = "Core Tests"
    tools = core_tools()
    if run_tests_now:
        checks = []
        failed = False
        for tool in tools:
            if tool.get("safety") == "live_order":
                failed = True
                checks.append(check_record(tool["name"], "fail",
                                           "live_order appeared in core set"))
                continue
            rec = run_tests.run_tool(tool, allow_network=False)
            if rec["status"] != "refused":
                run_tests.record(rec)
            status = "pass" if rec.get("status") == "pass" else "fail"
            failed = failed or status == "fail"
            checks.append(check_record(tool["name"], status, rec.get("reason", ""),
                                       rec.get("duration_ms", 0), rec.get("log", "")))
        return stage("core_tests", label, "fail" if failed else "pass",
                     "Core pure/offline tests %s." % ("failed" if failed else "passed"),
                     checks,
                     blocking_reason="Core tests must pass before downstream readiness."
                     if failed else "")

    latest = load_json(run_tests.LATEST, {})
    checks = []
    missing = []
    failed = []
    for tool in tools:
        rec = latest.get(tool["name"])
        if not rec:
            missing.append(tool["name"])
            checks.append(check_record(tool["name"], "not_started",
                                       "No latest result in %s." % rel(run_tests.LATEST)))
        else:
            status = "pass" if rec.get("status") == "pass" else "fail"
            if status == "fail":
                failed.append(tool["name"])
            checks.append(check_record(tool["name"], status, rec.get("reason", ""),
                                       rec.get("duration_ms", 0), rec.get("log", "")))
    if failed:
        return stage("core_tests", label, "fail",
                     "%d core test(s) failed." % len(failed), checks,
                     blocking_reason="Failing core tests block lifecycle readiness.",
                     evidence_log=rel(run_tests.LATEST) if os.path.exists(run_tests.LATEST) else "")
    if missing:
        return stage("core_tests", label, "not_started",
                     "%d core test(s) do not have latest results; run with --run-core-tests." %
                     len(missing),
                     checks,
                     blocking_reason="Core test evidence is incomplete.",
                     evidence_log=rel(run_tests.LATEST) if os.path.exists(run_tests.LATEST) else "")
    return stage("core_tests", label, "pass",
                 "All registered core pure/offline tests have passing latest results.",
                 checks, evidence_log=rel(run_tests.LATEST))


def telemetry_stage(metrics_path):
    label = "Data Pipeline"
    readiness = feed_readiness.collect_status(ROOT, metrics_path)
    readiness_ok = readiness.get("status") in ("ready", "active")
    readiness_check = check_record(
        "feed_readiness",
        "pass" if readiness_ok else "fail",
        readiness.get("summary", "feed readiness unavailable"),
    )
    if readiness.get("status") != "active":
        metrics = readiness.get("metrics", {})
        status = "not_started" if readiness.get("status") in (
            "ready", "missing_prerequisites") else "fail"
        return stage("data_pipeline", label, status,
                     readiness.get("summary", "Real market feed is not active."),
                     [readiness_check,
                      check_record("feed_telemetry", "not_started",
                                   "active=%s real_feed_rows=%s real_market_rows=%s" %
                                   (metrics.get("active"),
                                    metrics.get("real_feed_rows", 0),
                                    metrics.get("real_market_rows", 0)))],
                     evidence_log=rel(metrics_path) if os.path.exists(metrics_path) else "",
                     blocking_reason=readiness.get("summary") or
                     "Real Kalshi feed is not active yet.")
    rows = read_last_ndjson(metrics_path, 1000)
    real_feeds = [r for r in rows if r.get("type") == "feed" and not r.get("synthetic")]
    if not real_feeds:
        return stage("data_pipeline", label, "not_started",
                     "No real non-synthetic feed telemetry found.",
                     [readiness_check,
                      check_record("feed_telemetry", "not_started",
                                   "Expected fresh real feed events in %s." % rel(metrics_path))],
                     evidence_log=rel(metrics_path) if os.path.exists(metrics_path) else "",
                     blocking_reason=readiness.get("summary") or
                     "WebSocket/feed telemetry is not wired as readiness evidence.")
    latest = max(real_feeds, key=lambda r: r.get("ts_ms", 0))
    age = now_ms() - int(latest.get("ts_ms", 0) or 0)
    if latest.get("valid") is False or latest.get("connected") is False:
        return stage("data_pipeline", label, "fail",
                     "Latest real feed telemetry is invalid or disconnected.",
                     [readiness_check,
                      check_record("feed_telemetry", "fail",
                                   "connected=%s valid=%s age_ms=%s" %
                                   (latest.get("connected"), latest.get("valid"), age))],
                     evidence_log=rel(metrics_path),
                     blocking_reason="Data pipeline must be connected and valid.")
    if age > 5000:
        return stage("data_pipeline", label, "fail",
                     "Latest real feed telemetry is stale (%d ms old)." % age,
                     [readiness_check,
                      check_record("feed_telemetry", "fail", "stale feed telemetry")],
                     evidence_log=rel(metrics_path),
                     blocking_reason="Data pipeline telemetry must be fresh.")
    return stage("data_pipeline", label, "pass",
                 "Fresh real feed telemetry is present.",
                 [readiness_check,
                  check_record("feed_telemetry", "pass",
                               "Latest feed age_ms=%d." % age)],
                 evidence_log=rel(metrics_path))


def strategy_shadow_stage(metrics_path):
    label = "Strategy Shadow"
    rows = read_last_ndjson(metrics_path, 1000)
    real_strategy = [r for r in rows if r.get("type") == "strategy" and not r.get("synthetic")]
    shadow_orders = [r for r in rows if r.get("type") == "order" and
                     str(r.get("mode", "")).lower() == "shadow" and not r.get("synthetic")]
    if not real_strategy and not shadow_orders:
        return stage("strategy_shadow", label, "blocked",
                     "No real strategy/shadow validation telemetry exists yet.",
                     [check_record("strategy_roster", "blocked",
                                   "Strategy roster/shadow PnL validation is not producing evidence.")],
                     evidence_log=rel(metrics_path) if os.path.exists(metrics_path) else "",
                     blocking_reason="Blocked until real data-pipeline evidence and shadow strategy validation exist.")
    return stage("strategy_shadow", label, "pass",
                 "Real strategy/shadow telemetry is present.",
                 [check_record("shadow_telemetry", "pass",
                               "%d strategy rows, %d shadow order rows." %
                               (len(real_strategy), len(shadow_orders)))],
                 evidence_log=rel(metrics_path))


def live_execution_stage(previous):
    label = "Live Execution Gate"
    gates = [
        ("risk", "Risk gate complete"),
        ("kill_switch", "Operator kill switch complete"),
        ("reconcile", "Reconcile-on-ambiguity complete"),
        ("endpoint_costs", "Endpoint costs verified"),
        ("account_limits", "Account limits verified"),
        ("token_budget", "Token budget/accounting complete"),
        ("clean_shadow", "Clean shadow validation complete"),
    ]
    checks = [check_record(name, "blocked", detail) for name, detail in gates]
    return stage("live_execution", label, "blocked",
                 "Live orders remain blocked until production safety gates are complete.",
                 checks,
                 blocking_reason="Live Execution is intentionally blocked: risk, kill switch, reconcile, endpoint costs, account limits, token budget, and clean shadow validation are not all complete.")


def enforce_dependencies(stages):
    by_id = {s["id"]: s for s in stages}
    for s in stages:
        if s["status"] != "pass":
            continue
        bad = []
        for dep in s.get("depends_on", []):
            d = by_id.get(dep)
            if d and d.get("status") not in ("pass", "skipped"):
                bad.append("%s=%s" % (dep, d.get("status")))
        if bad:
            s["status"] = "blocked"
            s["blocking_reason"] = "Earlier lifecycle dependency is not ready: " + ", ".join(bad)
            s["summary"] = "Blocked by earlier lifecycle dependency."
    return stages


def overall_status(stages):
    statuses = [s["status"] for s in stages]
    if "fail" in statuses:
        return "fail"
    if "blocked" in statuses:
        return "blocked"
    if "not_started" in statuses:
        return "not_started"
    if all(s == "skipped" for s in statuses):
        return "skipped"
    return "pass"


def build_status(args):
    os.makedirs(WORK, exist_ok=True)
    append_event({"type": "lifecycle_run_started",
                  "allow_network": args.allow_network,
                  "run_core_tests": args.run_core_tests})
    stages = []
    defs = {sid: (label, deps) for sid, label, deps in STAGES}
    stages.append(run_kalshi_updates(args.allow_network))
    stages[-1]["depends_on"] = defs[stages[-1]["id"]][1]
    stages.append(run_spec_alignment(args.allow_network))
    stages[-1]["depends_on"] = defs[stages[-1]["id"]][1]
    stages.append(run_connection_exchange(args.allow_network))
    stages[-1]["depends_on"] = defs[stages[-1]["id"]][1]
    stages.append(run_core_tests(args.run_core_tests))
    stages[-1]["depends_on"] = defs[stages[-1]["id"]][1]
    stages.append(telemetry_stage(os.path.abspath(args.metrics)))
    stages[-1]["depends_on"] = defs[stages[-1]["id"]][1]
    stages.append(strategy_shadow_stage(os.path.abspath(args.metrics)))
    stages[-1]["depends_on"] = defs[stages[-1]["id"]][1]
    stages.append(live_execution_stage(stages))
    stages[-1]["depends_on"] = defs[stages[-1]["id"]][1]
    stages = enforce_dependencies(stages)
    status = overall_status(stages)
    out = {
        "type": "lifecycle_status",
        "schema_version": 1,
        "generated_at_ms": now_ms(),
        "allow_network": args.allow_network,
        "run_core_tests": args.run_core_tests,
        "status_values": sorted(VALID_STATUS),
        "status": status,
        "stages": stages,
    }
    write_json(STATUS_FILE, out)
    append_event({"type": "lifecycle_run_finished", "status": status,
                  "status_file": rel(STATUS_FILE)})
    return out


def main():
    ap = argparse.ArgumentParser(description="Lifecycle readiness source of truth")
    ap.add_argument("--allow-network", action="store_true",
                    help="permit public-docs and read-only Exchange/API checks")
    ap.add_argument("--run-core-tests", action="store_true",
                    help="run the registered pure/offline core test set now")
    ap.add_argument("--metrics", default="work/metrics.ndjson",
                    help="dashboard telemetry NDJSON file to inspect")
    ap.add_argument("--json", action="store_true", help="print JSON status")
    args = ap.parse_args()

    status = build_status(args)
    if args.json:
        print(json.dumps(status, sort_keys=True))
    else:
        for s in status["stages"]:
            print("%-34s %s - %s" % (s["label"], s["status"], s["summary"]))
        if status["status"] == "pass":
            print("LIFECYCLE PASS")
        else:
            print("LIFECYCLE %s" % status["status"].upper())
    return 0 if status["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
