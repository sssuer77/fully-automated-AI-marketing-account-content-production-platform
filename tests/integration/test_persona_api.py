"""人物库 REST 面集成测试（T4.13 验收 · §02.4 / §04.1.6 / §04.5.8）。

验收六条（todolist T4.13）
--------------------------
① 展示当前激活人物（来源 / 版本 / `sha256` / 加载时刻 / `last_error`）；
② 人物库列表（`id` / 名称 / 口吻 / 受众 / 有效性 + 无效原因）；
③ 表单编辑 ⇒ **保存前强制 validate，不通过拒绝保存**；
④ 一键切换（旧版自动备份）+ 另存为；
⑤ 切换 / 编辑 ⇒ 广播 `system.persona_changed`（**无需重启任何进程**）；
⑥ 回滚（备份是一等公民，回滚本身也能再回滚）。

五条测试纪律
------------
1. **临时家目录 + 真 persona 抄一份**：本用例**要写盘**，拿仓库根当 home 会把真
   `config/persona.yaml` 改掉。抄进来的两份（激活 + 人物库）就是生产那两份。
2. **假探针**：`build_state(metrics_probe=…)`（与总览台 / 四池同一手法）——
   `lifespan` 会起采样泵，否则 `nvidia-smi` 与真磁盘会进来。
3. **断言也看盘、也看库**：REST 返回 200 只说明"没抛"。文件真写没写、备份真备没备、
   `audit_ops` 真留没留痕，一律回盘 / 回库查 —— 「校验不过一个字节都不写」这句
   承诺，只有比对**改动前后的字节**才算验过。
4. **WS 用真连接**：`client.websocket_connect`（TestClient 走完整 ASGI + lifespan），
   不然"事件发没发出去"永远只是代码里的一句话。
5. **手改文件也算一条路径**：人直接编辑 YAML / CLI `studio persona use` 都不经过
   REST 面 —— 事件必须照样发得出去（这正是把广播挂在 store 订阅上的理由）。
"""

from __future__ import annotations

import json
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
from studio.core.paths import StudioPaths
from studio.core.persona_store import PersonaChange
from studio.db.migrate import migrate
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

PERSONA_URL = "/api/v1/persona"
ACTIVATE_URL = "/api/v1/persona/activate"
SAVE_AS_URL = "/api/v1/persona/save-as"
ROLLBACK_URL = "/api/v1/persona/rollback"
WS_URL = "/ws/ui"

#: 人物库里的备选人物（`config/personas/` 那一份）
LIBRARY_ID = "solo_commentary"

#: 一份**语法就是坏的** YAML（`name: [` 让解析器直接炸）
BROKEN_YAML = "id: persona_default\nname: [\n"


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
    """临时家目录：激活人物与人物库都从仓库真那份抄一份（本用例会写它们）。"""
    root = tmp_path / "home"
    (root / "config" / "personas").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "persona.yaml", root / "config" / "persona.yaml")
    shutil.copyfile(
        REPO_ROOT / "config" / "personas" / f"{LIBRARY_ID}.yaml",
        root / "config" / "personas" / f"{LIBRARY_ID}.yaml",
    )
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


def _backup_dir(paths: StudioPaths) -> Path:
    return paths.backups_dir / "persona"


def _backups(paths: StudioPaths) -> list[str]:
    directory = _backup_dir(paths)
    if not directory.is_dir():
        return []
    return sorted(item.name for item in directory.glob("*.yaml"))


def _audit(connection: sqlite3.Connection, action: str) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM audit_ops WHERE action = ? ORDER BY id", (action,)).fetchall()


def _persona_logs(state: AppState) -> list[Any]:
    return [row for row in state.logs.recent(limit=200) if row.source == "persona"]


def _active(client: TestClient) -> dict[str, Any]:
    body = client.get(PERSONA_URL).json()
    active = body["active"]
    assert isinstance(active, dict)
    return active


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


# ══════════════════════════════════════════════════════════════════════
# ① 读 · 激活人物
# ══════════════════════════════════════════════════════════════════════


def test_read_returns_active_persona_with_provenance(client: TestClient, paths: StudioPaths) -> None:
    """来源 / 版本 / sha256 / 加载时刻 —— "这个人物是哪来的"必须答得出来。"""
    body = client.get(PERSONA_URL).json()
    assert body["active_error"] is None
    active = body["active"]
    assert active["persona_id"] == "persona_default"
    assert active["name"] == "熊大熊二·MC跑酷"
    assert active["source"] == "active"
    assert active["version"] == 1
    assert active["stale"] is False
    assert active["last_error"] is None
    assert len(active["sha256"]) == 64
    assert active["loaded_at"].endswith("Z")
    assert active["path"] == str(paths.persona_file)
    assert active["config"]["id"] == "persona_default"
    assert active["config"]["target_chars_min"] < active["config"]["target_chars_max"]


