"""降级决策表（T2.3 · §04.3.3）—— 那张表**逐行**。

为什么一行一个用例
------------------
"OOM 第二次要切引擎"这种规则，混在 ``voice_worker._on_failure`` 里是**验不出来**的：
要验它得先造一个真的 OOM（造不出来），于是那条规则永远没人跑过。把表抽成纯函数
之后，每一行都只是一次调用 —— 所以这里一行一个用例，改表就改灯。

这里**只**验"这一句下一步做什么"。熔断（池级）在
``tests/unit/tts/test_circuit.py``，"执行那一步"在
``tests/unit/pools/test_voice_worker.py``。
"""

from __future__ import annotations

from typing import cast

import pytest

from studio.core.errors import ErrorCode
from studio.tts.fallback import (
    ACTION_NOTES,
    DEGRADE_AFTER_ATTEMPTS,
    DefaultFallbackPolicy,
    DegradeAction,
    FailureState,
    action_note,
    decide,
)


def test_the_seven_actions_match_the_spec_verbatim() -> None:
    """§04.3.3 的枚举**逐字**（多一个 / 少一个都是契约漂移）。"""
    assert [action.value for action in DegradeAction] == [
        "retry_same",
        "rewarm_engine",
        "retry_simplified",
        "split_and_merge",
        "switch_engine",
        "placeholder",
        "fail_task",
    ]


def test_the_degrade_line_is_three() -> None:
    """§04.3.3 不变量 3：``tts_attempts >= 3 ⇒ skipped``。"""
    assert DEGRADE_AFTER_ATTEMPTS == 3


# ══════════════════════════════════════════════════════════════════════
# ① 配置错误 ⇒ 不降级
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("attempt", [1, 2, 3, 9])
def test_a_missing_voice_fails_the_task_at_any_attempt(attempt: int) -> None:
    """音色缺失是**配置错误**：做成静音占位的话，用户拿到一支"出片成功、整片没人声"
    的片子，而真正的错因（音色名写错）被"这条片子进了字幕模式"盖过去。"""
    action = decide(code=ErrorCode.TTS_VOICE_MISSING.value, state=FailureState(attempt=attempt))
    assert action is DegradeAction.FAIL_TASK


# ══════════════════════════════════════════════════════════════════════
# ② 显存不足：先卸了重载，第二次才切备用引擎
# ══════════════════════════════════════════════════════════════════════


def test_oom_on_the_first_attempt_rewarms_the_engine() -> None:
    action = decide(code=ErrorCode.TTS_OOM.value, state=FailureState(attempt=1))
    assert action is DegradeAction.REWARM_ENGINE


def test_oom_on_the_second_attempt_switches_the_engine() -> None:
    action = decide(code=ErrorCode.TTS_OOM.value, state=FailureState(attempt=2))
    assert action is DegradeAction.SWITCH_ENGINE


def test_oom_after_switching_degrades() -> None:
    """切过之后还 OOM ⇒ 占位。**没有再下一档可切**，重试只是白等。"""
    state = FailureState(attempt=2, switched=True)
    assert decide(code=ErrorCode.TTS_OOM.value, state=state) is DegradeAction.PLACEHOLDER


def test_oom_never_switches_the_engine_twice() -> None:
    state = FailureState(attempt=5, switched=True)
    assert decide(code=ErrorCode.TTS_OOM.value, state=state) is DegradeAction.PLACEHOLDER


# ══════════════════════════════════════════════════════════════════════
# ③ 引擎没了 / 连不上 / 忙：唤醒**一次**，然后退避到线
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.TTS_ENGINE_DOWN.value,
        ErrorCode.TTS_ENGINE_UNAVAILABLE.value,
        ErrorCode.TTS_BUSY.value,
    ],
)
def test_engine_trouble_rewarms_on_the_first_attempt(code: str) -> None:
    assert decide(code=code, state=FailureState(attempt=1)) is DegradeAction.REWARM_ENGINE


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.TTS_ENGINE_DOWN.value,
        ErrorCode.TTS_ENGINE_UNAVAILABLE.value,
        ErrorCode.TTS_BUSY.value,
    ],
)
def test_engine_trouble_retries_on_the_second_attempt(code: str) -> None:
    """**只在第一次**唤醒：每次都唤醒的话，"引擎起不来"会变成"每句都重载一次模型"
    （真机 20s 一次），而"到线降级"永远到不了。"""
    assert decide(code=code, state=FailureState(attempt=2)) is DegradeAction.RETRY_SAME


