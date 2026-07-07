# PLAN_CAPTURE_HARDENING — stop the capture gaps for good

**Problem (2026-07-07, SESSION_LOG 20:15).** The 24/7 capture still drops the
WS feed intermittently and only recovers at the **top of the hour** (the ws_shadow
hourly respawn). Confirmed gaps: 07-06 12:26–13:00 & 13:39–14:00; 07-07 morning
+ **post-STEP-0** 13:39–14:00 & 14:48–15:00. Data VALUES are byte-exact and
V-EP15 flags gap-overlapping events `degraded` (backtest is protected), but the
holes land in the highest-vol minutes (settlement-convergence). **STEP 0
(reconnect re-sign, 6d56aae) is NOT confirmed to work in production** — the
top-of-hour-only recovery pattern says a mid-hour drop stays dead until the
hourly restart, i.e. reconnect is not actually reviving the socket.

- **Phase:** 1 (data substrate). Satisfies no new gate; it PROTECTS every gate
  (a holed tape corrupts the Q2 bound).
- **Safety class:** CAPTURE-LAYER change to the live 24/7 pipeline — the highest
  P4 stakes in the project. Diagnosis is read-only; the one code change ships
  red-first + independently audited; deploy is zero-gap + monitored + rollback-ready.
- **Governing rules (bind every W):** (1) **diagnose before you touch prod** —
  W-C0 changes nothing; (2) one W per fresh session, independent audit after each;
  (3) every capture-path change ships a red-first test in the same commit (D4/E1);
  (4) never break capture continuity — each W states how; (5) fail-closed (S2):
  an uncertain recovery restarts the connection, never silently continues.

---

## W-C0 — DIAGNOSIS (read-only; NO production change)
Purpose:          Establish the ACTUAL failure mechanism before changing code.
                  Answer four questions with evidence, not hypothesis.
Blocked by:       none.
Allowed reads:    work/live/ws_shadow.log; work/metrics.ndjson; work/raw/date=*/
                  firehose_* (inter-record gaps); apps/ws_shadow.cpp; src/
                  ws_client.cpp; src/ix_transport.cpp; tools/pipeline_supervisor.sh;
                  the running process table + build/ws_shadow mtime.
Allowed writes:   docs/plan_audits/capture_diagnosis_<date>.md (findings only);
                  a throwaway read-only analysis script under tools/ (or scratch).
Forbidden writes: ANY capture/ingest/export code; the supervisor; config; the binary.
The four questions (each needs a yes/no + evidence):
  Q1 CAPTURE or INGEST? For a known gap window (e.g. 07-07 13:39–14:00), is the
     gap present in RAW (work/raw firehose inter-record gap > 5 min across all
     markets) — if yes it is CAPTURE; if raw is dense but staging has the hole it
     is INGEST-lag (different fix). Cross-check work/metrics.ndjson feed-status
     freshness over the window.
  Q2 Right BINARY? Did the ws_shadow session covering the gap start from a build
     that contains refresh_auth? Check each hourly respawn's start time vs
     build/ws_shadow mtime; grep the log for the epoch/reconnect counters.
  Q3 Does reconnect FIRE? In ws_shadow.log across a gap: do `reconnects`/`errors`
     climb during the dead window (ixwebsocket is trying, re-sign failing) or stay
     FLAT (ixwebsocket not retrying / socket wedged, only the :00 respawn revives)?
     This single fact picks the fix.
  Q4 Does the ping-silence WATCHDOG do anything? Read apps/ws_shadow.cpp ~L483:
     confirm it only COUNTS silence (missed_pong_disconnects++) and never forces a
     reconnect. If so, that inaction IS the gap (nothing recovers a wedged socket
     until :00).
Acceptance:       capture_diagnosis_<date>.md answers Q1–Q4 with cited log/raw
                  evidence and names the ONE root cause; no files outside docs/
                  changed; `git status` clean except the diagnosis doc.
Rollback:         n/a (read-only).
Exit evidence:    the diagnosis doc; the specific gap window analyzed; the decision
                  of which W-C1 variant to build.

## W-C1 — ROOT-CAUSE FIX (belt-and-suspenders reconnect) + red-first test
Purpose:          Make a mid-hour WS death recover in << 1 min regardless of why
                  ixwebsocket's internal reconnect failed — do not depend on the
                  hourly respawn. Primary design (unless W-C0 says otherwise):
                  wire the ping-silence watchdog to FORCE a reconnect — after N s
                  of inbound silence, client.stop()+client.start() (start()
                  re-signs fresh headers via refresh_auth), bounded backoff, logged.
                  This is independent of ixwebsocket's auto-reconnect path (which
                  the mock test can't fully exercise) and fail-closed.
Blocked by:       W-C0 (root cause) + operator approval (capture-layer change).
Allowed reads:    apps/ws_shadow.cpp; include/kalshi/ws_client.hpp; src/ws_client.cpp;
                  tests/test_ws_client.cpp; the W-C0 doc.
Allowed writes:   the minimal watchdog/reconnect wiring (ws_shadow.cpp and/or a
                  ws_client force_reconnect()); tests/test_ws_client.cpp (red-first);
                  a quality_log entry for the gaps in the same commit.
