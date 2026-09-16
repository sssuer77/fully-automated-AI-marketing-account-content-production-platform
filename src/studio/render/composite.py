"""单遍合成与最终导出（T3.3/T3.4/T3.5/T3.6 · §04.2.8）—— 一条 ffmpeg 命令出 final.mp4。

流水线（**一遍编码，无中间产物**）
----------------------------------
```text
跑酷底片（-stream_loop -1 循环）
      │
      ├─ scale=W:H:force_original_aspect_ratio=increase → crop=W:H   缩放铺满
      ├─ fps=<profile.fps> → setsar=1                                恒定帧率 + 方形像素
      ├─ ass=<subtitle.ass>:fontsdir=…      （**有字幕才加这一层**）  烧字幕
      ├─ overlay=x:y                        （**水印存在才加这一层**）可选装饰
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

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from studio.core.errors import ErrorCode, RenderError
from studio.core.media import ffmpeg_binary, run_command
from studio.render.mixdown import LoudnessMeasurement, MixSettings, build_audio_chain, measure_mix
from studio.render.profiles import CompositeProfile
from studio.render.watermark import WatermarkPlan

__all__ = [
    "BG_FILL_BLACK",
    "BG_FILL_BROLL",
    "RENDER_TIMEOUT_SEC",
    "CompositeRequest",
    "CompositeResult",
    "bg_fill",
    "build_composite_argv",
    "build_filter_graph",
    "duration_ms_for",
    "filter_path_arg",
    "run_composite",
    "video_label",
]

#: 一次合成的超时。给 30 分钟：1080×1920 的单遍编码在 8 核机器上大约 1–3 倍实时，
#: 一条 2 分钟的片子几分钟内能出；超时是"卡死了"的信号，不是"有点慢"。
RENDER_TIMEOUT_SEC: Final[int] = 1800


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output.as_posix(),
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "watermark_applied": self.watermark_applied,
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
    return argv, indices


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
    return "bg"


def build_filter_graph(req: CompositeRequest, *, warn: list[str] | None = None) -> str:
    """拼 ``-filter_complex`` 的图（纯函数）。

    每一路都以**显式标签**收尾（``[bg]`` ``[aout]`` …），因为 ``-map`` 要按标签取；
    让 ffmpeg 自动选流会在"底片自带音轨"时把画面选成音频，那种错很难从报错里读出来。

    视频那一段是**串起来**的：字幕先烧、水印后贴。反过来会让水印被字幕的描边盖住
    （两者都在画面下方时最容易撞），而"水印压在字幕上"比"字幕压在水印上"更难看。
    """
    profile = req.profile
    width, height = profile.canvas
    _input_argv, indices = _inputs(req)

    parts: list[str] = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={profile.fps},setsar=1[bg]"
    ]
    current = "bg"

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
        placement = plan.placement
        parts.append(
            f"[{index}:v]scale={placement.width_px}:{placement.height_px},"
            f"format=rgba,colorchannelmixer=aa={plan.spec.opacity:g}[wm]"
        )
        parts.append(f"[{current}][wm]overlay={placement.x}:{placement.y}:format=auto[vout]")
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
    argv: list[str] = [ffmpeg_binary(), "-hide_banner", "-nostats", "-y"]
    argv += input_argv
    argv += ["-filter_complex", build_filter_graph(req)]

    argv += ["-map", f"[{video_label(req)}]", "-map", "[aout]"]
    argv += ["-t", f"{req.duration_ms / 1000:.3f}"]
    if req.threads:
        argv += ["-threads", str(req.threads)]
    argv += req.profile.output_args()
    argv.append(str(req.output))
    return argv


def _partial_of(output: Path) -> Path:
    """临时产物路径（同目录、同扩展名 —— ffmpeg 靠扩展名推封装格式）。"""
    return output.with_name(f"{output.stem}.partial{output.suffix}")


def run_composite(
    req: CompositeRequest,
    *,
    timeout: int = RENDER_TIMEOUT_SEC,
    measure: bool = True,
) -> CompositeResult:
    """真跑一次 ffmpeg（外加一次纯音频的响度测量）。

    **先写 ``.partial`` 再原子改名**（陷阱 #9）：直接写目标文件时，进程中途被杀会留下
    一个"文件名正确、内容残缺"的 mp4，下游只看"文件在不在"就会当成成品发出去。

    :param measure: 要不要先跑 loudnorm 第一遍。``req.loudness`` 已经有值 ⇒ 不重复量
        （测试与"同一条片子重渲"都走这条路）。
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
    result = run_command(executed, timeout=timeout)

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

    return CompositeResult(
        output=req.output,
        duration_ms=req.duration_ms,
        size_bytes=req.output.stat().st_size,
        watermark_applied=_watermark_of(req) is not None,
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
