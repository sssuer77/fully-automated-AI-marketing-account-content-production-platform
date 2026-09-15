"""T1.9 验收：输入源 → Planner 方向 → Ideator 选题池（§04.1.2 / §04.1.3 / §04.1.7）。

五条验收（todolist T1.9）
------------------------
① 热点行解析（坏行**照入库留痕**，但不参与选题、也不阻塞归档 —— 裁定 67）
② 反馈容错（自由文本 / 结构化头混排；看着像结构化头但情感词不认 ⇒ 记 issue 不中断）
③ 归一化哈希去重（同一模板换数字 ⇒ **丢弃不入库**）
④ 相似度 ≥ 0.85 ⇒ 降分并写 ``similar_to``
⑤ 四条规则校验（含"被吐槽" ⇒ ``priority += 500``）

这里**不 mock 数据库、不 mock 去重算法、不 mock 提示词**：只有 LLM 是脚本化的
（``ScriptedTransport`` 按顺序吐 JSON）。链路上真实的部分越多，"接得上"才越可信。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from tests.unit.agents.fakes import FakeClock, Reply, ScriptedTransport, llm_config, persona

from studio.agents.cost import LlmCallStore
from studio.agents.feedback_classifier import FeedbackClassifierAgent
from studio.agents.gateway import GatewaySettings, LlmGateway
from studio.agents.ideator import IdeatorAgent
from studio.agents.planner import PlannerAgent
from studio.agents.prompts import PromptLibrary
from studio.core.config import PersonaConfig
from studio.core.paths import StudioPaths
from studio.core.proto import Severity
from studio.db import connect, migrate
from studio.db.repositories import FeedbackItemRepo, HotItemRepo, TopicRepo
from studio.domain.topics import (
    COMPLAINT_DEMOTION,
    DEDUP_SIMILARITY_PENALTY,
    RISK_COMPLAINED_TOPIC,
    hash_title,
)
from studio.services import ImportReport, InputService, TopicService

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 热点文件（3 条合法 + 1 条坏行）
HOT_TEXT = """# 2026-09-13 热点
MC 跑酷新版本|爆|douyin
熊出没大电影定档|1.2万|bilibili
MC 跑酷速通纪录|高|douyin
这一行只有两列|爆
"""

#: 反馈文件（自由文本 + 合法结构化头 + **看着像结构化头但情感词不认**）
FEEDBACK_TEXT = """想看MC跑酷跑酷合集，能不能出个新手向的
douyin|2026-09-01|neg|这个标题太吵了
bilibili|2026-09-02|不认识的词|这行是坏的结构化头
"""

#: 5 个方向的选题（第 2 个方向刻意撞库：1 条哈希命中、1 条高相似）
IDEATOR_TITLES: tuple[tuple[str, ...], ...] = (
    ("熊大跑酷翻车现场", "跑酷地图推荐", "MC冷知识盘点", "熊二名场面回顾"),
    ("我的世界跑酷7个技巧", "MC跑酷必看技巧大全", "生存模式第一天", "红石机关教程"),
    ("联机对战实录", "极限跳跃挑战", "隐藏关卡探秘", "速通记录刷新"),
    ("萌新入坑指南", "老玩家回归感想", "地图作者访谈", "模组推荐清单"),
    ("手机版操作技巧", "键鼠手感对比", "帧数优化设置", "录屏剪辑心得"),
)

#: 预置在池子里的两条历史选题（第 2 个方向要撞的就是它们）
SEEDED = ("MC跑酷5个技巧", "MC跑酷必看技巧合集")


class RecordingLog:
    """把服务层发出的日志收进列表（断言"该报警的地方报警了"）。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        level: Severity,
        source: str,
        message: str,
        task_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.entries.append(
            {"level": level, "source": source, "message": message, "task_id": task_id, "payload": payload}
        )

    @property
    def levels(self) -> list[str]:
        return [str(entry["level"]) for entry in self.entries]


