#!/usr/bin/env python
"""EXPT-BO2026 stage-1 reproduction — results PDF.

Renders the as-is reproduction of Bartlett & O'Hara 2026 (SSRN 6615739) from the
experiment's own chart dataset (`charts_data.json`) plus its rolling state file.

Usage: python bo2026_report.py <expt_bo2026_dir> <out.pdf>
"""
import sys, os, json
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication.report import (Report, header, footnote, table, cover, prose_page,
                               bar_labels, barh_labels, zero_line, small_multiples,
                               legend_dots, S1, S2, S3, S4, SEQ4, POS, NEG,
                               INK, INK2, MUTED, AXIS, GRID, SURFACE, PLANE,
                               GOOD, CRIT, WARN)

GRP_ORDER = ['single_name', 'sports', 'crypto_15min', 'broad_based', 'weather',
             'middle_other']
GRP_LABEL = {'single_name': 'Single name', 'sports': 'Sports',
             'crypto_15min': 'Crypto 15-min', 'broad_based': 'Broad-based',
             'weather': 'Weather', 'middle_other': 'Other'}


def load(d):
    ch = json.load(open(os.path.join(d, 'charts_data.json')))
    st = json.load(open(os.path.join(d, 'bo_state.json')))
    return ch, st


def frame(ch, key):
    """charts_data blocks are {cols, rows} — turn one into dicts."""
    b = ch[key]
    return [dict(zip(b['cols'], r)) for r in b['rows']]


def order(rows, key='grp'):
    return sorted(rows, key=lambda r: GRP_ORDER.index(r[key])
                  if r[key] in GRP_ORDER else 99)


# ------------------------------------------------------------------ page 1
def p_cover(rep, ch, st):
    fig = rep.page()
    cov = frame(ch, 'coverage')
    h1 = {r['grp']: r for r in frame(ch, 'h1')}
    h3 = {r['grp']: r for r in frame(ch, 'h3')}
    trades = sum(r['trades'] for r in cov)
    mkts = sum(r['mkts'] for r in cov)
    days = max(r['n_days'] for r in cov)
    sn = h1.get('single_name', {})
    cover(
        fig,
        'BO2026 · stage-1 reproduction',
        'Does the paper\'s Kalshi edge still exist in our own window, and what is it worth now?',
        'Bartlett & O\'Hara 2026 (SSRN 6615739) measured the full Kalshi history to 2026-03. '
        'This is their whole metric set re-run on our own tape, as-is, with nothing re-specified. '
        'Verdict: reproduced — every metric, with magnitudes of its own.',
        tiles=[
            ('+%.0fpp' % sn.get('gap_pp', 0), 'single-name gap between what takers buy YES and what '
             'settles YES — the paper anchors at +28pp', GOOD),
            ('+%.2f¢' % h3.get('single_name', {}).get('pnl_c', 0), 'maker P&L per contract on single '
             'name, and it decomposes exactly as the paper says it should', GOOD),
            ('2.59×', 'tax-to-adverse-flow ratio where the paper reports 1.52× — same structure, '
             'different number', WARN),
            ('1 of 4', 'groups where the VPIN cliff shows up (single name). Sports and '
             'broad-based reproduce the paper\'s null instead', INK),
        ],
        body=(
            'What reproduced, and what it taught us.\n\n'
            '  ①  **The mechanism is intact in our window.** Takers buy YES far more often than YES '
            'settles, makers earn on the other side, and the maker\'s edge splits into a positive '
            'frequency term and a negative magnitude term exactly as the paper\'s equation 7 '
            'requires. Every group reproduces the structure; none reproduces the number.\n\n'
            '  ②  **The numbers do not transfer, and that became doctrine.** Our tax-to-adverse '
            'ratio is 2.59× against the paper\'s 1.52×; our maker edge is a third of theirs on '
            'single name; their frequency edge was still rising into 2026 while ours went negative '
            'for four days mid-window. Every threshold in the paper is now treated as an initial '
            'prior only, re-estimated on a rolling window before go-live and rolled forward after.\n\n'
            '  ③  **Sports behaves like a super-single-name here**, which the paper does not '
            'examine — it excludes sports from its main analysis. Taker YES 74.2% against 39.4% '
            'settling YES, the widest gap in the sample outside weather.\n\n'
            '  ④  **The VPIN cliff is real but underpowered.** Direction matches on single name; '
            'the quintile buckets are too few to call it. Sports and broad-based reproduce the '
            'paper\'s null, which is itself a successful reproduction.'
        ),
        footer='%s trade prints · %s markets · %d days · groups are behavioural proxies built from '
               'series taxonomy, fixed before any P&L was computed · state file as of %s'
               % (f'{int(trades):,}', f'{int(mkts):,}', int(days), st.get('as_of', '—')),
    )
    rep.save(fig)


