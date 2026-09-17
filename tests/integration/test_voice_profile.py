"""参考音入库与音色注册（T2.4 验收 · §04.3.1 / R2）。

这一条验的是「我录的那几段，到底能不能用」的**首尾两端**
--------------------------------------------------------
前端：``data/voice_src/<id>/`` 里真的 wav；后端：``voice_profiles`` 表里的那一行，
以及**配音面板上真的选得到它**。中间那层判定不重写、不绕过 —— 本文件走的是
``AssetService.ingest``，与素材库面板点「扫描并入库」是**同一条代码路径**（所以
「命令行说能进、面板说不能」结构上不会发生）。

为什么用真 ffprobe / 真 ffmpeg
------------------------------
阈值是「单段 10-30 秒」「采样率 ≥ 16 kHz」「峰值 ≤ -1.0 dBFS」，这三件事都得**真的
解码**才知道。假件只能验「我传进去的 duration 被比较过了」，验不出「ffprobe 报出来的
时长是不是我写进去的那个」。参考音是整条配音链路的**输入**：它错了，后面每一句都
跟着错，而错误要到听成片时才暴露。

覆盖什么
--------
① §4.3.1 表格里「不通过 ⇒ **拒绝入库**」的那四条（段数 / 单段时长 / 削波 / 采样率）
   逐条有用例，且都断言**库里没有那一行** —— 只断言 ``check.ok is False`` 不够：
   拦在体检、漏在入库，是两件事；
② 旁车文件（``ref.txt`` / ``profile.json``）缺失或对不上只 ``warn``，不拦；
③ 一个坏音色不拖垮整批（第 2 个被拒，第 1 个照进）；
④ 默认 ``voice_map`` 能不能真的解析到占位音色 —— 这是 2026-09-17 修掉的一个真实
   bug：占位脚本造的名字与默认映射对不上 ⇒ 每个角色都**悄悄**退回进程音色，
   占位音色白造，而盘上明明躺着两个能用的。
"""

from __future__ import annotations

import sqlite3
import wave
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from studio.assets.layout import AssetKind
from studio.assets.validate import Problem
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.repositories import IngestAction, VoiceProfileRepo
from studio.domain.models import TaskPayload
from studio.services.asset_service import AssetService, ScannedAsset
from studio.services.voice_service import VOICE_SOURCE_MAP, resolve_voice, usable_voices

#: 一段参考音的默认参数：12 秒 @ 24 kHz —— 都在 §4.3.1 的区间里
SEGMENT_SECONDS = 12.0
SEGMENT_RATE = 24_000
#: 振幅 0.5 ⇒ 峰值约 -6 dBFS（低于 -1.0 的上限，不会被判削波）
QUIET_AMPLITUDE = 0.5


