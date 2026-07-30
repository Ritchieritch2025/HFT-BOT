# 云成本归零手册(项目暂停期)

状态:2026-07-30 起草。项目 07-29 暂停,此文档是把云账单从「~$100+/月 + 一条
$300/月的漏水」压到 **~$1/月** 的执行路径。

配套工具:
- `tools/ops/cloud_cost_inventory.py` — 只读盘点,算出每个计费面的月成本
- `tools/ops/cloud_teardown.py` — 分级拆除,默认 dry-run,必须 `--apply` 才动手

---

## 1. 已经做掉的事(2026-07-30)

**W09 研究机 `i-0e53d134dceffe166` (r8g.2xlarge) 空转了 6 天 16 小时。**

07-29 的停机清单只写了「EC2 实例 shutdown」,指的是生产机
`i-0fd427becf740a06b`。W09 是**第二台机器**,它的 1800 秒空闲自动关机守卫
`w09-idle-check.timer` 处于 `disabled / inactive` —— 守卫从来没被 enable,所以
「跑完自动关」这个假设一直不成立。机器 load average 0.00,除了操作系统自己的
housekeeping 什么都没跑。

- 烧掉的:6.7 天 × 24h × $0.42336/h ≈ **$68**
- 已执行:`sudo shutdown -h`,2026-07-30T16:36Z 下电(可逆,盘上数据全保留)
- 止住的:**$0.42/h = $10.2/天 = $309/月**

复活时如果要再开 W09,**先 `systemctl enable --now w09-idle-check.timer`**,
否则同样的漏水会再来一次。

---

## 2. 停机后仍在计费的面

| # | 资源 | 规格 | $/月 | 依据 |
|---|---|---|---|---|
| 1 | 生产机 EBS 根盘 | 726 GB gp3 | **58.08** | 07-25 实测 726G;$0.08/GB-月 |
| 2 | W09 EBS 根盘 | 300 GB gp3 | **24.00** | `deploy/w09/cost-contract.json`;实测 290G/111G 已用 |
| 3 | 弹性 IP × 2 | 3.130.232.109 / 18.226.151.192 | **7.30** | 未关联的公网 IPv4 $0.005/h |
| 4 | S3 `kalshi-vault-ritcardo` | STANDARD,**开着版本控制** | **? (见下)** | 只量到 `research/` = 80.09 GB / $1.84 |
| 5 | 两台实例本身(已停机) | | 0 | 停机不计算力 |
| | **合计(不含 S3 未知部分)** | | **≈ $89/月** | |

### S3 是最大的未知数

`deploy/ec2_s3_sync.sh` 每小时把**整个 `work/raw`**(firehose + l2 + rfq,只排除
当前正在写的那一个小时)同步到 `s3://kalshi-vault-ritcardo/ec2/raw/`。生产盘上
07-20 之后稳定 ~98–120 G/天。如果这个 timer 一直健康,桶里现在很可能有
**几百 GB 到 1.5 TB**,而且**桶开着版本控制、全部是 STANDARD**:

- 1 TB STANDARD = **$23/月**;若含同量的旧版本则翻倍
- 同样 1 TB 转 DEEP_ARCHIVE = **$1.01/月**

本机的 `researchReader` 密钥只能读 `research/` 前缀,`ec2/`、`mac-vault/` 全部
AccessDenied,`ListBuckets` 也没有。**所以第 4 行只能等更宽的凭证才能量出来,
但它同时也是性价比最高的一刀** —— 加一条生命周期规则就砍掉 95%,不删任何数据。

---

## 3. 目标态与四条路径

| 路径 | 做什么 | 结果 $/月 | 数据 | 恢复代价 |
|---|---|---|---|---|
| **A 保守** | 只释放 2 个 EIP + S3 转 Deep Archive | ~$83 | 全留,盘还在 | 开机即用 |
| **B 推荐** | 先核对 raw 已在 S3 → 补传缺口 → 终止两台实例 + 删两个盘 + 释放 EIP + S3 全转 Deep Archive | **~$1** | 全留(在 Glacier Deep Archive) | 取回 12–48h,~$0.02/GB;机器要重建 |
| **C 折中** | 给两个盘打快照 → 删盘 → 终止实例 + 释放 EIP + S3 转 Deep Archive | ~$35 | 全留(EBS 快照) | 快照恢复成盘,小时级 |
| **D 清零** | 全删,包括 S3 | **$0** | **全丢** | 不可恢复 |

**推荐 B。** 理由:

- 代码、报告、PnL 分钟序列、策略核这些真正的战果**已经在 git 仓库里**
  (`core2-lab@daa2550`、`w-pnl-spine-v1`),不依赖云端任何东西
- 云上那 726G 的价值是**原始行情磁带**——不可再生(Kalshi 不回填历史 L2/RFQ),
  但也不需要热访问。Deep Archive 每月 $1 就能把它整条留住,这是明显划算的
- 两台机器都是**可重建**的:`deploy/bringup_ec2.sh` 是幂等的,
  `deploy/w09/` 有完整的机器契约。留着 $82/月的盘只为省一次 bring-up,不值
- C 比 B 贵 35 倍,只换来「恢复快几十小时」——项目已暂停,没有这个时间压力

**B 的一次性成本:** 生产机开机跑核对+补传,r8g.2xlarge ~4 小时 ≈ **$1.7**。
同区 EC2→S3 传输免费。

**B 的代价必须说清楚:** Deep Archive 有 **180 天最低计费**(提前删按 180 天算),
取回要 **12–48 小时**,取回费 ~$0.02/GB(整条 1 TB 取回一次 ≈ $20)。项目暂停期
这些都可接受,但如果打算三个月内复活并需要热数据,改选 C。

