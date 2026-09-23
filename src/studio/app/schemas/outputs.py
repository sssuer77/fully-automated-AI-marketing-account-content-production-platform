"""合成配置契约（T4.7 · §04.2.8 / §04.5.9）。

面板要回答的三个问题，一个模型对一块
------------------------------------
① "现在这一档是什么参数？" ⇒ :class:`ProfileModel`（分辨率 / 帧率 / 质量 / 平台）
② "水印与字幕长什么样？" ⇒ :class:`WatermarkModel` / :class:`SubtitleModel`
③ "改完会不会把配置写坏？" ⇒ :class:`OutputsUpdateRequest`（服务端**校验不过一个字节都不写**）

请求体为什么全部 ``extra="forbid"``
-----------------------------------
与总览台 / 四池 / 选题 / 人物库同一条：把 ``margin_x`` 写成 ``marginx`` 被静默忽略，
人会以为"边距已经改好了"，而文件里一个字都没变 —— 这种"以为生效了"最贵。

为什么上下限跟着响应一起下发（:class:`OutputsLimitsModel`）
----------------------------------------------------------
上限的唯一真相是配置模型的字段约束（§04.5.9 裁定 161）。前端不抄第二份，它照着
``limits`` 画输入框：某天把 ``crf`` 的上限从 51 调到 63，面板跟着变，不需要改前端、
也不会出现"前端拦着、后端放行"。``exclusive_min`` / ``exclusive_max`` 必须一起给 ——
``width_ratio`` 是 ``gt=0.0``，画成"最小 0"会放行一个必然 422 的 0。

响应模型的集合字段一律**必填**（``x: list[T]`` 而非 ``Field(default_factory=list)``）：
后者在 JSON Schema 里既不进 ``required`` 也不带 ``default`` ⇒ 生成类型是
``T[] | undefined``，前端被迫到处 ``?? []``（裁定 135）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from studio.app.schemas.common import MAX_REASON

__all__ = [
    "BoundModel",
    "OutputsLimitsModel",
    "OutputsOutcomeModel",
    "OutputsResponse",
    "OutputsUpdateRequest",
    "ProfileBoundsModel",
    "ProfileModel",
    "ProfilePatch",
    "StickerBoundsModel",
    "StickerModel",
    "StickerPatch",
    "SubtitleBoundsModel",
    "SubtitleModel",
    "SubtitlePatch",
    "WatermarkBoundsModel",
    "WatermarkModel",
    "WatermarkPatch",
]


class _Body(BaseModel):
    """请求体基类（禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    """响应基类（字段全部显式声明，禁多字段）。"""

    model_config = ConfigDict(extra="forbid")


# ══════════════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════════════


class BoundModel(_Response):
    """一个数值字段的上下限（``None`` = 这一侧没约束）。

    ``exclusive_*`` 为真 ⇒ 边界本身**不合法**（``gt`` / ``lt`` 而不是 ``ge`` / ``le``）。
    """

    min: float | None
    max: float | None
    exclusive_min: bool
    exclusive_max: bool


class ProfileBoundsModel(_Response):
    """输出 profile 各字段的上下限。"""

    width: BoundModel
    height: BoundModel
    fps: BoundModel
    crf: BoundModel
    cq: BoundModel


class WatermarkBoundsModel(_Response):
    """水印各字段的上下限 + 位置枚举。"""

    positions: list[str]
    margin_x: BoundModel
    margin_y: BoundModel
    width_ratio: BoundModel
    opacity: BoundModel


class StickerBoundsModel(_Response):
    """人物贴图各字段的上下限 + 位置枚举（与水域共用同一份枚举）。"""

    positions: list[str]
    margin_x: BoundModel
    margin_y: BoundModel
    height_ratio: BoundModel
    opacity: BoundModel


class SubtitleBoundsModel(_Response):
    """字幕各字段的上下限。"""

    font_size: BoundModel
    outline: BoundModel
    #: 「距底」与「底部安全区」：两个都能改，真正生效的是两者的 max
    #: （``SubtitleModel.margin_v``，服务端算好一起下发）。
    margin_bottom: BoundModel
    safe_area_bottom: BoundModel
    max_chars_per_line: BoundModel


class OutputsLimitsModel(_Response):
    """输入框的上下限（从配置模型现取 · 裁定 161）。"""

    scalar_fields: list[str]
    profile_fields: list[str]
    watermark_fields: list[str]
    sticker_fields: list[str]
    subtitle_fields: list[str]
    default_profile: BoundModel
    profile: ProfileBoundsModel
    watermark: WatermarkBoundsModel
    sticker: StickerBoundsModel
    subtitle: SubtitleBoundsModel


