"""音频总线：侧链避让 → 混音 → 两遍 loudnorm → 限幅（T3.6 · §04.2.8.3）。

两种用例，各管一件事
--------------------
① **纯函数**（不跑 ffmpeg）：断言滤镜图里那几个"写错也不报错"的参数 ——
   ``normalize=0``、两遍 loudnorm、``alimiter`` 的自动电平关掉、人声 asplit。
   这些错了的后果是"人声偏小""响度超标"，而 ffmpeg 一个字都不会说。
② **真跑 ffmpeg**（标 ``slow``）：拿两个正弦波混一遍，量最终响度与真峰值，
   断言落在发布门禁 ``[-16.5, -15.5] LUFS`` / ``≤ -1.0 dBTP`` 里。

为什么必须有第 ② 类
--------------------
第 ① 类只能证明"图里写了这些东西"，证不了"这么写真的能出对的结果"。
本次实现里 ``alimiter`` 的自动电平与 ``true_peak`` 的关系就是靠第 ② 类量出来的：
按规格那个裸的 ``alimiter=limit=0.95`` 渲出来是 −0.85 dBTP，超门禁。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from studio.core.media import run_command
from studio.render.mixdown import (
    CODEC_PEAK_MARGIN_DB,
    LIMITER_AUTO_LEVEL,
    LoudnessMeasurement,
    MixSettings,
    build_audio_chain,
    build_measure_argv,
    build_measure_file_argv,
    limiter_limit,
    measure_file,
    measure_mix,
    parse_loudnorm_json,
)

#: 发布门禁（§01.8.2 / `config/publish.yaml → true_peak_max_dbtp`）
LUFS_MIN, LUFS_MAX = -16.5, -15.5
TP_MAX = -1.0


def _measured() -> LoudnessMeasurement:
    return LoudnessMeasurement(
        input_i=-23.0, input_tp=-5.0, input_lra=2.9, input_thresh=-33.0, target_offset=0.0
    )


def _ffmpeg_or_skip() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("本机没有 ffmpeg")
    return binary


# ══════════════════════════════════════════════════════════════════════
# 滤镜图（纯函数）
# ══════════════════════════════════════════════════════════════════════


def test_amix_never_normalises() -> None:
    """★ ``normalize=0``：默认的 ``normalize=1`` 会把两路输入各衰减 6dB。"""
    graph = ";".join(
        build_audio_chain(voice_index=1, bgm_index=2, settings=MixSettings(), measured=_measured())
    )
    assert "amix=inputs=2:duration=first:normalize=0" in graph


def test_voice_is_split_before_the_sidechain() -> None:
    """★ 人声必须 asplit：标签被消费两次时 ffmpeg 报"匹配不到流"。"""
    graph = ";".join(
        build_audio_chain(voice_index=1, bgm_index=2, settings=MixSettings(), measured=_measured())
    )
    assert "asplit=2[a_voice_sc][a_voice_mix]" in graph
    # 人声**不再**直接喂给侧链与混音 —— 那两处必须走 asplit 出来的两支
    assert "[a_bgm][a_voice]sidechaincompress" not in graph
    assert "[a_voice][a_bgm_duck]amix" not in graph


def test_sidechain_compresses_the_bgm_with_the_voice_as_the_trigger() -> None:
    """★ 侧链顺序是 ``[主路][侧链]``：**BGM 在前**（被压的那路），人声在后（触发）。

    ``ffmpeg -h filter=sidechaincompress`` 写明 ``#0: main`` / ``#1: sidechain``。
    接反了 ffmpeg 一个字都不说，但 ``a_bgm_duck`` 会变成"被压过的人声"：BGM 整条
    丢失、人声被叠一份，峰值顶穿 0dBFS（实测 +1.91 dBTP），后面 loudnorm 的真峰值
    约束就被卡死。**规格 §04.2.8.3 的模板就是接反的那一版**，这条用例是照着
    ffmpeg 的实际行为写的，不是照着规格写的。
    """
    graph = ";".join(
        build_audio_chain(voice_index=1, bgm_index=2, settings=MixSettings(), measured=_measured())
    )
    assert "[a_bgm][a_voice_sc]sidechaincompress=" in graph
    assert "[a_voice_sc][a_bgm]sidechaincompress=" not in graph


def test_loudnorm_carries_the_measured_readings() -> None:
    """★ 第二遍 loudnorm 必须带 ``measured_*`` 且 ``linear=true``（否则是动态压缩）。"""
    graph = ";".join(
        build_audio_chain(voice_index=1, bgm_index=2, settings=MixSettings(), measured=_measured())
    )
    assert "linear=true" in graph
    assert "measured_I=-23" in graph
    assert "measured_thresh=-33" in graph
    # 两遍法：测量那一遍走 print_format=json，正式那一遍不带它
    assert "print_format=json" not in graph


def test_measure_pass_asks_for_json() -> None:
    argv = build_measure_argv(voice=Path("v.wav"), bgm=None, settings=MixSettings(), duration_ms=1000)
    graph = argv[argv.index("-filter_complex") + 1]
    assert "print_format=json" in graph
    # 只量响度：不解视频、不编码
    assert argv[-2:] == ["-f", "null"] or argv[-3:] == ["-f", "null", "-"]


def test_output_qc_measures_the_file_itself() -> None:
    """★ QC 量的是**落盘的成片**：输入是 ``-i <file>``，不是滤镜链里的中间标签。

    这一条与 `measure_mix` 的区别是整条 QC 的地基：``measure_mix`` 报的 ``input_i``
    是 loudnorm **归一化之前**的输入读数，拿它填 ``quality_json.lufs`` 会让一条听起来
    正常的片子显示成 −22 LUFS，发布门禁再按这个数把好片子拦下来。
    """
    argv = build_measure_file_argv(Path("final.mp4"), settings=MixSettings())
    assert argv[argv.index("-i") + 1] == "final.mp4"
    assert "print_format=json" in argv[argv.index("-af") + 1]
    # 纯解码：不编码、不落盘
    assert argv[-3:] == ["-f", "null", "-"]


def test_limiter_auto_level_is_off_and_limit_follows_the_gate() -> None:
    """★ 限幅器的自动电平必须关（它会抵消 loudnorm），阈值从 ``true_peak_dbtp`` 推。"""
    graph = ";".join(
        build_audio_chain(voice_index=1, bgm_index=None, settings=MixSettings(), measured=_measured())
    )
    assert f"level={'1' if LIMITER_AUTO_LEVEL else '0'}" in graph
    assert LIMITER_AUTO_LEVEL is False
    # 0.95（规格里那个写死的值）比门禁宽了半 dB，不该再出现
    assert "limit=0.95" not in graph
    assert limiter_limit(MixSettings()) < 0.95


def test_the_peak_margin_sits_on_the_limiter_not_on_loudnorm() -> None:
    """★ 0.3 dB 的编码过冲余量加在 ``alimiter`` 上 —— **只有它真的压峰值**。

    `loudnorm` 的 `TP` 只是第一遍算 `offset` 时的参考值：第二遍 `linear=true` 施加的
    是"把响度归到目标"的那一个静态增益。实测 `measured_I=-17.35` 配 `offset=0.86` 时
    它老老实实 +0.86dB，把峰值推到 +2.78dBFS，**一点都没拦**。所以余量挪到 `loudnorm`
    上等于没人管峰值，解码出来就是 −0.85 dBTP 那种越界值。
    """
    settings = MixSettings()
    assert limiter_limit(settings) == pytest.approx(
        10 ** ((settings.true_peak_dbtp - CODEC_PEAK_MARGIN_DB) / 20)
    )
    # 限幅器必须比门禁**更紧**，否则解码过冲那零点几 dB 就顶出去了
    assert limiter_limit(settings) < 10 ** (settings.true_peak_dbtp / 20)
    # 两遍 loudnorm（测量 + 混音）用同一个 TP，且就是门禁值
    graphs = [
        ";".join(build_audio_chain(voice_index=1, bgm_index=2, settings=settings, measured=_measured())),
        ";".join(
            build_measure_argv(voice=Path("v.wav"), bgm=Path("b.mp3"), settings=settings, duration_ms=1000)
        ),
    ]
    for graph in graphs:
        assert f"TP={settings.true_peak_dbtp:g}" in graph
        assert f"limit={limiter_limit(settings):.4f}" in graph


def test_missing_bgm_degrades_to_a_single_voice_track() -> None:
    """★ BGM 缺失 ⇒ 单轨人声，**不报错**（可选输入不该阻塞出片）。"""
    parts = build_audio_chain(voice_index=1, bgm_index=None, settings=MixSettings(), measured=_measured())
    graph = ";".join(parts)
    assert "amix" not in graph
    assert "sidechaincompress" not in graph
    assert "[aout]" in graph


def test_measurement_parsing() -> None:
    payload = (
        '{"input_i" : "-18.4", "input_tp" : "-2.1", "input_lra" : "6.2", '
        '"input_thresh" : "-28.7", "target_offset" : "0.3"}'
    )
    parsed = parse_loudnorm_json(f"noise {payload} more noise")
    assert parsed is not None
    assert (parsed.input_i, parsed.input_thresh) == (-18.4, -28.7)
    # 缺字段 ⇒ None（宁可退回一遍法，也不拿半份读数去喂线性模式）
    assert parse_loudnorm_json('{"input_i" : "-18.4"}') is None
    assert parse_loudnorm_json("nothing here") is None


# ══════════════════════════════════════════════════════════════════════
# 真跑一遍
# ══════════════════════════════════════════════════════════════════════


def _sine(target: Path, *, freq: int, seconds: float, gain_db: float, channels: int) -> None:
    """造一段正弦波当素材。编码器按**扩展名**挑：mp3 收不了 pcm，混错会直接报错。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    codec = (
        ["-c:a", "pcm_s16le"] if target.suffix.lower() == ".wav" else ["-c:a", "libmp3lame", "-b:a", "192k"]
    )
    result = run_command(
        [
            _ffmpeg_or_skip(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
            "-af",
            f"volume={gain_db}dB",
            "-ac",
            str(channels),
            *codec,
            str(target),
        ]
    )
    assert result.ok, result.tail()


def _render_mix(voice: Path, bgm: Path | None, out: Path, *, seconds: float) -> None:
    settings = MixSettings()
    measured = measure_mix(voice=voice, bgm=bgm, settings=settings, duration_ms=int(seconds * 1000))
    chain = build_audio_chain(
        voice_index=0, bgm_index=1 if bgm else None, settings=settings, measured=measured
    )
    argv = [_ffmpeg_or_skip(), "-hide_banner", "-nostats", "-y", "-i", str(voice)]
    if bgm is not None:
        argv += ["-stream_loop", "-1", "-i", str(bgm)]
    argv += [
        "-filter_complex",
        ";".join(chain),
        "-map",
        "[aout]",
        "-t",
        f"{seconds:.3f}",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(out),
    ]
    result = run_command(argv)
    assert result.ok, result.tail()


def _gate(path: Path) -> tuple[float | None, float | None]:
    """量成片的响度与真峰值 —— 走的就是 QC 那条路径。

    这里**故意不再写一份 ffmpeg argv**：测试自己拼一条"差不多的"测量命令，
    测出来的是那条命令对不对，不是产品那条路对不对。
    """
    _ffmpeg_or_skip()
    measured = measure_file(path, settings=MixSettings())
    return (measured.input_i, measured.input_tp) if measured else (None, None)


@pytest.mark.slow
def test_mixed_output_hits_the_publish_gate(tmp_path: Path) -> None:
    """★ 混完的响度与真峰值都要落在发布门禁里（§01.8.2）。"""
    voice = tmp_path / "voice.wav"
    bgm = tmp_path / "bgm.mp3"
    _sine(voice, freq=440, seconds=6.0, gain_db=-24.0, channels=1)
    _sine(bgm, freq=180, seconds=4.0, gain_db=-8.0, channels=2)

    out = tmp_path / "mix.m4a"
    _render_mix(voice, bgm, out, seconds=6.6)

    lufs, peak = _gate(out)
    assert lufs is not None
    assert LUFS_MIN <= lufs <= LUFS_MAX, lufs
    assert peak is not None and peak <= TP_MAX, peak


@pytest.mark.slow
def test_voice_only_output_hits_the_gate_too(tmp_path: Path) -> None:
    """★ BGM 缺失那条分支也要过门禁 —— "没有 BGM"不该让响度跑偏。"""
    voice = tmp_path / "voice.wav"
    _sine(voice, freq=440, seconds=6.0, gain_db=-24.0, channels=1)

    out = tmp_path / "voice_only.m4a"
    _render_mix(voice, None, out, seconds=6.6)

    lufs, peak = _gate(out)
    assert lufs is not None
    assert LUFS_MIN <= lufs <= LUFS_MAX, lufs
    assert peak is not None and peak <= TP_MAX, peak
