"""素材库服务（T4.8.4 · §3.3.14 / §4.3.1）。

扫盘 / 入库这一层要守的四条，每条都有对应用例：

1. **预览与入库同一条路径**（``dry_run`` 开关）⇒ 预览说能入库的，入库就得能进；
2. **缺授权不入库**（不替用户伪造 R2 留痕），但**要如实说清**是哪一条缺；
3. **坏文件不中断整批**（第 3 个文件坏了，其余 59 个照进）；
4. **启停与改字段必须留痕**（``audit_ops`` + 一条日志），且重复点同一下不留痕。

外部工具（ffprobe / ffmpeg / 指纹）全部注入假件：这些断言讲的是**服务的行为**，
不是"这台机器装没装 ffmpeg"。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.assets.layout import AssetKind
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import MediaInfo, VolumeStats
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.models import BgmTrackRow, BrollClipRow, VoiceProfileRow
from studio.db.repositories import IngestAction
from studio.db.repositories.audit_repo import AuditRepo
from studio.services.asset_service import AssetService, DisabledAssets, disabled_assets


class _Log:
    """假的日志出口（只记调用，不落库）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    @property
    def messages(self) -> list[str]:
        return [str(item["message"]) for item in self.calls]


class _Tools:
    """一套假的外部工具（探测 / 响度 / 缩略图 / 响度测量 / 指纹）。"""

    def __init__(self) -> None:
        self.info: dict[str, MediaInfo] = {}
        self.peak_db: float | None = -3.0
        self.loudness: float | None = -14.0
        self.thumbnail_error: bool = False
        self.loudness_error: bool = False
        self.digest_error: bool = False
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
        Path(target).write_bytes(b"jpeg")
        self.thumbnails.append(Path(target))
        return Path(target)

    def loudness_of(self, path: Path, **_: Any) -> float | None:
        if self.loudness_error:
            raise StudioError("量不出来", code=ErrorCode.MEDIA_PROBE_FAILED)
        return self.loudness

    def digest(self, path: Path, **_: Any) -> str:
        if self.digest_error:
            raise OSError("读不了")
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


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    value = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    value.ensure_runtime_dirs()
    return value


@pytest.fixture
def connection(paths: StudioPaths) -> Iterator[sqlite3.Connection]:
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def tools() -> _Tools:
    return _Tools()


@pytest.fixture
def log() -> _Log:
    return _Log()


@pytest.fixture
def service(connection: sqlite3.Connection, paths: StudioPaths, tools: _Tools, log: _Log) -> AssetService:
    return AssetService(
        connection,
        paths=paths,
        log=log,
        probe=tools.probe,
        volume=tools.volume,
        thumbnail=tools.thumbnail,
        loudness=tools.loudness_of,
        digest=tools.digest,
    )


