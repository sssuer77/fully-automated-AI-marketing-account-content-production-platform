"""``core.faults``（T2.8）—— 故障注入开关的解析。

为什么这一层值得单独测
----------------------
``STUDIO_FAULT`` 是**演练**的入口，而演练最容易出的一种坏结局是"全绿但什么都没发生"：
拼错一个键、写了个认不出来的布尔词，解析器要是静默忽略，门禁就会替你相信一个
没验过的结论。所以这里逐个钉死"读不懂就报错"。
"""

from __future__ import annotations

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.faults import (
    DEFAULT_FAIL_TIMES,
    FAULT_ENV,
    FaultPlan,
    fault_plan_from_env,
    parse_faults,
)


def test_an_empty_value_injects_nothing() -> None:
    """没配 / 只配了空白 ⇒ 空计划（``enabled=False``，下游连壳都不包）。"""
    for raw in (None, "", "   ", ";;"):
        plan = parse_faults(raw)
        assert plan == FaultPlan()
        assert plan.enabled is False


@pytest.mark.parametrize("token", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_spellings_turn_the_engine_off(token: str) -> None:
    """真值的几种写法都认（大小写不敏感）。"""
    plan = parse_faults(f"tts_down={token}")
    assert plan.tts_down is True
    assert plan.enabled is True


@pytest.mark.parametrize("token", ["0", "false", "no", "off"])
def test_falsy_spellings_are_explicit_off(token: str) -> None:
    """显式写"关"也算写对了 —— 与"没写"是同一件事，但意图明确。"""
    plan = parse_faults(f"tts_down={token}")
    assert plan.tts_down is False
    assert plan.enabled is False


def test_a_bool_that_is_not_a_word_is_refused() -> None:
    """``tts_down=maybe`` 不能当成"开"，更不能当成"关" —— 只有报错。"""
    with pytest.raises(StudioError) as caught:
        parse_faults("tts_down=maybe")
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert "tts_down" in caught.value.message


def test_an_unknown_key_is_refused() -> None:
    """★ 拼错的键必须报错：静默忽略的话，演练会"全绿但什么都没发生"。"""
    with pytest.raises(StudioError) as caught:
        parse_faults("tts_downn=1")
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert "tts_downn" in caught.value.message


def test_the_per_sentence_fault_defaults_its_retry_count() -> None:
    """``tts_fail_sentence`` 不给次数 ⇒ 默认失败 2 次（正好走过一次退避重试）。"""
    plan = parse_faults("tts_fail_sentence=3")
    assert (plan.tts_fail_sentence, plan.tts_fail_times) == (3, DEFAULT_FAIL_TIMES)
    assert plan.enabled is True


def test_the_retry_count_can_be_pinned() -> None:
    plan = parse_faults("tts_fail_sentence=2;tts_fail_times=1")
    assert (plan.tts_fail_sentence, plan.tts_fail_times) == (2, 1)


def test_commas_work_like_semicolons() -> None:
    """PowerShell 里分号要转义，所以逗号也认 —— 两种写法解析出同一份计划。"""
    assert parse_faults("tts_down=1,tts_fail_sentence=2") == parse_faults("tts_down=1;tts_fail_sentence=2")


def test_a_lone_retry_count_is_refused() -> None:
    """``tts_fail_times`` 单独出现说明作者以为自己配上了什么 —— 报错。"""
    with pytest.raises(StudioError) as caught:
        parse_faults("tts_fail_times=3")
    assert caught.value.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize("raw", ["tts_fail_sentence=0", "tts_fail_sentence=-1", "tts_fail_sentence=x"])
def test_the_sentence_number_must_be_a_positive_integer(raw: str) -> None:
    """句序从 1 起；0 / 负数 / 非数字一律报错（不猜作者想说什么）。"""
    with pytest.raises(StudioError) as caught:
        parse_faults(raw)
    assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_a_repeated_key_is_refused() -> None:
    """同一个键写两遍 ⇒ 报错：哪一种写法生效不该由解析顺序决定。"""
    with pytest.raises(StudioError) as caught:
        parse_faults("tts_down=1;tts_down=0")
    assert caught.value.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize("raw", ["tts_down", "=1", "tts_down="])
def test_a_bare_word_is_refused(raw: str) -> None:
    """不是 ``键=值`` 的一律报错（``tts_down=`` 缺值、光秃秃一个词都算）。"""
    with pytest.raises(StudioError) as caught:
        parse_faults(raw)
    assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_a_trailing_separator_is_harmless() -> None:
    """结尾 / 中间多一个分号不算写错（手写环境变量时太容易多打一个）。"""
    assert parse_faults("tts_down=1;").tts_down is True
    assert parse_faults("tts_down=1;;").tts_down is True


def test_the_plan_reads_the_documented_env_var() -> None:
    """读的是 ``STUDIO_FAULT`` 这一个变量（名字本身也是契约）。"""
    assert fault_plan_from_env({FAULT_ENV: "tts_down=1"}).tts_down is True
    assert fault_plan_from_env({}).enabled is False
    assert fault_plan_from_env({"STUDIO_FAULTS": "tts_down=1"}).enabled is False


def test_the_plan_dumps_itself_for_logging() -> None:
    """落日志用：装配期打一行，排障时一眼看出"这次是不是演练"。"""
    assert parse_faults("tts_fail_sentence=3").to_dict() == {
        "tts_down": False,
        "tts_fail_sentence": 3,
        "tts_fail_times": DEFAULT_FAIL_TIMES,
    }
