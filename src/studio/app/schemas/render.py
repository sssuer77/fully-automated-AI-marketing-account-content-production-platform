"""渲染面板契约（T4.6 · §04.2.8）。

面板要回答的四个问题，一个模型对一块
------------------------------------
① "现在能拿什么参数跑？" ⇒ :class:`RenderConsoleResponse`（profile 列表 / 音色 / 引擎）
② "我这条跑得怎么样了？" ⇒ :class:`RenderJobModel`（状态 / 阶段 / 进度 / 日志尾巴）
③ "跑出来什么了？" ⇒ :class:`RenderVideoModel`（成片列表 + 可直接播的 url）
④ "参数填错了会怎样？" ⇒ :class:`RenderJobRequest`（``extra="forbid"`` + 服务端复核）

为什么文案与任务号**可以二选一**
--------------------------------
"给一段文案直接出片"和"拿库里某一版稿子出片"是同一件事的两个入口。分成两个端点会
让两条路径各写一遍"文案从哪来"，而它们的差异正是"面板说用的这版稿、实际读的是另一版"
的来源。所以一个端点，规则写在一处：``text`` 非空就用它，否则按 ``task_id`` 读生效稿件。

为什么请求体 ``extra="forbid"``
-------------------------------
与合成配置 / 总览台同一条：把 ``profile`` 写成 ``profle`` 被静默忽略，人会以为
"已经用新 profile 跑了"，而实际用的是默认档 —— 出来的片子分辨率不对，且没人知道为什么。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_SPEECH_CHARS",
    "RenderConsoleResponse",
    "RenderJobModel",
    "RenderJobRequest",
    "RenderProfileOptionModel",
    "RenderVideoModel",
    "RenderVoiceModel",
]

#: 口播文案的长度上限。一场 3 分钟的片子约 700–900 字；给到 5000 是"够用且能挡住
#: 误把整篇文档粘进来"，而不是一个业务约束（配音时长本身由切分与合成决定）。
MAX_SPEECH_CHARS: int = 5000


class RenderProfileOptionModel(BaseModel):
    """一档可选的合成 profile（下拉框的一行）。"""

    name: str
    width: int
    height: int
    fps: int
    vcodec: str
    quality_field: str
    quality: int
    platforms: list[str]
    is_default: bool


class RenderVoiceModel(BaseModel):
    """一个可选的音色（下拉框的一行）。"""

    name: str
    is_default: bool


class RenderVideoModel(BaseModel):
    """盘上的一支成片。``url`` 直接喂 ``<video src>``（浏览器自己发范围请求）。"""

    name: str
    url: str
    size_bytes: int
    modified_at: float


class RenderJobModel(BaseModel):
    """一次出片任务的全部可见状态。"""

    id: str
    task_id: str
    status: str
    stage: str
    done: int
    total: int
    percent: int
    note: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    error_code: str | None
    error_message: str | None
    remediation: str | None
    result: dict[str, object] | None
    logs: list[str]
    request: dict[str, object]


class RenderConsoleResponse(BaseModel):
    """面板首屏：一次拿全（可选参数 + 在跑的任务 + 最近任务 + 成片列表）。

    集合字段一律**必填**（``x: list[T]`` 而非 ``Field(default_factory=list)``）：
    后者在 JSON Schema 里既不进 ``required`` 也不带 ``default`` ⇒ 生成类型是
    ``T[] | undefined``，前端被迫到处 ``?? []``（裁定 135）。
    """

    engine: str
    engine_ready: bool
    engine_hint: str | None
    profiles: list[RenderProfileOptionModel]
    default_profile: str
    voices: list[RenderVoiceModel]
    running: RenderJobModel | None
    jobs: list[RenderJobModel]
    videos: list[RenderVideoModel]
    max_speech_chars: int
    #: 水印这次贴不贴（面板顶部要说清楚，避免"为什么我的片子没水印"）
    watermark_enabled: bool
    watermark_hint: str | None
    #: 字幕这次烧不烧（同上：字体缺失时会被跳过，得让面板说得出来）
    subtitle_enabled: bool
    subtitle_hint: str | None
    #: 人物贴图这次会贴上**哪几层**（槽位名）。空列表 ⇒ 一层都不贴。
    #: 与水印不同，这里给的是**名单**而不是一个布尔：贴图是若干层，
    #: "开了三层只上了一层"是必须一眼看得见的状态。
    stickers_applied: list[str]
    stickers_hint: str | None


class RenderJobRequest(BaseModel):
    """开一条出片任务。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=MAX_SPEECH_CHARS)
    profile: str | None = None
    voice: str | None = None
    reuse_voice: bool = False
    seed: int | None = None
    threads: int | None = Field(default=None, ge=1, le=64)
    #: 要不要烧字幕。``None`` = 听 ``outputs.yaml`` 里的开关（**三态**：传 False 才是
    #: 显式关掉）。用 bool 的话，"面板没传"与"面板传了 false"会变成同一件事，
    #: 于是配置里开着字幕、面板一提交就被顶掉。
    subtitle: bool | None = None
