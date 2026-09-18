"""写稿流水线（T1.10 · §04.1.4 / §04.1.5）—— 选题 → 大纲 → 成稿 → 逐句落库。

一次 ``draft`` 的链路
--------------------
```
topic_candidates(selected/candidate)
        │  ① 没有 task ⇒ 建任务（pending，幂等键 topic:<id>）并把选题置 queued
        ▼
   tasks: pending → drafting
        │  ② DirectorAgent（钩子 + 3–5 段 + CTA）
        ▼
   DirectorOutput
        │  ③ WriterAgent（600–800 字 + 逐句表 + 口癖/禁区自检，重写 ≤2）
        ▼
   WriterOutput
        │  ④ build_draft（强制切分 ≤28 字 ⇒ 复核字数/口癖/禁区/单人占比）
        ▼
   scripts + script_sentences ★ 同一事务（ScriptRepo.save_draft）
```

任务状态停在哪
--------------
本阶段只做 ``pending → drafting``。``drafting → reviewing`` 是 **T1.11** 的事
（评分 + 确认闸）—— 提前跳过去，只会让"评分还没跑但状态已经说评完了"成为事实。
稿子已经在库里，评分阶段按 ``scripts.get_active(task_id)`` 接着跑即可。

失败怎么办
----------
- 选题不存在 ⇒ ``TOPIC_NOT_FOUND``（不猜、不新建）。
- Director / Writer 失败（含**禁区命中**）⇒ 任务置 ``failed``（带 ``error_code``，
  ``last_healthy_status`` 会记下断点），**不落库半成品**。
- 成稿仍有小毛病（字数差一点、口癖少一个）⇒ **照常落库** + ``warnings``：
  规格书要求的是"取最接近版本 + warn"，不是整批失败（DoD 6）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from studio.agents.base import AgentContext, AgentResult
from studio.core.config import PersonaConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import EVENT_PAYLOAD_KEY, EventKind, Severity
from studio.db import JobStore
from studio.db.models import ScriptRow, SentenceRow, TopicRow
from studio.db.repositories import AuditRepo, ScriptRepo, TopicRepo
from studio.domain.enums import TaskStatus, UnitType
from studio.domain.models import TaskRead
from studio.domain.script import (
    DirectorInput,
    DirectorOutput,
    ScriptRules,
    SentenceSpec,
    WriterInput,
    WriterOutput,
    build_draft,
)
from studio.domain.task_service import TaskService
from studio.domain.topics import TopicSpec
from studio.services.log_service import LogSink
from studio.services.topic_service import topic_spec_from_row

__all__ = ["DirectorLike", "DraftReport", "EnqueueOutcome", "ScriptService", "WriterLike"]

#: structlog 的保留键（``_emit`` 兜底分支展开 payload 时会撞车）
_LOG_RESERVED: Final[frozenset[str]] = frozenset({"event", "level", "logger", "message", "timestamp"})

logger = get_logger("studio.script")

#: 写稿失败**有权**把任务置 ``failed`` 的状态：写稿段之内。
#: 出了这一段说明任务已被别的路径推走了 —— 最典型的现场是 CLI 的 `script draft`
#: 就地跑完（并推着任务一路进了配音），而池里那条 `draft/task` 单元还在跑同一个任务，
#: 它稍后失败时会把已经进配音的任务拽回 ``failed``，把一次好端端的生产打断。
#: 迟到的失败没有话语权（T1.9 裁定 316 现场）。
_FAILURE_HOME: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.PENDING, TaskStatus.DRAFTING, TaskStatus.FAILED}
)


class DirectorLike(Protocol):
    """Director 只需实现 ``run``（与 Planner/Ideator 同一手法：服务认协议不认类）。"""

    async def run(self, ctx: AgentContext, payload: DirectorInput) -> AgentResult[DirectorOutput]: ...


class WriterLike(Protocol):
    async def run(self, ctx: AgentContext, payload: WriterInput) -> AgentResult[WriterOutput]: ...


@dataclass(slots=True)
class DraftReport:
    """一次写稿的结果（成功与否都返回它，**不抛裸异常**）。"""

    ok: bool
    task_id: str
    topic_id: str
    created_task: bool = False
    script_id: str | None = None
    version: int | None = None
    title: str = ""
    sentence_count: int = 0
    word_count: int = 0
    est_duration_ms: int = 0
    speaker_ratio: dict[str, float] = field(default_factory=dict)
    segment_count: int = 0
    catchphrases_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "task_id": self.task_id,
            "topic_id": self.topic_id,
            "created_task": self.created_task,
            "script_id": self.script_id,
            "version": self.version,
            "title": self.title,
            "sentence_count": self.sentence_count,
            "word_count": self.word_count,
            "est_duration_ms": self.est_duration_ms,
            "speaker_ratio": self.speaker_ratio,
            "segment_count": self.segment_count,
            "catchphrases_used": self.catchphrases_used,
            "warnings": self.warnings,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class EnqueueOutcome:
    """一次"勾选入队"的结果（建任务 / 复用旧任务都返回它）。"""

    topic_id: str
    task_id: str
    title: str
    created_task: bool
    task_status: str
    topic_status: str
    selected_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "task_id": self.task_id,
            "title": self.title,
            "created_task": self.created_task,
            "task_status": self.task_status,
            "topic_status": self.topic_status,
            "selected_by": self.selected_by,
        }


class ScriptService:
    """写稿流水线的唯一编排入口（§04.1.4 / §04.1.5）。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        director: DirectorLike | None = None,
        writer: WriterLike | None = None,
        paths: StudioPaths | None = None,
        log: LogSink | None = None,
        tasks: TaskService | None = None,
    ) -> None:
        self._connection = connection
        self._director = director
        self._writer = writer
        self._paths = paths or StudioPaths.from_env()
        self._log = log
        self._tasks = tasks or TaskService(connection)
        self._topics = TopicRepo(connection)
        self._scripts = ScriptRepo(connection)
        self._audit = AuditRepo(connection)
        #: 写稿池的入口（`draft/task` 单元 · T4.11）：建任务与入队必须**同事务**
        #: 看得见 —— 少了这一行，任务会静静地躺在 `pending` 上没人认领。
        self._jobs = JobStore(connection)

    # ── 写稿 ────────────────────────────────────────────────────────────

    async def draft(
        self,
        *,
        topic_id: str,
        persona: PersonaConfig,
        task_id: str | None = None,
        target_duration_ms: int | None = None,
        trace_id: str | None = None,
        actor: str = "worker:draft#1",
    ) -> DraftReport:
        """选题 ⇒ 大纲 ⇒ 成稿 ⇒ 落库。**不抛裸异常**：失败返回 ``ok=False`` 的报告。"""
        director, writer = self._require_agents()
        trace = trace_id or new_ulid()
        topic = self._topics.get(topic_id)
        if topic is None:
            raise StudioError(
                f"选题不存在：{topic_id}",
                code=ErrorCode.TOPIC_NOT_FOUND,
                context={"topic_id": topic_id},
                remediation="先跑 `studio topics ideate` 生成选题，或用 `studio topics list` 确认 id",
            )

        spec = topic_spec_from_row(topic)
        created = (
            task_id is None and self._tasks.find_by_idempotency_key(self._idempotency_key(topic.id)) is None
        )
        task = (
            self._create_task(topic, spec=spec, persona=persona, target_duration_ms=target_duration_ms)
            if task_id is None
            else self._tasks.get(task_id)
        )
        if task.status is TaskStatus.PENDING:
            self._tasks.transition(task.id, TaskStatus.DRAFTING, actor=actor, reason="开始写稿")

        duration = target_duration_ms or persona.max_duration_ms
        ctx = AgentContext(task_id=task.id, persona=persona, trace_id=trace)

        outline_result = await director.run(
            ctx, DirectorInput(topic=spec, target_duration_ms=duration, angle=topic.angle)
        )
        if not outline_result.ok or outline_result.data is None:
            return self._failed(
                topic=topic,
                task_id=task.id,
                created=created,
                code=outline_result.error_code or str(ErrorCode.SCRIPT_DRAFT_FAILED),
                message=outline_result.error_message or "Director 未返回大纲",
                actor=actor,
            )
        outline = outline_result.data

        writer_result = await writer.run(ctx, WriterInput(topic=spec, outline=outline))
        if not writer_result.ok or writer_result.data is None:
            return self._failed(
                topic=topic,
                task_id=task.id,
                created=created,
                code=writer_result.error_code or str(ErrorCode.SCRIPT_DRAFT_FAILED),
                message=writer_result.error_message or "Writer 未返回成稿",
                actor=actor,
                extra=writer_result.warnings,
            )

        rules = ScriptRules.from_persona(persona)
        draft, report = build_draft(
            outline=outline,
            output=writer_result.data,
            catchphrases=persona.catchphrases,
            forbidden=persona.forbidden,
            rules=rules,
        )
        saved = self._scripts.save_draft(
            task_id=task.id,
            title=draft.title,
            hook=draft.hook,
            body_md=draft.body_md,
            cta=draft.cta,
            word_count=draft.word_count,
            est_duration_ms=draft.est_duration_ms,
            speaker_ratio=draft.speaker_ratio,
            outline=outline.model_dump(mode="json"),
            sentences=[_sentence_payload(sentence) for sentence in draft.sentences],
            target_chars=persona.target_chars_min,
            llm_model=writer_result.model or None,
            prompt_version=writer_result.prompt_version or None,
        )

        warnings = [
            *outline_result.warnings,
            *writer_result.warnings,
            *report.warnings,
            *(f"script:{problem}" for problem in report.problems),
        ]
        self._emit(
            "warn" if report.problems else "info",
            f"稿件已生成：{draft.title}（{draft.word_count} 字 / {len(draft.sentences)} 句）",
            payload={
                "task_id": task.id,
                "script_id": saved.script_id,
                "problems": report.problems,
                "split_count": report.split_count,
            },
        )
        return DraftReport(
            ok=True,
            task_id=task.id,
            topic_id=topic.id,
            created_task=created,
            script_id=saved.script_id,
            version=saved.version,
            title=draft.title,
            sentence_count=saved.sentence_count,
            word_count=draft.word_count,
            est_duration_ms=draft.est_duration_ms,
            speaker_ratio=draft.speaker_ratio,
            segment_count=len(outline.segments),
            catchphrases_used=draft.catchphrases_used,
            warnings=warnings,
        )

    def _require_agents(self) -> tuple[DirectorLike, WriterLike]:
        """要真的写稿时才检查 Agent 是否装配（与确认闸同一手法 · 裁定 125）。

        ``enqueue``（勾选入队）是**纯业务规则**：建任务、改选题状态、写留痕，
        一次 LLM 都不调。为了它去装配网关，等于让"配好 LLM Key 之前连勾选都点不了"。
        """
        if self._director is None or self._writer is None:
            raise StudioError(
                "写稿服务未装配 Director/Writer",
                code=ErrorCode.CONFIG_INVALID,
                context={"director": self._director is not None, "writer": self._writer is not None},
                remediation="检查 config/app.yaml 与 LLM Key（E6），或改用「入队」等 draft 池认领",
            )
        return self._director, self._writer

    # ── 勾选入队（T4.3）─────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        topic_id: str,
        persona: PersonaConfig,
        target_duration_ms: int | None = None,
        selected_by: str = "user",
        actor: str = "user",
    ) -> EnqueueOutcome:
        """选题 ⇒ 建任务（``pending``）并把选题置 ``queued``。**不跑写稿。**

        为什么与 :meth:`draft` 分开
        ---------------------------
        "入队"与"现在就跑"是两件事：入队是**意图**（面板上勾一下），跑是**执行**
        （draft 池认领，或 CLI 手动触发）。把它们缝在一起，面板上"勾选"这个动作就会
        变成一次几十秒的 LLM 调用 —— 而用户只是想先把这 6 条挑出来。

        幂等：``topic:<id>`` 命中已有任务 ⇒ **复用**那一行，不建第二个（裁定 87）。
        复用分支还要把选题状态拉回 ``queued``：它可能在上一次失败/废弃时被标成
        ``rejected``，而人这次明确又选了它 —— 状态不跟上来，面板会显示"已驳回"。
        """
        topic = self._topics.get(topic_id)
        if topic is None:
            raise StudioError(
                f"选题不存在：{topic_id}",
                code=ErrorCode.TOPIC_NOT_FOUND,
                context={"topic_id": topic_id},
                remediation="刷新选题面板（列表可能已被下一批选题覆盖）",
            )

        existing = self._tasks.find_by_idempotency_key(self._idempotency_key(topic.id))
        if existing is None:
            task = self._create_task(
                topic,
                spec=topic_spec_from_row(topic),
                persona=persona,
                target_duration_ms=target_duration_ms,
                selected_by=selected_by,
            )
            created = True
        else:
            task = existing
            created = False
            self._topics.set_status(
                topic_id=topic.id, status="queued", task_id=task.id, selected_by=selected_by
            )

        self._audit.record(
            actor=actor,
            action="topic.select",
            target_type="topic",
            target_id=topic.id,
            task_id=task.id,
            before={"topic_status": topic.status, "task_id": topic.task_id},
            after={"topic_status": "queued", "task_id": task.id, "selected_by": selected_by},
            reason="勾选入队",
            source="webui",
        )
        self._emit(
            "info",
            f"选题《{topic.title}》已入队 ⇒ 任务 {task.id}" + ("" if created else "（复用已有任务）"),
            payload={"topic_id": topic.id, "task_id": task.id, "selected_by": selected_by},
            event_kind=EventKind.TOPIC_SELECTED,
        )
        return EnqueueOutcome(
            topic_id=topic.id,
            task_id=task.id,
            title=topic.title,
            created_task=created,
            task_status=task.status.value,
            topic_status="queued",
            selected_by=selected_by,
        )

    # ── 内部 ────────────────────────────────────────────────────────────

    def _create_task(
        self,
        topic: TopicRow,
        *,
        spec: TopicSpec,
        persona: PersonaConfig,
        target_duration_ms: int | None,
        selected_by: str = "auto",
    ) -> TaskRead:
        """建任务并把选题置 ``queued``（幂等键 ``topic:<id>`` ⇒ 重复点不会建两个任务）。

        ``hook_type`` 取 **归一化后**的 ``spec.hook_type``（T1.10 裁定 80）：
        库里允许空/非法值，而 ``TaskPayload`` 是 ``Literal`` —— 拿原值会在这里炸。

        ``create()`` 命中幂等键时原样返回旧行 ⇒ 这里不能再把"新建"当成既成事实；
        新旧的判断在 :meth:`draft` 里用 ``find_by_idempotency_key`` 先问一次。
        """
        task = self._tasks.create(
            title=topic.title,
            topic=topic.title,
            source_topic_id=topic.id,
            source_direction_id=topic.direction_id,
            payload={
                "angle": spec.angle,
                "hook_type": spec.hook_type,
                "target_duration_ms": target_duration_ms or persona.max_duration_ms,
                "persona_id": persona.id,
            },
            idempotency_key=self._idempotency_key(topic.id),
        )
        self._topics.set_status(topic_id=topic.id, status="queued", task_id=task.id, selected_by=selected_by)
        self._enqueue_draft(task.id)
        return task

    def _enqueue_draft(self, task_id: str) -> None:
        """把任务交给写稿池（`draft/task` 单元 · T4.11 接线）。

        为什么建任务时就入队，而不是等某个"调度器"来扫：**入队是幂等的**
        （`(task_id, pool, unit_type, unit_ref)` 唯一键），多入一次不会多跑一遍；
        而少入一次就是"勾了 6 条，面板上 6 个 pending 永远不动" —— 后者才是灾难。
        CLI 的 `studio script draft` 也会走到这里：那次是**就地跑完**，
        池里那条单元随后认领到时发现任务已越过写稿段 ⇒ 空操作成功（可重入）。

        一期只有 `task` 一种写稿单元。`topic_batch`（Planner/Ideator 批次）**仍留待**
        有周期性触发源时再接（T4.12 的 APScheduler）—— 现在入队只会留下永远
        `pending` 的作业，那比"没有作业"更难排查（裁定 71）。
        """
        self._jobs.enqueue(
            task_id=task_id,
            pool="draft",
            unit_type=UnitType.TASK.value,
            unit_ref=task_id,
        )

    @staticmethod
    def _idempotency_key(topic_id: str) -> str:
        """一个选题只对应一个任务：幂等键的构造只此一处（读写两侧同源）。"""
        return f"topic:{topic_id}"

    def _failed(
        self,
        *,
        topic: TopicRow,
        task_id: str,
        created: bool,
        code: str,
        message: str,
        actor: str,
        extra: list[str] | None = None,
    ) -> DraftReport:
        self._emit(
            "error",
            f"写稿失败：{message}",
            payload={"task_id": task_id, "topic_id": topic.id, "error_code": code},
        )
        self._fail_task(task_id, code=code, message=message, actor=actor)
        return DraftReport(
            ok=False,
            task_id=task_id,
            topic_id=topic.id,
            created_task=created,
            warnings=list(extra or []),
            error_code=code,
            error_message=message,
        )

    def _fail_task(self, task_id: str, *, code: str, message: str, actor: str) -> None:
        """把任务置 ``failed``（带断点）；**绝不因为记不上失败而掩盖真正的错误**。

        任务已经越过写稿段 ⇒ **不置失败**，只留一条告警：这条失败是"迟到的"
        （见 :data:`_FAILURE_HOME` 的注释）。任务本身没错，稿子也还在。
        """
        current = self._status_of(task_id)
        if current is not None and current not in _FAILURE_HOME:
            logger.warning(
                "script.failure_arrived_late",
                task_id=task_id,
                status=current.value,
                error_code=code,
            )
            self._emit(
                "warn",
                f"写稿失败但任务已到 {current.value}（迟到的失败不改状态）：{message}",
                payload={"task_id": task_id, "error_code": code, "status": current.value},
            )
            return
        try:
            self._tasks.transition(
                task_id,
                TaskStatus.FAILED,
                actor=actor,
                reason=message,
                error_code=code,
                error_message=message,
            )
        except StudioError as exc:  # pragma: no cover — 状态已变时只记一笔
            logger.warning("fail_task_skipped", task_id=task_id, error=repr(exc))

    def _status_of(self, task_id: str) -> TaskStatus | None:
        """任务当前状态；任务不存在 ⇒ ``None``（失败路径**不再二次爆炸**）。"""
        try:
            return self._tasks.get(task_id).status
        except StudioError:  # pragma: no cover — 任务被删掉的极端情形
            logger.warning("script.task_missing_on_failure", task_id=task_id)
            return None

    def _emit(
        self,
        level: Severity,
        message: str,
        *,
        payload: Mapping[str, Any] | None = None,
        event_kind: EventKind | None = None,
    ) -> None:
        """写一条 ``script.pipeline`` 日志；``event_kind`` 非空 ⇒ 额外扇出一条 WS 事件。"""
        merged = dict(payload or {})
        if event_kind is not None:
            merged[EVENT_PAYLOAD_KEY] = event_kind.value
        if self._log is not None:
            task_id = merged.get("task_id")
            self._log(
                level=level,
                source="script.pipeline",
                message=message,
                task_id=task_id if isinstance(task_id, str) else None,
                payload=merged,
            )
            return
        extra = {key: value for key, value in merged.items() if key not in _LOG_RESERVED}
        if level in {"warn", "error", "fatal"}:
            logger.warning(message, source="script.pipeline", **extra)
        else:
            logger.info(message, source="script.pipeline", **extra)


def _sentence_payload(sentence: SentenceSpec) -> dict[str, Any]:
    """``SentenceSpec`` → ``script_sentences`` 的纯数据（``text_raw`` 与 ``text`` 同源）。"""
    return {
        "seq": sentence.seq,
        "text_raw": sentence.text,
        "text": sentence.text,
        "speaker": sentence.speaker,
        "emotion": sentence.emotion,
        "pause_after_ms": sentence.pause_after_ms,
    }


def read_active_script(
    connection: sqlite3.Connection, task_id: str
) -> tuple[ScriptRow, list[SentenceRow]] | None:
    """当前生效稿件 + 逐句表（WebUI 稿件面板读的是同一份数据）。

    故意**不做成** :class:`ScriptService` 的方法：读路径不需要 Agent，
    为了读一份稿子去凑 Director/Writer 只会让调用方（CLI / 只读面板）多背一堆依赖。
    """
    repo = ScriptRepo(connection)
    script = repo.get_active(task_id)
    if script is None:
        return None
    return script, repo.list_sentences(script.id)
