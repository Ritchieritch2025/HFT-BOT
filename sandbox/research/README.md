# Research Sandbox

This directory is for isolated, read-only experiments.

Rules:
- No credentials.
- No network calls.
- No order placement.
- No writes to `work/raw`, `work/warehouse`, `work/gold`, or `work/mm`.
- Default output is stdout only.
- Optional reports must stay under `sandbox/research/reports/`.

Current tool:
- `strategy_playground.py` scans recent raw firehose files and prints research-only ideas:
  market-making candidates, pair-parity checks, ladder sanity checks, and large moves.
- `mm_sandbox.py` is the executable market-making replay MVP: deterministic
  Gold/synthetic tape, submit/cancel latency, stale-order exposure,
  strict-through/queue/optimistic fills, integer cash/inventory, markouts, and
  static-vs-dynamic policy comparison. Gold results remain diagnostic until a
  receive-clock + source-sequence-valid L2 adapter lands.

Example:

```bash
python3 sandbox/research/strategy_playground.py --date 2026-07-09 --files 2 --top 12
python3 sandbox/research/strategy_playground.py --date 2026-07-09 --files 3 --focus 'FRAMAR|NICALA'
python3 sandbox/research/mm_sandbox.py --demo
python3 -m pytest -q sandbox/research/test_mm_sandbox.py
```
