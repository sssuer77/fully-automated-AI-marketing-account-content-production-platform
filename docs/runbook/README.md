# 运维手册（`docs/runbook/`）

> T4.12 · 6 个应急剧本 + 恢复演练记录。**照着敲就行**：每一条命令都在本机跑过，
> 每条路径都是仓库里真实存在的那个。

## 什么时候翻哪一本

| 你看到的现象 | 翻这本 |
| --- | --- |
| `studio db check` 报 `integrity_check` 失败 / 库打不开 / `MIGRATION_CHECKSUM_MISMATCH` | [`db_corrupt.md`](db_corrupt.md) |
| 总览台报警 `DISK_LOW`、写文件报 `[Errno 28] No space left on device`、`doctor` 拒绝启动 | [`disk_full.md`](disk_full.md) |
| `TTS_OOM`、显存打满、`tts` 进程反复重启、合成越来越慢 | [`tts_oom.md`](tts_oom.md) |
| `WORKER_RESTART_HALTED`（supervisor **停手**了，不再重启） | [`restart_storm.md`](restart_storm.md) |
| 任务长时间停在 `queued_voice` / `queued_render` / `voicing` / `rendering` 不动 | [`pipeline_stuck.md`](pipeline_stuck.md) |
| `PUBLISH_SELECTOR_MISS`、平台页面改版导致选择器失效 | [`publish_selector.md`](publish_selector.md) |
| 发布卡在 `pending` / `failed` / `manual_required`；`studio publish queue` 有待人工；任务 `completed` 但片子没发出去 | [`publish_stuck.md`](publish_stuck.md) |
| `PUBLISH_LOGIN_EXPIRED`、`health.hint` 说「需人工扫码登录」/「登录态已过期」、要加第二个账号 | [`publish_account.md`](publish_account.md) |
| 每月一次例行；或真要**回滚**数据 | [`restore_drill.md`](restore_drill.md) |
| CosyVoice 权重没了 / 换了机器要重装 / 合成报 `No module named 'matcha'`、`No module named 'hyperpyyaml'` | [`tts_models.md`](tts_models.md) |
| 想改 `voice.concurrency` / 怀疑并发开大了把卡拖慢 / 换了显卡要重新标定 | [`tts_concurrency.md`](tts_concurrency.md) |
| 面板上「缺密钥」、`studio llm probe` 报 `no_key`、想知道 Key 到底存在哪 | 见下「LLM 密钥存在哪」 |

## 三条通用纪律

1. **先看再动**。动手前把这两条的输出存一份（改完还能对比）：
   ```powershell
   . .\scripts\env.ps1
   uv run studio doctor --json | Out-File data\logs\_incident_doctor.json
   uv run studio service status
   ```
2. **动手前先备份**。回滚能力是唯一不能事后补的东西：
   ```powershell
   .\scripts\backup_db.ps1 -Overwrite
   ```
3. **留痕**。每次应急在对应剧本末尾的「本次记录」里补一行（时间 / 触发信号 / 动作 / 结果）。
   没有这一行，下次遇到同一个现象就得从零查起。

## LLM 密钥存在哪

**两处，环境变量优先**（`config/secrets.yaml` 从不入库）：

| 放哪 | 什么时候用 |
| --- | --- |
| WebUI **「设置」面板** → LLM 密钥 → 粘贴 → 保存 | 个人机器（默认用法） |
| 环境变量 `STUDIO_LLM_API_KEY` | 容器 / CI；**它会盖住面板里那份** |

改完**立刻生效**：网关每次调用现取一次密钥，不需要重启 API 或 4 个 worker。
面板上写着「环境变量优先」时，说明那份 Key 存在但没生效 —— 去清掉那个环境变量。

```powershell
uv run studio llm probe        # 逐通道探测（只发只读 GET，不产生一次计费调用）
```

## 一页速查

```powershell
. .\scripts\env.ps1                     # 所有运维动作之前先做这一步（R1：缓存/TEMP 必须落在非系统盘）
uv run studio doctor                    # 启动自检（阻塞项失败 ⇒ 退出码 1）
uv run studio service status            # 五进程台账 / 存活 / 端口
uv run studio pool status               # 各池 worker 心跳（存活 / 忙闲 / RSS / 当前 job）
uv run studio publish queue             # 待人工的发布（顺延 / 转人工 / 死信怎么分，见 publish_stuck.md）
uv run studio db check                  # 库自检（journal_mode / 外键 / 完整性 / 对象数 / 迁移）
uv run studio db backups                # 备份列表 + 最新一份距今几小时
uv run studio db vacuum                 # 只看：删行留下的空洞能还回多少（--yes 才动手）
.\scripts\backup_db.ps1                 # 热备一份（VACUUM INTO，不用停进程）
.\scripts\restore_db.ps1 -Force         # 恢复演练（还原到临时库 ⇒ db check ⇒ 抽查 3 张表）
.\scripts\gc_media.ps1 -DryRun          # 媒资回收**干跑**（一个字节都不动）
.\scripts\gc_media.ps1                  # 媒资回收（真的删）
.\tasks.ps1 check                       # 全量门禁（ruff + mypy + pytest）
```
