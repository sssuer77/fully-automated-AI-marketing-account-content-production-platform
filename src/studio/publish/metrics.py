"""数据回收（T5.4 · §06.6 · 原文 §8 第四步）。

一件事，两个调用面
------------------
① :func:`collect_metrics` —— **采一条**：取记录 ⇒ 打平台管理页读数 ⇒ 落库
   （``metrics_json`` 最新 + ``metrics_history_json`` 追加 + 算下一个时点）；
② :class:`studio.services.publish_metrics_service.PublishMetricsService` —— **轮询**：
   按 ``publications.next_metric_at`` 找出到点的记录，逐条调 ①。

为什么调度不用队列（§06.6 原文）
--------------------------------
数据回收**不是任务**：它没有"成片"要交、没有人工确认闸、失败也不该占一条作业的
重试额度。§06.6 明写"由 ``metrics_service`` 轮询 ``publications.next_metric_at``"。
把它塞进 ``jobs`` 会得到一件很别扭的事：一次"读个数"失败要在队列里重试三次，
而它本来只需要顺延一小时（本模块的 :data:`METRICS_RETRY_DELAY_HOURS`）。

为什么失败不写 ``error_code``
-----------------------------
``publications.error_code`` 说的是"**这条作品发出去**这件事出了什么事"。采数失败时
作品是好好发着的，把采集失败写进去，面板上的红色错误会指向一次并不存在的发布故障。
失败只体现在两处：``metric_attempts`` 计数 + 一条 ``warn`` 日志。

字段允许 ``None``（§06.6 平台限制）
-----------------------------------
有的平台数据延迟、有的维度不公开。**禁止用 0 冒充"无数据"** —— 0 是"确实没人看"，
``None`` 是"我们不知道"，两者在选题决策里的含义完全相反（前者要降权，后者什么都不该做）。
所以 :func:`parse_metric_count` 读不出来时返回 ``None``，一路传到库里仍是 ``None``。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Final, cast

from studio.core.clock import format_iso, parse_iso, utc_now
from studio.core.config import AccountConfig, PlatformCode, PublishConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.repositories.publication_repo import PUBLISHED, PublicationRepo, PublicationRow
from studio.publish.base import Publisher, PublisherContext, PublishMetrics, get_publisher

__all__ = [
    "METRICS_MAX_ATTEMPTS",
    "METRICS_RETRY_DELAY_HOURS",
    "METRICS_SOURCE",
    "collect_metrics",
    "defer_after_failure",
    "metrics_payload",
    "next_metric_at",
    "parse_metric_count",
    "parse_metric_ratio",
]

logger = get_logger("studio.publish.metrics")

#: 一个时点最多试几次（§06.6「顺延 1 小时重试（≤3 次）」）。
#: 含**第一次**：3 = 首发 + 2 次顺延。
METRICS_MAX_ATTEMPTS: Final[int] = 3
#: 失败后的顺延（小时 · §06.6）。
METRICS_RETRY_DELAY_HOURS: Final[int] = 1
#: ``metrics_json.source`` 的取值。留一列"这个数是谁给的"，是为了将来接平台开放接口
#: 时能一眼分辨"浏览器读的"与"API 拿的"——两者的可信度与失败模式都不一样。
METRICS_SOURCE: Final[str] = "publisher"

#: 计数文本里的数字（``1.2万`` / ``1,234`` / ``12.5k`` 都要认）。
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
#: 数字后面的量级单位（中文平台的 ``万`` / ``亿``，英文界面的 ``k``）。
_UNITS: Final[dict[str, float]] = {
    "亿": 1e8,
    "万": 1e4,
    "w": 1e4,
    "W": 1e4,
    "千": 1e3,
    "k": 1e3,
    "K": 1e3,
}


#: 负号（``-`` 与 Unicode 的 ``−`` U+2212）。平台上的 ``-`` 更常见的意思是
#: "无数据"（``-`` / ``--``），而**数字前面**的那个减号只有一个意思：这是个负数。
_MINUS: Final[tuple[str, ...]] = ("-", "\u2212")


def _is_negative(raw: str, end: int) -> bool:
    """匹配到的数字**紧前面**是不是负号。

    为什么不能靠 ``value < 0``：:data:`_NUMBER` 只抓 ``\\d``，减号根本不在匹配里 ——
    于是 ``-3%`` 会被读成 ``3%``。那是一个**安静的符号翻转**：库里的完播率从"负的"
    变成"正的"，而两者都是看着正常的数。计数那边同理（``-1.2万`` 曾会变成 12000）。
    """
    return end > 0 and raw[end - 1] in _MINUS


def parse_metric_count(text: str | None) -> int | None:
    """平台上的计数文本 ⇒ 整数；**读不出来 ⇒ ``None``**（绝不返回 0）。

    ``1.2万`` ⇒ 12000、``1,234`` ⇒ 1234、``12.5k`` ⇒ 12500、
    ``—`` / ``暂无`` / 空串 ⇒ ``None``。

    为什么要自己解析而不是 ``int(text)``：抖音/快手/视频号在管理页上显示的都是
    **缩写**（``1.2万``），而缩写是给人看的。直接 ``int`` 会在真机上第一次采数就炸，
    而那时"数据回收坏了"与"平台把数字缩写了"这两件事在日志里长得一模一样。
    """
    if text is None:
        return None
    raw = str(text).strip().replace(",", "").replace(" ", "").replace("\u00a0", "")
    if not raw:
        return None
    match = _NUMBER.search(raw)
    if match is None:
        return None
    if _is_negative(raw, match.start()):
        return None
    value = float(match.group(0))
    # 单位只看**数字紧后面那一个字符**：``1.2万`` 的 ``万`` 在 ``1.2`` 之后。
    # 放宽成"整串里有没有万"会把 ``2026-09-18 万`` 这种混进来的日期也乘一万。
    unit = raw[match.end() : match.end() + 1]
    multiplier = _UNITS.get(unit)
    if multiplier is not None:
        value *= multiplier
    return int(value)


def parse_metric_ratio(text: str | None) -> float | None:
    """平台上的**比率**文本 ⇒ 0–1 的比值；读不出来 ⇒ ``None``。

    ``42.3%`` ⇒ 0.423、``42.3`` ⇒ 0.423、``0.423`` ⇒ 0.423、
    ``—`` / 空串 / 负数 / 超过 100% ⇒ ``None``。

    三条口径，都是为了让"42.3"这个裸数字只有一个答案：

    ① **带 ``%`` ⇒ 一定按百分数**（``42.3%`` ⇒ 0.423）；
    ② **不带且 ≤ 1 ⇒ 已经是比值**（``0.42`` ⇒ 0.42）—— 有的平台直接给小数；
    ③ **不带且 > 1 ⇒ 按百分数**（``42.3`` ⇒ 0.423）—— 中文平台的界面上写的就是
       "42.3"，那个 ``%`` 在旁边的标签里，抓不到。

    读不出来返回 ``None`` 的理由与 :func:`parse_metric_count` 完全一样：``0`` 是
    "没人看完"，``None`` 是"我们不知道"，而"完播率 0%"会让选题去砍掉一个其实没问题的题材。
    """
    if text is None:
        return None
    raw = str(text).strip().replace(",", "").replace(" ", "").replace("\u00a0", "")
    if not raw:
        return None
    match = _NUMBER.search(raw)
    if match is None:
        return None
    if _is_negative(raw, match.start()):
        return None
    value = float(match.group(0))
    percent = "%" in raw or value > 1.0
    ratio = value / 100.0 if percent else value
    if ratio > 1.0:
        # 101% 这种要么是平台算错了，要么是我们抓到了别的数字 —— 两者都不该进库。
        return None
    return ratio


def metrics_payload(metrics: PublishMetrics) -> dict[str, Any]:
    """``PublishMetrics`` ⇒ ``publications.metrics_json`` 的形状（§03.3.15 的列注释）。

    ``source`` 由**本层**补，不放进 ``PublishMetrics``：那个 dataclass 是
    §4.6.1 的发布器契约，五个字段逐字钉在 ``tests/contract/test_publisher_abc.py`` 上；
    平台适配器不该知道"我们往库里怎么写"。
    """
    return {
        "views": metrics.views,
        "likes": metrics.likes,
        "comments": metrics.comments,
        "shares": metrics.shares,
        "completion_rate": metrics.completion_rate,
        "collected_at": metrics.collected_at,
        "source": METRICS_SOURCE,
    }


def next_metric_at(
    published_at: str | None,
    hours: Sequence[int],
    *,
    now: str | None = None,
) -> str | None:
    """下一个**还没到**的时点；都过了 ⇒ ``None``（= 停止采集这一条 · §06.6）。

    时点是**相对发布时刻**算的（T+1h/6h/24h/72h），不是"上一次采集 + 1h"：
    平台的数据是"这条作品发布后 1 小时的读数"，锚点搞错了整条趋势线都会漂。

    判据是**严格大于** ``now``。这一条看着多余（调用方只在到点时才采），
    但它挡住了两个真实的坏情况：① 面板上的"立即采集"（人想马上看一眼）；
    ② 顺延后的重试恰好落在下一个时点之前 —— 用 ``>=`` 的话，T+1h 顺延到 T+2h
    采完之后会把 T+6h **跳过**，那条趋势线上就永远少一个点。
    """
    points = sorted({int(hour) for hour in hours if int(hour) > 0})
    if not points:
        return None
    base = parse_iso(published_at) if published_at else utc_now()
    moment = parse_iso(now) if now else utc_now()
    for hour in points:
        candidate = base + timedelta(hours=hour)
        if candidate > moment:
            return format_iso(candidate)
    return None


def defer_after_failure(row: PublicationRow, *, now: str | None = None) -> str | None:
    """失败后把下一个时点顺延 1 小时；**次数用尽 ⇒ ``None``**（停止采集这一条）。

    ``row.metric_attempts`` 是**已经失败过**的次数（本次失败还没记进去），
    所以判据是 ``attempts + 1 >= METRICS_MAX_ATTEMPTS``：第 3 次失败落库之后
    就没有第 4 次了（§06.6「仍失败 ⇒ 停止采集该条」）。
    """
    if row.metric_attempts + 1 >= METRICS_MAX_ATTEMPTS:
        return None
    moment = parse_iso(now) if now else utc_now()
    return format_iso(moment + timedelta(hours=METRICS_RETRY_DELAY_HOURS))


async def collect_metrics(
    publication_id: str,
    *,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    config: PublishConfig,
    publisher: Publisher | None = None,
    now: str | None = None,
) -> PublishMetrics:
    """采一条作品的数据并落库（§4.6.2 的 ④；失败**原样抛**，由轮询层决定顺延）。

    :param publisher: 测试注入用。真机上按记录里的 ``platform`` + ``account_id`` 现造
        —— 登录态是**按账号**隔离的（§06.2.4），拿"该平台唯一启用的那个账号"去采
        另一条记录的数据，在换过账号之后会安静地采到别人的数（见 :func:`_account_for`）。
    :param now: 计算下一个时点用的"现在"（测试注入；不传取真实时间）。

    **不碰 ``tasks``**：与 ``publications`` 的其余写入同一条边界 —— 采集失败不是
    任务失败，成片与发布记录都仍然有效。
    """
    repo = PublicationRepo(connection)
    row = repo.get(publication_id)
    if row is None:
        raise StudioError(
            f"发布记录不存在：{publication_id}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"publication_id": publication_id},
        )
    if row.status != PUBLISHED:
        raise StudioError(
            f"只有已发布的记录能采数（当前 {row.status}）：{publication_id}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"publication_id": publication_id, "status": row.status},
            remediation="dry-run 的演练记录不会真的发出去，平台上没有对应的作品",
        )
    if not row.platform_post_id:
        raise StudioError(
            f"发布记录没有平台作品号，采不到数据：{publication_id}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"publication_id": publication_id, "platform": row.platform},
            remediation="等发布成功回填 platform_post_id 之后再采",
        )

    target = publisher or _build_publisher(row, paths=paths, config=config)
    metrics = await target.fetch_metrics(row.platform_post_id)
    payload = metrics_payload(metrics)
    repo.record_metrics(
        publication_id,
        payload,
        next_metric_at=next_metric_at(row.published_at, config.metrics_schedule_hours, now=now),
    )
    logger.info(
        "metrics.collected",
        publication_id=publication_id,
        platform=row.platform,
        views=payload["views"],
    )
    return metrics


def _build_publisher(row: PublicationRow, *, paths: StudioPaths, config: PublishConfig) -> Publisher:
    """按记录里的平台与账号造一个发布器（**不查"该平台唯一启用的账号"**）。"""
    # 平台代号在库里是自由文本（CHECK 约束管的是写入那一刻），而配置的键是
    # ``PlatformCode`` 这个 Literal —— 查表前必须显式收窄（与 ``resolve_platform`` 同一手法）。
    platform_cfg = config.platforms.get(cast("PlatformCode", row.platform))
    if platform_cfg is None:
        raise StudioError(
            f"config/publish.yaml 里没有平台 {row.platform}",
            code=ErrorCode.CONFIG_INVALID,
            context={"platform": row.platform, "known": sorted(config.platforms)},
            remediation="平台被从配置里删掉之后，历史发布记录采不到数 —— 补回那一段",
        )
    account = _account_for(config, row)
    return get_publisher(platform_cfg.publisher)(
        PublisherContext(paths=paths, account=account, platform=platform_cfg)
    )


def _account_for(config: PublishConfig, row: PublicationRow) -> AccountConfig:
    """这条记录**当时用的那个账号**。

    为什么不复用 ``services.publish_service.resolve_account``：那条口径是"该平台
    **唯一**启用的账号"（发布时用哪个账号还没定），而这里的问题是"这条记录属于谁"
    —— 记录里写着 ``account_id``，答案只有一个。账号被停用/改名之后，这里**明确报错**
    而不是回落到当前启用的账号：那会拿着 B 账号的登录态去读 A 账号作品的数，
    在面板上表现为"这个号的播放量突然变成 0"。
    """
    for account in config.accounts:
        if account.account_id == row.account_id:
            return account
    raise StudioError(
        f"config/publish.yaml 里没有账号 {row.account_id}",
        code=ErrorCode.CONFIG_INVALID,
        context={"account_id": row.account_id, "platform": row.platform},
        remediation="把账号补回 accounts（登录态目录 data/browser_profile/<账号> 也要还在）",
    )