class ProfileModel(_Response):
    """一档输出 profile。

    ``quality`` 是**抽象字段**：它落在文件里的 ``crf``（libx264）还是 ``cq``（NVENC）
    由 ``quality_field`` 说明。面板据此把标签写成"CRF"或"CQ"，用户不必知道这一档
    用的是哪种编码器。
    """

    name: str
    width: int
    height: int
    fps: int
    vcodec: str
    quality_field: str
    quality: int
    platforms: list[str]
    is_default: bool


class WatermarkModel(_Response):
    """固定水印（**可选装饰**）。``usable=false`` ⇒ 这次出片不贴水印，**照样出片**。

    ``exists``（盘上有这么个文件）与 ``usable``（渲染**真的会贴上**）是两件事，两个都下发：
    扩展名叫 ``.png`` 的 WebP / 没透明通道的 PNG 在盘上"存在"，渲染却会跳过它。
    只报前者的面板会让人对着一个绿点找半天"为什么片子上没有水印"。
    """

    path: str
    position: str
    margin_x: int
    margin_y: int
    width_ratio: float
    width_px: int
    opacity: float
    exists: bool
    usable: bool
    problem: str | None


class StickerModel(_Response):
    """一层人物贴图（T6.5）。``usable=false`` ⇒ 这一层不贴，**其余层照常**。

    ``height_px`` 是按**默认档**画布高算出来的像素高（与水域 ``width_px`` 同一条理由：
    面板上"多高"比"0.45"直观）。实际宽度由素材宽高比决定，所以这里不给 ——
    给一个"假设人物是正方形"的估算值，只会让人按一个假数字去调。

    ``usable`` / ``problem`` 的判据与渲染路径**同源**（都是 `render.png_probe.probe_png`）：
    面板说"会贴上"、渲染却跳过，是这一屏最贵的一种谎话（用户会去查 ffmpeg、查字体、
    查素材，唯独不会想到是面板骗了他）。

    ★ **换图（T6.5 追加）**：``speaker`` / ``speaking_path`` 是"这一层代表谁、他讲话时
    换成哪张图"，``speaking_usable`` / ``speaking_problem`` / ``speaking_warnings`` 是
    那一张图的结论 —— 三个判据与普通图那三个**完全同源**（同一份 `probe_png`），
    因为"讲话图坏了"与"普通图坏了"是同一种坏。

    代价如实说明：面板**判不了**"这个人这条片子里到底有没有讲话区间"（那要读稿子与
    时间轴）。所以这一屏的绿灯只说明"配齐了"，不说明"一定会换" —— 真的换没换、
    按哪份时间换的，写在成片的 `manifest.json` 的 `stickers[].speaking` 里。
    """

    name: str
    enabled: bool
    path: str
    #: 这一层代表稿子里的哪个说话人（空 ⇒ 不换图）
    speaker: str
    #: 讲话时换的那张图（相对 STUDIO_HOME；``None`` ⇒ 这一层不换图）
    speaking_path: str | None
    position: str
    margin_x: int
    margin_y: int
    height_ratio: float
    height_px: int
    opacity: float
    exists: bool
    usable: bool
    problem: str | None
    #: **讲话图**这一路能不能用（判据与 ``usable`` 一样，只是换了张图）
    speaking_usable: bool
    #: "换图换不成"的原因（**面板这一侧判得出来的那些**）。
    #:
    #: 面板判不了"有没有讲话区间"（见类注释），所以这里不编那一句：编了就是面板与
    #: 成片各说各话（陷阱 223）。
    speaking_problem: str | None
    #: "换得了图、但会难看"的条目（目前一条：两张图宽高比不一致 ⇒ 人物被压扁）。
    #: 与 :attr:`speaking_problem` 分开：一个是"换不成"，一个是"换成了但画风不对"。
    speaking_warnings: list[str]


class SubtitleModel(_Response):
    """字幕样式（Q11 开启）。

    ``margin_v`` 是**真正生效**的距底像素（= max(margin_bottom, safe_area_bottom)），
    由服务端算好一起下发 —— 面板据此在两者不一致时说明白"实际 N px（被底部安全区
    抬上来了）"。让面板自己再算一遍 max，就等于同一件事有两份口径（裁定 161 同源）。
    """

    enabled: bool
    font_name: str
    font_size: int
    outline: int
    shadow: int
    margin_bottom: int
    safe_area_bottom: int
    margin_v: int
    max_chars_per_line: int
    max_lines: int


