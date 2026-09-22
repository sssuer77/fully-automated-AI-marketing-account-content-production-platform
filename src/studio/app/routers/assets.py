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

为什么「停用」不删文件，而「删除」要分成两个开关
------------------------------------------------
Q3/§T4.8 的硬约束：**只允许禁用（不物理删除）**。误删一柜子素材是不可逆的，
而「停用」随时能点回来；真要腾空间，是用户自己在资源管理器里做的决定。

裁定 369 在那条约束上加了一个显式的例外：``DELETE /assets/{id}`` 默认**只删库里的
行**（安全的那一半，重扫一次就回来），``purge=true`` 才连盘上那份一起删。分成两个
开关的原因是音色与跑酷不一样 —— 参考音目录留在盘上，下次扫盘又会变成一条"盘上有、
库里没有"，用户刚删掉的东西自己回来了；而跑酷删了行，文件还在、出片照样挑得到。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from studio.app.deps import AppState, asset_service_for
from studio.app.schemas.assets import (
    AssetDeleteModel,
    AssetIngestRequest,
    AssetItemModel,
    AssetKindStatsModel,
    AssetLibraryModel,
    AssetPageModel,
    AssetPatchRequest,
    AssetStatsResponse,
    ScanReportModel,
    UploadedFileModel,
    UploadResultModel,
)
from studio.assets.layout import ASSET_ID_PATTERN, VOICE_TEXT_NAME, AssetKind, root_for
from studio.assets.upload import (
    check_suffix,
    copy_into_place,
    ref_name,
    target_for,
    write_text_into_place,
)
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
from studio.services.asset_service import (
    DEFAULT_PAGE_SIZE,
    LICENSES,
    MAX_PAGE_SIZE,
    AssetService,
    check_license,
    row_to_dict,
)

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


@router.delete("/api/v1/assets/{asset_id}", response_model=AssetDeleteModel)
def delete_asset(
    request: Request,
    asset_id: str = _ASSET_ID,
    *,
    kind: Annotated[AssetKind | None, Query(description="哪一类；不给就跨类反查")] = None,
    purge: Annotated[bool, Query(description="连盘上那份一起删（音色是目录，不可逆）")] = False,
) -> dict[str, Any]:
    """删掉一条素材（裁定 369：**默认只删库里的行**，``purge=true`` 才动盘上的文件）。

    面板上这件事是二次确认的，而且对音色默认勾上 ``purge`` —— 理由见模块头部那条
    裁定：音色不删盘，下次扫盘它自己就回来了。
    """
    state: AppState = request.app.state.studio
    service = _service(state)
    resolved = _kind_of(service, asset_id, kind)
    return service.delete(resolved, asset_id, purge=purge).to_dict()


@router.get("/api/v1/assets/list", response_model=AssetPageModel)
def list_assets(
    request: Request,
    kind: Annotated[AssetKind, Query(description="哪一类（跑酷 / 音色 / BGM 各是一个菜单）")],
    page: Annotated[int, Query(ge=1, description="第几页（1 起）")] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="每页几条")] = DEFAULT_PAGE_SIZE,
    q: Annotated[str | None, Query(max_length=64, description="按 id 或标签筛（不区分大小写）")] = None,
    enabled: Annotated[bool | None, Query(description="只看启用 / 只看停用；不给 = 全部")] = None,
) -> AssetPageModel:
    """**一类素材的一页**（T4.8：素材库按类别分菜单 + 分页）。

    与 ``GET /api/v1/assets`` 的分工
    --------------------------------
    那个端点一次给三类（每类的**全部**条目），适合"我就要一眼看全"；这个端点给一类
    的**一页**，适合"我要管理一柜子素材"。两者的 ``usable`` / ``shortfall`` 走的是
    **同一份** ``_section()``，所以同一类在两处不会给出不同的颜色。

    为什么 ``page`` / ``page_size`` 在**入参**就卡范围
    -------------------------------------------------
    ``page_size`` 不设上限的话，一个 ``page_size=100000`` 就把这个接口变回"把整库拖
    过来"—— 而那正是分页要避免的事。越界的页码**不报错**（服务层钳到最后一页）：
    "翻过头"最常见的成因是"你刚把最后一页的素材停用/筛掉了"，报错只会让人以为接口坏了。
    """
    state: AppState = request.app.state.studio
    service = _service(state)
    result = service.page(kind, page=page, page_size=page_size, query=q, enabled=enabled)
    return AssetPageModel.model_validate(result.to_dict())


