"""E15 report — results.json -> PDF with graphs (house style).

    REPORT_PNG=png python3 microstructure_e15_report.py results.json out.pdf meta.json
"""
import sys, json
import numpy as np
import matplotlib.pyplot as plt

sys.path.insert(0, __file__.rsplit('/', 3)[0])
from replication.report import (Report, header, footnote, table, cover, prose_page,  # noqa: E402
                                bar_labels, zero_line, legend_dots, wrap,
                                S1, S2, S3, S4, S5, S6, S8, INK, INK2, MUTED, GRID, AXIS,
                                PLANE, POS, NEG, GOOD, WARN, CRIT, SEQ4, compact)

FAM_LABEL = {'crypto_15m': 'crypto 15M', 'crypto_hourly': 'crypto hourly/daily',
             'sports_game': 'sports game', 'sports_prop': 'sports prop',
             'politics_events': 'politics', 'econ': 'econ'}
FAM_ORDER = ['crypto_15m', 'crypto_hourly', 'sports_game', 'sports_prop',
             'politics_events', 'econ']
BAND_LABEL = {'gt60m': '>60m', '10-60m': '10-60m', '5-10m': '5-10m',
              '2-5m': '2-5m', 'lt2m': '<2m'}
MN_LABEL = {'atm_0-5': '|mid−50|<5¢', 'near_5-15': '5-15¢', 'mid_15-30': '15-30¢',
            'far_30-45': '30-45¢', 'edge_45-50': '45-50¢'}


def fmt(x, d=2, dash='—'):
    return dash if x is None or not np.isfinite(float(x)) else ('%.*f' % (d, x))


# ------------------------------------------------------------------ cover
def page_cover(rep, R, meta):
    fig = rep.page()
    cov = R['coverage']
    g = R['sf2']['gate']
    sf8 = {s['spec']: s for s in R['sf8']['specs']}['market_fe_full']
    c15 = [x for x in R['sf1']['by_family'].items() if x[0] == 'crypto_15m'][0][1]
    cover(fig,
          'E15 · Kalshi 微结构解剖',
          'Dubach (2026) Polymarket order-book anatomy, re-measured on our own tape — '
          'plus the four cuts his venue cannot support',
          '预注册 · A 类只读研究 · 结构描述,样本内 · 零实盘,零交易规则产出。'
          '样本单位=市场,CI=市场聚类 bootstrap×1000(seed 20260727)。'
          'L1/成交:16 个封存日 07-10..07-24;L2 十档:干净三日 07-12/15/17。',
          tiles=[('%s' % compact(cov['sf1_markets']), '市场进入 SF1-K 点差表\n（%s 个两侧齐全报价秒）'
                  % compact(cov['sf1_quote_seconds']), S1),
                 ('%.0f bps' % c15['median_bps'], 'crypto 15M 中位报价价差\nPolymarket 最活跃十分位 = 400 bps', S3),
                 ('%d/%d' % (g['markets_passed'], g['markets_compared']),
                  'L2 重建过已知答案门\n(%s 次逐点比对,中位一致率 %.4f)'
                  % (compact(g['comparisons']), g['median_agreement']), S6),
                 ('%+.3f' % sf8['coef']['log_tte'], 'SF8-K 市场内 log(tte) 系数\nCI [%+.2f, %+.2f] — 排除 0'
                  % tuple(sf8['ci']['log_tte']), S2)],
          body='**四句判决。**\n'
               '① **SF1-K**:Kalshi 的价差-价位曲线以「分」计是**倒 U**(在值处最宽 10¢,两端 1-3¢);'
               '换成 bps 才长得像 PM 的「彩票溢价」——那是被 mid 做分母造出来的。'
               'crypto 15M 的 230 bps **比 PM 最活跃的十分位还窄**。\n'
               '② **SF2-K**:我们的书**不是 top-heavy**(强 top-heavy 仅 1.4%,PM 9%),但也不是均匀——'
               '**第二档比第一档还厚**(0.193 vs 0.147),PM 的剖面是单调衰减的。'
               '两侧不对称:买侧只占十档深度的 38.8%。\n'
               '③ **SF5-K**:用交易所真值 taker_side 量的有效半价差,减去 WHO_PROFITS 的 maker 毛边际 = '
               '逆向选择耗损。crypto 15M 吐回 0.44¢(62%),crypto 小时盘吐回 0.30¢(39%)。\n'
               '④ **SF8-K**:PM 把「单个市场自己临近结算时深度会不会掉」这个问题**明确推迟**了。'
               '我们用市场内面板回答:**会**,系数 +0.25,CI 排除 0——**深度撤退是独立通道**,不只是流的问题。',
          footer='预注册:agents/audit/PREREG_E15_Kalshi微结构解剖_2026-07-27.md(先于任何统计量提交,%s)· '
                 '代码:tools/research/replication/experiments/microstructure_e15{,_analyze,_report}.py · '
                 'catalog 快照 %s · taxonomy sha256 %s · 反例 15 四件套随本报告存档。'
                 % (meta['prereg_commit'], meta['catalog_snapshot'], meta['taxonomy_sha16']))
    rep.save(fig)


