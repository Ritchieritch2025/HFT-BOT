# W-RFQ-FRESH-01 — Capture 无中断切换 Runbook

日期：2026-07-17

状态：**DRAFT / NOT EXECUTED / PLACEHOLDER COMMANDS / CAPTURE ONLY**

本文件把 `W-RFQ-FRESH-01` 的 capture 部署边界落实成一次可审计的切换顺序。
它不是部署授权、AWS 写入令、W09 启动令或 research 发布令。本文中的命令只是模板；
所有 `REPLACE_*` 标记必须由经审计的精确值替换，并在执行前由另一名操作员复核。
只要仍存在一个 `REPLACE_*`，整个 runbook 就必须停止。

## 1. 不可变边界

1. 现有 `/home/ubuntu/hft-bot` 是生产数据工作树，即使它是 dirty，也必须保持原样。
   禁止在其中执行 checkout、reset、clean、stash、merge、pull、build 或测试；不得为了
   本次切换“顺手清理”任何已有修改。它继续拥有 canonical
   `/home/ubuntu/hft-bot/work/raw` 与 `/home/ubuntu/hft-bot/work/live`。
2. 新代码只从已经推送并独立审计的 40-hex exact commit 建立到
   `/home/ubuntu/hft-bot-rfq-fresh`。运行代码、Python 模块和 `build/ws_shadow` 必须全部
   来自这个 clean deployment worktree，不得从旧工作树混装文件。
3. 不启动第二个 RFQ writer。旧 `kalshi-rfq-capture.service` 在全部预检、precommit、
   readback、dry-run 和 unit verify 完成以前持续运行；最终只对这个独立 RFQ service
   做一次受控 restart。`kalshi-pipeline.service` 不停止、不重启、不 reload。
4. Fresh authority 使用 generation-specific、root-owned、service-user 不可写的目录。
   precommit 工具只 create-only 写一个 canonical authority envelope；capture 直接消费这一个
   文件。工具 stdout 只是操作摘要，不是第二份 durable receipt，也不是 capture 输入。不得
   unlink、覆盖、原地修补或用固定文件名复用下一 epoch。
5. T0 是未来 UTC 整点。它必须晚于 precommit、clean-worktree 验证、dry-run、unit 安装
   和 RFQ restart，并至少保留经审计 precommit 工具要求的 lead；当前下限不得小于
   300 秒。窗口不足时必须选择再下一个整点，不能压缩 lead。
6. 新 unit 的所有可写数据路径必须显式指回旧 canonical `work/raw` / `work/live`；
   只有代码与 binary 来自新 worktree，control evidence 来自 root-owned generation
   目录。这样不会复制原始数据，也不会建立第二个 bucket 或第二套 canonical raw。
7. 现有 `kalshi-s3-sync-hourly.timer` 保持原样运行，只继续上传已经关闭的 canonical
   source hours。不得手工补传、扩大 IAM、改变目标 prefix，或把 producer upload
   解释成 research 发布。
8. 全程禁止：旧 RFQ repair、W09 RFQ canary、RFQ tagger、object/version tag mutation、
   research/control S3 PUT/COPY/DELETE、research manifest 发布、pipeline 服务变更、交易
   或下单能力变更。

## 2. 操作变量与停止条件

以下变量只说明每个值必须来自哪里。不要把未替换的模板直接交给 shell。

```sh
OLD_ROOT='/home/ubuntu/hft-bot'
FRESH_ROOT='/home/ubuntu/hft-bot-rfq-fresh'
REMOTE_URL='REPLACE_WITH_PUSHED_REMOTE_URL'
REMOTE_REF='REPLACE_WITH_PUSHED_REMOTE_REF'
EXACT_COMMIT='REPLACE_WITH_AUDITED_40_HEX_COMMIT'
GENERATION='REPLACE_WITH_UNIQUE_SAFE_GENERATION_ID'
T0_UTC='REPLACE_WITH_YYYY-MM-DDTHH:00:00Z'
MIN_LEAD_SECONDS='REPLACE_WITH_AUDITED_VALUE_NOT_LESS_THAN_300'
AUTHORITY_INPUT='REPLACE_WITH_ROOT_OWNED_CANONICAL_AUTHORITY_INPUT'
CONTROL_DIR="/var/lib/kalshi-rfq-fresh/control/${GENERATION}"
AUTHORITY_ENVELOPE_PATH="${CONTROL_DIR}/authority-envelope.json"
DROPIN_DIR='/etc/systemd/system/kalshi-rfq-capture.service.d'
DROPIN_PATH="${DROPIN_DIR}/50-fresh-${GENERATION}.conf"
AUDIT_DIR="/var/log/kalshi-rfq-cutover/${GENERATION}"
RUNTIME_HASH_PATH="${AUDIT_DIR}/runtime-files.sha256"
```

