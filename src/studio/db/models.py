"""行映射（T1.9 · §02.2）—— DDL 的**轻量** Python 视图。

为什么是 dataclass 而不是 pydantic
----------------------------------
``db/`` 只能依赖 ``core``（§02.1 依赖方向），而领域契约（``domain/topics.py``）是
pydantic 模型。若让仓储直接返回领域模型，``db`` 就得 import ``domain`` ⇒ 依赖方向反转，
"谁依赖谁"就再也说不清了。所以分工是：

```
db/models.py（dataclass 行结构）  ⇄  db/repositories/*（SQL + 行映射）
        ▲                                          ▲
        └──── services/*（行 ⇄ 领域模型的翻译）──────┘
```

JSON 列（``grounded_on_json`` / ``similar_to_json`` / ``wants_json`` …）在**行结构里
就已经解析成 Python 对象**：调用方拿到的是数据，不是字符串。写路径反过来 ——
仓储负责 ``json.dumps``，调用方只给对象。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "ApprovalRow",
    "AuditOpRow",
    "BgmTrackRow",
    "BrollClipRow",
    "DirectionRow",
    "FeedbackItemRow",
    "HotItemRow",
    "ReviewRow",
    "ScriptRow",
    "SentenceRow",
    "TopicRow",
    "VoiceProfileRow",
]


class RowLike(Protocol):
    """``sqlite3.Row`` 的结构类型。

    typeshed 把 ``sqlite3.Row`` 标成 ``Sequence[Any]`` 而不是 ``Mapping``，
    所以这里只要求"能按列名取值"这一件事（与 ``domain/models.py`` 同一手法 ——
    ``db`` 不能反向 import ``domain``，故各留一份最小协议）。
    """

    def __getitem__(self, key: str) -> Any: ...


def _opt(row: RowLike, key: str) -> Any:
    """取列值；列不存在 ⇒ ``None``（``sqlite3.Row`` 缺键抛 ``IndexError``）。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _text(row: RowLike, key: str) -> str | None:
    value = _opt(row, key)
    return None if value is None else str(value)


def _int_or_none(row: RowLike, key: str) -> int | None:
    value = _opt(row, key)
    return None if value is None else int(value)


def _json_dict(row: RowLike, key: str) -> dict[str, Any]:
    """JSON 对象列 → ``dict``（坏值 ⇒ 空字典，理由同 :func:`_json_list`）。"""
    raw = _opt(row, key)
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(row: RowLike, key: str) -> list[Any]:
    """JSON 数组列 → ``list``（``NULL`` / 空串 / 坏 JSON ⇒ 空列表）。

    坏 JSON **不抛异常**：读路径上炸掉只会让"面板打不开"，而列本身有
    ``CHECK (json_valid(...))`` 兜着 —— 能读到坏值说明库被外部改过，
    此时"少显示一行"远好于"整页 500"。
    """
    raw = _opt(row, key)
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


# ══════════════════════════════════════════════════════════════════════
# hot_items（§03.3.2）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class HotItemRow:
    """一行热点。``parse_ok=False`` ⇒ 留痕但不参与选题（T1.9 裁定 67）。"""

    id: str
    source_file: str
    line_no: int | None
    title: str
    heat: str | None
    platform: str | None
    raw_line: str
    parse_ok: bool = True
    used_by_direction_id: str | None = None
    consumed_at: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> HotItemRow:
        return cls(
            id=str(_opt(row, "id")),
            source_file=str(_opt(row, "source_file")),
            line_no=_int_or_none(row, "line_no"),
            title=str(_opt(row, "title")),
            heat=_text(row, "heat"),
            platform=_text(row, "platform"),
            raw_line=str(_opt(row, "raw_line")),
            parse_ok=bool(_opt(row, "parse_ok")),
            used_by_direction_id=_text(row, "used_by_direction_id"),
            consumed_at=_text(row, "consumed_at"),
            created_at=_text(row, "created_at"),
        )


# ══════════════════════════════════════════════════════════════════════
# feedback_items（§03.3.3）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class FeedbackItemRow:
    """一行历史反馈。``sentiment`` 是 **DDL 口径**（``positive``/``neutral``/…）。"""

    id: str
    source_file: str
    platform: str | None
    occurred_on: str | None
    content: str
    sentiment: str | None = None
    wants: list[str] = field(default_factory=list)
    complaints: list[str] = field(default_factory=list)
    is_auto: bool = False
    source_publication_id: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> FeedbackItemRow:
        return cls(
            id=str(_opt(row, "id")),
            source_file=str(_opt(row, "source_file")),
            platform=_text(row, "platform"),
            occurred_on=_text(row, "occurred_on"),
            content=str(_opt(row, "content")),
            sentiment=_text(row, "sentiment"),
            wants=[str(item) for item in _json_list(row, "wants_json")],
            complaints=[str(item) for item in _json_list(row, "complaints_json")],
            is_auto=bool(_opt(row, "is_auto")),
            source_publication_id=_text(row, "source_publication_id"),
            created_at=_text(row, "created_at"),
        )


