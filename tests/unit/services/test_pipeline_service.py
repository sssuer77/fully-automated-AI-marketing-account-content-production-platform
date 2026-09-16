"""``pipeline_service``（T3.7）—— 编排纪律：从哪接手、到哪停、什么时候**不**往下走。

为什么这一层要用假件测
----------------------
它做的事**全是编排**：读状态、迁移、调服务。真跑一遍要 SAPI + ffmpeg（几十秒），
而真正容易错的恰恰是"状态判断"这一层 —— 比如把 `awaiting_approval` 也顺手放行、
或者在没有稿件时先把任务推到 `drafting` 再报错（任务从此卡在一个自己走不下去的地方）。
这些用假件几毫秒就能钉死，真跑一遍反而看不出来。

真链路在 `tests/integration/test_render_pipeline.py`（黑屏用例走的就是 `produce_video`）。
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db import connect, migrate
from studio.db.queue import JobStore
from studio.db.repositories import ApprovalRepo, ScriptRepo
from studio.domain.enums import TaskStatus
from studio.domain.models import QualityReport
from studio.domain.task_service import TaskService
from studio.render.composite import CompositeResult
from studio.render.mixdown import LoudnessMeasurement
from studio.render.subtitle import SubtitlePlan
from studio.services import pipeline_service
from studio.services.pipeline_service import SUPPORTED_UNTIL, TTS_DEGRADE_REASON, run_task
from studio.services.render_service import ProduceRequest, ProduceResult, quality_report
from studio.services.voice_service import VoiceStageReport
from studio.tts.timeline import TimelineSource, build_timeline

REPO_ROOT = Path(__file__).resolve().parents[3]
SENTENCES = ("第一句。", "第二句。")

#: 合成 / 字幕的结论在这里只是占位（编排层原样转给 QC，不解读内容）
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


def _outputs(tmp_path: Path) -> tuple[OutputsConfig, Path]:
    """真实 ``config/outputs.yaml`` 的一份副本（阈值与 profile 都不另写一份）。"""
    target = tmp_path / "studio" / "config" / "outputs.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((REPO_ROOT / "config" / "outputs.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return load_outputs_config(target), target


def _paths_with_pools(tmp_path: Path) -> StudioPaths:
    """带上真 ``pools.yaml`` 的临时家目录（排空那一步要按池的旋钮组装 worker）。

    与 ``connection`` 夹具落在**同一个** ``tmp_path`` 上：两处各构造一次
    :class:`StudioPaths` 是安全的，``db_file`` 由 ``home`` / ``data_dir`` 唯一决定。
    """
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    paths.ensure_runtime_dirs()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "config" / "pools.yaml", paths.config_dir / "pools.yaml")
    return paths


def _script(connection: sqlite3.Connection, task_id: str) -> None:
    ScriptRepo(connection).save_draft(
        task_id=task_id,
        title="测试稿",
        hook="开场",
        body_md="正文",
        cta="结尾",
        word_count=10,
        est_duration_ms=3000,
        speaker_ratio={"bigbear": 1.0},
        outline={},
        sentences=[
            {"seq": index, "text_raw": text, "text": text, "speaker": "bigbear"}
            for index, text in enumerate(SENTENCES, start=1)
        ],
    )


def _task(
    connection: sqlite3.Connection,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    with_script: bool = True,
) -> str:
    """建一个任务并推到 ``status``（走真实迁移，不直接 UPDATE —— 那是另一条纪律）。"""
    tasks = TaskService(connection)
    task = tasks.create(title="流水线用例", actor="test")
    if with_script:
        _script(connection, task.id)
    walk = {
        TaskStatus.PENDING: (),
        TaskStatus.DRAFTING: (TaskStatus.DRAFTING,),
        TaskStatus.REVIEWING: (TaskStatus.DRAFTING, TaskStatus.REVIEWING),
        TaskStatus.AWAITING_APPROVAL: (
            TaskStatus.DRAFTING,
            TaskStatus.REVIEWING,
            TaskStatus.AWAITING_APPROVAL,
        ),
        TaskStatus.QUEUED_VOICE: (TaskStatus.DRAFTING, TaskStatus.REVIEWING, TaskStatus.QUEUED_VOICE),
        TaskStatus.FAILED: (TaskStatus.DRAFTING, TaskStatus.FAILED),
    }[status]
    for target in walk:
        tasks.transition(task.id, target, actor="test", reason="setup")
    if status is TaskStatus.AWAITING_APPROVAL:
        # 走真实仓储开一条待审（不用裸 SQL 拼列名：DDL 改了这里会静默错位）
        ApprovalRepo(connection).request(
            task_id=task.id, script_id=None, grade="B", score_total=6.5, revision_round=0
        )
    return task.id


def _stage_report(task_id: str, *, sentences: int = 2, degraded: int = 0) -> VoiceStageReport:
    """一份假的配音收口结论。

    时间轴是**纯计算**（``tts.timeline.build_timeline`` 不碰盘），所以这里造得出
    一条结构完整的时间轴，而不必去真念几句 —— 编排层不解读它的内容，只读
    "几句 / 母带多长 / 几句降级"。
    """
    timeline = build_timeline(
        [
            TimelineSource(
                sentence_id=f"{task_id}-s{index:03d}",
                seq=index,
                speaker="bigbear",
                text=SENTENCES[index - 1],
                audio=Path(f"s{index:03d}.wav"),
                duration_ms=1500,
                base_pause_ms=200,
            )
            for index in range(1, sentences + 1)
        ],
        task_id=task_id,
        seed=task_id,
        tail_ms=400,
        data_dir=Path("data"),
        voice_master=Path("voice_master.wav"),
    )
    return VoiceStageReport(
        task_id=task_id,
        timeline=timeline,
        timeline_path=Path("timeline.json"),
        voice_master=Path("voice_master.wav"),
        voice_master_ms=timeline.master_ms,
        degraded=degraded,
    )


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把"真的干活"那两步换成假件；调用参数原样记下来供断言。

    ``calls["stage"]`` 是下一次收口的结论（用例可以换成"全句降级"那一份）；
    ``calls["render_degraded"]`` / ``["render_reason"]`` 同理，用来造"出片自己也降级"。
    """
    calls: dict[str, Any] = {
        "drain": [],
        "settle": [],
        "render": [],
        "stage": None,
        "render_degraded": True,
        "render_reason": "no_broll_assets",
    }

    def fake_drain(**kwargs: Any) -> None:
        calls["drain"].append(kwargs)
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress("voice", 2, 2, "假排空")

    def fake_settle(**kwargs: Any) -> VoiceStageReport:
        calls["settle"].append(kwargs)
        stage: VoiceStageReport | None = calls["stage"]
        if stage is None:
            stage = _stage_report(kwargs["task_id"])
        return stage

    def fake_render(request: ProduceRequest, **kwargs: Any) -> ProduceResult:
        calls["render"].append(request)
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress("render", 0, 1, "假渲染")
        final = paths_holder["paths"].videos_dir / "fake_final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"fake")
        return ProduceResult(
            task_id=request.task_id,
            final=final,
            clip=None,
            bgm=None,
            voice=None,
            voice_duration_ms=3000,
            duration_ms=3500,
            size_bytes=4,
            watermark_enabled=False,
            watermark_skipped_reason="没有水印文件",
            profile_name="douyin_1080x1920_30fps_v1",
            manifest=final,
            timeline=final,
            composite=COMPOSITE,
            subtitle=SUBTITLE,
            bg_fill="black",
            composite_hash="0" * 32,
            degraded=calls["render_degraded"],
            degrade_reason=calls["render_reason"],
            attempts=(),
            output_loudness=None,
        )

    paths_holder: dict[str, StudioPaths] = {}
    monkeypatch.setattr(pipeline_service, "_drain_voice_pool", fake_drain)
    monkeypatch.setattr(pipeline_service, "settle_voice", fake_settle)
    monkeypatch.setattr(pipeline_service, "produce_video", fake_render)
    calls["paths"] = paths_holder
    return calls


