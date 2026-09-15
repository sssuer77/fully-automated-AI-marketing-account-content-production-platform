"""人物库契约（T4.13 · §02.4 / §04.1.6 / §04.5.8）。

面板要回答的四个问题，一个模型对一块
------------------------------------
① 「现在用的是谁、哪来的、坏没坏？」 ⇒ :class:`ActivePersonaModel`
   （含 ``stale``：磁盘上那份已经坏了，你看到的是上一份好的）
② 「库里还有哪些人、哪个不能用？」 ⇒ :class:`PersonaLibraryItemModel`
③ 「改完存哪、改错了怎么退？」 ⇒ :class:`PersonaUpdateRequest` / :class:`PersonaRollbackRequest`
④ 「换个人上」 ⇒ :class:`PersonaActivateRequest` / :class:`PersonaSaveAsRequest`

请求体为什么全部 ``extra="forbid"``
-----------------------------------
与总览台 / 四池 / 选题面板同一条：把 ``catchphrases`` 写成 ``catchphrase`` 被静默忽略，
人会以为「口癖已经加上了」，而写出来的稿子一个都没有 —— 这种"以为生效了"最贵。

为什么上下限跟着响应一起下发（:class:`PersonaLimitsModel`）
----------------------------------------------------------
上限的唯一真相是 ``PersonaConfig`` 的字段约束（§04.5.8 裁定 161）。前端不抄第二份，
它照着 ``limits`` 画输入框：某天把 ``target_chars_max`` 从 8000 调到 12000，
面板跟着变，不需要改前端、也不会出现"前端拦着、后端放行"。

响应模型的集合字段一律**必填**（``x: list[T]`` 而非 ``Field(default_factory=list)``）：
后者在 JSON Schema 里既不进 ``required`` 也不带 ``default`` ⇒ 生成类型是
``T[] | undefined``，前端被迫到处 ``?? []``（裁定 135）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from studio.app.schemas.common import MAX_REASON
from studio.core.persona_store import PERSONA_ID_PATTERN

__all__ = [
    "ActivePersonaModel",
    "BoundsModel",
    "PersonaActivateRequest",
    "PersonaBackupModel",
    "PersonaLibraryItemModel",
    "PersonaLimitsModel",
    "PersonaOutcomeModel",
    "PersonaResponse",
    "PersonaRollbackRequest",
    "PersonaSaveAsRequest",
    "PersonaUpdateRequest",
]

#: 人物 id 的入参上限（与 `PERSONA_ID_PATTERN` 是**同一条**正则，见 §04.5.8 裁定 160）
_ID_FIELD = Field(min_length=1, max_length=64, pattern=PERSONA_ID_PATTERN)

#: 备份文件名的入参上限。**形状不在这里判**：白名单正则住在 store
#: （`_BACKUP_NAME_PATTERN`），请求体只挡住"明显不是文件名"的输入，
#: 免得同一套规则两处各写一份然后慢慢分叉。
_BACKUP_FIELD = Field(min_length=1, max_length=160)


class _Body(BaseModel):
    """请求体基类（禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    """响应基类（默认放行，字段全部显式声明）。"""

    model_config = ConfigDict(extra="forbid")


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════


class BoundsModel(_Response):
    """一个字段的上下限（``None`` = 这一侧没约束）。"""

    min: int | None
    max: int | None


class PersonaConfigModel(_Response):
    """激活人物的**全部字段**（表单回填用；键名与 `config/persona.yaml` 一一对应）。"""

    schema_version: str
    id: str
    name: str
    is_active: bool
    role_desc: str
    tone: str
    audience: str
    catchphrases: list[str]
    forbidden: list[str]
    style_hint: str
    target_chars_min: int
    target_chars_max: int
    max_duration_ms: int


class ActivePersonaModel(_Response):
    """当前激活人物。``stale=true`` ⇒ 下面这些是**上一份好的**，不是磁盘现状。"""

    persona_id: str
    name: str
    version: int
    sha256: str
    source: str
    path: str
    loaded_at: str
    last_error: str | None
    stale: bool
    config: PersonaConfigModel