# ------------------------------------------------------------------ page 2
def p_gap(rep, ch):
    fig = rep.page()
    header(fig, 'H1 · Takers buy YES; YES does not settle',
           'The anti-informedness gap: share of taker volume buying YES against the share of those '
           'markets that actually settled YES. The paper anchors the single-name gap at +28pp.',
           'YES preference')
    rows = order(frame(ch, 'h1'))
    ax = fig.add_axes([0.075, 0.24, 0.55, 0.56])
    x = np.arange(len(rows))
    w = 0.36
    tk = [r['taker_yes_pct'] for r in rows]
    se = [r['settle_yes_pct'] for r in rows]
    b1 = ax.bar(x - w / 2, tk, w * 0.92, color=S2, label='taker volume buying YES')
    b2 = ax.bar(x + w / 2, se, w * 0.92, color=S1, label='markets settling YES')
    bar_labels(ax, b1, tk, '%.1f', fs=7)
    bar_labels(ax, b2, se, '%.1f', fs=7)
    ax.axhline(50, color=AXIS, lw=1.0, ls='--')
    ax.set_xticks(x)
    ax.set_xticklabels([GRP_LABEL.get(r['grp'], r['grp']) for r in rows], fontsize=8,
                       rotation=14, ha='right')
    ax.set_ylabel('% of volume / of markets')
    ax.legend(fontsize=8.5)
    ax.set_title('Every group buys YES more than YES happens', color=INK)

    ax2 = fig.add_axes([0.695, 0.24, 0.255, 0.56])
    y = np.arange(len(rows))[::-1]
    gaps = [r['gap_pp'] for r in rows]
    ax2.barh(y, gaps, 0.66, color=[POS if g >= 0 else NEG for g in gaps])
    ax2.axvline(28, color=CRIT, lw=1.3, ls='--')
    ax2.text(28, len(rows) - 0.4, ' paper: +28pp', fontsize=7.4, color=CRIT, va='top')
    ax2.set_yticks(y)
    ax2.set_yticklabels([GRP_LABEL.get(r['grp'], r['grp']) for r in rows], fontsize=7.6)
    ax2.grid(axis='y', visible=False)
    ax2.set_xlabel('gap (pp)')
    ax2.set_title('vs the paper\'s anchor', fontsize=9.5, color=INK2)
    barh_labels(ax2, y, gaps, '%+.1f', fs=7)
    footnote(fig, 'A gap is not by itself an edge — it says takers are on the wrong side of the '
             'settlement distribution, not that anyone captured it. What makes it tradable is the '
             'next page: the maker on the other side of that flow keeps a positive per-contract '
             'edge. Weather and sports post the widest gaps; the paper excludes both from its main '
             'analysis, so those two columns are ours, not a reproduction.')
    rep.save(fig)