# ══════════════════════════════════════════════════════════════════════
# 写：浏览器上传（T4.8 的图形化入库入口）
# ══════════════════════════════════════════════════════════════════════


def _skipped(filename: str, message: str) -> UploadedFileModel:
    """一条"没写进去"的结局（**明说原因**，绝不静默少一个文件）。"""
    return UploadedFileModel(filename=filename, asset_id=None, status="skipped", message=message)


def _counts(rows: list[UploadedFileModel]) -> dict[str, int]:
    return {
        "stored": sum(1 for row in rows if row.status == "stored"),
        "replaced": sum(1 for row in rows if row.status == "replaced"),
        "skipped": sum(1 for row in rows if row.status == "skipped"),
    }


def _ingest_now(
    service: AssetService, *, kind: AssetKind, ids: list[str], license: str | None
) -> ScanReportModel | None:
    """落盘之后**立刻**入库一次 —— 回执里那句"能不能用"就是入库那条路算的。

    判定只有一份：这里不重算时长 / 采样率 / 削波，那些是 ``assets/validate.py``
    在 ``ingest`` 里做的。上传这条路上唯一多做的一件事，是**把文件放到该在的地方**。
    """
    if not ids:
        return None
    return ScanReportModel.model_validate(service.ingest(kind=kind, ids=ids, license=license).to_dict())


def _message(status: str, target: Path, original: str, suffix_note: str = "") -> str:
    head = "已覆盖" if status == "replaced" else "已落盘"
    renamed = "" if Path(original).stem == target.stem else f"（原名 {original}）"
    return f"{head} {target.name}{suffix_note}{renamed}"


@router.post("/api/v1/assets/upload", response_model=UploadResultModel)
def upload_assets(
    request: Request,
    kind: Annotated[AssetKind, Form()],
    files: Annotated[list[UploadFile], File()],
    license: Annotated[str | None, Form()] = None,
    overwrite: Annotated[bool, Form()] = False,
) -> UploadResultModel:
    """把文件传进跑酷 / BGM 目录，并**当场入库**（§3.3.14）。

    为什么"上传"与"入库"是同一个动作
    --------------------------------
    用户点的是"把这个文件放进素材库"，不是"把文件放进一个目录、然后我再去点一次
    扫描"。分成两步的后果，是面板上多出一批"盘上有、库里没有"的条目 —— 而那正是
    最容易被误读的一种（"我明明传了啊"）。

    为什么 `license` 在写盘**之前**校验
    -----------------------------------
    写了一半才 422，素材目录里会留下一批"没人认领"的文件，而它们看上去和正常素材
    一模一样。

    为什么冲突不是整体 409
    ----------------------
    一次拖 20 个文件进去，其中 1 个撞名就整批失败是最糟的交互。撞名的那个按
    ``skipped`` 逐条报出来（带上原因），其余照常落盘 —— 与扫盘"坏文件不中断整批"
    同一条。
    """
    state: AppState = request.app.state.studio
    service = _service(state)
    if kind is AssetKind.VOICE:
        raise StudioError(
            "音色不是平铺文件（它是一个目录：ref_NN + ref.txt）",
            code=ErrorCode.ASSET_INVALID,
            context={"kind": str(kind)},
            remediation="音色走 POST /api/v1/assets/voice（面板音色那一节的「上传参考音」）",
        )
    if not files:
        raise StudioError(
            "没有收到任何文件",
            code=ErrorCode.ASSET_INVALID,
            context={"kind": str(kind)},
            remediation="先在面板上选文件，或者把它们拖进这一类",
        )
    check_license(license)

    rows: list[UploadedFileModel] = []
    ids: list[str] = []
    for upload in files:
        filename = upload.filename or ""
        try:
            check_suffix(kind, filename)
            target = target_for(state.paths, kind, filename)
            status = copy_into_place(upload.file, target, overwrite=overwrite)
        except StudioError as exc:
            rows.append(_skipped(filename, exc.message))
            continue
        except OSError as exc:
            rows.append(_skipped(filename, f"写盘失败：{exc}"))
            continue
        ids.append(target.stem)
        rows.append(
            UploadedFileModel(
                filename=filename,
                asset_id=target.stem,
                status=status,
                message=_message(status, target, filename),
            )
        )

    return UploadResultModel(
        kind=str(kind),
        root=str(root_for(state.paths, kind)),
        overwrite=overwrite,
        files=rows,
        report=_ingest_now(service, kind=kind, ids=ids, license=license),
        **_counts(rows),
    )


