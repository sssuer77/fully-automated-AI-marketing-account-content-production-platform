r"""发布领域规则（T5.2 · §06.2.2 / §06.5.3）—— 全是纯函数，零 I/O。

为什么这些住在 ``domain/`` 而不是 ``publish/``
----------------------------------------------
它们是**规则**，不是"跟浏览器打交道"：标题多长算超、话题怎么按平台语法拼、幂等键怎么算、
回读比对上从哪一位开始不一致 —— 这些判据在 dry-run、真发布、单元测试三处都要用，
而它们**都不该**为了被验证先起一个 Chromium。碰 Playwright 的那半边在
``publish/playwright_publisher.py``。

三条规则来自规格书，逐条对应
----------------------------
① **超长是"报"，不是"静默截断"**（§06.2.2 第 2 条）：平台会把超长标题悄悄切掉，
   而我们这边若也切，就再没人知道"发出去的不是写的那一版"。:func:`fit_text` 只报
   越界多少，切不切由调用方按平台决定。
② **话题按平台语法拼**（§06.2.2 第 3 条）：抖音/快手/视频号是 ``#话题``，微博是
   ``#话题#``。语法来自 ``config/publish.yaml`` 的 ``tag_syntax``（**数据**），
   不是这里的常量 —— 平台改规则时不该改代码。
③ **回读比对是逐字比对**（§06.5.3 第 ⑤ 步）：平台富文本编辑器会吞 emoji / 换行 /
   超长文本。比对失败时**必须说清是哪一种吞法** —— "少了 3 个字"与"emoji 被吃了"
   对操作员是两条完全不同的处置路径，而"比对失败"四个字哪种都没说。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from studio.core.ids import IDEMPOTENCY_SEP, publication_idempotency_key

#: 发布幂等键（§03.3.15）。**实现住在 ``core/ids.py``**，这里只留契约名 ——
#: ``publications`` 的唯一写入者是 ``db/``，而 ``db`` 不许 import ``domain``（§02.4）。
idempotency_key = publication_idempotency_key

__all__ = [
    "IDEMPOTENCY_SEP",
    "UNIT_REF_SEP",
    "CaptionPlan",
    "ReadbackDiff",
    "TagPlan",
    "TextFit",
    "build_caption",
    "compare_readback",
    "fit_text",
    "idempotency_key",
    "parse_unit_ref",
    "render_tags",
    "unit_ref",
]

#: 发布作业单元标识里平台与账号之间的分隔符（T5.8）。
#:
#: **为什么单元标识必须带账号**：``jobs`` 的幂等键是
#: ``(task_id, pool, unit_type, unit_ref)`` —— 一条任务在一个平台上**一辈子只有一条**
#: 作业。而 §06.2.4 要求「同任务可安全分发到多账号」（两个账号是两件不同的事，
#: 幂等键里也算进了 ``account_id``）。单元标识不带账号，第二条作业根本投不出来。
#:
#: 用 ``:`` 而不是 ``|``：``|`` 已经被发布幂等键占用（§03.3.15 逐字
#: ``sha256(task_id|platform|account_id)``），两个键长得像会让人以为它们同源。
UNIT_REF_SEP = ":"


def unit_ref(platform: str, account_id: str) -> str:
    """发布作业的单元标识 ``platform:account_id``（T5.8 · §06.2.4）。

    **老格式（只有平台代号）仍然认**，见 :func:`parse_unit_ref` —— 真机库里
    已经有一批 ``unit_ref='other'`` 的作业行，把老格式当成"另一个单元"会让重投
    凭空多出一条作业。多出来的那条不会重复发布（``publications`` 的幂等键兜着），
    但它会白跑一次、并在死信/日志里多一行看不懂的东西。
    """
    return f"{platform}{UNIT_REF_SEP}{account_id}"


def parse_unit_ref(ref: str | None) -> tuple[str, str | None]:
    """``platform:account_id`` ⇒ ``(platform, account_id)``。

    **只切第一个分隔符**：``account_id`` 是人在配置里写的标识符，含 ``:`` 也照样还原。
    ``platform`` 是枚举（``douyin`` / ``other`` …），天然不含分隔符。

    没有分隔符 ⇒ 当作**老格式**（只有平台代号），账号给 ``None`` 让调用方回落到
    payload 或配置（:func:`~studio.services.publish_service.resolve_account`）。
    这一条是**向后兼容的全部实现**：老作业行重投时照常能跑。
    """
    text = (ref or "").strip()
    if UNIT_REF_SEP not in text:
        return (text, None)
    platform, _, account_id = text.partition(UNIT_REF_SEP)
    return (platform.strip(), account_id.strip() or None)


#: 回读比对的取景半径（不一致处左右各取这么多字符）。取 12 是因为一个中文标题
#: 在日志里折一行大约就是 20 出头 —— 再宽，日志里那条证据本身就得折行看。
_EXCERPT_RADIUS = 12

#: emoji 的粗略范围。**不求全**：这里只回答"这段文本里有没有 emoji、两边是不是同一批"，
#: 不回答"哪个字符属于哪个 emoji"（那是渲染层的事，而且是件很难做对的事）。
_EMOJI = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\u2b00-\u2bff\ufe0f\u200d]")

#: "看不见、但会出现在回读结果里"的格式字符（§06.5.3 第 ⑤ 步 · 真机 2026-09-23）。
#:
#: 抖音的富文本编辑器会在文案末尾塞一个**零宽空格**（U+200B）—— 那是编辑器用来撑住
#: 空行的哨兵，不是我们填进去的字，也不改变任何人读到的东西。逐字比对会因此判成
#: ``mismatch``，于是一条"码也扫了、视频也传完了"的发布卡在一个**看不见的字符**上。
#:
#: 为什么**不收** U+200C / U+200D（零宽非连接符 / 连接符）：它们参与 emoji 与印度语系
#: 的字形组合（``_EMOJI`` 里已经含 U+200D）。把它们当"不可见"会顺手放行"emoji 被吞"，
#: 而那正是回读要抓的东西。收在这里的都是纯排版控制符：软连字符、零宽空格、
#: 方向标记（LRM / RLM）、双向文本嵌入与隔离、词连接符、BOM。
_INVISIBLE = re.compile("[\u00ad\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")


# ── 标题 / 文案 / 话题 ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TextFit:
    """一段文本对某个平台上限的"超没超"。**只描述，不修改文本。**"""

    text: str
    limit: int
    over_by: int

    @property
    def ok(self) -> bool:
        return self.over_by <= 0


def fit_text(text: str, *, limit: int) -> TextFit:
    """量文本超没超平台上限（§06.2.2 第 2 条：超长 ⇒ ``warn``，**不静默截断**）。

    按**字符数**量（``len``）而不是字节数或显示宽度：平台的计数器就是字符数
    （一个汉字算 1），而"显示宽度"是排版问题 —— 拿排版口径去卡平台上限，
    会在纯中文标题上放行一批实际会被平台截断的文本。
    """
    return TextFit(text=text, limit=limit, over_by=max(0, len(text) - limit))


@dataclass(frozen=True, slots=True)
class TagPlan:
    """话题拼装结果：**拼出来的**、**被丢掉的**分开放。

    合在一个字段里（比如只返回 ``("  #a #b", 2)``）会让调用方没法把"丢了哪几个"
    写进告警 —— 而话题被丢是运营看得见的效果差（曝光少一截），必须报出来。
    """

    tags: tuple[str, ...]
    rendered: tuple[str, ...]
    dropped: tuple[str, ...]


def render_tags(tags: list[str] | tuple[str, ...], *, syntax: str, max_tags: int) -> TagPlan:
    """按平台语法拼话题；超出 ``max_tags`` 的**丢掉并报出来**。

    ``syntax`` 形如 ``"#{tag}"`` / ``"{tag}"`` / ``"#{tag}#"``，``{tag}`` 是占位符
    （``config/publish.yaml`` 的 ``tag_syntax``，:class:`PlatformConfig` 已校验它必须含占位符）。

    去重按**出现顺序**保留第一个：模型偶尔把同一个词写两遍，而平台上重复话题会被
    判成刷标签。空白项直接跳过 —— "文案里多打了个空格"不该变成一个空话题。
    写进来的 ``#`` 前缀会被剥掉再按 ``syntax`` 重新拼：让语法只有一个来源。
    """
    cleaned: list[str] = []
    for raw in tags:
        tag = str(raw).strip().lstrip("#").strip()
        if not tag or tag in cleaned:
            continue
        cleaned.append(tag)
    kept = tuple(cleaned[:max_tags]) if max_tags > 0 else ()
    dropped = cleaned[len(kept) :]
    return TagPlan(
        tags=kept,
        rendered=tuple(syntax.replace("{tag}", tag) for tag in kept),
        # ``dropped`` 与 ``rendered`` **同一形态**（都带语法）：这两个字段会被
        # ``build_caption`` 合并成一个 ``dropped_tags``，一半带 ``#`` 一半不带的话，
        # 告警里读起来像两种东西（T5.2 写测试时撞到）。
        dropped=tuple(syntax.replace("{tag}", tag) for tag in dropped),
    )


@dataclass(frozen=True, slots=True)
class CaptionPlan:
    """最终要填进"文案"输入框的那串文本，以及它是怎么被削出来的。"""

    text: str
    tags: tuple[str, ...]
    dropped_tags: tuple[str, ...]
    truncated_by: int


def build_caption(caption: str, plan: TagPlan, *, caption_max: int) -> CaptionPlan:
    """正文 + 话题拼成最终文案，装不下时**从尾部摘话题**，最后才动正文。

    为什么摘话题而不是截正文：正文是给人读的，话题是给算法读的。缺一个话题顶多少
    一点曝光，缺半句话是内容事故（"关注我看下集"变成"关注我看"）。

    为什么从**尾部**摘：话题顺序是模型按重要性排的，越靠后越边缘。
    """
    base = caption.strip()
    rendered = list(plan.rendered)
    dropped: list[str] = []
    while rendered and len(_join(base, rendered)) > caption_max:
        dropped.insert(0, rendered.pop())

    text = _join(base, rendered)
    truncated_by = max(0, len(text) - caption_max)
    if truncated_by:
        text = text[:caption_max]
    return CaptionPlan(
        text=text,
        tags=tuple(rendered),
        dropped_tags=(*plan.dropped, *dropped),
        truncated_by=truncated_by,
    )


def _join(base: str, rendered: list[str]) -> str:
    parts = [base, *rendered] if base else list(rendered)
    return " ".join(parts)


# ── 回读比对（§06.5.3 第 ⑤ 步）────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReadbackDiff:
    """页面回读的标题/文案与请求的差异。

    ``reason`` 的取值刻意分成六种而不是一个布尔：平台编辑器**吞法不同，处置也不同** ——

    ==================  ==========================================================
    ``ok``              一致
    ``empty``           输入框整个空了 ⇒ 多半是选择器点错了框，不是编辑器吞字
    ``emoji_stripped``  emoji 被吃掉 ⇒ 去掉 emoji 后两边一致（改文案，别重填）
    ``whitespace_only`` 只差空白/换行 ⇒ 编辑器把换行归一了（要不要放行由调用方定）
    ``invisible_only``  只差**不可见字符**（零宽空格等）⇒ 编辑器自己塞的哨兵，
                        **不是丢字**（见 :data:`_INVISIBLE`；要不要放行由调用方定）
    ``mismatch``        真丢字 ⇒ 重填 ≤2 次（§06.5.3）
    ==================  ==========================================================
    """

    matched: bool
    reason: str
    at: int | None = None
    expected_excerpt: str = ""
    actual_excerpt: str = ""
    #: 只在 ``invisible_only`` 时有值：两边对不上的那几个不可见字符（``U+200B`` 这种）。
    #: 为什么要把码点带出来：这两个字符**在日志里长得一模一样** —— 只说"回读不一致"，
    #: 看的人会以为是系统在胡说（T5.2 真机踩过同族的坑，见 :func:`_classify`）。
    invisible: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        """一行可进日志/告警的说明（**不含**全文，避免把整篇文案写进日志）。"""
        if self.matched:
            return "回读一致"
        if self.reason == "invisible_only":
            names = "、".join(self.invisible) if self.invisible else "（码点未知）"
            return f"回读只差不可见字符：{names} —— 编辑器自己塞的哨兵，不是丢字"
        if self.at is None:
            return f"回读不一致（{self.reason}）"
        return (
            f"回读不一致（{self.reason}）· 第 {self.at + 1} 字符起："
            f"期望 {self.expected_excerpt!r} / 实际 {self.actual_excerpt!r}"
        )


def compare_readback(expected: str, actual: str) -> ReadbackDiff:
    """逐字比对"我们填的"与"页面回读的"（§06.5.3 第 ⑤ 步）。

    ``whitespace_only`` 仍然算 ``matched=False`` —— 判定归判定，放行归放行
    （与 T5.1 的 ``GateResult`` 同一条：把"要不要拦"揉进"是不是一致"，
    调用方就再也拿不到干净的观测了）。要不要把空白差异当通过，由发布器决定。
    """
    if expected == actual:
        return ReadbackDiff(matched=True, reason="ok")

    reason = _classify(expected, actual)
    # ``empty`` 不给偏移：输入框整个空了的时候，"第 0 字符起"是个没有信息量的数字，
    # 而它会挤掉那一行里真正有用的东西（"框是空的 ⇒ 多半是选择器点错了"）。
    at = None if reason == "empty" else _first_difference(expected, actual)
    if reason == "invisible_only":
        # 只差不可见字符时**不给偏移**：那个下标指着一对"看起来完全一样"的字符，
        # 除了让人怀疑系统坏了没有任何用。要说的是"差的是哪几个码点"。
        return ReadbackDiff(
            matched=False, reason=reason, at=None, invisible=_invisible_codes(expected, actual)
        )
    if at is None:
        return ReadbackDiff(matched=False, reason=reason, at=None)
    radius = _EXCERPT_RADIUS
    return ReadbackDiff(
        matched=False,
        reason=reason,
        at=at,
        expected_excerpt=expected[max(0, at - radius) : at + radius],
        actual_excerpt=actual[max(0, at - radius) : at + radius],
    )


def _classify(expected: str, actual: str) -> str:
    """判定顺序**不能换**：先排除"只差空白"，再排除"只差 emoji"，剩下的才是真丢字。

    两个排除都用 :func:`_squash`（去掉全部空白）来比：平台把 ``\n`` 换成空格是常态，
    而"emoji 被吞"这件事几乎总是与"尾部多一个换行"一起来（富文本区 ``inner_text``
    会带上一行的结尾）。**只按去 emoji 后的原文比**，真实页面上的吞 emoji 会被判成
    ``mismatch`` —— 而那意味着操作员看到的建议从"去掉 emoji 重发"变成"去查选择器"，
    方向全错（T5.2 真机踩过）。

    **两条排除都要忽略不可见字符**（:func:`_drop_invisible`），这是真机 2026-09-23
    补上的：平台在文案末尾塞零宽空格（U+200B）之后，"emoji 被吞"的典型形态变成了
    ``点个关注 😀`` vs ``点个关注\u200b`` —— 只按去 emoji 后的原文比会把那个零宽空格
    留下来，于是它退化成 ``mismatch``，又回到上面那个"方向全错"的老坑。
    """
    if not actual.strip():
        return "empty"
    if _squash(expected) == _squash(actual):
        return "whitespace_only"
    if _squash(_drop_invisible(expected)) == _squash(_drop_invisible(actual)):
        return "invisible_only"
    if _EMOJI.findall(expected) != _EMOJI.findall(actual) and _squash(
        _drop_emoji(_drop_invisible(expected))
    ) == _squash(_drop_emoji(_drop_invisible(actual))):
        return "emoji_stripped"
    return "mismatch"


def _first_difference(left: str, right: str) -> int:
    """第一个不同的下标（两边从头相同时返回较短串的长度）。"""
    for index, (a, b) in enumerate(zip(left, right, strict=False)):
        if a != b:
            return index
    return min(len(left), len(right))


def _squash(text: str) -> str:
    """去掉**全部**空白：编辑器把 ``\\n`` 换成空格、把连续空格并成一个时用它比对。"""
    return "".join(text.split())


def _drop_invisible(text: str) -> str:
    return _INVISIBLE.sub("", text)


def _invisible_codes(expected: str, actual: str) -> tuple[str, ...]:
    """两边对不上的不可见字符（**对称差**：多了的、少了的都算）。

    对称差而不是"实际比期望多出来的那些"：平台**删掉**我们文案里的一个零宽字符同样是
    "只差不可见字符"，只算一侧的话这种情况会回一个空的码点清单 —— 一句"差在哪"都答不上。
    """
    left = set(_INVISIBLE.findall(expected))
    right = set(_INVISIBLE.findall(actual))
    return tuple(sorted(f"U+{ord(ch):04X}" for ch in left ^ right))


def _drop_emoji(text: str) -> str:
    return _EMOJI.sub("", text)
