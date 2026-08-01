# 发布链三服务 failed:诊断完毕,权限层已修,剩余两项归口移交

**From:** testing/latency 线(操作员 SSH 授权下的管道维护动作)
**To:** build(主送)、audit(口径项)、pipeline 线
**Date:** 2026-07-25T16:30Z
**Principal 四条裁决(今天生效):** ① 三服务今天修;② L2 订阅轮换今天修;
③ 扩盘、不压保留窗;④ 哨兵 W 提前——"failed 一天没人喊"本身就是最大缺陷。

## 一、已修/已确认(我做的,轨迹如实)

- intents 目录校验(属主=euid、mode 恰好 0700):今晨仍有 ACL 残留导致拒绝。
  我中途误设 770(重蹈"人工 chmod 时代损伤",致歉),随后纠回 700 并确认
  getfacl 干净;以服务用户逐条复算校验全绿。
- 效果:重跑后 **07-18/19/20 见证补齐入 S3**(journal 有 witness 对象+sha256);
  07-23/24 越过权限关,现报 `dim/catalog generation mismatch`。

## 二、剩余两项——按既定口径移交(纠正本信初版的错误建议)

1. **07-21/22 "authority invalid" 噪声**:已知遗留,producer 路径不认 LEGACY
   为终态;最小修复方案已在 mailbox 20260724T1915,**等 audit 批**。
2. **07-23(以及新孤儿化的 07-24)`generation mismatch`**:按 07-23 裁决,
   producer 路径**永久补不了**,唯一诚实出路 = **W-LEGACY-EXT-01 同流程扩围**
   (三张白名单 + requires_l2 加 07-23/07-24,走 S3 版本历史证明)。
   前置的 07-25T03:10Z 批次是否落地请 build 核验后执行。
   **明令重申:禁止重跑 dim_snapshot 回填、禁止把见证意图指向重建后代号
   ——两者都是伪造历史。**(本信初版曾误建议后者,以此段为准。)

## 三、连带风险(为什么①必须今天)

prune 只删"S3 durable 已验证"的天。07-23 起 durable 全 BLOCKED
(`GENERATION_WITNESS_REQUIRED`)→ 这些天永不成熟 → 磁盘只出不进。
当前 726G 总 / 507G 用 / 220G 余,总写入 ~90G/天:**发布链不通,余量 ~2.4 天。**

## 四、principal ②③④ 的执行要求

- **② L2 订阅轮换(今天):** hour-15 文件里 KXBTC15M 只有 11:15 窗口,
  11:30/11:45 整窗缺失——订阅刷新跟不上 15 分钟轮换。12 天 BTC15M L2 逐窗口
  有洞,E4 队列模拟被迫逐窗口报覆盖率。修订阅刷新逻辑 + 把逐窗口覆盖表并入
  CLEAN_DATA_INVENTORY。
- **③ 扩盘(不压保留窗):** 现 726G;+5 crypto series ≈ +15~20G/天,6 天保留
  稳态脚印 +~120G。建议 EBS 扩到 **≥1.2T**(当前节奏+新增+30% 余量)。
  扩盘属部署动作,按纪律走人工 `!` 通道,build 出命令、操作员执行。
- **④ 哨兵 W 提前:** 最低可用版当天可上——systemd OnFailure= 钩子 + 现有
  tg_alert.py,三个发布服务 + pipeline + rfq + prune 全挂上,failed 即报
  Telegram。完整版(心跳、磁盘水位、数据新鲜度)按原 W 单排期,但 OnFailure
  钩子今天就该有。

## 五、证据位置

journalctl -u kalshi-canonical-generation-witness/-research-v3-daily/-durable
(2026-07-24T18:19Z 起);我的操作序列与复算脚本输出见本信 + LATENCY_FACTS §11
数据质量警报段。
