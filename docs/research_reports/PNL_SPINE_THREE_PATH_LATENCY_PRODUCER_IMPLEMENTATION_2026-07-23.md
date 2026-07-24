# PnL Spine 三路径真实延迟：非下单实现报告

日期：2026-07-23
状态：repair-02 已按独立复审 `a5b092b` 的唯一剩余 B-2 收口；真实
PLACE/CANCEL 在任何 authority、凭据、网络或输出之前硬拒绝。没有联网，
没有读取真实凭据，没有发送订单，没有执行 IOC_EXIT。

## 交付结论

本次把“延迟采集”和“延迟证据接纳”拆成两个严格边界：

1. C++ probe 默认是无凭据、无 socket 的 dry-run。复审确认当前官方
   GetOrders 没有按 `client_order_id` 精确查询参数；ticker/时间窗列表可能
   超时、延迟可见或返回空，CANCEL 也可能超时，而 partial fill 又需要尚未
   实现的 IOC flatten。因此无法证明所有 ambiguous POST 结果都不残留挂单
   或仓位。`kAmbiguousPlaceRecoveryProven=false` 是编译期发布门，
   `--execute-place-cancel` 现在在解析 authority、创建 ledger、读取凭据或
   建立 socket 前直接退出 2。旧真实发送实现仅保留为不可达的待审 scaffold，
   不能部署或授权。
2. Python producer 只接纳完整的 PLACE、CANCEL、IOC_EXIT 三路径私有因果
   trace，而且必须精确为每条路径各一份。它不再接受调用方自报 SHA；会自己
   打开并哈希二进制、配置、CA、环境、主机、时钟、两份 raw authority、
   两份 durable consumption、两份 terminal receipt 和 root promotion
   receipt。缺字段、授权重放、IOC 生命周期重复、未来时间、来源未固定、
   部分成交或仓位对不上，全部整批拒绝。
3. 接纳成功后同时生成：
   - runner 直接消费的
     `pnl-spine-measured-latency-receipt-v1`；
   - 兼容原有审计链的
     `pnl-spine-latency-evidence-inventory-v1`；
   - 聚合、不泄露订单标识和原始 trace 的
     `pnl-spine-latency-audit-receipt-v1`。

当前没有伪造完整三路径证据。C++ 的 PLACE/CANCEL 与 IOC_EXIT 两条真实
mutation 路径都不可达；IOC_EXIT 只保留只读预检。完整 measured receipt
必须等待带官方可证明 ambiguous recovery、账户级交易阻断、partial-fill
flatten 的新执行器另行授权和审计。

## 文件

- `tools/research/pnl_spine/measured_latency_producer.py`
  - 严格加载 canonical 私有 trace；
  - 生产 CLI 只接收原始 artifact 路径，不接收 expected SHA 或 caller
    clock；producer 独立打开、哈希并交叉验证 code/config/CA/environment/
    host/clock/authority/consumption/terminal/promotion 全链；
  - root promotion receipt 同时绑定执行侧 source 的 path/device/inode/
    uid/mode/size/raw SHA 与提升后对象的 path/owner/mode/size/raw SHA；
  - 要求精确一份 PLACE、一份 CANCEL、一份 IOC_EXIT；每份 action authority
    必须 `max_attempts=1`，拥有不同 nonce，并且 consumption 与 terminal 的
    transaction ID、路径、上下文、mutation count 和最终 trace SHA 全部一致；
  - 强制 PLACE/CANCEL 为同一 `order_ref` 生命周期，且 CANCEL decision
    不得早于 PLACE effective；
  - PLACE/CANCEL 与 IOC_EXIT 必须分别由两份不同的 raw live authority
    固定，禁止一份授权或 nonce 跨动作扩权；
  - 强制三路径绑定同一 execution host/environment/clock receipt；
  - authority consumption 必须位于授权窗口；PLACE 与 IOC mutation 必须
    位于各自窗口；CANCEL 过期后只允许 terminal 明确记录的单次减险清理；
    trace、clock、matching-engine、consumption、terminal、promotion 任一
    时间位于未来或违反先后关系都会拒绝；
  - 生成 measured、inventory、aggregate 三份 canonical receipt；
  - 生产 CLI 只读取已经提升为 root-owned/read-only 且整条路径无 symlink
    的私有 trace；
  - 三份输出必须发布到 root-owned、非 group/world-writable 的父目录，
    先全部 `O_EXCL|O_NOFOLLOW` 预留，再统一写入、`fsync` 和改为 `0444`；
    任一路径冲突都不会留下半套 receipt。
