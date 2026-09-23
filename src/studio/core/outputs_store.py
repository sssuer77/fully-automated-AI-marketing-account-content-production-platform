"""合成配置（`config/outputs.yaml`）的编辑仓库（T4.7 · §04.2.8 / §04.5.9）。

文件是唯一真相
--------------
编码 profile / 水印 / 音频 / 字幕 / BGM 的真相在 `config/outputs.yaml`，消费方是
渲染编译器（T3.x）与发布前校验（T5.1）—— 它们读的都是**文件**。面板若写 DB，就会出现
"表说 A、文件说 B"，而真正出片的是文件。所以本仓库只碰文件，与 T4.13 的人物库、
T4.2 的「一键全自动」（裁定 139）是同一条取舍。

为什么值得有个 store，而不是每次现读现写
----------------------------------------
1. **热重载**：``current()`` 每次做一次廉价 ``stat`` 比对，内容变了才重新解析 ——
   人手动改了 YAML，面板下一次刷新就能看见（不用重启 API）。
2. **失败不降级成 500**：文件坏了 ⇒ 保留**上一份可用快照** + ``last_error``，
   面板照常打得开。`outputs.yaml` 被改坏时，用户最需要看到的恰恰是
   「配置读不到，去修这一份」（与 §04.5.7 裁定 150 同一条理由）。
3. **进程内版本号**：每次内容变化 +1，面板据此说"这是第几个版本"。

写盘是**保留注释**的（§04.5.9 裁定 166）
----------------------------------------
不整份重写，只逐行换冒号右边的标量（:mod:`studio.core.yaml_lines`）。这份文件的每一行
都带着理由（``# ★ 水印是必做项（D5）``），整份重写等于"点一次面板就永久毁掉可读性"。

多进程说明
----------
与 persona 一致：5 个进程各自持有实例，靠文件 mtime 独立发现变更，无需 IPC。
渲染 worker 在每个 job 开始前取一次配置 ⇒ 面板改完**下一个 job 就用新的**，
不需要重启任何进程。面板显示的是**本进程**（api）已发现的状态。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from studio.core.clock import now_iso
from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.errors import ConfigError, ErrorCode
from studio.core.files import file_sha256, stat_key
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.yaml_lines import set_scalar

__all__ = [
    "EDITABLE_SCALARS",
    "PROFILE_FIELDS",
    "SECTIONS",
    "STICKER_FIELDS",
    "SUBTITLE_FIELDS",
    "SUBTITLE_NESTED",
    "WATERMARK_FIELDS",
    "OutputsSnapshot",
    "OutputsStore",
    "field_key",
    "quality_field_of",
    "scalar_for_write",
]

logger = get_logger("studio.outputs")

#: 改动载荷允许出现的**段**（多一个就报错，不静默忽略）
SECTIONS: Final[tuple[str, ...]] = (
    "default_profile",
    "profiles",
    "watermark",
    "stickers",
    "subtitle",
)

#: 顶层可编辑标量
EDITABLE_SCALARS: Final[tuple[str, ...]] = ("default_profile",)

#: `profiles.<name>` 段可编辑字段。
#:
#: ``quality`` 是个**抽象字段**：写进文件时按 ``vcodec`` 落到 ``crf``（libx264）
#: 或 ``cq``（NVENC）—— 面板上只有一个"质量"数字框，用户不必知道这两个参数是
#: 两种编码器的方言（也正因如此，它不能直接在 YAML 里按同名找）。
PROFILE_FIELDS: Final[tuple[str, ...]] = ("width", "height", "fps", "quality")

#: `watermark` 段可编辑字段（D5 必做项的参数面）
WATERMARK_FIELDS: Final[tuple[str, ...]] = ("position", "margin_x", "margin_y", "width_ratio", "opacity")

#: `subtitle` 段可编辑字段（Q11 开启）。
#:
#: ``margin_bottom``（面板「距底」）与 ``safe_area_bottom``（面板「底部安全区」）是
#: T3.5 追加的（裁定 409）：字幕位置此前**只读**，而锁住它的唯一理由是"写了未必生效"
#: （两个数取 max，小的那个被抬上去）。现在生效值（``margin_v``）跟着响应一起下发，
#: 那个理由不成立了 —— 于是两个数都能改：往上挪调大「距底」，往下挪要把「底部安全区」
#: 一起调小（面板会当场说明白"实际 N px"）。
SUBTITLE_FIELDS: Final[tuple[str, ...]] = (
    "font_size",
    "outline",
    "margin_bottom",
    "safe_area_bottom",
    "max_chars_per_line",
)

#: 面板的**扁平**字段名 ⇒ 文件里的**嵌套**路径。
#:
#: 面板上叫 ``safe_area_bottom``（一个数一个框），而在 ``config/outputs.yaml`` 里它是
#: ``subtitle.safe_area.bottom``。这份对照表**只有一处**（预览与写盘都读它），于是
#: "面板能改的字段"与"文件里被改的那一行"不可能漂开。
SUBTITLE_NESTED: Final[dict[str, tuple[str, ...]]] = {"safe_area_bottom": ("safe_area", "bottom")}

#: 校验错误的 ``loc`` 是**文件路径**（``subtitle.safe_area.bottom``），而面板上的输入框
#: 叫**扁平字段名**（``subtitle.safe_area_bottom``）。两者不一致时，红字会落在"没有那个
#: 框"的地方 —— 用户只看到"保存失败"，找不到哪一格错。对照表只此一处。
_FIELD_ALIASES: Final[dict[str, str]] = {"subtitle.safe_area.bottom": "subtitle.safe_area_bottom"}


def field_key(path: Sequence[object]) -> str:
    """文件里的键路径 ⇒ 面板认得的字段名（对照表见 ``_FIELD_ALIASES``）。"""
    joined = ".".join(str(part) for part in path) or "<root>"
    return _FIELD_ALIASES.get(joined, joined)


#: `stickers.<name>` 段可编辑字段（T6.5 人物贴图）。
#:
#: ``path`` **在这里、而水印的 path 不在** —— 这不是疏忽。水印是"账号的标识"，一张图
#: 就够、换它要走一次刻意的人工动作；贴图是"这次要用哪张人物"，**本来就会来回换**，
#: 每换一次都去手改 YAML 等于把面板的意义削掉一半。它是字符串字段（不是 ``Path``），
#: 写盘时统一转正斜杠（见 ``_edits_locked``）。
STICKER_FIELDS: Final[tuple[str, ...]] = (
    "enabled",
    "path",
    # 换图那两件（T6.5 追加）：``speaker`` 说「这一层代表谁」，``speaking_path``
    # 说「讲话时换哪张图」。都进可编辑字段 —— 换人物换到别人身上时，第一件事就是
    # 改这两个。
    "speaker",
    "speaking_path",
    "position",
    "margin_x",
    "margin_y",
    "height_ratio",
    "opacity",
)


@dataclass(frozen=True, slots=True)
class OutputsSnapshot:
    """一份**已校验**的合成配置 + 来源留痕。"""

    config: OutputsConfig
    path: Path
    sha256: str
    version: int
    loaded_at: str

    @property
    def default_profile(self) -> str:
        return self.config.default_profile

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "version": self.version,
            "loaded_at": self.loaded_at,
            "default_profile": self.config.default_profile,
            "profiles": list(self.config.profiles),
        }


def quality_field_of(vcodec: str) -> str:
    """质量参数写到哪个键：libx264 用 ``crf``，NVENC 用 ``cq``。

    **公开**给服务层复用（面板要显示"这一档调的是 CRF 还是 CQ"）：这条判断
    是"面板上的一个数字框"与"文件里的两个键名"之间**唯一**的翻译，
    抄第二份就会出现"面板说 CQ、写进 crf"这种对不上的错。
    """
    return "crf" if vcodec == "libx264" else "cq"


def scalar_for_write(value: object) -> object:
    """模型上的值 ⇒ 写进 YAML 的字面量（**写盘口径的唯一一处**）。

    目前只有一条规则：**路径转正斜杠**。``config/*.yaml`` 通篇写的是
    ``templates/.../x.png``，而 ``str(Path)`` 在 Windows 上给的是反斜杠 —— 同一份配置
    在两种机器上写出两种字节，diff 里看着像"改了路径"，其实只是换了个分隔符。

    单独抽成函数而不是写在 ``_edits_locked`` 里：``test_yaml_lines`` 的"按原值写回去
    ⇒ 逐字节不变"那条不变量要按**同一个口径**取值，抄一份进测试就等于把这条规则变成
    两处维护 —— 而它一漂，面板保存一次就会把路径的分隔符改掉。
    """
    if isinstance(value, Path):
        return value.as_posix()
    return value


def _invalid(field: str, error: str, remediation: str) -> ConfigError:
    """字段级错误（形状与人物库 / 启动校验**一致**，前端只写一套红字逻辑）。"""
    return ConfigError(
        f"合成配置改动不合法：{field}",
        code=ErrorCode.OUTPUTS_INVALID,
        context={"field_errors": [{"field": field, "error": error}]},
        remediation=remediation,
    )


class OutputsStore:
    """运行期合成配置来源：热重载 + 表单保存（**保留注释**）。

    :param paths: 路径契约（``StudioPaths.from_env()``）
    :param path: 覆盖默认的 ``config/outputs.yaml``（测试用）
    """

    def __init__(self, paths: StudioPaths, *, path: Path | None = None) -> None:
        self._path = path if path is not None else paths.config_dir / "outputs.yaml"
        self._lock = threading.RLock()
        self._snapshot: OutputsSnapshot | None = None
        self._stat_key: tuple[int, int] | None = None
        self._version = 0
        self._last_error: ConfigError | None = None

    # ── 只读属性 ────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def version(self) -> int:
        """当前快照的进程内版本号（每次内容变化 +1）。"""
        with self._lock:
            return self._version

    @property
    def last_error(self) -> ConfigError | None:
        """最近一次热重载失败的原因（``None`` = 一切正常）。

        存在 ``last_error`` 时 :meth:`current` 仍返回**上一份可用快照**，
        调用方（WebUI / doctor）应显著提示"这份文件当前是坏的"。
        """
        with self._lock:
            return self._last_error

    # ── 读取 ────────────────────────────────────────────────

    def current(self) -> OutputsSnapshot:
        """取当前合成配置（自动热重载；文件损坏时返回上一份可用快照）。

        :raises ConfigError: 首次加载即失败（没有任何可用快照可退回）
        """
        with self._lock:
            if self._snapshot is None:
                return self._load_locked(reason="initial")
            if stat_key(self._path) != self._stat_key:
                return self._load_locked(reason="hot_reload")
            return self._snapshot

    def reload(self, *, force: bool = False) -> OutputsSnapshot:
        """强制重新读取（**失败即抛异常**，供 CLI / 显式刷新路径使用）。"""
        with self._lock:
            return self._load_locked(reason="reload", raise_on_error=True, force=force)

    # ── 编辑（表单保存）─────────────────────────────────────

    def preview(self, changes: Mapping[str, Any]) -> OutputsConfig:
        """把改动套到当前配置上并**校验**，但不落盘（"保存前先试"）。

        :raises ConfigError: ``OUTPUTS_INVALID``，``context['field_errors']`` 逐字段给原因
        """
        with self._lock:
            return self._compose_locked(self.current(), changes)

    def update(
        self,
        changes: Mapping[str, Any],
        *,
        expected_sha256: str | None = None,
    ) -> OutputsSnapshot:
        """保存表单改动：**先校验 → 后落盘 → 再回读**。

        顺序不能换。校验在写之前 ⇒ 一份坏表单永远不会出现在 ``config/outputs.yaml``
        里（那会让渲染池的每一个 job 都开始失败）；回读在写之后 ⇒ 写坏了要**在这里**
        炸，而不是等下一个 job 才发现（那时候人已经忘了自己点过什么）。

        ``expected_sha256`` 是**并发编辑**的判据（T4.7 六条之一）：面板提交时把它
        加载到的那一份指纹带回来，与盘上现状不符 ⇒ ``OUTPUTS_STALE``，一个字节都不写。
        """
        with self._lock:
            snapshot = self.current()
            self._guard_sha_locked(snapshot, expected_sha256)
            config = self._compose_locked(snapshot, changes)
            edits = self._edits_locked(config, changes)
            written, changed = self._write_locked(edits)
            logger.info(
                "outputs.update",
                changed=changed,
                fields=list(written),
                default_profile=config.default_profile,
            )
            if not changed:
                # 值与原文件逐字相同（用户只是点了一下保存）⇒ 不刷新快照，
                # 免得版本号凭空 +1 —— "第 3 版"与"第 2 版"内容一样是个假信号。
                return snapshot
            return self._load_locked(reason="update", raise_on_error=True, force=True)

    def _guard_sha_locked(self, snapshot: OutputsSnapshot, expected: str | None) -> None:
        if expected is None or expected == snapshot.sha256:
            return
        raise ConfigError(
            "配置文件在你编辑期间被改过（另一个人 / 编辑器 / 另一个标签页）",
            code=ErrorCode.OUTPUTS_STALE,
            context={
                "expected_sha256": expected,
                "actual_sha256": snapshot.sha256,
                "loaded_at": snapshot.loaded_at,
                "path": str(self._path),
            },
            remediation="刷新面板拿到最新一份，确认无误后重新提交（**本次改动没有被写入**）",
        )

    def _compose_locked(self, snapshot: OutputsSnapshot, changes: Mapping[str, Any]) -> OutputsConfig:
        """当前配置 + 改动 ⇒ 一份**已校验**的新配置（唯一走校验的入口）。"""
        unknown = [key for key in changes if key not in SECTIONS]
        if unknown:
            raise _invalid(
                unknown[0],
                f"不可编辑的段：{unknown[0]}",
                f"可编辑的是：{'、'.join(SECTIONS)}（编码参数之外的段不在一期面板范围内 · R17）",
            )
        payload = snapshot.config.model_dump(mode="python")
        if "default_profile" in changes:
            payload["default_profile"] = changes["default_profile"]
        for name, patch in (changes.get("profiles") or {}).items():
            if name not in payload["profiles"]:
                raise _invalid(
                    f"profiles.{name}",
                    f"没有这一档 profile：{name}",
                    f"现有的是：{'、'.join(payload['profiles'])}",
                )
            target = payload["profiles"][name]
            for field, value in patch.items():
                if field not in PROFILE_FIELDS:
                    raise _invalid(
                        f"profiles.{name}.{field}",
                        f"不可编辑的字段：{field}",
                        f"可编辑的是：{'、'.join(PROFILE_FIELDS)}",
                    )
                if field == "quality":
                    target[quality_field_of(str(target["vcodec"]))] = value
                else:
                    target[field] = value
        self._apply_section(payload, changes, "watermark", WATERMARK_FIELDS)
        for name, patch in (changes.get("stickers") or {}).items():
            # 槽位必须**已经在文件里**：贴图是命名块，加一个槽位等于往 YAML 里插一段
            # （行级替换做不到，见 `core/yaml_lines.py` 的"能改什么、不能改什么"）。
            # 这里如实报错并指出"该怎么加"，而不是静默忽略 —— 静默忽略会让人以为
            # "第 5 层已经建好了"，而文件里根本没有那一块。
            if name not in payload["stickers"]:
                raise _invalid(
                    f"stickers.{name}",
                    f"没有这一层贴图：{name}",
                    (
                        f"现有的是：{'、'.join(payload['stickers'])}；"
                        "要加一层就在 config/outputs.yaml 的 stickers: 段里复制一段槽位块"
                    ),
                )
            target = payload["stickers"][name]
            for field, value in patch.items():
                if field not in STICKER_FIELDS:
                    raise _invalid(
                        f"stickers.{name}.{field}",
                        f"不可编辑的字段：{field}",
                        f"可编辑的是：{'、'.join(STICKER_FIELDS)}",
                    )
                target[field] = value
        self._apply_section(payload, changes, "subtitle", SUBTITLE_FIELDS, SUBTITLE_NESTED)
        try:
            return OutputsConfig.model_validate(payload)
        except ValidationError as exc:
            raise ConfigError(
                f"合成配置校验不通过（{len(exc.errors())} 处问题）",
                code=ErrorCode.OUTPUTS_INVALID,
                context={"field_errors": _format_field_errors(exc)},
                remediation="按 field_errors 逐条修正后重新提交；**本次没有写入任何内容**",
            ) from exc

    @staticmethod
    def _apply_section(
        payload: dict[str, Any],
        changes: Mapping[str, Any],
        section: str,
        allowed: Sequence[str],
        nested: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        for field, value in (changes.get(section) or {}).items():
            if field not in allowed:
                raise _invalid(
                    f"{section}.{field}",
                    f"不可编辑的字段：{field}",
                    f"可编辑的是：{'、'.join(allowed)}",
                )
            path = (nested or {}).get(field)
            if path is None:
                payload[section][field] = value
                continue
            target = payload[section]
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value

    def _edits_locked(
        self, config: OutputsConfig, changes: Mapping[str, Any]
    ) -> list[tuple[tuple[str, ...], object]]:
        """改动载荷 ⇒ YAML 行级编辑清单。

        值一律取**已校验模型**上的那一份（不是原始入参）：pydantic 可能做过夹取与
        归一化，写盘必须写"最终算出来的那个值"，否则文件里会出现"面板显示 0.85、
        文件里是 0.8500000001"这种对不上的怪事。
        """
        edits: list[tuple[tuple[str, ...], object]] = []
        if "default_profile" in changes:
            edits.append((("default_profile",), config.default_profile))
        for name, patch in (changes.get("profiles") or {}).items():
            profile = config.profiles[name]
            for field in patch:
                if field == "quality":
                    key = quality_field_of(profile.vcodec)
                    edits.append((("profiles", name, key), getattr(profile, key)))
                else:
                    edits.append((("profiles", name, field), getattr(profile, field)))
        for field in changes.get("watermark") or {}:
            edits.append((("watermark", field), getattr(config.watermark, field)))
        for name, patch in (changes.get("stickers") or {}).items():
            sticker = config.stickers[name]
            for field in patch:
                value = scalar_for_write(getattr(sticker, field))
                edits.append((("stickers", name, field), value))
        for field in changes.get("subtitle") or {}:
            # 扁平名 ⇒ 文件路径（``safe_area_bottom`` ⇒ ``subtitle.safe_area.bottom``），
            # 值从**已校验模型**上按同一条路径取出来（夹取/归一化之后的那一份）。
            path = SUBTITLE_NESTED.get(field, (field,))
            node: object = config.subtitle
            for part in path:
                node = getattr(node, part)
            edits.append((("subtitle", *path), node))
        return edits

    def _write_locked(self, edits: Sequence[tuple[tuple[str, ...], object]]) -> tuple[tuple[str, ...], bool]:
        """逐行替换并落盘；返回 ``(改到的字段路径, 是否真的变了)``。

        **要么全改、要么一个字节都不写**：先在内存里把每一行都换好，全部成功才
        落盘。中途有一条找不到 ⇒ 抛异常，而盘上那份**原封未动**。
        """
        raw = self._path.read_text(encoding="utf-8")
        lines = raw.splitlines(keepends=True)
        written: list[str] = []
        for path, value in edits:
            if not set_scalar(lines, path, value):
                raise ConfigError(
                    f"config/outputs.yaml 里找不到这一行：{'.'.join(path)}",
                    code=ErrorCode.OUTPUTS_INVALID,
                    context={
                        "field_errors": [
                            {
                                "field": field_key(path),
                                "error": "文件里没有这个键（面板只改已存在的字段）",
                            }
                        ],
                        "path": str(self._path),
                    },
                    remediation="对照 config/outputs.yaml 的注释补齐这一行，或刷新面板重试",
                )
            written.append(".".join(path))
        updated = "".join(lines)
        if updated == raw:
            return tuple(written), False
        self._path.write_text(updated, encoding="utf-8", newline="\n")
        load_outputs_config(self._path)  # 回读校验：写坏必须当场炸
        return tuple(written), True

    def _load_locked(
        self,
        *,
        reason: str,
        raise_on_error: bool = False,
        force: bool = False,
    ) -> OutputsSnapshot:
        """在锁内重新加载。

        ``raise_on_error=False``（热重载路径）⇒ 失败时保留旧快照并记 ``last_error``；
        ``raise_on_error=True``（写后刷新 / 显式刷新）⇒ 直接抛给调用方。
        """
        key = stat_key(self._path)
        if not force and key is not None and key == self._stat_key and self._snapshot is not None:
            return self._snapshot

        try:
            config = load_outputs_config(self._path)
        except ConfigError as exc:
            self._last_error = exc
            logger.warning(
                "outputs.reload_failed",
                reason=reason,
                path=str(self._path),
                code=str(exc.code),
                error=exc.message,
                kept_version=self._version if self._snapshot else None,
            )
            if raise_on_error or self._snapshot is None:
                raise
            return self._snapshot

        self._version += 1
        self._last_error = None
        self._stat_key = key
        self._snapshot = OutputsSnapshot(
            config=config,
            path=self._path,
            sha256=file_sha256(self._path),
            version=self._version,
            loaded_at=now_iso(),
        )
        logger.info(
            "outputs.loaded",
            reason=reason,
            version=self._version,
            sha256=self._snapshot.sha256[:12],
            profiles=len(config.profiles),
            default_profile=config.default_profile,
        )
        return self._snapshot


def _format_field_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Pydantic 的校验错误 ⇒ 面板能标到输入框上的 ``[{field, error}]``。

    形状与 ``core/config.py`` 的 ``_format_errors``、``core/persona_store.py`` 的
    同名函数**一致**：同一件事给两种形状，前端就得写两套红字逻辑。
    跨字段校验（``default_profile`` 必须存在于 ``profiles``）没有 ``loc``，
    落到 ``"<root>"`` —— 面板把它显示在表单顶部，而不是假装它属于某个输入框。
    """
    return [
        {
            "field": field_key(item.get("loc", ())),
            "error": str(item.get("msg", "")),
        }
        for item in exc.errors(include_url=False)
    ]
