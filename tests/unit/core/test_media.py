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
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from studio.core import media as media_module
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


#: "父进程"脚本：立刻开一个写心跳的**孙进程**，记下它的 pid，然后自己睡 60 秒。
#: 用真脚本而不是 ``-c`` 拼字符串 —— Windows 上引号与转义会让这条用例测的是"引号"。
_PARENT_SCRIPT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
    "open(sys.argv[3], 'w').write(str(child.pid))\n"
    "time.sleep(60)\n"
)

#: "孙进程"脚本：每 50ms 把当前时间写进 argv[1]（判活用的心跳）。
_HEARTBEAT_SCRIPT = (
    "import pathlib, sys, time\n"
    "target = pathlib.Path(sys.argv[1])\n"
    "while True:\n"
    "    target.write_text(str(time.time()), encoding='utf-8')\n"
    "    time.sleep(0.05)\n"
)


def _process_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """造一套两级进程树的脚本；返回 ``(父脚本, 心跳脚本, 心跳文件, pid 文件)``。"""
    parent = tmp_path / "parent.py"
    heartbeat = tmp_path / "heartbeat.py"
    target = tmp_path / "beat.txt"
    pid_file = tmp_path / "grandchild.pid"
    parent.write_text(_PARENT_SCRIPT, encoding="utf-8")
    heartbeat.write_text(_HEARTBEAT_SCRIPT, encoding="utf-8")
    return parent, heartbeat, target, pid_file


def _pid_gone(pid: int, *, within: float = 5.0) -> bool:
    """那个 pid 是不是真没了（轮询而不是睡一觉就断言：进程退出与信号送达不是同一拍）。"""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return True
        time.sleep(0.1)
    return False


def _kill_leftovers(pid_file: Path) -> None:
    """用例失败时的清场：孙进程是 ``daemon`` 线程开出来的，没人替它收尸。"""
    if not pid_file.is_file():
        return
    try:
        process = psutil.Process(int(pid_file.read_text(encoding="utf-8")))
        process.kill()
    except (psutil.Error, ValueError):
        pass


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

    def test_a_killed_tree_leaves_no_grandchild(self, tmp_path: Path) -> None:
        """★ 超时 ⇒ 连**孙进程**一起杀掉（§01.5.2 的"取消/超时"约定）。

        为什么不能只杀直接子进程 —— 2026-09-17 实测：``subprocess.run(timeout=2)`` 遇到
        一个攥着管道的孙进程时，**12 秒后仍然挂着**（它在超时分支里又调了一次**不带超时**的
        ``communicate()``）。这条用例在回归时会**挂住**而不是失败，所以调用放在线程里、
        带一个看门狗 —— 否则整个测试套件跟着卡死，看到的现象会像"pytest 坏了"。
        """
        parent, heartbeat, target, pid_file = _process_tree(tmp_path)
        outcome: list[CommandResult] = []

        def call() -> None:
            outcome.append(
                run_command(
                    [sys.executable, str(parent), str(heartbeat), str(target), str(pid_file)],
                    timeout=2,
                )
            )

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(timeout=30)
        try:
            assert not worker.is_alive(), "run_command 没返回：管道还被孙进程攥着（这正是要修的那个 bug）"
            assert outcome[0].returncode == -2
            assert "超时" in outcome[0].stderr
            assert "已终止" in outcome[0].stderr
            assert target.is_file(), "孙进程压根没起来 ⇒ 这条用例没测到东西"
            assert _pid_gone(int(pid_file.read_text(encoding="utf-8")))
        finally:
            _kill_leftovers(pid_file)

    def test_without_taskkill_it_still_kills_the_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``taskkill`` 用不了（不在 PATH / 权限不够）⇒ 至少杀掉直接子进程，**并且如实说**。

        两件事一起验：①调用没有变慢 —— 只杀直接子进程时 ``_reap`` 会立刻拿到 EOF，而
        "没杀干净"会让它一直等到 :data:`~studio.core.media.REAP_TIMEOUT_SEC`（10s）；
        ②报错里不写"进程树"，不假装树已经干净了。
        """
        monkeypatch.setattr(media_module, "_taskkill", lambda pid: False)
        started = time.monotonic()

        result = run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)

        assert result.returncode == -2
        assert "已终止" in result.stderr
        assert "进程树" not in result.stderr, "taskkill 明明失败了，却报成杀掉了整棵树"
        assert time.monotonic() - started < 5, "直接子进程没被杀掉（_reap 一直等到了收尸超时）"

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
