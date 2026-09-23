"""契约：选题不得把**画面**写进内容（§04.1.3 落地口径 6）。

真机踩到（2026-09-23 用户报障）：选题候选里一直冒「熊大用跑酷台阶算给你看」这类标题，
而**跑酷只是底片**（渲染时从素材库随机挑一条），与选题无关。追下去两个原因：

1. ``prompts/ideator/system.md`` 第 4 条写着「3 分钟内能做完（**跑酷素材循环** + 配音 +
   字幕）」—— 提示词把"这条视频的画面是跑酷"教给了模型，模型就照着把选题编成游戏实况。
   判据：**一批方向是「张雪峰过劳猝死」的选题，5 条里 5 条带跑酷** —— 而那时库里一条
   跑酷选题都没有。所以根因在提示词，不在历史数据。
2. 那之后 ``【已有选题（不得重复）】`` 里全是跑酷标题 ⇒ 模型"为了不重复"只在跑酷母题里
   换说法（换关卡、换机制、换数值），越写越跑酷，**永远出不来**。

这份契约钉住的是第 1 条：**画面词不得出现在提示词的"正面要求"里**。
第 2 条由提示词里的「不要跟着学」那句 + 服务层的 warn 兜（见
``domain/topics.visual_leak_words``）。

为什么不写成"提示词里不许出现跑酷"：那句话**必须**能出现在**禁令**里 ——
不许出现的是"跑酷"被当成正面示例（"素材循环就是跑酷"）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from studio.agents.prompts import PromptLibrary
from studio.core.paths import StudioPaths
from studio.domain.topics import VISUAL_LEAK_WORDS, visual_leak_words

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 本次改动守的三个条目（其余条目各由自己的契约测试守）。
OWNED: tuple[str, ...] = ("ideator", "cover", "writer")

#: 提示词里**正面**出现就算漏的素材词（``VISUAL_LEAK_WORDS`` 是判据的那一份）。
ASSET_WORDS: tuple[str, ...] = (*VISUAL_LEAK_WORDS, "MC", "实况", "游戏画面")


@pytest.fixture(scope="module")
def library() -> PromptLibrary:
    return PromptLibrary.load(StudioPaths(home=REPO_ROOT, data_dir=REPO_ROOT / "data").prompts_dir)


class TestPromptRegistry:
    def test_no_drift(self, library: PromptLibrary) -> None:
        """``sha256`` 与文件内容一致 —— 改一个字就得重算（这是 P5 可复现的基础）。"""
        problems = [p for p in library.verify() if p.split(":")[0] in OWNED]
        assert problems == []

    def test_owned_entries_are_registered(self, library: PromptLibrary) -> None:
        for name in OWNED:
            assert name in library.names()


class TestIdeatorKeepsVisualsOutOfTopics:
    """**根因那条**：``ideator/system.md`` 不得把画面当成选题的一部分。"""

    def test_the_asset_loop_phrase_is_gone(self, library: PromptLibrary) -> None:
        """钉住那次改动本身：``跑酷素材循环`` 是当初漏进提示词的那五个字。"""
        assert "跑酷素材循环" not in library.read_repo("ideator/system.md")

    def test_visual_words_only_appear_in_the_prohibition(self, library: PromptLibrary) -> None:
        """``跑酷`` 这类词**可以**出现在「画面不参与选题」那一段里（那是禁令），
        但**不许**出现在它之前 —— 之前那部分才是"要什么"。

        这一条比"整篇不许出现跑酷"准：禁令里不点名，模型就认不出历史标题里那个词
        是要避开的。
        """
        text = library.read_repo("ideator/system.md")
        head, separator, tail = text.partition("【画面不参与选题】")
        assert separator, "禁令那一段被删掉了 —— 它是这次修复的落点"
        for word in ASSET_WORDS:
            assert word not in head, f"「{word}」出现在要求里（那是把画面当成选题内容）"
        assert "跑酷" in tail, "禁令里必须点名，否则模型认不出历史标题里的那个词"

    def test_the_existing_titles_block_warns_about_legacy(self, library: PromptLibrary) -> None:
        """第二条原因（自我强化）也要有对应的字：不然模型会照抄历史标题的画风。"""
        text = library.read_repo("ideator/system.md")
        assert "不要跟着学" in text

    def test_renders_without_missing_variables(self, library: PromptLibrary) -> None:
        rendered = library.compose(
            "ideator",
            system_blocks=("shared.json_contract",),
            user_blocks=("shared.persona_block",),
            persona_name="熊大熊二",
            role_desc="兄弟对话体",
            tone="东北话",
            audience="学生",
            catchphrases="熊大你听我说",
            forbidden="脏话",
            speaker_names="熊大、熊二",
            style_hint="（无）",
            direction_title="老楼装电梯",
            direction_rationale="民生争议",
            direction_grounding="（无）",
            existing_block="（暂无，放心写）",
            per_direction="4",
        )
        assert "老楼装电梯" in rendered.user
        assert "画面不参与选题" in rendered.system
        assert rendered.prompt_version


class TestWriterSeesVisualsAsNotForTheScript:
    def test_the_visual_line_is_marked_not_for_the_script(self, library: PromptLibrary) -> None:
        """Writer 的 user 模板里带着大纲的「画面」行 —— 不标清楚，它会写进口播稿。"""
        text = library.read_repo("writer/user.jinja")
        assert "给渲染看的" in text


class TestVisualLeakScanner:
    """服务层那条 warn 的判据（``domain/topics.visual_leak_words``）。"""

    @pytest.mark.parametrize(
        "text",
        [
            "熊大用跑酷台阶算给你看",
            "熊二在MC里跑了一晚上",
            "血条见底了，系统可不会提醒你",
            "第几关会趴下？",
        ],
    )
    def test_catches_the_leak(self, text: str) -> None:
        assert visual_leak_words(text)

    @pytest.mark.parametrize(
        "text",
        [
            "老楼装电梯9000部，这钱谁掏？",
            "500万的房子月租1万该买还是租",
            "MCU 新片定档",  # MC 后面跟着字母 ⇒ 不是那个游戏
            "地图 App 又更新了",  # 「地图」刻意不进判据（民生选题里是正经词）
        ],
    )
    def test_does_not_cry_wolf(self, text: str) -> None:
        assert visual_leak_words(text) == []
