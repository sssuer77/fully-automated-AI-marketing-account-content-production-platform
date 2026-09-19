"""配音熔断器（T2.3 · §04.3.3）—— 连续几句念不出来，就别再一句句白等了。

它防的是什么
------------
引擎坏了（显存炸了 / 服务挂了 / 权重目录被删了）时，**每一句**都要走完
"调用 ⇒ 超时 ⇒ 退避 ⇒ 重试 ⇒ 到线降级"这条链。真机上一句 60 秒超时 × 3 次重试
× N 句 —— 三十句的任务要跑一个半小时，而它从第 3 句起就已经注定整片没人声了。
熔断就是"从第 3 句起就承认这件事"：不再调用引擎，剩下的句子直接走占位，
把时间还给用户（§04.3.3："熔断后任务不失败，转字幕模式"）。

三个状态，以及为什么必须有半开
------------------------------
```text
CLOSED ──连续失败 ≥ threshold──▶ OPEN ──过了 open_sec──▶ HALF_OPEN ──成功──▶ CLOSED
   ▲                                                        │
   └──────────────────── 失败（重新计时）───────────────────┘
```
只有 CLOSED / OPEN 两个状态的话，时间一到就全放行 —— 而"引擎其实还没起来"会让
整整一批句子同时撞上去（真机：冷加载 20s，那批句子会一起超时）。HALF_OPEN
**只放一次**探测：成功才真正合闸，失败就重新计时。这一个状态就是"叫醒一个睡着的
服务"与"撞一个坏掉的服务"之间的区别。

为什么**不**去改 `pool_settings.paused`
--------------------------------------
§04.3.3 写的是"池暂停 5min"，但那个旋钮是**人工**的（T4.10 面板上的暂停/恢复，
写库、留痕、要人确认才恢复）。自动去动它，症状是"面板上显示暂停，而没人知道是
谁按的"—— 而且恢复还得靠人（那 5 分钟之后谁去点恢复？）。

熔断要的是"这段时间别白试"，而这件事在这一层就做完了：闸门打开期间 `allow()`
返回 `False` ⇒ 处理器**不调用引擎**，直接按占位收工。池子照常认领、照常往下走
（别的任务还能干），只有这一个动作被挡住。要人知道，就发一条
`system.alert(TTS_CIRCUIT_OPEN)`（error 级，永不合并）—— 看见告警、去查引擎，
而不是去猜面板上那个暂停是谁按的。

为什么状态在**进程内**
----------------------
与"池暂停"同理的另一面：它是**这台机器此刻能不能念**的结论，而不是任务数据。
进程重启（比如服务修好了、编排器拉起新 worker）时清零是**对的** —— 那一刻环境
已经变了，用五分钟前的结论挡住新的尝试只会让人以为"修了也没用"。代价是四个池
进程各有各的闸门（真机上 voice 池只有 1 个进程，所以这一点在一期不构成问题）。

为什么"连续失败"按**句**数，而不是按**次**数
--------------------------------------------
同一句本来就会被重试（决策表让它退避后再来），所以"一句挂了三次"是**正常**的
一轮降级，而它**不是三个信号** —— 它只是一个信号：这段文本这台引擎念不出来。
按次数计的话，一句难念的台词自己就能把闸门拉开，后果是**剩下的句子全部被静音**，
而那正是 `RETRY_SIMPLIFIED` 想避免的事。所以计数按句去重：一句只算一次，
念出来一句就归零（"连续"才成立）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.logging import get_logger

__all__ = [
    "DEFAULT_OPEN_SEC",
    "DEFAULT_THRESHOLD",
    "CircuitBreaker",
    "CircuitSnapshot",
    "CircuitState",
]

logger = get_logger("studio.tts.circuit")

#: 连续几句念不出来就打开闸门（§04.3.3 的"连续失败 ≥3 句 ⇒ 熔断"）
DEFAULT_THRESHOLD: Final[int] = 3

#: 闸门打开多久（§04.3.3 的"池暂停 5min"）。之后进入半开，放**一次**探测。
DEFAULT_OPEN_SEC: Final[float] = 300.0


class CircuitState(StrEnum):
    """闸门的三个状态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    """闸门现在什么样（日志 / 告警 / 面板读它）。"""

    state: str
    consecutive_failures: int
    trips: int
    threshold: int
    open_sec: float
    #: 这一轮是什么时候拉开的（ISO 时刻，给人看）；没开过 ⇒ ``None``
    opened_at: str | None
    #: 还要等多久才会放一次探测（毫秒）；不在 OPEN ⇒ 0
    remaining_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "trips": self.trips,
            "threshold": self.threshold,
            "open_sec": self.open_sec,
            "opened_at": self.opened_at,
            "remaining_ms": self.remaining_ms,
        }