- `apps/pnl_latency_probe.cpp`
  - 默认 dry-run；`--execute-place-cancel` 与 `--execute-ioc-exit` 均在
    runtime、authority、凭据、网络和输出前硬拒；
  - 内置纯函数 ambiguous recovery 判定与五类攻击 fixture：
    exact order found + clean cancel + terminal flat 是唯一可证明安全结果；
    order not found、query timeout、cancel timeout、partial fill 全部不安全。
    因为真实请求可能进入任一不安全分支，发布常量保持 false；
  - 以下 PLACE/CANCEL 发送、broker、到期和收据逻辑是为未来修复保留的
    不可达 scaffold，不能据此宣称当前具备真实下单能力；
  - PLACE/CANCEL 记录 decision、sent、acknowledged、effective；
  - mutation 使用当前 V2 event-order endpoint：
    `POST/DELETE /portfolio/events/orders[/order_id]`，不使用已进入弃用窗口
    的 legacy create/cancel；
  - 记录 request/response SHA、order reference SHA、live authority SHA；
  - 记录 V2 matching-engine `ts_ms`；零成交时
    `average_fee_paid_e6=null`，未来有成交的 IOC 样本必须提供精确 E6
    average fee 和 E4 average fill price；
  - 私有 trace 保存 ticker hash、subaccount、book side、TIF、
    request reduce-only、request price、fill/remaining、仓位和结构化 V2
    响应事实；mutation、order readback、position before/after 原始字节由
    response SHA 组合绑定；
  - mutation ack 后再 GET 订单状态和仓位，readback 完成才记 effective；
  - Get Order 只依赖官方记录的 `initial_count_fp`、`fill_count_fp` 和
    `remaining_count_fp`，不再假设未文档化的 `status/order_status` 字段；
  - PLACE 必须完整 resting、零 fill、仓位不变；
  - CANCEL 必须完整 canceled、零 fill、remaining=0、仓位不变；
  - PLACE 对账失败时 best-effort cancel，但该样本仍硬失败；
  - IOC_EXIT 只有 position/orderbook GET 预检，代码中没有 IOC POST；
  - IOC 预检明确输出未来执行必须使用 V2
    `time_in_force=immediate_or_cancel`、`reduce_only=true`，并按当前有符号
    仓位计算 ASK（减正仓）或 BID（减负仓）；
  - `--execute-ioc-exit` 在解析环境和读取凭据之前直接退出 2。
  - `producer_code_sha256` 来自 `/proc/self/exe`（Linux）或实际 Mach-O
    可执行文件（macOS）的字节哈希；生产发送要求该二进制及其整条路径
    root-owned/read-only/no-symlink。
  - producer config、environment receipt、execution-host receipt、
    clock-quality receipt 都必须是 root-owned/read-only/no-symlink；
    host receipt 会与本机 hostname、`/etc/machine-id` 及 EC2
    DMI/device-tree instance id 逐项核对。
  - clock receipt 必须声明 synchronized，使用 probe 的同一 clock id，
    与当前 host/instance/machine-id 完全一致，最大误差不超过 10ms，
    且生成时间不超过 5 分钟。
  - 当次 PLACE/CANCEL authority schema 升为 v2，限制为 15 分钟、一次
    transaction/attempt、1 张、
    1¢ price cap、固定 cash-loss/fee cap；authority 还绑定 ticker、
    receipt、构建、配置、host/environment、clock receipt、输出路径和
    consumption-ledger、terminal receipt 路径和已审核 CA SHA。
  - authority、输出、consumption-ledger、terminal 路径均必须为绝对规范
    路径且禁止 symlink；父目录必须 root-owned 且禁止 group/world 写。
    程序必须从 real/effective root 启动，在读取凭据、建客户端或 POST 前，
    先以 `openat(O_EXCL|O_NOFOLLOW)` 创建三个 root-owned `0400` 对象并
    `fsync` durable consumption，然后 `setgroups/setgid/setuid` 永久降权。
    执行 UID 无权删除这些对象，同一 authority 绑定的唯一 ledger 路径不能
    再创建。
  - authority 接受时同时派生 steady-clock deadline；PLACE 签名前和发送前
    分别重查 wall + monotonic 到期，CANCEL 签名/发送边界同样重查并只允许
    最多一次减险 DELETE。
  - POST 响应丢失或非 201 时不自动重试，写
    `BLOCKED_AMBIGUOUS_PLACE_OUTCOME_NO_RETRY` terminal receipt，消费保持
    不可逆，等待按 deterministic client order ID 人工对账。
  - 发送前清除大小写 proxy、SOCKS、CURL/SSL/REQUESTS/AWS CA 等环境覆盖；
    每个 mutation 前重新验证环境仍为空、libcurl 实际默认 CA 路径及其
    root 固定 SHA 未漂移。
