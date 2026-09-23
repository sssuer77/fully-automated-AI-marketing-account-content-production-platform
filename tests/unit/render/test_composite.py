"""单遍合成的 argv 与滤镜图（T3.3/T3.4 · §04.2.8）。

这一层的用例**不跑 ffmpeg**：:func:`build_composite_argv` / :func:`build_filter_graph`
是纯函数，直接断言它们产出的字符串，比"跑一遍再猜哪里不对"快几个数量级。
真正跑一遍的验证在 `studio render make` 的端到端用例里。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.core.config import OutputsConfig, StickerConfig
from studio.core.errors import ErrorCode, RenderError
from studio.core.paths import StudioPaths
from studio.render.composite import (
    BG_FILL_BLACK,
    BG_FILL_BROLL,
    PROGRESS_STATS_PERIOD_SEC,
    PROGRESS_TOTAL,
    CompositeRequest,
    ProgressThrottle,
    bg_fill,
    build_composite_argv,
    build_filter_graph,
    duration_ms_for,
    enable_expr,
    parse_out_time_us,
    progress_percent,
    video_label,
)
from studio.render.mixdown import MixSettings
from studio.render.profiles import resolve_profile
from studio.render.speech import SpeakingPlan
from studio.render.sticker import StickerPlan, plan_stickers
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


# ══════════════════════════════════════════════════════════════════════
# 进度流（T3.4）
# ══════════════════════════════════════════════════════════════════════


def test_argv_asks_for_a_machine_readable_progress_stream(outputs: OutputsConfig, tmp_path: Path) -> None:
    """★ 进度走 **stdout**（``-progress pipe:1``），不是让人去正则匹配 stderr 那行统计。

    ``-nostats`` 仍然留着：给人看的那行统计一关，stderr 里就只剩真正的报错 ——
    报错信息里混着几十行 ``frame=…`` 是最难读的一种现场。
    """
    argv = build_composite_argv(_request(outputs, tmp_path))
    assert argv[argv.index("-progress") + 1] == "pipe:1"
    assert argv[argv.index("-stats_period") + 1] == f"{PROGRESS_STATS_PERIOD_SEC:g}" == "0.5"
    assert "-nostats" in argv


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("out_time_us=123456", 123_456),
        ("out_time_us=0", 0),
        ("  out_time_us=42  ", 42),
        ("out_time_us=N/A", None),  # 第一拍就是它（ffmpeg 还没算出时间戳）
        ("out_time_ms=123456", None),  # 名字骗人：按毫秒读会得到一个 1000 倍的进度
        ("progress=continue", None),
        ("frame=180", None),
        ("out_time_us=", None),
        ("out_time_us=abc", None),
        ("", None),
        ("out_time_us=-5", 0),  # 负数当 0：进度不该往回走
    ],
)
def test_parse_out_time_us(line: str, expected: int | None) -> None:
    assert parse_out_time_us(line) == expected


@pytest.mark.parametrize(
    ("out_time_us", "duration_ms", "expected"),
    [
        (0, 10_000, 0),
        (5_000_000, 10_000, 50),
        (9_999_999, 10_000, 99),  # 还差一点点 ⇒ 不给 100
        (10_000_000, 10_000, 99),  # 编完了也还是 99：100 归"改名成功"那一刻
        (20_000_000, 10_000, 99),  # 超出分母（`-t` 与流时长对不齐）⇒ 仍然封顶
        (1_000, 0, 0),  # 没有分母 ⇒ 0，不编一个出来
        (1_000, -5, 0),
    ],
)
def test_progress_percent(out_time_us: int, duration_ms: int, expected: int) -> None:
    assert progress_percent(out_time_us, duration_ms) == expected


class _FakeClock:
    """手摇的时钟：限流用例不该靠 ``time.sleep`` 去等节拍（那既慢又看机器脸色）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttle(
    *, duration_ms: int = 10_000
) -> tuple[ProgressThrottle, list[tuple[int, int, str]], _FakeClock]:
    """一个接了假时钟的限流器 + 它推出去的那些拍。"""
    ticks: list[tuple[int, int, str]] = []
    clock = _FakeClock()
    throttle = ProgressThrottle(
        lambda done, total, note: ticks.append((done, total, note)),
        duration_ms=duration_ms,
        clock=clock,
    )
    return throttle, ticks, clock


def test_the_first_tick_always_goes_through() -> None:
    """★ 第一拍不设门槛：否则"开始了"这件事要等半秒才看得见。"""
    throttle, ticks, _clock = _throttle()
    throttle.feed("out_time_us=0")
    assert ticks == [(0, PROGRESS_TOTAL, "合成中 0%")]


