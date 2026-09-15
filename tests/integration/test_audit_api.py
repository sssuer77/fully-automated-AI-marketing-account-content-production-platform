"""审计页 REST 面集成测试（T4.12 验收 · §04.5.11 / §03.3.8）。

验收口径来自 todolist T4.12：「审计页可按任务 / 操作人筛选」。

为什么这几条必须测
------------------
① **筛选是真筛选**：`actor=user` 不能把 `system` 的行也带出来 —— 审计页上多出来
   一行"不是你干的"，等于把"谁干的"这个唯一要回答的问题弄脏了；
② **分页有头**：`total` 必须是**同一组筛选下**的总数，不是全表总数，否则
   "还有 3 页"会变成一个永远翻不完的假象；
③ **倒序稳定**：`at` 只到毫秒，同一毫秒的两条要靠 `id` 定序，否则翻页会出现
   "同一条出现两次、另一条永远看不到"；
④ **只读**：审计是别人写、这里读。开一个写入口，等于给"伪造留痕"开了条路。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.paths import StudioPaths
from studio.db import migrate
from studio.db.repositories.audit_repo import AuditRepo
from studio.ws.hub import HubSettings

AUDIT_URL = "/api/v1/audit"
FACETS_URL = "/api/v1/audit/facets"


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
def client(state: AppState) -> Iterator[TestClient]:
    with TestClient(create_app(state=state)) as test_client:
        yield test_client


@pytest.fixture
def seeded(state: AppState) -> AppState:
    """四条留痕：2 个操作人 × 2 个任务 × 2 种结果。"""
    repo = AuditRepo(state.connections.get())
    repo.record(
        actor="user",
        actor_ref="ops",
        action="task.approve",
        target_type="task",
        target_id="t1",
        task_id="t1",
        before={"status": "awaiting_approval"},
        after={"status": "queued_voice"},
        source="webui",
    )
    repo.record(
        actor="auto",
        action="task.approve",
        target_type="task",
        target_id="t1",
        task_id="t1",
        result="denied",
        reason="grade_b 不在放行范围",
        source="auto",
    )
    repo.record(actor="user", action="pool.pause", target_type="pool", target_id="render", source="webui")
    repo.record(
        actor="worker",
        action="job.dead",
        target_type="task",
        target_id="t2",
        task_id="t2",
        result="error",
        source="worker",
    )
    return state


def _ids(payload: dict[str, Any]) -> list[int]:
    return [item["id"] for item in payload["items"]]


# ══════════════════════════════════════════════════════════════════════
# ① 读全
# ══════════════════════════════════════════════════════════════════════


def test_empty_page_is_not_an_error(client: TestClient) -> None:
    payload = client.get(AUDIT_URL).json()

    assert payload["total"] == 0
    assert payload["items"] == []


def test_newest_first_and_total(client: TestClient, seeded: AppState) -> None:
    del seeded
    payload = client.get(AUDIT_URL).json()

    assert payload["total"] == 4
    assert len(payload["items"]) == 4
    assert _ids(payload) == sorted(_ids(payload), reverse=True)


def test_before_and_after_are_carried(client: TestClient, seeded: AppState) -> None:
    """留痕的全部意义就是这两块：只给一句"改过了"等于没留。"""
    del seeded
    payload = client.get(AUDIT_URL, params={"action": "task.approve"}).json()
    item = next(row for row in payload["items"] if row["actor"] == "user")

    assert item["before"] == {"status": "awaiting_approval"}
    assert item["after"] == {"status": "queued_voice"}
    assert item["actor_ref"] == "ops"


# ══════════════════════════════════════════════════════════════════════
# ② 筛选
# ══════════════════════════════════════════════════════════════════════


def test_filter_by_task(client: TestClient, seeded: AppState) -> None:
    del seeded
    payload = client.get(AUDIT_URL, params={"task_id": "t1"}).json()

    assert payload["total"] == 2
    assert {row["task_id"] for row in payload["items"]} == {"t1"}


def test_filter_by_actor_does_not_leak(client: TestClient, seeded: AppState) -> None:
    """★ 按操作人筛出来的每一行都必须是那个操作人干的。"""
    del seeded
    payload = client.get(AUDIT_URL, params={"actor": "user"}).json()

    assert payload["total"] == 2
    assert {row["actor"] for row in payload["items"]} == {"user"}


def test_filters_combine(client: TestClient, seeded: AppState) -> None:
    del seeded
    payload = client.get(AUDIT_URL, params={"actor": "auto", "action": "task.approve"}).json()

    assert payload["total"] == 1
    assert payload["items"][0]["result"] == "denied"
    assert payload["items"][0]["reason"] == "grade_b 不在放行范围"


def test_filter_by_result_and_target(client: TestClient, seeded: AppState) -> None:
    del seeded

    assert client.get(AUDIT_URL, params={"result": "error"}).json()["total"] == 1
    assert client.get(AUDIT_URL, params={"target_type": "pool"}).json()["total"] == 1


def test_since_cuts_off_the_old_rows(client: TestClient, seeded: AppState) -> None:
    del seeded

    assert client.get(AUDIT_URL, params={"since": "2999-01-01T00:00:00.000Z"}).json()["total"] == 0


# ══════════════════════════════════════════════════════════════════════
# ③ 分页
# ══════════════════════════════════════════════════════════════════════


def test_total_ignores_paging(client: TestClient, seeded: AppState) -> None:
    """★ `total` 是同一组筛选下的总数，不随 limit/offset 变。"""
    del seeded
    payload = client.get(AUDIT_URL, params={"limit": 2, "offset": 0}).json()

    assert payload["total"] == 4
    assert len(payload["items"]) == 2

    second = client.get(AUDIT_URL, params={"limit": 2, "offset": 2}).json()
    assert second["total"] == 4
    assert set(_ids(payload)).isdisjoint(_ids(second))


def test_out_of_range_limit_is_rejected(client: TestClient) -> None:
    assert client.get(AUDIT_URL, params={"limit": 0}).status_code == 422
    assert client.get(AUDIT_URL, params={"limit": 501}).status_code == 422


# ══════════════════════════════════════════════════════════════════════
# ④ 分面 / 只读
# ══════════════════════════════════════════════════════════════════════


def test_facets_feed_the_dropdowns(client: TestClient, seeded: AppState) -> None:
    del seeded
    payload = client.get(FACETS_URL).json()

    assert payload["actors"] == ["auto", "user", "worker"]
    assert payload["actions"] == ["job.dead", "pool.pause", "task.approve"]
    assert payload["target_types"] == ["pool", "task"]
    assert payload["results"] == ["denied", "error", "ok"]


def test_audit_is_read_only(client: TestClient) -> None:
    """★ 审计是别人写、这里读：开一个写入口等于给"伪造留痕"开了条路。"""
    assert client.post(AUDIT_URL, json={}).status_code == 405
    assert client.delete(AUDIT_URL).status_code == 405
