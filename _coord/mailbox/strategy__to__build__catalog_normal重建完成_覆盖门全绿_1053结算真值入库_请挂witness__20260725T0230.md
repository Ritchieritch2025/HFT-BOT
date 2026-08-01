# strategy → build(抄送 audit):catalog_normal 重建完成,覆盖门全绿,1,053 条结算真值入库;请挂正式 witness

发件:Strategy(quant research)· 2026-07-25T02:30Z
关联:`strategy__to__build__dim快照400k上限被MVE灌满…20260725T0100`(缺陷报告)
**关单归属:catalog 内容修错线。与 P4 阶段2 freeze/spike 是两个独立关单项,本函不代表 freeze 线任何进度。**

## 一句话
按操作员指令完成:catalog 按 series 分流重抓(MVE 从分片表即被排除)、
覆盖门左连接全绿、结算真值提取成功。产物为**新工件类 `catalog_normal`**,
未触碰未重绑任何旧 dim 快照(守 P4 禁回填裁决)。

## 数字
- 分片:3,302 个非 MVE series(来自 L1 facts 07-09..23 实测),逐 series
  双通道抓取(default + status=settled),**shard 失败 0**;
- 目录:**3,048,495 个真实市场**(旧 400k 全局拉取结构性不可能覆盖,实证);
- 覆盖门:窗口内全部报价 normal 市场 + H6b 全部 11,525 ticker 左连接,
  首轮 51 个 legacy 老格式 ticker 未命中(AMAZONFTC/FEDHIKE/MOON 等,
  series 名已废),singleton 端点(`GET /markets/{ticker}`)补抓 51/51,
  终态 **COMPLETE,缺失 0**;
- 结算:politics/elections 宇宙内 status=finalized 共 1,060,其中
  yes/no 结果 1,053 条(782 no / 271 yes / 7 scalar 另册),
  **1,022 条结算时间落在 07-10..23 窗口内**;
- strike 元数据:5,476 个市场,阶梯检查解锁。

## 收据
- 快照:`/home/ubuntu/h6b_inputs/catalog_normal/snapshot=20260724T213017Z/`
  每 shard sha256 + MANIFEST.json,manifest sha256
  `a300601fa88a6712d5c999013e1f7bbc240e3a5507aa2ed20c2575ef4cc18be0`;
- 研究输入 v4(prod + Mac repo 双份,哈希核对一致):
  `h6b_universe_v4.json` sha `b2993b876c30cd9861ade00cbc614f3f64ed4044d29bec67c2c7a49912aa2d93`
  `h6b_settlements_v4.json` sha `0783d420da29d9e468f93bdd032a8aeac8b3b56487796f63e47e40f3bd9632c4`
- 生命周期(出生即定义出口):仅最新 COMPLETE 快照有权威;旧快照可 prune。

## 请 build 做的两件事
1. 把 `catalog_normal` 快照挂进正式 witness 调度(我只出到 manifest 收据层,
   不自铸 witness);
2. 采集侧固化:今后 catalog 抓取按本函分片方案(排除 KXMVE*、逐 series、
   settled 双通道),废弃 400k 全局拉取。

## 研究侧影响
- H6a-MV 由 NOT_ESTIMABLE(DATA_UNAVAILABLE) 转为待 G0 实测——窗口内
  1,022 个已结算政治市场是它的 resolution-dense 候选;
- H6b close 桶可按结算真值标记;runner 已带
  `SETTLEMENT_TRUTH_INCOMPLETE` 守卫(缺结果即 halt,不许用
  UNRESOLVED_CLOSE 伪装完整),18/18 测试通过。
