# 跨会话状态转达 — 07-12 封印收口报告(VERBATIM ARCHIVE)

- received: 2026-07-13,操作员粘贴入本 Cowork 会话(来源 = EC2 管道侧会话
  的收口报告)
- 本会话无法独立核验其中数字(EC2/S3 不在本会话可达范围);按全文保存
  规则逐字归档,作为
  `AUDIT_SPORTS_AUTORESEARCH_01_2026-07-13.md` ADDENDUM 1 的证据出处。
  对方会话自称已 durable 落档(B11/B12、记忆、镜像);若属实,本文件为
  冗余副本,以生产侧归档为准。

---

## 操作员转达原文(VERBATIM)

✅ 核心问题解决 —— 07-12 封印落地并权威验证,数据面稳定。 收口报告:
主战果(你要"尽快全解决"的那个问题)
07-12 SEALED + verify PASS(18:31Z):322 个归档文件、1.0914 亿行、go_no_go=True、印 sha bc37de4c…、警报已清、采集/入库全程健康、零数据丢失。整整卡了一天的封印,收上了。
两个根因已定位并登记(这才是真正值钱的产出)
- B11(真凶):今天卡一天的机理不是 RFQ 本身,而是封印链在导出步反复输掉与 ingest 的写锁竞争——停了 ingest 它又被拉回抢锁,导出永远 ATTACH 不上,"daily seal failed"刷了一整天,直到某轮侥幸赢锁才封上。修法:封印链导出期间硬保持 ingest 停止(像 recover_rfq_seal 的 clauses 16-18)。与 B9(RFQ 解析税)并列,两个容量债,W06 Stage 2 加量前必须修。
- B12(W05 发布卡点):07-12 的研究 release 发布时失败关闭——箱上 vaultWriter 密钥能写 research/ 但没有 s3:GetObjectVersion 权限做 P0-1 强制的回读校验。这需要你控制台给 vaultWriter 加一条 s3:GetObjectVersion(+GetObject)on research/*。加完这一条,07-12 的研究数据就能发上桥、Mac 可取。
对 auto-research agent 零影响
它在 Mac/别的会话跑,不在生产箱上;我全程只碰 EC2 的封印/入库,没动它读的任何东西。箱上的生产研究链仍熔断(HOTFIX-02),那是另一回事(等 B5 内存护栏)。
我今天的教训(已写进记忆)
封印链是异步的,我误把中间信号(旧日志行、导出窗口内 ingest 短暂消失)当终局报了三次成败——只有 seals/date=D.json + verify PASS 才算数。为此向你道过歉,后续只认实证。
还差你一步(非阻塞、可稍后):控制台给 vaultWriter 加 s3:GetObjectVersion on research/*,W05 的 07-12 发布即可完成。核心数据面问题已解决,你现在 clear context / 让研究 agent 继续都安全——一切durable 落档(B11/B12、记忆、镜像已同步)。
