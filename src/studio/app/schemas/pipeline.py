"""一键出片面板契约（T4.14 延伸 · §04.6.8）。

面板要回答的四个问题，一个模型对一块
------------------------------------
① "这条任务交给流水线会怎样？" ⇒ :class:`PipelineTaskModel`（按按钮**之前**显示的那一块）
② "现在能拿什么参数跑？" ⇒ :class:`PipelineConsoleResponse`（落点下拉 / 音色 / 在跑的那条）
③ "我这条跑得怎么样了？" ⇒ :class:`PipelineJobModel`（阶段 / 进度 / 日志尾巴 / 成片）
④ "参数填错了会怎样？" ⇒ :class:`PipelineJobRequest`（``extra="forbid"`` + 服务端复核）

为什么落点收的是**字符串**而不是枚举
------------------------------------
"哪些落点支持"这件事的真相在 :data:`~studio.services.pipeline_service.SUPPORTED_UNTIL`。
在这里再抄一份枚举，等于把它写成两处 —— 加一个落点时总有一处会忘，而忘了的那一处
不会报错，只会让新落点从面板上消失。所以请求体收字符串，服务端按
:func:`~studio.services.pipeline_job_service.supported_until` 复核：不合法就 422，
并把能用的那些放进 ``context.supported``。

为什么请求体 ``extra="forbid"``
-------------------------------
与渲染 / 合成配置同一条：把 ``until`` 写成 ``untill`` 被静默忽略，人会以为
"已经按新落点跑了"，而实际一路跑到成片 —— 多花的是**真的编码时间**，而面板上
没有任何地方会显示"落点没生效"。

为什么集合字段一律**必填**
--------------------------
``x: list[T]`` 而非 ``Field(default_factory=list)``：后者在 JSON Schema 里既不进
``required`` 也不带 ``default`` ⇒ 生成的 TS 类型是 ``T[] | undefined``，前端被迫
到处 ``?? []``（裁定 135）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PipelineConsoleResponse",
    "PipelineJobModel",
    "PipelineJobRequest",
    "PipelineSubmitResponse",
    "PipelineTaskModel",
    "PipelineUntilOptionModel",
    "PipelineVoiceModel",
]


class PipelineUntilOptionModel(BaseModel):
    """一个可选的落点（下拉框的一行）。

    ``value`` 是喂给 :func:`~studio.services.pipeline_service.run_task` 的那个
    ``TaskStatus``；``label`` / ``description`` 是给人看的。文案由服务端给，
    因为"这个落点意味着什么"只有 ``pipeline_service`` 说得准（``voicing`` 收、
    ``rendering`` 不收，那条理由写在它的模块注释里）。
    """

    value: str
    label: str
    description: str


class PipelineVoiceModel(BaseModel):
    """一个可选的音色（下拉框的一行）。"""

    name: str
    is_default: bool


class PipelineJobModel(BaseModel):
    """一次「一路做到出片」的全部可见状态（面板轮询的就是它）。"""

    id: str
    task_id: str
    until: str
    status: str
    stage: str
    done: int
    total: int
    percent: int
    note: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    #: 走过的每一步（``PipelineReport.steps`` 的形状，跑完才有内容）
    steps: list[dict[str, object]]
    #: 成片路径（``final.mp4``）
    final: str | None
    #: QC 结论（``QualityReport`` 的形状）
    quality: dict[str, object] | None
    error_code: str | None
    error_message: str | None
    remediation: str | None
    logs: list[str]
    request: dict[str, object]


class PipelineTaskModel(BaseModel):
    """「这条任务交给流水线会怎样」—— 按按钮**之前**显示的那一块。

    ``runnable=False`` 时 ``reason`` 必有内容：面板据此把按钮灰掉并把原因写在旁边，
    而不是让人点了之后等一条 409 回来。判据来自
    :func:`~studio.services.pipeline_service.plan_task`（与 ``run_task`` 同一批常量）。
    """

    task_id: str
    title: str
    status: str
    #: 这条结论是**针对哪个落点**算出来的。面板换落点时会重调一次；把落点带回来，
    #: 前端才能判断"我手上这份结论是不是已经过时了"（否则它只能靠猜）。
    until: str
    runnable: bool
    reason: str | None
    #: 能跑时的一句话：从哪儿推到哪儿 / 已经在落点上了
    note: str
    #: 有没有生效稿件。``pending`` / ``drafting`` 的任务缺了它一步都走不了
    #: （``run_task`` 不写稿，见它的模块 docstring）。
    has_script: bool
    #: 这个任务当前有没有在跑的 job（面板据此把按钮换成"看进度"）
    active_job: PipelineJobModel | None


class PipelineConsoleResponse(BaseModel):
    """面板首屏：一次拿全（可选参数 + 在跑的那条 + 最近几条）。"""

    until_options: list[PipelineUntilOptionModel]
    default_until: str
    voices: list[PipelineVoiceModel]
    #: 缺省音色（本机第一个 SAPI 音色）。一个都没有时是 ``None``。
    default_voice: str | None
    #: 本机有没有可用音色。**没有 ⇒ 配音那一步必然失败**，面板要提前说。
    engine_ready: bool
    engine_hint: str | None
    active: PipelineJobModel | None
    jobs: list[PipelineJobModel]
    #: 日志尾巴最多留几行（服务端在裁，面板不必自己猜）
    max_log_lines: int


class PipelineJobRequest(BaseModel):
    """开一条「一路做到出片」。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=64)
    #: 落点。``None`` = 一路到成片（``completed``）。
    until: str | None = None
    voice: str | None = None
    profile: str | None = None
    seed: int | None = None
    #: 要不要烧字幕。``None`` = 听 ``outputs.yaml`` 里的开关（**三态**：传 False 才是
    #: 显式关掉）。用 bool 的话，"面板没传"与"面板传了 false"会变成同一件事，
    #: 于是配置里开着字幕、面板一提交就被顶掉（与 ``RenderJobRequest`` 同一条）。
    subtitle: bool | None = None


class PipelineSubmitResponse(BaseModel):
    """提交的结果。"""

    job: PipelineJobModel
    #: 这次点击**没有**新建一条：同一个任务已经有一条在跑了（连点两次的第二次）。
    #: 面板据此说一句"它还在跑"，而不是让人以为自己开了两条。
    deduped: bool
