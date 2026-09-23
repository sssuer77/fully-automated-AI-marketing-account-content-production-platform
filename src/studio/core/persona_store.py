"""人物（persona）热重载仓库 —— 让"改人物"不需要重启任何进程。

为什么需要它
------------
``load_config()`` 返回的是**冻结快照**（进程启动时读一次）。但频道人设是
**随时会改**的东西：换口吻、换受众、换口癖、甚至整个换人物。若把 persona
钉死在启动快照上，改一次就得重启 API + 4 个 worker，还会打断在跑的任务。

本模块提供**运行期唯一的人物来源**：

1. **热重载**：``current()`` 每次调用都做一次廉价 ``stat``（mtime_ns + size）
   比对，内容变了才真正重新解析校验；进程内 ``version`` 自增。
2. **多人物库**：``config/personas/<id>.yaml`` 存放备选人物；
   ``activate(id)`` 一键切换（覆盖 ``config/persona.yaml`` 并自动备份旧版）。
3. **失败不中断**：运行期改坏了 YAML ⇒ **保留上一份可用快照**并记录
   ``last_error``，绝不把正在跑的任务打死（DoD 6：可降级、无静默失败）。
4. **可观测**：变更时触发订阅回调（T1.7 接 WS ``system.persona_changed``）。
5. **可编辑**：:meth:`PersonaStore.update` 收表单改动 —— **先校验、再备份、后落盘**，
   校验不过一个字节都不写（T4.13 人物库面板）。
6. **可回滚**：每次写入前的旧版都落在 ``data/backups/persona/``，
   :meth:`PersonaStore.rollback` 把它换回来（回滚本身也会先备份，故可反复横跳）。

多进程说明
----------
5 个进程各自持有一个 store 实例，靠**文件 mtime** 独立发现变更 ——
无需 IPC，也就没有额外的单点。变更在最坏情况下于下一次 ``current()`` 生效
（worker 每个 job 开始前都会取一次，故实际延迟 < 1 个 job）。
"""

from __future__ import annotations

import re
import shutil
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import ValidationError

from studio.core.clock import file_stamp, format_iso, now_iso
from studio.core.config import PersonaConfig, load_persona_file
from studio.core.errors import ConfigError, ErrorCode
from studio.core.files import file_sha256, stat_key
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths

__all__ = [
    "ACTIVE_PERSONA_SOURCE",
    "BACKUP_PAGE",
    "EDITABLE_FIELDS",
    "LIBRARY_PERSONA_SOURCE",
    "PERSONA_ID_PATTERN",
    "PersonaBackup",
    "PersonaChange",
    "PersonaLibraryEntry",
    "PersonaSnapshot",
    "PersonaStore",
    "get_persona_store",
    "render_persona_yaml",
    "reset_persona_store",
]

logger = get_logger("studio.persona")

PersonaSource = Literal["active", "library"]

#: ``PersonaSnapshot.source`` 的取值
ACTIVE_PERSONA_SOURCE: Final[str] = "active"
LIBRARY_PERSONA_SOURCE: Final[str] = "library"

#: 人物 id 允许的字符（同时作为库文件名）。
#: **公开**给 REST 面复用：请求体的 `pattern` 与这里的判定必须是同一条正则，
#: 否则会出现"API 收了、store 拒绝"（或反过来）这种两边都对不上的 422。
PERSONA_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_\-]{0,63}$"

#: 回滚候选的默认条数（备份目录是"退路"，不是瀑布流 —— 与 §03.4.6 规则 4 同精神）
BACKUP_PAGE: Final[int] = 20

#: 表单**可以改**的字段白名单。
#:
#: ``id`` 不在其中：它是文件名、备份名前缀与审计 ``target_id`` 三处共用的身份，
#: 改了它等于把历史留痕的指向改掉（想换 id ⇒ ``save_as`` 存一份新的再 ``activate``）。
#: ``schema_version`` / ``is_active`` 也不在：前者是契约版本，后者由"谁在激活位"决定。
EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "role_desc",
    "tone",
    "audience",
    "catchphrases",
    "forbidden",
    "speaker_names",
    "style_hint",
    "target_chars_min",
    "target_chars_max",
    "max_duration_ms",
)

