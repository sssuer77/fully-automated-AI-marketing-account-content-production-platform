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
去 `config/publish.yaml` 调 `daily_limit` / `min_gap_min`（改完**立刻生效**，不用重启：
`publish` worker 每条单元认领前重读一次配置），
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
| `PUBLISH_FAILED`（消息里写着"平台要求短信验证"） | **人的活**：面板上点「人工过验证」，窗口里自己输码（见下面那段）。**别点 `retry`** —— 它只会再弹一次同一个框 |
| `PUBLISH_UPLOAD_FAILED` / `PUBLISH_TIMEOUT` / `PUBLISH_UNKNOWN` | 先看 `evidence.stage` 与截图；多半可以直接 `retry` |
| `PUBLISH_DISABLED` | 见第 5 节 —— 这是**开关**，不是故障 |

### 「平台要求短信验证」这一档要单独说

它**不是故障**，是"自动这条路走完了、接下来等人"：平台在点下发布之后弹一个
「接收短信验证码」的框（真机 2026-09-23 · `acc_douyin`，要人输 `131*****40` 收到的码）。
**R13：不自动登录、也不替人过验证** —— 所以系统把这条转成 `manual_required`（不是 `failed`），
错误信息写成一句能照做的话，并把「标记已人工完成」打开。

**怎么过掉它：面板上点「人工过验证」**（`POST /api/v1/publish/{id}/assist`）。

```powershell
uv run studio publish queue --json     # 找到那条 manual_required 的 id
# 面板「待人工」区块上点「人工过验证」；命令行没有对应子命令（这一步必须在有窗口的机器上做）
```

点下去之后发生的事，按顺序：

1. **这台电脑上会弹出一个浏览器窗口**（服务在跑的那台机器 —— 不是你看面板的那台，如果是
   远程访问面板的话）；
2. 它把片子**重新上传一遍**、标题文案填好、点下发布 —— 然后**停在验证框前**；
3. **你去那个窗口里点「获取验证码」**，把手机上收到的六位数填进去；
4. 你输完，流程自己接着走完，库里那条记录自己变成 `published`（面板上不用再点任何东西）。

**验证码发到哪**：发到**这个账号绑定的手机**上（`acc_douyin` 是 `131*****40`）——
系统不知道也不会去读它。窗口里那个「获取验证码」**必须由你点**（R13：不替人过验证），
所以"码一直没来"这件事只有你能触发它、也只有你能判断。

几条**必须知道**的边界：

| 事 | 说明 |
| --- | --- |
| 等多久 | 服务端最多等 `MANUAL_VERIFY_WAIT_SEC`（**900 秒**，比等机器的 600s 长一个量级）。超时的文案分三种：框一直在（没输码 / 输错了）/ 框没了但没看到结果页（**先去平台确认它到底发出去没有**，别急着点重试）/ 都没有 |
| 为什么「重试」不行 | 平台问的是"这台机器 / 这个出口 IP 是不是你本人"，那是**一次性质询** —— 重试一百次只会弹一百次（真机实测四条失败记录四次弹同一个框） |
| 一次只能开一个 | 同一个账号同时只能开一个浏览器（登录态目录是独占的）。发布池**正在发这个号**时点它会被 422 拒绝 —— 到四池面板把 `publish` 池暂停掉再来（发完记得 unpause） |
| 被拒了先看那条作业 | 拒绝的依据是"`publish` 池里有这个账号的 `claimed` 作业"。**死掉的 worker 会留下孤儿行**（真机 2026-09-23：`publish#1@65928` 崩了之后那条作业挂了一小时还写着 `claimed`，而池里一个在跑的进程都没有）—— 租约已过期的认领**不算"正在发"**，服务会放行 |
| 孤儿认领怎么清 | **已经自动了**（2026-09-23 补）：每个池自己常驻清扫（`worker_base.SWEEP_INTERVAL_SEC=15s`，空转那一拍 + 进程启动强制一次），过期的认领要么回 `pending`、要么到上限转死信 `LEASE_EXPIRED`。想**立刻**收（不想等那 15 秒）：`uv run python -c "from studio.db import connect; from studio.db.queue import JobStore; print(JobStore(connect('data/studio.db')).reclaim_expired(pool='publish'))"`（`attempts` 到上限 ⇒ 转死信，否则回 `pending`）。**这是已知缺口**，该由 pool runner 每轮做 |
| 会不会重复发 | 这条路会**重新上传并点发布**。所以：等的时候**别在这一行上再点「重试」**，也别开第二个窗口发同一条。已发布（`published`）的记录走这条路会被直接拒绝（R14 不可逆） |
| 失败落在哪 | 人过完了仍然没发出去 ⇒ 回到 `manual_required`（**不交回队列**，三个按钮留在原地），错误信息换成这一趟的真实原因 |

> **它是同一个流程，不是旁路**：走的是同一个 `PlaywrightPublisher`、同一份选择器、同一条
> 八步；差别只有 `PublisherContext` 上两个开关（`headless=False` + `await_manual_verify=True`）
> 与"谁落库"（worker 走作业，这一条直接落 `publications`，**不碰作业队列**）。服务在
> `src/studio/services/publish_assist_service.py`，契约见 §04.6.2。

> **为什么不是"等它自己好"**：验证是**平台对这个账号 + 这台机器/这个出口 IP** 的一次性质询，
> `retry` 一百次也只会弹一百次。而换网络环境（换出口 IP）往往能让它不再弹 —— 那是**你**
> 才能决定的事。同理，**不要**为了让它过去而去动 `verify_marker`：删掉那条判据只是把
> "等人"改回"卡到 600s 超时"，屏幕上那个框一个字都不会少。

