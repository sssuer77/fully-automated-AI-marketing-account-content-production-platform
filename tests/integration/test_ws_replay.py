"""WS 实时推送集成测试（T1.7 验收 · §04.4.1 / §04.4.6）。

三条验收（todolist T1.7）
-------------------------
① 断线重连按 `since_id` 补发 —— **不丢不重**
② 高频进度被合并到 ≤2Hz（且最后一条是最新值）
③ 慢客户端被主动断开（1008 + `WS_CLIENT_SLOW`）

外加：握手快照、通道 / 任务 / 级别三重过滤、告警走 `system` 通道、
`WORKER_DEAD` 不升格为告警（T1.6 裁定 39）、`ping/pong`、`resync`、
**跨进程日志（另一条连接直接写表）也能推到前端**。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from tests.unit.ws.helpers import RecordingSink, StuckSink, event

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.errors import ErrorCode
from studio.core.paths import StudioPaths
from studio.core.proto import AlertCode
from studio.db import connect, migrate
from studio.ws.backpressure import OutboundBuffer
from studio.ws.hub import HubSettings
from studio.ws.protocol import Channel, EventKind, FrameType, parse_subscription

WS_URL = "/ws/ui"


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def state(tmp_path: Path) -> Iterator[AppState]:
    """一套隔离运行期（临时库 + 已迁移 + 50ms tail 周期）。"""
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
def client(state: AppState) -> Iterator[TestClient]:
    """真端点（TestClient 走完整 ASGI + lifespan）。"""
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


def _frames(session: WebSocketTestSession, count: int) -> list[dict[str, Any]]:
    return [json.loads(session.receive_text()) for _ in range(count)]


# ══════════════════════════════════════════════════════════════════════
# ① 握手快照 / 补发：不丢不重
# ══════════════════════════════════════════════════════════════════════


def test_handshake_sends_snapshot_per_channel(client: TestClient) -> None:
    with client.websocket_connect(f"{WS_URL}?channels=pools,tasks") as session:
        frames = _frames(session, 2)
    assert [frame["type"] for frame in frames] == ["snapshot", "snapshot"]
    assert [frame["channel"] for frame in frames] == ["pools", "tasks"]
    assert "pools" in frames[0]["data"]
    assert "tasks" in frames[1]["data"]
    assert frames[1]["data"]["limit"] == 20


def test_fresh_connect_gets_recent_logs_snapshot(client: TestClient, state: AppState) -> None:
    for index in range(3):
        state.logs.append(level="info", source="test", message=f"m{index}")
    with client.websocket_connect(f"{WS_URL}?channels=logs") as session:
        frame = json.loads(session.receive_text())
    assert frame["type"] == "snapshot"
    assert [row["message"] for row in frame["data"]["logs"]] == ["m0", "m1", "m2"]
    assert frame["data"]["truncated"] is False


def test_since_id_replay_is_exact_and_ordered(client: TestClient, state: AppState) -> None:
    for index in range(3):
        state.logs.append(level="info", source="test", message=f"m{index}")
    with client.websocket_connect(f"{WS_URL}?channels=logs&since_id=0") as session:
        frames = _frames(session, 3)
    assert [frame["data"]["message"] for frame in frames] == ["m0", "m1", "m2"]
    assert [frame["data"]["log_id"] for frame in frames] == [1, 2, 3]
    assert [frame["seq"] for frame in frames] == [1, 2, 3], "seq 每连接单调且无缺口"
    assert all(frame["type"] == "event" for frame in frames)


def test_reconnect_resumes_without_gap_or_dup(client: TestClient, state: AppState) -> None:
    """① 断线重连：只补发缺失的那一段（不丢不重）。"""
    state.logs.append(level="info", source="test", message="m1")
    state.logs.append(level="info", source="test", message="m2")
    with client.websocket_connect(f"{WS_URL}?channels=logs&since_id=0") as session:
        first = _frames(session, 2)
    last_id = int(first[-1]["data"]["log_id"])

    state.logs.append(level="info", source="test", message="m3")
    with client.websocket_connect(f"{WS_URL}?channels=logs&since_id={last_id}") as session:
        second = _frames(session, 1)

    assert [frame["data"]["message"] for frame in second] == ["m3"]
    assert second[0]["data"]["log_id"] == last_id + 1
    assert second[0]["seq"] == 1, "新连接的 seq 从头开始"


def test_live_log_is_pushed_after_connect(client: TestClient, state: AppState) -> None:
    with client.websocket_connect(f"{WS_URL}?channels=logs&since_id=0") as session:
        state.logs.append(level="info", source="test", message="live")
        frame = json.loads(session.receive_text())
    assert frame["data"]["kind"] == EventKind.LOG_APPENDED.value
    assert frame["data"]["message"] == "live"


def test_logs_written_outside_log_service_are_still_pushed(client: TestClient, state: AppState) -> None:
    """worker 是**独立进程**：它直接写表，没有 IPC 也能到前端（tail 的价值所在）。"""
    with client.websocket_connect(f"{WS_URL}?channels=logs&since_id=0") as session:
        other = connect(state.paths.db_file)
        try:
            other.execute(
                "INSERT INTO system_logs(level, source, message) VALUES ('info', 'pool.render', '外进程写入')"
            )
        finally:
            other.close()
        frame = json.loads(session.receive_text())
    assert frame["data"]["source"] == "pool.render"
    assert frame["data"]["message"] == "外进程写入"


# ══════════════════════════════════════════════════════════════════════
# 过滤：通道 / 任务 / 级别
# ══════════════════════════════════════════════════════════════════════


def test_debug_is_not_persisted_so_no_min_level_can_show_it(client: TestClient, state: AppState) -> None:
    """`logging.yaml: database.min_level=INFO` ⇒ `debug` 不落库，前端任何 `min_level` 都看不到。

    这是**有意**的：日志表是给"事后追溯"用的，高频 debug 只会把它撑爆（§03.7.5 保留期）。
    """
    assert state.logs.append(level="debug", source="test", message="noise") is None
    state.logs.append(level="info", source="test", message="info-row")
    state.logs.append(level="warn", source="test", message="warn-row")

    with client.websocket_connect(f"{WS_URL}?channels=logs") as session:
        frame = json.loads(session.receive_text())
    assert [row["message"] for row in frame["data"]["logs"]] == ["info-row", "warn-row"]

    with client.websocket_connect(f"{WS_URL}?channels=logs&min_level=debug") as session:
        frame = json.loads(session.receive_text())
    assert [row["message"] for row in frame["data"]["logs"]] == ["info-row", "warn-row"]


def test_min_level_warn_hides_info_rows(client: TestClient, state: AppState) -> None:
    state.logs.append(level="info", source="test", message="info-row")
    state.logs.append(level="warn", source="test", message="warn-row")
    with client.websocket_connect(f"{WS_URL}?channels=logs&min_level=warn") as session:
        frame = json.loads(session.receive_text())
    assert [row["message"] for row in frame["data"]["logs"]] == ["warn-row"]


def test_task_filter_excludes_other_tasks(client: TestClient, state: AppState) -> None:
    state.logs.append(level="info", source="test", message="mine", task_id="t1")
    state.logs.append(level="info", source="test", message="other", task_id="t2")
    with client.websocket_connect(f"{WS_URL}?channels=logs&task_ids=t1") as session:
        frame = json.loads(session.receive_text())
    assert [row["message"] for row in frame["data"]["logs"]] == ["mine"]


def test_unsubscribed_channel_is_not_delivered(client: TestClient, state: AppState) -> None:
    """订阅 `pools` 的连接收不到日志：写一条日志后立刻 ping，pong 之前不应有日志帧。"""
    with client.websocket_connect(f"{WS_URL}?channels=pools&since_id=0") as session:
        assert json.loads(session.receive_text())["channel"] == "pools"  # 握手快照
        state.logs.append(level="info", source="test", message="不该出现")
        session.send_text(json.dumps({"type": "ping"}))
        frame = json.loads(session.receive_text())
    assert frame["type"] == "pong", "订阅 pools 的连接不该收到日志帧"


def test_invalid_subscription_is_rejected(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"{WS_URL}?channels=telemetry") as session,
    ):
        session.receive_text()


# ══════════════════════════════════════════════════════════════════════
# 告警：走 system 通道；非 8 值码不升格
# ══════════════════════════════════════════════════════════════════════


def test_alert_is_delivered_on_system_channel(client: TestClient, state: AppState) -> None:
    with client.websocket_connect(f"{WS_URL}?channels=system&since_id=0") as session:
        state.logs.alert(AlertCode.JOB_DEAD, message="任务死信", hint="去死信区重投", task_id="t1")
        frame = json.loads(session.receive_text())
    assert frame["channel"] == "system"
    assert frame["data"]["kind"] == EventKind.SYSTEM_ALERT.value
    assert frame["data"]["code"] == "JOB_DEAD"
    assert frame["data"]["hint"] == "去死信区重投"


def test_worker_dead_stays_a_log_not_an_alert(client: TestClient, state: AppState) -> None:
    """`WORKER_DEAD` 不在 §04.5.2 的 8 个告警码里 ⇒ 只能是 `log.appended`（裁定 39）。"""
    state.logs.append(
        level="error",
        source="pool.draft",
        message="worker 心跳超时",
        payload={"code": ErrorCode.WORKER_DEAD.value, "severity": "error"},
    )
    with client.websocket_connect(f"{WS_URL}?channels=system,logs&since_id=0") as session:
        frame = json.loads(session.receive_text())
    assert frame["channel"] == "logs"
    assert frame["data"]["kind"] == EventKind.LOG_APPENDED.value
    assert frame["data"]["payload"]["code"] == "WORKER_DEAD"


# ══════════════════════════════════════════════════════════════════════
# 上行：ping / resync / 非法
# ══════════════════════════════════════════════════════════════════════


def test_ping_pong_and_unknown_uplink(client: TestClient) -> None:
    with client.websocket_connect(f"{WS_URL}?channels=control") as session:
        session.send_text(json.dumps({"type": "ping"}))
        pong = json.loads(session.receive_text())
        assert pong["type"] == "pong"

        session.send_text(json.dumps({"type": "launch_missiles"}))
        error = json.loads(session.receive_text())
        assert error["type"] == "error"
        assert error["data"]["code"] == str(ErrorCode.WS_PROTOCOL_VIOLATION)

        session.send_text("not json at all")
        broken = json.loads(session.receive_text())
        assert broken["data"]["code"] == str(ErrorCode.WS_PROTOCOL_VIOLATION)


def test_resync_resends_snapshots(client: TestClient) -> None:
    with client.websocket_connect(f"{WS_URL}?channels=tasks") as session:
        assert json.loads(session.receive_text())["type"] == "snapshot"
        session.send_text(json.dumps({"type": "resync"}))
        ack = json.loads(session.receive_text())
        again = json.loads(session.receive_text())
    assert ack["type"] == "ack"
    assert again["type"] == "snapshot"
    assert again["channel"] == "tasks"


# ══════════════════════════════════════════════════════════════════════
# ② 合并 / 限流（Hub 级，假出口 + 真时钟）
# ══════════════════════════════════════════════════════════════════════


async def test_high_frequency_progress_is_coalesced(state: AppState) -> None:
    """② 60 次进度突发 ⇒ 1 秒内最多几条（2Hz），且最后一条是最新值。"""
    state.hub.start()
    sink = RecordingSink()
    conn = state.hub.connect(
        conn_id="c1",
        subscription=parse_subscription({"channels": "tasks"}),
        sink=sink,
    )
    try:
        for progress in range(1, 61):
            state.hub.publish(
                event(EventKind.TASK_UPDATED.value, channel=Channel.TASKS, id="t1", progress=progress)
            )
        await asyncio.sleep(1.2)
    finally:
        await state.hub.close_connection(conn, code=1000, reason="test")
        await state.hub.stop()

    updates = [frame for frame in sink.frames if frame["data"].get("kind") == EventKind.TASK_UPDATED.value]
    assert 1 <= len(updates) <= 4, f"2Hz 限流没生效：收到 {len(updates)} 条"
    assert updates[-1]["data"]["progress"] == 60, "合并必须保留最新值"


async def test_alerts_are_never_coalesced(state: AppState) -> None:
    state.hub.start()
    sink = RecordingSink()
    conn = state.hub.connect(
        conn_id="c1",
        subscription=parse_subscription({"channels": "system"}),
        sink=sink,
    )
    try:
        for index in range(5):
            state.hub.publish(
                event(EventKind.SYSTEM_ALERT.value, channel=Channel.SYSTEM, code="JOB_DEAD", id=index)
            )
        await asyncio.sleep(0.4)
    finally:
        await state.hub.close_connection(conn, code=1000, reason="test")
        await state.hub.stop()

    alerts = [frame for frame in sink.frames if frame["data"].get("kind") == EventKind.SYSTEM_ALERT.value]
    assert len(alerts) == 5


# ══════════════════════════════════════════════════════════════════════
# ③ 慢客户端
# ══════════════════════════════════════════════════════════════════════


async def test_slow_client_is_disconnected(tmp_path: Path) -> None:
    """③ 发不出去且持续超阈值 ⇒ 断开（1008 + WS_CLIENT_SLOW），不拖慢整个 Hub。"""
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    state = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.02, slow_sustain_sec=0.2),
    )
    state.hub.start()
    sink = StuckSink()
    conn = state.hub.connect(
        conn_id="slow",
        subscription=parse_subscription({"channels": "tasks"}),
        sink=sink,
    )
    try:
        for index in range(300):
            state.hub.publish(
                event(EventKind.TASK_UPDATED.value, channel=Channel.TASKS, id=f"t{index}", progress=1)
            )
        await asyncio.sleep(0.8)
        assert conn.closed is True
        assert sink.closed is not None
        assert sink.closed[0] == 1008
        assert "WS_CLIENT_SLOW" in sink.closed[1]
        assert state.hub.stats()["connections"] == 0
    finally:
        await state.hub.stop()
        state.close()


async def test_fast_client_is_not_disconnected(state: AppState) -> None:
    state.hub.start()
    sink = RecordingSink()
    conn = state.hub.connect(
        conn_id="fast",
        subscription=parse_subscription({"channels": "tasks"}),
        sink=sink,
    )
    try:
        for index in range(50):
            state.hub.publish(
                event(EventKind.TASK_UPDATED.value, channel=Channel.TASKS, id=f"t{index}", progress=1)
            )
        await asyncio.sleep(0.6)
        assert conn.closed is False
        assert sink.closed is None
        assert len(sink.frames) >= 50
    finally:
        await state.hub.close_connection(conn, code=1000, reason="test")
        await state.hub.stop()


# ══════════════════════════════════════════════════════════════════════
# REST 兜底分页（§04.4.6："更早日志请走 REST 分页"）
# ══════════════════════════════════════════════════════════════════════


def test_logs_endpoint_paginates(client: TestClient, state: AppState) -> None:
    for index in range(5):
        state.logs.append(level="info", source="test", message=f"m{index}")

    page = client.get("/api/v1/logs", params={"limit": 3}).json()
    assert [row["message"] for row in page["logs"]] == ["m2", "m3", "m4"]
    assert page["next_since_id"] == 5

    older = client.get("/api/v1/logs", params={"limit": 2, "since_id": 0}).json()
    assert [row["message"] for row in older["logs"]] == ["m0", "m1"]


def test_health_reports_hub_stats(client: TestClient) -> None:
    payload = client.get("/api/v1/health").json()
    assert payload["ok"] is True
    assert payload["ws"]["connections"] == 0
    assert payload["latest_log_id"] >= 0


def test_dropped_frames_are_counted_not_silent(state: AppState) -> None:
    """环形缓冲满了要留痕（`dropped` 计数），不能静默吞掉。"""
    buffer = OutboundBuffer(capacity=1)
    buffer.push(event(EventKind.TASK_UPDATED.value, id="t1", level="info"))
    buffer.push(event(EventKind.TASK_UPDATED.value, id="t2", level="info"))
    assert buffer.dropped == 1
    assert state.hub.stats()["clients"] == []


def test_foreign_sqlite_connection_sees_committed_log(state: AppState) -> None:
    """`append()` 是 autocommit ⇒ 另一条连接立刻看得见（tail 能捞到的前提）。"""
    state.logs.append(level="info", source="test", message="visible")
    other: sqlite3.Connection = connect(state.paths.db_file)
    try:
        row = other.execute("SELECT COUNT(*) AS n FROM system_logs").fetchone()
        assert int(row["n"]) == 1
    finally:
        other.close()


def test_snapshot_frame_type_is_registered() -> None:
    assert FrameType.SNAPSHOT.value == "snapshot"
