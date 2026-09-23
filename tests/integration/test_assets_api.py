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
PRUNE_URL = "/api/v1/assets/prune"

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
    # 段数**故意不在** thresholds 里（裁定 377）：它不是判据 —— 一段能用，很多段也能用。
    assert "voice_min_segments" not in body["thresholds"]
    assert "voice_max_segments" not in body["thresholds"]
    assert body["thresholds"]["voice_segment_min_ms"] == 2_000
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
# ①b 读 · 一类一页（跑酷 / 音色 / BGM 各是一个菜单）
# ══════════════════════════════════════════════════════════════════════
#
# 分菜单 + 分页之后，面板不再"一次拿三类"，而是"一屏看一类、一页看几条"。
# 这一组用例钉的就是那条接口上最容易写错的三件事：
# ① **只能看见这一类**（拿 BGM 的菜单却列出跑酷，是最难发现的一类错）；
# ② **`stats` 与 `total` 不是一个数**（筛出 1 条时说"库里只有 1 条"会让人去补素材）；
# ③ **翻过头给最后一页**，不是一页空白、也不是报错。

LIST_URL = "/api/v1/assets/list"


def _seed_clips(
    client: TestClient, paths: StudioPaths, tools: _Tools, count: int, *, prefix: str = "parkour"
) -> list[str]:
    """造 `count` 条跑酷素材并入库；返回它们的 id（按 id 排序）。"""
    for index in range(1, count + 1):
        _clip(paths, tools, f"{prefix}_{index:03d}.mp4")
    client.post(INGEST_URL, json={"license": "cc0", "kind": "broll"})
    return [f"{prefix}_{index:03d}" for index in range(1, count + 1)]


