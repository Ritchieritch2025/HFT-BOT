#!/usr/bin/env python3
"""Operator control panel for the crypto-MM live canary.

One page to: see live status, hot-edit runtime limits (control document),
edit launch parameters (env file), and start / pause / kill / restart the
engine.  Localhost-bound, token-gated, same-origin checked.

It never talks to an exchange: runtime limits go through the validated
control document (the engine polls it), launch params go to an env file
consumed by the start script, and process control is start/stop of that
script.  All writes are atomic; the engine's own ignition interlocks still
apply (unpausing live requires selfchecks + fresh recon inside the engine).

Run on the EC2 host:
    MM_PANEL_TOKEN=<>=16 chars> python3 mm_panel.py --dir /home/ubuntu/mm_live_canary_v2
View from your Mac:
    ssh -f -N -L 8950:localhost:8950 -i ~/.ssh/kalshi-key.pem ubuntu@<host>
    open http://localhost:8950
"""
from __future__ import annotations

import argparse
import datetime as dt
import hmac
import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import mm_control  # noqa: E402

# Hard ceilings mirror the engine's immutable startup caps.
HARD_MAX_COST = 200.0
HARD_MAX_CLIP = 20.0
HARD_MAX_NET = 100

# Launch params the panel may edit: name -> (default, kind, help)
LAUNCH_PARAMS = {
    "MM_MARGIN": ("2.5", "float", "min edge, cents (live requires explicit)"),
    "MM_ECON_HALT": ("-10.0", "float", "realized-loss halt, dollars"),
    "MM_ORDER_TTL_S": ("90", "float", "deadman order expiry, seconds"),
    "MM_GAMMA": ("0.5", "float", "inventory retreat, cents per contract"),
    "MM_MIN_QUOTE_AGE_S": ("15", "float", "min quote age before reprice"),
    "MM_MIN_REQUOTE": ("0.5", "float", "min reprice delta, cents"),
    "MM_ANCHOR_SHIELD": ("0", "int01",
                         "fast-anchor cancel shield (Binance+Coinbase)"),
    "MM_ANCHOR_BETA_BN": ("0.82", "float", "Binance beta -> BRTI (measured)"),
    "MM_ANCHOR_BETA_CB": ("0.74", "float", "Coinbase beta -> BRTI (measured)"),
    "MM_PAIR": ("0", "int01", "pairing closed loop (0=front latch)"),
    "MM_ONLY_TICKER": ("", "str", "restrict to one ticker (blank=all)"),
    "MM_REQUIRE_FLAT_START": ("1", "int01", "halt unless flat at start"),
}


