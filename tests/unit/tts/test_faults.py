"""``tts.faults``（T2.8）—— 故障引擎：按计划把"这一句念不出来"造出来。

这一层要钉死的只有三件事，但它们都容易悄悄写反：
① ``tts_down`` 时**内层引擎一次都不能被调到**（不然演练没打到引擎，白演）；
② 按句注入时**只打那一句**，而且失败次数是**跨重试累计**的（记在单元里的话，
   每次重试都从 0 开始，这一句会永远失败下去，直到降级）；
③ 包装后的引擎名**每场都得变**：``synthesize_sentence`` 先查缓存再调引擎，名字不变
   就会命中缓存、根本不进引擎 —— 演练于是变成"什么都没发生"，而门禁照样绿。
   **一个固定的后缀不够**（真机演练踩过）：第二场演练会命中第一场写下的缓存。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.faults import FaultPlan
from studio.tts.cache import tts_cache_key
from studio.tts.faults import FAULT_SUFFIX, FaultEngine, sentence_seq, wrap_engine


class CountingEngine:
    """数得出调用次数的假引擎（不写文件 —— 这里验的是"有没有被调到"）。"""

    name = "counting"
    revision = "test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, text: str, out_path: Path, *, voice: str | None, rate: int) -> None:
        self.calls.append((text, out_path.name))


def _engine(plan: FaultPlan) -> tuple[FaultEngine, CountingEngine]:
    inner = CountingEngine()
    return FaultEngine(inner, plan), inner


def test_an_empty_plan_leaves_the_engine_untouched() -> None:
    """★ 没配故障 ⇒ **原样返回**同一个对象（不留空壳，也不改缓存键）。"""
    inner = CountingEngine()
    assert wrap_engine(inner, FaultPlan()) is inner


def test_a_plan_wraps_the_engine_and_renames_it() -> None:
    """★ 名字变了才绕得过缓存 —— 这是"演练一定打到引擎"的前提。"""
    wrapped = wrap_engine(CountingEngine(), FaultPlan(tts_down=True))
    assert isinstance(wrapped, FaultEngine)
    assert wrapped.name.startswith(f"counting{FAULT_SUFFIX}.")
    assert wrapped.revision == "test"
    assert wrapped.inner.name == "counting"


def test_two_drills_never_share_a_cache_key() -> None:
    """★ 同一个 plan 装两次，缓存键也必须不同。

    真机演练里踩到的就是这个：``+fault`` 是个**常量**后缀 ⇒ 第二场演练
    （``tts_down=1``）与第一场（``tts_fail_sentence=3``）算出**同一个键** ⇒ 四句全命中
    第一场写下的缓存 ⇒ 引擎一次都没被调到，而 ``voice.fault_injected tts_down=True``
    照打。判据落在**缓存键**上而不是名字上：名字只是手段，"这一遍必须真的去念"才是目的。
    """
    plan = FaultPlan(tts_down=True)
    first = wrap_engine(CountingEngine(), plan)
    second = wrap_engine(CountingEngine(), plan)

    def key(engine: Any) -> str:
        return tts_cache_key(
            engine=engine.name,
            engine_revision=engine.revision,
            voice_id="v",
            normalized_text="念这一句",
            speed=1.0,
            emotion="neutral",
            seed=None,
            sample_rate=22_050,
        )

    assert key(first) != key(second)


@pytest.mark.parametrize("name", ["s003.wav", "s1.wav", "S012.wav"])
def test_the_sentence_number_comes_from_the_file_name(name: str) -> None:
    """产物名由 ``seq`` 决定（§04.3.3 不变量 1）⇒ 句序可以从文件名反推。"""
    assert sentence_seq(Path(name)) == int(name[1:-4])


@pytest.mark.parametrize("name", ["voice_master.wav", "s.wav", "s00a.wav", "s003.mp3"])
def test_an_unrecognised_file_name_yields_no_sentence(name: str) -> None:
    """认不出来 ⇒ ``None``：**不猜**（宁可这一句不失败，也不要失败到别的句子头上）。"""
    assert sentence_seq(Path(name)) is None


def test_tts_down_fails_every_call_without_touching_the_engine() -> None:
    """★ 引擎全挂：每次都抛 ``TTS_ENGINE_DOWN``，内层引擎一次都没被调到。"""
    engine, inner = _engine(FaultPlan(tts_down=True))

    for seq in (1, 2, 3):
        with pytest.raises(StudioError) as caught:
            engine.synthesize("念这一句", Path(f"s{seq:03d}.wav"), voice="v", rate=0)
        assert caught.value.code is ErrorCode.TTS_ENGINE_DOWN

    assert inner.calls == []


def test_the_per_sentence_fault_only_hits_that_sentence() -> None:
    """★ 只有第 2 句失败；第 1、3 句照常念出来。"""
    engine, inner = _engine(FaultPlan(tts_fail_sentence=2, tts_fail_times=2))

    engine.synthesize("第一句", Path("s001.wav"), voice="v", rate=0)
    engine.synthesize("第三句", Path("s003.wav"), voice="v", rate=0)
    for _ in range(2):
        with pytest.raises(StudioError):
            engine.synthesize("第二句", Path("s002.wav"), voice="v", rate=0)
    engine.synthesize("第二句", Path("s002.wav"), voice="v", rate=0)

    assert [name for _text, name in inner.calls] == ["s001.wav", "s003.wav", "s002.wav"]


def test_the_failure_count_survives_retries() -> None:
    """★ 失败次数**跨重试累计**：池重试时是同一句的第二次引擎调用，不能从头再数。

    记在"这一次调用"里的话，每次重试都从 0 开始 —— 这一句会永远失败下去，
    直到撞上 3 次降级线变成静音，而它本该在第 3 次就念出来。
    """
    engine, inner = _engine(FaultPlan(tts_fail_sentence=1, tts_fail_times=2))

    for attempt in (1, 2):
        with pytest.raises(StudioError) as caught:
            engine.synthesize("第一句", Path("s001.wav"), voice="v", rate=0)
        assert f"第 {attempt}/2 次" in caught.value.message

    engine.synthesize("第一句", Path("s001.wav"), voice="v", rate=0)
    assert len(inner.calls) == 1


def test_a_file_name_we_cannot_read_is_never_injected() -> None:
    """★ 认不出句序 ⇒ 放行。宁可这一句不失败，也不要失败到别的句子头上。"""
    engine, inner = _engine(FaultPlan(tts_fail_sentence=1, tts_fail_times=9))

    engine.synthesize("母带", Path("voice_master.wav"), voice="v", rate=0)

    assert [name for _text, name in inner.calls] == ["voice_master.wav"]