渲染后的执行包必须先机械确认：exact commit 是 40 个小写 hex；generation 只含经审计
允许的安全字符；T0 可严格解析且分钟、秒均为零；所有路径等于本 runbook 的固定根；
所有文件和命令中 `REPLACE_` 的出现次数为零。任一检查失败立即停止。

任何阶段出现以下情况也立即停止，不进入 restart：exact commit 未推送、旧 RFQ service
不健康、主 pipeline 不健康、主机 UTC/NTP 未同步、T0 lead 不足、旧工作树状态发生非预期
变化、新 worktree 不 clean、测试或 build 失败、control 文件可被 `ubuntu` 写入、envelope
readback 不一致、unit 仍引用固定 authority 文件名、或者 unit 混用了旧工作树中的
代码/binary。

## 3. 阶段 0 — 冻结现状，但不停止旧 RFQ

在 root-owned `AUDIT_DIR` 记录每条命令的开始/结束 UTC、退出码和输出 SHA-256，不记录
credential 内容。以下都是只读预检模板：

```sh
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 0700 "$AUDIT_DIR"
/usr/bin/date -u +%FT%TZ
/usr/bin/timedatectl show --property=NTPSynchronized --property=Timezone
/usr/bin/systemctl is-active kalshi-rfq-capture.service
/usr/bin/systemctl is-active kalshi-pipeline.service
/usr/bin/systemctl is-active kalshi-s3-sync-hourly.timer
/usr/bin/systemctl show kalshi-rfq-capture.service --property=MainPID --property=ExecStart --property=FragmentPath --property=DropInPaths
/usr/bin/systemctl cat kalshi-rfq-capture.service
/usr/bin/git -C /home/ubuntu/hft-bot status --porcelain=v2 --untracked-files=all
/usr/bin/git ls-remote --refs "$REMOTE_URL" "$REMOTE_REF"
```

保存旧 unit/drop-ins 的原始字节、SHA-256、RFQ MainPID/child PID、命令行以及旧工作树
status 输出的 SHA-256。后续只允许 `work/raw` 与 `work/live` 因现有生产进程正常增长；
不得改动旧工作树的 tracked/index 状态。

同时从推送端只读确认 `REMOTE_REF` 精确指向 `EXACT_COMMIT`。多结果、空结果、短 SHA、
annotated tag 未解析到预期 commit 或 remote/ref 不一致均停止。不得以本地分支名、
`HEAD`、`latest` 或当前 dirty 工作树内容代替 exact commit。

## 4. 阶段 1 — 建立并冻结 clean deployment worktree

优先使用独立 clone 作为 deployment worktree，使旧 `/home/ubuntu/hft-bot` 的工作文件、
index 和 Git 管理状态都不受影响。目录必须预先不存在；禁止覆盖已有目录。

```sh
/usr/bin/sudo -u ubuntu /usr/bin/git clone --no-checkout -- "$REMOTE_URL" /home/ubuntu/hft-bot-rfq-fresh
/usr/bin/sudo -u ubuntu /usr/bin/git -C /home/ubuntu/hft-bot-rfq-fresh checkout --detach "$EXACT_COMMIT"
/usr/bin/git -C /home/ubuntu/hft-bot-rfq-fresh rev-parse --verify HEAD
/usr/bin/git -C /home/ubuntu/hft-bot-rfq-fresh status --porcelain=v2 --untracked-files=all
```

`rev-parse HEAD` 必须逐字节等于 `EXACT_COMMIT`，status 必须为空。然后只在新 worktree
构建并运行该 exact commit 的审计测试：

