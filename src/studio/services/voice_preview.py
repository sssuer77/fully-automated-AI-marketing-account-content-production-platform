"""音色试听样本（T2.4 · §04.3.1）—— 「这个嗓子像不像」不用先出一整支片子。

为什么值得单独做
----------------
换音色是这条链路上**最贵的一次试错**：它要重配 N 句（几十秒到几分钟），再走一遍
收口与渲染，最后才能从成片里听见那个嗓子 —— 而用户真正的动作只有一句话：
"我想先听一下像不像"。这个模块就是那一步：一段固定文本 + 当前音色 ⇒ 一个 wav。

四条纪律
--------
1. **不占池、不占任务**。``jobs.task_id`` 有外键（陷阱 #101），而试听与任何一条任务
   都无关 —— 硬塞一条作业就得先造一条假任务。所以它在**进程内**跑（与
   ``render_job_service`` 同一条：重启即丢，产物不受影响）。
2. **不进配音缓存**。缓存键是 ``(引擎, 版本, 音色, 文本, 语速)``，而样本用的是一段
   **固定文本** —— 它会与用户稿子里恰好相同的那一句撞键。撞上的后果不是报错，是
   "换了参考音、试听却还是旧嗓子"（缓存命中）。所以样本落在自己的目录里。
3. **``get()`` 只读盘、``ensure()`` 才起线程**。面板每次刷新都会调 ``get()``：
   让它顺带合成，就是"每刷新一次念一句"。
4. **``.partial`` → 原子改名**（陷阱 #9）。半截 wav 被当成样本的后果是"试听放出来
   是一声爆音"，而文件明明在。

为什么是后台线程而不是直接同步返回
----------------------------------
真机实测：CosyVoice 空闲卸载后再念一句要 **20s 冷加载 + 十几秒推理**。同步做完，
一次点击就是一个挂住二十几秒的 HTTP 请求（面板看起来像死了，浏览器还可能先超时）。
所以这里只做"起一个线程 + 立刻回状态"，面板按状态轮询。

为什么引擎判据不自己写一份
--------------------------
"用哪台引擎"的唯一裁判是 :func:`~studio.tts.service_engine.active_resident` ——
配音池（``_EnginePicker``）与配音面板（``speakable_voices``）用的都是它。这里再判一次
"服务在不在"就是第三份判据，而三份判据的漂移症状是"面板说这个音色能念、试听却报错"
（陷阱 #154 的形状）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.media import probe_media
from studio.core.paths import StudioPaths, preview_slug
from studio.tts.sapi import pick_voice
from studio.tts.sentence import SapiEngine, SentenceEngine
from studio.tts.service_engine import ResidentEngine, active_resident

__all__ = [
    "PREVIEW_TEXT",
    "PREVIEW_URL_PREFIX",
    "STATUS_FAILED",
    "STATUS_MISSING",
    "STATUS_READY",
    "STATUS_RUNNING",
    "PreviewSample",
    "VoicePreviewService",
    "preview_url",
]

logger = get_logger("studio.services.voice_preview")

#: 样本念的那句话。挑的是**短、有中文声调、有标点**的一句：
#: 太长 ⇒ 每一句都要多等几秒（而用户只是想听个音色）；太短 ⇒ 听不出音色。
PREVIEW_TEXT: Final[str] = "大家好，这里是当前音色的试听样本，用来听一下像不像。"

#: 试听样本的 url 前缀（面板把它交给 ``<audio src>``）。
PREVIEW_URL_PREFIX: Final[str] = "/api/v1/media/voice_preview"

#: 四态。``missing`` 与 ``failed`` 必须分开：前者是"还没生成"（点一下就行），
#: 后者是"生成失败了"（再点一下大概率还是失败，得先看那句话）。
STATUS_MISSING: Final[str] = "missing"
STATUS_RUNNING: Final[str] = "running"
STATUS_READY: Final[str] = "ready"
STATUS_FAILED: Final[str] = "failed"

#: 念样本用的语速档（与 SAPI 的 ``rate`` 同一套刻度，0 = 正常）。
PREVIEW_RATE: Final[int] = 0

#: 旁车里记的字段（读的时候逐条校验，缺一个就当"没有旁车"—— 见 :meth:`VoicePreviewService.get`）。
_META_FIELDS: Final[tuple[str, ...]] = ("voice_id", "engine", "generated_at", "duration_ms")


def preview_url(voice_id: str) -> str:
    """样本的 url。**由 :func:`~studio.core.paths.preview_slug` 推出来**，不另立一套命名。"""
    return f"{PREVIEW_URL_PREFIX}/{preview_slug(voice_id)}.wav"


@dataclass(frozen=True, slots=True)
class PreviewSample:
    """一个音色的试听样本**现在**的状态（面板那一行直接画它）。"""

    voice_id: str
    status: str
    path: Path | None = None
    url: str | None = None
    duration_ms: int | None = None
    engine: str | None = None
    generated_at: str | None = None
    #: 失败原因（``status == failed`` 时才有）。**原样给人看** —— 它多半就是引擎那句报错。
    error: str | None = None
    note: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "status": self.status,
            "url": self.url,
            "duration_ms": self.duration_ms,
            "engine": self.engine,
            "generated_at": self.generated_at,
            "error": self.error,
            "note": self.note,
        }


#: 引擎装配的签名：⇒ ``(引擎, 兜底音色)``。测试注入假件就换它（不必去 patch 引擎探测）。
EngineFactory = Callable[[], "tuple[SentenceEngine, str | None]"]


def _engine_factory_for(paths: StudioPaths) -> EngineFactory:
    """缺省装配：按**配音池那一份判据**挑引擎（常驻服务能用就用它，否则退回系统语音包）。

    每次生成都现问一遍，而不是装配期定一次 —— 与 ``_EnginePicker`` 同一个理由
    （真机实测 2026-09-18）：启动器同时拉起五个进程，而 tts 要先把模型读进显存
    （真机 22s）。装配期问一次，答案永远是"服务不可用"，于是试听会一直用系统语音包
    念那些**只有 CosyVoice 念得出来**的参考音 —— 而面板上写着"当前引擎念得出来"。

    返回的兜底音色在试听这条路上基本用不到（调用方永远显式给音色）—— 留着是为了
    与配音池的装配形状一致，免得两处各长一个样。
    """

    def build() -> tuple[SentenceEngine, str | None]:
        status = active_resident(paths)
        if status is not None and status.usable:
            return ResidentEngine(paths=paths, status=status), status.voices[0] if status.voices else None
        return SapiEngine(), pick_voice()

    return build


def _no_sample(partial: Path) -> None:
    """引擎说成功、盘上却没有 —— 抽成一个函数是为了让 ``_generate`` 的 ``try`` 里
    只有"会抛的那几行"（TRY301）：异常消息里的路径要在**抛的那一刻**才取值。"""
    raise StudioError(
        f"引擎没有写出试听样本：{partial.name}",
        code=ErrorCode.TTS_SENTENCE_FAILED,
        context={"path": partial.as_posix()},
        remediation="看 data/logs/tts.log：服务多半把文件写到了别处（out_path 越界会被它拒）",
    )


class VoicePreviewService:
    """试听样本的生成与查询（**进程内单例**，挂在 ``AppState`` 上）。

    :param engine_factory: 注入点。缺省按常驻服务 / SAPI 现挑 —— 测试里换成假引擎，
        整条"生成 → 落盘 → 读回"的路径就都不需要 GPU。
    """

    def __init__(self, paths: StudioPaths, *, engine_factory: EngineFactory | None = None) -> None:
        self._paths = paths
        self._engine_factory = engine_factory if engine_factory is not None else _engine_factory_for(paths)
        self._lock = threading.Lock()
        #: 正在生成的那些音色（面板据此画"生成中"）
        self._running: set[str] = set()
        #: 上一次失败的原因。**不落盘**：它是"这一次进程里那次尝试"的结论，重启即失效
        #: —— 而盘上那个坏文件如果真写出来了，它压根到不了 ``ready``（见 ``_generate``）。
        self._failures: dict[str, str] = {}

    # ── 对外 ────────────────────────────────────────────────────────────

    def get(self, voice_id: str) -> PreviewSample:
        """**只读**：盘上有就 ``ready``，在生成就 ``running``，否则 ``missing``。"""
        with self._lock:
            running = voice_id in self._running
            failure = self._failures.get(voice_id)

        path = self._paths.voice_preview_wav(voice_id)
        if path.is_file():
            meta = self._read_meta(voice_id)
            return PreviewSample(
                voice_id=voice_id,
                status=STATUS_READY,
                path=path,
                url=preview_url(voice_id),
                duration_ms=meta.get("duration_ms"),
                engine=meta.get("engine"),
                generated_at=meta.get("generated_at"),
            )
        if running:
            return PreviewSample(voice_id=voice_id, status=STATUS_RUNNING, note="正在生成试听样本")
        if failure is not None:
            return PreviewSample(voice_id=voice_id, status=STATUS_FAILED, error=failure)
        return PreviewSample(voice_id=voice_id, status=STATUS_MISSING)

    def ensure(self, voice_id: str) -> PreviewSample:
        """盘上没有就**起一个后台线程**生成，然后立刻返回当前状态（**不阻塞**）。

        重复调用是安全的：已经在生成的那一个不会再起第二条线程（同一个音色起两条，
        两边的 ``.partial`` 会互相覆盖 —— 而 ffmpeg / 引擎正在写它）。
        """
        current = self.get(voice_id)
        if current.status in (STATUS_READY, STATUS_RUNNING):
            return current
        with self._lock:
            if voice_id in self._running:
                return self.get(voice_id)
            self._running.add(voice_id)
            self._failures.pop(voice_id, None)
        thread = threading.Thread(
            target=self._generate,
            args=(voice_id,),
            name=f"voice-preview-{preview_slug(voice_id)}",
            daemon=True,
        )
        thread.start()
        logger.info("voice.preview_started", voice_id=voice_id)
        return self.get(voice_id)

    def forget(self, voice_id: str) -> None:
        """把失败结论清掉（面板上那颗「重新生成」按下去时调）。

        不删盘上的样本：删了之后如果生成失败，用户会连**旧的**那一份也一起失去 ——
        而旧样本通常还是有用的（"至少能听出上个版本是什么样"）。
        """
        with self._lock:
            self._failures.pop(voice_id, None)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _generate(self, voice_id: str) -> None:
        """线程体：合成 → 量时长 → 原子改名 → 写旁车。**不抛**（结论记进 ``_failures``）。"""
        target = self._paths.voice_preview_wav(voice_id)
        partial = target.with_name(f"{target.stem}.partial{target.suffix}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            partial.unlink(missing_ok=True)
            engine, _default_voice = self._engine_factory()
            engine.synthesize(PREVIEW_TEXT, partial, voice=voice_id, rate=PREVIEW_RATE)
            info = probe_media(partial)
            if not partial.is_file() or partial.stat().st_size == 0:
                # 引擎说成功、盘上却没有 —— 与配音池那条"服务回了成功但产物不在盘上"同一条。
                _no_sample(partial)
            partial.replace(target)
            self._write_meta(
                voice_id,
                {
                    "voice_id": voice_id,
                    "engine": getattr(engine, "name", None),
                    "generated_at": now_iso(),
                    "duration_ms": info.duration_ms if info.has_audio else None,
                },
            )
            with self._lock:
                self._failures.pop(voice_id, None)
            logger.info(
                "voice.preview_ready",
                voice_id=voice_id,
                path=target.as_posix(),
                duration_ms=info.duration_ms,
            )
        except Exception as exc:
            # **什么都接**：这一段跑在一个没人 ``join`` 的守护线程里，抛出去就是一条
            # "Exception in thread ..." 的 stderr（进不了 ``system_logs``，面板上也看不见），
            # 而用户看到的是"点了生成，一直转圈"。所以结论一律转成那句话记下来。
            partial.unlink(missing_ok=True)
            message = exc.message if isinstance(exc, StudioError) else f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._failures[voice_id] = message
            logger.warning("voice.preview_failed", voice_id=voice_id, error=message)
        finally:
            with self._lock:
                self._running.discard(voice_id)

    def _read_meta(self, voice_id: str) -> dict[str, Any]:
        """读旁车；缺字段 / 坏 JSON / 不是对象 ⇒ 空字典（**不是错误**）。

        为什么"坏旁车"要当没有：它只影响"时长与引擎名"这两个**装饰性**字段。
        为一个装饰性字段把"盘上明明有样本"降级成"读不出来"，是把可用性换成了洁癖。
        """
        path = self._paths.voice_preview_meta(voice_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict) or any(key not in payload for key in _META_FIELDS):
            return {}
        return payload

    def _write_meta(self, voice_id: str, payload: dict[str, Any]) -> None:
        """写旁车。写不进去只记一句日志 —— 样本本身已经落盘了，那才是主产物。"""
        path = self._paths.voice_preview_meta(voice_id)
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("voice.preview_meta_failed", voice_id=voice_id, error=str(exc))