#: 备份文件名：``<yyyymmdd-HHMMSS>[-<n>]_<persona_id>.yaml``。
#: 用**白名单正则**而不是"拼路径再判断"：``..`` / ``/`` / ``\`` 一个都进不来，
#: 回滚接口因此不可能被用来读备份目录之外的任何文件。
#: ``-<n>`` 是**同秒去重**（`file_stamp()` 只到秒）：没有它，一秒内的"改一次、
#: 回滚一次"会让第二次备份**覆盖掉**第一次 —— 而回滚正要读那一份。
_BACKUP_NAME_PATTERN: Final[str] = r"^[0-9]{8}-[0-9]{6}(?:-[0-9]{1,4})?_[a-z0-9][a-z0-9_\-]{0,63}\.yaml$"

#: WebUI / CLI 写盘时加在文件头的一段话（**人工写在原文件里的注释不会再出现**）。
_RENDERED_HEADER: Final[str] = (
    "# ── 人物定义（由 WebUI 或 `studio persona` 保存）───────────────────────\n"
    "# 本文件是机器序列化的结果：原先写在里面的注释不会保留（值一个不少）。\n"
    "# 改坏了不要紧 —— `data/backups/persona/` 存着每次写入前的旧版，\n"
    "# 面板上的「回滚」与 `studio persona use <id>` 都能退回去。\n"
)


@dataclass(frozen=True, slots=True)
class PersonaSnapshot:
    """一份**已校验**的人物定义 + 来源留痕（可审计：这个人物是哪来的）。"""

    config: PersonaConfig
    path: Path
    sha256: str
    version: int
    loaded_at: str
    source: PersonaSource = "active"

    @property
    def persona_id(self) -> str:
        return self.config.id

    def to_dict(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "name": self.config.name,
            "source": self.source,
            "path": str(self.path),
            "sha256": self.sha256,
            "version": self.version,
            "loaded_at": self.loaded_at,
            "catchphrases": list(self.config.catchphrases),
            "forbidden_count": len(self.config.forbidden),
        }


@dataclass(frozen=True, slots=True)
class PersonaLibraryEntry:
    """人物库中的一条（可能无效 —— 无效条目不影响激活人物）。"""

    persona_id: str
    path: Path
    sha256: str
    valid: bool
    name: str = ""
    tone: str = ""
    audience: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "path": str(self.path),
            "sha256": self.sha256,
            "valid": self.valid,
            "name": self.name,
            "tone": self.tone,
            "audience": self.audience,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PersonaBackup:
    """``data/backups/persona/`` 里的一份历史版本（回滚的候选）。

    ``valid`` 是**回滚前**必须看的一栏：备份本身也可能是坏的（比如上一次切换时
    激活文件就已经是坏的）。坏备份照样列出来 —— 但 :meth:`PersonaStore.rollback`
    会拒绝拿它覆盖现在这份好的。
    """

    name: str
    path: Path
    persona_id: str
    created_at: str
    sha256: str
    size: int
    valid: bool
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "persona_id": self.persona_id,
            "created_at": self.created_at,
            "sha256": self.sha256,
            "size": self.size,
            "valid": self.valid,
            "error": self.error,
        }


def render_persona_yaml(config: PersonaConfig) -> str:
    """把一份**已校验**的人物定义序列化成 ``config/persona.yaml`` 的文本。

    键顺序**显式固定**（不靠 ``model_dump()`` 的字段顺序）：这份文件人还会打开看，
    ``id`` / ``name`` 排在最上面比"按字母序"有用得多。``schema_version`` 必须在顶层
    （§02.2 的 :class:`~studio.core.config._FileConfig`），否则下次加载直接报未知版本。
    """
    data: dict[str, object] = {
        "schema_version": config.schema_version,
        "id": config.id,
        "name": config.name,
        "is_active": config.is_active,
        "role_desc": config.role_desc,
        "tone": config.tone,
        "audience": config.audience,
        "catchphrases": list(config.catchphrases),
        "forbidden": list(config.forbidden),
        "speaker_names": list(config.speaker_names),
        "style_hint": config.style_hint,
        "target_chars_min": config.target_chars_min,
        "target_chars_max": config.target_chars_max,
        "max_duration_ms": config.max_duration_ms,
    }
    body = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    return _RENDERED_HEADER + body


