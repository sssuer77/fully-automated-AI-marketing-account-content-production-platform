"""``gc/media.py`` 单测：文件回收与"不误删成片"（T4.12 · §03.7.5）。

**这是本轮最重要的一份测试。** GC 无人值守地跑，判据写错的代价不对称：
多留一会儿只是占盘，删错一次是不可逆的。所以这里第一组用例是
:func:`test_deliverables_are_never_touched` —— 把一棵**完整的**任务树摆好，
跑一轮"什么都过期"的 GC，逐个断言交付物还在。

其余按 §03.7.5 的表格逐行验：句子音频 / 人声母带 / 场景产物 / ``.partial`` /
TTS 缓存 LRU / 热点"移动而非删除"。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.gc.media import (
    REASON_HOT_ARCHIVE,
    REASON_PARTIAL,
    REASON_SCENE_ARTIFACT,
    REASON_SENTENCE_AUDIO,
    REASON_TTS_CACHE,
    REASON_VOICE_MASTER,
    SKIP_FINAL_MISSING,
    SKIP_FINAL_UNKNOWN,
    CompletedTask,
    _Collector,
    gc_media,
)
from studio.gc.policy import RetentionPolicy, guard_path

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


def _write(path: Path, *, age_hours: float = 0.0, size: int = 4) -> Path:
    """造一个文件并把它"做旧"（mtime 是 GC 唯一的年龄判据）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    moment = (NOW - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (moment, moment))
    return path


def _policy(**overrides: object) -> RetentionPolicy:
    """默认策略 + 覆盖几项（测试里只想动一个数）。"""
    values = RetentionPolicy().to_dict()
    values.update(overrides)
    return RetentionPolicy(**values)


@pytest.fixture
def tree(tmp_paths: StudioPaths) -> dict[str, Path]:
    """一棵**完整的**已完成任务树：交付物 + 该被回收的中间产物。"""
    task = "t1"
    made = {
        "final": _write(tmp_paths.final_video(task, stamp="20260913-101500"), size=64),
        "cover": _write(tmp_paths.cover_image(task, stamp="20260913-101500"), size=16),
        "topic": _write(tmp_paths.topic_card(task), size=8),
        "script": _write(tmp_paths.script_json(task), size=8),
        "timeline": _write(tmp_paths.timeline_json(task), size=8),
        "ir": _write(tmp_paths.ir_json(task), size=8),
        "manifest": _write(tmp_paths.manifest_json(task), size=8),
        "graph": _write(tmp_paths.graphs_dir_for(task) / "filter_complex.txt", size=8),
        "sentence": _write(tmp_paths.sentence_wav(task, 1), age_hours=48),
        "voice_master": _write(tmp_paths.voice_master(task), age_hours=48),
        "scene": _write(tmp_paths.scenes_dir_for(task) / "s001.mp4", age_hours=200),
    }
    return made


# ══════════════════════════════════════════════════════════════════════
# ★ 不误删成片
# ══════════════════════════════════════════════════════════════════════


def test_deliverables_are_never_touched(tmp_paths: StudioPaths, tree: dict[str, Path]) -> None:
    """★ 把交付物和过期中间产物摆在一起跑一轮，交付物必须一个不少。"""
    result = gc_media(
        tmp_paths,
        policy=RetentionPolicy(),
        now=NOW,
        completed=(CompletedTask("t1", NOW - timedelta(days=10), tree["final"]),),
    )

    for name in ("final", "cover", "topic", "script", "timeline", "ir", "manifest", "graph"):
        assert tree[name].is_file(), f"{name} 被误删了"
    assert result.refused == ()
    assert not result.deleted or {item.reason for item in result.deleted} <= {
        REASON_SENTENCE_AUDIO,
        REASON_VOICE_MASTER,
        REASON_SCENE_ARTIFACT,
    }


def test_expired_intermediates_do_go(tmp_paths: StudioPaths, tree: dict[str, Path]) -> None:
    """与上一条配对：该删的真删了 —— 否则"没误删"可能只是"什么都没删"。"""
    gc_media(
        tmp_paths,
        policy=RetentionPolicy(),
        now=NOW,
        completed=(CompletedTask("t1", NOW - timedelta(days=10), tree["final"]),),
    )

    assert not tree["sentence"].exists()
    assert not tree["voice_master"].exists()
    assert not tree["scene"].exists()


