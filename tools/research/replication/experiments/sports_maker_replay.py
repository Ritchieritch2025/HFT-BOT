#!/usr/bin/env python
"""Sports maker replay v0 — follow-up to flb_stratified (operator-ordered 2026-07-27).

Pre-registered questions (fixed before running):
 Q1  Does the sports favorite-overpricing (>60m band) live in PRE-game or IN-game
     trading?  (resolves the convergence-check suspension)
 Q2  For a fixed 1-lot-per-market-per-cell maker portfolio fading the taxed side
     in the FAT cells, what are the daily P&L series and the four-line worst bill
     (worst single fill / worst day / max drawdown / line total)?
 Q3  Queue reality: what displayed size sits at the relevant price levels
     (pro-rata capture proxy for a k-lot quoter)?

Read-only measurement. No trading actions. Fill model is an assumption (we
inherit the average taker print at the market's vw taxed price), NOT a queue
simulation — Q3 quantifies how far from reality that is.

Phases: split | queue | portfolio  (or all)
Output: $SMR_OUT (default /home/ubuntu/sports_maker_replay_20260727)
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication import data_access as da
from replication.taxonomy import family_of, BUCKETS, BUCKET_SQL, BAND_SQL

BASE = os.environ.get('SMR_OUT', '/home/ubuntu/sports_maker_replay_20260727')
FLB = '/home/ubuntu/flb_expt_20260727'   # reuse settlement index from the FLB run
SEED = 20260727

# FAT cells (from FLB cells_full.csv, sports rows with exceeds_cost & bias>0):
# longshot fade (both sides) + favorite/near-certain fade (both sides)
FAT_BUCKETS_LONG = {'01-05', '05-10', '10-20', '20-35'}
FAT_BUCKETS_FAV = {'90-95', '95-99'}

# event ticker start time: -YYMONDDHHMM…  (ET; July = EDT = UTC-4)
MONTH_CASE = ("CASE regexp_extract(event_ticker, '-\\d{2}([A-Z]{3})\\d{2}\\d{4}', 1) "
              + ' '.join("WHEN '%s' THEN %d" % (m, i + 1) for i, m in enumerate(
                  ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']))
              + " ELSE NULL END")
START_US = ("CASE WHEN regexp_extract(event_ticker, '-(\\d{2})[A-Z]{3}\\d{2}\\d{4}', 1) = '' THEN NULL ELSE "
            "epoch_us(make_timestamp("
            "2000 + CAST(regexp_extract(event_ticker, '-(\\d{2})[A-Z]{3}\\d{2}\\d{4}', 1) AS INT), "
            + MONTH_CASE + ", "
            "CAST(regexp_extract(event_ticker, '-\\d{2}[A-Z]{3}(\\d{2})\\d{4}', 1) AS INT), "
            "CAST(regexp_extract(event_ticker, '-\\d{2}[A-Z]{3}\\d{2}(\\d{2})\\d{2}', 1) AS INT), "
            "CAST(regexp_extract(event_ticker, '-\\d{2}[A-Z]{3}\\d{2}\\d{2}(\\d{2})', 1) AS INT), 0)) "
            "+ 14400000000 END")


def sports_trade_files():
    return [f for f in da.trade_files() if 'category=Sports/' in f]


def sports_l1_files():
    return [f for f in da.l1_files() if 'category=Sports/' in f]


def _con():
    return da.duckdb_connect(memory_limit='12GB', temp_dir=BASE + '/tmp')


def phase_split():
    con = _con()
    fs = sports_trade_files()
    print('split: %d sports trade files' % len(fs), flush=True)
    con.execute("CREATE VIEW idx AS SELECT * FROM read_parquet('%s/settlement_index.parquet')" % FLB)
    q = """
