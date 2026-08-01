# Post-seal book-clock addendum

This note does not modify the sealed preregistration or script.

After the sealed run, the book-replay root cause was identified: ordering by
`(ts_utc, ws_seq)` can place a delta before its snapshot. The required causal
order is `(recv_wall_ns, recv_mono_ns, ws_seq)`. The corrected replay reduced
the cited negative-level sample from 1,120 to zero, with capture gap/loss zero.

This does not change the current `NO_DECISION_DATA_ALIGNMENT_BLOCKED` result.
The audit found zero CF Benchmarks files on 2026-07-20..22 and returned before
opening or reconstructing any book parquet. No mid, gap, or L2 completion-cost
metric was produced.

It does invalidate the dormant full-result book branch in the sealed script.
That script must not be reused if historical BRTI later appears. A new
preregistration must:

- order books by `recv_wall_ns`, then `recv_mono_ns`, then `ws_seq`;
- put book and trade observations used for future markouts in the same
  receive-clock domain;
- keep BRTI source-time versus official settlement as a separate calibration
  clock boundary;
- continue failing closed on negative displayed quantities, without clamping.
