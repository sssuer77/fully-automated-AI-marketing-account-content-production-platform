"""字幕生成：句级时间轴 → ASS（T3.5 · §04.2.6）。

为什么是 ASS 而不是 drawtext
----------------------------
drawtext 每加一行就要多一个滤镜实例、多一次 -vf 拼接，换行、描边、居中都得自己
算像素坐标；而 libass 一行 ass= 就把这些全做了，且字幕文件本身是**可复用资产**
—— 二次剪辑时重新烧一遍字幕，不必回到渲染器里改代码。代价是引入一个字体依赖，
这正是本模块最麻烦的那部分（见下）。

时间从哪来
----------
**只**取自 voice_master.wav 的逐句实测时长（studio.tts.synth.synthesize_script
里每合成一句就 ffprobe 一次）。禁止自行估算：按字数猜时长，遇上"数字读得慢、
英文读得快"就会出现"字幕比人声早半秒"的错位，而这种错位在成片里极其显眼。

字体：优先 assets/fonts/，退到系统字体目录
-----------------------------------------
§04.2.6 要求字体必须放在 templates/<模板>/assets/fonts/ 且"缺失直接报错"。
resolve_font_dir **确实**在两边都找不到字体时抛 FONT_MISSING（契约成立），
但调用方 plan_subtitle 会把它转成"跳过字幕层 + 记原因"。

这条降级是**刻意**的，理由与水印那次一模一样：早先把"水印缺失"做成硬门禁，
后果是 templates/ 下没有那张 PNG 时**一支片子都出不来** —— 装饰品把整条链路
堵死了。字幕同理：字体没就位时，正确的结果是"这条片子没字幕"，不是"这条片子没有"。

系统字体目录这条退路是 Windows 专用的（本项目本来就是：TTS 走 SAPI、杀进程走
taskkill）。它在**可用性**与**合规**之间选了前者，并把这件事记进 note 如实告诉人
—— 而不是悄悄用一个没放进 assets/fonts/ 的字体。

断行规则
--------
"每行 ≤ N 字" 与 "不许断在数字/英文单词中间" 是两条会打架的规则：iPhone15 是一个
不可分的整体，而一行放不下它的时候，硬塞会溢出、切开会变成 iPho / ne15。本模块的
选择是：**宁可让这一行超一点点，也不切开一个词**（见 wrap_lines 的 capacity 重算）。

另有一条容易漏的：**中文标点不能出现在行首**。所以断行单元不是"字符"而是"分句"
（标点跟在前一个分句的尾巴上），贪心装箱时先装分句、装不下才拆字符。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.config import SubtitleConfig
from studio.core.errors import ErrorCode, RenderError
from studio.core.fonts import system_font_dirs

__all__ = [
    "ASS_FONT_SUFFIXES",
    "ASS_LINE_BREAK",
    "BREAK_AFTER",
    "SPEAKER_STYLES",
    "Cue",
    "FontResolution",
    "SubtitlePlan",
    "assign_speaker_styles",
    "build_ass",
    "build_cues",
    "escape_ass_text",
    "format_ass_time",
    "plan_subtitle",
    "resolve_font_dir",
    "system_font_dirs",
    "wrap_lines",
    "write_ass",
]

#: 认的字体文件扩展名（ttc = TrueType Collection，Windows 的雅黑/宋体都是这个）。
ASS_FONT_SUFFIXES: Final[tuple[str, ...]] = (".ttf", ".otf", ".ttc")

#: ASS 的硬换行。libass 的 \N 是"换行且不重置样式"，\n 是"软换行"（宽度不够时
#: 才断）—— 我们要的是前者，因为每一行放什么已经由 wrap_lines 算好了。
ASS_LINE_BREAK: Final[str] = "\\N"

#: 断行时"可以断在它后面"的标点。中文标点必须**跟在前一句的尾巴上**，
#: 否则会出现"一行以「，」开头"这种排不上版面的东西。
BREAK_AFTER: Final[frozenset[str]] = frozenset("，。！？；：、,.!?;:）】」》…—")

#: 说话人 → 样式名（按**首次出现顺序**分配，见 assign_speaker_styles）。
SPEAKER_STYLES: Final[tuple[str, ...]] = ("SpeakerA", "SpeakerB")

#: 英文 / 数字的"不可分单元"：iPhone15、3.5、2026-09-15 都算一个词。
#: 不切开它们，是因为"3." 换行到 "5" 会把一个数字读成两个。
_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+(?:[.\-'/:][A-Za-z0-9]+)*")

#: 样式表里三种样式共用的颜色（ASS 是 &HAABBGGRR：AA 是**透明度**，
#: 00 完全不透明、FF 全透明 —— 与直觉相反，写反了字幕会直接看不见）。
_PRIMARY_WHITE: Final[str] = "&H00FFFFFF"
_SECONDARY: Final[str] = "&H000000FF"
_OUTLINE_BLACK: Final[str] = "&H00000000"
_SHADOW: Final[str] = "&H64000000"

#: 说话人强调色：A = 金黄，B = 青蓝（都是 &HAABBGGRR 的 BGR 顺序）。
_SPEAKER_COLOURS: Final[dict[str, str]] = {
    "SpeakerA": "&H0000D7FF",
    "SpeakerB": "&H00FFFF00",
}

#: ASS 的样式表列名。**顺序即协议**：少一列 / 换一列，libass 会把 MarginV 当成
#: Encoding 读，表现为"字幕跑到画面顶上去了"。
_ASS_STYLE_FORMAT: Final[str] = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)

_ASS_EVENT_FORMAT: Final[str] = (
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
)


@dataclass(frozen=True, slots=True)
class Cue:
    """一句字幕：屏幕上从 start_ms 显示到 end_ms。

    end_ms <= start_ms 的句子会被 build_ass 丢掉 —— libass 对零长度事件的行为是
    "不显示"，与其在成片里找"为什么少了一句"，不如在生成时就过滤掉。
    """

    start_ms: int
    end_ms: int
    text: str
    style: str = "Main"


@dataclass(frozen=True, slots=True)
class FontResolution:
    """这次字幕用的字体目录（以及它是从哪来的）。"""

    directory: Path
    source: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class SubtitlePlan:
    """字幕这一环的**唯一**结论：贴不贴、贴的是什么、没贴是为什么。"""

    enabled: bool
    ass_path: Path | None
    cues: tuple[Cue, ...]
    font_dir: Path | None
    font_name: str
    skipped_reason: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ass": self.ass_path.as_posix() if self.ass_path else None,
            "cue_count": len(self.cues),
            "font_dir": self.font_dir.as_posix() if self.font_dir else None,
            "font_name": self.font_name,
            "skipped_reason": self.skipped_reason,
            "note": self.note,
        }


# ══════════════════════════════════════════════════════════════════════
# 时间轴 → 字幕事件
# ══════════════════════════════════════════════════════════════════════


def assign_speaker_styles(speakers: Sequence[str]) -> dict[str, str]:
    """说话人 → 样式名（按**首次出现**顺序：第 1 个 SpeakerA、第 2 个 SpeakerB）。

    按出现顺序而不是按名字排，是因为"谁是 A"在单条片子里没有稳定含义，而
    "两个人在对话"这件事有 —— 换一条片子换了说话人名字，颜色分配仍然一致。
    第三个及以后的说话人退回 Main：调色板只有两个强调色，多塞一个只会让观众
    以为那是第三种含义。
    """
    mapping: dict[str, str] = {}
    for speaker in speakers:
        if not speaker or speaker in mapping:
            continue
        mapping[speaker] = SPEAKER_STYLES[len(mapping)] if len(mapping) < len(SPEAKER_STYLES) else "Main"
    return mapping


def build_cues(
    sentences: Sequence[str],
    durations_ms: Sequence[int],
    *,
    speakers: Sequence[str] | None = None,
) -> tuple[Cue, ...]:
    """逐句文本 + 逐句实测时长 → 字幕事件（首尾相接，与拼接后的人声一一对应）。

    这里**不加** pause_after_ms 之类的停顿：人声母带是 concat 拼出来的，句与句
    之间没有额外静音。给字幕加上"规格里应该有"的停顿，字幕就会越飘越远。要加
    停顿，得先让人声真的有停顿（那是 T2.7 的事）。
    """
    if len(sentences) != len(durations_ms):
        raise RenderError(
            f"句子数与时长数对不上：{len(sentences)} 句 / {len(durations_ms)} 个时长",
            code=ErrorCode.RENDER_FAILED,
            context={"sentences": len(sentences), "durations": len(durations_ms)},
            remediation="逐句时长由 synthesize_script 产出，两者必须同源同长",
        )

    styles = assign_speaker_styles(list(speakers)) if speakers is not None else {}
    cues: list[Cue] = []
    cursor = 0
    for index, (sentence, duration) in enumerate(zip(sentences, durations_ms, strict=True)):
        start = cursor
        end = start + max(0, duration)
        cursor = end
        speaker = speakers[index] if speakers is not None and index < len(speakers) else ""
        cues.append(Cue(start_ms=start, end_ms=end, text=sentence, style=styles.get(speaker, "Main")))
    return tuple(cues)


# ══════════════════════════════════════════════════════════════════════
# 断行
# ══════════════════════════════════════════════════════════════════════


def _clauses(text: str) -> list[str]:
    """按标点切成"分句"，标点留在前一个分句的**尾巴**上。"""
    out: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if char in BREAK_AFTER:
            out.append(buffer)
            buffer = ""
    if buffer:
        out.append(buffer)
    return out


def _atomic_units(text: str) -> list[str]:
    """把一段文字拆成"不可再分"的单元：一个词是一整个单元，其余一个字符一个单元。"""
    units: list[str] = []
    cursor = 0
    for match in _WORD.finditer(text):
        if match.start() > cursor:
            units.extend(text[cursor : match.start()])
        units.append(match.group())
        cursor = match.end()
    if cursor < len(text):
        units.extend(text[cursor:])
    return units


def _pack(units: Sequence[str], capacity: int) -> list[str]:
    """贪心装箱：能塞就塞，塞不下就换行；单个单元超长时**独占一行**（不切开它）。"""
    lines: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) > capacity:
            lines.append(current)
            current = ""
        current += unit
    if current:
        lines.append(current)
    return lines


def wrap_lines(text: str, *, max_chars_per_line: int, max_lines: int) -> list[str]:
    """把一句台词排成若干行（纯函数）。

    capacity 会在必要时**放宽**：一段没有标点的长句若按"每行 13 字"排出来是 4 行，
    而规格只允许 2 行 —— 此时正确的做法是把容量抬到"刚好排成 2 行"
    （ceil(总字数 / max_lines)），而不是丢掉后面两句台词，也不是硬压成 2 行让画面
    溢出。字号是固定的，观众不会因为某一行 15 个字而看不懂。

    放宽之后仍可能超过 max_lines（一个超长英文词独占一行时）—— 那就让它超，
    返回的每一行都是**完整**的台词。
    """
    stripped = text.strip()
    if not stripped:
        return []

    lines_cap = max(1, max_lines)
    clauses = _clauses(stripped)
    total = sum(len(clause) for clause in clauses)
    capacity = max(1, max_chars_per_line, -(-total // lines_cap))

    lines: list[str] = []
    current = ""
    for clause in clauses:
        for piece in _pack(_atomic_units(clause), capacity):
            if current and len(current) + len(piece) > capacity:
                lines.append(current)
                current = ""
            current += piece
    if current:
        lines.append(current)
    return [line.strip() for line in lines if line.strip()]


# ══════════════════════════════════════════════════════════════════════
# 字体
# ══════════════════════════════════════════════════════════════════════


def _has_font(directory: Path) -> bool:
    """目录里有没有至少一个字体文件（只有 .gitkeep 的空目录算没有）。"""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return False
    return any(entry.is_file() and entry.name.lower().endswith(ASS_FONT_SUFFIXES) for entry in entries)


def resolve_font_dir(templates_dir: Path) -> FontResolution:
    """找到这次字幕要用的字体目录；**两边都没有 ⇒ 抛 FONT_MISSING**。

    顺序：templates/*/assets/fonts/（§04.2.6 的合规位置，路径靠前的模板赢）
    ⇒ 系统字体目录（可用性退路，会带一句 note）。

    为什么用"目录"而不是"文件"：libass 的 fontsdir 收的是目录，它自己按家族名在
    里面挑字体。让 libass 挑，比我们自己拿文件名去猜家族名靠谱得多（msyh.ttc 的
    家族名是 Microsoft YaHei，文件名里一个字母都对不上）。
    """
    bundled = sorted(
        (path for path in templates_dir.glob("*/assets/fonts") if _has_font(path)),
        key=lambda path: path.as_posix(),
    )
    if bundled:
        return FontResolution(directory=bundled[0], source="assets")

    for candidate in system_font_dirs():
        if _has_font(candidate):
            return FontResolution(
                directory=candidate,
                source="system",
                note=(
                    f"模板里没有字体，这次用了系统字体目录 {candidate}；把 .ttf/.otf 放进 "
                    f"{templates_dir}/<模板>/assets/fonts/ 才算合规（§04.2.6）"
                ),
            )

    raise RenderError(
        f"找不到任何可用字体（模板目录与系统字体目录都没有）：{templates_dir}",
        code=ErrorCode.FONT_MISSING,
        context={
            "templates_dir": templates_dir.as_posix(),
            "system_dirs": [path.as_posix() for path in system_font_dirs()],
        },
        remediation=f"把一个中文字体（.ttf/.otf/.ttc）放进 {templates_dir}/<模板>/assets/fonts/",
    )


