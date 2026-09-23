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
version: "2026-09-23.4"          # ★ 改完**必须**顺手改这里（见第 3 节）
calibrated: true                 # ★ 这份 pack 有没有在真机上逐条验过（见 §2.2）
calibrated_at: "2026-09-23"      # `calibrated: true` 时**必填**（装的时候会拒）
known_gaps:                      # 已经**确认**没做的 —— 面板会照实显示出来
  - "数据回收那五条没验过：要一条**真发出去过**的作品才验得了"

urls:
  upload: "https://…"            # 八步第 ① 步打开的页面
  manage: "https://…"            # 数据回收读的那一页（也是"页面跳走式成功"的判据）

selectors:
  # ── 必需（缺一个就装不起来 ⇒ 当场 PUBLISH_SELECTOR_MISS）──
  login_ok: "…"                  # 已登录的标志元素
  login_required: "…"            # 未登录的标志元素
  upload_input: "…"              # 文件输入框（第 ② 步）
  title_input: "…"               # 标题输入框（第 ③ 步）
  publish_button: "…"            # 发布按钮（第 ⑥ 步，dry-run **不点**）
  # ── 可选（缺了只是少做一步，不该拦下整条链路）──
  caption_input: "…"             # 文案框；标题与文案同一个框时可以不写
  upload_progress: "…"           # 上传进度元素（判"传完了没有"，见 §5）
  cover_trigger: "…"             # 打开"设置封面"弹层
  cover_input: "…"               # 弹层里的图片输入框
  dismiss_overlay: "…"           # 压在发布按钮上的说明弹层（点一下关掉它）
  success_marker: "…"            # 结果页标志（`:text-is()`，**不是** `:text()`）
  reject_marker: "…"             # 审核不通过标志
  verify_marker: "…"             # 平台要求短信 / 人脸验证的框 ⇒ 转人工（R13）
  # ── 数据回收（T5.4）：验它们要一条**真发出去过**的作品 ──
  metric_row: '…{post_id}…'      # `{post_id}` 是**必填占位符**（见 §2.1）
  metric_views: "…"
  metric_likes: "…"
  metric_comments: "…"
  metric_shares: "…"
  metric_completion_rate: "…"    # 完播率是**比率**，解析器与上面四个计数不同

readback:                        # 回读怎么取值：value = input.value，text = innerText
  title: value
  caption: text

markers:                         # 页面上的**中文提示语**（不是选择器）
  login_expired_text: ["登录已过期", "请重新登录"]
  review_rejected_text: ["审核不通过", "内容违规", "未通过审核"]
  success_url_contains: ["…"]    # 第 ⑦ 步的**第二个**成功判据（平台把页面跳走）
