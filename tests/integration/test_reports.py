"""数据报告与决策闭环集成测试（T5.7 验收 · §04.6.5.2 / §06.7）。

九条验收口径，一条一个用例
--------------------------
① 周报含 ``summary_md`` + ``data_json`` + ``insights``
② **同周期重复生成不重复插入**（幂等靠 ``UNIQUE``，不靠调用方记得先查）
③ ``n<10`` ⇒ ``confidence='low'`` 且文案里带「样本不足」
④ ``apply`` ⇒ 写进人物偏好 + ``audit_ops(report.apply_insight)`` 带 before/after
⑤ **闭环**：采纳后下一轮 Planner 读到的 persona 变量里含该结论
⑥ 非法 ``period`` 被 CHECK 拒绝
⑦ **周期编辑** ⇒ ``next_run_at`` 重算 + ``audit_ops``
⑧ **同周期仅 1 个启用**（部分唯一索引的友好版）
⑨ 停用 ⇒ 不再被到期查询取到

四条纪律（与 ``test_scheduler.py`` 同一套）
------------------------------------------
1. **临时家目录**：真配置抄一份进 tmp —— 采纳建议会写 ``config/persona.yaml``，
   拿仓库根当 home 跑就是把真人物改了。
2. **真库真盘**：报告行、留痕、落盘的 md 一律回库/回盘查。「返回 200」只说明没抛。
3. **时刻一律显式传入**：窗口与 ``now`` 都写死，用例不在 00:00 前后随机红。
4. **不调 LLM**：这些用例一个模型都不碰 —— 报告的全部输入都在库里（§6.7.3）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio.agents.base import persona_variables
from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.core.proto import EVENT_PAYLOAD_KEY, AlertCode, EventKind
from studio.db.migrate import migrate
from studio.db.repositories.audit_repo import AuditRepo
from studio.db.repositories.publication_repo import PublicationRepo
from studio.db.repositories.report_repo import ReportRepo, ReportRow, ReportScheduleRepo
from studio.domain.enums import TaskKind, TaskStatus
from studio.domain.report import confidence_for, period_bounds
from studio.domain.task_service import TaskService
from studio.services import report_service
from studio.services.metrics_service import ResourceSnapshot
from studio.services.persona_service import PersonaService
from studio.services.report_service import (
    AUDIT_APPLY,
    AUDIT_GENERATE,
    AUDIT_SCHEDULE_CREATE,
    AUDIT_SCHEDULE_UPDATE,
    FAIL_STREAK_ALERT,
    RESULT_OK,
    RESULT_SKIPPED_NO_DATA,
    ReportSchedulerService,
    apply_insight,
    create_schedule,
    delete_schedule,
    export_report,
    generate_report,
    run_now,
    update_schedule,
)
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

REPORTS_URL = "/api/v1/reports"
SCHEDULES_URL = "/api/v1/report-schedules"

#: 固定的报告窗口（本地日；UTC 的那几条发布都落在里面）
START = "2026-09-07"
END = "2026-09-13"

#: 固定的"生成那一刻"（本地 2026-09-14 09:00 = UTC 01:00）
NOW = "2026-09-14T01:00:00.000Z"

TO_COMPLETED: tuple[TaskStatus, ...] = (
    TaskStatus.DRAFTING,
    TaskStatus.REVIEWING,
    TaskStatus.QUEUED_VOICE,
    TaskStatus.VOICING,
    TaskStatus.QUEUED_RENDER,
    TaskStatus.RENDERING,
    TaskStatus.COMPLETED,
)


class _Probe:
    """假资源探针（不碰真 `nvidia-smi` 与真磁盘）。"""

    def __call__(self, **kwargs: object) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at="2026-09-18T06:00:00.000Z",
            cpu_pct=10.0,
            ram_used_mb=4096,
            ram_total_mb=32768,
            ram_pct=12.5,
            process_rss_mb=256,
            disk_free_c_gb=50.0,
            disk_free_d_gb=100.0,
            disk_free_d_min_gb=15.0,
            disk_drive="D:",
            disk_low=False,
            gpu_name="NVIDIA GeForce RTX 2070",
            gpu_util_pct=3.0,
            gpu_mem_used_mb=1024,
            gpu_mem_total_mb=8192,
        )


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    """临时家目录：真配置抄一份（**一个字都不改** —— 报告不看发布开关）。"""
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    value.config_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((REPO_ROOT / "config").glob("*.yaml")):
        shutil.copyfile(source, value.config_dir / source.name)
    return value


@pytest.fixture
def state(paths: StudioPaths) -> Iterator[AppState]:
    migrate(paths.db_file)
    built = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=_Probe(),
    )
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def connection(state: AppState) -> sqlite3.Connection:
    return state.connections.get()


@pytest.fixture
def client(state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


def make_task(
    connection: sqlite3.Connection,
    *,
    title: str = "报告用",
    grade: str | None = "A",
    duration_ms: int | None = 45_000,
    hook_type: str | None = None,
) -> str:
    """建一条出片完成的任务（可选：评级 / 时长 / 选题钩子类型）。"""
    tasks = TaskService(connection)
    task_id = tasks.create(title=title, kind=TaskKind.VIDEO, actor="test").id
    for status in TO_COMPLETED:
        tasks.transition(task_id, status, actor="test")
    if grade is not None:
        connection.execute("UPDATE tasks SET grade = ? WHERE id = ?", (grade, task_id))
    if duration_ms is not None:
        payload = json.dumps({"target_duration_ms": duration_ms}, ensure_ascii=False)
        connection.execute("UPDATE tasks SET payload_json = ? WHERE id = ?", (payload, task_id))
    if hook_type is not None:
        connection.execute(
            "INSERT INTO content_directions(id, batch_id, seq, title, rationale)"
            " VALUES (?, 'batch-1', 1, '方向', '理由')",
            (f"dir-{task_id}",),
        )
        connection.execute(
            "INSERT INTO topic_candidates(id, direction_id, seq, title, angle, hook_type)"
            " VALUES (?, ?, 1, '选题', '角度', ?)",
            (f"topic-{task_id}", f"dir-{task_id}", hook_type),
        )
        connection.execute("UPDATE tasks SET source_topic_id = ? WHERE id = ?", (f"topic-{task_id}", task_id))
    return task_id


def make_publication(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    published_at: str,
    views: int,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    completion_rate: float | None = None,
    platform: str = "other",
    account_id: str = "_rehearsal",
) -> str:
    """登记一条已发布的记录并写回读数（``published_at`` 由用例钉死）。"""
    repo = PublicationRepo(connection)
    row, _ = repo.create(
        task_id=task_id,
        platform=platform,
        account_id=account_id,
        video_path="/tmp/x.mp4",
        video_sha256="0" * 64,
        title="报告用",
    )
    repo.mark_published(row.id, url="http://127.0.0.1/rehearsal/x")
    metrics: dict[str, object] = {
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "collected_at": published_at,
        "source": "test",
    }
    if completion_rate is not None:
        metrics["completion_rate"] = completion_rate
    repo.record_metrics(row.id, metrics, next_metric_at=None)
    connection.execute("UPDATE publications SET published_at = ? WHERE id = ?", (published_at, row.id))
    return row.id


def seed_window(
    connection: sqlite3.Connection,
    *,
    per_group: int = 6,
    views_a: int = 1_000,
    views_b: int = 300,
    grade: str = "A",
) -> None:
    """造两组钩子类型（数字 vs 悬念）各 ``per_group`` 条，播放中位数相差 3 倍。

    播放刻意做成"每组一个固定值"：中位数与均值都可预测，断言不必跟着随机数走。
    """
    for index in range(per_group):
        task_a = make_task(connection, title=f"数字{index}", hook_type="数字", grade=grade)
        make_publication(
            connection,
            task_id=task_a,
            published_at=f"2026-09-0{8}T02:0{index}:00.000Z",
            views=views_a,
            likes=views_a // 10,
            completion_rate=0.5,
        )
        task_b = make_task(connection, title=f"悬念{index}", hook_type="悬念", grade=grade)
        make_publication(
            connection,
            task_id=task_b,
            published_at=f"2026-09-09T06:0{index}:00.000Z",
            views=views_b,
            likes=views_b // 10,
            completion_rate=0.3,
        )


class _Collector:
    """假的日志出口：把每条日志收下来（验 WS 事件与文案时用）。

    为什么不直接读 ``system_logs``：``LogSink`` 的契约就是"调用方拿到一条日志"，
    而 WS 事件是**搭日志的车**扇出去的（``payload[event_kind]``）。收在这里
    能同时看到"有没有发"和"发的是什么"，比回头去库里捞更直接。
    """

    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        level: str,
        source: str,
        message: str,
        task_id: str | None = None,
        payload: object = None,
    ) -> None:
        self.items.append(
            {
                "level": level,
                "source": source,
                "message": message,
                "task_id": task_id,
                "payload": payload,
            }
        )

    def events(self) -> list[str]:
        out: list[str] = []
        for item in self.items:
            payload = item["payload"]
            if isinstance(payload, dict) and EVENT_PAYLOAD_KEY in payload:
                out.append(str(payload[EVENT_PAYLOAD_KEY]))
        return out


def generate(connection: sqlite3.Connection, paths: StudioPaths, **kwargs: object) -> ReportRow:
    """生成一份报告（默认就是上面那个固定窗口）。"""
    params: dict[str, object] = {
        "period": "weekly",
        "start_date": START,
        "end_date": END,
        "trigger": "manual",
        "now": NOW,
    }
    params.update(kwargs)
    return generate_report(connection=connection, paths=paths, **params)  # type: ignore[arg-type]


def make_due_schedule(connection: sqlite3.Connection, *, schedule_id: str = "rsched_weekly") -> str:
    """把 seed 里那条内置周报**拨到已到点**（``0006_seed.sql`` 的 id 是固定的）。

    为什么不新建一条：出厂就有 ``rsched_weekly`` / ``rsched_monthly`` 两条启用中的
    内置计划（同周期只能有一条启用 ⇒ 再建 weekly 会被挡住）。用 seed 那一份
    反而更贴近真机：用户看到的就是这两条。
    """
    connection.execute(
        "UPDATE report_schedules SET next_run_at = ? WHERE id = ?",
        ("2026-09-14T00:00:00.000Z", schedule_id),
    )
    return schedule_id


# ══════════════════════════════════════════════════════════════════════
# ① 报告内容（summary_md + data_json + insights）
# ══════════════════════════════════════════════════════════════════════


def test_weekly_report_has_summary_data_and_insights(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """① 周报含 ``summary_md`` + ``data_json`` + ``insights``（验收口径逐条对上）。"""
    seed_window(connection, per_group=6, views_a=1_000, views_b=300)
    row = generate(connection, paths)
    assert row.period == "weekly"
    assert row.publish_count == 12
    assert row.median_views == 650.0  # (300 + 1000) / 2
    assert row.total_views == 6 * 1000 + 6 * 300
    # data_json：七维里能算的那几维都在
    assert row.data["by_hook_type"], row.data
    keys = {item["key"] for item in row.data["by_hook_type"]}
    assert keys == {"数字", "悬念"}
    assert row.data["by_platform"][0]["key"] == "other"
    assert row.data["by_hour"], "时段归因要有内容"
    assert row.data["by_grade"][0]["key"] == "A"
    assert row.data["by_duration"][0]["key"] == "30-60s"
    # summary_md：人类可读的那一份
    assert row.summary_md.startswith("# 周报（2026-09-07 ~ 2026-09-13）")
    assert "选题类型归因" in row.summary_md
    assert "播放中位数" in row.summary_md
    # insights：数字 1000 vs 悬念 300 ⇒ 差 233%，必须出一条 topic 建议
    kinds = [item["kind"] for item in row.insights]
    assert "topic" in kinds
    topic = next(item for item in row.insights if item["kind"] == "topic")
    assert topic["evidence"]["a"] == "数字"
    assert topic["evidence"]["delta_pct"] > 200
    assert topic["confidence"] == "low"  # n = min(6, 6) = 6 < 10 ⇒ low
    assert row.artifacts and row.artifacts[0].endswith("weekly_2026-09-07_2026-09-13.md")
    assert Path(row.artifacts[0]).read_text(encoding="utf-8") == row.summary_md


def test_low_confidence_marks_insufficient_samples(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """③ ``n<10`` ⇒ ``confidence='low'`` 且**文案里带「样本不足」**（§6.7.3 安全阀二）。"""
    seed_window(connection, per_group=3, views_a=1_000, views_b=300)
    row = generate(connection, paths)
    topic = next(item for item in row.insights if item["kind"] == "topic")
    assert topic["evidence"]["n"] == 3
    assert topic["confidence"] == "low"
    assert "样本不足" in topic["statement"]
    # 面板渲染的就是这份 md ⇒ 警示必须在正文里，不能只活在 confidence 字段里
    assert "样本不足" in row.summary_md


def test_confidence_rule_boundaries() -> None:
    """③′ 置信度分档的边界（9/10/29/30）—— 纯函数，不必造库。"""
    assert confidence_for(0) == "low"
    assert confidence_for(9) == "low"
    assert confidence_for(10) == "medium"
    assert confidence_for(29) == "medium"
    assert confidence_for(30) == "high"


def test_no_metrics_gives_none_not_zero(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """没有读数时给 ``None``（"不知道"）而不是 0（"没人看"）—— 两者的处置相反。"""
    task = make_task(connection)
    make_publication(connection, task_id=task, published_at="2026-09-08T02:00:00.000Z", views=0)
    connection.execute("UPDATE publications SET metrics_json = '{}'")
    row = generate(connection, paths)
    assert row.publish_count == 1
    assert row.total_views is None
    assert row.avg_views is None
    assert "—" in row.summary_md


# ══════════════════════════════════════════════════════════════════════
# ② 幂等
# ══════════════════════════════════════════════════════════════════════


def test_same_period_is_idempotent(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """② 同周期重复生成 ⇒ **返回同一行**，表里还是一条。"""
    seed_window(connection, per_group=4)
    first = generate(connection, paths)
    second = generate(connection, paths)
    assert first.id == second.id
    count = connection.execute("SELECT count(*) FROM reports").fetchone()[0]
    assert count == 1


def test_invalid_period_rejected_by_check(connection: sqlite3.Connection) -> None:
    """⑥ 非法 ``period`` 被 DDL 的 CHECK 拒绝（**约束是最后一道防线**）。"""
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO reports(id, period, start_date, end_date, summary_md)"
            " VALUES ('x', 'hourly', '2026-09-07', '2026-09-13', 'x')"
        )


def test_invalid_period_rejected_by_service(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """⑥′ 服务层先拦一次（400 + ``REPORT_INVALID``），落库前就报错。"""
    with pytest.raises(StudioError) as info:
        generate(connection, paths, period="hourly")
    assert info.value.code == ErrorCode.REPORT_INVALID


# ══════════════════════════════════════════════════════════════════════
# ④⑤ 决策闭环
# ══════════════════════════════════════════════════════════════════════


def test_apply_writes_persona_and_audit(
    connection: sqlite3.Connection, paths: StudioPaths, state: AppState
) -> None:
    """④ 采纳 ⇒ 写进人物偏好（走 ``PersonaStore``）+ ``audit_ops`` 带 before/after。"""
    seed_window(connection, per_group=12)
    row = generate(connection, paths)
    topic_index = next(index for index, item in enumerate(row.insights) if item["kind"] == "topic")
    before_hint = state.persona.current().config.style_hint

    updated = apply_insight(
        connection=connection,
        persona=state.persona,
        report_id=row.id,
        index=topic_index,
        actor="user",
        now=NOW,
    )
    after_hint = state.persona.current().config.style_hint
    assert after_hint != before_hint
    assert "【数据结论" in after_hint
    assert updated.insights[topic_index]["applied"] is True
    assert updated.applied_count == 1
    # 留痕：谁在何时依据哪条建议改了什么
    ops = [item for item in AuditRepo(connection).list_recent(limit=20) if item.action == AUDIT_APPLY]
    assert ops, "采纳必须留痕"
    assert "style_hint" in ops[0].before
    assert "style_hint" in ops[0].after
    # 人物那一侧也要有留痕（PersonaStore 自己写的那条）
    actions = {item.action for item in AuditRepo(connection).list_recent(limit=50)}
    assert "persona.update" in actions


def test_apply_is_idempotent(connection: sqlite3.Connection, paths: StudioPaths, state: AppState) -> None:
    """④′ 已经采纳过的再点一次**什么都不做**（提示词不许重复追加）。"""
    seed_window(connection, per_group=12)
    row = generate(connection, paths)
    index = next(index for index, item in enumerate(row.insights) if item["kind"] == "topic")
    first = apply_insight(
        connection=connection, persona=state.persona, report_id=row.id, index=index, now=NOW
    )
    hint_after_first = state.persona.current().config.style_hint
    second = apply_insight(
        connection=connection, persona=state.persona, report_id=row.id, index=index, now=NOW
    )
    assert second.applied_count == first.applied_count == 1
    assert state.persona.current().config.style_hint == hint_after_first


def test_applied_preference_reaches_planner_variables(
    connection: sqlite3.Connection, paths: StudioPaths, state: AppState
) -> None:
    """⑤ **闭环**：采纳之后，下一轮 Planner 读到的 persona 变量里就有这条结论。

    断言的是 ``persona_variables`` —— 那是 ``agents/base.py`` 每次调用
    自动注入提示词的那份变量（含 Planner）。改 ``content_directions`` 的权重
    做不到这一点：下一轮 Planner 会重新生成批次，上一批的 priority 对它没有影响。
    """
    seed_window(connection, per_group=12)
    row = generate(connection, paths)
    index = next(index for index, item in enumerate(row.insights) if item["kind"] == "topic")
    statement = row.insights[index]["statement"]
    apply_insight(connection=connection, persona=state.persona, report_id=row.id, index=index, now=NOW)
    variables = persona_variables(state.persona.current().config)
    assert statement in variables["style_hint"]


def test_style_hint_keeps_only_latest_conclusions(
    connection: sqlite3.Connection, paths: StudioPaths, state: AppState
) -> None:
    """④″ 结论行只留最新 5 条，用户自己写的风格要求一条都不丢。"""
    PersonaService(state.persona).update(changes={"style_hint": "开头必须有一句反问"})
    applied = 0
    for day in range(1, 8):
        for index in range(12):
            hook = "数字" if index % 2 == 0 else "悬念"
            task = make_task(connection, title=f"{day}-{index}", hook_type=hook)
            make_publication(
                connection,
                task_id=task,
                published_at=f"2026-09-{day:02d}T02:00:00.000Z",
                views=1_000 if index % 2 == 0 else 300,
            )
        row = generate(
            connection,
            paths,
            period="daily",
            start_date=f"2026-09-{day:02d}",
            end_date=f"2026-09-{day:02d}",
        )
        slot = next((i for i, item in enumerate(row.insights) if item["kind"] == "topic"), None)
        if slot is None:
            continue
        apply_insight(
            connection=connection,
            persona=state.persona,
            report_id=row.id,
            index=slot,
            now=f"2026-09-{day:02d}T23:00:00.000Z",
        )
        applied += 1
    hint = state.persona.current().config.style_hint
    assert applied == 7
    assert "开头必须有一句反问" in hint
    assert hint.count("【数据结论") == 5


def test_apply_index_out_of_range(
    connection: sqlite3.Connection, paths: StudioPaths, state: AppState
) -> None:
    """④‴ 越界的建议序号 ⇒ 400（``REPORT_INVALID``），不静默取第一条。"""
    seed_window(connection, per_group=4)
    row = generate(connection, paths)
    with pytest.raises(StudioError) as info:
        apply_insight(connection=connection, persona=state.persona, report_id=row.id, index=99, now=NOW)
    assert info.value.code == ErrorCode.REPORT_INVALID


# ══════════════════════════════════════════════════════════════════════
# ⑦⑧⑨ 周期（可编辑 · 同周期仅 1 个启用 · 停用即不再触发）
# ══════════════════════════════════════════════════════════════════════


def test_schedule_create_plans_next_run(connection: sqlite3.Connection) -> None:
    """新建周期 ⇒ ``next_run_at`` 当场算好落库（陷阱 #30：重启不丢计划）。

    用 daily 而不是 weekly：seed 里已经有一条启用中的 ``rsched_weekly``，
    而同周期只允许一条启用（那是下一条用例要验的事）。
    """
    row = create_schedule(
        connection=connection,
        spec={"period": "daily", "at_time": "09:00"},
        now="2026-09-14T01:00:00.000Z",  # 本地 2026-09-14 09:00:00 整
    )
    # 那一格正好等于 now ⇒ 取第二天（严格大于才算"下一次"）
    assert row.next_run_at == "2026-09-15T01:00:00.000Z"
    assert row.lookback_days == 1  # 缺省跟着 period 走


def test_schedule_edit_recomputes_next_run_and_audits(connection: sqlite3.Connection) -> None:
    """⑦ 编辑 ⇒ ``next_run_at`` 重算 **且** 写 ``audit_ops``（陷阱 #33 的同一条纪律）。"""
    before = ReportScheduleRepo(connection).require("rsched_weekly")
    assert before.next_run_at is None  # seed 的初值：还没排期
    updated = update_schedule(
        connection=connection,
        schedule_id=before.id,
        patch={"at_time": "21:30"},
        reason="改到晚上出报告",
        now="2026-09-14T01:00:00.000Z",
    )
    # now 是本地 09-14 09:00，21:30 还没到 ⇒ 就是**今晚**（本地 21:30 = UTC 13:30）
    assert updated.next_run_at == "2026-09-14T13:30:00.000Z"
    ops = [
        item for item in AuditRepo(connection).list_recent(limit=20) if item.action == AUDIT_SCHEDULE_UPDATE
    ]
    assert ops, "编辑周期必须留痕"
    assert ops[0].before.get("at_time") == "09:00"
    assert ops[0].after.get("at_time") == "21:30"


