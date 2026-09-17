"""交付包（T5.5 · §06.11 / §06.12 · A2）—— 把一支成片连同它的证据打成一个自包含目录。

为什么需要它（而不是"成片就在 `data/output/videos/` 里，自己去拿"）
----------------------------------------------------------------
一支片子能不能发出去，不只取决于那个 mp4：封面、字幕、稿件、渲染留痕、质检结论、
以及**这份素材的来源登记**，缺一样就得回头翻仓库。而"外部对接未定义"（A2）意味着
这些文件迟早要被交给**另一个系统**（审片台 / 归档盘 / 别人的流水线）。

所以这一层的产出是一个**自包含目录**：拿到它的人不需要知道我们的目录结构、
不需要连我们的库、也不需要读我们的文档。

``HandoffAdapter`` 是可替换的那一个缝
------------------------------------
``LocalHandoffAdapter`` 只是把包复制到 ``data/handoff/`` 下 —— 它是**默认实现**，
不是唯一实现。要接到别的系统（HTTP 上传 / 对象存储 / 人工 U 盘），实现
:class:`HandoffAdapter` 即可，**主流程一行都不用改**：调用方只认
``push(package, output_dir=...)``。

为什么包里有 ``handoff.json``
-----------------------------
它是这个目录的**说明书**：每件交付物的文件名、大小、sha256、缺失原因，外加质检结论、
发布记录（发到哪个平台、什么时候、URL）与 R2 来源登记快照。少了它，收到包的人
只能靠文件名猜"哪个是最终版"。哈希是给**归档**用的：三个月后要确认"这份还是当初
发出去的那份"时，只有一个 sha256 能回答。

缺件怎么处理
------------
``video`` 是**唯一必需**的一件（没有成片的"交付包"没有意义）；其余缺了照打，
在清单里写明 ``missing`` —— 让包**如实不完整**，好过让它打不出来。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.files import file_sha256
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.repositories.publication_repo import (
    PUBLISHED,
    PublicationRepo,
    PublicationRow,
)
from studio.publish.compliance import ComplianceSnapshot, compliance_snapshot
from studio.services.publish_service import resolve_final_video

__all__ = [
    "DELIVERY_KINDS",
    "HANDOFF_MANIFEST_NAME",
    "DeliveryItem",
    "DeliveryPackage",
    "HandoffAdapter",
    "HandoffResult",
    "LocalHandoffAdapter",
    "build_package",
    "handoff_adapter",
    "resolve_cover",
]

logger = get_logger("studio.publish.handoff")

#: 包内**自己的清单**文件名（说明书，见模块注释）。
HANDOFF_MANIFEST_NAME: Final[str] = "handoff.json"

#: 交付物清单（§06.11 点名的那几件）：``(kind, 人话标签, 是否必需)``。
#:
#: 只有成片必需：缺了它这个包没有任何意义。封面 / 字幕 / 稿件 / 时间轴 / 渲染留痕
#: 缺了只是"这份包不完整"，而"不完整"本身是有用的信息（发布前二次校验也读它）。
DELIVERY_KINDS: Final[tuple[tuple[str, str, bool], ...]] = (
    ("video", "成片", True),
    ("cover", "封面", False),
    ("subtitle", "字幕（ASS）", False),
    ("script", "稿件", False),
    ("timeline", "时间轴", False),
    ("manifest", "渲染留痕", False),
)


def resolve_cover(task_id: str, paths: StudioPaths) -> Path | None:
    """这条任务的封面在哪（目录里按名字取**最新**的那一张）。

    与 :func:`~studio.services.publish_service.resolve_final_video` 同一条口径：
    答不出来 ⇒ ``None``（**不抛**）。封面文件名带时间戳（``{stamp}_{task_id}_cover.jpg``），
    所以"重新生成过一张"会留下两张 —— 取最后一张与成片的取法一致。
    """
    directory = paths.covers_dir
    if not directory.is_dir():
        return None
    matches = sorted(directory.glob(f"*_{task_id}_cover.jpg"))
    return matches[-1] if matches else None


@dataclass(frozen=True, slots=True)
class DeliveryItem:
    """一件交付物（**预览与打包共用**这一个模型）。"""

    kind: str
    label: str
    required: bool
    source: Path | None
    note: str | None = None

    @property
    def present(self) -> bool:
        return self.source is not None and self.source.is_file()

    def to_dict(self) -> dict[str, Any]:
        size: int | None = None
        if self.source is not None and self.source.is_file():
            size = self.source.stat().st_size
        return {
            "kind": self.kind,
            "label": self.label,
            "required": self.required,
            "present": size is not None,
            "source": None if self.source is None else self.source.as_posix(),
            "bytes": size,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class DeliveryPackage:
    """一支片子的交付包**内容**（还没落盘）。"""

    task_id: str
    items: tuple[DeliveryItem, ...]
    #: 这次是从哪条发布记录取的文件（没有发布记录 ⇒ ``None``）。
    publication: PublicationRow | None = None
    #: R2 来源登记快照（进包内清单 ⇒ 拿到包的人**不必回头查我们的库**）。
    compliance: ComplianceSnapshot | None = None

    @property
    def missing(self) -> tuple[DeliveryItem, ...]:
        """缺件（**必需的那件缺了才是硬伤**，其余只是不完整）。"""
        return tuple(item for item in self.items if not item.present)

    @property
    def ready(self) -> bool:
        """必需的那几件都在 ⇒ ``True``。"""
        return all(item.present for item in self.items if item.required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ready": self.ready,
            "items": [item.to_dict() for item in self.items],
            "missing": [item.kind for item in self.missing],
        }


@dataclass(frozen=True, slots=True)
class HandoffResult:
    """一次打包的结论（面板与 CLI 都读它）。"""

    task_id: str
    adapter: str
    root: Path
    manifest: Path
    copied: tuple[dict[str, Any], ...]
    missing: tuple[str, ...]
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "adapter": self.adapter,
            "root": self.root.as_posix(),
            "manifest": self.manifest.as_posix(),
            "copied": list(self.copied),
            "missing": list(self.missing),
            "bytes": self.bytes,
        }


def _read_json(path: Path) -> Any:
    """读一个 JSON；不存在 / 读不了 / 不是 JSON ⇒ ``None``（**不抛**）。

    与 ``publish_service._read_json`` 同一条：``manifest.json`` 是"有更好、没有也能
    继续"的输入，为它抛一次异常只会让"这条任务还没渲染"变成一个要 try/except 的常态。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pick_publication(connection: sqlite3.Connection | None, task_id: str) -> PublicationRow | None:
    """这条任务最近一条发布记录（优先已发布的：那份才是**真的送出去过**的）。

    "发出去的那一份"与"盘上最新的那一份"可能不是同一个文件 —— 发布之后又重渲过一次
    就会这样。归档要的是前者，所以优先取 ``published``。
    """
    if connection is None:
        return None
    rows = PublicationRepo(connection).list_for_task(task_id)
    if not rows:
        return None
    for row in rows:
        if row.status == PUBLISHED:
            return row
    return rows[0]


