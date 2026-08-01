#!/usr/bin/env python3
"""Observe panel v2 — diagnostic view (operator 5-section spec).

http://localhost:8931 · pulls shadow v3 receipts from prod via SSH every
2s.  Sections: health strip / BRTI / markets (FV vs book divergence) /
our open quotes & positions / cumulative results + skip reasons /
self-explaining event feed with receipt refs.  Read-only, no orders.
"""
import json, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SSH = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
       "-i", "/Users/ritcardo/.ssh/kalshi-key.pem", "ubuntu@3.130.232.109"]
TAIL = ("tail -q -n 9000 $(ls -t /home/ubuntu/h6b_inputs/shadow/"
        "shadow_*.ndjson | head -2) 2>/dev/null")

STATE = {"updated": 0}

def poll():
    while True:
        try:
            out = subprocess.run(SSH + [TAIL], capture_output=True, text=True,
                                 timeout=15).stdout
            rti, tops, feed = [], {}, []
            health, skips_min, skips_tot = {}, {}, {}
            quotes, positions, settles = {}, [], []
            counts = {"PLACE": 0, "CANCEL": 0, "FILL": 0, "SETTLE": 0}
            for line in out.splitlines():
                try: o = json.loads(line)
                except ValueError: continue
                ev = o.get("ev")
                if ev == "RTI": rti.append(o["v"])
                elif ev == "TOP": tops[o["mt"]] = o
                elif ev == "HEALTH": health = o
                elif ev == "SKIPS":
                    skips_min = o.get("counts", {})
                    for k, v in skips_min.items():
                        skips_tot[k] = skips_tot.get(k, 0) + v
                elif ev in ("PLACE", "CANCEL", "FILL", "SETTLE"):
                    counts[ev] += 1
                    feed.append(o)
                    key = (o.get("mt"), o.get("side"))
                    if ev == "PLACE": quotes[key] = o
                    elif ev == "CANCEL": quotes.pop(key, None)
                    elif ev == "FILL":
                        quotes.pop(key, None)
                        positions.append(o)
                    elif ev == "SETTLE":
                        settles.append(o)
                        for p in positions:
                            if (p.get("mt") == o.get("mt")
                                    and p.get("side") == o.get("side")
                                    and not p.get("_settled")):
                                p["_settled"] = True; break
            STATE.update(
                rti=rti[-600:], tops=tops, health=health,
                skips_min=skips_min, skips_tot=skips_tot,
                quotes=[{"mt": k[0], "side": k[1], **v}
                        for k, v in quotes.items()],
                positions=[p for p in positions if not p.get("_settled")],
                settles=settles, counts=counts,
                feed=feed[-40:], updated=time.time())
        except Exception:
            pass
        time.sleep(2)

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>BTC15M 策略诊断台</title><style>
body{background:#0d1117;color:#d7e2ea;font:12.5px Menlo,monospace;margin:14px}
h3{color:#5fd4a2;margin:14px 0 4px}.big{font-size:30px;color:#fff}
table{border-collapse:collapse}td,th{border:1px solid #263038;padding:3px 8px;text-align:right}
th{background:#16202a;color:#9fb6c6}.pos{color:#5fd4a2}.neg{color:#e06c60}
.mut{color:#6b7c8a}.warn{color:#e6b450}
.chip{display:inline-block;padding:2px 8px;border-radius:3px;margin-right:6px;background:#163324;color:#5fd4a2}
.chip.bad{background:#3a1a16;color:#e06c60}
#feed div{padding:1px 0;border-bottom:1px dotted #1b242c}
canvas{background:#0a0e13;border:1px solid #263038}
.grid{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}
</style></head><body>
<div id="strip"></div>
<div class="grid">
 <div><span class="big" id="brti">—</span> <span class="mut">BRTI</span><br>
  <canvas id="spark" width="640" height="90"></canvas></div>
 <div><h3 style="margin-top:0">今日成绩</h3><div id="score"></div>
  <canvas id="pnl" width="360" height="90"></canvas>
  <div class="mut">白线=我们(已结算) 灰线=基线0 橙=陈旧锚斜率 红=坟场斜率</div></div>
</div>
<h3>市场:模型 vs 盘口(分歧越大越该盯)</h3><table id="mkts"></table>
<h3>我方挂单 &amp; 影子持仓</h3><div class="grid">
 <table id="q"></table><table id="pos"></table></div>
<h3>为什么没挂单(最近一分钟 / 累计)——策略被哪道门卡住</h3><div id="skips"></div>
<h3>决策流(每条含完整证据,src=收据行)</h3><div id="feed"></div>
<script>
const Z=(s)=>s==null?'—':s;
function zone(t){return t<=300?'撤退区':(t<600?'过渡(不挂)':(t<1800?'挂单窗':'太早'))}
async function tick(){
 const d = await (await fetch('/data')).json();
 const h = d.health||{}; const age = Math.round(Date.now()/1000-d.updated);
 const chip=(ok,txt)=>`<span class="chip${ok?'':' bad'}">${txt}</span>`;
 document.getElementById('strip').innerHTML =
  chip(h.rti_age_s<5,'RTI '+Z(h.rti_age_s)+'s')+
  chip(h.cf_ws_age_s<10,'CF-WS '+Z(h.cf_ws_age_s)+'s')+
  chip(h.md_ws_age_s<10,'MD-WS '+Z(h.md_ws_age_s)+'s')+
  chip(true,'市场 '+Z(h.markets))+
  chip(true,'运行 '+Math.floor((h.uptime_s||0)/60)+'m')+
  chip(true,'挂单 '+Z(h.open_quotes)+' 持仓 '+Z(h.positions_open))+
  chip(age<8,'面板同步 '+age+'s前');
 if(d.rti&&d.rti.length){
  document.getElementById('brti').textContent=d.rti[d.rti.length-1].toFixed(2);
  const c=document.getElementById('spark').getContext('2d');
  c.clearRect(0,0,640,90);const a=d.rti,mn=Math.min(...a),mx=Math.max(...a),sp=(mx-mn)||1;
  c.strokeStyle='#5fd4a2';c.beginPath();
  a.forEach((v,i)=>{const x=i*640/a.length,y=82-(v-mn)/sp*74;i?c.lineTo(x,y):c.moveTo(x,y)});
  c.stroke();c.fillStyle='#6b7c8a';c.fillText(mx.toFixed(0),4,10);c.fillText(mn.toFixed(0),4,88);}
 // score + pnl curve
 const st=d.settles||[];const tot=st.reduce((s,x)=>s+x.pnl_c*5/100,0);
 const mean=st.length?(st.reduce((s,x)=>s+x.pnl_c,0)/st.length):null;
 const fills=d.counts.FILL||0, places=d.counts.PLACE||0;
 document.getElementById('score').innerHTML=
  `挂单 ${places} · 成交 ${fills} · 成交率 ${places?(100*fills/places).toFixed(1):0}%<br>`+
  `已结算 ${st.length} 笔 · 每张净益 <b class="${mean>0?'pos':'neg'}">${mean==null?'—':mean.toFixed(2)+'¢'}</b>`+
  ` · 累计 <b class="${tot>=0?'pos':'neg'}">$${tot.toFixed(2)}</b> · 未结算 ${(d.positions||[]).length}`;
 const pc=document.getElementById('pnl').getContext('2d');
 pc.clearRect(0,0,360,90);
 if(st.length){let c1=0;const ys=[0];st.forEach(x=>{c1+=x.pnl_c*5/100;ys.push(c1)});
  const mn=Math.min(0,...ys),mx=Math.max(0.01,...ys),sp=mx-mn;
  const line=(slope,color)=>{pc.strokeStyle=color;pc.beginPath();
   for(let i=0;i<ys.length;i++){const y=82-((slope*i*5/100)-mn)/sp*74;
    const x=i*360/(ys.length-1||1);i?pc.lineTo(x,y):pc.moveTo(x,y)}pc.stroke()};
  line(0,'#3a4652');line(-1.5,'#8a5a20');line(-3.5,'#6a2a20');
  pc.strokeStyle='#fff';pc.beginPath();
  ys.forEach((v,i)=>{const x=i*360/(ys.length-1||1),y=82-(v-mn)/sp*74;
   i?pc.lineTo(x,y):pc.moveTo(x,y)});pc.stroke();}
 // markets
 const mk=document.getElementById('mkts');
 mk.innerHTML='<tr><th>market</th><th>剩余</th><th>时段</th><th>bid/ask¢</th><th>模型FV¢</th><th>分歧</th><th>edgeY</th><th>edgeN</th><th>波动态</th><th>σ/s</th></tr>';
 Object.values(d.tops||{}).sort((a,b)=>a.tte-b.tte).forEach(t=>{
  const mid=(t.yb+t.ya)/200, fv=t.p*100, dv=fv-mid;
  const r=mk.insertRow(-1);
  r.innerHTML=`<td>${t.mt.slice(-11)}</td><td>${Math.floor(t.tte/60)}m${(''+t.tte%60).padStart(2,'0')}</td>`+
   `<td>${zone(t.tte)}</td><td>${(t.yb/100).toFixed(1)}/${(t.ya/100).toFixed(1)}</td>`+
   `<td>${fv.toFixed(1)}</td><td class="${Math.abs(dv)>3?'warn':'mut'}">${dv>0?'+':''}${dv.toFixed(1)}</td>`+
   `<td class="${t.ey>=2?'pos':'mut'}">${t.ey.toFixed(1)}</td>`+
   `<td class="${t.en>=2?'pos':'mut'}">${t.en.toFixed(1)}</td><td>${t.state}</td><td>${t.sigma}</td>`;});
 // quotes & positions
 const q=document.getElementById('q');
 q.innerHTML='<tr><th colspan=5>在场挂单</th></tr><tr><th>market</th><th>侧</th><th>价¢</th><th>排队前方</th><th>已等</th></tr>';
 (d.quotes||[]).forEach(x=>{const r=q.insertRow(-1);
  r.innerHTML=`<td>${x.mt.slice(-11)}</td><td>${x.side}</td><td>${x.lvl_c}</td><td>${x.queue_ahead}</td><td>${Math.round(Date.now()/1000-x.wall_ns/1e9)}s</td>`;});
 const po=document.getElementById('pos');
 po.innerHTML='<tr><th colspan=5>影子持仓(未结算)</th></tr><tr><th>market</th><th>侧</th><th>成本¢</th><th>现FV¢</th><th>浮动¢/张</th></tr>';
 (d.positions||[]).forEach(x=>{const t=(d.tops||{})[x.mt];
  const fv=t?t.p*100:null;
  const fl=fv==null?null:(x.side==='y'?fv-x.entry_c*1:100-fv-(100-x.entry_c*1));
  const r=po.insertRow(-1);
  r.innerHTML=`<td>${x.mt.slice(-11)}</td><td>${x.side}</td><td>${x.entry_c}</td>`+
   `<td>${fv==null?'—':fv.toFixed(1)}</td><td class="${fl>0?'pos':'neg'}">${fl==null?'—':fl.toFixed(1)}</td>`;});
 // skips
 const sk=d.skips_tot||{}, sm=d.skips_min||{};
 const names={edge_below_theta:'edge不足',outside_window:'时段不对',burst_gate:'风暴闸',
  withdraw_zone:'撤退区',one_sided_book:'单边簿',no_model_or_book:'缺锚/缺簿'};
 document.getElementById('skips').innerHTML=Object.keys(names).map(k=>
  `<span class="chip" style="background:#1a2430;color:#9fb6c6">${names[k]}: ${sm[k]||0} / ${sk[k]||0}</span>`).join(' ');
 // feed
 document.getElementById('feed').innerHTML=(d.feed||[]).slice().reverse().map(e=>{
  const ts=new Date(e.wall_ns/1e6).toLocaleTimeString();
  if(e.ev==='PLACE') return `<div>[${ts}] <b class="pos">挂</b> ${e.mt.slice(-11)} ${e.side==='y'?'YES':'NO'} @${e.lvl_c}¢ ×${e.clip} | 排队第${e.queue_ahead}张后 | 理由: BRTI ${e.rti} 距线${e.dist>0?'+':''}${e.dist} → FV ${e.fv_c}¢, 盘口便宜 ${e.edge_c}¢(≥θ${e.theta}) | 剩${Math.floor(e.tte_s/60)}m(${zone(e.tte_s)}) ${e.state} σ=${e.sigma} <span class="mut">${e.src}</span></div>`;
  if(e.ev==='CANCEL') return `<div class="mut">[${ts}] 撤 ${e.mt.slice(-11)} ${e.side} @${e.lvl_c}¢ | ${e.reason} | FV ${e.fv_c}¢ edge ${e.edge_c}¢ 等了${e.waited_s}s <span>${e.src}</span></div>`;
  if(e.ev==='FILL') return `<div class="warn">[${ts}] <b>成交</b> ${e.mt.slice(-11)} ${e.side} @${e.entry_c}¢ ×${e.clip} 排队${e.waited_s}s后轮到 <span class="mut">${e.src}</span></div>`;
  if(e.ev==='SETTLE') return `<div class="${e.pnl_c>0?'pos':'neg'}">[${ts}] <b>结算</b> ${e.mt.slice(-11)} ${e.side} 成本${e.entry_c}¢ 结果=${e.result} → ${e.pnl_c>0?'+':''}${e.pnl_c}¢/张 <span class="mut">${e.src}</span></div>`;
  return '';}).join('');
}
setInterval(tick,2000);tick();
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/data":
            body = json.dumps(STATE).encode(); ct = "application/json"
        else:
            body = PAGE.encode(); ct = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

if __name__ == "__main__":
    threading.Thread(target=poll, daemon=True).start()
    print("panel v2: http://localhost:8931")
    ThreadingHTTPServer(("127.0.0.1", 8931), H).serve_forever()
