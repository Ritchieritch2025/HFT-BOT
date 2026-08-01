#!/usr/bin/env python
"""Who Profits from Prediction? — Kalshi replication, analysis + results PDF.

Consumes cells.parquet produced by who_profits.py (run on the prod box) and
emits results.json + a multi-page PDF.

Usage: python who_profits_analyze.py <cells.parquet> <outdir>
"""
import sys, os, json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

FAM_ORDER = ['crypto_15m', 'crypto_hourly', 'sports_game', 'sports_prop',
             'politics_events', 'econ']
FAM_LABEL = {'crypto_15m': 'Crypto 15-min', 'crypto_hourly': 'Crypto hourly/daily',
             'sports_game': 'Sports game outcome', 'sports_prop': 'Sports props',
             'politics_events': 'Politics / elections', 'econ': 'Economics'}
BANDS = ['gt60m', '10-60m', '5-10m', '2-5m', 'lt2m']
BAND_LABEL = {'gt60m': '>60 min', '10-60m': '10-60 min', '5-10m': '5-10 min',
              '2-5m': '2-5 min', 'lt2m': '<2 min'}
BUCKETS = ['01-05', '05-10', '10-20', '20-35', '35-50', '50-65', '65-80', '80-90',
           '90-95', '95-99']
BUCKET_MID = {'01-05': 3, '05-10': 7.5, '10-20': 15, '20-35': 27.5, '35-50': 42.5,
              '50-65': 57.5, '65-80': 72.5, '80-90': 85, '90-95': 92.5, '95-99': 97}
SEED = 20260727
BOOT = 1000
MIN_COHORT_QTY = 50.0     # contracts, for cohort-level regressions


# ---------------------------------------------------------------- helpers
def wratio(df, num, den):
    d = df[den].sum()
    return float(df[num].sum() / d) if d > 0 else np.nan


def boot_ratio(mkt_df, num, den, seed=SEED, n_boot=BOOT):
    """Cluster bootstrap (cluster = market) CI for sum(num)/sum(den)."""
    a = mkt_df[num].to_numpy(float)
    b = mkt_df[den].to_numpy(float)
    n = len(a)
    if n < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    bs = a[idx].sum(axis=1) / np.where(b[idx].sum(axis=1) == 0, np.nan, b[idx].sum(axis=1))
    return tuple(float(x) for x in np.nanpercentile(bs, [2.5, 97.5]))


def ols(y, X):
    """OLS with intercept. X: (n,k) without intercept. Returns dict."""
    y = np.asarray(y, float)
    X = np.asarray(X, float)
    ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
    y, X = y[ok], X[ok]
    if len(y) < len(X.T) + 5:
        return None
    A = np.column_stack([np.ones(len(y)), X])
    with np.errstate(all='ignore'):     # BLAS emits spurious flags on wide fits
        beta, *_ = np.linalg.lstsq(A, y, rcond=None)
        ss_res = float(resid @ resid)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    # HC1 robust standard errors
    with np.errstate(all='ignore'):
        XtX_inv = np.linalg.pinv(A.T @ A)
        Ar = A * resid[:, None]      # meat = Σ r² a aᵀ, formed this way to stay in range
        meat = Ar.T @ Ar
        n, k = A.shape
        cov = XtX_inv @ meat @ XtX_inv * (n / max(n - k, 1))
        se = np.sqrt(np.abs(np.diag(cov)))
    return {'beta': beta.tolist(), 'se': se.tolist(),
            't': (beta / np.where(se == 0, np.nan, se)).tolist(),
            'r2': 1 - ss_res / ss_tot if ss_tot > 0 else np.nan, 'n': int(n)}