def test_engine_trouble_degrades_at_the_line() -> None:
    state = FailureState(attempt=DEGRADE_AFTER_ATTEMPTS)
    assert decide(code=ErrorCode.TTS_ENGINE_DOWN.value, state=state) is DegradeAction.PLACEHOLDER


# ══════════════════════════════════════════════════════════════════════
# ④ 超时：长句是主因 ⇒ 再切一刀
# ══════════════════════════════════════════════════════════════════════


def test_a_splittable_timeout_is_split() -> None:
    state = FailureState(attempt=1, split_available=True)
    assert decide(code=ErrorCode.TTS_TIMEOUT.value, state=state) is DegradeAction.SPLIT_AND_MERGE


def test_a_sentence_that_cannot_be_split_just_retries() -> None:
    """本来就是一段（切不动）⇒ 切分这张牌打不出来，走退避。"""
    state = FailureState(attempt=1, split_available=False)
    assert decide(code=ErrorCode.TTS_TIMEOUT.value, state=state) is DegradeAction.RETRY_SAME


def test_timeout_splits_only_once() -> None:
    """切过了还超时 ⇒ 不再切（切第二次不会变短）。"""
    state = FailureState(attempt=2, split_available=True, split=True)
    assert decide(code=ErrorCode.TTS_TIMEOUT.value, state=state) is DegradeAction.RETRY_SAME


def test_a_split_timeout_degrades_at_the_line() -> None:
    state = FailureState(attempt=DEGRADE_AFTER_ATTEMPTS, split_available=True, split=True)
    assert decide(code=ErrorCode.TTS_TIMEOUT.value, state=state) is DegradeAction.PLACEHOLDER


# ══════════════════════════════════════════════════════════════════════
# ⑤ 假成功（静音 / 爆音）：换一组合成参数再试一次
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("code", [ErrorCode.TTS_SILENT.value, ErrorCode.TTS_CLIP.value])
def test_bad_audio_retries_simplified_first(code: str) -> None:
    """真机：同一个音色换个 seed 就正常 —— 所以先别动引擎。"""
    assert decide(code=code, state=FailureState(attempt=1)) is DegradeAction.RETRY_SIMPLIFIED


@pytest.mark.parametrize("code", [ErrorCode.TTS_SILENT.value, ErrorCode.TTS_CLIP.value])
def test_bad_audio_simplifies_only_once(code: str) -> None:
    state = FailureState(attempt=2, simplified=True)
    assert decide(code=code, state=state) is DegradeAction.RETRY_SAME


def test_bad_audio_degrades_at_the_line() -> None:
    state = FailureState(attempt=DEGRADE_AFTER_ATTEMPTS, simplified=True)
    assert decide(code=ErrorCode.TTS_SILENT.value, state=state) is DegradeAction.PLACEHOLDER


# ══════════════════════════════════════════════════════════════════════
# ⑥ 文本非法：SPLIT_AND_MERGE → PLACEHOLDER（**二次失败即占位**）
# ══════════════════════════════════════════════════════════════════════


def test_invalid_text_is_split_first() -> None:
    state = FailureState(attempt=1, split_available=True)
    assert decide(code=ErrorCode.TTS_TEXT_INVALID.value, state=state) is DegradeAction.SPLIT_AND_MERGE


def test_invalid_text_goes_straight_to_placeholder_after_splitting() -> None:
    """规格原文的箭头是"二次失败即占位" —— **不再退避重试**：
    同一段文本喂回去，结果不会变，而每一次重试都要等一轮退避。"""
    state = FailureState(attempt=2, split_available=True, split=True)
    assert decide(code=ErrorCode.TTS_TEXT_INVALID.value, state=state) is DegradeAction.PLACEHOLDER


