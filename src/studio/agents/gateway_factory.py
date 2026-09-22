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
from collections.abc import Callable, Mapping

from studio.agents.budget import TokenBudget
from studio.agents.circuit import CircuitBreaker
from studio.agents.cost import LlmCallStore
from studio.agents.gateway import GatewaySettings, LlmGateway, LogSink, SecretLookup
from studio.agents.llm_client import HttpLlmTransport, LlmTransport
from studio.core.config import LlmConfig
from studio.core.paths import StudioPaths
from studio.core.secret_store import SecretStore, get_secret_store

__all__ = ["build_gateway"]


def build_gateway(
    *,
    connection: sqlite3.Connection,
    llm: LlmConfig,
    config_provider: Callable[[], LlmConfig] | None = None,
    paths: StudioPaths | None = None,
    transport: LlmTransport | None = None,
    settings: GatewaySettings | None = None,
    log: LogSink | None = None,
    env: Mapping[str, str] | None = None,
    secrets: SecretLookup | None = None,
) -> LlmGateway:
    """装配一个可直接使用的网关（预算闸门 + 熔断 + 记账全部就位）。

    :param config_provider: 动态配置入口（``None`` ⇒ 用 ``llm`` 这一份冻结快照）。
        常驻进程（写稿池 worker）**应该**传它 —— 否则在设置页换了模型名，
        那个进程直到重启都还在用旧模型（见 ``core.config.llm_config_provider``）。
    """
    resolved = paths or StudioPaths.from_env()
    # 密钥的第二来源：面板填的 Key 落在 config/secrets.yaml，网关每次调用现取一次
    # ⇒ 填完立刻生效。调用方（如 `app/deps.py`）传了自己那一个 store 就用它的 ——
    # 「面板写 A、网关读 B」是这一层最该防的事。没传则现造：显式给了 env（测试）
    # 就单独造一个，避免单例把上一个用例的环境变量带进来。
    lookup = secrets
    if lookup is None:
        store = SecretStore(resolved, env=env) if env is not None else get_secret_store(resolved)
        lookup = store.lookup
    return LlmGateway(
        config=llm,
        config_provider=config_provider,
        transport=transport or HttpLlmTransport(),
        calls=LlmCallStore(connection),
        budget=TokenBudget(llm.budget, LlmCallStore(connection)),
        breaker=CircuitBreaker(),
        settings=settings or GatewaySettings(),
        schema_root=resolved.schemas_dir,
        log=log,
        env=env,
        secrets=lookup,
    )
