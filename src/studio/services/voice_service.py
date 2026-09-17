"""配音阶段收口：逐句音频 → 母带 + 时间轴（T2.7 · §04.2.7）。

它比"一个函数"多出来的那点东西，全是**顺序**
--------------------------------------------
```text
① 全部句子都定局了吗（done / skipped）—— 没定局 ⇒ 报错停下，别拼半条母带
② 逐句 ffprobe 实测（量盘上那一份，不是库里记的那一份）
③ 算时间轴（纯计算，tts/timeline.build_timeline）
④ 拼母带（一次 ffmpeg，把句间停顿真的填进音频）
⑤ 量母带、与时间轴对账（超线只 warn，见 MASTER_DRIFT_TOLERANCE_MS）
⑥ 写 timeline.json
⑦ 回写 script_sentences.start_ms / end_ms（事务内，逐行带 version 守卫）
⑧ 记 artifacts（timeline / voice_master）
```

为什么 ⑦ 的失败要**整条作废**
------------------------------
回写带 ``version`` 守卫：某一行的 ``version`` 在"读句子"与"回写"之间变过（有人改了稿），
这一行的 ``UPDATE`` 就命中 0 行 —— 而这时**整条时间轴都旧了**：第 3 句改短了 200ms，
第 4 句起每一句的 ``start_ms`` 都该往前挪。所以不做"跳过这一行、其余照写"，
而是抛错让调用方重跑（重跑就是全量重算，陷阱 #26）。

为什么音频路径以**盘上**为准
----------------------------
``tts_audio_path`` 记的是"上次写库时那份音频在哪"。产物会被 §03.7.5 的 24 小时 GC
删掉、也可能被手工换过 —— 拼母带要读的是**现在盘上**那一份，所以这里先看库里的路径，
再看 ``StudioPaths.sentence_wav``（路径由 ``seq`` 决定，§04.3.3 不变量 1）。
两处都没有 ⇒ 由 :func:`~studio.tts.timeline.probe_sentence_ms` 报错（报错只该有一处）。

这个服务**不改任务状态**
------------------------
``voicing → queued_render`` 的迁移归 T2.8 的编排（§03.4.5）：这一步只回答
"母带与时间轴好了没有"，至于"好了之后往哪走"是编排的事。把状态迁移塞进来，
会让"只想重新算一遍时间轴"变成一个会改任务状态的副作用。

操作面（T2.9）也放在这里，理由同一条
------------------------------------
单句重配（:func:`resynth_sentence`）与任务级换音色（:func:`set_voice_map`）改的都是
"这一句该不该重念"，与收口读的是同一张表。拆成两个模块会让"什么算待办"出现两处判据
（:meth:`~studio.db.repositories.sentence_repo.SentenceRepo.invalidate` 与
:meth:`~studio.db.repositories.sentence_repo.SentenceRepo.pending_for_task`），
而两处一旦不一致，症状是"点了重配、任务却永远停在 ``voicing``"（陷阱 #115）。

操作面**只投递，不合成**
------------------------
重配一句要几秒，换音色要重配 N 句（几十秒到几分钟）。REST 面把它同步做完，
一次点击就会变成一个挂住的请求。所以这里只做三件事：改业务表、把作业排回池、
留痕；真正念的是 voice 池（与 T2.8 裁定 224 的"投递 / 排空两步"同一条）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.config import OutputsConfig, load_outputs_config
from studio.core.errors import ErrorCode, StudioError
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.db.models import SentenceRow
from studio.db.queue import JobStore
from studio.db.repositories import AuditRepo, VoiceProfileRepo
from studio.db.repositories.artifact_repo import ArtifactRepo
from studio.db.repositories.sentence_repo import (
    DONE_STATUS,
    FAILED_STATUS,
    PENDING_STATUS,
    SKIPPED_STATUS,
    SYNTHESIZING_STATUS,
    SentenceProgress,
    SentenceRepo,
    TimelineSpan,
)
from studio.domain.enums import UnitType
from studio.domain.task_service import TaskService
from studio.tts.cache import TtsCache
from studio.tts.sapi import list_voices_cached
from studio.tts.timeline import (
    Timeline,
    TimelineSource,
    assemble_voice_master,
    build_timeline,
    has_audio,
    measure_master_ms,
    probe_sentence_ms,
    write_timeline,
)

__all__ = [
    "ResolvedVoice",
    "ResynthReport",
    "VoiceChange",
    "VoiceMapReport",
    "VoiceStageReport",
    "enqueue_sentences",
    "preview_audio",
    "read_timeline_total_ms",
    "resolve_voice",
    "resynth_sentence",
    "set_voice_map",
    "settle_voice",
    "speakable_voices",
    "usable_voices",
    "voice_payloads",
]

logger = get_logger("studio.services.voice")

#: ``artifacts.ttl_hint`` 的两个取值（§03.3.11 的词表）。
TTL_FOREVER: Final[str] = "forever"
TTL_VOICE_MASTER: Final[str] = "24h"


@dataclass(frozen=True, slots=True)
class VoiceStageReport:
    """配音收口时的结论（面板 / manifest / 日志都读它）。"""

    task_id: str
    timeline: Timeline
    timeline_path: Path
    voice_master: Path
    voice_master_ms: int
    degraded: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "timeline_path": self.timeline_path.as_posix(),
            "voice_master": self.voice_master.as_posix(),
            "voice_master_ms": self.voice_master_ms,
            "sentences": len(self.timeline.sentences),
            "degraded": self.degraded,
            "total_ms": self.timeline.total_ms,
            "tail_ms": self.timeline.tail_ms,
            "seed": self.timeline.seed,
        }


#: 配音作业的池名（``pools.yaml`` 的 ``voice``）—— 重排作业要用它
VOICE_POOL: Final[str] = "voice"

#: :attr:`ResolvedVoice.source` 的三个取值（面板据此说"这个音色到底生效了没有"）
VOICE_SOURCE_MAP: Final[str] = "voice_map"
VOICE_SOURCE_FALLBACK: Final[str] = "fallback"
VOICE_SOURCE_DEFAULT: Final[str] = "engine_default"

#: 可以被 :meth:`SentenceRepo.invalidate` 退回待办的句级状态（T2.9 的"重配"判据）。
#: ``synthesizing`` **不在**其中：那一句正被某个 worker 攥着，抢在它眼皮底下改状态
#: 会让两个 worker 往同一个文件里写（见 ``invalidate`` 的 docstring）。
REINVALIDATABLE_STATUSES: Final[tuple[str, ...]] = (
    PENDING_STATUS,
    FAILED_STATUS,
    DONE_STATUS,
    SKIPPED_STATUS,
)


@dataclass(frozen=True, slots=True)
class ResolvedVoice:
    """一个逻辑角色（``speaker``）这一次**用哪个音色**念。

    ``voice is None`` 的意思是"**不覆盖**"：作业 payload 里不放 ``voice`` 键，
    由配音池进程装配时挑的那个音色来念。这不是"没解析出来"，而是"这件事不该由
    这里定" —— 进程级音色在 ``build_voice_handler`` 里已经解析过一次了。
    """

    speaker: str
    voice: str | None
    requested: str | None
    source: str

    @property
    def is_fallback(self) -> bool:
        """``voice_map`` 里写了、但本机没有这个音色 ⇒ 退回进程音色。"""
        return self.source == VOICE_SOURCE_FALLBACK

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "voice": self.voice,
            "requested": self.requested,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ResynthReport:
    """单句重配的结论（面板点完「重配」要看到的东西）。"""

    sentence_id: str
    task_id: str
    seq: int
    status: str
    job_id: str | None
    job_created: bool
    progress: SentenceProgress
    timeline_stale: bool
    timeline_total_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sentence_id": self.sentence_id,
            "task_id": self.task_id,
            "seq": self.seq,
            "status": self.status,
            "job_id": self.job_id,
            "job_created": self.job_created,
            "progress": self.progress.to_dict(),
            "timeline_stale": self.timeline_stale,
            "timeline_total_ms": self.timeline_total_ms,
        }


@dataclass(frozen=True, slots=True)
class VoiceChange:
    """一个角色的音色从哪换到哪，以及它牵动了哪几句。"""

    speaker: str
    before: str | None
    after: str
    sentences: tuple[int, ...]

    @property
    def affected(self) -> int:
        return len(self.sentences)

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "before": self.before,
            "after": self.after,
            "affected": self.affected,
            "sentences": list(self.sentences),
        }


@dataclass(frozen=True, slots=True)
class VoiceMapReport:
    """任务级换音色的结论。

    ``busy`` 与 ``affected`` 分开：``busy`` 是"想失效但没动成"的那几句（正被某个
    worker 念着，或者在这几毫秒里被认领了）。把它们**报出来**而不是静默跳过：
    不报的话，用户看到"换音色成功"，而那几句的成片里还是旧嗓子 —— 这是最难查的
    一类（陷阱 #117）。
    """

    task_id: str
    voice_map: dict[str, str]
    changes: tuple[VoiceChange, ...]
    affected: int
    requeued: int
    created_jobs: int
    busy: tuple[str, ...]
    progress: SentenceProgress
    timeline_stale: bool
    timeline_total_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "voice_map": dict(self.voice_map),
            "changes": [change.to_dict() for change in self.changes],
            "affected": self.affected,
            "requeued": self.requeued,
            "created_jobs": self.created_jobs,
            "busy": list(self.busy),
            "progress": self.progress.to_dict(),
            "timeline_stale": self.timeline_stale,
            "timeline_total_ms": self.timeline_total_ms,
        }


def enqueue_sentences(
    *,
    connection: sqlite3.Connection,
    task_id: str,
    repo: SentenceRepo | None = None,
) -> int:
    """把这条任务里**还没定局**的句子投进 voice 池（幂等；返回**这次真投出去**的条数）。

    待办判据就是 §04.3.3 不变量 2 的原话：``tts_status ∈ {pending, failed}``。

    ``synthesizing`` 为什么不算待办
    ------------------------------
    它是"上一次念到一半崩了"的中间态，出口是租约过期后由 sweeper 回收
    （§03.3.7），在这里重投只会和 sweeper 抢同一句 —— 而抢赢的那次很可能念到
    一半又崩。**把"正在跑"混进"该跑"，会让"重跑一次"变成"重跑两遍"。**

    幂等由 ``JobStore.enqueue`` 保证（``(task_id, pool, unit_type, unit_ref)`` 上有
    唯一约束，冲突即 ``DO NOTHING``）—— 所以"断点续跑时再投一遍"是安全的，
    返回 0 就是"都投过了"。

    为什么投递时要**带上音色**（T2.9）
    ----------------------------------
    ``tasks.payload_json.voice_map`` 是"这条任务用什么音色"的唯一答案，而它现在
    只有两条路径能生效：这里（首次投递）与
    :meth:`~studio.db.queue.JobStore.requeue_unit`（重排）。不在这里带上，
    换完音色重跑一次的人会看到"库里写着新音色、念出来的还是旧嗓子" ——
    而这条路径**不报错**（陷阱 #117）。解析不出音色（``voice_map`` 里那个名字
    本机没有）时给空 payload：那是"不覆盖"，由池进程装配的音色兜底。
    """
    store = JobStore(connection)
    rows = (repo if repo is not None else SentenceRepo(connection)).pending_for_task(task_id)
    payloads = voice_payloads(connection=connection, task_id=task_id)
    queued = 0
    for row in rows:
        job_id = store.enqueue(
            task_id=task_id,
            pool=VOICE_POOL,
            unit_type=UnitType.SENTENCE.value,
            unit_ref=row.id,
            payload=payloads.get(row.id),
        )
        if job_id is not None:
            queued += 1
    logger.info("voice.enqueued", task_id=task_id, queued=queued, outstanding=len(rows))
    return queued


def usable_voices(connection: sqlite3.Connection) -> tuple[str, ...]:
    """本机现在**选得出来**的音色（``voice_profiles`` 已启用 ∪ SAPI 已装）。

    为什么是**并集**而不是"以音色库为准"
    ------------------------------------
    ``voice_profiles`` 是参考音（CosyVoice 那一档要用的），SAPI 的音色是系统装的。
    本机现在**一条音色行都没有**（E4 未到位）：只认前者的话，换音色的下拉框是空的
    —— 而配音明明跑得动（SAPI 有中文音色）。反过来说，只认 SAPI 又会把"已经入库的
    参考音"判成不存在。两处都列，用户看到的就是"这台机器现在真的能用什么"。

    ``sorted`` 而不是按入库顺序：下拉框两次打开的顺序必须一样，否则人会以为选项变了。

    ⚠️ 这是**候选**，不是"念得出来"：要判"这次配音真的能用它吗"请用
    :func:`speakable_voices` —— 两者在只有 SAPI 的时候差得很远（参考音的名字
    SAPI 不认识）。
    """
    from_profiles = tuple(row.id for row in VoiceProfileRepo(connection).list_all(enabled_only=True))
    return tuple(sorted(set(from_profiles) | set(list_voices_cached())))


def speakable_voices(connection: sqlite3.Connection) -> tuple[str, ...]:
    """**当前这台引擎**真的念得出来的音色名 —— 与 :func:`usable_voices` 不是一回事。

    两个问题，两条答案
    ------------------
    :func:`usable_voices` 回答"这台机器上**有什么**"（已启用参考音 ∪ 系统音色）——
    那是**下拉框的候选**，它必须全，否则用户会以为"我刚入库的音色不见了"。
    这里回答"**念得出来吗**"，而唯一的裁判是**当前这台引擎**。

    并集为什么会让整条配音哑掉
    --------------------------
    ``voice_profiles`` 里的 id 是**零样本参考音**的名字，SAPI 不认识它：把
    ``bigbear`` 交给 SAPI，``SelectVoice`` 直接抛 ⇒ 这一句连失败 3 次、降级成静音，
    **每一句都是** ⇒ 成片没人声 —— 而库里写着"换音色成功"。这正是
    :func:`set_voice_map` 那段 docstring 里怕的那件事，而并集让它**通过了校验**。
    （真机实测 2026-09-17：``synthesize(voice="bigbear")`` ⇒ ``rc=1``、
    ``SelectVoice`` 抛异常；同一个文本传 ``voice=None`` 正常出 162 KB 的 wav。）

    今天只有 SAPI 一台引擎（T2.1 / E5 之前 CosyVoice 装不上）⇒ 能念的就是系统
    语音包。T2.3 的引擎路由到位后，这里按**当前引擎**分叉（CosyVoice 那一档才把
    ``voice_profiles`` 合进来），而不是无条件并集。
    """
    return tuple(list_voices_cached())


def resolve_voice(*, voice_map: Mapping[str, str], speaker: str, available: Sequence[str]) -> ResolvedVoice:
    """``speaker`` ⇒ 这一次要传给引擎的音色（**纯函数，三种结局都不抛**）。

    - ``voice_map`` 没写这个角色 ⇒ ``engine_default``（不覆盖，交给池进程的音色）
    - 写了、且本机有 ⇒ ``voice_map``（真正生效）
    - 写了、但本机没有 ⇒ ``fallback``（退回进程音色，**在报告里说出来**）

    为什么"写了但本机没有"不在这里抛
    --------------------------------
    ``TaskPayload.voice_map`` 的默认值是 ``{"bigbear": "bigbear", ...}`` —— 那是
    **逻辑角色名**，不是真音色。本机没装同名音色是常态（E4 未到位时必然如此），
    在这里抛就等于"每个新建任务都配不出音"。用户的**显式**选择在写入时校验
    （:func:`set_voice_map` ⇒ ``TTS_VOICE_MISSING``）—— 那面对的是"人刚填的那个
    字符串"，这里面对的是"库里早就存着的那个"，两件事。
    """
    requested = voice_map.get(speaker)
    if not requested:
        return ResolvedVoice(speaker, None, None, VOICE_SOURCE_DEFAULT)
    if requested in available:
        return ResolvedVoice(speaker, requested, requested, VOICE_SOURCE_MAP)
    return ResolvedVoice(speaker, None, requested, VOICE_SOURCE_FALLBACK)


def voice_payloads(
    *,
    connection: sqlite3.Connection,
    task_id: str,
    available: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """句子 id ⇒ 该句作业的**运行期** payload（目前只有 ``voice``）。

    返回的字典**每句都有键**（解析不出音色时是空 dict）：调用方要区分"这一句没有
    覆盖"与"我忘了给它"，而 ``.get(id)`` 拿到 ``None`` 分不出这两件事。

    逐角色解析一次就够：``speaker`` 的取值集合很小（一期就两个），而
    :func:`resolve_voice` 是纯函数 —— 同一个角色解析两次只会得到同一个答案。
    """
    rows = SentenceRepo(connection).list_for_task(task_id)
    if not rows:
        return {}
    voice_map = TaskService(connection).get(task_id).payload.voice_map
    voices = tuple(available) if available is not None else speakable_voices(connection)
    resolved: dict[str, ResolvedVoice] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.speaker not in resolved:
            resolved[row.speaker] = resolve_voice(voice_map=voice_map, speaker=row.speaker, available=voices)
        choice = resolved[row.speaker]
        payloads[row.id] = {} if choice.voice is None else {"voice": choice.voice}
    return payloads


def preview_audio(*, paths: StudioPaths, row: SentenceRow) -> tuple[Path, str] | None:
    """这一句现在**能播**哪一份音频 ⇒ ``(路径, 来源)``；一处都没有 ⇒ ``None``。

    三处按顺序找，只认**盘上已经存在**的文件：

    1. 规范路径 ``data/output/voice/<task_id>/s00N.wav``（§04.3.3 不变量 1）
    2. 库里记的那一份（``tts_audio_path``）—— **必须**落在 ``data_dir`` 里
    3. TTS 缓存里的那一份（``tts_hash`` 就是缓存键）

    **绝不触发合成**（T2.9 的硬要求）：试听与合成抢同一台机器，点一下试听就排一次
    合成，等于把"看一眼"变成"重跑一遍"；而"重跑一遍"还会覆盖交付产物。

    第 2 条为什么要判"在不在 ``data_dir`` 里"：那一列是**库里的一个字符串**，
    被改过（或某次写入带进了绝对路径）就会让这个端点变成一个任意文件读取。
    规范路径与缓存路径都由 ``paths`` 拼出来，本来就没有这个问题。
    """
    canonical = paths.sentence_wav(row.task_id, row.seq)
    if canonical.is_file():
        return canonical, "sentence"
    recorded = Path(row.tts_audio_path) if row.tts_audio_path else None
    if recorded is not None and _inside(recorded, paths.data_dir) and recorded.is_file():
        return recorded, "recorded"
    if row.tts_hash:
        cached = TtsCache(paths.tts_cache_dir).path_for(row.tts_hash)
        if cached.is_file():
            return cached, "cache"
    return None


def resynth_sentence(
    *,
    connection: sqlite3.Connection,
    sentence_id: str,
    paths: StudioPaths | None = None,
    actor: str = "user",
) -> ResynthReport:
    """单句重配（T2.9）：这一句退回待办 + 它的作业排回 voice 池。

    为什么必须**同时**动两处（业务表 + 作业表）
    ------------------------------------------
    只改 ``script_sentences.tts_status``：队列里那条作业已经是 ``succeeded``，
    而幂等键 ``(task_id, pool, unit_type, unit_ref)`` 让它**一辈子只有一条**作业 ——
    没有任何 worker 会再看这一句一眼。症状是"点了重配没反应、不报错、任务卡在
    ``voicing``"（陷阱 #115）。所以状态与调度令牌要一起改，见
    :meth:`~studio.db.queue.JobStore.requeue_unit` 的 docstring。

    **不在这里合成**：REST 面的职责是"把这件事排进池"，真正念的是 voice 池
    （与 T2.8 裁定 224 的"投递 / 排空两步"同一条）。同步念一句要几秒，而
    "换音色会重配 N 句"那种操作会把这个请求拖到几分钟 —— 那时它已经不是"一次点击"了。

    :param paths: 给了才读得到"这条片子现在多长"（``timeline.json``）。**只读**，
        不重算 —— 重算归下一轮收口（全量重算，陷阱 #26）。
    """
    repo = SentenceRepo(connection)
    row = repo.get(sentence_id)
    if row is None:
        raise StudioError(
            f"稿子里没有这一句：{sentence_id}",
            code=ErrorCode.SCRIPT_NOT_FOUND,
            context={"sentence_id": sentence_id},
            remediation="刷新稿件面板：这一句可能已经随改稿被删掉了",
        )
    if row.tts_status == SYNTHESIZING_STATUS:
        raise StudioError(
            f"第 {row.seq} 句正在被念，不能重配",
            code=ErrorCode.STATE_TRANSITION_ILLEGAL,
            context={"sentence_id": sentence_id, "seq": row.seq, "tts_status": row.tts_status},
            remediation=(
                "等它跑完（面板上显示「进行中」）再点重配：抢在它眼皮底下改状态会让两个 worker 写同一个文件"
            ),
        )

    if not repo.invalidate(sentence_id, expected_version=row.version):
        raise StudioError(
            f"第 {row.seq} 句在重配之前被改过，这一次作废",
            code=ErrorCode.STATE_VERSION_CONFLICT,
            context={"sentence_id": sentence_id, "expected_version": row.version},
            remediation="刷新面板后重试（新的一版会按改后的文本重念）",
        )

    payloads = voice_payloads(connection=connection, task_id=row.task_id)
    payload = payloads.get(sentence_id)
    job_id, created = _reschedule(connection, task_id=row.task_id, sentence_id=sentence_id, payload=payload)
    progress = repo.progress(row.task_id)
    total_ms = read_timeline_total_ms(paths, row.task_id)
    AuditRepo(connection).record(
        actor=actor,
        action="sentence.resynth",
        target_type="sentence",
        target_id=sentence_id,
        task_id=row.task_id,
        before={
            "tts_status": row.tts_status,
            "tts_attempts": row.tts_attempts,
            "tts_voice_id": row.tts_voice_id,
            "tts_duration_ms": row.tts_duration_ms,
        },
        after={
            "tts_status": PENDING_STATUS,
            "tts_attempts": 0,
            "job_id": job_id,
            "job_created": created,
            "voice": None if payload is None else payload.get("voice"),
            "timeline_stale": True,
        },
        reason=f"第 {row.seq} 句人工重配",
    )
    logger.info(
        "voice.resynth",
        task_id=row.task_id,
        sentence_id=sentence_id,
        seq=row.seq,
        before=row.tts_status,
        job_id=job_id,
        job_created=created,
    )
    return ResynthReport(
        sentence_id=sentence_id,
        task_id=row.task_id,
        seq=row.seq,
        status=PENDING_STATUS,
        job_id=job_id,
        job_created=created,
        progress=progress,
        timeline_stale=True,
        timeline_total_ms=total_ms,
    )


def set_voice_map(
    *,
    connection: sqlite3.Connection,
    task_id: str,
    voice_map: Mapping[str, str],
    confirm: bool = False,
    available: Sequence[str] | None = None,
    paths: StudioPaths | None = None,
    actor: str = "user",
) -> VoiceMapReport:
    """任务级换音色（T2.9）：写 ``voice_map`` + 受影响的句子全部失效并重排。

    请求是**增量**（PATCH 的语义）
    --------------------------------
    只给 ``{"bigbear": "X"}`` 就只改熊大，熊二那条映射原样保留。整体替换看起来更"干净"，
    但它的失败方式是静默的：面板只提交被改的那个角色 ⇒ 另一个角色的映射被悄悄抹掉
    ⇒ 它退回进程音色，而用户以为自己只改了一个人。要一次改两个就一次给两个键。

    **先校验，再算代价，最后才写**
    ------------------------------
    ① 音色本机没有 ⇒ ``TTS_VOICE_MISSING``（422）。这一条必须在**写入之前**报：
       写进去之后，配音会连失败 3 次 ⇒ 每一句都降级成静音 ⇒ 用户看到"换音色成功"，
       拿到的却是一支**没人声**的成片。这是一处"成功了但结果全错"的失败。
       判的是**这次提交的那几条**（不是合并后的整张表）：库里存着的占位值（默认的
       ``{"bigbear": "bigbear", ...}``）不归这次提交管 —— 详见 ``missing`` 那一段。
    ② 会重配 N 句且没确认 ⇒ ``VOICE_MAP_CONFIRM_REQUIRED``（409）。换音色 = 重配
       N 句（每句几秒），这是个要人点头的代价，不是一次静默的写入。
    ③ 前两条都过了才真写。

    三处要一起改，缺一处就是一个"看起来成功"的错
    --------------------------------------------
    - ``tasks.payload_json.voice_map``（:meth:`TaskService.set_voice_map`）——
      "这条任务用什么音色"的唯一答案，也是 :func:`voice_payloads` 的输入；
    - ``script_sentences.tts_status``（:meth:`SentenceRepo.invalidate`）——
      句子自己的结论：旧音色念出来的那一份不算数了；
    - ``jobs.payload_json``（``requeue_unit(payload=...)``）—— **运行期**的选择。
      只改前两处，重排出去的作业还是旧音色，而库里显示的是新音色（陷阱 #117）。

    ``synthesizing`` 的句子**不动**：它正被某个 worker 攥着，抢在它眼皮底下改状态
    会让两个 worker 往同一个文件里写。它们的 id 进 :attr:`VoiceMapReport.busy`，
    面板据此提示"这几句等念完再单独重配"。

    时间轴（T2.9 验收里的"重算"）**不在这里算**：任务在 ``completed`` 时没有
    ``completed → voicing`` 这条边（§03.4.5），硬造一条迁移只为了让一个端点顺手
    重算时间轴，代价是把状态机改松。真正重算的是下一轮收口（``settle_voice``，
    全量重算，陷阱 #26）—— 响应里的 ``timeline_stale`` 说的就是这件事。
    """
    service = TaskService(connection)
    task = service.get(task_id)
    before = dict(task.payload.voice_map)
    rows = SentenceRepo(connection).list_for_task(task_id)
    submitted = {str(speaker): str(voice) for speaker, voice in voice_map.items()}
    after = {**before, **submitted}
    voices = tuple(available) if available is not None else speakable_voices(connection)

    # 角色名写错（``bigBear``）是一个**静默无操作**：请求 200、映射多一个没人用的键、
    # 真正要换的那个角色纹丝不动。已知角色 = 现在映射里的键 ∪ 这条任务稿子里的 speaker。
    known = set(before) | {row.speaker for row in rows}
    unknown = sorted(set(submitted) - known)
    if unknown:
        raise StudioError(
            f"不认识这些角色：{'、'.join(unknown)}",
            code=ErrorCode.VALIDATION_FAILED,
            context={"task_id": task_id, "unknown": unknown, "known": sorted(known)},
            remediation="角色名用 speaker 那一列里的值（一期是 bigbear / littlebear）",
        )

    # 只校验**这次提交的**那几条（§04.3.7「音色校验的两条线」）
    # ------------------------------------------------
    # 判 `after` 整张表会把**库里早就存着的**占位值一起判死：`TaskPayload.voice_map` 的
    # 默认值是 `{"bigbear": "bigbear", "littlebear": "littlebear"}`（逻辑角色名，不是真
    # 音色），于是"新建任务第一次换音色"必然 422 —— 用户改的是熊大，被拒的理由却是他根本
    # 没碰过的熊二（真机演练里撞上的就是这一条）。
    # 分工：人**刚填**的那个字符串必须存在（这里）；库里**早就存着**的允许解析不出来
    # （`resolve_voice` 退回进程音色并标 `fallback`，面板照实显示）。
    missing = {speaker: voice for speaker, voice in submitted.items() if voice not in voices}
    if missing:
        # **分两句说**：「本机根本没有」与「本机有、但这台引擎念不出来」要用户做的事
        # 完全不同 —— 混成一句"本机没有"，用户会去重新入库一个已经入好的音色。
        # 真机上撞到过：映射写 `bigbear`，而当时本机只有 `bear_da` / `bear_xiong`。
        registered = {row.id for row in VoiceProfileRepo(connection).list_all(enabled_only=True)}
        unspeakable = sorted(value for value in missing.values() if value in registered)
        absent = sorted(value for value in missing.values() if value not in registered)
        raise StudioError(
            f"这些音色现在念不出来：{'、'.join(sorted(missing.values()))}",
            code=ErrorCode.TTS_VOICE_MISSING,
            context={
                "task_id": task_id,
                "missing": missing,
                "absent": absent,
                "unspeakable": unspeakable,
                "available": list(voices),
            },
            remediation=(
                "换一个 available 里的音色；系统音色在「设置 → 时间和语言 → 语音」里装。"
                "参考音（absent / unspeakable 里那些）要 CosyVoice 才念得出来 —— "
                "权重未到位时它只能当素材留着，见 T2.1 / E5"
            ),
        )

    changed = {speaker: after[speaker] for speaker in after if before.get(speaker) != after[speaker]}
    affected = [row for row in rows if row.speaker in changed and row.tts_status in REINVALIDATABLE_STATUSES]
    busy = [row.id for row in rows if row.speaker in changed and row.tts_status == SYNTHESIZING_STATUS]

    if affected and not confirm:
        raise StudioError(
            f"换音色会让 {len(affected)} 句重新配音，确认后再提交",
            code=ErrorCode.VOICE_MAP_CONFIRM_REQUIRED,
            context={
                "task_id": task_id,
                "affected": len(affected),
                "total": len(rows),
                "speakers": sorted(changed),
                "sentences": [row.seq for row in affected],
            },
            remediation="带上 confirm=true 重发一次（面板据此弹二次确认框）",
        )

    service.set_voice_map(task_id, after)
    payloads = voice_payloads(connection=connection, task_id=task_id, available=voices)
    repo = SentenceRepo(connection)
    invalidated: list[SentenceRow] = []
    for row in affected:
        if repo.invalidate(row.id, expected_version=row.version):
            invalidated.append(row)
        else:
            # 读到写之间被某个 worker 认领了（``begin`` ⇒ ``synthesizing``）：
            # 不去和它抢，但要说出来 —— 静默跳过就是"换音色成功、成片还是旧嗓子"。
            busy.append(row.id)

    requeued = 0
    created_jobs = 0
    for row in invalidated:
        job_id, created = _reschedule(
            connection, task_id=task_id, sentence_id=row.id, payload=payloads.get(row.id)
        )
        if job_id is None:
            continue
        requeued += 1
        created_jobs += 1 if created else 0

    changes = tuple(
        VoiceChange(
            speaker=speaker,
            before=before.get(speaker),
            after=after[speaker],
            sentences=tuple(row.seq for row in invalidated if row.speaker == speaker),
        )
        for speaker in sorted(changed)
    )
    progress = repo.progress(task_id)
    total_ms = read_timeline_total_ms(paths, task_id)
    AuditRepo(connection).record(
        actor=actor,
        action="task.voice_map",
        target_type="task",
        target_id=task_id,
        task_id=task_id,
        before={"voice_map": before},
        after={
            "voice_map": after,
            "affected": len(invalidated),
            "requeued": requeued,
            "created_jobs": created_jobs,
            "busy": list(busy),
            "timeline_stale": bool(invalidated),
        },
        reason=f"任务级换音色（重配 {len(invalidated)} 句）",
    )
    logger.info(
        "voice.map_changed",
        task_id=task_id,
        changes=len(changes),
        affected=len(invalidated),
        requeued=requeued,
        created_jobs=created_jobs,
        busy=len(busy),
    )
    return VoiceMapReport(
        task_id=task_id,
        voice_map=after,
        changes=changes,
        affected=len(invalidated),
        requeued=requeued,
        created_jobs=created_jobs,
        busy=tuple(busy),
        progress=progress,
        timeline_stale=bool(invalidated),
        timeline_total_ms=total_ms,
    )


def _reschedule(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    sentence_id: str,
    payload: Mapping[str, Any] | None,
) -> tuple[str | None, bool]:
    """把一条句级作业排回待办；**没有这条作业** ⇒ 现开一条。

    返回 ``(job_id, created)``。"没有就新建"而不是报错：作业表只是**调度令牌**，
    而权威是业务表（"这一句要做"）。令牌被 GC / 死信清理 / 手工取消弄丢过，补一个
    就是 —— 反过来，为一个丢掉的令牌报错会让这一句永远念不出来。
    """
    store = JobStore(connection)
    job_id = store.requeue_unit(
        task_id=task_id,
        pool=VOICE_POOL,
        unit_type=UnitType.SENTENCE.value,
        unit_ref=sentence_id,
        payload=payload,
    )
    if job_id is not None:
        return job_id, False
    created = store.enqueue(
        task_id=task_id,
        pool=VOICE_POOL,
        unit_type=UnitType.SENTENCE.value,
        unit_ref=sentence_id,
        payload=payload,
    )
    return created, created is not None


def read_timeline_total_ms(paths: StudioPaths | None, task_id: str) -> int | None:
    """盘上那份 ``timeline.json`` 说整条片子多长（没有 / 读不动 ⇒ ``None``）。

    **只读不写**：重配之后这份时间轴就过期了，但删它或改它都不该由这里做 ——
    下一轮收口会全量重算并覆盖它（陷阱 #26）。读它只为一件事：让面板能把
    "现在 17.4 秒"和"重配后会变"放在一起说（T2.9 的验收口径）。
    """
    if paths is None:
        return None
    try:
        payload = json.loads(paths.timeline_json(task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    total = payload.get("total_ms") if isinstance(payload, dict) else None
    return total if isinstance(total, int) else None


def _inside(path: Path, root: Path) -> bool:
    """``path`` 是否落在 ``root`` 里（**解析之后**再比，符号链接也算）。"""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def settle_voice(
    *,
    paths: StudioPaths,
    connection: sqlite3.Connection,
    task_id: str,
    outputs: OutputsConfig | None = None,
    outputs_source: Path | None = None,
    seed: str | None = None,
) -> VoiceStageReport:
    """逐句音频 → ``voice_master.wav`` + ``timeline.json``（**全量重算**）。

    :param outputs: 已加载的合成配置（``audio.tail_ms`` 从它来）。缺省按
        ``config/outputs.yaml`` 现读一份 —— 与 ``render_service.produce_video``
        同一条口径，面板与 CLI 走的是同一份配置。
    :param seed: 停顿抖动的种子。缺省取 ``tasks.payload_json.seed``（再缺省用 ``task_id``）。
    """
    repo = SentenceRepo(connection)
    rows = repo.list_for_task(task_id)
    if not rows:
        raise StudioError(
            f"任务 {task_id} 没有句子，配音无从收口",
            code=ErrorCode.SCRIPT_NOT_FOUND,
            context={"task_id": task_id},
            remediation="先落一版稿件（studio script draft）再跑配音",
        )

    progress = repo.progress(task_id)
    if not progress.is_settled:
        raise StudioError(
            f"配音还没定局（{progress.settled}/{progress.total} 句），时间轴不能算",
            code=ErrorCode.STATE_TRANSITION_ILLEGAL,
            context={"task_id": task_id, "progress": progress.to_dict()},
            remediation="等 voice 池把剩下的句子念完（studio pool status voice），或先重投失败的句子",
        )

    config = (
        outputs
        if outputs is not None
        else load_outputs_config(outputs_source or (paths.config_dir / "outputs.yaml"))
    )
    resolved_seed = seed or _task_seed(connection, task_id)
    sources = tuple(_source(row, paths=paths, task_id=task_id) for row in rows)
    timeline = build_timeline(
        sources,
        task_id=task_id,
        seed=resolved_seed,
        tail_ms=config.audio.tail_ms,
        data_dir=paths.data_dir,
        voice_master=paths.voice_master(task_id),
    )

    master = assemble_voice_master(timeline, paths.voice_master(task_id))
    measured_ms = measure_master_ms(timeline, master)
    timeline_path = write_timeline(timeline, paths.timeline_json(task_id))
    _write_back(repo, rows=rows, timeline=timeline, task_id=task_id)
    _record(
        connection,
        paths=paths,
        task_id=task_id,
        timeline=timeline,
        timeline_path=timeline_path,
        master=master,
        measured_ms=measured_ms,
    )

    degraded = sum(1 for row in rows if row.tts_status == SKIPPED_STATUS)
    logger.info(
        "voice.settled",
        task_id=task_id,
        sentences=len(timeline.sentences),
        total_ms=timeline.total_ms,
        voice_master_ms=measured_ms,
        degraded=degraded,
    )
    return VoiceStageReport(
        task_id=task_id,
        timeline=timeline,
        timeline_path=timeline_path,
        voice_master=master,
        voice_master_ms=measured_ms,
        degraded=degraded,
    )


def _task_seed(connection: sqlite3.Connection, task_id: str) -> str:
    """任务种子：``tasks.payload_json.seed`` 优先，缺省用 ``task_id``。

    抖动必须**可复现**（重跑得到逐毫秒一致的时间轴），所以种子不能来自时钟或随机数。
    没给种子时拿 ``task_id`` 顶上 —— 它是 ULID，本身就足够打散；而"没种子就不抖"
    会让这一类任务在成片里长得一模一样，把防搬运的初衷做反了。
    """
    payload = TaskService(connection).get(task_id).payload
    return task_id if payload.seed is None else f"{task_id}:{payload.seed}"


def _source(row: SentenceRow, *, paths: StudioPaths, task_id: str) -> TimelineSource:
    """一句的音频在哪 + 实测多长 + 后面停多久。"""
    audio = _resolve_audio(row, paths=paths, task_id=task_id)
    return TimelineSource(
        sentence_id=row.id,
        seq=row.seq,
        speaker=row.speaker,
        # 字幕文本优先：``subtitle`` 是"可含手工断行"的那一列（§03.3.7），
        # 时间轴的 ``text`` 是喂给字幕的，喂 ``text`` 会把人工断行丢掉。
        text=row.subtitle or row.text,
        audio=audio,
        duration_ms=probe_sentence_ms(audio, seq=row.seq),
        base_pause_ms=row.pause_after_ms,
    )


def _resolve_audio(row: SentenceRow, *, paths: StudioPaths, task_id: str) -> Path:
    """这一句的音频在盘上的哪一份：库里记的优先，其次按 ``seq`` 推。

    两处**本该**是同一个文件（路径由 ``seq`` 决定，§04.3.3 不变量 1）；都看一遍是因为
    库里那一列只是"上次写进去的记录"，而规范路径上可能已经有一份新的（重合成刚写的）。
    """
    recorded = Path(row.tts_audio_path) if row.tts_audio_path else None
    if recorded is not None and has_audio(recorded):
        return recorded
    return paths.sentence_wav(task_id, row.seq)


def _write_back(
    repo: SentenceRepo,
    *,
    rows: list[SentenceRow],
    timeline: Timeline,
    task_id: str,
) -> None:
    """把 ``start_ms`` / ``end_ms`` 批量写回 ``script_sentences``（一个事务）。"""
    versions = {row.id: row.version for row in rows}
    spans = [
        TimelineSpan(
            sentence_id=sentence.sentence_id,
            version=versions[sentence.sentence_id],
            start_ms=sentence.start_ms,
            end_ms=sentence.end_ms,
        )
        for sentence in timeline.sentences
    ]
    updated = repo.set_timeline(spans)
    if updated != len(spans):
        raise StudioError(
            f"回写时间轴时有 {len(spans) - updated} 句的文本被改过 ⇒ 这一版时间轴作废",
            code=ErrorCode.STATE_VERSION_CONFLICT,
            context={"task_id": task_id, "expected": len(spans), "updated": updated},
            remediation="重跑一次：新的一版会按改后的句子**全量重算**（不做增量拼接）",
        )


def _record(
    connection: sqlite3.Connection,
    *,
    paths: StudioPaths,
    task_id: str,
    timeline: Timeline,
    timeline_path: Path,
    master: Path,
    measured_ms: int,
) -> None:
    """记两条产物（§03.3.11）：时间轴**永久保留**，母带 24 小时 TTL。

    ``ttl_hint`` 只是给面板看的提示 —— 真正删文件的判据是 ``config/retention.yaml``
    （``gc/media.py``），这里不假装自己管回收。
    """
    repo = ArtifactRepo(connection, data_dir=paths.data_dir)
    repo.record(
        task_id=task_id,
        kind="timeline",
        path=timeline_path,
        ttl_hint=TTL_FOREVER,
        meta={
            "total_ms": timeline.total_ms,
            "tail_ms": timeline.tail_ms,
            "sentences": len(timeline.sentences),
            "seed": timeline.seed,
        },
    )
    repo.record(
        task_id=task_id,
        kind="voice_master",
        path=master,
        ttl_hint=TTL_VOICE_MASTER,
        meta={
            "duration_ms": measured_ms,
            "expected_ms": timeline.master_ms,
            "sample_rate": timeline.sample_rate,
            "channels": timeline.channels,
        },
    )
