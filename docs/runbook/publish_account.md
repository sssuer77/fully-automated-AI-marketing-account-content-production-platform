# 剧本 · 登录态失效与多账号切换（R13 · T5.5）

> **触发信号**：`PUBLISH_LOGIN_EXPIRED`；面板上某条发布卡在 `manual_required`；
> `evidence.stage == "health"`；`health.hint` 是「需人工扫码登录」或「登录态已过期」；
> 或者更隐蔽的 —— 全流程没报错，但平台那边什么都没有。
> **前提**：登录态**只**住在 `data/browser_profile/<account_id>/` 里（§06.2.4）。
> 本系统**不自动登录、不绕过验证码**（R13 合规底线），所以"重新扫码"这一步
> 永远是人做的 —— 这本剧本只负责让你少点几下、别点错账号。

## 1. 先分清是哪一种"没登录"

两种提示长得很像，处置不同：

| `health.hint` | 意思 | 做什么 |
| --- | --- | --- |
| 需人工扫码登录 | 这个 `profile_dir` **从没登过**（新建账号 / 目录被清过） | 第 2 节：开一次可见浏览器扫码 |
| 登录态已过期 | 登过，但平台把会话作废了（过期 / 异地登录 / 平台改版） | 第 2 节，扫完再 `retry` |

```powershell
. .\scripts\env.ps1
uv run studio publish dry-run --task <task_id> --platform douyin --json
```

`--json` 里的 `health` 那一段就是答案（`ready` / `logged_in` / `last_check_at` / `hint`）。
**先看这一段再看别的**：登录态没过时，后面每一步的失败都是它的连锁反应，
先修前面那些等于白修。

## 2. 重新扫码（一次性人工动作）

```powershell
uv run studio publish dry-run --task <task_id> --platform douyin --show-browser
```

`--show-browser` 是**唯一**会把浏览器窗口显示出来的开关（其余时候一律无头）。
窗口起来之后：

1. 用**手机 App** 扫页面上的码；
2. 登录成功后**别关窗口**，让它自己走完流程（它会继续跑到第 ⑥ 步之前停下）；
3. 退出码 0 ⇒ 登录态已经写进 `data/browser_profile/<account_id>/`。

为什么必须走 `dry-run` 而不是单开一个浏览器：持久化 profile 是**按账号隔离的目录**，
用别的浏览器打开同一个目录不会写回我们认的那个 profile —— 看起来"登上了"，
下一次发布照样报过期。

> **不要**把 `data/browser_profile/` 拷给别人、也不要放进任何备份。
> 它等于这个账号的登录凭据（§02.5 明确：不得进入任何备份 / GC 流程）。

## 3. 扫完之后把那一条放出来

登录态修好**不会自动重试**已经转人工的那条 —— 那是刻意的：自动重试意味着
"平台一恢复就立刻发出去"，而人可能正想先看一眼内容。

```powershell
uv run studio publish queue
uv run studio publish retry --id <publication_id> --reason "重新扫码了"
```

面板上等价的三下：发布面板 →「待人工」→ 那一条的「重试」。
`retry` 会把 `attempt_count` **归零**（人工这一下是"给它一次完整的机会"），
并写一行 `audit_ops`。

如果决定**不发这一条**（内容过期了 / 平台那边已经手工发过）：

```powershell
uv run studio publish cancel      --id <publication_id> --reason "改期，改天重投"
uv run studio publish manual-done --id <publication_id> --reason "已在平台上手工发布"
```

`manual-done` 的 `--reason` **必填**（API 上会 422）。理由是三个月后唯一能回答
"这条当时为什么算了"的东西。

## 4. 多账号：加一个账号要动什么

结构上支持多账号（D1），出厂**只启用一个**（`acc_main`）。新增第二个账号：

```yaml
# config/publish.yaml
accounts:
  - account_id: acc_main
    platform: douyin
    profile_dir: data/browser_profile/acc_main
    enabled: true
    daily_limit: 3
    min_gap_min: 30
  - account_id: acc_second          # ★ 新增
    platform: douyin
    profile_dir: data/browser_profile/acc_second   # ★ 必须与其它账号不同
    display_name: 副账号
    enabled: true
    daily_limit: 2
    min_gap_min: 60
```

三条硬约束：

1. **`profile_dir` 不能重复**：两个账号共用一个目录 ⇒ 启动时就报
   `accounts 中 profile_dir 重复（登录态必须按账号隔离）`。共用目录的后果不是
   "发错账号"那么轻 —— 后登的那个会把前一个的会话顶掉，表现为**两个账号轮流失效**。
2. **限频按账号独立计数**（`daily_limit` / `min_gap_min` 各算各的）：
   一个账号被平台限流不会拖住另一个。
3. **幂等键含 `account_id`**：同一条片子投两个账号 ⇒ 两条 `publications`，
   互不阻塞（重复投**同一个**账号才叫重复）。

改完配置**不用迁移库**，然后给新账号扫一次码（第 2 节，`--account acc_second`）：

```powershell
uv run studio publish dry-run --task <task_id> --platform douyin --account acc_second --show-browser
```

### 4.1 发到指定账号

```powershell
uv run studio publish enqueue --task <task_id> --platform douyin --account acc_second
```

面板上：发布面板 → 投递时选账号（缺省 = 该平台第一个启用的账号）。
**默认永远是"该平台唯一启用的那个"**：多账号误配时宁可不发，也不猜。

### 4.2 一个账号失效，另一个照发

账号 A 登录态没了 ⇒ 只有 A 的作业转人工，B 的作业继续跑。
排查时按 `account_id` 分开看：

```powershell
uv run studio publish queue --json
```

`queue` 每条都带 `account_id` —— 看到 `PUBLISH_LOGIN_EXPIRED` 先确认是**哪一个**
账号，别顺手把另一个也停了。

## 5. 这一条为什么不做成自动的

| 想法 | 为什么不做 |
| --- | --- |
| 检测到过期就自动重新登录 | 自动登录必然要存密码或绕过验证码 —— 越过了 R13 的合规底线 |
| 检测到过期就无限重试 | 平台会把"反复失败登录"当成风控信号，代价是**整个账号**被限 |
| 过期时自动换另一个账号发 | 账号是**品牌身份**，不是可替换的出口。换账号发出去的东西归属就变了 |
| 扫完码自动把待人工那几条全放出去 | 人可能正想先看一眼内容 / 改个标题。自动放行等于把确认权交回给程序 |

## 本次记录

> 每次应急在下面补一行（时间 / 触发信号 / 动作 / 结果）。没有这一行，
> 下次遇到同一个现象就得从零查起。

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
| — | — | — | — |