# Kalshi API 官方文档验证机制（Claude instructions）

在核对、引用或修改任何 Kalshi API 相关代码/文档前，必须遵守以下验证规则。
目的：防止用过期快照、缺失索引或训练数据记忆得出错误结论。

## 信源与权威级别（高 → 低）

1. **Prod 环境只读实测**（`external-api.kalshi.com`；demo 环境已下线、不再支持，
   `external-api.demo.kalshi.co` / `demo-api.kalshi.co` 不可用）—— 最终仲裁。
   文档之间冲突、或"是否存在/是否被接受"类问题，一律以对 prod 的**只读**请求
   返回定案；注意 prod 会下真实订单，任何写/下单类验证都是真金白银，谨慎。
2. **Changelog**（`docs.kalshi.com/changelog/index.md`）—— 行为变更 + 生效
   日期的权威来源（例：`/account/limits` 嵌套 schema 自 2026-04-30 生效）。
3. **端点页面内嵌 spec**（`docs.kalshi.com/api-reference/**/*.md`，页内
   ```yaml 块）—— 每页快照各自带 `info.version`，**必须先读版本号**；
   不同页面版本参差（实测同站并存 v3.13–v3.23），冲突时取最高版本。
4. **人写说明页**（rate_limits、getting_started、websockets/*.md）——
   语义解释可靠，字段级细节以 spec 为准。
5. **llms.txt 索引** —— 只用于发现页面，**不可用于否定**（索引缺项是常态）。

## 硬性规则

1. **否定性结论必须有直接证据。**"端点 X 不存在 / 字段 Y 无出处"这类结论,
   禁止仅凭索引缺失或某页快照未提及得出。必须:按 URL 模式
   `docs.kalshi.com/api-reference/{tag}/{operation-slug}.md` 直接探测页面 +
   changelog 全文搜索;仍无结果的,标注"未在文档中找到,待 prod 只读实测",
   而不是断言不存在。
2. **每条 spec 引用必须带版本号和核对日期。** 引用格式:
   `<事实> (openapi v3.23.0, verified 2026-07-05)`。页面内嵌 spec 是
   会漂移的快照(本仓库 F12 教训),同一事实在低版本快照里的相反表述
   不构成反驳。
3. **文档与代码冲突时,先验证再"纠正"。** 本仓库代码对照的往往是比页面
   快照更新的 spec —— 修改代码前必须确认自己手里的文档版本不低于代码
   当时对照的版本(见 `docs/PLAN_TOKEN_RULES.md` 记录的版本与日期)。
4. **路径规则**:所有 wire 路径与签名路径都含 `/trade-api/v2` 前缀;
   签名串 = `timestamp_ms + METHOD + 去掉 query 的路径`。文档 `paths:`
   键是相对 server base 的,引用时必须补前缀。
5. **抓取方式**:任何页面加 `.md` 后缀可拿纯 markdown;
   `openapi.yaml`/`asyncapi.yaml` 直接抓是二进制,改用端点页面内嵌快照 +
   changelog 交叉;大文件(changelog)先落盘再 grep,不要通读。
6. **每次会话核对过的事实,写进 `docs/KALSHI_RULEBOOK.md` 或对应 PLAN 文档**
   (RULE-ID / 事实 / 来源+版本 / 核对日期),避免下个会话重复劳动或
   凭记忆引用。

## 已核对基线（openapi v3.23.0 / asyncapi 现行, verified 2026-07-05）

- 下单 `POST /portfolio/events/orders`(V2):必填 ticker / client_order_id /
  side(bid|ask) / count(定点串) / price(定点美元串) / time_in_force
  (fill_or_kill|good_till_canceled|immediate_or_cancel) /
  self_trade_prevention_type(taker_at_cross|maker);可选 post_only、
  expiration_time、subaccount、order_group_id 等;201 响应含
  order_id/fill_count/remaining_count/ts_ms。
- 撤单 `DELETE /portfolio/events/orders/{order_id}` → 200
  {order_id, client_order_id, reduced_by}。旧 `/portfolio/orders` 仍在,
  "不早于 2026-05-21 弃用"。
- `GET /markets/{ticker}/orderbook?depth=0..100` → `orderbook_fp`
  {yes_dollars, no_dollars},档位=[价格美元串, 数量定点串]。
- `GET /account/limits` → 嵌套 {usage_tier, read/write:{refill_rate,
  bucket_capacity}, grants[]}(2026-04-30 起)。
- `POST /account/api_usage_level/upgrade`:存在;30 tokens Write 桶;
  201=永久 Advanced grant(仅 Predictions);403=最近 100 笔无 API 单。
- 限流:读/写分桶;批量按条计费(25 创建×10=250,25 撤单×2=50);
  429 无 Retry-After/X-RateLimit;写桶容量=2 秒预算(Basic 为 1 秒)。
- WS `wss://…/trade-api/ws/v2`:签名串 `ts+GET+/trade-api/ws/v2`;
  subscribe 参数:channels、market_ticker(s)、market_id(s)、
  send_initial_snapshot、skip_ticker_ack、shard_factor、shard_key;
  错误码 1–22 + 25(订阅缓冲溢出);orderbook_snapshot 用
  yes_dollars_fp/no_dollars_fp,delta 用 price_dollars/delta_fp/side/ts_ms,
  seq 按 sid 单调。
- 待定:`use_yes_price` 未见于现行 subscribe 参数表 —— prod 只读实测定案前,
  不删不加。
