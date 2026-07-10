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

Example:

```bash
python3 sandbox/research/strategy_playground.py --date 2026-07-09 --files 2 --top 12
python3 sandbox/research/strategy_playground.py --date 2026-07-09 --files 3 --focus 'FRAMAR|NICALA'
```
