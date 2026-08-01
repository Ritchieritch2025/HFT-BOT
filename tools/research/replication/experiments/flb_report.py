#!/usr/bin/env python
"""FLB stratified measurement — figure pack.

The prose report (agents/audit/REPORT_FLB_分层测量结果_2026-07-27.md) opens with a 图版
section promising "three charts to read the whole table"; its `@@FIGURES@@` marker was
filled with numeric tiles, so no chart ever shipped. This renders the figures it
promised, from the same artifacts, in the house style.

Usage: python flb_report.py <artifact_dir> <out.pdf>
"""
import sys, os, json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication.report import (Report, header, footnote, table, cover, prose_page,
                               bar_labels, zero_line, small_multiples,
                               S1, S2, S3, POS, NEG, SEQ, INK, INK2, MUTED, AXIS,
                               GRID, SURFACE, PLANE, GOOD, CRIT, WARN)

FAM_ORDER = ['crypto_15m', 'crypto_hourly', 'sports_game', 'sports_prop',
             'politics_events', 'econ']
FAM_LABEL = {'crypto_15m': 'Crypto 15-min', 'crypto_hourly': 'Crypto hourly/daily',
             'sports_game': 'Sports game', 'sports_prop': 'Sports props',
             'politics_events': 'Politics/elections', 'econ': 'Economics'}
BUCKETS = ['01-05', '05-10', '10-20', '20-35', '35-50', '50-65', '65-80', '80-90',
           '90-95', '95-99']
BUCKET_MID = {'01-05': 3, '05-10': 7.5, '10-20': 15, '20-35': 27.5, '35-50': 42.5,
              '50-65': 57.5, '65-80': 72.5, '80-90': 85, '90-95': 92.5, '95-99': 97}
BANDS = ['gt60m', '10-60m', '5-10m', '2-5m', 'lt2m']
BAND_LABEL = ['>60m', '10-60m', '5-10m', '2-5m', '<2m']


def load(d):
    c = pd.read_csv(os.path.join(d, 'cells_full.csv'))
    c = c[c['bucket'] != '45-55(null)'].copy()
    g = pd.read_csv(os.path.join(d, 'gaps_summary.csv'))
    s = json.load(open(os.path.join(d, 'sanity.json')))
    st = json.load(open(os.path.join(d, 'stats_cells.json')))
    return c, g, s, st


# ------------------------------------------------------------------ page 1
def p_cover(rep, c, s, st):
    fig = rep.page()
    n_cells = len(c)
    n_exc = int(c['exceeds_cost'].sum())
    cover(
        fig,
        'Favourite–longshot bias, stratified',
        'Is the textbook regularity real on our tape — where, how big, and bigger than what it costs?',
        'The literature says recreational flow overpays for longshots and underpays favourites. '
        'We measured it on 16 sealed days of the whole exchange, one cell per family × side × '
        'price bucket × time-to-close band, with the market as the sample unit.',
        tiles=[
            ('%d / %d' % (n_exc, n_cells), 'cells where the deviation beats the round-trip cost, '
             'with n ≥ 30 and a CI clear of zero', GOOD),
            ('6 shapes', 'market families measured — and six different pictures. There is no single '
             '"longshot bias"', INK),
            ('2 FAIL', 'families whose 45–55¢ null cell fails: econ and politics. That is not FLB, '
             'it is directional YES optimism', CRIT),
            ('111.7M', 'trade prints joined to settlement truth (82.3% of the tape)', INK),
        ],
        body=(
            'What the pictures say.\n\n'
            '  ①  **Crypto hourly is the textbook case** — longshots over-priced, favourites '
            'under-priced, and the deviation shrinks as the market approaches close (3.35 → 1.23pp). '
            'That is FLB behaving exactly as written.\n\n'
            '  ②  **Sports near the close is a different animal.** The deviation grows into the '
            'final minutes instead of shrinking (4.91 → 6.84pp on single games, 3.38 → 9.20 on '
            'props). Diagnosed as end-of-game lottery behaviour, not a broken anchor — settlement '
            'is never stamped before close anywhere in the warehouse.\n\n'
            '  ③  **Economics and politics fail the null-hypothesis cell.** At 45–55¢, where a '
            'price-bucket story predicts no deviation at all, econ shows YES +18.8pp / NO −14.3pp. '
            'A price-symmetric bias cannot do that; a directional over-pricing of YES can. Those '
            'two families must be read after netting the directional component.\n\n'
            '  ④  **Complementarity holds**, which is the check that the measurement is not an '
            'artifact: of the 90 pairs where both sides deviate by ≥2pp, 84 (93.3%) point in '
            'opposite directions, as two sides of the same contract must.'
        ),
        footer='16 sealed days (2026-07-10..25) · %s prints in scope · sample unit = market · '
               'cells with n < 30 are greyed and carry no conclusion · cluster bootstrap ×1000, '
               'seed 20260727 · cost = median in-cell spread + taker fee 7·p·(1−p)'
               % f"{st.get('trades_total', 0):,}",
    )
    rep.save(fig)


