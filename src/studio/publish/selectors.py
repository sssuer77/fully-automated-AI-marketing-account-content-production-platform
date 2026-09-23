"""选择器包（T5.2 · §06.5.2「选择器集中化」· R13 可热修）。

为什么选择器是**数据**（yaml）而不是代码里的常量
------------------------------------------------
平台改版是**常态**（R13 的原文就是"页面改版导致选择器失效"）。选择器写在
``platforms/douyin.py`` 里时，修一个 CSS 选择器要：改代码 → 跑门禁 → 重启 worker；
写在 yaml 里时只要改文件 + 重启 worker（选择器在装配期读一次）。而
``selectors_version`` 会随这次改版一起进 ``publications.evidence_json`` ——
事后能回答"这条是哪个版本的选择器发的"。

**哪些 pack 验过、哪些没验过：这是一份数据，不是一句注释**
--------------------------------------------------------
真机校准需要**一个已登录的真实账号**（§06.2.2 把"实测校准"写成现场动作）。所以
每个 pack 自己声明 ``calibrated`` / ``calibrated_at``，:class:`SelectorPack` 把它们
读进来，面板上照实显示。**注释不拦人** —— "看起来挺像那么回事"最容易让人以为
它验过了，而库里那条记录是**真发出去**的（R14 不可逆）。

``calibrated: false`` 的 pack **能用**（投递、演练都不拦），但面板会明说"这个平台的
选择器还没真机校准过"。校准的入口是 ``studio publish calibrate --platform <code>``：
它开一个可见窗口，把 pack 里每一条选择器在真页面上逐条跑一遍，报"命中几个 / 0 个"。

**装配期只放行"结构上能跑"的 pack**（缺必需键、选择器写错、状态判据必然失灵 ⇒ 直接拒）。
具体 CSS 值对不对是**真机校准**的事，这里判不了 —— 判得了的那几条，见
:func:`_check_semantics`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from studio.core.errors import ErrorCode, PublishError

__all__ = [
    "AMBIGUOUS_LOGIN_MARKERS",
    "DEFAULT_READBACK",
    "METRIC_KEYS",
    "METRIC_SELECTOR_KEYS",
    "OPTIONAL_SELECTORS",
    "POST_ID_PLACEHOLDER",
    "RATIO_METRIC_KEYS",
    "READBACK_KINDS",
    "REQUIRED_SELECTORS",
    "SELECTOR_KEY_GROUPS",
    "SelectorPack",
    "load_selector_pack",
    "selector_root",
]

#: 选择器 yaml 的目录（随包走，不随 ``STUDIO_HOME`` 走）。
_SELECTOR_DIRNAME: Final[str] = "selectors"

#: **必须有**的选择器。缺一个就装不起来 —— 装起来再在真机上以
#: ``PUBLISH_SELECTOR_MISS`` 收场，等于把"配置写错了"推迟成"发不出去"。
REQUIRED_SELECTORS: Final[tuple[str, ...]] = (
    "login_ok",  # 已登录的标志元素（health 用）
    "login_required",  # 未登录的标志元素（health 用）
    "upload_input",  # 文件输入框（八步第 ② 步）
    "title_input",  # 标题输入框（第 ③ 步）
    "publish_button",  # 发布按钮（第 ⑥ 步，dry-run **不点**）
)

#: 可选选择器：缺了只是少做一步，不该拦下整条链路。
OPTIONAL_SELECTORS: Final[tuple[str, ...]] = (
    "caption_input",  # 文案框；有些平台标题与文案是同一个框
    "upload_progress",  # 上传进度元素（轮询百分比）
    "cover_trigger",  # 打开"设置封面"弹层的按钮（第 ④ 步）
    "cover_input",  # 弹层里的图片输入框（第 ④ 步）
    "dismiss_overlay",  # 挡住发布按钮的平台提示弹层（第 ⑥ 步，点一下关掉它）
    "verify_marker",  # 平台要求短信/人脸验证的框（第 ⑦ 步 ⇒ 转人工，R13）
    "success_marker",  # 结果页标志（第 ⑦ 步）
    "reject_marker",  # 审核不通过的标志（§06.10 的 PUBLISH_REVIEW_REJECTED）
    # ── 数据回收（T5.4 · §06.6）───────────────────────────────────────
    # 这一组**全是可选**：采不到数只是"这条作品的趋势图空着"，不该让发布链路
    # 或整条回收循环挂掉（§06.6「不阻塞其他发布」）。四个计数各占一个键而不是
    # 一个"容器选择器"，是因为平台把它们放在**四个兄弟节点**里（没有共同父节点
    # 能一次读全），而"读一个节点"这件事已经有现成的 `text_content` 了。
    "metric_row",  # 管理页上**这一条作品**的那一行（内含 `{post_id}` 占位符）
    "metric_views",  # 播放量（**相对** metric_row 的后代选择器）
    "metric_likes",  # 点赞
    "metric_comments",  # 评论
    "metric_shares",  # 分享
    "metric_completion_rate",  # 完播率（**比率**，解析器与上面四个计数不同）
)

#: ``metric_row`` 里的作品号占位符（T5.4）。用 ``str.replace`` 而不是 ``str.format``：
#: 选择器里本来就全是 ``{}``（CSS 属性选择器、``:has-text()``），拿 ``format`` 去填
#: 一个占位符会把其余的 ``{}`` 当成字段名 —— 报错还算好的，改坏成静默的空串才要命。
POST_ID_PLACEHOLDER: Final[str] = "{post_id}"

#: 四个**计数**的键（顺序 = 面板上的列顺序）。`parse_metric_count` 按它遍历，
#: 于是"加一个计数维度"（比如收藏）只改这一行。
METRIC_KEYS: Final[tuple[str, ...]] = ("views", "likes", "comments", "shares")

#: **比率**类的键。与 :data:`METRIC_KEYS` 分开是必须的：``42.3%`` 走
#: ``parse_metric_count`` 会变成整数 42，而 42 与 0.423 在"完播率"这个语义下
#: 差 100 倍，且两者都是**看着正常**的数（42 与 0.42 都像真的）。
#: 读取端按这个分组挑解析器，写库端按同一分组拼 payload —— 分组只此一份。
RATIO_METRIC_KEYS: Final[tuple[str, ...]] = ("completion_rate",)

#: 数据回收那一组（T5.4）的选择器键。
#:
#: 与"发布路径"那一组有一个**根本区别**：验它们要一条**真的发出去过**的作品 ——
#: 管理页上一条作品都没有时，``metric_row`` 里的 ``{post_id}`` 没有东西可换，
#: 于是探针只能报"跳过"。把"哪些键属于哪一组"放在**一处**（而不是让 calibrate
#: 自己列一份），是因为加一个计数维度（比如收藏）时只该改 :data:`METRIC_KEYS`
#: 一行 —— 抄成两份的那一刻，"新维度没被校准"就变成了一个安静的缺口。
METRIC_SELECTOR_KEYS: Final[tuple[str, ...]] = (
    "metric_row",
    *(f"metric_{key}" for key in METRIC_KEYS),
    *(f"metric_{key}" for key in RATIO_METRIC_KEYS),
)

#: 回读方式的取值（§06.5.3 第 ⑤ 步）。**为什么这是数据不是常量**：标题框在四个平台
#: 上都是 ``<input>``（读 ``input_value``），而文案框有的是 ``<textarea>``（也是
#: ``input_value``）、有的是 ``contenteditable``（只能读 ``inner_text``）——
#: 对着 ``contenteditable`` 调 ``input_value`` 会直接抛，对着 ``<textarea>`` 调
#: ``inner_text`` 读到的是**初始内容**而不是当前值（一个安静的错值）。
READBACK_KINDS: Final[tuple[str, ...]] = ("value", "text")

#: 回读方式的默认值（pack 没写时用）。
DEFAULT_READBACK: Final[dict[str, str]] = {"title": "value", "caption": "text"}

#: 必须有正文的**文本**标记组（不是选择器，是页面上的中文提示语）。
REQUIRED_MARKERS: Final[tuple[str, ...]] = (
    "login_expired_text",  # 与"从未登录"区分开：见 PlaywrightPublisher.health
    "review_rejected_text",  # 平台审核不通过的提示语
)

#: **两种状态下都在页面上**的文案，因此不能拿来当 ``login_expired_text``。
#:
#: 真机 2026-09-23（陷阱 #212）：``login_expired_text`` 里带着"扫码登录"，而那是
#: **登录页自己的按钮文案** —— 一个**全新、从没登录过**的 profile 也会命中它，
#: 于是面板报"登录态已过期，需人工重新扫码登录"，而真相是"这个号从来没登录过"。
#: 两句给的操作员动作**正好相反**（一个是"重新扫一次"，一个是"先扫一次"），
#: 混成一句就等于给一半人指错路。
AMBIGUOUS_LOGIN_MARKERS: Final[tuple[str, ...]] = ("扫码登录",)

SELECTOR_KEY_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "required": REQUIRED_SELECTORS,
    "optional": OPTIONAL_SELECTORS,
}


def selector_root() -> Path:
    """选择器目录 ``src/studio/publish/selectors/``。"""
    return Path(__file__).resolve().parent / _SELECTOR_DIRNAME


@dataclass(frozen=True, slots=True)
class SelectorPack:
    """一个平台的选择器 + 页面地址 + 文本标记（**加载后只读**）。"""

    platform: str
    version: str
    urls: dict[str, str]
    selectors: dict[str, str]
    markers: dict[str, tuple[str, ...]]
    readback: dict[str, str]
    source: Path
    #: 这份 pack 的**发布路径**选择器（八步用到的那些）**有没有在真机上逐条校准过**
    #: （``studio publish calibrate``）。
    #:
    #: 为什么是数据而不是注释：注释不拦人，也不会出现在面板上。而"这个平台的选择器
    #: 到底验过没有"是操作员决定"今天要不要拿它真发一条"时唯一要紧的信息 ——
    #: 发出去就收不回来了（R14）。
    #:
    #: ⚠️ 它**不覆盖** :attr:`known_gaps`：校准的是"这些选择器对不对"，而
    #: "还有没有一段流程压根没写"是另一件事（B 站的必选分区就是）。两件事都得让
    #: 操作员看见 —— 只写"未校准"会让人以为"校准完就能发了"。
    calibrated: bool = False
    #: 校准日期（``YYYY-MM-DD``）。``calibrated=True`` 时**必须有**（见 :func:`_check_semantics`）：
    #: "校准过"与"什么时候校准的"是同一件事的两半 —— 平台会改版，一份三年前的
    #: 校准记录与没校准几乎一样没用。
    calibrated_at: str = ""
    #: 这个平台**已经确认没做**的部分（真机上撞到的、或规格书明写的必做步骤）。
    #:
    #: 为什么连"缺口"也要变成数据：校准状态回答的是"这些选择器验过没有"，而它
    #: **回答不了**"这个平台还有没有一段流程压根没写"（比如 B 站的必选分区）。
    #: 两件事都得让操作员在面板上看见 —— 只写"未校准"会让人以为"校准完就能发了"。
    #:
    #: 只写**已经确认**的（真机现场撞到的、或 §06.2.2 那种规格书明写的），不写猜测：
    #: 猜出来的"缺口"会让人去修一个不存在的问题，与"假装做完了"一样贵。
    known_gaps: tuple[str, ...] = ()

    def url(self, key: str) -> str:
        return self._get(self.urls, key, kind="url")

    def selector(self, key: str) -> str:
        """取一个选择器；缺 ⇒ ``PUBLISH_SELECTOR_MISS``。

        **必选项在加载时已经校验过**，所以这里抛出的只可能是"要了一个可选键"。
        运行期仍要抛而不是返回空串：空串喂给 ``page.click()`` 会变成
        "在当前页面上随便点了某个东西"，比一个明确的报错危险得多。
        """
        return self._get(self.selectors, key, kind="selector")

    def has(self, key: str) -> bool:
        return bool(self.selectors.get(key))

    def texts(self, key: str) -> tuple[str, ...]:
        return self.markers.get(key, ())

    def readback_kind(self, key: str) -> str:
        """该字段怎么读回来：``value``（``<input>``/``<textarea>``）或 ``text``（富文本）。"""
        return self.readback.get(key) or DEFAULT_READBACK.get(key, "text")

    @staticmethod
    def _get(mapping: dict[str, str], key: str, *, kind: str) -> str:
        value = mapping.get(key, "")
        if not value:
            raise PublishError(
                f"选择器包缺少 {kind}：{key}",
                code=ErrorCode.PUBLISH_SELECTOR_MISS,
                context={"kind": kind, "key": key},
                remediation="在 selectors/<platform>.yaml 里补上这一项"
                "（见 docs/runbook/publish_selector.md）",
            )
        return value


def load_selector_pack(platform: str, *, root: Path | None = None) -> SelectorPack:
    """读 ``selectors/<platform>.yaml`` 并**在装配期**把结构性错误全部暴露出来。

    校验放在这里（而不是第一次用到时）的理由：装配发生在 worker 启动的几十毫秒里，
    而"第一次用到"发生在视频已经上传完之后 —— 后者要重传一次 12MB。
    """
    path = (root or selector_root()) / f"{platform}.yaml"
    if not path.is_file():
        # 现有哪几份**当场列出来**（而不是写死在文案里）：平台是一批批加的，
        # 写死的那句话会在"加了第四份 pack"之后变成一句错的指路。
        existing = " / ".join(sorted(item.stem for item in (root or selector_root()).glob("*.yaml")))
        raise PublishError(
            f"没有 {platform} 的选择器文件：{path}",
            code=ErrorCode.PUBLISH_SELECTOR_MISS,
            context={"platform": platform, "path": path.as_posix()},
            remediation=f"在 selectors/ 下加一份 {platform}.yaml（骨架见 docs/runbook/publish_selector.md）；"
            f"现有：{existing or '（一份都没有）'}",
        )

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - 触发靠坏文件
        raise PublishError(
            f"选择器文件不是合法 YAML：{path.name}",
            code=ErrorCode.PUBLISH_SELECTOR_MISS,
            context={"path": path.as_posix(), "error": str(exc)},
            remediation="按 docs/runbook/publish_selector.md 的骨架改回来",
        ) from exc

    if not isinstance(payload, dict):
        raise _bad(path, "顶层必须是一个映射（platform / version / urls / selectors / markers）")

    declared = str(payload.get("platform", "")).strip()
    if declared != platform:
        raise _bad(path, f"文件里写的 platform 是 {declared!r}，与文件名 {platform!r} 不一致")

    version = str(payload.get("version", "")).strip()
    if not version:
        raise _bad(path, "缺少 version（它会被写进 publications.evidence_json，事后定位改版用）")

    urls = _string_map(path, payload.get("urls"), field="urls")
    if not urls.get("upload"):
        raise _bad(path, "urls.upload 是必填（八步第 ① 步要打开的页面）")

    selectors = _string_map(path, payload.get("selectors"), field="selectors")
    missing = [key for key in REQUIRED_SELECTORS if not selectors.get(key)]
    if missing:
        raise _bad(path, f"缺少必需选择器：{', '.join(missing)}")
    broken = {key: value for key, value in selectors.items() if _engine_prefix(value)}
    if broken:
        raise _bad(
            path,
            "这些选择器用了引擎前缀，而 Playwright 只允许它**单独**出现："
            f"{broken} —— 写成 `div.x, text=文案` 会直接抛 "
            '`Unexpected token "=" while parsing css selector`，而调用方把异常吞成'
            '"元素不在"，于是整条流程一路等到超时（陷阱 #226）。要用文案就用 CSS 伪类 '
            "`:text('文案')`。",
        )
    if _LOOSE_PUBLISH.search(selectors.get("publish_button", "")):
        raise _bad(
            path,
            "publish_button 用了 `:has-text()`（**包含**匹配）。平台的导航项常常叫"
            "「作品发布」/「发布作品」，而它在文档序里排在真按钮**前面** —— 于是点下去"
            "只是切了个页面、表单被重置，真按钮一次都没被碰过。症状是"
            '"点了发布什么都没发生"，然后第 ⑦ 步一路等到超时（陷阱 #227）。'
            "用 `:text-is('发布')`（全等）钉住真按钮。",
        )
    loose_markers = {key: selectors[key] for key in _EXACT_TEXT_KEYS if ":text(" in selectors.get(key, "")}
    if loose_markers:
        raise _bad(
            path,
            f"这些结果页标志用了 `:text()`（**子串**匹配）：{loose_markers} —— 页面上总有一句"
            "包含这几个字的**普通提示**（真机 2026-09-23：「视频发布成功后，价格将无法更改」"
            '里就有"发布成功"），于是在**发布之前**就判成了成功，一条根本没发出去的内容被'
            '记成 published（陷阱 #229）。判据要钉在"这一整块就是这几个字"上 ⇒ '
            "用 `:text-is('发布成功')`。",
        )

    readback = _string_map(path, payload.get("readback"), field="readback")
    bad_kinds = {key: value for key, value in readback.items() if value not in READBACK_KINDS}
    if bad_kinds:
        raise _bad(path, f"readback 只允许 {'/'.join(READBACK_KINDS)}：{bad_kinds}")

    markers_raw = payload.get("markers") or {}
    if not isinstance(markers_raw, dict):
        raise _bad(path, "markers 必须是映射（键 → 文本列表）")
    markers: dict[str, tuple[str, ...]] = {}
    for key, value in markers_raw.items():
        if isinstance(value, str):
            markers[str(key)] = (value,)
        elif isinstance(value, list):
            markers[str(key)] = tuple(str(item) for item in value)
        else:
            raise _bad(path, f"markers.{key} 必须是字符串或字符串列表")
    for key in REQUIRED_MARKERS:
        if not markers.get(key):
            raise _bad(path, f"缺少必需文本标记：markers.{key}")

    calibrated = bool(payload.get("calibrated", False))
    calibrated_at = str(payload.get("calibrated_at", "")).strip()
    known_gaps = _string_list(path, payload.get("known_gaps"), field="known_gaps")
    _check_semantics(
        path,
        selectors=selectors,
        markers=markers,
        calibrated=calibrated,
        calibrated_at=calibrated_at,
    )

    return SelectorPack(
        platform=platform,
        version=version,
        urls=urls,
        selectors=selectors,
        markers=markers,
        readback=readback,
        source=path,
        calibrated=calibrated,
        calibrated_at=calibrated_at,
        known_gaps=known_gaps,
    )


def _check_semantics(
    path: Path,
    *,
    selectors: dict[str, str],
    markers: dict[str, tuple[str, ...]],
    calibrated: bool,
    calibrated_at: str,
) -> None:
    """判据里**必然失灵**的那几条，在装配期就拒（而不是等真机上耗掉 600 秒）。

    与上面那几条（缺键 / 引擎前缀 / 包含匹配）的分工
    ------------------------------------------------
    那几条判的是"**这句话写错了**"（Playwright 会抛、或者命中的是另一个元素）；
    这里判的是"**这句话永远不成立**" —— 语法合法、也能命中，但它在**任何**状态下
    都给不出可用答案。两类错误的现场症状一模一样（第 ⑦ 步一路等到超时），
    所以都在这里拦。

    每一条都对应一个真机踩过的坑，注释里写明是哪一个 —— 删掉任何一条之前，
    先读那段注释。
    """
    # ① 两种状态下都在的文案不能当"登录已过期"的判据（陷阱 #212）。
    ambiguous = [
        text
        for text in markers.get("login_expired_text", ())
        for phrase in AMBIGUOUS_LOGIN_MARKERS
        if phrase in text
    ]
    if ambiguous:
        raise _bad(
            path,
            f"markers.login_expired_text 里有**两种状态下都会出现**的文案：{ambiguous} —— "
            "它是**登录页自己的按钮文案**，于是「从没登录过」的号也被报成「登录态已过期」。"
            "这两句话给的操作员动作正好相反（一个「重新扫一次」、一个「先扫一次」），"
            "混成一句就等于给一半人指错路（陷阱 #212）。删掉它 —— 这一侧要的是"
            "「被平台踢下线」时才会出现的那句（比如「登录已过期」）。",
        )

    # ② 第 ⑦ 步必须有一个**能成立**的成功判据。
    #
    # 抖音真机（陷阱 #228）：平台不发"发布成功"这几个字，而是把页面**跳走** ——
    # 只认元素的话，一条**已经发出去**的内容会在 600 秒之后被记成失败，而那时人已经
    # 在平台上看到它了。所以两个判据至少得有一个：元素标志、或者地址片段。
    if not selectors.get("success_marker") and not markers.get("success_url_contains"):
        raise _bad(
            path,
            "第 ⑦ 步**一个成功判据都没有**：success_marker 与 markers.success_url_contains "
            "都空着。这样「发布成功」永远判不出来，每一次都会走到超时 —— 而超时之后"
            "那条记录写的是「失败」，平台上却已经有了那条作品（R14 不可逆，重试就是第二条）。"
            "至少给一个：页面上那句「发布成功」（用 `:text-is()`），或者平台跳走之后地址里的片段。",
        )

    # ③ metric_row 要能被"这一条作品"填进去。
    #
    # 占位符不是装饰：四个计数是**相对它**取的后代选择器。少了它，选择器会命中
    # **第一条**作品的行 —— 于是"这一条的数据"读的是别人的数，而面板上两个数字
    # 都长得像正常的数（T5.4 的数据回收最贵的一类错：错得看不出来）。
    row = selectors.get("metric_row", "")
    if row and POST_ID_PLACEHOLDER not in row:
        raise _bad(
            path,
            f"metric_row 里没有 {POST_ID_PLACEHOLDER} 占位符：{row!r} —— 数据回收会去读"
            "**第一条**作品那一行，于是「这一条的数据」其实是别人的数，而面板上两个数字"
            "都长得像正常的数（T5.4）。",
        )

    # ④ "校准过"必须说得出是哪天。
    if calibrated and not calibrated_at:
        raise _bad(
            path,
            "calibrated: true 但没有 calibrated_at —— 「校准过」与「什么时候校准的」是同一件事"
            "的两半：平台会改版，一份很久以前的校准记录与没校准几乎一样没用"
            "（面板上那一列就是给人看这个的）。",
        )


def _string_map(path: Path, value: Any, *, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _bad(path, f"{field} 必须是映射")
    out: dict[str, str] = {}
    for key, raw in value.items():
        text = str(raw).strip()
        if text:
            out[str(key)] = text
    return out


def _string_list(path: Path, value: Any, *, field: str) -> tuple[str, ...]:
    """一串人写的说明（``known_gaps`` 这类）。缺 ⇒ 空；写成一个字符串也收下。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, list):
        raise _bad(path, f"{field} 必须是字符串或字符串列表")
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return tuple(out)