COPY (
WITH t AS (
  SELECT ts_utc, market_ticker, series_ticker, event_ticker, category, taker_side,
         yes_price_e4/100.0 AS yes_c, no_price_e4/100.0 AS no_c, count_e4/1e4 AS qty,
         %s AS start_us
  FROM read_csv_auto(%s, union_by_name=1, types={'taker_side': 'VARCHAR'})
), j AS (
  SELECT t.*, s.result, s.anchor_us,
    CASE WHEN taker_side='yes' THEN yes_c ELSE no_c END AS paid_c,
    CASE WHEN result = taker_side THEN 1 ELSE 0 END AS win,
    (s.anchor_us - t.ts_utc)/60000000.0 AS tts_min,
    CASE WHEN start_us IS NULL THEN 'unknown'
         WHEN ts_utc < start_us THEN 'pregame' ELSE 'ingame' END AS phase
  FROM t JOIN idx s ON t.market_ticker = s.ticker
  WHERE taker_side IN ('yes','no')
), b AS (
  SELECT *, %s AS bucket, %s AS band FROM j
)
SELECT market_ticker, any_value(series_ticker) AS series, any_value(category) AS category,
       taker_side, bucket, band, phase,
       any_value(win) AS win, any_value(anchor_us) AS anchor_us,
       sum(qty) AS qty, sum(paid_c*qty)/sum(qty) AS vw_paid_c, count(*) AS n_trades
FROM b
WHERE bucket IS NOT NULL AND band IS NOT NULL AND qty > 0
GROUP BY market_ticker, taker_side, bucket, band, phase
) TO '%s/sports_cells_phase.parquet' (FORMAT PARQUET)
""" % (START_US, da.sql_file_list(fs), BUCKET_SQL.format(x='paid_c'), BAND_SQL.format(t='tts_min'), BASE)
    con.execute(q)
    st = con.execute("""SELECT phase, count(DISTINCT market_ticker), sum(n_trades)
                        FROM read_parquet('%s/sports_cells_phase.parquet') GROUP BY 1""" % BASE).fetchall()
    print('split done:', st, flush=True)


def phase_queue():
    con = _con()
    fs = sports_l1_files()
    print('queue: %d sports l1 files' % len(fs), flush=True)
    q = """
