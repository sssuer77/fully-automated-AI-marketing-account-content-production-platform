"""发布前二次校验（T5.1 · §06.4 · R14 不可逆防护）。

它回答的只有一个问题
--------------------
"这条现在**能不能**发？不能的话，是哪一条不行？"

为什么要"重跑"而不是直接读当初的结论
--------------------------------------
渲染那一步当然已经量过响度（``tasks.quality_json``），但两件事会让那份读数**过期**：

1. 成片在盘上被动过（重合成 / 手动换了一支 / 磁盘修复）；
2. 成片与当初那一条**不是同一个文件**（新片覆盖了旧名）。

而发布是不可逆的（R14）：发出去之后发现响度不对，只能删。所以响度一项**重量盘上那个文件**，
与 ``scripts/audio_qc.py`` 同一条口径（它就是"发布前重跑的那一次"的命令行等价物）。

三道门禁，两种后果
----------------------
``blocking=True`` 的门禁没过 ⇒ **拒绝发布**（转 ``manual_required``，不静默放行）；
``blocking=False`` 的门禁没过 ⇒ 只是 ``warn``，照发。

后者只有一个：**相似度**。§06.4 对它的定位是"建议复核"而不是"不准发"——
相似度高不等于抄袭（同一素材素材库里只有那几支跑酷），而拒发的代价是"一条能发的片子被卡住"。
真要卡死就把 ``precheck.block_on_similarity`` 打开 —— 那是配置，不是代码里的一个 if。

``av_sync`` 为什么连门禁都不算
----------------------------
C12 已经取消了它的硬门禁（口述 D2"不需要音画同步"），所以它**只进 diagnostics**，
进不了 ``gates``。把它写成一条"总是通过的门禁"看起来很安全，实际上只会让人以为
"这个数不重要"—— 而它在排查画面卡顿时是有用的。

为什么禁区词不再调 LLM
------------------------
§06.4 明写"复用规则通道，不额外调 LLM"。封面文案只有几个字，而"这几个字里有没有禁区词"
是一个**字符串包含**问题 —— 让模型来判，既会判错，又会把一个确定性的事变成概率的。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from studio.core.clock import now_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.domain.models import QualityReport
from studio.domain.script import find_forbidden

__all__ = [
    "BANNED_WORDS_RELATIVE",
    "GATE_FORBIDDEN",
    "GATE_LOUDNESS",
    "GATE_SIMILARITY",
    "GATE_WATERMARK",
    "GateResult",
    "PrecheckReport",
    "PublishContent",
    "load_banned_words",
    "run_precheck",
]

logger = get_logger("studio.publish.precheck")

#: 禁区词表（§02-project-layout 给的位置：``prompts/shared/banned_words.yaml``）。
BANNED_WORDS_RELATIVE: Final[str] = "shared/banned_words.yaml"

GATE_WATERMARK: Final[str] = "watermark"
GATE_LOUDNESS: Final[str] = "loudness"
GATE_SIMILARITY: Final[str] = "similarity"
GATE_FORBIDDEN: Final[str] = "forbidden"

#: 每道门禁不通过时写进 ``publications.error_code`` 的码（§06.10）。
_GATE_ERROR_CODES: Final[dict[str, str]] = {
    GATE_WATERMARK: "PRECHECK_WATERMARK",
    GATE_LOUDNESS: "PRECHECK_LOUDNESS",
    GATE_SIMILARITY: "PRECHECK_SIMILARITY",
    GATE_FORBIDDEN: "PRECHECK_BANNED",
}


class PublishContent(BaseModel):
    """一次发布要交出的东西（校验的输入，也是 ``publications`` 行的内容快照）。

    把它单独建模是为了让校验可单测：门禁该只看“要发什么”与“成片怎么样”，
    而不该顺手去读库里的任务状态。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    title: str
    video_path: Path
    caption: str = ""
    tags: list[str] = Field(default_factory=list)
    cover_path: Path | None = None


