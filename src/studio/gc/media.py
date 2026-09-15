"""媒资回收（T4.12 · §03.7.5）——文件那一半；DB 行那一半在 :mod:`studio.gc.rows`。

候选清单，而不是目录扫描
------------------------
GC 无人值守地跑，一旦判据写错就是"扫到哪删到哪"。所以这里的结构是
**先列候选 → 再过守卫 → 最后才落地**：每个候选都说得出"凭哪条规则、属于哪个任务"，
而 :func:`~studio.gc.policy.guard_path` 是唯一的落地闸口。

成片缺失时**不删音频**
----------------------
§03.7.5 说句子 WAV 在任务 ``completed`` 后 24 小时删除（"保留 tts_hash 与缓存副本"）。
但缓存是会被 LRU 淘汰的，于是"交付物还在不在"成了删除音频前的最后一道人肉判据：
任务上下文里没有 ``final_path``、或那个文件**已经不在了** ⇒ 跳过，并把原因写进报告。
宁可多占几个 MB，也不做"把最后一份东西删掉"的那一步。

TTS 缓存的 ``use_count`` 从哪来
------------------------------
缓存条目（T2.x 落地）约定为「一份音频 + 同名 ``.meta.json`` 旁车」，
旁车形如 ``{"use_count": 3, "last_used_at": "2026-09-14T02:00:00.000Z"}``。
读不到旁车就按 ``use_count = 0`` 处理 —— **降权是优化，不是前提**：没有旁车时
按文件访问时间淘汰，缓存不会因此长到天上去。``use_count >= 2`` 的条目
（§03.7.5「复用率高，是主要提速手段」）排在同权重队列的**最后**才被淘汰。

``data/tmp/`` 不动
------------------
它是 pytest / node / uv 与在途渲染共用的暂存区（``STUDIO_DATA_DIR/tmp``），
删它的风险与收益完全不成比例。GC 只在报告里给出它的**体积**，不碰内容。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from studio.core.clock import parse_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.gc.policy import RetentionPolicy, guard_path

__all__ = [
    "FAILED_STATUSES",
    "REUSE_THRESHOLD",
    "CompletedTask",
    "FileAction",
    "FileNote",
    "MediaGcResult",
    "gc_media",
    "tree_size",
]

logger = get_logger("studio.gc.media")

# ── 候选来源（报告里的 reason）────────────────────────────────────────
REASON_HOT_ARCHIVE: Final[str] = "hot_archive"
REASON_PARTIAL: Final[str] = "partial"
REASON_SCENE_ARTIFACT: Final[str] = "scene_artifact"
REASON_SENTENCE_AUDIO: Final[str] = "sentence_audio"
REASON_TTS_CACHE: Final[str] = "tts_cache"
REASON_VOICE_MASTER: Final[str] = "voice_master"

# ── 跳过原因（不是错误，但必须出现在报告里）──────────────────────────
SKIP_FINAL_MISSING: Final[str] = "FINAL_MISSING"
SKIP_FINAL_UNKNOWN: Final[str] = "FINAL_UNKNOWN"

#: 失败任务的中间产物**立刻**清（§03.7.5「失败任务立即清理 ``.partial``」）
FAILED_STATUSES: Final[tuple[str, ...]] = ("failed", "canceled", "discarded")

#: ``use_count`` 达到这个数 ⇒ 淘汰降权（§03.7.5）
REUSE_THRESHOLD: Final[int] = 2

#: TTS 缓存旁车后缀（``<name>.meta.json``）
META_SUFFIX: Final[str] = ".meta.json"


@dataclass(frozen=True, slots=True)
class CompletedTask:
    """一个已完成的任务 + 它的交付物位置（判"能不能删音频"用）。"""

    task_id: str
    finished_at: datetime
    deliverable: Path | None = None


@dataclass(frozen=True, slots=True)
class FileAction:
    """一次落地（删除 / 移动）。"""

    path: Path
    reason: str
    size_bytes: int
    moved_to: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(self.path),
            "reason": self.reason,
            "size_bytes": self.size_bytes,
        }
        if self.moved_to is not None:
            payload["moved_to"] = str(self.moved_to)
        return payload


@dataclass(frozen=True, slots=True)
class FileNote:
    """没落地的一次候选（守卫拒绝 / 正常跳过）。"""

    path: Path
    code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "code": self.code, "detail": self.detail}


@dataclass(slots=True)
class MediaGcResult:
    """文件回收的结论。

    ``tts_cache_bytes`` / ``hot_archive_bytes`` 是**回收后**的占用（dry-run 时是预计值）——
    报告要回答的是"现在还剩多少"，不是"跑之前有多少"。
    """

    deleted: tuple[FileAction, ...] = ()
    moved: tuple[FileAction, ...] = ()
    refused: tuple[FileNote, ...] = ()
    skipped: tuple[FileNote, ...] = ()
    tts_cache_bytes: int = 0
    tts_cache_limit_bytes: int = 0
    hot_archive_bytes: int = 0
    tmp_bytes: int = 0
    dry_run: bool = False

    @property
    def freed_bytes(self) -> int:
        return sum(item.size_bytes for item in self.deleted)

    @property
    def moved_bytes(self) -> int:
        return sum(item.size_bytes for item in self.moved)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "deleted": [item.to_dict() for item in self.deleted],
            "moved": [item.to_dict() for item in self.moved],
            "refused": [item.to_dict() for item in self.refused],
            "skipped": [item.to_dict() for item in self.skipped],
            "freed_bytes": self.freed_bytes,
            "moved_bytes": self.moved_bytes,
            "tts_cache_bytes": self.tts_cache_bytes,
            "tts_cache_limit_bytes": self.tts_cache_limit_bytes,
            "hot_archive_bytes": self.hot_archive_bytes,
            "tmp_bytes": self.tmp_bytes,
        }


def gc_media(
    paths: StudioPaths,
    *,
    policy: RetentionPolicy,
    now: datetime,
    completed: tuple[CompletedTask, ...] = (),
    failed_task_ids: tuple[str, ...] = (),
    dry_run: bool = False,
) -> MediaGcResult:
    """按 §03.7.5 回收文件。``dry_run`` 时只列清单、一个字节都不动。"""
    collector = _Collector(paths, dry_run=dry_run)
    _collect_task_audio(collector, paths, policy=policy, now=now, completed=completed)
    _collect_work_artifacts(collector, paths, policy=policy, now=now, failed_task_ids=failed_task_ids)
    cache_bytes = _collect_tts_cache(collector, paths, policy=policy)
    archive_bytes = _collect_hot_archive(collector, paths, policy=policy, now=now)
    return MediaGcResult(
        deleted=tuple(collector.deleted),
        moved=tuple(collector.moved),
        refused=tuple(collector.refused),
        skipped=tuple(collector.skipped),
        tts_cache_bytes=cache_bytes,
        tts_cache_limit_bytes=policy.tts_cache_limit_bytes,
        hot_archive_bytes=archive_bytes,
        tmp_bytes=tree_size(paths.tmp_dir),
        dry_run=dry_run,
    )


# ══════════════════════════════════════════════════════════════════════
# 候选收集
# ══════════════════════════════════════════════════════════════════════


def _collect_task_audio(
    collector: _Collector,
    paths: StudioPaths,
    *,
    policy: RetentionPolicy,
    now: datetime,
    completed: tuple[CompletedTask, ...],
) -> None:
    """① 交付级句子音频 + 人声母带（任务 completed 后延时删除）。"""
    sentence_ttl = timedelta(hours=policy.sentence_audio_hours)
    master_ttl = timedelta(hours=policy.voice_master_hours)
    for task in completed:
        age = now - task.finished_at
        wanted = age >= sentence_ttl or age >= master_ttl
        if wanted and not _deliverable_kept(task):
            collector.skip(
                paths.voice_dir_for(task.task_id),
                SKIP_FINAL_MISSING if task.deliverable is not None else SKIP_FINAL_UNKNOWN,
                f"任务 {task.task_id} 的成片不在，句子音频保留",
            )
            continue
        if age >= sentence_ttl:
            for wav in sorted(paths.voice_dir_for(task.task_id).glob("s*.wav")):
                collector.delete(wav, REASON_SENTENCE_AUDIO)
        if age >= master_ttl:
            collector.delete(paths.voice_master(task.task_id), REASON_VOICE_MASTER)


def _deliverable_kept(task: CompletedTask) -> bool:
    """成片还在 ⇒ 音频可以删；不在 / 不知道 ⇒ 不许删。"""
    return task.deliverable is not None and task.deliverable.is_file()


def _collect_work_artifacts(
    collector: _Collector,
    paths: StudioPaths,
    *,
    policy: RetentionPolicy,
    now: datetime,
    failed_task_ids: tuple[str, ...],
) -> None:
    """② 场景中间产物按 mtime 过期；失败任务的 ``.partial`` 不等 TTL。

    交付物（``graphs/`` / ``script.json`` / ``timeline.json`` / ``ir.json`` /
    ``manifest.json``）由 :func:`~studio.gc.policy.guard_path` 挡在门外 ——
    这里**故意**不做第二次判断：白名单只写一处，才不会两处慢慢走岔。
    """
    work_root = paths.work_dir
    if not work_root.is_dir():
        return
    ttl = timedelta(hours=policy.scene_artifact_hours)
    failed = set(failed_task_ids)
    for task_dir in sorted(work_root.iterdir()):
        if not task_dir.is_dir():
            continue
        is_failed = task_dir.name in failed
        for item in sorted(task_dir.rglob("*")):
            if not item.is_file():
                continue
            if item.name.endswith(".partial"):
                if is_failed or _age(now, item) >= ttl:
                    collector.delete(item, REASON_PARTIAL)
                continue
            if _is_scene_artifact(item, task_dir) and _age(now, item) >= ttl:
                collector.delete(item, REASON_SCENE_ARTIFACT)


def _is_scene_artifact(item: Path, task_dir: Path) -> bool:
    """``work/<task>/scenes/`` 下的东西（场景中间产物）。"""
    try:
        parts = item.relative_to(task_dir).parts
    except ValueError:
        return False
    return len(parts) >= 2 and parts[0] == "scenes"


def _collect_tts_cache(
    collector: _Collector,
    paths: StudioPaths,
    *,
    policy: RetentionPolicy,
) -> int:
    """③ TTS 缓存按 LRU 压到上限，返回**回收后**的占用。

    淘汰顺序（升序取前面的）：先 ``use_count < 2``，再 ``use_count >= 2``；
    同档内按"最久没用过"排。旁车缺失 ⇒ ``use_count = 0`` 且退回文件时间戳。
    """
    root = paths.tts_cache_dir
    limit = policy.tts_cache_limit_bytes
    if not root.is_dir():
        return 0
    entries = [item for item in root.rglob("*") if item.is_file() and not item.name.endswith(META_SUFFIX)]
    total = sum(_size(item) for item in entries)
    if total <= limit:
        return total
    for item in sorted(entries, key=_eviction_key):
        if total <= limit:
            break
        size = _size(item)
        collector.delete(item, REASON_TTS_CACHE)
        collector.delete(_meta_path(item), REASON_TTS_CACHE)
        total -= size
    return total


def _collect_hot_archive(
    collector: _Collector,
    paths: StudioPaths,
    *,
    policy: RetentionPolicy,
    now: datetime,
) -> int:
    """④ 已消费热点**移动**进 ``data/hot/archive/<YYYYMM>/``（§03.7.5：移动而非删除）。

    归档区自己是白名单（``protected_roots``），所以这里只往里写、从不往外删。
    返回归档区当前体积 —— 它是"复盘依据"的代价，得让人看得见。
    """
    root = paths.hot_dir
    ttl = timedelta(days=policy.hot_archive_days)
    if root.is_dir():
        for item in sorted(root.iterdir()):
            if item.is_file() and _age(now, item) >= ttl:
                collector.move(item, _month_bucket(item), REASON_HOT_ARCHIVE)
    return tree_size(paths.hot_archive_dir)


def _month_bucket(path: Path) -> str:
    """按文件自己的月份分桶（``202609``）—— 一个桶里的东西好一次看完。"""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y%m")


def _eviction_key(path: Path) -> tuple[int, float, str]:
    meta = _read_meta(path)
    used = meta.get("use_count")
    last = meta.get("last_used_at")
    when = parse_iso(last).timestamp() if isinstance(last, str) and last else _access_time(path)
    return (1 if isinstance(used, int) and used >= REUSE_THRESHOLD else 0, when, path.name)


def _read_meta(path: Path) -> dict[str, Any]:
    """读旁车；读不出 / 不是对象 ⇒ 空字典（降权信息缺失不该让 GC 报错）。"""
    try:
        raw = json.loads(_meta_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _meta_path(path: Path) -> Path:
    return path.parent / f"{path.name}{META_SUFFIX}"


def _access_time(path: Path) -> float:
    """最近一次"被用到"的时间。

    ``max(atime, mtime)`` 而不是只看 atime：Windows 上 NTFS 的"最后访问时间"
    默认是**关着**的（``NtfsDisableLastAccessUpdate``），只看 atime 会让所有条目
    排成同一个时间，LRU 退化成随机淘汰。
    """
    try:
        info = path.stat()
    except OSError:
        return 0.0
    return max(info.st_atime, info.st_mtime)


# ══════════════════════════════════════════════════════════════════════
# 落地器
# ══════════════════════════════════════════════════════════════════════


class _Collector:
    """候选 → 守卫 → 落地的唯一通道。

    为什么不散在调用处：``guard_path`` 少调一次，就是一条绕过白名单的删除路径。
    收在这里之后，"删"这个动作在整个 GC 里只出现一次。
    """

    def __init__(self, paths: StudioPaths, *, dry_run: bool) -> None:
        self._paths = paths
        self._dry_run = dry_run
        self.deleted: list[FileAction] = []
        self.moved: list[FileAction] = []
        self.refused: list[FileNote] = []
        self.skipped: list[FileNote] = []

    def delete(self, path: Path, reason: str) -> None:
        try:
            resolved = guard_path(path, paths=self._paths)
        except StudioError as exc:
            self.refused.append(FileNote(path=path, code=str(exc.code), detail=reason))
            return
        if not resolved.is_file():
            return
        size = _size(resolved)
        if not self._dry_run:
            try:
                resolved.unlink()
            except OSError as exc:
                logger.warning("gc.unlink_failed", path=str(resolved), error=str(exc))
                self.refused.append(
                    FileNote(path=resolved, code=str(ErrorCode.GC_ACTION_FAILED), detail=reason)
                )
                return
        self.deleted.append(FileAction(path=resolved, reason=reason, size_bytes=size))

    def move(self, path: Path, bucket: str, reason: str) -> None:
        try:
            resolved = guard_path(path, paths=self._paths)
        except StudioError as exc:
            self.refused.append(FileNote(path=path, code=str(exc.code), detail=reason))
            return
        if not resolved.is_file():
            return
        size = _size(resolved)
        target = _unique_target(self._paths.hot_archive_dir / bucket / resolved.name)
        if not self._dry_run:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(resolved), str(target))
            except OSError as exc:
                logger.warning("gc.move_failed", path=str(resolved), error=str(exc))
                self.refused.append(
                    FileNote(path=resolved, code=str(ErrorCode.GC_ACTION_FAILED), detail=reason)
                )
                return
        self.moved.append(FileAction(path=resolved, reason=reason, size_bytes=size, moved_to=target))

    def skip(self, path: Path, code: str, detail: str) -> None:
        self.skipped.append(FileNote(path=path, code=code, detail=detail))


def _unique_target(target: Path) -> Path:
    """同名时退让成 ``name-2`` / ``name-3`` ……（**绝不覆盖**归档区里的东西）。"""
    if not target.exists():
        return target
    for index in range(2, 1000):
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    return target


def _age(now: datetime, path: Path) -> timedelta:
    return now - datetime.fromtimestamp(_mtime(path), tz=UTC)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def tree_size(root: Path) -> int:
    """目录下所有文件的字节数（不存在 ⇒ 0）。观测面板与 GC 报告共用这一份口径。"""
    if not root.is_dir():
        return 0
    return sum(_size(item) for item in root.rglob("*") if item.is_file())