class PersonaLibraryItemModel(_Response):
    """人物库里的一条（``active`` 标出"就是他"；``valid=false`` 时 ``error`` 给原因）。"""

    persona_id: str
    name: str
    tone: str
    audience: str
    valid: bool
    error: str
    sha256: str
    path: str
    active: bool


class PersonaBackupModel(_Response):
    """一份备份（回滚的候选）。``valid=false`` 的备份**照样列出**，只是不能被回滚。"""

    name: str
    path: str
    persona_id: str
    created_at: str
    sha256: str
    size: int
    valid: bool
    error: str


class PersonaLimitsModel(_Response):
    """输入框的上下限（从 `PersonaConfig` 现取 · 裁定 161）。"""

    editable: list[str]
    name: BoundsModel
    role_desc: BoundsModel
    tone: BoundsModel
    audience: BoundsModel
    catchphrases: BoundsModel
    forbidden: BoundsModel
    target_chars_min: BoundsModel
    target_chars_max: BoundsModel
    max_duration_ms: BoundsModel


class PersonaResponse(_Response):
    """一次拿全：激活人物 + 人物库 + 备份 + 上下限。"""

    generated_at: str
    active: ActivePersonaModel | None
    active_error: str | None
    library: list[PersonaLibraryItemModel]
    backups: list[PersonaBackupModel]
    limits: PersonaLimitsModel
    active_path: str
    library_dir: str
    backup_dir: str
    backup_page: int


# ══════════════════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════════════════


class PersonaUpdateRequest(_Body):
    """保存表单（``None`` = **这次不动它**，与"清空"不是一回事）。

    校验的**唯一权威**是 `PersonaConfig`（服务层 `model_validate`）：这里刻意不加
    ``min_length`` 之类的约束 —— 抄一份到请求体，就会出现"请求体拦下了、但错误形状
    与 store 给的不一样"，面板于是要写两套红字逻辑。
    """

    name: str | None = None
    role_desc: str | None = None
    tone: str | None = None
    audience: str | None = None
    catchphrases: list[str] | None = None
    forbidden: list[str] | None = None
    style_hint: str | None = None
    target_chars_min: int | None = None
    target_chars_max: int | None = None
    max_duration_ms: int | None = None
    reason: str | None = Field(default=None, max_length=MAX_REASON)

    def changes(self) -> dict[str, Any]:
        """只把**显式给了**的字段交出去（``exclude_none`` 的语义边界写在这里）。

        不这么做的话，"面板只改了口吻、却把整份表单发上来"会把没动过的字段也重写一遍
        —— 而重写用的是**提交那一刻的旧值**，两个标签页同时开着就会互相覆盖。
        """
        return self.model_dump(exclude={"reason"}, exclude_none=True)


class PersonaActivateRequest(_Body):
    """一键切换。"""

    persona_id: str = _ID_FIELD
    backup: bool = True
    reason: str | None = Field(default=None, max_length=MAX_REASON)


class PersonaSaveAsRequest(_Body):
    """把当前激活人物存进人物库。"""

    persona_id: str = _ID_FIELD
    overwrite: bool = False
    reason: str | None = Field(default=None, max_length=MAX_REASON)


class PersonaRollbackRequest(_Body):
    """回滚到某份备份（``name`` 来自 ``GET /api/v1/persona`` 的 ``backups[].name``）。"""

    name: str = _BACKUP_FIELD
    backup: bool = True
    reason: str | None = Field(default=None, max_length=MAX_REASON)


class PersonaOutcomeModel(_Response):
    """一次写动作的结果（``backup`` = 退路是哪一份；``changed=false`` ⇒ 本来就是这样）。"""

    action: str
    persona_id: str
    name: str
    version: int
    sha256: str
    changed: bool
    previous_persona_id: str | None
    backup: str | None
    note: str
