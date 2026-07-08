# CAPTURE-GAP TAXONOMY (2026-07-08) — input to the W-C5 fix session

Classifies the recorded capture gaps by RECOVERY MECHANISM to establish which the
current W-C1 watchdog can and cannot handle, and to settle whether a
**data-frame-silence trigger** is actually needed. Result: it is — **proven, not
hypothesised.** Read-only diagnosis; no production code touched here.

## Method (operator full-repo scan, 2026-07-08)
For each of the **62** recorded gaps, the criterion is: **is there a `subscribed`
control-channel record within 60 s AFTER the gap's end?**
- **YES ⇒ RECONNECT-RECOVERY class.** The feed came back because the connection
  was re-established → fresh `Open` → `resubscribe()` → a `subscribed` ack. **58 / 62.**
- **NO ⇒ SAME-CONNECTION DATA-DROPOUT class.** Market data resumed on the SAME
  live connection — no reconnect, no resubscribe, no `subscribed` ack. **4 / 62.**

## The 4 same-connection data-dropout cases (verified against capture_gaps.csv, UTC)
| Case | Window (UTC) | Duration | Note |
|---|---|---|---|
| 1 | 2026-07-06 12:26:34 → 12:27:36 | **63 s** | immediately followed by 12:27:38→13:00:16 (32.6 min) reconnect-recovery wedge |
| 2 | 2026-07-06 14:48:18 → 14:49:20 | **62 s** | immediately followed by 14:49:21→15:00:16 (10.9 min) reconnect-recovery wedge |
| 3 | 2026-07-07 17:29:07 → 17:51:48 | **22.7 min** | evening; long same-connection silence, recovered WITHOUT reconnect — the heaviest sample |
| 4 | 2026-07-07 23:55:39 → 00:00:00 | **4.3 min** | abuts midnight |

Two signals in the data:
- **Cases 1 & 2 are each ~62 s and each precede a larger wedge** — a brief
  data-dropout that then escalates into a full connection wedge. The near-identical
  ~62 s duration hints at a **periodic / TTL-like cause** (a subscription lease
  expiring, a server refresh cycle) rather than a random drop.
- **Case 3 (22.7 min) recovered on the same connection**, not at a top-of-hour
  respawn — i.e. the data genuinely came back by itself while the socket stayed up.

## What these 4 prove (hypothesis → fact)
The current W-C1 watchdog measures **ANY-frame silence**: `last_activity_ms_` is
bumped by EVERY inbound frame, including Kalshi's keepalive **WS ping/pong**
(`src/ws_client.cpp:114`, updated before the type switch; transport auto-pongs).
On all 4 cases the connection stayed alive (pings flowing) while market DATA
stopped — so `ping_silent(20s)` never tripped, and (for cases 1–4) recovery didn't
even come from our reconnect. **The any-frame watchdog is therefore totally blind
to data holes on a live connection.** This is no longer a hypothesis — the scan
exhibits 4 live-connection data holes it could not have seen. The
**data-frame-silence trigger is a proven requirement.**

(Corroborating the reconnect-recovery class: the ws_shadow.log evidence already
shows the watchdog firing after ~901 s — "WATCHDOG forced reconnect after 901821ms
inbound silence" — i.e. only when the whole connection finally dies and pings stop,
which is why the 58 reconnect-recovery gaps are ~15-min holes instead of ~20 s.)

## Root cause (code-level)
1. **Trigger is ping-masked.** `last_activity_ms_` (the watchdog clock) is updated
   for all frame types incl. Ping/Pong ⇒ `ping_silent(20s)` cannot detect a
   data-only stall while keepalives flow.
2. **We only (re)subscribe on a fresh `Open`.** `on_open()→resubscribe()`
   (`ws_client.cpp:161,172`) sends the subscribe synchronously on connect — but
   there is NO mechanism to re-assert the subscription on an already-open
   connection. So a server-side subscription lapse (connection alive, data gone)
   has no recovery path short of the connection fully dying.

## The unifying fix (W-C5 — production, its own full-discipline session)
Introduce a **`last_data_ms_`** updated ONLY on market-data **Text** frames (never
Ping/Pong). Watchdog on DATA silence (≈20 s), two-stage:
1. **First, re-send the subscribe on the LIVE connection** (`resubscribe()` with no
   teardown). Cheap; directly fixes the **same-connection data-dropout class**
   (cases 1–4) and tests the subscription-lapse hypothesis.
2. **If data does not resume within a few seconds, escalate to a full reconnect**
   (the existing `force_reconnect()`), which fixes the **reconnect-recovery class**
   (the 58) — now triggered at ~20 s of DATA silence instead of ~15 min.