#### 别再去试"换个浏览器就能绕过它"（2026-09-23 试过了，不行）

真机实测（`acc_douyin`，同一条成片）：

| 启动方式 | `navigator.userAgent` |
| --- | --- |
| 默认（Playwright 自带 chromium） | `… HeadlessChrome/153.0.0.0 Safari/537.36` |
| `channel="chrome"`（本机真 Chrome） | `… HeadlessChrome/153.0.0.0 Safari/537.36` |
| `channel="chromium"` | `… HeadlessChrome/151.0.0.0 Safari/537.36` |
| **`headless=False`**（有窗口） | `… Chrome/153.0.0.0 Safari/537.36` ← **只有这一种不带 `Headless`** |

**UA 里那个 `Headless` 是 `headless=True` 造成的，与用哪个浏览器二进制无关** ——
所以 `channel="chrome"` 改不动它（改了之后重试，验证框**照样弹**，已实测）。
只有"真的开一个可见窗口"才会去掉它（开窗口不是伪装，改 UA 才是 —— 见下面那条 ⚠️）。

> ⚠️ **不要去改 UA**（`user_agent=…` 之类）。那是**反检测伪装**，越过 R13 的合规底线，
> 而且它只骗得过最粗的那一层判据 —— 真要伪装就得一路伪装下去，那不是这个系统要做的事。

## 4. 重试中：看退避，不要手动催

`pool_settings.backoff_base_ms`（出厂 60s）按 `attempts` 指数退避，最多 `max_attempts`（出厂 3）次。
到第 3 次仍失败 ⇒ **自动转人工**（第 3 节），不需要你盯着。

## 5. `PUBLISH_DISABLED`：出厂开关是关的

`config/publish.yaml` 的 `enabled: false` 是**出厂状态**（R14：发布不可逆）。
开关关着的时候，投递进来的作业会在 worker 那一侧带 `PUBLISH_DISABLED` **进死信** ——
**这不是故障**，是"你还没说要真发"。

> 为什么不是"待人工"：开关守卫跑在**建 `publications` 那一行之前**，所以发布面板上
> 不会出现任何记录，`GET /publish/queue` 也是空的。要看它去哪了：四池调度面板的
> `publish` 池，失败 / 死信那一栏。**投递面板现在会把这句话写在真平台选项旁边**（T5.10）。

```powershell
# 只想验证链路（不真发）：演练**不看**开关
uv run studio publish dry-run --task <task_id> --platform douyin --target fixture

# 真要发：改 config/publish.yaml 的 enabled: true —— **改完不用重启**
# （publish worker 每条单元认领前重读一次配置，见 publish_worker._refresh_config）
```

> ⚠️ **2026-09-23 之前不是这样**：那时 worker 钉着启动那一刻的快照，于是"面板上
> 配好了号、扫码也成功、投递也进了队列"却什么都没发出去 —— 而且**发布面板上连一条
> 记录都不会出现**（守卫跑在建 `publications` 那一行之前），只能靠猜。现在改完配置
> 下一条单元就生效；面板「账号配置」那一行的副标题也会把总开关的状态写出来。

> ⚠️ 发布进程**不在** `studio service start` 的五个进程里（裁定 274）：它随 T5.5 的发布面板
> 一起接进 supervisor。在那之前，**发布池要自己起**（上面那条命令）—— 没起的话，
> 投递进去的作业会一直停在 `pending`，面板上看不出原因。

## 6. 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
| 2026-09-23 | `publish` 池里 3 条死信；面板上「投递」提示已排入作业，然后什么都没发出去 | 重投 2 条（第 3 条 `01M2SQ3MMATZG1HGJ96EA3PKWA` 是 09-18 的老任务，盘上已无成片 ⇒ **不投**，投了只会再失败一次）；`config/publish.yaml` 的 `enabled` 翻 `true`；`publish_worker` 改成**每条单元认领前重读配置** | 2 条重新入队；真机一路跑到"平台要求短信验证" ⇒ `manual_required`，等人过验证（见第 3 节那段） |
| 2026-09-23 | 上面那条等人验证：面板上只有「重试 / 取消 / 标记已处理」，**没有地方输码**（窗口是无头的、跑完就关） | 加「人工过验证」：可见窗口 + `await_manual_verify=True`，八步不分叉，等的是**人**（`MANUAL_VERIFY_WAIT_SEC=900s`） | 面板上多一个按钮；窗口开在这台机器上，人输完码流程自己走完并落库。**真机未验**（会真发一条，R14 不可逆，要用户在场） |
| 2026-09-23 | 同一天：`publish` 池里一条作业挂着 `claimed` 一个多小时（`publish#1@65928` 崩了留下的孤儿），而池里一个在跑的进程都没有 | 认领守卫改成**忽略租约已过期的认领**（否则"上一次崩了"会变成"这一次不许你修"）；顺手把那行收掉（`reclaim_expired(pool='publish')` ⇒ 死信 `LEASE_EXPIRED`） | 那条记录可以走「人工过验证」了。⚠️ 当时 `publish` 池的孤儿认领**没有自动回收**（已知缺口）—— **同日晚已补**：每个池自己常驻清扫（15s + 启动强制一次），见 todolist §10.17 / 陷阱 234 |
