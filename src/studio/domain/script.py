"""写稿领域契约（T1.10 · §04.1.4 / §04.1.5）—— Director 大纲 + Writer 成稿。

本模块**只有纯函数与 pydantic 契约**：不碰库、不碰网络、不碰提示词。
"LLM 说了什么"由 ``agents/director.py`` / ``agents/writer.py`` 负责，
"什么算合格"在这里 —— 于是规则能被单测穷举，而不是埋在 Agent 的分支里。

三道闸门的顺序（**顺序本身就是设计**）
------------------------------------
```
① 结构闸（pydantic + JSON Schema）  段数 / 单段字数 / 时长区间 / 字段名
② 规则闸（本模块 check_*）          字数区间 / 口癖命中 / 禁区 / 单人占比
③ 切分闸（enforce_sentence_limit）  超长句**强制切分**（不重写，省 token）
```
③ 排在②后面：先切分再算字数与占比，校验看到的才是**真正要送去合成的东西**。

为什么 LLM 契约里的 ``text`` 放到 200 字
----------------------------------------
§04.1.5 写的是 ``max_length=28``。但那是**生效文本**的上限；若把它直接钉在
LLM 输出契约上，"模型写了个长句"会变成一次 JSON Schema 修复重试（烧 token，
且可能反复失败），而规格书要求的是"**切分，不重写**"。所以契约层放宽到 200，
28 由 :func:`enforce_sentence_limit` 在服务端强制 —— 落库的
``script_sentences.text`` 必然 ≤28（T1.10 裁定 78）。

为什么时长由字数**算**出来
--------------------------
``WriterOutput.est_duration_ms`` 是模型自报值，只代表"它自己觉得多长"。
落库的 ``scripts.est_duration_ms`` 一律由 :func:`estimate_duration_ms` 从实际字数
重算（≈5 字/秒）—— 与 ADR-005「音频为时长真相」同一取向：**能算的不要信模型**。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.core.config import PersonaConfig
from studio.domain.text import compact_ws, count_chars, split_long_sentence, split_sentences
from studio.domain.topics import TopicSpec

__all__ = [
    "CATCHPHRASE_MIN_HITS",
    "CHARS_PER_SECOND",
    "DURATION_DEFAULT_MS",
    "DURATION_MAX_MS",
    "DURATION_MIN_MS",
    "FACTS_MAX",
    "FACTS_UNSET",
    "FORBIDDEN_PREFIX",
    "OUTLINE_ARGUMENT_MAX",
    "OUTLINE_TITLE_MAX",
    "OUTLINE_UNSET",
    "PAUSE_DEFAULT_MS",
    "REWRITE_LIMIT",
    "SEGMENT_CHARS_MAX",
    "SEGMENT_CHARS_MIN",
    "SEGMENT_MAX",
    "SEGMENT_MIN",
    "SENTENCE_INPUT_MAX_CHARS",
    "SENTENCE_MAX_CHARS",
    "SENTENCE_MIN_COUNT",
    "SPEAKERS",
    "SPEAKER_RATIO_MAX",
    "WORD_COUNT_MAX",
    "WORD_COUNT_MIN",
    "WORD_COUNT_TARGET",
    "DirectorInput",
    "DirectorOutput",
    "OutlineInput",
    "OutlineOutput",
    "OutlineReport",
    "ScriptDraft",
    "ScriptReport",
    "ScriptRules",
    "ScriptSegment",
    "SentenceSpec",
    "Speaker",
    "WriterInput",
    "WriterOutput",
    "audience_speaker_labels",
    "build_draft",
    "catchphrase_hits",
    "character_name_hits",
    "character_name_problems",
    "check_outline",
    "check_outline_title",
    "check_script",
    "ending_asks_audience",
    "enforce_sentence_limit",
    "estimate_duration_ms",
    "find_forbidden",
    "rewrite_hint",
    "speaker_label_problems",
    "speaker_ratio",
    "strip_speaker_labels",
]

# ── 阈值（默认值 = 规格书原文；persona 可覆盖，见 ScriptRules）────────────
WORD_COUNT_MIN: Final[int] = 600
WORD_COUNT_MAX: Final[int] = 800
WORD_COUNT_TARGET: Final[int] = 700
SEGMENT_MIN: Final[int] = 3
SEGMENT_MAX: Final[int] = 5
SEGMENT_CHARS_MIN: Final[int] = 80
SEGMENT_CHARS_MAX: Final[int] = 350
DURATION_MIN_MS: Final[int] = 60_000
DURATION_MAX_MS: Final[int] = 180_000
DURATION_DEFAULT_MS: Final[int] = 180_000
SENTENCE_MAX_CHARS: Final[int] = 28
SENTENCE_INPUT_MAX_CHARS: Final[int] = 200
SENTENCE_MIN_COUNT: Final[int] = 15
SPEAKER_RATIO_MAX: Final[float] = 0.7
CATCHPHRASE_MIN_HITS: Final[int] = 2

# ── 二级产物（视频标题 + 核心论点）────────────────────────────────────
OUTLINE_TITLE_MAX: Final[int] = 60
OUTLINE_ARGUMENT_MAX: Final[int] = 200

#: 二级产物缺位时喂给提示词的**字面量**（不能给空串：空行会让模型以为
#: 「上游给了一个空标题」，而事实是「上游还没定」）。
OUTLINE_UNSET: Final[str] = "（未定 —— 这一级还没定，你按选题自行发挥）"

# ── 事实块（今日新闻那条链路 · T5.12 增补）────────────────────────────
#
#: 注入提示词的**已知事实**上限：目前唯一来源是"今日新闻挑出来的方向"里那条事件总结
#: （``topic_service.direction_facts``，单条 ≤200 字）。放宽到 400 是给"以后可能多来源"
#: 留的余量，不是为了让人往这里塞一篇新闻。
FACTS_MAX: Final[int] = 400

#: 没有事实可给时的**字面量**（同 :data:`OUTLINE_UNSET`：空串会让模型以为"上游给了空事实"，
#: 而事实是"这一条不是从新闻来的"）。
FACTS_UNSET: Final[str] = "（无 —— 这一条不是从今日新闻来的，按选题本身写）"

REWRITE_LIMIT: Final[int] = 2
CHARS_PER_SECOND: Final[float] = 5.0
PAUSE_DEFAULT_MS: Final[int] = 200

#: 禁区命中在 ``problems`` 里的前缀（服务层据此判 block，不靠字符串猜）
FORBIDDEN_PREFIX: Final[str] = "forbidden:"

#: 结尾在向观众提问时的**问题文案**（大纲闸与成稿闸共用一份 —— 两处各写一遍，
#: 迟早会出现“大纲这么说、成稿那么说”，而它们是同一件事）。
ENDING_QUESTION_PROBLEM: Final[str] = "结尾在向观众提问 —— 结尾的职责是把观点钉死，不是把问题踢回给观众"

#: 结尾“向观众提问”的判据：问号（全角 / 半角）。
#:
#: 为什么只认问号：这是**当场可判**的事实。“你们怎么看”这种不带问号的求互动，
#: 机器判不了（它和一句普通转述长得一样），交给 Reviewer 那六个维度去判；
#: 反过来，问号是“在向观众抛问题”最确定的形态 —— 而它恰恰是这条片子最不该有的结尾。
QUESTION_MARKS: Final[tuple[str, ...]] = ("？", "?")

# ── 说话人标签（T1.10 追加 · 观众可见字段里不许出现"谁在说"）─────────────
#
# 用户口径（2026-09-23）：**这个账号只是借这两个角色的口讨论社会问题** —— 标题与
# 开场、结尾是给观众看的字，里面不该出现"谁在说"。真机踩到的形态是标题写成
# 「老人登记遗体捐献被拒收，熊大：凉的不是他一个人的心」。
#
# ★ 判据是**形态**，不是名字表：项目里没有名字表（角色名只活在 persona 的自由
#   文本里，去解析那段文字只会得到一条随时会失效的判据）。所以它抓得住"名字被
#   当成标签用"（`熊大：`），抓不住"名字被写进正文句子"（口癖「熊大你听我说」
#   就是这一种）—— 那一条由提示词纪律与 Reviewer 的六个维度管。

#: 行首的说话人标签（`熊大：…`）—— **口播全文里不许有它**：谁说的由 ``sentences[].speaker`` 表达。
#:
#: 只认 **2–3 个字**的名字，比观众字段那一侧**更保守** —— 两侧误判的代价不一样：
#: 剥多一个字是**删正文**（`他后来说：便宜的东西…` 里的引述会被吃掉），漏剥一个
#: 只是行首多三个字（提示词已经明令不许写）。所以这里不追长标签：`熊二说：` 那种
#: 形态归提示词与 Reviewer 管（真机观察到的形态就是 2 字的 `熊大：` / `熊二：`）。
LEADING_SPEAKER_LABEL: Final[re.Pattern[str]] = re.compile(r"^\s*[\u4e00-\u9fff]{2,3}\s*[：:]\s*")

#: 任意位置的说话人标签 —— **观众直接看到的字段（标题 / 开场 / 结尾）里不许有它**。
#:
#: 这一侧放到 **1–4 个字**（外加英文标识，如 `littlebear:`）：那三个字段误判的代价
#: 只是多问一轮，而漏判的代价是标题里挂着"谁在说"直接发出去。
#:
#: 冒号前面那截必须**短**（1–4 个字）**且**紧跟在句首或停顿标点之后：于是
#: `…被拒收，熊大：凉的不是…` 命中，而 `退休捐献被拒收：制度别凉了心` 不命中
#: —— 后者是**一级标题本来的写法**（冒号前那截是正文，不是名字），不能冤枉它。
#: 代价是「钱，谁掏：…」这种写法会被误判；但那种写法读起来本来就像标签。
INLINE_SPEAKER_LABEL: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s，,。！？；…])(?:[\u4e00-\u9fff]{1,4}|[A-Za-z][A-Za-z0-9_]{0,15})\s*[：:](?=\s*\S)"
)

#: 观众可见字段里出现说话人标签时的**问题文案**（大纲闸与成稿闸共用一份）。
SPEAKER_LABEL_PROBLEM: Final[str] = (
    "说话人标签出现在观众能看到的字段里（标题 / 开场 / 结尾）—— 那是给观众的字，谁说的由逐句表表达"
)

#: **角色名**出现在观众可见字段里时的**问题文案**。
#:
#: 用户口径（2026-09-23）：**这个账号只是借两个角色的口讨论社会问题** —— 名字是配音与
#: 贴图的事，观众看到的那行字里不该有它。真机形态：候选标题写成「护工钱谁掏？熊二问完
#: 这句，熊大把账分成了两笔」—— 那是一条"关于两个角色"的标题，而不是"关于这件事"的标题。
#:
#: 名字从 ``persona.speaker_names`` 来（**可编辑** ⇒ 换角色时规则跟着走）。那一栏留空
#: 就**不查这一条** —— 与其内置一份"熊大熊二"的名字表，不如让它跟人设走。
CHARACTER_NAME_PROBLEM: Final[str] = (
    "角色名出现在观众能看到的字里 —— 观众看到的是「一条讲这件事的视频」，不是「谁和谁在聊」"
)

#: 口播全文里出现角色名时的**警告前缀**（**不是** problem：口癖本身就可能是"熊大你听我说"，
#: 判成不合格会与"口癖至少命中 N 个"直接打架，把人卡死在重写循环里）。
CHARACTER_NAME_WARNING: Final[str] = "character_name:"

Speaker = Literal["bigbear", "littlebear", "narrator"]
SPEAKERS: Final[tuple[str, ...]] = ("bigbear", "littlebear", "narrator")


class ScriptRules(BaseModel):
    """一次校验真正生效的阈值（**persona 可编辑 ⇒ 阈值不能写死在函数里**）。"""

    model_config = ConfigDict(extra="forbid")

    word_count_min: int = Field(default=WORD_COUNT_MIN, ge=100, le=5000)
    word_count_max: int = Field(default=WORD_COUNT_MAX, ge=100, le=8000)
    duration_max_ms: int = Field(default=DURATION_MAX_MS, ge=10_000, le=1_800_000)
    sentence_max_chars: int = Field(default=SENTENCE_MAX_CHARS, ge=8, le=120)
    catchphrase_min_hits: int = Field(default=CATCHPHRASE_MIN_HITS, ge=0, le=10)
    speaker_ratio_max: float = Field(default=SPEAKER_RATIO_MAX, ge=0.1, le=1.0)
    #: 角色名（``persona.speaker_names``）—— 观众可见字段里不许出现它们；空 ⇒ 不查
    speaker_names: tuple[str, ...] = ()

    @classmethod
    def from_persona(cls, persona: PersonaConfig) -> ScriptRules:
        """从人物库取生效阈值（``config/personas/<id>.yaml`` 一改就跟着变）。"""
        return cls(
            word_count_min=persona.target_chars_min,
            word_count_max=persona.target_chars_max,
            duration_max_ms=persona.max_duration_ms,
            speaker_names=tuple(persona.speaker_names),
        )


# ══════════════════════════════════════════════════════════════════════
# 二级产物 · 视频标题 + 核心论点（文案三级流水线的中间一级）
# ══════════════════════════════════════════════════════════════════════


class OutlineInput(BaseModel):
    """二级产物的输入（选题 + 角度；与 Director 同一口径）。"""

    model_config = ConfigDict(extra="forbid")

    topic: TopicSpec
    angle: str | None = Field(default=None, max_length=120)
    #: 已知事实（今日新闻挑出来的方向给的事件总结，见 ``topic_service.direction_facts``）；
    #: 空 ⇒ 这一条不是从新闻来的，调用方渲染成 :data:`FACTS_UNSET` 再喂给模型。
    facts: str = Field(default="", max_length=FACTS_MAX)


class OutlineOutput(BaseModel):
    """二级产物：**视频标题 + 核心论点**。

    为什么标题要在这一级就定死
    --------------------------
    ``scripts.title`` 是观众看到的第一行字，而它此前是 Writer 写完 600–800 字之后
    顺手起的 —— 也就是「内容决定标题」。人对这件事的控制点恰恰相反：**先想清楚要
    说什么**（标题 + 论点），再让模型展开成对话。定在这一级，三级就只是「按它写」。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=OUTLINE_TITLE_MAX)
    core_argument: str = Field(min_length=1, max_length=OUTLINE_ARGUMENT_MAX)


