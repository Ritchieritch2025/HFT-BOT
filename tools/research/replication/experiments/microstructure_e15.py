"""E15 — Kalshi microstructure anatomy (Dubach 2026 replication + Kalshi-only cuts).

Heavy pass. Runs on the EC2 prod box, reads sealed warehouse data only, writes
small aggregate artifacts into <out>/ . Nothing is written into the warehouse.

Pre-registration: agents/audit/PREREG_E15_Kalshi微结构解剖_2026-07-27.md
All definitions below are the ones locked in that document; do not change one
without amending the pre-registration.

    python microstructure_e15.py all            # every pass
    python microstructure_e15.py settle l1 ...  # selected passes

Passes
  settle  catalog_normal -> settlement.parquet (ticker, series, result, close_us)
  census  per-category row census + unidentified-series list (counterexample-15 kit)
  l1      per (market, tte band) quote aggregates: spread, tick binding, empty book
  trades  trades ASOF L1 -> effective half-spread per (market, price bucket, band)
  l2      orderbooks_full reconstruction -> depth shape cells + 60s panel + gate rows
  vol     per (market, 60s bucket) traded contracts on the three L2 days
  ladder  crypto hourly strike ladder: L1 depth vs distance to strike
"""
import os, sys, glob, json, time, math
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication import data_access as da, taxonomy  # noqa: E402

OUT = os.environ.get('E15_OUT', '/home/ubuntu/e15_scratch/out')
TMP = os.environ.get('E15_TMP', '/dev/shm/e15tmp')
# Pre-registered window: right edge is the catalog snapshot instant.
DATES = ['2026-07-%02d' % d for d in range(10, 25)]
L2_DATES = ['2026-07-12', '2026-07-15', '2026-07-17']
SNAPSHOT_CUTOFF_US = 1784064617_000000  # 2026-07-24T21:30:17Z
IN_SCOPE_CATEGORIES = ['Sports', 'Crypto', 'Politics', 'Elections', 'Mentions', 'Economics']
DWELL_CAP_US = 60_000_000
STALE_CAP_US = 60_000_000
GRID_SHAPE_S = 10          # SF2-K depth-shape sampling grid
GRID_PANEL_S = 60          # SF8-K panel / reconstruction gate grid


def con_new(memory='8GB', threads=2, preserve_order=False):
    con = da.duckdb_connect(threads=threads, memory_limit=memory, temp_dir=TMP)
    con.execute('SET preserve_insertion_order=%s' % ('true' if preserve_order else 'false'))
    return con


def log(*a):
    print('[%s]' % time.strftime('%H:%M:%S'), *a, flush=True)


def subcats(fact, category, dates):
    root = da.warehouse_root()
    out = collections.defaultdict(list)
    for d in dates:
        for f in glob.glob('%s/facts/%s/category=%s/subcategory=*/date=%s/*' % (root, fact, category, d)):
            out[f.split('subcategory=')[1].split('/')[0]].append(f)
    return out


# ---------------------------------------------------------------- settlement
def pass_settle():
    p = os.path.join(OUT, 'settlement.parquet')
    n, n_scalar, n_unfinal = da.build_settlement_index(p)
    con = con_new()
    keep = con.execute("""SELECT count(*) FROM read_parquet('%s') WHERE close_us <= %d""" % (p, SNAPSHOT_CUTOFF_US)).fetchone()[0]
    log('settlement: %d finalized binary markets (%d scalar, %d unfinalized skipped); %d within snapshot cutoff'
        % (n, n_scalar, n_unfinal, keep))
    json.dump({'markets': n, 'skipped_scalar': n_scalar, 'skipped_unfinalized': n_unfinal,
               'within_cutoff': keep, 'snapshot': da.latest_catalog_snapshot(),
               'cutoff_us': SNAPSHOT_CUTOFF_US},
              open(os.path.join(OUT, 'settlement_meta.json'), 'w'), indent=1)