```sh
/usr/bin/sudo -u ubuntu /usr/bin/make -C /home/ubuntu/hft-bot-rfq-fresh build/ws_shadow
/usr/bin/sudo -u ubuntu /usr/bin/bash -c 'cd /home/ubuntu/hft-bot-rfq-fresh && exec /usr/bin/python3 -m pytest -q REPLACE_WITH_AUDITED_CAPTURE_TEST_LIST'
/usr/bin/git -C /home/ubuntu/hft-bot-rfq-fresh diff --quiet
/usr/bin/git -C /home/ubuntu/hft-bot-rfq-fresh diff --cached --quiet
/usr/bin/git -C /home/ubuntu/hft-bot-rfq-fresh status --porcelain=v2 --untracked-files=all
/usr/bin/sha256sum /home/ubuntu/hft-bot-rfq-fresh/tools/rfq_capture.py /home/ubuntu/hft-bot-rfq-fresh/tools/fresh_rfq_receipts.py /home/ubuntu/hft-bot-rfq-fresh/build/ws_shadow
/usr/bin/sudo /bin/chown -R root:root /home/ubuntu/hft-bot-rfq-fresh
/usr/bin/find /home/ubuntu/hft-bot-rfq-fresh -xdev -perm /022 -print
/usr/bin/git -C /home/ubuntu/hft-bot-rfq-fresh status --porcelain=v2 --untracked-files=all
```

测试列表必须来自该 commit 的独立审计收据，不能临场缩减。被 Git ignore 的 build
产物可以存在，但任何 tracked/index drift 或未解释的 untracked source 文件均停止。
把 commit、测试结果和 runtime file hashes 写入 `AUDIT_DIR` 下的 root-owned 独立部署
证据。当前 canonical envelope **不绑定** `RUNTIME_HASH_PATH`，capture 也不读取该 manifest；
不得把它描述为 envelope binding 或启动门。当前实现真正执行的是 clean Git worktree
检查，并要求 runtime HEAD 等于 envelope 内的 `deployment_commit`。最后把 deployment
worktree 交给 root，并要求 `find ... -perm /022` 无输出；service user 只能读/执行，不能在
Python import 与 child exec 之间修改代码或 binary。

## 5. 阶段 2 — 选择 T0 并 create-only precommit

先根据实际剩余维护时间选择 `T0_UTC`。它必须：

- 是未来 UTC 整点；
- 晚于旧 RFQ 终止裁决；
- 晚于本次新代码预计 restart 和立即验收；
- 在 authority create 时仍满足 `T0 - now >= MIN_LEAD_SECONDS`；
- 给 unit 渲染、dry-run、独立复核和失败撤回留出额外余量。

如果任何步骤逼近 T0，停止并创建新的 generation 和更晚 T0；不得修改已经创建的
authority，也不得继续使用 lead 已不足的 generation。

每个 generation 使用独立目录。目录在 precommit 时为 root-owned `0700`，最终改成
root-owned `0555`。输入必须是刚生成、root-owned 的 canonical authority。precommit 必须
以 root 执行，并由工具直接用 `O_CREAT|O_EXCL` 和 mode `0444` 创建唯一的 canonical
envelope；不能先由 `ubuntu` 创建后再移交给 root。

```sh
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 0700 "$CONTROL_DIR"
/usr/bin/sudo /usr/bin/python3 /home/ubuntu/hft-bot-rfq-fresh/tools/precommit_fresh_rfq_authority.py \
  --input "$AUTHORITY_INPUT" \
  --output "$AUTHORITY_ENVELOPE_PATH" \
  --min-lead-seconds "$MIN_LEAD_SECONDS" \
  --max-created-age-seconds 600 \
  --expected-owner-uid 0
```

这是当前 CLI 的真实接口：`--input` 读 authority，`--output` 只写
`AUTHORITY_ENVELOPE_PATH`，`--expected-owner-uid 0` 同时要求执行者、父目录和新文件 owner
都是 UID 0。工具会写 canonical JSON 加一个换行，fsync 文件与父目录，再用固定 FD
readback。成功 stdout 只是一条不含完整 deny set 的摘要；由阶段 0 的审计执行器原样记录
即可。capture 不消费 stdout，也不存在需要另存的 authority file / durable precommit
receipt / envelope 三文件协议。