def test_dry_run_touches_nothing(tmp_paths: StudioPaths, tree: dict[str, Path]) -> None:
    result = gc_media(
        tmp_paths,
        policy=RetentionPolicy(),
        now=NOW,
        completed=(CompletedTask("t1", NOW - timedelta(days=10), tree["final"]),),
        dry_run=True,
    )

    assert result.dry_run is True
    assert tree["sentence"].is_file()
    assert tree["scene"].is_file()
    assert {item.reason for item in result.deleted} >= {REASON_SENTENCE_AUDIO, REASON_SCENE_ARTIFACT}


# ══════════════════════════════════════════════════════════════════════
# 句子音频 / 人声母带
# ══════════════════════════════════════════════════════════════════════


def test_sentence_audio_waits_for_its_ttl(tmp_paths: StudioPaths) -> None:
    """24 小时以内不动（"刚做完就被清掉"是最气人的一种 bug）。"""
    _write(tmp_paths.sentence_wav("t1", 1), age_hours=2)
    final = _write(tmp_paths.final_video("t1", stamp="20260914-020000"), size=64)

    gc_media(
        tmp_paths,
        policy=RetentionPolicy(),
        now=NOW,
        completed=(CompletedTask("t1", NOW - timedelta(hours=2), final),),
    )

    assert tmp_paths.sentence_wav("t1", 1).is_file()


def test_sentence_audio_is_kept_when_the_final_is_gone(tmp_paths: StudioPaths) -> None:
    """★ 成片不在 ⇒ 音频保留（缓存会被 LRU 淘汰，音频可能是最后一份）。"""
    wav = _write(tmp_paths.sentence_wav("t1", 1), age_hours=48)
    missing = tmp_paths.final_video("t1", stamp="20260912-090000")

    result = gc_media(
        tmp_paths,
        policy=RetentionPolicy(),
        now=NOW,
        completed=(CompletedTask("t1", NOW - timedelta(days=10), missing),),
    )

    assert wav.is_file()
    assert [item.code for item in result.skipped] == [SKIP_FINAL_MISSING]


def test_sentence_audio_is_kept_when_the_final_is_unknown(tmp_paths: StudioPaths) -> None:
    """上下文里没记 ``final_path`` ⇒ 也保留（"不知道"不许当成"不在"）。"""
    wav = _write(tmp_paths.sentence_wav("t1", 1), age_hours=48)

    result = gc_media(
        tmp_paths,
        policy=RetentionPolicy(),
        now=NOW,
        completed=(CompletedTask("t1", NOW - timedelta(days=10), None),),
    )

    assert wav.is_file()
    assert [item.code for item in result.skipped] == [SKIP_FINAL_UNKNOWN]


def test_voice_master_follows_its_own_ttl(tmp_paths: StudioPaths) -> None:
    """两条 TTL 可以不同：句子音频 24h、母带 72h ⇒ 3 天时只删音频。"""
    policy = _policy(voice_master_hours=72)
    wav = _write(tmp_paths.sentence_wav("t1", 1), age_hours=48)
    master = _write(tmp_paths.voice_master("t1"), age_hours=48)
    final = _write(tmp_paths.final_video("t1", stamp="20260912-090000"), size=64)

    result = gc_media(
        tmp_paths,
        policy=policy,
        now=NOW,
        completed=(CompletedTask("t1", NOW - timedelta(hours=48), final),),
    )

    assert not wav.exists()
    assert master.is_file()
    assert {item.reason for item in result.deleted} == {REASON_SENTENCE_AUDIO}


# ══════════════════════════════════════════════════════════════════════
# 场景中间产物 / .partial
# ══════════════════════════════════════════════════════════════════════