def _write_wav(
    path: Path,
    *,
    seconds: float,
    rate: int = SEGMENT_RATE,
    amplitude: float = QUIET_AMPLITUDE,
) -> Path:
    """写一段**真的** PCM wav（200 Hz 方波，振幅可调）。

    用方波而不是正弦：峰值**就是**振幅，不用等 ffmpeg 量完才知道；要造「削波」把
    振幅开到 1.0 就行（0 dBFS > -1.0 的上限）。用 stdlib ``wave`` 而不是 ffmpeg 来
    造输入：这里要验的是**判定**，造输入这一步少一个外部依赖少一处飘。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    peak = int(max(-1.0, min(1.0, amplitude)) * 32767)
    period = max(2, rate // 200)
    half = period // 2
    high = peak.to_bytes(2, "little", signed=True) * half
    low = (-peak).to_bytes(2, "little", signed=True) * (period - half)
    body = ((high + low) * (frames // period + 1))[: frames * 2]
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(body)
    return path


def _voice(
    paths: StudioPaths,
    voice_id: str = "bigbear",
    *,
    durations: Sequence[float] = (SEGMENT_SECONDS, SEGMENT_SECONDS),
    rate: int = SEGMENT_RATE,
    amplitude: float = QUIET_AMPLITUDE,
    text: bool = True,
    profile: bool = True,
) -> Path:
    """造一个音色目录（段数 = ``durations`` 的长度）。

    ``text`` / ``profile`` 关掉就是「旁车文件缺失」那一条 —— 它只该 ``warn``。
    """
    root = paths.voice_src_dir / voice_id
    root.mkdir(parents=True, exist_ok=True)
    for index, seconds in enumerate(durations, start=1):
        _write_wav(root / f"ref_{index:02d}.wav", seconds=seconds, rate=rate, amplitude=amplitude)
    if text:
        lines = [f"第 {index} 段参考文本" for index in range(1, len(durations) + 1)]
        (root / "ref.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if profile:
        (root / "profile.json").write_text(
            f'{{"id": "{voice_id}", "origin": "self_recorded"}}', encoding="utf-8"
        )
    return root


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
def service(connection: sqlite3.Connection, paths: StudioPaths) -> AssetService:
    """真 ffprobe / 真 ffmpeg 的 ``AssetService``（**不注入假件**）。"""
    return AssetService(connection, paths=paths, log=None)


def _ingested(service: AssetService, voice_id: str = "bigbear") -> ScannedAsset:
    """真的入一次库，返回那一份体检结论（顺带保证报告里只有这一个音色）。"""
    section = service.ingest(kind=AssetKind.VOICE, ids=[voice_id]).section(AssetKind.VOICE)
    assert len(section.assets) == 1
    return section.assets[0]


def _codes(items: Sequence[Problem]) -> list[str]:
    return [item.code for item in items]


class TestFourRejections:
    """§4.3.1 表格里「不通过 ⇒ 拒绝入库」的那四条 —— 一条都不能只是提醒。"""

    def test_too_few_segments(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, durations=(SEGMENT_SECONDS,))
        asset = _ingested(service)
        assert asset.check.ok is False
        assert _codes(asset.check.problems) == ["too_few_refs"]
        assert VoiceProfileRepo(connection).get("bigbear") is None

    def test_too_many_segments(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, durations=(SEGMENT_SECONDS,) * 4)
        asset = _ingested(service)
        assert asset.check.ok is False
        assert _codes(asset.check.problems) == ["too_many_refs"]
        assert VoiceProfileRepo(connection).get("bigbear") is None

    def test_segment_too_short(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, durations=(SEGMENT_SECONDS, 5.0))
        asset = _ingested(service)
        assert asset.check.ok is False
        assert _codes(asset.check.problems) == ["ref_02_too_short"]
        assert VoiceProfileRepo(connection).get("bigbear") is None

    def test_segment_too_long(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, durations=(SEGMENT_SECONDS, 35.0))
        asset = _ingested(service)
        assert asset.check.ok is False
        assert _codes(asset.check.problems) == ["ref_02_too_long"]
        assert VoiceProfileRepo(connection).get("bigbear") is None

    def test_clipped(self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths) -> None:
        _voice(paths, amplitude=1.0)
        asset = _ingested(service)
        assert asset.check.ok is False
        assert _codes(asset.check.problems) == ["ref_01_clipped", "ref_02_clipped"]
        assert VoiceProfileRepo(connection).get("bigbear") is None

    def test_sample_rate_too_low(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, rate=8_000)
        asset = _ingested(service)
        assert asset.check.ok is False
        assert _codes(asset.check.problems) == ["ref_01_sample_rate", "ref_02_sample_rate"]
        assert VoiceProfileRepo(connection).get("bigbear") is None


class TestRegistration:
    """入库之后：库里有行、面板上选得到、默认映射真的接得上。"""

    def test_reference_audio_becomes_a_usable_voice(
        self,
        service: AssetService,
        connection: sqlite3.Connection,
        paths: StudioPaths,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # SAPI 那一侧清空：这样「选得到」只可能来自**刚入库的参考音**，
        # 不会因为这台机器碰巧装了同名语音包而假绿
        monkeypatch.setattr("studio.services.voice_service.list_voices_cached", lambda: ())
        _voice(paths, "bigbear")

        asset = _ingested(service, "bigbear")
        assert asset.check.ok is True
        assert asset.check.warnings == ()
        assert asset.action is IngestAction.CREATED

        row = VoiceProfileRepo(connection).get("bigbear")
        assert row is not None
        assert row.enabled is True
        assert row.ref_count == 2
        assert row.total_duration_ms == pytest.approx(2 * SEGMENT_SECONDS * 1000, abs=200)
        assert row.sample_rate == SEGMENT_RATE
        assert row.text_path is not None and row.proof_path is not None
        assert row.peak_db is not None and row.peak_db <= -1.0

        # 「入库了」不等于「选得到」：配音面板读的是 usable_voices
        assert "bigbear" in usable_voices(connection)

    def test_default_voice_map_resolves_to_the_seeded_names(
        self,
        service: AssetService,
        connection: sqlite3.Connection,
        paths: StudioPaths,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """默认 ``voice_map`` 必须真的解析得到（2026-09-17 修的那个 bug）。

        占位脚本造的名字一旦与 ``TaskPayload.voice_map`` 的默认值对不上，症状**不是
        报错**，而是每个角色都悄悄退回进程音色 —— 占位音色白造，只有翻 manifest 才
        看得见。这里从**默认值**出发走完整条「入库 → 可选 → 解析」，接不上就红。
        """
        monkeypatch.setattr("studio.services.voice_service.list_voices_cached", lambda: ())
        for voice_id in ("bigbear", "littlebear"):
            _voice(paths, voice_id)
        service.ingest(kind=AssetKind.VOICE)

        voice_map = TaskPayload().voice_map
        available = usable_voices(connection)
        for speaker in ("bigbear", "littlebear"):
            choice = resolve_voice(voice_map=voice_map, speaker=speaker, available=available)
            assert choice.source == VOICE_SOURCE_MAP, f"{speaker} 退回了进程音色"
            assert choice.voice == voice_map[speaker]

    def test_ingest_is_idempotent(self, service: AssetService, paths: StudioPaths) -> None:
        _voice(paths, "bigbear")
        assert _ingested(service).action is IngestAction.CREATED
        assert _ingested(service).action is IngestAction.UNCHANGED


class TestSidecarWarnings:
    """旁车文件：缺了、对不上，都只提醒 —— 引擎仍能跑，但复刻与留档会打折。"""

    def test_missing_sidecars_only_warn(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, text=False, profile=False)
        asset = _ingested(service)
        assert asset.check.ok is True
        assert _codes(asset.check.warnings) == ["ref_text_missing", "profile_missing"]
        assert VoiceProfileRepo(connection).get("bigbear") is not None

    def test_ref_text_must_line_up_with_the_segments(self, service: AssetService, paths: StudioPaths) -> None:
        root = _voice(paths)
        (root / "ref.txt").write_text("只有一行\n", encoding="utf-8")
        asset = _ingested(service)
        assert asset.check.ok is True
        assert _codes(asset.check.warnings) == ["ref_text_mismatch"]


class TestBatchBehaviour:
    """一批里有一个坏的不该拖垮其余 —— 素材是人工攒的，坏的那个才是常态。"""

    def test_preview_writes_nothing(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, "bigbear")
        section = service.scan(kind=AssetKind.VOICE).section(AssetKind.VOICE)
        assert section.assets[0].check.ok is True
        assert section.assets[0].stored is False
        assert VoiceProfileRepo(connection).list_all() == []

    def test_a_rejected_voice_does_not_block_the_good_one(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, "bigbear")
        _voice(paths, "littlebear", durations=(SEGMENT_SECONDS, 5.0))
        section = service.ingest(kind=AssetKind.VOICE).section(AssetKind.VOICE)
        by_id = {item.id: item for item in section.assets}
        assert by_id["bigbear"].action is IngestAction.CREATED
        assert by_id["littlebear"].check.ok is False
        assert [row.id for row in VoiceProfileRepo(connection).list_all()] == ["bigbear"]