# ------------------------------------------------------------------ page 2
def p_bucket_curves(rep, c):
    fig = rep.page()
    header(fig, 'F1 · The shape of the bias, family by family',
           'Deviation = price paid − realized win rate, per price bucket, pooled over time bands '
           'and weighted by markets. Above zero the taker overpaid. Filled markers cleared the '
           'round-trip cost (dashed); hollow ones did not.',
           'bias by price bucket')
    for i, fam in enumerate(FAM_ORDER):
        ax = small_multiples(fig, i)
        d = c[(c['family'] == fam) & (~c['grey'])]
        allv = []
        for side, col, mk in (('yes', S1, 'o'), ('no', S2, 's')):
            dd = d[d['side'] == side]
            xs, ys, lo, hi, exc = [], [], [], [], []
            for b in BUCKETS:
                r = dd[dd['bucket'] == b]
                if not len(r):
                    continue
                w = r['n_markets'].to_numpy(float)
                xs.append(BUCKET_MID[b])
                ys.append(float(np.average(r['bias_pp'], weights=w)))
                lo.append(float(np.average(r['ci_lo'], weights=w)))
                hi.append(float(np.average(r['ci_hi'], weights=w)))
                exc.append(bool(r['exceeds_cost'].any()))
            if not xs:
                continue
            ys, lo, hi = np.array(ys), np.array(lo), np.array(hi)
            allv += list(lo) + list(hi)
            ax.errorbar(xs, ys, yerr=[ys - lo, hi - ys], fmt='none', elinewidth=1.1,
                        ecolor=GRID, zorder=2)
            ax.plot(xs, ys, color=col, lw=1.4, alpha=0.8, zorder=3,
                    label='taker buys %s' % side.upper())
            # filled = at least one cell in this bucket cleared its own cost
            for x, y, e in zip(xs, ys, exc):
                ax.plot([x], [y], marker=mk, ms=5, color=col, zorder=4,
                        mfc=col if e else SURFACE, mew=1.3)
        # cost drawn as a pair of lines, not a filled band: in thin families it is
        # far larger than the signal and a band would swallow the whole panel
        cx, cy = [], []
        for b in BUCKETS:
            r = d[d['bucket'] == b]
            if not len(r):
                continue
            cx.append(BUCKET_MID[b])
            cy.append(float(np.average(r['cost_c'], weights=r['n_markets'])))
        if cx:
            ax.plot(cx, cy, color=MUTED, lw=1.0, ls='--', zorder=1, label='round-trip cost')
            ax.plot(cx, [-v for v in cy], color=MUTED, lw=1.0, ls='--', zorder=1)
        zero_line(ax)
        ax.set_title(FAM_LABEL[fam], fontsize=9.5)
        ax.set_xlim(0, 100)
        span = max(np.nanmax(np.abs(allv)) if allv else 1, 1.0)
        ax.set_ylim(-span * 1.15, span * 1.15)
        if i % 3 == 0:
            ax.set_ylabel('deviation (pp)', fontsize=8)
        if i // 3 == 1:
            ax.set_xlabel('price paid (¢)', fontsize=8)
        if i == 0:
            ax.legend(fontsize=6.4, loc='lower right')
    footnote(fig, 'Error bars are 95% cluster bootstrap over markets; the dashed cost lines run '
             'off the panel in the thin families, which is the finding, not a plotting accident. '
             'Classic FLB reads as points above zero on the left and below zero on the right — '
             'crypto hourly and, more weakly, crypto 15-min show it. Sports props sit above zero at '
             'BOTH ends, and carry the largest statistical mass in the study (95-99¢ YES +5.86pp on '
             'n=15,663 markets, 3.6× cost). Politics and economics are a level shift, not a slope — '
             'see F4.', width=176)
    rep.save(fig)


