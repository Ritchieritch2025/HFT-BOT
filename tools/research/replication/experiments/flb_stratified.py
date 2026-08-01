#!/usr/bin/env python
"""FLB stratified measurement — implements agents/audit/EXPT_FLB_分层实验设计_2026-07-27.md

Read-only measurement over sealed warehouse days. NO trading actions.

Usage:  python flb_stratified.py <index|cells|gaps|spreads|post|all>
Output: $FLB_OUT (default /home/ubuntu/flb_expt_20260727)

Design mapping to the spec:
- price = the price the TAKER actually paid on their side (参与者实际付了什么);
  cells therefore exist per side ('yes'/'no' taker), bucketed on the paid price.
- realized rate = share of markets in the cell where the taker side settled as
  winner (settlement truth from catalog_normal, finalized yes/no only).
- sample unit = market; every trade of a market collapses to one row per
  (market, side, bucket, band); CI = cluster bootstrap x1000 over markets.
- time band anchor = market close_time (fallback settlement_ts).
- cost = median L1 spread of the cell (yes-axis, mirrored for the no side)
  + Kalshi taker fee 7*p*(1-p) cents.
- gap events operationalized as >=30c yes-price jump between consecutive
  trades in one market (spec left X open; documented deviation).
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication import data_access as da
from replication.taxonomy import (family_of, BUCKETS, MIRROR, BANDS, BUCKET_SQL, BAND_SQL)
from replication.stats import taker_fee_c

BASE = os.environ.get('FLB_OUT', '/home/ubuntu/flb_expt_20260727')
# only categories that map to in-scope families (taxonomy.family_of)
SCOPE_CATEGORIES = ['Crypto', 'Sports', 'Politics', 'Elections', 'Mentions', 'Economics']


def scope_files(files):
    keep = tuple('category=%s/' % c for c in SCOPE_CATEGORIES)
    return [f for f in files if any(k in f for k in keep)]
BOOT_N = 1000
SEED = 20260727
JUMP_C = 30.0


def phase_index():
    n, n_scalar, n_unfinal = da.build_settlement_index(BASE + '/settlement_index.parquet')
    print('index: finalized_yn=%d skipped_scalar=%d skipped_unfinalized=%d'
          % (n, n_scalar, n_unfinal), flush=True)


def _con():
    return da.duckdb_connect(memory_limit='12GB', temp_dir=BASE + '/tmp')


def phase_cells():
    con = _con()
    fs = scope_files(da.trade_files())
    print('cells: %d trade files' % len(fs), flush=True)
    con.execute("CREATE VIEW idx AS SELECT * FROM read_parquet('%s/settlement_index.parquet')" % BASE)
    q = """
COPY (
WITH t AS (
  SELECT ts_utc, market_ticker, series_ticker, category, taker_side,
         yes_price_e4/100.0 AS yes_c, no_price_e4/100.0 AS no_c, count_e4/1e4 AS qty
  FROM read_csv_auto(%s, union_by_name=1, types={'taker_side': 'VARCHAR'})
), j AS (
  SELECT t.*, s.result, s.anchor_us,
    CASE WHEN taker_side='yes' THEN yes_c ELSE no_c END AS paid_c,
    CASE WHEN result = taker_side THEN 1 ELSE 0 END AS win,
    (s.anchor_us - t.ts_utc)/60000000.0 AS tts_min
  FROM t JOIN idx s ON t.market_ticker = s.ticker
  WHERE taker_side IN ('yes','no')
), b AS (
  SELECT *, %s AS bucket, %s AS band,
    (paid_c >= 45 AND paid_c < 55) AS in4555
  FROM j
)
SELECT market_ticker, any_value(series_ticker) AS series, any_value(category) AS category,
       taker_side, bucket, band, in4555,
       any_value(result) AS result, any_value(win) AS win,
       sum(qty) AS qty, sum(paid_c*qty)/sum(qty) AS vw_paid_c, count(*) AS n_trades
FROM b
WHERE bucket IS NOT NULL AND band IS NOT NULL AND qty > 0
GROUP BY market_ticker, taker_side, bucket, band, in4555
) TO '%s/market_cells.parquet' (FORMAT PARQUET)
""" % (da.sql_file_list(fs), BUCKET_SQL.format(x='paid_c'), BAND_SQL.format(t='tts_min'), BASE)
    con.execute(q)
    st = con.execute("""
      SELECT count(*), sum(CASE WHEN s.ticker IS NULL THEN 1 ELSE 0 END)
      FROM (SELECT market_ticker FROM read_csv_auto(%s, union_by_name=1)) t
      LEFT JOIN idx s ON t.market_ticker = s.ticker
    """ % da.sql_file_list(fs)).fetchone()
    json.dump({'trades_total': int(st[0]), 'trades_unjoined_no_settlement': int(st[1]),
               'trade_files': len(fs), 'sealed_dates': da.sealed_dates()},
              open(BASE + '/stats_cells.json', 'w'), indent=1)
    print('cells done: trades_total=%s unjoined=%s' % st, flush=True)


def phase_gaps():
    con = _con()
    con.execute("CREATE VIEW idx AS SELECT * FROM read_parquet('%s/settlement_index.parquet')" % BASE)
    os.makedirs(BASE + '/gap_parts', exist_ok=True)
    for cat in SCOPE_CATEGORIES:
        fs = [f for f in da.trade_files() if 'category=%s/' % cat in f]
        if not fs:
            continue
        q = """
