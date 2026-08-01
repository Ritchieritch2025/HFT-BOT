# crypto15m 线隔离审计（操作员侧,2026-07-27T01:45Z）

背景:操作员要求确认 crypto 15 分钟链条独立成线,扩张不乱。审计结果:**链条主体已隔离,发现两个扩张前必须处置的缺口。**

## 已确认隔离(六环)

| 环节 | 载体 | 隔离状态 |
|---|---|---|
| 结算锚采集 | kalshi-cfbenchmarks.service → `work/live/cfbenchmarks/date=*` | 独立服务+独立目录,07-27 在写 |
| 永续对照 | `~/research_fast_anchor/perp_*.ndjson` | 独立 |
| 代码 | `tools/research/crypto_mm/`(engine/pricing/trend/replay 全在内,无外部工程 import) | 自包含 |
| 影子回执 | kalshi-crypto-shadow-quoter → `work/live/crypto_mm_shadow/` | 独立服务+独立目录 |
| 回放判决 | `~/replay_grid_v2_20260726/out*` | 独立 |
| 市场数据 | 共享 firehose/L2(只读公共设施,l2_targets 内 crypto 22 条) | 共享但只读,合规 |

## 缺口一:同线双引擎(需处置裁决)

`mm-v0-shadow.service`(`tools/mm_v0.py`,SERIES=KXBTC15M,1张,影子)与新 mm_engine 链**同时在跑、瞄准同一系列**。影子阶段无害;但两者都有升实盘路径,若无明文"谁是正编、谁是原型",扩张时会出现同账户同系列双引擎互相成交/互相挤兑。
**建议**:round4/配对引擎收编为唯一正编;mm_v0 标记为 DEPRECATED_PROTOTYPE 或明确其存续理由,写入 BOARD。

## 缺口二:实盘归因缺线别前缀(任何第二条线上实盘前必修)

- `mm_engine.py:882` `client_order_id = uuid4()` 裸随机,无线别标识;
- `mm_budget_adapter` 以整账户 balance 为额度基数,无分线额度;
- 历史教训:372 笔真实成交至今无主(audit G12)。
**建议三闸(扩张前置条件)**:
1. client_order_id 加线别前缀(如 `C15-`),每线自记 client_order_id→order_id 映射;
2. portfolio 对账器按 order_id 归线,**孤儿成交=事故**,报警而非忽略;
3. mm_budget 分线额度包,总账户余额只做上限,不做任何单线的默认额度。

## 附带卫生项

`work/live/l2_targets.csv.tmp.*` 残留 ~70 个临时文件(原子改名未清理),建议 l2 目标生成器加 tmp 清扫。

—— 操作员会话(Claude)代录;本信为审计记录,不含任何已执行的生产变更。

## 执行记录(2026-07-27T02:1xZ 追加)

操作员指示"全部修复",已执行:

| 项 | 状态 | 载体 |
|---|---|---|
| 闸1 线别前缀 | ✅ 已落地(本地仓库) | `mm_engine.py`:`LINE_TAG`(env `MM_LINE_TAG`,默认`C15`),两处实盘 client_order_id 改 `f"{LINE_TAG}-{uuid4()}"`;107 守卫测试全绿 |
| 闸2 孤儿对账器 | ✅ 已落地(本地仓库) | 新工具 `tools/research/crypto_mm/line_reconciler.py`:按 order_id→线别归属,孤儿=exit 1;冒烟通过 |
| 闸3 分线额度检查 | ✅ 已落地(同上) | reconciler `--budget/--equity-usd`:Σmax_loss>equity 判 FAIL |
| mm_v0 退役 | ⚠ 待操作员 | `sudo systemctl disable --now mm-v0-shadow.service`(会话权限拦截,需人工 `!`);数据目录 `work/mm/v0/` 保留 |
| l2_targets tmp 泄漏 | ✅ 已打补丁(EC2,ast 校验过;备份 `.bak-20260727`) | `write_atomic` 开头清扫 >600s 的 `.tmp.*` 兄弟文件;现存 ~70 个残留将在下一次整点生成时自动清除 |

注:mm_engine/line_reconciler 改动在本地仓库 `w-pnl-spine-v1` 工作树(与 build 未提交改动同树),随下次引擎部署上 EC2。
