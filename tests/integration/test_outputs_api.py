"""合成配置 REST 面集成测试（T4.7 验收 · §04.2.8 / §04.5.9）。

验收六条（todolist T4.7）
------------------------
① 一期表单编辑：合成 profile（分辨率 / 帧率 / CRF / 720P 保底档）；
② 水印（位置 / 边距 / 宽度 / 透明度）；
③ 字幕（字号 / 描边 / 每行字数）；
④ 保存前强制 `validate`（不通过**拒绝保存**）；
⑤ 改动写 `audit_ops` 且 `version +1`；
⑥ 并发编辑 ⇒ `source_sha256` 比对。

五条测试纪律
------------
1. **临时家目录 + 真 `outputs.yaml` 抄一份**：本用例**要写盘**，拿仓库根当 home 会把真配置
   改掉。抄进来的那一份就是生产那一份（含全部注释）。
2. **假探针**：`build_state(metrics_probe=...)`（与人物库 / 四池同一手法）—— `lifespan` 会起
   采样泵，否则 `nvidia-smi` 与真磁盘会进来。
3. **断言也看盘、也看库**：REST 返回 200 只说明"没抛"。文件真写没写、`audit_ops` 真留没留痕，
   一律回盘 / 回库查 —— 「校验不过一个字节都不写」这句承诺，只有比对**改动前后的字节**才算验过。
4. **WS 用真连接**：面板靠 `logs` 通道里 `source === "outputs"` 的那条帧自己刷新
   （§04.4.3 的事件表是契约，本轮不为一次配置保存新增事件）。
5. **手改文件也算一条路径**：人直接编辑 YAML 不经 REST 面 —— 下一次 GET 必须看到新值，
   而拿旧指纹提交必须被拦下（这正是把并发判据放在**文件**上的理由）。
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
from studio.db.migrate import migrate
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

OUTPUTS_URL = "/api/v1/outputs"
WS_URL = "/ws/ui"

DOUYIN = "douyin_1080x1920_30fps_v1"
FALLBACK = "fallback_720x1280_v1"

#: 水印 PNG 的**相对**路径（相对 STUDIO_HOME，与 `config/outputs.yaml` 的注释一致）
WATERMARK_REL = "templates/douyin_9x16_default/assets/images/watermark.png"

#: 一份**语法就是坏的** YAML（`default_profile: [` 让解析器直接炸）
BROKEN_YAML = 'schema_version: "1.0"\ndefault_profile: [\n'


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
    """临时家目录：真 `outputs.yaml` 抄一份 + 放一张（占位）水印 PNG。

    水印 PNG 也造出来，是因为面板要回答的正是「这张图在不在」——**在**与**不在**两条路径
    都得验（D5：缺失 ⇒ 渲染拒绝出片）。
    """
    root = tmp_path / "home"
    (root / "config").mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "config" / "outputs.yaml", root / "config" / "outputs.yaml")
    watermark = root / WATERMARK_REL
    watermark.parent.mkdir(parents=True)
    watermark.write_bytes(b"\x89PNG\r\n")
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


def _file(paths: StudioPaths) -> Path:
    return paths.config_dir / "outputs.yaml"


def _read(paths: StudioPaths) -> str:
    return _file(paths).read_text(encoding="utf-8")


def _bytes(paths: StudioPaths) -> bytes:
    return _file(paths).read_bytes()


def _write(paths: StudioPaths, text: str) -> None:
    _file(paths).write_text(text, encoding="utf-8", newline="\n")


def _audit(connection: sqlite3.Connection, action: str = "outputs.update") -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM audit_ops WHERE action = ? ORDER BY id", (action,)).fetchall()


def _outputs_logs(state: AppState) -> list[Any]:
    return [row for row in state.logs.recent(limit=200) if row.source == "outputs"]


def _last_log_id(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(id), 0) FROM system_logs").fetchone()
    return int(row[0])


def _changed_lines(before: list[str], after: list[str]) -> list[int]:
    return [index for index, line in enumerate(before) if after[index] != line]


# ══════════════════════════════════════════════════════════════════════
# ① 读 · 当前配置
# ══════════════════════════════════════════════════════════════════════


def test_read_returns_profiles_watermark_subtitle_and_provenance(
    client: TestClient, paths: StudioPaths
) -> None:
    """一次拿全：四档 profile + 水印 + 字幕 + 来源（版本 / sha256 / 加载时刻 / 文件）。"""
    body = client.get(OUTPUTS_URL).json()
    assert body["stale"] is False
    assert body["error"] is None
    assert body["version"] == 1
    assert len(body["sha256"]) == 64
    assert body["path"] == str(_file(paths))
    assert body["default_profile"] == DOUYIN

    names = [profile["name"] for profile in body["profiles"]]
    assert names[0] == DOUYIN
    assert len(names) == 4
    assert sum(1 for profile in body["profiles"] if profile["is_default"]) == 1

    douyin = body["profiles"][0]
    assert (douyin["width"], douyin["height"], douyin["fps"]) == (1080, 1920, 30)
    assert douyin["quality_field"] == "crf"
    assert douyin["quality"] == 21
    assert douyin["platforms"] == ["douyin", "kuaishou", "shipinhao"]

    fallback = next(profile for profile in body["profiles"] if profile["name"] == FALLBACK)
    assert (fallback["width"], fallback["height"]) == (720, 1280)
    assert fallback["quality_field"] == "cq", "NVENC 档的质量参数叫 CQ，不是 CRF"
    assert fallback["quality"] == 26


def test_read_returns_the_watermark_with_pixel_width(client: TestClient) -> None:
    watermark = client.get(OUTPUTS_URL).json()["watermark"]
    assert watermark["path"] == WATERMARK_REL
    assert watermark["position"] == "bottom_right"
    assert (watermark["margin_x"], watermark["margin_y"]) == (48, 420)
    assert watermark["width_ratio"] == 0.25
    assert watermark["width_px"] == 270, "1080 * 0.25"
    assert watermark["opacity"] == 1.0
    assert watermark["exists"] is True


def test_read_returns_the_subtitle_style(client: TestClient) -> None:
    subtitle = client.get(OUTPUTS_URL).json()["subtitle"]
    assert subtitle["enabled"] is True
    assert subtitle["font_size"] == 64
    assert subtitle["outline"] == 4
    assert subtitle["max_chars_per_line"] == 13
    # margin_bottom 与 safe_area.bottom 取 max 之后才是 ASS 的 MarginV（§04.2.6）。
    # 这一屏只下发**可编辑**的那几个字段；safe_area 的契约由
    # `tests/unit/render/test_subtitle.py` 盯着（它直接断言 ASS 里的 MarginV）。
    assert subtitle["margin_bottom"] == 420


def test_read_ships_form_limits_and_the_position_enum(client: TestClient) -> None:
    """上下限跟着响应下发（唯一真相是配置模型的字段约束 · 裁定 161）。"""
    limits = client.get(OUTPUTS_URL).json()["limits"]
    assert limits["profile"]["width"] == {
        "min": 64.0,
        "max": 7680.0,
        "exclusive_min": False,
        "exclusive_max": False,
    }
    assert limits["watermark"]["width_ratio"] == {
        "min": 0.0,
        "max": 0.25,
        "exclusive_min": True,
        "exclusive_max": False,
    }
    assert limits["watermark"]["positions"] == [
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "center",
    ]
    assert limits["subtitle"]["font_size"]["max"] == 200.0
    assert limits["subtitle_fields"] == ["font_size", "outline", "max_chars_per_line"]


def test_missing_watermark_png_is_reported_before_render(client: TestClient, paths: StudioPaths) -> None:
    """PNG 不在盘上 ⇒ 渲染会拒绝出片（D5）。面板必须**提前**说出来，不是等点了"开始"才报错。"""
    (paths.home / WATERMARK_REL).unlink()
    body = client.get(OUTPUTS_URL).json()
    assert body["watermark"]["exists"] is False
    assert body["stale"] is False, "水印不在与「配置读不出来」是两件事"


# ══════════════════════════════════════════════════════════════════════
# ② 写 · 保存表单
# ══════════════════════════════════════════════════════════════════════


def test_save_changes_one_line_and_leaves_a_trace(
    client: TestClient, paths: StudioPaths, state: AppState, connection: sqlite3.Connection
) -> None:
    """★ 保存：只动那一行 + 版本 +1 + `audit_ops` 留痕 + `system_logs` 记一行。"""
    before = _read(paths).splitlines(keepends=True)
    body = client.get(OUTPUTS_URL).json()

    response = client.post(
        OUTPUTS_URL,
        json={
            "subtitle": {"font_size": 72},
            "source_sha256": body["sha256"],
            "reason": "字号偏小",
        },
    )
    assert response.status_code == 200
    outcome = response.json()
    assert outcome["action"] == "outputs.update"
    assert outcome["changed"] is True
    assert outcome["fields"] == ["subtitle.font_size"]
    assert outcome["version"] == body["version"] + 1
    assert outcome["sha256"] != body["sha256"]
    assert outcome["path"] == str(_file(paths))
    assert "只影响后续渲染" in outcome["note"]

    after = _read(paths).splitlines(keepends=True)
    assert len(after) == len(before)
    changed = _changed_lines(before, after)
    assert len(changed) == 1
    assert after[changed[0]].strip() == "font_size: 72"
    assert "# ★ 水印是可选装饰" in _read(paths), "注释必须还在"

    rows = _audit(connection)
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "user"
    assert row["source"] == "webui"
    assert row["target_type"] == "outputs"
    assert row["target_id"] == str(_file(paths))
    assert row["reason"] == "字号偏小"
    assert row["result"] == "ok"
    # `before` 是**唯一**能查到"改之前是多少"的地方（这份配置没有备份目录）
    before_trace = json.loads(row["before_json"])
    after_trace = json.loads(row["after_json"])
    assert before_trace["values"] == {"subtitle.font_size": 64}
    assert after_trace["values"] == {"subtitle.font_size": 72}
    assert before_trace["sha256"] == body["sha256"]
    assert before_trace["version"] == body["version"]
    assert after_trace["version"] == outcome["version"]

    logs = _outputs_logs(state)
    assert len(logs) == 1
    assert logs[0].level == "info"
    assert "subtitle.font_size" in logs[0].message
    assert logs[0].payload["action"] == "outputs.update"
    assert logs[0].payload["changed"] is True

    reread = client.get(OUTPUTS_URL).json()
    assert reread["subtitle"]["font_size"] == 72
    assert reread["sha256"] == outcome["sha256"], "面板拿到的必须是最新指纹（否则下次保存必 409）"


def test_save_can_change_a_profile_and_the_default(client: TestClient, paths: StudioPaths) -> None:
    """①② 的合并面：改分辨率 / 帧率 / 质量，并把默认档换成 720P 保底档。"""
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={
            "default_profile": FALLBACK,
            "profiles": {DOUYIN: {"width": 1080, "height": 1920, "fps": 60, "quality": 18}},
            "source_sha256": body["sha256"],
        },
    )
    assert response.status_code == 200
    assert response.json()["changed"] is True
    assert response.json()["fields"] == [
        "default_profile",
        f"profiles.{DOUYIN}.width",
        f"profiles.{DOUYIN}.height",
        f"profiles.{DOUYIN}.fps",
        f"profiles.{DOUYIN}.quality",
    ]
    text = _read(paths)
    assert "default_profile: " + FALLBACK in text
    assert "fps: 60" in text
    assert "crf: 18" in text
    reread = client.get(OUTPUTS_URL).json()
    assert reread["default_profile"] == FALLBACK
    assert reread["watermark"]["width_px"] == 180, "水印像素宽跟着**默认档**的画布宽走"


def test_save_updates_the_watermark(client: TestClient, paths: StudioPaths) -> None:
    """② 水印四项：位置 / 边距 / 宽度 / 透明度。"""
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={
            "watermark": {
                "position": "top_left",
                "margin_x": 32,
                "margin_y": 64,
                "width_ratio": 0.18,
                "opacity": 0.6,
            },
            "source_sha256": body["sha256"],
        },
    )
    assert response.status_code == 200
    text = _read(paths)
    assert "position: top_left" in text
    assert "margin_x: 32" in text
    assert "margin_y: 64" in text
    assert "width_ratio: 0.18" in text
    assert "opacity: 0.6" in text
    assert "# top_left | top_right | bottom_left | bottom_right" in text, "行尾注释必须还在"


def test_save_updates_the_subtitle(client: TestClient, paths: StudioPaths) -> None:
    """③ 字幕三项：字号 / 描边 / 每行字数。"""
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={
            "subtitle": {"font_size": 48, "outline": 6, "max_chars_per_line": 12},
            "source_sha256": body["sha256"],
        },
    )
    assert response.status_code == 200
    text = _read(paths)
    assert "font_size: 48" in text
    assert "outline: 6" in text
    assert "max_chars_per_line: 12" in text
    assert "shadow: 2" in text, "没改的字段一个字都不动"


def test_saving_the_same_value_writes_nothing_and_leaves_no_trace(
    client: TestClient,
    paths: StudioPaths,
    state: AppState,
    connection: sqlite3.Connection,
) -> None:
    """`changed=false` 是**如实回答**：不写盘、版本不动、不留痕（否则审计表会被刷屏）。"""
    before = _bytes(paths)
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={"subtitle": {"font_size": 64}, "source_sha256": body["sha256"]},
    )
    assert response.status_code == 200
    outcome = response.json()
    assert outcome["changed"] is False
    assert outcome["version"] == body["version"]
    assert outcome["sha256"] == body["sha256"]
    assert "没有做任何改动" in outcome["note"]
    assert _bytes(paths) == before
    assert _audit(connection) == []
    assert _outputs_logs(state) == []


# ══════════════════════════════════════════════════════════════════════
# ④ 校验不过 ⇒ 拒绝保存
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"subtitle": {"font_size": 9999}}, "subtitle.font_size"),
        ({"watermark": {"margin_x": 49}}, "watermark.margin_x"),
        ({"watermark": {"width_ratio": 0.0}}, "watermark.width_ratio"),
        ({"profiles": {DOUYIN: {"width": 1081}}}, f"profiles.{DOUYIN}"),
        ({"profiles": {"nope_v1": {"width": 720}}}, "profiles.nope_v1"),
        ({"default_profile": "nope_v1"}, "<root>"),
    ],
)
def test_invalid_forms_are_rejected_with_422_and_write_nothing(
    client: TestClient,
    paths: StudioPaths,
    connection: sqlite3.Connection,
    changes: dict[str, Any],
    field: str,
) -> None:
    """④ 保存前强制 validate：不通过 ⇒ 422 + `field_errors`，**盘上原封未动**。"""
    before = _bytes(paths)
    body = client.get(OUTPUTS_URL).json()
    response = client.post(OUTPUTS_URL, json={**changes, "source_sha256": body["sha256"]})
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "OUTPUTS_INVALID"
    assert [item["field"] for item in payload["context"]["field_errors"]] == [field]
    assert _bytes(paths) == before
    assert _audit(connection) == []


def test_an_odd_canvas_is_rejected_with_a_readable_reason(client: TestClient, paths: StudioPaths) -> None:
    """宽高必须是偶数（yuv420p 色度对齐）—— 报错要说得出"为什么"。"""
    body = client.get(OUTPUTS_URL).json()
    response = client.post(
        OUTPUTS_URL,
        json={"profiles": {DOUYIN: {"height": 1921}}, "source_sha256": body["sha256"]},
    )
    assert response.status_code == 422
    error = response.json()["context"]["field_errors"][0]["error"]
    assert "偶数" in error


# ══════════════════════════════════════════════════════════════════════
# ⑤ 并发编辑 ⇒ source_sha256
# ══════════════════════════════════════════════════════════════════════


def test_a_stale_sha_is_rejected_with_409_and_writes_nothing(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    """⑥ 指纹对不上 ⇒ 409 `OUTPUTS_STALE`（状态问题，不是入参问题）。"""
    before = _bytes(paths)
    response = client.post(
        OUTPUTS_URL,
        json={"subtitle": {"font_size": 72}, "source_sha256": "0" * 64},
    )
    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "OUTPUTS_STALE"
    assert payload["context"]["expected_sha256"] == "0" * 64
    assert payload["context"]["actual_sha256"] != "0" * 64
    assert _bytes(paths) == before
    assert _audit(connection) == []


def test_a_manual_edit_is_seen_and_then_blocks_a_save_with_the_old_sha(
    client: TestClient, paths: StudioPaths
) -> None:
    """人直接编辑 YAML：下一次 GET 看得到新值，而拿**旧**指纹提交必须被拦下。"""
    stale = client.get(OUTPUTS_URL).json()
    _write(paths, _read(paths).replace("  margin_y: 420", "  margin_y: 196"))

    fresh = client.get(OUTPUTS_URL).json()
    assert fresh["watermark"]["margin_y"] == 196
    assert fresh["sha256"] != stale["sha256"]
    assert fresh["version"] > stale["version"], "热重载要能被看见（版本 +1）"

    before = _bytes(paths)
    response = client.post(
        OUTPUTS_URL,
        json={"subtitle": {"font_size": 72}, "source_sha256": stale["sha256"]},
    )
    assert response.status_code == 409
    assert _bytes(paths) == before


def test_two_tabs_saving_in_turn_do_not_clobber_each_other(client: TestClient, paths: StudioPaths) -> None:
    """两个标签页各改各的：第一个成功，第二个必须先拿新指纹再提交（顺序保存不丢改动）。"""
    first = client.get(OUTPUTS_URL).json()
    second = client.get(OUTPUTS_URL).json()
    assert first["sha256"] == second["sha256"]

    assert (
        client.post(
            OUTPUTS_URL,
            json={"subtitle": {"font_size": 72}, "source_sha256": first["sha256"]},
        ).status_code
        == 200
    )
    rejected = client.post(
        OUTPUTS_URL,
        json={"watermark": {"opacity": 0.5}, "source_sha256": second["sha256"]},
    )
    assert rejected.status_code == 409

    refreshed = client.get(OUTPUTS_URL).json()
    assert refreshed["subtitle"]["font_size"] == 72, "第一个标签页的改动还在"
    retried = client.post(
        OUTPUTS_URL,
        json={"watermark": {"opacity": 0.5}, "source_sha256": refreshed["sha256"]},
    )
    assert retried.status_code == 200
    assert client.get(OUTPUTS_URL).json()["watermark"]["opacity"] == 0.5


# ══════════════════════════════════════════════════════════════════════
# ⑥ 实时推送（`logs` 通道 · 不新增 WS 事件）
# ══════════════════════════════════════════════════════════════════════


def test_save_pushes_a_log_frame_for_the_console(client: TestClient, connection: sqlite3.Connection) -> None:
    """面板靠这条帧自己刷新 —— 别人（CLI / 另一个标签页）改了配置也走同一条路。"""
    body = client.get(OUTPUTS_URL).json()
    last_id = _last_log_id(connection)
    with client.websocket_connect(f"{WS_URL}?channels=logs&since_id={last_id}") as session:
        client.post(
            OUTPUTS_URL,
            json={"subtitle": {"font_size": 72}, "source_sha256": body["sha256"]},
        )
        frame = json.loads(session.receive_text())
    assert frame["type"] == "event"
    assert frame["channel"] == "logs"
    assert frame["data"]["kind"] == "log.appended"
    assert frame["data"]["source"] == "outputs"
    assert frame["data"]["level"] == "info"
    assert "subtitle.font_size" in frame["data"]["message"]


# ══════════════════════════════════════════════════════════════════════
# ⑦ 配置坏了：面板仍要能把它修回来
# ══════════════════════════════════════════════════════════════════════


def test_a_broken_file_still_returns_200_with_limits(client: TestClient, paths: StudioPaths) -> None:
    """读取失败**也返回 200**：这个面板存在的意义就是"配置坏了的时候把它修回来"。"""
    _write(paths, BROKEN_YAML)
    response = client.get(OUTPUTS_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is True
    assert body["profiles"] == []
    assert body["watermark"] is None
    assert body["subtitle"] is None
    assert body["error"]
    assert body["limits"]["profile"]["width"]["max"] == 7680.0, "上下限来自模型，照常下发"


def test_saving_into_a_broken_file_is_rejected_and_writes_nothing(
    client: TestClient, paths: StudioPaths, connection: sqlite3.Connection
) -> None:
    _write(paths, BROKEN_YAML)
    before = _bytes(paths)
    response = client.post(OUTPUTS_URL, json={"subtitle": {"font_size": 72}})
    assert response.status_code == 422
    assert response.json()["code"] == "OUTPUTS_INVALID"
    assert _bytes(paths) == before
    assert _audit(connection) == []


def test_a_missing_file_is_reported_as_not_found(client: TestClient, paths: StudioPaths) -> None:
    _file(paths).unlink()
    response = client.get(OUTPUTS_URL)
    assert response.status_code == 200
    assert response.json()["stale"] is True
    assert "不存在" in response.json()["error"]