class GateResult(BaseModel):
    """一道门禁的结论（面板直接画它）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    #: 没过时是不是"不准发"。``False`` = 只是建议复核。
    blocking: bool
    detail: str
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def error_code(self) -> str | None:
        return None if self.passed else _GATE_ERROR_CODES.get(self.name)


class PrecheckReport(BaseModel):
    """一次发布前二次校验的全部结论。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    passed: bool
    gates: list[GateResult]
    blocking_failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: 不阻断发布的诊断读数（``av_sync_offset_ms`` 在这里，不在 ``gates`` 里）。
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    #: 命中禁区而被**剔掉**的话题（title/caption 命中是拒发，tags 命中只是剔掉）。
    dropped_tags: list[str] = Field(default_factory=list)
    checked_at: str

    @property
    def error_code(self) -> str | None:
        """第一道没过的阻断门禁对应的码（写进 ``publications.error_code``）。"""
        for gate in self.gates:
            if not gate.passed and gate.blocking:
                return gate.error_code
        return None


def load_banned_words(paths: StudioPaths) -> tuple[str, ...]:
    """读禁区词表（``prompts/shared/banned_words.yaml``）。

    文件缺失 ⇒ **空表 + 一条日志**，不报错：这份表是**数据**（人编辑），
    而 ``persona.forbidden`` 已经是一道必然生效的线（E7 唯一人工必填）。
    把"表没建"升级成"不能发布"，会让一个缺文件变成一个停产故障。
    """
    source = paths.prompts_dir / BANNED_WORDS_RELATIVE
    if not source.is_file():
        logger.info("precheck.banned_words_missing", path=str(source))
        return ()
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StudioError(
            f"禁区词表读不了：{source}",
            code=ErrorCode.CONFIG_INVALID,
            context={"path": str(source), "error": str(exc)},
            remediation="检查 YAML 格式：顶层应为 {words: […]} 或一个列表",
        ) from exc
    raw = payload.get("words") if isinstance(payload, dict) else payload
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise StudioError(
            f"禁区词表形状不对：{source}",
            code=ErrorCode.CONFIG_INVALID,
            context={"path": str(source), "type": type(raw).__name__},
            remediation="写成 {words: […]} 或一个列表",
        )
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _watermark_gate(
    *,
    config_required: bool,
    quality: QualityReport,
    manifest_applied: bool | None,
) -> GateResult:
    """门禁 1：水印已叠加（D5 必做）。

    两个来源的先后顺序是刻意的：``quality_json`` 是契约写明的那个字段，
    ``manifest.json`` 是**同一次渲染自己写的录录**（不会被后续回填覆盖）。
    两边都没有 ⇒ 判**不过**。这不是严格，而是诚实："没记录"与"记录了没贴"在发布这件事上
    必须同样对待 —— 发出去就收不回来了。
    """
    if not config_required:
        return GateResult(
            name=GATE_WATERMARK,
            passed=True,
            blocking=False,
            detail="配置里关了水印门禁（precheck.require_watermark=false）",
        )
    applied = quality.watermark_applied
    source = "quality_json"
    if applied is None:
        applied = manifest_applied
        source = "manifest.json"
    if applied is None:
        return GateResult(
            name=GATE_WATERMARK,
            passed=False,
            blocking=True,
            detail="没有任何水印判据（quality_json 与 manifest.json 都没记）—— 无法证明它贴了，按没贴处理",
            context={"quality_json": None, "manifest": None},
        )
    return GateResult(
        name=GATE_WATERMARK,
        passed=bool(applied),
        blocking=True,
        detail=(
            f"成片已叠加水印（来源：{source}）"
            if applied
            else f"成片**没有**水印（来源：{source}）—— D5 硬缺陷"
        ),
        context={"applied": bool(applied), "source": source},
    )