def test_invalid_text_that_cannot_be_split_goes_straight_to_placeholder() -> None:
    state = FailureState(attempt=1, split_available=False)
    assert decide(code=ErrorCode.TTS_TEXT_INVALID.value, state=state) is DegradeAction.PLACEHOLDER


# ══════════════════════════════════════════════════════════════════════
# ⑦ 其余（含未知码）：退避重试到线再降级
# ══════════════════════════════════════════════════════════════════════


def test_a_single_sentence_failure_retries_then_degrades() -> None:
    code = ErrorCode.TTS_SENTENCE_FAILED.value
    assert decide(code=code, state=FailureState(attempt=1)) is DegradeAction.RETRY_SAME
    assert decide(code=code, state=FailureState(attempt=2)) is DegradeAction.RETRY_SAME
    assert decide(code=code, state=FailureState(attempt=3)) is DegradeAction.PLACEHOLDER


def test_an_unknown_code_falls_into_the_same_line() -> None:
    """服务回了个我们不认识的码 ⇒ 当成"重试三次然后静音"，而不是直接放弃任务
    （P4 无人值守优先），而且三次的失败原因都留在 ``tts_error`` 里。"""
    code = "SOMETHING_WE_HAVE_NEVER_SEEN"
    assert decide(code=code, state=FailureState(attempt=1)) is DegradeAction.RETRY_SAME
    assert decide(code=code, state=FailureState(attempt=3)) is DegradeAction.PLACEHOLDER


def test_a_qc_failure_that_is_not_silence_or_clip_retries() -> None:
    """``TTS_AUDIO_QC_FAILED``（时长 0 / 探针读不出）与静音 / 爆音不是一回事：
    它没有"换参数就能好"的判据，所以走退避。"""
    code = ErrorCode.TTS_AUDIO_QC_FAILED.value
    assert decide(code=code, state=FailureState(attempt=1)) is DegradeAction.RETRY_SAME


def test_an_attempt_count_read_back_from_the_db_still_degrades() -> None:
    """``>=`` 而不是 ``==``：库里读回来是 4（手工改过库 / 上一轮没写成功）也**到线了**。
    用 ``==`` 的话那种句子会永远重试下去，每次都在等一次退避。"""
    code = ErrorCode.TTS_SENTENCE_FAILED.value
    assert decide(code=code, state=FailureState(attempt=4)) is DegradeAction.PLACEHOLDER


# ══════════════════════════════════════════════════════════════════════
# 表的性质：纯、可注入、每种动作都有人话
# ══════════════════════════════════════════════════════════════════════


def test_the_table_is_pure() -> None:
    """同样的输入永远给同样的动作 —— 表里**不许**藏时钟 / 随机 / 状态。"""
    state = FailureState(attempt=2, split_available=True)
    first = decide(code=ErrorCode.TTS_TIMEOUT.value, state=state)
    second = decide(code=ErrorCode.TTS_TIMEOUT.value, state=state)
    assert first is second


def test_the_default_policy_forwards_to_the_table() -> None:
    policy = DefaultFallbackPolicy()
    state = FailureState(attempt=1)
    assert policy.decide(code=ErrorCode.TTS_OOM.value, state=state) is decide(
        code=ErrorCode.TTS_OOM.value, state=state
    )


def test_every_action_has_its_own_note() -> None:
    """光写 ``retry_simplified`` 等于让人去翻源码才知道刚才发生了什么。"""
    assert set(ACTION_NOTES) == set(DegradeAction)
    assert len(set(ACTION_NOTES.values())) == len(DegradeAction)
    for action in DegradeAction:
        assert action_note(action)


def test_an_unknown_action_still_gets_a_note() -> None:
    """查表兜底成枚举值本身，**绝不抛** —— 它跑在日志 / 留痕路径上，
    在那里抛异常会把真正的错因顶掉。"""
    assert action_note(cast(DegradeAction, "no_such_action")) == "no_such_action"