# ------------------------------------------------------------------ SF1-K
def page_sf1(rep, R):
    fig = rep.page()
    header(fig, 'SF1-K · 价差结构:以「分」计是倒 U,以 bps 计才像彩票溢价',
           '按每市场 dwell 加权平均 mid 分十等分。左轴 bps(与 PM 同轴),右图同一批市场的「分」价差——'
           '同一份数据,两个方向相反的故事。PM 的锚点(空心圈)是 600 个 Polymarket 市场。', 'SF1-K')
    d = R['sf1']['deciles']
    x = np.arange(len(d))
    ours = [r['median_bps'] for r in d]
    pm = [r['pm_median_bps'] for r in d]
    lo = [r['p25_bps'] for r in d]
    hi = [r['p75_bps'] for r in d]

    ax = fig.add_axes([0.075, 0.30, 0.50, 0.50])
    ax.fill_between(x, lo, hi, color=S1, alpha=0.16, lw=0)
    ax.plot(x, ours, color=S1, lw=2.2, marker='o', ms=5, label='Kalshi 中位(IQR 带)')
    ax.plot(x, pm, color=S2, lw=1.6, marker='o', ms=6, mfc='none', ls='--',
            label='Polymarket (Dubach 2026 Table 1)')
    ax.set_yscale('log')
    ax.set_xticks(x)
    ax.set_xticklabels(['%d-%d' % (r['lo'], r['hi']) for r in d], fontsize=7.5, rotation=45)
    ax.set_xlabel('市场生命期平均 mid(¢)')
    ax.set_ylabel('报价价差中位数(bps of mid,对数轴)')
    ax.legend(fontsize=8, loc='upper right')

    ax2 = fig.add_axes([0.655, 0.30, 0.29, 0.50])
    ax2.plot(x, [r['median_c'] for r in d], color=S3, lw=2.2, marker='s', ms=5)
    ax2.set_xticks(x[::2])
    ax2.set_xticklabels(['%d' % d[i]['lo'] for i in range(0, len(d), 2)], fontsize=7.5)
    ax2.set_xlabel('平均 mid(¢)')
    ax2.set_ylabel('报价价差中位数(¢)')
    ax2.set_title('同一批市场,单位换成分', fontsize=9.5)
    ax2.annotate('最宽在在值处', xy=(4.2, d[4]['median_c']), xytext=(5.4, d[4]['median_c'] * 1.15),
                 fontsize=8, color=INK2,
                 arrowprops=dict(arrowstyle='->', color=AXIS, lw=1))
    footnote(fig, '报价价差 = yes_ask − yes_bid,仅在两侧齐全(yes_bid>0 且 yes_ask<10000)时计入;'
                  'bps = 10000×价差/mid。每市场先取自身报价事件上的中位数,再在市场间取中位数(样本单位=市场)。'
                  'PM 的十分位是同样的固定十等分,他们报的是全价差 bps,与本图同轴。'
                  '注意 0-10¢ 档:我们 6,667 bps vs PM 1,818 bps,看起来我们「彩票溢价」大 3.7 倍——'
                  '但那一档我们的分价差只有 3¢,而在值档是 10¢。bps 的分母是 mid,mid→0 时它必然爆炸。'
                  '**「彩票价差溢价」在 Kalshi 上不是一种溢价,是一个除法。**')
    rep.save(fig)

    # ---- table page
    fig = rep.page()
    header(fig, 'SF1-K · 十分位表(含 tick 绑定率与空书时间占比)',
           'tick 绑定率 = 价差恰为 1 个最小变动单位的时间占比(每市场自行判定 tick:出现非整分报价 → 0.1¢,否则 1¢)。'
           '空书占比 = 两侧全空的时间占比——PM 没有这个读数,Kalshi 的墓地区有。', 'SF1-K')
    rows = [['%d-%d' % (r['lo'], r['hi']), str(r['markets']), fmt(r['median_bps'], 0),
             '[%s, %s]' % (fmt(r['p25_bps'], 0), fmt(r['p75_bps'], 0)),
             fmt(r['median_c']), '%.1f%%' % (100 * r['tick_bind']),
             '%.2f%%' % (100 * r['empty_share']),
             fmt(r['pm_median_bps'], 0), str(r['pm_markets'])] for r in d]
    table(fig, [0.055, 0.30, 0.89, 0.50], rows,
          ['mid 档 (¢)', '市场数', '价差中位 (bps)', 'IQR (bps)', '价差中位 (¢)',
           'tick 绑定率', '空书占比', 'PM 中位 (bps)', 'PM 市场数'],
          widths=[0.10, 0.09, 0.13, 0.16, 0.12, 0.11, 0.10, 0.12, 0.10], fontsize=7.6, scale=1.6)
    byf = R['sf1']['by_family']
    rows2 = [[FAM_LABEL.get(f, f), str(byf[f]['markets']), fmt(byf[f]['median_bps'], 0),
              fmt(byf[f]['median_c']), '%.1f%%' % (100 * byf[f]['tick_bind']),
              '%.2f%%' % (100 * byf[f]['empty_share']),
              '%.0f%%' % (100 * byf[f]['deci_tick_share'])]
             for f in FAM_ORDER if f in byf]
    table(fig, [0.055, 0.115, 0.60, 0.20], rows2,
          ['族', '市场数', '价差中位 (bps)', '价差中位 (¢)', 'tick 绑定率', '空书占比', '走 0.1¢ tick 的市场'],
          widths=[0.17, 0.11, 0.16, 0.15, 0.14, 0.13, 0.16], fontsize=7.4, scale=1.5)
    fig.text(0.685, 0.235, wrap(
        '分族看,「价差宽」几乎完全是**市场选择**而不是价位:\n'
        'crypto 15M 230 bps / 1¢,sports_game 625 bps / 2¢,'
        'sports_prop 3,916 bps / 7¢,politics 2,171 bps / 6¢。\n\n'
        'crypto 小时盘 65% 的时间**价差恰好 1 tick**——'
        '那是一条被 maker 挤满的书;crypto 15M 只有 14%,'
        '因为它走 0.1¢ deci-cent tick,有十倍的价格格子可以站。', 62),
        fontsize=8.2, color=INK, va='top', linespacing=1.5)
    footnote(fig, '跨族永不合并(操作员 2026-07-25 裁决)。族的定义见 taxonomy.py;'
                  '未识别 series 与被排除类目见方法页的反例 15 四件套。')
    rep.save(fig)


