"""``ReviewService`` 服务层（T1.11）—— 编排纪律与四路去向。

集成测试跑的是**真实链路**（真网关 + 真提示词 + 真 schema + 真 Agent）；这里用假
Agent 把"分到哪个等级之后去哪儿"和"出岔子时怎么办"单独钉死：

- A 级 ⇒ ``queued_voice`` + 署名 + ``approvals``/``audit_ops`` 各一行；
- B 级 ⇒ 改稿（≤2 轮）⇒ 仍 B ⇒ 进确认闸；
- C 级 ⇒ ``discarded`` + 选题候选置 ``rejected``（**不删**，可人工捞回）；
- 失败 ⇒ ``ok=False`` 的 :class:`ReviewReport` + 任务置 ``failed``（不抛裸异常）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from studio.agents.base import AgentContext, AgentResult
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.core.proto import Severity
from studio.db import connect, migrate
from studio.db.repositories import (
    ApprovalRepo,
    AuditRepo,
    DirectionRepo,
    ReviewRepo,
    ScriptRepo,
    TopicRepo,
)
from studio.domain import TaskService, TaskStatus
from studio.domain.enums import AutoApprovePolicy
from studio.domain.scoring import (
    ChannelItem,
    EditorInput,
    EditorOutput,
    LlmChannelDetail,
    ReviewerInput,
    ReviewerOutput,
    ReviewIssue,
)
from studio.domain.script import DirectorOutput, ScriptSegment, SentenceSpec, WriterOutput
from studio.domain.task_service import HUMAN_GATE_KEY
from studio.services import ReviewService, read_latest_review
from tests.unit.agents.fakes import persona

LINE = "这不科学，熊大又跑起来了。"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════════════════


def outline() -> DirectorOutput:
    return DirectorOutput(
        hook_3s="熊大又整活了",
        segments=[
            ScriptSegment(seq=index, point=f"要点{index}", visual=f"画面{index}", mood="兴奋", est_chars=200)
            for index in range(1, 4)
        ],
        cta="点个关注看下集",
        est_duration_ms=140_000,
    )


def writer_output(*, hook: str = "熊大又整活了") -> WriterOutput:
    return WriterOutput(
        title="MC跑酷最难的一跳",
        hook=hook,
        body_md="这不科学，俺寻思也是。" + "熊" * 691,
        cta="点个关注看下集",
        sentences=[
            SentenceSpec(
                seq=index,
                text=LINE,
                speaker="bigbear" if index % 2 else "littlebear",
                emotion="兴奋",
                pause_after_ms=200,
            )
            for index in range(1, 21)
        ],
        est_duration_ms=140_000,
        catchphrases_used=["这不科学", "俺寻思"],
    )


def seed_task(connection: sqlite3.Connection, *, title: str = "跑酷合集") -> str:
    """建任务并推到 ``drafting``（``ReviewService`` 会自己接着推到 ``reviewing``）。"""
    tasks = TaskService(connection)
    task_id = tasks.create(title=title).id
    tasks.transition(task_id, TaskStatus.DRAFTING, actor="test")
    return task_id


def seed_script(connection: sqlite3.Connection, task_id: str, *, hook: str = "熊大又整活了") -> str:
    draft = writer_output(hook=hook)
    saved = ScriptRepo(connection).save_draft(
        task_id=task_id,
        title=draft.title,
        hook=draft.hook,
        body_md=draft.body_md,
        cta=draft.cta,
        word_count=700,
        est_duration_ms=140_000,
        speaker_ratio={"bigbear": 0.5, "littlebear": 0.5},
        outline=outline().model_dump(mode="json"),
        sentences=[
            {
                "seq": item.seq,
                "text_raw": item.text,
                "text": item.text,
                "speaker": item.speaker,
                "emotion": item.emotion,
                "pause_after_ms": item.pause_after_ms,
            }
            for item in draft.sentences
        ],
    )
    return saved.script_id


def seed_topic(connection: sqlite3.Connection, task_id: str) -> str:
    """一条绑在任务上的选题候选（C 级废弃时它应变成 ``rejected``）。"""
    directions = DirectionRepo(connection)
    (direction_id,) = directions.insert_batch(
        batch_id=directions.new_batch_id(),
        directions=[
            {
                "title": "跑酷技术流",
                "rationale": "账号定位就是跑酷",
                "grounded_on": [{"type": "persona"}],
                "priority": 100,
                "risk_flags": [],
            }
        ],
    )
    topics = TopicRepo(connection)
    (topic_id,) = topics.insert_many(
        [
            {
                "direction_id": direction_id,
                "seq": 1,
                "title": "MC跑酷最难的一跳",
                "hook_type": "suspense",
                "angle": "只讲那一跳",
                "exec_feasible": True,
                "score": 8.5,
                "reason": "钩子够硬",
            }
        ]
    )
    topics.set_status(topic_id=topic_id, status="queued", task_id=task_id)
    return topic_id


# ══════════════════════════════════════════════════════════════════════
# 假 Agent
# ══════════════════════════════════════════════════════════════════════


def channel(score: float) -> ChannelItem:
    return ChannelItem(score=score, comment="测试评语")


def llm_detail(score: float) -> LlmChannelDetail:
    return LlmChannelDetail(
        hook_opening=channel(score),
        positioning_fit=channel(score),
        oral_style=channel(score),
        emotion_rhythm=channel(score),
        ending_cta=channel(score),
        forbidden=channel(score),
    )


def issue(target: str = "hook") -> ReviewIssue:
    return ReviewIssue(
        code="weak_hook",
        severity="major",
        target=target,
        detail="开场太平",
        suggestion="把结果前置",
    )


def reviewer_output(score: float, *, issues: list[ReviewIssue] | None = None) -> ReviewerOutput:
    """规则通道固定 10.0 ⇒ ``total = 3.0 + 0.7 × score``。

    ``9.0 ⇒ 9.3 A`` / ``4.0 ⇒ 5.8 B`` / ``1.0 ⇒ 3.7 C``。
    """
    return ReviewerOutput(llm_detail=llm_detail(score), issues=[issue()] if issues is None else issues)


def editor_output(*, hook: str = "熊大又整活了") -> EditorOutput:
    return EditorOutput(script=writer_output(hook=hook), changes=["把开场换成结果前置"])


class FakeReviewer:
    """按调用顺序吐结论（用尽 ⇒ 复用最后一条）。"""

    def __init__(self, *outputs: ReviewerOutput, ok: bool = True, **extra: Any) -> None:
        self.outputs = list(outputs)
        self.ok = ok
        self.extra = extra
        self.inputs: list[ReviewerInput] = []

    async def run(self, ctx: AgentContext, payload: ReviewerInput) -> AgentResult[ReviewerOutput]:
        self.inputs.append(payload)
        data = None
        if self.ok and self.outputs:
            data = self.outputs[min(len(self.inputs) - 1, len(self.outputs) - 1)]
        return AgentResult[ReviewerOutput](ok=self.ok, data=data, **self.extra)


class FakeEditor:
    def __init__(self, *outputs: EditorOutput, ok: bool = True, **extra: Any) -> None:
        self.outputs = list(outputs)
        self.ok = ok
        self.extra = extra
        self.inputs: list[EditorInput] = []

    async def run(self, ctx: AgentContext, payload: EditorInput) -> AgentResult[EditorOutput]:
        self.inputs.append(payload)
        data = None
        if self.ok and self.outputs:
            data = self.outputs[min(len(self.inputs) - 1, len(self.outputs) - 1)]
        return AgentResult[EditorOutput](ok=self.ok, data=data, **self.extra)


class FakeLog:
    """记录推给前端的日志（``_emit`` 的唯一出口）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(
        self,
        *,
        level: Severity,
        source: str,
        message: str,
        task_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.records.append((str(level), message))

    def messages(self) -> str:
        return "\n".join(message for _, message in self.records)


def build(
    connection: sqlite3.Connection,
    *,
    reviewer: FakeReviewer | None = None,
    editor: FakeEditor | None = None,
    policy: AutoApprovePolicy = AutoApprovePolicy.GRADE_A,
    log: FakeLog | None = None,
) -> ReviewService:
    return ReviewService(
        connection,
        reviewer=reviewer or FakeReviewer(reviewer_output(9.0)),
        editor=editor or FakeEditor(editor_output()),
        policy=policy,
        log=log,
    )


# ══════════════════════════════════════════════════════════════════════
# A 级：自动放行
# ══════════════════════════════════════════════════════════════════════


class TestAutoPass:
    async def test_an_a_grade_goes_straight_to_the_voice_pool(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        script_id = seed_script(connection, task_id)
        report = await build(connection).review(task_id=task_id, persona=persona())

        assert report.ok
        assert (report.action, report.grade, report.decision) == ("auto_pass", "A", "pass_auto")
        assert (report.total, report.rule_total, report.llm_total) == (9.3, 10.0, 9.0)
        assert report.script_id == script_id and report.version == 1

        task = TaskService(connection).get(task_id)
        assert task.status is TaskStatus.QUEUED_VOICE
        assert task.approved_by == "auto_approve_A"
        assert task.approved_at is not None

    async def test_the_approval_row_is_written_in_one_shot(self, connection: sqlite3.Connection) -> None:
        """裁定 94：自动放行也留一条 ``approved``，且不留永远 pending 的幽灵。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(connection).review(task_id=task_id, persona=persona())

        approvals = ApprovalRepo(connection)
        assert approvals.pending_for_task(task_id) is None
        assert approvals.count_by_status() == {"approved": 1}
        row = approvals.get(report.approval_id or "")
        assert row is not None
        assert (row.status, row.decided_by, row.grade) == ("approved", "auto_approve_A", "A")
        assert (row.score_total, row.revision_round) == (9.3, 0)

    async def test_an_audit_row_is_written(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        await build(connection).review(task_id=task_id, persona=persona())

        rows = AuditRepo(connection).list_for_task(task_id)
        assert [(row.actor, row.actor_ref, row.action, row.source) for row in rows] == [
            ("auto", "auto_approve_A", "task.approve", "auto")
        ]

    async def test_the_review_row_records_both_channels(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(connection).review(task_id=task_id, persona=persona())

        row = ReviewRepo(connection).get(report.review_id or "")
        assert row is not None
        assert (row.round_no, row.grade, row.decision) == (1, "A", "pass_auto")
        assert (row.rule_total, row.llm_total, row.total) == (10.0, 9.0, 9.3)
        assert row.rule_detail["chars"] == 700
        assert row.llm_detail["hook_opening"]["score"] == 9.0
        assert [item["target"] for item in row.issues] == ["hook"]

    async def test_the_grade_ab_policy_signs_b_differently(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0)),
            policy=AutoApprovePolicy.GRADE_AB,
        ).review(task_id=task_id, persona=persona())

        assert (report.action, report.grade) == ("auto_pass", "B")
        task = TaskService(connection).get(task_id)
        assert task.approved_by == "auto_approve_AB"

    async def test_the_off_policy_never_auto_passes(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(connection, policy=AutoApprovePolicy.OFF).review(
            task_id=task_id, persona=persona()
        )
        assert report.action == "human_gate"
        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL


# ══════════════════════════════════════════════════════════════════════
# 人工送审标记：只认更严的方向
# ══════════════════════════════════════════════════════════════════════


class TestHumanGateMarker:
    """``context_json.human_gate`` —— 人亲手点过「生成文案并送审」的那条任务。

    全局那把旋钮（默认 ``grade_a``）是给**批量**流水线用的。人亲手点的送审必须
    真的进闸：不然按钮就是假的（点了送审，确认闸里空空如也 —— 线上踩过一次）。
    """

    @pytest.mark.parametrize("policy", [AutoApprovePolicy.GRADE_A, AutoApprovePolicy.GRADE_AB])
    async def test_a_manually_sent_script_always_reaches_the_gate(
        self, connection: sqlite3.Connection, policy: AutoApprovePolicy
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        TaskService(connection).require_human_gate(task_id)

        report = await build(connection, policy=policy).review(task_id=task_id, persona=persona())

        assert (report.action, report.grade) == ("human_gate", "A")
        task = TaskService(connection).get(task_id)
        assert task.status is TaskStatus.AWAITING_APPROVAL
        assert task.approved_by is None

    async def test_the_marker_does_not_loosen_the_off_policy(self, connection: sqlite3.Connection) -> None:
        """标记只往严的方向拉：全局 ``off`` 时它不会把稿子放开。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        TaskService(connection).require_human_gate(task_id)

        report = await build(connection, policy=AutoApprovePolicy.OFF).review(
            task_id=task_id, persona=persona()
        )

        assert report.action == "human_gate"

    def test_the_marker_keeps_the_rest_of_the_context(self, connection: sqlite3.Connection) -> None:
        """标记是**合并**进去的：``context_json`` 里还躺着成片路径、封面这些。"""
        task_id = seed_task(connection)
        tasks = TaskService(connection)
        tasks.set_cover_path(task_id, cover_path=Path("/tmp/cover.png"))
        tasks.require_human_gate(task_id)

        context = tasks.get(task_id).context
        assert context[HUMAN_GATE_KEY] is True
        assert context["cover_path"] == "/tmp/cover.png"


# ══════════════════════════════════════════════════════════════════════
# B 级：改稿 ⇒ 进闸
# ══════════════════════════════════════════════════════════════════════


class TestEditThenGate:
    async def test_a_b_grade_is_edited_then_reviewed_again(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        reviewer = FakeReviewer(reviewer_output(4.0), reviewer_output(9.0))
        editor = FakeEditor(editor_output())
        report = await build(connection, reviewer=reviewer, editor=editor).review(
            task_id=task_id, persona=persona()
        )

        assert report.ok
        assert (report.action, report.grade) == ("auto_pass", "A")
        assert (report.round_no, report.revision_round) == (2, 1)
        assert len(reviewer.inputs) == 2 and len(editor.inputs) == 1
        assert [row.round_no for row in ReviewRepo(connection).list_for_task(task_id)] == [1, 2]

    async def test_the_editor_receives_the_review_issues(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        editor = FakeEditor(editor_output())
        await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0), reviewer_output(9.0)),
            editor=editor,
        ).review(task_id=task_id, persona=persona())
        assert [item.target for item in editor.inputs[0].issues] == ["hook"]
        assert editor.inputs[0].round_no == 1

    async def test_the_editor_receives_the_whole_script(self, connection: sqlite3.Connection) -> None:
        """裁定 98 的另一面：改稿的输入必须是**完整稿件**，不是一句占位。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        editor = FakeEditor(editor_output())
        await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0), reviewer_output(9.0)),
            editor=editor,
        ).review(task_id=task_id, persona=persona())
        assert len(editor.inputs[0].script.sentences) == 20
        assert editor.inputs[0].script.sentences[0].text == LINE

    async def test_a_new_script_version_lands_with_its_round_and_notes(
        self, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0), reviewer_output(9.0)),
            editor=FakeEditor(editor_output(hook="新的开场")),
        ).review(task_id=task_id, persona=persona())

        repo = ScriptRepo(connection)
        active = repo.get_active(task_id)
        assert active is not None
        assert active.version == 2 and active.id == report.script_id
        assert active.revision_round == 1
        assert active.editor_notes == ["把开场换成结果前置"]
        assert active.hook == "新的开场"
        assert repo.count_sentences(active.id) == 20
        assert connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 2

    async def test_two_rounds_of_editing_then_the_gate(self, connection: sqlite3.Connection) -> None:
        """★ 验收④：改稿 2 轮后仍 B ⇒ 进闸，**不无限循环**。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        reviewer = FakeReviewer(reviewer_output(4.0))
        editor = FakeEditor(editor_output())
        report = await build(connection, reviewer=reviewer, editor=editor).review(
            task_id=task_id, persona=persona()
        )

        assert report.ok
        assert (report.action, report.grade, report.decision) == ("human_gate", "B", "need_edit")
        assert len(reviewer.inputs) == 3, "1 次初审 + 2 次改后重审"
        assert len(editor.inputs) == 2, "改稿硬上限 2 轮"
        assert report.round_no == 3 and report.revision_round == 2
        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL

    async def test_every_round_is_recorded_so_the_panel_can_compare(
        self, connection: sqlite3.Connection
    ) -> None:
        """裁定 97：面板要能并排显示"第 1 轮 / 第 2 轮 / 第 3 轮"，否则改稿有没有用说不清。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0)),
            editor=FakeEditor(editor_output()),
        ).review(task_id=task_id, persona=persona())

        rows = ReviewRepo(connection).list_for_task(task_id)
        assert [row.round_no for row in rows] == [1, 2, 3]
        assert {row.grade for row in rows} == {"B"}
        assert len({row.script_id for row in rows}) == 3, "每轮审的是不同版本"

    async def test_the_gate_writes_a_pending_approval_with_the_three_facts(
        self, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0)),
            editor=FakeEditor(editor_output()),
        ).review(task_id=task_id, persona=persona())

        approvals = ApprovalRepo(connection)
        pending = approvals.pending_for_task(task_id)
        assert pending is not None and pending.id == report.approval_id
        assert (pending.grade, pending.score_total, pending.revision_round) == ("B", 5.8, 2)
        assert approvals.count_by_status() == {"pending": 1}

    async def test_the_gate_writes_no_audit_row(self, connection: sqlite3.Connection) -> None:
        """留痕留给**人**：进闸本身不是"谁做了什么"，放行/打回才是。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0)),
            editor=FakeEditor(editor_output()),
        ).review(task_id=task_id, persona=persona())
        assert AuditRepo(connection).list_for_task(task_id) == []

    async def test_edit_warnings_are_carried_back(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        editor = FakeEditor(editor_output(), warnings=["edit:edited:out_of_scope:第8句"])
        report = await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0), reviewer_output(9.0)),
            editor=editor,
        ).review(task_id=task_id, persona=persona())
        assert report.warnings == ["edit:edited:out_of_scope:第8句"]

    async def test_reviewer_warnings_accumulate_across_rounds(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        reviewer = FakeReviewer(
            reviewer_output(4.0), reviewer_output(9.0), warnings=["review:score_issue_mismatch:hook_opening"]
        )
        report = await build(connection, reviewer=reviewer, editor=FakeEditor(editor_output())).review(
            task_id=task_id, persona=persona()
        )
        assert report.warnings == [
            "review:score_issue_mismatch:hook_opening",
            "review:score_issue_mismatch:hook_opening",
        ]


# ══════════════════════════════════════════════════════════════════════
# C 级：废弃（可人工捞回）
# ══════════════════════════════════════════════════════════════════════


class TestDiscard:
    async def test_a_c_grade_is_discarded(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(connection, reviewer=FakeReviewer(reviewer_output(1.0))).review(
            task_id=task_id, persona=persona()
        )

        assert report.ok
        assert (report.action, report.grade, report.decision) == ("discard", "C", "discard")
        assert report.approval_id is None
        assert TaskService(connection).get(task_id).status is TaskStatus.DISCARDED

    async def test_the_topic_candidate_is_rejected_not_deleted(self, connection: sqlite3.Connection) -> None:
        """★ 验收：C 级误废弃要能**人工捞回** ⇒ 候选置 ``rejected`` 而不是删掉。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        topic_id = seed_topic(connection, task_id)
        await build(connection, reviewer=FakeReviewer(reviewer_output(1.0))).review(
            task_id=task_id, persona=persona()
        )

        row = TopicRepo(connection).get(topic_id)
        assert row is not None and row.status == "rejected"

    async def test_no_approval_row_is_written(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        await build(connection, reviewer=FakeReviewer(reviewer_output(1.0))).review(
            task_id=task_id, persona=persona()
        )
        assert ApprovalRepo(connection).count_by_status() == {}

    async def test_an_audit_row_records_the_discard(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        await build(connection, reviewer=FakeReviewer(reviewer_output(1.0))).review(
            task_id=task_id, persona=persona()
        )
        rows = AuditRepo(connection).list_for_task(task_id)
        assert [(row.actor, row.actor_ref, row.action) for row in rows] == [
            ("auto", "score_discard", "task.discard")
        ]

    async def test_a_c_grade_is_never_edited(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        editor = FakeEditor(editor_output())
        await build(connection, reviewer=FakeReviewer(reviewer_output(1.0)), editor=editor).review(
            task_id=task_id, persona=persona()
        )
        assert editor.inputs == []


# ══════════════════════════════════════════════════════════════════════
# 失败路径
# ══════════════════════════════════════════════════════════════════════


class TestFailures:
    async def test_a_task_without_a_script_is_refused(self, connection: sqlite3.Connection) -> None:
        """没有稿件就别猜 —— 报 ``REVIEW_SCRIPT_MISSING``，任务状态不动。"""
        task_id = seed_task(connection)
        with pytest.raises(StudioError) as excinfo:
            await build(connection).review(task_id=task_id, persona=persona())
        assert excinfo.value.code is ErrorCode.REVIEW_SCRIPT_MISSING
        assert TaskService(connection).get(task_id).status is TaskStatus.DRAFTING

    async def test_a_reviewer_failure_marks_the_task_failed(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        reviewer = FakeReviewer(ok=False, error_code=str(ErrorCode.LLM_TIMEOUT), error_message="超时")
        report = await build(connection, reviewer=reviewer).review(task_id=task_id, persona=persona())

        assert not report.ok
        assert report.error_code == str(ErrorCode.LLM_TIMEOUT)
        assert report.status == "failed"
        assert TaskService(connection).get(task_id).status is TaskStatus.FAILED

    async def test_a_reviewer_failure_writes_no_review_row(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        await build(connection, reviewer=FakeReviewer(ok=False, error_code="LLM_TIMEOUT")).review(
            task_id=task_id, persona=persona()
        )
        assert ReviewRepo(connection).list_for_task(task_id) == []

    async def test_a_reviewer_failure_falls_back_to_review_failed(
        self, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(connection, reviewer=FakeReviewer(ok=False)).review(
            task_id=task_id, persona=persona()
        )
        assert report.error_code == str(ErrorCode.REVIEW_FAILED)

    async def test_an_editor_failure_marks_the_task_failed(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        editor = FakeEditor(ok=False, error_code=str(ErrorCode.EDIT_FAILED), error_message="改稿失败")
        report = await build(connection, reviewer=FakeReviewer(reviewer_output(4.0)), editor=editor).review(
            task_id=task_id, persona=persona()
        )

        assert not report.ok
        assert report.error_code == str(ErrorCode.EDIT_FAILED)
        assert TaskService(connection).get(task_id).status is TaskStatus.FAILED

    async def test_an_editor_failure_leaves_the_review_row_that_already_landed(
        self, connection: sqlite3.Connection
    ) -> None:
        """第 1 轮的分已经落库了 ⇒ 保留它（"这一稿评了多少分"是事实，不该被回滚）。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0)),
            editor=FakeEditor(ok=False, error_code="EDIT_FAILED"),
        ).review(task_id=task_id, persona=persona())
        assert [row.round_no for row in ReviewRepo(connection).list_for_task(task_id)] == [1]

    async def test_an_editor_failure_falls_back_to_edit_failed(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(
            connection, reviewer=FakeReviewer(reviewer_output(4.0)), editor=FakeEditor(ok=False)
        ).review(task_id=task_id, persona=persona())
        assert report.error_code == str(ErrorCode.EDIT_FAILED)


# ══════════════════════════════════════════════════════════════════════
# 留痕与读路径
# ══════════════════════════════════════════════════════════════════════


class TestEmitAndReads:
    async def test_the_pipeline_narrates_what_it_did(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        log = FakeLog()
        await build(connection, log=log).review(task_id=task_id, persona=persona())
        assert "审稿第 1 轮" in log.messages()
        assert "自动放行" in log.messages()

    async def test_the_edit_round_is_narrated(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        log = FakeLog()
        await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0), reviewer_output(9.0)),
            editor=FakeEditor(editor_output()),
            log=log,
        ).review(task_id=task_id, persona=persona())
        assert "改稿完成：第 1 轮" in log.messages()

    async def test_the_discard_is_narrated_as_a_warning(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        log = FakeLog()
        await build(connection, reviewer=FakeReviewer(reviewer_output(1.0)), log=log).review(
            task_id=task_id, persona=persona()
        )
        assert ("warn", "C 级废弃（3.7 分）⇒ 选题候选置 rejected（可人工捞回）") in log.records

    async def test_a_failure_is_narrated_as_an_error(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        log = FakeLog()
        await build(connection, reviewer=FakeReviewer(ok=False, error_code="LLM_TIMEOUT"), log=log).review(
            task_id=task_id, persona=persona()
        )
        assert any(level == "error" for level, _ in log.records)

    async def test_read_latest_review_returns_the_last_round(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        await build(
            connection,
            reviewer=FakeReviewer(reviewer_output(4.0), reviewer_output(9.0)),
            editor=FakeEditor(editor_output()),
        ).review(task_id=task_id, persona=persona())

        row = read_latest_review(connection, task_id)
        assert row is not None and (row.round_no, row.grade) == (2, "A")

    async def test_read_latest_review_is_none_without_reviews(self, connection: sqlite3.Connection) -> None:
        assert read_latest_review(connection, "NOPE") is None

    async def test_the_report_is_json_ready(self, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id)
        report = await build(connection).review(task_id=task_id, persona=persona())
        payload = report.to_dict()
        assert payload["task_id"] == task_id
        assert payload["ok"] is True
        assert set(payload) >= {"action", "grade", "decision", "review_id", "approval_id"}