class OutputsResponse(_Response):
    """一次拿全：profile 列表 + 水印 + 字幕 + 上下限。

    ``stale=true`` ⇒ 磁盘上那份现在读不出来，``error`` 给原因，``profiles`` 为空。
    这时 ``limits`` **照常下发**（它来自模型，不是文件）—— 面板仍能画出表单骨架。
    """

    generated_at: str
    version: int
    sha256: str
    loaded_at: str
    path: str
    stale: bool
    error: str | None
    default_profile: str
    profiles: list[ProfileModel]
    watermark: WatermarkModel | None
    stickers: list[StickerModel]
    subtitle: SubtitleModel | None
    limits: OutputsLimitsModel


# ══════════════════════════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════════════════════════


class ProfilePatch(_Body):
    """一档 profile 的改动（``None`` = **这次不动它**，与"清零"不是一回事）。"""

    width: int | None = None
    height: int | None = None
    fps: int | None = None
    quality: int | None = None


class WatermarkPatch(_Body):
    """水印改动。"""

    position: str | None = None
    margin_x: int | None = None
    margin_y: int | None = None
    width_ratio: float | None = None
    opacity: float | None = None


class StickerPatch(_Body):
    """一层贴图的改动（``None`` = **这次不动它**）。"""

    enabled: bool | None = None
    path: str | None = None
    #: 换图那两件（T6.5 追加）：换人物换到别人身上时，第一件事就是改这两个。
    #: 与 ``path`` 一样是**字符串**（不是 ``Path``）：写盘时统一转正斜杠。
    speaker: str | None = None
    speaking_path: str | None = None
    position: str | None = None
    margin_x: int | None = None
    margin_y: int | None = None
    height_ratio: float | None = None
    opacity: float | None = None


class SubtitlePatch(_Body):
    """字幕改动。

    ``safe_area_bottom`` 是**扁平名**，落到文件里是 ``subtitle.safe_area.bottom``
    （对照表在 ``core/outputs_store.SUBTITLE_NESTED``）：面板一个数一个框，没必要
    让它知道安全区底下还有 ``top`` / ``left`` / ``right`` 三个兄弟。
    """

    font_size: int | None = None
    outline: int | None = None
    margin_bottom: int | None = None
    safe_area_bottom: int | None = None
    max_chars_per_line: int | None = None


class OutputsUpdateRequest(_Body):
    """保存表单。

    校验的**唯一权威**是 `OutputsConfig`（服务层 ``model_validate``）：这里刻意不加
    ``ge`` / ``le`` 之类的约束 —— 抄一份到请求体，就会出现"请求体拦下了、但错误形状
    与 store 给的不一样"，面板于是要写两套红字逻辑。

    ``source_sha256`` 是**并发编辑**判据：把加载到的那一份指纹带回来，与盘上现状不符
    ⇒ 409 ``OUTPUTS_STALE``，一个字节都不写。
    """

    default_profile: str | None = None
    profiles: dict[str, ProfilePatch] | None = None
    watermark: WatermarkPatch | None = None
    stickers: dict[str, StickerPatch] | None = None
    subtitle: SubtitlePatch | None = None
    source_sha256: str | None = None
    reason: str | None = Field(default=None, max_length=MAX_REASON)

    def changes(self) -> dict[str, Any]:
        """只把**显式给了**的字段交出去。

        不这么做的话，"面板只改了字号、却把整份表单发上来"会把没动过的字段也重写一遍
        —— 而重写用的是**提交那一刻的旧值**，两个标签页同时开着就会互相覆盖。
        """
        payload: dict[str, Any] = {}
        if self.default_profile is not None:
            payload["default_profile"] = self.default_profile
        profiles: dict[str, Any] = {}
        for name, patch in (self.profiles or {}).items():
            fields = patch.model_dump(exclude_none=True)
            if fields:
                profiles[name] = fields
        if profiles:
            payload["profiles"] = profiles
        if self.watermark is not None:
            watermark = self.watermark.model_dump(exclude_none=True)
            if watermark:
                payload["watermark"] = watermark
        stickers: dict[str, Any] = {}
        for name, sticker_patch in (self.stickers or {}).items():
            fields = sticker_patch.model_dump(exclude_none=True)
            if fields:
                stickers[name] = fields
        if stickers:
            payload["stickers"] = stickers
        if self.subtitle is not None:
            subtitle = self.subtitle.model_dump(exclude_none=True)
            if subtitle:
                payload["subtitle"] = subtitle
        return payload


class OutputsOutcomeModel(_Response):
    """一次写动作的结果（``changed=false`` ⇒ 值与盘上完全一致）。"""

    action: str
    changed: bool
    version: int
    sha256: str
    path: str
    fields: list[str]
    note: str
