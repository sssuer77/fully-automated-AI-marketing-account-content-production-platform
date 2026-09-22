"""引擎选择：这一台机器现在用哪台 TTS，以及它念得出来哪些音色。

为什么它**不**待在配音池里（2026-09-21）
----------------------------------------
这段判据原先长在 ``pools/voice_worker.py`` 里（``_EnginePicker``），只有配音池用它。
于是**渲染面板**那条路（``tts/synth.py::synthesize_script``）一直直连 SAPI：

- 它的音色下拉框来自**常驻引擎**（``GET /api/v1/voices`` ⇒ ``bigbear`` / ``littlebear``），
- 而它合成时把 ``bigbear`` 原样交给 **SAPI** ⇒ ``SelectVoice`` 抛
  ``Cannot set voice. No matching voice is installed``（真机 2026-09-21 的
  ``r0001`` 就是这么挂在 55/58 的）。

面板说"引擎 cosyvoice2 · 2 个音色"、点下去却报"音色名拼写不对" —— 这是
陷阱 #154 的第三次现身，而这次的原因既不是时序、也不是 payload：是**两处各挑各的引擎**。
判据只能有一份，所以它下沉到这里，配音池与渲染路径共用。

四条纪律（前三条与它原先在池里时一模一样）
-----------------------------------------
1. **每一次取**，不是装配期定一次：启动器同时拉起五个进程，而 tts 要先把模型读进
   显存（真机 22s）—— 装配期必然落在"服务还没就绪"里。
2. **只升不降**：常驻服务一旦可用就固定用它。中途降回 SAPI 会让同一支片子里
   一半 CosyVoice、一半系统音色，那比"如实失败"更难查。
3. **它自报能念什么**：``speakable`` 是这台引擎**念得出来**的音色。调用方拿它
   判"用户要的那个音色这一档能不能念"（见 :func:`pick_speakable_voice`）。
4. **档位冻住，音色清单不冻**（2026-09-22）：第 2 条冻的是**档位**，不是**清单** ——
   清单随素材库变（入库一个音色、删掉一个音色），而池子手里的那份是几分钟前的。
   详见 :meth:`EnginePicker._refresh_voices` 里那台真机事故。
"""

from __future__ import annotations

import time
from typing import Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.faults import FaultPlan
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.tts.engine import SentenceEngine
from studio.tts.faults import wrap_engine
from studio.tts.sapi import list_voices_cached, pick_voice
from studio.tts.sentence import SapiEngine
from studio.tts.service_engine import ResidentEngine, active_resident

__all__ = [
    "VOICE_LIST_TTL_SEC",
    "EnginePicker",
    "pick_speakable_voice",
    "resolve_sapi_voice",
]

logger = get_logger("studio.tts.engine_picker")

#: 音色清单的有效期（秒）。为什么是 5 秒见 :meth:`EnginePicker._refresh_voices`：
#: 一句合成要 5–15 秒，所以实际上"每句至多问一次"；探测只是两个本机 GET，
#: 而且**不会叫醒睡着的模型**。
VOICE_LIST_TTL_SEC: Final[float] = 5.0


