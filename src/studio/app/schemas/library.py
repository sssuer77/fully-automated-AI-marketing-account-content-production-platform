"""成片库 REST 形状（T5.11 · §06.11）。

两个模型之间**没有**第二个真相源
--------------------------------
``publications`` 直接嵌 :class:`~studio.app.schemas.publish.PublicationView` ——
发布状态、``can_retry`` / ``can_cancel`` 那几个布尔、以及失败原因，全都只有一份定义。
在成片库里另起一个"精简版"的代价是：发布面板上写着"可以重试"，而成片库里那一行
说"不行"，两边各自成立，而**没有任何地方会报错**。

集合字段一律**必填**（``x: list[T]`` 而非 ``Field(default_factory=list)``）：
后者在 JSON Schema 里既不进 ``required`` 也不带 ``default`` ⇒ 生成类型是
``T[] | undefined``，前端被迫到处 ``?? []``（裁定 135）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from studio.app.schemas.publish import PublicationView
from studio.services.library_service import BATCH_MAX

__all__ = [
    "LibraryBatchItemView",
    "LibraryItemView",
    "LibraryPublishRequest",
    "LibraryPublishResponse",
    "LibraryResponse",
]

#: 一条成片都没有时面板说什么。**带上下一步去哪**：空列表本身不是问题，
#: "我该干什么"才是。
NO_FINAL_HINT = "data/output/videos/ 里还没有成片 —— 先去「一键出片」或「渲染」那一屏出一条。"


class LibraryItemView(BaseModel):
    """成片库里的一行（= 一条任务，见 ``services/library_service.py`` 的模块注释）。"""

    task_id: str
    video_name: str
    #: 在线播放用的地址（复用渲染面板那条，不另开一个读文件的口子）。
    video_url: str
    video_path: str
    size_bytes: int
    modified_at: float
    #: 这条任务在盘上有几支成片。**发布时用的不是"你选的那一支"**（发布池认任务号），
    #: 所以这个数字只是"它重出过几版"的交代。
    versions: int
    #: ``tasks`` 里那一行还在吗。``false`` ⇒ 这条发不了（发布池要读任务）。
    task_found: bool
    title: str
    task_status: str | None
    duration_ms: int | None
    publications: list[PublicationView]


class LibraryResponse(BaseModel):
    """成片库首屏。"""

    items: list[LibraryItemView]
    #: 这次回了几行 / 上限是多少（面板据此说"还有更多"）。
    total: int
    limit: int
    #: 一条都没有时的提示（有就不给，免得盖住内容）。
    hint: str | None = None


class LibraryPublishRequest(BaseModel):
    """批量投递：把勾中的这几条任务排进发布池。"""

    model_config = ConfigDict(extra="forbid")

    task_ids: list[str] = Field(min_length=1, max_length=BATCH_MAX)
    #: 目标平台；缺省 = 所有启用账号所在的平台（与单条投递同一条判据）。
    platforms: list[str] | None = None
    #: 演练（走完前七步停在第 ⑥ 步之前）；缺省 = 跟随 ``publish.yaml``。
    dry_run: bool | None = None


class LibraryBatchItemView(BaseModel):
    """批量投递里的一条结论。"""

    task_id: str
    queued: int
    #: 没投出去的目标及原因（``"douyin：平台未启用"``）。
    skipped: list[str]
    #: 其中"早就投过"的那些（幂等命中）。**处置动作是"什么都不用做"**，
    #: 与其余跳过分开，免得操作员去改一个本来就没问题的配置。
    duplicates: list[str]
    missing: bool


class LibraryPublishResponse(BaseModel):
    """一次批量投递的总账（``items`` 与 ``task_ids`` **同序同长**）。"""

    items: list[LibraryBatchItemView]
    platforms: list[str]
    queued_total: int
    task_total: int
