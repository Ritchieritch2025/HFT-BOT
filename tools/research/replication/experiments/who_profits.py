#!/usr/bin/env python
"""Who Profits from Prediction? — Kalshi replication (Della Vedova 2026-06-30).

Paper: 222M Polymarket trades. Two skills — forecasting (side picked) and
execution (price paid) — are near independent; execution decides who profits.
Bots at 49.9% accuracy earn +$133M, active retail at 51.3% lose $79M.

What our data allows (and what it does not)
-------------------------------------------
Kalshi's public tape has NO wallet identity, so the paper's wallet-level
classification (bot / sophisticated / active retail / casual / one-shot) and
its wallet-level persistence tests CANNOT be reproduced. What the tape does
carry on every print is `taker_side`, i.e. the ORDER ROLE. The paper's own
mechanism section (5.2/5.3) shows the profitable type is the one that operates
disproportionately as maker (57% of bot trades), and the unprofitable type pays
the spread as taker. So the reproducible axis here is maker vs taker, which is
the paper's mechanism margin rather than its classification margin.

Zero-sum note: with two roles per print, maker components are the exact
negative of taker components (before fees). Everything below is therefore
reported from the TAKER side; the maker side is its mirror, except for fees,
which only takers pay on Kalshi (7*p*(1-p) c/contract, maker fee 0) — a
difference from fee-free Polymarket that we measure separately.

Experiments (paper analog in brackets)
  E1 inversion            [Table 2 A]  accuracy vs realized P&L by family/role
  E2 excess accuracy      [Table 2 B]  accuracy minus max(p,1-p) price-implied null
  E3 decomposition        [Figure 2]   edge = directional + execution (+ fee)
  E4 benchmark gradient   [Table 4]    exec~dir coefficient under 3 benchmarks
  E5 lifecycle timing     [Figure 5]   edge-at-entry vs time-to-close
  E6 accuracy x distance  [Table 7]    marginal value of accuracy vs |p-0.5|
  E7 calibration          [Sec 4.1]    realized win rate vs price (FLB context)
  E8 markout              [Sec 5.2]    maker edge at entry / +5min / settlement

Design rules inherited from the house doctrine
  - never pool across market families (taxonomy.family_of); family first
  - settlement truth = catalog_normal finalized yes/no only
  - sample unit for headline numbers = MARKET (cluster bootstrap x1000)
  - only sealed dates

Usage:  python who_profits.py <index|cells|all>
Output: $WP_OUT (default /home/ubuntu/who_profits_20260727)
"""
import sys, os, json, glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication import data_access as da
from replication.taxonomy import BUCKET_SQL, BAND_SQL

BASE = os.environ.get('WP_OUT', '/home/ubuntu/who_profits_20260727')

# family -> (category filter, series predicate SQL).  Mirrors taxonomy.family_of
# but expressed in SQL so the whole pass stays inside DuckDB.
FAMILIES = {
    'crypto_15m':    (['Crypto'],  "series_ticker IN ('KXBTC15M','KXETH15M')"),
    'crypto_hourly': (['Crypto'],  "series_ticker IN ('KXBTCD','KXETHD','KXBTC','KXETH')"),
    'sports_game':   (['Sports'],  "(series_ticker LIKE '%GAME%' OR series_ticker IN "
                                   "('KXBOXING','KXUFC','KXMMA','KXATPMATCH','KXWTAMATCH','KXTENNISMATCH'))"),
    'sports_prop':   (['Sports'],  "NOT (series_ticker LIKE '%GAME%' OR series_ticker IN "
                                   "('KXBOXING','KXUFC','KXMMA','KXATPMATCH','KXWTAMATCH','KXTENNISMATCH'))"),
    'politics_events': (['Politics', 'Elections', 'Mentions'], "1=1"),
    'econ':          (['Economics'], "1=1"),
}
# big families are streamed one sealed date at a time (bounded memory); their
# markets are intraday so a per-date pass loses only each market's day-opening
# print, which has no preceding history and is excluded by the paper too.
CHUNKED = {'crypto_15m', 'crypto_hourly', 'sports_game', 'sports_prop'}

MK_WINDOW_US = 300 * 1000000   # 5-minute markout horizon


def _con():
    os.makedirs(BASE + '/tmp', exist_ok=True)
    return da.duckdb_connect(threads=3, memory_limit='16GB', temp_dir=BASE + '/tmp')


