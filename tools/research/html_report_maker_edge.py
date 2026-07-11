#!/usr/bin/env python3
"""Interactive HTML report for the maker-edge pilot (PLAN_RESEARCH_CYCLE_1 S1).

Renders results.json (built by aggregate_maker_edge.py) into ONE self-contained
offline HTML: the vendored ECharts lib is inlined (no CDN, no network), the
PRE-REGISTRATION section appears BEFORE any result, the NON-GATE fee banner +
dev-grade/regime labels + fingerprint header are mandatory, and the seven S1
charts are interactive (hover = value + n + CI, brush zoom, legend toggle,
filter by tour / price band / tick stratum / phase).

Honest-chart five rules (plan viz spec): one-chart-one-question titles;
bootstrap CI on chart; zero line + no unmarked y truncation on markout/edge
charts; no dual y-axis; log horizon axis; all money in ¢.
"""
import html as _html
import json
import os

# dataviz reference palette (validated categorical order, light surface)
PAL = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948",
       "#e87ba4", "#eb6834"]

CSS = """
:root { --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e;
        --muted:#898781; --grid:#e1e0d9; --border:rgba(11,11,11,.10);
        --warn-bg:#fff3e0; --warn-bd:#eb6834; }
* { box-sizing: border-box; }
body { margin:0; background:var(--page); color:var(--ink);
       font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 16px; }
.badges span { display:inline-block; padding:3px 10px; border-radius:12px;
       font-size:12px; font-weight:600; margin-right:8px; }
.b-grade { background:#e8eef9; color:#1c5cab; }
.b-regime { background:#e6f4ec; color:#006300; }
.banner { background:var(--warn-bg); border:1px solid var(--warn-bd);
       border-radius:8px; padding:10px 14px; margin:12px 0; font-size:13px;
       font-weight:600; color:#7a3413; }
.card { background:var(--surface); border:1px solid var(--border);
       border-radius:10px; padding:16px 18px; margin:16px 0; }
.card h2 { margin:0 0 2px; font-size:16px; }
.card .scope { color:var(--ink2); font-size:12px; margin:0 0 8px; }
.chart { width:100%; height:430px; }
.chart-sm { width:100%; height:340px; }
.controls { margin:6px 0 10px; font-size:12px; color:var(--ink2); }
.controls label { margin-right:14px; }
.controls select { font-size:12px; padding:2px 4px; }
.note { color:var(--muted); font-size:11.5px; margin-top:6px; }
table.t { border-collapse:collapse; width:100%; font-size:12.5px; }
table.t th, table.t td { border-bottom:1px solid var(--grid); padding:5px 8px;
       text-align:right; font-variant-numeric: tabular-nums; }
table.t th { color:var(--ink2); font-weight:600; }
table.t td:first-child, table.t th:first-child { text-align:left; }
.collect { color:#b25000; font-weight:700; }
.okn { color:#006300; font-weight:700; }
pre.prereg { white-space:pre-wrap; background:#f4f3ef; border:1px solid
       var(--grid); border-radius:8px; padding:12px; font-size:12px;
       max-height:420px; overflow:auto; }
details.fp { font-size:12px; color:var(--ink2); }
details.fp pre { white-space:pre-wrap; font-size:11px; background:#f4f3ef;
       padding:8px; border-radius:6px; overflow:auto; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
@media (max-width: 900px) { .grid2 { grid-template-columns:1fr; } }
footer { color:var(--muted); font-size:11px; margin:24px 0; }
h1 { font-size:20px; margin:8px 0 4px; }
"""


def _fmt(x, d=3):
    return ("%%.%df" % d) % x if x is not None else "–"


