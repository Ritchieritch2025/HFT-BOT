#!/usr/bin/env python
"""Event-level queue simulation for the sports favorite-fade line (step 1 of the
money path; follows sports_maker_replay.py).

Question: quoting k lots passively on the cheap side of favorite-end books,
what do we ACTUALLY capture once queue position and sweep-moment adverse
selection are priced in?

Two quoting programs, both "buy the cheap side from favorite buyers":
  ask: sit on yes_ask in [90,99]  -> we end up long NO at (100-ask)
  bid: sit on yes_bid in [1,10]   -> we end up long YES at bid

Queue model (conservative, from L1 only):
  - join back of displayed queue when (re)joining a level; level move = re-join
    (queue position lost); best leaves band = pull quote
  - displayed qty drops without a trade -> assume cancels BEHIND us except we
    clamp queue_ahead to displayed (can't have more ahead than shown)
  - a trade at our level first consumes queue ahead, overflow fills us
  - fills are one round per market-program: fill sequence recorded, then
    filled(k) = first fills up to k lots; position held to settlement
  - no market impact from our own quote (replay optimism — stated caveat)

Maker fees netted per replication.fees. Read-only; scratch output only.
Phases: tape | sim | analyze  (or all)   Output: $QS_OUT
"""
import sys, os, json, re, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication import data_access as da
from replication.fees import NONSTANDARD, MAKER_RATE

BASE = os.environ.get('QS_OUT', '/home/ubuntu/queue_sim_20260727')
FLB = '/home/ubuntu/flb_expt_20260727'
K_GRID = [1, 2, 5, 10, 20, 50]
EV_CAP_EVENTS = 300000  # per-market event cap (runaway guard, logged)