def test_scenes_expire_by_mtime(tmp_paths: StudioPaths) -> None:
    fresh = _write(tmp_paths.scenes_dir_for("t1") / "s001.mp4", age_hours=24)
    stale = _write(tmp_paths.scenes_dir_for("t1") / "s002.mp4", age_hours=200)

    result = gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW)

    assert fresh.is_file()
    assert not stale.exists()
    assert [item.reason for item in result.deleted] == [REASON_SCENE_ARTIFACT]


def test_failed_task_partials_go_immediately(tmp_paths: StudioPaths) -> None:
    """★ §03.7.5「失败任务立即清理 ``.partial``」—— 不等 7 天 TTL。"""
    partial = _write(tmp_paths.scenes_dir_for("t1") / "s003.mp4.partial", age_hours=0)

    result = gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW, failed_task_ids=("t1",))

    assert not partial.exists()
    assert [item.reason for item in result.deleted] == [REASON_PARTIAL]


def test_active_task_partials_stay(tmp_paths: StudioPaths) -> None:
    """在途渲染的 ``.partial`` 不许碰（删了等于把正在跑的任务弄坏）。"""
    partial = _write(tmp_paths.scenes_dir_for("t1") / "s003.mp4.partial", age_hours=0)

    result = gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW, failed_task_ids=("other",))

    assert partial.is_file()
    assert result.deleted == ()


def test_stale_partials_go_even_for_unknown_tasks(tmp_paths: StudioPaths) -> None:
    """兜底：崩在"任务还没落库"那一刻留下的 ``.partial``，按 TTL 也能清掉。"""
    partial = _write(tmp_paths.scenes_dir_for("ghost") / "s001.mp4.partial", age_hours=200)

    gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW)

    assert not partial.exists()


# ══════════════════════════════════════════════════════════════════════
# TTS 缓存（LRU）
# ══════════════════════════════════════════════════════════════════════


def _cache_entry(paths: StudioPaths, name: str, *, age_hours: float, use_count: int | None = None) -> Path:
    path = _write(paths.tts_cache_dir / name, age_hours=age_hours, size=10)
    if use_count is not None:
        (path.parent / f"{path.name}.meta.json").write_text(
            f'{{"use_count": {use_count}, "last_used_at": "2026-09-01T00:00:00.000Z"}}', encoding="utf-8"
        )
    return path


def test_tts_cache_evicts_the_oldest_first(tmp_paths: StudioPaths) -> None:
    """上限 = 两份 ⇒ 最久没用过的那一份走。"""
    old = _cache_entry(tmp_paths, "a.wav", age_hours=100)
    middle = _cache_entry(tmp_paths, "b.wav", age_hours=50)
    newest = _cache_entry(tmp_paths, "c.wav", age_hours=1)
    policy = _policy(tts_cache_max_gb=20 / 1024**3)

    result = gc_media(tmp_paths, policy=policy, now=NOW)

    assert not old.exists()
    assert middle.is_file()
    assert newest.is_file()
    assert [item.reason for item in result.deleted] == [REASON_TTS_CACHE]
    assert result.tts_cache_bytes <= result.tts_cache_limit_bytes


def test_tts_cache_spares_reused_entries(tmp_paths: StudioPaths) -> None:
    """★ §03.7.5：``use_count >= 2`` 的条目**淘汰降权** —— 先淘汰别人。"""
    reused = _cache_entry(tmp_paths, "a.wav", age_hours=100, use_count=5)
    plain_b = _cache_entry(tmp_paths, "b.wav", age_hours=50)
    plain_c = _cache_entry(tmp_paths, "c.wav", age_hours=1)
    policy = _policy(tts_cache_max_gb=10 / 1024**3)

    gc_media(tmp_paths, policy=policy, now=NOW)

    assert reused.is_file()
    assert not plain_b.exists()
    assert not plain_c.exists()


def test_tts_cache_below_the_limit_is_left_alone(tmp_paths: StudioPaths) -> None:
    kept = _cache_entry(tmp_paths, "a.wav", age_hours=500)

    result = gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW)

    assert kept.is_file()
    assert result.deleted == ()