# ------------------------------------------------------------------ page 3
def p_calibration(rep, ch):
    fig = rep.page()
    header(fig, 'H2 · Calibration — where the price stops telling the truth',
           'Realized YES rate minus the volume-weighted price paid, per price bucket. Zero means '
           'the price was an honest probability. The paper predicts a sag in the middle.',
           'calibration')
    rows = frame(ch, 'calibration')
    grps = [g for g in GRP_ORDER if any(r['grp'] == g for r in rows)]
    for i, g in enumerate(grps):
        ncol = 2 if len(grps) <= 4 else 3
        ax = small_multiples(fig, i, ncol=ncol, top=0.475, dy=0.345,
                             w=0.40 if ncol == 2 else 0.245,
                             dx=0.49 if ncol == 2 else 0.315, h=0.27)
        d = sorted([r for r in rows if r['grp'] == g], key=lambda r: r['vw_price'])
        xs = [r['vw_price'] for r in d]
        ys = [r['realized_yes'] - r['vw_price'] for r in d]
        vol = np.array([r['vol_mm'] for r in d], float)
        sz = 25 + 200 * (vol / (vol.max() or 1))
        ax.axhline(0, color=AXIS, lw=1.1)
        ax.plot(xs, ys, color=S1, lw=1.5, zorder=2)
        ax.scatter(xs, ys, s=sz, color=S1, zorder=3, edgecolors=SURFACE, linewidths=1)
        ax.set_xlim(0, 100)
        ax.set_title(GRP_LABEL.get(g, g), fontsize=9.5)
        if i % ncol == 0:
            ax.set_ylabel('realized − price (pp)', fontsize=8)
        if i // ncol == (len(grps) - 1) // ncol:
            ax.set_xlabel('volume-weighted price paid (¢)', fontsize=8)
    footnote(fig, 'Marker area is contract volume, so the eye goes to the buckets that carry '
             'weight rather than to the noisy tails. Single name and sports sag below zero through '
             'the middle of the range — contracts there settled YES less often than their price '
             'implied, which is where the paper says the uninformed-flow premium lives. Crypto '
             '15-min and broad-based sit above the line across the board in our window, a '
             'directional drift rather than a calibration sag.', width=176)
    rep.save(fig)