# ══════════════════════════════════════════════════════════════════════
# Director（§04.1.4）
# ══════════════════════════════════════════════════════════════════════


class ScriptSegment(BaseModel):
    """大纲里的一段（要点 / 画面建议 / 情绪 / 预估字数）。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    point: str = Field(max_length=120)
    visual: str = Field(max_length=200)
    mood: str = Field(max_length=40)
    est_chars: int = Field(ge=SEGMENT_CHARS_MIN, le=SEGMENT_CHARS_MAX)


class DirectorInput(BaseModel):
    """Director 的输入（§04.1.4 未定义输入模型，此处按"选题 + 目标时长"补齐）。"""

    model_config = ConfigDict(extra="forbid")

    topic: TopicSpec
    target_duration_ms: int = Field(default=DURATION_DEFAULT_MS, ge=DURATION_MIN_MS, le=DURATION_MAX_MS)
    angle: str | None = Field(default=None, max_length=120)
    #: 二级产物（视频标题 + 核心论点）；``None`` ⇒ 这一级还没定，按选题自由发挥
    outline_title: str | None = Field(default=None, max_length=OUTLINE_TITLE_MAX)
    core_argument: str | None = Field(default=None, max_length=OUTLINE_ARGUMENT_MAX)
    #: 已知事实（同 :class:`OutlineInput.facts`）
    facts: str = Field(default="", max_length=FACTS_MAX)


class DirectorOutput(BaseModel):
    """大纲：黄金 3 秒钩子 + 3–5 段 + CTA + 时长预估。"""

    model_config = ConfigDict(extra="forbid")

    hook_3s: str = Field(max_length=80)
    segments: list[ScriptSegment] = Field(min_length=SEGMENT_MIN, max_length=SEGMENT_MAX)
    cta: str = Field(max_length=80)
    est_duration_ms: int = Field(ge=DURATION_MIN_MS, le=DURATION_MAX_MS)


class OutlineReport(BaseModel):
    """大纲校验结果（空 ``problems`` = 合格）。"""

    model_config = ConfigDict(extra="forbid")

    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        return "；".join(self.problems)


# ══════════════════════════════════════════════════════════════════════
# Writer（§04.1.5）
# ══════════════════════════════════════════════════════════════════════


class SentenceSpec(BaseModel):
    """一句口播（**按句配音**的最小单位，也是断点续传的粒度）。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=SENTENCE_INPUT_MAX_CHARS)
    speaker: Speaker
    emotion: str = Field(default="neutral", max_length=40)
    pause_after_ms: int = Field(default=PAUSE_DEFAULT_MS, ge=0, le=2000)