def page_sf1_life(rep, R):
    fig = rep.page()
    header(fig, 'SF1-K 加切面 · 15 分钟生命周期里价差怎么演化',
           'PM 的市场活几周,这张图他测不了。crypto 15M 从开盘到结算只有 15 分钟——'
           '看它的书在最后两分钟变成什么样。', 'SF1-K · Kalshi only')
    life = R['sf1']['lifecycle']
    bands = ['gt60m', '10-60m', '5-10m', '2-5m', 'lt2m']

    ax = fig.add_axes([0.075, 0.545, 0.40, 0.29])
    ax2 = fig.add_axes([0.575, 0.545, 0.37, 0.29])
    ax3 = fig.add_axes([0.075, 0.135, 0.40, 0.29])
    ax4 = fig.add_axes([0.575, 0.135, 0.37, 0.29])
    cols = dict(zip(FAM_ORDER, [S1, S2, S3, S4, S5, S6]))
    for f in FAM_ORDER:
        rr = life.get(f, [])
        if not rr:
            continue
        xs = [bands.index(r['band']) for r in rr]
        for a, key in ((ax, 'median_bps'), (ax2, 'median_c'), (ax3, 'tick_bind'), (ax4, 'empty_share')):
            a.plot(xs, [r[key] for r in rr], color=cols[f], lw=2 if f == 'crypto_15m' else 1.3,
                   marker='o', ms=4.5 if f == 'crypto_15m' else 3,
                   alpha=1.0 if f == 'crypto_15m' else 0.75, label=FAM_LABEL[f])
    for a, t, yl in ((ax, '报价价差(bps)', 'bps'), (ax2, '报价价差(¢)', '¢'),
                     (ax3, 'tick 绑定率(时间占比)', ''), (ax4, '空书时间占比', '')):
        a.set_xticks(range(5))
        a.set_xticklabels([BAND_LABEL[b] for b in bands], fontsize=7.5)
        a.set_title(t, fontsize=9.5)
        a.set_xlabel('距收盘', fontsize=8)
    ax.set_yscale('log')
    ax.legend(fontsize=7, ncol=2, loc='upper left')
    footnote(fig, 'crypto 15M 的四张图连起来是一个完整的死亡过程:'
                  '**bps 价差从 220 涨到 370(看着变差),分价差却从 1.00¢ 收到 0.10¢——收到了最小变动单位本身;'
                  'tick 绑定率 0% → 63%;然后 33% 的时间书是空的。**'
                  'bps 上涨只是因为 mid 从 49¢ 掉到 40¢(分母变小)。真实的故事是:'
                  '临近结算时报价格子被压到一个 tick,做市商无处可站,于是干脆不站——书直接消失三分之一的时间。'
                  '其他族的空书占比在 <2m 段同样跳到 25-46%,只有 crypto 15M 在此之前就已经 0%。'
                  '每个族每个 tte 段都要求该市场在该段有 ≥5 个两侧齐全报价秒。')
    rep.save(fig)


# ------------------------------------------------------------------ SF2-K
def page_sf2(rep, R):
    fig = rep.page()
    sh = R['sf2']['shape']
    header(fig, 'SF2-K · 深度分布形状:不是 top-heavy,但**第二档比第一档厚**',
           '每档占前十档累计深度的份额(先在市场内对时间取平均,再做比值——与 PM 同口径)。'
           '均匀零假设 = 每档 0.10。', 'SF2-K')
    lv = R['sf2']['overall']['levels']
    x = np.arange(1, 11)
    med = [l['median'] for l in lv]
    ax = fig.add_axes([0.075, 0.31, 0.52, 0.49])
    ax.fill_between(x, [l['p10'] for l in lv], [l['p90'] for l in lv], color=S1, alpha=0.10, lw=0)
    ax.fill_between(x, [l['p25'] for l in lv], [l['p75'] for l in lv], color=S1, alpha=0.20, lw=0)
    ax.plot(x, med, color=S1, lw=2.4, marker='o', ms=5.5, label='Kalshi 中位(IQR / p10-p90)')
    ax.plot(x, [l['pm_median'] for l in lv], color=S2, lw=1.6, ls='--', marker='o', ms=6,
            mfc='none', label='Polymarket (Dubach Table 2)')
    ax.axhline(0.10, color=MUTED, lw=1.2, ls=':', label='均匀零假设 0.10')
    ax.set_xticks(x)
    ax.set_xlabel('档位(自最优价向内)')
    ax.set_ylabel('占前十档累计深度的份额')
    ax.legend(fontsize=8)
    ax.annotate('L2 > L1', xy=(2, med[1]), xytext=(3.1, med[1] * 1.10), fontsize=9,
                color=S8, fontweight='bold',
                arrowprops=dict(arrowstyle='->', color=S8, lw=1.2))

    rows = [['L1 份额中位', fmt(sh['l1_share_median'], 4), fmt(sh['pm']['l1_share_median'], 4)],
            ['KL(份额‖均匀)中位', fmt(sh['kl_median'], 3), fmt(sh['pm']['kl_median'], 3)],
            ['强 top-heavy (L1>0.5)', '%.1f%%' % (100 * sh['topheavy_share']),
             '%.0f%%' % (100 * sh['pm']['topheavy_share'])],
            ['L1 份额在 [0.05,0.20]', '%.0f%%' % (100 * sh['within_factor2']),
             '%.0f%%' % (100 * sh['pm']['within_factor2'])],
            ['买侧占十档深度', '%.1f%%' % (100 * sh['bid_frac_median']), '未报'],
            ['市场数', str(R['sf2']['overall']['markets']), '547']]
    table(fig, [0.625, 0.40, 0.33, 0.32], rows, ['形状统计量', 'Kalshi', 'Polymarket'],
          widths=[0.52, 0.23, 0.25], fontsize=7.6, scale=1.7)
    fig.text(0.638, 0.375, wrap(
        '两个方向相反的偏离:我们比 PM **更集中**(KL 0.150 vs 0.087),'
        '却**更少极端 top-heavy**(1.4% vs 9%)。'
        '集中度不在第一档,在第二档。', 56), fontsize=8.2, color=INK, va='top', linespacing=1.5)
    footnote(fig, '样本 = 通过已知答案门的 %d 个市场(门:重建的最优买/卖价对该市场自己的 L1 报价时间线逐点比对,'
                  '逐市场一致率 ≥99%%;共 %s 次比对,中位一致率 %.4f)。'
                  '每市场需 ≥30 个 10 秒采样点。**PM 的剖面从 L1 单调衰减到 L10;我们的在 L2 抬头。**'
                  '一个机械解释是最优价上的排队与逆向选择:站在第一档要承担被打的风险,'
                  '于是挂单往里退一个格子——但这只是猜测,本实验不产生任何交易规则(反例 13)。'
                  % (R['sf2']['overall']['markets'], compact(R['sf2']['gate']['comparisons']),
                     R['sf2']['gate']['median_agreement']))
    rep.save(fig)


