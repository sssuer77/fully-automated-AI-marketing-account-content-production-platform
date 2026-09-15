"""审计页的响应契约（T4.12 · §04.5.11）。

审计页要回答的三个问题，一个模型对一块
--------------------------------------
① "谁在什么时候动了什么？" ⇒ :class:`AuditOpModel`（含 ``before`` / ``after``，
   留痕的全部意义就是这两块 —— 只给一句"改过了"等于没留）；
② "还有多少条没看到？" ⇒ :class:`AuditPage.total`（翻页得有个头，否则
   前端只能靠"这一页满没满"猜）；
③ "筛选下拉里有哪些选项？" ⇒ :class:`AuditFacets`（要"按操作人筛"，
   得先知道有哪几个操作人 —— 让人手打 id 是另一种形式的"查不到"）。

为什么 ``at`` 是可空的
----------------------
DDL 给了默认值，理论上不会空；但历史行（或外部工具写进来的行）可能没有它。
把它标成必填 ⇒ 一条脏行能让**整页** 500，而审计页最不该"打不开"。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

__all__ = [
    "AuditFacets",
    "AuditOpModel",
    "AuditPage",
]


class AuditOpModel(BaseModel):
    """一条操作留痕（``audit_ops`` 的展示面 · §03.3.8）。"""

    id: int
    at: str | None = None
    actor: str
    actor_ref: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    task_id: str | None = None
    before: dict[str, Any]
    after: dict[str, Any]
    result: str
    reason: str | None = None
    source: str
    ip: str | None = None
    request_id: str | None = None


class AuditPage(BaseModel):
    """一页留痕（``total`` 是**同一组筛选下**的总数，不是全表总数）。"""

    total: int
    limit: int
    offset: int
    items: list[AuditOpModel]


class AuditFacets(BaseModel):
    """筛选下拉的取值（按动作 / 对象 / 操作人各一份）。"""

    actors: list[str]
    actions: list[str]
    target_types: list[str]
    results: list[str]