# ---------------------------------------------------------------- load
def load(path):
    import duckdb
    from replication import fees
    df = duckdb.connect().execute("SELECT * FROM read_parquet('%s')" % path).df()
    df = df[df['qty'] > 0].copy()
    # taker-side per-cell weighted sums are already in the file; add helpers
    df['w_maker_notional'] = df['qty'] * 100.0 - df['w_paid_c']
    # w_fee_c was computed at the DEFAULT taker rate (7*p*(1-p) c). Rescale by the
    # per-series multipliers of the published schedule: takers pay Mt x that,
    # makers pay Mm x a quarter of it (0.0175 vs 0.07).
    mult = df['series'].map(lambda s: fees.multipliers(s))
    df['mm'] = [m[0] for m in mult]
    df['mt'] = [m[1] for m in mult]
    df['w_fee_taker'] = df['w_fee_c'] * df['mt']
    df['w_fee_maker'] = df['w_fee_c'] * df['mm'] * (fees.MAKER_RATE / fees.TAKER_RATE)
    return df


def market_level(df):
    """Collapse to one row per (family, market) — the sample unit for headlines."""
    g = df.groupby(['family', 'market_ticker'], as_index=False).sum(numeric_only=True)
    return g


def cohort_level(df):
    """(family, market, taker_side) — the closest available analog to the
    paper's wallet unit: one directional/execution pair per role per market."""
    g = df.groupby(['family', 'market_ticker', 'taker_side'], as_index=False).sum(
        numeric_only=True)
    for tag in ('b', 'l', 'g'):
        q = g['qty_%s' % tag].replace(0, np.nan)
        g['dir_%s' % tag] = g['w_dir_%s' % tag] / q
        g['exe_%s' % tag] = g['w_exe_%s' % tag] / q
    g['acc'] = g['w_win'] / g['qty']
    g['pdist'] = g['w_pdist_c'] / g['qty'] / 100.0        # dollars from 0.50
    g['edge_d'] = g['w_total_c'] / g['qty'] / 100.0        # dollars per contract
    return g


# ---------------------------------------------------------------- experiments
def e1_e2_e3(df):
    """Inversion, excess accuracy, decomposition — per family, taker view."""
    mk = market_level(df)
    out = {}
    for fam, d in df.groupby('family'):
        m = mk[mk['family'] == fam]
        qty = d['qty'].sum()
        notional = d['w_paid_c'].sum() / 100.0
        mkr_notional = d['w_maker_notional'].sum() / 100.0
        acc = wratio(d, 'w_win', 'qty')
        implied = wratio(d, 'w_implied_c', 'qty') / 100.0
        gross_c = wratio(d, 'w_total_c', 'qty')
        fee_c = wratio(d, 'w_fee_taker', 'qty')
        mfee_c = wratio(d, 'w_fee_maker', 'qty')
        rec = {
            'markets': int(d['market_ticker'].nunique()),
            'trades': int(d['n'].sum()),
            'contracts': float(qty),
            'taker_notional_usd': notional,
            'maker_notional_usd': mkr_notional,
            'taker_accuracy': acc,
            'taker_accuracy_prints': wratio(d, 'n_win', 'n'),
            'price_implied_accuracy': implied,
            'taker_excess_accuracy': acc - implied,
            'maker_accuracy': 1 - acc,
            'maker_excess_accuracy': (1 - acc) - implied,
            'mean_paid_c': wratio(d, 'w_paid_c', 'qty'),
            'taker_gross_c': gross_c,
            'taker_fee_c': fee_c,
            'taker_net_c': gross_c - fee_c,
            'maker_gross_c': -gross_c,
            'maker_fee_c': mfee_c,
            'maker_net_c': -gross_c - mfee_c,
            'maker_fee_volume_share': float(d.loc[d['mm'] > 0, 'qty'].sum() / qty),
            'taker_gross_usd': d['w_total_c'].sum() / 100.0,
            'taker_fee_usd': d['w_fee_taker'].sum() / 100.0,
            'taker_net_usd': (d['w_total_c'].sum() - d['w_fee_taker'].sum()) / 100.0,
            'maker_gross_usd': -d['w_total_c'].sum() / 100.0,
            'maker_fee_usd': d['w_fee_maker'].sum() / 100.0,
            'maker_net_usd': (-d['w_total_c'].sum() - d['w_fee_maker'].sum()) / 100.0,
        }
        rec['taker_roi'] = rec['taker_net_usd'] / notional if notional else np.nan
        rec['maker_roi'] = rec['maker_net_usd'] / mkr_notional if mkr_notional else np.nan
        rec['exchange_fee_usd'] = rec['taker_fee_usd'] + rec['maker_fee_usd']
        # decomposition under the three benchmarks (taker view, cents/contract)
        for tag, name in (('b', 'bvwap'), ('l', 'lvwap'), ('g', 'lag')):
            rec['dir_%s_c' % name] = wratio(d, 'w_dir_%s' % tag, 'qty_%s' % tag)
            rec['exe_%s_c' % name] = wratio(d, 'w_exe_%s' % tag, 'qty_%s' % tag)
        # cluster-bootstrap CIs (cluster = market)
        rec['ci_taker_accuracy'] = boot_ratio(m, 'w_win', 'qty')
        m2 = m.assign(_net=m['w_total_c'] - m['w_fee_taker'])
        rec['ci_taker_net_c'] = boot_ratio(m2, '_net', 'qty')
        m3 = m.assign(_mk=-m['w_total_c'])
        rec['ci_maker_gross_c'] = boot_ratio(m3, '_mk', 'qty')
        m4 = m.assign(_mn=-m['w_total_c'] - m['w_fee_maker'])
        rec['ci_maker_net_c'] = boot_ratio(m4, '_mn', 'qty')
        out[fam] = rec
    return out