def _fingerprint_html(fp):
    man = "\n".join("  %s  md5=%s" % (k, v)
                    for k, v in sorted(fp.get("manifest_md5", {}).items()))
    body = ("code SHA: %s\ngenerated: %s\ncommand: %s\nregime: %s\n%s\n"
            "data manifest:\n%s"
            % (fp.get("code_sha"), fp.get("generated_at"), fp.get("command"),
               fp.get("regime"), fp.get("non_gate"), man))
    return ("<details class='fp' open><summary><b>指纹头 fingerprint"
            "(SHA + manifest md5 + 命令行)</b></summary><pre>%s</pre>"
            "</details>" % _html.escape(body))


def _layer_table(r):
    rows = []
    for L in r["layers"]:
        ci = L["ci95"]
        ci_txt = ("[%s, %s]" % (_fmt(ci[0]), _fmt(ci[1]))) \
            if ci and ci[0] is not None else "n/a"
        if L["auto_collect_n_lt_min"]:
            verdict = ("<span class='collect'>n&lt;%d → 自动 collect,"
                       "主指标不解读</span>" % r["min_n"])
        else:
            verdict = "<span class='okn'>n≥%d,可解读</span>" % r["min_n"]
        rows.append(
            "<tr><td>%s / %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (L["maker_fee_class"], L["tour_level"],
               format(L["n_pessimistic"], ","), format(L["n_matches"], ","),
               _fmt(L["half_spread_c"]), _fmt(L["drift_30_c"]),
               _fmt(L["maker_fee_c"]), _fmt(L["net_edge_c"]), ci_txt, verdict,
               "是" if L["h1_fee_wall_applicable"] else "否(费用列仅供参考)"))
    return """
<table class='t'>
<tr><th>层(费类/巡回级)</th><th>n 悲观成交</th><th>n 场</th>
<th>半价差 ¢</th><th>30s markout ¢</th><th>maker费 ¢(预览)</th>
<th>净 edge ¢/张</th><th>95%% CI(按场 bootstrap≥1000)</th>
<th>样本判读</th><th>H1 适用</th></tr>%s</table>""" % "".join(rows)


def _dq_tables(r):
    dq = r["dq"]
    cls = "".join("<tr><td>%s</td><td>%s</td></tr>" % (k, format(v, ","))
                  for k, v in dq["classes"].items())
    fill = "".join("<tr><td>%s</td><td>%s</td></tr>" % (k, format(v, ","))
                   for k, v in dq["fill_class"].items())
    st = dq["mid_staleness_s"]
    burst = r["exploratory"].get("burstiness", {})
    brows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (ph, format(b["n"], ","), _fmt(b["p50_s"], 2), _fmt(b["p90_s"], 2),
           _fmt(b["p99_s"], 2)) for ph, b in burst.items())
    return """
<div class='grid2'>
<div><h3 style='font-size:13px'>剔除计数(一笔不漏)</h3>
<table class='t'><tr><th>dq_class</th><th>n</th></tr>%s</table>
<h3 style='font-size:13px'>成交归类(join-the-touch 双口径)</h3>
<table class='t'><tr><th>fill_class</th><th>n</th></tr>%s</table></div>
<div><h3 style='font-size:13px'>mid 陈旧度(dq=ok,秒)</h3>
<table class='t'><tr><th>p50</th><th>p90</th><th>p99</th><th>max</th></tr>
<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr></table>
<h3 style='font-size:13px'>成交间隔 trade-gap(Tennis,秒)</h3>
<p class='note'><b>ERRATUM 2026-07-10:</b>此表为同一市场相邻两笔<b>成交</b>
的间隔(trade-gap, not book-update),数据源是成交行;真正的盘口更新间隔
(book-level heartbeat)需 L1 切片,留给 S4。主指标不受影响。</p>
<table class='t'><tr><th>phase</th><th>n</th><th>p50</th><th>p90</th>
<th>p99</th></tr>%s</table></div></div>""" % (
        cls, fill, _fmt(st["p50"], 2), _fmt(st["p90"], 2), _fmt(st["p99"], 2),
        _fmt(st["max"], 2), brows)


