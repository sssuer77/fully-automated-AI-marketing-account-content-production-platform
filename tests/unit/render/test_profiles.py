"""合成 profile 与 `studio render profile --show`（T3.2 · §01.5.5 / §04.2.8.3）。

这一层唯一的职责是**翻译**：`config/outputs.yaml` 的四个旋钮 ⇒ ffmpeg 的 argv。
所以用例的重点不是"参数串长什么样"，而是**规格里写死的那几条不能少**：
CRF/preset/profile/level、bt709 三件套、`+faststart`、以及"没有 `-shortest`"。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from studio.core.config import OutputsConfig
from studio.core.errors import ConfigError, ErrorCode
from studio.core.paths import StudioPaths
from studio.render.profiles import (
    FALLBACK_PROFILE_NAME,
    H264_LEVEL,
    H264_PROFILE,
    RenderProfileReport,
    build_render_profile_report,
    resolve_profile,
)

from .conftest import write_png


def _args(profile_args: list[str]) -> dict[str, str]:
    """``["-crf", "21", "-preset", "medium"]`` ⇒ ``{"-crf": "21", "-preset": "medium"}``。

    只处理"旗标 + 一个值"的形式 —— 本模块生成的 argv 全是这种，没有位置参数。
    """
    return dict(zip(profile_args[::2], profile_args[1::2], strict=True))


# ══════════════════════════════════════════════════════════════════════
# profile 解析
# ══════════════════════════════════════════════════════════════════════


def test_default_profile_is_the_douyin_one(outputs: OutputsConfig) -> None:
    profile = resolve_profile(outputs)
    assert profile.name == outputs.default_profile
    assert profile.canvas == (1080, 1920)
    assert profile.fps == 30
    assert profile.vcodec == "libx264"
    assert profile.quality_field == "crf"
    assert profile.quality == 21
    assert profile.preset == "medium"
    assert profile.is_fallback is False


def test_unknown_profile_name_is_not_silently_replaced(outputs: OutputsConfig) -> None:
    """点名一个不存在的档 ⇒ 报错。悄悄回落到默认档 = "降级了但没降"。"""
    with pytest.raises(ConfigError) as excinfo:
        resolve_profile(outputs, "nope_v9")
    assert excinfo.value.code is ErrorCode.OUTPUTS_NOT_FOUND
    assert excinfo.value.context["known"]


def test_fallback_profile_exists(outputs: OutputsConfig) -> None:
    """降级链点名它（§04.2.8.6）—— 名字被删掉的话，降级路径会在最需要的时候断。"""
    assert FALLBACK_PROFILE_NAME in outputs.profiles
    assert resolve_profile(outputs, FALLBACK_PROFILE_NAME).is_fallback is True


def test_platform_lookup_never_returns_the_fallback(outputs: OutputsConfig) -> None:
    """保底档 `platforms: []` ⇒ 按平台选档永远选不到它（只能在降级时被点名）。

    反过来说：如果某天有人给保底档填了一个平台，那个平台的正常出片就会默默变成 720P 降级画质。
    """
    fallback = outputs.profiles[FALLBACK_PROFILE_NAME]
    assert list(fallback.platforms) == [], "保底档不能挂任何平台"
    for platform in ("douyin", "kuaishou", "shipinhao", "xiaohongshu", "bilibili", "xigua"):
        picked = outputs.profile_for_platform(platform)
        assert picked.platforms, f"{platform} 落到了一档没有平台的 profile"
        assert picked is not fallback, f"{platform} 选中了 720P 保底档"
    assert outputs.profile_for_platform("unknown-platform").width == 1080


# ══════════════════════════════════════════════════════════════════════
# 输出参数（§04.2.8.3 + §01.5.5）
# ══════════════════════════════════════════════════════════════════════


def test_libx264_args_follow_the_contract(outputs: OutputsConfig) -> None:
    args = _args(resolve_profile(outputs).output_args())
    assert args["-c:v"] == "libx264"
    assert args["-crf"] == "21"
    assert args["-preset"] == "medium"
    assert args["-pix_fmt"] == "yuv420p"
    assert args["-profile:v"] == H264_PROFILE == "high"
    assert args["-level"] == H264_LEVEL == "4.1"
    assert args["-r"] == "30"
    assert args["-g"] == "60"
    assert args["-keyint_min"] == "60"
    assert args["-sc_threshold"] == "0"
    assert args["-colorspace"] == "bt709"
    assert args["-color_primaries"] == "bt709"
    assert args["-color_trc"] == "bt709"
    assert args["-movflags"] == "+faststart"
    assert args["-c:a"] == "aac"
    assert args["-b:a"] == "192k"
    assert args["-ar"] == "48000"
    assert args["-ac"] == "2"


def test_no_shortest_and_no_explicit_duration(outputs: OutputsConfig) -> None:
    """军规：**禁用 `-shortest`**（陷阱 #3：视频流短于音频 ⇒ 提前截断/冻结）。

    `-t` 与 `-map` 也不在这一层 —— 它们要等 T3.3 按 ``total_ms`` 和输入分配来给。
    混进 profile 就变成"一档 profile 自带一个固定时长"，换任务就出错。
    """
    args = resolve_profile(outputs).output_args()
    assert "-shortest" not in args
    assert "-t" not in args
    assert "-map" not in args


def test_nvenc_args_use_cq_and_disable_the_bitrate_cap(outputs: OutputsConfig) -> None:
    """NVENC 的 `cq` 只在 `-rc vbr` 下有意义，且必须 `-b:v 0` 让 cq 说话。"""
    args = _args(resolve_profile(outputs, FALLBACK_PROFILE_NAME).output_args())
    assert args["-c:v"] == "h264_nvenc"
    assert args["-cq"] == "26"
    assert args["-rc"] == "vbr"
    assert args["-b:v"] == "0"
    assert args["-preset"] == "p4"
    assert args["-tune"] == "hq"
    assert "-crf" not in args
    assert "-profile:v" not in args, "NVENC 那行规格没写 profile/level，别自作主张加"
    assert args["-movflags"] == "+faststart"


def test_fallback_canvas_is_720p(outputs: OutputsConfig) -> None:
    assert resolve_profile(outputs, FALLBACK_PROFILE_NAME).canvas == (720, 1280)


def test_faststart_can_be_turned_off(outputs: OutputsConfig) -> None:
    """`+faststart` 是可关的（虽然 `config/outputs.yaml` 里全开着）：
    关掉之后 argv 里**不能留下一个空壳 `-movflags`**，否则 ffmpeg 会报缺参数。
    """
    profile = resolve_profile(outputs)
    assert profile.faststart is True
    assert "-movflags" in profile.output_args()
    off = replace(profile, faststart=False)
    assert off.container_args() == []
    assert "-movflags" not in off.output_args()


@pytest.mark.parametrize("name", ["douyin_1080x1920_30fps_v1", "xhs_1080x1440_30fps_v1"])
def test_every_profile_yields_a_complete_argv(outputs: OutputsConfig, name: str) -> None:
    args = resolve_profile(outputs, name).output_args()
    assert args and all(token for token in args)
    assert len(args) % 2 == 0, "全是「旗标 + 值」的成对形式，奇数说明拼错了"


def test_all_profiles_have_even_canvases(outputs: OutputsConfig) -> None:
    """yuv420p 色度对齐：画布宽高必须偶数（模型层已拦，这里再确认一次）。"""
    for name in outputs.profiles:
        profile = resolve_profile(outputs, name)
        assert profile.width % 2 == 0
        assert profile.height % 2 == 0


def test_profile_dict_is_json_ready(outputs: OutputsConfig) -> None:
    payload = resolve_profile(outputs).to_dict()
    assert json.loads(json.dumps(payload))["output_args"][0] == "-c:v"
    assert payload["is_fallback"] is False


# ══════════════════════════════════════════════════════════════════════
# `studio render profile --show` 的报告
# ══════════════════════════════════════════════════════════════════════


def test_report_skips_a_missing_watermark_without_blocking(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path
) -> None:
    """水印缺失 ⇒ 报告说"跳过"并给原因，**但报告本身照常出得来**（不是阻塞项）。"""
    report = build_render_profile_report(
        outputs, source=outputs_home.config_dir / "outputs.yaml", home=outputs_home.home
    )
    assert not watermark_path.exists()
    assert report.watermark_enabled is False
    assert report.watermark.placement is None
    assert report.watermark.skipped_reason is not None
    assert "不存在" in report.watermark.skipped_reason
    assert report.watermark.asset.exists is False


def test_report_places_a_valid_watermark(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path
) -> None:
    write_png(watermark_path, width=400, height=200)
    report = build_render_profile_report(
        outputs, source=outputs_home.config_dir / "outputs.yaml", home=outputs_home.home
    )
    assert report.watermark_enabled is True
    assert report.watermark.asset.usable is True
    placement = report.watermark.placement
    assert placement is not None
    assert placement.width_px == 238
    assert placement.height_px == 118
    assert placement.x % 2 == 0
    assert placement.y % 2 == 0
    assert placement.x == 1080 - 238 - 48


def test_report_skips_an_unusable_watermark(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path
) -> None:
    """不带透明通道的图贴上去是一块实心方块 ⇒ 跳过，且原因说得出是"不可用"。"""
    write_png(watermark_path, color_type=2)
    report = build_render_profile_report(
        outputs, source=outputs_home.config_dir / "outputs.yaml", home=outputs_home.home
    )
    assert report.watermark_enabled is False
    assert report.watermark.placement is None
    assert report.watermark.skipped_reason is not None
    assert "不可用" in report.watermark.skipped_reason


def test_report_uses_the_selected_profile_canvas(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path
) -> None:
    """保底档 720 宽 ⇒ 水印像素宽跟着变（158 = round(0.22*720)）。"""
    write_png(watermark_path, width=400, height=200)
    report = build_render_profile_report(
        outputs,
        source=outputs_home.config_dir / "outputs.yaml",
        home=outputs_home.home,
        name=FALLBACK_PROFILE_NAME,
    )
    assert report.profile.canvas == (720, 1280)
    assert report.watermark.spec.width_px == 158
    placement = report.watermark.placement
    assert placement is not None
    assert placement.x == 720 - 158 - 48


def test_report_carries_the_width_warning(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path
) -> None:
    """画布宽 1078（偶数）+ 0.22 ⇒ round(237.16)=237 是**奇数**，必须取偶并记账。

    这条与「超过 1/4 夹取」走的是同一条 warn 路径 —— 两者的修法都是改 width_ratio。
    """
    outputs.profiles["douyin_1080x1920_30fps_v1"] = outputs.profiles["douyin_1080x1920_30fps_v1"].model_copy(
        update={"width": 1078}
    )
    write_png(watermark_path, width=400, height=200)
    report = build_render_profile_report(
        outputs, source=outputs_home.config_dir / "outputs.yaml", home=outputs_home.home
    )
    assert report.watermark.spec.width_px == 236
    assert len(report.watermark.warnings) == 1


def test_report_lists_every_profile(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path
) -> None:
    write_png(watermark_path)
    report = build_render_profile_report(
        outputs, source=outputs_home.config_dir / "outputs.yaml", home=outputs_home.home
    )
    assert {item.name for item in report.profiles} == set(outputs.profiles)
    assert report.default_profile == outputs.default_profile


def test_report_has_no_blocking_fields() -> None:
    """报告里**没有**"能不能出片"的门禁字段 —— 一期没有任何输入能挡住出片。"""
    assert not hasattr(RenderProfileReport, "ready")
    assert not hasattr(RenderProfileReport, "blockers")


def test_report_dict_is_json_ready(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path
) -> None:
    write_png(watermark_path)
    payload = json.loads(
        json.dumps(
            build_render_profile_report(
                outputs, source=outputs_home.config_dir / "outputs.yaml", home=outputs_home.home
            ).to_dict()
        )
    )
    assert payload["watermark"]["enabled"] is True
    assert payload["watermark"]["asset"]["has_alpha"] is True
    assert payload["watermark"]["placement"]["width_px"] == 238
    assert payload["profile"]["output_args"][:2] == ["-c:v", "libx264"]
