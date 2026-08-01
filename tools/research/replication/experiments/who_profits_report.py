#!/usr/bin/env python
"""Who Profits from Prediction? — Kalshi replication, results PDF.

Usage: python who_profits_report.py <results.json> <out.pdf>
"""
import sys, json, datetime
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Patch

# ---- palette (dataviz reference instance, light surface) -------------------
SURFACE, PLANE = '#fcfcfb', '#f9f9f7'
INK, INK2, MUTED = '#0b0b0b', '#52514e', '#898781'
GRID, AXIS = '#e1e0d9', '#c3c2b7'
S1, S2, S3, S4 = '#2a78d6', '#eb6834', '#1baf7a', '#eda100'   # blue orange aqua yellow
POS, NEG = '#2a78d6', '#e34948'                               # diverging poles
GOOD, CRIT = '#0ca30c', '#d03b3b'

plt.rcParams.update({
    'font.family': 'sans-serif',
    # DejaVu carries the arrows/markers/¢ the copy uses; Helvetica drops them
    'font.sans-serif': ['DejaVu Sans'],
    'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE,
    'savefig.facecolor': SURFACE,
    'text.color': INK, 'axes.labelcolor': INK2, 'axes.edgecolor': AXIS,
    'xtick.color': MUTED, 'ytick.color': MUTED,
    'axes.grid': True, 'grid.color': GRID, 'grid.linewidth': 0.6,
    'axes.spines.top': False, 'axes.spines.right': False,
    'axes.titlesize': 11, 'axes.titleweight': 'bold', 'font.size': 9,
    'legend.frameon': False,
})
PAGE = (11.69, 8.27)   # A4 landscape

FAM_ORDER = ['crypto_15m', 'crypto_hourly', 'sports_game', 'sports_prop',
             'politics_events', 'econ']
FAM_LABEL = {'crypto_15m': 'Crypto 15-min', 'crypto_hourly': 'Crypto hourly/daily',
             'sports_game': 'Sports game', 'sports_prop': 'Sports props',
             'politics_events': 'Politics/elections', 'econ': 'Economics'}
BANDS = ['gt60m', '10-60m', '5-10m', '2-5m', 'lt2m']
BAND_LABEL = ['>60m', '10-60m', '5-10m', '2-5m', '<2m']


def fams(res, minmarkets=30):
    e = res['e1_e2_e3']
    return [f for f in FAM_ORDER if f in e and (e[f]['markets'] or 0) >= minmarkets]


def wrap(s, width):
    """Hard-wrap to a character width matplotlib can actually fit on the page,
    keeping paragraph breaks and hanging indents for the ①/▸ bullets."""
    import textwrap
    out = []
    for para in s.split('\n'):
        if not para.strip():
            out.append('')
            continue
        indent = para[:len(para) - len(para.lstrip())]
        hang = indent + ('    ' if para.strip()[:1] in '①②③④⑤▸' else '')
        out.extend(textwrap.wrap(para, width=width, subsequent_indent=hang) or [''])
    return '\n'.join(out)


def page_header(fig, title, sub=None, tag=None):
    fig.text(0.055, 0.955, title, fontsize=16, fontweight='bold', color=INK, va='top')
    if sub:
        fig.text(0.055, 0.905, wrap(sub, 132), fontsize=9.5, color=INK2, va='top',
                 linespacing=1.45)
    if tag:
        fig.text(0.945, 0.958, tag, fontsize=8.5, color=MUTED, ha='right', va='top')


def footnote(fig, text, width=158):
    fig.text(0.055, 0.030, wrap(text, width), fontsize=7.8, color=MUTED, va='bottom',
             linespacing=1.45)


def money(x):
    a = abs(x)
    s = '-' if x < 0 else ''
    if a >= 1e6:
        return '%s$%.2fM' % (s, a / 1e6)
    if a >= 1e3:
        return '%s$%.0fk' % (s, a / 1e3)
    return '%s$%.0f' % (s, a)


def bar_labels(ax, bars, vals, fmt='%.2f', dy=0.0, fs=8):
    for b, v in zip(bars, vals):
        if not np.isfinite(v):
            continue
        off = (ax.get_ylim()[1] - ax.get_ylim()[0]) * 0.015
        ax.text(b.get_x() + b.get_width() / 2, v + (off if v >= 0 else -off - dy),
                fmt % v, ha='center', va='bottom' if v >= 0 else 'top',
                fontsize=fs, color=INK2)


