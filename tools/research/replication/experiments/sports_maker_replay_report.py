#!/usr/bin/env python
"""Sports maker replay v0 — results PDF.

Reads the replay artifacts (phase_grid / worst_bills / daily_pnl / queue_depth /
portfolio_stats) and renders the house-style report.

Usage: python sports_maker_replay_report.py <artifact_dir> <out.pdf>
"""
import sys, os, json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication.report import (Report, header, footnote, table, cover, prose_page,
                               wrap, money, bar_labels, barh_labels, zero_line,
                               small_multiples, S1, S2, S3, S4, SEQ4, POS, NEG,
                               INK, INK2, MUTED, AXIS, GRID, SURFACE, PLANE,
                               GOOD, CRIT)

FAM_LABEL = {'sports_game': 'Single game (match result)',
             'sports_prop': 'Props (in-game / season markets)'}
PHASES = ['pregame', 'ingame', 'unknown']
PHASE_LABEL = {'pregame': 'pre-game', 'ingame': 'in-game', 'unknown': 'not event-anchored'}
GRP_LABEL = {'favorite': 'favourite 90–99¢', 'longshot': 'longshot 1–20¢'}
BUCKETS = ['01-05', '05-10', '10-20', '20-35', '35-50', '50-65', '65-80', '80-90',
           '90-95', '95-99']


def usd(v):
    """Signed dollars with a real minus sign — '$-178' reads as a typo."""
    return ('−$%s' if v < 0 else '$%s') % format(abs(round(v)), ',')


def load(d):
    pg = pd.read_csv(os.path.join(d, 'phase_grid.csv'))
    wb = pd.read_csv(os.path.join(d, 'worst_bills.csv'))
    dp = pd.read_csv(os.path.join(d, 'daily_pnl.csv'))
    qd = pd.read_csv(os.path.join(d, 'queue_depth.csv'))
    st = json.load(open(os.path.join(d, 'portfolio_stats.json')))
    return pg, wb, dp, qd, st


# ------------------------------------------------------------------ page 1
def p_cover(rep, pg, wb, dp, st):
    fig = rep.page()
    lines = wb.groupby(['family', 'grp'])['total_usd'].sum()
    prop_fav = lines.get(('sports_prop', 'favorite'), np.nan)
    game_long = lines.get(('sports_game', 'longshot'), np.nan)
    days = int(wb['days'].max())
    cover(
        fig,
        'Sports maker replay v0',
        'Does the favourite–longshot table survive contact with phase, cost and queue?',
        'Follow-on to the FLB stratified measurement. Same 15 sealed trading days, but the '
        'sports cells are now split pre-game vs in-game, priced as a one-lot maker who fades '
        'the taker, and charged the real worst-case bill.',
        tiles=[
            ('+%s' % money(prop_fav), 'fading favourites on props, 15 days, one lot per cell', GOOD),
            ('−10¢', 'worst single fill on the favourite line — structurally capped', GOOD),
            ('%s' % money(game_long), 'the same trade on single-game longshots: the tax is gone', CRIT),
            ('25–33', 'contracts of displayed depth on the prop favourite NO side — the real ceiling', INK),
        ],
        body=(
            'Three pre-registered questions, three answers.\n\n'
            '  ①  Is favourite over-pricing a pre-game or an in-game phenomenon? **Both, and the '
            'pre-game unit is fatter.** Single-game favourites are over-priced by +11.4pp pre-game '
            '(n=182) against +2.8pp in-game (n≈1,300). That releases the FLB report\'s suspended '
            'verdict on this end: it is not just chasing a team that went ahead.\n\n'
            '  ②  What does the four-line worst-case bill look like? **The two ends have opposite '
            'shapes.** Fading favourites: small wins, high frequency, worst single fill capped at '
            '−10¢, drawdown ≈ 0. Fading longshots: positive on props, negative on single games, '
            'and every line carries a −94 to −99¢ tail. The +10~16pp lottery tax in the FLB table '
            'only lives inside the last ten minutes; aggregate the whole in-game phase and fair '
            'pricing earlier in the match dilutes it away.\n\n'
            '  ③  Is there room to trade it? **Not much, and the constraint is the queue, not the '
            'flow.** The prop favourite NO side shows 25–33 contracts of depth against 49.7M '
            'contracts of volume in the same cells.'
        ),
        footer='%d trading days (2026-07-10..24) · %s replayed positions (%s in-game, %s pre-game) · '
               'fill model = one lot inherited at the volume-weighted taker price, NOT a per-event '
               'queue simulation — Q3 measures how far that proxy sits from reality'
               % (days, f"{st['positions']:,}", f"{st['phases']['ingame']:,}",
                  f"{st['phases']['pregame']:,}"),
    )
    rep.save(fig)