@dataclass
class Harness:
    paths: StudioPaths
    connection: sqlite3.Connection
    transport: ScriptedTransport
    service: TopicService
    #: 夹具阶段那一次导入的结果（``run_planner(import_sources=False)`` 不会重导）
    hot_import: ImportReport
    feedback_import: ImportReport
    log: RecordingLog = field(default_factory=RecordingLog)

    @property
    def hot(self) -> HotItemRepo:
        return HotItemRepo(self.connection)

    @property
    def topics(self) -> TopicRepo:
        return TopicRepo(self.connection)


def _planner_reply(hot_id: str, feedback_id: str) -> str:
    """5 个方向：前 4 个干净，第 5 个"被吐槽过"（priority=10 ⇒ 期望 +500）。"""
    grounded = [
        {"type": "persona", "quote": "账号定位"},
        {"type": "hot", "ref_id": hot_id, "quote": "MC 跑酷新版本"},
        {"type": "feedback", "ref_id": feedback_id, "kind": "want", "quote": "新手向合集"},
    ]
    directions = [
        {
            "title": f"方向{i}",
            "rationale": f"理由{i}",
            "grounded_on": grounded,
            "priority": 100,
            "risk_flags": [],
            "fit_score": 8,
        }
        for i in range(1, 6)
    ]
    directions[-1] = {
        **directions[-1],
        "title": "被吐槽过的方向",
        "priority": 10,
        "risk_flags": [RISK_COMPLAINED_TOPIC],
    }
    return json.dumps({"directions": directions}, ensure_ascii=False)


def _ideator_reply(titles: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "topics": [
                {
                    "title": title,
                    "hook_type": "suspense",
                    "angle": f"{title} 的差异化角度",
                    "exec_feasible": True,
                    "score": 8.0,
                    "reason": "钩子够硬",
                }
                for title in titles
            ]
        },
        ensure_ascii=False,
    )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    paths = StudioPaths(home=REPO_ROOT, data_dir=tmp_path / "data")
    (paths.hot_dir).mkdir(parents=True, exist_ok=True)
    (paths.feedback_dir).mkdir(parents=True, exist_ok=True)
    (paths.hot_dir / "0913.md").write_text(HOT_TEXT, encoding="utf-8")
    (paths.feedback_dir / "0913.md").write_text(FEEDBACK_TEXT, encoding="utf-8")
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    try:
        yield _build(paths, connection)
    finally:
        connection.close()


def _build(paths: StudioPaths, connection: sqlite3.Connection) -> Harness:
    """先导入输入拿到 id（分类要按 ``ref`` 回抄），再按 id 脚本化 LLM 回应。"""
    log = RecordingLog()
    inputs = InputService(connection, paths=paths, log=log)
    hot_import = inputs.import_hot()
    feedback_import = inputs.import_feedback()

    feedback_rows = FeedbackItemRepo(connection).list_recent()
    hot_rows = HotItemRepo(connection).list_unconsumed()
    refs = [row.id for row in feedback_rows]
    classify = json.dumps(
        {
            "items": [
                {"ref": refs[0], "sentiment": "pos", "wants": ["新手向合集"], "complaints": []},
                {"ref": refs[1], "sentiment": "neg", "wants": [], "complaints": ["标题太吵"]},
                {"ref": refs[2], "sentiment": "neu", "wants": [], "complaints": []},
            ]
        },
        ensure_ascii=False,
    )
    replies = [
        Reply(text=classify),
        Reply(text=_planner_reply(hot_rows[0].id, refs[0])),
        *[Reply(text=_ideator_reply(titles)) for titles in IDEATOR_TITLES],
    ]
    transport = ScriptedTransport(replies=replies)
    gateway = LlmGateway(
        config=llm_config(
            routing={
                "planner": {"profile": "cloud", "fallback": "local"},
                "ideator": {"profile": "cloud", "fallback": "local"},
            }
        ),
        transport=transport,
        calls=LlmCallStore(connection),
        budget=None,
        breaker=None,
        settings=GatewaySettings(backoff_base_sec=0.0),
        schema_root=paths.schemas_dir,
        log=None,
        clock=FakeClock(),
        env={"STUDIO_LLM_API_KEY": "test-key"},
    )
    prompts = PromptLibrary.load(paths.prompts_dir)
    service = TopicService(
        connection,
        planner=PlannerAgent(gateway, prompts),
        ideator=IdeatorAgent(gateway, prompts),
        classifier=FeedbackClassifierAgent(gateway, prompts),
        paths=paths,
        log=log,
        input_service=inputs,
    )
    return Harness(
        paths=paths,
        connection=connection,
        transport=transport,
        service=service,
        hot_import=hot_import,
        feedback_import=feedback_import,
        log=log,
    )


