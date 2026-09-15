"""T1.11 验收：审稿 → 双通道评分 → 分级放行（§04.1.6 / §04.4.4）。

三条基线（todolist 验收）
-------------------------
``8.0/9.0 ⇒ 8.7 → A`` / ``7.0/6.0 ⇒ 6.3 → B`` / ``4.0/4.5 ⇒ 4.35 → C``。

基线要**端到端**复现，就得把规则通道的分数真的算出来 —— 而规则通道只有四项
（长度 / 禁区 / 开场 / 结尾，等权平均）加一个重复度扣分，所以：

| 基线 | 正文 | 规则通道怎么来的 |
| --- | --- | --- |
| 8.0 / 9.0 | 700 字，**两段一模一样** | 四项全 10 ⇒ 10.0 − 2.0（重复度 0.5 > 0.3）= 8.0 |
| 7.0 / 6.0 | 840 字 + 禁区词 | 长度 8.0 + 禁区 0 + 开场 10 + 结尾 10 ⇒ 7.0 |
| 4.0 / 4.5 | 480 字 + 禁区词 + 两段重复 | (4.0 + 0 + 10 + 10)/4 − 2.0 = 4.0 |

只有 LLM 是脚本化的（``ScriptedTransport`` 按调用顺序吐 JSON），其余全是真的：
真库 / 真提示词 / 真 schema / 真 Reviewer / 真 Editor / 真服务层。用假 Agent 测不出
契约漂移，用假 schema 测不出"模型真能填出这份 JSON"。

⚠️ 一个被这组用例照出来的事实：``writer_output_from_rows`` 会把空的 ``hook`` / ``cta``
兜成"（无开场）/（无结尾）"，所以 **``opening_ok`` / ``ending_ok`` 对入库稿件恒为真**，
规则通道实际只有"长度"与"禁区"两个自由度（外加重复度扣分）。这是兜底文案的副作用，
不是设计意图 —— 见 ``tests/unit/domain/test_scoring.py`` 对四项本身的覆盖。
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
from studio.db.repositories import ApprovalRepo, AuditRepo, ReviewRepo, ScriptRepo
from studio.domain import TaskService, TaskStatus
from studio.domain.enums import AutoApprovePolicy
from studio.domain.scoring import LLM_DIMENSIONS
from studio.domain.script import DirectorOutput, ScriptSegment, estimate_duration_ms
from studio.domain.text import count_chars
from studio.services import ReviewService

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 正文开头（9 个口播字，**同时命中两个口癖**）
CATCH_LINE = "这不科学，俺寻思也是。"

#: 一句干净的口播（12 字）
SENTENCE_TEXT = "这不科学，熊大又跑起来了。"

#: 一句话同时是"该改的"与"可机检的落点"
ISSUE: dict[str, Any] = {
    "code": "weak_hook",
    "severity": "major",
    "target": "hook",
    "detail": "开场太平",
    "suggestion": "把结果前置",
}


# ══════════════════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════════════════


def body_of(chars: int, *, tail: str = "") -> str:
    """**恰好** ``chars`` 口播字的正文（``tail`` 计入 —— 它也是要念出来的字）。"""
    return CATCH_LINE + "熊" * (chars - count_chars(CATCH_LINE) - count_chars(tail)) + tail


def duplicated(line_chars: int, *, tail: str = "") -> str:
    """两段**一模一样**的正文 ⇒ 段落重复度 0.5（> 0.3 ⇒ 扣 2.0 分）。"""
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
    """建任务并推到 ``drafting``（写稿链路由 T1.10 的集成测试负责）。"""
    tasks = TaskService(connection)
    task_id = tasks.create(title="MC跑酷最难的一跳").id
    tasks.transition(task_id, TaskStatus.DRAFTING, actor="test")
    return task_id


def seed_script(connection: sqlite3.Connection, task_id: str, body_md: str) -> str:
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


def reviewer_json(score: float, *, issues: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(
        {
            "llm_detail": {name: {"score": score, "comment": f"{name} 的评语"} for name in LLM_DIMENSIONS},
            "issues": issues if issues is not None else [ISSUE],
        },
        ensure_ascii=False,
    )


def editor_json(
    *,
    body_md: str,
    hook: str = "熊大又整活了",
    cta: str = "点个关注看下集",
    sentences: list[dict[str, Any]] | None = None,
    changes: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "script": {
                "title": "MC跑酷最难的一跳",
                "hook": hook,
                "body_md": body_md,
                "cta": cta,
                "sentences": sentence_rows() if sentences is None else sentences,
                "est_duration_ms": estimate_duration_ms(count_chars(body_md)),
                "catchphrases_used": ["这不科学", "俺寻思"],
            },
            "changes": changes if changes is not None else ["修掉正文里的禁区词"],
        },
        ensure_ascii=False,
    )


# ══════════════════════════════════════════════════════════════════════
# 装配
# ══════════════════════════════════════════════════════════════════════


@dataclass
class Harness:
    paths: StudioPaths
    connection: sqlite3.Connection
    transport: ScriptedTransport
    service: ReviewService

    @property
    def calls(self) -> int:
        """LLM 被调了几次（审稿 + 改稿都算）。"""
        return len(self.transport.calls)

    def prompt(self, index: int) -> str:
        _, messages = self.transport.calls[index]
        return "\n".join(message.content for message in messages)

    def reviews(self, task_id: str) -> ReviewRepo:
        assert self.connection is not None
        return ReviewRepo(self.connection)


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
    return Harness(paths=paths, connection=connection, transport=transport, service=service)


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """``home`` 指向仓库根 ⇒ 提示词与 schema 用的是**生产那两份**。"""
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


# ══════════════════════════════════════════════════════════════════════
# 三条基线（端到端）
# ══════════════════════════════════════════════════════════════════════


class TestBaselines:
    async def test_800_900_becomes_87_and_an_a(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(350))
        harness = build(paths, connection, reviewer_json(9.0))

        report = await harness.service.review(task_id=task_id, persona=persona())

        assert (report.rule_total, report.llm_total, report.total) == (8.0, 9.0, 8.7)
        assert (report.grade, report.decision, report.action) == ("A", "pass_auto", "auto_pass")

    async def test_700_600_becomes_63_and_a_b(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, body_of(840, tail="脏话"))
        harness = build(
            paths,
            connection,
            reviewer_json(6.0),  # 第 1 轮：B
            editor_json(body_md=body_of(700)),  # 改稿（顺手把禁区词修掉）
            reviewer_json(9.0),  # 第 2 轮：A ⇒ 放行
        )

        report = await harness.service.review(task_id=task_id, persona=persona())

        first = ReviewRepo(connection).list_for_task(task_id)[0]
        assert (first.rule_total, first.llm_total, first.total) == (7.0, 6.0, 6.3)
        assert (first.grade, first.decision) == ("B", "need_edit")
        assert report.grade == "A" and report.revision_round == 1

    async def test_400_450_becomes_435_and_a_c(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(240, tail="脏话"))
        harness = build(paths, connection, reviewer_json(4.5))

        report = await harness.service.review(task_id=task_id, persona=persona())

        assert (report.rule_total, report.llm_total, report.total) == (4.0, 4.5, 4.35)
        assert (report.grade, report.decision, report.action) == ("C", "discard", "discard")

    async def test_the_service_uses_the_documented_formula(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """★ 硬契约：``total = 0.3×rule + 0.7×llm``（落库的那一行也必须是它）。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(350))
        await build(paths, connection, reviewer_json(9.0)).service.review(task_id=task_id, persona=persona())
        row = ReviewRepo(connection).list_for_task(task_id)[0]
        assert row.total == round(0.3 * row.rule_total + 0.7 * row.llm_total, 2)

    async def test_the_rule_channel_is_computed_server_side(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """模型只报六维度；规则明细（字数 / 禁区 / 重复度）是服务端算的。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(240, tail="脏话"))
        await build(paths, connection, reviewer_json(4.5)).service.review(task_id=task_id, persona=persona())
        row = ReviewRepo(connection).list_for_task(task_id)[0]
        assert row.rule_detail["chars"] == 480
        assert row.rule_detail["paragraph_dup_ratio"] == 0.5
        assert [(item["term"], item["count"]) for item in row.rule_detail["banned_hits"]] == [("脏话", 2)]
        assert row.llm_detail["hook_opening"]["score"] == 4.5


# ══════════════════════════════════════════════════════════════════════
# 放行 / 进闸 / 废弃（端到端）
# ══════════════════════════════════════════════════════════════════════


class TestOutcomes:
    async def test_an_a_grade_lands_in_the_voice_pool_with_a_signature(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(350))
        report = await build(paths, connection, reviewer_json(9.0)).service.review(
            task_id=task_id, persona=persona()
        )

        task = TaskService(connection).get(task_id)
        assert task.status is TaskStatus.QUEUED_VOICE
        assert task.approved_by == "auto_approve_A"
        assert ApprovalRepo(connection).count_by_status() == {"approved": 1}
        assert [row.action for row in AuditRepo(connection).list_for_task(task_id)] == ["task.approve"]
        assert report.approval_id is not None

    async def test_two_rounds_of_editing_then_the_human_gate(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """★ 验收④：改稿 2 轮后仍 B ⇒ 进闸，**不无限循环**。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id, body_of(840, tail="脏话"))
        harness = build(
            paths,
            connection,
            reviewer_json(6.0),
            editor_json(body_md=body_of(700)),
            reviewer_json(6.0),
            editor_json(body_md=body_of(700)),
            reviewer_json(6.0),
        )

        report = await harness.service.review(task_id=task_id, persona=persona())

        assert (report.action, report.grade, report.decision) == ("human_gate", "B", "need_edit")
        assert (report.round_no, report.revision_round) == (3, 2)
        assert harness.calls == 5, "3 次审稿 + 2 次改稿"
        assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL

        rows = ReviewRepo(connection).list_for_task(task_id)
        assert [row.round_no for row in rows] == [1, 2, 3]
        assert len({row.script_id for row in rows}) == 3, "每轮审的是不同版本"

        pending = ApprovalRepo(connection).pending_for_task(task_id)
        assert pending is not None and pending.id == report.approval_id
        # 第 1 轮审的是 840 字的旧稿（规则 7.0）；改稿把正文换成干净的 700 字 ⇒
        # 第 2、3 轮的规则通道回到 10.0 ⇒ 进闸时看到的是 3.0 + 0.7×6.0 = 7.2
        assert (pending.grade, pending.score_total, pending.revision_round) == ("B", 7.2, 2)

    async def test_a_c_grade_is_discarded(self, paths: StudioPaths, connection: sqlite3.Connection) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, body_of(480, tail="脏话"))
        report = await build(paths, connection, reviewer_json(1.0)).service.review(
            task_id=task_id, persona=persona()
        )
        assert TaskService(connection).get(task_id).status is TaskStatus.DISCARDED
        assert report.approval_id is None
        assert ApprovalRepo(connection).count_by_status() == {}
        assert [row.action for row in AuditRepo(connection).list_for_task(task_id)] == ["task.discard"]

    async def test_the_grade_ab_policy_sweeps_b_through(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, body_of(840, tail="脏话"))
        report = await build(
            paths, connection, reviewer_json(6.0), policy=AutoApprovePolicy.GRADE_AB
        ).service.review(task_id=task_id, persona=persona())
        assert (report.action, report.grade) == ("auto_pass", "B")
        assert TaskService(connection).get(task_id).approved_by == "auto_approve_AB"


