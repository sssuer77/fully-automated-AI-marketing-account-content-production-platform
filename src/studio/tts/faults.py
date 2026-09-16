"""把 ``STUDIO_FAULT`` 的计划套在引擎上（T2.8 的降级演练）。

为什么注入点选在引擎缝上
------------------------
"引擎挂了"与"这一句念不出来"是**两件事**：前者是环境故障（要重载模型 / 重启服务），
后者是这一句自己的问题（要换切分 / 降级）。§04.3.3 的决策表按 ``error_code`` 分流，
所以演练要注入的必须是**引擎层**的错误码。注入点放在引擎缝上，池那侧一行都不用改：
它照样走"失败 ⇒ 记账 ⇒ 退避重试 ⇒ 到线降级"那条**真实**路径，演练验的就是那条路。

为什么包装之后引擎名要带一个**每场演练都不同**的标记
--------------------------------------------------
``synthesize_sentence`` 先查缓存、再调引擎，而缓存键吃的正是引擎名。名字不变的话，
**已经念过的句子会命中缓存、根本不进引擎** —— 演练于是变成"什么都没发生"，而门禁照样绿。

只加一个**固定的** ``+fault`` 后缀不够，这是真机演练里踩出来的：第一场演练
（``tts_fail_sentence=3``）会把念成功的音频按 ``sapi+fault`` 收进缓存，第二场演练
（``tts_down=1``）算出来的键**一模一样** ⇒ 四句全命中缓存 ⇒ 引擎一次都没被调到 ——
日志里 ``voice.fault_injected tts_down=True`` 照打，而四句全都"念"出来了。
所以标记必须**每场都不同**（进程号 + 随机段）：演练的语义就是"这一遍必须真的去念"，
缓存不能替它念。

代价是每场演练都会往缓存里多写一份**真音频**（键不再复用，只能等 LRU 淘汰）。
一场演练三五句，比"演练全绿但什么都没发生"便宜得多。

句序从哪来
----------
引擎缝上拿不到 ``seq``（``synthesize`` 只收文本与产物路径），而"第 3 句失败"是演练里
唯一好写的写法。于是从**产物文件名**反推：``s003.wav`` ⇒ 3。这不是巧合而是契约
（§04.3.3 不变量 1：文件名由 ``seq`` 决定）。认不出文件名 ⇒ **不注入** —— 宁可这一句
不失败，也不要失败到别的句子头上。
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Final

from studio.core.errors import ErrorCode, StudioError
from studio.core.faults import FaultPlan
from studio.tts.sentence import SentenceEngine

__all__ = [
    "FAULT_SUFFIX",
    "FaultEngine",
    "sentence_seq",
    "wrap_engine",
]

#: 包装后引擎名的后缀。**它只是名字的前半截** —— 真正让缓存键失效的是
#: :func:`_run_tag` 那截随机段，见模块 docstring。
FAULT_SUFFIX: Final[str] = "+fault"

#: 产物文件名里的句序（``s003.wav``）
_SEQ_PATTERN: Final[re.Pattern[str]] = re.compile(r"^s(\d{1,6})\.wav$", re.IGNORECASE)


def sentence_seq(out_path: Path) -> int | None:
    """从产物文件名反推句序；认不出来 ⇒ ``None``（**不猜**）。"""
    match = _SEQ_PATTERN.match(out_path.name)
    return int(match.group(1)) if match is not None else None


class FaultEngine:
    """按计划失败的 :class:`~studio.tts.sentence.SentenceEngine` 包装。"""

    name: str
    revision: str

    def __init__(self, inner: SentenceEngine, plan: FaultPlan) -> None:
        self._inner = inner
        self._plan = plan
        #: 每一句已经失败过几次。**跨单元**累计 —— "重试"就是同一句的第二次引擎调用，
        #: 记在单元里的话每次重试都从 0 开始，这一句会永远失败下去（直到降级）。
        self._failures: dict[int, int] = {}
        self.name = f"{inner.name}{FAULT_SUFFIX}.{_run_tag()}"
        self.revision = inner.revision

    @property
    def inner(self) -> SentenceEngine:
        """被包着的那个引擎（排障时想知道"到底是谁在念"）。"""
        return self._inner

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        seq = sentence_seq(out_path)
        if self._plan.tts_down:
            raise _down(out_path, seq)
        if seq is not None and seq == self._plan.tts_fail_sentence:
            failed = self._failures.get(seq, 0)
            if failed < self._plan.tts_fail_times:
                self._failures[seq] = failed + 1
                raise _flaky(out_path, seq, failed + 1, self._plan.tts_fail_times)
        self._inner.synthesize(text, out_path, voice=voice, rate=rate)


def _run_tag() -> str:
    """这一场演练的标记（进程号 + 随机段）—— 同一个进程里装两次也不会撞键。"""
    return f"{os.getpid():x}{uuid.uuid4().hex[:6]}"


def wrap_engine(engine: SentenceEngine, plan: FaultPlan) -> SentenceEngine:
    """``plan`` 生效 ⇒ 包一层；否则**原样返回**（没配故障时不留下任何多余的壳）。"""
    return FaultEngine(engine, plan) if plan.enabled else engine


def _down(out_path: Path, seq: int | None) -> StudioError:
    return StudioError(
        f"演练注入：引擎不可用（{out_path.name}）",
        code=ErrorCode.TTS_ENGINE_DOWN,
        context={"audio": out_path.as_posix(), "seq": seq, "fault": "tts_down"},
        remediation="这是 STUDIO_FAULT 故意造的故障，不是真问题；清掉环境变量即恢复",
    )


def _flaky(out_path: Path, seq: int, failed: int, planned: int) -> StudioError:
    return StudioError(
        f"演练注入：第 {seq} 句第 {failed}/{planned} 次失败",
        code=ErrorCode.TTS_ENGINE_DOWN,
        context={"audio": out_path.as_posix(), "seq": seq, "failed": failed, "planned": planned},
        remediation="这是 STUDIO_FAULT 故意造的故障；到次数就会成功（验的是重试链）",
    )
