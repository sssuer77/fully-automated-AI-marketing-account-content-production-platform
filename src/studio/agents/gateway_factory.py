"""LLM 网关的装配（composition root）。

为什么工厂函数放在网关模块而不是 ``cli.py``
------------------------------------------
网关要凑齐 8 样东西（配置 / 传输 / 记账 / 预算 / 熔断 / 策略 / schema 根 / 日志出口），
任何一个服务层调用方都得凑一遍。放在 ``cli.py`` 意味着"只有 CLI 能用"；
放在这里则"谁 import 谁就能拿到一个配好的网关"，而 CLI 依然只是它的一个调用方。

依赖方向仍然干净：``agents`` → ``core``（``LlmConfig`` / ``StudioPaths``），
不碰 ``services`` / ``app``。日志出口照旧是注入的 :class:`LogSink` 回调。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from studio.agents.budget import TokenBudget
from studio.agents.circuit import CircuitBreaker
from studio.agents.cost import LlmCallStore
from studio.agents.gateway import GatewaySettings, LlmGateway, LogSink
from studio.agents.llm_client import HttpLlmTransport, LlmTransport
from studio.core.config import LlmConfig
from studio.core.paths import StudioPaths

__all__ = ["build_gateway"]


def build_gateway(
    *,
    connection: sqlite3.Connection,
    llm: LlmConfig,
    paths: StudioPaths | None = None,
    transport: LlmTransport | None = None,
    settings: GatewaySettings | None = None,
    log: LogSink | None = None,
    env: Mapping[str, str] | None = None,
) -> LlmGateway:
    """装配一个可直接使用的网关（预算闸门 + 熔断 + 记账全部就位）。"""
    resolved = paths or StudioPaths.from_env()
    return LlmGateway(
        config=llm,
        transport=transport or HttpLlmTransport(),
        calls=LlmCallStore(connection),
        budget=TokenBudget(llm.budget, LlmCallStore(connection)),
        breaker=CircuitBreaker(),
        settings=settings or GatewaySettings(),
        schema_root=resolved.schemas_dir,
        log=log,
        env=env,
    )
