"""YAML 行级标量编辑（T4.7 · §04.5.9）—— **保留注释**地改一个标量值。

为什么不用 `yaml.safe_dump` 整份重写
-----------------------------------
`config/outputs.yaml` 每一行都带着理由：``# ★ 水印是必做项（D5）``、
``# 上限 0.25（T3.2 夹取）``、``# 发布门禁 [-16.5, -15.5]``。整份重写会把它们
全部抹掉 —— 于是"在面板上调了一次字号"就永久毁掉了这份文件的可读性，而回退时
人也看不出"这行原来是干什么的"。这条取舍在 T4.2 的「一键全自动」上已经立过
（裁定 139）：**逐行替换，不用 `yaml.safe_dump`**。

本模块把那件事从"一个键"推广到"任意深度的标量"：给定一条键路径
（如 ``("watermark", "margin_x")``），找到那一行，只换冒号右边的标量 ——
缩进、键名、行尾注释、行尾换行符**一个字节都不动**。

能改什么、不能改什么
--------------------
只改**标量叶子**（``int`` / ``float`` / ``bool`` / ``str``）。块（映射）与序列
（列表）不在能力范围内，面板一期也不编辑它们（R17：拖拽与层结构树延后二期）。
想改列表就得自己动手写 YAML —— 这是**故意**的：行级替换改不了"多一行少一行"，
硬做只能靠重排整块，而那正是要避免的事。

支持的 YAML 形态（``config/*.yaml`` 的实际写法）
------------------------------------------------
- 缩进块映射（``watermark:`` / ``  margin_x: 48``），缩进用空格；
- 叶子值可带行尾注释（``position: bottom_right  # top_left | ...``）；
- 行内流序列（``platforms: [douyin, kuaishou]``）**能解析但不会被改**。

不支持：锚点/别名、多行折叠标量、行内流映射。``config/*.yaml`` 里一个都没有，
真出现了也只会在"找不到那条路径"上如实报错（:func:`set_scalar` 返回 ``False``），
**不会写坏文件**。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from typing import Final

__all__ = ["find_scalar", "render_scalar", "set_scalar", "split_inline_comment"]

#: 一行"键: 值"。行首允许缩进；键名不含 ``#`` 与 ``:``；注释行与列表项都不匹配。
_KEY_LINE: Final[re.Pattern[str]] = re.compile(r"^([ \t]*)([^#\s][^:]*?)[ \t]*:([ \t]*)(.*)$")

#: 可以裸写（不加引号）的标量形状。首字符必须是字母数字或下划线 ——
#: 以 ``-`` / ``[`` / ``{`` / ``&`` / ``*`` / ``!`` 开头在 YAML 里有别的含义。
_PLAIN_SAFE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .+()/-]*$")

#: 看着像数字的字符串必须加引号，否则回读时会被解析成 int / float。
_NUMERIC: Final[re.Pattern[str]] = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")

#: YAML 里"不是字符串"的裸词（``font_name: no`` 会读出布尔值，那种故障极难查）。
_RESERVED_WORDS: Final[frozenset[str]] = frozenset(
    {"true", "false", "null", "none", "yes", "no", "on", "off", "y", "n", "~"}
)


def split_inline_comment(text: str) -> tuple[str, str]:
    """把"冒号右边的部分"切成 ``(值, 行尾注释)``。

    注释的判据与 YAML 一致：``#`` 在行首，或**前面是空白**。所以
    ``font_name: A#B`` 里的 ``#`` 是值的一部分，而
    ``opacity: 0.85  # 上限 1.0`` 里的才是注释。引号内的 ``#`` 同样不算
    （``name: "a # b"``）—— 单双引号都认，双引号里的 ``\"`` 不当收尾。

    返回的第二段**含**它前面的空白，于是"拼回去"对未改动的行是恒等变换
    （这一点由 ``tests/unit/core/test_yaml_lines.py`` 逐字断言）。
    """
    quote: str | None = None
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quote is not None:
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            value = text[:index].rstrip()
            return value, text[len(value) :]
        index += 1
    value = text.rstrip()
    return value, text[len(value) :]


def render_scalar(value: object) -> str:
    """把一个 Python 标量渲染成 YAML 里的写法（**不**做整份 dump）。

    字符串的引号判据取**保守**那一侧：只要有一点点可能被 YAML 读成别的类型
    （``yes`` / ``~`` / ``0.85`` / 带 ``#`` 或 ``:``），就加双引号。宁可多引一对，
    也不要出现"面板上写的是字符串、回读变成布尔"这种要查半天的怪事。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # `repr` 而不是 `f"{value:g}"`：后者对**整数值的浮点数**会把 `1.0` 写成 `1`，
        # 于是"把每一个可编辑标量按原值写回去 ⇒ 逐字节还是原来那一份"这条地基在
        # 手写过 `opacity: 1.0` 的文件上当场塌掉（真机 2026-09-22：合成配置面板保存
        # 一次就会把那行改成 `1`，而 `test_set_scalar_is_the_identity_on_every_editable_path`
        # 正是靠这条不变量守住"改一个值不会顺带改掉别的东西"）。
        # `repr` 是 Python 里**保证往返**的那个写法：`float(repr(x)) == x` 恒成立。
        return repr(value)
    if value is None:
        return "null"
    text = str(value)
    if (
        _PLAIN_SAFE.match(text)
        and text == text.strip()
        and ":" not in text
        and "#" not in text
        and text.lower() not in _RESERVED_WORDS
        and not _NUMERIC.match(text)
    ):
        return text
    return json.dumps(text, ensure_ascii=False)