def test_until_rejects_states_the_pipeline_cannot_land_on(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """``--until rendering`` 这种"一口气跑完"的瞬时态不收 —— 收了会得到"有时停得住"。

    ``voicing`` 反过来**收**：配音被拆成"投递"与"排空"两步，投递完任务就真的停在
    那儿（见 ``test_until_voicing_enqueues_and_stops``）。
    """
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection)
    with pytest.raises(StudioError) as caught:
        run_task(
            task_id=task_id,
            paths=StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data"),
            outputs=outputs,
            outputs_source=source,
            connection=connection,
            until=TaskStatus.RENDERING,
        )
    assert caught.value.code is ErrorCode.STATE_TRANSITION_ILLEGAL
    assert TaskStatus.RENDERING not in SUPPORTED_UNTIL
    assert TaskStatus.VOICING in SUPPORTED_UNTIL


def test_already_past_until_is_a_no_op(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """幂等：已经跑过的命令再跑一次，不重渲、不报错。"""
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection, status=TaskStatus.REVIEWING)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    report = run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.REVIEWING,
    )
    assert report.steps == ()
    assert report.status == TaskStatus.REVIEWING.value
    assert fakes["render"] == []


def test_missing_script_stops_before_touching_the_state(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """★ 没有生效稿件 ⇒ 报错，且**状态原样不动**。

    先推到 ``drafting`` 再报错的话，任务会停在一个自己走不下去的地方（没有稿件就
    到不了 ``reviewing``），下次接手的人只能人工捞回。
    """
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection, with_script=False)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    with pytest.raises(StudioError) as caught:
        run_task(
            task_id=task_id,
            paths=paths,
            outputs=outputs,
            outputs_source=source,
            connection=connection,
            until=TaskStatus.COMPLETED,
        )
    assert caught.value.code is ErrorCode.SCRIPT_NOT_FOUND
    assert TaskService(connection).get(task_id).status is TaskStatus.PENDING


