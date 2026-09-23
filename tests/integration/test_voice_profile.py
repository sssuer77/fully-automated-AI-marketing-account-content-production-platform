"""参考音入库与音色注册（T2.4 验收 · §04.3.1 / R2）。

这一条验的是「我录的那几段，到底能不能用」的**首尾两端**
--------------------------------------------------------
前端：``data/voice_src/<id>/`` 里真的 wav；后端：``voice_profiles`` 表里的那一行，
以及**配音面板上真的选得到它**。中间那层判定不重写、不绕过 —— 本文件走的是
``AssetService.ingest``，与素材库面板点「扫描并入库」是**同一条代码路径**（所以
「命令行说能进、面板说不能」结构上不会发生）。

为什么用真 ffprobe / 真 ffmpeg
------------------------------
阈值是「单段 2-30 秒」（下限见裁定 369）「采样率 ≥ 16 kHz」「峰值 ≤ -1.0 dBFS」，这三件事都得**真的
解码**才知道。假件只能验「我传进去的 duration 被比较过了」，验不出「ffprobe 报出来的
时长是不是我写进去的那个」。参考音是整条配音链路的**输入**：它错了，后面每一句都
跟着错，而错误要到听成片时才暴露。

覆盖什么
--------
① §4.3.1 表格里「不通过 ⇒ **拒绝入库**」的那三条（单段时长 / 削波 / 采样率）
   逐条有用例，且都断言**库里没有那一行** —— 只断言 ``check.ok is False`` 不够：
   拦在体检、漏在入库，是两件事。（**段数**原本是第四条，裁定 377 起不再是判据：
   一段能用，很多段也能用；段数只影响音色稳不稳。）
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
from studio.services.voice_service import (
    VOICE_SOURCE_FALLBACK,
    resolve_voice,
    speakable_voices,
    usable_voices,
)

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


class TestRejections:
    """§4.3.1 表格里「不通过 ⇒ 拒绝入库」的那三条 —— 一条都不能只是提醒。"""

    def test_one_segment_is_enough(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        """一段就入库（**裁定 377**），只是提醒"多给几段更稳"。

        门槛拦下来的代价很具体：手边只有一句干净台词的人会**把同一个文件复制一份**
        去凑数 —— 真机库里那条 ``sunxiaochuan`` 的两段 sha256 完全相同，就是这么来的。
        """
        _voice(paths, durations=(SEGMENT_SECONDS,))
        asset = _ingested(service)
        assert asset.check.ok is True
        assert _codes(asset.check.problems) == []
        assert "single_ref" in _codes(asset.check.warnings)
        assert VoiceProfileRepo(connection).get("bigbear") is not None

    def test_many_segments_are_fine(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        """段数**不设上限**（**裁定 377**）—— 上限 3 是我们自己加的，引擎没有这条。"""
        _voice(paths, durations=(SEGMENT_SECONDS,) * 6)
        asset = _ingested(service)
        assert asset.check.ok is True
        assert _codes(asset.check.problems) == []
        row = VoiceProfileRepo(connection).get("bigbear")
        assert row is not None
        assert row.ref_count == 6

    def test_segment_too_short(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        _voice(paths, durations=(SEGMENT_SECONDS, 1.0))
        asset = _ingested(service)
        assert asset.check.ok is False
        assert _codes(asset.check.problems) == ["ref_02_too_short"]
        assert VoiceProfileRepo(connection).get("bigbear") is None

    def test_three_second_segment_passes(
        self, service: AssetService, connection: sqlite3.Connection, paths: StudioPaths
    ) -> None:
        """3 秒的参考音**能入库**（裁定 369）。

        这一条就是"下限从 10 秒降到 2 秒"的全部意义：手边只有一句台词的人，
        以前永远入不了库。上游 ``frontend.py`` 只挡 >30s（"提取不了 speech token"），
        没有任何"太短"的判据；真机也验过 2.978 秒的参考音能克隆出 4.48 秒音频。
        """
        _voice(paths, durations=(3.0, 3.0))
        asset = _ingested(service)
        assert asset.check.ok is True
        assert VoiceProfileRepo(connection).get("bigbear") is not None

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
    """入库之后：库里有行、面板的下拉框里选得到。"""

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

        # 「入库了」不等于「选得到」：配音面板的下拉框读的是 usable_voices
        assert "bigbear" in usable_voices(connection)

    def test_ingest_is_idempotent(self, service: AssetService, paths: StudioPaths) -> None:
        _voice(paths, "bigbear")
        assert _ingested(service).action is IngestAction.CREATED
        assert _ingested(service).action is IngestAction.UNCHANGED


class TestEngineSpeakability:
    """**候选 != 念得出来** —— 2026-09-17 真机实测撞出来的那个回归。

    两件事曾经被一个并集回答：``usable_voices`` 是**下拉框的候选**（"这台机器上有
    什么"，必须全，否则用户以为入库的东西丢了），``speakable_voices`` 是**当前这台
    引擎认不认**。占位音色改名与默认 ``voice_map`` 对齐之后，前者把 ``bigbear``
    判成"有"，于是 ``voice=bigbear`` 被写进作业 payload、传给 SAPI，``SelectVoice``
    直接抛 ⇒ 这一句连失败 3 次、降级成静音，**每一句都是** ⇒ 成片没人声，而库里
    写着"换音色成功"（正是 ``set_voice_map`` 那段 docstring 里怕的那件事）。
    """

    def test_the_default_map_points_at_the_seeded_reference_audio(
        self,
        service: AssetService,
        connection: sqlite3.Connection,
        paths: StudioPaths,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """默认 ``voice_map`` 的值必须**刚好**是占位脚本造的那两个 id。

        对不上的后果不是报错，而是每个角色都悄悄退回进程音色 —— 占位音色白造，
        只有翻 manifest 才看得见（陷阱 #153）。这条从**默认值**出发断言。
        """
        monkeypatch.setattr("studio.services.voice_service.list_voices_cached", lambda: ())
        for voice_id in ("bigbear", "littlebear"):
            _voice(paths, voice_id)
        service.ingest(kind=AssetKind.VOICE)

        candidates = usable_voices(connection)
        for speaker, voice_id in TaskPayload().voice_map.items():
            assert voice_id in candidates, f"{speaker} 指向的 {voice_id} 没入库"

    def test_a_reference_voice_is_not_speakable_by_sapi(
        self,
        service: AssetService,
        connection: sqlite3.Connection,
        paths: StudioPaths,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """参考音在库里、映射也对得上，但 SAPI 念不出来 ⇒ 解析结果是 ``fallback``。

        这不是"坏消息"，是**正确结论**：参考音要 CosyVoice 才能念（T2.1 / E5）。
        退回进程音色至少出得来一支有人声的片子；不退回则是整片静音。
        """
        monkeypatch.setattr(
            "studio.services.voice_service.list_voices_cached",
            lambda: ("Microsoft Huihui Desktop",),
        )
        for voice_id in ("bigbear", "littlebear"):
            _voice(paths, voice_id)
        service.ingest(kind=AssetKind.VOICE)

        assert speakable_voices(connection) == ("Microsoft Huihui Desktop",)
        voice_map = TaskPayload().voice_map
        for speaker in ("bigbear", "littlebear"):
            choice = resolve_voice(
                voice_map=voice_map, speaker=speaker, available=speakable_voices(connection)
            )
            assert choice.source == VOICE_SOURCE_FALLBACK
            assert choice.voice is None  # 「不覆盖」⇒ 由池进程的系统音色念
            assert choice.requested == voice_map[speaker]


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
        _voice(paths, "littlebear", durations=(SEGMENT_SECONDS, 1.0))
        section = service.ingest(kind=AssetKind.VOICE).section(AssetKind.VOICE)
        by_id = {item.id: item for item in section.assets}
        assert by_id["bigbear"].action is IngestAction.CREATED
        assert by_id["littlebear"].check.ok is False
        assert [row.id for row in VoiceProfileRepo(connection).list_all()] == ["bigbear"]