class EnginePicker:
    """每一次取引擎：常驻服务能用就用它，否则退回 SAPI（§1.7 降级）。

    为什么是"每一次"而不是装配期定一次（真机实测 2026-09-18）
    --------------------------------------------------------
    启动器**同时**拉起五个进程，而 tts 要先把模型读进显存（真机 22s）。配音池的
    装配期必然落在那 22s 里 ⇒ 判据永远是"服务不可用" ⇒ 整条链路退回系统语音包，
    而且**一直退到进程重启为止**：面板上写着 ``bigbear``（它问的时候服务已经就绪），
    池子却把 ``bigbear`` 交给 SAPI —— ``SelectVoice`` 抛 ⇒ 每句失败 3 次 ⇒ 成片
    没人声（陷阱 #154 的第二次现身，这次的原因在**时序**上）。

    判据用的还是 :func:`~studio.tts.service_engine.active_resident` **那一份**（与
    配音面板"这个音色念得出来吗"同源），只是问得晚一点、多问几次。

    只升不降
    --------
    常驻服务一旦可用就固定用它。反过来的"中途降回 SAPI"会让同一支片子里一半
    CosyVoice、一半系统音色 —— 那比"如实失败"更难查（听起来只是"有几句话怪"）。

    返回的 ``speakable`` 是这台引擎**念得出来**的音色（SAPI 是系统语音包；常驻是
    服务自报的可克隆音色）。要它是因为 payload 里那个音色可能是**上一次判据**下
    算出来的 —— 同一条时序坑的另一半。

    ``plan`` 传进来是因为换引擎的动作发生在**这里**：故障壳得套在每一台
    真正上场的引擎上，而不是套在装配期那一台上（T2.8 降级演练）。
    """

    def __init__(self, paths: StudioPaths, *, plan: FaultPlan | None = None) -> None:
        self._paths = paths
        self._plan = plan if plan is not None else FaultPlan()
        self._sapi = wrap_engine(SapiEngine(), self._plan)
        self._sapi_voice: str | None = None
        self._resident: tuple[SentenceEngine, str | None, tuple[str, ...]] | None = None
        #: 已经**强制**降档到系统语音包（`SWITCH_ENGINE` 打过这张牌）
        self._switched = False
        #: 上一次问"这台引擎认哪些音色"的时刻（``monotonic``；0 = 还没问过）
        self._voices_at: float = 0.0

    @property
    def switched(self) -> bool:
        """已经强制降档了吗（决策表用它判"换引擎这张牌打过了没有"）。"""
        return self._switched

    def switch(self) -> bool:
        """强制降档到系统语音包（§04.3.3 的 ``SWITCH_ENGINE``）。

        ⇒ **这一次真的换了没有**：已经在备用引擎上 ⇒ ``False``（调用方据此说
        "没得换了"，而不是谎报换成功）。

        与"只升不降"（裁定 314）不矛盾：那一条说的是**判据**不许自己往下降
        （服务明明可用却退回 SAPI ⇒ 同一支片子两种嗓子）。这里是**决策表**在
        连续失败之后主动换档 —— 它知道自己在做什么，而且换完就不再回头看常驻服务
        （``self._switched`` 一置位，``__call__`` 里那句探测也不会再问）。

        **不切回常驻服务**：这台引擎刚刚被判定"念不出来"，切回去只会再失败一轮；
        要恢复就重启配音池（那是"修完引擎"之后的事）。
        """
        if self._switched:
            return False
        self._switched = True
        self._resident = None
        logger.warning("voice.engine_switched", reason="决策表判定常驻引擎连续失败 ⇒ 改走系统语音包")
        return True

    def __call__(self) -> tuple[SentenceEngine, str | None, tuple[str, ...]]:
        """⇒ ``(引擎, 兜底音色, 念得出来的音色)``。"""
        if self._resident is not None:
            self._refresh_voices()
            return self._resident
        if self._switched:
            # 已经强制降档 ⇒ **不再问常驻服务**：它刚刚被判"念不出来"，
            # 再问一次只会把整批句子又带回那条路上（见 `switch`）。
            if self._sapi_voice is None:
                self._sapi_voice = resolve_sapi_voice()
            return self._sapi, self._sapi_voice, tuple(list_voices_cached())
        status = active_resident(self._paths)
        if status is None:
            if self._sapi_voice is None:
                # 列音色要起一次 PowerShell（1–2 秒）⇒ 进程生命周期内只解析一次
                self._sapi_voice = resolve_sapi_voice()
            logger.info("voice.engine_sapi", reason="常驻推理服务不可用，按降级档用系统语音包")
            return self._sapi, self._sapi_voice, tuple(list_voices_cached())
        logger.info(
            "voice.engine_resident",
            base_url=status.base_url,
            revision=status.revision,
            voices=list(status.voices),
        )
        self._resident = (
            wrap_engine(ResidentEngine(paths=self._paths, status=status), self._plan),
            status.voices[0],
            status.voices,
        )
        self._voices_at = time.monotonic()
        return self._resident

    def _refresh_voices(self) -> None:
        """把"这台引擎现在认哪些音色"重新问一遍（**档位与引擎对象不动**）。

        为什么清单不能跟着档位一起冻住
        ------------------------------
        第 2 条纪律冻的是**档位**（常驻 / 系统语音包），那是对的；``speakable``
        不是档位的一部分 —— 它随**素材库**变：入库一个音色、或删掉一个音色，
        服务下一秒就报新的那一份，而池子手里的还是几分钟前的。

        真机事故（2026-09-22 · 陷阱 203）
        ---------------------------------
        用户导入 `sunxiaochuan`（入库、`/voices` 里 `usable: true`）之后点「开始配音」，
        55 句里**每一句**都写着「音色 sunxiaochuan 当前引擎念不出来 ⇒ 改用 bigbear」——
        池子拿的是**导入之前**那一份清单（`("bigbear", "littlebear")`），而它兜底的那个
        音色刚被删掉 ⇒ 每句都失败 ⇒ 熔断 ⇒ 18 句被静音占位。面板上"可用音色"里明明
        有它（那是**现问**的），只有池子不认。判据没分叉，**时效**分叉了。

        ``TTL`` 的取值
        --------------
        5 秒是"刚入库的音色马上能用"与"别把日志刷满"之间的折中：一句合成要 5–15 秒，
        所以实际上每句至多问一次。探测是 `active_resident` 那两个本机 GET
        （``/health`` + ``/voices``），**不会叫醒睡着的模型** —— 它认 ``unloaded``
        但不发 ``/warmup``（陷阱 168），所以这一段没有显存代价。

        问不成时不降档
        --------------
        ``active_resident`` 回 ``None`` ⇒ **手里的那一份照用**：中途降回 SAPI 会让
        同一支片子里两种嗓子（第 2 条纪律）。真要降档是决策表的事（:meth:`switch`）。
        时间戳**先记**再问 —— 否则服务连不上时，每一句都要去敲一次。
        """
        now = time.monotonic()
        if now - self._voices_at < VOICE_LIST_TTL_SEC:
            return
        self._voices_at = now
        status = active_resident(self._paths)
        if status is None:
            return
        engine, _fallback, voices = self._resident  # type: ignore[misc]
        if voices == status.voices:
            return
        logger.info(
            "voice.voices_refreshed",
            before=list(voices),
            after=list(status.voices),
            reason="素材库变了（入库 / 删除音色），引擎自报的清单跟着变",
        )
        # 只换清单（含兜底音色）。**引擎对象不换**：它的 `revision` 进了缓存键，
        # 中途换掉会让同一支片子里前后两段的缓存键分属两个版本。
        self._resident = (engine, status.voices[0], status.voices)


