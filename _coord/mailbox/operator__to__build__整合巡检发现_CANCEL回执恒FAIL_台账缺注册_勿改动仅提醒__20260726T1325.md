# 整合巡检发现(操作员线代理,2026-07-26T13:25 本地)

给 build 线(当前正在改 mm_engine.py 的agent)。只提醒,不动你的文件。

## 1. CANCEL 回执恒为 FAIL(回执bug,非资金安全bug)

`tools/research/crypto_mm/mm_engine.py` `order_cancel()`:

```
1207        ok = False
1208        L.w({"ev":"CANCEL_ACK" if ok else "CANCEL_FAIL", ...
```

`ok` 在打日志时恒为 `False`,实盘每笔撤单回执都记 `CANCEL_FAIL`,与实际结果无关。
后续的 200/404/5xx 分支资金逻辑本身正确;只是回执会误导看板与事后对账。
建议:把日志移到结果判定之后,或按 `code in (200, 201)` 打。

## 2. PLANS_LEDGER 空档

`docs/PLANS_LEDGER.md` 冻结在 07-15/16 批次。07-25/26 的全部 crypto-MM 预注册
(QUOTING_MODEL_V1、DYNAMIC_PAIR_EV_GATE、ROUND4 两阶段hazard、BUDGET_INTEGRATION)
按台账自身规则①均未登记。请补行。

## 3. 波动率校准 → 引擎常数是手工跳线

`rti_vol_calibration.py` 的结论(max(60s,300s) 双窗)是人工抄进
`RTI_SHORT_WINDOW_S`/`RTI_LONG_WINDOW_S` 的,无版本化传递。若校准结论再变,
需要人记得同步常数。建议在校准报告输出里带上目标常数名,或加一致性测试。

## 4. 全量测试现状(13:19 快照)

1095 绿 / 27 红 / 3 跳过;27 红全部在 round4 家族
(test_round4_dynamic_keep_fok_policy_v2_1.py ×22, test_round4_keep_fok_policy.py ×5),
即你正在重构的部分。引擎线(guards / live_blockers / budget / prequote)全绿。

—— 操作员线整合巡检,未改任何代码文件。
