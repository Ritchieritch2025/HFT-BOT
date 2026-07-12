# PIPE-W03 — 操作员释放令收据(2026-07-11 ~23:1xZ)

- base(recovery branch `codex/pipeline-recovery-hardening`)HEAD @ 收到时:`75d05e3`
- status: `CONSUMED — IMPLEMENTED + DIFF-REVIEW PASS, MERGED LOCALLY`
  (实现 commit `a8e6b4b`(隔离 worktree 分支 pipe-w03,ff 合入 recovery);
  diff 轻审计 **PASS**,0 P0 / 0 P1 / 4 P2(逐字归档
  `docs/plan_audits/pipe_w03_diffreview_2026-07-11.md`);根因写在
  `docs/plan_audits/pipe_w03_rootcause_2026-07-11.md`——**轮转命名嫌疑被
  推翻**,真凶 = 旧 ingest 启动裸连无重试被锁顶死,当日 57% 行缺失而旧
  导出发绿灯;prune 恢复已验(07-12 00:00Z 首删 123 文件,保留全有理由)。
  **不含部署**——生产变更等下一个操作员维护窗(部署时注意:新扫描器会
  一次性补灌 07-09 尾巴 ~10GB)。**Stage 1 implementation W 自此解锁**
  (硬门 (a)(c) 已闭,(b) 本 W 结论已吸收进扫描器/封印证据设计)。
  操作员遗留决定:07-09 是否补封(无自动路径)。
- 执行安排:隔离 worktree(分支 `pipe-w03`)内实现,gates 全绿后 diff 轻审计,
  审计过再本地合入 recovery 分支;全程不碰生产、不碰采集连续性。

## 操作员释放令原文(VERBATIM)

```
OPERATOR RELEASE — PIPE-W03 (single engineering session): Investigate and fix the root cause of the 2026-07-10 hour-13 raw file never being discovered by the ingest scanner (rotation-naming suspicion, evidence in PIPE-R001 receipts). Deliver: root-cause writeup, the fix + regression test (a rotated file that the old scanner would miss must be caught), per-channel discovery-completeness check folded into the seal chain's evidence, and the B4 pause-ownership fix (chain only removes its own export_pause). Also verify prune has resumed post-seal (Stage-1 gate a). Isolated worktree, tests green, self-report; light audit by diff review only. Then Stage 1 implementation W unlocks.
```
