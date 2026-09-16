"""封面文案与排版（T5.1 · §04.1.8 / §06.3 · 原文 §8 第一步）。

分工（契约写死的）
------------------
本模块只答两件事：**封面上写什么字**（:class:`CoverOutput`）与**这些字怎么摆**（换行 / 分段着色 / 字号收缩 / 安全区）。
真正把字画到图上是 :mod:`studio.publish.cover` 的事 —— 那一半是 ffmpeg 子进程。
拆开的理由与规格书一致：文案可被审稿与禁区扫描，合成可被单测。

为什么换行不能只算字数
------------------------
中文字宽并不相等（一个「。」与一个「国」字宽就不一样），但**对封面这个量级来说，
字数就是足够好的一级近似**：真正的"溢出与否"由合成那一步用 bbox **量出来**的宽度判定
（见 :func:`fit_font_size`）。两层各管一件事：这里管"断在哪里好看"，那里管"放不放得下"。
如果把两件事都压在字数上，一个全是标点的标题会被误判成合格。

为什么高亮要拆成"段"
----------------------
``drawtext`` 没有富文本：一次只能一个颜色。要让个别词换色，只能**把一行拆成若干段、每段一个 drawtext**
（坐标逐段累加）。所以"拆段"是排版层的活，不是合成层的临时拼接 —— 合成层只负责把每一段画到给定的 x 上。
"""  # noqa: E501

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "COVER_HEIGHT",
    "COVER_WIDTH",
    "HOOK_FRAME_OFFSET_MS",
    "SAFE_BOTTOM",
    "SAFE_LEFT",
    "SAFE_RIGHT",
    "SAFE_TOP",
    "SUB_FONT_SIZE",
    "SUB_MAX_CHARS",
    "TITLE_FONT_MIN",
    "TITLE_FONT_SIZE",
    "TITLE_MAX_CHARS",
    "TITLE_MAX_LINES",
    "CoverInput",
    "CoverLine",
    "CoverOutput",
    "CoverRun",
    "CoverWrap",
    "cover_block_top",
    "cover_plan_payload",
    "default_frame_at_ms",
    "fit_font_size",
    "line_height",
    "rule_cover_output",
    "split_runs",
    "strip_trailing_punctuation",
    "wrap_cover_text",
]

#: 封面画布（§06.3：竖屏 1080×1920）。
COVER_WIDTH: Final[int] = 1080
COVER_HEIGHT: Final[int] = 1920

#: 抽帧点相对 ``hook`` 句起点的偏移（§06.3 逐字：``start_ms + 500``）。
#:
#: 为什么不是 ``start_ms``：那一帧人**还没开口**（嘴是闭的、字幕也还没上），
#: 截下来像一张静止的截图。500ms 是刚过开口、表情最自然的那一格。
HOOK_FRAME_OFFSET_MS: Final[int] = 500

#: 安全区（§06.3"强制落在安全区内"）。
#:
#: 上下留白不是美学取舍，是**平台 UI 会盖上去**：顶部是任务栏 / 时间，底部是播放控件与文案。
#: 字压在那两条上，再漂亮的封面也是白做。
SAFE_TOP: Final[int] = 230
SAFE_BOTTOM: Final[int] = 1632
SAFE_LEFT: Final[int] = 76
SAFE_RIGHT: Final[int] = COVER_WIDTH - SAFE_LEFT

#: 主文案：≤2 行 × ≤10 字（§04.1.8 / §06.3）。
TITLE_MAX_LINES: Final[int] = 2
TITLE_MAX_CHARS: Final[int] = 10
#: 次文案：一行（字号小一半，两行会把主文案挤出屏）。
SUB_MAX_CHARS: Final[int] = 12

#: 字号（§06.3"最小 60pt"是自动缩放的下限）。
TITLE_FONT_SIZE: Final[int] = 96
TITLE_FONT_MIN: Final[int] = 60
SUB_FONT_SIZE: Final[int] = 52

#: 行高倍率（行距 = 字号 × 这个数）。
#: 1.0 会让两行贴在一起（中文字面比字号高），1.3 以上又散得像两个段落。
_LINE_HEIGHT_RATIO: Final[float] = 1.18

#: 换行优先断在这些字符之后（中文里"逗号换行"比"硬断词"好看得多）。
_BREAK_AFTER: Final[str] = "，。！？、；：—…"

#: 不能落在行首的标点（中文排版的基本规矩）。
_NO_LINE_START: Final[str] = "，。！？、；：”’）】》」"


class CoverOutput(BaseModel):
    """Cover Agent 的输出（§04.1.8 逐字）。

    它**不含图像**：只有文案与一个时刻。合成是另一步（:mod:`studio.publish.cover`），
    所以这份东西可以单独审、单独扫禁区、单独留痕。
    """

    model_config = ConfigDict(extra="forbid")

    title_text: str = Field(min_length=1, max_length=20)
    sub_text: str | None = Field(default=None, max_length=24)
    #: 高亮词的上限**必须与 schema 逐字一致**（``maxItems: 4`` / 每项 ``maxLength: 20``）。
    #: 少一条，模型给 5 个词时 schema 拦得住、而**服务层与规则兜底直接构造的这个模型拦不住** ——
    #: 两条路各自校验一次，却只有一条真的在把关。
    highlight_words: list[Annotated[str, Field(min_length=1, max_length=20)]] = Field(
        default_factory=list, max_length=4
    )
    frame_at_ms: int = Field(ge=0)
    banned_checked: bool = False


