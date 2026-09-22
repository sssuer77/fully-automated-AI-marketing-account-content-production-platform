"""流水线编排：把**一个任务**从"现在在哪"推到 ``--until`` 指定的状态（T3.7）。

它做什么
--------
``studio pipeline run <task_id> --until completed`` 是 T3.7 的验收命令。它按任务
**当前状态**决定下一步，而不是写死一条从头到尾的流水线 —— 于是"断点续跑"与"只跑到
配音为止"是同一段代码，而不是两条要各自维护的路径。

```text
pending → drafting → reviewing → queued_voice → voicing → queued_render → rendering → completed
   │          │          │            │                        │
   │          │          │            │                        └ 合成出片 + 回填 quality_json
   │          │          │            └ 逐句合成 + 拼母带
   │          │          └ 自动放行（无人工确认闸）
   │          └ 稿件必须已经存在（本命令不写稿）
   └ 同上
```

三件事它**故意不做**
--------------------
1. **不写稿、不审稿**。``pending`` / ``drafting`` 这两步只做状态迁移：Director /
   Writer / Reviewer 要 LLM 通道与花费，而本命令的定位是"稿子已经在了，把它变成片子"。
   没有生效稿件时直接报错并给出补救命令（``studio script draft``），而不是顺手去调一次
   LLM —— 那会让一条本地命令的耗时与账单在无人预期的时候暴涨。
2. **不代按确认闸**。``awaiting_approval`` ⇒ 报错停下。§04.4.4 说这是全流程**唯一**
   人工节点；"一条命令跑到底"如果顺手把它按了，那个节点就等于不存在了。
3. **不发布**。``completed`` 即停：发布有它自己的三道门禁与限频（§06.4）。

配音阶段为什么拆成两步（T2.8）
------------------------------
```text
queued_voice ──①投递──▶ voicing ──②排空 + 收口──▶ queued_render
```

①**投递**只做一件事：把还没定局的句子塞进 ``jobs``（幂等）。投完就停下 ——
``--until voicing`` 的落点就是这里："作业已经派出去了，等池干完"。

②**排空 + 收口**先就地借一条 worker 把这条任务的配音干完，再调
``voice_service.settle_voice`` 拼母带、算时间轴。``settle_voice`` 自己带守卫
（不是全部 ``done/skipped`` 就抛），所以 ``voicing → queued_render`` 这条边
**只有在时间轴真的算出来了**之后才可能被跨过 —— 守卫写在编排里会变成第二处
判据，而两处判据迟早会不一致。

拆成两步而不是一步做完，是为了让"作业派出去了"成为一个**可停在、可续跑**的
状态：``voicing`` 卡住时，一条 ``--until queued_render`` 就能接着把剩下的干完，
而不用重投一遍（重投是幂等的，但没人愿意先怀疑这个）。

为什么落在 ``services/`` 而不是规格里的 ``pipeline/orchestrator.py``
------------------------------------------------------------------
§02.1 的依赖方向是 ``services → pipeline → {agents, tts, render, publish, pools}``，
即 **pipeline 不能 import services**。而这段编排的主体恰恰就是调服务：读稿件
（``script_service``）、出片（``render_service``）、迁移状态（``TaskService``）。
塞进 ``pipeline/`` 只有两条路 —— 把 pipeline 提到 services 之上（改规格），或者在这里
铺一层回调协议（多出来的间接层没有任何人受益）。两条都不值当，所以放在服务层，
名字直接点明它是编排。``pipeline/`` 目录留空不动：等二期三层模板真的需要独立的场景
编排时再谈，那时它编排的是 ``render`` 内部的东西，与这里不冲突。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Final

from studio.core.config import OutputsConfig, load_pools_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths
from studio.db.queue import JobStore
from studio.db.repositories.sentence_repo import SentenceRepo
from studio.domain.enums import TaskStatus
from studio.domain.models import QualityReport
from studio.domain.task_service import TaskService
from studio.pools.voice_worker import build_voice_handler
from studio.pools.worker_base import PoolWorker
from studio.services.asset_service import disabled_assets
from studio.services.render_service import (
    ProduceRequest,
    produce_video,
    quality_report,
)
from studio.services.script_service import read_active_script
from studio.services.voice_service import (
    VoiceStageReport,
    active_script_voices,
    enqueue_sentences,
    settle_voice,
)

__all__ = [
    "SUPPORTED_UNTIL",
    "PipelinePlan",
    "PipelineReport",
    "StepReport",
    "plan_task",
    "run_task",
]

#: 进度回调，与 ``render_service.ProgressSink`` 同形（CLI 与面板共用同一个渲染器）。
ProgressSink = Callable[[str, int, int, str], None]

#: ``--until`` 允许的落点。
#:
#: ``voicing`` 收，``rendering`` 不收 —— 两者的区别不是"名字像不像瞬时态"，而是
#: **能不能真的停在那一刻**：配音阶段被拆成了"投递"与"排空"两步（见模块 docstring），
#: 投递完任务就停在 ``voicing``，等下一次接手（或常驻池）。渲染没有这一步 ——
#: ``rendering`` 是一口气跑完的瞬时态，拿它当落点只会得到"有时停得住、有时停不住"。
#:
#: ``awaiting_approval`` 也不收：那是人工节点，不是流水线的落点。
SUPPORTED_UNTIL: Final[tuple[TaskStatus, ...]] = (
    TaskStatus.REVIEWING,
    TaskStatus.QUEUED_VOICE,
    TaskStatus.VOICING,
    TaskStatus.QUEUED_RENDER,
    TaskStatus.COMPLETED,
)

#: 配音降级写进 ``quality_json.degrade_reason`` 的那个值（§04.3.3 熔断后行为）。
#: 它由**配音**那一步写、由**渲染**那一步带过去（见 :func:`_final_quality`）。
TTS_DEGRADE_REASON: Final[str] = "tts_unavailable"

#: 就地排空配音作业时"一直认不到活"的容忍下限（毫秒）。
#: 真正的上限按池的 ``backoff_max_ms`` 取（见 :func:`_drain_voice_pool`）—— 一个正在
#: 退避的句子最长要等这么久才会重新可认领，比它短就会把"还在退避"误判成"干完了"。
DRAIN_MIN_IDLE_MS: Final[int] = 5_000

#: 就地排空时用的槽位号。``worker_id`` 形如 ``voice#1@<pid>``（§04.5.1）⇒
#: 与常驻池的 worker 不会撞名，租约也就不会互相顶掉。
DRAIN_SLOT: Final[int] = 1

#: 就地排空时两次认领之间的**最小**间隔（毫秒）。真正的间隔按池的 ``poll_ms`` 取 ——
#: "多久该再看一眼队列"的官方答案就在池配置里；这个下限只防"配置被手改成 0"。
#: **没有间隔就是忙等**：``max_empty_rounds=1`` 的 ``run()`` 在空池时立刻返回，
#: 而句子退避最长要等 ``backoff_max_ms``（voice 池 20s）—— 忙等 20 秒等于几万次认领，
#: 外加几万对 ``worker.started/stopped`` 日志（真机演练里已经见过这一幕）。
DRAIN_POLL_FLOOR_MS: Final[int] = 50

#: 正向顺序。只用来回答一个问题："已经越过 ``--until`` 了吗" ⇒ 越过就直接收工。
#: 回退边（``editing`` / ``failed`` / ``manual_pool``）不在其中 —— 它们不是"更靠后"，
#: 是"另一条路"，见 :func:`run_task` 的显式分支。
_FORWARD: Final[tuple[TaskStatus, ...]] = (
    TaskStatus.PENDING,
    TaskStatus.DRAFTING,
    TaskStatus.REVIEWING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.QUEUED_VOICE,
    TaskStatus.VOICING,
    TaskStatus.QUEUED_RENDER,
    TaskStatus.RENDERING,
    TaskStatus.COMPLETED,
)

#: 停在这儿就别自动往下走的状态（各有各的人工出口）。
#:
#: ``editing`` 也在其中：它意味着审稿把稿子退回去改了，接手的应该是改稿循环，
#: 而不是渲染。少了这一条，``editing`` 会因为"不在正向链上"被 :func:`_rank` 判成
#: "已经越过 ``--until``" —— 命令返回成功、任务一步没动。**静默的空转比报错更难查**。
_STUCK: Final[frozenset[TaskStatus]] = frozenset(
    {
        TaskStatus.EDITING,
        TaskStatus.FAILED,
        TaskStatus.MANUAL_POOL,
        TaskStatus.DISCARDED,
        TaskStatus.CANCELED,
    }
)


def _rank(status: TaskStatus) -> int:
    """正向顺序里的位置；不在正向链上的状态排在 ``completed`` 之后（⇒ 立刻收工）。"""
    try:
        return _FORWARD.index(status)
    except ValueError:
        return len(_FORWARD)


def _check_until(until: TaskStatus) -> None:
    """落点必须落在 :data:`SUPPORTED_UNTIL` 里。

    ``run_task`` 与 ``plan_task`` 共用这一份判据：面板的预览说"能跑"、真跑起来却说
    "落点不支持"，是最难解释的一类不一致。
    """
    if until not in SUPPORTED_UNTIL:
        raise StudioError(
            f"--until 不支持 {until.value}",
            code=ErrorCode.STATE_TRANSITION_ILLEGAL,
            context={"until": until.value, "supported": [item.value for item in SUPPORTED_UNTIL]},
            remediation="落点只能是 " + " / ".join(item.value for item in SUPPORTED_UNTIL),
        )


@dataclass(frozen=True, slots=True)
class StepReport:
    """流水线走过的一步（留痕：这条命令到底做了什么）。"""

    stage: str
    status_before: str
    status: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status_before": self.status_before,
            "status": self.status,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class PipelineReport:
    """一条流水线跑完的结论（CLI 的 ``--json`` 与测试读的就是它）。"""

    task_id: str
    status_before: str
    status: str
    until: str
    steps: tuple[StepReport, ...]
    final: Path | None
    quality: QualityReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status_before": self.status_before,
            "status": self.status,
            "until": self.until,
            "steps": [step.to_dict() for step in self.steps],
            "final": self.final.as_posix() if self.final else None,
            "quality": self.quality.model_dump(mode="json") if self.quality else None,
        }


def run_task(
    *,
    task_id: str,
    paths: StudioPaths,
    outputs: OutputsConfig,
    connection: sqlite3.Connection,
    until: TaskStatus = TaskStatus.COMPLETED,
    outputs_source: Path | None = None,
    profile_name: str | None = None,
    voice: str | None = None,
    seed: int | None = None,
    subtitle: bool | None = None,
    streaming_render: bool = False,
    on_progress: ProgressSink | None = None,
) -> PipelineReport:
    """把 ``task_id`` 推到 ``until``；返回走过的每一步。

    已经在 ``until`` 或更靠后 ⇒ **原样返回**（不重跑、不报错）：幂等是"断点续跑"的
    前提，一条跑过的命令再跑一次不该把已经渲好的片子再渲一遍。

    :param streaming_render: ``app.yaml → pipeline.streaming_render``（C7，默认关）。
        由调用方读配置传进来，与 ``outputs`` 同一条：服务层不自己去翻 YAML。
    """
    _check_until(until)

    if streaming_render:
        raise StudioError(
            "pipeline.streaming_render=true 在一期没有实现",
            code=ErrorCode.CONFIG_INVALID,
            context={"config": "config/app.yaml → pipeline.streaming_render", "until": until.value},
            remediation=(
                "改回 false：一期以人声实测总时长为**唯一**基准（C12 / C7），"
                "边配音边渲染会让这个基准随未完成的句子漂移；"
                "§04.3.4 把这条能力留到二期场景化之后再评估"
            ),
        )

    tasks = TaskService(connection)
    status_before = tasks.get(task_id).status
    steps: list[StepReport] = []
    final: Path | None = None
    quality: QualityReport | None = None

    def record(stage: str, before: TaskStatus, after: TaskStatus, note: str) -> None:
        steps.append(
            StepReport(
                stage=stage,
                status_before=before.value,
                status=after.value,
                note=note,
            )
        )

    while True:
        task = tasks.get(task_id)
        status = task.status

        # 顺序要紧：**先**判"卡住 / 人工闸"，**再**判"越过 --until"。
        # 反过来的话，一个 `failed` 的任务会因为"不在正向链上"被算成"已经越过终点"，
        # 命令打印成功、任务一步没动 —— 静默的空转比报错难查得多。
        if status in _STUCK:
            raise StudioError(
                f"任务停在 {status.value}，流水线不能自动往下走",
                code=ErrorCode.STATE_TRANSITION_ILLEGAL,
                context={"task_id": task_id, "status": status.value},
                remediation=(
                    "这一步要人工或重试链介入：failed ⇒ 退避重试 / manual_pool；"
                    "editing ⇒ 等改稿循环；discarded / canceled ⇒ 人工捞回"
                ),
            )

        if status is TaskStatus.AWAITING_APPROVAL:
            raise StudioError(
                f"任务 {task_id} 停在确认闸（awaiting_approval），流水线不代按",
                code=ErrorCode.APPROVAL_NOT_PENDING,
                context={"task_id": task_id},
                remediation="在「确认闸」面板决断，或先放行再跑 `studio pipeline run`",
            )

        if _rank(status) >= _rank(until):
            break

        if status in (TaskStatus.PENDING, TaskStatus.DRAFTING):
            _require_script(connection, task_id)
            if status is TaskStatus.PENDING:
                after = tasks.transition(task_id, TaskStatus.DRAFTING, actor="pipeline", reason="流水线接手")
                record("script", status, after.task.status, "已有生效稿件 ⇒ 进入审稿")
            after = tasks.transition(task_id, TaskStatus.REVIEWING, actor="pipeline", reason="稿件已就绪")
            record("review", TaskStatus.DRAFTING, after.task.status, "跳过 LLM 审稿（本命令不写稿）")
            continue

        if status is TaskStatus.REVIEWING:
            after = tasks.transition(
                task_id,
                TaskStatus.QUEUED_VOICE,
                actor="pipeline",
                reason="无人工确认闸，自动放行到配音",
            )
            record("gate", status, after.task.status, "自动放行")
            continue

        if status is TaskStatus.QUEUED_VOICE:
            queued = enqueue_sentences(connection=connection, task_id=task_id, paths=paths)
            after = tasks.transition(task_id, TaskStatus.VOICING, actor="pipeline", reason="配音作业已投递")
            record("voice", status, after.task.status, f"投递 {queued} 句到 voice 池")
            continue

        if status is TaskStatus.VOICING:
            _drain_voice_pool(
                paths=paths,
                connection=connection,
                task_id=task_id,
                voice=voice,
                on_progress=on_progress,
            )
            stage = settle_voice(
                paths=paths,
                connection=connection,
                task_id=task_id,
                outputs=outputs,
                outputs_source=outputs_source,
            )
            _note_voice_degrade(tasks, task_id, stage)
            after = tasks.transition(
                task_id,
                TaskStatus.QUEUED_RENDER,
                actor="pipeline",
                reason="人声母带与时间轴已就绪",
            )
            record("timeline", status, after.task.status, _voice_note(stage))
            continue

        if status is TaskStatus.QUEUED_RENDER:
            tasks.transition(task_id, TaskStatus.RENDERING, actor="pipeline", reason="开始渲染")
            script_text, script_sentences = _script_payload(connection, task_id)
            script_voices = active_script_voices(connection, task_id, paths=paths)
            result = produce_video(
                ProduceRequest(
                    task_id=task_id,
                    text=script_text,
                    sentences=script_sentences,
                    sentence_voices=script_voices,
                    profile_name=profile_name,
                    voice=voice,
                    reuse_voice=True,
                    seed=seed,
                    subtitle=subtitle,
                ),
                paths=paths,
                outputs=outputs,
                outputs_source=outputs_source,
                on_progress=on_progress,
                disabled=disabled_assets(connection),
            )
            final = result.final
            tasks.set_quality(
                task_id,
                _final_quality(tasks.get(task_id).quality, quality_report(result)),
            )
            after = tasks.transition(task_id, TaskStatus.COMPLETED, actor="pipeline", reason="成片已落盘")
            record("render", TaskStatus.RENDERING, after.task.status, result.final.as_posix())
            continue

        raise StudioError(  # pragma: no cover —— 上面已穷举，留作新增状态时的显式失败
            f"流水线不认识状态 {status.value}",
            code=ErrorCode.STATE_TRANSITION_ILLEGAL,
            context={"task_id": task_id, "status": status.value},
            remediation="补 `pipeline_service.run_task` 的分支，别让它静静卡住",
        )

    return PipelineReport(
        task_id=task_id,
        status_before=status_before.value,
        status=tasks.get(task_id).status.value,
        until=until.value,
        steps=tuple(steps),
        final=final,
        quality=quality,
    )


@dataclass(frozen=True, slots=True)
class PipelinePlan:
    """「这条任务交给流水线会怎样」的结论（面板按按钮**之前**显示的那一行）。"""

    task_id: str
    status: str
    until: str
    #: 能不能跑。``False`` 时 ``reason`` 必有内容。
    runnable: bool
    reason: str | None
    #: 能跑时的一句话：从哪儿推到哪儿 / 已经在落点上了
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "until": self.until,
            "runnable": self.runnable,
            "reason": self.reason,
            "note": self.note,
        }


def plan_task(task_id: str, status: TaskStatus, until: TaskStatus = TaskStatus.COMPLETED) -> PipelinePlan:
    """把"这条任务交给流水线会怎样"提前算出来（**只读一个状态，不碰库**）。

    与 :func:`run_task` 的分工：这里没有副作用，判据用的是同一批常量（``_STUCK`` /
    ``_FORWARD``）与同一个 :func:`_check_until`。把判断写两份的话，面板会说"可以跑"、
    而真跑起来第一件事就是报错；反过来更糟：面板说"不能跑"，其实能跑 —— 于是这个按钮
    在真需要它的时候没人敢按。
    """
    _check_until(until)
    if status in _STUCK:
        return PipelinePlan(
            task_id=task_id,
            status=status.value,
            until=until.value,
            runnable=False,
            reason=f"任务停在 {status.value}，流水线不自动往下走",
            note="这一步要人工或重试链介入：failed ⇒ 退避重试 / manual_pool；editing ⇒ 改稿循环",
        )
    if status is TaskStatus.AWAITING_APPROVAL:
        return PipelinePlan(
            task_id=task_id,
            status=status.value,
            until=until.value,
            runnable=False,
            reason="任务停在确认闸（awaiting_approval），流水线不代按",
            note="在「确认闸」放行或退回之后再来",
        )
    if _rank(status) >= _rank(until):
        return PipelinePlan(
            task_id=task_id,
            status=status.value,
            until=until.value,
            runnable=True,
            reason=None,
            note=f"已经在 {until.value} 或更靠后：再点一次不会重跑（幂等）",
        )
    return PipelinePlan(
        task_id=task_id,
        status=status.value,
        until=until.value,
        runnable=True,
        reason=None,
        note=f"从 {status.value} 一路推到 {until.value}",
    )


def _require_script(connection: sqlite3.Connection, task_id: str) -> None:
    """没有生效稿件 ⇒ 报错并说清补救命令（**不去偷偷调 LLM**，理由见模块 docstring）。"""
    if read_active_script(connection, task_id) is None:
        raise StudioError(
            f"任务 {task_id} 还没有生效稿件，流水线不写稿",
            code=ErrorCode.SCRIPT_NOT_FOUND,
            context={"task_id": task_id},
            remediation=f"先跑 `studio script draft --task-id {task_id}`（要 LLM 通道）",
        )


def _script_payload(connection: sqlite3.Connection, task_id: str) -> tuple[str, tuple[str, ...]]:
    """生效稿件的正文与**逐句**（正文给缓存指纹，逐句给配音）。

    两句都给：拼接后的正文是渲染指纹的输入，而配音要的是**句子边界** ——
    在路上重新切句会切出另一个句数（真机 2026-09-21：55 句 vs 58 句）。
    """
    payload = read_active_script(connection, task_id)
    if payload is None:  # pragma: no cover —— 上面已经拦过一次
        raise StudioError(
            f"任务 {task_id} 没有生效稿件",
            code=ErrorCode.SCRIPT_NOT_FOUND,
            context={"task_id": task_id},
            remediation="先跑 `studio script draft`",
        )
    _script, sentences = payload
    return "".join(row.text for row in sentences), tuple(row.text for row in sentences)


# ══════════════════════════════════════════════════════════════════════
# 配音阶段（T2.8）
# ══════════════════════════════════════════════════════════════════════


def _drain_voice_pool(
    *,
    paths: StudioPaths,
    connection: sqlite3.Connection,
    task_id: str,
    voice: str | None,
    on_progress: ProgressSink | None,
) -> None:
    """把这条任务的配音作业**就地干完**（借一条 worker，跑到全部句子定局）。

    为什么编排层自己跑一条 worker
    ----------------------------
    生产里 ``voice`` 池是常驻进程（``workers/run_voice.py``，由 supervisor 拉起），
    但 ``studio pipeline run`` 是一条**一次性命令**：它不该要求"你先把池起起来"。
    就地跑与常驻池**不冲突** —— 认领是单语句原子操作（§03.4.4），谁抢到谁干，
    另一边只是少干一件。

    为什么按库里的状态驱动，而不是 ``worker.run(max_units=N)`` 一次到位
    ------------------------------------------------------------------
    句子失败后进**退避**（``not_before`` 在未来），这时 ``claim`` 返回 ``None`` ——
    和"池空了"长得一模一样。一次到位的话，遇到退避就会当成"干完了"收工，接着
    ``settle_voice`` 报"还没定局"，一条本该成功的任务变成失败。所以这里以
    ``SentenceProgress.is_settled`` 为**唯一**收工判据，空转时才退避重试。

    空转时顺手回收一次过期租约：上一次念到一半崩了留下的 ``synthesizing`` 行，
    只有 sweeper 能救回来，而一期还没有常驻 sweeper（T4.11 只做任务级清扫）。

    空转时还要**等一拍**（池的 ``poll_ms``）。认领的语义是"没有就立刻返回"，外层不等
    就成了忙等 —— 而句子退避最长要等 20 秒，那是几万次认领与几万对
    ``worker.started/stopped`` 日志。等一拍不影响正常节奏：真认出活来的那几轮根本
    走不到这里。

    认领**不按任务过滤**：这里借的是 voice 池的一条 worker，池里有什么活就干什么活 ——
    与常驻池的语义完全一致（谁抢到谁干）。代价是"顺手把另一条卡在 ``voicing`` 的任务
    也念了"，收益是"这台机器上的配音活儿一直在往前推"。真要按任务隔离，得给
    ``JobStore.claim`` 加任务过滤 —— 那是一条四个池共用的契约，而这里没有谁吃亏：
    另一条任务的句子念完了，它的下一次 ``pipeline run`` 照样从 ``settle_voice`` 接着走。
    """
    pools = load_pools_config(paths)
    store = JobStore(connection)
    repo = SentenceRepo(connection)
    worker = PoolWorker(
        pool="voice",
        handler=build_voice_handler(paths=paths, connection=connection, voice=voice),
        pool_config=pools.pools["voice"],
        paths=paths,
        slot=DRAIN_SLOT,
        connection=connection,
        auto_concurrency=pools.auto_concurrency,
    )
    budget_ms = max(store.pool_runtime("voice").backoff_max_ms, DRAIN_MIN_IDLE_MS)
    poll_sec = max(pools.pools["voice"].poll_ms, DRAIN_POLL_FLOOR_MS) / 1000.0
    idle_since: float | None = None

    while True:
        progress = repo.progress(task_id)
        if progress.is_settled:
            return
        if on_progress is not None:
            on_progress("voice", progress.settled, progress.total, "等 voice 池把句子念完")
        report = worker.run(max_units=max(1, progress.outstanding), max_empty_rounds=1)
        if report.units_done > 0:
            idle_since = None
            continue
        store.reclaim_expired(pool="voice")
        if idle_since is None:
            idle_since = monotonic()
        elif (monotonic() - idle_since) * 1000 >= budget_ms:
            raise StudioError(
                f"配音干不完：{progress.outstanding} 句既没定局、也认不到活",
                code=ErrorCode.STATE_TRANSITION_ILLEGAL,
                context={"task_id": task_id, "progress": progress.to_dict(), "idle_ms": budget_ms},
                remediation=(
                    "看 `studio pool status voice` 与系统日志：句子卡在 synthesizing ⇒ "
                    "等租约过期后重跑；作业已是死信 ⇒ `studio pool retry` 重投该任务"
                ),
            )
        sleep(poll_sec)


def _voice_note(stage: VoiceStageReport) -> str:
    """配音收口那一步的留痕（跑完最想知道的就是"母带多长、几句降级了"）。"""
    total = len(stage.timeline.sentences)
    head = f"母带 {stage.voice_master_ms}ms / {total} 句"
    if stage.degraded:
        return f"{head}，其中 {stage.degraded} 句降级为静音"
    return head


def _note_voice_degrade(tasks: TaskService, task_id: str, stage: VoiceStageReport) -> None:
    """配音降级写进 ``quality_json``（§04.3.3 的熔断后行为）。

    **只在全句都降级时写** ``tts_unavailable``：那是"转字幕模式"这个结论本身
    （原文 §3.4：任务不失败，继续产出）。部分降级是"这条片子有几句话没声音"，
    它的留痕在 ``script_sentences.tts_status`` 与 ``jobs.result_json`` 里（逐句、
    面板上点得开）—— 塞进只有一个槽位的 ``degrade_reason`` 只会把"整条片子进了
    字幕模式"这个更重的话挤掉。
    """
    if stage.degraded == 0 or stage.degraded < len(stage.timeline.sentences):
        return
    tasks.set_quality(task_id, QualityReport(degraded=True, degrade_reason=TTS_DEGRADE_REASON))


def _final_quality(before: QualityReport, after: QualityReport) -> QualityReport:
    """出片这一步的 QC 结论 —— **别把配音的降级结论冲掉**。

    ``TaskService.set_quality`` 是整体覆盖（"一次出片就是一份结论"），而出片这一步
    只知道自己那一摊（黑屏 / 720P 保底）。配音那一步记下的"整条片子进了字幕模式"
    如果不带过来，就会被这一次覆盖掉 —— 成片照样出得来，但"这条片子一句人声都没有"
    这件事在 ``quality_json`` 里消失了，而面板正是靠它提示用户的。

    两边都降级时以**出片**的原因为准：``degrade_reason`` 只有一个槽位，而"这条片子
    画面也是降级的"是更靠后的那一步的结论；配音那边的实情仍在逐句状态里。
    """
    if before.degrade_reason == TTS_DEGRADE_REASON and not after.degraded:
        return after.model_copy(update={"degraded": True, "degrade_reason": TTS_DEGRADE_REASON})
    return after