COPY (
WITH t AS (
  SELECT ts_utc, market_ticker, series_ticker, category, yes_price_e4/100.0 AS yes_c
  FROM read_csv_auto(%s, union_by_name=1)
), w AS (
  SELECT market_ticker, series_ticker, category, ts_utc, yes_c,
         lag(yes_c) OVER win AS prev_c, lag(ts_utc) OVER win AS prev_ts
  FROM t WINDOW win AS (PARTITION BY market_ticker ORDER BY ts_utc)
)
SELECT w.market_ticker, w.series_ticker, w.category, w.ts_utc, w.prev_ts,
       w.prev_c, w.yes_c, w.yes_c - w.prev_c AS jump_c,
       (s.anchor_us - w.ts_utc)/60000000.0 AS tts_min, s.result
FROM w JOIN idx s ON w.market_ticker = s.ticker
WHERE abs(w.yes_c - w.prev_c) >= %f
) TO '%s/gap_parts/%s.parquet' (FORMAT PARQUET)
""" % (da.sql_file_list(fs), JUMP_C, BASE, cat)
        con.execute(q)
        print('gaps part %s done' % cat, flush=True)
    con.execute("""COPY (SELECT * FROM read_parquet('%s/gap_parts/*.parquet'))
                   TO '%s/gap_events.parquet' (FORMAT PARQUET)""" % (BASE, BASE))
    n = con.execute("SELECT count(*) FROM read_parquet('%s/gap_events.parquet')" % BASE).fetchone()[0]
    print('gaps done: %d events' % n, flush=True)


def phase_spreads():
    con = _con()
    fs = scope_files(da.l1_files())
    print('spreads: %d l1 files' % len(fs), flush=True)
    con.execute("CREATE VIEW idx AS SELECT * FROM read_parquet('%s/settlement_index.parquet')" % BASE)
    q = """