def resolve_sapi_voice() -> str | None:
    """挑一个本机装了的音色；一个都没有 ⇒ 抛（**启动期**就该知道）。"""
    chosen = pick_voice()
    if chosen is None:
        raise StudioError(
            "本机没有可用的语音音色，配音池无法工作",
            code=ErrorCode.TTS_ENGINE_UNAVAILABLE,
            context={"engine": "sapi"},
            remediation=(
                "在「设置 → 时间和语言 → 语音」里装一个中文语音包（如 Microsoft Huihui），"
                "或给 pools.yaml 的 voice 池配一个已装音色"
            ),
        )
    return chosen


def pick_speakable_voice(
    requested: str | None,
    *,
    speakable: tuple[str, ...],
    default: str | None,
    engine: str,
) -> tuple[str | None, str | None]:
    """用户要的音色 ⇒ ``(这一档引擎真念得出来的那个, 一句人话)``。

    ``speakable`` 为空 ⇒ **原样返回** ``requested``：那是"这台引擎不肯自报家门"
    （SAPI 列表起不来 / 常驻服务没给 ``/voices``），这时拦下来反而会让本来能念的
    音色念不出来。宁可让它去试一次、由引擎自己报错。

    要不到的后果说清楚：``bigbear`` 交给 SAPI ⇒ ``SelectVoice`` 抛 ⇒ 每一句失败
    3 次 ⇒ 成片没人声。所以这里**不**把参考音硬塞给系统语音包，而是退回这台引擎的
    兜底音色并把原因交给调用方（裁定 314：真选了也不会没声）。
    """
    if requested is None:
        return default, None
    if not speakable or requested in speakable:
        return requested, None
    if default is None or default == requested:
        return requested, None
    known = "、".join(speakable[:4])
    note = f"音色 {requested} 在 {engine} 上念不出来（它认的是 {known}）⇒ 这一次改用 {default}"
    logger.warning("tts.voice_fallback", requested=requested, engine=engine, fallback=default)
    return default, note