JS = r"""
'use strict';
const R = __DATA__;
const PAL = __PAL__;
const HORIZONS = [1, 10, 30, 120];
const BANDS = ['1-10c','10-30c','30-50c','50-70c','70-90c','90-99c'];
const TOUR_ORDER = ['ALL','ITF','Challenger','ATP','WTA','_unsegmented'];
const CURVE = R.exploratory.markout_curve, DEC = R.exploratory.decomposition;
const tours = TOUR_ORDER.filter(t => CURVE.some(d => d.tour === t));
const tourColor = {}; TOUR_ORDER.slice(1).forEach((t,i)=>tourColor[t]=PAL[i%PAL.length]);
const CHARTS = [];
function el(id){ return document.getElementById(id); }
function mk(id){ const c = echarts.init(el(id)); CHARTS.push(c); return c; }
function fmt(x,d){ return (x===null||x===undefined)?'–':(+x).toFixed(d===undefined?3:d); }
function fillSel(id, opts, labels){
  const s = el(id); s.innerHTML = '';
  opts.forEach((o,i)=>{ const e=document.createElement('option');
    e.value=o; e.textContent=labels?labels[i]:o; s.appendChild(e); });
}
const PHASES = ['pre_match','in_play'], PHASE_LBL = ['赛前 pre_match','开打 in_play'];
const BASE = {
  textStyle:{fontFamily:'system-ui,-apple-system,"Segoe UI",sans-serif'},
  toolbox:{feature:{dataZoom:{yAxisIndex:'none'},restore:{},saveAsImage:{}},right:8},
  grid:{left:56,right:24,top:52,bottom:46,containLabel:false}
};
function axis(name, extra){
  return Object.assign({name:name, nameTextStyle:{color:'#52514e'},
    axisLine:{lineStyle:{color:'#c3c2b7'}}, axisLabel:{color:'#898781'},
    splitLine:{lineStyle:{color:'#e1e0d9'}}}, extra||{});
}
function zeroFloor(v){ return Math.min(0, Math.floor(v.min)); }
const ZLINE = {silent:true,symbol:'none',label:{show:false},
  lineStyle:{color:'#52514e',type:'solid',width:1},data:[{yAxis:0}]};
function errbar(data, color, name){
  // data: [xIndexOrValue, lo, hi][]
  return {type:'custom', name:name||'95% CI', color:color||'#0b0b0b', z:10,
    renderItem:(p,api)=>{
      const lo=api.coord([api.value(0),api.value(1)]);
      const hi=api.coord([api.value(0),api.value(2)]);
      const w=7, st={stroke:color||'#0b0b0b',lineWidth:1.5};
      return {type:'group',children:[
        {type:'line',shape:{x1:lo[0],y1:lo[1],x2:hi[0],y2:hi[1]},style:st},
        {type:'line',shape:{x1:lo[0]-w,y1:lo[1],x2:lo[0]+w,y2:lo[1]},style:st},
        {type:'line',shape:{x1:hi[0]-w,y1:hi[1],x2:hi[0]+w,y2:hi[1]},style:st}]};
    },
    encode:{x:0,y:[1,2]}, data:data, tooltip:{show:false}};
}

/* ── chart 1: toxicity curve ─────────────────────────────────────────── */
const C1 = mk('c1');
fillSel('c1tour', tours); fillSel('c1phase', PHASES, PHASE_LBL);
function chart1(){
  const tour=el('c1tour').value, phase=el('c1phase').value;
  const rows=CURVE.filter(d=>d.tour===tour&&d.phase===phase);
  const series=[]; const legend=[];
  BANDS.forEach((b,i)=>{
    const pts=rows.filter(d=>d.band===b).sort((a,c)=>a.h-c.h);
    if(!pts.length) return;
    const color=PAL[i%PAL.length]; legend.push(b);
    const ebd=pts.filter(p=>p.lo!==null).map(p=>[p.h,p.lo,p.hi]);
    if(ebd.length) series.push(errbar(ebd, color, b));
    series.push({name:b,type:'line',color:color,symbolSize:7,z:3,
      lineWidth:2,
      data:pts.map(p=>({value:[p.h,p.mean],n:p.n,lo:p.lo,hi:p.hi}))});
  });
  if(series.length) series[series.length-1].markLine = ZLINE;
  C1.setOption(Object.assign({},BASE,{
    legend:{data:legend,top:24},
    tooltip:{trigger:'item',formatter:pr=>{
      const d=pr.data; if(!d||d.n===undefined) return '';
      return pr.seriesName+' · h='+d.value[0]+'s<br>markout = <b>'+
        fmt(d.value[1])+'¢</b><br>95% CI ['+fmt(d.lo)+', '+fmt(d.hi)+
        ']<br>n = '+d.n.toLocaleString();}},
    xAxis:axis('horizon(秒,对数轴)',{type:'log',min:0.8,max:150}),
    yAxis:axis('markout ¢',{type:'value',min:zeroFloor}),
    series:series}),true);
}
el('c1tour').onchange=chart1; el('c1phase').onchange=chart1; chart1();

/* ── chart 2: bounce/drift decomposition ─────────────────────────────── */
const C2 = mk('c2');
fillSel('c2tour', tours); fillSel('c2phase', PHASES, PHASE_LBL);
function chart2(){
  const tour=el('c2tour').value, phase=el('c2phase').value;
  const rows=HORIZONS.map(h=>DEC.find(d=>d.tour===tour&&d.phase===phase&&d.h===h))
                     .filter(Boolean);
  const cats=rows.map(d=>d.h+'s');
  C2.setOption(Object.assign({},BASE,{
    legend:{data:['bounce(价差弹跳)','drift(真信息/毒性)','markout 总计'],top:24},
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:prs=>{
      const i=prs[0].dataIndex, d=rows[i];
      return 'h='+d.h+'s · n='+d.n.toLocaleString()+
        '<br>bounce = '+fmt(d.bounce)+'¢ CI['+fmt(d.bounce_lo)+', '+fmt(d.bounce_hi)+']'+
        '<br>drift = '+fmt(d.drift)+'¢ CI['+fmt(d.drift_lo)+', '+fmt(d.drift_hi)+']'+
        '<br><b>markout = '+fmt(d.markout)+'¢</b> CI['+fmt(d.markout_lo)+', '+
        fmt(d.markout_hi)+']<br>恒等式残差 = '+d.identity_gap.toExponential(2)+'¢';}},
    xAxis:axis('horizon(柱,1/10/30/120s)',{type:'category',data:cats,
      splitLine:{show:false}}),
    yAxis:axis('¢/张',{type:'value',min:zeroFloor}),
    series:[
      {name:'bounce(价差弹跳)',type:'bar',stack:'d',color:PAL[0],barWidth:46,
       data:rows.map(d=>d.bounce), markLine:ZLINE},
      {name:'drift(真信息/毒性)',type:'bar',stack:'d',color:PAL[5],
       data:rows.map(d=>d.drift)},
      {name:'markout 总计',type:'scatter',color:'#0b0b0b',symbolSize:9,
       data:rows.map(d=>d.markout)},
      errbar(rows.map((d,i)=>[i,d.markout_lo,d.markout_hi]))
    ]}),true);
}
el('c2tour').onchange=chart2; el('c2phase').onchange=chart2; chart2();

/* ── chart 3: pre-match spread histogram ─────────────────────────────── */
const C3 = mk('c3');
{
  const H=R.exploratory.spread_hist;
  const toursH=TOUR_ORDER.slice(1).filter(t=>H[t]);
  const maxS=Math.max.apply(null,[].concat.apply([],
    toursH.map(t=>H[t].map(d=>d.spread_c))));
  const cats=[]; for(let s2=1;s2<=maxS;s2++) cats.push(s2);
  const series=toursH.map((t,i)=>({name:t,type:'bar',color:tourColor[t],
    barGap:'10%',
    data:cats.map(sc=>{const e=(H[t]||[]).find(d=>d.spread_c===sc);
      return e?e.n:0;})}));
  C3.setOption(Object.assign({},BASE,{
    legend:{data:toursH,top:24},
    dataZoom:[{type:'inside'},{type:'slider',height:18,bottom:6}],
    grid:Object.assign({},BASE.grid,{bottom:64}),
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:prs=>
      'spread = '+prs[0].axisValue+'¢<br>'+prs.map(p=>p.seriesName+': n='+
      (+p.value).toLocaleString()).join('<br>')},
    xAxis:axis('赛前点差 ¢(tick 层:全部 1¢)',{type:'category',data:cats,
      splitLine:{show:false}}),
    yAxis:axis('笔数 n',{type:'value'}),
    series:series}),true);
}

/* ── chart 4: primary-metric waterfall per layer (verdict chart) ─────── */
const C4 = mk('c4');
const LAYERS=R.layers;
fillSel('c4layer', LAYERS.map((L,i)=>String(i)),
  LAYERS.map(L=>L.maker_fee_class+' / '+L.tour_level+'(n='+
    L.n_pessimistic.toLocaleString()+')'));
function chart4(){
  const L=LAYERS[+el('c4layer').value];
  const hs=L.half_spread_c, dr=L.drift_30_c, fee=L.maker_fee_c,
        net=L.net_edge_c, ci=L.ci95;
  const cats=['半价差收入','− 30s markout','− maker费(NON-GATE预览)','净 edge/张'];
  const help=[0, Math.min(hs,hs-dr), Math.min(hs-dr,hs-dr-fee), 0];
  const vals=[hs, Math.abs(dr), Math.abs(fee), net];
  const cols=[PAL[0], dr>=0?PAL[5]:PAL[3], PAL[7], net>=0?PAL[3]:PAL[5]];
  const badge=L.auto_collect_n_lt_min?
    '⚠ n='+L.n_pessimistic.toLocaleString()+' < '+R.min_n+' → 自动 collect,本层主指标不解读':
    'n='+L.n_pessimistic.toLocaleString()+' ≥ '+R.min_n+'('+
    L.n_matches.toLocaleString()+' 场)';
  el('c4badge').innerHTML=badge+(L.h1_fee_wall_applicable?
    ' · H1 费率墙检验在本层成立(零费)':' · 收费层:费用列仅供参考,H1 不在此层检验');
  el('c4badge').className=L.auto_collect_n_lt_min?'note collect':'note okn';
  C4.setOption(Object.assign({},BASE,{
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:prs=>{
      const i=prs[0].dataIndex;
      const v=[hs,-dr,-fee,net][i];
      let s2=cats[i]+' = <b>'+fmt(v)+'¢</b><br>n = '+
        L.n_pessimistic.toLocaleString()+'(悲观成交,'+
        L.n_matches.toLocaleString()+' 场)';
      if(i===3&&ci&&ci[0]!==null)
        s2+='<br>95% CI ['+fmt(ci[0])+', '+fmt(ci[1])+'](按场 bootstrap)';
      return s2;}},
    xAxis:axis('',{type:'category',data:cats,splitLine:{show:false},
      axisLabel:{color:'#52514e',interval:0}}),
    yAxis:axis('¢/张',{type:'value',min:zeroFloor}),
    series:[
      {type:'bar',stack:'w',itemStyle:{color:'transparent'},silent:true,
       tooltip:{show:false},data:help},
      {type:'bar',stack:'w',barWidth:64,data:vals.map((v,i)=>({value:v,
        itemStyle:{color:cols[i]}})), markLine:ZLINE},
      (ci&&ci[0]!==null)?errbar([[3,ci[0],ci[1]]]):null
    ].filter(Boolean)}),true);
}
el('c4layer').onchange=chart4; chart4();

/* ── chart 5: time-of-day heatmap ────────────────────────────────────── */
const C5 = mk('c5');
fillSel('c5metric',['vol','spread_c','n'],['成交量(张)','平均点差 ¢','笔数 n']);
fillSel('c5phase', PHASES, PHASE_LBL);
const DOWL=['日','一','二','三','四','五','六'];
function chart5(){
  const met=el('c5metric').value, phase=el('c5phase').value;
  const rows=R.exploratory.time_of_day.filter(d=>d.phase===phase);
  const data=rows.map(d=>({value:[d.hr,d.dow,
    met==='vol'?Math.round(d.vol):met==='n'?d.n:+d.spread_c.toFixed(3)],
    n:d.n}));
  const vals=data.map(d=>d.value[2]);
  C5.setOption(Object.assign({},BASE,{
    tooltip:{formatter:pr=>'周'+DOWL[pr.data.value[1]]+' '+pr.data.value[0]+
      ':00 UTC<br>'+el('c5metric').selectedOptions[0].text+' = <b>'+
      (+pr.data.value[2]).toLocaleString()+'</b><br>n = '+
      pr.data.n.toLocaleString()},
    grid:Object.assign({},BASE.grid,{bottom:70}),
    visualMap:{min:Math.min.apply(null,vals),max:Math.max.apply(null,vals),
      calculable:true,orient:'horizontal',left:'center',bottom:0,
      inRange:{color:['#cde2fb','#3987e5','#0d366b']}},
    xAxis:axis('UTC 小时',{type:'category',
      data:Array.from({length:24},(_,i)=>i),splitLine:{show:false}}),
    yAxis:axis('星期',{type:'category',data:DOWL,splitLine:{show:false}}),
    series:[{type:'heatmap',data:data,
      itemStyle:{borderColor:'#fcfcfb',borderWidth:2}}]}),true);
}
el('c5metric').onchange=chart5; el('c5phase').onchange=chart5; chart5();

/* ── chart 6: per-match scatter ──────────────────────────────────────── */
const C6 = mk('c6');
{
  const S=R.exploratory.per_match_scatter;
  const toursS=TOUR_ORDER.slice(1).filter(t=>S.some(d=>d.tour===t));
  const mx=Math.max.apply(null,S.map(d=>d.half_spread))*1.05;
  const series=toursS.map(t=>({name:t,type:'scatter',color:tourColor[t],
    symbolSize:8,
    data:S.filter(d=>d.tour===t).map(d=>({value:[d.half_spread,d.drift],
      event:d.event,n:d.n,fee:d.fee,symbol:d.fee==='charged'?'triangle':'circle'}))}));
  series[0].markLine={silent:true,symbol:'none',
    lineStyle:{color:'#52514e',width:1},
    data:[{yAxis:0,label:{show:false},lineStyle:{type:'solid'}},
          [{coord:[0,0],label:{formatter:'打平线 markout=半价差(零费)',
            position:'end',color:'#898781',fontSize:11}},
           {coord:[mx,mx]}]]};
  C6.setOption(Object.assign({},BASE,{
    legend:{data:toursS,top:24},
    dataZoom:[{type:'inside'},{type:'inside',yAxisIndex:0}],
    tooltip:{formatter:pr=>{const d=pr.data;
      return d.event+'('+pr.seriesName+', '+d.fee+')<br>半价差 = '+
        fmt(d.value[0])+'¢ · 30s markout = '+fmt(d.value[1])+
        '¢<br>n = '+d.n.toLocaleString()+' 笔悲观成交(≥5 才入图)';}},
    xAxis:axis('该场 vwap 半价差 ¢',{type:'value'}),
    yAxis:axis('该场 vwap 30s markout ¢',{type:'value',min:zeroFloor}),
    series:series}),true);
}

/* ── chart 7: DQ two panels ──────────────────────────────────────────── */
const C7a = mk('c7a'), C7b = mk('c7b');
{
  const cls=Object.entries(R.dq.classes);
  C7a.setOption(Object.assign({},BASE,{
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:prs=>
      prs[0].axisValue+'<br>n = '+(+prs[0].value).toLocaleString()},
    xAxis:axis('',{type:'category',data:cls.map(c=>c[0]),
      splitLine:{show:false},axisLabel:{color:'#52514e',interval:0,rotate:12}}),
    yAxis:axis('n(对数轴)',{type:'log'}),
    series:[{type:'bar',color:PAL[0],barWidth:40,data:cls.map(c=>c[1]),
      label:{show:true,position:'top',color:'#52514e',
        formatter:pr=>(+pr.value).toLocaleString()}}]}),true);
  const st=R.dq.staleness_hist;
  C7b.setOption(Object.assign({},BASE,{
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:prs=>
      'mid 陈旧度 '+prs[0].axisValue+'<br>n = '+(+prs[0].value).toLocaleString()},
    xAxis:axis('mid 陈旧度(dq=ok)',{type:'category',
      data:st.map(d=>d.bin),splitLine:{show:false},
      axisLabel:{color:'#52514e',interval:0,rotate:12}}),
    yAxis:axis('n(对数轴)',{type:'log'}),
    series:[{type:'bar',color:PAL[1],barWidth:26,
      data:st.map(d=>Math.max(d.n,1))}]}),true);
}

window.addEventListener('resize',()=>CHARTS.forEach(c=>c.resize()));
"""


