# TECH STACK — 全系统技术栈总览（按数据流每一步）

操作员跟踪文档（2026-07-08 应操作员要求创建）。每层：用了什么、为什么、
在哪。改技术选型时本文件同步更新（E5）。git 为权威，镜像在
`TradingSys Report/docs-mirror/`。

---

## 数据流全链（每一步的技术）

### ① 采集 · CAPTURE（热路径，24/7 生产）
| 部件 | 技术 | 说明 |
|---|---|---|
| 语言 | **C++23**（`-O2 -Wall -Wextra -Wpedantic`） | 热路径全 C++（宪法 E7），Python 永不上采集路径 |
| WS 客户端 | **ixwebsocket**（vendored，C++17 编译） | 连 Kalshi WSS firehose（ticker+trade 全市场）；自身 ping 关闭，Kalshi ping→库自动 pong |
| TLS | **OpenSSL**（vendored） | 也用于 REST 请求签名（KALSHI-ACCESS-SIGNATURE） |
| JSON 解析 | **simdjson**（vendored，ondemand API） | 热路径提取 market_ticker 等，零拷贝 |
| 线程间交接 | **自研无锁环** `include/kalshi/ring.hpp` | Vyukov 有界 MPMC；try_push 永不阻塞，环满=显式丢弃计数 |
| 落盘格式 | **NDJSON 信封** | 每条：recv_mono_ns（单调钟）+ recv_wall_ns（墙钟）+ stream_epoch（重连血统）+ 原始消息全文；小时轮转，raw 保留 3 天 |
| 守护进程 | `apps/ws_shadow.cpp` + `apps/ingestd.cpp` | IO 隔离在控制线程；transport 线程只碰原子量 |
| 看门狗 | W-C1 强制重连（20s 任意帧静默） | 排队升级中：数据帧静默 + 先重订阅阶梯（W-C5 会话） |
| 进程监管 | macOS **launchd** + `tools/pipeline_supervisor.sh`（bash） | Linux 版 systemd 单元已备（deploy/tradingd.service）；迁移后换 systemd + chrony |

### ② 入库 · STAGING
| 部件 | 技术 | 说明 |
|---|---|---|
| 语言 | **Python 3.x — 当前未钉版本**（研究/工具层专用） | ⚠️ 治理缺口（2026-07-08 发现）：无 .python-version/requirements 约束，各机器随缘。W-A1 装机时钉死一个成熟版本（如 3.12.x）+ 测试跑绿为契约；"最新版"非目标，可复现才是 |
| 引擎 | **DuckDB**（staging.duckdb） | 单写者规则（D6）；checkpoint(file, byte_offset) 表保证重启不丢不重 |
| 策略配置 | **YAML**（config/market_classes.yaml 等） | 两类市场策略：Class A 存 L1，Class B 不存 |

### ③ 归档 · ARCHIVE（写一次，永不改）
| 部件 | 技术 | 说明 |
|---|---|---|
| 订单簿 | **Parquet + zstd-15**，hive 分区（category/subcategory/date） | 日终 export_day.py 导出，重读逐文件核对行数 |
| 成交 | **csv.gz** | |
| 账本 | manifest.csv + compression_report.csv | 每日追加 |

### ④ 仓库/研究 · WAREHOUSE & GOLD
| 部件 | 技术 | 说明 |
|---|---|---|
| 查询 | **DuckDB**（内存模式直读 Parquet） | 研究工具从不长期持锁 |
| 数值约定 | **E4 定点**（价格=美元×10⁴ INT32；数量×10⁴ BIGINT） | 21% 成交是 sub-penny、68% size 带小数——禁浮点（D5） |
| 时间约定 | int64 **微秒 UTC**，全链统一 | |
| gold 装载 | Python loader，隔离账本（坏行分类计数+样本留档，D3） | |

### ⑤ 质量检测 · DETECTORS
| 部件 | 技术 | 说明 |
|---|---|---|
| 缺口侦探 | `tools/capture_gaps.py`（日界感知、损坏防御） | 产出 capture_gaps.csv 权威缺口记录 + --live 60s 看门狗（45s 报警） |
| 覆盖审计 | `tools/coverage_audit.py` | V15 深度集缩水检测 |
| 就绪检查 | `tools/lifecycle_check.py` → lifecycle_status.json | dashboard 的 gates 数据源 |
| 事件校验 | `tools/event_validate.py` | 回测前置：缺口日自动标 degraded |

### ⑥ 监控台 · DASHBOARD
| 部件 | 技术 | 说明 |
|---|---|---|
| 生产版 | **Python 标准库 http.server + SSE**（dashboard_server.py，单文件） | 只读、localhost、live_order 类工具永拒（S5） |
| 原型（已批设计） | 原生 JS + 手写 SVG + **uPlot**（vendored）+ SSE | 四区决策驱动 IA；七项契约冻结（W-D1 批准 2026-07-08） |
| 待建采集器 | W-D2..D5（EC2 上建，B1 裁决） | latency/incident/readiness/catalog 四个 Python 采集器 |
| 报告 | **PDF 渲染器**（W-R，排队中） | 每日体检 PDF → `Desktop/TradingSys Report/`；标题+一行裁决+白话简介 |

### ⑦ 测试 · TESTING
| 部件 | 技术 | 说明 |
|---|---|---|
| C++ | make check 自研断言 harness + **ASan/UBSan** 消毒器构建 + fuzz_decode | 红字先行纪律（先证明测试会红） |
| Python | **pytest**（19+ 缺口侦探用例等） | |
| 集成 | tests/run_pipeline.sh + mock 交易所（mock_ws_exchange.py 等） | 真实 401 事故日志已存 fixtures 供重放 |

### ⑧ 流程/治理 · PROCESS
| 部件 | 技术 | 说明 |
|---|---|---|
| 版本 | **git**（分支 plan-live-validation-p0-p3，远端 GitHub） | |
| 治理 | GUARDRAILS 宪法 + W 纪律 + 独立审计 + SESSION_LOG | 每会话退出仪式；docs 镜像到 TradingSys Report |
| 构建 | **CMake + Makefile**（clang，macOS → 迁移后 gcc/clang，Linux） | 依赖全 vendored（third_party/），零外部包管理 |

---

## 排队中/未建（技术选型已定）
- **AWS 迁移**（STEP 1）：EC2（Ubuntu/systemd/chrony）+ **S3**（版本化金库 + 报告回流）
- **回测器**：契约已冻结（PLAN_DASHBOARD_OBSERVATORY §4，E4 定点、悲观界为准） — 引擎未建
- **定价模型**（STEP 6）：**log-odds 空间** Avellaneda-Stoikov/GLFT 骨架 + 自校准参数（Q1/Q4）
- **风控 kill-switch**：独立进程 panic CLI（S3），永不进 dashboard
- **研究交互层**：Streamlit 允许用于 sandbox 研究玩具，永不进操作台

## 刻意不用的（免得复盘时疑惑）
- 前端框架（React/Vue）、Web 框架（Flask/Django/FastAPI）——审计面换不来收益
- 浮点做账、价格空间做策略数学——宪法 D5/Q1 禁止
- 外部包管理的运行时依赖——热路径依赖全部 vendored 锁版本