def test_read_ships_form_limits_from_the_model(client: TestClient) -> None:
    """上下限跟着响应下发（唯一真相是 `PersonaConfig` 的字段约束）。"""
    limits = client.get(PERSONA_URL).json()["limits"]
    assert limits["target_chars_max"] == {"min": 100, "max": 8000}
    assert limits["max_duration_ms"] == {"min": 10000, "max": 1800000}
    assert limits["catchphrases"] == {"min": 2, "max": 20}
    assert "tone" in limits["editable"]
    assert "id" not in limits["editable"], "id 不能改：它是文件名 + 备份名 + 审计 target_id"


def test_read_reports_stale_when_active_file_is_broken(client: TestClient, paths: StudioPaths) -> None:
    """文件被改坏 ⇒ 仍用上一份好的，但必须**说出来**（`stale` + `last_error`）。"""
    first = _active(client)
    _write(paths.persona_file, BROKEN_YAML)
    body = client.get(PERSONA_URL).json()
    assert body["active_error"] is None, "有可用快照 ⇒ 不算「连上一份都没有」"
    assert body["active"]["stale"] is True
    assert body["active"]["last_error"]
    assert body["active"]["persona_id"] == first["persona_id"]
    assert body["active"]["config"]["tone"] == first["config"]["tone"]


