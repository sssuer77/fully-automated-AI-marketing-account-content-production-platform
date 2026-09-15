"""Web 侧契约生成（T4.1 · §02.2 / §04.4）—— 前端类型的**唯一来源**。

为什么要有这个模块
------------------
T4.1 的硬约束是"**API 类型由 OpenAPI 自动生成，禁止手写接口类型**"。光有生成命令
不够 —— 生成物一旦入库就会漂移，而"漂移"只有在能被断言时才叫漂移。所以：

1. 真相源是 Python 侧（FastAPI 的 ``app.openapi()`` + ``ws/protocol.py`` 的枚举表）；
2. 渲染函数（本模块）是**纯函数**：给同样的输入永远给同样的字符串；
3. 生成物入库；``tests/contract/test_web_contracts.py`` 直接调本模块比对 ⇒ 后端一改、
   生成物没跟着改，门禁就红（和 ``prompts/manifest.yaml`` 的 sha256 漂移同一条思路）。

渲染与写盘分开
--------------
``render_*`` 不碰文件系统（测试只调它们），``write_web_contracts`` 才落盘。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from types import NoneType
from typing import Any, Final, get_args, get_origin, get_type_hints

from fastapi import FastAPI

from studio.core.proto import AlertCode
from studio.services.log_service import SystemLog
from studio.ws.protocol import (
    CHANNELS,
    COALESCE_WINDOW_SEC,
    DEFAULT_MIN_LEVEL,
    KIND_POLICY,
    REPLAY_LIMIT,
    RING_CAPACITY,
    SEVERITY_ORDER,
    SLOW_SUSTAIN_SEC,
    WS_PATH,
    WS_PROTOCOL_VERSION,
    EventKind,
    FrameType,
)

__all__ = [
    "EVENTS_TS_RELATIVE",
    "OPENAPI_JSON_RELATIVE",
    "render_events_ts",
    "render_openapi_json",
    "write_web_contracts",
]

#: 生成物在 ``web/`` 下的相对路径（写盘与漂移检查共用同一份常量）
OPENAPI_JSON_RELATIVE: Final[str] = "openapi.json"
EVENTS_TS_RELATIVE: Final[str] = "src/ws/events.ts"

_TS_SCALARS: Final[Mapping[Any, str]] = {
    str: "string",
    bool: "boolean",
    int: "number",
    float: "number",
}


def render_openapi_json(app: FastAPI) -> str:
    """渲染 ``web/openapi.json``（排序 + 固定缩进 ⇒ 同输入同输出）。"""
    schema = app.openapi()
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _ts_literal_union(name: str, values: tuple[str, ...]) -> str:
    body = "".join(f'  "{value}",\n' for value in values)
    return f"export const {name} = [\n{body}] as const;\n"


def _ts_type(hint: Any) -> str:
    """Python 类型注解 → TypeScript 类型（只覆盖 ``SystemLog`` 用到的几种）。"""
    if get_origin(hint) in (Mapping, dict):
        return "Record<string, unknown>"
    args = get_args(hint)
    if args and NoneType in args:
        inner = [item for item in args if item is not NoneType]
        if len(inner) == 1:
            return f"{_ts_type(inner[0])} | null"
    return _TS_SCALARS.get(hint, "unknown")


def render_events_ts() -> str:
    """渲染 ``web/src/ws/events.ts``（WS 协议的常量 + 信封 + 日志行）。"""
    hints = get_type_hints(SystemLog)
    log_fields = "".join(
        f"  {field.name}: {_ts_type(hints[field.name])};\n" for field in dataclasses.fields(SystemLog)
    )
    kind_channel = "".join(
        f'  "{kind.value}": "{policy.channel.value}",\n' for kind, policy in KIND_POLICY.items()
    )
    severity = "".join(f'  "{level}": {order},\n' for level, order in SEVERITY_ORDER.items())

    return (
        "// ⚠️ 本文件由 `tasks.ps1 web:gen` 自动生成，**禁止手改**。\n"
        "// 真相源：`src/studio/ws/protocol.py` + `src/studio/services/log_service.py`；\n"
        "// 漂移由 `tests/contract/test_web_contracts.py` 拦截。\n"
        "\n"
        f"export const WS_PROTOCOL_VERSION = {WS_PROTOCOL_VERSION};\n"
        f'export const WS_PATH = "{WS_PATH}";\n'
        "\n" + _ts_literal_union("CHANNELS", CHANNELS) + "export type Channel = (typeof CHANNELS)[number];\n"
        "\n"
        + _ts_literal_union("FRAME_TYPES", tuple(item.value for item in FrameType))
        + "export type FrameType = (typeof FRAME_TYPES)[number];\n"
        "\n"
        + _ts_literal_union("EVENT_KINDS", tuple(item.value for item in EventKind))
        + "export type EventKind = (typeof EVENT_KINDS)[number];\n"
        + "\n"
        + _ts_literal_union("ALERT_CODES", tuple(item.value for item in AlertCode))
        + "export type AlertCode = (typeof ALERT_CODES)[number];\n"
        "\n"
        "/** 事件种类 → 归属通道（前端按它决定一条事件该进哪个面板）。 */\n"
        "export const KIND_CHANNEL: Record<EventKind, Channel> = {\n"
        f"{kind_channel}"
        "};\n"
        "\n"
        "/** 级别序（只用于比较；与 `system_logs.level` 的 CHECK 同源）。 */\n"
        "export const SEVERITY_ORDER: Record<string, number> = {\n"
        f"{severity}"
        "};\n"
        f'export const DEFAULT_MIN_LEVEL = "{DEFAULT_MIN_LEVEL}";\n'
        "\n"
        "/** 服务端背压参数（前端据此理解 `WS_CLIENT_SLOW` 断开的含义）。 */\n"
        f"export const COALESCE_WINDOW_MS = {int(COALESCE_WINDOW_SEC * 1000)};\n"
        f"export const RING_CAPACITY = {RING_CAPACITY};\n"
        f"export const SLOW_SUSTAIN_SEC = {SLOW_SUSTAIN_SEC};\n"
        f"export const REPLAY_LIMIT = {REPLAY_LIMIT};\n"
        "\n"
        "/** 统一报文信封（§04.4.2）。`seq` 是**每连接**单调，用于缺口检测。 */\n"
        "export interface Envelope {\n"
        "  v: number;\n"
        "  type: FrameType;\n"
        "  channel: Channel;\n"
        "  seq: number;\n"
        "  ts: string;\n"
        "  data: Record<string, unknown>;\n"
        "}\n"
        "\n"
        "/** `system_logs` 一行（= `SystemLog.to_dict()`；快照与日志事件共用）。 */\n"
        "export interface SystemLogRow {\n"
        f"{log_fields}"
        "}\n"
        "\n"
        "/** `logs` 通道快照载荷（§04.4.6）。 */\n"
        "export interface LogSnapshot {\n"
        "  logs: SystemLogRow[];\n"
        "  truncated: boolean;\n"
        "  hint?: string;\n"
        "}\n"
    )


def write_web_contracts(web_dir: Path, *, app: FastAPI) -> tuple[Path, ...]:
    """把两份生成物写到 ``web_dir``（返回写出的路径，便于日志与测试断言）。"""
    written: list[Path] = []
    for relative, text in (
        (OPENAPI_JSON_RELATIVE, render_openapi_json(app)),
        (EVENTS_TS_RELATIVE, render_events_ts()),
    ):
        target = web_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    return tuple(written)