def write(r, outdir, echarts_path):
    prereg_path = os.path.join(outdir, "PRE_REGISTRATION.md")
    with open(prereg_path) as f:
        prereg = f.read()
    with open(echarts_path) as f:
        echarts_js = f.read().replace("</script", "<\\/script")
    data_js = json.dumps(r, default=str).replace("</", "<\\/")

    fp = r["fingerprint"]
    n_total = sum(L["n_pessimistic"] for L in r["layers"])
    head = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>S1 maker-edge pilot — dev-grade (regime=slam-week)</title>
<style>%s</style></head><body><div class="wrap">
<div class="badges"><span class="b-grade">dev-grade(Mac-era 数据,禁止外推)</span>
<span class="b-regime">regime=slam-week(Wimbledon 2026-07-06..08)</span></div>
<h1>S1 · Maker-edge pilot — 网球赛前做市:价差收入盖得住毒性吗?</h1>
<div class="banner">⚠ %s</div>
%s
<div class="card"><h2>预注册(冻结于任何指标计算之前 — 在结果之前,原文照录)</h2>
<pre class="prereg">%s</pre></div>
""" % (CSS, _html.escape(r["non_gate_banner"]), _fingerprint_html(fp),
       _html.escape(prereg))

    verdict = """
<div class="card"><h2>主指标(判决表)— 每层各算各报,禁止跨层合并</h2>
<p class="scope">预注册口径:成交量加权 半价差 − 30s mid markout − maker费(¢/张),
悲观口径(队尾),赛前时段,val=2026-07-08 只测不选。收费层的 maker 费为
NON-GATE 公式预览,独立一列扣除。恒等式自检 max gap =
%s¢ &lt; %s¢ PASS(纪律 11 机器门)。</p>
%s
<p class="note">S1 结论(合法集 = {methodology-valid+collect, methodology-flawed}):
<b>%s</b> — %s</p></div>
""" % (_fmt(r["machine_gate"]["identity_max_gap_c"], 6),
       _fmt(r["machine_gate"]["identity_tolerance_c"], 2),
       _layer_table(r), _html.escape(r["s1_conclusion"]),
       _html.escape(r["s1_conclusion_basis"]))

    charts = """