@dataclass(frozen=True, slots=True)
class PersonaChange:
    """一次人物变更（供 WS 广播 / 审计）。

    ``failed=True`` 是"**变更失败**"这一类：磁盘上那份读不出来了，``new`` 是
    **仍在生效的上一份快照**（此时 ``new is previous``）。它与"换了个人"走同一条
    广播 —— 对面板来说，两者都是"你现在看到的人物需要重新理解一下"。
    """

    new: PersonaSnapshot
    previous: PersonaSnapshot | None
    reason: str
    failed: bool = False
    error: str | None = None

    @property
    def version(self) -> int:
        return self.new.version

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "version": self.new.version,
            "persona_id": self.new.persona_id,
            "name": self.new.config.name,
            "sha256": self.new.sha256,
            "source": self.new.source,
            "failed": self.failed,
            "error": self.error,
            "previous_persona_id": self.previous.persona_id if self.previous else None,
            "previous_version": self.previous.version if self.previous else None,
        }


PersonaListener = Callable[[PersonaChange], None]


class PersonaStore:
    """运行期人物来源：热重载 + 人物库 + 一键切换。

    :param paths: 路径契约（``StudioPaths.from_env()``）
    :param backup_dir: 切换人物时旧版的备份目录；``None`` ⇒ ``data/backups/persona``
    :param listeners: 变更订阅者（T1.7 接 WS 广播）
    """

    def __init__(
        self,
        paths: StudioPaths,
        *,
        backup_dir: Path | None = None,
        listeners: tuple[PersonaListener, ...] = (),
    ) -> None:
        self._paths = paths
        self._active_path = paths.persona_file
        self._library_dir = paths.persona_library_dir
        self._backup_dir = backup_dir if backup_dir is not None else paths.backups_dir / "persona"
        self._lock = threading.RLock()
        self._snapshot: PersonaSnapshot | None = None
        self._stat_key: tuple[int, int] | None = None
        self._version = 0
        self._last_error: ConfigError | None = None
        #: 上一次**广播过的**失败指纹：文件一直坏着时，`current()` 每次都会重试并再次
        #: 失败 —— 没有这个去重，一条坏文件会变成"每次请求广播一条告警"。
        self._error_key: str | None = None
        self._listeners: list[PersonaListener] = list(listeners)

    # ── 只读属性 ────────────────────────────────────────────
    @property
    def active_path(self) -> Path:
        return self._active_path

    @property
    def library_dir(self) -> Path:
        return self._library_dir

    @property
    def backup_dir(self) -> Path:
        """切换人物时的自动备份目录（回滚用）。"""
        return self._backup_dir

    @property
    def version(self) -> int:
        """当前快照的进程内版本号（每次内容变化 +1）。"""
        with self._lock:
            return self._version

    @property
    def last_error(self) -> ConfigError | None:
        """最近一次热重载失败的原因（``None`` = 一切正常）。

        存在 ``last_error`` 时 :meth:`current` 仍返回**上一份可用快照**，
        但调用方（WebUI / doctor）应显著提示"人物文件当前是坏的"。
        """
        with self._lock:
            return self._last_error

    # ── 订阅 ────────────────────────────────────────────────
    def subscribe(self, listener: PersonaListener) -> None:
        """注册变更回调（回调抛异常只记日志，不影响主流程）。"""
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: PersonaListener) -> bool:
        """注销变更回调；返回是否真的摘掉了一个。

        为什么需要它：本 store 是**进程级单例**，而"订阅"的持有者是 `lifespan`
        （每个 app 一份）。没有退订，测试里反复 `TestClient(...)` 就会把同一个
        监听器叠成 N 份 —— 一条 `system.persona_changed` 广播 N 次。
        """
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                return False
            return True

    # ── 读取 ────────────────────────────────────────────────
    def current(self) -> PersonaSnapshot:
        """取当前人物（自动热重载；文件损坏时返回上一份可用快照）。

        :raises ConfigError: 首次加载即失败（没有任何可用快照可退回）
        """
        with self._lock:
            if self._snapshot is None:
                return self._load_locked(reason="initial")
            if stat_key(self._active_path) != self._stat_key:
                return self._load_locked(reason="hot_reload")
            return self._snapshot

    def reload(self, *, force: bool = False) -> PersonaSnapshot:
        """强制重新读取（**失败即抛异常**，供 CLI / 显式刷新路径使用）。"""
        with self._lock:
            return self._load_locked(reason="reload", raise_on_error=True, force=force)

    # ── 人物库 ──────────────────────────────────────────────
    def library(self) -> tuple[PersonaLibraryEntry, ...]:
        """列出人物库（按 id 排序）；无效条目一并返回并带 ``error``。"""
        if not self._library_dir.is_dir():
            return ()
        entries: list[PersonaLibraryEntry] = []
        for path in sorted(self._library_dir.glob("*.yaml")):
            if path.name.startswith("."):
                continue
            digest = file_sha256(path)
            try:
                config = load_persona_file(path)
            except ConfigError as exc:
                entries.append(
                    PersonaLibraryEntry(
                        persona_id=path.stem, path=path, sha256=digest, valid=False, error=exc.message
                    )
                )
                continue
            mismatch = "" if config.id == path.stem else f"文件内 id={config.id} 与文件名 {path.stem} 不一致"
            entries.append(
                PersonaLibraryEntry(
                    persona_id=path.stem,
                    path=path,
                    sha256=digest,
                    valid=not mismatch,
                    name=config.name,
                    tone=config.tone,
                    audience=config.audience,
                    error=mismatch,
                )
            )
        return tuple(entries)

    def library_entry(self, persona_id: str) -> PersonaLibraryEntry:
        """按 id 取库中条目；不存在 ⇒ 抛 :class:`ConfigError`。"""
        _validate_persona_id(persona_id)
        for entry in self.library():
            if entry.persona_id == persona_id:
                return entry
        available = ", ".join(entry.persona_id for entry in self.library()) or "（库为空）"
        raise ConfigError(
            f"人物库中不存在：{persona_id}",
            code=ErrorCode.CONFIG_MISSING,
            context={"persona_id": persona_id, "library_dir": str(self._library_dir)},
            remediation=f"可选人物：{available}",
        )

    # ── 写入 ────────────────────────────────────────────────
    def activate(self, persona_id: str, *, backup: bool = True) -> PersonaSnapshot:
        """一键切换人物：把库中人物复制为激活的 ``config/persona.yaml``。

        先校验后落盘（**不会把坏内容写进激活文件**）；旧版自动备份到
        ``data/backups/persona/``，因此"换错人"永远可以回滚。
        """
        entry = self.library_entry(persona_id)
        if not entry.valid:
            raise ConfigError(
                f"人物库条目无效，拒绝切换：{persona_id}",
                code=ErrorCode.CONFIG_INVALID,
                context={"persona_id": persona_id, "path": str(entry.path), "reason": entry.error},
                remediation="修正该文件后重跑 `studio persona validate`",
            )

        with self._lock:
            if backup:
                self._backup_active_locked()
            self._active_path.parent.mkdir(parents=True, exist_ok=True)
            content = entry.path.read_text(encoding="utf-8").replace("\r\n", "\n")
            self._active_path.write_text(content, encoding="utf-8", newline="\n")
            return self._load_locked(reason=f"activate:{persona_id}", raise_on_error=True, force=True)

    def save_as(self, persona_id: str, *, overwrite: bool = False) -> Path:
        """把**当前激活人物**存进人物库（"改完存一份"，便于随时切回）。"""
        _validate_persona_id(persona_id)
        with self._lock:
            snapshot = self.current()
            target = self._library_dir / f"{persona_id}.yaml"
            if target.exists() and not overwrite:
                raise ConfigError(
                    f"人物库已存在：{persona_id}",
                    code=ErrorCode.PERSONA_EXISTS,
                    context={"persona_id": persona_id, "path": str(target)},
                    remediation="换一个 id，或加 --overwrite 覆盖",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                self._active_path.read_text(encoding="utf-8").replace("\r\n", "\n"),
                encoding="utf-8",
                newline="\n",
            )
        logger.info("persona.save_as", persona_id=persona_id, source_sha=snapshot.sha256, target=str(target))
        return target

    # ── 编辑（表单保存）─────────────────────────────────────
    def preview(self, changes: Mapping[str, object]) -> PersonaConfig:
        """把改动套到当前人物上并**校验**，但不落盘（面板的"保存前先试"）。

        :raises ConfigError: ``PERSONA_INVALID``，``context['field_errors']`` 逐字段给原因
        """
        with self._lock:
            return self._compose_locked(self.current(), changes)

    def update(self, changes: Mapping[str, object], *, backup: bool = True) -> PersonaSnapshot:
        """保存表单改动：**先校验 → 再备份 → 后落盘**。

        顺序不能换。校验在写之前 ⇒ 一份坏表单永远不会出现在 ``config/persona.yaml``
        里（那会让 5 个进程同时降级）；备份在写之前 ⇒ "刚改坏了想退回去"永远有退路。
        """
        with self._lock:
            config = self._compose_locked(self.current(), changes)
            if backup:
                self._backup_active_locked()
            self._write_active_locked(config)
            return self._load_locked(reason="update", raise_on_error=True, force=True)

    def _compose_locked(self, snapshot: PersonaSnapshot, changes: Mapping[str, object]) -> PersonaConfig:
        """当前人物 + 改动 ⇒ 一份**已校验**的新定义（唯一走校验的入口）。"""
        payload = snapshot.config.model_dump()
        for key, value in changes.items():
            if key not in EDITABLE_FIELDS:
                raise ConfigError(
                    f"字段不可编辑：{key}",
                    code=ErrorCode.PERSONA_INVALID,
                    context={"field": key, "editable": list(EDITABLE_FIELDS)},
                    remediation="换 id 请用「另存为」存成新 id 再切换；schema_version / is_active 由系统维护",
                )
            payload[key] = value
        try:
            return PersonaConfig.model_validate(payload)
        except ValidationError as exc:
            raise ConfigError(
                f"人物表单校验不通过（{len(exc.errors())} 处问题）",
                code=ErrorCode.PERSONA_INVALID,
                context={"field_errors": _format_field_errors(exc)},
                remediation="按 field_errors 逐条修正后重新提交；**本次没有写入任何内容**",
            ) from exc

    def _write_active_locked(self, config: PersonaConfig) -> None:
        self._active_path.parent.mkdir(parents=True, exist_ok=True)
        self._active_path.write_text(render_persona_yaml(config), encoding="utf-8", newline="\n")

    # ── 备份与回滚 ──────────────────────────────────────────
    def backups(self, *, limit: int = BACKUP_PAGE) -> tuple[PersonaBackup, ...]:
        """列出备份（**新到旧**）；无效备份一并返回并带 ``error``。"""
        if not self._backup_dir.is_dir():
            return ()
        found: list[tuple[float, PersonaBackup]] = []
        for path in self._backup_dir.glob("*.yaml"):
            if path.name.startswith("."):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:  # 正在被别人删/换 ⇒ 这一拍跳过它，不影响整页
                continue
            found.append((mtime, _backup_entry(path, mtime)))
        found.sort(key=lambda item: item[0], reverse=True)
        return tuple(entry for _, entry in found[:limit])

    def backup_entry(self, name: str) -> PersonaBackup:
        """按文件名取一份备份（名字先过白名单正则，故不可能读到目录之外）。"""
        _validate_backup_name(name)
        path = self._backup_dir / name
        if not path.is_file():
            recent = ", ".join(item.name for item in self.backups(limit=5)) or "（还没有备份）"
            raise ConfigError(
                f"备份不存在：{name}",
                code=ErrorCode.PERSONA_NOT_FOUND,
                context={"name": name, "backup_dir": str(self._backup_dir)},
                remediation=f"最近的备份：{recent}",
            )
        return _backup_entry(path, path.stat().st_mtime)

    def rollback(self, name: str, *, backup: bool = True) -> PersonaSnapshot:
        """回滚到某份备份（**先备份当前**，所以回滚本身也能再回滚）。"""
        entry = self.backup_entry(name)
        if not entry.valid:
            raise ConfigError(
                f"这份备份本身是坏的，拒绝用它覆盖当前人物：{name}",
                code=ErrorCode.PERSONA_INVALID,
                context={"name": name, "path": str(entry.path), "reason": entry.error},
                remediation="挑一份 valid=true 的备份；坏文件已原样列出，不会被静默跳过",
            )
        with self._lock:
            # 先把要回滚的内容**读进内存**，再备份当前 —— 顺序反了的话，同秒撞名
            # 时刚写下的那份备份会把 `entry.path` 指向的文件换掉，回滚就成了空转。
            content = entry.path.read_text(encoding="utf-8").replace("\r\n", "\n")
            if backup:
                self._backup_active_locked()
            self._active_path.parent.mkdir(parents=True, exist_ok=True)
            self._active_path.write_text(content, encoding="utf-8", newline="\n")
            return self._load_locked(reason=f"rollback:{name}", raise_on_error=True, force=True)

    # ── 内部 ────────────────────────────────────────────────
    def _active_persona_id_locked(self) -> str:
        """备份文件名用的旧人物 id（冷进程没有内存快照时回退读盘）。"""
        if self._snapshot is not None:
            return self._snapshot.persona_id
        return _persona_id_of_file(self._active_path)

    def _backup_active_locked(self) -> Path | None:
        """备份当前激活文件（不存在则跳过）；同名则退到 ``-2`` / ``-3`` …。

        `file_stamp()` 只到秒，而"改一次 + 回滚一次"完全可能落在同一秒里。撞名时
        直接覆盖的后果不是"少一份历史"，而是**回滚读到刚被覆盖的那一份** ——
        回滚等于没回滚，还看不出来（版本 +1、sha 不变）。
        """
        if not self._active_path.exists():
            return None
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        stem = self._active_persona_id_locked()
        stamp = file_stamp()
        target = self._backup_dir / f"{stamp}_{stem}.yaml"
        suffix = 1
        while target.exists():
            suffix += 1
            target = self._backup_dir / f"{stamp}-{suffix}_{stem}.yaml"
        shutil.copy2(self._active_path, target)
        return target

    def _load_locked(
        self,
        *,
        reason: str,
        raise_on_error: bool = False,
        force: bool = False,
    ) -> PersonaSnapshot:
        """在锁内重新加载。

        ``raise_on_error=False``（热重载路径）⇒ 失败时保留旧快照并记 ``last_error``；
        ``raise_on_error=True``（CLI / 显式刷新）⇒ 直接抛给调用方。
        """
        # 局部名**不能**叫 `stat_key`：那会把上面导入的那个函数遮蔽掉，
        # 于是这一行变成 UnboundLocalError（T4.7 门禁抓到的一次真回归）。
        key = stat_key(self._active_path)
        if not force and key is not None and key == self._stat_key and self._snapshot is not None:
            return self._snapshot

        try:
            config = load_persona_file(self._active_path)
        except ConfigError as exc:
            self._last_error = exc
            self._notify_failure_locked(exc, key)
            logger.warning(
                "persona.reload_failed",
                reason=reason,
                path=str(self._active_path),
                code=str(exc.code),
                error=exc.message,
                kept_version=self._version if self._snapshot else None,
            )
            if raise_on_error or self._snapshot is None:
                raise
            return self._snapshot

        previous = self._snapshot
        self._version += 1
        self._last_error = None
        self._error_key = None
        self._stat_key = key
        self._snapshot = PersonaSnapshot(
            config=config,
            path=self._active_path,
            sha256=file_sha256(self._active_path),
            version=self._version,
            loaded_at=now_iso(),
            source="active",
        )
        change = PersonaChange(new=self._snapshot, previous=previous, reason=reason)
        logger.info(
            "persona.loaded",
            reason=reason,
            persona_id=config.id,
            version=self._version,
            sha256=self._snapshot.sha256[:12],
            changed=previous is None or previous.sha256 != self._snapshot.sha256,
        )
        self._notify_locked(change)
        return self._snapshot

    def _notify_failure_locked(self, exc: ConfigError, stat_key: tuple[int, int] | None) -> None:
        """文件坏了 ⇒ 广播一条 ``failed`` 变更，但**同一份坏文件只广播一次**。

        去重键里带 ``stat_key``：文件被改成"另一种坏法"（或又坏了一次）会重新广播
        —— 状态迁移才报，不是每次读都报（与 `DISK_LOW` 的去重同一条理由）。

        首次加载就失败（没有任何可用快照）时不广播：那种情况面板走 ``active_error``，
        没有"仍在生效的人物"可谈。
        """
        key = f"{exc.code}|{exc.message}|{stat_key}"
        if self._error_key == key:
            return
        self._error_key = key
        if self._snapshot is None:
            return
        self._notify_locked(
            PersonaChange(
                new=self._snapshot,
                previous=self._snapshot,
                reason="reload_failed",
                failed=True,
                error=exc.message,
            )
        )

    def _notify_locked(self, change: PersonaChange) -> None:
        for listener in self._listeners:
            try:
                listener(change)
            except Exception:
                logger.exception("persona.listener_failed", version=change.version)


