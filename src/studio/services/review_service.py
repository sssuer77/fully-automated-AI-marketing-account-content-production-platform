"""审稿编排（T1.11 · §04.1.6 / §04.4.4）—— 写稿池的最后一站。

一次 ``review`` = 审稿 ⇒ 评分 ⇒ 分级放行
---------------------------------------
```
读生效稿件（没有 ⇒ REVIEW_SCRIPT_MISSING，不猜）
   │
   ├─ 算规则通道（服务端，可复算）
   ├─ Reviewer（LLM 六维度 + 问题清单）
   ├─ total = 0.3×rule + 0.7×llm ⇒ grade
   ├─ 落 review_scores（一行一轮）
   │
   └─ 按 gate_action 分四路
        ├─ auto_pass  ⇒ queued_voice（署名 auto_approve_A / AB）+ approvals + audit_ops
        ├─ edit       ⇒ editing ⇒ Editor ⇒ 新版本落库 ⇒ reviewing（回到最上面，最多 2 轮）
        ├─ human_gate ⇒ awaiting_approval + approvals(pending)
        └─ discard    ⇒ discarded + 选题候选置 rejected + audit_ops
```

三条纪律
--------
1. **改稿轮次是硬闸门**（R16）：``round_no > REVISION_LIMIT`` 时 B 级不再进 Editor，
   直接进确认闸。这与"这次改得好不好"无关 —— 是钱的问题。
2. **每一轮都落库**（``review_scores`` + 新版 ``scripts``）：面板要能并排显示
   "第 1 轮 6.3 分 / 第 2 轮 7.1 分"，否则"改稿有没有用"永远说不清。
3. **不抛裸异常**：失败返回 ``ok=False`` 的 :class:`ReviewReport` 并把任务置 ``failed``
   （与 :class:`~studio.services.script_service.ScriptService` 同一口径）。

为什么 ``policy`` 是构造参数而不是每次调用传
--------------------------------------------
它是**运行期配置**（``config/app.yaml → approval.auto_approve_policy``，WebUI 的
"一键全自动"开关会改它）。放进构造参数，WebUI 换策略时只要重建服务；
塞进每次调用，就等于让每个调用点都记得去读一次配置 —— 漏一处，那条路径的
放行策略就悄悄退回默认值。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol

from pydantic import ValidationError

from studio.agents.base import AgentContext, AgentResult
from studio.core.config import PersonaConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import EVENT_PAYLOAD_KEY, EventKind, Severity
from studio.db.models import ReviewRow, ScriptRow, SentenceRow
from studio.db.repositories import ApprovalRepo, AuditRepo, ReviewRepo, ScriptRepo, TopicRepo
from studio.domain.enums import ApprovalDecision, AutoApprovePolicy, TaskStatus
from studio.domain.scoring import (
    REVISION_LIMIT,
    EditorInput,
    EditorOutput,
    GateAction,
    ReviewerInput,
    ReviewerOutput,
    ReviewOutput,
    auto_approved_by,
    compute_score,
    decision_for,
    evaluate_rule_channel,
    gate_action,
    llm_total_of,
    rule_total_of,
)
from studio.domain.script import (
    DirectorOutput,
    ScriptRules,
    SentenceSpec,
    WriterOutput,
    build_draft,
    catchphrase_hits,
    estimate_duration_ms,
)
from studio.domain.state_machine import RESCUE_STATUSES
from studio.domain.task_service import TaskService
from studio.domain.topics import TopicSpec
from studio.services.log_service import LogSink
from studio.services.script_service import _sentence_payload, read_active_script

__all__ = [
    "ApprovalOutcome",
    "RescueOutcome",
    "ReviewReport",
    "ReviewService",
    "read_latest_review",
    "writer_output_from_rows",
]

logger = get_logger(__name__)

#: 决断 ⇒ 任务去哪儿（§04.4.4 REST 上行的第三列，逐字对应）
_DECISION_TARGET: Final[Mapping[ApprovalDecision, TaskStatus]] = MappingProxyType(
    {
        ApprovalDecision.APPROVE: TaskStatus.QUEUED_VOICE,
        ApprovalDecision.REJECT: TaskStatus.EDITING,
        ApprovalDecision.DISCARD: TaskStatus.DISCARDED,
    }
)

#: structlog 的保留键：`_emit` 的兜底分支要把 payload 展开成 kwargs，这些键会撞车
_LOG_RESERVED: Final[frozenset[str]] = frozenset({"event", "level", "logger", "message", "timestamp"})


class ReviewerLike(Protocol):
    """审稿 Agent 的最小接口（服务层只认这个，测试可注入假件）。"""

    async def run(self, ctx: AgentContext, payload: ReviewerInput) -> AgentResult[ReviewerOutput]: ...


class EditorLike(Protocol):
    """改稿 Agent 的最小接口。"""

    async def run(self, ctx: AgentContext, payload: EditorInput) -> AgentResult[EditorOutput]: ...


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """一次 ``review`` 的结论（成功与否都返回它）。"""

    ok: bool
    task_id: str
    status: str
    action: str = ""
    grade: str | None = None
    decision: str | None = None
    total: float | None = None
    rule_total: float | None = None
    llm_total: float | None = None
    round_no: int = 0
    revision_round: int = 0
    script_id: str | None = None
    version: int | None = None
    review_id: str | None = None
    approval_id: str | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "task_id": self.task_id,
            "status": self.status,
            "action": self.action,
            "grade": self.grade,
            "decision": self.decision,
            "total": self.total,
            "rule_total": self.rule_total,
            "llm_total": self.llm_total,
            "round_no": self.round_no,
            "revision_round": self.revision_round,
            "script_id": self.script_id,
            "version": self.version,
            "review_id": self.review_id,
            "approval_id": self.approval_id,
            "issues": self.issues,
            "warnings": self.warnings,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """一次人工决断的结果。

    与 :class:`ReviewReport` 的差别是**刻意的**：``review()`` 是流水线里的一站，
    失败只能返回 ``ok=False``（不能把 Worker 崩掉）；``decide_approval()`` 是人
    按下的按钮，失败必须**说清为什么被拒**（REST 层要把它翻成 4xx），所以它抛
    :class:`StudioError`，成功时返回这个没有 ``ok`` 字段的结果。
    """

    task_id: str
    approval_id: str
    decision: str
    status: str
    grade: str | None = None
    score_total: float | None = None
    revision_round: int = 0
    comment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "approval_id": self.approval_id,
            "decision": self.decision,
            "status": self.status,
            "grade": self.grade,
            "score_total": self.score_total,
            "revision_round": self.revision_round,
            "comment": self.comment,
        }


@dataclass(frozen=True, slots=True)
class RescueOutcome:
    """一次"捞回"的结果（``discarded`` / ``canceled`` → ``pending``）。

    为什么不复用 :class:`ApprovalOutcome`：捞回**不是**一次决断 —— 它没有
    ``approval_id``，也没有 ``decision``。硬塞进去只会让调用方去猜"哪些字段在
    这个场景下是有意义的"。两个场景各有一个结果类型，字段就都是真的。
    """

    task_id: str
    status: str
    previous_status: str
    revision_round: int = 0
    topic_id: str | None = None
    topic_restored: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "previous_status": self.previous_status,
            "revision_round": self.revision_round,
            "topic_id": self.topic_id,
            "topic_restored": self.topic_restored,
        }


class ReviewService:
    """审稿 + 改稿 + 分级放行的唯一编排入口。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        reviewer: ReviewerLike | None = None,
        editor: EditorLike | None = None,
        policy: AutoApprovePolicy = AutoApprovePolicy.GRADE_A,
        paths: StudioPaths | None = None,
        log: LogSink | None = None,
        tasks: TaskService | None = None,
    ) -> None:
        """``reviewer`` / ``editor`` 可省 —— 确认闸不需要任何 Agent。

        ``decide_approval`` / ``rescue_task`` 只碰 ``tasks`` / ``approvals`` /
        ``audit_ops`` / ``topic_candidates``，是**纯业务规则**。REST 面只用这两个
        能力，省掉 Agent 依赖后，API 进程就不必为了"点一个按钮"把 LLM 网关
        整条依赖链拉起来（§04.4.4）。审稿流水线由写稿池 worker 自己造服务。

        省掉之后调 ``review()`` 会**在入口处**报错，而不是跑到一半才炸。
        """
        self._connection = connection
        self._reviewer = reviewer
        self._editor = editor
        self._policy = policy
        self._paths = paths or StudioPaths.from_env()
        self._log = log
        self._tasks = tasks or TaskService(connection)
        self._scripts = ScriptRepo(connection)
        self._reviews = ReviewRepo(connection)
        self._approvals = ApprovalRepo(connection)
        self._audit = AuditRepo(connection)
        self._topics = TopicRepo(connection)

    # ── 主流程 ──────────────────────────────────────────────────────────

    async def review(
        self,
        *,
        task_id: str,
        persona: PersonaConfig,
        trace_id: str | None = None,
        actor: str = "worker:draft#1",
    ) -> ReviewReport:
        """审稿 → 评分 → 放行/改稿/进闸/废弃。**不抛裸异常**。"""
        reviewer, _ = self._require_agents()
        trace = trace_id or new_ulid()
        task = self._tasks.get(task_id)
        payload = read_active_script(self._connection, task_id)
        if payload is None:
            raise StudioError(
                f"任务 {task_id} 还没有稿件，无法审稿",
                code=ErrorCode.REVIEW_SCRIPT_MISSING,
                context={"task_id": task_id, "status": task.status.value},
                remediation="先跑 `studio script draft <topic_id>` 产出稿件",
            )
        script_row, sentence_rows = payload

        if task.status in {TaskStatus.DRAFTING, TaskStatus.EDITING}:
            # ``editing`` 也要归一：人工退回（§04.4.4）之后稿件是被**改稿工人**
            # 重新交回来的，它可能没来得及自己把状态推回 ``reviewing``。
            # 少了这一句，紧接着的 ``_apply_action`` 就会从 ``editing`` 直接
            # 往 ``queued_voice`` 跳 —— 那是一条不存在的边，任务当场炸成 failed。
            self._tasks.transition(task_id, TaskStatus.REVIEWING, actor=actor, reason="开始审稿")

        rules = ScriptRules.from_persona(persona)
        outline = outline_from_row(script_row)
        ctx = AgentContext(task_id=task_id, persona=persona, trace_id=trace)
        warnings: list[str] = []
        result: ReviewOutput | None = None
        approval_id: str | None = None
        action = GateAction.HUMAN_GATE

        for _ in range(1 + rules_revision_limit()):
            current = writer_output_from_rows(script_row, sentence_rows)
            round_no = script_row.revision_round + 1
            detail = evaluate_rule_channel(
                body_md=current.body_md,
                hook=current.hook,
                cta=current.cta,
                est_duration_ms=estimate_duration_ms(script_row.word_count),
                catchphrases_hit=len(catchphrase_hits(current.body_md, persona.catchphrases)),
                forbidden=list(persona.forbidden),
                rules=rules,
            )
            reviewer_result = await reviewer.run(
                ctx,
                ReviewerInput(
                    topic=topic_spec_of(script_row),
                    script=current,
                    outline=outline,
                    rule_detail=detail,
                    round_no=round_no,
                ),
            )
            if not reviewer_result.ok or reviewer_result.data is None:
                return self._failed(
                    task_id=task_id,
                    code=reviewer_result.error_code or str(ErrorCode.REVIEW_FAILED),
                    message=reviewer_result.error_message or "Reviewer 未返回结论",
                    warnings=reviewer_result.warnings,
                    actor=actor,
                )

            review = assemble(
                detail=detail,
                llm=reviewer_result.data,
                round_no=round_no,
                policy=self._policy,
            )
            self._reviews.insert(
                task_id=task_id,
                script_id=script_row.id,
                round_no=round_no,
                rule_total=review.rule_total,
                rule_detail=review.rule_detail.model_dump(mode="json"),
                llm_total=review.llm_total,
                llm_detail=review.llm_detail.model_dump(mode="json"),
                total=review.total,
                grade=review.grade.value,
                decision=review.decision.value,
                issues=[item.model_dump(mode="json") for item in review.issues],
                llm_model=reviewer_result.model or None,
                prompt_version=reviewer_result.prompt_version or None,
            )
            warnings = [*warnings, *reviewer_result.warnings]
            action = gate_action(review.grade, self._policy, round_no=round_no)
            self._emit_review(task_id=task_id, review=review, action=action)
            result = review

            if action is not GateAction.EDIT:
                break

            edited = await self._edit(
                ctx=ctx,
                task_id=task_id,
                script_row=script_row,
                sentence_rows=sentence_rows,
                outline=outline,
                review=review,
                persona=persona,
                round_no=round_no,
                actor=actor,
            )
            if isinstance(edited, ReviewReport):
                return edited  # 改稿失败 ⇒ 已经把任务置 failed
            warnings = [*warnings, *edited.warnings]
            script_row = edited.script_row
            sentence_rows = edited.sentence_rows

        assert result is not None  # 循环至少执行一次

        approval_id = self._apply_action(
            task_id=task_id,
            review=result,
            action=action,
            script_row=script_row,
            actor=actor,
        )
        last_review = self._reviews.latest_for_task(task_id)
        return ReviewReport(
            ok=True,
            task_id=task_id,
            status=self._tasks.get(task_id).status.value,
            action=action.value,
            grade=result.grade.value,
            decision=result.decision.value,
            total=result.total,
            rule_total=result.rule_total,
            llm_total=result.llm_total,
            round_no=result.round_no,
            revision_round=script_row.revision_round,
            script_id=script_row.id,
            version=script_row.version,
            review_id=last_review.id if last_review is not None else None,
            approval_id=approval_id,
            issues=[item.model_dump(mode="json") for item in result.issues],
            warnings=warnings,
        )

    # ── 改稿 ────────────────────────────────────────────────────────────

    async def _edit(
        self,
        *,
        ctx: AgentContext,
        task_id: str,
        script_row: ScriptRow,
        sentence_rows: Sequence[SentenceRow],
        outline: DirectorOutput,
        review: ReviewOutput,
        persona: PersonaConfig,
        round_no: int,
        actor: str,
    ) -> _Edited | ReviewReport:
        """进 Editor 改一轮 ⇒ 落新版稿件 ⇒ 回到 ``reviewing``。"""
        _, editor = self._require_agents()
        self._tasks.transition(task_id, TaskStatus.EDITING, actor=actor, reason="审稿未过，改稿")
        current = writer_output_from_rows(script_row, sentence_rows)
        editor_result = await editor.run(
            ctx,
            EditorInput(
                topic=topic_spec_of(script_row),
                script=current,
                outline=outline,
                issues=review.issues,
                round_no=min(round_no, rules_revision_limit()),
            ),
        )
        if not editor_result.ok or editor_result.data is None:
            return self._failed(
                task_id=task_id,
                code=editor_result.error_code or str(ErrorCode.EDIT_FAILED),
                message=editor_result.error_message or "Editor 未返回改稿",
                warnings=editor_result.warnings,
                actor=actor,
            )

        rules = ScriptRules.from_persona(persona)
        draft, report = build_draft(
            outline=outline,
            output=editor_result.data.script,
            catchphrases=persona.catchphrases,
            forbidden=persona.forbidden,
            rules=rules,
        )
        saved = self._scripts.save_draft(
            task_id=task_id,
            title=draft.title,
            hook=draft.hook,
            body_md=draft.body_md,
            cta=draft.cta,
            word_count=draft.word_count,
            est_duration_ms=draft.est_duration_ms,
            speaker_ratio=draft.speaker_ratio,
            outline=outline.model_dump(mode="json"),
            sentences=[_sentence_payload(item) for item in draft.sentences],
            target_chars=persona.target_chars_min,
            llm_model=editor_result.model or None,
            prompt_version=editor_result.prompt_version or None,
            revision_round=round_no,
            editor_notes=editor_result.data.changes,
        )
        self._tasks.transition(task_id, TaskStatus.REVIEWING, actor=actor, reason="改稿完成，重审")
        self._emit(
            "info",
            f"改稿完成：第 {round_no} 轮（{draft.word_count} 字）",
            payload={
                "task_id": task_id,
                "script_id": saved.script_id,
                "round_no": round_no,
                "changes": list(editor_result.data.changes),
            },
        )
        fresh = read_active_script(self._connection, task_id)
        assert fresh is not None  # 刚写完
        warnings = [
            *editor_result.warnings,
            *report.warnings,
            *(f"script:{problem}" for problem in report.problems),
        ]
        return _Edited(script_row=fresh[0], sentence_rows=fresh[1], warnings=warnings)

    # ── 四种去向 ────────────────────────────────────────────────────────

    def _apply_action(
        self,
        *,
        task_id: str,
        review: ReviewOutput,
        action: GateAction,
        script_row: ScriptRow,
        actor: str,
    ) -> str | None:
        """把 :class:`GateAction` 落成状态迁移 + 留痕；返回 ``approvals.id``（若有）。"""
        if action is GateAction.AUTO_PASS:
            signature = auto_approved_by(review.grade, self._policy)
            self._tasks.transition(
                task_id,
                TaskStatus.QUEUED_VOICE,
                actor="auto",
                reason=f"{review.grade.value} 级自动放行（{signature}）",
                approved_by=signature,
                detail={"grade": review.grade.value, "total": review.total},
            )
            approval = self._approvals.record_auto_approval(
                task_id=task_id,
                script_id=script_row.id,
                grade=review.grade.value,
                score_total=review.total,
                revision_round=script_row.revision_round,
                decided_by=signature,
            )
            self._audit.record(
                actor="auto",
                actor_ref=signature,
                action="task.approve",
                target_type="task",
                target_id=task_id,
                task_id=task_id,
                after={"status": TaskStatus.QUEUED_VOICE.value, "grade": review.grade.value},
                reason=f"{review.grade.value} 级自动放行",
                source="auto",
            )
            self._emit(
                "info",
                f"{review.grade.value} 级自动放行（{review.total} 分）⇒ 待配音",
                payload={"task_id": task_id, "approval_id": approval.id, "by": signature},
            )
            return approval.id

        if action is GateAction.DISCARD:
            self._tasks.transition(
                task_id,
                TaskStatus.DISCARDED,
                actor="auto",
                reason=f"{review.grade.value} 级废弃（{review.total} 分）",
                detail={"grade": review.grade.value, "total": review.total},
            )
            topic = self._topics.get_by_task(task_id)
            if topic is not None:
                self._topics.set_status(topic_id=topic.id, status="rejected")
            self._audit.record(
                actor="auto",
                actor_ref="score_discard",
                action="task.discard",
                target_type="task",
                target_id=task_id,
                task_id=task_id,
                before={"topic_status": None if topic is None else topic.status},
                after={"status": TaskStatus.DISCARDED.value, "grade": review.grade.value},
                reason=f"{review.grade.value} 级废弃",
                source="auto",
            )
            self._emit(
                "warn",
                f"{review.grade.value} 级废弃（{review.total} 分）⇒ 选题候选置 rejected（可人工捞回）",
                payload={"task_id": task_id, "topic_id": None if topic is None else topic.id},
            )
            return None

        # HUMAN_GATE：唯一的人工节点
        self._tasks.transition(
            task_id,
            TaskStatus.AWAITING_APPROVAL,
            actor="auto",
            reason=f"{review.grade.value} 级待人工确认（{review.total} 分）",
            detail={"grade": review.grade.value, "total": review.total},
        )
        approval = self._approvals.request(
            task_id=task_id,
            script_id=script_row.id,
            grade=review.grade.value,
            score_total=review.total,
            revision_round=script_row.revision_round,
        )
        self._emit(
            "info",
            f"{review.grade.value} 级进入确认闸（{review.total} 分，改稿 {script_row.revision_round} 轮）",
            payload={
                "task_id": task_id,
                "approval_id": approval.id,
                # §04.4.4：`approval.requested` 必须携带这三样 —— 少了它们，
                # 确认闸就只是「盲点通过」（原文 §2.2⑦ 的「稿件 + 评分 + 修改次数」）。
                "grade": review.grade.value,
                "score_total": review.total,
                "revision_round": script_row.revision_round,
            },
            event_kind=EventKind.APPROVAL_REQUESTED,
        )
        return approval.id

    # ── 内部 ────────────────────────────────────────────────────────────

    def _failed(
        self,
        *,
        task_id: str,
        code: str,
        message: str,
        warnings: Sequence[str],
        actor: str,
    ) -> ReviewReport:
        self._emit(
            "error",
            f"审稿失败：{message}",
            payload={"task_id": task_id, "error_code": code},
        )
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
            logger.warning("review_fail_skipped", task_id=task_id, error=repr(exc))
        return ReviewReport(
            ok=False,
            task_id=task_id,
            status=TaskStatus.FAILED.value,
            warnings=list(warnings),
            error_code=code,
            error_message=message,
        )

    def _emit_review(self, *, task_id: str, review: ReviewOutput, action: GateAction) -> None:
        self._emit(
            "info",
            f"审稿第 {review.round_no} 轮：{review.total} 分（{review.grade.value} 级）"
            f"｜规则 {review.rule_total} × 0.3 + LLM {review.llm_total} × 0.7",
            payload={
                "task_id": task_id,
                "round_no": review.round_no,
                "grade": review.grade.value,
                "total": review.total,
                "action": action.value,
                "issues": len(review.issues),
            },
        )

    # ── 确认闸（§04.4.4 · 全流程唯一人工节点）────────────────────────────

    def decide_approval(
        self,
        *,
        task_id: str,
        decision: ApprovalDecision,
        comment: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> ApprovalOutcome:
        """人工决断：确认 ⇒ 配音池 / 退回 ⇒ 改稿 / 放弃 ⇒ 废弃。

        为什么规则写在服务层而不是 REST 控制器里
        ----------------------------------------
        §04.4.4 的四条不变量（唯一人工节点 / 自动放行必须留痕 / **退回必填意见** /
        不阻塞他人）是**业务规则**，不是 HTTP 细节。写在控制器里，等于让每个新入口
        （WebUI、CLI、批量、未来的 API 版本）各抄一遍 —— 而"抄漏一条"的后果是
        一条没有意见的退回，下一轮 Editor 拿到空 issues 只能瞎改。

        为什么**抛异常**而不是返回 ``ok=False``
        --------------------------------------
        这里的失败是"这次点击不合法"（不在闸里 / 已被别人处理 / 退回没写意见），
        必须原样告诉按按钮的人；吞成 ``ok=False`` 只会让前端显示一个没有理由的失败。
        """
        task = self._tasks.get(task_id)
        if task.status is not TaskStatus.AWAITING_APPROVAL:
            raise StudioError(
                f"任务 {task_id} 不在确认闸里（当前 {task.status.value}），无法决断",
                code=ErrorCode.APPROVAL_NOT_PENDING,
                context={"task_id": task_id, "status": task.status.value},
                remediation="只有 `awaiting_approval` 的任务需要人工决断",
            )
        pending = self._approvals.pending_for_task(task_id)
        if pending is None:
            raise StudioError(
                f"任务 {task_id} 的待审记录已经不存在了（可能已被处理）",
                code=ErrorCode.APPROVAL_NOT_PENDING,
                context={"task_id": task_id, "status": task.status.value},
                remediation="刷新确认闸列表，看这条是不是已经被决断过",
            )

        note = (comment or "").strip()
        if decision is ApprovalDecision.REJECT and not note:
            raise StudioError(
                "退回必须写清理由：下一轮改稿只认这条意见",
                code=ErrorCode.APPROVAL_COMMENT_REQUIRED,
                context={"task_id": task_id, "approval_id": pending.id},
                remediation="写一条具体意见（改哪一句、往哪个方向改）",
            )

        decided = self._approvals.decide(
            pending.id, status=decision.value, decided_by=actor, comment=note or None
        )
        if decided is None:
            raise StudioError(
                f"这条待审已经被别人抢先决断（approval_id={pending.id}）",
                code=ErrorCode.APPROVAL_NOT_PENDING,
                context={"task_id": task_id, "approval_id": pending.id},
                remediation="刷新页面看最新的决断结果，不要覆盖别人的意见",
            )

        target = _DECISION_TARGET[decision]
        reason = _reason_for(decision, pending.grade, pending.score_total, note)
        self._tasks.transition(
            task_id,
            target,
            actor=actor,
            reason=reason,
            approved_by=actor if decision is ApprovalDecision.APPROVE else None,
            bump_revision=decision is ApprovalDecision.REJECT,
            detail={
                "approval_id": pending.id,
                "decision": decision.value,
                "grade": pending.grade,
                "score_total": pending.score_total,
                "comment": note or None,
            },
        )
        self._audit.record(
            actor="user",
            actor_ref=actor,
            action=f"task.{decision.name.lower()}",
            target_type="task",
            target_id=task_id,
            task_id=task_id,
            before={"status": TaskStatus.AWAITING_APPROVAL.value, "grade": pending.grade},
            after={"status": target.value, "comment": note or None},
            reason=note or None,
            source=source,
        )
        after = self._tasks.get(task_id)
        self._emit(
            "info",
            f"确认闸决断：{decision.value} ⇒ {target.value}（{pending.grade} 级 "
            f"{pending.score_total} 分，改稿 {after.revision_round} 轮）",
            payload={
                "task_id": task_id,
                "approval_id": pending.id,
                "decision": decision.value,
                "status": target.value,
                "grade": pending.grade,
                "score_total": pending.score_total,
                "revision_round": after.revision_round,
            },
            event_kind=EventKind.APPROVAL_DECIDED,
        )
        return ApprovalOutcome(
            task_id=task_id,
            approval_id=pending.id,
            decision=decision.value,
            status=target.value,
            grade=pending.grade,
            score_total=pending.score_total,
            revision_round=after.revision_round,
            comment=note or None,
        )

    def rescue_task(
        self,
        *,
        task_id: str,
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> RescueOutcome:
        """捞回：``discarded`` / ``canceled`` → ``pending``（todolist T4.4 的降级预案）。

        "误点放弃"是**唯一**人工节点上最容易犯的错，而放弃会把任务推进终局态。
        所以状态机早就留了 ``DISCARDED → PENDING`` 这条边（T1.5），这里只是把它
        接上人工入口。

        为什么顺带恢复选题候选
        ----------------------
        ``review`` 的自动废弃分支会把 ``topic_candidates.status`` 置 ``rejected``
        （"这稿不行"同时也是"这个选题不行"）。只把任务捞回来、选题仍躺在
        ``rejected`` 里，下一轮选题分析就看不到它了 —— 捞回只捞回了一半。
        恢复成 ``queued`` 而不是 ``selected``：任务已经在跑了，它属于"已入队"。
        """
        task = self._tasks.get(task_id)
        if task.status not in RESCUE_STATUSES:
            raise StudioError(
                f"任务 {task_id} 不在可捞回状态（当前 {task.status.value}）",
                code=ErrorCode.APPROVAL_NOT_PENDING,
                context={"task_id": task_id, "status": task.status.value},
                remediation="只有被放弃（discarded）或取消（canceled）的任务需要捞回",
            )
        note = (reason or "").strip() or "人工捞回：重新走流程"
        self._tasks.transition(
            task_id,
            TaskStatus.PENDING,
            actor=actor,
            reason=note,
            detail={"rescued_from": task.status.value},
        )
        topic = self._topics.get_by_task(task_id)
        restored = topic is not None and topic.status == "rejected"
        if restored and topic is not None:
            self._topics.set_status(topic_id=topic.id, status="queued")
        self._audit.record(
            actor="user",
            actor_ref=actor,
            action="task.rescue",
            target_type="task",
            target_id=task_id,
            task_id=task_id,
            before={"status": task.status.value},
            after={
                "status": TaskStatus.PENDING.value,
                "topic_status": "queued" if restored else None,
            },
            reason=note,
            source=source,
        )
        after = self._tasks.get(task_id)
        self._emit(
            "warn",
            f"捞回：{task.status.value} ⇒ {TaskStatus.PENDING.value}（改稿 {after.revision_round} 轮）",
            payload={
                "task_id": task_id,
                "topic_id": None if topic is None else topic.id,
                "topic_restored": restored,
            },
        )
        return RescueOutcome(
            task_id=task_id,
            status=after.status.value,
            previous_status=task.status.value,
            revision_round=after.revision_round,
            topic_id=None if topic is None else topic.id,
            topic_restored=restored,
        )

    def _require_agents(self) -> tuple[ReviewerLike, EditorLike]:
        """取审稿 / 改稿 Agent；没注入 ⇒ **在入口处**报错，而不是跑到一半才炸。

        为什么不做成构造期的硬要求：确认闸（``decide_approval`` / ``rescue_task``）
        是纯业务规则，REST 面只需要它 —— 让 API 进程为了「点一个按钮」去装配
        LLM 网关，是把依赖方向搞反了。
        """
        if self._reviewer is None or self._editor is None:
            raise StudioError(
                "这个 ReviewService 实例没有注入 Reviewer/Editor，无法跑审稿流水线",
                code=ErrorCode.INTERNAL,
                remediation="REST 面只提供确认闸；审稿请走写稿池 worker",
            )
        return self._reviewer, self._editor

    def _emit(
        self,
        level: Severity,
        message: str,
        *,
        payload: Mapping[str, Any] | None = None,
        event_kind: EventKind | None = None,
    ) -> None:
        """写一条 ``review.pipeline`` 日志；``event_kind`` 非空 ⇒ 额外扇出一条 WS 事件。

        事件名落在 ``payload[EVENT_PAYLOAD_KEY]`` 上，Hub 读到它就会在
        ``log.appended`` 之外**再扇出一条**同名事件（走该事件自己的通道与合并策略）。
        这样"面板要立刻反应"的事件搭日志的车跨进程，不需要第二套 IPC。
        """
        merged = dict(payload or {})
        if event_kind is not None:
            merged[EVENT_PAYLOAD_KEY] = event_kind.value
        if self._log is not None:
            # `task_id` 同时进**列**与 payload：列是日志面板按任务过滤的依据
            # （`GET /api/v1/logs?task_id=`），只有 payload 带着它的话，
            # 「只看这条任务的日志」会把审稿这一段整段漏掉。
            task_id = merged.get("task_id")
            self._log(
                level=level,
                source="review.pipeline",
                message=message,
                task_id=task_id if isinstance(task_id, str) else None,
                payload=merged,
            )
            return
        # 兜底走 structlog：payload 是自由字典，而保留键（`event` / `message` …）
        # 展开成 kwargs 会直接 TypeError。挡一道 —— 日志不该把主链路带崩。
        extra = {key: value for key, value in merged.items() if key not in _LOG_RESERVED}
        if level in {"warn", "error", "fatal"}:
            logger.warning(message, source="review.pipeline", **extra)
        else:
            logger.info(message, source="review.pipeline", **extra)


@dataclass(frozen=True, slots=True)
class _Edited:
    """一轮改稿的产物（新版稿件行 + 它的句子）。"""

    script_row: ScriptRow
    sentence_rows: list[SentenceRow]
    warnings: list[str] = field(default_factory=list)


def _reason_for(
    decision: ApprovalDecision,
    grade: str | None,
    score_total: float | None,
    note: str,
) -> str:
    """写进 ``task_events.reason`` 的一句话（退回时就是人工那条意见本身）。"""
    if decision is ApprovalDecision.APPROVE:
        return f"人工确认放行（{grade} 级 {score_total} 分）"
    if decision is ApprovalDecision.REJECT:
        return note
    return note or "人工放弃"


def rules_revision_limit() -> int:
    """改稿轮次上限（``scoring.REVISION_LIMIT`` 的本地别名，便于单测替换）。"""
    return REVISION_LIMIT


def assemble(
    *,
    detail: Any,
    llm: ReviewerOutput,
    round_no: int,
    policy: AutoApprovePolicy,
) -> ReviewOutput:
    """把"服务端规则明细 + LLM 六维度"拼成一份完整结论（**定级只在这里发生**）。"""
    rule_total = rule_total_of(detail)
    llm_total = llm_total_of(llm.llm_detail)
    total, grade = compute_score(rule_total, llm_total)
    return ReviewOutput(
        rule_total=rule_total,
        rule_detail=detail,
        llm_total=llm_total,
        llm_detail=llm.llm_detail,
        issues=list(llm.issues),
        total=total,
        grade=grade,
        decision=decision_for(gate_action(grade, policy, round_no=round_no)),
        round_no=round_no,
    )


def writer_output_from_rows(
    script_row: ScriptRow,
    sentence_rows: Sequence[SentenceRow],
) -> WriterOutput:
    """``scripts`` + ``script_sentences`` 行 → :class:`WriterOutput`（喂回 Agent 用）。

    **不重新读库**：调用方手上已经有行，再查一次只会让"审的是哪一版"变得含糊。
    """
    return WriterOutput(
        title=script_row.title or "（无标题）",
        hook=script_row.hook or "（无开场）",
        body_md=script_row.body_md,
        cta=script_row.cta or "（无结尾）",
        sentences=[
            SentenceSpec(
                seq=row.seq,
                text=row.text,
                speaker=row.speaker,  # type: ignore[arg-type]
                emotion=row.emotion,
                pause_after_ms=row.pause_after_ms,
            )
            for row in sentence_rows
        ]
        or [SentenceSpec(seq=1, text=script_row.hook or "（无开场）", speaker="narrator")],
        est_duration_ms=estimate_duration_ms(script_row.word_count),
        catchphrases_used=list(script_row.review.get("catchphrases_used", [])),
    )


def outline_from_row(script_row: ScriptRow) -> DirectorOutput:
    """``scripts.outline_json`` → :class:`DirectorOutput`。

    坏数据（人工改过库 / 老版本没写）⇒ :class:`StudioError`：审稿的"定位契合"
    这一维度要看大纲，没有大纲就没有参照物 —— 与其拿一个空大纲糊过去，
    不如直接说清是哪条任务的大纲坏了。
    """
    try:
        return DirectorOutput.model_validate(script_row.outline)
    except ValidationError as exc:
        raise StudioError(
            f"任务 {script_row.task_id} 的大纲数据不可用，无法审稿",
            code=ErrorCode.REVIEW_FAILED,
            context={"task_id": script_row.task_id, "script_id": script_row.id, "error": str(exc)},
            remediation="重新跑写稿（`studio script draft`）生成一版带完整大纲的稿件",
        ) from exc


def topic_spec_of(script_row: ScriptRow) -> TopicSpec:
    """从稿件行反推审稿/改稿提示词里要用的选题信息。

    ``scripts`` 表**没有**选题字段（选题在 ``topic_candidates``），所以这里只带
    标题与大纲里的角度 —— 提示词里"选题"这一块是给模型定调的，不是机器判据。
    """
    return TopicSpec(
        title=script_row.title or "（无标题）",
        hook_type="other",
        angle=script_row.hook or "",
        exec_feasible=True,
        score=0.0,
        reason="（审稿阶段不重新评分选题）",
    )


def read_latest_review(connection: sqlite3.Connection, task_id: str) -> ReviewRow | None:
    """读最近一次审稿结论（WebUI 稿件面板读的就是它）。

    与 :func:`~studio.services.script_service.read_active_script` 同一手法：
    读路径做成模块级函数，不为了读一份分数去凑 Reviewer/Editor。
    """
    return ReviewRepo(connection).latest_for_task(task_id)
