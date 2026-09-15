"""T1.11 验收：确认闸端到端（§04.4.4）—— 全流程**唯一**人工节点。

四条验收（todolist T1.11）
-------------------------
① A 级自动放行并写 ``approved_by='auto_approve_A'``；
② B 级进 ``awaiting_approval``；
③ reject **必填 comment** 且 ``revision_round + 1``；
④ 改稿 2 轮后仍 B ⇒ 进闸（**不无限循环**）。

为什么 REST 层不在这个文件里
----------------------------
§04.4.4 的 REST 上行（``POST /tasks/{id}/approve|reject|discard|approve_batch``）
是 T4.4 的交付物；但**规则本身**（唯一人工节点、退回必填意见、``revision_round``
递增、逐条留痕）是 T1.11 的交付物，住在
``ReviewService.decide_approval`` 里。所以这里直接调它 —— T4.4 的控制器到时候
只负责把它翻译成 HTTP，规则不再抄第二遍。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.unit.agents.fakes import Reply, ScriptedTransport, agent_gateway, persona

from studio.agents.editor import EditorAgent
from studio.agents.prompts import PromptLibrary
from studio.agents.reviewer import ReviewerAgent
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import ApprovalRepo, AuditRepo, ScriptRepo
from studio.domain import ApprovalDecision, TaskService, TaskStatus
from studio.domain.enums import AutoApprovePolicy
from studio.domain.scoring import LLM_DIMENSIONS
from studio.domain.script import DirectorOutput, ScriptSegment, estimate_duration_ms
from studio.domain.text import count_chars
from studio.services import ReviewReport, ReviewService

REPO_ROOT = Path(__file__).resolve().parents[2]

CATCH_LINE = "这不科学，俺寻思也是。"
SENTENCE_TEXT = "这不科学，熊大又跑起来了。"

ISSUE: dict[str, Any] = {
    "code": "weak_hook",
    "severity": "major",
    "target": "hook",
    "detail": "开场太平",
    "suggestion": "把结果前置",
}

#: 一次 B 级审稿的分数：规则 10.0 × 0.3 + LLM 6.0 × 0.7 = 7.2（B）
B_SCORE = 6.0


# ══════════════════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════════════════


def body_of(chars: int, *, tail: str = "") -> str:
    """**恰好** ``chars`` 口播字的正文（``tail`` 计入 —— 它也是要念出来的字）。"""
    return CATCH_LINE + "熊" * (chars - count_chars(CATCH_LINE) - count_chars(tail)) + tail


def duplicated(line_chars: int, *, tail: str = "") -> str:
    """两段一模一样的正文 ⇒ 段落重复度 0.5（扣 2.0 分）。"""
    line = body_of(line_chars, tail=tail)
    return f"{line}\n{line}"


def outline_json() -> dict[str, Any]:
    return DirectorOutput(
        hook_3s="熊大又整活了",
        segments=[
            ScriptSegment(seq=index, point=f"要点{index}", visual=f"画面{index}", mood="兴奋", est_chars=200)
            for index in range(1, 4)
        ],
        cta="点个关注看下集",
        est_duration_ms=140_000,
    ).model_dump(mode="json")


def sentence_rows(count: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "seq": index,
            "text": SENTENCE_TEXT,
            "speaker": "bigbear" if index % 2 else "littlebear",
            "emotion": "兴奋",
            "pause_after_ms": 200,
        }
        for index in range(1, count + 1)
    ]


def seed_task(connection: sqlite3.Connection) -> str:
    tasks = TaskService(connection)
    task_id = tasks.create(title="MC跑酷最难的一跳").id
    tasks.transition(task_id, TaskStatus.DRAFTING, actor="test")
    return task_id


def seed_script(
    connection: sqlite3.Connection, task_id: str, body_md: str, *, revision_round: int = 0
) -> str:
    rows = sentence_rows()
    saved = ScriptRepo(connection).save_draft(
        task_id=task_id,
        title="MC跑酷最难的一跳",
        hook="熊大又整活了",
        body_md=body_md,
        cta="点个关注看下集",
        word_count=count_chars(body_md),
        est_duration_ms=estimate_duration_ms(count_chars(body_md)),
        speaker_ratio={"bigbear": 0.5, "littlebear": 0.5},
        outline=outline_json(),
        revision_round=revision_round,
        editor_notes=["按人工意见改了一轮"],
        sentences=[
            {
                "seq": row["seq"],
                "text_raw": row["text"],
                "text": row["text"],
                "speaker": row["speaker"],
                "emotion": row["emotion"],
                "pause_after_ms": row["pause_after_ms"],
            }
            for row in rows
        ],
    )
    return saved.script_id


def reviewer_json(score: float) -> str:
    return json.dumps(
        {
            "llm_detail": {name: {"score": score, "comment": f"{name} 的评语"} for name in LLM_DIMENSIONS},
            "issues": [ISSUE],
        },
        ensure_ascii=False,
    )


def editor_json(*, body_md: str) -> str:
    return json.dumps(
        {
            "script": {
                "title": "MC跑酷最难的一跳",
                "hook": "熊大这一跳直接封神",
                "body_md": body_md,
                "cta": "点个关注看下集",
                "sentences": sentence_rows(),
                "est_duration_ms": estimate_duration_ms(count_chars(body_md)),
                "catchphrases_used": ["这不科学", "俺寻思"],
            },
            "changes": ["把开场换成结果前置"],
        },
        ensure_ascii=False,
    )


# ══════════════════════════════════════════════════════════════════════
# 装配
# ══════════════════════════════════════════════════════════════════════


@dataclass
class Harness:
    connection: sqlite3.Connection
    transport: ScriptedTransport
    service: ReviewService

    @property
    def calls(self) -> int:
        return len(self.transport.calls)


def build(
    paths: StudioPaths,
    connection: sqlite3.Connection,
    *replies: str | Reply,
    policy: AutoApprovePolicy = AutoApprovePolicy.GRADE_A,
) -> Harness:
    transport = ScriptedTransport(
        replies=[item if isinstance(item, Reply) else Reply(text=item) for item in replies]
    )
    gateway = agent_gateway(paths, connection, transport, agents=["reviewer", "editor"])
    prompts = PromptLibrary.load(paths.prompts_dir)
    service = ReviewService(
        connection,
        reviewer=ReviewerAgent(gateway, prompts),
        editor=EditorAgent(gateway, prompts),
        policy=policy,
        paths=paths,
    )
    return Harness(connection=connection, transport=transport, service=service)


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    return StudioPaths(home=REPO_ROOT, data_dir=tmp_path / "data")


@pytest.fixture
def connection(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


async def drive_to_the_gate(
    paths: StudioPaths, connection: sqlite3.Connection
) -> tuple[Harness, str, ReviewReport]:
    """把一个 B 级任务推到确认闸（改稿 2 轮后仍 B ⇒ 验收④ 顺手也验了）。"""
    task_id = seed_task(connection)
    seed_script(connection, task_id, duplicated(350))
    harness = build(
        paths,
        connection,
        reviewer_json(B_SCORE),
        editor_json(body_md=body_of(700)),
        reviewer_json(B_SCORE),
        editor_json(body_md=body_of(700)),
        reviewer_json(B_SCORE),
    )
    report = await harness.service.review(task_id=task_id, persona=persona())
    return harness, task_id, report


def approvals(connection: sqlite3.Connection) -> ApprovalRepo:
    return ApprovalRepo(connection)


# ══════════════════════════════════════════════════════════════════════
# ① A 级自动放行
# ══════════════════════════════════════════════════════════════════════


class TestAutoPass:
    async def test_an_a_grade_is_signed_auto_approve_a(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(350))
        await build(paths, connection, reviewer_json(9.0)).service.review(task_id=task_id, persona=persona())

        task = TaskService(connection).get(task_id)
        assert task.status is TaskStatus.QUEUED_VOICE
        assert task.approved_by == "auto_approve_A"
        assert task.approved_at is not None

    async def test_the_auto_pass_leaves_an_approval_and_an_audit_row(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """§04.4.4 不变量 2：自动放行**必须**留痕。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(350))
        await build(paths, connection, reviewer_json(9.0)).service.review(task_id=task_id, persona=persona())

        assert approvals(connection).count_by_status() == {"approved": 1}
        row = approvals(connection).list_for_task(task_id)[0]
        assert (row.decided_by, row.grade) == ("auto_approve_A", "A")

        audit = AuditRepo(connection).list_for_task(task_id)
        assert [(item.actor, item.actor_ref, item.action) for item in audit] == [
            ("auto", "auto_approve_A", "task.approve")
        ]

    async def test_an_auto_passed_task_is_never_gated(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(350))
        await build(paths, connection, reviewer_json(9.0)).service.review(task_id=task_id, persona=persona())
        assert approvals(connection).pending_for_task(task_id) is None