def _persona_id_of_file(path: Path) -> str:
    """尽力读出人物 id（文件损坏时也要给出可读的备份名）。"""
    try:
        return load_persona_file(path).id
    except ConfigError:
        pass
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "id":
            candidate = value.strip().strip("\"'")
            return candidate if re.fullmatch(PERSONA_ID_PATTERN, candidate) else "unknown"
    return "unknown"


def _backup_entry(path: Path, mtime: float) -> PersonaBackup:
    """读一份备份的元信息（坏文件也要给得出 ``persona_id``，否则列表里认不出它是谁）。"""
    digest = file_sha256(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    created_at = format_iso(datetime.fromtimestamp(mtime, tz=UTC))
    try:
        config = load_persona_file(path)
    except ConfigError as exc:
        return PersonaBackup(
            name=path.name,
            path=path,
            persona_id=_persona_id_of_file(path),
            created_at=created_at,
            sha256=digest,
            size=size,
            valid=False,
            error=exc.message,
        )
    return PersonaBackup(
        name=path.name,
        path=path,
        persona_id=config.id,
        created_at=created_at,
        sha256=digest,
        size=size,
        valid=True,
    )


def _validate_backup_name(name: str) -> None:
    if not re.fullmatch(_BACKUP_NAME_PATTERN, name):
        raise ConfigError(
            f"备份文件名非法：{name!r}",
            code=ErrorCode.PERSONA_NOT_FOUND,
            context={"name": name, "pattern": _BACKUP_NAME_PATTERN},
            remediation="只接受 `<yyyymmdd-HHMMSS>_<persona_id>.yaml`（面板列出的就是这些名字）",
        )


def _format_field_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Pydantic 的校验错误 ⇒ 面板能标到输入框上的 ``[{field, error}]``。

    形状与 ``core/config.py`` 的 ``_format_errors`` **一致**：同一件事在两处
    （启动校验 / 表单校验）给两种形状，前端就得写两套红字逻辑。
    """
    return [
        {
            "field": ".".join(str(part) for part in item.get("loc", ())) or "<root>",
            "error": str(item.get("msg", "")),
        }
        for item in exc.errors(include_url=False)
    ]


def _validate_persona_id(persona_id: str) -> None:
    if not re.fullmatch(PERSONA_ID_PATTERN, persona_id):
        raise ConfigError(
            f"人物 id 非法：{persona_id!r}",
            code=ErrorCode.CONFIG_INVALID,
            context={"persona_id": persona_id, "pattern": PERSONA_ID_PATTERN},
            remediation="只允许小写字母、数字、下划线与连字符，且以字母或数字开头（≤64 字符）",
        )


# ── 进程级单例 ──────────────────────────────────────────────────────────

_STORE: PersonaStore | None = None
_STORE_LOCK = threading.Lock()


def get_persona_store(
    paths: StudioPaths | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> PersonaStore:
    """取进程级单例（5 个进程各持一份，靠 mtime 独立发现变更）。"""
    global _STORE  # noqa: PLW0603 — 进程级单例，与 structlog 同样的惯例
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = PersonaStore(paths or StudioPaths.from_env(dict(env) if env else None))
        return _STORE


def reset_persona_store() -> None:
    """丢弃单例（测试 / 切换 STUDIO_HOME 后使用）。"""
    global _STORE  # noqa: PLW0603
    with _STORE_LOCK:
        _STORE = None
