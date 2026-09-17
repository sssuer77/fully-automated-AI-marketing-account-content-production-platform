"""素材库 REST 面（T4.8 · §3.3.14 / §4.3.1 / §04.5.12）。

这一层只做三件事
----------------
① 把 HTTP 请求翻成 :class:`~studio.services.asset_service.AssetService` 调用；
② 把服务层的数据形状交给 Pydantic 复核（``model_validate(to_dict())``）；
③ 让 :class:`~studio.core.errors.StudioError` 自己冒到应用级 handler
   （``app/errors.py``）—— 「授权类型不合法」返回 422 ``ASSET_INVALID``，
   「这条不在库里」返回 404 ``ASSET_NOT_FOUND``。

为什么「扫盘」和「入库」是**同一个端点的两个模式**
------------------------------------------------
``POST /assets/ingest`` 带 ``dry_run=true`` 就是「先看看会怎样」。分成两个端点会让
两条路径各写一遍「怎么判定一条素材合格」—— 而它们的差异，恰恰就是「预览说能进、
真入库却被拒」的来源。判定只有一份（``assets/validate.py``），跑两次而已。

为什么扫描/启停**不新增 WS 事件**
---------------------------------
§04.4.3 的事件表是**被契约测试解析的契约**，为一次素材扫描新增一种事件不划算：
入库本来就会往 ``system_logs`` 写一行（``source='assets'``），面板靠 ``logs`` 通道
自己刷新，「谁动了素材库」这条信息一条都不少（与 T4.11 / T4.7 同一条裁定）。

为什么「停用」不删文件
----------------------
Q3/§T4.8 的硬约束：**只允许禁用（不物理删除）**。误删一柜子素材是不可逆的，
而「停用」随时能点回来；真要腾空间，是用户自己在资源管理器里做的决定。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from studio.app.deps import AppState, asset_service_for
from studio.app.schemas.assets import (
    AssetIngestRequest,
    AssetItemModel,
    AssetKindStatsModel,
    AssetLibraryModel,
    AssetPatchRequest,
    AssetStatsResponse,
    ScanReportModel,
)
from studio.assets.layout import ASSET_ID_PATTERN, AssetKind
from studio.assets.validate import (
    BGM_MIN_DURATION_MS,
    BROLL_LIBRARY_MIN_CLIPS,
    BROLL_LIBRARY_MIN_MS,
    VOICE_MAX_SEGMENTS,
    VOICE_MIN_SEGMENTS,
    VOICE_SEGMENT_MAX_MS,
    VOICE_SEGMENT_MIN_MS,
)
from studio.core.errors import ErrorCode, StudioError
from studio.services.asset_service import LICENSES, AssetService, row_to_dict

__all__ = ["router"]

router = APIRouter(tags=["assets"])

#: 素材 id 的入参上限（与 `ASSET_ID_PATTERN` 是**同一条**正则：id 会进 SQL 参数、
#: 会拼进文件名、会在 URL 里当 query，一个空格或一个 `..` 都是后面某处的事故）
_ASSET_ID = PathParam(min_length=1, max_length=64, pattern=ASSET_ID_PATTERN)


def _service(state: AppState) -> AssetService:
    return asset_service_for(state)


def _missing(asset_id: str) -> StudioError:
    """「这条不在库里」—— 404，不是 422：请求没写错，是它还没入库。"""
    return StudioError(
        f"素材不在库里：{asset_id}",
        code=ErrorCode.ASSET_NOT_FOUND,
        context={"id": asset_id},
        remediation="先在素材库面板里扫盘入库（POST /api/v1/assets/ingest）",
    )


def _kind_of(service: AssetService, asset_id: str, kind: AssetKind | None) -> AssetKind:
    """定这条素材属于哪一类：给了 ``kind`` 就用它，没给就跨类反查。

    反查命中两类（比如一个音色目录正好叫 ``bgm_001``）⇒ 服务层抛
    ``ASSET_INVALID``。猜一个然后改错行，比报错难查得多。
    """
    if kind is not None:
        return kind
    hit = service.resolve(asset_id)
    if hit is None:
        raise _missing(asset_id)
    return hit[0]


def _thresholds() -> dict[str, Any]:
    """判据线**现取**（前端不抄第二份，与 §04.5.9 裁定 161 同一条）。"""
    return {
        "broll_min_clips": BROLL_LIBRARY_MIN_CLIPS,
        "broll_min_duration_ms": BROLL_LIBRARY_MIN_MS,
        "bgm_min_duration_ms": BGM_MIN_DURATION_MS,
        "voice_min_segments": VOICE_MIN_SEGMENTS,
        "voice_max_segments": VOICE_MAX_SEGMENTS,
        "voice_segment_min_ms": VOICE_SEGMENT_MIN_MS,
        "voice_segment_max_ms": VOICE_SEGMENT_MAX_MS,
    }


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/v1/assets", response_model=AssetLibraryModel)
def get_assets(request: Request) -> AssetLibraryModel:
    """一次拿全：三类分节（条目 + 家底 + 缺口）+ 降级判定（面板首屏就这一个请求）。"""
    state: AppState = request.app.state.studio
    return AssetLibraryModel.model_validate(_service(state).library().to_dict())


@router.get("/api/v1/assets/stats", response_model=AssetStatsResponse)
def get_asset_stats(request: Request) -> AssetStatsResponse:
    """只要数字：三类家底 + 判据线（总览台的「素材够不够」小卡片用这个，不拖整库）。"""
    state: AppState = request.app.state.studio
    library = _service(state).library()
    sections = [
        AssetKindStatsModel.model_validate(
            {
                "kind": section.to_dict()["kind"],
                "root": section.root,
                "stats": section.stats.to_dict(),
                "shortfall": section.shortfall,
                "disk_total": section.disk_total,
                "usable": section.usable,
            }
        )
        for section in library.sections
    ]
    return AssetStatsResponse(
        sections=sections,
        degraded=library.degraded,
        note=library.note,
        thresholds=_thresholds(),
        licenses=sorted(LICENSES),
    )


# ══════════════════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/assets/ingest", response_model=ScanReportModel)
def ingest_assets(request: Request, body: AssetIngestRequest) -> ScanReportModel:
    """扫盘 / 入库（``dry_run=true`` ⇒ **一个字节都不写库**）。

    ``license`` 只对本次新入库的条目生效；已入库的按行里存的那份走 —— 否则一次
    「顺手全扫」会把每条素材的授权都改成本次请求里填的那个。
    """
    state: AppState = request.app.state.studio
    service = _service(state)
    if body.dry_run:
        report = service.scan(kind=body.kind, ids=body.ids, license=body.license)
    else:
        report = service.ingest(kind=body.kind, ids=body.ids, license=body.license)
    return ScanReportModel.model_validate(report.to_dict())


@router.patch("/api/v1/assets/{asset_id}", response_model=AssetItemModel)
def patch_asset(
    request: Request,
    asset_id: str = _ASSET_ID,
    *,
    body: AssetPatchRequest,
) -> dict[str, Any]:
    """改一条素材（启用 / 停用 / 授权 / 标签 / 可用区间 / 情绪…），并留痕。

    ``enabled`` 走的是**同一条** :meth:`AssetService.set_enabled` 路径（幂等：
    状态没变就不留痕），而不是这里再写一遍「赋值 + 记日志」。

    返回的是**行字典**，由 ``response_model``（``kind`` 判别的联合）复核 ——
    路由这一层不重复实现「三类各自长什么样」，那是契约的事。
    """
    state: AppState = request.app.state.studio
    service = _service(state)
    kind = _kind_of(service, asset_id, body.kind)
    changes = body.changes()
    if not changes:
        # 空 PATCH 不是错误，但也不该假装改了一次：回当前行
        row = service.get(kind, asset_id)
        if row is None:
            raise _missing(asset_id)
        return row_to_dict(row)
    updated = service.patch(kind, asset_id, changes)
    return row_to_dict(updated)


# ══════════════════════════════════════════════════════════════════════
# 二进制：预览与试听
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/v1/assets/{asset_id}/thumb")
def get_asset_thumb(request: Request, asset_id: str = _ASSET_ID) -> FileResponse:
    """缩略图（跑酷素材的抽帧图；**没有就 404**，不临时现抽 —— 那是入库该干的事）。"""
    state: AppState = request.app.state.studio
    service = _service(state)
    kind = _kind_of(service, asset_id, None)
    path = state.paths.thumb_file(str(kind), asset_id)
    if not path.is_file():
        raise StudioError(
            f"缩略图还没生成：{asset_id}",
            code=ErrorCode.ASSET_NOT_FOUND,
            context={"id": asset_id, "thumb": str(path)},
            remediation="重新入库这一条（入库时抽帧；抽帧失败不阻塞入库，所以可能一直没有）",
        )
    return FileResponse(path, media_type="image/jpeg")


@router.get("/api/v1/assets/{asset_id}/media")
def get_asset_media(request: Request, asset_id: str = _ASSET_ID) -> FileResponse:
    """原文件（BGM 试听 / 跑酷预览）。

    路径**只从库里取**（``row.path``），绝不拼请求参数 —— 素材 id 是白名单正则，
    但真正的路径来自 DB 行，这中间没有一处能让 ``../`` 生效。

    音色不在这里给：``voice_profiles.path`` 是**目录**（``ref_NN`` + ``ref.txt`` +
    ``profile.json``），不是能播的文件。面板按条目里的路径逐段播放。
    """
    state: AppState = request.app.state.studio
    service = _service(state)
    kind = _kind_of(service, asset_id, None)
    row = service.get(kind, asset_id)
    if row is None:
        raise _missing(asset_id)
    if kind is AssetKind.VOICE:
        raise StudioError(
            f"音色不是一个文件：{asset_id}",
            code=ErrorCode.ASSET_INVALID,
            context={"id": asset_id, "kind": str(kind)},
            remediation="音色目录里的每段参考音各自播放（见条目里的 path）",
        )
    path = Path(str(row.path))
    if not path.is_file():
        raise StudioError(
            f"素材文件不在了：{path}",
            code=ErrorCode.ASSET_NOT_FOUND,
            context={"id": asset_id, "path": str(path)},
            remediation="把文件放回原位，或在面板里停用这一条（**不会**替你删库里的记录）",
        )
    guessed, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=guessed or "application/octet-stream")
