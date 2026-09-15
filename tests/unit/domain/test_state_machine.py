"""``domain/state_machine.py`` 单测：**16×16 = 256 格逐格覆盖**（§03.5.1）。

期望值是照 §README.3 / §03.5.1 的迁移表**手抄**的一份独立副本 ——
不 import 实现里的矩阵，否则实现写错时测试会跟着一起错。
"""

from __future__ import annotations

from collections import deque

import pytest

from studio.domain.enums import TaskStatus as S
from studio.domain.errors import IllegalTransition
from studio.domain.state_machine import (
    ALLOWED_TRANSITIONS,
    RESCUE_STATUSES,
    RETRY_FROM_WHITELIST,
    RETRY_SOURCES,
    TERMINAL_STATUSES,
    allowed_targets,
    assert_allowed,
    is_allowed,
)

ALL_STATUSES: tuple[S, ...] = tuple(S)

#: §03.5.1 迁移表的手抄副本（**静态半边**；failed/manual_pool 的 retry_from 是动态边）
EXPECTED_STATIC: dict[S, frozenset[S]] = {
    S.PENDING: frozenset({S.DRAFTING, S.DISCARDED, S.CANCELED}),
    S.DRAFTING: frozenset({S.REVIEWING, S.FAILED, S.DISCARDED, S.CANCELED}),
    S.REVIEWING: frozenset(
        {S.EDITING, S.AWAITING_APPROVAL, S.QUEUED_VOICE, S.DISCARDED, S.FAILED, S.CANCELED}
    ),
    S.EDITING: frozenset({S.REVIEWING, S.DISCARDED, S.FAILED, S.CANCELED}),
    S.AWAITING_APPROVAL: frozenset({S.QUEUED_VOICE, S.EDITING, S.DISCARDED, S.CANCELED}),
    S.QUEUED_VOICE: frozenset({S.VOICING, S.FAILED, S.CANCELED}),
    S.VOICING: frozenset({S.QUEUED_RENDER, S.FAILED, S.CANCELED}),
    S.QUEUED_RENDER: frozenset({S.RENDERING, S.FAILED, S.CANCELED}),
    S.RENDERING: frozenset({S.COMPLETED, S.FAILED, S.CANCELED}),
    S.COMPLETED: frozenset({S.PUBLISHING, S.CANCELED}),
    S.PUBLISHING: frozenset({S.PUBLISHED, S.COMPLETED, S.FAILED, S.CANCELED}),
    S.PUBLISHED: frozenset(),
    S.FAILED: frozenset({S.MANUAL_POOL, S.DISCARDED, S.CANCELED}),
    S.MANUAL_POOL: frozenset({S.DISCARDED, S.CANCELED}),
    S.DISCARDED: frozenset({S.PENDING}),
    S.CANCELED: frozenset({S.PENDING}),
}


# ══════════════════════════════════════════════════════════════════════
# 16×16 全矩阵
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("source", ALL_STATUSES, ids=str)
@pytest.mark.parametrize("target", ALL_STATUSES, ids=str)
def test_matrix_cell_without_retry_from(source: S, target: S) -> None:
    """不带 ``retry_from`` 时，256 格全部只能走静态边。"""
    assert is_allowed(source, target) is (target in EXPECTED_STATIC[source])


@pytest.mark.parametrize("source", ALL_STATUSES, ids=str)
@pytest.mark.parametrize("target", ALL_STATUSES, ids=str)
def test_matrix_cell_with_irrelevant_retry_from(source: S, target: S) -> None:
    """``retry_from`` 只对 failed/manual_pool 生效；对别的状态传了也不开新边。"""
    if source in RETRY_SOURCES:
        pytest.skip("failed/manual_pool 的动态边由下面的用例覆盖")
    assert is_allowed(source, target, retry_from=S.VOICING) is (target in EXPECTED_STATIC[source])


def test_matrix_has_all_sixteen_rows() -> None:
    """缺行会让"忘了给新状态配出边"变成 KeyError，必须显式空集。"""
    assert set(ALLOWED_TRANSITIONS) == set(S)
    assert len(ALLOWED_TRANSITIONS) == 16


def test_matrix_is_immutable() -> None:
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[S.PENDING] = frozenset()  # type: ignore[index]


def test_no_self_loops() -> None:
    for source, targets in ALLOWED_TRANSITIONS.items():
        assert source not in targets, f"{source} 不该有自环"


def test_published_is_terminal() -> None:
    """发布不可逆：``published`` 没有任何出边（连人工都没有）。"""
    assert not ALLOWED_TRANSITIONS[S.PUBLISHED]
    assert set(TERMINAL_STATUSES) == {S.PUBLISHED}
    for target in ALL_STATUSES:
        assert not is_allowed(S.PUBLISHED, target)


