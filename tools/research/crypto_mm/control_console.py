#!/usr/bin/env python3
"""Localhost-only manual control console for the crypto MM prototype.

This process has no exchange client and cannot place an order.  It publishes a
validated, atomically replaced control document consumed by ``mm_engine.py``.
Run it on the same host as the engine and reach it through an SSH tunnel.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from mm_control import (
    ControlError,
    STATUS_SCHEMA,
    atomic_write_json,
    default_control,
    next_control,
    read_control,
)


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto MM 资金控制台</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0d1117;color:#d7e2ea;font:14px ui-monospace,SFMono-Regular,Menlo,monospace}
main{max-width:900px;margin:24px auto;padding:0 16px}
h1{font-size:20px;margin:0 0 6px}.sub{color:#8b949e;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.card{border:1px solid #30363d;border-radius:8px;background:#161b22;padding:14px}
.wide{grid-column:1/-1}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
label{display:block;color:#8b949e;margin:10px 0 4px}
input{box-sizing:border-box;width:100%;padding:8px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:5px}
input[type=checkbox]{width:auto}.value{font-size:24px;color:#fff}.muted{color:#8b949e}
button{padding:8px 12px;border:1px solid #30363d;border-radius:5px;background:#21262d;color:#e6edf3;cursor:pointer}
button:hover{background:#30363d}button:disabled{opacity:.5;cursor:not-allowed}
.primary{background:#1f6feb}.warn{border-color:#d29922;color:#e3b341}.danger{background:#8b1a1a;border-color:#f85149}
.ok{color:#3fb950}.pending{color:#d29922}.bad{color:#f85149}.msg{min-height:20px;margin-top:10px;white-space:pre-wrap}
code{color:#79c0ff}
</style>
</head>
<body><main>
<h1>Crypto MM · 资金控制台</h1>
<div class="sub">只写本机控制文件；不包含交易 API。目标 250ms 读取，但同进程阻塞会延迟；这不是实盘急停保证。</div>
<div class="grid">
  <section class="card">
    <div class="muted">引擎本地应用状态</div>
    <div id="engine" class="value">等待状态…</div>
    <div id="engine-detail" class="muted"></div>
  </section>
  <section class="card">
    <div class="muted">引擎本地资金占用 / 上限</div>
    <div class="value"><span id="exposure">—</span> / $<span id="current-limit">—</span></div>
    <div id="revision" class="muted"></div>
  </section>
  <section class="card wide">
    <div class="grid">
      <div>
        <label for="max-cost">资金上限（美元）</label>
        <input id="max-cost" type="number" min="0" step="0.01">
      </div>
      <div>
        <label for="clip">每笔手数</label>
        <input id="clip" type="number" min="0.01" step="0.01">
      </div>
      <div>
        <label for="max-net">单市场最大净仓</label>
        <input id="max-net" type="number" min="0" step="1">
      </div>
    </div>
    <label for="note">操作备注</label>
    <input id="note" maxlength="200" placeholder="为什么改限制">
    <label for="token">控制令牌（只保存在本页 session）</label>
    <input id="token" type="password" autocomplete="off" placeholder="MM_CONTROL_TOKEN">
    <div class="row" style="margin-top:12px">
      <button id="apply" class="primary">应用限制</button>
      <button id="pause" class="warn">请求暂停并撤销挂单</button>
      <button id="resume">恢复报价</button>
      <button id="kill" class="danger">请求紧急 KILL</button>
      <button id="reset-kill">清除文件侧 KILL</button>
      <button id="reload">放弃本地编辑并刷新</button>
    </div>
    <div class="muted" style="margin-top:10px">
      KILL 会锁住引擎；清除文件标志后仍需重启引擎，并保持 paused=true 后重新检查。
    </div>
    <div id="msg" class="msg"></div>
  </section>
</div>
<script>
"use strict";
let revision=null, dirty=false;
const $=id=>document.getElementById(id);
$('token').value=sessionStorage.getItem('mm-token')||'';
$('token').oninput=()=>sessionStorage.setItem('mm-token',$('token').value);
['max-cost','clip','max-net','note'].forEach(
  id=>$(id).oninput=()=>{dirty=true}
);
function n(v,d=2){return v==null?'—':Number(v).toFixed(d)}
async function state(){
  try{
    const d=await (await fetch('/api/state',{cache:'no-store'})).json();
    if(d.error) throw new Error(d.error);
    const c=d.control, s=d.status||{};
    const conflict=dirty && revision!==null && c.revision!==revision;
    if(!dirty || revision===null){
      revision=c.revision;
      $('max-cost').value=c.max_open_cost;
      $('clip').value=c.clip;
      $('max-net').value=c.max_net;
      $('note').value=c.note||'';
    }
    $('revision').textContent=conflict
      ?`远端 revision ${c.revision}；本地编辑基于 ${revision}，请刷新后重做`
      :`控制 revision ${c.revision} · ${c.updated_at}`;
    $('current-limit').textContent=n(c.max_open_cost);
    $('exposure').textContent=n(s.exposure);
    const stale=!s.observed_at_ns || Date.now()-s.observed_at_ns/1e6>15000;
    const e=s.effective||{};
    const effectiveMatches=
      Number(e.max_open_cost)===Number(c.max_open_cost)&&
      Number(e.clip)===Number(c.clip)&&
      Number(e.max_net)===Number(c.max_net)&&
      e.paused===c.paused&&e.kill===c.kill;
    const applied=!stale && s.revision===c.revision &&
      effectiveMatches && !s.control_error;
    let label='REQUESTED / WAITING';
    if(applied){
      if(c.kill) label=s.open_orders===0?'KILL APPLIED (LOCAL ONLY)':'KILL DRAINING';
      else if(c.paused) label=s.open_orders===0?'PAUSE APPLIED (LOCAL ONLY)':'PAUSE DRAINING';
      else if(s.halted) label='HALTED';
      else if(s.limit_breached) label='OVER LIMIT';
      else label='ARMED';
    }
    $('engine').textContent=label;
    $('engine').className='value '+(
      label==='ARMED'?'ok':
      label.includes('LOCAL ONLY')||label.includes('DRAINING')||
        label.includes('WAITING')?'pending':'bad');
    $('engine-detail').textContent=
      `mode=${s.mode||'—'} · requested rev=${c.revision} · applied rev=${s.revision??'—'}`
      +` · engine-local open=${s.open_orders??'—'}`
      +` · effective limit=${e.max_open_cost??'—'}`
      +` · status ${stale?'STALE':'fresh'}`
      +` · exchange reconciliation unavailable`
      +(s.control_error?` · ERROR ${s.control_error}`:'');
    $('resume').disabled=!(
      applied && c.paused && !c.kill && !s.halted &&
      s.mode==='shadow' && s.open_orders===0
    );
    if(conflict) $('msg').textContent=
      '控制文件已由另一窗口更新；本地编辑未覆盖。请刷新后重新输入。';
  }catch(e){$('msg').textContent='读取失败: '+e}
}
async function update(patch,confirmText){
  $('msg').textContent='写入中…';
  try{
    const r=await fetch('/api/control',{method:'POST',
      headers:{'Content-Type':'application/json','X-MM-Control-Token':$('token').value},
      body:JSON.stringify({expected_revision:revision,confirm:confirmText,patch})});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||('HTTP '+r.status));
    revision=d.control.revision; dirty=false;
    $('msg').textContent=`已写 revision ${d.control.revision}；等待引擎确认`;
    await state();
  }catch(e){$('msg').textContent='拒绝: '+e.message}
}
$('apply').onclick=()=>update({
  max_open_cost:Number($('max-cost').value),clip:$('clip').value,
  max_net:Number($('max-net').value),note:$('note').value
},'APPLY');
$('pause').onclick=()=>update({paused:true,note:$('note').value},'PAUSE');
$('resume').onclick=()=>{
  if(!window.confirm('确认恢复自动报价？')) return;
  update({paused:false,note:$('note').value},'APPLY');
};
$('kill').onclick=()=>{
  if(window.prompt('输入 KILL 确认紧急停机')!=='KILL') return;
  update({paused:true,kill:true,note:$('note').value},'KILL');
};
$('reset-kill').onclick=()=>{
  if(window.prompt('输入 RESET KILL；清除后仍必须重启引擎')!=='RESET KILL') return;
  update({kill:false,paused:true,note:$('note').value},'RESET KILL');
};
$('reload').onclick=()=>{dirty=false;revision=null;$('msg').textContent='';state()};
setInterval(state,2000);state();
</script>
</main></body></html>"""


