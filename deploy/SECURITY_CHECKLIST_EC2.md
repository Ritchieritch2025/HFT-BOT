# EC2 security checklist — W-A1（操作员逐条核对/点击；agent 不碰控制台）

装机脚本（bringup_ec2.sh）跑完之后再做第 4 条；1–3 条开机时应已就位，核对即可。

1. **SSH 只对你的 IP 开放** — 控制台 EC2 → Security Groups → 该实例的组 →
   Inbound rules：只有一条 `SSH TCP 22  来源 = 你的 IP/32`。
   没有 0.0.0.0/0，没有其他端口。家里换 IP 后连不上 = 正常，来这里改成新 IP。
2. **密钥硬件位**：SSH 登录钥匙在你本机 `~/.ssh/kalshi-key.pem`（chmod 400，
   2026-07-09 已就位）；丢失 = 去控制台换 key pair，不要把 .pem 发给
   任何人/任何聊天（包括 agent 会话）。
3. **凭证（S4）**：`~/.kalshi/env.sh`（600）+ Kalshi 私钥由你手工创建在
   盒子上；永不进 repo/日志/聊天。AWS 侧本步不需要任何 IAM（S3 是 W-A3）。
4. **出站收紧 —— 操作员裁定 2026-07-09：顺延为「实盘（live）前必办」硬门，
   不再是 W-A4 前置。** 理由：测试/部署阶段出站依赖仍在变（装包、
   Telegram、外部数据源、Polymarket 等），现在收紧会反复改规则；纯安全
   加固，不影响割接零缺口。**S1 关联：第一笔真实订单前，本条必须完成
   并按 §5 验证** —— 到时 Security Group
   → Outbound rules，删掉默认 All traffic，只留：
   - `HTTPS TCP 443 → 0.0.0.0/0`（Kalshi API + S3 + git 均走 443）
   - DNS 其实也不用开：VPC 内置解析器（VPC+2 / 169.254.169.253）不经 SG
     评估（同下面 NTP 的旁路，AWS 文档口径）。想加一条
     `DNS UDP/TCP 53 → <VPC CIDR>` 做保险也无害。
   - 时钟同步不用开：chrony 已指向 169.254.169.123（链路本地，不经 SG）
   - 注意：此后 `apt-get` 默认源（http 80）会失效——**包括
     unattended-upgrades 的自动安全更新也会静默停摆**。要装包/恢复自动
     更新时临时加回 `HTTP TCP 80 → 0.0.0.0/0`，装完删掉（或把 apt 源
     永久改 https，一劳永逸）。
5. **验证**（收紧后在盒子上跑）：
   - `curl -sS https://external-api.kalshi.com/trade-api/v2/exchange/status` 通
   - `chronyc tracking` 偏移 < 1ms
   - `curl -m 5 http://example.com` 应超时（80 被封 = 收紧生效）