<div class="card"><h2>图1 · 被打之后价格往哪走、走多远?(毒性曲线)</h2>
<p class="scope">零费层 · val(07-08)· 悲观成交 · 按价格带分线,CI 带 =
按场 bootstrap≥1000。图例点选显隐价格带。</p>
<div class="controls"><label>巡回级:<select id="c1tour"></select></label>
<label>阶段:<select id="c1phase"></select></label>
<label>tick 层:<select disabled><option>1¢(全部)</option></select></label></div>
<div id="c1" class="chart"></div>
<div class="note">悬停出精确值 + n + CI;工具栏可框选缩放。</div></div>

<div class="card"><h2>图2 · taker 付的是价差弹跳还是真信息?(bounce/drift 分解)</h2>
<p class="scope">零费层 · val · 悲观成交。堆叠柱恒等式:bounce + drift ≡ markout
(纪律 11,残差入悬停)。黑点 = markout 总计,工字线 = 95%% CI。</p>
<div class="controls"><label>巡回级:<select id="c2tour"></select></label>
<label>阶段:<select id="c2phase"></select></label></div>
<div id="c2" class="chart"></div></div>

<div class="card"><h2>图3 · 摆摊的毛利空间长什么样?(赛前点差分布)</h2>
<p class="scope">全部成交(不限成交口径)· 赛前 · 按巡回级分色。本窗口全部
series 均为 1¢ tick,次美分层无数据(分面折叠为单面)。</p>
<div id="c3" class="chart"></div></div>

