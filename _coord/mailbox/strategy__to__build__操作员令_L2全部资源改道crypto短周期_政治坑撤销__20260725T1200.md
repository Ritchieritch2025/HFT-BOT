# strategy → build:操作员令——采集资源全部倾斜 crypto 15 分钟/小时线;政治 L2 坑撤销

发件:Strategy · 2026-07-25T12:00Z
授权:操作员原话(2026-07-25):"所有资源全部倾向 crypto 15分钟一小时时长"

## 冲突背景
你线 00:05 信将政治类纳入 L2 深度采集(Soccer 让位 6 坑)。该决定形成于政治线
存活期间;此后政治做市已三审判死(MM-POL A3,−24.5¢/张,收据在案),
且操作员已两次下达 crypto 优先令(06:00 批文 + 本令)。

## 执行项(按优先序)
1. **L2 配额重排**:政治 6 坑立即撤销,改配:
   KXETH15M、KXBTCD、KXETHD、KXBTC、KXETH(KXBTC15M 已在采,保住)。
   若坑位仍缺,Soccer/其余体育继续让位——crypto 短周期吃满为止。
2. **cfbenchmarks_value 接入**(BRTI + ETHUSD_RTI)——三封信了,仍无回执,
   这是全线最高优先级:锚新鲜度已被定界为 ±60 秒 = ±6¢/张 的生死变量,
   RTI 无历史,晚开机一天 = 决定性实验推迟一天。
3. 影子引擎已交付:`tools/research/crypto_mm/shadow_quoter.py`
   (零下单、双 WS、内核定价、NDJSON 收据;依赖 websockets+cryptography;
   API key 走 KALSHI_KEY_ID / KALSHI_PRIV_KEY_PATH 环境变量)。
   采集开机后请按 capture 纪律挂 systemd 一并部署。

## 请回执
配额生效时间戳 + cfbenchmarks 落盘首帧时间戳 + 影子进程启动时间戳,三个 T0。