# ══════════════════════════════════════════════════════════════════════
# content_directions（§03.3.4）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DirectionRow:
    """一行内容方向。"""

    id: str
    batch_id: str
    seq: int
    title: str
    rationale: str
    grounded_on: list[dict[str, Any]] = field(default_factory=list)
    priority: int = 100
    risk_flags: list[str] = field(default_factory=list)
    status: str = "open"
    llm_model: str | None = None
    prompt_version: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> DirectionRow:
        return cls(
            id=str(_opt(row, "id")),
            batch_id=str(_opt(row, "batch_id")),
            seq=int(_opt(row, "seq")),
            title=str(_opt(row, "title")),
            rationale=str(_opt(row, "rationale")),
            grounded_on=[item for item in _json_list(row, "grounded_on_json") if isinstance(item, dict)],
            priority=int(_opt(row, "priority")),
            risk_flags=[str(item) for item in _json_list(row, "risk_flags_json")],
            status=str(_opt(row, "status")),
            llm_model=_text(row, "llm_model"),
            prompt_version=_text(row, "prompt_version"),
            created_at=_text(row, "created_at"),
        )


# ══════════════════════════════════════════════════════════════════════
# topic_candidates（§03.3.5）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class TopicRow:
    """一行选题候选。"""

    id: str
    direction_id: str
    seq: int
    title: str
    angle: str
    hook_type: str | None = None
    exec_feasible: bool = True
    score: float | None = None
    reason: str | None = None
    dedup_hash: str | None = None
    similar_to: list[dict[str, Any]] = field(default_factory=list)
    status: str = "candidate"
    task_id: str | None = None
    selected_at: str | None = None
    selected_by: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> TopicRow:
        score = _opt(row, "score")
        return cls(
            id=str(_opt(row, "id")),
            direction_id=str(_opt(row, "direction_id")),
            seq=int(_opt(row, "seq")),
            title=str(_opt(row, "title")),
            angle=str(_opt(row, "angle")),
            hook_type=_text(row, "hook_type"),
            exec_feasible=bool(_opt(row, "exec_feasible")),
            score=None if score is None else float(score),
            reason=_text(row, "reason"),
            dedup_hash=_text(row, "dedup_hash"),
            similar_to=[item for item in _json_list(row, "similar_to_json") if isinstance(item, dict)],
            status=str(_opt(row, "status")),
            task_id=_text(row, "task_id"),
            selected_at=_text(row, "selected_at"),
            selected_by=_text(row, "selected_by"),
            created_at=_text(row, "created_at"),
        )