def test_only_one_enabled_schedule_per_period(connection: sqlite3.Connection) -> None:
    """⑧ 同周期只能有一条启用（部分唯一索引的友好版：给能照着做的错误）。"""
    with pytest.raises(StudioError) as info:
        create_schedule(
            connection=connection,
            spec={"period": "weekly", "weekday": 3},
            now="2026-09-14T01:00:00.000Z",
        )
    assert info.value.code == ErrorCode.REPORT_INVALID
    assert "rsched_weekly" in str(info.value)  # 错误里要指出是谁占着
    # 停用的那条不占位（"我想换一天"不必先删掉旧的）
    created = create_schedule(
        connection=connection,
        spec={"period": "weekly", "weekday": 3, "enabled": False},
        now="2026-09-14T01:00:00.000Z",
    )
    assert created.enabled is False
    assert created.next_run_at is None


def test_disabled_schedule_is_not_due(connection: sqlite3.Connection) -> None:
    """⑨ 停用 ⇒ ``next_run_at`` 清空 ⇒ 到期查询永远取不到它（陷阱 178）。"""
    update_schedule(
        connection=connection,
        schedule_id="rsched_weekly",
        patch={"enabled": False},
        now="2026-09-14T01:00:00.000Z",
    )
    row = ReportScheduleRepo(connection).require("rsched_weekly")
    assert row.next_run_at is None
    # 就算有人手动把时刻拨回过去，停用那一行也不会被取到（enabled=1 才是判据）
    connection.execute(
        "UPDATE report_schedules SET next_run_at = ? WHERE id = ?",
        ("2026-09-14T00:00:00.000Z", row.id),
    )
    assert ReportScheduleRepo(connection).list_due(now="2026-09-15T00:00:00.000Z") == ()


