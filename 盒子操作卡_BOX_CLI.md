# 盒子操作卡 — 从 Mac 登进 EC2 跑测试

> **心法:看命令行开头,就知道你在哪台机器**
> - 开头 `ritcardo@…MacBook` = 你在 **Mac** 上 → 要先 `ssh` 才能进盒子
> - 开头 `ubuntu@ip-172-31…` = 你在**盒子里** → 命令直接跑,别再 `ssh`

---

## 常用命令(按顺序)

**① 从 Mac 登进盒子**
```bash
ssh -i ~/.ssh/kalshi-key.pem ubuntu@3.130.232.109
```
> 用你的私钥登录云主机;行开头变成 `ubuntu@ip-172-31…` 就是进去了。

**② 进盒子后,跑 preflight(认证 + 行情自检,几秒出结果)**
```bash
cd ~/hft-bot && source ~/.kalshi/env.sh && KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 KALSHI_MODE=data_collect ./build/preflight --prod-ok
```
> 只读体检:验证凭证有效、交易所可达、行情能读;看到 `PREFLIGHT PASS` 即通过,物理上不下单。

**③(可选)10 秒实时抓行情测试**
```bash
KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 KALSHI_MODE=data_collect KALSHI_WS_FIREHOSE=1 KALSHI_SHADOW_SECONDS=10 KALSHI_SHADOW_CAPTURE=work/probe/test.ndjson KALSHI_SHADOW_METRICS=work/probe/test.metrics.ndjson ./build/ws_shadow; echo "WS_RC=$?"; wc -l work/probe/test.ndjson
```
> 只收不发地抓 10 秒真行情落到隔离文件;粘完会**停约 10 秒(正常)**,看 `WS SHADOW PASS` / `WS_RC=0` / 最后数字 >0。

**④ 退回 Mac**
```bash
exit
```
> 离开盒子,回到你自己的 Mac;行开头变回 `ritcardo@…MacBook`。

---

## 懒人版:不登进去,从 Mac 一条跑完 preflight
```bash
ssh -i ~/.ssh/kalshi-key.pem ubuntu@3.130.232.109 'cd ~/hft-bot && source ~/.kalshi/env.sh && KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 KALSHI_MODE=data_collect ./build/preflight --prod-ok'
```
> 在 Mac 上一条命令跑完 preflight 并自动回到 Mac,不用手动 `exit`。

---

## 改了代码后:部署到盒子(手动三步)

> 记住:**推 GitHub ≠ 更新盒子**。`git push origin` 只存档到 GitHub,不动盒子。
> 盒子要靠下面三步才更新。而且**改采集核心才需要重启,且要挑交易所清淡时段**(重启会留数据缺口)。

**① Mac 上:先本地测过,再把代码送到盒子**
```bash
git push ec2 <你的分支名>
```

**② 盒子上:取代码 + 重编 + 重测**
```bash
ssh -i ~/.ssh/kalshi-key.pem ubuntu@3.130.232.109 'cd ~/hft-bot && git pull && make && make check'
```
> 看到测试全绿(exit 0)才继续第三步;红了就别重启,先修。

**③ 盒子上:确认绿了,重启让新代码生效**(仅在必要时,挑清淡时段)
```bash
ssh -i ~/.ssh/kalshi-key.pem ubuntu@3.130.232.109 'sudo systemctl restart kalshi-pipeline'
```

> 将来 W-A5 会建"自动部署":推到 `deploy` 分支 → 盒子自动拉取/编译/测试/重启(仅策略进程,永不自动重启采集)。现在是手动三步。

---

## 卡住了怎么办

| 情况 | 处理 |
|---|---|
| 命令没反应 / 像死机 | 按 **Ctrl + C** 取消,回到干净的 `$` 提示符 |
| 抓行情时停住 ~10 秒 | 正常,它在采集,等它自己出结果 |
| `ssh` 报 "Identity file not accessible" | 你可能已经在盒子里了(开头 `ubuntu@…`),不用再 ssh,命令直接跑 |
| 彻底乱了 | 关掉终端窗口重开,从 ① 重新登 |

---

## ⚠️ 重要提醒

- **IP `3.130.232.109` 是固定的(Elastic IP,已设)**:盒子停/起地址都不变,这张卡永久有效。
- **换网络/换地方登录**:SSH 防火墙目前只放行你家的 IP;换到别的网络(咖啡馆、公司)要先在控制台安全组里把新 IP 加进入站规则,否则登不进。
- **私钥 `~/.ssh/kalshi-key.pem` 只在你 Mac 上、只归你管**(S4):别发给任何人,包括任何聊天。
- 以上全部是**只读**操作,永不下单;真正下单需要另外的实盘闸门 + 你当场确认。