def page_sf2_sides(rep, R):
    fig = rep.page()
    header(fig, 'SF2-K 加切面 ① · YES 侧和 NO 侧的书对称吗?',
           '毒性地图(E11/E13)说**流**是不对称的。书呢?买侧 = YES 买盘;卖侧 = NO 买盘镜像'
           '(yes_ask = 100 − no_bid)——两侧都是真实挂单,不是推导出来的。', 'SF2-K · Kalshi only')
    b = R['sf2']['bid_side']['levels']
    a = R['sf2']['ask_side']['levels']
    x = np.arange(1, 11)
    ax = fig.add_axes([0.075, 0.42, 0.40, 0.38])
    ax.plot(x, [l['median'] for l in b], color=S1, lw=2.2, marker='o', ms=5, label='买侧 (YES 买盘)')
    ax.plot(x, [l['median'] for l in a], color=S2, lw=2.2, marker='s', ms=5, label='卖侧 (NO 买盘镜像)')
    ax.axhline(0.10, color=MUTED, lw=1.1, ls=':')
    ax.set_xticks(x)
    ax.set_xlabel('档位')
    ax.set_ylabel('占该侧十档深度的份额')
    ax.set_title('形状:两侧几乎同形', fontsize=9.5)
    ax.legend(fontsize=8)

    ax2 = fig.add_axes([0.565, 0.42, 0.38, 0.38])
    fams = [f for f in FAM_ORDER if f in R['sf2']['by_family']]
    vals = [R['sf2']['by_family'][f]['bid_frac_median'] for f in fams]
    bars = ax2.barh(range(len(fams)), vals, color=[S3 if v >= 0.5 else S2 for v in vals], height=0.55)
    ax2.axvline(0.5, color=INK2, lw=1.4)
    ax2.set_yticks(range(len(fams)))
    ax2.set_yticklabels([FAM_LABEL[f] for f in fams], fontsize=8.5)
    ax2.set_xlabel('买侧占十档深度的比例(0.5 = 对称)')
    ax2.set_title('总量:两侧明显不对称', fontsize=9.5)
    for i, v in enumerate(vals):
        ax2.text(v + 0.008, i, '%.3f' % v, va='center', fontsize=8, color=INK2)
    ax2.set_xlim(0, max(0.62, max(vals) + 0.08))

    fig.text(0.075, 0.335, wrap(
        '**形状对称,总量不对称。** 两侧的逐档份额几乎重合(都在 L2 抬头),'
        '但卖侧扛着 %.1f%% 的十档深度。方向是一致的:愿意卖 YES(= 买 NO)的挂单比愿意买 YES 的多。'
        'crypto 15M 最接近对称(买侧 %.3f),sports_game 最偏(买侧 %.3f)。'
        '这与 E11 的结论并列而不冲突:E11 说的是**成交流**的方向毒性,这里说的是**静止挂单**的方向存量。'
        % (100 * (1 - R['sf2']['shape']['bid_frac_median']),
           R['sf2']['by_family']['crypto_15m']['bid_frac_median'],
           R['sf2']['by_family']['sports_game']['bid_frac_median']), 140),
        fontsize=8.8, color=INK, va='top', linespacing=1.5)
    rows = [[FAM_LABEL[f], str(R['sf2']['by_family'][f]['markets']),
             fmt(R['sf2']['by_family'][f]['l1_share_median'], 4),
             fmt(R['sf2']['by_family'][f]['kl_median'], 3),
             '%.1f%%' % (100 * R['sf2']['by_family'][f]['topheavy_share']),
             fmt(R['sf2']['by_family'][f]['bid_frac_median'], 3)] for f in fams]
    table(fig, [0.075, 0.06, 0.60, 0.18], rows,
          ['族', '市场数', 'L1 份额中位', 'KL vs 均匀', 'top-heavy 占比', '买侧份额'],
          widths=[0.14, 0.10, 0.13, 0.12, 0.13, 0.11], fontsize=7.6, scale=1.5)
    footnote(fig, '同一批通过门的市场。crypto 15M 只有 %d 个市场进入十档表——'
                  'L2 抓取是配额抽样(每小时约 1 个 15M 市场),不是随机样本;'
                  '这一列只作结构描述,不承载任何统计推断。'
                  % R['sf2']['by_family']['crypto_15m']['markets'])
    rep.save(fig)


def page_ladder(rep, R):
    fig = rep.page()
    L = R['ladder']
    header(fig, 'SF2-K 加切面 ② · 贴近行权价的书和远离的书,形状一样吗?',
           'Polymarket 没有行权价阶梯,这一页他的场子里不存在。KXBTCD/KXBTC 每个事件挂约 180 个行权价;'
           '隐含现货由阶梯上 mid 穿越 50¢ 的位置线性插值得到,距离 = 行权价 − 隐含现货。', 'SF2-K · Kalshi only')
    tb = L.get('abs_distance', [])
    if tb:
        x = np.arange(len(tb))
        ax = fig.add_axes([0.075, 0.42, 0.40, 0.38])
        bars = ax.bar(x, [t['median_depth'] for t in tb], color=S1, width=0.62)
        bar_labels(ax, bars, [t['median_depth'] for t in tb], fmt='%.0f', fs=7.5)
        ax.set_xticks(x)
        ax.set_xticklabels([t['band'] for t in tb], fontsize=8)
        ax.set_xlabel('|行权价 − 隐含现货|($)')
        ax.set_ylabel('顶档深度中位数(张)')
        ax.set_title('深度峰值不在平值,在 $300-600 外', fontsize=9.5)

        ax2 = fig.add_axes([0.565, 0.42, 0.38, 0.38])
        ax2.plot(x, [t['bid_frac'] for t in tb], color=S2, lw=2.2, marker='o', ms=5)
        ax2.axhline(0.5, color=INK2, lw=1.2)
        ax2.set_xticks(x)
        ax2.set_xticklabels([t['band'] for t in tb], fontsize=8)
        ax2.set_xlabel('|行权价 − 隐含现货|($)')
        ax2.set_ylabel('买侧占顶档深度')
        ax2.set_title('平值处的书是卖侧压倒性的', fontsize=9.5)

        rows = [[t['band'], str(t['n']), str(t['markets']), fmt(t['median_depth'], 0),
                 fmt(t['median_bid_depth'], 0), fmt(t['median_ask_depth'], 0),
                 fmt(t['median_spread_c']), fmt(t['median_mid_c'], 1), fmt(t['bid_frac'], 3)]
                for t in tb]
        table(fig, [0.075, 0.125, 0.87, 0.20], rows,
              ['距离 ($)', '观测', '市场', '顶档深度中位', '买侧', '卖侧', '价差 (¢)', 'mid (¢)', '买侧份额'],
              widths=[0.10, 0.08, 0.08, 0.13, 0.09, 0.09, 0.09, 0.08, 0.10],
              fontsize=7.6, scale=1.5)
    footnote(fig, '%s,%d 个事件,%s 行(每市场每 5 分钟取最后一条两侧齐全报价),干净三日。'
                  '**读数与直觉相反:平值处深度最薄(2,072 张)而 $300-600 外最厚(2,633 张)。**'
                  '两个混杂必须写在脸上:①各距离档的 mid 不同(平值 49.5¢ vs 最远档 90.5¢),'
                  '距离与价位在这张表里没有分离;②各档市场数差很多(78 vs 54)。'
                  '这一格只做结构描述,不构成任何挂单位置结论——要当结论用必须另立实验分离价位与距离。'
                  % (' / '.join(L.get('series', [])), L.get('events', 0), compact(L.get('rows', 0))))
    rep.save(fig)