# ------------------------------------------------------------------- census
def pass_census():
    """Counterexample-15 kit part 4: what we looked at and what fell out of scope."""
    con = con_new()
    root = da.warehouse_root()
    rows = []
    for cat in sorted(set(f.split('category=')[1].split('/')[0]
                          for f in glob.glob(root + '/facts/orderbooks_l1/category=*'))):
        files = []
        for d in DATES:
            files += glob.glob('%s/facts/orderbooks_l1/category=%s/subcategory=*/date=%s/*.parquet' % (root, cat, d))
        if not files:
            continue
        if cat not in IN_SCOPE_CATEGORIES:
            r = con.execute("SELECT count(*), count(distinct market_ticker) FROM read_parquet(%s)"
                            % da.sql_file_list(files)).fetchone()
            rows.append({'category': cat, 'series': None, 'family': None, 'in_scope': False,
                         'l1_rows': r[0], 'markets': r[1]})
            continue
        df = con.execute("""SELECT series_ticker, count(*) n, count(distinct market_ticker) m
                            FROM read_parquet(%s) GROUP BY 1""" % da.sql_file_list(files)).df()
        for _, x in df.iterrows():
            fam = taxonomy.family_of(x.series_ticker, cat)
            rows.append({'category': cat, 'series': x.series_ticker, 'family': fam,
                         'in_scope': fam is not None, 'l1_rows': int(x.n), 'markets': int(x.m)})
    import pandas as pd
    df = pd.DataFrame(rows)
    da.write_df_parquet(df, os.path.join(OUT, 'census.parquet'))
    log('census: %d rows; in-scope series %d, unidentified series %d, excluded categories %d'
        % (len(df), int((df.in_scope == True).sum()),  # noqa: E712
           int(((df.in_scope == False) & df.series.notna()).sum()),  # noqa: E712
           int(df.series.isna().sum())))


# ----------------------------------------------------------------- SF1-K: L1
L1_SQL = """
WITH raw AS (
  SELECT market_ticker, series_ticker, category, ts_utc,
         yes_bid_e4, yes_ask_e4, yes_bid_qty_e4, yes_ask_qty_e4
  FROM read_parquet({files})
  WHERE ts_utc IS NOT NULL AND yes_bid_e4 IS NOT NULL AND yes_ask_e4 IS NOT NULL
), j AS (
  SELECT r.*, s.close_us FROM raw r
  JOIN read_parquet('{settle}') s ON r.market_ticker = s.ticker
  WHERE s.close_us <= {cutoff}
), d AS (
  SELECT *, lead(ts_utc) OVER (PARTITION BY market_ticker ORDER BY ts_utc) AS nxt,
         max(CASE WHEN (yes_bid_e4 % 100) <> 0 OR (yes_ask_e4 % 100) <> 0 THEN 1 ELSE 0 END)
             OVER (PARTITION BY market_ticker) AS deci
  FROM j
), f AS (
  SELECT market_ticker, series_ticker, category, deci,
    GREATEST(COALESCE(nxt, close_us) - ts_utc, 0) AS raw_dwell,
    (close_us - ts_utc) / 60000000.0 AS tte_min,
    (yes_bid_e4 > 0 AND yes_ask_e4 < 10000) AS two_sided,
    (yes_bid_e4 = 0 AND yes_ask_e4 = 10000) AS empty_book,
    (yes_ask_e4 - yes_bid_e4) / 100.0 AS spread_c,
    (yes_bid_e4 + yes_ask_e4) / 200.0 AS mid_c,
    (yes_bid_qty_e4 + yes_ask_qty_e4) / 10000.0 AS l1_depth,
    CASE WHEN deci = 1 THEN 0.1 ELSE 1.0 END AS tick_c
  FROM d
), g AS (
  SELECT *, LEAST(raw_dwell, {cap}) / 1e6 AS dw, raw_dwell / 1e6 AS dw_unc,
         {band} AS band
  FROM f WHERE raw_dwell > 0
)
SELECT market_ticker,
  any_value(series_ticker) AS series_ticker, any_value(category) AS category,
  COALESCE(band, 'ALL') AS band, max(deci) AS deci,
  count(*) AS n_rows,
  sum(dw) AS dwell_s, sum(dw_unc) AS dwell_unc_s,
  sum(CASE WHEN two_sided THEN dw ELSE 0 END) AS dwell_2s,
  sum(CASE WHEN empty_book THEN dw ELSE 0 END) AS dwell_empty,
  sum(CASE WHEN two_sided THEN dw * mid_c ELSE 0 END) AS w_mid,
  sum(CASE WHEN two_sided THEN dw * spread_c ELSE 0 END) AS w_spread_c,
  sum(CASE WHEN two_sided THEN dw * 10000.0 * spread_c / mid_c ELSE 0 END) AS w_spread_bps,
  sum(CASE WHEN two_sided AND spread_c <= tick_c * 1.001 THEN dw ELSE 0 END) AS dwell_1tick,
  sum(CASE WHEN two_sided AND spread_c <= tick_c * 2.001 THEN dw ELSE 0 END) AS dwell_le2tick,
  sum(CASE WHEN two_sided THEN dw * l1_depth ELSE 0 END) AS w_l1_depth,
  quantile_cont(CASE WHEN two_sided THEN spread_c END, 0.5) AS med_spread_c,
  quantile_cont(CASE WHEN two_sided THEN 10000.0 * spread_c / mid_c END, 0.25) AS p25_spread_bps,
  quantile_cont(CASE WHEN two_sided THEN 10000.0 * spread_c / mid_c END, 0.5) AS med_spread_bps,
  quantile_cont(CASE WHEN two_sided THEN 10000.0 * spread_c / mid_c END, 0.75) AS p75_spread_bps,
  quantile_cont(CASE WHEN two_sided THEN mid_c END, 0.5) AS med_mid_c
FROM g
WHERE band IS NOT NULL
GROUP BY GROUPING SETS ((market_ticker, band), (market_ticker))
"""


