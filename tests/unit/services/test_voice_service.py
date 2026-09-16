"""``voice_service`` 的纯函数与"试听从哪取"（T2.9 · §04.3）。

为什么这几条要用单测而不是集成测试
----------------------------------
它们是**判据**，不是流程：音色该不该覆盖（三种结局）、试听该读盘上哪一份（三处候选）、
过期的时间轴还能不能读。这些判断错了不会抛异常，只会让结果**悄悄不对** ——
比如"``voice_map`` 里写了但本机没装"被当成"配好了"，用户就会拿到一条不是自己要的
音色的片子。判据用假件几毫秒钉死，比跑一遍真 SAPI 清楚得多。

真链路在 ``tests/integration/test_voice_api.py``（REST 面 + 真 ffmpeg 重算时间轴）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.models import SentenceRow
from studio.db.repositories import ScriptRepo, SentenceRepo
from studio.domain.task_service import TaskService
from studio.services.voice_service import (
    VOICE_SOURCE_DEFAULT,
    VOICE_SOURCE_FALLBACK,
    VOICE_SOURCE_MAP,
    preview_audio,
    read_timeline_total_ms,
    resolve_voice,
    voice_payloads,
)

INSTALLED = ("熊大", "熊二")
TEXTS = ("第一句。", "第二句。")


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    return value


@pytest.fixture
def connection(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    migrate(paths.db_file)
    handle = connect(paths.db_file)
    try:
        yield handle
    finally:
        handle.close()


def _task(connection: sqlite3.Connection, *, voice_map: dict[str, str] | None = None) -> str:
    """一条带两句稿子的任务（熊大 + 熊二各一句）。"""
    tasks = TaskService(connection)
    payload: dict[str, object] = {"seed": 7}
    if voice_map is not None:
        payload["voice_map"] = voice_map
    task_id = tasks.create(title="判据", payload=payload).id
    saved = ScriptRepo(connection).save_draft(
        task_id=task_id,
        title="标题",
        hook="钩子",
        body_md="正文",
        cta="关注",
        word_count=10,
        est_duration_ms=5_000,
        speaker_ratio={"bigbear": 0.5, "littlebear": 0.5},
        outline={"hook_3s": "钩子", "segments": []},
        sentences=[
            {
                "seq": seq,
                "text_raw": text,
                "text": text,
                "speaker": "bigbear" if seq == 1 else "littlebear",
                "emotion": "neutral",
                "pause_after_ms": 200,
            }
            for seq, text in enumerate(TEXTS, start=1)
        ],
    )
    connection.commit()
    assert len(saved.sentence_ids) == 2
    return task_id


def _row(connection: sqlite3.Connection, task_id: str, seq: int) -> SentenceRow:
    row = SentenceRepo(connection).get_by_seq(task_id, seq)
    assert row is not None
    return row


# ══════════════════════════════════════════════════════════════════════
# ① 音色解析：三种结局
# ══════════════════════════════════════════════════════════════════════


def test_an_absent_speaker_is_not_overridden() -> None:
    """``voice_map`` 里没有这个角色 ⇒ ``voice=None``（**不覆盖**），由池进程的音色兜底。

    这里的 ``None`` 不是"没解析出来"，而是"这件事不该由这里定" —— 进程级音色在
    ``build_voice_handler`` 里已经解析过一次了。
    """
    resolved = resolve_voice(voice_map={}, speaker="bigbear", available=INSTALLED)

    assert resolved.voice is None
    assert resolved.source == VOICE_SOURCE_DEFAULT
    assert resolved.is_fallback is False


def test_a_known_speaker_overrides_the_process_voice() -> None:
    resolved = resolve_voice(voice_map={"bigbear": "熊二"}, speaker="bigbear", available=INSTALLED)

    assert resolved.voice == "熊二"
    assert resolved.requested == "熊二"
    assert resolved.source == VOICE_SOURCE_MAP


def test_a_voice_this_machine_does_not_have_falls_back_but_says_so() -> None:
    """★ ``voice_map`` 写了、本机没有 ⇒ **不覆盖**，但把"你要的那个"留在报告里。

    ``TaskPayload.voice_map`` 的默认值是逻辑角色名（``bigbear``），本机没装同名音色是
    常态 —— 在这里抛就等于"每个新建任务都配不出音"。所以退回进程音色，但**说出来**：
    静默退回的后果是"用户以为用的是自己选的那个音色"。
    """
    resolved = resolve_voice(voice_map={"bigbear": "不存在的音色"}, speaker="bigbear", available=INSTALLED)

    assert resolved.voice is None
    assert resolved.requested == "不存在的音色"
    assert resolved.source == VOICE_SOURCE_FALLBACK
    assert resolved.is_fallback is True
    assert resolved.to_dict()["requested"] == "不存在的音色"


def test_the_default_role_names_fall_back_instead_of_breaking_a_new_task() -> None:
    """默认映射（``bigbear`` → ``bigbear``）在本机没装同名音色时**不该**让任务配不出音。"""
    resolved = resolve_voice(voice_map={"bigbear": "bigbear"}, speaker="bigbear", available=INSTALLED)

    assert resolved.voice is None
    assert resolved.is_fallback is True


# ══════════════════════════════════════════════════════════════════════
# ② 投递时带上运行期音色
# ══════════════════════════════════════════════════════════════════════


def test_every_sentence_gets_a_payload_even_when_it_has_no_override(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """★ 每句**都有键**（解析不出音色时是空 dict）。

    返回 ``.get(id)`` 拿到 ``None`` 的话，调用方分不出"这一句没有覆盖"与"我忘了给它"。
    """
    task_id = _task(connection, voice_map={"bigbear": "熊大", "littlebear": "不存在的音色"})

    payloads = voice_payloads(connection=connection, task_id=task_id, available=INSTALLED)

    rows = SentenceRepo(connection).list_for_task(task_id)
    assert set(payloads) == {row.id for row in rows}
    assert payloads[rows[0].id] == {"voice": "熊大"}
    assert payloads[rows[1].id] == {}  # 熊二那个音色本机没有 ⇒ 不覆盖


def test_payloads_are_empty_when_the_task_has_no_sentences(
    connection: sqlite3.Connection,
) -> None:
    task_id = TaskService(connection).create(title="还没落稿").id

    assert voice_payloads(connection=connection, task_id=task_id, available=INSTALLED) == {}


# ══════════════════════════════════════════════════════════════════════
# ③ 试听：读盘上哪一份
# ══════════════════════════════════════════════════════════════════════


def _touch(path: Path, payload: bytes = b"RIFF....WAVE") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_preview_reads_the_canonical_sentence_wav(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    task_id = _task(connection)
    row = _row(connection, task_id, 1)
    canonical = _touch(paths.sentence_wav(task_id, 1))

    found = preview_audio(paths=paths, row=row)

    assert found == (canonical, "sentence")


def test_preview_falls_back_to_the_recorded_path(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """规范路径上那份被清理掉了（§03.7.5 的 24h GC）⇒ 读库里记的那一份。"""
    task_id = _task(connection)
    row = _row(connection, task_id, 1)
    recorded = _touch(paths.work_dir_for(task_id) / "tts" / "s001.wav")

    found = preview_audio(paths=paths, row=replace(row, tts_audio_path=str(recorded)))

    assert found == (recorded, "recorded")


def test_preview_refuses_a_recorded_path_outside_the_data_dir(
    connection: sqlite3.Connection, paths: StudioPaths, tmp_path: Path
) -> None:
    """★ ``tts_audio_path`` 是**库里的一个字符串**：落在 ``data_dir`` 之外一律不认。

    不判这一条，这个端点就是一个任意文件读取 —— 而它的入参看起来只是"某一句的音频"。
    """
    task_id = _task(connection)
    row = _row(connection, task_id, 1)
    outside = _touch(tmp_path / "secret.wav")

    assert preview_audio(paths=paths, row=replace(row, tts_audio_path=str(outside))) is None


def test_preview_falls_back_to_the_tts_cache(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """两处交付产物都没了、但缓存还在 ⇒ 播缓存那一份（**不触发新合成**）。"""
    task_id = _task(connection)
    row = _row(connection, task_id, 1)
    cached = _touch(paths.tts_cache_dir / "abc123.wav")

    found = preview_audio(paths=paths, row=replace(row, tts_hash="abc123"))

    assert found == (cached, "cache")


def test_preview_says_nothing_when_there_is_nothing(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """一处都没有 ⇒ ``None``（**绝不**顺手合成一份：那是"看一眼"变成"重跑一遍"）。"""
    task_id = _task(connection)
    row = _row(connection, task_id, 1)

    assert preview_audio(paths=paths, row=row) is None
    assert not paths.sentence_wav(task_id, 1).exists()
    assert not paths.tts_cache_dir.exists() or not list(paths.tts_cache_dir.iterdir())


# ══════════════════════════════════════════════════════════════════════
# ④ 盘上那份时间轴还能不能读
# ══════════════════════════════════════════════════════════════════════


def test_timeline_total_is_read_from_disk(paths: StudioPaths) -> None:
    target = paths.timeline_json("01TASK")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"total_ms": 17_470, "tail_ms": 400}), encoding="utf-8")

    assert read_timeline_total_ms(paths, "01TASK") == 17_470


def test_a_missing_or_broken_timeline_is_not_an_error(paths: StudioPaths) -> None:
    """读不动就 ``None``：**过期的时间轴**是常态（重配之后它就不再描述这条片子了），
    而为了读一个提示性的数字把请求打回去，会让"重配"在第一次收口之前永远失败。
    """
    assert read_timeline_total_ms(paths, "01TASK") is None
    assert read_timeline_total_ms(None, "01TASK") is None

    target = paths.timeline_json("01TASK")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{不是 json", encoding="utf-8")
    assert read_timeline_total_ms(paths, "01TASK") is None

    target.write_text(json.dumps({"total_ms": "十七秒"}), encoding="utf-8")
    assert read_timeline_total_ms(paths, "01TASK") is None
