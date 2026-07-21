# 团队协作协议（多 agent 异步走文件，不打架）

> 4 个 Claude agent + 审计(Cowork Claude)同一个文件夹干活。不定规矩,它们会:改同一个文件、重复造、抢同一个任务、git 状态互踩。
> 这份协议用 **文件所有权 + 邮箱 + 决策文件** 三招把冲突消掉。**所有 agent 开工前先读这份。**

---

## 角色与所有权（第一铁律:只在自己的目录里写）

| 角色 | 谁 | **只写这里** | 读 |
|---|---|---|---|
| **策略 Strategy** | Claude #1 | `docs/`(计划/规格) + `agents/strategy/` | 全部 |
| **建造 General** | Claude #2 | `agents/build/` + `agents/data/` | `docs/`, `_coord/` |
| **仪表盘 Dashboard** | Claude #3 | `agents/dashboard/` | `agents/data/`(只读), `docs/` |
| **审计 Audit** | Cowork Claude(我) | `agents/audit/` | 全部 |
| (备用) | Claude #5 | `agents/<新角色>/` | 按需 |

**没有任何 agent 改别人目录里的文件。** 需要别人动手 → 走邮箱,不直接改。

---

## 三条防打架规则

1. **一个目录一个写者。** 你只写 `agents/<你>/`(策略额外管 `docs/`)。
2. **计划只读。** `docs/`(AGENT_BRIEF / BUILD_PLAN / DOCTRINE / 各规格)**只有策略 agent + 操作员能改**;别的 agent 只读——防止计划被各自 fork 覆盖。
3. **跨 agent 走邮箱。** 要别人做事,在 `_coord/mailbox/` 丢一个文件,命名:
   `<from>__to__<to>__<主题>__<YYYYMMDDTHHMM>.md`
   对方读到、处理、用**另一个**文件回复(同样命名)。**唯一文件名 = 永不撞车**;只追加、不改别人的。

---

## 任务板与状态

- **`_coord/BOARD.md`** = 任务板。**只有策略 agent 写**(派任务、记状态);其他人**只读**,知道该干嘛、什么完成了。
- **完成一件事** → 把结果写进自己的 `agents/<你>/RESULTS_<任务>.md`(+代码),并往 `_coord/mailbox/` 丢一条 `done` 给策略和审计。
- **抢任务防撞**:认领一个任务 = 在 BOARD 上把它标 `CLAIMED_BY_<你>`;已认领的别碰。

---

## 决策门（操作员拍板,取代逐字批准仪式）

- agent 碰到关键定义**停下**,在 BOARD 上标 `WAIT_DECISION:` + 问题 + 选项 + 推荐默认。**不许自己猜。**
- **只有操作员写 `_coord/DECISIONS.md`。** 你把答案写进去,agent 读到才继续。
- 又快、又还是你说了算。

---

## git（防状态互踩）

- 推荐:**每个 agent 一个分支或 worktree**,合并由策略 agent 或你统一做。
- 最简:各自在自己目录干,**只由 General 一个 agent 做 commit**,别人不碰 git。

---

## 审计(我 · Cowork Claude)干什么

- 读 `agents/` 全部产出 + 论文 + `docs/`,写审计报告到 `agents/audit/`。
- 查四样:**① 数据真不真;② 结果合不合规格;③ robust 不 robust(样本外、样本量够不够);④ 有没有造假 / 过拟合。**
- **只写 `agents/audit/`,从不改别人代码**——发现问题走邮箱提给对应 agent + 操作员。
- 我是 on-demand 的(你叫我时读文件审),不是 24 小时挂着——所以审计是**关卡**,不是实时。

---

## 每个 agent 的开工话（复制粘贴给对应窗口）

**策略 Strategy(Claude #1):**
> 你是 STRATEGY owner。先读 `_coord/ROLES.md` 和 `docs/AGENT_BRIEF.md`。你只写 `docs/` 和 `agents/strategy/`。你负责:从论文策略(S1–S4)出发,在 `_coord/BOARD.md` 派任务、追状态;碰关键定义就在 BOARD 标 `WAIT_DECISION` 等操作员写 `DECISIONS.md`。不许改别人目录。

**建造 General(Claude #2):**
> 你是 BUILDER。先读 `_coord/ROLES.md` 和 `_coord/BOARD.md`。你只写 `agents/build/` 和 `agents/data/`。只做 BOARD 上派给你的任务,结果写 `agents/build/RESULTS_*.md`,完成往 `_coord/mailbox/` 丢 done。需要别人的东西走 mailbox,别直接改。git commit 由你统一做。

**仪表盘 Dashboard(Claude #3):**
> 你是 DASHBOARD。先读 `_coord/ROLES.md` 和 `_coord/BOARD.md`。你只写 `agents/dashboard/`,从 `agents/data/` 只读取数。你负责监控视图:净利、交叉补贴比值(T11)、成交后 markout、毒性(VPIN)、敞口。要数据走 mailbox 找 General,别自己去改数据。

**审计 Audit = 我(Cowork Claude):** 你把要审的产出(或 RESULTS 文件路径)发我,我读了写审计报告到 `agents/audit/`,四项(真/合规/robust/无造假过拟合)逐条过。