def pass_l1():
    settle = os.path.join(OUT, 'settlement.parquet')
    band = taxonomy.BAND_SQL.format(t='tte_min')
    dst = os.path.join(OUT, 'l1_market_band')
    os.makedirs(dst, exist_ok=True)
    total = 0
    for cat in IN_SCOPE_CATEGORIES:
        for sub, files in sorted(subcats('orderbooks_l1', cat, DATES).items()):
            part = os.path.join(dst, 'part_%s_%s.parquet' % (cat, sub))
            if os.path.exists(part):
                log('l1 %s/%s: exists, skipped' % (cat, sub))
                continue
            con = con_new()
            t0 = time.time()
            sql = L1_SQL.format(files=da.sql_file_list(sorted(files)), settle=settle,
                                cutoff=SNAPSHOT_CUTOFF_US, cap=DWELL_CAP_US, band=band)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET)" % (sql, part))
            n = con.execute("SELECT count(*) FROM read_parquet('%s')" % part).fetchone()[0]
            total += n
            log('l1 %s/%s: %d files -> %d rows (%.0fs)' % (cat, sub, len(files), n, time.time() - t0))
            con.close()
    log('l1 total rows %d -> %s' % (total, dst))


# -------------------------------------------------------------- SF5-K: trades
TRADES_SQL = """
WITH q AS (
  SELECT market_ticker, ts_utc, (yes_bid_e4 + yes_ask_e4) / 200.0 AS mid_c
  FROM read_parquet({qfiles})
  WHERE yes_bid_e4 > 0 AND yes_ask_e4 < 10000
), t AS (
  SELECT market_ticker, series_ticker, category, ts_utc,
         yes_price_e4 / 100.0 AS px_c, count_e4 / 10000.0 AS contracts,
         CASE WHEN taker_side = 'yes' THEN 1 ELSE -1 END AS dir
  FROM read_csv_auto({tfiles}, union_by_name=true)
  WHERE taker_side IN ('yes','no')
), j AS (
  SELECT t.*, q.mid_c, q.ts_utc AS q_ts
  FROM t ASOF JOIN q ON t.market_ticker = q.market_ticker AND t.ts_utc > q.ts_utc
), s AS (
  SELECT j.*, st.close_us,
         (st.close_us - j.ts_utc) / 60000000.0 AS tte_min
  FROM j JOIN read_parquet('{settle}') st ON j.market_ticker = st.ticker
  WHERE st.close_us <= {cutoff} AND j.ts_utc - j.q_ts <= {stale}
), f AS (
  SELECT *, dir * (px_c - mid_c) AS eff_c,
         10000.0 * dir * (px_c - mid_c) / mid_c AS eff_bps,
         {bucket} AS bucket, {band} AS band
  FROM s
)
SELECT market_ticker, any_value(series_ticker) AS series_ticker, any_value(category) AS category,
  COALESCE(bucket, 'ALL') AS bucket, COALESCE(band, 'ALL') AS band,
  count(*) AS n_trades, sum(contracts) AS contracts,
  sum(eff_c) AS sum_eff_c, sum(eff_c * contracts) AS sum_eff_c_w,
  sum(eff_bps) AS sum_eff_bps, sum(eff_c * eff_c) AS sum_eff_c_sq,
  sum(px_c * contracts) AS sum_px_w, sum(mid_c) AS sum_mid,
  quantile_cont(eff_c, 0.5) AS med_eff_c
FROM f
WHERE bucket IS NOT NULL AND band IS NOT NULL
GROUP BY GROUPING SETS ((market_ticker, bucket, band), (market_ticker, bucket), (market_ticker))
"""


