"""句级状态仓储（T2.6 · §03.3.7）。

这里测的是**句级状态机**与**version 竞态闸门**，不是 SQL 语法：合成跑在另一个
进程里、要几秒，这几秒里用户随时可能改这一句 —— 所有写回都带 ``expected_version``，
"改过了就丢弃结果"这条必须逐条钉住。用真实库（临时目录 + 迁移），因为要验的
正是 SQL 的 ``WHERE version = ?`` 与触发器维护的 ``updated_at``。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import SavedScript, ScriptRepo, SentenceRepo, TimelineSpan
from studio.domain import TaskService


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """迁移好的临时库（**用完关掉**，否则 Windows 上临时目录删不干净）。"""
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    return TaskService(connection).create(title="跑酷合集").id


@pytest.fixture
def saved(connection: sqlite3.Connection, task_id: str) -> SavedScript:
    sentences: list[dict[str, Any]] = [
        {
            "seq": index,
            "text_raw": f"第{index}句",
            "text": f"第{index}句",
            "speaker": "bigbear",
            "emotion": "neutral",
            "pause_after_ms": 200,
        }
        for index in range(1, 4)
    ]
    return ScriptRepo(connection).save_draft(
        task_id=task_id,
        title="标题",
        hook="钩子",
        body_md="正文",
        cta="关注",
        word_count=700,
        est_duration_ms=140_000,
        speaker_ratio={"bigbear": 1.0},
        outline={"hook_3s": "钩子", "segments": []},
        sentences=sentences,
    )


def _finish(repo: SentenceRepo, sentence_id: str, *, version: int = 1, **extra: Any) -> bool:
    payload: dict[str, Any] = {
        "expected_version": version,
        "audio_path": "data/output/voice/t1/s001.wav",
        "duration_ms": 1200,
        "sample_rate": 22_050,
        "tts_hash": "abc123",
        "engine": "sapi",
        "voice_id": "Microsoft Huihui Desktop",
    }
    payload.update(extra)
    return repo.finish(sentence_id, **payload)


# ══════════════════════════════════════════════════════════════════════
# begin：进入"正在合成"
# ══════════════════════════════════════════════════════════════════════


def test_begin_marks_synthesizing_and_records_engine(
    connection: sqlite3.Connection, saved: SavedScript
) -> None:
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]

    assert repo.begin(first, engine="sapi", voice_id="Huihui") is True

    row = repo.get(first)
    assert row is not None
    assert (row.tts_status, row.tts_engine, row.tts_voice_id) == ("synthesizing", "sapi", "Huihui")


def test_begin_is_allowed_again_after_a_crash(connection: sqlite3.Connection, saved: SavedScript) -> None:
    """进程被杀会把句子留在 ``synthesizing``，而重做是正常路径 —— 不该被状态挡住。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    repo.begin(first, engine="sapi", voice_id=None)
    assert repo.begin(first, engine="sapi", voice_id=None) is True


def test_begin_refuses_a_sentence_that_is_already_done(
    connection: sqlite3.Connection, saved: SavedScript
) -> None:
    """已完成的句子不是待办：重投一次作业不该把它打回"正在合成"。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    _finish(repo, first)
    assert repo.begin(first, engine="sapi", voice_id=None) is False
    row = repo.get(first)
    assert row is not None and row.tts_status == "done"


# ══════════════════════════════════════════════════════════════════════
# reopen：产物与缓存都没了 ⇒ 把 done 退回待办
# ══════════════════════════════════════════════════════════════════════


def test_reopen_returns_a_done_sentence_to_pending(
    connection: sqlite3.Connection, saved: SavedScript
) -> None:
    """★ ``done`` 的承诺是"音频已存在"；产物被 GC、缓存被淘汰之后这个承诺不成立。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    _finish(repo, first)

    assert repo.reopen(first, expected_version=1) is True

    row = repo.get(first)
    assert row is not None
    assert row.tts_status == "pending", "退回待办 ⇒ 重投时才会被真正重念"
    assert row.tts_hash is None, "缓存条目已经不存在了，留着只会让人以为还能复用"
    assert row.tts_error is None
    assert row.tts_audio_path == "data/output/voice/t1/s001.wav", (
        "路径由 seq 决定（§04.3.3 不变量 1）：重合成会原地覆盖它"
    )