def test_read_reports_active_error_when_nothing_is_loadable(client: TestClient, paths: StudioPaths) -> None:
    """首次加载就失败 ⇒ 200 + `active_error`（**不是** 500：这个面板就是用来修它的）。"""
    paths.persona_file.unlink()
    response = client.get(PERSONA_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["active"] is None
    assert body["active_error"]
    assert body["library"], "激活人物没了，人物库照样要能列出来（还得靠它切回去）"


# ══════════════════════════════════════════════════════════════════════
# ② 读 · 人物库
# ══════════════════════════════════════════════════════════════════════


def test_read_lists_library_with_active_marker(client: TestClient, home: Path) -> None:
    body = client.get(PERSONA_URL).json()
    items = {item["persona_id"]: item for item in body["library"]}
    assert set(items) == {LIBRARY_ID}
    entry = items[LIBRARY_ID]
    assert entry["valid"] is True
    assert entry["active"] is False
    assert entry["name"] == "快嘴单人解说"
    assert entry["tone"]
    assert entry["audience"]
    assert entry["path"] == str(home / "config" / "personas" / f"{LIBRARY_ID}.yaml")
    assert body["library_dir"] == str(home / "config" / "personas")


def test_read_marks_the_active_persona_in_the_library(client: TestClient, home: Path) -> None:
    """把当前人物另存进库 ⇒ 库里那一条要标出来（"就是他"）。"""
    client.post(SAVE_AS_URL, json={"persona_id": "persona_default"})
    items = {item["persona_id"]: item for item in client.get(PERSONA_URL).json()["library"]}
    assert items["persona_default"]["active"] is True


def test_read_flags_invalid_library_entry_with_reason(client: TestClient, home: Path) -> None:
    """无效条目**直接显示原因**（而不是从列表里悄悄消失）。"""
    _write(home / "config" / "personas" / "broken.yaml", "id: broken\nname: [\n")
    items = {item["persona_id"]: item for item in client.get(PERSONA_URL).json()["library"]}
    assert items["broken"]["valid"] is False
    assert items["broken"]["error"]
    assert items["broken"]["name"] == ""


def test_read_lists_backups_with_validity(client: TestClient, paths: StudioPaths) -> None:
    client.post(PERSONA_URL, json={"tone": "京腔、快嘴"})
    backups = client.get(PERSONA_URL).json()["backups"]
    assert len(backups) == 1
    assert backups[0]["valid"] is True
    assert backups[0]["persona_id"] == "persona_default"
    assert backups[0]["size"] > 0
    assert backups[0]["name"].endswith("_persona_default.yaml")
    assert backups[0]["created_at"].endswith("Z")


# ══════════════════════════════════════════════════════════════════════
# ③ 改 · 保存前强制 validate
# ══════════════════════════════════════════════════════════════════════


def test_update_rewrites_file_and_backs_up_previous(client: TestClient, paths: StudioPaths) -> None:
    before = _read(paths.persona_file)
    body = client.post(PERSONA_URL, json={"tone": "京腔、快嘴、不油腻"}).json()
    assert body["action"] == "persona.update"
    assert body["changed"] is True
    assert body["version"] == 2
    assert body["backup"]
    after = _read(paths.persona_file)
    assert after != before
    assert "京腔、快嘴、不油腻" in after
    assert _read(_backup_dir(paths) / body["backup"]) == before, "备份里躺的必须是**改之前**那一份"


def test_update_keeps_fields_that_were_not_sent(client: TestClient) -> None:
    """只发 `tone` ⇒ 其余字段原样（两个标签页同时开着不该互相覆盖）。"""
    before = _active(client)["config"]
    client.post(PERSONA_URL, json={"tone": "京腔"})
    after = _active(client)["config"]
    assert after["tone"] == "京腔"
    for field in ("name", "role_desc", "audience", "catchphrases", "forbidden", "max_duration_ms"):
        assert after[field] == before[field], field


def test_update_rejects_invalid_form_and_writes_nothing(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    """★ 验收核心：校验不过 ⇒ **一个字节都不写**（文件、备份、留痕三处都得是原样）。"""
    before = _read(paths.persona_file)
    response = client.post(PERSONA_URL, json={"name": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "PERSONA_INVALID"
    assert body["context"]["field_errors"][0]["field"] == "name"
    assert "没有写入任何内容" in body["remediation"]
    assert _read(paths.persona_file) == before
    assert _backups(paths) == []
    assert _audit(connection, "persona.update") == []


def test_update_rejects_range_violation(client: TestClient, paths: StudioPaths) -> None:
    """区间不自洽（min ≥ max）也走同一条拒绝路径。"""
    before = _read(paths.persona_file)
    response = client.post(PERSONA_URL, json={"target_chars_min": 900, "target_chars_max": 800})
    assert response.status_code == 422
    assert response.json()["code"] == "PERSONA_INVALID"
    assert _read(paths.persona_file) == before


def test_update_rejects_unknown_key(client: TestClient) -> None:
    """`catchphrase` 写成单数被静默忽略 ⇒ 人会以为"口癖加上了"。禁掉。"""
    response = client.post(PERSONA_URL, json={"catchphrase": ["x"]})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


def test_update_records_audit_with_before_and_after(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    client.post(PERSONA_URL, json={"tone": "京腔", "reason": "换口吻试试"})
    rows = _audit(connection, "persona.update")
    assert len(rows) == 1
    row = rows[0]
    assert row["target_type"] == "persona"
    assert row["target_id"] == "persona_default"
    assert row["reason"] == "换口吻试试"
    assert row["actor"] == "user"
    assert row["source"] == "webui"
    assert row["result"] == "ok"
    before = json.loads(row["before_json"])
    after = json.loads(row["after_json"])
    assert before["sha256"] != after["sha256"]
    assert (before["version"], after["version"]) == (1, 2)


def test_update_writes_a_log_line_that_does_not_declare_the_event(
    client: TestClient, state: AppState
) -> None:
    """日志行是**给人看的流水**；事件由 store 的订阅发出。

    如果这行 payload 里带 ``kind``，Hub 的 tail 会再广播一遍 —— 一条改动两条事件。
    """
    client.post(PERSONA_URL, json={"tone": "京腔"})
    rows = _persona_logs(state)
    assert len(rows) == 1
    assert rows[0].level == "info"
    assert "只影响" in rows[0].message, "影响范围必须写在人看得见的地方"
    assert "kind" not in rows[0].payload


# ══════════════════════════════════════════════════════════════════════
# ④ 切 · 一键切换 / 另存为
# ══════════════════════════════════════════════════════════════════════


def test_activate_switches_to_the_library_entry(client: TestClient, paths: StudioPaths, home: Path) -> None:
    library_file = home / "config" / "personas" / f"{LIBRARY_ID}.yaml"
    body = client.post(ACTIVATE_URL, json={"persona_id": LIBRARY_ID}).json()
    assert body["action"] == "persona.activate"
    assert body["changed"] is True
    assert body["persona_id"] == LIBRARY_ID
    assert body["name"] == "快嘴单人解说"
    assert body["backup"]
    assert _read(paths.persona_file) == _read(library_file)
    assert _active(client)["persona_id"] == LIBRARY_ID


def test_activate_is_idempotent_for_the_same_persona(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """已经是他了 ⇒ 不写盘、不备份、不留痕（重复点按钮不该把审计刷成一串）。"""
    body = client.post(ACTIVATE_URL, json={"persona_id": "persona_default"}).json()
    assert body["changed"] is False
    assert body["backup"] is None
    assert "没有做任何改动" in body["note"]
    assert _audit(connection, "persona.activate") == []
    assert _backups(paths) == []


def test_activate_unknown_id_is_404(client: TestClient) -> None:
    """★ 库里没有这个人 ⇒ 404 + 可选清单（不能是 500：那只是 id 打错了）。"""
    response = client.post(ACTIVATE_URL, json={"persona_id": "ghost"})
    assert response.status_code == 404
    assert response.json()["code"] == "PERSONA_NOT_FOUND"


def test_activate_invalid_entry_is_422_and_keeps_active(
    client: TestClient, paths: StudioPaths, home: Path
) -> None:
    _write(home / "config" / "personas" / "broken.yaml", "id: broken\nname: [\n")
    before = _read(paths.persona_file)
    response = client.post(ACTIVATE_URL, json={"persona_id": "broken"})
    assert response.status_code == 422
    assert response.json()["code"] == "PERSONA_INVALID"
    assert _read(paths.persona_file) == before


def test_activate_rejects_bad_id_shape(client: TestClient) -> None:
    """id 的形状在**请求体**就拦住（与 store 共用同一条正则）。"""
    response = client.post(ACTIVATE_URL, json={"persona_id": "UPPER CASE"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_FAILED"


def test_save_as_copies_active_into_the_library(client: TestClient, paths: StudioPaths, home: Path) -> None:
    body = client.post(SAVE_AS_URL, json={"persona_id": "baseline_v2"}).json()
    target = home / "config" / "personas" / "baseline_v2.yaml"
    assert target.is_file()
    assert _read(target) == _read(paths.persona_file)
    assert body["action"] == "persona.save_as"
    assert body["changed"] is False, "另存为**不动**激活人物"
    assert body["backup"] is None
    assert "当前激活人物未变" in body["note"]


def test_save_as_audit_points_at_the_new_entry(client: TestClient, connection: sqlite3.Connection) -> None:
    """留痕指向新存的那一份（否则"谁新建了 X"会被记成"改了 persona_default"）。"""
    client.post(SAVE_AS_URL, json={"persona_id": "baseline_v2"})
    rows = _audit(connection, "persona.save_as")
    assert len(rows) == 1
    assert rows[0]["target_id"] == "baseline_v2"


def test_save_as_existing_without_overwrite_is_409(client: TestClient) -> None:
    client.post(SAVE_AS_URL, json={"persona_id": "snap"})
    response = client.post(SAVE_AS_URL, json={"persona_id": "snap"})
    assert response.status_code == 409
    assert response.json()["code"] == "PERSONA_EXISTS"


def test_save_as_overwrite_replaces_the_entry(client: TestClient, home: Path) -> None:
    client.post(SAVE_AS_URL, json={"persona_id": "snap"})
    target = home / "config" / "personas" / "snap.yaml"
    _write(target, "stale: true\n")
    body = client.post(SAVE_AS_URL, json={"persona_id": "snap", "overwrite": True}).json()
    assert body["changed"] is False
    assert "stale" not in _read(target)


# ══════════════════════════════════════════════════════════════════════
# ⑤ 回滚
# ══════════════════════════════════════════════════════════════════════


def test_rollback_restores_the_backup(client: TestClient, paths: StudioPaths) -> None:
    original = _read(paths.persona_file)
    updated = client.post(PERSONA_URL, json={"tone": "京腔"}).json()
    body = client.post(ROLLBACK_URL, json={"name": updated["backup"]}).json()
    assert body["action"] == "persona.rollback"
    assert body["changed"] is True
    assert _read(paths.persona_file) == original
    assert body["backup"] != updated["backup"], "回滚前也要备份 ⇒ 回滚本身还能再回滚"
    assert len(client.get(PERSONA_URL).json()["backups"]) == 2


def test_rollback_unknown_backup_is_404(client: TestClient) -> None:
    response = client.post(ROLLBACK_URL, json={"name": "20200101-000000_nobody.yaml"})
    assert response.status_code == 404
    assert response.json()["code"] == "PERSONA_NOT_FOUND"
    assert "最近的备份" in response.json()["remediation"]


@pytest.mark.parametrize("name", ["../../config/persona.yaml", "..\\persona.yaml", "/etc/passwd"])
def test_rollback_rejects_anything_outside_the_backup_dir(client: TestClient, name: str) -> None:
    """白名单正则：`..` / 斜杠一个都进不来（回滚不可能读到备份目录之外）。"""
    response = client.post(ROLLBACK_URL, json={"name": name})
    assert response.status_code == 404
    assert response.json()["code"] == "PERSONA_NOT_FOUND"


def test_rollback_refuses_a_broken_backup(client: TestClient, paths: StudioPaths) -> None:
    """坏备份照样列出来，但**不许**拿它覆盖现在这份好的。"""
    client.post(PERSONA_URL, json={"tone": "京腔"})
    broken = _backup_dir(paths) / "20200101-000000_persona_default.yaml"
    _write(broken, BROKEN_YAML)
    before = _read(paths.persona_file)
    response = client.post(ROLLBACK_URL, json={"name": broken.name})
    assert response.status_code == 422
    assert response.json()["code"] == "PERSONA_INVALID"
    assert _read(paths.persona_file) == before
    listed = {item["name"]: item for item in client.get(PERSONA_URL).json()["backups"]}
    assert listed[broken.name]["valid"] is False
    assert listed[broken.name]["error"]


# ══════════════════════════════════════════════════════════════════════
# ⑥ 广播（`system.persona_changed`）
# ══════════════════════════════════════════════════════════════════════


def test_update_broadcasts_persona_changed(client: TestClient) -> None:
    """★ 验收核心：改完**不用重启任何进程**，前端立刻知道。"""
    _active(client)
    with client.websocket_connect(f"{WS_URL}?channels=system") as session:
        client.post(PERSONA_URL, json={"tone": "京腔"})
        frame = json.loads(session.receive_text())
    assert frame["type"] == "event"
    assert frame["channel"] == "system"
    assert frame["data"]["kind"] == "system.persona_changed"
    assert frame["data"]["persona_id"] == "persona_default"
    assert frame["data"]["version"] == 2
    assert frame["data"]["changed"] is True
    assert frame["data"]["failed"] is False
    assert frame["data"]["error"] is None
    assert frame["data"]["previous_version"] == 1
    assert len(frame["data"]["sha256"]) == 64


def test_activate_broadcasts_with_the_new_id(client: TestClient) -> None:
    _active(client)
    with client.websocket_connect(f"{WS_URL}?channels=system") as session:
        client.post(ACTIVATE_URL, json={"persona_id": LIBRARY_ID})
        frame = json.loads(session.receive_text())
    assert frame["data"]["kind"] == "system.persona_changed"
    assert frame["data"]["persona_id"] == LIBRARY_ID
    assert frame["data"]["previous_persona_id"] == "persona_default"
    assert frame["data"]["reason"] == f"activate:{LIBRARY_ID}"


def test_hand_edit_broadcasts_too(client: TestClient, paths: StudioPaths) -> None:
    """人直接在编辑器里改 YAML（或 CLI 切换）**也要发事件**。

    这正是把广播挂在 store 订阅上、而不是"写入口顺手声明一下"的理由：那两条路径
    根本不经过我们的写入口。
    """
    _active(client)
    with client.websocket_connect(f"{WS_URL}?channels=system") as session:
        _write(
            paths.persona_file, _read(paths.persona_file).replace("name: 熊大熊二·MC跑酷", "name: 手改的名字")
        )
        client.get(PERSONA_URL)  # 下一次 `current()` 才发现
        frame = json.loads(session.receive_text())
    assert frame["data"]["kind"] == "system.persona_changed"
    assert frame["data"]["name"] == "手改的名字"
    assert frame["data"]["reason"] == "hot_reload"


def test_broken_file_broadcasts_once(client: TestClient, paths: StudioPaths, state: AppState) -> None:
    """文件坏了 ⇒ 广播 `failed`；但**同一份坏文件只报一次**（不是每次读都报）。"""
    _active(client)
    events: list[PersonaChange] = []
    state.persona.subscribe(events.append)
    try:
        _write(paths.persona_file, BROKEN_YAML)
        client.get(PERSONA_URL)
        assert [item.failed for item in events] == [True]
        assert events[0].error
        assert events[0].new.persona_id == "persona_default"
        assert events[0].version == 1, "`new` 是**仍在生效**的上一份"
        client.get(PERSONA_URL)
        client.get(PERSONA_URL)
        assert [item.failed for item in events] == [True], "同一份坏文件不该反复广播"
    finally:
        state.persona.unsubscribe(events.append)


def test_listener_is_unsubscribed_when_the_app_stops(state: AppState) -> None:
    """反复起停（测试里每个用例一套 app）不该把监听器叠成 N 份。"""
    # 直接看订阅表：这条用例的全部意义就是"关停后监听器真的被摘掉了"
    before = len(state.persona._listeners)
    with TestClient(create_app(state=state)):
        assert len(state.persona._listeners) == before + 1
    assert len(state.persona._listeners) == before