def test_every_status_reachable_from_pending() -> None:
    """可达性守卫：新增状态却没接进图 ⇒ 这里红。"""
    seen = {S.PENDING}
    queue = deque([S.PENDING])
    while queue:
        for target in ALLOWED_TRANSITIONS[queue.popleft()]:
            if target not in seen:
                seen.add(target)
                queue.append(target)
    assert seen == set(S), f"从 pending 到不了：{sorted(set(S) - seen)}"


# ══════════════════════════════════════════════════════════════════════
# failed / manual_pool 的动态边
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("source", sorted(RETRY_SOURCES, key=str), ids=str)
@pytest.mark.parametrize("target", sorted(RETRY_FROM_WHITELIST, key=str), ids=str)
def test_retry_edge_requires_matching_retry_from(source: S, target: S) -> None:
    assert not is_allowed(source, target)  # 没带 retry_from ⇒ 拒绝
    assert is_allowed(source, target, retry_from=target)  # 带了且等于目标 ⇒ 放行
    assert not is_allowed(source, target, retry_from=S.PENDING)  # retry_from 非白名单 ⇒ 拒绝


@pytest.mark.parametrize("source", sorted(RETRY_SOURCES, key=str), ids=str)
def test_retry_edge_cannot_jump_elsewhere(source: S) -> None:
    """``retry_from=voicing`` 不能成为去 ``completed`` / ``pending`` 的后门。"""
    for target in ALL_STATUSES:
        if target in RETRY_FROM_WHITELIST:
            continue
        assert is_allowed(source, target, retry_from=S.VOICING) is (target in EXPECTED_STATIC[source])


def test_retry_whitelist_contents() -> None:
    """白名单 = "确实有活可干"的 8 个状态（§03.5.1）。"""
    assert set(RETRY_FROM_WHITELIST) == {
        S.DRAFTING,
        S.REVIEWING,
        S.EDITING,
        S.QUEUED_VOICE,
        S.VOICING,
        S.QUEUED_RENDER,
        S.RENDERING,
        S.PUBLISHING,
    }
    forbidden = {
        S.PENDING,
        S.AWAITING_APPROVAL,
        S.COMPLETED,
        S.PUBLISHED,
        S.FAILED,
        S.MANUAL_POOL,
        S.DISCARDED,
        S.CANCELED,
    }
    assert not (RETRY_FROM_WHITELIST & forbidden)
    assert not (RETRY_SOURCES & RETRY_FROM_WHITELIST)


def test_rescue_statuses_go_back_to_pending() -> None:
    assert set(RESCUE_STATUSES) == {S.DISCARDED, S.CANCELED}
    for status in RESCUE_STATUSES:
        assert set(ALLOWED_TRANSITIONS[status]) == {S.PENDING}


# ══════════════════════════════════════════════════════════════════════
# allowed_targets / assert_allowed
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("source", ALL_STATUSES, ids=str)
def test_allowed_targets_agrees_with_is_allowed(source: S) -> None:
    for retry_from in (None, S.VOICING):
        targets = allowed_targets(source, retry_from=retry_from)
        for target in ALL_STATUSES:
            assert (target in targets) is is_allowed(source, target, retry_from=retry_from)


@pytest.mark.parametrize("source", ALL_STATUSES, ids=str)
def test_allowed_targets_without_retry_from_is_static(source: S) -> None:
    assert allowed_targets(source) == EXPECTED_STATIC[source]


def test_assert_allowed_passes_silently() -> None:
    assert_allowed(S.PENDING, S.DRAFTING, actor="system")
    assert_allowed(S.FAILED, S.VOICING, actor="user", retry_from=S.VOICING)


def test_assert_allowed_raises_with_breakdown() -> None:
    with pytest.raises(IllegalTransition) as excinfo:
        assert_allowed(S.PENDING, S.RENDERING, actor="worker:draft#1")
    error = excinfo.value
    assert error.context["from"] == "pending"
    assert error.context["to"] == "rendering"
    assert error.context["actor"] == "worker:draft#1"
    assert error.context["allowed"] == ["canceled", "discarded", "drafting"]
    assert error.remediation is not None


def test_assert_allowed_reports_retry_hint() -> None:
    with pytest.raises(IllegalTransition) as excinfo:
        assert_allowed(S.FAILED, S.COMPLETED, actor="system", retry_from=S.VOICING)
    assert excinfo.value.context["retry_from"] == "voicing"
    assert "voicing" in str(excinfo.value.context["allowed"])
