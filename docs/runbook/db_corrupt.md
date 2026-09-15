# 剧本 · 数据库损坏 / 备份不可用

> **触发信号**：`studio db check` 报 `db.integrity_check` fail、`database disk image is malformed`、
> `MIGRATION_CHECKSUM_MISMATCH`、`studio db migrate` 中途失败。
> **优先级**：最高（全站唯一的真相源就是它）。

## 1. 先分清是哪一类

```powershell
. .\scripts\env.ps1
uv run studio db check --json            # 逐项：journal_mode / 外键 / 完整性 / 对象数 / 迁移
uv run studio db status                  # 迁移版本与校验和（磁盘文件 vs 库内登记）
```

| 报什么 | 是什么 | 怎么处理 |
| --- | --- | --- |
| `db.journal_mode` fail | 连接级 PRAGMA 没生效 | 检查 `src/studio/db/pragmas.sql` 是否被改；重跑 `db check` |
| `db.foreign_key_check` 有行 | 有孤儿行 | 先留档（截图 + `--json`），再按下面第 2 节还原 |
| `db.integrity_check` fail | **真损坏** | 按下面第 2 节还原 |
| `db.tables` / `db.indexes` / `db.triggers` 数量不符 | 少建 / 多建对象 | 先 `db status` 看是不是迁移没跑完；别手写 DDL 补 |
| `MIGRATION_CHECKSUM_MISMATCH` | **有人改了已应用的迁移文件** | 这是设计红线（§03.7.2 只增不改）。恢复被改的文件，或从备份还原 |
| `db.migrations` 有 pending | 只是没跑迁移 | `uv run studio db migrate`（会自动钉一份迁移前检查点） |

## 2. 从备份还原（正确顺序）

**必须停进程**：带着在跑的 worker 换库文件 = 旧库 + 新 WAL，两个故障一起造。

```powershell
.\停止.bat                                        # ① 停全部进程，确认 data\logs\*.pid 都没了
Copy-Item data\studio.db data\studio.broken.db    # ② 坏库留档（取证用，别删）
uv run studio db backups                          # ③ 挑一份（越新越好，但**先验再用**）
uv run studio db restore --from data\backups\studio_20260910.db --to data\restore_check.db --force
uv run studio db check data\restore_check.db      # ④ 这一步过了再往下走
```

第 ④ 步通过后：

```powershell
Move-Item data\studio.db data\studio.replaced.db
Copy-Item data\backups\studio_20260910.db data\studio.db
Remove-Item data\studio.db-wal, data\studio.db-shm -ErrorAction SilentlyContinue
uv run studio db migrate                          # ⑤ 补迁移（还原出来的库必然落后）
uv run studio db check                            # ⑥ 8 项全 ok
.\启动.bat
uv run studio service status
```

细节与检查单见 [`restore_drill.md`](restore_drill.md) 第 4 节。

## 3. 备份也不可用时

```powershell
uv run studio db backups                          # 一份一份往早里试
uv run studio db restore --from <更早的一份> --to data\restore_check.db --force
```

- **还有更早的**：重复第 2 节，只是会丢掉中间那几天的数据。丢多少要写进本次记录。
- **一份都不行**：说明备份任务**一直没产出**（不是"今天坏了"）。这时唯一的数据来源是
  `data/backups/checkpoints/premigrate_*.db`（迁移前检查点，不参与轮转）。
  仍然不行 ⇒ 从零重建：`uv run studio db migrate` 建新库，然后把
  `data/output/`（成片 / 封面 / 稿件）、`data/work/*/script.json|timeline.json|manifest.json`
  这些**永久保留的交付物**重新登记回库。媒资没有丢，丢的是"库里的账"。

## 4. 复盘时要回答的三个问题

1. **备份为什么没产出？**（计划任务没建 / 退出码没人看 / 磁盘满 —— 见 `disk_full.md`）
2. **`db check` 多久没跑过？**（它是启动门禁的一部分，跑不到说明启动路径被绕过了）
3. **还原演练上一次通过是什么时候？**（`restore_drill.md` 第 3 节那张表）

## 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
|  |  |  |  |