def test_builtin_schedule_cannot_be_deleted(connection: sqlite3.Connection) -> None:
    """⑨′ 内置周期**不可删、只能停用**（§3.3.20；seed 里那三条就是 ``is_builtin=1``）。"""
    with pytest.raises(StudioError) as info:
        delete_schedule(connection=connection, schedule_id="rsched_monthly")
    assert info.value.code == ErrorCode.REPORT_INVALID
    assert "只能停用" in str(info.value)
    # 停用可以，而且那一行还在
    stopped = update_schedule(
        connection=connection,
        schedule_id="rsched_monthly",
        patch={"enabled": False},
        now="2026-09-14T01:00:00.000Z",
    )
    assert stopped.enabled is False
    assert ReportScheduleRepo(connection).get("rsched_monthly") is not None
    # 非内置的照样能删
    created = create_schedule(
        connection=connection,
        spec={"period": "weekly", "weekday": 5, "enabled": False},
        now="2026-09-14T01:00:00.000Z",
    )
    assert delete_schedule(connection=connection, schedule_id=created.id) is True


def test_schedule_create_is_audited(connection: sqlite3.Connection) -> None:
    """⑦′ 新建周期也留痕（``report_schedule.create``）。"""
    create_schedule(
        connection=connection, spec={"period": "daily", "enabled": False}, now="2026-09-14T01:00:00.000Z"
    )
    actions = {item.action for item in AuditRepo(connection).list_recent(limit=20)}
    assert AUDIT_SCHEDULE_CREATE in actions


