"""单遍合成的 argv 与滤镜图（T3.3/T3.4 · §04.2.8）。

这一层的用例**不跑 ffmpeg**：:func:`build_composite_argv` / :func:`build_filter_graph`
是纯函数，直接断言它们产出的字符串，比"跑一遍再猜哪里不对"快几个数量级。
真正跑一遍的验证在 `studio render make` 的端到端用例里。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.core.config import OutputsConfig
from studio.core.errors import ErrorCode, RenderError
from studio.core.paths import StudioPaths
from studio.render.composite import (
    BG_FILL_BLACK,
    BG_FILL_BROLL,
    CompositeRequest,
    bg_fill,
    build_composite_argv,
    build_filter_graph,
    duration_ms_for,
)
from studio.render.mixdown import MixSettings
from studio.render.profiles import resolve_profile
from studio.render.watermark import plan_watermark

from .conftest import write_png


def _request(outputs: OutputsConfig, tmp_path: Path, **overrides: object) -> CompositeRequest:
    """一份最小可用的合成请求（默认**不带**水印与 BGM）。"""
    base: dict[str, object] = {
        "profile": resolve_profile(outputs),
        "clip": tmp_path / "clip.mp4",
        "voice": tmp_path / "voice.wav",
        "output": tmp_path / "out.mp4",
        "duration_ms": 12_345,
    }
    base.update(overrides)
    return CompositeRequest(**base)  # type: ignore[arg-type]


def _with_watermark(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path, **overrides: object
) -> object:
    """那份"贴了水印"的计划（报告与滤镜图都要用它）。"""
    write_png(watermark_path, width=400, height=200)
    plan = plan_watermark(outputs.watermark, canvas_width=1080, canvas_height=1920, home=outputs_home.home)
    assert plan.placement is not None
    return plan


# ══════════════════════════════════════════════════════════════════════
# 时长
# ══════════════════════════════════════════════════════════════════════


def test_duration_is_voice_plus_tail() -> None:
    """成片时长 = 人声 + 尾巴（ADR-005：音频是时长真相）。"""
    assert duration_ms_for(10_000, tail_ms=600) == 10_600
    assert duration_ms_for(10_000, tail_ms=0) == 10_000


def test_zero_length_voice_is_an_error() -> None:
    """0 毫秒人声说明上游没产出音频 —— 报错，不编一支 0.6 秒的片子把问题藏起来。"""
    with pytest.raises(RenderError) as excinfo:
        duration_ms_for(0, tail_ms=600)
    assert excinfo.value.code is ErrorCode.RENDER_FAILED


# ══════════════════════════════════════════════════════════════════════
# 滤镜图
# ══════════════════════════════════════════════════════════════════════


def test_base_graph_scales_fills_and_sets_fps(outputs: OutputsConfig, tmp_path: Path) -> None:
    graph = build_filter_graph(_request(outputs, tmp_path))
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in graph
    assert "crop=1080:1920" in graph
    assert "fps=30" in graph
    assert "setsar=1" in graph


def test_graph_without_watermark_has_no_overlay(outputs: OutputsConfig, tmp_path: Path) -> None:
    """没有水印 ⇒ 图里不该出现 overlay（不是"贴一张透明的"）。"""
    graph = build_filter_graph(_request(outputs, tmp_path))
    assert "overlay" not in graph
    assert "[bg]" in graph


def test_graph_with_watermark_adds_an_overlay_layer(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path, tmp_path: Path
) -> None:
    plan = _with_watermark(outputs, outputs_home, watermark_path)
    placement = plan.placement  # type: ignore[attr-defined]
    graph = build_filter_graph(_request(outputs, tmp_path, watermark=plan))
    assert f"overlay={placement.x}:{placement.y}:format=auto" in graph
    assert f"scale={placement.width_px}:{placement.height_px}" in graph
    assert "[vout]" in graph


def test_graph_without_bgm_does_not_mix(outputs: OutputsConfig, tmp_path: Path) -> None:
    """没有 BGM ⇒ 单轨人声（anull 占位），不做 amix / 不做侧链。"""
    graph = build_filter_graph(_request(outputs, tmp_path))
    assert "amix" not in graph
    assert "sidechaincompress" not in graph
    assert "anull" in graph


def test_graph_without_subtitle_has_no_ass_layer(outputs: OutputsConfig, tmp_path: Path) -> None:
    """没字幕 ⇒ 图里不该出现 ass（不是"烧一份空的"）。"""
    graph = build_filter_graph(_request(outputs, tmp_path))
    assert "ass=" not in graph
    assert "[vsub]" not in graph


def test_subtitle_layer_sits_between_the_base_and_the_watermark(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path, tmp_path: Path
) -> None:
    """★ 字幕先烧、水印后贴：反过来水印会被字幕的描边盖住。"""
    plan = _with_watermark(outputs, outputs_home, watermark_path)
    request = _request(outputs, tmp_path, subtitle=tmp_path / "sub.ass", watermark=plan)
    graph = build_filter_graph(request)
    assert "[bg]ass=" in graph
    assert "[vsub][wm]overlay=" in graph
    assert graph.index("ass=") < graph.index("overlay=")
    assert "[vout]" in graph


def test_subtitle_path_is_escaped_for_the_filter_parser(outputs: OutputsConfig, tmp_path: Path) -> None:
    """★ 盘符冒号会被滤镜参数解析器当成选项分隔符（报错是 Option not found）。"""
    request = _request(outputs, tmp_path, subtitle=tmp_path / "sub.ass")
    graph = build_filter_graph(request)
    assert "ass='" in graph
    assert "\\:" in graph
    assert "fontsdir=" not in graph  # 没给字体目录就不写这个选项


def test_subtitle_fonts_dir_is_passed_when_known(outputs: OutputsConfig, tmp_path: Path) -> None:
    request = _request(outputs, tmp_path, subtitle=tmp_path / "sub.ass", subtitle_font_dir=tmp_path / "fonts")
    graph = build_filter_graph(request)
    assert ":fontsdir='" in graph


def test_subtitle_only_still_maps_the_right_label(outputs: OutputsConfig, tmp_path: Path) -> None:
    """只有字幕（没水印）时 ``-map`` 要取 ``[vsub]``，取错是"匹配不到流"。"""
    request = _request(outputs, tmp_path, subtitle=tmp_path / "sub.ass")
    argv = build_composite_argv(request)
    assert argv[argv.index("-map") + 1] == "[vsub]"


def test_graph_with_bgm_mixes_with_voice_as_the_master(outputs: OutputsConfig, tmp_path: Path) -> None:
    graph = build_filter_graph(_request(outputs, tmp_path, bgm=tmp_path / "bgm.mp3"))
    assert "amix=inputs=2:duration=first:normalize=0" in graph
    # 侧链的输入顺序是 [主路][侧链]：BGM 在前（被压的），人声在后（触发的）。
    # ffmpeg -h filter=sidechaincompress 写明 #0: main / #1: sidechain；接反了不报错，
    # 但 BGM 整条丢失、人声被叠一份（规格 §04.2.8.3 的模板就是接反的那一版）。
    assert "[a_bgm][a_voice_sc]sidechaincompress=" in graph
    assert "[a_voice_sc][a_bgm]sidechaincompress=" not in graph
    # 人声必须先 asplit 才能既当侧链又进混音（一个标签只能被消费一次）
    assert "[a_voice]asplit=2[a_voice_sc][a_voice_mix]" in graph
    assert "[a_voice_mix][a_bgm_duck]amix=" in graph


def test_voice_volume_uses_the_configured_gain(outputs: OutputsConfig, tmp_path: Path) -> None:
    graph = build_filter_graph(_request(outputs, tmp_path, mix=MixSettings(voice_gain_db=-3.0)))
    assert "volume=-3dB" in graph


# ══════════════════════════════════════════════════════════════════════
# argv
# ══════════════════════════════════════════════════════════════════════


def test_argv_loops_the_clip_and_sets_an_explicit_duration(outputs: OutputsConfig, tmp_path: Path) -> None:
    """底片用 ``-stream_loop -1`` 拉成无限长，收尾交给显式 ``-t``。"""
    argv = build_composite_argv(_request(outputs, tmp_path))
    first_input = argv.index("-i")
    # 循环必须紧贴在**第一个** -i 之前（即作用在底片上，不是别的输入）
    assert argv[first_input - 2 : first_input + 1] == ["-stream_loop", "-1", "-i"]
    assert argv[argv.index("-t") + 1] == "12.345"


def test_argv_never_uses_shortest(outputs: OutputsConfig, tmp_path: Path) -> None:
    """显式 ``-t`` 才可预测；``-shortest`` 的语义会随输入组合漂移。"""
    assert "-shortest" not in build_composite_argv(_request(outputs, tmp_path))


def test_argv_maps_the_labelled_streams(outputs: OutputsConfig, tmp_path: Path) -> None:
    argv = build_composite_argv(_request(outputs, tmp_path))
    assert argv[argv.index("-map") + 1] == "[bg]"
    assert "[aout]" in argv


def test_argv_loops_the_bgm_too(outputs: OutputsConfig, tmp_path: Path) -> None:
    """BGM 的循环在**输入侧**做（滤镜侧 aloop 要开几 GB 的缓冲）。"""
    argv = build_composite_argv(_request(outputs, tmp_path, bgm=tmp_path / "bgm.mp3"))
    # "-stream_loop -1" 必须紧贴在 BGM 那个 -i 之前，而不是在别的输入上
    at = argv.index(str(tmp_path / "bgm.mp3"))
    assert argv[at - 3 : at + 1] == ["-stream_loop", "-1", "-i", str(tmp_path / "bgm.mp3")]


def test_argv_carries_the_profile_encoding_args(outputs: OutputsConfig, tmp_path: Path) -> None:
    argv = build_composite_argv(_request(outputs, tmp_path))
    for flag in ("-c:v", "-crf", "-preset", "-profile:v", "-level", "-movflags", "-c:a"):
        assert flag in argv, flag
    assert "+faststart" in argv


def test_argv_orders_inputs_clip_voice_bgm_watermark(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path, tmp_path: Path
) -> None:
    """``-i`` 的**序号**就是滤镜图里的 ``[N:v]`` —— 顺序错了会贴错东西。"""
    plan = _with_watermark(outputs, outputs_home, watermark_path)
    request = _request(outputs, tmp_path, bgm=tmp_path / "bgm.mp3", watermark=plan)
    argv = build_composite_argv(request)
    inputs = [argv[index + 1] for index, token in enumerate(argv) if token == "-i"]
    assert inputs[0].endswith("clip.mp4")
    assert inputs[1].endswith("voice.wav")
    assert inputs[2].endswith("bgm.mp3")
    assert inputs[3].endswith("watermark.png")
    assert "[3:v]" in build_filter_graph(request)


# ══════════════════════════════════════════════════════════════════════
# 黑屏降级（T3.7 · §04.2.8.6）
# ══════════════════════════════════════════════════════════════════════


def test_no_clip_becomes_a_lavfi_black_source(outputs: OutputsConfig, tmp_path: Path) -> None:
    """★ 没有底片 ⇒ 0 号输入换成 ``lavfi`` 的纯黑源，**照常出片**（不是失败）。

    为什么用 ``color`` 而不是"找一张黑图再循环"：它是无限长的**合成源**，与
    ``-stream_loop -1`` 的底片同构，于是整张滤镜图一个字都不用改。
    """
    argv = build_composite_argv(_request(outputs, tmp_path, clip=None))
    inputs = [argv[index + 1] for index, token in enumerate(argv) if token == "-i"]
    assert inputs[0] == "color=c=black:s=1080x1920:r=30"
    assert argv[argv.index("-i") - 1] == "lavfi"
    # 配音仍然是 1 号 —— 滤镜图里的 [1:a] 靠这个序号
    assert inputs[1].endswith("voice.wav")


def test_black_fill_reuses_the_very_same_filter_graph(outputs: OutputsConfig, tmp_path: Path) -> None:
    """★ 降级路径与正常路径**共用同一张图**，只换了 0 号输入的来路。

    这条是这次改动的**主要不变量**：如果哪天有人给黑屏单开一张图，两份必然漂移，
    而漂移的症状是"有底片时字幕正常、没底片时字幕不见了"这种极难二分的问题。
    """
    with_clip = build_filter_graph(_request(outputs, tmp_path))
    without_clip = build_filter_graph(_request(outputs, tmp_path, clip=None))
    assert with_clip == without_clip


def test_black_fill_never_loops_a_file(outputs: OutputsConfig, tmp_path: Path) -> None:
    """纯黑源是无限的，不该再挂 ``-stream_loop``（挂了也不会错，但语义是假的）。"""
    argv = build_composite_argv(_request(outputs, tmp_path, clip=None))
    assert "-stream_loop" not in argv


def test_bg_fill_reports_which_source_was_used(outputs: OutputsConfig, tmp_path: Path) -> None:
    """留痕：``bg_fill`` 是 ``broll`` 还是 ``black``，下游靠它判 ``degraded``。"""
    assert bg_fill(_request(outputs, tmp_path)) == BG_FILL_BROLL
    assert bg_fill(_request(outputs, tmp_path, clip=None)) == BG_FILL_BLACK
