"""设置面板 REST 面（LLM 通道与密钥）。

这一层只做三件事
----------------
① 把 HTTP 请求翻成 :class:`~studio.services.settings_service.SettingsService` 调用；
② 把服务层的数据形状交给 Pydantic 复核（``model_validate(...)``）；
③ 让 :class:`~studio.core.errors.StudioError` 自己冒到应用级 handler
   （``app/errors.py``）—— 「Key 太短」返回 422 ``VALIDATION_FAILED``，
   面板据此把红字标到输入框上。

为什么是 PUT 而不是 POST
------------------------
写密钥是**幂等替换**（同一个值写两次，结果一样、第二次连盘都不碰）。PUT 的语义
正好是这个；POST 会让人以为"每点一次就多一把"。清除走同一个端点的 ``clear=true``
—— 清空是显式动作，不是"少传一个字段"的副作用。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from studio.app.deps import AppState, settings_service_for
from studio.app.schemas.common import clean_reason
from studio.app.schemas.settings import (
    LlmKeyOutcome,
    LlmKeyRequest,
    LlmProbeModel,
    LlmProfileOutcome,
    LlmProfileRequest,
    LlmSettingsResponse,
)

__all__ = ["router"]

router = APIRouter(tags=["settings"])


@router.get("/api/v1/settings/llm", response_model=LlmSettingsResponse)
def get_llm_settings(request: Request) -> LlmSettingsResponse:
    """设置面板首屏：通道卡片 + 路由表 + 密钥状态（**只有掩码**）。"""
    state: AppState = request.app.state.studio
    return LlmSettingsResponse.model_validate(settings_service_for(state).read())


@router.put("/api/v1/settings/llm", response_model=LlmKeyOutcome)
def put_llm_key(request: Request, body: LlmKeyRequest) -> LlmKeyOutcome:
    """保存 / 清除密钥（**先校验、后落盘**：校验不过一个字节都不写）。

    写完直接回刷新后的整屏 —— 面板不必再发一次 GET（那一次 GET 与这次 PUT 之间的
    窗口里，面板显示的是旧掩码，用户会以为没存上）。
    """
    state: AppState = request.app.state.studio
    payload = settings_service_for(state).write_key(
        api_key=body.api_key,
        clear=body.clear,
        reason=clean_reason(body.reason),
    )
    return LlmKeyOutcome.model_validate(payload)


@router.put("/api/v1/settings/llm/profile", response_model=LlmProfileOutcome)
def put_llm_profile(request: Request, body: LlmProfileRequest) -> LlmProfileOutcome:
    """改写某条通道的模型名 / base_url（写回 ``config/llm.yaml``，**存完立刻生效**）。

    为什么不是「把整份 llm.yaml 传上来」：那份文件每一行都带注释（通道用途、成本口径、
    为什么 fallback 指向本地），整份替换等于让面板来负责保住它们 —— 它保不住。
    这里只认三个字段，落盘时按行改写，段外的 routing / budget / 注释一个字节都不碰。

    生效方式：网关每次取配置先做一次 ``stat``（``core.config.llm_config_provider``）
    ⇒ 常驻的写稿 worker **不需要重启**。
    """
    state: AppState = request.app.state.studio
    payload = settings_service_for(state).write_profile(
        profile=body.profile,
        model=body.model,
        base_url=body.base_url,
        reason=clean_reason(body.reason),
    )
    return LlmProfileOutcome.model_validate(payload)


@router.post("/api/v1/settings/llm/probe", response_model=LlmProbeModel)
async def probe_llm(request: Request) -> LlmProbeModel:
    """按需探测各通道（只发只读 GET，不产生一次计费调用）。

    与 ``studio llm probe`` 走**同一份**判定（``llm_settings_service``）：
    两处各写一份的话，迟早出现「命令行说能进、面板说不能」。
    """
    state: AppState = request.app.state.studio
    return LlmProbeModel.model_validate(await settings_service_for(state).probe())
