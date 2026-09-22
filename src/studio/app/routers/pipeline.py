"""一键出片 REST 面（T4.14 延伸 · §04.6.8）。

五个端点对应面板上的五件事
--------------------------
看首屏（``GET /pipeline/console``）、看"这条任务点了会怎样"（``GET /pipeline/tasks/{id}``）、
开一条（``POST /pipeline/jobs``）、看进度（``GET /pipeline/jobs/{id}``）、叫停
（``POST /pipeline/jobs/{id}/cancel``）。

为什么进度是**轮询**而不是 WS 推送
----------------------------------
与渲染面板同一条：WS 通道（``ws/``）是**只读且不碰数据库**的契约，把一个"进程内的任务
进度"塞进去要动 Hub 的协议与快照注册表。而这条链路的进度是**粗粒度**的（配音第 N 句 /
渲染中 / 完成），不是每帧都在动的东西 —— 1 秒一次的轮询完全够，且断线重连天然正确
（重新 GET 一次就是最新状态）。

为什么要有"预览"这个端点
------------------------
这条链路有三条硬规矩：**不写稿**、**不代按确认闸**、**不接 stuck 状态**（``run_task``
的模块 docstring）。把它们做成"点了之后返回 409"，面板就只能等错误回来才说话；而任务号
是用户手输的，敲错一个字符也要等一轮往返才知道。所以先把 :func:`plan_task` 的结论摆出来：
能不能跑、不能跑是为什么、会从哪儿推到哪儿。

**判据与 ``run_task`` 是同一批常量**（``_STUCK`` / ``_FORWARD`` / ``_check_until``），
不是在这里又写一遍 —— 两处判断迟早会分叉，而分叉的表现是"面板说能跑、点了报错"。

为什么提交这一下**不读库**
--------------------------
``POST /jobs`` 是个入队端点：它只登记一条请求就返回，活在工作线程里干。任务号写错了
会变成一条 ``TASK_NOT_FOUND`` 的失败 job，面板照样把它显示出来（错误码已登记 ⇒ 404 语义
在 job 上保留）。提前替用户读一次库意味着请求线程上多一次 IO 与一个 TOCTOU 窗口，
而面板本来就会先调预览 —— 那个端点才是回答"这个任务号对不对"的地方。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from fastapi import APIRouter, Request
from fastapi import Path as PathParam

from studio.app.deps import AppState
from studio.app.schemas.pipeline import (
    PipelineConsoleResponse,
    PipelineJobModel,
    PipelineJobRequest,
    PipelineSubmitResponse,
    PipelineTaskModel,
)
from studio.app.schemas.voice import NO_VOICE_HINT
from studio.core.errors import ErrorCode, StudioError
from studio.domain.enums import TaskStatus
from studio.domain.task_service import TaskService
from studio.services.pipeline_job_service import (
    MAX_LOG_LINES,
    PipelineJob,
    PipelineJobService,
    PipelineRequest,
    supported_until,
)
from studio.services.pipeline_service import plan_task
from studio.services.script_service import read_active_script
from studio.services.voice_service import voice_engine_info

__all__ = ["router"]

router = APIRouter(tags=["pipeline"])

#: job id 的形状（``PipelineJobService`` 生成的就是它，前缀 ``p`` 与渲染的 ``r`` 区分）。
#:
#: **必须写成 `PathParam(pattern=...)`，不能写成 ``job_id: str = "^p[0-9]{4,}$"``** ——
#: 后者只是一个字符串默认值，一个字节的校验都不做（`/jobs/not-a-job` 会一路进到服务里）。
_JOB_ID = PathParam(pattern=r"^p[0-9]{4,}$")

#: 任务 id 的形状。与 ``routers/voice.py`` 的 ``_TASK_ID`` 同一条：**不能只认 ULID**，
#: 任务号是调用方起的名（渲染面板默认给 ``ui-20260915-120000``），按 ``[0-9A-Za-z]``
#: 卡的话面板自己创建的任务号一进来就整屏 422（陷阱 #122）。
_TASK_ID = PathParam(pattern=r"^[0-9A-Za-z_-]{1,64}$")

#: 落点的人话（下拉框的第二、三列）。
#:
#: 键**必须覆盖** :func:`supported_until` 的全部取值 —— 有一条契约测试盯着
#: （``tests/integration/test_pipeline_api.py``）。取不到时退回枚举值本身（下拉框还在，
#: 只是那一行没有说明），而不是让整个首屏 500：为了一个文案把面板打死是不划算的。
_UNTIL_TEXT: Final[Mapping[str, tuple[str, str]]] = {
    "reviewing": ("推到审稿", "稿件就绪、进确认闸之前（**不写稿**：没有生效稿件会报错）"),
    # ⚠️ 这两行的文案是**落点语义**，不是"做了什么事"：落点 = "到了这儿就停"。
    # `queued_voice` 是**停**在待配音（这一步还不投递），投递发生在它推到 `voicing`
    # 的那一下。写反了会让人以为"选了 queued_voice 就会投递"，而任务本来就在
    # queued_voice 时，那条命令一步都不会走（`_rank` 判它已越过落点）—— 真机上
    # 的表现是"点了投递、什么都没发生"，用户只能去面板上一颗一颗点「重配」。
    "queued_voice": ("推到待配音", "落到 queued_voice 就停 —— 这一步**还不投递**"),
    "voicing": ("投递配音作业", "把句子排进 voice 池、推到 voicing；念不念由常驻池决定"),
    "queued_render": ("配音 + 母带", "把配音念完、母带拼好，停在待渲染（不成片）"),
    "completed": ("一路出成片", "默认：配音 → 母带 → 渲染 → final.mp4"),
}


def _service(state: AppState) -> PipelineJobService:
    """取**进程内**的一键出片服务。

    必须是 ``AppState`` 上那一份（不是每次现造）：任务登记表与工作线程都是内存状态，
    现造一份等于"刚提交的任务下一次请求就查不到了"。与渲染面板同一条。
    """
    return state.pipeline_jobs


def _resolve_until(value: str | None) -> TaskStatus:
    """面板上的落点字符串 ⇒ ``TaskStatus``。

    不合法 ⇒ **422**（``VALIDATION_FAILED``，已登记）并把能用的那些放进
    ``context.supported``：落点是**入参**，写错了改一个字符串重提交就好，
    不是"状态冲突"。也**不能**用 ``STATE_TRANSITION_ILLEGAL`` —— 那个码在这里
    会让人以为是任务状态不对，去查任务号。
    """
    if value is None or value == "":
        return TaskStatus.COMPLETED
    supported = supported_until()
    if value not in supported:
        raise StudioError(
            f"落点不支持：{value}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"until": value, "supported": list(supported)},
            remediation="落点只能是 " + " / ".join(supported),
        )
    return TaskStatus(value)


def _job_dict(job: PipelineJob | None) -> dict[str, object] | None:
    """``PipelineJob | None`` ⇒ ``dict | None``（``None`` 原样传下去，别编一个空任务）。"""
    return None if job is None else job.to_dict()


def _job_model(job: PipelineJob) -> PipelineJobModel:
    return PipelineJobModel.model_validate(job.to_dict())


@router.get("/api/v1/pipeline/console", response_model=PipelineConsoleResponse)
def get_console(request: Request) -> PipelineConsoleResponse:
    """面板首屏：一次拿全（可选落点 / 可选音色 / 在跑的那条 / 最近几条）。"""
    state: AppState = request.app.state.studio
    service = _service(state)
    # 引擎与音色**同一个真相源**（与配音池装配那份判据同源，裁定 310）：面板上写着
    # "引擎 cosyvoice2 / 音色 bigbear"，池子就必须拿这两个去念；两处各算一次，
    # 就会出现「面板说 bigbear 能念、池子把它交给 SAPI」（陷阱 #154）。
    engine = voice_engine_info(state.connections.get(), paths=state.paths)

    return PipelineConsoleResponse.model_validate(
        {
            "until_options": [
                {
                    "value": value,
                    "label": _UNTIL_TEXT.get(value, (value, ""))[0],
                    "description": _UNTIL_TEXT.get(value, (value, ""))[1],
                }
                for value in supported_until()
            ],
            "default_until": TaskStatus.COMPLETED.value,
            "voices": [{"name": name, "is_default": index == 0} for index, name in enumerate(engine.voices)],
            "default_voice": engine.voices[0] if engine.voices else None,
            "engine": engine.name,
            "engine_ready": engine.ready,
            "engine_hint": engine.hint or (None if engine.ready else NO_VOICE_HINT),
            "active": _job_dict(service.active()),
            "jobs": [_job_dict(job) for job in service.recent(limit=10)],
            "max_log_lines": MAX_LOG_LINES,
        }
    )


@router.get("/api/v1/pipeline/tasks/{task_id}", response_model=PipelineTaskModel)
def get_task(request: Request, task_id: str = _TASK_ID, until: str | None = None) -> PipelineTaskModel:
    """这条任务现在在哪一步、点了会怎样（**面板按按钮之前**显示的就是它）。

    ``until`` 可给可不给：面板换落点时重调一次，那一行字就跟着变（"从 voicing 推到
    completed"和"已经在 completed 上了"是两句话）。
    """
    state: AppState = request.app.state.studio
    connection = state.connections.get()
    # 任务不存在 ⇒ ``TaskNotFound``（``TASK_NOT_FOUND`` 已登记 ⇒ 404）。**不吞**：
    # 面板要把"这个任务号不存在"和"这个任务现在不能跑"分成两句不同的话。
    task = TaskService(connection).get(task_id)
    plan = plan_task(task_id, task.status, _resolve_until(until))

    return PipelineTaskModel.model_validate(
        {
            **plan.to_dict(),
            "title": task.title,
            # 没有生效稿件 ⇒ ``pending`` / ``drafting`` 的任务一步都走不了（流水线不写稿）。
            # 提前说出来，比让人点一下再看 409 有用。
            "has_script": read_active_script(connection, task_id) is not None,
            "active_job": _job_dict(_service(state).active_for(task_id)),
        }
    )


@router.post("/api/v1/pipeline/jobs", response_model=PipelineSubmitResponse)
def create_job(request: Request, body: PipelineJobRequest) -> PipelineSubmitResponse:
    """开一条「一路做到出片」（**立刻返回**，活在工作线程里跑）。

    **同一个任务已有在跑的 job ⇒ 原样返回那一条**（服务层的幂等），此时 ``deduped=true``：
    连点两次"开始出片"是很自然的动作，面板据此说一句"它还在跑"，而不是让人以为自己开了两条。
    """
    state: AppState = request.app.state.studio
    service = _service(state)
    # 先看一眼有没有在跑的，用来区分"新建"与"复用了那一条"。两次调用之间有极小的
    # 竞争窗口，但**竞争的结果是安全的**：``submit`` 自己也会再判一次并返回同一条，
    # 最多是 ``deduped`` 这个布尔值差一位 —— 它只影响面板上的一句话。
    existing = service.active_for(body.task_id)
    job = service.submit(
        PipelineRequest(
            task_id=body.task_id,
            until=_resolve_until(body.until),
            voice=body.voice,
            profile_name=body.profile,
            seed=body.seed,
            subtitle=body.subtitle,
        )
    )
    return PipelineSubmitResponse.model_validate(
        {"job": job.to_dict(), "deduped": existing is not None and existing.id == job.id}
    )


@router.get("/api/v1/pipeline/jobs/{job_id}", response_model=PipelineJobModel)
def get_job(request: Request, job_id: str = _JOB_ID) -> PipelineJobModel:
    """查一条的进度 / 结果（面板轮询的就是它）。"""
    job = _service(request.app.state.studio).get(job_id)
    if job is None:
        raise _no_such_job(job_id)
    return _job_model(job)


@router.post("/api/v1/pipeline/jobs/{job_id}/cancel", response_model=PipelineJobModel)
def cancel_job(request: Request, job_id: str = _JOB_ID) -> PipelineJobModel:
    """叫停一条。

    **协作式**：排队中的立刻作废；已经在跑的会在下一次进度回调处停下 —— 检查点只在
    **配音的每一句**与**渲染的每一段**，投递与拼母带那几步之间按取消要等它进到下一个
    回调点。这一点由服务层写进 ``note``，面板照它显示。
    """
    job = _service(request.app.state.studio).cancel(job_id)
    if job is None:
        raise _no_such_job(job_id)
    return _job_model(job)


def _no_such_job(job_id: str) -> StudioError:
    """登记表里没有这条（两个端点共用一句话，免得两处措辞各写一遍）。"""
    return StudioError(
        f"没有这条出片任务：{job_id}",
        code=ErrorCode.JOB_NOT_FOUND,
        context={"job_id": job_id},
        remediation="登记表在进程内存里：API 重启过，或这条已经滚出最近 50 条",
    )
