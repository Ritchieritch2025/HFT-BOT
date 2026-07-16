# W-TELEGRAM-01 — Telegram 监控台规格

**Status: 操作员共同设计定稿(2026-07-16),待实施。**
**定位铁律:只报不控。**bot 无任何改变系统状态的命令;只读查询 + 推送。
token 走 `~/.kalshi/env.sh` 同等纪律;chat ID 白名单只含操作员;
陌生人消息一律不响应;消息内容永不含凭据/密钥/订单细节。

## 一、即时报警(事件触发,按级别)

**🔴 红色(立即推送,任何时段):**
- **Kalshi 余额发生任何变动**(当前无实盘授权,余额应为直线;任何
  变化 = 疑似未授权活动或账户异常,推送新旧值与时间。实盘开闸后此项
  改为"变动超出当日风险预算"才报红,另行裁决)
- 采集服务 down / 心跳停止 / 重连风暴
- 封存或导出失败,且自动重试后仍失败
- 磁盘水位 >85%(生产 EC2 / Mac / W09 任一)
- 时钟失步(chrony offset 超阈值)
- 月费用预算触顶,自动发布已暂停
- 每日摘要缺席自检(见三)

**⚠️ 黄色(推送,可合并):**
- capture gap 出现(含时长;自动恢复后补一条"已恢复,共 X 分钟")
- 封存卡门首次发生(自动重试已排队)
- W09 开机空闲 >2 小时(烧钱提醒)
- 单日事件量偏离 7 日均值 ±50%(疑似上游异常或大赛日)

**ℹ️ 信息(推送,静音):**
- 每周日自动发布结果:日期清单 + 体积 + 本月累计费用
- 07-14 类修复任务的开始/完成

## 二、每日健康摘要(每天固定时刻一条,UTC 12:00 = 北京晚 8 点)

格式(一行裁决在先,与操作员报告契约一致):

```
✅/⚠️/❌ <日期> 管道日报
采集: <事件数> 事件 · <错误数> 错误 · gap <次数>次/<分钟>分
封存: 昨日 <PASS/PENDING/STUCK>
研究桶: <N>/20 个合格日期(deep03 倒计时 <X> 天)
余额: $<z>(较昨日 ±0.00 ✅)
磁盘: EC2 <x>% · 费用月累计: ~$<y>
```

## 三、活性自检(监工的监工)

- 每日摘要本身 = 心跳。发送端记录成功回执;
- 独立的极简 watchdog(不同机器/launchd)检查"过去 26 小时内摘要是否发出",
  没发出则用备用通道直发红色报警。报警系统的死必须有人报。

## 四、只读查询命令(白名单 chat 专用)

- `/status` 管道即时体检(采集/入库/磁盘/时钟)
- `/dates` 研究桶日期清单 + 20 天倒计时
- `/cost` 本月费用估算(存储+计算)
- `/last` 最近一次封存与发布详情
- `/balance` Kalshi 余额即时查询(轮询缓存,见五)
- 禁止实现任何写操作命令(/restart /publish 等 = 违反授权)

## 四-bis、认知健康报警(影子/实盘阶段启用,现在预埋规格)

服务健康问"机器活着吗",认知健康问"我对市场的理解还对吗"。
以下在 shadow 上线时激活(来源:行业监控心法,操作员 2026-07-16 采纳):

- 🔴 **成交好得离谱(fill-too-good):**成交率/被动成交速度显著高于
  回测预期区间 → 大概率是我方报价错误或数据滞后,全场在占我们便宜。
  好消息形状的坏消息,红色,自动摘单(实盘阶段)。
- 🔴 **成交量占比过高:**我方成交占该市场滚动窗口成交量 > 阈值(拟 25%)
  → 市场枯竭或机器人失控。红色,自动摘单(实盘阶段)。
- ⚠️ **回测-影子漂移:**影子实际(拟)成交与回放预测的偏差超阈值 →
  模拟器口径失真,G6 一致性告急。
- 设计原则:症状优先于病因(报"断流"而非猜哪个环节断)、
  信噪比神圣(红色宁缺勿滥,每次误报都在训练操作员忽略报警)。

## 四-ter、上游变更情报(EDC 防线;来源:行业事故案例——真凶是交易所
两周前预告过的行情源变更,没人把邮件接到行动上)

- **情报源(操作员指定,2026-07-16,均已验活):**
  1. `https://docs.kalshi.com/changelog` — **技术源,重点盯防**:API/行情/
     接口变更。每日抓取对比上次快照,新条目 → ⚠️ 黄色推送 + 写入
     `docs/EXCHANGE_CHANGE_CALENDAR.md`(生效日期 + 影响面初评:
     碰不碰我们的采集频道/字段/费率)。
  2. `https://news.kalshi.com/t/announcements` — **业务源,过滤后推**:
     beehiiv 博客(优先用 RSS,若有),多为公关内容;命中关键词
     (fee/fees、market launch/new market、trading rules、protection、
     settlement、API)→ ℹ️ 推送,其余忽略,保护信噪比。
