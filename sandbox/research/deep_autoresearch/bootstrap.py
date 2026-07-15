#!/usr/bin/env python3
"""Bootstrap a governed MODE-1 SPORTS-AUTORESEARCH run.

This module writes identity and preregistration artifacts only.  It performs
no data analysis and has no network or trading capability.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


MISSION_REL = Path(
    "docs/plan_audits/SPORTS_AUTORESEARCH_01_MISSION_TEXT_V2_2026-07-15.md"
)
PROMPT_REL = Path("docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md")
EXPECTED_MISSION_SHA = (
    "9b4ca417dca394223ecdc6719cd1628bd5ad69d5a80003db304f73b1c485a69c"
)
EXPECTED_PROMPT_SHA = (
    "575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54"
)
BANNERS = {
    "data_evidence": "SEALED_DEGRADED_EVIDENCE",
    "timestamp_discipline": "TL1",
    "experiment_split": "EXPLORATORY_ONLY",
    "artifact_status": "DIAGNOSTIC_ONLY",
    "authorization": "NOT A LIVE-TRADING AUTHORIZATION",
}
RELEASES = [
    {
        "date": "2026-07-12",
        "release_id": "2026-07-12__seal-bc37de4c__pub-2bf8871ad4750c03",
        "manifest_sha256": "6fedbd5d2b0d811b5189a953331bd236aeea3fb5843a5a549d1100d5ee290505",
        "objects_verified": 485,
        "bytes_verified": 35_585_100_000,
        "gap_intervals_l1": 0,
    },
    {
        "date": "2026-07-13",
        "release_id": "2026-07-13__seal-7f6e5c1b__pub-f8e4c0abc742b7d5",
        "manifest_sha256": "1662fb21148c068f2a53bf6f30597b9c26230f2d72fa722b2b849fd490085ddd",
        "objects_verified": 476,
        "bytes_verified": 32_514_000_000,
        "gap_intervals_l1": 1,
    },
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def hypothesis_cards() -> list[dict]:
    common = {
        "split": "EXPLORATORY_ONLY",
        "data_evidence": "SEALED_DEGRADED_EVIDENCE",
        "timestamp_discipline": "TL1",
        "artifact_status": "DIAGNOSTIC_ONLY",
        "unit_of_inference": "root_event",
        "multiplicity_policy": "BH-FDR within family; no formal verdict",
        "fee_policy": "exact known fee class or economics remain descriptive",
        "fill_policy": "strict-through one-contract binding; at-touch diagnostic only",
        "authority_boundary": "No VALIDATION, VERDICT_PASS, live-ready, or live trading claim",
        "status": "CANDIDATE",
        "expected_direction": "The direction stated in the frozen sentence; post-hoc sign inversion is a new trial",
        "secondary_metrics": [
            "n_rows", "n_markets", "n_root_events", "n_day_blocks",
            "effect distribution", "p50", "p99", "max", "missingness",
            "delete-best-day", "delete-best-root-event",
        ],
        "exclusions": [
            "failed or unverified release", "capture-gap overlap",
            "missing causal receive clock", "ambiguous root-event mapping",
            "unknown required fee for any net-economics claim",
        ],
        "test_family": "root-event aggregation plus calendar-day block bootstrap; descriptive with only two days",
        "sample_size_power": "Fewer than 200 relevant root events or fewer than 20 independent day blocks => COLLECT_MORE for economics",
        "random_seed": 20260715,
        "required_engine_capabilities": [
            "strict receive-clock as-of join", "root-event clustering",
            "gap exclusion", "deterministic ordering", "append-only trial registry",
        ],
        "code_data_references": {
            "code_root": "sandbox/research/deep_autoresearch",
            "data_release_ids": [r["release_id"] for r in RELEASES],
        },
    }
    cards = [
        {
            "hypothesis_id": "C1-SPREAD-CAPTURE-01",
            "family": "market_making",
            "sentence": "In eligible Sports root events, when a causally observable pre-match tight-spread regime occurs, a one-contract passive quote has positive event-level net economics after strict-through fills, exact fees, adverse markout, latency, and forced exit, compared with eligible no-quote events.",
            "mechanism": "Temporary spread compensation may exceed adverse selection in selected pre-match liquidity regimes.",
            "population": "All Sports root events with catalog mapping, valid TL1 L1/trades, known lifecycle and fee class; segment selection is exploratory.",
            "decision_clock": "local receive/processed clock where available, else explicitly degraded exchange timestamp",
            "feature": "log-odds spread, quote age, message/trade intensity, imbalance, time-to-start",
            "treatment": "predeclared regime cell eligible for quoting",
            "control": "all other eligible root events including zero-fill/zero-quote events",
            "horizons": ["100ms", "1s", "5s", "30s", "120s", "forced_exit"],
            "primary_metric": "mean NetPnL per root event under strict-through one-contract replay",
            "economic_threshold": "exploratory CI lower bound > 0; cannot promote on degraded evidence",
            "negative_controls": ["sign flip", "market-label shuffle", "delayed execution", "look-ahead sentinel"],
            "rejection": "non-positive conservative event economics, leakage, or concentration in one event/day",
            "reopen": "new SEALED_CONFIRMATION days and ratified fee/fill authority",
        },
        {
            "hypothesis_id": "C1-HFOLLOW-RETREAT-01",
            "family": "market_making",
            "sentence": "In L2-covered Sports root events, when coordinated same-side depth retreat is observable across related markets, subsequent log-odds spread widening and adverse markout increase over 100ms–10s relative to matched non-retreat windows.",
            "mechanism": "Professional-liquidity withdrawal may transfer adverse-selection risk to slower quotes.",
            "population": "Sports markets with INCLUDED_SEALED_FACTS L2 and root-event linkage",
            "decision_clock": "recv_mono_ns/local_recv_ts_us with ws_sid/ws_seq continuity",
            "feature": "causal coordinated depth-retreat score; no participant identity claim",
            "treatment": "retreat score above frozen exploratory percentile within prior data",
            "control": "same sport/family/hour/spread/activity matched non-retreat windows",
            "horizons": ["100ms", "1s", "3s", "10s"],
            "primary_metric": "root-event clustered change in future log-odds spread and signed markout",
            "economic_threshold": "effect large enough to change quote eligibility after costs",
            "negative_controls": ["future-shifted retreat", "unrelated-event retreat", "market-label shuffle"],
            "rejection": "negative control works, effect lacks event support, or L2 population is too narrow",
            "reopen": "more L2-covered sealed sports days or calibrated own-order queue data",
        },
        {
            "hypothesis_id": "C1-DEPLETION-REFILL-01",
            "family": "market_making",
            "sentence": "In sequence-valid L2 Sports root events, when causally observed same-side depth depletion is followed by rapid refill, subsequent adverse log-odds markout and spread recovery improve relative to matched depletions without refill.",
            "mechanism": "Resilient liquidity replenishment can distinguish temporary consumption from informed depletion.",
            "population": "L2-covered Sports markets with snapshot reset handling and continuous ws_sid/ws_seq evidence",
            "decision_clock": "recv_mono_ns/local_recv_ts_us after the causal depletion row",
            "feature": "depletion size, refill fraction, refill latency, touch distance",
            "treatment": "depletion followed by frozen-threshold refill within 100ms/1s",
            "control": "size/side/regime matched depletion without refill",
            "horizons": ["100ms", "1s", "5s", "30s"],
            "primary_metric": "root-event clustered adverse markout and spread-recovery difference",
            "economic_threshold": "effect large enough to change conservative quote eligibility",
            "negative_controls": ["future refill", "snapshot reset", "far-from-touch removal", "side flip"],
            "rejection": "future-refill sentinel works, sequence support fails, or refill is a reset artifact",
            "reopen": "more sequence-valid L2 Sports days",
        },
        {
            "hypothesis_id": "C1-THREEWAY-OVERROUND-01",
            "family": "relative_value",
            "sentence": "In catalog-proven mutually exclusive and exhaustive three-way match-winner families, simultaneous executable YES asks sum above one after exact fees for persistent intervals with usable size, compared with synchronized non-violating family minutes.",
            "mechanism": "Fragmented family quoting can leave persistent payout-constraint residuals.",
            "population": "Only catalog-proven three-outcome winner families; arbitrary three-market groups forbidden",
            "decision_clock": "latest causal quote at a frozen synchronization clock and quote-age cap",
            "feature": "sum of simultaneous executable YES asks and minimum displayed size",
            "treatment": "fee-adjusted family ask sum above 1 plus economic threshold",
            "control": "same family non-violation intervals",
            "horizons": ["1s", "10s", "60s", "5m"],
            "primary_metric": "event-level frequency, persistence, and executable residual distribution",
            "economic_threshold": "positive residual after all-leg taker fees, slippage, and asynchronous-leg stress",
            "negative_controls": ["random three-market grouping", "stale-leg injection", "event-label shuffle"],
            "rejection": "family proof absent, simultaneous size absent, or residual vanishes after fees/staleness",
            "reopen": "catalog family-role mapping or future confirmation releases",
        },
        {
            "hypothesis_id": "C1-SOCCER-POISSON-RV-01",
            "family": "relative_value",
            "sentence": "In same-game soccer root events with observable winner and totals families, market-implied Poisson parameters produce structural residuals that persist and correct after executable prices and fees relative to same-game matched controls.",
            "mechanism": "Related family prices may be internally inconsistent even without external odds or final scores.",
            "population": "Catalog-linked soccer games with required simultaneously quoted families",
            "decision_clock": "strict as-of synchronized executable quotes",
            "feature": "market-implied Poisson residual in log-odds space",
            "treatment": "large predeclared structural residual",
            "control": "low-residual windows within the same root-event regime",
            "horizons": ["10s", "60s", "5m"],
            "primary_metric": "root-event clustered residual correction after executable costs",
            "economic_threshold": "residual exceeds fee and leg-risk bound",
            "negative_controls": ["market-label shuffle", "impossible final-score input sentinel"],
            "rejection": "required family coverage absent or executable correction is non-economic",
            "reopen": "more linked soccer family coverage",
        },
        {
            "hypothesis_id": "C1-LARGE-FLOW-CONTINUATION-01",
            "family": "directional",
            "sentence": "In Sports root events, when a causally defined large signed trade-flow event occurs, future log-odds return, spread, depth, and activity differ over fixed horizons from matched same-market non-event windows after executable entry and exit costs.",
            "mechanism": "Observable flow can reveal information or temporary liquidity pressure; it is not actor identity.",
            "population": "Sports trades with causal L1 context and root-event mapping",
            "decision_clock": "trade local receive timestamp with prior L1 state",
            "feature": "rolling past-only signed flow percentile fit inside prior-exposed exploratory data",
            "treatment": "large-flow event with side and size known at decision time",
            "control": "same sport/family/price/spread/activity/hour matched windows",
            "horizons": ["100ms", "1s", "3s", "10s", "30s", "120s"],
            "primary_metric": "root-event clustered signed future log-odds return and net taker return",
            "economic_threshold": "effect survives spread, fee, latency, slippage, and displayed-size cap",
            "negative_controls": ["sign flip", "future flow", "event-label shuffle", "unrelated-event flow"],
            "rejection": "no event-level effect, non-executable economics, or leakage sentinel failure",
            "reopen": "more sealed days or stronger causal size normalization",
        },
        {
            "hypothesis_id": "C1-PREMATCH-TTS-01",
            "family": "market_making",
            "sentence": "In Sports root events before scheduled start, transitions into active-tight regimes identify windows with better conservative spread-capture economics than dormant, active-wide, or in-play windows.",
            "mechanism": "Liquidity arrival before start can improve fill opportunity before toxicity dominates.",
            "population": "Sports markets with valid scheduled start and lifecycle timestamps",
            "decision_clock": "causal L1/trade receive clock",
            "feature": "transparent threshold regime from past message rate, log-odds spread, quote age, and volatility",
            "treatment": "active-tight pre-match transition",
            "control": "other regimes within root event, with all events retained",
            "horizons": ["1s", "10s", "30s", "120s"],
            "primary_metric": "paired per-root-event conservative economics and adverse markout",
            "economic_threshold": "positive event-level lower bound is descriptive only on current evidence",
            "negative_controls": ["future regime", "time-of-day shuffled within sport", "delayed execution"],
            "rejection": "no incremental economics over simple spread filter or unstable by sport/day",
            "reopen": "new days and governed validation authority",
        },
        {
            "hypothesis_id": "C1-RFQ-CLOB-01",
            "family": "rfq",
            "sentence": "In joinable Sports markets, observable RFQ creation changes subsequent CLOB activity, log-odds volatility, spread, and signed flow over 100ms–120s relative to matched non-RFQ windows.",
            "mechanism": "Broadcast demand may reveal hedging pressure or information arrival before it reaches CLOB trades.",
            "population": "Captured rfq_created events with market/event linkage and causal CLOB context",
            "decision_clock": "RFQ local receipt wall/monotonic time",
            "feature": "RFQ creation, size, combo status, leg count, and age; no acceptance/fill inference",
            "treatment": "first valid RFQ create event",
            "control": "matched sport/family/hour/price/spread/activity non-RFQ windows",
            "horizons": ["100ms", "1s", "3s", "10s", "30s", "120s"],
            "primary_metric": "root-event clustered post-minus-pre CLOB response versus matched controls",
            "economic_threshold": "signal effect exceeds executable cost bound; RFQ PnL remains unavailable",
            "negative_controls": ["future RFQ", "unrelated-event RFQ", "market-label shuffle"],
            "rejection": "joinability too low, matched balance fails, or negative controls reproduce effect",
            "reopen": "more RFQ days or verified quote/acceptance data",
        },
        {
            "hypothesis_id": "C1-ANOM-RFQ-SIZE-TAIL-01",
            "family": "rfq",
            "sentence": "After RFQ schema and unit validation, extreme-tail RFQs have shorter survival and larger subsequent CLOB activity and absolute log-odds volatility than matched ordinary-size RFQs.",
            "mechanism": "The private RFQ size tail may reflect institutional/combo intent or a recorder/unit artifact; either outcome is falsifiable.",
            "population": "Valid RFQ creates with size fields and market/root linkage",
            "decision_clock": "RFQ outer-envelope local receive clock",
            "feature": "validated log size, round-number concentration, combo status and leg count",
            "treatment": "past-frozen extreme-tail size threshold",
            "control": "matched median/interquartile RFQs",
            "horizons": ["lifecycle", "1s", "10s", "30s", "120s"],
            "primary_metric": "root-event clustered censor-aware survival and CLOB-impact contrast",
            "economic_threshold": "CLOB impact exceeds executable cost bound; no RFQ PnL claim",
            "negative_controls": ["unit rescaling", "market-label shuffle", "pseudo-RFQ timestamps"],
            "trigger_source": {
                "run_id": "20260715T023720Z__c21a79a8cff__cycle0",
                "rfq_rows": 2000000,
                "target_cost_e6": {"p50": 10000000, "p99": 604880000, "max": 250000000000},
                "contracts_e2": {"p50": 12987, "p99": 909090, "max": 1028102800},
            },
            "required_trigger_plot": "REPORT/charts/C1-ANOM-RFQ-SIZE-TAIL-01__trigger_ccdf.png",
            "why_data_unique": "Requires private raw RFQ broadcasts, local clocks, combo fields and same-event CLOB linkage.",
            "rejection": "tail is schema/unit corruption or has no balanced matched effect",
            "reopen": "corrected RFQ schema or more sealed RFQ days",
        },
        {
            "hypothesis_id": "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01",
            "family": "rfq",
            "sentence": "After boundary and censor correction, RFQ lifetimes contain an early-cancel/long-tail mixture whose state predicts subsequent CLOB activity more strongly than RFQ arrival alone.",
            "mechanism": "Heterogeneous intent and expiry/cancel behavior may create distinct information regimes.",
            "population": "Linkable RFQ create/delete lifecycles with explicit right censoring",
            "decision_clock": "RFQ outer-envelope local receive clock",
            "feature": "causal RFQ age/hazard state, size, combo status and arrival intensity",
            "treatment": "frozen early-cancel versus long-lived lifecycle state",
            "control": "matched RFQs outside the state plus matched non-RFQ windows",
            "horizons": ["lifecycle", "1s", "10s", "30s", "120s after create/delete"],
            "primary_metric": "root-event clustered censor-aware state/impact contrast",
            "economic_threshold": "incremental signal exceeds CLOB execution-cost bound; no RFQ PnL claim",
            "negative_controls": ["scan-boundary shift", "future delete", "arrival-only baseline", "label shuffle"],
            "trigger_source": {
                "run_id": "20260715T023720Z__c21a79a8cff__cycle0",
                "created": 770452, "deleted": 1229374, "right_censored": 159739,
                "lifetime_seconds": {"p50": 13.34562717, "p99": 896.972320723, "max": 6068.472941939},
            },
            "required_trigger_plot": "REPORT/charts/C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01__trigger_survival.png",
            "why_data_unique": "Requires private create/delete broadcasts, recorder boundaries and CLOB linkage.",
            "rejection": "mixture disappears after censor correction or adds no balanced impact information",
            "reopen": "more complete lifecycle days or stronger boundary evidence",
        },
    ]
    return [{**common, **card} for card in cards]


def methods_markdown() -> str:
    return """# METHODS — per-run preregistration snapshot

