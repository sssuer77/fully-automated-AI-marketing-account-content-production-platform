"""渲染池单元处理器（``render/final``）的单元测试（T3.7 收口）。

测什么、不测什么
----------------
"真出一支能播的 MP4"由 ``tests/integration/test_render_pool.py`` 端到端覆盖。
这里只测**单元处理器自己的几件事** —— 每一件出错都会**静默走偏**：

① ``payload_json`` ⇒ ``ProduceRequest`` 的逐字段收编：多一个键 / 类型不对必须退回默认值，
   而不是让整个池停摆（滚动升级时新旧字段会同时存在）；
② 进度**整份覆写** ``jobs.result_json``，失败时那份 dict 就是"停在哪儿"的现场；
③ 失败时把 ``remediation`` 一起写进现场 —— ``jobs`` 表**没有**这一列，
   丢了它面板就只能说"失败了"，说不出"下一步怎么办"；
④ 处理器**不自己收尾**：跑完之后作业仍是 ``claimed``，``succeed`` / ``fail`` 是队列的事。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect
from studio.db.migrate import migrate
from studio.db.queue import JobStore
from studio.pools import render_worker
from studio.pools.heartbeat import WorkerIdentity
from studio.pools.render_worker import (
    RENDER_UNIT_TYPES,
    RenderFinalHandler,
    _clip,
    _request_from,
    build_render_handler,
)
from studio.pools.runner import HANDLER_MODULES
from studio.pools.worker_base import UnitContext, _Pulse
from studio.render.mixdown import LoudnessMeasurement
from studio.services.render_service import ProduceResult

WORKER_ID = "render#1@4242"


@dataclass
class Rig:
    """一套隔离的运行时：路径契约 + 已迁移的库 + 队列门面（**主线程专用**）。"""

    paths: StudioPaths
    connection: sqlite3.Connection
    store: JobStore


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    home = tmp_path / "studio"
    paths = StudioPaths(home=home, data_dir=home / "data")
    paths.ensure_runtime_dirs()
    migrate(paths.db_file)
    connection = connect(paths.db_file)
    connection.execute("INSERT INTO tasks(id, title) VALUES ('t1', '测试任务')")
    try:
        yield Rig(paths=paths, connection=connection, store=JobStore(connection))
    finally:
        connection.close()


def _handler(rig: Rig) -> RenderFinalHandler:
    return build_render_handler(paths=rig.paths, connection=rig.connection)


def _claimed(rig: Rig, payload: Mapping[str, Any]) -> tuple[UnitContext, str]:
    """入队一条 ``render/final`` 并认领它，返回（执行上下文, job_id）。

    真的走一遍 ``enqueue`` + ``claim``（而不是手搓一个 ``Job``）：处理器读的是
    ``ctx.payload`` / ``ctx.job_id`` / ``ctx.worker_id``，这三样只有真作业才有；
    手搓出来的假件会让"幂等键 / 租约"这些真正会出问题的地方测不到。
    """
    job_id = rig.store.enqueue(
        task_id="t1", pool="render", unit_type="final", unit_ref="final", payload=payload
    )
    assert job_id is not None
    job = rig.store.claim(pool="render", worker_id=WORKER_ID)
    assert job is not None and job.id == job_id
    pulse = _Pulse(
        identity=WorkerIdentity(pool="render", slot=1, pid=4242),
        connection_factory=lambda: rig.connection,
        version=None,
    )
    ctx = UnitContext(
        job=job,
        pool="render",
        worker_id=WORKER_ID,
        paths=rig.paths,
        timeout_sec=900,
        pulse=pulse,
        renew=lambda: True,
    )
    return ctx, job_id


def _fake_result(final: Path, *, loudness: LoudnessMeasurement | None = None) -> ProduceResult:
    """一份够用的 ``ProduceResult``。

    **不**手搓真的 ``CompositeResult`` / ``SubtitlePlan`` / ``VoiceResult``：那三个
    数据结构各有十几项，手搓出来的假件会随着它们的演化悄悄失真 —— 而这里要验的是
    "结论有没有被如实投影进 ``jobs.result_json``"，不是"ffmpeg 跑没跑通"。
    """
    return cast(
        ProduceResult,
        SimpleNamespace(
            task_id="t1",
            final=final,
            profile_name="douyin_1080x1920_30fps_v1",
            duration_ms=12_000,
            size_bytes=1_234_567,
            composite_hash="c0ffee",
            bg_fill="broll",
            degraded=False,
            degrade_reason=None,
            watermark_enabled=True,
            watermark_skipped_reason=None,
            output_loudness=loudness,
            warnings=("一句 warn",),
        ),
    )


# ══════════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════════


def test_handler_claims_the_final_unit_type() -> None:
    assert frozenset({"final"}) == RENDER_UNIT_TYPES
    assert RenderFinalHandler.unit_types == RENDER_UNIT_TYPES


def test_the_render_pool_module_is_declared_for_the_launcher() -> None:
    """启动器（裁定 103）**先问再拉**：它问的就是这张表。

    忘了登记 ⇒ 渲染池永远被判 ``handler_missing`` ⇒ 拒绝重启 ⇒ 起不来，
    而失败模式是"看着在跑、队列永远不消化"。
    """
    assert HANDLER_MODULES["render"] == "studio.pools.render_worker"
    assert importlib.util.find_spec(HANDLER_MODULES["render"]) is not None


# ══════════════════════════════════════════════════════════════════════
# ① payload ⇒ ProduceRequest
# ══════════════════════════════════════════════════════════════════════


def test_request_from_reads_every_field() -> None:
    request = _request_from(
        {
            "text": "口播文案",
            "profile_name": "douyin_1080x1920_30fps_v1",
            "voice": "Huihui",
            "reuse_voice": True,
            "threads": 5,
            "seed": 7,
            "subtitle": False,
        },
        task_id="t1",
    )
    assert request.task_id == "t1"
    assert request.text == "口播文案"
    assert request.profile_name == "douyin_1080x1920_30fps_v1"
    assert request.voice == "Huihui"
    assert request.reuse_voice is True
    assert request.threads == 5
    assert request.seed == 7
    assert request.subtitle is False


def test_request_from_ignores_unknown_keys() -> None:
    """面板加了新字段、worker 还没升上去 ⇒ **忽略它**，不是让整个池停摆。"""
    request = _request_from({"text": "x", "future_knob": {"nested": 1}}, task_id="t1")
    assert request.text == "x"
    assert request.profile_name is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, (None, None, None)),
        ({"threads": "5", "seed": "7"}, (None, None, None)),  # 字符串不是整数
        ({"threads": True, "seed": False}, (None, None)),  # bool 是 int 的子类，必须挡掉
        ({"threads": 5, "seed": 7}, (5, 7, None)),
    ],
)
def test_request_from_coerces_bad_types_to_defaults(
    payload: Mapping[str, Any], expected: tuple[int | None, int | None, bool | None]
) -> None:
    request = _request_from(payload, task_id="t1")
    assert (request.threads, request.seed) == expected[:2]


def test_request_from_keeps_subtitle_tristate() -> None:
    """``subtitle`` 是**三态**：``None`` = 听配置，``False`` = 显式关掉。

    用 ``bool(payload.get(...))`` 收编会把"面板没传"变成"面板说不要字幕" ——
    配置里开着的字幕会被一次提交悄悄顶掉。
    """
    assert _request_from({}, task_id="t1").subtitle is None
    assert _request_from({"subtitle": None}, task_id="t1").subtitle is None
    assert _request_from({"subtitle": False}, task_id="t1").subtitle is False
    assert _request_from({"subtitle": True}, task_id="t1").subtitle is True
    assert _request_from({"subtitle": "false"}, task_id="t1").subtitle is None


# ══════════════════════════════════════════════════════════════════════
# ② 进度说明的收尾
# ══════════════════════════════════════════════════════════════════════


def test_clip_flattens_newlines_and_truncates() -> None:
    assert _clip("第一句\n第二句") == "第一句 第二句"
    assert _clip("短") == "短"
    clipped = _clip("啊" * 200)
    assert len(clipped) == 80
    assert clipped.endswith("…")


# ══════════════════════════════════════════════════════════════════════
# ③ 失败现场
# ══════════════════════════════════════════════════════════════════════


def test_failure_writes_the_scene_and_reraises(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """失败时：现场留在 ``result_json``（含 ``remediation``），异常**照抛**给队列。"""

    def boom(request: object, **kwargs: Any) -> ProduceResult:
        on_progress = kwargs.get("on_progress")
        assert callable(on_progress)
        on_progress("voice", 2, 5, "第二句")
        raise StudioError(
            "ffmpeg 退出码 1",
            code=ErrorCode.RENDER_FAILED,
            context={"task_id": "t1"},
            remediation="看 data/logs/render.log 的 stderr 尾巴",
        )

    monkeypatch.setattr(render_worker, "produce_video", boom)
    ctx, job_id = _claimed(rig, {"text": "口播文案"})

    with pytest.raises(StudioError) as excinfo:
        _handler(rig).run(ctx)
    assert excinfo.value.code is ErrorCode.RENDER_FAILED

    stored = rig.store.get(job_id)
    assert stored.result["stage"] == "voice"  # 停在哪儿
    assert stored.result["done"] == 2 and stored.result["total"] == 5
    assert stored.result["error_code"] == str(ErrorCode.RENDER_FAILED)
    assert stored.result["remediation"] == "看 data/logs/render.log 的 stderr 尾巴"
    assert stored.result["note"].startswith("失败：")
    # ④ 处理器不自己收尾：收尾（重试 / 退避 / 死信 / 告警）是队列的事
    assert stored.status == "claimed"
    assert stored.error_code is None


def test_missing_text_and_missing_script_is_a_script_error(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 payload 文案、库里也没有生效稿件 ⇒ ``SCRIPT_NOT_FOUND``，且**不**去跑渲染。"""
    calls: list[str] = []

    def never(request: object, **kwargs: Any) -> ProduceResult:
        calls.append("called")
        raise AssertionError("不该走到渲染这一步")

    monkeypatch.setattr(render_worker, "produce_video", never)
    ctx, _job_id = _claimed(rig, {})

    with pytest.raises(StudioError) as excinfo:
        _handler(rig).run(ctx)
    assert excinfo.value.code is ErrorCode.SCRIPT_NOT_FOUND
    assert calls == []


