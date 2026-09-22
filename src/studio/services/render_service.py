"""渲染服务：文案 → 配音 → 合成出片（T3.x 的**可跑通主线**）。

一句话：把"一段口播文本"变成"一个 ``final.mp4``"。

六步（每一步的失败口径都写清楚）
--------------------------------
```text
① 配音   tts.synth.synthesize_script       文本 → voice_master.wav（逐句可续传 + 逐句实测时长）
② 选底片 render.assets.pick_parkour_clip   随机挑一条跑酷 mp4（挑不到 ⇒ **黑屏降级**）
         └─ 库里被**停用**的从候选里剔除（`disabled=` 注入，见下）
③ 定参数 render.profiles.resolve_profile   profile → 编码 argv
         render.watermark.plan_watermark    水印有就贴、没有就跳过
         render.subtitle.plan_subtitle      字幕：句级时长 → ASS；字体缺失就跳过
④ 合成   render.degrade.deliver            两遍响度 + 一条 ffmpeg 命令出 final.mp4
                                            失败 ⇒ 换 720P 保底档再试一次（T3.7）
⑤ 留痕   manifest.json / timeline.json     这条片子是用什么渲的（可复现 + composite_hash）
⑥ 质检   mixdown.measure_file → quality_report   量**落盘的成片**，回填 tasks.quality_json
```

**命中整片级缓存时少跑两步**：`render/cache.py`（§04.2.8.7）在合成前先比一次 `composite_hash`
—— 命中就跳过 ④ 合成与 ⑥ 的成片响度测量（读数从上一轮 manifest 里读回来），直接把盘上
那一支当本次交付。判据是「这个文件是这批输入渲出来的」，**不是**「文件在不在」；
理由写在那个模块的开头。

什么算**真**失败，什么算"降级 / 跳过"
------------------------------------
- 真失败：**没人声**（配音没产出音频）、ffmpeg 报错且保底档也失败；
  质检量不出响度**不算**（它是报告，不是出片的前提 —— 只记一句 warn）；
- 降级（照常出片，但置 ``degraded``）：**没有底片** ⇒ 纯黑底（§04.2.8.6）、
  正常档编码失败 ⇒ 720P 保底档；
- 跳过（连 ``degraded`` 都不置）：水印缺失 / 不可用、BGM 目录为空、字幕字体缺失 ——
  它们只是少一层装饰，观众根本看不出来。

这条"装饰品不阻塞主链路"的口径是**刻意**的：早先把水印做成硬门禁，后果是
``templates/`` 下没有那张 PNG 时一支片子都出不来。字幕、BGM 走同一条路。

**没有底片**在 T3.7 从"真失败"挪进了"降级"，理由只有一条：**无人值守**。素材是人工
收集的、随时可能被清空或全部禁用，而"今天没有底片"不该等于"今天出不了片"——纯黑底配
字幕仍是一条能发的片子（画质差，但不是废品）。这是本项目第三次把"看起来该硬"的门禁
降成"留痕 + 继续"（前两次：水印、字体）。

为什么服务层放在 `services/` 而不是 `render/`
--------------------------------------------
`render/` 只认 `core/` 与 `domain/`（§02.1 的依赖方向），它不该知道"配音"这件事。
把 tts 与 render 缝在一起是**服务层**的职责 —— 它本来就可以依赖两边。
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from studio.core.clock import now_iso
from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.media import probe_media
from studio.core.paths import StudioPaths
from studio.domain.models import QualityReport
from studio.render.assets import pick_bgm, pick_parkour_clip
from studio.render.cache import reusable_output
from studio.render.composite import (
    PROGRESS_TOTAL,
    CompositeRequest,
    CompositeResult,
    duration_ms_for,
)
from studio.render.degrade import NO_BROLL, deliver
from studio.render.hashing import composite_hash, input_digests
from studio.render.mixdown import LoudnessMeasurement, MixSettings, measure_file
from studio.render.profiles import FALLBACK_PROFILE_NAME, resolve_profile
from studio.render.subtitle import Cue, SubtitlePlan, build_cues, plan_subtitle
from studio.render.watermark import plan_watermark
from studio.services.asset_service import DisabledAssets
from studio.tts.segmenter import split_for_tts
from studio.tts.synth import VoiceResult, synthesize_script
from studio.tts.timeline import TimelineSource, build_timeline
from studio.tts.timeline import write_timeline as write_timeline_json

__all__ = [
    "ProduceRequest",
    "ProduceResult",
    "produce_video",
    "quality_report",
    "resolve_cues",
    "reuse_or_synthesize_voice",
    "write_timeline",
]

#: 进度回调：(阶段名, 已完成, 总数, 说明)
ProgressSink = Callable[[str, int, int, str], None]


@dataclass(frozen=True, slots=True)
class ProduceRequest:
    """一次出片的请求。"""

    task_id: str
    text: str
    profile_name: str | None = None
    voice: str | None = None
    reuse_voice: bool = False
    threads: int | None = None
    #: 固定"随机挑素材"的种子（复现某一条片子时用；None = 真随机）
    seed: int | None = None
    #: 要不要烧字幕。``None`` = 听 ``config/outputs.yaml → subtitle.enabled``
    #: （三态而不是 bool：面板不传时不该把配置里的开关顶掉）
    subtitle: bool | None = None
    #: 无视整片级缓存，强制重跑一遍 ffmpeg（``render/cache.py``）。
    #: 平时**不该**用它：命中缓存意味着输入一个字节都没变，重渲出来的还是同一支片子。
    #: 留给"我怀疑盘上那支是坏的"这种排查场景。
    force_render: bool = False
    #: 稿件**逐句**正文（``script_sentences``）。给了它，配音就不再自己切句 ——
    #: 与配音池读的是同一份句子（真机：重切会得到另一个句数，字幕从此与音频对不上）。
    #: 空 ⇒ 退回「把 ``text`` 切一遍」的老口径（CLI 直接给文案那条路）。
    sentences: tuple[str, ...] = ()
    #: 逐句音色（与 ``sentences`` 同序；渲染这条路按角色解析出来的，见
    #: ``voice_service.active_script_voices``）。空 ⇒ 整篇用 ``voice`` 那一个嗓子。
    sentence_voices: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class ProduceResult:
    """一次出片的结论（也是 CLI 与面板的读数）。"""

    task_id: str
    final: Path
    #: 画面来源。``None`` ⇒ 这次是**黑屏降级**（没有底片，用了纯黑底）
    clip: Path | None
    bgm: Path | None
    voice: VoiceResult | None
    voice_duration_ms: int
    duration_ms: int
    size_bytes: int
    watermark_enabled: bool
    watermark_skipped_reason: str | None
    profile_name: str
    manifest: Path
    timeline: Path
    composite: CompositeResult
    subtitle: SubtitlePlan
    #: ``broll`` / ``black``（与 :attr:`clip` 同源，但面板不必自己推）
    bg_fill: str
    #: 合成指纹（§04.2.8.7）—— "这条片子是不是同一支"的判据
    composite_hash: str
    #: 这次出片有没有走降级（黑屏 / 720P）。**跳过装饰不算降级**
    degraded: bool
    degrade_reason: str | None
    #: 试过哪几档、各自什么下场（`["douyin_1080x1920_30fps_v1: RENDER_FAILED"]`）
    attempts: tuple[str, ...]
    #: 这一次**没有**跑 ffmpeg，而是复用了盘上同哈希的那一支（§04.2.8.7）
    reused: bool = False
    #: **成片自己**的响度读数（`mixdown.measure_file` 量的那一支；量不出来 ⇒ None）。
    #: 注意它与 ``composite.loudness`` 不是一回事：后者是 loudnorm **归一化之前**的输入读数。
    output_loudness: LoudnessMeasurement | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "final": self.final.as_posix(),
            "clip": self.clip.as_posix() if self.clip else None,
            "bgm": self.bgm.as_posix() if self.bgm else None,
            "voice_duration_ms": self.voice_duration_ms,
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "profile": self.profile_name,
            "watermark_enabled": self.watermark_enabled,
            "watermark_skipped_reason": self.watermark_skipped_reason,
            "subtitle": self.subtitle.to_dict(),
            "voice": self.voice.to_dict() if self.voice else None,
            "manifest": self.manifest.as_posix(),
            "timeline": self.timeline.as_posix(),
            "bg_fill": self.bg_fill,
            "composite_hash": self.composite_hash,
            "reused": self.reused,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "attempts": list(self.attempts),
            "output_loudness": self.output_loudness.to_dict() if self.output_loudness else None,
            "warnings": list(self.warnings),
            "composite": self.composite.to_dict(),
        }


def reuse_or_synthesize_voice(
    request: ProduceRequest,
    *,
    paths: StudioPaths,
    on_progress: ProgressSink | None = None,
) -> VoiceResult | None:
    """配音这一步：能复用已有的 ``voice_master.wav`` 就不重跑。

    ``reuse_voice=True`` 且母带已存在 ⇒ 直接量时长、返回 ``None``（表示"没有新产物"）。
    调参阶段反复重渲时，这一条能把每次迭代从"几十秒配音"压到"只跑渲染"。
    """
    master = paths.voice_master(request.task_id)
    if request.reuse_voice and master.is_file() and master.stat().st_size > 0:
        if on_progress is not None:
            on_progress("voice", 1, 1, f"复用已有母带：{master.name}")
        return None

    def report(index: int, total: int, sentence: str) -> None:
        if on_progress is not None:
            on_progress("voice", index, total, sentence)

    return synthesize_script(
        request.text,
        sentences=request.sentences or None,
        voices=request.sentence_voices or None,
        paths=paths,
        task_id=request.task_id,
        voice=request.voice,
        on_progress=report,
    )


def resolve_cues(
    request: ProduceRequest,
    *,
    paths: StudioPaths,
    voice: VoiceResult | None,
    timeline_sentences: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[Cue, ...]:
    """这一次渲染的字幕事件（**只从实测时长来**）。

    三条来源，按可靠性排：

    1. ``voice`` 非空（这一次真的合成了）⇒ 用它刚量出来的逐句时长，最准；
    2. 复用了母带 + 有 ``timeline.json`` ⇒ 用上次记下的时长（同一个母带，数值不变）；
    3. 复用母带 + 没有 timeline ⇒ **就地重新量一遍**盘上的 ``sNNN.wav``
       （逐句音频可能已被 24h 保留期回收 ⇒ 量不到 ⇒ 退到"没有字幕"）。

    三条都拿不到 ⇒ 返回空元组 ⇒ 字幕被跳过（而不是"按字数估一个时长"）。估出来的
    时长会让字幕与声音错位，而错位比没有字幕更糟 —— 观众会以为配音配错了。
    """
    if voice is not None:
        return build_cues(voice.sentences, voice.sentence_durations_ms)

    if timeline_sentences:
        sentences = [str(row.get("text", "")) for row in timeline_sentences]
        durations = [int(row.get("duration_ms", 0)) for row in timeline_sentences]
        if sentences and len(sentences) == len(durations) and all(d > 0 for d in durations):
            return build_cues(sentences, durations)

    if not request.text.strip():
        return ()

    sentences = split_for_tts(request.text)
    if not sentences:
        return ()
    durations = [
        _probe_ms(paths.sentence_wav(request.task_id, index)) for index in range(1, len(sentences) + 1)
    ]
    if any(duration <= 0 for duration in durations):
        return ()
    return build_cues(sentences, durations)


def _probe_ms(path: Path) -> int:
    """量一个文件的时长；量不到 ⇒ ``0``（**不抛**）。

    为什么吞掉异常：逐句音频的保留期只有 24 小时（``gc/policy.py``），而母带有 24 小时
    之外的复用路径。于是"复用配音 + timeline.json 也丢了 + 逐句音频已被回收"是一个
    **真实存在**的状态 —— 此时正确的行为是"这条片子没字幕"，而不是"这条片子渲不出来"。
    装饰品不该阻塞主链路，字幕与水印同一条口径。
    """
    try:
        return max(0, probe_media(path).duration_ms)
    except StudioError:
        return 0


def write_timeline(
    *,
    paths: StudioPaths,
    task_id: str,
    cues: Sequence[Cue],
    voice_master: Path,
    tail_ms: int,
) -> Path:
    """句级时间轴 ``timeline.json``（§04.2.7）—— 出片这一步的**兜底**写法。

    正常情况下它由配音阶段（``voice_service.settle_voice``）写；这里只在"还没有
    时间轴"时补一份，见 :func:`produce_video`。两条纪律（陷阱 #108）：

    1. **形状只有一份**。这里不自己拼 JSON，而是交给
       :func:`studio.tts.timeline.build_timeline` + ``write_timeline`` —— 与配音
       阶段写出来的是同一种文件。以前这里手写一份"少几个字段"的 JSON，于是同一个
       路径下躺着两种形状，读的人得同时容忍两种。
    2. **不覆盖已有的那一份**（由调用方判断）。出片这一步的 cue 是从盘上的逐句音频
       **反推**的：没有句间停顿、没有抖动种子。拿它去覆盖配音阶段那份精确的时间轴，
       等于把"停顿 350ms"改回"0ms" —— 字幕与画面整体错位，而且**不会报错**。

    ``scenes[]`` 与 ``duration_policy`` 校验是**二期**的事，这里不写 —— 单遍合成
    没有场景概念，凭空造一个空数组只会让下游以为"场景算出来是空的"。
    """
    timeline = build_timeline(
        [
            TimelineSource(
                sentence_id=f"{task_id}-s{index:03d}",
                seq=index,
                speaker="",
                text=cue.text,
                audio=paths.sentence_wav(task_id, index),
                duration_ms=max(0, cue.end_ms - cue.start_ms),
                base_pause_ms=0,
            )
            for index, cue in enumerate(cues, start=1)
        ],
        task_id=task_id,
        # 兜底这一份没有任务种子可用（这里读不到库），拿 task_id 顶上 ——
        # 它本来也只写 base_pause_ms=0，抖动不参与。
        seed=task_id,
        tail_ms=tail_ms,
        data_dir=paths.data_dir,
        voice_master=voice_master,
    )
    return write_timeline_json(timeline, paths.timeline_json(task_id))


def _read_timeline(paths: StudioPaths, task_id: str) -> list[Mapping[str, Any]] | None:
    """读上一轮的 ``timeline.json``；读不出来 / 结构不对 ⇒ ``None``（**不抛**）。

    容错的理由：这份文件是"锦上添花"的时长来源，读坏了应当退回"重新量一遍盘上的
    逐句音频"，而不是让整支片子渲不出来。
    """
    target = paths.timeline_json(task_id)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = payload.get("sentences")
    if not isinstance(rows, list) or not rows:
        return None
    return [row for row in rows if isinstance(row, dict)]


def produce_video(
    request: ProduceRequest,
    *,
    paths: StudioPaths,
    outputs: OutputsConfig | None = None,
    outputs_source: Path | None = None,
    on_progress: ProgressSink | None = None,
    disabled: DisabledAssets | None = None,
) -> ProduceResult:
    """文案 → 配音 → 合成 → ``final.mp4``（外加一份 ``manifest.json``）。

    :param disabled: 库里被**停用**的素材文件名（``asset_service.disabled_assets``）。
        面板上点了「停用」，出片就不该再挑到它 —— T4.8 的验收写着这一条，而落地点
        在这里：调用方查库、这里转手交给 :mod:`studio.render.assets` 做集合减法。
        ``None`` ⇒ 一个都不排除（**退回"能进目录就算数"的老口径**，CLI 与旧调用方
        不受影响）。为什么不在这里自己开连接：这一步跑在渲染线程里，而"查库"是
        调用方的既有职责（它本来就在读生效稿件）。
    """
    source = outputs_source or (paths.config_dir / "outputs.yaml")
    config = outputs if outputs is not None else load_outputs_config(source)
    rng = random.Random(request.seed) if request.seed is not None else None

    reused_timeline = _read_timeline(paths, request.task_id) if request.reuse_voice else None
    voice = reuse_or_synthesize_voice(request, paths=paths, on_progress=on_progress)
    voice_master = paths.voice_master(request.task_id)
    if not voice_master.is_file() or voice_master.stat().st_size == 0:
        raise StudioError(
            f"配音没有产出母带：{voice_master}",
            code=ErrorCode.RENDER_FAILED,
            context={"task_id": request.task_id, "voice_master": voice_master.as_posix()},
            remediation="先跑 `studio render make`（不带 --reuse-voice）生成配音",
        )

    voice_ms = probe_media(voice_master).duration_ms
    duration_ms = duration_ms_for(voice_ms, tail_ms=config.audio.tail_ms)

    if on_progress is not None:
        on_progress("assets", 0, 1, "挑底片 / BGM")

    # 降级链自己产生的说明（与合成器的 warnings 汇总在一处，见下）
    warnings_seed: list[str] = []
    if voice is not None:
        # 音色退回（「这个音色这台引擎念不出来」）必须让人看得见：库里写着换音色
        # 成功、听起来却是另一个嗓子时，这句话是唯一的线索。
        warnings_seed.extend(voice.warnings)

    # 挑不到底片不再是失败：`clip=None` 会让合成器现造一块纯黑（§04.2.8.6）。
    # 被停用的从候选里剔除（`disabled=None` ⇒ 不排除任何东西）。
    clip = pick_parkour_clip(paths, rng=rng, exclude=None if disabled is None else disabled.clips)
    bgm = (
        pick_bgm(paths, rng=rng, exclude=None if disabled is None else disabled.bgm)
        if config.bgm.enabled
        else None
    )

    profile = resolve_profile(config, request.profile_name)
    watermark = plan_watermark(
        config.watermark,
        canvas_width=profile.width,
        canvas_height=profile.height,
        home=paths.home,
    )

    cues = resolve_cues(request, paths=paths, voice=voice, timeline_sentences=reused_timeline)
    timeline = paths.timeline_json(request.task_id)
    if cues and not timeline.is_file():
        # 还没有时间轴才补一份（配音阶段已经写过的话，那一份更准 —— 它有句间停顿
        # 与抖动种子，覆盖它会让字幕整体错位，而且不会报错）。
        timeline = write_timeline(
            paths=paths,
            task_id=request.task_id,
            cues=cues,
            voice_master=voice_master,
            tail_ms=config.audio.tail_ms,
        )

    subtitle_config = config.subtitle
    if request.subtitle is not None:
        # 面板显式传了开关就听它的（三态：None 才是"听配置"）。
        subtitle_config = subtitle_config.model_copy(update={"enabled": request.subtitle})
    subtitle = plan_subtitle(
        subtitle_config,
        cues,
        ass_path=paths.subtitle_ass(request.task_id),
        templates_dir=paths.templates_dir,
        canvas=profile.canvas,
        title=request.task_id,
    )

    if on_progress is not None:
        detail = f"{profile.name} · {duration_ms}ms"
        if subtitle.enabled:
            detail += f" · 字幕 {len(cues)} 句"
        on_progress("render", 0, PROGRESS_TOTAL, detail)

    composite_request = CompositeRequest(
        profile=profile,
        clip=clip,
        voice=voice_master,
        output=paths.final_video(request.task_id),
        duration_ms=duration_ms,
        watermark=watermark,
        bgm=bgm,
        subtitle=subtitle.ass_path,
        subtitle_font_dir=subtitle.font_dir,
        mix=MixSettings.from_config(config.audio),
        threads=request.threads,
        graph_path=paths.graphs_dir_for(request.task_id) / "composite.txt",
    )
    # 720P 保底档：正常档编码失败时换它再试一次（§04.2.8.6）。
    # 配置里没有这个档 ⇒ 不做回退并记一句 warn，而不是把"配置缺项"升级成"出不了片"——
    # 那正是这条降级链想避免的事。
    fallback_profile = None
    fallback_output = None
    try:
        fallback_profile = resolve_profile(config, FALLBACK_PROFILE_NAME)
    except StudioError as error:
        warnings_seed.append(
            f"配置里没有 720P 保底档（{FALLBACK_PROFILE_NAME}）：{error.message}；正常档失败时不会回退"
        )
    else:
        fallback_output = paths.final_video(request.task_id, degraded=True)

    # ── 整片级缓存（§04.2.8.7）：同样的输入 ⇒ 盘上那一支就是答案 ──
    # 指纹在这里先算一遍，而且把 digests 传进去：下面"真正渲出来那一支"还要再算一次
    # （档位可能被降级换掉），两次的输入文件完全相同 —— 底片几十 MB，别为它读两遍盘。
    digests = input_digests(composite_request)
    cached = (
        None
        if request.force_render
        else reusable_output(
            manifest=paths.manifest_json(request.task_id),
            plan_hash=composite_hash(composite_request, digests=digests),
        )
    )

    if cached is not None:
        composite = cached.composite
        final = cached.output
        plan_hash = cached.plan_hash
        profile_name = cached.profile_name
        output_loudness = cached.output_loudness
        degraded = cached.degraded
        degrade_reason = cached.degrade_reason
        attempts = cached.attempts
        # 上一轮那几条说明**原样沿用**：它们描述的是同一支片子，重写一遍不该改内容。
        # （本次新增的只有下面 manifest 里的 `reused` / `rendered_at` 两个字段。）
        warnings: list[str] = list(cached.warnings)
        if on_progress is not None:
            on_progress("render", PROGRESS_TOTAL, PROGRESS_TOTAL, f"命中渲染缓存，复用 {final.name}")
    else:
        delivery = deliver(
            composite_request,
            fallback_profile=fallback_profile,
            fallback_output=fallback_output,
            on_progress=on_progress,
        )
        composite = delivery.composite
        final = composite.output
        # 指纹描述的是**真正渲出来那一支**，所以用生效后的档位重建请求（输出路径不进哈希）。
        plan_hash = composite_hash(replace(composite_request, profile=delivery.profile), digests=digests)
        # QC 量的是**落盘的这一支**，不是 loudnorm 的输入读数（见 `mixdown.measure_file`）。
        output_loudness = measure_file(final, settings=composite_request.mix)
        profile_name = delivery.profile.name
        degraded = delivery.degraded or clip is None
        degrade_reason = delivery.degrade_reason or (NO_BROLL if clip is None else None)
        attempts = delivery.attempts
        # 降级说明汇总成一处：面板只读这个数组，不必自己拼四种来源。
        warnings = list(warnings_seed) + list(delivery.warnings)
        if clip is None:
            warnings.append("没有跑酷底片，这次用纯黑底出片（黑屏降级）")
        if subtitle.note:
            warnings.append(subtitle.note)
        if output_loudness is None:
            warnings.append("成片响度没量出来：quality_json 里的 lufs / true_peak 会是空的")

    manifest = _write_manifest(
        request=request,
        paths=paths,
        source=source,
        composite=composite,
        clip=clip,
        bgm=bgm,
        voice=voice,
        voice_ms=voice_ms,
        watermark_enabled=composite.watermark_applied,
        watermark_skipped_reason=watermark.skipped_reason,
        profile_name=profile_name,
        subtitle=subtitle,
        timeline=timeline,
        warnings=warnings,
        plan_hash=plan_hash,
        degraded=degraded,
        degrade_reason=degrade_reason,
        attempts=attempts,
        output_loudness=output_loudness,
        reused=cached is not None,
        rendered_at=cached.rendered_at if cached is not None else None,
    )

    return ProduceResult(
        task_id=request.task_id,
        final=final,
        clip=clip,
        bgm=bgm,
        voice=voice,
        voice_duration_ms=voice_ms,
        duration_ms=duration_ms,
        size_bytes=composite.size_bytes,
        watermark_enabled=composite.watermark_applied,
        watermark_skipped_reason=watermark.skipped_reason,
        profile_name=profile_name,
        manifest=manifest,
        timeline=timeline,
        composite=composite,
        subtitle=subtitle,
        bg_fill=composite.bg_fill,
        composite_hash=plan_hash,
        reused=cached is not None,
        degraded=degraded,
        degrade_reason=degrade_reason,
        attempts=attempts,
        output_loudness=output_loudness,
        warnings=tuple(warnings),
    )


def quality_report(result: ProduceResult) -> QualityReport:
    """一次出片的读数 → ``tasks.quality_json``（§03.5.3）。

    三条口径写在这里，免得别处再各推一遍：

    1. **响度/峰值取成片自己的读数**（:attr:`ProduceResult.output_loudness`），
       不是 ``composite.loudness`` —— 后者是 loudnorm 归一化**之前**的输入值。
       发布门禁（§06.4）量的是发布出去的那个文件，QC 就得报同一个东西。
    2. ``av_sync_offset_ms`` **恒为 None**：一期不做音画同步（C12），字段留着是给二期
       和 QC 报告用的。写 0 会被读成"同步误差是 0"——那是**谎报**，比留空更糟。
    3. ``phash_distance_avg`` / ``dup_audit_pass`` 同样留空：相似度审计
       （``scripts/dup_audit.py`` · §04.2.4.5）一期未落地，理由见 T3.7 清单。
       ``dup_audit_pass=None`` 与 ``False`` 是两件事：前者是"没审"，后者是"审了没过"。
    """
    loudness = result.output_loudness
    return QualityReport(
        lufs=round(loudness.input_i, 2) if loudness else None,
        true_peak=round(loudness.input_tp, 2) if loudness else None,
        # 水印判据必须落库（T5.1）：发布前二次校验的门禁 1 只认它。
        # 不写的话，门禁只能去读 manifest.json，而任务一旦重合成
        # （旧成片被新名覆盖），两者就不再是同一个事实。
        watermark_applied=result.watermark_enabled,
        degraded=result.degraded,
        degrade_reason=result.degrade_reason,
    )


def _write_manifest(
    *,
    request: ProduceRequest,
    paths: StudioPaths,
    source: Path,
    composite: CompositeResult,
    clip: Path | None,
    bgm: Path | None,
    voice: VoiceResult | None,
    voice_ms: int,
    watermark_enabled: bool,
    watermark_skipped_reason: str | None,
    profile_name: str,
    subtitle: SubtitlePlan,
    timeline: Path,
    warnings: list[str],
    plan_hash: str,
    degraded: bool,
    degrade_reason: str | None,
    attempts: tuple[str, ...],
    output_loudness: LoudnessMeasurement | None,
    reused: bool,
    rendered_at: str | None,
) -> Path:
    """把"这条片子是怎么渲出来的"落盘（复现、排障、以及整片级缓存都读它）。

    :param reused: 这一次**没有**跑 ffmpeg，而是复用了盘上同哈希的那一支
    :param rendered_at: 那一支成片**渲出来的**时刻。复用命中时它比 ``created_at`` 早 ——
        两者不是一回事：``created_at`` 是这份 manifest 的写入时间。
    """
    created_at = now_iso()
    payload: dict[str, Any] = {
        "task_id": request.task_id,
        "created_at": created_at,
        "outputs_source": source.as_posix(),
        "profile": profile_name,
        "final": composite.output.as_posix(),
        "duration_ms": composite.duration_ms,
        "size_bytes": composite.size_bytes,
        # 黑屏降级时没有底片 —— 写 null 而不是编一个假路径：下游（重复度审计、
        # 素材使用统计）看到 null 才知道"这条片子的画面不是素材给的"。
        "clip": clip.as_posix() if clip else None,
        "bg_fill": composite.bg_fill,
        "bgm": bgm.as_posix() if bgm else None,
        "voice_master": paths.voice_master(request.task_id).as_posix(),
        "voice_duration_ms": voice_ms,
        "voice": voice.to_dict() if voice else None,
        "watermark": {
            "enabled": watermark_enabled,
            "skipped_reason": watermark_skipped_reason,
        },
        "subtitle": subtitle.to_dict(),
        "timeline": timeline.as_posix(),
        # 两个响度**都要留**，它们的名字必须能一眼分清：
        # ``loudness`` = loudnorm 第一遍量到的**输入**（归一化的起点），
        # ``output_loudness`` = 成片落盘后量的**成品**（发布门禁读的就是它）。
        # 只留一个的话，下一个人一定会拿输入值当成品值用。
        "loudness": composite.loudness.to_dict() if composite.loudness else None,
        "output_loudness": output_loudness.to_dict() if output_loudness else None,
        # 合成指纹（§04.2.8.7）：同样的输入 ⇒ 同样的值。"这条片子是不是同一支"、
        # "换稿之后有没有真的重剪"，都靠这一行回答。
        "composite_hash": plan_hash,
        # 复用的留痕：`reused=true` + 比 created_at 早的 rendered_at。
        # 缺了这两行，"这一支是本次渲的还是上次留下的"就只能靠猜。
        "reused": reused,
        "rendered_at": rendered_at or created_at,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "attempts": list(attempts),
        "warnings": warnings,
        "argv": list(composite.argv),
    }
    target = paths.manifest_json(request.task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
