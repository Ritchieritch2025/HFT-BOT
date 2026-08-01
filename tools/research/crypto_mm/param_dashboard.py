#!/usr/bin/env python3
"""param_dashboard.py — 参数户口本可视化(登记值 vs 滚动重估,漂移报警)。

数据契约(agent 的每周/每日重估作业往这个文件追加,一行一条):
    metrics/params_history.ndjson
    {"date":"2026-07-27","param":"GAMMA_C","registered":0.5,
     "estimated":0.83,"lo":0.61,"hi":1.02,"unit":"c/ct",
     "estimator":"glft_delta_fit","n":1441,"tier":"stat"}
字段:registered=当前代码/env 在用的值;estimated=最新数据重估值;
lo/hi=重估的95%区间;tier: struct|governance|stat。
漂移判定:registered 落在 [lo,hi] 外 → 该参数亮红。

用法: python3 param_dashboard.py --history metrics/params_history.ndjson \
          --out "Strat Reports/PARAM_REGISTRY.html"
输出单文件 HTML(Plotly CDN):每参数一张小图——登记值横线 vs 重估带,
外加汇总红绿表。多轮测试的每一轮 = 图上多一个点,趋势肉眼可见。
"""
import argparse
import collections
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    by_param = collections.defaultdict(list)
    for line in open(a.history, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        by_param[r["param"]].append(r)
    for rows in by_param.values():
        rows.sort(key=lambda r: r["date"])

    cards, plots = [], []
    for i, (name, rows) in enumerate(sorted(by_param.items())):
        last = rows[-1]
        drift = not (last["lo"] <= last["registered"] <= last["hi"])
        cards.append({"name": name, "tier": last.get("tier", "stat"),
                      "registered": last["registered"],
                      "estimated": last["estimated"],
                      "unit": last.get("unit", ""), "n": last.get("n"),
                      "estimator": last.get("estimator", ""),
                      "drift": drift, "runs": len(rows),
                      "date": last["date"]})
        plots.append({
            "div": f"p{i}", "name": name, "drift": drift,
            "unit": last.get("unit", ""),
            "t": [r["date"] for r in rows],
            "est": [r["estimated"] for r in rows],
            "lo": [r["lo"] for r in rows],
            "hi": [r["hi"] for r in rows],
            "reg": [r["registered"] for r in rows]})

    n_red = sum(1 for c in cards if c["drift"])
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>参数户口本</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js"></script>
<style>
body{{background:#0d1117;color:#dde3ea;font-family:-apple-system,Segoe UI,sans-serif;padding:14px 22px}}
h1{{font-size:17px}} .sub{{color:#7d8896;font-size:12px;margin-bottom:12px}}
table{{border-collapse:collapse;font-size:12px;margin-bottom:18px}}
td,th{{border:1px solid #21262d;padding:4px 10px;text-align:right}}
th{{color:#9fb6d0}} td:first-child,th:first-child{{text-align:left}}
.red{{color:#ff7b72;font-weight:600}} .ok{{color:#7ee787}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px}}
.plot{{height:220px;border:1px solid #21262d;border-radius:8px}}
.plot.drift{{border-color:#ff7b72}}
</style></head><body>
<h1>参数户口本 — 登记值 vs 数据重估</h1>
<div class="sub">{len(cards)} 个参数在册 · <span class="{'red' if n_red else 'ok'}">
{n_red} 个漂移报警</span> · 判定:登记值落在最新重估95%区间外=红</div>
<table><tr><th>参数</th><th>级别</th><th>登记值</th><th>最新重估</th>
<th>轮次</th><th>n</th><th>估计器</th><th>状态</th></tr>"""
    for c in sorted(cards, key=lambda x: not x["drift"]):
        st = '<span class="red">漂移</span>' if c["drift"] else '<span class="ok">在带内</span>'
        html += (f"<tr><td>{c['name']} <small>({c['unit']})</small></td>"
                 f"<td>{c['tier']}</td><td>{c['registered']}</td>"
                 f"<td>{c['estimated']}</td><td>{c['runs']}</td>"
                 f"<td>{c['n'] or '—'}</td><td>{c['estimator']}</td>"
                 f"<td>{st}</td></tr>")
    html += '</table><div class="grid">'
    for p in plots:
        html += f'<div id="{p["div"]}" class="plot{" drift" if p["drift"] else ""}"></div>'
    html += "</div><script>\n"
    html += ("const L={paper_bgcolor:'#0d1117',plot_bgcolor:'#11161d',"
             "font:{color:'#dde3ea',size:10},margin:{l:45,r:12,t:30,b:28},"
             "xaxis:{gridcolor:'#1c2430'},yaxis:{gridcolor:'#1c2430'},"
             "showlegend:false};\n")
    for p in plots:
        d = json.dumps
        html += f"""Plotly.newPlot('{p["div"]}',[
 {{x:{d(p["t"] + p["t"][::-1])},y:{d(p["hi"] + p["lo"][::-1])},fill:'toself',
   fillcolor:'rgba(88,166,255,0.15)',line:{{width:0}},hoverinfo:'skip'}},
 {{x:{d(p["t"])},y:{d(p["est"])},name:'重估',line:{{color:'#58a6ff',width:1.5}}}},
 {{x:{d(p["t"])},y:{d(p["reg"])},name:'登记值',
   line:{{color:'{"#ff7b72" if p["drift"] else "#7ee787"}',width:1.5,dash:'dash'}}}}],
 Object.assign(JSON.parse(JSON.stringify(L)),{{title:{{text:'{p["name"]} ({p["unit"]})'
   + ({str(p["drift"]).lower()} ? '  ⚠ 漂移' : ''),font:{{size:11}}}}}}));\n"""
    html += "</script></body></html>"
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {a.out}: {len(cards)} params, {n_red} drift alarms")


if __name__ == "__main__":
    main()
