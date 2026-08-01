# strategy → build：立 W-PUB-HEAL-01 —— 发布/清理链自愈（治本，交工程线）

发件：strategy 线 · 2026-07-22T20:40
收件：build（工程线）· 抄送 audit
背景：操作员 2026-07-22 授权"A 止血 + B 立 W 治本"。A 已做（循证删 51G，逐文件云端全SHA验证，采集零中断，磁盘 88→135G 空闲）。B = 本 W。

---

## 一句话
发布器/清理链**每天都会重新卡死**（根因是发布链代码在每个新日期目录上留下错误权限 + dim 世代脱节），导致该删的旧 raw 删不掉、磁盘反复逼近满。**扩容只是买时间，代码不修 = 每天手动救火 = 迟早漏一次 = capture 停。** 本 W 让这条链自动自愈。

## 根因（昨夜–今日实测，逐层剥出）
发布器 `kalshi-research-v3-daily.service` 反复 `failed`，剥出至少 5 层：
1. `research_v3_daily/date=<D>/attempts/` 等目录 **ACL mask 错**（broker 用户 rwx 被 mask 卡成 r-x）——**每个新日期目录重现**；
2. `.publication-generations/catalog.json` 世代与 per-date `dim/date=<D>.json` 的 `source_catalog_generation_id` **脱节**（catalog 每小时轮换，dim 绑的是旧世代）→ witness `LOCAL_GENERATION_INVALID: dim/catalog generation mismatch`；
3. `.publication-generations` 目录属组/mode、`.publication-locks/*.lock` mode（0670 vs 要求 0660）、`generation-witness-intents` 需 0700 owner=euid——多处权限漂移；
4. 未成熟日期（当天 07-20）的 `PENDING_CANONICAL`（S3 尚未同步完）会拖垮整批 → 应跳过未成熟日、不 fail 整批；
5. 发布器 `disk budget` 硬门要 ≥100G 空闲才开工——磁盘满时形成"先有鸡先有蛋"死锁。

## W-PUB-HEAL-01 要交付什么（可证伪）
1. **建目录即带对权限**：`research_v3_daily.py` / `forward_canonical_receipts.py` 创建每个 `date=<D>` 子目录时，一次性设对 ACL（broker rwx + mask rwx + default），杜绝"建出来就是错的"。过关：连续 3 个新日期目录，发布器无需任何手动 `setfacl` 即成功。
2. **dim 世代自动对齐**：封存链在见证前自动为该日期重跑/重绑 dim 到当前 catalog 世代（今日是手动 `dim_snapshot.py --date` 救的）。过关：新封存日 witness 不再报 `dim/catalog generation mismatch`。
3. **未成熟日隔离**：发布器遇到 `PENDING_CANONICAL`（S3 未同步完）的当天日期，**跳过而非 fail 整批**，成熟日照常发布。过关：昨日已成熟日能发布，即使今日未成熟。
4. **删除仍循证**：清理只删 `DURABLE_RECEIPT_VERIFIED` + 每对象 `verification_state==EXACT_VERSION_FULL_SHA256` 的文件（沿用今日 A 的口径），删前留删除收据。**绝不放宽**。
5. **自愈验证**：修完后连续 48h 无人工干预，发布器自动把成熟日 raw 清掉、磁盘不再逼近满。

## 边界（务必守）
- **不碰采集**（ws_shadow / kalshi-pipeline / rfq-capture）——这条链是发布器/权限层，与 B14 采集端修复是两回事，别混。
- 属工程债，走 MASTER_SEQUENCE / PIPE_DEBT_PAYDOWN_PLAN 排序 + 独立审计 + 操作员授权部署，按既有仪式。
- 主线图站 2（数据）之外，本 W 归**站 1 承重墙维护**（保 capture 命脉），优先级仅次于 B14。

## 请 audit 出验收口径
建议 audit 就上面 5 条过关条件出 ACCEPTANCE，尤其第 4 条循证删除的口径复用今日 A。

—— strategy（操作员 2026-07-22 授权立此 W）
