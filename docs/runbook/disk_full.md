# 剧本 · 磁盘爆满（R1 / 陷阱 #15）

> **触发信号**：总览台报警 `DISK_LOW`；写文件报 `[Errno 28] No space left on device`；
> `uv run studio doctor` 因 `DISK_LOW_C` / `DISK_LOW_D` 拒绝启动；
> 池被自动暂停认领（`pool.pause(auto)`）。
> **背景**：C 盘常年 < 5 GB，**写爆即全站停摆**（连 uv 缓存、TEMP 都写不进去）。

## 1. 先看是谁在占

```powershell
. .\scripts\env.ps1
Get-PSDrive C, D | Select-Object Name, Used, Free

# 各子目录的体积（data\ 下逐层看）
Get-ChildItem data -Directory | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue |
             Measure-Object -Property Length -Sum).Sum
    [pscustomobject]@{ Dir = $_.Name; MB = [math]::Round($size / 1MB, 1) }
} | Sort-Object MB -Descending
```

D 盘的水位门禁是 **15 GB**（`config/app.yaml → disk_gate.free_d_min_gb`），
低于它 `render` / `publish` 池会**自动暂停认领**并报 `DISK_LOW` —— 那是保护，不是故障。

## 2. 先干跑一遍 GC，看能腾多少（不动手）

```powershell
.\scripts\gc_media.ps1 -DryRun          # 表里"删除文件 / 移动文件"两行 = 这一轮能腾出来的量
```

**注意**：`tmp（不动）` 那一行是 `data/tmp/`（pytest / node / uv 与在途渲染共用的暂存区），
GC **故意不碰它**。要清它只能人工确认后手动清，且**必须在没有任务在跑的时候**。

## 3. 真的回收

```powershell
.\scripts\gc_media.ps1                  # 按 §03.7.5 的保留期回收
```

它会做五件事：删过期 DB 行（日志 / LLM 流水 / 任务事件）· 删 `completed` 满 24h 的句子音频与
人声母带（**成片不在则跳过**）· 删 7 天前的 `scenes/*.mp4` 与失败任务的 `.partial` ·
把 TTS 缓存按 LRU 压到 5 GB · 把已消费热点**移动**进 `data/hot/archive/`。

**永不清理**：成片 / 封面 / 稿件 / 时间轴 / manifest / 滤镜图 / 备份 / 素材库 / 热点归档。

## 4. 还差一点：把删掉的空洞还给盘

`DELETE` **不会让库文件变小**。GC 报告最后一行 `库体积 … 空洞 X MB` 就是"删了但没还给盘"的量：

```powershell
uv run studio db vacuum                  # 只看：报告可回收多少，退出码 1
.\停止.bat                                # VACUUM 会独占写 + 重写整个文件，必须停进程
uv run studio db vacuum --yes            # 它会**自己先钉一份检查点**再动手
.\启动.bat
uv run studio db check
```

> 顺序很重要：**先备份再 VACUUM**。VACUUM 中途没空间 = 库损坏，而那时你手里必须有一份可用的备份。
> `db vacuum` 把这条纪律做进了命令里：没有 `--yes` 不动手；检查点产出失败 ⇒ **直接中止**
> （与"迁移前检查点失败只警告"不同：迁移是增量的，VACUUM 是把库整个重写一遍）。

## 5. 备份与检查点也会占盘

```powershell
uv run studio db backups                 # 日备 7 + 周备 4，看总量
Get-ChildItem data\backups -Recurse -File | Measure-Object -Property Length -Sum
```

- 日备/周备由 `db backup` 自动轮转（7 + 4），**不要手删**：手删会删掉"唯一可用的那一份"。
- `data/backups/checkpoints/premigrate_*.db` **不参与轮转**（故意的）。确认新库已经稳定运行
  几天之后，可以人工清理最老的那几份，**保留最近 2 份**。
- `data/backups/restore_drill/` 是演练用的临时库，随时可删。

## 6. 长期：别让它再发生

| 手段 | 在哪 |
| --- | --- |
| 环境变量重定向（uv/pip/HF/ModelScope/torch/TEMP 全部离开 C 盘） | `scripts/env.ps1`；`uv run studio env` 逐项断言 |
| 水位门禁自动暂停认领 | `config/app.yaml → disk_gate`（阈值可调） |
| 保留期 | `config/app.yaml → retention`（§03.7.5） |
| 每日自动回收 | `config/app.yaml → scheduler.gc_cron`（默认 `0 4 * * *`）；未接调度器前用计划任务 `scripts/gc_media.ps1` |

## 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
|  |  |  |  |