class StaleRevision(ControlError):
    pass


class ControlStore:
    STATUS_MAX_AGE_NS = 15_000_000_000

    def __init__(
        self,
        control_path: Path,
        status_path: Path,
        *,
        hard_max_cost: float,
        hard_max_clip: str,
        hard_max_net: int,
        initial_max_cost: float = 0.0,
        initial_clip: str = "1.00",
        initial_max_net: int = 0,
    ):
        self.control_path = control_path
        self.status_path = status_path
        self.hard_max_cost = hard_max_cost
        self.hard_max_clip = hard_max_clip
        self.hard_max_net = hard_max_net
        self.lock = threading.Lock()
        self.lock_path = control_path.with_name(control_path.name + ".lock")
        if not control_path.exists():
            initial = default_control(
                max_open_cost=initial_max_cost,
                clip=initial_clip,
                max_net=initial_max_net,
                paused=True,
            )
            # Validate the startup document through the same path used later.
            initial = next_control(
                {**initial, "revision": -1},
                {
                    "max_open_cost": initial_max_cost,
                    "clip": initial_clip,
                    "max_net": initial_max_net,
                    "paused": True,
                    "kill": False,
                    "note": "console initialized fail-closed",
                },
                hard_max_cost=hard_max_cost,
                hard_max_clip=hard_max_clip,
                hard_max_net=hard_max_net,
            )
            atomic_write_json(control_path, initial)

    @contextmanager
    def file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self) -> dict[str, Any]:
        return read_control(
            self.control_path,
            hard_max_cost=self.hard_max_cost,
            hard_max_clip=self.hard_max_clip,
            hard_max_net=self.hard_max_net,
        )

    def read_status(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != STATUS_SCHEMA:
                return None
            return value
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"control": self.read(), "status": self.read_status()}

    def require_shadow_resume_ready(self, current: dict[str, Any]) -> None:
        """Allow resume only against a fresh, matching shadow-engine receipt."""
        status = self.read_status()
        if status is None:
            raise ControlError("resume refused: engine status is unavailable")
        observed = status.get("observed_at_ns")
        if type(observed) is not int:
            raise ControlError("resume refused: engine status has no valid clock")
        age = time.time_ns() - observed
        if age < 0 or age > self.STATUS_MAX_AGE_NS:
            raise ControlError("resume refused: engine status is stale")
        if status.get("mode") != "shadow":
            raise ControlError(
                "live resume is disabled until exchange reconciliation exists"
            )
        if status.get("revision") != current["revision"]:
            raise ControlError("resume refused: engine has not applied this revision")
        effective = status.get("effective")
        if not isinstance(effective, dict) or not (
            effective.get("max_open_cost") == current["max_open_cost"]
            and str(effective.get("clip")) == current["clip"]
            and effective.get("max_net") == current["max_net"]
            and effective.get("paused") is current["paused"]
            and effective.get("kill") is current["kill"]
        ):
            raise ControlError("resume refused: effective controls do not match")
        if status.get("control_error"):
            raise ControlError("resume refused: engine reports a control error")
        if status.get("halted"):
            raise ControlError("resume refused: engine is halted")
        if status.get("limit_breached"):
            raise ControlError("resume refused: engine is over a limit")
        if status.get("open_orders") != 0:
            raise ControlError("resume refused: engine-local orders are not zero")

    def apply(
        self,
        patch: object,
        *,
        expected_revision: object,
        confirmation: object,
    ) -> dict[str, Any]:
        if type(expected_revision) is not int:
            raise ControlError("expected_revision must be an integer")
        if not isinstance(confirmation, str):
            raise ControlError("confirmation is required")
        with self.lock:
            with self.file_lock():
                current = self.read()
                if expected_revision != current["revision"]:
                    raise StaleRevision(
                        f"stale revision: expected {expected_revision}, "
                        f"current {current['revision']}"
                    )
                if not isinstance(patch, dict):
                    raise ControlError("patch must be an object")

                sets_kill = patch.get("kill") is True
                clears_kill = current["kill"] and patch.get("kill") is False
                if sets_kill:
                    if confirmation != "KILL":
                        raise ControlError(
                            "setting kill requires confirmation KILL"
                        )
                elif clears_kill:
                    if confirmation != "RESET KILL":
                        raise ControlError(
                            "clearing kill requires confirmation RESET KILL"
                        )
                    patch = {**patch, "paused": True}
                elif patch.get("paused") is True:
                    if confirmation != "PAUSE":
                        raise ControlError("pausing requires confirmation PAUSE")
                elif confirmation != "APPLY":
                    raise ControlError("update requires confirmation APPLY")
                if current["kill"] and not clears_kill:
                    raise ControlError(
                        "kill is latched; clear it before other updates"
                    )
                if patch.get("paused") is False and (
                    current["kill"] or patch.get("kill") is True
                ):
                    raise ControlError("cannot resume while kill is set")
                if patch.get("paused") is False:
                    self.require_shadow_resume_ready(current)

                updated = next_control(
                    current,
                    patch,
                    hard_max_cost=self.hard_max_cost,
                    hard_max_clip=self.hard_max_clip,
                    hard_max_net=self.hard_max_net,
                )
                atomic_write_json(self.control_path, updated)
                return updated


