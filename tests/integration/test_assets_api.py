"""素材库 REST 面集成测试（T4.8 验收 · §3.3.14 / §4.3.1 / §04.5.12）。

验收六条（todolist T4.8）
------------------------
① 三类素材的列表 / 统计 / 缺口（够不够用）；
② 扫盘入库（含 `dry_run` 预览：**一个字节都不写库**）；
③ 预览（缩略图）与试听（原文件）；
④ 标记（启用 / 停用，**只停用不删除**）+ 授权信息可改；
⑤ 缺授权 ⇒ 拒绝入库（不替用户伪造 R2 留痕）；
⑥ 统计满足 `clips >= 60 且 >= 30min` 的判据线**跟着响应下发**。

四条测试纪律
------------
1. **临时家目录**：`StudioPaths(home=tmp, data_dir=tmp/data)`。素材是**文件 + 表**两处
   一起才算数，用例必须真在盘上放文件、真往库里写行。
2. **假工具**（`build_state(asset_tools=...)`）：扫盘要跑 ffprobe / ffmpeg 抽帧 / 响度 /
   指纹。注入假件之后，「入库时探不出来怎么办」不再以"这台机器装没装 ffmpeg"为前提。
3. **断言也看盘、也看库**：REST 返回 200 只说明"没抛"。行写没写、留痕有没有、文件动没动，
   一律回库 / 回盘查。
4. **不新增 WS 事件**：入库与启停只往 `system_logs` 写一行（`source='assets'`），
   面板靠 `logs` 通道自己刷新（§04.4.3 的事件表是契约，本轮不动它）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studio.app.deps import AppState, AssetTools, build_state
from studio.app.main import create_app
from studio.core.clock import now_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import MediaInfo, VolumeStats
from studio.core.paths import StudioPaths
from studio.db.migrate import migrate
from studio.services.metrics_service import ResourceSnapshot
from studio.ws.hub import HubSettings

ASSETS_URL = "/api/v1/assets"
INGEST_URL = "/api/v1/assets/ingest"
STATS_URL = "/api/v1/assets/stats"

BROLL = "parkour_001"
BGM = "bgm_001"
VOICE = "bear_da"


# ══════════════════════════════════════════════════════════════════════
# 假工具
# ══════════════════════════════════════════════════════════════════════


class _Probe:
    """假资源探针（默认磁盘充裕、GPU 在）—— 不碰真 `nvidia-smi` 与真磁盘。"""

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


class _Tools:
    """一套假的外部工具（探测 / 音量 / 缩略图 / 响度 / 指纹）。"""

    def __init__(self) -> None:
        self.info: dict[str, MediaInfo] = {}
        self.peak_db: float | None = -3.0
        self.loudness: float | None = -14.0
        self.thumbnail_error: bool = False
        self.digests: dict[str, str] = {}
        self.thumbnails: list[Path] = []

    def probe(self, path: Path, **_: Any) -> MediaInfo:
        key = Path(path).name
        if key not in self.info:
            raise StudioError("解不开", code=ErrorCode.MEDIA_UNDECODABLE)
        return self.info[key]

    def volume(self, path: Path, **_: Any) -> VolumeStats:
        return VolumeStats(max_db=self.peak_db, mean_db=-20.0)

    def thumbnail(self, source: Path, target: Path, **_: Any) -> Path:
        if self.thumbnail_error:
            raise StudioError("抽帧失败", code=ErrorCode.MEDIA_UNDECODABLE)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"jpeg-bytes")
        self.thumbnails.append(Path(target))
        return Path(target)

    def loudness_of(self, path: Path, **_: Any) -> float | None:
        return self.loudness

    def digest(self, path: Path, **_: Any) -> str:
        return self.digests.setdefault(Path(path).name, Path(path).name.ljust(64, "0")[:64])


def _video(name: str, duration_ms: int = 30_000) -> MediaInfo:
    return MediaInfo(
        path=name,
        size_bytes=1,
        duration_ms=duration_ms,
        video_codec="h264",
        audio_codec=None,
        width=1920,
        height=1080,
        fps=30.0,
        pix_fmt="yuv420p",
        sample_rate=None,
        channels=None,
        bitrate_kbps=4000,
    )


def _audio(name: str, duration_ms: int = 60_000, sample_rate: int = 44_100) -> MediaInfo:
    return MediaInfo(
        path=name,
        size_bytes=1,
        duration_ms=duration_ms,
        video_codec=None,
        audio_codec="mp3",
        width=None,
        height=None,
        fps=None,
        pix_fmt=None,
        sample_rate=sample_rate,
        channels=2,
        bitrate_kbps=320,
    )


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    return value


@pytest.fixture
def tools() -> _Tools:
    return _Tools()


@pytest.fixture
def state(paths: StudioPaths, tools: _Tools) -> Iterator[AppState]:
    migrate(paths.db_file)
    built = build_state(
        paths=paths,
        hub_settings=HubSettings(tail_interval_sec=0.05),
        metrics_probe=_Probe(),
        asset_tools=AssetTools(
            probe=tools.probe,
            volume=tools.volume,
            thumbnail=tools.thumbnail,
            loudness=tools.loudness_of,
            digest=tools.digest,
        ),
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


def _clip(
    paths: StudioPaths, tools: _Tools, name: str = "parkour_001.mp4", *, duration_ms: int = 30_000
) -> Path:
    target = paths.mc_parkour_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"video-bytes")
    tools.info[target.name] = _video(target.name, duration_ms=duration_ms)
    return target


def _track(
    paths: StudioPaths, tools: _Tools, name: str = "bgm_001.mp3", *, duration_ms: int = 120_000
) -> Path:
    target = paths.bgm_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"audio-bytes")
    tools.info[target.name] = _audio(target.name, duration_ms=duration_ms)
    return target


def _voice_dir(paths: StudioPaths, tools: _Tools, voice_id: str = VOICE, segments: int = 2) -> Path:
    root = paths.voice_src_dir / voice_id
    root.mkdir(parents=True, exist_ok=True)
    for index in range(1, segments + 1):
        name = f"ref_{index:02d}.mp3"
        (root / name).write_bytes(b"ref-bytes")
        tools.info[name] = _audio(name, duration_ms=15_000, sample_rate=24_000)
    (root / "ref.txt").write_text("这是一段参考音的文字稿。", encoding="utf-8")
    return root


def _rows(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _audit(connection: sqlite3.Connection, action: str | None = None) -> list[sqlite3.Row]:
    if action is None:
        return connection.execute("SELECT * FROM audit_ops ORDER BY id").fetchall()
    return connection.execute("SELECT * FROM audit_ops WHERE action = ? ORDER BY id", (action,)).fetchall()


def _asset_logs(state: AppState) -> list[Any]:
    return [row for row in state.logs.recent(limit=200) if row.source == "assets"]


def _error(body: dict[str, Any]) -> str:
    assert "code" in body, body
    return str(body["code"])


# ══════════════════════════════════════════════════════════════════════
# ① 读 · 库里有什么
# ══════════════════════════════════════════════════════════════════════


def test_library_lists_three_sections_with_roots(client: TestClient, paths: StudioPaths) -> None:
    """一次拿全：三类分节，每节带**目录路径**（面板要常驻显示"往哪放素材"）。"""
    body = client.get(ASSETS_URL).json()
    assert [section["kind"] for section in body["sections"]] == ["broll", "voice", "bgm"]
    roots = {section["kind"]: Path(section["root"]) for section in body["sections"]}
    assert roots["broll"] == paths.mc_parkour_dir
    assert roots["voice"] == paths.voice_src_dir
    assert roots["bgm"] == paths.bgm_dir


def test_empty_library_is_degraded_and_says_why(client: TestClient) -> None:
    """空库 ⇒ `degraded=true` + 说清原因：出片会走黑屏降级（P4：不如实说等于骗人）。"""
    body = client.get(ASSETS_URL).json()
    assert body["degraded"] is True
    assert body["note"] is not None and "黑屏" in body["note"]
    broll = body["sections"][0]
    assert broll["items"] == []
    assert broll["stats"] == {
        "total": 0,
        "enabled": 0,
        "total_duration_ms": 0,
        "enabled_duration_ms": 0,
    }
    assert broll["shortfall"] is not None


def test_stats_endpoint_carries_thresholds(client: TestClient) -> None:
    """判据线**现取**（前端不抄第二份）：`clips >= 60 且 >= 30min`。"""
    body = client.get(STATS_URL).json()
    assert body["thresholds"]["broll_min_clips"] == 60
    assert body["thresholds"]["broll_min_duration_ms"] == 30 * 60 * 1000
    assert body["thresholds"]["bgm_min_duration_ms"] == 15_000
    assert body["thresholds"]["voice_min_segments"] == 2
    assert body["thresholds"]["voice_max_segments"] == 3
    # 授权枚举也现取（面板的下拉不抄第二份）
    assert body["licenses"] == ["authorized", "cc0", "purchased", "self_recorded"]
    assert body["degraded"] is True
    assert len(body["sections"]) == 3
    # 只要数字：不把整库拖过来
    assert "items" not in body["sections"][0]
    assert body["sections"][0]["stats"]["total"] == 0


def test_ingest_then_library_shows_item_with_kind_discriminator(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """入库之后列表里就有它，`kind` 是判别字段（前端据此收窄到跑酷那一支）。"""
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    broll = client.get(ASSETS_URL).json()["sections"][0]
    assert broll["stats"]["total"] == 1
    item = broll["items"][0]
    assert item["kind"] == "broll"
    assert item["id"] == BROLL
    assert item["enabled"] is True
    assert item["license"] == "cc0"
    assert item["duration_ms"] == 30_000
    assert item["has_text"] is False
    assert item["use_count"] == 0


# ══════════════════════════════════════════════════════════════════════
# ② 入库 · 扫盘
# ══════════════════════════════════════════════════════════════════════


def test_dry_run_writes_nothing_then_real_ingest_creates(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """`dry_run=true` ⇒ **一个字节都不写库**；真入库之后才有行（同一套判定跑两次）。"""
    _clip(paths, tools)
    preview = client.post(INGEST_URL, json={"license": "cc0", "dry_run": True}).json()
    assert preview["dry_run"] is True
    assert preview["sections"][0]["counts"]["found"] == 1
    assert preview["sections"][0]["assets"][0]["usable"] is True
    assert preview["sections"][0]["assets"][0]["action"] is None
    assert _rows(connection, "broll_clips") == []

    real = client.post(INGEST_URL, json={"license": "cc0"}).json()
    assert real["dry_run"] is False
    assert real["totals"]["created"] == 1
    assert real["sections"][0]["assets"][0]["action"] == "created"
    assert real["sections"][0]["assets"][0]["stored"] is True
    assert len(_rows(connection, "broll_clips")) == 1


def test_missing_license_rejects_new_item_without_faking_consent(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """缺授权 ⇒ **拒绝入库**（不替用户伪造 R2 留痕），并如实说清是哪一条缺。"""
    _clip(paths, tools)
    body = client.post(INGEST_URL, json={}).json()
    item = body["sections"][0]["assets"][0]
    assert item["usable"] is False
    assert item["action"] is None
    assert item["note"] is not None and "未入库" in item["note"]
    codes = [problem["code"] for problem in item["check"]["problems"]]
    assert "license_missing" in codes
    assert body["totals"]["rejected"] == 1
    assert _rows(connection, "broll_clips") == []


def test_invalid_license_is_422_with_allowed_values(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """授权类型不在枚举里 ⇒ 422（不是"入库失败"），并把**合法值**一起回给面板。"""
    _clip(paths, tools)
    resp = client.post(INGEST_URL, json={"license": "whatever"})
    assert resp.status_code == 422
    body = resp.json()
    assert _error(body) == ErrorCode.ASSET_INVALID.value
    assert body["context"]["allowed"] == ["authorized", "cc0", "purchased", "self_recorded"]
    assert _rows(connection, "broll_clips") == []


def test_strays_are_reported_not_silently_dropped(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """命名不合规的文件 ⇒ 进 `strays` 报出来（用户看到的是"它没被认出来"，不是"扫了 0 条"）。"""
    stray = paths.mc_parkour_dir / "跑酷1.mp4"
    stray.write_bytes(b"x")
    section = client.post(INGEST_URL, json={"license": "cc0", "kind": "broll"}).json()["sections"][0]
    assert section["strays"] == [str(stray)]
    assert section["counts"]["strays"] == 1
    assert section["counts"]["found"] == 0
    assert section["assets"] == []


def test_ingest_writes_one_assets_log_line(
    client: TestClient, state: AppState, paths: StudioPaths, tools: _Tools
) -> None:
    """入库只往 `system_logs` 写一行（`source='assets'`）—— 不新增 WS 事件，面板靠 logs 通道刷新。"""
    _clip(paths, tools)
    assert _asset_logs(state) == []
    client.post(INGEST_URL, json={"license": "cc0"})
    logs = _asset_logs(state)
    assert len(logs) == 1
    assert "素材入库" in logs[0].message
    assert logs[0].payload["totals"]["created"] == 1


def test_duplicate_content_is_refused(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """同一段内容换个文件名再丢进来 ⇒ `duplicate`，不新增行（防搬运的机器判据）。"""
    _clip(paths, tools, "parkour_001.mp4")
    _clip(paths, tools, "parkour_002.mp4")
    same = "a" * 64
    tools.digests["parkour_001.mp4"] = same
    tools.digests["parkour_002.mp4"] = same
    body = client.post(INGEST_URL, json={"license": "cc0", "kind": "broll"}).json()
    actions = {item["id"]: item["action"] for item in body["sections"][0]["assets"]}
    assert actions["parkour_001"] == "created"
    assert actions["parkour_002"] == "duplicate"
    assert body["sections"][0]["counts"]["duplicate"] == 1
    assert len(_rows(connection, "broll_clips")) == 1


def test_ids_filter_lets_you_rescan_one_item(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """`ids` 只处理指定的那几条（坏文件修好之后不用整批重来）。"""
    _clip(paths, tools, "parkour_001.mp4")
    _clip(paths, tools, "parkour_002.mp4")
    body = client.post(INGEST_URL, json={"license": "cc0", "kind": "broll", "ids": ["parkour_002"]}).json()
    section = body["sections"][0]
    assert [item["id"] for item in section["assets"]] == ["parkour_002"]
    assert [row["id"] for row in _rows(connection, "broll_clips")] == ["parkour_002"]


# ══════════════════════════════════════════════════════════════════════
# ③ 标记 · 启用 / 停用 / 改字段
# ══════════════════════════════════════════════════════════════════════


def test_disable_keeps_the_file_and_writes_audit(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """停用 = **只改一行**：物理文件原地不动（误删不可逆，停用随时能点回来）。"""
    target = _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    body = client.patch(f"{ASSETS_URL}/{BROLL}", json={"enabled": False}).json()
    assert body["enabled"] is False
    assert _rows(connection, "broll_clips")[0]["enabled"] == 0
    assert target.is_file()
    ops = _audit(connection, "asset.disable")
    assert len(ops) == 1
    assert ops[0]["target_id"] == BROLL
    assert ops[0]["target_type"] == "asset"
    assert ops[0]["actor"] == "user"
    assert ops[0]["source"] == "webui"
    # 留痕里带 `kind`：同一个 id 在不同类里出现时，审计页要能分清是哪一类
    assert json.loads(ops[0]["before_json"]) == {"kind": "broll", "enabled": True}
    assert json.loads(ops[0]["after_json"]) == {"kind": "broll", "enabled": False}


def test_repeating_the_same_toggle_leaves_no_audit_noise(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """状态没变就不留痕（否则审计页会被"重复点同一下"刷满）。"""
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    client.patch(f"{ASSETS_URL}/{BROLL}", json={"enabled": False})
    again = client.patch(f"{ASSETS_URL}/{BROLL}", json={"enabled": False}).json()
    assert again["enabled"] is False
    assert len(_audit(connection, "asset.disable")) == 1


def test_patch_license_and_tags_records_before_after(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """授权 / 标签可改，且 `before` / `after` 只放**被改的字段**（审计表不被整行刷满）。"""
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    body = client.patch(
        f"{ASSETS_URL}/{BROLL}",
        json={"license": "purchased", "tags": ["夜间", "霓虹"], "has_text": True},
    ).json()
    assert body["license"] == "purchased"
    assert body["tags"] == ["夜间", "霓虹"]
    assert body["has_text"] is True
    op = _audit(connection, "asset.update")[0]
    assert json.loads(op["before_json"]) == {
        "kind": "broll",
        "license": "cc0",
        "tags": [],
        "has_text": False,
    }
    assert json.loads(op["after_json"]) == {
        "kind": "broll",
        "license": "purchased",
        "tags": ["夜间", "霓虹"],
        "has_text": True,
    }


def test_patch_empty_body_returns_row_without_audit(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """空 PATCH 不是错误，但也不该假装改了一次：回当前行、不留痕。"""
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    body = client.patch(f"{ASSETS_URL}/{BROLL}", json={}).json()
    assert body["id"] == BROLL
    assert body["license"] == "cc0"
    assert _audit(connection) == []


def test_patch_invalid_license_is_422(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    resp = client.patch(f"{ASSETS_URL}/{BROLL}", json={"license": "nope"})
    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.ASSET_INVALID.value


def test_patch_empty_usable_range_is_422(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """可用区间为空 ⇒ 422（留着它只会在渲染那一刻才报错）。"""
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    resp = client.patch(f"{ASSETS_URL}/{BROLL}", json={"usable_from_ms": 20_000, "usable_to_ms": 1_000})
    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.ASSET_INVALID.value


def test_patch_unknown_field_is_rejected_by_the_contract(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """字段名写错 ⇒ 422（`extra="forbid"`）：静默忽略会让人以为"区间已经标好了"。"""
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    resp = client.patch(f"{ASSETS_URL}/{BROLL}", json={"usableFrom": 0})
    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.VALIDATION_FAILED.value


def test_patch_missing_asset_is_404(client: TestClient) -> None:
    """不在库里 ⇒ 404（请求没写错，是它还没入库）。"""
    resp = client.patch(f"{ASSETS_URL}/parkour_999", json={"enabled": False})
    assert resp.status_code == 404
    assert _error(resp.json()) == ErrorCode.ASSET_NOT_FOUND.value


def test_patch_ambiguous_id_without_kind_is_422(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """同一个 id 在两类里都有（音色目录正好叫 `bgm_001`）⇒ **明确报错**，绝不猜一个改错行。"""
    _voice_dir(paths, tools, BGM)
    _track(paths, tools, f"{BGM}.mp3")
    client.post(INGEST_URL, json={"license": "cc0"})
    assert len(_rows(connection, "voice_profiles")) == 1
    assert len(_rows(connection, "bgm_tracks")) == 1

    explicit = client.patch(f"{ASSETS_URL}/{BGM}", json={"enabled": False, "kind": "bgm"})
    assert explicit.status_code == 200
    assert explicit.json()["kind"] == "bgm"

    ambiguous = client.patch(f"{ASSETS_URL}/{BGM}", json={"enabled": False})
    assert ambiguous.status_code == 422
    assert _error(ambiguous.json()) == ErrorCode.ASSET_INVALID.value


# ══════════════════════════════════════════════════════════════════════
# ④ 二进制 · 预览与试听
# ══════════════════════════════════════════════════════════════════════


def test_thumb_serves_the_generated_jpeg(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    resp = client.get(f"{ASSETS_URL}/{BROLL}/thumb")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == b"jpeg-bytes"
    assert tools.thumbnails == [paths.thumb_file("broll", BROLL)]


def test_thumb_missing_is_404(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """抽帧失败**不阻塞入库** ⇒ 这条素材在库里、但没有预览图。此时如实 404，不临时现抽。"""
    tools.thumbnail_error = True
    _clip(paths, tools)
    report = client.post(INGEST_URL, json={"license": "cc0"}).json()
    item = report["sections"][0]["assets"][0]
    assert item["action"] == "created"
    assert item["thumb_path"] is None
    assert item["note"] is not None and "缩略图" in item["note"]
    resp = client.get(f"{ASSETS_URL}/{BROLL}/thumb")
    assert resp.status_code == 404
    assert _error(resp.json()) == ErrorCode.ASSET_NOT_FOUND.value


def test_media_serves_the_original_file(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """试听 / 预览走原文件（路径**只从库里取**，请求参数进不了路径）。"""
    target = _track(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    resp = client.get(f"{ASSETS_URL}/{BGM}/media")
    assert resp.status_code == 200
    assert resp.content == target.read_bytes()
    assert resp.headers["content-type"] == "audio/mpeg"


def test_media_for_voice_is_422(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """音色是**目录**（多段参考音），不是一个能播的文件 ⇒ 明确 422，不猜播哪一段。"""
    _voice_dir(paths, tools)
    client.post(INGEST_URL, json={})
    resp = client.get(f"{ASSETS_URL}/{VOICE}/media")
    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.ASSET_INVALID.value


def test_media_when_file_moved_away_is_404_but_row_survives(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """文件被挪走 ⇒ 404 说清路径，**库里那行照旧**（我们不会替你删记录）。"""
    target = _track(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    target.unlink()
    resp = client.get(f"{ASSETS_URL}/{BGM}/media")
    assert resp.status_code == 404
    assert _error(resp.json()) == ErrorCode.ASSET_NOT_FOUND.value
    assert len(_rows(connection, "bgm_tracks")) == 1


def test_asset_id_pattern_is_enforced(client: TestClient) -> None:
    """id 白名单（小写字母 / 数字开头）在**入参**就挡住：它要进 SQL 参数、拼文件名、当 URL。"""
    resp = client.get(f"{ASSETS_URL}/Parkour/thumb")
    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.VALIDATION_FAILED.value
