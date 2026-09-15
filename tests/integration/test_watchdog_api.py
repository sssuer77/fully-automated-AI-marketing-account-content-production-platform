"""无人值守守护 REST 面集成测试（T4.11 验收 · §04.5.10）。

验收三条（todolist T4.11 · §04.5.10）
------------------------------------
① `GET /api/v1/watchdog`：守谁 / 自愈几次 / 谁停手了 / 门禁什么水位 / **人工池有几条**；
② `POST /api/v1/watchdog/tick`：跑的是**同一个** `tick()`，且写 `audit_ops`；
   守护停用（`config/pools.yaml` 读不到）时**照跑照留痕** —— 回 200 + `note`，
   而不是一个 400；
③ 总览台那份（`/api/v1/overview` 的 `watchdog` 段）与独立面板**同一来源**。

四条测试纪律
------------
1. **临时家目录**：`config/pools.yaml` / `config/app.yaml` 抄进 tmp。守护会**真的**
   写 `pool_settings` 与 `audit_ops`，拿仓库根当 home 会改开发机的库；
2. **临时家目录里没有 `workers/`**：`ServiceManager.readiness` 恒为 `entry_missing`，
   一次 `start()` 都不会真的落到 `Popen`（`runnable` 为假，周期泵也不会起跳）。
   这既是安全边界，也是被测行为本身 —— 拉不起来就如实报错，不假装重启成功；
3. **假探针**：`build_state(metrics_probe=…)`（与总览台同一手法），不碰 `nvidia-smi`
   与真磁盘；
4. **状态靠跑出来，不靠 UPDATE 出来**：人工池那条走真状态机
   （`drafting → failed` ×3），直接 `UPDATE tasks SET status='manual_pool'`
   验不出"清扫真的把它推过去了"。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.clock import now_iso
from studio.core.paths import StudioPaths
from studio.db.migrate import migrate
from studio.db.repositories import AuditRepo
from studio.domain.enums import TaskStatus
from studio.domain.task_service import TaskService
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

WATCHDOG_URL = "/api/v1/watchdog"
TICK_URL = "/api/v1/watchdog/tick"
OVERVIEW_URL = "/api/v1/overview"

#: `config/pools.yaml` 的出厂值（改 YAML 要同步改这里）
YAML_TICK_SEC = 5.0
YAML_MANUAL_POOL_AFTER = 3
YAML_GUARDED = ("api", "tts", "draft", "voice", "render")


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


class _Probe:
    """假资源探针（磁盘充裕、显存 1/8 ⇒ 门禁与显存回升都不动手）。"""

    def __call__(self, **kwargs: Any) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at=now_iso(),
            cpu_pct=12.5,
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
def home(tmp_path: Path) -> Path:
    """临时家目录：抄真 `config/pools.yaml` 与 `config/app.yaml`。"""
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    for name in ("pools", "app"):
        shutil.copyfile(REPO_ROOT / "config" / f"{name}.yaml", root / "config" / f"{name}.yaml")
    return root


def _build(paths: StudioPaths) -> AppState:
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    return build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=_Probe(),
    )


@pytest.fixture
def state(home: Path, tmp_path: Path) -> Iterator[AppState]:
    built = _build(StudioPaths(home=home, data_dir=tmp_path / "data"))
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


@pytest.fixture
def bare_state(tmp_path: Path) -> Iterator[AppState]:
    """家目录**在**，但 `config/pools.yaml` 不在 ⇒ 守护整体停用（不是 API 起不来）。"""
    home = tmp_path / "bare"
    home.mkdir()
    built = _build(StudioPaths(home=home, data_dir=tmp_path / "bare-data"))
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def bare_connection(bare_state: AppState) -> sqlite3.Connection:
    return bare_state.connections.get()


@pytest.fixture
def bare_client(bare_state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=bare_state)) as test_client:
        yield test_client


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════


def _park_task(connection: sqlite3.Connection) -> str:
    """造一条 `attempt_count == 3` 的失败任务（走真状态机），返回 task_id。"""
    service = TaskService(connection)
    task_id = service.create(title="反复失败的任务", actor="system").id
    service.transition(task_id, TaskStatus.DRAFTING, actor="system")
    for round_no in range(YAML_MANUAL_POOL_AFTER):
        service.transition(
            task_id,
            TaskStatus.FAILED,
            actor="system",
            error_code="LLM_TIMEOUT",
            error_message=f"模型超时（第 {round_no + 1} 轮）",
        )
        if round_no < YAML_MANUAL_POOL_AFTER - 1:
            service.transition(task_id, TaskStatus.DRAFTING, actor="system")
    return task_id


def _task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT status, attempt_count, retry_from, error_code FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row is not None
    return cast(sqlite3.Row, row)


# ══════════════════════════════════════════════════════════════════════
# ① 读 · 守护状态
# ══════════════════════════════════════════════════════════════════════


def test_a_fresh_guard_reports_its_limits(client: TestClient) -> None:
    """没跑过任何一轮：报**配置值**（阈值与拍子），不虚报"守护中"。"""
    body = client.get(WATCHDOG_URL).json()

    assert body["enabled"] is True
    assert body["runnable"] is False  # 临时家目录没有 workers/ ⇒ 周期泵不起跳
    assert body["tick_sec"] == YAML_TICK_SEC
    assert body["manual_pool_after"] == YAML_MANUAL_POOL_AFTER
    assert body["last_tick_at"] is None
    assert body["services"] == []
    assert body["manual_pool"] == []
    assert body["detail"] is None


def test_the_guard_list_says_who_it_cannot_save(client: TestClient) -> None:
    """`api` 那条必须标 `guarded=false` + 指出真正的守护者（守不了自己就别画绿灯）。"""
    assert client.post(TICK_URL, json={}).status_code == 200

    body = client.get(WATCHDOG_URL).json()
    guards = {item["name"]: item for item in body["services"]}

    assert tuple(guards) == YAML_GUARDED
    assert guards["api"]["guarded"] is False
    assert "start_all" in guards["api"]["detail"]
    assert guards["voice"]["guarded"] is True
    assert guards["voice"]["running"] is False  # 没拉起过，也确实没活着
    assert "未就绪" in guards["voice"]["detail"]  # 裁定 103：未就绪不硬拉
    assert body["restarted_total"] == 0  # 一次都没去拉 ⇒ 自愈次数就是 0，不虚报
    assert body["last_tick_at"] is not None


def test_the_manual_pool_is_visible(client: TestClient, connection: sqlite3.Connection) -> None:
    """`attempt_count ≥ 3` ⇒ 被清扫进人工池，且**在面板上看得见、看得到为什么**。"""
    task_id = _park_task(connection)
    assert client.post(TICK_URL, json={}).status_code == 200

    row = _task_row(connection, task_id)
    assert row["status"] == "manual_pool"
    assert row["error_code"] == "LLM_TIMEOUT"  # 停在这里等人 ⇒ 错误码留着

    rows = client.get(WATCHDOG_URL).json()["manual_pool"]
    assert [item["task_id"] for item in rows] == [task_id]
    assert rows[0]["attempt_count"] == YAML_MANUAL_POOL_AFTER
    assert rows[0]["retry_from"] == "drafting"  # 断点留着 ⇒ 人知道该从哪继续
    assert rows[0]["error_code"] == "LLM_TIMEOUT"
    assert rows[0]["updated_at"]


def test_the_overview_carries_the_same_section(client: TestClient) -> None:
    """总览台与独立面板**同一来源**（两处口径分叉是这类面板最常见的坏味道）。"""
    assert client.post(TICK_URL, json={}).status_code == 200

    panel = client.get(WATCHDOG_URL).json()
    overview = client.get(OVERVIEW_URL).json()["watchdog"]

    assert overview["enabled"] is True
    assert overview["services"] == panel["services"]
    assert overview["restarted_total"] == panel["restarted_total"]
    assert overview["disk_gate"] == panel["disk_gate"]


# ══════════════════════════════════════════════════════════════════════
# ② 写 · 人工触发一轮
# ══════════════════════════════════════════════════════════════════════


def test_a_manual_tick_leaves_a_trace(client: TestClient, connection: sqlite3.Connection) -> None:
    """按钮的动作必须留痕（谁、什么时候、动了什么）。"""
    response = client.post(TICK_URL, json={"reason": "  手工验证守护  "})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["audit_id"] is not None
    assert body["note"].startswith("守护自检完成：")

    row = AuditRepo(connection).list_recent(limit=1)[0]
    assert row.action == "watchdog.tick"
    assert row.actor == "user"
    assert row.source == "webui"
    assert row.reason == "手工验证守护"  # 去空白（`clean_reason`）
    assert row.after["restarted"] == []  # 家目录没有 workers/ ⇒ 一个都拉不起来
    assert row.after["disk_gate"]["applied"] == []  # 水位充裕 ⇒ 门禁没动手
    assert row.after["sweep"]["manual_pool"] == []  # 没有到阈值的失败任务


def test_the_body_rejects_unknown_fields(client: TestClient) -> None:
    """`extra=forbid`：把 `reason` 写成 `reasom` 被静默忽略，留痕里就少一句话。"""
    assert client.post(TICK_URL, json={"reasom": "写错字段名"}).status_code == 422


def test_a_disabled_guard_still_answers_and_still_leaves_a_trace(
    bare_client: TestClient, bare_connection: sqlite3.Connection
) -> None:
    """`config/pools.yaml` 读不到 ⇒ 守护停用，但**照跑照留痕**（回 200，不是 400）。"""
    body = bare_client.get(WATCHDOG_URL).json()

    assert body["enabled"] is False
    assert body["runnable"] is False
    assert body["manual_pool_after"] == 0
    assert body["services"] == []
    assert body["detail"] is not None  # "读不到 ⇒ 停用"要说出来，不能只是静默

    response = bare_client.post(TICK_URL, json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["services"] == []
    assert payload["audit_id"] is not None
    assert "停用" in payload["note"]

    row = AuditRepo(bare_connection).list_recent(limit=1)[0]
    assert row.action == "watchdog.tick"
    assert row.actor == "user"
    assert row.after["restarted"] == []
