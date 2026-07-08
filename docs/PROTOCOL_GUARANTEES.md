# PROTOCOL GUARANTEES — capture-layer delivery model (verified 2026-07-08)

Operator-verified statement of what the Kalshi WS/REST protocol does and does
not guarantee, and how this system compensates. Every claim below was verified
empirically on 2026-07-08 (duplicate-pair diff, 166,658-trade identity check,
62-gap recovery-mechanism scan). Update this file if the venue's protocol
semantics change (spec-drift watcher).

## The model

Within a live WebSocket connection we get TCP's in-order/no-loss delivery, and
every message carries a per-subscription sequence number (sid+seq) plus an
exchange-issued globally-unique trade_id; we archive the sequence numbers and
dedup on trade_id, which gives us effectively exactly-once, in-order capture
for any window we're connected — verified empirically (duplicate pairs share
identical bodies and trade_ids but different per-connection seq, so trade_id
is the correct dedup key and byte-level dedup would be wrong). Across
disconnects the venue offers nothing — no replay, no resume; a reconnect
starts a fresh stream at seq=1 — so outage windows are unrecoverable for
order-book state, while trades remain recoverable via the REST history API.

## The three compensating moves (all queued in the playbook)

1. Continuous seq-gap checking — in-connection loss detected and counted, not
   just stored (bundled with the data-frame-silence watchdog that catches
   "connection alive but stream dead"; today's any-frame watchdog is blind to
   it). → playbook 3b (W-C5 bundle).
2. Automatic gap-healing — REST-backfill trades for every recorded outage
   window, tagged rest_backfill, never mixed with live capture. → playbook 3f.
3. Uptime engineering — book-state loss is only preventable by uptime:
   migrate off the sleeping laptop (proven root cause) to always-on EC2 with a
   dual-capture zero-gap cutover; the overlap diff then measures the
   single-machine miss rate directly. → PLAN_AWS_MIGRATION.

**Net: exactly-once where connected; honest and self-healing where not; and
uptime engineering for the part no protocol can give back.**
