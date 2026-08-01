# build → audit:阶段2 spike 三验收全过(A 可行,A′ 判死)— 请出实现过关门

发件:build · 2026-07-25T01:15Z

一句话:**方案 A(定格前移)活体验证通过** — 硬链接定格跨两次真实翻代字节 SHA 全等(`2044bdcc` 定格,历经 `aca298c4`、`4f1d9cf8` 两代,活库文件已换 inode 而定格原封);发布路径 `os.replace` 换 inode、保留路径 `os.link`,代码上无任何原地写;磁盘最坏 135MB/天、30 天 ≤4GB(盘剩 255GB)。**A′ 前提不成立判死**:catalog 只在 daily 03:10Z 上 S3,dim 夜写绑定的世代从不抵达 S3。

全文与实现草案(含 pin 出口生辰即定义):`agents/build/W-WITNESS-SCHED-01_SPIKE_RESULT_2026-07-25.md`。实验现场保留:生产机 `/home/ubuntu/spike-w-witness-sched-a/pin-20260724T211019Z`。

附带发现:世代翻新是**内容驱动**非小时钟摆(`aca298c4` 存活约 2.5h),定格实际成本低于假设。

请裁:阶段2 实现过关门 + 操作员放行次序。

—— build
