"""引擎选择的判据（``tts/engine_picker.py`` · 2026-09-21）。

这一份判据从配音池里下沉出来，因为**渲染面板**那条路（``tts/synth.py``）原先自己
直连 SAPI：面板的音色下拉来自常驻引擎，选 ``bigbear`` 却交给系统语音包 ⇒
``SelectVoice`` 抛 ⇒ 每句失败 3 次 ⇒ 成片没人声（真机 ``r0001`` 挂在 55/58）。
"""

from __future__ import annotations

from studio.tts.engine_picker import pick_speakable_voice


def test_a_speakable_voice_is_left_alone() -> None:
    assert pick_speakable_voice(
        "bigbear", speakable=("bigbear", "littlebear"), default="bigbear", engine="cosyvoice2"
    ) == ("bigbear", None)


def test_no_voice_given_takes_the_engines_default() -> None:
    assert pick_speakable_voice(None, speakable=("bigbear",), default="bigbear", engine="cosyvoice2") == (
        "bigbear",
        None,
    )


def test_an_unspoken_voice_falls_back_and_says_why() -> None:
    voice, note = pick_speakable_voice(
        "Microsoft Huihui Desktop",
        speakable=("bigbear", "littlebear"),
        default="bigbear",
        engine="cosyvoice2",
    )

    assert voice == "bigbear"
    assert note is not None
    assert "Microsoft Huihui Desktop" in note
    assert "cosyvoice2" in note


def test_an_engine_that_lists_no_voices_is_not_second_guessed() -> None:
    """``speakable`` 为空 = 这台引擎不肯自报家门 ⇒ **原样放行**，让它自己去试。"""
    assert pick_speakable_voice(
        "Microsoft Huihui Desktop", speakable=(), default="bigbear", engine="sapi"
    ) == ("Microsoft Huihui Desktop", None)
