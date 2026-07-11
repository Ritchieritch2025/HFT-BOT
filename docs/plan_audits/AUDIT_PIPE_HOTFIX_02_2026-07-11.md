# Independent audit — PIPE-HOTFIX-02 (2026-07-11)

Auditor role: independent Codex subagent; read-only; did not modify files.

## Verbatim verdict

> 独立只读审计结论：PASS（无阻断项）。基点精确为 f4769ad4cf56；diff
> 仅 supervisor/systemd/test + release doc。AUTO_RESEARCH 在 source
> ~/.kalshi/env.sh 前捕获、source 后恢复，默认/非法值均归 0；生产 unit
> 明确 Environment=AUTO_RESEARCH=0，因此 credential 变量不能扩权。
> disabled 分支仍先 verify-seal + coverage_audit，capture_gaps 路径未动，
> 只跳过 3 个 mm_*，且不进入 publish_research_receipt。bash -n、git
> diff --check、2 个定向 pytest 均 PASS。部署/回滚条件：必须复制新 unit +
> daemon-reload 后 controlled restart；确认 exact SHA、auto_research=0/
> AUTO_RESEARCH_DISABLED、跨两 UTC 整点无 mm_*、raw/staging 增长、无
> SIGKILL。非阻断测试债：新增 contract test 是静态字符串/顺序断言，理论上
> 可绿骗（未执行真实 shell 分支，也不检查 systemd drop-in）；当前代码经全文
> rg/控制流人工核对无旁路，且两整点生产验收补足。systemd-analyze 本机不可用，
> 需 EC2 部署前对安装后的 unit verify。未修改任何文件。

## Root-agent gate evidence after audit handoff

- `bash -n tools/pipeline_supervisor.sh`: PASS.
- `python3 -m pytest -q tests/test_pipeline_contract.py`: PASS with the one
  repository-known xfail.
- `make check`: PASS.
- `tests/run_pipeline.sh`: all listed suites PASS after the isolated worktree's
  full build artifacts were generated; registry PASS (127 tools).
- `git diff --check`: PASS.

The production restart and two-hour-boundary observation remain deployment
acceptance, not pre-deployment evidence.