# ------------------------------------------------------------------ page 3
def p_time(rep, c, s):
    fig = rep.page()
    header(fig, 'F2 · Does the bias converge as the market runs out of time?',
           'Mean absolute deviation per time-to-close band. A price-discovery story says this must '
           'fall toward the close. It does for crypto and politics; it does the opposite for sports.',
           'convergence check')
    ax = fig.add_axes([0.075, 0.30, 0.55, 0.48])
    conv = s['convergence']
    for i, fam in enumerate(FAM_ORDER):
        d = c[(c['family'] == fam) & (~c['grey'])]
        xs, ys = [], []
        for j, b in enumerate(BANDS):
            dd = d[d['band'] == b]
            if len(dd) < 2:
                continue
            xs.append(j)
            ys.append(float(np.average(dd['bias_pp'].abs(), weights=dd['n_markets'])))
        if not xs:
            continue
        verdict = conv.get(fam, {}).get('pass')
        col = {True: S1, False: NEG, None: MUTED}[verdict]
        ax.plot(xs, ys, marker='o', ms=5, lw=2.0 if verdict is not None else 1.2,
                ls='-' if verdict is not None else ':', color=col,
                label='%s  %s' % (FAM_LABEL[fam],
                                  {True: 'PASS', False: 'FAIL', None: 'n/a'}[verdict]))
    ax.set_xticks(range(len(BANDS)))
    ax.set_xticklabels(BAND_LABEL)
    ax.set_xlabel('time to close')
    ax.set_ylabel('mean |deviation| (pp)')
    ax.legend(fontsize=7.6, loc='upper left')
    ax.set_title('Blue converges, red diverges, dotted has no comparison band', color=INK)

    rows = []
    for fam in FAM_ORDER:
        v = conv.get(fam, {})
        rows.append([FAM_LABEL[fam],
                     '%.2f' % v['gt60m_mean_abs_bias'] if 'gt60m_mean_abs_bias' in v else '—',
                     '%.2f' % v['lt2m_mean_abs_bias'] if 'lt2m_mean_abs_bias' in v else '—',
                     {True: 'PASS', False: 'FAIL', None: 'n/a'}[v.get('pass')]])
    table(fig, [0.665, 0.34, 0.29, 0.40], rows,
          ['family', '>60m', '<2m', 'verdict'], widths=[0.40, 0.19, 0.19, 0.22],
          fontsize=7.4, scale=1.5)
    footnote(fig, 'Two families have no comparison to make: crypto 15-min markets never live long '
             'enough to have a >60m band, and economics has almost no volume in the last minutes. '
             'The sports failures were chased down rather than waved away — settlement_ts is never '
             'earlier than close_time anywhere in the warehouse (median lag 3 min on sports, 6 s on '
             'crypto 15-min), so the anchor is clean. The <2m sports band is in-game trading, where '
             'a losing side\'s comeback ticket gets more expensive as the clock runs out. The '
             'spec\'s "must converge" premise simply does not hold for in-game markets; the sports '
             'time dimension stays suspended until the pre-game/in-game split (done in the maker '
             'replay follow-on).')
    rep.save(fig)