# ------------------------------------------------------------------ SF5-K
def page_sf5(rep, R):
    fig = rep.page()
    header(fig, 'SF5-K · 分族有效价差,以及减掉 maker 毛边际之后剩下什么',
           '有效半价差 = dir×(成交价 − 成交时 mid),dir 用交易所真值 taker_side。'
           'Dubach 的场子做不到这一步:他从盘口推断方向只有 59% 对,导致 67% 的市场有效价差**变号**。',
           'SF5-K')
    rows = R['sf5']['by_family']
    order = [r for f in FAM_ORDER for r in rows if r['family'] == f]
    x = np.arange(len(order))
    ax = fig.add_axes([0.075, 0.465, 0.44, 0.335])
    med = [r['median_eff_c'] for r in order]
    lo = [r['median_eff_c'] - r['p25'] for r in order]
    hi = [r['p75'] - r['median_eff_c'] for r in order]
    bars = ax.bar(x, med, color=[S1 if r['family'].startswith('crypto') else S4 for r in order],
                  width=0.6)
    ax.errorbar(x, med, yerr=[lo, hi], fmt='none', ecolor=INK2, lw=1.2, capsize=3)
    bar_labels(ax, bars, med, fmt='%.2f', fs=8)
    ax.set_xticks(x)
    ax.set_xticklabels([FAM_LABEL[r['family']] for r in order], fontsize=8, rotation=20, ha='right')
    ax.set_ylabel('有效半价差中位数(¢/张),误差棒 = 市场间 IQR')
    ax.set_title('taker 每张付出的即时成本', fontsize=9.5)

    ax2 = fig.add_axes([0.60, 0.465, 0.345, 0.335])
    cr = [r for r in order if r['wp_maker_gross_c'] is not None]
    xc = np.arange(len(cr))
    w = 0.36
    b1 = ax2.bar(xc - w / 2, [r['median_eff_c'] for r in cr], w, color=S1, label='有效半价差(即时收得)')
    b2 = ax2.bar(xc + w / 2, [r['wp_maker_gross_c'] for r in cr], w, color=S3,
                 label='WHO_PROFITS maker 毛边际(结算兑现)')
    bar_labels(ax2, b1, [r['median_eff_c'] for r in cr], fmt='%.3f', fs=7.5)
    bar_labels(ax2, b2, [r['wp_maker_gross_c'] for r in cr], fmt='%.3f', fs=7.5)
    ax2.set_xticks(xc)
    ax2.set_xticklabels([FAM_LABEL[r['family']] for r in cr], fontsize=8)
    ax2.set_ylabel('¢/张')
    ax2.set_title('差 = 逆向选择耗损', fontsize=9.5)
    ax2.set_ylim(0, max(r['median_eff_c'] for r in cr) * 1.45)
    ax2.legend(fontsize=7.2, loc='upper left')

    tr = [[FAM_LABEL[r['family']], compact(r['markets']), compact(r['trades']),
           fmt(r['median_eff_c'], 3), '[%s, %s]' % (fmt(r['p25'], 3), fmt(r['p75'], 3)),
           '[%s, %s]' % (fmt(r['ci_mean_eff_c'][0], 3), fmt(r['ci_mean_eff_c'][1], 3)),
           fmt(r['median_eff_bps'], 0),
           fmt(r['wp_maker_gross_c'], 3), fmt(r['adverse_selection_c'], 3),
           ('%.0f%%' % (100 * r['adverse_selection_c'] / r['median_eff_c']))
           if r['adverse_selection_c'] is not None else '—'] for r in order]
    table(fig, [0.055, 0.135, 0.89, 0.24], tr,
          ['族', '市场', '成交笔数', '有效半价差 (¢)', '市场间 IQR', '均值聚类 CI',
           'bps', 'maker 毛边际', '逆向选择耗损', '耗损占比'],
          widths=[0.12, 0.07, 0.09, 0.12, 0.13, 0.13, 0.06, 0.10, 0.10, 0.08],
          fontsize=7.4, scale=1.6)
    footnote(fig, 'mid 取成交时刻严格之前最近一条两侧齐全 L1 报价,陈旧上限 60 秒,超时该笔丢弃;'
                  '每市场需 ≥10 笔可匹配成交。CI = 市场聚类 bootstrap×1000。'
                  'maker 毛边际取自 WHO_PROFITS(17 封存日,结算真值口径),两者窗口不完全相同,'
                  '相减得到的耗损是**量级读数不是精算**。'
                  '**读法(反例 14)**:crypto 15M 的 taker 每张即时付 0.715¢,而平均在场 maker 最后只兑现 0.273¢——'
                  '**0.44¢(62%)在成交后被行情走掉了**。crypto 小时盘只吐回 39%。'
                  '这不是我们的 EV,是「平均在场者」的地板;我们的报价宽度、size、止损一律由自家成交数据定。')
    rep.save(fig)


