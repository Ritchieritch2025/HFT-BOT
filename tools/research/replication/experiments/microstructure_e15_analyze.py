"""E15 analysis — artifacts -> results.json. Runs anywhere (Mac), no heavy IO.

    python microstructure_e15_analyze.py <artifact_dir> results.json

Every definition here is the one locked in
agents/audit/PREREG_E15_Kalshi微结构解剖_2026-07-27.md.
Sample unit is the market; CIs are market-cluster bootstrap x1000, seed 20260727.
"""
import os, sys, json
import numpy as np
import pandas as pd
import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication import taxonomy, stats  # noqa: E402

SEED = 20260727
NBOOT = 1000
MIN_DWELL_2S = 30.0     # pre-registered: >=30 two-sided quote-seconds
MIN_TRADES = 10         # pre-registered: >=10 asof-matched prints
MIN_SAMPLES = 30        # pre-registered: >=30 book samples
GATE = 0.99             # pre-registered known-answer threshold

# ---- Dubach (2026) anchors, read off Tables 1-4 and section 5.2 -------------
PM = {
    'sf1_decile_median_bps': [1818, 1339, 754.99, 2581, 400.0, 400.0, 444.44, 425.09, 222.22, 53.48],
    'sf1_decile_markets': [33, 18, 18, 21, 184, 214, 27, 12, 15, 4],
    'sf1_central_bps': 400.0, 'sf1_longshot_bps': [1339, 1818],
    'sf2_share': [0.1364, 0.1034, 0.0945, 0.0806, 0.0824, 0.0789, 0.0818, 0.0821, 0.0767, 0.0830],
    'sf2_l1_p25': 0.0800, 'sf2_l1_p75': 0.2055, 'sf2_kl_median': 0.087,
    'sf2_topheavy_share': 0.09, 'sf2_within_factor2': 0.57, 'sf2_markets': 547,
    'sf5_median_prob_pp': {'Sports': 0.0075, 'Geopolitics': 0.0001, 'Other': -0.0004, 'Crypto': -0.0393},
    'sf8_slopes': {'bivariate': 0.8178, 'category_fe': 0.5500, 'category_fe_volume': 0.3048,
                   'full_v2': 0.0084, 'full_v2_mid_decile_2_7': -0.2702},
    'sf8_se': {'bivariate': 0.1133, 'category_fe': 0.1429, 'category_fe_volume': 0.1036,
               'full_v2': 0.1015, 'full_v2_mid_decile_2_7': 0.1687},
    'sf8_r2': {'bivariate': 0.1293, 'category_fe': 0.2165, 'category_fe_volume': 0.4857,
               'full_v2': 0.5702, 'full_v2_mid_decile_2_7': 0.2909},
    'sf8_full_v2_controls': {'log_duration': 0.222, 'log_p1p': -1.02, 'log_volume': 0.41},
    'sign_agreement': 0.592, 'eff_spread_sign_flip': 0.67, 'kyle_sign_flip': 0.60,
}
# WHO_PROFITS (Repo Research/WHO_PROFITS_20260727/results.json) maker gross edge, c/contract
WP_MAKER_GROSS_C = {'crypto_15m': 0.2730, 'crypto_hourly': 0.4734}

BANDS_ORDER = ['gt60m', '10-60m', '5-10m', '2-5m', 'lt2m']
MN_ORDER = ['atm_0-5', 'near_5-15', 'mid_15-30', 'far_30-45', 'edge_45-50']


def q(con, sql):
    return con.execute(sql).df()


def fam(df):
    df = df.copy()
    s = df.series_ticker.where(df.series_ticker.notna(), None)
    c = df.category.where(df.category.notna(), None)
    df['family'] = [taxonomy.family_of(a, b) if (a is not None and b is not None) else None
                    for a, b in zip(s, c)]
    return df


