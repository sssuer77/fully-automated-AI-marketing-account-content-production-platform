# 剧本 · 任务卡住 / 池停摆

> **触发信号**：任务长时间停在 `pending` / `queued_voice` / `queued_render` / `voicing` /
> `rendering` 不动；`stage_detail` 的进度数字不再变；某个池一个 job 都不动。
> **先分清**：是**没有 worker 在认领**，还是**有 worker 但 job 起不来**。

## 1. 三张表定位

```powershell
. .\scripts\env.ps1
uv run studio service status     # 五进程在不在
uv run studio pool status        # 每个 worker 的存活 / 忙闲 / 当前 job
uv run studio db check           # 库本身是否健康（先排除"账本坏了"）
```

| 看到 | 结论 | 去哪一节 |
| --- | --- | --- |
| 某池**没有任何存活 worker** | 认领的人不在 | 第 2 节 |
| worker 存活但 `busy` 且 `当前 job` 一直不变 | 单元卡住了（超时是协作式的，见下） | 第 3 节 |
| worker 存活且空闲，但队列里有 `pending` job | 认领被**门禁**挡住了 | 第 4 节 |
| 任务状态是 `manual_pool` | 已经自动转人工池了（不是卡住） | 第 5 节 |

## 2. 没有 worker 在认领

```powershell
.\停止.bat
.\启动.bat
uv run studio service status
```

进程起不来 ⇒ 看 `data\logs\<name>.log` 的尾部，然后翻 [`restart_storm.md`](restart_storm.md)。

**注意**：池 worker 在**未就绪**时**不会**被硬拉起来（T4.11 裁定 103）：
`readiness` 不过就记 `degraded`，而不是静默起一个空转的 worker。所以
"某个池一直没有 worker" 在 M1~M3 阶段是**如实报告**，不是 bug。

## 3. worker 忙但不动

单元超时是**协作式**的：脉冲线程置 `timed_out`，handler 自己调 `ctx.check_alive()` 才会退出。
**"硬杀进程"是 supervisor 的事**，不是 worker 自己的职责。所以：

- 单句合成超时 ⇒ 该句记 `TTS_SENTENCE_FAILED`，**只重试那一句**（句子级断点续传）。
- 整单元超时 ⇒ 按 `UNIT_TIMEOUT` 重排；`attempt_count` 达到上限 ⇒ 进死信 / `manual_pool`。
- worker 自己僵死（连脉冲都不跑了）⇒ 心跳超时 ⇒ supervisor 判死并重启，**在途租约过期后自动回收**。

手工回收过期租约（supervisor 每轮自动做，这里是给"守护也没在跑"的场合）：

```powershell
uv run studio pool reap --timeout-sec 15
```

## 4. 有 job 但没被认领：看门禁

按可能性排序：

1. **磁盘水位**：`free_D < 15 GB` ⇒ `render` / `publish` 池**自动暂停认领**并报 `DISK_LOW`。
   这是保护。处理见 [`disk_full.md`](disk_full.md)；腾出空间后水位恢复，覆盖会**自清**。
2. **手动暂停**：四池控制台里被人工暂停过。恢复要在**同一个界面**做，
   且相邻两条 `audit_ops`（`pool.pause` → `pool.resume`）要能对上 —— 判据落在留痕上。
3. **限频**：`daily_limit_per_account` / `min_gap_min`（`config/pools.yaml`）挡住的 job
   会带 `not_before` 顺延，**不算失败**。到点自然会动。
4. **发布开关**：`config/publish.yaml → enabled: false` 时 publish 池空转并记 `skipped_disabled`。
5. **并发降到 0**：不会发生 —— 下限硬编码为 1（陷阱 #79：并发降到 0 = 沉默的暂停）。

## 5. 已经是 `manual_pool`（不是卡住）

任务 `attempt_count` 达到 `max_attempts` 后会转 `manual_pool` 并在总览台可见。
这是**要人做决定**的状态：

```powershell
uv run studio script show <task_id>       # 看它死在哪一步
```

然后在 WebUI 上重试 / 改稿重跑 / 丢弃（三者都会写 `audit_ops`）。
**不要**直接改库里的 `status`：`tasks.status` 只允许 `TaskService.transition()` 写
（契约测试静态拦截），手改会绕过状态机与留痕。

## 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
|  |  |  |  |