COPY (
WITH l AS (
  SELECT market_ticker, series_ticker, category, ts_utc,
         yes_bid_e4/100.0 AS bid_c, yes_ask_e4/100.0 AS ask_c
  FROM read_parquet(%s, union_by_name=true)
  WHERE yes_bid_e4 > 0 AND yes_ask_e4 > 0 AND yes_ask_e4 < 10000 AND yes_ask_e4 >= yes_bid_e4
), j AS (
  SELECT l.market_ticker, l.series_ticker, l.category,
         (l.bid_c + l.ask_c)/2.0 AS mid_c, l.ask_c - l.bid_c AS spread_c,
         (s.anchor_us - l.ts_utc)/60000000.0 AS tts_min
  FROM l JOIN idx s ON l.market_ticker = s.ticker
), b AS (
  SELECT market_ticker, series_ticker, category, spread_c,
         %s AS bucket, %s AS band
  FROM j
)
SELECT market_ticker, any_value(series_ticker) AS series, any_value(category) AS category,
       bucket, band,
       avg(spread_c) AS avg_spread_c, median(spread_c) AS med_spread_c, count(*) AS n_quotes
FROM b
WHERE bucket IS NOT NULL AND band IS NOT NULL
GROUP BY market_ticker, bucket, band
) TO '%s/market_spreads.parquet' (FORMAT PARQUET)
""" % (da.sql_file_list(fs), BUCKET_SQL.format(x='mid_c'), BAND_SQL.format(t='tts_min'), BASE)
    con.execute(q)
    n = con.execute("SELECT count(*) FROM read_parquet('%s/market_spreads.parquet')" % BASE).fetchone()[0]
    print('spreads done: %d market-cells' % n, flush=True)


def _agg_market_rows(df, keys):
    df = df.copy()
    df['pw'] = df['vw_paid_c'] * df['qty']
    g = df.groupby(keys, sort=False).agg(
        qty=('qty', 'sum'), pw=('pw', 'sum'), n_trades=('n_trades', 'sum'),
        win=('win', 'first'), family=('family', 'first')).reset_index()
    g['vw_paid_c'] = g['pw'] / g['qty']
    return g.drop(columns=['pw'])


def phase_post():
    import pandas as pd
    import numpy as np
    rng = np.random.default_rng(SEED)
    mc = da.read_parquet_df(BASE + '/market_cells.parquet')
    # taker_side may arrive as bool (CSV sniffer reads yes/no as BOOLEAN)
    mc['taker_side'] = np.where(
        mc['taker_side'].astype(str).str.lower().isin(['true', 'yes']), 'yes', 'no')
    mc['family'] = [family_of(s, c) for s, c in zip(mc['series'], mc['category'])]
    sp = (mc[mc['category'] == 'Sports'].groupby(['series', 'family'], dropna=False)
          .agg(markets=('market_ticker', 'nunique'), qty=('qty', 'sum')).reset_index())
    sp.sort_values('qty', ascending=False).to_csv(BASE + '/sports_series_map.csv', index=False)
    mc = mc[mc['family'].notna()].copy()

    std = _agg_market_rows(mc, ['market_ticker', 'taker_side', 'bucket', 'band'])
    nc = _agg_market_rows(mc[mc['in4555']], ['market_ticker', 'taker_side', 'band'])
    nc['bucket'] = '45-55(null)'

    spr = da.read_parquet_df(BASE + '/market_spreads.parquet')
    spr['family'] = [family_of(s, c) for s, c in zip(spr['series'], spr['category'])]
    spr = spr[spr['family'].notna()]
    spr_cell = (spr.groupby(['family', 'bucket', 'band'])
                   .agg(spread_med_c=('avg_spread_c', 'median'),
                        spread_n_markets=('market_ticker', 'nunique')).reset_index())
    spr_map = {(r.family, r.bucket, r.band): (r.spread_med_c, r.spread_n_markets)
               for r in spr_cell.itertuples()}

    def spread_for(family, side, bucket, band):
        if bucket.startswith('45-55'):
            vs = [v for v in (spr_map.get((family, '35-50', band)),
                              spr_map.get((family, '50-65', band))) if v]
            if not vs:
                return None, 0
            return float(np.mean([v[0] for v in vs])), int(sum(v[1] for v in vs))
        b = bucket if side == 'yes' else MIRROR.get(bucket, bucket)
        v = spr_map.get((family, b, band))
        return (float(v[0]), int(v[1])) if v else (None, 0)

    def build_cells(df):
        out = []
        if len(df) == 0:
            return pd.DataFrame(out)
        for (fam, side, bucket, band), g in df.groupby(['family', 'taker_side', 'bucket', 'band']):
            prices = g['vw_paid_c'].to_numpy(float)
            wins = g['win'].to_numpy(float)
            qty = g['qty'].to_numpy(float)
            n = len(g)
            avg_p = float(prices.mean())
            rate = float(wins.mean() * 100.0)
            bias = avg_p - rate
            if n >= 2:
                i = rng.integers(0, n, size=(BOOT_N, n))
                bs = prices[i].mean(axis=1) - wins[i].mean(axis=1) * 100.0
                lo, hi = (float(x) for x in np.percentile(bs, [2.5, 97.5]))
            else:
                lo = hi = None
            sprd, sprd_n = spread_for(fam, side, bucket, band)
            f = taker_fee_c(avg_p)
            cost = (sprd + f) if sprd is not None else None
            losers = prices[wins == 1]  # shorting the taker side loses when taker side wins
            max_loss = float((100.0 - losers).max()) if losers.size else None
            out.append(dict(
                family=fam, side=side, bucket=bucket, band=band,
                n_markets=n, grey=(n < 30),
                avg_paid_c=round(avg_p, 2), realized_pct=round(rate, 2), bias_pp=round(bias, 2),
                ci_lo=round(lo, 2) if lo is not None else None,
                ci_hi=round(hi, 2) if hi is not None else None,
                vw_paid_c=round(float((prices * qty).sum() / qty.sum()), 2),
                vw_realized_pct=round(float((wins * qty).sum() / qty.sum() * 100.0), 2),
                qty_contracts=round(float(qty.sum()), 1), n_trades=int(g['n_trades'].sum()),
                spread_med_c=round(sprd, 2) if sprd is not None else None,
                spread_n_markets=sprd_n,
                fee_c=round(f, 2),
                cost_c=round(cost, 2) if cost is not None else None,
                exceeds_cost=bool(cost is not None and abs(bias) > cost and n >= 30
                                  and lo is not None and (lo > 0 or hi < 0)),
                max_single_loss_c=round(max_loss, 1) if max_loss is not None else None,
            ))
        return pd.DataFrame(out)

    cells = build_cells(std)
    cells_nc = build_cells(nc)
    allcells = pd.concat([cells, cells_nc], ignore_index=True)
    allcells.sort_values(['family', 'side', 'band', 'bucket']).to_csv(BASE + '/cells_full.csv', index=False)

    sanity = {}
    conv = {}
    for fam in cells['family'].unique():
        cf = cells[(cells['family'] == fam) & (~cells['grey'])]
        a = cf[cf['band'] == 'lt2m']['bias_pp'].abs()
        b = cf[cf['band'] == 'gt60m']['bias_pp'].abs()
        if len(a) >= 2 and len(b) >= 2:
            conv[fam] = {'lt2m_mean_abs_bias': round(float(a.mean()), 2),
                         'gt60m_mean_abs_bias': round(float(b.mean()), 2),
                         'pass': bool(a.mean() < b.mean()),
                         'n_buckets_lt2m': int(len(a)), 'n_buckets_gt60m': int(len(b))}
        else:
            conv[fam] = {'pass': None, 'reason': 'insufficient non-grey buckets',
                         'n_buckets_lt2m': int(len(a)), 'n_buckets_gt60m': int(len(b))}
    sanity['convergence'] = conv
    comp = []
    for r in cells[(cells['side'] == 'yes') & (~cells['grey'])].itertuples():
        m = cells[(cells['family'] == r.family) & (cells['side'] == 'no') &
                  (cells['bucket'] == MIRROR[r.bucket]) & (cells['band'] == r.band) & (~cells['grey'])]
        if len(m) == 1:
            mb = float(m['bias_pp'].iloc[0])
            comp.append({'family': r.family, 'band': r.band, 'yes_bucket': r.bucket,
                         'yes_bias': r.bias_pp, 'no_mirror_bias': mb,
                         'opposite_sign': bool(r.bias_pp * mb < 0),
                         'mag_ratio': round(abs(r.bias_pp) / abs(mb), 2) if mb != 0 else None})
    if comp:
        ok = sum(1 for c in comp if c['opposite_sign'])
        sanity['complementarity'] = {'pairs': len(comp), 'opposite_sign_pairs': ok,
                                     'share': round(ok / len(comp), 3), 'detail': comp}
    else:
        sanity['complementarity'] = {'pairs': 0}
    nullc = []
    for r in cells_nc.itertuples():
        nullc.append({'family': r.family, 'side': r.side, 'band': r.band, 'n': int(r.n_markets),
                      'grey': bool(r.grey), 'bias_pp': r.bias_pp, 'ci': [r.ci_lo, r.ci_hi],
                      'pass': bool(r.grey or (r.ci_lo is not None and r.ci_lo <= 0 <= r.ci_hi))})
    sanity['null_cell_45_55'] = nullc
    json.dump(sanity, open(BASE + '/sanity.json', 'w'), indent=1, ensure_ascii=False)

    gp = da.read_parquet_df(BASE + '/gap_events.parquet')
    gp['family'] = [family_of(s, c) for s, c in zip(gp['series_ticker'], gp['category'])]
    gp = gp[gp['family'].notna()].copy()
    if len(gp):
        gp['ts_iso'] = pd.to_datetime(gp['ts_utc'], unit='us', utc=True).astype(str)
        gp['dt_prev_s'] = (gp['ts_utc'] - gp['prev_ts']) / 1e6
        gsum = (gp.groupby('family')
                  .agg(events=('market_ticker', 'count'), markets=('market_ticker', 'nunique'),
                       med_abs_jump=('jump_c', lambda x: float(np.median(np.abs(x))))).reset_index())
        gsum['criterion_valid'] = gsum['events'] >= 3
        gsum.to_csv(BASE + '/gaps_summary.csv', index=False)
        (gp.sort_values('jump_c', key=abs, ascending=False)
           .head(500)[['family', 'market_ticker', 'series_ticker', 'ts_iso', 'prev_c', 'yes_c',
                       'jump_c', 'dt_prev_s', 'tts_min', 'result']]
           ).to_csv(BASE + '/gap_events_top.csv', index=False)
    else:
        open(BASE + '/gaps_summary.csv', 'w').write('family,events\n')
    print('post done: cells=%d nullcells=%d gaps=%d' % (len(cells), len(cells_nc), len(gp)), flush=True)


PHASES = {'index': phase_index, 'cells': phase_cells, 'gaps': phase_gaps,
          'spreads': phase_spreads, 'post': phase_post}

if __name__ == '__main__':
    os.makedirs(BASE + '/tmp', exist_ok=True)
    arg = sys.argv[1]
    if arg == 'all':
        for name in ['index', 'cells', 'gaps', 'spreads', 'post']:
            print('== phase %s ==' % name, flush=True)
            PHASES[name]()
    else:
        PHASES[arg]()
