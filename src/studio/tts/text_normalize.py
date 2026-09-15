"""文本归一化（T2.5 · §04.3.6）—— 把"写出来的字"整成"念得对的音"。

为什么需要它
------------
写稿 Agent 产出的是**给人看**的文本：``2026年``、``50%``、``FFmpeg``、Markdown 星号、
emoji。这些直接喂给 CosyVoice，``50%`` 会念成"五十百分号"、``FFmpeg`` 会念成
"艾弗艾弗艾姆派格"。归一化就是那道"人看的文本 → 机器念的文本"的闸门。

处理顺序**固定**（§04.3.6，顺序本身就是契约）
---------------------------------------------
1) 去 Markdown / emoji / 不可朗读符号（保留中文标点、书名号、引号）
2) 数字 / 百分比 / 日期 / 单位 → 中文读法（``2026年`` → ``二零二六年``）
3) 英文缩写与专名 → glossary 词典（``AI`` → ``诶哎``）
4) 多音字 / 易错词 → glossary 强制替换
5) 标点 → 停顿标记（:data:`PAUSE_MAP`，**不改变文本**，由 prosody 层消费）

顺序为什么不能调
----------------
- **步骤 2 必须在步骤 1 之后，但步骤 1 不能吃掉步骤 2 要用的字符**：
  ``2026-09-15`` 里的 ``-`` 一旦被当"特殊符号"删掉，日期就再也认不出来。
  所以步骤 1 **特意保留** ``.`` ``%`` ``-`` ``/`` ``:`` 与数字，剩下的交给步骤 2
  消费；步骤 2 没消费掉的 ``-`` ``/`` 由步骤 5 清掉。
- **步骤 3/4 在步骤 2 之后**：``FFmpeg`` 的读法只对"已经不是数字"的文本有意义。
- **步骤 5 不改变文本**：停顿是**元数据**。把停顿标记塞进正文会污染句级缓存键
  （T2.6）—— 同一句话因为停顿不同而算出两个键，缓存就永远命不中。

幂等性（``normalize(normalize(x)) == normalize(x)``）
-----------------------------------------------------
不是"尽量做到"，而是由 glossary 的**加载期校验**结构性保证的：
任一 value 不得包含任何 key（见 :func:`validate_glossary`）。否则
"第一遍把 A 换成 B、第二遍把 B 里的 A 又换一次"，同一句话两次合成出两个音，
而 TTS 是按句缓存的 ⇒ 缓存键永远命不中、每轮重试都要重新推理。

禁止：``pynini`` / ``WeTextProcessing``（R5 —— Windows 装不上，装了也编不出）。
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio.core.errors import ConfigError, ErrorCode
from studio.core.logging import get_logger

__all__ = [
    "DEFAULT_PAUSE_MS",
    "PAUSE_MAP",
    "Glossary",
    "GlossaryStore",
    "NormalizeMode",
    "load_glossary",
    "normalize",
    "pause_after_ms",
    "validate_glossary",
]

logger = get_logger("studio.tts.normalize")

#: ``"tts"`` = 交给合成引擎念的文本；``"subtitle"`` = 烧进画面的字幕。
#: 字幕**保留数字与半角标点**（``2026年`` 比 ``二零二六年`` 好读），
#: 但同样要过 Markdown/emoji 与词表 —— 字幕里冒出 ``**`` 和 emoji 一样是事故。
NormalizeMode = Literal["tts", "subtitle"]


# ══════════════════════════════════════════════════════════════════════
# 1. 词表（数据，不是代码）
# ══════════════════════════════════════════════════════════════════════


class Glossary(BaseModel):
    """``prompts/shared/glossary.yaml`` 的强类型视图。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    #: 步骤 3：英文缩写与专名 → 读法
    abbreviations: dict[str, str] = Field(default_factory=dict)
    #: 步骤 4：多音字 / 易错词 → 强制替换
    terms: dict[str, str] = Field(default_factory=dict)
    #: 步骤 2：单位读法（**只在紧跟数字时生效**）
    units: dict[str, str] = Field(default_factory=dict)