class WriterInput(BaseModel):
    """Writer 的输入（``revision_notes`` 非空 ⇒ 本轮是改稿，T1.11 用）。"""

    model_config = ConfigDict(extra="forbid")

    topic: TopicSpec
    outline: DirectorOutput
    revision_notes: list[str] = Field(default_factory=list)
    #: 二级产物；``outline_title`` 非空 ⇒ 成稿标题**锁定**用它（见 ScriptService.draft）
    outline_title: str | None = Field(default=None, max_length=OUTLINE_TITLE_MAX)
    core_argument: str | None = Field(default=None, max_length=OUTLINE_ARGUMENT_MAX)
    #: 已知事实（同 :class:`OutlineInput.facts`）
    facts: str = Field(default="", max_length=FACTS_MAX)


class WriterOutput(BaseModel):
    """成稿（``body_md`` 的字数区间**不**在 schema 里卡 —— 见模块 docstring）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=60)
    hook: str = Field(max_length=80)
    body_md: str = Field(min_length=1)
    cta: str = Field(max_length=80)
    sentences: list[SentenceSpec] = Field(min_length=SENTENCE_MIN_COUNT)
    est_duration_ms: int = Field(ge=DURATION_MIN_MS, le=DURATION_MAX_MS)
    catchphrases_used: list[str] = Field(default_factory=list)


class ScriptDraft(BaseModel):
    """落库前的最终产物（**已过强制切分**，句长 ≤ ``sentence_max_chars``）。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    hook: str
    body_md: str
    cta: str
    sentences: list[SentenceSpec]
    est_duration_ms: int
    word_count: int = Field(ge=0)
    speaker_ratio: dict[str, float] = Field(default_factory=dict)
    catchphrases_used: list[str] = Field(default_factory=list)
    outline: DirectorOutput
    warnings: list[str] = Field(default_factory=list)