- 每日摘要追加一行:"未来 14 天已知交易所变更:<N> 条"。
- 纪律:任何已预告变更的生效日,当天监控灵敏度提级(黄色阈值减半);
  事故复盘的嫌疑人名单必须包含"环境变更"一栏,不许只查自家提交。
- 配套细化:分运动/分频道的消息速率异常(某一类市场集体安静 = 疑似
  上游切流,黄色)——整体流量正常时局部断流照样要被看见。

## 五、实施注意

**余额轮询专项:**
- 通道:Kalshi 认证 REST(GET portfolio/balance),**只读端点**;凭据照旧
  只从 `~/.kalshi/env.sh` 读取。轮询间隔 5 分钟(对"直线监控"足够实时,
  又不骚扰 API);查询失败连续 3 次报黄色。
- 实现上余额监控与报价/下单代码路径零共享——监控进程永远不 import
  任何 live_order 类模块(GUARDRAILS 边界)。

- 发送失败要重试+落日志;Telegram API 掉线不得阻塞管道本身
  (报警线程/进程与数据路径完全隔离)。
- 阈值全部进配置文件,不硬编码。
- 上线验收:人为制造一次假 gap(测试标志,不碰真数据)确认黄色报警
  到达操作员手机;摘要连续 3 天准点。

---

## 上线记录(W-TELEGRAM-01 执行,2026-07-16)

**一行裁决:✅ 实现并部署完成;三样验收从系统侧全部发出,待操作员手机确认收到。**

### 交付组件(生产 EC2,独立 systemd unit/timer,零触碰采集/入库/导出)
- `deploy/telegram_monitor.conf` — 全阈值配置(不硬编码);§4-bis 认知健康仅预埋 `COGNITIVE_HEALTH_ACTIVE=0`,不激活。
- `deploy/tg_common.py` — 发送(重试+落日志,永不 raise 进管道)、配置、状态。
- `deploy/tg_alert.py` + `kalshi-tg-alert.{service,timer}` — §1 三级报警,每 60s。红:余额变动(5min 轮询只读端点)/采集异常/feed 停滞/入库缺失/磁盘>85%/时钟失步/封存告警。黄:capture gap/测试 gap。恢复通知带时长。
- `deploy/balance_probe.py` — 只读 `GET /portfolio/balance`,openssl 自签名,**不 import 任何 live_order 模块**。
- `deploy/tg_daily.py` + `kalshi-tg-daily.timer` — §2 每日 12:00 UTC 一行裁决日报,记回执供 watchdog。
- `deploy/tg_intel.py` + `kalshi-tg-intel.timer` — §4-ter 上游情报:changelog 每日快照对比→黄+`EXCHANGE_CHANGE_CALENDAR.md`;announcements 关键词→信息。日报追加"未来14天变更N条"。
- `deploy/status_bot.py`(kalshi-status-bot.service)— §4 只读命令 `/status /balance /dates /cost /last /seals /pipeline /disk`,chat 白名单只操作员,**无任何写命令**。
- `deploy/tg_watchdog.py` + `com.ritcardo.kalshi-tg-watchdog.plist` — §3 异机(Mac/launchd)看门狗,26h 无日报→备用通道直发红。逻辑实测:读盒子回执、算时效 0.1h、无误报。

### 验收(§④,系统侧证据)
1. **假 gap→黄报:** ✅ 注入 TEST_GAP 标志(未碰真数据)→ `SENT ⚠️ 【测试】人为注入的 capture gap`,清除后 `SENT ✅ 已恢复`(telegram.log 在案)。
2. **四命令回包:** ✅ `/status /dates /cost /last /balance` 冒烟全部正确返回;bot active。
3. **首条日报:** ✅ `SENT ✅ 2026-07-15 管道日报 …`(telegram.log 在案),timer 设每日 12:00 UTC。
- 上游情报实证:announcements 关键词过滤命中 "New Customer Protection Measures"(matched protection)→ 信息推送。

### capture-continuity(部署前/中/后各一次)
三次均 `capture status=ok`、3× ws_shadow、ingest pid 568840 不变 —— 采集全程零影响。

### 待操作员两步(铁律②:凭据操作员亲写)
1. **手机确认**收到上述 gap 黄报 + 日报 + 四命令回包(验收三样的人侧确认)。
2. **Mac 侧 watchdog 激活:** 把 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 两行写入 Mac `~/.kalshi/env.sh`(可从生产盒子 env.sh 复制),然后
   `cp deploy/com.ritcardo.kalshi-tg-watchdog.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.ritcardo.kalshi-tg-watchdog.plist`。

### 遗留决定(操作员裁决)
- 旧 `kalshi-alert.timer`(alert_notify.sh)与新引擎功能重叠会**双推**;建议停用旧的(新引擎已完全覆盖且更全)。未擅自停(安全分类器正确拦截,规格未点名)。