# ══════════════════════════════════════════════════════════════════════
# 调度：到点生成 / 没数据跳过
# ══════════════════════════════════════════════════════════════════════


def test_tick_generates_report_and_records_ok(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """到点 ⇒ 生成报告 + ``last_result='ok'`` + 排下一期（``trigger='scheduled'``）。"""
    seed_window(connection, per_group=5)
    schedule_id = make_due_schedule(connection)
    service = ReportSchedulerService(connection=connection, paths=paths)
    report = service.tick(now=NOW)
    assert report.generated == (schedule_id,)
    row = ReportScheduleRepo(connection).require(schedule_id)
    assert row.last_result == RESULT_OK
    assert row.last_report_id is not None
    assert row.next_run_at is not None and row.next_run_at > NOW
    stored = ReportRepo(connection).require(row.last_report_id)
    assert stored.trigger == "scheduled"
    assert stored.start_date == START and stored.end_date == END


def test_pending_schedule_gets_planned(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """陷阱 178：``next_run_at`` 为 NULL 的**启用**计划必须被补排期，不能永不触发。

    seed 里三条内置计划的初值就是 NULL（0006 的注释点名了这一条）。只写
    ``next_run_at <= now`` 的话，出厂自带的周报与月报一次都不会跑，而且
    面板上它们看起来完全正常 —— 这正是最难查的一类静默失败。
    """
    before = ReportScheduleRepo(connection).require("rsched_monthly")
    assert before.next_run_at is None
    report = ReportSchedulerService(connection=connection, paths=paths).tick(now=NOW)
    assert "rsched_monthly" in report.scheduled
    after = ReportScheduleRepo(connection).require("rsched_monthly")
    assert after.next_run_at == "2026-10-01T01:00:00.000Z"  # 每月 1 日 09:00（本地）
    # 停用的那条**不该**被补排期（它的 NULL 意思正好相反）
    daily = ReportScheduleRepo(connection).require("rsched_daily")
    assert daily.next_run_at is None


def test_tick_without_data_records_skipped_no_data(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """没数据 ⇒ ``skipped_no_data`` 且**不生成报告行**（空报告会污染列表）。"""
    schedule_id = make_due_schedule(connection)
    service = ReportSchedulerService(connection=connection, paths=paths)
    report = service.tick(now=NOW)
    assert report.skipped_no_data == (schedule_id,)
    row = ReportScheduleRepo(connection).require(schedule_id)
    assert row.last_result == RESULT_SKIPPED_NO_DATA
    assert row.fail_streak == 0  # 没数据**不是失败**
    assert connection.execute("SELECT count(*) FROM reports").fetchone()[0] == 0


def test_schedule_failure_alerts_after_threshold(
    connection: sqlite3.Connection, paths: StudioPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续失败到阈值 ⇒ ``system.alert(REPORT_FAILING)``（**不阻断生产**）。

    怎么造失败：让生成那一步炸（磁盘写不下去就是真机上最可能的那一种）。
    直接改 ``at_time`` 造不出失败 —— DDL 的 CHECK 会把非法值挡在写入口之外，
    那说明约束是对的，只是它不该被用来造测试数据。
    """
    schedule_id = make_due_schedule(connection)

    def boom(**kwargs: object) -> None:
        del kwargs
        raise OSError("No space left on device")

    monkeypatch.setattr(report_service, "generate_report", boom)
    collector = _Collector()
    service = ReportSchedulerService(connection=connection, paths=paths, log=collector)
    for index in range(FAIL_STREAK_ALERT):
        connection.execute(
            "UPDATE report_schedules SET next_run_at = ? WHERE id = ?",
            ("2026-09-14T00:00:00.000Z", schedule_id),
        )
        outcome = service.fire(
            ReportScheduleRepo(connection).require(schedule_id),
            now=f"2026-09-14T0{index}:30:00.000Z",
            trigger="scheduled",
        )
        assert outcome.failed
    row = ReportScheduleRepo(connection).require(schedule_id)
    assert row.fail_streak >= FAIL_STREAK_ALERT
    codes = [
        str(item["payload"]["code"])
        for item in collector.items
        if isinstance(item["payload"], dict) and "code" in item["payload"]
    ]
    assert str(AlertCode.REPORT_FAILING) in codes, "连败到阈值必须发一条 system.alert(REPORT_FAILING)"


def test_run_now_uses_manual_trigger(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """``run_now`` ⇒ ``trigger='manual'``（与到点触发的报告分得清）。"""
    seed_window(connection, per_group=3)
    outcome = run_now(connection=connection, paths=paths, schedule_id="rsched_weekly", now=NOW)
    assert outcome.ok
    stored = ReportRepo(connection).require(str(outcome.report_id))
    assert stored.trigger == "manual"


# ══════════════════════════════════════════════════════════════════════
# 导出 / 事件 / REST 面
# ══════════════════════════════════════════════════════════════════════


def test_export_md_and_csv(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """导出 md / csv；不认识的格式 ⇒ 400。"""
    seed_window(connection, per_group=4)
    row = generate(connection, paths)
    md_name, md_text, md_type = export_report(connection=connection, report_id=row.id, fmt="md")
    assert md_name == "weekly_2026-09-07_2026-09-13.md"
    assert md_text == row.summary_md
    assert md_type.startswith("text/markdown")
    csv_name, csv_text, csv_type = export_report(connection=connection, report_id=row.id, fmt="csv")
    assert csv_name.endswith(".csv")
    assert csv_text.startswith("dimension,key,n,")
    assert "by_hook_type,数字" in csv_text
    assert csv_type.startswith("text/csv")
    with pytest.raises(StudioError) as info:
        export_report(connection=connection, report_id=row.id, fmt="xlsx")
    assert info.value.code == ErrorCode.REPORT_INVALID


def test_generate_emits_ws_event(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """生成 ⇒ 一条 ``report.generated``（搭日志的车扇给 WS · §04.4.4）。"""
    seed_window(connection, per_group=3)
    collector = _Collector()
    row = generate(connection, paths, log=collector)
    assert EventKind.REPORT_GENERATED.value in collector.events()
    payloads = [
        item["payload"]
        for item in collector.items
        if isinstance(item["payload"], dict)
        and item["payload"].get(EVENT_PAYLOAD_KEY) == EventKind.REPORT_GENERATED.value
    ]
    assert payloads and payloads[0]["report_id"] == row.id
    assert payloads[0]["insights_count"] == len(row.insights)


def test_generate_is_audited(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """生成 ⇒ ``audit_ops(report.generate)``（§6.7.5 的留痕那一行）。"""
    seed_window(connection, per_group=3)
    generate(connection, paths)
    actions = {item.action for item in AuditRepo(connection).list_recent(limit=20)}
    assert AUDIT_GENERATE in actions


def test_api_roundtrip(client: TestClient, state: AppState) -> None:
    """REST 面走一遍：列表 / 生成 / 详情 / 采纳 / 导出 / 周期 CRUD。"""
    connection = state.connections.get()
    seed_window(connection, per_group=4)
    created = client.post(
        REPORTS_URL + "/generate",
        json={"period": "weekly", "start": START, "end": END},
    )
    assert created.status_code == 200, created.text
    report_id = created.json()["id"]
    listed = client.get(REPORTS_URL)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == report_id
    detail = client.get(f"{REPORTS_URL}/{report_id}")
    assert detail.status_code == 200
    assert detail.json()["summary_md"]
    index = next(i for i, item in enumerate(detail.json()["insights"]) if item["kind"] == "topic")
    applied = client.post(f"{REPORTS_URL}/{report_id}/insights/{index}/apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert applied.json()["style_hint"]
    exported = client.get(f"{REPORTS_URL}/{report_id}/export", params={"format": "csv"})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment" in exported.headers["content-disposition"]
    # 周期：seed 里已经有三条内置的（weekly / monthly 启用、daily 停用）
    seeded = client.get(SCHEDULES_URL).json()
    assert seeded["counts"]["total"] == 3
    assert seeded["counts"]["builtin"] == 3
    schedule = client.post(SCHEDULES_URL, json={"period": "daily", "at_time": "08:15", "lookback_days": 2})
    assert schedule.status_code == 200, schedule.text
    schedule_id = schedule.json()["id"]
    assert schedule.json()["next_run_at"] is not None
    patched = client.patch(f"{SCHEDULES_URL}/{schedule_id}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert client.get(SCHEDULES_URL).json()["counts"]["total"] == 4
    removed = client.delete(f"{SCHEDULES_URL}/{schedule_id}")
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True
    # 内置的删不掉：400 + 说清只能停用
    blocked = client.delete(f"{SCHEDULES_URL}/rsched_weekly")
    assert blocked.status_code == 400
    assert blocked.json()["code"] == str(ErrorCode.REPORT_INVALID)


def test_api_rejects_bad_period(client: TestClient) -> None:
    """非法周期 ⇒ **400**（``REPORT_INVALID``），不是 422。"""
    bad_schedule = client.post(SCHEDULES_URL, json={"period": "weekly", "weekday": 0, "at_time": "25:00"})
    assert bad_schedule.status_code == 400, bad_schedule.text
    assert bad_schedule.json()["code"] == str(ErrorCode.REPORT_INVALID)
    # weekly 缺 weekday：跨字段的判据同样走 400（Pydantic 拦不到它）
    missing_weekday = client.post(SCHEDULES_URL, json={"period": "weekly"})
    assert missing_weekday.status_code == 400
    # 模型层的形状错误才是 422（前端据此把红字标到输入框上）
    wrong_shape = client.post(REPORTS_URL + "/generate", json={"period": "hourly"})
    assert wrong_shape.status_code == 422


def test_period_bounds_defaults() -> None:
    """区间默认值：右端是**触发日的前一天**（周一一早生成的报告不该含今天）。"""
    now = datetime.fromisoformat("2026-09-14T01:00:00+00:00")  # 本地 09:00
    assert period_bounds("weekly", now=now) == ("2026-09-07", "2026-09-13")
    assert period_bounds("daily", now=now) == ("2026-09-13", "2026-09-13")
    assert period_bounds("monthly", now=now) == ("2026-08-14", "2026-09-13")
