"""`GET /api/v1/health` 的响应契约（T4.1 起有类型）。

为什么原来没有、现在必须有
--------------------------
T1.7 的 health 返回 ``dict[str, Any]`` —— 类型是 ``additionalProperties: true``，
OpenAPI 里就只是一个空壳对象。T4.1 要求"**API 类型由 OpenAPI 自动生成，禁止手写
接口类型**"：如果响应本身没类型，生成出来的 TS 就全是 ``unknown``，
这条约束就变成了走过场。所以把两个已有端点补上响应模型。
"""

from __future__ import annotations

from pydantic import BaseModel

__all__ = ["HealthResponse", "WsClientStat", "WsStats"]


class WsClientStat(BaseModel):
    """一条 WS 连接的画像（`Hub.stats()["clients"]` 的一项）。"""

    conn_id: str
    sent: int
    seq: int
    backlog: int
    dropped: int


class WsStats(BaseModel):
    """Hub 运行态（背压面板 / 兜底探针都读它）。"""

    connections: int
    published: int
    emitted: int
    merged: int
    pending: int
    cursor: int
    clients: list[WsClientStat]


class HealthResponse(BaseModel):
    """进程存活 + 依赖就绪（`api` 服务的**就绪判据**，见 §04.8.1）。"""

    ok: bool
    spec_version: str
    db: str
    latest_log_id: int
    ws: WsStats
