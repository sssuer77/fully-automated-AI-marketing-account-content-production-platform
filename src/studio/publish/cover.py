"""封面合成（T5.1 · §06.3 · 原文 §8 第一步）—— 把 :class:`CoverOutput` 变成一张 1080×1920 的 JPEG。

一次合成的四步
------------------
```text
① 量字   把要画的文案先画到一张黑底上，用 bbox 量出**它到底多宽**
② 算字号 实测宽 > 安全宽 ⇒ 按比例收（下限 60pt）
③ 拼 argv 抽帧（-ss 在 -i 前）→ 缩放铺满→ drawtext 逐段叠加
④ 落盘   先写 .partial 再原子改名（与成片同一条：半张封面比没封面更坏）
```

为什么要"先量后画"
------------------
§06.3 要求文字**强制落在安全区内**，而中文字宽不等。靠字数推宽度，
一个全是"国/图/围"的标题与一个全是"、/，/。"的标题会得到同一个预测宽度，
而它们实际相差可能一倍。量出来的宽度是**唯一能担这件事的证据**。

为什么量的时候用黑底白字
------------------------
``bbox`` 看的是**像素亮度**：透明底上的白字在它眼里是"全幅都亮"（底也算亮），
于是量出来的宽度永远是画布宽度。黑底 + 白字 + ``min_val`` 把背景排除在外，
量到的就只剩字形。（真机实测：透明底量出 1080×1920，黑底量出 667×94。）

为什么高亮词是"先画整行、再重叠"
------------------------------
``drawtext`` 没有富文本，一次只能一个颜色。把整行先用正常色画上去，再把高亮段
用另一个颜色**重叠**在同一位置上：字形完全一致，重叠上去刚刚好盖住，
而先画那一层带的描边还留在外面（不会把边消掉）。若改成"把高亮词从整行里扣掉"，
字间距会变（字符串变短了），后面的字全会往前跑。

降级链（§06.3）
--------
抽帧失败（成片损坏 / 时刻超出）⇒ 纯色底 + 文字（``warn``）；
连纯色底都出不来 ⇒ 返回 ``None``（无封面发布，平台用首帧），**不抛**。
封面是可选装饰 —— 它不值得把一条能发的片子卡死在这一步。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from studio.core.config import CoverConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.fonts import system_font_dirs
from studio.core.logging import get_logger
from studio.core.media import ffmpeg_binary
from studio.core.paths import StudioPaths
from studio.domain.cover import (
    COVER_HEIGHT,
    COVER_WIDTH,
    SAFE_LEFT,
    SAFE_RIGHT,
    SUB_FONT_SIZE,
    TITLE_FONT_SIZE,
    CoverLine,
    CoverOutput,
    CoverWrap,
    cover_block_top,
    cover_plan_payload,
    default_frame_at_ms,
    fit_font_size,
    line_height,
    split_runs,
    strip_trailing_punctuation,
    wrap_cover_text,
)

__all__ = [
    "COVER_BG_COLOR",
    "COVER_JPEG_QSCALE",
    "COVER_MEASURE_TIMEOUT_SEC",
    "COVER_TIMEOUT_SEC",
    "DEFAULT_FRAME_AT_MS",
    "CoverResult",
    "CoverSticker",
    "build_cover",
    "measure_text_width",
    "resolve_frame_at_ms",
]

logger = get_logger("studio.publish.cover")

#: 采样帧的默认时刻（§06.3："``hook`` 句 ``start_ms + 500``）。
#: 真正的值由 :func:`resolve_frame_at_ms` 从时间轴算，这个只是"算不出来时的兜底"。
DEFAULT_FRAME_AT_MS: Final[int] = 1_500

#: 抽不到帧时的纯色底（深灰 —— 白字在它上面永远可读）。
COVER_BG_COLOR: Final[str] = "0x1F2430"

#: 描边的颜色。**实心黑**而不是半透明的 ``black@0.85``：黄字压在跑酷那种花花绿绿的
#: 底上时，半透明描边会让底色的明暗从边里透出来，字缘看着毛。描边要的就是"把字从底里
#: 抠出来"这一件事，抠得越干脆越好。
COVER_OUTLINE_COLOR: Final[str] = "black"

#: 压暗带上下各留的边距。
#:
#: 压暗带本身（画不画、多暗）是 ``config/outputs.yaml`` 的 ``cover.scrim`` ——
#: 它是一道**取舍**：画上去字永远读得清，但底片会变成一张灰图（跑酷那种花花的画面
#: 一压就没了）。取舍归用户，不归这里；这里只管"压暗带比文字块宽出多少"。
COVER_SCRIM_PAD: Final[int] = 28

#: JPEG 质量（§06.3：q=3）。
COVER_JPEG_QSCALE: Final[int] = 3

COVER_TIMEOUT_SEC: Final[int] = 120
COVER_MEASURE_TIMEOUT_SEC: Final[int] = 30

#: 测量用的临时画布：只要比任何一行字宽，不需要 1080 那么宽。
_MEASURE_CANVAS_W: Final[int] = 4096
_MEASURE_CANVAS_H: Final[int] = 256
#: ``bbox`` 的亮度阈值：黑底上只有白字亮于它。
_MEASURE_MIN_VAL: Final[int] = 128

_BBOX_RE = re.compile(r"x1:(\d+) x2:(\d+) y1:(\d+) y2:(\d+) w:(\d+) h:(\d+)")


@dataclass(frozen=True, slots=True)
class CoverSticker:
    """封面上的**主体贴图**：一张透明 PNG + 它在封面上的摆放（T5.1 追加）。

    为什么与成片里那套（``render.sticker``）分开
    --------------------------------------------
    **摆放规则不一样**：成片里人物缩在右下角，是为了给字幕让位；封面上没有字幕，
    人物要当主体、居中站，标题压在它下面。所以这里只留"画在哪、多大"。

    "用哪张图、占多高"仍然是 ``config/outputs.yaml`` 的 ``stickers`` 说了算 ——
    服务层把它解出来之后交给这里。**不在这里解析配置**，是因为解析要探盘
    （``render.png_probe``），而 ``publish/`` 不得 import ``render/``（§02.1）。

    路径是**绝对**的（与 ``render.sticker.StickerSpec.image_path`` 同一条：渲染进程的
    CWD 不保证是仓库根，而这份东西会进 manifest）。
    """

    name: str
    path: Path
    x: int
    y: int
    width_px: int
    height_px: int
    opacity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path.as_posix(),
            "x": self.x,
            "y": self.y,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "opacity": self.opacity,
        }


@dataclass(frozen=True, slots=True)
class CoverResult:
    """一次封面合成的结论（面板 / manifest / artifacts 都读它）。"""

    path: Path | None
    plan: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    #: 纯色底降级了吗（抽帧失败）。
    fallback_background: bool = False

    @property
    def ok(self) -> bool:
        return self.path is not None


def resolve_frame_at_ms(timeline: dict[str, Any] | None, *, fallback: int = DEFAULT_FRAME_AT_MS) -> int:
    """从 ``timeline.json`` 找"开口瞬间"：第一句的 ``start_ms + 500``。

    为什么是第一句而不是"分数最高的那句"：§06.3 说的是 ``hook`` 句，而稿件的
    ``hook`` 就是第一句（:func:`studio.services.review_service` 写稿时就是这么排的）。
    另外 500ms 是**开口瞬间**：第一帧还是闭嘴的。

    算不出来（没时间轴 / 空句表 / 第一句没时间）⇒ 退回 ``fallback``，**不抛**。
    """
    if not timeline:
        return fallback
    sentences = timeline.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        return fallback
    first = sentences[0]
    if not isinstance(first, dict):
        return fallback
    start = first.get("start_ms")
    if not isinstance(start, int):
        return fallback
    # 偏移量只有 domain 那一处说了算（``HOOK_FRAME_OFFSET_MS``）——
    # 这里再写一个字面量 500，改契约时就会漏掉一边。
    return default_frame_at_ms(start)


def _escape_drawtext(text: str) -> str:
    """``drawtext`` 的文本转义。

    三个字符会把过滤器图打断（它们是**过滤器语法**，不是普通文本）：

    - ``\\`` —— 转义符本身，必须**最先**处理（否则会把后面加的反斜杠再转一遍）；
    - ``:`` —— 参数分隔符（``text=a:b`` 会被读成两个参数）；
    - ``'`` —— 值的引号（提前把整个值闭合了）。
    """
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")


def _font_arg(font_file: Path) -> str:
    """``fontfile=`` 的值 —— **带单引号**的 ``'C\\:/Windows/Fonts/msyh.ttc'``。

    为什么驱动器那个冒号要转义：``:`` 是过滤器参数的分隔符，不转义的话
    ``fontfile=C:/Windows/...`` 会被读成"选项名 ``fontfile`` 的值是 ``C``，
    后面还有一堆没有 ``=`` 的 token" ⇒ ``No option name near '/Windows/...'``。

    为什么还要**加单引号**：``-vf`` 的值要过**两层**解析（filtergraph 一层、
    drawtext 选项一层），而只有一层转义的反斜杠会在第一层就被吃掉。
    引号把这段值钉成"一个字面量"，两层都动不了它里面的冒号。

    真机实测（argv 直传、**不经 shell**）：

    | 写法 | 结果 |
    | --- | --- |
    | ``fontfile=C:/Windows/...`` | ✗ ``No option name near '/Windows/...'`` |
    | ``fontfile=C\\:/Windows/...`` | ✗ 同上（那一层反斜杠在第一层就被吃了） |
    | ``fontfile='C\\:/Windows/...'`` | ✓ |

    ⚠️ **在 shell 里手敲单反斜杠会成功** —— shell 先吃掉一层，ffmpeg 收到的其实还是
    ``C:/Windows/...`` 那一层。别拿"我在命令行里试过"当 argv 的证据（陷阱 #130）。

    路径里的反斜杠一律换成正斜杠：反斜杠在本层是转义符，混在里面没人读得懂。
    """
    posix = font_file.as_posix()
    if len(posix) > 1 and posix[1] == ":":
        posix = posix[0] + "\\:" + posix[2:]
    return f"'{posix}'"


def _drawtext(
    *,
    font_arg: str,
    text: str,
    font_size: int,
    x: str,
    y: str,
    color: str,
    outline: int,
) -> str:
    """一个 ``drawtext`` 过滤器。

    描边（``borderw``）不是装饰：底片是跑酷画面，什么颜色都有可能出现在字后面。
    没有描边，字在亮地方就消失了 —— 而用户看到的是"这张封面没标题"，不会去想是对比度的事。

    宽度从 ``config/outputs.yaml`` 的 ``cover.outline`` 来（``0`` = 不描边）：
    底素材越花，描边越要宽。写死一个 6 的话，"这张封面的字看不清"就只能靠改代码。
    """
    parts = [
        f"fontfile={font_arg}",
        f"text='{_escape_drawtext(text)}'",
        f"fontsize={font_size}",
        f"fontcolor={color}",
        f"x={x}",
        f"y={y}",
    ]
    if outline > 0:
        parts.append(f"borderw={outline}")
        parts.append(f"bordercolor={COVER_OUTLINE_COLOR}")
    return "drawtext=" + ":".join(parts)


def _estimate_width(text: str, font_size: int) -> int:
    """量不出来时按"一个汉字一个字宽"估一个（CJK 方块字在等宽意义上就是成立的）。

    为什么**不能**返回 0：调用方拿这个数做两件事 —— 缩字号（:func:`fit_font_size`）与
    居中（:func:`_drawtext_layer` 的 ``(画布宽 - 实测宽) / 2``）。0 会被读成
    "这行一个字都不占"，于是**既不放宽也不居中**，长标题正好溢出到屏幕外。
    估一个偏大的值，最坏结果是字号多缩一点 —— 那比溢出安全得多。
    """
    return max(0, len(text) * font_size)


def measure_text_width(
    text: str,
    *,
    font_file: Path,
    font_size: int,
    ffmpeg: str | None = None,
    runner: Any = None,
) -> int:
    """量一串文字在某个字号下的**实际像素宽度**（量不出来 ⇒ ``0``）。

    为什么不抛：这个数只用来"决定要不要缩字号"，而"量不出来"的合理应对是
    **按原字号画**（宁可可能溢出，也不能因为一次量不出来就丢掉封面）。真正会报错的是合成那一步。
    """
    if text.strip() == "":
        return 0
    command = [
        ffmpeg or ffmpeg_binary(),
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={_MEASURE_CANVAS_W}x{_MEASURE_CANVAS_H}:d=1",
        "-vf",
        # ★ ``bbox`` 必须挂在这一串的最后：量宽度靠的是它打进 ``stderr`` 的那行
        # ``x1:.. x2:.. w:..``。漏掉它，量出来**恒为 0**，而 0 会被下游读成
        # "这行很窄" ⇒ 不缩字号、还按画布中心摆 ⇒ 长标题直接溢出到屏幕外，
        # 日志里一句报错都没有（真机踩过）。
        _drawtext(
            font_arg=_font_arg(font_file),
            text=text,
            font_size=font_size,
            x="0",
            y="0",
            color="white",
            outline=0,
        )
        + f",bbox=min_val={_MEASURE_MIN_VAL}",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    result = (runner or subprocess.run)(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=COVER_MEASURE_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        logger.warning("cover.measure_failed", text=text, rc=result.returncode)
        return _estimate_width(text, font_size)
    found = _BBOX_RE.search(result.stderr)
    if found is None:
        logger.warning("cover.bbox_missing", text=text)
        return _estimate_width(text, font_size)
    return int(found.group(5))


def _drawtext_layer(
    *,
    filters: list[str],
    font_arg: str,
    lines: list[CoverLine],
    font_size: int,
    top: int,
    color: str,
    highlight_color: str,
    outline: int,
    width_of: Any,
) -> None:
    """把一组行按"先整行、再重叠高亮段"的顺序追加到**滤镜链**上。

    追加到一个列表而不是多个 ``-vf``：**ffmpeg 只认最后一个 ``-vf``**，
    写成多个就只剩最后一句文字，而前面那几句不报错地消失 ——
    封面上会只有一行字，而日志里什么异常都没有。真机踩过（陷阱 #130）。

    ``width_of`` 是一个 ``(text, font_size) -> int`` 的取宽函数（注入以便单测不真跑 ffmpeg）。
    居中靠累加段宽算：段与段之间字间距为 0（同一个字体、同一个字号）。
    """
    for index, line in enumerate(lines):
        y = str(top + index * line_height(font_size))
        total = sum(width_of(run.text, font_size) for run in line.runs)
        start = max(SAFE_LEFT, (COVER_WIDTH - total) // 2)
        cursor = start
        for run in line.runs:
            filters.append(
                _drawtext(
                    font_arg=font_arg,
                    text=run.text,
                    font_size=font_size,
                    x=str(cursor),
                    y=y,
                    color=highlight_color if run.highlight else color,
                    outline=outline,
                )
            )
            cursor += width_of(run.text, font_size)


def _compose_argv(
    *,
    ffmpeg: str,
    source: Path | None,
    frame_at_ms: int,
    title: CoverWrap,
    sub: CoverWrap,
    output: CoverOutput,
    title_size: int,
    sub_size: int,
    target: Path,
    font_file: Path,
    width_of: Any,
    style: CoverConfig,
    sticker: CoverSticker | None,
) -> list[str]:
    """组装一条 ffmpeg 命令（抽帧 + 缩放铺满 + 贴图 + 文字）。

    ``-ss`` 放在 ``-i`` **前面**：那是"跳过去再解"。放在后面会把前面那么多秒全解一遍，
    而封面只要一帧。（与 :func:`studio.core.media.extract_thumbnail` 同一条口径。）

    ``scale=...:force_original_aspect_ratio=increase,crop``：先按短边铺满、再裁成 9:16。
    直接 ``scale=1080:1920`` 会把横屏底片拉成高瘦的，人脸会变形。

    ★ 有贴图时**必须**从 ``-vf`` 换成 ``-filter_complex``
    ---------------------------------------------------
    ``-vf`` 只吃**一路**输入。贴图是第二路（``-i hero.png``），两路要在同一条滤镜图里
    相叠，所以只能写 ``-filter_complex`` + ``-map``。两条路都留着（而不是一律用
    ``filter_complex``）是为了：没有贴图时命令与改前**逐字节相同** —— 那一串在真机上
    验过很多遍，不该为了"少一个分支"把它换掉。
    """
    command = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error"]
    #: 背景那一段滤镜。``None`` = 背景是 lavfi 纯色（它本来就是画布尺寸，不用缩放）。
    background: str | None = None
    if source is None:
        command.extend(["-f", "lavfi", "-i", f"color=c={COVER_BG_COLOR}:s={COVER_WIDTH}x{COVER_HEIGHT}:d=1"])
    else:
        command.extend(["-ss", f"{max(0, frame_at_ms) / 1000:.3f}", "-i", str(source)])
        background = (
            f"scale={COVER_WIDTH}:{COVER_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={COVER_WIDTH}:{COVER_HEIGHT}"
        )
    # 贴图**永远是输入 1**（背景恒为输入 0）—— 不管背景是抽的帧还是 lavfi 纯色。
    if sticker is not None:
        command.extend(["-i", str(sticker.path)])

    #: 文字那一段（压暗带 + 逐段 ``drawtext``）。顺序即执行顺序。
    filters: list[str] = []

    font_arg = _font_arg(font_file)
    title_lines = [
        CoverLine(text=line, runs=split_runs(line, output.highlight_words)) for line in title.lines
    ]
    sub_lines = [CoverLine(text=line, runs=split_runs(line, [])) for line in sub.lines]

    block_height = len(title_lines) * line_height(title_size) + len(sub_lines) * line_height(sub_size)
    top = cover_block_top(block_height)
    # 压暗带**先画**（滤镜链是按顺序执行的），文字才叠在它上面。
    # 高度按文字块算而不是铺满全屏：整屏压暗会让底片变成一张灰图，封面就不像封面了。
    if style.scrim > 0:
        scrim_top = max(0, top - COVER_SCRIM_PAD)
        scrim_bottom = min(COVER_HEIGHT, top + block_height + COVER_SCRIM_PAD)
        filters.append(
            f"drawbox=x=0:y={scrim_top}:w={COVER_WIDTH}:h={scrim_bottom - scrim_top}"
            f":color=black@{style.scrim:g}:t=fill"
        )
    _drawtext_layer(
        filters=filters,
        font_arg=font_arg,
        lines=title_lines,
        font_size=title_size,
        top=top,
        color=style.title_color,
        highlight_color=style.highlight_color,
        outline=style.outline,
        width_of=width_of,
    )
    if sub_lines:
        _drawtext_layer(
            filters=filters,
            font_arg=font_arg,
            lines=sub_lines,
            font_size=sub_size,
            top=top + len(title_lines) * line_height(title_size),
            color="white@0.92",
            highlight_color=style.highlight_color,
            outline=style.outline,
            width_of=width_of,
        )

    if sticker is None:
        # ★ 单条 ``-vf``（逗号连成一条滤镜链）—— 多个 ``-vf`` 只会生效最后一个。
        head = [] if background is None else [background]
        command.extend(["-vf", ",".join(head + filters), "-frames:v", "1"])
    else:
        command.extend(["-filter_complex", _filter_graph(background, filters, sticker), "-map", "[out]"])
        command.extend(["-frames:v", "1"])
    command.extend(["-q:v", str(COVER_JPEG_QSCALE), "-y", str(target)])
    return command


def _filter_graph(background: str | None, text_filters: list[str], sticker: CoverSticker) -> str:
    """有贴图时的 ``-filter_complex`` 图（三段：背景 → 叠贴图 → 画字）。

    为什么拆成一条 ``filter_complex`` 而不是多个 ``-vf``：见 :func:`_compose_argv`。

    贴图那一段与成片里那条**逐字同构**（``scale`` ⇒ ``format=rgba`` ⇒
    ``colorchannelmixer=aa=``）：``overlay`` 要在 alpha 上合成，而抽出来的帧是
    ``yuv420p``；不先转 ``rgba``，透明度会被当成亮度用 —— 人物周围会出现一圈黑。
    """
    parts: list[str] = []
    parts.append("[0:v]null[bg]" if background is None else f"[0:v]{background}[bg]")
    parts.append(
        f"[1:v]scale={sticker.width_px}:{sticker.height_px},format=rgba,"
        f"colorchannelmixer=aa={sticker.opacity:g}[stk]"
    )
    parts.append(f"[bg][stk]overlay={sticker.x}:{sticker.y}:format=auto[ov]")
    parts.append("[ov]" + ",".join(text_filters) + "[out]")
    return ";".join(parts)


def resolve_cover_font(paths: StudioPaths) -> Path:
    """找一个能画中文的字体：``assets/fonts/`` 优先，退到系统字体目录。

    与字幕（:func:`studio.render.subtitle.resolve_font_dir`）同一条顺序，但**不共用同一个函数**：
    字幕要的是"一个目录"（libass 自己按族名找），封面要的是"一个文件"（``fontfile=``）。
    把两种取法揉成一个只会让两边都别扭。

    都没有 ⇒ 抛 ``FONT_MISSING``（封面**没有字体就是一张空图**，这与水印那种"没有就跳过"的可选层不同）。
    """
    roots = [paths.home / "assets" / "fonts", paths.templates_dir]
    for root in roots:
        if not root.is_dir():
            continue
        found = _first_font(root)
        if found is not None:
            return found
    for directory in system_font_dirs():
        found = _first_font(directory)
        if found is not None:
            return found
    raise StudioError(
        "找不到可用中文字体，封面无法生成",
        code=ErrorCode.FONT_MISSING,
        context={"searched": [path.as_posix() for path in roots]},
        remediation="把一个中文字体（.ttf/.otf/.ttc）放进 assets/fonts/",
    )


#: 中文字体的优先顺序（先找它们，找不到才退到"目录里的第一个字体”）。
#: 顺序即偏好：黑体比宋体更适合封面大字（笔画粗、远看清楚）。
_PREFERRED_FONTS: Final[tuple[str, ...]] = (
    "msyhbd.ttc",
    "msyh.ttc",
    "simhei.ttf",
    "SourceHanSansSC-Bold.otf",
    "NotoSansCJKsc-Bold.otf",
)

_FONT_SUFFIXES: Final[tuple[str, ...]] = (".ttf", ".otf", ".ttc")


def _first_font(root: Path) -> Path | None:
    """在一个目录（可能是 templates 的上层）里找字体：先按偏好名，再扫全目录。"""
    for name in _PREFERRED_FONTS:
        candidate = root / name
        if candidate.is_file():
            return candidate
    for candidate in sorted(root.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() in _FONT_SUFFIXES:
            return candidate
    return None


def build_cover(
    *,
    output: CoverOutput,
    target: Path,
    paths: StudioPaths,
    source: Path | None,
    sticker: CoverSticker | None = None,
    style: CoverConfig | None = None,
    ffmpeg: str | None = None,
    runner: Any = None,
    width_of: Any = None,
) -> CoverResult:
    """把一份 :class:`CoverOutput` 画成 1080×1920 的 JPEG。

    降级链写在这里：抽帧失败 ⇒ 纯色底（``fallback_background=True`` + 一条 ``warn``）；
    连纯色底都失败 ⇒ ``path=None``（无封面发布）。**两种降级都不抛**。

    ``sticker`` 由**服务层**解好（它才知道 ``config/outputs.yaml`` 里有哪些层、图在不在
    盘上）。这里只负责"把它画上去" —— 传 ``None`` ⇒ 与改前逐字节相同的命令。
    ``style`` 缺省时用 :class:`CoverConfig` 的默认值（黄字黑边）。
    """
    warnings: list[str] = []
    style = style or CoverConfig()
    font_file = resolve_cover_font(paths)
    take_width = width_of or (
        lambda text, size: measure_text_width(
            text, font_file=font_file, font_size=size, ffmpeg=ffmpeg, runner=runner
        )
    )

    title = wrap_cover_text(
        strip_trailing_punctuation(output.title_text),
        max_lines=2,
        max_chars=10,
    )
    sub = (
        wrap_cover_text(strip_trailing_punctuation(output.sub_text), max_lines=1, max_chars=12)
        if output.sub_text
        else CoverWrap(lines=())
    )
    if title.truncated:
        warnings.append(f"主文案超出 2 行 × 10 字，已截断 {title.dropped_chars} 字")
    if sub.truncated:
        warnings.append(f"次文案超出 1 行 × 12 字，已截断 {sub.dropped_chars} 字")

    title_size = TITLE_FONT_SIZE
    widest = max((take_width(line, title_size) for line in title.lines), default=0)
    title_size = fit_font_size(title_size, measured_width=widest, safe_width=SAFE_RIGHT - SAFE_LEFT)
    if title_size < TITLE_FONT_SIZE:
        warnings.append(f"主文案实测 {widest}px 超安全宽，字号从 {TITLE_FONT_SIZE} 收到 {title_size}")

    sub_size = SUB_FONT_SIZE
    if sub.lines:
        widest_sub = max(take_width(line, sub_size) for line in sub.lines)
        sub_size = fit_font_size(
            sub_size,
            measured_width=widest_sub,
            safe_width=SAFE_RIGHT - SAFE_LEFT,
            minimum=32,
        )

    plan = cover_plan_payload(output=output, title=title, sub=sub, title_size=title_size, sub_size=sub_size)
    plan["warnings"] = list(warnings)
    # 画了什么就得记什么：这张封面上的颜色 / 描边 / 主体是谁，全都跟着 plan 走。
    # 只留一句"出过一张封面"的话，下一个人看到一张不合意的图只能猜是哪一步的事。
    plan["sticker"] = None if sticker is None else sticker.to_dict()
    plan["style"] = {
        "title_color": style.title_color,
        "highlight_color": style.highlight_color,
        "outline": style.outline,
        "scrim": style.scrim,
    }

    binary = ffmpeg or ffmpeg_binary()
    for use_source in (source, None):
        argv = _compose_argv(
            ffmpeg=binary,
            source=use_source,
            frame_at_ms=output.frame_at_ms,
            title=title,
            sub=sub,
            output=output,
            title_size=title_size,
            sub_size=sub_size,
            target=target,
            font_file=font_file,
            width_of=take_width,
            style=style,
            sticker=sticker,
        )
        try:
            result = (runner or subprocess.run)(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COVER_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            result = None
            reason = str(exc)
        else:
            # 显式 ``is not None`` 而不是靠 else 分支的语义：``runner`` 是注入的，
            # 它的返回类型是 ``Any`` —— 少这一句，mypy 会把"返回 None"也当成可能。
            reason = (
                (result.stderr or "").strip().splitlines()[-1]
                if result is not None and result.returncode != 0
                else ""
            )
        if result is not None and result.returncode == 0 and target.is_file():
            fallback = use_source is None
            if fallback and source is not None:
                warnings.append(f"抽帧失败，已退成纯色底：{reason}")
            return CoverResult(path=target, plan=plan, warnings=warnings, fallback_background=fallback)
        if use_source is not None:
            logger.warning("cover.frame_failed", source=str(source), reason=reason)
            continue
        warnings.append(f"封面生成失败（连纯色底都没出来）：{reason}")
        logger.warning("cover.solid_failed", reason=reason)
        return CoverResult(path=None, plan=plan, warnings=warnings, fallback_background=True)
    return CoverResult(path=None, plan=plan, warnings=warnings)  # pragma: no cover —— 上面已穷举


def write_cover_atomically(result: CoverResult, target: Path) -> Path | None:
    """把 ``.partial`` 改成正式名（半张封面比没封面更坏）。

    与成片那一条同一个理由：下游（发布面板 / 上传）只认最终名，
    而一个写了一半的 JPEG 在目录里**看起来就是一张能用的封面**。
    """
    partial = result.path
    if partial is None:
        return None
    if partial == target:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(partial), str(target))
    return target