class ScriptReport(BaseModel):
    """成稿校验结果。"""

    model_config = ConfigDict(extra="forbid")

    word_count: int = 0
    problems: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    forbidden_hits: list[str] = Field(default_factory=list)
    catchphrase_hits: list[str] = Field(default_factory=list)
    split_count: int = 0
    #: 服务端剥掉的说话人标签**处数**（口播全文与逐句表**各算一处** —— 两处都得干净，
    #: 见 :func:`strip_speaker_labels`）
    speaker_labels_stripped: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def blocked(self) -> bool:
        """禁区命中 ⇒ **直接 block**（不进评分、不重写 —— §04.1.5）。"""
        return bool(self.forbidden_hits)

    def describe(self) -> str:
        return "；".join(self.problems)


# ══════════════════════════════════════════════════════════════════════
# 纯函数
# ══════════════════════════════════════════════════════════════════════


def check_outline(outline: DirectorOutput) -> OutlineReport:
    """大纲规则闸（结构约束已由 pydantic / JSON Schema 保证，这里只查语义）。"""
    problems: list[str] = []
    seqs = [segment.seq for segment in outline.segments]
    if seqs != list(range(1, len(seqs) + 1)):
        problems.append(f"段落 seq 必须从 1 连续递增（当前 {seqs}）")
    total = sum(segment.est_chars for segment in outline.segments)
    if not WORD_COUNT_MIN <= total <= WORD_COUNT_MAX:
        problems.append(f"段落字数合计 {total} 不在 {WORD_COUNT_MIN}–{WORD_COUNT_MAX}")
    if not outline.hook_3s.strip():
        problems.append("hook_3s 不得为空")
    if not outline.cta.strip():
        problems.append("cta 不得为空")
    elif ending_asks_audience(outline.cta) is not None:
        problems.append(ENDING_QUESTION_PROBLEM)
    return OutlineReport(problems=problems)


