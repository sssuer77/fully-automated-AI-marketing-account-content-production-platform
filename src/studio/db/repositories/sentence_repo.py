"""``script_sentences`` 的**句级状态**写入（T2.6 · §03.3.7）。

为什么与 :class:`~studio.db.repositories.script_repo.ScriptRepo` 分开
-------------------------------------------------------------------
两个仓储守的是两条不同的不变式。``ScriptRepo`` 守"稿件与句子必须在同一个事务里
落库"（有稿无句 = 半成品）；本仓储守"**一次合成只能改它自己那一句**"。
合成是并发跑的（voice 池 1–3 个 worker 同时在念不同的句子），合在一起会让
"写稿"与"写合成结果"共用一套写入口 —— 而这两件事连冲突的可能性都不该有。

``version`` 是竞态的唯一闸门
---------------------------
合成一句要几秒。这几秒里用户完全可能在改这一句。所以每次写回都带
``expected_version``：``UPDATE ... WHERE id = ? AND version = ?`` 命中 0 行，
就说明"我读到的文本已经不是现在的文本了" ⇒ **丢弃这次结果**（返回 ``False`` /
``None``），而不是把旧文本的音频贴到新文本上（§03.3.7 续传语义 4）。
调用方据此把这次合成记成"白跑一趟"，而不是失败 —— 它没有失败，只是过期了。

``tts_status ∈ {pending, failed}`` 是唯一的"待办"判据
----------------------------------------------------
它同时是续传的入口（:meth:`SentenceRepo.pending_for_task`）与面板红点的来源。
``synthesizing`` **不算待办**：它只表示"某个 worker 正在念这一句"，进程被杀之后
由 sweeper 回收租约再重排。把"正在跑"混进"该跑"，会让"重跑一次"变成"重跑两遍"。

``done`` 是**可撤销**的（:meth:`SentenceRepo.reopen`）
---------------------------------------------------
"已完成"这个结论依赖两样**盘上的**东西：句子 WAV 与缓存副本。两者都会被清理
（§03.7.5 的 24 小时 GC、缓存 LRU 淘汰），所以 ``done`` 不等于"永远不用再念"。
唯一有权撤销它的是合成处理器 —— 它先确认过这两处都没东西。

时间轴回写（T2.7）为什么也带 ``version``
--------------------------------------
:meth:`SentenceRepo.set_timeline` 写的是"这一句在第几毫秒到第几毫秒"，而它成立的
前提是"这一句的**音频**没换过"。音频换了（改稿重念）时间轴就该全量重算，所以回写
同样带 ``version`` 守卫，并且**整批要么全成、要么整条作废**："跳过改过的那一行、
其余照写"会留下一份自相矛盾的时间轴 —— 第 3 句是新时长，第 4 句起的 ``start_ms``
还是按旧时长算出来的（陷阱 #26）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from studio.db.engine import transaction
from studio.db.models import SentenceRow
from studio.db.repositories.script_repo import SENTENCE_COLUMNS

__all__ = [
    "DONE_STATUS",
    "FAILED_STATUS",
    "PENDING_STATUS",
    "PENDING_STATUSES",
    "SKIPPED_STATUS",
    "SYNTHESIZING_STATUS",
    "SentenceProgress",
    "SentenceRepo",
    "TimelineSpan",
]

#: §03.3.7 续传语义 1：``pending`` / ``failed`` 是**唯一**的"待办"判据
PENDING_STATUSES: Final[tuple[str, ...]] = ("pending", "failed")

PENDING_STATUS: Final[str] = "pending"
DONE_STATUS: Final[str] = "done"
SKIPPED_STATUS: Final[str] = "skipped"
SYNTHESIZING_STATUS: Final[str] = "synthesizing"
FAILED_STATUS: Final[str] = "failed"

#: 「生效稿件」的判据 —— **每一个按 ``task_id`` 读句子的地方都要带上它**。
#:
#: 句子的唯一键是 ``(script_id, seq)``（§03.3.7），而一个任务可以有**多版**稿件：
#: 写稿每重写一次就落一版，旧的置 ``is_active = 0`` 但**行还在**。只按 ``task_id``
#: 读，会把上一版的句子一起读进来 —— ``seq`` 立刻重复（1,1,2,2,…），时间轴报
#: 「句序不连续」把整条链路卡死，而队列还会替**旧稿**再建一轮作业（陷阱 #193）。
_ACTIVE_SCRIPT_SQL: Final[str] = "script_id = (SELECT id FROM scripts WHERE task_id = ? AND is_active = 1)"


@dataclass(frozen=True, slots=True)
class SentenceProgress:
    """一个任务的逐句进度（面板的进度条 / T2.8 的放行判据）。"""

    total: int
    pending: int
    synthesizing: int
    done: int
    skipped: int
    failed: int

    @property
    def settled(self) -> int:
        """已定局（``done + skipped``）—— 分子。"""
        return self.done + self.skipped

    @property
    def outstanding(self) -> int:
        """还没定局（``pending + failed``）—— 分母里真正剩下的部分。"""
        return self.pending + self.failed

    @property
    def is_settled(self) -> bool:
        """全部句子都定局（含 ``skipped`` 的降级句）⇒ T2.8 可以放行渲染。

        ``synthesizing`` **不**参与判定：它既不是"待办"也不是"完成"，
        而它必然伴随一个活着的租约 —— 那一刻放行渲染会拿到不完整的母带。
        所以判据是"没有任何 pending/failed/synthesizing"。
        """
        return self.total > 0 and self.outstanding == 0 and self.synthesizing == 0

    def to_dict(self) -> dict[str, int | float]:
        return {
            "total": self.total,
            "pending": self.pending,
            "synthesizing": self.synthesizing,
            "done": self.done,
            "skipped": self.skipped,
            "failed": self.failed,
            "settled": self.settled,
            "ratio": round(self.settled / self.total, 4) if self.total else 0.0,
        }


@dataclass(frozen=True, slots=True)
class TimelineSpan:
    """一句的时间区间 + 它所属的那一版文本（``version`` 就是回写的守卫）。

    ``version`` 放进这里、而不是回写时现查：守卫要守的正是"调用方读到的那一版"，
    现查拿到的是**现在**那一版 —— 两者相等才真的说明"从读到写这一句没被改过"。
    """

    sentence_id: str
    version: int
    start_ms: int
    end_ms: int


class SentenceRepo:
    """``script_sentences`` 的句级状态读写（合成结果与时间轴回填都走这里）。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, sentence_id: str) -> SentenceRow | None:
        row = self._connection.execute(
            f"SELECT {SENTENCE_COLUMNS} FROM script_sentences WHERE id = ?", (sentence_id,)
        ).fetchone()
        return None if row is None else SentenceRow.from_row(row)

    def get_by_seq(self, task_id: str, seq: int) -> SentenceRow | None:
        """按 ``(task_id, seq)`` 找一句（试听端点的入口，T2.9）。

        为什么不复用 :meth:`get`：试听拿到的键是 ``voice/<task_id>/s00N.wav``
        —— 它说的是"第几句"，不是"哪一行"。唯一约束落在 ``(script_id, seq)``
        上，所以 ``seq`` 要配**生效稿件**才有唯一答案（陷阱 #193）：不配的话，
        重写过稿件的任务会同时命中好几行，``fetchone()`` 取到哪一版全看运气
        —— 试听念出来的可能是**上一版**的台词。
        """
        row = self._connection.execute(
            f"SELECT {SENTENCE_COLUMNS} FROM script_sentences "
            f"WHERE task_id = ? AND {_ACTIVE_SCRIPT_SQL} AND seq = ?",
            (task_id, task_id, seq),
        ).fetchone()
        return None if row is None else SentenceRow.from_row(row)

    def pending_for_task(self, task_id: str) -> list[SentenceRow]:
        """该任务里**该合成**的句子（按 ``seq``）—— 断点续跑的入口。"""
        placeholders = ", ".join("?" for _ in PENDING_STATUSES)
        rows = self._connection.execute(
            f"SELECT {SENTENCE_COLUMNS} FROM script_sentences "
            f"WHERE task_id = ? AND {_ACTIVE_SCRIPT_SQL} "
            f"AND tts_status IN ({placeholders}) ORDER BY seq",
            (task_id, task_id, *PENDING_STATUSES),
        ).fetchall()
        return [SentenceRow.from_row(row) for row in rows]

    def list_for_task(self, task_id: str) -> list[SentenceRow]:
        """该任务的**全部**句子（按 ``seq``）—— 时间轴（T2.7）按它全量重算。

        与 :meth:`pending_for_task` 只差一处：那个按 ``tts_status`` 过滤，这个不。
        时间轴要回答的是"这条片子从头到尾有哪几句"，而降级成静音的 ``skipped``
        句照样占时间（§04.3.3 不变量 2）—— 漏掉它，后面每一句的 ``start_ms``
        都会往前错一整句。
        """
        rows = self._connection.execute(
            f"SELECT {SENTENCE_COLUMNS} FROM script_sentences "
            f"WHERE task_id = ? AND {_ACTIVE_SCRIPT_SQL} ORDER BY seq",
            (task_id, task_id),
        ).fetchall()
        return [SentenceRow.from_row(row) for row in rows]

    def progress(self, task_id: str) -> SentenceProgress:
        """逐句进度（一条 ``GROUP BY`` 拿全，不把句子行读进内存再数）。"""
        counts = dict.fromkeys(("pending", "synthesizing", "done", "skipped", "failed"), 0)
        rows = self._connection.execute(
            "SELECT tts_status, COUNT(*) FROM script_sentences "
            f"WHERE task_id = ? AND {_ACTIVE_SCRIPT_SQL} GROUP BY tts_status",
            (task_id, task_id),
        ).fetchall()
        for row in rows:
            status = str(row[0])
            if status in counts:
                counts[status] = int(row[1])
        return SentenceProgress(total=sum(counts.values()), **counts)

    # ── 写（都带 version 守卫）──────────────────────────────────────────

    def begin(self, sentence_id: str, *, engine: str, voice_id: str | None) -> bool:
        """置 ``synthesizing`` 并记下这次用的是哪个引擎 / 音色。

        允许从 ``synthesizing`` 再进 ``synthesizing``：上一次进程被杀会把它留在
        这个状态（没人会把它改回去），而"重做"是正常路径，不该被状态挡住。
        """
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                "UPDATE script_sentences SET tts_status = ?, tts_engine = ?, tts_voice_id = ? "
                "WHERE id = ? AND tts_status IN ('pending', 'failed', 'synthesizing')",
                (SYNTHESIZING_STATUS, engine, voice_id, sentence_id),
            )
            return cursor.rowcount > 0

    def reopen(self, sentence_id: str, *, expected_version: int) -> bool:
        """把一句 ``done`` 退回 ``pending``：产物与缓存都没了 ⇒ 只能重念。

        ``done`` 的承诺是"这一句的音频已经存在"。产物被 §03.7.5 的 24 小时 GC 删掉、
        缓存条目又被 LRU 淘汰之后，这个承诺不再成立 —— 这时"相信库里的 done"只会交出
        一个不存在的路径（成片里那一句没声音，而且没有任何地方报错）。所以把状态**如实**
        退回待办。

        **不**动 ``version``：那道闸门守的是"文本有没有被改过"，而这里文本一个字没变。
        跟着 ``+1`` 会把在飞的合成结果一并作废 —— 那是编辑该做的事，不是清理该做的事。
        ``tts_hash`` 清空：它指向的缓存条目已经不存在了（留着会让下一个 worker 又去查一次
        注定查不到的缓存，并且让面板显示一个假的"可复用"）。
        """
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                "UPDATE script_sentences SET tts_status = ?, tts_hash = NULL, tts_error = NULL "
                "WHERE id = ? AND version = ? AND tts_status = ?",
                (PENDING_STATUS, sentence_id, expected_version, DONE_STATUS),
            )
            return cursor.rowcount > 0

    def invalidate(self, sentence_id: str, *, expected_version: int) -> bool:
        """人工「重配」：把这一句退回待办，不论它现在是 ``done`` 还是 ``skipped``（T2.9）。

        与 :meth:`reopen` 的分工
        ------------------------
        ``reopen`` 是**如实纠正**：产物与缓存都没了，"已完成"这个结论不再成立，
        于是退回待办（不是谁要求的，是事实变了）。
        ``invalidate`` 是**用户要求重来**：产物好端端地在那儿，只是人不满意。

        两者都不动 ``version`` —— 那道闸门守的是"文本有没有被改过"，这两件事都没改文本。
        跟着 ``+1`` 会把在飞的合成结果一并作废，那是编辑该做的事。

        ``tts_hash`` 清空、``tts_attempts`` 归零，理由各一条：

        - ``tts_hash`` 指向的缓存条目**还是好的**（清它不是为了省事），而是因为
          ``finish`` 会重新写它 —— 留着旧值只会让"这一句现在配的是哪一版音频"
          在重合成完成前有一个错的答案。
        - ``tts_attempts`` **必须归零**：不归零的话，上一轮已经撞过
          ``DEGRADE_AFTER_ATTEMPTS``（3 次）的降级句重配时**再失败一次就直接降级**
          —— 用户点"重配"的本意是"再给它一次机会"，结果只给了一次。
          这是一处**看不见**的错：不归零也能跑通，只是失败得比预期早得多。

        ``synthesizing`` 不在可退回之列：那一句正被某个 worker 攥在手里，在它眼皮底下
        把状态改成 ``pending`` 会让两个 worker 往同一个文件里写。调用方应当等它跑完
        （面板上那一条显示"进行中"，本来也不该给"重配"按钮）。
        """
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                "UPDATE script_sentences SET tts_status = ?, tts_hash = NULL, tts_error = NULL, "
                "tts_attempts = 0 WHERE id = ? AND version = ? "
                "AND tts_status IN (?, ?, ?, ?)",
                (
                    PENDING_STATUS,
                    sentence_id,
                    expected_version,
                    PENDING_STATUS,
                    FAILED_STATUS,
                    DONE_STATUS,
                    SKIPPED_STATUS,
                ),
            )
            return cursor.rowcount > 0

    def finish(
        self,
        sentence_id: str,
        *,
        expected_version: int,
        audio_path: str,
        duration_ms: int,
        sample_rate: int | None,
        tts_hash: str,
        engine: str,
        voice_id: str | None,
    ) -> bool:
        """写回一次成功的合成；``version`` 变过 ⇒ ``False``（**丢弃结果**）。

        ``tts_error`` 一并清空：上一轮的失败原因留在这一列上，会让面板把一句
        **已经念好了**的句子显示成"上次失败过"。
        """
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                "UPDATE script_sentences SET "
                "tts_status = ?, tts_audio_path = ?, tts_duration_ms = ?, tts_sample_rate = ?, "
                "tts_hash = ?, tts_engine = ?, tts_voice_id = ?, tts_error = NULL "
                "WHERE id = ? AND version = ?",
                (
                    DONE_STATUS,
                    audio_path,
                    duration_ms,
                    sample_rate,
                    tts_hash,
                    engine,
                    voice_id,
                    sentence_id,
                    expected_version,
                ),
            )
            return cursor.rowcount > 0

    def skip(
        self,
        sentence_id: str,
        *,
        expected_version: int,
        audio_path: str,
        duration_ms: int,
        error: str,
    ) -> bool:
        """降级落 ``skipped``：**等长静音占位 + 字幕保留**（§04.3.3）。

        它写的是**成功**的一种（产物是一段静音），所以带 ``audio_path`` 与
        ``duration_ms`` —— 时间轴（T2.7）照样能算，字幕照样在，只是这一句没声音。
        与 :meth:`finish` 的区别只有 ``tts_status`` 与那条 ``tts_error``：留痕
        "这句是降级来的"，面板与报告才说得出"这条片子有 2 句没念出来"。
        """
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                "UPDATE script_sentences SET "
                "tts_status = ?, tts_audio_path = ?, tts_duration_ms = ?, tts_error = ? "
                "WHERE id = ? AND version = ?",
                (SKIPPED_STATUS, audio_path, duration_ms, error, sentence_id, expected_version),
            )
            return cursor.rowcount > 0

    def fail(self, sentence_id: str, *, expected_version: int, error: str) -> int | None:
        """记一次失败并让 ``tts_attempts + 1``，返回**累加后**的次数。

        返回 ``None`` 表示 ``version`` 变过 ⇒ 这次失败属于一份**已经不存在的稿子**，
        不该记到新稿子头上（否则改一句会让这一句的"失败次数"凭空多一次，
        三轮之后它就被降级成静音了 —— 而它一次都没被念过）。

        计数与读取在同一个事务里：分开写会让两个并发 worker 读到同一个数，
        于是"第 3 次失败 ⇒ 降级"这条线被两个单元各自判一次。
        """
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                "UPDATE script_sentences SET tts_status = ?, tts_error = ?, "
                "tts_attempts = tts_attempts + 1 WHERE id = ? AND version = ?",
                (FAILED_STATUS, error, sentence_id, expected_version),
            )
            if cursor.rowcount == 0:
                return None
            row = self._connection.execute(
                "SELECT tts_attempts FROM script_sentences WHERE id = ?", (sentence_id,)
            ).fetchone()
            return None if row is None else int(row[0])

    # ── 时间轴回填（T2.7 · §04.2.7 第 5 步）────────────────────────────

    def set_timeline(self, spans: Sequence[TimelineSpan]) -> int:
        """批量回写 ``start_ms`` / ``end_ms``（一个事务，逐行带 ``version`` 守卫）。

        返回**真正写进去的行数**；调用方拿它和 ``len(spans)`` 比 —— 少了就说明有句子
        在"读句子"与"回写"之间被改过，那一行的 ``version`` 已经不是我们读到的那一个，
        于是时间轴**整条**作废（见模块 docstring，陷阱 #26）。

        为什么不用 ``executemany``：它拿不到每一行的 ``rowcount``，而"哪几行没写进去"
        正是这个方法的返回值要回答的东西（一次 SQL 往返省不下多少，丢掉这个信号
        却会让"改稿与回写撞车"变成一件静默发生的事）。
        """
        if not spans:
            return 0
        updated = 0
        with transaction(self._connection, immediate=True):
            for span in spans:
                cursor = self._connection.execute(
                    "UPDATE script_sentences SET start_ms = ?, end_ms = ? WHERE id = ? AND version = ?",
                    (span.start_ms, span.end_ms, span.sentence_id, span.version),
                )
                updated += cursor.rowcount
        return updated

    def update_text(self, sentence_id: str, *, text: str, subtitle: str | None = None) -> int | None:
        """改一句的文本 ⇒ ``version + 1`` + 置 ``pending`` + 清 ``tts_hash``（§03.3.7.3）。

        只失效**这一句**：其余句子的 ``tts_hash`` 原封不动，重跑时它们命中缓存
        （引擎调用 0 次），只有这一句真的重合成 —— 这正是句级缓存的全部意义。

        ``tts_audio_path`` **故意留着**：路径由 ``seq`` 决定（§04.3.3 不变量 1），
        重合成会原地覆盖它。清空它反而让面板在"待办"期间显示不出"这一句上次的
        音频在哪"，而那份音频马上就会被新的替掉。

        返回新的 ``version``；句子不存在 ⇒ ``None``。
        """
        with transaction(self._connection, immediate=True):
            cursor = self._connection.execute(
                "UPDATE script_sentences SET text = ?, subtitle = COALESCE(?, subtitle), "
                "version = version + 1, tts_status = ?, tts_hash = NULL, tts_error = NULL "
                "WHERE id = ?",
                (text, subtitle, "pending", sentence_id),
            )
            if cursor.rowcount == 0:
                return None
            row = self._connection.execute(
                "SELECT version FROM script_sentences WHERE id = ?", (sentence_id,)
            ).fetchone()
            return None if row is None else int(row[0])
