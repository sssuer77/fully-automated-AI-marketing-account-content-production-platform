"""契约：NewsScout Agent 的输出契约（T5.12 · §04.1.7 同族）。

与 ``test_cover_schema.py`` 同一手法：``schemas/news_scout.schema.json`` 与
:class:`studio.domain.topics.NewsScoutResult` 是**同一份契约的两面**，漂移了就必须红 ——
网关按 schema 校验、服务层按 pydantic 建模，两边不一致时"schema 过了但模型不过"会变成
一次修复重试（另一套预算、另一种失败）。

另外钉住提示词注册表：``prompts/manifest.yaml`` 里的 ``news_scout`` 条目必须与文件内容
一致（``sha256`` 是**算出来的**，不是抄的）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from studio.agents.news_scout import NewsScoutAgent
from studio.agents.prompts import PromptLibrary
from studio.core.paths import StudioPaths
from studio.domain.topics import (
    NEWS_DIRECTION_TITLE_MAX,
    NEWS_RATIONALE_MAX,
    NEWS_SUMMARY_MAX,
    NewsScoutResult,
    NewsVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "news_scout.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _verdict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ref": "n01",
        "keep": True,
        "direction_title": "聚能环致死：省钱省出的命案",
        "rationale": "有冲突也有具体后果，讲得出来",
        "event_summary": "一家人用聚能环取暖，三人一氧化碳中毒身亡。",
    }
    payload.update(overrides)
    return payload


def _document(*items: dict[str, Any]) -> dict[str, Any]:
    return {"items": list(items) or [_verdict()]}


class TestSchemaIsWellFormed:
    def test_file_exists(self) -> None:
        assert SCHEMA_PATH.is_file()

    def test_is_valid_json_schema(self, schema: dict[str, Any]) -> None:
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_forbids_extra_fields(self, schema: dict[str, Any]) -> None:
        assert schema["additionalProperties"] is False

    def test_document_passes(self, schema: dict[str, Any]) -> None:
        jsonschema.Draft202012Validator(schema).validate(_document())


class TestMirrorsThePydanticModel:
    def test_schema_requires_every_verdict_field(self, schema: dict[str, Any]) -> None:
        """每一条判定都要给全 5 个字段（提示词里就是这么约定的）。

        pydantic 那边给 ``keep`` / ``direction_title`` / ``rationale`` / ``event_summary``
        留了默认值，是为了让服务层能只写 ``ref`` 就造一份合法输出 —— 那不是漂移，
        而是"模型这条路更严"。
        """
        assert set(schema["properties"]["items"]["items"]["required"]) == {
            "ref",
            "keep",
            "direction_title",
            "rationale",
            "event_summary",
        }

    def test_property_names_match(self, schema: dict[str, Any]) -> None:
        assert set(schema["properties"]["items"]["items"]["properties"]) == set(NewsVerdict.model_fields)

    def test_title_ceiling(self, schema: dict[str, Any]) -> None:
        assert (
            schema["properties"]["items"]["items"]["properties"]["direction_title"]["maxLength"]
            == NEWS_DIRECTION_TITLE_MAX
        )

    def test_rationale_ceiling(self, schema: dict[str, Any]) -> None:
        assert (
            schema["properties"]["items"]["items"]["properties"]["rationale"]["maxLength"]
            == NEWS_RATIONALE_MAX
        )

    def test_summary_ceiling(self, schema: dict[str, Any]) -> None:
        """事件总结的上限两边一致 —— 它会原样进写稿的提示词，超了就该在网关上被打回。"""
        assert (
            schema["properties"]["items"]["items"]["properties"]["event_summary"]["maxLength"]
            == NEWS_SUMMARY_MAX
        )

    def test_ref_must_not_be_empty(self, schema: dict[str, Any]) -> None:
        assert schema["properties"]["items"]["items"]["properties"]["ref"]["minLength"] == 1


class TestSchemaAndModelAgreeOnRejections:
    """两边**都**必须拒绝同一批输入（只测一边等于没测漂移）。"""

    @pytest.mark.parametrize(
        "document",
        [
            _document(_verdict(ref="")),
            _document(_verdict(direction_title="一" * (NEWS_DIRECTION_TITLE_MAX + 1))),
            _document(_verdict(rationale="一" * (NEWS_RATIONALE_MAX + 1))),
            _document(_verdict(event_summary="一" * (NEWS_SUMMARY_MAX + 1))),
            {**_document(), "extra": 1},
        ],
    )
    def test_both_reject(self, schema: dict[str, Any], document: dict[str, Any]) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(document)
        with pytest.raises(Exception):  # noqa: B017 —— pydantic 的 ValidationError
            NewsScoutResult.model_validate(document)

    def test_keep_must_be_a_boolean_on_the_schema_side(self, schema: dict[str, Any]) -> None:
        """``keep`` 不是布尔 ⇒ **schema 拒**（网关据此回灌重试）。

        这里只钉 schema 那一面：pydantic 在宽松模式下会把 ``"yes"`` 收成 ``True``，而真正
        的那道闸是 schema（网关先过它），所以"两边都拒"这条断言在它身上不成立 —— 与其把
        ``strict=True`` 塞进领域模型（那是另一套语义，别的模型都没这么写），不如把这件事
        写清楚。
        """
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(_document(_verdict(keep="yes")))

    def test_a_skipped_item_needs_no_title_on_either_side(self, schema: dict[str, Any]) -> None:
        document = _document(_verdict(keep=False, direction_title="", rationale="只有赛果"))
        jsonschema.Draft202012Validator(schema).validate(document)
        assert NewsScoutResult.model_validate(document).items[0].keep is False


class TestPromptRegistry:
    @pytest.fixture
    def library(self) -> PromptLibrary:
        return PromptLibrary.load(StudioPaths(home=REPO_ROOT, data_dir=REPO_ROOT / "data").prompts_dir)

    def test_news_scout_is_registered(self, library: PromptLibrary) -> None:
        assert "news_scout" in library.names()

    def test_no_drift(self, library: PromptLibrary) -> None:
        """``sha256`` 与文件内容一致 —— 改一个字就得重算（这是 P5 可复现的基础）。"""
        assert library.verify() == []

    def test_entry_points_at_both_files(self, library: PromptLibrary) -> None:
        entry = library.entry("news_scout")
        assert entry.system == "news_scout/system.md"
        assert entry.user == "news_scout/user.jinja"

    def test_renders_without_missing_variables(self, library: PromptLibrary) -> None:
        """缺一个变量 ``render_template`` 会**报错**（不静默渲染成空串）⇒ 这里就是那道闸。"""
        rendered = library.compose(
            "news_scout",
            system_blocks=("shared.json_contract",),
            user_blocks=("shared.persona_block",),
            persona_name="熊大熊二",
            role_desc="跑酷解说",
            tone="嘴碎",
            audience="学生",
            catchphrases="这不科学、俺寻思",
            forbidden="脏话",
            speaker_names="熊大、熊二",
            style_hint="（无）",
            item_count="2",
            items_block="- [n01]（今日头条热榜）聚能环致一家三口身亡",
        )
        assert "2" in rendered.user
        assert "n01" in rendered.user
        assert rendered.prompt_version

    def test_agent_uses_the_registered_name(self) -> None:
        """Agent 的 ``name`` 就是注册名 —— 对不上会 ``LLM_PROMPT_MISSING``。"""
        assert NewsScoutAgent.name == "news_scout"
        assert NewsScoutAgent.schema_name == "news_scout"
