# 剧本 · 重启风暴 / 守护停手（T4.11）

> **触发信号**：日志出现 `WORKER_RESTART_HALTED`；supervisor 停止重启某个进程；
> `system_logs` 里同一个进程的 `WORKER_DEAD` 在几分钟内刷了几十条。
> **重要**：`WORKER_DEAD` / `WORKER_RESTART_HALTED` **不是** `system.alert.code` 的取值
> （§04.5.2 锁死 8 个值）⇒ 它们落在 `system_logs` 的 `error` 行里，用日志面板筛 `source=watchdog`。

## 0. 守护停手是**设计行为**，不是故障

supervisor 的重启策略是**指数退避**（5s → … → 300s）+ **窗口内上限 20 次**。
超限 ⇒ `halted=True` + 落 `system_logs` error。这是刻意的：一个"起完立刻死"的进程
重启一百次不会变好，只会把日志刷爆、把磁盘写满、把真正的原因埋掉。

**所以守护停手时，第一件事不是重启它，是找根因。**

## 1. 找根因（看子进程自己的日志）

```powershell
. .\scripts\env.ps1
uv run studio service status                     # 谁在跑 / 谁不在 / 端口
Get-Content data\logs\api.log -Tail 60           # 每个进程的 stdout/stderr 都在 data\logs\<name>.log
Get-Content data\logs\tts.log -Tail 60
```

启动器已经把 `stdout`/`stderr` 追加到 `data/logs/<name>.log`（`PYTHONUNBUFFERED=1`），
**起来就死时第一眼就有原因** —— 不用去猜。

| 日志里看到 | 是什么 | 怎么办 |
| --- | --- | --- |
| `[Errno 10048]` / `SERVICE_PORT_BUSY` | 端口被占（上一次没退干净） | 找出占用进程并结束它；确认 `data\logs\*.pid` 里没有僵进程 |
| `ModuleNotFoundError` / `NVENC_UNAVAILABLE` / `FFMPEG_NOT_FOUND` | 环境缺件 | `uv run studio doctor` 逐项修；`scripts/env.ps1` 是否被跳过 |
| `CONFIG_INVALID` / `CONFIG_MISSING` | 配置被改坏 | `uv run studio config validate` 看是哪一份 YAML 的哪一行 |
| `TTS_ENGINE_UNAVAILABLE` | 权重没就位 / 显存不够 | 见 `tts_oom.md`；权重留痕见 `docs/runbook/tts_models.md` |
| `[Errno 28]` | 磁盘满 | 见 `disk_full.md` |
| 什么都没有（日志是空的） | 进程根本没起来 | 手工跑一次：`uv run studio service start`，看前台输出 |

## 2. 修好之后重启

```powershell
.\停止.bat
.\启动.bat
uv run studio service status                     # api ready；其余如实报 degraded 也没关系
```

**重启守护 = 重新开始计数**（超限停手**没有**自动解封，这是设计：让它停着，
直到有人看过日志为止）。

## 3. 验证真的好了

```powershell
# 观察 2~3 分钟：重启次数应该停在 0，而不是缓慢上涨
Get-Content data\logs\api.log -Tail 20
uv run studio pool status
```

真机验收口径（T4.11）是「杀掉任一 worker ⇒ 15s 内被重启并恢复认领」：

```powershell
Get-Process python | Where-Object { $_.Id -in (Get-Content data\logs\voice.pid) } | Stop-Process
# 等 ≤15s，再看：
uv run studio pool status                        # voice 应该回来了，并在认领 job
```

## 4. 不要做的事

- **不要**反复手动重启守护去看"这次能不能起来"：每次重启都从退避的最小值重新开始，
  风暴会再来一遍，而日志会多出几十条噪声。
- **不要**为了让某个进程"至少活着"而把 `restart_limit` 调到很大。
- **不要**改 `pools.yaml` 的阈值来"修"重启问题 —— 守护**不做**阈值热重载，
  改完仍需重启 API 才生效，容易误判"改了没用"。

## 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
|  |  |  |  |