def e4_benchmark_gradient(coh):
    """exec ~ directional across cohorts under three benchmarks (paper Table 4)."""
    out = {}
    for fam, d in coh.groupby('family'):
        d = d[d['qty'] >= MIN_COHORT_QTY]
        rec = {'n_cohorts': int(len(d))}
        for tag, name in (('b', 'bvwap'), ('l', 'lvwap'), ('g', 'lag')):
            r = ols(d['exe_%s' % tag], d[['dir_%s' % tag]].to_numpy())
            if r:
                rec[name] = {'beta': r['beta'][1], 't': r['t'][1], 'r2': r['r2'],
                             'n': r['n']}
        out[fam] = rec
    return out


def e5_lifecycle(df):
    out = {}
    for fam, d in df.groupby('family'):
        rows = []
        for band in BANDS:
            b = d[d['band'] == band]
            if b['qty'].sum() <= 0:
                continue
            rows.append({
                'band': band,
                'contracts': float(b['qty'].sum()),
                'trades': int(b['n'].sum()),
                'maker_gross_c': -wratio(b, 'w_total_c', 'qty'),
                'maker_net_c': -wratio(b, 'w_total_c', 'qty') - wratio(b, 'w_fee_maker', 'qty'),
                'taker_net_c': wratio(b, 'w_total_c', 'qty') - wratio(b, 'w_fee_taker', 'qty'),
                'taker_fee_c': wratio(b, 'w_fee_taker', 'qty'),
                'maker_markout5_c': wratio(b, 'w_mkout_c', 'qty_mk'),
                'taker_accuracy': wratio(b, 'w_win', 'qty'),
                'mean_paid_c': wratio(b, 'w_paid_c', 'qty'),
            })
        out[fam] = rows
    return out


def e6_interaction(coh):
    """edge ~ acc + pdist + acc*pdist  (paper Table 7)."""
    out = {}
    for fam, d in coh.groupby('family'):
        d = d[d['qty'] >= MIN_COHORT_QTY]
        X = np.column_stack([d['acc'], d['pdist'], d['acc'] * d['pdist']])
        r = ols(d['edge_d'], X)
        if not r:
            continue
        b0, b1, b2, b3 = r['beta']
        mv = {p: 0.10 * (b1 + b3 * abs(p - 0.5)) for p in (0.50, 0.65, 0.80, 0.95)}
        out[fam] = {'beta_acc': b1, 'beta_pdist': b2, 'beta_inter': b3,
                    't_inter': r['t'][3], 'r2': r['r2'], 'n': r['n'],
                    'marginal_value_10pt': {str(k): v for k, v in mv.items()}}
    return out


