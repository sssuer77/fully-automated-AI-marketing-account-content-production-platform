"""总览台 REST 面集成测试（T4.2 验收 · §04.4.5 第 1 行）。

验收五条（todolist T4.2）
-------------------------
① 四池状态卡片（`pending`/`claimed`/`failed`/`dead`）+ worker 心跳；
② 队列长度、今日产量（**本地日**口径）；
③ 资源占用（CPU / 内存 / 显存 / 磁盘），采样 0.2 Hz；
④ `free_D < 15GB` ⇒ `DISK_LOW` 告警（且**只在状态迁移时**写一次）；
⑤ 启动 / 暂停 / 一键全自动（切 `GRADE_AB` 且写 `audit_ops`，可一键回退 `GRADE_A`）。

三条测试纪律
------------
1. **临时家目录**：`config_dir` 由 `home` 决定，而"一键全自动"会**真的写盘**。
   拿仓库根当 home 会让用例把开发机的 `auto_approve_policy` 改掉（第一次跑这个
   用例时就踩到了），所以 fixture 先把 `config/app.yaml` 抄进 tmp。
2. **假探针**：`build_state(metrics_probe=…)` 注入 —— 否则 `pump.tick()` 会碰
   `nvidia-smi` 与真磁盘，用例随开发机的显存与剩余空间飘。
3. **时间注入**：`local_day_window(now=…)` 是纯函数，边界用例直接钉时刻；
   需要"现在"的地方用 `now_iso()`（库与 API 同一口径）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state, overview_service_for
from studio.app.main import create_app
from studio.app.routers import overview as overview_router
from studio.core.clock import local_tz, now_iso
from studio.core.paths import StudioPaths
from studio.db import JobStore
from studio.db.migrate import migrate
from studio.db.repositories import AuditRepo, StatsRepo, local_day_window
from studio.pools.runner import POOL_NAMES
from studio.services.log_service import SystemLog, is_alert_code
from studio.services.metrics_service import MetricsService, ResourceSnapshot
from studio.services.service_manager import StartReport
from studio.ws.hub import HubSettings
from studio.ws.protocol import WS_PATH, Envelope

REPO_ROOT = Path(__file__).resolve().parents[2]

OVERVIEW_URL = "/api/v1/overview"
POOLS_URL = "/api/v1/overview/pools"
AUTO_URL = "/api/v1/overview/auto"
START_URL = "/api/v1/overview/start"


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


class _Probe:
    """可编排的假探针（默认磁盘充裕、GPU 在）。

    `free_d_gb` 是**可变**的：磁盘告警那几条用例靠翻它来造状态迁移。
    """

    def __init__(self) -> None:
        self.free_d_gb = 100.0
        self.free_c_gb = 50.0
        self.calls = 0

    def __call__(self, **kwargs: Any) -> ResourceSnapshot:
        del kwargs
        self.calls += 1
        return ResourceSnapshot(
            sampled_at=now_iso(),
            cpu_pct=12.5,
            ram_used_mb=4096,
            ram_total_mb=32768,
            ram_pct=12.5,
            process_rss_mb=256,
            disk_free_c_gb=self.free_c_gb,
            disk_free_d_gb=self.free_d_gb,
            disk_free_d_min_gb=15.0,
            disk_drive="D:",
            disk_low=self.free_d_gb < 15.0,
            gpu_name="NVIDIA GeForce RTX 2070",
            gpu_util_pct=3.0,
            gpu_mem_used_mb=1024,
            gpu_mem_total_mb=8192,
        )


@pytest.fixture
def probe() -> _Probe:
    return _Probe()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """临时家目录：把真 `config/app.yaml` 抄一份（"一键全自动"写的是**副本**）。"""
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "app.yaml", root / "config" / "app.yaml")
    return root


@pytest.fixture
def paths(home: Path, tmp_path: Path) -> StudioPaths:
    return StudioPaths(home=home, data_dir=tmp_path / "data")


@pytest.fixture
def state(paths: StudioPaths, probe: _Probe) -> Iterator[AppState]:
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    built = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=probe,
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


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════


def _capture(state: AppState, monkeypatch: pytest.MonkeyPatch) -> list[Envelope]:
    """把 `Hub.publish` 换成一个收集器（**不动**合并器 / 限流 / 背压）。"""
    captured: list[Envelope] = []
    monkeypatch.setattr(state.hub, "publish", captured.append)
    return captured


def _kinds(captured: list[Envelope]) -> list[str]:
    return [str(envelope.data.get("kind")) for envelope in captured]


def _alerts(state: AppState) -> list[SystemLog]:
    return [row for row in state.logs.recent(limit=200) if row.is_alert]


def _add_task(connection: sqlite3.Connection, task_id: str, **columns: Any) -> None:
    names = ["id", *columns]
    placeholders = ", ".join("?" for _ in names)
    connection.execute(
        f"INSERT INTO tasks({', '.join(names)}) VALUES ({placeholders})",
        (task_id, *columns.values()),
    )


# ══════════════════════════════════════════════════════════════════════
# ① 读 · 四池与心跳
# ══════════════════════════════════════════════════════════════════════


def test_overview_lists_four_pools_in_canonical_order(client: TestClient) -> None:
    body = client.get(OVERVIEW_URL).json()
    assert [card["pool"] for card in body["pools"]] == list(POOL_NAMES)
    for card in body["pools"]:
        assert card["error"] is None
        assert card["paused"] is False
        assert card["concurrency"] >= 0
        assert card["workers"] == []


def test_overview_pool_card_carries_worker_heartbeat(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    connection.execute(
        "INSERT INTO worker_heartbeats(worker_id, pool, status, last_seen_at, gpu_mem_mb)"
        " VALUES (?, ?, ?, ?, ?)",
        ("render#1@4242", "render", "busy", now_iso(), 2048),
    )
    body = client.get(OVERVIEW_URL).json()
    card = next(item for item in body["pools"] if item["pool"] == "render")
    beat = card["workers"][0]
    assert beat["worker_id"] == "render#1@4242"
    assert beat["status"] == "busy"
    assert beat["gpu_mem_mb"] == 2048
    assert beat["stale"] is False
    assert body["worker_total"] == 1
    assert body["worker_alive"] == 1


def test_stale_heartbeat_is_flagged_but_dead_is_not(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """超 15s 没动静 ⇒ `stale`；已经标死的不算"疑似"（那是已确认的事实）。"""
    old = (datetime.now(UTC) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    connection.execute(
        "INSERT INTO worker_heartbeats(worker_id, pool, status, last_seen_at) VALUES (?, ?, ?, ?)",
        ("draft#1@1", "draft", "busy", old),
    )
    connection.execute(
        "INSERT INTO worker_heartbeats(worker_id, pool, status, last_seen_at) VALUES (?, ?, ?, ?)",
        ("draft#2@2", "draft", "dead", old),
    )
    body = client.get(OVERVIEW_URL).json()
    card = next(item for item in body["pools"] if item["pool"] == "draft")
    flags = {beat["worker_id"]: beat["stale"] for beat in card["workers"]}
    assert flags == {"draft#1@1": True, "draft#2@2": False}
    assert body["worker_total"] == 2
    assert body["worker_alive"] == 1


# ══════════════════════════════════════════════════════════════════════
# ② 读 · 今日产量
# ══════════════════════════════════════════════════════════════════════


def test_local_day_window_uses_local_midnight() -> None:
    """UTC 17:00 = 本地次日 01:00（UTC+8）⇒ 属于**下一个**本地日。

    这条就是"不能用 `substr(created_at,1,10)`"的理由：那样会把本地 09-14 的
    前 8 小时算成 09-13（陷阱 #75）。
    """
    day, start, end = local_day_window(datetime(2026, 9, 13, 17, 0, tzinfo=UTC))
    assert day == "2026-09-14"
    assert start == "2026-09-13T16:00:00.000Z"
    assert end == "2026-09-14T16:00:00.000Z"


def test_overview_today_window_matches_local_day(client: TestClient) -> None:
    today = client.get(OVERVIEW_URL).json()["today"]
    start = datetime.fromisoformat(today["window_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(today["window_end"].replace("Z", "+00:00"))
    assert end - start == timedelta(days=1)
    local_start = start.astimezone(local_tz())
    assert (local_start.hour, local_start.minute, local_start.second) == (0, 0, 0)
    assert local_start.strftime("%Y-%m-%d") == today["day"]


def test_overview_counts_today_output(client: TestClient, connection: sqlite3.Connection) -> None:
    stamp = now_iso()
    _add_task(connection, "t1", title="出片了", status="completed", grade="A", finished_at=stamp)
    _add_task(connection, "t2", title="还在跑", status="drafting")
    _add_task(connection, "t3", title="炸了", status="failed", finished_at=stamp)
    body = client.get(OVERVIEW_URL).json()
    today = body["today"]
    assert today["created"] == 3
    assert today["completed"] == 1
    assert today["failed"] == 1
    assert today["by_grade"] == {"A": 1}
    assert today["by_status"] == {"completed": 1, "failed": 1}


def test_yesterday_output_is_not_counted(client: TestClient, connection: sqlite3.Connection) -> None:
    """**昨天**出片的不能算进今天（窗口是左闭右开的本地日）。"""
    yesterday = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _add_task(
        connection,
        "t1",
        title="昨天的",
        status="completed",
        grade="B",
        created_at=yesterday,
        finished_at=yesterday,
    )
    today = client.get(OVERVIEW_URL).json()["today"]
    assert today["created"] == 0
    assert today["completed"] == 0
    assert today["by_grade"] == {}


def test_stats_repo_by_grade_keeps_unknown_grade(connection: sqlite3.Connection) -> None:
    """没评级的成片归到 `?` 而不是丢掉 —— "有一条没走评分"本身就是信息。"""
    stamp = now_iso()
    _add_task(connection, "t1", title="无评级", status="completed", finished_at=stamp)
    assert StatsRepo(connection).today_output().by_grade == {"?": 1}


# ══════════════════════════════════════════════════════════════════════
# ③ 读 · 资源占用
# ══════════════════════════════════════════════════════════════════════


def test_resources_are_null_until_first_tick(state: AppState) -> None:
    """**不**发一个全 0 的假快照：0% CPU 与"还没采到"在面板上是两件事。

    这里刻意**不**用 `client` 夹具：`lifespan` 一起来泵就拍第一张，
    而"还没采过"只存在于"泵还没起跳"那一瞬。
    """
    body = overview_service_for(state).read().to_dict()
    assert body["resources"] is None
    assert body["resources_age_sec"] is None
    assert body["disk_low"] is None


def test_tick_fills_resources_and_age(client: TestClient, state: AppState, probe: _Probe) -> None:
    state.pump.tick()
    body = client.get(OVERVIEW_URL).json()
    assert body["resources"]["cpu_pct"] == 12.5
    assert body["resources"]["gpu_mem_total_mb"] == 8192
    assert body["resources"]["disk_drive"] == "D:"
    assert body["resources_age_sec"] is not None
    assert body["disk_low"] is False
    assert probe.calls >= 1  # 泵在 lifespan 里已经拍过一张


def test_metrics_snapshot_channel_is_registered(state: AppState) -> None:
    assert "metrics" in state.snapshots.channels()
    payload = state.snapshots("metrics", task_ids=None)
    assert payload["metrics"] is None
    assert payload["interval_sec"] == 5.0


# ══════════════════════════════════════════════════════════════════════
# ④ 写 · 暂停 / 恢复
# ══════════════════════════════════════════════════════════════════════


def test_pause_pool_writes_settings_and_audit(client: TestClient, connection: sqlite3.Connection) -> None:
    response = client.post(POOLS_URL, json={"pool": "render", "paused": True, "reason": "换素材"})
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["paused"] is True
    assert body["paused_by"] == "user"
    assert "在途 0 个" in body["note"]

    row = connection.execute("SELECT * FROM pool_settings WHERE pool = 'render'").fetchone()
    assert row["paused"] == 1
    assert row["paused_at"] == body["paused_at"]
    assert row["updated_by"] == "user"

    ops = AuditRepo(connection).list_recent(limit=10)
    assert [(op.action, op.target_id, op.reason) for op in ops] == [("pool.pause", "render", "换素材")]
    assert ops[0].actor == "user"
    assert ops[0].source == "webui"


def test_pause_is_idempotent_and_does_not_double_audit(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    client.post(POOLS_URL, json={"pool": "voice", "paused": True})
    second = client.post(POOLS_URL, json={"pool": "voice", "paused": True})
    assert second.json()["changed"] is False
    assert len(AuditRepo(connection).list_recent(limit=10)) == 1


def test_resume_clears_paused_at_and_audits(client: TestClient, connection: sqlite3.Connection) -> None:
    client.post(POOLS_URL, json={"pool": "voice", "paused": True})
    body = client.post(POOLS_URL, json={"pool": "voice", "paused": False}).json()
    assert body["changed"] is True
    assert body["paused_at"] is None
    assert body["paused_by"] is None
    row = connection.execute("SELECT * FROM pool_settings WHERE pool = 'voice'").fetchone()
    assert row["paused"] == 0
    assert row["paused_at"] is None
    actions = [op.action for op in AuditRepo(connection).list_recent(limit=10)]
    assert actions == ["pool.resume", "pool.pause"]


def test_pause_stops_claiming_but_lets_inflight_finish(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """T4.2 的核心语义：**在途跑完，新单元不认领**（不重启 worker）。"""
    _add_task(connection, "t1", title="任务")
    store = JobStore(connection)
    store.enqueue(task_id="t1", pool="render", unit_type="final", unit_ref="final-1")
    store.enqueue(task_id="t1", pool="render", unit_type="final", unit_ref="final-2")
    inflight = store.claim(pool="render", worker_id="render#1@1", lease_sec=600)
    assert inflight is not None

    body = client.post(POOLS_URL, json={"pool": "render", "paused": True}).json()
    assert body["running"] == 1
    assert body["pending"] == 1
    assert "在途 1 个" in body["note"]

    # 在途那个的租约与状态**一点没动**（不是"被回收后重排"）
    assert store.get(inflight.id).status == "claimed"
    # 新的不再被认领
    assert store.claim(pool="render", worker_id="render#1@1", lease_sec=600) is None

    # 恢复之后又能认领 —— 但要等在途那个把并发位让出来（render 池 concurrency=1）
    client.post(POOLS_URL, json={"pool": "render", "paused": False})
    assert store.claim(pool="render", worker_id="render#1@1", lease_sec=600) is None
    assert store.succeed(job_id=inflight.id, worker_id="render#1@1") is True
    assert store.claim(pool="render", worker_id="render#1@1", lease_sec=600) is not None


def test_pause_pool_rejects_unknown_pool(client: TestClient) -> None:
    response = client.post(POOLS_URL, json={"pool": "bogus", "paused": True})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


def test_pause_pool_rejects_extra_fields(client: TestClient) -> None:
    """`extra="forbid"`：把 `paused` 写成 `pause` 必须报错，不能静默当"没暂停"。"""
    response = client.post(POOLS_URL, json={"pool": "render", "pause": True})
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════════════
# ⑤ 写 · 一键全自动
# ══════════════════════════════════════════════════════════════════════


def test_auto_approve_writes_yaml_and_audits(
    client: TestClient, connection: sqlite3.Connection, home: Path
) -> None:
    body = client.post(AUTO_URL, json={"policy": "grade_ab", "reason": "无人值守过夜"}).json()
    assert body["changed"] is True
    assert body["previous"] == "grade_a"
    assert body["policy"] == "grade_ab"

    text = (home / "config" / "app.yaml").read_text(encoding="utf-8")
    assert "  auto_approve_policy: grade_ab   # off | grade_a | grade_ab\n" in text
    # 其余注释与版式**原样保留**（不是 yaml.safe_dump 整份重写）
    assert text.count("# ── 应用配置") == 1
    assert "# 确认闸分级放行（§04.4.4）" in text

    ops = AuditRepo(connection).list_recent(limit=10)
    assert [(op.action, op.target_id, op.reason) for op in ops] == [
        ("approval.policy_change", "approval.auto_approve_policy", "无人值守过夜")
    ]
    assert ops[0].before == {"auto_approve_policy": "grade_a"}
    assert ops[0].after == {"auto_approve_policy": "grade_ab"}


def test_auto_approve_is_visible_immediately_and_reversible(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """**内存同步**：改完不用重启 API，`GET /overview` 下一拍就是新值（裁定 139）。"""
    client.post(AUTO_URL, json={"policy": "grade_ab"})
    assert client.get(OVERVIEW_URL).json()["settings"]["auto_approve_policy"] == "grade_ab"

    back = client.post(AUTO_URL, json={"policy": "grade_a", "reason": "回退"}).json()
    assert back["changed"] is True
    assert back["previous"] == "grade_ab"
    assert client.get(OVERVIEW_URL).json()["settings"]["auto_approve_policy"] == "grade_a"
    assert len(AuditRepo(connection).list_recent(limit=10)) == 2


def test_auto_approve_is_idempotent(client: TestClient, connection: sqlite3.Connection) -> None:
    assert client.post(AUTO_URL, json={"policy": "grade_a"}).json()["changed"] is False
    assert AuditRepo(connection).list_recent(limit=10) == []


def test_auto_approve_rejects_unknown_policy(client: TestClient) -> None:
    response = client.post(AUTO_URL, json={"policy": "grade_z"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


# ══════════════════════════════════════════════════════════════════════
# ④ 告警 · DISK_LOW 去重
# ══════════════════════════════════════════════════════════════════════


def test_disk_low_alert_is_written_once_per_transition(state: AppState, probe: _Probe) -> None:
    """`free_D < 15GB` 会**持续**成立，而 `system.alert` 永不合并 ⇒ 每拍写一条会刷屏。"""
    probe.free_d_gb = 10.0
    for _ in range(5):
        state.pump.tick()
    alerts = _alerts(state)
    assert len(alerts) == 1
    assert alerts[0].alert_code == "DISK_LOW"
    assert alerts[0].level == "warn"
    assert "10.00 GB" in alerts[0].message


def test_first_sample_on_healthy_disk_stays_silent(state: AppState, probe: _Probe) -> None:
    """首拍就健康 ⇒ **一个字都不写**：`None → ok` 不是恢复。

    这条是 T1.7 的补发用例逼出来的 —— 状态机把 `None`（还没采过）当成"低水位"，
    启动时凭空写一条 `DISK_RECOVERED`，它排在 tail 游标之后，于是顶掉了日志通道的
    第一帧，`test_ws_replay.py` 的两条用例直接挂掉。
    """
    probe.free_d_gb = 100.0
    state.pump.tick()
    assert state.metrics.disk_low is False
    assert [row.payload.get("code") for row in state.logs.recent(limit=50)] == []
    assert _alerts(state) == []


def test_disk_recovery_logs_info_and_is_not_an_alert(state: AppState, probe: _Probe) -> None:
    probe.free_d_gb = 10.0
    state.pump.tick()
    probe.free_d_gb = 40.0
    state.pump.tick()
    rows = state.logs.recent(limit=20)
    recovery = [row for row in rows if row.payload.get("code") == "DISK_RECOVERED"]
    assert len(recovery) == 1
    assert recovery[0].level == "info"
    assert recovery[0].source == "system"
    assert is_alert_code("DISK_RECOVERED") is False
    assert len(_alerts(state)) == 1  # 恢复**不**新增告警


def test_disk_alert_follows_state_transitions_not_throttle(state: AppState, probe: _Probe) -> None:
    """**API 起来时磁盘已经满了**同样要留痕（首拍照常判），去重靠"状态没变就不写"。"""
    probe.free_d_gb = 3.0
    state.pump.tick()
    assert [row.alert_code for row in _alerts(state)] == ["DISK_LOW"]
    probe.free_d_gb = 1.0  # 还在低位（更低了）：不是迁移，不重复写
    state.pump.tick()
    assert len(_alerts(state)) == 1
    probe.free_d_gb = 80.0  # 上来
    state.pump.tick()
    assert len(_alerts(state)) == 1
    probe.free_d_gb = 2.0  # 再掉下去 ⇒ 这才是第二次迁移
    state.pump.tick()
    assert len(_alerts(state)) == 2


# ══════════════════════════════════════════════════════════════════════
# ③ 事件 · `metrics.tick` / `pool.stats` / `pool.worker_status`
# ══════════════════════════════════════════════════════════════════════


def test_tick_publishes_metrics_tick_and_four_pool_stats(
    state: AppState, probe: _Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe.free_d_gb = 42.0
    captured = _capture(state, monkeypatch)
    state.pump.tick()

    kinds = _kinds(captured)
    assert kinds.count("metrics.tick") == 1
    assert kinds.count("pool.stats") == len(POOL_NAMES)

    tick = next(item for item in captured if item.data["kind"] == "metrics.tick")
    assert tick.channel.value == "metrics"
    assert tick.type.value == "event"
    assert tick.data["disk_free_gb"] == 42.0
    assert tick.data["ram_mb"] == 4096

    stats = [item for item in captured if item.data["kind"] == "pool.stats"]
    assert [item.data["pool"] for item in stats] == list(POOL_NAMES)
    assert all(item.channel.value == "pools" for item in stats)
    assert all("workers" not in item.data for item in stats)  # 心跳另走 worker_status


def test_worker_status_is_published_only_when_it_changes(
    state: AppState, connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture(state, monkeypatch)
    state.pump.tick()
    assert "pool.worker_status" not in _kinds(captured)

    connection.execute(
        "INSERT INTO worker_heartbeats(worker_id, pool, status, last_seen_at) VALUES (?, ?, ?, ?)",
        ("render#1@4242", "render", "busy", now_iso()),
    )
    captured.clear()
    state.pump.tick()
    beats = [item for item in captured if item.data["kind"] == "pool.worker_status"]
    assert [item.data["worker_id"] for item in beats] == ["render#1@4242"]
    assert beats[0].data["status"] == "busy"

    captured.clear()
    state.pump.tick()
    assert "pool.worker_status" not in _kinds(captured)


def test_ws_handshake_delivers_metrics_and_pools_snapshots(client: TestClient, state: AppState) -> None:
    state.pump.tick()
    with client.websocket_connect(f"{WS_PATH}?channels=metrics,pools") as session:
        frames = [json.loads(session.receive_text()) for _ in range(2)]
    assert [frame["channel"] for frame in frames] == ["metrics", "pools"]
    assert [frame["type"] for frame in frames] == ["snapshot", "snapshot"]
    assert frames[0]["data"]["metrics"]["disk_free_gb"] == 100.0
    assert frames[0]["data"]["interval_sec"] == 5.0
    assert sorted(frames[1]["data"]["pools"]) == sorted(POOL_NAMES)


def test_ws_receives_pool_stats_event(client: TestClient, state: AppState) -> None:
    with client.websocket_connect(f"{WS_PATH}?channels=pools") as session:
        assert json.loads(session.receive_text())["type"] == "snapshot"
        state.pump.tick()
        frame = json.loads(session.receive_text())
    assert frame["type"] == "event"
    assert frame["data"]["kind"] == "pool.stats"
    assert frame["data"]["pool"] in POOL_NAMES


# ══════════════════════════════════════════════════════════════════════
# ⑤ 写 · 启动
# ══════════════════════════════════════════════════════════════════════


class _FakeManager:
    """假编排器：只记录"被怎么调的"，绝不真起进程。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def start(self, **kwargs: Any) -> StartReport:
        self.calls.append(kwargs)
        return StartReport(
            started=("draft",),
            ready=("api", "draft"),
            degraded=("tts",),
            url="http://127.0.0.1:8787",
            browser_opened=bool(kwargs.get("open_browser")),
            elapsed_ms=1234,
        )


