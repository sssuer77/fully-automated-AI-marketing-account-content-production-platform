"""CosyVoice2 推理后端（T2.2 · §04.3.5）—— 模型加载 / 卸载 / 零样本合成。

这一层**只有推理**，没有 HTTP、没有音色注册表
------------------------------------------------
``server.py`` 负责协议（HTTP、并发、背压、空闲卸载），这一层负责"把一句文本变成
一段 WAV"。分开的理由很实际：**HTTP 那部分能在没有 GPU 的机器上测**，推理这部分
只能在真机上测 —— 混在一个文件里，前者就永远测不了。

为什么 torch / cosyvoice 都在函数里 import
------------------------------------------
主 venv 是 Python 3.12 且**没有 torch**（torch 2.4.0+cu121 只有 cp311 wheel，
见 §01.6.2）。而这个模块必须能被主 venv import —— 服务管理器的就绪判据
（``importlib.util.find_spec``）、单元测试都在主 venv 里跑。模块级 ``import torch``
会让"没有 GPU 的机器连 import 都过不去"，那等于把整条链路焊死在 GPU 上。
所以：**模块级只 import 标准库**，torch / cosyvoice 在 :meth:`CosyVoiceBackend.load`
里才进来。

PYTHONPATH 的两段（陷阱 160）
-----------------------------
``source_dir`` 与 ``matcha_dir`` **两段都要**加进 ``sys.path``：前者是 CosyVoice
本体，后者是它的子模块 Matcha-TTS（``cosyvoice.flow.flow_matching`` 依赖 ``matcha``
包）。少了第二段报的是 ``No module named 'matcha'`` —— 看上去完全不像路径问题。

三个非显而易见的接口事实（都踩过，见 runbook）
----------------------------------------------
1. ``inference_zero_shot`` 的第三参是**参考音路径**，不是加载好的张量（陷阱 161）；
2. 该 revision 的 ``torchaudio.save`` 在本环境报 ``Invalid file`` ⇒ 落盘用
   ``soundfile.write``；
3. ``text_frontend=False``：归一化是 T2.5 的活（幂等 + glossary），这里再跑一遍
   上游前端等于把已经定好的读法又改一次。
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger

__all__ = [
    "CosyVoiceBackend",
    "EngineState",
    "LoadResult",
    "SynthResult",
    "cuda_free_mb",
    "looks_like_oom",
    "peak_trim_gain",
]

logger = get_logger("studio.tts.cosyvoice")

#: 判定 OOM 的关键词（torch 抛的异常类型在不同版本里不一样，消息反而稳定）
_OOM_MARKERS: Final[tuple[str, ...]] = (
    "out of memory",
    "cuda error: out of memory",
    "cublas_status_alloc_failed",
)

#: 落盘前给产物留的峰值上限（dBFS）。
#:
#: 为什么引擎自己产出的东西还要压（真机 2026-09-22）
#: ------------------------------------------------
#: CosyVoice2 的零样本输出是**峰值归一化**的：用户导入 `sunxiaochuan`（参考音峰值
#: −6.0 dBFS）之后，念出来每一句都是 **−0.1 dBFS**。而句子级的爆音门禁是
#: 「峰值 > −0.5 dBFS ⇒ ``TTS_CLIP``」（§04.3.3）—— 于是**每一句**都被判破音：
#: 重试 3 次（同一句文本同一个音色，输出当然一样）⇒ 连续 3 次失败 ⇒ 配音熔断打开
#: ⇒ 后面每一句都只查缓存、查不到就降级成静音占位。用户看到的是一整条稿子
#: 「18 句跳过」，日志里一句人话都没有。
#:
#: 判据没错（混音那一步要的是**有余量**的人声轨），错的是引擎的输出电平：
#: 它没有余量。所以这里补的就是那个余量 —— 与成片门禁的 −1.0 dBTP 同一个数。
OUTPUT_PEAK_CEILING_DBFS: Final[float] = -1.0


class EngineState(StrEnum):
    """模型的状态（``/health`` 如实报它，不报"一切正常"）。"""

    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


def looks_like_oom(exc: BaseException) -> bool:
    """这个异常是不是显存不够（R4 的自动降并发据此判定）。"""
    if type(exc).__name__ in {"OutOfMemoryError", "CudaOutOfMemoryError"}:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _OOM_MARKERS)


def peak_trim_gain(peak: float, *, ceiling_dbfs: float = OUTPUT_PEAK_CEILING_DBFS) -> float:
    """峰值超过上限 ⇒ 返回把它压到上限的那个**线性增益**；否则返回 ``1.0``。

    **只压不抬**：引擎自己念得轻（真机上另一支音色只有 −24 dBFS 峰值）是素材的
    事，不是这里该管的 —— 抬电平会把底噪一起抬起来，而且会让同一支片子里前后两句
    的响度关系反过来。

    纯函数（不碰 numpy / torch）：门禁与它都是"数字进、数字出"，这样这一条能在
    没有 GPU 的主 venv 里被钉死（见 ``tests/unit/tts/test_cosyvoice_backend.py``）。
    """
    # 标注类型不是仪式：mypy 眼里 `float ** float` 可能是复数（`__pow__` 的重载里有它），
    # 于是这一行会推成 `Any`，整个函数就变成"从 Any 返回 float"。
    ceiling: float = 10.0 ** (ceiling_dbfs / 20.0)
    if peak <= ceiling:
        return 1.0
    return ceiling / peak


def cuda_free_mb() -> int | None:
    """当前空闲显存（MB）；没有 CUDA 时返回 ``None``。"""
    try:
        import torch  # noqa: PLC0415  （只在真有 torch 的环境里才可用）
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    free_bytes, _total = torch.cuda.mem_get_info()
    return int(free_bytes // (1024 * 1024))


@dataclass(frozen=True, slots=True)
class LoadResult:
    """一次加载的读数（``/warmup`` 与 ``/health`` 都回它）。"""

    load_ms: int
    device: str
    revision: str
    sample_rate: int
    fp16: bool
    vram_mb: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "load_ms": self.load_ms,
            "device": self.device,
            "revision": self.revision,
            "sample_rate": self.sample_rate,
            "fp16": self.fp16,
            "vram_mb": self.vram_mb,
        }


@dataclass(frozen=True, slots=True)
class SynthResult:
    """一句合成的读数（RTF 是"合成耗时 / 音频时长"，标定并发就看它）。"""

    duration_ms: int
    sample_rate: int
    synth_ms: int
    rtf: float
    segments: int

    def to_dict(self) -> dict[str, object]:
        return {
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "synth_ms": self.synth_ms,
            "rtf": self.rtf,
            "segments": self.segments,
        }


class CosyVoiceBackend:
    """CosyVoice2 的加载 / 卸载 / 零样本合成。

    :param model_dir: 权重目录（含 ``cosyvoice2.yaml``）
    :param revision: 权重 revision（**留痕**：进 ``/health`` 与日志，Q7）
    :param source_dir: CosyVoice 源码目录（进 ``sys.path``）
    :param matcha_dir: Matcha-TTS 子模块目录（进 ``sys.path``，陷阱 160）
    :param fp16: 半精度常驻（Turing 上 bf16 不可用，R4）
    :param sample_rate: 期望采样率（与权重一致，不一致说明指错了目录）
    """

    def __init__(
        self,
        model_dir: Path,
        *,
        revision: str,
        source_dir: Path | None = None,
        matcha_dir: Path | None = None,
        fp16: bool = True,
        sample_rate: int = 24_000,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.revision = revision
        self.source_dir = Path(source_dir) if source_dir is not None else None
        self.matcha_dir = Path(matcha_dir) if matcha_dir is not None else None
        self.fp16 = fp16
        self.expected_sample_rate = sample_rate
        self._model: Any = None
        self._state: EngineState = EngineState.UNLOADED
        self._last_error: str | None = None
        self._load_result: LoadResult | None = None

    # ── 只读 ────────────────────────────────────────────────────────────

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def load_result(self) -> LoadResult | None:
        return self._load_result

    @property
    def sample_rate(self) -> int:
        if self._model is not None:
            return int(self._model.sample_rate)
        return self.expected_sample_rate

    @property
    def device(self) -> str:
        """生效设备。**没加载时也如实回答**（探活不该要求先把模型读进来）。"""
        try:
            import torch  # noqa: PLC0415
        except ImportError:
            return "unavailable"
        return "cuda" if torch.cuda.is_available() else "cpu"

    # ── 加载 / 卸载 ─────────────────────────────────────────────────────

    def _check_paths(self) -> None:
        if not self.model_dir.is_dir():
            raise StudioError(
                f"模型权重目录不存在：{self.model_dir}",
                code=ErrorCode.TTS_ENGINE_UNAVAILABLE,
                context={
                    "model_dir": str(self.model_dir),
                    "revision": self.revision,
                    "task": "T2.1",
                },
                remediation=(
                    "按 docs/runbook/tts_models.md 第二节重新下载权重，"
                    "或改 config/tts.yaml 的 model.dir 指向已有目录"
                ),
            )
        for label, path in (("source_dir", self.source_dir), ("matcha_dir", self.matcha_dir)):
            if path is not None and not path.is_dir():
                raise StudioError(
                    f"CosyVoice {label} 不存在：{path}",
                    code=ErrorCode.TTS_ENGINE_UNAVAILABLE,
                    context={"field": label, "path": str(path)},
                    remediation="按 docs/runbook/tts_models.md 第二节重新 clone 源码",
                )

    def _extend_sys_path(self) -> None:
        """把源码与 Matcha-TTS 加进 ``sys.path``（**两段都要**，陷阱 160）。"""
        for path in (self.source_dir, self.matcha_dir):
            if path is None:
                continue
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)

    def _import_engine(self) -> Any:
        """导入 torch 与 CosyVoice2，并确认 CUDA 真的可用。

                两件事都在**函数内**做（模块级只 import 标准库，见模块 docstring）：
        没有 GPU 的机器上这个模块仍然 import 得动，只是 :meth:`load` 会如实报错。
        """
        self._extend_sys_path()
        try:
            import torch  # noqa: PLC0415
            from cosyvoice.cli.cosyvoice import CosyVoice2  # noqa: PLC0415
        except ImportError as exc:
            self._state = EngineState.ERROR
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise StudioError(
                f"推理依赖缺失：{self._last_error}",
                code=ErrorCode.TTS_ENGINE_UNAVAILABLE,
                context={"model_dir": str(self.model_dir), "source_dir": str(self.source_dir)},
                remediation="tts 进程要用 tts/.venv 的解释器起（config/tts.yaml 的 python），"
                "依赖清单见 tts/requirements-cosyvoice.txt",
            ) from exc
        if not torch.cuda.is_available():
            self._state = EngineState.ERROR
            self._last_error = "CUDA 不可用"
            raise StudioError(
                "CUDA 不可用 —— CosyVoice 常驻服务需要 GPU",
                code=ErrorCode.TTS_ENGINE_UNAVAILABLE,
                context={"device": "cpu", "torch": torch.__version__},
                remediation="检查显卡驱动与 tts/.venv 里的 torch 是否 cu121 版",
            )
        return CosyVoice2

    def load(self) -> LoadResult:
        """加载模型（**幂等**：已就绪时直接返回上次读数，不重读 5 GB）。"""
        if self._state is EngineState.READY and self._model is not None:
            assert self._load_result is not None
            return self._load_result

        self._state = EngineState.LOADING
        started = time.monotonic()
        try:
            self._check_paths()
            engine_cls = self._import_engine()
            model = engine_cls(
                str(self.model_dir),
                load_jit=False,
                load_trt=False,
                load_vllm=False,
                fp16=self.fp16,  # bf16 在 Turing 上不可用（R4）
            )
        except StudioError as exc:
            # 权重目录不在（:meth:`_check_paths`）与依赖缺失（:meth:`_import_engine`）
            # 都要落到 ``ERROR``：停在 ``UNLOADED`` 会让这个实例看起来像**睡着的健康
            # 实例**（叫得醒），停在 ``LOADING`` 会让它看起来像"还在加载"—— 两种都会
            # 被当成"等一下就好"，于是每一句都白试一遍才失败（陷阱 168）。
            self._state = EngineState.ERROR
            self._last_error = exc.message
            raise
        except BaseException as exc:
            self._state = EngineState.ERROR
            self._last_error = f"{type(exc).__name__}: {exc}"
            code = ErrorCode.TTS_OOM if looks_like_oom(exc) else ErrorCode.TTS_ENGINE_UNAVAILABLE
            raise StudioError(
                f"CosyVoice 加载失败：{self._last_error}",
                code=code,
                context={
                    "model_dir": str(self.model_dir),
                    "revision": self.revision,
                    "fp16": self.fp16,
                },
                remediation=(
                    "显存不足 ⇒ 先 /unload 再试，或关掉占显存的程序（R4）；其它错误看 data/logs/tts.log"
                ),
            ) from exc

        self._model = model
        self._state = EngineState.READY
        self._last_error = None
        load_ms = int((time.monotonic() - started) * 1000)
        used = _used_vram_mb()
        self._load_result = LoadResult(
            load_ms=load_ms,
            device="cuda",
            revision=self.revision,
            sample_rate=int(model.sample_rate),
            fp16=self.fp16,
            vram_mb=used,
        )
        logger.info(
            "cosyvoice.loaded",
            load_ms=load_ms,
            revision=self.revision,
            sample_rate=int(model.sample_rate),
            fp16=self.fp16,
            vram_mb=used,
        )
        return self._load_result

    def unload(self) -> int:
        """卸载模型并释放显存，返回**释放的 MB**（拿不到读数时返回 0）。

        幂等：本来就没加载 ⇒ 返回 0，不报错。空闲策略会反复调它。
        """
        if self._model is None:
            self._state = EngineState.UNLOADED
            return 0
        before = _used_vram_mb()
        self._model = None
        self._load_result = None
        try:
            import torch  # noqa: PLC0415

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:  # pragma: no cover  （没 torch 时本来也没有模型）
            pass
        after = _used_vram_mb()
        freed = max(0, (before or 0) - (after or 0))
        self._state = EngineState.UNLOADED
        logger.info("cosyvoice.unloaded", freed_mb=freed)
        return freed

    # ── 合成 ────────────────────────────────────────────────────────────

    def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        ref_wav: Path,
        ref_text: str,
        speed: float = 1.0,
    ) -> SynthResult:
        """零样本合成一句，落盘 WAV，返回实测读数。

        :raises StudioError: ``TTS_VOICE_MISSING`` 参考音不在 /
            ``TTS_OOM`` 显存不够 / ``TTS_SENTENCE_FAILED`` 其它合成失败
        """
        if not ref_wav.is_file():
            raise StudioError(
                f"参考音不存在：{ref_wav}",
                code=ErrorCode.TTS_VOICE_MISSING,
                context={"ref_wav": str(ref_wav)},
                remediation="按 §4.3.1 把 2–3 段 2–30 秒的原声放进 data/voice_src/<音色>/",
            )
        if not ref_text.strip():
            raise StudioError(
                f"参考文本为空：{ref_wav.parent}",
                code=ErrorCode.TTS_VOICE_MISSING,
                context={"ref_wav": str(ref_wav)},
                remediation="在同目录的 ref.txt 里写上与第 1 段参考音**逐字**对应的那句话",
            )

        self.load()
        import soundfile as sf  # noqa: PLC0415
        import torch  # noqa: PLC0415

        started = time.monotonic()
        try:
            chunks = list(
                self._model.inference_zero_shot(
                    text,
                    ref_text,
                    str(ref_wav),  # ← **路径**，不是张量（陷阱 161）
                    stream=False,
                    speed=speed,
                    text_frontend=False,  # 归一化是 T2.5 的活
                )
            )
            audio = torch.cat([chunk["tts_speech"] for chunk in chunks], dim=1) if chunks else None
            samples = audio.squeeze(0).to(torch.float32).cpu().numpy() if audio is not None else None
        except BaseException as exc:
            code = ErrorCode.TTS_OOM if looks_like_oom(exc) else ErrorCode.TTS_SENTENCE_FAILED
            raise StudioError(
                f"合成失败：{type(exc).__name__}: {exc}",
                code=code,
                context={"text_len": len(text), "voice_ref": ref_wav.name},
                remediation=("显存不足 ⇒ 降并发（pools.yaml）或先 /unload；其它错误看 data/logs/tts.log"),
            ) from exc
        if samples is None:
            raise StudioError(
                "引擎没有产出任何音频",
                code=ErrorCode.TTS_SENTENCE_FAILED,
                context={"text_len": len(text)},
                remediation="看 data/logs/tts.log 里这一句的引擎日志",
            )

        # 留余量（见 `OUTPUT_PEAK_CEILING_DBFS`）：模型输出是峰值归一化的，
        # 原样落盘会让每一句都顶在 0 dBFS 上，被句子级爆音门禁判成破音。
        peak = float(abs(samples).max())
        gain = peak_trim_gain(peak)
        if gain < 1.0:
            samples = samples * gain
            logger.info(
                "cosyvoice.peak_trimmed",
                peak_dbfs=round(20.0 * math.log10(peak), 2),
                gain_db=round(20.0 * math.log10(gain), 2),
                ceiling_dbfs=OUTPUT_PEAK_CEILING_DBFS,
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        # soundfile 而不是 torchaudio.save：后者在这个环境报 Invalid file（陷阱 161）
        sf.write(str(out_path), samples, self.sample_rate)
        synth_ms = int((time.monotonic() - started) * 1000)
        duration_ms = int(len(samples) / self.sample_rate * 1000)
        rtf = round(synth_ms / duration_ms, 3) if duration_ms > 0 else 0.0
        return SynthResult(
            duration_ms=duration_ms,
            sample_rate=self.sample_rate,
            synth_ms=synth_ms,
            rtf=rtf,
            segments=len(chunks),
        )


def _used_vram_mb() -> int | None:
    """当前**已分配**显存（MB）；没有 CUDA 时 ``None``。"""
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    return int(torch.cuda.memory_allocated() // (1024 * 1024))
