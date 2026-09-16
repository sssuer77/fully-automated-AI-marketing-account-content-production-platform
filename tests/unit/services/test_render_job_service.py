"""``RenderJobService`` 的 QC 回填（T3.7）—— 出片之后那一笔写库。

它只做一件事，但做错的方式有好几种，而且都**不会**让人立刻发现：

- 写库失败把整次出片翻成 ``failed`` ⇒ 片子明明在盘上，面板却报红；
- 任务不在库里（面板上直接填文案那种）⇒ 报错，等于用一个附属功能否掉主功能；
- 写进去的响度是 loudnorm 的**输入**读数 ⇒ 一条正常的片子显示成 −22 LUFS。

这三条在这里各钉一个用例。真跑 ffmpeg 的那部分在
`tests/integration/test_render_pipeline.py`（黑屏用例会顺带验 QC 数字）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.domain.task_service import TaskService
from studio.render.composite import CompositeResult
from studio.render.mixdown import LoudnessMeasurement
from studio.render.subtitle import SubtitlePlan
from studio.services.render_job_service import RenderJobService
from studio.services.render_service import ProduceResult

COMPOSITE = CompositeResult(
    output=Path("final.mp4"),
    duration_ms=3500,
    size_bytes=4,
    watermark_applied=False,
    bgm_applied=False,
    subtitle_applied=False,
    bg_fill="black",
    loudness=None,
    warnings=(),
    argv=(),
)
SUBTITLE = SubtitlePlan(
    enabled=False, ass_path=None, cues=(), font_dir=None, font_name="", skipped_reason="没有字体"
)
#: 成片实测（不是 loudnorm 的输入读数 —— 那一个大概是 −22 上下）
MEASURED = LoudnessMeasurement(
    input_i=-16.1, input_tp=-1.12, input_lra=3.0, input_thresh=-26.0, target_offset=0.2
)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    conn = connect(paths.db_file)
    try:
        yield conn
    finally:
        conn.close()


def _result(task_id: str, tmp_path: Path, *, measured: Any = MEASURED) -> ProduceResult:
    return ProduceResult(
        task_id=task_id,
        final=tmp_path / "final.mp4",
        clip=None,
        bgm=None,
        voice=None,
        voice_duration_ms=3000,
        duration_ms=3500,
        size_bytes=4,
        watermark_enabled=False,
        watermark_skipped_reason="没有水印文件",
        profile_name="douyin_1080x1920_30fps_v1",
        manifest=tmp_path / "manifest.json",
        timeline=tmp_path / "timeline.json",
        composite=COMPOSITE,
        subtitle=SUBTITLE,
        bg_fill="black",
        composite_hash="0" * 32,
        degraded=True,
        degrade_reason="no_broll_assets",
        attempts=(),
        output_loudness=measured,
    )


def _service(tmp_path: Path, factory: Any) -> RenderJobService:
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    return RenderJobService(paths=paths, connection_factory=factory)


def test_quality_is_backfilled_onto_the_task(connection: sqlite3.Connection, tmp_path: Path) -> None:
    """★ 出片之后 ``tasks.quality_json`` 拿到的是**成片实测**与降级留痕。"""
    task_id = TaskService(connection).create(title="QC 回填用例", actor="test").id
    service = _service(tmp_path, lambda: connection)

    assert service._record_quality(_result(task_id, tmp_path)) is None

    stored = TaskService(connection).get(task_id)
    assert stored.quality.lufs == -16.1
    assert stored.quality.true_peak == -1.12
    assert stored.quality.degraded is True
    assert stored.quality.degrade_reason == "no_broll_assets"


def test_a_task_that_is_not_in_the_db_is_not_an_error(connection: sqlite3.Connection, tmp_path: Path) -> None:
    """★ 面板上直接填文案出片（任务不在库里）⇒ 说明一句，**不报错**。

    为了回填 QC 而把"不建任务也能出片"这条路堵死，等于用一个附属功能否掉主功能。
    """
    service = _service(tmp_path, lambda: connection)
    note = service._record_quality(_result("不存在的任务", tmp_path))
    assert note is not None and "不在库里" in note


def test_no_connection_factory_is_also_fine(tmp_path: Path) -> None:
    """没给连接工厂（只读 / 纯出片场景）⇒ 同样只是说明一句。"""
    service = _service(tmp_path, None)
    note = service._record_quality(_result("t1", tmp_path))
    assert note is not None and "不在库里" in note


def test_unmeasured_loudness_leaves_the_fields_empty(connection: sqlite3.Connection, tmp_path: Path) -> None:
    """量不出来 ⇒ 留空（``None``），**不填一个默认值**。

    填 −16.0 会让发布门禁读到一个"恰好合格"的假数 —— 比没有数危险得多。
    """
    task_id = TaskService(connection).create(title="QC 空值用例", actor="test").id
    service = _service(tmp_path, lambda: connection)

    assert service._record_quality(_result(task_id, tmp_path, measured=None)) is None

    quality = TaskService(connection).get(task_id).quality
    assert quality.lufs is None and quality.true_peak is None
    # 降级留痕照写：响度量不出来不该把"这次是黑屏降级"也一起吞掉
    assert quality.degrade_reason == "no_broll_assets"