class CircuitBreaker:
    """连续失败计数器 + 闸门（**线程安全**：配音池的 worker 是多个线程）。

    :param threshold: 连续几次失败打开闸门
    :param open_sec: 打开多久之后放一次探测
    :param clock: 单调时钟（测试注入假钟；**不能用 wall clock** —— 系统对时会让
        时间倒流，而"倒流"在 `elapsed >= open_sec` 这条判据上的表现是闸门永远
        打不开）
    :param now: ISO 时刻来源（只为日志与告警，不参与判断）
    """

    __slots__ = (
        "_clock",
        "_counted",
        "_failures",
        "_lock",
        "_now",
        "_open_sec",
        "_opened_at_iso",
        "_opened_at_mono",
        "_probe_in_flight",
        "_state",
        "_threshold",
        "_trips",
    )

    def __init__(
        self,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        open_sec: float = DEFAULT_OPEN_SEC,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = now_iso,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold 至少要 1：0 会让闸门一起步就是打开的")
        if open_sec <= 0:
            raise ValueError("open_sec 必须为正：0 会让半开变成每句都放行")
        self._threshold = threshold
        self._open_sec = open_sec
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._trips = 0
        self._opened_at_mono: float | None = None
        self._opened_at_iso: str | None = None
        self._probe_in_flight = False
        #: 这一轮里**已经计过数的句子**（同一句重试不再叠加，见模块 docstring）
        self._counted: set[str] = set()

    # ── 问 ──────────────────────────────────────────────────────────────

    def allow(self) -> bool:
        """现在**允许调用引擎**吗。

        ``False`` 的语义是"别试了，直接按占位收工" —— 不是"这次失败"。
        """
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.OPEN:
                if not self._elapsed_enough():
                    return False
                # 时间到了：转半开，放**这一次**探测（后面来的都被挡住）
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = True
                logger.info("tts.circuit_half_open", trips=self._trips)
                return True
            # HALF_OPEN：只放一次
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def blocking(self) -> bool:
        """闸门现在**是不是在挡**（``OPEN`` 或 ``HALF_OPEN``）。

        与 :meth:`allow` 的区别只有一条：它**没有副作用**。失败路径上要判的是
        "现在该不该再做点什么"，而那一步不该顺手把半开探测的名额用掉 ——
        名额是留给**下一次真的调用引擎**的。
        """
        with self._lock:
            return self._state is not CircuitState.CLOSED

    def snapshot(self) -> CircuitSnapshot:
        """当前状态（**不改状态** —— 面板与日志都会调它）。"""
        with self._lock:
            remaining = 0
            if self._state is CircuitState.OPEN and self._opened_at_mono is not None:
                left = self._open_sec - (self._clock() - self._opened_at_mono)
                remaining = max(0, round(left * 1000))
            return CircuitSnapshot(
                state=self._state.value,
                consecutive_failures=self._failures,
                trips=self._trips,
                threshold=self._threshold,
                open_sec=self._open_sec,
                opened_at=self._opened_at_iso,
                remaining_ms=remaining,
            )

    # ── 报 ──────────────────────────────────────────────────────────────

    def record_success(self) -> None:
        """念出来了一句 ⇒ 归零并合闸（半开的探测成功也走这里）。"""
        with self._lock:
            recovered = self._state is not CircuitState.CLOSED
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._counted.clear()
            self._probe_in_flight = False
            self._opened_at_mono = None
            self._opened_at_iso = None
        if recovered:
            logger.info("tts.circuit_closed", trips=self._trips)

    def record_failure(self, key: str) -> bool:
        """记**一句**念不出来（``key`` 是句子 id）。⇒ 闸门**是不是刚被拉开**。

        ``==`` 而不是 ``>=``：``>=`` 会在越线之后的每一次失败上重复返回 ``True``
        ⇒ 重复告警（与 T4.10 的自动降并发同一条，陷阱 #79）。

        ``key`` 去重：同一句的第 2、3 次失败**不计数**（它还是那一句）。不然一句
        难念的台词会自己拉开闸门，把整条片子的其余句子一起送进静音。
        """
        with self._lock:
            self._probe_in_flight = False
            if self._state is CircuitState.HALF_OPEN:
                # 探测失败 ⇒ 回 OPEN，重新计时（不叠加计数：它本来就是坏的）
                self._open()
                return True
            if self._state is CircuitState.OPEN:
                # 闸门开着的时候不该走到这里（`allow()` 已经挡住了）——
                # 真走到了只说明有人绕过了闸门，不重复告警。
                return False
            if key in self._counted:
                return False
            self._counted.add(key)
            self._failures += 1
            if self._failures < self._threshold:
                return False
            self._open()
            return True

    def reset(self) -> None:
        """人工清零（测试与运维用：修完引擎不必等那 5 分钟）。"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._counted.clear()
            self._probe_in_flight = False
            self._opened_at_mono = None
            self._opened_at_iso = None

    # ── 内部 ────────────────────────────────────────────────────────────

    def _open(self) -> None:
        """拉开闸门（**调用方必须持锁**）。"""
        self._state = CircuitState.OPEN
        self._opened_at_mono = self._clock()
        self._opened_at_iso = self._now()
        self._trips += 1
        logger.warning(
            "tts.circuit_open",
            consecutive_failures=self._failures,
            threshold=self._threshold,
            open_sec=self._open_sec,
            trips=self._trips,
        )

    def _elapsed_enough(self) -> bool:
        """闸门开够久了吗（**调用方必须持锁**）。"""
        if self._opened_at_mono is None:
            return True
        return (self._clock() - self._opened_at_mono) >= self._open_sec