> EXPLORATORY_ONLY · SEALED_DEGRADED_EVIDENCE · NOT A LIVE-TRADING AUTHORIZATION

All methods below were registered before the first Cycle-1 result. Registration
does not imply execution: the core stage applies descriptive summaries and
strict as-of diagnostics only; block bootstrap, BH inference and matched tests
remain NOT_RUN until their named full-test stages record a result. Any method
added later must be registered before its first result is computed.

## M-BASIC-01 — 描述统计、ECDF 与标准区组自助法种子

- 通俗说明：先按比赛/root event 汇总，再看分布、中位数、尾部和每日差异，避免把数百万条消息误当成数百万次独立实验。
- 定义：报告 n_rows、n_markets、n_games、n_days、p50、p99、max；连续量同时给直方图或 ECDF。自助抽样以完整日区组抽取，区组内保留全部 root events。
- 假设与破坏条件：日期区组之间近似可交换；只有两天时区间极不稳定，必须标 COLLECT_MORE/DIAGNOSTIC_ONLY。
- 用途：描述部分用于所有 atlas 页；区组自助法仅用于后来明确记录为 EXECUTED 的完整假设检验，core 未执行该推断。
- 实现与测试：`sandbox/research/deep_autoresearch/stats.py`; 单元/符号测试随代码归档。
- 读法示例：若事件均值为 0.3¢、95% 区间 [-0.4, 1.0]¢，不能称为正收益；长右尾可能只来自一个比赛。
- 失败模式/负控：行级伪重复；强制同时展示 n_games 与 delete-best-event/day。

