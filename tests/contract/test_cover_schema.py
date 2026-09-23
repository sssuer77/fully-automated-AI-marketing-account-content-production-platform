"""契约：Cover Agent 的输出契约（T5.1 · §04.1.8 / §06.3）。

与 ``test_script_schema.py`` 同一手法：``schemas/cover_result.schema.json``
与 :class:`studio.domain.cover.CoverOutput` 是**同一份契约的两面**，
漂移了就必须红 —— 网关按 schema 校验、服务层按 pydantic 建模，两边不一致时
"schema 过了但模型不过"会变成一次修复重试（另一套预算、另一种失败）。

另外钉住提示词注册表：``prompts/manifest.yaml`` 里的 ``cover`` 条目
必须与文件内容一致（``sha256`` 是**算出来的**，不是抄的）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from studio.agents.cover import CoverAgent
from studio.agents.prompts import PromptLibrary
from studio.core.paths import StudioPaths
from studio.domain.cover import CoverOutput

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "cover_result.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _document(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title_text": "离谱跑酷地图",
        "sub_text": "点个关注",
        "highlight_words": ["跑酷"],
        "frame_at_ms": 500,
        "banned_checked": True,
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

    def test_document_passes(self, schema: dict[str, Any]) -> None:
        jsonschema.Draft202012Validator(schema).validate(_document())


class TestMirrorsThePydanticModel:
    def test_schema_requires_every_field(self, schema: dict[str, Any]) -> None:
        """schema 要求**全部 5 个字段**（提示词里就是这么约定的：每次都要给全）。

        pydantic 那边给 ``sub_text`` / ``highlight_words`` / ``banned_checked`` 留了默认值，
        是为了让**规则兜底**能只写几个字段就造出一份合法输出 —— 那不是漂移，
        而是"模型这条路更严"（网关先过 schema，实际行为以 schema 为准）。
        """
        assert set(schema["required"]) == set(CoverOutput.model_fields)

    def test_schema_covers_the_pydantic_required_ones(self, schema: dict[str, Any]) -> None:
        required = {name for name, field in CoverOutput.model_fields.items() if field.is_required()}
        assert required <= set(schema["required"])

    def test_property_names_match(self, schema: dict[str, Any]) -> None:
        assert set(schema["properties"]) == set(CoverOutput.model_fields)

    def test_title_ceiling(self, schema: dict[str, Any]) -> None:
        assert schema["properties"]["title_text"]["maxLength"] == 20

    def test_sub_ceiling(self, schema: dict[str, Any]) -> None:
        assert schema["properties"]["sub_text"]["maxLength"] == 24

    def test_highlight_words_ceiling(self, schema: dict[str, Any]) -> None:
        assert schema["properties"]["highlight_words"]["maxItems"] == 4


class TestSchemaAndModelAgreeOnRejections:
    """两边**都**必须拒绝同一批输入（只测一边等于没测漂移）。"""

    @pytest.mark.parametrize(
        "document",
        [
            _document(title_text=""),
            _document(title_text="一" * 21),
            _document(sub_text="一" * 25),
            _document(frame_at_ms=-1),
            _document(highlight_words=["a"] * 5),
            {**_document(), "extra": 1},
        ],
    )
    def test_both_reject(self, schema: dict[str, Any], document: dict[str, Any]) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(document)
        with pytest.raises(Exception):  # noqa: B017 —— pydantic 的 ValidationError
            CoverOutput.model_validate(document)

    def test_sub_text_may_be_null_on_both_sides(self, schema: dict[str, Any]) -> None:
        document = _document(sub_text=None)
        jsonschema.Draft202012Validator(schema).validate(document)
        assert CoverOutput.model_validate(document).sub_text is None


class TestPromptRegistry:
    @pytest.fixture
    def library(self) -> PromptLibrary:
        return PromptLibrary.load(StudioPaths(home=REPO_ROOT, data_dir=REPO_ROOT / "data").prompts_dir)

    def test_cover_is_registered(self, library: PromptLibrary) -> None:
        assert "cover" in library.names()

    def test_no_drift(self, library: PromptLibrary) -> None:
        """``sha256`` 与文件内容一致 —— 改一个字就得重算（这是 P5 可复现的基础）。"""
        assert library.verify() == []

    def test_entry_points_at_both_files(self, library: PromptLibrary) -> None:
        entry = library.entry("cover")
        assert entry.system == "cover/system.md"
        assert entry.user == "cover/user.jinja"

    def test_version_is_derivable(self, library: PromptLibrary) -> None:
        version = library.prompt_version("cover")
        assert version.startswith(entry_version(library) + "+")

    def test_renders_without_missing_variables(self, library: PromptLibrary) -> None:
        """缺一个变量 ``render_template`` 会**报错**（不静默渲染成空串）⇒ 这里就是那道闸。"""
        rendered = library.compose(
            "cover",
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
            duration_ms="17482",
            default_frame_ms="500",
            script_title="标题",
            script_hook="钩子",
            script_cta="CTA",
        )
        assert "17482" in rendered.system
        assert "标题" in rendered.user
        assert rendered.prompt_version

    def test_agent_uses_the_registered_name(self) -> None:
        """Agent 的 ``name`` 就是注册名 —— 对不上会 ``LLM_PROMPT_MISSING``。"""
        assert CoverAgent.name == "cover"


def entry_version(library: PromptLibrary) -> str:
    return library.entry("cover").version
