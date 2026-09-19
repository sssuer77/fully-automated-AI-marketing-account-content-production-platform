"""引擎缝的正式契约（T2.3 · §04.3.2）—— 一句文本进，一段 WAV 出。

它比 T2.6 那个最小缝多了什么，以及为什么多的正是这三件事
-------------------------------------------------------
:class:`SentenceEngine` 只有 ``name`` / ``revision`` / ``synthesize``：够 T2.6 跑
"按句合成 + 缓存 + 续传"，但不够 T2.3 的**降级决策表**用 —— 表里两条动作
（``REWARM_ENGINE`` / ``SWITCH_ENGINE``）的前提是"能问引擎现在怎么样、能叫它
卸了重载"，而这三个能力在 T2.3 之前只存在于**服务端的 HTTP 上**（`POST /warmup`、
`POST /unload`、`GET /health`），引擎缝上一点都摸不到。于是决策表写出来也只能是
一句"重试"，那正是本轮之前的样子。

所以 :class:`VoiceEngine` = :class:`SentenceEngine` + ``warmup`` / ``unload`` /
``health``。多出来的三个都是**引擎生命周期**原语，而且**必须有 no-op 实现**：
系统语音包没有"显存"可卸，它的 ``unload`` 就是什么都不做（见
:meth:`~studio.tts.sentence.SapiEngine.unload`）。这不叫"空实现凑数"——它叫
"降级档也能被同一套决策表驱动"：决策表不该知道对面是哪台引擎。

为什么是**同步**的，而规格写的是 ``async def``
-------------------------------------------
规格 §04.3.2 的 ABC 是 ``async``。这里**故意**定成同步，理由是调用点全都在同步
世界里：配音池是 `threading` 线程（``pools/worker_base.py``），服务端是 FastAPI 的
同步路由（跑在线程池里），CLI 是普通函数。把它们改成 async 要连带改掉池的认领循环
（那是一期的骨架，T1.6 已经收口）。

硬要保留 ``async def`` 的话，唯一能落地的方式是每个调用点 ``asyncio.run(...)``
—— 每念一句起一个事件循环，而且**异常会多包一层 traceback**（决策表靠
``error_code`` 分流，包一层不影响码，但排障时那层栈会让人以为是别的地方出的错）。
换来的只有一个"与规格字面一致"，代价是每次调用多一次 loop 创建。所以这里选择
如实偏离，并在 ``todolist.md`` 的裁定里写下来。

同理**没有** ``register_voice`` / ``list_voices``：音色的注册与注销**不在推理服务
上**（裁定 305：``POST|DELETE /voices`` ⇒ 501）。入库判定只在
``AssetService.ingest`` 有一份，服务上再实现一遍就是两份判定迟早对不上。
"这台引擎念得出哪些音色"由 :meth:`VoiceEngine.health` 的 ``voices`` 回答 ——
它是**只读**的，正好是决策表要的那一半。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from studio.core.logging import get_logger

__all__ = [
    "EngineHealth",
    "SentenceEngine",
    "VoiceEngine",
    "rewarm",
]

logger = get_logger("studio.tts.engine")


@runtime_checkable
class SentenceEngine(Protocol):
    """一句文本 ⇒ 一段 WAV 的最小缝（T2.6）。

    ``name`` 与 ``revision`` **进缓存键**（``tts_cache_key``）：引擎换了、权重版本
    换了，旧产物就不能再被复用 —— 否则成片里那一句是**别人的嗓子**，而每一处日志
    都写着成功（陷阱 #165）。所以这两个值必须**从引擎自己报的来**，不能由调用方
    写死一个字面量。
    """

    name: str
    revision: str

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None: ...


@dataclass(frozen=True, slots=True)
class EngineHealth:
    """引擎此刻的自述（**问出来的，不是猜出来的**）。

    ``ready`` 与 ``wakeable`` 是**两件事**，不能合成一个（陷阱 #168）：
    ``ready=False`` 可能是"坏了"（子环境里没有 torch），也可能是"睡着了"
    （空闲卸载，下一次调用会自己把模型读回来，真机 ~20s）。前者要接管重启，
    后者**算可用** —— 混成一种会同时犯两个错（杀一个健康实例 / 悄悄退回系统
    语音包）。
    """

    engine: str
    ready: bool
    #: 人话说明（"模型未加载，首次 /synth 会冷加载"）—— 面板直接显示它
    detail: str | None = None
    #: 这台引擎**念得出来**的音色（决策表与面板的判据，见模块 docstring）
    voices: tuple[str, ...] = ()
    #: 服务自报的模型状态（``unloaded`` / ``ready`` / ``error``…）；没有 ⇒ ``None``
    model_state: str | None = None
    #: "没就绪但叫得醒"
    wakeable: bool = False

    @property
    def usable(self) -> bool:
        """能不能拿它念（**就绪，或者叫得醒**）。"""
        return self.ready or self.wakeable

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "ready": self.ready,
            "detail": self.detail,
            "voices": list(self.voices),
            "model_state": self.model_state,
            "wakeable": self.wakeable,
            "usable": self.usable,
        }


@runtime_checkable
class VoiceEngine(SentenceEngine, Protocol):
    """可替换的配音引擎（§04.3.2）：常驻服务 / 系统语音包 / 测试用的 Mock。

    三个生命周期原语都要**幂等且可重入**：决策表在失败路径上调它们，而失败路径
    本身会被重试（同一句可能被两个 worker 先后认领）。
    """

    def warmup(self) -> None:
        """把模型读进显存 / 预热一次（避免首句抖动）。**已经在跑的时候不该重载**。"""
        ...

    def unload(self) -> None:
        """释放显存（R4 空闲策略）。没有显存可放的引擎（系统语音包）⇒ 什么都不做。"""
        ...

    def health(self) -> EngineHealth:
        """此刻的自述。**不许抛** —— 问不到就回一个 ``ready=False`` 的结论。"""
        ...


def rewarm(engine: VoiceEngine) -> None:
    """卸了再载 —— §04.3.3 决策表里 ``REWARM_ENGINE`` 那个动作的落点。

    两步**分开容忍失败**，而且顺序不能反
    ----------------------------------
    卸载失败（服务正忙、已经没在跑、没有显存可放）**不该阻止重载**：那种时候
    重载正是要做的事。反过来，"重载失败"才是真的失败 —— 它说明这台引擎叫不醒，
    调用方据此走下一步（切备用引擎 / 到线降级）。

    为什么先卸再载，而不是直接载
    ----------------------------
    显存不足（``TTS_OOM``）是这条动作最常见的起因，而"直接载"在显存已经被自己
    占着的时候会**再炸一次**（真机：2442 MB 已占用，重载又申请一份）。先释放
    再加载是唯一能真正把这块显存腾出来的顺序。代价是"引擎其实没坏"时白等一次
    重载（真机 20–32s）—— 所以它**每句最多用一次**（见
    :func:`~studio.tts.fallback.decide` 的 ``attempt <= 1`` 判据）。

    :raises StudioError: 重载失败（卸载失败只记日志）
    """
    try:
        engine.unload()
    except Exception as exc:
        logger.warning(
            "tts.rewarm_unload_failed",
            engine=engine.name,
            error=f"{type(exc).__name__}: {exc}",
        )
    engine.warmup()
