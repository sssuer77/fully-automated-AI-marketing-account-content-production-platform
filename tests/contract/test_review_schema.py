"""契约：Reviewer 的输出契约（T1.11 · §04.1.6）。

与 ``test_script_schema.py`` 同一手法，另外钉住**两个刻意的缺席**：

1. **没有** ``total`` / ``grade`` / ``verdict`` / ``decision`` —— 裁定 89：
   让模型报总分 = 让考生自己判卷，而且"0.3×规则 + 0.7×LLM"这条硬契约会失去唯一实现；
2. schema 比 pydantic **更严**：``issues`` 在两边都有默认值，但 schema 把它列进
   ``required`` —— 问题清单必须被显式给出，"省略掉"与"没有"是两回事。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from studio.agents.prompts import PromptLibrary
from studio.agents.reviewer import ReviewerAgent
from studio.domain.scoring import (
    LLM_DIMENSIONS,
    ChannelItem,
    LlmChannelDetail,
    ReviewerOutput,
    ReviewIssue,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "review_result.schema.json"

#: 契约里**故意不存在**的字段（裁定 89）
ABSENT_FIELDS = ("total", "grade", "verdict", "decision", "rule_total", "rule_detail")


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _channel(score: float = 8.0) -> dict[str, Any]:
    return {"score": score, "comment": "还行"}


def _issue(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": "weak_hook",
        "severity": "major",
        "target": "hook",
        "detail": "开场太平",
        "suggestion": "把结果前置",
    }
    payload.update(overrides)
    return payload


def _document(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "llm_detail": {name: _channel() for name in LLM_DIMENSIONS},
        "issues": [_issue()],
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

    def test_the_six_dimensions_are_named(self, schema: dict[str, Any]) -> None:
        assert tuple(schema["properties"]["llm_detail"]["properties"]) == LLM_DIMENSIONS


class TestMirrorsThePydanticModel:
    def test_required_fields_cover_the_model(self, schema: dict[str, Any]) -> None:
        required = {name for name, field in ReviewerOutput.model_fields.items() if field.is_required()}
        assert required <= set(schema["required"])

    def test_issues_is_required_by_the_schema_only(self, schema: dict[str, Any]) -> None:
        """模型有默认值、schema 没有 —— 这是**故意**的（问题清单必须显式给出）。"""
        required = {name for name, field in ReviewerOutput.model_fields.items() if field.is_required()}
        assert set(schema["required"]) - required == {"issues"}

    def test_llm_detail_required_fields_match(self, schema: dict[str, Any]) -> None:
        expected = {name for name, field in LlmChannelDetail.model_fields.items() if field.is_required()}
        assert set(schema["properties"]["llm_detail"]["required"]) == expected

    def test_issue_required_fields_match(self, schema: dict[str, Any]) -> None:
        expected = {name for name, field in ReviewIssue.model_fields.items() if field.is_required()}
        assert set(schema["properties"]["issues"]["items"]["required"]) == expected

    def test_channel_item_required_fields_match(self, schema: dict[str, Any]) -> None:
        expected = {name for name, field in ChannelItem.model_fields.items() if field.is_required()}
        assert set(schema["$defs"]["channel_item"]["required"]) == expected

    def test_score_bounds(self, schema: dict[str, Any]) -> None:
        score = schema["$defs"]["channel_item"]["properties"]["score"]
        assert (score["minimum"], score["maximum"]) == (0, 10)

    def test_comment_bounds(self, schema: dict[str, Any]) -> None:
        comment = schema["$defs"]["channel_item"]["properties"]["comment"]
        assert (comment["minLength"], comment["maxLength"]) == (1, 200)

    def test_severity_enum(self, schema: dict[str, Any]) -> None:
        severity = schema["properties"]["issues"]["items"]["properties"]["severity"]
        assert severity["enum"] == ["block", "major", "minor"]

    def test_target_documents_the_machine_readable_landings(self, schema: dict[str, Any]) -> None:
        target = schema["properties"]["issues"]["items"]["properties"]["target"]
        assert "segment:N" in target["description"]


class TestDeliberateOmissions:
    @pytest.mark.parametrize("field", ABSENT_FIELDS)
    def test_the_model_is_not_asked_for_a_verdict(self, schema: dict[str, Any], field: str) -> None:
        assert field not in schema["properties"]
        assert field not in ReviewerOutput.model_fields

    def test_a_document_that_reports_a_total_is_rejected(self, schema: dict[str, Any]) -> None:
        """模型硬塞一个 total 进来 ⇒ schema 直接拒绝（不然它会被当成真结论用）。"""
        document = _document(total=9.5)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)
        with pytest.raises(ValueError):
            ReviewerOutput.model_validate(document)


class TestBothSidesAgree:
    def test_a_valid_document_passes_both(self, schema: dict[str, Any]) -> None:
        document = _document()
        jsonschema.validate(document, schema)
        assert len(ReviewerOutput.model_validate(document).issues) == 1

    def test_an_empty_issue_list_is_fine(self, schema: dict[str, Any]) -> None:
        document = _document(issues=[])
        jsonschema.validate(document, schema)
        assert ReviewerOutput.model_validate(document).issues == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"llm_detail": {name: _channel() for name in LLM_DIMENSIONS[:-1]}},  # 少一维
            {"llm_detail": {**{name: _channel() for name in LLM_DIMENSIONS}, "extra": _channel()}},
            {"llm_detail": {name: _channel(42) for name in LLM_DIMENSIONS}},  # 分数越界
            {"llm_detail": {name: _channel() for name in LLM_DIMENSIONS} | {"hook_opening": {"score": 8.0}}},
            {"issues": [_issue(severity="fatal")]},
            {"issues": [_issue(target="")]},
            {"issues": [_issue(detail="")]},
        ],
    )
    def test_an_invalid_document_fails_both(self, schema: dict[str, Any], overrides: dict[str, Any]) -> None:
        document = _document(**overrides)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(document, schema)
        with pytest.raises(ValueError):
            ReviewerOutput.model_validate(document)


class TestPromptRegistry:
    def test_reviewer_prompt_is_registered(self) -> None:
        library = PromptLibrary.load(REPO_ROOT / "prompts")
        assert library.verify() == []
        assert "reviewer" in library.names()

    def test_agent_schema_name_points_at_this_file(self) -> None:
        assert SCHEMA_PATH.name == f"{ReviewerAgent.schema_name}.schema.json"

    def test_prompt_version_covers_shared_blocks(self) -> None:
        library = PromptLibrary.load(REPO_ROOT / "prompts")
        combined = library.combined_version("reviewer", "shared.json_contract", "shared.persona_block")
        assert combined != library.prompt_version("reviewer")
