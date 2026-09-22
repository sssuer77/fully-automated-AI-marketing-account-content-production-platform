"""提示词面板的服务层（T6.2 · 运行期覆盖）。

这一层解决的问题
----------------
提示词是**入库的**（``prompts/`` + ``manifest.yaml`` 的 sha256 逐字校验它），
而"这条文案读起来不对"这件事只有人看着产出才说得出来。所以改提示词必须能
**当场改、当场生效**，但不能改仓库文件 —— 一改，git 里那份就不等于实际跑的
那份，``prompts verify`` 也就没意义了。

做法是**覆盖层**：面板写的每一份落 ``data/prompts/<相对路径>``，
:meth:`~studio.agents.prompts.PromptLibrary.read` 先看覆盖目录。于是：

1. 仓库文件一个字节都不动（``verify`` 永远只报入库文件的漂移）；
2. ``prompt_version`` 跟着覆盖走（``digest`` 走 ``read``）⇒ 改一个字，
   之后落的 ``scripts.prompt_version`` 就变 —— P5「同输入同产物」不需要谁
   记得去 bump 一个号；
3. 「还原」= 删掉覆盖文件，退回仓库那一份。没有"改回原样"的第二种语义。

三条硬规矩（与 ``settings_service`` 同源）
------------------------------------------
1. **先校验、后落盘**：覆盖里的 ``{{变量}}`` 必须都在
   :meth:`~studio.agents.prompts.PromptLibrary.allowed_variables` 里 ——
   那种错不会在保存时报，只会在下一次生成时炸，而那时人早忘了自己改过什么。
2. **没变就不留痕**：提交上来的文本与生效那一份逐字相同 ⇒ ``changed=[]``，
   不写盘也不写审计。
3. **审计失败不回滚**：文件已经写完了，这时回滚等于"把用户刚改好的又删掉"。
   只能记账（与 ``persona_service`` / ``settings_service`` 同一条）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from studio.agents.prompts import (
    PromptEntry,
    PromptLibrary,
    render_template,
    template_variables,
)
from studio.core.errors import ErrorCode, LlmError, StudioError
from studio.core.logging import get_logger
from studio.db.repositories import AuditRepo

__all__ = [
    "LOG_SOURCE",
    "MAX_PROMPT_CHARS",
    "ROLES",
    "PromptCatalogEntry",
    "PromptFileEntry",
    "PromptService",
    "PromptWriteOutcome",
]

logger = get_logger("studio.prompts")

#: 日志的 ``source`` 字段（日志面板按它筛）
LOG_SOURCE: Final[str] = "prompts"

#: 一个条目最多两段（``system`` / ``user``），顺序固定
ROLES: Final[tuple[str, ...]] = ("system", "user")

#: 单份提示词的长度上限（字符）。给得宽松（最长的 writer 也才两三千字），
#: 但**必须有**：这是直接进 LLM 的一段文本，任意长的输入既烧预算又难排查。
MAX_PROMPT_CHARS: Final[int] = 20000


def _normalize(text: str) -> str:
    """统一换行 + 掐掉首尾空白 + 保证以换行收尾（写盘与比较都走它）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


@dataclass(frozen=True, slots=True)
class PromptFileEntry:
    """一个条目里的一段（``system`` 或 ``user``）。"""

    role: str
    path: str
    text: str
    overridden: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "text": self.text,
            "overridden": self.overridden,
        }


@dataclass(frozen=True, slots=True)
class PromptCatalogEntry:
    """面板上的一行：生效正文 + 版本 + 可用变量。"""

    name: str
    description: str
    version: str
    prompt_version: str
    overridden: bool
    files: tuple[PromptFileEntry, ...]
    #: 这个条目渲染时**拿得到**的变量（覆盖里只许引用它们）
    variables: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "prompt_version": self.prompt_version,
            "overridden": self.overridden,
            "files": [item.to_dict() for item in self.files],
            "variables": list(self.variables),
        }


@dataclass(frozen=True, slots=True)
class PromptWriteOutcome:
    """一次保存 / 还原的结果。"""

    name: str
    changed: tuple[str, ...]
    restored: bool
    entry: PromptCatalogEntry

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "changed": list(self.changed),
            "restored": self.restored,
            "entry": self.entry.to_dict(),
        }


