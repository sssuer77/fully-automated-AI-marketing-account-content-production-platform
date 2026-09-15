# 剧本 · 恢复演练与真回滚（§03.7.4）

> **对象**：`data/backups/studio_YYYYMMDD.db`（日备 7 份 + 周备 4 份）。
> **频率**：**每月一次**。**耗时**：约 5 分钟。**要不要停进程**：演练不用停；真回滚必须停。

## 0. 演练在回答什么问题

只有两个：

1. **这一份备份打得开吗？**（不是"文件在不在" —— 磁盘上有备份与备份可用是两件事）
2. **打不开的话，是这一份坏了，还是备份一直没产出？**

演练**不是**在回答"现在要不要回滚"。这两件事的工具是分开的：`restore_db.ps1` 拒绝把备份
还原到活库上（`RESTORE_REFUSED_LIVE_DB`），真回滚走下面第 4 节的手工流程。

## 1. 月度演练（不动活库）

```powershell
. .\scripts\env.ps1

# ① 先看盘上的备份新不新（age_hours > 48 就先查计划任务，别急着演练）
uv run studio db backups

# ② 还原到临时库 ⇒ db check ⇒ 抽查 3 张表行数
.\scripts\restore_db.ps1 -Force

# ③ 想要机读结果（留痕 / CI）
.\scripts\restore_db.ps1 -Force -Json
```

`-Force` 是覆盖上一次演练的临时库（`data/backups/restore_drill/studio.db`）。
**它永远不碰 `data/studio.db`。**

## 2. 判定标准（读那三行）

| 输出行 | 怎么读 |
| --- | --- |
| 行数抽查 | `tasks=… jobs=… system_logs=…`。三张表**都在**才算抽查做全了；表缺失 ⇒ 记 `skipped` 并判定失败（"验了三张表"这句话必须真的验过） |
| 活库基线失败项 | 活库**自己**有哪些失败项。（无）＝ 活库全绿 |
| 备份版本落后 | 备份比活库旧（刚上过迁移）。**不是**故障；回滚时补跑一次 `studio db migrate` 即可 |
| 备份自己的问题 | **这一行才是结论**。非空 ⇒ 备份**不可用**，退出码 1 |

一句话：**「备份自己的问题」为空 = 演练通过**。活库自己欠的账、备份自己老了几版，都不算它的问题。

## 3. 演练记录（每月填一行）

| 日期 | 执行人 | 备份文件 | 备份日期 | tasks | jobs | system_logs | db check | 备份版本落后 | 备份自己的问题 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-14 | 施工（T4.12 收尾） | `data/backups/studio_20260914.db` | 2026-09-14 | 0 | 0 | 0 | 有失败项 | `db.indexes` | （无） | ✅ 通过 |
|  |  |  |  |  |  |  |  |  |  |  |

> 第一次演练时活库已迁到 `0008`、而当日备份是迁移前产的 ⇒ 「备份版本落后 = `db.indexes`」。
> 这正是这一列存在的意义：把"备份旧了"与"备份坏了"分开写。

## 4. 真要回滚时的检查单（**停进程 ⇒ 换文件 ⇒ 自检 ⇒ 起进程**）

> 顺序不能换。**带着在跑的进程换库文件**，等于同时制造两个故障：旧库 + 新 WAL。

- [ ] ① 通知：确认没有正在跑的任务（总览台看队列；`uv run studio pool status` 全空闲）
- [ ] ② 停全部进程：`.\停止.bat`（或 `uv run studio service stop`），确认 `data/logs/*.pid` 都不在了
- [ ] ③ **备份当前库**（哪怕它已经坏了 —— "坏库 + 好备份"才能做取证）：
      ```powershell
      Copy-Item data\studio.db data\studio.broken.$(Get-Date -Format yyyyMMdd-HHmmss).db
      ```
- [ ] ④ 记下要回滚到哪一份（日期 / 大小）：
      ```powershell
      uv run studio db backups
      ```
- [ ] ⑤ 把备份**还原到临时路径**并验一遍（不要直接覆盖活库）：
      ```powershell
      uv run studio db restore --from data\backups\studio_20260910.db --to data\restore_check.db --force
      ```
- [ ] ⑥ 验过之后再换文件：把 `data\studio.db` 改名留档，把备份复制成 `data\studio.db`
- [ ] ⑦ **删掉 `data\studio.db-wal` 与 `data\studio.db-shm`**（旧 WAL 配新主文件 = "能打开但对不上"）
- [ ] ⑧ 迁移到当前版本：`uv run studio db migrate`（回滚到旧库之后，schema 必然落后）
- [ ] ⑨ 自检：`uv run studio db check` ⇒ 8 项全 ok
- [ ] ⑩ 起进程：`.\启动.bat`；`uv run studio service status` ⇒ `api` ready
- [ ] ⑪ 总览台看一遍：任务列表、最近日志、池状态
- [ ] ⑫ 留痕：写 `audit_ops`（WebUI 操作台），并在本文件第 3 节补一行

## 5. 不要做的事

- **不要**用 `Copy-Item` 备份正在跑的库：WAL 下库的真身分散在 `studio.db` + `studio.db-wal`，
  复制出来的是"两个瞬间的拼盘"（`VACUUM INTO` 才是对的，`backup_db.ps1` 已经这么做了）。
- **不要**把备份还原到活库上：`restore_db.ps1` 会拒绝（`RESTORE_REFUSED_LIVE_DB`），
  这不是限制，是保护 —— 一个能被误触的"还原"按钮迟早会在某个手滑的下午抹掉当天的工作。
- **不要**因为演练红了就调低判据：先换更早的一份重试，确认是"这一份坏了"还是"备份一直没产出"。
- **不要**在演练之外删除 `data/backups/checkpoints/`：那是迁移前那一刻的库，
  是出事时唯一能回去的点（它**不参与**日备轮转，就是为了不被顺手删掉）。

## 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
|  |  |  |  |