# ══════════════════════════════════════════════════════════════════════
# scripts / script_sentences（§03.3.7 / §03.3.8 · T1.10）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ScriptRow:
    """一版稿件。``is_active=True`` 的那一版才是当前生效版本（部分唯一索引兜底）。"""

    id: str
    task_id: str
    version: int
    is_active: bool
    title: str | None
    hook: str | None
    body_md: str
    cta: str | None
    word_count: int = 0
    est_duration_ms: int | None = None
    speaker_ratio: dict[str, Any] = field(default_factory=dict)
    outline: dict[str, Any] = field(default_factory=dict)
    grade: str | None = None
    score_total: float | None = None
    score_rule: float | None = None
    score_llm: float | None = None
    revision_round: int = 0
    editor_notes: list[Any] = field(default_factory=list)
    target_chars: int = 700
    llm_model: str | None = None
    prompt_version: str | None = None
    review: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> ScriptRow:
        return cls(
            id=str(_opt(row, "id")),
            task_id=str(_opt(row, "task_id")),
            version=int(_opt(row, "version")),
            is_active=bool(_opt(row, "is_active")),
            title=_text(row, "title"),
            hook=_text(row, "hook"),
            body_md=str(_opt(row, "body_md")),
            cta=_text(row, "cta"),
            word_count=int(_opt(row, "word_count") or 0),
            est_duration_ms=_int_or_none(row, "est_duration_ms"),
            speaker_ratio=_json_dict(row, "speaker_ratio_json"),
            outline=_json_dict(row, "outline_json"),
            grade=_text(row, "grade"),
            score_total=_opt(row, "score_total"),
            score_rule=_opt(row, "score_rule"),
            score_llm=_opt(row, "score_llm"),
            revision_round=int(_opt(row, "revision_round") or 0),
            editor_notes=_json_list(row, "editor_notes_json"),
            target_chars=int(_opt(row, "target_chars") or 700),
            llm_model=_text(row, "llm_model"),
            prompt_version=_text(row, "prompt_version"),
            review=_json_dict(row, "review_json"),
            created_at=_text(row, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class SentenceRow:
    """一句口播（★ 断点续传与单句重试的载体，§03.3.8）。

    ``tts_status`` 是**句级状态机**；``tts_duration_ms`` 是音频时长的唯一来源
    （ffprobe 实测，T2.7 时间轴按它回填）。
    """

    id: str
    task_id: str
    script_id: str
    seq: int
    text_raw: str
    text: str
    speaker: str
    tts_text: str | None = None
    subtitle: str | None = None
    emotion: str = "neutral"
    speed: float = 1.0
    pause_after_ms: int = 200
    emphasis: list[Any] = field(default_factory=list)
    tts_status: str = "pending"
    tts_engine: str | None = None
    tts_voice_id: str | None = None
    tts_audio_path: str | None = None
    tts_duration_ms: int | None = None
    tts_sample_rate: int | None = None
    tts_hash: str | None = None
    tts_attempts: int = 0
    tts_error: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    version: int = 1
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> SentenceRow:
        speed = _opt(row, "speed")
        return cls(
            id=str(_opt(row, "id")),
            task_id=str(_opt(row, "task_id")),
            script_id=str(_opt(row, "script_id")),
            seq=int(_opt(row, "seq")),
            text_raw=str(_opt(row, "text_raw")),
            text=str(_opt(row, "text")),
            speaker=str(_opt(row, "speaker")),
            tts_text=_text(row, "tts_text"),
            subtitle=_text(row, "subtitle"),
            emotion=str(_opt(row, "emotion") or "neutral"),
            speed=1.0 if speed is None else float(speed),
            pause_after_ms=int(_opt(row, "pause_after_ms") or 0),
            emphasis=_json_list(row, "emphasis_json"),
            tts_status=str(_opt(row, "tts_status") or "pending"),
            tts_engine=_text(row, "tts_engine"),
            tts_voice_id=_text(row, "tts_voice_id"),
            tts_audio_path=_text(row, "tts_audio_path"),
            tts_duration_ms=_int_or_none(row, "tts_duration_ms"),
            tts_sample_rate=_int_or_none(row, "tts_sample_rate"),
            tts_hash=_text(row, "tts_hash"),
            tts_attempts=int(_opt(row, "tts_attempts") or 0),
            tts_error=_text(row, "tts_error"),
            start_ms=_int_or_none(row, "start_ms"),
            end_ms=_int_or_none(row, "end_ms"),
            version=int(_opt(row, "version") or 1),
            created_at=_text(row, "created_at"),
            updated_at=_text(row, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class ReviewRow:
    """一次审稿结论（``review_scores`` 的一行 · §03.3.8）。

    ``round_no`` 是**审稿轮次**（不是改稿轮次）：第一版稿子审出来是 1，改完再审是 2。
    ``UNIQUE (script_id, round_no)`` 保证"同一版稿子不会被记两次分" ——
    重跑审稿是覆盖不了它的，只会撞唯一键（宁可报错，也不要两份互相矛盾的分数）。
    """

    id: str
    task_id: str
    script_id: str
    round_no: int
    rule_total: float
    rule_detail: dict[str, Any] = field(default_factory=dict)
    llm_total: float = 0.0
    llm_detail: dict[str, Any] = field(default_factory=dict)
    total: float = 0.0
    grade: str = "C"
    decision: str = "need_edit"
    issues: list[Any] = field(default_factory=list)
    llm_model: str | None = None
    prompt_version: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> ReviewRow:
        return cls(
            id=str(_opt(row, "id")),
            task_id=str(_opt(row, "task_id")),
            script_id=str(_opt(row, "script_id")),
            round_no=int(_opt(row, "round_no") or 1),
            rule_total=float(_opt(row, "rule_total") or 0.0),
            rule_detail=_json_dict(row, "rule_detail_json"),
            llm_total=float(_opt(row, "llm_total") or 0.0),
            llm_detail=_json_dict(row, "llm_detail_json"),
            total=float(_opt(row, "total") or 0.0),
            grade=str(_opt(row, "grade") or "C"),
            decision=str(_opt(row, "decision") or "need_edit"),
            issues=_json_list(row, "issues_json"),
            llm_model=_text(row, "llm_model"),
            prompt_version=_text(row, "prompt_version"),
            created_at=_text(row, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    """确认闸的一条待办 / 历史（``approvals`` · §03.3.8 / §04.4.4）。

    ``status='pending'`` 的行才是"要人管的事"；``decided_by`` 为
    ``auto_approve_A`` / ``auto_approve_AB`` 时说明是**策略放行**（不是人点的），
    复盘时必须能区分这两者。
    """

    id: str
    task_id: str
    script_id: str | None = None
    status: str = "pending"
    grade: str | None = None
    score_total: float | None = None
    revision_round: int = 0
    requested_at: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    comment: str | None = None
    auto_expire_at: str | None = None

    @property
    def pending(self) -> bool:
        return self.status == "pending"

    @classmethod
    def from_row(cls, row: RowLike) -> ApprovalRow:
        return cls(
            id=str(_opt(row, "id")),
            task_id=str(_opt(row, "task_id")),
            script_id=_text(row, "script_id"),
            status=str(_opt(row, "status") or "pending"),
            grade=_text(row, "grade"),
            score_total=_opt(row, "score_total"),
            revision_round=int(_opt(row, "revision_round") or 0),
            requested_at=_text(row, "requested_at"),
            decided_at=_text(row, "decided_at"),
            decided_by=_text(row, "decided_by"),
            comment=_text(row, "comment"),
            auto_expire_at=_text(row, "auto_expire_at"),
        )


@dataclass(frozen=True, slots=True)
class AuditOpRow:
    """一条操作留痕（``audit_ops`` · §03.3.8）。

    ``task_id`` **刻意不加外键**（DDL 注释：留痕不得被级联删除）——
    任务被 GC 掉之后，"谁在什么时候放行了它"仍要查得到。
    """

    id: int
    at: str | None
    actor: str
    action: str
    target_type: str
    actor_ref: str | None = None
    target_id: str | None = None
    task_id: str | None = None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    result: str = "ok"
    reason: str | None = None
    request_id: str | None = None
    ip: str | None = None
    source: str = "webui"

    @classmethod
    def from_row(cls, row: RowLike) -> AuditOpRow:
        return cls(
            id=int(_opt(row, "id") or 0),
            at=_text(row, "at"),
            actor=str(_opt(row, "actor") or "system"),
            actor_ref=_text(row, "actor_ref"),
            action=str(_opt(row, "action")),
            target_type=str(_opt(row, "target_type")),
            target_id=_text(row, "target_id"),
            task_id=_text(row, "task_id"),
            before=_json_dict(row, "before_json"),
            after=_json_dict(row, "after_json"),
            result=str(_opt(row, "result") or "ok"),
            reason=_text(row, "reason"),
            request_id=_text(row, "request_id"),
            ip=_text(row, "ip"),
            source=str(_opt(row, "source") or "webui"),
        )


# ══════════════════════════════════════════════════════════════════════
# 素材库（§3.3.14 / §4.3.1 · T4.8）
# ══════════════════════════════════════════════════════════════════════
# 三张表对应三类素材，但**列宽极不对称**：跑酷要 usable 区间与防镜像判据
# （``has_text``），BGM 要响度与循环性，音色要"几段、每段多长、最响那一段多响"。
# 所以它们是三个行结构，不是一个"通用素材行" —— 后者会得到一张永远有 2/3 列是
# NULL 的表，而面板分不清"这一项不适用"与"这一项还没测"。


@dataclass(frozen=True, slots=True)
class BrollClipRow:
    """一条 MC 跑酷素材（``broll_clips``）。"""

    id: str
    path: str
    sha256: str
    duration_ms: int
    license: str
    usable_from_ms: int = 0
    usable_to_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    tags: list[str] = field(default_factory=list)
    has_text: bool = False
    thumb_path: str | None = None
    phash: str | None = None
    frame_hashes: list[str] = field(default_factory=list)
    source_url: str | None = None
    proof_path: str | None = None
    licensed_to: str | None = None
    use_count: int = 0
    last_used_at: str | None = None
    enabled: bool = True
    created_at: str | None = None

    @property
    def usable_to_effective(self) -> int:
        """可用区间上界（``NULL`` ⇒ 片尾，即 :attr:`duration_ms`）。

        抽成属性的理由：§04.2.4 的入点抽样要的是"能用到第几毫秒"，而库里存的是
        "人标到第几毫秒，没标就是全片"。让每个调用方各写一次
        ``usable_to_ms or duration_ms`` ⇒ 迟早有人写成 ``usable_to_ms or 0``。
        """
        return self.duration_ms if self.usable_to_ms is None else self.usable_to_ms

    @property
    def usable_ms(self) -> int:
        return self.usable_to_effective - self.usable_from_ms

    @classmethod
    def from_row(cls, row: RowLike) -> BrollClipRow:
        return cls(
            id=str(_opt(row, "id")),
            path=str(_opt(row, "path")),
            sha256=str(_opt(row, "sha256") or ""),
            duration_ms=int(_opt(row, "duration_ms") or 0),
            license=str(_opt(row, "license") or ""),
            usable_from_ms=int(_opt(row, "usable_from_ms") or 0),
            usable_to_ms=_int_or_none(row, "usable_to_ms"),
            width=_int_or_none(row, "width"),
            height=_int_or_none(row, "height"),
            fps=_opt(row, "fps"),
            tags=[str(item) for item in _json_list(row, "tags_json")],
            has_text=bool(_opt(row, "has_text")),
            thumb_path=_text(row, "thumb_path"),
            phash=_text(row, "phash"),
            frame_hashes=[str(item) for item in _json_list(row, "frame_hashes_json")],
            source_url=_text(row, "source_url"),
            proof_path=_text(row, "proof_path"),
            licensed_to=_text(row, "licensed_to"),
            use_count=int(_opt(row, "use_count") or 0),
            last_used_at=_text(row, "last_used_at"),
            enabled=bool(_opt(row, "enabled") if _opt(row, "enabled") is not None else 1),
            created_at=_text(row, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class BgmTrackRow:
    """一条 BGM（``bgm_tracks``）。"""

    id: str
    path: str
    sha256: str
    duration_ms: int
    license: str
    sample_rate: int | None = None
    channels: int | None = None
    bitrate_kbps: int | None = None
    loudness_lufs: float | None = None
    bpm: float | None = None
    mood: str | None = None
    tags: list[str] = field(default_factory=list)
    loopable: bool = False
    source_url: str | None = None
    proof_path: str | None = None
    licensed_to: str | None = None
    use_count: int = 0
    last_used_at: str | None = None
    enabled: bool = True
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> BgmTrackRow:
        return cls(
            id=str(_opt(row, "id")),
            path=str(_opt(row, "path")),
            sha256=str(_opt(row, "sha256") or ""),
            duration_ms=int(_opt(row, "duration_ms") or 0),
            license=str(_opt(row, "license") or ""),
            sample_rate=_int_or_none(row, "sample_rate"),
            channels=_int_or_none(row, "channels"),
            bitrate_kbps=_int_or_none(row, "bitrate_kbps"),
            loudness_lufs=_opt(row, "loudness_lufs"),
            bpm=_opt(row, "bpm"),
            mood=_text(row, "mood"),
            tags=[str(item) for item in _json_list(row, "tags_json")],
            loopable=bool(_opt(row, "loopable")),
            source_url=_text(row, "source_url"),
            proof_path=_text(row, "proof_path"),
            licensed_to=_text(row, "licensed_to"),
            use_count=int(_opt(row, "use_count") or 0),
            last_used_at=_text(row, "last_used_at"),
            enabled=bool(_opt(row, "enabled") if _opt(row, "enabled") is not None else 1),
            created_at=_text(row, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class VoiceProfileRow:
    """一个零样本音色（``voice_profiles`` · 迁移 ``0009``）。"""

    id: str
    path: str
    ref_count: int
    total_duration_ms: int
    sample_rate: int | None = None
    peak_db: float | None = None
    text_path: str | None = None
    proof_path: str | None = None
    license: str | None = None
    source_url: str | None = None
    licensed_to: str | None = None
    enabled: bool = True
    use_count: int = 0
    last_used_at: str | None = None
    created_at: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> VoiceProfileRow:
        return cls(
            id=str(_opt(row, "id")),
            path=str(_opt(row, "path")),
            ref_count=int(_opt(row, "ref_count") or 0),
            total_duration_ms=int(_opt(row, "total_duration_ms") or 0),
            sample_rate=_int_or_none(row, "sample_rate"),
            peak_db=_opt(row, "peak_db"),
            text_path=_text(row, "text_path"),
            proof_path=_text(row, "proof_path"),
            license=_text(row, "license"),
            source_url=_text(row, "source_url"),
            licensed_to=_text(row, "licensed_to"),
            enabled=bool(_opt(row, "enabled") if _opt(row, "enabled") is not None else 1),
            use_count=int(_opt(row, "use_count") or 0),
            last_used_at=_text(row, "last_used_at"),
            created_at=_text(row, "created_at"),
        )
