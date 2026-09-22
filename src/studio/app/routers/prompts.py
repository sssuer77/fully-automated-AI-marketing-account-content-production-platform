"""提示词面板 REST 面（T6.2）。

三个端点，三件事
----------------
① ``GET    /api/v1/prompts``                    —— 全部条目 + 生效正文 + 版本；
② ``PUT    /api/v1/prompts/{name}``             —— 存一份覆盖（**先校验、后落盘**）；
③ ``DELETE /api/v1/prompts/{name}/override``    —— 还原一段（删掉覆盖文件）。

为什么改名要带 ``?file=``
-------------------------
"还原"作用在**一段**上（``system`` / ``user`` 是两份独立文件）。不带这个参数的话，
"还原 ideator" 到底是还原哪一份就没有答案了，而默认成"两段都还原"会让一次
误点把两处改动一起丢掉。

为什么这里没有"新建提示词"
--------------------------
条目是**注册表**里的东西（``prompts/manifest.yaml`` + sha256），而注册表是入库的。
面板造一个新条目，就意味着要往仓库文件里写 —— 那是 git 的事，不是面板的事。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from studio.app.deps import AppState, prompt_service_for
from studio.app.schemas.prompts import (
    PromptCatalogResponse,
    PromptEntryModel,
    PromptOutcome,
    PromptSaveBody,
)
from studio.services.prompt_service import PromptCatalogEntry, PromptWriteOutcome

__all__ = ["router"]

router = APIRouter(tags=["prompts"])


def _entry(item: PromptCatalogEntry) -> PromptEntryModel:
    return PromptEntryModel.model_validate(item.to_dict())


def _outcome(outcome: PromptWriteOutcome) -> PromptOutcome:
    return PromptOutcome(
        name=outcome.name,
        changed=list(outcome.changed),
        restored=outcome.restored,
        entry=_entry(outcome.entry),
    )


@router.get("/api/v1/prompts", response_model=PromptCatalogResponse)
def list_prompts(request: Request) -> PromptCatalogResponse:
    """全部提示词条目（含**生效**正文与 ``prompt_version``）。"""
    state: AppState = request.app.state.studio
    service = prompt_service_for(state)
    prompts = service.catalog()
    return PromptCatalogResponse(
        prompts=[_entry(item) for item in prompts],
        override_dir=service.override_dir,
        count=len(prompts),
    )


@router.put("/api/v1/prompts/{name}", response_model=PromptOutcome)
def save_prompt(request: Request, name: str, body: PromptSaveBody) -> PromptOutcome:
    """存一份覆盖：仓库文件一个字节都不动，改的是 ``data/prompts/`` 那一份。

    校验不过 ⇒ 422 ``VALIDATION_FAILED``，**一个字节都不落盘**（``context.unknown``
    会点名"你用了哪个没人填的变量"）。提交的内容与生效那份逐字相同 ⇒
    ``changed=[]``、不写盘、不留痕。
    """
    state: AppState = request.app.state.studio
    outcome = prompt_service_for(state).save(name, system=body.system, user=body.user, reason=body.reason)
    return _outcome(outcome)


@router.delete("/api/v1/prompts/{name}/override", response_model=PromptOutcome)
def restore_prompt(
    request: Request,
    name: str,
    file: Annotated[str, Query(pattern="^(system|user)$", description="还原哪一段")] = "system",
) -> PromptOutcome:
    """还原一段（删掉覆盖文件 ⇒ 退回仓库那一份）。**幂等**：本来就没覆盖也回 200。"""
    state: AppState = request.app.state.studio
    return _outcome(prompt_service_for(state).restore(name, role=file))