def test_reopen_does_not_move_the_version(connection: sqlite3.Connection, saved: SavedScript) -> None:
    """``version`` 守的是"文本有没有被改过"，而这里一个字都没改。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    _finish(repo, first)

    repo.reopen(first, expected_version=1)

    row = repo.get(first)
    assert row is not None and row.version == 1


def test_reopen_refuses_when_the_text_changed(connection: sqlite3.Connection, saved: SavedScript) -> None:
    """读到的是旧行（别人已经改过这一句）⇒ 什么都不做，交给队列重试。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    _finish(repo, first)
    repo.update_text(first, text="改过的台词")

    assert repo.reopen(first, expected_version=1) is False

    row = repo.get(first)
    assert row is not None and row.tts_status == "pending", "改动过的那一行仍按编辑语义走"


def test_reopen_only_touches_done_sentences(connection: sqlite3.Connection, saved: SavedScript) -> None:
    """``skipped``（降级占位）与还没念过的句子都不该被它碰。"""
    repo = SentenceRepo(connection)
    first, second = saved.sentence_ids[0], saved.sentence_ids[1]
    repo.skip(first, expected_version=1, audio_path="p.wav", duration_ms=500, error="降级")

    assert repo.reopen(first, expected_version=1) is False, "降级是人工可查的结论，不该被悄悄改回待办"
    assert repo.reopen(second, expected_version=1) is False, "还没念过的句子本来就不是 done"

    row = repo.get(first)
    assert row is not None and row.tts_status == "skipped"


def test_a_reopened_sentence_can_be_synthesized_again(
    connection: sqlite3.Connection, saved: SavedScript
) -> None:
    """退回待办之后 ``begin`` 必须放行 —— 否则处理器会卡在"begin 被拒"那条分支上。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    _finish(repo, first)
    repo.reopen(first, expected_version=1)

    assert repo.begin(first, engine="sapi", voice_id="Huihui") is True
    assert _finish(repo, first, version=1, tts_hash="def456") is True
    row = repo.get(first)
    assert row is not None and (row.tts_status, row.tts_hash) == ("done", "def456")


# ══════════════════════════════════════════════════════════════════════
# finish：写回成功（带 version 闸门）
# ══════════════════════════════════════════════════════════════════════


def test_finish_writes_the_whole_result(connection: sqlite3.Connection, saved: SavedScript) -> None:
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]

    assert _finish(repo, first) is True

    row = repo.get(first)
    assert row is not None
    assert row.tts_status == "done"
    assert row.tts_audio_path == "data/output/voice/t1/s001.wav"
    assert row.tts_duration_ms == 1200
    assert row.tts_sample_rate == 22_050
    assert row.tts_hash == "abc123"
    assert row.tts_error is None


def test_finish_clears_the_previous_error(connection: sqlite3.Connection, saved: SavedScript) -> None:
    """上一轮的失败原因留在 ``tts_error`` 上，会让**已经念好**的句子显示成"上次失败过"。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    repo.fail(first, expected_version=1, error="SAPI 超时")
    _finish(repo, first)
    row = repo.get(first)
    assert row is not None and row.tts_error is None


def test_finish_discards_the_result_when_the_text_changed(
    connection: sqlite3.Connection, saved: SavedScript
) -> None:
    """★ 竞态：合成期间用户改了这一句 ⇒ **丢弃音频**，不把旧文本的声音贴到新文本上。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    repo.begin(first, engine="sapi", voice_id=None)
    new_version = repo.update_text(first, text="改过的台词")
    assert new_version == 2

    assert _finish(repo, first, version=1) is False

    row = repo.get(first)
    assert row is not None
    assert row.tts_status == "pending", "结果被丢弃 ⇒ 这一句仍是待办"
    assert row.tts_hash is None
    assert row.tts_audio_path is None, "过期的音频路径不该被写进去"


def test_finish_accepts_the_current_version(connection: sqlite3.Connection, saved: SavedScript) -> None:
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    version = repo.update_text(first, text="改过的台词")
    assert version == 2
    assert _finish(repo, first, version=2) is True


# ══════════════════════════════════════════════════════════════════════
# fail：计数与降级线
# ══════════════════════════════════════════════════════════════════════


def test_fail_counts_attempts(connection: sqlite3.Connection, saved: SavedScript) -> None:
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]

    assert repo.fail(first, expected_version=1, error="第 1 次") == 1
    assert repo.fail(first, expected_version=1, error="第 2 次") == 2
    assert repo.fail(first, expected_version=1, error="第 3 次") == 3

    row = repo.get(first)
    assert row is not None
    assert row.tts_status == "failed"
    assert row.tts_error == "第 3 次"


def test_fail_ignores_a_result_from_a_stale_version(
    connection: sqlite3.Connection, saved: SavedScript
) -> None:
    """过期失败不记账：否则改一句会让它的"失败次数"凭空多一次，三轮后被降级成静音。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    repo.update_text(first, text="改过的台词")

    assert repo.fail(first, expected_version=1, error="旧稿子的失败") is None

    row = repo.get(first)
    assert row is not None
    assert row.tts_attempts == 0
    assert row.tts_status == "pending"


