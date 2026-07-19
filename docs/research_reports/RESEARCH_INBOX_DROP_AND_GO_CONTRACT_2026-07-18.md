# Research Inbox: Drop-and-Go Contract

**Status:** implementation contract  
**Execution target:** isolated W09 research instance  
**Data plane:** canonical S3 objects, exact VersionIds, read-only  
**User action:** drop or paste one Markdown research plan and press **Run research**

## What the user should experience

1. Open the dashboard's **Research** tab.
2. Drop a Markdown plan, or paste its text.
3. Press **Run research** once.
4. Watch one job move through `QUEUED -> PREFLIGHT -> READY -> RUNNING -> COMPLETE`.
5. Press **Open report**. The report contains the method, findings, limitations,
   exact data releases, code/method hashes, and execution receipts.

The user does not select S3 keys, copy data into a research bucket, build a
manifest, log in to W09, or manually download a report.

## Fixed pipeline

```text
Markdown plan
    -> immutable job + plan SHA-256
    -> method router
    -> W09 exact-version data catalog
    -> automatic date/release selection
    -> data-quality and scope preflight
    -> hash-pinned method runner on W09
    -> RESULTS.json + self-contained HTML report
    -> verified report returned to the dashboard
```

Only small control and report files cross the Mac/W09 control channel. Market
data never travels through that channel. W09 reads the canonical production
objects named by the V3 manifests and keeps a content-addressed local cache;
there is no duplicate research S3 bucket.

## Generalization model

The data/catalog/queue/report pipeline is shared by every experiment. Each
research method is a small, reviewed, SHA-256-pinned adapter registered by a
stable method ID. A registered method can be reused by any later plan without
new data plumbing.

- A known method is routed and run automatically.
- A genuinely new analysis method is accepted but stops at `NEEDS_METHOD`.
  Adding its adapter is a one-time engineering step; after registration, plans
  using that method become drop-and-go.
- Markdown is always treated as data. It is never executed as Python, SQL, or
  shell text.

This distinction is deliberate: “generalizable” means one common job/data/
execution/report framework, not unrestricted execution of prose.

## Minimal plan format

Plain Markdown is accepted. Optional YAML front matter makes routing exact:

```markdown
---
title: My market experiment
method: registered-method-id
objective: Test whether the stated signal survives realistic costs.
date_window:
  start: AUTO
  end: AUTO
data:
  required: [L1, L2, TRADES, MARKET_GRAPH]
  optional: []
  forbidden: [RFQ]
  selector: AUTO_AVAILABLE_CONTIGUOUS
budget:
  max_runtime_seconds: 86400
  max_spend_usd: 15
report: [SUMMARY, METHOD, RESULTS, LIMITATIONS, RECEIPTS]
---

# Hypothesis

Describe the hypothesis, controls, tests, rejection criteria, and desired
figures or tables in normal prose.
```

`AUTO` selects the newest contiguous set of releases that satisfies the
method's declared data requirements. A plan may instead bind explicit dates or
release IDs when reproducibility requires a frozen sample.

## Safety and truthfulness

- W09 uses its instance profile; no static AWS or trading credential is sent.
- S3 access is exact-version read-only. S3 writes, production mutations, and
  orders are outside this interface.
- Every plan, method, catalog, selection, result, and report is hash-bound.
- Partial catalogs, missing days, malformed releases, scope drift, and status
  regression fail closed and remain visible in the job.
- `COMPLETE` is impossible unless a matching `RESULTS.json`, report, and report
  receipt have all been verified locally.
- RFQ is opt-in per method and per release. Old damaged RFQ data is never
  silently reintroduced.

## Definition of done

The interface is production-ready only after all of these are demonstrated:

1. A dashboard submission creates an immutable job.
2. W09 automatically selects and queries exact V3 releases.
3. The registered method actually runs under its authority and resource limits.
4. Results and the report return to the same dashboard job.
5. A newly published UTC day automatically appears in the W09 catalog and can
   be selected without code or IAM changes.
6. A second, non-Deep03 method completes through the identical pipeline.

