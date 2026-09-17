"""LLM 通道与密钥的读面 —— CLI 与面板**共用同一份判定**。

为什么要有这一层
----------------
``studio llm probe`` 早就有了，判定写在 ``cli.py`` 里。面板要显示同一件事
（这条通道现在能不能用、缺什么），如果照抄一份，就会出现这个仓库反复踩的那类
bug：**命令行说能进、面板说不能**（陷阱 #150 / #151 是同一族）。所以判定收到
这里，CLI 与 REST 都只做「调它 + 排版」。

两个刻意的取舍
--------------
1. **探测只发只读 GET**：云端走 ``/models``、本地 ollama 走 ``/api/tags``。
   不发一次真补全 —— 面板上点一下「测试连接」不该产生一次计费调用。
2. **「配了密钥」与「通道通」是两件事**：前者是静态事实（文件里有值），
   后者要真发一次请求。面板两个都给：静态的随首屏返回，动态的按需探测。
   混成一个的话，用户会在「网络不通」时去反复重填密钥。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

import httpx

from studio.core.config import LlmConfig, LlmProfileConfig
from studio.core.secret_store import KeySource, SecretStore

__all__ = [
    "PROBE_TIMEOUT_SEC",
    "LlmProbeRow",
    "LlmProfileCard",
    "key_source_label",
    "probe_one",
    "probe_profiles",
    "profile_cards",
]

#: 探测超时（只发只读 GET；超过这个时间就是「连不上」，不该让面板一直转）
PROBE_TIMEOUT_SEC: Final[float] = 10.0

ProbeStatus = Literal["ok", "no_key", "unreachable", "http_error"]

_SOURCE_LABELS: Final[dict[KeySource, str]] = {
    "env": "环境变量",
    "file": "config/secrets.yaml",
    "none": "尚未配置",
}


def key_source_label(source: KeySource) -> str:
    """密钥来源的中文说明（面板与 CLI 用同一份措辞）。"""
    return _SOURCE_LABELS[source]


@dataclass(frozen=True, slots=True)
class LlmProbeRow:
    """一次探测的结果（``status`` 是**判定**，``detail`` 是给人看的一句话）。"""

    profile: str
    engine: str
    model: str
    base_url: str
    status: ProbeStatus
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile,
            "engine": self.engine,
            "model": self.model,
            "base_url": self.base_url,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class LlmProfileCard:
    """一条通道的**静态**状态（不发任何请求就能回答的那部分）。"""

    name: str
    engine: str
    base_url: str
    model: str
    api_key_env: str | None
    is_default: bool
    needs_key: bool
    usable: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "engine": self.engine,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "is_default": self.is_default,
            "needs_key": self.needs_key,
            "usable": self.usable,
            "detail": self.detail,
        }


def profile_cards(llm: LlmConfig, store: SecretStore) -> list[LlmProfileCard]:
    """把配置里的每条通道翻成一张卡片（顺序 = 配置里的声明顺序，稳定好读）。"""
    source = store.source()
    masked = store.masked()
    cards: list[LlmProfileCard] = []
    for name, profile in llm.profiles.items():
        needs_key = profile.engine != "ollama" and profile.api_key_env is not None
        if not needs_key:
            usable = True
            detail = "本地通道，不需要密钥"
        elif source == "none":
            usable = False
            detail = f"缺密钥 —— 在下面填一把，或设置环境变量 {profile.api_key_env}"
        else:
            usable = True
            detail = f"密钥来自{key_source_label(source)}（{masked}）"
        cards.append(
            LlmProfileCard(
                name=name,
                engine=profile.engine,
                base_url=profile.base_url,
                model=profile.model,
                api_key_env=profile.api_key_env,
                is_default=name == llm.default_profile,
                needs_key=needs_key,
                usable=usable,
                detail=detail,
            )
        )
    return cards


async def probe_profiles(
    profiles: Mapping[str, LlmProfileConfig],
    store: SecretStore,
    *,
    timeout_sec: float = PROBE_TIMEOUT_SEC,
) -> list[LlmProbeRow]:
    """并发探测各通道（只发只读 GET；本地走 ``/api/tags``）。"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec)) as client:
        return [await probe_one(client, name, profile, store) for name, profile in sorted(profiles.items())]


async def probe_one(
    client: httpx.AsyncClient,
    name: str,
    profile: LlmProfileConfig,
    store: SecretStore,
) -> LlmProbeRow:
    """探测一条通道。

    密钥**从 store 取**（环境变量 > ``config/secrets.yaml``）—— 与网关用的是同一条
    查找，所以这里说「能用」，真跑任务时就是真能用。
    """
    base = profile.base_url.rstrip("/")

    def row(status: ProbeStatus, detail: str) -> LlmProbeRow:
        return LlmProbeRow(
            profile=name,
            engine=profile.engine,
            model=profile.model,
            base_url=profile.base_url,
            status=status,
            detail=detail,
        )

    if profile.engine == "ollama":
        url, headers = f"{base}/api/tags", {}
    else:
        api_key = store.lookup(profile.api_key_env) if profile.api_key_env else None
        if profile.api_key_env and not api_key:
            return row("no_key", f"环境变量 {profile.api_key_env} 未设置，密钥文件里也没有")
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return row("unreachable", f"{type(exc).__name__}: {url}")
    if response.status_code >= 400:
        return row("http_error", f"HTTP {response.status_code}")
    if profile.engine == "ollama":
        payload = response.json()
        names = [item.get("name") for item in payload.get("models", []) if isinstance(item, dict)]
        detail = f"本地模型 {len(names)} 个" + (f"（{names[0]}…）" if names else "（尚未 pull 模型）")
        return row("ok", detail)
    return row("ok", "已连接")