# ------------------------------------------------------------------ page 2
def p_phase(rep, pg):
    fig = rep.page()
    header(fig, 'Q1 · Is the favourite over-priced before the match, or only during it?',
           'Taker over-payment = price paid − realized win rate, in percentage points. '
           'Positive means the taker overpaid and the maker on the other side collected it.',
           'over-payment by phase')
    fams = ['sports_game', 'sports_prop']
    handles = []
    for k, fam in enumerate(fams):
        ax = fig.add_axes([0.075 + k * 0.49, 0.19, 0.40, 0.585])
        d = pg[pg['family'] == fam]
        x = np.arange(len(PHASES))
        w = 0.20
        combos = [('favorite', 'yes', S1, None), ('favorite', 'no', S1, '////'),
                  ('longshot', 'yes', S2, None), ('longshot', 'no', S2, '////')]
        for j, (grp, side, col, hatch) in enumerate(combos):
            vals, ns = [], []
            for ph in PHASES:
                r = d[(d['grp'] == grp) & (d['taker_side'] == side) & (d['phase'] == ph)]
                vals.append(float(r['bias_pp'].iloc[0]) if len(r) else np.nan)
                ns.append(int(r['n'].iloc[0]) if len(r) else 0)
            bs = ax.bar(x + (j - 1.5) * w, vals, w * 0.9, color=col, hatch=hatch,
                        edgecolor=SURFACE, lw=0.6,
                        label='%s · taker buys %s' % (GRP_LABEL[grp].split()[0], side.upper()))
            if k == 0:
                handles.append(bs[0])
            bar_labels(ax, bs, vals, '%+.1f', fs=6.2)
        zero_line(ax)
        ax.set_xticks(x)
        ax.set_xticklabels([PHASE_LABEL[p] for p in PHASES], fontsize=8.5)
        ax.set_ylabel('taker over-payment (pp)' if k == 0 else '')
        ax.set_title(FAM_LABEL[fam], fontsize=10, pad=8)
    fig.legend(handles=handles, ncol=4, fontsize=8, loc='upper center',
               bbox_to_anchor=(0.52, 0.865))
    footnote(fig, 'Hatched bars are the NO side of the same cell — a genuine second sample, not a '
             'mirror, because the two sides are traded by different flow. "Not event-anchored" is '
             'the residual: season-long and multi-event props with no single kick-off to split on. '
             'Read the favourite columns (blue): over-payment is present in every phase and every '
             'family, largest pre-game on single games (+11.4pp on the YES side, n=182). The '
             'longshot columns (orange) are near zero or negative once the whole phase is '
             'aggregated — which is the correction this replay exists to deliver.')
    rep.save(fig)


# ------------------------------------------------------------------ page 3
def p_curves(rep, dp, wb):
    fig = rep.page()
    header(fig, 'Q2 · The four lines, day by day',
           'Cumulative P&L of a one-lot maker fading each end, all phases aggregated. '
           'The shape matters more than the level: what we need is a line with no cliff in it.',
           'cumulative P&L')
    ax = fig.add_axes([0.075, 0.20, 0.55, 0.58])
    dp = dp.copy()
    dp['date'] = pd.to_datetime(dp['date'])
    lines = [('sports_prop', 'favorite', S1, '-'), ('sports_game', 'favorite', S2, '-'),
             ('sports_prop', 'longshot', S3, '--'), ('sports_game', 'longshot', S4, '--')]
    for fam, grp, col, ls in lines:
        s = (dp[(dp['family'] == fam) & (dp['grp'] == grp)]
             .groupby('date')['pnl_c'].sum().sort_index().cumsum())
        ax.plot(s.index, s.values, color=col, lw=2.1, ls=ls,
                label='%s · %s' % (FAM_LABEL[fam].split(' (')[0], GRP_LABEL[grp].split()[0]))
        ax.annotate(usd(s.values[-1]), (s.index[-1], s.values[-1]),
                    textcoords='offset points', xytext=(6, 0), fontsize=7.6, color=col,
                    va='center')
    zero_line(ax)
    ax.set_ylabel('cumulative USD, one lot per cell')
    ax.legend(fontsize=8, loc='upper left')
    ax.tick_params(axis='x', labelrotation=25, labelsize=7.4)
    for lbl in ax.get_xticklabels():
        lbl.set_ha('right')
    ax.set_xlim(dp['date'].min(), dp['date'].max() + pd.Timedelta(days=1.6))

    rows = []
    for fam, grp, _, _ in lines:
        w = wb[(wb['family'] == fam) & (wb['grp'] == grp)]
        tot = w['total_usd'].sum()
        rows.append([FAM_LABEL[fam].split(' (')[0], GRP_LABEL[grp].split()[0],
                     usd(tot),
                     '%+.2f' % np.average(w['ev_c_per_lot'], weights=w['n_positions']),
                     '%.0f¢' % w['worst_single_fill_c'].min(),
                     usd(w['worst_day_usd'].min()),
                     usd(w['max_drawdown_usd'].min()),
                     f"{int(w['n_positions'].sum()):,}"])
    table(fig, [0.655, 0.30, 0.315, 0.42], rows,
          ['book', 'end', 'total', 'EV\n¢/lot', 'worst\nfill', 'worst\nday', 'max draw-\ndown', 'fills'],
          widths=[0.21, 0.12, 0.12, 0.10, 0.10, 0.11, 0.13, 0.14], fontsize=6.6, scale=1.8)
    footnote(fig, 'One lot per (market, cell) — no sizing, no compounding, no inventory. The two '
             'favourite lines climb in small steps and never give it back; the two longshot lines '
             'are step functions with cliffs, which is the same −94 to −99¢ tail showing up as '
             'realized days. The single-game longshot line ends below where it started. Loss-budget '
             'doctrine reads the last three columns, not the first: a line whose worst single fill '
             'is structurally capped at −10¢ is a different animal from one that can print −99¢.')
    rep.save(fig)


