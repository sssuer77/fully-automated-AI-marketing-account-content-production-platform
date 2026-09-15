"""四池调度控制台 REST 面集成测试（T4.10 验收 · §04.4.5 / §03.4.4）。

验收五条（todolist T4.10）
--------------------------
① 池状态 / 积压 / 优先级（**配置值与运行值并排**，缺一个就会出「我明明把 YAML
   改成 3 了，面板还是 1」这种查不完的悬案）；
② 并发旋钮：写 `pool_settings` + `audit_ops`，**生效无需重启**；
③ 暂停 / 恢复（在途跑完，不丢进度）—— 写入口仍是 T4.2 那个（裁定 144）；
④ 死信重投（`dead → pending` + `audit_ops`）；
⑤ 自动降级：连续 `TTS_OOM` 达阈值 ⇒ 自动降并发 + `POOL_AUTODEGRADED` 告警。

四条测试纪律
------------
1. **临时家目录**：`config_dir` 由 `home` 决定。本用例不写盘，但 `PoolService`
   每个请求都要读 `config/pools.yaml`，所以夹具把真那份**抄进 tmp** —— 拿仓库根
   当 home，`auto_concurrency.oom_threshold` 一改，这里的断言就跟着飘。
2. **假探针**：`build_state(metrics_probe=…)`（与总览台同一手法）—— 否则
   `MetricsPump` 会去碰 `nvidia-smi`。
3. **状态靠跑出来，不靠 UPDATE 出来**：死信走 `enqueue → claim → fail`，
   直接 `UPDATE jobs SET status='dead'` 会绕过 §03.4.3 的告警路径，于是
   「重投一条真死信」这件事从来没被这条用例验过。
4. **断言也看库**：REST 返回 200 只说明"没抛"，`pool_settings` 真写没写、
   `audit_ops` 真留没留痕，一律回库查。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.clock import now_iso
from studio.core.config import AutoConcurrencyConfig, load_pools_config
from studio.core.paths import StudioPaths
from studio.db import JobStore
from studio.db.migrate import migrate
from studio.db.repositories import AuditRepo
from studio.pools.runner import POOL_NAMES
from studio.services.log_service import SystemLog
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

POOLS_URL = "/api/v1/pools"
CONCURRENCY_URL = "/api/v1/pools/concurrency"
REQUEUE_URL = "/api/v1/pools/requeue"

#: `config/pools.yaml` 的出厂值（断言"配置值与运行值并排"时用；改 YAML 要同步改这里）
YAML_CONCURRENCY = {"draft": 2, "voice": 1, "render": 1, "publish": 1}
YAML_PRIORITY = {"draft": 100, "voice": 200, "render": 300, "publish": 400}
YAML_UNIT_TYPE = {"draft": "task", "voice": "sentence", "render": "final", "publish": "publish"}

#: `pool_settings` 的 seed 值（0006_seed.sql）——与 YAML 恰好同值，但**是两份真相**
SEED_CONCURRENCY = {"draft": 2, "voice": 1, "render": 1, "publish": 1}


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


class _Probe:
    """假资源探针（默认磁盘充裕、GPU 在）。"""

    def __call__(self, **kwargs: Any) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at=now_iso(),
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
def home(tmp_path: Path) -> Path:
    """临时家目录：把真 `config/pools.yaml` 抄一份（面板每请求都要读它）。"""
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "pools.yaml", root / "config" / "pools.yaml")
    return root


@pytest.fixture
def paths(home: Path, tmp_path: Path) -> StudioPaths:
    return StudioPaths(home=home, data_dir=tmp_path / "data")


@pytest.fixture
def state(paths: StudioPaths) -> Iterator[AppState]:
    paths.ensure_runtime_dirs()
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


# ══════════════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════════════


def _store(connection: sqlite3.Connection, *, threshold: int = 2) -> JobStore:
    """库内直连的 `JobStore`（阈值显式给，避免用例跟着 YAML 飘）。"""
    return JobStore(connection, auto_concurrency=AutoConcurrencyConfig(oom_threshold=threshold))


def _add_task(connection: sqlite3.Connection, task_id: str = "t1") -> None:
    connection.execute("INSERT INTO tasks(id, title) VALUES (?, ?)", (task_id, "测试任务"))


def _dead_job(
    connection: sqlite3.Connection,
    *,
    unit_ref: str = "s1",
    pool: str = "voice",
    unit_type: str = "sentence",
    code: str = "TTS_OOM",
    task_id: str = "t1",
    worker_id: str = "voice#1@1",
) -> str:
    """造一条**真**死信（enqueue → claim → fail(不可重试)），并返回 job_id。"""
    store = _store(connection)
    job_id = store.enqueue(task_id=task_id, pool=pool, unit_type=unit_type, unit_ref=unit_ref, max_attempts=1)
    assert job_id is not None
    claimed = store.claim(pool=pool, worker_id=worker_id)
    assert claimed is not None and claimed.id == job_id
    store.fail(
        job_id=job_id,
        worker_id=worker_id,
        error_code=code,
        error_message="显存不足",
        retryable=False,
    )
    return job_id


def _card(body: dict[str, Any], pool: str) -> dict[str, Any]:
    return next(item for item in body["pools"] if item["pool"] == pool)


def _alerts(state: AppState) -> list[SystemLog]:
    return [row for row in state.logs.recent(limit=200) if row.is_alert]


def _degraded(state: AppState) -> list[SystemLog]:
    """只看 `POOL_AUTODEGRADED`（死信本身也写 `JOB_DEAD`，别混进来）。"""
    return [row for row in _alerts(state) if row.alert_code == "POOL_AUTODEGRADED"]


def _actions(connection: sqlite3.Connection) -> list[str]:
    return [op.action for op in AuditRepo(connection).list_recent(limit=20)]


def _concurrency(connection: sqlite3.Connection, pool: str) -> int:
    row = connection.execute("SELECT concurrency FROM pool_settings WHERE pool = ?", (pool,)).fetchone()
    return int(row["concurrency"])


# ══════════════════════════════════════════════════════════════════════
# ① 读 · 四池卡片
# ══════════════════════════════════════════════════════════════════════


def test_pools_lists_four_pools_in_canonical_order(client: TestClient) -> None:
    body = client.get(POOLS_URL).json()
    assert [card["pool"] for card in body["pools"]] == list(POOL_NAMES)
    assert body["config_error"] is None
    assert body["config_path"].endswith("pools.yaml")
    for card in body["pools"]:
        assert card["error"] is None
        assert card["paused"] is False
        assert card["dead_letters"] == []


def test_card_shows_config_and_runtime_side_by_side(client: TestClient) -> None:
    """配置（YAML）与运行值（DB）**两个都摆出来** —— 双真相的唯一体面解法。"""
    body = client.get(POOLS_URL).json()
    for name in POOL_NAMES:
        card = _card(body, name)
        assert card["unit_type"] == YAML_UNIT_TYPE[name]
        assert card["priority"] == YAML_PRIORITY[name]
        assert card["config_concurrency"] == YAML_CONCURRENCY[name]
        assert card["concurrency"] == SEED_CONCURRENCY[name]
        assert card["concurrency_min"] == 1


def test_voice_pool_ceiling_is_three(client: TestClient) -> None:
    """§01.4.4：voice ≤ 3（8 GB 显存的工程上限），其余池 ≤ 8。"""
    body = client.get(POOLS_URL).json()
    assert _card(body, "voice")["concurrency_max"] == 3
    for name in ("draft", "render", "publish"):
        assert _card(body, name)["concurrency_max"] == 8
    assert body["oom_threshold"] == 2
    assert body["auto_degrade_enabled"] is True


def test_card_reports_backlog_and_oldest_pending_age(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    _add_task(connection)
    store = _store(connection)
    store.enqueue(task_id="t1", pool="draft", unit_type="task", unit_ref="u1")
    store.enqueue(task_id="t1", pool="draft", unit_type="task", unit_ref="u2", depends_on=["nonexistent"])
    card = _card(client.get(POOLS_URL).json(), "draft")
    assert card["pending"] == 1
    assert card["blocked"] == 1
    assert card["backlog"] == 2
    assert card["oldest_pending_age_sec"] is not None
    assert card["oldest_pending_age_sec"] >= 0


def test_card_reports_running_and_ceiling_flag(client: TestClient, connection: sqlite3.Connection) -> None:
    _add_task(connection)
    store = _store(connection)
    store.enqueue(task_id="t1", pool="voice", unit_type="sentence", unit_ref="s1")
    assert store.claim(pool="voice", worker_id="voice#1@1") is not None
    card = _card(client.get(POOLS_URL).json(), "voice")
    assert card["running"] == 1
    assert card["claimed"] == 1
    assert card["at_concurrency_ceiling"] is False  # 1 < 3（voice 的工程上限）
    # 顶到上限时置位：上限是硬编码的那条线，面板据此提示"再往上没有意义"
    client.post(CONCURRENCY_URL, json={"pool": "voice", "concurrency": 3})
    assert _card(client.get(POOLS_URL).json(), "voice")["at_concurrency_ceiling"] is True


def test_config_read_failure_is_reported_not_raised(client: TestClient, paths: StudioPaths) -> None:
    """`pools.yaml` 读不到 ⇒ 200 + `config_error`，**不是** 500。

    面板的作用是"告诉我现在什么情况"。配置坏了的时候，用户最需要看到的恰恰是
    「配置读不到，去修这一份」—— 一个什么都不说的 500 把这条信息也吞掉了。
    """
    (paths.config_dir / "pools.yaml").unlink()
    response = client.get(POOLS_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["config_error"] is not None
    assert "pools.yaml" in body["config_error"]
    for card in body["pools"]:
        assert card["error"] is not None
        assert card["unit_type"] is None
        assert card["priority"] is None
        assert card["config_concurrency"] is None
    # 运行值来自 DB，配置读不到也照给（这两块本来就不该互相拖累）
    assert _card(body, "voice")["concurrency"] == 1


def test_paused_pool_is_flagged_in_console(client: TestClient, connection: sqlite3.Connection) -> None:
    """暂停写入口在总览台（T4.2）；四池控制台是它的**第二个视图**（裁定 144）。"""
    connection.execute(
        "UPDATE pool_settings SET paused = 1, paused_at = ?, paused_by = 'user' WHERE pool = 'render'",
        (now_iso(),),
    )
    card = _card(client.get(POOLS_URL).json(), "render")
    assert card["paused"] is True
    assert card["paused_by"] == "user"


# ══════════════════════════════════════════════════════════════════════
# ② 写 · 并发旋钮
# ══════════════════════════════════════════════════════════════════════


def test_set_concurrency_writes_db_and_audit(client: TestClient, connection: sqlite3.Connection) -> None:
    body = client.post(CONCURRENCY_URL, json={"pool": "draft", "concurrency": 4}).json()
    assert {key: value for key, value in body.items() if key != "note"} == {
        "pool": "draft",
        "concurrency": 4,
        "previous": 2,
        "changed": True,
        "running": 0,
        "pending": 0,
        "paused": False,
    }
    assert "4" in body["note"]  # 文案里的数就是新值（别把 previous 抄进去）
    assert _concurrency(connection, "draft") == 4
    ops = AuditRepo(connection).list_recent(limit=5)
    assert [op.action for op in ops] == ["pool.set_concurrency"]
    assert ops[0].actor == "user"
    assert ops[0].source == "webui"
    assert ops[0].target_id == "draft"


def test_set_concurrency_takes_effect_without_restart(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """旋钮改完，**下一个请求**就该看到新值（不需要重启 API / worker）。

    顺带钉住「配置值不跟着动」：`config_concurrency` 永远是 YAML 那份，
    它要是跟着运行值一起变，"我改的 YAML 到底生效没有"就再也看不出来了。
    """
    client.post(CONCURRENCY_URL, json={"pool": "draft", "concurrency": 3})
    card = _card(client.get(POOLS_URL).json(), "draft")
    assert card["concurrency"] == 3
    assert card["config_concurrency"] == 2
    # 而且 worker 的认领守卫真的认这个数：并发 3 ⇒ 能连认 3 个
    _add_task(connection)
    store = _store(connection)
    for index in range(3):
        store.enqueue(task_id="t1", pool="draft", unit_type="task", unit_ref=f"u{index}")
    assert sum(store.claim(pool="draft", worker_id="draft#1@1") is not None for _ in range(4)) == 3


def test_set_concurrency_is_idempotent(client: TestClient, connection: sqlite3.Connection) -> None:
    client.post(CONCURRENCY_URL, json={"pool": "draft", "concurrency": 3})
    second = client.post(CONCURRENCY_URL, json={"pool": "draft", "concurrency": 3}).json()
    assert second["changed"] is False
    assert second["previous"] == 3
    assert "本来就是" in second["note"]
    assert _actions(connection) == ["pool.set_concurrency"]  # 没多留一条痕


def test_lowering_concurrency_does_not_kill_inflight(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """§03.4.4 不变量 1：新值只影响**认领**，在途的跑完再收敛。"""
    _add_task(connection)
    store = _store(connection)
    for index in range(2):
        store.enqueue(task_id="t1", pool="draft", unit_type="task", unit_ref=f"u{index}")
    assert store.claim(pool="draft", worker_id="draft#1@1") is not None
    assert store.claim(pool="draft", worker_id="draft#2@2") is not None

    body = client.post(CONCURRENCY_URL, json={"pool": "draft", "concurrency": 1}).json()
    assert body["changed"] is True
    assert body["running"] == 2
    assert "在途 2 个会跑完" in body["note"]
    # 在途两个没被掐掉
    assert connection.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'claimed'").fetchone()["n"] == 2
    # 但新一轮认领已经收敛：running(2) >= concurrency(1) ⇒ 不认领
    assert store.claim(pool="draft", worker_id="draft#3@3") is None


def test_voice_concurrency_above_engineering_ceiling_is_rejected(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """voice 4 ⇒ 422 `POOL_CONCURRENCY_LIMIT`（带 remediation 的业务错误）。"""
    response = client.post(CONCURRENCY_URL, json={"pool": "voice", "concurrency": 4})
    assert response.status_code == 422
    error = response.json()
    assert error["code"] == "POOL_CONCURRENCY_LIMIT"
    assert error["context"]["max"] == 3
    assert "显存" in error["remediation"]
    assert _concurrency(connection, "voice") == 1  # 没写进去


@pytest.mark.parametrize(
    ("pool", "value"),
    [("voice", 0), ("draft", 0), ("draft", 9), ("voice", 9)],
)
def test_concurrency_out_of_field_range_is_rejected_by_schema(
    client: TestClient, connection: sqlite3.Connection, pool: str, value: int
) -> None:
    """0 与 9 连**字段级**校验都过不去（裁定 145：0 是"沉默的暂停"，WebUI 拒绝它）。"""
    response = client.post(CONCURRENCY_URL, json={"pool": pool, "concurrency": value})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"
    assert _concurrency(connection, pool) == SEED_CONCURRENCY[pool]


def test_concurrency_rejects_unknown_pool_and_extra_field(client: TestClient) -> None:
    bad_pool = client.post(CONCURRENCY_URL, json={"pool": "tts", "concurrency": 1})
    assert bad_pool.status_code == 422
    assert bad_pool.json()["code"] == "VALIDATION_FAILED"
    # 把 concurrency 拼错被静默忽略 ⇒ 人以为"调上去了"，池还按旧值认领（代价是 OOM）
    extra = client.post(CONCURRENCY_URL, json={"pool": "draft", "concurrency": 2, "force": True})
    assert extra.status_code == 422
    assert extra.json()["code"] == "VALIDATION_FAILED"


# ══════════════════════════════════════════════════════════════════════
# ③ 写 · 死信重投
# ══════════════════════════════════════════════════════════════════════


def test_dead_letters_are_listed_in_pool_card(client: TestClient, connection: sqlite3.Connection) -> None:
    _add_task(connection)
    job_id = _dead_job(connection, unit_ref="s1", code="TTS_OOM")
    card = _card(client.get(POOLS_URL).json(), "voice")
    assert card["dead"] == 1
    letter = card["dead_letters"][0]
    assert letter["job_id"] == job_id
    assert letter["task_id"] == "t1"
    assert letter["unit_ref"] == "s1"
    assert letter["error_code"] == "TTS_OOM"
    assert letter["attempts"] == 1
    assert letter["max_attempts"] == 1


def test_requeue_dead_moves_job_back_to_pending(client: TestClient, connection: sqlite3.Connection) -> None:
    _add_task(connection)
    job_id = _dead_job(connection, unit_ref="s1")
    body = client.post(REQUEUE_URL, json={"job_ids": [job_id], "reason": "换小模型重试"}).json()
    assert body["requested"] == 1
    assert body["requeued"] == [job_id]
    assert body["failed"] == []
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["error_code"] is None
    assert row["lease_owner"] is None
    assert _card(client.get(POOLS_URL).json(), "voice")["dead"] == 0
    ops = AuditRepo(connection).list_recent(limit=5)
    assert [op.action for op in ops] == ["job.requeue"]
    assert ops[0].reason == "换小模型重试"


def test_requeue_reports_each_failure_individually(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """一条不是死信不影响其余（与批量通过同一取舍：逐条独立、部分失败不回滚）。"""
    _add_task(connection)
    dead_id = _dead_job(connection, unit_ref="s1")
    live_id = _store(connection).enqueue(task_id="t1", pool="voice", unit_type="sentence", unit_ref="s2")
    assert live_id is not None
    body = client.post(REQUEUE_URL, json={"job_ids": [dead_id, live_id]}).json()
    assert body["requested"] == 2
    assert body["requeued"] == [dead_id]
    assert [item["job_id"] for item in body["failed"]] == [live_id]
    assert body["failed"][0]["code"] == "JOB_DEAD"
    assert body["failed"][0]["remediation"] is not None
    assert (
        connection.execute("SELECT status FROM jobs WHERE id = ?", (live_id,)).fetchone()["status"]
        == "pending"
    )


def test_requeue_unknown_job_reports_not_found(client: TestClient, connection: sqlite3.Connection) -> None:
    body = client.post(REQUEUE_URL, json={"job_ids": ["job_不存在"]}).json()
    assert body["requeued"] == []
    assert body["failed"][0]["code"] == "JOB_NOT_FOUND"
    assert _actions(connection) == []


def test_requeue_empty_list_is_rejected(client: TestClient) -> None:
    response = client.post(REQUEUE_URL, json={"job_ids": []})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


def test_requeue_does_not_reset_oom_streak(client: TestClient, connection: sqlite3.Connection) -> None:
    """裁定 153：计数器量的是"显存能不能装下"，与某条作业的进度无关。"""
    _add_task(connection)
    job_id = _dead_job(connection, unit_ref="s1")
    connection.execute("UPDATE pool_settings SET consecutive_oom = 1 WHERE pool = 'voice'")
    client.post(REQUEUE_URL, json={"job_ids": [job_id]})
    row = connection.execute("SELECT consecutive_oom FROM pool_settings WHERE pool = 'voice'").fetchone()
    assert row["consecutive_oom"] == 1


# ══════════════════════════════════════════════════════════════════════
# ④ 自动降并发（§01.4.4）
# ══════════════════════════════════════════════════════════════════════


def _oom_once(store: JobStore, unit_ref: str, *, pool: str = "voice") -> None:
    """造一条「认领后 OOM 挂掉」的作业（走 `fail()` 那条真路径，不直接 UPDATE）。"""
    unit_type = "sentence" if pool == "voice" else "task"
    job_id = store.enqueue(task_id="t1", pool=pool, unit_type=unit_type, unit_ref=unit_ref, max_attempts=1)
    assert job_id is not None
    worker_id = f"{pool}#1@1"
    assert store.claim(pool=pool, worker_id=worker_id) is not None
    store.fail(
        job_id=job_id,
        worker_id=worker_id,
        error_code="TTS_OOM",
        error_message="CUDA out of memory",
        retryable=False,
    )


def _fail_with_oom(connection: sqlite3.Connection, unit_ref: str, *, pool: str = "voice") -> None:
    """`_oom_once` 的便捷版（库内默认 `JobStore`，阈值取 YAML 同值 2）。"""
    _oom_once(_store(connection), unit_ref, pool=pool)


def test_oom_streak_is_counted_per_pool(client: TestClient, connection: sqlite3.Connection) -> None:
    _add_task(connection)
    connection.execute("UPDATE pool_settings SET concurrency = 2 WHERE pool = 'voice'")
    _fail_with_oom(connection, "s1")
    assert _card(client.get(POOLS_URL).json(), "voice")["consecutive_oom"] == 1
    _fail_with_oom(connection, "s2")
    assert _card(client.get(POOLS_URL).json(), "voice")["consecutive_oom"] == 2
    assert _card(client.get(POOLS_URL).json(), "draft")["consecutive_oom"] == 0


def test_consecutive_oom_degrades_concurrency_and_alerts(
    client: TestClient, connection: sqlite3.Connection, state: AppState
) -> None:
    """连续 2 次 `TTS_OOM`（阈值 2）⇒ 并发 −1 + `POOL_AUTODEGRADED` + 留痕。"""
    _add_task(connection)
    connection.execute("UPDATE pool_settings SET concurrency = 3 WHERE pool = 'voice'")
    _fail_with_oom(connection, "s1")
    assert _concurrency(connection, "voice") == 3  # 还没到阈值
    assert _degraded(state) == []
    _fail_with_oom(connection, "s2")
    assert _concurrency(connection, "voice") == 2  # 降了
    alerts = _degraded(state)
    assert len(alerts) == 1
    assert alerts[0].level == "warn"
    assert "voice" in alerts[0].message
    card = _card(client.get(POOLS_URL).json(), "voice")
    assert card["concurrency"] == 2
    assert card["consecutive_oom"] == 2
    ops = AuditRepo(connection).list_recent(limit=5)
    degrade = [op for op in ops if op.action == "pool.autodegrade"]
    assert len(degrade) == 1
    assert degrade[0].actor == "auto"
    assert degrade[0].source == "worker"


def test_degrade_alerts_are_written_once_per_crossing(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    """裁定 147：判据是 `== threshold` —— 越过线只降一次、只告警一次。"""
    _add_task(connection)
    connection.execute("UPDATE pool_settings SET concurrency = 8 WHERE pool = 'voice'")
    for index in range(5):
        _fail_with_oom(connection, f"s{index}")
    # 阈值 2 ⇒ 只有第 2 次越线降过一次
    assert _concurrency(connection, "voice") == 7
    assert _card(client.get(POOLS_URL).json(), "voice")["consecutive_oom"] == 5


def test_success_resets_oom_streak(client: TestClient, connection: sqlite3.Connection) -> None:
    _add_task(connection)
    _fail_with_oom(connection, "s1")
    assert _card(client.get(POOLS_URL).json(), "voice")["consecutive_oom"] == 1
    store = _store(connection)
    job_id = store.enqueue(task_id="t1", pool="voice", unit_type="sentence", unit_ref="ok1")
    assert job_id is not None
    assert store.claim(pool="voice", worker_id="voice#1@1") is not None
    store.succeed(job_id=job_id, worker_id="voice#1@1", result=None)
    assert _card(client.get(POOLS_URL).json(), "voice")["consecutive_oom"] == 0


def test_degrade_at_floor_still_alerts(client: TestClient, connection: sqlite3.Connection) -> None:
    """裁定 151：降不动也要告警 —— 静默会让"一直 OOM"看起来像任务本身有问题。"""
    _add_task(connection)
    assert _concurrency(connection, "voice") == 1  # 出厂即下限
    _fail_with_oom(connection, "s1")
    _fail_with_oom(connection, "s2")
    assert _concurrency(connection, "voice") == 1
    ops = AuditRepo(connection).list_recent(limit=5)
    degrade = [op for op in ops if op.action == "pool.autodegrade"]
    assert len(degrade) == 1
    assert degrade[0].after["concurrency"] == 1


def test_auto_concurrency_disabled_in_yaml_stops_degrade(
    connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """`auto_concurrency.enabled=false` ⇒ 只计数、不降并发。

    走的是**真那条线**：`load_pools_config(paths)` 读盘 → 把这一份交给 `JobStore`
    （`pools/runner.build_worker` 就是这么透传的），所以「改 YAML 即生效」这件事
    在这里被真的验了一遍，而不是验一个写死的默认值。
    """
    yaml = paths.config_dir / "pools.yaml"
    yaml.write_text(
        yaml.read_text(encoding="utf-8").replace(
            "auto_concurrency:\n  enabled: true", "auto_concurrency:\n  enabled: false"
        ),
        encoding="utf-8",
    )
    config = load_pools_config(paths)
    assert config.auto_concurrency.enabled is False

    _add_task(connection)
    connection.execute("UPDATE pool_settings SET concurrency = 3 WHERE pool = 'voice'")
    store = JobStore(connection, auto_concurrency=config.auto_concurrency)
    _oom_once(store, "s1")
    _oom_once(store, "s2")
    assert _concurrency(connection, "voice") == 3
    assert _actions(connection) == []
