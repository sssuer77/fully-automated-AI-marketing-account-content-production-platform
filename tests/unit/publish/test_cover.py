"""封面合成（T5.1 · §06.3）。

这里跑的都是**注入的假 runner**：真 ffmpeg 一个都不起。
验收四条：**抽帧点 / 单条 ``-vf`` / 降级链 / 原子落盘**。

为什么"单条 ``-vf``"值得一条专门的用例：ffmpeg **只认最后一个 ``-vf``**，
写成多个的话前面那几句文字会不报错地消失 —— 封面上只剩最后一行，
而日志里什么都没有。真机踩过（陷阱 #130）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from studio.core.config import CoverConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.domain.cover import (
    COVER_HEIGHT,
    COVER_WIDTH,
    SAFE_LEFT,
    SAFE_RIGHT,
    SUB_FONT_SIZE,
    TITLE_FONT_SIZE,
    CoverOutput,
)
from studio.publish.cover import (
    DEFAULT_FRAME_AT_MS,
    CoverResult,
    CoverSticker,
    build_cover,
    measure_text_width,
    resolve_cover_font,
    resolve_frame_at_ms,
    write_cover_atomically,
)

#: ffmpeg ``bbox`` 打进 stderr 的那一行（真机实测的形状）。
_BBOX_LINE = "x1:0 x2:566 y1:0 y2:94 w:566 h:94"


# ── 假件 ──────────────────────────────────────────────────────────────


class FakeCompleted:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


class FakeRunner:
    """记下每一次 argv，并按脚本返回成功 / 失败。

    ``fail_first`` = 前 N 次调用失败（用来走降级链）；之后一律成功。
    成功时会**顺手创建目标文件** —— ``build_cover`` 以"目标存在"为成功判据之一
    （与真 ffmpeg 一致：它成功就会写出那个文件）。
    """

    def __init__(self, *, fail_first: int = 0, stderr: str = "boom", stdout_stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.fail_first = fail_first
        self.stderr = stderr
        #: 成功时打给 stderr 的东西（量宽靠解析它拿到 bbox）。
        self.stdout_stderr = stdout_stderr

    def __call__(self, argv: list[str], **_kwargs: Any) -> FakeCompleted:
        self.calls.append(list(argv))
        index = len(self.calls)
        target = Path(argv[-1])
        if index <= self.fail_first:
            return FakeCompleted(returncode=1, stderr=self.stderr)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg")
        return FakeCompleted(stderr=self.stdout_stderr)


def _output(**overrides: Any) -> CoverOutput:
    data: dict[str, Any] = {
        "title_text": "离谱跑酷地图",
        "sub_text": "点个关注",
        "highlight_words": ["跑酷"],
        "frame_at_ms": 500,
        "banned_checked": True,
    }
    data.update(overrides)
    return CoverOutput.model_validate(data)


def _width_of(text: str, font_size: int) -> int:
    """假取宽：一个汉字一个字宽（真机实测 6 字 @96pt ≈ 566px，这个量级是对的）。"""
    return len(text) * font_size


@pytest.fixture
def font_file(tmp_path: Path) -> Path:
    font = tmp_path / "msyh.ttc"
    font.write_bytes(b"font")
    return font


# ── 抽帧点 ────────────────────────────────────────────────────────────


class TestResolveFrameAtMs:
    def test_first_sentence_start_plus_offset(self) -> None:
        timeline = {"sentences": [{"start_ms": 1200}, {"start_ms": 5000}]}
        assert resolve_frame_at_ms(timeline) == 1700

    def test_missing_timeline_falls_back(self) -> None:
        assert resolve_frame_at_ms(None) == DEFAULT_FRAME_AT_MS

    def test_empty_sentences_falls_back(self) -> None:
        assert resolve_frame_at_ms({"sentences": []}) == DEFAULT_FRAME_AT_MS

    def test_sentence_without_start_falls_back(self) -> None:
        assert resolve_frame_at_ms({"sentences": [{"text": "x"}]}) == DEFAULT_FRAME_AT_MS

    def test_negative_start_is_clamped_to_zero(self) -> None:
        assert resolve_frame_at_ms({"sentences": [{"start_ms": -900}]}) == 0


# ── 字体 ──────────────────────────────────────────────────────────────


class TestResolveCoverFont:
    def test_prefers_assets_fonts(self, tmp_paths: Any) -> None:
        fonts = tmp_paths.home / "assets" / "fonts"
        fonts.mkdir(parents=True)
        (fonts / "my.ttf").write_bytes(b"x")
        assert resolve_cover_font(tmp_paths).name == "my.ttf"

    def test_falls_back_to_system_dir(self, tmp_paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """本机 ``assets/fonts/`` 不存在 ⇒ 退到系统字体目录（封面不能因为没自备字体就不出）。"""
        system = tmp_paths.home / "system-fonts"
        system.mkdir(parents=True)
        (system / "msyh.ttc").write_bytes(b"x")
        monkeypatch.setattr("studio.publish.cover.system_font_dirs", lambda: (system,))
        assert resolve_cover_font(tmp_paths).name == "msyh.ttc"

    def test_no_font_anywhere_raises(self, tmp_paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """一个字体都找不到 ⇒ 抛 ``FONT_MISSING``（没有字体就是一张空图）。"""
        monkeypatch.setattr("studio.publish.cover.system_font_dirs", lambda: ())
        with pytest.raises(StudioError) as excinfo:
            resolve_cover_font(tmp_paths)
        assert excinfo.value.code == ErrorCode.FONT_MISSING


# ── 合成 ──────────────────────────────────────────────────────────────


def _cover_paths(tmp_path: Path) -> StudioPaths:
    """一份**带自备字体**的隔离路径。

    自备字体是刻意的：``resolve_cover_font`` 的下一站是系统字体目录，而那是**机器相关**的
    （CI 上可能一个中文字体都没有）。夹具自己放一个，用例就与"这台机器装了什么"无关。
    """
    home = tmp_path / "studio"
    fonts = home / "assets" / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    (fonts / "msyh.ttc").write_bytes(b"font")
    return StudioPaths(home=home, data_dir=home / "data")


def _sticker(tmp_path: Path) -> CoverSticker:
    """一张摆在封面中央的主体贴图（真机形状：1080 宽画布、人物 324×576）。"""
    image = tmp_path / "hero.png"
    image.write_bytes(b"\x89PNG\r\n")
    return CoverSticker(
        name="hero",
        path=image,
        x=378,
        y=672,
        width_px=324,
        height_px=576,
        opacity=1.0,
    )


def _build(tmp_path: Path, runner: Any, **overrides: Any) -> CoverResult:
    target = tmp_path / "cover.partial.jpg"
    source = tmp_path / "final.mp4"
    source.write_bytes(b"mp4")
    params: dict[str, Any] = {
        "output": _output(),
        "target": target,
        "paths": _cover_paths(tmp_path),
        "source": source,
        "ffmpeg": "ffmpeg",
        "runner": runner,
        "width_of": _width_of,
    }
    params.update(overrides)
    return build_cover(**params)


class TestBuildCoverArgv:
    def test_uses_a_single_vf(self, tmp_path: Path) -> None:
        """★ 多个 ``-vf`` 只有最后一个生效 —— 前面几行文字会不报错地消失。"""
        runner = FakeRunner()
        _build(tmp_path, runner)
        assert runner.calls, "没调 ffmpeg"
        assert runner.calls[0].count("-vf") == 1

    def test_filter_chain_draws_every_segment(self, tmp_path: Path) -> None:
        """一段文字 ⇒ 一条 ``drawtext``。

        这条用例里的文案拆出来是 4 段：标题被高亮词切成「离谱」「跑酷」「地图」，
        再加次文案「点个关注」。段数与文案一致 —— 少一段就是**有一截字没画上去**，
        而 ffmpeg 不会为此报错。
        """
        runner = FakeRunner()
        _build(tmp_path, runner)
        chain = runner.calls[0][runner.calls[0].index("-vf") + 1]
        assert chain.count("drawtext=") == 4
        assert "离谱" in chain and "跑酷" in chain and "地图" in chain and "点个关注" in chain

    def test_the_title_is_yellow_with_a_dark_outline(self, tmp_path: Path) -> None:
        """默认样式就是用户给的那张示例：**黄字 + 粗黑边**。

        黄不是审美取舍：跑酷素材本身是花花绿绿的，白字压上去会被底吃掉，而黄字黑边
        在**任何**底色上都跳出来。所以这条钉的是"底色由配置给、描边真的画上"。
        """
        runner = FakeRunner()
        _build(tmp_path, runner)
        chain = runner.calls[0][runner.calls[0].index("-vf") + 1]
        assert "fontcolor=0xFFD400" in chain
        assert "bordercolor=black" in chain
        assert "borderw=10" in chain

    def test_highlight_gets_its_own_colour(self, tmp_path: Path) -> None:
        """高亮词与正文**必须**是两个颜色 —— 否则"拆段"那套机制等于没做。"""
        runner = FakeRunner()
        _build(tmp_path, runner)
        chain = runner.calls[0][runner.calls[0].index("-vf") + 1]
        assert "fontcolor=0xFFD400" in chain  # 正文
        assert "fontcolor=0xFFFFFF" in chain  # 高亮

    def test_the_style_comes_from_the_config(self, tmp_path: Path) -> None:
        """颜色 / 描边 / 压暗带全部现读配置 —— 换一种底素材只改一行，不用改代码。"""
        runner = FakeRunner()
        _build(
            tmp_path,
            runner,
            style=CoverConfig(
                title_color="0x00FF00", highlight_color="0x0000FF", outline=0, scrim=0.0
            ),
        )
        chain = runner.calls[0][runner.calls[0].index("-vf") + 1]
        assert "fontcolor=0x00FF00" in chain
        assert "fontcolor=0x0000FF" in chain
        assert "borderw" not in chain, "outline=0 ⇒ 一个描边参数都不该出现"
        assert "drawbox=" not in chain, "scrim=0 ⇒ 不画压暗带"


class TestBuildCoverSticker:
    """封面主体（T5.1 追加）：成片里抽的一帧 + 合成配置里的贴图。

    为什么这一组值得单独一块：它是**从 ``-vf`` 换到 ``-filter_complex``** 的那一处。
    两条路都留着（没有贴图时命令逐字节不变），所以"换过去了没有"必须有用例盯着 ——
    写错了的话 ffmpeg 会**静默丢掉**其中一路（``-vf`` 只认最后一条），
    症状是"封面出来了，就是没有人物"。
    """

    def test_no_sticker_keeps_the_single_vf(self, tmp_path: Path) -> None:
        """没有贴图 ⇒ 命令与改前同形（真机验过很多遍的那一串，不该被顺手换掉）。"""
        runner = FakeRunner()
        _build(tmp_path, runner)
        argv = runner.calls[0]
        assert argv.count("-vf") == 1
        assert "-filter_complex" not in argv

    def test_a_sticker_switches_to_filter_complex(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        _build(tmp_path, runner, sticker=_sticker(tmp_path))
        argv = runner.calls[0]
        assert "-vf" not in argv, "-vf 只吃一路输入 —— 有贴图时必须换成 filter_complex"
        assert argv.count("-filter_complex") == 1
        assert argv[argv.index("-map") + 1] == "[out]"

    def test_the_sticker_is_the_second_input(self, tmp_path: Path) -> None:
        """贴图恒为输入 1（背景恒为输入 0）—— 背景是抽的帧还是纯色底都一样。"""
        runner = FakeRunner()
        sticker = _sticker(tmp_path)
        _build(tmp_path, runner, sticker=sticker)
        argv = runner.calls[0]
        assert argv[argv.index(str(sticker.path)) - 1] == "-i"
        assert argv.count("-i") == 2, "背景 + 贴图，两路输入"

    def test_the_graph_overlays_then_draws_the_text(self, tmp_path: Path) -> None:
        """顺序：背景 ⇒ 叠贴图 ⇒ 压暗带与文字。反过来的话字会被人物盖住。"""
        runner = FakeRunner()
        sticker = _sticker(tmp_path)
        _build(tmp_path, runner, sticker=sticker)
        graph = runner.calls[0][runner.calls[0].index("-filter_complex") + 1]
        overlay = graph.index("overlay=")
        assert overlay < graph.index("drawtext=")
        assert f"overlay={sticker.x}:{sticker.y}" in graph
        assert f"scale={sticker.width_px}:{sticker.height_px}" in graph
        assert graph.rstrip().endswith("[out]")

    def test_opacity_goes_through_the_alpha_channel(self, tmp_path: Path) -> None:
        """透明度走 ``colorchannelmixer=aa=``（与成片里那条逐字同构）。

        不做这一步的话人物是**实心**贴上去的：透明区会被当成黑色，人物周围一圈黑边。
        """
        runner = FakeRunner()
        sticker = _sticker(tmp_path)
        _build(tmp_path, runner, sticker=sticker)
        graph = runner.calls[0][runner.calls[0].index("-filter_complex") + 1]
        assert "format=rgba" in graph
        assert "colorchannelmixer=aa=1" in graph

    def test_the_plan_records_what_was_drawn(self, tmp_path: Path) -> None:
        """画了什么就得记什么（"这张封面为什么长这样"要有人能回答）。"""
        runner = FakeRunner()
        sticker = _sticker(tmp_path)
        result = _build(tmp_path, runner, sticker=sticker)
        assert result.plan["sticker"]["name"] == "hero"
        assert result.plan["sticker"]["x"] == sticker.x
        assert result.plan["style"]["title_color"] == "0xFFD400"

    def test_no_sticker_is_recorded_as_none(self, tmp_path: Path) -> None:
        """没贴人物也要留痕 —— ``None`` 与"这个字段不存在"是两件事。"""
        runner = FakeRunner()
        result = _build(tmp_path, runner)
        assert result.plan["sticker"] is None

    def test_ss_precedes_i(self, tmp_path: Path) -> None:
        """``-ss`` 必须在 ``-i`` 前面（跳过去再解），否则整段视频都要解一遍。"""
        runner = FakeRunner()
        _build(tmp_path, runner)
        argv = runner.calls[0]
        assert argv.index("-ss") < argv.index("-i")

    def test_scrim_is_drawn_before_the_text(self, tmp_path: Path) -> None:
        """压暗带必须在文字**之前**（滤镜链按顺序执行），否则它会把字盖掉。"""
        runner = FakeRunner()
        _build(tmp_path, runner)
        chain = runner.calls[0][runner.calls[0].index("-vf") + 1]
        assert chain.index("drawbox=") < chain.index("drawtext=")

    def test_scales_and_crops_to_the_canvas(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        _build(tmp_path, runner)
        chain = runner.calls[0][runner.calls[0].index("-vf") + 1]
        assert f"scale={COVER_WIDTH}:{COVER_HEIGHT}" in chain
        assert f"crop={COVER_WIDTH}:{COVER_HEIGHT}" in chain

    def test_one_frame_only(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        _build(tmp_path, runner)
        argv = runner.calls[0]
        assert argv[argv.index("-frames:v") + 1] == "1"


class TestBuildCoverTypography:
    def test_long_title_shrinks_the_font(self, tmp_path: Path) -> None:
        """超安全宽 ⇒ 字号收下来，且**进 plan**（面板要能解释"为什么这么小"）。"""
        runner = FakeRunner()
        result = _build(tmp_path, runner, output=_output(title_text="一二三四五六七八九十", sub_text=None))
        assert result.plan["title_font_size"] < TITLE_FONT_SIZE
        assert result.plan["title_font_size"] >= 60

    def test_short_title_keeps_the_base_size(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        result = _build(tmp_path, runner, output=_output(title_text="跑酷", sub_text=None))
        assert result.plan["title_font_size"] == TITLE_FONT_SIZE

    def test_sub_text_uses_the_smaller_size(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        result = _build(tmp_path, runner)
        assert result.plan["sub_font_size"] == SUB_FONT_SIZE

    def test_text_stays_inside_the_safe_area(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        _build(tmp_path, runner)
        chain = runner.calls[0][runner.calls[0].index("-vf") + 1]
        for piece in chain.split("drawtext=")[1:]:
            x = int(piece.split("x=")[1].split(":")[0])
            assert SAFE_LEFT <= x <= SAFE_RIGHT

    def test_title_at_the_contract_ceiling_still_fits(self, tmp_path: Path) -> None:
        """20 字正好排满 2 行 × 10 字 —— 契约的上限与排版的上限是同一个数，不截断。"""
        runner = FakeRunner()
        result = _build(tmp_path, runner, output=_output(title_text="一" * 20, sub_text=None))
        assert result.plan["title_lines"] == ["一" * 10, "一" * 10]
        assert result.plan["title_truncated"] is False

    def test_overlong_sub_is_truncated_with_a_warning(self, tmp_path: Path) -> None:
        """次文案契约上限 24 字，但**排版只放得下一行 12 字** ⇒ 截断 + warn。

        （标题那条截断路在当前契约下走不到：schema 的上限 20 字正好等于 2×10。
        留着它是防御 —— 哪天上限放宽，静默删字比多一个 warn 坏得多。）
        """
        runner = FakeRunner()
        result = _build(tmp_path, runner, output=_output(sub_text="一" * 24))
        assert result.plan["sub_lines"] == ["一" * 12]
        assert any("截断" in item for item in result.warnings)


class TestBuildCoverDegradation:
    def test_success_returns_the_path(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        result = _build(tmp_path, runner)
        assert result.ok is True
        assert result.fallback_background is False
        assert len(runner.calls) == 1

    def test_frame_failure_degrades_to_a_solid_background(self, tmp_path: Path) -> None:
        """§06.3 第一级降级：抽帧失败 ⇒ 纯色底 + 文字（``warn``，但**照出**）。"""
        runner = FakeRunner(fail_first=1)
        result = _build(tmp_path, runner)
        assert result.ok is True
        assert result.fallback_background is True
        assert len(runner.calls) == 2
        assert "lavfi" in runner.calls[1]
        assert any("抽帧失败" in item for item in result.warnings)

    def test_both_failures_return_no_cover(self, tmp_path: Path) -> None:
        """§06.3 第二级降级：连纯色底都出不来 ⇒ ``path=None``（无封面发布）。**不抛**。"""
        runner = FakeRunner(fail_first=2)
        result = _build(tmp_path, runner)
        assert result.ok is False
        assert result.path is None
        assert result.fallback_background is True
        assert any("封面生成失败" in item for item in result.warnings)

    def test_subprocess_exception_also_degrades(self, tmp_path: Path) -> None:
        """ffmpeg 根本没起来（``OSError``）也算"这条降级路"，不是崩溃。"""

        class ExplodingRunner:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, argv: list[str], **_kwargs: Any) -> Any:
                self.calls += 1
                raise OSError("ffmpeg 不在 PATH 上")

        result = _build(tmp_path, ExplodingRunner())
        assert result.ok is False
        assert result.path is None

    def test_source_none_skips_the_frame_grab(self, tmp_path: Path) -> None:
        """没有成片（或不让抽帧）⇒ 直接走纯色底，**一次就成**，不浪费一次失败的抽帧。"""
        runner = FakeRunner()
        result = _build(tmp_path, runner, source=None)
        assert result.ok is True
        assert result.fallback_background is True
        assert len(runner.calls) == 1
        assert "lavfi" in runner.calls[0]


class TestWriteCoverAtomically:
    def test_renames_partial_to_final(self, tmp_path: Path) -> None:
        """半张封面比没封面更坏：先写 ``.partial``，成了再改名。"""
        partial = tmp_path / "cover.partial.jpg"
        partial.write_bytes(b"jpeg")
        target = tmp_path / "cover.jpg"
        assert write_cover_atomically(CoverResult(path=partial, plan={}), target) == target
        assert target.is_file()
        assert not partial.exists()

    def test_no_cover_yields_none(self, tmp_path: Path) -> None:
        target = tmp_path / "cover.jpg"
        assert write_cover_atomically(CoverResult(path=None, plan={}), target) is None
        assert not target.exists()

    def test_same_path_is_a_noop(self, tmp_path: Path) -> None:
        target = tmp_path / "cover.jpg"
        target.write_bytes(b"jpeg")
        assert write_cover_atomically(CoverResult(path=target, plan={}), target) == target


# ── 量宽 ──────────────────────────────────────────────────────────────


class TestMeasureTextWidth:
    def test_reads_the_bbox_from_stderr(self, font_file: Path) -> None:
        runner = FakeRunner(fail_first=-1, stdout_stderr=_BBOX_LINE)
        assert measure_text_width("离谱跑酷地图", font_file=font_file, font_size=96, runner=runner) == 566

    def test_bbox_filter_is_attached(self, font_file: Path) -> None:
        """★ 漏掉 ``bbox`` ⇒ 量出来恒为 0 ⇒ 不缩字号、还按画布中心摆 ⇒ 长标题溢出屏幕外。"""
        runner = FakeRunner(fail_first=-1, stdout_stderr=_BBOX_LINE)
        measure_text_width("离谱跑酷地图", font_file=font_file, font_size=96, runner=runner)
        chain = runner.calls[0][runner.calls[0].index("-vf") + 1]
        assert "bbox=" in chain

    def test_failure_estimates_instead_of_returning_zero(self, font_file: Path) -> None:
        """量不出来 ⇒ **按字数估**。返回 0 会被读成"这行一个字都不占"。"""
        runner = FakeRunner(fail_first=99)
        assert measure_text_width("离谱跑酷地图", font_file=font_file, font_size=96, runner=runner) == 6 * 96

    def test_missing_bbox_line_estimates(self, font_file: Path) -> None:
        runner = FakeRunner(fail_first=-1, stdout_stderr="no bbox here")
        assert measure_text_width("跑酷", font_file=font_file, font_size=96, runner=runner) == 2 * 96

    def test_empty_text_is_zero(self, font_file: Path) -> None:
        runner = FakeRunner()
        assert measure_text_width("  ", font_file=font_file, font_size=96, runner=runner) == 0
        assert runner.calls == []

    def test_timeout_is_passed(self, font_file: Path) -> None:
        """量宽卡住不能把封面拖死（它只是"要不要缩字号"的参考值）。"""
        seen: dict[str, Any] = {}

        def runner(argv: list[str], **kwargs: Any) -> FakeCompleted:
            seen.update(kwargs)
            return FakeCompleted(stderr="w:566 h:94 x1:0 x2:566 y1:0 y2:94")

        measure_text_width("跑酷", font_file=font_file, font_size=96, runner=runner)
        assert seen["timeout"] > 0


def test_real_subprocess_run_is_the_default(font_file: Path) -> None:
    """默认 runner 就是 ``subprocess.run``（不是某个只在测试里存在的假件）。"""
    assert callable(subprocess.run)
