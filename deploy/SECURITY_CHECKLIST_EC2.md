# EC2 security checklist — W-A1（操作员逐条核对/点击；agent 不碰控制台）

装机脚本（bringup_ec2.sh）跑完之后再做第 4 条；1–3 条开机时应已就位，核对即可。

1. **SSH 只对你的 IP 开放** — 控制台 EC2 → Security Groups → 该实例的组 →
   Inbound rules：只有一条 `SSH TCP 22  来源 = 你的 IP/32`。
   没有 0.0.0.0/0，没有其他端口。家里换 IP 后连不上 = 正常，来这里改成新 IP。
2. **密钥硬件位**：YINQIAN.pem 只存你本机，`chmod 400`；丢失 = 去控制台
   换 key pair，不要把 .pem 发给任何人/任何聊天（包括 agent 会话）。
3. **凭证（S4）**：`~/.kalshi/env.sh`（600）+ Kalshi 私钥由你手工创建在
   盒子上；永不进 repo/日志/聊天。AWS 侧本步不需要任何 IAM（S3 是 W-A3）。
4. **出站收紧（bring-up 完成后才做，apt 装包需要 80 口）** — Security Group
   → Outbound rules，删掉默认 All traffic，只留：
   - `HTTPS TCP 443 → 0.0.0.0/0`（Kalshi API + S3 + git 均走 443）
   - `DNS UDP 53 + TCP 53 → <VPC CIDR>`（如 172.31.0.0/16；VPC 内置解析器）
   - 时钟同步不用开：chrony 已指向 169.254.169.123（链路本地，不经 SG）
   - 注意：此后 `apt-get` 默认源（http 80）会失效；要装包时临时加回
     `HTTP TCP 80 → 0.0.0.0/0`，装完删掉（或把 apt 源改 https）。
5. **验证**（收紧后在盒子上跑）：
   - `curl -sS https://external-api.kalshi.com/trade-api/v2/exchange/status` 通
   - `chronyc tracking` 偏移 < 1ms
   - `curl -m 5 http://example.com` 应超时（80 被封 = 收紧生效）