def validate_glossary(glossary: Glossary, *, path: Path | None = None) -> None:
    """加载期校验 —— 违反即 ``CONFIG_INVALID``（**绝不静默放过**）。

    三条硬规则：

    1. key / value 都非空（空 key 会匹配在每个字符之间，空 value 等于删字）；
    2. 同一 key 不得同时出现在 ``abbreviations`` 与 ``terms``
       （重了就成了"哪张表先生效"的悬案）；
    3. 任一 value **不得包含任何 key** —— 幂等性的结构性保证。

    ``units`` 只查"大小写撞车"：它的 key 只在紧跟数字时生效（``5km``），
    换出来的 ``五公里`` 里没有数字，第二遍不会再被吃一次；
    但 ``m`` 与 ``M`` 同时存在会变成"哪个赢"的悬案，必须拦。
    """
    problems: list[dict[str, str]] = []

    for table in ("abbreviations", "terms"):
        for key, value in getattr(glossary, table).items():
            if not key.strip() or not value.strip():
                problems.append({"table": table, "key": key, "reason": "key / value 不得为空"})

    for key in sorted(set(glossary.abbreviations) & set(glossary.terms)):
        problems.append({"table": "abbreviations|terms", "key": key, "reason": "两张表定义了同一个 key"})

    keys = [*glossary.abbreviations, *glossary.terms]
    for table in ("abbreviations", "terms"):
        for key, value in getattr(glossary, table).items():
            for other in keys:
                if other and other in value:
                    problems.append(
                        {
                            "table": table,
                            "key": key,
                            "reason": f"value {value!r} 里含 key {other!r} ⇒ normalize 不幂等",
                        }
                    )

    seen: dict[str, str] = {}
    for unit in glossary.units:
        lowered = unit.lower()
        if lowered in seen:
            problems.append({"table": "units", "key": unit, "reason": f"与 {seen[lowered]!r} 大小写撞车"})
        else:
            seen[lowered] = unit

    if problems:
        raise ConfigError(
            f"读音词表有 {len(problems)} 处问题，拒绝加载",
            code=ErrorCode.CONFIG_INVALID,
            context={"problems": problems, "path": str(path) if path else None},
            remediation="改 prompts/shared/glossary.yaml："
            "同一个 key 只留一处；value 里不得出现任何 key（否则归一化不幂等）",
        )


def load_glossary(path: Path) -> Glossary:
    """读并**强校验**一份词表（缺文件 ⇒ ``CONFIG_MISSING``）。"""
    if not path.is_file():
        raise ConfigError(
            f"读音词表不存在：{path}",
            code=ErrorCode.CONFIG_MISSING,
            context={"path": str(path)},
            remediation="确认 prompts/shared/glossary.yaml 已入库（§02.2）",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"读音词表读不出来：{path}",
            code=ErrorCode.CONFIG_INVALID,
            context={"path": str(path), "error": str(exc)},
            remediation="检查文件权限与编码（必须是 UTF-8）",
        ) from exc
    return _parse_glossary(text, path)


def _parse_glossary(text: str, path: Path) -> Glossary:
    """解析 + 校验（不碰磁盘 ⇒ 测试与热重载走同一条路径）。"""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"读音词表不是合法 YAML：{path}",
            code=ErrorCode.CONFIG_INVALID,
            context={"path": str(path), "error": str(exc)},
            remediation="修好 YAML 语法（缩进 / 冒号 / 引号）",
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            f"读音词表顶层必须是映射：{path}",
            code=ErrorCode.CONFIG_INVALID,
            context={"path": str(path), "type": type(raw).__name__},
            remediation="顶层写 schema_version / abbreviations / terms / units",
        )
    try:
        glossary = Glossary.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"读音词表字段不合法：{path}",
            code=ErrorCode.CONFIG_INVALID,
            context={"path": str(path), "errors": exc.errors(include_url=False)[:10]},
            remediation="只允许 schema_version / abbreviations / terms / units 四个顶层字段",
        ) from exc
    validate_glossary(glossary, path=path)
    return glossary