def phase_index():
    os.makedirs(BASE, exist_ok=True)
    n, n_scalar, n_unfinal = da.build_settlement_index(BASE + '/settlement_index.parquet')
    print('index: finalized_yn=%d skipped_scalar=%d skipped_unfinalized=%d'
          % (n, n_scalar, n_unfinal), flush=True)


def _files(cats, dates=None):
    fs = []
    for f in da.trade_files(dates):
        if any('category=%s/' % c.replace(' ', '_') in f for c in cats):
            fs.append(f)
    return fs


CELL_SQL = """
COPY (
WITH t AS (
  SELECT ts_utc, trade_id, market_ticker, series_ticker, taker_side,
         yes_price_e4/100.0 AS yes_c,
         no_price_e4/100.0  AS no_c,
         count_e4/1e4       AS qty
  FROM read_csv_auto({files}, union_by_name=1,
                     types={{'taker_side':'VARCHAR','trade_id':'VARCHAR'}})
  WHERE taker_side IN ('yes','no') AND count_e4 > 0
    AND yes_price_e4 BETWEEN 1 AND 9999
    AND {series_pred}
), j AS (
  SELECT t.*, s.result, s.anchor_us,
         CASE WHEN t.taker_side='yes' THEN t.yes_c ELSE t.no_c END AS paid_c,
         CASE WHEN s.result = t.taker_side THEN 1.0 ELSE 0.0 END   AS win,
         (s.anchor_us - t.ts_utc)/60000000.0                        AS tts_min
  FROM t JOIN idx s ON t.market_ticker = s.ticker
), w AS (
  SELECT *,
    lag(yes_c) OVER wo                                          AS lag_yes,
    sum(yes_c*qty) OVER wcum / nullif(sum(qty) OVER wcum, 0)    AS bvwap_yes,
    sum(yes_c*qty) OVER wloc / nullif(sum(qty) OVER wloc, 0)    AS lvwap_yes,
    avg(yes_c) OVER wfwd                                        AS mk5_yes
  FROM j
  WINDOW wo   AS (PARTITION BY market_ticker ORDER BY ts_utc, trade_id),
         wcum AS (PARTITION BY market_ticker ORDER BY ts_utc, trade_id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
         wloc AS (PARTITION BY market_ticker ORDER BY ts_utc, trade_id
                  ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING),
         wfwd AS (PARTITION BY market_ticker ORDER BY ts_utc
                  RANGE BETWEEN 1 FOLLOWING AND {mkw} FOLLOWING)
), d AS (
  SELECT *,
    -- fair price expressed on the axis the taker actually paid
    CASE WHEN taker_side='yes' THEN bvwap_yes ELSE 100-bvwap_yes END AS fair_b,
    CASE WHEN taker_side='yes' THEN lvwap_yes ELSE 100-lvwap_yes END AS fair_l,
    CASE WHEN taker_side='yes' THEN lag_yes   ELSE 100-lag_yes   END AS fair_g,
    CASE WHEN taker_side='yes' THEN 100-mk5_yes ELSE mk5_yes     END AS mk5_maker,
    7.0*(paid_c/100.0)*(1.0-paid_c/100.0)                            AS fee_c,
    greatest(paid_c, 100-paid_c)                                     AS implied_c,
    abs(paid_c-50.0)                                                 AS pdist_c
  FROM w
), b AS (
  SELECT *, {bucket_sql} AS bucket, {band_sql} AS band FROM d
)
SELECT
  '{family}'         AS family,
  market_ticker,
  any_value(series_ticker) AS series,
  taker_side,
  bucket, band,
  any_value(result)  AS result,
  count(*)           AS n,
  sum(qty)           AS qty,
  sum(qty*paid_c)    AS w_paid_c,
  sum(qty*win)       AS w_win,
  sum(win)           AS n_win,
  sum(qty*implied_c) AS w_implied_c,
  sum(qty*pdist_c)   AS w_pdist_c,
  sum(qty*fee_c)     AS w_fee_c,
  sum(qty*(100*win - paid_c))                            AS w_total_c,
  -- backward cumulative VWAP benchmark (paper primary)
  sum(CASE WHEN fair_b IS NULL THEN 0 ELSE qty END)      AS qty_b,
  sum(CASE WHEN fair_b IS NULL THEN 0 ELSE qty*(100*win - fair_b) END) AS w_dir_b,
  sum(CASE WHEN fair_b IS NULL THEN 0 ELSE qty*(fair_b - paid_c) END)  AS w_exe_b,
  -- local 50-trade VWAP
  sum(CASE WHEN fair_l IS NULL THEN 0 ELSE qty END)      AS qty_l,
  sum(CASE WHEN fair_l IS NULL THEN 0 ELSE qty*(100*win - fair_l) END) AS w_dir_l,
  sum(CASE WHEN fair_l IS NULL THEN 0 ELSE qty*(fair_l - paid_c) END)  AS w_exe_l,
  -- lagged single print (own-trade content minimised)
  sum(CASE WHEN fair_g IS NULL THEN 0 ELSE qty END)      AS qty_g,
  sum(CASE WHEN fair_g IS NULL THEN 0 ELSE qty*(100*win - fair_g) END) AS w_dir_g,
  sum(CASE WHEN fair_g IS NULL THEN 0 ELSE qty*(fair_g - paid_c) END)  AS w_exe_g,
  -- 5-minute markout on the MAKER side (maker entry = 100-paid_c)
  sum(CASE WHEN mk5_maker IS NULL THEN 0 ELSE qty END)   AS qty_mk,
  sum(CASE WHEN mk5_maker IS NULL THEN 0 ELSE qty*(mk5_maker-(100-paid_c)) END) AS w_mkout_c,
  min(tts_min)       AS tts_min_min,
  max(tts_min)       AS tts_min_max
FROM b
WHERE bucket IS NOT NULL AND band IS NOT NULL
GROUP BY market_ticker, taker_side, bucket, band
) TO '{out}' (FORMAT PARQUET)
"""


