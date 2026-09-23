"""配音操作面契约（T2.9 · §04.3 / T4.5）。

面板上这一块要回答三个问题，一个模型对一块
------------------------------------------
① "这条任务配到哪一步了？" ⇒ :class:`SentenceVoiceList`（逐句状态 + 进度 + 可播 url）
② "重配这一句之后会怎样？" ⇒ :class:`ResynthResponse`（回到 ``pending`` + 时间轴已过期）
③ "换音色要付什么代价？" ⇒ :class:`VoiceMapRequest` / :class:`VoiceMapResponse`
   （``confirm`` 与 ``affected`` 是一对：服务端先算代价，面板据此弹确认框）
④ "待配音那一步谁来推？" ⇒ :class:`EnqueueVoiceResponse`
   （``queued_voice`` 只是个状态：把 54 句投进 voice 池的是这个端点）

为什么 ``audio_url`` 由服务端拼
-------------------------------
url 的形状（``/api/v1/media/voice/<task_id>/s00N.wav``）是**后端**的事。让面板自己拼，
等于把"文件落在哪"这条知识抄进前端 —— 而它一改（§2.3 的目录调整）就会变成
"点播放没反应"，且不报错。没有音频时给 ``null``：不要发一个注定 404 的 url。

为什么试听 url 里带的是 ``task_id + seq`` 而不是句子 id
-------------------------------------------------------
它指的是**盘上那份文件**，而文件名由 ``seq`` 决定（``s00N.wav``，§04.3.3 不变量 1）。
带句子 id 的话，url 与文件名对不上，排查"这一句播的到底是哪个文件"要跳两次。

``can_resynth`` 为什么由服务端算
--------------------------------
"这一句现在能不能重配"的判据（``synthesizing`` 不能动，见
``SentenceRepo.invalidate``）是**服务端**的规则。发给面板一个布尔值，比让前端
记住"哪些状态不能点"可靠 —— 规则改一次就漏一处，而漏的那一处会变成
"点了重配没反应"。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from studio.db.models import SentenceRow
from studio.db.repositories.sentence_repo import SKIPPED_STATUS, SYNTHESIZING_STATUS

__all__ = [
    "ENQUEUE_HINT",
    "NO_VOICE_HINT",
    "PROFILE_UNSPEAKABLE_HINT",
    "TIMELINE_HINT",
    "EnqueueVoiceResponse",
    "ResynthResponse",
    "SentenceProgressModel",
    "SentenceVoice",
    "SentenceVoiceList",
    "VoiceChangeModel",
    "VoiceMapRequest",
    "VoiceMapResponse",
    "VoiceOption",
    "VoiceOptions",
    "VoicePreview",
    "progress_view",
    "sentence_voice_view",
]

#: 重配之后时间轴怎么回来 —— 三处响应里说的是同一句话（面板直接显示它）
TIMELINE_HINT: str = (
    "重配完成后跑一次配音收口（studio pipeline run <task_id> --until queued_render）"
    "会让时间轴**全量重算**：成片时长以那一版为准"
)

#: 一个音色都没有时的提示（与渲染面板 `_NO_VOICE_HINT` 同一条口径）
NO_VOICE_HINT: str = (
    "本机没有可用的音色：系统音色在「设置 → 时间和语言 → 语音」里装一个中文语音包"
    "（如 Microsoft Huihui），参考音色走 studio assets ingest --kind voice"
)

#: 候选里有"念不出来"的参考音时的提示（R2 解耦的另一半：**能登记 ≠ 能发声**）。
#:
#: 参考音入库与"这台引擎念得出来"是两件事：``voice_profiles`` 里的 id 是**零样本
#: 参考音**的名字，要 CosyVoice 才能念。**现在它们大多数时候都念得出来**（E5 权重已就位，
#: 常驻服务就是当前引擎）；变回不可用只有一种情形 —— **常驻服务没起**（模型还在加载、进程挂了），
#: 引擎退回系统语音包。这时它们仍然该出现在候选里（用户要知道自己入库的东西还在），
#: 但**选了也发不出声** —— 而失败的样子是"每一句连失败 3 次、降级成静音"，
#: 成片没人声，库里却写着"换音色成功"。
#: 所以这句话必须挂在下拉框旁边，不能只写在文档里。
PROFILE_UNSPEAKABLE_HINT: str = (
    "带「当前引擎念不出来」的那些是已入库的**参考音**：零样本复刻要 CosyVoice。"
    "**常驻推理服务没起**时引擎会退回系统语音包，这时它们念不出来；"
    "真选了也不会没声 —— 配音会改用当前引擎的兜底音色（裁定 314）"
)


class SentenceProgressModel(BaseModel):
    """逐句进度（``SentenceProgress.to_dict()`` 的展示模型）。"""

    total: int
    pending: int
    synthesizing: int
    done: int
    skipped: int
    failed: int
    settled: int
    ratio: float


class VoicePreview(BaseModel):
    """一个音色的试听样本现在什么样（T2.4）。

    ``status`` **五态**：``missing``（还没生成，点一下就生成）· ``running``（正在生成，
    真机上一次十几秒）· ``ready``（盘上有，可以播）· ``failed``（生成失败了，
    ``error`` 里是引擎原话）· ``stale``（盘上有，但那是**上一版参考音**念的 ——
    同名重传参考音之后就会出现，见 ``services/voice_preview.py``）。

    ``stale`` 必须与 ``ready`` 分开：两者都「盘上有文件、能播」，但前者播出来是
    旧嗓子。合成一个状态，用户听到不像时的第一反应会是去查引擎、查模型，
    而真正的原因（参考音换过、样本没重生成）面板上一个字都没提。

    ``missing`` 与 ``failed`` **必须分开**：前者点一下就行，后者再点一下大概率还是
    失败 —— 得先看那句话。合成一个"没有"会让用户反复点一个注定失败的按钮。
    """

    voice_id: str
    status: str
    #: 能播的时候才有（`<audio src>` 直接用它；面板**不自己拼**这个 url）
    url: str | None = None
    duration_ms: int | None = None
    #: 这一份是**哪台引擎**念的（``cosyvoice2`` / ``sapi``）—— "换了参考音但听起来
    #: 还是旧嗓子"时，第一件要看的就是它
    engine: str | None = None
    generated_at: str | None = None
    error: str | None = None
    note: str | None = None


class VoiceOption(BaseModel):
    """音色下拉框的一行。

    ``source`` 说明它**从哪来**：``profile`` 是入库的参考音（``voice_profiles``），
    ``sapi`` 是系统装的。面板据此分组显示 —— 两者的音质与用途不一样，混在一列里
    会让人以为"这两个是同一档东西"。

    ``speakable`` 说明**当前这台引擎念不念得出来** —— 它与 ``source`` 是**两件事**：
    参考音是"素材已经在库里"，而它要 CosyVoice 才能念。权重没到位时它照旧出现在
    候选里（用户要知道入库的东西还在），但**选了也发不出声**：SAPI 收到 ``bigbear``
    会 ``SelectVoice`` 失败 ⇒ 这一句降级成静音 ⇒ 成片没人声。把这件事写在
    ``source`` 里是不够的（"参考音"听起来只是"另一种音色"），得有独立的字段。
    """

    id: str
    source: str
    speakable: bool
    #: 试听样本的五态（见 :class:`VoicePreview`）。下拉框旁边那颗「试听」按钮按它画：
    #: ``missing`` 显示「生成试听」、``running`` 转圈、``ready`` 可直接播、
    #: ``stale`` 显示「重新生成」（盘上那份是上一版参考音念的）。
    preview_state: str = "missing"
    #: 样本的 url（``ready`` 时才有）。**由后端给** —— 面板自己拼路径的话，
    #: 后端一改目录就变成"点了播放没反应"，且不报错。
    preview_url: str | None = None


class VoiceOptions(BaseModel):
    """可选的音色（下拉框）+ 这条任务现在的映射（``task_id`` 给了才有）。"""

    voices: list[VoiceOption]
    task_id: str | None = None
    voice_map: dict[str, str] = Field(default_factory=dict)
    note: str | None = None


class SentenceVoice(BaseModel):
    """一句的配音状态（面板上那一行）。"""

    id: str
    seq: int
    speaker: str
    text: str
    tts_status: str
    tts_engine: str | None = None
    tts_voice_id: str | None = None
    tts_duration_ms: int | None = None
    tts_attempts: int = 0
    tts_error: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    version: int = 1
    degraded: bool = False
    audio_url: str | None = None
    audio_source: str | None = None
    can_resynth: bool = True


class SentenceVoiceList(BaseModel):
    """一条任务的逐句配音状态（面板的主列表）。

    ``timeline_stale`` 是**推断**而不是事实：盘上那份时间轴存在、而还有句子没定局
    ⇒ 它说的不是现在这条片子。真正的判据要等下一轮收口重算出来
    （陷阱 #26：时间轴只有"整条重算"这一种更新方式）。
    """

    task_id: str
    task_status: str
    progress: SentenceProgressModel
    sentences: list[SentenceVoice]
    voice_map: dict[str, str]
    timeline_total_ms: int | None = None
    timeline_stale: bool = False


class ResynthResponse(BaseModel):
    """单句重配的结果。"""

    sentence_id: str
    task_id: str
    seq: int
    status: str
    job_id: str | None = None
    job_created: bool = False
    progress: SentenceProgressModel
    timeline_stale: bool = False
    timeline_total_ms: int | None = None
    hint: str = TIMELINE_HINT


#: 「开始配音」之后那句话 —— 面板直接显示它（下一站在哪，用户不用猜）
ENQUEUE_HINT: str = (
    "作业已经排进 voice 池：常驻池在跑就会接着念，这一屏每 1 秒自己刷新一次。"
    "全部定局之后，去「一键出片」把母带与渲染推完（那一步会出 final.mp4）"
)


class EnqueueVoiceResponse(BaseModel):
    """「开始配音」的结果：把这条任务待配音的句子一次投进 voice 池。

    ``queued`` 与 ``outstanding`` 是**两个数**，面板两个都要说：前者是"这一下真投出去
    几条"，后者是"这条任务还有几条没定局"。只有前者，用户看到 0 会以为按钮坏了
    （其实那 54 条早就在池子里排着）。
    """

    task_id: str
    status_before: str
    status: str
    queued: int
    outstanding: int
    progress: SentenceProgressModel
    timeline_stale: bool = False
    timeline_total_ms: int | None = None
    hint: str = ENQUEUE_HINT


class VoiceMapRequest(BaseModel):
    """任务级换音色的请求体。

    ``extra="forbid"`` 与合成配置 / 渲染面板同一条：把 ``voice_map`` 拼成
    ``voiceMap`` 被静默忽略，人会以为"音色已经换了"，而配音用的是旧的那一个。
    """

    model_config = ConfigDict(extra="forbid")

    voice_map: dict[str, str] = Field(min_length=1)
    confirm: bool = False


class VoiceChangeModel(BaseModel):
    """一个角色换成了什么。"""

    speaker: str
    before: str | None = None
    after: str
    affected: int = 0
    sentences: list[int] = Field(default_factory=list)


class VoiceMapResponse(BaseModel):
    """任务级换音色的结果。

    ``busy`` 是"想失效但没动成"的句子 id（正被某个 worker 念着）。面板必须显示它：
    不显示的话，用户看到"换音色成功"，而那几句的成片里还是旧嗓子。
    """

    task_id: str
    voice_map: dict[str, str]
    changes: list[VoiceChangeModel]
    affected: int = 0
    requeued: int = 0
    created_jobs: int = 0
    busy: list[str] = Field(default_factory=list)
    progress: SentenceProgressModel
    timeline_stale: bool = False
    timeline_total_ms: int | None = None
    hint: str = TIMELINE_HINT


def progress_view(progress: Any) -> SentenceProgressModel:
    """``SentenceProgress`` ⇒ 展示模型（**唯一**转换点）。"""
    return SentenceProgressModel.model_validate(progress.to_dict())


def sentence_voice_view(
    row: SentenceRow, *, audio_url: str | None = None, audio_source: str | None = None
) -> SentenceVoice:
    """``SentenceRow`` ⇒ 展示模型（**唯一**转换点）。"""
    return SentenceVoice(
        id=row.id,
        seq=row.seq,
        speaker=row.speaker,
        text=row.text,
        tts_status=row.tts_status,
        tts_engine=row.tts_engine,
        tts_voice_id=row.tts_voice_id,
        tts_duration_ms=row.tts_duration_ms,
        tts_attempts=row.tts_attempts,
        tts_error=row.tts_error,
        start_ms=row.start_ms,
        end_ms=row.end_ms,
        version=row.version,
        degraded=row.tts_status == SKIPPED_STATUS,
        audio_url=audio_url,
        audio_source=audio_source,
        can_resynth=row.tts_status != SYNTHESIZING_STATUS,
    )