# ------------------------------------------------------------------ page 4
def p_risk(rep, wb):
    fig = rep.page()
    header(fig, 'Q2b · The worst-case bill, per line and phase',
           'The loss-budget doctrine wants four rows filled in before anything goes live: worst '
           'single fill, worst day, max drawdown, and the line total. Here they are.',
           'loss budget')
    d = wb.copy()
    d['label'] = (d['family'].map(lambda f: FAM_LABEL[f].split(' (')[0]) + ' · '
                  + d['grp'].map(lambda g: GRP_LABEL[g].split()[0]) + ' · '
                  + d['phase'].map(PHASE_LABEL))
    d = d.sort_values(['grp', 'total_usd'], ascending=[True, False])
    y = np.arange(len(d))[::-1]
    panels = [('total_usd', 'line total (USD)', 0.235),
              ('worst_single_fill_c', 'worst single fill (¢)', 0.495),
              ('max_drawdown_usd', 'max drawdown (USD)', 0.735)]
    for k, (col, lab, x0) in enumerate(panels):
        ax = fig.add_axes([x0, 0.20, 0.215, 0.60])
        vals = d[col].to_numpy(float)
        ax.barh(y, vals, color=[POS if v >= 0 else NEG for v in vals], height=0.64)
        ax.axvline(0, color=INK, lw=1.1)
        ax.grid(axis='y', visible=False)
        ax.set_xlabel(lab, fontsize=8.5)
        if k == 0:
            ax.set_yticks(y)
            ax.set_yticklabels(d['label'], fontsize=6.6)
        else:
            ax.set_yticks(y); ax.set_yticklabels([])
        pad = (max(abs(vals.min()), abs(vals.max())) or 1) * 0.30
        ax.set_xlim(min(vals.min(), 0) - pad, max(vals.max(), 0) + pad)
        barh_labels(ax, y, vals, '%+.0f', fs=6.2)
    footnote(fig, 'Sorted with the favourite lines on top. The favourite block is uniform: every '
             'row is positive, every worst single fill is exactly −10¢ (the fade is capped by how '
             'far a 90–99¢ contract can move against the maker), and drawdown is zero or a rounding '
             'error. The longshot block has the opposite signature — a −94 to −99¢ worst fill in '
             'every row, drawdowns to −$149, and one line negative outright. Same table, two '
             'completely different risk objects; only the first survives the doctrine.')
    rep.save(fig)


