"""整片级渲染缓存（T3.4 · §04.2.8.7）—— 同哈希就不必再渲一遍。

判据是哈希，不是「文件在不在」
------------------------------
``pools/render_worker.py`` 的纪律 2 明确否掉过一种短路：「产物已存在就跳过」。理由是
「上次跑到一半崩了」与「已经完成」在盘上长得一模一样 —— 只看文件在不在，就会把半成品
当成品交出去。

这里做的**不是**那种短路。命中要同时满足四条，缺一条就重渲：

1. 上一轮 ``manifest.json`` 记的 ``composite_hash`` 与**这一次**算出来的完全一致 ——
   哈希里含 canonical plan（编码参数 / 时长 / 画面来源 / 水印位置 / 混音参数）与每个
   输入文件的 ``sha256``（人声母带、底片、BGM、水印 PNG、字幕 ASS）；
2. manifest 记的 ``final`` 就是盘上那一支（不是另一条任务、另一个名字的文件）；
3. 那一支还在盘上，且**字节数与 manifest 记的一致**（截断 / 被换掉的文件过不了）；
4. 重建 :class:`~studio.render.composite.CompositeResult` 要用的字段一个不少、类型都对
   —— 缺一个就重渲，而不是猜个默认值填上。

第 1 条还顺手挡掉了半成品：manifest 是**最后**才写的（成片原子改名 → 量响度 → 写
manifest），所以「manifest 在」本身就意味着「那一轮跑到底了」。

为什么不建一个哈希命名的缓存目录
--------------------------------
成片已经在 ``data/output/videos/`` 里按天保留、由清理策略回收。再拷一份到
``data/cache/render/<hash>.mp4`` 等于同一支片子存两遍，还要给第二份再写一套 GC 与配额。
一期要的只是「重跑任务时省时」（§04.2.8.7），复用本体就够。

命中面有多大（如实说）
----------------------
底片是**随机**挑的（陷阱 #14：反搬运）。所以不带 ``seed`` 的重跑通常挑到另一条底片 ⇒
哈希不同 ⇒ 照常重渲 —— 这是设计如此，不是缓存失效。真正会命中的是：

① 带 ``seed`` 的重跑 / 复现（CLI ``--seed`` / REST ``body.seed``）；
② 队列把同一条 ``render/final`` 重投（payload 里带着同一个 seed）；
③ 恰好又挑到同一条底片的重跑。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.render.composite import CompositeResult
from studio.render.mixdown import LoudnessMeasurement

__all__ = [
    "CacheHit",
    "reusable_output",
]


class _UnusableError(Exception):
    """这一份 manifest 不足以支撑复用（缺字段 / 类型不对 / 产物对不上）。

    内部信号，**不出模块**：对外只有「命中」与「不命中」两种结果。之所以用异常而不是
    一串 ``return None``，是因为校验有十来条、每条都要带上「是哪一条没过」的说明 ——
    写成返回值会在每一行后面挂一个 ``if``，读的人得在十几个分支里拼出"到底为什么重渲"。
    """


@dataclass(frozen=True, slots=True)
class CacheHit:
    """一次命中的全部证据 —— 每一项都是从上一轮 manifest 里**读回来**的，没有推断。"""

    output: Path
    manifest: Path
    plan_hash: str
    #: 上一轮真正生效的档位名（降级到 720P 时它与请求里的档位不是同一个）
    profile_name: str
    composite: CompositeResult
    #: 成片落盘后量到的响度（命中时**不重量**：文件没变，读数就没变）
    output_loudness: LoudnessMeasurement | None
    degraded: bool
    degrade_reason: str | None
    attempts: tuple[str, ...]
    #: 那一支成片**渲出来的**时刻（manifest 的 ``created_at``；老格式没记 ⇒ None）
    rendered_at: str | None
    #: 上一轮留下的降级 / 跳过说明（原样沿用：它们描述的是同一支片子）
    warnings: tuple[str, ...]


def reusable_output(*, manifest: Path, plan_hash: str) -> CacheHit | None:
    """上一轮渲出来的那一支能不能直接复用；``None`` ⇒ 老老实实重渲。

    :param manifest: 上一次出片写的 ``data/work/<task_id>/manifest.json``
    :param plan_hash: 这一次要渲的东西的指纹（:func:`~studio.render.hashing.composite_hash`）
    """
    try:
        return _reusable(manifest=manifest, plan_hash=plan_hash)
    except (_UnusableError, OSError, ValueError):
        # 读盘出错（权限 / 被占用）与"内容不能用"在这里是同一种下场：重渲一遍。
        # 缓存是**省时**手段，为它让一次出片失败，是拿优化堵主链路。
        return None


def _reusable(*, manifest: Path, plan_hash: str) -> CacheHit:
    payload = _load(manifest)
    if payload.get("composite_hash") != plan_hash:
        raise _UnusableError("composite_hash 与这一次算出来的不一致")

    output = Path(_field(payload, "final", str))
    size_bytes = _field(payload, "size_bytes", int)
    if not output.is_file():
        raise _UnusableError("manifest 记的那支成片不在盘上（被清理或换了家目录）")
    if output.stat().st_size != size_bytes:
        raise _UnusableError("成片字节数与 manifest 记的不一致（被截断或换过内容）")

    watermark = _field(payload, "watermark", dict)
    subtitle = _field(payload, "subtitle", dict)

    return CacheHit(
        output=output,
        manifest=manifest,
        plan_hash=plan_hash,
        profile_name=_field(payload, "profile", str),
        composite=CompositeResult(
            output=output,
            duration_ms=_field(payload, "duration_ms", int),
            size_bytes=size_bytes,
            watermark_applied=_field(watermark, "enabled", bool),
            # 有没有 BGM 看 ``bgm`` 是不是 null —— 这是那一轮挑素材的结果，不必再猜。
            bgm_applied=payload.get("bgm") is not None,
            subtitle_applied=_field(subtitle, "enabled", bool),
            bg_fill=_field(payload, "bg_fill", str),
            loudness=_measurement(payload.get("loudness")),
            # ``warnings`` 允许缺省（缺省 = 那一轮没有话要说）；其余字段缺一个就重渲。
            warnings=_strings(payload.get("warnings", []), "warnings"),
            argv=_strings(_field(payload, "argv", list), "argv"),
        ),
        output_loudness=_measurement(payload.get("output_loudness")),
        degraded=_field(payload, "degraded", bool),
        degrade_reason=_optional_str(payload.get("degrade_reason"), "degrade_reason"),
        attempts=_strings(_field(payload, "attempts", list), "attempts"),
        rendered_at=_optional_str(payload.get("created_at"), "created_at"),
        warnings=_strings(payload.get("warnings", []), "warnings"),
    )


def _load(manifest: Path) -> Mapping[str, Any]:
    if not manifest.is_file():
        raise _UnusableError("没有上一轮的 manifest.json（这条任务还没出过片）")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise _UnusableError(f"manifest.json 不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise _UnusableError("manifest.json 的顶层不是对象")
    return payload


def _field(payload: Mapping[str, Any], key: str, kind: type) -> Any:
    """取一个**必需**字段，类型不对就判这一份不能用。

    ``bool`` 是 ``int`` 的子类：``"size_bytes": true`` 能过 ``isinstance(..., int)``，
    然后被当成 1 字节 —— 这类"能过类型检查的错值"正是要挡的东西，所以取整数时先把
    bool 挑出来（与 ``render_worker._opt_int`` 同一条纪律）。
    """
    if key not in payload:
        raise _UnusableError(f"manifest 缺字段：{key}")
    value = payload[key]
    if kind is int and isinstance(value, bool):
        raise _UnusableError(f"{key} 是布尔值，不是整数")
    if not isinstance(value, kind):
        raise _UnusableError(f"{key} 的类型不对：{type(value).__name__}")
    if kind is str and not value:
        raise _UnusableError(f"{key} 是空串")
    return value


def _optional_str(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _UnusableError(f"{key} 既不是字符串也不是 null")
    return value


def _strings(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _UnusableError(f"{key} 不是字符串数组")
    return tuple(value)


def _measurement(value: Any) -> LoudnessMeasurement | None:
    """响度读数。``null`` ⇒ 那一轮没量出来（**不是**错误）；结构不对 ⇒ 这份不能用。"""

    def number(name: str) -> float:
        raw = value.get(name) if isinstance(value, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise _UnusableError(f"响度读数缺字段：{name}")
        return float(raw)

    if value is None:
        return None
    return LoudnessMeasurement(
        input_i=number("input_i"),
        input_tp=number("input_tp"),
        input_lra=number("input_lra"),
        input_thresh=number("input_thresh"),
        target_offset=number("target_offset"),
    )