class GlossaryStore:
    """``prompts/shared/glossary.yaml`` 的热重载仓库（T2.5）。

    :param path: 词表路径（``StudioPaths.glossary_file``）

    为什么按 mtime 重载，而不是"重启才生效"
    ---------------------------------------
    读音纠正是**边听边改**的活：听到"银行"念错，改一行 YAML 就该立刻生效。
    要求重启 TTS 常驻服务（模型加载几十秒）会把这件事变成"攒一批再改"，
    而攒着改的代价是"这一批片子全带着错音发出去了"。

    为什么启动失败要抛、运行中失败只记
    ----------------------------------
    与 :class:`~studio.core.persona_store.PersonaStore` 同一条取舍：
    **启动时**文件不存在 = 装错了（拒绝启动，doctor 能提前拦下）；
    **运行中**把文件改坏 = 保住上一份好的并记一条 ``warning`` ——
    正在合成的任务不该因为别人正在编辑词表而中断。

    坏文件会连同它的 ``(mtime, size)`` 一起被记住，所以"文件一直坏着"只会告警一次，
    而不是每次请求告警一次。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._glossary: Glossary | None = None
        self._stat_key: tuple[int, int] | None = None
        self._version = ""
        self._last_error: ConfigError | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def version(self) -> str:
        """当前生效词表的 ``sha256``（前 12 位）—— 落 ``llm_calls`` 与产物 manifest。

        没有它，"这条片子为什么把银行念成 yínxíng"会变成一条查不完的悬案：
        词表是可以随时改的，产物必须能追溯到**当时那一份**。
        """
        with self._lock:
            self._ensure()
            return self._version

    @property
    def last_error(self) -> ConfigError | None:
        """上一次重载失败（``None`` = 当前生效的就是盘上那一份）。"""
        with self._lock:
            return self._last_error

    def current(self) -> Glossary:
        with self._lock:
            self._ensure()
            assert self._glossary is not None  # _ensure 保证：要么有值，要么已抛
            return self._glossary

    def reload(self) -> Glossary:
        """强制重读（忽略 mtime 缓存）。"""
        with self._lock:
            self._stat_key = None
            self._ensure()
            assert self._glossary is not None
            return self._glossary

    # ── 内部 ────────────────────────────────────────────────
    def _ensure(self) -> None:
        stat_key = _stat_key_of(self._path)
        if self._glossary is not None and stat_key is not None and stat_key == self._stat_key:
            return
        if stat_key is None:
            if self._glossary is None:
                self._glossary = load_glossary(self._path)  # 抛 CONFIG_MISSING（启动路径）
                return
            return  # 运行中被删：保住上一份好的
        try:
            text = self._path.read_text(encoding="utf-8")
            glossary = _parse_glossary(text, self._path)
        except (ConfigError, OSError) as exc:
            self._stat_key = stat_key  # 记住这个坏版本：一直坏着只告警一次
            if self._glossary is None:
                if isinstance(exc, ConfigError):
                    raise
                raise ConfigError(
                    f"读音词表读不出来：{self._path}",
                    code=ErrorCode.CONFIG_INVALID,
                    context={"path": str(self._path), "error": str(exc)},
                    remediation="检查文件权限与编码（必须是 UTF-8）",
                ) from exc
            self._last_error = exc if isinstance(exc, ConfigError) else None
            logger.warning(
                "tts.glossary_reload_failed",
                code=str(getattr(exc, "code", ErrorCode.CONFIG_INVALID)),
                error=str(exc),
                path=str(self._path),
                kept_version=self._version,
            )
            return
        self._glossary = glossary
        self._stat_key = stat_key
        self._version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        self._last_error = None
        logger.info("tts.glossary_loaded", path=str(self._path), version=self._version)


def _stat_key_of(path: Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)``；文件不在 ⇒ ``None``。"""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


# ══════════════════════════════════════════════════════════════════════
# 2. 停顿映射（§04.3.6 步骤 5：**不改变文本**）
# ══════════════════════════════════════════════════════════════════════

