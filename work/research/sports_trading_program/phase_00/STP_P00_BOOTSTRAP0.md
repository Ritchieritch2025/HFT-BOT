# STP-P00-W01 — BOOTSTRAP-0 record (read-only, per canonical prompt §5)

- W-id: STP-P00-W01 · release: STP-R004-P00
  (`docs/plan_releases/sports_trading_program/STP-R004-P00.md`,
  operator_text_sha256 `0006f1670ebfc2393ec3b5c96924724bd48223a8533fd6a923db9603f9da4e78`)
- date: 2026-07-11 (UTC)
- active_prompt_path: `docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md`
- active_prompt_sha256 (measured this session, `shasum -a 256`):
  `575ea27ae08477bdcc4234a42ad5ff456c4ad45cd32ede79011caec2be3fbe54` — matches
  the release pin byte-for-byte. Prompt left read-only. The in-file CANDIDATE
  banner is creation-time metadata superseded by release pinning (D-2 ruling,
  `docs/PLAN_SPORTS_TRADING_DECISIONS.md` D-2 归档注记).

## Git state (all measured at session start)

- `BOOTSTRAP_BRANCH` = `plan-sports-market-dynamics-v2` (`git branch --show-current`)
- `BOOTSTRAP_BASE_HEAD` = `50018a55e383fd4f7cb331466da79ef023a42e88`
  (`git rev-parse HEAD`) — this is the STP-R004 receipt commit, one commit on
  top of the release's `base_commit: 833e535`. Delta explained exactly: the
  release's own "First action" required the receipt commit before any work;
  the receipt session created it. No other delta.
- worktree = `/Users/ritcardo/HFT BOT` — matches release `authorized_worktree`.
- upstream: none configured (`fatal: no upstream configured`) — expected;
  no push is authorized.
- `git status --short`: only `?? outputs/` (untracked).
- `git diff --name-status`: empty. `git diff --cached --name-status`: empty.
  **No pre-existing staged change** ⇒ fail-closed rule §5.D.1 not triggered.
- untracked inspected: `outputs/2026-07-10_muchova_gauff/Muchova_vs_Gauff_完整市场时间线.xlsx`
  — pre-existing operator-facing export, NOT owned by this program (§5.D.6),
  excluded from all staging; release marks `outputs/` read-only.

## Checkpoint (prompt §6.2)

- The release enumerates NO pre-existing dirty paths; tracked tree is clean.
- `CHECKPOINT_STATUS=NOT_NEEDED`
- `CHECKPOINT_BASE_HEAD=50018a55e383fd4f7cb331466da79ef023a42e88`

## Concurrent-session risk

- Known concurrent writer: production launchd job `com.ritcardo.rtt-baseline`
  appends `work/latency_baseline/{samples.ndjson,sampler.out.log}` every
  ~5 min (documented as R2 in
  `docs/plan_audits/sports_trading_program/STP_P00_TEST_ISOLATION_ARTIFACT.md`
  §8). Untracked/ignored paths; any isolated-run snapshot diff confined to
  those two files is attributed to the sampler.
- No other session owns this worktree during W01 (release session plan:
  W01 and AUD01 are separate sequential sessions).

## Governance hashes recorded at start (for exit verification)

- `docs/GUARDRAILS.md` sha256 =
  `9c71a5b36a02ac718bb9affbfa507cb66f773b5bab3bfadc463055ec3a15832b`
  (last commit touching it: 17c72f18, 2026-07-08 — must be unchanged at exit)
- `docs/PLAN_SPORTS_TRADING_DECISIONS.md` sha256 =
  `e679fa13b1ddf5afceb6f08da0cb15564c73c51852f90408e5abb47be4f5303c`
  (D-1 present verbatim; ledger not modified by W01)
- `docs/MASTER_SEQUENCE.md` sha256 =
  `c8e31b3eca72cf66e1214495f95e7cfdbc1d5e99bc78852407ebbf04feffa732`
- `CLAUDE.md` sha256 =
  `b0ed9d060471b526eca315db662a2cc46fe7df1194f1efe3c7db49565a596949`

## Prompt-vs-release evidence-path note (registered deviation, see conflict register)

The canonical prompt §31.2 designates the eight W01 evidence documents under
`docs/plan_audits/sports_trading_program/STP_P00_*.{md,json}`. The operator
release STP-R004-P00 allows evidence writes ONLY under
`work/research/sports_trading_program/phase_00/**` (plus STATE, SESSION_LOG,
receipt, mirror) and reserves `docs/plan_audits/sports_trading_program/` for
the audit session. Per the release's own conflict clause and the R004 session
instruction ("the prompt governs content; the release governs write scope"),
all eight required outputs are produced with the prompt-specified CONTENT at
the release-designated paths under this directory. Recorded in
`STP_P00_CONFLICT_RISK_REGISTER.md` (C-1) for AUD01 and the operator.