# ------------------------------------------------------------------ page 4
def p_heatmap(rep, c):
    fig = rep.page()
    header(fig, 'F3 · Where the bias beats its own cost',
           'Each square is one family × price bucket. The number is how many of its cells (side × '
           'time band) showed a deviation larger than the round-trip cost with n ≥ 30 and a '
           'confidence interval clear of zero, out of how many were measured.',
           'net of cost')
    ax = fig.add_axes([0.16, 0.26, 0.70, 0.50])
    M = np.zeros((len(FAM_ORDER), len(BUCKETS)))
    T = np.zeros_like(M)
    for i, fam in enumerate(FAM_ORDER):
        for j, b in enumerate(BUCKETS):
            d = c[(c['family'] == fam) & (c['bucket'] == b)]
            T[i, j] = len(d)
            M[i, j] = int(d['exceeds_cost'].sum())
    im = ax.imshow(M, cmap='Blues', vmin=0, vmax=max(M.max(), 1), aspect='auto')
    ax.set_xticks(range(len(BUCKETS)))
    ax.set_xticklabels(BUCKETS, fontsize=7.6, rotation=35, ha='right')
    ax.set_yticks(range(len(FAM_ORDER)))
    ax.set_yticklabels(['%s  (%d)' % (FAM_LABEL[f], int(M[i].sum()))
                        for i, f in enumerate(FAM_ORDER)], fontsize=8.2)
    ax.grid(False)
    hot = M.max()
    for i in range(len(FAM_ORDER)):
        for j in range(len(BUCKETS)):
            if T[i, j] == 0:
                continue
            ax.text(j, i, '%d/%d' % (M[i, j], T[i, j]), ha='center', va='center',
                    fontsize=6.6, color=SURFACE if M[i, j] > hot * 0.55 else INK2)
    cb = fig.colorbar(im, ax=ax, fraction=0.022, pad=0.015)
    cb.set_label('cells clearing cost', fontsize=8)
    cb.ax.tick_params(labelsize=7)
    ax.set_xlabel('price paid (¢)', fontsize=8.5)
    ax.set_title('Cells that clear their own round-trip cost, out of the cells measured',
                 color=INK)
    footnote(fig, 'The number beside each family is its row total; 133 of 613 cells clear cost '
             'across the study. Sports game is the densest (51 of 110), sports props carries the '
             'largest volume. The 35–65¢ columns are almost empty, which is what a price-driven '
             'bias should look like — the deviation lives at the ends. Cost is the median in-cell '
             'spread plus the taker fee, so a cell that merely covers its own spread does not '
             'appear. This is a measurement, not a trade list: every position is held to '
             'settlement, no maker fee is charged, the fill is assumed at the volume-weighted taker '
             'price, and nothing here says the queue would ever have given us one. The maker replay '
             'follow-on answers those questions, and it kills half of this map.', width=176)
    rep.save(fig)


# ------------------------------------------------------------------ page 5
def p_null(rep, s):
    fig = rep.page()
    header(fig, 'F4 · The null-hypothesis cell',
           'The test that separates FLB from something else. At 45–55¢ a price-symmetric bias has '
           'nothing to say — both sides are near even money — so any deviation here is a different '
           'animal. Each bar is one family × side × time band.',
           '45–55¢ cell')
    d = pd.DataFrame(s['null_cell_45_55'])
    d = d[~d['grey']].copy()
    d['fam_i'] = d['family'].map({f: i for i, f in enumerate(FAM_ORDER)})
    d = d.sort_values(['fam_i', 'side', 'band'])
    ax = fig.add_axes([0.075, 0.28, 0.87, 0.50])
    x = np.arange(len(d))
    cols = [S1 if r.side == 'yes' else S2 for r in d.itertuples()]
    ax.bar(x, d['bias_pp'], 0.72, color=cols)
    lo = np.array([ci[0] for ci in d['ci']])
    hi = np.array([ci[1] for ci in d['ci']])
    ax.errorbar(x, d['bias_pp'], yerr=[d['bias_pp'] - lo, hi - d['bias_pp']], fmt='none',
                ecolor=INK2, elinewidth=0.9, capsize=2, zorder=3)
    fails = ~d['pass'].to_numpy()
    for xi, yi in zip(x[fails], d['bias_pp'].to_numpy()[fails]):
        ax.text(xi, yi + (1.4 if yi >= 0 else -1.4), 'FAIL', ha='center',
                va='bottom' if yi >= 0 else 'top', fontsize=6.4, color=CRIT,
                fontweight='bold')
    zero_line(ax)
    ax.set_xticks(x)
    ax.set_xticklabels(['%s\n%s · %s' % (FAM_LABEL[r.family].split('/')[0], r.side.upper(), r.band)
                        for r in d.itertuples()], fontsize=5.6, rotation=90)
    ax.set_ylabel('deviation at 45–55¢ (pp)')
    ax.set_title('Everything here should be zero. In econ and politics it is not.', color=INK)
    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([], [], marker='s', ls='', color=S1, ms=8, label='taker buys YES'),
                       Line2D([], [], marker='s', ls='', color=S2, ms=8, label='taker buys NO')],
              fontsize=8, loc='upper left')
    footnote(fig, 'Bars are 95% cluster-bootstrap intervals over markets; a cell FAILS when the '
             'interval excludes zero. Crypto passes across the board (1 of 19 cells grazes). '
             'Economics fails hardest — YES +18.8pp against NO −14.3pp, mirror-imaged, which is the '
             'signature of the market systematically over-pricing YES rather than over-pricing the '
             'longshot. Politics fails in the >60m band the same way. Same direction as the 07-18 '
             'audit\'s "optimism tax". Practical consequence: for these two families the price-bucket '
             'gradient on page 2 cannot be read as FLB until the directional component is netted out, '
             'and both families are in any case a fast-settling subset (68% of politics and 91% of '
             'elections volume never joined settlement inside the window).')
    rep.save(fig)