def build_package(
    *, task_id: str, paths: StudioPaths, connection: sqlite3.Connection | None = None
) -> DeliveryPackage:
    """把这条任务的交付物点一遍（**只读盘与库，不复制任何东西**）。

    预览与打包走**同一个函数**：分开写的话，面板上"七件齐"与真打出来的包里
    "少两件"会各自成立 —— 那种不一致最难查（陷阱 #150 / #151 是同一族）。
    """
    sent = _pick_publication(connection, task_id)

    video = None if sent is None else Path(sent.video_path)
    video_note: str | None = None
    if video is None or not video.is_file():
        # 发布记录里的路径可能已经被 GC 收走（它是**当时**那份的绝对路径）——
        # 那就退回"盘上现在有什么"，并在清单里说清这次用的是哪一个。
        if video is not None:
            video_note = "发布记录里的那份已不在盘上（可能被 GC 收走），改用盘上最新的一支"
        video = resolve_final_video(task_id, paths)

    cover = None if sent is None else (None if sent.cover_path is None else Path(sent.cover_path))
    cover_note: str | None = None
    if cover is None or not cover.is_file():
        if cover is not None:
            cover_note = "发布记录里的那份封面已不在盘上，改用盘上最新的一张"
        cover = resolve_cover(task_id, paths)

    sources: dict[str, tuple[Path | None, str | None]] = {
        "video": (video, video_note),
        "cover": (cover, cover_note),
        "subtitle": (paths.subtitle_ass(task_id), None),
        "script": (paths.script_json(task_id), None),
        "timeline": (paths.timeline_json(task_id), None),
        "manifest": (paths.manifest_json(task_id), None),
    }

    items: list[DeliveryItem] = []
    for kind, label, required in DELIVERY_KINDS:
        source, note = sources[kind]
        present = source is not None and source.is_file()
        # 「缺」有两种来源：路径没给（还没渲染 / 还没生成），与路径给了但文件不在了。
        # 两种都要有一句**指向下一步动作**的话 —— 面板上只显示「缺失」等于让用户自己猜
        # 去哪一屏补。发布记录里那份被 GC 收走时 note 已经写好了，不覆盖它。
        if not present and note is None:
            note = _why_missing(kind)
        items.append(
            DeliveryItem(
                kind=kind,
                label=label,
                required=required,
                source=None if not present else source,
                note=note,
            )
        )
    return DeliveryPackage(
        task_id=task_id,
        items=tuple(items),
        publication=sent,
        compliance=None if connection is None else compliance_snapshot(connection, paths=paths),
    )


