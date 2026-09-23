"""降级链（T3.7 · §04.2.8.6）—— **出片优先，画质其次**。

三个降级不在同一条轴上
----------------------
规格与清单里都写作"三级降级链：正常 → 720P → 黑屏 → 分块"，读起来像一条线。实际是
**三条互相独立的轴**，触发条件、发生的位置、可恢复性都不一样：

| 轴 | 触发 | 发生在 | 本模块？ |
| --- | --- | --- | --- |
| 黑屏 | 没有底片素材 | 挑素材时（**输入**侧） | 否 —— `clip=None` 即可 |
| 720P | 正常档编码失败 | 合成失败后（**编码**侧） | 是，:func:`deliver` |
| 分块 | 滤镜图节点过多 / 单遍超时 | 编译时（**复杂度**侧） | 否 —— 见下 |

把它们当一条线写，会得到"720P 失败了再黑屏"这种没有意义的时序（黑屏是**输入**的缺失，
不是编码失败的补救）。

为什么不做分块
--------------
分块的判据是 ``estimated_nodes > 60``。一期的合成是**单底片**：整张图的节点数是
scale/crop/fps + 可选的字幕 + 可选的水印 + 音频链，实测十来二十个，离 60 差得远。
现在写分块就是写一段**永远不会被执行、因而永远不会被验证**的代码 —— 等二期上三层模板
（多场景、转场、多段镜头）时，节点数才会真的爆，那时候写才知道该按什么切。
宁可在这里写清"没做、以及什么条件下才该做"，也不要摆一个看起来完备的空壳。

为什么 720P 只重试**一次**
--------------------------
保底档不是"多试几次"：它换的是编码器与分辨率，失败原因（比如机器根本没有 NVENC）不会
因为再试一遍而消失。一次之后仍失败，就让它以**原来的错误**抛出去 —— 那才是重试链
（``attempt_count`` / 退避 / 死信 / ``manual_pool``）该接的手。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from studio.core.errors import ErrorCode, RenderError
from studio.render.composite import (
    PROGRESS_TOTAL,
    CompositeRequest,
    CompositeResult,
    run_composite,
)
from studio.render.profiles import CompositeProfile

__all__ = [
    "COMPOSITE_CHUNKED",
    "NO_BROLL",
    "RENDER_720P",
    "Delivery",
    "deliver",
]

#: 降级原因 —— 直接写进 ``tasks.quality_json.degrade_reason``（§03.5.3）。
#: 用常量而不是散落的字面量：这些字符串会被写进库、被面板读、被报告统计，
#: 拼错一个字母就是"这条降级在报表里凭空消失"。
NO_BROLL: Final[str] = "no_broll_assets"
RENDER_720P: Final[str] = "render_720p"
COMPOSITE_CHUNKED: Final[str] = "composite_chunked"

#: 哪些错误值得换保底档再试一次。只收合成器自己抛的那两个 ——
#: "人声文件不见了"这类错误换分辨率也一样失败，重试只是把真正的报错埋到第二层。
_RETRYABLE: Final[frozenset[ErrorCode]] = frozenset({ErrorCode.RENDER_FAILED, ErrorCode.RENDER_TIMEOUT})


@dataclass(frozen=True, slots=True)
class Delivery:
    """一次交付的结论：成片 + 用的是哪一档 + 为什么降级。"""

    composite: CompositeResult
    profile: CompositeProfile
    #: 用的是不是保底档（黑屏降级**不算**在这里 —— 它降的是画质来源，不是档位）
    degraded: bool
    degrade_reason: str | None
    #: 试过哪几档、各自什么下场（留痕：出问题时先看这一行）
    attempts: tuple[str, ...]
    warnings: tuple[str, ...]


def deliver(
    request: CompositeRequest,
    *,
    fallback_profile: CompositeProfile | None = None,
    fallback_output: Path | None = None,
    replan: Callable[[CompositeProfile], CompositeRequest] | None = None,
    on_progress: Callable[[str, int, int, str], None] | None = None,
) -> Delivery:
    """按 §04.2.8.6 出片：正常档 → （失败时）720P 保底档。

    :param fallback_profile: 保底档。``None`` ⇒ **不做 720P 回退**，合成失败直接抛
        （测试与"只想要这一档"的调用方走这条路）。
    :param fallback_output: 保底档的落盘路径。文件名带 ``_720p``，是为了让"哪支是保底
        产物"在**文件列表里**就看得出来 —— 只写在 manifest 里，人翻目录时看不到。
    :param replan: "换一块画布 ⇒ 重来一份请求"的工厂。**装饰层的像素坐标必须重算**：
        水印与人物贴图的摆放算的是"这一块画布下的具体像素"（见 ``render/watermark.py``
        与 ``render/sticker.py`` 的 docstring），1080×1920 下算好的 x/y 拿到 720×1280
        上就是另一个位置、甚至整个贴到画布外 —— 而 ``overlay`` 对越界**不报错**，
        它只是把图裁掉，于是"保底档上没有水印 / 没有人物"会静默发生。
        ``None`` ⇒ 只换 profile（**调用方要自己保证装饰层与画布无关**，测试走这条路）。
    """

    def relay(done: int, total: int, note: str) -> None:
        """合成器的 ``(done, total, note)`` ⇒ 上游的 ``("render", done, total, note)``。

        "哪一档在渲"由上游那几句说明负责；这里只搬运"编到哪儿了"。重试时百分比**从头数**：
        换了档就是另一次编码，让进度条接着往上涨会得到"100% 了又回到 0%"。
        """
        if on_progress is not None:
            on_progress("render", done, total, note)

    attempts: list[str] = []
    try:
        result = run_composite(request, on_progress=relay)
    except RenderError as error:
        if fallback_profile is None or fallback_output is None or error.code not in _RETRYABLE:
            raise
        attempts.append(f"{request.profile.name}: {error.code.value}")
        if on_progress is not None:
            on_progress("render", 0, PROGRESS_TOTAL, f"正常档失败（{error.code.value}），换 720P 保底档重试")
        base = request if replan is None else replan(fallback_profile)
        retry = replace(base, profile=fallback_profile, output=fallback_output)
        result = run_composite(retry, on_progress=relay)
        warnings = (
            *result.warnings,
            f"正常档（{request.profile.name}）失败，已用 720P 保底档出片：{error.message}",
        )
        return Delivery(
            composite=result,
            profile=fallback_profile,
            degraded=True,
            degrade_reason=RENDER_720P,
            attempts=tuple(attempts),
            warnings=warnings,
        )

    return Delivery(
        composite=result,
        profile=request.profile,
        degraded=False,
        degrade_reason=None,
        attempts=tuple(attempts),
        warnings=tuple(result.warnings),
    )
