"""House report style — every research artifact ships as a PDF with graphs.

Operator rule (2026-07-27): research output is a PDF with charts, not a wall of
markdown. Import this module, do not re-invent the chrome.

    from replication.report import Report, header, footnote, table, S1, S2
    with Report('out.pdf', 'My study') as rep:
        fig = rep.page()
        header(fig, 'E1 · The thing', 'what the reader is looking at', 'tag')
        ax = fig.add_axes(rep.AX_WIDE)
        ...
        footnote(fig, 'caveats and definitions')
        rep.save(fig)

Set REPORT_PNG=<dir> to also drop one PNG per page — always look at the pages
before shipping; the palette is validated, the layout is not.

Palette is the validated light-surface instance: categorical hues are assigned
in fixed order and never cycled, sequential is one hue light→dark, diverging is
blue↔red with a grey midpoint, and status colours are reserved.
"""
import os
import textwrap  # noqa: F401  (kept for callers importing it)
import unicodedata
import datetime

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D

# ---- surfaces & ink -------------------------------------------------------
SURFACE, PLANE = '#fcfcfb', '#f9f9f7'
INK, INK2, MUTED = '#0b0b0b', '#52514e', '#898781'
GRID, AXIS = '#e1e0d9', '#c3c2b7'

# ---- categorical, fixed order (never cycle, never generate a 9th) ---------
S1, S2, S3, S4 = '#2a78d6', '#eb6834', '#1baf7a', '#eda100'   # blue orange aqua yellow
S5, S6, S7, S8 = '#e87ba4', '#008300', '#4a3aa7', '#e34948'   # magenta green violet red
SERIES = [S1, S2, S3, S4, S5, S6, S7, S8]

# ---- sequential (one hue, light→dark) and diverging poles ----------------
SEQ = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b']
SEQ4 = ['#0d366b', '#256abf', '#5598e7', '#9ec5f4']           # dark→light, ordinal
POS, NEG, MID = '#2a78d6', '#e34948', '#f0efec'

# ---- status (reserved; always shipped with a label, never colour alone) --
GOOD, WARN, SERIOUS, CRIT = '#0ca30c', '#fab219', '#ec835a', '#d03b3b'

PAGE = (11.69, 8.27)          # A4 landscape


# DejaVu carries ¢ → ▸ ① etc. but has no Han glyphs; a Chinese report built on it
# renders every character as a box.  matplotlib does NOT fall through the
# font.sans-serif list per glyph (measured on 3.9.4: the first family wins and the
# missing glyph is dropped with a warning), so a CJK report has to put a
# CJK-complete font first.  Arial Unicode MS is the one macOS ships that also
# carries ¢ ▸ ① and the typographic minus, so the two modes look the same.
FONTS_LATIN = ['DejaVu Sans']
FONTS_CJK = ['Arial Unicode MS', 'Hiragino Sans GB', 'PingFang HK', 'Heiti TC',
             'Songti SC', 'DejaVu Sans']


def style(cjk=False):
    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': FONTS_CJK if cjk else FONTS_LATIN,
        'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE,
        'savefig.facecolor': SURFACE,
        'text.color': INK, 'axes.labelcolor': INK2, 'axes.edgecolor': AXIS,
        'xtick.color': MUTED, 'ytick.color': MUTED,
        'axes.grid': True, 'grid.color': GRID, 'grid.linewidth': 0.6,
        'axes.spines.top': False, 'axes.spines.right': False,
        'axes.titlesize': 11, 'axes.titleweight': 'bold', 'font.size': 9,
        'legend.frameon': False,
    })


style()


# --------------------------------------------------------------- text
def _cols(ch):
    """Display columns of one character — CJK glyphs occupy two."""
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def dwidth(s):
    return sum(_cols(c) for c in s)


def _wrap_para(para, width, hang=''):
    """Wrap by DISPLAY width, breaking between CJK characters as well as at
    spaces — Chinese has no word spaces, so a whitespace-only wrapper either
    runs a paragraph off the page or never breaks it at all."""
    tokens, buf = [], ''
    for ch in para:
        if _cols(ch) == 2:
            if buf:
                tokens.append(buf)
                buf = ''
            tokens.append(ch)
        elif ch == ' ':
            if buf:
                tokens.append(buf)
                buf = ''
            tokens.append(' ')
        else:
            buf += ch
    if buf:
        tokens.append(buf)
    lines, cur, curw = [], '', 0
    for t in tokens:
        tw = dwidth(t)
        if curw + tw > width and cur.strip():
            lines.append(cur.rstrip())
            cur, curw = hang, dwidth(hang)
            if t == ' ':
                continue
        cur += t
        curw += tw
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or ['']