def make_handler(store: ControlStore, token: str):
    token_bytes = token.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def send_body(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; "
                             "script-src 'unsafe-inline'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, value: object) -> None:
            self.send_body(
                status,
                json.dumps(value, separators=(",", ":")).encode("utf-8"),
                "application/json",
            )

        def authorized(self) -> bool:
            supplied = self.headers.get("X-MM-Control-Token", "").encode("utf-8")
            return bool(supplied) and hmac.compare_digest(supplied, token_bytes)

        def same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True  # allows local CLI/curl with the control token
            host = self.headers.get("Host", "")
            return origin in {f"http://{host}", f"https://{host}"}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self.send_body(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/healthz":
                self.send_json(200, {"ok": True})
            elif path == "/api/state":
                try:
                    self.send_json(200, store.snapshot())
                except ControlError as exc:
                    self.send_json(500, {"error": str(exc)})
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/api/control":
                self.send_json(404, {"error": "not found"})
                return
            if not self.authorized():
                self.send_json(403, {"error": "invalid control token"})
                return
            if not self.same_origin():
                self.send_json(403, {"error": "cross-origin control refused"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    raise ControlError("JSON body must be 1..4096 bytes")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict) or set(body) != {
                    "expected_revision", "confirm", "patch"
                }:
                    raise ControlError(
                        "body fields must be expected_revision, confirm, patch"
                    )
                control = store.apply(
                    body["patch"],
                    expected_revision=body["expected_revision"],
                    confirmation=body["confirm"],
                )
                self.send_json(200, {"ok": True, "control": control})
            except StaleRevision as exc:
                self.send_json(409, {"error": str(exc)})
            except (ControlError, json.JSONDecodeError, UnicodeError, ValueError) as exc:
                self.send_json(400, {"error": str(exc)})

    return Handler


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="localhost crypto MM control console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8932)
    parser.add_argument(
        "--control",
        type=Path,
        default=Path("/home/ubuntu/h6b_inputs/mm_control.json"),
    )
    parser.add_argument(
        "--status",
        type=Path,
        default=Path("/home/ubuntu/h6b_inputs/mm_control_status.json"),
    )
    parser.add_argument("--hard-max-cost", type=float, default=200.0)
    parser.add_argument("--hard-max-clip", default="20.00")
    parser.add_argument("--hard-max-net", type=int, default=100)
    parser.add_argument("--initial-max-cost", type=float, default=0.0)
    parser.add_argument("--initial-clip", default="1.00")
    parser.add_argument("--initial-max-net", type=int, default=0)
    args = parser.parse_args()

    if not _loopback(args.host):
        raise SystemExit("refusing non-loopback bind; use an SSH tunnel")
    token = os.environ.get("MM_CONTROL_TOKEN", "")
    if len(token) < 16:
        raise SystemExit("MM_CONTROL_TOKEN must contain at least 16 characters")

    store = ControlStore(
        args.control,
        args.status,
        hard_max_cost=args.hard_max_cost,
        hard_max_clip=args.hard_max_clip,
        hard_max_net=args.hard_max_net,
        initial_max_cost=args.initial_max_cost,
        initial_clip=args.initial_clip,
        initial_max_net=args.initial_max_net,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, token))
    server.daemon_threads = True
    print(f"Crypto MM control console: http://{args.host}:{args.port}")
    print(f"control: {args.control}")
    print(f"status : {args.status}")
    print("localhost only; no exchange API is present in this process")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