def _loudness_gate(
    *,
    measured: tuple[float, float] | None,
    lufs_min: float,
    lufs_max: float,
    true_peak_max: float,
    recorded: tuple[float | None, float | None],
) -> GateResult:
    """门禁 2：响度（量盘上那个文件）。

    量不出来 ⇒ **不过**。与 ``scripts/audio_qc.py`` 退码 2 同一条理由：
    一份量不出响度的 QC 如果返回"通过"，等于给"根本没检查"发了一张合格证。
    """
    if measured is None:
        return GateResult(
            name=GATE_LOUDNESS,
            passed=False,
            blocking=True,
            detail="成片量不出响度（ffmpeg 没给出读数）—— 不能把“没检查”当“检查过”",
        )
    lufs, true_peak = measured
    loudness_ok = lufs_min <= lufs <= lufs_max
    peak_ok = true_peak <= true_peak_max
    detail = (
        f"成片实测 {lufs:.2f} LUFS / {true_peak:.2f} dBTP，"
        f"门禁 [{lufs_min}, {lufs_max}] LUFS 且 ≤ {true_peak_max} dBTP"
    )
    recorded_lufs, recorded_peak = recorded
    context: dict[str, Any] = {
        "lufs": round(lufs, 2),
        "true_peak": round(true_peak, 2),
        "lufs_range": [lufs_min, lufs_max],
        "true_peak_max": true_peak_max,
    }
    if recorded_lufs is not None:
        context["recorded_lufs"] = round(recorded_lufs, 2)
    if recorded_peak is not None:
        context["recorded_true_peak"] = round(recorded_peak, 2)
    return GateResult(
        name=GATE_LOUDNESS,
        passed=loudness_ok and peak_ok,
        blocking=True,
        detail=detail,
        context=context,
    )


def _similarity_gate(
    *,
    dup_audit_pass: bool | None,
    block_on_similarity: bool,
) -> GateResult:
    """门禁 3：相似度（防搬运）。默认**不阻断**（P4）。"""
    if dup_audit_pass is None:
        return GateResult(
            name=GATE_SIMILARITY,
            # ``None`` = **没审**，不是"审了没过"。没审不该被当成一次失败，
            # 所以 ``passed`` 为真；要不要因此拦住，由 ``blocking`` 说了算。
            passed=True,
            blocking=block_on_similarity,
            detail="没跑过相似度审计（quality_json 里没有 dup_audit_pass）",
        )
    return GateResult(
        name=GATE_SIMILARITY,
        # ★ ``passed`` 报的是**审计结论本身**，``blocking`` 才报"要不要拦"。
        # 两者不能揉在一起：写成 ``dup_audit_pass or not block_on_similarity``
        # 会让"审了没过"变成 ``passed=True`` ⇒ ``run_precheck`` 那条
        # "没过就 warn" 的循环**一句提示都不出** —— 一条相似度偏高的片子
        # 会安安静静地过审，而 detail 里还写着"建议复核"。
        passed=bool(dup_audit_pass),
        blocking=block_on_similarity,
        detail=("相似度审计通过" if dup_audit_pass else "相似度偏高，建议复核（不阻断发布）"),
        context={"dup_audit_pass": dup_audit_pass},
    )


def _forbidden_gate(
    *,
    content: PublishContent,
    terms: Sequence[str],
) -> tuple[GateResult, list[str]]:
    """禁区扫描：title / caption 命中 ⇒ **拒发**；tags 命中 ⇒ 剔掉 + warn。

    为什么 tags 不同对待：一个话题标签命中禁区词，把它剔掉就完了（标签不是作品本身）；
    而标题/正文里命中，剔掉就把话说残了 —— 那是人该看一眼的事。
    """
    title_hits = find_forbidden(content.title, terms)
    caption_hits = find_forbidden(content.caption, terms)
    dropped = [tag for tag in content.tags if find_forbidden(tag, terms)]
    hits = [*title_hits, *caption_hits]
    passed = not hits
    detail = "标题与发布文案未命中禁区词" if passed else "命中禁区词：" + "、".join(dict.fromkeys(hits))
    return (
        GateResult(
            name=GATE_FORBIDDEN,
            passed=passed,
            blocking=True,
            detail=detail,
            context={
                "title_hits": title_hits,
                "caption_hits": caption_hits,
                "dropped_tags": dropped,
                "term_count": len(terms),
            },
        ),
        dropped,
    )