# ══════════════════════════════════════════════════════════════════════
# ASS 文本
# ══════════════════════════════════════════════════════════════════════


def escape_ass_text(text: str) -> str:
    """转义 ASS 里有特殊含义的三个字符（与 libass 的 ass_escape() 同一口径）。

    反斜杠开启转义序列，花括号块是**样式覆盖指令**（{\\an8} 之类）。一句台词里
    混进一个左花括号，libass 会把后面一直到右花括号的内容当指令吃掉 —— 观众看到
    的是"字幕少了一截"，而不是报错。
    """
    out: list[str] = []
    for char in text:
        if char in "\\{}":
            out.append("\\")
        out.append(char)
    return "".join(out)


def format_ass_time(ms: int) -> str:
    """毫秒 → ASS 的 H:MM:SS.cc（**厘秒**，两位小数；写三位会被 libass 截断）。"""
    total = max(0, ms)
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


def _style_line(
    name: str,
    *,
    config: SubtitleConfig,
    primary: str,
    margins: tuple[int, int, int],
) -> str:
    margin_left, margin_right, margin_v = margins
    return (
        f"Style: {name},{config.font_name},{config.font_size},{primary},{_SECONDARY},"
        f"{_OUTLINE_BLACK},{_SHADOW},0,0,0,0,100,100,0,0,1,{config.outline},{config.shadow},"
        f"2,{margin_left},{margin_right},{margin_v},1"
    )