def test_start_never_opens_browser_and_keeps_doctor_gate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """从 WebUI 点"启动"时浏览器**已经开着**；门禁是磁盘/环境契约的硬底线。"""
    manager = _FakeManager()
    monkeypatch.setattr(overview_router, "_service_manager", lambda paths: manager)
    body = client.post(START_URL).json()
    assert manager.calls == [{"open_browser": False, "doctor_gate": True}]
    assert body["ok"] is True
    assert body["started"] == ["draft"]
    assert body["degraded"] == ["tts"]
    assert body["browser_opened"] is False


def test_start_is_single_flight(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """两次点击各自读到"没在跑" ⇒ 各拉一份进程（池 worker 会真的起两份）。"""
    monkeypatch.setattr(overview_router, "_service_manager", lambda paths: _FakeManager())
    with overview_router._START_GUARD.hold():
        response = client.post(START_URL)
    assert response.status_code == 409
    assert response.json()["code"] == "SERVICE_START_BUSY"
    # 锁放掉之后照常能用
    assert client.post(START_URL).status_code == 200


def test_overview_service_is_rebuilt_per_request_with_shared_settings(state: AppState) -> None:
    """`settings` 必须是**引用共享**：现造一份会让"改了但没变"变成常态。"""
    first = overview_service_for(state)
    second = overview_service_for(state)
    assert first is not second
    assert first._settings is second._settings


def test_metrics_service_is_shared_across_requests(state: AppState) -> None:
    """最近一拍与磁盘告警去重状态必须跨请求存活（否则每次刷新都从零开始）。"""
    assert isinstance(state.metrics, MetricsService)
    state.pump.tick()
    assert state.metrics.last is not None
    assert state.metrics.disk_low is False


def test_audit_repo_records_actor_user(connection: sqlite3.Connection) -> None:
    """留痕的 `actor` / `source` 是 DDL 的 CHECK 值（写错了会当场炸）。"""
    AuditRepo(connection).record(
        actor="user", action="pool.pause", target_type="pool", target_id="draft", source="webui"
    )
    row = AuditRepo(connection).list_recent(limit=1)[0]
    assert (row.actor, row.result, row.source) == ("user", "ok", "webui")
