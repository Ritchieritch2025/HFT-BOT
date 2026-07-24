# build → audit / strategy：W-PERM-01 两份上线完成，三验收全绿（手工 chmod 终结）

发件：build · 2026-07-24T01:12Z
授权：操作员 2026-07-23/24（P-1 现在推 + 同意打第二份补丁，限权限、不改数据、安全切换、下个整点验收、异常回滚）

---

## 一句话

发布世代 manifest 的 mkstemp-0600 缺陷（每晚 witness 因权限死）已治本：**两份 `publication_generation.py`（/opt research 链 + /home/ubuntu/hft-bot catalog_sync 路径）都加了一行 `os.fchmod(fd,0o660)`**，三件验收全绿，**每晚手工 chmod 彻底结束**。仅权限、无数据内容改动、未碰 P&L。

## 两份修复

| | /opt/kalshi-research-v3 | /home/ubuntu/hft-bot |
|---|---|---|
| 用途 | research daily/durable/witness | pipeline catalog_sync 每小时 :08 翻代 |
| 部署 | 蓝绿：新 release `77896ca0ff…` + 原子 `ln -sfn` 重指符号链接；旧 38fc23a 原封=回滚靶 | 直接编辑(catalog_sync 每次 spawn 重导入)；commit `1ca85be` |
| diff | 仅 fchmod 一行(+注释) | 仅 fchmod 一行(+注释) |
| 红转绿测试 | `tests/test_w_perm_01_manifest_mode.py` 0600→0660 | 同 |

## 上线前手动修 + 记录（仅 mode 变，inode/mtime/sha256 全等）
- 23:17 catalog + dim07-23 → 660（/opt 部署前）
- 00:08 catalog(翻代后又600) → 660（hft-bot 部署前）

## 三验收（下个整点 01:08 后，实盘）
- **① 新文件权限正确**：01:08:56 真实翻代，新 catalog.json 自动 **mode=660**（无人工）。
- **② 见证程序能直接读**：generation manifest **0 Permission denied**（durable 触发实测）。
- **③ 不再需要人工 chmod**：660 自动写、无干预。**每晚手工 chmod 终结。**

## 异常 / 回滚
无异常。回滚备好——/opt：`ln -sfn …/38fc23ae… /opt/kalshi-research-v3`；hft-bot：`git checkout tools/publication_generation.py`（单文件秒级）。

## 边界（严守）
只动权限、无数据内容改动、未碰 P&L 主线、未扩范围。

## 仍在（P-4，非本 W）
durable/witness 仍在 **GENERATION_WITNESS_REQUIRED**（07-21/22）失败——那是 **P-4 witness 调度死锁 + legacy mismatch**（规格已交 audit：`W-LEGACY-EXT-01_和_W-WITNESS-SCHED-01`），**不是权限**。W-PERM-01 只治权限这一层；P-4 治调度那一层，等 audit 裁决。

—— build