唯一 envelope 当前实际绑定：

- 内嵌完整 canonical authority，包括 lane ID、`GENERATION`、`T0_UTC`、`EXACT_COMMIT`、
  固定操作员授权 SHA-256、capture template、read-only credential scope 和完整旧 284-object
  deny authority；
- canonical authority SHA-256、deployment commit、strict T0；
- `precommitted_at_utc`、`minimum_lead_seconds` 和 canonical `envelope_sha256`。

stdout 摘要还报告 output path、file SHA-256、envelope SHA-256、创建字节数和时间字段，供
审计对照，但这些字段不会让 stdout 变成 capture authority。runtime file hashes 和绝对路径
也没有被当前 envelope 绑定；绝对路径由 generation-specific systemd 参数固定。

**创建 envelope 后绝不能对该文件执行 `chown`、`chmod`、`touch`、`cp`、`mv`、rename
或重新写入。** 这些动作会改变 inode/identity/ctime；capture 要求文件 ctime 与
`precommitted_at_utc` 相差不超过 5 秒，否则 fail closed。只允许只读 `stat`、hash 和
validator。目录封口可改父目录 mode，不会修改 envelope ctime：

```sh
/usr/bin/stat --format='%u:%g %a %h %d:%i %s %Z %n' "$AUTHORITY_ENVELOPE_PATH"
/usr/bin/sha256sum "$AUTHORITY_ENVELOPE_PATH"
/usr/bin/sudo /bin/chmod 0555 "$CONTROL_DIR"
/usr/bin/stat --format='%u:%g %a %h %d:%i %s %Z %n' "$CONTROL_DIR" "$AUTHORITY_ENVELOPE_PATH"
/usr/bin/sudo -u ubuntu /usr/bin/test -r "$AUTHORITY_ENVELOPE_PATH"
/usr/bin/sudo -u ubuntu /usr/bin/test ! -w "$CONTROL_DIR"
/usr/bin/sudo -u ubuntu /usr/bin/test ! -w "$AUTHORITY_ENVELOPE_PATH"
```

第二次 `stat` 中 envelope 的 owner/mode/link/inode/size/ctime 必须与创建后第一次记录
一致，且必须为 UID/GID `0:0`、mode `444`、link count `1`。不要为了“修正”结果而修改
envelope。

若事务中途失败，precommit 工具会仅对自己刚创建且 identity 未变化的 inode 执行内部
rollback；不能假定 partial 一定会留下。无论输出是否被安全清除，都把 generation 标记
`ABANDONED` 并另建新 generation 与更晚 T0。若工具报告 rollback 失败且 partial 仍在，
保留原样审计；禁止人工删除、补写或修补。

## 6. 阶段 3 — 新代码 dry-run 与 unit 渲染

在旧 RFQ service 仍运行时，用与最终 unit 完全相同的 absolute interpreter、代码、
control evidence 和 data-path 参数执行 dry-run。它不得打开 socket、取得 live lock 或
写 raw/live 文件。

```sh
/usr/bin/sudo -u ubuntu /usr/bin/bash -c '
  export PYTHONDONTWRITEBYTECODE=1
  source /home/ubuntu/.kalshi/rfq_readonly.env.sh || exit 78
  exec /usr/bin/python3 /home/ubuntu/hft-bot-rfq-fresh/tools/rfq_capture.py \
    --dry-run \
    --raw-root /home/ubuntu/hft-bot/work/raw \
    --live-root /home/ubuntu/hft-bot/work/live \
    --disable-flag /home/ubuntu/hft-bot/work/live/rfq_disable \
    --lock-dir /home/ubuntu/hft-bot/work/live/rfq_capture.lock \
    --alert-path /home/ubuntu/hft-bot/work/live/rfq_alert.json \
    --state-path /home/ubuntu/hft-bot/work/live/rfq_state.json \
    --ledger-path /home/ubuntu/hft-bot/work/live/rfq_segments.ndjson \
    --metrics-path /home/ubuntu/hft-bot/work/live/rfq_metrics.ndjson \
    --fresh-lane-authority "$1" \
    --fresh-lane-authority-owner-uid 0 \
    --fresh-lane-expected-generation "$2"
' _ "$AUTHORITY_ENVELOPE_PATH" "$GENERATION"
```