def _iter_key_lines(lines: Sequence[str]) -> Iterator[tuple[int, tuple[str, ...], re.Match[str]]]:
    """逐行扫出 ``(行号, 键路径, 正则匹配)``；块映射按缩进维护路径栈。

    路径栈只在**空值行**（``watermark:``）上压栈：有值的行不可能是块的开头，
    压进去只会让下一条同缩进的键算错父级。
    """
    stack: list[tuple[int, str]] = []
    for index, raw in enumerate(lines):
        match = _KEY_LINE.match(raw.rstrip("\r\n"))
        if match is None:
            continue
        indent = len(match.group(1).expandtabs(8))
        key = match.group(2).rstrip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        names = [name for _, name in stack]
        names.append(key)
        yield index, tuple(names), match
        value, _comment = split_inline_comment(match.group(4))
        if value == "":
            stack.append((indent, key))


def find_scalar(lines: Sequence[str], path: Sequence[str]) -> int | None:
    """找出某条键路径所在的行号（0 基）；找不到 ⇒ ``None``。"""
    target = tuple(path)
    for index, found, _match in _iter_key_lines(lines):
        if found == target:
            return index
    return None


def set_scalar(lines: list[str], path: Sequence[str], value: object) -> bool:
    """**就地**替换一条路径的标量值；返回是否真的找到并改写了。

    找不到 ⇒ ``False``，**不插入**。面板只改已有字段，而"文件里没有这个键"
    多半意味着配置与代码的版本对不上 —— 那种时候静默插一行进去，比报错更难查。
    """
    index = find_scalar(lines, path)
    if index is None:
        return False
    raw = lines[index]
    body = raw.rstrip("\r\n")
    # 行尾**原样保留**（包括"最后一行没有换行符"这种）：补一个 `\n` 会让
    # "只改一个标量"变成"顺带改了文件末尾"，而那是校验不出来的差异。
    ending = raw[len(body) :]
    match = _KEY_LINE.match(body)
    assert match is not None  # find_scalar 只会返回匹配到的行
    indent, key, gap, rest = match.groups()
    _value, comment = split_inline_comment(rest)
    # 冒号后原本没有空格（``width:1080``）⇒ 补一个，别写出 ``width:1080`` 那种读不出
    # 是"键: 值"还是"字符串"的东西。
    separator = gap or " "
    lines[index] = f"{indent}{key}:{separator}{render_scalar(value)}{comment}{ending}"
    return True