def pass_trades():
    settle = os.path.join(OUT, 'settlement.parquet')
    bucket = taxonomy.BUCKET_SQL.format(x='px_c')
    band = taxonomy.BAND_SQL.format(t='tte_min')
    dst = os.path.join(OUT, 'eff_spread')
    os.makedirs(dst, exist_ok=True)
    total = 0
    for cat in IN_SCOPE_CATEGORIES:
        tf = subcats('trades', cat, DATES)
        qf = subcats('orderbooks_l1', cat, DATES)
        for sub in sorted(tf):
            if sub not in qf:
                log('trades %s/%s: no L1, skipped' % (cat, sub))
                continue
            part = os.path.join(dst, 'part_%s_%s.parquet' % (cat, sub))
            if os.path.exists(part):
                log('trades %s/%s: exists, skipped' % (cat, sub))
                continue
            con = con_new()
            t0 = time.time()
            sql = TRADES_SQL.format(qfiles=da.sql_file_list(sorted(qf[sub])),
                                    tfiles=da.sql_file_list(sorted(tf[sub])),
                                    settle=settle, cutoff=SNAPSHOT_CUTOFF_US,
                                    stale=STALE_CAP_US, bucket=bucket, band=band)
            con.execute("COPY (%s) TO '%s' (FORMAT PARQUET)" % (sql, part))
            n = con.execute("SELECT count(*) FROM read_parquet('%s')" % part).fetchone()[0]
            total += n
            log('trades %s/%s -> %d rows (%.0fs)' % (cat, sub, n, time.time() - t0))
            con.close()
    log('trades total rows %d -> %s' % (total, dst))


# ------------------------------------------------------------------ SF2/SF8
def mn_band(mid_c):
    """Distance from 50c = market-implied moneyness (pre-registered proxy)."""
    d = abs(mid_c - 50.0)
    if d < 5:
        return 'atm_0-5'
    if d < 15:
        return 'near_5-15'
    if d < 30:
        return 'mid_15-30'
    if d < 45:
        return 'far_30-45'
    return 'edge_45-50'


def tte_band(tte_min):
    if tte_min > 60:
        return 'gt60m'
    if tte_min > 10:
        return '10-60m'
    if tte_min > 5:
        return '5-10m'
    if tte_min > 2:
        return '2-5m'
    if tte_min > -5:
        return 'lt2m'
    return None


class Book:
    __slots__ = ('yes', 'no', 'init', 'b10', 'b60', 'last_ts')

    def __init__(self):
        self.yes = {}
        self.no = {}
        self.init = False
        self.b10 = None
        self.b60 = None
        self.last_ts = None