这里两个 owner 参数不要混淆：precommit CLI 使用 `--expected-owner-uid 0`；capture CLI
实际名称是 `--fresh-lane-authority-owner-uid 0`。Dry-run 必须 exit 0，并报告
`fresh_lane_state=BOUND_AUTHORITY`、generation-specific authority path 和预期 canonical
authority SHA。exit 0 还意味着 loader 已验证：安全 parent、root owner、exact mode `0444`、
link count 1、canonical envelope、generation、ctime/precommit/T0 lead、clean runtime HEAD 与
`deployment_commit` 一致。当前 dry-run 不打印 runtime hash manifest、file SHA 或 stdout
summary binding；不得把未打印/未验证的字段写进验收标准。任何 `UNBOUND_DIAGNOSTIC`、路径
漂移、owner/mode/ctime 拒绝或 commit 不一致均停止。

使用 generation-specific root-owned systemd drop-in；不要把 authority 复制回固定
`/home/ubuntu/hft-bot/work/control/fresh_rfq_epoch_authority.json`。渲染后的 drop-in 至少
具备以下语义，所有占位符必须在安装前消失：

```ini
[Service]
WorkingDirectory=/home/ubuntu/hft-bot-rfq-fresh
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
# Reset the generic unit's fixed EnvironmentFile selector.  This audited
# generation drop-in pins its immutable path directly below instead.
EnvironmentFile=
ReadOnlyPaths=/var/lib/kalshi-rfq-fresh/control/REPLACE_WITH_GENERATION
ReadOnlyPaths=/home/ubuntu/hft-bot-rfq-fresh
ExecStart=
ExecStart=/usr/bin/bash -c 'source /home/ubuntu/.kalshi/rfq_readonly.env.sh || exit 78; exec /usr/bin/python3 /home/ubuntu/hft-bot-rfq-fresh/tools/rfq_capture.py --raw-root /home/ubuntu/hft-bot/work/raw --live-root /home/ubuntu/hft-bot/work/live --disable-flag /home/ubuntu/hft-bot/work/live/rfq_disable --lock-dir /home/ubuntu/hft-bot/work/live/rfq_capture.lock --alert-path /home/ubuntu/hft-bot/work/live/rfq_alert.json --state-path /home/ubuntu/hft-bot/work/live/rfq_state.json --ledger-path /home/ubuntu/hft-bot/work/live/rfq_segments.ndjson --metrics-path /home/ubuntu/hft-bot/work/live/rfq_metrics.ndjson --fresh-lane-authority /var/lib/kalshi-rfq-fresh/control/REPLACE_WITH_GENERATION/authority-envelope.json --fresh-lane-authority-owner-uid 0 --fresh-lane-expected-generation REPLACE_WITH_GENERATION'
StandardOutput=append:/home/ubuntu/hft-bot/work/live/rfq_capture.out.log
StandardError=append:/home/ubuntu/hft-bot/work/live/rfq_capture.err.log
```

不再虚构一个不存在的 runtime-hash/envelope `ExecStartPre` verifier。当前
`rfq_capture.py` 启动路径本身 fail closed 地验证 envelope 的 owner/mode/link/identity、
canonical bytes、generation、ctime/precommit/T0 lead，并现场 probe clean Git HEAD 后与
envelope `deployment_commit` 比较；运行期间及 segment finalize 还会重新验证文件 identity
和 digest。`RUNTIME_HASH_PATH` 只保留作独立部署审计材料。

把渲染后的 drop-in 放入 `AUDIT_DIR`，先检查 `REPLACE_` 为零并记录 SHA-256，再安装、
daemon-reload 和 verify。此时不要 restart：

