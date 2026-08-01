# 工单 — GLFT 最优库存报价接入 (arXiv:1105.3115, Guéant–Lehalle–Fernandez-Tapia 2012)

## 定位:这块补什么
当前引擎的库存控制是**固定 0.5¢/张线性退价**——拍脑袋数字。GLFT 给出同一件事的最优解:
退价幅度应随波动率、成交强度、风险厌恶动态变化。它只管**库存风险**;
逆向选择(接刀子)由 `trend_signals.py` 管。两者叠加使用,不互相替代。

## 公式(参考实现 `tools/research/crypto_mm/glft_quotes.py`)
概率空间(¢)下,库存 q 张(+为多 YES):

    d0    = (1/γ)·ln(1+γ/k)                       —— 基础半价差
    Δ     = √[ σ²γ/(2kA) · (1+γ/k)^(1+k/γ) ]      —— 每张库存的退价
    δ_bid(q) = d0 + (2q+1)/2·Δ                     —— YES 买价距 fair 的距离
    δ_ask(q) = d0 − (2q−1)/2·Δ                     —— YES 卖价距 fair 的距离
    spread   = 2·d0 + Δ                            —— 与 q 无关

直觉:q>0 时买价退、卖价进,两边各按 Δ 平移;σ 升高或流动性(kA)变差,Δ 自动变大。

## 参数与单位(全部要从自己数据校准,零个拍脑袋)
| 参数 | 含义 | 单位 | 校准方法 |
|---|---|---|---|
| σ_c | fair 价格波动 | ¢/√s | `trend_signals.fair_price_sensitivity_c(μ,K,V) × σ_RTI`,逐 tick 现算 |
| A | 贴 fair 报价时的成交强度 | fills/s | `glft_quotes.fit_intensity`:用 QUOTE_EVAL(报价距 fair 的距离×驻留时间)+ SHADOW_FILL_SIM/FILL 拟合 λ(δ)=A·e^(−kδ) |
| k | 成交强度衰减 | 1/¢ | 同上,一次回归同时出 A、k,报告 R² |
| γ | 风险厌恶 | 1/¢ | 不直接拍:用 `gamma_from_target_skew` 反推——"满仓 Q 张时报价应已退 X¢" ⇒ 目标 Δ=X/Q |

校准数据:18.5h 影子日志足够拟合首版 A、k。**按 tte 区间分层拟合**(≥600s / 300–600s),
不同生命周期成交强度差异大。σ_c 逐 tick 现算,A、k 每日离线重校准即可。

## 接线方式
1. 在 QUOTE_EVAL 计算 edge 之后、报价生成之前调 `glft_offsets(net, σ_c, γ, k, A)`。
2. 最终报价距离 = **max(GLFT 距离, trend_signals.required_edge_c, 2.5¢ 底线)**——
   三层各管一事:库存风险 / 逆向选择 / 手续费+利润底线。
3. δ_ask(q) 在 |q| 大时可为负(模型催你平仓可以穿 fair)——落地时 floor 在
   风险削减路径允许的价格上,不许因此变成主动亏钱挂单。
4. 影子模式先跑:并行记录 GLFT 报价 vs 现行 0.5¢ 线性报价,比较两者的
   模拟成交 markout 与库存路径(|net| 时间积分、峰值)。

## 验收标准(离线,动引擎前)
- fit_intensity 的 R² ≥ 0.6 且 k>0;不达标先查 δ 定义与数据,不许硬上。
- 影子回放对比:GLFT 版相对固定 skew 版,库存峰值不升高的前提下,
  pair 周期收益或模拟 PnL 不劣化;给数字。
- 极端区(<20¢/>80¢)σ_c→0 会使 Δ→0(模型说"不用退价"),但那里的真实风险
  是跳跃——极端区仍由 trend_signals 的 jump/zone 门主导,GLFT 结果仅供参考。

## 模型边界(agent 必须在白皮书里如实标注)
- 假设布朗运动 + 无信息流:不覆盖跳跃与逆向选择。[代码事实:由 trend_signals 补]
- 渐近解(T→∞):tte 数分钟以上适用;tte<120s 本就不报价,不受影响。
- 假设单位下单量:clip 仍由 `dynamic_clip` 决定。
