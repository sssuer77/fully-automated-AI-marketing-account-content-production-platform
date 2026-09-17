# 剧本 · 发布卡住 / 待人工（R13 · T5.3）

> **触发信号**：面板上某条记录停在 `queued` / `uploading` / `failed`；
> `GET /api/v1/publish/queue` 里冒出一条待人工；`studio publish queue` 有输出；
> 或者更常见的一种 —— **任务明明 `completed`，片子却没发出去**。
> **前提**：发布**失败不回退任务状态**（§06.5.4）。任务停在 `completed` 是对的，
> 成片仍然有效，随时可以下载后人工发 —— 不要为了"让它动起来"去改任务状态。

## 1. 先分清是哪一种"没发出去"

四种收场长得很像，处置完全不同。**看 `error_code` 与作业状态对号入座**：

| 现象 | 库里长什么样 | 意思 | 翻哪一节 |
| --- | --- | --- | --- |
| **顺延**（不是故障） | 作业 `pending` + `error_code=PUBLISH_RATELIMIT` + `not_before` 在未来；`publications` 里**没有**这一条 | 今天这个账号的额度用完了 / 距上次发布不足 30 分钟 | 第 2 节 |
| **待人工** | `publications.status='manual_required'` + `error_code` | 自动这条路走完了，等人做决定 | 第 3 节 |
| **重试中** | `publications.status='failed'`，作业 `pending`（退避中） | 网络抖了 / 上传断了，队列会自己再试 | 第 4 节 |
| **死信** | 作业 `status='dead'` | 重试次数用完（或撞上不可重试的错误码） | `restart_storm.md` + 第 3 节 |

```powershell
. .\scripts\env.ps1
uv run studio publish queue                       # 六状态计数 + 待人工那几条（= GET /api/v1/publish/queue）
uv run studio publish queue --json                # 带 evidence（截图 / DOM 快照路径）
uv run studio pool status                         # 各池心跳：publish 池有没有 worker 在
```

⚠️ **先确认发布进程在跑**：`publish` **不在** `studio service start` 的五个进程里（裁定 274），
在那之前要自己起（第 5 节）。没起的话，投递进去的作业会一直停在 `pending` ——
而看板上什么都看不出来，因为记录是**发布那一刻**才建的。

## 2. 顺延：**什么都别做**

额度是**按本地日**清零的，间隔是 30 分钟。作业会在 `not_before` 之后自己回来，
而且**顺延不消耗重试次数**（`JobStore.defer` 把认领时加的那一次退了回去）——
所以它永远不会因为"等了三天"而进死信。

**顺延的那一条在看板上看不到** —— 它还没建 `publications` 记录（记录是"发布那一刻"的事实）。
它此刻只存在于 `jobs` 表里，这是正常的：顺延的意思是"还没轮到"。要看 `not_before`：

```powershell
uv run python -c 'import sqlite3
for row in sqlite3.connect("data/studio.db").execute("SELECT task_id,status,attempts,error_code,not_before FROM jobs WHERE pool = ''publish''"):
    print(row)'
```

读法是：`status='pending'` + `attempts=0` + `error_code='PUBLISH_RATELIMIT'` + `not_before` 在未来
⇒ 它在**排队等额度**，不是失败了。

**唯一的例外**：如果你根本不打算发这么多条，那不是顺延的问题，是**投多了** ——
去 `config/publish.yaml` 调 `daily_limit` / `min_gap_min`（改完重启 `publish` worker 生效），
或者直接把不要的那几条 `cancel` 掉（第 3 节）。

## 3. 待人工：三个动作，各改什么

```powershell
uv run studio publish retry       --id <publication_id> --reason "重新扫码了"
uv run studio publish cancel      --id <publication_id> --reason "这条不发了"
uv run studio publish manual-done --id <publication_id> --reason "已到平台手工发出"   # --reason 必填
```

| 动作 | 库里改了什么 | 什么时候用 |
| --- | --- | --- |
| `retry` | 记录回 `queued` + `attempt_count` **归零** + 作业重排（`requeue_unit`） | 你**修好了**原因（重新扫码 / 改完选择器 / 平台恢复了） |
| `cancel` | 记录 `canceled` + 作废还没被认领的作业 | 这条**不再发**（内容过期 / 平台不要了）。已发出去的**取消不了** |
| `manual-done` | 记录 `finished_at`（状态仍是 `manual_required`） | 你已经**到平台上手工发完了** —— 记账用，不改变"这条是怎么发出去的" |

`retry` 会**同时改两处**（业务表 + 作业表）：只改业务表的话，那条作业一辈子只有一条，
`manual_required` 之后没有任何 worker 会再看它一眼 —— 症状是"点了重试没反应、不报错"（陷阱 #115）。

**按 `error_code` 决定先做什么**：

| `error_code` | 先做什么 |
| --- | --- |
| `PUBLISH_LOGIN_EXPIRED` | 重新扫码登录（`publish_account.md`，T5.5）⇒ 再 `retry` |
| `PUBLISH_SELECTOR_MISS` | 修选择器（`publish_selector.md`）⇒ 再 `retry` |
| `PUBLISH_REVIEW_REJECTED` | 平台审核不通过 —— **改内容**再投一条，`retry` 只会再撞一次 |
| `PUBLISH_UPLOAD_FAILED` / `PUBLISH_TIMEOUT` / `PUBLISH_UNKNOWN` | 先看 `evidence.stage` 与截图；多半可以直接 `retry` |
| `PUBLISH_DISABLED` | 见第 5 节 —— 这是**开关**，不是故障 |

## 4. 重试中：看退避，不要手动催

`pool_settings.backoff_base_ms`（出厂 60s）按 `attempts` 指数退避，最多 `max_attempts`（出厂 3）次。
到第 3 次仍失败 ⇒ **自动转人工**（第 3 节），不需要你盯着。

## 5. `PUBLISH_DISABLED`：出厂开关是关的

`config/publish.yaml` 的 `enabled: false` 是**出厂状态**（R14：发布不可逆）。
它开着的时候，投递进来的作业会在 worker 那一侧直接转人工并带上 `PUBLISH_DISABLED` ——
**这不是故障**，是"你还没说要真发"。

```powershell
# 只想验证链路（不真发）：演练**不看**开关
uv run studio publish dry-run --task <task_id> --platform douyin --target fixture

# 真要发：改 config/publish.yaml 的 enabled: true，然后重启 publish worker
uv run python workers/run_publish.py
```

> ⚠️ 发布进程**不在** `studio service start` 的五个进程里（裁定 274）：它随 T5.5 的发布面板
> 一起接进 supervisor。在那之前，**发布池要自己起**（上面那条命令）—— 没起的话，
> 投递进去的作业会一直停在 `pending`，面板上看不出原因。

## 6. 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
| | | | |
