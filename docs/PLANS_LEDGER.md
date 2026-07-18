# PLANS_LEDGER — 计划总账(唯一状态板)

**规则(操作员裁决 2026-07-16):**①任何新计划/提案文档落盘时必须同时在
本账登记一行;②状态变更(批准/开工/完工/作废)当次更新;③旧文档永不
删除,作废标 SUPERSEDED 并注明被谁取代;④工程排序权仍在
`MASTER_SEQUENCE.md`,本账只管"计划文档的生死状态",不管排序。
**看板口径:**ACTIVE=生效中 · PROPOSED=待操作员批 · IN-PROGRESS=执行中 ·
DONE=完工验收 · BLOCKED=受阻 · SUPERSEDED=已被取代。

## 本周活跃(2026-07-15/16 批次)

| 文档 | 状态 | 一句话 |
|---|---|---|
| PROPOSAL_AUTORESEARCH_CONVERGENCE_CLAUSES_2026-07-15.md | **ACTIVE**(deep02 实战验证) | 五条刹车,所有任务书标配 |
| SPORTS_AUTORESEARCH_02_AUTHORIZATION_2026-07-15.md(+EN 版) | **DONE**(deep02 完工) | 探索档研究授权,产出信号地图 |
| plan_audits/SPORTS_AUTORESEARCH_01_CLAIMS_VERIFICATION_2026-07-15.md | DONE | deep01 交接书核验,F-1 已闭 |
| PLAN_LATENCY_BENCH_ORDER_CANCEL_2026-07-16.md | **DONE**(294d7fe) | 延迟实测收官;Tier 4 仍锁 |
| LATENCY_FACTS.md | **ACTIVE** | 延迟数字唯一引用源 |
| PLAN_B9_BATCH_INGEST_2026-07-16.md | PROPOSED | 批量入库 100MB/s + 等价证明 |
| PLAN_AUTOMATION_SWEEP_2026-07-16.md | PROPOSED | 手动点全盘点 + 1-5 项自动化 |
| PLAN_TELEGRAM_MONITOR_2026-07-16.md | PROPOSED(规格已冻结,待 token) | 监控台全规格 |
| PLAN_NEXT_PHASE_DATA_QUEUE_EN_2026-07-16.md(+中文历史草稿) | **PROPOSED**(审计修订 v2,待操作员批准) | 双轨下一阶段队列;英文版为权威版本 |
| PLAN_W_PUB_REF_01_ZERO_COPY_2026-07-16.md | **PROPOSED**(待独立审计+操作员批准) | S3 数据面零复制;含 sealed RFQ opt-in;拆分 01A/01B/01C |
| BACKTEST_SYSTEM_CONCEPT_2026-07-16.md | ACTIVE(概念) | 1:1 回测哲学 + 缺件清单 |
| RISK_INDICATOR_FRAMEWORK_2026-07-16.md | ACTIVE(框架) | 19 指标仪表盘,阈值待校准 |
| plan_audits/SPORTS_AUTORESEARCH_01_W_SAR_RFQ_REPLAY_01_DEBT_2026-07-15.md | BLOCKED(未授权) | RFQ 回放债,重开需操作员令 |
| AGENT_OPERATING_MODEL_2026-07-17.md | **WITHDRAWN**(操作员 07-17 撤回) | 作业机制草案,不生效仅存档;既有守则/五条刹车/审计实践不受影响 |

## 进行中的 W(执行态)

| W | 状态 | 备注 |
|---|---|---|
| 07-14 修复(导出+封存) | IN-PROGRESS | 双流并行重灌,预计今晚收 |
| W-TELEGRAM-01 | 待发射(等 bot token) | 执行令已拟好 |
| W-HYGIENE-01 / W-AUTO-01 / W-B9-BATCH-01 | 待发射/待批 | 顺序:HYGIENE→AUTO→B9 |

## 历史计划(deep01 之前)

盘点未完成——**归入 W-HYGIENE-01 的 CODEBASE_MAP 任务**:老计划逐份
定状态(多数应为 DONE/SUPERSEDED,如 EXECUTION_PLAN 队列已于
2026-07-07 完结,见 SESSION_LOG),补录到本账"历史"节。