def e7_calibration(df):
    out = {}
    for fam, d in df.groupby('family'):
        rows = []
        for bk in BUCKETS:
            b = d[d['bucket'] == bk]
            if b['qty'].sum() <= 0:
                continue
            mkts = b.groupby('market_ticker', as_index=False).sum(numeric_only=True)
            lo, hi = boot_ratio(mkts, 'w_win', 'qty')
            rows.append({'bucket': bk, 'mid_c': BUCKET_MID[bk],
                         'paid_c': wratio(b, 'w_paid_c', 'qty'),
                         'win_rate': wratio(b, 'w_win', 'qty'),
                         'ci': (lo, hi),
                         'contracts': float(b['qty'].sum()),
                         'markets': int(b['market_ticker'].nunique())})
        out[fam] = rows
    return out


def e8_markout(df):
    out = {}
    for fam, d in df.groupby('family'):
        out[fam] = {
            'maker_markout5_c': wratio(d, 'w_mkout_c', 'qty_mk'),
            'maker_settle_c': -wratio(d, 'w_total_c', 'qty'),
            'coverage': float(d['qty_mk'].sum() / d['qty'].sum()),
        }
    return out


def e9_series(df, top=18):
    """Per-series maker economics — which books are worth quoting, net of the
    series' own maker fee."""
    g = df.groupby(['family', 'series'], as_index=False).sum(numeric_only=True)
    g['maker_gross_c'] = -g['w_total_c'] / g['qty']
    g['maker_fee_c'] = g['w_fee_maker'] / g['qty']
    g['maker_net_c'] = g['maker_gross_c'] - g['maker_fee_c']
    g['maker_markout5_c'] = g['w_mkout_c'] / g['qty_mk'].replace(0, np.nan)
    g['taker_net_c'] = (g['w_total_c'] - g['w_fee_taker']) / g['qty']
    g['notional_usd'] = g['w_paid_c'] / 100.0
    g['maker_pnl_usd'] = (-g['w_total_c'] - g['w_fee_maker']) / 100.0
    g = g.sort_values('qty', ascending=False).head(top)
    mk = df.groupby(['series', 'market_ticker'], as_index=False).sum(numeric_only=True)
    out = []
    for _, r in g.iterrows():
        m = mk[mk['series'] == r['series']].assign(
            _mn=lambda x: -x['w_total_c'] - x['w_fee_maker'])
        lo, hi = boot_ratio(m, '_mn', 'qty')
        out.append({'family': r['family'], 'series': r['series'],
                    'contracts': float(r['qty']), 'markets': int(len(m)),
                    'notional_usd': float(r['notional_usd']),
                    'maker_gross_c': float(r['maker_gross_c']),
                    'maker_fee_c': float(r['maker_fee_c']),
                    'maker_net_c': float(r['maker_net_c']),
                    'maker_net_ci': (lo, hi),
                    'maker_markout5_c': float(r['maker_markout5_c']),
                    'taker_net_c': float(r['taker_net_c']),
                    'maker_pnl_usd': float(r['maker_pnl_usd'])})
    return out


def run(cells_path, outdir):
    os.makedirs(outdir, exist_ok=True)
    df = load(cells_path)
    coh = cohort_level(df)
    res = {
        'source': os.path.abspath(cells_path),
        'families_present': sorted(df['family'].unique().tolist()),
        'totals': {'cells': int(len(df)), 'trades': int(df['n'].sum()),
                   'contracts': float(df['qty'].sum()),
                   'markets': int(df['market_ticker'].nunique())},
        'e1_e2_e3': e1_e2_e3(df),
        'e4_benchmark_gradient': e4_benchmark_gradient(coh),
        'e5_lifecycle': e5_lifecycle(df),
        'e6_interaction': e6_interaction(coh),
        'e7_calibration': e7_calibration(df),
        'e8_markout': e8_markout(df),
        'e9_series': e9_series(df),
    }
    json.dump(res, open(os.path.join(outdir, 'results.json'), 'w'), indent=1,
              default=lambda o: None if isinstance(o, float) and np.isnan(o) else float(o))
    print(json.dumps(res['e1_e2_e3'], indent=1, default=str)[:4000])
    return res


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else '.')
