"""R2 来源登记留档（T5.5 · §06.11 / §06.12）。

这一条钉住三件事
----------------
① **三样素材各扫一遍**：音色（``voice_profiles.proof_path``）、BGM
   （``bgm_tracks.proof_path``）、跑酷素材（``broll_clips.source_url``）。少扫一样，
   面板上那个「缺一份」的数字就会偏小 —— 而偏小的数字比没有数字更糟；
② **缺口逐条列出来**，每条都点得出是谁、缺的是什么；
③ **常驻提示要把三件事说全**，否则「常驻」就只是一行谁都不会细看的字。

为什么用真库（临时目录里迁移出来的那一份）
------------------------------------------
这一层读的就是那三张表。换成假仓储，验的就不再是「库里的空值会不会被看见」，
而是「我假件写对了没」—— 而前者正是它唯一的价值。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories.asset_repo import BgmTrackRepo, BrollClipRepo, VoiceProfileRepo
from studio.publish.compliance import R2_NOTICE, compliance_snapshot


@pytest.fixture
def paths(tmp_path: Path) -> StudioPaths:
    return StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """临时库（真迁移跑一遍）—— 这一层要验的正是「库里的空值会不会被看见」。"""
    database = tmp_path / "studio.db"
    migrate(database)
    value = connect(database)
    try:
        yield value
    finally:
        value.close()


def _sha(value: str) -> str:
    """每条素材一个**各不相同**的 sha256。

    跑酷 / BGM 的 ``upsert`` 按 sha256 去重（同一条素材重复入库 ⇒ 跳过），
    所以夹具里不能拿一个常量当哈希 —— 那会让第二条素材静默不入库，
    而断言失败的样子是「KeyError」，看起来像查询写错了。
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _voice(connection: sqlite3.Connection, voice_id: str, *, proof: str | None) -> None:
    VoiceProfileRepo(connection).upsert(
        voice_id=voice_id,
        path=f"data/voice_src/{voice_id}/ref.wav",
        ref_count=1,
        total_duration_ms=1_000,
        proof_path=proof,
        license="self_recorded",
    )


def _broll(connection: sqlite3.Connection, clip_id: str, *, source: str | None) -> None:
    repo = BrollClipRepo(connection)
    repo.upsert(
        clip_id=clip_id,
        path=f"data/broll/{clip_id}.mp4",
        sha256=_sha(clip_id),
        duration_ms=10_000,
        license="authorized",
    )
    if source is not None:
        repo.patch(clip_id, source_url=source)


def _bgm(connection: sqlite3.Connection, track_id: str, *, proof: str | None) -> None:
    repo = BgmTrackRepo(connection)
    repo.upsert(
        track_id=track_id,
        path=f"data/bgm/{track_id}.mp3",
        sha256=_sha(track_id),
        duration_ms=30_000,
        license="purchased",
    )
    if proof is not None:
        repo.patch(track_id, proof_path=proof)


def test_empty_library_is_compliant(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """一件素材都没有 ⇒ 没有缺口。

    这条不是废话：它把「空库」与「有素材但没登记」分开。合并之后，出厂状态的发布面板
    会顶着一片红，而那时它其实**什么都没做错**。
    """
    snapshot = compliance_snapshot(connection, paths=paths)

    assert snapshot.ok is True
    assert snapshot.gaps == ()
    assert snapshot.items == ()
    assert snapshot.notice == R2_NOTICE


def test_missing_proof_is_listed_per_item(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """三样各缺一样 ⇒ 三条缺口，且每条都点得出是谁。"""
    _voice(connection, "voice_a", proof=None)
    _broll(connection, "clip_a", source=None)
    _bgm(connection, "track_a", proof=None)

    snapshot = compliance_snapshot(connection, paths=paths)

    assert snapshot.ok is False
    assert len(snapshot.gaps) == 3
    assert [item.kind for item in snapshot.items] == ["voice", "broll", "bgm"]
    assert all(item.proof_present is False for item in snapshot.items)
    assert all(item.note for item in snapshot.items), "缺口那一行必须写清「怎么补」"


def test_gap_wording_names_the_kind(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """缺口那句话里要有量词（音色 / 跑酷素材 / BGM）—— 面板上「缺了什么」得读得出来。"""
    _voice(connection, "voice_a", proof=None)
    _broll(connection, "clip_a", source=None)
    _bgm(connection, "track_a", proof=None)

    text = "\n".join(compliance_snapshot(connection, paths=paths).gaps)

    assert "音色 voice_a" in text
    assert "跑酷素材 clip_a" in text
    assert "BGM track_a" in text


def test_registered_proof_is_not_a_gap(
    connection: sqlite3.Connection, paths: StudioPaths, tmp_path: Path
) -> None:
    """音色与 BGM 的留档是**盘上那个文件**：在，就不算缺口。"""
    proof = tmp_path / "授权说明.txt"
    proof.write_text("自制", encoding="utf-8")
    _voice(connection, "voice_a", proof=str(proof))
    _bgm(connection, "track_a", proof=str(proof))

    snapshot = compliance_snapshot(connection, paths=paths)

    assert snapshot.ok is True
    assert snapshot.gaps == ()
    assert {item.id for item in snapshot.items} == {"voice_a", "track_a"}
    assert all(item.proof_present for item in snapshot.items)


def test_proof_path_pointing_at_nothing_is_a_gap(
    connection: sqlite3.Connection, paths: StudioPaths, tmp_path: Path
) -> None:
    """``proof_path`` 写了但文件不在了 ⇒ **仍然算缺口**。

    这是这一层最容易漏的一条：库里那一列非空就判「登记过」，等于把「文件被挪走了」
    读成「登记齐了」—— 而留档的意义恰恰是**三个月后还能拿出来**。
    """
    _voice(connection, "voice_a", proof=str(tmp_path / "早就没了.txt"))

    snapshot = compliance_snapshot(connection, paths=paths)

    assert snapshot.ok is False
    assert len(snapshot.gaps) == 1
    assert "voice_a" in snapshot.gaps[0]


def test_broll_source_url_is_the_proof(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """跑酷素材的留档是 ``source_url``（``license`` 是必填列，缺不了）。"""
    _broll(connection, "clip_ok", source="https://example.com/v/1")
    _broll(connection, "clip_bad", source=None)

    snapshot = compliance_snapshot(connection, paths=paths)

    by_id = {item.id: item for item in snapshot.items}
    assert by_id["clip_ok"].proof_present is True
    assert by_id["clip_ok"].proof == "https://example.com/v/1"
    assert by_id["clip_bad"].proof_present is False
    assert snapshot.ok is False
    assert len(snapshot.gaps) == 1
    assert "clip_bad" in snapshot.gaps[0]


def test_notice_covers_the_three_things(connection: sqlite3.Connection, paths: StudioPaths) -> None:
    """常驻提示必须说全：声音权 + 著作权 + 三件事 + 一句「是否可商用请自行确认」。"""
    notice = compliance_snapshot(connection, paths=paths).notice

    assert "声音权" in notice
    assert "著作权" in notice
    assert "自行确认" in notice