- `tests/test_pnl_spine_measured_latency_producer.py`
  - producer 的字段、因果、守恒、隐私、全链原始 artifact、权限重放、
    重复生命周期、未来证据和 promotion 对抗测试。
- `tests/test_pnl_latency_probe_safety.py`
  - C++ dry-run、IOC 硬拒、root broker/降权顺序、wall+monotonic 到期、
    proxy/CA 清除、未文档化 Get Order 字段缺失与模糊 POST 硬阻塞测试。
- `Makefile`
  - 增加 on-demand `build/pnl_latency_probe`。
- `tools.json`
  - 注册为 `live_order`，因此控制台禁止运行，即使默认模式是 dry-run。

## 私有 trace 接纳条件

trace 必须精确包含三个样本：PLACE、CANCEL、IOC_EXIT 各一个，不能增加
“第二次同 authority 测量”或重复 IOC。每个样本必须具备：

- `path` 与 `action_semantics` 精确对应：
  - PLACE → `NEW_ORDER_PLACE`
  - CANCEL → `RESTING_ORDER_CANCEL`
  - IOC_EXIT → `IOC_POSITION_REDUCING_EXIT`
- 同一 `clock_id` 上：
  `decision_ns <= sent_ns <= acknowledged_ns <= effective_ns`，且总延迟大于
  零；
- 非占位 SHA-256：
  `order_ref`、request、response、source event、current live authority、
  host/environment/clock receipt；
- 正整数 matching-engine `ts_ms`，以及与 fill 状态一致的
  `average_fee_paid_e6` 和 `average_fill_price_e4`；
- matching-engine wall-clock 时间不得在未来，必须不晚于 receipt 的 UTC
  wall clock，且全部样本位于同一个五分钟 clock-quality 窗口和对应
  authority transaction 内；
- 2xx mutation 状态；
- 固定点数量守恒：
  `requested = filled + canceled + remaining`；
- 订单状态、fill 和 position readback 已明确标记 reconciled。

路径特定条件：

- PLACE：RESTING、filled=0、canceled=0、remaining=requested、仓位不变。
- CANCEL：CANCELED、filled=0、canceled=requested、remaining=0、仓位不变。
- PLACE/CANCEL：必须使用同一订单哈希，ticker/subaccount/book side/
  request price/TIF 完全相同，且 exchange ts 与本地 causal ts 均保持先后。