# ══════════════════════════════════════════════════════════════════════
# ② B 级进闸
# ══════════════════════════════════════════════════════════════════════


class TestHumanGate:
    async def test_a_b_grade_waits_for_a_human(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        _, task_id, report = await drive_to_the_gate(paths, connection)

        assert report.ok
        assert (report.action, report.grade) == ("human_gate", "B")
        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL

    async def test_the_pending_row_presents_the_three_facts(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """原文 §2.2⑦："呈现稿件 + 评分 + 修改次数" —— 少了它们就是盲点通过。"""
        _, task_id, report = await drive_to_the_gate(paths, connection)

        pending = approvals(connection).pending_for_task(task_id)
        assert pending is not None and pending.id == report.approval_id
        assert (pending.grade, pending.score_total, pending.revision_round) == ("B", 7.2, 2)
        assert pending.decided_at is None and pending.decided_by is None

    async def test_the_gate_blocks_nobody_else(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """§04.4.4 不变量 4：闸只影响当前任务的状态迁移。"""
        _, gated, _ = await drive_to_the_gate(paths, connection)

        other = seed_task(connection)
        seed_script(connection, other, duplicated(350))
        await build(paths, connection, reviewer_json(9.0)).service.review(task_id=other, persona=persona())

        assert TaskService(connection).get(gated).status is TaskStatus.AWAITING_APPROVAL
        assert TaskService(connection).get(other).status is TaskStatus.QUEUED_VOICE


# ══════════════════════════════════════════════════════════════════════
# ③ 人工决断
# ══════════════════════════════════════════════════════════════════════


class TestApprove:
    async def test_approve_sends_the_task_to_the_voice_pool(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        outcome = build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.APPROVE
        )

        assert outcome.status == "queued_voice"
        task = TaskService(connection).get(task_id)
        assert task.status is TaskStatus.QUEUED_VOICE
        assert task.approved_by == "user", "人工放行的署名必须能跟 auto_approve_* 区分开"

    async def test_approve_closes_the_pending_row(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        _, task_id, report = await drive_to_the_gate(paths, connection)
        build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.APPROVE, comment="这条能用"
        )

        row = approvals(connection).get(report.approval_id or "")
        assert row is not None
        assert (row.status, row.decided_by, row.comment) == ("approved", "user", "这条能用")
        assert row.decided_at is not None
        assert approvals(connection).pending_for_task(task_id) is None

    async def test_approve_writes_an_audit_row(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.APPROVE, comment="放行"
        )

        audit = AuditRepo(connection).list_for_task(task_id)
        assert [(item.actor, item.actor_ref, item.action, item.result) for item in audit] == [
            ("user", "user", "task.approve", "ok")
        ]
        assert audit[0].before == {"status": "awaiting_approval", "grade": "B"}
        assert audit[0].after["status"] == "queued_voice"


class TestReject:
    async def test_a_reject_without_a_comment_is_refused(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """★ 验收③：退回**必填**意见（§04.4.4 不变量 3）。"""
        _, task_id, _ = await drive_to_the_gate(paths, connection)

        with pytest.raises(StudioError) as excinfo:
            build(paths, connection).service.decide_approval(
                task_id=task_id, decision=ApprovalDecision.REJECT
            )

        assert excinfo.value.code is ErrorCode.APPROVAL_COMMENT_REQUIRED
        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL

    @pytest.mark.parametrize("comment", ["", "   ", "\n\t"])
    async def test_a_blank_comment_is_not_a_comment(
        self, paths: StudioPaths, connection: sqlite3.Connection, comment: str
    ) -> None:
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        with pytest.raises(StudioError) as excinfo:
            build(paths, connection).service.decide_approval(
                task_id=task_id, decision=ApprovalDecision.REJECT, comment=comment
            )
        assert excinfo.value.code is ErrorCode.APPROVAL_COMMENT_REQUIRED

    async def test_a_refused_reject_leaves_the_gate_untouched(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """规则要在**动库之前**生效：被拒的那一下不能留下任何痕迹。"""
        _, task_id, report = await drive_to_the_gate(paths, connection)
        with pytest.raises(StudioError):
            build(paths, connection).service.decide_approval(
                task_id=task_id, decision=ApprovalDecision.REJECT
            )

        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL
        row = approvals(connection).get(report.approval_id or "")
        assert row is not None and row.pending
        assert AuditRepo(connection).list_for_task(task_id) == []

    async def test_a_reject_with_a_comment_bumps_the_revision_round(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """★ 验收③的另一半：``awaiting_approval → editing`` 且 ``revision_round + 1``。"""
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        before = TaskService(connection).get(task_id).revision_round

        outcome = build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.REJECT, comment="开场还是太平，换成结果前置"
        )

        assert outcome.status == "editing"
        after = TaskService(connection).get(task_id)
        assert after.status is TaskStatus.EDITING
        assert after.revision_round == before + 1

    async def test_the_reject_comment_lands_in_the_approval_and_the_audit_row(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """§04.4.4 不变量 3：意见要能被**下一轮 Editor** 取到。"""
        _, task_id, report = await drive_to_the_gate(paths, connection)
        build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.REJECT, comment="开场还是太平"
        )

        row = approvals(connection).get(report.approval_id or "")
        assert row is not None
        assert (row.status, row.decided_by, row.comment) == ("rejected", "user", "开场还是太平")

        audit = AuditRepo(connection).list_for_task(task_id)
        assert [(item.action, item.result, item.reason) for item in audit] == [
            ("task.reject", "ok", "开场还是太平")
        ]

    async def test_the_reject_reason_is_the_humans_own_words(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.REJECT, comment="第二段太快，慢一点"
        )
        events = connection.execute(
            "SELECT reason FROM task_events WHERE task_id = ? AND to_status = 'editing' ORDER BY id",
            (task_id,),
        ).fetchall()

        assert events[-1][0] == "第二段太快，慢一点"
        assert [row[0] for row in events[:-1]] == ["审稿未过，改稿"] * (len(events) - 1)


class TestDiscard:
    async def test_discard_abandons_the_task(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        outcome = build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.DISCARD, comment="选题本身不行"
        )

        assert outcome.status == "discarded"
        assert TaskService(connection).get(task_id).status is TaskStatus.DISCARDED
        assert [item.action for item in AuditRepo(connection).list_for_task(task_id)] == ["task.discard"]

    async def test_discard_does_not_need_a_comment(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        outcome = build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.DISCARD
        )
        assert outcome.comment is None


class TestRefusals:
    async def test_deciding_a_task_that_is_not_in_the_gate_is_refused(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(350))
        with pytest.raises(StudioError) as excinfo:
            build(paths, connection).service.decide_approval(
                task_id=task_id, decision=ApprovalDecision.APPROVE
            )
        assert excinfo.value.code is ErrorCode.APPROVAL_NOT_PENDING
        assert TaskService(connection).get(task_id).status is TaskStatus.DRAFTING

    async def test_deciding_twice_is_refused(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """第二次点击不能把第二个人的意见盖在第一个人的上面。"""
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        service = build(paths, connection).service
        service.decide_approval(task_id=task_id, decision=ApprovalDecision.APPROVE, comment="第一次")

        with pytest.raises(StudioError) as excinfo:
            service.decide_approval(task_id=task_id, decision=ApprovalDecision.REJECT, comment="反悔")
        assert excinfo.value.code is ErrorCode.APPROVAL_NOT_PENDING

        assert TaskService(connection).get(task_id).status is TaskStatus.QUEUED_VOICE


# ══════════════════════════════════════════════════════════════════════
# ④ 不无限循环
# ══════════════════════════════════════════════════════════════════════


class TestNoInfiniteLoop:
    async def test_two_edit_rounds_then_the_gate(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """★ 验收④：3 次审稿 + 2 次改稿 ⇒ 进闸，**到此为止**。"""
        harness, task_id, report = await drive_to_the_gate(paths, connection)

        assert (report.round_no, report.revision_round) == (3, 2)
        assert harness.calls == 5, "1 初审 + 2 轮（改稿 + 重审）"
        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL

    async def test_a_rejected_script_is_reviewed_again_without_burning_edits(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """人工退回 ⇒ 改稿工人交回新版 ⇒ 重审；但**自动**改稿预算不重置。

        这一版是**人**要求改的（改稿工人按 reject 的意见改），所以不该再自动烧
        两轮 token；重审仍是 B ⇒ 回到人面前。这条断言防的是"退回之后悄悄自动
        改两轮"，那正是 R16 要拦的事。
        """
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.REJECT, comment="开场还是太平"
        )
        assert TaskService(connection).get(task_id).status is TaskStatus.EDITING

        # 改稿工人交回新版（人工意见驱动，不计入自动预算）
        seed_script(connection, task_id, body_of(700), revision_round=3)
        TaskService(connection).transition(
            task_id, TaskStatus.REVIEWING, actor="worker:draft#1", reason="人工意见改稿完成"
        )

        harness = build(paths, connection, reviewer_json(B_SCORE))
        report = await harness.service.review(task_id=task_id, persona=persona())

        assert (report.action, report.grade) == ("human_gate", "B")
        assert harness.calls == 1, "只重审一次，**没有**再自动改稿"
        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL
        assert approvals(connection).pending_for_task(task_id) is not None

    async def test_review_normalises_a_task_left_in_editing(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """改稿工人忘了把状态推回 ``reviewing`` 也不能炸：服务层自己归一。"""
        _, task_id, _ = await drive_to_the_gate(paths, connection)
        build(paths, connection).service.decide_approval(
            task_id=task_id, decision=ApprovalDecision.REJECT, comment="再来一轮"
        )
        seed_script(connection, task_id, body_of(700), revision_round=3)
        assert TaskService(connection).get(task_id).status is TaskStatus.EDITING

        harness = build(paths, connection, reviewer_json(B_SCORE))
        report = await harness.service.review(task_id=task_id, persona=persona())

        assert report.ok
        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL
        assert harness.calls == 1
