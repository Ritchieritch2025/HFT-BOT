# ARCHIVE — 全部产出归档清单(供逐项审阅筛选)

更新:2026-07-20。位置都在 `sandbox/expt_bo2026/` 下,除非另注。
标记:⭐=筛选策略时必看 · 📄=文档 · 📊=数据 · 🔧=代码 · 🌐=网页

## 一、结论与决策文档(先看这层)
- ⭐📄 `MASTER.md` — 总图:四阶段进度、策略清单、未决事项
- ⭐📄 `CONCLUSIONS.md` — 实验结论 H1–H8 + P1 关账(每条假设的判定和数字)
- ⭐📄 `STRATEGY_SPECS.md` — 每个策略的公式/参数/常数出处
- 📄 `DESIGN.md` — 实验设计(假设、通过标准、局限)
- 📄 `RESULTS_V2.md` — 11 天全量结果表(A–K 共 11 张表)
- 📄 `PLAN_BO_LIVE.md` — 论文线上线计划与验收标准
- 📄 `REQUIREMENTS.md` — 数据/权限/预算清单(当时的决策记录)
- 📄 `watchtower/README.md` — 监控框架说明

## 二、网页(可视化)
- ⭐🌐 研究报告:https://claude.ai/code/artifact/72f70c7c-a389-489a-a985-a1d33ec4ea49
- 🌐 微观结构终端 v0:https://claude.ai/code/artifact/e840dba7-5c4a-4e3a-9d29-fe7ee8a41b6d

## 三、案例库(研究素材)
- ⭐📄 `EVENT_STUDY_CARNEY_AI.md` — Carney-AI 逐分钟解剖(VPIN 盲区的发现现场)
- ⭐📊 `cases.ndjson` — casebot 自动记录的论文现象(含 Mamdani 七连击全程)
- 📊 `whale_alerts.ndjson` — 全市场大单/爆量原始告警流(持续增长)
- 📊 `albums/日期/` — 告警市场的量价图相册(明早起每日生成)

## 四、账本(策略成色的最终证据,每日更新)
- ⭐📊 `bo_shadow_fills.ndjson` / `bo_shadow_ledger.csv` — 论文摆法 BO-1 逐笔+结算
- ⭐📊 `rc4_shadow_fills.ndjson` / `rc4_shadow_ledger.csv` — 改良摆法 RC-4
- ⭐📊 `t100_fills.ndjson` — 百元快转 T100(含 settle 回款记录)
- 📊 `bo_state.json` — 健康仪表:日度 gap + 频率/幅度分解 + 校准表
- 📊 `bo_vpin_alerts.ndjson` — 撤退线开火记录

## 五、数据资产
- 📊 `expt.duckdb` — 11 天 8,860 万笔成交 + 15.3 万市场结算 + λ/GH 度量表(可查询)
- 📊 `ec2_warehouse/` — EC2 原始日更数据(csv.gz,md5 清单)+ dim(含结算条款原文)
- 📊 `universe.json` / `blocklist.json` / `series_categories.json` — 宇宙/风险/分类

## 六、代码(全部可复现)
- 🔧 `watchtower/` — 监控+影子框架(feed/state/detectors/shadow{bo1,t100}/casebot/daily/render)
- 🔧 `merge_trades.py` → `fetch_settlements_v2.py` → `analyze_v2.py` — 数据管线三部曲
- 🔧 `bo_gauges.py` / `bo_universe.py` / `bo_daily_update.py` / `bo_daily.sh` — 日更链
- 🔧 `attic/` — 被框架替代的旧脚本(bo_live/whale_watch,留档)

## 七、写进主仓库/记忆的
- 📄 `docs/MM_DOCTRINE_FROM_LITERATURE.md`(主仓库)— 文献提炼 §1b 含复现状态、P5 降级注记
- 记忆:实验结论 / 策略分线原则 / 大白话偏好(跨会话保留)

## 审阅建议顺序
MASTER → CONCLUSIONS(H1-H8)→ 三本账本 → STRATEGY_SPECS → cases/事件解剖 → 报告网页
