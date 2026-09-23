"""配音池的单元处理器（``voice/sentence`` · T2.6）。

``voice/sentence`` 单元 = 「把这一句念出来」
-----------------------------------------
```text
claim(voice/sentence, unit_ref = sentence_id)
   ├─ 读 script_sentences 那一行（文本 / 情感 / 语速 / version 一律**以库为准**）
   ├─ payload 只带**运行期选择**（音色 / seed）—— 它不该重复稿件内容
   ├─ 已经是 done：产物在盘上 ⇒ 直接用；只在缓存里 ⇒ 拷回来（**0 次引擎调用**）；
   │  两处都没了 ⇒ 退回 pending 重念（否则这一句永远没声音，见 _settled_outcome）
   ├─ begin → synthesize_sentence（归一化 → 查缓存 → 引擎 → ffprobe 实测 → 音频 QC）
   ├─ finish（带 expected_version：稿件被改过就丢弃结果）
   └─ 失败 ⇒ tts_attempts + 1 ⇒ **决策表**（T2.3）决定下一步做什么：
       重试 / 唤醒引擎 / 简化重试 / 再切分 / 换引擎 / 静音占位 / 放弃任务
```

失败之后做什么，由决策表说了算（T2.3 · §04.3.3）
------------------------------------------------
本轮之前，这里只有一条规则："失败 ⇒ 到 3 次 ⇒ 静音占位"。于是"引擎显存炸了"
与"这一句太长念不完"得到**同一个**处置 —— 而它们该做的事正好相反（前者要卸了重载，
后者要再切一刀）。现在分流在 :func:`~studio.tts.fallback.decide`（纯函数、逐条可测），
这里只负责**执行**它选出来的那个动作。

三个动作是"就地再干一次活"（唤醒引擎 / 简化重试 / 再切分），四个是"交给队列"
（原样重试 / 换引擎后重试 / 占位 / 放弃）。区别在于：需要**跨单元记住**的东西
（这一句已经简化过、已经切过）由本处理器持有；不需要记的直接抛出去让队列退避重试。

熔断（:class:`~studio.tts.circuit.CircuitBreaker`）是**池级**的
----------------------------------------------------------
"连续 3 句念不出来"与"这一句失败了 3 次"是两件事：前者说明**这台引擎现在不能念**
（每一句都会白等一轮 60 秒超时 + 退避），后者说明**这一句有问题**。前者一旦成立，
剩下的句子不再调用引擎 —— 但**缓存还是要查**（那一句别人念过的话，音频就在盘上，
不查白不查，见 ``synthesize_sentence(cache_only=…)``）。

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

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from studio.core.clock import now_iso
from studio.core.errors import ErrorCode, StudioError
from studio.core.faults import FaultPlan, fault_plan_from_env
from studio.core.logging import get_logger
from studio.core.media import probe_media
from studio.core.paths import StudioPaths
from studio.core.proto import AlertCode, Severity
from studio.db import connect
from studio.db.models import SentenceRow
from studio.db.queue import JobStore
from studio.db.repositories.sentence_repo import DONE_STATUS, SKIPPED_STATUS, SentenceRepo
from studio.domain.enums import UnitType
from studio.domain.text import split_long_sentence
from studio.pools.worker_base import UnitContext
from studio.services.log_service import LogService
from studio.tts.cache import TtsCache
from studio.tts.circuit import CircuitBreaker
from studio.tts.engine import VoiceEngine, rewarm
from studio.tts.engine_picker import EnginePicker, resolve_sapi_voice
from studio.tts.fallback import (
    DEGRADE_AFTER_ATTEMPTS,
    DefaultFallbackPolicy,
    DegradeAction,
    FailureState,
    FallbackPolicy,
    action_note,
)
from studio.tts.faults import wrap_engine
from studio.tts.refprint import voice_fingerprint
from studio.tts.sentence import (
    SapiEngine,
    SentenceEngine,
    copy_audio,
    synthesize_sentence,
    write_placeholder,
)
from studio.tts.synth import concat_wavs
from studio.tts.text_normalize import GlossaryStore
from studio.tts.timeline import has_audio

__all__ = [
    "DEGRADE_AFTER_ATTEMPTS",
    "VOICE_UNIT_TYPES",
    "VoiceSentenceHandler",
    "VoiceSentenceOutcome",
    "build_voice_handler",
]

# ``DEGRADE_AFTER_ATTEMPTS`` 的**定义**已经下沉到 :mod:`studio.tts.fallback`
# （决策表要用它），这里原样 re-export：调用方写的
# ``from studio.pools.voice_worker import DEGRADE_AFTER_ATTEMPTS`` 一行都不用改。

logger = get_logger("studio.pools.voice")

#: 本处理器认领的单元类型（``jobs.unit_type``）
VOICE_UNIT_TYPES: Final[frozenset[str]] = frozenset({UnitType.SENTENCE.value})

#: 进度说明写进 ``result_json`` 时截断到多少字符
_NOTE_CHARS: Final[int] = 80

#: 日志来源（``system_logs.source``）
_LOG_SOURCE: Final[str] = "studio.pools.voice"

#: 池名（告警的 ``stage`` / ``source`` 用它，与 ``config/pools.yaml`` 的键一致）
POOL_NAME: Final[str] = "voice"

#: ``SPLIT_AND_MERGE`` 里"再切一刀"的目标长度（字）。与写稿那一步的上限
#: （``ScriptRules`` 的句长阈值）同一条口径：切分**比重新合成便宜**，所以宁可
#: 多切一句，也不要再赌一次超时。
SPLIT_LIMIT_CHARS: Final[int] = 20

#: 分片合成的产物放在 ``data/work/<task_id>/tts_split/``（**不是**交付目录）。
#: 交付目录（``output/voice/<task_id>/``）里每一份都是"这一句的音频"，多出来的
#: 分片会让"盘上有几个文件"与"稿子有几句话"对不上 —— 而拼接那一步的产物才叫
#: ``s00N.wav``（§04.3.3 不变量 1）。
_SPLIT_DIRNAME: Final[str] = "tts_split"


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
        engine_picker: EnginePicker | None = None,
        voice: str | None = None,
        glossary: GlossaryStore | None = None,
        log: LogService | None = None,
        breaker: CircuitBreaker | None = None,
        policy: FallbackPolicy | None = None,
    ) -> None:
        self._paths = paths
        self._connection = connection
        self._store = JobStore(connection)
        self._repo = SentenceRepo(connection)
        self._cache = cache if cache is not None else TtsCache(paths.tts_cache_dir)
        self._engine = engine if engine is not None else SapiEngine()
        self._engine_picker = engine_picker
        self._voice = voice
        self._glossary = glossary
        self._log = log
        self._breaker = breaker if breaker is not None else CircuitBreaker()
        self._policy = policy if policy is not None else DefaultFallbackPolicy()
        #: 这一句已经用过哪些招（**进程内**，见 `FailureState` 的说明）。
        #: 两处都只在降级链里有意义，所以不落库 —— 进程重启后多试一次而已。
        self._simplified: set[str] = set()
        self._split: set[str] = set()

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

    def _current_engine(self) -> tuple[SentenceEngine, str | None, tuple[str, ...] | None]:
        """这一次用哪台引擎 + 它认的兜底音色 + 它念得出来的音色。

        注入的引擎（``engine_picker is None``）⇒ 返回装配期定的那一对，音色不判
        （"就用这一个"是明确指令，判了反而会把注入的假件顶掉，裁定 311）。
        """
        if self._engine_picker is None:
            return self._engine, self._voice, None
        engine, fallback, speakable = self._engine_picker()
        self._engine = engine  # 报告与失败留痕读的都是它（_outcome / _on_failure）
        return engine, fallback, speakable

    def _synthesize(
        self,
        sentence: SentenceRow,
        ctx: UnitContext,
        *,
        started_at: str,
        state: dict[str, Any],
    ) -> VoiceSentenceOutcome:
        payload = ctx.payload
        engine, fallback_voice, speakable = self._current_engine()
        voice = _opt_str(payload, "voice")
        if voice is not None and speakable is not None and voice not in speakable:
            # payload 里那个音色是**面板算的时候**能念的，而引擎可能在那之后换了
            # （真机：模型要加载 22s，池子先起）。硬交给现在的引擎只会每句失败 3 次、
            # 降级成静音 —— 成片没人声，而库里写着"换音色成功"（陷阱 #154 的另一半）。
            self._log_line(ctx, "warn", f"音色 {voice} 当前引擎念不出来 ⇒ 改用 {fallback_voice}")
            voice = None
        voice = voice if voice is not None else fallback_voice
        seed = _opt_int(payload, "seed")
        target = self._paths.sentence_wav(ctx.task_id, int(sentence.seq))

        # 熔断：闸门开着 ⇒ **不调引擎**（`cache_only`），但缓存照查 —— 那一句
        # 别人念过的话，音频就在盘上，白丢可惜（见 `synthesize_sentence` 的说明）。
        allowed = self._breaker.allow()
        if not allowed:
            self._log_line(ctx, "warn", "配音熔断中（连续多句念不出来）⇒ 这一句只查缓存，不调引擎")
        simplified = sentence.id in self._simplified
        if simplified:
            self._log_line(ctx, "info", "这一句上一轮已简化 ⇒ 去 emotion / speed=1.0 / 换 seed")

        if not self._repo.begin(sentence.id, engine=engine.name, voice_id=voice):
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
                engine=engine,
                voice=voice,
                # 参考音指纹进缓存键：``voice`` 只是**名字**，而用户换参考音时名字
                # 不变（删掉重传 / 勾「覆盖同名」重传，裁定 381）—— 只按名字记的话，
                # 换了嗓子之后每一句都命中旧音频，而库里写着「换音色成功」。
                # 这里逐句算：``refprint`` 内部按 (大小, mtime) 记忆化，实际只在
                # 参考音真的变了之后重哈希一次。
                voice_fingerprint=voice_fingerprint(self._paths.voice_src_dir, voice or ""),
                # 简化重试：去 emotion、语速回正、换一个**确定性**的 seed。
                # 换 seed 不是仪式 —— 它进缓存键，不换的话"重试"会命中上一次那段
                # 坏音频（`RETRY_SIMPLIFIED` 于是变成"读一遍缓存再失败一次"）。
                speed=1.0 if simplified else sentence.speed,
                emotion="neutral" if simplified else (sentence.emotion or "neutral"),
                seed=_simplified_seed(sentence) if simplified else seed,
                glossary=self._glossary.current() if self._glossary is not None else None,
                cache_only=not allowed,
            )
        except StudioError as exc:
            return self._on_failure(sentence, ctx, exc, started_at=started_at, state=state, voice=voice)
        if not synthesis.cache_hit:
            # 真的念出来了一句 ⇒ 引擎此刻是好的（缓存命中不算：它没碰引擎）
            self._breaker.record_success()

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
        """记一次失败 ⇒ **问决策表** ⇒ 执行它选出来的那个动作（T2.3 · §04.3.3）。

        七个动作在这里分成两类
        ----------------------
        **就地做完**（唤醒引擎 / 简化重试 / 再切分 / 占位）：前三件是"现在就该做的事"
        （卸了重载、换个采样点、把长句切开），等一轮退避只会把 20 秒的加载拖成
        20 秒 + 退避；占位是终点。

        **交给队列**（原样重试 / 换引擎后重试 / 放弃任务）：它们要么需要**退避**
        （给引擎喘息），要么需要**下一次执行时用不同的参数**（换引擎后那一句要
        重新解析音色 —— 那是单元开头的事）。抛出去即可。
        """
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

        # 熔断器记的是"**连续几句**念不出来"（池级），与"这一句第几次失败"是两件事：
        # 同一句重试三次也只是一句没念出来，而它判的是"这台引擎现在还能不能用"。
        # 所以传 `sentence.id` 让它**按句去重** —— 否则一句难念的台词会自己把闸门
        # 拉开，把整条片子剩下的句子一起送进静音。
        if self._breaker.record_failure(sentence.id):
            self._alert_circuit_open(ctx, attempts=attempts, exc=exc)

        action = self._decide(exc, sentence=sentence, attempts=attempts)
        self._log_line(ctx, "info", f"voice.sentence 处置：{action.value}（{action_note(action)}）")

        if action is DegradeAction.PLACEHOLDER:
            return self._placeholder(
                sentence, ctx, exc, started_at=started_at, state=state, voice=voice, attempts=attempts
            )

        if action is DegradeAction.FAIL_TASK:
            # **不降级**（§04.3.3 的"音色缺失"那一行）：把它做成静音占位的话，用户
            # 拿到的是一支"出片成功、整片没人声"的片子 —— 而真正的错因（音色名写错）
            # 会被"这条片子进了字幕模式"盖过去。响亮地失败，让 pipeline 停下来报它。
            self._log_line(ctx, "error", f"这一句不降级（配置错误）：{exc.message}")
            self._report(
                ctx,
                {**state, "note": _clip(f"配置错误，不降级：{exc.message}"), "action": action.value},
                sentence,
            )
            raise exc

        if action is DegradeAction.REWARM_ENGINE:
            self._rewarm(ctx)
        elif action is DegradeAction.RETRY_SIMPLIFIED:
            self._simplified.add(sentence.id)
        elif action is DegradeAction.SPLIT_AND_MERGE:
            outcome = self._try_split(sentence, ctx, exc, started_at=started_at, state=state, voice=voice)
            if outcome is not None:
                return outcome
            # 切了还是不行 ⇒ 交给下一次：那时 `split=True`，决策表会到线降级。
            # **不在这里再试一遍**：一次单元执行里试两次等于把退避机制绕过去了。
            self._log_line(ctx, "warn", "切分后仍然失败 ⇒ 交给队列重试（下一次直接到线降级）")
        elif action is DegradeAction.SWITCH_ENGINE:
            self._switch_engine(ctx)

        state.update(
            note=_clip(f"失败（第 {attempts} 次）⇒ {action_note(action)}：{exc.message}"),
            error_code=str(exc.code),
            action=action.value,
            remediation=exc.remediation,
        )
        self._report(ctx, state, sentence)
        raise exc

    def _decide(self, exc: StudioError, *, sentence: SentenceRow, attempts: int) -> DegradeAction:
        """问决策表。

        闸门开着的时候**不问**：表里每一个动作都要碰引擎（唤醒 / 重试 / 切分后再念 /
        换引擎），而闸门就是"别碰引擎"这个结论本身 —— 这时候唯一该做的是占位收工。
        """
        if self._breaker.blocking():
            return DegradeAction.PLACEHOLDER
        return self._policy.decide(
            code=str(exc.code),
            state=FailureState(
                attempt=attempts,
                simplified=sentence.id in self._simplified,
                split=sentence.id in self._split,
                switched=self._engine_switched(),
                split_available=self._split_available(sentence),
            ),
        )

    def _split_available(self, sentence: SentenceRow) -> bool:
        """这一句当前的文本切得动吗（**实时算**：切分判据不该是一份可能过时的状态）。"""
        text = sentence.tts_text or sentence.text
        return len(split_long_sentence(text, limit=SPLIT_LIMIT_CHARS)) > 1

    def _engine_switched(self) -> bool:
        """已经强制降档到备用引擎了吗（引擎级事实，问 picker）。"""
        return self._engine_picker is not None and self._engine_picker.switched

    def _rewarm(self, ctx: UnitContext) -> None:
        """唤醒引擎（``REWARM_ENGINE``）：卸了再载。

        **失败只记日志，不改抛出去的那个异常**：唤醒失败说明这台引擎叫不醒，而
        "这一句为什么失败"仍然是原来那个原因（显存 / 崩溃）—— 换成"唤醒失败"会让
        真正的原因从 ``tts_error`` 里消失，而决策表下一轮正是按那个码分流的。
        """
        engine, _voice, _speakable = self._current_engine()
        if not isinstance(engine, VoiceEngine):
            # 只实现 `synthesize` 的引擎（测试假件、以及将来某个最小实现）没有
            # 生命周期原语可调。**跳过而不是报错**：决策表选这张牌是因为"引擎可能
            # 是坏的"，而"这台引擎压根没有显存可卸"是个正当答案 —— 抛出去只会让
            # 一次正常的降级链变成一条新故障。
            self._log_line(ctx, "warn", f"引擎 {engine.name} 没有生命周期原语 ⇒ 跳过唤醒")
            return
        try:
            rewarm(engine)
        except Exception as exc:
            self._log_line(ctx, "warn", f"唤醒引擎失败：{type(exc).__name__}: {exc}")
            return
        self._log_line(ctx, "info", f"已唤醒引擎 {engine.name}（卸载后重载）")

    def _switch_engine(self, ctx: UnitContext) -> None:
        """切备用引擎（``SWITCH_ENGINE``）：常驻服务这台别用了，改走系统语音包。"""
        if self._engine_picker is None:
            self._log_line(ctx, "warn", "没有可切换的引擎（引擎是注入的）⇒ 继续用当前这台")
            return
        if not self._engine_picker.switch():
            self._log_line(ctx, "warn", "已经在备用引擎上了 ⇒ 继续用当前这台")
            return
        engine, voice, _speakable = self._current_engine()
        self._log_line(ctx, "warn", f"已切到备用引擎 {engine.name}（兜底音色 {voice}）")

    def _alert_circuit_open(self, ctx: UnitContext, *, attempts: int, exc: StudioError) -> None:
        """闸门拉开 ⇒ 发 ``TTS_CIRCUIT_OPEN``（error 级，WS 层永不合并）。

        **只在拉开的那一拍发**（``record_failure()`` 返回 ``True`` 时才走到这里）：
        每句都发的话，三十句的任务会往日志里灌三十条一样的告警 —— 而告警面板正是
        靠"稀有"来工作的。
        """
        if self._log is None:
            return
        snapshot = self._breaker.snapshot()
        self._log.alert(
            AlertCode.TTS_CIRCUIT_OPEN,
            message=(
                f"配音连续 {snapshot.consecutive_failures} 句念不出来 ⇒ 熔断 "
                f"{snapshot.open_sec:.0f}s（这一句第 {attempts} 次失败：{exc.message}）"
            ),
            hint=(
                "闸门打开期间剩下的句子走静音占位（字幕模式），**任务不失败**；"
                "到点自动半开一次探测。看 data/logs/tts.log 与 `studio service status`；"
                "修完引擎重启配音池可立刻清零"
            ),
            task_id=ctx.task_id,
            job_id=ctx.job_id,
            pool=POOL_NAME,
        )

    def _try_split(
        self,
        sentence: SentenceRow,
        ctx: UnitContext,
        exc: StudioError,
        *,
        started_at: str,
        state: dict[str, Any],
        voice: str | None,
    ) -> VoiceSentenceOutcome | None:
        """再切一刀、分段合成、拼成一份交付音频（§04.3.3 的 ``SPLIT_AND_MERGE``）。

        ⇒ 成功时给这一句的结论（**``done``**，不是失败）；切不动 / 切了还是失败 ⇒ ``None``
        （调用方交给队列重试，那时 ``split=True`` ⇒ 决策表到线降级）。

        分片落在 ``data/work/<task_id>/tts_split/``，**不是交付目录**：交付目录里
        每一份都对应稿子的一句话，多出来的分片会让"盘上有几个文件"与"稿子有几句话"
        对不上（§04.3.3 不变量 1 说的是"文件名由 seq 决定"）。
        """
        text = sentence.tts_text or sentence.text
        pieces = split_long_sentence(text, limit=SPLIT_LIMIT_CHARS)
        if len(pieces) < 2:
            # 决策那一刻判"切得动"、真到切的时候切不动（文本在这中间被改过）⇒ 不硬来
            return None

        self._split.add(sentence.id)
        engine, _fallback, _speakable = self._current_engine()
        target = self._paths.sentence_wav(ctx.task_id, int(sentence.seq))
        split_dir = self._paths.work_dir_for(ctx.task_id) / _SPLIT_DIRNAME
        glossary = self._glossary.current() if self._glossary is not None else None
        try:
            parts: list[Path] = []
            for index, piece in enumerate(pieces, start=1):
                part = split_dir / f"s{int(sentence.seq):03d}_p{index}.wav"
                synthesize_sentence(
                    piece,
                    out_path=part,
                    cache=self._cache,
                    engine=engine,
                    voice=voice,
                    speed=sentence.speed,
                    emotion=sentence.emotion or "neutral",
                    glossary=glossary,
                )
                parts.append(part)
            concat_wavs(parts, target)
            info = probe_media(target)
        except StudioError as split_exc:
            self._log_line(ctx, "warn", f"切分合成失败：{split_exc.message}")
            return None

        written = self._repo.finish(
            sentence.id,
            expected_version=sentence.version,
            audio_path=target.as_posix(),
            duration_ms=info.duration_ms,
            sample_rate=info.sample_rate,
            # 切分产物是**多段拼起来的**，没有单一的缓存键可记。留空是诚实的：
            # 它不会命中任何缓存，也不会假装命中（见 `_settled_outcome` 的第三条）。
            tts_hash="",
            engine=engine.name,
            voice_id=voice,
        )
        if not written:
            raise StudioError(
                f"这一句在切分合成期间被改过（sentence {sentence.id}）",
                code=ErrorCode.STATE_VERSION_CONFLICT,
                context={"sentence_id": sentence.id, "version": sentence.version},
                remediation="无需处理：队列会重试，重试时读到的就是新文本",
            ) from exc

        self._breaker.record_success()
        self._log_line(ctx, "info", f"voice.sentence 切分合成完成：{len(pieces)} 段 ⇒ {info.duration_ms}ms")
        return self._outcome(
            sentence,
            ctx,
            started_at=started_at,
            status=DONE_STATUS,
            audio_path=target.as_posix(),
            duration_ms=info.duration_ms,
            sample_rate=info.sample_rate,
            tts_hash="",
            cache_hit=False,
            degraded=False,
            degrade_reason=None,
            note=f"再切分后分段合成（{len(pieces)} 段）",
            state=state,
            voice=voice,
        )

    def _placeholder(
        self,
        sentence: SentenceRow,
        ctx: UnitContext,
        exc: StudioError,
        *,
        started_at: str,
        state: dict[str, Any],
        voice: str | None,
        attempts: int,
    ) -> VoiceSentenceOutcome:
        """降级：等长静音占位 + 字幕保留（§04.3.3 的 ``PLACEHOLDER``）。

        这是**成功**的一种：任务继续往下走（字幕模式），而不是卡在配音这一步
        （§04.3.3："熔断后任务不失败，改为字幕模式继续产出"）。
        """
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
    breaker: CircuitBreaker | None = None,
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

    ``breaker`` 缺省**自己建一个**（T2.3 的熔断）：它是**进程级**状态，一个池进程
    共用一只闸门 —— 每句新建一只的话，计数器永远是 1，闸门永远打不开（而那正是
    "连续 3 句念不出来"这条判据唯一要防的事）。注入点留给测试与运维。
    """
    plan = fault if fault is not None else fault_plan_from_env()
    if plan.enabled:
        logger.warning("voice.fault_injected", **plan.to_dict())
    resolved = connection if connection is not None else connect(paths.db_file)
    if engine is not None:
        # 注入的引擎 = "就用这一个"（测试 / 演练）⇒ 不探测（裁定 311）
        chosen: SentenceEngine = wrap_engine(engine, plan)
        default_voice: str | None = resolve_sapi_voice()
        picker: EnginePicker | None = None
    else:
        # 故障壳由 picker 自己套：它中途会换引擎，套在外面就等于没套
        # —— 换上去的那台是裸的，T2.8 的降级演练会在真机上静默失效。
        picker = EnginePicker(paths, plan=plan)
        chosen, default_voice, _ = picker()
    return VoiceSentenceHandler(
        paths=paths,
        connection=resolved,
        engine=chosen,
        engine_picker=picker,
        voice=voice if voice is not None else default_voice,
        glossary=GlossaryStore(paths.glossary_file),
        log=log if log is not None else LogService(resolved),
        breaker=breaker if breaker is not None else CircuitBreaker(),
    )


def _simplified_seed(sentence: SentenceRow) -> int:
    """简化重试用的 seed —— **确定性的**（同一句每次算出来都一样）。

    为什么要确定性：seed 进缓存键。用随机数的话，每次"重试"都是一个新键 ⇒
    每次都要真念一遍（真机 14 秒），而我们要的只是"换一个采样点"。确定性的值让
    第二次重试能命中第一次简化成功时留下的产物。

    为什么非换不可：``RETRY_SIMPLIFIED`` 的前提是"上一轮的产物是静音 / 爆音"。
    seed 不变 ⇒ 缓存键不变 ⇒ 重试**命中那段坏音频**，于是"重试"变成"读一遍缓存
    再失败一次"，三次退避白等。
    """
    digest = hashlib.sha256(sentence.id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


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