---

## 4. B 路径执行单(操作员控制台,2026-07-30 裁决)

裁决:走 B,操作员在控制台亲自点。下面按顺序,**每一步做完再做下一步**。
Claude 只能碰盒子内部(SSH),碰不到 AWS API —— 所以第 2 步由 Claude 跑,
其余全在控制台。

### 第 1 步 · 开机生产盒(控制台)

EC2 → Instances → `i-0fd427becf740a06b` → Instance state → **Start**。
起来后把新的公网 IP 告诉 Claude(EIP 3.130.232.109 如果还关联着就还是它)。

> 为什么要开机:整台机器的 726G 原始磁带能不能安全删,取决于它是不是真的
> 都躺在 S3 里。`kalshi-s3-sync-hourly.timer` 在 07-27 前后有过健康问题
> (durable 回执 timer 从 07-22 起就是 dead),不能假设同步是完整的。
> **没核对就删盘 = 永久丢磁带。**

### 第 2 步 · 核对 + 补传(Claude 通过 SSH 执行)

Claude 会做三件事,做完给出一份「本地 vs S3 逐日对账表」再请示:

1. 逐个 `work/raw/date=*` 目录比对本地文件名+字节数与
   `s3://kalshi-vault-ritcardo/ec2/raw/` 的清单
2. 有缺口就 `aws s3 sync` 补传(此刻无进程在写,不存在 W-A3 撕裂拷贝风险)
3. 把 `work/warehouse`、`work/live`、`work/mm` 这些不在 `raw` 下的也传上去

盒子上的 `~/.kalshi/env.sh` 里是 `vaultWriter` 凭证(List/Get/**Put**,无
Delete)—— 补传够用,而且它天然删不掉任何东西,这一步没有误删风险。

对完账 Claude 会 `sudo shutdown -h now`,然后回报「可以删盘 / 不可以删盘」。

### 第 3 步 · S3 生命周期(控制台,零风险,可以和第 2 步并行)

S3 → `kalshi-vault-ritcardo` → Management → **Create lifecycle rule**

- Rule name: `paused-project-deep-archive`,作用范围选 **整个桶**
- 勾 *Move current versions of objects between storage classes* →
  **Glacier Deep Archive**,Days after object creation = **0**
- 勾 *Move noncurrent versions* → 同样 Deep Archive,Days = 0
- 勾 *Permanently delete noncurrent versions*,Days = **1**
- 勾 *Delete expired object delete markers* 和 *Abort incomplete multipart
  uploads after 1 day*

然后 Properties → Bucket Versioning → **Suspend**。

> 转换是异步的,几小时到一天才全部落到 Deep Archive,账单跟着降。
> 转换请求费 $0.05/1000 对象 —— 这个桶是 GB 级大文件,总共几块钱。

### 第 4 步 · 终止实例(控制台,**不可逆**)

**等 Claude 在第 2 步回报「可以删盘」之后再做。**

EC2 → Instances → 选中 `i-0fd427becf740a06b` 和 `i-0e53d134dceffe166`
(两台都应是 stopped)→ Instance state → **Terminate**。

### 第 5 步 · 删盘(控制台,**不可逆**)

生产机的根盘当初是 `DeleteOnTermination = No` 建的
(`PLAN_AWS_MIGRATION.md:230`),**终止实例不会把它删掉**,它会变成
`available` 状态继续按 $58/月计费。这一步不能省。

EC2 → Volumes → 筛 State = **available** → 选中 726 GB 和 300 GB 那两个 →
Actions → **Delete volume**。删之前确认 `available` 列表里没有别的东西。

### 第 6 步 · 释放弹性 IP(控制台)

终止实例只会解除关联,**不会释放** EIP,闲置的 EIP 照样 $3.65/月。

EC2 → Elastic IPs → 两个都选(3.130.232.109 / 18.226.151.192)→
Actions → **Release Elastic IP addresses**。

### 第 7 步 · 收尾核对

Billing → Cost Explorer,次日看一眼日成本是不是掉到 $0.0x。
或者 EC2 控制台确认:Instances 全 terminated、Volumes 空、Elastic IPs 空。

---

### W09 盒子上有什么值得留的

111G 里 **91G 是 `/srv/w09-research/cache`** —— 那是 S3 研究数据的本地缓存,
可重建,**不用传**。真正值得留的只有 `checkpoints` 9.4G + `runs` 5.1G +
`c1-runs` 160M ≈ 15G。W09 已经在 07-30T16:36Z 下电,如果要保这 15G,需要在
第 4 步之前重新开机让 Claude 传一次(多花约 $0.5 的计算费)。

### 如果改主意想让 Claude 代劳

`tools/ops/cloud_teardown.py` 已经把第 3~6 步全实现了,默认 dry-run:

```bash
python3 tools/ops/cloud_teardown.py --stage s3 --stage term --stage volume --stage eip
python3 tools/ops/cloud_teardown.py --stage s3 --stage term --stage volume --stage eip --apply
```

需要一把带 `ec2:*` + `s3:*` 的临时 IAM key 放进 `~/.aws/credentials`。
`--stage volume` 有护栏:没有 completed 快照的盘会被 SKIP,走 B 路径时需要
显式放行。

---

## 5. 复活时要记得的三件事

1. **先 enable `w09-idle-check.timer`** 再开 W09,否则又是 $309/月的漏水
2. Deep Archive 的数据要先 `restore-object`(12–48h)才能读,不能直接 `s3 cp`
3. `deploy/bringup_ec2.sh` 是幂等的;引擎用 `/home/ubuntu/restart_official.sh`
   (见 memory `project-paused-20260729`)