def test_page_shows_one_kind_only(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """一个菜单只看见一类：跑酷那一页里**没有** BGM 的条目。"""
    _seed_clips(client, paths, tools, 3)
    _track(paths, tools, "bgm_001.mp3")
    client.post(INGEST_URL, json={"license": "cc0", "kind": "bgm"})

    body = client.get(LIST_URL, params={"kind": "broll"}).json()
    assert body["kind"] == "broll"
    assert [item["id"] for item in body["items"]] == ["parkour_001", "parkour_002", "parkour_003"]
    assert {item["kind"] for item in body["items"]} == {"broll"}
    assert body["stats"]["total"] == 3
    assert body["total"] == 3
    assert body["pages"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 20

    bgm = client.get(LIST_URL, params={"kind": "bgm"}).json()
    assert [item["id"] for item in bgm["items"]] == ["bgm_001"]


def test_page_slices_and_clamps_the_last_page(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """每页 2 条 ⇒ 3 页；翻过头（page=99）给**最后一页**，而不是一页空白。"""
    _seed_clips(client, paths, tools, 5)

    first = client.get(LIST_URL, params={"kind": "broll", "page": 1, "page_size": 2}).json()
    assert first["pages"] == 3
    assert [item["id"] for item in first["items"]] == ["parkour_001", "parkour_002"]

    last = client.get(LIST_URL, params={"kind": "broll", "page": 3, "page_size": 2}).json()
    assert [item["id"] for item in last["items"]] == ["parkour_005"]

    over = client.get(LIST_URL, params={"kind": "broll", "page": 99, "page_size": 2}).json()
    assert over["page"] == 3
    assert [item["id"] for item in over["items"]] == ["parkour_005"]


def test_page_query_matches_id_and_tags(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """按 id 或**标签**筛（标签是人自己填的，记不住 id 的时候靠它找）。"""
    _seed_clips(client, paths, tools, 3)
    client.patch(f"{ASSETS_URL}/parkour_002", json={"kind": "broll", "tags": ["夜景", "备用"]})

    by_id = client.get(LIST_URL, params={"kind": "broll", "q": "003"}).json()
    assert [item["id"] for item in by_id["items"]] == ["parkour_003"]

    by_tag = client.get(LIST_URL, params={"kind": "broll", "q": "夜景"}).json()
    assert [item["id"] for item in by_tag["items"]] == ["parkour_002"]

    nothing = client.get(LIST_URL, params={"kind": "broll", "q": "没有这种东西"}).json()
    assert nothing["items"] == []
    assert nothing["total"] == 0
    assert nothing["pages"] == 1, "筛空时也该给一页（页码从 1 起，不是 0）"


def test_page_enabled_filter_is_three_state(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """`enabled` 三态：不给 = 全部，`true` / `false` 各筛一边。"""
    _seed_clips(client, paths, tools, 3)
    client.patch(f"{ASSETS_URL}/parkour_002", json={"kind": "broll", "enabled": False})

    every = client.get(LIST_URL, params={"kind": "broll"}).json()
    assert every["total"] == 3

    on = client.get(LIST_URL, params={"kind": "broll", "enabled": "true"}).json()
    assert [item["id"] for item in on["items"]] == ["parkour_001", "parkour_003"]

    off = client.get(LIST_URL, params={"kind": "broll", "enabled": "false"}).json()
    assert [item["id"] for item in off["items"]] == ["parkour_002"]


def test_page_stats_stay_whole_under_filters(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """`stats` 是**家底**（不受筛选影响），`total` 才是**这一页的筛选结果**。

    合成一个数的后果：筛出 1 条时面板说"这一类只有 1 条素材"，用户接着就去补素材了。
    """
    _seed_clips(client, paths, tools, 4)
    body = client.get(LIST_URL, params={"kind": "broll", "q": "001"}).json()

    assert body["total"] == 1
    assert body["stats"]["total"] == 4
    assert body["stats"]["enabled"] == 4
    assert body["usable"] == 4


def test_page_carries_the_same_usable_as_the_whole_library(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """同一类在两处（一次拿全 / 一页一页看）必须给出**同一个** `usable` —— 面板拿它上色。"""
    _seed_clips(client, paths, tools, 3)
    client.patch(f"{ASSETS_URL}/parkour_002", json={"kind": "broll", "enabled": False})

    library = client.get(ASSETS_URL).json()["sections"][0]
    page = client.get(LIST_URL, params={"kind": "broll"}).json()
    assert page["usable"] == library["usable"] == 2
    assert page["disk_total"] == library["disk_total"] == 3


def test_page_requires_a_kind(client: TestClient) -> None:
    """`kind` 必填：不给就 422（面板三个菜单各自知道自己是谁，没有"默认哪一类"）。"""
    resp = client.get(LIST_URL)
    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.VALIDATION_FAILED.value


def test_page_bounds_are_enforced_at_the_edge(client: TestClient) -> None:
    """页码与每页条数在**入参**就卡范围：不卡的话 `page_size=100000` 就是"把整库拖过来"。"""
    for params in (
        {"kind": "broll", "page": 0},
        {"kind": "broll", "page_size": 0},
        {"kind": "broll", "page_size": 100_000},
    ):
        resp = client.get(LIST_URL, params=params)
        assert resp.status_code == 422, params
        assert _error(resp.json()) == ErrorCode.VALIDATION_FAILED.value


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


def test_patch_treats_kind_as_a_selector_not_a_field(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """面板每次 PATCH 都带 `kind`（它得防"两类同名 id"）—— 那**不是**要写进表的字段。

    这条用例的由来是一个真 bug：`changes()` 曾把 `kind` 一起交给仓储层，而那一层的
    `_patch` 有列白名单 ⇒ **面板上每一次启停 / 改授权都是 422**。而 CLI / 早期用例
    都没带 `kind`，所以它一直没红。
    """
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})

    resp = client.patch(f"{ASSETS_URL}/{BROLL}", json={"kind": "broll", "enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    assert [row["id"] for row in _rows(connection, "broll_clips")] == [BROLL]

    # 留痕里只有 `enabled` 真的变了（`kind` 是服务层**给留痕加的定位信息**，
    # 不是"被改的字段"—— 它出现在 before/after 里是刻意的，便于审计页知道这是哪一类）
    op = _audit(connection, "asset.disable")[0]
    assert json.loads(op["before_json"]) == {"kind": "broll", "enabled": True}
    assert json.loads(op["after_json"]) == {"kind": "broll", "enabled": False}


def test_patch_null_clears_a_field(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """显式 `null` = **清空**（面板上把「来源地址」擦干净、点保存，就该真的没了）。

    这条用例的由来是素材库面板验收时的一个真 bug：`changes()` 用 `exclude_none`，
    于是「字段没给」与「字段给了 null」被当成同一件事 —— 而这两件事在 PATCH 里
    **正好相反**（前者"别动它"、后者"把它清掉"）。后果是：擦干净一个框、点保存，
    **什么都没发生**，而面板看起来是保存成功了（值还在，用户以为自己没点到）。

    `usable_to_ms: null`（留空 = 到片尾）走的是同一条路，所以两条一起钉住。
    """
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    client.patch(
        f"{ASSETS_URL}/{BROLL}",
        json={"source_url": "https://example.com/a.mp4", "usable_to_ms": 20_000},
    )

    resp = client.patch(f"{ASSETS_URL}/{BROLL}", json={"source_url": None, "usable_to_ms": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["source_url"] is None
    assert resp.json()["usable_to_ms"] is None

    row = _rows(connection, "broll_clips")[0]
    assert row["source_url"] is None
    assert row["usable_to_ms"] is None


def test_patch_ignores_null_on_a_field_that_cannot_be_empty(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """不该为空的字段上收到 `null` ⇒ 当成**没给**，而不是把 `NULL` 塞进 NOT NULL 的列。

    `enabled` / `has_text` / `loopable` 在 DDL 里是 `INTEGER NOT NULL CHECK IN (0,1)`：
    把 `None` 交给它们换来的是一条 500（数据库约束炸了），而不是一句人能看懂的话。
    """
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})

    resp = client.patch(f"{ASSETS_URL}/{BROLL}", json={"enabled": None, "has_text": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    assert resp.json()["has_text"] is False


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


# ══════════════════════════════════════════════════════════════════════
# ⑦ 上传（T4.8 的图形化入库入口 · §3.3.14 / §4.3.1）
# ══════════════════════════════════════════════════════════════════════
#
# 这一组用例回答的是同一个问题：**在面板上拖几个文件进去，会发生什么**。
# 四条纪律在这里的具体形状：
#
# ① 断言也看盘、也看库 —— "上传即入库"这句承诺必须两边都兑现；
# ② 一条坏文件**不中断整批**（与扫盘同一条）；
# ③ 同名冲突**绝不静默覆盖**（那可能是一份手工剪过的片段）；
# ④ 缺授权时文件落盘但**不入库** —— 盘上多一条 pending 是如实的结果，
#    替用户填一个 license 才是伪造 R2 留痕。

UPLOAD_URL = "/api/v1/assets/upload"
VOICE_UPLOAD_URL = "/api/v1/assets/voice"


def _part(name: str, body: bytes = b"video-bytes") -> tuple[str, tuple[str, bytes, str]]:
    return ("files", (name, body, "application/octet-stream"))


def _on_disk(paths: StudioPaths, kind: str, name: str) -> Path:
    root = {
        "broll": paths.mc_parkour_dir,
        "bgm": paths.bgm_dir,
        "voice": paths.voice_src_dir,
    }[kind]
    return root / name


def test_upload_broll_stores_the_file_and_ingests_it_in_one_step(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """上传 = 落盘 + 入库**一次做完**（用户点的是"放进素材库"，不是"放进一个目录"）。"""
    tools.info["parkour_new.mp4"] = _video("parkour_new.mp4")
    resp = client.post(
        UPLOAD_URL,
        data={"kind": "broll", "license": "cc0"},
        files=[_part("parkour_new.mp4")],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["stored"], body["replaced"], body["skipped"]) == (1, 0, 0)
    assert body["files"][0]["asset_id"] == "parkour_new"
    assert body["files"][0]["status"] == "stored"

    # 盘上真在（而且没有留下 .partial）
    target = _on_disk(paths, "broll", "parkour_new.mp4")
    assert target.read_bytes() == b"video-bytes"
    assert list(paths.mc_parkour_dir.glob("*.partial")) == []

    # 库里真有 —— 不用再点一次「扫描并入库」
    assert [row["id"] for row in _rows(connection, "broll_clips")] == ["parkour_new"]
    assert body["report"] is not None
    assert body["report"]["totals"]["created"] == 1
    assert body["report"]["sections"][0]["assets"][0]["usable"] is True


def test_upload_normalizes_the_browser_filename_and_says_so(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """``跑酷 01.MP4`` ⇒ ``parkour_01.mp4``：改名可以，**不说一声**不行。"""
    tools.info["parkour_01.mp4"] = _video("parkour_01.mp4")
    resp = client.post(
        UPLOAD_URL,
        data={"kind": "broll", "license": "cc0"},
        files=[_part("跑酷 01.MP4", b"video-bytes")],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files"][0]["asset_id"] == "parkour_01"
    assert _on_disk(paths, "broll", "parkour_01.mp4").is_file()
    assert "原名" in body["files"][0]["message"]


def test_upload_rejects_an_unknown_suffix_with_a_reason(client: TestClient, paths: StudioPaths) -> None:
    """扩展名不对 ⇒ 逐条报"跳过 + 为什么"，**一个字节都不写**，也不假装入库了。"""
    resp = client.post(
        UPLOAD_URL,
        data={"kind": "broll", "license": "cc0"},
        files=[_part("notes.txt", b"hello")],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["stored"], body["skipped"]) == (0, 1)
    assert body["files"][0]["status"] == "skipped"
    assert ".txt" in body["files"][0]["message"]
    assert body["report"] is None
    assert list(paths.mc_parkour_dir.iterdir()) == []


def test_one_bad_file_does_not_stop_the_batch(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """一次拖两个文件、坏了一个 ⇒ 好的那个照常入库（与扫盘"坏文件不中断整批"同一条）。"""
    tools.info["bgm_chill.mp3"] = _audio("bgm_chill.mp3", duration_ms=120_000)
    resp = client.post(
        UPLOAD_URL,
        data={"kind": "bgm", "license": "cc0"},
        files=[_part("bgm_chill.mp3", b"audio-bytes"), _part("clip.mp4", b"video-bytes")],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["stored"], body["skipped"]) == (1, 1)
    assert [row["id"] for row in _rows(connection, "bgm_tracks")] == ["bgm_chill"]


def test_upload_never_clobbers_until_you_ask(client: TestClient, paths: StudioPaths, tools: _Tools) -> None:
    """同名文件默认**拒绝**（409 的语义，逐条报出来）；勾了「覆盖同名」才替换。"""
    target = _on_disk(paths, "broll", "parkour_001.mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"hand-edited")
    tools.info["parkour_001.mp4"] = _video("parkour_001.mp4")

    first = client.post(
        UPLOAD_URL,
        data={"kind": "broll", "license": "cc0"},
        files=[_part("parkour_001.mp4", b"from-browser")],
    )
    assert first.status_code == 200, first.text
    assert first.json()["files"][0]["status"] == "skipped"
    assert target.read_bytes() == b"hand-edited", "手工剪过的片段被静默盖掉了"

    second = client.post(
        UPLOAD_URL,
        data={"kind": "broll", "license": "cc0", "overwrite": "true"},
        files=[_part("parkour_001.mp4", b"from-browser")],
    )
    assert second.status_code == 200, second.text
    assert second.json()["files"][0]["status"] == "replaced"
    assert target.read_bytes() == b"from-browser"


def test_upload_without_license_writes_the_file_but_does_not_fake_consent(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """没选授权 ⇒ 文件落盘（盘上多一条 pending）、**不入库**，报告里如实说"未入库"。"""
    tools.info["parkour_9.mp4"] = _video("parkour_9.mp4")
    resp = client.post(UPLOAD_URL, data={"kind": "broll"}, files=[_part("parkour_9.mp4", b"video-bytes")])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files"][0]["status"] == "stored"
    assert _on_disk(paths, "broll", "parkour_9.mp4").is_file()
    assert _rows(connection, "broll_clips") == []
    assert body["report"]["totals"]["rejected"] == 1


def test_upload_with_a_bad_license_writes_nothing(client: TestClient, paths: StudioPaths) -> None:
    """授权类型不合法 ⇒ 422，而且**在写盘之前**就挡住（不留孤儿文件）。"""
    resp = client.post(
        UPLOAD_URL,
        data={"kind": "broll", "license": "borrowed-from-a-friend"},
        files=[_part("parkour_1.mp4")],
    )

    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.ASSET_INVALID.value
    assert list(paths.mc_parkour_dir.iterdir()) == []


def test_flat_upload_refuses_the_voice_kind(client: TestClient) -> None:
    """音色是目录，不是平铺文件 ⇒ 明确指路（面板上音色那一节有它自己的上传口）。"""
    resp = client.post(
        UPLOAD_URL,
        data={"kind": "voice"},
        files=[_part("a.wav", b"audio")],
    )
    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.ASSET_INVALID.value


def test_voice_upload_numbers_refs_in_filename_order(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """音色上传：按原文件名排序落成 ``ref_01`` / ``ref_02``，``ref_text`` 逐行写成 ``ref.txt``。"""
    for name in ("ref_01.wav", "ref_02.wav"):
        tools.info[name] = _audio(name, duration_ms=15_000, sample_rate=24_000)

    resp = client.post(
        VOICE_UPLOAD_URL,
        data={"voice_id": "bear_da", "license": "self_recorded", "ref_text": "第一句\n\n第二句"},
        files=[_part("b.wav", b"B"), _part("a.wav", b"A")],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["stored"], body["skipped"]) == (3, 0), body
    root = paths.voice_src_dir / "bear_da"
    assert (root / "ref_01.wav").read_bytes() == b"A", "排序没生效（ref_01 该是 a.wav）"
    assert (root / "ref_02.wav").read_bytes() == b"B"
    assert (root / "ref.txt").read_text(encoding="utf-8") == "第一句\n第二句\n"
    assert [row["id"] for row in _rows(connection, "voice_profiles")] == ["bear_da"]
    assert body["report"]["totals"]["created"] == 1


def test_voice_upload_with_a_bad_id_is_422(client: TestClient, paths: StudioPaths) -> None:
    """``voice_id`` 就是目录名 ⇒ 与素材 id **同一条**正则，在入参那一层就挡住。"""
    resp = client.post(
        VOICE_UPLOAD_URL,
        data={"voice_id": "../escape"},
        files=[_part("a.wav", b"A")],
    )
    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.VALIDATION_FAILED.value
    assert not (paths.voice_src_dir.parent / "escape").exists()


def test_voice_upload_keeps_the_line_numbers_aligned_when_a_file_is_skipped(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """中间夹一个非音频文件 ⇒ 它被跳过，**后面的段号不跳号**（跳了 ref.txt 就整体错位）。"""
    for name in ("ref_01.wav", "ref_02.wav"):
        tools.info[name] = _audio(name, duration_ms=15_000, sample_rate=24_000)

    resp = client.post(
        VOICE_UPLOAD_URL,
        data={"voice_id": "bear_da", "license": "self_recorded"},
        files=[_part("a.wav", b"A"), _part("readme.txt", b"x"), _part("c.wav", b"C")],
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["stored"], body["skipped"]) == (2, 1)
    root = paths.voice_src_dir / "bear_da"
    assert sorted(item.name for item in root.iterdir()) == ["ref_01.wav", "ref_02.wav"]
    assert (root / "ref_02.wav").read_bytes() == b"C"


# ══════════════════════════════════════════════════════════════════════
# ③′ 写 · 音色的逐段管理（裁定 381）
# ══════════════════════════════════════════════════════════════════════

VOICE_SEGMENTS_URL = "/api/v1/assets/voice/{voice_id}/segments"


def _voice_upload(
    client: TestClient,
    tools: _Tools,
    *,
    voice_id: str = VOICE,
    parts: list[tuple[str, bytes]],
    ref_text: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """传一批参考音（顺带把它们登记进假探针，免得入库时报「解不开」）。

    探针是按**盘上那个名字**去查的（``ref_01.wav``），不是用户选的那个原文件名 ——
    上传做的第一件事就是改名，这里要按改名后的名字登记。
    """
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for index, (name, blob) in enumerate(parts, start=1):
        target = f"ref_{index:02d}{Path(name).suffix}"
        tools.info[target] = _audio(target, duration_ms=15_000, sample_rate=24_000)
        files.append(_part(name, blob))
    data: dict[str, str] = {"voice_id": voice_id, "license": "self_recorded"}
    if ref_text is not None:
        data["ref_text"] = ref_text
    if overwrite:
        data["overwrite"] = "true"
    resp = client.post(VOICE_UPLOAD_URL, data=data, files=files)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def test_voice_overwrite_mirrors_the_directory(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """勾了「覆盖同名」⇒ **镜像**：这次没传到的旧段清掉，并逐条报出来。

    只管同名的那几个，会留下一份"两边都不是"的目录：新传 2 段、旧的 ``ref_03.wav``
    还在 ⇒ 库里报 3 段、引擎把三段拼起来当 prompt，而用户以为自己只留了 2 段。
    用户看到的正是"覆盖没生效"。
    """
    _voice_upload(
        client,
        tools,
        parts=[("a.wav", b"A"), ("b.wav", b"B"), ("c.wav", b"C")],
        ref_text="一\n二\n三",
    )
    root = paths.voice_src_dir / VOICE
    assert sorted(item.name for item in root.iterdir()) == [
        "ref.txt",
        "ref_01.wav",
        "ref_02.wav",
        "ref_03.wav",
    ]

    body = _voice_upload(
        client,
        tools,
        parts=[("a.wav", b"A2"), ("b.wav", b"B2")],
        ref_text="一\n二",
        overwrite=True,
    )

    assert body["removed"] == ["ref_03.wav"], body
    assert (root / "ref_01.wav").read_bytes() == b"A2"
    assert (root / "ref_02.wav").read_bytes() == b"B2"
    assert not (root / "ref_03.wav").exists()
    assert (root / "ref.txt").read_text(encoding="utf-8") == "一\n二\n"
    # 库里那行也跟着变成 2 段（否则面板还是显示 3 段 —— 那正是"没生效"的观感）
    row = connection.execute("SELECT ref_count FROM voice_profiles WHERE id = ?", (VOICE,)).fetchone()
    assert row["ref_count"] == 2


def test_voice_upload_never_prunes_without_overwrite(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """没勾覆盖 ⇒ 一个字节都不删（撞名的逐条 skipped，其余段原地不动）。"""
    _voice_upload(
        client,
        tools,
        parts=[("a.wav", b"A"), ("b.wav", b"B"), ("c.wav", b"C")],
        ref_text="一\n二\n三",
    )
    body = _voice_upload(client, tools, parts=[("a.wav", b"A2")], ref_text="一")

    assert body["removed"] == []
    assert body["skipped"] >= 1
    root = paths.voice_src_dir / VOICE
    assert sorted(item.name for item in root.iterdir()) == [
        "ref.txt",
        "ref_01.wav",
        "ref_02.wav",
        "ref_03.wav",
    ]
    assert (root / "ref_01.wav").read_bytes() == b"A"


def test_voice_upload_without_text_says_the_text_did_not_move(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """换了参考音却没填文字稿 ⇒ ``ref.txt`` 还是上一次那份，**必须当场说**。

    文本与音频对不上是克隆质量最直接的来源，而它的症状要等到听了成片才出现 ——
    到那时没人会回头怀疑"第二次上传时没填文字稿"。
    """
    _voice_upload(client, tools, parts=[("a.wav", b"A"), ("b.wav", b"B")], ref_text="一\n二")
    body = _voice_upload(
        client,
        tools,
        parts=[("a.wav", b"A2"), ("b.wav", b"B2"), ("c.wav", b"C2")],
        overwrite=True,
    )

    assert body["notes"], body
    assert "ref.txt 没动" in body["notes"][0]
    assert "2 行" in body["notes"][0] and "3 段" in body["notes"][0]
    assert (paths.voice_src_dir / VOICE / "ref.txt").read_text(encoding="utf-8") == "一\n二\n"


def test_voice_segments_lists_each_segment_with_its_own_text(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """逐段现状：段号 / 文件名 / 时长 / **同一位置的那行文本**（位置即对应）。"""
    _voice_upload(client, tools, parts=[("a.wav", b"A"), ("b.wav", b"B")], ref_text="一\n二")

    resp = client.get(VOICE_SEGMENTS_URL.format(voice_id=VOICE))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["voice_id"], body["ref_count"], body["text_lines"]) == (VOICE, 2, 2)
    assert [item["name"] for item in body["segments"]] == ["ref_01.wav", "ref_02.wav"]
    assert [item["text"] for item in body["segments"]] == ["一", "二"]
    assert all(item["usable"] for item in body["segments"]), body
    assert body["segments"][0]["duration_ms"] == 15_000
    assert body["enabled"] is True and body["in_library"] is True


def test_voice_segments_marks_the_segment_that_is_bad(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """不合格的那一段，结论要落在**它那一行**上（面板据此告诉用户删哪一段）。"""
    _voice_upload(client, tools, parts=[("a.wav", b"A"), ("b.wav", b"B")], ref_text="一\n二")
    tools.info["ref_02.wav"] = _audio("ref_02.wav", duration_ms=500, sample_rate=24_000)

    body = client.get(VOICE_SEGMENTS_URL.format(voice_id=VOICE)).json()

    first, second = body["segments"]
    assert first["usable"] and first["problems"] == []
    assert not second["usable"]
    assert [item["code"] for item in second["problems"]] == ["ref_02_too_short"]


def test_voice_segments_unknown_voice_is_404(client: TestClient) -> None:
    """盘上没这个目录 ⇒ 404（不是一份空表 —— 空表会被读成"这个音色是空的"）。"""
    resp = client.get(VOICE_SEGMENTS_URL.format(voice_id="nothing_here"))
    assert resp.status_code == 404
    assert _error(resp.json()) == ErrorCode.ASSET_NOT_FOUND.value


def test_voice_segment_delete_renumbers_and_rewrites_the_text(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """删中间那一段 ⇒ 后面的段**重编号**，``ref.txt`` 那一行同步丢掉。

    不重编号的话，``ref_03.wav`` 会顶上第 2 位，而第 2 行文本说的是**被删掉那一段**
    的话 —— 克隆拿到的 prompt 就成了"这段音频 + 另一段音频的文本"，而且不报错。
    """
    _voice_upload(
        client,
        tools,
        parts=[("a.wav", b"A"), ("b.wav", b"B"), ("c.wav", b"C")],
        ref_text="一\n二\n三",
    )
    root = paths.voice_src_dir / VOICE

    resp = client.delete(f"{VOICE_SEGMENTS_URL.format(voice_id=VOICE)}/ref_02.wav")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["removed"] == "ref_02.wav"
    assert body["removed_text"] == "二"
    assert body["renamed"] == [{"from": "ref_03.wav", "to": "ref_02.wav"}]
    assert body["text_rewritten"] is True
    assert sorted(item.name for item in root.iterdir()) == ["ref.txt", "ref_01.wav", "ref_02.wav"]
    assert (root / "ref_01.wav").read_bytes() == b"A"
    assert (root / "ref_02.wav").read_bytes() == b"C"
    assert (root / "ref.txt").read_text(encoding="utf-8") == "一\n三\n"
    assert [item["name"] for item in body["segments"]["segments"]] == ["ref_01.wav", "ref_02.wav"]
    assert [item["text"] for item in body["segments"]["segments"]] == ["一", "三"]
    row = connection.execute("SELECT ref_count FROM voice_profiles WHERE id = ?", (VOICE,)).fetchone()
    assert row["ref_count"] == 2
    ops = _audit(connection, "asset.voice_segment_remove")
    assert len(ops) == 1
    assert ops[0]["target_id"] == VOICE


def test_voice_segment_delete_leaves_a_mismatched_text_alone(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """``ref.txt`` 本来就与段数对不上 ⇒ **不动它**，如实说（猜一行删掉比留着更坏）。"""
    root = _voice_dir(paths, tools, segments=3)
    (root / "ref.txt").write_text("只有一行\n", encoding="utf-8")
    client.post(INGEST_URL, json={"license": "self_recorded"})

    resp = client.delete(f"{VOICE_SEGMENTS_URL.format(voice_id=VOICE)}/ref_02.mp3")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text_rewritten"] is False
    assert body["notes"] and "没动它" in body["notes"][0]
    assert (root / "ref.txt").read_text(encoding="utf-8") == "只有一行\n"


def test_voice_segment_delete_refuses_the_last_one(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """最后一段不能删 —— 删了音色就念不出来了，而库里那一行还在、面板还是绿的。"""
    _voice_upload(client, tools, parts=[("a.wav", b"A")], ref_text="一")

    resp = client.delete(f"{VOICE_SEGMENTS_URL.format(voice_id=VOICE)}/ref_01.wav")

    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.ASSET_INVALID.value
    assert (paths.voice_src_dir / VOICE / "ref_01.wav").is_file()


def test_voice_segment_delete_unknown_name_is_404(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """名字不在这个音色里 ⇒ 404，并**把盘上真有的那几个列出来**（下一步该点哪个）。"""
    _voice_upload(client, tools, parts=[("a.wav", b"A"), ("b.wav", b"B")], ref_text="一\n二")

    resp = client.delete(f"{VOICE_SEGMENTS_URL.format(voice_id=VOICE)}/ref_09.wav")

    assert resp.status_code == 404
    body = resp.json()
    assert _error(body) == ErrorCode.ASSET_NOT_FOUND.value
    assert body["context"]["refs"] == ["ref_01.wav", "ref_02.wav"]


def test_voice_segment_delete_rejects_a_name_that_is_not_a_ref(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """``ref.txt`` / ``profile.json`` 不是参考音 ⇒ 422（逐段管理只能删段）。"""
    _voice_upload(client, tools, parts=[("a.wav", b"A"), ("b.wav", b"B")], ref_text="一\n二")

    resp = client.delete(f"{VOICE_SEGMENTS_URL.format(voice_id=VOICE)}/ref.txt")

    assert resp.status_code == 422
    assert _error(resp.json()) == ErrorCode.ASSET_INVALID.value
    assert (paths.voice_src_dir / VOICE / "ref.txt").is_file()


# ══════════════════════════════════════════════════════════════════════
# ④ 写 · 删除（裁定 369：删行 / 删文件是**两个**开关）
# ══════════════════════════════════════════════════════════════════════


def test_delete_removes_the_row_but_keeps_the_file_by_default(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """默认只删库里的行：盘上的文件原地不动（重扫一次就回来）。"""
    target = _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})

    resp = client.delete(f"{ASSETS_URL}/{BROLL}", params={"kind": "broll"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["kind"], body["id"], body["purge"], body["purged"]) == ("broll", BROLL, False, [])
    assert _rows(connection, "broll_clips") == []
    assert target.is_file()
    ops = _audit(connection, "asset.delete")
    assert len(ops) == 1
    assert ops[0]["target_id"] == BROLL
    assert ops[0]["target_type"] == "asset"
    assert json.loads(ops[0]["after_json"]) == {"kind": "broll", "purged": []}


def test_delete_with_purge_removes_the_voice_directory(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools
) -> None:
    """音色带上 ``purge`` ⇒ 参考音目录一起删。

    不删盘的话，下次扫盘这个目录又会变成一条"盘上有、库里没有" —— 用户刚删掉的
    东西自己回来了。``purged`` 回的是**真的删掉的那个路径**（绝对路径）。
    """
    root = _voice_dir(paths, tools)
    client.post(INGEST_URL, json={"license": "self_recorded"})

    resp = client.delete(f"{ASSETS_URL}/{VOICE}", params={"kind": "voice", "purge": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == [str(root.resolve())]
    assert _rows(connection, "voice_profiles") == []
    assert not root.exists()


def test_delete_unknown_id_is_404(client: TestClient) -> None:
    """id 打错 ⇒ 404，不是"删掉了"（否则面板会说成功而那一行还在）。"""
    resp = client.delete(f"{ASSETS_URL}/nothing_here", params={"kind": "broll"})
    assert resp.status_code == 404
    assert _error(resp.json()) == ErrorCode.ASSET_NOT_FOUND.value


def test_purge_refuses_a_path_outside_the_root(
    client: TestClient,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    tools: _Tools,
    tmp_path: Path,
) -> None:
    """库里那一行指到根目录**外面** ⇒ 一个字节都不动。

    行是能被人手改的、也可能从别的机器同步过来。删素材时信 ``row.path``，
    一次"清理素材库"就会变成删掉另一个文件。
    """
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not-an-asset")
    _clip(paths, tools)
    client.post(INGEST_URL, json={"license": "cc0"})
    connection.execute("UPDATE broll_clips SET path = ? WHERE id = ?", (str(outside), BROLL))

    resp = client.delete(f"{ASSETS_URL}/{BROLL}", params={"kind": "broll", "purge": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == []
    assert outside.is_file()
    assert _rows(connection, "broll_clips") == []


# ══════════════════════════════════════════════════════════════════════
# ⑦ 写 · 孤儿清理（裁定 384：盘上有、库里没有的东西**以前删不掉**）
# ══════════════════════════════════════════════════════════════════════


def test_prune_removes_an_unusable_orphan(client: TestClient, paths: StudioPaths) -> None:
    """盘上认得出、库里没有、**本身不合格**（空目录）⇒ 清掉。"""
    orphan = paths.voice_src_dir / "bigbear"
    orphan.mkdir(parents=True)

    resp = client.post(PRUNE_URL, json={"kind": "voice"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["id"] for item in body["removed"]] == ["bigbear"]
    assert body["removed"][0]["problems"]
    assert body["kept"] == []
    assert not orphan.exists()


def test_prune_keeps_a_usable_orphan_and_says_why(
    client: TestClient, paths: StudioPaths, tools: _Tools
) -> None:
    """★ 合格、只是还没入库的底片**一个字节都不动**，并说明它该入库。

    这条是这个动作最容易做错的地方：跑酷 / BGM 的未入库文件**出片照样挑得到**
    （``render/assets.py`` 只列目录），删了等于凭空少一条底片。而 ``check_broll``
    会把「授权没填」算进 ``problems`` —— 孤儿恰恰还没有库里那一行，而授权就存在
    那一行里。照着 ``check.ok`` 删，这条用例就会红。
    """
    target = _clip(paths, tools)

    resp = client.post(PRUNE_URL, json={"kind": "broll"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["removed"] == []
    assert [item["id"] for item in body["kept"]] == [BROLL]
    assert "该入库" in body["kept"][0]["reason"]
    assert target.is_file()


def test_prune_dry_run_writes_nothing(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """``dry_run=true`` ⇒ 只报会清掉哪些，**盘与库都不动**。"""
    orphan = paths.voice_src_dir / "bigbear"
    orphan.mkdir(parents=True)

    resp = client.post(PRUNE_URL, json={"kind": "voice", "dry_run": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert [item["id"] for item in body["removed"]] == ["bigbear"]
    assert orphan.exists()
    assert _audit(connection, "asset.prune") == []


def test_prune_writes_one_audit_row_for_the_batch(
    client: TestClient, connection: sqlite3.Connection, paths: StudioPaths
) -> None:
    """一次清理**一行**留痕：``target_id`` 是类别，逐条明细在 ``after`` 里。"""
    (paths.voice_src_dir / "bigbear").mkdir(parents=True)
    (paths.voice_src_dir / "littlebear").mkdir(parents=True)

    resp = client.post(PRUNE_URL, json={"kind": "voice"})

    assert resp.status_code == 200, resp.text
    ops = _audit(connection, "asset.prune")
    assert len(ops) == 1
    assert ops[0]["target_id"] == "voice"
    assert json.loads(ops[0]["after_json"])["removed"] == ["bigbear", "littlebear"]


def test_prune_reports_strays_without_touching_them(client: TestClient, paths: StudioPaths) -> None:
    """名字不合规的可能是用户自己的原始素材 —— 如实报出来，一个都不动。"""
    stray = paths.voice_src_dir / "跑酷素材.wav"
    stray.write_bytes(b"raw")

    resp = client.post(PRUNE_URL, json={"kind": "voice"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == []
    assert resp.json()["strays"] == [str(stray)]
    assert stray.is_file()


def test_prune_requires_a_kind(client: TestClient) -> None:
    """``kind`` 必填：这个动作会删盘上的东西，"删哪一类"必须由人说出来。"""
    assert client.post(PRUNE_URL, json={}).status_code == 422