#: 缺件时那句"为什么缺"（**每一句都要指向一个具体的补救动作**）。
_MISSING_NOTES: Final[dict[str, str]] = {
    "video": "还没有成片：先在「一键出片」或「渲染」面板把这条任务跑完",
    "cover": "还没有封面：发布前二次校验会挡（§06.4 门禁 1），先在「发布」面板生成一张",
    "subtitle": "没有字幕文件：这一版渲染没开字幕，或中间产物已被 GC 收走（重渲一次即可）",
    "script": "没有稿件快照：这条任务还没走过渲染（script.json 由渲染那一步写出）",
    "timeline": "没有时间轴：同上（timeline.json 由配音收口那一步写出）",
    "manifest": "没有渲染留痕：这条任务还没渲染过",
}


def _why_missing(kind: str) -> str:
    return _MISSING_NOTES.get(kind, "盘上没有这一件")


class HandoffAdapter(ABC):
    """交付包的落地方式（**可替换的那一个缝**，见模块注释）。"""

    #: 写进 ``handoff.json`` 与日志的实现名（``local`` / 外部实现自报）。
    name: str = ""

    @abstractmethod
    def push(self, package: DeliveryPackage, *, output_dir: Path) -> HandoffResult:
        """把包落到 ``output_dir`` 下，返回落点与清单。"""


