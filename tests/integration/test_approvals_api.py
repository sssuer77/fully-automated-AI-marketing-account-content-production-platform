"""确认闸 REST 面集成测试（T4.4 验收 · §04.4.4）。

验收五条（todolist T4.4）
-------------------------
① 待审列表可见，且带 ``grade`` / ``score_total`` / ``revision_round``（原文 §2.2⑦）；
② 确认 / 退回（**必填意见**）/ 放弃三条路径各自的落点与留痕；
③ 批量通过**逐条**写 ``audit_ops``，且**部分失败不回滚**；
④ 不在闸里 / 重复决断 ⇒ 409（不是 500，也不是静默成功）；
⑤ 放弃可"捞回"（``discarded → pending``），被连带置 ``rejected`` 的选题候选一起恢复。

为什么造数据不走 LLM
--------------------
REST 面认的是**状态与行**，不是"模型说了什么"。把任务直接推到
``awaiting_approval`` 并写一条 ``approvals``，测的就是控制器 + 服务层这条链路；
真跑一遍 Reviewer/Editor 只会让这几条用例依赖网络与提示词版本。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.paths import StudioPaths
from studio.db import migrate
from studio.db.repositories import ApprovalRepo, AuditRepo, DirectionRepo, TopicRepo
from studio.domain import TaskService, TaskStatus
from studio.ws.hub import HubSettings

APPROVALS_URL = "/api/v1/approvals"
BATCH_URL = "/api/v1/approvals/approve_batch"


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def state(tmp_path: Path) -> Iterator[AppState]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    built = build_state(paths=paths, hub_settings=HubSettings(tail_interval_sec=0.05))
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


# ══════════════════════════════════════════════════════════════════════
# 造数据
# ══════════════════════════════════════════════════════════════════════


def seed_gate_task(
    connection: sqlite3.Connection,
    *,
    title: str = "MC跑酷最难的一跳",
    grade: str = "B",
    score: float = 7.2,
    revision_round: int = 2,
) -> tuple[str, str]:
    """把一个任务推到确认闸；返回 ``(task_id, approval_id)``。"""
    tasks = TaskService(connection)
    task_id = tasks.create(title=title).id
    tasks.transition(task_id, TaskStatus.DRAFTING, actor="test")
    tasks.transition(task_id, TaskStatus.REVIEWING, actor="test")
    # 走**合法**状态链把轮次推上去：`revision_round` 是任务上的字段，
    # 直接改库会让这条用例绕过状态机（而那正是确认闸要守的东西）。
    for _ in range(revision_round):
        tasks.transition(task_id, TaskStatus.EDITING, actor="auto", bump_revision=True)
        tasks.transition(task_id, TaskStatus.REVIEWING, actor="auto")
    tasks.transition(task_id, TaskStatus.AWAITING_APPROVAL, actor="auto")
    approval = ApprovalRepo(connection).request(
        task_id=task_id,
        script_id=None,
        grade=grade,
        score_total=score,
        revision_round=revision_round,
    )
    return task_id, approval.id


def seed_rejected_topic(connection: sqlite3.Connection, task_id: str) -> str:
    """造一条"被废弃"的选题候选（挂在任务上）—— 捞回用例的前置条件。"""
    direction_id = DirectionRepo(connection).insert_batch(
        batch_id=DirectionRepo.new_batch_id(),
        directions=[{"title": "方向一", "rationale": "因为跑酷好看", "priority": 10}],
    )[0]
    topics = TopicRepo(connection)
    topic_id = topics.insert_many(
        [{"direction_id": direction_id, "seq": 1, "title": "选题一", "angle": "结果前置"}]
    )[0]
    topics.set_status(topic_id=topic_id, status="rejected", task_id=task_id)
    return topic_id


def audit_actions(connection: sqlite3.Connection, task_id: str) -> list[str]:
    return [row.action for row in AuditRepo(connection).list_for_task(task_id)]


def task_status(connection: sqlite3.Connection, task_id: str) -> str:
    return TaskService(connection).get(task_id).status.value


# ══════════════════════════════════════════════════════════════════════
# ① 待审列表
# ══════════════════════════════════════════════════════════════════════


def test_pending_list_carries_the_three_things_the_gate_needs(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """§04.4.4：确认闸必须能看到 稿件 + 评分 + 修改次数，否则就是"盲点通过"。"""
    task_id, approval_id = seed_gate_task(connection)
    body = client.get(APPROVALS_URL, params={"status": "pending"}).json()
    assert body["counts"]["pending"] == 1
    assert body["limit"] == 200
    (item,) = body["approvals"]
    assert item["id"] == approval_id
    assert item["task_id"] == task_id
    assert item["grade"] == "B"
    assert item["score_total"] == 7.2
    assert item["revision_round"] == 2
    assert item["decided_by"] is None


def test_list_can_look_at_decided_history(client: TestClient, connection: sqlite3.Connection) -> None:
    """面板既要看"待审"，也要能回看"已决"（否则复盘只能靠翻日志）。"""
    task_id, _ = seed_gate_task(connection)
    client.post(f"/api/v1/tasks/{task_id}/approve", json={})
    pending = client.get(APPROVALS_URL, params={"status": "pending"}).json()
    approved = client.get(APPROVALS_URL, params={"status": "approved"}).json()
    assert pending["approvals"] == []
    (item,) = approved["approvals"]
    assert item["status"] == "approved"
    assert item["decided_by"] == "user"


def test_unknown_status_is_rejected(client: TestClient) -> None:
    response = client.get(APPROVALS_URL, params={"status": "whatever"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


# ══════════════════════════════════════════════════════════════════════
# ② 三条决断路径
# ══════════════════════════════════════════════════════════════════════


def test_approve_sends_the_task_to_the_voice_queue(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    task_id, _ = seed_gate_task(connection)
    response = client.post(f"/api/v1/tasks/{task_id}/approve", json={"comment": "这版可以"})
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approved"
    assert body["status"] == "queued_voice"
    assert body["revision_round"] == 2
    assert task_status(connection, task_id) == "queued_voice"
    assert "task.approve" in audit_actions(connection, task_id)


def test_approve_accepts_an_empty_body(client: TestClient, connection: sqlite3.Connection) -> None:
    """意见是可选的 —— 但**请求体本身**不该是必填（否则前端还得编一个 `{}`）。"""
    task_id, _ = seed_gate_task(connection)
    assert client.post(f"/api/v1/tasks/{task_id}/approve").status_code == 200


def test_reject_without_a_comment_is_refused(client: TestClient, connection: sqlite3.Connection) -> None:
    """不变量 3：退回必须带意见 —— 少了它，下一轮 Editor 拿到空 issues 只能瞎改。"""
    task_id, _ = seed_gate_task(connection)
    response = client.post(f"/api/v1/tasks/{task_id}/reject", json={})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert task_status(connection, task_id) == "awaiting_approval"


def test_reject_with_a_blank_comment_is_refused_by_the_service(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """只填空格能过 Pydantic，但过不了服务层 —— 业务不变量只有一个权威落点。"""
    task_id, _ = seed_gate_task(connection)
    response = client.post(f"/api/v1/tasks/{task_id}/reject", json={"comment": "   "})
    assert response.status_code == 422
    assert response.json()["code"] == "APPROVAL_COMMENT_REQUIRED"
    assert task_status(connection, task_id) == "awaiting_approval"


def test_reject_bumps_the_round_and_records_the_comment_everywhere(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """意见要同时进 `approvals` / `task_events` / `audit_ops`（下一轮 Editor 的输入）。"""
    task_id, _ = seed_gate_task(connection)
    response = client.post(f"/api/v1/tasks/{task_id}/reject", json={"comment": "开场太平，把结果前置"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "editing"
    assert body["revision_round"] == 3
    assert task_status(connection, task_id) == "editing"

    approval = ApprovalRepo(connection).get(body["approval_id"])
    assert approval is not None
    assert approval.comment == "开场太平，把结果前置"

    event = connection.execute(
        "SELECT reason FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    assert event is not None and event[0] == "开场太平，把结果前置"

    audit = AuditRepo(connection).list_for_task(task_id)
    assert audit[-1].action == "task.reject"
    assert audit[-1].reason == "开场太平，把结果前置"


def test_discard_sends_the_task_to_the_graveyard(client: TestClient, connection: sqlite3.Connection) -> None:
    task_id, _ = seed_gate_task(connection)
    response = client.post(f"/api/v1/tasks/{task_id}/discard", json={"reason": "选题不行"})
    assert response.status_code == 200
    assert response.json()["status"] == "discarded"
    assert task_status(connection, task_id) == "discarded"
    assert "task.discard" in audit_actions(connection, task_id)


# ══════════════════════════════════════════════════════════════════════
# ④ 状态不对就是 409
# ══════════════════════════════════════════════════════════════════════


def test_deciding_a_task_that_is_not_in_the_gate_is_a_conflict(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    tasks = TaskService(connection)
    task_id = tasks.create(title="还没写稿").id
    response = client.post(f"/api/v1/tasks/{task_id}/approve", json={})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "APPROVAL_NOT_PENDING"
    assert body["context"]["status"] == "pending"
    assert body["remediation"]


def test_deciding_twice_is_a_conflict_not_a_silent_success(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """第二次必须报错：静默成功会让人以为"两条都放行了"。"""
    task_id, _ = seed_gate_task(connection)
    assert client.post(f"/api/v1/tasks/{task_id}/approve", json={}).status_code == 200
    second = client.post(f"/api/v1/tasks/{task_id}/approve", json={})
    assert second.status_code == 409
    assert second.json()["code"] == "APPROVAL_NOT_PENDING"


def test_unknown_task_is_not_found(client: TestClient) -> None:
    response = client.post("/api/v1/tasks/no-such-task/approve", json={})
    assert response.status_code == 404
    assert response.json()["code"] == "TASK_NOT_FOUND"


# ══════════════════════════════════════════════════════════════════════
# ③ 批量
# ══════════════════════════════════════════════════════════════════════


def test_batch_approves_every_task_and_leaves_one_audit_row_each(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    ids = [seed_gate_task(connection, title=f"稿子{i}")[0] for i in range(3)]
    response = client.post(BATCH_URL, json={"task_ids": ids, "comment": "批量过"})
    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 3
    assert [item["task_id"] for item in body["approved"]] == ids
    assert body["failed"] == []
    for task_id in ids:
        assert task_status(connection, task_id) == "queued_voice"
        assert audit_actions(connection, task_id).count("task.approve") == 1


def test_batch_keeps_the_good_ones_when_one_fails(client: TestClient, connection: sqlite3.Connection) -> None:
    """★ 部分失败不回滚：把 2 成功 + 1 失败报成"整体失败"，会让人重按一次。"""
    good = [seed_gate_task(connection, title=f"稿子{i}")[0] for i in range(2)]
    stray = TaskService(connection).create(title="不在闸里").id
    response = client.post(BATCH_URL, json={"task_ids": [*good, stray]})
    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 3
    assert [item["task_id"] for item in body["approved"]] == good
    (failure,) = body["failed"]
    assert failure["task_id"] == stray
    assert failure["code"] == "APPROVAL_NOT_PENDING"
    assert failure["remediation"]
    assert task_status(connection, stray) == "pending"


def test_batch_with_an_empty_list_is_rejected(client: TestClient) -> None:
    response = client.post(BATCH_URL, json={"task_ids": []})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


def test_batch_rejects_unknown_fields(client: TestClient) -> None:
    """`extra="forbid"`：打错字段名不能被静默忽略（那会让人以为"意见写上了"）。"""
    response = client.post(BATCH_URL, json={"task_ids": ["t1"], "commnet": "typo"})
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════════════
# ⑤ 捞回
# ══════════════════════════════════════════════════════════════════════


def test_rescue_returns_a_discarded_task_to_pending(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    task_id, _ = seed_gate_task(connection)
    client.post(f"/api/v1/tasks/{task_id}/discard", json={"reason": "手滑了"})
    response = client.post(f"/api/v1/tasks/{task_id}/rescue", json={"reason": "点错了"})
    assert response.status_code == 200
    body = response.json()
    assert body["previous_status"] == "discarded"
    assert body["status"] == "pending"
    assert body["topic_restored"] is False
    assert task_status(connection, task_id) == "pending"
    assert "task.rescue" in audit_actions(connection, task_id)


def test_rescue_restores_the_rejected_topic_candidate(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """只把任务捞回来、选题仍躺在 `rejected` 里 ⇒ 下一轮分析看不到它（捞回只捞回一半）。"""
    task_id, _ = seed_gate_task(connection)
    topic_id = seed_rejected_topic(connection, task_id)
    client.post(f"/api/v1/tasks/{task_id}/discard", json={})
    response = client.post(f"/api/v1/tasks/{task_id}/rescue", json={})
    body = response.json()
    assert body["topic_id"] == topic_id
    assert body["topic_restored"] is True
    topic = TopicRepo(connection).get(topic_id)
    assert topic is not None and topic.status == "queued"


def test_rescue_refuses_a_task_that_is_still_running(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    task_id, _ = seed_gate_task(connection)
    response = client.post(f"/api/v1/tasks/{task_id}/rescue", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "APPROVAL_NOT_PENDING"


def test_rescue_accepts_an_empty_body(client: TestClient, connection: sqlite3.Connection) -> None:
    task_id, _ = seed_gate_task(connection)
    client.post(f"/api/v1/tasks/{task_id}/discard", json={})
    assert client.post(f"/api/v1/tasks/{task_id}/rescue").status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 事件与错误信封
# ══════════════════════════════════════════════════════════════════════


def test_approval_requested_is_declared_on_the_log_row(
    state: AppState, client: TestClient, connection: sqlite3.Connection
) -> None:
    """确认闸跑在写稿池进程里 ⇒ 事件只能搭日志的车跨进程到 Hub（T4.4 裁定 122）。

    这里断言**声明**已经写进行；扇出规则由 `tests/unit/ws/test_hub_events.py` 覆盖。
    """
    task_id, _ = seed_gate_task(connection)
    client.post(f"/api/v1/tasks/{task_id}/reject", json={"comment": "改一版"})
    rows = state.logs.recent(limit=20, source="review.pipeline")
    declared = [row.payload.get("event_kind") for row in rows]
    assert "approval.decided" in declared


def test_error_body_has_one_shape(client: TestClient, connection: sqlite3.Connection) -> None:
    """业务错误与入参校验错误必须**同形**，否则前端要写两套解析。"""
    task_id, _ = seed_gate_task(connection)
    business = client.post(f"/api/v1/tasks/{task_id}/reject", json={"comment": "  "}).json()
    validation = client.post(f"/api/v1/tasks/{task_id}/reject", json={}).json()
    expected = {"code", "message", "context", "remediation", "type"}
    assert set(business) == expected
    assert set(validation) == expected