## M-ASOF-01 — 严格因果 as-of 连接

- 通俗说明：每个信号只能配上当时已经看到的最后一条盘口，未来盘口绝不能倒灌。
- 定义：交易前状态对同一 market 选择严格 `book_ts < decision_ts` 的最大 book_ts；固定 horizon 的结果选择 target 时刻或之前最后一个可用状态（`book_ts <= target_ts`），并报告 decision/outcome book age。所有 target 必须在封存覆盖边界内且窗口不得跨 capture gap。
- 假设与破坏条件：时钟含义一致且 TL1 有效；混钟或 future row 会使结果作废。
- 用途：C1-SPREAD-CAPTURE-01、C1-LARGE-FLOW-CONTINUATION-01、C1-PREMATCH-TTS-01、C1-RFQ-CLOB-01，以及两张 L2 卡。
- 实现与测试：`features.py`、`run_cycle1.py`、`core_hypothesis_tests.py`、`rfq_full_stage.py`、`l2_hypothesis_stage.py`; 各 stage 的 look-ahead sentinel 必须为零。
- 读法示例：trade 发生于 t，只能看到 t 以前的 book；t+1s markout 不可参与 t 的 eligibility。
- 失败模式/负控：时间反向、同 timestamp tie；future-shift signal 和 impossible-lookahead sentinel。

