"""契约：Director 的输出契约（T1.10 · §04.1.4）。

``schemas/director_result.schema.json`` 与 ``domain/script.py`` 的
:class:`DirectorOutput` 是**同一份契约的两面**：前者给 LLM（网关用它拦坏 JSON），
后者给运行期（服务层用它取字段）。两面漂移的后果是"schema 放行、模型报错"
或者反过来 —— 都在生产里表现为"模型明明按 schema 答了却被拒"。

所以这里逐条对齐：必填字段、段数、单段字数、时长区间、提示词注册。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from studio.agents.director import DirectorAgent
from studio.agents.prompts import PromptLibrary
from studio.domain.script import (
    DURATION_MAX_MS,
    DURATION_MIN_MS,
    SEGMENT_CHARS_MAX,
    SEGMENT_CHARS_MIN,
    SEGMENT_MAX,
    SEGMENT_MIN,
    DirectorOutput,
    ScriptSegment,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "director_result.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _segment(seq: int = 1, chars: int = 200) -> dict[str, Any]:
    return {"seq": seq, "point": "要点", "visual": "画面", "mood": "兴奋", "est_chars": chars}


def _document(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hook_3s": "熊大又整活了",
        "segments": [_segment(index) for index in (1, 2, 3)],
        "cta": "点个关注",
        "est_duration_ms": 140_000,
    }
    payload.update(overrides)
    return payload


class TestSchemaIsWellFormed:
    def test_file_exists(self) -> None:
        assert SCHEMA_PATH.is_file()

    def test_is_valid_json_schema(self, schema: dict[str, Any]) -> None:
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_declares_draft_2020_12(self, schema: dict[str, Any]) -> None:
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_forbids_extra_fields(self, schema: dict[str, Any]) -> None:
        assert schema["additionalProperties"] is False


class TestMirrorsThePydanticModel:
    def test_required_fields_match(self, schema: dict[str, Any]) -> None:
        expected = {name for name, field in DirectorOutput.model_fields.items() if field.is_required()}
        assert set(schema["required"]) == expected

    def test_segment_count_bounds(self, schema: dict[str, Any]) -> None:
        segments = schema["properties"]["segments"]
        assert segments["minItems"] == SEGMENT_MIN
        assert segments["maxItems"] == SEGMENT_MAX

    def test_segment_char_bounds(self, schema: dict[str, Any]) -> None:
        est_chars = schema["properties"]["segments"]["items"]["properties"]["est_chars"]
        assert est_chars["minimum"] == SEGMENT_CHARS_MIN
        assert est_chars["maximum"] == SEGMENT_CHARS_MAX

    def test_duration_bounds(self, schema: dict[str, Any]) -> None:
        duration = schema["properties"]["est_duration_ms"]
        assert duration["minimum"] == DURATION_MIN_MS
        assert duration["maximum"] == DURATION_MAX_MS

    def test_hook_length_matches(self, schema: dict[str, Any]) -> None:
        assert schema["properties"]["hook_3s"]["maxLength"] == 80

    def test_segment_required_fields(self, schema: dict[str, Any]) -> None:
        expected = {name for name, field in ScriptSegment.model_fields.items() if field.is_required()}
        assert set(schema["properties"]["segments"]["items"]["required"]) == expected


class TestBothSidesAgree:
    def test_valid_document_passes_both(self, schema: dict[str, Any]) -> None:
        document = _document()
        jsonschema.validate(document, schema)
        assert len(DirectorOutput.model_validate(document).segments) == 3

    @pytest.mark.parametrize(
        "overrides",
        [
            {"segments": [_segment(1)]},  # 段数不足
            {"segments": [_segment(index) for index in range(1, 7)]},  # 段数过多
            {"segments": [_segment(1, SEGMENT_CHARS_MIN - 1), _segment(2), _segment(3)]},
            {"est_duration_ms": DURATION_MIN_MS - 1},
            {"est_duration_ms": DURATION_MAX_MS + 1},
            {"extra_field": 1},  # 多余字段
        ],
    )
    def test_invalid_document_fails_both(self, schema: dict[str, Any], overrides: dict[str, Any]) -> None:
        document = _document(**overrides)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)
        with pytest.raises(ValueError):
            DirectorOutput.model_validate(document)


class TestPromptRegistry:
    def test_director_prompt_is_registered(self) -> None:
        library = PromptLibrary.load(REPO_ROOT / "prompts")
        assert library.verify() == []
        assert "director" in library.names()

    def test_agent_schema_name_points_at_this_file(self) -> None:
        assert SCHEMA_PATH.name == f"{DirectorAgent.schema_name}.schema.json"

    def test_prompt_version_covers_shared_blocks(self) -> None:
        library = PromptLibrary.load(REPO_ROOT / "prompts")
        combined = library.combined_version("director", "shared.json_contract", "shared.persona_block")
        assert combined != library.prompt_version("director")
        assert combined.startswith("1+")
