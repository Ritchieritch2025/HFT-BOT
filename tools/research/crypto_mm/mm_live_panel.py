#!/usr/bin/env python3
"""mm_live_panel.py — real-time MM engine panel. Stdlib only, one file.

Tails the engine's mm_*.ndjson output directory and serves a live dashboard
organized as the DECISION PIPELINE, so every layer shows: what it reads,
what it decided, and why. Layers:

  L0 版本/状态   START config, control revision, halted/paused, uptime
  L1 定价层      RTI, fair, mid, fair-mid, sigma(60/300), locked_n
  L2 报价决策层  edge per side, our px vs book touch, gate/pricing reasons
  L3 库存风险层  net, exposure, open cost, realized, recon
  L4 成交质量层  fills + LIVE 5s/30s markout, pair cycles, orphan stats
  L5 流/毒性层   flow 60s, touch imbalance, sentinel pauses, taker exits

Deploy on the box where the engine writes (e.g. EC2):
    python3 mm_live_panel.py --dir /home/ubuntu/h6b_inputs/mm_engine --port 8791
View from laptop:  ssh -i KEY -L 8791:localhost:8791 ubuntu@HOST  → open
http://localhost:8791
"""
import argparse
import bisect
import collections
import glob
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_HIST = 7200          # points kept per series (~2h at 1/s)
MAX_FILLS = 400
MARKOUT_HORIZONS = (5, 30)

S = {
    "start": {}, "health": {}, "quote": {},          # latest per type
    "hist": collections.deque(maxlen=MAX_HIST),       # (t, fair, mid, rti, sigma, eb, en, net)
    "risk_hist": collections.deque(maxlen=MAX_HIST),  # (t, realized, exposure, open_cost)
    "fills": collections.deque(maxlen=MAX_FILLS),     # dicts, markout filled in lazily
    "pairs": collections.deque(maxlen=MAX_FILLS),
    "fair_by_mt": collections.defaultdict(lambda: (collections.deque(maxlen=MAX_HIST),
                                                   collections.deque(maxlen=MAX_HIST))),
    "counters": collections.Counter(),
    "gate_reasons": collections.Counter(),
    "pricing_reasons": collections.Counter(),
    "cancel_reasons": collections.Counter(),
    "taker_reasons": collections.Counter(),
    "pair_pnl_cum": 0.0,
    "last_event_wall": 0.0,
    "files_seen": {},
}
LOCK = threading.Lock()


def ingest(r):
    ev = r.get("ev")
    t = (r.get("wall_ns") or 0) / 1e9
    S["last_event_wall"] = max(S["last_event_wall"], t)
    S["counters"][ev] += 1
    if ev == "START":
        S["start"] = r
    elif ev == "HEALTH":
        S["health"] = r
        S["risk_hist"].append((t, r.get("realized"), r.get("exposure"), r.get("open_cost")))
    elif ev == "QUOTE_EVAL":
        S["quote"] = r
        S["hist"].append((t, r.get("fair_c"), r.get("mid_c"), r.get("rti"), r.get("sigma"),
                          r.get("edge_bid"), r.get("edge_no"), r.get("net")))
        ts, fs = S["fair_by_mt"][r.get("mt")]
        if r.get("fair_c") is not None:
            ts.append(t)
            fs.append(r["fair_c"])
        S["gate_reasons"][r.get("cycle_gate_reason")] += 1
        S["pricing_reasons"][r.get("pricing_reason")] += 1
    elif ev == "FILL":
        S["fills"].append({"t": t, "mt": r.get("ticker"), "side": r.get("side"),
                           "ct": r.get("count"), "px_c": round(100 * (r.get("px_dollars") or 0), 2),
                           "fee": r.get("fee_dollars"), "m5": None, "m30": None, "f0": None})
    elif ev == "PAIR_LOCK":
        pnl = (r.get("ct") or 0) * (1 - (r.get("px_a") or 0) - (r.get("px_b") or 0)) - (r.get("fee_usd") or 0)
        S["pair_pnl_cum"] += pnl
        S["pairs"].append({"t": t, "mt": r.get("mt"), "ct": r.get("ct"),
                           "sum_c": round(100 * ((r.get("px_a") or 0) + (r.get("px_b") or 0)), 1),
                           "wait_s": r.get("pair_wait_s"), "pnl": round(pnl, 4)})
    elif ev == "INTENT_CANCEL":
        S["cancel_reasons"][r.get("reason")] += 1
    elif ev == "INTENT_TAKER":
        S["taker_reasons"][r.get("reason")] += 1


