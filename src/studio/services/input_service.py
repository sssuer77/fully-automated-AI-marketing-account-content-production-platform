"""输入源解析与导入（T1.9 · §04.1.7 · Q4）。

三样输入里的两样（热点、历史反馈）落在这里；第三样（频道定位）在
``core/persona_store.py``。

铁律：**解析阶段不调 LLM**
--------------------------
§04.1.7 明写"禁止在解析阶段调用 LLM（成本与不确定性）"。解析只做一件事：
把文件变成**行**。情感 / 诉求分类由 Planner 阶段批量做一次
（``agents/feedback_classifier.py``），结果再回填 ``feedback_items``。

坏行不丢（T1.9 裁定 67）
------------------------
"缺字段 ⇒ 跳过 + warn"里的**跳过**指"不参与选题"，不是"不入库"：
坏行照写 ``hot_items`` 并置 ``parse_ok=0``。理由有三：
1. DDL 专门留了 ``parse_ok`` + ``raw_line`` 两列（注释写着"解析失败可回溯"），
   若坏行不入库，这两列永远是常量；
2. 消费后热点要整文件归档（§04.1.7），坏行不入库就等于**静默丢数据**；
3. 前端能直接告诉用户"第 7 行格式不对"，改起来比"整批没反应"快得多。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from studio.core.clock import file_stamp
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.models import FeedbackItemRow, HotItemRow
from studio.db.repositories import FeedbackItemRepo, HotItemRepo
from studio.domain.topics import FeedbackItemSpec, HotItemSpec, Sentiment
from studio.services.log_service import LogSink

__all__ = [
    "FEEDBACK_GLOB",
    "HOT_GLOB",
    "FeedbackParseReport",
    "HotParseReport",
    "ImportReport",
    "InputService",
    "ParseIssue",
    "parse_feedback_text",
    "parse_hot_text",
]

logger = get_logger("studio.input")

HOT_GLOB: Final[str] = "*.md"
FEEDBACK_GLOB: Final[str] = "*.md"

#: 行首的 markdown 列表标记（``- `` / ``* `` / ``1. `` / ``1) ``）
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
#: 列分隔符（半角 + 全角）
_CELL_SPLIT = re.compile(r"[|｜]")
#: 结构化反馈头里的日期（§04.1.7：``平台|日期|情感|内容``）
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 情感词 → 领域简写（结构化头里写中文或英文都认）
_SENTIMENT_TOKENS: Final[Mapping[str, str]] = {
    "pos": "pos",
    "positive": "pos",
    "正面": "pos",
    "好评": "pos",
    "neu": "neu",
    "neutral": "neu",
    "中性": "neu",
    "中评": "neu",
    "neg": "neg",
    "negative": "neg",
    "负面": "neg",
    "差评": "neg",
    "unknown": "unknown",
    "未知": "unknown",
}


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """一行没能按契约解析（**不中断整批**）。"""

    line_no: int
    raw_line: str
    reason: str

    def describe(self) -> str:
        preview = self.raw_line[:60]
        return f"第 {self.line_no} 行：{self.reason} ⇒ {preview}"


@dataclass(frozen=True, slots=True)
class HotParseReport:
    """一个热点文件的解析结果（``items`` 含坏行，用 ``parse_ok`` 区分）。"""

    source_file: str
    items: list[HotItemSpec] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)

    @property
    def good(self) -> list[HotItemSpec]:
        return [item for item in self.items if item.parse_ok]

    @property
    def bad(self) -> list[HotItemSpec]:
        return [item for item in self.items if not item.parse_ok]


@dataclass(frozen=True, slots=True)
class FeedbackParseReport:
    """一个反馈文件的解析结果（自由文本形态永远能兜住 ⇒ 没有"坏行"）。"""

    source_file: str
    items: list[FeedbackItemSpec] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)


@dataclass(slots=True)
class ImportReport:
    """一次导入的结果（CLI / API 直接展示）。

    **可变**（不像同文件的解析报告那样 frozen）：它是逐文件累加的累加器，
    最后再交给调用方读；造一个不可变的"半成品链"只会让代码更长。
    """

    kind: str
    files: list[str] = field(default_factory=list)
    parsed: int = 0
    bad: int = 0
    inserted: int = 0
    skipped: int = 0
    issues: list[ParseIssue] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        return [issue.describe() for issue in self.issues]


# ══════════════════════════════════════════════════════════════════════
# 解析内核（纯函数，可单测）
# ══════════════════════════════════════════════════════════════════════


def _iter_lines(text: str) -> list[tuple[int, str]]:
    """切行 + 剥列表标记；空行与 ``#`` 注释行**直接不产出**（行号仍按原文计）。"""
    lines = text.lstrip("\ufeff").splitlines()
    result: list[tuple[int, str]] = []
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result.append((index, _LIST_MARKER.sub("", raw).strip()))
    return result


