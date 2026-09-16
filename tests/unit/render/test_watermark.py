"""固定水印：参数模型 + PNG 实测 + 编译期摆放（T3.2 · §04.2.8.2）。

验收四条（`todolist.md` T3.2）：**位置枚举 / 边距偶数 / 宽度上限（画布 1/4）/ 透明度范围**。
外加一条口径：水印是**可选装饰** —— 缺失 / 坏文件 / 放不下都只是**跳过**，
绝不阻塞出片（成片优先；早先那版把它做成硬门禁，结果是没水印就一支片子都出不来）。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from studio.core.config import CANVAS_WIDTH_DIVISOR, WatermarkConfig, WatermarkPosition
from studio.core.errors import ErrorCode, RenderError
from studio.render.watermark import (
    PNG_SIGNATURE,
    WatermarkAsset,
    WatermarkSpec,
    place_watermark,
    plan_watermark,
    probe_watermark,
    resolve_watermark,
)

from .conftest import png_bytes, write_png

# ── 夹具 ──────────────────────────────────────────────────────────────

SPEC_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right", "center")


def _config(**overrides: object) -> WatermarkConfig:
    """一份合法的水印配置（默认值照 `config/outputs.yaml`）。"""
    data: dict[str, object] = {
        "path": "templates/t/assets/images/watermark.png",
        "position": "bottom_right",
        "margin_x": 48,
        "margin_y": 96,
        "width_ratio": 0.22,
        "opacity": 0.85,
    }
    data.update(overrides)
    return WatermarkConfig.model_validate(data)


def _asset(path: Path, *, width: int = 200, height: int = 100) -> WatermarkAsset:
    return WatermarkAsset(
        path=path,
        exists=True,
        width_px=width,
        height_px=height,
        has_alpha=True,
        sha256="0" * 64,
    )


def _spec(**overrides: object) -> WatermarkSpec:
    data: dict[str, object] = {
        "image_path": Path("watermark.png"),
        "position": "bottom_right",
        "margin_px": (48, 96),
        "width_px": 238,
        "opacity": 0.85,
    }
    data.update(overrides)
    return WatermarkSpec.model_validate(data)


# ══════════════════════════════════════════════════════════════════════
# ① 位置枚举
# ══════════════════════════════════════════════════════════════════════


def test_position_enum_matches_spec() -> None:
    """§04.2.8.2 的五个位置一个不少、一个不多（面板的下拉框就是这个注解现取的）。"""
    assert get_args(WatermarkPosition) == SPEC_POSITIONS


def test_spec_defaults_follow_the_contract() -> None:
    """默认值照抄 §04.2.8.2：右下角 / 边距 (40,40) / 宽 220 / 透明度 0.85。"""
    spec = WatermarkSpec(image_path=Path("watermark.png"))
    assert spec.position == "bottom_right"
    assert spec.margin_px == (40, 40)
    assert spec.width_px == 220
    assert spec.opacity == 0.85
    assert spec.apply_to == "full"
    assert spec.enabled is True


def test_unknown_position_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(position="middle")


def test_every_position_is_accepted_by_the_config() -> None:
    for position in SPEC_POSITIONS:
        assert _config(position=position).position == position


# ══════════════════════════════════════════════════════════════════════
# ② 边距必须偶数
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(("margin_x", "margin_y"), [(49, 96), (48, 97), (1, 1)])
def test_odd_margin_rejected_by_config(margin_x: int, margin_y: int) -> None:
    with pytest.raises(ValidationError):
        _config(margin_x=margin_x, margin_y=margin_y)


@pytest.mark.parametrize(("margin_x", "margin_y"), [(49, 96), (48, 97)])
def test_odd_margin_rejected_by_spec(margin_x: int, margin_y: int) -> None:
    """两道防线：面板写不进去（config），手搓的计划也构造不出来（spec）。"""
    with pytest.raises(ValidationError):
        _spec(margin_px=(margin_x, margin_y))


@pytest.mark.parametrize(("margin_x", "margin_y"), [(-2, 0), (0, -2)])
def test_negative_margin_rejected(margin_x: int, margin_y: int) -> None:
    with pytest.raises(ValidationError):
        _config(margin_x=margin_x, margin_y=margin_y)


def test_even_margin_accepted() -> None:
    assert _config(margin_x=0, margin_y=200).margin_x == 0


# ══════════════════════════════════════════════════════════════════════
# ③ 宽度上限 = 画布 1/4（且取偶）
# ══════════════════════════════════════════════════════════════════════


def test_width_ratio_cap_is_a_quarter() -> None:
    assert CANVAS_WIDTH_DIVISOR == 4
    with pytest.raises(ValidationError):
        _config(width_ratio=0.2501)
    assert _config(width_ratio=0.25).width_ratio == 0.25


@pytest.mark.parametrize("width_ratio", [0.0, -0.1])
def test_width_ratio_must_be_positive(width_ratio: float) -> None:
    with pytest.raises(ValidationError):
        _config(width_ratio=width_ratio)


def test_width_px_follows_the_canvas() -> None:
    """同一个比例，换画布就是另一个像素宽（保底档 720 宽 ⇒ 158px）。"""
    config = _config()
    assert config.width_px_for(1080) == 238
    assert config.width_px_for(720) == 158
    assert config.width_px_raw(1080) == 238


def test_width_px_is_capped_at_a_quarter_even_when_rounding_overshoots() -> None:
    """奇数画布宽 + round() 会越界 1px：1078 ⇒ round(269.5)=270 > 1078//4=269。

    这条是"夹取"存在的**唯一**理由（``width_ratio`` 的上限已经是 0.25）。
    """
    config = _config(width_ratio=0.25)
    assert config.width_px_raw(1078) == 270
    assert config.width_px_for(1078) == 268  # 夹到 269，再取偶


@pytest.mark.parametrize("canvas_width", [64, 720, 1078, 1080, 1920, 3840, 7680])
@pytest.mark.parametrize("width_ratio", [0.01, 0.1, 0.22, 0.25])
def test_width_px_is_always_even_and_never_over_a_quarter(canvas_width: int, width_ratio: float) -> None:
    """不变量：``width_px`` 恒为偶数，且恒 ≤ 画布 1/4。

    偶数是为了 ``overlay`` 的 x 为偶数（右下角 x = W - w - margin，W 与 margin 已偶）；
    上限是规格硬要求。两条都由**同一个方法**保证 —— 面板与编译器读的是它。
    """
    width_px = _config(width_ratio=width_ratio).width_px_for(canvas_width)
    assert width_px % 2 == 0
    assert width_px <= canvas_width // CANVAS_WIDTH_DIVISOR
    assert width_px >= 2


def test_spec_rejects_odd_width() -> None:
    with pytest.raises(ValidationError):
        _spec(width_px=237)


def test_spec_rejects_degenerate_width() -> None:
    with pytest.raises(ValidationError):
        _spec(width_px=0)


# ══════════════════════════════════════════════════════════════════════
# ④ 透明度范围
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("opacity", [0.0, 0.5, 1.0])
def test_opacity_in_range_accepted(opacity: float) -> None:
    assert _config(opacity=opacity).opacity == opacity
    assert _spec(opacity=opacity).opacity == opacity


@pytest.mark.parametrize("opacity", [-0.01, 1.01, 2.0])
def test_opacity_out_of_range_rejected(opacity: float) -> None:
    with pytest.raises(ValidationError):
        _config(opacity=opacity)
    with pytest.raises(ValidationError):
        _spec(opacity=opacity)


# ══════════════════════════════════════════════════════════════════════
# 开关：计划层可以关，面板那一侧没有这个旋钮
# ══════════════════════════════════════════════════════════════════════


def test_spec_accepts_enabled_false() -> None:
    """计划层允许关掉水印（早先的版本连构造都拒绝，那等于没有退路）。"""
    assert WatermarkSpec(image_path=Path("w.png"), enabled=False).enabled is False


def test_plan_defaults_to_enabled_when_the_png_is_there(tmp_path: Path) -> None:
    """配置里没有开关：文件在 ⇒ 贴。默认行为必须是"贴"，不然水印就成了摆设。"""
    path = write_png(tmp_path / "watermark.png")
    plan = plan_watermark(
        _config(path=str(path)),
        canvas_width=1080,
        canvas_height=1920,
        home=tmp_path,
    )
    assert plan.spec.enabled is True
    assert plan.enabled is True


def test_config_has_no_enabled_knob() -> None:
    """面板那一侧没有这个开关：`WatermarkConfig` 里根本没有这个字段。

    "这次贴不贴"由**文件在不在**决定，而不是靠一个配置项 —— 少一个旋钮，
    就少一种"配置说贴、文件却没了"的不一致。
    """
    assert "enabled" not in WatermarkConfig.model_fields


# ══════════════════════════════════════════════════════════════════════
# 配置 ⇒ 计划参数
# ══════════════════════════════════════════════════════════════════════


def test_resolve_watermark_uses_the_canvas(tmp_path: Path) -> None:
    resolved = resolve_watermark(_config(), canvas_width=1080, home=tmp_path)
    assert resolved.spec.width_px == 238
    assert resolved.warnings == ()
    assert resolved.spec.image_path.is_absolute()


def test_resolve_watermark_warns_when_clamped(tmp_path: Path) -> None:
    resolved = resolve_watermark(_config(width_ratio=0.25), canvas_width=1078, home=tmp_path)
    assert resolved.spec.width_px == 268
    assert len(resolved.warnings) == 1
    assert "修正" in resolved.warnings[0]


def test_resolve_watermark_keeps_absolute_paths(tmp_path: Path) -> None:
    absolute = tmp_path / "logo.png"
    resolved = resolve_watermark(_config(path=absolute), canvas_width=1080, home=tmp_path)
    assert resolved.spec.image_path == absolute


def test_resolve_watermark_rejects_a_tiny_canvas(tmp_path: Path) -> None:
    with pytest.raises(RenderError) as excinfo:
        resolve_watermark(_config(), canvas_width=4, home=tmp_path)
    assert excinfo.value.code is ErrorCode.RENDER_WATERMARK_INVALID


def test_resolve_watermark_touches_no_file(tmp_path: Path) -> None:
    """解析参数**不碰盘**：水印在不在是 `probe_watermark` 的事（两件事分别会失败）。"""
    resolved = resolve_watermark(_config(), canvas_width=1080, home=tmp_path)
    assert not resolved.spec.image_path.exists()


def test_resolved_spec_is_json_ready(tmp_path: Path) -> None:
    payload = resolve_watermark(_config(), canvas_width=1080, home=tmp_path).spec.to_dict()
    assert payload["position"] == "bottom_right"
    assert payload["margin_px"] == [48, 96]
    assert payload["enabled"] is True
    assert isinstance(payload["image_path"], str)


# ══════════════════════════════════════════════════════════════════════
# PNG 实测（probe 永不抛）
# ══════════════════════════════════════════════════════════════════════


def test_probe_missing_file_reports_instead_of_raising(tmp_path: Path) -> None:
    asset = probe_watermark(tmp_path / "nope.png")
    assert asset.exists is False
    assert asset.usable is False
    assert asset.problem == "文件不存在"
    assert asset.sha256 == ""


@pytest.mark.parametrize("color_type", [4, 6])
def test_probe_reads_dimensions_and_alpha(tmp_path: Path, color_type: int) -> None:
    path = write_png(tmp_path / "wm.png", width=360, height=120, color_type=color_type)
    asset = probe_watermark(path)
    assert (asset.width_px, asset.height_px) == (360, 120)
    assert asset.has_alpha is True
    assert asset.usable is True
    assert len(asset.sha256) == 64


@pytest.mark.parametrize("color_type", [0, 2])
def test_probe_truecolor_without_trns_has_no_alpha(tmp_path: Path, color_type: int) -> None:
    path = write_png(tmp_path / "wm.png", color_type=color_type)
    asset = probe_watermark(path)
    assert asset.exists is True
    assert asset.has_alpha is False
    assert asset.usable is False
    assert asset.problem is not None and "透明通道" in asset.problem


@pytest.mark.parametrize("color_type", [0, 2])
def test_probe_color_key_trns_counts_as_alpha(tmp_path: Path, color_type: int) -> None:
    """``tRNS`` 在真彩/灰度里是透明**色键**，ffmpeg 认它 ⇒ 算有透明。"""
    path = write_png(tmp_path / "wm.png", color_type=color_type, trns=True)
    assert probe_watermark(path).has_alpha is True


def test_probe_palette_needs_trns(tmp_path: Path) -> None:
    """调色板 PNG 没 ``tRNS`` ⇒ 整张图不透明 ⇒ 会盖出一块实心方块。"""
    opaque = probe_watermark(write_png(tmp_path / "a.png", color_type=3))
    assert opaque.has_alpha is False
    transparent = probe_watermark(write_png(tmp_path / "b.png", color_type=3, trns=True))
    assert transparent.has_alpha is True


def test_probe_rejects_a_non_png(tmp_path: Path) -> None:
    path = tmp_path / "fake.png"
    path.write_bytes(b"GIF89a" + b"\x00" * 32)
    asset = probe_watermark(path)
    assert asset.exists is True
    assert asset.usable is False
    assert asset.problem is not None and "不是 PNG" in asset.problem


def test_probe_rejects_a_truncated_ihdr(tmp_path: Path) -> None:
    path = tmp_path / "cut.png"
    path.write_bytes(png_bytes()[:20])
    asset = probe_watermark(path)
    assert asset.usable is False
    assert asset.problem is not None and "截断" in asset.problem


def test_probe_rejects_an_absurd_chunk_length(tmp_path: Path) -> None:
    """块长声明成 4 MB ⇒ 立刻判不可用，不陪着 seek 到底。"""
    path = tmp_path / "huge.png"
    path.write_bytes(PNG_SIGNATURE + struct.pack(">I4s", 4 << 20, b"IHDR"))
    asset = probe_watermark(path)
    assert asset.usable is False
    assert asset.problem is not None and "块长度异常" in asset.problem


def test_probe_rejects_a_zero_sized_png(tmp_path: Path) -> None:
    path = write_png(tmp_path / "zero.png", width=0, height=0)
    asset = probe_watermark(path)
    assert asset.usable is False
    assert asset.problem is not None and "尺寸非法" in asset.problem


def test_probe_reports_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    asset = probe_watermark(path)
    assert asset.usable is False
    assert asset.problem is not None


# ══════════════════════════════════════════════════════════════════════
# 可选装饰：缺了就跳过（**不阻塞出片**）
# ══════════════════════════════════════════════════════════════════════


def test_missing_png_skips_instead_of_blocking(tmp_path: Path) -> None:
    """水印不存在 ⇒ 计划里 ``placement`` 为 ``None`` + 原因写清楚（**不抛**）。"""
    config = _config(path=str(tmp_path / "templates" / "t" / "images" / "watermark.png"))
    plan = plan_watermark(config, canvas_width=1080, canvas_height=1920, home=tmp_path)
    assert plan.enabled is False
    assert plan.placement is None
    assert plan.skipped_reason is not None
    assert "不存在" in plan.skipped_reason


def test_broken_png_skips(tmp_path: Path) -> None:
    path = tmp_path / "watermark.png"
    path.write_bytes(b"not a png")
    plan = plan_watermark(_config(path=str(path)), canvas_width=1080, canvas_height=1920, home=tmp_path)
    assert plan.enabled is False
    assert plan.skipped_reason is not None
    assert "不可用" in plan.skipped_reason


def test_opaque_png_skips(tmp_path: Path) -> None:
    """不带透明通道的 PNG 贴上去是一块实心方块 ⇒ 跳过比贴错强。"""
    path = write_png(tmp_path / "watermark.png", color_type=2)
    plan = plan_watermark(_config(path=str(path)), canvas_width=1080, canvas_height=1920, home=tmp_path)
    assert plan.enabled is False


def test_valid_png_is_placed(tmp_path: Path) -> None:
    path = write_png(tmp_path / "watermark.png", width=400, height=200)
    plan = plan_watermark(_config(path=str(path)), canvas_width=1080, canvas_height=1920, home=tmp_path)
    assert plan.enabled is True
    assert plan.placement is not None
    assert plan.skipped_reason is None
    assert plan.asset.usable is True


def test_plan_never_raises_for_bad_watermarks(tmp_path: Path) -> None:
    """三种坏水印（缺 / 坏 / 不透明）都走同一条出口：跳过。**没有报错分支**。"""
    for name, prepare in (
        ("missing.png", lambda p: None),
        ("broken.png", lambda p: p.write_bytes(b"nope")),
        ("opaque.png", lambda p: write_png(p, color_type=2)),
    ):
        path = tmp_path / name
        prepare(path)
        plan = plan_watermark(_config(path=str(path)), canvas_width=1080, canvas_height=1920, home=tmp_path)
        assert plan.enabled is False, name
        assert plan.skipped_reason, name


def test_plan_dict_is_json_ready(tmp_path: Path) -> None:
    plan = plan_watermark(
        _config(path=str(tmp_path / "nope.png")),
        canvas_width=1080,
        canvas_height=1920,
        home=tmp_path,
    )
    payload = plan.to_dict()
    assert payload["enabled"] is False
    assert payload["placement"] is None
    assert payload["skipped_reason"]
    assert isinstance(payload["asset"], dict)


# ══════════════════════════════════════════════════════════════════════
# 编译期摆放
# ══════════════════════════════════════════════════════════════════════


def test_bottom_right_uses_margins(tmp_path: Path) -> None:
    placement = place_watermark(_spec(), _asset(tmp_path / "wm.png"), canvas_width=1080, canvas_height=1920)
    assert placement is not None
    assert placement.width_px == 238
    assert placement.height_px == 118  # 238 * 100/200 = 119 ⇒ 取偶 118
    assert placement.x == 1080 - 238 - 48
    assert placement.y == 1920 - 118 - 96


def test_top_left_uses_margins_directly(tmp_path: Path) -> None:
    placement = place_watermark(
        _spec(position="top_left"),
        _asset(tmp_path / "wm.png"),
        canvas_width=1080,
        canvas_height=1920,
    )
    assert placement is not None
    assert (placement.x, placement.y) == (48, 96)


@pytest.mark.parametrize("position", SPEC_POSITIONS)
def test_placement_xy_are_always_even(tmp_path: Path, position: str) -> None:
    """§04.2.8.4：``overlay`` 的 x/y 必须为偶数（yuv420p 色度对齐）。

    这条对 ``center`` 尤其要紧：1080-238 = 842，842//2 = 421 是**奇数** ——
    居中时靠取偶把它拉回 420。
    """
    placement = place_watermark(
        _spec(position=position),
        _asset(tmp_path / "wm.png"),
        canvas_width=1080,
        canvas_height=1920,
    )
    assert placement is not None
    assert placement.x % 2 == 0
    assert placement.y % 2 == 0


def test_center_ignores_margins(tmp_path: Path) -> None:
    spec = _spec(position="center", margin_px=(0, 0))
    shifted = _spec(position="center", margin_px=(400, 400))
    asset = _asset(tmp_path / "wm.png")
    first = place_watermark(spec, asset, canvas_width=1080, canvas_height=1920)
    second = place_watermark(shifted, asset, canvas_width=1080, canvas_height=1920)
    assert first == second
    assert first is not None
    assert first.x == 420
    assert first.y == 900


def test_height_follows_the_source_aspect_ratio(tmp_path: Path) -> None:
    """宽高比取自**素材本身**，不是"假设正方形"的估算 —— 长条水印往左占位。"""
    wide = place_watermark(
        _spec(width_px=240),
        _asset(tmp_path / "wide.png", width=800, height=100),
        canvas_width=1080,
        canvas_height=1920,
    )
    assert wide is not None
    assert wide.height_px == 30  # 240 * 100/800 = 30
    assert wide.x == 1080 - 240 - 48


def test_out_of_bounds_returns_none(tmp_path: Path) -> None:
    """放不下 ⇒ ``None``（跳过），而不是抛错把整支片子拦下来。"""
    assert (
        place_watermark(
            _spec(margin_px=(2000, 96)),
            _asset(tmp_path / "wm.png"),
            canvas_width=1080,
            canvas_height=1920,
        )
        is None
    )


def test_out_of_bounds_bottom_margin_returns_none(tmp_path: Path) -> None:
    assert (
        place_watermark(
            _spec(margin_px=(48, 1900)),
            _asset(tmp_path / "wm.png"),
            canvas_width=1080,
            canvas_height=1920,
        )
        is None
    )


def test_tall_watermark_that_does_not_fit_returns_none(tmp_path: Path) -> None:
    """一张竖长图按 238px 宽缩放后高于画布 ⇒ 也是放不下（不能只看宽度）。"""
    assert (
        place_watermark(
            _spec(width_px=238),
            _asset(tmp_path / "tall.png", width=100, height=2000),
            canvas_width=1080,
            canvas_height=1920,
        )
        is None
    )


def test_placement_requires_a_known_size(tmp_path: Path) -> None:
    asset = WatermarkAsset(
        path=tmp_path / "wm.png",
        exists=True,
        width_px=None,
        height_px=None,
        has_alpha=True,
        sha256="",
        problem="不是 PNG",
    )
    assert place_watermark(_spec(), asset, canvas_width=1080, canvas_height=1920) is None


def test_placement_is_json_ready(tmp_path: Path) -> None:
    placement = place_watermark(_spec(), _asset(tmp_path / "wm.png"), canvas_width=1080, canvas_height=1920)
    assert placement is not None
    assert set(placement.to_dict()) == {"x", "y", "width_px", "height_px"}
