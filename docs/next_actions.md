# Next actions — operator-gated line items

Items here require explicit operator approval before any agent implements
them. They exist because a plan's Forbidden-writes list blocked the wiring,
not because the wiring is optional.

- [ ] **Wire `tools/coverage_audit.py` into `tools/pipeline_supervisor.sh`**
  (daily, after the day's export completes): run
  `python3 tools/coverage_audit.py --date <yesterday>` and surface a nonzero
  exit (V15 depth-set shrinkage) in supervisor logs. OPERATOR-GATED per
  PLAN_GOLD_DATA_CONTRACT W4 Forbidden-writes (P4: the 24/7 pipeline is
  revenue-critical; supervisor edits need their own reviewed change).
  Until wired, the audit is manual and the lifecycle "Coverage Audit
  (research)" stage reports `skipped` on days nobody ran it.
- [ ] **Create `config/depth_watchlist.txt`** (the DECLARED full-depth
  subscription list for V15). Today no declared list exists: the firehose
  subscribes no `orderbook_delta`, and the 4 observed full-depth markets on
  2026-07-06 are legacy watchlist leftovers. V15 currently reports
  `declared_list_missing` (documented, not invented). The file becomes
  meaningful with the W6 depth-expansion rollout plan.