## M-BOOT-01 — root-event/calendar-day block bootstrap

- 通俗说明：重复抽“完整的一天”，一天里的所有比赛一起保留，用来估计每场比赛平均结果的不确定性。
- 定义：`mu = mean_e(NetPnL_e)`；固定 seed 重抽 day blocks 至少 1,000 次，每次计算所有抽中日内 root events 的 mu，取 2.5%/97.5% 分位。
- 参考：Efron & Tibshirani (1993), *An Introduction to the Bootstrap*。
- 假设与破坏条件：日区组近似代表未来日；本轮仅两日，区间仅诊断，不能 promotion/verdict。
- 用途：C1-LARGE-FLOW-CONTINUATION-01、C1-PREMATCH-TTS-01、C1-HFOLLOW-RETREAT-01 与 C1-DEPLETION-REFILL-01 的诊断性 event effect；单 evaluation day 必须标退化。
- 实现与测试：`stats.py:block_bootstrap_mean` 与各 stage wrapper；deterministic seed 20260715，至少 1,000 次。
- 读法示例：lower bound <= 0 表示不能排除无效/亏损；两日结果受单日强烈支配。
- 失败模式/负控：按消息抽样会虚假缩窄区间；与 clean-room exact day enumeration 对照。

## M-BH-01 — Benjamini–Hochberg FDR

