# W-RFQ-FRESH-01 offset-generation deployment receipt

Date: `2026-07-19`

State: `DEPLOYED / PRE-T0 CANARY PENDING HOUR CLOSE`

## Independent audit

- audited commits: `faebc17` and
  `759625c0e471d7a1cdb5878317192aa5a7007c9b`;
- verdict: `PASS_WITH_DEPLOYMENT_PRECONDITIONS`;
- tests: `321/321` passed (`60` capture, `173` receipt validator, `26`
  source-evidence, `38` flow-report, `24` authority-precommit);
- whole-object SHA-256 remains byte-zero based; only segment evidence parsing
  begins at the exact pre-capture complete-line cursor;
- `bytes_before != 0` remains ineligible and was not relaxed.

## Immutable deployment binding

- base capture commit: `f39f574068fe544e27e759963db8aabe5755148b`;
- deployed capture commit:
  `759625c0e471d7a1cdb5878317192aa5a7007c9b`;
- changed paths relative to base: `tools/rfq_capture.py` and
  `tests/test_rfq_capture.py` only;
- runtime: `/home/ubuntu/hft-bot-rfq-fresh-20260720-01`;
- runtime state at deployment: detached exact HEAD, clean worktree;
- collector binary SHA-256:
  `5fb25e9269c5b1562c1b4a74f4e820f2557d3acc277424886814b10987686e7b`;
- generation: `fresh-rfq-20260720-01`;
- strict T0: `2026-07-20T00:00:00Z`;
- authority created/precommitted: `2026-07-19T14:56:55Z`;
- authority SHA-256:
  `11faaf27e1f7b49689e77e6b58034d83ff23b94ed4d507291cfcff4deee37912`;
- precommit envelope SHA-256:
  `0ef4e0d52911cfdd0b10ffd09ee770e56e5e19f0370d5068951627d6a3eb561e`;
- immutable envelope file SHA-256:
  `2fa1caf792d540e0b7ce4641bb82161f09ce3926a81652a2006468a9649ceaf2`;
- authority file: root-owned `0444`; generation directory: root-owned `0555`;
- dry run result before restart: `fresh_lane_state=BOUND_AUTHORITY`.

The complete historical 284-object deny identity set was copied from the
previous validated authority through the canonical authority builder. The old
lineage remains `DATA_INTEGRITY_BLOCKED / REPAIR_FORBIDDEN`.

## Isolated service cutover

- restart requested: `2026-07-19T14:57:57Z`;
- new RFQ service active: `2026-07-19T14:58:27Z`;
- systemd unit changed: `kalshi-rfq-capture.service` only;
- previous RFQ supervisor PID: `1346076`;
- new RFQ supervisor PID: `1987742`;
- new RFQ child PID: `1987753`;
- RFQ service `NRestarts`: `0` after cutover;
- primary `kalshi-pipeline.service` PID before/after: `36630` / `36630`;
- primary `kalshi-pipeline.service` `NRestarts`: `0`;
- primary service state after cutover: `active`.

The old RFQ supervisor did not complete its graceful stop inside the existing
30-second `TimeoutStopSec` and systemd killed that isolated RFQ cgroup. This
does not affect the primary pipeline, but it is another reason the 14:00 UTC
hour remains diagnostic/ineligible.

## Canary boundary

- `2026-07-19T14` is ineligible because the generation changed mid-hour and
  the object contained pre-existing bytes;
- at `2026-07-19T15:00:00Z`, the same new child created
  `date=2026-07-19/rfq_15.ndjson` from zero bytes;
- its first complete row is the canonical `hour_open` marker;
- supervisor PID, child PID and primary pipeline PID did not change at the
  boundary;
- final status remains pending until the 15:00–16:00 object closes and the
  canonical hour receipt is independently validated.

No prior failed hour is relabelled or repaired. The earliest possible full
research-eligible RFQ analysis date is `2026-07-20`, and only if all 24
analysis-hour receipts plus the D+1 00/01 watermark gates pass.

## Canary validation — 2026-07-19T16:00Z hour close (read-only)

Observed over SSH read-only at `2026-07-19T16:02:54Z` from
`work/live/rfq_segments.ndjson` (last line, receipt-line SHA-256
`9ec4757319be2c4cfcaa53f455dc741b8c7bb39a4485ebc95b85f084e9d5d5b5`):

- segment hour `2026-07-19T15`: **status `PASS`** — the first fully clean
  closed hour under generation `fresh-rfq-20260720-01`;
- `start_lag_ms = 0` (the hourly late-start defect is mechanically fixed);
- `findings = []`, `end_reason = boundary`, `subscription_proven = true`,
  `subscription_invalidations = 0`, `max_stream_epoch = 1`,
  markers `hour_open = 1`, `close_stability_ms = 2027`;
- volume: 16 shards, shard-set SHA
  `010cd1293eedcf7b9095eb76b8129bcd26c4006909340f3977277ec5b8078567`,
  recorder rows `4,274,959` (`rfq_created 2,225,153` / `rfq_deleted
  2,049,805`), recorder dropped `0`, write failures `0`.

Scope of this entry: capture/parser mechanics proof ONLY. The hour is
pre-T0 (`fresh_lane_state = BOUND_PRE_T0_DIAGNOSTIC`) and remains
research-ineligible. No prior hour is relabelled. Eligibility still requires
the full `2026-07-20` 24/24 analysis hours plus D+1 00/01 watermark gates.
