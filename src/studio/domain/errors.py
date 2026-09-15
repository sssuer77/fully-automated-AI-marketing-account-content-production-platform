# ruff: noqa: N818 —— 三个异常类的名字由 §03.5.1 冻结（规格书原文即 IllegalTransition /
# ConcurrentModification），改名会让规格书、契约测试与调用方一起漂移，故保留原名。
"""领域异常（§03.5.1）。

命名说明：规格书写的是 ``IllegalTransition(DomainError)``；本项目没有单独的
``DomainError`` 基类，状态机类异常统一挂在 ``core.errors.StateTransitionError``
下（它已登记 ``STATE_TRANSITION_ILLEGAL``），语义一致、错误码不重复造。
"""

from __future__ import annotations

from studio.core.errors import ErrorCode, StateTransitionError

__all__ = ["ConcurrentModification", "IllegalTransition", "TaskNotFound"]


class IllegalTransition(StateTransitionError):
    """非法迁移。

    携带 ``from`` / ``to`` / ``actor`` 供 ``task_events`` 与排障使用
    （DoD 5：禁止静默失败 ⇒ 非法迁移必须抛，不能"就当没发生"）。
    """

    default_code = ErrorCode.STATE_TRANSITION_ILLEGAL


class ConcurrentModification(StateTransitionError):
    """乐观锁冲突：``version`` 已被别的写者推进。

    调用方应**重读 + 重试**，而不是覆盖写（覆盖会丢掉并发方的事件与产物）。
    """

    default_code = ErrorCode.STATE_VERSION_CONFLICT


class TaskNotFound(StateTransitionError):
    """任务不存在（或已被 GC 掉）。"""

    default_code = ErrorCode.TASK_NOT_FOUND