def parse_hot_text(text: str, *, source_file: str) -> HotParseReport:
    """解析热点文件：每行 ``标题|热度|平台``（§04.1.7）。

    - **列数不足 3** ⇒ 记 issue + 产出一条 ``parse_ok=False`` 的行（不丢原文）
    - ``heat`` 解析成 ``float`` 失败 ⇒ ``heat=None`` 但 ``heat_raw`` 保留原文
      （DDL 明说 ``heat`` 可以存 ``'爆' | '高'``），**不算错误**
    - 标题为空 ⇒ 同"列数不足"处理
    """
    items: list[HotItemSpec] = []
    issues: list[ParseIssue] = []

    for line_no, line in _iter_lines(text):
        cells = [cell.strip() for cell in _CELL_SPLIT.split(line)]
        title = cells[0] if cells else ""
        if len(cells) != 3 or not title:
            reason = f"列数为 {len(cells)}（需要 `标题|热度|平台`）" if len(cells) != 3 else "标题为空"
            issues.append(ParseIssue(line_no=line_no, raw_line=line, reason=reason))
            items.append(
                HotItemSpec(
                    title=line[:80] or "(空行)",
                    raw_line=line,
                    line_no=line_no,
                    parse_ok=False,
                    parse_error=reason,
                )
            )
            continue

        heat_raw = cells[1] or None
        items.append(
            HotItemSpec(
                title=title,
                heat=_to_float(heat_raw),
                heat_raw=heat_raw,
                platform=cells[2] or None,
                raw_line=line,
                line_no=line_no,
            )
        )

    return HotParseReport(source_file=source_file, items=items, issues=issues)


def parse_feedback_text(text: str, *, source_file: str) -> FeedbackParseReport:
    """解析反馈文件（**双形态兼容** · Q4）。

    - **结构化头**：恰好 4 列 **且** 第 2 列是 ``YYYY-MM-DD`` **且** 第 3 列是情感词
      ⇒ 按 ``平台|日期|情感|内容`` 解析；
    - **其余一切**（含"看着像结构化头但情感词不认"）⇒ **整行当自由文本**，
      ``sentiment='unknown'`` / ``kind='other'``。

    第二种情况会额外记一条 issue（有信息量：用户以为自己写对了）。
    自由文本形态**不会**产生 issue —— 它本来就是合法输入，否则 Q4 的"容错"就是假的。
    """
    items: list[FeedbackItemSpec] = []
    issues: list[ParseIssue] = []

    for line_no, line in _iter_lines(text):
        cells = [cell.strip() for cell in _CELL_SPLIT.split(line)]
        structured = (
            len(cells) == 4 and bool(_DATE.match(cells[1])) and cells[2].casefold() in _SENTIMENT_TOKENS
        )
        if structured:
            items.append(
                FeedbackItemSpec(
                    platform=cells[0] or None,
                    date=cells[1],
                    sentiment=cast("Sentiment", _SENTIMENT_TOKENS[cells[2].casefold()]),
                    text=cells[3],
                    raw_line=line,
                    line_no=line_no,
                )
            )
            continue

        if len(cells) == 4 and _DATE.match(cells[1]):
            issues.append(
                ParseIssue(
                    line_no=line_no,
                    raw_line=line,
                    reason=f"看着像结构化头，但情感词无法识别：{cells[2]!r}",
                )
            )
        items.append(FeedbackItemSpec(text=line, raw_line=line, line_no=line_no))

    return FeedbackParseReport(source_file=source_file, items=items, issues=issues)


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════
# 导入服务
# ══════════════════════════════════════════════════════════════════════