- IOC_EXIT：reduce-only、EXECUTED、filled=requested、remaining=0、
  canceled=0、filled 等于退出前绝对仓位、退出后仓位严格为零；正仓只能
  ASK exit，负仓只能 BID exit，且必须是独立订单生命周期。

全局只允许两个 order lifecycle：PLACE/CANCEL 共享一个，IOC_EXIT 独占
另一个；request、response 与 source-event SHA 也必须全局唯一。

这意味着部分成交 PLACE/CANCEL、部分 IOC exit、仓位翻向、残余仓位和
“只收到 HTTP ack 但没有 effective readback”均不能成为成功证据。

## 隐私边界

私有 trace 可以在受控目录保留真实事件的哈希绑定，但所有公开 receipt
仅包含单向 `order_ref_sha256`。测试确认公开 measured/inventory/aggregate
中不会出现 raw order ID、请求正文或响应正文。

## Repair-02：为什么没有“假装恢复”

独立复审 `a5b092b` 证明首轮修复只做到了 evidence fail-closed，没有做到
order-risk fail-closed。对丢失 POST response 的真实 create：

- GetOrders 只支持 ticker/status/min_ts/max_ts/cursor 等列表过滤，不支持
  精确 `client_order_id` 查询；
- “列表里没找到”不能证明订单不存在，可能是可见性延迟；
- query timeout 不能证明订单不存在；
- cancel timeout 不能证明 remaining 已归零；
- partial fill 即使撤掉剩余量，仍会留下仓位，而本 binary 的 IOC transmitter
  明确未实现；
- 仓库当前也没有由该 probe 能原子触发并被所有执行器强制遵守的账户级
  trading block。

因此 repair-02 没有采用盲重发 POST，也没有把“没查到”包装成安全结果。
真实 PLACE 在函数入口即由编译期 false gate 拒绝。该 gate 位于
`require_common_options`、`collect_runtime_facts`、root ledger reservation、
credential `getenv` 和 `lane.send` 全部之前。要重新开放，必须同时具备并
独立审计：

1. 官方支持的确定性订单身份恢复；
2. query/cancel 模糊结果下的账户级强制交易阻断；
3. cancel 后 exact order + position terminal readback；
4. partial fill 的授权、限次、可证明归零 flatten；
5. 对上述五类 transport/state 攻击的真实 mock transport 测试。

## Authority 一次性消费边界

下述格式仍由 publisher 验证并保留为未来实现基础，但当前 live gate 在读取
authority 前已拒绝，因此不会消费或执行任何真实 authority。

PLACE/CANCEL authority schema 是
`pnl-spine-latency-probe-authority-v2`，必须精确包含：

- 身份与时效：`nonce_sha256`、`receipt_id`、`issued_at_unix_s`、
  `expires_at_unix_s`、`single_use=true`、`max_attempts=1`；
- 资金上限：`quantity_e4=10000`、`price_cap_e4=100`、
  `max_cash_loss_e6=10000`、`max_fee_e6=10000`；
- 行为上限：`allow_place_cancel=true`、`allow_ioc_exit=false`、
  `max_place_orders=1`、`max_cancel_orders=1`；
- 精确绑定：ticker、measured host、environment/host/clock SHA、
  producer code/config SHA、reviewed CA SHA、trace output path SHA、
  consumption ledger path SHA、terminal consumption receipt path SHA。

上线时三个输出父目录必须由 root 创建为 root-only、不可由 execution UID
写目录项的目录。probe 的 root preamble 是本地一次性 broker：它在任何凭据
读取之前创建并 durable 写入 nonce consumption；随后永久降权，execution
UID 只能通过继承的 write-only fd 完成本次 trace/terminal，不能 unlink、
rename、重开或清空 root-owned 对象。`transaction_id` 由 raw authority
SHA、nonce 和 receipt ID 唯一派生，同一 authority 只对应这一个路径集合和
一个 transaction，不是“每次 invocation 再生一个 transaction”。