# Replay order is NOT the same across families and cannot be assumed.  The
# capture re-subscribes periodically, and ws_seq counts per subscription, so on
# a market carried by two overlapping subscriptions (all of Sports) ws_seq order
# interleaves two different time ranges.  On crypto 15M, which lives inside one
# subscription, ws_seq IS the true event order and ts_utc order is the broken
# one.  Measured against L1 truth on nine markets:
#     crypto   seq 0.94-0.98   ts 0.02-0.31
#     sports   seq 0.02-0.15   ts 0.92-0.96
# Dropping duplicate (ts, side, price, delta) deltas makes both worse, so the
# repeats are real book events, not stream duplicates.
# We therefore reconstruct under BOTH orders and let the known-answer gate pick
# per market — the gate is the arbiter, not an assumption about the feed.
VARIANTS = ('seq', 'ts')


def top_levels(d, best_is_max, k=10):
    """Top-k (price, size) from best inward. d maps price_e4 -> size (count_e4)."""
    if not d:
        return []
    ps = sorted(d.keys(), reverse=best_is_max)
    return [(p, d[p]) for p in ps[:k]]


def pass_l2():
    import numpy as np
    import pandas as pd
    settle = {}
    con = con_new()
    for t, c in con.execute("SELECT ticker, close_us FROM read_parquet('%s') WHERE close_us <= %d"
                            % (os.path.join(OUT, 'settlement.parquet'), SNAPSHOT_CUTOFF_US)).fetchall():
        settle[t] = c
    con.close()
    log('l2: settlement map %d markets' % len(settle))

    root = da.warehouse_root()
    cells = collections.defaultdict(lambda: np.zeros(24, dtype=np.float64))
    # layout: [0]=n, [1:11]=bid level depth sums, [11:21]=ask level depth sums,
    #         [21]=sum mid, [22]=sum spread_c, [23]=sum total top10 depth
    panel = []
    meta = collections.defaultdict(lambda: {'series': None, 'category': None, 'neg': 0,
                                            'nonempty': False, 'samples': 0, 'skipped_pre': 0,
                                            'one_sided': 0, 'crossed': 0,
                                            'n_cmp': 0, 'n_match': 0, 'n_near': 0, 'err_abs': 0})
    files = []
    for d in L2_DATES:
        files += sorted(glob.glob('%s/facts/orderbooks_full/category=*/subcategory=*/date=%s/*.parquet' % (root, d)))

    for fp in files:
        t0 = time.time()
        con = con_new(memory='6GB')
        qf = fp.replace('/orderbooks_full/', '/orderbooks_l1/').replace(
            'orderbooks_full__', 'orderbooks_l1__')
        quotes = {}
        if os.path.exists(qf):
            qdf = con.execute("""SELECT market_ticker, ts_utc, yes_bid_e4, yes_ask_e4
                                 FROM read_parquet('%s')
                                 WHERE yes_bid_e4 IS NOT NULL AND yes_ask_e4 IS NOT NULL
                                 ORDER BY market_ticker, ts_utc""" % qf).df()
            for mk, gq in qdf.groupby('market_ticker', sort=False):
                quotes[mk] = list(zip(gq.ts_utc.tolist(), gq.yes_bid_e4.tolist(),
                                      gq.yes_ask_e4.tolist()))
        cur = con.execute("""SELECT market_ticker, ts_utc, msg_type, side, price_e4, delta_e4,
                                    yes_levels, no_levels, series_ticker, category
                             FROM read_parquet('%s')
                             ORDER BY market_ticker, ws_sid, ws_seq""" % fp)
        nrows, nmkt = 0, 0
        cur_m, buf = None, []
        while True:
            batch = cur.fetch_df_chunk(50)
            if batch is None or len(batch) == 0:
                break
            nrows += len(batch)
            mt = batch['market_ticker'].tolist()
            ts = batch['ts_utc'].tolist()
            mg = batch['msg_type'].tolist()
            sd = batch['side'].tolist()
            px = batch['price_e4'].fillna(-1).astype('int64').tolist()
            dl = batch['delta_e4'].fillna(0).astype('int64').tolist()
            yl = batch['yes_levels'].tolist()
            nl = batch['no_levels'].tolist()
            st = batch['series_ticker'].tolist()
            ct = batch['category'].tolist()
            for i in range(len(mt)):
                if mt[i] != cur_m:
                    if buf:
                        _replay(cur_m, buf, settle, cells, meta, panel,
                                quotes.get(cur_m, ()))
                        nmkt += 1
                    cur_m, buf = mt[i], []
                    for _v in VARIANTS:            # meta is keyed (market, variant)
                        meta[(cur_m, _v)]['series'] = st[i]
                        meta[(cur_m, _v)]['category'] = ct[i]
                buf.append((ts[i], mg[i], sd[i], px[i], dl[i], yl[i], nl[i]))
        if buf:
            _replay(cur_m, buf, settle, cells, meta, panel, quotes.get(cur_m, ()))
            nmkt += 1
        con.close()
        log('l2 %s: %d rows, %d markets (%.0fs)'
            % (os.path.basename(fp)[:60], nrows, nmkt, time.time() - t0))

    # ---- write artifacts ---------------------------------------------------
    rows = []
    for (m, variant, mb, tb), v in cells.items():
        r = {'market_ticker': m, 'variant': variant, 'mn_band': mb, 'tte_band': tb, 'n': v[0]}
        for k in range(10):
            r['bid_%d' % (k + 1)] = v[1 + k]
            r['ask_%d' % (k + 1)] = v[11 + k]
        r['sum_mid'] = v[21]
        r['sum_spread_c'] = v[22]
        r['sum_top10'] = v[23]
        r['series_ticker'] = meta[(m, variant)]['series']
        r['category'] = meta[(m, variant)]['category']
        rows.append(r)
    da.write_df_parquet(pd.DataFrame(rows), os.path.join(OUT, 'depth_cells.parquet'))
    da.write_df_parquet(pd.DataFrame(panel, columns=[
        'market_ticker', 'variant', 'ts_utc', 'state_ts', 'bid_e4', 'ask_e4', 'mid_c', 'top10',
        'top10_bid', 'top10_ask', 'tte_min']), os.path.join(OUT, 'depth_panel.parquet'))
    mrows = [dict(market_ticker=k[0], variant=k[1], series_ticker=v['series'],
                  category=v['category'], neg=v['neg'], nonempty_snap=v['nonempty'],
                  samples=v['samples'], skipped_pre_snapshot=v['skipped_pre'],
                  one_sided=v['one_sided'], crossed=v['crossed'], n_cmp=v['n_cmp'],
                  n_match=v['n_match'], n_near=v['n_near'], err_abs=v['err_abs'])
             for k, v in meta.items()]
    da.write_df_parquet(pd.DataFrame(mrows), os.path.join(OUT, 'depth_market_meta.parquet'))
    log('l2: %d cells, %d panel rows, %d (market, variant) pairs'
        % (len(rows), len(panel), len(mrows)))


