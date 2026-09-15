"""素材库仓储（T4.8.3 · §3.3.14 / §4.3.1）。

这一层最值钱的断言只有一条：**重扫不许覆盖人做过的决定**。
"停用一条素材"是人的动作，而扫盘是每天都会跑的机器动作 —— 如果 ``upsert``
顺手把 ``enabled`` 刷回 1，用户的停用会在第二天早上静默失效（陷阱 #188）。
所以下面每个"刷新"用例都在刷新前后对同一行做人填字段的对照断言。

用真实库（临时目录 + 迁移）而不是 mock 连接：这里测的就是 SQL 本身。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import (
    BgmTrackRepo,
    BrollClipRepo,
    IngestAction,
    VoiceProfileRepo,
)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """迁移好的临时库（用完关掉，否则 Windows 上临时目录删不干净）。"""
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def clip_repo(connection: sqlite3.Connection) -> BrollClipRepo:
    return BrollClipRepo(connection)


@pytest.fixture
def track_repo(connection: sqlite3.Connection) -> BgmTrackRepo:
    return BgmTrackRepo(connection)


@pytest.fixture
def voice_repo(connection: sqlite3.Connection) -> VoiceProfileRepo:
    return VoiceProfileRepo(connection)


def _clip(path: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "clip_id": "parkour_001",
        "path": path,
        "sha256": "a" * 64,
        "duration_ms": 30_000,
        "license": "self_recorded",
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
    }
    payload.update(overrides)
    return payload


def _track(path: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "track_id": "bgm_001",
        "path": path,
        "sha256": "b" * 64,
        "duration_ms": 120_000,
        "license": "cc0",
        "sample_rate": 44_100,
        "channels": 2,
        "bitrate_kbps": 320,
    }
    payload.update(overrides)
    return payload


class TestBrollClipRepo:
    def test_created_then_unchanged(self, clip_repo: BrollClipRepo, tmp_path: Path) -> None:
        target = tmp_path / "parkour_001.mp4"
        first = clip_repo.upsert(**_clip(target))
        assert first.action is IngestAction.CREATED
        assert first.stored is True

        second = clip_repo.upsert(**_clip(target))
        assert second.action is IngestAction.UNCHANGED

    def test_duplicate_content_is_rejected(self, clip_repo: BrollClipRepo, tmp_path: Path) -> None:
        clip_repo.upsert(**_clip(tmp_path / "parkour_001.mp4"))
        result = clip_repo.upsert(**_clip(tmp_path / "parkour_002.mp4", clip_id="parkour_002"))
        assert result.action is IngestAction.DUPLICATE
        assert result.stored is False
        assert "parkour_001" in (result.reason or "")
        assert clip_repo.get("parkour_002") is None

    def test_same_content_under_the_same_id_is_not_a_duplicate(
        self, clip_repo: BrollClipRepo, tmp_path: Path
    ) -> None:
        target = tmp_path / "parkour_001.mp4"
        clip_repo.upsert(**_clip(target))
        assert clip_repo.upsert(**_clip(target)).action is IngestAction.UNCHANGED

    def test_refresh_updates_machine_facts_only(self, clip_repo: BrollClipRepo, tmp_path: Path) -> None:
        target = tmp_path / "parkour_001.mp4"
        clip_repo.upsert(**_clip(target))
        # 人做的三件事：停用、改授权、标可用区间
        clip_repo.set_enabled("parkour_001", False)
        clip_repo._connection.execute(
            "UPDATE broll_clips SET license = 'authorized', usable_from_ms = 1500, "
            "usable_to_ms = 28000, has_text = 1, tags_json = ? WHERE id = 'parkour_001'",
            (json.dumps(["night"]),),
        )

        result = clip_repo.upsert(
            **_clip(target, sha256="c" * 64, duration_ms=31_000, width=1280, height=720, fps=25.0)
        )
        assert result.action is IngestAction.REFRESHED

        row = clip_repo.get("parkour_001")
        assert row is not None
        assert (row.sha256, row.duration_ms, row.width, row.fps) == ("c" * 64, 31_000, 1280, 25.0)
        # ↓ 人填的一个都没被动过
        assert row.enabled is False
        assert row.license == "authorized"
        assert (row.usable_from_ms, row.usable_to_ms) == (1500, 28_000)
        assert row.has_text is True
        assert row.tags == ["night"]

    def test_refresh_keeps_the_existing_thumbnail_when_generation_failed(
        self, clip_repo: BrollClipRepo, tmp_path: Path
    ) -> None:
        target = tmp_path / "parkour_001.mp4"
        thumb = tmp_path / "thumbs" / "parkour_001.jpg"
        clip_repo.upsert(**_clip(target, thumb_path=thumb))
        clip_repo.upsert(**_clip(target, sha256="d" * 64, thumb_path=None))
        row = clip_repo.get("parkour_001")
        assert row is not None
        assert row.thumb_path == thumb.resolve().as_posix()

    def test_path_spelling_does_not_create_a_second_row(
        self, clip_repo: BrollClipRepo, tmp_path: Path
    ) -> None:
        target = tmp_path / "parkour_001.mp4"
        clip_repo.upsert(**_clip(target))
        messy = tmp_path / "nested" / ".." / "parkour_001.mp4"
        result = clip_repo.upsert(**_clip(messy))
        assert result.action is IngestAction.UNCHANGED
        assert len(clip_repo.list_all()) == 1
        assert clip_repo.get("parkour_001").path == target.resolve().as_posix()  # type: ignore[union-attr]

    def test_enabled_toggle_and_missing_id(self, clip_repo: BrollClipRepo, tmp_path: Path) -> None:
        clip_repo.upsert(**_clip(tmp_path / "parkour_001.mp4"))
        assert clip_repo.set_enabled("parkour_001", False) is True
        assert clip_repo.set_enabled("parkour_999", False) is False
        assert clip_repo.list_all(enabled_only=True) == []
        assert len(clip_repo.list_all()) == 1
        assert clip_repo.set_enabled("parkour_001", True) is True
        assert len(clip_repo.list_all(enabled_only=True)) == 1

    def test_stats_separate_all_from_enabled(self, clip_repo: BrollClipRepo, tmp_path: Path) -> None:
        clip_repo.upsert(**_clip(tmp_path / "parkour_001.mp4", duration_ms=10_000))
        clip_repo.upsert(
            **_clip(
                tmp_path / "parkour_002.mp4",
                clip_id="parkour_002",
                sha256="e" * 64,
                duration_ms=20_000,
            )
        )
        clip_repo.set_enabled("parkour_002", False)
        stats = clip_repo.stats()
        assert (stats.total, stats.enabled) == (2, 1)
        assert (stats.total_duration_ms, stats.enabled_duration_ms) == (30_000, 10_000)
        assert stats.to_dict()["enabled"] == 1

    def test_usable_window_helpers(self, clip_repo: BrollClipRepo, tmp_path: Path) -> None:
        clip_repo.upsert(**_clip(tmp_path / "parkour_001.mp4"))
        row = clip_repo.get("parkour_001")
        assert row is not None
        assert row.usable_to_effective == 30_000
        assert row.usable_ms == 30_000
        clip_repo._connection.execute(
            "UPDATE broll_clips SET usable_from_ms = 2000, usable_to_ms = 12000 WHERE id = ?",
            ("parkour_001",),
        )
        trimmed = clip_repo.get("parkour_001")
        assert trimmed is not None
        assert (trimmed.usable_to_effective, trimmed.usable_ms) == (12_000, 10_000)

    def test_find_by_sha256(self, clip_repo: BrollClipRepo, tmp_path: Path) -> None:
        clip_repo.upsert(**_clip(tmp_path / "parkour_001.mp4"))
        found = clip_repo.find_by_sha256("a" * 64)
        assert found is not None and found.id == "parkour_001"
        assert clip_repo.find_by_sha256("f" * 64) is None

    def test_tags_round_trip(self, clip_repo: BrollClipRepo, tmp_path: Path) -> None:
        clip_repo.upsert(**_clip(tmp_path / "parkour_001.mp4", tags=["day", "forest"]))
        row = clip_repo.get("parkour_001")
        assert row is not None
        assert row.tags == ["day", "forest"]


class TestBgmTrackRepo:
    def test_created_then_unchanged(self, track_repo: BgmTrackRepo, tmp_path: Path) -> None:
        target = tmp_path / "bgm_001.mp3"
        assert track_repo.upsert(**_track(target)).action is IngestAction.CREATED
        assert track_repo.upsert(**_track(target)).action is IngestAction.UNCHANGED

    def test_duplicate_content_is_rejected(self, track_repo: BgmTrackRepo, tmp_path: Path) -> None:
        track_repo.upsert(**_track(tmp_path / "bgm_001.mp3"))
        result = track_repo.upsert(**_track(tmp_path / "bgm_002.mp3", track_id="bgm_002"))
        assert result.action is IngestAction.DUPLICATE
        assert track_repo.get("bgm_002") is None

    def test_refresh_keeps_human_fields(self, track_repo: BgmTrackRepo, tmp_path: Path) -> None:
        target = tmp_path / "bgm_001.mp3"
        track_repo.upsert(**_track(target, loudness_lufs=-14.0))
        track_repo.set_enabled("bgm_001", False)
        track_repo._connection.execute(
            "UPDATE bgm_tracks SET mood = 'upbeat', bpm = 128.0, loopable = 1, "
            "tags_json = ?, license = 'purchased' WHERE id = 'bgm_001'",
            (json.dumps(["sport"]),),
        )

        result = track_repo.upsert(**_track(target, sha256="f" * 64, duration_ms=90_000))
        assert result.action is IngestAction.REFRESHED
        row = track_repo.get("bgm_001")
        assert row is not None
        assert (row.sha256, row.duration_ms) == ("f" * 64, 90_000)
        assert row.enabled is False
        assert (row.mood, row.bpm, row.loopable) == ("upbeat", 128.0, True)
        assert row.tags == ["sport"]
        assert row.license == "purchased"
        # 响度这次没量出来（None）⇒ 保留上一次量到的值
        assert row.loudness_lufs == pytest.approx(-14.0)

    def test_loudness_is_written_on_insert_and_refresh(
        self, track_repo: BgmTrackRepo, tmp_path: Path
    ) -> None:
        target = tmp_path / "bgm_001.mp3"
        track_repo.upsert(**_track(target, loudness_lufs=-18.5))
        row = track_repo.get("bgm_001")
        assert row is not None and row.loudness_lufs == pytest.approx(-18.5)
        track_repo.upsert(**_track(target, sha256="f" * 64, loudness_lufs=-12.0))
        refreshed = track_repo.get("bgm_001")
        assert refreshed is not None and refreshed.loudness_lufs == pytest.approx(-12.0)

    def test_enabled_and_stats(self, track_repo: BgmTrackRepo, tmp_path: Path) -> None:
        track_repo.upsert(**_track(tmp_path / "bgm_001.mp3", duration_ms=60_000))
        assert track_repo.set_enabled("bgm_999", True) is False
        stats = track_repo.stats()
        assert (stats.total, stats.enabled, stats.enabled_duration_ms) == (1, 1, 60_000)
        track_repo.set_enabled("bgm_001", False)
        after = track_repo.stats()
        assert (after.total, after.enabled, after.enabled_duration_ms) == (1, 0, 0)
        assert track_repo.list_all(enabled_only=True) == []


class TestVoiceProfileRepo:
    def test_created_then_unchanged(self, voice_repo: VoiceProfileRepo, tmp_path: Path) -> None:
        root = tmp_path / "voice_src" / "bigbear"
        payload: dict[str, Any] = {
            "voice_id": "bigbear",
            "path": root,
            "ref_count": 2,
            "total_duration_ms": 30_000,
            "sample_rate": 44_100,
            "peak_db": -3.5,
            "text_path": root / "ref.txt",
            "proof_path": root / "profile.json",
        }
        assert voice_repo.upsert(**payload).action is IngestAction.CREATED
        assert voice_repo.upsert(**payload).action is IngestAction.UNCHANGED

    def test_refresh_updates_facts_and_keeps_the_human_license(
        self, voice_repo: VoiceProfileRepo, tmp_path: Path
    ) -> None:
        root = tmp_path / "voice_src" / "bigbear"
        voice_repo.upsert(voice_id="bigbear", path=root, ref_count=2, total_duration_ms=30_000, license="cc0")
        voice_repo.set_enabled("bigbear", False)
        result = voice_repo.upsert(voice_id="bigbear", path=root, ref_count=3, total_duration_ms=45_000)
        assert result.action is IngestAction.REFRESHED
        row = voice_repo.get("bigbear")
        assert row is not None
        assert (row.ref_count, row.total_duration_ms) == (3, 45_000)
        assert row.enabled is False
        assert row.license == "cc0"

    def test_license_may_stay_empty(self, voice_repo: VoiceProfileRepo, tmp_path: Path) -> None:
        root = tmp_path / "voice_src" / "bigbear"
        voice_repo.upsert(voice_id="bigbear", path=root, ref_count=2, total_duration_ms=30_000)
        row = voice_repo.get("bigbear")
        assert row is not None and row.license is None

    def test_two_voices_are_independent(self, voice_repo: VoiceProfileRepo, tmp_path: Path) -> None:
        root = tmp_path / "voice_src"
        voice_repo.upsert(voice_id="bigbear", path=root / "bigbear", ref_count=2, total_duration_ms=20_000)
        voice_repo.upsert(voice_id="xiongda", path=root / "xiongda", ref_count=3, total_duration_ms=30_000)
        stats = voice_repo.stats()
        assert (stats.total, stats.enabled, stats.total_duration_ms) == (2, 2, 50_000)
        assert [row.id for row in voice_repo.list_all()] == ["bigbear", "xiongda"]

    def test_set_enabled_reports_a_missing_id(self, voice_repo: VoiceProfileRepo, tmp_path: Path) -> None:
        assert voice_repo.set_enabled("ghost", False) is False
        voice_repo.upsert(
            voice_id="bigbear", path=tmp_path / "bigbear", ref_count=2, total_duration_ms=20_000
        )
        assert voice_repo.set_enabled("bigbear", False) is True
        row = voice_repo.get("bigbear")
        assert row is not None and row.enabled is False