<div class="card"><h2>图4 · 这门生意每张赚几分?(主指标瀑布 — 判决图)</h2>
<p class="scope">预注册主指标,按层显示(下拉切层,禁止合并)。
半价差 → − 30s markout → − maker费(NON-GATE 预览)→ 净 edge/张,
净值带按场 bootstrap 95%% CI。</p>
<div class="controls"><label>层(费类/巡回级):<select id="c4layer"></select>
</label></div><div id="c4badge" class="note"></div>
<div id="c4" class="chart"></div></div>

<div class="card"><h2>图5 · 散户几点来?(时段热力图)</h2>
<p class="scope">小时(UTC)× 星期。窗口仅 3 天(07-06 周一..07-08 周三),
空行 = 无数据,不外推。</p>
<div class="controls"><label>指标:<select id="c5metric"></select></label>
<label>阶段:<select id="c5phase"></select></label></div>
<div id="c5" class="chart"></div></div>

<div class="card"><h2>图6 · edge 是普遍的还是被几场极端值扛着的?(逐场散点)</h2>
<p class="scope">一点一场(该场 vwap 半价差 vs vwap 30s markout,悲观口径,
赛前,val;n≥5 笔才入图)。对角线下方 = 该场价差收入 &gt; 毒性(零费口径)。
三角 = 收费层场次。滚轮/拖拽缩放。</p>
<div id="c6" class="chart"></div></div>