def _persona() -> PersonaConfig:
    return persona()


# ══════════════════════════════════════════════════════════════════════
# ① 热点行解析 + ② 反馈容错 + ⑤ 四条规则
# ══════════════════════════════════════════════════════════════════════


async def test_planner_parses_sources_and_enforces_the_four_rules(harness: Harness) -> None:
    report = await harness.service.run_planner(persona=_persona(), import_sources=False)

    # ① 热点：3 条合法入库可消费，1 条坏行**留痕但不参与**
    hot_rows = harness.connection.execute("SELECT * FROM hot_items ORDER BY line_no").fetchall()
    assert len(hot_rows) == 4
    assert [bool(row["parse_ok"]) for row in hot_rows] == [True, True, True, False]
    assert hot_rows[3]["raw_line"] == "这一行只有两列|爆"
    assert harness.hot_import.bad == 1
    assert harness.hot_import.inserted == 4
    assert report.hot_consumed == 3
    # 坏行不阻塞归档：文件整批消费完就进 archive/
    assert len(report.archived) == 1
    assert not (harness.paths.hot_dir / "0913.md").exists()
    assert len(list(harness.paths.hot_archive_dir.glob("*.md"))) == 1

    # ② 反馈：三行全部入库（坏结构化头也兜成自由文本），并回填 DDL 口径的情感
    feedback_rows = FeedbackItemRepo(harness.connection).list_recent()
    assert len(feedback_rows) == 3
    assert {row.sentiment for row in feedback_rows} == {"positive", "negative", "neutral"}
    assert any(row.wants == ["新手向合集"] for row in feedback_rows)
    assert harness.feedback_import.inserted == 3
    assert len(harness.feedback_import.issues) == 1
    # 坏行不静默：导入阶段就该有一条 warn 落到日志出口（DoD 5）
    assert any(
        entry["source"] == "input.import" and entry["level"] == "warn" for entry in harness.log.entries
    )
    assert "directions_demoted:1" in report.warnings

    # ⑤ 方向：5 个、四条规则全过、被吐槽的那个 +500
    assert report.ok is True
    assert report.direction_count == 5
    assert report.rule_report is not None
    assert report.rule_report.ok is True
    assert report.rule_report.demoted == ["被吐槽过的方向"]
    priorities = {row.title: row.priority for row in report.directions}
    assert priorities["被吐槽过的方向"] == 10 + COMPLAINT_DEMOTION
    assert priorities["方向1"] == 100
    # 依据落库（WebUI 靠它回答"为什么是这个方向"）
    grounded = next(row for row in report.directions if row.title == "方向1").grounded_on
    assert {ref["type"] for ref in grounded} == {"persona", "hot", "feedback"}

    # LLM 记账：分类 1 次 + Planner 1 次（规则一次过 ⇒ 不重试），两次都落 llm_calls
    records = list(reversed(LlmCallStore(harness.connection).recent(limit=20)))
    assert len(records) == 2
    assert {record.status for record in records} == {"ok"}
    # 反馈分类与 Planner 共用 `planner` 通道路由（§01.2.4：都是"读中文想内容"的活）
    assert {record.agent for record in records} == {"planner"}


