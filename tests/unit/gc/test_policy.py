"""``gc/policy.py`` 单测：保留期快照与删除闸口（T4.12 · §03.7.5）。

两条"必须做到"
--------------
① **白名单只写一处**：成片 / 封面 / 稿件 / 备份 / 素材库 / 热点归档 / 任务交付物
   一律拒绝，且拒绝是**抛异常**而不是静默跳过 —— 静默跳过会让"候选清单写错了"
   看起来像"本轮没什么可删的"；
② **越界即拒**：``data/`` 之外的东西碰都不碰。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.gc.policy import PROTECTED_WORK_NAMES, RetentionPolicy, guard_path, protected_roots


class _Retention:
    """`config/app.yaml → retention` 的最小替身（pydantic 模型在别处验）。"""

    sentence_audio_hours = 6
    voice_master_hours = 12
    scene_artifact_hours = 48
    system_logs_days = 14
    debug_logs_days = 3
    llm_calls_days = 45
    task_events_days = 60
    hot_archive_days = 30
    tts_cache_max_gb = 2.5


# ══════════════════════════════════════════════════════════════════════
# 保留期快照
# ══════════════════════════════════════════════════════════════════════


def test_defaults_match_the_spec_table() -> None:
    """默认值必须与 §03.7.5 的表格逐条一致（它是"读不到配置"时的兜底）。"""
    policy = RetentionPolicy()

    assert (policy.sentence_audio_hours, policy.voice_master_hours) == (24, 24)
    assert policy.scene_artifact_hours == 168
    assert (policy.system_logs_days, policy.debug_logs_days) == (30, 7)
    assert (policy.llm_calls_days, policy.task_events_days) == (90, 90)
    assert policy.hot_archive_days == 90
    assert policy.tts_cache_max_gb == 5.0
    assert policy.tts_cache_limit_bytes == 5 * 1024**3


def test_snapshot_takes_every_value_from_config() -> None:
    policy = RetentionPolicy.from_config(_Retention())

    assert policy.sentence_audio_hours == 6
    assert policy.voice_master_hours == 12
    assert policy.scene_artifact_hours == 48
    assert policy.system_logs_days == 14
    assert policy.debug_logs_days == 3
    assert policy.llm_calls_days == 45
    assert policy.task_events_days == 60
    assert policy.hot_archive_days == 30
    assert policy.tts_cache_max_gb == 2.5


def test_snapshot_falls_back_when_a_field_is_missing() -> None:
    class Partial:
        system_logs_days = 10

    policy = RetentionPolicy.from_config(Partial())

    assert policy.system_logs_days == 10
    assert policy.debug_logs_days == 7


def test_snapshot_ignores_a_bool() -> None:
    """★ ``debug_logs_days: true`` 不能静默变成"保留 1 天"。

    ``True`` 是 ``int`` 的子类，不显式排除就会被 ``int()`` 收下 ——
    一个从 YAML 表面看不出来的**过度清理**。
    """

    class Boolean:
        debug_logs_days = True

    assert RetentionPolicy.from_config(Boolean()).debug_logs_days == 7


def test_no_config_means_defaults() -> None:
    assert RetentionPolicy.from_config(None) == RetentionPolicy()


def test_to_dict_covers_every_field() -> None:
    payload = RetentionPolicy().to_dict()

    assert set(payload) == {
        "sentence_audio_hours",
        "voice_master_hours",
        "scene_artifact_hours",
        "system_logs_days",
        "debug_logs_days",
        "llm_calls_days",
        "task_events_days",
        "hot_archive_days",
        "tts_cache_max_gb",
    }


# ══════════════════════════════════════════════════════════════════════
# 删除闸口
# ══════════════════════════════════════════════════════════════════════


def test_guard_allows_sentence_audio(tmp_paths: StudioPaths) -> None:
    """句子音频是白名单**唯一**的例外（§03.7.5 给了它 24 小时 TTL）。"""
    path = tmp_paths.sentence_wav("t1", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF")

    assert guard_path(path, paths=tmp_paths) == path.resolve()


def test_guard_allows_scenes_and_voice_master(tmp_paths: StudioPaths) -> None:
    for path in (
        tmp_paths.scenes_dir_for("t1") / "s001.mp4",
        tmp_paths.voice_master("t1"),
    ):
        assert guard_path(path, paths=tmp_paths) == path.resolve()


@pytest.mark.parametrize(
    "relative",
    [
        "assets/mc_parkour/clip01.mp4",
        "backups/studio_20260914.db",
        "hot/archive/202609/a.md",
        "output/covers/x_cover.jpg",
        "output/topics/t1.json",
        "output/videos/x_final.mp4",
        "voice_src/bigbear/ref.wav",
    ],
)
def test_guard_refuses_protected_roots(tmp_paths: StudioPaths, relative: str) -> None:
    """★ 白名单：成片 / 封面 / 稿件 / 备份 / 素材库 / 热点归档 —— 一个都不许删。"""
    path = tmp_paths.data_dir / Path(relative)

    with pytest.raises(StudioError) as caught:
        guard_path(path, paths=tmp_paths)

    assert caught.value.code is ErrorCode.GC_REFUSED_PROTECTED
    assert caught.value.remediation


@pytest.mark.parametrize(
    "relative", ["graphs/filter_complex.txt", "ir.json", "manifest.json", "script.json", "timeline.json"]
)
def test_guard_refuses_task_deliverables(tmp_paths: StudioPaths, relative: str) -> None:
    """任务交付物（滤镜图 / IR / manifest / 稿件 / 时间轴）永久保留。"""
    path = tmp_paths.work_dir_for("t1") / relative

    with pytest.raises(StudioError) as caught:
        guard_path(path, paths=tmp_paths)

    assert caught.value.code is ErrorCode.GC_REFUSED_PROTECTED


def test_guard_refuses_paths_outside_data(tmp_paths: StudioPaths) -> None:
    """★ 越界即拒：``prompts/`` 在 ``STUDIO_HOME`` 下，但不在 ``data/`` 下。"""
    outside = tmp_paths.prompts_dir / "writer.md"

    with pytest.raises(StudioError) as caught:
        guard_path(outside, paths=tmp_paths)

    assert caught.value.code is ErrorCode.GC_PATH_OUT_OF_BOUNDS


def test_protected_roots_are_inside_data(tmp_paths: StudioPaths) -> None:
    """白名单里的每一项都得真的在 ``data/`` 下 —— 否则守卫是空转的。"""
    for root in protected_roots(tmp_paths):
        assert tmp_paths.data_dir.resolve() in root.resolve().parents


def test_protected_work_names_are_the_deliverables() -> None:
    assert {"graphs", "ir.json", "manifest.json", "script.json", "timeline.json"} == PROTECTED_WORK_NAMES