# ══════════════════════════════════════════════════════════════════════
# ④ 成功：结论投影 + QC 回填
# ══════════════════════════════════════════════════════════════════════


def test_success_projects_the_outcome_and_backfills_quality(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = rig.paths.videos_dir / "20260916-010000_t1_final.mp4"
    loudness = LoudnessMeasurement(
        input_i=-16.1, input_tp=-1.3, input_lra=2.4, input_thresh=-26.0, target_offset=0.1
    )

    def produce(request: object, **kwargs: Any) -> ProduceResult:
        on_progress = kwargs.get("on_progress")
        assert callable(on_progress)
        on_progress("render", 0, 1, "合成中")
        return _fake_result(final, loudness=loudness)

    monkeypatch.setattr(render_worker, "produce_video", produce)
    ctx, job_id = _claimed(rig, {"text": "口播文案"})

    outcome = _handler(rig).run(ctx)

    assert outcome["stage"] == "done"
    assert outcome["final"] == final.as_posix()
    assert outcome["composite_hash"] == "c0ffee"
    assert outcome["output_loudness"]["input_i"] == -16.1
    assert outcome["quality_backfilled"] is True
    assert outcome["warnings"] == ["一句 warn"]

    # QC 结论落到了任务上（面板与发布门禁读的是同一份）
    row = rig.connection.execute("SELECT quality_json FROM tasks WHERE id = 't1'").fetchone()
    assert row is not None and '"lufs":-16.1' in row["quality_json"]
    # 作业本身仍未收尾 —— 那是队列的事
    assert rig.store.get(job_id).status == "claimed"


def test_success_without_a_task_row_still_returns_an_outcome(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务在"入队之后、出片之前"被人删掉 ⇒ QC 回填不了，但**片子照报成功**。

    为这个窗口把一次已经成功出片的作业判成失败，等于用附属数据否掉主产物。
    """
    monkeypatch.setattr(
        render_worker,
        "produce_video",
        lambda request, **kwargs: _fake_result(rig.paths.videos_dir / "x_final.mp4"),
    )
    ctx, _job_id = _claimed(rig, {"text": "口播文案"})
    rig.connection.execute("DELETE FROM jobs")
    rig.connection.execute("DELETE FROM tasks WHERE id = 't1'")

    outcome = _handler(rig).run(ctx)
    assert outcome["quality_backfilled"] is False
    assert outcome["note"] == "出片完成（QC 未回填）"