def check_outline_title(title: str, *, names: Sequence[str] = ()) -> OutlineReport:
    """二级产物的标题闸 —— **只查可机检的那两条**。

    为什么只查这两条：二级产物只有标题与论点两个自由文本字段，"标题够不够得体"
    是主观判断（那归提示词与 Reviewer），而"标题里写着说话人标签"与"标题里写着
    角色名"都是**当场可判的事实** —— 也正是真机踩到的两条（用户 2026-09-23：标题
    写成「老人登记遗体捐献被拒收，熊大：凉的不是他一个人的心」，以及「护工钱谁掏？
    熊二问完这句，熊大把账分成了两笔」）。

    ``names`` 来自 ``persona.speaker_names``：留空 ⇒ 只查标签那一条。
    """
    problems = [*speaker_label_problems(title), *character_name_problems(title, names=names)]
    return OutlineReport(problems=problems)


def catchphrase_hits(text: str, catchphrases: Sequence[str]) -> list[str]:
    """口癖命中（**服务端复核**模型的自报值；忽略空白差异）。"""
    compact = _compact(text)
    hits: list[str] = []
    for phrase in catchphrases:
        key = _compact(phrase)
        if key and key in compact and phrase not in hits:
            hits.append(phrase)
    return hits


def ending_asks_audience(cta: str) -> str | None:
    """结尾是不是在向观众提问（命中 ⇒ 返回那个问句，否则 ``None``）。

    这条判据来自用户的一句话：**不要在文稿最后给观众抛出问题 —— 文稿的意义就是
    提出一个合适的观点**。所以结尾的职责是**把观点钉死**，而不是把问题踢回给观众。

    判据落在 ``cta`` 上（而不是最后一句口播）：``cta`` 就是这条片子的落点，
    它同时会当作发布文案 —— 在这里提问，等于把问题留在片尾又留在简介里。
    """
    for sentence in split_sentences(cta):
        if any(mark in sentence for mark in QUESTION_MARKS):
            return sentence
    return None