<div class="card"><h2>图7 · 数据干净吗?(DQ 两小图 + 计数表)</h2>
<p class="scope">左:剔除计数(对数轴,标注精确值);右:mid 陈旧度分布
(dq=ok 内,上限 60s,超限已剔除并计数)。</p>
<div class="grid2"><div id="c7a" class="chart-sm"></div>
<div id="c7b" class="chart-sm"></div></div>
%s</div>
""" % (_dq_tables(r),)

    tail = """
<footer>指纹:code SHA %s · 生成 %s · %s<br>
n(全层悲观成交合计)= %s · 相位探测器 τ(train 冻结)= %s ·
结论合法集 = %s · dev-grade · regime=slam-week</footer>
</div>
<script>%s</script>
<script>%s</script>
</body></html>""" % (
        _html.escape(str(fp.get("code_sha"))),
        _html.escape(str(fp.get("generated_at"))),
        _html.escape(str(fp.get("command"))),
        format(n_total, ","), _fmt(r["phase_tau_train"], 1),
        _html.escape(json.dumps(r["conclusions_allowed"])),
        echarts_js,
        JS.replace("__DATA__", data_js).replace("__PAL__", json.dumps(PAL)))

    out = os.path.join(outdir, "index.html")
    with open(out, "w") as f:
        f.write(head + verdict + charts + tail)
    print("  html: %s (%.1f MB)" % (out, os.path.getsize(out) / 1e6))
