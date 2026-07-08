# W-C1 AUDIT — force-reconnect watchdog (2026-07-07)

**Subject:** commit 06db331 (force_reconnect + ws_shadow watchdog wiring +
red-first test). **Verdict: NO BLOCKING DEFECTS.**

**Independence caveat (read this):** the mandatory *independent fresh-context*
audit was attempted TWICE via a separate agent; both runs died on an
infrastructure API error ("Connection closed mid-response"), each partway
through the ixwebsocket-semantics check. What follows is a rigorous in-context
adversarial self-audit that traced the real library source. It is NOT a
substitute for the independent pass — **the independent-agent audit MUST be
re-run and clear before the W-C3 deploy** (W-C1 is committed but NOT deployed;
the live pipeline still runs the approved pre-W-C1 binary, so nothing is at risk
in the meantime).

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