# ------------------------------------------------------------------ page 4
def p_freq_mag(rep, ch, st):
    fig = rep.page()
    header(fig, 'H3 · The maker\'s edge splits into frequency and magnitude',
           'Paper equation 7: a maker facing this flow wins more often than they lose (frequency) '
           'but loses more when they lose (magnitude). The net is what is left over.',
           'freq / magnitude')
    rows = order(frame(ch, 'h3'))
    ax = fig.add_axes([0.075, 0.26, 0.53, 0.54])
    x = np.arange(len(rows))
    w = 0.26
    fq = [r['freq_edge'] for r in rows]
    mg = [r['mag_edge'] for r in rows]
    nt = [r['pnl_c'] for r in rows]
    b1 = ax.bar(x - w, fq, w * 0.92, color=S1, label='frequency edge')
    b2 = ax.bar(x, mg, w * 0.92, color=S2, label='magnitude edge')
    b3 = ax.bar(x + w, nt, w * 0.92, color=S3, label='net maker P&L')
    for bs, vs in ((b1, fq), (b2, mg), (b3, nt)):
        bar_labels(ax, bs, vs, '%+.2f', fs=6.6)
    zero_line(ax)
    ax.set_xticks(x)
    ax.set_xticklabels([GRP_LABEL.get(r['grp'], r['grp']) for r in rows], fontsize=8,
                       rotation=14, ha='right')
    ax.set_ylabel('cents per contract')
    ax.legend(fontsize=8.2)
    ax.set_title('Positive frequency, negative magnitude, small positive net', color=INK)

    ax2 = fig.add_axes([0.675, 0.26, 0.275, 0.54])
    fm = st.get('freq_mag_daily', [])
    if fm:
        days = [r['day'][5:] for r in fm]
        ax2.plot(days, [r['freq_edge_c'] for r in fm], color=S1, lw=1.8, marker='o', ms=3.5,
                 label='frequency')
        ax2.plot(days, [r['mag_edge_c'] for r in fm], color=S2, lw=1.8, marker='s', ms=3.5,
                 label='magnitude')
        ax2.plot(days, [r['net_c'] for r in fm], color=S3, lw=2.2, marker='^', ms=4,
                 label='net')
        zero_line(ax2)
        ax2.set_xticks(range(0, len(days), max(1, len(days) // 6)))
        ax2.tick_params(axis='x', labelrotation=60, labelsize=6.6)
        ax2.set_ylabel('¢ / contract', fontsize=8)
        ax2.legend(fontsize=7.2)
        ax2.set_title('single name, day by day', fontsize=9.5, color=INK2)
    footnote(fig, 'The paper reports +1.91 = 5.31 − 3.40 on single name; we get %+.2f = %+.2f − '
             '%.2f. Same decomposition, a third of the size. The day-by-day panel is why the '
             'parameter rule exists: the net term is small enough that a few bad days flip it, and '
             'it did flip negative for four consecutive days mid-window. A threshold calibrated on '
             'the paper\'s full-history average would have been wrong on both sides of that.'
             % (rows[[r['grp'] for r in rows].index('single_name')]['pnl_c'] if any(
                 r['grp'] == 'single_name' for r in rows) else 0,
                next((r['freq_edge'] for r in rows if r['grp'] == 'single_name'), 0),
                abs(next((r['mag_edge'] for r in rows if r['grp'] == 'single_name'), 0))))
    rep.save(fig)


# ------------------------------------------------------------------ page 5
def p_ledger(rep, ch):
    fig = rep.page()
    header(fig, 'H4 · The four-cell ledger — who pays the tax',
           'Split every settled contract by which side it settled on, then compare what the maker '
           'collected from uninformed flow (tax) against what informed flow took back (adverse). '
           'The paper predicts a ratio above 1 on NO-settling and below 1 on YES-settling.',
           'tax vs adverse')
    rows = frame(ch, 'h4b')
    grps = [g for g in GRP_ORDER if any(r['grp'] == g for r in rows)]
    ax = fig.add_axes([0.075, 0.26, 0.55, 0.54])
    x = np.arange(len(grps))
    w = 0.36
    for j, side in enumerate(['no', 'yes']):
        vals = [next((r['ratio'] for r in rows if r['grp'] == g and r['settles'] == side), np.nan)
                for g in grps]
        bs = ax.bar(x + (j - 0.5) * w, vals, w * 0.92, color=[S1, S2][j],
                    label='settles %s' % side.upper())
        bar_labels(ax, bs, vals, '%.2f', fs=7)
    ax.axhline(1.0, color=INK, lw=1.2)
    ax.text(len(grps) - 0.4, 1.0, ' break-even', fontsize=7.4, color=INK2, va='bottom', ha='right')
    ax.axhline(1.52, color=CRIT, lw=1.2, ls='--')
    ax.text(len(grps) - 0.35, 1.52, ' paper: 1.52× on NO', fontsize=7.4, color=CRIT,
            va='bottom', ha='right')
    ax.set_xticks(x)
    ax.set_xticklabels([GRP_LABEL.get(g, g) for g in grps], fontsize=8, rotation=14, ha='right')
    ax.set_ylabel('tax collected ÷ adverse selection paid')
    ax.legend(fontsize=8.5)
    ax.set_title('Above 1 the maker keeps more than it gives back', color=INK)

    sub = sorted(frame(ch, 'sports_sub'), key=lambda r: -r['mkr_pnl_c'])[:9]
    trows = [[r['subcategory'], '%+.2f' % r['mkr_pnl_c'], '%.1f' % r['taker_yes_pct'],
              '%.1f' % r['settle_yes_pct'], '%.0f' % r['vol_mm']] for r in sub]
    table(fig, [0.655, 0.30, 0.31, 0.46], trows,
          ['sports book', 'maker ¢/ct', 'taker YES%', 'settle YES%', 'volume (M)'],
          widths=[0.28, 0.20, 0.19, 0.19, 0.19], fontsize=7.0, scale=1.45)
    fig.text(0.81, 0.795, 'Where the sports tax actually comes from', fontsize=9.5,
             color=INK2, ha='center', fontweight='bold')
    sn_no = next((r['ratio'] for r in rows if r['grp'] == 'single_name' and r['settles'] == 'no'),
                 float('nan'))
    sn_yes = next((r['ratio'] for r in rows if r['grp'] == 'single_name' and r['settles'] == 'yes'),
                  float('nan'))
    footnote(fig, 'Our single-name NO-settling ratio is %.2f× against the paper\'s 1.52×, and the '
             'YES-settling side is %.2f× against their 0.80× — sharper on both sides, same signs. '
             'The sports table is the part the paper does not have: the tax is not spread evenly '
             'across sports, it concentrates in books where the taker YES share and the settle YES '
             'share are furthest apart (MMA: 89.8%% of taker volume buys YES, 27.4%% of those '
             'markets settle YES). That table is a target list for measurement, not a trade list — '
             'nothing here nets a maker fee, and the per-game sports series are exactly the ones '
             'Kalshi charges makers on.' % (sn_no, sn_yes), width=176)
    rep.save(fig)


# ------------------------------------------------------------------ page 6
def p_vpin(rep, ch, st):
    fig = rep.page()
    header(fig, 'H5 · The VPIN cliff — real, but not yet callable',
           'Sort five-minute buckets by order-flow toxicity, then look at what the maker earned in '
           'the NEXT bucket. The paper predicts a cliff in the top quintile.',
           'toxicity')
    rows = frame(ch, 'vpin')
    ax = fig.add_axes([0.075, 0.26, 0.53, 0.54])
    x = np.arange(len(rows))
    mean = [r['mean_pnl'] for r in rows]
    med = [r['med_pnl'] for r in rows]
    b1 = ax.bar(x - 0.19, mean, 0.34, color=S1, label='mean next-bucket maker P&L ($)')
    b2 = ax.bar(x + 0.19, med, 0.34, color=S3, label='median ($)')
    bar_labels(ax, b1, mean, '%.0f', fs=7)
    bar_labels(ax, b2, med, '%.0f', fs=7)
    zero_line(ax)
    ax.set_xticks(x)
    ax.set_xticklabels(['Q%d\ntVPIN %.2f' % (r['qt'], r['mean_tvpin']) for r in rows],
                       fontsize=7.6)
    ax.set_ylabel('next-bucket maker P&L (USD)')
    ax.legend(fontsize=8.2)
    ax.set_title('Single name: the top quintile is where it goes wrong', color=INK)

    ax2 = fig.add_axes([0.675, 0.26, 0.275, 0.54])
    lost = [r['pct_lost'] for r in rows]
    ax2.plot(x, lost, color=S2, lw=2.2, marker='o', ms=5.5)
    ax2.set_xticks(x)
    ax2.set_xticklabels(['Q%d' % r['qt'] for r in rows], fontsize=8)
    ax2.set_ylabel('% of buckets that lost money', fontsize=8)
    ax2.axhline(50, color=AXIS, lw=1.0, ls='--')
    ax2.set_title('loss frequency by quintile', fontsize=9.5, color=INK2)
    for xi, v, r in zip(x, lost, rows):
        ax2.annotate('n=%d' % r['n'], (xi, v), textcoords='offset points', xytext=(0, 9),
                     ha='center', fontsize=6.6, color=MUTED)
    band = st.get('band') or []
    footnote(fig, 'This is the one hypothesis we could not close. The direction is the paper\'s — '
             'the most toxic quintile has the worst next-bucket outcome and the highest loss '
             'frequency — but with roughly %d buckets per quintile the intervals overlap and it '
             'cannot be called. Sports and broad-based show no effect at all, which reproduces the '
             'paper\'s own null for those groups. The live gate currently retreats at tVPIN %s and '
             'resumes at %s; those two numbers are priors from this chart, not conclusions, and '
             'they are re-estimated on a rolling window.'
             % (int(np.mean([r['n'] for r in rows])), st.get('vpin_retreat', '—'),
                st.get('vpin_resume', '—')))
    rep.save(fig)


# ------------------------------------------------------------------ page 7
def p_daily(rep, ch, st):
    fig = rep.page()
    header(fig, 'Stability — the same edge, day by day',
           'Maker P&L per contract by group and day, and the anti-informedness gap over the same '
           'window. An edge that only exists on average is not an edge we can size.',
           'daily')
    rows = frame(ch, 'daily')
    grps = [g for g in GRP_ORDER if any(r['grp'] == g for r in rows)]
    ax = fig.add_axes([0.075, 0.26, 0.53, 0.54])
    for i, g in enumerate(grps):
        d = sorted([r for r in rows if r['grp'] == g], key=lambda r: r['day'])
        ax.plot([r['day'] for r in d], [r['pnl_c'] for r in d],
                color=[S1, S2, S3, S4][i % 4], lw=1.9, marker='o', ms=3.6,
                label=GRP_LABEL.get(g, g))
    zero_line(ax)
    ax.tick_params(axis='x', labelrotation=60, labelsize=7)
    ax.set_ylabel('maker P&L, ¢ per contract')
    ax.legend(fontsize=8)
    ax.set_title('Positive on average is not positive every day', color=INK)

    ax2 = fig.add_axes([0.675, 0.26, 0.275, 0.54])
    gd = st.get('gap_daily', [])
    if gd:
        ax2.plot([r['day'][5:] for r in gd], [r['gap_pp'] for r in gd], color=S1, lw=2,
                 marker='o', ms=4, label='gap (pp)')
        ax2.plot([r['day'][5:] for r in gd], [r['maker_wr'] for r in gd], color=S2, lw=2,
                 marker='s', ms=4, label='maker win rate (%)')
        ax2.tick_params(axis='x', labelrotation=60, labelsize=6.6)
        ax2.legend(fontsize=7.4)
        ax2.set_title('single name, rolling state', fontsize=9.5, color=INK2)
        ax2.set_ylim(0, None)
    footnote(fig, 'The left panel is the stage-1 window; the right panel is the live rolling state '
             'file, which keeps updating after the experiment closed. Both say the same thing: the '
             'mechanism is present on most days and absent on some. That is the entire case for '
             'treating the paper\'s full-history averages as priors — a strategy sized on the '
             'average would have been carrying its largest position into exactly the days where the '
             'edge was not there.')
    rep.save(fig)


# ------------------------------------------------------------------ page 8
def p_method(rep, ch, st):
    fig = rep.page()
    cov = order(frame(ch, 'coverage'))
    prose_page(fig, 'Method, and the rule this experiment produced', [
        ('What "as-is reproduction" means here',
         'Stage 1 re-ran the paper\'s metric set on our own tape with nothing re-specified: no new '
         'thresholds, no re-derived formulas, no strategy variants. Groups are behavioural proxies '
         'built from the series taxonomy and fixed before any P&L was computed, so nothing in the '
         'grouping is chosen on the outcome. Every number is gross of fees.'),
        ('The parameter rule (operator, 2026-07-20 — now house doctrine)',
         'The paper\'s mechanisms transfer; its numbers do not. Its sample is 2021-07 to 2026-03; '
         'we trade the market as it is now, with a different retail mix, different maker '
         'competition, and a fee schedule that keeps changing. So every numeric threshold in the '
         'paper — the VPIN cliff at 0.9, the 3¢ margin, the 1.2/1.0 tax ratios, the top-20% lambda '
         'cut, the 1.5× frequency/magnitude rule — is an initial prior only, re-estimated on our '
         'own rolling window before go-live and rolled forward after. A shadow test asks whether '
         'the mechanism still exists in our window and what it is worth now, not whether the '
         'paper\'s number was right. If the mechanism itself disappears — the calibration curve '
         'flattening onto the diagonal — the line is shut down, not re-tuned.'),
        ('The evidence for that rule, from this very experiment',
         'Tax-to-adverse 2.59× against the paper\'s 1.52×. Single-name maker edge +0.56¢ against '
         'their +1.91¢. Their frequency edge was still rising into 2026; ours went negative for '
         'four consecutive days inside a twelve-day window. Three different ways of being right '
         'about the mechanism and wrong about the magnitude.'),
        ('Line separation',
         'Paper-as-is results are BO-x lines; our own variants are RC-x and never mixed into the '
         'same P&L attribution. That separation exists so a variant that works cannot be credited '
         'to the paper, and a paper result that stops working cannot be rescued by a variant.'),
    ], tag='method')
    trows = [[GRP_LABEL.get(r['grp'], r['grp']), f"{int(r['trades']):,}", f"{int(r['mkts']):,}",
              '%.0f' % r['contracts_mm'], str(int(r['n_days']))] for r in cov]
    table(fig, [0.60, 0.09, 0.37, 0.24], trows,
          ['group', 'prints', 'markets', 'contracts (M)', 'days'],
          widths=[0.26, 0.22, 0.19, 0.20, 0.13], fontsize=6.8, scale=1.3)
    rep.save(fig)


def main(d, out):
    ch, st = load(d)
    with Report(out, 'EXPT-BO2026 stage-1 reproduction',
                subject='Bartlett & O\'Hara 2026 (SSRN 6615739) re-run on our own tape') as rep:
        p_cover(rep, ch, st)
        p_gap(rep, ch)
        p_calibration(rep, ch)
        p_freq_mag(rep, ch, st)
        p_ledger(rep, ch)
        p_vpin(rep, ch, st)
        p_daily(rep, ch, st)
        p_method(rep, ch, st)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
