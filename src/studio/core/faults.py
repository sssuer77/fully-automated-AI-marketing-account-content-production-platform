"""故障注入开关 ``STUDIO_FAULT``（T2.8 · §04.3.3 的降级演练）。

为什么要有它
------------
"TTS 全挂 ⇒ 任务仍推进到 ``queued_render``"这条要求，光读代码验不了：真把 SAPI
弄坏（卸掉语音包）不可复现，而且**验完还得装回去**。一个环境变量把这条演练变成
一条命令，也让集成测试不必去 monkeypatch 生产路径里的内部函数 —— 测的是"演练
怎么走"，而不是"我们怎么把那个函数换掉"。

语法
----
``STUDIO_FAULT=<键>=<值>[;<键>=<值>]...``（逗号与分号等价，方便在 PowerShell 里写）

======================  ================  ========================================
键                      取值              效果
======================  ================  ========================================
``tts_down``            真值              每一次引擎调用都失败（``TTS_ENGINE_DOWN``）
``tts_fail_sentence``   句序（从 1 起）    这一句的**前 N 次**引擎调用失败
``tts_fail_times``      次数（默认 2）     配合 ``tts_fail_sentence``
======================  ================  ========================================

真值认 ``1`` / ``true`` / ``yes`` / ``on``，假值认 ``0`` / ``false`` / ``no`` / ``off``。
**别的取值一律报错**，认不出来的键同样报错（不静默忽略）—— 一个拼错的
``tts_downn=1`` 会让演练"全绿但什么都没发生"，那比演练失败更坏：门禁会替你相信
一个没验过的结论。

为什么是环境变量而不是配置项
----------------------------
配置是"这台机器长期这样跑"，故障注入是"这一次故意让它坏"。混在一起的话，一次
演练之后忘了改回来，产线会带着"引擎全挂"的配置一直跑下去 —— 而它看起来跟正常
配置一模一样，没有任何东西会提醒你。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from studio.core.errors import ErrorCode, StudioError

__all__ = [
    "DEFAULT_FAIL_TIMES",
    "FAULT_ENV",
    "FaultPlan",
    "fault_plan_from_env",
    "parse_faults",
]

#: 读哪个环境变量
FAULT_ENV: Final[str] = "STUDIO_FAULT"

#: 认得的"开" / "关"
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: ``tts_fail_sentence`` 不给次数时失败几次。
#: 取 **2** 而不是 1：池的降级线是 3 次（§04.3.3 不变量 3），"失败 2 次再成功"
#: 才真的走过"退避 ⇒ 重试 ⇒ 这次成了"那条边；只失败 1 次等于没验到重试。
DEFAULT_FAIL_TIMES: Final[int] = 2

#: 认得的键（写错一个字母就报错，理由见模块 docstring）
_KEYS: Final[frozenset[str]] = frozenset({"tts_down", "tts_fail_sentence", "tts_fail_times"})


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """这一次要注入哪些故障（``enabled=False`` ⇒ 原样放行，不留空壳）。"""

    tts_down: bool = False
    #: 第几句的引擎调用要失败（``None`` ⇒ 不按句注入）
    tts_fail_sentence: int | None = None
    #: ``tts_fail_sentence`` 那一句失败几次之后放行
    tts_fail_times: int = DEFAULT_FAIL_TIMES

    @property
    def enabled(self) -> bool:
        """这份计划会不会真的改行为（空计划不包引擎，见 ``tts.faults.wrap_engine``）。"""
        return self.tts_down or self.tts_fail_sentence is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tts_down": self.tts_down,
            "tts_fail_sentence": self.tts_fail_sentence,
            "tts_fail_times": self.tts_fail_times,
        }


def parse_faults(raw: str | None) -> FaultPlan:
    """把 ``STUDIO_FAULT`` 的原文解析成一份计划（空 / 全空白 ⇒ 什么都不注入）。"""
    text = (raw or "").strip()
    if not text:
        return FaultPlan()

    values: dict[str, str] = {}
    for chunk in text.replace(",", ";").split(";"):
        item = chunk.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        name = key.strip().lower()
        if not sep or not name:
            raise _bad(raw, f"{item!r} 不是 `键=值`")
        if name in values:
            raise _bad(raw, f"{name} 写了两次")
        values[name] = value.strip()

    unknown = sorted(set(values) - _KEYS)
    if unknown:
        raise _bad(raw, f"不认识 {' / '.join(unknown)}（只认 {' / '.join(sorted(_KEYS))}）")
    if "tts_fail_times" in values and "tts_fail_sentence" not in values:
        raise _bad(raw, "tts_fail_times 只在同时给了 tts_fail_sentence 时才有意义")

    times = _positive_int(raw, "tts_fail_times", values.get("tts_fail_times"))
    return FaultPlan(
        tts_down=_flag(raw, "tts_down", values.get("tts_down")),
        tts_fail_sentence=_positive_int(raw, "tts_fail_sentence", values.get("tts_fail_sentence")),
        tts_fail_times=DEFAULT_FAIL_TIMES if times is None else times,
    )


def fault_plan_from_env(env: Mapping[str, str] | None = None) -> FaultPlan:
    """从环境变量读一份计划（``env`` 缺省读 ``os.environ``，测试可以传一份假的）。"""
    source = os.environ if env is None else env
    return parse_faults(source.get(FAULT_ENV))


def _flag(raw: str | None, name: str, value: str | None) -> bool:
    """布尔开关：不写 = 关；写了就必须是认得的那几个词。"""
    if value is None:
        return False
    token = value.strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    raise _bad(raw, f"{name} 只认 {' / '.join(sorted(_TRUTHY | _FALSY))}（收到 {value!r}）")


def _positive_int(raw: str | None, name: str, value: str | None) -> int | None:
    """正整数（句序从 1 起）。不写 ⇒ ``None``；写了但不是正整数 ⇒ 报错。"""
    if value is None:
        return None
    try:
        number = int(value.strip())
    except ValueError as exc:
        raise _bad(raw, f"{name} 得是整数（收到 {value!r}）") from exc
    if number < 1:
        raise _bad(raw, f"{name} 得是 ≥1 的整数（收到 {value!r}）")
    return number


def _bad(raw: str | None, why: str) -> StudioError:
    return StudioError(
        f"STUDIO_FAULT 读不懂：{why}",
        code=ErrorCode.CONFIG_INVALID,
        context={"env": FAULT_ENV, "value": raw},
        remediation=(
            "写法是 `STUDIO_FAULT=tts_down=1`，或 `STUDIO_FAULT=tts_fail_sentence=3;tts_fail_times=2`"
        ),
    )