#: 引擎前缀（``text=`` / ``xpath=`` …）。
#:
#: 判据是**"只要值里有逗号，整条就必须是 CSS"**：单独一个 ``text=文案`` 是合法的
#: （Playwright 按前缀选引擎），但只要拼上第二个选择器，它整条就走 CSS 解析器，
#: 于是抛 `Unexpected token "=" while parsing css selector`（2026-09-23 真机实测）。
#: 而调用方把异常吞成"元素不在" ⇒ 发布成功也一路等到 600s 超时（陷阱 #226）。
_ENGINE_PREFIX = re.compile(r"(?:^|,)\s*(?:text|xpath|css|id|data-testid|role|nth)=")


def _engine_prefix(value: str) -> bool:
    return "," in value and bool(_ENGINE_PREFIX.search(value))


#: ``publish_button`` 里的**包含**匹配（``:has-text`` / ``text=``）。
#:
#: 发布按钮的文案都很短（"发布" / "发表"），而平台上总有一个更早出现的同名词
#: （导航项「作品发布」就是）。包含匹配会命中那个 —— 点下去只是切页面、表单被重置，
#: 真按钮一次都没被碰过，而现场看到的是"点了没反应"（陷阱 #227）。
_LOOSE_PUBLISH = re.compile(r":has-text\(|(?:^|,)\s*text=")

#: 这几个标志的文案都是**平台上的常用词**（"发布成功" / "审核不通过"），页面上几乎
#: 一定有一句包含它们的普通提示 —— 子串匹配会命中那一句（见 ``load_selector_pack``
#: 里那条校验 / 陷阱 #229）。
_EXACT_TEXT_KEYS: Final[tuple[str, ...]] = ("success_marker", "reject_marker", "verify_marker")


def _bad(path: Path, reason: str) -> PublishError:
    return PublishError(
        f"选择器文件不合法（{path.name}）：{reason}",
        code=ErrorCode.PUBLISH_SELECTOR_MISS,
        context={"path": path.as_posix(), "reason": reason},
        remediation="按 docs/runbook/publish_selector.md 的骨架修",
    )