def test_tts_cache_sidecar_goes_with_its_audio(tmp_paths: StudioPaths) -> None:
    entry = _cache_entry(tmp_paths, "a.wav", age_hours=100, use_count=1)
    _cache_entry(tmp_paths, "b.wav", age_hours=1)
    sidecar = entry.parent / "a.wav.meta.json"

    gc_media(tmp_paths, policy=_policy(tts_cache_max_gb=10 / 1024**3), now=NOW)

    assert not entry.exists()
    assert not sidecar.exists()


# ══════════════════════════════════════════════════════════════════════
# 热点归档（移动而非删除）
# ══════════════════════════════════════════════════════════════════════


def test_hot_items_are_moved_not_deleted(tmp_paths: StudioPaths) -> None:
    stale = _write(tmp_paths.hot_dir / "hot_20260601_ai.md", age_hours=24 * 100)
    fresh = _write(tmp_paths.hot_dir / "hot_20260914_ai.md", age_hours=1)

    result = gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW)

    assert not stale.exists()
    assert fresh.is_file()
    assert [item.reason for item in result.moved] == [REASON_HOT_ARCHIVE]
    moved_to = result.moved[0].moved_to
    assert moved_to is not None and moved_to.is_file()
    assert moved_to.parent == tmp_paths.hot_archive_dir / "202606"


def test_hot_archive_is_never_cleaned(tmp_paths: StudioPaths) -> None:
    """★ 归档区是白名单：再老也不动（"保留复盘依据"）。"""
    ancient = _write(tmp_paths.hot_archive_dir / "202001" / "old.md", age_hours=24 * 2000)

    result = gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW)

    assert ancient.is_file()
    assert result.deleted == ()


def test_hot_archive_does_not_overwrite_a_same_named_file(tmp_paths: StudioPaths) -> None:
    _write(tmp_paths.hot_dir / "hot_x.md", age_hours=24 * 100)
    bucket = tmp_paths.hot_archive_dir / "202606"
    _write(bucket / "hot_x.md", age_hours=1)

    result = gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW)

    assert (bucket / "hot_x.md").read_bytes() == b"xxxx"
    assert result.moved[0].moved_to == bucket / "hot_x-2.md"


# ══════════════════════════════════════════════════════════════════════
# 闸口 / 报告
# ══════════════════════════════════════════════════════════════════════


def test_guard_refuses_a_protected_candidate(tmp_paths: StudioPaths) -> None:
    """★ 就算候选清单写错了，落地闸口也要拦住（这是最后一道防线）。"""
    final = _write(tmp_paths.videos_dir / "20260913-101500_t1_final.mp4", size=64)
    collector = _Collector(tmp_paths, dry_run=False)

    collector.delete(final, REASON_SCENE_ARTIFACT)

    assert final.is_file()
    assert [item.code for item in collector.refused] == [str(ErrorCode.GC_REFUSED_PROTECTED)]


def test_guard_refuses_a_candidate_outside_data(tmp_paths: StudioPaths) -> None:
    outside = _write(tmp_paths.prompts_dir / "writer.md")
    collector = _Collector(tmp_paths, dry_run=False)

    collector.delete(outside, REASON_SCENE_ARTIFACT)

    assert outside.is_file()
    assert [item.code for item in collector.refused] == [str(ErrorCode.GC_PATH_OUT_OF_BOUNDS)]


def test_report_serializes(tmp_paths: StudioPaths) -> None:
    _write(tmp_paths.scenes_dir_for("t1") / "s001.mp4", age_hours=200)

    payload = gc_media(tmp_paths, policy=RetentionPolicy(), now=NOW).to_dict()

    assert payload["dry_run"] is False
    assert len(payload["deleted"]) == 1
    assert payload["freed_bytes"] == 4
    assert payload["tmp_bytes"] >= 0
    assert payload["tts_cache_limit_bytes"] == 5 * 1024**3


def test_guard_path_is_the_only_door(tmp_paths: StudioPaths) -> None:
    """``gc_media`` 的每一次落地都必须过闸口 —— 用一条越界候选反向验证。"""
    stray = _write(tmp_paths.data_dir / "stray.bin")

    assert guard_path(stray, paths=tmp_paths) == stray.resolve()
    with pytest.raises(StudioError):
        guard_path(tmp_paths.home / "stray.bin", paths=tmp_paths)