```sh
/usr/bin/sudo /usr/bin/install -d -o root -g root -m 0755 /etc/systemd/system/kalshi-rfq-capture.service.d
/usr/bin/sudo /usr/bin/install -o root -g root -m 0644 REPLACE_WITH_RENDERED_DROPIN "$DROPIN_PATH"
/usr/bin/sudo /usr/bin/systemctl daemon-reload
/usr/bin/sudo /usr/bin/systemd-analyze verify kalshi-rfq-capture.service
/usr/bin/systemctl show kalshi-rfq-capture.service --property=ExecStart --property=ExecStartPre --property=WorkingDirectory --property=DropInPaths
/usr/bin/systemctl is-active kalshi-rfq-capture.service
```

最后一条仍须为 `active`，MainPID 必须仍是阶段 0 记录的旧 RFQ supervisor；
daemon-reload 不能被误报成已经切换。重新确认 pipeline PID 未变化、hourly sync timer
仍 active、T0 lead 仍充足、旧工作树 tracked/index 状态未改变。

## 7. 阶段 4 — 唯一一次生产切换

在 T0 以前留出足够时间让新 child 完成 authenticated subscribe ACK。所有预检和两人
复核通过后，记录 restart 前一秒 UTC，只执行：

```sh
/usr/bin/sudo /usr/bin/systemctl restart kalshi-rfq-capture.service
```

禁止同时 restart/reload `kalshi-pipeline.service`、sync timer、daily services 或 W09。
禁止先 stop 后长时间人工操作；systemd restart 是本次成功路径唯一允许的 writer
中断。restart 所在小时无条件视为维护影响小时，不得作为 fresh PASS；T0 必须位于其后
的未来整点。

## 8. 阶段 5 — 立即验收与首个完整小时

### 8.1 Restart 后立即验收

记录每项 UTC 与原始输出：

```sh
/usr/bin/systemctl is-active kalshi-rfq-capture.service
/usr/bin/systemctl show kalshi-rfq-capture.service --property=MainPID --property=ExecMainPID --property=ExecStart --property=WorkingDirectory --property=DropInPaths
/usr/bin/systemctl status kalshi-rfq-capture.service --no-pager
/usr/bin/pgrep -a -f '/home/ubuntu/hft-bot-rfq-fresh/tools/rfq_capture.py|/home/ubuntu/hft-bot-rfq-fresh/build/ws_shadow'
/usr/bin/systemctl is-active kalshi-pipeline.service
/usr/bin/systemctl is-active kalshi-s3-sync-hourly.timer
```

必须证明：

- 只有一个 RFQ supervisor 和其唯一 active child，没有旧、新双 writer；
- supervisor 代码路径与 child binary 路径都来自 clean worktree；
- 实际 clean HEAD、authority envelope 和 generation 与预提交值一致；独立 runtime hash
  快照如有记录，只作为部署审计对照，不冒充 capture-enforced binding；
- 新 supervisor PID、child PID、child generation 已记录，且与旧 PID 明确区分；
- state/log 显示 authority 已绑定，pre-T0 时只能是 diagnostic，不虚假声称 eligible；
- pipeline PID 与服务状态未受影响，raw/live canonical 路径继续增长；
- 没有 AWS tag、research publish、W09 或 trading 动作。

### 8.2 T0 与第一小时验收

持续观察到 T0，不再 restart。T0 前数据只可作 diagnostic。T0 后第一个完整小时关闭并
经过稳定窗口后，读取本地 immutable receipt evidence，机械验证：

- `schema=rfq-segment-receipt-v3`、正确 lane/generation；
- `fresh_lane_state=BOUND_AUTHORITY`；
- lane ID、canonical authority SHA、generation 和 exact deployment commit 与本地 envelope
  内嵌 authority 完全一致；
- supervisor PID、child PID、child generation 等于本次进程；
- segment hour 正好从 T0 开始，boundary/health/subscription/counter/EOF/shard exact-set
  全部 strict PASS，findings 为空；
- 同一小时不存在第二条、冲突或被省略的 receipt。

首小时未 strict PASS 即验收失败。该小时失格；若它属于候选日期 D，则 D 也失格。
不得通过重写 receipt、supersession、相邻小时补齐或降低门槛挽救。