class PromptService:
    """提示词面板的读写入口。

    :param library: 带覆盖目录的提示词库（``override_root`` 为空 ⇒ 保存会报错，
        这是**故意的**：没有覆盖目录的入口不该悄悄改仓库文件）
    :param audit: ``audit_ops`` 仓储（``None`` ⇒ 不写留痕，单测可以直接省掉）
    :param log: 日志出口（``None`` ⇒ 不落日志）
    """

    def __init__(
        self,
        library: PromptLibrary,
        *,
        audit: AuditRepo | None = None,
        log: Any = None,
    ) -> None:
        self._library = library
        self._audit = audit
        self._log = log

    # ── 读 ──────────────────────────────────────────────────────────────

    @property
    def override_dir(self) -> str | None:
        root = self._library.override_root
        return None if root is None else root.as_posix()

    def names(self) -> tuple[str, ...]:
        return self._library.names()

    def catalog(self) -> list[PromptCatalogEntry]:
        """全部条目（按名字排序）—— 面板的下拉框就是它。"""
        return [self.view(name) for name in self._library.names()]

    def view(self, name: str) -> PromptCatalogEntry:
        """一个条目的**生效**状态（覆盖优先）。"""
        entry = self._entry(name)
        files: list[PromptFileEntry] = []
        for role in ROLES:
            relative = getattr(entry, role)
            if not relative:
                continue
            files.append(
                PromptFileEntry(
                    role=role,
                    path=relative,
                    text=self._library.read(relative),
                    overridden=self._library.is_overridden(relative),
                )
            )
        return PromptCatalogEntry(
            name=name,
            description=entry.description,
            version=entry.version,
            prompt_version=self._library.prompt_version(name),
            overridden=any(item.overridden for item in files),
            files=tuple(files),
            variables=tuple(sorted(self._library.allowed_variables(name))),
        )

    # ── 写 ──────────────────────────────────────────────────────────────

    def save(
        self,
        name: str,
        *,
        system: str | None = None,
        user: str | None = None,
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> PromptWriteOutcome:
        """保存覆盖（**先校验、后落盘**；校验不过一个字节都不写）。

        只处理**显式给过**的那一段：``system=None`` 表示"这一段不动"，
        不是"把这一段清空" —— 清空一段提示词的后果是那一条纪律整个消失，
        而它在面板上长得和"没改"几乎一样。
        """
        entry = self._entry(name)
        submitted = {"system": system, "user": user}
        if system is None and user is None:
            raise StudioError(
                "至少要给一段提示词（system 或 user）",
                code=ErrorCode.VALIDATION_FAILED,
                context={"prompt": name},
                remediation="只改一段时，另一段留空即可（留空 = 不动）",
            )

        before_version = self._library.prompt_version(name)
        before_roles = self._overridden_roles(entry)
        changed: list[str] = []
        for role, raw in submitted.items():
            if raw is None:
                continue
            relative = getattr(entry, role)
            if not relative:
                raise StudioError(
                    f"提示词 {name} 没有 {role} 段",
                    code=ErrorCode.VALIDATION_FAILED,
                    context={"prompt": name, "role": role, "files": list(entry.files())},
                    remediation="只有这个条目登记过的文件才改得动（见 prompts/manifest.yaml）",
                )
            cleaned = self._clean(name=name, role=role, text=raw)
            if _normalize(cleaned) == _normalize(self._library.read(relative)):
                continue
            if _normalize(cleaned) == _normalize(self._library.read_repo(relative)):
                # 改回了仓库那一份 ⇒ 当作"还原"：留一份与仓库逐字相同的覆盖，
                # 只会让面板上的「已覆盖」徽标永远亮着，而它明明等于原样。
                if self._library.clear_override(relative):
                    changed.append(role)
                continue
            self._library.write_override(relative, cleaned)
            changed.append(role)

        outcome = PromptWriteOutcome(name=name, changed=tuple(changed), restored=False, entry=self.view(name))
        if changed:
            self._record(
                action="prompt.updated",
                name=name,
                before_version=before_version,
                before_roles=before_roles,
                after=outcome.entry,
                reason=reason or "、".join(changed),
                actor=actor,
                source=source,
            )
        return outcome

    def restore(
        self,
        name: str,
        *,
        role: str = "system",
        reason: str | None = None,
        actor: str = "user",
        source: str = "webui",
    ) -> PromptWriteOutcome:
        """还原一段（删掉覆盖文件 ⇒ 退回仓库那一份）。**幂等**：没覆盖也回 200。"""
        entry = self._entry(name)
        if role not in ROLES:
            raise StudioError(
                f"不认识的角色：{role}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"prompt": name, "role": role, "known": list(ROLES)},
                remediation="只支持 system / user 两段",
            )
        relative = getattr(entry, role)
        if not relative:
            raise StudioError(
                f"提示词 {name} 没有 {role} 段",
                code=ErrorCode.VALIDATION_FAILED,
                context={"prompt": name, "role": role, "files": list(entry.files())},
                remediation="只有这个条目登记过的文件才还原得动",
            )
        before_version = self._library.prompt_version(name)
        before_roles = self._overridden_roles(entry)
        cleared = self._library.clear_override(relative)
        outcome = PromptWriteOutcome(
            name=name,
            changed=(role,) if cleared else (),
            restored=True,
            entry=self.view(name),
        )
        if cleared:
            self._record(
                action="prompt.restored",
                name=name,
                before_version=before_version,
                before_roles=before_roles,
                after=outcome.entry,
                reason=reason or role,
                actor=actor,
                source=source,
            )
        return outcome

    # ── 内部 ────────────────────────────────────────────────────────────

    def _entry(self, name: str) -> PromptEntry:
        """取登记项；未注册的名字在这里报 422（不是 500）。"""
        try:
            return self._library.entry(name)
        except LlmError as exc:
            raise StudioError(
                f"提示词未注册：{name}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"prompt": name, "known": list(self._library.names())},
                remediation="提示词清单见 prompts/manifest.yaml；刷新面板再试",
            ) from exc

    def _overridden_roles(self, entry: PromptEntry) -> list[str]:
        return [
            role
            for role in ROLES
            if getattr(entry, role) and self._library.is_overridden(getattr(entry, role))
        ]

    def _clean(self, *, name: str, role: str, text: str) -> str:
        """校验并归一（不通过 ⇒ 422，**一个字节都不落盘**）。"""
        cleaned = _normalize(text)
        if not cleaned.strip():
            raise StudioError(
                f"{name} 的 {role} 段不能是空的",
                code=ErrorCode.VALIDATION_FAILED,
                context={"prompt": name, "role": role},
                remediation="要删掉一段纪律，请直接改文本；整段清空会把那条纪律一起删掉",
            )
        if len(cleaned) > MAX_PROMPT_CHARS:
            raise StudioError(
                f"{name} 的 {role} 段太长了（{len(cleaned)} 字 > {MAX_PROMPT_CHARS}）",
                code=ErrorCode.VALIDATION_FAILED,
                context={"prompt": name, "role": role, "length": len(cleaned)},
                remediation="提示词是每次调用都要发的，太长既贵又容易把重点淹掉",
            )
        allowed = self._library.allowed_variables(name)
        unknown = sorted(template_variables(cleaned) - allowed)
        if unknown:
            raise StudioError(
                f"{name} 的 {role} 段引用了没人会填的变量：{unknown}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"prompt": name, "role": role, "unknown": unknown, "allowed": sorted(allowed)},
                remediation="只能用面板上列出的变量（它们是 Python 侧真的会传的那些）",
            )
        try:
            render_template(cleaned, dict.fromkeys(allowed, ""), name=name)
        except LlmError as exc:
            raise StudioError(
                f"{name} 的 {role} 段没通过模板校验：{exc.message}",
                code=ErrorCode.VALIDATION_FAILED,
                context={"prompt": name, "role": role, **dict(exc.context)},
                remediation="模板只支持 {{变量}} 替换；控制流要写在 Python 侧",
            ) from exc
        return cleaned

    def _record(
        self,
        *,
        action: str,
        name: str,
        before_version: str,
        before_roles: Iterable[str],
        after: PromptCatalogEntry,
        reason: str,
        actor: str,
        source: str,
    ) -> None:
        """先落盘（调用方已做完）→ 再留痕 → 再写日志。两处失败都不许静默。"""
        after_roles = sorted(item.role for item in after.files if item.overridden)
        if self._audit is not None:
            try:
                self._audit.record(
                    actor=actor,
                    action=action,
                    target_type="prompt",
                    target_id=name,
                    before={"prompt_version": before_version, "overridden": sorted(before_roles)},
                    after={"prompt_version": after.prompt_version, "overridden": after_roles},
                    reason=reason,
                    source=source,
                )
            except Exception:  # 覆盖已经落盘：这里只能记账，不能回滚文件
                logger.exception("prompts.audit_failed", action=action, prompt=name)
        if self._log is not None:
            verb = "已还原" if action == "prompt.restored" else "已保存"
            self._log(
                level="info",
                source=LOG_SOURCE,
                message=f"提示词 {name} {verb}（{reason}）⇒ 版本 {after.prompt_version}",
                payload={
                    "action": action,
                    "prompt": name,
                    "roles": after_roles,
                    "prompt_version": after.prompt_version,
                },
            )
