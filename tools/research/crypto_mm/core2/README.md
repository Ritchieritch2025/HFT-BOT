# core2-lab — 新报价核隔离实验区

**分支**: `core2-lab`(git worktree,与主线 `w-pnl-spine-v1` 物理隔离)
**规矩**: 本目录代码不进实盘引擎路径;实盘 release 永不从本分支部署。
合并回主线需要:回放对照数据 + 操作员批准(成年计划 Stage 2→3)。

## 规格(操作员 2026-07-28 口述)

```
盘口状态
  ├─ best bid / best ask
  ├─ 两侧深度与失衡
  ├─ 近期成交流向
  ├─ 锚流和波动率
  ├─ 当前库存
  └─ 剩余时间
          ↓
    连续计算两侧尺寸
          ↓
     bid 挂多少 / ask 挂多少
```

- 不预测下一秒涨跌;盘口是锚,模型只管尺寸和撤退
- 盘口均衡 → 双边提供流动性
- 某侧成交流变毒 → 危险侧按量递减,必要时归零
- 库存偏向哪边 → 少挂继续增加那边的量
- 越接近结算 → 整体尺寸和持仓容忍度越小
- 只有 best quote 扣除毒性、延迟、费用后仍有净空间,才跟价或改善一档

```
bid_size = f(spread, depth, flow, sigma, inventory, tte, latency)
ask_size = f(spread, depth, flow, sigma, inventory, tte, latency)
```

## 计划

1. `quote_core_v2.py` — 纯函数,无 I/O,输入状态向量 → (bid_px, bid_size, ask_px, ask_size)
2. `test_quote_core_v2.py` — 每条规格一条黄金用例
3. 回放驱动:用已录制的 L2+成交磁带喂状态向量,量 would-fill / would-pair / 被砸率,与现役对照
4. 数据够硬 → 申请 Stage 2 影子 A/B