现有 hourly producer upload 继续按原 timer 执行。不要因为 receipt container 在其观察
小时仍为 active、因此比 capture shard 晚一个同步周期，便手工执行 S3 copy；等待既有
closed-hour 排除规则自然上传。此 runbook 不运行任何 AWS CLI，也不把 S3 出现对象解释
成 research eligibility。

## 9. 失败回滚

以下任一项触发回滚：service 未 active、真实 HEAD/tree 漂移、canonical envelope/readback
不一致、多 writer、错误代码/binary 路径、credential scope
异常、pipeline 受影响、T0 前无法稳定订阅、或首个完整小时不是 strict PASS。

回滚步骤：

1. 记录失败 UTC、阶段、PID、generation、T0、unit/drop-in SHA 和原始错误；立即把受影响
   小时标为 `INELIGIBLE_CUTOVER_FAILURE`。禁止 repair。
2. 使用阶段 0 保存的 exact unit/drop-in 字节恢复旧 RFQ unit 配置；不得修改、reset
   或 checkout `/home/ubuntu/hft-bot`。
3. daemon-reload、`systemd-analyze verify`，然后只 restart
   `kalshi-rfq-capture.service`。不得触碰 pipeline、sync timer 或 W09。
4. 验证旧 RFQ service 恢复、只有一个 writer、canonical raw/live 继续增长、pipeline
   PID 未变化。
5. 保留失败的新 worktree、control directory、唯一 canonical envelope、stdout 摘要日志和
   独立部署输出供审计；不得删除或覆盖。
6. 整个 generation 标记 `ABANDONED`。任何重试都必须使用新 generation、新目录、
   新 create-only envelope 和更晚的 UTC 整点 T0。

回滚本身需要第二次 RFQ restart，这是失败路径的受控例外；成功路径只有阶段 7 的一次
restart。回滚不恢复该小时的 eligibility，也不授权旧 RFQ lineage。

## 10. 下一 epoch 与固定路径禁令

`O_EXCL` authority 与固定 service 路径不能同时支持安全轮换。禁止通过 unlink、rename
覆盖或原地编辑
`/home/ubuntu/hft-bot/work/control/fresh_rfq_epoch_authority.json` 来启动下一 epoch。

每个 epoch 必须拥有新的 generation-specific control directory，且 systemd 使用新的
root-owned drop-in 直接引用该 epoch 的 absolute canonical envelope path。旧 drop-in、
envelope 和 stdout 摘要日志保留为审计材料；切换新 epoch 前重复本 runbook 的完整
precommit、readback、dry-run、verify 和单次 RFQ restart。不得使用可变 symlink、`current`
指针或服务启动后可被 `ubuntu` 改写的 selector。

## 11. 完成收据清单

本次 cutover 只有在以下材料齐全时才可标记 `CAPTURE_CUTOVER_ACCEPTED`：

- 旧工作树切换前后 tracked/index status SHA 相同；
- pushed remote/ref 与 exact 40-hex commit 的证明；
- clean deployment worktree HEAD/status、测试、build 和独立 runtime hash 审计记录；
- generation/T0/lead 计算与 UTC/NTP 证据；
- 唯一 canonical envelope 的 stat、SHA、ctime 和工具/capture fixed-FD readback，以及仅作
  操作摘要的 precommit stdout；
- runtime manifest（如生成）的独立 stat/SHA，并明确记录它未被 envelope 或 capture 绑定；
- service user 对 control dir/files 的只读、不可写证明；
- old unit/drop-ins、rendered drop-in、安装后 unit 的完整字节与 SHA；
- dry-run 输出、systemd verify、restart 精确 UTC；
- old/new supervisor PID、child PID/generation、唯一 writer 与 clean code/binary 路径；
- T0 后首个完整小时的 strict v3 PASS receipt 与完整 shard set；
- pipeline 未重启、W09 未启动、AWS tag/research publish 未发生、hourly canonical timer
  未改变的证明；
- 若失败，则 rollback 与 `ABANDONED` generation 收据。

该收据只证明 fresh capture 切换成功。它不等于 24+2 数据窗口完成，不等于 receipt
container/exact-VersionId/base overlay 已验证，也不把状态提升为
`READY_FOR_W09_RFQ_RESEARCH`。