def phase_cells():
    con = _con()
    con.execute("CREATE VIEW idx AS SELECT * FROM read_parquet('%s/settlement_index.parquet')"
                % BASE)
    parts = BASE + '/parts'
    os.makedirs(parts, exist_ok=True)
    dates = da.sealed_dates()
    manifest = {'sealed_dates': dates, 'families': {}}
    for fam, (cats, pred) in FAMILIES.items():
        chunks = [[d] for d in dates] if fam in CHUNKED else [None]
        got = 0
        for ch in chunks:
            fs = _files(cats, ch)
            if not fs:
                continue
            tag = (ch[0] if ch else 'all')
            out = '%s/%s__%s.parquet' % (parts, fam, tag)
            if os.path.exists(out):
                got += 1
                continue
            con.execute(CELL_SQL.format(
                files=da.sql_file_list(fs), series_pred=pred, family=fam,
                bucket_sql=BUCKET_SQL.format(x='paid_c'),
                band_sql=BAND_SQL.format(t='tts_min'),
                mkw=MK_WINDOW_US, out=out))
            got += 1
            print('  %s %s -> %s rows' % (fam, tag,
                  con.execute("SELECT count(*) FROM read_parquet('%s')" % out).fetchone()[0]),
                  flush=True)
        manifest['families'][fam] = got
        print('%s: %d chunks' % (fam, got), flush=True)

    con.execute("COPY (SELECT * FROM read_parquet('%s/*.parquet', union_by_name=1)) "
                "TO '%s/cells.parquet' (FORMAT PARQUET)" % (parts, BASE))
    n, mk, q = con.execute(
        "SELECT count(*), count(DISTINCT market_ticker), sum(qty) "
        "FROM read_parquet('%s/cells.parquet')" % BASE).fetchone()
    manifest.update({'cells': int(n), 'markets': int(mk), 'contracts': float(q)})

    # join-rate diagnostic: how much of the in-scope tape has settlement truth
    diag = {}
    for fam, (cats, pred) in FAMILIES.items():
        fs = _files(cats, [dates[len(dates)//2]])
        if not fs:
            continue
        row = con.execute("""
          SELECT count(*), sum(CASE WHEN s.ticker IS NULL THEN 1 ELSE 0 END)
          FROM (SELECT market_ticker, series_ticker FROM read_csv_auto(%s, union_by_name=1)
                WHERE %s) t
          LEFT JOIN idx s ON t.market_ticker = s.ticker
        """ % (da.sql_file_list(fs), pred)).fetchone()
        diag[fam] = {'probe_date': dates[len(dates)//2],
                     'trades': int(row[0]), 'unjoined': int(row[1] or 0)}
    manifest['settlement_join_probe'] = diag
    json.dump(manifest, open(BASE + '/manifest.json', 'w'), indent=1)
    print('cells done: %d cells / %d markets / %.0f contracts' % (n, mk, q), flush=True)


if __name__ == '__main__':
    what = sys.argv[1] if len(sys.argv) > 1 else 'all'
    os.makedirs(BASE, exist_ok=True)
    if what in ('index', 'all'):
        phase_index()
    if what in ('cells', 'all'):
        phase_cells()
