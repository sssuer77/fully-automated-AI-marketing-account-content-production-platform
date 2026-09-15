"""领域枚举（§03.5.1）——DDL 的 ``CHECK`` 约束在 Python 侧的镜像。

约定
----
**DDL 是唯一真相**：本模块不允许出现迁移 SQL 里没有的取值。
``tests/unit/domain/test_enums.py`` 会把 CHECK 里的字面量解析出来逐项比对，
因此"改了 DDL 忘了改枚举"会立刻变红。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ApprovalDecision",
    "AutoApprovePolicy",
    "Grade",
    "JobStatus",
    "PoolName",
    "PublishStatus",
    "TaskKind",
    "TaskPool",
    "TaskStatus",
    "UnitType",
]


class TaskKind(StrEnum):
    """``tasks.kind``（§03.3.6）。"""

    VIDEO = "video"
    AUDIO_ONLY = "audio_only"
    DRAFT_ONLY = "draft_only"


class TaskStatus(StrEnum):
    """16 态任务状态机（§README.3）。

    状态机**只增不改**：新增状态必须同时补 ``ALLOWED_TRANSITIONS`` 的进出边，
    否则 ``tests/unit/domain/test_state_machine.py`` 的可达性用例会红。
    """

    PENDING = "pending"  # 待写稿（已建任务）
    DRAFTING = "drafting"  # 写稿中（Director + Writer）
    REVIEWING = "reviewing"  # 审稿中（双通道评分）
    EDITING = "editing"  # 改稿中（≤2 轮）
    AWAITING_APPROVAL = "awaiting_approval"  # ★ 唯一人工节点（仅 B 级）
    QUEUED_VOICE = "queued_voice"  # 待配音（句级 job 已入队）
    VOICING = "voicing"  # 配音中（句级续传）
    QUEUED_RENDER = "queued_render"  # 待渲染
    RENDERING = "rendering"  # 渲染中（一期：单遍合成）
    COMPLETED = "completed"  # 成片完成（可发布）
    PUBLISHING = "publishing"  # 发布中
    PUBLISHED = "published"  # 已发布（**真终态**）
    FAILED = "failed"  # 失败（可自动重试）
    MANUAL_POOL = "manual_pool"  # 人工池（反复失败）
    DISCARDED = "discarded"  # 废弃（C 级 / 人工放弃）
    CANCELED = "canceled"  # 已取消


class Grade(StrEnum):
    """稿件分级（§03.5.2）：A ≥ 8.0 / B 5.0–7.9 / C < 5.0。"""

    A = "A"
    B = "B"
    C = "C"


class PoolName(StrEnum):
    """**作业池**身份：``jobs.pool`` / ``pool_settings.pool`` / ``config/pools.yaml``。

    只有 4 个（§01.4.4）；"不在任何池里"是 ``tasks.pool`` 独有的语义，
    用 :class:`TaskPool` 表达，**不要**往这里塞 ``none``。
    """

    DRAFT = "draft"
    VOICE = "voice"
    RENDER = "render"
    PUBLISH = "publish"


class TaskPool(StrEnum):
    """``tasks.pool``：池身份 + ``none``（不在任何池里）。

    ⚠️ T1.4 施工裁定：DDL 的 ``tasks.pool`` CHECK 允许 5 值，
    而 §03.5.1 的 ``PoolName`` 只有 4 值（那是**作业池**身份）。
    两者语义不同，故拆成两个枚举，避免"给作业池塞一个 none"这种脏类型。
    """

    DRAFT = "draft"
    VOICE = "voice"
    RENDER = "render"
    PUBLISH = "publish"
    NONE = "none"


class UnitType(StrEnum):
    """``jobs.unit_type``：一个作业认领的**最小单元**（§03.4）。"""

    TASK = "task"
    SENTENCE = "sentence"
    SCENE = "scene"
    FINAL = "final"
    TOPIC_BATCH = "topic_batch"
    PUBLISH = "publish"


class JobStatus(StrEnum):
    """``jobs.status``（T1.5 队列内核使用）。"""

    PENDING = "pending"
    BLOCKED = "blocked"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    CANCELED = "canceled"


class PublishStatus(StrEnum):
    """``publications.status``（T5.x 发布使用）。"""

    QUEUED = "queued"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"
    CANCELED = "canceled"


class AutoApprovePolicy(StrEnum):
    """分级放行策略（§03.5.2）：配置项 ``config/app.yaml → approval.auto_approve_policy``。"""

    OFF = "off"  # A/B 都等人工（最保守）
    GRADE_A = "grade_a"  # ★ 默认：A 级自动放行，B 级进确认闸
    GRADE_AB = "grade_ab"  # 全自动：A+B 都放行（WebUI"一键全自动"）


class ApprovalDecision(StrEnum):
    """确认闸的人工决断（§04.4.4 REST 上行的 approve / reject / discard）。

    取值直接取 ``approvals.status`` 的 DDL 字面量 ⇒ 它是那一列的**真镜像**，
    可以直接喂给 ``ApprovalRepo.decide(status=...)``。``pending``（还没决断）与
    ``expired``（超时作废）**不在**其中 —— 它们不是按钮，是人看不见的状态。
    """

    APPROVE = "approved"  # ⇒ queued_voice（并署 approved_by）
    REJECT = "rejected"  # ⇒ editing（revision_round + 1；comment 必填）
    DISCARD = "discarded"  # ⇒ discarded