# ------------------------------------------------------------------ page 6
def p_method(rep, c, g, s, st):
    fig = rep.page()
    comp = s['complementarity']
    fig_rows = [[r['family'], str(int(r['events'])), str(int(r['markets'])),
                 '%.0f¢' % r['med_abs_jump'], 'yes' if r['criterion_valid'] else 'no']
                for _, r in g.iterrows()]
    prose_page(fig, 'Method, checks, and the three confounds that travel with these numbers', [
        ('Definitions, fixed in code',
         'Price is what the TAKER actually paid, and cells are built per taker side, so the YES '
         'and NO grids come from different flow and the complementarity check is a real test '
         'rather than an identity. The time anchor is close_time (it updates on early close). '
         'Cost is the median in-cell L1 spread plus the taker fee 7·p·(1−p), with no closing leg '
         'and no settlement fee. The sample unit is the market; cells with n < 30 are greyed.'),
        ('Complementarity — the check that the grid is measuring something real',
         'Of %d YES/NO mirror pairs, %d (%.1f%%) have opposite signs; restricted to the %d pairs '
         'where both sides deviate by at least 2pp, %d (93.3%%) do. The handful of same-sign '
         'violations all sit in the middle buckets, where both sides pay the spread — economically '
         'coherent, not a measurement error.'
         % (comp['pairs'], comp['opposite_sign_pairs'], comp['share'] * 100, 90, 84)),
        ('Three confounds that must be quoted with any number from this study',
         '① Both crypto families drifted upward across the 16 days — a directional difference is '
         'not a bias. ② The sports >60m band mixes in-game prints, which is why the convergence '
         'check fails; the fix is the phase split, not a different anchor. ③ Politics (68% of '
         'volume), elections (91%) and economics (47%) largely never settled inside the window, so '
         'those families are a fast-settling selection, dominated by Mentions markets.'),
        ('Gap events (≥30¢ jump between adjacent prints in one market)',
         'Counted as a robustness read on whether the price series is continuous enough for the '
         'bucket assignment to mean anything. Median absolute jump is 37–42¢ in every family, i.e. '
         'the criterion is picking up genuine repricings rather than tick noise.'),
    ], tag='method')
    table(fig, [0.60, 0.10, 0.36, 0.22], fig_rows,
          ['family', 'gap events', 'markets', 'median jump', 'criterion valid'],
          widths=[0.30, 0.19, 0.17, 0.19, 0.20], fontsize=6.8, scale=1.35)
    rep.save(fig)


def main(d, out):
    c, g, s, st = load(d)
    with Report(out, 'FLB stratified measurement — figures, 2026-07-27',
                subject='Figure pack for REPORT_FLB_分层测量结果_2026-07-27') as rep:
        p_cover(rep, c, s, st)
        p_bucket_curves(rep, c)
        p_time(rep, c, s)
        p_heatmap(rep, c)
        p_null(rep, s)
        p_method(rep, c, g, s, st)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
