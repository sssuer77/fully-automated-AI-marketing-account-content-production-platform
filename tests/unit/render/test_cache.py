"""整片级缓存的判据（T3.4 · §04.2.8.7）。

为什么这一层要单独测
--------------------
``render/cache.py`` 是**唯一**一处"可以不跑 ffmpeg 就交出片子"的判断。它错一格的后果不是
慢几秒，而是**交出一支不是这次要渲的片子**（下游只看"文件在不在"）。所以用例围着"什么
情况下必须判不命中"写：哈希不同、产物被截断、manifest 缺字段、字段类型不对。

命名约定：``..._is_a_miss`` = 期望重渲（返回 ``None``）；其余 = 期望命中。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from studio.render.cache import CacheHit, reusable_output

#: 与 ``hashing.HASH_LENGTH`` 同长的一串假指纹（内容不重要，相等/不等才有意义）
HASH = "a" * 32
OTHER_HASH = "b" * 32


def _artifact(tmp_path: Path, *, size: int = 2048) -> Path:
    """一支"已经渲好"的成片（内容随便，字节数要能与 manifest 对上）。"""
    target = tmp_path / "output" / "videos" / "20260917-120000_t1_final.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"m" * size)
    return target


def _payload(final: Path, *, size: int | None = None, **overrides: Any) -> dict[str, object]:
    """一份 manifest 的载荷。

    :param size: 那一轮**记下的**字节数。默认取成片当前的大小；要测"记下的与盘上的
        对不上"（截断 / 被换掉）时才显式传 —— 真实场景里 manifest 记的是**当时**的大小，
        所以夹具也必须能在成片被改坏**之后**再造出一份"当时那份"的 manifest。
    """
    payload: dict[str, object] = {
        "task_id": "t1",
        "created_at": "2026-09-17T12:00:00+08:00",
        "profile": "douyin_1080x1920_30fps_v1",
        "final": final.as_posix(),
        "duration_ms": 17470,
        "size_bytes": final.stat().st_size if size is None else size,
        "clip": None,
        "bg_fill": "black",
        "bgm": None,
        "watermark": {"enabled": False, "skipped_reason": "水印图不在"},
        "subtitle": {"enabled": False, "ass": None},
        "loudness": {
            "input_i": -22.4,
            "input_tp": -3.1,
            "input_lra": 2.0,
            "input_thresh": -32.0,
            "target_offset": 0.2,
        },
        "output_loudness": {
            "input_i": -16.48,
            "input_tp": -1.12,
            "input_lra": 1.8,
            "input_thresh": -26.5,
            "target_offset": 0.1,
        },
        "composite_hash": HASH,
        "degraded": False,
        "degrade_reason": None,
        "attempts": ["douyin_1080x1920_30fps_v1"],
        "warnings": ["没有跑酷底片，这次用纯黑底出片（黑屏降级）"],
        "argv": ["ffmpeg", "-i", "voice_master.wav", "-filter_complex", "..."],
    }
    payload.update(overrides)
    return payload


def _manifest(tmp_path: Path, payload: dict[str, object] | None = None, **overrides: Any) -> Path:
    target = tmp_path / "work" / "t1" / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        payload = _payload(_artifact(tmp_path), **overrides)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def _hit(tmp_path: Path, *, plan_hash: str = HASH, **overrides: Any) -> CacheHit | None:
    return reusable_output(manifest=_manifest(tmp_path, **overrides), plan_hash=plan_hash)


def _rig(tmp_path: Path, **overrides: Any) -> tuple[Path, Path]:
    """造一支成片 + 一份**对得上**的 manifest；返回 ``(成片, manifest)``。

    要测"成片后来被动了"的用例走这条路：先让两份文件是自洽的，再去破坏其中一份。
    """
    artifact = _artifact(tmp_path)
    return artifact, _manifest(tmp_path, _payload(artifact, **overrides))


# ══════════════════════════════════════════════════════════════════════
# 命中
# ══════════════════════════════════════════════════════════════════════


def test_a_matching_manifest_reuses_the_artifact(tmp_path: Path) -> None:
    """哈希一致 + 产物在盘上 + 字段齐全 ⇒ 命中，且每一样都从 manifest 里读回来。"""
    artifact = _artifact(tmp_path)
    hit = _hit(tmp_path)

    assert hit is not None
    assert hit.output == artifact
    assert hit.plan_hash == HASH
    assert hit.profile_name == "douyin_1080x1920_30fps_v1"
    assert hit.degraded is False
    assert hit.degrade_reason is None
    assert hit.attempts == ("douyin_1080x1920_30fps_v1",)
    assert hit.rendered_at == "2026-09-17T12:00:00+08:00"
    assert hit.warnings == ("没有跑酷底片，这次用纯黑底出片（黑屏降级）",)


def test_the_composite_result_is_rebuilt_from_the_manifest(tmp_path: Path) -> None:
    """``CompositeResult`` 的每一个字段都来自 manifest（不推断、不填默认值）。"""
    hit = _hit(tmp_path)

    assert hit is not None
    composite = hit.composite
    assert composite.size_bytes == hit.output.stat().st_size
    assert composite.duration_ms == 17470
    assert composite.bg_fill == "black"
    assert composite.watermark_applied is False
    assert composite.bgm_applied is False  # `bgm` 是 null
    assert composite.subtitle_applied is False
    assert composite.argv == ("ffmpeg", "-i", "voice_master.wav", "-filter_complex", "...")
    assert composite.loudness is not None and composite.loudness.input_i == -22.4
    assert hit.output_loudness is not None and hit.output_loudness.input_i == -16.48


def test_a_bgm_that_was_used_is_read_back_as_used(tmp_path: Path) -> None:
    """``bgm`` 不是 null ⇒ 那次贴了 BGM（判据是那一轮挑素材的结果，不靠猜）。"""
    hit = _hit(tmp_path, bgm="data/assets/bgm/a.mp3")

    assert hit is not None
    assert hit.composite.bgm_applied is True


def test_a_missing_loudness_reading_is_not_a_reason_to_re_render(tmp_path: Path) -> None:
    """响度量不出来 ⇒ 那一栏是 null，**不是**判不命中（它是报告，不是出片的前提）。"""
    hit = _hit(tmp_path, loudness=None, output_loudness=None)

    assert hit is not None
    assert hit.composite.loudness is None
    assert hit.output_loudness is None


def test_a_manifest_without_warnings_is_still_usable(tmp_path: Path) -> None:
    """``warnings`` 允许缺省（缺省 = 那一轮没有话要说）；其余字段缺一个就重渲。"""
    artifact = _artifact(tmp_path)
    payload = _payload(artifact)
    del payload["warnings"]
    hit = reusable_output(manifest=_manifest(tmp_path, payload), plan_hash=HASH)

    assert hit is not None
    assert hit.warnings == ()
    assert hit.composite.warnings == ()


# ══════════════════════════════════════════════════════════════════════
# 不命中：指纹对不上
# ══════════════════════════════════════════════════════════════════════


def test_a_different_plan_hash_is_a_miss(tmp_path: Path) -> None:
    """★ 判据本身：输入变了一个字节，指纹就不是同一个，必须重渲。"""
    assert _hit(tmp_path, plan_hash=OTHER_HASH) is None


def test_a_manifest_from_another_task_is_a_miss(tmp_path: Path) -> None:
    """manifest 里没有这次要渲的东西（指纹不同）⇒ 重渲，而不是"有 manifest 就用"。"""
    assert reusable_output(manifest=_manifest(tmp_path), plan_hash=OTHER_HASH) is None


# ══════════════════════════════════════════════════════════════════════
# 不命中：manifest 本身不可用
# ══════════════════════════════════════════════════════════════════════


def test_no_manifest_is_a_miss(tmp_path: Path) -> None:
    """这条任务还没出过片 ⇒ 没有可复用的东西。"""
    assert reusable_output(manifest=tmp_path / "work" / "t1" / "manifest.json", plan_hash=HASH) is None


def test_a_broken_manifest_is_a_miss(tmp_path: Path) -> None:
    """JSON 坏了（写到一半断电）⇒ 重渲，**不抛**。"""
    target = tmp_path / "work" / "t1" / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ 这不是 JSON", encoding="utf-8")

    assert reusable_output(manifest=target, plan_hash=HASH) is None


def test_a_manifest_that_is_not_an_object_is_a_miss(tmp_path: Path) -> None:
    target = tmp_path / "work" / "t1" / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[]", encoding="utf-8")

    assert reusable_output(manifest=target, plan_hash=HASH) is None


@pytest.mark.parametrize(
    "key",
    [
        "final",
        "size_bytes",
        "duration_ms",
        "bg_fill",
        "argv",
        "profile",
        "degraded",
        "attempts",
        "watermark",
        "subtitle",
        "composite_hash",
    ],
)
def test_a_manifest_missing_a_field_is_a_miss(tmp_path: Path, key: str) -> None:
    """★ 缺字段 ⇒ 重渲，而不是拿个默认值把 ``CompositeResult`` 拼出来。

    这些字段少一个，"这一支是怎么渲出来的"就有一段是编的 —— 编出来的 manifest 会
    被下一次复用当成真的。
    """
    artifact = _artifact(tmp_path)
    payload = _payload(artifact)
    del payload[key]

    assert reusable_output(manifest=_manifest(tmp_path, payload), plan_hash=HASH) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("size_bytes", "2048"),
        ("duration_ms", None),
        ("bg_fill", 30),
        ("argv", [1, 2]),
        ("argv", "ffmpeg -i x"),
        ("degraded", "false"),
        ("attempts", ["ok", 7]),
        ("watermark", ["enabled"]),
        ("subtitle", None),
    ],
)
def test_a_wrong_typed_field_is_a_miss(tmp_path: Path, key: str, value: object) -> None:
    """类型不对与缺字段同罪：``"false"`` 不是 ``False``，``"2048"`` 不是 2048。"""
    artifact = _artifact(tmp_path)
    payload = _payload(artifact)
    payload[key] = value

    assert reusable_output(manifest=_manifest(tmp_path, payload), plan_hash=HASH) is None


def test_a_boolean_size_is_a_miss(tmp_path: Path) -> None:
    """★ ``bool`` 是 ``int`` 的子类：``size_bytes: true`` 会被读成 1 字节。

    这类"能过 ``isinstance`` 的错值"正是要挡的 —— 挡不住就变成"用 1 字节去比成片大小"，
    比出来永远是"不一致"，于是缓存**永远不命中**，而看起来一切正常。
    """
    artifact = _artifact(tmp_path)
    payload = _payload(artifact, size_bytes=True)

    assert reusable_output(manifest=_manifest(tmp_path, payload), plan_hash=HASH) is None


# ══════════════════════════════════════════════════════════════════════
# 不命中：产物本身对不上
# ══════════════════════════════════════════════════════════════════════


def test_a_cleaned_up_artifact_is_a_miss(tmp_path: Path) -> None:
    """成片被清理策略回收了（manifest 还在）⇒ 重渲。"""
    artifact, manifest = _rig(tmp_path)
    artifact.unlink()

    assert reusable_output(manifest=manifest, plan_hash=HASH) is None


def test_a_truncated_artifact_is_a_miss(tmp_path: Path) -> None:
    """★ 陷阱 #9 的另一面：文件在、名字对，但**字节数不对** ⇒ 重渲。

    只看"文件在不在"的短路会把这一支当成成品交出去。
    """
    artifact, manifest = _rig(tmp_path)
    artifact.write_bytes(b"x")

    assert reusable_output(manifest=manifest, plan_hash=HASH) is None


def test_an_artifact_that_grew_is_a_miss(tmp_path: Path) -> None:
    """被换过内容（哪怕更长）⇒ 同样不认。"""
    artifact, manifest = _rig(tmp_path)
    artifact.write_bytes(b"x" * 9999)

    assert reusable_output(manifest=manifest, plan_hash=HASH) is None


def test_a_manifest_pointing_at_another_home_is_a_miss(tmp_path: Path) -> None:
    """manifest 记的是另一个家目录下的路径（换了 STUDIO_HOME / 挪了 data）⇒ 重渲。"""
    payload = _payload(tmp_path / "somewhere-else" / "t1_final.mp4", size=2048)

    assert reusable_output(manifest=_manifest(tmp_path, payload), plan_hash=HASH) is None