def build_ass(
    cues: Sequence[Cue],
    *,
    config: SubtitleConfig,
    canvas: tuple[int, int],
    title: str,
) -> str:
    """字幕事件 + 配置 → 一份完整的 ASS 文本（纯函数；测试直接断言这份字符串）。

    三处"写错也看不出来"的地方：

    - PlayResX/Y 必须是**画布尺寸**。libass 拿它当坐标系，写错（或让它自己从视频
      里猜）会让字号与边距全部等比缩放 —— 表现为"字幕比预期小一半"。
    - WrapStyle: 2 = 不做自动换行，只认显式换行符。默认的 0/1 会在行末自动折行，
      把我们已经排好的两行折成三行。
    - MarginV 取 max(margin_bottom, safe_area.bottom)（§04.2.6 的安全区要求）。
      配置里写小了不会出事，只会被抬到安全区下沿。
    """
    width, height = canvas
    safe = config.safe_area
    margin_v = max(config.margin_bottom, safe.bottom)
    margins = (safe.left, safe.right, margin_v)

    styles = [_style_line("Main", config=config, primary=_PRIMARY_WHITE, margins=margins)]
    for name in SPEAKER_STYLES:
        styles.append(_style_line(name, config=config, primary=_SPEAKER_COLOURS[name], margins=margins))

    head = "\n".join(
        [
            "[Script Info]",
            "; 由 studio 生成（T3.5 · §04.2.6）—— 时间取自 voice_master.wav 的逐句实测时长",
            f"Title: {title.replace(chr(10), ' ')}",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "",
            "[V4+ Styles]",
            _ASS_STYLE_FORMAT,
            *styles,
            "",
            "[Events]",
            _ASS_EVENT_FORMAT,
        ]
    )

    events: list[str] = []
    for cue in cues:
        if cue.end_ms <= cue.start_ms or not cue.text.strip():
            continue
        body = ASS_LINE_BREAK.join(
            escape_ass_text(line)
            for line in wrap_lines(
                cue.text,
                max_chars_per_line=config.max_chars_per_line,
                max_lines=config.max_lines,
            )
        )
        if not body:
            continue
        events.append(
            f"Dialogue: 0,{format_ass_time(cue.start_ms)},{format_ass_time(cue.end_ms)},"
            f"{cue.style},,0,0,0,,{body}"
        )

    return "\n".join([head, *events]) + "\n"


