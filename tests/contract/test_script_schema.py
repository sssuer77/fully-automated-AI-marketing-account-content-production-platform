"""契约：Writer 的输出契约（T1.10 · §04.1.5）。

与 ``test_director_schema.py`` 同一手法，另外钉住**一个刻意的缺席**：
``body_md`` **不**带字数区间。原因写在 ``domain/script.py`` 的模块 docstring 里 ——
字数越界要走"重写 ≤2 次 ⇒ 取最接近版本 + warn"这条降级路径，若 schema 直接拒绝，
就变成了网关的修复重试（另一套预算、另一种失败），降级路径永远走不到。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from studio.agents.prompts import PromptLibrary
from studio.agents.writer import WriterAgent
from studio.domain.script import (
    DURATION_MAX_MS,
    DURATION_MIN_MS,
    SENTENCE_INPUT_MAX_CHARS,
    SENTENCE_MIN_COUNT,
    SPEAKERS,
    SentenceSpec,
    WriterOutput,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "script_result.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _sentence(seq: int = 1, text: str = "这不科学。") -> dict[str, Any]:
    return {"seq": seq, "text": text, "speaker": "bigbear", "emotion": "兴奋", "pause_after_ms": 200}


def _document(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "标题",
        "hook": "开场",
        "body_md": "熊" * 700,
        "cta": "关注",
        "sentences": [_sentence(index) for index in range(1, 16)],
        "est_duration_ms": 140_000,
        "catchphrases_used": ["这不科学"],
    }
    payload.update(overrides)
    return payload


class TestSchemaIsWellFormed:
    def test_file_exists(self) -> None:
        assert SCHEMA_PATH.is_file()

    def test_is_valid_json_schema(self, schema: dict[str, Any]) -> None:
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_forbids_extra_fields(self, schema: dict[str, Any]) -> None:
        assert schema["additionalProperties"] is False


class TestMirrorsThePydanticModel:
    def test_required_fields_match(self, schema: dict[str, Any]) -> None:
        expected = {name for name, field in WriterOutput.model_fields.items() if field.is_required()}
        assert set(schema["required"]) == expected

    def test_sentence_item_required_fields_match(self, schema: dict[str, Any]) -> None:
        expected = {name for name, field in SentenceSpec.model_fields.items() if field.is_required()}
        assert set(schema["properties"]["sentences"]["items"]["required"]) == expected

    def test_sentence_count_floor(self, schema: dict[str, Any]) -> None:
        assert schema["properties"]["sentences"]["minItems"] == SENTENCE_MIN_COUNT

    def test_sentence_text_ceiling_is_the_lenient_contract_value(self, schema: dict[str, Any]) -> None:
        """契约层是 200（宽松），28 由 ``enforce_sentence_limit`` 在服务端强制。"""
        assert (
            schema["properties"]["sentences"]["items"]["properties"]["text"]["maxLength"]
            == SENTENCE_INPUT_MAX_CHARS
        )

    def test_speaker_enum_matches_domain(self, schema: dict[str, Any]) -> None:
        assert schema["properties"]["sentences"]["items"]["properties"]["speaker"]["enum"] == list(SPEAKERS)

    def test_pause_bounds(self, schema: dict[str, Any]) -> None:
        pause = schema["properties"]["sentences"]["items"]["properties"]["pause_after_ms"]
        assert (pause["minimum"], pause["maximum"]) == (0, 2000)

    def test_duration_bounds(self, schema: dict[str, Any]) -> None:
        duration = schema["properties"]["est_duration_ms"]
        assert (duration["minimum"], duration["maximum"]) == (DURATION_MIN_MS, DURATION_MAX_MS)


class TestDeliberateOmissions:
    def test_body_md_has_no_length_bound(self, schema: dict[str, Any]) -> None:
        body = schema["properties"]["body_md"]
        assert body == {"type": "string", "minLength": 1}

    def test_out_of_range_body_still_validates(self, schema: dict[str, Any]) -> None:
        """500 字的稿子 schema 必须放行 —— 否则"重写 ≤2 次"这条降级路径永远走不到。"""
        document = _document(body_md="熊" * 500)
        jsonschema.validate(document, schema)
        assert WriterOutput.model_validate(document).body_md == "熊" * 500


class TestBothSidesAgree:
    def test_valid_document_passes_both(self, schema: dict[str, Any]) -> None:
        document = _document()
        jsonschema.validate(document, schema)
        assert len(WriterOutput.model_validate(document).sentences) == 15

    @pytest.mark.parametrize(
        "overrides",
        [
            {"sentences": [_sentence(index) for index in range(1, 5)]},  # 句数不足
            {"sentences": [_sentence(1, "熊" * (SENTENCE_INPUT_MAX_CHARS + 1))] * SENTENCE_MIN_COUNT},
            {"sentences": [{"seq": 1, "text": "x", "speaker": "熊大"}]},  # 说话人不在枚举里
            {"sentences": [{"seq": 1, "text": "x", "speaker": "bigbear", "pause_after_ms": 5000}]},
            {"est_duration_ms": DURATION_MAX_MS + 1},
            {"extra_field": 1},
        ],
    )
    def test_invalid_document_fails_both(self, schema: dict[str, Any], overrides: dict[str, Any]) -> None:
        document = _document(**overrides)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)
        with pytest.raises(ValueError):
            WriterOutput.model_validate(document)


class TestPromptRegistry:
    def test_writer_prompt_is_registered(self) -> None:
        library = PromptLibrary.load(REPO_ROOT / "prompts")
        assert library.verify() == []
        assert "writer" in library.names()

    def test_agent_schema_name_points_at_this_file(self) -> None:
        assert SCHEMA_PATH.name == f"{WriterAgent.schema_name}.schema.json"

    def test_prompt_version_covers_shared_blocks(self) -> None:
        library = PromptLibrary.load(REPO_ROOT / "prompts")
        combined = library.combined_version("writer", "shared.json_contract", "shared.persona_block")
        assert combined != library.prompt_version("writer")
