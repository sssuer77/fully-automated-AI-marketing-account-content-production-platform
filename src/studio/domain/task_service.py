"""任务状态的**唯一写入口**（§03.5.1 · T1.4）。

架构约束
--------
``tasks`` 表**只允许本模块写**。任何其他模块想改任务状态或进度，都必须调用
:class:`TaskService`，否则：

- 状态机守卫被绕过（非法迁移静默落库，后续所有守卫失效）；
- ``task_events`` 审计断链（WebUI 回放、排障、DoD 5"禁止静默失败"全部失效）。

``tests/contract/test_no_direct_task_write.py`` 用静态扫描兜底这条约束。

事务纪律（R10）
--------------
``transition`` 全程在**一个** ``BEGIN IMMEDIATE`` 事务里：读 → 校验 → 改 → 记事件 →
写日志。事务内**只有 SQL**（无文件/网络 I/O），目标 < 20ms。

广播顺序（§04.5.2）
------------------
**先落库再广播**。本模块只负责落库，并返回 :class:`TransitionResult`；
广播由编排层在 ``with`` 块**退出之后**发起（T1.7 的 ``/ws/ui``）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.ids import new_ulid
from studio.db.engine import transaction
from studio.domain.enums import TaskKind, TaskPool, TaskStatus
from studio.domain.errors import ConcurrentModification, TaskNotFound
from studio.domain.models import QualityReport, TaskEventRead, TaskPayload, TaskRead
from studio.domain.state_machine import RETRY_FROM_WHITELIST, RETRY_SOURCES, assert_allowed

__all__ = ["TaskService", "TransitionResult"]

_SELECT_TASK: Final[str] = "SELECT * FROM tasks WHERE id = ?"
_SELECT_TASK_BY_KEY: Final[str] = "SELECT * FROM tasks WHERE idempotency_key = ?"
_SELECT_EVENT: Final[str] = "SELECT * FROM task_events WHERE id = ?"

_INSERT_TASK: Final[str] = """
INSERT INTO tasks(
    id, kind, title, topic, status, pool, priority,
    source_direction_id, source_topic_id, payload_json, idempotency_key
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

#: 目标状态 → ``tasks.pool``。
#: T1.4 施工裁定：``tasks.pool`` 是给 WebUI 过滤用的**冗余标记**，
#: 既然 ``tasks`` 只有本模块能写，它就得在这里维护，不能指望调用方顺手改。
_POOL_FOR_STATUS: Final[dict[TaskStatus, TaskPool]] = {
    TaskStatus.PENDING: TaskPool.DRAFT,
    TaskStatus.DRAFTING: TaskPool.DRAFT,
    TaskStatus.REVIEWING: TaskPool.DRAFT,
    TaskStatus.EDITING: TaskPool.DRAFT,
    TaskStatus.AWAITING_APPROVAL: TaskPool.DRAFT,
    TaskStatus.QUEUED_VOICE: TaskPool.VOICE,
    TaskStatus.VOICING: TaskPool.VOICE,
    TaskStatus.QUEUED_RENDER: TaskPool.RENDER,
    TaskStatus.RENDERING: TaskPool.RENDER,
    TaskStatus.COMPLETED: TaskPool.NONE,
    TaskStatus.PUBLISHING: TaskPool.PUBLISH,
    TaskStatus.PUBLISHED: TaskPool.PUBLISH,
    TaskStatus.FAILED: TaskPool.NONE,
    TaskStatus.MANUAL_POOL: TaskPool.NONE,
    TaskStatus.DISCARDED: TaskPool.NONE,
    TaskStatus.CANCELED: TaskPool.NONE,
}

#: 到达这些状态 ⇒ 本轮结束，写 ``finished_at``。
_FINISHED_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.PUBLISHED,
        TaskStatus.DISCARDED,
        TaskStatus.CANCELED,
    }
)

#: 进入这些状态 ⇒ 写 ``error_code`` / ``error_message``。
_ERROR_STATUSES: Final[frozenset[TaskStatus]] = frozenset({TaskStatus.FAILED, TaskStatus.MANUAL_POOL})


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """一次成功迁移的结果（广播层只认这个对象）。"""

    task: TaskRead
    event: TaskEventRead


