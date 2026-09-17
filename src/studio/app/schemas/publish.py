"""发布操作面契约（T5.3 · §06.5.4 / §06.10）。

这一层回答三个问题，一个模型对一块
----------------------------------
① "有哪些发布记录、现在都什么状态？" ⇒ :class:`PublicationList`（六状态计数 + 各若干条）
② "这条现在能不能重试 / 取消？" ⇒ :class:`PublicationView` 的 ``can_*`` 三个布尔
③ "我刚才那一下的结果是什么？" ⇒ :class:`PublishActionResponse`

为什么 ``can_retry`` / ``can_cancel`` / ``can_mark_done`` 由服务端算
-------------------------------------------------------------------
判据（``published`` 不能取消、``manual_required`` 才能标记已处理……）是**服务端**的
状态机规则。发给面板三个布尔，比让前端记住"哪些状态能点"可靠 —— 规则改一次就漏一处，
而漏的那一处会变成"点了按钮报 400"，用户看到的是"这个按钮坏了"。

为什么 ``evidence`` 里的路径**原样**给出
----------------------------------------
它指的是盘上那份截图 / DOM 快照，面板要用它开一个可点开的预览（与渲染面板的
``/render/videos/{name}`` 同一条）。后端只保证"这个字符串是发布器当时写下来的"，
不去猜它现在还在不在 —— 文件可能已经被 GC 收走了，那时面板显示"打不开"才是诚实的。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from studio.db.repositories.publication_repo import (
    CANCELED,
    MANUAL_REQUIRED,
    PUBLISHED,
    PublicationRow,
)

__all__ = [
    "NO_PUBLICATION_HINT",
    "PublicationList",
    "PublicationView",
    "PublishActionRequest",
    "PublishActionResponse",
    "PublishEnqueueRequest",
    "PublishEnqueueResponse",
    "publication_view",
]

#: 一条记录都没有时的提示（面板第一屏直接显示它）
NO_PUBLICATION_HINT: str = (
    "还没有发布记录：任务出片后在发布面板点「确认发布」会投递一条，"
    "或跑 studio publish enqueue --task <任务号>（出厂 publish.enabled=false，"
    "投递的作业会带 PUBLISH_DISABLED 转人工 —— 那是 R14 的不可逆防护，不是故障）"
)


class PublicationView(BaseModel):
    """一条发布记录（``PublicationRow.to_dict()`` 的展示模型）。"""

    id: str
    task_id: str
    platform: str
    account_id: str
    status: str
    title: str
    caption: str = ""
    tags: list[str] = Field(default_factory=list)
    video_path: str
    cover_path: str | None = None
    profile_key: str | None = None
    dry_run: bool = False
    url: str | None = None
    platform_post_id: str | None = None
    published_at: str | None = None
    scheduled_at: str | None = None
    next_metric_at: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    error_code: str | None = None
    error_message: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    #: 三个"现在能做什么"（服务端判据，见模块注释）。
    can_retry: bool = False
    can_cancel: bool = False
    can_mark_done: bool = False


def publication_view(row: PublicationRow) -> PublicationView:
    """行 ⇒ 展示模型（``can_*`` 三个布尔在这里补上）。"""
    payload = row.to_dict()
    terminal = row.status in (PUBLISHED, CANCELED)
    return PublicationView(
        **payload,
        can_retry=not terminal,
        can_cancel=not terminal,
        can_mark_done=row.status == MANUAL_REQUIRED,
    )


class PublicationList(BaseModel):
    """一份发布面板快照（六状态计数 + 各若干条）。"""

    counts: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, list[PublicationView]] = Field(default_factory=dict)
    #: 只有待人工那几条（T5.3 验收点名的 ``GET /api/v1/publish/queue`` 用它）。
    manual_required: list[PublicationView] = Field(default_factory=list)
    hint: str | None = None


class PublishEnqueueRequest(BaseModel):
    """投递请求：把这条任务排进发布池。"""

    platforms: list[str] | None = Field(default=None, description="目标平台；缺省 = 所有启用账号所在的平台")
    account_id: str | None = Field(default=None, description="指定账号；缺省 = 该平台唯一启用的那个")
    dry_run: bool | None = Field(
        default=None, description="演练（走完前七步停在第 ⑥ 步之前）；缺省 = 跟随 publish.yaml"
    )
    scheduled_at: str | None = Field(default=None, description="定时发布时刻（T5.6 用）")


class PublishEnqueueResponse(BaseModel):
    """投递结论。"""

    task_id: str
    platforms: list[str] = Field(default_factory=list)
    queued: int = 0
    skipped: list[str] = Field(default_factory=list)
    missing: bool = False


class PublishActionRequest(BaseModel):
    """人工处置的请求体（重试 / 取消 / 标记已人工处理共用）。"""

    actor: str = Field(default="user", max_length=32)
    actor_ref: str | None = Field(default=None, max_length=64, description="操作人标识（写进留痕）")
    reason: str | None = Field(default=None, max_length=500, description="说明（标记已处理时必填）")


class PublishActionResponse(BaseModel):
    """人工处置的结论。"""

    publication: PublicationView
    action: str
    #: 一句人话：这条动作**到底改了什么**（面板直接显示，不必自己拼）。
    message: str
    job_changed: bool = False
