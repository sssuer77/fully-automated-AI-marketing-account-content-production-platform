"""出片服务接上整片级缓存（T3.4 · §04.2.8.7）。

这一层测的是**接线**：第二次调用 ``produce_video`` 时 ffmpeg 到底有没有被调起来。
所以 ``deliver``（真正的合成）与 ``measure_file``（成片响度，也要起 ffmpeg）都换成记账的
替身 —— 断言的是"调了几次"，不是"文件长得对不对"（那是 ``test_render_pipeline`` 的活）。

底片**故意**不放进目录（``clip=None`` ⇒ 纯黑底）：没有"随机挑素材"这一环，两次运行的
输入才是逐字节相同的 —— 那正是一次缓存命中该有的样子。真机上不带 ``--seed`` 的重跑通常
挑到另一条底片、哈希不同、照常重渲（见 ``render/cache.py`` 的"命中面有多大"）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import MediaInfo
from studio.core.paths import StudioPaths
from studio.render.composite import PROGRESS_TOTAL, CompositeRequest, CompositeResult, bg_fill
from studio.render.degrade import Delivery
from studio.render.mixdown import LoudnessMeasurement
from studio.services import render_service
from studio.services.render_service import ProduceRequest, ProduceResult, produce_video, quality_report

REPO_ROOT = Path(__file__).resolve().parents[3]
TASK = "t1"
SPEECH = "第一句在这里。第二句在这里。"

#: 成片量到的响度（替身返回值；真值是 ffmpeg 量的，这里只要"读回来的是同一个数"）
MEASURED = LoudnessMeasurement(
    input_i=-16.48, input_tp=-1.12, input_lra=1.8, input_thresh=-26.5, target_offset=0.1
)


def _outputs(tmp_path: Path) -> tuple[OutputsConfig, Path]:
    """真实 ``config/outputs.yaml`` 的一份副本（与 ``test_pipeline_service`` 同一条约定）。"""
    target = tmp_path / "studio" / "config" / "outputs.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((REPO_ROOT / "config" / "outputs.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return load_outputs_config(target), target


@dataclass
class Rig:
    """一次用例的全部家当：路径 + 配置 + **记账**。"""

    paths: StudioPaths
    outputs: OutputsConfig
    source: Path
    #: 每一次"真的合成"（``deliver``）与"真的量响度"（``measure_file``）
    delivered: list[CompositeRequest] = field(default_factory=list)
    measured: list[Path] = field(default_factory=list)
    progress: list[tuple[str, int, int, str]] = field(default_factory=list)

    @property
    def ffmpeg_calls(self) -> int:
        """这次出片一共起了几次 ffmpeg（合成 + 响度测量）。"""
        return len(self.delivered) + len(self.measured)

    def run(self, **overrides: Any) -> ProduceResult:
        request = ProduceRequest(task_id=TASK, text=SPEECH, **overrides)
        return produce_video(
            request,
            paths=self.paths,
            outputs=self.outputs,
            outputs_source=self.source,
            on_progress=lambda *row: self.progress.append(row),
        )


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    outputs, source = _outputs(tmp_path)
    rig = Rig(paths=paths, outputs=outputs, source=source)

    def fake_deliver(
        request: CompositeRequest,
        *,
        fallback_profile: object = None,
        fallback_output: Path | None = None,
        on_progress: object = None,
    ) -> Delivery:
        rig.delivered.append(request)
        output = request.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"mp4" * 1024)
        return Delivery(
            composite=CompositeResult(
                output=output,
                duration_ms=request.duration_ms,
                size_bytes=output.stat().st_size,
                watermark_applied=request.watermark is not None,
                bgm_applied=request.bgm is not None,
                subtitle_applied=request.subtitle is not None,
                bg_fill=bg_fill(request),
                loudness=None,
                warnings=(),
                argv=("ffmpeg", "-i", str(request.voice)),
            ),
            profile=request.profile,
            degraded=False,
            degrade_reason=None,
            attempts=(request.profile.name,),
            warnings=(),
        )

    def fake_measure(path: Path, *, settings: object = None) -> LoudnessMeasurement:
        rig.measured.append(path)
        return MEASURED

    monkeypatch.setattr(render_service, "deliver", fake_deliver)
    monkeypatch.setattr(render_service, "measure_file", fake_measure)
    # 配音这一步：母带已经在盘上（下面造），所以"没有新产物"。
    monkeypatch.setattr(render_service, "reuse_or_synthesize_voice", lambda request, **kwargs: None)
    monkeypatch.setattr(render_service, "probe_media", _probe)

    master = paths.voice_master(TASK)
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"RIFF" + bytes(2048))
    return rig


def _probe(path: Path) -> MediaInfo:
    """``ffprobe`` 的替身。

    人声母带的时长进哈希（两次必须一模一样），所以固定成 8100ms。逐句音频**故意不造**，
    于是这里要像真 :func:`~studio.core.media.probe_media` 那样对不存在的文件抛
    ``StudioError`` —— 让"量不到逐句音频 ⇒ 这条片子没字幕"那条降级路径照常走
    （``_probe_ms`` 会吞掉它）。字幕这一环不参与本文件要测的东西。
    """
    if not path.is_file():
        raise StudioError(
            f"素材文件不存在：{path}",
            code=ErrorCode.MEDIA_UNDECODABLE,
            context={"path": path.as_posix()},
        )
    return MediaInfo(
        path=path.as_posix(),
        size_bytes=path.stat().st_size,
        duration_ms=8100,
        video_codec=None,
        audio_codec="pcm_s16le",
        width=None,
        height=None,
        fps=None,
        pix_fmt=None,
        sample_rate=24000,
        channels=1,
        bitrate_kbps=384,
    )


# ══════════════════════════════════════════════════════════════════════
# 命中：第二次不跑 ffmpeg
# ══════════════════════════════════════════════════════════════════════


def test_a_second_run_with_the_same_inputs_does_not_call_ffmpeg(rig: Rig) -> None:
    """★ 验收 ⑤：同 ``composite_hash`` 的二次运行**一次 ffmpeg 都不跑**。"""
    first = rig.run()
    assert rig.ffmpeg_calls == 2  # 合成一次 + 量响度一次
    assert first.reused is False

    second = rig.run()

    assert rig.ffmpeg_calls == 2, "第二次又起了 ffmpeg"
    assert second.reused is True
    assert second.final == first.final, "复用的应当是盘上那一支（不再另存一个新名字）"
    assert second.composite_hash == first.composite_hash
    assert second.size_bytes == first.size_bytes


def test_a_reused_delivery_still_carries_the_quality_reading(rig: Rig) -> None:
    """QC 不重量也能报：读数从上一轮 manifest 里读回来（文件没变，读数就没变）。"""
    rig.run()
    second = rig.run()

    assert rig.measured == [second.final], "响度被重量了一遍"
    report = quality_report(second)
    assert report.lufs == round(MEASURED.input_i, 2)
    assert report.true_peak == round(MEASURED.input_tp, 2)


def test_the_manifest_records_the_reuse(rig: Rig) -> None:
    """★ 留痕：manifest 说得出"这一支不是本次渲的"，以及它是**什么时候**渲的。"""
    first = rig.run()
    first_payload = json.loads(first.manifest.read_text(encoding="utf-8"))
    assert first_payload["reused"] is False
    assert first_payload["rendered_at"] == first_payload["created_at"]

    rig.run()
    payload = json.loads(first.manifest.read_text(encoding="utf-8"))

    assert payload["reused"] is True
    assert payload["rendered_at"] == first_payload["created_at"], "渲染时刻应当留在上一轮那个"
    # 上一轮那几条说明原样沿用（它们描述的是同一支片子）
    assert payload["warnings"] == first_payload["warnings"]


def test_the_panel_is_told_that_the_delivery_was_reused(rig: Rig) -> None:
    """进度里要看得见"命中缓存"：否则面板上会显示成"又渲了一遍"。"""
    rig.run()
    rig.progress.clear()
    rig.run()

    notes = [row[3] for row in rig.progress]
    assert any("命中渲染缓存" in note for note in notes), notes
    # 分母也是百分比（T3.4）：命中缓存 = 100%，而不是"第 1 段 / 共 1 段"
    assert (PROGRESS_TOTAL, PROGRESS_TOTAL) in [(row[1], row[2]) for row in rig.progress]


# ══════════════════════════════════════════════════════════════════════
# 不命中：输入变了 / 产物没了 / 强制重渲
# ══════════════════════════════════════════════════════════════════════


def test_a_changed_voice_master_invalidates_the_cache(rig: Rig) -> None:
    """★ 验收 ⑥：人声变了 ⇒ 哈希变 ⇒ 重渲（重录一遍配音是最常见的改动）。"""
    first = rig.run()
    rig.paths.voice_master(TASK).write_bytes(b"RIFF" + bytes(4096))

    second = rig.run()

    assert len(rig.delivered) == 2, "人声换了却没重渲"
    assert second.reused is False
    assert second.composite_hash != first.composite_hash


def test_a_cleaned_up_artifact_is_rendered_again(rig: Rig) -> None:
    """成片被清理策略回收了 ⇒ 重渲（manifest 还在也不认）。"""
    first = rig.run()
    first.final.unlink()

    second = rig.run()

    assert len(rig.delivered) == 2
    assert second.reused is False


def test_a_truncated_artifact_is_rendered_again(rig: Rig) -> None:
    """★ 半成品（字节数对不上）⇒ 重渲：这正是"产物在就跳过"那种短路的反面。"""
    first = rig.run()
    first.final.write_bytes(b"x")

    second = rig.run()

    assert len(rig.delivered) == 2
    assert second.reused is False


def test_force_render_ignores_the_cache(rig: Rig) -> None:
    """``--force-render``：输入一个字节没变也重跑一遍（排查"盘上那支是不是坏的"）。"""
    first = rig.run()
    forced = rig.run(force_render=True)

    assert len(rig.delivered) == 2
    assert forced.reused is False
    assert forced.composite_hash == first.composite_hash
