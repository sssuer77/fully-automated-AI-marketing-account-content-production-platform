"""传输层 wire format（T1.8 · §01.2.4）：本地通道的**语法约束解码**。

为什么单独钉这一层
------------------
"本地兜底为什么总失败"曾经是个玄学问题：模型每次 HTTP 200，网关的修复重试也
照跑 3 轮，却永远只吐 1 条方向（``$.directions is too short``）。根因不在重试策略，
而在请求体 —— 只给了 ``format: "json"``（"是 JSON 就行"），没给 ``format: <schema>``
（"必须长成这个样子"）。把契约交给解码器，``minItems`` 这类**数量约束**才真正生效
（qwen2.5:7b 实测：给 schema 后稳定 5–8 条）。

schema 是**契约事实**、与 engine 无关，所以由网关挂到 :class:`LlmCall` 上；
"怎么编码"才是传输层的事 —— 云端仍走 ``json_object``（要真 Key 才能验，另开一刀）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from studio.agents.llm_client import ChatMessage, LlmCall, build_body

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title": {"type": "string", "minLength": 1}},
    "required": ["title"],
    "additionalProperties": False,
}

MESSAGES: Sequence[ChatMessage] = [ChatMessage(role="user", content="出个方向")]


def _call(engine: str, **overrides: Any) -> LlmCall:
    params: dict[str, Any] = {
        "profile": engine,
        "engine": engine,
        "base_url": "http://127.0.0.1:11434" if engine == "ollama" else "http://cloud.test/v1",
        "model": "m",
        "response_schema": SCHEMA,
    }
    params.update(overrides)
    return LlmCall(**params)


def test_ollama_sends_the_schema_as_format() -> None:
    """有 schema ⇒ ``format`` 是 schema 本体（不是字符串 ``"json"``）。"""
    body = build_body(_call("ollama"), MESSAGES)
    assert body["format"] == SCHEMA


def test_ollama_falls_back_to_plain_json_without_a_schema() -> None:
    """没有 schema（如无 guard 的调用方）⇒ 退回 ``"json"``，不凭空造契约。"""
    body = build_body(_call("ollama", response_schema=None), MESSAGES)
    assert body["format"] == "json"


def test_ollama_without_json_mode_sends_no_format() -> None:
    """``json_mode=False`` ⇒ 连 ``format`` 都不发（schema 也压不住它）。"""
    body = build_body(_call("ollama", json_mode=False), MESSAGES)
    assert "format" not in body


def test_cloud_keeps_json_object_and_never_ships_the_schema() -> None:
    """云端通道**不带** schema 出门：``response_format`` 仍是 ``json_object``。"""
    body = build_body(_call("oi_compatible"), MESSAGES)
    assert body["response_format"] == {"type": "json_object"}
    assert SCHEMA not in body.values()
