"""CosyVoice 常驻推理服务（T2.2 · §04.3.5）—— 一句进、一段 WAV 出。

它为什么是一个**独立进程**
--------------------------
模型常驻显存与"每句重载一次"是 10 秒与 0.01 秒的差别（陷阱 #10：首句 20s+）。
而它必须与主进程分开：torch 2.4.0+cu121 只装得进 Python 3.11 的 ``tts/.venv``，
主 venv 是 3.12（§01.6.2）。所以这里是一个**只干推理**的 HTTP 服务，
业务侧通过 ``127.0.0.1:8788`` 用它（T2.3 的引擎路由接上去）。

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| GET | ``/health`` | 探活（supervisor 与 doctor 都看它） |
| POST | ``/warmup`` | 加载 + 念一句短句，把首句延迟压到 <5s |
| POST | ``/unload`` | 释放显存（空闲策略与排障都调它） |
| GET | ``/voices`` | 本机**这台引擎能克隆**的音色 |
| POST | ``/synth`` | 按句合成 |

三条硬规矩
----------
1. **单并发 + 背压**：GPU 串行（信号量），排队超过 ``queue_max`` 直接 429 ——
   排队雪崩比快速失败更糟：超时全堆在最后一秒，客户端连"该退避"都看不出来。
2. **``out_path`` 不许逃出 ``data/``**：绝对路径与 ``..`` 一律拒（§04.3.5 路径穿越防护）。
3. **不画假数据**：``POST``/``DELETE /voices`` 返回 **501** 并说清注册在哪
   （T2.4 的 ``scripts/ingest_voice_src.py`` 与素材库面板）。在这里再实现一遍入库，
   等于把"四条硬拒 + R2 留痕"复制成两份 —— 两份判定迟早对不上（陷阱 #150 同族）。

"就绪"与"已加载"是两件事
------------------------
``ok`` = 进程活着（supervisor 看这个）；``ready`` = 模型在显存里（业务看这个）。
混成一个字段的后果：进程活着但模型没加载时，探活说"就绪"、第一句却要等 10 秒。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from studio.core.config import TtsConfig, TtsServerConfig, load_tts_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.tts.cosyvoice import CosyVoiceBackend, EngineState

__all__ = [
    "ENGINE_NAME",
    "TtsService",
    "VoiceRegistry",
    "build_service",
    "create_app",
    "resolve_out_path",
    "serve",
]

logger = get_logger("studio.tts.server")

#: 引擎名（进缓存键与 ``/health``；换引擎必须换这个名字，好让旧缓存整体失效）
ENGINE_NAME: Final[str] = "cosyvoice2"

#: 音色 id 的字符集（与素材库入库同一条：目录名就是 id）
VOICE_ID_PATTERN: Final[str] = r"^[A-Za-z0-9_]{1,64}$"

#: 空闲看门狗的 tick（秒）。比 idle_unload_min 小两个数量级就够 ——
#: 它只决定"最多晚多久卸载"，而卸载本身不赶时间。
IDLE_TICK_SEC: Final[float] = 30.0

#: 错误码 ⇒ HTTP 状态（与 ``app/errors.py`` 同一口径；这里只列本服务会抛的）
STATUS_BY_CODE: Final[dict[ErrorCode, int]] = {
    ErrorCode.TTS_VOICE_MISSING: 422,
    ErrorCode.TTS_BUSY: 429,
    ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS: 400,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.TTS_OOM: 503,
    ErrorCode.TTS_ENGINE_UNAVAILABLE: 503,
    ErrorCode.TTS_SENTENCE_FAILED: 500,
    ErrorCode.INTERNAL: 500,
}


# ══════════════════════════════════════════════════════════════════════
# 线上模型（请求 / 响应）
# ══════════════════════════════════════════════════════════════════════


class HealthResponse(BaseModel):
    """``GET /health``。字段名与验收口径一致：``ready`` / ``device`` / ``model_state``。

    ``pid`` 是**给自己人看的**：编排器要能认出「8788 上那个进程是我的哪一个实例」。
    光凭端口占用查不出来 —— 而「端口被一个坏掉的旧实例占着」正是最费时间的那类故障：
    旧实例每句都念不出声，探活却回 200（陷阱 166）。
    """

    ok: bool = True
    pid: int
    ready: bool
    device: str
    model_state: str
    engine: str = ENGINE_NAME
    revision: str
    fp16: bool
    sample_rate: int
    concurrency: int
    queue_max: int
    in_flight: int
    idle_unload_min: int
    idle_sec: float | None = None
    vram_mb: int | None = None
    load_ms: int | None = None
    detail: str | None = None


class WarmupResponse(BaseModel):
    """``POST /warmup`` —— 验收要的 ``{ready: true, load_ms: int}`` 在这里。"""

    ok: bool = True
    ready: bool
    load_ms: int
    device: str
    model_state: str
    revision: str
    sample_rate: int
    synth_ms: int | None = None
    vram_mb: int | None = None


class UnloadResponse(BaseModel):
    ok: bool = True
    unloaded: bool
    freed_mb: int
    model_state: str


class VoiceInfo(BaseModel):
    """一个可克隆的音色（**这台引擎能不能念**，不是"库里有没有"）。"""

    id: str
    ref_wavs: list[str]
    ref_text: str | None
    origin: str | None = None
    note: str | None = None
    usable: bool
    detail: str


class VoicesResponse(BaseModel):
    ok: bool = True
    engine: str = ENGINE_NAME
    revision: str
    voices: list[VoiceInfo]


class SynthRequest(BaseModel):
    """``POST /synth``。

    ``out_path`` 是**相对 ``data/`` 的 POSIX 路径**；不给 ⇒ 由 ``task_id`` + ``seq``
    推出来（``data/work/<task_id>/tts/s00N.wav``，与 T2.6 的落盘约定一致）。
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    voice_id: str = Field(pattern=VOICE_ID_PATTERN)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    seq: int | None = Field(default=None, ge=0, le=99_999)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    out_path: str | None = Field(default=None, min_length=1, max_length=512)