@router.post("/api/v1/assets/voice", response_model=UploadResultModel)
def upload_voice(
    request: Request,
    voice_id: Annotated[str, Form(min_length=1, max_length=64, pattern=ASSET_ID_PATTERN)],
    files: Annotated[list[UploadFile], File()],
    ref_text: Annotated[str | None, Form()] = None,
    license: Annotated[str | None, Form()] = None,
    overwrite: Annotated[bool, Form()] = False,
) -> UploadResultModel:
    """传一个音色的参考音（§4.3.1：``ref_01`` … + ``ref.txt``），并当场入库。

    ``voice_id`` 就是目录名，也**就是**素材 id：与 ``ASSET_ID_PATTERN`` 同一条正则
    （id 会进 SQL 参数、会拼进路径、会在 URL 里当 query，一个 ``..`` 都是事故）。

    顺序为什么要排序
    ----------------
    ``ref.txt`` 的第 N 行对应第 N 段参考音，顺序是**契约的一部分**。而用户在文件
    选择框里没法指定顺序，浏览器给的顺序每台机器还不一样 —— 按原文件名排序至少是
    **可预期**的，并且面板会把"第 N 段 ← 哪个原文件"逐条报出来。
    """
    state: AppState = request.app.state.studio
    service = _service(state)
    if not files:
        raise StudioError(
            "没有收到任何参考音文件",
            code=ErrorCode.ASSET_INVALID,
            context={"voice_id": voice_id},
            remediation="选 2–3 段 2–30 秒的干净人声（§4.3.1 · 下限见裁定 369）",
        )
    check_license(license)

    root = state.paths.voice_src_dir / voice_id
    ordered = sorted(files, key=lambda item: item.filename or "")
    rows: list[UploadedFileModel] = []
    written = 0
    segment = 0
    for upload in ordered:
        filename = upload.filename or ""
        try:
            check_suffix(AssetKind.VOICE, filename)
        except StudioError as exc:
            rows.append(_skipped(filename, exc.message))
            continue
        # 段号只数**后缀合格**的那些：中间夹一个 .txt 不该让后面的段跳号，
        # 否则 ref.txt 的行与 ref_NN 会整体错位一格（最难查的一种对不上）。
        segment += 1
        target = root / ref_name(segment, filename)
        try:
            status = copy_into_place(upload.file, target, overwrite=overwrite)
        except StudioError as exc:
            rows.append(_skipped(filename, exc.message))
            continue
        except OSError as exc:
            rows.append(_skipped(filename, f"写盘失败：{exc}"))
            continue
        written += 1
        rows.append(
            UploadedFileModel(
                filename=filename,
                asset_id=voice_id,
                status=status,
                message=_message(status, target, filename, suffix_note=f"（第 {segment} 段）"),
            )
        )

    if ref_text is not None and ref_text.strip():
        body = "\n".join(line.strip() for line in ref_text.splitlines() if line.strip()) + "\n"
        text_target = root / VOICE_TEXT_NAME
        try:
            status = write_text_into_place(text_target, body, overwrite=overwrite)
        except StudioError as exc:
            rows.append(_skipped(VOICE_TEXT_NAME, exc.message))
        except OSError as exc:
            rows.append(_skipped(VOICE_TEXT_NAME, f"写盘失败：{exc}"))
        else:
            written += 1
            rows.append(
                UploadedFileModel(
                    filename=VOICE_TEXT_NAME,
                    asset_id=voice_id,
                    status=status,
                    message=_message(status, text_target, VOICE_TEXT_NAME, suffix_note="（逐行对应参考音）"),
                )
            )

    return UploadResultModel(
        kind=str(AssetKind.VOICE),
        root=str(root),
        overwrite=overwrite,
        files=rows,
        report=_ingest_now(service, kind=AssetKind.VOICE, ids=[voice_id] if written else [], license=license),
        **_counts(rows),
    )


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