# --------------------------------------------------------------- page 1
def p_title(pdf, res, meta):
    fig = plt.figure(figsize=PAGE)
    t = res['totals']
    fig.text(0.055, 0.955, 'Who Profits on Kalshi?', fontsize=29, fontweight='bold',
             color=INK, va='top')
    fig.text(0.055, 0.878, 'Execution, not forecasting — a replication of Della Vedova (2026) '
             'on our own exchange tape', fontsize=12.5, color=INK2, va='top')
    fig.text(0.055, 0.828, wrap(meta['subtitle'], 128), fontsize=9.3, color=MUTED, va='top',
             linespacing=1.5)

    # hero stat tiles
    tiles = meta['tiles']
    x0, w = 0.055, 0.216
    for i, (val, lab, col) in enumerate(tiles):
        x = x0 + i * (w + 0.012)
        fig.patches.append(plt.Rectangle((x, 0.610), w, 0.145, transform=fig.transFigure,
                                         facecolor=PLANE, edgecolor=GRID, lw=0.8, zorder=0))
        fig.text(x + 0.014, 0.722, val, fontsize=20, fontweight='bold', color=col, va='top')
        fig.text(x + 0.014, 0.667, wrap(lab, 37), fontsize=8.0, color=INK2, va='top',
                 linespacing=1.4)

    fig.text(0.055, 0.556, wrap(meta['body'], 124), fontsize=8.8, color=INK, va='top',
             linespacing=1.45)
    fig.text(0.055, 0.072, wrap(
             'Sample: %s · %s trade prints · %s contracts · %s resolved markets · '
             'settlement truth = catalog_normal finalized yes/no only'
             % (meta['days'], f"{t['trades']:,}", f"{t['contracts']:,.0f}", f"{t['markets']:,}"), 150),
             fontsize=8.2, color=MUTED, va='top', linespacing=1.5)
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 2
def p_inversion(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E1 · The inversion',
                'Paper Figure 1 analog. Each point is one market family × order role. '
                'The role that picks winners more often is the role that loses money.',
                'accuracy vs return')
    ax = fig.add_axes([0.075, 0.16, 0.60, 0.66])
    e = res['e1_e2_e3']
    ff = fams(res)
    from matplotlib.lines import Line2D
    for role, col, mark, dy in (('maker', S1, 'o', 15), ('taker', S2, 's', -19)):
        xs = [e[f]['taker_accuracy' if role == 'taker' else 'maker_accuracy'] * 100 for f in ff]
        ys = [e[f]['taker_roi' if role == 'taker' else 'maker_roi'] * 100 for f in ff]
        sz = [max(35, min(430, np.sqrt(e[f]['contracts']) / 6.5)) for f in ff]
        ax.scatter(xs, ys, s=sz, c=col, marker=mark, alpha=0.85, linewidths=1.2,
                   edgecolors=SURFACE, zorder=3)
        order = sorted(range(len(ff)), key=lambda i: xs[i])
        prev = None
        for k, i in enumerate(order):          # nudge the second of any close pair
            off = dy if prev is None or abs(xs[i] - prev) > 3.5 else dy * 2.1
            ax.annotate(FAM_LABEL[ff[i]], (xs[i], ys[i]), textcoords='offset points',
                        xytext=(0, off), ha='center', fontsize=7.4, color=INK2, zorder=4)
            prev = xs[i]
    ax.axvline(50, color=AXIS, lw=1.0, ls='--', zorder=1)
    ax.axhline(0, color=AXIS, lw=1.0, zorder=1)
    ax.text(50, ax.get_ylim()[1], ' coin flip', fontsize=7.4, color=MUTED, va='top')
    ax.set_xlabel('directional accuracy (%)  →  picks the winning side more often')
    ax.set_ylabel('net return on capital at risk (%)')
    ax.legend(handles=[Line2D([], [], marker='o', ls='', color=S1, ms=8, label='maker'),
                       Line2D([], [], marker='s', ls='', color=S2, ms=8, label='taker')],
              loc='lower left', fontsize=9)
    ax.set_title('Accuracy does not buy profit', color=INK)

    ax2 = fig.add_axes([0.755, 0.16, 0.195, 0.66])
    y = np.arange(len(ff))[::-1].astype(float)
    mk = [e[f]['maker_net_usd'] / 1e6 for f in ff]
    tk = [e[f]['taker_net_usd'] / 1e6 for f in ff]
    ax2.barh(y + 0.19, mk, 0.34, color=S1, label='maker')
    ax2.barh(y - 0.19, tk, 0.34, color=S2, label='taker')
    ax2.set_yticks(y); ax2.set_yticklabels([FAM_LABEL[f] for f in ff], fontsize=7.6)
    ax2.axvline(0, color=AXIS, lw=1.0)
    ax2.set_xlabel('net P&L (USD millions, 15 days)')
    ax2.set_title('who kept the money', fontsize=9.5, color=INK2)
    ax2.grid(axis='y', visible=False)
    lim = max(abs(min(tk)), max(mk)) * 1.45
    ax2.set_xlim(-lim, lim)
    for yy, v in list(zip(y + 0.19, mk)) + list(zip(y - 0.19, tk)):
        ax2.text(v, yy, (' %+.1f' % v) if v >= 0 else ('%+.1f ' % v), va='center',
                 ha='left' if v >= 0 else 'right', fontsize=6.8, color=INK2)
    ax2.legend(fontsize=7.5, loc='lower left')
    footnote(fig, 'Marker area ∝ contracts traded. Both roles are net of the fees that role actually '
             'pays: takers 0.07·p·(1−p) ¢/contract on every series, makers 0.0175·p·(1−p) on the ~80 '
             'series Kalshi lists with a maker multiplier and nothing elsewhere. Capital at risk = '
             'Σ price paid × size on that role\'s own side. Zero-sum before fees: maker gross P&L = '
             '−taker gross P&L by construction, so the gap between the two bars is exactly the '
             'exchange\'s cut.')
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 3
def p_excess(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E2 · Nobody beats the price',
                'Paper Table 2 Panel B analog. Excess accuracy = realized win rate − max(p, 1−p), '
                'the win rate a no-skill trader gets by always siding with the favourite.',
                'excess accuracy')
    e = res['e1_e2_e3']; ff = fams(res)
    ax = fig.add_axes([0.075, 0.30, 0.87, 0.50])
    x = np.arange(len(ff)); w = 0.30
    tk = [e[f]['taker_excess_accuracy'] * 100 for f in ff]
    mk = [e[f]['maker_excess_accuracy'] * 100 for f in ff]
    b1 = ax.bar(x - w / 2, mk, w, color=S1, label='maker')
    b2 = ax.bar(x + w / 2, tk, w, color=S2, label='taker')
    ax.axhline(0, color=AXIS, lw=1.2)
    ax.set_xticks(x); ax.set_xticklabels([FAM_LABEL[f] for f in ff], fontsize=9)
    ax.set_ylabel('excess accuracy (percentage points)')
    ax.legend(loc='lower right')
    bar_labels(ax, b1, mk, '%.1f'); bar_labels(ax, b2, tk, '%.1f')
    ax.set_title('Both sides of every print sit below the price-implied benchmark', color=INK)

    rows = [['family', 'raw accuracy', 'price-implied', 'excess (taker)', 'mean price paid']]
    for f in ff:
        r = e[f]
        rows.append([FAM_LABEL[f], '%.1f%%' % (r['taker_accuracy'] * 100),
                     '%.1f%%' % (r['price_implied_accuracy'] * 100),
                     '%+.1f pp' % (r['taker_excess_accuracy'] * 100),
                     '%.1f¢' % r['mean_paid_c']])
    tab_ax = fig.add_axes([0.075, 0.075, 0.60, 0.17]); tab_ax.axis('off')
    tbl = tab_ax.table(cellText=rows[1:], colLabels=rows[0], loc='center', cellLoc='center')
    tbl.auto_set_font_size(False); tbl.set_fontsize(7.6); tbl.scale(1, 1.25)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID)
        if r == 0:
            cell.set_text_props(color=INK2, fontweight='bold')
    footnote(fig, 'Raw accuracy above 50% is not skill: a contract bought at 70¢ wins ~70% of the '
             'time with no information at all. Excess accuracy nets that out. Because max(p,1−p) is '
             'symmetric in the two sides of a print, maker and taker excess accuracy sum to '
             '1−2·max(p,1−p) ≤ 0 — mechanically, at most one side can beat the price, and here neither does.')
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 4
def p_decomp(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E3 · Where the money actually comes from',
                'Paper Figure 2 analog. Taker edge per contract split into a directional part '
                '(outcome − fair) and an execution part (fair − price paid), fair = backward VWAP.',
                'edge decomposition')
    e = res['e1_e2_e3']; ff = fams(res)
    ax = fig.add_axes([0.075, 0.22, 0.60, 0.58])
    x = np.arange(len(ff)); w = 0.26
    d = [e[f]['dir_bvwap_c'] for f in ff]
    x_ = [e[f]['exe_bvwap_c'] for f in ff]
    fee = [-e[f]['taker_fee_c'] for f in ff]
    net = [e[f]['taker_net_c'] for f in ff]
    b1 = ax.bar(x - w, d, w * 0.92, color=S1, label='directional (side picked)')
    b2 = ax.bar(x, x_, w * 0.92, color=S2, label='execution (price paid)')
    b3 = ax.bar(x + w, fee, w * 0.92, color=S3, label='taker fee 0.07·p·(1−p)')
    ax.axhline(0, color=AXIS, lw=1.2)
    ax.set_xticks(x); ax.set_xticklabels([FAM_LABEL[f] for f in ff], fontsize=8.6)
    ax.set_ylabel('cents per contract, taker side')
    ax.legend(loc='best', fontsize=8.5)
    for bs, vs in ((b1, d), (b2, x_), (b3, fee)):
        bar_labels(ax, bs, vs, '%.2f', fs=7)
    ax.set_title('Taker components (maker components are the exact mirror, minus the fee)', color=INK)

    ax2 = fig.add_axes([0.755, 0.22, 0.195, 0.58])
    y = np.arange(len(ff))[::-1]
    cols = [POS if v >= 0 else NEG for v in net]
    ax2.barh(y, net, color=cols, height=0.62)
    ax2.set_xlim(min(net) * 1.35, max(0.2, max(net) * 1.2))
    ax2.set_yticks(y); ax2.set_yticklabels([FAM_LABEL[f] for f in ff], fontsize=7.6)
    ax2.axvline(0, color=AXIS, lw=1.2)
    ax2.set_xlabel('taker net ¢/contract')
    ax2.set_title('bottom line', fontsize=9.5, color=INK2)
    ax2.grid(axis='y', visible=False)
    for yy, v in zip(y, net):
        ax2.text(v, yy, ' %+.2f' % v, va='center', fontsize=7.2,
                 ha='left' if v >= 0 else 'right', color=INK2)
    footnote(fig, 'Backward VWAP is the paper\'s primary benchmark and is contaminated by the '
             'evaluated flow\'s own prior prints — see E4 for what that does to the split between the two '
             'columns. The bottom line (net ¢/contract) does not depend on the benchmark: it is '
             'outcome − price paid − fee, measured against settlement.')
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 5
def p_benchmark(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E4 · Benchmark contamination',
                'Paper Table 4 analog. The same regression — execution edge on directional edge, '
                'one observation per market × role — run under three reference prices that draw '
                'progressively less on the evaluated flow\'s own prints.',
                'cross-skill coefficient')
    g = res['e4_benchmark_gradient']; ff = [f for f in fams(res) if f in g]
    ax = fig.add_axes([0.075, 0.22, 0.87, 0.58])
    x = np.arange(len(ff)); w = 0.26
    series = [('bvwap', 'backward VWAP (all own prior prints)', S1),
              ('lvwap', 'local 50-print VWAP', S2),
              ('lag', 'lagged single print (cleanest)', S3)]
    for i, (k, lab, col) in enumerate(series):
        vals = [g[f].get(k, {}).get('beta', np.nan) for f in ff]
        bs = ax.bar(x + (i - 1) * w, vals, w * 0.92, color=col, label=lab)
        bar_labels(ax, bs, vals, '%.2f', fs=7)
    ax.axhline(0, color=AXIS, lw=1.2)
    ax.set_xticks(x); ax.set_xticklabels([FAM_LABEL[f] for f in ff], fontsize=8.6)
    ax.set_ylabel('β  (execution edge on directional edge)')
    ax.legend(loc='best', fontsize=8.5)
    ax.set_title('A "tradeoff between forecasting and execution" that exists only in the benchmark',
                 color=INK)
    footnote(fig, 'Paper: β = −0.178 under backward VWAP → −0.013 under local VWAP → 0.000 under the '
             'lagged price, and the analytical bias bound reproduces the contaminated number to within 1%. '
             'A real relation between the two skills would not move when only the reference price changes. '
             'Caveat: our unit is market × order role, not wallet — Kalshi\'s tape carries no trader identity — '
             'so this measures the mechanical benchmark bias, which is exactly what the paper attributes '
             'the negative coefficient to; it is not a wallet-level skill correlation.')
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 6
def p_lifecycle(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E5 · Lifecycle timing — when the edge is there',
                'Paper Figure 5 analog. Edge per contract by how long before the market closed the '
                'print happened. Both series are net of the fee that role pays.',
                'edge at entry')
    lc = res['e5_lifecycle']; ff = [f for f in fams(res) if lc.get(f)]
    n = len(ff); ncol = 3; nrow = int(np.ceil(n / ncol))
    for i, f in enumerate(ff):
        ax = fig.add_subplot(nrow, ncol, i + 1)
        ax.set_position([0.07 + (i % ncol) * 0.315,
                         0.55 - (i // ncol) * 0.40, 0.245, 0.28])
        rows = {r['band']: r for r in lc[f]}
        xs, mk, tk, vol = [], [], [], []
        for j, b in enumerate(BANDS):
            if b not in rows or rows[b]['contracts'] < 1e5:
                continue
            xs.append(j); mk.append(rows[b]['maker_net_c']); tk.append(rows[b]['taker_net_c'])
            vol.append(rows[b]['contracts'])
        ax.plot(xs, mk, color=S1, lw=2, marker='o', ms=5, label='maker net of fee')
        ax.plot(xs, tk, color=S2, lw=2, marker='s', ms=5, label='taker net of fee')
        ax.axhline(0, color=AXIS, lw=1.1)
        ax.set_xticks(range(len(BANDS))); ax.set_xticklabels(BAND_LABEL, fontsize=7.2)
        ax.set_title(FAM_LABEL[f], fontsize=9.5)
        ax.tick_params(labelsize=7.2)
        if i % ncol == 0:
            ax.set_ylabel('¢ / contract', fontsize=8)
        if i == 0:
            ax.legend(fontsize=7.2, loc='best')
        tot = sum(vol) or 1
        cts = ('%.1fB' % (tot / 1e9)) if tot >= 1e9 else ('%.0fM' % (tot / 1e6))
        ax.text(0.99, 0.03, '%s contracts' % cts, transform=ax.transAxes,
                ha='right', fontsize=6.8, color=MUTED)
    footnote(fig, 'Time bands are minutes before market close (settlement anchor = close_time, '
             'fallback settlement_ts). The paper finds bot edge-at-entry decays monotonically over the '
             'market lifecycle — large early, ~zero at 7–14 days, negative at the close — because the '
             'value of being right collapses as price converges on the outcome (E6). Read this chart as '
             'where in a market\'s life liquidity provision is paid, and where it is picked off.')
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 7
def p_interaction(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E6 · Why execution wins: being right stops paying',
                'Paper Table 7 analog. Edge regressed on accuracy, on distance of the traded price '
                'from 50¢, and on their interaction (market × role observations).',
                'accuracy × price distance')
    it = res['e6_interaction']; ff = [f for f in fams(res) if f in it]
    ax = fig.add_axes([0.075, 0.24, 0.55, 0.56])
    ps = [0.50, 0.65, 0.80, 0.95]
    x = np.arange(len(ff)); w = 0.2
    ramp = ['#0d366b', '#256abf', '#5598e7', '#9ec5f4']   # sequential blue, dark→light
    for i, p in enumerate(ps):
        vals = [it[f]['marginal_value_10pt'][str(p)] * 100 for f in ff]  # cents
        bs = ax.bar(x + (i - 1.5) * w, vals, w * 0.9, color=ramp[i],
                    label='price %d¢' % int(p * 100))
        bar_labels(ax, bs, vals, '%.1f', fs=6.4)
    ax.axhline(0, color=AXIS, lw=1.2)
    ax.set_xticks(x); ax.set_xticklabels([FAM_LABEL[f] for f in ff], fontsize=8,
                                         rotation=14, ha='right')
    ax.set_ylabel('¢ per contract won by +10 points of accuracy')
    ax.legend(fontsize=8, ncol=2)
    ax.set_title('The payoff to forecasting collapses as price approaches its terminal value', color=INK)

    tax = fig.add_axes([0.665, 0.30, 0.31, 0.50]); tax.axis('off')
    rows = [['family', 'β accuracy', 'β interaction', 'R²']]
    for f in ff:
        r = it[f]
        rows.append([FAM_LABEL[f], '%.3f' % r['beta_acc'], '%.3f' % r['beta_inter'],
                     '%.2f' % r['r2']])
    rows.append(['paper (Polymarket)', '1.162', '−2.244', '0.78'])
    tbl = tax.table(cellText=rows[1:], colLabels=rows[0], loc='upper center', cellLoc='center',
                    colWidths=[0.40, 0.20, 0.22, 0.14])
    tbl.auto_set_font_size(False); tbl.set_fontsize(7.2); tbl.scale(1, 1.35)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID)
        if r == 0:
            cell.set_text_props(color=INK2, fontweight='bold')
        if r == len(rows) - 1:
            cell.set_text_props(color=MUTED, style='italic')
    footnote(fig, 'Coefficients in dollars per contract. A negative interaction means a correct forecast '
             'is worth progressively less the further price has already moved toward the outcome — the '
             'binary payoff ceiling. In the paper a ten-point accuracy gain is worth 11.6¢ at 50¢ and 1.5¢ '
             'at 95¢, an eightfold collapse; that is why the trader who arrives early with no view beats '
             'the trader who arrives late with a good one.')
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 8
def p_calibration(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E7 · Are Kalshi prices honest probabilities?',
                'Realized win rate minus the price paid, per price bucket. Zero means the price was '
                'a fair probability. The dashed line is the taker fee — anything below it loses money '
                'even when the price is fair.',
                'miscalibration by family')
    cal = res['e7_calibration']; ff = [f for f in fams(res) if cal.get(f)]
    pp = np.linspace(1, 99, 99)
    feec = 7.0 * (pp / 100.0) * (1 - pp / 100.0)
    ncol = 3; nrow = int(np.ceil(len(ff) / ncol))
    for i, f in enumerate(ff):
        ax = fig.add_subplot(nrow, ncol, i + 1)
        ax.set_position([0.07 + (i % ncol) * 0.315, 0.55 - (i // ncol) * 0.40, 0.245, 0.28])
        rows = cal[f]
        xs = [r['paid_c'] for r in rows]
        ys = [r['win_rate'] * 100 - r['paid_c'] for r in rows]
        lo = [max(0.0, (r['win_rate'] - (r['ci'][0] or r['win_rate']))) * 100 for r in rows]
        hi = [max(0.0, ((r['ci'][1] or r['win_rate']) - r['win_rate'])) * 100 for r in rows]
        ax.axhline(0, color=AXIS, lw=1.1, zorder=1)
        ax.plot(pp, feec, color=NEG, lw=1.3, ls='--', zorder=2,
                label='taker fee' if i == 0 else None)
        ax.errorbar(xs, ys, yerr=[lo, hi], fmt='o', ms=5, color=S1, ecolor=GRID,
                    elinewidth=1.4, capsize=0, zorder=3,
                    label='taker edge before fee' if i == 0 else None)
        ax.set_xlim(0, 100)
        ax.set_title(FAM_LABEL[f], fontsize=9.5)
        ax.tick_params(labelsize=7.2)
        if i % ncol == 0:
            ax.set_ylabel('realized − price (¢)', fontsize=8)
        if i // ncol == nrow - 1:
            ax.set_xlabel('price paid (¢)', fontsize=8)
        if i == 0:
            ax.legend(fontsize=6.8, loc='lower right')
    footnote(fig, 'One point per price bucket, plotted as the deviation from a fair price rather than '
             'on a 0–100 diagonal, where deviations this small are invisible. Error bars are 95% cluster '
             'bootstrap over markets (1,000 resamples, cluster = market — the house rule, because '
             'per-print n inflates by tens of times). Two things to read off it. First, the tilt is the '
             'textbook favourite–longshot bias: cheap contracts settle less often than their price '
             'promises (points below zero on the left) and expensive ones more often (points above zero '
             'on the right). Second, and more important for us, the fee curve sits above almost every '
             'point — Kalshi prices are close enough to honest that lifting a fair contract is a losing '
             'trade, and selling into one is a winning trade, before either side has an opinion.')
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 9
def p_markout(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E8 · Adverse selection: how fast the maker\'s edge decays',
                'Maker edge measured two ways on the same fills: mark-to-market 5 minutes after the '
                'fill, and carried to settlement.',
                'markout vs settlement')
    mo = res['e8_markout']; lc = res['e5_lifecycle']; ff = [f for f in fams(res) if f in mo]
    ax = fig.add_axes([0.075, 0.26, 0.55, 0.54])
    x = np.arange(len(ff)); w = 0.36
    a = [mo[f]['maker_markout5_c'] for f in ff]
    b = [mo[f]['maker_settle_c'] for f in ff]
    b1 = ax.bar(x - w / 2, a, w * 0.92, color=S1, label='+5 min markout')
    b2 = ax.bar(x + w / 2, b, w * 0.92, color=S2, label='held to settlement')
    bar_labels(ax, b1, a, '%.2f', fs=7); bar_labels(ax, b2, b, '%.2f', fs=7)
    ax.axhline(0, color=AXIS, lw=1.2)
    ax.set_xticks(x); ax.set_xticklabels([FAM_LABEL[f] for f in ff], fontsize=8,
                                         rotation=14, ha='right')
    ax.set_ylabel('maker edge, ¢ per contract')
    ax.legend(fontsize=8.5)
    ax.set_title('Maker edge at two horizons', color=INK)

    ax2 = fig.add_axes([0.695, 0.26, 0.255, 0.54])
    for i, f in enumerate(ff[:3]):
        rows = {r['band']: r for r in lc.get(f, [])}
        xs = [j for j, bd in enumerate(BANDS) if bd in rows]
        ys = [rows[BANDS[j]]['maker_markout5_c'] for j in xs]
        ax2.plot(xs, ys, lw=2, marker='o', ms=4.5, color=[S1, S2, S3][i], label=FAM_LABEL[f])
    ax2.axhline(0, color=AXIS, lw=1.1)
    ax2.set_xticks(range(len(BANDS))); ax2.set_xticklabels(BAND_LABEL, fontsize=7.2)
    ax2.set_ylabel('+5 min markout, ¢/contract', fontsize=8)
    ax2.legend(fontsize=7.2)
    ax2.set_title('markout over the lifecycle', fontsize=9.5, color=INK2)
    footnote(fig, 'Markout = the market\'s own price 0–5 minutes after the fill, evaluated on the side '
             'the maker took, minus the maker\'s entry price. A positive markout is spread captured and '
             'kept; the gap between markout and settlement is what the position gives back (or gains) '
             'over the rest of the market\'s life. Coverage: fills with at least one later print inside '
             'the 5-minute window (%s).'
             % ', '.join('%s %.0f%%' % (FAM_LABEL[f], mo[f]['coverage'] * 100) for f in ff))
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 10
def p_series(pdf, res):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'E9 · Which books actually pay a market maker',
                'The 18 highest-volume series on the tape. Blue is the maker\'s gross edge per '
                'contract; the hatched part is the series\' own maker fee, which Kalshi charges on '
                'roughly eighty listed series and on no others.',
                'maker edge net of fee')
    rows = sorted(res['e9_series'], key=lambda r: -r['maker_net_c'])
    ax = fig.add_axes([0.20, 0.185, 0.50, 0.655])
    y = np.arange(len(rows))[::-1]
    net = [r['maker_net_c'] for r in rows]
    fee = [r['maker_fee_c'] for r in rows]
    lo = [max(0.0, r['maker_net_c'] - (r['maker_net_ci'][0] if r['maker_net_ci'][0] is not None
                                       else r['maker_net_c'])) for r in rows]
    hi = [max(0.0, (r['maker_net_ci'][1] if r['maker_net_ci'][1] is not None
                    else r['maker_net_c']) - r['maker_net_c']) for r in rows]
    cols = [POS if v > 0 else NEG for v in net]
    ax.barh(y, net, color=cols, height=0.66, zorder=2)
    ax.barh(y, fee, left=net, color='none', edgecolor=MUTED, hatch='////', lw=0.6,
            height=0.66, zorder=2, label='maker fee')
    ax.errorbar(net, y, xerr=[lo, hi], fmt='none', ecolor=AXIS, elinewidth=1.2,
                capsize=2, zorder=3)
    ax.axvline(0, color=INK, lw=1.2, zorder=4)
    ax.set_yticks(y)
    ax.set_yticklabels(['%s  (%.0fM ct)' % (r['series'], r['contracts'] / 1e6) for r in rows],
                       fontsize=7.4)
    ax.set_xlabel('maker edge, ¢ per contract  (bar = net, hatch = fee, whisker = 95% CI over markets)')
    ax.grid(axis='y', visible=False)
    hi_x = 2.6
    ax.set_xlim(min(-2.6, min(net) * 1.25), hi_x)
    ax.legend(fontsize=8, loc='lower right')
    for yy, r, v in zip(y, rows, net):
        ax.text(hi_x, yy, '  %s' % money(r['maker_pnl_usd']), va='center',
                fontsize=6.8, color=INK2, clip_on=False)
        if v > hi_x:                      # bar runs off scale — say by how much
            ax.text(hi_x * 0.955, yy, '%.1f ¢ ▶' % v, va='center', ha='right',
                    fontsize=6.6, color=SURFACE, fontweight='bold')
    fig.text(0.878, 0.862, 'maker P&L', fontsize=7.6, color=MUTED, ha='center')
    footnote(fig, 'Sorted by net edge. A wide confidence interval means the series\' maker profit '
             'came from a handful of markets, not from a repeatable edge — treat those as untraded. '
             'The tennis-match books (KXATPMATCH, KXWTAMATCH) are the clearest warning: gross maker '
             'edge is real but the 0.0175·p·(1−p) maker fee is the same size, so the net is zero or '
             'below. The fee-free crypto books are the opposite case.')
    pdf.savefig(fig); plt.close(fig)


# --------------------------------------------------------------- page 11
def p_table(pdf, res, meta):
    fig = plt.figure(figsize=PAGE)
    page_header(fig, 'Numbers and what they mean for us', None, 'summary')
    e = res['e1_e2_e3']; ff = fams(res)
    cols = ['family', 'markets', 'prints', 'contracts', 'taker\naccuracy',
            'excess\naccuracy', 'taker gross\n¢/ct', 'taker fee\n¢/ct', 'taker net\n¢/ct',
            'maker gross\n¢/ct', 'maker fee\n¢/ct', 'maker net ¢/ct\n[95% CI]',
            'taker P&L', 'maker P&L', 'fees to\nexchange']
    rows = []
    for f in ff:
        r = e[f]
        ci = r.get('ci_maker_net_c') or (None, None)
        cis = '' if ci[0] is None else '\n[%.2f, %.2f]' % (ci[0], ci[1])
        rows.append([FAM_LABEL[f], f"{r['markets']:,}", f"{r['trades']:,}",
                     f"{r['contracts']:,.0f}", '%.1f%%' % (r['taker_accuracy'] * 100),
                     '%+.1f pp' % (r['taker_excess_accuracy'] * 100),
                     '%+.2f' % r['taker_gross_c'], '%.2f' % r['taker_fee_c'],
                     '%+.2f' % r['taker_net_c'], '%+.2f' % r['maker_gross_c'],
                     '%.2f' % r['maker_fee_c'], '%+.2f%s' % (r['maker_net_c'], cis),
                     money(r['taker_net_usd']), money(r['maker_net_usd']),
                     money(r['exchange_fee_usd'])])
    tax = fig.add_axes([0.035, 0.60, 0.935, 0.26]); tax.axis('off')
    widths = [0.100, 0.048, 0.058, 0.066, 0.056, 0.056, 0.058, 0.052, 0.054,
              0.062, 0.054, 0.082, 0.058, 0.056, 0.056]
    tbl = tax.table(cellText=rows, colLabels=cols, loc='upper center', cellLoc='center',
                    colWidths=widths[:len(cols)])
    tbl.auto_set_font_size(False); tbl.set_fontsize(6.9); tbl.scale(1, 1.5)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID)
        if r == 0:
            cell.set_text_props(color=INK2, fontweight='bold'); cell.set_facecolor(PLANE)
    fig.text(0.045, 0.575, wrap(meta['implications'], 140), fontsize=8.5, color=INK, va='top',
             linespacing=1.42)
    footnote(fig, meta['caveats'], width=176)
    pdf.savefig(fig); plt.close(fig)


class _Tee:
    """PdfPages wrapper that also drops a PNG per page (WP_PNG=<dir>) so the
    layout can actually be looked at before shipping."""

    def __init__(self, pdf, png_dir):
        self.pdf, self.dir, self.i = pdf, png_dir, 0

    def savefig(self, fig):
        self.pdf.savefig(fig)
        if self.dir:
            self.i += 1
            fig.savefig('%s/page%02d.png' % (self.dir, self.i), dpi=95)

    def infodict(self):
        return self.pdf.infodict()


def build(res, out, meta):
    import os
    png = os.environ.get('WP_PNG')
    if png:
        os.makedirs(png, exist_ok=True)
    with PdfPages(out) as _raw:
        pdf = _Tee(_raw, png)
        p_title(pdf, res, meta)
        p_inversion(pdf, res)
        p_excess(pdf, res)
        p_decomp(pdf, res)
        p_benchmark(pdf, res)
        p_lifecycle(pdf, res)
        p_interaction(pdf, res)
        p_calibration(pdf, res)
        p_markout(pdf, res)
        p_series(pdf, res)
        p_table(pdf, res, meta)
        d = pdf.infodict()
        d['Title'] = 'Who Profits on Kalshi? — replication of Della Vedova (2026)'
        d['Author'] = 'HFT-BOT research'
        d['CreationDate'] = datetime.datetime.now()
    print('wrote', out)


if __name__ == '__main__':
    res = json.load(open(sys.argv[1]))
    meta = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else {}
    build(res, sys.argv[2], meta)
