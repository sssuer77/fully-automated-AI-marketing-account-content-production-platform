# 剧本 · 平台选择器失效（R13 · T5.2）

> **触发信号**：`PublishResult.error_code == 'PUBLISH_SELECTOR_MISS'`；日志里出现
> `没有找到选择器 …`；`studio publish dry-run` 在真平台上停在第 ①②③④ 步之一；
> 面板上某条作品卡在 `uploading`；或者更隐蔽的 —— **流程全绿，但平台那边什么都没收到**。
> **前提**：选择器**只**住在 `src/studio/publish/selectors/<platform>.yaml` 里（§06.5.2）。
> 改这个剧本**不需要改一行代码**，也不需要重新跑 `check`。

## 1. 先分清是哪一种"发不出去"

四种收场长得很像，处置完全不同。**按 `error_code` 对号入座**，不要凭感觉：

| `error_code` | 意思 | 翻哪一节 |
| --- | --- | --- |
| `PUBLISH_SELECTOR_MISS` | 选择器**没命中**（页面改版 / 选择器写错） | 本文档第 2–4 节 |
| `PUBLISH_LOGIN_EXPIRED` | 登录态没了 —— 选择器是好的，页面是登录页 | `publish_account.md`（T5.5） |
| `PUBLISH_UPLOAD_FAILED` | 文件选中了但上传没完成 | 本文档第 5 节（超时与进度元素） |
| `PUBLISH_UNKNOWN` | 异常从没预期的地方冒出来 | 看 `evidence.stage` + traceback，**不是**选择器的事 |

```powershell
. .\scripts\env.ps1
uv run studio publish dry-run --task <task_id> --platform douyin --json
```

`--json` 的输出里有 `evidence.stage` 与 `evidence.screenshot_path`。**先看图**：
截图拍在失败那一刻，绝大多数时候一眼就能看出"页面变了"还是"我们点错了地方"。

## 2. 改选择器（改 yaml，不改代码）

打开 `src/studio/publish/selectors/<platform>.yaml`。骨架如下 ——
**这些键的名字是契约，值才是你要改的东西**：

```yaml
platform: douyin                 # 必须与文件名一致，否则装不起来
version: "2026-09-16.1"          # ★ 改完**必须**顺手改这里（见第 3 节）

urls:
  upload: "https://…"            # 八步第 ① 步打开的页面
  manage: "https://…"            # health() 探测登录态用的页面

selectors:
  # ── 必需（缺一个就装不起来 ⇒ 当场 PUBLISH_SELECTOR_MISS）──
  login_ok: "…"                  # 已登录的标志元素
  login_required: "…"            # 未登录的标志元素
  upload_input: "…"              # 文件输入框（第 ② 步）
  title_input: "…"               # 标题输入框（第 ③ 步）
  publish_button: "…"            # 发布按钮（第 ⑥ 步，dry-run **不点**）
  # ── 可选（缺了只是少做一步，不该拦下整条链路）──
  caption_input: "…"             # 文案框；标题与文案同一个框时可以不写
  upload_progress: "…"           # 上传进度元素
  cover_trigger: "…"             # 打开"设置封面"弹层
  cover_input: "…"               # 弹层里的图片输入框
  success_marker: "…"            # 结果页标志
  reject_marker: "…"             # 审核不通过标志

readback:                        # 回读怎么取值：value = input.value，text = innerText
  title: value
  caption: text

markers:                         # 页面上的**中文提示语**（不是选择器）
  login_expired_text: ["登录已过期", "请重新登录", "扫码登录"]
  review_rejected_text: ["审核不通过", "内容违规", "未通过审核"]
```

**为什么"必需"和"可选"要分开**：必需项缺了 = 这条链路根本走不完，
装起来再在真机上以 `PUBLISH_SELECTOR_MISS` 收场，等于把"配置写错了"推迟成"发不出去"；
可选项缺了 = 少做一步（比如平台把标题与文案合并成一个框），拦下整条链路是过度反应。

**`markers` 为什么不是选择器**：`login_expired_text` 与 `login_required` 回答的是
两个不同的问题 —— "从没登录过"（去扫码）与"登录过但过期了"（重新扫码）。
揉成一个的话，面板上给的操作员建议会有一半是错的。

## 3. 改完顺手把 `version` 改掉

`version` 会被写进 `publications.evidence_json`（§06.5.3）。它存在的**唯一**意义是
事后能回答"这条是哪个版本的选择器发的"。不改的话，页面改版前后的两条记录长得一模一样，
而"到底是改版前还是改版后"正是你要查的那件事。

命名约定：`<日期>.<当天第几次>`，例如 `2026-09-16.1`。

## 4. 改完怎么验（**先靶页，再真机**）

```powershell
# ① 靶页：证明"改了 yaml 没把流程改坏"（不需要账号、不需要网络）
uv run studio publish dry-run --task <task_id> --platform douyin --target fixture

# ② 真机：证明"新选择器在真页面上命中"（需要**已登录**的账号）
uv run studio publish dry-run --task <task_id> --platform douyin
```

第 ① 条跑通只说明 yaml 语法与必需键齐了 —— **靶页用的不是你的选择器值**，
它有自己的 `fixture.yaml`。所以**第 ② 条不能省**。

真机上你要看到的是：退出码 0（或"尚未登录"这种如实报告），
截图落在 `data/work/<task_id>/publish/<platform>/…_06-before-publish.png`，
而且**发布按钮一次都没被点**（dry-run 停在第 ⑥ 步之前）。

> ⚠️ **`--show-browser` 是排障用的**：加上它浏览器会**可见**，你能看着它一步步走。
> 排查选择器时这是最快的办法；但别在无人值守的时段用它。

## 5. 上传卡住（`PUBLISH_UPLOAD_FAILED`）

先确认是"选择器没命中"还是"命中了但没传完"：

- 截图里**文件已经选中**（输入框旁边有文件名）⇒ 是上传没完成 ⇒ 调
  `playwright_publisher.py` 的 `upload_timeout_sec` / `upload_progress` 选择器。
- 截图里**文件压根没选中** ⇒ 还是选择器的事 ⇒ 回到第 2 节。

**不要**为了让它过去而把超时调到很大：真机上"传得慢"与"传挂了"必须能分开，
把超时调到 10 分钟只是让失败来得更晚。

## 6. 回滚

选择器是**纯配置**，回滚就是改回上一个 `version` 的那份值：

```powershell
git diff src/studio/publish/selectors/<platform>.yaml
git checkout -- src/studio/publish/selectors/<platform>.yaml
```

改回之后 `version` 也要改回去 —— 否则记录里会出现"版本号说是新的、实际跑的是旧的"。

## 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
| — | — | — | — |