#: 标点 → 句尾停顿毫秒（§04.3.6 原表，数值就是契约）
PAUSE_MAP: Final[dict[str, int]] = {
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

#: 认不出标点时的兜底停顿（疑问/感叹都比陈述长，这是"读起来自然"的最小代价）
DEFAULT_PAUSE_MS: Final[int] = 200


def pause_after_ms(text: str) -> int:
    """句尾停顿（毫秒）：取**最长**匹配的标点；认不出来 ⇒ :data:`DEFAULT_PAUSE_MS`。

    为什么"最长优先"：``……`` 与 ``。`` 都在表里，按单字符查会把 ``……``
    判成两个句号（420×2 = 840ms），而省略号的语气强度跟句号根本不是一回事。
    """
    stripped = text.rstrip()
    for mark in sorted(PAUSE_MAP, key=len, reverse=True):
        if stripped.endswith(mark):
            return PAUSE_MAP[mark]
    return DEFAULT_PAUSE_MS


# ══════════════════════════════════════════════════════════════════════
# 3. 步骤 1：去 Markdown / emoji / 不可朗读符号
# ══════════════════════════════════════════════════════════════════════

_FENCED_CODE: Final[re.Pattern[str]] = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_IMAGE: Final[re.Pattern[str]] = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK: Final[re.Pattern[str]] = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_INLINE_CODE: Final[re.Pattern[str]] = re.compile(r"`([^`]*)`")
_EMPHASIS: Final[re.Pattern[str]] = re.compile(
    r"\*\*([^*]+)\*\*|\*([^*\n]+)\*|__([^_]+)__|(?<![\w])_([^_\n]+)_(?![\w])|~~([^~]+)~~"
)
_TABLE_ROW: Final[re.Pattern[str]] = re.compile(r"(?m)^\s*\|.*\|\s*$")
_RULE: Final[re.Pattern[str]] = re.compile(r"(?m)^\s{0,3}(?:[-*_]\s*){3,}$")
_HEADING: Final[re.Pattern[str]] = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_BLOCKQUOTE: Final[re.Pattern[str]] = re.compile(r"(?m)^\s{0,3}>\s?")
_LIST_MARKER: Final[re.Pattern[str]] = re.compile(r"(?m)^\s{0,3}(?:[-*+]|\d{1,2}[.)])\s+")

#: emoji / 变体选择符 / 零宽连接符 / 箭头 / 装饰符
_EMOJI: Final[re.Pattern[str]] = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U00002b00-\U00002bff"
    "\U00002190-\U000021ff\U0000fe0f\U0000200d\U000020e3\U00002122\U00002139]"
)

#: 不可朗读符号。**故意不含** ``.`` ``%`` ``-`` ``/`` ``:`` —— 步骤 2 还要用它们。
#: 也**故意不含**中文标点 / 书名号 / 引号（§04.3.6 步骤 1 明说要保留）。
_UNSPEAKABLE: Final[re.Pattern[str]] = re.compile(r"[@#$^&*+=|~`<>\[\]{}\\\"']")

_WS: Final[re.Pattern[str]] = re.compile(r"\s+")