def fair_at(mt, t, tol=20.0):
    ts, fs = S["fair_by_mt"].get(mt, (None, None))
    if not ts:
        return None
    tl, fl = list(ts), list(fs)
    i = bisect.bisect_right(tl, t) - 1
    if i >= 0 and t - tl[i] <= tol:
        return fl[i]
    j = bisect.bisect_left(tl, t)
    return fl[j] if j < len(fl) and tl[j] - t <= tol else None


def update_markouts():
    now = S["last_event_wall"]
    for f in S["fills"]:
        sgn = 1 if f["side"] == "bid" else -1
        if f["f0"] is None:
            f["f0"] = fair_at(f["mt"], f["t"])
        if f["f0"] is None:
            continue
        for h in MARKOUT_HORIZONS:
            k = f"m{h}"
            if f[k] is None and now >= f["t"] + h:
                fh = fair_at(f["mt"], f["t"] + h)
                if fh is not None:
                    f[k] = round(sgn * (fh - f["f0"]), 3)


def tail_loop(directory, pattern, poll):
    while True:
        try:
            files = sorted(glob.glob(os.path.join(directory, pattern)))
            for path in files:
                pos = S["files_seen"].get(path, 0)
                size = os.path.getsize(path)
                if size <= pos:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    buf = fh.read()
                    # keep partial trailing line for next round
                    nl = buf.rfind("\n")
                    if nl < 0:
                        continue
                    consume, tail_off = buf[:nl + 1], len(buf) - (nl + 1)
                with LOCK:
                    for line in consume.splitlines():
                        try:
                            ingest(json.loads(line))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    S["files_seen"][path] = size - tail_off
            with LOCK:
                update_markouts()
        except Exception:
            pass
        time.sleep(poll)


def snapshot():
    with LOCK:
        q, h = S["quote"], S["health"]
        fills = list(S["fills"])
        mos = {f"m{hz}": [f[f"m{hz}"] for f in fills if f[f"m{hz}"] is not None]
               for hz in MARKOUT_HORIZONS}
        mo_stats = {k: {"n": len(v), "mean": round(sum(v) / len(v), 3) if v else None,
                        "pct_neg": round(100 * sum(1 for x in v if x < 0) / len(v)) if v else None}
                    for k, v in mos.items()}
        hist = list(S["hist"])[-1800:]
        risk = list(S["risk_hist"])[-1800:]
        return json.dumps({
            "now": time.time(),
            "staleness_s": round(time.time() - S["last_event_wall"], 1) if S["last_event_wall"] else None,
            "start": S["start"], "health": h,
            "quote": q,
            "hist": {"t": [x[0] for x in hist], "fair": [x[1] for x in hist],
                     "mid": [x[2] for x in hist], "rti": [x[3] for x in hist],
                     "sigma": [x[4] for x in hist], "eb": [x[5] for x in hist],
                     "en": [x[6] for x in hist], "net": [x[7] for x in hist]},
            "risk": {"t": [x[0] for x in risk], "realized": [x[1] for x in risk],
                     "exposure": [x[2] for x in risk], "open_cost": [x[3] for x in risk]},
            "fills": fills[-60:], "pairs": list(S["pairs"])[-40:],
            "pair_pnl_cum": round(S["pair_pnl_cum"], 4),
            "markout": mo_stats,
            "counters": dict(S["counters"]),
            "gate_reasons": dict(S["gate_reasons"]),
            "pricing_reasons": dict(S["pricing_reasons"]),
            "cancel_reasons": dict(S["cancel_reasons"]),
            "taker_reasons": dict(S["taker_reasons"]),
        })


PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>MM Live Panel</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js"></script>
<style>
body{background:#0d1117;color:#dde3ea;font-family:-apple-system,Segoe UI,sans-serif;margin:0;padding:10px 18px}
h2{font-size:13px;color:#9fb6d0;border-bottom:1px solid #21262d;margin:18px 0 6px;padding-bottom:3px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:5px 10px;font-size:12px}
.chip b{font-size:14px}
.ok{color:#7ee787}.bad{color:#ff7b72}.warn{color:#ffa657}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.plot{height:230px}
table{border-collapse:collapse;font-size:11px;width:100%}
td,th{border:1px solid #21262d;padding:2px 6px;text-align:right}
th{color:#9fb6d0}
#stale{position:fixed;top:8px;right:14px;font-size:12px}
</style></head><body>
<div id="stale"></div>
<h2>L0 版本 / 引擎状态 <span style="color:#566">— 这轮跑的是什么配置</span></h2><div class="chips" id="l0"></div>
<h2>L1 定价层 <span style="color:#566">— 模型看什么:指数→σ→fair</span></h2><div class="chips" id="l1"></div>
<div class="grid"><div id="p_fair" class="plot"></div><div id="p_sigma" class="plot"></div></div>
<h2>L2 报价决策层 <span style="color:#566">— 挂单逻辑:edge够不够、门开不开</span></h2><div class="chips" id="l2"></div>
<div class="grid"><div id="p_edge" class="plot"></div><div id="p_reasons" class="plot"></div></div>
<h2>L3 库存 / 风险层</h2><div class="chips" id="l3"></div>
<div class="grid"><div id="p_risk" class="plot"></div><div id="p_net" class="plot"></div></div>
<h2>L4 成交质量层 <span style="color:#566">— 谁在跟我们成交:markout是裁判</span></h2><div class="chips" id="l4"></div>
<div class="grid"><div id="t_fills"></div><div id="t_pairs"></div></div>
<h2>L5 流 / 毒性层</h2><div class="chips" id="l5"></div>
<script>
const $=id=>document.getElementById(id);
const L={paper_bgcolor:'#0d1117',plot_bgcolor:'#11161d',font:{color:'#dde3ea',size:10},
 margin:{l:45,r:15,t:24,b:30},xaxis:{gridcolor:'#1c2430'},yaxis:{gridcolor:'#1c2430'},
 legend:{orientation:'h',y:1.18},title:{font:{size:11}}};
const lay=o=>Object.assign(JSON.parse(JSON.stringify(L)),o||{});
const chip=(k,v,cls)=>`<div class="chip">${k}<br><b class="${cls||''}">${v==null?'—':v}</b></div>`;
const ts=a=>a.map(x=>new Date(x*1000).toISOString().substr(11,8));
async function tick(){
 let d;try{d=await(await fetch('/data.json')).json()}catch(e){$('stale').innerHTML='<span class=bad>面板断线</span>';return}
 const q=d.quote||{},h=d.health||{},s=d.start||{};
 $('stale').innerHTML=d.staleness_s==null?'':(d.staleness_s>10?`<span class=bad>数据滞后 ${d.staleness_s}s</span>`:`<span class=ok>live ${d.staleness_s}s</span>`);
 $('l0').innerHTML=chip('mode',s.mode)+chip('series',(s.series||[]).join(','))+chip('clip',s.clip)
  +chip('pair_prequote',s.pair_prequote)+chip('ctrl_rev',h.control_revision)
  +chip('halted',h.halted,h.halted?'bad':'ok')+chip('paused',h.paused,h.paused?'warn':'ok')
  +chip('kill',h.kill,h.kill?'bad':'ok')+chip('recon_fails',h.recon_fails,h.recon_fails?'bad':'ok');
 $('l1').innerHTML=chip('market',q.mt)+chip('tte s',q.tte)+chip('RTI',q.rti)
  +chip('fair ¢',q.fair_c)+chip('mid ¢',q.mid_c)
  +chip('fair−mid',q.fair_c!=null&&q.mid_c!=null?(q.fair_c-q.mid_c).toFixed(2):null,
        Math.abs(q.fair_c-q.mid_c)>5?'warn':'')
  +chip('σ60',q.sigma_short)+chip('σ300',q.sigma_long)+chip('locked_n',q.locked_n)
  +chip('pricing',q.pricing_reason,q.pricing_reason==='ready'?'ok':'warn');
 $('l2').innerHTML=chip('edge YES ¢',q.edge_bid)+chip('edge NO ¢',q.edge_no)
  +chip('our YES bid',q.y_px!=null?(100*q.y_px).toFixed(1):null)
  +chip('our NO bid',q.n_px!=null?(100*q.n_px).toFixed(1):null)
  +chip('book Ybid/Yask',q.yb!=null?(q.yb/100)+'/'+(q.ya/100):null)
  +chip('spread ¢',q.spread_c)+chip('gate',q.cycle_gate_reason)
  +chip('want bid/no',(q.want_bid?'Y':'n')+'/'+(q.want_no?'Y':'n'));
 $('l3').innerHTML=chip('net',q.net)+chip('exposure',h.exposure)
  +chip('open $',h.open_cost)+chip('max $',h.max_open_cost)
  +chip('realized $',h.realized,(h.realized||0)<0?'bad':'ok')
  +chip('orders',h.orders)+chip('requote_supp',h.requote_suppressed);
 const mo=d.markout||{};
 $('l4').innerHTML=chip('fills',(d.counters||{}).FILL||0)
  +chip('markout5s ¢',mo.m5&&mo.m5.mean,mo.m5&&mo.m5.mean<0?'bad':'ok')
  +chip('neg%@5s',mo.m5&&mo.m5.pct_neg)+chip('markout30s ¢',mo.m30&&mo.m30.mean,mo.m30&&mo.m30.mean<0?'bad':'ok')
  +chip('pair cycles',(d.counters||{}).PAIR_LOCK||0)+chip('pair PnL $',d.pair_pnl_cum,(d.pair_pnl_cum||0)<0?'bad':'ok')
  +chip('taker exits',(d.counters||{}).INTENT_TAKER||0,'warn');
 $('l5').innerHTML=chip('Yflow60',q.y_flow_60s)+chip('Nflow60',q.n_flow_60s)
  +chip('imbalance',q.touch_imbalance)+chip('queue Y/N',
    (q.y_queue_ahead!=null?q.y_queue_ahead:'—')+' / '+(q.n_queue_ahead!=null?q.n_queue_ahead:'—'))
  +chip('sentinel pauses',(d.counters||{}).SENTINEL_PAUSE||0,'warn')
  +chip('cancels',JSON.stringify(d.cancel_reasons||{}));
 const H=d.hist,T=ts(H.t);
 Plotly.react('p_fair',[{x:T,y:H.fair,name:'fair',line:{color:'#58a6ff',width:1}},
  {x:T,y:H.mid,name:'mid',line:{color:'#d29922',width:1}}],lay({title:{text:'fair vs mid (¢)'}}));
 Plotly.react('p_sigma',[{x:T,y:H.sigma,name:'σ used',line:{color:'#f778ba',width:1}},
  {x:T,y:H.rti,name:'RTI',yaxis:'y2',line:{color:'#8b949e',width:1}}],
  lay({title:{text:'σ 与 指数'},yaxis2:{overlaying:'y',side:'right',gridcolor:'#1c2430'}}));
 Plotly.react('p_edge',[{x:T,y:H.eb,name:'edge YES',line:{color:'#7ee787',width:1}},
  {x:T,y:H.en,name:'edge NO',line:{color:'#ff7b72',width:1}}],lay({title:{text:'edge per side (¢)'}}));
 const gr=d.gate_reasons||{};
 Plotly.react('p_reasons',[{x:Object.keys(gr),y:Object.values(gr),type:'bar',marker:{color:'#58a6ff'}}],
  lay({title:{text:'为什么不挂/挂 (gate reasons 累计)'}}));
 const R=d.risk,TR=ts(R.t);
 Plotly.react('p_risk',[{x:TR,y:R.realized,name:'realized $',line:{color:'#7ee787',width:1.5}},
  {x:TR,y:R.open_cost,name:'open $',line:{color:'#d29922',width:1}}],lay({title:{text:'PnL 与资金占用'}}));
 Plotly.react('p_net',[{x:T,y:H.net,name:'net',line:{color:'#a371f7',width:1}}],lay({title:{text:'净库存'}}));
 const ftab=(d.fills||[]).slice(-12).reverse().map(f=>
  `<tr><td>${new Date(f.t*1000).toISOString().substr(11,8)}</td><td>${f.side}</td><td>${f.px_c}</td>`+
  `<td class="${f.m5<0?'bad':'ok'}">${f.m5==null?'…':f.m5}</td><td class="${f.m30<0?'bad':'ok'}">${f.m30==null?'…':f.m30}</td></tr>`).join('');
 $('t_fills').innerHTML=`<table><tr><th>成交</th><th>side</th><th>px¢</th><th>mo5s</th><th>mo30s</th></tr>${ftab}</table>`;
 const ptab=(d.pairs||[]).slice(-12).reverse().map(p=>
  `<tr><td>${new Date(p.t*1000).toISOString().substr(11,8)}</td><td>${p.sum_c}</td><td>${p.wait_s}</td>`+
  `<td class="${p.pnl<0?'bad':'ok'}">${p.pnl}</td></tr>`).join('');
 $('t_pairs').innerHTML=`<table><tr><th>配对</th><th>sum¢</th><th>wait s</th><th>pnl $</th></tr>${ptab}</table>`;
}
tick();setInterval(tick,1000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/data.json"):
            body = snapshot().encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="engine output dir with mm_*.ndjson")
    ap.add_argument("--glob", default="mm_*.ndjson")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--poll", type=float, default=0.5)
    a = ap.parse_args()
    threading.Thread(target=tail_loop, args=(a.dir, a.glob, a.poll), daemon=True).start()
    print(f"panel on http://localhost:{a.port}  (tailing {a.dir}/{a.glob})")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