- 通俗说明：同时试很多信号会偶然出现小 p 值；BH 在同一家族内控制预期错误发现比例。
- 定义：排序 p_(i)，调整值 `q_(i)=min_{j>=i}(m/j)*p_(j)` 并保持单调，探索阈值 q<=0.10；仍不构成正式 verdict。
- 参考：Benjamini & Hochberg (1995), JRSS-B 57(1):289–300。
- 假设与破坏条件：独立或正依赖近似；高度相关时同时报告原始 trial 数和效应分布。
- 用途：后来真正产生 p 值的固定 horizon/segment families；Cycle-1 core 不生成 p 值也不执行 BH。
- 实现与测试：`stats.py:benjamini_hochberg`。
- 读法示例：raw p=.01 但 q=.18 表示校正后不存活。
- 失败模式/负控：漏报失败 trial；TRIAL_REGISTRY append-only 并对注册数交叉检查。

## M-MATCH-01 — 分层匹配事件研究

- 通俗说明：把 RFQ/大流量发生时段与相同体育、价格、点差、活跃度和小时的普通时段比较，减少“本来就更热闹”的混淆。
- 定义：在预先列出的离散 strata 内 deterministic nearest-neighbor matching；报告标准化差异、未匹配率、root-event clustered effect。
- 参考：Rosenbaum & Rubin (1983), *Biometrika* 70(1):41–55。
- 假设与破坏条件：只控制观测到的混淆；balance 失败或 positivity 不足则 DATA_STARVED。
- 用途：C1-RFQ-CLOB-01、C1-LARGE-FLOW-CONTINUATION-01、C1-HFOLLOW-RETREAT-01 与 C1-DEPLETION-REFILL-01 的 matched diagnostic。
- 实现与测试：`core_hypothesis_tests.py:build_large_flow`、`rfq_full_stage.py:build_clob_context`、`l2_hypothesis_stage.py:deterministic_match` 及对应 balance/fixture tests。
- 读法示例：处理组活跃度仍高 2 个标准差时，post effect 不能归因于处理。
- 失败模式/负控：未观测比赛状态；加入 unrelated-event、future-event 和 label-shuffle controls。

## M-KM-01 — 右删失 Kaplan–Meier 生存曲线