def wrap(s, width):
    """Hard-wrap to a display width matplotlib can fit, keeping paragraph
    breaks and hanging indents for ①/▸ bullets.

    Markdown emphasis is stripped rather than rendered — matplotlib has no
    inline rich text, so `**bold**` would otherwise print its asterisks.
    """
    out = []
    for para in str(s).replace('**', '').split('\n'):
        if not para.strip():
            out.append('')
            continue
        indent = para[:len(para) - len(para.lstrip())]
        hang = indent + ('    ' if para.strip()[:1] in '①②③④⑤⑥▸•-' else '')
        out.extend(_wrap_para(para, width, hang))
    return '\n'.join(out)


def header(fig, title, sub=None, tag=None):
    # titles go through the same emphasis-stripping as body copy
    fig.text(0.055, 0.955, str(title).replace('**', ''), fontsize=16,
             fontweight='bold', color=INK, va='top')
    if sub:
        fig.text(0.055, 0.905, wrap(sub, 132), fontsize=9.5, color=INK2, va='top',
                 linespacing=1.45)
    if tag:
        fig.text(0.945, 0.958, tag, fontsize=8.5, color=MUTED, ha='right', va='top')


def footnote(fig, text, width=158):
    """Bottom-margin note. Anchored at the bottom and grown upward so long
    notes never run off the page."""
    fig.text(0.055, 0.030, wrap(text, width), fontsize=7.8, color=MUTED,
             va='bottom', linespacing=1.45)


def money(x, unit='$'):
    a = abs(x)
    s = '-' if x < 0 else ''
    if a >= 1e9:
        return '%s%s%.2fB' % (s, unit, a / 1e9)
    if a >= 1e6:
        return '%s%s%.2fM' % (s, unit, a / 1e6)
    if a >= 1e3:
        return '%s%s%.0fk' % (s, unit, a / 1e3)
    return '%s%s%.0f' % (s, unit, a)


def compact(n):
    """Counts, not money: 1.2B / 340M / 12k."""
    return money(n, unit='').replace('-', '−')


# --------------------------------------------------------------- marks
def bar_labels(ax, bars, vals, fmt='%.2f', fs=8, color=INK2):
    span = ax.get_ylim()[1] - ax.get_ylim()[0]
    for b, v in zip(bars, vals):
        if not np.isfinite(v):
            continue
        off = span * 0.015
        ax.text(b.get_x() + b.get_width() / 2, v + (off if v >= 0 else -off),
                fmt % v, ha='center', va='bottom' if v >= 0 else 'top',
                fontsize=fs, color=color)


def barh_labels(ax, ys, vals, fmt='%+.2f', fs=7, color=INK2):
    for y, v in zip(ys, vals):
        if not np.isfinite(v):
            continue
        ax.text(v, y, (' ' + fmt % v) if v >= 0 else (fmt % v + ' '), va='center',
                ha='left' if v >= 0 else 'right', fontsize=fs, color=color)


def zero_line(ax, axis='y'):
    (ax.axhline if axis == 'y' else ax.axvline)(0, color=AXIS, lw=1.2, zorder=1)


def legend_dots(ax, labels, colors, markers=None, **kw):
    """Legend with fixed-size marks — scatter legends otherwise inherit the
    data's marker area and render as blobs."""
    markers = markers or ['o'] * len(labels)
    h = [Line2D([], [], marker=m, ls='', color=c, ms=8, label=l)
         for l, c, m in zip(labels, colors, markers)]
    ax.legend(handles=h, **kw)


def table(fig, rect, rows, cols, widths=None, fontsize=7.2, scale=1.5,
          italic_last=False):
    tax = fig.add_axes(rect)
    tax.axis('off')
    tbl = tax.table(cellText=rows, colLabels=cols, loc='upper center',
                    cellLoc='center', colWidths=widths)
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(fontsize)
    tbl.scale(1, scale)
    for (r, c), cell in tbl.get_celld().items():
        cell.set_edgecolor(GRID)
        if r == 0:
            cell.set_text_props(color=INK2, fontweight='bold')
            cell.set_facecolor(PLANE)
        elif italic_last and r == len(rows):
            cell.set_text_props(color=MUTED, style='italic')
    return tax


