"""素材体检（T4.8.2 · §3.3.14 / §4.3.1）。

用例的纪律有两条：

1. **一律注入假 probe / 假 volume** —— 判据是"时长 ≥ 15s""峰值 ≤ −1.0 dBFS"这类
   数字比较，真跑 ffprobe 只会把"判据写错了"和"这台机器没装 ffmpeg"混在一起；
2. **坏素材不许抛异常** —— 一个坏文件中断整批扫描，比它本身坏得多。所以每个
   "不合格"用例断言的都是一条 ``problems``，而不是一个异常。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from studio.assets.layout import AssetCandidate, AssetKind
from studio.assets.validate import (
    BGM_MIN_DURATION_MS,
    BROLL_MIN_USABLE_MS,
    VOICE_MIN_SAMPLE_RATE,
    VOICE_PEAK_CEILING_DB,
    VOICE_SEGMENT_MAX_MS,
    VOICE_SEGMENT_MIN_MS,
    AssetCheck,
    check,
    check_bgm,
    check_broll,
    check_voice,
)
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import MediaInfo, VolumeStats


def _info(
    *,
    duration_ms: int = 30_000,
    video: str | None = "h264",
    audio: str | None = "aac",
    sample_rate: int | None = 44_100,
) -> MediaInfo:
    return MediaInfo(
        path="x",
        size_bytes=1,
        duration_ms=duration_ms,
        video_codec=video,
        audio_codec=audio,
        width=1920 if video else None,
        height=1080 if video else None,
        fps=30.0 if video else None,
        pix_fmt="yuv420p" if video else None,
        sample_rate=sample_rate,
        channels=2 if audio else None,
        bitrate_kbps=1000,
    )


def _probe_ok(info: MediaInfo) -> Callable[..., MediaInfo]:
    def probe(path: Path, **_: Any) -> MediaInfo:
        return info

    return probe


def _probe_by_name(mapping: dict[str, MediaInfo]) -> Callable[..., MediaInfo]:
    """按文件名分别回放；表里没有的（= 打算模拟坏文件的那段）报"解不开"。"""

    def probe(path: Path, **_: Any) -> MediaInfo:
        if path.name not in mapping:
            raise StudioError("坏文件", code=ErrorCode.MEDIA_UNDECODABLE)
        return mapping[path.name]

    return probe


def _probe_raises(code: ErrorCode = ErrorCode.MEDIA_UNDECODABLE) -> Callable[..., MediaInfo]:
    def probe(path: Path, **_: Any) -> MediaInfo:
        raise StudioError("探测失败", code=code)

    return probe


def _volume_ok(max_db: float = -3.0) -> Callable[..., VolumeStats]:
    def volume(path: Path, **_: Any) -> VolumeStats:
        return VolumeStats(max_db=max_db, mean_db=max_db - 12)

    return volume


def _volume_raises(code: ErrorCode = ErrorCode.MEDIA_PROBE_FAILED) -> Callable[..., VolumeStats]:
    def volume(path: Path, **_: Any) -> VolumeStats:
        raise StudioError("量不了", code=code)

    return volume


def _candidate(kind: AssetKind, root: Path, names: list[str], asset_id: str = "x") -> AssetCandidate:
    files = tuple(root / name for name in names)
    return AssetCandidate(kind=kind, id=asset_id, path=root / names[0], files=files)


def _voice_candidate(tmp_path: Path, names: list[str]) -> AssetCandidate:
    directory = tmp_path / "bigbear"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text("data", encoding="utf-8")
    return AssetCandidate(
        kind=AssetKind.VOICE, id="bigbear", path=directory, files=tuple(directory / n for n in names)
    )


def _codes(check_result: AssetCheck) -> list[str]:
    return [item.code for item in check_result.problems]


def _warning_codes(check_result: AssetCheck) -> list[str]:
    return [item.code for item in check_result.warnings]


class TestCheckBroll:
    def test_healthy_clip_passes(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="self_recorded",
            probe=_probe_ok(_info()),
        )
        assert result.ok is True
        assert result.problems == ()
        assert result.warnings == ()
        assert result.info is not None
        assert result.info.duration_ms == 30_000

    def test_missing_license_is_a_problem(self, tmp_path: Path) -> None:
        for license_value in (None, ""):
            result = check_broll(
                _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
                license=license_value,
                probe=_probe_ok(_info()),
            )
            assert "license_missing" in _codes(result)
            assert result.ok is False

    def test_audio_only_file_is_not_broll(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            probe=_probe_ok(_info(video=None)),
        )
        assert "not_video" in _codes(result)

    def test_short_usable_window_is_rejected(self, tmp_path: Path) -> None:
        # 片头 5s 不能用 ⇒ 剩下 25s 还是够；这里故意压到下限以下
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            usable_from_ms=0,
            usable_to_ms=BROLL_MIN_USABLE_MS - 1,
            probe=_probe_ok(_info(duration_ms=60_000)),
        )
        assert "usable_too_short" in _codes(result)

    def test_usable_window_at_the_limit_passes(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            usable_to_ms=BROLL_MIN_USABLE_MS,
            probe=_probe_ok(_info(duration_ms=60_000)),
        )
        assert result.ok is True

    def test_usable_to_defaults_to_the_probed_duration(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            probe=_probe_ok(_info(duration_ms=3_000)),
        )
        assert "usable_too_short" in _codes(result)

    def test_usable_to_is_capped_by_the_probed_duration(self, tmp_path: Path) -> None:
        # 库里标到 10 分钟，但文件只有 4s ⇒ 按文件算，不能凭空多出可用区间
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            usable_to_ms=600_000,
            probe=_probe_ok(_info(duration_ms=4_000)),
        )
        assert "usable_too_short" in _codes(result)

    def test_empty_usable_range_is_reported(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            usable_from_ms=10_000,
            usable_to_ms=5_000,
            probe=_probe_ok(_info(duration_ms=60_000)),
        )
        assert set(_codes(result)) == {"usable_too_short", "usable_range_empty"}

    def test_negative_usable_from_is_clamped(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            usable_from_ms=-5_000,
            probe=_probe_ok(_info(duration_ms=30_000)),
        )
        assert result.ok is True

    def test_probe_failure_becomes_a_problem_not_an_exception(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            probe=_probe_raises(ErrorCode.MEDIA_UNDECODABLE),
        )
        assert _codes(result) == ["media_undecodable"]
        assert result.info is None

    def test_tool_missing_keeps_its_own_code(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            probe=_probe_raises(ErrorCode.MEDIA_PROBE_FAILED),
        )
        assert _codes(result) == ["media_probe_failed"]

    def test_to_dict_is_panel_ready(self, tmp_path: Path) -> None:
        result = check_broll(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license=None,
            probe=_probe_ok(_info()),
        )
        payload = result.to_dict()
        assert payload["kind"] == "broll"
        assert payload["ok"] is False
        assert payload["problems"][0]["code"] == "license_missing"
        assert payload["segments"] == []


class TestCheckBgm:
    def test_healthy_track_passes(self, tmp_path: Path) -> None:
        result = check_bgm(
            _candidate(AssetKind.BGM, tmp_path, ["bgm_001.mp3"]),
            license="purchased",
            probe=_probe_ok(_info(video=None, duration_ms=120_000)),
        )
        assert result.ok is True

    def test_duration_limit_is_inclusive(self, tmp_path: Path) -> None:
        exactly = check_bgm(
            _candidate(AssetKind.BGM, tmp_path, ["bgm_001.mp3"]),
            license="cc0",
            probe=_probe_ok(_info(video=None, duration_ms=BGM_MIN_DURATION_MS)),
        )
        assert exactly.ok is True

        just_under = check_bgm(
            _candidate(AssetKind.BGM, tmp_path, ["bgm_001.mp3"]),
            license="cc0",
            probe=_probe_ok(_info(video=None, duration_ms=BGM_MIN_DURATION_MS - 1)),
        )
        assert "too_short" in _codes(just_under)

    def test_file_without_audio_stream_is_rejected(self, tmp_path: Path) -> None:
        result = check_bgm(
            _candidate(AssetKind.BGM, tmp_path, ["bgm_001.mp3"]),
            license="cc0",
            probe=_probe_ok(_info(audio=None, duration_ms=60_000)),
        )
        assert _codes(result) == ["not_audio"]

    def test_license_and_probe_problems_add_up(self, tmp_path: Path) -> None:
        result = check_bgm(
            _candidate(AssetKind.BGM, tmp_path, ["bgm_001.mp3"]),
            license=None,
            probe=_probe_raises(),
        )
        assert set(_codes(result)) == {"license_missing", "media_undecodable"}


class TestCheckVoice:
    def test_healthy_voice_passes(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(),
        )
        assert result.ok is True
        assert len(result.segments) == 2
        assert result.info is not None

    def test_no_reference_audio_at_all(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref.txt", "profile.json"])
        result = check_voice(candidate, probe=_probe_ok(_info()), volume=_volume_ok())
        assert _codes(result) == ["no_refs"]

    def test_one_segment_passes_with_a_warning(self, tmp_path: Path) -> None:
        """一段也能入库（**裁定 377**）—— 引擎只需要一段，门槛是我们自己加的。

        只给一段时给一条 warning（多给几段音色更稳），而不是拦下来：拦下来的代价很
        具体 —— 手边只有一句干净台词的人会**把同一个文件复制一份**去凑数。
        """
        candidate = _voice_candidate(tmp_path, ["ref_01.wav"])
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(),
        )
        assert result.ok is True
        assert "single_ref" in _warning_codes(result)

    def test_many_segments_are_fine(self, tmp_path: Path) -> None:
        """段数**不设上限**（**裁定 377**）：多给几段是真的有用，不是负担。

        上游只挡单段 >30s；"2–3 段"那条上限是我们自己抄进 §4.3.1 的。
        """
        names = [f"ref_{index:02d}.wav" for index in range(1, 6)]
        candidate = _voice_candidate(tmp_path, names)
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(),
        )
        assert result.ok is True
        assert len(result.segments) == 5
        assert "single_ref" not in _warning_codes(result)

    def test_segment_length_window(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        probe = _probe_by_name(
            {
                "ref_01.wav": _info(video=None, duration_ms=VOICE_SEGMENT_MIN_MS - 1),
                "ref_02.wav": _info(video=None, duration_ms=VOICE_SEGMENT_MAX_MS + 1),
            }
        )
        result = check_voice(candidate, probe=probe, volume=_volume_ok())
        assert _codes(result) == ["ref_01_too_short", "ref_02_too_long"]

    def test_segment_boundaries_are_inclusive(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        probe = _probe_by_name(
            {
                "ref_01.wav": _info(video=None, duration_ms=VOICE_SEGMENT_MIN_MS),
                "ref_02.wav": _info(video=None, duration_ms=VOICE_SEGMENT_MAX_MS),
            }
        )
        assert check_voice(candidate, probe=probe, volume=_volume_ok()).ok is True

    def test_low_sample_rate_is_rejected(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000, sample_rate=VOICE_MIN_SAMPLE_RATE - 1)),
            volume=_volume_ok(),
        )
        assert _codes(result) == ["ref_01_sample_rate", "ref_02_sample_rate"]

    def test_segment_without_audio_stream_is_rejected(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        probe = _probe_by_name(
            {
                "ref_01.wav": _info(video="h264", audio=None, duration_ms=15_000),
                "ref_02.wav": _info(video=None, duration_ms=15_000),
            }
        )
        result = check_voice(candidate, probe=probe, volume=_volume_ok())
        assert _codes(result) == ["ref_01_not_audio"]

    def test_clipping_is_rejected(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(max_db=VOICE_PEAK_CEILING_DB + 0.5),
        )
        assert _codes(result) == ["ref_01_clipped", "ref_02_clipped"]

    def test_peak_exactly_at_the_ceiling_passes(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(max_db=VOICE_PEAK_CEILING_DB),
        )
        assert result.ok is True

    def test_unmeasurable_peak_is_a_warning_not_a_rejection(self, tmp_path: Path) -> None:
        # 量不出来 ≠ 削波了：不能因为工具跑不动就把好素材标红
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav", "ref.txt", "profile.json"])
        (candidate.path / "ref.txt").write_text("第一句\n第二句\n", encoding="utf-8")
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_raises(),
        )
        assert result.ok is True
        assert _warning_codes(result) == ["ref_01_volume_unknown", "ref_02_volume_unknown"]

    def test_segment_probe_failure_is_per_segment(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        probe = _probe_by_name({"ref_02.wav": _info(video=None, duration_ms=15_000)})
        result = check_voice(candidate, probe=probe, volume=_volume_ok())
        assert _codes(result) == ["ref_01_media_undecodable"]
        # 探测失败的那段不进 segments，成功的照进 —— 面板才能说清"第 1 段读不了"
        assert [item.duration_ms for item in result.segments] == [15_000]

    def test_missing_sidecars_are_warnings(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(),
        )
        assert result.ok is True
        assert set(_warning_codes(result)) == {"ref_text_missing", "profile_missing"}

    def test_matching_sidecars_produce_no_warning(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav", "ref.txt", "profile.json"])
        (candidate.path / "ref.txt").write_text("第一句\n第二句\n", encoding="utf-8")
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(),
        )
        assert result.warnings == ()

    def test_text_line_count_must_match_the_segments(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav", "ref.txt", "profile.json"])
        (candidate.path / "ref.txt").write_text("只有一句\n", encoding="utf-8")
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(),
        )
        assert _warning_codes(result) == ["ref_text_mismatch"]

    def test_blank_lines_do_not_count(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav", "ref.txt", "profile.json"])
        (candidate.path / "ref.txt").write_text("第一句\n\n   \n第二句\n", encoding="utf-8")
        result = check_voice(
            candidate,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(),
        )
        assert "ref_text_mismatch" not in _warning_codes(result)

    def test_segments_follow_file_name_order(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_02.wav", "ref_01.wav"])
        probe = _probe_by_name(
            {
                "ref_01.wav": _info(video=None, duration_ms=11_000),
                "ref_02.wav": _info(video=None, duration_ms=22_000),
            }
        )
        result = check_voice(candidate, probe=probe, volume=_volume_ok())
        assert [item.duration_ms for item in result.segments] == [11_000, 22_000]


class TestDispatch:
    def test_routes_broll(self, tmp_path: Path) -> None:
        result = check(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            probe=_probe_ok(_info()),
        )
        assert result.kind is AssetKind.BROLL
        assert result.ok is True

    def test_routes_bgm(self, tmp_path: Path) -> None:
        result = check(
            _candidate(AssetKind.BGM, tmp_path, ["bgm_001.mp3"]),
            license="cc0",
            probe=_probe_ok(_info(video=None, duration_ms=60_000)),
        )
        assert result.kind is AssetKind.BGM

    def test_routes_voice_and_ignores_license(self, tmp_path: Path) -> None:
        candidate = _voice_candidate(tmp_path, ["ref_01.wav", "ref_02.wav"])
        result = check(
            candidate,
            license=None,
            probe=_probe_ok(_info(video=None, duration_ms=15_000)),
            volume=_volume_ok(),
        )
        assert result.kind is AssetKind.VOICE
        assert result.ok is True

    def test_passes_the_usable_window_through(self, tmp_path: Path) -> None:
        result = check(
            _candidate(AssetKind.BROLL, tmp_path, ["parkour_001.mp4"]),
            license="cc0",
            usable_from_ms=0,
            usable_to_ms=1_000,
            probe=_probe_ok(_info(duration_ms=60_000)),
        )
        assert "usable_too_short" in _codes(result)

    def test_constants_match_the_frozen_contract(self) -> None:
        # 段数**故意没有常量**（裁定 377）：它不是判据 —— 引擎一次只吃一段 prompt，
        # 段数是"越多越稳"的建议，不是"少了就拒"的门。
        assert BGM_MIN_DURATION_MS == 15_000
        assert VOICE_MIN_SAMPLE_RATE == 16_000
        assert VOICE_PEAK_CEILING_DB == -1.0
        assert BROLL_MIN_USABLE_MS == 4_500
        assert pytest.approx(-1.0) == VOICE_PEAK_CEILING_DB
