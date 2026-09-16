"""端到端：文案 → 配音 → 出一支 MP4（T3.x 主线的验收）。

这条用例就是"第一支片子"的自动化版本：喂一段中文口播，最后断言盘上有一个
**真的能播**的 mp4（有画面流、有声音流、时长对得上）。

为什么要真跑 ffmpeg / 真调 SAPI
-------------------------------
整条链路的失败模式几乎全在"两个组件之间的接缝"上：滤镜图里 ``[1:a]`` 指的是不是
人声、``-t`` 有没有生效、音频采样率对不对……这些用 mock 一个都测不出来。
代价是用例慢（几秒），所以打 ``e2e`` / ``slow`` 标记。

环境不满足（没 ffmpeg / 没 SAPI 音色）⇒ **skip**，不是 fail：
CI 上缺音色不该把"渲染链路写错了"和"这台机器没装语音包"混成同一个红灯。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.errors import ErrorCode, RenderError
from studio.core.media import probe_media, run_command
from studio.core.paths import StudioPaths
from studio.render import degrade
from studio.render.composite import CompositeRequest, CompositeResult, build_filter_graph, run_composite
from studio.render.mixdown import MixSettings
from studio.render.profiles import FALLBACK_PROFILE_NAME, resolve_profile
from studio.services.render_service import ProduceRequest, produce_video
from studio.tts.sapi import sapi_available

pytestmark = pytest.mark.e2e

#: 一段够短、够有标点的中文口播（会被切成好几句 —— 顺带验切分）
SPEECH = "今天我们来看一张特别离谱的跑酷地图。开局只有一格方块，脚下就是虚空。我试了三次，全都掉下去了。"


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("本机没有 ffmpeg")
    return binary


def _require_voice() -> None:
    if not sapi_available():
        pytest.skip("本机没有 SAPI 音色（装一个中文语音包再跑）")


def _require_nvenc() -> None:
    """保底档（``fallback_720x1280_v1``）编的是 ``h264_nvenc``。

    本机没有 NVENC 时，保底档自己也会失败 —— 那时这条用例测的是"这台机器没有显卡"，
    不是"降级链写错了"，所以跳过而不是失败。**这个坑本身是配置问题**：保底档应当比
    正常档**更容易成功**，而它现在依赖一个可能不存在的硬件编码器（见 T3.7 清单的已知偏差）。
    """
    result = run_command(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=30:d=0.1",
            "-c:v",
            "h264_nvenc",
            "-f",
            "null",
            "-",
        ]
    )
    if not result.ok:
        pytest.skip("本机没有可用的 h264_nvenc，720P 保底档在这里跑不起来")


def _run(argv: list[str]) -> None:
    result = run_command(argv)
    assert result.ok, f"{argv} ⇒ {result.tail()}"


def _make_clip(target: Path, *, seconds: float = 2.0) -> Path:
    """造一条 2 秒的测试底片（比搬一个 18MB 的真素材快得多）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=640x360:rate=30:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(target),
        ]
    )
    return target


