"""写稿流水线（T1.10 · §04.1.4 / §04.1.5）—— 文案三级：选题 → 标题与论点 → 对话文案。

文案生成分三级（本模块是后两级的编排入口）
------------------------------------------
```
① 一级 · 话题主体        topic_candidates（人工加 / Ideator 产出，可改可删）
        │
        ▼
② 二级 · 视频标题+核心论点  topic_outlines（Outliner 产出，或人手写；可改可清空）
        │
        ▼
③ 三级 · 对话文案         scripts + script_sentences（Director 大纲 ⇒ Writer 成稿）
```
二级是**可选**的一级：没有它，三级照旧按选题自由发挥（少一张表不能变成「写不出稿」）；
有它，成稿的标题**锁定**用它，正文围绕核心论点展开（见 :meth:`ScriptService.draft`）。

一次 ``draft`` 的链路
--------------------
```
topic_candidates(selected/candidate) + topic_outlines(可选)
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
from typing import Any, Final, Protocol, cast

from studio.agents.base import AgentContext, AgentResult
from studio.core.config import PersonaConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import EVENT_PAYLOAD_KEY, EventKind, Severity
from studio.db import JobStore
from studio.db.models import ScriptRow, SentenceRow, TopicOutlineRow, TopicRow
from studio.db.repositories import AuditRepo, DirectionRepo, OutlineRepo, ScriptRepo, TopicRepo
from studio.domain.enums import TaskStatus, UnitType
from studio.domain.models import TaskRead
from studio.domain.script import (
    FACTS_MAX,
    OUTLINE_ARGUMENT_MAX,
    OUTLINE_TITLE_MAX,
    DirectorInput,
    DirectorOutput,
    OutlineInput,
    OutlineOutput,
    ScriptRules,
    SentenceSpec,
    WriterInput,
    WriterOutput,
    build_draft,
)
from studio.domain.task_service import TaskService
from studio.domain.topics import TopicSpec
from studio.services.log_service import LogSink
from studio.services.topic_service import direction_facts, topic_spec_from_row

__all__ = [
    "DirectorLike",
    "DraftReport",
    "DraftReviewOutcome",
    "EnqueueOutcome",
    "OutlineLike",
    "OutlineReport",
    "ScriptService",
    "WriterLike",
]

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


class OutlineLike(Protocol):
    """Outliner 只需实现 ``run``（与 Director/Writer 同一手法）。"""

    async def run(self, ctx: AgentContext, payload: OutlineInput) -> AgentResult[OutlineOutput]: ...


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


@dataclass(slots=True)
class OutlineReport:
    """一次二级产物的读写结果（生成 / 手改 / 清空都返回它，**不抛裸异常**）。"""

    ok: bool
    topic_id: str
    title: str = ""
    core_argument: str = ""
    llm_model: str | None = None
    prompt_version: str | None = None
    #: 这一份是**模型刚产出的**（``False`` ⇒ 手写或手改的）
    generated: bool = False
    changed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "topic_id": self.topic_id,
            "title": self.title,
            "core_argument": self.core_argument,
            "llm_model": self.llm_model,
            "prompt_version": self.prompt_version,
            "generated": self.generated,
            "changed": list(self.changed),
            "warnings": list(self.warnings),
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


@dataclass(frozen=True, slots=True)
class DraftReviewOutcome:
    """一次「生成完整文案并移交审核」的结果（面板上那一下）。"""

    ok: bool
    topic_id: str
    task_id: str
    script_id: str | None = None
    title: str = ""
    sentence_count: int = 0
    word_count: int = 0
    #: 交接完成时任务停在哪（``reviewing``；失败时是 ``failed``）
    task_status: str = ""
    #: ``True`` ⇒ 已经有生效稿件，这一次**没有重跑 LLM**
    reused: bool = False
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "topic_id": self.topic_id,
            "task_id": self.task_id,
            "script_id": self.script_id,
            "title": self.title,
            "sentence_count": self.sentence_count,
            "word_count": self.word_count,
            "task_status": self.task_status,
            "reused": self.reused,
            "warnings": list(self.warnings),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class ScriptService:
    """写稿流水线的唯一编排入口（§04.1.4 / §04.1.5）。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        director: DirectorLike | None = None,
        writer: WriterLike | None = None,
        outliner: OutlineLike | None = None,
        paths: StudioPaths | None = None,
        log: LogSink | None = None,
        tasks: TaskService | None = None,
    ) -> None:
        self._connection = connection
        self._director = director
        self._writer = writer
        self._outliner = outliner
        self._paths = paths or StudioPaths.from_env()
        self._log = log
        self._tasks = tasks or TaskService(connection)
        self._topics = TopicRepo(connection)
        self._directions = DirectionRepo(connection)
        self._scripts = ScriptRepo(connection)
        self._outlines = OutlineRepo(connection)
        self._audit = AuditRepo(connection)
        #: 写稿池的入口（`draft/task` 单元 · T4.11）：建任务与入队必须**同事务**
        #: 看得见 —— 少了这一行，任务会静静地躺在 `pending` 上没人认领。
        self._jobs = JobStore(connection)

    # ── 二级产物：视频标题 + 核心论点 ────────────────────────────────
    async def outline(
        self,
        *,
        topic_id: str,
        persona: PersonaConfig,
        actor: str = "user",
        trace_id: str | None = None,
        source: str = "webui",
    ) -> OutlineReport:
        """让模型给这条选题定「视频标题 + 核心论点」（文案三级流水线的二级）。

        为什么这一级**不需要任务**
        --------------------------
        ``draft`` 要先建任务再写稿（稿子挂在任务上），而二级产物挂在**选题**上：
        它是「这条选题要说什么」的定论，在入队之前就该能定。所以这里只认 ``topic_id``，
        任务号有就顺手带进 ``AgentContext``（记账能串起来），没有也照跑。
        """
        outliner = self._require_outliner()
        topic = self._require_topic(topic_id)
        ctx = AgentContext(
            task_id=topic.task_id,
            persona=persona,
            trace_id=trace_id or new_ulid(),
        )
        result = await outliner.run(
            ctx,
            OutlineInput(
                topic=topic_spec_from_row(topic),
                angle=topic.angle,
                facts=self._facts_for(topic),
            ),
        )
        if not result.ok or result.data is None:
            message = result.error_message or result.error_code or "Outliner 未返回标题与论点"
            self._emit(
                "warn",
                f"选题《{topic.title}》的标题与论点没产出：{message}",
                payload={"topic_id": topic_id, "error_code": result.error_code},
            )
            return OutlineReport(
                ok=False,
                topic_id=topic_id,
                warnings=list(result.warnings),
                error_code=result.error_code or str(ErrorCode.SCRIPT_DRAFT_FAILED),
                error_message=message,
            )
        data = result.data
        row = self._outlines.upsert(
            topic_id=topic_id,
            title=data.title.strip(),
            core_argument=data.core_argument.strip(),
            llm_model=result.model or None,
            prompt_version=result.prompt_version or None,
        )
        self._audit.record(
            actor=actor,
            action="outline.generated",
            target_type="topic",
            target_id=topic_id,
            after={"title": row.title, "core_argument": row.core_argument},
            reason="模型产出二级产物",
            source=source,
        )
        self._emit(
            "info",
            f"二级产物已生成：《{row.title}》",
            payload={"topic_id": topic_id, "llm_model": row.llm_model},
        )
        return OutlineReport(
            ok=True,
            topic_id=topic_id,
            title=row.title,
            core_argument=row.core_argument,
            llm_model=row.llm_model,
            prompt_version=row.prompt_version,
            generated=True,
            warnings=list(result.warnings),
        )

    def save_outline(
        self,
        *,
        topic_id: str,
        title: str,
        core_argument: str,
        actor: str = "user",
    ) -> OutlineReport:
        """手写 / 手改二级产物（**两个字段一起给**：这一级只有这两样东西）。

        改标题与改论点是同一件事的两面（论点变了标题往往也得变），所以这里不做
        「只改一半」的接口 —— 那只会让「标题承诺 A、论点讲 B」成为库里的一种合法状态。
        留痕里 ``before``/``after`` 都记全，谁在什么时候把论点掰弯了查得到。
        """
        topic = self._require_topic(topic_id)
        cleaned_title = _clean_outline_text(title, field="title", limit=OUTLINE_TITLE_MAX, topic_id=topic_id)
        cleaned_argument = _clean_outline_text(
            core_argument, field="core_argument", limit=OUTLINE_ARGUMENT_MAX, topic_id=topic_id
        )
        before = self._outlines.get(topic_id)
        row = self._outlines.upsert(topic_id=topic_id, title=cleaned_title, core_argument=cleaned_argument)
        self._audit.record(
            actor=actor,
            action="outline.updated",
            target_type="topic",
            target_id=topic_id,
            before=None if before is None else _outline_snapshot(before),
            after=_outline_snapshot(row),
            reason="人工定稿二级产物",
            source="webui",
        )
        self._emit(
            "info",
            f"二级产物已定稿：《{row.title}》",
            payload={"topic_id": topic_id, "topic_title": topic.title},
        )
        return OutlineReport(
            ok=True,
            topic_id=topic_id,
            title=row.title,
            core_argument=row.core_argument,
            llm_model=row.llm_model,
            prompt_version=row.prompt_version,
            changed=["title", "core_argument"],
        )

    def clear_outline(self, *, topic_id: str, actor: str = "user") -> OutlineReport:
        """清掉二级产物（**幂等**：本来就没有也算成功，``changed`` 为空）。

        清掉之后三级会退回「按选题自由发挥」—— 这是刻意的：二级是**可选**的一级，
        没定就照旧写，不能因为少一张表就写不出稿。
        """
        topic = self._require_topic(topic_id)
        before = self._outlines.get(topic_id)
        removed = self._outlines.delete(topic_id)
        if removed:
            self._audit.record(
                actor=actor,
                action="outline.deleted",
                target_type="topic",
                target_id=topic_id,
                before=None if before is None else _outline_snapshot(before),
                reason="人工清空二级产物",
                source="webui",
            )
            self._emit(
                "info",
                f"二级产物已清空：《{topic.title}》",
                payload={"topic_id": topic_id},
            )
        return OutlineReport(
            ok=True,
            topic_id=topic_id,
            changed=["title", "core_argument"] if removed else [],
        )

    def get_outline(self, topic_id: str) -> TopicOutlineRow | None:
        """读二级产物（没有就 ``None`` —— 它不是错误状态）。"""
        return self._outlines.get(topic_id)

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
        """选题 ⇒ 大纲 ⇒ 成稿 ⇒ 落库。**不抛裸异常**：失败返回 ``ok=False`` 的报告。

        ``topic_id`` 那一行**可以已经不在了**：选题被删（删方向 / 删候选）之后，已经排队的
        那条任务照跑 —— 它自己带着要说什么（``tasks.title`` + ``payload_json`` 里的
        ``angle`` / ``hook_type``）。删掉的是**想法**，不是**活**。只有"没有任务可依托、
        选题又不存在"时才报 ``TOPIC_NOT_FOUND``（那才是真的无从下笔）。
        """
        director, writer = self._require_agents()
        trace = trace_id or new_ulid()
        topic = self._topics.get(topic_id)

        if topic is None:
            if task_id is None:
                # 没有任务可依托 ⇒ 真的无从下笔：这一句**一定抛**（`cast` 只是给类型一个交代）
                self._require_topic(topic_id)
            task = self._tasks.get(cast("str", task_id))
            spec = _spec_from_task(task)
            created = False
        else:
            spec = topic_spec_from_row(topic)
            created = (
                task_id is None
                and self._tasks.find_by_idempotency_key(self._idempotency_key(topic.id)) is None
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

        # 二级产物（视频标题 + 核心论点）：有就**锁定**标题并当主线喂下去。
        #
        # 缺位时**自动补一次**，而不是「自由发挥」：这一级是「先说清楚要说什么」，
        # 而它以前只有面板上那颗手动按钮能触发 —— 于是三级拿到的是 OUTLINE_UNSET，
        # 「围绕核心论点深挖」这句指令**没有论点可围绕**，只能写成表面叙事；改了
        # outliner 提示词也看不到效果，因为这一级压根没跑（topic_outlines 长期空表）。
        # 自动补是 best-effort：模型不给就不给，退回下面的兜底，绝不因此写不出稿。
        saved_outline = self._outlines.get(topic_id)
        if saved_outline is None and topic is not None:
            saved_outline = await self._auto_outline(topic_id=topic_id, persona=persona, trace=trace)
        # 标题兜底 = 一级选题标题（用户口径：原标题已经够好了）。落到 OUTLINE_UNSET 上
        # 等于让模型「按选题自行发挥」一个标题 —— 而标题是最不该自由发挥的东西。
        locked_title = (saved_outline.title if saved_outline is not None else None) or spec.title
        core_argument = saved_outline.core_argument if saved_outline is not None else None

        # 已知事实（今日新闻挑出来的方向才有）：**在服务层按方向回读**，不指望上游
        # 哪一级的模型把它抄下来 —— 抄写会漂，而事实漂了就是幻觉（T5.12 增补）。
        facts = self._facts_for(topic)

        outline_result = await director.run(
            ctx,
            DirectorInput(
                topic=spec,
                target_duration_ms=duration,
                angle=spec.angle,
                outline_title=locked_title,
                core_argument=core_argument,
                facts=facts,
            ),
        )
        if not outline_result.ok or outline_result.data is None:
            return self._failed(
                topic_id=topic_id,
                task_id=task.id,
                created=created,
                code=outline_result.error_code or str(ErrorCode.SCRIPT_DRAFT_FAILED),
                message=outline_result.error_message or "Director 未返回大纲",
                actor=actor,
            )
        outline = outline_result.data

        writer_result = await writer.run(
            ctx,
            WriterInput(
                topic=spec,
                outline=outline,
                outline_title=locked_title,
                core_argument=core_argument,
                facts=facts,
            ),
        )
        if not writer_result.ok or writer_result.data is None:
            return self._failed(
                topic_id=topic_id,
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
        if locked_title is not None:
            # 标题在二级就定死了：Writer 起的那一个只当它自己写的草稿（提示词已经要求照抄，
            # 这里再强制一次 —— 模型偶尔仍会另起一个，而「标题被悄悄换掉」是最难发现的一类漂移）。
            draft = draft.model_copy(update={"title": locked_title})
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
        # 就地写稿到此结束 ⇒ **现在**才把作业交给写稿池（时机说明见 `_enqueue_draft`）
        self._enqueue_draft(task.id)
        return DraftReport(
            ok=True,
            task_id=task.id,
            topic_id=topic_id,
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

    async def draft_and_review(
        self,
        *,
        topic_id: str,
        persona: PersonaConfig,
        target_duration_ms: int | None = None,
        actor: str = "user",
    ) -> DraftReviewOutcome:
        """选题 ⇒ 入队 ⇒ 写稿 ⇒ 推 ``reviewing``（面板上「生成文案并送审」那一下）。

        为什么把三步缝在一起
        --------------------
        面板上这是一个**动作**：选中一条候选，我要看到成稿，然后进确认闸。把它拆成
        「入队」「写稿」「送审」三个按钮，用户就得记住顺序，而且中间任何一步忘了按，
        结果都是一条躺在 ``pending`` 上不动的任务 —— 那不是灵活，那是三倍的出错面。

        为什么推 ``reviewing`` 而不是停在 ``drafting``
        ----------------------------------------------
        ``drafting`` 的语义是"正在写"，而这一刻稿子已经在库里了。停在 ``drafting``
        会让面板显示"写稿中"，然后由写稿池认领时才发现"已有稿件 ⇒ 跳过写稿直接审稿"，
        于是状态在没有任何写入的情况下自己往前跳一格 —— 看着像有人偷偷动了它。
        ``drafting → reviewing`` 本来就是静态边（T1.11 那条），这里只是按规矩走。

        重复点按
        --------
        已经有生效稿件、而且任务已经越过写稿段 ⇒ **不重跑 LLM**，直接复用（``reused``）。
        重写会白烧一次 Director + Writer，而且会盖掉正在审的那一版。

        为什么要打「人工送审」标记
        --------------------------
        ``approval.auto_approve_policy``（默认 ``grade_a``）会让 A 级稿子自动放行 ——
        那对**批量**流水线是对的，对**这一下**是错的：人亲手点的送审，结果确认闸里
        空空如也，按钮就成了假的。所以这里给任务打上 ``context_json.human_gate``，
        审稿侧据此把放行策略降为 ``off``（见 ``ReviewService.review``）。
        """
        outcome = self.enqueue(
            topic_id=topic_id,
            persona=persona,
            target_duration_ms=target_duration_ms,
            selected_by="user",
            actor=actor,
            # 就地写稿跑完之前不许投作业（否则与写稿池抢同一个任务，见 `_enqueue_draft`）
            defer_draft_job=True,
        )
        task_id = outcome.task_id
        # 人工送审 ⇒ 这条稿子降为 off：什么等级都停在确认闸等人（见 ReviewService）
        self._tasks.require_human_gate(task_id)
        current = self._tasks.get(task_id)
        existing = read_active_script(self._connection, task_id)
        # 稿件已经在库里 ⇒ 一个 token 都不再烧。``pending`` 是唯一的例外：
        # 那个状态意味着"任务刚被（重新）放回起点"，此刻库里那份稿件属于上一轮。
        if existing is not None and current.status is not TaskStatus.PENDING:
            script_row, sentence_rows = existing
            warnings: list[str] = []
            if current.status is TaskStatus.DRAFTING:
                # 上次崩在"落库之后、审稿之前"：状态还停在 drafting，稿子却是现成的。
                self._tasks.transition(
                    task_id, TaskStatus.REVIEWING, actor=actor, reason="已有稿件，直接移交审核"
                )
                current = self._tasks.get(task_id)
            elif current.status is TaskStatus.FAILED:
                # 任务是在**后面**的段落挂的（配音 / 渲染），稿件本身没问题。
                # 这里硬要"送审"等于假装那条任务没失败 —— 如实说出来，重试走任务面板。
                warnings.append(
                    f"任务当前处于 {current.status.value}：稿件已在库里，重试请走「任务 / 四池」面板"
                )
            self._emit(
                "info",
                f"《{script_row.title or outcome.title}》已有生效稿件，直接移交审核（不重跑写稿）",
                payload={"task_id": task_id, "topic_id": topic_id, "script_id": script_row.id},
            )
            # 复用分支同样要保证作业在（上一次可能崩在"投作业之前"）
            self._enqueue_draft(task_id)
            return DraftReviewOutcome(
                ok=True,
                topic_id=topic_id,
                task_id=task_id,
                script_id=script_row.id,
                title=script_row.title or outcome.title,
                sentence_count=len(sentence_rows),
                word_count=script_row.word_count,
                task_status=current.status.value,
                reused=True,
                warnings=tuple(warnings),
            )

        report = await self.draft(
            topic_id=topic_id,
            persona=persona,
            task_id=task_id,
            target_duration_ms=target_duration_ms,
            actor=actor,
        )
        status = self._status_of(task_id)
        if not report.ok:
            return DraftReviewOutcome(
                ok=False,
                topic_id=topic_id,
                task_id=task_id,
                title=report.title,
                warnings=tuple(report.warnings),
                task_status="" if status is None else status.value,
                error_code=report.error_code or str(ErrorCode.SCRIPT_DRAFT_FAILED),
                error_message=report.error_message or "写稿失败",
            )

        if status is TaskStatus.DRAFTING:
            # 推到 ``reviewing``：写稿池里那条单元会接着跑审稿 + 评分 + 确认闸
            # （它认 ``drafting`` / ``reviewing`` 两个入口，稿件已在库里 ⇒ 不重写）。
            self._tasks.transition(
                task_id, TaskStatus.REVIEWING, actor=actor, reason="生成完整文案后移交审核"
            )
            status = self._status_of(task_id)
        self._emit(
            "info",
            f"《{report.title}》已移交审核（任务 {task_id}）",
            payload={"task_id": task_id, "topic_id": topic_id, "script_id": report.script_id},
        )
        return DraftReviewOutcome(
            ok=True,
            topic_id=topic_id,
            task_id=task_id,
            script_id=report.script_id,
            title=report.title,
            sentence_count=report.sentence_count,
            word_count=report.word_count,
            task_status="" if status is None else status.value,
            warnings=tuple(report.warnings),
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

    def _require_topic(self, topic_id: str) -> TopicRow:
        """按 id 取选题（没有 ⇒ ``TOPIC_NOT_FOUND``，不猜、不新建）。"""
        topic = self._topics.get(topic_id)
        if topic is not None:
            return topic
        raise StudioError(
            f"选题不存在：{topic_id}",
            code=ErrorCode.TOPIC_NOT_FOUND,
            context={"topic_id": topic_id},
            remediation="先跑 `studio topics ideate` 生成选题，或用 `studio topics list` 确认 id",
        )

    def _facts_for(self, topic: TopicRow | None) -> str:
        """这条选题的**已知事实**（今日新闻挑出来的方向才有；其余是空串）。

        为什么按 ``topic.direction_id`` 回读方向，而不是把事实抄进 ``topic_candidates``：
        抄一份就多一处会漂的副本（人改了方向那一行，选题上那份还是旧的），而方向才是
        事实的落点。方向被删、选题本来就是人工加的（``direction_id`` 指向别处）⇒ 回空串，
        写稿照跑 —— 少几句事实，不是一次失败。

        截到 :data:`FACTS_MAX`：它是个 pydantic 上限，超了会在建 ``WriterInput`` 时抛
        ValidationError，把"多了一条依据"变成"这条选题写不出稿"。
        """
        if topic is None or not topic.direction_id:
            return ""
        direction = self._directions.get(topic.direction_id)
        return "" if direction is None else direction_facts(direction)[:FACTS_MAX]

    async def _auto_outline(
        self, *, topic_id: str, persona: PersonaConfig, trace: str
    ) -> TopicOutlineRow | None:
        """写稿前自动补一次二级产物（**best-effort，绝不阻塞写稿**）。

        为什么值得多花这一次调用：三级那句「围绕核心论点深挖价值观」要有一个**论点**
        才落得下去。没有它，模型只能对着选题写表面叙事 —— 而这一级以前只在面板上
        手动触发，实际链路里几乎从不发生。
        """
        if self._outliner is None:
            return None
        try:
            report = await self.outline(
                topic_id=topic_id, persona=persona, actor="system", trace_id=trace, source="worker"
            )
        except StudioError as exc:
            logger.warning("script.auto_outline_failed", topic_id=topic_id, error=exc.message)
            return None
        if not report.ok:
            return None
        return self._outlines.get(topic_id)

    def _require_outliner(self) -> OutlineLike:
        """要生成二级产物时才检查 Outliner 是否装配（与 :meth:`_require_agents` 同一手法）。

        手写 / 手改 / 清空这三个动作**一次 LLM 都不调** —— 没配 Key 也该能用它们把标题
        与论点先定下来（那正是「配 Key 之前也能干活」的意义）。
        """
        if self._outliner is not None:
            return self._outliner
        raise StudioError(
            "写稿服务未装配 Outliner",
            code=ErrorCode.CONFIG_INVALID,
            context={"outliner": False},
            remediation="检查 config/llm.yaml 的 Key（E6），或手工填标题与核心论点",
        )

    # ── 勾选入队（T4.3）─────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        topic_id: str,
        persona: PersonaConfig,
        target_duration_ms: int | None = None,
        selected_by: str = "user",
        actor: str = "user",
        defer_draft_job: bool = False,
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

        ``defer_draft_job=True`` 是给 :meth:`draft_and_review` 用的：那个流程紧接着
        就要**就地**写稿，作业得等它跑完再投（见 :meth:`_enqueue_draft` 的时机说明）。
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

        if not defer_draft_job:
            self._enqueue_draft(task.id)

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

        **这里不投作业**：入队时机见 :meth:`_enqueue_draft` —— 写稿池 ~1s 就会
        来认领，而就地写稿要跑几分钟，在同一个任务上撞车就是两份 LLM 账单。
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
        return task

    def _enqueue_draft(self, task_id: str) -> None:
        """把任务交给写稿池（`draft/task` 单元 · T4.11 接线）。

        什么时候投 —— 这条线踩过一次，写清楚
        --------------------------------------
        作业必须在**就地写稿跑完之后**才投，不能在 :meth:`_create_task` 里顺手投。
        写稿池 ~1s 就来认领，而一次就地写稿要跑几分钟的 LLM：两边认领到同一个任务，
        就是两份 Director + Writer 并发跑 —— token 翻倍、预算当场烧穿
        （`budget_exceeded` 之后云端通道熔断，评分降级到本地小模型，等级与放行全跟着歪）。

        所以规矩是两条：

        * :meth:`enqueue`（勾选入队，**不跑写稿**）⇒ 立刻投，池子负责写稿；
        * :meth:`draft`（就地写稿，含 ``draft_now`` 与 CLI）⇒ 跑完/跑挂之后才投，
          池子认领时稿件已在库里 ⇒ 只跑审稿（``drafting`` / ``reviewing`` 都是它的入口）。

        为什么不是等某个"调度器"来扫：**入队是幂等的**
        （`(task_id, pool, unit_type, unit_ref)` 唯一键），多入一次不会多跑一遍；
        而少入一次就是"勾了 6 条，面板上 6 个 pending 永远不动" —— 后者才是灾难。
        失败路径也要投，理由同上：写稿挂了，池子照旧按 ``retry_from`` 重试。

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
        topic_id: str,
        task_id: str,
        created: bool,
        code: str,
        message: str,
        actor: str,
        extra: list[str] | None = None,
    ) -> DraftReport:
        # 写稿挂了也要投作业：池子认领时按 `retry_from` 回到断点重试（与成功路径同一条规矩）
        self._enqueue_draft(task_id)
        self._emit(
            "error",
            f"写稿失败：{message}",
            payload={"task_id": task_id, "topic_id": topic_id, "error_code": code},
        )
        self._fail_task(task_id, code=code, message=message, actor=actor)
        return DraftReport(
            ok=False,
            task_id=task_id,
            topic_id=topic_id,
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


def _clean_outline_text(value: str, *, field: str, limit: int, topic_id: str) -> str:
    """二级产物的两个自由文本字段：去空白 + 非空 + 长度上限。

    上限在这里再卡一次（schema 已经卡过）：手写的入口**不经**模型输出 schema，
    少这一道，一个 5000 字的「标题」能直接落库。
    """
    cleaned = value.strip()
    if not cleaned:
        raise StudioError(
            f"{field} 不能为空",
            code=ErrorCode.OUTLINE_INVALID,
            context={"topic_id": topic_id, "field": field},
            remediation="标题与核心论点都是必填 —— 想撤掉这一级请用「清空」",
        )
    if len(cleaned) > limit:
        raise StudioError(
            f"{field} 超过 {limit} 字（实际 {len(cleaned)} 字）",
            code=ErrorCode.OUTLINE_INVALID,
            context={"topic_id": topic_id, "field": field, "limit": limit},
            remediation=f"删到 {limit} 字以内再存",
        )
    return cleaned


def _outline_snapshot(row: TopicOutlineRow) -> dict[str, Any]:
    """留痕里那两列（``before``/``after`` 同一份形状，方便逐列比对）。"""
    return {"title": row.title, "core_argument": row.core_argument}


def _spec_from_task(task: TaskRead) -> TopicSpec:
    """任务自带的那一份选题信息（**选题行已经被删掉**时的退路）。

    ``tasks`` 建的时候就把标题抄在自己身上（``tasks.title``），``payload_json`` 里还带着
    ``angle`` / ``hook_type`` —— 也就是说一条已经排队的活**本来就说得清自己要做什么**。
    所以"选题没了"不该把它变成一条注定写不出稿的作业。

    两处只能给个交代、给不出原值：

    - ``reason``（提示词里的【为什么做它】）在 ``TaskPayload`` 里没有对应字段，而那一份是
      §03.5.3 冻结的契约 ⇒ 这里给一句**实话**，而不是编一个理由；
    - ``score`` 不是提示词的一部分（Director 只读标题 / 角度 / 理由）⇒ 给 0，把"没有分"说清楚。

    为什么不用 ``task.context`` 兜：那是渲染/配音阶段的运行期上下文，与选题无关。
    """
    hook = task.payload.hook_type
    return TopicSpec(
        title=task.title,
        hook_type=hook if hook is not None else "other",
        angle=task.payload.angle or "",
        exec_feasible=True,
        score=0.0,
        reason="（这条选题已从选题池删除，按任务自己记下的标题与角度继续）",
    )


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