最终 Python publisher 不能只相信提升后的 trace SHA。它必须同时看到：

1. raw PLACE/CANCEL 与 raw IOC authority；
2. 两份 root 不可变 initial consumption；
3. 两份 terminal consumption，成功状态且 mutation 次数各为一；
4. 实际 binary/config/CA/environment/host/clock artifacts；
5. 独立 root promotion receipt，证明 execution source 的 inode/uid/mode/
   bytes 与 promoted trace 完全相同。

任一对象缺失、被删、被重写、时间在未来、路径/nonce/transaction/count
不一致，都不能发布 `LATENCY_AGGREGATE_READY`。

## 测试收据

执行：

```text
make build/pnl_latency_probe
PYTHONPYCACHEPREFIX=/private/tmp/codex_pycache python3 -m unittest \
  tests.test_pnl_latency_probe_safety \
  tests.test_pnl_spine_measured_latency_producer -v
PYTHONPYCACHEPREFIX=/private/tmp/codex_pycache python3 -m unittest \
  tests.test_pnl_spine_measured_latency_producer \
  tests.test_pnl_spine_latency_evidence -v
PYTHONPYCACHEPREFIX=/private/tmp/codex_pycache python3 -m unittest \
  discover -s tests -p 'test_pnl_spine*.py' -q
python3 tools/check_registry.py
```

结果：

- C++ probe：编译通过，无 warning；
- 内置官方 V2 fixed-point create/cancel/get-order/position fixture：
  `V2 CONTRACT SELF-TEST PASS`；
- C++ safety、producer 与既有 latency evidence 联合专项：36/36 通过；
- 全部 `test_pnl_spine*.py` 联合回归：65/65 通过；
- 本轮专项测试以当前代码重新执行并通过；旧版报告中的 30/30 与 66/66
  计数已被代码并发演进淘汰，不再作为当前收据；
- tool registry：通过，probe 被列入 console-forbidden `live_order`。

## 仍需单独授权的真实动作

以下事项不在本次实现和测试中，也没有被隐式授权：

1. 实现并审计可证明的 PLACE ambiguous-recovery 及 partial-fill flatten，
   然后才可能重新开放生产 probe；
2. 创建、签发或安装当次 live authority；当前 binary 即使收到 authority
   也会在读取前拒绝；
3. 实现、独立审计或执行 IOC_EXIT；当前 `--execute-ioc-exit` 明确
   fail-closed；当前也不会产生新的 PLACE/CANCEL 输出；
4. 使用真实凭据、真实订单、真实仓位；
5. 将任何真实私有 trace 发布到公共仓库。

实现依据为 Kalshi 官方
[Create Order (V2)](https://docs.kalshi.com/api-reference/orders/create-order-v2)
与
[Cancel Order (V2)](https://docs.kalshi.com/api-reference/orders/cancel-order-v2)
契约。

独立审计 `8cf9517` 对首版判 FAIL；复审 `a5b092b` 确认除 ambiguous POST
风险外的修复全部闭合。本报告继续记录 repair-02 的最终裁决：在缺少可证明
恢复链时，真实 PLACE 必须保持硬关闭。下一道门是独立代理在 repair-02
commit 上确认该 gate 不可绕过并重放五类 ambiguous 攻击。即使复审 PASS，
其含义也只是“当前 binary 不可能遗留新挂单”，不是授权真实采样。

要真正采 PLACE/CANCEL，必须提交新的、具备完整恢复/账户阻断/partial-fill
flatten 的实现并再次审计。IOC_EXIT 也必须另立执行器、完成同等级审计和
当次授权；只有真实完全退出且 fill/position 守恒、两份消费链与 promotion
链都完整时，producer 才会生成三路径 runner-ready receipt。