# ------------------------------------------------------------------ page 5
def p_queue(rep, qd, pg):
    fig = rep.page()
    header(fig, 'Q3 · How much of it could we actually get?',
           'Median displayed depth per price bucket, by book and side. This is the ceiling on the '
           'replay above — the one-lot model quietly assumes a fill that the queue has to provide.',
           'queue reality')
    for k, fam in enumerate(['sports_game', 'sports_prop']):
        ax = small_multiples(fig, k, ncol=2, top=0.19, w=0.40, h=0.60, x0=0.075, dx=0.49)
        d = qd[qd['fam'] == fam]
        present = [b for b in BUCKETS if b in set(d['bucket'])]
        x = np.arange(len(present))
        w = 0.38
        for j, side in enumerate(['yes', 'no']):
            vals = []
            for b in present:
                r = d[(d['side'] == side) & (d['bucket'] == b)]
                vals.append(float(r['med_displayed_lots'].median()) if len(r) else np.nan)
            ax.bar(x + (j - 0.5) * w, vals, w * 0.9, color=[S1, S2][j],
                   label='taker buys %s' % side.upper())
        ax.set_yscale('log')
        ax.set_xticks(x); ax.set_xticklabels(present, fontsize=7, rotation=35, ha='right')
        ax.set_title(FAM_LABEL[fam], fontsize=10)
        ax.set_ylabel('median displayed contracts (log)' if k == 0 else '', fontsize=8)
        if k == 0:
            ax.legend(fontsize=7.6)
        if fam == 'sports_prop':
            thin = d[(d['side'] == 'no') & (d['bucket'].isin(['90-95', '95-99']))]
            if len(thin):
                ax.annotate('25–33 contracts —\nthe favourite line lives here',
                            xy=(len(present) - 1.15, float(thin['med_displayed_lots'].min())),
                            xytext=(-24, 96), textcoords='offset points', fontsize=7.6,
                            color=CRIT, ha='right',
                            bbox=dict(boxstyle='round,pad=0.35', fc=SURFACE, ec=CRIT, lw=0.8),
                            arrowprops=dict(arrowstyle='->', color=CRIT, lw=1.2))
    footnote(fig, 'Log scale — the spread across buckets is two orders of magnitude. The number '
             'that matters is the prop NO side at 90–99¢: a median of 25–33 displayed contracts, '
             'against 49.7M contracts of volume traded in the same cells over the window. Capacity '
             'is queue-bound, not flow-bound. Quoting 10 lots there takes roughly a quarter of the '
             'queue pro-rata; the YES side (767 in-game / 2,754 pre-game) dilutes our share to a '
             'few per cent. Any size extrapolation from the one-lot replay has to pass through '
             'this chart first.')
    rep.save(fig)


# ------------------------------------------------------------------ page 6
def p_method(rep, st, wb):
    fig = rep.page()
    prose_page(fig, 'Method, deviations, and what this is not', [
        ('What was replayed',
         'Every sealed-day sports print that joined settlement truth was assigned to a price '
         'bucket and a phase, and a one-lot maker position was booked on the opposite side of the '
         'taker at the cell\'s volume-weighted taker price, held to settlement. %s positions across '
         '%s markets. Phase comes from the event anchor: pre-game before kick-off, in-game after '
         'it, and "not event-anchored" for season-long and multi-event props that have no single '
         'kick-off — that residual is the largest block (%s positions) and is reported separately '
         'rather than folded into either.'
         % (f"{st['positions']:,}", f"{int(wb['n_markets'].sum()):,}",
            f"{st['phases']['unknown']:,}")),
        ('The fill model, stated plainly',
         'This is a one-lot proxy that inherits the market\'s volume-weighted taker price. It is '
         'NOT a per-event queue simulation. It assumes we were at the front of the queue for one '
         'contract every time a taker crossed, which is optimistic in exactly the cells where the '
         'edge is largest. Q3 exists to measure how far that assumption sits from the displayed '
         'book. Fees are not netted in this replay — see the Who Profits report for the maker-fee '
         'multipliers, which bite on precisely these per-game sports series.'),
        ('What this corrects in the FLB report',
         'The stratified table showed a +10~16pp "lottery tax" on sports longshots inside the last '
         'ten minutes and suspended judgement on the time dimension because the convergence check '
         'failed. Splitting by phase resolves both. The favourite end is real, pre-game as well as '
         'in-game, so its verdict is released. The longshot end is a narrow-band artifact: '
         'aggregate the whole in-game phase and it nearly vanishes, and on single games it goes '
         'negative. The convergence failure was end-of-game behaviour, not a broken anchor — '
         'settlement_ts is never earlier than close_time anywhere in the warehouse.'),
        ('What it would take to trade this',
         'Four things, none of them done here: a per-event L2 queue simulation (we have the full '
         'book, so this is buildable); robustness across a second window, since 15 days inside one '
         'season is not a prior; the worst-case bill recomputed at real size and submitted for '
         'approval under the loss-budget doctrine; and registration as an RC line. The arithmetic '
         'extrapolation in the source report (+$6k/day gross at 20 lots) is arithmetic only — it '
         'ignores that larger size concentrates fills at the moments the queue gets swept, which '
         'is adverse selection by another name.'),
    ], tag='method')
    footnote(fig, 'Source report: agents/audit/REPORT_SPORTS_MAKER_REPLAY_2026-07-27.md · '
             'code: tools/research/replication/experiments/sports_maker_replay.py · '
             'artifacts: Repo Research/FLB_STRATIFIED_20260727/sports_maker_replay/')
    rep.save(fig)


def main(d, out):
    pg, wb, dp, qd, st = load(d)
    with Report(out, 'Sports maker replay v0 — 2026-07-27',
                subject='FLB follow-on: phase split, worst-case bill, queue reality') as rep:
        p_cover(rep, pg, wb, dp, st)
        p_phase(rep, pg)
        p_curves(rep, dp, wb)
        p_risk(rep, wb)
        p_queue(rep, qd, pg)
        p_method(rep, st, wb)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