def page_sf5_shape(rep, R):
    fig = rep.page()
    header(fig, 'SF5-K · 有效价差沿价位轴与生命周期的形状:六个族,六个形状',
           '左:按成交价分十档(与 E11/E13 的毒性轴同轴)。右:按距收盘分段。'
           '这两张图放在一起是为了回答「哪一族的耗损最大」之外的问题——**它长在什么地方**。', 'SF5-K')
    ax = fig.add_axes([0.075, 0.42, 0.40, 0.38])
    cols = dict(zip(FAM_ORDER, [S1, S2, S3, S4, S5, S6]))
    bk = R['sf5']['by_bucket']
    import replication.taxonomy as tx
    for f in FAM_ORDER:
        rr = bk.get(f, [])
        if len(rr) < 3:
            continue
        xs = [tx.BUCKETS.index(r['bucket']) for r in rr]
        ax.plot(xs, [r['median_eff_c'] for r in rr], color=cols[f], lw=1.8, marker='o', ms=3.5,
                label=FAM_LABEL[f])
    ax.set_xticks(range(len(tx.BUCKETS)))
    ax.set_xticklabels(tx.BUCKETS, fontsize=6.8, rotation=55)
    ax.set_xlabel('成交价档(¢)')
    ax.set_ylabel('有效半价差中位数(¢)')
    ax.set_title('沿价位轴', fontsize=9.5)
    ax.legend(fontsize=7, ncol=2)

    ax2 = fig.add_axes([0.565, 0.42, 0.38, 0.38])
    bands = ['gt60m', '10-60m', '5-10m', '2-5m', 'lt2m']
    for f in FAM_ORDER:
        rr = R['sf5']['lifecycle'].get(f, [])
        if len(rr) < 2:
            continue
        xs = [bands.index(r['band']) for r in rr]
        ax2.plot(xs, [r['median_eff_c'] for r in rr], color=cols[f], lw=1.8, marker='o', ms=3.5,
                 label=FAM_LABEL[f])
    ax2.set_xticks(range(5))
    ax2.set_xticklabels([BAND_LABEL[b] for b in bands], fontsize=8)
    ax2.set_xlabel('距收盘')
    ax2.set_ylabel('有效半价差中位数(¢)')
    ax2.set_title('沿生命周期', fontsize=9.5)
    fig.text(0.075, 0.335, wrap(
        '**两族走的是相反的形状。** crypto(15M 与小时盘)沿价位轴是**倒 U**:'
        '在值处最贵(0.95-1.34¢),两端最便宜(0.30-0.55¢)——'
        '和「彩票档点差窄」是同一件事,因为那里的 tick 就是全部。'
        'sports_prop 则**单调上升**,到 95-99¢ 档冲到 5.79¢,是在值处的两倍。'
        '沿生命周期,crypto 15M 的耗损**随临近结算下降**(0.81 → 0.50¢),'
        'crypto 小时盘反而**上升**(0.75 → 1.05¢)。同为 crypto,同为二元合约,'
        '生命周期长度一换,方向就翻。这是「跨族不合并」这条军规最直接的一张证据图。', 140),
        fontsize=8.8, color=INK, va='top', linespacing=1.5)
    footnote(fig, '每个 (市场, 格) 需 ≥5 笔成交;价位档是**成交价**不是 mid。'
                  'politics 只有两段有足够样本,econ 的 n 在每段都只有 22-123 个市场,读数噪声大。')
    rep.save(fig)


