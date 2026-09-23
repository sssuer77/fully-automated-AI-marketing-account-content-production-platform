"""契约：这条片子**必须立一个观点**，且**结尾不得向观众提问**（用户口径）。

真机成稿（2026-09-23，任务 ``01M36FA9NWSX9HWPEJSS0GA543``，692 字 / 50 句）暴露两件事：

1. 稿子全是**口水吵架** —— ``排练啥呀？`` / ``你排练得还挺细。`` / ``赶紧的。`` /
   ``嘿嘿，哥你太懂俺了。``：两个人打岔了一整条片子，**没有一个观点**。根因是
   ``prompts/writer/system.md`` 只要求"对话体、别一个人讲完"，**没有任何**"要立一个
   观点"的纪律 —— 于是模型的默认解就是闲聊。
2. 结尾是 ``评论区打俩字：牛肉还是五仁？俺俩看看哪边人多。``：**向观众提问 + 求互动**。
   根因更直接 —— ``config/persona.yaml`` 的 ``style_hint`` 原文写着"结尾用一句互动式
   提问收尾，引导评论"，模型照做。

这份契约把两件事都钉住：**提示词里必须有立论纪律**（缺一段，那段流水线就会退回
口水话），**人设里不得再有"提问收尾"**（它是最容易被改回来的那一句）。

分工是用户定死的：**熊大输出正确观点（立论方），熊二提出假设（试探方），两人辩论**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from studio.agents.prompts import PromptLibrary
from studio.core.paths import StudioPaths

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 立论纪律必须落到这几条提示词里（各管流水线的一段）。
ARGUMENT_PROMPTS: tuple[str, ...] = ("outliner", "director", "writer", "reviewer", "editor")

#: 每条提示词里那句"要立一个观点"的落点（缺了就等于那段流水线会退回口水话）。
ANCHORS: dict[str, str] = {
    "outliner": "这条片子存在的意义就是把这个观点讲出来",
    "director": "这条片子必须立一个观点",
    "writer": "两个人辩一件事",
    "reviewer": "用一个观点解释一件事",
    "editor": "不许**向观众提问",
}


@pytest.fixture(scope="module")
def library() -> PromptLibrary:
    return PromptLibrary.load(StudioPaths(home=REPO_ROOT, data_dir=REPO_ROOT / "data").prompts_dir)


@pytest.fixture(scope="module")
def persona_doc() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (REPO_ROOT / "config" / "persona.yaml").read_text(encoding="utf-8")
    )
    return loaded


class TestPromptRegistry:
    def test_no_drift(self, library: PromptLibrary) -> None:
        """``sha256`` 与文件内容一致 —— 改一个字就得重算（P5 可复现的基础）。"""
        problems = [p for p in library.verify() if p.split(":")[0] in ARGUMENT_PROMPTS]
        assert problems == []


class TestArgumentDisciplineReachesEveryStage:
    @pytest.mark.parametrize("name", ARGUMENT_PROMPTS)
    def test_the_prompt_carries_the_argument_rule(self, library: PromptLibrary, name: str) -> None:
        assert ANCHORS[name] in library.read_repo(f"{name}/system.md")

    @pytest.mark.parametrize("name", ("writer", "director"))
    def test_the_ending_may_not_be_a_question(self, library: PromptLibrary, name: str) -> None:
        """出口那两个 Agent 都要点名禁问号收尾 —— 只在一处写，另一处会漏。"""
        assert "问号结尾" in library.read_repo(f"{name}/system.md")

    def test_the_outliner_anchors_the_title_to_the_first_level_register(self, library: PromptLibrary) -> None:
        """用户 2026-09-23：一级标题很得体，二级反而写成口语台词 —— 提示词得点名。"""
        text = library.read_repo("outliner/system.md")
        assert "调子对齐一级" in text
        assert "沿用它" in text

    def test_the_outliner_forbids_speaker_labels_in_the_title(self, library: PromptLibrary) -> None:
        text = library.read_repo("outliner/system.md")
        assert "不许出现角色名" in text
        assert "{{speaker_names}}" in text

    def test_the_writer_defines_the_body_as_pure_spoken_text(self, library: PromptLibrary) -> None:
        """`熊大：` 是**格式**，不是内容 —— 提示词得说清谁说的由 speaker 表达。"""
        assert "纯口播全文" in library.read_repo("writer/system.md")

    def test_the_director_spares_the_visual_field(self, library: PromptLibrary) -> None:
        """禁的是观众可见的字；`visual` 是给渲染看的，不能一起禁掉。"""
        assert "`visual` **不受此限**" in library.read_repo("director/system.md")

    def test_the_reviewer_can_flag_a_colloquial_title(self, library: PromptLibrary) -> None:
        text = library.read_repo("reviewer/system.md")
        assert "TITLE_COLLOQUIAL" in text
        assert "`title`（标题）" in text

    def test_the_editor_knows_the_title_is_a_target(self, library: PromptLibrary) -> None:
        assert "- `title` ⇒ 标题" in library.read_repo("editor/system.md")

    def test_the_writer_forbids_filler_talk(self, library: PromptLibrary) -> None:
        """口水话是这次真机成稿的主症（"排练啥呀？""赶紧的。"）—— 得点名禁掉。"""
        assert "禁止口水话" in library.read_repo("writer/system.md")

    def test_the_reviewer_can_flag_a_question_ending(self, library: PromptLibrary) -> None:
        """审稿要能把它判下去，否则改稿环节没有依据。"""
        text = library.read_repo("reviewer/system.md")
        assert "ENDING_QUESTION" in text and "NO_ARGUMENT" in text


class TestPersonaDoesNotAskForAQuestionEnding:
    """根因那条：``style_hint`` 原文写着"结尾用一句互动式提问收尾，引导评论"。"""

    def test_style_hint_no_longer_asks_for_a_question(self, persona_doc: dict[str, Any]) -> None:
        assert "互动式提问" not in persona_doc["style_hint"]
        assert "不许向观众提问" in persona_doc["style_hint"]

    def test_role_desc_fixes_the_two_roles(self, persona_doc: dict[str, Any]) -> None:
        assert "熊大负责立论" in persona_doc["role_desc"]
        assert "熊二负责提出假设" in persona_doc["role_desc"]

    def test_the_shipped_baseline_says_the_same_thing(self) -> None:
        """``config/personas/persona_default.yaml`` 是出厂基线 —— 两边口径必须一致。"""
        text = (REPO_ROOT / "config" / "personas" / "persona_default.yaml").read_text(encoding="utf-8")
        assert "熊大负责立论" in text
        assert "熊二负责提出假设" in text
        assert "互动式提问" not in text
