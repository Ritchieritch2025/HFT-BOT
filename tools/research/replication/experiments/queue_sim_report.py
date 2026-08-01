#!/usr/bin/env python
"""Queue-level capture simulation — results PDF (house style).

Reads queue_sim artifacts (capture_curve / worst_bills_by_k / phase_breakdown /
series_k10 / sim_stats) and renders the verdict report.

Usage: python queue_sim_report.py <artifact_dir> <out.pdf>
"""
import sys, os, json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from replication.report import (Report, header, footnote, table, cover, prose_page,
                               bar_labels, barh_labels, zero_line, legend_dots,
                               S1, S2, SEQ4, INK, INK2, MUTED, GOOD, CRIT, money, compact)

PROG_LABEL = {'ask': 'sit on yes-ask 90–99¢ (end long NO)',
              'bid': 'sit on yes-bid 1–10¢ (end long YES)'}
PHASE_LABEL = {'pregame': 'pre-game', 'ingame': 'in-game', 'unknown': 'not event-anchored'}


def usd(v):
    return ('−$%s' if v < 0 else '$%s') % format(abs(round(v)), ',')


def main(d, out):
    cap = pd.read_csv(os.path.join(d, 'capture_curve.csv'))
    wb = pd.read_csv(os.path.join(d, 'worst_bills_by_k.csv'))
    ph = pd.read_csv(os.path.join(d, 'phase_breakdown.csv'))
    sr = pd.read_csv(os.path.join(d, 'series_k10.csv'))
    st = json.load(open(os.path.join(d, 'sim_stats.json')))
    ks = sorted(wb['k'].unique())
    k10 = wb[wb['k'] == 10].iloc[0]
    cap10 = cap[cap['k'] == 10]
    ev10 = (cap10['pnl_usd'].sum() * 100) / max(cap10['lots'].sum(), 1e-9)
    V0_TOTAL = 4875 + 465  # unit-lot replay total (props+game favourite), context anchor

    with Report(out, 'Queue-level capture — favourite-fade line',
                subject='event-level L1 queue replay, 16 sealed days') as rep:
        # -------- cover
        fig = rep.page()
        pos_total = float(k10['total_usd'])
        verdict = ('POSITIVE after queueing' if pos_total > 0 else 'DOES NOT SURVIVE queueing')
        cover(fig,
              'Queue-level capture',
              'What a passive k-lot quoter actually keeps on the favourite end, '
              'once queue position and sweep timing are real',
              blurb='Event-level replay of L1 quotes + prints for 39 fee-netted target books over the '
                    '16 sealed days. We join the back of the displayed queue, lose our spot on every '
                    'level move, get filled only by volume that chews through the queue ahead, and '
                    'hold to settlement. Maker fees netted per fees.py. Verdict: %s.' % verdict,
              tiles=[(usd(float(k10['total_usd'])), 'net P&L, k=10 lots, 16 days',
                      GOOD if k10['total_usd'] > 0 else CRIT),
                     ('%.2f¢' % ev10, 'net EV per filled lot at k=10', INK),
                     (compact(float(k10['lots'])), 'lots filled at k=10 across %s markets'
                      % compact(int(k10['markets'])), INK),
                     (usd(float(k10['max_drawdown_usd'])), 'max drawdown of daily curve, k=10',
                      GOOD if k10['max_drawdown_usd'] > -100 else CRIT)],
              body='Context: the zero-queue unit-lot replay (1 lot in every market) made '
                   '%s over the same days. The number above is what queueing leaves of it at '
                   'k=10 — the honest capture, not the arithmetic ceiling. Fill events: %s; '
                   'markets truncated by the event cap: %d.'
                   % (usd(V0_TOTAL), compact(st['fill_events']), st['truncated_markets']),
              footer='Chain: FLB stratified measurement -> sports maker replay v0 -> fee netting -> '
                     'THIS. Next gates: shadow quoting 2 weeks, four-line bill at chosen size, '
                     'probe, $200 micro-live. Not a trading recommendation; ladder and approvals '
                     'per loss-budget doctrine.')
        rep.save(fig)

        # -------- capture curve
        fig = rep.page()
        header(fig, 'C1 · Capture curve — P&L and EV per lot vs quote size k',
               'Bars: 16-day net P&L by k (both programs). Dots: net EV per filled lot. '
               'The gap between k and k×(unit EV) is the queue tax.', 'queue_sim')
        ax = fig.add_axes(rep.AX_MAIN)
        tot = wb.set_index('k')['total_usd']
        bars = ax.bar([str(k) for k in ks], [tot[k] for k in ks], 0.62, color=S1)
        bar_labels(ax, bars, [tot[k] for k in ks], fmt='$%.0f')
        zero_line(ax)
        ax.set_xlabel('quote size k (lots per market-side)')
        ax.set_ylabel('16-day net P&L ($)')
        ax2 = fig.add_axes(rep.AX_SIDE)
        for prog, col in (('ask', S1), ('bid', S2)):
            cc = cap[cap['program'] == prog].sort_values('k')
            ax2.plot(cc['k'], cc['ev_c_per_lot'], '-o', color=col, lw=2, ms=5)
        zero_line(ax2)
        ax2.set_xscale('log')
        ax2.set_xticks(ks)
        ax2.set_xticklabels([str(k) for k in ks], fontsize=7)
        ax2.set_title('net EV ¢/lot by program', fontsize=9, loc='left')
        legend_dots(ax2, [PROG_LABEL['ask'].split(' (')[0], PROG_LABEL['bid'].split(' (')[0]],
                    [S1, S2], loc='lower left', fontsize=6.5)
        footnote(fig, 'Left: one bar per k, both programs pooled, maker fees netted. Right: EV per '
                      'filled lot — if EV holds while k grows, size costs volume share, not edge '
                      'quality; if EV decays with k, later fills are adversely selected. '
                      'No market impact from our own quote is modelled (replay optimism).')
        rep.save(fig)

        # -------- worst bills table + phase breakdown
        fig = rep.page()
        header(fig, 'C2 · Four-line worst bill by size, and where fills happen',
               'Loss-budget doctrine numbers for the simulated line at each k; phase split at k=10.',
               'queue_sim')
        rows = [[str(r.k), usd(r.total_usd), compact(r.lots), compact(r.markets),
                 usd(r.worst_single_market_usd), usd(r.worst_day_usd), usd(r.max_drawdown_usd)]
                for r in wb.itertuples()]
        table(fig, [0.06, 0.42, 0.55, 0.42], rows,
              ['k', 'total', 'lots', 'markets', 'worst market', 'worst day', 'max DD'],
              fontsize=7.4)
        axp = fig.add_axes([0.68, 0.30, 0.28, 0.48])
        p10 = ph[ph['k'] == 10].groupby('phase')['pnl_usd'].sum()
        phases = [p for p in ['pregame', 'ingame', 'unknown'] if p in p10.index]
        bars = axp.bar([PHASE_LABEL[p] for p in phases], [p10[p] for p in phases],
                       0.6, color=[SEQ4[1], SEQ4[0], SEQ4[3]][:len(phases)])
        bar_labels(axp, bars, [p10[p] for p in phases], fmt='$%.0f')
        zero_line(axp)
        axp.tick_params(labelsize=7.5)
        axp.set_title('net P&L by phase, k=10', fontsize=9, loc='left')
        footnote(fig, 'Worst market = biggest per-market loss (both programs, all fills in that '
                      'market). Daily curve anchored on settlement date. The worst single FILL '
                      'remains capped at −10¢/lot by construction of the favourite-end band.')
        rep.save(fig)

        # -------- per-series
        fig = rep.page()
        header(fig, 'C3 · Where the captured money lives — per book at k=10',
               'Top 18 and bottom 6 books by simulated net P&L. Books that die under queueing '
               'leave the target list.', 'queue_sim')
        s = sr.sort_values('pnl_usd', ascending=False)
        top = pd.concat([s.head(18), s.tail(6)]).drop_duplicates('series')
        y = np.arange(len(top))[::-1]
        ax = fig.add_axes([0.16, 0.10, 0.76, 0.76])
        ax.barh(y, top['pnl_usd'], 0.62,
                color=[S1 if v >= 0 else CRIT for v in top['pnl_usd']])
        barh_labels(ax, y, top['pnl_usd'].to_numpy(), fmt='%+.0f')
        ax.set_yticks(y)
        ax.set_yticklabels(top['series'], fontsize=7)
        zero_line(ax, axis='x')
        ax.set_title('16-day net P&L at k=10 ($)', fontsize=9, loc='left')
        footnote(fig, 'Fee-netted. Books with negative simulated capture are removed from the '
                      'deployment list even if their zero-queue edge was positive — queueing '
                      'reorders the ranking because thin-queue books fill better.')
        rep.save(fig)

        # -------- method
        fig = rep.page()
        prose_page(fig, 'Method & caveats', [
            ('Queue model',
             'Join the back of the displayed best-level queue on our side whenever the price is '
             'in band (ask program: yes-ask 90–99¢; bid program: yes-bid 1–10¢). A level move '
             're-joins at the new level (queue position lost). Displayed-size drops without a '
             'trade clamp queue-ahead to displayed (cancels assumed behind us otherwise). A '
             'print at our level first consumes the queue ahead; overflow fills us, up to k. '
             'One round per market-side; positions held to settlement; maker fees per fees.py.'),
            ('What this inherits from the chain',
             'Universe = 39 books, fee-netted positive in the unit-lot replay, minus exclusions '
             '(KXMLBRBI, KXMLBSB, KXMLBTB). Settlement truth = catalog_normal 07-24 snapshot. '
             'Sealed days 07-10..25 only.'),
            ('Known optimism',
             'Our quote adds no size to the book and never moves the market; competitors do not '
             'react to us; queue-ahead clamping is conservative but re-join-at-back is not '
             'perfectly so. Prices are exchange prints — no latency model.'),
            ('Known pessimism',
             'We never improve the price (always join, never lead), never re-quote after the '
             'k-lot round completes, and abandon queue position on every tick of the best level. '
             'A real engine does better on all three.'),
            ('Next gates',
             'Shadow quoting (2 weeks, existing engine shadow rig, target-book universe) must '
             'reproduce this capture curve; then the four-line bill at chosen k goes for '
             'approval; then probe; then $200 micro-live. This document is a measurement, '
             'not an authorisation.')], tag='queue_sim')
        rep.save(fig)
    print('report written:', out)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
