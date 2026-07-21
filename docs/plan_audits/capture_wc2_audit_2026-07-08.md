# W-C2 AUDIT — capture-gap record/detector (2026-07-08)

**Subject:** commit e455f99 (tools/capture_gaps.py). Independent fresh-context
adversarial audit. **Initial verdict: 3 BLOCKING false-negative defects** — all
now REMEDIATED (+ a 4th robustness crash found while re-scanning real data).

The audit was right to be adversarial: for a gap record that GATES whether a
backtest is trusted, a false negative (missing a real hole) is the dangerous
direction — it lets a holed tape pass as complete. All three findings were real.

## Findings + remediation
- **B1 [FIXED] day-boundary / edge holes silently missed.** find_gaps only
  emitted a gap for a consecutive record PAIR, so a hole crossing midnight (last
  record in date=D/, first in date=D+1/) was seen by neither day's scan; ditto
  head-of-day and tail-of-day silence. FIX: scan_date now also emits LEADING
  (day_start→first record) and TRAILING (last record→min(day_end, now)) edge
  gaps; a midnight-crossing hole is recorded as a trailing gap of D + a leading
  gap of D+1. `now` cap prevents flagging an in-progress day. PROVEN ON REAL
  DATA: the fixed tool caught 2026-07-08 00:00:00→00:10:25 (10.4-min midnight
  respawn-delay hole) that the buggy version reported as "0 gaps".
- **B2 [FIXED] a day with zero parseable records recorded as CLEAN.** An empty
  scan returned gaps=[] whether the raw was pruned OR present-but-unparseable
  (e.g. a recv_wall_ns field rename → all lines skipped). FIX: files present +
  0 records ⇒ record a full-day gap and `main` exits non-zero (fail-closed,
  mirroring --live); no raw files ⇒ see B3.
- **B3 [FIXED] re-scanning a raw-pruned day WIPED its recorded gaps.** Raw
  retention is 3 days (pipeline_supervisor.sh); write_record unconditionally
  replaced a day's rows with the fresh (empty, because pruned) scan, destroying
  the durable record it exists to keep. FIX: write_record(replace_day=...); the
  build path passes replace_day=False when a day has no raw files, PRESERVING
  its existing rows. The record now outlives the 3-day raw.
- **B4 [FIXED, found during real re-scan] corrupt line crashed the scan.** A
  garbage digit-run on 2026-07-06 parsed to an absurd timestamp that overflowed
  the int64 array (OverflowError). FIX: _extract_recv_us plausibility-windows
  recv_wall_ns to 2020..2100 (D3 — validate at the boundary); implausible/corrupt
  values are dropped and counted (07-06: 163 unparsed, scan completed clean).

## Verified SOUND by the audit (kept)
- The global-sort fix (overlapping/duplicate/out-of-order segments) is correct;
  dropping records can only enlarge a gap, never hide one. _extract_recv_us slice
  anchors on the leading quote (won't mis-grab orig_recv_wall_ns-style fields).
  _day_bounds_us is correct UTC; os.replace is atomic; units are µs end-to-end
  into event_validate.load_gaps.

## Non-blocking (accepted / BACKLOG)
- N1 partial-subscription loss (some markets keep flowing while one drops) is
  invisible to a market-wide silence detector — a per-market-family check is a
  later enhancement; caveat noted. N2 --live reads the whole current file each
  call (bounded-tail later). N3 _newest_raw_file could pick a compressed rotation
  (fail-SAFE: spurious alert, not false-clean). find_gaps uses strict `>`
  (exactly min_gap not flagged — policy choice).

## Tests + result
- 19 pytest (was 11): added midnight-crossing, leading/trailing edge,
  in-progress-day-not-flagged, unreadable-day fail-closed, pruned-day preserve,
  rescan-after-prune preserve, corrupt/implausible-value drop. All green.
- Real data (boundary-aware, corrupt-safe): 07-06 14 gaps (163 unparsed dropped),
  07-07 21 gaps (+2 midnight edges vs the earlier 19), 07-08 1 gap (the
  00:00-00:10 midnight hole). 36 total in work/event_packs/capture_gaps.csv.
- Remediation commit: (this change). Independent re-audit recommended before this
  record is relied on as the sole backtest gate, but the blocking defects are
  closed and pinned by red-first tests.
