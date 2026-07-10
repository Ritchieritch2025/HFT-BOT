# Maker-edge pilot spec (operator-authored, 2026-07-10, verbatim)
# Hypothesis: expected maker edge = spread captured − post-fill adverse-selection markout − fees
# Full 10-point spec preserved from operator instruction; see chat-of-record 2026-07-10.
# 1 inventory data (paths/schemas/rows/dates/ts-quality/dupes/depth)
# 2 separate microstructure edge vs sports-prediction edge (no score data)
# 3 one well-sampled PRE-MATCH market/category pilot
# 4 test hypothesis above
# 5 markouts at 1s/10s/30s/120s × spread/price/time-to-event/vol/liquidity/side/size
# 6 sample size + median + tails + uncertainty; chronological train/val/test; no look-ahead, no same-period selection
# 7 document fill-model assumptions; no hypothetical fills presented as real
# 8 columnar workflow (DuckDB), no needless pandas row-loads
# 9 outputs: DQ report, small results table, HTML, reproducible commands, trade/reject/collect verdict, next experiment
# 10 map metrics to Glosten-Milgrom / A-S / Kyle / VPIN; parameters from OUR data only
# SANDBOX RUN 2026-07-10 (this dir): trades-only slice executed (inventory.py, markout_trades.py).
# BLOCKER for full pilot in web-sandbox: L1 is parquet-only (266 files), no parquet reader available there.
# Full pilot belongs on Mac/EC2 via duckdb + warehouse.load().
