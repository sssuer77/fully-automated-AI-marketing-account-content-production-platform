"""人物贴图：参数模型 + 资产实测 + 编译期摆放（T6.5）。

与水印是**同一种东西、不同的用途**
----------------------------------
两者都是"一张透明 PNG 按固定位置贴到画面上"。区别在**尺寸口径**：水印是角标
（禁止超过画布 1/4 宽），贴图是主体（可以占大半屏、可以顶满高度）。所以参数各一份、
摆放各一份，但**PNG 实测**与**取偶对齐**这两件通用的事共用
（:mod:`studio.render.png_probe` / :mod:`studio.render.alignment`）。

口径：**有就贴，没有就跳过**（与水印完全一致）
----------------------------------------------
贴图是**可选**输入。盘上有可用的 PNG ⇒ 贴；没有 / 读不出来 / 不带透明通道 / 放不进
画布 ⇒ **跳过这一层继续出片**，原因写进该层的 ``skipped_reason``。

这条口径是刻意复用水印那一条的（见 ``render/watermark.py`` 的模块注释）：装饰品不该
成为整条链路的单点阻塞。区别只在**粒度** —— 一层贴图跳过不影响其他层，
所以这里返回的是**每一层各自的结论**（``tuple[StickerPlan, ...]``），而不是一个总开关。

为什么是"若干层"而不是"一层"
----------------------------
一支片子里同时出现两个人物（主讲 + 客串）、或者一个人物 + 一个道具是常见构图。
层数**不写死在代码里**：``config/outputs.yaml`` 的 ``stickers`` 段里有几个名字就渲染
几层（``dict`` 保序 ⇒ 声明顺序 = 叠放顺序，先声明的在**下面**）。

为什么 x/y 算成字面量
--------------------
与水域同一条理由：字面量走 ``overlay`` 的常量路径，测试能直接断言"x/y 是偶数"；
表达式求值要带 ``eval=init`` 并走"每帧算一次"的慢路径。代价同样如实说明 ——
``StickerPlacement`` 里存的是**这一块画布下的具体像素**，换画布（1080×1920 ⇒
720×1280 的保底档）必须**重算**，不能拿旧计划改个数字继续用。
:func:`plan_stickers` 就是那个重算入口。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from studio.core.config import StickerConfig, WatermarkPosition
from studio.core.errors import ErrorCode, RenderError
from studio.render.alignment import even_floor
from studio.render.png_probe import PngAsset, probe_png
from studio.render.speech import Interval, SpeakingPlan

__all__ = [
    "ResolvedSticker",
    "StickerAsset",
    "StickerPlacement",
    "StickerPlan",
    "StickerSpec",
    "aspect_warnings",
    "place_sticker",
    "plan_stickers",
    "resolve_sticker",
    "speaking_problem",
]

#: 贴图资产 = 通用 PNG 实测结果（与水印同一个类型：那三个事实没有区别）。
StickerAsset = PngAsset

#: 画布高太小就放不下任何贴图 —— 与 ``resolve_watermark`` 的"画布宽太小"同一个判据。
_MIN_CANVAS_PX = 8


class StickerSpec(BaseModel):
    """★ 一层人物贴图。

    "固定"指的是**摆放**：位置与尺寸在整条片子里恒定，不随机、不漂移。图本身可以
    有两张（普通 / 讲话）—— 那是"换哪张图"，不是"挪位置"；两张共用同一个摆放
    （见 :attr:`speaking_path`）。
    """

    model_config = ConfigDict(extra="forbid")

    #: 槽位名（``config/outputs.yaml`` 里那个键）。进 manifest，用来回答
    #: "这一层是哪个槽位贴上去的"。
    name: str
    enabled: bool = True
    image_path: Path
    #: 讲话时换的那张图（``None`` ⇒ 这一层不换图）。**摆放与** :attr:`image_path`
    #: 完全一致 —— 换图不挪位置、不改尺寸，否则人物会在开口的一瞬间跳一下。
    speaking_path: Path | None = None
    #: 这一层代表哪个说话人（进 manifest，也是"换图按谁的时间算"的答案）。
    speaker: str = ""
    position: WatermarkPosition = "bottom_right"
    margin_px: tuple[int, int] = (48, 420)
    #: 目标**高**（宽度按素材宽高比推出来）。两个都取偶，理由见 ``alignment.py``。
    height_px: int = Field(default=864, ge=2)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("margin_px")
    @classmethod
    def _margins_must_be_even(cls, value: tuple[int, int]) -> tuple[int, int]:
        x, y = value
        if x < 0 or y < 0:
            raise ValueError(f"贴图边距不得为负：{value}")
        if x % 2 or y % 2:
            raise ValueError(f"贴图边距必须为偶数（overlay 色度对齐）：{value}")
        return value

    @field_validator("height_px")
    @classmethod
    def _height_must_be_even(cls, value: int) -> int:
        if value % 2:
            raise ValueError(f"贴图高度必须为偶数（overlay 色度对齐）：{value}")
        return value

    def to_dict(self) -> dict[str, object]:
        """写进 ``manifest.json`` 的形状（路径统一正斜杠，跨机可读）。"""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "image_path": self.image_path.as_posix(),
            "speaking_path": None if self.speaking_path is None else self.speaking_path.as_posix(),
            "speaker": self.speaker,
            "position": self.position,
            "margin_px": list(self.margin_px),
            "height_px": self.height_px,
            "opacity": self.opacity,
        }


@dataclass(frozen=True, slots=True)
class ResolvedSticker:
    """``config/outputs.yaml`` 的一个贴图槽位 + **一块具体画布** ⇒ 计划里的参数。

    ``warnings`` 是"照做但如实记账"的条目（目前只有一条：高度被夹到画布高）。
    """

    spec: StickerSpec
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"spec": self.spec.to_dict(), "warnings": list(self.warnings)}


@dataclass(frozen=True, slots=True)
class StickerPlacement:
    """编译期算好的摆放位置（**像素字面量**，见模块 docstring）。"""

    x: int
    y: int
    width_px: int
    height_px: int

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width_px": self.width_px, "height_px": self.height_px}


@dataclass(frozen=True, slots=True)
class StickerPlan:
    """一层贴图的**结论**：贴还是不贴，不贴是因为什么。

    ``placement is None`` ⇒ 这一层不贴（跳过），原因在 ``skipped_reason``。
    渲染编译器只看 ``placement``：有就加一层 ``overlay``，没有就跳过那一层 ——
    **没有第三条分支**（不存在"报错退出"）。
    """

    spec: StickerSpec
    asset: StickerAsset
    placement: StickerPlacement | None = None
    skipped_reason: str | None = None
    warnings: tuple[str, ...] = ()
    #: 讲话图这一路的实测（``None`` ⇒ 这一层没配讲话图）。**与** :attr:`asset` 分开
    #: 一份：讲话图坏了不该让普通图也贴不上（装饰的两条腿各自站着）。
    speaking_asset: StickerAsset | None = None
    #: 这一层这次要换图的那些区间（毫秒，闭区间；合并后的）。
    speaking_intervals: tuple[Interval, ...] = ()
    #: "这一层为什么不换图"（配了讲话图却没换成时才有值）。**不是** ``skipped_reason``：
    #: 那一层照样贴得上，只是不换图 —— 两件事混在一个字段里，面板就没法同时说清。
    speaking_problem: str | None = None
    #: 讲话区间的来源（``timeline`` / ``cues`` / ``none``）+ 为什么没有区间。
    speaking_source: str = "none"
    speaking_note: str | None = None

    @property
    def applied(self) -> bool:
        """这一层这次真的贴上了吗（``placement`` 有值才算）。"""
        return self.placement is not None

    @property
    def swaps(self) -> bool:
        """这一层这次真的会**换图**吗（贴上了 + 讲话图可用 + 有讲话区间）。

        三个条件缺一不可，而它们各自都会失败：没贴上（图坏了）、讲话图坏了、
        这个人一句词都没有。渲染路径只认这一个属性 —— 与 :attr:`applied` 一样，
        判断点只有一处。
        """
        return (
            self.placement is not None
            and self.speaking_asset is not None
            and self.speaking_asset.usable
            and bool(self.speaking_intervals)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.spec.name,
            "enabled": self.spec.enabled,
            "asset": self.asset.to_dict(),
            "placement": None if self.placement is None else self.placement.to_dict(),
            "skipped_reason": self.skipped_reason,
            "warnings": list(self.warnings),
            "speaking": {
                "speaker": self.spec.speaker,
                "path": None if self.spec.speaking_path is None else self.spec.speaking_path.as_posix(),
                "asset": None if self.speaking_asset is None else self.speaking_asset.to_dict(),
                "intervals": [list(item) for item in self.speaking_intervals],
                "swapped": self.swaps,
                "problem": self.speaking_problem,
                "source": self.speaking_source,
                "note": self.speaking_note,
            },
        }


def resolve_sticker(
    config: StickerConfig,
    *,
    name: str,
    canvas_height: int,
    home: Path,
) -> ResolvedSticker:
    """把一个槽位 + **一块具体画布** ⇒ 计划里的参数。

    相对路径按 ``STUDIO_HOME`` 解析成绝对路径 —— 理由与水印一样：渲染 worker 的工作
    目录不保证是仓库根，而 ``StickerSpec`` 会原样写进 ``manifest.json`` 并被拿去算缓存
    哈希，存相对路径会让"同一份配置"在两个 CWD 下算出两个哈希。

    这里**不碰盘**：图在不在是 :func:`probe_png` 的事。分开的理由是面板/CLI 想显示
    "参数是什么 + 文件在不在"两件事，而它们各自会失败。
    """
    if canvas_height < _MIN_CANVAS_PX:
        raise RenderError(
            f"画布高太小，放不下贴图：{canvas_height}px",
            code=ErrorCode.RENDER_WATERMARK_INVALID,
            context={"canvas_height": canvas_height, "sticker": name},
            remediation="检查 config/outputs.yaml 里这一档 profile 的 height",
        )

    height_px = config.height_px_for(canvas_height)
    raw_px = round(config.height_ratio * canvas_height)
    warnings: tuple[str, ...] = ()
    if height_px != raw_px:
        # 两种原因都会走到这里：比例算出超过画布高（ratio 已 ≤1.0，只在奇数画布高下
        # 可能越界 1px）、以及取偶。不分开写两条：人看到的都是「比例算出来的高度被改了」。
        warnings = (f"贴图高度按画布修正：{raw_px}px ⇒ {height_px}px（画布高 {canvas_height}px，并取偶）",)

    path = config.path if config.path.is_absolute() else home / config.path
    speaking_path = config.speaking_path
    if speaking_path is not None and not speaking_path.is_absolute():
        speaking_path = home / speaking_path
    spec = StickerSpec(
        name=name,
        enabled=config.enabled,
        image_path=path,
        speaking_path=speaking_path,
        speaker=config.speaker,
        position=config.position,
        margin_px=(config.margin_x, config.margin_y),
        height_px=height_px,
        opacity=config.opacity,
    )
    return ResolvedSticker(spec=spec, warnings=warnings)


def place_sticker(
    spec: StickerSpec,
    asset: StickerAsset,
    *,
    canvas_width: int,
    canvas_height: int,
) -> StickerPlacement | None:
    """算出贴图在画布上的 ``x/y`` 与缩放后的 ``宽/高``；放不下 ⇒ ``None``（跳过）。

    高度是给定的（``spec.height_px``），宽度按素材原始宽高比推出来 —— 两个都取偶
    （:mod:`studio.render.alignment`）。这样越界判断用的是**真实占位**，不是"假设人物
    是正方形"的估算：一个 1:3 的竖长人物按正方形估会漏判宽度。

    ``center`` 位置忽略边距（规格里它和四个角并列，边距对居中无意义）。

    放不下时**返回 None 而不是抛错**：贴图是装饰，配错了不该让整支片子出不来
    （调用方 :func:`plan_stickers` 会把原因记进 ``skipped_reason``）。
    """
    if asset.width_px is None or asset.height_px is None or asset.height_px <= 0:
        return None

    height = spec.height_px
    width = max(even_floor(round(height * asset.width_px / asset.height_px)), 2)
    margin_x, margin_y = spec.margin_px

    if spec.position == "center":
        x = even_floor((canvas_width - width) // 2)
        y = even_floor((canvas_height - height) // 2)
    else:
        x = margin_x if spec.position.endswith("left") else canvas_width - width - margin_x
        y = margin_y if spec.position.startswith("top") else canvas_height - height - margin_y

    if x < 0 or y < 0 or x + width > canvas_width or y + height > canvas_height:
        return None

    return StickerPlacement(x=x, y=y, width_px=width, height_px=height)


@dataclass(frozen=True, slots=True)
class _Speaking:
    """ "这一层怎么换图"的结论（内部类型 —— 只有 :func:`plan_stickers` 用）。

    单独一个类型而不是往 :class:`StickerPlan` 上摊五个字段，是为了让"换图"这件事
    在代码里也是一个**整体**：它要么整套成立（图可用 + 有区间），要么带着一条
    ``problem`` 整体不成立。摊平之后，迟早有人只看了其中一半。
    """

    asset: StickerAsset | None = None
    intervals: tuple[Interval, ...] = ()
    problem: str | None = None
    source: str = "none"
    note: str | None = None
    warnings: tuple[str, ...] = ()


def _resolve_speaking(
    spec: StickerSpec,
    asset: StickerAsset,
    *,
    speaking: SpeakingPlan | None,
) -> _Speaking:
    """这一层要不要换图、换在哪些区间、换不成是因为什么。

    **没配就一个字都不说**（``speaking_path`` 与 ``speaker`` 都空 ⇒ 空结论）：
    绝大多数层不换图，让每一层都挂一条"你没配讲话图"的说明，等于把面板淹掉。

    配了却换不成 ⇒ 必有一条 ``problem``。这一条是这次改动的重点：上一轮"贴图开了
    没反应、面板一个字都不说"就是这么来的（陷阱 222 / 223）。

    ``speaking is None``（调用方**压根没算**讲话区间）与"算了但这个人没词"是**两件
    事**，所以 :attr:`_Speaking.problem` 的措辞也分两份：前者是"这次没有拿到讲话
    区间"，后者才是"他没有讲话区间"。混成一句的话，一条没带稿子的调用会写下
    "这个人没讲话"这种**它并不知道**的结论 —— 面板与成片各说各话就是这么来的。
    """
    if spec.speaking_path is None and not spec.speaker:
        return _Speaking()

    source = speaking.source if speaking is not None else "none"
    # ``note`` 回答的是"**这份讲话计划为什么是空的**"，所以只在"这一层因此不换图"
    # 的那一刻才有值。这一层真的拿到了区间时留着 ``None`` —— 给一份有区间的计划挂一句
    # "没有可用的讲话区间"，manifest 里就成了自相矛盾的两行。
    note: str | None = None

    # 下面这一串 if 全部只**赋值**、不返回：七种"换不成"的理由各是一条 ``problem``，
    # 而结论只有一份。写成七个 return 的话，每加一条理由就要再抄一遍那五个字段。
    spk: StickerAsset | None = None
    problem: str | None = None
    intervals: tuple[Interval, ...] = ()
    warnings: tuple[str, ...] = ()

    # 配置与图这一层交给 ``speaking_problem``（面板读的是**同一个函数**）；
    # 它返回 ``None`` 才说明"配置与图都齐了"，剩下的唯一问题是"有没有词"。
    spk = probe_png(spec.speaking_path) if spec.speaking_path is not None and spec.speaker else None
    problem = speaking_problem(speaking_path=spec.speaking_path, speaker=spec.speaker, asset=spk)
    if problem is None:
        assert spk is not None  # problem 为 None ⇒ 上面那个条件必然成立
        warnings = aspect_warnings(asset, spk)
        intervals = speaking.intervals_for(spec.speaker) if speaking is not None else ()
        if not intervals:
            if speaking is None or speaking.source == "none":
                # 拿不到依据（面板首屏就是这么调的）⇒ 如实说"不知道"，**不是**"他没有词"。
                note = speaking.note if speaking is not None and speaking.note else "没有可用的讲话区间"
                problem = f"这一层代表 {spec.speaker}，但这次没有拿到讲话区间（{note}）⇒ 全程都是普通图"
            else:
                problem = f"这一层代表 {spec.speaker}，但这条片子里他没有讲话区间 ⇒ 全程都是普通图"

    return _Speaking(
        asset=spk, intervals=intervals, problem=problem, source=source, note=note, warnings=warnings
    )


def speaking_problem(
    *,
    speaking_path: Path | None,
    speaker: str,
    asset: StickerAsset | None,
) -> str | None:
    """这一层"换不成图"的原因 —— **只看配置与图，不看稿子**。

    两处调用、同一个判据：

    - 渲染路径（:func:`_resolve_speaking`）：拿到这一条后再往下问"这个人有没有词"；
    - 面板（``services/outputs_service.py``）：**只能问到这一步**。

    为什么必须共用一个函数而不是各写一份：面板与成片"各说各话"正是陷阱 223 记的那件事
    —— 面板说"讲话图没问题"，成片里人物从头到尾没换过图，而两边的判据差了一个
    ``elif``。判据搬到这里之后，改一处就是改两处。

    面板**问不出来**"这个人这条片子里有没有讲话区间"（那要看稿子与时间轴，只有渲染
    路径知道）⇒ 那句话**不在**这个函数里，由 :func:`_resolve_speaking` 自己补。

    :param speaking_path: 这一层配的讲话图（``None`` ⇒ 没配）
    :param speaker: 这一层代表谁（空串 ⇒ 没写）
    :param asset: 讲话图的实测结果；``None`` ⇒ 还没测（配置不全时不必测）
    """
    if speaking_path is None:
        return "没填讲话图（speaking_path）⇒ 这一层不换图"
    if not speaker:
        return "没写这一层代表谁（speaker）⇒ 不知道什么时候该换图"
    if asset is None or not asset.exists:
        return f"讲话图不在盘上：{speaking_path}"
    if not asset.usable:
        return f"讲话图不可用（{asset.problem}）"
    return None


def aspect_warnings(asset: StickerAsset, speaking: StickerAsset) -> tuple[str, ...]:
    """两张图宽高比对不上 ⇒ 一条 warning（**照常出片**）。

    换图**不改摆放**（见 :attr:`StickerSpec.speaking_path`）：两张图按同一个框缩放，
    宽高比不一致就会在开口那一瞬间把人物压扁。这是"看得见的错"，但它是**画风**问题、
    不是"贴不上"问题，所以记账照做，不拦。

    **公开**（不是 ``_`` 开头）是因为面板要用它：``services/outputs_service.py``
    也要在贴图卡片上写这句话。判据只有这一处 —— 面板与渲染各写一遍，早晚会漂
    （陷阱 223 就是这么来的）。
    """
    if not asset.usable or not speaking.usable:
        return ()
    if not (asset.width_px and asset.height_px and speaking.width_px and speaking.height_px):
        return ()
    want = asset.width_px / asset.height_px
    got = speaking.width_px / speaking.height_px
    if want <= 0 or abs(got / want - 1.0) <= 0.02:
        return ()
    return (
        f"讲话图与普通图宽高比不一致（{got:.3f} vs {want:.3f}）⇒ "
        "换图时人物会被压扁；两张图导出成同一尺寸即可",
    )


def plan_stickers(
    configs: Mapping[str, StickerConfig],
    *,
    canvas_width: int,
    canvas_height: int,
    home: Path,
    speaking: SpeakingPlan | None = None,
) -> tuple[StickerPlan, ...]:
    """贴图这一环的**唯一**判断点：每一层各自"能用 ⇒ 给出摆放；不能用 ⇒ 跳过 + 记原因"。

    返回**全部**槽位的结论（含被跳过的），顺序 = ``configs`` 的声明顺序。
    渲染路径只取 ``applied`` 的那几层；面板与 ``manifest.json`` 要的却是全部 ——
    "第 2 层为什么没贴上"是必须能回答的问题，而那个答案只在这里产生。

    返回全部而不是"只返回贴上的"还有一个理由：**叠放顺序**。调用方拿到的是声明顺序，
    过滤器图照着它串，于是"谁压在谁上面"由 YAML 的书写顺序决定 —— 那是一个人能看懂、
    也能自己调整的规则。

    :param speaking: 这一次渲染里"谁在什么时候讲话"（``render/speech.py``）。
        ``None`` ⇒ 所有层都不换图（老行为）。**它是一份独立的输入而不是从配置里读的**：
        讲话区间只跟稿子与时间轴有关，跟画布、跟贴图参数都无关 —— 把它塞进配置，
        等于让"谁在讲话"这件事有一个可以写错的地方。
    """
    plans: list[StickerPlan] = []
    for name, config in configs.items():
        resolved = resolve_sticker(config, name=name, canvas_height=canvas_height, home=home)
        spec = resolved.spec
        asset = probe_png(spec.image_path)

        if not spec.enabled:
            plans.append(
                StickerPlan(
                    spec=spec,
                    asset=asset,
                    skipped_reason="这一层在配置里是关的（enabled=False）",
                    warnings=resolved.warnings,
                )
            )
            continue
        if not asset.exists:
            plans.append(
                StickerPlan(
                    spec=spec,
                    asset=asset,
                    skipped_reason=f"贴图文件不存在，跳过：{spec.image_path}",
                    warnings=resolved.warnings,
                )
            )
            continue
        if not asset.usable:
            plans.append(
                StickerPlan(
                    spec=spec,
                    asset=asset,
                    skipped_reason=f"贴图不可用（{asset.problem}），跳过",
                    warnings=resolved.warnings,
                )
            )
            continue

        placement = place_sticker(spec, asset, canvas_width=canvas_width, canvas_height=canvas_height)
        if placement is None:
            plans.append(
                StickerPlan(
                    spec=spec,
                    asset=asset,
                    skipped_reason=(
                        f"贴图放不进画布：位置 {spec.position}、边距 {spec.margin_px}、"
                        f"高度 {spec.height_px}px、画布 {canvas_width}x{canvas_height}"
                    ),
                    warnings=resolved.warnings,
                )
            )
            continue

        # 换图这一路**只在真的贴上之后**才算：一层没贴上的时候再报一条"而且它也没换图"，
        # 是两句同义的话。已经贴在画面上的层才有"换不换图"这个问题。
        swap = _resolve_speaking(spec, asset, speaking=speaking)
        plans.append(
            StickerPlan(
                spec=spec,
                asset=asset,
                placement=placement,
                warnings=resolved.warnings + swap.warnings,
                speaking_asset=swap.asset,
                speaking_intervals=swap.intervals,
                speaking_problem=swap.problem,
                speaking_source=swap.source,
                speaking_note=swap.note,
            )
        )
    return tuple(plans)
