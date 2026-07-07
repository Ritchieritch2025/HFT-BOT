# CAPTURE DIAGNOSIS — 2026-07-07 (W-C0, read-only)

**Scope:** answer W-C0's four questions with cited evidence; name the ONE root
cause; no code/config/binary/supervisor touched. Analysis performed
2026-07-07 ~20:30–20:40 UTC.

**Headline:** the diagnosis caught a **gap in progress**. At the time of writing
the live feed had been dead for **30 minutes** (last raw record 20:07:30 UTC,
now 20:37:45 UTC) with `reconnects=0`, and it will only recover at the 21:00 UTC
hourly respawn. This is the exact failure the plan set out to explain, observed
live rather than reconstructed from history.

---

## Gap window analyzed
- **Live window: 2026-07-07 20:07:30 UTC → (ongoing, ≥ 20:37:45 UTC).**
  Chosen because it is happening now and every artifact (raw, log, process) is
  fresh and self-consistent. Historical windows in SESSION_LOG (13:39–14:00,
  14:48–15:00) show the identical signature; this one is directly observable.

---

## Q1 — CAPTURE or INGEST?  → **CAPTURE** (raw itself has the hole)
- Raw is the source of truth (D1). The active raw file
  `work/raw/date=2026-07-07/firehose_20.ndjson.1` **stops at 20:07:30.017 UTC**:
  - last parseable record `recv_wall_ns = …` → **2026-07-07T20:07:30.017Z**
    (34,865 records parsed; last complete one at that time).
  - file **mtime frozen at 20:07:30** — no bytes written for ≥30 min.
- So the hole is in RAW, before ingest ever sees it. This is **not** ingest lag
  (that would be dense raw + a lagging staging tail). CAPTURE.
- Cross-check: ws_shadow's own `events` counter is frozen (below), i.e. the
  collector received zero frames — consistent with a raw hole, not a staging
  delay.

## Q2 — Right BINARY? (does the running ws_shadow contain STEP 0?)  → **YES**
- STEP 0 commit `6d56aae` ("re-sign WS auth headers before every reconnect")
  dated **2026-07-07 08:29:38 -0400**.
- Running binary `build/ws_shadow` **mtime 2026-07-07 09:18 EDT** — built AFTER
  the STEP 0 commit. `refresh_auth()` is present in `src/ws_client.cpp` (L101)
  and wired into `start()`, `on_close()`, and the transport-Error branch.
- Running process **PID 2394 started 16:00:01 EDT** (the hourly respawn) from
  that binary; single instance, supervised by `pipeline_supervisor.sh`.
- **Conclusion: STEP 0 IS in production. STEP 0 is not the bug** — and it is not
  the fix for this failure mode either (see root cause).

## Q3 — Does reconnect FIRE during the gap?  → **NO** (counters stay FLAT)
- `work/live/ws_shadow.log`, current session (epoch 1): across the entire dead
  window the counter line is frozen —
  `events=416253 deltas=0 reconnects=0 errors=0 overflow=0 epoch=1`
  repeated verbatim (77+ identical lines at ~10s cadence). `events` does not
  advance one tick; `reconnects` and `errors` never leave 0; `epoch` never
  leaves 1.
- Contrast (proves the reconnect path CAN work when exercised): an **earlier**
  session in the same log shows `reconnects=1 … epoch=2` with `events` climbing
  — i.e. when ixwebsocket actually delivers a Close/Error, STEP 0 re-signs and
  the socket revives. It simply never gets the chance here.
- **Interpretation:** the socket is **wedged / half-open** — TCP silently dead,
  no FIN/RST, so ixwebsocket delivers no `Close` and no `Error` event, believes
  it is still connected, and **never attempts a reconnect**. Because no
  reconnect is attempted, STEP 0's re-sign (which only runs from `on_close()` and
  the Error branch, `src/ws_client.cpp` L130/L150) is never entered. Reconnect
  does not fire.

## Q4 — Does the ping-silence WATCHDOG do anything?  → **NO** (counts only)
- `apps/ws_shadow.cpp` L483–487:
  ```cpp
  // Ping-silence watchdog (I6). Count each transition into silence as a
  // missed-pong incident; ixwebsocket owns the actual reconnect+backoff.
  const bool silent = client.ping_silent(now_ms);
  if (silent && !was_silent) ++missed_pong_disconnects;
  was_silent = silent;
  ```
  The watchdog **only increments `missed_pong_disconnects`**. It never calls
  `client.stop()`/`client.start()` or otherwise forces recovery. The comment
  explicitly delegates recovery to "ixwebsocket … reconnect+backoff" — which, per
  Q3, never fires for a wedged socket.
- `ping_silent` (`include/kalshi/ws_client.hpp` L96) returns true after **30 s**
  of inbound silence. So the process *knows within 30 s* that the socket is dead
  — and does nothing about it. **That inaction IS the gap** (exactly the Q4
  hypothesis).

---

## ROOT CAUSE (the ONE)
**A silently-wedged (half-open) WebSocket produces no Close/Error event, so
ixwebsocket never attempts a reconnect; STEP 0's re-sign only runs on a
reconnect attempt, so it never runs; and the ping-silence watchdog detects the
30 s silence but only counts it instead of forcing a reconnect. Nothing recovers
the feed until the top-of-hour respawn (SIGTERM → fresh `start()`).**

This is consistent with every observed symptom: market-wide silence (the whole
socket, not one market), `reconnects=0` throughout, and recovery *exactly* at
:00. STEP 0 was a real fix for a different mode (401 relockout *on* reconnect)
and remains correct — it just cannot help a socket that never reconnects.

## Decision → which W-C1 variant to build
Build the plan's **PRIMARY** design, now confirmed by evidence, not hypothesis:
**wire the ping-silence watchdog to FORCE a reconnect** — after N s of inbound
silence, `client.stop()` + `client.start()` (start() re-signs fresh headers via
`refresh_auth()`), with bounded backoff and a logged recovery marker. This is
independent of ixwebsocket's internal auto-reconnect (which the wedge defeats)
and is fail-closed (S2): uncertain liveness → restart the connection.
- Red-first test (W-C1 acceptance): MockWebSocketTransport goes silent past the
  watchdog timeout ⇒ client issues a fresh `start()` carrying a NEWER signature
  timestamp and resumes; remove the wiring ⇒ test fails (silence never
  recovers).
- Suggested timeout: the watchdog already trips at 30 s silence; force the
  reconnect a short margin after that (e.g. 30–45 s) so a real drop recovers in
  << 1 min vs today's up-to-60-min wait.

## Acceptance check (W-C0)
- Q1–Q4 answered yes/no with cited raw/log/source evidence. ✅
- ONE root cause named. ✅
- No files changed outside `docs/` — this doc is the only write; `git status`
  shows only pre-existing pipeline-generated churn
  (`config/classification_review.csv`, `config/series_tags_report.csv`, catalog
  job outputs) + untracked `sandbox/discovery/*`, none touched by W-C0. ✅
- Rollback: n/a (read-only).

## OPERATIONAL FLAG (not part of W-C0 scope — operator's call)
A gap is **live right now**. Read-only diagnosis does not permit me to touch
prod, so I did not. If you want early recovery instead of waiting for 21:00 UTC,
the manual action is `kill -TERM 2394` (the ws_shadow PID) — the supervisor
respawns a fresh `start()` and capture resumes immediately. That is a production
action and your decision, not mine.
