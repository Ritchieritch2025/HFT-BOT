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

## 4. B 路径的执行顺序

前置:需要一把有 `ec2:*` 和 `s3:*` 的凭证(现有的 `researchReader` 和
`vaultWriter` 都不够 —— `vaultWriter` 是 List/Get/Put **无 Delete**,那是当初
故意设计的防误删护栏)。

```bash
# 0. 盘点,拿到真实数字(只读,先跑这个)
python3 tools/ops/cloud_cost_inventory.py

# 1. S3 生命周期 —— 不删数据,立刻砍掉 95% 的 S3 账单。先做,因为零风险
python3 tools/ops/cloud_teardown.py --stage s3            # 预览
python3 tools/ops/cloud_teardown.py --stage s3 --apply
```

**2. 核对生产盘的 raw 是否真的都在 S3**(这一步决定能不能直接删盘)。
控制台启动 `i-0fd427becf740a06b`,然后在盒子上:

```bash
# 每个 date= 目录的本地文件清单 vs S3 清单,逐个 key 比大小
cd /home/ubuntu/hft-bot
for d in work/raw/date=*; do
  D=$(basename "$d")
  echo "== $D"
  ls -l "$d" | awk '{print $9, $5}' | sort > /tmp/local.$D
  aws s3 ls "s3://kalshi-vault-ritcardo/ec2/raw/$D/" \
    | awk '{print $4, $3}' | sort > /tmp/s3.$D
  diff /tmp/local.$D /tmp/s3.$D | head -20
done
# 有缺口就补传(注意:此时没有进程在写,不存在撕裂拷贝风险)
aws s3 sync work/raw s3://kalshi-vault-ritcardo/ec2/raw
# 另外把 warehouse / live / mm 这些不在 raw 里的也传上去
aws s3 sync work/warehouse s3://kalshi-vault-ritcardo/ec2/warehouse
aws s3 sync work/live      s3://kalshi-vault-ritcardo/ec2/live
aws s3 sync work/mm        s3://kalshi-vault-ritcardo/ec2/mm
sudo shutdown -h now
```

W09 上的 111G 里 **91G 是 `cache`**(S3 研究数据的本地缓存,可重建,不用传)。
值得传的只有 `checkpoints` 9.4G + `runs` 5.1G + `c1-runs` 160M。

```bash
# 3. 终止 → 删盘 → 释放 IP(不可逆,按这个顺序)
python3 tools/ops/cloud_teardown.py --stage term --stage volume --stage eip
python3 tools/ops/cloud_teardown.py --stage term --stage volume --stage eip --apply
```

`--stage volume` 内置护栏:没有 completed 快照的盘会被 SKIP。走 B 路径
(数据已在 S3)时这个护栏会拦下所有盘,需要先 `--stage snap` 或手动确认放行。

### 控制台替代路径(不给凭证时)

1. EC2 → Elastic IPs → 选中两个 → Actions → Release,×2
2. EC2 → Instances → 选中两台(已 stopped)→ Instance state → Terminate
3. EC2 → Volumes → 状态 `available` 的两个 → Actions → Delete volume
4. S3 → `kalshi-vault-ritcardo` → Management → Create lifecycle rule →
   作用于全桶 → Transition current+noncurrent versions to Glacier Deep Archive
   after 0 days → 另加 "Permanently delete noncurrent versions after 1 day"
5. S3 → Properties → Bucket Versioning → Suspend

---

## 5. 复活时要记得的三件事

1. **先 enable `w09-idle-check.timer`** 再开 W09,否则又是 $309/月的漏水
2. Deep Archive 的数据要先 `restore-object`(12–48h)才能读,不能直接 `s3 cp`
3. `deploy/bringup_ec2.sh` 是幂等的;引擎用 `/home/ubuntu/restart_official.sh`
   (见 memory `project-paused-20260729`)
