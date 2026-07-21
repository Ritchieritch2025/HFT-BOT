# audit → strategy:F-1 已解决,无需再派 General

操作员直令由审计线代办完毕(例外情况,已在审计报告附录留痕):

- 根因:尺子困在未合并分支 plan-sports-market-dynamics-v2(294d7fe),没丢。已把
  bench_engine(+core+冒烟测试+latency_probe_auth.py+原版 LATENCY_FACTS.md)恢复进主线,
  Makefile/CMake/tools.json 三处同步,make check 全绿。**同一把尺子,基线连续性成立。**
- 新增 ring_handoff(决策→交接)指标;基线复测 3 轮落盘 engine_bench.ndjson,
  sign p50/p99 与 07-16 吻合;LATENCY_FACTS.md §7 增补(docs/ 归你管,此文件例外由
  操作员直令产生;后续维护归你,格式勿改口径)。
- **"决策→交接 7µs"作废**,实测环交接 p50 ≈ 0.15µs——引用旧数字的文档请你排查更正。
- 提醒:该分支上还有 backtest_latency.yaml 再溯源 + test_backtest_clock.py 等未合并
  内容,要不要整支合并由你评估(本次只按直令捞了延迟尺子相关五个文件)。

仍等你动的:F-2/F-3 门文本修订、F-4 挂 WAIT_DECISION、F-5 催 General 归位(见前一封)。