class LocalHandoffAdapter(HandoffAdapter):
    """默认实现：复制到 ``output_dir/<task_id>/<时间戳>/``。

    为什么按时间戳分目录而不是覆盖同一个：交付包是**给人拿走的东西**，一旦拿走就
    脱离了我们。覆盖会得到"上一份被悄悄换掉了"，而两边都没记录 —— 留两份的代价是
    几百 MB，留一份错的代价是"发出去的片子对不上归档"。
    """

    name = "local"

    def push(self, package: DeliveryPackage, *, output_dir: Path) -> HandoffResult:
        # 时间戳取到**毫秒**（``20260917-104610.946``）：留两份的前提是两次打包落在
        # 不同目录，而秒级精度下"连按两下"会算出同一个名字 —— 那正好是要避免的覆盖。
        stamp = now_iso().replace(":", "").replace("-", "").replace("T", "-")[:19]
        root = output_dir / package.task_id / stamp
        root.mkdir(parents=True, exist_ok=True)

        copied: list[dict[str, Any]] = []
        total = 0
        for item in package.items:
            if not item.present or item.source is None:
                continue
            target = root / f"{item.kind}{item.source.suffix}"
            shutil.copy2(item.source, target)
            digest = file_sha256(target)
            size = target.stat().st_size
            total += size
            copied.append(
                {
                    "kind": item.kind,
                    "label": item.label,
                    "file": target.name,
                    "bytes": size,
                    "sha256": digest,
                }
            )

        manifest = root / HANDOFF_MANIFEST_NAME
        manifest.write_text(
            json.dumps(
                _manifest_payload(package, copied=copied, adapter=self.name),
                ensure_ascii=False,
                indent=2,
            )
            + chr(10),
            encoding="utf-8",
        )
        total += manifest.stat().st_size
        logger.info(
            "publish.handoff_pushed",
            task_id=package.task_id,
            root=root.as_posix(),
            copied=len(copied),
            missing=[item.kind for item in package.missing],
        )
        return HandoffResult(
            task_id=package.task_id,
            adapter=self.name,
            root=root,
            manifest=manifest,
            copied=tuple(copied),
            missing=tuple(item.kind for item in package.missing),
            bytes=total,
        )


def _manifest_payload(
    package: DeliveryPackage, *, copied: list[dict[str, Any]], adapter: str
) -> dict[str, Any]:
    """``handoff.json`` 的内容（说明书，见模块注释）。"""
    by_kind = {entry["kind"]: entry for entry in copied}
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "task_id": package.task_id,
        "created_at": now_iso(),
        "adapter": adapter,
        "items": [
            {
                "kind": item.kind,
                "label": item.label,
                "required": item.required,
                "present": item.present,
                "file": by_kind.get(item.kind, {}).get("file"),
                "bytes": by_kind.get(item.kind, {}).get("bytes"),
                "sha256": by_kind.get(item.kind, {}).get("sha256"),
                "source": None if item.source is None else item.source.as_posix(),
                "note": item.note,
            }
            for item in package.items
        ],
        "missing": [item.kind for item in package.missing],
    }
    compliance = package.compliance
    payload["compliance"] = (
        None
        if compliance is None
        else {
            "ok": compliance.ok,
            "gaps": list(compliance.gaps),
            "items": [item.to_dict() for item in compliance.items],
        }
    )
    row = package.publication
    payload["publication"] = (
        None
        if row is None
        else {
            "id": row.id,
            "platform": row.platform,
            "account_id": row.account_id,
            "status": row.status,
            "url": row.url,
            "published_at": row.published_at,
            "dry_run": row.dry_run,
        }
    )
    return payload


#: 适配器注册表（``config/publish.yaml → handoff.adapter`` 里的名字 ⇒ 实现）。
_ADAPTERS: Final[dict[str, type[HandoffAdapter]]] = {"local": LocalHandoffAdapter}


def handoff_adapter(name: str) -> HandoffAdapter:
    """按名字取适配器；不认识的名字 ⇒ 抛（**不静默退回 local**）。

    静默退回的后果是"配置写了一个外部实现，包却一直落在本地盘上"，而两边都不报错 ——
    用户会以为对接好了。宁可开面板时报一句"没有这个适配器"。
    """
    factory = _ADAPTERS.get(name)
    if factory is None:
        raise StudioError(
            f"没有这个交付包适配器：{name}",
            code=ErrorCode.CONFIG_INVALID,
            context={"adapter": name, "known": sorted(_ADAPTERS)},
            remediation=(
                "把 config/publish.yaml → handoff.adapter 改成 local，"
                "或在 publish/handoff.py 的 _ADAPTERS 里注册你的实现"
            ),
        )
    return factory()
