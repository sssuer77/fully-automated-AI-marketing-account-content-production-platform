"""合成配置 REST 面（T4.7 · §04.2.8 / §04.5.9）。

这一层只做三件事
----------------
① 把 HTTP 请求翻成 :class:`~studio.services.outputs_service.OutputsService` 调用；
② 把服务层的数据形状交给 Pydantic 复核（``model_validate(to_dict())``）；
③ 让 :class:`~studio.core.errors.StudioError` 自己冒到应用级 handler
   （``app/errors.py``）—— 「表单校验不通过」返回 422 ``OUTPUTS_INVALID`` +
   ``context.field_errors``，面板据此把红字标到具体输入框上。

为什么没有 `/validate` 端点
--------------------------
保存本身就**先校验后落盘**（`OutputsStore.update`）。再开一个"只校验"的端点，等于
同一套判定跑在两条路径上 —— 两条路径的差异就是"面板说没问题、保存却 422"的来源。
表单要即时反馈，前端拿 ``limits`` 自己判范围即可；**真判定只有一次**，在保存那一下。

为什么读取失败**也返回 200**
----------------------------
``config/outputs.yaml`` 坏了 ⇒ ``stale`` / ``error`` 带原因、``profiles`` 为空，
而不是 500。这个面板存在的意义就是"配置坏了的时候把它修回来"，一个 500 恰好把工具
关在门外（与 §04.5.7 裁定 150 同一条理由）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from studio.app.deps import AppState, outputs_service_for
from studio.app.schemas.common import clean_reason
from studio.app.schemas.outputs import (
    OutputsOutcomeModel,
    OutputsResponse,
    OutputsUpdateRequest,
)

__all__ = ["router"]

router = APIRouter(tags=["outputs"])


@router.get("/api/v1/outputs", response_model=OutputsResponse)
def get_outputs(request: Request) -> OutputsResponse:
    """一次拿全：profile 列表 + 水印 + 字幕 + 表单上下限（面板首屏就这一个请求）。"""
    state: AppState = request.app.state.studio
    return OutputsResponse.model_validate(outputs_service_for(state).read().to_dict())


@router.post("/api/v1/outputs", response_model=OutputsOutcomeModel)
def update_outputs(request: Request, body: OutputsUpdateRequest) -> OutputsOutcomeModel:
    """保存表单（**先校验、后落盘、再回读**：校验不过一个字节都不写）。"""
    state: AppState = request.app.state.studio
    outcome = outputs_service_for(state).update(
        changes=body.changes(),
        expected_sha256=body.source_sha256,
        reason=clean_reason(body.reason),
    )
    return OutputsOutcomeModel.model_validate(outcome.to_dict())
