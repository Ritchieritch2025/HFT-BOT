# W-C1 AUDIT — force-reconnect watchdog (2026-07-07)

**Subject:** commit 06db331 (force_reconnect + ws_shadow watchdog wiring +
red-first test). **Verdict: NO BLOCKING DEFECTS.**

**Independence status:** the first TWO independent-agent runs died on an infra
API error; the THIRD completed and CLEARED the change — **verdict: no blocking
defects**, confirming this in-context self-audit. It independently verified the
stop()/start() restart safety (and found the exact bound that makes join()
non-hanging: `kClosingMaximumWaitingDelayInMs = 300ms`,
IXWebSocketTransport.cpp:58 — a wedged socket's CLOSING is force-completed within
300ms). It raised 3 non-blocking findings; their remediation is below.

## Independent-audit findings + remediation (2026-07-07)
- **#1 [FIXED] `last_activity_ms_` non-atomic data race.** Written on the
  transport thread, read on the watchdog thread to decide teardown — a formal
  C++ data race (UB), pre-existing but escalated by W-C1 to load-bearing. FIXED:
  made `std::atomic<std::int64_t>` with relaxed load/store (ws_client.hpp,
  ws_client.cpp). make check GREEN after. The remaining cross-thread reads of
  `reconnects_/epoch_` are logging-only and left as-is (benign, pre-existing).
- **#2 [RESOLVED by measurement — the deploy gate] 20s threshold vs a healthy
  quiet socket.** The risk: if Kalshi emits no frame for ≥20s during a genuine
  all-market lull, the watchdog would false-teardown a healthy socket. RESOLVED
  with our own data (Q4): over a healthy hour (firehose_23, 134,100 records) the
  **max inter-record silence excluding real holes is 0.33s** (p99.99 ≈ 0.17s) —
  the firehose is sub-second-continuous when alive, so 20s of silence is
  unambiguously a dead socket (~60× margin). The only >5s gaps in that hour were
  three ~903s (15-min) holes = the wedge itself (recovered late by an eventual
  TCP-level error, ~15-min Kalshi LB idle-timeout, instead of at :00 — same root
  cause W-C1 fixes). 20s stays; no false-teardown risk in production.
- **#3 [ACCEPTED] cold-start wedge uncovered.** If the INITIAL connect wedges
  before any frame, `last_activity_ms_==0` so the watchdog never fires; the
  hourly respawn bounds it. Coverage gap, not a regression (the incident is a
  mid-run wedge). BACKLOG: optionally fire on "no Open within N s of start()".
- nits #4/#5 (pre-bump values in the post-force log line; two non-discriminating
  test asserts) noted, not fixed — cosmetic, do not weaken the red-first proof.

**Deploy status:** the independent audit now CLEARS W-C1. Deploy remains the
separate operator-gated W-C3; the live pipeline still runs the approved pre-W-C1
binary (sha 3c389ae) until the operator says go.

---
(original in-context self-audit follows)

## What was verified SOUND (with evidence)
1. **Real-transport restart is safe.** `ix::WebSocket::stop()` (IXWebSocket.cpp
   L187) calls `close()` FIRST, which `wakeUpFromPoll(kCloseRequest)`s the
   transport's poll loop; the loop is timeout-bounded (`isReadyToRead(timeout)`,
   IXWebSocketTransport.cpp L363), so even a wedged/half-open socket wakes and
   exits, and `_thread.join()` (L197) returns promptly — no hang. `start()`
   (L182) guards on `_thread.joinable()`; after a join the thread is
   non-joinable, so `start()` spawns a fresh run() thread. **stop()->start()
   mid-run is a supported restart.**
2. **No new data race / no deadlock.** `force_reconnect()` runs on the ws_shadow
   MAIN thread. `t_.stop()`'s `join()` is a hard barrier: the transport thread
   is fully gone before `refresh_auth()`/`t_.start()` run, so those never race a
   concurrent `on_message`. Any `Close` delivered during `stop()` runs
   `on_close()->refresh_auth()` on the transport thread BEFORE join returns, i.e.
   serialized with the main thread's `refresh_auth()`, not concurrent. After
   `start()`, the new transport thread's `on_open` runs concurrently with the
   main loop — but that is the pre-existing steady-state concurrency, unchanged.
   The cross-thread reads of `last_activity_ms_`/counters by the watchdog are a
   PRE-EXISTING benign non-atomic race (a stale read delays detection ≤1 tick);
   W-C1 does not introduce it.
3. **State survives the restart.** `_url`, `_extraHeaders`, and the onMessage
   callback are ixwebsocket members that persist across stop()/start(); our
   `IxWebSocketTransport::start()` also re-registers the callback every time, and
   `refresh_auth()` re-sets fresh headers before `t_.start()`. The reopened
   socket authenticates fresh and resubscribes via `on_open()->resubscribe()`.
4. **Fail-closed retry loop.** On a still-broken reopen, `last_activity_ms_`
   stays frozen (no frames), `ping_silent(now_ms, 20s)` stays true, and after the
   15s backoff the watchdog forces again — bounded, repeating, never silently
   continues (S2). Units are consistent: `now_ms` and `last_activity_ms_` are
   both `trading::wall_ns()/1e6` (ms); thresholds are ms.
5. **Observability (D2).** A successful forced reconnect bumps the epoch, so the
   recorder writes an `epoch_change` marker into the raw capture; each attempt
   also logs to stderr and emits a `watchdog_reconnect` system event
   (`write_system_event` is null-safe: `if (!f) return;`); the periodic counter
   line now carries `forced=`.
6. **Red-first test is valid.** With `force_reconnect` neutered to a no-op, the
   recovery asserts (NEWER signature timestamp, reconnect+epoch bump,
   resubscribe) FAIL; restored, all pass. `make check` + `run_pipeline.sh` green,
   and `ws_shadow_mock` exercised the REAL W-C1 binary against a mock server.

## NON-BLOCKING notes (operator judgment, not defects)
- **N1 — 20s force threshold vs quiet windows.** On an ALL-markets firehose, 20s
  of ZERO frames is anomalous, but a genuinely dead-quiet overnight window could
  trip a spurious force_reconnect (~1-3s reopen gap + one artificial epoch bump).
  This is logged/counted (`forced=`), and the tradeoff (rare ~2s gap vs
  eliminating 20-60min holes) is strongly net-positive. Tunable to 30-45s if the
  `forced=` counter shows spurious reconnects in production. Chosen 20s to
  prioritize the operator's fast-recovery ask.
- **N2 — legacy `missed_pong_disconnects` may undercount.** A 20s force fires
  before the 30s silence transition, so that legacy counter may not increment on
  a wedge the watchdog recovers first. Cosmetic; the meaningful signal is now
  `forced=` + the epoch markers.
- **N3 — weak asserts in the red test.** With the fix neutered, "reopened the
  transport" and "stream is live again" still passed (the mock stayed open / a
  later inject refreshed liveness). They are not load-bearing; the three recovery
  asserts that DO fail are sufficient to make the test genuinely red-first.

## Test + deploy gates
- make check: GREEN. tests/run_pipeline.sh: PIPELINE PASS (ws_shadow_mock incl.).
- NOT deployed: build/ws_shadow restored to the approved pre-W-C1 binary
  (sha 3c389ae). Deploy is the separate operator-gated W-C3.
- **Gate before W-C3:** re-run the independent-agent adversarial audit; address
  anything it finds beyond this self-audit.
