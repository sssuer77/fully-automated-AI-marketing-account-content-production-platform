"""日志 REST 面（T1.7 分页 + T4.9 过滤 / 搜索 / 导出 · §04.4.6 / §04.5.2）。

两条通道的分工
--------------
- **WS**：低延迟的实时尾随（会被 100ms 合并窗口与 2Hz 限流压过，属"看得快"）；
- **REST**：可重复、可分页、可导出的真相查询（属"查得全"）。

日志面板两个都要用：WS 攒实时流，REST 负责①翻更早的历史②**补洞**（T4.9 裁定 121）
③导出。所以这里的过滤参数必须与 WS 订阅面的语义一致，否则"面板看到的"与"导出的"
就会是两份不同的数据 —— 那正是最难查的一类问题。

导出为什么是流式的
------------------
`limit` 上限是 20 万行；一次性 `json.dumps` 整个列表会先造出几十 MB 的字符串再拷进
响应体，峰值内存是数据的两倍以上。改成生成器后，每次只在内存里留一页（500 行），
并且**边查边发** —— 前端拿到第一行的时间与总量无关。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from studio.app.deps import AppState
from studio.app.schemas.logs import LogPage, LogRow
from studio.services.log_service import LogService, ndjson_lines

__all__ = ["DEFAULT_EXPORT", "EXPORT_PAGE", "MAX_EXPORT", "MAX_PAGE", "router"]

router = APIRouter(tags=["logs"])

#: 单页上限（防一次拉爆前端）
MAX_PAGE: int = 1000

#: 导出总行数上限（防"导出全库"把磁盘和内存一起吃掉）
MAX_EXPORT: int = 200_000

#: 导出的默认行数（够一次排查，又不至于让浏览器卡住）
DEFAULT_EXPORT: int = 20_000

#: 导出时每批从库里取多少行（流式的粒度）
EXPORT_PAGE: int = 500


def _ndjson_response(
    service: LogService,
    *,
    since_id: int | None,
    until_id: int | None,
    limit: int,
    min_level: str | None,
    task_id: str | None,
    source: str | None,
    search: str | None,
) -> StreamingResponse:
    """把一页页日志拼成 NDJSON 流（生成器 ⇒ 内存里只留一页）。"""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    def stream() -> Iterator[str]:
        emitted = 0
        if since_id is None:
            # 没给游标 ⇒ "导出最近 N 条"（一次查完，不需要翻页）。
            yield ndjson_lines(
                service.recent(
                    limit=limit,
                    min_level=min_level,
                    task_id=task_id,
                    source=source,
                    search=search,
                    until_id=until_id,
                )
            )
            return
        cursor = since_id
        while emitted < limit:
            batch = min(EXPORT_PAGE, limit - emitted)
            rows = service.since(
                cursor,
                limit=batch,
                min_level=min_level,
                task_id=task_id,
                source=source,
                search=search,
            )
            if not rows:
                return
            fresh = rows if until_id is None else tuple(row for row in rows if row.id <= until_id)
            if fresh:
                yield ndjson_lines(fresh)
                emitted += len(fresh)
            cursor = rows[-1].id
            if len(fresh) < len(rows) or len(rows) < batch:
                return  # 撞到 `until_id` 或已经到底

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={
            "content-disposition": f'attachment; filename="studio-logs-{stamp}.ndjson"',
            # 让前端不必解析 Content-Disposition 就能拿到文件名（也便于反向代理改写时兜底）。
            "x-studio-filename": f"studio-logs-{stamp}.ndjson",
            "cache-control": "no-store",
        },
    )


@router.get("/api/v1/logs", response_model=LogPage)
def list_logs(
    request: Request,
    since_id: Annotated[int | None, Query(ge=0, description="只取 id 大于它的日志（增量）")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 200,
    level: Annotated[str | None, Query(description="级别下限：debug|info|warn|error|fatal")] = None,
    task_id: Annotated[str | None, Query(description="只看某个任务")] = None,
    source: Annotated[str | None, Query(description="只看某个来源，如 pool.voice")] = None,
    until_id: Annotated[int | None, Query(ge=0, description="只取 id 小于等于它的日志（往前翻历史）")] = None,
    search: Annotated[str | None, Query(description="message / source 的字面子串")] = None,
) -> LogPage:
    """`since_id` 给定 ⇒ 增量；不给 ⇒ 最近 `limit` 条（都按 id 升序返回）。"""
    state: AppState = request.app.state.studio
    if since_id is None:
        rows = state.logs.recent(
            limit=limit,
            min_level=level,
            task_id=task_id,
            source=source,
            search=search,
            until_id=until_id,
        )
    else:
        rows = state.logs.since(
            since_id, limit=limit, min_level=level, task_id=task_id, source=source, search=search
        )
    return LogPage(
        logs=[LogRow.from_row(row) for row in rows],
        next_since_id=rows[-1].id if rows else since_id,
        limit=limit,
    )


@router.get("/api/v1/logs/export")
def export_logs(
    request: Request,
    since_id: Annotated[int | None, Query(ge=0, description="从这条之后开始导出（增量）")] = None,
    until_id: Annotated[int | None, Query(ge=0, description="导出到这条为止（含）")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT)] = DEFAULT_EXPORT,
    level: Annotated[str | None, Query(description="级别下限")] = None,
    task_id: Annotated[str | None, Query(description="只看某个任务")] = None,
    source: Annotated[str | None, Query(description="只看某个来源")] = None,
    search: Annotated[str | None, Query(description="message / source 的字面子串")] = None,
) -> StreamingResponse:
    """导出 NDJSON（一行一条日志，字段与 `/api/v1/logs` 完全一致）。

    行数**恰等于 `limit`** ⇒ 大概率被截断（前端据此提示"收紧过滤条件再导"）；
    服务端不额外回一行 meta —— 那会让这个文件不再是干净的 NDJSON。
    """
    state: AppState = request.app.state.studio
    return _ndjson_response(
        state.logs,
        since_id=since_id,
        until_id=until_id,
        limit=limit,
        min_level=level,
        task_id=task_id,
        source=source,
        search=search,
    )
