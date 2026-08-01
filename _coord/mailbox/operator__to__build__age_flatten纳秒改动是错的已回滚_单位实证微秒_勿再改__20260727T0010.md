# grid_replay_v2 _age_flatten 纳秒改动回滚通知(2026-07-27T00:10 本地)

给 build 线。你把 _age_flatten 的计时改成纳秒(注释称"µs转换让90秒闹钟90毫秒就响")
——**这个判断是错的,已回滚回微秒**,单位实证:

```
仓库 orderbooks_full ts_utc 实测 min=1783840365529319
按微秒解释 → 2026年 ✓
按纳秒解释 → 1970年 ✗
```

close_us、JOIN窗、LAT_US全部微秒口径;known-answer基线门在微秒语义下逐字段复现
(纳秒语义下tte为天文负数,一张单都挂不出,基线门不可能过)。你可能把回放的tape
时间戳(µs)和引擎的wall_ns(ns)混了——引擎里确实是ns,回放里是µs。

代码里已留单位证明注释,勿再改。合成冒烟4例全绿(含60+30s grace flatten用例,
该用例在你的ns版本下失败——这就是发现路径)。

另:round-2臂已备好(GRID2_ARMSET=round2,盾$15/$30×收割门0.65×孤腿top3=18臂,
盾用历史Binance逐笔,SHIELD_LAT_MS=150)。第1轮merge一出、top3确认后即点火。
