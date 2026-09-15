"""保留策略与删除守卫（T4.12 · §03.7.5）。

为什么 GC 的第一等公民是白名单而不是 TTL
----------------------------------------
TTL 写错，后果是"多留了一会儿"；白名单写错，后果是"成片没了"。两者不对称，
所以本模块的默认动作是**拒绝**：GC 只删**显式列进候选清单**的东西，而每一个
候选在落地前都要过 :func:`guard_path` 的两道闸 —— 不越界 + 不碰白名单。

策略值不在本模块定义
--------------------
所有天数都来自 ``config/app.yaml → retention``（§01.2.5 已冻结）。
:meth:`RetentionPolicy.from_config` 只把它搬成一份**不可变快照**：GC 跑到一半
有人改了 YAML，不该让同一次回收里前半段按 30 天、后半段按 7 天算。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Final

from studio.core.config import load_app_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.paths import StudioPaths

__all__ = [
    "PROTECTED_WORK_NAMES",
    "RetentionPolicy",
    "guard_path",
    "protected_roots",
    "resolve_policy",
]

#: ``data/work/<task>/`` 下**永不清理**的名字（§03.7.5 末几行的"交付物"）。
#: ``graphs`` 是目录，其余是单文件 —— 判据落在"任务目录下的第二段名字"上，
#: 于是 ``graphs/**`` 与 ``script.json`` 走同一条规则，不必写两遍。
PROTECTED_WORK_NAMES: Final[frozenset[str]] = frozenset(
    {"graphs", "ir.json", "manifest.json", "script.json", "timeline.json"}
)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """§03.7.5 的保留期快照（小时 / 天 / GB）。"""

    sentence_audio_hours: int = 24
    voice_master_hours: int = 24
    scene_artifact_hours: int = 168
    system_logs_days: int = 30
    debug_logs_days: int = 7
    llm_calls_days: int = 90
    task_events_days: int = 90
    hot_archive_days: int = 90
    tts_cache_max_gb: float = 5.0

    @classmethod
    def from_config(cls, retention: object | None) -> RetentionPolicy:
        """从 ``AppConfig.retention`` 取快照；缺字段 ⇒ 用本类默认值。

        这里**不做校验**：校验是配置加载器（``config._validate``）的职责，
        GC 只负责"读到什么就按什么办"。传 ``None`` ⇒ 全套默认值，于是
        ``studio gc run --dry-run`` 在一台还没配好 ``config/`` 的机器上也能出报告。
        """
        if retention is None:
            return cls()
        fallback = cls()
        return cls(
            sentence_audio_hours=_number(retention, "sentence_audio_hours", fallback.sentence_audio_hours),
            voice_master_hours=_number(retention, "voice_master_hours", fallback.voice_master_hours),
            scene_artifact_hours=_number(retention, "scene_artifact_hours", fallback.scene_artifact_hours),
            system_logs_days=_number(retention, "system_logs_days", fallback.system_logs_days),
            debug_logs_days=_number(retention, "debug_logs_days", fallback.debug_logs_days),
            llm_calls_days=_number(retention, "llm_calls_days", fallback.llm_calls_days),
            task_events_days=_number(retention, "task_events_days", fallback.task_events_days),
            hot_archive_days=_number(retention, "hot_archive_days", fallback.hot_archive_days),
            tts_cache_max_gb=_number(retention, "tts_cache_max_gb", fallback.tts_cache_max_gb),
        )

    @property
    def tts_cache_limit_bytes(self) -> int:
        """TTS 缓存上限（字节）。"""
        return int(self.tts_cache_max_gb * 1024**3)

    def to_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


def _number(source: object, name: str, fallback: Any) -> Any:
    """取一个数值字段；不是数（含 bool）就退回默认值。

    bool 被显式排除：``True`` 在 Python 里是 ``int`` 的子类，于是 ``debug_logs_days: true``
    会静默变成"保留 1 天" —— 一个从 YAML 里看不出来的**过度清理**。
    """
    raw = getattr(source, name, None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return fallback
    return type(fallback)(raw)


def resolve_policy(paths: StudioPaths) -> tuple[RetentionPolicy, str | None]:
    """读 ``config/app.yaml → retention``；读不出来 ⇒ 默认值 + 一句降级说明。

    为什么要容错：GC 是**无人值守**跑的（计划任务 / 调度器），"配置文件写坏了"的
    后果不该是"垃圾从此没人清"。默认值与 §03.7.5 的表格逐条一致，用它跑一轮
    总好过什么都不做 —— 但**必须把降级说出来**（P4：宁要真话，不要好看的假绿灯），
    所以降级说明是返回值的一部分，由调用方负责展示。

    **只碰这一份文件**：走 ``load_config`` 会把 9 份 YAML 全读一遍并全量校验 ⇒
    ``llm.yaml`` 里少一个 key，每日 GC 直接停摆（与 ``load_pools_config`` 同一理由）。
    """
    try:
        loaded = load_app_config(paths)
    except StudioError as exc:
        return (
            RetentionPolicy(),
            f"读不到 config/app.yaml 的 retention（{exc.code}），按 §03.7.5 默认值执行",
        )
    return RetentionPolicy.from_config(loaded.retention), None


def protected_roots(paths: StudioPaths) -> tuple[Path, ...]:
    """**永不清理**的目录根（§03.7.5 末三行）。

    逐条对应：

    - ``output/videos`` / ``output/covers`` / ``output/topics`` —— 成片 / 封面 / 稿件；
    - ``hot/archive`` —— 已消费热点（复盘依据，规则是"移动而非删除"）；
    - ``backups`` —— 备份与迁移前检查点（删了它等于把回滚能力删了）；
    - ``assets`` —— MC 跑酷素材与 BGM 素材库（**人工提供**，删了没法重建）；
    - ``voice_src`` —— 零样本复刻的参考音（同上）。

    注意 ``output/voice`` **不在**这里：§03.7.5 明确给了句子 WAV 24 小时 TTL，
    它是白名单唯一的例外（见 :mod:`studio.gc.media`）。
    """
    return (
        paths.assets_dir,
        paths.backups_dir,
        paths.covers_dir,
        paths.hot_archive_dir,
        paths.topics_dir,
        paths.videos_dir,
        paths.voice_src_dir,
    )


def guard_path(path: Path | str, *, paths: StudioPaths) -> Path:
    """删除 / 移动前的**唯一闸口**：不越界 + 不碰白名单。

    返回解析后的绝对路径 —— 调用方拿它去 ``unlink()``，别再自己拼一次：
    两次 ``resolve()`` 之间路径被换成符号链接，就是"检查的是 A、删的是 B"。
    """
    resolved = Path(path).resolve()
    data_root = paths.data_dir.resolve()
    if resolved != data_root and data_root not in resolved.parents:
        raise StudioError(
            f"GC 拒绝越界路径：{resolved}",
            code=ErrorCode.GC_PATH_OUT_OF_BOUNDS,
            context={"path": str(resolved), "data_dir": str(data_root)},
            remediation="GC 只清理 STUDIO_DATA_DIR 内部；模板 / 提示词 / 素材源不在其中",
        )
    for root in protected_roots(paths):
        if _is_within(resolved, root):
            raise StudioError(
                f"GC 拒绝清理受保护路径：{resolved}",
                code=ErrorCode.GC_REFUSED_PROTECTED,
                context={"path": str(resolved), "root": str(root)},
                remediation="成片 / 封面 / 稿件 / 备份 / 素材库 / 热点归档永久保留（§03.7.5）",
            )
    if _is_protected_work_artifact(resolved, paths):
        raise StudioError(
            f"GC 拒绝清理任务交付物：{resolved}",
            code=ErrorCode.GC_REFUSED_PROTECTED,
            context={"path": str(resolved), "names": sorted(PROTECTED_WORK_NAMES)},
            remediation="滤镜图 / 稿件 / 时间轴 / IR / manifest 永久保留，人工确认后才归档",
        )
    return resolved


def _is_within(child: Path, parent: Path) -> bool:
    """``child`` 是否在 ``parent`` 之下（含自身）。父目录不存在也照常判。"""
    try:
        root = parent.resolve()
    except OSError:
        return False
    return child == root or root in child.parents


def _is_protected_work_artifact(resolved: Path, paths: StudioPaths) -> bool:
    """``data/work/<task>/`` 下的交付物（滤镜图 / 稿件 / 时间轴 / IR / manifest）。"""
    work_root = paths.work_dir.resolve()
    if work_root not in resolved.parents:
        return False
    parts = resolved.relative_to(work_root).parts
    return len(parts) >= 2 and parts[1] in PROTECTED_WORK_NAMES