# ------------------------------------------------------------------ SF8-K
def page_sf8(rep, R):
    fig = rep.page()
    specs = {s['spec']: s for s in R['sf8']['specs']}
    header(fig, 'SF8-K · 临近结算深度会掉吗?——PM 推迟的那个问题',
           'Dubach 第 5.8 节:横截面上加了 duration / p(1−p) / volume 之后,log(距收盘) 的系数塌到 +0.008。'
           '他明确写道「单个市场自己的深度轨迹只能由逐市场时间序列识别,本文推迟」。'
           '我们有逐市场时间序列。', 'SF8-K · 回答 PM 的遗留问题')
    order = ['bivariate', 'family_fe', 'family_fe_volume', 'market_fe_full']
    labels = ['① 双变量', '② + 族固定效应', '③ + log 成交量', '④ 市场固定效应全规格']
    ys = np.arange(len(order))[::-1]
    ax = fig.add_axes([0.155, 0.44, 0.36, 0.36])
    for y, k in zip(ys, order):
        s = specs[k]
        b = s['coef']['log_tte']
        ci = s['ci']['log_tte']
        col = S1 if ci[0] > 0 else (S2 if ci[1] < 0 else MUTED)
        ax.plot(ci, [y, y], color=col, lw=2.4, solid_capstyle='round')
        ax.plot([b], [y], 'o', color=col, ms=8)
        ax.text(b, y + 0.17, '%+.3f' % b, va='bottom', ha='center', fontsize=8.5,
                color=INK2)
    pmv = R['sf8']['pm']['slopes']
    for y, k in zip(ys, ['bivariate', 'category_fe', 'category_fe_volume', 'full_v2']):
        ax.plot([pmv[k]], [y + 0.22], marker='D', ms=6, color=S2, mfc='none')
    zero_line(ax, axis='x')
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel('log(距收盘秒数) 的系数,95% 市场聚类 CI')
    ax.set_title('实心 = Kalshi(市场内);空心菱形 = PM(横截面)', fontsize=9.3)

    ax2 = fig.add_axes([0.655, 0.44, 0.29, 0.36])
    sub = [('market_fe_full', '全部'), ('market_fe_full__sports_game', 'sports game'),
           ('market_fe_full__sports_prop', 'sports prop'), ('market_fe_full__crypto_15m', 'crypto 15M')]
    sub = [(k, l) for k, l in sub if k in specs]
    ys2 = np.arange(len(sub))[::-1]
    for y, (k, l) in zip(ys2, sub):
        s = specs[k]
        ci = s['ci']['log_tte']
        col = S1 if ci[0] > 0 else MUTED
        ax2.plot(ci, [y, y], color=col, lw=2.4, solid_capstyle='round')
        ax2.plot([s['coef']['log_tte']], [y], 'o', color=col, ms=7)
    zero_line(ax2, axis='x')
    ax2.set_yticks(ys2)
    ax2.set_yticklabels([l for _, l in sub], fontsize=8.5)
    ax2.set_xlabel('同一全规格,分族')
    ax2.set_title('分族拆开:crypto 15M 的 CI 含 0(n 太小)', fontsize=9.3)

    f = specs['market_fe_full']
    rows = [['① 双变量', compact(f['n']), fmt(specs['bivariate']['coef']['log_tte'], 3),
             fmt(specs['bivariate']['se']['log_tte'], 3), fmt(specs['bivariate']['r2'], 3),
             fmt(R['sf8']['pm']['slopes']['bivariate'], 3)],
            ['② + 族 FE', compact(f['n']), fmt(specs['family_fe']['coef']['log_tte'], 3),
             fmt(specs['family_fe']['se']['log_tte'], 3), fmt(specs['family_fe']['r2'], 3),
             fmt(R['sf8']['pm']['slopes']['category_fe'], 3)],
            ['③ + log 成交量', compact(f['n']), fmt(specs['family_fe_volume']['coef']['log_tte'], 3),
             fmt(specs['family_fe_volume']['se']['log_tte'], 3),
             fmt(specs['family_fe_volume']['r2'], 3),
             fmt(R['sf8']['pm']['slopes']['category_fe_volume'], 3)],
            ['④ 市场 FE 全规格', compact(f['n']), fmt(f['coef']['log_tte'], 3),
             fmt(f['se']['log_tte'], 3), fmt(f['r2'], 3),
             fmt(R['sf8']['pm']['slopes']['full_v2'], 3)]]
    table(fig, [0.115, 0.225, 0.44, 0.16], rows,
          ['规格', 'n', 'log(tte) 系数', '聚类 SE', 'R²', 'PM 同规格'],
          widths=[0.14, 0.07, 0.11, 0.09, 0.07, 0.10], fontsize=7.6, scale=1.5)
    fig.text(0.625, 0.375, wrap(
        '**全规格的控制项也和 PM 打架。**\n'
        'log p(1−p):我们 %+.3f(SE %.3f),PM %+.2f。\n'
        'log 成交量:我们 %+.3f(SE %.3f),PM %+.2f。\n\n'
        'PM 说价格越极端深度越厚(横截面上成熟市场已走向结算);'
        '我们在**同一个市场内部**看到的是相反的:'
        '越靠近 50¢ 书越厚。两者不必然矛盾——'
        '一个是市场之间,一个是市场自己走过的路——'
        '但把 PM 的横截面系数当先验搬进任何模型都会搬反方向。\n\n'
        '**E13 压缩验收读数:tte 的增量 R² = %.4f**(全规格 %.3f,去掉 tte 后 %.3f)。'
        '排除 0 但很薄:tte 是一条**真实存在但很细**的独立通道。'
        % (f['coef']['log_p1p'], f['se']['log_p1p'], R['sf8']['pm']['controls']['log_p1p'],
           f['coef']['log_volume'], f['se']['log_volume'], R['sf8']['pm']['controls']['log_volume'],
           f['delta_r2_tte'], f['r2'], f['r2_without_tte']), 60),
        fontsize=8.2, color=INK, va='top', linespacing=1.5)
    footnote(fig, '被解释变量 = log(前十档累计深度,张),60 秒面板,%s 行 / %d 个市场(通过已知答案门者)。'
                  '①②③ 为混合 OLS(与 PM 的横截面阶梯对位),④ 为市场内去均值 + 市场聚类三明治 SE。'
                  '市场固定效应吸收了 PM 的 duration 控制项(市场存续期在市场内是常数)。'
                  '**判决:全规格 CI [%+.3f, %+.3f] 排除 0 → 深度撤退是独立通道,墓地不纯是流的问题。**'
                  '但分族拆开后 crypto 15M 的 CI 含 0(17 个市场 / 217 行,功效不足)——'
                  '**这条判决目前由体育盘扛着,不能直接外推到 crypto 15M。**'
                  % (compact(f['n']), f['groups'], f['ci']['log_tte'][0], f['ci']['log_tte'][1]))
    rep.save(fig)


# ------------------------------------------------------------------ surprises
def page_surprises(rep, R, surprises):
    for pageno, chunk in enumerate([surprises[:5], surprises[5:]]):
        if not chunk:
            continue
        fig = rep.page()
        header(fig, '意外清单 %s· 与锚点相反、与已有结论冲突、或形状不对劲的读数'
               % ('(续)' if pageno else ''),
               '强制产出。每行:读数 / 对照物 / 初步猜测 / 值不值得立独立实验。'
               '**没有一行是结论,全部是「结构描述,样本内」。**' if not pageno else None,
               '意外清单 %d/%d' % (pageno + 1, 1 + (len(surprises) > 5)))
        W, LH = 140, 0.0182
        y = 0.855
        for i, s in enumerate(chunk):
            n = pageno * 5 + i + 1
            col = {'A': CRIT, 'B': WARN, 'C': S1, 'D': MUTED}[s['kind']]
            read = wrap('读数:' + s['reading'], W)
            rest = wrap('对照:' + s['against'] + '  ▸ 猜测:' + s['guess'] +
                        '  ▸ 立独立实验?' + s['verdict'], W)
            h = 0.026 + LH * (read.count('\n') + 1) + 0.006 + LH * (rest.count('\n') + 1)
            fig.patches.append(plt.Rectangle((0.055, y - h), 0.005, h + 0.012,
                                             transform=fig.transFigure, facecolor=col,
                                             edgecolor='none', zorder=2))
            fig.text(0.072, y + 0.012, '%d. %s' % (n, s['title']), fontsize=10.0,
                     fontweight='bold', color=INK, va='top')
            fig.text(0.072, y - 0.016, read, fontsize=8.1, color=INK, va='top', linespacing=1.42)
            fig.text(0.072, y - 0.016 - LH * (read.count('\n') + 1) - 0.004, rest,
                     fontsize=8.1, color=INK2, va='top', linespacing=1.42)
            y -= h + 0.030
        rep.save(fig)


