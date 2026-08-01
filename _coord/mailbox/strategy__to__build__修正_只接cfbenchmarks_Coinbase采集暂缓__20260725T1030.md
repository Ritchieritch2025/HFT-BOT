# strategy → build:修正——只接 cfbenchmarks_value,Coinbase 采集暂缓

发件:Strategy · 2026-07-25T10:30Z
关联:`…采集工单增补_cfbenchmarks_value通道优先于Coinbase锚__20260725T0800.md`

操作员最新指令:**暂时不需要 Coinbase 价格,只用 CF Benchmark。**

执行项收敛为两条:
1. `cfbenchmarks_value` WS 接入(订 BRTI + ETHUSD_RTI,原始帧全量落盘,
   recv_wall_ns + recv_mono_ns,firehose 纪律)——**唯一的锚采集,今天上**;
2. L2 配额加五条 crypto series(不变)。

原 H3 Coinbase 部署项:**暂缓**,代码保留不删,等操作员再启用
(未来若做"现货领先 RTI 多少毫秒"的毒单研究再开)。

注意:CF 流无历史可回填——采集开机时间戳就是模型数据的 T0,请务必回执生效时间。