def med_iqr(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return dict(n=0)
    return dict(n=int(len(x)), median=float(np.median(x)),
                p25=float(np.percentile(x, 25)), p75=float(np.percentile(x, 75)),
                p10=float(np.percentile(x, 10)), p90=float(np.percentile(x, 90)),
                mean=float(np.mean(x)))


def boot_mean_ci(x):
    lo, hi = stats.cluster_bootstrap_ci(np.asarray(x, dtype=float), n_boot=NBOOT, seed=SEED)
    return [lo, hi]


def boot_median_ci(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return [None, None]
    r = np.random.default_rng(SEED)
    idx = r.integers(0, len(x), size=(NBOOT, len(x)))
    bs = np.median(x[idx], axis=1)
    return [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]


# ============================================================ SF1-K
def sf1(con, d, out):
    a = q(con, """SELECT * FROM read_parquet('%s/l1_market_band/*.parquet')""" % d)
    a = fam(a)
    out['coverage']['l1_rows_all'] = int(len(a))
    allb = a[(a.band == 'ALL') & (a.dwell_2s >= MIN_DWELL_2S)].copy()
    allb['mean_mid'] = allb.w_mid / allb.dwell_2s
    allb['tick_bind'] = allb.dwell_1tick / allb.dwell_2s
    allb['le2tick'] = allb.dwell_le2tick / allb.dwell_2s
    allb['empty_share'] = allb.dwell_empty / allb.dwell_s
    allb['decile'] = np.clip((allb.mean_mid // 10).astype(int), 0, 9)
    allb = allb[allb.family.notna()]
    out['coverage']['sf1_markets'] = int(len(allb))
    out['coverage']['sf1_markets_by_family'] = allb.family.value_counts().to_dict()
    out['coverage']['sf1_quote_seconds'] = float(allb.dwell_2s.sum())

    rows = []
    for dec, g in allb.groupby('decile'):
        rows.append(dict(decile=int(dec), lo=int(dec) * 10, hi=int(dec) * 10 + 10,
                         markets=int(len(g)),
                         median_bps=float(g.med_spread_bps.median()),
                         p25_bps=float(g.med_spread_bps.quantile(.25)),
                         p75_bps=float(g.med_spread_bps.quantile(.75)),
                         median_c=float(g.med_spread_c.median()),
                         tick_bind=float(g.tick_bind.median()),
                         le2tick=float(g.le2tick.median()),
                         empty_share=float(g.empty_share.median()),
                         ci_median_bps=boot_median_ci(g.med_spread_bps),
                         pm_median_bps=PM['sf1_decile_median_bps'][int(dec)],
                         pm_markets=PM['sf1_decile_markets'][int(dec)]))
    out['sf1'] = {'deciles': rows}

    byfam = {}
    for f, g in allb.groupby('family'):
        dd = []
        for dec, gg in g.groupby('decile'):
            if len(gg) < 5:
                continue
            dd.append(dict(decile=int(dec), markets=int(len(gg)),
                           median_bps=float(gg.med_spread_bps.median()),
                           median_c=float(gg.med_spread_c.median()),
                           tick_bind=float(gg.tick_bind.median()),
                           empty_share=float(gg.empty_share.median())))
        byfam[f] = dict(markets=int(len(g)), deciles=dd,
                        median_bps=float(g.med_spread_bps.median()),
                        median_c=float(g.med_spread_c.median()),
                        tick_bind=float(g.tick_bind.median()),
                        empty_share=float(g.empty_share.median()),
                        deci_tick_share=float((g.deci == 1).mean()))
    out['sf1']['by_family'] = byfam

    # ---- the Kalshi-only cut: the same table again, sliced by time to close --
    b = a[(a.band != 'ALL') & (a.dwell_2s >= 5)].copy()
    b['mean_mid'] = b.w_mid / b.dwell_2s
    b['tick_bind'] = b.dwell_1tick / b.dwell_2s
    b['empty_share'] = b.dwell_empty / b.dwell_s
    b = b[b.family.notna()]
    life = {}
    for f, g in b.groupby('family'):
        rr = []
        for band in BANDS_ORDER:
            gg = g[g.band == band]
            if len(gg) < 5:
                continue
            rr.append(dict(band=band, markets=int(len(gg)),
                           median_bps=float(gg.med_spread_bps.median()),
                           p25_bps=float(gg.med_spread_bps.quantile(.25)),
                           p75_bps=float(gg.med_spread_bps.quantile(.75)),
                           median_c=float(gg.med_spread_c.median()),
                           tick_bind=float(gg.tick_bind.median()),
                           empty_share=float(gg.empty_share.median()),
                           median_mid=float(gg.mean_mid.median()),
                           ci_median_bps=boot_median_ci(gg.med_spread_bps)))
        life[f] = rr
    out['sf1']['lifecycle'] = life

    # decile x band grid for crypto_15m (the 15-minute lifecycle, PM cannot do this)
    c = b[b.family == 'crypto_15m'].copy()
    c['decile'] = np.clip((c.mean_mid // 10).astype(int), 0, 9)
    grid = []
    for (dec, band), g in c.groupby(['decile', 'band']):
        if len(g) < 5:
            continue
        grid.append(dict(decile=int(dec), band=band, markets=int(len(g)),
                         median_bps=float(g.med_spread_bps.median()),
                         median_c=float(g.med_spread_c.median()),
                         tick_bind=float(g.tick_bind.median()),
                         empty_share=float(g.empty_share.median())))
    out['sf1']['crypto15m_grid'] = grid


# ============================================================ SF5-K
def sf5(con, d, out):
    e = q(con, """SELECT * FROM read_parquet('%s/eff_spread/*.parquet')""" % d)
    e = fam(e)
    e = e[e.family.notna()]
    m = e[(e.bucket == 'ALL') & (e.band == 'ALL') & (e.n_trades >= MIN_TRADES)].copy()
    m['eff_c'] = m.sum_eff_c / m.n_trades
    m['eff_c_w'] = m.sum_eff_c_w / m.contracts
    m['eff_bps'] = m.sum_eff_bps / m.n_trades
    m['mid'] = m.sum_mid / m.n_trades
    out['coverage']['sf5_markets'] = int(len(m))
    out['coverage']['sf5_trades'] = int(m.n_trades.sum())
    out['coverage']['sf5_contracts'] = float(m.contracts.sum())

    rows = []
    for f, g in m.groupby('family'):
        s = med_iqr(g.eff_c)
        wp = WP_MAKER_GROSS_C.get(f)
        rows.append(dict(family=f, markets=int(len(g)), trades=int(g.n_trades.sum()),
                         contracts=float(g.contracts.sum()),
                         median_eff_c=s.get('median'), p25=s.get('p25'), p75=s.get('p75'),
                         mean_eff_c=s.get('mean'),
                         ci_mean_eff_c=boot_mean_ci(g.eff_c),
                         ci_median_eff_c=boot_median_ci(g.eff_c),
                         median_eff_c_weighted=float(g.eff_c_w.median()),
                         median_eff_bps=float(g.eff_bps.median()),
                         median_mid_c=float(g.mid.median()),
                         print_weighted_eff_c=float(g.sum_eff_c.sum() / g.n_trades.sum()),
                         wp_maker_gross_c=wp,
                         adverse_selection_c=(None if wp is None else float(g.eff_c.median()) - wp)))
    out['sf5'] = {'by_family': sorted(rows, key=lambda r: -r['trades'])}

    # by time-to-close band, per family.  The pass emits (market, bucket, band) cells,
    # so the band totals are re-aggregated over price buckets here.
    mb = e[(e.bucket != 'ALL') & (e.band != 'ALL')].copy()
    mb = mb.groupby(['market_ticker', 'family', 'band'], as_index=False).agg(
        n_trades=('n_trades', 'sum'), sum_eff_c=('sum_eff_c', 'sum'),
        contracts=('contracts', 'sum'))
    mb = mb[mb.n_trades >= 5]
    mb['eff_c'] = mb.sum_eff_c / mb.n_trades
    life = {}
    for f, g in mb.groupby('family'):
        rr = []
        for band in BANDS_ORDER:
            gg = g[g.band == band]
            if len(gg) < 5:
                continue
            rr.append(dict(band=band, markets=int(len(gg)), trades=int(gg.n_trades.sum()),
                           median_eff_c=float(gg.eff_c.median()),
                           ci_mean_eff_c=boot_mean_ci(gg.eff_c)))
        life[f] = rr
    out['sf5']['lifecycle'] = life

    # by price bucket, per family (mirror of the E11/E13 toxicity axis)
    mk = e[(e.bucket != 'ALL') & (e.band == 'ALL') & (e.n_trades >= 5)].copy()
    mk['eff_c'] = mk.sum_eff_c / mk.n_trades
    buck = {}
    for f, g in mk.groupby('family'):
        rr = []
        for b in taxonomy.BUCKETS:
            gg = g[g.bucket == b]
            if len(gg) < 5:
                continue
            rr.append(dict(bucket=b, markets=int(len(gg)), trades=int(gg.n_trades.sum()),
                           median_eff_c=float(gg.eff_c.median())))
        buck[f] = rr
    out['sf5']['by_bucket'] = buck


def _shape_only(cells_all, keep):
    """Headline shape statistics for an arbitrary gate cut (sensitivity column)."""
    c = cells_all[[(t, v) in keep for t, v in zip(cells_all.market_ticker, cells_all.variant)]]
    lv = ['bid_%d' % k for k in range(1, 11)] + ['ask_%d' % k for k in range(1, 11)]
    mk = c.groupby(['market_ticker']).agg({**{k: 'sum' for k in lv}, 'n': 'sum'}).reset_index()
    mk = mk[mk.n >= MIN_SAMPLES]
    if not len(mk):
        return {'markets': 0}
    B = mk[['bid_%d' % k for k in range(1, 11)]].values
    A = mk[['ask_%d' % k for k in range(1, 11)]].values
    tot = (B + A).sum(axis=1, keepdims=True)
    share = (B + A) / np.where(tot > 0, tot, np.nan)
    share = share[np.isfinite(share).all(axis=1)]
    kl = np.nansum(np.where(share > 0, share * np.log(share / 0.1), 0.0), axis=1)
    return dict(markets=int(share.shape[0]),
                l1_share_median=float(np.median(share[:, 0])),
                kl_median=float(np.median(kl)),
                topheavy_share=float(np.mean(share[:, 0] > 0.5)),
                within_factor2=float(np.mean((share[:, 0] >= 0.05) & (share[:, 0] <= 0.20))),
                levels=[float(np.median(share[:, k])) for k in range(10)])


# ============================================================ SF2-K + gate
def sf2(con, d, out):
    g = q(con, "SELECT * FROM read_parquet('%s/l2_gate.parquet')" % d)
    g['agree'] = g.n_match / g.n_cmp
    best = g.sort_values(['agree', 'n_cmp']).groupby('market_ticker').tail(1)
    passed = best[(best.agree >= GATE) & (best.n_cmp >= 20)]
    out['sf2'] = {'gate': dict(
        markets_compared=int(best.market_ticker.nunique()),
        markets_passed=int(len(passed)), threshold=GATE,
        median_agreement=float(best.agree.median()),
        p25_agreement=float(best.agree.quantile(.25)),
        variant_counts=passed.variant.value_counts().to_dict(),
        comparisons=int(best.n_cmp.sum()))}
    keep = set(zip(passed.market_ticker, passed.variant))
    if not keep:
        out['sf2']['status'] = 'GATE FAILED — SF2-K/SF8-K not reported'
        return None
    # sensitivity: the gate reference (L1) is itself a separate capture stream, so a
    # market can miss 99% because L1 saw an update L2 did not.  Re-run the shape at a
    # looser cut and check the conclusion does not move.  Labelled, never substituted.
    loose = best[(best.agree >= 0.95) & (best.n_cmp >= 20)]
    out['sf2']['gate']['markets_passed_95'] = int(len(loose))
    out['_keep95'] = set(zip(loose.market_ticker, loose.variant))

    cells_all = fam(q(con, "SELECT * FROM read_parquet('%s/depth_cells.parquet')" % d))
    cells_all = cells_all[cells_all.family.notna()]
    out['sf2']['sensitivity_95'] = _shape_only(cells_all, out['_keep95'])
    del out['_keep95']

    c = cells_all[[(t, v) in keep for t, v in zip(cells_all.market_ticker, cells_all.variant)]]
    lv = ['bid_%d' % k for k in range(1, 11)] + ['ask_%d' % k for k in range(1, 11)]
    mk = c.groupby(['market_ticker', 'family']).agg(
        {**{k: 'sum' for k in lv}, 'n': 'sum', 'sum_top10': 'sum', 'sum_mid': 'sum',
         'sum_spread_c': 'sum'}).reset_index()
    mk = mk[mk.n >= MIN_SAMPLES]
    out['coverage']['sf2_markets'] = int(len(mk))
    out['coverage']['sf2_samples'] = int(mk.n.sum())

    B = mk[['bid_%d' % k for k in range(1, 11)]].values
    A = mk[['ask_%d' % k for k in range(1, 11)]].values
    tot = (B + A).sum(axis=1, keepdims=True)
    share = (B + A) / np.where(tot > 0, tot, np.nan)
    share_b = B / np.where(B.sum(1, keepdims=True) > 0, B.sum(1, keepdims=True), np.nan)
    share_a = A / np.where(A.sum(1, keepdims=True) > 0, A.sum(1, keepdims=True), np.nan)
    ok = np.isfinite(share).all(axis=1)
    mk = mk[ok].reset_index(drop=True)
    share, share_b, share_a = share[ok], share_b[ok], share_a[ok]
    kl = np.nansum(np.where(share > 0, share * np.log(share / 0.1), 0.0), axis=1)
    mk['kl'] = kl
    mk['l1_share'] = share[:, 0]
    mk['bid_frac'] = B[ok].sum(1) / (B[ok].sum(1) + A[ok].sum(1))

    def lvl_table(S, label):
        return dict(label=label, markets=int(S.shape[0]), levels=[
            dict(k=k + 1, median=float(np.nanmedian(S[:, k])),
                 p25=float(np.nanpercentile(S[:, k], 25)), p75=float(np.nanpercentile(S[:, k], 75)),
                 p10=float(np.nanpercentile(S[:, k], 10)), p90=float(np.nanpercentile(S[:, k], 90)),
                 pm_median=PM['sf2_share'][k]) for k in range(10)])

    out['sf2']['overall'] = lvl_table(share, 'both sides')
    out['sf2']['bid_side'] = lvl_table(share_b, 'bid side (YES bids)')
    out['sf2']['ask_side'] = lvl_table(share_a, 'ask side (NO bids, mirrored)')
    out['sf2']['shape'] = dict(
        kl_median=float(np.nanmedian(kl)), kl_p25=float(np.nanpercentile(kl, 25)),
        kl_p75=float(np.nanpercentile(kl, 75)),
        l1_share_median=float(np.nanmedian(share[:, 0])),
        topheavy_share=float(np.nanmean(share[:, 0] > 0.5)),
        within_factor2=float(np.nanmean((share[:, 0] >= 0.05) & (share[:, 0] <= 0.20))),
        bid_frac_median=float(np.nanmedian(mk.bid_frac)),
        pm=dict(kl_median=PM['sf2_kl_median'], l1_share_median=PM['sf2_share'][0],
                topheavy_share=PM['sf2_topheavy_share'], within_factor2=PM['sf2_within_factor2']))

    byfam = {}
    for f, idx in mk.groupby('family').groups.items():
        i = mk.index.get_indexer(idx)
        if len(i) < 5:
            continue
        byfam[f] = dict(markets=int(len(i)),
                        l1_share_median=float(np.nanmedian(share[i, 0])),
                        kl_median=float(np.nanmedian(kl[i])),
                        topheavy_share=float(np.nanmean(share[i, 0] > 0.5)),
                        bid_frac_median=float(np.nanmedian(mk.bid_frac.values[i])),
                        levels=[float(np.nanmedian(share[i, k])) for k in range(10)],
                        levels_bid=[float(np.nanmedian(share_b[i, k])) for k in range(10)],
                        levels_ask=[float(np.nanmedian(share_a[i, k])) for k in range(10)])
    out['sf2']['by_family'] = byfam

    # moneyness x tte cells, per family (the "gamma zone" cut)
    cells = []
    c2 = c[c.n > 0].copy()
    for (f, mb, tb), g2 in c2.groupby(['family', 'mn_band', 'tte_band']):
        n = g2.n.sum()
        if n < 60:
            continue
        b = np.array([g2['bid_%d' % k].sum() for k in range(1, 11)])
        a = np.array([g2['ask_%d' % k].sum() for k in range(1, 11)])
        t = (b + a).sum()
        if t <= 0:
            continue
        cells.append(dict(family=f, mn_band=mb, tte_band=tb, samples=int(n),
                          markets=int(g2.market_ticker.nunique()),
                          l1_share=float((b[0] + a[0]) / t),
                          mean_top10=float(t / n),
                          mean_spread_c=float(g2.sum_spread_c.sum() / n),
                          bid_frac=float(b.sum() / t),
                          shares=[float((b[k] + a[k]) / t) for k in range(10)]))
    out['sf2']['cells'] = cells
    return keep


# ============================================================ SF8-K
def ols_cluster(y, X, groups, names):
    np.seterr(all='ignore')
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, k = X.shape
    XtX = X.T @ X
    XtXi = np.linalg.pinv(XtX, rcond=1e-12)
    beta = XtXi @ (X.T @ y)
    u = y - X @ beta
    gs = pd.factorize(groups)[0]
    G = gs.max() + 1
    meat = np.zeros((k, k))
    order = np.argsort(gs, kind='stable')
    gs_s, X_s, u_s = gs[order], X[order], u[order]
    bounds = np.searchsorted(gs_s, np.arange(G + 1))
    for g in range(G):
        lo, hi = bounds[g], bounds[g + 1]
        if hi <= lo:
            continue
        xu = X_s[lo:hi].T @ u_s[lo:hi]
        meat += np.outer(xu, xu)
    dfc = (G / max(G - 1, 1)) * ((n - 1) / max(n - k, 1))
    V = XtXi @ meat @ XtXi * dfc
    se = np.sqrt(np.clip(np.diag(V), 0, None))
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1 - (u ** 2).sum() / ss_tot if ss_tot > 0 else np.nan
    return dict(n=int(n), groups=int(G), r2=float(r2),
                coef={nm: float(b) for nm, b in zip(names, beta)},
                se={nm: float(s) for nm, s in zip(names, se)},
                ci={nm: [float(b - 1.96 * s), float(b + 1.96 * s)] for nm, b, s in zip(names, beta, se)})


def demean(df, cols, by):
    out = df[cols].values.astype(float).copy()
    g = pd.factorize(df[by])[0]
    for j in range(out.shape[1]):
        s = np.bincount(g, weights=out[:, j])
        c = np.bincount(g)
        out[:, j] -= (s / c)[g]
    return out


def sf8(con, d, out, keep):
    p = q(con, "SELECT * FROM read_parquet('%s/depth_panel.parquet')" % d)
    if keep:
        p = p[[(t, v) in keep for t, v in zip(p.market_ticker, p.variant)]]
    v = q(con, "SELECT * FROM read_parquet('%s/panel_volume.parquet')" % d)
    meta = q(con, "SELECT DISTINCT market_ticker, series_ticker, category FROM read_parquet('%s/depth_cells.parquet')" % d)
    p = p.merge(meta, on='market_ticker', how='left')
    p = fam(p)
    p = p[p.family.notna()]
    v = v.sort_values(['market_ticker', 'ts_bucket'])
    v['cum'] = v.groupby('market_ticker').contracts.cumsum()
    p['ts_bucket'] = (p.ts_utc // 60_000_000) * 60_000_000
    p = p.merge(v[['market_ticker', 'ts_bucket', 'cum']], on=['market_ticker', 'ts_bucket'], how='left')
    p['cum'] = p.groupby('market_ticker')['cum'].ffill().fillna(0.0)
    p = p[(p.tte_min > 0) & (p.top10 > 0) & (p.mid_c > 0) & (p.mid_c < 100)]
    p['y'] = np.log(p.top10)
    p['x_tte'] = np.log(p.tte_min * 60.0)
    pr = p.mid_c / 100.0
    p['x_p1p'] = np.log(pr * (1 - pr))
    p['x_vol'] = np.log1p(p.cum)
    p = p.replace([np.inf, -np.inf], np.nan).dropna(subset=['y', 'x_tte', 'x_p1p', 'x_vol'])
    out['coverage']['sf8_rows'] = int(len(p))
    out['coverage']['sf8_markets'] = int(p.market_ticker.nunique())
    if len(p) < 100:
        out['sf8'] = {'status': 'insufficient panel'}
        return

    specs = []
    one = np.ones((len(p), 1))
    X1 = np.hstack([one, p[['x_tte']].values])
    specs.append(('bivariate', ols_cluster(p.y, X1, p.market_ticker, ['const', 'log_tte'])))

    fams = pd.get_dummies(p.family, drop_first=True).values.astype(float)
    X2 = np.hstack([one, p[['x_tte']].values, fams])
    n2 = ['const', 'log_tte'] + ['fam_%d' % i for i in range(fams.shape[1])]
    specs.append(('family_fe', ols_cluster(p.y, X2, p.market_ticker, n2)))

    X3 = np.hstack([one, p[['x_tte', 'x_vol']].values, fams])
    n3 = ['const', 'log_tte', 'log_volume'] + ['fam_%d' % i for i in range(fams.shape[1])]
    specs.append(('family_fe_volume', ols_cluster(p.y, X3, p.market_ticker, n3)))

    cols = ['y', 'x_tte', 'x_p1p', 'x_vol']
    D = demean(p, cols, 'market_ticker')
    specs.append(('market_fe_full', ols_cluster(D[:, 0], D[:, 1:], p.market_ticker,
                                                ['log_tte', 'log_p1p', 'log_volume'])))
    drop = ols_cluster(D[:, 0], D[:, 2:], p.market_ticker, ['log_p1p', 'log_volume'])
    full = specs[-1][1]
    full['delta_r2_tte'] = float(full['r2'] - drop['r2'])
    full['r2_without_tte'] = float(drop['r2'])

    # crypto-only replication of the full spec (the family that matters to us)
    for f in ('crypto_15m', 'sports_game', 'sports_prop'):
        pf = p[p.family == f]
        if pf.market_ticker.nunique() < 5 or len(pf) < 200:
            continue
        Df = demean(pf, cols, 'market_ticker')
        r = ols_cluster(Df[:, 0], Df[:, 1:], pf.market_ticker, ['log_tte', 'log_p1p', 'log_volume'])
        rd = ols_cluster(Df[:, 0], Df[:, 2:], pf.market_ticker, ['log_p1p', 'log_volume'])
        r['delta_r2_tte'] = float(r['r2'] - rd['r2'])
        specs.append(('market_fe_full__' + f, r))

    out['sf8'] = {'specs': [dict(spec=k, **v) for k, v in specs],
                  'pm': {'slopes': PM['sf8_slopes'], 'se': PM['sf8_se'], 'r2': PM['sf8_r2'],
                         'controls': PM['sf8_full_v2_controls']}}


# ============================================================ strike ladder
def ladder(con, d, out):
    L = q(con, "SELECT * FROM read_parquet('%s/ladder.parquet')" % d)
    if not len(L):
        out['ladder'] = {'status': 'no rows'}
        return

    def strike(t):
        s = t.rsplit('-', 1)[-1]
        try:
            return float(s.lstrip('TB'))
        except ValueError:
            return np.nan
    L['strike'] = [strike(t) for t in L.market_ticker]
    L = L.dropna(subset=['strike'])
    rows = []
    for (ev, ts), g in L.groupby(['event_ticker', 'ts_bucket']):
        g = g.sort_values('strike')
        if len(g) < 8:
            continue
        # implied spot = strike where the ladder's mid crosses 50c (monotone decreasing)
        s, m = g.strike.values, g.mid_c.values
        above = np.where(m >= 50)[0]
        below = np.where(m < 50)[0]
        if not len(above) or not len(below):
            continue
        i = above[-1]
        j = below[0]
        if j <= i or m[i] == m[j]:
            spot = s[i]
        else:
            spot = s[i] + (s[j] - s[i]) * (m[i] - 50.0) / (m[i] - m[j])
        gg = g.copy()
        gg['dist'] = gg.strike - spot
        rows.append(gg)
    if not rows:
        out['ladder'] = {'status': 'no ladder crossings'}
        return
    D = pd.concat(rows, ignore_index=True)
    D['adist'] = D.dist.abs()
    edges = [0, 100, 300, 600, 1200, 1e9]
    labels = ['0-100', '100-300', '300-600', '600-1200', '1200+']
    D['dband'] = pd.cut(D.adist, edges, labels=labels, right=False)
    tb = []
    for lab in labels:
        g = D[D.dband == lab]
        if len(g) < 20:
            continue
        tb.append(dict(band=lab, n=int(len(g)), markets=int(g.market_ticker.nunique()),
                       median_depth=float(g.l1_depth.median()),
                       median_bid_depth=float(g.bid_depth.median()),
                       median_ask_depth=float(g.ask_depth.median()),
                       median_spread_c=float(g.spread_c.median()),
                       median_mid_c=float(g.mid_c.median()),
                       bid_frac=float(g.bid_depth.sum() / (g.bid_depth.sum() + g.ask_depth.sum()))))
    sgn = []
    for lab, lo, hi in [('deep_itm', -1e9, -600), ('itm', -600, -100), ('atm', -100, 100),
                        ('otm', 100, 600), ('deep_otm', 600, 1e9)]:
        g = D[(D.dist >= lo) & (D.dist < hi)]
        if len(g) < 20:
            continue
        sgn.append(dict(band=lab, n=int(len(g)), median_depth=float(g.l1_depth.median()),
                        median_spread_c=float(g.spread_c.median()),
                        median_mid_c=float(g.mid_c.median()),
                        bid_frac=float(g.bid_depth.sum() / (g.bid_depth.sum() + g.ask_depth.sum()))))
    out['ladder'] = {'abs_distance': tb, 'signed_distance': sgn,
                     'events': int(D.event_ticker.nunique()), 'rows': int(len(D)),
                     'series': sorted(D.series_ticker.unique().tolist())}


# ============================================================ taxonomy kit
def kit(con, d, out):
    c = q(con, "SELECT * FROM read_parquet('%s/census.parquet')" % d)
    ins = c[c.in_scope == True]                                          # noqa: E712
    uni = c[(c.in_scope == False) & c.series.notna()]                    # noqa: E712
    exc = c[c.series.isna()]
    out['taxonomy_kit'] = {
        'rule_version_sha256_16': 'from meta.json',
        'in_scope_series': int(len(ins)), 'in_scope_l1_rows': int(ins.l1_rows.sum()),
        'in_scope_markets': int(ins.markets.sum()),
        'unidentified_series': int(len(uni)), 'unidentified_l1_rows': int(uni.l1_rows.sum()),
        'unidentified_top': uni.sort_values('l1_rows', ascending=False)
                              .head(20)[['category', 'series', 'l1_rows', 'markets']]
                              .to_dict('records'),
        'excluded_categories': exc.sort_values('l1_rows', ascending=False)
                                 [['category', 'l1_rows', 'markets']].to_dict('records'),
        'family_l1_rows': ins.groupby('family').l1_rows.sum().to_dict(),
        'family_markets': ins.groupby('family').markets.sum().to_dict()}


def main():
    d, dst = sys.argv[1], sys.argv[2]
    con = duckdb.connect()
    out = {'coverage': {}, 'source_dir': os.path.abspath(d)}
    out['settlement'] = json.load(open(os.path.join(d, 'settlement_meta.json')))
    kit(con, d, out)
    sf1(con, d, out)
    sf5(con, d, out)
    if os.path.exists(os.path.join(d, 'l2_gate.parquet')):
        keep = sf2(con, d, out)
        sf8(con, d, out, keep)
    else:
        out['sf2'] = out['sf8'] = {'status': 'L2 artifacts absent'}
    ladder(con, d, out)
    out['pm_anchors'] = PM
    json.dump(out, open(dst, 'w'), indent=1, default=float)
    print('wrote', dst)
    print('markets: SF1 %s, SF5 %s, SF2 %s, SF8 %s'
          % (out['coverage'].get('sf1_markets'), out['coverage'].get('sf5_markets'),
             out['coverage'].get('sf2_markets'), out['coverage'].get('sf8_markets')))


if __name__ == '__main__':
    main()