async def test_planner_reports_empty_when_the_model_returns_nothing(harness: Harness) -> None:
    harness.transport.replies = [Reply(text='{"directions": []}')]
    report = await harness.service.run_planner(persona=_persona(), import_sources=False)
    assert report.ok is False
    assert report.error_code is not None
    assert "error" in harness.log.levels


# ══════════════════════════════════════════════════════════════════════
# ③ 归一化哈希去重 + ④ 相似度降分
# ══════════════════════════════════════════════════════════════════════


async def test_ideator_dedups_and_demotes(harness: Harness) -> None:
    planned = await harness.service.run_planner(persona=_persona(), import_sources=False)
    direction_id = planned.directions[0].id
    harness.topics.insert_many(
        [
            {
                "direction_id": direction_id,
                "seq": 90 + index,
                "title": title,
                "hook_type": "number",
                "angle": "历史选题",
                "exec_feasible": True,
                "score": 7.0,
                "reason": "历史",
                "dedup_hash": hash_title(title),
                "similar_to": [],
                "status": "candidate",
            }
            for index, title in enumerate(SEEDED)
        ]
    )
    calls_before = len(harness.transport.calls)

    report = await harness.service.run_ideator(persona=_persona(), batch_id=planned.batch_id)

    assert report.batch_id == planned.batch_id
    assert len(report.outcomes) == 5
    assert all(outcome.ok for outcome in report.outcomes)
    # 每方向 4 个 × 5 方向 = 20，其中 1 个哈希命中被丢弃 ⇒ 19 条入库
    assert report.inserted == 19
    assert report.dropped == 1
    assert report.demoted == 1
    assert len(harness.transport.calls) - calls_before == 5

    titles = {row.title for row in harness.topics.list_for_dedup()}
    # ③ 同一模板换数字 ⇒ 丢弃，**不入库**
    assert "我的世界跑酷7个技巧" not in titles
    assert SEEDED[0] in titles
    # ④ 相似度 ≥ 0.85 ⇒ 入库但降分，并写清"跟谁像"
    demoted = next(row for row in harness.topics.list_for_dedup() if row.title == "MC跑酷必看技巧大全")
    assert demoted.score == pytest.approx(8.0 - DEDUP_SIMILARITY_PENALTY)
    assert demoted.similar_to[0]["target_title"] == SEEDED[1]
    assert demoted.similar_to[0]["similarity"] >= 0.85
    assert "去重：" in (demoted.reason or "")

    # 方向状态回写：产出过选题的方向置 selected
    assert {row.status for row in harness.service._directions.list_batch(planned.batch_id)} == {"selected"}


async def test_ideator_isolates_a_failing_direction(harness: Harness) -> None:
    """§04.1.3：单个方向失败**不**影响其他方向。"""
    planned = await harness.service.run_planner(persona=_persona(), import_sources=False)
    # 第 1 个方向拿好回复，之后一直是坏 JSON（``ScriptedTransport`` 用尽后复用最后一条）
    harness.transport.replies = [
        harness.transport.replies[0],
        harness.transport.replies[1],
        Reply(text=_ideator_reply(IDEATOR_TITLES[0])),
        Reply(text="这不是 JSON"),
    ]
    report = await harness.service.run_ideator(
        persona=_persona(),
        batch_id=planned.batch_id,
        direction_ids=[planned.directions[0].id, planned.directions[1].id],
    )
    assert [outcome.ok for outcome in report.outcomes] == [True, False]
    assert report.failed[0].direction_id == planned.directions[1].id
    assert report.failed[0].error_code is not None
    assert report.inserted == 4
    assert "warn" in harness.log.levels


async def test_ideator_requires_a_known_batch(harness: Harness) -> None:
    with pytest.raises(Exception, match="选题批次"):
        await harness.service.run_ideator(persona=_persona(), batch_id=None)