def test_two_ticks_closer_than_the_interval_collapse() -> None:
    """2Hz：半秒内的第二拍直接丢掉（它只是"同一句话又说了一遍"）。"""
    throttle, ticks, clock = _throttle()
    throttle.feed("out_time_us=3_000_000")  # 30% —— 第一拍，过
    clock.advance(0.2)
    throttle.feed("out_time_us=4_000_000")  # 40% —— 才过 0.2s，丢
    assert [done for done, _t, _n in ticks] == [30]
    clock.advance(0.3)  # 距上一拍刚好 0.5s
    throttle.feed("out_time_us=4_000_000")
    assert [done for done, _t, _n in ticks] == [30, 40]


def test_the_same_percentage_is_never_pushed_twice() -> None:
    """百分比没变就不推 —— 一个 20 分钟的片子不该每半秒报一次一模一样的 37%。"""
    throttle, ticks, clock = _throttle()
    throttle.feed("out_time_us=3_700_000")
    for _ in range(20):
        clock.advance(1.0)
        throttle.feed("out_time_us=3_700_000")
    assert len(ticks) == 1


def test_non_progress_lines_are_ignored() -> None:
    """ffmpeg 那一小块 kv 里只有 ``out_time_us`` 有用，别的行不该被当成进度。"""
    throttle, ticks, clock = _throttle()
    for line in ("frame=1", "fps=0.00", "progress=continue", "out_time_ms=999999", ""):
        throttle.feed(line)
    clock.advance(1.0)
    throttle.feed("progress=end")
    assert ticks == []


def test_a_render_never_pushes_more_than_a_hundred_ticks() -> None:
    """★ 上限就是"一次渲染最多 100 拍"：面板显示不出更细的粒度，而每一拍都要写一次库。"""
    throttle, ticks, clock = _throttle()
    for step in range(1000):
        clock.advance(1.0)
        throttle.feed(f"out_time_us={step * 10_000}")
    assert len(ticks) == 100
    assert ticks[-1] == (99, PROGRESS_TOTAL, "合成中 99%")
    assert PROGRESS_TOTAL not in [done for done, _t, _n in ticks], "编码期间不许报 100%"


# ══════════════════════════════════════════════════════════════════════
# 人物贴图：讲话时换图（T6.5 追加 · 裁定 391）
# ══════════════════════════════════════════════════════════════════════

STICKER_REL = "templates/t/assets/images/stickers/hero.png"
STICKER_SPEAKING_REL = "templates/t/assets/images/stickers/hero_speaking.png"


def _sticker_plans(
    outputs_home: StudioPaths,
    speaking: SpeakingPlan | None = None,
    *,
    speaker: str = "bigbear",
    speaking_size: tuple[int, int] = (600, 1200),
) -> tuple[StickerPlan, ...]:
    """造一层真的贴得上的贴图（``speaking`` 给了就再配一张讲话图）。

    走的是**真** :func:`plan_stickers`（而不是手搓一个 ``StickerPlan``）：换图那三个
    条件（贴上了 + 讲话图可用 + 有区间）就写在它里面，手搓一份等于把判据抄第二遍。
    """
    home = outputs_home.home
    write_png(home / STICKER_REL, width=600, height=1200)
    if speaking is not None:
        write_png(home / STICKER_SPEAKING_REL, width=speaking_size[0], height=speaking_size[1])
    config = StickerConfig(
        enabled=True,
        path=STICKER_REL,
        speaker=speaker if speaking is not None else "",
        speaking_path=STICKER_SPEAKING_REL if speaking is not None else None,
        position="bottom_right",
        margin_x=48,
        margin_y=420,
        height_ratio=0.45,
        opacity=1.0,
    )
    return plan_stickers(
        {"hero": config},
        canvas_width=1080,
        canvas_height=1920,
        home=home,
        speaking=speaking,
    )


def _talking(spans: tuple[tuple[int, int], ...] = ((0, 1000),)) -> SpeakingPlan:
    return SpeakingPlan(by_speaker={"bigbear": spans}, source="timeline")


def test_a_sticker_layer_adds_one_overlay(
    outputs: OutputsConfig, outputs_home: StudioPaths, tmp_path: Path
) -> None:
    """没配讲话图 ⇒ 还是**一层 overlay**，一个字都不多（与水印"没有就不加"同一条）。"""
    request = _request(outputs, tmp_path, stickers=_sticker_plans(outputs_home))
    graph = build_filter_graph(request)
    assert graph.count("overlay=") == 1
    assert "enable=" not in graph


