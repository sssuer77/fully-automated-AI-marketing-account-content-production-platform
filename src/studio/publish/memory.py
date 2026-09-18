"""记忆沉淀（T5.4 · §06.8 · 原文 §8 第五步）。

三条动作，一条闭环
------------------
① **高互动评论** ⇒ ``feedback_items(is_auto=1, source_publication_id=…)``；
② **低互动选题** ⇒ 同方向的待选选题**降权**（幅度上限 20%）；
③ **汇总** ⇒ ``data/feedback/auto_YYYYMM.md``（下一轮 Planner 直接吃）。

闭环的判据是 §6.8 那一句：``auto_YYYYMM.md`` 必须**能被 §04.1.7 的解析器直接消费**
（无需人工编辑）。所以本模块写完文件之后会**真的解析一遍**（:func:`_consumable`），
把结论放进 :attr:`MemorySinkResult.planner_consumable` —— 而不是"我按格式写了，
应该没问题"。格式这种东西，声明与实现在两个文件里，漂移是迟早的事。

为什么评论要"注入"进来
----------------------
§6.8 ① 的输入是**评论列表**（每条带点赞数/回复数），而 §4.6.1 的发布器契约只有
``fetch_metrics``（五个计数，逐字钉在契约测试上）。评论抓取是平台适配层的下一个
动作（各平台的评论页结构差异很大），所以这里留一条**显式的缝**：

- ``sink_memory(..., comments=[…])`` —— 调用方给；
- 不给 ⇒ 试着用发布器的 ``fetch_comments``（鸭子类型，**不**改 §4.6.1 的 ABC）；
- 还是没有 ⇒ ``comments=()``，本轮只做 ②③，并在结论里说清楚"评论抓取没做"。

把这条缝写成"假装抓到了"（比如拿评论**计数**当评论**内容**）会让回流的反馈里
出现一堆编出来的话，而它们会一路进到 Planner 的提示词里 —— 那比空着危险得多。

三条安全阀（§06.8 原文）
------------------------
① ``is_auto=1`` 区分来源（Planner 侧开关：``config/llm.yaml: planner.include_auto_feedback``）；
② 降权幅度上限 20%（:data:`DEMOTION_MAX_RATIO`）；
③ 只留**文本**：:class:`CommentSample` 里没有昵称 / 头像 / 用户 ID 三个字段 ——
   不是"我们不填"，是**这个类型里根本没有地方可以填**。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Final

from pydantic import BaseModel

from studio.core.clock import local_tz, parse_iso, utc_now
from studio.core.config import PublishConfig
from studio.core.ids import new_ulid
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.models import FeedbackItemRow
from studio.db.repositories.feedback_repo import FeedbackItemRepo
from studio.db.repositories.publication_repo import PUBLISHED, PublicationRepo, PublicationRow
from studio.db.repositories.topic_repo import TopicRepo
from studio.publish.base import Publisher
from studio.publish.metrics import _build_publisher

__all__ = [
    "DEMOTION_MAX_RATIO",
    "LOW_ENGAGEMENT_RATIO",
    "MIN_COMMENT_LIKES",
    "MIN_COMMENT_REPLIES",
    "CommentSample",
    "MemorySinkResult",
    "digest_path",
    "is_high_engagement",
    "sink_memory",
]

logger = get_logger("studio.publish.memory")

#: "高互动"的两条门槛（§6.8 ①：点赞 ≥ 阈值 **或** 回复数 ≥ 阈值）。
#: 值是**政策**不是配置：真机上要按账号量级调，但在那之前给一组保守值，
#: 好过留一个没人填的配置项（空配置会让"阈值是多少"变成"看代码"）。
MIN_COMMENT_LIKES: Final[int] = 20
MIN_COMMENT_REPLIES: Final[int] = 5
#: 低互动判据：``views < 同账号近期中位数 × 0.5``（§6.8 ②）。
LOW_ENGAGEMENT_RATIO: Final[float] = 0.5
#: 降权幅度**上限**（§6.8 安全阀 ②：一次低播放不许变成"永久放弃这个方向"）。
DEMOTION_MAX_RATIO: Final[float] = 0.2
#: "近期中位数"取最近几条（同账号、已发布、有播放量读数的）。
MEDIAN_WINDOW: Final[int] = 20
#: ``data/feedback/`` 下自动汇总文件的命名（§6.6 / §6.8）。
DIGEST_PREFIX: Final[str] = "auto_"

#: 行首注释（``_iter_lines`` 会跳过 ``#`` 开头的行 ⇒ 它们不会被当成反馈）。
_HEADER: Final[tuple[str, ...]] = (
    "# 自动回流 · 由 studio 生成（§06.8），**请勿手工编辑** —— 下一轮会被覆盖式追加。",
    "# 每行 = 平台|日期|情感|内容（§04.1.7 的结构化头，`studio input import` 直接吃）。",
)


@dataclass(frozen=True, slots=True)
class CommentSample:
    """一条公开评论的**最小**采样（§06.8 安全阀 ③）。

    三个字段，没有第四个：昵称 / 头像 / 用户 ID 在这一层**无处可放**。
    真机上从页面上读评论时，也请只把这三样传进来。
    """

    text: str
    likes: int | None = None
    replies: int | None = None


class MemorySinkResult(BaseModel):
    """一次记忆沉淀的结论（§4.6.2 逐字四个字段 + 三个说明性的补充）。"""

    feedback_items_created: int
    topics_demoted: int
    digest_path: Path
    #: ★ 闭环验收：``auto_*.md`` 能被 §04.1.7 的解析器**无人工编辑**地消费。
    planner_consumable: bool
    #: 本轮看了几条评论（0 ⇒ 平台适配层还没提供评论抓取，见模块注释）。
    comments_seen: int = 0
    #: 本轮是否判定为"低互动"（判不出来 ⇒ ``False``，不降权）。
    low_engagement: bool = False


def is_high_engagement(
    comment: CommentSample,
    *,
    like_min: int = MIN_COMMENT_LIKES,
    reply_min: int = MIN_COMMENT_REPLIES,
) -> bool:
    """点赞 ≥ 阈值 **或** 回复数 ≥ 阈值（§6.8 ①）。

    ``None`` **不算命中**：读不到点赞数不等于"没人点赞"（§06.6 的平台限制同一条口径）。
    """
    likes = comment.likes
    replies = comment.replies
    return (likes is not None and likes >= like_min) or (replies is not None and replies >= reply_min)


def digest_path(paths: StudioPaths, *, now: str | None = None) -> Path:
    """``data/feedback/auto_YYYYMM.md``（月份取**本地时区**）。

    本地时区而不是 UTC：这个文件是给人看的月度汇总，而"这个月"在操作员心里
    就是本地的那个月。
    """
    moment = parse_iso(now) if now else utc_now()
    stamp = moment.astimezone(local_tz()).strftime("%Y%m")
    return paths.feedback_dir / f"{DIGEST_PREFIX}{stamp}.md"


async def sink_memory(
    publication_id: str,
    *,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    config: PublishConfig,
    comments: Sequence[CommentSample] | None = None,
    publisher: Publisher | None = None,
    now: str | None = None,
) -> MemorySinkResult:
    """把一条已发布作品的数据沉淀成"下一轮能用"的记忆（§6.8 ①②③）。

    :param comments: 评论采样；``None`` ⇒ 试着用发布器的 ``fetch_comments``（见模块注释）。
    :param publisher: 测试注入用（与 ``collect_metrics`` 同一个缝）。

    **不碰 ``tasks`` / 不碰发布状态**：沉淀失败不该让"这条发出去过"这个事实翻掉。
    """
    repo = PublicationRepo(connection)
    row = repo.get(publication_id)
    if row is None or row.status != PUBLISHED:
        return MemorySinkResult(
            feedback_items_created=0,
            topics_demoted=0,
            digest_path=digest_path(paths, now=now),
            planner_consumable=False,
        )

    samples = tuple(
        comments
        if comments is not None
        else await _fetch_comments(row, paths=paths, config=config, publisher=publisher)
    )
    hot = tuple(item for item in samples if is_high_engagement(item))
    day = (parse_iso(now) if now else utc_now()).astimezone(local_tz()).strftime("%Y-%m-%d")

    path = digest_path(paths, now=now)
    created = _insert_feedback(connection, row, hot, source_file=path.name, day=day)
    demoted, low = _demote_low_engagement(connection, row)
    _append_digest(path, _digest_lines(row, hot, day=day))
    consumable = _consumable(path)

    if not consumable:
        # 自己写的文件自己读不了 ⇒ 闭环断了。这是 **error**：下一轮 Planner 会看不见
        # 这批回流，而面板上"数据回流"那一块看起来完全正常。
        logger.error("memory.digest_unreadable", path=path.as_posix(), publication_id=publication_id)
    return MemorySinkResult(
        feedback_items_created=created,
        topics_demoted=demoted,
        digest_path=path,
        planner_consumable=consumable,
        comments_seen=len(samples),
        low_engagement=low,
    )


async def _fetch_comments(
    row: PublicationRow,
    *,
    paths: StudioPaths,
    config: PublishConfig,
    publisher: Publisher | None,
) -> tuple[CommentSample, ...]:
    """评论来源的缝（见模块注释）：发布器有 ``fetch_comments`` 就用它，没有就空手。

    ``getattr`` 而不是往 §4.6.1 的 ABC 上加第四个抽象方法：那个 ABC 的
    ``__abstractmethods__`` 被契约测试**逐字**钉着（``health``/``publish``/``fetch_metrics``），
    加一个抽象方法会让所有实现（含二线平台的接口桩）立刻不可实例化。
    """
    target = publisher or _build_publisher(row, paths=paths, config=config)
    fetch = getattr(target, "fetch_comments", None)
    if fetch is None:
        logger.info("memory.no_comment_source", platform=row.platform, publication_id=row.id)
        return ()
    result = await fetch(row.platform_post_id or "")
    return tuple(result)


def _insert_feedback(
    connection: sqlite3.Connection,
    row: PublicationRow,
    comments: Iterable[CommentSample],
    *,
    source_file: str,
    day: str,
) -> int:
    """高互动评论 ⇒ ``feedback_items``（``is_auto=1`` + 来源发布记录）。

    ``sentiment`` 留 ``None``：分类由 Planner 阶段那一次批量跑（§04.1.7 的
    "解析阶段不调 LLM"在回流这条路上同样成立），``apply_classification`` 之后
    ``kind`` 也就有了。**在这里猜情感**会让同一批数据有两套口径。

    ``source_file`` 填**汇总文件的文件名**而不是"某次采集"：于是后面
    ``InputService.import_feedback()`` 扫到这个文件时，``(source_file, content)``
    幂等键正好命中，不会插第二遍（见 ``feedback_repo`` 的模块注释）。
    """
    items = [
        FeedbackItemRow(
            id=new_ulid(),
            source_file=source_file,
            platform=row.platform,
            occurred_on=day,
            content=item.text,
            is_auto=True,
            source_publication_id=row.id,
        )
        for item in comments
    ]
    return FeedbackItemRepo(connection).insert_many(items)


def _demote_low_engagement(connection: sqlite3.Connection, row: PublicationRow) -> tuple[int, bool]:
    """低互动 ⇒ 同方向的待选选题降权（§6.8 ②）⇒ ``(降了几条, 是否判为低互动)``。"""
    views = row.metrics.get("views")
    if not isinstance(views, int):
        # 读不到播放量 ⇒ **不判**（禁止用 0 冒充"无数据" · §06.6）。
        return 0, False
    baseline = _median_views(connection, row)
    if baseline is None or baseline <= 0 or views >= baseline * LOW_ENGAGEMENT_RATIO:
        return 0, False

    topic = TopicRepo(connection).get_by_task(row.task_id)
    if topic is None:
        # 人加的任务没有选题候选（§T4.3 的"人工加选题"也要落一行，但历史数据可能没有）。
        logger.info("memory.no_topic_for_task", task_id=row.task_id, publication_id=row.id)
        return 0, True
    demoted = TopicRepo(connection).demote_candidates(
        direction_id=topic.direction_id, factor=1.0 - DEMOTION_MAX_RATIO
    )
    logger.info(
        "memory.demoted",
        direction_id=topic.direction_id,
        demoted=demoted,
        views=views,
        baseline=baseline,
    )
    return demoted, True


def _median_views(connection: sqlite3.Connection, row: PublicationRow) -> float | None:
    """同账号近期已发布作品的播放量中位数（**不含这一条**）。

    中位数而不是均值：一条爆款会把均值拉到"所有正常作品都算低互动"，
    而那正是 §6.8 安全阀 ② 要防的过拟合（一次误判 ⇒ 整个方向被压）。
    """
    rows = PublicationRepo(connection).list_by_status(PUBLISHED, limit=MEDIAN_WINDOW * 4)
    # 写成显式循环而不是推导式：`metrics` 是 `Mapping[str, Any]`，推导式里那一次
    # `isinstance` 收窄不会传到列表的元素类型上（mypy 只看到 `Any | None`）。
    values: list[int] = []
    for item in rows:
        if item.id == row.id or item.account_id != row.account_id:
            continue
        views = item.metrics.get("views")
        if isinstance(views, int):
            values.append(views)
    if not values:
        return None
    return float(median(values[-MEDIAN_WINDOW:]))


def _digest_lines(row: PublicationRow, comments: Sequence[CommentSample], *, day: str) -> list[str]:
    """高互动评论 ⇒ 汇总文件里的行（§04.1.7 的结构化头）。

    第 3 列固定 ``unknown``：情感由 Planner 阶段的分类器判，写一个我们自己猜的值
    会与库里的分类结果**打架**（文件说 pos、库里说 neg，谁对？）。
    """
    return [f"{row.platform}|{day}|unknown|{_one_line(item.text)}" for item in comments]


def _one_line(text: str) -> str:
    """压成一行且**不含列分隔符**（解析器按 ``|`` / ``｜`` 切列）。

    评论里的换行与竖线都很常见（"1. 这样 / 2. 那样"），不清理的话一行会变成两行、
    或者多切出一列 —— 前者产出半条反馈，后者产出"看着像结构化头但坏了"的告警。
    """
    return " ".join(text.replace("|", "/").replace("｜", "/").split())


def _append_digest(path: Path, lines: Sequence[str]) -> int:
    """把新行追加进汇总文件（**幂等**：已存在的行不重复写）⇒ 返回新增行数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if path.is_file():
        existing = path.read_text(encoding="utf-8").splitlines()
    known = {line.strip() for line in existing}
    fresh = [line for line in lines if line.strip() and line.strip() not in known]
    if not fresh and existing:
        return 0
    body = [*existing, *fresh] if existing else [*_HEADER, *fresh]
    path.write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")
    return len(fresh)


def _consumable(path: Path) -> bool:
    """这个文件下一轮 Planner 能不能**直接吃**（§6.8 的闭环验收）。

    用**真的解析器**（§04.1.7）而不是"再看一眼格式"：声明与实现分在两个文件里，
    只有跑一遍才知道它们还一致。import 写在函数里，是因为 ``services/`` 在
    ``publish/`` **之上**（§02.1）—— 模块顶层 import 会把这条依赖方向反过来，
    而函数级的延迟 import 只在真正要验收时才用到它。
    """
    if not path.is_file():
        return False
    from studio.services.input_service import parse_feedback_text  # noqa: PLC0415

    try:
        report = parse_feedback_text(path.read_text(encoding="utf-8"), source_file=path.name)
    except Exception as exc:  # pragma: no cover - 只有坏编码会走到
        logger.warning("memory.digest_parse_failed", path=path.as_posix(), error=str(exc))
        return False
    return not report.issues
