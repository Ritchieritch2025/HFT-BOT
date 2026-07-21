#!/usr/bin/env python3
"""Ops-console orchestration backend (PLAN_PROD_V1 P2). stdlib only.

Reads tools.json, runs a tool (satisfying its `needs` by spinning up localhost
mocks on ephemeral ports), parses the uniform PASS:/FAIL: + pass_token output,
and records results to work/test_results.ndjson (+ work/test_results_latest.json
for a cheap dashboard load).

SAFETY POLICY (also enforced by the dashboard server, guardrail 2):
  pure/offline  -> runnable
  network_read  -> runnable only with allow_network=True (server --allow-network)
  live_order    -> NEVER runnable from here (transmits/mutates real state)

CLI: run_tests.py --list | --tool NAME [--allow-network] | --all | --json
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")
LOGDIR = os.path.join(WORK, "logs")
RESULTS = os.path.join(WORK, "test_results.ndjson")
LATEST = os.path.join(WORK, "test_results_latest.json")


def load_registry():
    with open(os.path.join(ROOT, "tools.json")) as f:
        return json.load(f)["tools"]


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --- safety policy (single source of truth; the server calls may_run too) ------
def may_run(tool, allow_network=False):
    safety = tool.get("safety")
    if safety in ("pure", "offline"):
        return True, "ok"
    if safety == "network_read":
        if allow_network:
            return True, "ok"
        return False, "network_read requires the server's --allow-network flag"
    if safety == "live_order":
        return False, "live_order tools transmit/mutate real state and are never runnable from the console"
    return False, "unknown safety class '%s'" % safety


# --- output parsing ------------------------------------------------------------
def parse_output(text, pass_token):
    passed = sum(1 for ln in text.splitlines() if ln.startswith("PASS:"))
    failed = sum(1 for ln in text.splitlines() if ln.startswith("FAIL:"))
    token_ok = (pass_token in text) if pass_token else True
    # "FAILURES" is the house sentinel for a failed unit-test binary.
    if "FAILURES" in text:
        token_ok = False
    return passed, failed, token_ok


# --- needs / mock lifecycle ----------------------------------------------------
class Mocks:
    """Starts the localhost mocks a tool needs; substitutes ports into its cmd."""
    def __init__(self):
        self.procs = []

    def _spawn(self, script, *args):
        log = open(os.path.join(LOGDIR, "mock_%s.log" % script), "ab")
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", script)]
                             + [str(a) for a in args], stdout=log, stderr=log)
        self.procs.append((p, log))
        time.sleep(0.7)
        return p

    def build_cmd(self, tool):
        """Return the argv list to run, starting any mocks the tool needs."""
        cmd = tool["cmd"].split()
        needs = tool.get("needs", [])
        if "mini_redis" in needs:
            port = free_port(); self._spawn("mini_redis.py", port)
            cmd += [str(port)]
        elif "mock_rest" in needs:
            port = free_port(); self._spawn("mock_rest.py", port)
            cmd += ["http://127.0.0.1:%d" % port]
        elif "mock_server" in needs:
            port = free_port()
            self._spawn("mock_server.py", port, os.path.join(WORK, "mock_capture.jsonl"))
            key = os.path.join(ROOT, "build", "scratch", "console_key.pem")
            if not os.path.exists(key):
                os.makedirs(os.path.dirname(key), exist_ok=True)
                subprocess.run(["openssl", "genrsa", "-out", key, "2048"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cmd += ["http://127.0.0.1:%d" % port, key, "3", "50"]
        elif "mock_ws" in needs:
            port = free_port(); self._spawn("mock_ws_exchange.py", port)
            cmd += ["ws://127.0.0.1:%d/trade-api/ws/v2" % port]
        return cmd

    def stop(self):
        for p, log in self.procs:
            p.terminate()
            try:
                p.wait(timeout=2)
            except Exception:
                p.kill()
            log.close()
        self.procs = []


def _absolutize(cmd):
    """ROOT-anchor a relative command so it survives a cwd change (below).

    cmd[0] like `./build/x` or `./tools/x.py` -> absolute; for an interpreter
    (python3/bash/sh) ROOT-anchor the relative script argument. Mock args built by
    build_cmd are already absolute/URLs, so only these leading tokens matter.
    """
    if not cmd:
        return cmd
    cmd = list(cmd)
    if cmd[0].startswith("./"):
        cmd[0] = os.path.join(ROOT, cmd[0][2:])
    elif os.path.basename(cmd[0]) in ("python3", "python", "bash", "sh") and len(cmd) > 1 \
            and not cmd[1].startswith("-") and not os.path.isabs(cmd[1]):
        cmd[1] = os.path.join(ROOT, cmd[1])
    return cmd


def run_tool(tool, allow_network=False):
    os.makedirs(LOGDIR, exist_ok=True)
    # Run every tool from a scratch cwd so any cwd-relative output (rotation
    # shards, replay/recorder NDJSON, fuzz corpus) lands under build/scratch
    # (gitignored) instead of littering the repo root. Commands are ROOT-anchored
    # via _absolutize so the cwd change is transparent.
    scratch = os.path.join(ROOT, "build", "scratch")
    os.makedirs(scratch, exist_ok=True)
    ok, reason = may_run(tool, allow_network)
    name = tool["name"]
    if not ok:
        return {"suite": name, "status": "refused", "passed": 0, "failed": 0,
                "duration_ms": 0, "reason": reason, "log": ""}
    mocks = Mocks()
    logpath = os.path.join(LOGDIR, "%s.txt" % name)
    start = time.time()
    err_reason = ""
    try:
        cmd = _absolutize(mocks.build_cmd(tool))
        run_cwd = ROOT if tool.get("cwd") == "root" else scratch
        proc = subprocess.run(cmd, cwd=run_cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)
        out = proc.stdout.decode("utf-8", "replace")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        out, rc, err_reason = "TIMEOUT\n", 124, "timeout"
    except FileNotFoundError as e:
        # Binary/script not built or missing: report cleanly, never crash the caller.
        out, rc, err_reason = "ERROR: %s\n" % e, 127, "binary missing (run make?)"
    except OSError as e:
        # Exec format error, permission denied, etc.: report cleanly.
        out, rc, err_reason = "ERROR: %s\n" % e, 126, "exec failed: %s" % e
    finally:
        mocks.stop()
    dur = int((time.time() - start) * 1000)
    with open(logpath, "w") as f:
        f.write(out)
    passed, failed, token_ok = parse_output(out, tool.get("pass_token"))
    status = "pass" if (rc == 0 and token_ok) else ("error" if err_reason else "fail")
    rec = {"suite": name, "status": status, "passed": passed, "failed": failed,
           "duration_ms": dur, "log": os.path.relpath(logpath, ROOT)}
    if err_reason:
        rec["reason"] = err_reason
    return rec


def record(rec):
    os.makedirs(WORK, exist_ok=True)
    rec = dict(rec)
    rec["type"] = "test_suite"
    rec["ts_ms"] = int(time.time() * 1000)
    with open(RESULTS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    latest = {}
    if os.path.exists(LATEST):
        try:
            latest = json.load(open(LATEST))
        except Exception:
            latest = {}
    latest[rec["suite"]] = rec
    with open(LATEST, "w") as f:
        json.dump(latest, f, indent=2)


def runnable_test_set(tools):
    # The console "Run all" set: test + check kinds that are pure/offline.
    return [t for t in tools if t.get("kind") in ("test", "check")
            and t.get("safety") in ("pure", "offline")
            and t.get("autorun", True)  # component validators that need input args opt out
            and "<" not in t.get("args_template", "")
            and t.get("name") not in ("run_pipeline", "run_tests", "lifecycle_check")
            and not t.get("name", "").startswith("run_")]  # wrappers live in Tools


def main():
    ap = argparse.ArgumentParser(description="Ops-console test/tool orchestrator")
    ap.add_argument("--list", action="store_true", help="list registered tools")
    ap.add_argument("--tool", help="run one tool by name")
    ap.add_argument("--all", action="store_true", help="run the pure/offline test+check set")
    ap.add_argument("--allow-network", action="store_true", help="permit network_read tools")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    tools = load_registry()
    by_name = {t["name"]: t for t in tools}

    if args.list:
        if args.json:
            print(json.dumps(tools, indent=2))
        else:
            for t in tools:
                print("%-22s %-8s %-13s %s" % (t["name"], t["kind"], t["safety"],
                                               t.get("description", "")))
        return 0

    if args.tool:
        if args.tool not in by_name:
            print("unknown tool: %s" % args.tool, file=sys.stderr)
            return 2
        rec = run_tool(by_name[args.tool], args.allow_network)
        if rec["status"] != "refused":
            record(rec)
        print(json.dumps(rec) if args.json else
              "%s: %s (%sms, +%s/-%s) %s" % (rec["suite"], rec["status"],
              rec["duration_ms"], rec["passed"], rec["failed"], rec.get("reason", "")))
        return 0 if rec["status"] == "pass" else 1

    if args.all:
        overall = 0
        for t in runnable_test_set(tools):
            rec = run_tool(t, args.allow_network)
            record(rec)
            print("%-22s %s (+%s/-%s)" % (rec["suite"], rec["status"], rec["passed"], rec["failed"]))
            if rec["status"] != "pass":
                overall = 1
        return overall

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
