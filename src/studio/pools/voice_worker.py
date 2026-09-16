"""配音池的单元处理器（``voice/sentence`` · T2.6）。

``voice/sentence`` 单元 = 「把这一句念出来」
-----------------------------------------
```text
claim(voice/sentence, unit_ref = sentence_id)
   ├─ 读 script_sentences 那一行（文本 / 情感 / 语速 / version 一律**以库为准**）
   ├─ payload 只带**运行期选择**（音色 / seed）—— 它不该重复稿件内容
   ├─ 已经是 done：产物在盘上 ⇒ 直接用；只在缓存里 ⇒ 拷回来（**0 次引擎调用**）；
   │  两处都没了 ⇒ 退回 pending 重念（否则这一句永远没声音，见 _settled_outcome）
   ├─ begin → synthesize_sentence（归一化 → 查缓存 → 引擎 → ffprobe 实测）
   ├─ finish（带 expected_version：稿件被改过就丢弃结果）
   └─ 失败 ⇒ tts_attempts + 1；到 3 次 ⇒ **降级**（等长静音 + 字幕保留），不是失败
```

为什么单元处理器放在 ``pools/`` 而不是 ``services/``
--------------------------------------------------
与 ``draft_worker`` / ``render_worker`` 同一条理由：它实现的是
:class:`~studio.pools.worker_base.UnitHandler` 协议 —— 那是**池**的契约。
它只做三件事：把认领到的 id 翻成服务调用、把失败翻成 ``StudioError``（好让队列的
重试 / 退避 / 死信 / 告警接手）、把结果压成可留痕的摘要。

三条纪律（与渲染池逐条对应）
----------------------------
1. **不自己收尾**。``succeed`` / ``fail`` / 退避 / 死信一律由 :class:`PoolWorker`
   负责。这里唯一的"自己判成功"是**降级**（写静音占位）—— 那不是收尾，那是
   "这一句的产物是静音"这个业务结论（§04.3.3：熔断后任务不失败，转字幕模式）。
2. **可重入**。同一条单元被重投时重念一遍：产物路径由 ``seq`` 决定，重跑覆盖同一
   个文件。短路只认**库里的结论**（``done`` / ``skipped``），再据它去看盘：``done``
   时产物在盘上就收工、只在缓存里就拷回来、两处都没有就**退回待办重念**。
   ``pending`` / ``synthesizing`` 的行一概不看盘 —— 那种状态下盘上的"文件"更可能
   是"上次跑到一半崩了"留下的半截产物，把它当完成就是把整段片子交给一段残缺音频。
3. **不在单元里开池**。缓存 / 引擎 / 音色 / 词表由进程入口装配一次
   （:func:`build_voice_handler`），单元只拿装配好的东西。

音色为什么在装配期解析一次
--------------------------
列音色要起一个 PowerShell（1–2 秒）。放在单元里就是"每念一句都先卡两秒"，
而音色是**进程生命周期内不会变**的环境事实。解析不到 ⇒ 启动时就报
``TTS_ENGINE_UNAVAILABLE``，而不是等第 1 句合成时才失败。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.faults import FaultPlan, fault_plan_from_env
from studio.core.logging import get_logger
from studio.core.paths import StudioPaths
from studio.core.proto import Severity
from studio.db import connect
from studio.db.models import SentenceRow
from studio.db.queue import JobStore
from studio.db.repositories.sentence_repo import DONE_STATUS, SKIPPED_STATUS, SentenceRepo
from studio.domain.enums import UnitType
from studio.pools.worker_base import UnitContext
from studio.services.log_service import LogService
from studio.tts.cache import TtsCache
from studio.tts.faults import wrap_engine
from studio.tts.sapi import pick_voice
from studio.tts.sentence import (
    SapiEngine,
    SentenceEngine,
    copy_audio,
    synthesize_sentence,
    write_placeholder,
)
from studio.tts.text_normalize import GlossaryStore
from studio.tts.timeline import has_audio

__all__ = [
    "DEGRADE_AFTER_ATTEMPTS",
    "VOICE_UNIT_TYPES",
    "VoiceSentenceHandler",
    "VoiceSentenceOutcome",
    "build_voice_handler",
]

logger = get_logger("studio.pools.voice")

#: 本处理器认领的单元类型（``jobs.unit_type``）
VOICE_UNIT_TYPES: Final[frozenset[str]] = frozenset({UnitType.SENTENCE.value})

#: 到几次失败就把这一句降级成静音（§04.3.3 不变量 3：``tts_attempts >= 3 ⇒ skipped``）
DEGRADE_AFTER_ATTEMPTS: Final[int] = 3

#: 进度说明写进 ``result_json`` 时截断到多少字符
_NOTE_CHARS: Final[int] = 80

#: 日志来源（``system_logs.source``）
_LOG_SOURCE: Final[str] = "studio.pools.voice"


@dataclass(frozen=True, slots=True)
class VoiceSentenceOutcome:
    """一次 ``voice/sentence`` 单元的结论（进 ``jobs.result_json``，面板直接读它）。

    ``progress`` 带的是**整个任务**的逐句进度（``12/30``）：面板要显示的是
    "这条片子念到哪儿了"，而不是"这一句念完了" —— 后者每句都是 1/1，等于没信息。
    """

    task_id: str
    job_id: str
    sentence_id: str
    seq: float
    stage: str
    done: int
    total: int
    note: str
    started_at: str
    status: str
    audio_path: str
    duration_ms: int
    sample_rate: int | None
    tts_hash: str
    cache_hit: bool
    engine: str
    voice_id: str | None
    attempts: int
    degraded: bool
    degrade_reason: str | None
    progress: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "job_id": self.job_id,
            "sentence_id": self.sentence_id,
            "seq": self.seq,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "note": self.note,
            "started_at": self.started_at,
            "status": self.status,
            "audio_path": self.audio_path,
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "tts_hash": self.tts_hash,
            "cache_hit": self.cache_hit,
            "engine": self.engine,
            "voice_id": self.voice_id,
            "attempts": self.attempts,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "progress": dict(self.progress),
        }


class VoiceSentenceHandler:
    """``voice/sentence`` 单元的处理器（**进程内一个实例**，跨单元复用）。"""

    unit_types: frozenset[str] = VOICE_UNIT_TYPES

    def __init__(
        self,
        *,
        paths: StudioPaths,
        connection: sqlite3.Connection,
        cache: TtsCache | None = None,
        engine: SentenceEngine | None = None,
        voice: str | None = None,
        glossary: GlossaryStore | None = None,
        log: LogService | None = None,
    ) -> None:
        self._paths = paths
        self._connection = connection
        self._store = JobStore(connection)
        self._repo = SentenceRepo(connection)
        self._cache = cache if cache is not None else TtsCache(paths.tts_cache_dir)
        self._engine = engine if engine is not None else SapiEngine()
        self._voice = voice
        self._glossary = glossary
        self._log = log

    def run(self, ctx: UnitContext) -> Mapping[str, Any]:
        """念一句：库里已是 ``done`` 且音频还在 ⇒ 只把音频找回来；否则合成 + 回填。"""
        sentence = self._repo.get(ctx.unit_ref)
        if sentence is None:
            raise StudioError(
                f"作业引用的句子不存在：{ctx.unit_ref}",
                code=ErrorCode.SCRIPT_NOT_FOUND,
                context={"sentence_id": ctx.unit_ref, "task_id": ctx.task_id},
                remediation="这一句的稿件被删了 ⇒ 检查 jobs.unit_ref 是否已失效，或重排该任务",
            )

        started_at = now_iso()
        state: dict[str, Any] = {
            "task_id": ctx.task_id,
            "sentence_id": sentence.id,
            "seq": sentence.seq,
            "stage": "start",
            "done": 0,
            "total": 0,
            "note": f"开始念第 {sentence.seq} 句",
            "started_at": started_at,
        }
        self._report(ctx, state, sentence)
        self._log_line(ctx, "info", f"voice.sentence 开始（第 {ctx.attempt} 次尝试）")

        settled = self._settled_outcome(sentence, ctx, started_at=started_at)
        if settled is not None:
            self._log_line(ctx, "info", f"voice.sentence 跳过：{settled.note}")
            return settled.to_dict()

        ctx.check_alive()
        outcome = self._synthesize(sentence, ctx, started_at=started_at, state=state)
        ctx.check_alive()
        return outcome.to_dict()

    # ── 内部 ────────────────────────────────────────────────────────────

    def _settled_outcome(
        self, sentence: SentenceRow, ctx: UnitContext, *, started_at: str
    ) -> VoiceSentenceOutcome | None:
        """已定局的句子**不重念**：``done`` 把音频找回来，``skipped`` 原样收工。

        ``done`` 的承诺是"这一句的音频已经存在"，所以要顺着**两处盘上的东西**验一遍：

        1. 句子 WAV 还在 ⇒ 直接收工（重跑一条刚做完的任务走的就是这条路）；
        2. 不在，但缓存里有（§03.7.5 说句子 WAV 在任务完成后 24 小时删除、只留缓存
           副本）⇒ 拷回来，仍然 0 次引擎调用；
        3. 两处都没了（缓存也被 LRU 淘汰了）⇒ **不能假装命中**。把状态如实退回待办，
           让这一句重念 —— 否则交出去的是一份指向空气的 ``tts_audio_path``，成片里
           那一句没声音，而且没有任何地方会报错。

        第 3 条**只退回、不就地重念**：重念要经过 :meth:`_synthesize` 的 ``begin``
        —— 竞态闸门与失败记账都在那条路上。
        """
        if sentence.tts_status == SKIPPED_STATUS:
            return self._outcome(
                sentence,
                ctx,
                started_at=started_at,
                status=SKIPPED_STATUS,
                audio_path=sentence.tts_audio_path or "",
                duration_ms=sentence.tts_duration_ms or 0,
                sample_rate=sentence.tts_sample_rate,
                tts_hash=sentence.tts_hash or "",
                cache_hit=False,
                degraded=True,
                degrade_reason=sentence.tts_error or "已降级为静音占位",
                note="这一句是降级占位，不重念",
            )

        if sentence.tts_status != DONE_STATUS:
            return None

        target = self._paths.sentence_wav(ctx.task_id, int(sentence.seq))
        if has_audio(target):
            return self._restored(
                sentence,
                ctx,
                started_at=started_at,
                audio_path=target,
                cache_hit=False,
                note="这一句已经念过（产物在盘上）",
            )

        cached = self._cache.get(sentence.tts_hash) if sentence.tts_hash else None
        if cached is not None:
            copy_audio(cached, target)
            return self._restored(
                sentence,
                ctx,
                started_at=started_at,
                audio_path=target,
                cache_hit=True,
                note="已念过，从缓存取回（0 次引擎调用）",
            )

        self._repo.reopen(sentence.id, expected_version=sentence.version)
        return None

    def _restored(
        self,
        sentence: SentenceRow,
        ctx: UnitContext,
        *,
        started_at: str,
        audio_path: Path,
        cache_hit: bool,
        note: str,
    ) -> VoiceSentenceOutcome:
        """``done`` 的两种"不用念"（产物在盘上 / 从缓存拷回来）压成同一份结论。"""
        return self._outcome(
            sentence,
            ctx,
            started_at=started_at,
            status=DONE_STATUS,
            audio_path=audio_path.as_posix(),
            duration_ms=sentence.tts_duration_ms or 0,
            sample_rate=sentence.tts_sample_rate,
            tts_hash=sentence.tts_hash or "",
            cache_hit=cache_hit,
            degraded=False,
            degrade_reason=None,
            note=note,
        )

    def _synthesize(
        self,
        sentence: SentenceRow,
        ctx: UnitContext,
        *,
        started_at: str,
        state: dict[str, Any],
    ) -> VoiceSentenceOutcome:
        payload = ctx.payload
        voice = _opt_str(payload, "voice") or self._voice
        seed = _opt_int(payload, "seed")
        target = self._paths.sentence_wav(ctx.task_id, int(sentence.seq))

        if not self._repo.begin(sentence.id, engine=self._engine.name, voice_id=voice):
            # ``begin`` 被拒只有一种可能：这一句在**读行之后**被别人做完了
            # （并发投递或人工重投）。那不是错误 —— 如实报"已定局"即可。
            fresh = self._repo.get(sentence.id)
            if fresh is not None and fresh.tts_status in (DONE_STATUS, SKIPPED_STATUS):
                return self._outcome(
                    fresh,
                    ctx,
                    started_at=started_at,
                    status=fresh.tts_status,
                    audio_path=fresh.tts_audio_path or "",
                    duration_ms=fresh.tts_duration_ms or 0,
                    sample_rate=fresh.tts_sample_rate,
                    tts_hash=fresh.tts_hash or "",
                    cache_hit=False,
                    degraded=fresh.tts_status == SKIPPED_STATUS,
                    degrade_reason=fresh.tts_error,
                    note="这一句已被别的单元做完",
                )
            raise StudioError(
                f"句子 {sentence.id} 无法进入合成状态（tts_status={sentence.tts_status}）",
                code=ErrorCode.STATE_TRANSITION_ILLEGAL,
                context={"sentence_id": sentence.id, "tts_status": sentence.tts_status},
                remediation="检查是不是有人手工改了 script_sentences.tts_status",
            )

        try:
            synthesis = synthesize_sentence(
                sentence.tts_text or sentence.text,
                out_path=target,
                cache=self._cache,
                engine=self._engine,
                voice=voice,
                speed=sentence.speed,
                emotion=sentence.emotion,
                seed=seed,
                glossary=self._glossary.current() if self._glossary is not None else None,
            )
        except StudioError as exc:
            return self._on_failure(sentence, ctx, exc, started_at=started_at, state=state, voice=voice)

        written = self._repo.finish(
            sentence.id,
            expected_version=sentence.version,
            audio_path=synthesis.audio_path.as_posix(),
            duration_ms=synthesis.duration_ms,
            sample_rate=synthesis.sample_rate,
            tts_hash=synthesis.tts_hash,
            engine=synthesis.engine,
            voice_id=synthesis.voice_id,
        )
        if not written:
            raise StudioError(
                f"这一句在合成期间被改过（sentence {sentence.id}）",
                code=ErrorCode.STATE_VERSION_CONFLICT,
                context={"sentence_id": sentence.id, "version": sentence.version},
                remediation="无需处理：队列会重试，重试时读到的就是新文本",
            )

        self._log_line(
            ctx,
            "info",
            f"voice.sentence 完成：s{int(sentence.seq):03d}.wav"
            f"{'（缓存命中）' if synthesis.cache_hit else ''}",
        )
        return self._outcome(
            sentence,
            ctx,
            started_at=started_at,
            status=DONE_STATUS,
            audio_path=synthesis.audio_path.as_posix(),
            duration_ms=synthesis.duration_ms,
            sample_rate=synthesis.sample_rate,
            tts_hash=synthesis.tts_hash,
            cache_hit=synthesis.cache_hit,
            degraded=False,
            degrade_reason=None,
            note=("缓存命中，未调用引擎" if synthesis.cache_hit else "合成完成"),
            state=state,
        )

    def _on_failure(
        self,
        sentence: SentenceRow,
        ctx: UnitContext,
        exc: StudioError,
        *,
        started_at: str,
        state: dict[str, Any],
        voice: str | None,
    ) -> VoiceSentenceOutcome:
        """记一次失败；到线就**降级成静音占位**（成功），否则抛出去让队列重试。"""
        attempts = self._repo.fail(
            sentence.id, expected_version=sentence.version, error=f"{exc.code}: {exc.message}"[:200]
        )
        if attempts is None:
            # 稿件在合成期间被改了：这次失败属于一份**已经不存在的稿子**，不记账。
            # 抛出去让队列重试 —— 重试会重新读行，于是自然念上新文本。
            raise StudioError(
                f"这一句在合成期间被改过，丢弃本次结果（sentence {sentence.id}）",
                code=ErrorCode.STATE_VERSION_CONFLICT,
                context={"sentence_id": sentence.id, "version": sentence.version},
                remediation="无需处理：队列会重试，重试时读到的就是新文本",
            ) from exc

        self._log_line(ctx, "warn", f"voice.sentence 失败（第 {attempts} 次）：{exc.message}")
        if attempts < DEGRADE_AFTER_ATTEMPTS:
            state.update(
                note=f"失败（第 {attempts} 次）：{exc.message}",
                error_code=str(exc.code),
                remediation=exc.remediation,
            )
            self._report(ctx, state, sentence)
            raise exc

        # 到线 ⇒ 降级：等长静音占位 + 字幕保留（§04.3.3 的 PLACEHOLDER）。
        # 这是**成功**的一种：任务继续往下走（字幕模式），而不是卡在配音这一步。
        target = self._paths.sentence_wav(ctx.task_id, int(sentence.seq))
        measured = write_placeholder(sentence.tts_text or sentence.text, out_path=target)
        skipped = self._repo.skip(
            sentence.id,
            expected_version=sentence.version,
            audio_path=target.as_posix(),
            duration_ms=measured,
            error=f"连续 {attempts} 次失败 ⇒ 静音占位（最后一次：{exc.message}）",
        )
        if not skipped:
            raise StudioError(
                f"这一句在降级期间被改过（sentence {sentence.id}）",
                code=ErrorCode.STATE_VERSION_CONFLICT,
                context={"sentence_id": sentence.id, "version": sentence.version},
                remediation="无需处理：队列会重试，重试时读到的就是新文本",
            ) from exc

        self._log_line(ctx, "warn", f"voice.sentence 降级为静音占位（{measured}ms）")
        return self._outcome(
            sentence,
            ctx,
            started_at=started_at,
            status=SKIPPED_STATUS,
            audio_path=target.as_posix(),
            duration_ms=measured,
            sample_rate=None,
            tts_hash="",
            cache_hit=False,
            degraded=True,
            degrade_reason=f"连续 {attempts} 次合成失败：{exc.message}",
            note=f"降级为静音占位（{measured}ms），字幕保留",
            state=state,
            voice=voice,
        )

    def _outcome(
        self,
        sentence: SentenceRow,
        ctx: UnitContext,
        *,
        started_at: str,
        status: str,
        audio_path: str,
        duration_ms: int,
        sample_rate: int | None,
        tts_hash: str,
        cache_hit: bool,
        degraded: bool,
        degrade_reason: str | None,
        note: str,
        state: Mapping[str, Any] | None = None,
        voice: str | None = None,
    ) -> VoiceSentenceOutcome:
        """压一份结论 + 把**整个任务**的逐句进度写进 ``result_json``。"""
        progress = self._repo.progress(ctx.task_id)
        final_state: dict[str, Any] = dict(state or {})
        final_state.update(
            stage="done" if not degraded else "degraded",
            done=progress.settled,
            total=progress.total,
            note=_clip(note),
        )
        self._report(ctx, final_state, sentence)
        return VoiceSentenceOutcome(
            task_id=ctx.task_id,
            job_id=ctx.job_id,
            sentence_id=sentence.id,
            seq=sentence.seq,
            stage="done" if not degraded else "degraded",
            done=progress.settled,
            total=progress.total,
            note=note,
            started_at=started_at,
            status=status,
            audio_path=audio_path,
            duration_ms=duration_ms,
            sample_rate=sample_rate,
            tts_hash=tts_hash,
            cache_hit=cache_hit,
            engine=self._engine.name,
            voice_id=voice if voice is not None else self._voice,
            attempts=sentence.tts_attempts,
            degraded=degraded,
            degrade_reason=degrade_reason,
            progress=progress.to_dict(),
        )

    def _report(
        self, ctx: UnitContext, state: Mapping[str, Any], sentence: SentenceRow | None = None
    ) -> None:
        """把进度整份写进 ``jobs.result_json``（写不进去只说明租约丢了，**不是错误**）。"""
        payload = dict(state)
        if sentence is not None:
            payload.setdefault("sentence_id", sentence.id)
            payload.setdefault("seq", sentence.seq)
        payload.setdefault("started_at", now_iso())
        self._store.report_progress(job_id=ctx.job_id, worker_id=ctx.worker_id, result=payload)

    def _log_line(self, ctx: UnitContext, level: Severity, message: str) -> None:
        if self._log is None:
            return
        self._log.append(
            level=level,
            source=_LOG_SOURCE,
            message=message,
            task_id=ctx.task_id,
            job_id=ctx.job_id,
            worker_id=ctx.worker_id,
        )


def build_voice_handler(
    *,
    paths: StudioPaths,
    log: LogService | None = None,
    connection: sqlite3.Connection | None = None,
    engine: SentenceEngine | None = None,
    voice: str | None = None,
    fault: FaultPlan | None = None,
) -> VoiceSentenceHandler:
    """配音池进程入口的装配（``workers/run_voice.py`` 调它）。

    连接**默认自己开一条、故意不关**：它挂着进度写入、句级回填与日志，而池 worker
    是"起一次、跑到关停"的常驻进程（与 ``build_render_handler`` 同一条）。

    ``voice`` 缺省在**装配期**解析一次（列音色要起 PowerShell，1–2 秒）：放进单元
    里就是"每念一句先卡两秒"，而音色是进程生命周期内不变的环境事实。解析不到
    ⇒ 现在就报，别等第 1 句才失败。

    ``log`` 缺省**不是"不打日志"**，而是挂一条落在同一条连接上的 :class:`LogService`
    —— 面板的日志尾巴读的就是 ``system_logs``。

    ``fault`` 缺省从 ``STUDIO_FAULT`` 读（T2.8 的降级演练）。**在装配期读一次**：
    故障是"这次进程启动时定的环境事实"，与音色同理 —— 每念一句重读一遍只会让
    "演练到一半改了环境变量"变成一个没人能复现的现象。
    """
    resolved = connection if connection is not None else connect(paths.db_file)
    chosen = engine if engine is not None else SapiEngine()
    plan = fault if fault is not None else fault_plan_from_env()
    if plan.enabled:
        logger.warning("voice.fault_injected", **plan.to_dict())
    return VoiceSentenceHandler(
        paths=paths,
        connection=resolved,
        engine=wrap_engine(chosen, plan),
        voice=voice if voice is not None else _resolve_voice(),
        glossary=GlossaryStore(paths.glossary_file),
        log=log if log is not None else LogService(resolved),
    )


def _resolve_voice() -> str | None:
    """挑一个本机装了的音色；一个都没有 ⇒ 抛（**启动期**就该知道）。"""
    chosen = pick_voice()
    if chosen is None:
        raise StudioError(
            "本机没有可用的语音音色，配音池无法工作",
            code=ErrorCode.TTS_ENGINE_UNAVAILABLE,
            context={"engine": "sapi"},
            remediation=(
                "在「设置 → 时间和语言 → 语音」里装一个中文语音包（如 Microsoft Huihui），"
                "或给 pools.yaml 的 voice 池配一个已装音色"
            ),
        )
    return chosen


def _opt_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value != "" else None


def _opt_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    # ``bool`` 是 ``int`` 的子类：`"seed": true` 会悄悄变成 1，得先挡掉。
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _clip(note: str) -> str:
    """进度说明截断（``result_json`` 是每行作业都要存一份的东西，不装长文本）。"""
    flat = " ".join(note.split())
    if len(flat) <= _NOTE_CHARS:
        return flat
    return flat[: _NOTE_CHARS - 1] + "…"
