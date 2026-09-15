"""文本归一化（T2.5 · §04.3.6）—— 黄金用例 + 幂等性 + 词表校验 + 热重载。

为什么用例要"黄金"而不是"大概对"
--------------------------------
归一化的输出会**烧进成片**（字幕）并**喂进引擎**（配音），两条路都改不了口。
所以这里断言的是**逐字相等**，不是"包含关系" —— 一个多出来的空格就足以让
句级缓存（T2.6）算出另一个键，让同一句话重算一遍。

幂等性为什么单列一节
--------------------
``normalize(normalize(x)) == normalize(x)`` 是**缓存的前提**，不是"顺手做到"。
它由词表加载期校验结构性保证（见 :func:`test_value_containing_key_is_rejected`），
本节再把"真词表 + 一批难缠输入"整体跑一遍两轮，确认没有漏网之鱼。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from studio.core.errors import ConfigError, ErrorCode
from studio.tts.text_normalize import (
    DEFAULT_PAUSE_MS,
    PAUSE_MAP,
    Glossary,
    GlossaryStore,
    load_glossary,
    normalize,
    pause_after_ms,
    validate_glossary,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_GLOSSARY = REPO_ROOT / "prompts" / "shared" / "glossary.yaml"

GLOSSARY = load_glossary(REPO_GLOSSARY)


# ══════════════════════════════════════════════════════════════════════
# 1. 黄金用例（§04.3.6 五步，逐字断言）
# ══════════════════════════════════════════════════════════════════════

#: (原始文本, 期望输出)。注释标的是它守哪一步 —— 挂掉时一眼看出是"哪一步坏了"。
GOLDEN: list[tuple[str, str]] = [
    # ── 步骤 1：Markdown ───────────────────────────────────────
    ("**震惊！**这个跑酷地图", "震惊！这个跑酷地图"),
    ("# 一级标题", "一级标题"),
    ("### 三级标题", "三级标题"),
    ("> 引用一段话", "引用一段话"),
    ("- 列表项", "列表项"),
    ("* 星号列表项", "星号列表项"),
    ("1. 有序列表项", "有序列表项"),
    ("2) 另一种有序列表", "另一种有序列表"),
    ("[看这里](http://example.com)", "看这里"),
    ("![封面图](cover.png)", ""),
    ("`代码` 要念出来", "代码要念出来"),
    ("~~~\nprint(1)\n~~~", ""),
    ("| 列一 | 列二 |", ""),
    ("---", ""),
    ("~~删除线~~留着", "删除线留着"),
    # ── 步骤 1：emoji 与不可朗读符号 ───────────────────────────
    ("跑酷🎉生存", "跑酷生存"),
    ("👉点这里", "点这里"),
    ("🔥🔥🔥", ""),
    ("等等...", "等等。"),
    ("OK.", "OK。"),
    ("好.走", "好。走"),
    ('他说"你好"', "他说你好"),
    # ── 步骤 2：数字 ──────────────────────────────────────────
    ("2026年", "二零二六年"),
    ("2026-09-15", "二零二六年九月十五日"),
    ("2026/9/5", "二零二六年九月五日"),
    ("9月15日", "九月十五日"),
    ("3号选手", "三号选手"),
    ("第3名", "第三名"),
    ("50%", "百分之五十"),
    ("3.5%", "百分之三点五"),
    ("3.14", "三点一四"),
    ("1200", "一千二百"),
    ("15", "十五"),
    ("100", "一百"),
    ("105", "一百零五"),
    ("100000001", "一亿零一"),
    ("3-5个", "三到五个"),
    ("12:30", "十二点三十"),
    # ── 步骤 2：单位（只在紧跟数字时生效）─────────────────────
    ("5km", "五公里"),
    ("5min", "五分钟"),
    ("5ms", "五毫秒"),
    ("5m", "五米"),
    ("5GB", "五G"),
    # ── 步骤 3：英文缩写 ──────────────────────────────────────
    ("AI", "诶哎"),
    ("FFmpeg", "F F m peg"),
    ("MC跑酷", "M C跑酷"),
    ("用GPU跑", "用G P U跑"),
    # ── 步骤 4：多音字 / 易错词 ───────────────────────────────
    ("银行行长", "银航行长"),
    ("重要的事情", "众要的事情"),
    ("主角登场", "主决登场"),
    ("因为所以", "因位所以"),
    ("重新开始", "崇新开始"),
    # ── 步骤 5：标点与空白 ────────────────────────────────────
    ("好,走!", "好，走！"),
    ("(笑)", "（笑）"),
    ("跑酷-生存", "跑酷生存"),
    ("a/b", "ab"),
    ("好。。走", "好。走"),
    ("跑 酷 生 存", "跑酷生存"),
    ("  前后有空格  ", "前后有空格"),
]


@pytest.mark.parametrize(("raw", "expected"), GOLDEN)
def test_golden(raw: str, expected: str) -> None:
    assert normalize(raw, glossary=GLOSSARY) == expected


def test_golden_covers_at_least_40_cases() -> None:
    """§T2.5 验收线：**≥40 条**黄金用例。数量本身是被断言的对象。"""
    assert len(GOLDEN) >= 40


# ══════════════════════════════════════════════════════════════════════
# 2. 幂等性
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("raw", [case[0] for case in GOLDEN] + ["", "   ", "。。。"])
def test_idempotent_on_golden_inputs(raw: str) -> None:
    once = normalize(raw, glossary=GLOSSARY)
    assert normalize(once, glossary=GLOSSARY) == once


def test_idempotent_on_messy_paragraph() -> None:
    """一段"什么都掺了"的文本，跑两轮必须收敛。"""
    raw = (
        "# 标题🔥\n"
        "**2026年9月15日**，我用 `FFmpeg` 压了 5GB 素材，只花了 5min；\n"
        "- 涨了 3.14 倍（50% 的人不知道）\n"
        '> 银行行长说："重要的事情说三遍"……\n'
        "[原文](http://example.com)  \n"
    )
    once = normalize(raw, glossary=GLOSSARY)
    assert normalize(once, glossary=GLOSSARY) == once
    assert "二零二六年九月十五日" in once
    assert "F F m peg" in once
    assert "百分之五十" in once
    assert "银航行长" in once
    assert "example.com" not in once


def test_normalize_is_single_pass_even_with_cascading_map() -> None:
    """绕过加载路径直接构造的词表也不会**级联**替换。

    ``A→B`` 且 ``B→C``：两遍 ``str.replace`` 会把 ``A`` 一路换成 ``C``，
    单遍扫描只会换成 ``B``。加载期校验本就把这种词表拦在门外
    （见 :func:`test_value_containing_key_is_rejected`），这里堵的是第二层。
    """
    glossary = Glossary(terms={"A": "B", "B": "C"})
    assert normalize("A", glossary=glossary) == "B"


def test_dot_between_ascii_words_is_not_a_sentence_break() -> None:
    """域名里的点不能变成句号：那会在词中间插一个 420ms 停顿，还会多切一句。"""
    out = normalize("访问 a.b.com 看看", glossary=GLOSSARY)
    assert "。" not in out
    assert out == "访问abcom看看"


def test_empty_text_returns_empty() -> None:
    assert normalize("", glossary=GLOSSARY) == ""


def test_subtitle_mode_keeps_digits_and_halfwidth_punct() -> None:
    """字幕模式：去 Markdown/emoji + 词表，但**保留数字与半角标点**。"""
    raw = "**2026年9月15日**，用AI做的,第3集"
    out = normalize(raw, glossary=GLOSSARY, mode="subtitle")
    assert out == "2026年9月15日，用诶哎做的,第3集"


# ══════════════════════════════════════════════════════════════════════
# 3. 停顿映射（步骤 5：不改变文本）
# ══════════════════════════════════════════════════════════════════════


def test_pause_map_matches_contract() -> None:
    """数值就是契约（§04.3.6 原表），改动必须是有意的。"""
    assert PAUSE_MAP == {
        "，": 180,
        "、": 120,
        "；": 260,
        "：": 240,
        "。": 420,
        "？": 480,
        "！": 460,
        "……": 520,
        "——": 300,
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [("你好。", 420), ("你好？", 480), ("你好！", 460), ("你好……", 520), ("你好", DEFAULT_PAUSE_MS)],
)
def test_pause_after_ms(text: str, expected: int) -> None:
    assert pause_after_ms(text) == expected


def test_pause_prefers_longest_mark() -> None:
    """``……`` 不能被当成两个句号（420×2）—— 那是 840ms 的沉默。"""
    assert pause_after_ms("他愣了一下……") == PAUSE_MAP["……"]


def test_pause_map_does_not_change_text() -> None:
    """停顿是**元数据**：归一化输出里不许出现停顿标记。"""
    out = normalize("你好。走。", glossary=GLOSSARY)
    assert out == "你好。走。"


# ══════════════════════════════════════════════════════════════════════
# 4. 词表加载期校验（幂等性的结构性保证）
# ══════════════════════════════════════════════════════════════════════


def _yaml(body: str) -> str:
    return "schema_version: '1.0'\n" + body


def test_repo_glossary_is_valid() -> None:
    """仓库里那一份必须是干净的 —— 它是要被改的，改坏要在这里先炸。"""
    assert GLOSSARY.abbreviations
    assert GLOSSARY.terms
    assert GLOSSARY.units
    validate_glossary(GLOSSARY, path=REPO_GLOSSARY)


def test_value_containing_key_is_rejected(tmp_path: Path) -> None:
    """规则 3：value 含 key ⇒ 不幂等 ⇒ 拒绝加载。"""
    path = tmp_path / "glossary.yaml"
    path.write_text(_yaml("terms:\n  A: AB\n  B: C\n"), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_glossary(path)
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    reasons = " ".join(item["reason"] for item in excinfo.value.context["problems"])
    assert "不幂等" in reasons


def test_duplicate_key_across_tables_is_rejected(tmp_path: Path) -> None:
    """规则 2：同一个 key 出现在两张表里 ⇒ "哪张表先生效"的悬案。"""
    path = tmp_path / "glossary.yaml"
    path.write_text(_yaml("abbreviations:\n  AI: 诶哎\nterms:\n  AI: 爱\n"), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_glossary(path)
    assert "两张表" in str(excinfo.value.context["problems"])


def test_empty_key_or_value_is_rejected(tmp_path: Path) -> None:
    """规则 1：空 key 会匹配在每个字符之间；空 value 等于删字。"""
    path = tmp_path / "glossary.yaml"
    path.write_text(_yaml("terms:\n  '': 空键\n"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_glossary(path)


def test_units_case_collision_is_rejected(tmp_path: Path) -> None:
    """``m`` 与 ``M`` 同时存在 ⇒ "哪个赢"的悬案（匹配是大小写不敏感的）。"""
    path = tmp_path / "glossary.yaml"
    path.write_text(_yaml("units:\n  m: 米\n  M: 兆\n"), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_glossary(path)
    assert "撞车" in str(excinfo.value.context["problems"])


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    """拼错的字段名不能静默忽略（与人物库同一条：``catchphrase`` ≠ ``catchphrases``）。"""
    path = tmp_path / "glossary.yaml"
    path.write_text(_yaml("term:\n  AI: 诶哎\n"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_glossary(path)


def test_missing_file_raises_config_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_glossary(tmp_path / "nope.yaml")
    assert excinfo.value.code is ErrorCode.CONFIG_MISSING


def test_broken_yaml_raises_config_invalid(tmp_path: Path) -> None:
    path = tmp_path / "glossary.yaml"
    path.write_text("terms: [未闭合\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_glossary(path)
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


# ══════════════════════════════════════════════════════════════════════
# 5. 热重载（T2.5：改一行 YAML 立刻生效）
# ══════════════════════════════════════════════════════════════════════


def _write(path: Path, text: str, *, tick: int) -> None:
    """写文件并**显式钉住 mtime**。

    不靠"写完等一会儿"：Windows 的系统时钟粒度约 15ms，两次相邻写入完全可能拿到
    同一个 ``st_mtime_ns`` ⇒ 热重载用例会变成"有时过有时不过"。
    """
    path.write_text(text, encoding="utf-8")
    # 步长必须 ≫ NTFS 的 100ns 时间戳分辨率：差 1ns 会被舍入成同一个值，
    # 热重载用例就变成"有时过有时不过"
    stamp = 1_700_000_000_000_000_000 + tick * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def test_store_hot_reloads_on_change(tmp_path: Path) -> None:
    path = tmp_path / "glossary.yaml"
    _write(path, _yaml("abbreviations:\n  AI: 诶哎\n"), tick=1)
    store = GlossaryStore(path)
    assert store.current().abbreviations == {"AI": "诶哎"}
    first = store.version
    assert first

    _write(path, _yaml("abbreviations:\n  AI: 哎诶\n"), tick=2)
    assert store.current().abbreviations == {"AI": "哎诶"}
    assert store.version != first
    assert store.last_error is None


def test_store_keeps_last_good_when_file_breaks(tmp_path: Path) -> None:
    """运行中改坏 ⇒ 保住上一份好的并记一条 error，**不打断在跑的任务**。"""
    path = tmp_path / "glossary.yaml"
    _write(path, _yaml("terms:\n  银行: 银航\n"), tick=1)
    store = GlossaryStore(path)
    good = store.current()

    _write(path, _yaml("terms:\n  A: AB\n  B: C\n"), tick=2)  # 违反规则 3
    assert store.current() == good
    assert isinstance(store.last_error, ConfigError)

    # 文件一直坏着：不重复重读、也不再重复告警（否则一次坏文件 = 每次请求一条告警）
    assert store.current() == good
    assert store.last_error is not None

    # 改好之后自动恢复
    _write(path, _yaml("terms:\n  银行: 银航\n  重要: 众要\n"), tick=3)
    assert store.current().terms == {"银行": "银航", "重要": "众要"}
    assert store.last_error is None


def test_store_missing_file_on_startup_raises(tmp_path: Path) -> None:
    """启动时缺文件 = 装错了 ⇒ 拒绝启动（doctor 能提前拦下）。"""
    store = GlossaryStore(tmp_path / "nope.yaml")
    with pytest.raises(ConfigError) as excinfo:
        store.current()
    assert excinfo.value.code is ErrorCode.CONFIG_MISSING


def test_store_reload_forces_reread(tmp_path: Path) -> None:
    path = tmp_path / "glossary.yaml"
    _write(path, _yaml("terms:\n  银行: 银航\n"), tick=1)
    store = GlossaryStore(path)
    assert store.current().terms == {"银行": "银航"}
    path.write_text(_yaml("terms:\n  银行: 银航\n  重要: 众要\n"), encoding="utf-8")
    assert store.reload().terms == {"银行": "银航", "重要": "众要"}
