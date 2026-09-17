"""交付包与 R2 留档的 REST 面（T5.5 · §06.11 / §06.12）。

三个端点，三件事
----------------
① ``GET  /api/v1/publish/handoff/{task_id}`` —— **预览**：包里会有什么、缺哪件。
   最要紧的一条是「**一个字节都不写**」：预览会写盘的话，人只是点开看一眼，
   盘上就多一个目录，而那个目录谁都不认领；
② ``POST /api/v1/publish/handoff/{task_id}`` —— 打包出去，并写 ``audit_ops``。
   它**不看** ``handoff.enabled``（出厂 ``false``）：那个开关管的是「发布时自动顺手
   带一份」，人按下的这一下就是意图本身 —— 拦它只会得到「按钮是坏的」；
③ ``GET  /api/v1/publish/compliance`` —— R2 来源登记快照，**只读**。

为什么留痕这一条必须验
----------------------
交付包是**离开我们掌控**的东西。没有 ``audit_ops`` 那一行，「这份片子什么时候被谁
导出去过」就答不上来 —— 而那是 A2（外部对接未定义）之下唯一能追的东西。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, build_state
from studio.app.main import create_app
from studio.core.paths import StudioPaths
from studio.db.migrate import migrate
from studio.db.repositories import AuditRepo
from studio.db.repositories.asset_repo import BrollClipRepo
from studio.domain.task_service import TaskService
from studio.publish.handoff import DELIVERY_KINDS
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

HANDOFF_URL = "/api/v1/publish/handoff/{task_id}"
COMPLIANCE_URL = "/api/v1/publish/compliance"


class _Probe:
    """假资源探针 —— 不碰真 ``nvidia-smi`` 与真磁盘（与发布池那条同一手法）。"""

    def __call__(self, **kwargs: object) -> ResourceSnapshot:
        del kwargs
        return ResourceSnapshot(
            sampled_at="2026-09-17T06:00:00.000Z",
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
    """临时家目录 + 真配置抄一份（改配置不该让这里的断言跟着飘）。"""
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


@pytest.fixture
def task_id(connection: sqlite3.Connection) -> str:
    return TaskService(connection).create(title="一条测试任务", actor="test").id


def _tree(root: Path) -> set[str]:
    """目录下的文件清单（目录不存在 ⇒ 空集）—— 用来断言「一个字节都没写」。"""
    if not root.is_dir():
        return set()
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _video(paths: StudioPaths, task_id: str, stamp: str, payload: str) -> Path:
    target = paths.videos_dir / f"{stamp}_{task_id}_final.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    return target


def test_preview_lists_the_package_and_writes_nothing(
    client: TestClient, paths: StudioPaths, task_id: str
) -> None:
    """预览：六件都列出来、缺件点名、``auto_on_publish`` 如实 —— 而且**不落盘**。"""
    _video(paths, task_id, "20260917-120000", "片子")
    output_dir = paths.home / "data" / "handoff"
    before = _tree(output_dir)

    response = client.get(HANDOFF_URL.format(task_id=task_id))

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == task_id
    assert body["ready"] is True
    assert body["adapter"] == "local"
    assert [item["kind"] for item in body["items"]] == [kind for kind, _, _ in DELIVERY_KINDS]
    assert body["missing"] == ["cover", "subtitle", "script", "timeline", "manifest"]
    # 出厂 `app.yaml → handoff.enabled: false`：预览要如实说「发布时不会自动带一份」。
    assert body["auto_on_publish"] is False
    assert _tree(output_dir) == before, "预览写了盘 —— 点开看一眼不该留下任何东西"


def test_preview_of_an_empty_task_says_what_is_missing(client: TestClient, task_id: str) -> None:
    """没成片 ⇒ ``ready=false`` + 一句补救说明（不是 404：任务号认不出来时也答得上）。"""
    response = client.get(HANDOFF_URL.format(task_id=task_id))

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert "video" in body["missing"]
    assert body["note"]
    assert body["items"][0]["note"], "缺件那一行要写清「怎么补」"


def test_push_writes_a_selfcontained_package_and_an_audit_row(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, task_id: str
) -> None:
    """打包：真落盘 + 真留痕（``publish.handoff``）。"""
    _video(paths, task_id, "20260917-120000", "片子")

    response = client.post(
        HANDOFF_URL.format(task_id=task_id),
        json={"actor": "user", "actor_ref": "ops", "reason": "交给审片台"},
    )

    assert response.status_code == 200
    body = response.json()
    root = Path(body["root"])
    assert root.is_dir(), "打包要真的落盘"
    assert (root / "handoff.json").is_file()
    assert body["adapter"] == "local"
    assert body["bytes"] > 0
    assert body["missing"] == ["cover", "subtitle", "script", "timeline", "manifest"]

    audits = [row.action for row in AuditRepo(connection).list_for_task(task_id)]
    assert "publish.handoff" in audits, "交付包是离开我们掌控的东西 —— 必须留痕"


def test_push_does_not_look_at_the_enabled_switch(
    client: TestClient, paths: StudioPaths, task_id: str
) -> None:
    """``handoff.enabled=false``（出厂值）**不拦**手动导出。

    那个开关管的是「发布时自动顺手带一份」。拦手动这一下，得到的是「按钮是坏的」，
    而用户没有任何办法让它变好 —— 与 T5.3 裁定 269 同一条：投递期不看开关。
    """
    _video(paths, task_id, "20260917-120000", "片子")

    response = client.post(HANDOFF_URL.format(task_id=task_id), json={"actor": "user"})

    assert response.status_code == 200
    assert Path(response.json()["root"]).is_dir()


def test_push_requires_a_reason_field_shape(client: TestClient, paths: StudioPaths, task_id: str) -> None:
    """请求体省略也照打（导出这件事本身不需要理由字段）—— 但写了就要收下。"""
    _video(paths, task_id, "20260917-120000", "片子")

    assert client.post(HANDOFF_URL.format(task_id=task_id)).status_code == 200


def test_compliance_reports_gaps_and_is_read_only(
    client: TestClient, connection: sqlite3.Connection, task_id: str
) -> None:
    """合规快照：缺一件就报一条，且**不改任何东西**。"""
    repo = BrollClipRepo(connection)
    repo.upsert(
        clip_id="clip_no_source",
        path="data/broll/clip_no_source.mp4",
        sha256="a" * 64,
        duration_ms=10_000,
        license="authorized",
    )

    first = client.get(COMPLIANCE_URL)
    second = client.get(COMPLIANCE_URL)

    assert first.status_code == 200
    body = first.json()
    assert body["ok"] is False
    assert len(body["gaps"]) == 1
    assert "clip_no_source" in body["gaps"][0]
    assert "声音权" in body["notice"], "R2 提示是常驻的，不是可选的"
    assert second.json() == body, "合规快照是只读的：连查两次结果必须逐字相同"


def test_compliance_is_clean_on_an_empty_library(client: TestClient) -> None:
    """空库 ⇒ 没有缺口（「没素材」与「有素材没登记」是两件事）。"""
    body = client.get(COMPLIANCE_URL).json()

    assert body["ok"] is True
    assert body["gaps"] == []


def test_handoff_task_id_shape_is_enforced(client: TestClient) -> None:
    """任务号要进 URL 路径 ⇒ 形状不合规时是 422，而不是一个查不到的 200。"""
    assert client.get(HANDOFF_URL.format(task_id="带空格 的任务号")).status_code == 422