def _replay(m, rows, settle, cells, meta, panel, quotes):
    """Replay one market under both candidate orders; the gate arbitrates later."""
    for v in VARIANTS:
        ordered = rows if v == 'seq' else sorted(rows, key=lambda r: r[0])
        _replay_one(m, v, ordered, settle, cells, meta, panel, quotes)


def _replay_one(m, variant, rows, settle, cells, meta, panel, quotes):
    """Replay, sampling the book on the grids and scoring it against L1 truth.

    The gate walks the market's own L1 quote timeline: the book state held
    between two consecutive L2 events is the state that must reproduce every L1
    quote stamped inside that interval.  Comparing on a fixed grid instead (the
    first cut of this pass) scored the state against an L1 row up to a second
    stale, which on a fast crypto book is a mismatch by construction.
    """
    mm = meta[(m, variant)]
    b = Book()
    qi, nq = 0, len(quotes)
    for (ts, mg, sd, px, dl, yl, nl) in rows:
        while qi < nq and quotes[qi][0] < ts:
            qt, qb, qa = quotes[qi]
            qi += 1
            if not b.init or not b.yes or not b.no:
                continue
            rb, ra = max(b.yes), 10000 - max(b.no)
            mm['n_cmp'] += 1
            if rb == qb and ra == qa:
                mm['n_match'] += 1
            else:
                mm['err_abs'] += abs(rb - qb) + abs(ra - qa)
                if abs(rb - qb) <= 100 and abs(ra - qa) <= 100:
                    mm['n_near'] += 1
        g10 = ts // (GRID_SHAPE_S * 1_000_000)
        g60 = ts // (GRID_PANEL_S * 1_000_000)
        if b.b10 is None:
            b.b10, b.b60 = g10, g60
        elif g10 != b.b10:
            # the state carried into this row is the state at the end of bucket b10
            if b.init:
                _emit(m, variant, b, (b.b10 + 1) * GRID_SHAPE_S * 1_000_000, settle, cells,
                      meta, panel, g60 != b.b60)
            b.b10, b.b60 = g10, g60
        b.last_ts = ts
        if mg == 'snapshot':
            b.yes, b.no = {}, {}
            for arr, dd in ((yl, b.yes), (nl, b.no)):
                if arr and arr != '[]':
                    for p, s in json.loads(arr):
                        if s > 0:
                            dd[p] = s
            b.init = True
            if b.yes or b.no:
                mm['nonempty'] = True
        else:
            if not b.init:
                mm['skipped_pre'] += 1
                continue
            if px < 0:
                continue
            dd = b.yes if sd == 'yes' else b.no
            v = dd.get(px, 0) + dl
            if v > 0:
                dd[px] = v
            else:
                if v < 0:
                    mm['neg'] += 1
                dd.pop(px, None)