# ------------------------------------------------------------------ method
def page_method(rep, R, meta):
    fig = rep.page()
    k = R['taxonomy_kit']
    g = R['sf2']['gate']
    st = R['settlement']
    cov = R['coverage']
    prose_page(fig, '方法 · 门 · 反例 15 四件套', [
        ('数据与窗口',
         'L1 报价(~1 Hz 顶档流,实测中位间隔 1.002 s)与成交 firehose:封存日 2026-07-10..07-24,'
         '共 %s 行 (市场 × tte 段) 聚合。L2 十档:干净三日 07-12/15/17(07-13/14/16 因数据质量被排除,'
         '07-10/11 未抓 L2)。窗口右端是 07-24 而不是 07-25/26:唯一 DONE 的 catalog 快照拍摄于 '
         '2026-07-24T21:30:17Z,之后收盘的市场没有结算真值。结算索引 %s 个已终结二元市场,'
         '其中 %s 个在快照截止内。'
         % (compact(cov['l1_rows_all']), compact(st['markets']), compact(st['within_cutoff']))),
        ('已知答案门(C1 教训:grid_replay v1 无门,打出 199,482 笔假成交整轮作废)',
         'L2 是增量流,必须重建。重建**必须**先证明它重建对了。门的做法:每个市场从自己的第一条 snapshot 起'
         '重放,重放出的最优买/卖价,拿去逐点比对**该市场自己的 L1 报价时间线**——两条 L2 事件之间持有的'
         '书状态,必须复现落在该区间内的每一条 L1 报价。%s 次比对,%d 个市场,中位一致率 %.4f,'
         '**%d 个市场(%.0f%%)达到 ≥99%% 并进入 SF2-K / SF8-K**;放宽到 ≥95%% 是 %d 个,'
         '形状统计量分毫不差(L1 份额 %.4f vs %.4f,KL %.3f vs %.3f),所以门的选择没有在挑形状。\n'
         '门还替我们**证伪了一个假设**:重放顺序不能想当然。抓取会周期性重订阅,ws_seq 是**每订阅**计数的,'
         '所以同一个体育市场的行按 ws_seq 排会把两段不同时间交错在一起;而 crypto 15M 活在单一订阅里,'
         'ws_seq 才是真序、ts_utc 反而是错的。实测(九个市场对 L1 真值):crypto seq 0.94-0.98 / ts 0.02-0.31;'
         '体育 seq 0.02-0.15 / ts 0.92-0.96。因此两种顺序都重建一遍,**由门逐市场裁决**(最终 ts %d 个 / seq %d 个),'
         '而不是靠对数据流的假设。去重(丢掉相同 (ts, side, price, delta) 的增量)让两边都变差,'
         '说明那些重复是真实的簿记事件。'
         % (compact(g['comparisons']), g['markets_compared'], g['median_agreement'],
            g['markets_passed'], 100.0 * g['markets_passed'] / g['markets_compared'],
            g['markets_passed_95'], R['sf2']['shape']['l1_share_median'],
            R['sf2']['sensitivity_95']['l1_share_median'], R['sf2']['shape']['kl_median'],
            R['sf2']['sensitivity_95']['kl_median'],
            g['variant_counts'].get('ts', 0), g['variant_counts'].get('seq', 0))),
        ('反例 15 四件套(分层口径不版本化 → 口径一变历史结果全部不可复现)',
         '①分类规则版本:taxonomy.py sha256 前 16 位 %s;②catalog 快照哈希:%s;'
         '③逐 ticker 归属表:census.parquet(%d 条 series × 族)+ 各 SF 的逐市场工件;'
         '④**未识别清单:%d 条 series / %s L1 行**——按行数排头部是 KXSOLD / KXSOLE / KXBNB / KXBNBD / '
         'KXXRPD / KXHYPED,即 **taxonomy.py 的 CRYPTO_HOURLY 只写了 BTC/ETH,SOL/BNB/XRP/HYPE/DOGE 整族在册外**;'
         '被整类排除的类目:Exotics %s 行、Commodities %s、Financials %s(均不在六族定义内)。'
         % (meta['taxonomy_sha16'], meta['catalog_snapshot'], k['in_scope_series'],
            k['unidentified_series'], compact(k['unidentified_l1_rows']),
            compact(k['excluded_categories'][0]['l1_rows']),
            compact(k['excluded_categories'][1]['l1_rows']),
            compact(k['excluded_categories'][2]['l1_rows']))),
        ('这份报告不产生什么',
         '不产生交易规则;不改任何引擎参数;不碰实盘。样本内形状进入引擎的唯一通道是独立的样本外实验'
         '(反例 13:当天发现的规律当天接线 = 已经付过代价的错误)。'
         '「平均在场者」的读数不是我们的 EV(反例 14)。所有 CI 是市场聚类 bootstrap×1000,'
         '不是按笔数算的 n(反例 3:CI 虚窄 7 倍)。'),
    ], tag='方法')
    footnote(fig, '重跑:见 Repo Research/MICROSTRUCTURE_E15_20260727/README.md。'
                  'EC2 重活 nice -n 19 ionice -c3,DuckDB threads=2 / 8GB / temp 走 /dev/shm,'
                  '输出只落 scratch,不写 warehouse。')
    rep.save(fig)


def main():
    src, dst, metap = sys.argv[1], sys.argv[2], sys.argv[3]
    R = json.load(open(src))
    meta = json.load(open(metap))
    surprises = json.load(open(metap.replace('meta.json', 'surprises.json')))
    with Report(dst, 'E15 · Kalshi 微结构解剖(Dubach 2026 复现 + 探索)',
                subject='pre-registered, exploratory only, structural description in-sample',
                cjk=True) as rep:
        page_cover(rep, R, meta)
        page_sf1(rep, R)
        page_sf1_life(rep, R)
        page_sf2(rep, R)
        page_sf2_sides(rep, R)
        page_ladder(rep, R)
        page_sf5(rep, R)
        page_sf5_shape(rep, R)
        page_sf8(rep, R)
        page_surprises(rep, R, surprises)
        page_method(rep, R, meta)


if __name__ == '__main__':
    main()