COPY (
WITH l AS (
  SELECT market_ticker, series_ticker, event_ticker, ts_utc,
         yes_bid_e4/100.0 AS bid_c, yes_ask_e4/100.0 AS ask_c,
         yes_bid_qty_e4/1e4 AS bid_q, yes_ask_qty_e4/1e4 AS ask_q,
         %s AS start_us
  FROM read_parquet(%s, union_by_name=true)
  WHERE yes_bid_e4 > 0 AND yes_ask_e4 > 0 AND yes_ask_e4 < 10000 AND yes_ask_e4 >= yes_bid_e4
), p AS (
  SELECT series_ticker, (bid_c+ask_c)/2.0 AS mid_c, ask_q, bid_q,
    CASE WHEN start_us IS NULL THEN 'unknown'
         WHEN ts_utc < start_us THEN 'pregame' ELSE 'ingame' END AS phase
  FROM l
)
SELECT CASE WHEN series_ticker LIKE '%%GAME%%' THEN 'sports_game' ELSE 'sports_prop' END AS fam,
       phase, side, bucket, median(disp_q) AS med_displayed_lots, count(*) AS n_quotes
FROM (
  SELECT series_ticker, phase, 'yes' AS side, %s AS bucket, ask_q AS disp_q FROM p
  UNION ALL
  SELECT series_ticker, phase, 'no' AS side, %s AS bucket, bid_q AS disp_q
  FROM (SELECT series_ticker, phase, 100.0 - mid_c AS mid_c, bid_q FROM p)
)
WHERE bucket IS NOT NULL
GROUP BY 1, 2, 3, 4
) TO '%s/queue_depth.parquet' (FORMAT PARQUET)
""" % (START_US, da.sql_file_list(fs), BUCKET_SQL.format(x='mid_c'), BUCKET_SQL.format(x='mid_c'), BASE)
    con.execute(q)
    n = con.execute("SELECT count(*) FROM read_parquet('%s/queue_depth.parquet')" % BASE).fetchone()[0]
    print('queue done: %d rows' % n, flush=True)


def phase_portfolio():
    import pandas as pd
    import numpy as np
    df = da.read_parquet_df(BASE + '/sports_cells_phase.parquet')
    df['taker_side'] = np.where(df['taker_side'].astype(str).str.lower().isin(['true', 'yes']), 'yes', 'no')
    df['family'] = [family_of(s, c) for s, c in zip(df['series'], df['category'])]
    df = df[df['family'].isin(['sports_game', 'sports_prop'])].copy()
    df['grp'] = np.where(df['bucket'].isin(FAT_BUCKETS_LONG), 'longshot',
                np.where(df['bucket'].isin(FAT_BUCKETS_FAV), 'favorite', None))
    fat = df[df['grp'].notna()].copy()
    # one market may appear in several (bucket, band) rows of a cell family; collapse to
    # one 1-lot position per (market, side, bucket, phase) at the volume-weighted price
    fat['pw'] = fat['vw_paid_c'] * fat['qty']
    pos = fat.groupby(['market_ticker', 'taker_side', 'bucket', 'phase'], sort=False).agg(
        family=('family', 'first'), grp=('grp', 'first'), win=('win', 'first'),
        qty=('qty', 'sum'), pw=('pw', 'sum'), anchor_us=('anchor_us', 'first')).reset_index()
    pos['p'] = pos['pw'] / pos['qty']
    # maker fades the taxed side: short 1 contract at p -> win of taxed side costs (100-p), else +p
    pos['pnl_c'] = np.where(pos['win'] == 1, -(100.0 - pos['p']), pos['p'])
    pos['date'] = pd.to_datetime(pos['anchor_us'], unit='us', utc=True).dt.date

    # Q1: pre/in-game grid for the favorite buckets (and longshots), market-equal-weight bias
    g = (pos.groupby(['family', 'phase', 'grp', 'taker_side'])
            .agg(n=('market_ticker', 'nunique'), avg_p=('p', 'mean'),
                 win_rate=('win', 'mean'), pnl_sum_c=('pnl_c', 'sum')).reset_index())
    g['bias_pp'] = g['avg_p'] - g['win_rate'] * 100.0
    g.to_csv(BASE + '/phase_grid.csv', index=False)

    # Q2: four-line worst bill per (family, phase, grp) for the unit-lot portfolio
    bills = []
    for (fam, ph, grp), gg in pos.groupby(['family', 'phase', 'grp']):
        daily = gg.groupby('date')['pnl_c'].sum().sort_index() / 100.0  # dollars
        cum = daily.cumsum()
        dd = float((cum - cum.cummax()).min()) if len(cum) else 0.0
        bills.append(dict(
            family=fam, phase=ph, grp=grp,
            n_positions=int(len(gg)), n_markets=int(gg['market_ticker'].nunique()),
            total_usd=round(float(gg['pnl_c'].sum()) / 100.0, 2),
            ev_c_per_lot=round(float(gg['pnl_c'].mean()), 2),
            win_rate_of_fade=round(float((gg['pnl_c'] > 0).mean()), 4),
            worst_single_fill_c=round(float(gg['pnl_c'].min()), 1),
            worst_day_usd=round(float(daily.min()) if len(daily) else 0.0, 2),
            max_drawdown_usd=round(dd, 2),
            days=int(len(daily)),
        ))
    pd.DataFrame(bills).sort_values(['family', 'grp', 'phase']).to_csv(BASE + '/worst_bills.csv', index=False)
    (pos.groupby(['family', 'phase', 'grp', 'date'])['pnl_c'].sum() / 100.0
     ).reset_index().to_csv(BASE + '/daily_pnl.csv', index=False)

    # Q3: attach queue medians
    qd = da.read_parquet_df(BASE + '/queue_depth.parquet')
    qd.to_csv(BASE + '/queue_depth.csv', index=False)
    json.dump({'positions': int(len(pos)),
               'phases': {k: int(v) for k, v in pos['phase'].value_counts().items()}},
              open(BASE + '/portfolio_stats.json', 'w'), indent=1)
    print('portfolio done: %d unit positions' % len(pos), flush=True)


PHASES = {'split': phase_split, 'queue': phase_queue, 'portfolio': phase_portfolio}

if __name__ == '__main__':
    os.makedirs(BASE + '/tmp', exist_ok=True)
    arg = sys.argv[1]
    for name in (['split', 'queue', 'portfolio'] if arg == 'all' else [arg]):
        print('== phase %s ==' % name, flush=True)
        PHASES[name]()
