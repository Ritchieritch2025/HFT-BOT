#!/usr/bin/env python3
"""mm_report.py — build a single-file interactive HTML report from mm_engine NDJSON logs.

Usage:
    python3 mm_report.py LOG1.ndjson [LOG2.ndjson ...] [--out REPORT.html]

Series-agnostic: everything is grouped by market ticker (mt). Works for any
Kalshi series the engine quotes, not just BTC.
"""
import argparse
import bisect
import collections
import glob
import json
import math
import statistics
import sys
from datetime import datetime, timezone

PLOTLY_CDN = "https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js"
MARKOUT_HORIZONS = (10, 30, 60)  # seconds


def iso(ns):
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def load(paths):
    rows, seen = [], set()
    for p in paths:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (r.get("ev"), r.get("wall_ns"), r.get("mt") or r.get("ticker"),
                       r.get("fill_id") or r.get("order_id") or r.get("src"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
    rows.sort(key=lambda r: r.get("wall_ns") or 0)
    return rows


def build(rows):
    ev = collections.defaultdict(list)
    for r in rows:
        ev[r.get("ev")].append(r)

    q = [r for r in ev["QUOTE_EVAL"] if r.get("wall_ns")]
    health = ev["HEALTH"]
    fills = ev["FILL"]
    pauses = ev["SENTINEL_PAUSE"]
    intents = ev["INTENT_PLACE"]
    takers = ev["INTENT_TAKER"]
    pairs = ev["PAIR_LOCK"]
    anomalies = ev["CF_VALUE_REJ"] + ev["CF_ERR"] + ev["LIMIT_BREACH"]

    def series(recs, fields, tkey="wall_ns"):
        out = {"t": [iso(r[tkey]) for r in recs]}
        for f in fields:
            out[f] = [r.get(f) for r in recs]
        return out

    d = {}
    d["quote"] = series(q, ["mt", "tte", "fair_c", "mid_c", "sigma", "sigma_short",
                            "sigma_long", "rti", "spread_c", "edge_bid", "edge_no", "net",
                            "y_queue_ahead", "n_queue_ahead", "y_clear_eta_s", "n_clear_eta_s",
                            "y_flow_60s", "n_flow_60s", "touch_imbalance", "pair_quote_sum_c",
                            "locked_n", "yb", "ya", "y_px", "n_px",
                            "want_bid", "want_no", "cycle_gate_reason", "pricing_reason"])
    d["health"] = series(health, ["exposure", "open_cost", "orders", "realized",
                                  "requote_suppressed", "recon_fails", "halted", "paused"])
    d["fill"] = series(fills, ["ticker", "side", "count", "px_dollars", "fee_dollars"])
    d["pause"] = series(pauses, ["mt", "fv", "mid", "tte", "sigma"])
    d["intent"] = series(intents, ["mt", "side", "px", "edge_c", "qty", "risk_reducing"])
    d["taker"] = series(takers, ["mt", "side", "px", "count", "reason"])
    d["pair"] = series(pairs, ["mt", "ct", "px_a", "px_b", "locked_usd", "pair_wait_s",
                               "fee_usd", "realized"])
    # PAIR_LOCK 'realized' is the engine's cumulative running total; derive per-cycle pnl
    d["pair"]["cycle_pnl"] = [
        round((r.get("ct") or 0) * (1.0 - (r.get("px_a") or 0) - (r.get("px_b") or 0))
              - (r.get("fee_usd") or 0), 4) for r in pairs]
    d["anomaly"] = series(anomalies, ["ev", "mt"]) if anomalies else {"t": [], "ev": [], "mt": []}

    # ---- markouts: fair_c trajectory per market, sampled around each fill ----
    fair_by_mt = collections.defaultdict(lambda: ([], []))  # mt -> (ts_ns[], fair[])
    for r in q:
        if r.get("fair_c") is not None:
            ts, fs = fair_by_mt[r["mt"]]
            ts.append(r["wall_ns"])
            fs.append(r["fair_c"])

    def fair_at(mt, t_ns, tol_ns=20e9):
        ts, fs = fair_by_mt.get(mt, ([], []))
        if not ts:
            return None
        i = bisect.bisect_right(ts, t_ns) - 1
        if i < 0 or t_ns - ts[i] > tol_ns:
            # fall forward if nothing behind us
            j = bisect.bisect_left(ts, t_ns)
            if j < len(ts) and ts[j] - t_ns <= tol_ns:
                return fs[j]
            return None
        return fs[i]

    mk = {"t": [], "mt": [], "side": [], "px_c": []}
    for h in MARKOUT_HORIZONS:
        mk[f"m{h}"] = []
    for r in fills:
        t0 = r["wall_ns"]
        mt = r.get("ticker")
        f0 = fair_at(mt, t0)
        sgn = 1.0 if r.get("side") == "bid" else -1.0
        mk["t"].append(iso(t0))
        mk["mt"].append(mt)
        mk["side"].append(r.get("side"))
        mk["px_c"].append(round(100 * (r.get("px_dollars") or 0), 2))
        for h in MARKOUT_HORIZONS:
            fh = fair_at(mt, t0 + h * 1e9)
            mk[f"m{h}"].append(round(sgn * (fh - f0), 3) if (f0 is not None and fh is not None) else None)
    d["markout"] = mk

    # ---- summary ----
    t0 = iso(rows[0]["wall_ns"]) if rows else ""
    t1 = iso(rows[-1]["wall_ns"]) if rows else ""
    pair_pnl = sum(d["pair"]["cycle_pnl"])
    fees = sum(r.get("fee_dollars") or 0 for r in fills)
    mo = {h: [v for v in mk[f"m{h}"] if v is not None] for h in MARKOUT_HORIZONS}
    d["summary"] = {
        "window": f"{t0} → {t1} UTC",
        "events": len(rows),
        "markets": len({r.get('mt') for r in q}),
        "quote_evals": len(q),
        "fills": len(fills),
        "fees_usd": round(fees, 4),
        "pair_cycles": len(pairs),
        "pair_pnl_usd": round(pair_pnl, 4),
        "sentinel_pauses": len(pauses),
        "taker_exits": len(takers),
        "limit_breaches": len(ev["LIMIT_BREACH"]),
        "recon_fails_max": max((r.get("recon_fails") or 0 for r in health), default=0),
        "final_realized_usd": (health[-1].get("realized") if health else None),
        "markout_mean_c": {str(h): (round(statistics.mean(v), 3) if v else None) for h, v in mo.items()},
        "markout_n": {str(h): len(v) for h, v in mo.items()},
        "gate_counts": dict(collections.Counter(r.get("cycle_gate_reason") for r in q)),
        "pricing_counts": dict(collections.Counter(r.get("pricing_reason") for r in q)),
    }
    return d


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>MM Engine Report</title>
<script src="__PLOTLY__"></script>
<style>
 body{background:#111418;color:#dde3ea;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:18px 26px}
 h1{font-size:20px;margin:4px 0 2px} h2{font-size:15px;margin:26px 0 6px;color:#9fb6d0;border-bottom:1px solid #2a3340;padding-bottom:4px}
 .sub{color:#7d8896;font-size:12px;margin-bottom:10px}
 .cards{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
 .card{background:#1a2027;border:1px solid #2a3340;border-radius:8px;padding:8px 14px;min-width:110px}
 .card .v{font-size:17px;font-weight:600} .card .k{font-size:11px;color:#7d8896}
 .neg{color:#ff7b72}.pos{color:#7ee787}
 .plot{width:100%;height:340px;margin-bottom:8px}
 .tall{height:420px}.short{height:260px}
</style></head><body>
<h1>Crypto MM Engine — Pipeline Report</h1>
<div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<h2>1 · Fair value engine — index, fair vs market mid</h2>
<div id="rti" class="plot short"></div>
<div id="fair" class="plot tall"></div>
<h2>2 · Volatility estimator</h2>
<div id="sigma" class="plot"></div>
<h2>3 · Quoting — book, our prices, spread</h2>
<div id="quotes" class="plot tall"></div>
<div id="edges" class="plot"></div>
<h2>4 · Order intents & edge captured at placement</h2>
<div id="intents" class="plot"></div>
<h2>5 · Fills & markouts (adverse selection)</h2>
<div id="fills" class="plot"></div>
<div id="markout" class="plot"></div>
<h2>6 · Inventory, exposure & realized PnL</h2>
<div id="risk" class="plot tall"></div>
<h2>7 · Pair engine economics</h2>
<div id="pair" class="plot"></div>
<h2>8 · Microstructure — queue, flow, imbalance</h2>
<div id="micro" class="plot tall"></div>
<h2>9 · Gates, pauses & anomalies</h2>
<div id="gates" class="plot short"></div>
<div id="tte" class="plot short"></div>
<script>
const D = __DATA__;
const L = {paper_bgcolor:'#111418',plot_bgcolor:'#161b22',font:{color:'#dde3ea',size:11},
  margin:{l:55,r:20,t:28,b:40},xaxis:{gridcolor:'#232b36'},yaxis:{gridcolor:'#232b36'},
  legend:{orientation:'h',y:1.12},hovermode:'x unified'};
const lay = o => Object.assign(JSON.parse(JSON.stringify(L)), o||{});
const C = {fair:'#58a6ff',mid:'#d29922',rti:'#8b949e',s:'#f778ba',sl:'#a371f7',ss:'#79c0ff',
  bid:'#7ee787',ask:'#ff7b72',pause:'#ffa657'};

// summary cards
const S = D.summary;
document.getElementById('sub').textContent = S.window + '   ·   ' + S.events + ' events · ' + S.markets + ' markets';
const fmt=(v)=> v==null?'—':v;
const cards=[['Fills',S.fills],['Pair cycles',S.pair_cycles],['Pair PnL $',S.pair_pnl_usd],
 ['Final realized $',S.final_realized_usd],['Fees $',S.fees_usd],
 ['Markout 30s ¢',S.markout_mean_c['30']],['Sentinel pauses',S.sentinel_pauses],
 ['Taker exits',S.taker_exits],['Limit breaches',S.limit_breaches],['Recon fails',S.recon_fails_max]];
document.getElementById('cards').innerHTML=cards.map(([k,v])=>{
 const cls=(typeof v==='number'&&(k.includes('PnL')||k.includes('realized')||k.includes('Markout')))?(v<0?'neg':'pos'):'';
 return `<div class="card"><div class="v ${cls}">${fmt(v)}</div><div class="k">${k}</div></div>`}).join('');

// helper: break lines between markets so plotly doesn't connect across expiries
function perMarket(t, mt, y){ const T=[],Y=[]; for(let i=0;i<t.length;i++){ if(i>0&&mt[i]!==mt[i-1]){T.push(null);Y.push(null);} T.push(t[i]);Y.push(y[i]); } return [T,Y]; }
const Q=D.quote;
const [Tq,Fair]=perMarket(Q.t,Q.mt,Q.fair_c), [,Mid]=perMarket(Q.t,Q.mt,Q.mid_c);

// 1 RTI + fair vs mid
Plotly.newPlot('rti',[{x:Q.t,y:Q.rti,name:'RTI index',line:{color:C.rti,width:1}}],
 lay({title:{text:'Underlying index (RTI)',font:{size:13}}}),{responsive:true});
const pauseTr={x:D.pause.t,y:D.pause.fv,mode:'markers',name:'sentinel pause (fair)',
 marker:{color:C.pause,size:5,symbol:'x'}};
const fillTr={x:D.fill.t,y:D.fill.px_dollars.map(p=>100*p),mode:'markers',name:'fills (px ¢)',
 marker:{color:D.fill.side.map(s=>s==='bid'?C.bid:C.ask),size:7,symbol:'circle-open',line:{width:2}}};
Plotly.newPlot('fair',[
 {x:Tq,y:Fair,name:'fair ¢',line:{color:C.fair,width:1}},
 {x:Tq,y:Mid,name:'mid ¢',line:{color:C.mid,width:1}}, pauseTr, fillTr],
 lay({title:{text:'Fair value vs market mid (¢) — gaps = market roll',font:{size:13}},yaxis:{gridcolor:'#232b36',range:[0,100]}}),{responsive:true});

// 2 sigma
Plotly.newPlot('sigma',[
 {x:Q.t,y:Q.sigma_short,name:'σ 60s',line:{color:C.ss,width:1}},
 {x:Q.t,y:Q.sigma_long,name:'σ 300s',line:{color:C.sl,width:1}},
 {x:Q.t,y:Q.sigma,name:'σ used = max',line:{color:C.s,width:1.5}}],
 lay({title:{text:'Per-second volatility estimate ($/s)',font:{size:13}}}),{responsive:true});

// 3 quotes vs book
const [ ,Yb]=perMarket(Q.t,Q.mt,Q.yb.map(v=>v==null?null:v/100));
const [ ,Ya]=perMarket(Q.t,Q.mt,Q.ya.map(v=>v==null?null:v/100));
const [ ,Ypx]=perMarket(Q.t,Q.mt,Q.y_px.map(v=>v==null?null:100*v));
const [ ,Npx]=perMarket(Q.t,Q.mt,Q.n_px.map(v=>v==null?null:100-100*v));
Plotly.newPlot('quotes',[
 {x:Tq,y:Yb,name:'book YES bid',line:{color:'#3d5878',width:1}},
 {x:Tq,y:Ya,name:'book YES ask',line:{color:'#7d5836',width:1}},
 {x:Tq,y:Ypx,name:'our YES bid',line:{color:C.bid,width:1,dash:'dot'}},
 {x:Tq,y:Npx,name:'our NO bid (as YES ask)',line:{color:C.ask,width:1,dash:'dot'}}],
 lay({title:{text:'Book touch vs our quotes (¢)',font:{size:13}}}),{responsive:true});
const [ ,Eb]=perMarket(Q.t,Q.mt,Q.edge_bid); const [ ,En]=perMarket(Q.t,Q.mt,Q.edge_no);
Plotly.newPlot('edges',[
 {x:Tq,y:Eb,name:'edge YES side ¢',line:{color:C.bid,width:1}},
 {x:Tq,y:En,name:'edge NO side ¢',line:{color:C.ask,width:1}},
 {x:Tq,y:Q.spread_c,name:'spread ¢',line:{color:C.rti,width:1}}],
 lay({title:{text:'Model edge per side & book spread (¢)',font:{size:13}}}),{responsive:true});

// 4 intents
const I=D.intent;
Plotly.newPlot('intents',[
 {x:I.t,y:I.edge_c,mode:'markers',name:'maker intents',text:I.side,
  marker:{color:I.side.map(s=>s==='bid'?C.bid:C.ask),size:7}},
 {x:D.taker.t,y:D.taker.px.map(p=>null),mode:'markers',name:'',showlegend:false},
 {x:D.taker.t,y:Array(D.taker.t.length).fill(0),mode:'markers',name:'taker exits (unpaired age)',
  marker:{color:C.pause,size:9,symbol:'triangle-down'}}],
 lay({title:{text:'Edge at placement (¢) — green YES / red NO; orange = forced taker exits',font:{size:13}}}),{responsive:true});

// 5 fills + markout
const F=D.fill;
Plotly.newPlot('fills',[
 {x:F.t,y:F.px_dollars.map(p=>100*p),mode:'markers',name:'fill px ¢',text:F.side,
  marker:{color:F.side.map(s=>s==='bid'?C.bid:C.ask),size:8}}],
 lay({title:{text:'Fills (¢) — green bid / red ask',font:{size:13}}}),{responsive:true});
const M=D.markout;
Plotly.newPlot('markout',[
 {x:M.t,y:M.m10,mode:'markers',name:'10s',marker:{color:C.ss,size:6}},
 {x:M.t,y:M.m30,mode:'markers',name:'30s',marker:{color:C.fair,size:6}},
 {x:M.t,y:M.m60,mode:'markers',name:'60s',marker:{color:C.sl,size:6}},
 {x:M.t,y:M.t.map(()=>0),mode:'lines',showlegend:false,line:{color:'#444',width:1}}],
 lay({title:{text:'Fill markout: signed fair-value move after fill (¢); mean 10/30/60s = '
   +[S.markout_mean_c['10'],S.markout_mean_c['30'],S.markout_mean_c['60']].join(' / '),font:{size:13}}}),{responsive:true});

// 6 risk
const H=D.health;
Plotly.newPlot('risk',[
 {x:H.t,y:H.realized,name:'realized $',line:{color:C.bid,width:1.5},yaxis:'y'},
 {x:H.t,y:H.open_cost,name:'open cost $',line:{color:C.mid,width:1},yaxis:'y'},
 {x:H.t,y:H.exposure,name:'exposure (ct)',line:{color:C.ask,width:1},yaxis:'y2'},
 {x:H.t,y:H.orders,name:'live orders',line:{color:C.rti,width:1,dash:'dot'},yaxis:'y2'},
 {x:Q.t,y:Q.net,name:'net inventory',line:{color:C.sl,width:1},yaxis:'y2'}],
 lay({title:{text:'Realized PnL / open cost ($, left) · exposure, orders, net (right)',font:{size:13}},
  yaxis2:{overlaying:'y',side:'right',gridcolor:'#232b36'}}),{responsive:true});

// 7 pair economics
const P=D.pair;
Plotly.newPlot('pair',[
 {x:P.t,y:P.cycle_pnl,type:'bar',name:'cycle PnL $',marker:{color:P.cycle_pnl.map(v=>v>=0?C.bid:C.ask)}},
 {x:P.t,y:P.realized,name:'engine cumulative realized $',line:{color:C.fair,width:1.5},yaxis:'y2'},
 {x:P.t,y:P.pair_wait_s,mode:'markers',name:'wait s',marker:{color:C.pause,size:5},yaxis:'y2'}],
 lay({title:{text:'Pair cycles: per-cycle locked PnL, cumulative, wait time',font:{size:13}},
  yaxis2:{overlaying:'y',side:'right',gridcolor:'#232b36'}}),{responsive:true});

// 8 microstructure
const [ ,Yq]=perMarket(Q.t,Q.mt,Q.y_queue_ahead); const [ ,Nq]=perMarket(Q.t,Q.mt,Q.n_queue_ahead);
const [ ,Yf]=perMarket(Q.t,Q.mt,Q.y_flow_60s); const [ ,Nf]=perMarket(Q.t,Q.mt,Q.n_flow_60s);
Plotly.newPlot('micro',[
 {x:Tq,y:Yq,name:'queue ahead YES',line:{color:C.bid,width:1}},
 {x:Tq,y:Nq,name:'queue ahead NO',line:{color:C.ask,width:1}},
 {x:Tq,y:Yf,name:'YES flow 60s',line:{color:C.ss,width:1},yaxis:'y2'},
 {x:Tq,y:Nf,name:'NO flow 60s',line:{color:C.sl,width:1},yaxis:'y2'},
 {x:Tq,y:Q.touch_imbalance,name:'touch imbalance',line:{color:C.mid,width:1},yaxis:'y2'}],
 lay({title:{text:'Queue position (ct, left) · traded flow & imbalance (right)',font:{size:13}},
  yaxis2:{overlaying:'y',side:'right',gridcolor:'#232b36'}}),{responsive:true});

// 9 gates + tte
const G=S.gate_counts, PR=S.pricing_counts;
Plotly.newPlot('gates',[
 {x:Object.keys(G),y:Object.values(G),type:'bar',name:'cycle gate',marker:{color:C.fair}},
 {x:Object.keys(PR),y:Object.values(PR),type:'bar',name:'pricing',marker:{color:C.pause}}],
 lay({title:{text:'Why quoting was allowed/blocked (event counts)',font:{size:13}},barmode:'group'}),{responsive:true});
Plotly.newPlot('tte',[
 {x:Q.tte,type:'histogram',name:'quote evals',marker:{color:C.fair},nbinsx:60},
 {x:D.pause.tte,type:'histogram',name:'sentinel pauses',marker:{color:C.pause},nbinsx:60}],
 lay({title:{text:'Activity vs time-to-expiry (s)',font:{size:13}},barmode:'overlay'}),{responsive:true});
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--out", default="mm_report.html")
    a = ap.parse_args()
    paths = []
    for p in a.logs:
        paths.extend(sorted(glob.glob(p)))
    if not paths:
        sys.exit("no logs matched")
    rows = load(paths)
    d = build(rows)
    html = HTML.replace("__PLOTLY__", PLOTLY_CDN).replace("__DATA__", json.dumps(d))
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {a.out}: {len(rows)} events, {d['summary']['fills']} fills, "
          f"{d['summary']['pair_cycles']} pair cycles")


if __name__ == "__main__":
    main()
