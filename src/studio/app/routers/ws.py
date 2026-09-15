"""`/ws/ui` 长连接（T1.7 · §04.4.1）。

路由只做三件事：解析订阅 → 交给 Hub → 把上行报文转给 Hub。
所有"发什么、发多快、发不出去怎么办"都在 `ws/hub.py`，路由层零策略。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from studio.app.deps import AppState
from studio.core.logging import get_logger
from studio.ws.hub import Hub
from studio.ws.protocol import WS_PATH, WsSubscriptionError, parse_subscription

__all__ = ["WebSocketSink", "router"]

logger = get_logger("studio.app.ws")

router = APIRouter(tags=["ws"])


class WebSocketSink:
    """把 Starlette 的 `WebSocket` 收敛成 Hub 需要的那两个动作。"""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_text(self, payload: str) -> None:
        await self._websocket.send_text(payload)

    async def close(self, code: int, reason: str) -> None:
        await self._websocket.close(code=code, reason=reason)


def _state(websocket: WebSocket) -> AppState:
    state: AppState = websocket.app.state.studio
    return state


@router.websocket(WS_PATH)
async def ws_ui(websocket: WebSocket) -> None:
    """握手 → 订阅 → 转发上行；断开时统一由 Hub 收尾。"""
    state = _state(websocket)
    hub: Hub = state.hub
    try:
        subscription = parse_subscription(websocket.query_params)
    except WsSubscriptionError as exc:
        logger.warning("ws.subscription_rejected", error=exc.message)
        await websocket.close(code=1008, reason=f"{exc.code}: {exc.message}")
        return

    await websocket.accept()
    conn = hub.connect(
        conn_id=uuid.uuid4().hex[:12],
        subscription=subscription,
        sink=WebSocketSink(websocket),
    )
    try:
        while True:
            raw = await websocket.receive_text()
            hub.handle_uplink(conn, raw)
    except WebSocketDisconnect:
        pass
    finally:
        await hub.close_connection(conn, code=1000, reason="client disconnected")
