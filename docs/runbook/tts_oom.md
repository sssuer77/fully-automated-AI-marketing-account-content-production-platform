# 剧本 · TTS / 显存 OOM（R4 · 裁决 C8）

> **触发信号**：日志出现 `TTS_OOM`；`nvidia-smi` 显存打满；`tts` 进程反复重启；
> 单句合成越来越慢（开始换页）；`voice` 池连续失败进死信。
> **硬件前提**：8 GB 卡，桌面占约 1.5 GB ⇒ **默认并发 1**。

## 1. 先确认是"显存"还是"内存"

```powershell
. .\scripts\env.ps1
nvidia-smi                                # 看显存占用与是谁占的（python.exe 的 PID）
uv run studio pool status                 # 各池 worker 的 RSS / 忙闲 / 当前 job
uv run studio service status
```

- 显存（`nvidia-smi` 里 `MiB / 8192MiB` 打满）⇒ 本文档第 2 节。
- 系统内存（任务管理器里 python 的提交内存涨到几个 G）⇒ 多半是**单句太长**或**批处理没切分**，
  见第 3 节。

## 2. 显存打满：降并发 + 卸载

```powershell
# ① 立刻降并发（改完需要重启对应 worker 才生效）
#    config/pools.yaml → pools.voice.concurrency: 1
# ② 卸载常驻模型，把显存还回去（空闲 20min 也会自动卸载）
curl.exe 127.0.0.1:8811/unload
# ③ 确认其它占显存的程序（浏览器 / 其它推理服务）先关掉
nvidia-smi
```

**不要**为了让任务跑完而把并发调到 2 以上：8 GB 卡上"3 并发"是原始方案的假设，
T2.2 的实测标定会给出本机安全值（写进 `docs/runbook/tts_concurrency.md` 与 `pools.yaml`）。
在标定之前，**并发 1 是唯一有依据的值**。

## 3. 单句太长

`config/app.yaml → timeouts.tts_sentence_sec` 默认 60s。合成超时 ⇒ 该句记
`TTS_SENTENCE_FAILED` 并**只重试那一句**（句子级断点续传，§03.3.4）：

```powershell
uv run studio script show <task_id>       # 看逐句状态，找出卡住的那一句
```

单句过长通常是稿件的问题（一个句子被写成了 80 个字）。修稿件的**正确姿势**是在
WebUI 稿件面板改那一句 —— 改完只有**该句**的 `tts_status` 回 `pending`、`tts_hash` 置空，
重合成后时间轴**全量重算**。不要手工去删 `data/output/voice/<task_id>/` 里的 WAV。

## 4. 自动降级已经做了什么（先看再动）

四池控制台有自动降级：连续 OOM 达阈值（`pools.yaml → auto_concurrency.oom_threshold`）
会**自动把并发降到下限 1** 并广播；恢复需要**人工确认**（`recover_after_min` /
`gpu_mem_high_ratio` 是恢复判据，不是自动回升）。

所以你的第一动作应该是**看控制台**：如果它已经降到 1 并标了"待人工确认恢复"，
那你要做的是修根因（关掉占显存的程序 / 等实测标定），而不是再降一次。

## 5. 不要做的事

- **不要**改 `restart_limit` 让 `tts` 一直重启：OOM 重启一百次也还是 OOM，
  只是把"一次清楚的失败"换成"一百条刷屏的日志"。
- **不要**开 bf16：Turing 架构没有原生支持（会退化成极慢的软件路径）。
- **不要**在 OOM 之后直接重跑整个任务：句子级重试已经覆盖了这一点，
  整任务重跑会把已经合成好的句子全部重做（GPU 时间是这里最贵的资源）。

## 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
|  |  |  |  |