def write_ass(path: Path, body: str) -> Path:
    """落盘：**UTF-8 无 BOM + LF**（§04.2.6）。

    无 BOM 不是为了好看：libass 会把 BOM 当成 [Script Info] 之前的一段文本，某些
    版本据此判定"这不是 ASS 文件"而静默不显示字幕。LF 是为了 golden 比对稳定
    （Windows 上 write_text 默认写 CRLF，会让同一份字幕在不同机器上字节不同）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


# ══════════════════════════════════════════════════════════════════════
# 结论：贴还是不贴
# ══════════════════════════════════════════════════════════════════════


def plan_subtitle(
    config: SubtitleConfig,
    cues: Sequence[Cue],
    *,
    ass_path: Path,
    templates_dir: Path,
    canvas: tuple[int, int],
    title: str,
) -> SubtitlePlan:
    """字幕这一环的**唯一**判断点：能贴 ⇒ 生成 ASS；不能 ⇒ 跳过 + 记原因。

    与 studio.render.watermark.plan_watermark 同一条口径 —— 渲染路径与面板读的是
    同一个结论，于是"面板说会贴字幕"与"成片真的有字幕"不可能对不上。
    """
    if not config.enabled:
        return SubtitlePlan(
            enabled=False,
            ass_path=None,
            cues=tuple(cues),
            font_dir=None,
            font_name=config.font_name,
            skipped_reason="配置里关掉了字幕（subtitle.enabled=False）",
        )

    if not cues:
        return SubtitlePlan(
            enabled=False,
            ass_path=None,
            cues=(),
            font_dir=None,
            font_name=config.font_name,
            skipped_reason="没有句级时间轴（复用母带时找不到逐句音频），跳过字幕",
        )

    try:
        font = resolve_font_dir(templates_dir)
    except RenderError as exc:
        return SubtitlePlan(
            enabled=False,
            ass_path=None,
            cues=tuple(cues),
            font_dir=None,
            font_name=config.font_name,
            skipped_reason=f"找不到字体（{exc.message}），跳过字幕（宁可不贴，也不贴一排豆腐块）",
        )

    body = build_ass(cues, config=config, canvas=canvas, title=title)
    write_ass(ass_path, body)
    return SubtitlePlan(
        enabled=True,
        ass_path=ass_path,
        cues=tuple(cues),
        font_dir=font.directory,
        font_name=config.font_name,
        note=font.note,
    )
