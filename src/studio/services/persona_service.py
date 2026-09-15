"""人物库服务（T4.13 · §02.4 / §04.1.6 / §04.5.8）—— 看 / 改 / 切 / 回滚。

面板要回答四个问题，一个模型对一块
----------------------------------
① "现在用的是谁、哪来的、坏没坏？" ⇒ :class:`PersonaActiveCard`（含 ``stale``）
② "库里还有哪些人、哪个不能用？"   ⇒ :class:`PersonaLibraryCard`
③ "改完存哪、改错了怎么退？"       ⇒ :meth:`PersonaService.update` / :meth:`PersonaService.rollback`
④ "换个人上"                      ⇒ :meth:`PersonaService.activate` / :meth:`PersonaService.save_as`

为什么状态变更**不走数据库**
----------------------------
``config/persona.yaml`` 是唯一真相（0006_seed 顶部写明），``personas`` 表只是它的投影。
面板直接写 DB 会让"表说 A、文件说 B"，而 5 个进程读的都是**文件** —— 于是面板上改完
看起来生效了，跑出来的稿子还是旧口吻。所以本服务只碰 :class:`PersonaStore`。

文件型状态没有"同一事务"可共享
------------------------------
"留痕与状态变更同事务"是 DB 侧的硬约定（§03.4.3）。这里的状态在文件里，能做的就是把
**顺序钉死**：先落盘（写前先备份）→ 再留痕。落盘失败 ⇒ 没有留痕（正确：什么都没变）；
留痕失败 ⇒ 状态已经变了，这时必须记 ``error`` 级日志，**不能静默**。

为什么"多进程无 IPC"要写进返回体
--------------------------------
面板显示的是**本进程**（api）已发现的状态。切换之后，worker 要等下一次
:meth:`PersonaStore.current` 才发现（worker 每个单元开始前都取一次）—— 这不是 bug，
是 T1.2 的既定设计。但它必须被**说出来**，否则"我切了人物，在跑的那条还是旧口吻"
会被当成故障查半天。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from studio.core.clock import format_iso, utc_now
from studio.core.config import PersonaConfig
from studio.core.errors import ConfigError, ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.persona_store import (
    BACKUP_PAGE,
    EDITABLE_FIELDS,
    PersonaBackup,
    PersonaSnapshot,
    PersonaStore,
)
from studio.db.repositories import AuditRepo
from studio.services.log_service import LogSink

__all__ = [
    "ACTION_ACTIVATE",
    "ACTION_ROLLBACK",
    "ACTION_SAVE_AS",
    "ACTION_UPDATE",
    "LOG_SOURCE",
    "PersonaActiveCard",
    "PersonaConsoleSnapshot",
    "PersonaLibraryCard",
    "PersonaOutcome",
    "PersonaService",
    "persona_limits",
]

logger = get_logger("studio.services.persona")

#: `audit_ops.action` 的四个取值（"谁在什么时候换了人物"必须查得到）
ACTION_UPDATE: Final[str] = "persona.update"
ACTION_ACTIVATE: Final[str] = "persona.activate"
ACTION_SAVE_AS: Final[str] = "persona.save_as"
ACTION_ROLLBACK: Final[str] = "persona.rollback"

#: `system_logs.source`（§04.5.2 的命名空间表里新增的一行）
LOG_SOURCE: Final[str] = "persona"


# ══════════════════════════════════════════════════════════════════════
# 读 · 数据形状
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PersonaActiveCard:
    """当前激活人物（**一份可能已经过期的可用快照** + 它是不是过期了）。"""

    persona_id: str
    name: str
    version: int
    sha256: str
    source: str
    path: str
    loaded_at: str
    #: 磁盘上那份现在读不出来了（:meth:`PersonaStore.last_error` 的 message）。
    #: 有值 ⇒ 下面这些字段是**上一份好的**，不是磁盘上的现状。
    last_error: str | None
    #: 表单要回填的全部字段（`PersonaConfig` 的原样 dump）
    config: dict[str, Any]

    @property
    def stale(self) -> bool:
        return self.last_error is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
            "source": self.source,
            "path": self.path,
            "loaded_at": self.loaded_at,
            "last_error": self.last_error,
            "stale": self.stale,
            "config": dict(self.config),
        }


@dataclass(frozen=True, slots=True)
class PersonaLibraryCard:
    """人物库里的一条（``active`` 标出"就是他"）。"""

    persona_id: str
    name: str
    tone: str
    audience: str
    valid: bool
    error: str
    sha256: str
    path: str
    active: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "name": self.name,
            "tone": self.tone,
            "audience": self.audience,
            "valid": self.valid,
            "error": self.error,
            "sha256": self.sha256,
            "path": self.path,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class PersonaConsoleSnapshot:
    """一次读全（面板首屏就这一个请求）。"""

    generated_at: str
    active: PersonaActiveCard | None
    #: 连"上一份好的"都没有（首次加载就失败）⇒ 面板显示这条，其余照常可看可切。
    active_error: str | None
    library: tuple[PersonaLibraryCard, ...]
    backups: tuple[PersonaBackup, ...]
    limits: dict[str, Any]
    active_path: str
    library_dir: str
    backup_dir: str
    backup_page: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "active": None if self.active is None else self.active.to_dict(),
            "active_error": self.active_error,
            "library": [item.to_dict() for item in self.library],
            "backups": [item.to_dict() for item in self.backups],
            "limits": dict(self.limits),
            "active_path": self.active_path,
            "library_dir": self.library_dir,
            "backup_dir": self.backup_dir,
            "backup_page": self.backup_page,
        }


@dataclass(frozen=True, slots=True)
class PersonaOutcome:
    """一次写动作的结果（改没改、退路是哪一份、下一步该看什么）。"""

    action: str
    persona_id: str
    name: str
    version: int
    sha256: str
    changed: bool
    previous_persona_id: str | None
    #: 本次写下的备份文件名（**退路**）；没写备份 ⇒ ``None``
    backup: str | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "persona_id": self.persona_id,
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
            "changed": self.changed,
            "previous_persona_id": self.previous_persona_id,
            "backup": self.backup,
            "note": self.note,
        }


# ══════════════════════════════════════════════════════════════════════
# 表单上下限（从模型现取，不抄第二份）
# ══════════════════════════════════════════════════════════════════════


def _bounds(field: Any) -> dict[str, int | None]:
    """取一个字段的上下限（``ge`` / ``le`` 与 ``min_length`` / ``max_length`` 都认）。

    参数刻意标 ``Any`` 而不是 ``pydantic.fields.FieldInfo``：那是 Pydantic 的内部
    类型，mypy 在这条路径上把它当成变量而不是类（``variables-vs-type-aliases``）。
    这里真正依赖的只是"有个 ``.metadata`` 序列"，结构式用法反而更稳。
    """
    minimum: int | None = None
    maximum: int | None = None
    for meta in field.metadata:
        if hasattr(meta, "ge"):
            minimum = int(meta.ge)
        elif hasattr(meta, "min_length"):
            minimum = int(meta.min_length)
        if hasattr(meta, "le"):
            maximum = int(meta.le)
        elif hasattr(meta, "max_length"):
            maximum = int(meta.max_length)
    return {"min": minimum, "max": maximum}


def persona_limits() -> dict[str, Any]:
    """输入框的上下限 —— **从 `PersonaConfig` 现取**。

    抄一份的后果很具体：某天把 ``target_chars_max`` 的上限从 8000 调到 12000，
    后端放行了、前端还在拦；或者反过来 —— 前端放行、后端 422。同一份约束在两处
    各写一遍，迟早有一处是旧的。
    """
    fields = PersonaConfig.model_fields
    return {
        "editable": list(EDITABLE_FIELDS),
        "name": _bounds(fields["name"]),
        "role_desc": _bounds(fields["role_desc"]),
        "tone": _bounds(fields["tone"]),
        "audience": _bounds(fields["audience"]),
        "catchphrases": _bounds(fields["catchphrases"]),
        "forbidden": _bounds(fields["forbidden"]),
        "target_chars_min": _bounds(fields["target_chars_min"]),
        "target_chars_max": _bounds(fields["target_chars_max"]),
        "max_duration_ms": _bounds(fields["max_duration_ms"]),
    }


# ══════════════════════════════════════════════════════════════════════
# 服务
# ══════════════════════════════════════════════════════════════════════


class PersonaService:
    """人物库的读写入口（**唯一**改人物的正门：WebUI / CLI / 面板共用同一份 store）。

    :param store: 热重载仓库（由 ``AppState`` 持有，见 §04.5.8 裁定 158）
    :param audit: `audit_ops` 仓储（``None`` ⇒ 不写留痕，单测可以直接省掉）
    :param log: 日志出口（``None`` ⇒ 不落日志）
    """

    def __init__(
        self,
        store: PersonaStore,
        *,
        audit: AuditRepo | None = None,
        log: LogSink | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._log = log

    # ── 读 ──────────────────────────────────────────────────────────────

    def read(self) -> PersonaConsoleSnapshot:
        """一次读全：激活人物 + 人物库 + 备份 + 表单上下限。

        **激活文件坏了也照常返回**（``active_error`` 带原因）：这个面板存在的意义
        就是"人物坏了的时候能把它修回来"，一个什么都不说的 500 恰好把工具关在门外。
        """
        active: PersonaActiveCard | None = None
        active_error: str | None = None
        try:
            active = self._active_card()
        except StudioError as exc:
            active_error = exc.message
            logger.warning("persona.active_unreadable", error=exc.message, code=str(exc.code))
        return PersonaConsoleSnapshot(
            generated_at=format_iso(utc_now()),
            active=active,
            active_error=active_error,
            library=self._library_cards(active),
            backups=self._store.backups(),
            limits=persona_limits(),
            active_path=str(self._store.active_path),
            library_dir=str(self._store.library_dir),
            backup_dir=str(self._store.backup_dir),
            backup_page=BACKUP_PAGE,
        )

    def _active_card(self) -> PersonaActiveCard:
        snapshot = self._store.current()
        last = self._store.last_error
        return PersonaActiveCard(
            persona_id=snapshot.persona_id,
            name=snapshot.config.name,
            version=snapshot.version,
            sha256=snapshot.sha256,
            source=snapshot.source,
            path=str(snapshot.path),
            loaded_at=snapshot.loaded_at,
            last_error=None if last is None else last.message,
            config=snapshot.config.model_dump(),
        )

    def _library_cards(self, active: PersonaActiveCard | None) -> tuple[PersonaLibraryCard, ...]:
        current_id = None if active is None else active.persona_id
        return tuple(
            PersonaLibraryCard(
                persona_id=entry.persona_id,
                name=entry.name,
                tone=entry.tone,
                audience=entry.audience,
                valid=entry.valid,
                error=entry.error,
                sha256=entry.sha256,
                path=str(entry.path),
                active=entry.persona_id == current_id,
            )
            for entry in self._store.library()
        )

    # ── 写 ──────────────────────────────────────────────────────────────

    def update(
        self,
        *,
        changes: Mapping[str, Any],
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> PersonaOutcome:
        """保存表单改动（**校验不过一个字节都不写** · :meth:`PersonaStore.update`）。"""
        before = self._current_or_none()
        snapshot = self._store.update(changes)
        return self._finish(
            action=ACTION_UPDATE,
            before=before,
            after=snapshot,
            reason=reason or "WebUI 保存人物表单",
            actor=actor,
            source=source,
            note=f"已保存「{snapshot.config.name}」（v{snapshot.version}）；"
            "只影响**后续稿件** —— 已入队/在跑的任务不中断、不重写",
        )

    def activate(
        self,
        *,
        persona_id: str,
        backup: bool = True,
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> PersonaOutcome:
        """一键切换（旧版自动备份到 ``data/backups/persona/``）。

        已经是他了 ⇒ **不改动、不备份、不留痕**（与池暂停的幂等同一条规矩：重复点
        同一个按钮不该把审计表刷成一串"又切了一次"）。
        """
        before = self._current_or_none()
        if before is not None and before.persona_id == persona_id:
            return self._idempotent(before, action=ACTION_ACTIVATE, target=persona_id)
        with _translated():
            snapshot = self._store.activate(persona_id, backup=backup)
        return self._finish(
            action=ACTION_ACTIVATE,
            before=before,
            after=snapshot,
            reason=reason or f"WebUI 切换人物 → {persona_id}",
            actor=actor,
            source=source,
            note=f"已切换到「{snapshot.config.name}」；旧版已备份。"
            "切换对**后续稿件**立即生效，已入队的任务不重写",
        )

    def save_as(
        self,
        *,
        persona_id: str,
        overwrite: bool = False,
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> PersonaOutcome:
        """把当前激活人物存进人物库（"改完存一份"，便于随时切回）。"""
        before = self._current_or_none()
        with _translated():
            target = self._store.save_as(persona_id, overwrite=overwrite)
            snapshot = self._store.current()
        outcome = PersonaOutcome(
            action=ACTION_SAVE_AS,
            persona_id=snapshot.persona_id,
            name=snapshot.config.name,
            version=snapshot.version,
            sha256=snapshot.sha256,
            changed=False,
            previous_persona_id=None if before is None else before.persona_id,
            backup=None,
            note=f"已存进人物库：{persona_id}（当前激活人物未变，仍是「{snapshot.config.name}」）",
        )
        self._record(
            action=ACTION_SAVE_AS,
            # 这一条改的是**人物库**，不是激活人物 ⇒ 留痕指向新存的那一份，
            # 否则"谁新建了 solo_commentary"在审计里会记成"改了 persona_default"。
            target_id=persona_id,
            outcome=outcome,
            before={"library_id": persona_id, "overwrite": overwrite},
            after={"library_path": str(target), "overwrite": overwrite},
            reason=reason or f"WebUI 另存为 {persona_id}",
            actor=actor,
            source=source,
        )
        return outcome

    def rollback(
        self,
        *,
        name: str,
        backup: bool = True,
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> PersonaOutcome:
        """回滚到某份备份（**先备份当前** ⇒ 回滚本身也可以再回滚）。"""
        before = self._current_or_none()
        with _translated():
            snapshot = self._store.rollback(name, backup=backup)
        return self._finish(
            action=ACTION_ROLLBACK,
            before=before,
            after=snapshot,
            reason=reason or f"WebUI 回滚人物 → {name}",
            actor=actor,
            source=source,
            note=f"已回滚到「{name}」（现在是 v{snapshot.version}）；当前这份也备份了，回滚错了可以再滚回来",
        )

    # ── 内部 ────────────────────────────────────────────────────────────

    def _current_or_none(self) -> PersonaSnapshot | None:
        """取当前快照；**首次加载就失败**（连上一份好的都没有）⇒ ``None``。"""
        try:
            return self._store.current()
        except StudioError as exc:
            logger.warning("persona.current_unreadable", error=exc.message, code=str(exc.code))
            return None

    def _finish(
        self,
        *,
        action: str,
        before: PersonaSnapshot | None,
        after: PersonaSnapshot,
        reason: str,
        actor: str,
        source: str,
        note: str,
    ) -> PersonaOutcome:
        outcome = PersonaOutcome(
            action=action,
            persona_id=after.persona_id,
            name=after.config.name,
            version=after.version,
            sha256=after.sha256,
            changed=before is None or before.sha256 != after.sha256,
            previous_persona_id=None if before is None else before.persona_id,
            backup=self._latest_backup(),
            note=note,
        )
        self._record(
            action=action,
            outcome=outcome,
            before=_fingerprint(before),
            after=_fingerprint(after),
            reason=reason,
            actor=actor,
            source=source,
        )
        return outcome

    def _idempotent(self, before: PersonaSnapshot, *, action: str, target: str) -> PersonaOutcome:
        """ "本来就是这样" ⇒ 不写盘、不备份、不留痕，但要如实告诉用户。"""
        return PersonaOutcome(
            action=action,
            persona_id=before.persona_id,
            name=before.config.name,
            version=before.version,
            sha256=before.sha256,
            changed=False,
            previous_persona_id=None,
            backup=None,
            note=f"当前就是「{before.config.name}」（{target}），没有做任何改动",
        )

    def _latest_backup(self) -> str | None:
        """本次写下的备份 = 备份目录里最新的那一份。

        依赖"同一进程内的写是串行的"（面板是唯一的写入口，`PersonaStore` 内部还有
        一把 `RLock`）。它不是状态，只是给用户的**退路指路**：指错了顶多多看一眼列表，
        不会把任何东西改坏。
        """
        recent = self._store.backups(limit=1)
        return recent[0].name if recent else None

    def _record(
        self,
        *,
        action: str,
        outcome: PersonaOutcome,
        target_id: str | None = None,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        reason: str,
        actor: str,
        source: str,
    ) -> None:
        """先落盘（调用方已做完）→ 再留痕 → 再写日志。两处失败都不许静默。"""
        if self._audit is not None:
            try:
                self._audit.record(
                    actor=actor,
                    action=action,
                    target_type="persona",
                    target_id=target_id or outcome.persona_id,
                    before=before,
                    after=after,
                    reason=reason,
                    source=source,
                )
            except Exception:  # 状态已经变了：这里只能记账，不能回滚文件
                logger.exception("persona.audit_failed", action=action, persona_id=outcome.persona_id)
        if self._log is not None:
            self._log(
                level="info",
                source=LOG_SOURCE,
                message=f"{outcome.note}（{reason}）",
                payload={
                    "action": action,
                    "persona_id": outcome.persona_id,
                    "version": outcome.version,
                    "sha256": outcome.sha256,
                    "changed": outcome.changed,
                    "previous_persona_id": outcome.previous_persona_id,
                    "backup": outcome.backup,
                },
            )
        logger.info(
            "persona.changed",
            action=action,
            persona_id=outcome.persona_id,
            version=outcome.version,
            changed=outcome.changed,
        )


#: store 用的"通用配置码" ⇒ REST 面能用的"人物码"。
#:
#: 为什么不直接把 store 的码改成 PERSONA_*：`CONFIG_MISSING` / `CONFIG_INVALID` 是
#: T1.2 就钉住的契约（单测逐条断言），而"库里没有这个人"对 REST 面来说是 404、
#: 对 CLI 来说是"去 `studio persona list` 看看" —— 同一件事两种读法，翻译放在
#: **面向调用方的那一层**（本服务）比改 store 的既有语义便宜。
_NOT_FOUND_CODES: Final[frozenset[ErrorCode]] = frozenset({ErrorCode.CONFIG_MISSING})
_INVALID_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {ErrorCode.CONFIG_INVALID, ErrorCode.CONFIG_PERSONA_INCOMPLETE}
)


@contextmanager
def _translated() -> Iterator[None]:
    """把 store 抛出的通用配置错误翻成人物码（**不吞异常**：翻不了的原样上抛）。

    没有这一层，`activate('ghost')` 会带着 `CONFIG_MISSING` 冒到应用级 handler ——
    而那张映射表里没有它 ⇒ **500**。用户看到的是"服务器挂了"，实际只是 id 打错了。
    """
    try:
        yield
    except ConfigError as exc:
        if exc.code in _NOT_FOUND_CODES:
            raise _rewrap(exc, ErrorCode.PERSONA_NOT_FOUND) from exc
        if exc.code in _INVALID_CODES:
            raise _rewrap(exc, ErrorCode.PERSONA_INVALID) from exc
        raise


def _rewrap(exc: ConfigError, code: ErrorCode) -> ConfigError:
    return ConfigError(
        exc.message,
        code=code,
        context=dict(exc.context),
        remediation=exc.remediation,
    )


def _fingerprint(snapshot: PersonaSnapshot | None) -> dict[str, Any]:
    """留痕里的 ``before`` / ``after``（**不放全文**：审计表不是媒资库）。"""
    if snapshot is None:
        return {"persona_id": None}
    return {
        "persona_id": snapshot.persona_id,
        "name": snapshot.config.name,
        "version": snapshot.version,
        "sha256": snapshot.sha256,
    }
