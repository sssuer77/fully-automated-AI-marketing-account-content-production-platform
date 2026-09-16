"""发布前二次校验（T5.1 · §06.4 · R14）。

三道门禁 + 禁区扫描，逐条钉住**通过 / 阻断**两种结局：

| 门禁 | 判据 | 不过 |
| --- | --- | --- |
| 水印 | ``quality_json.watermark_applied == true`` | **拒发**（``PRECHECK_WATERMARK``） |
| 响度 | ``lufs ∈ [-16.5,-15.5]`` 且 ``true_peak ≤ -1.0`` | **拒发**（``PRECHECK_LOUDNESS``） |
| 相似度 | ``dup_audit_pass`` | 只 warn（``block_on_similarity=false``） |
| 禁区 | ``persona.forbidden`` + ``banned_words.yaml`` | title/caption **拒发**；tags 剔除 |

外加两条刻意的缺席：``av_sync`` **只进 diagnostics**（C12 取消了硬门禁），
以及"量不出响度"判**不过**（不能把"没检查"当"检查过"）。

这里一个 ffmpeg 都不跑（``measure`` 是注入的）；这里也不碰真库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from studio.core.config import PrecheckConfig
from studio.core.errors import ErrorCode, StudioError
from studio.domain.models import QualityReport
from studio.publish.precheck import (
    GATE_FORBIDDEN,
    GATE_LOUDNESS,
    GATE_SIMILARITY,
    GATE_WATERMARK,
    PublishContent,
    load_banned_words,
    run_precheck,
)

# ── 夹具 ──────────────────────────────────────────────────────────────


def _config(**overrides: Any) -> PrecheckConfig:
    return PrecheckConfig.model_validate(overrides)


def _content(**overrides: Any) -> PublishContent:
    data: dict[str, Any] = {
        "task_id": "01TASK",
        "title": "离谱跑酷地图",
        "video_path": Path("data/output/videos/x_final.mp4"),
        "caption": "今天看一张离谱的跑酷地图",
        "tags": ["跑酷", "我的世界"],
    }
    data.update(overrides)
    return PublishContent.model_validate(data)


def _quality(**overrides: Any) -> QualityReport:
    data: dict[str, Any] = {
        "lufs": -16.0,
        "true_peak": -1.2,
        "watermark_applied": True,
        "dup_audit_pass": True,
    }
    data.update(overrides)
    return QualityReport.model_validate(data)


def _measure_ok(_path: Path) -> tuple[float, float]:
    return (-16.0, -1.2)


def _run(
    *,
    content: PublishContent | None = None,
    quality: QualityReport | None = None,
    config: PrecheckConfig | None = None,
    terms: tuple[str, ...] = (),
    measure: Any = _measure_ok,
    manifest_applied: bool | None = None,
    av_sync_offset_ms: int | None = None,
) -> Any:
    return run_precheck(
        content or _content(),
        config=config or _config(),
        quality=quality or _quality(),
        forbidden_terms=terms,
        measure=measure,
        manifest_applied=manifest_applied,
        av_sync_offset_ms=av_sync_offset_ms,
    )


def _gate(report: Any, name: str) -> Any:
    return next(gate for gate in report.gates if gate.name == name)


# ── 总览 ──────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_all_gates_pass(self) -> None:
        report = _run()
        assert report.passed is True
        assert report.blocking_failures == []
        assert report.error_code is None

    def test_four_gates_are_reported(self) -> None:
        """三道门禁 + 禁区扫描，一条都不能少 —— 少一条就是"以为检查过了"。"""
        names = {gate.name for gate in _run().gates}
        assert names == {GATE_WATERMARK, GATE_LOUDNESS, GATE_SIMILARITY, GATE_FORBIDDEN}

    def test_checked_at_is_recorded(self) -> None:
        assert _run().checked_at


# ── 门禁 1：水印 ──────────────────────────────────────────────────────


class TestWatermarkGate:
    def test_applied_passes(self) -> None:
        assert _gate(_run(quality=_quality(watermark_applied=True)), GATE_WATERMARK).passed is True

    def test_missing_watermark_blocks(self) -> None:
        report = _run(quality=_quality(watermark_applied=False))
        gate = _gate(report, GATE_WATERMARK)
        assert gate.passed is False
        assert gate.blocking is True
        assert report.passed is False
        assert report.error_code == ErrorCode.PRECHECK_WATERMARK.value

    def test_falls_back_to_manifest(self) -> None:
        """``quality_json`` 没记 ⇒ 读 manifest（同一次渲染自己写的原始记录）。"""
        gate = _gate(_run(quality=_quality(watermark_applied=None), manifest_applied=True), GATE_WATERMARK)
        assert gate.passed is True
        assert gate.context["source"] == "manifest.json"

    def test_manifest_says_no_blocks(self) -> None:
        gate = _gate(_run(quality=_quality(watermark_applied=None), manifest_applied=False), GATE_WATERMARK)
        assert gate.passed is False

    def test_no_evidence_at_all_blocks(self) -> None:
        """ "没记录"与"记录了没贴"在发布这件事上必须同样对待 —— 发出去就收不回来。"""
        report = _run(quality=_quality(watermark_applied=None), manifest_applied=None)
        assert _gate(report, GATE_WATERMARK).passed is False
        assert report.error_code == ErrorCode.PRECHECK_WATERMARK.value

    def test_disabled_gate_is_not_blocking(self) -> None:
        report = _run(
            config=_config(require_watermark=False),
            quality=_quality(watermark_applied=False),
        )
        assert _gate(report, GATE_WATERMARK).passed is True
        assert _gate(report, GATE_WATERMARK).blocking is False
        assert report.passed is True


# ── 门禁 2：响度 ──────────────────────────────────────────────────────


class TestLoudnessGate:
    def test_inside_the_window_passes(self) -> None:
        assert _gate(_run(measure=lambda _p: (-16.0, -1.2)), GATE_LOUDNESS).passed is True

    @pytest.mark.parametrize("lufs", [-16.6, -15.4])
    def test_outside_the_window_blocks(self, lufs: float) -> None:
        report = _run(measure=lambda _p: (lufs, -1.2))
        assert _gate(report, GATE_LOUDNESS).passed is False
        assert report.error_code == ErrorCode.PRECHECK_LOUDNESS.value

    def test_true_peak_too_hot_blocks(self) -> None:
        report = _run(measure=lambda _p: (-16.0, -0.5))
        assert _gate(report, GATE_LOUDNESS).passed is False
        assert report.error_code == ErrorCode.PRECHECK_LOUDNESS.value

    def test_boundaries_are_inclusive(self) -> None:
        assert _gate(_run(measure=lambda _p: (-16.5, -1.0)), GATE_LOUDNESS).passed is True
        assert _gate(_run(measure=lambda _p: (-15.5, -1.0)), GATE_LOUDNESS).passed is True

    def test_unmeasurable_blocks(self) -> None:
        """量不出来 ⇒ **不过**。给"根本没检查"发合格证是最坏的一种通过。"""
        report = _run(measure=lambda _p: None)
        gate = _gate(report, GATE_LOUDNESS)
        assert gate.passed is False
        assert gate.blocking is True
        assert report.error_code == ErrorCode.PRECHECK_LOUDNESS.value

    def test_recorded_values_ride_along_for_comparison(self) -> None:
        """当初的读数一并带上 —— 排查时要能一眼看出"是重量之后变了"还是"本来就不对"。"""
        gate = _gate(_run(quality=_quality(lufs=-16.1, true_peak=-1.3)), GATE_LOUDNESS)
        assert gate.context["recorded_lufs"] == -16.1
        assert gate.context["lufs"] == -16.0

    def test_custom_window_from_config(self) -> None:
        config = _config(lufs_min=-20.0, lufs_max=-10.0)
        assert _gate(_run(config=config, measure=lambda _p: (-18.0, -1.2)), GATE_LOUDNESS).passed is True


# ── 门禁 3：相似度 ────────────────────────────────────────────────────


class TestSimilarityGate:
    def test_pass_is_reported(self) -> None:
        assert _gate(_run(quality=_quality(dup_audit_pass=True)), GATE_SIMILARITY).passed is True

    def test_failure_only_warns_by_default(self) -> None:
        """§06.4：相似度不阻断 —— 相似不等于抄袭，而拒发的代价是"能发的片子被卡住"。"""
        report = _run(quality=_quality(dup_audit_pass=False))
        gate = _gate(report, GATE_SIMILARITY)
        assert gate.passed is False
        assert gate.blocking is False
        assert report.passed is True
        assert any("similarity" in item for item in report.warnings)

    def test_audit_never_run_is_not_a_failure(self) -> None:
        """``None`` = 没审（一期未落地），不是"审了没过"。"""
        report = _run(quality=_quality(dup_audit_pass=None))
        assert _gate(report, GATE_SIMILARITY).passed is True
        assert report.passed is True

    def test_config_can_make_it_blocking(self) -> None:
        """真要卡死就把 ``precheck.block_on_similarity`` 打开 —— 那是配置，不是代码里的 if。"""
        report = _run(
            config=_config(block_on_similarity=True),
            quality=_quality(dup_audit_pass=False),
        )
        assert _gate(report, GATE_SIMILARITY).blocking is True
        assert report.passed is False
        assert report.error_code == ErrorCode.PRECHECK_SIMILARITY.value


# ── 禁区扫描 ──────────────────────────────────────────────────────────


class TestForbiddenGate:
    def test_clean_text_passes(self) -> None:
        assert _gate(_run(terms=("国家级",)), GATE_FORBIDDEN).passed is True

    def test_title_hit_blocks(self) -> None:
        report = _run(content=_content(title="国家级跑酷地图"), terms=("国家级",))
        assert _gate(report, GATE_FORBIDDEN).passed is False
        assert report.passed is False
        assert report.error_code == ErrorCode.PRECHECK_BANNED.value

    def test_caption_hit_blocks(self) -> None:
        report = _run(content=_content(caption="全网最低价"), terms=("最低价",))
        assert _gate(report, GATE_FORBIDDEN).passed is False

    def test_tag_hit_is_dropped_not_blocked(self) -> None:
        """标签不是作品本身，剔掉就完了；标题里命中剔掉就把话说残了。"""
        report = _run(content=_content(tags=["跑酷", "国家级"]), terms=("国家级",))
        assert report.passed is True
        assert report.dropped_tags == ["国家级"]
        assert any("剔除" in item or "剔掉" in item for item in report.warnings)

    def test_scan_can_be_disabled(self) -> None:
        report = _run(
            config=_config(scan_forbidden_words=False),
            content=_content(title="国家级"),
            terms=("国家级",),
        )
        assert all(gate.name != GATE_FORBIDDEN for gate in report.gates)
        assert report.passed is True
        assert any("scan_forbidden_words" in item for item in report.warnings)

    def test_hits_are_reported_in_context(self) -> None:
        report = _run(content=_content(title="国家级"), terms=("国家级",))
        gate = _gate(report, GATE_FORBIDDEN)
        assert gate.context["title_hits"] == ["国家级"]
        assert gate.context["caption_hits"] == []


# ── av_sync 的缺席 ────────────────────────────────────────────────────


class TestAvSyncIsDiagnosticOnly:
    def test_not_a_gate(self) -> None:
        """C12 取消了音画同步的硬门禁 ⇒ 它连门禁都不算（不写成"总是通过的门禁"）。"""
        report = _run(av_sync_offset_ms=420)
        assert all(gate.name != "av_sync" for gate in report.gates)
        assert report.passed is True

    def test_lands_in_diagnostics(self) -> None:
        report = _run(av_sync_offset_ms=420)
        assert report.diagnostics["av_sync_offset_ms"] == 420
        assert "C12" in report.diagnostics["av_sync_note"]

    def test_absent_when_not_measured(self) -> None:
        assert "av_sync_offset_ms" not in _run().diagnostics


# ── 降级出片的提示 ────────────────────────────────────────────────────


class TestDegradedWarns:
    def test_degraded_video_warns_but_does_not_block(self) -> None:
        report = _run(quality=_quality(degraded=True, degrade_reason="no_broll"))
        assert report.diagnostics["degraded"] is True
        assert report.diagnostics["degrade_reason"] == "no_broll"
        assert any("降级" in item for item in report.warnings)
        assert report.passed is True


# ── 多道门禁同时不过 ──────────────────────────────────────────────────


class TestMultipleFailures:
    def test_error_code_is_the_first_blocking_one(self) -> None:
        """``publications.error_code`` 只有一个格子 ⇒ 报**第一道**没过的（顺序固定）。"""
        report = _run(
            quality=_quality(watermark_applied=False),
            measure=lambda _p: (-20.0, -1.2),
        )
        assert report.blocking_failures == [GATE_WATERMARK, GATE_LOUDNESS]
        assert report.error_code == ErrorCode.PRECHECK_WATERMARK.value


# ── 禁区词表 ──────────────────────────────────────────────────────────


class TestLoadBannedWords:
    def test_reads_the_words_list(self, tmp_paths: Any) -> None:
        shared = tmp_paths.prompts_dir / "shared"
        shared.mkdir(parents=True)
        (shared / "banned_words.yaml").write_text(
            yaml.safe_dump({"words": ["国家级", "最高级"]}), encoding="utf-8"
        )
        assert load_banned_words(tmp_paths) == ("国家级", "最高级")

    def test_missing_file_is_an_empty_table_not_an_error(self, tmp_paths: Any) -> None:
        """表缺失 ⇒ 空表。把"表没建"升级成"不能发布"，会让一个缺文件变成一个停产故障。"""
        assert load_banned_words(tmp_paths) == ()

    def test_blank_entries_are_dropped(self, tmp_paths: Any) -> None:
        shared = tmp_paths.prompts_dir / "shared"
        shared.mkdir(parents=True)
        (shared / "banned_words.yaml").write_text(
            yaml.safe_dump({"words": ["国家级", "  ", ""]}), encoding="utf-8"
        )
        assert load_banned_words(tmp_paths) == ("国家级",)

    def test_broken_yaml_raises_with_remediation(self, tmp_paths: Any) -> None:
        shared = tmp_paths.prompts_dir / "shared"
        shared.mkdir(parents=True)
        (shared / "banned_words.yaml").write_text("words: [国家级", encoding="utf-8")
        with pytest.raises(StudioError) as excinfo:
            load_banned_words(tmp_paths)
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID
        assert excinfo.value.remediation

    def test_wrong_shape_raises(self, tmp_paths: Any) -> None:
        shared = tmp_paths.prompts_dir / "shared"
        shared.mkdir(parents=True)
        (shared / "banned_words.yaml").write_text(yaml.safe_dump({"words": "国家级"}), encoding="utf-8")
        with pytest.raises(StudioError) as excinfo:
            load_banned_words(tmp_paths)
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    def test_the_shipped_table_is_loadable(self, repo_paths: Any) -> None:
        """仓库里那份真表必须读得出来（它是发布链路的输入，坏了不该等到发布时才发现）。"""
        words = load_banned_words(repo_paths)
        assert len(words) > 0
        assert "国家级" in words
