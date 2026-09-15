"""T1.10 验收：选题池 → Director → Writer → 逐句落库（§04.1.4 / §04.1.5）。

六条验收（todolist T1.10）
--------------------------
① 大纲：3–5 段 / 每段 80–350 字 / Σ 600–800 / 时长 60–180s
② 成稿：600–800 字 / 口癖命中 ≥2 / 句长 ≤28 字 / 单人占比 ≤70%
③ 逐句落库：``scripts`` + ``script_sentences`` **同一事务**（不留"有稿无句"）
④ 句长 >28 字 ⇒ **强制切分**（不重写：切分比重写省 token）
⑤ 禁区命中 ⇒ **直接 block**（不重写、不进评分），库里不留半成品
⑥ 字数越界 ⇒ 重写 ≤2 次；仍越界 ⇒ 取**最接近**版本 + ``warn``

只有 LLM 是脚本化的（``ScriptedTransport`` 按调用顺序吐 JSON），其余全是真的：
真库 / 真提示词 / 真 schema / 真 Agent / 真服务层。用假 Agent 测不出契约漂移，
用假 schema 测不出"模型真能填出这份 JSON"。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.unit.agents.fakes import (
    Reply,
    ScriptedTransport,
    agent_gateway,
    persona,
    timeout_error,
)

from studio.agents.director import DirectorAgent
from studio.agents.prompts import PromptLibrary
from studio.agents.writer import WriterAgent
from studio.core.errors import ErrorCode
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import DirectionRepo, ScriptRepo, TopicRepo
from studio.domain.script import REWRITE_LIMIT, estimate_duration_ms
from studio.domain.task_service import TaskService
from studio.domain.text import count_chars
from studio.services import ScriptService

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 正文开头（9 字，**同时命中两个口癖**）
CATCH_LINE = "这不科学，俺寻思也是。"

#: 一句干净的口播（11 字，含一个口癖）
LINE = "这不科学，熊大又跑起来了。"

#: 契约上限（§04.1.5：单句 >28 字由服务端切分）
SENTENCE_MAX_CHARS = 28


# ══════════════════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════════════════


def body(chars: int, *, tail: str = "") -> str:
    """**恰好** ``chars`` 字的正文（含两个口癖）；``tail`` 追加在末尾且不计入。

    末尾用"熊"补齐：字数区间是硬闸门（599 与 600 的差别就是"多烧一轮 token"），
    填充必须可数 —— 靠"多写一句话"碰运气是测不准边界的。
    """
    return CATCH_LINE + "熊" * (chars - count_chars(CATCH_LINE)) + tail


def director_json(*, segments: int = 3, est_chars: int = 200, duration_ms: int = 140_000) -> str:
    """一份合格大纲（默认 3 段 × 200 字 = 600，正好卡在区间下沿）。"""
    return json.dumps(
        {
            "hook_3s": "熊大这一跳把我看傻了",
            "segments": [
                {
                    "seq": index,
                    "point": f"第{index}个要点",
                    "visual": f"第{index}段画面建议",
                    "mood": "兴奋",
                    "est_chars": est_chars,
                }
                for index in range(1, segments + 1)
            ],
            "cta": "点个关注，下期更狠",
            "est_duration_ms": duration_ms,
        },
        ensure_ascii=False,
    )


def sentences(count: int = 20) -> list[dict[str, Any]]:
    """20 句交替口播（单人占比 50%，不触发占比告警）。"""
    return [
        {"seq": index, "text": LINE, "speaker": "bigbear" if index % 2 else "littlebear"}
        for index in range(1, count + 1)
    ]


def writer_json(
    *,
    chars: int = 700,
    duration_ms: int = 170_000,
    lines: list[dict[str, Any]] | None = None,
    tail: str = "",
) -> str:
    """一份成稿。

    ``duration_ms`` 默认 **170s ≠ 700 字的真实估值 140s**：这是故意的 ——
    落库必须用服务端算出来的值（T1.10 裁定 84），拿模型自报值就测不出来了。
    """
    return json.dumps(
        {
            "title": "MC跑酷最难的一跳",
            "hook": "熊大又整活了",
            "body_md": body(chars, tail=tail),
            "cta": "点个关注看下集",
            "sentences": lines if lines is not None else sentences(),
            "est_duration_ms": duration_ms,
            "catchphrases_used": ["这不科学", "俺寻思"],
        },
        ensure_ascii=False,
    )


def seed_topic(connection: sqlite3.Connection, title: str = "MC跑酷最难的一跳") -> str:
    """往选题池里放一条选题（选题池本身由 T1.9 的集成测试负责）。"""
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
    (topic_id,) = TopicRepo(connection).insert_many(
        [
            {
                "direction_id": direction_id,
                "seq": 1,
                "title": title,
                "hook_type": "suspense",
                "angle": "只讲那一跳",
                "exec_feasible": True,
                "score": 8.5,
                "reason": "钩子够硬",
            }
        ]
    )
    return topic_id


# ══════════════════════════════════════════════════════════════════════
# 装配
# ══════════════════════════════════════════════════════════════════════


@dataclass
class Harness:
    paths: StudioPaths
    connection: sqlite3.Connection
    transport: ScriptedTransport
    service: ScriptService

    @property
    def calls(self) -> int:
        """LLM 被调了几次（1 次 Director + N 次 Writer 尝试）。"""
        return len(self.transport.calls)

    def scripts(self) -> ScriptRepo:
        return ScriptRepo(self.connection)


def build(paths: StudioPaths, connection: sqlite3.Connection, *replies: str | Reply) -> Harness:
    """按调用顺序脚本化 LLM 回应，装配**真** Director / Writer / 服务层。"""
    transport = ScriptedTransport(
        replies=[item if isinstance(item, Reply) else Reply(text=item) for item in replies]
    )
    gateway = agent_gateway(paths, connection, transport, agents=["director", "writer"])
    prompts = PromptLibrary.load(paths.prompts_dir)
    service = ScriptService(
        connection,
        director=DirectorAgent(gateway, prompts),
        writer=WriterAgent(gateway, prompts),
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
# ① ② ③ 正常路径
# ══════════════════════════════════════════════════════════════════════


class TestHappyPath:
    async def test_script_and_sentences_land_in_one_piece(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        topic_id = seed_topic(connection)
        harness = build(paths, connection, director_json(), writer_json())
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        assert report.ok and report.created_task
        assert (report.word_count, report.sentence_count) == (700, 20)
        assert report.catchphrases_used == ["这不科学", "俺寻思"]

        script = harness.scripts().get_active(report.task_id)
        assert script is not None and script.id == report.script_id
        assert harness.scripts().count_sentences(script.id) == 20

        # ③ "同一事务"的可观测不变式：不存在只有稿、没有句子的版本
        orphans = connection.execute(
            "SELECT COUNT(*) FROM scripts s "
            "WHERE NOT EXISTS (SELECT 1 FROM script_sentences t WHERE t.script_id = s.id)"
        ).fetchone()[0]
        assert orphans == 0

    async def test_duration_is_computed_not_taken_from_the_model(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """裁定 84：模型自报 170s，落库必须是算出来的 140s（≈5 字/秒）。"""
        topic_id = seed_topic(connection)
        harness = build(paths, connection, director_json(), writer_json(duration_ms=170_000))
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        assert report.est_duration_ms == estimate_duration_ms(700) == 140_000
        row = connection.execute(
            "SELECT est_duration_ms FROM scripts WHERE id = ?", (report.script_id,)
        ).fetchone()
        assert row[0] == 140_000

    async def test_sentences_are_ordered_and_within_the_tts_limit(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        topic_id = seed_topic(connection)
        harness = build(paths, connection, director_json(), writer_json())
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        script_id = report.script_id
        assert script_id is not None
        rows = harness.scripts().list_sentences(script_id)
        assert [row.seq for row in rows] == list(range(1, len(rows) + 1))
        assert all(count_chars(row.text) <= SENTENCE_MAX_CHARS for row in rows)
        assert max(report.speaker_ratio.values()) <= 0.7

    async def test_task_carries_the_persona_and_the_hook_type(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """人物是**随时可改**的 ⇒ 任务必须记住"这一稿是哪个 persona 写的"。"""
        topic_id = seed_topic(connection)
        harness = build(paths, connection, director_json(), writer_json())
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        task = TaskService(connection).get(report.task_id)
        assert task.status.value == "drafting" and task.pool.value == "draft"
        assert task.payload.persona_id == persona().id
        assert task.payload.hook_type == "suspense"

        topic = TopicRepo(connection).get(topic_id)
        assert topic is not None and topic.status == "queued" and topic.task_id == report.task_id

    async def test_outline_is_persisted_for_the_template_layer(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        """大纲与稿件同事务落库：T2 的三层模板要靠它还原 Scene 结构。"""
        topic_id = seed_topic(connection)
        harness = build(paths, connection, director_json(segments=4), writer_json())
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        raw = connection.execute(
            "SELECT outline_json FROM scripts WHERE id = ?", (report.script_id,)
        ).fetchone()[0]
        outline = json.loads(raw)
        assert [segment["seq"] for segment in outline["segments"]] == [1, 2, 3, 4]
        assert outline["hook_3s"] == "熊大这一跳把我看傻了"


# ══════════════════════════════════════════════════════════════════════
# ④ 超长句：切分，不重写
# ══════════════════════════════════════════════════════════════════════


class TestSentenceSplitting:
    async def test_long_sentence_is_split_without_a_rewrite(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        topic_id = seed_topic(connection)
        long_line = {"seq": 1, "text": "熊大说这不科学，" * 5, "speaker": "bigbear"}
        harness = build(
            paths,
            connection,
            director_json(),
            writer_json(lines=[long_line, *sentences()[1:]]),
        )
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        assert report.ok
        assert harness.calls == 2, "切分不该触发第二次 Writer 调用"
        script_id = report.script_id
        assert script_id is not None
        rows = harness.scripts().list_sentences(script_id)
        assert [row.seq for row in rows] == list(range(1, len(rows) + 1))
        assert all(count_chars(row.text) <= SENTENCE_MAX_CHARS for row in rows)
        assert len(rows) > 20, "40 字的长句必须被切成多句"
        assert any(item.startswith("sentence_split:") for item in report.warnings)


# ══════════════════════════════════════════════════════════════════════
# ⑤ 禁区：直接 block
# ══════════════════════════════════════════════════════════════════════


class TestForbidden:
    async def test_forbidden_body_blocks_and_persists_nothing(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        topic_id = seed_topic(connection)
        harness = build(paths, connection, director_json(), writer_json(tail="脏话"))
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        assert not report.ok
        assert report.error_code == str(ErrorCode.SCRIPT_FORBIDDEN)
        assert "forbidden:脏话" in report.warnings
        assert harness.calls == 2, "合规问题必须让人看见，不能悄悄重写掉"

        assert connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM script_sentences").fetchone()[0] == 0
        assert TaskService(connection).get(report.task_id).status.value == "failed"


# ══════════════════════════════════════════════════════════════════════
# ⑥ 字数越界：重写 ≤2 次，取最接近的一版
# ══════════════════════════════════════════════════════════════════════


class TestRewriteBudget:
    async def test_out_of_range_body_is_rewritten_twice_then_the_closest_wins(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        topic_id = seed_topic(connection)
        harness = build(
            paths,
            connection,
            director_json(),
            writer_json(chars=500),  # 差 100
            writer_json(chars=900),  # 差 100（并列 ⇒ 保留先出现的）
            writer_json(chars=550),  # 差 50 ⇒ 胜出
        )
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        assert harness.calls == 1 + 1 + REWRITE_LIMIT, "1 次 Director + 1 次首写 + 2 次重写"
        assert report.ok, "越界不是失败：取最接近的一版照样落库（DoD 6）"
        assert report.word_count == 550
        assert any(item.startswith("script:字数") for item in report.warnings)
        assert any(item.startswith("rewrite:1:") for item in report.warnings)
        assert any(item.startswith("rewrite:2:") for item in report.warnings)
        assert harness.scripts().get_active(report.task_id) is not None


# ══════════════════════════════════════════════════════════════════════
# 幂等 + 失败路径
# ══════════════════════════════════════════════════════════════════════


class TestReruns:
    async def test_second_run_reuses_the_task_and_activates_a_new_version(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        topic_id = seed_topic(connection)
        harness = build(
            paths,
            connection,
            director_json(),
            writer_json(),
            director_json(),
            writer_json(),
        )
        first = await harness.service.draft(topic_id=topic_id, persona=persona())
        second = await harness.service.draft(topic_id=topic_id, persona=persona())

        assert first.task_id == second.task_id
        assert (first.created_task, second.created_task) == (True, False)
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert (first.version, second.version) == (1, 2)

        # 部分唯一索引 ux_scripts_active：生效的只能有一版，且是最新的那版
        active = connection.execute("SELECT COUNT(*) FROM scripts WHERE is_active = 1").fetchone()[0]
        assert active == 1
        latest = harness.scripts().get_active(second.task_id)
        assert latest is not None and latest.version == 2


class TestLlmFailure:
    async def test_timeout_fails_the_task_without_a_half_baked_script(
        self, paths: StudioPaths, connection: sqlite3.Connection
    ) -> None:
        topic_id = seed_topic(connection)
        harness = build(paths, connection, Reply(error=timeout_error()))
        report = await harness.service.draft(topic_id=topic_id, persona=persona())

        assert not report.ok
        assert report.error_code == str(ErrorCode.LLM_TIMEOUT)
        assert connection.execute("SELECT COUNT(*) FROM scripts").fetchone()[0] == 0

        task = TaskService(connection).get(report.task_id)
        assert task.status.value == "failed"
        assert task.error_code == str(ErrorCode.LLM_TIMEOUT)
        assert task.last_healthy_status is not None
        assert task.last_healthy_status.value == "drafting", "断点续传要知道从哪儿接着跑"

        # 失败不静默：system_logs 里有可展示的 error 行
        errors = connection.execute(
            "SELECT COUNT(*) FROM system_logs WHERE task_id = ? AND level = 'error'",
            (report.task_id,),
        ).fetchone()[0]
        assert errors >= 1