# ══════════════════════════════════════════════════════════════════════
# 提示词与改稿越界（端到端）
# ══════════════════════════════════════════════════════════════════════


class TestPromptsAndEditing:
    async def test_the_reviewer_sees_the_facts_the_server_computed(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(240, tail="脏话"))
        harness = build(paths, connection, reviewer_json(4.5))
        await harness.service.review(task_id=task_id, persona=persona())

        prompt = harness.prompt(0)
        assert "480" in prompt
        assert "脏话×2" in prompt
        assert "0.50" in prompt
        assert "第 1 轮" in prompt

    async def test_the_editor_sees_the_issues(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, body_of(840, tail="脏话"))
        harness = build(
            paths,
            connection,
            reviewer_json(6.0),
            editor_json(body_md=body_of(700)),
            reviewer_json(9.0),
        )
        await harness.service.review(task_id=task_id, persona=persona())

        assert "`hook`" in harness.prompt(1)
        assert "开场太平" in harness.prompt(1)
        assert f"第 1 句（bigbear）：{SENTENCE_TEXT}" in harness.prompt(1)

    async def test_an_out_of_scope_edit_is_retried_then_accepted(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """★ 真实 EditorAgent 的越界闸：重试 1 次；改对了就干净放行。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id, body_of(840, tail="脏话"))
        stray = sentence_rows()
        stray[7] = {**stray[7], "text": "顺手改了第8句。"}
        harness = build(
            paths,
            connection,
            reviewer_json(6.0),
            editor_json(body_md=body_of(700), sentences=stray),  # 越界
            editor_json(body_md=body_of(700)),  # 改对了
            reviewer_json(9.0),
        )

        report = await harness.service.review(task_id=task_id, persona=persona())

        assert report.ok and report.grade == "A"
        assert harness.calls == 4
        assert "edit:retry:edited:out_of_scope:第8句" in report.warnings
        assert "第8句" in harness.prompt(2), "纠正提示必须点名第几句"

    async def test_a_persistently_out_of_scope_edit_is_accepted_with_warnings(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """裁定 96：照收 + 留痕，不判失败（判失败 = 白烧一轮且任务掉 failed）。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id, body_of(840, tail="脏话"))
        stray = sentence_rows()
        stray[7] = {**stray[7], "text": "顺手改了第8句。"}
        harness = build(
            paths,
            connection,
            reviewer_json(6.0),
            editor_json(body_md=body_of(700), sentences=stray),
            editor_json(body_md=body_of(700), sentences=stray),  # 重试一次，照样越界
            reviewer_json(9.0),
        )

        report = await harness.service.review(task_id=task_id, persona=persona())

        assert report.ok, "越界不是失败"
        assert harness.calls == 4, "1 次审稿 + 2 次改稿尝试 + 1 次重审"
        assert "edit:edited:out_of_scope:第8句" in report.warnings
        assert TaskService(connection).get(task_id).status is TaskStatus.QUEUED_VOICE

    async def test_a_global_issue_lets_the_editor_rewrite_freely(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        seed_script(connection, task_id, body_of(840, tail="脏话"))
        rewritten = sentence_rows()
        rewritten[0] = {**rewritten[0], "text": "熊大这一跳直接封神。"}
        harness = build(
            paths,
            connection,
            reviewer_json(6.0, issues=[{**ISSUE, "target": "global"}]),
            editor_json(body_md=body_of(700), sentences=rewritten),
            reviewer_json(9.0),
        )

        report = await harness.service.review(task_id=task_id, persona=persona())

        assert report.ok
        assert not [item for item in report.warnings if "out_of_scope" in item]
        assert harness.calls == 3


# ══════════════════════════════════════════════════════════════════════
# 失败与断点
# ══════════════════════════════════════════════════════════════════════


class TestFailures:
    async def test_a_dead_llm_marks_the_task_failed_without_a_script_row(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """E6（无密钥）时的干净降级：``ok=false`` + ``failed``，库里不留半成品。"""
        task_id = seed_task(connection)
        seed_script(connection, task_id, duplicated(350))
        harness = build(paths, connection, Reply(error=None, text="not json at all"))

        report = await harness.service.review(task_id=task_id, persona=persona())

        assert not report.ok
        assert report.error_code is not None
        assert TaskService(connection).get(task_id).status is TaskStatus.FAILED
        assert ReviewRepo(connection).list_for_task(task_id) == []

    async def test_a_task_without_a_script_is_refused(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        task_id = seed_task(connection)
        with pytest.raises(StudioError) as excinfo:
            await build(paths, connection, reviewer_json(9.0)).service.review(
                task_id=task_id, persona=persona()
            )
        assert excinfo.value.code is ErrorCode.REVIEW_SCRIPT_MISSING
        assert TaskService(connection).get(task_id).status is TaskStatus.DRAFTING
