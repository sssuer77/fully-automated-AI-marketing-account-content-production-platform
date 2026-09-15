"""WS 测试共用的假件与帧工具。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from studio.core.clock import now_iso
from studio.ws.protocol import Channel, Envelope, FrameType

__all__ = ["RecordingSink", "StuckSink", "decode", "event"]


def event(kind: str, channel: Channel = Channel.TASKS, **data: Any) -> Envelope:
    """造一条下行事件帧（`data.kind` 必填）。"""
    return Envelope(
        type=FrameType.EVENT,
        channel=channel,
        ts=now_iso(),
        data={"kind": kind} | data,
    )


def decode(payload: str) -> dict[str, Any]:
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


class RecordingSink:
    """把"发出去的东西"记下来的假出口（不需要真 socket）。"""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.closed: tuple[int, str] | None = None

    async def send_text(self, payload: str) -> None:
        self.frames.append(decode(payload))

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    def kinds(self) -> list[str]:
        return [str(frame["data"].get("kind")) for frame in self.frames]


class StuckSink:
    """永远卡在 `send_text` 上的假出口 —— 用来验证慢客户端判定。"""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed: tuple[int, str] | None = None
        self.attempts = 0

    async def send_text(self, payload: str) -> None:
        self.attempts += 1
        await self.release.wait()

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)
        self.release.set()
