"""``scripts`` / ``script_sentences`` 仓储（T1.10 · §03.3.7）。

一次落库 = **一个事务**（"有稿无句"是不允许存在的半成品）
------------------------------------------------------
``scripts`` 与 ``script_sentences`` 必须在同一个 ``BEGIN IMMEDIATE`` 里写完：
只要出现"稿件行在、句子行不在"，下游（配音池按句认领、面板逐句显示）就会
读到一份**看起来正常但永远配不出音**的稿子 —— 这种半成品比"整批失败"难查得多
（DoD 5：不允许静默的半成品）。所以两表的 INSERT 由本模块**一起**持有。

版本与 ``is_active``
--------------------
``UNIQUE (task_id, version)`` 管版本号，``ux_scripts_active``（部分唯一索引）管
"同一任务只能有一版生效"。顺序是死的：**先**把旧版置 ``is_active=0``，**再**插入
新版 —— 反过来的话部分唯一索引会当场拒绝。

为什么 T2 的 ``SentenceRepo`` 还不存在
--------------------------------------
句级**状态更新**（``tts_status`` / 音频路径 / 时间轴回填）到 T2.6 才出现；
此刻先把它塞进来，只会得到一个没有调用方的类。落库这一侧的 INSERT 由本仓储
统一持有（T1.10 裁定 79）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.db.models import ScriptRow, SentenceRow

__all__ = ["SavedScript", "ScriptRepo"]

_SCRIPT_COLUMNS: Final[str] = (
    "id, task_id, version, is_active, title, hook, body_md, cta, word_count, est_duration_ms, "
    "speaker_ratio_json, outline_json, grade, score_total, score_rule, score_llm, revision_round, "
    "editor_notes_json, target_chars, llm_model, prompt_version, review_json, created_at"
)

_SENTENCE_COLUMNS: Final[str] = (
    "id, task_id, script_id, seq, text_raw, text, tts_text, subtitle, speaker, emotion, speed, "
    "pause_after_ms, emphasis_json, tts_status, tts_engine, tts_voice_id, tts_audio_path, "
    "tts_duration_ms, tts_sample_rate, tts_hash, tts_attempts, tts_error, start_ms, end_ms, "
    "version, created_at, updated_at"
)

_INSERT_SCRIPT: Final[str] = """
INSERT INTO scripts(
    id, task_id, version, is_active, title, hook, body_md, cta, word_count, est_duration_ms,
    speaker_ratio_json, outline_json, target_chars, llm_model, prompt_version,
    revision_round, editor_notes_json
) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_SENTENCE: Final[str] = """
INSERT INTO script_sentences(
    id, task_id, script_id, seq, text_raw, text, speaker, emotion, pause_after_ms
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True, slots=True)
class SavedScript:
    """一次 ``save_draft`` 的结果（``sentence_ids`` 与 ``seq`` 同序）。"""

    script_id: str
    version: int
    sentence_ids: tuple[str, ...]

    @property
    def sentence_count(self) -> int:
        return len(self.sentence_ids)


class ScriptRepo:
    """``scripts`` + ``script_sentences`` 的唯一读写入口（写入侧）。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 写 ──────────────────────────────────────────────────────────────

    def save_draft(
        self,
        *,
        task_id: str,
        title: str | None,
        hook: str | None,
        body_md: str,
        cta: str | None,
        word_count: int,
        est_duration_ms: int | None,
        speaker_ratio: Mapping[str, float],
        outline: Mapping[str, Any],
        sentences: Sequence[Mapping[str, Any]],
        target_chars: int = 700,
        llm_model: str | None = None,
        prompt_version: str | None = None,
        revision_round: int = 0,
        editor_notes: Sequence[str] = (),
    ) -> SavedScript:
        """把一版稿件与它的**全部句子**写进同一个事务 ⇒ 新版本生效。

        ``sentences`` 是已翻译好的纯数据：``{seq, text_raw, text, speaker, emotion,
        pause_after_ms}``。空句表 ⇒ :class:`ValueError`（不允许写"有稿无句"）。

        ``revision_round`` 是**改稿轮次**（首稿 0，每改一轮 +1），由改稿服务传入；
        ``editor_notes`` 是这一轮"改了什么"（原文 §2.2⑥ 要求每轮留痕）。
        """
        if not sentences:
            raise ValueError("稿件的句子表不得为空（有稿无句＝半成品，禁止入库）")
        script_id = new_ulid()
        sentence_ids = tuple(new_ulid() for _ in sentences)
        params = [
            (
                sentence_id,
                task_id,
                script_id,
                int(item["seq"]),
                str(item["text_raw"]),
                str(item["text"]),
                str(item["speaker"]),
                str(item.get("emotion", "neutral")),
                int(item.get("pause_after_ms", 200)),
            )
            for sentence_id, item in zip(sentence_ids, sentences, strict=True)
        ]
        with transaction(self._connection, immediate=True):
            version = self._next_version(task_id)
            # 顺序是死的：先让旧版失效，再插新版（部分唯一索引 ux_scripts_active）
            self._connection.execute(
                "UPDATE scripts SET is_active = 0 WHERE task_id = ? AND is_active = 1",
                (task_id,),
            )
            self._connection.execute(
                _INSERT_SCRIPT,
                (
                    script_id,
                    task_id,
                    version,
                    title,
                    hook,
                    body_md,
                    cta,
                    word_count,
                    est_duration_ms,
                    json.dumps(dict(speaker_ratio), ensure_ascii=False),
                    json.dumps(dict(outline), ensure_ascii=False),
                    target_chars,
                    llm_model,
                    prompt_version,
                    revision_round,
                    json.dumps(list(editor_notes), ensure_ascii=False),
                ),
            )
            self._connection.executemany(_INSERT_SENTENCE, params)
        return SavedScript(script_id=script_id, version=version, sentence_ids=sentence_ids)

    def _next_version(self, task_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM scripts WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row[0]) if row is not None else 1

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, script_id: str) -> ScriptRow | None:
        row = self._connection.execute(
            f"SELECT {_SCRIPT_COLUMNS} FROM scripts WHERE id = ?", (script_id,)
        ).fetchone()
        return None if row is None else ScriptRow.from_row(row)

    def get_active(self, task_id: str) -> ScriptRow | None:
        """当前生效版本（``ux_scripts_active`` 保证最多一条）。"""
        row = self._connection.execute(
            f"SELECT {_SCRIPT_COLUMNS} FROM scripts WHERE task_id = ? AND is_active = 1",
            (task_id,),
        ).fetchone()
        return None if row is None else ScriptRow.from_row(row)

    def get_by_version(self, task_id: str, version: int) -> ScriptRow | None:
        """按版本号取（版本对照的读取口径；不存在 ⇒ ``None``）。"""
        row = self._connection.execute(
            f"SELECT {_SCRIPT_COLUMNS} FROM scripts WHERE task_id = ? AND version = ?",
            (task_id, version),
        ).fetchone()
        return None if row is None else ScriptRow.from_row(row)

    def list_versions(self, task_id: str) -> list[ScriptRow]:
        rows = self._connection.execute(
            f"SELECT {_SCRIPT_COLUMNS} FROM scripts WHERE task_id = ? ORDER BY version DESC",
            (task_id,),
        ).fetchall()
        return [ScriptRow.from_row(row) for row in rows]

    def list_sentences(self, script_id: str) -> list[SentenceRow]:
        """按 ``seq`` 顺序读回全部句子（断点续传的读取口径）。"""
        rows = self._connection.execute(
            f"SELECT {_SENTENCE_COLUMNS} FROM script_sentences WHERE script_id = ? ORDER BY seq",
            (script_id,),
        ).fetchall()
        return [SentenceRow.from_row(row) for row in rows]

    def count_sentences(self, script_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM script_sentences WHERE script_id = ?", (script_id,)
        ).fetchone()
        return int(row[0]) if row is not None else 0
