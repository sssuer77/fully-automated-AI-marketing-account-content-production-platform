"""降级决策表（T2.3 · §04.3.3）—— 「这一句失败了，接下来做什么」。

规格里的那张表，逐条落在这里
----------------------------
```text
显存不足 TTS_OOM              attempt 1      ⇒ REWARM_ENGINE（卸了重载，R4）
显存不足 TTS_OOM              attempt ≥2     ⇒ SWITCH_ENGINE（切 CPU 备用档）
超时     TTS_TIMEOUT          任意           ⇒ SPLIT_AND_MERGE（长句是超时主因）
静音/爆音 TTS_SILENT/CLIP     任意           ⇒ RETRY_SIMPLIFIED
文本非法 TTS_TEXT_INVALID     任意           ⇒ SPLIT_AND_MERGE → PLACEHOLDER
引擎崩   TTS_ENGINE_DOWN      attempt ≥1     ⇒ REWARM_ENGINE
音色缺失 TTS_VOICE_MISSING    任意           ⇒ FAIL_TASK
```
（"连续失败 ≥3 句 ⇒ 熔断"不在这张表里：它是**池级**的结论，见
:mod:`studio.tts.circuit`。这一张回答的是"**这一句**下一步做什么"。）

为什么是纯函数，而不是"在异常处理里写一串 if"
--------------------------------------------
因为这张表是**契约**，而契约要被逐条钉住。现在它一行一个用例，加一条规则就加
一个用例；混进 `voice_worker._on_failure` 的话，验"OOM 第二次要切引擎"得先造一个
真的 OOM —— 于是那条规则永远没人验（造不出来）。

执行那侧（`voice_worker`）因此只剩一个 `match action:`，每种动作的**落点**在那边
写一次。

为什么吃**错误码字符串**而不是异常对象
------------------------------------
码有四个来源，它们不是同一个类：服务端回的信封（`TTS_OOM` / `TTS_BUSY`）、
本地 QC 判据（`TTS_SILENT` / `TTS_CLIP`）、演练注入（`TTS_ENGINE_DOWN`）、
以及队列自己的失败。按类型分流会让"服务回了个我们不认识的码"落进 `except` 的
兜底分支 —— 而那正是最该被看见的一种失败。吃字符串，未知码就走"到线降级"。

为什么 `attempt` 是"这一句"的次数
--------------------------------
降级链是**逐句**的：第 3 句 OOM 不该让第 4 句直接跳到最后一步。所以计数来自
`script_sentences.tts_attempts`（§04.3.3 不变量 3），而不是池的统计。
"连续失败句数"是另一回事，它进熔断器。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from studio.core.errors import ErrorCode

__all__ = [
    "ACTION_NOTES",
    "DEGRADE_AFTER_ATTEMPTS",
    "DefaultFallbackPolicy",
    "DegradeAction",
    "FailureState",
    "FallbackPolicy",
    "action_note",
    "decide",
]

#: 到几次失败就把这一句降级成静音（§04.3.3 不变量 3：``tts_attempts >= 3 ⇒ skipped``）。
#: **定义在这里**而不是池里：它是决策表的输入（`_terminal` 那条线），池只是执行者。
DEGRADE_AFTER_ATTEMPTS: Final[int] = 3


class DegradeAction(StrEnum):
    """一句念不出来时的七种去处（与 §04.3.3 的枚举**逐字一致**）。"""

    #: 原样重试（瞬时故障：网络抖动、服务刚起）
    RETRY_SAME = "retry_same"
    #: 卸了重载再试（显存不足 / 引擎进程崩了）
    REWARM_ENGINE = "rewarm_engine"
    #: 去 emotion、speed=1.0、换 seed 再试（静音 / 爆音 ⇒ 多半是这一句的合成参数）
    RETRY_SIMPLIFIED = "retry_simplified"
    #: 再切分后分段合成并无缝拼接（超时 / 文本超长）
    SPLIT_AND_MERGE = "split_and_merge"
    #: 切备用引擎（常驻服务不可用 ⇒ 系统语音包，保证成片有人声）
    SWITCH_ENGINE = "switch_engine"
    #: 等长静音占位 + 保留字幕（"字幕模式"，§04.3.3 的终点）
    PLACEHOLDER = "placeholder"
    #: 放弃并置任务失败（配置错误 —— 修配置不算人工介入流程）
    FAIL_TASK = "fail_task"


#: 每种动作的**人话**（日志 / 留痕 / 面板都要显示它）。
#: 光写 ``retry_simplified`` 等于让人去翻源码才知道刚才发生了什么。
ACTION_NOTES: Final[Mapping[DegradeAction, str]] = {
    DegradeAction.RETRY_SAME: "原样重试",
    DegradeAction.REWARM_ENGINE: "卸载模型后重载再试",
    DegradeAction.RETRY_SIMPLIFIED: "去 emotion / speed=1.0 / 换 seed 再试",
    DegradeAction.SPLIT_AND_MERGE: "再切分后分段合成并拼接",
    DegradeAction.SWITCH_ENGINE: "切换到备用引擎",
    DegradeAction.PLACEHOLDER: "降级为等长静音占位（字幕保留）",
    DegradeAction.FAIL_TASK: "放弃这一句并置任务失败",
}


def action_note(action: DegradeAction) -> str:
    """动作 ⇒ 一句人话（查表兜底成枚举值本身，绝不抛）。"""
    return ACTION_NOTES.get(action, str(action))


@dataclass(frozen=True, slots=True)
class FailureState:
    """这一句**已经试过哪些招**（决策表的另一半输入）。

    ``simplified`` / ``split`` / ``switched`` 是"牌打过了没有"，与 ``attempt``
    不是一回事：``attempt`` 数的是"失败了几次"（含退避重试），而这三张牌各自
    只该打一次 —— 打第二次不会变好，只会把"到线降级"往后拖三次退避（真机上
    那就是三次 60 秒的超时）。

    这些标志**住在配音池进程内**（``voice_worker`` 的三个集合），而不是库里：
    ``script_sentences`` 没有这三列，要落库就得加三列 —— 而它们只在降级链那几步
    里有意义（不过度设计）。进程重启后"退回原参数再试一遍"是**可接受**的：``attempt``
    在库里累计，所以不会死循环，最多多试一次。

    ``split_available`` 是**实时算**的（这一句当前的文本切得动吗），不靠任何状态。
    """

    #: 这一句累计失败次数（含本次）
    attempt: int = 1
    #: 已经简化重试过
    simplified: bool = False
    #: 已经切分重试过
    split: bool = False
    #: 已经换过引擎
    switched: bool = False
    #: 还能不能再切（这一句当前**多于一段** ⇒ 切得动）
    split_available: bool = False


#: "引擎这台机器不可用"那一族：唤醒它有意义，重试也有意义。
#: ``TTS_BUSY`` 是"此刻忙"（背压），与"引擎没了"归成一族是因为**处置相同**
#: （退避重试就会好）—— 而 §04.3.3 的表里没有它，这里不另立一条。
_ENGINE_TROUBLE: Final[frozenset[str]] = frozenset(
    {
        ErrorCode.TTS_ENGINE_DOWN.value,
        ErrorCode.TTS_ENGINE_UNAVAILABLE.value,
        ErrorCode.TTS_BUSY.value,
    }
)

#: "引擎说成功、产物其实不能听"那一族：合成参数可疑，简化重试。
_BAD_AUDIO: Final[frozenset[str]] = frozenset({ErrorCode.TTS_SILENT.value, ErrorCode.TTS_CLIP.value})


def _terminal(state: FailureState) -> DegradeAction:
    """退无可退：到线 ⇒ 占位，否则原样重试。

    ``>=`` 而不是 ``==``：计数是从库里读回来的，而"读回来的时候已经是 4"完全
    可能（手工改过库、或者上一轮降级没写成功）—— 用 ``==`` 的话那种句子会
    **永远重试下去**，每次都在等一次退避。
    """
    return DegradeAction.PLACEHOLDER if state.attempt >= DEGRADE_AFTER_ATTEMPTS else DegradeAction.RETRY_SAME


def decide(*, code: str, state: FailureState) -> DegradeAction:  # noqa: PLR0911
    """§04.3.3 的决策表（**纯函数**：同样的输入永远给同样的动作**）。

    ``PLR0911``（return 太多）在这一个函数上是**故意**的：它就是那张表，
    每一行一个失败类型 ⇒ 一个动作。改写成查表（`dict[code, ...]`）会让
    "attempt 分档 + 牌打过了没有"这些条件没处放，最后还是要写一堆 if。
    规则的形状在这里比规则的整洁更重要 —— 它是契约，要被逐条读、逐条测。
    """
    # ① 配置错误 ⇒ 不降级。音色名写错了，"再试三次然后静音"只会把一条配错了的
    # 任务做完 —— 而用户看到的是"出片成功"，听到的是没人声。修配置不是人工介入流程
    # （§04.3.3 原话），所以这里必须响亮地失败。
    if code == ErrorCode.TTS_VOICE_MISSING.value:
        return DegradeAction.FAIL_TASK

    # ② 显存不足：先卸了重载（真机实测释放 2.4 GB），第二次才切备用引擎。
    # 切完还 OOM ⇒ 到线降级 —— 说明这一句在这台机器上就是念不出来。
    if code == ErrorCode.TTS_OOM.value:
        if state.attempt <= 1:
            return DegradeAction.REWARM_ENGINE
        if not state.switched:
            return DegradeAction.SWITCH_ENGINE
        return DegradeAction.PLACEHOLDER

    # ③ 引擎没了 / 连不上 / 忙：唤醒一次再试。**只在第一次**唤醒 ——
    # 每次都唤醒的话，"引擎起不来"会变成"每句都重载一次模型"（真机 20s 一次），
    # 而"到线降级"永远到不了。
    if code in _ENGINE_TROUBLE:
        if state.attempt <= 1:
            return DegradeAction.REWARM_ENGINE
        return _terminal(state)

    # ④ 超时：长句是主因 ⇒ 再切一刀。切不动了（本来就是一段）就到线降级。
    if code == ErrorCode.TTS_TIMEOUT.value:
        if state.split_available and not state.split:
            return DegradeAction.SPLIT_AND_MERGE
        return _terminal(state)

    # ⑤ 假成功（静音 / 爆音）：换一组合成参数再试一次。这多半是 emotion 或 seed
    # 落在了一个坏点上（真机：同一个音色换个 seed 就正常），而不是引擎坏了 ——
    # 所以先别动引擎。
    if code in _BAD_AUDIO:
        if not state.simplified:
            return DegradeAction.RETRY_SIMPLIFIED
        return _terminal(state)

    # ⑥ 文本引擎吃不下：切分是唯一有意义的动作。**切完还不行就直接占位**，
    # 不再退避重试（规格原文："SPLIT_AND_MERGE → PLACEHOLDER，二次失败即占位"）：
    # 同一段文本喂回去，结果不会变，而每一次重试都要等一轮退避。
    if code == ErrorCode.TTS_TEXT_INVALID.value:
        if state.split_available and not state.split:
            return DegradeAction.SPLIT_AND_MERGE
        return DegradeAction.PLACEHOLDER

    # ⑦ 其余（单句合成失败 / QC 失败 / 服务回了个我们不认识的码）：退避重试到线
    # 再降级。**未知码也走这条** —— 把它当成"重试三次然后静音"，比"直接放弃任务"
    # 更符合 P4（无人值守优先），而且三次的失败原因都留在 `tts_error` 里。
    return _terminal(state)


class FallbackPolicy(Protocol):
    """决策表的接口（§04.3.3 的 ``FallbackPolicy``）。

    存在的理由是**可注入**：`voice_worker` 持有它，测试可以把"某条码 ⇒ 某个动作"
    钉死，从而单独验"执行"那一侧（决策本身由 :func:`decide` 的用例逐条守住）。
    """

    def decide(self, *, code: str, state: FailureState) -> DegradeAction: ...


class DefaultFallbackPolicy:
    """缺省策略：转发到 :func:`decide`（一张表，一个实现）。"""

    __slots__ = ()

    def decide(self, *, code: str, state: FailureState) -> DegradeAction:
        return decide(code=code, state=state)
