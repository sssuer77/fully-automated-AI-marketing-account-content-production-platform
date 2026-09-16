"""Windows SAPI 语音合成 —— 配音的**本地可用**引擎（T2.x 的可跑通替代）。

为什么先用 SAPI 而不是 CosyVoice
--------------------------------
CosyVoice 需要 2–4 GB 权重 + 源码（T2.1），本机现在两样都没有 ⇒ 在它们到位之前
配音环节会**整段缺席**，也就没有第一支 MP4。SAPI（``System.Speech``）是 Windows
自带的：零下载、零显存占用、中文音色现成（Huihui / Yaoyao / Kangkang）。
音质不如 CosyVoice —— 但**能出片**。CosyVoice 到位后按同一接口替换即可。

实现方式：起一个 PowerShell 子进程调 ``SpeechSynthesizer``。
不引 ``pywin32`` / ``comtypes``：只为读一个 COM 对象拖一份二进制依赖不划算，
而 PowerShell 在 Windows 上一定在。

文本怎么传给子进程：**走临时文件，不走命令行**
----------------------------------------------
口播文本里有引号、换行、破折号、emoji。把它拼进命令行，任何一层（Python 参数
转义 / PowerShell 解析 / cmd）都可能吃掉一个字符，而且报错时看不出是哪一层干的。
写进临时文件、让脚本 ``ReadAllText``，这条路径上没有任何转义。
"""

from __future__ import annotations

import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.media import run_command

__all__ = [
    "DEFAULT_RATE",
    "DEFAULT_VOICE_PREFERENCE",
    "SAPI_TIMEOUT_SEC",
    "VOICES_CACHE_SEC",
    "list_voices",
    "list_voices_cached",
    "sapi_available",
    "synthesize",
]

#: 一次合成的超时（长句 + 冷启动 PowerShell 约 2–4 秒，给足余量）。
SAPI_TIMEOUT_SEC: Final[int] = 180

#: 语速（SAPI 的 ``Rate`` 范围 -10..10，0 = 正常）。口播略快一点更像短视频。
DEFAULT_RATE: Final[int] = 1

#: 中文音色的偏好顺序（按"听起来像口播"排）。第一个装了的就用它。
DEFAULT_VOICE_PREFERENCE: Final[tuple[str, ...]] = (
    "Microsoft Huihui Desktop",
    "Microsoft Huihui",
    "Microsoft Yaoyao",
    "Microsoft Kangkang",
)

#: 音色清单的缓存时长（秒）。列音色要起一个 PowerShell 子进程（约 1–2 秒），
#: 而面板每打开一次就要问一次"有哪些音色" —— 不缓存的话，点开面板先卡两秒。
#: 音色是**装语音包才会变**的环境事实，5 分钟的陈旧窗口完全够用。
VOICES_CACHE_SEC: Final[int] = 300

#: 合成脚本：读文本文件 → 写 WAV。参数走 ``-File`` 的命名参数，文本不经过命令行。
_SCRIPT: Final[str] = """param(
    [Parameter(Mandatory=$true)][string]$TextFile,
    [Parameter(Mandatory=$true)][string]$OutFile,
    [string]$Voice = "",
    [int]$Rate = 0
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($Voice) { $synth.SelectVoice($Voice) }
    $synth.Rate = $Rate
    $synth.SetOutputToWaveFile($OutFile)
    $text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
    $synth.Speak($text)
} finally {
    $synth.Dispose()
}
"""


def _powershell() -> str:
    """``powershell.exe`` 的路径（Windows 自带；找不到就让 :func:`sapi_available` 说实话）。"""
    return "powershell"


def list_voices() -> list[str]:
    """本机已安装的音色名（装了什么说什么；读不到 ⇒ 空列表，**不抛**）。"""
    result = run_command(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Add-Type -AssemblyName System.Speech; "
            "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
            ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }",
        ],
        timeout=60,
    )
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def sapi_available() -> bool:
    """本机能不能用 SAPI 合成（有没有音色）。"""
    return bool(list_voices())


@lru_cache(maxsize=1)
def _voices_snapshot(at: int) -> tuple[str, ...]:
    """按"时间桶"缓存音色清单（``at`` 是 ``now // TTL``）。

    用参数化的 ``lru_cache`` 而不是自己拿一个全局变量 + 时间戳：桶号一变就是新的
    一次调用，旧的那份由 ``maxsize=1`` 自然淘汰 —— 少一处"什么时候该失效"的手写判断。
    """
    return tuple(list_voices())


def list_voices_cached() -> tuple[str, ...]:
    """音色清单（带 TTL 缓存）。面板与 API 用它，别直接调 :func:`list_voices`。"""
    return _voices_snapshot(int(time.monotonic() // VOICES_CACHE_SEC))


def pick_voice(preferred: tuple[str, ...] = DEFAULT_VOICE_PREFERENCE) -> str | None:
    """按偏好顺序挑一个装了的音色；一个都没有 ⇒ ``None``（调用方报错说清楚）。"""
    installed = list_voices()
    if not installed:
        return None
    lowered = {name.lower(): name for name in installed}
    for candidate in preferred:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return installed[0]


def synthesize(
    text: str,
    out_path: Path,
    *,
    voice: str | None = None,
    rate: int = DEFAULT_RATE,
    timeout: int = SAPI_TIMEOUT_SEC,
) -> Path:
    """把一段文本合成成 WAV，返回落盘路径。

    ``text`` 为空 ⇒ 报错：合成一段空文本会得到一个**能播但没声音**的 wav，
    而它在下游看起来"成功"了 —— 那是最坏的一种失败。
    """
    if not text.strip():
        raise StudioError(
            "要合成的文本为空",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"out_path": out_path.as_posix()},
            remediation="检查切分这一步：空句不该走到合成（split_for_tts 会滤掉空片段）",
        )

    chosen = voice or pick_voice()
    if chosen is None:
        raise StudioError(
            "本机没有可用的 SAPI 音色，配音无法进行",
            code=ErrorCode.TTS_ENGINE_UNAVAILABLE,
            context={"engine": "sapi", "voice_preference": list(DEFAULT_VOICE_PREFERENCE)},
            remediation=(
                "在「设置 → 时间和语言 → 语音」里装一个中文语音包（如 Microsoft Huihui），"
                "或改用 `--voice` 指定一个已装音色"
            ),
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="studio_sapi_") as tmp:
        text_file = Path(tmp) / "text.txt"
        text_file.write_text(text, encoding="utf-8")
        script = Path(tmp) / "synth.ps1"
        script.write_text(_SCRIPT, encoding="utf-8")
        result = run_command(
            [
                _powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-TextFile",
                str(text_file),
                "-OutFile",
                str(out_path),
                "-Voice",
                chosen,
                "-Rate",
                str(rate),
            ],
            timeout=timeout,
        )

    if not result.ok or not out_path.is_file() or out_path.stat().st_size == 0:
        raise StudioError(
            f"SAPI 合成失败（rc={result.returncode}）：{result.tail()}",
            code=ErrorCode.TTS_SENTENCE_FAILED,
            context={"voice": chosen, "text": text[:80], "out_path": out_path.as_posix()},
            remediation="确认音色名拼写正确（`studio voice voices` 列出已装音色）",
        )
    return out_path
