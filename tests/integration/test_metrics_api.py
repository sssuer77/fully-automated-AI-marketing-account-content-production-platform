"""观测面板 REST 面集成测试（T4.12 验收 · §04.5.11）。

验收口径来自 todolist T4.12：「指标面板」。

这一屏与总览台的分工
--------------------
总览台回答"现在怎么样"，观测面板回答**"出事了还有得救吗"**。所以这里最要紧的
两条不是数字好不好看，而是：

① **备份新鲜度**：目录里一直有文件、只是都是旧的 —— 面板必须报"最新一份距今
   几小时"与 `stale`，而不是"有没有备份"；
② **存储体检**：`DELETE` 删了行不等于文件变小 —— 面板必须报 `db_freelist_bytes`，
   否则"我明明清了垃圾"会变成一句没人能证伪的话。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.paths import StudioPaths
from studio.db import migrate
from studio.db.backup import backup_database
from studio.ws.hub import HubSettings

METRICS_URL = "/api/v1/metrics"


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


# ══════════════════════════════════════════════════════════════════════
# 一次拿全
# ══════════════════════════════════════════════════════════════════════


def test_every_block_is_present(client: TestClient) -> None:
    payload = client.get(METRICS_URL).json()

    for key in ("generated_at", "pools", "today", "services", "backups", "storage"):
        assert key in payload, key
    assert [card["pool"] for card in payload["pools"]] == ["draft", "voice", "render", "publish"]
    assert payload["worker_total"] == 0


def test_pool_counters_are_numbers(client: TestClient) -> None:
    card = client.get(METRICS_URL).json()["pools"][0]

    assert card["pending"] == 0
    assert card["running"] == 0
    assert card["dead"] == 0
    assert card["paused"] is False


def test_metrics_is_read_only(client: TestClient) -> None:
    """GC / 备份 / VACUUM 都是 CLI 或计划任务的事，不该在这一屏留按钮。"""
    assert client.post(METRICS_URL, json={}).status_code == 405


# ══════════════════════════════════════════════════════════════════════
# ★ 备份新鲜度
# ══════════════════════════════════════════════════════════════════════


def test_no_backup_is_stale_not_zero(client: TestClient) -> None:
    """一份都没有 ⇒ `stale=true`（不是"age=0 看起来挺新鲜"）。"""
    payload = client.get(METRICS_URL).json()["backups"]

    assert payload["count"] == 0
    assert payload["newest"] is None
    assert payload["age_hours"] is None
    assert payload["stale"] is True


def test_a_fresh_backup_clears_the_flag(client: TestClient, state: AppState) -> None:
    result = backup_database(state.paths.db_file, dest_dir=state.paths.backups_dir)

    payload = client.get(METRICS_URL).json()["backups"]

    assert payload["count"] == 1
    assert payload["newest"] == result.path.stem.removeprefix("studio_")
    assert payload["age_hours"] is not None and payload["age_hours"] < 1.0
    assert payload["stale"] is False
    assert payload["total_bytes"] == result.size_bytes


# ══════════════════════════════════════════════════════════════════════
# ★ 存储体检
# ══════════════════════════════════════════════════════════════════════


def test_storage_reports_the_cache_and_the_archive(client: TestClient, state: AppState) -> None:
    (state.paths.tts_cache_dir / "a.wav").write_bytes(b"x" * 32)
    (state.paths.hot_archive_dir / "202609").mkdir(parents=True, exist_ok=True)
    (state.paths.hot_archive_dir / "202609" / "a.md").write_bytes(b"y" * 8)

    payload = client.get(METRICS_URL).json()["storage"]

    assert payload["tts_cache_bytes"] == 32
    assert payload["hot_archive_bytes"] == 8
    assert payload["tts_cache_limit_bytes"] == 5 * 1024**3
    assert payload["db_bytes"] > 0


def test_freelist_is_reported_after_a_delete(client: TestClient, state: AppState) -> None:
    """★ `DELETE` 删了行不等于文件变小 —— 空洞必须被报出来。

    要真删出空洞，得**整行**删（按 `ts` 过滤只删掉时间戳对上的那几行，页根本没还回去），
    而且量得够大（几十字节的行全挤在一两页里，删完一页都不空）。
    """
    connection = state.connections.get()
    before = client.get(METRICS_URL).json()["storage"]

    connection.execute("BEGIN IMMEDIATE")
    connection.executemany(
        "INSERT INTO system_logs(level, source, message) VALUES ('info','test',?)",
        [(f"填充 {index} " + "x" * 4000,) for index in range(400)],
    )
    connection.execute("COMMIT")

    filled = client.get(METRICS_URL).json()["storage"]
    connection.execute("DELETE FROM system_logs")
    freed = client.get(METRICS_URL).json()["storage"]

    assert filled["db_bytes"] > before["db_bytes"]  # 那 400 页真的写进去了
    assert freed["db_freelist_bytes"] > 0  # 删完留下的空洞被报出来
    assert freed["db_bytes"] >= before["db_bytes"]  # 而文件并没有跟着行一起消失


def test_storage_ignores_a_future_timestamp(client: TestClient) -> None:
    """`generated_at` 是可解析的 ISO（前端要按它算"这一屏多旧"）。"""
    payload = client.get(METRICS_URL).json()
    moment = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))

    assert moment.tzinfo is not None
    assert abs(datetime.now(UTC) - moment) < timedelta(minutes=5)