Forbidden writes: subscribe/record/decode/rotation paths (the healthy-stream path
                  stays byte-for-byte unchanged); export/ingest.
Acceptance:       RED-FIRST MockWebSocketTransport test: simulate inbound silence
                  past the watchdog timeout ⇒ the client issues a fresh
                  start()/reconnect carrying a NEWER signature timestamp and
                  RESUMES; with the wiring removed the test FAILS (silence never
                  recovers). make check + tests/run_pipeline.sh green. P4 statement
                  in the commit: healthy path untouched; the change only adds a
                  recovery action on silence.
Rollback:         revert commit; rebuild ws_shadow; the prior binary stays valid.
Exit evidence:    commit; red→green test tail; the P4/continuity statement.

## W-C2 — GAP DETECTION + ALERT + durable capture-gap record
Purpose:          Never discover a gap by eyeballing a chart again. A supervised
                  detector scans raw-feed inter-record gaps + metrics freshness,
                  writes a STRUCTURED capture-gap record (start_us,end_us), ALERTS
                  on a live gap, and becomes V-EP15's authoritative source
                  (replaces the coarse quality_log parser — BACKLOG).
Blocked by:       W-C1 (fix first, then instrument) — or parallel, operator's call.
Allowed reads:    work/raw; work/metrics.ndjson; work/quality_log.ndjson;
                  tools/event_validate.py (gaps consumer).
Allowed writes:   tools/capture_gaps.py (builder+detector); its test + fixtures;
                  work/event_packs/capture_gaps.csv (the record); event_validate.py
                  (point V-EP15 at the structured record); tools.json/run_pipeline
                  (append); a dashboard-readable alert file (+ operator-chosen
                  email/webhook — ask).
Forbidden writes: capture/ingest/export code.
Acceptance:       red-first: a fixture raw stream with a seeded 10-min silence ⇒
                  detector emits the exact [start_us,end_us]; a dense stream ⇒ none;
                  event_validate reads the structured record and flags an
                  overlapping event `degraded` (the AF-1 interior_gap fixture now
                  fed by real gaps). make check + run_pipeline green.
Rollback:         revert; delete the derived record.
Exit evidence:    commit; detector test tail; a real gap detected + alerted.

## W-C3 — DEPLOY + VERIFY (zero-gap, monitored)
Purpose:          Ship W-C1 to the live Mac pipeline and PROVE the gaps are gone.
Blocked by:       W-C1 + W-C2 green + independent audits + operator go/no-go.
Allowed reads:    the running pipeline; the detector output.
Allowed writes:   rebuild build/ws_shadow from committed HEAD; backup prior binary;
                  deploy via the natural hourly boundary (Option A) or a clean
                  SIGTERM respawn; a SESSION_LOG deploy record.
Forbidden writes: n/a beyond the deploy.
Acceptance:       after deploy, INDUCE or wait for a real WS drop and confirm the
                  watchdog recovers it in << 1 min (reconnects increments, capture
                  resumes, NO top-of-hour-only recovery). Then W-C2 detector shows
                  ZERO market-wide gaps for a full 24h. Rollback = restore the prior
                  binary; the hourly respawn picks it up.
Exit evidence:    deploy commit/log; a recovered-drop timeline from ws_shadow.log;
                  24h zero-gap detector report.

## W-C4 — (optional) mark the known historical holes
Purpose:          Write the already-found 07-06/07-07 gap windows into the
                  structured capture-gap record so every pack/backtest over them
                  is correctly `degraded` retroactively. Data-only, reversible.
(Full seven fields at execution time.)

---

## §6 self-audit (this plan vs GUARDRAILS)
1. Phase/gates (P1,P2): named phase 1; reaches for no live trading. ✅
2. Live orders (S1–S6): none. ✅
3. Log-odds + fees (Q1,Q3): n/a (capture, not strategy). ✅
4. Pessimistic bound (Q2): this PROTECTS it (a holed tape breaks it). ✅
5. WS trading data (Q5): this is the WS capture path itself. ✅
6. Tests incl. behavior (E1,D4): W-C1/W-C2 ship red-first tests in-change. ✅
7. Pipeline continuity (P4): W-C0 read-only; W-C1 leaves the healthy path
   byte-for-byte unchanged (only adds a recovery action); W-C3 is zero-gap +
   rollback-ready. The single biggest risk in the project — hence diagnose-first,
   audit-each, monitored deploy. ✅
8. Reversible/bounded (P3,P6): each W its own commit; binary backup; derived
   records deletable. ✅
9. Docs move with code (E5): quality_log + this plan + SESSION_LOG updated in-change. ✅
10. Could green lie (D2): W-C2 exists precisely so a gap can never again pass
    unnoticed; V-EP15 fed by the real record. ✅

## Test + audit gates (do not skip)
- Every W: `make check` + `tests/run_pipeline.sh` green before "done".
- W-C1 and W-C2: **red-first proven** (test fails without the fix, passes with).
- After EACH W: an **independent, fresh-context adversarial audit** (as in the
  event-packaging Ws — it has caught real defects every time: the AF-3 aggregate
  residual, V-EP15 being inert). Fix what it finds before proceeding.
- W-C3 deploy is **operator go/no-go** and is not "done" until the 24h zero-gap
  report is green.
