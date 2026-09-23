"""单遍合成与最终导出（T3.3/T3.4/T3.5/T3.6 · §04.2.8）—— 一条 ffmpeg 命令出 final.mp4。

流水线（**一遍编码，无中间产物**）
----------------------------------
```text
跑酷底片（-stream_loop -1 循环）
      │
      ├─ scale=W:H:force_original_aspect_ratio=increase → crop=W:H   缩放铺满
      ├─ fps=<profile.fps> → setsar=1                                恒定帧率 + 方形像素
      ├─ overlay=x:y   × N                  （**有贴图才加这几层**）  人物贴图（在字幕**下**面）
      ├─ ass=<subtitle.ass>:fontsdir=…      （**有字幕才加这一层**）  烧字幕
      ├─ overlay=x:y                        （**水印存在才加这一层**）水印（在最上面）
      │
配音 ─┤ 见 render/mixdown.py：侧链避让 → amix(normalize=0) → 两遍 loudnorm → alimiter
      │
      └─ -t <voice_ms + tail_ms> → 编码参数（profiles.output_args）→ final.mp4
```

四个刻意的取舍
--------------
1. **时长以人声为准**：``-t = ffprobe(voice_master) + tail_ms``（ADR-005「音频为时长
   真相」）。画面被 ``-stream_loop -1`` 拉成无限长，不给人声留出"画面先结束"的机会。
2. **不用 ``-shortest``**：画面是无限循环流，``-shortest`` 的语义会随输入组合漂移；
   显式 ``-t`` 才是可预测的（profile 的用例里也钉着"不许出现 -shortest"）。
3. **水印 / BGM / 字幕都是可选层**：滤镜图按"有没有"拼，缺了就直接不拼那一层 ——
   没有"报错退出"这条分支（成片优先，装饰其次）。
4. **响度是两遍的**：:func:`run_composite` 先跑一次**纯音频**的测量（见
   ``render/mixdown.py`` 里"为什么两遍 loudnorm"），再把读数喂给正式那一遍。

为什么把滤镜图拼成字符串而不是常量模板
--------------------------------------
四处开关（字幕 / 水印 / BGM / 音频路数）组合出八种图，写八份模板必然漂移；拼装只有
一处，且 :func:`build_composite_argv` 是**纯函数** —— 测试能直接断言 argv，不必真跑
ffmpeg。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from studio.core.errors import ErrorCode, RenderError
from studio.core.media import ffmpeg_binary, run_command
from studio.render.mixdown import LoudnessMeasurement, MixSettings, build_audio_chain, measure_mix
from studio.render.profiles import CompositeProfile
from studio.render.speech import Interval
from studio.render.sticker import StickerPlan
from studio.render.watermark import WatermarkPlan

__all__ = [
    "BG_FILL_BLACK",
    "BG_FILL_BROLL",
    "PROGRESS_MIN_INTERVAL_SEC",
    "PROGRESS_STATS_PERIOD_SEC",
    "PROGRESS_TOTAL",
    "RENDER_TIMEOUT_SEC",
    "CompositeRequest",
    "CompositeResult",
    "ProgressThrottle",
    "bg_fill",
    "build_composite_argv",
    "build_filter_graph",
    "duration_ms_for",
    "enable_expr",
    "filter_path_arg",
    "parse_out_time_us",
    "progress_percent",
    "run_composite",
    "video_label",
]

#: 一次合成的超时。给 30 分钟：1080×1920 的单遍编码在 8 核机器上大约 1–3 倍实时，
#: 一条 2 分钟的片子几分钟内能出；超时是"卡死了"的信号，不是"有点慢"。
RENDER_TIMEOUT_SEC: Final[int] = 1800

#: ffmpeg 进度流的节拍（秒）。0.5s ⇒ **2Hz**（T3.4）：这是"机器可读的进度"该有的频率 ——
#: 更密只是把同一句话重复给面板看，更疏会让进度条一跳一跳。
PROGRESS_STATS_PERIOD_SEC: Final[float] = 0.5

#: 渲染这一段进度的分母。它是**百分比**，不是"第几段"：编码是一条连续的动作，
#: 说"第 1 段 / 共 1 段"等于没说（T3.4 之前就是这个样子）。
PROGRESS_TOTAL: Final[int] = 100

#: 两拍之间至少隔多久（秒）—— 2Hz 限流。见 :class:`ProgressThrottle` 里"为什么 ffmpeg
#: 那边已经有节拍了还要再限一道"。
PROGRESS_MIN_INTERVAL_SEC: Final[float] = 0.5

#: 编码期间能报到的最大百分比。**100 只在 ffmpeg 退出、``.partial`` 改名之后报**：
#: 提前报满会得到"进度条 100% 而成片还没落盘"，而那一刻用户已经在点播放了。
_ENCODE_CEILING: Final[int] = 99


@dataclass(frozen=True, slots=True)
class CompositeRequest:
    """一次合成要的全部输入（**只描述，不执行** —— 纯数据便于断言与留痕）。"""

    profile: CompositeProfile
    #: 画面来源。``None`` ⇒ **黑屏降级**：现造一块纯黑当底（§04.2.8.6）。
    clip: Path | None
    voice: Path
    output: Path
    duration_ms: int
    watermark: WatermarkPlan | None = None
    #: 人物贴图（T6.5）。**声明顺序 = 叠放顺序**（先声明的在下面）。
    #: 空元组 ⇒ 一层都不贴，滤镜图里连一个节点都不多 —— 与水印的"没有就不加"同一条。
    stickers: tuple[StickerPlan, ...] = ()
    bgm: Path | None = None
    subtitle: Path | None = None
    subtitle_font_dir: Path | None = None
    mix: MixSettings = field(default_factory=MixSettings)
    #: ``loudnorm`` 第一遍的读数。``None`` ⇒ 这次渲染只跑一遍（动态模式），
    #: 并会把这件事记进 manifest 的 ``warnings``。
    loudness: LoudnessMeasurement | None = None
    threads: int | None = None
    graph_path: Path | None = None


@dataclass(frozen=True, slots=True)
class CompositeResult:
    """一次合成的结论（进 manifest.json 与面板）。"""

    output: Path
    duration_ms: int
    size_bytes: int
    watermark_applied: bool
    bgm_applied: bool
    subtitle_applied: bool
    #: ``broll`` / ``black`` —— 这次画面是真底片还是纯黑底（降级留痕）
    bg_fill: str
    loudness: LoudnessMeasurement | None
    warnings: tuple[str, ...]
    argv: tuple[str, ...]
    #: 这次真的贴上去的贴图**槽位名**（按叠放顺序）。空元组 ⇒ 一层都没贴。
    #:
    #: 带默认值、且排在最后：这个字段是**后加的**（T4.14），而 ``CompositeResult`` 被
    #: 测试与降级链按关键字构造了很多次 —— 不给默认值等于让"加一层贴图"变成一次
    #: 全仓库的构造点改写。生产路径只有 ``run_composite`` 一个构造点，它显式传值。
    stickers_applied: tuple[str, ...] = ()
    #: 这次**真的在换图**的贴图槽位名（讲话图可用 + 有讲话区间）。它是
    #: ``stickers_applied`` 的子集 —— 一层贴上了但没换图，两处报的必须不一样，
    #: 否则"开了换图、成片里嘴一直不动"这种事没人能查。
    stickers_speaking: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output.as_posix(),
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "watermark_applied": self.watermark_applied,
            "stickers_applied": list(self.stickers_applied),
            "stickers_speaking": list(self.stickers_speaking),
            "bgm_applied": self.bgm_applied,
            "subtitle_applied": self.subtitle_applied,
            "bg_fill": self.bg_fill,
            "loudness": self.loudness.to_dict() if self.loudness else None,
            "warnings": list(self.warnings),
            "argv": list(self.argv),
        }


def duration_ms_for(voice_ms: int, *, tail_ms: int) -> int:
    """成片时长 = 人声时长 + 尾巴（§04.2.8 的 audio.tail_ms）。

    ``voice_ms <= 0`` ⇒ 报错而不是"给个默认值"：一个 0 毫秒的人声说明上游配音
    根本没产出音频，此时编出一支 0.6 秒的片子只会把问题藏起来。
    """
    if voice_ms <= 0:
        raise RenderError(
            f"人声时长为 {voice_ms}ms，无法确定成片长度",
            code=ErrorCode.RENDER_FAILED,
            context={"voice_ms": voice_ms},
            remediation="确认配音这一步真的产出了音频（ffprobe voice_master.wav）",
        )
    return voice_ms + max(0, tail_ms)


def filter_path_arg(path: Path) -> str:
    """把路径写成 ``-filter_complex`` 里能用的字面量（**引号 + 转义盘符冒号**）。

    滤镜图先由 ffmpeg 的滤镜参数解析器拆一层，再由滤镜自己解析一层，于是
    ``D:/x.ass`` 里的冒号会被第一层当成"选项分隔符"，报出来的错是
    ``Option not found`` —— 一个完全指不到"路径写错了"的错。

    所以：整体用单引号包住（挡掉滤镜层的选项拆分），冒号再转义一次（挡掉参数层）。
    这套写法是**实测**出来的（`data/tmp` 里的探针），不是照抄文档。
    """
    return "'" + path.as_posix().replace(":", r"\:") + "'"


#: 画面来源的两种取值（进 manifest 与 ``quality_json``）
BG_FILL_BROLL: Final[str] = "broll"
BG_FILL_BLACK: Final[str] = "black"


def bg_fill(req: CompositeRequest) -> str:
    """这次合成用的是真底片还是纯黑底（黑屏降级的留痕）。"""
    return BG_FILL_BROLL if req.clip is not None else BG_FILL_BLACK


def _inputs(req: CompositeRequest) -> tuple[list[str], dict[str, int]]:
    """输入清单 —— **已翻成 argv 片段**，外加每一路的 ``-i`` 序号。

    0 号永远是画面源，1 号永远是配音，之后按需追加 BGM / 水印。

    片段与序号由**同一个函数**产出，是为了避免"两边各排一遍"然后悄悄错位：错位时
    ffmpeg 报的是 ``Stream specifier ... matches no streams``，从那条报错里读不出
    "是输入顺序排错了"。
    """
    argv: list[str] = []
    indices: dict[str, int] = {}
    counter = 0

    def take(fragment: list[str]) -> int:
        nonlocal counter
        index = counter
        counter += 1
        argv.extend(fragment)
        return index

    if req.clip is not None:
        # 底片循环：跑酷素材通常比口播短，循环到人声结束为止（-t 负责收尾）。
        indices["clip"] = take(["-stream_loop", "-1", "-i", str(req.clip)])
    else:
        # 黑屏降级（§04.2.8.6）：没有底片时现造一块纯黑当画面源。
        # 用 lavfi 的 ``color`` 而不是"找一张黑图再循环"：它是无限长的**合成源**，
        # 与 ``-stream_loop -1`` 的底片同构，于是后面 scale/crop/fps 那一段一个字
        # 都不用改 —— 降级路径与正常路径共用同一张滤镜图，只换了 0 号输入的来路。
        width, height = req.profile.canvas
        indices["clip"] = take(["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={req.profile.fps}"])
    indices["voice"] = take(["-i", str(req.voice)])
    if req.bgm is not None:
        # BGM 也循环：放在**输入侧**由 demuxer 做，零内存开销。
        # 滤镜侧的 aloop 要按样本数开一块缓冲（规格里写的 size=20 亿 = 几 GB 内存），
        # 短 BGM 配长口播时那就是一次 OOM。
        indices["bgm"] = take(["-stream_loop", "-1", "-i", str(req.bgm)])
    watermark = _watermark_of(req)
    if watermark is not None:
        indices["watermark"] = take(["-i", str(watermark)])
    for slot, plan in enumerate(_stickers_of(req)):
        # 每层贴图各占一路输入。键用**序号**而不是槽位名：序号与
        # :func:`build_filter_graph` 里的 ``enumerate`` 是同一个枚举，
        # 槽位名进不了 argv，进了反而要多维护一次"名字到序号"的映射。
        indices[f"sticker_{slot}"] = take(["-i", str(plan.spec.image_path)])
        speaking_image = _speaking_image_of(plan)
        if speaking_image is not None:
            # 讲话图**另占一路输入**（同一个键规则，只是多一个后缀）。不共用普通图那
            # 一路：两路各是各的静帧，ffmpeg 的单帧输入靠 ``eof_action=repeat``
            # 一直续着，合成一路会让两张图在时间轴上互相覆盖。
            indices[f"sticker_speaking_{slot}"] = take(["-i", str(speaking_image)])
    return argv, indices


def _stickers_of(req: CompositeRequest) -> tuple[StickerPlan, ...]:
    """这次合成**真的贴上去**的那几层（``placement`` 有值才算），顺序 = 声明顺序。

    跳过的层在这里就被滤掉了 —— 滤镜图与输入清单都只看这一份，于是"某一层因为缺图
    被跳过"不会在滤镜图里留下一个指向不存在的输入的空节点（那种错 ffmpeg 报的是
    ``Stream specifier ... matches no streams``，指不到真正的原因）。
    """
    return tuple(plan for plan in req.stickers if plan.applied)


def _speaking_image_of(plan: StickerPlan) -> Path | None:
    """这一层这次要换图吗；要 ⇒ 讲话图那份路径。

    判据只有 :attr:`StickerPlan.swaps` 一处（图可用 + 有讲话区间），与
    :func:`studio.render.hashing.input_digests` 用的是同一个属性 —— 两处各写一遍
    "能不能换"，就会出现"哈希认为换了、滤镜图没换"这种对不上的账。
    """
    if not plan.swaps:
        return None
    return plan.spec.speaking_path


def enable_expr(intervals: Sequence[Interval]) -> str:
    """讲话区间（毫秒）⇒ ffmpeg ``enable=`` 的表达式。

    ``enable`` 是**逐帧求值**的时间轴选项，所以这里给的就是"这一帧该不该画"：
    ``between(t,a,b)+between(t,c,d)`` 里任何一个非零 ⇒ 画。加法当"或"用是 ffmpeg
    表达式语言自己的规矩（它没有逻辑或运算符）。

    两个细节：

    - 毫秒转秒**保留三位小数**（1ms 分辨率），与 ``-t`` 的写法一致；
    - 表达式外面由调用方套**单引号**（``enable='...'``）。不套的话，``between`` 里
      那两个逗号会被滤镜图解析器当成"下一个滤镜"的分隔符 —— 报出来的是
      ``No such filter: '5.000'`` 这种完全指不到原因的错误。表达式只由数字、括号
      与运算符拼出来，所以里面**不可能**出现单引号，套一层就够。
    """
    return "+".join(f"between(t,{start / 1000:.3f},{end / 1000:.3f})" for start, end in intervals)


def _watermark_of(req: CompositeRequest) -> Path | None:
    """这次合成到底贴不贴水印（``placement`` 有值才算贴）。"""
    plan = req.watermark
    if plan is None or plan.placement is None:
        return None
    return plan.spec.image_path


def _subtitle_of(req: CompositeRequest) -> Path | None:
    """这次合成到底烧不烧字幕（``subtitle`` 有值就算烧）。"""
    return req.subtitle


def video_label(req: CompositeRequest) -> str:
    """视频那一路最终的标签（与 :func:`build_filter_graph` 的收尾标签必须一致）。

    单独抽出来是因为 ``-map`` 要按标签取：图的收尾标签与 ``-map`` 的名字对不上时，
    ffmpeg 报的是 ``Stream specifier ... matches no streams`` —— 一条读不出
    "是字幕那层加错了"的错。
    """
    if _watermark_of(req) is not None:
        return "vout"
    if _subtitle_of(req) is not None:
        return "vsub"
    if _stickers_of(req):
        return "vstk"
    return "bg"


def build_filter_graph(req: CompositeRequest, *, warn: list[str] | None = None) -> str:
    """拼 ``-filter_complex`` 的图（纯函数）。

    每一路都以**显式标签**收尾（``[bg]`` ``[aout]`` …），因为 ``-map`` 要按标签取；
    让 ffmpeg 自动选流会在"底片自带音轨"时把画面选成音频，那种错很难从报错里读出来。

    视频那一段是**串起来**的，顺序从下到上：**贴图 → 字幕 → 水印**。

    这个顺序不是随手排的，三层的"谁该压住谁"各有理由：

    - **字幕压在贴图之上**：字幕是内容，贴图是装饰。人物恰好站在画面下方时（那是最
      常见的位置），字幕被人物盖住会直接影响"看不看得懂这条片子"，反过来只是人脸上
      多了一行字。
    - **水印压在最上面**：水印要始终可辨（它是"这条片子是谁的"那个标识），被别的层
      盖住就失去意义了。
    - **多层贴图之间**按**声明顺序**叠：YAML 里先写的在下面。这条规则一个人能看懂、
      也能自己调整，比任何"自动分层"都好解释。

    某一层配了**讲话图**时，那一层在图上变成两个节点（普通图 + 讲话图，各自带
    ``enable=`` 窗口）。窗口来自 :mod:`studio.render.speech` 算出来的讲话区间 ——
    谁在讲话是稿子的事，这里只负责把那几段毫秒翻成 ffmpeg 表达式。
    """
    profile = req.profile
    width, height = profile.canvas
    _input_argv, indices = _inputs(req)

    parts: list[str] = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={profile.fps},setsar=1[bg]"
    ]
    current = "bg"

    stickers = _stickers_of(req)
    for slot, sticker_plan in enumerate(stickers):
        index = indices[f"sticker_{slot}"]
        placement = sticker_plan.placement
        assert placement is not None  # _stickers_of 只挑 placement 有值的
        # 中间标签带序号（``vstk0`` / ``vstk1`` …），**最后一个**才叫 ``vstk`` ——
        # ``video_label()`` 要按固定名字取那一路，中间那几层叫什么不影响它。
        label = "vstk" if slot == len(stickers) - 1 else f"vstk{slot}"
        box = (
            f"scale={placement.width_px}:{placement.height_px},"
            f"format=rgba,colorchannelmixer=aa={sticker_plan.spec.opacity:g}"
        )
        parts.append(f"[{index}:v]{box}[stk{slot}]")

        speaking_image = _speaking_image_of(sticker_plan)
        if speaking_image is None:
            parts.append(f"[{current}][stk{slot}]overlay={placement.x}:{placement.y}:format=auto[{label}]")
            current = label
            continue

        # 讲话 ⇒ 换图。两张图**互斥**地画（而不是把讲话图叠在普通图上面）：
        # 它们是两次导出的两张画，轮廓未必逐像素重合，叠着画会在边缘露出下面那一张
        # 的半个身子。互斥还正好是用户说的那件事 ——"讲话时换成讲话图，讲完换回来"。
        #
        # 两张图共用**同一个** ``box`` 与同一个 ``x/y``：换图那一瞬间不能挪位置，
        # 否则人物会跳一下（宽高比不一致时会被压扁，那条在 ``plan_stickers`` 里报）。
        window = enable_expr(sticker_plan.speaking_intervals)
        parts.append(f"[{indices[f'sticker_speaking_{slot}']}:v]{box}[stk{slot}s]")
        parts.append(
            f"[{current}][stk{slot}]overlay={placement.x}:{placement.y}:format=auto:"
            f"enable='not({window})'[stk{slot}a]"
        )
        parts.append(
            f"[stk{slot}a][stk{slot}s]overlay={placement.x}:{placement.y}:format=auto:"
            f"enable='{window}'[{label}]"
        )
        current = label

    subtitle = _subtitle_of(req)
    if subtitle is not None:
        options = f"ass={filter_path_arg(subtitle)}"
        if req.subtitle_font_dir is not None:
            options += f":fontsdir={filter_path_arg(req.subtitle_font_dir)}"
        parts.append(f"[{current}]{options}[vsub]")
        current = "vsub"

    plan = req.watermark
    if "watermark" in indices and plan is not None and plan.placement is not None:
        index = indices["watermark"]
        wm_placement = plan.placement
        parts.append(
            f"[{index}:v]scale={wm_placement.width_px}:{wm_placement.height_px},"
            f"format=rgba,colorchannelmixer=aa={plan.spec.opacity:g}[wm]"
        )
        parts.append(f"[{current}][wm]overlay={wm_placement.x}:{wm_placement.y}:format=auto[vout]")
        current = "vout"

    parts += build_audio_chain(
        voice_index=indices["voice"],
        bgm_index=indices.get("bgm"),
        settings=req.mix,
        measured=req.loudness,
        warn=warn,
    )

    return ";".join(parts)


def build_composite_argv(req: CompositeRequest) -> list[str]:
    """把请求翻成 ffmpeg 的 argv（纯函数；测试直接断言这一份）。"""
    input_argv, _indices = _inputs(req)
    # ``-nostats`` 保留：给人看的那行统计仍然关掉（stderr 只留真正的报错）。机器可读的进度
    # 另开一路走 **stdout**（``-progress pipe:1``），节拍 2Hz —— 见 :class:`ProgressThrottle`。
    argv: list[str] = [
        ffmpeg_binary(),
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
        "-stats_period",
        f"{PROGRESS_STATS_PERIOD_SEC:g}",
        "-y",
    ]
    argv += input_argv
    argv += ["-filter_complex", build_filter_graph(req)]

    argv += ["-map", f"[{video_label(req)}]", "-map", "[aout]"]
    argv += ["-t", f"{req.duration_ms / 1000:.3f}"]
    if req.threads:
        argv += ["-threads", str(req.threads)]
    argv += req.profile.output_args()
    argv.append(str(req.output))
    return argv


def parse_out_time_us(line: str) -> int | None:
    """``out_time_us=123456`` ⇒ ``123456``；其余行（含 ``N/A``）⇒ ``None``。

    ``-progress pipe:1`` 每拍吐一小块 ``key=value``，其中 ``out_time_us`` 是"已经编到第几
    微秒"。只认这一行：``progress=continue/end`` 那行不带时间，而 ``out_time_ms`` 在不少
    版本里其实是**微秒**（名字骗人，两个字段值一模一样），按毫秒读会得到一个 1000 倍的
    进度。实测第一拍就可能是 ``out_time_us=N/A``（ffmpeg 还没算出第一帧的时间戳）——
    那不是错误，跳过就是。
    """
    key, separator, value = line.strip().partition("=")
    if not separator or key != "out_time_us":
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def progress_percent(out_time_us: int, duration_ms: int) -> int:
    """已编时长 ⇒ 百分比（**封顶 99**，见 :data:`_ENCODE_CEILING`）。

    ``duration_ms <= 0`` ⇒ ``0``：没有分母就不编一个出来（与面板 ``percent`` 同一条纪律）。
    """
    if duration_ms <= 0:
        return 0
    percent = out_time_us // (duration_ms * 10)
    return max(0, min(_ENCODE_CEILING, percent))


class ProgressThrottle:
    """把 ffmpeg 的进度流折成"百分比"，并按 **2Hz** 限流后推给上游（T3.4）。

    为什么 ffmpeg 那边已经有节拍了还要再限一道
    -----------------------------------------
    ``-stats_period 0.5`` 是 **ffmpeg 的选项**。每收一拍，上游就要写一次
    ``jobs.result_json``（一条 SQLite UPDATE）并在作业日志里追加一行 —— "写库的频率"该由
    **我们**决定，而不是由一个第三方命令行选项决定：换个 ffmpeg 版本、或者哪天有人把那
    个参数删了，节拍就没了，而症状是"渲染时数据库突然很忙"这种查不出源头的事。

    两条判据一起用：**百分比真的变了**，且**距上一拍 ≥ 0.5s**。

    - 只按时间限流：一个 20 分钟的片子会每半秒推一次一模一样的 37%；
    - 只按变化限流：``out_time_us`` 抖一下就是一拍，频率完全不可控。

    上限因此是"一次渲染最多 100 拍"，而面板本来也显示不出更细的粒度。
    """

    __slots__ = ("_clock", "_duration_ms", "_last_at", "_last_percent", "_min_interval", "_report")

    def __init__(
        self,
        report: Callable[[int, int, str], None],
        *,
        duration_ms: int,
        min_interval_sec: float = PROGRESS_MIN_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._report = report
        self._duration_ms = duration_ms
        self._min_interval = min_interval_sec
        self._clock = clock
        # ``-inf`` ⇒ **第一拍一定过**：否则"开始了"这件事要等半秒才看得见。
        self._last_at = float("-inf")
        self._last_percent = -1

    def feed(self, line: str) -> None:
        """喂一行 ffmpeg 的进度输出（不是 ``out_time_us`` 的行直接忽略）。"""
        out_time_us = parse_out_time_us(line)
        if out_time_us is None:
            return
        percent = progress_percent(out_time_us, self._duration_ms)
        if percent == self._last_percent:
            return
        now = self._clock()
        if now - self._last_at < self._min_interval:
            return
        self._last_percent = percent
        self._last_at = now
        self._report(percent, PROGRESS_TOTAL, f"合成中 {percent}%")


def _partial_of(output: Path) -> Path:
    """临时产物路径（同目录、同扩展名 —— ffmpeg 靠扩展名推封装格式）。"""
    return output.with_name(f"{output.stem}.partial{output.suffix}")


def run_composite(
    req: CompositeRequest,
    *,
    timeout: int = RENDER_TIMEOUT_SEC,
    measure: bool = True,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> CompositeResult:
    """真跑一次 ffmpeg（外加一次纯音频的响度测量）。

    **先写 ``.partial`` 再原子改名**（陷阱 #9）：直接写目标文件时，进程中途被杀会留下
    一个"文件名正确、内容残缺"的 mp4，下游只看"文件在不在"就会当成成品发出去。

    :param measure: 要不要先跑 loudnorm 第一遍。``req.loudness`` 已经有值 ⇒ 不重复量
        （测试与"同一条片子重渲"都走这条路）。
    :param on_progress: 编码进度（``(done, total, note)``，``total`` 恒为
        :data:`PROGRESS_TOTAL`）。**只在真正调 ffmpeg 这一趟里有**，而且最多 2Hz
        （见 :class:`ProgressThrottle`）；响度测量那趟不发进度 —— 它是"开跑前的准备"，
        把它算进百分比只会让进度条先跳到一半再退回来。
    """
    warnings: list[str] = []
    if req.loudness is None and measure:
        req = replace(
            req,
            loudness=measure_mix(
                voice=req.voice,
                bgm=req.bgm,
                settings=req.mix,
                duration_ms=req.duration_ms,
            ),
        )
        if req.loudness is None:
            warnings.append("响度测量没跑通，这次 loudnorm 走一遍的动态模式（响度可能不够准）")

    graph_warnings: list[str] = []
    argv = build_composite_argv(req)
    _record_graph(req, warn=graph_warnings)
    warnings += graph_warnings

    partial = _partial_of(req.output)
    partial.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists():
        partial.unlink()

    executed = list(argv)
    executed[-1] = str(partial)
    # 有进度回调 ⇒ 边跑边读（`-progress pipe:1` 吐在 stdout 上）；没人看进度就走老路径，
    # 不必为它付两条读线程的钱。
    throttle = None if on_progress is None else ProgressThrottle(on_progress, duration_ms=req.duration_ms)
    result = run_command(
        executed,
        timeout=timeout,
        on_stdout_line=None if throttle is None else throttle.feed,
    )

    if not result.ok or not partial.is_file():
        if partial.exists():
            partial.unlink()
        raise RenderError(
            f"合成失败（rc={result.returncode}）：{result.tail()}",
            code=ErrorCode.RENDER_TIMEOUT if result.returncode == -2 else ErrorCode.RENDER_FAILED,
            context={
                "clip": req.clip.as_posix() if req.clip else BG_FILL_BLACK,
                "voice": req.voice.as_posix(),
                "output": req.output.as_posix(),
                "returncode": result.returncode,
                "argv": executed,
            },
            remediation="看上面的 ffmpeg 报错；滤镜图已留在 graphs/ 目录里，可直接手工重跑那条命令",
        )

    req.output.parent.mkdir(parents=True, exist_ok=True)
    partial.replace(req.output)

    if on_progress is not None:
        # 100 只在这里报：**文件已经躺在目标路径上了**才配叫"合成完成"（见 `_ENCODE_CEILING`）。
        on_progress(PROGRESS_TOTAL, PROGRESS_TOTAL, "合成完成")

    return CompositeResult(
        output=req.output,
        duration_ms=req.duration_ms,
        size_bytes=req.output.stat().st_size,
        watermark_applied=_watermark_of(req) is not None,
        stickers_applied=tuple(plan.spec.name for plan in _stickers_of(req)),
        stickers_speaking=tuple(
            plan.spec.name for plan in _stickers_of(req) if _speaking_image_of(plan) is not None
        ),
        bgm_applied=req.bgm is not None,
        subtitle_applied=_subtitle_of(req) is not None,
        bg_fill=bg_fill(req),
        loudness=req.loudness,
        warnings=tuple(warnings),
        argv=tuple(executed),
    )


def _record_graph(req: CompositeRequest, *, warn: list[str] | None = None) -> None:
    """把滤镜图落盘（graphs/ 永久保留 —— "这条片子当时是怎么渲的"要能查）。"""
    if req.graph_path is None:
        return
    argv = build_composite_argv(req)
    body = "\n".join(
        [
            "# 自动生成：单遍合成（T3.3 · 字幕 T3.5 · 混音 T3.6）",
            f"# profile:  {req.profile.name}",
            f"# clip:     {req.clip.as_posix() if req.clip else '（无底片 ⇒ 纯黑底降级）'}",
            f"# voice:    {req.voice.as_posix()}",
            f"# bgm:      {req.bgm.as_posix() if req.bgm else '（无）'}",
            f"# subtitle: {req.subtitle.as_posix() if req.subtitle else '（无）'}",
            f"# 响度测量: {req.loudness.to_dict() if req.loudness else '（没量，走一遍法）'}",
            "",
            "filter_complex:",
            build_filter_graph(req, warn=warn),
            "",
            "argv:",
            " ".join(argv),
            "",
        ]
    )
    req.graph_path.parent.mkdir(parents=True, exist_ok=True)
    req.graph_path.write_text(body, encoding="utf-8")
