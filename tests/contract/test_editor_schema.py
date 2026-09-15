"""契约：Editor 的输出契约（T1.11 · §04.1.6 / 原文 §2.2⑥）。

Editor **整份回写**稿件（裁定 98），所以这份 schema 内联了完整的 ``WriterOutput``
结构 —— 网关按单文件加载 schema，跨文件 ``$ref`` 用不了。

一个刻意的缺席与 Writer 一致：``body_md`` **不带字数区间**。字数越界要走
"改稿 ⇒ 取最接近版本 + warn"这条降级路径，若 schema 直接拒绝，就变成了网关的
修复重试（另一套预算、另一种失败），降级路径永远走不到。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from studio.agents.editor import EditorAgent
from studio.agents.prompts import PromptLibrary
from studio.domain.scoring import EditorOutput
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
SCHEMA_PATH = REPO_ROOT / "schemas" / "editor_result.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def script_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", schema["properties"]["script"])


def _sentence(seq: int = 1, text: str = "这不科学。") -> dict[str, Any]:
    return {"seq": seq, "text": text, "speaker": "bigbear", "emotion": "兴奋", "pause_after_ms": 200}


def _script(**overrides: Any) -> dict[str, Any]:
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


def _document(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"script": _script(), "changes": ["把开场换成结果前置"]}
    payload.update(overrides)
    return payload


class TestSchemaIsWellFormed:
    def test_file_exists(self) -> None:
        assert SCHEMA_PATH.is_file()

    def test_is_valid_json_schema(self, schema: dict[str, Any]) -> None:
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_forbids_extra_fields(self, schema: dict[str, Any]) -> None:
        assert schema["additionalProperties"] is False

    def test_the_script_block_is_inlined(self, script_schema: dict[str, Any]) -> None:
        """单文件加载 ⇒ 不能跨文件 ``$ref``，只能是内联对象。"""
        assert script_schema["type"] == "object"
        assert "$ref" not in script_schema


class TestMirrorsThePydanticModel:
    def test_required_fields_cover_the_model(self, schema: dict[str, Any]) -> None:
        required = {name for name, field in EditorOutput.model_fields.items() if field.is_required()}
        assert required <= set(schema["required"])

    def test_changes_is_required_by_the_schema_only(self, schema: dict[str, Any]) -> None:
        """模型有默认值、schema 没有 —— "我没改"也要写出来（空数组 ≠ 省略）。"""
        required = {name for name, field in EditorOutput.model_fields.items() if field.is_required()}
        assert set(schema["required"]) - required == {"changes"}

    def test_script_required_fields_match(self, script_schema: dict[str, Any]) -> None:
        expected = {name for name, field in WriterOutput.model_fields.items() if field.is_required()}
        assert set(script_schema["required"]) == expected

    def test_sentence_item_required_fields_match(self, script_schema: dict[str, Any]) -> None:
        expected = {name for name, field in SentenceSpec.model_fields.items() if field.is_required()}
        assert set(script_schema["properties"]["sentences"]["items"]["required"]) == expected

    def test_sentence_count_floor(self, script_schema: dict[str, Any]) -> None:
        assert script_schema["properties"]["sentences"]["minItems"] == SENTENCE_MIN_COUNT

    def test_sentence_text_ceiling_is_the_lenient_contract_value(self, script_schema: dict[str, Any]) -> None:
        """契约层是 200（宽松），28 由 ``enforce_sentence_limit`` 在服务端强制。"""
        text = script_schema["properties"]["sentences"]["items"]["properties"]["text"]
        assert text["maxLength"] == SENTENCE_INPUT_MAX_CHARS

    def test_speaker_enum_matches_domain(self, script_schema: dict[str, Any]) -> None:
        speaker = script_schema["properties"]["sentences"]["items"]["properties"]["speaker"]
        assert speaker["enum"] == list(SPEAKERS)

    def test_duration_bounds(self, script_schema: dict[str, Any]) -> None:
        duration = script_schema["properties"]["est_duration_ms"]
        assert (duration["minimum"], duration["maximum"]) == (DURATION_MIN_MS, DURATION_MAX_MS)

    def test_changes_are_bounded(self, schema: dict[str, Any]) -> None:
        changes = schema["properties"]["changes"]
        assert changes["items"] == {"type": "string", "minLength": 1, "maxLength": 200}


class TestDeliberateOmissions:
    def test_body_md_has_no_length_bound(self, script_schema: dict[str, Any]) -> None:
        assert script_schema["properties"]["body_md"] == {"type": "string", "minLength": 1}

    def test_out_of_range_body_still_validates(self, schema: dict[str, Any]) -> None:
        """500 字的改稿必须放行 —— 否则"取最接近版本 + warn"这条降级路径永远走不到。"""
        document = _document(script=_script(body_md="熊" * 500))
        jsonschema.validate(document, schema)
        assert EditorOutput.model_validate(document).script.body_md == "熊" * 500

    def test_there_is_no_diff_format(self, schema: dict[str, Any]) -> None:
        """裁定 98：整份回写，不返回 diff（``patch`` / ``diff`` 这类字段都不该存在）。"""
        assert set(schema["properties"]) == {"script", "changes"}


class TestBothSidesAgree:
    def test_a_valid_document_passes_both(self, schema: dict[str, Any]) -> None:
        document = _document()
        jsonschema.validate(document, schema)
        assert len(EditorOutput.model_validate(document).script.sentences) == 15

    def test_an_empty_change_list_is_fine(self, schema: dict[str, Any]) -> None:
        document = _document(changes=[])
        jsonschema.validate(document, schema)
        assert EditorOutput.model_validate(document).changes == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"script": _script(sentences=[_sentence(index) for index in range(1, 5)])},  # 句数不足
            {"script": _script(body_md="")},
            {"script": _script(est_duration_ms=10_000)},
            {"script": _script(est_duration_ms=200_000)},
            {"script": _script(sentences=[_sentence(1, text="熊" * 201)])},
            {"script": _script(sentences=[{"seq": 1, "text": "句", "speaker": "unknown"}])},
            {"script": _script(title="熊" * 61)},
        ],
    )
    def test_an_invalid_document_fails_both(self, schema: dict[str, Any], overrides: dict[str, Any]) -> None:
        document = _document(**overrides)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)
        with pytest.raises(ValueError):
            EditorOutput.model_validate(document)


class TestTheSchemaIsStricterThanTheModel:
    """网关的两层校验里 schema 先跑 ⇒ **严的那一边说了算**。

    下面几处 pydantic 会放行（模型没写 ``min_length``），schema 不会。把它们列出来，
    是为了让"哪一层在把关"这件事有据可查 —— 否则等模型塞进来一个空开场才发现，
    而那时它已经过完网关、进了库。
    """

    @pytest.mark.parametrize(
        "overrides",
        [
            {"script": _script(hook="")},
            {"script": _script(cta="")},
            {"changes": [""]},
            {"changes": ["熊" * 201]},
        ],
    )
    def test_the_schema_rejects_what_the_model_would_accept(
        self, schema: dict[str, Any], overrides: dict[str, Any]
    ) -> None:
        document = _document(**overrides)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)
        EditorOutput.model_validate(document)  # 不报错 —— 正因如此才需要上面那条断言


class TestPromptRegistry:
    def test_editor_prompt_is_registered(self) -> None:
        library = PromptLibrary.load(REPO_ROOT / "prompts")
        assert library.verify() == []
        assert "editor" in library.names()

    def test_agent_schema_name_points_at_this_file(self) -> None:
        assert SCHEMA_PATH.name == f"{EditorAgent.schema_name}.schema.json"

    def test_prompt_version_covers_shared_blocks(self) -> None:
        library = PromptLibrary.load(REPO_ROOT / "prompts")
        combined = library.combined_version("editor", "shared.json_contract", "shared.persona_block")
        assert combined != library.prompt_version("editor")
