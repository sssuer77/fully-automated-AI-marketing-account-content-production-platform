"""成片库：盘上已出的片子 × 它属于哪条任务 × 它发到哪儿了（T5.11 · §06.11）。

为什么要有这一层（真机 2026-09-22）
-----------------------------------
发布这条路此前只能**手敲任务号**：面板上那一格要人把一个 26 位的 ULID 抄进去，
而人手里有的是**一支片子**（他刚在渲染面板看着它出出来），不是一串 id。两者之间
没有任何东西把它们连起来 ⇒「把刚出的这条发出去」变成「翻库、复制、粘贴」三步。

这一层补的就是那个连接：盘上有什么 ⇒ 它是哪条任务的 ⇒ 它发到哪儿了。

为什么一行是**一条任务**，不是一个文件
--------------------------------------
发布池认的是任务号：它自己拿 :func:`~studio.services.publish_service.resolve_final_video`
去找片子（``manifest.json`` 优先、目录按名字兜底），**不认调用方指的那个文件**。
所以面板按文件列的话（一条任务重出过三版就是三行），勾第二行与勾第三行的结果
**一模一样** —— 那是一句谎话，而且是最难查的那种（「我明明选了新那一版」）。
按任务列，把「盘上有几版」如实写在行里。

「哪一版算这一条的成片」的判据**不在这一层**：这里调的就是发布池那一个函数。
两处各算一次的代价是「面板上勾的」与「发出去的」可以是两支不同的片子，
而两边都不会报错。

为什么批量投递要**逐条**回结论
------------------------------
一批十条里有三条发不出去是常态（平台没启用、这条早投过、盘上没成片），
而它们的处置动作完全不同。合成一个「成功 7 条」之后，操作员只能靠猜哪三条、
为什么 —— 所以每一条都带上它自己的 ``queued`` / ``skipped`` / ``duplicates``
（与单条投递同一份形状，见 :class:`~studio.services.publish_service.EnqueueReport`）。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.config import PublishConfig
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.repositories.publication_repo import PublicationRepo, PublicationRow
from studio.domain.task_service import TaskService
from studio.services.publish_service import enqueue_publications, read_json, resolve_final_video

__all__ = [
    "BATCH_MAX",
    "LIBRARY_LIMIT_DEFAULT",
    "LIBRARY_LIMIT_MAX",
    "BatchItemReport",
    "BatchPublishReport",
    "LibraryItem",
    "parse_task_id",
    "publish_many",
    "read_library",
]

logger = get_logger("studio.services.library")

#: 一次批量投递最多几条任务。
#:
#: 50 是「一个人一次真会勾的条数」的上限，不是技术上限。再往上（比如把整个成片库
#: 全勾上）该走的是定时计划那一路（T5.6），而不是让一次点击在发布池里堆 500 条作业
#: —— 那些作业会按 ``min_gap_min`` 一条条发出去，而人已经离开面板了。
BATCH_MAX: Final[int] = 50

#: 成片库一次最多回几行。
LIBRARY_LIMIT_MAX: Final[int] = 500
LIBRARY_LIMIT_DEFAULT: Final[int] = 200

#: 成片文件名的形状：``{yyyymmdd-HHMMSS}_{task_id}_final[_720p].mp4``（§2.4）。
#:
#: ``.+`` 是**贪婪**的（不是 ``.+?``）：任务号本身可以含下划线（``ui-2026…``、
#: 以及用户自己起的名），贪婪匹配会一路吃到**最后一个** ``_final``，非贪婪则会
#: 在任务号里遇到第一个 ``_final`` 就断 —— 那会切出一个不存在的任务号。
_VIDEO_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(?P<stamp>\d{8}-\d{6})_(?P<task_id>.+)_final(?P<degraded>_720p)?\.mp4$"
)


def parse_task_id(name: str) -> str | None:
    """成片文件名 ⇒ 任务号；认不出来 ⇒ ``None``（**不抛**）。

    认不出来是常态，不是异常：``data/output/videos/`` 是人也会往里放东西的地方
    （手工导出的、别人发来的），而那些东西**不该**让整个成片库打不开。
    """
    match = _VIDEO_NAME.match(name)
    return None if match is None else match.group("task_id")


@dataclass(frozen=True, slots=True)
class LibraryItem:
    """成片库里的一行 = **一条任务**（不是一支片子，见模块注释）。"""

    task_id: str
    #: 盘上那条任务的成片（``manifest.json`` 优先，与发布池同一份判据）。
    video_name: str
    video_path: Path
    size_bytes: int
    modified_at: float
    #: 这条任务在盘上有几支成片（重出过就有多支）。**不参与选择** —— 发布时用的是
    #: ``video_path`` 那一支。写出来是因为「为什么我改的那一版没发出去」总要有个答案。
    versions: int
    #: ``tasks`` 里那一行还在吗。不在了（任务被删、只剩片子）照样列出来，
    #: 但面板要说得出来「这条发不了」。
    task_found: bool = False
    title: str = ""
    task_status: str | None = None
    #: 成片时长（``manifest.json`` 里的实测值）。读不到 ⇒ ``None``（**不现场 ffprobe**：
    #: 那是每行一次子进程，成片库一屏几十行就是几十秒）。
    duration_ms: int | None = None
    publications: tuple[PublicationRow, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """**不含** ``publications``：那一个由路由层用 ``publication_view`` 拼
        （``can_retry`` / ``can_cancel`` 那几个布尔是服务端的状态机判据，只有一份，
        见 ``app/schemas/publish.py``）。
        """
        return {
            "task_id": self.task_id,
            "video_name": self.video_name,
            "video_url": f"/api/v1/render/videos/{self.video_name}",
            "video_path": self.video_path.as_posix(),
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "versions": self.versions,
            "task_found": self.task_found,
            "title": self.title,
            "task_status": self.task_status,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class BatchItemReport:
    """批量投递里的一条结论（形状与单条投递同源，见模块注释）。"""

    task_id: str
    queued: int = 0
    skipped: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "queued": self.queued,
            "skipped": list(self.skipped),
            "duplicates": list(self.duplicates),
            "missing": self.missing,
        }


@dataclass(frozen=True, slots=True)
class BatchPublishReport:
    """一次批量投递的总账。"""

    items: tuple[BatchItemReport, ...] = ()
    platforms: tuple[str, ...] = ()
    #: 一共排进发布池几条（``平台 × 账号`` 的合计，与单条投递的 ``queued`` 同义）。
    queued_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "platforms": list(self.platforms),
            "queued_total": self.queued_total,
            "task_total": len(self.items),
        }


def read_library(
    *,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    limit: int = LIBRARY_LIMIT_DEFAULT,
) -> tuple[LibraryItem, ...]:
    """成片库：盘上每一支成片 ⇒ 一条任务一行（新的在前）。

    三个来源合起来才是一行（**缺一个都不该让这一行消失**）：

    ① ``data/output/videos/`` —— 「盘上有什么」的唯一真相。文件名给出任务号；
    ② ``tasks`` + ``manifest.json`` —— 标题与时长（读不到就是空 / ``None``，
       不挡住这一行：片子是真的，元数据是锦上添花）；
    ③ ``publications`` —— 它发到哪儿了（一条都没有 ⇒ 未发布）。
    """
    grouped = _scan_finals(paths)
    if not grouped:
        return ()

    tasks = TaskService(connection)
    # 一次查完所有相关任务的发布记录：逐行查是 N+1，而 N 是"成片库有多大"。
    by_task = _publications_by_task(connection, tuple(grouped))

    items: list[LibraryItem] = []
    for task_id, files in grouped.items():
        canonical = resolve_final_video(task_id, paths)
        if canonical is None:
            # 盘上有文件、但发布池那条判据答不出来（文件在两次调用之间被挪走）。
            # 退回"这一组里最新的那一支"，而不是让这一行凭空消失。
            canonical = files[0]
        try:
            stat = canonical.stat()
        except OSError:
            logger.warning("library.video_unreadable", task_id=task_id, path=canonical.as_posix())
            continue

        title = ""
        status: str | None = None
        found = True
        try:
            task = tasks.get(task_id)
        except StudioError:
            found = False
        else:
            title = task.title
            status = task.status.value

        items.append(
            LibraryItem(
                task_id=task_id,
                video_name=canonical.name,
                video_path=canonical,
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
                versions=len(files),
                task_found=found,
                title=title,
                task_status=status,
                duration_ms=_manifest_duration_ms(paths, task_id),
                publications=by_task.get(task_id, ()),
            )
        )

    items.sort(key=lambda item: item.modified_at, reverse=True)
    return tuple(items[: max(int(limit), 1)])


def publish_many(
    *,
    connection: sqlite3.Connection,
    paths: StudioPaths,
    config: PublishConfig,
    task_ids: Sequence[str],
    platforms: Sequence[str] | None = None,
    dry_run: bool | None = None,
) -> BatchPublishReport:
    """把勾中的这几条任务一次排进发布池（**逐条**回结论，见模块注释）。

    两件事在这里就挡掉，**不投出去**：

    ① 任务不存在 ⇒ ``missing``（发布池那条路也会返回同一个结论，但那时作业已经在
       队列里了，面板上会多出一条永远不动的记录）；
    ② 盘上没有成片 ⇒ ``skipped``。发布池会在认领之后报 ``RENDER_FAILED``，然后那条
       作业进死信、``publications`` 里连一行都没有 —— 用户看到的是"投了，然后没了"。
       在投递这一侧说清楚，比在死信里说清楚要早一步（也就是人还在面板上的时候）。

    输入本身不合法（空清单 / 超过 :data:`BATCH_MAX`）⇒ 抛 ``VALIDATION_FAILED``：
    那是**调用方**的错，不是这一批里某一条的命运，混进 ``items`` 里会让面板以为
    "这一条没投出去"。
    """
    wanted: list[str] = []
    for task_id in task_ids:
        if task_id not in wanted:
            wanted.append(task_id)
    if not wanted:
        raise StudioError(
            "没有勾中任何成片",
            code=ErrorCode.VALIDATION_FAILED,
            context={"task_ids": list(task_ids)},
            remediation="在成片库里勾一条以上再点批量发布",
        )
    if len(wanted) > BATCH_MAX:
        raise StudioError(
            f"一次最多投 {BATCH_MAX} 条（勾了 {len(wanted)} 条）",
            code=ErrorCode.VALIDATION_FAILED,
            context={"count": len(wanted), "max": BATCH_MAX},
            remediation=(
                "分成几批，或者用「定时计划」那一路（T5.6）—— 一次点击在发布池里堆几百条作业，人会先离开面板"
            ),
        )

    reports: list[BatchItemReport] = []
    queued_total = 0
    tasks = TaskService(connection)
    for task_id in wanted:
        # 先判任务、再判成片：反过来的话，"任务号打错了"会报成"盘上没有成片"
        # —— 两句都是真话，但指向的下一步动作完全不同（改任务号 vs 先去出片）。
        try:
            tasks.get(task_id)
        except StudioError:
            reports.append(BatchItemReport(task_id=task_id, missing=True))
            continue
        if resolve_final_video(task_id, paths) is None:
            reports.append(BatchItemReport(task_id=task_id, skipped=("盘上没有成片",)))
            continue
        report = enqueue_publications(
            connection=connection,
            task_id=task_id,
            config=config,
            platforms=platforms,
            dry_run=dry_run,
        )
        reports.append(
            BatchItemReport(
                task_id=task_id,
                queued=report.queued,
                skipped=report.skipped,
                duplicates=report.duplicates,
                missing=report.missing,
            )
        )
        queued_total += report.queued

    logger.info(
        "library.published",
        tasks=len(wanted),
        queued=queued_total,
        platforms=list(platforms or ()),
    )
    return BatchPublishReport(
        items=tuple(reports),
        platforms=tuple(platforms or ()),
        queued_total=queued_total,
    )


def _scan_finals(paths: StudioPaths) -> dict[str, list[Path]]:
    """``data/output/videos/`` ⇒ ``{任务号: [成片文件…]}``（新的在前）。"""
    directory = paths.videos_dir
    if not directory.is_dir():
        return {}
    try:
        entries = list(directory.iterdir())
    except OSError:
        logger.warning("library.videos_dir_unreadable", path=directory.as_posix())
        return {}
    grouped: dict[str, list[Path]] = {}
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() != ".mp4":
            continue
        task_id = parse_task_id(entry.name)
        if task_id is None:
            continue
        grouped.setdefault(task_id, []).append(entry)
    for files in grouped.values():
        files.sort(key=_mtime, reverse=True)
    return grouped


def _mtime(path: Path) -> float:
    """文件的修改时刻；量不到 ⇒ ``0.0``（**不抛**：排个序而已）。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _manifest_duration_ms(paths: StudioPaths, task_id: str) -> int | None:
    """``manifest.json`` 里那次出片的实测时长；没有 / 不是整数 ⇒ ``None``。"""
    payload = read_json(paths.manifest_json(task_id))
    if not isinstance(payload, dict):
        return None
    value = payload.get("duration_ms")
    return value if isinstance(value, int) and value > 0 else None


def _publications_by_task(
    connection: sqlite3.Connection, task_ids: tuple[str, ...]
) -> dict[str, tuple[PublicationRow, ...]]:
    """这些任务的发布记录，按任务号分组（走 repo，不自己拼 SQL）。"""
    grouped: dict[str, tuple[PublicationRow, ...]] = {}
    for row in PublicationRepo(connection).list_for_tasks(task_ids):
        grouped[row.task_id] = (*grouped.get(row.task_id, ()), row)
    return grouped