This single change handles BOTH taxonomy classes. Keep the existing behaviour on
the healthy path (data flowing ⇒ neither stage fires). Red-first test must inject
Ping/Pong-only frames and prove the OLD any-frame watchdog stays silent while the
NEW data-silence watchdog fires (the exact gap the mocks missed before).

## Autopsy TODOs for the W-C5 session
- **Case 3 (07-07 17:29→17:51):** isolate ws_shadow.log for that window
  (471 transport errors exist across all sessions — attribute them by time) and
  cross-reference Kalshi's status page for 17:29–17:51 UTC — confirm it was a
  subscription lapse on our side vs any exchange-published incident.
- **Cases 1 & 2 (~62 s each):** test the periodicity hypothesis — is there a
  ~fixed subscription lease / server refresh interval? If so, a proactive periodic
  re-subscribe (below any lease TTL) may prevent the dropout entirely, upstream of
  the watchdog.

---

## APPENDIX — pmset alignment: the DOMINANT cause is Mac SLEEP (2026-07-08, read-only)

Operator hypothesis (cheaper than lease-TTL): the ~62s cases are precursors of a
Mac Deep-Idle sleep cascade (throttle → data stops → TCP freezes → wedge → wake
revival). Tested against `pmset -g log` (Mac local time = EDT, UTC−4). **Confirmed
— and it explains far more than cases 1/2.**

### Case-by-case alignment (gap in EDT ↔ actual sleep/wake events)
| Case | Gap (EDT) | pmset events | Verdict |
|---|---|---|---|
| 1 | 08:26:34→08:27:36 | `08:26:32 Sleep` → `08:27:11 Wake` | **SLEEP** (feed ~25s to resume post-wake) |
| 2 | 10:48:18→10:49:20 | `10:48:16 Sleep` → `10:48:51 Wake` | **SLEEP** |
| 3 | 13:29:07→13:51:48 | `13:29:05 Sleep` → (brief Wake) → `13:43:09 Sleep` → `13:44:24 Sleep` → `13:51:42 Wake` | **SLEEP cascade** spanning the whole 22.7 min |

### The 15-min "wedge" pattern IS the DarkWake cycle
The 07-08 05:13→09:59 EDT catastrophe (12 back-to-back ~15-min gaps) aligns 1:1
with a `Sleep → DarkWake` cascade every ~15 min:
`05:13:22 Sleep→05:28:25 DarkWake · 05:29:10 Sleep→05:44:13 DarkWake · 05:44:58
Sleep→06:00:01 DarkWake · …`. Each ~15-min gap = one Deep-Idle sleep between
DarkWakes. (07-08 alone: 50 sleep/wake transitions.)

### Re-attribution + hypothesis outcomes
- **Cases 1, 2, 3 → re-attributed to "MAC SLEEP CASCADE."** Not subscription
  lapse, not exchange-side.
- **Lease-TTL hypothesis: DISPROVEN / downgraded.** The ~62s "periodicity" is the
  sleep/wake cycle duration (~35–40s asleep + ~25s feed-resume), not a server lease.
- The ping-masked any-frame-watchdog finding (main body) **still holds and is still
  a real bug**, but it is now **secondary**: even a perfect data-silence watchdog
  cannot force a reconnect while the machine is ASLEEP — the CPU isn't running the
  watchdog loop. It only helps in the ~25s post-wake resume.

### Root cause (definitive) + fix priority
**PRIMARY root cause: the Mac enters Deep-Idle sleep whenever idle, freezing the
WS feed. A laptop is not 24/7 infrastructure.** This is the empirical answer to
"why isn't capture 24/7."
1. **Immediate (operator, system-level — agent cannot change power settings):**
   hold a power assertion while the pipeline runs — `caffeinate -dimsu` (or the
   launchd job / supervisor wraps ws_shadow in `caffeinate -s`, or `sudo pmset -c
   disablesleep 1` on AC). This alone should eliminate the great majority of gaps
   TODAY, before AWS.
2. **Permanent:** MASTER_SEQUENCE STEP 1 (AWS/EC2 migration) — a always-on Linux
   host that never sleeps. This finding is the concrete justification for it.
3. **Still worth doing (defense-in-depth, secondary):** the data-frame-silence
   watchdog + live re-subscribe (main body) — for genuine connection-alive data
   loss once sleep is off the table, and for faster post-wake recovery.

### Follow-up
- Case 4 (07-07 23:55:39→00:00:00 UTC = 19:55 EDT) not yet pmset-checked; given
  the overwhelming pattern it is almost certainly another sleep — verify in the
  W-C5 session for completeness.
