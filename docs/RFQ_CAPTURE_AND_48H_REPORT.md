# Passive RFQ capture + first 48-hour flow report

Status: **IMPLEMENTED LOCALLY / NOT DEPLOYED**. Production enable/start waits
for the next operator maintenance window alongside W03. This document does not
authorize that deployment.

## What this adds

```text
kalshi-rfq-capture.service (independent)
  → tools/rfq_capture.py (own lock/backoff/disable flag)
    → one persistent build/ws_shadow process + second authenticated WS connection
      → channels=[communications], no market filter
        → RawLogWriter partitions by TL1 recv_wall_ns
          → work/raw/date=<UTC-D>/rfq_<HH>.ndjson
```

The socket is **not** restarted at each hour. The recorder stays connected and
switches files on the local receive clock using `{UTC_DATE}`/`{UTC_HOUR}` path
tokens. Its single writer emits a durable `hour_open` marker after draining the
current queue. The supervisor then waits for the new marker plus two seconds of
old-file size stability before hashing an hourly receipt. Any later append
causes the receipt SHA to disagree with the final day seal, so that hour cannot
silently pass. The child has a 30-day safety lifetime; any reconnect or process
restart invalidates the affected hour because RFQ broadcasts provide no replay
or mandatory sequence proof.

The recorder is passive. It never creates an RFQ, posts a quote, accepts a
quote, or places an order. `rfq_capture.py` has no communications REST client;
it forces `KALSHI_MODE=data_collect`, removes `KALSHI_ALLOW_LIVE`, disables the
REST cross-check, omits the orderbook-only `use_yes_price` subscription field,
and then delegates to `ws_shadow`, which independently refuses
`Mode::Live` and asserts `can_place_orders(rt)==false`.

Credential capability is also removed: deployment requires a dedicated Kalshi
API key created with `scopes=["read"]`, stored only in
`~/.kalshi/rfq_readonly.env.sh` (mode 0600) with
`KALSHI_API_KEY_SCOPE=read`. Kalshi's official API-key reference says omitted
scopes default to full `read+write`; therefore the shared production key is not
accepted for this service. This is checked at startup in addition to the two
code-level refusals.

One-touch disable:

```bash
touch /home/ubuntu/hft-bot/work/live/rfq_disable
```

The supervisor notices the flag during a running segment and terminates only
its RFQ child. Removing the flag allows the independent service to resume. The
flag never stops or restarts `kalshi-pipeline`.

## Official field contract (fixed-point evidence)

Implementation was checked against Kalshi's official references, not memory:

- [RFQ guide](https://docs.kalshi.com/getting_started/rfqs)
- [Communications WebSocket](https://docs.kalshi.com/websockets/communications)
- vendored official `docs/vendor/kalshi/latest/asyncapi.yaml`, SHA-256
  `00858d5a892eb7066a8da247660622721e8ee80bfdd89b1b9a24278c15d7d421`

`communications` requires authentication; market specification is ignored.
`rfq_created` and `rfq_deleted` are broadcast to every subscriber. Account-
private `quote_*` messages may also arrive if this account is involved; raw
capture preserves every inbound text frame, while the flow report analyzes
only the two broadcast `rfq_*` types.

Exact current payload fields:

| event | required | optional |
|---|---|---|
| `rfq_created` | `id`, `creator_id`, `market_ticker`, `created_ts` | `event_ticker`, `contracts_fp`, `target_cost_dollars`, `mve_collection_ticker`, `mve_selected_legs` |
| `rfq_deleted` | `id`, `creator_id`, `market_ticker`, `deleted_ts` | `event_ticker`, `contracts_fp`, `target_cost_dollars` |

Each selected leg may contain `event_ticker`, `market_ticker`, `side`, and
`yes_settlement_value_dollars`. The AsyncAPI does not declare combo fields on
`rfq_deleted`, so the report never requires them there. It also does not make a
per-event `seq` mandatory, so no report may claim sequence-proven completeness.

The official reference explicitly says `rfq_created.creator_id` is currently
empty. Requester analysis therefore joins the first `rfq_deleted` event back to
the create by `id`, reports known-ID coverage/right-censoring, excludes empty
IDs, and hashes displayed requester IDs. Raw retains the original public
anonymous ID verbatim as requested.

## Capture-continuity statement

RFQ capture cannot block the existing firehose by construction:

- separate systemd unit, process tree, WebSocket connection, lock, raw file,
  metrics and logs;
- no `PartOf=` / `Requires=` relationship with `kalshi-pipeline`;
- no firehose PID discovery or signals in `rfq_capture.py`;
- the RFQ unit is resource-secondary (`OOMScoreAdjust=+300`, low CPU/IO
  weight, 512 MiB hard memory cap); `oom_guard.sh` distinguishes its cgroup so
  only the primary firehose `ws_shadow` receives `-1000` OOM protection;
- RFQ child failures write `work/live/rfq_alert.json` and retry only inside the
  RFQ service;
- `work/live/rfq_disable` affects only the RFQ child;
- primary `tools/freshness.py` now considers only `firehose_*.ndjson*`, so a
  fresh low-rate RFQ file cannot hide a dead firehose;
- S3 sync excludes active `firehose_<HH>`, `rfq_<HH>` and
  `rfq_receipts_<HH>` files, preventing torn copies; completed RFQ hours upload
  normally; daily sync also copies `warehouse/seals`, so a downloaded vault
  tree contains the proof files required by the report;
- the existing ingest/seal `*.ndjson*` discovery checkpoints and seals this
  raw family automatically. Unknown RFQ frames create no market fact rows;
  report parsing remains directly over sealed raw.

Low RFQ traffic is not itself a health failure. Connection health comes from
the dedicated per-second `rfq_metrics.ndjson` feed heartbeat (Ping/Pong-backed),
the authenticated subscribe ACK for the persistent connection, recorder
drops/write failures/invalid connected state, per-hour close receipts and the
RFQ alert file. A capture-path write failure makes `ws_shadow` exit nonzero;
`rfq_capture.py` also promotes loss/overflow/reconnect/disconnect/error or
unexpected `unsubscribed` evidence. Close/Error callbacks cannot masquerade as
liveness: feed health requires an explicitly open transport and each counter
delta also emits a receive-clock `transport_close`/`transport_error` raw marker,
so even a failure in the last millisecond of an hour invalidates that exact
partition. A fresh RFQ file never substitutes for
primary-firehose health.
An open/ping-active socket is still `RUNNING_UNPROVEN` until the exact
`communications` subscribe ACK is captured; no ACK within 30 seconds terminates
and retries only the RFQ child instead of waiting until the end of the hour.
`rfq_alert.json` is deliberately latched: a later healthy hour may update
`rfq_state.json`, but it never erases evidence that the current 48-hour cohort
was broken. The operator clears the alert only after acknowledging the gap and
freezing a new T0; sealed receipts retain the permanent history.

## Deployment boundary (not executed here)

The W03 implementation must land first so its channel-family inventory and
pause ownership are authoritative. Then, in the same approved maintenance
window:

1. rebase/cherry-pick this change onto the W03 production branch;
2. run the offline full suite and production-package diff;
3. install `deploy/kalshi-rfq-capture.service` but do not couple it to the
   firehose unit;
4. create/verify the dedicated `scopes=["read"]` API key and its 0600 env file;
5. install the S3-sync/freshness changes;
6. start the RFQ service, verify one authenticated `subscribed` frame, one
   persistent child PID, `transmitted=0`, recorder drops/write failures=0 and
   firehose PID/raw growth unchanged;
7. freeze `T0` to the first full UTC-hour boundary and collect 48 consecutive
   receipt-backed hourly partitions. The deployment's initial partial hour is
   retained and labelled `PARTIAL_START`, but is not part of the 48-hour cohort.

Rollback is one touch (`rfq_disable`) or stopping only
`kalshi-rfq-capture.service`; neither action stops the firehose.

## First 48-hour report

After 48 complete hourly files are sealed, run on the host or on a local copy
of the S3 sealed raw tree:

```bash
python3 tools/research/rfq_flow_report.py \
  --raw-root work/raw \
  --warehouse-root work/warehouse \
  --start <T0-RFC3339> \
  --hours 48 \
  --out work/research/rfq_flow_48h
```

The command refuses an incomplete 48-hour window unless explicitly invoked
with `--allow-incomplete`, which produces a diagnostic—not a complete report.
Each included hour needs its RFQ file, `hour_open`, exactly one PASS receipt
with continuous subscription proof and no reconnect, and byte size/SHA equality
between the receipt, the closed file and a `full_v2` day seal. Outputs are
`rfq_flow_report.json` and a single-file offline `index.html`.

Mandatory exploratory views:

1. unique requests/day by category;
2. `contracts_fp` distribution (official E2 count, separate population);
3. `target_cost_dollars` distribution (official E6 dollars, separate population);
4. combo vs single ratio;
5. repeat-requester concentration (known static IDs only; display hashes);
6. HVM share lower bound;
7. created→deleted coverage/lifetime;
8. hourly capture coverage and DQ ledger.

HVM limitation is explicit: Kalshi says every combo is HVM, but the
communications payload and current Market schema expose no general `is_hvm`
field. Therefore `combo share` is a **known-HVM lower bound**; exact HVM share
is `null`, and non-combo HVM status remains `UNKNOWN`.

Every output is bannered `EXPLORATORY — NOT A TRADING GATE`. The first window
describes flow and generates later falsifiable hypotheses; it does not prove
stability, edge, fillability or profitability.