def test_awaiting_approval_is_never_pressed_by_the_pipeline(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """★ 确认闸是**唯一**人工节点：一条命令跑到底不能顺手把它按了。"""
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection, status=TaskStatus.AWAITING_APPROVAL)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    with pytest.raises(StudioError) as caught:
        run_task(
            task_id=task_id,
            paths=paths,
            outputs=outputs,
            outputs_source=source,
            connection=connection,
            until=TaskStatus.COMPLETED,
        )
    assert caught.value.code is ErrorCode.APPROVAL_NOT_PENDING
    assert TaskService(connection).get(task_id).status is TaskStatus.AWAITING_APPROVAL


def test_stuck_statuses_are_refused(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """``failed`` 不自动续跑：重试链（退避 / 死信 / manual_pool）才是它的出口。"""
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection, status=TaskStatus.FAILED)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    with pytest.raises(StudioError) as caught:
        run_task(
            task_id=task_id,
            paths=paths,
            outputs=outputs,
            outputs_source=source,
            connection=connection,
            until=TaskStatus.COMPLETED,
        )
    assert caught.value.code is ErrorCode.STATE_TRANSITION_ILLEGAL


def test_happy_path_walks_to_completed_and_backfills_quality(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """★ 主线：``pending`` ⇒ ``completed``，四步都留痕，QC 真的写进库里。"""
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    report = run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.COMPLETED,
    )

    assert report.status_before == TaskStatus.PENDING.value
    assert report.status == TaskStatus.COMPLETED.value
    assert [step.stage for step in report.steps] == [
        "script",
        "review",
        "gate",
        "voice",
        "timeline",
        "render",
    ]
    assert report.final is not None and report.final.is_file()
    # 配音拆成两步（投递 / 排空 + 收口），渲染走的是"复用母带"（配音那一步已经合成了）
    assert len(fakes["drain"]) == 1 and len(fakes["settle"]) == 1
    assert fakes["render"][0].reuse_voice is True
    # 文案来自库里的生效稿件（逐句拼接），不是面板随手传的空串
    assert fakes["render"][0].text == "".join(SENTENCES)

    stored = TaskService(connection).get(task_id)
    assert stored.status is TaskStatus.COMPLETED
    assert stored.quality.degraded is True
    assert stored.quality.degrade_reason == "no_broll_assets"
    # 一期不做音画同步（C12）⇒ 留空，不是写 0（写 0 会被读成"误差为 0"）
    assert stored.quality.av_sync_offset_ms is None


def test_until_queued_voice_stops_before_synthesizing(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """``--until queued_voice``：放行到"待配音"就停 —— 一步活都不干。"""
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection, status=TaskStatus.REVIEWING)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    report = run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.QUEUED_VOICE,
    )
    assert report.status == TaskStatus.QUEUED_VOICE.value
    assert [step.stage for step in report.steps] == ["gate"]
    assert fakes["drain"] == [] and fakes["settle"] == [] and fakes["render"] == []
    assert JobStore(connection).stats(pool="voice").pending == 0, "排队 ≠ 投递"


def test_until_voicing_enqueues_and_stops(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """★ ``--until voicing``：投递作业后**停在 voicing** —— 排空与收口留给下一次接手。

    这就是"配音拆两步"的全部意义：投递是一个**可停在、可续跑**的状态。合成一口气
    做完的话，``--until voicing`` 只能得到"有时停得住"（命令返回时任务已经跑过去了）。
    """
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection, status=TaskStatus.REVIEWING)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    report = run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.VOICING,
    )

    assert report.status == TaskStatus.VOICING.value
    assert [step.stage for step in report.steps] == ["gate", "voice"]
    assert JobStore(connection).stats(pool="voice").pending == len(SENTENCES)
    assert fakes["drain"] == [] and fakes["settle"] == [] and fakes["render"] == []


