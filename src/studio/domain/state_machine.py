"""16 态任务状态机（§03.5.1 · §README.3）。

本模块是**纯函数**：不碰数据库、不碰时钟，因此 16×16 全矩阵可以在单测里
逐格覆盖（``tests/unit/domain/test_state_machine.py``）。

两类出边
--------
1. **静态边**：:data:`ALLOWED_TRANSITIONS` 里写死的目标，任何情况都放行；
2. **动态边**：``failed`` / ``manual_pool`` 的"回到 ``retry_from``" ——
   目标必须**等于**任务自己的 ``retry_from``，且 ``retry_from`` 必须落在
   :data:`RETRY_FROM_WHITELIST` 白名单内（防止重试把任务送回非法位置）。

判定唯一入口是 :func:`is_allowed`；别处不要再写第二份 if-else。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from studio.domain.enums import TaskStatus
from studio.domain.errors import IllegalTransition

__all__ = [
    "ALLOWED_TRANSITIONS",
    "RESCUE_STATUSES",
    "RETRY_FROM_WHITELIST",
    "RETRY_SOURCES",
    "TERMINAL_STATUSES",
    "allowed_targets",
    "assert_allowed",
    "is_allowed",
]

_S = TaskStatus

#: 静态迁移矩阵。**16 行必须全部出现**（无出边者用空集，而不是缺键）——
#: 缺键会让"忘了给新状态配出边"变成运行时 KeyError，而不是编译期可见的空集。
ALLOWED_TRANSITIONS: Final[Mapping[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {
        _S.PENDING: frozenset({_S.DRAFTING, _S.DISCARDED, _S.CANCELED}),
        _S.DRAFTING: frozenset({_S.REVIEWING, _S.FAILED, _S.DISCARDED, _S.CANCELED}),
        _S.REVIEWING: frozenset(
            {
                _S.EDITING,
                _S.AWAITING_APPROVAL,
                _S.QUEUED_VOICE,  # A 级自动放行
                _S.DISCARDED,  # C 级
                _S.FAILED,
                _S.CANCELED,
            }
        ),
        _S.EDITING: frozenset({_S.REVIEWING, _S.DISCARDED, _S.FAILED, _S.CANCELED}),
        _S.AWAITING_APPROVAL: frozenset(
            {
                _S.QUEUED_VOICE,  # 确认
                _S.EDITING,  # 退回（revision_round + 1）
                _S.DISCARDED,  # 放弃
                _S.CANCELED,
            }
        ),
        _S.QUEUED_VOICE: frozenset({_S.VOICING, _S.FAILED, _S.CANCELED}),
        _S.VOICING: frozenset({_S.QUEUED_RENDER, _S.FAILED, _S.CANCELED}),
        _S.QUEUED_RENDER: frozenset({_S.RENDERING, _S.FAILED, _S.CANCELED}),
        _S.RENDERING: frozenset({_S.COMPLETED, _S.FAILED, _S.CANCELED}),
        _S.COMPLETED: frozenset({_S.PUBLISHING, _S.CANCELED}),
        _S.PUBLISHING: frozenset(
            {
                _S.PUBLISHED,
                _S.COMPLETED,  # 发布失败但成片有效 ⇒ 回落（不回退产物）
                _S.FAILED,
                _S.CANCELED,
            }
        ),
        _S.PUBLISHED: frozenset(),  # 真终态：发布不可逆
        # 下面两行只有"静态半边"，"回到 retry_from"是动态边（见 is_allowed）
        _S.FAILED: frozenset({_S.MANUAL_POOL, _S.DISCARDED, _S.CANCELED}),
        _S.MANUAL_POOL: frozenset({_S.DISCARDED, _S.CANCELED}),
        _S.DISCARDED: frozenset({_S.PENDING}),  # 人工"捞回"重新走流程
        _S.CANCELED: frozenset({_S.PENDING}),  # 人工恢复
    }
)

#: 可以成为"断点重试落点"的状态白名单（§03.5.1）。
#: 判据：该状态**确实有活可干**，回去能续上；纯终态 / 纯人工态不在此列。
RETRY_FROM_WHITELIST: Final[frozenset[TaskStatus]] = frozenset(
    {
        _S.DRAFTING,
        _S.REVIEWING,
        _S.EDITING,
        _S.QUEUED_VOICE,
        _S.VOICING,
        _S.QUEUED_RENDER,
        _S.RENDERING,
        _S.PUBLISHING,
    }
)

#: 拥有"动态出边"的状态（``failed`` / ``manual_pool``）。
RETRY_SOURCES: Final[frozenset[TaskStatus]] = frozenset({_S.FAILED, _S.MANUAL_POOL})

#: 真终态：没有任何出边（含人工）。
TERMINAL_STATUSES: Final[frozenset[TaskStatus]] = frozenset({_S.PUBLISHED})

#: 人工态：只能人工干预（"捞回"到 ``pending``）。
RESCUE_STATUSES: Final[frozenset[TaskStatus]] = frozenset({_S.DISCARDED, _S.CANCELED})


def _is_retry_edge(source: TaskStatus, target: TaskStatus, retry_from: TaskStatus | None) -> bool:
    """动态边判定：``failed`` / ``manual_pool`` 回到 ``retry_from``。"""
    if source not in RETRY_SOURCES or retry_from is None:
        return False
    if retry_from not in RETRY_FROM_WHITELIST:
        return False
    return target == retry_from


def is_allowed(
    source: TaskStatus,
    target: TaskStatus,
    *,
    retry_from: TaskStatus | None = None,
) -> bool:
    """16×16 迁移矩阵的唯一判定入口。

    :param retry_from: 任务当前的 ``tasks.retry_from``（仅 ``failed`` /
        ``manual_pool`` 用得上；其余状态传了也不影响）
    """
    if target in ALLOWED_TRANSITIONS[source]:
        return True
    return _is_retry_edge(source, target, retry_from)


def allowed_targets(
    source: TaskStatus,
    *,
    retry_from: TaskStatus | None = None,
) -> frozenset[TaskStatus]:
    """``source`` 当前可去往的全部目标（含动态边）。"""
    static = ALLOWED_TRANSITIONS[source]
    if source in RETRY_SOURCES and retry_from is not None and retry_from in RETRY_FROM_WHITELIST:
        return static | {retry_from}
    return static


def assert_allowed(
    source: TaskStatus,
    target: TaskStatus,
    *,
    actor: str,
    retry_from: TaskStatus | None = None,
) -> None:
    """不合法即抛 :class:`IllegalTransition`（携带 from/to/actor 供排障）。"""
    if is_allowed(source, target, retry_from=retry_from):
        return
    hint = ""
    if source in RETRY_SOURCES:
        hint = f"；合法回退目标 = {sorted(t.value for t in RETRY_FROM_WHITELIST)}"
    raise IllegalTransition(
        f"非法状态迁移：{source} → {target}",
        context={
            "from": source.value,
            "to": target.value,
            "actor": actor,
            "retry_from": retry_from.value if retry_from is not None else None,
            "allowed": sorted(t.value for t in allowed_targets(source, retry_from=retry_from)),
        },
        remediation=f"先查 §README.3 状态机图{hint}",
    )