def audience_speaker_labels(*fields: str) -> list[str]:
    """观众可见字段里命中的说话人标签（去重，按出现顺序）。

    传进来的应当是**观众直接看到的字**（标题 / 开场 / 结尾）—— 口播全文另有
    :func:`strip_speaker_labels` 处理（那边是剥掉，不是报错）。
    """
    hits: list[str] = []
    for field in fields:
        for match in INLINE_SPEAKER_LABEL.finditer(field):
            label = match.group(0).strip(" \t，,。！？；…")
            if label and label not in hits:
                hits.append(label)
    return hits


def character_name_hits(text: str, names: Sequence[str]) -> list[str]:
    """文本里出现的角色名（去重，按 ``names`` 的顺序）。

    纯子串匹配（英文名不区分大小写）：名字是**人设给的**，不是猜的 —— 猜名字只会得到
    一条随时会失效的判据。
    """
    haystack = text.casefold()
    hits: list[str] = []
    for name in names:
        key = name.strip()
        if key and key.casefold() in haystack and key not in hits:
            hits.append(key)
    return hits


def character_name_problems(*fields: str, names: Sequence[str] = ()) -> list[str]:
    """观众可见字段里出现角色名 ⇒ 一条 ``problems``（干净或没配名字 ⇒ 空列表）。"""
    hits: list[str] = []
    for field in fields:
        for hit in character_name_hits(field, names):
            if hit not in hits:
                hits.append(hit)
    if not hits:
        return []
    return [f"{CHARACTER_NAME_PROBLEM}（{'、'.join(hits)}）"]


def speaker_label_problems(*fields: str) -> list[str]:
    """把 :func:`audience_speaker_labels` 的结果收成一条 ``problems``（干净 ⇒ 空列表）。"""
    labels = audience_speaker_labels(*fields)
    if not labels:
        return []
    return [f"{SPEAKER_LABEL_PROBLEM}（{'、'.join(labels)}）"]


