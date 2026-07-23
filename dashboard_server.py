#!/usr/bin/env python3
"""Localhost-only operational console for the Kalshi PoC.

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
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Shared tool registry + safety policy (single source of truth with the console
# backend). The POST /api/run handler enforces may_run() SERVER-SIDE — the UI
# affordance is not the security boundary (guardrail 2).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
import run_tests  # noqa: E402

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


# ------------------------------------------------------- shadow research tab
SHADOW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "sandbox", "expt_bo2026")


def follow_data():
    try:
        import sys as _sys
        if SHADOW_DIR not in _sys.path:
            _sys.path.insert(0, SHADOW_DIR)
        from watchtower import follow_labels as _fl
        return _fl.build_follow_view()
    except Exception as e:
        return {"error": str(e)}


def ops_data():
    try:
        import sys as _sys
        if SHADOW_DIR not in _sys.path:
            _sys.path.insert(0, SHADOW_DIR)
        from watchtower import ops as _ops
        return _ops.build_ops()
    except Exception as e:
        return {"error": str(e)}


def shadow_data(allow_network):
    """Shadow-trading books/gauges from the sandbox watchtower (fail-soft:
    console keeps working if the experiment dir is absent). Price marks need
    network; the refresher thread starts only under --allow-network."""
    try:
        import sys as _sys
        import threading as _threading
        if SHADOW_DIR not in _sys.path:
            _sys.path.insert(0, SHADOW_DIR)
        from watchtower import dashboard as _wt
        if allow_network and not getattr(_wt, "_refresher_started", False):
            _threading.Thread(target=_wt.price_refresher, daemon=True).start()
            _wt._refresher_started = True
        d = _wt.build_data()
        d["marks_live"] = bool(allow_network)
        return d
    except Exception as e:
        return {"error": str(e)}


def make_handler(metrics_path, backfill_default, allow_network=False,
                 results_latest=None):
    results_latest = results_latest or run_tests.LATEST
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

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif path == "/healthz":
                self._send(200, json.dumps({"ok": True}), "application/json")
            elif path == "/stream":
                self.handle_stream()
            elif path == "/api/tools":
                # Never expose a run affordance the server won't honor: annotate
                # each tool with whether THIS server instance would run it, and
                # whether it belongs to the "Run all" set (defined once, in
                # run_tests.runnable_test_set — the UI must not re-derive it).
                reg = run_tests.load_registry()
                run_all = {t["name"] for t in run_tests.runnable_test_set(reg)}
                out = []
                for t in reg:
                    ok, reason = run_tests.may_run(t, allow_network)
                    e = dict(t); e["runnable"] = ok; e["run_reason"] = reason
                    e["in_run_all"] = t["name"] in run_all
                    out.append(e)
                self._json(200, {"allow_network": allow_network, "tools": out})
            elif path == "/api/results":
                latest = {}
                if os.path.exists(results_latest):
                    try:
                        latest = json.load(open(results_latest))
                    except Exception:
                        latest = {}
                self._json(200, latest)
            elif path == "/api/shadow":
                self._json(200, shadow_data(allow_network))
            elif path == "/api/ops":
                self._json(200, ops_data())
            elif path == "/api/follow":
                self._json(200, follow_data())
            else:
                self._send(404, "not found")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
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
  <button data-tab="tests" class="tabbtn">Tests</button>
  <button data-tab="tools" class="tabbtn">Tools</button>
  <button data-tab="ops" class="tabbtn">Ops</button>
  <button data-tab="follow" class="tabbtn">跟单观测</button>
  <button data-tab="shadow" class="tabbtn">Shadow</button>
  <span id="tab-note" style="margin-left:auto;color:#888;font-size:12px"></span>
</nav>

<main id="tab-live" class="tabview">
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
  <div id="tools-cards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px"></div>
</section>

<section id="tab-follow" class="tabview" hidden style="padding:12px">
  <div id="follow-head" style="font-size:14px;margin-bottom:10px">loading…</div>
  <div id="follow-body" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px"></div>
</section>

<section id="tab-ops" class="tabview" hidden style="padding:12px">
  <div id="ops-head" style="font-size:13px;margin-bottom:10px">loading…</div>
  <div id="ops-cards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px"></div>
</section>

<section id="tab-shadow" class="tabview" hidden style="padding:12px">
  <div id="shadow-meta" style="color:#888;font-size:12px;margin-bottom:8px">loading shadow data…</div>
  <div id="shadow-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:10px"></div>
</section>

<script>
"use strict";
const MAX_LOG = 1000, MAX_ORDERS = 200, LOG_VIEW = 200;

// ---- state ----
const state = {
  system:{}, start_ts:null, last_ts:0,
  feeds:new Map(), strategies:new Map(),
  orders:[], log:[],
  risk:{rejects:{stale_signal:0,duplicate:0,risk_check:0,other:0}, fields:{}},
  evtWindow:[],
};
let dirty = {sys:1,feed:1,strat:1,order:1,risk:1,log:1};
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

// ---- ingest ----
function ingest(o){
  if(!o||typeof o!=='object'||!o.type) return;
  const ts = o.ts_ms||Date.now();
  state.last_ts = Math.max(state.last_ts, ts);
  state.evtWindow.push(Date.now());

  // log ring
  state.log.push(o);
  if(state.log.length>MAX_LOG) state.log.splice(0, state.log.length-MAX_LOG);
  dirty.log=1;

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
    case 'feed': return (o.source||'')+' connected='+o.connected+' fresh='+(o.freshness_ms??'—')+'ms gaps='+(o.gaps??'—');
    case 'order': return (o.strategy||'')+' '+(o.side||'')+' '+(o.ticker||'')+' @'+(o.price??'—')+' → '+(o.status||'')+(o.http_status?' ('+o.http_status+')':'');
    case 'strategy': return (o.name||'')+' triggers='+(o.triggers??'—')+' edge='+(o.edge_signal_cents??'—')+'¢ '+(o.reason||'');
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
    return '<tr class="logline" data-i="'+i+'"><td>'+tstr(o.ts_ms)+'</td><td>'+badge(o.type,cls)+'</td>'+
      '<td class="raw">'+esc(logSummary(o))+'</td></tr>';
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
  if(dirty.sys){ renderSys(); dirty.sys=0; }
  if(dirty.risk){ renderRisk(); renderSys(); dirty.risk=0; }   // risk feeds kill badge
  if(dirty.feed){ renderFeed(); dirty.feed=0; }
  if(dirty.strat){ renderStrat(); dirty.strat=0; }
  if(dirty.order){ renderOrders(); dirty.order=0; }
  if(dirty.log){ renderLog(); dirty.log=0; }
}, 250);
// ages tick even without new events
setInterval(()=>{ dirty.sys=1; dirty.feed=1; }, 1000);
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

// ---- Tests / Tools tabs (P2) ----
function showTab(name){
  document.querySelectorAll('.tabview').forEach(v=>v.hidden = (v.id!=='tab-'+name));
  document.querySelectorAll('.tabbtn[data-tab]').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  if(name==='tests') loadTests();
  if(name==='tools') loadTools();
}
document.querySelectorAll('.tabbtn[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));

// ---- Shadow tab (research books; data from /api/shadow) ----
async function refreshShadow(){
  const meta=document.getElementById('shadow-meta'), grid=document.getElementById('shadow-grid');
  if(!meta) return;
  try{
    const d=await (await fetch('/api/shadow')).json();
    if(d.error){ meta.textContent='shadow data unavailable: '+d.error; return; }
    const sys=d.sys||{};
    meta.innerHTML=`更新 ${d.ts} · 监控进程 ${sys.daemon?'<span style="color:#3fb950">在线</span>':'<span style="color:#f85149">掉线!</span>'} · 案例进程 ${sys.casebot?'在线':'掉线'} · 数据流 ${sys.feed_age_s!=null?sys.feed_age_s+'s前':'?'} · 闸门 ${sys.gate_open?'开':'关'} · 今日告警 ${Object.entries(d.alerts_today||{}).map(([k,v])=>k+':'+v).join(' ')||'-'}`;
    const P=v=>`<span style="color:${v>=0?'#3fb950':'#f85149'}">${(v>=0?'+':'')+v.toFixed(2)}</span>`;
    let h='';
    for(const b of d.books){
      h+=`<section><h2>${b.name}${b.cash!=null?` · 现金$${b.cash}`:''}</h2>
      <div style="padding:8px 11px;font-size:15px;font-weight:600">合计 ${P(b.total)} $
      <span style="font-size:12px;font-weight:400;color:#8b949e"> 落袋 ${P(b.realized)}(${b.wins}赢/${b.losses}输)· 浮动 ${P(b.floating)} · 押金$${b.collateral} · ${b.n_open_mkts}市场${b.open_contracts}手 · 今日${b.fills_today}笔</span></div>
      <div style="max-height:250px;overflow:auto"><table>
      <thead><tr><th>市场</th><th>手</th><th>卖价¢</th><th>现价/果</th><th>押金$</th><th>盈亏$</th></tr></thead><tbody>`+
      b.rows.map(r=>`<tr><td style="max-width:250px;overflow:hidden;text-overflow:ellipsis">${r.tk}</td><td>${r.q}</td><td>${r.avg}</td><td>${r.mark}</td><td>${r.coll||''}</td><td>${P(r.pnl)}</td></tr>`).join('')+
      `</tbody></table></div></section>`;
    }
    h+=`<section><h2>健康仪表(单名)</h2><div style="padding:8px 11px;font-size:12px">
      <div style="color:#8b949e">买"会"的人比实际多付的程度(日度 pp)</div>
      <div style="display:flex;gap:3px;align-items:flex-end;height:44px;margin:6px 0 10px">`+
      (d.gap_daily||[]).map(g=>`<div title="${g.day} ${g.gap_pp}pp" style="width:20px;border-radius:2px 2px 0 0;background:${g.gap_pp>=0?'#3fb950':'#f85149'};height:${Math.min(44,Math.abs(g.gap_pp)*2.2)}px"></div>`).join('')+
      `</div><table><thead><tr><th>日</th><th>常赢小钱¢</th><th>偶亏大钱¢</th><th>净¢</th></tr></thead><tbody>`+
      (d.freq_mag||[]).map(f=>`<tr><td>${f.day.slice(5)}</td><td>${f.freq_edge_c}</td><td>${f.mag_edge_c}</td><td>${f.net_c}</td></tr>`).join('')+
      `</tbody></table></div></section>`;
    h+=`<section style="grid-column:1/-1"><h2>进单流水(何时·哪单·为什么)</h2>
      <div style="max-height:320px;overflow:auto"><table>
      <thead><tr><th>时间</th><th>策略</th><th>动作</th><th>市场</th><th>为什么</th></tr></thead><tbody>`+
      (d.trades||[]).map(t=>{const dt=new Date(t.ts*1000);
        return `<tr><td>${dt.toTimeString().slice(0,8)}</td><td>${t.who}</td>
        <td style="text-align:left">${t.act}</td>
        <td style="text-align:left;max-width:240px;overflow:hidden;text-overflow:ellipsis">${t.tk}</td>
        <td style="text-align:left;color:#8b949e">${t.why}</td></tr>`}).join('')+
      `</tbody></table></div></section>`;
    if(d.backtest&&d.backtest.h7){
      const bt=d.backtest;
      const tbl=(title,obj)=>{if(!obj)return '';
        return `<div style="margin-bottom:10px"><div style="color:#8b949e;margin:4px 0">${title}</div>
        <table><thead><tr>`+obj.cols.map(c=>`<th>${c}</th>`).join('')+`</tr></thead><tbody>`+
        obj.rows.slice(0,24).map(r=>`<tr>`+r.map(x=>`<td>${x==null?'':(typeof x==='number'?x.toLocaleString():x)}</td>`).join('')+`</tr>`).join('')+
        `</tbody></table></div>`;};
      h+=`<section style="grid-column:1/-1"><h2>回测数据区(11天历史,策略依据)</h2>
        <div style="padding:8px 11px;font-size:11.5px;max-height:420px;overflow:auto">`+
        tbl('跟单策略:各价位段命中率与净利(H7)',bt.h7)+
        tbl('摆摊:各组每合约盈亏与频率/幅度分解(H3)',bt.h3)+
        tbl('按天稳定性(daily)',bt.daily)+
        `</div></section>`;}
    h+=`<section><h2>动态(成交/开奖/案例)</h2><div style="padding:6px 11px;font-size:12px;max-height:420px;overflow:auto">`+
      (d.feed||[]).map(e=>`<div style="border-bottom:1px solid #2a3139;padding:4px 0"><span style="color:${e.icon==='settle'?'#3fb950':e.icon==='case'?'#e3b341':'#8b949e'}">[${e.who}]</span> ${e.text}</div>`).join('')+
      `</div></section>`;
    grid.innerHTML=h;
  }catch(e){ meta.textContent='shadow fetch failed: '+e; }
}
setInterval(refreshShadow, 10000); refreshShadow();

// ---- Ops tab (strategy control panel; /api/ops) ----
async function refreshOps(){
  const head=document.getElementById('ops-head'), grid=document.getElementById('ops-cards');
  if(!head) return;
  try{
    const d=await (await fetch('/api/ops')).json();
    if(d.error){ head.textContent='ops unavailable: '+d.error; return; }
    const H=d.health, T=d.totals;
    const dot=(ok)=>`<span style="color:${ok?'#3fb950':'#f85149'}">●</span>`;
    const P=v=>`<span style="color:${v>=0?'#3fb950':'#f85149'}">${(v>=0?'+':'')+v.toFixed(2)}</span>`;
    head.innerHTML=`更新 ${d.ts} &nbsp; ${dot(H.daemon)}监控进程 &nbsp; ${dot(H.casebot)}案例进程 &nbsp;`+
      `数据流 ${H.feed_age_s!=null?H.feed_age_s+'秒前':'?'} ${dot(H.feed_age_s!=null&&H.feed_age_s<60)} &nbsp;`+
      `| 策略 <b>${T.n_running}/${T.n_strategies}</b> 运行中 · 合计已结算 ${P(T.realized)} $ · 占用押金 $${T.collateral}`+
      (H.marks_live?'':' <span style="color:#d29922">(现价未加载)</span>');
    let h='';
    for(const s of d.strategies){
      const tot=s.wins+s.losses;
      const wr=tot? (100*s.wins/tot).toFixed(0)+'%':'—';
      h+=`<section><h2>${dot(s.live)} ${s.name} <span style="font-size:10px;color:#8b949e">${s.kind}</span></h2>
      <div style="padding:9px 12px;font-size:12px">
        <div style="font-size:18px;font-weight:700;margin-bottom:4px">已结算 ${P(s.realized)} $</div>
        <table style="font-size:11.5px">
        <tr><td style="text-align:left;color:#8b949e">已跑</td><td>${s.age}</td>
            <td style="text-align:left;color:#8b949e">最近事件</td><td>${s.last_age_s!=null?s.last_age_s+'秒前':'—'}</td></tr>
        <tr><td style="text-align:left;color:#8b949e">胜/负</td><td>${s.wins}/${s.losses}(${wr})</td>
            <td style="text-align:left;color:#8b949e">成交笔</td><td>${s.fills}</td></tr>
        <tr><td style="text-align:left;color:#8b949e">持仓中</td><td>${s.open_mkts}市场</td>
            <td style="text-align:left;color:#8b949e">押金</td><td>$${s.collateral}</td></tr>
        </table>
        <div style="color:#8b949e;margin-top:5px">${s.note}</div>
        <details style="margin-top:6px"><summary style="cursor:pointer;color:#58a6ff">查看具体市场 (${s.rows.length})</summary>
        <div style="max-height:280px;overflow:auto;margin-top:5px"><table style="font-size:10.5px">
        <thead><tr><th>市场(点击核对)</th><th>手</th><th>价¢</th><th>状态</th><th>盈亏$</th></tr></thead><tbody>`+
        s.rows.map(r=>`<tr><td style="text-align:left;max-width:230px;overflow:hidden;text-overflow:ellipsis">
          <a href="${r.url}" target="_blank" style="color:#58a6ff;text-decoration:none" title="Kalshi官方数据(结算/价格核对)">${r.tk}</a></td>
          <td>${r.q}</td><td>${r.px}</td>
          <td>${r.mark}</td><td style="color:${r.pnl>=0?'#3fb950':'#f85149'}">${r.pnl.toFixed(2)}</td></tr>`).join('')+
        `</tbody></table></div></details>
      </div></section>`;
    }
    grid.innerHTML=h || '<div style="color:#8b949e">no strategies found</div>';
  }catch(e){ head.textContent='ops fetch failed: '+e; }
}
setInterval(refreshOps, 10000); refreshOps();

// ---- 跟单观测 tab (/api/follow) ----
async function refreshFollow(){
  const head=document.getElementById('follow-head'), body=document.getElementById('follow-body');
  if(!head) return;
  try{
    const d=await (await fetch('/api/follow')).json();
    if(d.error){ head.textContent='follow unavailable: '+d.error; return; }
    const P=v=>`<span style="color:${v>=0?'#3fb950':'#f85149'}">${(v>=0?'+':'')+v.toFixed(1)}</span>`;
    head.innerHTML=`跟单 · 已结算 <b>${d.n_settled}</b> 笔(样本量,越多越可信)· 待结算 ${d.n_open} · 总赢率 <b>${d.win_rate}%</b> · 落袋 ${P(d.total_pnl)} $
      <div style="color:#8b949e;font-size:12px;margin-top:4px">观测仪表,不下结论。看赢率随样本变多怎么走。会价90+段=机械收敛(假脚印),80-89=真脚印区。</div>`;
    const tbl=(title,rows,note)=>`<section><h2>${title}</h2>
      <div style="color:#8b949e;font-size:11.5px;padding:2px 11px">${note||''}</div>
      <table><thead><tr><th>${title.includes('会价')?'会价段':'距结算'}</th><th>赢</th><th>输</th><th>赢率</th><th>落袋$</th></tr></thead><tbody>`+
      rows.map(g=>`<tr><td style="text-align:left">${g.k}</td><td>${g.w}</td><td>${g.l}</td>
        <td>${g.n?Math.round(100*g.w/g.n)+'%':'—'}</td><td style="color:${g.pnl>=0?'#3fb950':'#f85149'}">${g.pnl.toFixed(1)}</td></tr>`).join('')+
      `</tbody></table></section>`;
    body.innerHTML=
      tbl('按触发时会价段',d.by_price,'会价越高越可能是机械收敛;90+段目前几乎全输')+
      tbl('按距结算时长',d.by_ttc,'区分"直播中真信息"vs"临近结算机械收敛"。旧单无此数据(?),新单开始累积');
  }catch(e){ head.textContent='follow fetch failed: '+e; }
}
setInterval(refreshFollow, 15000); refreshFollow();

function stbadge(s){ const c = s==='pass'?'green':(s==='fail'?'red':'gray'); return badge(s||'—', c); }
async function loadTests(){
  let m={}; try{ m = await (await fetch('/api/results')).json(); }catch(_){}
  const g=$('tests-grid'); const names=Object.keys(m).sort();
  let np=0,nf=0;
  g.innerHTML = names.length? names.map(n=>{
    const r=m[n]; if(r.status==='pass')np++; else if(r.status==='fail')nf++;
    return '<div style="border:1px solid #2a2a3a;border-radius:6px;padding:8px">'
      +'<div style="display:flex;justify-content:space-between"><b>'+esc(n)+'</b>'+stbadge(r.status)+'</div>'
      +'<div style="color:#888;font-size:12px;margin-top:4px">+'+num(r.passed)+' / -'+num(r.failed)
      +' · '+num(r.duration_ms)+'ms</div></div>';
  }).join('') : '<div class="muted">no results yet — click “Run all”.</div>';
  $('tests-summary').textContent = names.length? (np+' pass, '+nf+' fail'):'';
}
async function runTool(name){
  try{
    const res = await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    return await res.json();
  }catch(e){ return {status:'error',reason:String(e)}; }
}
$('run-all').onclick = async ()=>{
  $('run-all').disabled=true; $('tests-summary').textContent='running…';
  const tj = await (await fetch('/api/tools')).json();
  const runnable = tj.tools.filter(t=>t.in_run_all && t.runnable); // set defined server-side (runnable_test_set)
  for(const t of runnable){ await runTool(t.name); await loadTests(); }
  $('run-all').disabled=false;
};
async function loadTools(){
  const tj = await (await fetch('/api/tools')).json();
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
    ap.add_argument("--allow-network", action="store_true",
                    help="permit network_read tools to run from the console "
                         "(live_order is ALWAYS refused regardless)")
    args = ap.parse_args()

    metrics_path = os.path.abspath(args.metrics)
    # --results names the NDJSON history; the per-suite "latest" JSON the API
    # serves lives next to it (both are written by run_tests.record).
    results_latest = os.path.join(os.path.dirname(os.path.abspath(args.results)),
                                  "test_results_latest.json")
    handler = make_handler(metrics_path, args.backfill, args.allow_network,
                           results_latest)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    httpd.daemon_threads = True

    print("Kalshi PoC ops console")
    print("  metrics : %s%s" % (metrics_path,
          "" if os.path.exists(metrics_path) else "  (not present yet — will appear when written)"))
    print("  serving : http://%s:%d" % (args.host, args.port))
    print("  bind    : %s (localhost only)" % args.host)
    print("  network : %s" % ("ALLOWED (network_read runnable)" if args.allow_network
                              else "blocked (network_read tools disabled)"))
    print("Read-only console. Ctrl-C to stop; trading is unaffected.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.server_close()


if __name__ == "__main__":
    main()