def _make_watermark(target: Path) -> Path:
    """用 ffmpeg 造一张**带透明通道**的水印 PNG（真 PNG，不是字节拼的假货）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=white@0.6:size=400x200,format=rgba",
            "-frames:v",
            "1",
            "-y",
            str(target),
        ]
    )
    return target


def _stage_home(tmp_path: Path, *, with_clip: bool = True) -> StudioPaths:
    """一个带配置的临时家目录（测试**不碰**真实 data/ 与 templates/）。

    ``with_clip=False`` ⇒ 素材目录**空着**，用来验黑屏降级那条路（§04.2.8.6）。
    以前这个函数只有一个形态，于是"没有底片"这条分支在端到端里从来没被走过 ——
    而它恰恰是无人值守时最常发生的一种。
    """
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    (paths.home / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        Path(__file__).resolve().parents[2] / "config" / "outputs.yaml",
        paths.config_dir / "outputs.yaml",
    )
    if with_clip:
        _make_clip(paths.mc_parkour_dir / "parkour_test.mp4")
    return paths


def _outputs(paths: StudioPaths) -> OutputsConfig:
    return load_outputs_config(paths.config_dir / "outputs.yaml")


def _watermark_path(paths: StudioPaths) -> Path:
    config = _outputs(paths).watermark
    return config.path if config.path.is_absolute() else paths.home / config.path


@pytest.mark.slow
def test_script_to_voice_to_mp4(tmp_path: Path) -> None:
    """★ 主线：一段文案进去，一支能播的 MP4 出来。"""
    _require_voice()
    paths = _stage_home(tmp_path)

    result = produce_video(
        ProduceRequest(task_id="e2e001", text=SPEECH, seed=1),
        paths=paths,
        outputs=_outputs(paths),
    )

    assert result.final.is_file()
    assert result.final.stat().st_size > 0

    info = probe_media(result.final)
    assert info.has_video, "成片必须有画面流"
    assert info.has_audio, "成片必须有声音流"
    assert (info.width, info.height) == (1080, 1920)
    assert info.audio_codec == "aac"

    # 时长以人声为准（+ tail_ms），允许编码带来的少量出入
    assert abs(info.duration_ms - result.duration_ms) < 1000
    assert result.voice_duration_ms > 0

    # 配音这一步真的产出了逐句文件
    assert result.voice is not None
    assert len(result.voice.sentences) >= 3
    assert all(path.is_file() for path in result.voice.sentence_files)

    # 留痕可读
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["task_id"] == "e2e001"
    assert manifest["duration_ms"] == result.duration_ms
    assert manifest["argv"]


@pytest.mark.slow
def test_watermark_is_optional_end_to_end(tmp_path: Path) -> None:
    """没水印照样出片；放了水印就贴上去 —— 两种都**成功**（这是这次改动的核心）。"""
    _require_voice()
    paths = _stage_home(tmp_path)

    # ① 没有水印文件 ⇒ 跳过水印，但片子照出
    skipped = produce_video(
        ProduceRequest(task_id="e2e002", text=SPEECH, seed=1),
        paths=paths,
        outputs=_outputs(paths),
    )
    assert skipped.final.is_file()
    assert skipped.watermark_enabled is False
    assert skipped.watermark_skipped_reason

    # ② 放一张带透明通道的水印 ⇒ 贴上去
    _make_watermark(_watermark_path(paths))
    placed = produce_video(
        ProduceRequest(task_id="e2e002", text=SPEECH, seed=1, reuse_voice=True),
        paths=paths,
        outputs=_outputs(paths),
    )
    assert placed.final.is_file()
    assert placed.watermark_enabled is True
    assert placed.watermark_skipped_reason is None


@pytest.mark.slow
def test_subtitle_is_burned_and_optional(tmp_path: Path) -> None:
    """★ 字幕：默认烧（时间取自逐句实测）· 关掉开关仍能出片（可选性验证）。

    这条用例覆盖的是**两个失败模式**：
    ① 字体找不到时 libass 画出一排豆腐块 —— 所以这里断言"ASS 真的生成了"；
    ② 字幕层把整条链路拖死 —— 所以这里断言"关掉它照样出片"。
    """
    _require_voice()
    paths = _stage_home(tmp_path)

    burned = produce_video(
        ProduceRequest(task_id="e2e004", text=SPEECH, seed=1), paths=paths, outputs=_outputs(paths)
    )
    assert burned.subtitle.enabled is True, burned.subtitle.skipped_reason
    ass = burned.subtitle.ass_path
    assert ass is not None and ass.is_file()
    # 句级时间轴与 ASS 一一对应（3 句 ⇒ 3 个 Dialogue）
    body = ass.read_text(encoding="utf-8")
    assert body.count("Dialogue:") == len(burned.subtitle.cues) >= 3
    # §04.2.6：UTF-8 无 BOM + LF
    raw = ass.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    # 留痕里也要说清楚（面板读的就是它）
    manifest = json.loads(burned.manifest.read_text(encoding="utf-8"))
    assert manifest["subtitle"]["enabled"] is True
    assert manifest["timeline"]

    # ② 显式关掉 ⇒ 不产 ASS，但片子照出
    plain = produce_video(
        ProduceRequest(task_id="e2e004", text=SPEECH, seed=1, reuse_voice=True, subtitle=False),
        paths=paths,
        outputs=_outputs(paths),
    )
    assert plain.subtitle.enabled is False
    assert plain.subtitle.ass_path is None
    assert plain.final.is_file()
    assert probe_media(plain.final).has_audio


@pytest.mark.slow
def test_bgm_is_mixed_in_when_present(tmp_path: Path) -> None:
    """★ 有 BGM ⇒ 真的混进去（侧链 + amix + 两遍 loudnorm 那条分支）。"""
    _require_voice()
    paths = _stage_home(tmp_path)
    paths.bgm_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=4",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-y",
            str(paths.bgm_dir / "bgm_test.mp3"),
        ]
    )

    result = produce_video(
        ProduceRequest(task_id="e2e005", text=SPEECH, seed=1), paths=paths, outputs=_outputs(paths)
    )
    assert result.bgm is not None
    assert result.composite.bgm_applied is True

    graph = build_filter_graph(
        CompositeRequest(
            profile=resolve_profile(_outputs(paths)),
            clip=result.clip,
            voice=paths.voice_master("e2e005"),
            output=result.final,
            duration_ms=result.duration_ms,
            bgm=result.bgm,
            mix=MixSettings.from_config(_outputs(paths).audio),
        )
    )
    assert "sidechaincompress=" in graph
    assert "normalize=0" in graph
    # BGM 在输入侧循环（滤镜侧 aloop 会申请几 GB 缓冲）
    assert "-stream_loop" in (paths.graphs_dir_for("e2e005") / "composite.txt").read_text(encoding="utf-8")
    assert probe_media(result.final).has_audio


@pytest.mark.slow
def test_reuse_voice_skips_the_tts_stage(tmp_path: Path) -> None:
    """``reuse_voice`` 第二次不再重合成 —— 反复调渲染参数时每次省掉配音那几十秒。"""
    _require_voice()
    paths = _stage_home(tmp_path)
    request = ProduceRequest(task_id="e2e003", text=SPEECH, seed=1)

    first = produce_video(request, paths=paths, outputs=_outputs(paths))
    assert first.voice is not None

    second = produce_video(
        ProduceRequest(task_id="e2e003", text=SPEECH, seed=1, reuse_voice=True),
        paths=paths,
        outputs=_outputs(paths),
    )
    assert second.voice is None  # 没有新产物 ⇒ 复用
    assert second.voice_duration_ms == first.voice_duration_ms
    assert second.final.is_file()


@pytest.mark.slow
def test_empty_asset_pool_still_produces_a_playable_video(tmp_path: Path) -> None:
    """★ **素材为空 ⇒ 黑屏出片**（T3.7 DoD 的一条）。

    这是 T3.7 把"没有底片"从**真失败**挪进**降级**的全部理由：素材是人工收集的、
    随时可能被清空或全部禁用，而"今天没有底片"不该等于"今天出不了片"。

    断言分三层，缺一层这条用例就白写：
    ① 片子**真的能播**（有画面流、有声音流、尺寸对）—— 只看"文件在不在"会放过
       一个 0 字节的假货；
    ② 留痕**如实**（``clip`` 写 null 而不是编一个假路径、``bg_fill='black'``、
       ``degraded=True``）—— 下游（重复度审计、素材使用统计）靠它分清"画面不是素材给的"；
    ③ **QC 也照跑**（成片响度落在发布门禁里）—— 黑屏降级降的是画质，不是音频。
    """
    _require_voice()
    paths = _stage_home(tmp_path, with_clip=False)

    result = produce_video(
        ProduceRequest(task_id="e2e006", text=SPEECH, seed=1), paths=paths, outputs=_outputs(paths)
    )

    # ① 能播
    info = probe_media(result.final)
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (1080, 1920)

    # ② 留痕如实
    assert result.clip is None
    assert result.bg_fill == "black"
    assert result.degraded is True
    assert result.degrade_reason == "no_broll_assets"
    assert result.composite_hash  # 指纹照算（黑底也是一次确定的合成）
    assert any("纯黑底" in warning for warning in result.warnings)
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["clip"] is None
    assert manifest["bg_fill"] == "black"
    assert manifest["degrade_reason"] == "no_broll_assets"
    # 黑底是 lavfi 现造的，不是 `-stream_loop` 循环一段不存在的素材
    graph = (paths.graphs_dir_for("e2e006") / "composite.txt").read_text(encoding="utf-8")
    assert "color=c=black" in graph

    # ③ QC 照跑：黑屏降级不降音频
    assert result.output_loudness is not None
    assert -16.5 <= result.output_loudness.input_i <= -15.5, result.output_loudness.input_i
    assert result.output_loudness.input_tp <= -1.0, result.output_loudness.input_tp


@pytest.mark.slow
def test_injected_encode_failure_falls_back_to_720p(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ **注入编码失败 ⇒ 720P 保底出片**（T3.7 DoD 的另一条）。

    注入点在 ``degrade.run_composite``：**第一次**（正常档）抛 ``RENDER_FAILED``，
    **第二次**（保底档）走真实现。这样测的是"降级链真的把档位换掉了、而且换完真的
    出得来片"，而不是"我们写了一个 try/except"。

    真实世界里这个失败长这样：机器没有 NVENC、显存被占满、某个滤镜在这个 ffmpeg
    build 里不存在 —— 换一档编码参数就能绕过去，而"再试一次同样的命令"不行。
    """
    _require_voice()
    _require_nvenc()
    paths = _stage_home(tmp_path)
    real = run_composite
    seen: list[str] = []

    def flaky(request: CompositeRequest, **kwargs: Any) -> CompositeResult:
        seen.append(request.profile.name)
        if len(seen) == 1:
            raise RenderError(
                "注入的编码失败（模拟 NVENC 不可用）",
                code=ErrorCode.RENDER_FAILED,
                context={"profile": request.profile.name},
                remediation="换 720P 保底档",
            )
        return real(request, **kwargs)

    monkeypatch.setattr(degrade, "run_composite", flaky)

    result = produce_video(
        ProduceRequest(task_id="e2e007", text=SPEECH, seed=1), paths=paths, outputs=_outputs(paths)
    )

    assert seen == ["douyin_1080x1920_30fps_v1", FALLBACK_PROFILE_NAME]
    assert result.profile_name == FALLBACK_PROFILE_NAME
    assert result.degraded is True
    assert result.degrade_reason == "render_720p"
    # 保底产物的文件名里带 _720p：翻目录时也看得出哪支是保底产物
    assert result.final.name.endswith("_final_720p.mp4")
    assert any("720P 保底档" in warning for warning in result.warnings)

    info = probe_media(result.final)
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (720, 1280)

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["profile"] == FALLBACK_PROFILE_NAME
    assert manifest["attempts"] == ["douyin_1080x1920_30fps_v1: RENDER_FAILED"]