def run_precheck(
    content: PublishContent,
    *,
    config: Any,
    quality: QualityReport,
    forbidden_terms: Sequence[str],
    measure: Callable[[Path], tuple[float, float] | None] | None = None,
    manifest_applied: bool | None = None,
    av_sync_offset_ms: int | None = None,
) -> PrecheckReport:
    """跑完三道门禁 + 禁区扫描，返回一份完整结论。

    ``measure`` 是 ``(Path) -> (lufs, true_peak) | None``（注入以便单测不真跑 ffmpeg；
    真实路径由 :func:`studio.publish.precheck.default_measure` 提供）。

    ``av_sync_offset_ms`` 只进 ``diagnostics``（C12）。
    """
    reader = measure or default_measure
    measured = reader(content.video_path)
    gates: list[GateResult] = [
        _watermark_gate(
            config_required=bool(getattr(config, "require_watermark", True)),
            quality=quality,
            manifest_applied=manifest_applied,
        ),
        _loudness_gate(
            measured=measured,
            lufs_min=float(getattr(config, "lufs_min", -16.5)),
            lufs_max=float(getattr(config, "lufs_max", -15.5)),
            true_peak_max=float(getattr(config, "true_peak_max_dbtp", -1.0)),
            recorded=(quality.lufs, quality.true_peak),
        ),
        _similarity_gate(
            dup_audit_pass=quality.dup_audit_pass,
            block_on_similarity=bool(getattr(config, "block_on_similarity", False)),
        ),
    ]
    warnings: list[str] = []
    dropped_tags: list[str] = []
    if bool(getattr(config, "scan_forbidden_words", True)):
        gate, dropped_tags = _forbidden_gate(content=content, terms=forbidden_terms)
        gates.append(gate)
    else:
        warnings.append("配置里关了禁区词扫描（precheck.scan_forbidden_words=false）")

    for gate in gates:
        if not gate.passed and not gate.blocking:
            warnings.append(f"{gate.name}：{gate.detail}")
    if dropped_tags:
        warnings.append("已剔掉命中禁区词的话题：" + "、".join(dropped_tags))

    diagnostics: dict[str, Any] = {}
    if av_sync_offset_ms is not None:
        diagnostics["av_sync_offset_ms"] = av_sync_offset_ms
        diagnostics["av_sync_note"] = "仅诊断（C12：不阻断发布）"
    if quality.degraded:
        diagnostics["degraded"] = True
        diagnostics["degrade_reason"] = quality.degrade_reason
        warnings.append(f"成片是降级出片：{quality.degrade_reason or '原因未记录'}")

    blocking = [gate.name for gate in gates if not gate.passed and gate.blocking]
    return PrecheckReport(
        task_id=content.task_id,
        passed=not blocking,
        gates=gates,
        blocking_failures=blocking,
        warnings=warnings,
        diagnostics=diagnostics,
        dropped_tags=dropped_tags,
        checked_at=now_iso(),
    )


def default_measure(path: Path) -> tuple[float, float] | None:
    """量盘上那个成片的响度（``(lufs, true_peak)``；量不出来 ⇒ ``None``）。

    读的是 ``config/outputs.yaml`` 的音频参数 —— 与渲染那一步**同一份设置**，
    否则"重量一次"量出来的数与当初那个可能不同源。
    """
    from studio.core.config import load_config  # noqa: PLC0415 —— 避免导入期就读配置

    loaded = load_config()
    from studio.render.mixdown import MixSettings, measure_file  # noqa: PLC0415 —— §02.1 明列的
    # publish→render 唯一例外：门禁与 quality_json 的口径必须同源

    settings = MixSettings.from_config(loaded.bundle.outputs.audio)
    reading = measure_file(path, settings=settings)
    if reading is None:
        return None
    return reading.input_i, reading.input_tp
