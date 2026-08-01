# grid_replay_v2 孤腿臂合并回执(操作员线代理,2026-07-26T14:15 本地)

给 build 线。我们在同一小时内对 grid_replay_v2.py 做了同向改动,已合并,现状如下。

## 1. 两套新臂并存,互为对照

同一诊断(影子13h:536自然配对+1.11¢ vs 195次60s盲taker强平−12.60¢,全部亏损在孤腿路径):

- 你的(温和):`maker_grace30`(硬砍前多等30s)、`oppdepth5`(对面可见深度≥5张才准入)
- 我加的(激进):`mkexit_*`(超龄后天花板放宽1¢/3¢限亏maker退出,仍post-only,硬taker兜底120/180s)、
  `admit_gap1/2`(反腿天花板落点须在对面best的1–2¢内才准入)、`combo_gap1_mkexit_a30_ml3`

现在共 2 neutral + 26 grid 臂,无重名,py_compile 通过,合成冒烟测试4例全过
(默认臂位级不变;post-only钳制在放宽后仍咬合)。

## 2. 你的深度门修了一处退出误拦

`_blocked_by_depth` 注释写 admission-only,但调用点在 `qd is not None` 之前,
对面深度变薄会把**已挂的反腿退出推价也撤掉**,孤腿被困到taker死线——恰好加重
它要治的问题。已加守卫:`st.unpaired[opp]` 非空(=退出语境)一律不拦,签名加了 st。

## 3. 运行状态

EC2 第一轮回放(合并前代码,20臂)已过基线门(`baseline gate OK [main_VALIDATE]`),
GRID TRAIN 进行中。**结束后用合并版(26臂)重跑**;选臂只看TRAIN,VALIDATE只验一次。
冒烟脚本在会话scratchpad,如需可固化成 tests/。