def strip_speaker_labels(text: str) -> tuple[str, int]:
    """剥掉**行首**的说话人标签，返回 ``(新文本, 剥掉的条数)``。

    为什么是"剥掉"而不是"判不合格"：标签是**格式**（谁说的另有字段表达），不是
    内容 —— 与"超长句强制切分"同一条取舍：能当场收敛的就不烧一轮重写。剥掉之后
    字数才是真正的口播字数（真机那两条：726 → 606、766 → 650，多出来的全是标签）。

    只认**行首**：口播全文一行一句，行首那个标签是格式；行中间出现的"…说：…"是
    正文，删了就是改内容。
    """
    kept: list[str] = []
    stripped = 0
    for line in text.splitlines():
        cleaned, count = LEADING_SPEAKER_LABEL.subn("", line, count=1)
        if count:
            stripped += 1
        kept.append(cleaned)
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(kept) + trailing, stripped


def find_forbidden(text: str, terms: Sequence[str]) -> list[str]:
    """禁区词扫描（同样忽略空白差异）。"""
    compact = _compact(text)
    hits: list[str] = []
    for term in terms:
        key = _compact(term)
        if key and key in compact and term not in hits:
            hits.append(term)
    return hits


def speaker_ratio(sentences: Sequence[SentenceSpec]) -> dict[str, float]:
    """各说话人的占比（**按口播字数加权**，不是按句数 —— 10 个短句抵不过 1 段长独白）。"""
    weights: dict[str, int] = {}
    for sentence in sentences:
        weights[sentence.speaker] = weights.get(sentence.speaker, 0) + max(1, count_chars(sentence.text))
    total = sum(weights.values())
    if total == 0:
        return {}
    return {speaker: round(value / total, 4) for speaker, value in weights.items()}


def enforce_sentence_limit(
    sentences: Sequence[SentenceSpec],
    *,
    limit: int = SENTENCE_MAX_CHARS,
) -> tuple[list[SentenceSpec], int]:
    """超长句**强制切分**并重排 ``seq``；返回 ``(新句表, 切分次数)``。

    切分产生的碎片之间 ``pause_after_ms=0``（同一句话的中间不该有句间停顿），
    只有最后一个碎片保留原停顿。
    """
    out: list[SentenceSpec] = []
    splits = 0
    for sentence in sentences:
        pieces = split_long_sentence(sentence.text, limit=limit)
        if not pieces:
            continue
        splits += max(0, len(pieces) - 1)
        for index, piece in enumerate(pieces):
            is_last = index == len(pieces) - 1
            out.append(
                sentence.model_copy(
                    update={
                        "seq": len(out) + 1,
                        "text": piece,
                        "pause_after_ms": sentence.pause_after_ms if is_last else 0,
                    }
                )
            )
    return out, splits


def estimate_duration_ms(word_count: int, *, chars_per_second: float = CHARS_PER_SECOND) -> int:
    """由口播字数估时长（≈5 字/秒 ⇒ 700 字 ≈ 140 秒）。"""
    if word_count <= 0:
        return 0
    return round(word_count / chars_per_second * 1000)


