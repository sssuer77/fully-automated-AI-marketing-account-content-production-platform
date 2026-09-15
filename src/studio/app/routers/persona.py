"""人物库 REST 面（T4.13 · §02.4 / §04.1.6 / §04.5.8）。

这一层只做三件事
----------------
① 把 HTTP 请求翻成 :class:`~studio.services.persona_service.PersonaService` 调用；
② 把服务层的数据形状交给 Pydantic 复核（``model_validate(to_dict())``）；
③ 让 :class:`~studio.core.errors.StudioError` 自己冒到应用级 handler
   （``app/errors.py``）—— 「表单校验不通过」返回 422 ``PERSONA_INVALID`` +
   ``context.field_errors``，面板据此把红字标到具体输入框上。

为什么没有 `/validate` 端点
--------------------------
保存本身就**先校验后落盘**（`PersonaStore.update`）。再开一个"只校验"的端点，等于
同一套判定跑在两条路径上 —— 两条路径的差异就是"面板说没问题、保存却 422"的来源。
表单要即时反馈，前端拿 ``limits`` 自己判长度即可；**真判定只有一次**，在保存那一下。

为什么切换 / 保存都返回 ``changed``
----------------------------------
"我已经是他了"不是失败，但也不该假装做了一次操作（不写盘、不备份、不留痕）。
把这件事如实回给面板，比让它"看起来成功了"更有用。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from studio.app.deps import AppState, persona_service_for
from studio.app.schemas.common import clean_reason
from studio.app.schemas.persona import (
    PersonaActivateRequest,
    PersonaOutcomeModel,
    PersonaResponse,
    PersonaRollbackRequest,
    PersonaSaveAsRequest,
    PersonaUpdateRequest,
)

__all__ = ["router"]

router = APIRouter(tags=["persona"])


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/v1/persona", response_model=PersonaResponse)
def get_persona(request: Request) -> PersonaResponse:
    """一次拿全：激活人物 + 人物库 + 备份 + 表单上下限（面板首屏就这一个请求）。

    激活文件坏了**也返回 200**（``active_error`` / ``active.stale`` 带原因）：
    这个面板存在的意义就是"人物坏了的时候把它修回来"，一个 500 恰好把工具关在门外。
    """
    state: AppState = request.app.state.studio
    return PersonaResponse.model_validate(persona_service_for(state).read().to_dict())


# ══════════════════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/v1/persona", response_model=PersonaOutcomeModel)
def update_persona(request: Request, body: PersonaUpdateRequest) -> PersonaOutcomeModel:
    """保存表单（**先校验、再备份、后落盘**：校验不过一个字节都不写）。"""
    state: AppState = request.app.state.studio
    outcome = persona_service_for(state).update(
        changes=body.changes(),
        reason=clean_reason(body.reason),
    )
    return PersonaOutcomeModel.model_validate(outcome.to_dict())


@router.post("/api/v1/persona/activate", response_model=PersonaOutcomeModel)
def activate_persona(request: Request, body: PersonaActivateRequest) -> PersonaOutcomeModel:
    """一键切换（旧版自动备份 ⇒ 换错人永远退得回来）。"""
    state: AppState = request.app.state.studio
    outcome = persona_service_for(state).activate(
        persona_id=body.persona_id,
        backup=body.backup,
        reason=clean_reason(body.reason),
    )
    return PersonaOutcomeModel.model_validate(outcome.to_dict())


@router.post("/api/v1/persona/save-as", response_model=PersonaOutcomeModel)
def save_as_persona(request: Request, body: PersonaSaveAsRequest) -> PersonaOutcomeModel:
    """把当前激活人物存进人物库（"改完存一份"，便于随时切回）。"""
    state: AppState = request.app.state.studio
    outcome = persona_service_for(state).save_as(
        persona_id=body.persona_id,
        overwrite=body.overwrite,
        reason=clean_reason(body.reason),
    )
    return PersonaOutcomeModel.model_validate(outcome.to_dict())


@router.post("/api/v1/persona/rollback", response_model=PersonaOutcomeModel)
def rollback_persona(request: Request, body: PersonaRollbackRequest) -> PersonaOutcomeModel:
    """回滚到某份备份（**先备份当前** ⇒ 回滚本身也可以再回滚）。"""
    state: AppState = request.app.state.studio
    outcome = persona_service_for(state).rollback(
        name=body.name,
        backup=body.backup,
        reason=clean_reason(body.reason),
    )
    return PersonaOutcomeModel.model_validate(outcome.to_dict())