class SynthResponse(BaseModel):
    ok: bool = True
    out_path: str
    duration_ms: int
    sample_rate: int
    synth_ms: int
    rtf: float
    segments: int
    queue_wait_ms: int
    engine: str = ENGINE_NAME
    engine_revision: str
    voice_id: str
    ref_wav: str


# ══════════════════════════════════════════════════════════════════════
# 音色注册表（读盘，不读库）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class VoiceRef:
    """一次克隆要的三样东西：参考音路径 + 与它**逐字**对应的文本 + 音色 id。"""

    voice_id: str
    ref_wav: Path
    ref_text: str


class VoiceRegistry:
    """``data/voice_src/<id>/`` 的只读视图。

    为什么读**盘**而不是读 ``voice_profiles`` 表：这个进程的判据是"**我念得出来吗**"，
    而那取决于参考音文件在不在这台机器上。库里的行可能是别的机器入库的
    （陷阱 #154 就是这么来的：库里有行、引擎念不出来）。
    """

    def __init__(self, voice_src_dir: Path) -> None:
        self.voice_src_dir = Path(voice_src_dir)

    def list(self) -> list[VoiceInfo]:
        if not self.voice_src_dir.is_dir():
            return []
        found: list[VoiceInfo] = []
        for child in sorted(self.voice_src_dir.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            found.append(self._describe(child))
        return found

    def _describe(self, root: Path) -> VoiceInfo:
        wavs = sorted(root.glob("ref_*.wav"))
        text = _first_line(root / "ref.txt")
        origin, note = _read_profile(root / "profile.json")
        if not wavs:
            detail = "目录里没有 ref_*.wav"
        elif text is None:
            detail = "缺 ref.txt（参考文本）—— 没有它就没法零样本克隆"
        else:
            detail = "可用"
        return VoiceInfo(
            id=root.name,
            ref_wavs=[item.name for item in wavs],
            ref_text=text,
            origin=origin,
            note=note,
            usable=bool(wavs) and text is not None,
            detail=detail,
        )

    def resolve(self, voice_id: str) -> VoiceRef:
        """音色 id ⇒ 参考音与参考文本（缺什么就说什么，不猜）。

        :raises StudioError: ``TTS_VOICE_MISSING``（附 ``context.available``）
        """
        root = self.voice_src_dir / voice_id
        wavs = sorted(root.glob("ref_*.wav")) if root.is_dir() else []
        text = _first_line(root / "ref.txt")
        if not wavs or text is None:
            available = [item.id for item in self.list() if item.usable]
            missing = "目录" if not root.is_dir() else ("参考音" if not wavs else "ref.txt")
            raise StudioError(
                f"音色 {voice_id} 在这台引擎上念不出来（缺{missing}）",
                code=ErrorCode.TTS_VOICE_MISSING,
                context={
                    "voice_id": voice_id,
                    "voice_dir": str(root),
                    "missing": missing,
                    "available": available,
                },
                remediation=(
                    "按 §4.3.1 放进 2–3 段 10–30 秒原声 + ref.txt（逐字文本），"
                    "再跑 scripts/ingest_voice_src.py；换音色不必改代码"
                ),
            )
        return VoiceRef(voice_id=voice_id, ref_wav=wavs[0], ref_text=text)


def _first_line(path: Path) -> str | None:
    """``ref.txt`` 的第一行（第 1 段参考音对应的逐字文本）。"""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return line.strip()
    return None


def _read_profile(path: Path) -> tuple[str | None, str | None]:
    """``profile.json`` 里的来源登记（R2 留档；读不出来不算错）。"""
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    origin = payload.get("origin")
    note = payload.get("note")
    return (str(origin) if origin is not None else None, str(note) if note is not None else None)


# ══════════════════════════════════════════════════════════════════════
# 路径解析（§04.3.5 的"拒绝任意路径"）
# ══════════════════════════════════════════════════════════════════════


def resolve_out_path(
    request: SynthRequest,
    paths: StudioPaths,
) -> Path:
    """请求 ⇒ 落盘路径，**只允许落在 ``data/`` 下**。

    :raises StudioError: ``CONFIG_PATH_OUT_OF_BOUNDS`` 绝对路径 / ``..`` / 逃出 data/
    """
    data_root = paths.data_dir.resolve()
    if request.out_path is None:
        if request.task_id is None or request.seq is None:
            raise StudioError(
                "要么给 out_path，要么同时给 task_id 与 seq",
                code=ErrorCode.VALIDATION_FAILED,
                context={"out_path": None, "task_id": request.task_id, "seq": request.seq},
                remediation="补上 task_id + seq（服务端据此推出 data/work/<task>/tts/s00N.wav）",
            )
        return paths.tts_work_dir_for(request.task_id) / f"s{request.seq:03d}.wav"

    relative = PurePosixPath(request.out_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise StudioError(
            f"out_path 必须是 data/ 下的相对路径：{request.out_path}",
            code=ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS,
            context={"out_path": request.out_path, "reason": "绝对路径或含 .."},
            remediation="传相对 data/ 的 POSIX 路径，例如 work/<task_id>/tts/s001.wav",
        )
    target = (data_root / Path(*relative.parts)).resolve()
    if not _within(target, data_root):
        raise StudioError(
            f"out_path 逃出了 data/：{request.out_path}",
            code=ErrorCode.CONFIG_PATH_OUT_OF_BOUNDS,
            context={"out_path": request.out_path, "data_dir": str(data_root)},
            remediation="传相对 data/ 的 POSIX 路径（服务端不写 data/ 以外的任何地方）",
        )
    return target


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════
# 服务本体
# ══════════════════════════════════════════════════════════════════════


class TtsService:
    """常驻推理服务的**判定层**（HTTP 层只做翻译，测试直接打这一层）。

    :param backend: 推理后端（测试注入假件 ⇒ 没有 GPU 也能跑这一层）
    :param paths: 路径契约（``out_path`` 的边界就是它）
    :param server: 服务配置（并发 / 排队上限 / 空闲卸载）
    :param monotonic: 时钟注入点（测试用它把"20 分钟"压成毫秒）
    """

    def __init__(
        self,
        backend: CosyVoiceBackend,
        *,
        paths: StudioPaths,
        server: TtsServerConfig,
        registry: VoiceRegistry | None = None,
        warmup_text: str = "",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._paths = paths
        self._server = server
        self._registry = registry or VoiceRegistry(paths.voice_src_dir)
        self._warmup_text = warmup_text
        self._monotonic = monotonic
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=server.concurrency,
            thread_name_prefix="tts-synth",
        )
        self._last_used = monotonic()
        self._last_warmup: int | None = None
        self._stop = threading.Event()
        self._idle_thread: threading.Thread | None = None
        self._stuck: str | None = None

    # ── 只读 ────────────────────────────────────────────────────────────

    @property
    def backend(self) -> CosyVoiceBackend:
        return self._backend

    @property
    def registry(self) -> VoiceRegistry:
        return self._registry

    @property
    def in_flight(self) -> int:
        with self._pending_lock:
            return self._pending

    # ── 生命周期 ────────────────────────────────────────────────────────

    def warmup(self) -> WarmupResponse:
        """加载模型并**念一句短句**（把首句延迟压下去，陷阱 #10）。

        "加载了"与"能念"是两件事：加载成功但前端有问题时，第一句仍然会炸。
        预热必须真念一句，否则它证明不了任何东西。
        """
        result = self._backend.load()
        synth_ms: int | None = None
        text = self._warmup_text
        if text:
            try:
                ref = self._registry.resolve(self._pick_warmup_voice())
                outcome = self._backend.synthesize(
                    text,
                    self._paths.tmp_dir / "tts_warmup.wav",
                    ref_wav=ref.ref_wav,
                    ref_text=ref.ref_text,
                )
                synth_ms = outcome.synth_ms
            except StudioError as exc:
                # 没有可用参考音 ⇒ 只加载不试念。**不假装成功**：说清跳过了什么。
                logger.warning("warmup.synth_skipped", code=str(exc.code), message=exc.message)
                self._stuck = None
        self._last_warmup = result.load_ms
        self._last_used = self._monotonic()
        return WarmupResponse(
            ready=self._backend.state is EngineState.READY,
            load_ms=result.load_ms,
            device=result.device,
            model_state=str(self._backend.state),
            revision=result.revision,
            sample_rate=result.sample_rate,
            synth_ms=synth_ms,
            vram_mb=result.vram_mb,
        )

    def unload(self) -> UnloadResponse:
        freed = self._backend.unload()
        return UnloadResponse(
            unloaded=True,
            freed_mb=freed,
            model_state=str(self._backend.state),
        )

    def health(self) -> HealthResponse:
        state = self._backend.state
        load = self._backend.load_result
        idle = self._monotonic() - self._last_used
        detail: str | None = None
        if self._stuck is not None:
            detail = self._stuck
        elif state is EngineState.ERROR:
            detail = self._backend.last_error or "模型加载失败"
        elif state is EngineState.UNLOADED:
            detail = "模型未加载（首次 /synth 会冷加载，或调 /warmup 预热）"
        return HealthResponse(
            ok=True,
            pid=os.getpid(),
            ready=state is EngineState.READY and self._stuck is None,
            device=self._backend.device,
            model_state=str(state),
            revision=self._backend.revision,
            fp16=self._backend.fp16,
            sample_rate=self._backend.sample_rate,
            concurrency=self._server.concurrency,
            queue_max=self._server.queue_max,
            in_flight=self.in_flight,
            idle_unload_min=self._server.idle_unload_min,
            idle_sec=round(idle, 3),
            vram_mb=load.vram_mb if load is not None else None,
            load_ms=load.load_ms if load is not None else self._last_warmup,
            detail=detail,
        )

    def voices(self) -> VoicesResponse:
        return VoicesResponse(
            revision=self._backend.revision,
            voices=self._registry.list(),
        )

    # ── 合成 ────────────────────────────────────────────────────────────

    def synth(self, request: SynthRequest) -> SynthResponse:
        """按句合成（**带背压**：排队超限 ⇒ 429 ``TTS_BUSY``）。"""
        out_path = resolve_out_path(request, self._paths)
        ref = self._registry.resolve(request.voice_id)
        queue_started = self._monotonic()
        self._enter_queue()
        try:
            future = self._executor.submit(
                self._backend.synthesize,
                request.text,
                out_path,
                ref_wav=ref.ref_wav,
                ref_text=ref.ref_text,
                speed=request.speed,
            )
            wait_sec = self._server.request_timeout_sec
            try:
                result = future.result(timeout=wait_sec)
            except FutureTimeout as exc:
                # 超时的合成**不会**停：CUDA 调用没有取消点，它会继续占着显存与
                # 这个工作线程。所以这里如实标 stuck，让 /health 说真话 ——
                # 恢复路径是 supervisor 重启（T4.11），不是"再等一会儿"。
                self._stuck = (
                    f"上一句合成超时（{wait_sec}s 仍未返回，GPU 可能已卡死）"
                    " —— 看 data/logs/tts.log，必要时重启 tts 进程"
                )
                raise StudioError(
                    f"单句合成超时（{wait_sec}s）",
                    code=ErrorCode.TTS_SENTENCE_FAILED,
                    context={"timeout_sec": wait_sec, "text_len": len(request.text)},
                    remediation="先看 /health 的 model_state 与显存；仍无响应 ⇒ 重启 tts 进程",
                ) from exc
        finally:
            self._leave_queue()
            self._last_used = self._monotonic()

        return SynthResponse(
            out_path=_data_relative(out_path, self._paths),
            duration_ms=result.duration_ms,
            sample_rate=result.sample_rate,
            synth_ms=result.synth_ms,
            rtf=result.rtf,
            segments=result.segments,
            queue_wait_ms=max(0, int((self._monotonic() - queue_started) * 1000) - result.synth_ms),
            engine_revision=self._backend.revision,
            voice_id=ref.voice_id,
            ref_wav=ref.ref_wav.name,
        )

    def _enter_queue(self) -> None:
        """占一个排队位；超限 ⇒ 429。**先占位再执行**：反过来的话，
        超额请求会先排进线程池再被拒 —— 那时 429 已经没有意义了。"""
        with self._pending_lock:
            if self._pending >= self._server.queue_max:
                raise StudioError(
                    f"推理服务排队已满（{self._server.queue_max}）",
                    code=ErrorCode.TTS_BUSY,
                    context={
                        "queue_max": self._server.queue_max,
                        "in_flight": self._pending,
                        "concurrency": self._server.concurrency,
                    },
                    remediation="退避后重试（voice worker 按指数退避自动重投，§03.4）",
                )
            self._pending += 1

    def _leave_queue(self) -> None:
        with self._pending_lock:
            self._pending = max(0, self._pending - 1)

    # ── 空闲卸载 ────────────────────────────────────────────────────────

    def start_idle_watchdog(self) -> None:
        """起一个守护线程：空闲超过 ``idle_unload_min`` ⇒ 卸载显存（R4）。"""
        if self._idle_thread is not None:
            return
        self._idle_thread = threading.Thread(target=self._idle_loop, name="tts-idle", daemon=True)
        self._idle_thread.start()

    def stop_idle_watchdog(self) -> None:
        self._stop.set()

    def _idle_loop(self) -> None:
        while not self._stop.wait(IDLE_TICK_SEC):
            try:
                self.unload_if_idle()
            except Exception:  # 守护线程不许把服务带崩
                logger.exception("tts.idle_watchdog_failed")

    def unload_if_idle(self) -> int:
        """空闲够久就卸载，返回释放的 MB（没卸载 ⇒ 0）。**幂等**，看门狗与排障都能调。"""
        if self._backend.state is not EngineState.READY:
            return 0
        idle_sec = self._monotonic() - self._last_used
        if idle_sec < self._server.idle_unload_min * 60:
            return 0
        logger.info("tts.idle_unload", idle_sec=round(idle_sec, 1))
        return self.unload().freed_mb

    # ── 内部 ────────────────────────────────────────────────────────────

    def _pick_warmup_voice(self) -> str:
        """预热用哪个音色：**第一个能用的**。一个都没有 ⇒ 抛 TTS_VOICE_MISSING，
        由 :meth:`warmup` 降级成"只加载不试念"。"""
        for item in self._registry.list():
            if item.usable:
                return item.id
        raise StudioError(
            "没有可用的参考音，预热跳过试念",
            code=ErrorCode.TTS_VOICE_MISSING,
            context={"voice_src_dir": str(self._paths.voice_src_dir)},
        )

    def shutdown(self) -> None:
        self.stop_idle_watchdog()
        self._executor.shutdown(wait=False, cancel_futures=True)


def _data_relative(path: Path, paths: StudioPaths) -> str:
    """响应里的路径统一成**相对 data/** 的 POSIX 串（与库里的落盘约定一致）。"""
    try:
        return path.resolve().relative_to(paths.data_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ══════════════════════════════════════════════════════════════════════
# HTTP 层
# ══════════════════════════════════════════════════════════════════════


def create_app(
    service: TtsService,
    *,
    lifespan: Callable[[FastAPI], Any] | None = None,
) -> FastAPI:
    """把服务包成 FastAPI 应用（HTTP 层只做翻译，判定全在 :class:`TtsService`）。

    ``lifespan`` 可注入：测试里不需要起看门狗线程与预热。
    """
    app = FastAPI(
        title="Studio TTS 常驻推理服务",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(StudioError)
    async def _studio_error(_request: Request, exc: StudioError) -> JSONResponse:
        return JSONResponse(status_code=STATUS_BY_CODE.get(exc.code, 500), content=exc.to_dict())

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return service.health()

    @app.post("/warmup", response_model=WarmupResponse)
    def warmup() -> WarmupResponse:
        return service.warmup()

    @app.post("/unload", response_model=UnloadResponse)
    def unload() -> UnloadResponse:
        return service.unload()

    @app.get("/voices", response_model=VoicesResponse)
    def voices() -> VoicesResponse:
        return service.voices()

    @app.post("/voices", response_model=None)
    def register_voice() -> JSONResponse:
        """**501**：注册不在这个进程里（裁定 305）。"""
        exc = StudioError(
            "音色注册不在推理服务上做",
            code=ErrorCode.INTERNAL,
            context={"endpoint": "POST /voices", "owner": "T2.4"},
            remediation=(
                "把原声放进 data/voice_src/<音色 id>/ 后跑 scripts/ingest_voice_src.py，"
                "或用素材库面板导入（同一代码路径，含四条硬拒与 R2 留痕）"
            ),
        )
        return JSONResponse(status_code=501, content=exc.to_dict())

    @app.delete("/voices/{voice_id}", response_model=None)
    def delete_voice(voice_id: str) -> JSONResponse:
        """**501**：注销同上（删目录是文件操作，不属于推理进程）。"""
        exc = StudioError(
            "音色注销不在推理服务上做",
            code=ErrorCode.INTERNAL,
            context={"endpoint": "DELETE /voices/{voice_id}", "voice_id": voice_id},
            remediation="删除 data/voice_src/<音色 id>/ 目录并在素材库面板重新扫描",
        )
        return JSONResponse(status_code=501, content=exc.to_dict())

    @app.post("/synth", response_model=SynthResponse)
    def synth(request: SynthRequest) -> SynthResponse:
        return service.synth(request)

    return app


# ══════════════════════════════════════════════════════════════════════
# 进程入口
# ══════════════════════════════════════════════════════════════════════


def build_service(
    paths: StudioPaths,
    config: TtsConfig,
    *,
    backend: CosyVoiceBackend | None = None,
) -> TtsService:
    """按配置组装服务（``backend`` 可注入 ⇒ 测试不需要 GPU）。"""
    resolved = backend or CosyVoiceBackend(
        config.model.dir,
        revision=config.model.revision,
        source_dir=config.model.source_dir,
        matcha_dir=config.model.matcha_dir,
        fp16=config.model.fp16,
        sample_rate=config.model.sample_rate,
    )
    return TtsService(
        resolved,
        paths=paths,
        server=config.server,
        warmup_text=config.model.warmup_text,
    )


def serve() -> None:
    """``workers/run_tts.py`` 的入口：读配置 → 建服务 → 起 uvicorn。"""
    import uvicorn  # noqa: PLC0415  （只有真起服务时才需要）

    paths = StudioPaths.from_env()
    config = load_tts_config(paths)
    service = build_service(paths, config)
    app = create_app(service, lifespan=_service_lifespan(service, config))

    logger.info(
        "tts.serving",
        host=config.server.host,
        port=config.server.port,
        revision=config.model.revision,
        fp16=config.model.fp16,
        concurrency=config.server.concurrency,
    )
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


def _service_lifespan(
    service: TtsService,
    config: TtsConfig,
) -> Callable[[FastAPI], Any]:
    """起停：空闲看门狗 + 启动预热（与 ``app/lifespan.py`` 同一形状）。

    预热走**后台线程**：起服务本身要秒回 —— supervisor 的重启就绪预算是 6 秒，
    等不了 10 秒的模型加载。加载结果由 ``/health`` 如实报出来（不假装已就绪）。
    """

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        service.start_idle_watchdog()
        if config.server.warmup_on_start:
            threading.Thread(target=_safe_warmup, args=(service,), name="tts-warmup", daemon=True).start()
        try:
            yield
        finally:
            service.shutdown()

    return _lifespan


def _safe_warmup(service: TtsService) -> None:
    try:
        service.warmup()
    except Exception:  # 预热失败不能把服务带崩：/health 会说真话
        logger.exception("tts.warmup_failed")
