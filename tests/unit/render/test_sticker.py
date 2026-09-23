"""人物贴图：参数模型 + 资产实测 + 编译期摆放（T6.5 · §04.2.8.2）。

验收四条（`todolist.md` T6.5）：**位置枚举 / 边距偶数 / 高度占比上限（可顶满画面）/ 透明度范围**。
外加三条这一层特有的口径：

1. **按高度定尺寸**，不是水印那条宽度占比 —— 1080 宽下按宽算只有 270px，做不了主体；
2. **每层各自判断**：关掉的 / 图不在的 / 放不下的只跳过**这一层**，其余层照常；
3. **声明顺序 = 叠放顺序**（先声明的在下面），这条由 `plan_stickers` 的返回顺序保证。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast, get_args, get_type_hints

import pytest
from pydantic import ValidationError

from studio.core.config import StickerConfig, WatermarkPosition
from studio.core.errors import RenderError
from studio.render.speech import SpeakingPlan
from studio.render.sticker import (
    StickerAsset,
    StickerPlan,
    StickerSpec,
    place_sticker,
    plan_stickers,
    resolve_sticker,
)

from .conftest import write_png

# ── 夹具 ──────────────────────────────────────────────────────────────

SPEC_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right", "center")


def _config(**overrides: object) -> StickerConfig:
    """一份合法的贴图配置（默认值照 `config/outputs.yaml` 的 `hero` 那一层）。"""
    data: dict[str, object] = {
        "enabled": True,
        "path": "templates/t/assets/images/stickers/hero.png",
        "position": "bottom_right",
        "margin_x": 48,
        "margin_y": 420,
        "height_ratio": 0.45,
        "opacity": 1.0,
    }
    data.update(overrides)
    return StickerConfig.model_validate(data)


def _asset(path: Path, *, width: int = 600, height: int = 1200) -> StickerAsset:
    """一张"量出来"的贴图资产：600x1200 = 1:2 的竖长人物。"""
    return StickerAsset(
        path=path,
        exists=True,
        width_px=width,
        height_px=height,
        has_alpha=True,
        sha256="0" * 64,
    )


def _spec(**overrides: object) -> StickerSpec:
    data: dict[str, object] = {
        "name": "hero",
        "image_path": Path("hero.png"),
        "position": "bottom_right",
        "margin_px": (48, 420),
        "height_px": 864,
        "opacity": 1.0,
    }
    data.update(overrides)
    return StickerSpec.model_validate(data)


# ══════════════════════════════════════════════════════════════════════
# ① 位置枚举
# ══════════════════════════════════════════════════════════════════════


def test_position_enum_is_the_same_one_the_watermark_uses() -> None:
    """贴图与水域**共用**同一个位置枚举（不是抄第二份）—— 面板的下拉框只有一个来源。"""
    assert get_args(WatermarkPosition) == SPEC_POSITIONS
    assert get_type_hints(StickerConfig)["position"] is WatermarkPosition


@pytest.mark.parametrize("position", SPEC_POSITIONS)
def test_every_position_is_accepted(position: str) -> None:
    assert _config(position=position).position == position


def test_unknown_position_rejected() -> None:
    with pytest.raises(ValidationError):
        _config(position="middle")


# ══════════════════════════════════════════════════════════════════════
# ② 边距必须偶数
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(("margin_x", "margin_y"), [(49, 420), (48, 421), (1, 1)])
def test_odd_margin_rejected_by_config(margin_x: int, margin_y: int) -> None:
    with pytest.raises(ValidationError):
        _config(margin_x=margin_x, margin_y=margin_y)


@pytest.mark.parametrize(("margin_x", "margin_y"), [(49, 420), (48, 421)])
def test_odd_margin_rejected_by_spec(margin_x: int, margin_y: int) -> None:
    """两道防线：面板写不进去（config），手搓的计划也构造不出来（spec）。"""
    with pytest.raises(ValidationError):
        _spec(margin_px=(margin_x, margin_y))


def test_negative_margin_rejected() -> None:
    with pytest.raises(ValidationError):
        _spec(margin_px=(-2, 0))
    with pytest.raises(ValidationError):
        _config(margin_x=-2)


# ══════════════════════════════════════════════════════════════════════
# ③ 按高度定尺寸（上限 = 顶满画面）
# ══════════════════════════════════════════════════════════════════════


def test_sticker_has_no_width_ratio() -> None:
    """★ 贴图**没有** `width_ratio` —— 它就是和水印最大的那处区别。

    真机验证过：人物按 1080 × 0.25 只有 270px 宽，做不了主体。谁把水印那套参数
    合并回来，这条就会红。
    """
    assert "width_ratio" not in StickerConfig.model_fields
    assert "height_ratio" in StickerConfig.model_fields


def test_defaults_follow_the_contract() -> None:
    config = _config()
    assert config.enabled is True
    assert config.position == "bottom_right"
    assert (config.margin_x, config.margin_y) == (48, 420)
    assert config.height_ratio == 0.45
    assert config.opacity == 1.0


def test_a_layer_is_off_by_default() -> None:
    """默认关：往文件里加一段 `stickers` 不该改变现有成片。"""
    assert StickerConfig(path=Path("hero.png")).enabled is False


@pytest.mark.parametrize("ratio", [0.0, -0.1, 1.01])
def test_height_ratio_out_of_range_rejected(ratio: float) -> None:
    with pytest.raises(ValidationError):
        _config(height_ratio=ratio)


def test_full_canvas_height_is_allowed() -> None:
    """上限 1.0 = 人物可以顶满画面（与水域那条 1/4 宽的上限不是一回事）。"""
    assert _config(height_ratio=1.0).height_px_for(1920) == 1920


def test_height_px_follows_the_default_profile() -> None:
    config = _config()
    assert config.height_px_for(1920) == 864
    assert config.height_px_for(1280) == 576


def test_height_px_is_always_even() -> None:
    """取偶：`overlay` 的 y = H - h - margin_y，H 与 margin 都已偶数 ⇒ 只有 h 偶才能保证 y 偶。"""
    assert _config(height_ratio=0.5).height_px_for(1922) == 960  # 961 ⇒ 960
    assert _config(height_ratio=1.0).height_px_for(1921) == 1920  # 越界 1px ⇒ 拉回画布内


# ══════════════════════════════════════════════════════════════════════
# ④ 透明度范围
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("opacity", [0.0, 0.5, 1.0])
def test_opacity_range_accepted(opacity: float) -> None:
    assert _config(opacity=opacity).opacity == opacity


@pytest.mark.parametrize("opacity", [-0.1, 1.1])
def test_opacity_out_of_range_rejected(opacity: float) -> None:
    with pytest.raises(ValidationError):
        _config(opacity=opacity)


# ══════════════════════════════════════════════════════════════════════
# 编译期参数（配置 + 一块具体画布）
# ══════════════════════════════════════════════════════════════════════


def test_relative_path_is_resolved_against_home(tmp_path: Path) -> None:
    resolved = resolve_sticker(_config(), name="hero", canvas_height=1920, home=tmp_path)
    assert resolved.spec.image_path == tmp_path / "templates/t/assets/images/stickers/hero.png"
    assert resolved.spec.name == "hero"


def test_absolute_path_is_kept(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "hero.png"
    resolved = resolve_sticker(_config(path=str(absolute)), name="hero", canvas_height=1920, home=tmp_path)
    assert resolved.spec.image_path == absolute


def test_a_corrected_height_is_reported_as_a_warning(tmp_path: Path) -> None:
    """比例算出来的高度被改过（取偶 / 拉回画布内）⇒ 说一句，不闷着改。"""
    resolved = resolve_sticker(_config(height_ratio=0.5), name="hero", canvas_height=1922, home=tmp_path)
    assert resolved.spec.height_px == 960
    assert resolved.warnings and "修正" in resolved.warnings[0]


def test_no_warning_when_the_ratio_lands_exactly(tmp_path: Path) -> None:
    resolved = resolve_sticker(_config(), name="hero", canvas_height=1920, home=tmp_path)
    assert resolved.warnings == ()


def test_tiny_canvas_is_a_render_error(tmp_path: Path) -> None:
    """画布高小到放不下任何东西 ⇒ 抛错（这是**配置错了**，不是"跳过一层"）。"""
    with pytest.raises(RenderError) as excinfo:
        resolve_sticker(_config(), name="hero", canvas_height=4, home=tmp_path)
    assert excinfo.value.context["sticker"] == "hero"


# ══════════════════════════════════════════════════════════════════════
# 摆放
# ══════════════════════════════════════════════════════════════════════


def test_bottom_right_uses_margins_and_the_source_aspect_ratio(tmp_path: Path) -> None:
    placement = place_sticker(_spec(), _asset(tmp_path / "hero.png"), canvas_width=1080, canvas_height=1920)
    assert placement is not None
    assert placement.height_px == 864
    assert placement.width_px == 432  # 864 * 600/1200
    assert (placement.x, placement.y) == (1080 - 432 - 48, 1920 - 864 - 420)


def test_top_left_uses_margins_directly(tmp_path: Path) -> None:
    placement = place_sticker(
        _spec(position="top_left"), _asset(tmp_path / "hero.png"), canvas_width=1080, canvas_height=1920
    )
    assert placement is not None
    assert (placement.x, placement.y) == (48, 420)


def test_center_ignores_margins(tmp_path: Path) -> None:
    asset = _asset(tmp_path / "hero.png")
    first = place_sticker(
        _spec(position="center", margin_px=(0, 0)), asset, canvas_width=1080, canvas_height=1920
    )
    second = place_sticker(
        _spec(position="center", margin_px=(400, 400)), asset, canvas_width=1080, canvas_height=1920
    )
    assert first == second
    assert first is not None
    assert (first.x, first.y) == (324, 528)


@pytest.mark.parametrize("position", SPEC_POSITIONS)
def test_placement_is_always_even(tmp_path: Path, position: str) -> None:
    """§04.2.8.4：`overlay` 的 x/y 必须为偶数（yuv420p 色度对齐）。"""
    placement = place_sticker(
        _spec(position=position), _asset(tmp_path / "hero.png"), canvas_width=1080, canvas_height=1920
    )
    assert placement is not None
    assert placement.x % 2 == 0
    assert placement.y % 2 == 0
    assert placement.width_px % 2 == 0
    assert placement.height_px % 2 == 0


def test_width_is_derived_from_the_source_and_then_evened(tmp_path: Path) -> None:
    """宽度由素材宽高比推出来（不是"假设人物是正方形"），推出来是奇数就取偶。"""
    placement = place_sticker(
        _spec(), _asset(tmp_path / "hero.png", width=601), canvas_width=1080, canvas_height=1920
    )
    assert placement is not None
    assert placement.width_px == 432  # 864 * 601/1200 = 432.72 ⇒ round 433 ⇒ 取偶 432


def test_a_wide_sticker_that_does_not_fit_returns_none(tmp_path: Path) -> None:
    """横长素材按高度放大后比画布还宽 ⇒ 放不下（只看高度是看不出来的）。"""
    assert (
        place_sticker(
            _spec(), _asset(tmp_path / "wide.png", width=3000), canvas_width=1080, canvas_height=1920
        )
        is None
    )


def test_out_of_bounds_returns_none(tmp_path: Path) -> None:
    """放不下 ⇒ `None`（跳过这一层），而不是抛错把整支片子拦下来。"""
    asset = _asset(tmp_path / "hero.png")
    assert place_sticker(_spec(margin_px=(2000, 420)), asset, canvas_width=1080, canvas_height=1920) is None
    assert place_sticker(_spec(margin_px=(48, 1900)), asset, canvas_width=1080, canvas_height=1920) is None
    assert place_sticker(_spec(height_px=1920), asset, canvas_width=1080, canvas_height=1920) is None


def test_placement_requires_a_known_size(tmp_path: Path) -> None:
    asset = StickerAsset(
        path=tmp_path / "hero.png",
        exists=True,
        width_px=None,
        height_px=None,
        has_alpha=True,
        sha256="",
        problem="不是 PNG",
    )
    assert place_sticker(_spec(), asset, canvas_width=1080, canvas_height=1920) is None


def test_placement_is_json_ready(tmp_path: Path) -> None:
    placement = place_sticker(_spec(), _asset(tmp_path / "hero.png"), canvas_width=1080, canvas_height=1920)
    assert placement is not None
    assert set(placement.to_dict()) == {"x", "y", "width_px", "height_px"}


# ══════════════════════════════════════════════════════════════════════
# 多层：每层各自判断 + 声明顺序 = 叠放顺序
# ══════════════════════════════════════════════════════════════════════


def _write_layers(home: Path, **layers: object) -> dict[str, StickerConfig]:
    """把几层贴图写到临时家目录里（每层的图都造出来），返回配置。

    图一律是 **600x1200 的竖长人物**：横长图按高度放大后比画布还宽，会走"放不下"那条分支，
    而这些用例要验的是"贴上了"。
    """
    configs: dict[str, StickerConfig] = {}
    for name, overrides in layers.items():
        relative = f"templates/t/assets/images/stickers/{name}.png"
        write_png(home / relative, width=600, height=1200)
        configs[name] = _config(path=relative, **overrides)  # type: ignore[arg-type]
    return configs


def test_empty_section_plans_nothing(tmp_path: Path) -> None:
    assert plan_stickers({}, canvas_width=1080, canvas_height=1920, home=tmp_path) == ()


def test_declaration_order_is_the_stacking_order(tmp_path: Path) -> None:
    """先声明的在**下面** —— 顺序只由 YAML 的书写顺序决定，代码里不排序。"""
    configs = _write_layers(tmp_path, hero={}, guest={"position": "bottom_left"})
    plans = plan_stickers(configs, canvas_width=1080, canvas_height=1920, home=tmp_path)
    assert tuple(plan.spec.name for plan in plans) == ("hero", "guest")
    assert all(plan.applied for plan in plans)


def test_a_disabled_layer_is_skipped_but_still_reported(tmp_path: Path) -> None:
    """关掉的层**也要出现在结论里** —— 面板要能回答"第 2 层为什么没贴上"。"""
    configs = _write_layers(tmp_path, hero={}, guest={"enabled": False})
    plans = plan_stickers(configs, canvas_width=1080, canvas_height=1920, home=tmp_path)
    assert [plan.applied for plan in plans] == [True, False]
    assert plans[1].placement is None
    assert "关" in (plans[1].skipped_reason or "")
    assert plans[0].skipped_reason is None


def test_one_missing_file_does_not_take_down_the_other_layer(tmp_path: Path) -> None:
    """★ 每层各自判断：guest 的图不在 ⇒ 只跳过 guest，hero 照贴（成片照出）。"""
    configs = _write_layers(tmp_path, hero={})
    configs["guest"] = _config(path="templates/t/assets/images/stickers/nope.png")
    plans = plan_stickers(configs, canvas_width=1080, canvas_height=1920, home=tmp_path)
    assert [plan.applied for plan in plans] == [True, False]
    assert "不存在" in (plans[1].skipped_reason or "")


def test_a_png_without_alpha_is_not_usable(tmp_path: Path) -> None:
    """没有透明通道的 PNG 会盖住一块实心画面 ⇒ 判不可用（只跳过这一层）。"""
    relative = "templates/t/assets/images/stickers/opaque.png"
    write_png(tmp_path / relative, color_type=2)
    plans = plan_stickers(
        {"hero": _config(path=relative)}, canvas_width=1080, canvas_height=1920, home=tmp_path
    )
    assert plans[0].applied is False
    assert "透明" in (plans[0].skipped_reason or "")


def test_a_layer_that_does_not_fit_is_skipped_with_the_numbers(tmp_path: Path) -> None:
    """放不下 ⇒ 跳过，且原因里带上**具体数字**（"差多少"必须能看出来）。"""
    configs = _write_layers(tmp_path, hero={"margin_y": 1900})
    plans = plan_stickers(configs, canvas_width=1080, canvas_height=1920, home=tmp_path)
    assert plans[0].applied is False
    reason = plans[0].skipped_reason or ""
    assert "放不进画布" in reason
    assert "1080x1920" in reason


def test_plan_is_json_ready(tmp_path: Path) -> None:
    configs = _write_layers(tmp_path, hero={})
    plan = plan_stickers(configs, canvas_width=1080, canvas_height=1920, home=tmp_path)[0]
    payload = plan.to_dict()
    assert set(payload) == {
        "name",
        "enabled",
        "asset",
        "placement",
        "skipped_reason",
        "warnings",
        "speaking",
    }
    assert payload["name"] == "hero"
    assert payload["skipped_reason"] is None
    assert isinstance(payload["placement"], dict)


def test_plan_to_dict_reports_a_skipped_layer(tmp_path: Path) -> None:
    configs = _write_layers(tmp_path, hero={"enabled": False})
    plan = plan_stickers(configs, canvas_width=1080, canvas_height=1920, home=tmp_path)[0]
    payload = plan.to_dict()
    assert payload["enabled"] is False
    assert payload["placement"] is None
    assert payload["skipped_reason"]
    assert isinstance(payload["asset"], dict)


# ══════════════════════════════════════════════════════════════════════
# ③ 换图：讲话时换成另一张（T6.5 追加 · 裁定 389/390/391）
# ══════════════════════════════════════════════════════════════════════


def _two_images(home: Path, *, speaking_size: tuple[int, int] = (600, 1200)) -> tuple[str, str]:
    """造出这一层的**普通图 + 讲话图**（600x1200 的竖长人物），返回两个相对路径。

    两张图的尺寸可以分开指定 —— "宽高比不一致"那条 warning 就是靠它造的。
    """
    normal = "templates/t/assets/images/stickers/hero.png"
    speaking = "templates/t/assets/images/stickers/hero_speaking.png"
    write_png(home / normal, width=600, height=1200)
    write_png(home / speaking, width=speaking_size[0], height=speaking_size[1])
    return normal, speaking


def _talking(who: str = "bigbear", spans: tuple[tuple[int, int], ...] = ((0, 1000),)) -> SpeakingPlan:
    """一份"谁在什么时候讲话"（只有一个人）。"""
    return SpeakingPlan(by_speaker={who: spans}, source="timeline")


def _one(
    home: Path,
    configs: dict[str, StickerConfig],
    speaking: SpeakingPlan | None = None,
) -> StickerPlan:
    plans = plan_stickers(configs, canvas_width=1080, canvas_height=1920, home=home, speaking=speaking)
    return plans[0]


def test_a_layer_without_a_speaking_image_says_nothing_about_swapping(tmp_path: Path) -> None:
    """没配讲话图 ⇒ 不换图，而且**一个字都不说** —— 不换图是默认状态。"""
    configs = _write_layers(tmp_path, hero={})
    plan = _one(tmp_path, configs, _talking())
    assert plan.applied is True
    assert plan.swaps is False
    assert plan.speaking_problem is None
    assert plan.speaking_intervals == ()


def test_swaps_when_the_image_is_usable_and_the_speaker_has_lines(tmp_path: Path) -> None:
    normal, speaking = _two_images(tmp_path)
    configs = {"hero": _config(path=normal, speaker="bigbear", speaking_path=speaking)}
    plan = _one(tmp_path, configs, _talking("bigbear", ((0, 1000), (2000, 3000))))
    assert plan.applied is True
    assert plan.swaps is True
    assert plan.speaking_intervals == ((0, 1000), (2000, 3000))
    assert plan.speaking_problem is None


def test_the_speaker_must_match_the_one_in_the_script(tmp_path: Path) -> None:
    """名字对不上 ⇒ 不换图，并说清"这条片子里他没有讲话区间"（不是"没配"）。"""
    normal, speaking = _two_images(tmp_path)
    configs = {"hero": _config(path=normal, speaker="littlebear", speaking_path=speaking)}
    plan = _one(tmp_path, configs, _talking("bigbear"))
    assert plan.swaps is False
    assert "littlebear" in (plan.speaking_problem or "")
    assert "没有讲话区间" in (plan.speaking_problem or "")


def test_the_two_images_share_one_placement(tmp_path: Path) -> None:
    """★ 换图**不挪位置**：摆放只由普通图算，讲话图那份宽高比不参与。

    两张图尺寸故意不一样（600x1000 的讲话图）—— 摆放要是跟着讲话图算，宽度会是 518，
    人物就会在开口那一瞬间跳一下。
    """
    normal, speaking = _two_images(tmp_path, speaking_size=(600, 1000))
    configs = {"hero": _config(path=normal, speaker="bigbear", speaking_path=speaking)}
    plan = _one(tmp_path, configs, _talking())
    assert plan.placement is not None
    assert plan.placement.width_px == 432, "864 高 * 600/1200 的普通图比例"
    assert plan.placement.height_px == 864


def test_a_different_aspect_ratio_is_a_warning_not_a_block(tmp_path: Path) -> None:
    """两张图宽高比不一致 ⇒ 记账（会被压扁），但**照常换**（这是画风问题，不是坏图）。"""
    normal, speaking = _two_images(tmp_path, speaking_size=(600, 1000))
    configs = {"hero": _config(path=normal, speaker="bigbear", speaking_path=speaking)}
    plan = _one(tmp_path, configs, _talking())
    assert plan.swaps is True
    assert any("宽高比" in item for item in plan.warnings)


def test_the_same_aspect_ratio_is_not_a_warning(tmp_path: Path) -> None:
    normal, speaking = _two_images(tmp_path)
    configs = {"hero": _config(path=normal, speaker="bigbear", speaking_path=speaking)}
    assert _one(tmp_path, configs, _talking()).warnings == ()


def test_a_broken_speaking_image_does_not_take_down_the_normal_layer(tmp_path: Path) -> None:
    """讲话图不在盘上 ⇒ 这一层**照样贴普通图**，只是不换（装饰的两条腿各自站着）。"""
    normal, _ = _two_images(tmp_path)
    configs = {
        "hero": _config(
            path=normal,
            speaker="bigbear",
            speaking_path="templates/t/assets/images/stickers/nope_speaking.png",
        )
    }
    plan = _one(tmp_path, configs, _talking())
    assert plan.applied is True
    assert plan.swaps is False
    assert "不在盘上" in (plan.speaking_problem or "")


def test_a_speaking_image_without_alpha_is_not_usable(tmp_path: Path) -> None:
    normal, speaking = _two_images(tmp_path)
    write_png(tmp_path / speaking, color_type=2)  # 真彩、无 alpha
    configs = {"hero": _config(path=normal, speaker="bigbear", speaking_path=speaking)}
    plan = _one(tmp_path, configs, _talking())
    assert plan.applied is True
    assert plan.swaps is False
    assert "透明" in (plan.speaking_problem or "")


@pytest.mark.parametrize(
    ("speaker", "speaking_path", "expected"),
    [
        ("bigbear", None, "没填讲话图"),
        ("", "templates/t/assets/images/stickers/hero_speaking.png", "没写这一层代表谁"),
    ],
)
def test_every_way_the_config_can_be_incomplete_has_a_reason(
    tmp_path: Path, speaker: str, speaking_path: str | None, expected: str
) -> None:
    """★ 配了却换不成 ⇒ 必有一条理由（上一轮"开了没反应、面板不说"就是这么来的）。"""
    normal, _ = _two_images(tmp_path)
    configs = {"hero": _config(path=normal, speaker=speaker, speaking_path=speaking_path)}
    plan = _one(tmp_path, configs, _talking())
    assert plan.swaps is False
    assert expected in (plan.speaking_problem or "")


def test_a_missing_speaking_plan_says_it_does_not_know(tmp_path: Path) -> None:
    """★ ``speaking=None``（调用方没读稿子）与"这个人没词"是**两件事**。

    混成一句的话，一条没带稿子的调用（面板首屏就是这么调的）会写下"这个人没讲话"
    这种**它并不知道**的结论。
    """
    normal, speaking = _two_images(tmp_path)
    configs = {"hero": _config(path=normal, speaker="bigbear", speaking_path=speaking)}
    plan = _one(tmp_path, configs, None)
    assert plan.swaps is False
    assert "这次没有拿到讲话区间" in (plan.speaking_problem or "")
    assert "他没有讲话区间" not in (plan.speaking_problem or "")


def test_the_source_and_the_note_are_carried_into_the_plan(tmp_path: Path) -> None:
    """区间是从哪份东西读出来的，必须一路带到 manifest（"它按什么判断的"）。"""
    normal, speaking = _two_images(tmp_path)
    configs = {"hero": _config(path=normal, speaker="bigbear", speaking_path=speaking)}
    plan = _one(tmp_path, configs, _talking())
    assert plan.speaking_source == "timeline"
    assert plan.speaking_note is None


def test_the_manifest_block_reports_the_swap(tmp_path: Path) -> None:
    normal, speaking = _two_images(tmp_path)
    configs = {"hero": _config(path=normal, speaker="bigbear", speaking_path=speaking)}
    # `to_dict()` 的类型是 `dict[str, object]`（manifest 那一层的诚实类型，块里本来就
    # 深浅不一）。这里断言的是"块里有什么"，就地收窄一次即可 —— 为了好断言把源类型放宽
    # 成 `Any`，等于把这条契约从类型上抹掉。
    block = cast("dict[str, Any]", _one(tmp_path, configs, _talking()).to_dict()["speaking"])
    assert set(block) == {
        "speaker",
        "path",
        "asset",
        "intervals",
        "swapped",
        "problem",
        "source",
        "note",
    }
    assert block["speaker"] == "bigbear"
    assert block["swapped"] is True
    assert block["intervals"] == [[0, 1000]]
    assert isinstance(block["asset"], dict)


def test_the_speaking_path_is_resolved_against_home(tmp_path: Path) -> None:
    """相对路径按 ``STUDIO_HOME`` 解析 —— 与普通图同一条规则。"""
    normal, speaking = _two_images(tmp_path)
    resolved = resolve_sticker(
        _config(path=normal, speaker="bigbear", speaking_path=speaking),
        name="hero",
        canvas_height=1920,
        home=tmp_path,
    )
    assert resolved.spec.speaking_path == tmp_path / speaking


def test_an_absolute_speaking_path_is_kept(tmp_path: Path) -> None:
    normal, speaking = _two_images(tmp_path)
    absolute = (tmp_path / speaking).resolve()
    resolved = resolve_sticker(
        _config(path=normal, speaker="bigbear", speaking_path=str(absolute)),
        name="hero",
        canvas_height=1920,
        home=tmp_path,
    )
    assert resolved.spec.speaking_path == absolute
