# 操作员军规:损失预算(2026-07-27,操作员会话代录)

原话:"I need to have estimated losses — they are allowed to be extreme at black
swan events, but not intolerable."

## 规则

任何实盘配置(含每次调 clip/net/margin/开新系列)上线前,必须附**四行最坏账单**,
四行全是精确数字(二元合约最坏损失可枚举,禁止用σ/VaR近似替代):

| 行 | 定义 | 当前 A 档实值 | 封顶工具 |
|---|---|---|---|
| 1 单笔最坏 | risk_px × clip | $0.79(clip=1,无价上限) | 小数张 clip、风险价上限 |
| 2 瞬时最坏(黑天鹅) | 全部挂单同秒被扫×最坏价 | ≤ $5(MAX_OPEN_COST) | MAX_OPEN_COST |
| 3 会话最坏 | 硬闩 | $10(ECON_HALT/floor) | 预算守卫 |
| 4 线最坏 | budget 文档 max_loss;Σ各线 ≤ equity | $10 / $19.79 单线 | line_reconciler --budget |

黑天鹅允许打到第 2/3 行的顶,但顶必须是操作员批准的"可承受"数。
任何理论上无界的敞口(无 TTL 挂单、无对账仓位)= 违规,不论期望收益。

## 待操作员批准的两个现值

- 第 3 行 $10 = 账户 51%——这是历史沿革值,不是被"可承受"倒推的,请确认或改小;
- 第 1 行无价上限(79¢ 入场已发生)——建议风险价 ≤60¢ 或 clip ≤0.25 至少取一。

账单进台账,每档留痕。build 线重武装前请按此出 A 档修订账单。