def check_script(
    *,
    body_md: str,
    sentences: Sequence[SentenceSpec],
    catchphrases: Sequence[str],
    forbidden: Sequence[str],
    title: str = "",
    hook: str = "",
    cta: str = "",
    rules: ScriptRules | None = None,
) -> ScriptReport:
    """成稿规则闸：禁区 / 说话人标签 / 字数 / 口癖 / 单人占比 / 结尾不得提问。"""
    active = rules or ScriptRules()
    word_count = count_chars(body_md)
    problems: list[str] = []
    warnings: list[str] = []

    hits = find_forbidden(body_md, forbidden)
    problems.extend(f"{FORBIDDEN_PREFIX}{term}" for term in hits)

    # 观众可见的三个字段（标题 / 开场 / 结尾）："谁在说"不该出现在给观众的字里。
    # 口播全文那一路是**剥掉**（见 strip_speaker_labels），这里不能剥 —— 标题是二级
    # 产物、开场与结尾是发布文案，悄悄改掉等于换了一个东西交出去。
    problems.extend(speaker_label_problems(title, hook, cta))
    problems.extend(character_name_problems(title, hook, cta, names=active.speaker_names))

    # 口播全文里的角色名只**警告**：口癖本身可能就是"熊大你听我说"，判成不合格会与
    # "口癖至少命中 N 个"打架（两条都满足不了 ⇒ 重写循环白烧 token）。
    warnings.extend(
        f"{CHARACTER_NAME_WARNING}{name}" for name in character_name_hits(body_md, active.speaker_names)
    )

    if not active.word_count_min <= word_count <= active.word_count_max:
        problems.append(f"字数 {word_count} 不在 {active.word_count_min}–{active.word_count_max}")

    if ending_asks_audience(cta) is not None:
        problems.append(ENDING_QUESTION_PROBLEM)

    used = catchphrase_hits(body_md, catchphrases)
    if len(used) < active.catchphrase_min_hits:
        problems.append(
            f"口癖命中 {len(used)} 个，少于 {active.catchphrase_min_hits} 个（{list(catchphrases)}）"
        )

    ratio = speaker_ratio(sentences)
    if len(ratio) == 1:
        warnings.append(f"single_speaker:{next(iter(ratio))}")
    elif ratio:
        dominant, share = max(ratio.items(), key=lambda item: item[1])
        if share > active.speaker_ratio_max:
            warnings.append(f"speaker_ratio:{dominant}={share:.2f}")

    return ScriptReport(
        word_count=word_count,
        problems=problems,
        warnings=warnings,
        forbidden_hits=hits,
        catchphrase_hits=used,
    )


def build_draft(
    *,
    outline: DirectorOutput,
    output: WriterOutput,
    catchphrases: Sequence[str],
    forbidden: Sequence[str],
    rules: ScriptRules | None = None,
) -> tuple[ScriptDraft, ScriptReport]:
    """把 Writer 的原始输出收敛成**可落库**的 :class:`ScriptDraft` + 校验报告。

    顺序：**剥说话人标签** ⇒ 切分 ⇒ 校验 ⇒ 重算时长/占比。``est_duration_ms`` 一律用
    **算出来的**值（模型自报值只留在 ``WriterOutput`` 里，不落库）。

    剥标签排在最前面：模型写对话体时习惯把 `熊大：` 写在行首，而那是**格式**。
    它混在正文里有两个后果 —— 观众看到的稿子与字幕里多出"谁在说"，字数也会被
    撑起来（真机两条各多算 120 / 116 字）。**口播全文与逐句表都要剥**：前者是面板
    上给人看的稿子，后者是喂 TTS 的那一份。
    """
    active = rules or ScriptRules()
    body_md, labels_stripped = strip_speaker_labels(output.body_md)
    spoken: list[SentenceSpec] = []
    for item in output.sentences:
        text, count = strip_speaker_labels(item.text)
        labels_stripped += count
        spoken.append(item.model_copy(update={"text": text}))
    sentences, splits = enforce_sentence_limit(spoken, limit=active.sentence_max_chars)
    report = check_script(
        body_md=body_md,
        sentences=sentences,
        catchphrases=catchphrases,
        forbidden=forbidden,
        title=output.title,
        hook=output.hook,
        cta=output.cta,
        rules=active,
    )
    report.split_count = splits
    report.speaker_labels_stripped = labels_stripped
    if labels_stripped:
        report.warnings.append(f"speaker_label_stripped:{labels_stripped}")
    draft = ScriptDraft(
        title=output.title,
        hook=output.hook,
        body_md=body_md,
        cta=output.cta,
        sentences=sentences,
        est_duration_ms=estimate_duration_ms(report.word_count),
        word_count=report.word_count,
        speaker_ratio=speaker_ratio(sentences),
        catchphrases_used=report.catchphrase_hits,
        outline=outline,
        warnings=report.warnings,
    )
    return draft, report


def rewrite_hint(report: ScriptReport) -> str:
    """把"哪里不合格"回灌给模型（重写提示；空 = 不用重写）。"""
    if not report.problems:
        return ""
    return "上一版不合格：" + "；".join(report.problems) + "。请重写完整 JSON，不要解释。"


def _compact(text: str) -> str:
    """去掉所有空白（口径在 :func:`studio.domain.text.compact_ws`，此处只是别名）。"""
    return compact_ws(text)