```

**为什么"必需"和"可选"要分开**：必需项缺了 = 这条链路根本走不完，
装起来再在真机上以 `PUBLISH_SELECTOR_MISS` 收场，等于把"配置写错了"推迟成"发不出去"；
可选项缺了 = 少做一步（比如平台把标题与文案合并成一个框），拦下整条链路是过度反应。

**`markers` 为什么不是选择器**：`login_expired_text` 与 `login_required` 回答的是
两个不同的问题 —— "从没登录过"（去扫码）与"登录过但过期了"（重新扫码）。
揉成一个的话，面板上给的操作员建议会有一半是错的。

⚠️ **`login_expired_text` 里不许出现"扫码登录"**：那是**登录页自己的按钮文案**，两种状态
下都在页面上 —— 拿它当判据等于永远判成"过期"（陷阱 #212）。装配期会直接拒（见 §2.1）。

**`success_marker` 与 `success_url_contains` 至少要有一个**：有的平台不发"发布成功"这几个字，
而是把页面**跳走**（抖音落到内容管理列表）。只认元素的话，一条**已经发出去**的内容会在 600 秒
之后被记成失败 —— 而那时人已经在平台上看到它了（陷阱 #228）。两个都空 ⇒ 装配期直接拒。

**`calibrated` / `known_gaps` 也是数据，不是注释**（见 §2.2）：注释不拦人、也不会出现在面板上，
而"这份 pack 到底验过没有"正是操作员决定"今天要不要拿它真发一条"时唯一要紧的信息
（发出去就收不回来了 · R14）。

### 2.1 七条装配期校验：写错当场拒，不等真机超时

真机 2026-09-23 连着踩了**三条都表现为"发布成功也判不出来、一路等到 600s 超时"**的坑
（陷阱 #226 / #227 / #229），后来又补上四条**"装得起来、但判据必然失灵"**的。它们的共同点是：
**yaml 语法是合法的、Playwright 也收下了，错的是"这条选择器描述的是哪一件事"** —— 而调用方
把异常吞成"元素不在"，于是配置写错在现场看起来像"平台改版了"，唯一的线索是 600 秒之后那张截图。

这七条在**装选择器的时候**就拒（`load_selector_pack` ⇒ `PUBLISH_SELECTOR_MISS`），
不用等到真机上耗掉 600 秒。前三条拦的是**写法**：

| 拦住什么 | 判据 | 为什么 |
| --- | --- | --- |
| **引擎前缀拼进列表** | 值里**有逗号** + 出现 `text=` / `xpath=` / `css=` / `id=` / `data-testid=` / `role=` / `nth=` | `text=文案` 只在**单独**出现时才合法；用逗号拼上第二个选择器，整条就走 CSS 解析器 ⇒ `Unexpected token "=" while parsing css selector`（陷阱 #226） |
| **`publish_button` 用包含匹配** | 值里出现 `:has-text(` 或 `text=` | 导航项「作品发布」在文档序里排在真按钮**前面**，包含匹配会命中它 —— 点下去只是切页面、表单被重置成"你还有上次未发布的视频"（陷阱 #227） |
| **结果页标志用 `:text()`** | `success_marker` / `reject_marker` / `verify_marker` 的值里出现 `:text(` | `:text()` 是**子串**匹配，而平台上总有一句包含这几个字的普通提示（「视频发布成功**后**，价格将无法更改」）⇒ 在**发布之前**就判成成功（陷阱 #229） |

后四条拦的是**判据** —— 它们拦下的是"装得起来、但一定会判错"的 pack：

| 拦住什么 | 判据 | 为什么 |
| --- | --- | --- |
| **"登录已过期"里混进了两种状态都在的文案** | `markers.login_expired_text` 里出现 `扫码登录` | 那是**登录页自己的按钮文案**：一个**从没登录过**的号也会命中它，于是面板报"登录态已过期，需人工重新扫码登录"，而真相是"这个号从来没登录过"。两句给的操作员动作**正好相反**（陷阱 #212） |
| **两个成功判据都空** | `success_marker` 与 `markers.success_url_contains` **同时**为空 | 第 ⑦ 步会一路等到 600s 超时 —— 而"平台其实收下了"与"真的没发出去"在这条路径上长得一模一样（陷阱 #228） |
| **`metric_row` 少了 `{post_id}`** | `metric_row` 有值但不含 `{post_id}` | 四个计数是**相对它**取的后代选择器：少了占位符就会命中**第一条**作品那一行 —— 于是"这一条的数据"读的是别人的数，而面板上那两个数字都长得像正常的数（T5.4） |
| **说"校准过"却没说哪天** | `calibrated: true` 而 `calibrated_at` 为空 | "校准过"与"什么时候校准的"是同一件事的两半：平台会改版，一份很久以前的校准记录与没校准几乎一样没用（面板那一列就是给人看这个的） |

要用文案就写 **`:text-is('文案')`**（**全等**，可以安全地跟在逗号后面），例如
`success_marker: "div.success-page, :text-is('发布成功')"`。

⚠️ **别为了让它装起来而绕开校验**（把逗号拆成两个键、或者把文案删掉）：那正是这几条要拦的
东西。拆开写只会让判据**永远不成立**，症状还是 600 秒超时。

### 2.2 校准状态：`calibrated` / `calibrated_at` / `known_gaps`

装配期判得了"**结构上**能不能跑"（上面那七条），判不了"这些 CSS 值在真页面上对不对" ——
后者只有一个办法：**到真页面上逐条问一遍**。

```powershell
. .\scripts\env.ps1
uv run studio publish calibrate --platform xiaohongshu     # 开一个可见窗口，逐条问
```

它**只读**：不点发布、不填输入框、不往平台上送任何东西（R13 / R14），唯一的写操作是往
`data/work/calibrate/<platform>/` 落一张排障截图。输出是一张表：每条选择器**命中几个**、
以及"这一条期望命中还是期望不命中"。

它跑**三轮**，其中第一轮最容易漏：

| 轮次 | 在哪儿 | 问什么 |
| --- | --- | --- |
| ① 未登录页 | 一份**空的** profile（`data/browser_profile/_calibrate`） | `login_required` **必须**命中、`login_ok` **必须不**命中 —— 陷阱 #212 的另一半，只测已登录那一侧**测不出来** |
| ② 创作页 | 该账号的 profile | 八步要用的那几条（必选键期望命中，可选键只报个数） |
| ③ 管理页 | 同上 | 数据回收那一组（`metric_row` 要一条真作品号 ⇒ 跳过；`success_url_contains` 只能真发一条时验） |

**没登录就立刻收手**：创作页的表单没渲染出来时，`upload_input` / `title_input` /
`publish_button` 全是"命中 0 个"，而那与"这三条选择器写错了"在读数上**一模一样** ——
照着改，改的是三条其实好好的选择器。所以这一轮只报 `login_ok`，其余全部标成"这轮问不了"，
并提示先去面板上扫码登录（T6.4）。

校完之后，**要人手写回三个字段**（探针不改文件）：

```yaml
calibrated: true
calibrated_at: "2026-09-23"
known_gaps: ["……还有哪一段流程压根没写……"]
```

- `calibrated` / `calibrated_at` 决定面板上"校准"那一列（`calibrated: true` 而不写日期，
  装配期会拒）。平台会改版，所以**日期是这一列的一半**。
- `known_gaps` 回答的是**另一件事**："这个平台还有没有一段流程压根没写"（B 站的必选分区
  就是）。只写"未校准"会让人以为"校准完就能发了" —— 两件事都得让操作员看见。

七份 pack 现在的状态：**只有抖音是 `calibrated: true`**，其余六份的 CSS 是照着它的形状猜的
（面板上会显示 ⚠️）。**"猜的"不等于"不能用"**：投递与演练都不拦它，但它大概率发不出去 ——
所以拿它真发之前先跑一次校准。

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

### 4.1 登录态那一组要在**两种状态**下各验一次

只验一侧的判据，另一侧就是一个**安静的错值**。2026-09-23 真机上就是这么翻的车：
`login_ok` 与 `login_required` 在"已登录"和"未登录"**两侧都不存在**，于是
`_logged_in()` 恒为 False —— 人扫完码、平台已经把页面换成创作中心，系统仍然报
"没扫到"，一路等到 180 秒超时（陷阱 #212）。**两个号的 `sessionid` 都写进 profile 了，
而两次都被报成失败。**

三种状态都要过一遍：

| 状态 | 怎么造 | 期望 |
| --- | --- | --- |
| **已登录** | 面板 → 那一行的「检测登录态」（等价：`--show-browser` 跑一次 dry-run） | `ready=True` |
| **未登录** | 一个**全新的空 profile**（下面那条命令，别拿真账号试） | `ready=False` + "尚未登录，需人工扫码登录" |
| **页面还没渲染完** | 不用造，它每次都在 | **不能**报"没登录"：要等 `MARKER_WAIT_SEC`（8s），等不到就说"没渲染出可判断的东西" |

```powershell
. .\scripts\env.ps1
# 未登录那一侧：现造一个空 profile（真机实测：+1.3s 才出现二维码）
@'
import asyncio
from pathlib import Path
from studio.core.config import AccountConfig, load_publish_config
from studio.core.paths import StudioPaths
from studio.services.publish_service import publisher_for

paths = StudioPaths(home=Path.cwd(), data_dir=Path.cwd() / "data")
account = AccountConfig(
    account_id="_probe_blank", platform="douyin", display_name="空 profile",
    profile_dir=paths.browser_profile_dir / "_probe_blank",
)
health = asyncio.run(publisher_for(paths, load_publish_config(paths), account, headless=True).health())
print(f"ready={health.ready} logged_in={health.logged_in} hint={health.hint}")
'@ | uv run python -
```

**平台类名大多带哈希后缀**（抖音真页面上是 `dropdown-wrapper-sdUW26 avatar-wrapper-DIbwVi`），
所以 `login_ok` 这类值要写成 `div[class*='avatar-wrapper']` —— 精确类选择器
`div.avatar-wrapper` 永远命不中。

**别拿"两种状态下都在"的文案当判据**：`login_expired_text` 里曾经带着"扫码登录"，
而那是**登录页自己的按钮文案** —— 全新账号也被报成"登录态已过期"。
`login_required` / `login_ok` 要的是"这一侧独有"的元素。

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

## 7. 新增一个平台要做什么（T5.14）

加一个平台**不写新流程代码**：七步走的是同一个 `PlaywrightPublisher`，差异全在数据里。
顺序很重要 —— 每一步都让下一步更便宜：

1. **`config/publish.yaml` → `platforms.<code>`**：`publisher`（注册表里的代号）、`profile`、
   `title_max` / `caption_max` / `tag_syntax` / `tag_max`、`cover_required`。
   `enabled` **先留 `false`**（见第 5 步）。
2. **`src/studio/publish/platforms/<code>.py`**：一个 `PlaywrightPublisher` 子类 + 一行
   `platform = "<code>"`。**平台特有的流程**（比如 B 站的必选分区）写在这里，
   **不许**往 `playwright_publisher.py` 里加 `if platform == ...`（§4.6.1）。
3. **`src/studio/publish/selectors/<code>.yaml`**：照 §2 的骨架抄一份（`calibrated: false`、
   错的键先留空、`known_gaps` 写清"哪一段压根没写"）。**装配期七条校验**会当场告诉你哪里写错了。
4. **测试**：`tests/unit/publish/test_selectors.py` 的 `REAL_PACKS` 加一个代号
   （它会连带检查"每份随包 pack 都能装起来"）；契约测试会检查"每个平台都有真实现 + 有 pack"。
5. **`studio publish calibrate --platform <code>`**：开一个可见窗口，逐条问真页面。
   这一步**需要**该平台的账号先扫码登录（面板 → 账号 → 「扫码登录」）。
6. **按输出改 yaml，再跑一次**，直到没有"要修"的那几条。然后把 `calibrated: true` /
   `calibrated_at` / `version` 写回去。
7. **`studio publish dry-run --platform <code> --show-browser`**（停在发布前一步、截图），
   最后才是真发一条 —— **R14 不可逆，真发之前跟操作员确认**。
8. **`config/publish.yaml` → `platforms.<code>.enabled: true`**：这一步放在最后。
   顺序反过来的话，面板上会出现一个"能投、但一定发不出去"的平台，而它看起来与抖音一模一样。

## 本次记录

| 时间 | 触发信号 | 动作 | 结果 |
| --- | --- | --- | --- |
| 2026-09-23 | 「国内大部分短视频平台都做一遍」—— 四个二线平台此前是**空实现**，`enabled: false` | ① 二线四个平台改成 `PlaywrightPublisher` 真实现 + 各自一份 pack（`calibrated: false` + `known_gaps`）；② `load_selector_pack` 补**四条语义校验**（§2.1）；③ `calibrated` / `calibrated_at` / `known_gaps` 三个字段 + 面板「校准」那一列；④ 新增 `studio publish calibrate`（只读探针，见 §2.2）与本节这份清单 | ⏳ 四份 pack 仍是**未校准**（面板显示 ⚠️）—— 逐个跑 `publish calibrate` 才算做完 |
| 2026-09-23 | 面板：扫码后一直"等扫码中…"直到 180s 超时；「检测登录态」报"尚未登录" | `douyin.yaml` 的 `login_ok` / `login_required` / `login_expired_text` 按**两种状态**真机校准；`MARKER_WAIT_SEC` 等标志渲染（见 §4.1） | ✅ `acc_main` ⇒ `ready=True`（2.9s）；空 profile ⇒ "尚未登录" |
| 2026-09-23 | 第 ⑦ 步一路等到 600s 超时（两种症状：**发布成功也判不出来** / **点了发布没反应**） | `douyin.yaml` 按**全等**校准：`publish_button: button:text-is('发布')`、`success_marker: "div.success-page, :text-is('发布成功')"`、`reject_marker`、`verify_marker`，新增 `dismiss_overlay` 与 `markers.success_url_contains`；`load_selector_pack` 加**三条装配期校验**（见 §2.1） | ✅ `acc_douyin` 真机跑到"平台要短信验证"（人工那一步，陷阱 #230）；选择器版本 `2026-09-23.4` |
