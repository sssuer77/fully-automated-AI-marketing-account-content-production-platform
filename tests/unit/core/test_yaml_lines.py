"""YAML 行级标量编辑（T4.7 · §04.5.9）—— 「只改一个标量，别的字节一个都不动」。

为什么值得单独测
----------------
面板的保存路径是「读整份文件 -> 逐行替换 -> 写回」。这条路径最贵的失败不是"改错了值"，
而是"顺手把别的东西也改了"：注释被吃掉、缩进被重排、文件末尾凭空多一个换行。这些差异
在 diff 里很小、在"回退时看不懂这行原来是干什么的"这件事上很大。

所以本文件里最硬的一条断言是**恒等变换**：拿真 `config/outputs.yaml`，把**每一个可编辑
标量**按原值写回去，逐字节必须还是原来那一份（:func:`test_set_scalar_is_the_identity_on_every_editable_path`）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.outputs_store import (
    PROFILE_FIELDS,
    SUBTITLE_FIELDS,
    WATERMARK_FIELDS,
    quality_field_of,
)
from studio.core.yaml_lines import (
    find_scalar,
    render_scalar,
    set_scalar,
    split_inline_comment,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUTS_YAML = REPO_ROOT / "config" / "outputs.yaml"


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _editable_paths() -> list[tuple[str, ...]]:
    """`config/outputs.yaml` 上**全部**可编辑路径（面板能碰到的每一个标量）。

    从**已校验的模型**上现取（而不是在测试里再抄一份字段表）：面板能改的字段与这里
    走的是同一份 `PROFILE_FIELDS` / `WATERMARK_FIELDS` / `SUBTITLE_FIELDS`。
    """
    config = load_outputs_config(OUTPUTS_YAML)
    paths: list[tuple[str, ...]] = [("default_profile",)]
    for name, profile in config.profiles.items():
        for field in PROFILE_FIELDS:
            key = quality_field_of(profile.vcodec) if field == "quality" else field
            paths.append(("profiles", name, key))
    paths.extend(("watermark", field) for field in WATERMARK_FIELDS)
    paths.extend(("subtitle", field) for field in SUBTITLE_FIELDS)
    return paths


def _value_at(config: OutputsConfig, path: tuple[str, ...]) -> object:
    """按键路径从**模型**上取值（写回文件时用的就是它）。"""
    if path == ("default_profile",):
        return config.default_profile
    if path[0] == "profiles":
        return getattr(config.profiles[path[1]], path[2])
    return getattr(getattr(config, path[0]), path[1])


# ══════════════════════════════════════════════════════════════════════
# 拆注释
# ══════════════════════════════════════════════════════════════════════


def test_split_inline_comment_keeps_the_whitespace_before_it() -> None:
    """注释段**含**它前面的空白 —— 于是「拼回去」对未改动的行是恒等变换。"""
    assert split_inline_comment("0.85  # 上限 1.0") == ("0.85", "  # 上限 1.0")
    assert split_inline_comment("bottom_right        # top_left | top_right") == (
        "bottom_right",
        "        # top_left | top_right",
    )


def test_split_inline_comment_needs_whitespace_before_the_hash() -> None:
    """`#` 前面没有空白 ⇒ 它是值的一部分（`A#B` 是一个字符串，不是注释）。"""
    assert split_inline_comment("A#B") == ("A#B", "")
    assert split_inline_comment('"a # b"') == ('"a # b"', "")
    assert split_inline_comment("'a # b'") == ("'a # b'", "")


def test_split_inline_comment_handles_an_empty_value() -> None:
    """`watermark:` 这种块开头没有值（`:func:`find_scalar` 靠它压栈）。"""
    assert split_inline_comment("") == ("", "")
    assert split_inline_comment("  # 整行注释") == ("", "  # 整行注释")


def test_split_inline_comment_round_trips_every_real_line() -> None:
    """真文件里每一行「值 + 注释」都要拼得回去（否则未改动的行也会漂）。"""
    for raw in _lines(OUTPUTS_YAML):
        body = raw.rstrip("\r\n")
        if body.strip().startswith("#"):
            continue
        value, comment = split_inline_comment(body)
        assert value + comment == body.rstrip(), body


# ══════════════════════════════════════════════════════════════════════
# 渲染标量
# ══════════════════════════════════════════════════════════════════════


def test_render_scalar_keeps_numbers_as_numbers() -> None:
    assert render_scalar(48) == "48"
    assert render_scalar(0.22) == "0.22"
    assert render_scalar(0.85) == "0.85"
    assert render_scalar(True) == "true"
    assert render_scalar(False) == "false"


def test_render_scalar_round_trips_a_whole_number_float() -> None:
    """`1.0` 必须写成 `1.0`，不能写成 `1`。

    真机（2026-09-22）：`config/outputs.yaml` 里的 `opacity: 1.0` 是人手写的，而面板
    保存一次会把它改成 `1` —— 语义没变，但"按原值写回去还是原来那一份"这条地基塌了
    （`test_set_scalar_is_the_identity_on_every_editable_path` 当场红）。
    """
    assert render_scalar(1.0) == "1.0"
    assert render_scalar(0.0) == "0.0"
    assert yaml.safe_load(render_scalar(1.0)) == 1.0


def test_render_scalar_quotes_anything_yaml_would_read_as_something_else() -> None:
    """宁可多引一对，也不要出现「面板上写的是字符串、回读变成布尔」这种怪事。"""
    assert render_scalar("bottom_right") == "bottom_right"
    assert render_scalar("0.85") == '"0.85"'
    assert render_scalar("yes") == '"yes"'
    assert render_scalar("null") == '"null"'
    assert render_scalar("a: b") == '"a: b"'
    assert render_scalar("a#b") == '"a#b"'
    assert render_scalar("带 空格 的中文") == '"带 空格 的中文"'


# ══════════════════════════════════════════════════════════════════════
# 找路径
# ══════════════════════════════════════════════════════════════════════


def test_find_scalar_walks_nested_block_mappings() -> None:
    lines = _lines(OUTPUTS_YAML)
    assert find_scalar(lines, ("default_profile",)) is not None
    assert find_scalar(lines, ("watermark", "margin_x")) is not None
    assert find_scalar(lines, ("profiles", "fallback_720x1280_v1", "cq")) is not None
    assert find_scalar(lines, ("subtitle", "max_chars_per_line")) is not None


def test_find_scalar_returns_none_instead_of_guessing() -> None:
    lines = _lines(OUTPUTS_YAML)
    assert find_scalar(lines, ("watermark", "nope")) is None
    assert find_scalar(lines, ("profiles", "nope_v1", "width")) is None
    assert find_scalar(lines, ("watermark", "margin_x", "deeper")) is None
    assert find_scalar(lines, ("audio", "margin_x")) is None


def test_find_scalar_does_not_confuse_same_named_keys_in_different_blocks() -> None:
    """同名键在 `watermark` / `audio` 下都可能有 —— 路径栈必须认父级。"""
    lines = [
        "watermark:\n",
        "  margin_x: 48\n",
        "audio:\n",
        "  margin_x: 1\n",
    ]
    assert find_scalar(lines, ("watermark", "margin_x")) == 1
    assert find_scalar(lines, ("audio", "margin_x")) == 3


def test_find_scalar_ignores_a_same_named_key_that_is_not_a_block_parent() -> None:
    """有值的行不可能是块的开头：`subtitle: enabled` 之后不该把 `enabled` 当父级。"""
    lines = [
        "subtitle:\n",
        "  enabled: true\n",
        "  font_size: 64\n",
    ]
    assert find_scalar(lines, ("subtitle", "font_size")) == 2
    assert find_scalar(lines, ("subtitle", "enabled", "font_size")) is None


# ══════════════════════════════════════════════════════════════════════
# 改标量
# ══════════════════════════════════════════════════════════════════════


def test_set_scalar_is_the_identity_on_every_editable_path() -> None:
    """★ 真文件上把每一个可编辑标量「按原值写回去」⇒ 逐字节还是原来那一份。

    这条是整份面板保存路径的地基：只要有一处不是恒等变换，就说明"改一个值"会顺带
    改掉别的东西（注释 / 缩进 / 行尾），而那种差异在 diff 里几乎看不见。
    """
    raw = OUTPUTS_YAML.read_text(encoding="utf-8")
    config = load_outputs_config(OUTPUTS_YAML)
    lines = raw.splitlines(keepends=True)
    paths = _editable_paths()
    assert len(paths) >= 20, "可编辑路径太少 ⇒ 这条断言没覆盖到什么东西"
    for path in paths:
        assert set_scalar(lines, path, _value_at(config, path)) is True, path
    assert "".join(lines) == raw


def test_set_scalar_keeps_indentation_key_and_trailing_comment() -> None:
    lines = [
        "watermark:\n",
        "  position: bottom_right        # top_left | top_right\n",
        "  margin_x: 48\n",
    ]
    assert set_scalar(lines, ("watermark", "margin_x"), 64) is True
    assert lines[2] == "  margin_x: 64\n"
    assert set_scalar(lines, ("watermark", "position"), "top_left") is True
    assert lines[1] == "  position: top_left        # top_left | top_right\n"


def test_set_scalar_does_not_invent_a_newline_at_end_of_file() -> None:
    """最后一行没有换行符 ⇒ 改完还是没有（补一个会让"改一个值"变成"也动了文件末尾"）。"""
    lines = ["watermark:\n", "  margin_x: 48"]
    assert set_scalar(lines, ("watermark", "margin_x"), 50) is True
    assert lines[1] == "  margin_x: 50"


def test_set_scalar_keeps_the_line_ending() -> None:
    lines = ["watermark:\r\n", "  margin_x: 48\r\n"]
    assert set_scalar(lines, ("watermark", "margin_x"), 50) is True
    assert lines[1] == "  margin_x: 50\r\n"


def test_set_scalar_adds_the_missing_space_after_the_colon() -> None:
    """`width:1080` 这种写法读得出值，但补上空格才不会被当成字符串。"""
    lines = ["profiles:\n", "  a:\n", "    width:1080\n"]
    assert set_scalar(lines, ("profiles", "a", "width"), 720) is True
    assert lines[2] == "    width: 720\n"


def test_set_scalar_quotes_a_value_that_would_read_back_as_a_boolean() -> None:
    lines = ["watermark:\n", "  position: bottom_right\n"]
    assert set_scalar(lines, ("watermark", "position"), "no") is True
    assert lines[1] == '  position: "no"\n'


def test_set_scalar_returns_false_and_touches_nothing_when_the_path_is_missing() -> None:
    """找不到 ⇒ `False`，**不插入**（静默插一行比报错难查得多）。"""
    lines = ["watermark:\n", "  margin_x: 48\n"]
    before = list(lines)
    assert set_scalar(lines, ("watermark", "nope"), 1) is False
    assert set_scalar(lines, ("audio", "margin_x"), 1) is False
    assert lines == before