def _emit(m, variant, b, ts, settle, cells, meta, panel, want_panel):
    close = settle.get(m)
    if close is None:
        return
    tte = (close - ts) / 60_000_000.0
    tb = tte_band(tte)
    if tb is None:
        return
    bids = top_levels(b.yes, True)
    asks_no = top_levels(b.no, True)       # NO bids, best = highest no price
    if not bids or not asks_no:
        meta[(m, variant)]['one_sided'] += 1
        return
    best_bid = bids[0][0]
    best_ask = 10000 - asks_no[0][0]
    if best_ask <= best_bid:
        meta[(m, variant)]['crossed'] += 1
        return                              # crossed/locked snapshot: not a valid quote
    mid = (best_bid + best_ask) / 200.0
    mb = mn_band(mid)
    v = cells[(m, variant, mb, tb)]
    v[0] += 1
    tot = 0.0
    for k in range(10):
        db = bids[k][1] / 10000.0 if k < len(bids) else 0.0
        dk = asks_no[k][1] / 10000.0 if k < len(asks_no) else 0.0
        v[1 + k] += db
        v[11 + k] += dk
        tot += db + dk
    v[21] += mid
    v[22] += (best_ask - best_bid) / 100.0
    v[23] += tot
    meta[(m, variant)]['samples'] += 1
    if want_panel:
        tb_bid = sum(x[1] for x in bids) / 10000.0
        tb_ask = sum(x[1] for x in asks_no) / 10000.0
        panel.append((m, variant, ts, b.last_ts, best_bid, best_ask, mid, tb_bid + tb_ask,
                      tb_bid, tb_ask, tte))


# ---------------------------------------------------------------- gate + vol
def pass_gate():
    """Known-answer gate (counterexample C1): scored inside the replay against the
    market's own L1 quote timeline; this pass only summarises it."""
    con = con_new()
    df = con.execute("""SELECT market_ticker, variant, n_cmp, n_match, n_near, err_abs
                        FROM read_parquet('%s') WHERE n_cmp > 0"""
                     % os.path.join(OUT, 'depth_market_meta.parquet')).df()
    da.write_df_parquet(df, os.path.join(OUT, 'l2_gate.parquet'))
    df['agree'] = df.n_match / df.n_cmp
    best = df.sort_values(['agree', 'n_cmp']).groupby('market_ticker').tail(1)
    near = (best.n_match + best.n_near) / best.n_cmp
    log('gate: %d (market, variant) pairs, %d markets, %d comparisons; best-variant median '
        'agreement %.4f (within 1 tick %.4f), %d pass at 99%% (%s)'
        % (len(df), df.market_ticker.nunique(), int(best.n_cmp.sum()), best.agree.median(),
           near.median(), int((best.agree >= 0.99).sum()),
           best.variant.value_counts().to_dict()))


