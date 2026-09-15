"""外部媒体工具（``ffmpeg`` / ``ffprobe``）的统一出口（T4.8 · §3.3.14）。

为什么值得单独一层
------------------
"跑一个外部进程并把它的话读回来"是 doctor / 素材入库 / T3 渲染三处共同的需要。
抄三份的代价很具体：某天要给"命令超时"补一种处理，或者统一加
``CREATE_NO_WINDOW``（不加的话每探一个文件就在屏幕上闪一个黑框），就得记得改三处，
而漏掉的那一处会表现为"素材面板闪黑框、渲染面板不闪"这种看起来毫无道理的差异。

分工（**这一层不做业务判断**）
------------------------------
- :func:`run_command` —— 只负责"跑起来、把三样东西拿回来"（rc / stdout / stderr）；
- :func:`probe_media` —— 只负责"``ffprobe`` 一次、把 JSON 翻成 :class:`MediaInfo`"；
- :func:`analyze_volume` —— 只负责"``volumedetect`` 一次、把两行日志翻成 :class:`VolumeStats`"。

"时长够不够""采样率达不达标"是素材库（:mod:`studio.assets`）的事。这里一旦开始判
"合格"，T3 渲染复核成片时就只能绕开它另写一份 —— 于是同一句 ffprobe 命令有了两个版本。

为什么两个错误码而不是一个
--------------------------
工具不在（:attr:`ErrorCode.MEDIA_PROBE_FAILED`）与文件坏了
（:attr:`ErrorCode.MEDIA_UNDECODABLE`）的处置完全不同：一个去修环境，一个去换素材。
混成一个码，面板上就只剩一句"探测失败"，而这两种情况恰恰是用户最需要被分开告知的。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError

__all__ = [
    "ALPHA_PIX_FMTS",
    "LOUDNESS_TIMEOUT_SEC",
    "PROBE_TIMEOUT_SEC",
    "CommandResult",
    "MediaInfo",
    "VolumeStats",
    "analyze_volume",
    "extract_thumbnail",
    "ffmpeg_binary",
    "ffprobe_binary",
    "measure_loudness",
    "parse_loudness_output",
    "parse_probe_json",
    "parse_volume_output",
    "probe_media",
    "run_command",
]

#: 一次探测的超时。给 30s 而不是 5s：素材可能在慢盘上，而"探测超时"会让一个
#: 好素材被标红 —— 那比多等 25 秒贵得多（ffprobe 正常情况下是毫秒级）。
PROBE_TIMEOUT_SEC: Final[int] = 30

#: 一次响度测量的超时。比探测宽得多：``loudnorm`` 要**把整条音轨解一遍**，
#: 一首 5 分钟的曲子在一台被渲染占满的机器上不是几毫秒的事。
LOUDNESS_TIMEOUT_SEC: Final[int] = 300

#: 带 alpha 通道的像素格式（水印必须落在这些里，否则叠上去是黑底方块）。
ALPHA_PIX_FMTS: Final[frozenset[str]] = frozenset(
    {
        "rgba",
        "bgra",
        "argb",
        "abgr",
        "ya8",
        "ya16le",
        "ya16be",
        "graya",
        "graya16le",
        "graya16be",
        "gbrap",
        "gbrap10le",
        "gbrap12le",
        "pal8",
    }
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """一次外部命令的结果。``returncode < 0`` 是**我们自己**给的码（见 :func:`run_command`）。"""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, limit: int = 400) -> str:
        """stderr 的尾巴（报错时给人看的那一段）。"""
        text = self.stderr.strip() or self.stdout.strip()
        return text[-limit:]


def run_command(argv: Sequence[str], *, timeout: int = PROBE_TIMEOUT_SEC) -> CommandResult:
    """执行外部命令；**不抛异常**，失败也返回一个 :class:`CommandResult`。

    负的返回码是我们自己给的，与进程真实退出码区分开：

    - ``-1`` 找不到可执行文件（工具没装 / 不在 PATH）
    - ``-2`` 超时
    - ``-3`` 无法启动（权限 / OSError）

    为什么不抛：调用方（探测 / 入库 / 渲染）都要把失败**记进报告**继续跑完其余条目，
    而不是让一个坏文件中断整批扫描。真正的硬门禁（"ffmpeg 在不在"）在 ``doctor`` 那侧。
    """
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return CommandResult(-1, "", f"未找到可执行文件：{argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(-2, "", f"命令超时（{timeout}s）：{' '.join(argv)}")
    except OSError as exc:
        return CommandResult(-3, "", f"命令无法启动：{exc}")
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def ffmpeg_binary(env: Mapping[str, str] | None = None) -> str:
    """``ffmpeg`` 的路径：``STUDIO_FFMPEG_BIN`` > PATH > 裸名字。"""
    return _binary("ffmpeg", env)


def ffprobe_binary(env: Mapping[str, str] | None = None) -> str:
    """``ffprobe`` 的路径：``STUDIO_FFPROBE_BIN`` > PATH > 裸名字。"""
    return _binary("ffprobe", env)


def _binary(name: str, env: Mapping[str, str] | None) -> str:
    """与 ``doctor`` 同一条口径（环境变量可覆盖，便于换一套自带的 ffmpeg）。"""
    source = os.environ if env is None else env
    override = source.get(f"STUDIO_{name.upper()}_BIN")
    if override:
        return override
    return shutil.which(name) or name


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """一个媒体文件的客观事实（**不含任何"合格 / 不合格"判断**）。"""

    path: str
    size_bytes: int
    duration_ms: int
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    pix_fmt: str | None
    sample_rate: int | None
    channels: int | None
    bitrate_kbps: int | None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def has_alpha(self) -> bool:
        """像素格式带不带 alpha（水印判据；见 :data:`ALPHA_PIX_FMTS`）。"""
        return self.pix_fmt is not None and self.pix_fmt.lower() in ALPHA_PIX_FMTS

    @property
    def is_image(self) -> bool:
        """静态图（png / jpeg / webp）—— ffprobe 也把它们报成 video 流，靠时长区分。"""
        return self.has_video and not self.has_audio and self.duration_ms <= 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "duration_ms": self.duration_ms,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "pix_fmt": self.pix_fmt,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "bitrate_kbps": self.bitrate_kbps,
            "has_alpha": self.has_alpha,
            "is_image": self.is_image,
        }


def probe_media(
    path: Path | str,
    *,
    ffprobe: str | None = None,
    timeout: int = PROBE_TIMEOUT_SEC,
    runner: Any = None,
) -> MediaInfo:
    """``ffprobe`` 一次并翻成 :class:`MediaInfo`；失败 ⇒ :class:`StudioError`。

    :param runner: 单测注入点（默认 :func:`run_command`）—— "探测失败怎么报"这件事
        不该靠"把 ffprobe 从 PATH 里拿掉"来验。
    """
    target = Path(path)
    if not target.is_file():
        raise StudioError(
            f"素材文件不存在：{target}",
            code=ErrorCode.MEDIA_UNDECODABLE,
            context={"path": str(target)},
            remediation="确认文件还在（素材目录被手工搬动过？）后重新扫描",
        )
    size = target.stat().st_size
    if size <= 0:
        raise StudioError(
            f"素材是空文件（0 字节）：{target}",
            code=ErrorCode.MEDIA_UNDECODABLE,
            context={"path": str(target)},
            remediation="换一份可解码的文件（0 字节通常是下载中断或复制失败留下的）",
        )

    call = runner or run_command
    result: CommandResult = call(
        [
            ffprobe or ffprobe_binary(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(target),
        ],
        timeout=timeout,
    )
    if not result.ok:
        # 工具跑不起来（负码）与文件解不开（非零码）分开报 —— 见模块 docstring。
        code = ErrorCode.MEDIA_PROBE_FAILED if result.returncode < 0 else ErrorCode.MEDIA_UNDECODABLE
        raise StudioError(
            f"探测失败（rc={result.returncode}）：{target}",
            code=code,
            context={"path": str(target), "stderr": result.tail()},
            remediation=(
                "确认 STUDIO_FFPROBE_BIN / PATH 里有可用的 ffprobe"
                if result.returncode < 0
                else "换一份可解码的素材（截断 / 扩展名与实际格式不符都会这样）"
            ),
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise StudioError(
            f"ffprobe 的输出不是 JSON：{target}",
            code=ErrorCode.MEDIA_PROBE_FAILED,
            context={"path": str(target), "error": str(exc), "stdout": result.stdout[:400]},
            remediation="通常是 ffprobe 版本不兼容；检查 STUDIO_FFPROBE_BIN 指向哪一个",
        ) from exc
    if not isinstance(payload, Mapping):
        raise StudioError(
            f"ffprobe 的输出结构异常：{target}",
            code=ErrorCode.MEDIA_PROBE_FAILED,
            context={"path": str(target)},
            remediation="检查 STUDIO_FFPROBE_BIN",
        )
    return parse_probe_json(payload, path=target, size_bytes=size)


def parse_probe_json(payload: Mapping[str, Any], *, path: Path | str, size_bytes: int) -> MediaInfo:
    """把 ``ffprobe -print_format json`` 的输出翻成 :class:`MediaInfo`（**纯函数**）。

    抽出来的理由很实际：真 ffprobe 的输出形态（``avg_frame_rate`` 是 ``"30000/1001"``
    这种分数字符串、时长既可能在 ``format`` 也可能只在流上、图片根本没有时长）
    是这个模块里最容易写错的一段，而它**不需要真 ffprobe 就能测**。
    """
    streams = payload.get("streams")
    stream_list = [item for item in streams if isinstance(item, Mapping)] if isinstance(streams, list) else []
    video = next((s for s in stream_list if s.get("codec_type") == "video"), None)
    audio = next((s for s in stream_list if s.get("codec_type") == "audio"), None)
    fmt = payload.get("format")
    container: Mapping[str, Any] = fmt if isinstance(fmt, Mapping) else {}

    duration = _duration_ms(container, video)
    return MediaInfo(
        path=str(path),
        size_bytes=size_bytes,
        duration_ms=duration,
        video_codec=_text(video, "codec_name"),
        audio_codec=_text(audio, "codec_name"),
        width=_int(video, "width"),
        height=_int(video, "height"),
        fps=_fps(video),
        pix_fmt=_text(video, "pix_fmt"),
        sample_rate=_int(audio, "sample_rate"),
        channels=_int(audio, "channels"),
        bitrate_kbps=_bitrate_kbps(container, video, audio),
    )


def _duration_ms(container: Mapping[str, Any], video: Mapping[str, Any] | None) -> int:
    """时长（毫秒）：``format.duration`` 优先，退到视频流；都没有 ⇒ 0（图片就是 0）。"""
    for source in (container, video):
        seconds = _float(source, "duration")
        if seconds is not None and seconds > 0:
            return round(seconds * 1000)
    return 0


def _fps(video: Mapping[str, Any] | None) -> float | None:
    """帧率：``avg_frame_rate``（``"30000/1001"``）优先，退到 ``r_frame_rate``。

    ``avg_frame_rate`` 可能是 ``"0/0"``（图片 / 变帧率容器）⇒ 当作"没有帧率"，
    而不是算出 0.0：0.0 会让后面"按帧率算帧数"的代码除以零。
    """
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = video.get(key) if video is not None else None
        if not isinstance(raw, str) or "/" not in raw:
            continue
        numerator, _, denominator = raw.partition("/")
        try:
            top = float(numerator)
            bottom = float(denominator)
        except ValueError:
            continue
        if bottom > 0 and top > 0:
            return round(top / bottom, 4)
    return None


def _bitrate_kbps(
    container: Mapping[str, Any],
    video: Mapping[str, Any] | None,
    audio: Mapping[str, Any] | None,
) -> int | None:
    """码率（kbps）：容器总码率优先；没有就把音视频两路加起来。"""
    total = _int(container, "bit_rate")
    if total:
        return round(total / 1000)
    parts = [_int(video, "bit_rate"), _int(audio, "bit_rate")]
    present = [value for value in parts if value]
    return round(sum(present) / 1000) if present else None


def _text(source: Mapping[str, Any] | None, key: str) -> str | None:
    value = source.get(key) if source is not None else None
    return value if isinstance(value, str) and value else None


def _int(source: Mapping[str, Any] | None, key: str) -> int | None:
    """取整数字段；**字符串也收**（ffprobe 的 ``sample_rate`` 就是字符串）。"""
    value = source.get(key) if source is not None else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _float(source: Mapping[str, Any] | None, key: str) -> float | None:
    value = source.get(key) if source is not None else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class VolumeStats:
    """``volumedetect`` 的两行结论（dBFS，**负数**，越接近 0 越响）。"""

    max_db: float | None
    mean_db: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"max_db": self.max_db, "mean_db": self.mean_db}


def analyze_volume(
    path: Path | str,
    *,
    ffmpeg: str | None = None,
    timeout: int = PROBE_TIMEOUT_SEC,
    runner: Any = None,
) -> VolumeStats:
    """跑一次 ``volumedetect`` 拿峰值与均值（零样本参考音的"无削波"判据 · §4.3.1）。

    为什么用 ``ffmpeg -f null -`` 而不是自己算：削波判据（峰值 ≤ −1.0 dBFS）要的是
    **样本级峰值**，自己读 WAV 只覆盖得了 wav（参考音可能是 mp3 / m4a），而 ffmpeg
    已经把这件事做对了。代价是解一遍全片 —— 参考音只有十几秒，可以接受。
    """
    target = Path(path)
    call = runner or run_command
    result: CommandResult = call(
        [
            ffmpeg or ffmpeg_binary(),
            "-hide_banner",
            "-nostats",
            "-i",
            str(target),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    if not result.ok:
        raise StudioError(
            f"响度分析失败（rc={result.returncode}）：{target}",
            code=ErrorCode.MEDIA_PROBE_FAILED if result.returncode < 0 else ErrorCode.MEDIA_UNDECODABLE,
            context={"path": str(target), "stderr": result.tail()},
            remediation="确认 ffmpeg 可用、文件可解码",
        )
    return parse_volume_output(result.stderr + "\n" + result.stdout)


def parse_volume_output(text: str) -> VolumeStats:
    """从 ``volumedetect`` 的日志里抠出 ``max_volume`` / ``mean_volume``（纯函数）。

    ⚠️ 这两个数在 **stderr**（ffmpeg 的日志走 stderr），且行首带滤镜实例名
    （``[Parsed_volumedetect_0 @ 0x...]``）—— 所以按"最后一个冒号后的数字"抠，
    而不是按固定列位置切。
    """
    max_db = _last_db(text, "max_volume")
    mean_db = _last_db(text, "mean_volume")
    return VolumeStats(max_db=max_db, mean_db=mean_db)


def _last_db(text: str, label: str) -> float | None:
    """取 ``<label>: -1.2 dB`` 里的数（同一段日志里出现多次 ⇒ 取最后一次）。"""
    found: float | None = None
    for line in text.splitlines():
        if label not in line:
            continue
        _, _, tail = line.partition(label)
        _, _, tail = tail.partition(":")
        token = tail.strip().split(" ")[0]
        try:
            found = float(token)
        except ValueError:
            continue
    return found


def extract_thumbnail(
    source: Path | str,
    target: Path | str,
    *,
    at_ms: int = 1_000,
    width: int = 480,
    ffmpeg: str | None = None,
    timeout: int = PROBE_TIMEOUT_SEC,
    runner: Any = None,
) -> Path:
    """抽一帧做缩略图（素材面板的"这张素材长什么样"）。

    ``-ss`` 放在 ``-i`` **前面**：那是"跳过去再解"（关键帧定位，毫秒级）；
    放在后面是"解到那个时间点再丢"（把前面几秒全解一遍）。素材面板一次要出
    几十张缩略图，差别就是"刷一下出来"与"转圈十秒"。

    ``scale=<width>:-2``：宽度定死、高度按比例（``-2`` 而不是 ``-1`` ——
    ``-1`` 会算出奇数高度，而 h264 的 yuv420p 要求偶数）。
    """
    destination = Path(target)
    result: CommandResult = (runner or run_command)(
        [
            ffmpeg or ffmpeg_binary(),
            "-hide_banner",
            "-nostats",
            "-ss",
            f"{max(0, at_ms) / 1000:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "3",
            "-y",
            str(destination),
        ],
        timeout=timeout,
    )
    if not result.ok or not destination.is_file():
        raise StudioError(
            f"缩略图生成失败（rc={result.returncode}）：{source}",
            code=ErrorCode.MEDIA_PROBE_FAILED if result.returncode < 0 else ErrorCode.MEDIA_UNDECODABLE,
            context={"source": str(source), "target": str(destination), "stderr": result.tail()},
            remediation="确认 ffmpeg 可用、素材可解码（缩略图失败不阻塞入库，只是没有预览图）",
        )
    return destination


def measure_loudness(
    path: Path | str,
    *,
    ffmpeg: str | None = None,
    timeout: int = LOUDNESS_TIMEOUT_SEC,
    runner: Any = None,
) -> float | None:
    """量一次整体响度（LUFS · §3.3.14 的 ``bgm_tracks.loudness_lufs``）。

    用 ``loudnorm`` 的**第一遍**（``print_format=json``）而不是 ``ebur128``：
    它输出的就是第二遍要喂回去的 ``input_i``，同一个滤镜的两遍用的是同一套口径
    —— 换成另一个滤镜量，混音时按这个数对齐就会差零点几个 LU。

    量不出来（老 ffmpeg / 输出格式变了）⇒ ``None`` 而不是抛：响度是**用来让混音
    更均匀**的，不是能不能用的判据（§3.3.14 对 BGM 的硬判据只有时长与可解码）。
    """
    target = Path(path)
    result: CommandResult = (runner or run_command)(
        [
            ffmpeg or ffmpeg_binary(),
            "-hide_banner",
            "-nostats",
            "-i",
            str(target),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    if not result.ok:
        raise StudioError(
            f"响度测量失败（rc={result.returncode}）：{target}",
            code=ErrorCode.MEDIA_PROBE_FAILED if result.returncode < 0 else ErrorCode.MEDIA_UNDECODABLE,
            context={"path": str(target), "stderr": result.tail()},
            remediation="确认 ffmpeg 可用、文件可解码",
        )
    return parse_loudness_output(result.stderr + "\n" + result.stdout)


def parse_loudness_output(text: str) -> float | None:
    """从 ``loudnorm`` 的日志里抠出 ``input_i``（纯函数）。

    抠法刻意**不解析整段 JSON**：ffmpeg 的日志里混着普通文本与 ``[Parsed_...]``
    前缀，整段当 JSON 解会失败。这里只找键名，取**最后一次**出现（同一段日志里
    可能打印多次）。
    """
    found: float | None = None
    for line in text.splitlines():
        if "input_i" not in line:
            continue
        _, _, tail = line.partition("input_i")
        _, _, tail = tail.partition(":")
        # 取"冒号到下一个逗号"这一段：loudnorm 的 JSON 一行可能只有这一个键
        # （多行缩进打印），也可能整段挤在一行（`{"input_i" : "-23.5", "input_tp" ...}`）。
        token = tail.split(",")[0].strip().strip('"')
        if token in {"-inf", "inf", "nan", ""}:
            continue
        try:
            found = float(token)
        except ValueError:
            continue
    return found