class Panel:
    def __init__(self, base: Path):
        self.base = base
        self.ctrl = base / "mm_control.json"
        self.status = base / "mm_control_status.json"
        self.env = base / "launch.env"
        self.start = base / "start.sh"
        self.log = base / "engine.log"

    # ---------------------------------------------------------- reads
    def read_ctrl(self) -> dict:
        return mm_control.read_control(
            self.ctrl, hard_max_cost=HARD_MAX_COST,
            hard_max_clip=HARD_MAX_CLIP, hard_max_net=HARD_MAX_NET)

    def read_status(self) -> dict | None:
        try:
            return json.loads(self.status.read_text())
        except Exception:
            return None

    def read_env(self) -> dict:
        out = {k: v[0] for k, v in LAUNCH_PARAMS.items()}
        try:
            for line in self.env.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k in LAUNCH_PARAMS:
                    out[k] = v
        except FileNotFoundError:
            pass
        return out

    def engine_pid(self) -> int | None:
        try:
            r = subprocess.run(["pgrep", "-f", str(self.base) + "/release"],
                               capture_output=True, text=True, timeout=5)
            pids = [int(x) for x in r.stdout.split()]
            return pids[0] if pids else None
        except Exception:
            return None

    def tail_log(self, n: int = 40) -> str:
        try:
            return "\n".join(self.log.read_text(
                errors="ignore").splitlines()[-n:])
        except Exception:
            return "(no log)"

    def recent_events(self, n: int = 25) -> list:
        """Latest engine receipts (newest first), compacted."""
        out = []
        try:
            files = sorted((self.base / "out").glob("mm_*.ndjson"))
            for fn in reversed(files[-2:]):
                lines = fn.read_text(errors="ignore").splitlines()
                for line in reversed(lines):
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("ev") in ("QUOTE_EVAL", "HEALTH"):
                        continue
                    out.append({k: r.get(k) for k in
                                ("ev", "mt", "side", "px", "qty", "code",
                                 "ms", "edge_c", "reason", "oid")
                                if r.get(k) is not None})
                    if len(out) >= n:
                        return out
        except Exception:
            pass
        return out

    # --------------------------------------------------------- writes
    def apply_ctrl(self, fields: dict) -> dict:
        cur = self.read_ctrl()
        nxt = dict(cur)
        for key in ("max_open_cost", "clip", "max_net", "paused", "kill",
                    "note"):
            if key in fields:
                nxt[key] = fields[key]
        nxt["revision"] = int(cur["revision"]) + 1
        nxt["updated_at"] = dt.datetime.now(
            dt.timezone.utc).isoformat().replace("+00:00", "Z")
        validated = mm_control.validate_control(
            nxt, hard_max_cost=HARD_MAX_COST, hard_max_clip=HARD_MAX_CLIP,
            hard_max_net=HARD_MAX_NET)
        mm_control.atomic_write_json(self.ctrl, validated)
        return validated

    def apply_env(self, fields: dict) -> dict:
        cur = self.read_env()
        for k, spec in LAUNCH_PARAMS.items():
            if k not in fields:
                continue
            raw = str(fields[k]).strip()
            kind = spec[1]
            if kind == "float":
                float(raw)                      # raises on garbage
            elif kind == "int01":
                if raw not in ("0", "1"):
                    raise ValueError(f"{k} must be 0 or 1")
            cur[k] = raw
        body = "".join(f"{k}={v}\n" for k, v in cur.items())
        tmp = self.env.with_suffix(".env.tmp")
        tmp.write_text(body)
        os.replace(tmp, self.env)
        return cur

    def proc_action(self, action: str) -> str:
        pid = self.engine_pid()
        if action == "stop":
            if not pid:
                return "not running"
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            return f"sent SIGTERM to {pid}"
        if action == "start":
            if pid:
                return f"already running (pid {pid})"
            if not self.start.exists():
                return "start.sh missing — run setup first"
            subprocess.Popen(["nohup", "bash", str(self.start)],
                             stdout=open(self.log, "ab"),
                             stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL,
                             start_new_session=True)
            time.sleep(3)
            return f"started (pid {self.engine_pid()})"
        if action == "restart":
            self.proc_action("stop")
            time.sleep(1)
            return self.proc_action("start")
        return "unknown action"


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>MM control panel</title><style>
:root{color-scheme:dark}
body{margin:0;background:#1a1a19;color:#fff;font:13px/1.5 ui-monospace,Menlo,monospace}
.wrap{max-width:1080px;margin:0 auto;padding:16px}
h1{font-size:15px;margin:0 0 4px}
h2{font-size:12px;color:#c3c2b7;margin:18px 0 6px;font-weight:600;
   text-transform:uppercase;letter-spacing:.06em}
.tok{display:flex;gap:8px;align-items:center;margin-bottom:10px}
input,button{font:inherit;background:#232322;color:#fff;border:1px solid #3a3a38;
  border-radius:5px;padding:5px 8px}
button{cursor:pointer}button:hover{border-color:#666}
.tiles{display:flex;gap:8px;flex-wrap:wrap}
.tile{background:#232322;border:1px solid #333;border-radius:6px;padding:8px 12px;min-width:150px}
.tile .n{color:#c3c2b7;font-size:11px}.tile .v{font-size:18px;font-weight:600;margin-top:2px}
.grid{display:grid;grid-template-columns:210px 130px 1fr;gap:6px 10px;align-items:center}
.grid label{color:#c3c2b7}.grid .h{color:#8a8a80;font-size:11px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.go{background:#199e70;border-color:#199e70}.warn{background:#c98500;border-color:#c98500}
.bad{background:#e66767;border-color:#e66767;color:#1a1a19;font-weight:600}
pre{background:#232322;border:1px solid #333;border-radius:6px;padding:8px;
  overflow:auto;max-height:230px;font-size:11px;color:#c3c2b7}
#msg{margin-top:8px;color:#c98500;min-height:18px}
table{border-collapse:collapse;width:100%;font-size:11px}
td,th{padding:2px 6px;border-bottom:1px solid #2c2c2b;text-align:left;white-space:nowrap}
th{color:#8a8a80;font-weight:500}
.on{color:#199e70}.off{color:#e66767}
</style></head><body><div class="wrap">
<h1>MM control panel <span id="dir" style="color:#8a8a80;font-size:11px"></span></h1>
<div class="tok"><input id="token" type="password" placeholder="MM_PANEL_TOKEN" size="34">
<button onclick="save()">remember</button><span id="msg"></span></div>

<h2>status</h2><div class="tiles" id="tiles"></div>

<h2>runtime limits — hot, no restart (control document)</h2>
<div class="grid" id="ctrlgrid"></div>
<div class="row">
  <button class="go" onclick="ctrl({paused:false})">UNPAUSE (money on)</button>
  <button class="warn" onclick="ctrl({paused:true})">pause</button>
  <button class="bad" onclick="if(confirm('KILL: cancel all + latch halt?'))ctrl({kill:true,paused:true})">KILL</button>
  <button onclick="ctrlFromForm()">apply limits</button>
</div>

<h2>launch parameters — need restart</h2>
<div class="grid" id="envgrid"></div>
<div class="row">
  <button onclick="saveEnv()">save env</button>
  <button class="warn" onclick="proc('restart')">save+restart engine</button>
  <button onclick="proc('start')">start</button>
  <button onclick="proc('stop')">stop</button>
</div>

<h2>recent engine events</h2><div id="events"></div>
<h2>engine log</h2><pre id="log"></pre>
</div><script>
let T=localStorage.getItem("mmtok")||"";
document.getElementById("token").value=T;
function save(){T=document.getElementById("token").value;localStorage.setItem("mmtok",T);msg("token saved");poll()}
function msg(s){document.getElementById("msg").textContent=s}
async function api(path,body){
  const r=await fetch(path,{method:body?"POST":"GET",
    headers:{"X-Panel-Token":T,"Content-Type":"application/json"},
    body:body?JSON.stringify(body):undefined});
  const j=await r.json().catch(()=>({error:"bad json"}));
  if(!r.ok)msg("error: "+(j.error||r.status)); return j}
function tile(n,v,cls){return `<div class="tile"><div class="n">${n}</div>
  <div class="v ${cls||""}">${v}</div></div>`}
async function poll(){
  const d=await api("/state"); if(!d||d.error)return;
  document.getElementById("dir").textContent=d.dir;
  const st=d.status||{},c=d.ctrl||{};
  document.getElementById("tiles").innerHTML=
    tile("engine",d.pid?("pid "+d.pid):"stopped",d.pid?"on":"off")+
    tile("mode",st.mode||"–")+
    tile("paused",String(c.paused),c.paused?"off":"on")+
    tile("halted",String(st.halted),st.halted?"off":"on")+
    tile("realized $",(st.realized!=null?st.realized.toFixed(2):"–"))+
    tile("exposure $",(st.exposure!=null?st.exposure.toFixed(2):"–"))+
    tile("open orders",st.open_orders!=null?st.open_orders:"–")+
    tile("revision",c.revision);
  document.getElementById("ctrlgrid").innerHTML=
    [["max_open_cost","total exposure cap, $ (hard 200)"],
     ["clip","contracts per order (hard 20)"],
     ["max_net","net position cap (hard 100)"]].map(([k,h])=>
     `<label>${k}</label><input id="c_${k}" value="${c[k]}">
      <span class="h">${h}</span>`).join("");
  document.getElementById("envgrid").innerHTML=Object.entries(d.env_spec).map(
    ([k,sp])=>`<label>${k}</label>
      <input id="e_${k}" value="${(d.env[k]??"").replace(/"/g,'&quot;')}">
      <span class="h">${sp[2]}</span>`).join("");
  const ev=d.events||[];
  document.getElementById("events").innerHTML=ev.length?
    "<table><tr><th>ev</th><th>mkt</th><th>side</th><th>px</th><th>qty</th>"+
    "<th>code</th><th>ms</th><th>edge</th><th>reason</th></tr>"+
    ev.map(e=>`<tr><td>${e.ev||""}</td><td>${(e.mt||"").slice(-12)}</td>
      <td>${e.side||""}</td><td>${e.px??""}</td><td>${e.qty??""}</td>
      <td>${e.code??""}</td><td>${e.ms??""}</td><td>${e.edge_c??""}</td>
      <td>${e.reason||""}</td></tr>`).join("")+"</table>":"(none yet)";
  document.getElementById("log").textContent=d.log;
}
async function ctrl(fields){const r=await api("/ctrl",fields);
  if(r&&!r.error)msg("control revision "+r.revision);poll()}
function ctrlFromForm(){ctrl({
  max_open_cost:parseFloat(document.getElementById("c_max_open_cost").value),
  clip:document.getElementById("c_clip").value,
  max_net:parseInt(document.getElementById("c_max_net").value)})}
async function saveEnv(){const f={};
  document.querySelectorAll("[id^=e_]").forEach(el=>f[el.id.slice(2)]=el.value);
  const r=await api("/env",f); if(r&&!r.error)msg("env saved (restart to apply)");poll()}
async function proc(a){if(a!=="stop"&&document.querySelector("[id^=e_]"))await saveEnv();
  const r=await api("/proc",{action:a}); if(r&&r.result)msg(r.result);poll()}
poll();setInterval(poll,3000);
</script></body></html>"""


def make_handler(panel: Panel, token: str):
    tok = token.encode()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _authed(self) -> bool:
            got = (self.headers.get("X-Panel-Token") or "").encode()
            return bool(got) and hmac.compare_digest(got, tok)

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            return origin.startswith("http://localhost") or \
                origin.startswith("http://127.0.0.1")

        def do_GET(self):
            if self.path == "/":
                return self._send(200, PAGE.encode(),
                                  "text/html; charset=utf-8")
            if self.path == "/state":
                if not self._authed():
                    return self._json(403, {"error": "bad token"})
                try:
                    ctrl = panel.read_ctrl()
                except Exception as exc:
                    ctrl = {"error": str(exc)[:120]}
                return self._json(200, {
                    "dir": str(panel.base),
                    "pid": panel.engine_pid(),
                    "ctrl": ctrl,
                    "status": panel.read_status(),
                    "env": panel.read_env(),
                    "env_spec": LAUNCH_PARAMS,
                    "events": panel.recent_events(),
                    "log": panel.tail_log(),
                })
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed() or not self._same_origin():
                return self._json(403, {"error": "unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json(400, {"error": "bad json"})
            try:
                if self.path == "/ctrl":
                    return self._json(200, panel.apply_ctrl(body))
                if self.path == "/env":
                    return self._json(200, panel.apply_env(body))
                if self.path == "/proc":
                    return self._json(200, {
                        "result": panel.proc_action(str(body.get("action")))})
            except Exception as exc:
                return self._json(400, {"error": str(exc)[:200]})
            return self._json(404, {"error": "not found"})

    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True,
                    help="canary directory (holds mm_control.json, start.sh)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8950)
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("panel is loopback-only")
    token = os.environ.get("MM_PANEL_TOKEN", "")
    if len(token) < 16:
        raise SystemExit("MM_PANEL_TOKEN must be at least 16 characters")

    panel = Panel(Path(args.dir))
    srv = ThreadingHTTPServer((args.host, args.port),
                              make_handler(panel, token))
    print(f"panel on http://{args.host}:{args.port} dir={panel.base}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
