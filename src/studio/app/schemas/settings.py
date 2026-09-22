"""设置面板契约（LLM 通道与密钥）。

面板要回答的四个问题，一个模型对一块
------------------------------------
① 「这台机器现在配了没有、哪来的？」 ⇒ :class:`LlmKeyModel`（**只有掩码**）
② 「有哪几条通道、哪条现在能用、缺什么？」 ⇒ :class:`LlmProfileModel`
③ 「填进去 / 换一把 / 清掉」 ⇒ :class:`LlmKeyRequest`
④ 「填完真的通吗？」 ⇒ :class:`LlmProbeModel`（按需探测，只发只读 GET）
⑤ 「用哪个模型？」 ⇒ :class:`LlmProfileRequest` / :class:`LlmProfileOutcome`

为什么请求体用 ``clear`` 而不是「``api_key`` 传 null 就是清空」
--------------------------------------------------------------
两者在 JSON 里长得几乎一样，后果却差很远：面板某次请求少带一个字段
（比如以后加了「只改模型名」的端点顺手复用了这个模型），密钥就被**静默删掉**了，
而症状要到下一次真跑任务才出现（``no_key``）。清空必须是**显式**动作。

为什么响应里没有明文字段
------------------------
一个都没有，不是「有但默认不返回」—— 后者迟早会被某个 ``?include=key``
打开。要显示就显示 :func:`studio.core.secret_store.mask_secret` 的产物。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from studio.app.schemas.common import MAX_REASON
from studio.core.secret_store import API_KEY_MAX_LEN

__all__ = [
    "LlmKeyModel",
    "LlmKeyOutcome",
    "LlmKeyRequest",
    "LlmLimitsModel",
    "LlmProbeModel",
    "LlmProbeRowModel",
    "LlmProfileModel",
    "LlmProfileOutcome",
    "LlmProfileRequest",
    "LlmRoutingModel",
    "LlmSettingsResponse",
]

#: 模型名长度上限。给得宽松（有的服务商把版本与日期都塞进名字里），
#: 但**必须有上限**：这是直接写进配置文件的一行，不能是任意长的输入。
MODEL_MAX_LEN = 200

#: base_url 长度上限
BASE_URL_MAX_LEN = 500

KeySourceLiteral = Literal["env", "file", "none"]
ProbeStatusLiteral = Literal["ok", "no_key", "unreachable", "http_error"]


class LlmKeyModel(BaseModel):
    """密钥的**静态**状态（不发任何请求就能回答）。"""

    model_config = ConfigDict(extra="forbid")

    configured: bool
    masked_key: str | None
    source: KeySourceLiteral
    source_label: str
    env_var: str
    path: str
    file_exists: bool
    version: int
    loaded_at: str
    last_error: str | None
    #: 环境变量**已经**占了位置 ⇒ 面板写的这一份**不生效**（必须说出来）
    env_overrides_file: bool


class LlmProfileModel(BaseModel):
    """一条通道的静态状态。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    engine: str
    base_url: str
    model: str
    api_key_env: str | None
    is_default: bool
    needs_key: bool
    usable: bool
    detail: str


class LlmRoutingModel(BaseModel):
    """一个 Agent 走哪条通道（只读展示；改它属于 ``llm.yaml``，不在这一屏）。"""

    model_config = ConfigDict(extra="forbid")

    agent: str
    profile: str
    fallback: str | None


class LlmLimitsModel(BaseModel):
    """密钥长度上下限（唯一真相在 ``secret_store``，前端不抄第二份）。"""

    model_config = ConfigDict(extra="forbid")

    min_len: int
    max_len: int


class LlmSettingsResponse(BaseModel):
    """设置面板首屏（一个请求拿全）。"""

    model_config = ConfigDict(extra="forbid")

    generated_at: str
    default_profile: str
    profiles: list[LlmProfileModel]
    routing: list[LlmRoutingModel]
    key: LlmKeyModel
    limits: LlmLimitsModel
    #: 需要用户知道、但不构成错误的话（如「环境变量优先，面板这份暂不生效」）
    notes: list[str]


class LlmKeyRequest(BaseModel):
    """写 / 清密钥。

    ``api_key`` 与 ``clear`` **二选一**：都没给 ⇒ 422（不知道你想干嘛），
    都给了 ⇒ 422（"填一把新的"与"删掉"不可能同时成立）。
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, max_length=API_KEY_MAX_LEN)
    clear: bool = False
    reason: str | None = Field(default=None, max_length=MAX_REASON)

    @model_validator(mode="after")
    def _exactly_one(self) -> LlmKeyRequest:
        wants_write = self.api_key is not None
        if wants_write and self.clear:
            raise ValueError("不能同时提交 api_key 与 clear")
        if not wants_write and not self.clear:
            raise ValueError("要么提交 api_key，要么 clear=true")
        return self


class LlmProfileRequest(BaseModel):
    """改写某条通道的模型名 / base_url。

    ``model`` 与 ``base_url`` **至少要给一个**：两个都不给 ⇒ 422（不知道你想改什么）。
    与 :class:`LlmKeyRequest` 的 ``clear`` 同一条理由 —— "少带一个字段"绝不能变成
    "把某个值改成空"。
    """

    model_config = ConfigDict(extra="forbid")

    profile: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=MODEL_MAX_LEN)
    base_url: str | None = Field(default=None, min_length=1, max_length=BASE_URL_MAX_LEN)
    reason: str | None = Field(default=None, max_length=MAX_REASON)

    @field_validator("model")
    @classmethod
    def _model_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("模型名不能是空白")
        return None if value is None else value.strip()

    @field_validator("base_url")
    @classmethod
    def _base_url_is_http(cls, value: str | None) -> str | None:
        """只收 http(s)：写一个 ``ftp://`` 进去的后果是**下一次调用才炸**。"""
        if value is None:
            return None
        stripped = value.strip().rstrip("/")
        if not stripped.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return stripped

    @model_validator(mode="after")
    def _at_least_one(self) -> LlmProfileRequest:
        if self.model is None and self.base_url is None:
            raise ValueError("至少要给 model 或 base_url 之一")
        return self


class LlmProfileOutcome(LlmSettingsResponse):
    """一次通道参数写入的结果 = **刷新后的整屏** + 这次改了什么。

    与 :class:`LlmKeyOutcome` 同一条：面板写完要立刻显示新模型名，只回 ``changed``
    的话前端还得再发一次 GET，那两次请求之间显示的是**旧**模型。
    """

    changed: bool
    profile: str
    model: str
    base_url: str
    reason: str | None


class LlmKeyOutcome(LlmSettingsResponse):
    """一次写入的结果 = **刷新后的整屏** + 这次做了什么。

    为什么把整屏带回来：面板写完要立刻显示新状态（掩码、来源、通道能不能用）。
    只回一个 ``changed`` 的话，前端得再发一次 GET —— 那一次 GET 与这一次 PUT 之间
    的窗口里，面板显示的是**旧的**掩码，用户会以为没存上。
    """

    changed: bool
    cleared: bool
    reason: str | None


class LlmProbeRowModel(BaseModel):
    """一条通道的探测结果。"""

    model_config = ConfigDict(extra="forbid")

    profile: str
    engine: str
    model: str
    base_url: str
    status: ProbeStatusLiteral
    detail: str


class LlmProbeModel(BaseModel):
    """一次探测的全部结果（``ok_count`` 让面板能一句话说结论）。"""

    model_config = ConfigDict(extra="forbid")

    generated_at: str
    rows: list[LlmProbeRowModel]
    ok_count: int
