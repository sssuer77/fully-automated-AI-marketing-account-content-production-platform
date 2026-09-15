"""确认闸 REST 面的请求 / 响应契约（T4.4 · §04.4.4）。

请求体为什么全部 ``extra="forbid"``
-----------------------------------
确认闸是全流程**唯一**的人工节点，一次误调用就是一条放行或一条废弃。多打一个
字段名（``commnet``）被静默忽略，会让人以为"意见写上了" —— 而它其实没进
``approvals.comment``，下一轮改稿只能瞎改。宁可 422。
"""

from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field

from studio.db.models import ApprovalRow
from studio.services.review_service import ApprovalOutcome, RescueOutcome

__all__ = [
    "ApprovalItem",
    "ApprovalList",
    "ApprovalResult",
    "ApproveBatchBody",
    "ApproveBody",
    "BatchFailure",
    "BatchResult",
    "DiscardBody",
    "RejectBody",
    "RescueBody",
    "RescueResult",
]


class _Body(BaseModel):
    """请求体基类（禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


class ApprovalItem(BaseModel):
    """一条待审 / 已决记录。

    ``decided_by`` 是 ``user`` 还是 ``auto_approve_A`` / ``auto_approve_AB``，
    决定"这个月人工放行了几条"能不能算出来 —— 面板必须显示它，不能只显示状态。
    """

    id: str
    task_id: str
    script_id: str | None = None
    status: str
    grade: str | None = None
    score_total: float | None = None
    revision_round: int = 0
    requested_at: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    comment: str | None = None
    auto_expire_at: str | None = None

    @classmethod
    def from_row(cls, row: ApprovalRow) -> ApprovalItem:
        return cls(**asdict(row))


class ApprovalList(BaseModel):
    """待审 / 已决列表 + 按状态计数（面板顶部的"待审 N 条"）。"""

    approvals: list[ApprovalItem]
    counts: dict[str, int]
    limit: int


class ApproveBody(_Body):
    """确认放行（``awaiting_approval → queued_voice``）。"""

    comment: str | None = None


class RejectBody(_Body):
    """退回改稿（``awaiting_approval → editing``）。``comment`` **必填**。

    这里的 ``min_length=1`` 只挡"完全没填"这个最常见的错；"只填了空格"由服务层
    的 ``APPROVAL_COMMENT_REQUIRED`` 拒绝 —— 业务不变量只有一个权威落点，
    REST 层不抄第二遍语义（§04.4.4 不变量 3）。
    """

    comment: str = Field(min_length=1, description="必填：下一轮改稿只认这条意见")


class DiscardBody(_Body):
    """放弃（``awaiting_approval → discarded``）。可被 :class:`RescueBody` 捞回。"""

    reason: str | None = None


class ApproveBatchBody(_Body):
    """批量通过（原文 §7.2"批量操作"）。

    路径是 ``/api/v1/approvals/approve_batch`` 而**不是** §04.4.4 表里的
    ``/api/v1/tasks/{id}/approve_batch`` —— 后者的 ``{id}`` 在请求体是
    ``task_ids`` 的情况下无处可用（T4.4 施工裁定 124，规格书已同步）。
    """

    task_ids: list[str] = Field(min_length=1, description="要放行的任务；逐条留痕")
    comment: str | None = None


class RescueBody(_Body):
    """捞回（``discarded`` / ``canceled`` → ``pending``）。"""

    reason: str | None = None


class ApprovalResult(BaseModel):
    """一次决断的结果（**没有** ``ok`` 字段：能返回就是成功）。"""

    task_id: str
    approval_id: str
    decision: str
    status: str
    grade: str | None = None
    score_total: float | None = None
    revision_round: int = 0
    comment: str | None = None

    @classmethod
    def from_outcome(cls, outcome: ApprovalOutcome) -> ApprovalResult:
        return cls(**outcome.to_dict())


class BatchFailure(BaseModel):
    """批量里失败的那一条（**不**因为一条失败就回滚其余的）。"""

    task_id: str
    code: str
    message: str
    remediation: str | None = None


class BatchResult(BaseModel):
    """批量通过的逐条结果。

    ``approved`` / ``failed`` 都如实返回：批量是"尽力而为"的人工操作，
    把 9 条成功 + 1 条失败报成"整体失败"，会让人重按一次 —— 而重复放行
    撞上 ``APPROVAL_NOT_PENDING`` 只是噪音，真正的问题是没人知道哪 9 条成了。
    """

    requested: int
    approved: list[ApprovalResult]
    failed: list[BatchFailure]


class RescueResult(BaseModel):
    """一次捞回的结果。"""

    task_id: str
    status: str
    previous_status: str
    revision_round: int = 0
    topic_id: str | None = None
    topic_restored: bool = False

    @classmethod
    def from_outcome(cls, outcome: RescueOutcome) -> RescueResult:
        return cls(**outcome.to_dict())