def test_a_second_run_picks_up_from_voicing(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """★ 断点续跑：停在 ``voicing`` 之后，下一次接手**接着**排空 + 收口。

    投递是幂等的（``JobStore.enqueue`` 命中唯一约束就 ``DO NOTHING``），所以
    "再投一遍"也安全 —— 但这条用例钉的是"续跑不需要重投"：第二次只走 ``timeline``
    这一步，队列里那两条作业还是第一次投的。
    """
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection, status=TaskStatus.QUEUED_VOICE)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    first = run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.VOICING,
    )
    assert first.status == TaskStatus.VOICING.value

    second = run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.QUEUED_RENDER,
    )

    assert second.status == TaskStatus.QUEUED_RENDER.value
    assert [step.stage for step in second.steps] == ["timeline"]
    assert len(fakes["drain"]) == 1 and len(fakes["settle"]) == 1
    assert fakes["render"] == [], "queued_render 就是落点，渲染是下一步的事"


def test_streaming_render_is_refused_in_phase_one(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """C7：``pipeline.streaming_render=true`` 一期没实现 ⇒ **开工就报**，且状态不动。

    默默忽略它比报错更坏：改配置的人会以为"边配音边渲染开着"，而成片时长其实来自
    另一条路径 —— 等到发现时长不对时，没人会想到去看这个开关。
    """
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths

    with pytest.raises(StudioError) as caught:
        run_task(
            task_id=task_id,
            paths=paths,
            outputs=outputs,
            outputs_source=source,
            connection=connection,
            until=TaskStatus.COMPLETED,
            streaming_render=True,
        )

    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert TaskService(connection).get(task_id).status is TaskStatus.PENDING


def test_a_fully_degraded_voice_stage_survives_the_render_step(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """★ 全句降级 ⇒ ``quality.degrade_reason='tts_unavailable'``，**出片那一步冲不掉它**。

    两件事一起验，因为它们必须同时成立才有意义：
    ① 配音这一步要把"整条片子进了字幕模式"写进 ``quality_json``（§04.3.3 熔断后行为，
       任务**不失败**）；
    ② 出片这一步是整体覆盖式的 ``set_quality``（"一次出片就是一份结论"），它得把
       配音的结论带过来 —— 否则成片照样出得来，而"一句人声都没有"这件事在库里消失了。
    """
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths
    fakes["stage"] = _stage_report(task_id, degraded=len(SENTENCES))
    # 出片这一步自己**没有**降级（有底片、编码没失败）
    fakes["render_degraded"] = False
    fakes["render_reason"] = None

    report = run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.COMPLETED,
    )

    assert report.status == TaskStatus.COMPLETED.value
    stored = TaskService(connection).get(task_id)
    assert stored.quality.degraded is True
    assert stored.quality.degrade_reason == TTS_DEGRADE_REASON


def test_the_render_reason_wins_when_both_steps_degrade(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """两边都降级 ⇒ ``degrade_reason`` 留给**出片**那一步。

    这个槽位只有一个。配音的实情仍在逐句状态里（哪几句是静音、点得开），而"画面也是
    降级的"只在这一处留痕 —— 挑那个**没有别的地方能说清**的结论。
    """
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths
    fakes["stage"] = _stage_report(task_id, degraded=len(SENTENCES))
    fakes["render_degraded"] = True
    fakes["render_reason"] = "no_broll_assets"

    run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.COMPLETED,
    )

    stored = TaskService(connection).get(task_id)
    assert stored.quality.degraded is True
    assert stored.quality.degrade_reason == "no_broll_assets"


def test_a_partly_degraded_voice_stage_stays_out_of_quality(
    connection: sqlite3.Connection, tmp_path: Path, fakes: dict[str, Any]
) -> None:
    """部分降级**不**写 ``tts_unavailable`` —— 那是"整条片子进了字幕模式"的结论。

    留痕在逐句状态（``script_sentences.tts_status``）与 ``jobs.result_json`` 里，
    那里说得出**是哪一句**；塞进只有一个槽位的 ``degrade_reason`` 只会把更重的那句话
    挤掉，而且说不清是谁。
    """
    outputs, source = _outputs(tmp_path)
    task_id = _task(connection)
    paths = StudioPaths(home=tmp_path / "studio", data_dir=tmp_path / "studio" / "data")
    fakes["paths"]["paths"] = paths
    fakes["stage"] = _stage_report(task_id, degraded=1)
    fakes["render_degraded"] = False
    fakes["render_reason"] = None

    run_task(
        task_id=task_id,
        paths=paths,
        outputs=outputs,
        outputs_source=source,
        connection=connection,
        until=TaskStatus.COMPLETED,
    )

    stored = TaskService(connection).get(task_id)
    assert stored.quality.degraded is False
    assert stored.quality.degrade_reason is None


def test_quality_report_reads_the_delivered_file_not_the_input(
    tmp_path: Path,
) -> None:
    """★ ``quality_json.lufs`` 取**成片实测**，不是 loudnorm 的输入读数。

    拿输入读数充数的话，一条听起来正常的片子会显示成 −22 LUFS，
    发布门禁再按这个数把好片子拦下来。
    """
    measured = LoudnessMeasurement(
        input_i=-16.1, input_tp=-1.12, input_lra=3.0, input_thresh=-26.0, target_offset=0.2
    )
    assert quality_report(_produce_result(tmp_path, measured)).lufs == -16.1
    assert quality_report(_produce_result(tmp_path, measured)).true_peak == -1.12
    # 量不出来 ⇒ 留空（None），不是填一个默认值
    assert quality_report(_produce_result(tmp_path, None)).lufs is None


def _produce_result(tmp_path: Path, loudness: Any) -> ProduceResult:
    return ProduceResult(
        task_id="t1",
        final=tmp_path / "final.mp4",
        clip=None,
        bgm=None,
        voice=None,
        voice_duration_ms=1,
        duration_ms=1,
        size_bytes=1,
        watermark_enabled=False,
        watermark_skipped_reason=None,
        profile_name="p",
        manifest=tmp_path / "manifest.json",
        timeline=tmp_path / "timeline.json",
        composite=COMPOSITE,
        subtitle=SUBTITLE,
        bg_fill="black",
        composite_hash="0" * 32,
        degraded=False,
        degrade_reason=None,
        attempts=(),
        output_loudness=loudness,
    )


def test_set_quality_does_not_bump_version(connection: sqlite3.Connection) -> None:
    """★ 回填 QC **不动** ``version``：它是状态迁移的乐观锁，不是"这行被写过几次"。"""
    tasks = TaskService(connection)
    task_id = _task(connection, status=TaskStatus.REVIEWING)
    before = tasks.get(task_id)

    after = tasks.set_quality(task_id, QualityReport(lufs=-16.1, true_peak=-1.12, degraded=True))
    assert after.version == before.version
    assert after.quality.lufs == -16.1
    # 整体覆盖而不是合并：一次出片就是一份结论
    after = tasks.set_quality(task_id, QualityReport(lufs=-16.4))
    assert after.quality.true_peak is None
    assert after.quality.degraded is False


# ══════════════════════════════════════════════════════════════════════
# 排空的节奏：空转时必须**等一拍**
# ══════════════════════════════════════════════════════════════════════


class _IdleWorker:
    """认领永远返回空的 worker 假件（这一条验的是"空转时外层等不等"）。"""

    def __init__(self, **_: Any) -> None:
        self.runs = 0

    def run(self, *, max_units: int | None = None, max_empty_rounds: int | None = None) -> Any:
        self.runs += 1
        return SimpleNamespace(units_done=0)


class _WaitedError(Exception):
    """哨兵：捕获到它就说明外层确实等了一拍。"""


def test_an_idle_drain_waits_instead_of_spinning(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 空转时**必须等一拍**（池的 ``poll_ms``）。

    ``run(max_empty_rounds=1)`` 在空池时**立刻**返回（认领的语义就是"没有就返回"），
    外层不等就成了忙等 —— 而句子退避最长要等 ``backoff_max_ms``（voice 池 20s）。
    真机演练里已经见过这一幕：30 毫秒刷了 30 多对 ``worker.started/stopped``。
    """
    paths = _paths_with_pools(tmp_path)
    task_id = _task(connection, status=TaskStatus.QUEUED_VOICE)
    monkeypatch.setattr("studio.services.pipeline_service.build_voice_handler", lambda **_: object())
    monkeypatch.setattr("studio.services.pipeline_service.PoolWorker", _IdleWorker)
    waits: list[float] = []

    def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        raise _WaitedError

    monkeypatch.setattr("studio.services.pipeline_service.sleep", fake_sleep)

    with pytest.raises(_WaitedError):
        pipeline_service._drain_voice_pool(
            paths=paths, connection=connection, task_id=task_id, voice=None, on_progress=None
        )

    assert waits == [0.5], "等一拍 = 池的 poll_ms（voice 池 500ms）"
