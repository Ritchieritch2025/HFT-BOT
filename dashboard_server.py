#!/usr/bin/env python3
"""Localhost-only operational and research console for the Kalshi PoC.

Read-only: it tails an append-only NDJSON file the trading system writes from
its cold telemetry thread and streams new lines to the browser over SSE. It
never connects to Kalshi, never places orders, and never touches the trading
hot path. Killing or reloading it cannot affect the trading process.

    python3 dashboard_server.py --metrics work/metrics.ndjson --port 8765
    open http://127.0.0.1:8765
"""
import argparse
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Shared tool registry + safety policy (single source of truth with the console
# backend). The POST /api/run handler enforces may_run() SERVER-SIDE — the UI
# affordance is not the security boundary (guardrail 2).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import run_tests  # noqa: E402
import warehouse_status  # noqa: E402  (Phase 5 warehouse panel; read-only, no conversion)
import feed_readiness  # noqa: E402  (read-only local market-feed readiness)
from research import inbox as research_inbox  # noqa: E402
from research.coordinator import ResearchW09Coordinator  # noqa: E402
from research.plan_contract import PlanContractError, compile_plan  # noqa: E402

LIFECYCLE_STATUS = os.path.join(run_tests.WORK, "lifecycle_status.json")
LIFECYCLE_EVENTS = os.path.join(run_tests.WORK, "lifecycle_events.ndjson")
KALSHI_UPDATES = os.path.join(run_tests.WORK, "kalshi_updates.ndjson")

# ---------------------------------------------------------------- init checks


def _tool_by_name():
    return {t["name"]: t for t in run_tests.load_registry()}


def _read_log(relpath):
    if not relpath:
        return ""
    path = os.path.join(run_tests.ROOT, relpath)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _line(text, prefix):
    for ln in text.splitlines():
        if ln.startswith(prefix):
            return ln
    return ""


def _init_detail(name, ok, output):
    if not ok:
        fail = _line(output, "FAIL")
        if fail:
            return fail
        return "Failed; open the log for details"
    if name == "check_gates":
        return "Safety gates Successful"
    if name == "check_registry":
        return "Tool registry Successful"
    if name == "test_env_safety":
        return "Runtime safety Successful"
    if name == "test_signing":
        return "Signing Successful"
    if name == "test_request_spec":
        return "Request classification Successful"
    if name == "test_endpoint_costs":
        return "Endpoint-cost parser Successful"
    if name == "preflight":
        exchange = "true" if "PASS  GET /exchange/status" in output else "unknown"
        auth = "true" if "PASS  API key authenticated" in output else "unknown"
        markets = "true" if "PASS  market data parses" in output else "unknown"
        return "Exchange Successful, exchange=%s, api_auth=%s, market_data=%s" % (
            exchange, auth, markets)
    if name == "account_info":
        tier = "unknown"
        for ln in output.splitlines():
            if ln.startswith("usage_tier"):
                tier = ln.split(":", 1)[-1].strip()
                break
        costs = "true" if "Official endpoint costs" in output else "unknown"
        return "Account limits Successful, usage_tier=%s, endpoint_costs=%s" % (
            tier, costs)
    return "Successful"


def run_engine_init(allow_network):
    """One-click, read-only readiness check. Never runs live_order tools."""
    start = time.time()
    tools = _tool_by_name()
    plan = [
        ("check_gates", "Safety gates"),
        ("check_registry", "Tool registry"),
        ("test_env_safety", "Runtime safety"),
        ("test_signing", "Signing"),
        ("test_request_spec", "Request classifier"),
        ("test_endpoint_costs", "Endpoint costs"),
    ]
    network_plan = [
        ("preflight", "Exchange + API availability"),
        ("account_info", "Account limits + cost table"),
    ]
    steps = []

    def add_tool(name, label):
        tool = tools.get(name)
        if not tool:
            steps.append({"name": name, "label": label, "status": "fail",
                          "successful": False, "detail": "Tool is not registered"})
            return
        ok, reason = run_tests.may_run(tool, allow_network)
        if not ok:
            steps.append({"name": name, "label": label, "status": "skipped",
                          "successful": None, "detail": reason})
            return
        rec = run_tests.run_tool(tool, allow_network)
        out = _read_log(rec.get("log", ""))
        success = rec.get("status") == "pass"
        steps.append({
            "name": name,
            "label": label,
            "status": rec.get("status"),
            "successful": success,
            "duration_ms": rec.get("duration_ms", 0),
            "log": rec.get("log", ""),
            "detail": _init_detail(name, success, out),
        })

    for name, label in plan:
        add_tool(name, label)
    if allow_network:
        for name, label in network_plan:
            add_tool(name, label)
    else:
        for name, label in network_plan:
            steps.append({"name": name, "label": label, "status": "skipped",
                          "successful": None,
                          "detail": "Network check skipped; restart dashboard with --allow-network"})

    failed = [s for s in steps if s.get("successful") is False]
    return {
        "type": "engine_init",
        "allow_network": allow_network,
        "status": "pass" if not failed else "fail",
        "duration_ms": int((time.time() - start) * 1000),
        "steps": steps,
    }


def read_lifecycle_status():
    try:
        with open(LIFECYCLE_STATUS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "type": "lifecycle_status",
            "schema_version": 1,
            "status": "not_started",
            "allow_network": False,
            "generated_at_ms": 0,
            "status_values": ["pass", "fail", "skipped", "not_started", "blocked"],
            "stages": [
                {"id": "kalshi_api_updates", "label": "Kalshi API Updates", "status": "not_started",
                 "summary": "Lifecycle check has not run yet.", "depends_on": []},
                {"id": "api_spec_alignment", "label": "API Spec Alignment", "status": "not_started",
                 "summary": "Lifecycle check has not run yet.", "depends_on": ["kalshi_api_updates"]},
                {"id": "connection_exchange", "label": "Connection / Exchange Evaluation", "status": "not_started",
                 "summary": "Lifecycle check has not run yet.", "depends_on": ["api_spec_alignment"]},
                {"id": "core_tests", "label": "Core Tests", "status": "not_started",
                 "summary": "Lifecycle check has not run yet.", "depends_on": ["connection_exchange"]},
                {"id": "data_pipeline", "label": "Data Pipeline", "status": "not_started",
                 "summary": "Lifecycle check has not run yet.", "depends_on": ["core_tests"]},
                {"id": "strategy_shadow", "label": "Strategy Shadow", "status": "blocked",
                 "summary": "Waiting for data pipeline and strategy-shadow evidence.", "depends_on": ["data_pipeline"]},
                {"id": "live_execution", "label": "Live Execution Gate", "status": "blocked",
                 "summary": "Live orders remain blocked until production safety gates are complete.",
                 "depends_on": ["strategy_shadow"]},
            ],
        }


def run_lifecycle_check(metrics_path, allow_network, run_core_tests=False):
    cmd = [sys.executable, os.path.join(run_tests.ROOT, "tools", "lifecycle_check.py"),
           "--json", "--metrics", metrics_path]
    if allow_network:
        cmd.append("--allow-network")
    if run_core_tests:
        cmd.append("--run-core-tests")
    try:
        proc = subprocess.run(cmd, cwd=run_tests.ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=600)
        out = proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return {"type": "lifecycle_status", "status": "fail",
                "error": "lifecycle_check timed out"}
    except OSError as e:
        return {"type": "lifecycle_status", "status": "fail",
                "error": "lifecycle_check failed to start: %s" % e}
    try:
        return json.loads(out)
    except Exception:
        saved = read_lifecycle_status()
        saved["status"] = "fail"
        saved["error"] = "lifecycle_check returned invalid JSON"
        saved["raw_output"] = out[-1000:]
        return saved

# ------------------------------------------------------------------ file tail


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def tail_lines(path, n):
    """Return the last n complete, non-blank lines (as str), and the file
    size they were read at, so the live tail can resume with no gap/overlap."""
    size = file_size(path)
    if size <= 0 or n <= 0:
        return [], max(size, 0)
    try:
        with open(path, "rb") as f:
            block = 65536
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= n:
                read = min(block, pos)
                pos -= read
                f.seek(pos)
                data = f.read(read) + data
    except OSError:
        return [], max(size, 0)
    lines = [ln for ln in data.split(b"\n") if ln.strip()]
    out = [ln.decode("utf-8", "replace") for ln in lines[-n:]]
    return out, size


def stream_new_lines(path, start_pos):
    """Generator: yield complete lines appended after start_pos. Yields None
    as an idle tick so the caller can emit keepalives. Handles truncation
    (rotation) by resetting to 0, and a not-yet-existing file by waiting."""
    pos = start_pos
    pending = b""
    while True:
        size = file_size(path)
        if size < 0:
            yield None  # file gone / never created yet
            time.sleep(0.25)
            continue
        if size < pos:  # truncated or rotated
            pos = 0
            pending = b""
        if size > pos:
            try:
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(size - pos)
            except OSError:
                yield None
                time.sleep(0.2)
                continue
            pos = size
            pending += chunk
            nl = pending.rfind(b"\n")
            if nl >= 0:
                complete, pending = pending[: nl + 1], pending[nl + 1 :]
                for ln in complete.split(b"\n"):
                    if ln.strip():
                        yield ln.decode("utf-8", "replace")
            else:
                yield None
        else:
            yield None
            time.sleep(0.15)


# ------------------------------------------------------------------ handler


# --- Data pipeline: one-click activation + uptime monitoring -------------------
# Controls ONLY the read-only 24/7 firehose collector LaunchAgent (no orders,
# no trading path). Scoped to a single launchd label.
PIPELINE_LABEL = "com.ritcardo.kalshi-pipeline"


def _pgrep(pat):
    try:
        out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=5)
        pids = [int(x) for x in out.stdout.split()]
        return pids[0] if pids else None
    except Exception:
        return None


