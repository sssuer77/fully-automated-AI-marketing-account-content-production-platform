"""提示词面板契约（T6.2）—— 提示词的界面化编辑。

面板要回答的四个问题，一个模型对一块
------------------------------------
① 「有哪些提示词、现在生效的是哪一份？」 ⇒ :class:`PromptEntryModel`
② 「这段读起来不对，改成这样」 ⇒ :class:`PromptSaveBody`
③ 「还是退回仓库那份吧」 ⇒ :class:`PromptOutcome`（``restored=true``）
④ 「我改的到底生效没有？」 ⇒ :class:`PromptEntryModel.prompt_version` +
   ``overridden`` —— 版本号跟着覆盖走（``digest`` 走生效那一份）

为什么请求体里 ``system=None`` 是「不动」而不是「清空」
------------------------------------------------------
两者在 JSON 里长得一模一样（字段缺省 vs 显式 null 在多数客户端里没有区别），
后果却差很远：某次请求少带一个字段，那一整段纪律就被静默删掉了，而症状要到
下一次生成才出现。所以"清空一段提示词"这件事**没有**端点 —— 想删就直接把
文本改短，改成一个空串会被 422 拦下。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from studio.app.schemas.common import MAX_REASON

__all__ = [
    "PromptCatalogResponse",
    "PromptEntryModel",
    "PromptFileModel",
    "PromptOutcome",
    "PromptSaveBody",
]

PromptRole = Literal["system", "user"]


class PromptFileModel(BaseModel):
    """一个条目里的一段（``system`` 或 ``user``）。"""

    model_config = ConfigDict(extra="forbid")

    role: PromptRole
    #: 相对 ``prompts/`` 的路径（覆盖文件落在 ``data/prompts/<同一个相对路径>``）
    path: str
    #: **生效**的正文（有覆盖就是覆盖那一份）
    text: str
    overridden: bool


class PromptEntryModel(BaseModel):
    """面板上的一个提示词条目。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    #: ``manifest.yaml`` 里登记的版本号（人写的那个）
    version: str
    #: ``<version>+<sha12>`` —— 会落 ``scripts.prompt_version`` 的可复现值
    prompt_version: str
    #: 有任何一段被覆盖
    overridden: bool
    files: list[PromptFileModel]
    #: 这个条目渲染时拿得到的变量（覆盖里只许引用它们）
    variables: list[str]


class PromptCatalogResponse(BaseModel):
    """全部条目（面板的下拉框）。"""

    model_config = ConfigDict(extra="forbid")

    prompts: list[PromptEntryModel]
    #: 覆盖目录（``data/prompts``）—— 面板上要能指出"改的东西落在哪"
    override_dir: str | None
    count: int


class PromptSaveBody(BaseModel):
    """保存覆盖：**只处理显式给过的那一段**。"""

    model_config = ConfigDict(extra="forbid")

    system: str | None = None
    user: str | None = None
    reason: str | None = Field(default=None, max_length=MAX_REASON)

    @model_validator(mode="after")
    def _at_least_one(self) -> PromptSaveBody:
        if self.system is None and self.user is None:
            raise ValueError("至少要给一段提示词（system 或 user）")
        return self


class PromptOutcome(BaseModel):
    """一次保存 / 还原的结果（含**改完之后**的条目，省一次往返）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    #: 真的写盘 / 真的删掉覆盖的那几段（空 = 提交的内容与生效那份逐字相同）
    changed: list[str]
    #: ``true`` ⇒ 这一次是"退回仓库那一份"
    restored: bool
    entry: PromptEntryModel