- 通俗说明：RFQ 到扫描结束仍没有看到删除，并不等于它“永远不删除”。KM 会把这类记录当作只知道至少活到某时刻的右删失样本，而不是硬算成完整寿命。
- 定义：按接收时钟寿命排序，`S(t)=prod_{t_i<=t}(1-d_i/n_i)`；`d_i` 为时刻 t_i 观察到的首个有效删除数，`n_i` 为该时刻前仍处于风险集的请求数。首个 recorder loss/close/error/epoch 边界截断风险集，跨边界删除不得倒填。
- 参考：Kaplan & Meier (1958), *Journal of the American Statistical Association* 53(282):457–481。
- 假设与破坏条件：删失机制在已观察分层内不携带未建模的寿命信息；若采集边界漏记、create/delete 身份错连或时钟回退，生存曲线作废。
- 用途：C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01 的 RFQ 生命周期，以及 C1-DEPLETION-REFILL-01 的 refill-time 描述；都不解释为成交、接受、own-order fill 或 PnL。
- 实现与测试：`rfq_full_stage.py:kaplan_meier_from_counts` 与 `l2_hypothesis_stage.py:generate_charts`；grouped-censor、observation-boundary、snapshot/reset/right-censor fixture tests。
- 读法示例：先导触发样本的未校正寿命 p50=13.34562717 秒只说明触发分布；完整阶段若边界校正后的 `S(10s)=0.6`，含义是估计 60% 请求至少存活 10 秒，不是 60% 会成交。
- 失败模式/负控：把 scan end 当删除会向下偏寿命；把跨断流 delete 倒填会虚假完整。负控为 scan-boundary shift、future-delete sentinel 与跨 observation-boundary delete 隔离计数。

## M-L2-REPLAY-01 — snapshot-aware L2 确定性回放

