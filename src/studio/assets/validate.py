"""素材校验：每类素材的合格判据（T4.8 · §3.3.14 / §4.3.1 / §04.2.4）。

这一层回答的唯一问题是"这个素材能不能用"，而且**只回答，不阻断**
--------------------------------------------------------------
扫盘时遇到一个坏文件（0 字节、截断、扩展名骗人），正确的结果是"把它标红、把原因写清楚、
其余照常入库" —— 而不是让整批扫描停在第一个坏文件上。所以本模块**不抛异常**，
一律返回 :class:`AssetCheck`（``ok=False`` + ``problems`` 逐条给原因）。

问题与警告分开
--------------
``problems`` 是**拒绝入库**的（§3.3.14 / §4.3.1 明写"拒绝入库"的那几条）；
``warnings`` 是"能用，但你会想知道的"。两者混在一起，面板就没法只用一个红点
表达"这条到底进没进库"。

尚未机器化的判据（如实标注）
----------------------------
§4.3.1 的"疑似有背景音乐""单发言人一致性""有效语音占比 ≥ 70%"需要谱平坦度 / 说话人
嵌入一类的分析，一期**没做**（引擎侧也没有现成输出）。它们不是"默认通过"，而是
**没有被检查** —— 面板上不出现这一条，而不是画一个假的绿勾（P4）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.assets.layout import (
    AssetCandidate,
    AssetKind,
    voice_profile,
    voice_refs,
    voice_text,
)
from studio.core.errors import StudioError
from studio.core.media import MediaInfo, VolumeStats, analyze_volume, probe_media

__all__ = [
    "BGM_MIN_DURATION_MS",
    "BROLL_LIBRARY_MIN_CLIPS",
    "BROLL_LIBRARY_MIN_MS",
    "BROLL_MIN_USABLE_MS",
    "VOICE_MIN_SAMPLE_RATE",
    "VOICE_PEAK_CEILING_DB",
    "VOICE_SEGMENT_MAX_MS",
    "VOICE_SEGMENT_MIN_MS",
    "AssetCheck",
    "Problem",
    "check",
    "check_bgm",
    "check_broll",
    "check_voice",
]

#: BGM 最短时长（§3.3.14 表格逐字：**时长 ≥ 15s**）。
BGM_MIN_DURATION_MS: Final[int] = 15_000

#: 跑酷素材的 usable 区间下限（施工裁定 187）。
#: 依据是 §04.2.4 的两条硬约束：入点要避开首尾各 1.5s（``in_ms ~ U(from+1500, to-duration-1500)``），
#: 所以 usable 区间必须至少容纳"头 1.5s + 尾 1.5s + 一个 1.5s 的候选窗口"。
#: 低于这个数的片段**抽不出任何合法入点**，留着只会在渲染那一刻才报错。
BROLL_MIN_USABLE_MS: Final[int] = 4_500

#: 参考音**一段都没有**才是问题：段数不设上限，也不设"至少几段"（**裁定 379**）。
#:
#: 这里原本写着「2–3 段，超出**拒绝入库**」，而这两条都是**我们自己加的**：
#: - 上游只挡单段 >30s（``cosyvoice/cli/frontend.py`` 的
#:   ``assert speech.shape[1] / 16000 <= 30``），全文没有任何段数判据；
#: - 下限 2 的代价很具体：手边只有一句干净台词的人，为了过这道门会**把同一个文件
#:   复制一份**当第二段 —— 真机库里就躺着这样一条音色（两段 sha256 完全相同）。
#:   复制不产生任何新信息，只让人以为自己给了两段。
#: 多给几段是**真的有用**的（合成时会把它们拼成一段 prompt 一起喂给引擎），
#: 所以它是"越多越好"的建议，不是"少了就拒"的门。只给一段时下面给一条 warning。

#: 单段参考音时长（**裁定 369**：下限从 §4.3.1 原文的 10s 改成 2s）。
#:
#: 原文写的是 10–30 秒，但那个下限**在引擎里没有对应物**，是我们自己加的：
#: - 上游只有**上限**那一条 —— ``cosyvoice/cli/frontend.py`` 里
#:   ``assert speech.shape[1] / 16000 <= 30``（"超过 30 秒的提取不了 speech token"），
#:   全文没有任何"太短"的判据；
#: - 真机实测（2026-09-22 · RTX 2080 SUPER）：拿一条 **2.978 秒**的参考音跑
#:   ``POST /synth``（``cosyvoice2``）⇒ ``ok=true``、出 4.48 秒音频、RTF 1.137。
#:
#: 零样本克隆本来就是"给一小段原声"，不是"给一整段朗读"。把下限压在 10 秒上，
#: 代价是**手边只有一句台词的人永远入不了库**，而那句话的效果并不差。
#: 30 秒上限保留（那是引擎的硬约束）。
VOICE_SEGMENT_MIN_MS: Final[int] = 2_000
VOICE_SEGMENT_MAX_MS: Final[int] = 30_000

#: 参考音采样率下限（§4.3.1：≥ 16 kHz）。
VOICE_MIN_SAMPLE_RATE: Final[int] = 16_000

#: 参考音峰值上限（§4.3.1：峰值 ≤ −1.0 dBFS；削波的参考音会把这股破音一起复刻出来）。
VOICE_PEAK_CEILING_DB: Final[float] = -1.0


#: 素材库**整体**够不够用的判据（§T4.8 验收："统计：clips ≥ 60 且 ≥ 30min"）。
#: 与上面的单条判据分开：单条合格不等于"库够用" —— 60 条 5 秒的片段合起来只有
#: 5 分钟，出片时会翻来覆去用同几条，这正是"素材不够"的表现。
BROLL_LIBRARY_MIN_CLIPS: Final[int] = 60
BROLL_LIBRARY_MIN_MS: Final[int] = 30 * 60 * 1000


@dataclass(frozen=True, slots=True)
class Problem:
    """一条不合格 / 提醒。``code`` 是机器可读的（面板据此分类），``message`` 是给人看的。"""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class AssetCheck:
    """一个素材的体检结论。

    :param info: 主文件的探测结果（``None`` = 根本没探测成功，见 ``problems``）；
    :param segments: 音色的逐段结果（其余类别为空）；面板要按段显示"第 2 段短了"。
        只含**探测成功**的段（读不了的那段在 ``problems`` 里，不在这个元组里）；
    :param peaks: 与 ``segments`` **同序同长**的峰值（dBFS）；量不出来那一段是 ``None``。
        入库时要把"最响的那一段"记进 ``voice_profiles.peak_db`` —— 在体检里已经量过
        一遍了，让入库再量一遍等于把每个参考音解码两次。
    :param segment_indexes: ``segments`` / ``peaks`` 里每一项**是第几段**（1 起）。
        少了它，两者就只能按"第几个探测成功的"去对齐，而探不出来的段会被跳过 ——
        于是"第 3 段的时长"可能被当成第 2 段的（逐段管理面板会照着这个数让人删段）。
    """

    kind: AssetKind
    id: str
    problems: tuple[Problem, ...]
    warnings: tuple[Problem, ...]
    info: MediaInfo | None
    segments: tuple[MediaInfo, ...] = ()
    peaks: tuple[float | None, ...] = ()
    segment_indexes: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        """能不能入库（有 ``problems`` 就是不能）。"""
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "id": self.id,
            "ok": self.ok,
            "problems": [item.to_dict() for item in self.problems],
            "warnings": [item.to_dict() for item in self.warnings],
            "info": None if self.info is None else self.info.to_dict(),
            "segments": [item.to_dict() for item in self.segments],
            "peaks": list(self.peaks),
        }


def check_broll(
    candidate: AssetCandidate,
    *,
    license: str | None,
    usable_from_ms: int = 0,
    usable_to_ms: int | None = None,
    probe: Callable[..., MediaInfo] = probe_media,
) -> AssetCheck:
    """跑酷素材：可解码 + 有视频流 + usable 区间够长 + 授权已填。

    ``usable_from_ms`` / ``usable_to_ms`` 来自库里的那一行（默认 0 / 无上界 = 全片可用）：
    它们**不是**从文件推出来的，而是"这条素材从第几秒起没有黑帧 / 片头"的人工判断，
    所以体检要把它们收进来一起算，否则"标了 usable 区间却还是太短"这件事永远发现不了。
    """
    problems: list[Problem] = []
    warnings: list[Problem] = []
    info, failure = _probe(candidate.path, probe)
    if failure is not None:
        problems.append(failure)
    elif info is not None:
        if not info.has_video:
            problems.append(Problem("not_video", "这个文件里没有视频流（是纯音频？）"))
        usable_from = max(0, usable_from_ms)
        usable_to = info.duration_ms if usable_to_ms is None else min(usable_to_ms, info.duration_ms)
        usable = usable_to - usable_from
        if usable < BROLL_MIN_USABLE_MS:
            problems.append(
                Problem(
                    "usable_too_short",
                    f"可用区间只有 {usable} ms（下限 {BROLL_MIN_USABLE_MS} ms）："
                    "入点要避开首尾各 1.5s，再短就抽不出合法入点了",
                )
            )
        if usable_from >= usable_to:
            problems.append(
                Problem(
                    "usable_range_empty",
                    f"usable 区间为空（from={usable_from} to={usable_to}）",
                )
            )
    if not license:
        problems.append(
            Problem("license_missing", "授权类型没填（self_recorded / authorized / cc0 / purchased）")
        )
    return AssetCheck(
        kind=candidate.kind,
        id=candidate.id,
        problems=tuple(problems),
        warnings=tuple(warnings),
        info=info,
    )


def check_bgm(
    candidate: AssetCandidate,
    *,
    license: str | None,
    probe: Callable[..., MediaInfo] = probe_media,
) -> AssetCheck:
    """BGM：可解码 + 有音频流 + 时长 ≥ 15s + 授权已填（§3.3.14 表格）。"""
    problems: list[Problem] = []
    info, failure = _probe(candidate.path, probe)
    if failure is not None:
        problems.append(failure)
    elif info is not None:
        if not info.has_audio:
            problems.append(Problem("not_audio", "这个文件里没有音频流"))
        elif info.duration_ms < BGM_MIN_DURATION_MS:
            problems.append(
                Problem(
                    "too_short",
                    f"时长 {info.duration_ms} ms 低于下限 {BGM_MIN_DURATION_MS} ms（§3.3.14）",
                )
            )
    if not license:
        problems.append(
            Problem("license_missing", "授权类型没填（self_recorded / authorized / cc0 / purchased）")
        )
    return AssetCheck(
        kind=candidate.kind,
        id=candidate.id,
        problems=tuple(problems),
        warnings=(),
        info=info,
    )


def check_voice(
    candidate: AssetCandidate,
    *,
    probe: Callable[..., MediaInfo] = probe_media,
    volume: Callable[..., VolumeStats] = analyze_volume,
) -> AssetCheck:
    """零样本参考音：段数**不设上下限**、单段 2–30s（下限见裁定 369）、
    采样率 ≥ 16kHz、峰值 ≤ −1.0 dBFS（§4.3.1）。

    段数为什么不再是判据（**裁定 379**）：引擎一次只吃"一段 prompt"，而合成时会把
    多段**拼起来**用 ⇒ 段数越多，克隆听到的原声越多。它既不是引擎的限制，也不该
    是我们拦人的理由（详见 ``VOICE_*_SEGMENTS`` 删掉处那段注释）。

    旁车文件（``ref.txt`` / ``profile.json``）缺失或对不上记 **warning**：
    它们不阻止入库（引擎仍能跑），但会让复刻质量与合规留档打折 —— 这两件事
    用户会想知道，而"静默通过"会让他在出片质量变差时找不到原因。
    """
    problems: list[Problem] = []
    warnings: list[Problem] = []
    refs = voice_refs(candidate)
    segments: list[MediaInfo] = []
    peaks: list[float | None] = []
    indexes: list[int] = []

    if not refs:
        problems.append(Problem("no_refs", "目录里没有参考音（要 ref_01.wav 这种名字）"))

    for index, ref in enumerate(refs, start=1):
        info, failure = _probe(ref, probe)
        if failure is not None:
            problems.append(Problem(f"ref_{index:02d}_{failure.code}", f"第 {index} 段：{failure.message}"))
            continue
        if info is None:
            continue
        segments.append(info)
        indexes.append(index)
        # 峰值先量（不管有没有音频流）：``peaks`` 必须与 ``segments`` 同序同长，
        # 否则入库时"最响的那一段"会张冠李戴。
        peak, peak_problem = _peak(ref, volume)
        peaks.append(peak)
        if not info.has_audio:
            problems.append(Problem(f"ref_{index:02d}_not_audio", f"第 {index} 段里没有音频流"))
            continue
        if info.duration_ms < VOICE_SEGMENT_MIN_MS:
            problems.append(
                Problem(
                    f"ref_{index:02d}_too_short",
                    f"第 {index} 段只有 {info.duration_ms} ms（下限 {VOICE_SEGMENT_MIN_MS // 1000}s）",
                )
            )
        elif info.duration_ms > VOICE_SEGMENT_MAX_MS:
            problems.append(
                Problem(
                    f"ref_{index:02d}_too_long",
                    f"第 {index} 段有 {info.duration_ms} ms（上限 {VOICE_SEGMENT_MAX_MS // 1000}s）",
                )
            )
        if info.sample_rate is not None and info.sample_rate < VOICE_MIN_SAMPLE_RATE:
            problems.append(
                Problem(
                    f"ref_{index:02d}_sample_rate",
                    f"第 {index} 段采样率 {info.sample_rate} Hz（下限 16 kHz）",
                )
            )
        if peak_problem is not None:
            warnings.append(
                Problem(f"ref_{index:02d}_volume_unknown", f"第 {index} 段：{peak_problem.message}")
            )
        elif peak is not None and peak > VOICE_PEAK_CEILING_DB:
            problems.append(
                Problem(
                    f"ref_{index:02d}_clipped",
                    f"第 {index} 段峰值 {peak:.1f} dBFS 超过 {VOICE_PEAK_CEILING_DB} dBFS"
                    "（削波会被一起复刻）",
                )
            )

    warnings.extend(_voice_sidecar_warnings(candidate, ref_count=len(refs)))
    if len(refs) == 1:
        warnings.append(
            Problem(
                "single_ref",
                "只有 1 段参考音：能用，但多给几段音色会更稳 —— "
                "合成时会把各段拼成一段 prompt 一起喂给引擎，听到的原声越多越像",
            )
        )
    return AssetCheck(
        kind=candidate.kind,
        id=candidate.id,
        problems=tuple(problems),
        warnings=tuple(warnings),
        info=segments[0] if segments else None,
        segments=tuple(segments),
        peaks=tuple(peaks),
        segment_indexes=tuple(indexes),
    )


def check(
    candidate: AssetCandidate,
    *,
    license: str | None = None,
    usable_from_ms: int = 0,
    usable_to_ms: int | None = None,
    probe: Callable[..., MediaInfo] = probe_media,
    volume: Callable[..., VolumeStats] = analyze_volume,
) -> AssetCheck:
    """按类别分发（入库那条路只调这一个函数）。"""
    if candidate.kind is AssetKind.BROLL:
        return check_broll(
            candidate,
            license=license,
            usable_from_ms=usable_from_ms,
            usable_to_ms=usable_to_ms,
            probe=probe,
        )
    if candidate.kind is AssetKind.BGM:
        return check_bgm(candidate, license=license, probe=probe)
    return check_voice(candidate, probe=probe, volume=volume)


def _probe(path: Path, probe: Callable[..., MediaInfo]) -> tuple[MediaInfo | None, Problem | None]:
    """探测一次并把失败翻成一条 problem（**不抛** —— 见模块 docstring）。"""
    try:
        return probe(path), None
    except StudioError as exc:
        return None, Problem(exc.code.lower(), exc.message)


def _peak(path: Path, volume: Callable[..., VolumeStats]) -> tuple[float | None, Problem | None]:
    """峰值（dBFS）。分析失败 ⇒ ``(None, problem)``：**不因为量不了就说它削波了**。"""
    try:
        stats = volume(path)
    except StudioError as exc:
        return None, Problem(exc.code.lower(), exc.message)
    return stats.max_db, None


def _voice_sidecar_warnings(candidate: AssetCandidate, *, ref_count: int) -> list[Problem]:
    """旁车文件的两条提醒（``ref.txt`` 逐行对应参考音、``profile.json`` 合规留档）。"""
    warnings: list[Problem] = []
    text = voice_text(candidate)
    if text is None:
        warnings.append(Problem("ref_text_missing", "没有 ref.txt（参考文本），复刻质量会打折"))
    else:
        body = text.read_text(encoding="utf-8", errors="replace")
        lines = [line for line in body.splitlines() if line.strip()]
        if len(lines) != ref_count:
            warnings.append(
                Problem(
                    "ref_text_mismatch",
                    f"ref.txt 有 {len(lines)} 行，参考音有 {ref_count} 段"
                    " —— 两者要一一对应（顺序即文件名顺序）",
                )
            )
    if voice_profile(candidate) is None:
        warnings.append(Problem("profile_missing", "没有 profile.json（来源登记），R2 合规留档会缺一份"))
    return warnings