class TaskService:
    """``tasks`` 表的唯一写入口。

    :param connection: 已应用 PRAGMA 的 sqlite3 连接（``db.engine.connect``）
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # ── 建任务 ──────────────────────────────────────────────────────────

    def create(
        self,
        *,
        title: str,
        kind: TaskKind = TaskKind.VIDEO,
        topic: str | None = None,
        source_topic_id: str | None = None,
        source_direction_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        priority: int = 100,
        idempotency_key: str | None = None,
        actor: str = "system",
        reason: str | None = None,
    ) -> TaskRead:
        """新建任务（``pending`` ⇒ ``pool='draft'``），**同事务**写创建事件与日志。

        ``idempotency_key`` 命中已有行 ⇒ **原样返回那一行，不写任何东西**
        （§4.7：勾选入队要能被重复点击而不产生第二个任务）。幂等的是"结果"，
        不是"又记了一笔" —— 命中时不会多出第二条 ``created`` 事件。
        """
        if not title.strip():
            raise ValueError("任务标题不能为空")
        if not actor.strip():
            raise ValueError("actor 不能为空：task_events 必须能追责到主体")
        with transaction(self._connection, immediate=True):
            if idempotency_key is not None:
                existing = self._connection.execute(_SELECT_TASK_BY_KEY, (idempotency_key,)).fetchone()
                if existing is not None:
                    return TaskRead.from_row(existing)
            task_id = new_ulid()
            self._connection.execute(
                _INSERT_TASK,
                (
                    task_id,
                    kind.value,
                    title,
                    topic,
                    TaskStatus.PENDING.value,
                    TaskPool.DRAFT.value,
                    priority,
                    source_direction_id,
                    source_topic_id,
                    json.dumps(dict(payload or {}), ensure_ascii=False),
                    idempotency_key,
                ),
            )
            self._insert_event(
                task_id,
                source=None,
                to=TaskStatus.PENDING,
                actor=actor,
                reason=reason or "created",
                detail={"title": title, "kind": kind.value, "source_topic_id": source_topic_id},
            )
            self._emit_log(task_id, source=None, to=TaskStatus.PENDING, actor=actor, reason=reason)
            return TaskRead.from_row(self._row(task_id))

    # ── 读 ──────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> TaskRead:
        """读单个任务；不存在 ⇒ :class:`TaskNotFound`。"""
        return TaskRead.from_row(self._row(task_id))

    def find_by_idempotency_key(self, idempotency_key: str) -> TaskRead | None:
        """按幂等键查任务；没有 ⇒ ``None``（**不抛异常**）。

        调用方拿它判"这次点击是新任务还是复用"（T1.10 裁定 87）——
        ``create()`` 命中幂等键时会原样返回旧行，光看返回值分不出新旧。
        """
        row = self._connection.execute(_SELECT_TASK_BY_KEY, (idempotency_key,)).fetchone()
        return None if row is None else TaskRead.from_row(row)

    def _row(self, task_id: str) -> sqlite3.Row:
        row = self._connection.execute(_SELECT_TASK, (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(
                f"任务不存在：{task_id}",
                context={"task_id": task_id},
                remediation="确认 task_id；被 GC 掉的任务可从 data/backups/ 还原",
            )
        return row  # type: ignore[no-any-return]

    # ── 状态迁移 ────────────────────────────────────────────────────────

    def transition(
        self,
        task_id: str,
        to: TaskStatus,
        *,
        actor: str,
        reason: str | None = None,
        expected_version: int | None = None,
        retry_from: TaskStatus | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        approved_by: str | None = None,
        bump_revision: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """★ 任务状态的唯一写入口。

        1) 事务内读当前行（``BEGIN IMMEDIATE`` 已取写锁 ⇒ 读到的一定是最新值）；
        2) ``expected_version`` 不匹配 ⇒ :class:`ConcurrentModification`（重试而非覆盖）；
        3) ``to`` 不在 ``ALLOWED_TRANSITIONS[status]``（含 ``retry_from`` 动态边）⇒
           :class:`IllegalTransition`；
        4) 进入 ``failed`` ⇒ 记 ``last_healthy_status`` + ``retry_from`` + ``attempt_count+1``；
        5) ``UPDATE ... WHERE id=? AND version=?``，0 行 ⇒ :class:`ConcurrentModification`；
        6) 同事务写 ``task_events`` + ``system_logs``，返回 :class:`TransitionResult`。

        :param actor: 追责主体，形如 ``'user'`` / ``'system'`` / ``'worker:voice#1'``
        :param retry_from: 显式指定断点落点（一般留 ``None``，用行上的 ``retry_from``）
        :param approved_by: 非空 ⇒ 记 ``approved_at`` + ``approved_by``（确认闸放行署名）。
            取值 ``'user'`` / ``'auto_approve_A'`` / ``'auto_approve_AB'``
        :param bump_revision: ``True`` ⇒ ``revision_round + 1``（退回改稿）。用**增量**而不是
            让调用方传目标值：并发下"读到的旧值 + 1"会丢掉另一方的改动（T1.11 裁定 91）
        """
        if not actor.strip():
            raise ValueError("actor 不能为空：task_events 必须能追责到主体")
        if expected_version is not None and expected_version < 1:
            raise ValueError("expected_version 必须 ≥ 1（tasks.version 从 1 起）")

        with transaction(self._connection, immediate=True):
            row = self._row(task_id)
            current = TaskRead.from_row(row)

            if expected_version is not None and current.version != expected_version:
                raise ConcurrentModification(
                    f"任务 {task_id} 已被并发修改：期望 version={expected_version}，"
                    f"实际 version={current.version}",
                    context={
                        "task_id": task_id,
                        "expected_version": expected_version,
                        "actual_version": current.version,
                        "current_status": current.status.value,
                    },
                    remediation="重读任务后重试；不要覆盖写（会丢掉并发方的事件与产物）",
                )

            source = current.status
            resume_target = retry_from if retry_from is not None else current.retry_from
            assert_allowed(source, to, actor=actor, retry_from=resume_target)

            updates = self._build_updates(
                current,
                source=source,
                to=to,
                error_code=error_code,
                error_message=error_message,
                approved_by=approved_by,
                bump_revision=bump_revision,
            )
            self._update(task_id, current.version, updates)
            event_id = self._insert_event(
                task_id,
                source=source,
                to=to,
                actor=actor,
                reason=reason,
                detail=detail,
            )
            self._emit_log(
                task_id,
                source=source,
                to=to,
                actor=actor,
                reason=reason,
            )
            updated = TaskRead.from_row(self._row(task_id))
            event = TaskEventRead.from_row(self._event_row(event_id))

        return TransitionResult(task=updated, event=event)

    def _build_updates(
        self,
        current: TaskRead,
        *,
        source: TaskStatus,
        to: TaskStatus,
        error_code: str | None,
        error_message: str | None,
        approved_by: str | None,
        bump_revision: bool,
    ) -> dict[str, Any]:
        """算出本次迁移要写的列（``updated_at`` 交给 ``trg_tasks_touch`` 触发器）。"""
        updates: dict[str, Any] = {
            "status": to.value,
            "pool": _POOL_FOR_STATUS[to].value,
        }

        if to is TaskStatus.FAILED:
            updates["last_healthy_status"] = source.value
            updates["attempt_count"] = current.attempt_count + 1
            if source in RETRY_FROM_WHITELIST:
                # 断点落点 = 失败前那个"确实有活可干"的状态（§03.3.x 队列表）
                updates["retry_from"] = source.value
        if to is TaskStatus.PENDING:
            # 人工捞回 ⇒ 重新开始，清掉上一轮的收尾时间
            updates["finished_at"] = None
        if to in _ERROR_STATUSES:
            updates["error_code"] = error_code
            updates["error_message"] = error_message
        elif source in RETRY_SOURCES:
            # 离开失败态 ⇒ 上一次的错误归零（历史留在 task_events 里）
            updates["error_code"] = None
            updates["error_message"] = None
        if approved_by is not None:
            # 放行时刻与署名：确认闸最该查得到的两样（谁放的、什么时候）
            updates["approved_at"] = now_iso()
            updates["approved_by"] = approved_by
        if bump_revision:
            updates["revision_round"] = current.revision_round + 1
        if current.started_at is None and to is not TaskStatus.PENDING:
            updates["started_at"] = now_iso()
        if to in _FINISHED_STATUSES:
            updates["finished_at"] = now_iso()
        return updates

    def _update(self, task_id: str, version: int, updates: dict[str, Any]) -> None:
        columns = ", ".join(f"{name} = ?" for name in updates)
        cursor = self._connection.execute(
            f"UPDATE tasks SET version = version + 1, {columns} WHERE id = ? AND version = ?",
            [*updates.values(), task_id, version],
        )
        if cursor.rowcount == 0:
            raise ConcurrentModification(
                f"任务 {task_id} 的乐观锁在写入瞬间失效（version={version}）",
                context={"task_id": task_id, "version": version},
                remediation="重读任务后重试",
            )

    def _insert_event(
        self,
        task_id: str,
        *,
        source: TaskStatus | None,
        to: TaskStatus,
        actor: str,
        reason: str | None,
        detail: dict[str, Any] | None,
    ) -> int:
        cursor = self._connection.execute(
            "INSERT INTO task_events(task_id, from_status, to_status, actor, reason, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                source.value if source is not None else None,
                to.value,
                actor,
                reason,
                json.dumps(detail or {}, ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid or 0)

    def _emit_log(
        self,
        task_id: str,
        *,
        source: TaskStatus | None,
        to: TaskStatus,
        actor: str,
        reason: str | None,
    ) -> None:
        """状态迁移的 ``system_logs`` 留痕（与事件**同事务**）。

        ⚠️ T1.7 会落地 ``log_service.emit()``（含折叠 / 广播）；届时本方法改为
        委托给它。现在先内联，保证"先落库再广播"的顺序从第一天就成立。
        """
        level = "error" if to is TaskStatus.FAILED else "warn" if to is TaskStatus.MANUAL_POOL else "info"
        message = "created" if source is None else f"{source.value} → {to.value}"
        if reason and source is not None:
            message = f"{message}（{reason}）"
        self._connection.execute(
            "INSERT INTO system_logs(level, source, task_id, stage, message, payload_json, worker_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                level,
                "domain.task",
                task_id,
                to.value,
                message,
                json.dumps(
                    {
                        "from": source.value if source is not None else None,
                        "to": to.value,
                        "reason": reason,
                        "actor": actor,
                    },
                    ensure_ascii=False,
                ),
                actor,
            ),
        )

    def _event_row(self, event_id: int) -> sqlite3.Row:
        row = self._connection.execute(_SELECT_EVENT, (event_id,)).fetchone()
        if row is None:  # pragma: no cover — 同事务刚插入，不可能读不到
            raise TaskNotFound(
                f"task_events 写入后读回失败：id={event_id}",
                context={"event_id": event_id},
            )
        return row  # type: ignore[no-any-return]

    # ── 非状态字段（进度）───────────────────────────────────────────────

    def update_progress(
        self,
        task_id: str,
        *,
        progress: float,
        stage_detail: str | None = None,
    ) -> TaskRead:
        """更新进度条 / 阶段文案。

        **不动 ``version``**：``version`` 是状态迁移的乐观锁，进度是高频覆盖写
        （"voice: 12/27 句"），若也 bump 会让所有持有 version 的调用方白白重试。
        """
        if not 0.0 <= progress <= 1.0:
            raise ValueError(f"progress 必须在 [0, 1]：{progress}")
        with transaction(self._connection, immediate=True):
            self._row(task_id)  # 不存在 ⇒ TaskNotFound
            self._connection.execute(
                "UPDATE tasks SET progress = ?, stage_detail = ? WHERE id = ?",
                (progress, stage_detail, task_id),
            )
            return TaskRead.from_row(self._row(task_id))

    def set_voice_map(self, task_id: str, voice_map: Mapping[str, str]) -> TaskRead:
        """改 ``tasks.payload_json.voice_map``（T2.9 的任务级换音色）。

        为什么这一列也归本模块
        ----------------------
        与 ``tasks.pool`` 同一条理由：``payload_json`` 是**建任务时的输入契约**
        （§03.5.3），它的字段含义只有 ``TaskPayload`` 一处说了算。让调用方自己拼一段
        JSON 写进去，``extra="forbid"`` 那道闸门就形同虚设 —— 而它的作用恰恰是
        "把拼错的键在**写入时**就拦住"，而不是等某个读的人某天发现读不到。

        **不动 ``version``**：``version`` 是状态迁移的乐观锁，换音色不是状态迁移。
        与 ``update_progress`` / ``set_quality`` 同一条纪律。

        写回时保留 ``payload_json`` 原本的**稀疏形状**（``exclude_unset``）：建任务时
        只写了 ``{"seed": 7}`` 就还是只写那一个键，不会因为这一次改动突然铺开成
        一份带一堆 ``null`` 的完整快照 —— 那些 ``null`` 会出现在审计的 ``before/after``
        里，把"到底改了什么"淹掉。
        """
        with transaction(self._connection, immediate=True):
            row = self._row(task_id)  # 不存在 ⇒ TaskNotFound
            raw: dict[str, Any] = json.loads(row["payload_json"] or "{}")
            raw["voice_map"] = dict(voice_map)
            merged = TaskPayload.model_validate(raw)  # 顺手校验：拼错的键在这里就炸
            self._connection.execute(
                "UPDATE tasks SET payload_json = ? WHERE id = ?",
                (merged.model_dump_json(exclude_unset=True), task_id),
            )
            return TaskRead.from_row(self._row(task_id))

    def set_quality(self, task_id: str, report: QualityReport) -> TaskRead:
        """回填 ``tasks.quality_json``（§03.5.3 · T3.7）。

        与 `update_progress` 同一条纪律：**不动 ``version``**。理由是同一个 ——
        ``version`` 是状态迁移的乐观锁，而 QC 结论是"迁移到 ``completed`` 之前顺手写下"
        的附属数据。若这里也 bump，编排层在 `render` 阶段持有的 version 会立刻失效，
        紧接着的 `completed` 迁移必然撞 `ConcurrentModification` 重试一轮。

        **整体覆盖**而不是合并：一次出片就是一份结论，旧字段留在库里只会让"这条片子
        到底合没合格"出现两个答案。要保留历史请查 ``manifest.json``（它逐条落盘、不覆盖）。
        """
        payload = report.model_dump_json()
        with transaction(self._connection, immediate=True):
            self._row(task_id)  # 不存在 ⇒ TaskNotFound
            self._connection.execute(
                "UPDATE tasks SET quality_json = ? WHERE id = ?",
                (payload, task_id),
            )
            return TaskRead.from_row(self._row(task_id))

    def set_cover_path(
        self,
        task_id: str,
        *,
        cover_path: Path | None,
        plan: Mapping[str, Any] | None = None,
    ) -> TaskRead:
        """回填 ``tasks.context_json.cover_path``（T5.1 · §06.3）。

        为什么进 ``context_json`` 而不是新开一列
        ----------------------------------------
        ``context_json`` 的定位就是"这条任务走到哪儿了、手上有什么"（§03.5.3），
        成片路径 ``final_path`` 已经住在里面。给封面单独开一列会让"一条任务的产物在哪"
        分裂成两处，而查它的人（发布面板、GC）永远会漏掉一处。

        为什么**合并**而不是整体覆盖（与 :meth:`set_quality` 相反）
        ----------------------------------------------------------
        ``quality_json`` 是"一次出片一份结论"，旧字段留着会出现两个答案，所以整体覆盖。
        而 ``context_json`` 里躺着的是**互不相干**的几件事（成片路径、这次封面）。
        整体覆盖会把 ``final_path`` 抹掉 —— 那是静默的数据损坏，比留一个旧封面路径坏得多。

        **不动 ``version``**：与 :meth:`update_progress` / :meth:`set_quality` 同一条纪律
        （``version`` 是状态迁移的乐观锁）。

        ``cover_path=None`` 是**合法值**：封面生成失败（§06.3：无封面发布）也要留痕，
        否则下一个人看到 context 里没有这个键，分不清"没试过"与"试了没成"。
        """
        with transaction(self._connection, immediate=True):
            row = self._row(task_id)  # 不存在 ⇒ TaskNotFound
            payload: dict[str, Any] = json.loads(row["context_json"] or "{}")
            payload["cover_path"] = None if cover_path is None else cover_path.as_posix()
            if plan is not None:
                payload["cover_plan"] = dict(plan)
            self._connection.execute(
                "UPDATE tasks SET context_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), task_id),
            )
            return TaskRead.from_row(self._row(task_id))