def small_multiples(fig, i, ncol=3, top=0.55, dy=0.40, w=0.245, h=0.28, x0=0.07,
                    dx=0.315):
    """Axes for panel i of a grid of small multiples — same geometry in every
    report so the pages read as one document."""
    ax = fig.add_axes([x0 + (i % ncol) * dx, top - (i // ncol) * dy, w, h])
    ax.tick_params(labelsize=7.2)
    return ax


# standard single-plot rectangles
AX_WIDE = [0.075, 0.22, 0.87, 0.58]
AX_LEFT = [0.075, 0.24, 0.55, 0.56]
AX_RIGHT = [0.695, 0.26, 0.255, 0.54]
AX_MAIN = [0.075, 0.22, 0.60, 0.58]
AX_SIDE = [0.755, 0.22, 0.195, 0.58]


# --------------------------------------------------------------- pages
def cover(fig, title, subtitle, blurb=None, tiles=None, body=None, footer=None):
    """Title page: hero title, one-line thesis, up to four stat tiles, body."""
    fig.text(0.055, 0.955, str(title).replace('**', ''), fontsize=29,
             fontweight='bold', color=INK, va='top')
    fig.text(0.055, 0.878, wrap(subtitle, 118), fontsize=12.5, color=INK2, va='top')
    if blurb:
        fig.text(0.055, 0.828, wrap(blurb, 128), fontsize=9.3, color=MUTED,
                 va='top', linespacing=1.5)
    if tiles:
        w = 0.216
        for i, (val, lab, col) in enumerate(tiles[:4]):
            x = 0.055 + i * (w + 0.012)
            fig.patches.append(plt.Rectangle((x, 0.610), w, 0.145,
                                             transform=fig.transFigure,
                                             facecolor=PLANE, edgecolor=GRID,
                                             lw=0.8, zorder=0))
            fig.text(x + 0.014, 0.722, val, fontsize=20, fontweight='bold',
                     color=col, va='top')
            fig.text(x + 0.014, 0.667, wrap(lab, 37), fontsize=8.0, color=INK2,
                     va='top', linespacing=1.4)
    if body:
        fig.text(0.055, 0.556 if tiles else 0.78, wrap(body, 124), fontsize=8.8,
                 color=INK, va='top', linespacing=1.45)
    if footer:
        fig.text(0.055, 0.072, wrap(footer, 150), fontsize=8.2, color=MUTED,
                 va='top', linespacing=1.5)


def prose_page(fig, title, blocks, tag=None, width=138, fontsize=8.8):
    """A text page that still belongs to the document — used for method notes
    and caveats that do not deserve a chart."""
    header(fig, title, None, tag)
    y = 0.86
    for head, text in blocks:
        if head:
            fig.text(0.055, y, head, fontsize=10.5, fontweight='bold', color=INK,
                     va='top')
            y -= 0.038
        w = wrap(text, width)
        fig.text(0.055, y, w, fontsize=fontsize, color=INK, va='top',
                 linespacing=1.45)
        y -= 0.021 * (w.count('\n') + 1) + 0.030


class Report:
    """PDF writer. Use as a context manager; call .page() then .save(fig)."""

    AX_WIDE, AX_LEFT, AX_RIGHT = AX_WIDE, AX_LEFT, AX_RIGHT
    AX_MAIN, AX_SIDE = AX_MAIN, AX_SIDE

    def __init__(self, path, title, author='HFT-BOT research', subject=None,
                 png_dir=None, cjk=False):
        self.path, self.title, self.author = path, title, author
        self.subject = subject
        self.png = png_dir or os.environ.get('REPORT_PNG')
        self.cjk = cjk
        self.n = 0

    def __enter__(self):
        style(self.cjk)
        self._pdf = PdfPages(self.path)
        if self.png:
            os.makedirs(self.png, exist_ok=True)
        return self

    def page(self):
        return plt.figure(figsize=PAGE)

    def save(self, fig):
        self._pdf.savefig(fig)
        self.n += 1
        if self.png:
            fig.savefig('%s/page%02d.png' % (self.png, self.n), dpi=95)
        plt.close(fig)

    def __exit__(self, *exc):
        d = self._pdf.infodict()
        d['Title'] = self.title
        d['Author'] = self.author
        if self.subject:
            d['Subject'] = self.subject
        d['CreationDate'] = datetime.datetime.now()
        self._pdf.close()
        if exc[0] is None:
            print('wrote %s (%d pages)' % (self.path, self.n))
        return False