class InputService:
    """``data/hot/*.md`` 与 ``data/feedback/*.md`` 的导入 + 归档。

    归档时机是**消费后**而不是导入后（§04.1.7）：文件还在 ``data/hot/`` 里，
    说明"这批热点还没被选题用掉"；被 Planner 用掉之后才移进 ``archive/``，
    保留 90 天供复盘"当时为什么选这个方向"。
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        paths: StudioPaths | None = None,
        log: LogSink | None = None,
    ) -> None:
        self._paths = paths or StudioPaths.from_env()
        self._hot = HotItemRepo(connection)
        self._feedback = FeedbackItemRepo(connection)
        self._log = log

    # ── 热点 ────────────────────────────────────────────────────────
    def hot_files(self) -> list[Path]:
        """``data/hot/*.md``（**不含** ``archive/`` 子目录）。"""
        directory = self._paths.hot_dir
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.glob(HOT_GLOB) if path.is_file())

    def import_hot(self) -> ImportReport:
        """扫描并导入热点文件（幂等：重复导入不产生重复行）。"""
        rows: list[HotItemRow] = []
        report = ImportReport(kind="hot")
        files = self.hot_files()
        for path in files:
            parsed = parse_hot_text(path.read_text(encoding="utf-8"), source_file=path.name)
            report.files.append(path.name)
            report.issues.extend(parsed.issues)
            report.parsed += len(parsed.good)
            report.bad += len(parsed.bad)
            rows.extend(_hot_row(item, source_file=path.name) for item in parsed.items)

        report.inserted = self._hot.insert_many(rows)
        report.skipped = len(rows) - report.inserted
        self._emit(
            "info" if not report.issues else "warn",
            f"导入热点 {report.inserted} 条（{len(files)} 个文件，坏行 {report.bad}）",
            payload={
                "kind": "hot",
                "files": report.files,
                "inserted": report.inserted,
                "bad": report.bad,
                "issues": report.warnings[:10],
            },
        )
        return report

    def archive_hot(self, source_files: Sequence[str]) -> list[str]:
        """把**已消费**的热点文件移入 ``data/hot/archive/``（幂等、不覆盖）。"""
        archive_dir = self._paths.hot_archive_dir
        moved: list[str] = []
        for name in source_files:
            path = self._paths.hot_dir / name
            if not path.is_file():
                continue
            archive_dir.mkdir(parents=True, exist_ok=True)
            target = archive_dir / f"{path.stem}-{file_stamp()}{path.suffix}"
            if target.exists():
                target = archive_dir / f"{path.stem}-{file_stamp()}-{new_ulid()[:6]}{path.suffix}"
            shutil.move(str(path), str(target))
            moved.append(target.name)
        if moved:
            self._emit("info", f"归档热点文件 {len(moved)} 个", payload={"moved": moved})
        return moved

    # ── 反馈 ────────────────────────────────────────────────────────
    def feedback_files(self) -> list[Path]:
        directory = self._paths.feedback_dir
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.glob(FEEDBACK_GLOB) if path.is_file())

    def import_feedback(self) -> ImportReport:
        """扫描并导入反馈文件（幂等：同文件同原文只入一行）。"""
        rows: list[FeedbackItemRow] = []
        report = ImportReport(kind="feedback")
        files = self.feedback_files()
        for path in files:
            parsed = parse_feedback_text(path.read_text(encoding="utf-8"), source_file=path.name)
            report.files.append(path.name)
            report.issues.extend(parsed.issues)
            report.parsed += len(parsed.items)
            rows.extend(_feedback_row(item, source_file=path.name) for item in parsed.items)

        report.inserted = self._feedback.insert_many(rows)
        report.skipped = len(rows) - report.inserted
        self._emit(
            "info" if not report.issues else "warn",
            f"导入反馈 {report.inserted} 条（{len(files)} 个文件）",
            payload={
                "kind": "feedback",
                "files": report.files,
                "inserted": report.inserted,
                "issues": report.warnings[:10],
            },
        )
        return report

    # ── 内部 ────────────────────────────────────────────────────────
    def _emit(self, level: str, message: str, *, payload: Mapping[str, Any] | None = None) -> None:
        if self._log is not None:
            self._log(level=level, source="input.import", message=message, payload=payload)  # type: ignore[arg-type]
            return
        if level == "warn":
            logger.warning(message, source="input.import", **(payload or {}))
        else:
            logger.info(message, source="input.import", **(payload or {}))


def _hot_row(item: HotItemSpec, *, source_file: str) -> HotItemRow:
    return HotItemRow(
        id=new_ulid(),
        source_file=source_file,
        line_no=item.line_no,
        title=item.title,
        heat=item.heat_raw,
        platform=item.platform,
        raw_line=item.raw_line,
        parse_ok=item.parse_ok,
    )


def _feedback_row(item: FeedbackItemSpec, *, source_file: str) -> FeedbackItemRow:
    return FeedbackItemRow(
        id=new_ulid(),
        source_file=source_file,
        platform=item.platform,
        occurred_on=item.date,
        content=item.text,
        # 解析阶段不分类（§04.1.7）⇒ sentiment 留 NULL，等 Planner 阶段回填
        sentiment=None,
        is_auto=item.is_auto,
    )
