"""结构化输出守卫（T1.8 · §04.1.1 硬约束 1）。

三条纪律
--------
1. **Agent 输出必须是 JSON 且过 ``schemas/*.schema.json``**；解析失败**不得**用正则
   从散文里"抠"出 JSON —— 那只会掩盖提示词问题，把偶发成功当稳定。
2. 唯一的宽容是**剥一层代码围栏**：模型偶尔把 JSON 包在 ```` ```json ```` 里。
   仅当**整段文本就是**一个围栏块时才剥（见 :func:`strip_fence`），不做局部搜索。
3. 校验失败 ⇒ 把**结构化错误**回灌给模型做"修复重试"（≤3 次，见 ``gateway.py``），
   而不是换个 schema 硬套。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from studio.core.errors import ErrorCode, LlmSchemaError

__all__ = [
    "MAX_REPAIR_ERRORS",
    "REPAIR_INSTRUCTION",
    "SchemaGuard",
    "parse_json",
    "repair_prompt",
    "strip_fence",
]

#: 回灌给模型的错误条数上限（太多会把上下文挤爆，且前几条已足够定位）
MAX_REPAIR_ERRORS: Final[int] = 10

#: 修复重试时追加的指令（**不含**任何"猜一个"的暗示 —— 要求它按错误逐条改）
REPAIR_INSTRUCTION: Final[str] = (
    "你上一次的输出未通过 JSON Schema 校验。请**只输出修正后的 JSON**，"
    "不要解释、不要加代码围栏、不要改变未被指出问题的字段。\n"
    "校验错误如下：\n"
)


def strip_fence(text: str) -> str:
    """剥掉**整段包裹**的代码围栏（```` ```json ... ``` ````）。

    只处理"整段就是一个围栏块"的情形：首行以 ```` ``` ```` 开头且末行是
    ```` ``` ````。任何其它形态（前后有散文、多个围栏）一律原样返回，
    交给 :func:`parse_json` 报错 —— 不做正则搜索。
    """
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2 or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def parse_json(text: str) -> Any:
    """严格解析 JSON（失败 ⇒ :class:`LlmSchemaError`）。"""
    candidate = strip_fence(text)
    if not candidate:
        raise LlmSchemaError(
            "LLM 输出为空，无法解析为 JSON",
            context={"length": len(text)},
            remediation="检查提示词是否要求 JSON 输出，以及 json_mode 是否被通道忽略",
        )
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LlmSchemaError(
            f"LLM 输出不是合法 JSON：{exc.msg}（行 {exc.lineno} 列 {exc.colno}）",
            context={"length": len(text), "head": text[:200]},
            remediation="提示词里补强『只输出 JSON』约束；禁止用正则补救（§04.1.1）",
        ) from exc


def repair_prompt(errors: list[str], previous_text: str) -> str:
    """构造修复重试的 user 消息（把校验错误 + 上次输出回灌）。"""
    listed = "\n".join(f"- {item}" for item in errors[:MAX_REPAIR_ERRORS])
    return f"{REPAIR_INSTRUCTION}{listed}\n\n上一次的输出：\n{previous_text}"


class SchemaGuard:
    """一个 JSON Schema 的校验器（构造时即校验 schema 自身合法）。"""

    def __init__(self, schema: Mapping[str, Any], *, name: str = "<inline>") -> None:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise LlmSchemaError(
                f"JSON Schema 自身不合法（{name}）：{exc.message}",
                context={"schema": name},
                remediation="修 schemas/ 下的 schema 文件（契约测试会比对 §04 条款）",
            ) from exc
        self._name = name
        self._validator = Draft202012Validator(schema)

    @property
    def name(self) -> str:
        return self._name

    @classmethod
    def from_file(cls, path: Path) -> SchemaGuard:
        """从 ``schemas/*.schema.json`` 载入。"""
        if not path.is_file():
            raise LlmSchemaError(
                f"Schema 文件不存在：{path}",
                code=ErrorCode.LLM_SCHEMA_INVALID,
                context={"path": str(path)},
                remediation="补齐 schemas/ 下的契约文件（每个 Agent 一个）",
            )
        return cls(json.loads(path.read_text(encoding="utf-8")), name=path.name)

    def validate(self, data: object) -> list[str]:
        """返回人类可读的校验错误（``$.directions[0].title: 'x' is not of type 'string'``）。

        错误按 JSON 路径排序 ⇒ **同一份坏输出每次给出同一串错误**（可测、可复现）。
        """
        errors: list[str] = []
        for error in self._validator.iter_errors(data):
            errors.append(f"{error.json_path}: {error.message}")
        errors.sort()
        return errors[:MAX_REPAIR_ERRORS]

    def check(self, text: str) -> tuple[Any, list[str]]:
        """解析 + 校验。返回 ``(数据, 错误列表)``；错误非空时数据不可信。"""
        data = parse_json(text)
        return data, self.validate(data)