def _launchd_loaded():
    try:
        return subprocess.run(["launchctl", "list", PIPELINE_LABEL],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _staging_stats(root):
    path = os.path.join(root, "work", "warehouse", "staging.duckdb")
    if not os.path.exists(path):
        return {}
    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
        cnt, last = con.execute("SELECT count(*), max(ts_utc) FROM orderbooks_l1").fetchone()
        out = {"l1_rows": cnt,
               "trade_rows": con.execute("SELECT count(*) FROM trades").fetchone()[0],
               "categories": [r[0] for r in con.execute(
                   "SELECT DISTINCT category FROM orderbooks_l1 "
                   "WHERE category IS NOT NULL ORDER BY 1").fetchall()]}
        if last:
            out["staging_age_s"] = round(time.time() - last / 1_000_000)
        con.close()
        return out
    except Exception:
        return {"staging_note": "busy (ingesting)"}


def pipeline_status(root):
    import glob
    live = os.path.join(root, "work", "live")
    st = {"label": PIPELINE_LABEL,
          "creds_present": os.path.exists(os.path.expanduser("~/.kalshi/env.sh")),
          "launchd_loaded": _launchd_loaded(),
          "supervisor_pid": _pgrep("pipeline_supervisor"),
          "collector_pid": _pgrep("build/ws_shadow")}
    st["running"] = bool(st["supervisor_pid"] or st["collector_pid"])
    caps = sorted(glob.glob(os.path.join(live, "capture-*.ndjson")))
    if caps:
        st["last_capture"] = os.path.basename(caps[-1])
        st["capture_age_s"] = round(time.time() - os.path.getmtime(caps[-1]))
    ing = os.path.join(live, "ingest.log")
    if os.path.exists(ing):
        lines = tail_lines(ing, 1)[0]
        st["last_ingest"] = lines[0] if lines else ""
    st.update(_staging_stats(root))
    st["healthy"] = bool(st["running"] and st.get("capture_age_s", 10 ** 9) < 900)
    return st


def pipeline_control(root, action):
    src = os.path.join(root, "deploy", PIPELINE_LABEL + ".plist")
    dst = os.path.expanduser("~/Library/LaunchAgents/" + PIPELINE_LABEL + ".plist")
    if action == "activate":
        if not os.path.exists(os.path.expanduser("~/.kalshi/env.sh")):
            return {"ok": False, "error": "creds_missing",
                    "hint": "Create ~/.kalshi/env.sh with your KALSHI_API_KEY_ID + "
                            "KALSHI_PRIVATE_KEY_PATH exports, then click Activate."}
        try:
            import shutil
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            subprocess.run(["launchctl", "load", "-w", dst], capture_output=True, timeout=10)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    elif action == "stop":
        try:
            subprocess.run(["launchctl", "unload", "-w", dst], capture_output=True, timeout=10)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    else:
        return {"ok": False, "error": "unknown action"}
    return {"ok": True, "action": action, "status": pipeline_status(root)}


def make_handler(metrics_path, backfill_default, allow_network=False,
                 research_inbox_root=None, research_coordinator=None):
    research_inbox_root = os.path.abspath(
        research_inbox_root or os.path.join(run_tests.WORK, "research_inbox"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # quiet

        # -- helpers --
        def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def _research_job_id(self, path, suffix=""):
            prefix = "/api/research/jobs/"
            if not path.startswith(prefix):
                return None
            tail = path[len(prefix):]
            if suffix:
                if not tail.endswith(suffix):
                    return None
                tail = tail[:-len(suffix)]
            return tail if research_inbox.JOB_ID_RE.fullmatch(tail or "") else None

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif path == "/healthz":
                self._send(200, json.dumps({"ok": True}), "application/json")
            elif path == "/stream":
                self.handle_stream()
            elif path == "/updates-stream":
                self.handle_updates_stream()
            elif path == "/api/lifecycle":
                self._json(200, read_lifecycle_status())
            elif path == "/api/tools":
                # Never expose a run affordance the server won't honor: annotate
                # each tool with whether THIS server instance would run it.
                out = []
                for t in run_tests.load_registry():
                    ok, reason = run_tests.may_run(t, allow_network)
                    e = dict(t); e["runnable"] = ok; e["run_reason"] = reason
                    out.append(e)
                self._json(200, {"allow_network": allow_network, "tools": out})
            elif path == "/api/results":
                latest = {}
                if os.path.exists(run_tests.LATEST):
                    try:
                        latest = json.load(open(run_tests.LATEST))
                    except Exception:
                        latest = {}
                self._json(200, latest)
            elif path == "/api/warehouse":
                # Read-only Phase 5 warehouse panel: last run, row counts, latest
                # partition, schema/idempotency status, missing-category warnings.
                wh_dir = os.path.join(run_tests.ROOT, "work", "warehouse")
                self._json(200, warehouse_status.collect_status(wh_dir))
            elif path == "/api/feed_readiness":
                self._json(200, feed_readiness.collect_status(run_tests.ROOT, metrics_path))
            elif path == "/api/pipeline":
                self._json(200, pipeline_status(run_tests.ROOT))
            elif path == "/api/research/jobs":
                self._json(200, {
                    "schema_version": "research-inbox-list-v1",
                    "jobs": research_inbox.list_jobs(research_inbox_root),
                })
            elif self._research_job_id(path, "/report"):
                job_id = self._research_job_id(path, "/report")
                try:
                    report = research_inbox.read_report(research_inbox_root, job_id)
                    self._send(
                        200, report, "text/html; charset=utf-8",
                        {"Content-Security-Policy":
                         "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"})
                except (OSError, research_inbox.InboxError) as exc:
                    self._json(404, {"error": str(exc)})
            elif self._research_job_id(path):
                job_id = self._research_job_id(path)
                try:
                    self._json(200, research_inbox.get_job(research_inbox_root, job_id))
                except research_inbox.InboxError as exc:
                    self._json(404, {"error": str(exc)})
            else:
                self._send(404, "not found")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/research/jobs":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    if n <= 0 or n > research_inbox.MAX_PLAN_BYTES + 65536:
                        self._json(413, {"error": "research plan request is too large"})
                        return
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                    plan_text = body.get("plan_text")
                    filename = body.get("filename") or "PLAN.md"
                    spec = compile_plan(plan_text, filename=filename)
                    job = research_inbox.create_job(
                        research_inbox_root,
                        filename=filename,
                        plan_text=plan_text,
                        job_spec=spec,
                        automatic_execution_requested=body.get("auto_run", True) is True,
                    )
                    if research_coordinator is not None and job["status"]["state"] == "QUEUED":
                        research_coordinator.submit(job["job_id"])
                except (UnicodeError, ValueError, PlanContractError,
                        research_inbox.InboxError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(201, job)
                return
            if path == "/api/init":
                self._json(200, run_lifecycle_check(metrics_path, allow_network, False))
                return
            if path in ("/api/pipeline/activate", "/api/pipeline/stop"):
                action = "activate" if path.endswith("activate") else "stop"
                self._json(200, pipeline_control(run_tests.ROOT, action))
                return
            if path == "/api/lifecycle/run":
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except Exception:
                    self._json(400, {"error": "bad JSON body"})
                    return
                self._json(200, run_lifecycle_check(
                    metrics_path, allow_network, bool(body.get("run_core_tests"))))
                return
            if path != "/api/run":
                self._send(404, "not found")
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                self._json(400, {"error": "bad JSON body"})
                return
            name = body.get("name", "")
            by_name = {t["name"]: t for t in run_tests.load_registry()}
            tool = by_name.get(name)
            if not tool:
                self._json(404, {"error": "unknown tool '%s'" % name})
                return
            # SERVER-SIDE safety enforcement (guardrail 2): live_order is always
            # refused; network_read needs the server's --allow-network flag.
            ok, reason = run_tests.may_run(tool, allow_network)
            if not ok:
                self._json(403, {"error": "refused", "name": name, "reason": reason})
                return
            rec = run_tests.run_tool(tool, allow_network)
            if rec["status"] != "refused":
                run_tests.record(rec)
            self._json(200, rec)

        def handle_stream(self):
            qs = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        qs[k] = v
            try:
                backfill = max(0, min(20000, int(qs.get("backfill", backfill_default))))
            except ValueError:
                backfill = backfill_default

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def write(data):
                self.wfile.write(data.encode("utf-8"))
                self.wfile.flush()

            try:
                lines, start_pos = tail_lines(metrics_path, backfill)
                write("retry: 3000\n\n")
                if lines:
                    payload = json.dumps(lines, separators=(",", ":"))
                    write("event: backfill\ndata: " + payload + "\n\n")
                else:
                    write(": no-data-yet\n\n")

                last_ping = time.time()
                for line in stream_new_lines(metrics_path, start_pos):
                    if line is not None:
                        write("data: " + line.replace("\r", "") + "\n\n")
                    now = time.time()
                    if now - last_ping >= 15:
                        write(": ping\n\n")  # keepalive + dead-peer detection
                        last_ping = now
            except (BrokenPipeError, ConnectionResetError, OSError):
                return  # client went away; nothing to clean up

        def handle_updates_stream(self):
            qs = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        qs[k] = v
            try:
                backfill = max(0, min(1000, int(qs.get("backfill", 100))))
            except ValueError:
                backfill = 100

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def write(data):
                self.wfile.write(data.encode("utf-8"))
                self.wfile.flush()

            try:
                lines, start_pos = tail_lines(KALSHI_UPDATES, backfill)
                write("retry: 3000\n\n")
                if lines:
                    payload = json.dumps(lines, separators=(",", ":"))
                    write("event: kalshi_updates_backfill\ndata: " + payload + "\n\n")
                else:
                    write(": no-kalshi-updates-yet\n\n")

                last_ping = time.time()
                for line in stream_new_lines(KALSHI_UPDATES, start_pos):
                    if line is not None:
                        write("event: kalshi_update\ndata: " + line.replace("\r", "") + "\n\n")
                    now = time.time()
                    if now - last_ping >= 15:
                        write(": ping\n\n")
                        last_ping = now
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


# --------------------------------------------------------------------- HTML

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi PoC — Ops Console</title>
<style>
:root{
  --bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--border:#2a3139;--text:#c9d1d9;
  --dim:#8b949e;--head:#7d8590;
  --green:#2ea043;--greenbg:#12261a;--yellow:#c69026;--yellowbg:#2a2411;
  --red:#da3633;--redbg:#2b1213;--gray:#484f58;--graybg:#1b2028;--blue:#388bfd;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
  font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{display:flex;align-items:center;gap:14px;padding:8px 14px;
  background:var(--panel);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:5}
header h1{font-size:13px;margin:0;letter-spacing:.5px;font-weight:600}
header .conn{margin-left:auto;display:flex;gap:14px;align-items:center;color:var(--dim)}
main{padding:12px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
/* [hidden] on a <main> tabview loses to the author main{display:grid} rule, so
   an explicit higher-specificity rule is needed for tab switching to actually
   hide the Live tab (otherwise every tab stacks under it). */
.tabview[hidden]{display:none!important}
.wide{grid-column:1 / -1}
section{background:var(--panel);border:1px solid var(--border);border-radius:6px;overflow:hidden}
section>h2{margin:0;padding:7px 11px;font-size:11px;letter-spacing:.8px;text-transform:uppercase;
  color:var(--head);background:var(--panel2);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:8px}
section>h2 .sub{margin-left:auto;color:var(--dim);font-weight:400;text-transform:none;letter-spacing:0}
.body{padding:8px 11px;overflow:auto}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;align-items:center}
.kv .k{color:var(--dim)}
.kv .v{text-align:right;font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:3px 7px;white-space:nowrap;border-bottom:1px solid var(--border)}
th{color:var(--head);font-weight:600;position:sticky;top:0;background:var(--panel);z-index:1}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:var(--panel2)}
.scroll{max-height:260px;overflow:auto}
.scroll.tall{max-height:340px}
.badge{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;font-weight:600;
  border:1px solid transparent;line-height:1.5}
.b-green{color:#3fb950;background:var(--greenbg);border-color:#194b28}
.b-yellow{color:#d29922;background:var(--yellowbg);border-color:#493f13}
.b-red{color:#f85149;background:var(--redbg);border-color:#5c1e1f}
.b-gray{color:#8b949e;background:var(--graybg);border-color:#30363d}
.b-blue{color:#58a6ff;background:#0d2a4a;border-color:#1b3a5c}
.tabbtn{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer}
.tabbtn:hover{background:#30363d}
.tabbtn.active{background:#1f6feb;color:#fff;border-color:#1f6feb}
.tabbtn:disabled{opacity:.5;cursor:not-allowed}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;vertical-align:middle;margin-right:5px}
.d-green{background:var(--green)}.d-yellow{background:var(--yellow)}.d-red{background:var(--red)}.d-gray{background:var(--gray)}
.panel-controls{display:flex;flex-wrap:wrap;gap:7px;padding:9px 11px}
button{font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--border);
  border-radius:5px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--blue);color:#fff}
button:active{transform:translateY(1px)}
button.warn:hover{border-color:var(--yellow)}
button.danger:hover{border-color:var(--red)}
.controls-row{display:flex;flex-wrap:wrap;gap:12px;padding:8px 11px;align-items:center}
.controls-row label{color:var(--dim);display:flex;align-items:center;gap:5px}
input[type=text]{font:inherit;background:var(--bg);color:var(--text);border:1px solid var(--border);
  border-radius:5px;padding:4px 7px;width:120px}
select{font:inherit;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:5px;padding:4px}
.muted{color:var(--dim)}
.empty{color:var(--gray);padding:8px 2px}
.raw{cursor:pointer;color:var(--dim)}
.raw:hover{color:var(--blue)}
pre.json{margin:4px 0 0;padding:7px;background:var(--bg);border:1px solid var(--border);
  border-radius:5px;white-space:pre-wrap;word-break:break-all;color:#a5d6ff;font-size:11px}
.logline td{border-bottom:1px solid #20262d}
.age-old{color:var(--yellow)}.age-stale{color:var(--red)}
.notice{padding:9px 11px;color:var(--dim)}
.research-grid{display:grid;grid-template-columns:minmax(280px,1fr) minmax(360px,1.4fr);gap:12px}
.research-drop{border:2px dashed var(--border);border-radius:8px;padding:18px;text-align:center;
  background:#111820;cursor:pointer;transition:border-color .15s,background .15s}
.research-drop.drag{border-color:var(--blue);background:#0d2a4a}
.research-plan{width:100%;min-height:250px;margin-top:10px;resize:vertical;font:inherit;
  color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:9px}
.research-job{border:1px solid var(--border);border-radius:7px;padding:9px;background:#111820;margin-bottom:8px}
.research-job .meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.research-job .why{color:var(--dim);margin-top:5px;white-space:normal}
@media (max-width:900px){.research-grid{grid-template-columns:1fr}}
.truth{border-color:#493f13;background:#15130b}
.truth .body{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.truth-msg{color:var(--dim)}
.topgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px}
.lifecycle-grid{display:grid;grid-template-columns:repeat(7,minmax(120px,1fr));gap:8px}
.stage-card{border:1px solid var(--border);background:#111820;border-radius:6px;padding:8px;min-height:105px}
.stage-card .stage-title{font-weight:700;margin-bottom:6px}
.stage-card .stage-summary{color:var(--dim);font-size:11px;margin-top:7px;white-space:normal}
.updates-list{display:grid;gap:6px;max-height:190px;overflow:auto}
.line-list{display:grid;gap:6px}
.line-item{display:flex;gap:8px;align-items:flex-start;border:1px solid var(--border);
  background:#111820;border-radius:6px;padding:7px 8px}
.line-item .label{min-width:190px;font-weight:600}
.line-item .detail{color:var(--dim)}
.stack{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.market-controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:9px;padding:9px 11px;border-bottom:1px solid var(--border);background:#111820}
.slider-field{display:grid;grid-template-columns:1fr auto;gap:4px 8px;color:var(--dim);align-items:center}
.slider-field input{grid-column:1 / -1;width:100%}
.slider-field output{color:var(--text);font-variant-numeric:tabular-nums}
input[type=range]{accent-color:var(--blue)}
.market-line{font-variant-numeric:tabular-nums}
.market-book{color:var(--dim)}
.market-hot{color:#3fb950}.market-watch{color:#d29922}.market-stale{color:#f85149}
@media (max-width:1100px){.lifecycle-grid{grid-template-columns:repeat(auto-fill,minmax(170px,1fr))}}
</style>
</head>
<body>
<header>
  <h1>KALSHI PoC · OPS CONSOLE</h1>
  <span class="badge b-gray" id="hdr-mode">mode —</span>
  <span class="badge b-gray" id="hdr-env">env —</span>
  <span class="badge b-gray" id="hdr-kill">kill —</span>
  <div class="conn">
    <span id="evt-rate">0 evt/s</span>
    <span id="evt-count">0 events</span>
    <span><span class="dot d-gray" id="conn-dot"></span><span id="conn-txt">connecting…</span></span>
  </div>
</header>

<nav id="tabnav" style="display:flex;gap:6px;align-items:center;padding:6px 12px;border-bottom:1px solid #2a2a3a">
  <button data-tab="live" class="tabbtn active">Live</button>
  <button data-tab="research" class="tabbtn">Research</button>
  <button data-tab="tests" class="tabbtn">Tests</button>
  <button data-tab="tools" class="tabbtn">Tools</button>
  <span id="tab-note" style="margin-left:auto;color:#888;font-size:12px"></span>
</nav>

<main id="tab-live" class="tabview">
  <section class="wide">
    <h2>Lifecycle Readiness <span class="sub" id="lifecycle-summary">single source: work/lifecycle_status.json</span></h2>
    <div class="body">
      <div class="stack" style="margin-bottom:8px">
        <button id="lifecycle-run" class="tabbtn">Run lifecycle check</button>
        <button id="lifecycle-run-core" class="tabbtn">Run lifecycle + core tests</button>
        <span class="muted">stages are generated by tools/lifecycle_check.py; skipped network checks are not failures</span>
      </div>
      <div id="lifecycle-grid" class="lifecycle-grid"></div>
    </div>
  </section>

  <section class="wide">
    <h2>Kalshi API Updates <span class="sub">public docs watcher events</span></h2>
    <div class="body">
      <div id="kalshi-updates" class="updates-list">
        <div class="muted">No Kalshi update events yet. Run the watcher from lifecycle with --allow-network or as a separate ops process.</div>
      </div>
    </div>
  </section>

  <section class="wide truth">
    <h2>Live Truth <span class="sub">what this page can actually prove</span></h2>
    <div class="body">
      <span id="truth-badge" class="badge b-gray">NO DATA</span>
      <span id="truth-msg" class="truth-msg">waiting for real telemetry</span>
    </div>
  </section>

  <section class="wide">
    <h2>Data Pipeline <span class="sub" id="pipe-summary">24/7 all-markets firehose</span></h2>
    <div class="body">
      <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:8px">
        <span id="pipe-dot" style="width:12px;height:12px;border-radius:50%;background:#666;display:inline-block"></span>
        <b id="pipe-state">checking…</b>
        <button id="pipe-activate" onclick="pipeControl('activate')"
          style="background:#1a7f37;color:#fff;border:0;border-radius:6px;padding:6px 14px;cursor:pointer">▶ Activate</button>
        <button id="pipe-stop" onclick="pipeControl('stop')"
          style="background:#8a1f1f;color:#fff;border:0;border-radius:6px;padding:6px 14px;cursor:pointer">■ Stop</button>
        <span class="sub" id="pipe-hint"></span>
      </div>
      <div id="pipe-metrics" class="line-list" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px"></div>
    </div>
  </section>

  <section class="wide">
    <h2>Market Feed Readiness <span class="sub" id="feed-ready-summary">local precheck, no network</span></h2>
    <div class="body">
      <div id="feed-ready-body" class="line-list">
        <div class="muted">checking local feed prerequisites…</div>
      </div>
    </div>
  </section>

  <section class="wide">
    <h2>Core Test Status <span class="sub" id="top-tests-summary">core tests only</span></h2>
    <div class="body">
      <div class="stack" style="margin-bottom:8px">
        <button id="top-run-all" class="tabbtn">Run core tests</button>
        <span class="muted">pure/offline tests only; probes and benches stay in Tools</span>
      </div>
      <div id="top-tests-grid" class="topgrid"></div>
    </div>
  </section>

  <!-- 1. System Status -->
  <section>
    <h2>System Status <span class="sub" id="sys-heartbeat">heartbeat —</span></h2>
    <div class="body"><div class="kv" id="sys-kv"></div></div>
  </section>

  <!-- 5. Risk Status -->
  <section>
    <h2>Risk Status</h2>
    <div class="body"><div class="kv" id="risk-kv"></div></div>
  </section>

  <!-- 2. Feed Status -->
  <section class="wide">
    <h2>Feed Status</h2>
    <div class="body scroll">
      <table>
        <thead><tr>
          <th>source</th><th>connected</th><th>last msg age</th><th>freshness ms</th>
          <th>rate hz</th><th>gaps</th><th>reconnects</th><th>flag</th>
        </tr></thead>
        <tbody id="feed-tbody"><tr><td colspan="8" class="empty">no feed events yet</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="wide">
    <h2>Market Feed Tape <span class="sub" id="market-summary">live odds and top-of-book one-liners</span></h2>
    <div class="market-controls">
      <label class="slider-field">max age <output id="md-max-age-out">5.0s</output>
        <input id="md-max-age" type="range" min="100" max="30000" step="100" value="5000">
      </label>
      <label class="slider-field">max spread ¢ <output id="md-max-spread-out">20</output>
        <input id="md-max-spread" type="range" min="1" max="100" step="1" value="20">
      </label>
      <label class="slider-field">min top size <output id="md-min-size-out">0</output>
        <input id="md-min-size" type="range" min="0" max="5000" step="1" value="0">
      </label>
      <label class="slider-field">rows <output id="md-row-limit-out">30</output>
        <input id="md-row-limit" type="range" min="5" max="100" step="5" value="30">
      </label>
      <label style="color:var(--dim);display:flex;align-items:center;gap:6px"><input type="checkbox" id="md-hot-only"> hot only</label>
      <label style="color:var(--dim);display:flex;align-items:center;gap:6px">find <input type="text" id="md-find" placeholder="ticker…"></label>
    </div>
    <div class="body scroll tall">
      <table>
        <thead><tr>
          <th>time</th><th>ticker</th><th>YES bid/ask</th><th>spread ¢</th>
          <th>top size</th><th>age</th><th>source</th><th>state</th>
        </tr></thead>
        <tbody id="market-tbody"><tr><td colspan="8" class="empty">waiting for market-data events…</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- 4. Strategy Status -->
  <section class="wide">
    <h2>Strategy Status</h2>
    <div class="body scroll">
      <table>
        <thead><tr>
          <th>strategy</th><th>state</th><th>triggers</th><th>last trigger</th>
          <th>edge signal ¢</th><th>edge ack ¢</th><th>shadow pnl ¢</th><th>reason</th>
        </tr></thead>
        <tbody id="strat-tbody"><tr><td colspan="8" class="empty">no strategy events yet</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- 3. Order Status -->
  <section class="wide">
    <h2>Order Status <span class="sub">latest 200</span></h2>
    <div class="body scroll tall">
      <table>
        <thead><tr>
          <th>time</th><th>strategy</th><th>ticker</th><th>side</th><th>price</th><th>size</th>
          <th>mode</th><th>status</th><th>http</th><th>sign µs</th>
          <th>submit→ack ms</th><th>signal→ack ms</th><th>reason</th>
        </tr></thead>
        <tbody id="order-tbody"><tr><td colspan="13" class="empty">no order events yet</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- 6. Log Data -->
  <section class="wide">
    <h2>Event Log <span class="sub">latest 200 shown · <span id="log-total">0</span> retained</span></h2>
    <div class="controls-row">
      <label>filter:
        <select id="log-filter">
          <option value="">all</option>
          <option value="system">system</option>
          <option value="feed">feed</option>
          <option value="order">order</option>
          <option value="strategy">strategy</option>
          <option value="risk">risk</option>
        </select>
      </label>
      <label>find: <input type="text" id="log-find" placeholder="substring…"></label>
      <label><input type="checkbox" id="log-pause"> pause</label>
      <span class="muted">click a row to expand raw JSON</span>
    </div>
    <div class="body scroll tall">
      <table>
        <thead><tr><th>time</th><th>type</th><th>summary</th></tr></thead>
        <tbody id="log-tbody"><tr><td colspan="3" class="empty">waiting for events…</td></tr></tbody>
      </table>
    </div>
  </section>
</main>

<section id="tab-tests" class="tabview" hidden style="padding:12px">
  <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px">
    <button id="run-all" class="tabbtn">▶ Run all (pure/offline)</button>
    <span id="tests-summary" style="color:#888;font-size:12px"></span>
  </div>
  <div id="tests-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px"></div>
</section>

<section id="tab-tools" class="tabview" hidden style="padding:12px">
  <div id="warehouse-panel" style="border:1px solid #2a2a3a;border-radius:8px;padding:10px;margin-bottom:12px">
    <h2 style="margin:0 0 6px">Warehouse <span class="sub" id="wh-summary">cold research warehouse status (read-only)</span></h2>
    <div id="wh-body" class="kv">loading…</div>
  </div>
  <div id="tools-cards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px"></div>
</section>

<section id="tab-research" class="tabview" hidden style="padding:12px">
  <div class="research-grid">
    <section>
      <h2>New Research Job <span class="sub">Markdown → W09 → report</span></h2>
      <div class="body">
        <div id="research-drop" class="research-drop" tabindex="0">
          <b>Drop a Markdown research plan here</b><br>
          <span class="muted">or click to choose a .md/.txt file · max 2 MiB</span>
        </div>
        <input id="research-file" type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" hidden>
        <textarea id="research-plan" class="research-plan" placeholder="# Research title\n\nDescribe the hypothesis, required data, experiment, and desired report.\nOptional YAML frontmatter can select a registered method plugin."></textarea>
        <div class="stack" style="margin-top:9px">
          <button id="research-submit" class="tabbtn">Run research</button>
          <span id="research-filename" class="muted">PLAN.md</span>
          <span id="research-submit-status" class="muted"></span>
        </div>
        <div class="notice">Registered read-only methods auto-queue on W09. A method with an
          explicit cost/execution gate pauses at <code>READY</code> for one approval. Unknown
          methods stop at <code>NEEDS_METHOD</code>; plan text is never executed as Python or SQL.</div>
      </div>
    </section>
    <section>
      <h2>Research Jobs <span class="sub" id="research-count">loading…</span></h2>
      <div class="body scroll tall" id="research-jobs">
        <div class="muted">No research jobs yet.</div>
      </div>
    </section>
  </div>
</section>

<script>
"use strict";
const MAX_LOG = 1000, MAX_ORDERS = 200, LOG_VIEW = 200;

// ---- state ----
const state = {
  system:{}, start_ts:null, last_ts:0,
  feeds:new Map(), strategies:new Map(), markets:new Map(),
  orders:[], log:[],
  lifecycle:null, kalshiUpdates:[],
  synthetic:0, real:0, latestRealTs:0,
  risk:{rejects:{stale_signal:0,duplicate:0,risk_check:0,other:0}, fields:{}},
  evtWindow:[],
};
let dirty = {truth:1,sys:1,feed:1,market:1,strat:1,order:1,risk:1,log:1};
const $ = id => document.getElementById(id);

// ---- badge helpers ----
function badge(text, cls){ return '<span class="badge b-'+cls+'">'+esc(text)+'</span>'; }
function boolBadge(v){ return v ? badge('yes','green') : badge('no','red'); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmt(v,d){ return (v==null||v==='') ? '<span class="muted">—</span>' : esc(typeof v==='number'?(''+ (Math.round(v*Math.pow(10,d||0))/Math.pow(10,d||0))):v); }
function num(v){ return (v==null||isNaN(v))?'<span class="muted">—</span>':esc(v); }
function ageClass(ms){ return ms>3000?'age-stale':ms>1000?'age-old':''; }
function tstr(ms){ if(!ms) return '—'; const d=new Date(ms); return d.toLocaleTimeString('en-GB')+'.'+String(ms%1000).padStart(3,'0'); }
function agestr(ms){ if(ms==null) return '—'; if(ms<1000) return ms.toFixed(0)+'ms'; if(ms<60000) return (ms/1000).toFixed(1)+'s'; return (ms/60000).toFixed(1)+'m'; }
function pick(o,names){ for(const n of names){ if(o && o[n]!=null) return o[n]; } return null; }
function centsValue(v){
  if(v==null || v==='') return null;
  if(typeof v==='string') v = v.trim().replace(/^\$/, '');
  const n = Number(v);
  if(!Number.isFinite(n)) return null;
  return Math.max(0, Math.min(100, n<=1 ? n*100 : n));
}
function sizeValue(v){ const n=Number(v); return Number.isFinite(n)?n:null; }
function topLevel(levels){
  if(!Array.isArray(levels) || !levels.length) return null;
  const x = levels[0];
  if(Array.isArray(x)) return {price:centsValue(x[0]), size:sizeValue(x[1])};
  if(x && typeof x==='object') return {price:centsValue(pick(x,['price_cents','price','price_dollars','yes_price_dollars'])), size:sizeValue(pick(x,['size','count','quantity','contracts','size_fp']))};
  return null;
}
function firstLevelFrom(o,names){ for(const n of names){ const lvl=topLevel(o[n]); if(lvl && lvl.price!=null) return lvl; } return null; }
function marketTicker(o){ return pick(o,['ticker','market_ticker','source_ticker','market','market_id']); }
function fmtCents(v,d){ return v==null?'<span class="muted">—</span>':esc((Math.round(v*Math.pow(10,d||0))/Math.pow(10,d||0)).toFixed(d||0)); }

// ---- ingest ----
function ingest(o){
  if(!o||typeof o!=='object'||!o.type) return;
  const ts = o.ts_ms||Date.now();
  state.last_ts = Math.max(state.last_ts, ts);
  state.evtWindow.push(Date.now());
  if(o.synthetic){
    state.synthetic++;
  }else{
    state.real++;
    state.latestRealTs = Math.max(state.latestRealTs, ts);
  }
  dirty.truth=1;

  // log ring
  state.log.push(o);
  if(state.log.length>MAX_LOG) state.log.splice(0, state.log.length-MAX_LOG);
  dirty.log=1;
  if(rememberMarket(o, ts)) dirty.market=1;

  switch(o.type){
    case 'system':
      Object.assign(state.system, o);
      if(state.start_ts==null) state.start_ts = ts;
      if(o.status && /start/i.test(o.status+' '+(o.message||''))) state.start_ts = state.start_ts||ts;
      dirty.sys=1; break;
    case 'feed':
      if(o.source){ state.feeds.set(o.source, o); dirty.feed=1; dirty.sys=1; }
      break;
    case 'strategy':
      if(o.name){
        const prev = state.strategies.get(o.name)||{};
        state.strategies.set(o.name, Object.assign({}, prev, o, {last_ts:ts}));
        dirty.strat=1;
      }
      break;
    case 'order':
      state.orders.push(o);
      if(state.orders.length>MAX_ORDERS) state.orders.splice(0, state.orders.length-MAX_ORDERS);
      dirty.order=1; dirty.risk=1; break;
    case 'risk':
      const r = state.risk;
      if(o.decision==='reject' || o.reason){
        const key = ({stale_signal:'stale_signal',stale:'stale_signal',duplicate:'duplicate',
          dup:'duplicate',risk_check:'risk_check',risk:'risk_check'})[o.reason] || 'other';
        r.rejects[key] = (r.rejects[key]||0)+1;
      }
      // carry any summary fields present (max_orders_per_sec, exposure, kill_switch, ...)
      for(const k of Object.keys(o)) if(!['type','ts_ms','decision','reason','ticker','strategy'].includes(k)) r.fields[k]=o[k];
      dirty.risk=1; break;
  }
}

// ---- derived system view ----
function feedFor(...subs){
  for(const [src,ev] of state.feeds) for(const s of subs) if(src.toLowerCase().includes(s)) return ev;
  return null;
}
function statusFromFeed(ev){
  if(!ev) return badge('unknown','gray');
  const age = Date.now()-(ev.ts_ms||0);
  if(!ev.connected) return badge('down','red');
  if(ev.valid===false || age>3000) return badge('degraded','yellow');
  return badge('up','green');
}

// ---- renderers ----
function renderTruth(){
  const now = Date.now();
  const realAge = state.latestRealTs ? now - state.latestRealTs : null;
  const latestAge = state.last_ts ? now - state.last_ts : null;
  const hasSynth = state.synthetic > 0;
  let label='NO LIVE ENGINE', cls='gray';
  if(realAge!=null && realAge <= 5000){ label='REAL TELEMETRY'; cls='green'; }
  else if(realAge!=null){ label='STALE REAL TELEMETRY'; cls='red'; }
  else if(hasSynth){ label='SYNTHETIC ONLY'; cls='yellow'; }
  $('truth-badge').outerHTML = '<span id="truth-badge" class="badge b-'+cls+'">'+label+'</span>';
  const parts=[];
  if(realAge==null) parts.push('No non-synthetic engine heartbeat/order/feed has arrived in this browser session.');
  else parts.push('Latest real event was '+agestr(realAge)+' ago.');
  if(latestAge!=null && latestAge>5000) parts.push('The newest event in the metrics file is stale: '+agestr(latestAge)+' old.');
  if(hasSynth) parts.push(state.synthetic+' synthetic/sample rows were seen and must not be treated as live state.');
  if(!state.log.length) parts.push('metrics.ndjson is empty; start tradingd with TRADINGD_NDJSON=work/metrics.ndjson to feed this page.');
  $('truth-msg').textContent = parts.join(' ');
}

function renderSys(){
  const s = state.system;
  const now = Date.now();
  const hbAge = state.last_ts?now-state.last_ts:null;
  const proc = hbAge==null?badge('unknown','gray'):hbAge>5000?badge('stale','red'):hbAge>2000?badge('lagging','yellow'):badge('running','green');
  const up = state.start_ts?agestr(now-state.start_ts):'—';
  const kill = (s.kill_switch!=null)?s.kill_switch:(state.risk.fields.kill_switch);
  const killBadge = kill==null?badge('unknown','gray'):(kill===true||kill==='engaged'||kill==='on')?badge('ENGAGED','red'):badge('clear','green');
  const rows = [
    ['process', proc],
    ['mode', s.mode?badge(s.mode, s.mode==='live'?'red':s.mode==='canary'?'yellow':'blue'):badge('—','gray')],
    ['uptime', esc(up)],
    ['Kalshi REST', statusFromFeed(feedFor('rest'))],
    ['Kalshi WebSocket', statusFromFeed(feedFor('ws','websocket'))],
    ['external provider', statusFromFeed(feedFor('provider','external'))],
    ['last heartbeat', hbAge==null?'<span class="muted">—</span>':'<span class="'+ageClass(hbAge)+'">'+agestr(hbAge)+' ago</span>'],
    ['kill switch', killBadge],
    ['config profile', fmt(s.profile||state.risk.fields.profile)],
    ['API environment', s.env?badge(s.env, s.env==='prod'?'red':'blue'):badge('—','gray')],
    ['component', fmt(s.component)],
    ['last message', fmt(s.message)],
  ];
  $('sys-kv').innerHTML = rows.map(([k,v])=>'<div class="k">'+k+'</div><div class="v">'+v+'</div>').join('');
  $('sys-heartbeat').textContent = hbAge==null?'heartbeat —':'heartbeat '+agestr(hbAge)+' ago';
  $('hdr-mode').outerHTML = '<span class="badge b-'+(s.mode==='live'?'red':s.mode==='canary'?'yellow':s.mode?'blue':'gray')+'" id="hdr-mode">mode '+esc(s.mode||'—')+'</span>';
  $('hdr-env').outerHTML = '<span class="badge b-'+(s.env==='prod'?'red':s.env?'blue':'gray')+'" id="hdr-env">env '+esc(s.env||'—')+'</span>';
  const kEng = (kill===true||kill==='engaged'||kill==='on');
  $('hdr-kill').outerHTML = '<span class="badge b-'+(kEng?'red':kill==null?'gray':'green')+'" id="hdr-kill">kill '+(kEng?'ENGAGED':kill==null?'—':'clear')+'</span>';
}

function renderRisk(){
  const f = state.risk.fields, rj = state.risk.rejects;
  // current order rate: orders in the last second
  const now=Date.now(); const rate = state.orders.filter(o=>now-(o.ts_ms||0)<1000).length;
  const kill = f.kill_switch;
  const rows = [
    ['max orders/sec', fmt(f.max_orders_per_sec)],
    ['current order rate', esc(rate)+'/s'],
    ['max position / market', fmt(f.max_position_per_market)],
    ['current exposure', fmt(f.current_exposure!=null?f.current_exposure:f.exposure)],
    ['rejected: stale signals', badge(rj.stale_signal, rj.stale_signal?'yellow':'gray')],
    ['rejected: duplicates', badge(rj.duplicate, rj.duplicate?'yellow':'gray')],
    ['rejected: risk checks', badge(rj.risk_check, rj.risk_check?'red':'gray')],
    ['rejected: other', badge(rj.other, rj.other?'yellow':'gray')],
    ['kill switch', kill==null?badge('unknown','gray'):(kill===true||kill==='engaged'||kill==='on')?badge('ENGAGED','red'):badge('clear','green')],
  ];
  $('risk-kv').innerHTML = rows.map(([k,v])=>'<div class="k">'+k+'</div><div class="v">'+v+'</div>').join('');
}

function renderFeed(){
  const rows=[...state.feeds.values()].sort((a,b)=>(a.source||'').localeCompare(b.source||''));
  const now=Date.now();
  const tb=$('feed-tbody');
  if(!rows.length){ tb.innerHTML='<tr><td colspan="8" class="empty">no feed events yet</td></tr>'; return; }
  tb.innerHTML = rows.map(f=>{
    const age = f.age_ms!=null?f.age_ms:(now-(f.ts_ms||now));
    const flag = (f.valid===false)?badge('STALE','red'):badge('valid','green');
    return '<tr><td>'+esc(f.source)+'</td><td>'+boolBadge(f.connected)+'</td>'+
      '<td class="'+ageClass(age)+'">'+agestr(age)+'</td><td>'+fmt(f.freshness_ms,1)+'</td>'+
      '<td>'+fmt(f.msg_rate_hz,1)+'</td><td>'+num(f.gaps)+'</td><td>'+num(f.reconnects)+'</td><td>'+flag+'</td></tr>';
  }).join('');
}

function rememberMarket(o, ts){
  if(o.synthetic) return false;
  const t = marketTicker(o);
  if(!t) return false;
  const type = String(o.type||'').toLowerCase();
  const likelyMarket = /market|quote|ticker|book|orderbook|feed|trade/.test(type) ||
    o.yes_bid!=null || o.yes_ask!=null || o.best_bid!=null || o.best_ask!=null ||
    o.bids!=null || o.asks!=null || o.yes_bids!=null || o.yes_asks!=null;
  if(!likelyMarket) return false;

  const prev = state.markets.get(String(t)) || {ticker:String(t), updates:0};
  const book = o.orderbook || o.book || {};
  const bidLvl = firstLevelFrom(o,['yes_bids','bids','bid_levels']) || firstLevelFrom(book,['yes','yes_dollars','bids','bid_levels']);
  const askLvl = firstLevelFrom(o,['yes_asks','asks','ask_levels']) || firstLevelFrom(book,['asks','ask_levels']);
  const noBidLvl = firstLevelFrom(o,['no_bids','no','no_levels']) || firstLevelFrom(book,['no','no_dollars']);

  let bid = centsValue(pick(o,['yes_bid_cents','yes_bid','yes_bid_dollars','best_bid_cents','best_bid','bid_cents','bid','bid_dollars']));
  let ask = centsValue(pick(o,['yes_ask_cents','yes_ask','yes_ask_dollars','best_ask_cents','best_ask','ask_cents','ask','ask_dollars']));
  const noBid = centsValue(pick(o,['no_bid','no_bid_cents','no_bid_dollars']));
  const noAsk = centsValue(pick(o,['no_ask','no_ask_cents','no_ask_dollars']));
  if(bid==null && noAsk!=null) bid = 100 - noAsk;
  if(ask==null && noBid!=null) ask = 100 - noBid;
  if(bid==null && bidLvl) bid = bidLvl.price;
  if(ask==null && askLvl) ask = askLvl.price;
  if(ask==null && noBidLvl) ask = 100 - noBidLvl.price;

  const bidSize = sizeValue(pick(o,['yes_bid_size','yes_bid_size_fp','bid_size','best_bid_size'])) ?? (bidLvl && bidLvl.size) ?? prev.bid_size;
  const askSize = sizeValue(pick(o,['yes_ask_size','yes_ask_size_fp','ask_size','best_ask_size'])) ?? (askLvl && askLvl.size) ?? (noBidLvl && noBidLvl.size) ?? prev.ask_size;
  const spread = (bid!=null && ask!=null) ? Math.max(0, ask-bid) : prev.spread_cents;
  const source = o.source || o.channel || o.type || prev.source || 'market_data';
  const valid = o.valid!==false && (bid==null || ask==null || bid<=ask);

  state.markets.set(String(t), Object.assign({}, prev, {
    ticker:String(t), ts_ms:ts, source, channel:o.channel||prev.channel,
    yes_bid_cents:bid ?? prev.yes_bid_cents, yes_ask_cents:ask ?? prev.yes_ask_cents,
    bid_size:bidSize, ask_size:askSize, spread_cents:spread,
    last_price_cents:centsValue(pick(o,['last_price','last_price_cents','last_price_dollars','price','price_dollars'])) ?? prev.last_price_cents,
    freshness_ms:o.freshness_ms ?? o.age_ms ?? prev.freshness_ms,
    valid, quality:o.quality || o.state || prev.quality, updates:(prev.updates||0)+1
  }));
  return true;
}

function renderMarketControls(){
  $('md-max-age-out').textContent = agestr(Number($('md-max-age').value));
  $('md-max-spread-out').textContent = $('md-max-spread').value;
  $('md-min-size-out').textContent = $('md-min-size').value;
  $('md-row-limit-out').textContent = $('md-row-limit').value;
}
function marketState(m, age, maxAge, maxSpread){
  if(m.valid===false) return ['invalid','red','market-stale'];
  if(age>maxAge) return ['stale','red','market-stale'];
  if(m.spread_cents!=null && m.spread_cents<=maxSpread) return ['hot','green','market-hot'];
  return ['watch','yellow','market-watch'];
}
function renderMarkets(){
  renderMarketControls();
  const now=Date.now(), maxAge=Number($('md-max-age').value), maxSpread=Number($('md-max-spread').value);
  const minSize=Number($('md-min-size').value), limit=Number($('md-row-limit').value);
  const hotOnly=$('md-hot-only').checked, find=$('md-find').value.trim().toLowerCase();
  const all=[...state.markets.values()].map(m=>Object.assign({age_ms:now-(m.ts_ms||now)},m));
  let hot=0, stale=0;
  let rows=all.filter(m=>{
    const st=marketState(m,m.age_ms,maxAge,maxSpread);
    if(st[0]==='hot') hot++; if(st[0]==='stale') stale++;
    if(find && !m.ticker.toLowerCase().includes(find)) return false;
    if(hotOnly && st[0]!=='hot') return false;
    if(m.age_ms>maxAge) return false;
    if(m.spread_cents!=null && m.spread_cents>maxSpread) return false;
    const top=Math.max(Number(m.bid_size||0), Number(m.ask_size||0));
    if(top<minSize) return false;
    return true;
  }).sort((a,b)=>(b.ts_ms||0)-(a.ts_ms||0)).slice(0,limit);
  $('market-summary').textContent = rows.length+'/'+all.length+' shown · '+hot+' hot · '+stale+' stale';
  const tb=$('market-tbody');
  if(!rows.length){ tb.innerHTML='<tr><td colspan="8" class="empty">no market rows match the sliders</td></tr>'; return; }
  tb.innerHTML = rows.map(m=>{
    const st=marketState(m,m.age_ms,maxAge,maxSpread);
    const odds=fmtCents(m.yes_bid_cents,1)+' / '+fmtCents(m.yes_ask_cents,1);
    const sizes=(m.bid_size==null && m.ask_size==null)?'<span class="muted">—</span>':esc((m.bid_size??'—')+' x '+(m.ask_size??'—'));
    const src=m.channel ? (m.source+'/'+m.channel) : m.source;
    return '<tr class="market-line"><td>'+tstr(m.ts_ms)+'</td><td>'+esc(m.ticker)+'</td>'+
      '<td>'+odds+'</td><td>'+fmtCents(m.spread_cents,1)+'</td><td class="market-book">'+sizes+'</td>'+
      '<td class="'+ageClass(m.age_ms)+'">'+agestr(m.age_ms)+'</td><td>'+esc(src)+'</td>'+
      '<td>'+badge(st[0],st[1])+'</td></tr>';
  }).join('');
}

function renderStrat(){
  const rows=[...state.strategies.values()].sort((a,b)=>(a.name||'').localeCompare(b.name||''));
  const tb=$('strat-tbody');
  if(!rows.length){ tb.innerHTML='<tr><td colspan="8" class="empty">no strategy events yet</td></tr>'; return; }
  tb.innerHTML = rows.map(s=>{
    const st = s.enabled===false?badge('disabled','gray'):badge('enabled','green');
    const pnl = s.shadow_pnl_cents;
    const pnlCell = pnl==null?'<span class="muted">—</span>':'<span class="'+(pnl>0?'':pnl<0?'':'')+'" style="color:'+(pnl>0?'#3fb950':pnl<0?'#f85149':'inherit')+'">'+esc(pnl)+'</span>';
    return '<tr><td>'+esc(s.name)+'</td><td>'+st+'</td><td>'+num(s.triggers)+'</td>'+
      '<td>'+tstr(s.last_ts)+'</td><td>'+fmt(s.edge_signal_cents,2)+'</td><td>'+fmt(s.edge_ack_cents,2)+'</td>'+
      '<td>'+pnlCell+'</td><td>'+fmt(s.reason)+'</td></tr>';
  }).join('');
}

function orderStatusBadge(o){
  const s=(o.status||'').toLowerCase();
  if(o.http_status>=400 || s==='rejected'||s==='error') return badge(o.status||'error','red');
  if(s==='would_send'||s==='shadow') return badge(o.status,'gray');
  if(s==='resting'||s==='pending'||s==='sent') return badge(o.status,'yellow');
  if(s==='filled'||s==='accepted'||s==='canceled') return badge(o.status,'green');
  return badge(o.status||'—','gray');
}
function renderOrders(){
  const tb=$('order-tbody');
  if(!state.orders.length){ tb.innerHTML='<tr><td colspan="13" class="empty">no order events yet</td></tr>'; return; }
  const rows=state.orders.slice(-MAX_ORDERS).reverse();
  tb.innerHTML = rows.map(o=>{
    const httpCell = !o.http_status?'<span class="muted">—</span>':(o.http_status>=400?badge(o.http_status,'red'):badge(o.http_status,'green'));
    return '<tr><td>'+tstr(o.ts_ms)+'</td><td>'+esc(o.strategy)+'</td><td>'+esc(o.ticker)+'</td>'+
      '<td>'+esc(o.side)+'</td><td>'+num(o.price)+'</td><td>'+num(o.size)+'</td>'+
      '<td>'+fmt(o.mode)+'</td><td>'+orderStatusBadge(o)+'</td><td>'+httpCell+'</td>'+
      '<td>'+num(o.sign_us)+'</td><td>'+fmt(o.submit_to_ack_ms,2)+'</td><td>'+fmt(o.signal_to_ack_ms,2)+'</td>'+
      '<td>'+fmt(o.reason)+'</td></tr>';
  }).join('');
}

function logSummary(o){
  switch(o.type){
    case 'system': return (o.component||'')+' · '+(o.status||'')+' · '+(o.message||'');
    case 'feed': return (o.source||'')+' connected='+o.connected+' fresh='+(o.freshness_ms==null?'—':o.freshness_ms)+'ms gaps='+(o.gaps==null?'—':o.gaps);
    case 'market': case 'market_data': case 'quote': case 'ticker': case 'orderbook':
      return (marketTicker(o)||'')+' bid='+(pick(o,['yes_bid','yes_bid_dollars','best_bid','bid','bid_dollars'])??'—')+
        ' ask='+(pick(o,['yes_ask','yes_ask_dollars','best_ask','ask','ask_dollars'])??'—');
    case 'order': return (o.strategy||'')+' '+(o.side||'')+' '+(o.ticker||'')+' @'+(o.price==null?'—':o.price)+' → '+(o.status||'')+(o.http_status?' ('+o.http_status+')':'');
    case 'strategy': return (o.name||'')+' triggers='+(o.triggers==null?'—':o.triggers)+' edge='+(o.edge_signal_cents==null?'—':o.edge_signal_cents)+'¢ '+(o.reason||'');
    case 'risk': return (o.decision||'')+' '+(o.reason||'')+' '+(o.ticker||'')+' '+(o.strategy||'');
    default: return JSON.stringify(o).slice(0,120);
  }
}
function renderLog(){
  if($('log-pause').checked) return;
  const filter=$('log-filter').value, find=$('log-find').value.toLowerCase();
  let items=state.log;
  if(filter) items=items.filter(o=>o.type===filter);
  if(find) items=items.filter(o=>JSON.stringify(o).toLowerCase().includes(find));
  items=items.slice(-LOG_VIEW).reverse();
  $('log-total').textContent=state.log.length;
  const tb=$('log-tbody');
  if(!items.length){ tb.innerHTML='<tr><td colspan="3" class="empty">no matching events</td></tr>'; return; }
  const typeBadge={system:'blue',feed:'green',order:'yellow',strategy:'gray',risk:'red'};
  tb.innerHTML=items.map((o,i)=>{
    const cls=typeBadge[o.type]||'gray';
    const synth=o.synthetic?(' '+badge('synthetic','yellow')):'';
    return '<tr class="logline" data-i="'+i+'"><td>'+tstr(o.ts_ms)+'</td><td>'+badge(o.type,cls)+'</td>'+
      '<td class="raw">'+synth+' '+esc(logSummary(o))+'</td></tr>';
  }).join('');
  // stash for expansion
  tb._items=items;
}

// expandable raw JSON
$('log-tbody').addEventListener('click', e=>{
  const tr=e.target.closest('tr.logline'); if(!tr) return;
  const items=$('log-tbody')._items||[]; const o=items[+tr.dataset.i]; if(!o) return;
  const next=tr.nextElementSibling;
  if(next && next.classList.contains('rawrow')){ next.remove(); return; }
  const r=document.createElement('tr'); r.className='rawrow';
  r.innerHTML='<td colspan="3"><pre class="json">'+esc(JSON.stringify(o,null,2))+'</pre></td>';
  tr.after(r);
});

// ---- render loop (throttled; decoupled from ingest rate) ----
setInterval(()=>{
  if(dirty.truth){ renderTruth(); dirty.truth=0; }
  if(dirty.sys){ renderSys(); dirty.sys=0; }
  if(dirty.risk){ renderRisk(); renderSys(); dirty.risk=0; }   // risk feeds kill badge
  if(dirty.feed){ renderFeed(); dirty.feed=0; }
  if(dirty.market){ renderMarkets(); dirty.market=0; }
  if(dirty.strat){ renderStrat(); dirty.strat=0; }
  if(dirty.order){ renderOrders(); dirty.order=0; }
  if(dirty.log){ renderLog(); dirty.log=0; }
}, 250);
// ages tick even without new events
setInterval(()=>{ dirty.truth=1; dirty.sys=1; dirty.feed=1; dirty.market=1; }, 1000);
// event-rate meter
setInterval(()=>{
  const now=Date.now(); state.evtWindow=state.evtWindow.filter(t=>now-t<1000);
  $('evt-rate').textContent=state.evtWindow.length+' evt/s';
  $('evt-count').textContent=state.log.length+' events';
}, 500);

// ---- SSE ----
let es;
function connect(){
  es = new EventSource('/stream?backfill=1000');
  es.addEventListener('backfill', e=>{
    try{ const arr=JSON.parse(e.data); for(const line of arr){ try{ ingest(JSON.parse(line)); }catch(_){} } }catch(_){}
  });
  es.onmessage = e=>{ try{ ingest(JSON.parse(e.data)); }catch(_){} };
  es.onopen = ()=>{ $('conn-dot').className='dot d-green'; $('conn-txt').textContent='connected'; };
  es.onerror = ()=>{ $('conn-dot').className='dot d-red'; $('conn-txt').textContent='reconnecting…'; };
}
connect();

function ingestKalshiUpdate(o){
  if(!o || typeof o !== 'object') return;
  state.kalshiUpdates.push(o);
  if(state.kalshiUpdates.length>200) state.kalshiUpdates.splice(0, state.kalshiUpdates.length-200);
  renderKalshiUpdates();
}
function connectKalshiUpdates(){
  const updates = new EventSource('/updates-stream?backfill=100');
  updates.addEventListener('kalshi_updates_backfill', e=>{
    try{
      const arr = JSON.parse(e.data);
      for(const line of arr){ try{ ingestKalshiUpdate(JSON.parse(line)); }catch(_){} }
    }catch(_){}
  });
  updates.addEventListener('kalshi_update', e=>{ try{ ingestKalshiUpdate(JSON.parse(e.data)); }catch(_){} });
}
connectKalshiUpdates();

// ---- Tests / Tools tabs (P2) ----
function showTab(name){
  document.querySelectorAll('.tabview').forEach(v=>v.hidden = (v.id!=='tab-'+name));
  document.querySelectorAll('.tabbtn[data-tab]').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  if(name==='tests') loadTests();
  if(name==='tools'){ loadTools(); loadWarehouse(); }
  if(name==='research') loadResearchJobs();
}
async function loadWarehouse(){
  const el = document.getElementById('wh-body');
  const sum = document.getElementById('wh-summary');
  try{
    const w = await apiJson('/api/warehouse');
    if(!w || !w.present){
      sum.textContent = 'not built yet';
      el.innerHTML = '<div class="muted">No data yet. Start the pipeline (tools/pipeline_supervisor.sh) or ingest a capture via tools/ingest.py.</div>';
      return;
    }
    const lr = w.last_run||{}, rc = w.row_counts||{};
    const order = ['orderbooks_l1','orderbooks_full','trades','markets','events','market_settlements','rfq_events'];
    const counts = order.map(t=>'<span style="margin-right:12px">'+t+'=<b>'+(rc[t]||0)+'</b></span>').join('');
    sum.innerHTML = 'last run '+esc(lr.run_id||'—')+' · '+esc(lr.finished_at||'—');
    const sourceKind = w.source_kind || 'unknown';
    const sourceBadgeColor = sourceKind==='operator_capture' ? 'green' :
      sourceKind==='synthetic_fixture' ? 'yellow' : 'gray';
    let html = '<div>'
      + (w.schema_ok ? badge('schema ok','green') : badge('schema FAIL','red')) + ' '
      + (w.idempotent_replace ? badge('idempotent','green') : badge('append','gray'))
      + ' ' + badge('source '+sourceKind.replace('_',' '), sourceBadgeColor)
      + ' <span class="muted">latest partition '+esc(w.latest_partition_date||'—')+' ('+(w.partitions||0)+' parts)</span></div>';
    html += '<div style="margin-top:6px">'+counts+'</div>';
    if(w.missing_categories && w.missing_categories.length)
      html += '<div style="margin-top:6px;color:#d0a000">missing (need REST catalog snapshot): '+esc(w.missing_categories.join(', '))+'</div>';
    if(w.warnings && w.warnings.length)
      html += '<div style="margin-top:6px;color:#d0a000">'+esc(w.warnings.join('; '))+'</div>';
    el.innerHTML = html;
  }catch(e){ el.innerHTML = errorBox('warehouse status unavailable: '+e.message); }
}
document.querySelectorAll('.tabbtn[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));

function stbadge(s){
  const c = s==='pass'?'green':s==='fail'?'red':s==='blocked'?'yellow':
    s==='skipped'?'gray':s==='not_started'?'gray':'gray';
  return badge((s||'—').replace('_',' '), c);
}
function isCoreTest(t){
  if(!(t.kind==='test'||t.kind==='check')) return false;
  if(!(t.safety==='pure'||t.safety==='offline')) return false;
  if(t.name==='run_pipeline'||t.name==='run_tests'||t.name==='lifecycle_check') return false;
  if(t.name.indexOf('run_')===0) return false;  // wrapper scripts live in Tools
  return true;
}
function errorBox(msg){
  return '<div style="border:1px solid #5c1e1f;background:#2b1213;color:#f85149;border-radius:6px;padding:8px">'
    + esc(msg) + '</div>';
}
async function apiJson(path){
  const res = await fetch(path, {cache:'no-store'});
  const text = await res.text();
  if(!res.ok) throw new Error(path+' returned HTTP '+res.status+': '+text.slice(0,160));
  try { return JSON.parse(text || '{}'); }
  catch(e) { throw new Error(path+' returned invalid JSON: '+text.slice(0,160)); }
}
async function postJson(path, obj){
  const res = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(obj||{}),cache:'no-store'});
  const text = await res.text();
  if(!res.ok) throw new Error(path+' returned HTTP '+res.status+': '+text.slice(0,160));
  try { return JSON.parse(text || '{}'); }
  catch(e) { throw new Error(path+' returned invalid JSON: '+text.slice(0,160)); }
}
function renderLifecycle(data){
  state.lifecycle = data;
  const stages = (data && data.stages) || [];
  const overall = data && data.status ? data.status : 'not_started';
  const age = data && data.generated_at_ms ? agestr(Date.now()-data.generated_at_ms)+' ago' : 'never';
  $('lifecycle-summary').innerHTML = stbadge(overall)+' generated '+esc(age)
    +(data && data.allow_network ? ' · network checks enabled' : ' · network checks skipped/blocked');
  $('lifecycle-grid').innerHTML = stages.length ? stages.map(s=>{
    return '<div class="stage-card">'
      +'<div class="stage-title">'+esc(s.label||s.id)+'</div>'
      +stbadge(s.status)
      +'<div class="stage-summary">'+esc(s.summary||s.blocking_reason||'')+'</div>'
      +(s.evidence_log?'<div class="stage-summary">evidence: '+esc(s.evidence_log)+'</div>':'')
      +'</div>';
  }).join('') : '<div class="muted">Lifecycle check has not run yet.</div>';
}
async function loadLifecycle(){
  try{
    renderLifecycle(await apiJson('/api/lifecycle'));
  }catch(e){
    $('lifecycle-summary').textContent='API error';
    $('lifecycle-grid').innerHTML=errorBox('Could not load lifecycle status. '+e.message);
  }
}
async function runLifecycle(runCore){
  $('lifecycle-run').disabled=true; $('lifecycle-run-core').disabled=true;
  $('lifecycle-summary').textContent = runCore ? 'running lifecycle and core tests...' : 'running lifecycle check...';
  try{
    renderLifecycle(await postJson('/api/lifecycle/run', {run_core_tests:!!runCore}));
    await loadTests();
  }catch(e){
    $('lifecycle-summary').textContent='API error';
    $('lifecycle-grid').innerHTML=errorBox('Could not run lifecycle check. '+e.message);
  }finally{
    $('lifecycle-run').disabled=false; $('lifecycle-run-core').disabled=false;
  }
}
function renderKalshiUpdates(){
  const rows = state.kalshiUpdates.slice(-20).reverse();
  if(!rows.length){
    $('kalshi-updates').innerHTML = '<div class="muted">No Kalshi update events yet. Run the watcher from lifecycle with --allow-network or as a separate ops process.</div>';
    return;
  }
  $('kalshi-updates').innerHTML = rows.map(u=>{
    const kind = u.kind || u.type || 'update';
    const title = u.title || u.source || u.url || '';
    const detail = u.link || u.url || u.new_sha256 || u.guid || '';
    return '<div class="line-item"><div class="label">'+esc(kind)+'</div>'
      +'<div>'+badge(u.source||'docs','blue')+'</div>'
      +'<div class="detail">'+esc(title)+' '+esc(detail)+'</div></div>';
  }).join('');
}
function feedReadyBadge(s){
  const c = s==='active'?'green':s==='ready'?'blue':s==='missing_prerequisites'?'red':'gray';
  return badge((s||'unknown').replace('_',' '), c);
}
function renderFeedReadiness(data){
  const status = data && data.status || 'unknown';
  $('feed-ready-summary').innerHTML = feedReadyBadge(status)+' '+esc(data && data.summary || '');
  const checks = data && data.checks || [];
  const rows = checks.map(c=>{
    const ccls = c.status==='pass' ? 'green' : 'red';
    return '<div class="line-item"><div class="label">'+esc(c.name)+'</div>'
      +'<div>'+badge(c.status,ccls)+'</div>'
      +'<div class="detail">'+esc(c.detail||'')+'</div></div>';
  });
  if(data && data.metrics){
    rows.push('<div class="line-item"><div class="label">metrics</div>'
      +'<div>'+badge(data.metrics.active?'active':'not active', data.metrics.active?'green':'gray')+'</div>'
      +'<div class="detail">'+esc(data.metrics.path)+' · feed='+num(data.metrics.real_feed_rows)
      +' market='+num(data.metrics.real_market_rows)
      +(data.metrics.latest_real_age_ms==null?'':' · age='+agestr(data.metrics.latest_real_age_ms))+'</div></div>');
  }
  if(data && data.next_command){
    rows.push('<div class="line-item"><div class="label">next command</div>'
      +'<div>'+badge('read only','blue')+'</div>'
      +'<div class="detail"><code>'+esc(data.next_command)+'</code></div></div>');
  }
  $('feed-ready-body').innerHTML = rows.length ? rows.join('') :
    '<div class="muted">feed readiness unavailable</div>';
}
async function loadFeedReadiness(){
  try{
    renderFeedReadiness(await apiJson('/api/feed_readiness'));
  }catch(e){
    $('feed-ready-summary').textContent='API error';
    $('feed-ready-body').innerHTML=errorBox('Could not load feed readiness. '+e.message);
  }
}
$('lifecycle-run').onclick = ()=>runLifecycle(false);
$('lifecycle-run-core').onclick = ()=>runLifecycle(true);
function coreTestNames(results, registry){
  const allowed = {};
  registry.filter(isCoreTest).forEach(t=>{ allowed[t.name]=true; });
  return Object.keys(results).filter(n=>allowed[n]).sort();
}
function renderTestCards(targetId, summaryId, results, names){
  let np=0,nf=0;
  $(targetId).innerHTML = names.length? names.map(n=>{
    const r=results[n]; if(r.status==='pass')np++; else if(r.status==='fail')nf++;
    return '<div style="border:1px solid #2a2a3a;border-radius:6px;padding:8px">'
      +'<div style="display:flex;justify-content:space-between"><b>'+esc(n)+'</b>'+stbadge(r.status)+'</div>'
      +'<div style="color:#888;font-size:12px;margin-top:4px">+'+num(r.passed)+' / -'+num(r.failed)
      +' · '+num(r.duration_ms)+'ms</div></div>';
  }).join('') : '<div class="muted">no core test results yet — click Run core tests.</div>';
  if($(summaryId)) $(summaryId).textContent = names.length? (np+' pass, '+nf+' fail'):'core tests only';
}
async function loadTests(){
  let m={};
  let registry;
  try{
    const out = await Promise.all([apiJson('/api/results'), apiJson('/api/tools')]);
    m = out[0];
    registry = out[1].tools || [];
  }catch(e){
    $('tests-grid').innerHTML = errorBox('Could not load test results. '+e.message);
    $('top-tests-grid').innerHTML = errorBox('Could not load test results. '+e.message);
    $('tests-summary').textContent = 'API error';
    $('top-tests-summary').textContent = 'API error';
    return;
  }
  const names=coreTestNames(m, registry);
  renderTestCards('tests-grid', 'tests-summary', m, names);
  renderTestCards('top-tests-grid', 'top-tests-summary', m, names);
}
async function runTool(name){
  try{
    const res = await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    return await res.json();
  }catch(e){ return {status:'error',reason:String(e)}; }
}
async function runCoreTests(){
  $('run-all').disabled=true; $('top-run-all').disabled=true;
  $('tests-summary').textContent='running…';
  $('top-tests-summary').textContent='running…';
  try{
    const tj = await apiJson('/api/tools');
    const runnable = tj.tools.filter(t=>isCoreTest(t) && t.runnable);
    for(const t of runnable){ await runTool(t.name); await loadTests(); }
  }catch(e){
    $('tests-grid').innerHTML = errorBox('Could not start test run. '+e.message);
    $('top-tests-grid').innerHTML = errorBox('Could not start test run. '+e.message);
    $('tests-summary').textContent = 'API error';
    $('top-tests-summary').textContent = 'API error';
  }finally{
    $('run-all').disabled=false; $('top-run-all').disabled=false;
  }
}
$('run-all').onclick = runCoreTests;
$('top-run-all').onclick = runCoreTests;
async function loadTools(){
  let tj;
  try{
    tj = await apiJson('/api/tools');
  }catch(e){
    $('tools-cards').innerHTML = errorBox('Could not load tools. '+e.message);
    $('tab-note').textContent = 'API error';
    return;
  }
  $('tab-note').textContent = tj.allow_network? 'network_read ENABLED':'network_read blocked (start with --allow-network)';
  const order={test:0,check:1,bench:2,probe:3,daemon:4,example:5};
  const tools=[...tj.tools].sort((a,b)=>(order[a.kind]-order[b.kind])||a.name.localeCompare(b.name));
  $('tools-cards').innerHTML = tools.map(t=>{
    const sc = t.safety==='pure'?'green':t.safety==='offline'?'blue':t.safety==='network_read'?'yellow':'red';
    const btn = t.runnable
      ? '<button class="tabbtn" onclick="runOne(this,\''+esc(t.name)+'\')">Run</button>'
      : '<button class="tabbtn" disabled title="'+esc(t.run_reason)+'">'+(t.safety==='live_order'?'✋ forbidden':'locked')+'</button>';
    return '<div style="border:1px solid #2a2a3a;border-radius:6px;padding:8px">'
      +'<div style="display:flex;justify-content:space-between;align-items:center">'
      +'<b>'+esc(t.name)+'</b>'+badge(t.safety, sc)+'</div>'
      +'<div style="color:#888;font-size:12px;margin:4px 0">'+esc(t.description||'')+'</div>'
      +'<div style="display:flex;justify-content:space-between;align-items:center">'
      +'<code style="color:#6cf;font-size:11px">'+esc(t.cmd)+'</code>'+btn+'</div>'
      +'<div class="run-out" style="color:#888;font-size:11px;margin-top:4px"></div></div>';
  }).join('');
}
async function runOne(btn,name){
  btn.disabled=true; const out=btn.closest('div').parentElement.querySelector('.run-out'); out.textContent='running…';
  const r=await runTool(name);
  out.innerHTML = stbadge(r.status)+' '+(r.reason? esc(r.reason):('+'+num(r.passed)+'/-'+num(r.failed)+' '+num(r.duration_ms)+'ms'));
  btn.disabled=false;
}

// ---- Research Inbox ----
let researchFilename = 'PLAN.md';
function researchBadge(state){
  const colors={COMPLETE:'green',RUNNING:'blue',READY:'blue',PREFLIGHT:'yellow',
    QUEUED:'yellow',NEEDS_METHOD:'yellow',BLOCKED:'red',FAILED:'red',REFUSED:'red'};
  return badge((state||'UNKNOWN').replaceAll('_',' '), colors[state]||'gray');
}
function renderResearchJobs(jobs){
  $('research-count').textContent=(jobs||[]).length+' job'+((jobs||[]).length===1?'':'s');
  if(!jobs || !jobs.length){
    $('research-jobs').innerHTML='<div class="muted">No research jobs yet.</div>';
    return;
  }
  $('research-jobs').innerHTML=jobs.map(j=>{
    const s=j.status||{}, spec=j.spec||{}, req=j.request||{}, coord=j.coordination||{};
    const report=j.report_available
      ? '<a class="tabbtn" target="_blank" rel="noopener" href="/api/research/jobs/'+esc(j.job_id)+'/report">Open report</a>' : '';
    const plugin=spec.plugin_id ? badge(spec.plugin_id,'blue') : badge('method needed','yellow');
    return '<div class="research-job">'
      +'<div class="meta"><b>'+esc(spec.title||req.source_filename||j.job_id)+'</b>'
      +researchBadge(s.state)+plugin+report+'</div>'
      +'<div class="why">'+esc(s.message||'')
      +(coord.message ? '<br>'+esc(coord.message) : '')+'</div>'
      +'<div class="muted" style="font-size:10px;margin-top:5px">'+esc(j.job_id)
      +' · '+esc(req.data_access||'')+'</div></div>';
  }).join('');
}
async function loadResearchJobs(){
  try{
    const data=await apiJson('/api/research/jobs');
    renderResearchJobs(data.jobs||[]);
  }catch(e){
    $('research-jobs').innerHTML=errorBox('Research Inbox unavailable: '+e.message);
    $('research-count').textContent='API error';
  }
}
async function acceptResearchFile(file){
  if(!file) return;
  if(file.size>2*1024*1024){
    $('research-submit-status').textContent='file exceeds 2 MiB'; return;
  }
  researchFilename=file.name||'PLAN.md';
  $('research-filename').textContent=researchFilename;
  $('research-plan').value=await file.text();
  $('research-submit-status').textContent='plan loaded';
}
async function submitResearchPlan(){
  const text=$('research-plan').value;
  if(!text.trim()){$('research-submit-status').textContent='add a research plan first';return;}
  $('research-submit').disabled=true;
  $('research-submit-status').textContent='compiling and queueing…';
  try{
    const job=await postJson('/api/research/jobs',{
      filename:researchFilename,plan_text:text,auto_run:true
    });
    $('research-submit-status').textContent='queued '+job.job_id;
    $('research-plan').value=''; researchFilename='PLAN.md';
    $('research-filename').textContent=researchFilename;
    await loadResearchJobs();
  }catch(e){ $('research-submit-status').textContent=e.message; }
  finally{ $('research-submit').disabled=false; }
}
const researchDrop=$('research-drop'), researchFile=$('research-file');
researchDrop.onclick=()=>researchFile.click();
researchDrop.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();researchFile.click();}};
researchFile.onchange=()=>acceptResearchFile(researchFile.files[0]);
for(const name of ['dragenter','dragover']) researchDrop.addEventListener(name,e=>{
  e.preventDefault();researchDrop.classList.add('drag');
});
for(const name of ['dragleave','drop']) researchDrop.addEventListener(name,e=>{
  e.preventDefault();researchDrop.classList.remove('drag');
});
researchDrop.addEventListener('drop',e=>acceptResearchFile(e.dataTransfer.files[0]));
$('research-submit').onclick=submitResearchPlan;
setInterval(()=>{if(!$('tab-research').hidden)loadResearchJobs();},5000);

['md-max-age','md-max-spread','md-min-size','md-row-limit','md-hot-only','md-find'].forEach(id=>{
  const el=$(id); if(el) el.addEventListener('input', ()=>{ dirty.market=1; });
});
renderMarketControls();
loadTests();
loadLifecycle();
loadFeedReadiness();
setInterval(loadFeedReadiness, 5000);

function pipeAge(s){ if(s==null) return '—'; if(s<60) return s+'s'; if(s<3600) return Math.round(s/60)+'m'; return (s/3600).toFixed(1)+'h'; }
async function loadPipeline(){
  let d; try{ d = await apiJson('/api/pipeline'); }catch(e){ return; }
  const dot=$('pipe-dot'); const running=d.running, healthy=d.healthy;
  dot.style.background = healthy?'#1a7f37':(running?'#c9a000':'#8a1f1f');
  $('pipe-state').textContent = running?(healthy?'RUNNING · healthy':'RUNNING · stale'):'STOPPED';
  $('pipe-activate').disabled = running;
  $('pipe-stop').disabled = !running && !d.launchd_loaded;
  $('pipe-hint').innerHTML = d.creds_present?'':'<span style="color:#c9a000">⚠ needs ~/.kalshi/env.sh</span>';
  const cells=[
    ['launchd', d.launchd_loaded?'loaded':'not loaded'],
    ['collector pid', d.collector_pid||'—'],
    ['last capture', pipeAge(d.capture_age_s)+(d.capture_age_s!=null?' ago':'')],
    ['staging fresh', pipeAge(d.staging_age_s)],
    ['L1 rows', (d.l1_rows||0).toLocaleString()],
    ['trades', (d.trade_rows||0).toLocaleString()],
    ['categories', (d.categories||[]).length]
  ];
  $('pipe-metrics').innerHTML = cells.map(c=>'<div style="border:1px solid #2a2a3a;border-radius:6px;padding:6px">'
    +'<div class="muted" style="font-size:11px">'+esc(c[0])+'</div>'
    +'<div style="font-size:15px">'+esc(c[1])+'</div></div>').join('');
}
async function pipeControl(action){
  $('pipe-activate').disabled=true; $('pipe-stop').disabled=true; $('pipe-hint').textContent='working…';
  try{
    const res=await fetch('/api/pipeline/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const r=await res.json();
    $('pipe-hint').innerHTML = (r&&r.ok===false)?('<span style="color:#c9a000">'+esc(r.hint||r.error||'failed')+'</span>'):'';
  }catch(e){ $('pipe-hint').textContent='error'; }
  setTimeout(loadPipeline, 700);
}
loadPipeline();
setInterval(loadPipeline, 4000);

</script>
</body>
</html>
"""


# --------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="Localhost NDJSON ops console")
    ap.add_argument("--metrics", default="work/metrics.ndjson",
                    help="append-only NDJSON file to tail (default work/metrics.ndjson)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1 — localhost only)")
    ap.add_argument("--backfill", type=int, default=1000,
                    help="history lines sent on connect (default 1000)")
    ap.add_argument("--results", default="work/test_results.ndjson",
                    help="test-results NDJSON (default work/test_results.ndjson)")
    ap.add_argument("--research-inbox", default="work/research_inbox",
                    help="local Research Inbox spool (default work/research_inbox)")
    ap.add_argument("--research-w09", action="store_true",
                    help="automatically dispatch READY registered-method plans to fixed W09")
    ap.add_argument("--research-ssh-key", default="~/.ssh/kalshi-key.pem",
                    help="SSH key for the fixed W09 Research Inbox transport")
    ap.add_argument("--allow-network", action="store_true",
                    help="permit network_read tools to run from the console "
                         "(live_order is ALWAYS refused regardless)")
    args = ap.parse_args()

    metrics_path = os.path.abspath(args.metrics)
    research_coordinator = None
    if args.research_w09:
        research_coordinator = ResearchW09Coordinator(
            os.path.abspath(args.research_inbox),
            ssh_key=os.path.expanduser(args.research_ssh_key),
        )
    handler = make_handler(
        metrics_path, args.backfill, args.allow_network,
        os.path.abspath(args.research_inbox), research_coordinator)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.daemon_threads = True
    if research_coordinator is not None:
        research_coordinator.start()

    print("Kalshi PoC ops console")
    print("  metrics : %s%s" % (metrics_path,
          "" if os.path.exists(metrics_path) else "  (not present yet — will appear when written)"))
    print("  serving : http://%s:%d" % (args.host, args.port))
    print("  bind    : %s (localhost only)" % args.host)
    print("  network : %s" % ("ALLOWED (network_read runnable)" if args.allow_network
                              else "blocked (network_read tools disabled)"))
    print("  research: %s" % os.path.abspath(args.research_inbox))
    print("  W09 auto : %s" % ("enabled" if args.research_w09 else "disabled"))
    print("Research intake writes only its local spool; trading is unaffected. Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if research_coordinator is not None:
            research_coordinator.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
