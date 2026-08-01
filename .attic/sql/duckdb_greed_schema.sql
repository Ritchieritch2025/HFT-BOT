-- Greed-compatible DuckDB views over local Parquet partitions.
--
-- Run from the repository root after converting a capture:
--   duckdb work/warehouse/research.duckdb < sql/duckdb_greed_schema.sql
--
-- Public views select only Greed public columns. Internal run/source metadata
-- stays under work/warehouse/_meta/warehouse_*.json.

CREATE OR REPLACE VIEW orderbooks_l1_all AS
SELECT
  exchange_ts,
  market_ticker,
  market_id,
  price_dollars,
  yes_bid_dollars,
  yes_ask_dollars,
  yes_bid_size,
  yes_ask_size,
  last_trade_size,
  volume,
  open_interest,
  dollar_volume,
  dollar_open_interest
FROM read_parquet(
  'work/warehouse/orderbooks_l1/date=*/part-*.parquet',
  hive_partitioning = true,
  union_by_name = false
);

CREATE OR REPLACE VIEW orderbooks_full_all AS
SELECT
  recorded_at,
  market_ticker,
  yes_bids,
  no_bids
FROM read_parquet(
  'work/warehouse/orderbooks_full/date=*/part-*.parquet',
  hive_partitioning = true,
  union_by_name = false
);

CREATE OR REPLACE VIEW trades_all AS
SELECT
  trade_id,
  ticker,
  taker_side,
  created_time,
  yes_price_dollars,
  no_price_dollars,
  count
FROM read_parquet(
  'work/warehouse/trades/date=*/part-*.parquet',
  hive_partitioning = true,
  union_by_name = false
);

CREATE OR REPLACE VIEW markets_all AS
SELECT
  ticker,
  event_ticker,
  series_ticker,
  market_type,
  yes_sub_title,
  no_sub_title,
  strike_type,
  floor_strike,
  cap_strike,
  functional_strike,
  custom_strike,
  expected_expiration_time,
  close_time,
  as_of
FROM read_parquet(
  'work/warehouse/markets/date=*/part-*.parquet',
  hive_partitioning = true,
  union_by_name = false
);

CREATE OR REPLACE VIEW events_all AS
SELECT
  event_ticker,
  series_ticker,
  title,
  category,
  mutually_exclusive,
  collateral_return_type,
  as_of
FROM read_parquet(
  'work/warehouse/events/date=*/part-*.parquet',
  hive_partitioning = true,
  union_by_name = false
);

CREATE OR REPLACE VIEW market_settlements_all AS
SELECT
  ticker,
  event_ticker,
  market_type,
  result,
  status,
  settlement_value_dollars,
  settlement_ts,
  expiration_value,
  close_time,
  yes_sub_title,
  no_sub_title,
  rules_primary,
  rules_secondary,
  fractional_trading_enabled,
  price_level_structure,
  price_ranges
FROM read_parquet(
  'work/warehouse/market_settlements/date=*/part-*.parquet',
  hive_partitioning = true,
  union_by_name = false
);

CREATE OR REPLACE VIEW rfq_events_all AS
SELECT
  exchange_ts,
  market_ticker,
  msg_type,
  rfq_id,
  contracts,
  cash_order_qty_dollars,
  party_id,
  reject_reason_code,
  reject_reason_text,
  mve_collection_ticker,
  mve_selected_event_tickers,
  mve_selected_market_tickers,
  mve_selected_sides
FROM read_parquet(
  'work/warehouse/rfq_events/date=*/part-*.parquet',
  hive_partitioning = true,
  union_by_name = false
);

CREATE OR REPLACE VIEW latest_orderbooks_l1 AS
SELECT *
FROM orderbooks_l1_all
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY market_ticker
  ORDER BY exchange_ts DESC
) = 1;

CREATE OR REPLACE VIEW dashboard_market_feed AS
SELECT
  l.exchange_ts,
  l.market_ticker,
  l.market_id,
  l.price_dollars,
  l.yes_bid_dollars,
  l.yes_ask_dollars,
  l.yes_bid_size,
  l.yes_ask_size,
  l.volume,
  l.open_interest
FROM latest_orderbooks_l1 AS l
ORDER BY l.market_ticker;