def test_speaking_adds_a_second_mutually_exclusive_overlay(
    outputs: OutputsConfig, outputs_home: StudioPaths, tmp_path: Path
) -> None:
    """★ 讲话那一层变成**两个互斥**的 overlay（不是把讲话图叠在普通图上面）。

    叠着画会在边缘露出下面那张的半个身子 —— 两张图是两次导出的画，轮廓未必逐像素重合。
    """
    plans = _sticker_plans(outputs_home, _talking(((0, 1000), (2000, 3500))))
    assert plans[0].swaps is True
    graph = build_filter_graph(_request(outputs, tmp_path, stickers=plans))
    assert graph.count("overlay=") == 2
    assert "enable='not(between(t,0.000,1.000)+between(t,2.000,3.500))'" in graph
    assert "enable='between(t,0.000,1.000)+between(t,2.000,3.500)'" in graph


def test_the_two_overlays_share_one_position(
    outputs: OutputsConfig, outputs_home: StudioPaths, tmp_path: Path
) -> None:
    """换图**不挪位置**：两张图用同一个 ``x/y`` 与同一份缩放（否则人物会跳一下）。"""
    plans = _sticker_plans(outputs_home, _talking())
    placement = plans[0].placement
    assert placement is not None
    graph = build_filter_graph(_request(outputs, tmp_path, stickers=plans))
    at = f"overlay={placement.x}:{placement.y}:format=auto"
    assert graph.count(at) == 2
    box = f"scale={placement.width_px}:{placement.height_px}"
    assert graph.count(box) == 2, "两张图各自缩放，但参数必须一样"


def test_the_speaking_image_is_a_separate_input(
    outputs: OutputsConfig, outputs_home: StudioPaths, tmp_path: Path
) -> None:
    """讲话图**另占一路输入** —— 合成一路会让两张图在时间轴上互相覆盖。"""
    plans = _sticker_plans(outputs_home, _talking())
    argv = build_composite_argv(_request(outputs, tmp_path, stickers=plans))
    assert str(outputs_home.home / STICKER_REL) in argv
    assert str(outputs_home.home / STICKER_SPEAKING_REL) in argv
    assert argv.count("-i") == 4, "底片 + 配音 + 普通图 + 讲话图"


def test_a_layer_that_cannot_swap_stays_a_single_overlay(
    outputs: OutputsConfig, outputs_home: StudioPaths, tmp_path: Path
) -> None:
    """★ 配了讲话图、但这个人这条片子里没词 ⇒ 退回**一个** overlay，且那一路输入都不加。

    只判"配没配讲话图"是不够的：那样会白加一路输入，还在滤镜图里留下一个永远不显示的
    节点（"到底换没换"从此只能靠猜）。
    """
    plans = _sticker_plans(
        outputs_home, SpeakingPlan(by_speaker={"littlebear": ((0, 1000),)}, source="timeline")
    )
    assert plans[0].applied is True
    assert plans[0].swaps is False
    graph = build_filter_graph(_request(outputs, tmp_path, stickers=plans))
    assert graph.count("overlay=") == 1
    assert "enable=" not in graph
    argv = build_composite_argv(_request(outputs, tmp_path, stickers=plans))
    assert str(outputs_home.home / STICKER_SPEAKING_REL) not in argv


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        ((), ""),
        (((0, 1000),), "between(t,0.000,1.000)"),
        (((1234, 5678),), "between(t,1.234,5.678)"),
        (((0, 1000), (2000, 3500)), "between(t,0.000,1.000)+between(t,2.000,3.500)"),
    ],
)
def test_enable_expr_turns_milliseconds_into_a_window(
    intervals: tuple[tuple[int, int], ...], expected: str
) -> None:
    """毫秒 ⇒ 秒（三位小数 = 1ms 分辨率），多段用加法当"或"（ffmpeg 没有逻辑或）。"""
    assert enable_expr(intervals) == expected


def test_the_sticker_layer_ends_on_the_vstk_label(
    outputs: OutputsConfig, outputs_home: StudioPaths, tmp_path: Path
) -> None:
    """收尾标签与 ``-map`` 必须对得上（对不上时 ffmpeg 的报错读不出原因）。"""
    request = _request(outputs, tmp_path, stickers=_sticker_plans(outputs_home, _talking()))
    assert video_label(request) == "vstk"
    assert any(node.endswith("[vstk]") for node in build_filter_graph(request).split(";"))
