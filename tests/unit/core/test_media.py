"""外部媒体工具层（T4.8 · §3.3.14）。

这一层最容易写错的两处都不是"跑没跑起来"，而是**怎么把 ffprobe / ffmpeg 的话读懂**：

- ``avg_frame_rate`` 是 ``"30000/1001"`` 这种**分数字符串**，还可能是 ``"0/0"``；
- ``sample_rate`` 是**字符串**，而图片根本没有时长。

所以解析函数是纯函数、单测不依赖真 ffprobe（注入 ``runner``），
"工具不在"与"文件坏了"两个码也各有一条用例 —— 它们的处置完全不同（修环境 vs 换素材）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.media import (
    PROBE_TIMEOUT_SEC,
    CommandResult,
    MediaInfo,
    analyze_volume,
    extract_thumbnail,
    ffmpeg_binary,
    ffprobe_binary,
    measure_loudness,
    parse_loudness_output,
    parse_probe_json,
    parse_volume_output,
    probe_media,
    run_command,
)


def _payload(**overrides: Any) -> dict[str, Any]:
    """一份典型的 ffprobe 输出（视频 + 音频 + 容器），可按需覆盖某一层。"""
    payload: dict[str, Any] = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30/1",
                "pix_fmt": "yuv420p",
                "duration": "12.5",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "44100",
                "channels": 2,
                "bit_rate": "128000",
            },
        ],
        "format": {"duration": "12.5", "bit_rate": "2500000"},
    }
    payload.update(overrides)
    return payload


class _FakeRunner:
    """记录收到的 argv，并回放一份预置结果（单测不需要真 ffprobe）。"""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.argv: list[str] = []
        self.timeout: int = PROBE_TIMEOUT_SEC

    def __call__(self, argv: list[str], *, timeout: int = PROBE_TIMEOUT_SEC) -> CommandResult:
        self.argv = list(argv)
        self.timeout = timeout
        return self.result


class TestParseProbeJson:
    def test_reads_the_whole_picture(self) -> None:
        info = parse_probe_json(_payload(), path="a.mp4", size_bytes=123)
        assert info.path == "a.mp4"
        assert info.size_bytes == 123
        assert info.duration_ms == 12500
        assert info.video_codec == "h264"
        assert info.audio_codec == "aac"
        assert (info.width, info.height) == (1920, 1080)
        assert info.fps == pytest.approx(29.97, abs=0.001)
        assert info.pix_fmt == "yuv420p"
        assert info.sample_rate == 44100
        assert info.channels == 2
        assert info.bitrate_kbps == 2500
        assert info.has_video and info.has_audio
        assert not info.is_image

    def test_frame_rate_is_a_fraction_string(self) -> None:
        payload = _payload(streams=[{"codec_type": "video", "avg_frame_rate": "24/1"}])
        assert parse_probe_json(payload, path="a.mp4", size_bytes=1).fps == 24.0

    def test_zero_over_zero_means_no_frame_rate(self) -> None:
        # 0.0 会让后面"按帧率算帧数"的代码除以零；这里必须是 None
        payload = _payload(streams=[{"codec_type": "video", "avg_frame_rate": "0/0"}])
        assert parse_probe_json(payload, path="a.mp4", size_bytes=1).fps is None

    def test_falls_back_to_r_frame_rate(self) -> None:
        payload = _payload(streams=[{"codec_type": "video", "avg_frame_rate": "0/0", "r_frame_rate": "25/1"}])
        assert parse_probe_json(payload, path="a.mp4", size_bytes=1).fps == 25.0

    def test_garbage_frame_rate_is_ignored(self) -> None:
        payload = _payload(streams=[{"codec_type": "video", "avg_frame_rate": "abc/def"}])
        assert parse_probe_json(payload, path="a.mp4", size_bytes=1).fps is None

    def test_sample_rate_is_a_string(self) -> None:
        payload = _payload(streams=[{"codec_type": "audio", "sample_rate": "16000", "channels": 1}])
        info = parse_probe_json(payload, path="a.wav", size_bytes=1)
        assert info.sample_rate == 16000
        assert info.channels == 1

    def test_duration_falls_back_to_the_video_stream(self) -> None:
        payload = _payload(format={}, streams=[{"codec_type": "video", "duration": "3.5"}])
        assert parse_probe_json(payload, path="a.mp4", size_bytes=1).duration_ms == 3500

    def test_image_has_no_duration(self) -> None:
        payload = _payload(
            format={},
            streams=[{"codec_type": "video", "codec_name": "png", "pix_fmt": "rgba", "width": 200}],
        )
        info = parse_probe_json(payload, path="w.png", size_bytes=1)
        assert info.duration_ms == 0
        assert info.is_image is True
        assert info.has_alpha is True

    def test_alpha_pixel_formats(self) -> None:
        for pix_fmt, expected in [("rgba", True), ("pal8", True), ("yuv420p", False)]:
            payload = _payload(streams=[{"codec_type": "video", "pix_fmt": pix_fmt}])
            info = parse_probe_json(payload, path="a.mp4", size_bytes=1)
            assert info.has_alpha is expected

    def test_bitrate_adds_up_streams_when_container_is_silent(self) -> None:
        payload = _payload(
            format={"duration": "1"},
            streams=[
                {"codec_type": "video", "bit_rate": "1000000"},
                {"codec_type": "audio", "bit_rate": "128000"},
            ],
        )
        assert parse_probe_json(payload, path="a.mp4", size_bytes=1).bitrate_kbps == 1128

    def test_bitrate_none_when_nothing_is_reported(self) -> None:
        payload = _payload(format={"duration": "1"}, streams=[{"codec_type": "video"}])
        assert parse_probe_json(payload, path="a.mp4", size_bytes=1).bitrate_kbps is None

    def test_empty_payload_is_all_unknown(self) -> None:
        info = parse_probe_json({}, path="a.mp4", size_bytes=0)
        assert info.duration_ms == 0
        assert info.video_codec is None and info.audio_codec is None
        assert info.has_video is False and info.has_audio is False
        assert info.is_image is False

    def test_non_mapping_streams_are_ignored(self) -> None:
        payload = _payload(streams=["nonsense", 42, None])
        info = parse_probe_json(payload, path="a.mp4", size_bytes=1)
        assert info.has_video is False

    def test_to_dict_is_json_ready(self) -> None:
        payload = _payload()
        info = parse_probe_json(payload, path="a.mp4", size_bytes=1)
        dumped = info.to_dict()
        assert dumped["duration_ms"] == 12500
        assert dumped["has_alpha"] is False
        assert dumped["is_image"] is False
        assert set(dumped) >= {"path", "size_bytes", "sample_rate", "bitrate_kbps"}


class TestParseVolumeOutput:
    SAMPLE = (
        "[Parsed_volumedetect_0 @ 000001] mean_volume: -21.5 dB\n"
        "[Parsed_volumedetect_0 @ 000001] max_volume: -3.2 dB\n"
        "[Parsed_volumedetect_0 @ 000001] histogram_3db: 10\n"
    )

    def test_reads_both_numbers(self) -> None:
        stats = parse_volume_output(self.SAMPLE)
        assert stats.max_db == pytest.approx(-3.2)
        assert stats.mean_db == pytest.approx(-21.5)

    def test_last_occurrence_wins(self) -> None:
        text = "max_volume: -20.0 dB\nmax_volume: -1.5 dB\n"
        assert parse_volume_output(text).max_db == pytest.approx(-1.5)

    def test_missing_labels_are_none(self) -> None:
        stats = parse_volume_output("nothing here")
        assert stats.max_db is None and stats.mean_db is None

    def test_unparsable_value_is_none(self) -> None:
        assert parse_volume_output("max_volume: n/a dB").max_db is None

    def test_to_dict(self) -> None:
        assert parse_volume_output(self.SAMPLE).to_dict() == {
            "max_db": pytest.approx(-3.2),
            "mean_db": pytest.approx(-21.5),
        }


class TestRunCommand:
    def test_success_keeps_both_streams(self) -> None:
        result = run_command([sys.executable, "-c", "print('out')"])
        assert result.ok is True
        assert result.returncode == 0
        assert "out" in result.stdout

    def test_missing_binary_is_minus_one(self) -> None:
        result = run_command(["definitely-not-a-real-binary-xyz"])
        assert result.returncode == -1
        assert result.ok is False
        assert "未找到可执行文件" in result.stderr

    def test_timeout_is_minus_two(self) -> None:
        result = run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        assert result.returncode == -2
        assert "超时" in result.stderr

    def test_unstartable_is_minus_three(self, tmp_path: Path) -> None:
        # 目录当可执行文件：CreateProcess 直接拒绝（PermissionError）
        result = run_command([str(tmp_path)])
        assert result.returncode == -3

    def test_tail_prefers_stderr_and_truncates(self) -> None:
        result = CommandResult(1, "stdout text", "x" * 500)
        assert len(result.tail(limit=10)) == 10
        assert result.tail(limit=10) == "x" * 10
        assert CommandResult(1, "only stdout", "  ").tail() == "only stdout"


class TestBinaryResolution:
    def test_env_override_wins(self) -> None:
        env = {"STUDIO_FFMPEG_BIN": "D:/tools/ffmpeg.exe", "STUDIO_FFPROBE_BIN": "D:/tools/ffprobe.exe"}
        assert ffmpeg_binary(env) == "D:/tools/ffmpeg.exe"
        assert ffprobe_binary(env) == "D:/tools/ffprobe.exe"

    def test_blank_override_falls_through(self) -> None:
        assert ffmpeg_binary({"STUDIO_FFMPEG_BIN": ""}) == ffmpeg_binary({})

    def test_falls_back_to_path_then_bare_name(self) -> None:
        # 装了就用 PATH 上的那个（大小写按平台来），没装就退成裸名字让 CreateProcess 去找
        assert Path(ffmpeg_binary({})).name.lower() in {"ffmpeg", "ffmpeg.exe"}
        assert Path(ffprobe_binary({})).name.lower() in {"ffprobe", "ffprobe.exe"}


class TestProbeMedia:
    def test_missing_file_is_undecodable(self, tmp_path: Path) -> None:
        with pytest.raises(StudioError) as excinfo:
            probe_media(tmp_path / "nope.mp4")
        assert excinfo.value.code is ErrorCode.MEDIA_UNDECODABLE

    def test_empty_file_is_undecodable(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        with pytest.raises(StudioError) as excinfo:
            probe_media(empty)
        assert excinfo.value.code is ErrorCode.MEDIA_UNDECODABLE

    def test_happy_path_uses_the_injected_runner(self, tmp_path: Path) -> None:
        target = tmp_path / "a.mp4"
        target.write_bytes(b"x")
        call = _FakeRunner(CommandResult(0, json.dumps(_payload()), ""))
        info = probe_media(target, runner=call)
        assert info.duration_ms == 12500
        assert info.size_bytes == 1
        assert "ffprobe" in Path(call.argv[0]).name.lower()

    def test_negative_return_code_is_probe_failed(self, tmp_path: Path) -> None:
        target = tmp_path / "a.mp4"
        target.write_bytes(b"x")
        with pytest.raises(StudioError) as excinfo:
            probe_media(target, runner=_FakeRunner(CommandResult(-1, "", "no tool")))
        assert excinfo.value.code is ErrorCode.MEDIA_PROBE_FAILED
        assert "STUDIO_FFPROBE_BIN" in (excinfo.value.remediation or "")

    def test_non_zero_return_code_is_undecodable(self, tmp_path: Path) -> None:
        target = tmp_path / "a.mp4"
        target.write_bytes(b"x")
        with pytest.raises(StudioError) as excinfo:
            probe_media(target, runner=_FakeRunner(CommandResult(1, "", "Invalid data")))
        assert excinfo.value.code is ErrorCode.MEDIA_UNDECODABLE
        assert excinfo.value.context["stderr"] == "Invalid data"

    def test_broken_json_is_probe_failed(self, tmp_path: Path) -> None:
        target = tmp_path / "a.mp4"
        target.write_bytes(b"x")
        with pytest.raises(StudioError) as excinfo:
            probe_media(target, runner=_FakeRunner(CommandResult(0, "not json", "")))
        assert excinfo.value.code is ErrorCode.MEDIA_PROBE_FAILED

    def test_non_mapping_json_is_probe_failed(self, tmp_path: Path) -> None:
        target = tmp_path / "a.mp4"
        target.write_bytes(b"x")
        with pytest.raises(StudioError) as excinfo:
            probe_media(target, runner=_FakeRunner(CommandResult(0, "[1, 2]", "")))
        assert excinfo.value.code is ErrorCode.MEDIA_PROBE_FAILED


class TestAnalyzeVolume:
    def test_reads_the_parsed_stats(self, tmp_path: Path) -> None:
        target = tmp_path / "ref_01.wav"
        target.write_bytes(b"x")
        call = _FakeRunner(CommandResult(0, "", TestParseVolumeOutput.SAMPLE))
        stats = analyze_volume(target, runner=call)
        assert stats.max_db == pytest.approx(-3.2)

    def test_negative_return_code_is_probe_failed(self, tmp_path: Path) -> None:
        with pytest.raises(StudioError) as excinfo:
            analyze_volume(tmp_path / "x.wav", runner=_FakeRunner(CommandResult(-1, "", "")))
        assert excinfo.value.code is ErrorCode.MEDIA_PROBE_FAILED

    def test_non_zero_return_code_is_undecodable(self, tmp_path: Path) -> None:
        with pytest.raises(StudioError) as excinfo:
            analyze_volume(tmp_path / "x.wav", runner=_FakeRunner(CommandResult(1, "", "boom")))
        assert excinfo.value.code is ErrorCode.MEDIA_UNDECODABLE


class TestMediaInfoHelpers:
    def test_defaults_and_derived_flags(self) -> None:
        info = MediaInfo(
            path="a.mp4",
            size_bytes=10,
            duration_ms=0,
            video_codec="h264",
            audio_codec=None,
            width=10,
            height=10,
            fps=None,
            pix_fmt=None,
            sample_rate=None,
            channels=None,
            bitrate_kbps=None,
        )
        assert info.is_image is True
        assert info.has_alpha is False


class _WritingRunner(_FakeRunner):
    """像真 ffmpeg 一样**落一个文件**再返回（缩略图那条路要检查产物存不存在）。"""

    def __init__(self, result: CommandResult, *, write: bool) -> None:
        super().__init__(result)
        self.write = write

    def __call__(self, argv: list[str], *, timeout: int = PROBE_TIMEOUT_SEC) -> CommandResult:
        super().__call__(argv, timeout=timeout)
        if self.write:
            Path(argv[-1]).write_bytes(b"jpeg")
        return self.result


class TestExtractThumbnail:
    def test_writes_the_frame_and_returns_the_path(self, tmp_path: Path) -> None:
        target = tmp_path / "thumbs" / "parkour_001.jpg"
        target.parent.mkdir(parents=True)
        call = _WritingRunner(CommandResult(0, "", ""), write=True)
        result = extract_thumbnail(tmp_path / "parkour_001.mp4", target, at_ms=1500, runner=call)
        assert result == target
        assert "-ss" in call.argv
        # -ss 必须在 -i 前面（跳过去再解，而不是解到那里再丢）
        assert call.argv.index("-ss") < call.argv.index("-i")
        assert call.argv[call.argv.index("-ss") + 1] == "1.500"
        assert "scale=480:-2" in call.argv

    def test_negative_seek_is_clamped(self, tmp_path: Path) -> None:
        target = tmp_path / "a.jpg"
        call = _WritingRunner(CommandResult(0, "", ""), write=True)
        extract_thumbnail(tmp_path / "a.mp4", target, at_ms=-500, runner=call)
        assert call.argv[call.argv.index("-ss") + 1] == "0.000"

    def test_tool_missing_is_probe_failed(self, tmp_path: Path) -> None:
        call = _WritingRunner(CommandResult(-1, "", "no ffmpeg"), write=False)
        with pytest.raises(StudioError) as excinfo:
            extract_thumbnail(tmp_path / "a.mp4", tmp_path / "a.jpg", runner=call)
        assert excinfo.value.code is ErrorCode.MEDIA_PROBE_FAILED

    def test_success_without_an_output_file_is_still_a_failure(self, tmp_path: Path) -> None:
        # ffmpeg 对"跳过头了"的常见表现就是 rc=0 且什么都不写 —— 这种"成功"不能算成功
        call = _WritingRunner(CommandResult(0, "", ""), write=False)
        with pytest.raises(StudioError) as excinfo:
            extract_thumbnail(tmp_path / "a.mp4", tmp_path / "a.jpg", runner=call)
        assert excinfo.value.code is ErrorCode.MEDIA_UNDECODABLE


class TestMeasureLoudness:
    MULTILINE = '[Parsed_loudnorm_0 @ 000001] \n{\n\t"input_i" : "-24.55",\n\t"input_tp" : "-2.14",\n}\n'

    def test_reads_the_integrated_loudness(self, tmp_path: Path) -> None:
        call = _FakeRunner(CommandResult(0, "", self.MULTILINE))
        assert measure_loudness(tmp_path / "bgm.mp3", runner=call) == pytest.approx(-24.55)
        assert "loudnorm=print_format=json" in call.argv

    def test_tool_missing_is_probe_failed(self, tmp_path: Path) -> None:
        with pytest.raises(StudioError) as excinfo:
            measure_loudness(tmp_path / "x.mp3", runner=_FakeRunner(CommandResult(-1, "", "")))
        assert excinfo.value.code is ErrorCode.MEDIA_PROBE_FAILED

    def test_broken_file_is_undecodable(self, tmp_path: Path) -> None:
        with pytest.raises(StudioError) as excinfo:
            measure_loudness(tmp_path / "x.mp3", runner=_FakeRunner(CommandResult(1, "", "boom")))
        assert excinfo.value.code is ErrorCode.MEDIA_UNDECODABLE


class TestParseLoudnessOutput:
    def test_multiline_json(self) -> None:
        text = '[Parsed_loudnorm_0 @ 0x1] \n{\n\t"input_i" : "-24.55",\n\t"input_tp" : "-2.14",\n}\n'
        assert parse_loudness_output(text) == pytest.approx(-24.55)

    def test_single_line_json(self) -> None:
        text = '{"input_i" : "-18.2", "input_tp" : "-1.0", "input_lra" : "3.0"}'
        assert parse_loudness_output(text) == pytest.approx(-18.2)

    def test_last_occurrence_wins(self) -> None:
        text = '"input_i" : "-30.0",\n"input_i" : "-20.0",\n'
        assert parse_loudness_output(text) == pytest.approx(-20.0)

    def test_silence_reports_minus_infinity(self) -> None:
        # 一段静音的曲子：``-inf`` 不是"响度 0"，当成"量不出来"处理
        assert parse_loudness_output('"input_i" : "-inf",') is None

    def test_missing_key(self) -> None:
        assert parse_loudness_output("nothing here") is None

    def test_non_numeric_value(self) -> None:
        assert parse_loudness_output('"input_i" : "n/a",') is None