# ══════════════════════════════════════════════════════════════════════
# skip：降级落"静音占位"
# ══════════════════════════════════════════════════════════════════════


def test_skip_keeps_the_timeline_inputs(connection: sqlite3.Connection, saved: SavedScript) -> None:
    """降级句也是**成功的一种**：有产物、有实测时长 ⇒ 时间轴照样算得出来。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]

    assert (
        repo.skip(
            first,
            expected_version=1,
            audio_path="data/output/voice/t1/s001.wav",
            duration_ms=1800,
            error="连续 3 次失败 ⇒ 静音占位",
        )
        is True
    )

    row = repo.get(first)
    assert row is not None
    assert (row.tts_status, row.tts_duration_ms) == ("skipped", 1800)
    assert row.tts_error == "连续 3 次失败 ⇒ 静音占位"


# ══════════════════════════════════════════════════════════════════════
# update_text：只失效这一句
# ══════════════════════════════════════════════════════════════════════


def test_update_text_invalidates_only_that_sentence(
    connection: sqlite3.Connection, saved: SavedScript
) -> None:
    """★ 句级缓存的意义全在这条：改第 2 句，第 1/3 句的 ``tts_hash`` 必须原封不动。"""
    repo = SentenceRepo(connection)
    first, second, third = saved.sentence_ids
    for index, sentence_id in enumerate((first, second, third)):
        _finish(repo, sentence_id, tts_hash=f"hash{index}")

    assert repo.update_text(second, text="改过的第二句") == 2

    updated = repo.get(second)
    assert updated is not None
    assert (updated.text, updated.tts_status, updated.tts_hash) == ("改过的第二句", "pending", None)
    for sentence_id, expected in ((first, "hash0"), (third, "hash2")):
        row = repo.get(sentence_id)
        assert row is not None
        assert (row.tts_status, row.tts_hash) == ("done", expected)


def test_update_text_keeps_the_audio_path(connection: sqlite3.Connection, saved: SavedScript) -> None:
    """路径由 ``seq`` 决定，重合成会原地覆盖 —— 清空它只会让面板在待办期间看不见音频。"""
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    _finish(repo, first)
    repo.update_text(first, text="改过的台词")
    row = repo.get(first)
    assert row is not None
    assert row.tts_audio_path == "data/output/voice/t1/s001.wav"


def test_update_text_can_set_a_subtitle(connection: sqlite3.Connection, saved: SavedScript) -> None:
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    repo.update_text(first, text="第1句", subtitle="字幕用这一行")
    row = repo.get(first)
    assert row is not None and row.subtitle == "字幕用这一行"


def test_update_text_leaves_the_subtitle_alone_when_not_given(
    connection: sqlite3.Connection, saved: SavedScript
) -> None:
    repo = SentenceRepo(connection)
    first = saved.sentence_ids[0]
    repo.update_text(first, text="第1句", subtitle="字幕用这一行")
    repo.update_text(first, text="又改了一次")
    row = repo.get(first)
    assert row is not None and row.subtitle == "字幕用这一行"


def test_update_text_on_a_missing_sentence_returns_none(connection: sqlite3.Connection) -> None:
    assert SentenceRepo(connection).update_text("nope", text="x") is None


# ══════════════════════════════════════════════════════════════════════
# 读：待办与进度
# ══════════════════════════════════════════════════════════════════════


def test_pending_for_task_covers_pending_and_failed_only(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    """``synthesizing`` / ``done`` / ``skipped`` **都不算待办**（续传语义 1）。"""
    repo = SentenceRepo(connection)
    first, second, third = saved.sentence_ids
    _finish(repo, first)
    repo.fail(second, expected_version=1, error="失败")
    repo.begin(third, engine="sapi", voice_id=None)

    pending = repo.pending_for_task(task_id)

    assert [row.id for row in pending] == [second]
    assert [row.seq for row in pending] == [2]


def test_pending_for_task_is_ordered_by_seq(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    repo = SentenceRepo(connection)
    assert [row.seq for row in repo.pending_for_task(task_id)] == [1, 2, 3]


def test_progress_counts_every_status(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    repo = SentenceRepo(connection)
    first, second, third = saved.sentence_ids
    _finish(repo, first)
    repo.fail(second, expected_version=1, error="失败")
    repo.skip(third, expected_version=1, audio_path="p.wav", duration_ms=100, error="降级")

    progress = repo.progress(task_id)

    assert (progress.total, progress.done, progress.failed, progress.skipped) == (3, 1, 1, 1)
    assert progress.settled == 2
    assert progress.outstanding == 1
    assert progress.is_settled is False


def test_progress_is_settled_when_only_done_and_skipped_remain(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    """T2.8 的放行判据：全部 ``done/skipped`` ⇒ 可以渲染。"""
    repo = SentenceRepo(connection)
    first, second, third = saved.sentence_ids
    _finish(repo, first)
    _finish(repo, second)
    repo.skip(third, expected_version=1, audio_path="p.wav", duration_ms=100, error="降级")

    progress = repo.progress(task_id)

    assert progress.is_settled is True
    assert progress.to_dict()["ratio"] == 1.0


def test_progress_is_not_settled_while_a_sentence_is_being_synthesized(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    """``synthesizing`` 必然伴随一个活着的租约 ⇒ 那一刻放行渲染会拿到不完整的母带。"""
    repo = SentenceRepo(connection)
    first, second, third = saved.sentence_ids
    _finish(repo, first)
    _finish(repo, second)
    repo.begin(third, engine="sapi", voice_id=None)

    assert repo.progress(task_id).is_settled is False


def test_progress_of_an_empty_task_is_not_settled(connection: sqlite3.Connection) -> None:
    """没有句子 ≠ 配好了：``total == 0`` 时放行渲染会得到一条 0 秒的片子。"""
    task_id = TaskService(connection).create(title="空稿").id
    progress = SentenceRepo(connection).progress(task_id)
    assert (progress.total, progress.is_settled) == (0, False)
    assert progress.to_dict()["ratio"] == 0.0


def test_get_returns_none_for_a_missing_sentence(connection: sqlite3.Connection) -> None:
    assert SentenceRepo(connection).get("nope") is None


# ══════════════════════════════════════════════════════════════════════
# 时间轴回填（T2.7 · §04.2.7 第 5 步）
# ══════════════════════════════════════════════════════════════════════


def _spans(saved: SavedScript, *, version: int = 1) -> list[TimelineSpan]:
    """三句的假时间轴（第 N 句 0.8 秒，间隔 0.2 秒）。"""
    return [
        TimelineSpan(
            sentence_id=sentence_id,
            version=version,
            start_ms=index * 1000,
            end_ms=index * 1000 + 800,
        )
        for index, sentence_id in enumerate(saved.sentence_ids)
    ]


def test_list_for_task_covers_every_status(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    """★ 时间轴读的是**全部**句子：降级句照样占时间，未定局的句子也不能少。

    漏掉 ``skipped`` 的后果不是"少一行"，而是它后面每一句的 ``start_ms``
    都往前错一整句（§04.3.3 不变量 2）。
    """
    repo = SentenceRepo(connection)
    first, second, third = saved.sentence_ids
    _finish(repo, first)
    repo.skip(second, expected_version=1, audio_path="p.wav", duration_ms=100, error="降级")
    repo.begin(third, engine="sapi", voice_id=None)

    rows = repo.list_for_task(task_id)

    assert [row.seq for row in rows] == [1, 2, 3]
    assert [row.tts_status for row in rows] == ["done", "skipped", "synthesizing"]


def test_set_timeline_writes_the_whole_batch(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    repo = SentenceRepo(connection)

    assert repo.set_timeline(_spans(saved)) == 3

    rows = repo.list_for_task(task_id)
    assert [(row.start_ms, row.end_ms) for row in rows] == [(0, 800), (1000, 1800), (2000, 2800)]


def test_set_timeline_reports_the_rows_the_version_guard_rejected(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    """★ 改过的那一句写不进去 ⇒ 返回的行数**少于**传进去的条数。

    调用方拿这个差判"这一版时间轴作废"（整条重算），所以差必须真的算得出来 ——
    少写一行而返回 3，会让一份自相矛盾的时间轴静默落库。
    """
    repo = SentenceRepo(connection)
    assert repo.update_text(saved.sentence_ids[1], text="第二句被改过了") == 2

    assert repo.set_timeline(_spans(saved)) == 2

    rows = repo.list_for_task(task_id)
    assert rows[1].start_ms is None, "文本变过的那一句不许被写上时间"
    assert (rows[0].start_ms, rows[2].start_ms) == (0, 2000)


def test_set_timeline_with_no_spans_changes_nothing(
    connection: sqlite3.Connection, saved: SavedScript, task_id: str
) -> None:
    repo = SentenceRepo(connection)

    assert repo.set_timeline([]) == 0

    assert [row.start_ms for row in repo.list_for_task(task_id)] == [None, None, None]