# fee-netted positive books (favorite_fade_net_by_series.md), exclusions dropped
TARGET_SERIES = [
    'KXITFMATCH', 'KXMLBKS', 'KXMLBHIT', 'KXMLBTEAMTOTAL', 'KXMLBTOTAL',
    'KXNBASUMMERTOTAL', 'KXITFWMATCH', 'KXITFWDOUBLES', 'KXATPCHALLENGERMATCH',
    'KXITFDOUBLES', 'KXWNBATOTAL', 'KXCS2MAP', 'KXCLUBFTOTAL', 'KXMLBSPREAD',
    'KXPGAROUNDSCORE', 'KXMLBHRR', 'KXMLBF3', 'KXNBASUMMERSPREAD', 'KXATPGTOTAL',
    'KXMLBF7', 'KXWCSTART', 'KXMLBF5SPREAD', 'KXMLBOUTS', 'KXLOLMAP',
    'KXVALORANTMAP', 'KXNPBTOTAL', 'KXCLUBFGAME', 'KXATPSETWINNER', 'KXWNBAPTS',
    'KXMLBF5', 'KXMLBF5TOTAL', 'KXMLBHR', 'KXATPEXACTMATCH', 'KXWNBA2QTOTAL',
    'KXNPBSPREAD', 'KXATPMATCH', 'KXMLBGAME', 'KXWTAMATCH', 'KXLMBGAME',
]
START_PAT = re.compile(r'-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})')
MONTHS = {m: i + 1 for i, m in enumerate(
    ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'])}


def start_us_of(ticker):
    m = START_PAT.search(ticker or '')
    if not m:
        return None
    import datetime as dt
    try:
        t = dt.datetime(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), tzinfo=dt.timezone.utc)
        return int(t.timestamp() * 1e6) + 14_400_000_000  # ET -> UTC (EDT)
    except Exception:
        return None


def _con():
    return da.duckdb_connect(threads=2, memory_limit='6GB', temp_dir=BASE + '/tmp')


def sports_files(kind):
    fs = da.trade_files() if kind == 'trades' else da.l1_files()
    return [f for f in fs if 'category=Sports/' in f]


def series_sql():
    return '(' + ','.join("'%s'" % s for s in TARGET_SERIES) + ')'


def phase_tape():
    con = _con()
    tf, lf = sports_files('trades'), sports_files('l1')
    print('tape: %d trade files, %d l1 files' % (len(tf), len(lf)), flush=True)
    con.execute("CREATE VIEW idx AS SELECT * FROM read_parquet('%s/settlement_index.parquet')" % FLB)
    # markets of target series with settled result and at least one in-band trade
    con.execute("""
CREATE TABLE m AS
SELECT t.market_ticker, any_value(t.series_ticker) AS series, any_value(s.result) AS result,
       any_value(s.anchor_us) AS anchor_us
FROM (SELECT market_ticker, series_ticker, yes_price_e4/100.0 AS yes_c
      FROM read_csv_auto(%s, union_by_name=1)
      WHERE series_ticker IN %s) t
JOIN idx s ON t.market_ticker = s.ticker
WHERE (t.yes_c >= 90 AND t.yes_c <= 99) OR (t.yes_c >= 1 AND t.yes_c <= 10)
GROUP BY t.market_ticker
""" % (da.sql_file_list(tf), series_sql()))
    n = con.execute('SELECT count(*), count(DISTINCT series) FROM m').fetchone()
    print('tape: %d markets across %d series' % n, flush=True)
    con.execute("COPY m TO '%s/markets.parquet' (FORMAT PARQUET)" % BASE)
    con.execute("""
COPY (
  SELECT l.market_ticker, m.series, l.ts_utc,
         CAST(l.yes_bid_e4/100.0 AS FLOAT) AS bid_c, CAST(l.yes_bid_qty_e4/1e4 AS FLOAT) AS bid_q,
         CAST(l.yes_ask_e4/100.0 AS FLOAT) AS ask_c, CAST(l.yes_ask_qty_e4/1e4 AS FLOAT) AS ask_q,
         CAST(1 AS TINYINT) AS ev
  FROM read_parquet(%s, union_by_name=true) l
  JOIN m ON l.market_ticker = m.market_ticker
) TO '%s/tape_quotes' (FORMAT PARQUET, PARTITION_BY (series))
""" % (da.sql_file_list(lf), BASE))
    print('tape: quotes dumped', flush=True)
    con.execute("""
COPY (
  SELECT t.market_ticker, m.series, t.ts_utc,
         CAST(t.yes_price_e4/100.0 AS FLOAT) AS px_c, CAST(t.count_e4/1e4 AS FLOAT) AS qty,
         CAST(CASE WHEN CAST(t.taker_side AS VARCHAR) IN ('yes','true') THEN 1 ELSE 0 END AS TINYINT) AS taker_yes
  FROM read_csv_auto(%s, union_by_name=1, types={'taker_side': 'VARCHAR'}) t
  JOIN m ON t.market_ticker = m.market_ticker
) TO '%s/tape_trades' (FORMAT PARQUET, PARTITION_BY (series))
""" % (da.sql_file_list(tf), BASE))
    print('tape done', flush=True)


def simulate_market(qts, tds, result):
    """qts: (ts, bid_c, bid_q, ask_c, ask_q) arrays; tds: (ts, px, qty, taker_yes).
    Returns fills per program: list of (ts, price_c, vol)."""
    fills = {'ask': [], 'bid': []}
    state = {'ask': [None, 0.0], 'bid': [None, 0.0]}  # [our_price, queue_ahead]
    qi, ti, nq, nt = 0, 0, len(qts[0]), len(tds[0])
    while qi < nq or ti < nt:
        take_trade = ti < nt and (qi >= nq or tds[0][ti] <= qts[0][qi])
        if take_trade:
            ts, px, qty, tk = tds[0][ti], tds[1][ti], tds[2][ti], tds[3][ti]
            prog = 'ask' if tk == 1 else 'bid'   # yes-taker consumes ask, no-taker consumes bid
            st = state[prog]
            if st[0] is not None and abs(px - st[0]) < 0.051:
                over = qty - st[1]
                st[1] = max(0.0, st[1] - qty)
                if over > 0:
                    fills[prog].append((int(ts), float(st[0]), float(over)))
            ti += 1
        else:
            ts, bc, bq, ac, aq = (a[qi] for a in qts)
            for prog, px, q, lo, hi in (('ask', ac, aq, 90.0, 99.0), ('bid', bc, bq, 1.0, 10.0)):
                st = state[prog]
                if px is None or not (lo <= px <= hi) or q <= 0:
                    st[0] = None
                elif st[0] is None or abs(px - st[0]) > 0.051:
                    st[0], st[1] = float(px), float(q)   # (re)join back of queue
                else:
                    st[1] = min(st[1], float(q))          # clamp to displayed
            qi += 1
    return fills


def phase_sim():
    import pandas as pd
    import numpy as np
    import glob as g
    mk = da.read_parquet_df(BASE + '/markets.parquet')
    meta = {r.market_ticker: (r.result, r.anchor_us) for r in mk.itertuples()}
    out = []
    trunc = 0
    con = _con()
    for si, series in enumerate(TARGET_SERIES):
        qd = '%s/tape_quotes/series=%s' % (BASE, series)
        td = '%s/tape_trades/series=%s' % (BASE, series)
        if not (g.glob(qd + '/*.parquet') and g.glob(td + '/*.parquet')):
            continue
        mkts = [m for m in mk[mk['series'] == series]['market_ticker']]
        for b0 in range(0, len(mkts), 400):
            batch = mkts[b0:b0 + 400]
            bl = '(' + ','.join("'%s'" % m for m in batch) + ')'
            q = con.execute("""SELECT market_ticker, ts_utc, bid_c, bid_q, ask_c, ask_q
                               FROM read_parquet('%s/*.parquet') WHERE market_ticker IN %s
                               ORDER BY market_ticker, ts_utc""" % (qd, bl)).df()
            t = con.execute("""SELECT market_ticker, ts_utc, px_c, qty, taker_yes
                               FROM read_parquet('%s/*.parquet') WHERE market_ticker IN %s
                               ORDER BY market_ticker, ts_utc""" % (td, bl)).df()
            qg = {k: v for k, v in q.groupby('market_ticker', sort=False)}
            tg = {k: v for k, v in t.groupby('market_ticker', sort=False)}
            for mkt in batch:
                qv, tv = qg.get(mkt), tg.get(mkt)
                if qv is None or tv is None or mkt not in meta:
                    continue
                if len(qv) > EV_CAP_EVENTS:
                    qv = qv.iloc[:EV_CAP_EVENTS]; trunc += 1
                qts = (qv['ts_utc'].to_numpy(), qv['bid_c'].to_numpy(), qv['bid_q'].to_numpy(),
                       qv['ask_c'].to_numpy(), qv['ask_q'].to_numpy())
                qts = (qts[0], qts[1], qts[2], qts[3], qts[4])
                tds = (tv['ts_utc'].to_numpy(), tv['px_c'].to_numpy(),
                       tv['qty'].to_numpy(), tv['taker_yes'].to_numpy())
                fl = simulate_market((qts[0], qts[1], qts[2], qts[3], qts[4]), tds, meta[mkt][0])
                for prog, evs in fl.items():
                    for ts, px, vol in evs:
                        out.append((series, mkt, prog, ts, px, vol))
        print('sim %d/%d %s: fills so far %d' % (si + 1, len(TARGET_SERIES), series, len(out)), flush=True)
        time.sleep(1)
    df = pd.DataFrame(out, columns=['series', 'market_ticker', 'program', 'ts_utc', 'price_c', 'vol'])
    da.write_df_parquet(df, BASE + '/fills.parquet')
    json.dump({'fill_events': len(df), 'truncated_markets': trunc},
              open(BASE + '/sim_stats.json', 'w'), indent=1)
    print('sim done: %d fill events, %d truncated' % (len(df), trunc), flush=True)


def phase_analyze():
    import pandas as pd
    import numpy as np
    fills = da.read_parquet_df(BASE + '/fills.parquet')
    mk = da.read_parquet_df(BASE + '/markets.parquet')
    meta = mk.set_index('market_ticker')
    mm = {s: (v[0] if isinstance(v, tuple) else v) for s, v in NONSTANDARD.items()}
    rows, daily_rows = [], []
    fills = fills.sort_values(['market_ticker', 'program', 'ts_utc'])
    for (mkt, prog), g in fills.groupby(['market_ticker', 'program'], sort=False):
        if mkt not in meta.index:
            continue
        res, anchor = meta.loc[mkt, 'result'], meta.loc[mkt, 'anchor_us']
        series = meta.loc[mkt, 'series']
        start = start_us_of(mkt)
        cum = 0.0
        for r in g.itertuples():
            take_all = r.vol
            for k in K_GRID:
                take_k = max(0.0, min(k - cum, take_all))
                if take_k <= 0:
                    continue
                # cheap-side economics
                if prog == 'ask':   # sold YES at price -> long NO at 100-price
                    cost = 100.0 - r.price_c
                    win = (res == 'no')
                else:               # bought YES at price
                    cost = r.price_c
                    win = (res == 'yes')
                fee = mm.get(series, 0) * MAKER_RATE * 100 * (r.price_c / 100) * (1 - r.price_c / 100)
                pnl = ((100.0 - cost) if win else -cost) - fee
                phase = ('unknown' if start is None else ('pregame' if r.ts_utc < start else 'ingame'))
                rows.append((series, mkt, prog, k, phase, take_k, cost, pnl * take_k,
                             int(anchor // 86_400_000_000)))
            cum += take_all
    df = pd.DataFrame(rows, columns=['series', 'market', 'program', 'k', 'phase',
                                     'lots', 'cost_c', 'pnl_c', 'day'])
    da.write_df_parquet(df, BASE + '/positions.parquet')

    cap = (df.groupby(['k', 'program']).agg(
        markets=('market', 'nunique'), lots=('lots', 'sum'),
        pnl_usd=('pnl_c', lambda x: round(float(x.sum()) / 100, 2))).reset_index())
    cap['ev_c_per_lot'] = (df.groupby(['k', 'program'])['pnl_c'].sum()
                           / df.groupby(['k', 'program'])['lots'].sum()).round(3).values
    cap.to_csv(BASE + '/capture_curve.csv', index=False)

    bills = []
    for k in K_GRID:
        dk = df[df['k'] == k]
        daily = dk.groupby('day')['pnl_c'].sum().sort_index() / 100.0
        cum = daily.cumsum()
        worst_market = dk.groupby('market')['pnl_c'].sum().min() / 100.0
        bills.append(dict(k=k, total_usd=round(float(dk['pnl_c'].sum()) / 100, 2),
                          lots=round(float(dk['lots'].sum()), 1),
                          markets=int(dk['market'].nunique()),
                          worst_single_market_usd=round(float(worst_market), 2),
                          worst_day_usd=round(float(daily.min()) if len(daily) else 0, 2),
                          max_drawdown_usd=round(float((cum - cum.cummax()).min()) if len(cum) else 0, 2)))
    pd.DataFrame(bills).to_csv(BASE + '/worst_bills_by_k.csv', index=False)
    (df.groupby(['k', 'program', 'phase']).agg(lots=('lots', 'sum'),
        pnl_usd=('pnl_c', lambda x: round(float(x.sum()) / 100, 2))).reset_index()
     ).to_csv(BASE + '/phase_breakdown.csv', index=False)
    (df[df['k'] == 10].groupby('series').agg(lots=('lots', 'sum'),
        pnl_usd=('pnl_c', lambda x: round(float(x.sum()) / 100, 2)),
        markets=('market', 'nunique')).reset_index().sort_values('pnl_usd', ascending=False)
     ).to_csv(BASE + '/series_k10.csv', index=False)
    print('analyze done', flush=True)


PHASES = {'tape': phase_tape, 'sim': phase_sim, 'analyze': phase_analyze}

if __name__ == '__main__':
    os.makedirs(BASE + '/tmp', exist_ok=True)
    for name in (['tape', 'sim', 'analyze'] if sys.argv[1] == 'all' else [sys.argv[1]]):
        print('== phase %s ==' % name, flush=True)
        PHASES[name]()