- 通俗说明：必须先用完整 snapshot 建立盘口，之后才能按接收顺序应用 delta；没有锚点或遇到重置时不能猜深度，snapshot 本身也不能冒充 refill。
- 定义：每个 date/market/side/price 状态由最近 snapshot 重建，随后应用有界 delta；pre-snapshot delta 排除。流完整性只认 sealed per-sid `quality/l2_gaps.json`，每市场 `ws_seq` 跳号禁止用作丢包推断。
- 参考：事件驱动 limit-order-book reconstruction 的标准状态机结构；本任务所有阈值由 2026-07-12 自有数据冻结，不采用文献默认数值。
- 假设与破坏条件：snapshot/delta schema 有效、接收排序确定、sealed receipt 覆盖完整 sid stream；任一失败都隔离相应 release/day。
- 用途：C1-HFOLLOW-RETREAT-01 与 C1-DEPLETION-REFILL-01。
- 实现与测试：`l2_hypothesis_stage.py` replay/FSM；synthetic snapshot/delta、reset sentinel 与 sealed-receipt fixture tests。
- 读法示例：depletion 后 100ms 内恢复到冻结比例才算 rapid refill；中途出现 snapshot 时该 episode 右删失，绝不是成功 refill。
- 失败模式/负控：snapshot 当新增量、按市场 ws_seq 误判缺口、负深度或前视 refill都会造假；防护为 reset sentinel、future-refill、unrelated-root/label shuffle 与非负深度断言。
"""


def direction_ledger() -> str:
    directions = [
        ("A01", "spread-capture economics by segment", "CYCLE1_DEEP"),
        ("A02", "toxicity/markout decomposition", "CYCLE1_DEEP"),
        ("A03", "depletion-refill resilience", "CYCLE1_TIER1"),
        ("A04", "queue/fill probability lambda(delta)", "DEFERRED_NEEDS_CALIBRATED_OWN_ORDER"),
        ("A05", "liquidity regimes", "CYCLE1_DEEP"),
        ("A06", "quote-lifetime and burst dynamics", "CYCLE1_TIER1"),
        ("A07a", "H-FOLLOW professional-presence fingerprint proxy", "CYCLE1_TIER1_NO_IDENTITY_CLAIM"),
        ("A07b", "H-FOLLOW displayed-anchor retreat signal", "CYCLE1_DEEP_NO_IDENTITY_CLAIM"),
        ("A07c", "H-FOLLOW adverse-selection transfer", "CYCLE1_TIER1"),
        ("A07d", "H-FOLLOW anchor manipulability", "CYCLE1_TIER1_NEGATIVE_CONTROL"),
        ("A08", "price-grid/round-number clustering", "CYCLE1_TIER1"),
        ("A09", "liquidity seasonality by hour/day", "DEFERRED_CANDIDATE_FIRST_AND_ONLY_TWO_DAYS"),
        ("A10", "cross-market quote spillover within events", "CYCLE1_TIER1_IF_ROOT_MAPPABLE"),
        ("B01", "multi-horizon continuation/reversion", "CYCLE1_DEEP"),
        ("B02", "OFI/imbalance/microprice predictivity", "CYCLE1_TIER1"),
        ("B03", "large-flow event studies", "CYCLE1_DEEP"),
        ("B04", "favorite-longshot calibration", "DEFERRED_NEEDS_SETTLED_OUTCOMES"),
        ("B05", "listing-to-start drift", "CYCLE1_TIER1_IF_START_VERIFIED"),
        ("B06", "in-play favorite erosion", "CYCLE1_TIER1_DIAGNOSTIC_ONLY"),
        ("B07", "realized-volatility structure by time-to-event", "CYCLE1_TIER1_IF_START_VERIFIED"),
        ("B08", "jump hazard modeling", "CYCLE1_TIER1"),
        ("B09", "settlement-calibration Brier/log scoring", "DATA_STARVED_NEEDS_SETTLED_OUTCOMES"),
        ("C01", "three-way family overround", "CYCLE1_DEEP"),
        ("C02", "bracket/threshold monotonicity", "CYCLE1_TIER1"),
        ("C03", "same-event family coherence/Poisson", "CYCLE1_DEEP_IF_MAPPABLE"),
        ("C04", "within-event lead-lag", "CYCLE1_TIER1"),
        ("C05", "combo/MVE versus leg cost", "CYCLE1_TIER1"),
        ("C06", "payout-state dominance", "CYCLE1_TIER1"),
        ("C07", "multi-day series structure", "COLLECT_MORE_ONLY_TWO_DAYS"),
        ("D01", "RFQ flow census", "CYCLE1_TIER1"),
        ("D02", "RFQ size and economic intent", "CYCLE1_TIER1"),
        ("D03", "RFQ lifecycle survival", "CYCLE1_TIER1"),
        ("D04", "RFQ combo demand structure and leg hedging pressure", "CYCLE1_TIER1_IF_LEGS_JOIN"),
        ("D05", "RFQ requester-hash clustering", "CYCLE1_TIER1_IF_ID_COVERAGE"),
        ("D06", "RFQ as direction/volatility signal", "CYCLE1_DEEP_DIRECTION_ONLY_IF_SIDE_OBSERVED"),
        ("D07", "RFQ-to-CLOB impact study", "CYCLE1_DEEP"),
        ("E01", "settlement-convergence microstructure", "DEFERRED_CANDIDATE_FIRST"),
        ("E02", "expiry liquidity migration", "DEFERRED_CANDIDATE_FIRST"),
        ("E03", "freeze-window behavior", "DEFERRED_CANDIDATE_FIRST"),
        ("F01", "participant-mix proxies over time", "DEFERRED_SPARE_CAPACITY_ONLY"),
        ("F02", "fee-structure effects on quoting", "DATA_STARVED_OQ1_FEE_RATIFICATION"),
        ("F03", "new-market cold-start dynamics", "DEFERRED_ONLY_TWO_DAYS"),
        ("X01", "external odds/data directions", "DEFERRED_REQUIRES_SEPARATE_APPROVAL"),
    ]
    lines = [
        "# RESEARCH_DIRECTION_LEDGER — Cycle 1 registration",
        "",
        "> EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION",
        "",
        "Registration precedes results. Candidate-first work is marked CYCLE1_DEEP;",
        "Tier-1 and deferred directions remain registered without preempting it.",
        "",
        "| ID | Direction | Cycle-1 disposition |",
        "|---|---|---|",
    ]
    lines.extend(f"| {i} | {name} | `{status}` |" for i, name, status in directions)
    return "\n".join(lines) + "\n"


def bootstrap(repo: Path, run_id: str, started_at: str, operator_text: str) -> Path:
    mission = repo / MISSION_REL
    prompt = repo / PROMPT_REL
    if sha256(mission) != EXPECTED_MISSION_SHA:
        raise SystemExit("mission SHA mismatch; refusing to bootstrap")
    if sha256(prompt) != EXPECTED_PROMPT_SHA:
        raise SystemExit("canonical prompt SHA mismatch; refusing to bootstrap")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    # The repository's existing run-id convention uses an 11-character Git
    # prefix (the Cycle-0 id is the precedent).  Eleven hexadecimal characters
    # still bind this run unambiguously in the current repository.
    if commit[:11] not in run_id:
        raise SystemExit("run_id does not bind current commit prefix")
    run_dir = repo / "work/research/auto_research" / run_id
    if run_dir.exists():
        raise SystemExit(f"run directory already exists: {run_dir}")
    for rel in (
        "REPORT/charts", "REPORT/tables", "CANDIDATE_DOSSIERS",
        "REJECTED_HYPOTHESES", "DATA_INTEGRITY/manifests",
        "DATA_INTEGRITY/verified", "queries", "logs", "cache",
        "DAILY_DIGESTS", "STEERING_ORDERS",
    ):
        (run_dir / rel).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mission, run_dir / "MISSION.md")
    operator_sha = hashlib.sha256(operator_text.encode("utf-8")).hexdigest()
    (run_dir / "OPERATOR_RELEASE.md").write_text(
        "# OPERATOR RELEASE\n\n"
        f"- received_at_utc: `{started_at}`\n"
        f"- operator_text_sha256: `{operator_sha}`\n"
        f"- mission_sha256: `{EXPECTED_MISSION_SHA}`\n"
        "- mode: `MODE 1 / EXPLORATORY_AUTORESEARCH`\n"
        "- degraded_evidence_admission: `EXPLICITLY_AUTHORIZED`\n\n"
        "## Verbatim operator text\n\n> "
        + operator_text.replace("\n", "\n> ")
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "sports-autoresearch-deep-v1",
        "run_id": run_id,
        "status": "BOOTSTRAPPED_BEFORE_ANALYSIS",
        "started_at_utc": started_at,
        "holdout_cutoff_utc": started_at,
        "repo_commit": commit,
        "repo_dirty_at_start": bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo, text=True
        ).strip()),
        "mission": {"path": str(MISSION_REL), "sha256": EXPECTED_MISSION_SHA},
        "canonical_prompt": {"path": str(PROMPT_REL), "sha256": EXPECTED_PROMPT_SHA},
        "operator_text_sha256": operator_sha,
        "mode": "EXPLORATORY_AUTORESEARCH",
        "explicit_degraded_admission": True,
        "banners": BANNERS,
        "compute": {
            "instance_id": "i-0e53d134dceffe166",
            "instance_type": "r8g.2xlarge",
            "region": "us-east-2",
            "role": "w09-research-runner",
            "threads": 8,
            "duckdb_memory_limit": "40GB",
            "remote_run_dir": f"/srv/w09-research/runs/{run_id}",
        },
        "releases": RELEASES,
        "trial_policy": {"max_new_hypotheses_cycle": 10, "registered_initial": 10},
        "prohibited_claims": [
            "VALIDATION", "HISTORICAL_CONFIRMATION", "VERDICT_PASS",
            "live-ready", "live trading authorization",
        ],
    }
    write_json(run_dir / "RUN_MANIFEST.json", manifest)
    write_json(run_dir / "PRIOR_EXPOSURE.json", {
        "holdout_cutoff_utc": started_at,
        "rule": "Every sealed release available before cutoff is PRIOR_EXPOSED",
        "release_ids": [r["release_id"] for r in RELEASES],
        "dates": [r["date"] for r in RELEASES],
        "post_start_sealed_days_opened": [],
        "formal_holdout_opened": False,
    })
    cards = hypothesis_cards()
    write_json(run_dir / "HYPOTHESIS_LEDGER.json", {
        "cycle": 1, "registered_before_results": True,
        "hypotheses": cards,
        "reserved_anomaly_slots": 0,
        "own_data_anomaly_hypotheses": [
            "C1-ANOM-RFQ-SIZE-TAIL-01",
            "C1-ANOM-RFQ-LIFECYCLE-MIXTURE-01"
        ],
    })
    (run_dir / "HYPOTHESIS_LEDGER.md").write_text(
        "# HYPOTHESIS LEDGER — Cycle 1 preregistration\n\n"
        "> EXPLORATORY_ONLY · DIAGNOSTIC_ONLY · NOT A LIVE-TRADING AUTHORIZATION\n\n"
        + "\n".join(
            f"## {c['hypothesis_id']}\n\n{c['sentence']}\n\n"
            f"- family: `{c['family']}`\n- status: `{c['status']}`\n"
            f"- primary metric: {c['primary_metric']}\n"
            f"- rejection: {c['rejection']}\n"
            for c in cards
        ),
        encoding="utf-8",
    )
    with (run_dir / "TRIAL_REGISTRY.jsonl").open("w", encoding="utf-8") as handle:
        for card in cards:
            handle.write(json.dumps({
                "registered_at_utc": started_at,
                "result_opened": False,
                "trial_id": card["hypothesis_id"],
                "family": card["family"],
                "status": "REGISTERED",
                "split": "EXPLORATORY_ONLY",
            }, sort_keys=True) + "\n")
    with (run_dir / "INCLUSION_EXCLUSION.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["release_id", "date", "decision", "evidence_tier", "reason"])
        for rel in RELEASES:
            writer.writerow([
                rel["release_id"], rel["date"], "INCLUDE_EXPLORATORY",
                "SEALED_DEGRADED_EVIDENCE",
                "VERSION_BOUND and exact-object verified; explicit operator admission; W03 quality pending",
            ])
    (run_dir / "METHODS.md").write_text(methods_markdown(), encoding="utf-8")
    (run_dir / "RESEARCH_DIRECTION_LEDGER.md").write_text(
        direction_ledger(), encoding="utf-8"
    )
    write_json(run_dir / "FEATURE_DICTIONARY.json", {
        "status": "PREREGISTERED_PENDING_SCHEMA_BINDING",
        "rule": "Every implemented feature must be filled before first dependent result",
        "features": [],
    })
    (run_dir / "FEATURE_DICTIONARY.md").write_text(
        "# FEATURE DICTIONARY\n\nSchema binding pending. No result may use a "
        "feature before its exact formula, clock, null/gap behavior and tests are "
        "recorded here.\n", encoding="utf-8"
    )
    (run_dir / "PILOT_SCOPE.md").write_text(
        "# Deep Cycle-1 scope\n\nThis is not a path smoke. It is a formal "
        "MODE-1 exploratory deep-research cycle over prior-exposed degraded "
        "evidence. It cannot issue validation, verdict, promotion, or live claims.\n",
        encoding="utf-8",
    )
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--operator-text", required=True)
    args = parser.parse_args()
    run_dir = bootstrap(args.repo.resolve(), args.run_id, args.started_at, args.operator_text)
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