class CoverInput(BaseModel):
    """封面文案生成的输入（一版已生效稿件的摘要）。

    为什么只给摘要而不给全文：封面只能写 20 字，而全文 600–800 字 ——
    把全文塞进去只会让模型把"摘要"做成"缩写"。真正有用的是钩子与 CTA：
    它们本来就是按"一句话抢人"写的。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str = ""
    hook: str = ""
    cta: str = ""
    #: 首句的起始时刻（毫秒）—— 模型选抽帧点时的参考值（§06.3：默认 ``start_ms + 500``）。
    hook_start_ms: int = 0
    #: 成片时长（毫秒）—— 把模型选的时刻**钳在片子里**，避免抽到片尾之外。
    duration_ms: int = 0
    #: 频道禁区词（封面文案不得命中）。
    forbidden: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CoverWrap:
    """换行结果（纯函数的输出，不碰磁盘）。

    ``truncated`` 与 ``dropped_chars`` 一起回答"我是否偷偷删了你的字"。
    它们必须能传到面板与日志上：一个被静默截断的标题会让人以为"模型就写了这么几个字"。
    """

    lines: tuple[str, ...]
    truncated: bool = False
    dropped_chars: int = 0

    @property
    def text(self) -> str:
        return "".join(self.lines)


@dataclass(frozen=True, slots=True)
class CoverRun:
    """一行里的一段：``highlight=True`` 的那些用另一个颜色画。"""

    text: str
    highlight: bool


@dataclass(frozen=True, slots=True)
class CoverLine:
    """要画的一行：原文 + 拆好的段（没有高亮词时只有一段）。"""

    text: str
    runs: tuple[CoverRun, ...]

    @property
    def plain(self) -> bool:
        return len(self.runs) == 1 and not self.runs[0].highlight


def _split_long(text: str, limit: int) -> list[str]:
    """把一串文字按 ``limit`` 切开，尽量断在标点之后。

    两道约束：① 优先在标点后断；② 不让标点落在下一行开头。
    第 ② 条看上去像小事，但"你好，" 起头的第二行在 1080×1920 的封面上非常刺眼。
    """
    lines: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[: limit + 1]
        cut = -1
        for index, char in enumerate(window):
            if char in _BREAK_AFTER and index < len(rest) - 1:
                cut = index + 1
        if cut <= 0:
            cut = limit
            while cut > 1 and rest[cut] in _NO_LINE_START:
                cut -= 1
        lines.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        lines.append(rest)
    return lines


def wrap_cover_text(
    text: str,
    *,
    max_lines: int,
    max_chars: int,
) -> CoverWrap:
    """把一串文字换成 ``≤ max_lines`` 行、每行 ``≤ max_chars`` 字。

    超出容量 ⇒ **截断**并如实记账（§06.3："仍溢出 ⇒ 截断 + warn"）。
    不能改成"缩小字号到能塞下"—— 那会把一个 40 字的标题压成蚊子腿，封面上谁也看不清。
    """
    cleaned = text.strip()
    if cleaned == "":
        return CoverWrap(lines=())
    raw = _split_long(cleaned, max_chars)
    if len(raw) <= max_lines:
        return CoverWrap(lines=tuple(raw))
    kept = raw[:max_lines]
    dropped = sum(len(line) for line in raw[max_lines:])
    return CoverWrap(lines=tuple(kept), truncated=True, dropped_chars=dropped)


def split_runs(line: str, highlight_words: list[str]) -> tuple[CoverRun, ...]:
    """把一行按高亮词拆成若干段（顺序与原文一致，不重叠）。

    匹配是**子串**且**从左到右最早匹配**：高亮词不在这一行里 ⇒ 跳过（不报错）。
    报错反而更坏：模型给的高亮词没落到换行后的某一行里，是**常态**而不是异常。
    """
    if not highlight_words:
        return (CoverRun(text=line, highlight=False),)
    hits: list[tuple[int, int]] = []
    for word in highlight_words:
        if not word:
            continue
        start = line.find(word)
        if start < 0:
            continue
        end = start + len(word)
        if any(start < other_end and other_start < end for other_start, other_end in hits):
            continue
        hits.append((start, end))
    if not hits:
        return (CoverRun(text=line, highlight=False),)
    hits.sort()
    runs: list[CoverRun] = []
    cursor = 0
    for start, end in hits:
        if start > cursor:
            runs.append(CoverRun(text=line[cursor:start], highlight=False))
        runs.append(CoverRun(text=line[start:end], highlight=True))
        cursor = end
    if cursor < len(line):
        runs.append(CoverRun(text=line[cursor:], highlight=False))
    return tuple(runs)


def fit_font_size(
    base: int,
    *,
    measured_width: int,
    safe_width: int,
    minimum: int = TITLE_FONT_MIN,
) -> int:
    """按实测宽度把字号收到能放下（下限 ``minimum``）。

    为什么是"一次算出来"而不是"一档一档试""
    ------------------------------------------------
    字宽与字号在这个区间里几乎是线性的（同一个字体、同一串字），所以
    ``新字号 = 基准字号 × 安全宽 / 实测宽`` 一次就到位。逐档试的代价是**每一档一次 ffmpeg**，
    而封面是要在发布前的关口上现算的，每多一秒都是白等。
    线性假设不成立时（字号小到某个程度字宽不再成比例），最坏结果是**字号多缩一点** ——
    那比溢出到屏幕外安全得多。
    """
    if measured_width <= 0 or safe_width <= 0:
        return base
    if measured_width <= safe_width:
        return base
    scaled = int(base * safe_width / measured_width)
    return max(minimum, min(base, scaled))


def line_height(font_size: int) -> int:
    """该字号下的行距（向上取整：不足一像素的缩放会让两行贴住）。"""
    return int(font_size * _LINE_HEIGHT_RATIO) + 1


def cover_block_top(block_height: int) -> int:
    """文字块的顶端 y：底部对齐、向上码，冲出安全区就压回来。

    为什么是"底部对齐"：一行标题与两行标题的封面，人眼里那块字的**下边缘**
    应该在同一个位置（否则一批封面摆在一起上下跳）。
    """
    bottom = SAFE_BOTTOM
    top = bottom - block_height
    return max(SAFE_TOP, top)


def cover_plan_payload(
    *,
    output: CoverOutput,
    title: CoverWrap,
    sub: CoverWrap,
    title_size: int,
    sub_size: int,
) -> dict[str, Any]:
    """封面的"算了什么"留痕（进 manifest / 面板 / artifacts.meta_json）。

    它回答的是"这张封面为什么长这样"：字号被缩过吗、文案被截过吗。
    只写最终图像的话，下一个人看到小字只能猜"是不是模型就写了这么少字"。
    """
    return {
        "title_text": output.title_text,
        "title_lines": list(title.lines),
        "title_truncated": title.truncated,
        "sub_text": output.sub_text,
        "sub_lines": list(sub.lines),
        "highlight_words": list(output.highlight_words),
        "frame_at_ms": output.frame_at_ms,
        "title_font_size": title_size,
        "sub_font_size": sub_size,
        "canvas": f"{COVER_WIDTH}x{COVER_HEIGHT}",
    }


_RE_TRAILING_PUNCT = re.compile(r"[。！？，、；：…]+$")


def strip_trailing_punctuation(text: str) -> str:
    """去掉末尾标点（封面上不写句号 —— 它只占地方、不传信息）。"""
    return _RE_TRAILING_PUNCT.sub("", text.strip())


def default_frame_at_ms(hook_start_ms: int) -> int:
    """§06.3 的默认抽帧点：``hook`` 句 ``start_ms + HOOK_FRAME_OFFSET_MS``。

    钳在 ``>= 0``：模型 / 库里给来的起点可能是负数（时间轴被手工改过），
    而 ``ffmpeg -ss`` 收到负数会直接报错 —— 那会让封面整条降级成纯色底。
    """
    return max(0, hook_start_ms + HOOK_FRAME_OFFSET_MS)


def rule_cover_output(payload: CoverInput, *, frame_at_ms: int) -> CoverOutput:
    """**不调 LLM** 的封面文案（规则兜底）。

    什么时候用它
    ------------
    1. 本地 / 云端通道都不可用（离线演练、模型挂了）；
    2. 模型给的文案**命中禁区词**（合规红线，不能靠重试赌它第二次不犯）；
    3. 模型返回的东西过不了 schema。

    为什么不干脆"没模型就不出封面"
    ------------------------------
    封面是**可选装饰**（§06.3：没有封面就用首帧），但"没有封面"与"没有**试过**出封面"
    在面板上是两件事。用标题当主文案、CTA 当次文案，最坏情况是一张朴素但**准确**的封面，
    比一张空图有用。

    为什么标题优先于钩子：标题本来就是按"一句话概括这一期"写的（Director 产出），
    而钩子是按"前三秒抓住人"写的 —— 后者更像口播的第一句，不像封面标题。
    标题缺失才退到钩子。
    """
    title = strip_trailing_punctuation(payload.title) or strip_trailing_punctuation(payload.hook)
    sub = strip_trailing_punctuation(payload.cta) or strip_trailing_punctuation(payload.hook)
    if sub == title:
        # 次文案与主文案一样 ⇒ 留空：同一句话写两遍是噪音，不是信息。
        sub = ""
    return CoverOutput(
        title_text=title[:20] or "无标题",
        sub_text=sub[:24] or None,
        # 兜底文案**不做高亮**：高亮是"模型认为这个词最关键"的表达，
        # 规则挑出来的词没有这个判断，涂成黄色只会误导。
        highlight_words=[],
        frame_at_ms=max(0, frame_at_ms),
        banned_checked=False,
    )