def _touch(path: Path, body: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _clip(paths: StudioPaths, name: str = "parkour_001.mp4") -> Path:
    return _touch(paths.mc_parkour_dir / name)


def _track(paths: StudioPaths, name: str = "bgm_001.mp3") -> Path:
    return _touch(paths.bgm_dir / name)


def _voice(paths: StudioPaths, voice_id: str = "bigbear") -> Path:
    root = paths.voice_src_dir / voice_id
    _touch(root / "ref_01.wav")
    _touch(root / "ref_02.wav")
    _touch(root / "ref.txt", "第一句\n第二句\n")
    _touch(root / "profile.json", "{}")
    return root


class TestScanIsReadOnly:
    def test_missing_roots_are_reported(self, service: AssetService, paths: StudioPaths) -> None:
        for directory in (paths.mc_parkour_dir, paths.bgm_dir, paths.voice_src_dir):
            directory.rmdir()
        report = service.scan()
        assert report.dry_run is True
        assert all(item.root_missing for item in report.sections)
        assert report.to_dict()["totals"]["created"] == 0

    def test_scan_does_not_write_anything(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, connection: sqlite3.Connection
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        report = service.scan()
        section = report.section(AssetKind.BROLL)
        assert [item.id for item in section.assets] == ["parkour_001"]
        assert section.assets[0].stored is False
        assert section.assets[0].action is None
        # 预览不落库：库里还是空的
        assert connection.execute("SELECT COUNT(*) FROM broll_clips").fetchone()[0] == 0

    def test_unlicensed_clip_is_usable_only_with_a_license(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        without = service.scan().section(AssetKind.BROLL).assets[0]
        assert without.usable is False
        assert [item.code for item in without.check.problems] == ["license_missing"]

        with_license = service.scan(license="self_recorded").section(AssetKind.BROLL).assets[0]
        assert with_license.usable is True

    def test_short_clip_is_rejected_with_a_reason(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name, duration_ms=2_000)
        item = service.scan(license="cc0").section(AssetKind.BROLL).assets[0]
        assert item.usable is False
        assert "usable_too_short" in [problem.code for problem in item.check.problems]

    def test_unreadable_file_is_reported_not_raised(self, service: AssetService, paths: StudioPaths) -> None:
        _clip(paths)  # 探测表里没有它 ⇒ 假 probe 抛"解不开"
        item = service.scan(license="cc0").section(AssetKind.BROLL).assets[0]
        assert item.usable is False
        assert [problem.code for problem in item.check.problems] == ["media_undecodable"]


class TestIngest:
    def test_ingests_a_clip_with_machine_facts(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name, duration_ms=42_000)
        report = service.ingest(license="self_recorded")
        item = report.section(AssetKind.BROLL).assets[0]
        assert item.action is IngestAction.CREATED
        assert item.stored is True

        row = service.get(AssetKind.BROLL, "parkour_001")
        assert isinstance(row, BrollClipRow)
        assert row is not None
        assert (row.duration_ms, row.width, row.fps) == (42_000, 1920, 30.0)
        assert row.license == "self_recorded"
        assert row.enabled is True
        assert row.sha256 == tools.digest(target)
        assert row.thumb_path is not None and Path(row.thumb_path).is_file()
        assert row.thumb_path.startswith(paths.thumb_dir.as_posix())

    def test_ingest_is_idempotent(self, service: AssetService, paths: StudioPaths, tools: _Tools) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        assert service.ingest(license="cc0").total_created == 1
        second = service.ingest(license="cc0")
        assert second.total_created == 0
        assert second.section(AssetKind.BROLL).assets[0].action is IngestAction.UNCHANGED

    def test_rescan_does_not_overwrite_the_enabled_flag(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        service.set_enabled(AssetKind.BROLL, "parkour_001", False)

        tools.info[target.name] = _video(target.name, duration_ms=50_000)
        report = service.ingest()
        assert report.section(AssetKind.BROLL).assets[0].action is IngestAction.REFRESHED
        row = service.get(AssetKind.BROLL, "parkour_001")
        assert isinstance(row, BrollClipRow)
        assert row is not None
        assert row.enabled is False
        assert row.duration_ms == 50_000

    def test_duplicate_content_is_not_ingested_twice(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        first = _clip(paths, "parkour_001.mp4")
        second = _clip(paths, "parkour_002.mp4")
        tools.info[first.name] = _video(first.name)
        tools.info[second.name] = _video(second.name)
        tools.digests["parkour_002.mp4"] = tools.digests["parkour_001.mp4"] = "a" * 64

        report = service.ingest(license="cc0")
        actions = {item.id: item.action for item in report.section(AssetKind.BROLL).assets}
        assert actions["parkour_001"] is IngestAction.CREATED
        assert actions["parkour_002"] is IngestAction.DUPLICATE
        assert service.get(AssetKind.BROLL, "parkour_002") is None

    def test_new_asset_without_a_license_stays_out(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        report = service.ingest()
        item = report.section(AssetKind.BROLL).assets[0]
        assert item.action is None
        assert item.note is not None and "未入库" in item.note
        assert service.get(AssetKind.BROLL, "parkour_001") is None
        assert report.to_dict()["totals"]["rejected"] == 1

    def test_illegal_license_is_refused_before_any_write(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        with pytest.raises(StudioError) as excinfo:
            service.ingest(license="stolen")
        assert excinfo.value.code is ErrorCode.ASSET_INVALID
        assert service.get(AssetKind.BROLL, "parkour_001") is None

    def test_ids_filter_limits_the_scope(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        first = _clip(paths, "parkour_001.mp4")
        second = _clip(paths, "parkour_002.mp4")
        tools.info[first.name] = _video(first.name)
        tools.info[second.name] = _video(second.name)
        report = service.ingest(ids=["parkour_002"], license="cc0")
        assert [item.id for item in report.section(AssetKind.BROLL).assets] == ["parkour_002"]

    def test_one_bad_file_does_not_stop_the_batch(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        bad = _clip(paths, "parkour_001.mp4")
        good = _clip(paths, "parkour_002.mp4")
        tools.info[good.name] = _video(good.name)
        report = service.ingest(license="cc0")
        assert report.total_created == 1
        assert report.total_rejected == 1
        assert service.get(AssetKind.BROLL, "parkour_002") is not None
        assert service.get(AssetKind.BROLL, "parkour_001") is None
        assert bad.exists()  # 坏文件**原地不动**（只不入库，不删）

    def test_strays_and_missing_files_are_reported(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        stray = _touch(paths.mc_parkour_dir / "clip_001.mp4")
        service.ingest(license="cc0")

        # 盘上把文件搬走 ⇒ 库里那一行变成"素材不见了"
        target.unlink()
        section = service.scan().section(AssetKind.BROLL)
        assert [Path(item).name for item in section.strays] == [stray.name]
        assert section.missing == ("parkour_001",)

    def test_thumbnail_failure_does_not_block_ingest(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        tools.thumbnail_error = True
        report = service.ingest(license="cc0")
        item = report.section(AssetKind.BROLL).assets[0]
        assert item.action is IngestAction.CREATED
        assert item.note is not None and "缩略图" in item.note
        row = service.get(AssetKind.BROLL, "parkour_001")
        assert isinstance(row, BrollClipRow)
        assert row is not None and row.thumb_path is None

    def test_digest_failure_is_reported_per_item(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        tools.digest_error = True
        report = service.ingest(license="cc0")
        item = report.section(AssetKind.BROLL).assets[0]
        assert item.action is None
        assert item.note is not None and "入库失败" in item.note
        assert service.get(AssetKind.BROLL, "parkour_001") is None


class TestBgmAndVoice:
    def test_bgm_ingest_records_loudness(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _track(paths)
        tools.info[target.name] = _audio(target.name, duration_ms=120_000)
        report = service.ingest(license="purchased")
        assert report.section(AssetKind.BGM).assets[0].action is IngestAction.CREATED
        row = service.get(AssetKind.BGM, "bgm_001")
        assert isinstance(row, BgmTrackRow)
        assert row is not None
        assert row.loudness_lufs == pytest.approx(-14.0)
        assert (row.sample_rate, row.channels) == (44_100, 2)

    def test_bgm_loudness_failure_still_ingests(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _track(paths)
        tools.info[target.name] = _audio(target.name)
        tools.loudness_error = True
        item = service.ingest(license="cc0").section(AssetKind.BGM).assets[0]
        assert item.action is IngestAction.CREATED
        assert item.note is not None and "响度" in item.note
        row = service.get(AssetKind.BGM, "bgm_001")
        assert isinstance(row, BgmTrackRow)
        assert row is not None and row.loudness_lufs is None

    def test_short_bgm_is_rejected(self, service: AssetService, paths: StudioPaths, tools: _Tools) -> None:
        target = _track(paths)
        tools.info[target.name] = _audio(target.name, duration_ms=9_000)
        item = service.ingest(license="cc0").section(AssetKind.BGM).assets[0]
        assert item.usable is False
        assert [problem.code for problem in item.check.problems] == ["too_short"]

    def test_voice_needs_no_license_but_records_segments(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        root = _voice(paths)
        tools.info["ref_01.wav"] = _audio("ref_01.wav", duration_ms=12_000)
        tools.info["ref_02.wav"] = _audio("ref_02.wav", duration_ms=18_000, sample_rate=48_000)
        report = service.ingest()
        item = report.section(AssetKind.VOICE).assets[0]
        assert item.action is IngestAction.CREATED
        row = service.get(AssetKind.VOICE, "bigbear")
        assert isinstance(row, VoiceProfileRow)
        assert row is not None
        assert (row.ref_count, row.total_duration_ms) == (2, 30_000)
        assert row.sample_rate == 44_100  # 取各段里**最低**的那一段
        assert row.peak_db == pytest.approx(-3.0)
        assert row.license is None
        assert row.text_path == (root / "ref.txt").resolve().as_posix()
        assert row.proof_path == (root / "profile.json").resolve().as_posix()

    def test_voice_with_one_segment_is_rejected(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        root = paths.voice_src_dir / "bigbear"
        _touch(root / "ref_01.wav")
        tools.info["ref_01.wav"] = _audio("ref_01.wav", duration_ms=12_000)
        item = service.ingest().section(AssetKind.VOICE).assets[0]
        assert item.usable is False
        assert "too_few_refs" in [problem.code for problem in item.check.problems]


class TestWritesAndAudit:
    def _ingest_one(self, service: AssetService, paths: StudioPaths, tools: _Tools) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")

    def test_set_enabled_writes_an_audit_row(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, connection: sqlite3.Connection
    ) -> None:
        self._ingest_one(service, paths, tools)
        row = service.set_enabled(AssetKind.BROLL, "parkour_001", False)
        assert isinstance(row, BrollClipRow)
        assert row.enabled is False

        ops = AuditRepo(connection).list_recent(limit=10)
        latest = ops[0]
        assert latest.action == "asset.disable"
        assert latest.target_type == "asset"
        assert latest.target_id == "parkour_001"
        assert latest.before == {"kind": "broll", "enabled": True}
        assert latest.after == {"kind": "broll", "enabled": False}

    def test_repeating_the_same_toggle_does_not_pile_up_audit_rows(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, connection: sqlite3.Connection
    ) -> None:
        self._ingest_one(service, paths, tools)
        service.set_enabled(AssetKind.BROLL, "parkour_001", False)
        before = len(AuditRepo(connection).list_recent(limit=50))
        service.set_enabled(AssetKind.BROLL, "parkour_001", False)
        assert len(AuditRepo(connection).list_recent(limit=50)) == before

    def test_set_enabled_on_a_missing_asset_is_404(self, service: AssetService) -> None:
        with pytest.raises(StudioError) as excinfo:
            service.set_enabled(AssetKind.BROLL, "parkour_999", False)
        assert excinfo.value.code is ErrorCode.ASSET_NOT_FOUND

    def test_patch_records_before_and_after(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, connection: sqlite3.Connection
    ) -> None:
        self._ingest_one(service, paths, tools)
        row = service.patch(AssetKind.BROLL, "parkour_001", {"license": "authorized", "tags": ["night"]})
        assert isinstance(row, BrollClipRow)
        assert row.license == "authorized"
        assert row.tags == ["night"]
        latest = AuditRepo(connection).list_recent(limit=1)[0]
        assert latest.action == "asset.update"
        assert latest.before["license"] == "cc0"
        assert latest.after["license"] == "authorized"

    def test_patch_rejects_an_unknown_field(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        self._ingest_one(service, paths, tools)
        with pytest.raises(StudioError) as excinfo:
            service.patch(AssetKind.BROLL, "parkour_001", {"sha256": "0" * 64})
        assert excinfo.value.code is ErrorCode.ASSET_INVALID

    def test_patch_rejects_an_empty_body(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        self._ingest_one(service, paths, tools)
        with pytest.raises(StudioError) as excinfo:
            service.patch(AssetKind.BROLL, "parkour_001", {})
        assert excinfo.value.code is ErrorCode.ASSET_INVALID

    def test_patch_rejects_an_empty_usable_window(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        self._ingest_one(service, paths, tools)
        with pytest.raises(StudioError) as excinfo:
            service.patch(
                AssetKind.BROLL,
                "parkour_001",
                {"usable_from_ms": 20_000, "usable_to_ms": 10_000},
            )
        assert excinfo.value.code is ErrorCode.ASSET_INVALID

    def test_patch_can_clear_the_voice_license_but_not_the_clip_license(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        self._ingest_one(service, paths, tools)
        with pytest.raises(StudioError) as excinfo:
            service.patch(AssetKind.BROLL, "parkour_001", {"license": None})
        assert excinfo.value.code is ErrorCode.ASSET_INVALID

        _voice(paths)
        tools.info["ref_01.wav"] = _audio("ref_01.wav", duration_ms=12_000)
        tools.info["ref_02.wav"] = _audio("ref_02.wav", duration_ms=18_000)
        service.ingest()
        row = service.patch(AssetKind.VOICE, "bigbear", {"license": None})
        assert isinstance(row, VoiceProfileRow)
        assert row.license is None

    def test_patch_routes_enabled_through_the_enable_path(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, connection: sqlite3.Connection
    ) -> None:
        self._ingest_one(service, paths, tools)
        row = service.patch(AssetKind.BROLL, "parkour_001", {"enabled": False})
        assert isinstance(row, BrollClipRow)
        assert row.enabled is False
        assert AuditRepo(connection).list_recent(limit=1)[0].action == "asset.disable"

    def test_patch_on_a_missing_asset_is_404(self, service: AssetService) -> None:
        with pytest.raises(StudioError) as excinfo:
            service.patch(AssetKind.BROLL, "parkour_999", {"license": "cc0"})
        assert excinfo.value.code is ErrorCode.ASSET_NOT_FOUND

    def test_ingest_writes_one_log_line(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, log: _Log
    ) -> None:
        self._ingest_one(service, paths, tools)
        assert len(log.calls) == 1
        assert log.calls[0]["source"] == "assets"
        assert "素材入库" in log.calls[0]["message"]

    def test_enable_writes_a_log_line(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, log: _Log
    ) -> None:
        self._ingest_one(service, paths, tools)
        service.set_enabled(AssetKind.BROLL, "parkour_001", False)
        assert any("素材停用" in item or "素材启用" in item for item in log.messages)


class TestResolve:
    def test_unique_id_resolves_to_its_kind(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        found = service.resolve("parkour_001")
        assert found is not None and found[0] is AssetKind.BROLL

    def test_unknown_id_resolves_to_none(self, service: AssetService) -> None:
        assert service.resolve("ghost") is None

    def test_ambiguous_id_is_refused(self, service: AssetService, paths: StudioPaths, tools: _Tools) -> None:
        # 一个音色目录恰好也叫 bgm_001：两类都有这一行 ⇒ 拒绝猜
        _touch(paths.voice_src_dir / "bgm_001" / "ref_01.wav")
        target = _track(paths, "bgm_001.mp3")
        tools.info[target.name] = _audio(target.name)
        tools.info["ref_01.wav"] = _audio("ref_01.wav", duration_ms=12_000)
        service.ingest(license="cc0")
        service._voice.upsert(
            voice_id="bgm_001", path=paths.voice_src_dir / "bgm_001", ref_count=1, total_duration_ms=1
        )
        with pytest.raises(StudioError) as excinfo:
            service.resolve("bgm_001")
        assert excinfo.value.code is ErrorCode.ASSET_INVALID


class TestLibrary:
    def test_empty_library_is_degraded(self, service: AssetService) -> None:
        snapshot = service.library()
        assert snapshot.degraded is True
        assert snapshot.note is not None and "黑屏降级" in snapshot.note
        broll = snapshot.section(AssetKind.BROLL)
        assert broll.stats.total == 0
        assert broll.shortfall is not None and "60" in broll.shortfall

    def test_one_clip_is_enough_to_render_but_still_short(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        """★ 1 条素材 ⇒ **不算降级**（出片真的会挑到它），但仍然"不够多"。

        这两件事曾经合成一个 ``degraded``，后果是面板上写着"当前为黑屏降级模式"，
        而片子里正放着这条跑酷 —— 同一句谎话换了个说法。现在 ``degraded`` 只回答
        "会不会真的黑屏"，"够不够多"归 ``shortfall``。
        """
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        snapshot = service.library()
        assert snapshot.degraded is False
        assert snapshot.note is None
        broll = snapshot.section(AssetKind.BROLL)
        assert broll.stats.enabled == 1
        assert broll.usable == 1
        assert broll.shortfall is not None and "60" in broll.shortfall

    def test_sections_serialize_with_stats_and_items(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        payload = service.library().to_dict()
        kinds = [item["kind"] for item in payload["sections"]]
        assert kinds == ["broll", "voice", "bgm"]
        broll = payload["sections"][0]
        assert broll["items"][0]["id"] == "parkour_001"
        assert broll["items"][0]["license"] == "cc0"
        assert broll["items"][0]["on_disk"] is True
        assert broll["stats"]["enabled"] == 1
        # 盘 / 库两组数字分开报：入了库的 1 条也就是盘上那 1 条，所以没有 pending
        assert broll["disk_total"] == 1
        assert broll["usable"] == 1
        assert broll["pending"] == []
        assert broll["strays"] == []
        assert broll["root_missing"] is False
        assert payload["degraded"] is False

    def test_pending_lists_what_the_renderer_will_still_pick(
        self, service: AssetService, paths: StudioPaths
    ) -> None:
        """★ 盘上有、库里没有 ⇒ 报成 ``pending``，而且**算进 usable**。

        这是本屏最重要的一条口径：出片挑素材走的是 ``render/assets.py``，它只列目录、
        不读库（"能进目录就默认可用"）。所以"还没入库"**不等于**"挑不到" ——
        面板把盘上 58 条说成"0 条"，用户从此不再相信这一屏上的任何数字。
        """
        for index in range(3):
            _clip(paths, f"parkour_{index:03d}.mp4")
        broll = service.library().section(AssetKind.BROLL)
        assert broll.stats.total == 0
        assert broll.disk_total == 3
        assert broll.usable == 3
        assert [item.id for item in broll.pending] == [
            "parkour_000",
            "parkour_001",
            "parkour_002",
        ]
        assert broll.pending[0].to_dict() == {
            "kind": "broll",
            "id": "parkour_000",
            "path": str(paths.mc_parkour_dir / "parkour_000.mp4"),
        }

    def test_ingested_files_are_not_pending_any_more(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        broll = service.library().section(AssetKind.BROLL)
        assert broll.pending == ()
        assert broll.disk_total == 1
        assert broll.usable == 1

    def test_degraded_means_black_screen_not_just_short(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        """★ ``degraded`` 只回答"出片会不会真的黑屏"，不回答"够不够多"。"""
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        assert service.library().degraded is False

        service.set_enabled(AssetKind.BROLL, "parkour_001", False)
        snapshot = service.library()
        assert snapshot.degraded is True
        assert snapshot.note is not None and "黑屏降级" in snapshot.note
        assert snapshot.section(AssetKind.BROLL).usable == 0

    def test_disabling_drops_the_clip_from_usable(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        """★ 停用 ⇒ 出片挑不到它，面板上的"能挑到几条"跟着减一（T4.8 验收）。"""
        for index in (1, 2):
            target = _clip(paths, f"parkour_{index:03d}.mp4")
            tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        assert service.library().section(AssetKind.BROLL).usable == 2
        service.set_enabled(AssetKind.BROLL, "parkour_001", False)
        assert service.library().section(AssetKind.BROLL).usable == 1

    def test_on_disk_goes_false_when_the_file_is_gone(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        """★ 文件被挪走之后行还在、开关还是绿的，而出片再也挑不到它 —— 分开显示。"""
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        assert service.library().to_dict()["sections"][0]["items"][0]["on_disk"] is True
        target.unlink()
        snapshot = service.library()
        assert snapshot.to_dict()["sections"][0]["items"][0]["on_disk"] is False
        assert snapshot.section(AssetKind.BROLL).usable == 0
        assert snapshot.degraded is True

    def test_voice_usable_comes_from_the_table_not_the_disk(
        self, service: AssetService, paths: StudioPaths, tools: _Tools
    ) -> None:
        """★ 音色与跑酷**口径不同**，而且这个不同是如实的。

        配音只认 ``voice_profiles``：盘上一个还没入库的音色目录挑不了（得先知道参考音
        有几段、逐字文本对不对）。把它写成和跑酷一样，面板就会开始说第二种谎。
        """
        _voice(paths, "bigbear")
        section = service.library().section(AssetKind.VOICE)
        assert section.disk_total == 1
        assert section.pending[0].id == "bigbear"
        assert section.usable == 0
        assert section.shortfall is not None and "配音无法开始" in section.shortfall

    def test_broll_shortfall_counts_what_the_renderer_can_pick(
        self, service: AssetService, paths: StudioPaths
    ) -> None:
        """一条没入库 ⇒ 缺口那句话说的也必须是"能挑到几条"，不是"库里有几条"。"""
        for index in range(2):
            _clip(paths, f"parkour_{index:03d}.mp4")
        section = service.library().section(AssetKind.BROLL)
        assert section.shortfall is not None and "只有 2 条" in section.shortfall


class TestDisabledAssets:
    """库 → 渲染器的**唯一**一样东西：一串文件名（见 ``render/assets.py``）。"""

    def test_returns_file_names_of_disabled_rows(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, connection: sqlite3.Connection
    ) -> None:
        for index in (1, 2):
            target = _clip(paths, f"parkour_{index:03d}.mp4")
            tools.info[target.name] = _video(target.name)
        track = _track(paths)
        tools.info[track.name] = _audio(track.name)
        service.ingest(license="cc0")

        assert disabled_assets(connection) == DisabledAssets()
        service.set_enabled(AssetKind.BROLL, "parkour_001", False)
        service.set_enabled(AssetKind.BGM, "bgm_001", False)

        found = disabled_assets(connection)
        assert found.clips == frozenset({"parkour_001.mp4"})
        assert found.bgm == frozenset({"bgm_001.mp3"})
        assert found.for_kind(AssetKind.BROLL) == frozenset({"parkour_001.mp4"})
        assert found.for_kind(AssetKind.VOICE) == frozenset()

    def test_uses_the_file_name_not_the_stored_path(
        self, service: AssetService, paths: StudioPaths, tools: _Tools, connection: sqlite3.Connection
    ) -> None:
        """库里存的是绝对路径（换台机器前缀就不同），渲染器只认目录 ⇒ 只能对文件名。"""
        target = _clip(paths)
        tools.info[target.name] = _video(target.name)
        service.ingest(license="cc0")
        service.set_enabled(AssetKind.BROLL, "parkour_001", False)
        assert disabled_assets(connection).clips == frozenset({"parkour_001.mp4"})

    def test_unreadable_table_means_nothing_is_excluded(self, tmp_path: Path) -> None:
        """读不出停用名单 ⇒ 空集合，**不抛**：查不到不该让出片失败。"""
        empty = connect(tmp_path / "empty.db")
        try:
            assert disabled_assets(empty) == DisabledAssets()
        finally:
            empty.close()

    def test_bgm_without_tracks_says_what_happens(self, service: AssetService) -> None:
        section = service.library().section(AssetKind.BGM)
        assert section.shortfall is not None and "单轨人声" in section.shortfall

    def test_voice_shortfall_does_not_talk_about_the_picture(self, service: AssetService) -> None:
        """★ 音色不进画面 —— 缺口那句话不能说"出片挑不到"。

        同一条 ``usable``，对用户却是两件事：跑酷/BGM 是出片挑素材，音色是配音挑音色。
        说成"出片一条都挑不到"会让人以为音色是拿去当底片的。
        """
        section = service.library().section(AssetKind.VOICE)
        assert section.shortfall is not None
        assert "配音一条都用不了" in section.shortfall
        assert "配音无法开始" in section.shortfall
        assert "出片" not in section.shortfall