def pass_vol():
    con = con_new()
    root = da.warehouse_root()
    tf = []
    for d in L2_DATES:
        tf += glob.glob('%s/facts/trades/category=Crypto/subcategory=*/date=%s/*.csv.gz' % (root, d))
        tf += glob.glob('%s/facts/trades/category=Sports/subcategory=*/date=%s/*.csv.gz' % (root, d))
    sql = """
    WITH m AS (SELECT DISTINCT market_ticker FROM read_parquet('{panel}')),
    t AS (SELECT market_ticker, ts_utc, count_e4 / 10000.0 AS contracts
          FROM read_csv({tf}, union_by_name=true) WHERE market_ticker IN (SELECT market_ticker FROM m))
    SELECT market_ticker, (ts_utc // {g}) * {g} AS ts_bucket,
           sum(contracts) AS contracts, count(*) AS n_trades
    FROM t GROUP BY 1, 2"""
    df = con.execute(sql.format(panel=os.path.join(OUT, 'depth_panel.parquet'),
                                tf=da.sql_file_list(sorted(tf)), g=GRID_PANEL_S * 1_000_000)).df()
    da.write_df_parquet(df, os.path.join(OUT, 'panel_volume.parquet'))
    log('vol: %d (market, 60s) buckets' % len(df))


# ------------------------------------------------- SF2-K cut 2: strike ladder
def pass_ladder():
    """crypto_hourly has ~180 strikes per event; PM's venue has no ladder at all.

    Emits, per (event, 5-minute bucket, market): last L1 quote + parsed strike.
    The implied spot (strike where mid crosses 50c) is computed in the analyze step.
    """
    con = con_new()
    root = da.warehouse_root()
    files = []
    for d in L2_DATES:
        files += glob.glob('%s/facts/orderbooks_l1/category=Crypto/subcategory=*/date=%s/*.parquet' % (root, d))
    series = "','".join(sorted(taxonomy.CRYPTO_HOURLY))
    sql = """
    WITH q AS (
      SELECT market_ticker, series_ticker, event_ticker, ts_utc,
             yes_bid_e4, yes_ask_e4, yes_bid_qty_e4, yes_ask_qty_e4,
             (ts_utc // 300000000) * 300000000 AS ts_bucket,
             row_number() OVER (PARTITION BY market_ticker, (ts_utc // 300000000)
                                ORDER BY ts_utc DESC) AS rn
      FROM read_parquet({files})
      WHERE series_ticker IN ('{series}') AND yes_bid_e4 > 0 AND yes_ask_e4 < 10000
    )
    SELECT q.market_ticker, q.series_ticker, q.event_ticker, q.ts_bucket,
           (q.yes_bid_e4 + q.yes_ask_e4) / 200.0 AS mid_c,
           (q.yes_ask_e4 - q.yes_bid_e4) / 100.0 AS spread_c,
           (q.yes_bid_qty_e4 + q.yes_ask_qty_e4) / 10000.0 AS l1_depth,
           q.yes_bid_qty_e4 / 10000.0 AS bid_depth, q.yes_ask_qty_e4 / 10000.0 AS ask_depth,
           (s.close_us - q.ts_utc) / 60000000.0 AS tte_min
    FROM q JOIN read_parquet('{settle}') s ON q.market_ticker = s.ticker
    WHERE q.rn = 1 AND s.close_us <= {cutoff}"""
    df = con.execute(sql.format(files=da.sql_file_list(sorted(files)), series=series,
                                settle=os.path.join(OUT, 'settlement.parquet'),
                                cutoff=SNAPSHOT_CUTOFF_US)).df()
    da.write_df_parquet(df, os.path.join(OUT, 'ladder.parquet'))
    log('ladder: %d (market, 5min) rows, %d events' % (len(df), df.event_ticker.nunique()))


PASSES = {'settle': pass_settle, 'census': pass_census, 'l1': pass_l1, 'trades': pass_trades,
          'l2': pass_l2, 'gate': pass_gate, 'vol': pass_vol, 'ladder': pass_ladder}
ORDER = ['settle', 'census', 'l1', 'trades', 'l2', 'gate', 'vol', 'ladder']

if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)
    want = sys.argv[1:] or ['all']
    todo = ORDER if want == ['all'] else want
    for p in todo:
        log('=== pass %s ===' % p)
        PASSES[p]()
    log('done')
