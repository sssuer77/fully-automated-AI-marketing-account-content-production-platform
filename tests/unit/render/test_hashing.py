"""合成指纹（T3.7 · §04.2.8.7）。

这一层是**纯函数**测试：不跑 ffmpeg、不碰盘（除了 :func:`file_digest` 那几条临时文件）。

判据只有一句话：**会改变像素的东西必须改变哈希，不会改变像素的东西必须不改变哈希**。
第二句比第一句更容易写错 —— 把输出路径、线程数算进去，哈希就永远不可能命中，而
"缓存从不命中"这件事**不会报错**，只会让人以为省了时间。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from studio.core.config import OutputsConfig
from studio.core.paths import StudioPaths
from studio.render.composite import CompositeRequest
from studio.render.hashing import (
    HASH_LENGTH,
    canonical_plan,
    composite_hash,
    file_digest,
    input_digests,
)
from studio.render.mixdown import MixSettings
from studio.render.profiles import resolve_profile
from studio.render.watermark import plan_watermark

from .conftest import write_png

#: 一份固定的输入指纹（不读盘 —— 这里测的是"指纹怎么进哈希"，不是"文件怎么读"）
_DIGESTS = {"clip": "a" * 64, "voice": "b" * 64}


def _request(outputs: OutputsConfig, tmp_path: Path, **overrides: object) -> CompositeRequest:
    base: dict[str, object] = {
        "profile": resolve_profile(outputs),
        "clip": tmp_path / "clip.mp4",
        "voice": tmp_path / "voice.wav",
        "output": tmp_path / "out.mp4",
        "duration_ms": 12_345,
    }
    base.update(overrides)
    return CompositeRequest(**base)  # type: ignore[arg-type]


def _hash(request: CompositeRequest, digests: dict[str, str] | None = None) -> str:
    return composite_hash(request, digests=_DIGESTS if digests is None else digests)


# ══════════════════════════════════════════════════════════════════════
# 形状
# ══════════════════════════════════════════════════════════════════════


def test_hash_is_a_short_hex_string(outputs: OutputsConfig, tmp_path: Path) -> None:
    """32 位 hex：短到能印在 manifest 里肉眼比对，长到碰撞概率在噪声以下。"""
    digest = _hash(_request(outputs, tmp_path))
    assert len(digest) == HASH_LENGTH == 32
    assert all(char in "0123456789abcdef" for char in digest)


def test_same_inputs_give_the_same_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    """同一次请求算两遍必须一样 —— 否则"缓存命中"就成了掷骰子。"""
    request = _request(outputs, tmp_path)
    assert _hash(request) == _hash(request)


# ══════════════════════════════════════════════════════════════════════
# 不改变像素的东西 ⇒ 哈希必须不变
# ══════════════════════════════════════════════════════════════════════


def test_output_path_does_not_change_the_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    """★ 输出文件名带时间戳，每次都不一样 —— 算进去就等于**永远不命中**。"""
    request = _request(outputs, tmp_path)
    moved = replace(request, output=tmp_path / "another-name.mp4")
    assert _hash(request) == _hash(moved)


def test_thread_count_does_not_change_the_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    """``-threads`` 只影响编码快慢，不影响画面。"""
    request = _request(outputs, tmp_path)
    assert _hash(request) == _hash(replace(request, threads=8))


def test_graph_path_does_not_change_the_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    """滤镜图落盘路径是排障用的，与成片内容无关。"""
    request = _request(outputs, tmp_path)
    assert _hash(request) == _hash(replace(request, graph_path=tmp_path / "g.txt"))


# ══════════════════════════════════════════════════════════════════════
# 改变像素的东西 ⇒ 哈希必须变
# ══════════════════════════════════════════════════════════════════════


def test_duration_changes_the_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    request = _request(outputs, tmp_path)
    assert _hash(request) != _hash(replace(request, duration_ms=99_999))


def test_changed_input_content_changes_the_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    """★ 路径没变、内容换了（重录一遍配音）必须换哈希 —— 只哈希路径会放它过去。"""
    request = _request(outputs, tmp_path)
    other = dict(_DIGESTS, voice="c" * 64)
    assert _hash(request) != _hash(request, other)


def test_black_fill_changes_the_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    """黑屏降级换了画面来源 ⇒ 是另一支片子（不能复用有底片那支的产物）。"""
    request = _request(outputs, tmp_path)
    black = replace(request, clip=None)
    assert _hash(black, {"voice": "b" * 64}) != _hash(request)


def test_mix_settings_change_the_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    """BGM 增益 / ducking 参数变了，音轨就变了。"""
    request = _request(outputs, tmp_path)
    louder = replace(request, mix=MixSettings(bgm_gain_db=-12.0))
    assert _hash(request) != _hash(louder)


def test_encoder_args_change_the_hash(outputs: OutputsConfig, tmp_path: Path) -> None:
    """★ 哈希的是 ``output_args()`` 而不是档位名：改一个 CRF 而不改名，画面就变了。"""
    request = _request(outputs, tmp_path)
    tweaked = replace(
        request,
        profile=replace(request.profile, quality=request.profile.quality + 4),
    )
    assert _hash(request) != _hash(tweaked)


def test_watermark_placement_changes_the_hash(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path, tmp_path: Path
) -> None:
    """水印换个角贴 ⇒ 像素不同。"""
    write_png(watermark_path, width=400, height=200)
    plan = plan_watermark(outputs.watermark, canvas_width=1080, canvas_height=1920, home=outputs_home.home)
    assert plan.placement is not None
    request = _request(outputs, tmp_path, watermark=plan)
    assert "watermark" in canonical_plan(request)
    assert canonical_plan(request)["watermark"] is not None
    assert canonical_plan(_request(outputs, tmp_path))["watermark"] is None


def test_missing_watermark_is_not_hashed(
    outputs: OutputsConfig, outputs_home: StudioPaths, watermark_path: Path, tmp_path: Path
) -> None:
    """水印文件不在 ⇒ 不贴 ⇒ 它的"身份"是 ``None``，不是某个路径。"""
    plan = plan_watermark(outputs.watermark, canvas_width=1080, canvas_height=1920, home=outputs_home.home)
    assert plan.placement is None
    request = _request(outputs, tmp_path, watermark=plan)
    assert canonical_plan(request)["watermark"] is None


# ══════════════════════════════════════════════════════════════════════
# 文件指纹
# ══════════════════════════════════════════════════════════════════════


def test_file_digest_reads_content(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"hello")
    first = file_digest(target)
    assert len(first) == 64
    target.write_bytes(b"hello!")
    assert file_digest(target) != first


def test_file_digest_of_a_missing_file_is_a_sentinel_not_an_error(tmp_path: Path) -> None:
    """★ 指纹是**留痕与优化**用的，不是门禁 —— 为它让整支片子渲不出来是本末倒置。"""
    assert file_digest(tmp_path / "nope.bin") == "missing"


def test_input_digests_skips_the_black_fill(outputs: OutputsConfig, tmp_path: Path) -> None:
    """纯黑底没有文件 —— 它的身份由 ``video_source='black'`` 表达，不该编一个假路径。"""
    black = _request(outputs, tmp_path, clip=None)
    assert "clip" not in input_digests(black)
    assert "voice" in input_digests(black)
