# PnL Spine 三路径真实延迟：非下单实现报告

日期：2026-07-23
状态：离线实现与测试完成；没有读取真实凭据，没有发送订单，没有执行
IOC_EXIT。

## 交付结论

本次把“延迟采集”和“延迟证据接纳”拆成两个严格边界：

1. C++ probe 默认是无凭据、无 socket 的 dry-run。PLACE/CANCEL 的真实发送
   分支必须同时通过现有 live 环境安全门，以及外部 SHA-256 固定、未过期、
   ticker/构建/配置完全匹配的一次性 authority。代码、配置、环境与主机
   指纹不再接受 CLI 自报：实际运行二进制会自哈希，并读取 root 固定的配置、
   环境、主机和时钟收据。
2. Python producer 只接纳完整的 PLACE、CANCEL、IOC_EXIT 三路径私有因果
   trace。缺字段、路径语义错配、非因果时间、来源未固定、部分成交或仓位
   对不上，全部整批拒绝。
3. 接纳成功后同时生成：
   - runner 直接消费的
     `pnl-spine-measured-latency-receipt-v1`；
   - 兼容原有审计链的
     `pnl-spine-latency-evidence-inventory-v1`；
   - 聚合、不泄露订单标识和原始 trace 的
     `pnl-spine-latency-audit-receipt-v1`。

当前没有伪造完整三路径证据。C++ 真实写路径只实现了 PLACE/CANCEL；
IOC_EXIT 只实现只读预检，完整 measured receipt 必须等待另行授权和审计的
真实 IOC_EXIT executor 产生合格样本。

## 文件

- `tools/research/pnl_spine/measured_latency_producer.py`
  - 严格加载 canonical 私有 trace；
  - 要求 raw source、environment、execution host、clock-quality receipt、
    live authority、producer code、producer config 全部由调用方提供外部
    SHA pin；
  - 强制 PLACE/CANCEL 为同一 `order_ref` 生命周期，且 CANCEL decision
    不得早于 PLACE effective；
  - PLACE/CANCEL 与 IOC_EXIT 必须分别由两份不同的外部 live-authority
    SHA 固定，禁止一份授权跨动作扩权；
  - 强制三路径绑定同一 execution host/environment/clock receipt；
  - 生成 measured、inventory、aggregate 三份 canonical receipt；
  - 生产 CLI 只读取已经提升为 root-owned/read-only 且整条路径无 symlink
    的私有 trace；
  - 三份输出必须发布到 root-owned、非 group/world-writable 的父目录，
    先全部 `O_EXCL|O_NOFOLLOW` 预留，再统一写入、`fsync` 和改为 `0444`；
    任一路径冲突都不会留下半套 receipt。
- `apps/pnl_latency_probe.cpp`
  - 默认 dry-run；
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
  - 当次 PLACE/CANCEL authority 限制为 15 分钟、一次 attempt、1 张、
    1¢ price cap、固定 cash-loss/fee cap；authority 还绑定 ticker、
    receipt、构建、配置、host/environment、clock receipt、输出路径和
    consumption-ledger 路径。
  - authority、输出和 consumption-ledger 路径均必须为绝对规范路径且
    禁止 symlink；输出父目录必须是 root-owned、sticky、仅专职 group
    可写。程序在读取凭据、建客户端或 POST 之前，先以
    `openat(O_EXCL|O_NOFOLLOW)` 预留输出和 ledger，写入消费记录、`fsync`
    并改为 `0400`；任何后续崩溃都不自动重放。
- `tests/test_pnl_spine_measured_latency_producer.py`
  - producer 的字段、因果、守恒、隐私和外部 pin 对抗测试。
- `tests/test_pnl_latency_probe_safety.py`
  - C++ dry-run、IOC 硬拒、缺 authority pin 拒绝、只读预检凭据门测试。
- `Makefile`
  - 增加 on-demand `build/pnl_latency_probe`。
- `tools.json`
  - 注册为 `live_order`，因此控制台禁止运行，即使默认模式是 dry-run。

## 私有 trace 接纳条件

每个样本必须具备：

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
- matching-engine wall-clock 时间必须不晚于 receipt 的 UTC wall clock，
  且全部样本位于同一个五分钟 clock-quality 窗口；
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

这意味着部分成交 PLACE/CANCEL、部分 IOC exit、仓位翻向、残余仓位和
“只收到 HTTP ack 但没有 effective readback”均不能成为成功证据。

## 隐私边界

私有 trace 可以在受控目录保留真实事件的哈希绑定，但所有公开 receipt
仅包含单向 `order_ref_sha256`。测试确认公开 measured/inventory/aggregate
中不会出现 raw order ID、请求正文或响应正文。

## Authority 一次性消费边界

authority schema 是 `pnl-spine-latency-probe-authority-v1`，必须精确包含：

- 身份与时效：`nonce_sha256`、`receipt_id`、`issued_at_unix_s`、
  `expires_at_unix_s`、`single_use=true`、`max_attempts=1`；
- 资金上限：`quantity_e4=10000`、`price_cap_e4=100`、
  `max_cash_loss_e6=10000`、`max_fee_e6=10000`；
- 行为上限：`allow_place_cancel=true`、`allow_ioc_exit=false`、
  `max_place_orders=1`、`max_cancel_orders=1`；
- 精确绑定：ticker、measured host、environment/host/clock SHA、
  producer code/config SHA、trace output path SHA、consumption ledger path
  SHA。

上线时 trace/ledger 父目录必须由 root 创建为专职 group 可写并带 sticky
bit；代码要求其整条祖先路径 root-owned/no-symlink，并用目录 fd +
`O_EXCL|O_NOFOLLOW` 消除路径替换和自动重放。生成后的私有 trace 与 ledger
由执行身份持有、模式 `0400`；在交给 Python producer 前，还必须由独立的
root 步骤原字节提升为 root-owned/read-only。代码不会把尚未提升的私有
trace 直接出版为研究收据。若威胁模型包含“执行身份主动删除自己生成的
ledger”，仍需 root broker/immutable-storage handoff；当前边界防的是事故
重跑与其他同组身份替换，不宣称抵御已控制执行身份。

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
- C++ safety 加 producer：29/29 通过；
- producer 加既有 latency evidence 联合回归：36/36 通过；
- 本轮专项测试以当前代码重新执行并通过；旧版报告中的 30/30 与 66/66
  计数已被代码并发演进淘汰，不再作为当前收据；
- tool registry：通过，probe 被列入 console-forbidden `live_order`。

## 仍需单独授权的真实动作

以下事项不在本次实现和测试中，也没有被隐式授权：

1. 在生产执行 PLACE/CANCEL probe；
2. 创建、签发或安装当次 live authority；
3. 实现、独立审计或执行 IOC_EXIT；当前 `--execute-ioc-exit` 明确
   fail-closed，PLACE/CANCEL 输出也明确标为
   `PLACE_CANCEL_RECONCILED_IOC_EXIT_MISSING`；
4. 使用真实凭据、真实订单、真实仓位；
5. 将任何真实私有 trace 发布到公共仓库。

实现依据为 Kalshi 官方
[Create Order (V2)](https://docs.kalshi.com/api-reference/orders/create-order-v2)
与
[Cancel Order (V2)](https://docs.kalshi.com/api-reference/orders/cancel-order-v2)
契约。

下一道门必须先独立审计 C++ 真实分支和 authority 格式。之后可以在明确
的当次 live authority 下采 PLACE/CANCEL。IOC_EXIT 必须另立执行器、完成
同等级审计和当次授权，只有真实完全退出且 fill/position 守恒后，producer
才会生成三路径 runner-ready receipt。