def _strip_markup(text: str) -> str:
    """步骤 1：Markdown / emoji / 不可朗读符号 ⇒ 纯口播文本。"""
    out = _FENCED_CODE.sub(" ", text)  # 代码块整块丢：它本来就不是念的
    out = _IMAGE.sub(" ", out)  # 图片整块丢（alt 文本是给读屏软件看的）
    out = _LINK.sub(r"\1", out)  # 链接留文字、丢 URL
    out = _INLINE_CODE.sub(r"\1", out)
    out = _EMPHASIS.sub(lambda m: next(g for g in m.groups() if g is not None), out)
    out = _TABLE_ROW.sub(" ", out)
    out = _RULE.sub(" ", out)
    out = _HEADING.sub("", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _LIST_MARKER.sub("", out)
    out = _EMOJI.sub("", out)
    out = _UNSPEAKABLE.sub("", out)
    return _WS.sub(" ", out)


# ══════════════════════════════════════════════════════════════════════
# 4. 步骤 2：数字 / 百分比 / 日期 / 单位 → 中文读法
# ══════════════════════════════════════════════════════════════════════

_DIGITS: Final[str] = "零一二三四五六七八九"
_SMALL_UNITS: Final[tuple[str, ...]] = ("", "十", "百", "千")
_BIG_UNITS: Final[tuple[str, ...]] = ("", "万", "亿", "兆")
_ZERO_RUN: Final[re.Pattern[str]] = re.compile("零{2,}")

#: 顺序即优先级：**日期必须在范围之前**，否则 ``2026-09-15`` 会被当成 ``2026-09`` 的区间。
_DATE: Final[re.Pattern[str]] = re.compile(r"(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})")
_RANGE: Final[re.Pattern[str]] = re.compile(r"(\d+(?:\.\d+)?)\s*[-~～至]\s*(\d+(?:\.\d+)?)")
_YEAR: Final[re.Pattern[str]] = re.compile(r"(\d{2,4})\s*年")
_MONTH: Final[re.Pattern[str]] = re.compile(r"(\d{1,2})\s*月")
_DAY: Final[re.Pattern[str]] = re.compile(r"(\d{1,2})\s*([日号])")
_CLOCK: Final[re.Pattern[str]] = re.compile(r"(\d{1,2}):(\d{2})")
_PERCENT: Final[re.Pattern[str]] = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_DECIMAL: Final[re.Pattern[str]] = re.compile(r"(\d+\.\d+)")
_INTEGER: Final[re.Pattern[str]] = re.compile(r"(\d+)")

#: 步骤 5 才清掉的残留符号（步骤 2 没消费完的）
_LEFTOVER: Final[re.Pattern[str]] = re.compile(r"[-/\\+]")

_CJK: Final[str] = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

#: 半角句点**只在句末位置**才算句号（后面是空白 / 结尾 / 中文）。
#: 夹在 ASCII 词中间的点（``a.b.com``）是残留符号，删掉 —— 把它当句号会在词中间
#: 插一个 420ms 的停顿，切分器还会就此多切一句、多花一次引擎调用。
_DOT_AS_PERIOD: Final[re.Pattern[str]] = re.compile(r"\.(?=\s|$|[" + _CJK + r"])")

#: 半角标点 → 全角（在**步骤 2 之后**做：``3.14`` 的小数点必须先被吃掉）
_ASCII_PUNCT: Final[dict[str, str]] = {
    ",": "，",
    "!": "！",
    "?": "？",
    ";": "；",
    ":": "：",
    ".": "。",
    "(": "（",
    ")": "）",
}
_PUNCT_RUN: Final[re.Pattern[str]] = re.compile(r"([，。！？；：、])\1+")

_SPACE_BEFORE_CJK: Final[re.Pattern[str]] = re.compile(rf"\s+(?=[{_CJK}])")
_SPACE_AFTER_CJK: Final[re.Pattern[str]] = re.compile(rf"(?<=[{_CJK}])\s+")


def _read_group(group: str) -> str:
    """读 1–4 位（含内部零），不加大单位。"""
    digits = group.lstrip("0")
    if not digits:
        return ""
    out: list[str] = []
    size = len(digits)
    for index, char in enumerate(digits):
        value = int(char)
        position = size - index - 1
        if value == 0:
            out.append(_DIGITS[0])
        elif position == 1 and value == 1 and index == 0:
            out.append("十")  # 15 ⇒ 十五（不是 一十五）
        else:
            out.append(_DIGITS[value] + _SMALL_UNITS[position])
    return _ZERO_RUN.sub(_DIGITS[0], "".join(out)).rstrip(_DIGITS[0])


def _read_integer(digits: str) -> str:
    """``"1200"`` → ``"一千二百"``；``"100000001"`` → ``"一亿零一"``。"""
    trimmed = digits.lstrip("0")
    if not trimmed:
        return _DIGITS[0]
    groups: list[str] = []
    rest = trimmed
    while rest:
        groups.append(rest[-4:])
        rest = rest[:-4]
    parts: list[str] = []
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group.strip("0"):
            continue
        # 高位段已输出、本段有前导零 ⇒ 补一个"零"（一亿**零**一）
        if parts and len(group.lstrip("0")) < len(group):
            parts.append(_DIGITS[0])
        text = _read_group(group)
        parts.append(text + _BIG_UNITS[index] if index else text)
    return "".join(parts)


def _read_number(raw: str) -> str:
    """``"3.14"`` → ``"三点一四"``；``"1200"`` → ``"一千二百"``。"""
    if "." in raw:
        head, _, tail = raw.partition(".")
        fraction = "".join(_DIGITS[int(c)] for c in tail if c.isdigit())
        return _read_integer(head) + "点" + fraction
    return _read_integer(raw)


def _read_year(raw: str) -> str:
    """年份**逐位**念：``2026`` → ``二零二六``（不是"两千零二十六"）。"""
    return "".join(_DIGITS[int(c)] for c in raw if c.isdigit())


def _unit_pattern(units: dict[str, str]) -> tuple[re.Pattern[str], dict[str, str]] | None:
    """单位正则 + 小写查表（大小写不敏感匹配，但读法查表要跟着不敏感）。"""
    if not units:
        return None
    lookup = {key.lower(): value for key, value in units.items()}
    keys = sorted(units, key=len, reverse=True)  # 最长优先：min 不能被 m 抢走
    alternation = "|".join(re.escape(key) for key in keys)
    pattern = re.compile(rf"(\d+(?:\.\d+)?)\s*({alternation})(?![A-Za-z])", re.IGNORECASE)
    return pattern, lookup


def _read_numbers(text: str, units: dict[str, str]) -> str:
    """步骤 2：把"看得懂的数字"换成"念得对的字"。"""
    out = _DATE.sub(
        lambda m: f"{_read_year(m.group(1))}年{_read_integer(m.group(2))}月{_read_integer(m.group(3))}日",
        text,
    )
    out = _RANGE.sub(lambda m: f"{_read_number(m.group(1))}到{_read_number(m.group(2))}", out)
    out = _YEAR.sub(lambda m: f"{_read_year(m.group(1))}年", out)
    out = _MONTH.sub(lambda m: f"{_read_integer(m.group(1))}月", out)
    out = _DAY.sub(lambda m: f"{_read_integer(m.group(1))}{m.group(2)}", out)
    out = _CLOCK.sub(lambda m: f"{_read_integer(m.group(1))}点{_read_integer(m.group(2))}", out)
    out = _PERCENT.sub(lambda m: f"百分之{_read_number(m.group(1))}", out)

    unit = _unit_pattern(units)
    if unit is not None:
        pattern, lookup = unit
        out = pattern.sub(lambda m: f"{_read_number(m.group(1))}{lookup[m.group(2).lower()]}", out)

    out = _DECIMAL.sub(lambda m: _read_number(m.group(1)), out)
    return _INTEGER.sub(lambda m: _read_integer(m.group(1)), out)


# ══════════════════════════════════════════════════════════════════════
# 5. 步骤 3/4：词表替换（**单遍扫描 + 最长优先**）
# ══════════════════════════════════════════════════════════════════════


def _apply_map(text: str, mapping: dict[str, str]) -> str:
    """单遍替换：最长 key 优先，换出来的字**不再参与本轮匹配**。

    为什么不用一串 ``str.replace``：``replace`` 是**级联**的 ——
    先把 ``A`` 换成 ``B``、再处理 ``B`` 的规则时，刚换出来的 ``B`` 会被再换一次。
    加载期校验已经把这种词表拦掉了，这里再堵一层：
    单遍扫描下，"输出被自己的规则再吃一遍"在结构上不可能发生。
    """
    if not mapping:
        return text
    keys = sorted(mapping, key=len, reverse=True)
    alternation = "|".join(re.escape(key) for key in keys)
    pattern = re.compile(alternation)
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def _map_punct(text: str) -> str:
    """步骤 5：残留半角标点 → 全角、清残留符号、压重复标点。"""
    out = _LEFTOVER.sub("", text)
    out = _DOT_AS_PERIOD.sub("。", out)
    out = out.replace(".", "")  # 剩下的点夹在 ASCII 词中间（域名 / 缩写）
    for ascii_mark, cjk_mark in _ASCII_PUNCT.items():
        if ascii_mark == ".":
            continue
        out = out.replace(ascii_mark, cjk_mark)
    return _PUNCT_RUN.sub(r"\1", out)


def _tidy_spaces(text: str) -> str:
    """空白收敛：连续空白压一个，**中文旁边的空格去掉**。

    为什么去掉中文旁的空格：``跑 酷`` 与 ``跑酷`` 对合成引擎是同一个词，
    但它们是**两个不同的缓存键**（T2.6）—— 写稿模型偶尔多打一个空格，
    不该让同一句话重算一遍。
    """
    out = _WS.sub(" ", text)
    out = _SPACE_AFTER_CJK.sub("", out)
    return _SPACE_BEFORE_CJK.sub("", out).strip()


# ══════════════════════════════════════════════════════════════════════
# 6. 对外入口
# ══════════════════════════════════════════════════════════════════════


def normalize(text: str, *, glossary: Glossary, mode: NormalizeMode = "tts") -> str:
    """归一化（§04.3.6）。**幂等**：``normalize(normalize(x)) == normalize(x)``。

    :param text: 原始文本（可以带 Markdown / emoji / 半角标点）
    :param glossary: 词表（:class:`Glossary`；通常来自 :class:`GlossaryStore`）
    :param mode: ``"tts"`` 走全部五步；``"subtitle"`` 只做"去 Markdown/emoji +
        词表 + 空白收敛" —— **字幕保留数字与半角标点**（``2026年`` 比
        ``二零二六年`` 好读），但字幕里冒出 ``**`` 和 emoji 一样是事故。
    """
    if not text:
        return ""
    out = _strip_markup(text)
    if mode == "tts":
        out = _read_numbers(out, glossary.units)
    out = _apply_map(out, glossary.abbreviations)
    out = _apply_map(out, glossary.terms)
    if mode == "tts":
        out = _map_punct(out)
    return _tidy_spaces(out)
