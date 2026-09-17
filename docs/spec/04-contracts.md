# §04 关键子系统通信契约与接口定义

> 本节的每个签名都是**跨进程/跨模块的稳定契约**：实现可替换，契约不得随意破坏。任何变更需同步更新对应 `tests/contract/*` 与 `schemas/*.schema.json`。
> 条款编号 `§04.x`。数据结构与 DDL 见 §03；目录与文件位置见 §02；任务编号（T*）见 §05。

---

## 4.0 子系统边界总览

```
┌────────────────────┐  §4.1 Agent 契约        ┌──────────────────────────┐
│ draft worker       │  LLM 网关（§01.2.4）      │ 云端 [OI] 兼容 API        │
│ (venv-app)         │ ───────────────────────▶ │  ↕ 失败兜底               │
│ JobStore 认领 task │ ◀─────────────────────── │ 本地模型（可选）           │
└─────────┬──────────┘  AgentResult[T]          └──────────────────────────┘
          │ scripts + script_sentences（逐句落库，§03.3.7）
          ▼
┌────────────────────┐  §4.3 VoiceEngine 契约  ┌──────────────────────────┐
│ voice worker       │  HTTP(127.0.0.1:8811)    │ TTS 服务进程 (venv-tts)   │
│ 认领 sentence      │ ───────────────────────▶ │ CosyVoice 常驻 GPU fp16   │
│                    │ ◀─────────────────────── │ /health /voices /synth    │
└─────────┬──────────┘  TTSResult(audio_path,…)  └──────────────────────────┘
          │ ffprobe 实测 ⇒ timeline.json（§4.2.7，时间轴唯一真相）
          ▼
┌────────────────────┐  §4.2 IR 协议          ┌──────────────────────────┐
│ render worker      │  VideoIR ─▶ FilterGraphIR│ FFmpeg 子进程            │
│ 认领 scene / final │  ─▶ filter_complex 文本  │ libx264 / NVENC / libass  │
└─────────┬──────────┘  + RandomizationPlan     └──────────────────────────┘
          │ 产物 + manifest.json
          ▼
┌────────────────────┐  §4.6 Publisher 契约   ┌──────────────────────────┐
│ publish worker     │  Playwright 浏览器自动化 │ 抖音 / 快手 / 视频号 …    │
│ 认领 publish       │ ───────────────────────▶ │ （登录态复用，不自动登录） │
└─────────┬──────────┘  PublishResult + 取证   └──────────────────────────┘
          │ publications + metrics_json ⇒ 回流 feedback（§06）
          ▼
┌────────────────────┐  §4.4 WS 广播协议      ┌──────────────────────────┐
│ 事件流（8 通道）    │ ──────────────────────▶ │ WebUI 操作台（11 面板）   │
└────────────────────┘  （只读下行 + REST 上行） └──────────────────────────┘
```

**契约稳定性分级（变更成本从高到低）**

| 级别 | 契约 | 破坏性变更的代价 |
| --- | --- | --- |
| **S** | `timeline.json`、`jobs` 单元映射、状态机迁移表、`tts_hash`/`render_hash` 公式 | 已产出的缓存与断点全部失效 ⇒ **必须走版本号 + 迁移** |
| **A** | `VoiceEngine` ABC、`Publisher` ABC、`VideoIR`、WS 信封 | 需同步改实现 + 契约测试 + 前端 |
| **B** | Agent 的 JSON Schema、模板 YAML | 需改提示词 + schema + golden |
| **C** | 日志 `source` 命名、面板内部结构 | 低风险，允许演进 |

---

## 4.1 智能体脚手架契约（原文 §2.2 八步管线）

### 4.1.0 八步管线与契约归属

```
[1]方向分析  [2]批量选题  [3]内容导演  [4]写稿   [5]审稿      [6]改稿⇄审稿  [7]确认闸  [8]配音
   Planner     Ideator      Director     Writer   Reviewer      Editor        Gate     → §4.3
   §4.1.2      §4.1.3       §4.1.4       §4.1.5   §4.1.6        §4.1.6       §4.4.4
   5–8 方向    每方向 4 个   3–5 段大纲   600–800字 0.3×规则+0.7×LLM  ≤2 轮     A 自动 / B 人工
```

**与原文的对应关系（逐条可追溯）**

| 原文步骤 | 契约 | 落库表 | 任务 |
| --- | --- | --- | --- |
| ① 方向分析 | §4.1.2 | `content_directions` | T1.9 |
| ② 批量选题 | §4.1.3 | `topic_candidates` | T1.9 |
| ③ 内容导演 | §4.1.4 | `scripts.outline_json` | T1.10 |
| ④ 写稿 | §4.1.5 | `scripts` + `script_sentences` | T1.10 |
| ⑤ 审稿（双通道） | §4.1.6 | `review_scores` + `scripts.score_*` | T1.11 |
| ⑥ 改稿（≤2 轮） | §4.1.6 | `scripts.version` + `editor_notes_json` | T1.11 |
| ⑦ 人工确认闸 | §4.4.4 | `approvals` + `audit_ops` | T1.11 / T4.4 |
| ⑧ 配音 | §4.3 | `script_sentences.tts_*` | T2.x |

### 4.1.1 通用 Agent 契约（`agents/base.py`）

```python
class AgentContext(BaseModel):
    """所有 Agent 的公共输入。persona 与预算必须注入，避免提示词与校验脱节。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str | None = None
    persona: PersonaSpec  # 原文 §2.1 频道定位
    trace_id: str  # 与 system_logs.request_id / llm_calls 关联
    seed: int | None = None
    token_budget_remaining: int | None = None  # R16 成本闸门


class AgentResult(BaseModel, Generic[T]):
    ok: bool
    data: T | None = None  # 通过 JSON Schema 校验后的结构化结果
    raw_text: str | None = None  # 原始输出（排障；不入库全文，只留 8KB 截断）
    engine: str  # 'oi_compatible' | 'ollama' | ...
    model: str
    profile: str  # ★T1.8 裁定 56：最后停在哪条通道（'' = 未调用任何通道）
    prompt_version: str  # 来自 prompts/manifest.yaml（"<version>+<sha12>"）
    input_tokens: int
    output_tokens: int
    latency_ms: int
    attempts: int  # ★T1.8 裁定 58：**跨通道累计**（云端 4 + 本地 4 = 8）
    warnings: list[str] = Field(default_factory=list)
    # 'repair:<channel>:<n>' | 'transport_retry:<channel>:<n>' | 'circuit_open:<channel>'
    # | 'budget_exceeded:<reason>'
    error_code: str | None = None
    error_message: str | None = None  # ★T1.8 裁定 56：失败原因（人读；与 error_code 并存）
    # 'LLM_SCHEMA_INVALID' | 'LLM_TIMEOUT' | 'LLM_BUDGET_EXCEEDED' | 'LLM_RATE_LIMIT'
    # | 'LLM_UPSTREAM' | 'LLM_CIRCUIT_OPEN' | 'LLM_ROUTE_MISSING'
    # | 'LLM_PROMPT_MISSING' | 'LLM_PROMPT_DRIFT'


class BaseAgent(ABC, Generic[TIn, TOut]):
    name: ClassVar[str]
    prompt_dir: ClassVar[str]  # prompts/<name>/
    schema_path: ClassVar[str]  # schemas/<name>_result.schema.json
    profile_key: ClassVar[str]  # config/llm.yaml 的 routing 键

    @abstractmethod
    async def run(self, ctx: AgentContext, payload: TIn) -> AgentResult[TOut]: ...

    async def _invoke(self, ctx: AgentContext, user_prompt: str) -> AgentResult[TOut]:
        """统一实现（只在 base.py，禁止各 Agent 重复实现）：
        1) 渲染 system.md + user 模板（注入 persona；模板变更 ⇒ prompt_version 变化）
           —— 模板只支持 ``{{变量}}`` 替换，控制流一律报错（T1.8 裁定 57）
        2) 检查 token 预算（R16）⇒ 超限按 budget.on_exceed 处理
           （'switch_to_local' | 'fail_task' | 'alert_only'）；闸门口径见 §01.2.4
        3) 按 llm.yaml routing 选通道（cloud → local 兜底；熔断打开的通道直接跳过）
        4) json_guard 校验 schema；失败则「修复重试」≤3：**追加两条消息**
           （assistant=上次输出 + user=校验错误）再问，不是重发同一请求
           ⇒ 每通道上限 1 + 3 = 4 次调用（T1.8 裁定 55）
        5) 通道失败（含修复重试耗尽）⇒ fallback 通道；仍失败 ⇒ 返回 error_code
           （**不抛裸异常**；§04.1.1 硬约束 5）
        6) 写 llm_calls（token/成本/耗时/模型/prompt_version，**失败也写**）
           + 经 LogSink 回调落 system_logs（T1.8 裁定 54/60）
        """
```

**硬约束**

1. 所有 Agent 输出**必须**是 JSON 且通过 `schemas/*.schema.json`；解析失败**不得**用正则补救（只会掩盖提示词问题）。
2. `prompt_version` 必须落库（`scripts.prompt_version`），保证"同输入同产物"可复现（P5）。
3. 单次 LLM 调用硬超时 **120s**（DoD 第 7 条）；超时按 `LLM_TIMEOUT` 走修复重试。
4. Agent **不得**直接写 `tasks.status`（只能返回结果，由 pipeline stage 决定迁移）。

### 4.1.2 Planner（方向分析）· 原文 §2.2①

```python
class GroundingRef(BaseModel):
    type: Literal["persona", "hot", "feedback"]
    ref_id: str | None = None
    kind: str | None = None  # feedback 用：'want' | 'complaint' | 'trend'
    quote: str | None = None


class DirectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=40)
    rationale: str = Field(max_length=200)  # 理由（WebUI"评分理由"直接展示）
    grounded_on: list[GroundingRef]  # 依据：热点/反馈/定位
    priority: int = Field(100, ge=0, le=999)  # 越小越优先
    risk_flags: list[str] = Field(default_factory=list)
    fit_score: int = Field(default=7, ge=0, le=10)  # ★ LLM 自评契合度（<6 丢弃，见下表）


class PlannerInput(BaseModel):
    hot_items: list[HotItemSpec] = Field(default_factory=list)
    feedback_digest: FeedbackDigest | None = None  # 已聚合的反馈摘要（§4.1.7）
    count_min: int = DIRECTION_COUNT_MIN  # = 5（唯一真相在 domain/topics.py）
    count_max: int = DIRECTION_COUNT_MAX  # = 8


class RuleViolation(BaseModel):
    rule: Literal["positioning", "want", "hot"]  # 三条"必须满足"的规则（"被吐槽"是降权不记违规）
    detail: str


class RuleReport(BaseModel):
    violations: list[RuleViolation] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)  # fit_score < 6 被丢弃的方向标题
    demoted: list[str] = Field(default_factory=list)  # 命中"被吐槽" ⇒ priority += 500 的方向标题
    low_grounding: bool = False  # 无热点且无反馈 ⇒ 整批打 low_grounding


class PlannerOutput(BaseModel):
    # ★ 模型层放宽到 min_length=1：5–8 由 schemas/planner_result.schema.json 保证（裁定 68）
    #   理由：过滤跑偏后不足 5 个时，宁可"4 个方向 + 告警"也不要整批丢弃
    directions: list[DirectionSpec] = Field(min_length=1, max_length=8)
    rule_report: RuleReport | None = None  # 规则校验留痕（`studio topics analyze --json` 直接输出）


def plan_directions(
    directions: Sequence[DirectionSpec],
    *,
    hot_available: bool,
    feedback_available: bool,
    min_fit: int = MIN_FIT_SCORE,  # 6
    demotion: int = COMPLAINT_DEMOTION,  # 500
) -> tuple[list[DirectionSpec], RuleReport]: ...
```

> **常量唯一真相**（`domain/topics.py`）：`DIRECTION_COUNT_MIN=5` / `DIRECTION_COUNT_MAX=8` / `MIN_FIT_SCORE=6` /
> `COMPLAINT_DEMOTION=500` / `DEDUP_SIMILARITY_THRESHOLD=0.85` / `DEDUP_SIMILARITY_PENALTY=2.0`。
> 服务层、Schema 与提示词都从这里取，禁止各自硬编码（裁定 77）。

**原文四条规则的机器化校验**

| 原文规则 | 校验实现 | 违反处理 |
| --- | --- | --- |
| 方向紧扣定位不跑偏 | `grounded_on` 必含 `type="persona"`；`fit_score < MIN_FIT_SCORE(6)` 则丢弃 | 丢弃并记 `rule_report.dropped` |
| 优先"用户想要"的方向 | 至少 1 个方向含 `type="feedback", kind="want"` | 不满足则重试 |
| 回避"被吐槽"的方向 | `risk_flags` 含 `complained_topic` ⇒ `priority += COMPLAINT_DEMOTION(500)`（排到末尾） | **降权而非丢弃**（记 `rule_report.demoted`） |
| 紧贴热点选有流量潜力的 | 至少 1 个方向含 `type="hot"` | 不满足则重试 |

> **降级**：`hot_items` 与 `feedback_items` 同时为空 ⇒ 仅凭 persona 产出方向，在每个 `DirectionSpec.risk_flags` 打
> `low_grounding`，同时 `RuleReport.low_grounding = True`（WebUI 提示"建议导入热点"）。
>
> **落库口径**：`content_directions` 表**没有** `fit_score` 列（§03.3.4）；落库的方向都已通过契合度过滤，
> 回读时 `direction_spec_from_row` 取默认 `7`（放行）—— 与事实一致，不虚构 LLM 原始分数（裁定 73）。

### 4.1.3 Ideator（批量选题）· 原文 §2.2②

```python
class TopicSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=50)  # 标题有网感、有钩子
    hook_type: Literal["conflict", "suspense", "contrast", "number", "other"]
    angle: str = Field(max_length=120)  # 角度差异化说明
    exec_feasible: bool = True  # 3 分钟内可执行
    score: float = Field(ge=0, le=10)  # 供瀑布流排序
    reason: str = Field(max_length=200)  # 评分理由


class IdeatorInput(BaseModel):
    direction: DirectionSpec
    per_direction: int = Field(default=4, ge=1, le=10)  # 原文 §2.2②：每方向 4 个
    existing_titles: list[str] = Field(default_factory=list)  # R15 去重输入（全状态历史，最近 500 条）


class IdeatorOutput(BaseModel):
    topics: list[TopicSpec] = Field(min_length=3, max_length=5)


class ExistingTopic(BaseModel):
    id: str | None = None
    title: str
    dedup_hash: str | None = None  # 历史行已有哈希 ⇒ 直接比哈希，不必重算


class DedupAction(StrEnum):
    KEEP = "keep"  # 无冲突
    DEMOTE = "demote"  # 相似度 ≥ 0.85 ⇒ 降分 + 写 similar_to_json
    DROP = "drop"  # 归一化哈希完全命中 ⇒ 丢弃，不入库


class DedupMatch(BaseModel):
    target_id: str | None = None
    target_title: str
    similarity: float = Field(ge=0.0, le=1.0)


class DedupResult(BaseModel):
    action: DedupAction
    dedup_hash: str  # 本条的归一化哈希（落库）
    similarity: float = 0.0  # 命中的最高相似度
    score_delta: float = 0.0  # DEMOTE 时为 -DEDUP_SIMILARITY_PENALTY(-2.0)
    reason: str = ""
    similar_to: list[DedupMatch] = Field(default_factory=list)  # 最多 DEDUP_MATCH_LIMIT(3) 条


def dedup_topic(
    title: str,
    existing: Iterable[ExistingTopic],
    *,
    threshold: float = DEDUP_SIMILARITY_THRESHOLD,  # 0.85
    penalty: float = DEDUP_SIMILARITY_PENALTY,  # 2.0
) -> DedupResult:
    """两级去重（R15）：
    ① 归一化哈希完全命中 ⇒ action='drop'（**写入前**拦下，不产生 topic_candidates 行）
    ② 字符级相似度 difflib.SequenceMatcher(autojunk=False) ≥ 0.85 ⇒ action='demote'，降分 + 写 similar_to_json
    归一化 = NFKC + casefold + 只保留 CJK/字母/数字 + 数字折叠为 '#' + 同义词长词优先替换
    结果写入 topic_candidates.dedup_hash / similar_to_json
    """
```

**产出量**：`len(directions) × per_direction` ⇒ 5–8 方向 × 4 = **20–32 个选题**（原文"一次可达 20+"）。

**降级**：单个方向失败**不**影响其他方向 —— 每方向独立 try/except + 独立事务，失败只记一条
`DirectionOutcome` + 一条 `warn`（不静默）。

> **`topic_batch` 队列单元推迟到四池接线（T4.11）**（裁定 71 / T1.12 复核）：此刻仍没有能认领它的 worker，提前入队只会留下永远
> `pending` 的作业 —— 比"没有作业"更难排查。`run_planner` / `run_ideator` 已是**批次键控**（`batch_id`），
> 届时接线是纯增量。原"每批次建一条 `tasks` 行"作废。

### 4.1.4 Director（内容导演）· 原文 §2.2③

```python
class ScriptSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=1)
    point: str = Field(max_length=120)  # 该段要点
    visual: str = Field(max_length=200)  # 画面建议（喂给模板/素材选择的语义提示）
    mood: str = Field(max_length=40)  # 情绪（驱动 TTS emotion 与 BGM 段落）
    est_chars: int = Field(ge=80, le=350)  # 该段预估字数


class DirectorOutput(BaseModel):
    hook_3s: str = Field(max_length=80)  # 黄金 3 秒钩子
    segments: list[ScriptSegment] = Field(min_length=3, max_length=5)  # 正文 3–5 段
    cta: str = Field(max_length=80)  # 结尾 CTA
    est_duration_ms: int = Field(ge=60_000, le=180_000)  # 时长预估（≤3 分钟）
```

**硬约束（原文"每 300 字一个段落"）**

| 校验 | 阈值 | 依据 |
| --- | --- | --- |
| 段落数 | 3–5 段 | §2.2③ |
| 每段字数 | `est_chars ∈ [80, 350]`，正文总量/段数 ≈ 200–300 字 | 支撑"每 300 字一段落" |
| 总字数 | `Σ est_chars ∈ [600, 800]` | §2.2④ |
| 时长 | `est_duration_ms ∈ [60s, 180s]` | "3 分钟以内" |
| 段落数 > 主画面场景数 | **不报错** | 由 `repeat_last` 扩展场景（§4.2.3） |

**输入契约（T1.10 补齐）**

```python
class DirectorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: TopicSpec
    target_duration_ms: int = Field(default=180_000, ge=60_000, le=180_000)
    angle: str | None = Field(default=None, max_length=120)  # 非空则优先于 topic.angle
```

**规则闸与降级（T1.10 落地）**

| 环节 | 实现 | 依据 |
| --- | --- | --- |
| 结构约束 | pydantic + `schemas/director_result.schema.json` | 段数 / 单段字数 / 时长区间的**唯一保证** |
| 语义校验 | `domain.script.check_outline()` → `OutlineReport` | `seq` 从 1 连续、`Σ est_chars ∈ [600, 800]`、钩子与 CTA 非空 |
| 不合规 | 带 `retry_hint` 重试 ≤2 轮（`OUTLINE_RETRIES = 2`） | 与 §4.1.2「丢弃并重试 ≤2」同口径 |
| 仍不合规 | **降级返回**：有结构就用，问题写进 `warnings`（`outline:<problem>`） | 字数最终由 Writer 决定，这里只是**预分配**；一次字数差 30 字的大纲比"0 段 + 一条错误"有用（DoD 6：可降级、无静默失败） |

### 4.1.5 Writer（写稿）· 原文 §2.2④

```python
class WriterInput(BaseModel):
    topic: TopicSpec
    outline: DirectorOutput
    persona: PersonaSpec
    revision_notes: list[str] = Field(default_factory=list)  # 非空 ⇒ 本轮为改稿


class SentenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=28)  # ★ 与 TTS 单句上限一致（§4.3.6）
    speaker: Literal["bigbear", "littlebear", "narrator"]
    emotion: str = "neutral"
    pause_after_ms: int = Field(200, ge=0, le=2000)


class WriterOutput(BaseModel):
    title: str = Field(max_length=60)
    hook: str
    body_md: str  # 全文（600–800 字）
    cta: str
    sentences: list[SentenceSpec] = Field(min_length=15)  # 逐句（按句配音的输入）
    est_duration_ms: int
    catchphrases_used: list[str] = Field(default_factory=list)  # 口癖命中（供审稿复核）
```

**硬约束（原文 §2.2④）**

| 原文要求 | 校验实现 | 违反处理 |
| --- | --- | --- |
| 600–800 字 | `600 ≤ 字数(body_md) ≤ 800` | 重写 ≤2 次，仍越界 ⇒ 取最接近的一版 + `warn` |
| 口吻/口癖必须带上 | `persona.catchphrases` 至少命中 **2 个**（自报 + 服务端复核） | 重写 |
| 口语化、句子短、适合朗读 | 平均句长 ≤ 30 字；`sentences[].text ≤ 28 字` | 超长**强制切分**（不重写） |
| 每 300 字一个段落 | 段落数 ≈ `ceil(字数/300)` | 重排段落 |
| 绝不碰禁区 | `persona.forbidden` + `banned_words.yaml` 全量扫描 | 命中即 `block` 并重写 |
| 双人设分工 | `speaker` 单人占比 ≤ 70% | `warn` + 建议重写 |

> **句子落库语义**：`sentences` 逐条写 `script_sentences`（`text_raw` 存 LLM 原文，`text` 存生效文本）。**同一事务内**写入并回填 `scripts.body_md`，避免"有稿无句"的半成品。

**输入契约修正（T1.10 裁定 82）**：`persona` **不在** `WriterInput` 里 —— 它由 `AgentContext.persona` 携带，`BaseAgent` 据此自动注入 `{{catchphrases}}` / `{{forbidden}}` / `{{persona_*}}` 等模板变量。实际签名为 `{topic, outline, revision_notes}`：同一次运行里 persona 只有**一份真相**，Agent 也不得再传同名 kwarg（会撞成 `got multiple values`）。

**阈值随 persona 走（T1.10 裁定）**：`persona` 是可编辑文件 ⇒ 阈值不能写死在函数里。

```python
class ScriptRules(BaseModel):
    word_count_min: int = 600  # ← persona.target_chars_min
    word_count_max: int = 800  # ← persona.target_chars_max
    duration_max_ms: int = 180_000  # ← persona.max_duration_ms
    sentence_max_chars: int = 28  # 与 TTS 单句上限同源（§4.3.6）
    catchphrase_min_hits: int = 2
    speaker_ratio_max: float = 0.7
```

**规则闸（`domain.script.check_script()` → `ScriptReport`）**

| 检查 | 阈值 | 违反处理 |
| --- | --- | --- |
| 禁区 | `persona.forbidden`（忽略空白差异全量扫描） | **`block`**：不重写、不进评分，`error_code = SCRIPT_FORBIDDEN` |
| 字数 | `count_chars(body_md) ∈ [min, max]` | 重写 ≤2 次；仍越界 ⇒ 取**离区间最近**的一版 + `script:字数 …` warn（**不整批失败**） |
| 口癖 | 服务端复核命中 ≥ `catchphrase_min_hits`（**不信模型自报**） | 重写；自报但复核不到 ⇒ `catchphrase_self_report:<词>` 留痕 |
| 句长 | `sentences[].text ≤ 28 字` | **强制切分**（不重写：切分比重写省 token） |
| 单人占比 | 最高说话人 ≤ 70%（按**口播字数**加权，不是按句数） | `warn`（`speaker_ratio:<人>=0.xx` / `single_speaker:<人>`） |

> **`SentenceSpec.text` 的 200 vs 28（裁定 78）**：契约层留 `max_length=200`（模型偶尔写长句是常态），**28 由服务端 `enforce_sentence_limit()` 强制**。若在 JSON Schema 里卡 28，"超长句"就变成网关的**修复重试**（另一套预算、另一种失败），而规格书要求的是"切分、不重写"——降级路径不能长在 schema 上。
>
> **`est_duration_ms` 落库值一律 `estimate_duration_ms(word_count)`（≈5 字/秒，裁定 84）**：模型自报值只留在 `WriterOutput` 里，不进 `scripts` 表 —— 否则"时长"会变成一个**没法复算**的数。
>
> **重写用尽后的取舍（裁定 86）**：按 `_distance`（到字数区间边界的距离）挑**最接近**的一版；距离并列时保留**先出现**的那一版 —— 并列规则必须可解释、可复盘。
### 4.1.6 Reviewer（双通道评分）与 Editor（改稿）· 原文 §2.2⑤⑥

```python
class ChannelItem(BaseModel):
    score: float = Field(ge=0, le=10)
    comment: str = Field(max_length=200)


class RuleChannelDetail(BaseModel):
    """通道一：规则（权重 0.3）· 原文 §2.2⑤ 四项"""

    length: ChannelItem  # 稿件长度（600–800 字）
    banned_hits: list[BannedHit]  # 禁区词命中（命中即 0 分并 block）
    opening_ok: bool  # 开场完整性（钩子存在且 ≤80 字）
    ending_ok: bool  # 结尾完整性（CTA 存在）
    paragraph_dup_ratio: float  # 段落重复度（>0.3 扣分）
    chars: int
    est_duration_ms: int
    catchphrases_hit: int


class LlmChannelDetail(BaseModel):
    """通道二：LLM 六维度（权重 0.7）· 原文 §2.2⑤ 六项，各 0–10 分 + 评语"""

    hook_opening: ChannelItem  # 开场钩子
    positioning_fit: ChannelItem  # 定位契合
    oral_style: ChannelItem  # 口语化
    emotion_rhythm: ChannelItem  # 情绪节奏
    ending_cta: ChannelItem  # 结尾引导
    forbidden: ChannelItem  # 禁区


class ReviewIssue(BaseModel):
    code: str  # 'HOOK_WEAK' | 'BANNED_WORD' | 'LENGTH_LOW' | 'CTA_MISSING' ...
    severity: Literal["block", "major", "minor"]
    target: str  # 'hook' | 'segment:3' | 'cta' | 'global'
    detail: str
    suggestion: str


class ReviewOutput(BaseModel):
    rule_total: float = Field(ge=0, le=10)
    rule_detail: RuleChannelDetail
    llm_total: float = Field(ge=0, le=10)
    llm_detail: LlmChannelDetail
    issues: list[ReviewIssue]  # ★ 传给 Editor（原文"只改被指出的问题"）
    verdict: Literal["pass", "revise", "reject"]


class EditorInput(BaseModel):
    script: WriterOutput
    issues: list[ReviewIssue]  # 上一轮审稿指出的问题（只改这些）
    round_no: int = Field(ge=1, le=2)  # ★ 原文 §2.2⑥：最多 2 轮
```

**评分公式（硬契约 · 唯一实现于 `domain/scoring.py`）**

```python
RULE_WEIGHT, LLM_WEIGHT = 0.3, 0.7
GRADE_A_MIN, GRADE_B_MIN = 8.0, 5.0


def compute_score(rule_total: float, llm_total: float) -> tuple[float, Grade]:
    total = round(RULE_WEIGHT * rule_total + LLM_WEIGHT * llm_total, 2)
    grade = Grade.A if total >= GRADE_A_MIN else (Grade.B if total >= GRADE_B_MIN else Grade.C)
    return total, grade
```

| 分级 | 区间 | 处置 | 目标状态 |
| --- | --- | --- | --- |
| **A** | ≥ 8.0 | `pass_auto` ⇒ 直接放行 | `reviewing → queued_voice`（`approved_by='auto_approve_A'`） |
| **B** | 5.0 – 7.9 | `need_edit` ⇒ 进 Editor（≤2 轮）→ 重审；2 轮后仍 B ⇒ 进确认闸 | `reviewing → editing → … → awaiting_approval` |
| **C** | < 5.0 | `discard` ⇒ 废弃（选题候选置 `rejected`，可人工捞回） | `reviewing → discarded` |

**已实测的评分基线（`tests/golden/scoring/*.json`）**：`rule=8.0, llm=9.0 ⇒ 8.7 → A`；`7.0/6.0 ⇒ 6.3 → B`；`4.0/4.5 ⇒ 4.35 → C`。

**Editor 的三条约束**

1. **只改被指出的问题**：`issues` 之外的段落必须逐字保持不变（测试用 diff 断言改动行数上限）。
2. **版本可追溯**：每轮 `scripts.version += 1`，旧版本 `is_active=0` 但**保留**（WebUI 可对比）。
3. **轮次硬上限 2**：`round_no > 2` 直接进确认闸（原文"最多改 2 轮"），**禁止**无限循环烧 token（R16）。

**T1.11 施工修正（裁定 89–101）—— 本节上述签名有三处被实现收窄/澄清**

| 契约 | 规格书原文 | T1.11 实现 | 为什么 |
| --- | --- | --- | --- |
| LLM 产出 | `ReviewOutput`（含 `total` / `verdict`） | **`ReviewerOutput` 只有 `llm_detail` + `issues`**；`ReviewOutput` 是**服务端**产出 | 让模型报总分 = 让考生自己判卷；`0.3×规则+0.7×LLM` 这条硬契约会失去唯一实现（裁定 89） |
| `need_edit` | 隐含"去改稿" | 语义 = "**未自动放行**" | DDL 把 `review_scores.decision` 锁死 5 值、**没有**"等人工"这一项；`AutoApprovePolicy.OFF` 下 A 级也落 `need_edit`（裁定 90） |
| `round_no` 上界 | `le=2` | `ge=1`，**无上界** | 人工退回后 `revision_round` 继续加 ⇒ 审稿轮次自然 > 2；轮次闸门由 `gate_action` 把守，输入模型不当第二道闸（裁定 100） |

```python
class ReviewerOutput(BaseModel):
    """★ LLM 的产出（`schemas/review_result.schema.json`）—— 只有这两样"""

    llm_detail: LlmChannelDetail
    issues: list[ReviewIssue] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    """★ 服务端的产出（落 `review_scores` 的就是它）"""

    rule_total: float  # 服务端算（四项等权平均 − 重复度扣分）
    rule_detail: RuleChannelDetail
    llm_total: float  # 六维度算术平均
    llm_detail: LlmChannelDetail
    issues: list[ReviewIssue]
    total: float  # = round(0.3*rule_total + 0.7*llm_total, 2)
    grade: Grade  # A / B / C
    decision: Decision  # 落库事实（5 值，DDL 锁死）
    round_no: int

    @property
    def verdict(self) -> Literal["pass", "revise", "reject"]:
        """由 grade 派生，**不接受模型自报**。"""
```

**两个通道各自怎么算出来（`domain/scoring.py` 是唯一实现）**

- `rule_total` = **四项等权平均**（长度 / 禁区 / 开场 / 结尾，各 0–10）− 重复度扣分（`paragraph_dup_ratio > 0.3` ⇒ −2.0）。
  长度项以**区间宽度做分母**线性扣分 ⇒ 改 persona 阈值时曲线跟着缩放，不用改代码（裁定 92）。
- `llm_total` = 六维度**算术平均**（裁定 93）。
- `Decision`（落库事实）与 `GateAction`（动作）**拆成两个枚举**：`AUTO_PASS` / `EDIT` / `HUMAN_GATE` / `DISCARD`；
  `GateAction.HUMAN_GATE ⇒ Decision.NEED_EDIT`（裁定 90）。
- **自动放行也必须写一条 `approvals` 记录**（`status='approved'`、`decided_by='auto_approve_A'`，裁定 94）。

**Editor 的"只改被指出的问题"是句子级机检（裁定 95/96/98）**

| `target` | 映射到 | 说明 |
| --- | --- | --- |
| `hook` | 第 1 句 | 开场钩子 |
| `cta` | 最后一句 | 结尾引导 |
| `segment:N` | `ceil(句数/段数)` 算出的句区间 | **刻意取宽**：宁松不误杀 |
| `global` | 全篇 | 全局性问题（如禁区、重复度） |

- 越界 ⇒ **带提示重试 1 次**；仍越界 ⇒ **照收 + `warnings`**，**不判失败**（"改稿失败"比"稿件稍越界"贵得多，裁定 96）。
- Editor **整份回写稿件**（不返回 diff）：diff 由服务端比对算，模型只负责"改完的稿子长什么样"（裁定 98）。

**每轮审稿都落 `review_scores`**（`UNIQUE (script_id, round_no)`，裁定 97）：重跑同一版撞唯一键是**故意的** —— "这一版被审过几次"必须能从库里看出来。

### 4.1.7 输入源解析契约（原文 §2.1 三样输入）

```python
class HotItemSpec(BaseModel):
    title: str
    heat: float | None = None  # 归一化热度（可缺失 ⇒ None，排序时置底）
    platform: str | None = None
    raw_line: str  # ★ 原始行（前端要能说清"第 7 行错在哪"）
    line_no: int = Field(ge=1)
    heat_raw: str | None = None  # ★ 第 2 列原文（'9821' | '爆' | '高'）：落库即 hot_items.heat(TEXT)
    parse_ok: bool = True  # ★ False ⇒ **照入库留痕**，不参与选题（裁定 65/67）
    parse_error: str | None = None  # 坏在哪（WebUI 直接展示，不猜）


class FeedbackItemSpec(BaseModel):
    platform: str | None = None
    date: str | None = None  # YYYY-MM-DD ⇒ 落库 feedback_items.occurred_on
    sentiment: Literal["pos", "neu", "neg", "unknown"] = "unknown"  # 领域口径，见下方"情感双口径"
    kind: Literal["want", "complaint", "trend", "other"] = "other"
    text: str
    is_auto: bool = False  # ★ 由 §06 记忆沉淀回流的自动反馈（Q4 默认容错解析）
    raw_line: str
    line_no: int = Field(ge=1)
    warnings: list[str] = Field(default_factory=list)  # ★ 容错解析的留痕（如 "feedback_head_bad"）


class FeedbackDigest(BaseModel):
    total: int = 0
    wants: list[FeedbackItemSpec] = Field(default_factory=list)  # 用户想要（Planner 优先采纳）
    complaints: list[FeedbackItemSpec] = Field(default_factory=list)  # 被吐槽（Planner 降权）
    trends: list[FeedbackItemSpec] = Field(default_factory=list)
    top_topics: list[tuple[str, int]] = Field(default_factory=list)  # 词频 Top-N（供 Planner 提示）


class FeedbackBatchItem(BaseModel):
    ref: str = Field(description="回抄锚点 = feedback_items.id")  # ★ 必须原样回抄
    text: str
    platform: str | None = None


class ClassifiedItem(BaseModel):
    ref: str  # ★ 回抄对齐：对不上 ⇒ **丢弃该条而不是猜**（猜错会把"想看的"写成"吐槽的"）
    sentiment: Literal["pos", "neu", "neg", "unknown"] = "unknown"
    wants: list[str] = Field(default_factory=list)
    complaints: list[str] = Field(default_factory=list)
```

**情感双口径（裁定 66）**：领域层一律用 `pos/neu/neg/unknown`（§04.1.7），落库一律用 DDL 口径
`positive/neutral/negative/unknown`（§03.3.3，`CHECK` 约束）。换算只允许在
`domain.SENTIMENT_TO_DB` / `domain.DB_TO_SENTIMENT` 一处发生 —— 否则运行期才被 `CHECK` 拒绝。

**`ref` 回抄纪律**：`FeedbackClassifierAgent` 的输出必须逐条回抄输入 `ref`（= `feedback_items.id`）。
缺失或对不上 ⇒ 该条走关键词兜底并写 `warn`（`feedback_ref_missing:<id>`），**禁止**按顺序错位匹配。

| 输入 | 路径 | 格式 | 解析失败处理 |
| --- | --- | --- | --- |
| 热点 | `data/hot/*.md` | 每行 `标题\|热度\|平台`（**原文明确**） | 单行跳过 + `warn` + **`parse_ok=0` 照入库**（不中断整批、不静默丢数据） |
| 历史反馈 | `data/feedback/*.md` | **自由文本**（Q4：支持可选结构化头 `平台\|日期\|情感\|内容`） | 无法解析 ⇒ 整行作为 `text`，`sentiment='unknown'` |
| 频道定位 | `config/persona.yaml` | 人设/口吻/受众/口癖/禁区 | 缺必填 ⇒ **启动即报错**（唯一人工必填项） |

**解析器契约（`scripts/parse_hot.py` / `parse_feedback.py` 的可测内核在 `services/input_service.py`）**

- 幂等：`hot_items` 按 `(source_file, line_no, raw_line)` 去重；`feedback_items` 按 `(source_file, content)` 去重
  （DDL 无 `line_no` 列，见 §03.3.3）—— 重复导入不产生重复行。
- 归档：消费后热点移入 `data/hot/archive/`（保留 90 天，用于复盘"当时为什么选这个方向"）。
  **归档判据是 `list_consumed_sources()`（看库里的事实）**，且 `list_unconsumed_sources()` 必须带
  `parse_ok = 1` —— 否则文件里只要有一个坏行就**永远归档不了**，每轮重复解析、重复报同一条（裁定 74）。
- 消费留痕：热点被读过但没被任何方向引用 ⇒ `mark_consumed(direction_id=None)` 只写 `consumed_at`，
  `used_by_direction_id` 留 `NULL`（编一个方向 id 会让"这个热点支撑了哪个方向"在复盘时变成错的，裁定 72）。
- **禁止**在解析阶段调用 LLM（成本与不确定性）；情感/诉求分类由 Planner 阶段批量做一次。

### 4.1.8 Cover Agent（封面文案）· 原文 §8"快速封面"

```python
class CoverOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_text: str = Field(max_length=20)  # 封面主文案（竖屏大字，≤2 行 × ≤10 字）
    sub_text: str | None = Field(None, max_length=24)
    highlight_words: list[str] = Field(default_factory=list)  # 高亮词（换色）
    frame_at_ms: int  # 抽帧时刻（默认 hook 句 start_ms + 500）
    banned_checked: bool  # 必须为 True（禁区词已扫描）
```

**契约**：封面**只生成文案与抽帧点**，实际合成由 `publish/cover.py` 用 ffmpeg `drawtext` 完成（§4.6.2）。文案禁区扫描复用 `persona.forbidden` + `banned_words.yaml`，命中 ⇒ `banned_checked=False` ⇒ 拒绝发布（R14）。

---

## 4.2 渲染引擎契约（一期：单遍合成 / 二期：三层模板编排）

> ⚠️ **v3.1 范围修订（口述 D2/D5 · 裁决 C12/C13 · 详见 §README.0.5）**
> **一期（P0 · 本期必做）= §4.2.8 单遍合成**：跑酷循环裁长 → 缩放铺满 → 叠加**固定水印** → 混音 → 一次编码出片。
> **不做**句子↔镜头对齐（D2），**不做**场景中间产物（C13）。
> **二期（P1 · 可选扩展）= §4.2.1–§4.2.7 三层模板编排**：以下 §4.2.1–§4.2.7 全部保留为二期设计，**不阻塞 M3**。
> 阅读顺序建议：先读 §4.2.8（一期），需要二期时再回看 §4.2.1–§4.2.7。
### 4.2.0【二期 P1】三层模板编排总览（原 v3.0 设计，保留）

### 4.2.1【二期】渲染流水线（四步降维）与 IR 数据结构

```
 稿件 + timeline.json + 模板(YAML) + RandomizationPlan(seed)
        │ ① builder：语义绑定（句子→场景窗口、时间→镜头区间）
        ▼
     VideoIR                  （纯数据、无 FFmpeg 概念、可单测、可 golden 快照）
        │ ② layout：几何解析（rect→绝对像素、安全区裁剪、z 序排序、输入槽位分配）
        ▼
     FilterGraphIR            （inputs[] + nodes[] + outputs[]，仍是结构化数据）
        │ ③ emit：字符串化（标签命名、路径转义、链长切分、扇出复用）
        ▼
  filter_complex 文本  ──④ runner──▶  ffmpeg 子进程  ──▶  scene_00N.mp4 / final.mp4
```

> **铁律**：IR 层**不得**出现 ffmpeg 特有语法（`trim=start=`、`=`、`:` 参数），只有 `FilterGraphIR` 才允许。这样 IR 可被 WebUI 直接渲染成分镜预览、被随机化审计、被快照测试。

```python
class FitMode(StrEnum):
    COVER = "cover"
    CONTAIN = "contain"
    STRETCH = "stretch"


class TimeRange(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms 必须大于 start_ms")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class BrollSource(BaseModel):
    """MC 跑酷素材源：★ 防搬运随机化的承载点（字段全部来自 RandomizationPlan）"""

    kind: Literal["broll"] = "broll"
    clip_path: Path
    in_ms: int
    out_ms: int  # 入/出点（避开固定片头片尾）
    fit: FitMode = FitMode.COVER
    roi: tuple[int, int, int, int] | None = None  # (x,y,w,h) 随机裁切窗
    zoom: float = 1.0  # aggressive 档：1.02–1.12
    speed: float = 1.0  # aggressive 档：0.92–1.10
    mirror: bool = False  # has_text=1 的素材禁止镜像
    color: ColorAdjust
    lut_path: Path | None = None
    noise_strength: int = 0
    vignette: float | None = None
    loop: bool = True


class SolidSource(BaseModel):
    kind: Literal["solid"] = "solid"
    color: str = "#000000"  # ★ 无素材降级：纯黑 + 纯字幕/标题


class ImageSource(BaseModel):
    kind: Literal["image"] = "image"
    path: Path
    fit: FitMode = FitMode.CONTAIN


class TextSource(BaseModel):
    kind: Literal["text"] = "text"
    text: str
    font_path: Path
    size: int
    fill: str
    stroke: tuple[str, int] | None = None
    shadow: tuple[str, int, int, int] | None = None
    align: Literal["left", "center", "right"] = "center"
    max_chars_per_line: int = 13
    max_lines: int = 2
    animation_in: Animation
    animation_out: Animation


class SubtitleSource(BaseModel):
    """字幕走 ASS（libass）通道，不用 drawtext —— 实现逐字卡拉OK与复杂描边。"""

    kind: Literal["subtitle"] = "subtitle"
    entries: list[SubtitleEntry]
    style: SubtitleStyle
    ass_path: Path


class AudioSource(BaseModel):
    kind: Literal["voice", "bgm", "sfx"]
    path: Path
    gain_db: float = 0.0
    loop: bool = False
    ducking: DuckingSpec | None = None  # 仅 bgm
    trim: TimeRange | None = None


class LayerIR(BaseModel):
    z: int
    rect: tuple[int, int, int, int]  # (x,y,w,h) 模板声明值（可为相对）
    opacity: float = 1.0
    source: BrollSource | ImageSource | SolidSource | TextSource | SubtitleSource
    animation_in: Animation | None = None
    animation_out: Animation | None = None


class SceneIR(BaseModel):
    seq: int
    scene_id: str
    role: Literal["opening", "main", "ending"]
    cloned_from: int | None = None  # ★ repeat_last 克隆来源（审计用）
    time: TimeRange
    sentence_ids: list[str]
    layers: list[LayerIR]
    transition_in: TransitionSpec | None = None


class VideoIR(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    template_id: str
    template_version: int
    canvas: CanvasSpec  # w/h/fps/sar/colorspace
    total_ms: int
    scenes: list[SceneIR]
    audio: list[AudioSource]  # [voice_master, bgm, (sfx...)]
    loudness: LoudnessSpec
    randomization: RandomizationPlan  # ★ 随机决策留痕（§4.2.4）
    seed: int


class InputSpec(BaseModel):
    index: int  # ffmpeg 输入序号（编译器分配，禁止硬编码）
    path: Path
    kind: Literal["video", "audio", "image", "lavfi"]
    args: list[str] = Field(default_factory=list)  # ['-stream_loop','-1'] / ['-f','lavfi','-i',...]
    canonical: str  # 去重键（同文件 + 同参数 ⇒ 复用同一 input）


class FilterNode(BaseModel):
    out_label: str  # '[v1_l3]' / '[a0]'
    filter: str  # 'trim' / 'overlay' / 'ass' ...
    args: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)


class OutputSpec(BaseModel):
    label: str  # '[vout]' / '[aout]'
    map_args: list[str]  # ['-map','[vout]']
    codec_args: list[str]  # 来自 output profile


class FilterGraphIR(BaseModel):
    inputs: list[InputSpec]
    nodes: list[FilterNode]
    outputs: list[OutputSpec]
    estimated_nodes: int  # 复杂度守卫：>60 ⇒ 编译期拒绝并降级拆分
```

**三个 pass 的职责（纯函数、可单测）**

| Pass | 名称 | 输入 → 输出 | 关键职责 |
| --- | --- | --- | --- |
| ① | **bind**（`builder.py`） | `script + timeline + template + plan` → `VideoIR` | 句子 → 场景窗口映射；时间区间由**实测音频**推导；绑定 `$sentence.*` / `$script.*`；执行 `repeat_last` 扩展（§4.2.3） |
| ② | **layout**（`compiler.py`） | `VideoIR` → `VideoIR`（已解析） | `rect` → 绝对像素；安全区裁剪（文字类强制）；z 排序；`input_slot` → 真实索引分配；时长对齐 |
| ③ | **emit**（`graph.py` + `filters.py`） | `VideoIR` → `FilterGraphIR` → `str` | 每层滤镜链；`overlay` 合成；`xfade` 转场；ASS 烧录；音频 `concat`+侧链+`loudnorm`；路径转义；链长切分与 `split` 扇出 |
### 4.2.2【二期】自动填充契约（原文 §4.3）

```python
class VoiceSegment(BaseModel):
    sentence_id: str
    seq: int
    speaker: str
    audio_path: Path
    start_ms: int
    end_ms: int
    duration_ms: int


class SceneAssignment(BaseModel):
    seq: int
    scene_id: str
    role: str
    cloned_from: int | None
    sentence_ids: list[str]
    start_ms: int
    end_ms: int


class BgmSlot(BaseModel):
    clip_id: str
    path: Path
    start_ms: int
    fade_in_ms: int
    fade_out_ms: int


class FillPlan(BaseModel):
    """把「稿件 + 时间轴 + 模板」映射为「每个场景装什么」。原文 §4.3 的机器化表达。"""

    title_text: str  # 标题 → 片头【标题】组件
    subtitle_entries: list[SubtitleEntry]  # 字幕 → 主画面【字幕】组件（与配音句对齐）
    voice_segments: list[VoiceSegment]  # 配音 → 主画面【配音】组件（按句分配）
    bg_picks: list[BrollPick]  # 背景 → 从 data/assets/mc_parkour 随机抽取
    bgm_slots: list[BgmSlot]  # BGM → 模板自带，全局不变
    scene_map: list[SceneAssignment]  # 场景 ↔ 句子窗口的最终映射
```

| 原文 §4.3 要求 | 实现 |
| --- | --- |
| 标题 → 自动填入片头【标题】组件 | `title_text = script.title`（或 `hook`，由模板 `bind` 决定） |
| 配音 → 自动填入主画面【配音】组件（按句分配） | `voice_segments`：每句一条，携带 `sentence_id/audio_path/start_ms/end_ms/speaker` |
| 字幕 → 自动填入主画面【字幕】组件（与配音句对齐） | `subtitle_entries` 的时间直接取自 `timeline.json`（**不估算**） |
| 背景 → 自动从素材库抽取跑酷画面 | `bg_picks` 来自 §4.2.4 随机化计划（通配 `parkour_*.mp4`） |
| BGM → 模板自带，全局不变 | `bgm_slots` 由模板 `bgm_slot` 组件声明，填充期不改 |
| **段落数 > 场景数时复制最后一个主画面场景继续填** | **`repeat_last`**（§4.2.3） |

**场景切片的唯一判据**：`sentence_window`（模板声明）+ 句子**实测**时长。

```
句子： s001(3200ms) s002(2800ms) │ s003(4100ms) s004(3600ms) │ s005(5200ms)
模板： hook(1–2)                  │ payload(3–4)              │ cta(5)
场景时长 = Σ句实测时长 + Σ句间停顿 + tail_ms
   hook = 3200 + 2800 + (420 + 200) + 300 = 6920ms
```

### 4.2.3【二期】场景扩展策略 `repeat_last`（原文 §4.3"无限扩展"）

```python
def build_scene_map(
    *, sentences: list[TimelineSentence], template: TemplateSpec, seed: int
) -> tuple[list[SceneAssignment], list[str]]:
    """返回 (场景分配, 警告列表)。算法：
    1) 按 sequence 顺序遍历模板场景：
       - opening / ending：duration_policy.mode == 'fixed' ⇒ 取固定时长，**不承载句子**
       - main：把句子按实测时长填满 duration_policy.max_ms 窗口
    2) 主画面场景用尽而句子仍有剩余 ⇒ **克隆最后一个主画面场景定义**（新 seq，cloned_from=源 seq）
    3) 每个场景时长 = Σ(句实测时长 + pause_after_ms) + tail_ms
    4) 句间停顿 jitter 由任务种子派生（可复现，±80ms）
    5) 校验 duration_policy.min_ms/max_ms；越界优先调整窗口边界，仍越界记 warn
    """
```

```
模板主画面场景：2 个（main_01, main_02），每个窗口 25s
稿件：7 段 × ~25s ≈ 175s

分配结果：
  opening(3s) → main_01(25s) → main_02(25s) → main_03=克隆main_02(25s)
              → main_04=克隆main_02(25s) → … → main_07=克隆main_02(25s) → ending(5s)
  ⇒ 场景数由 2 自动扩展为 7
```

> **`repeat_last` 的两个必要条件**（不满足则画面错乱，由 `TPL_ABSOLUTE_TIME_IN_REPEATABLE` 校验拦截）：
> ① 被克隆的场景定义**不得含绝对时间绑定** —— 时间一律用 `$sentence.start_ms/end_ms` 相对化。
> ② 克隆场景的 `bg_picks` **必须重新抽取**（不得与源场景同素材同入点），否则长视频内出现重复画面（R3）。

### 4.2.4 随机化协议（P3 的实现载体 · 防搬运）【一期 standard 档 / 二期 aggressive 档】

#### 4.2.4.1 两档策略（原文 §4.2 只要求"随机抽取"，工程上给两档）

| 档位 | 启用维度 | 适用 | 配置 |
| --- | --- | --- | --- |
| **`standard`（默认）** | ①素材随机 ②入点随机 ③转场随机 ④编码抖动 | **对齐原文 §4.2** | `config/randomization.yaml: mode: standard` |
| `aggressive` | 九维全开（+ 几何 / 色彩 / 纹理 / 音频偏移） | 平台判定搬运压力大时手动开启 | `mode: aggressive` |

#### 4.2.4.2 设计原则（五条）

| # | 原则 | 理由 |
| --- | --- | --- |
| 1 | **决策在编译前，结果落盘** | 保证任何随机组合走同一编译路径，正确性由 golden 统一覆盖 |
| 2 | **种子 = `f(task_id, template_version, attempt_no)`** | 可复现、可审计、可"再换一版"（`attempt_no + 1`） |
| 3 | **随机不得改变信息层** | 字幕/人声/关键文字的位置与内容**永不随机**（可读性优先） |
| 4 | **约束优先于随机** | 素材重复、时长匹配、安全区、授权状态等约束先满足，再在其中随机 |
| 5 | **可审计** | 必须能回答"这条 clip 为什么被选中、用了哪一段、做了什么变换" |

#### 4.2.4.3 `RandomizationPlan`（`render/randomizer.py`）

```python
class RandomizationPlan(BaseModel):
    """一次渲染的全部随机决策快照。写入 manifest.json，永久留痕。"""

    seed: int
    attempt_no: int = 1
    created_at: str
    mode: Literal["standard", "aggressive"] = "standard"
    broll_picks: list[BrollPick]  # 每个场景一条
    bgm_pick: BgmSlot
    cut_pattern: list[int]  # 每段时长 ms
    transition_plan: list[str]  # 每个场景出口转场类型
    transform_plan: dict[int, TransformPlan]  # scene_seq → 变换（standard 档为恒等变换）
    lut_choice: dict[int, str | None]
    sticker_plan: list[StickerPick]
    encoder_jitter: EncoderJitter


class BrollPick(BaseModel):
    scene_seq: int
    clip_id: str
    clip_path: Path
    in_ms: int
    out_ms: int
    score: float  # 加权抽样得分（可解释性）
    rejected: list[RejectionReason]  # 被排除的候选与原因（审计用）


class EncoderJitter(BaseModel):
    cq_delta: int  # NVENC CQ 抖动（±2）
    crf_delta: int  # 成片 CRF 抖动（±1）
    gop: int  # 48–72
    scenecut: int  # 0 或 40
```

#### 4.2.4.4 九维随机化矩阵（每一维都有开关与分布）

| # | 维度 | 随机方式 | 硬约束 | 防搬运收益 | 档位 |
| --- | --- | --- | --- | --- | --- |
| 1 | **素材选择** | 加权无放回抽样（权重 `1/(use_count+1) × 新鲜度`） | 近 K=10 任务未使用（`broll_usage`）；同片内不复用；`usable` 区间 ≥ 所需时长 | 去除"同一素材反复出现"特征 | both |
| 2 | **入点选择** | `in_ms ~ U(usable_from+1500, usable_to-duration-1500)` | 必须避开首尾 1.5s | 去除固定起始帧指纹 | both |
| 3 | **镜头切分节奏** | 每段 `L ~ U(2500, 5500)ms` | 段边界须落在场景内；禁止 <1.5s 碎切 | 打乱固定节奏 | both |
| 4 | **几何变换** | `zoom ~ U(1.02,1.12)`；ROI ±10% 抖动；`mirror ~ Bern(0.35)`；`speed ~ U(0.92,1.10)` | mirror 时画面内不得有文字（`has_text=0`） | 破坏像素级匹配 | aggressive |
| 5 | **色彩变换** | `brightness ±0.03`、`contrast ±0.06`、`saturation ±0.08`、`gamma ±0.04`，LUT 随机 | 与前一场景差异不得为 0 | 破坏直方图指纹 | aggressive |
| 6 | **纹理/噪点** | `noise_strength ∈ {0,4,6}`；`vignette ~ U(0.15,0.35)` | 强度上限硬编码 | 破坏高频特征 | aggressive |
| 7 | **转场** | 模板 `pool` 随机（≥6 种），时长 `U(min,max)` | **禁止连续两次同型**；首个 scene 的 `transition_in` 固定 | 破坏帧序列指纹 | both |
| 8 | **音频** | BGM 随机（近 5 任务不重复）+ 起始偏移 `U(0,15s)` | 人声层不变 | 破坏音频指纹 | both |
| 9 | **编码抖动** | CQ ±2 / CRF ±1 / GOP 48–72 | 不得超出画质下限（CQ ≤ 25） | 破坏编码指纹 | both |

#### 4.2.4.5 相似度审计门禁（`scripts/dup_audit.py` · R3）

| 检查项 | 方法 | 通过阈值 | 不通过处理 |
| --- | --- | --- | --- |
| 素材重叠 | `broll_usage` 查询 | 与最近 10 个成片的 clip 交集 ≤ 30% | 重抽（`attempt_no+1`，≤3 次） |
| 全局感知哈希 | 成片抽 8 帧 pHash，与历史 20 个成片逐帧比较 | Hamming ≤ 8 判"高相似"，高相似帧占比 ≤ 5% | 同上 |
| 局部关键帧 | 同秒关键帧 SSIM | `SSIM > 0.92` 的片段数 = 0 | 同上 |
| 音频指纹 | chromaprint（FFmpeg build 已含） | 与历史成片相似度 ≤ 0.6 | 换 BGM / 换素材 |
| 兜底 | 3 次重抽仍不通过 | — | **接受 + `warn` + WebUI 标记"建议人工复核"（不阻塞生产，P4）** |

### 4.2.5【二期】filtergraph 生成规则（`graph.py` / `filters.py`）

#### 4.2.5.1 标签与节点命名（禁止匿名输出与同名复用）

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 输入 | `[<index>:v]` / `[<index>:a]` | `[0:v]` |
| 源处理链输出 | `[v<scene>_s<slot>]` | `[v1_s0]` |
| 层合成中间态 | `[v<scene>_l<z>]` | `[v1_l40]` |
| 场景输出 | `[v<scene>_out]` | `[v1_out]` |
| 音频 | `[a<role>]` / `[aout]` | `[a_voice]` / `[a_bgm_duck]` |
| 最终 | `[vout]` / `[aout_final]` | — |

#### 4.2.5.2 路径转义（Windows 关键陷阱 · 必须有 golden 覆盖）

```python
def ff_path(p: Path) -> str:
    """把本地路径转为可安全嵌入 filter 参数的字符串。规则（顺序执行）：
    1) 优先使用相对 data/tmp 的相对路径（减少转义面）
    2) 反斜杠 → 正斜杠
    3) 驱动器冒号 → '\\:'     （'D:/x.ass' → 'D\\:/x.ass'）
    4) 单引号 → '\\''（filter 参数内）
    5) 逗号/方括号仅在参数内部出现时保留（label 使用独立引号包裹）
    """
```

| 场景 | 错误写法 | 正确写法 |
| --- | --- | --- |
| `subtitles`/`ass` 滤镜路径 | `ass=D:\data\sub.ass` | `ass='D\:/data/sub.ass'` |
| `fontsdir` | `fontsdir=C:\Windows\Fonts` | `fontsdir='assets/fonts'`（相对路径优先） |
| 含空格路径 | `ass=D:/my data/sub.ass` | `ass='D\:/my data/sub.ass'` |

> 编译期**一律**调用 `ff_path()`，禁止业务代码手拼路径（`tests/golden/filtergraph/*` 覆盖含中文、空格、冒号的路径用例）。

#### 4.2.5.3 场景滤镜图示例（Layer B/C 混合形态）

```
# inputs: 0=broll clip, 1=voice_master.wav, 2=bgm.mp3, 3=overlay png(贴纸)
[0:v]trim=start=14.235:end=21.155,setpts=PTS-STARTPTS,fps=30,
     scale=1210:2150:flags=bicubic,crop=1080:1920:64:114,          # zoom1.12 + ROI 抖动
     eq=brightness=0.021:contrast=1.043:saturation=1.061:gamma=1.017,
     noise=alls=5:allf=t+u,gblur=sigma=0.35,
     vignette=angle=0.28,setsar=1[v1_s0];
[1:a]aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono,
     atrim=start=0:end=6.920,asetpts=PTS-STARTPTS[a1_v];
[2:a]aloop=loop=-1:size=2000000000,volume=-21dB,aformat=sample_fmts=s16:sample_rates=48000[a1_b];
[a1_b][a1_v]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=420[a1_bd];
[a1_v][a1_bd]amix=inputs=2:duration=first:normalize=0[a1_out];
[v1_s0]ass='data/tmp/graphs/scene_001.ass':fontsdir='assets/fonts'[v1_l40];
[v1_l40][3:v]overlay=x=86:y=1432:shortest=0[v1_out]
```

**规则补充**

- 每条链的 `\` 续行仅用于文档可读性，**实际写入文件时单行输出**（FFmpeg 不认续行符）。
- `fps=30` 必须在 `scale` 之前（先统一时基再缩放，避免插值错位）。
- `overlay` 的 `x/y` 必须是**偶数**（yuv420p 色度对齐要求，奇数会导致 1px 偏移与编码警告）。
- `ass` 烧录放在**最后一个视频层之前**（贴纸/水印可在字幕之上），但字幕必须始终位于画面主体之上。
- `shortest=0`：禁止用最短流截断；总时长由 `-t` 显式控制（音画同步军规）。

#### 4.2.5.4 编译期守卫与缓存哈希

```python
def render_hash(
    *,
    template_id: str,
    template_version: int,
    ir: VideoIR,
    encoder_args: list[str],
    input_digests: dict[str, str],
) -> str:
    """sha256(template_id|version|canonical_ir_json|encoder_args|input_sha256…) → 32hex
    - canonical_ir_json：字段排序 + 去除无关字段（created_at 等）后的稳定 JSON
    - input_digests：全部输入文件的 sha256（素材一改，缓存必失效）
    """
```

| 守卫 | 阈值 | 触发动作 |
| --- | --- | --- |
| `estimated_nodes` | > 60 | 拒绝编译 ⇒ 拆两 pass 或降层数（记 `warn`） |
| 单链滤镜数 | > 12 | 自动插入中间 `split`/`nullsink` 分段（记 `warn`） |
| 输出分辨率 | ≠ canvas | 编译期报错（配置错误，不兜底） |
| 时长校验 | `Σ scene_duration ≠ total_ms ± 1ms` | 编译期报错（时间轴与场景不一致是严重 bug） |
| 语法预检 | `ffmpeg -filter_complex_script … -f null -` 非 0 退出 | 快速失败 + 落 `RENDER_FILTER_SYNTAX`，**保留滤镜文件供排障** |

### 4.2.6 字幕契约（`render/subtitle.py` → ASS）【一期可选，默认开启】

> **实施状态（T3.5 · 2026-09-15）**：已落地。下表逐行标注了**与最初设计有出入**的三处，
> 每一处都是照着原方案实现之后、在真机上被证明行不通才改的（理由写在行内）。

| 契约项 | 规则 |
| --- | --- |
| 通道 | ASS（libass），**不用** `drawtext` |
| 时间 | 从 `timeline.json` 取 `start_ms/end_ms`，禁止自行估算；句间不重叠。一期由 `synthesize_script` **逐句 ffprobe** 得到并写进 `timeline.json`；复用母带时优先读 `timeline.json`，读不到就**就地重量**盘上的 `sNNN.wav`，都拿不到 ⇒ **跳过字幕**（不按字数估） |
| 样式 | `Main`（正文）、`SpeakerA`/`SpeakerB`（按说话人换强调色，按**首次出现顺序**分配）、`Title`（标题卡，二期） |
| 断行 | `max_chars_per_line`（默认 13 字）+ `max_lines`（2 行）；优先标点处断行（标点**留在上一行**）；**禁止断在数字/英文单词中间**。★ **容量会放宽**：无标点长句按 13 字排会超过 `max_lines` ⇒ 容量抬到 `ceil(总字数 / max_lines)`，宁可某行多几个字，也不丢台词、不让画面溢出 |
| 卡拉OK | `\k` 按**字均分**落在句时长内（中文等宽）；`word_pop` 模式用 `\t` 做缩放动画（**二期**，一期不实现） |
| 安全区 | `MarginV = max(subtitle.margin_bottom, safe_area.bottom)`；`MarginL/R = safe_area.left/right`（`safe_area` 在 `config/outputs.yaml → subtitle.safe_area`） |
| 字体 | **优先** `templates/<模板>/assets/fonts/`，找不到则退到**系统字体目录**（`%WINDIR%\Fonts`）并记一条 `note` 如实说明"这次不算合规"。★ 两边都没有 ⇒ 抛 `FONT_MISSING`，由 `plan_subtitle` 转成**跳过字幕层** —— 与水印同一条口径：装饰品不该成为整条链路的单点阻塞。原方案是"缺失直接报错拒绝出片"，真机上一试就发现它把"没有字体"升级成了"没有片子" |
| 编码 | UTF-8 **不带 BOM**（BOM 会让某些 libass 判定"这不是 ASS 文件"而静默不显示）；行尾 LF（统一便于 golden 比对） |
| 落盘 | `data/work/<task_id>/final/subtitle.ass`（永久保留，可二次剪辑复用）。原方案写的是 `data/media/<date>/<task_id>/final/`，那是**二期**三层模板的目录；一期没有 `data/media/`，成片在 `data/output/videos/`、可复用资产在 `data/work/` |

**为什么默认字体是「微软雅黑」而不是「Source Han Sans SC」**

原方案要求"字体必须来自 `assets/fonts/`"，而仓库里那个目录**只有一个 `.gitkeep`** ——
真按字面实现，默认配置下每一支片子都出不来。默认值改成 Windows 自带的
`Microsoft YaHei`：本项目本来就是 Windows 专用（TTS 走 SAPI、杀进程走 `taskkill`、
路径按盘符写），挑一个本机一定有的字体比引用一个"应该存在"的开源字体名更稳。
要把字体换成自己的：把 `.ttf/.otf` 放进 `templates/<模板>/assets/fonts/`，
再把 `subtitle.font_name` 改成该字体的**家族名**（不是文件名 —— `msyh.ttc` 的家族名
是 `Microsoft YaHei`，文件名里一个字母都对不上）。

### 4.2.7 时间轴契约 `timeline.json`（**二期：成片时长基准** · 一期：句级时间轴）

> ⚠️ **一期（C12/C13）**：`timeline.json` 仍由 T2.7 产出（供**字幕计时**与诊断），但**不再是成片时长基准** —— 一期成片时长 = `ffprobe(voice_master.wav)` + `tail_ms`（§4.2.8.5）。下方 `scenes[]` 字段与第 4 步的 `duration_policy` 校验属**二期**。

```json
{
  "schema_version": "1.0",
  "task_id": "01J9Z3K7...",
  "sample_rate": 48000,
  "channels": 1,
  "total_ms": 61240,
  "voice_master": "media/20260913/01J9Z3K7.../tts/voice_master.wav",
  "sentences": [
    {"id": "…s001", "seq": 1, "speaker": "bigbear", "start_ms": 0, "end_ms": 3200,
     "duration_ms": 3200, "pause_after_ms": 420, "audio": "tts/s001.wav"},
    {"id": "…s002", "seq": 2, "speaker": "littlebear", "start_ms": 3620, "end_ms": 6420,
     "duration_ms": 2800, "pause_after_ms": 200, "audio": "tts/s002.wav"}
  ],
  "scenes": [{"seq": 1, "scene_id": "scene_hook", "start_ms": 0, "end_ms": 6920,
              "sentence_ids": ["…s001", "…s002"]}],
  "loudness": {"measured_i": -18.4, "measured_tp": -2.1, "measured_lra": 6.2, "target_i": -16.0}
}
```

**生成算法（顺序不可颠倒）**

1. 逐句 `ffprobe -show_entries format=duration` 取**实测**毫秒（**不信任**引擎返回的估算）。
2. `duration_ms += pause_after_ms`（`pause = base + jitter(seed)`，jitter 由随机化计划给出，±80ms）。
3. 累加得每句 `start_ms/end_ms`；末尾追加 `tail_ms`。
4. （**二期**）按 `sentence_window` 汇总场景区间，校验模板 `duration_policy`（`min_ms/max_ms`）。
5. 回写 `script_sentences.start_ms/end_ms`（**事务内批量更新**）。
6. 写 `timeline.json` + `artifacts(kind='timeline')`。

**音频链（三段式，固定形态）**

```
① 句子拼接（不落中间文件的 filter 方案）
   [1:a][2:a]...[N:a]concat=n=N:v=0:a=1[a_voice]
   —— 每个输入先 aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono
   —— 片段已自带尾部静音（由 pause_after_ms 决定），故无需 adelay
② BGM 侧链避让（ducking）
   [N+1:a]aloop=loop=-1:size=2000000000,volume=-21dB,aformat=…[a_bgm]
   [a_bgm][a_voice]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=420[a_bgm_duck]
③ 混音 + 响度归一（两遍法）
   [a_voice][a_bgm_duck]amix=inputs=2:duration=first:normalize=0[a_mix]
   [a_mix]loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:
          measured_I=…:measured_TP=…:measured_LRA=…:measured_thresh=…[aout]
   [aout]aresample=48000,alimiter=limit=0.95[aout_final]
```

- `amix` **必须** `normalize=0`（默认会把人声衰减到 −6dB，是"人声偏小"的常见根因）。
- `loudnorm` 第一遍仅测量（`print_format=json`，不入图），第二遍带 `measured_*`（线性模式，避免动态压缩失真）。
- 触发 `aresample=async=1` 兜底 ⇒ 记 `warn`（说明上游时间戳有问题，需排查）。

**落地说明（T2.7 · 2026-09-16）**

| # | 规格写法 | 落地写法 | 为什么 |
| --- | --- | --- | --- |
| 1 | 逐句 `ffprobe` 取**实测**毫秒 | 量的对象是**盘上那一份** WAV，不是 `script_sentences.tts_duration_ms` | 那一列记的是"合成那一刻量到的"；文件可能被换过、被 §03.7.5 的 GC 删过。两者不一致时以盘为准 —— 不一致本身就是该被看见的信号 |
| 2 | `pause = base + jitter(seed)`，±80ms | `blake2s(f"{seed}:pause:{seq}") % 161 − 80`；**`base = 0` 时短路成 0** | 抖动必须可复现（重跑得到逐毫秒一致的时间轴，手法同 `db/lease.py::jitter_ms`）；而"紧接下一句"不许被抖出静音（陷阱 #109） |
| 3 | jitter 由随机化计划给出 | 种子取 `tasks.payload_json.seed`，缺省用 `task_id` | `RandomizationPlan`（§4.2.4.3）是二期。一期没给种子时拿 ULID 顶上 —— 至少保证"这一类任务不共享同一条节奏" |
| 4 | 累加 `start_ms/end_ms`，末尾追加 `tail_ms` | `total_ms` **含** tail；`voice_master.wav` **不含**（只装句子 + 停顿） | §4.2.8.5 的成片时长 = `ffprobe(voice_master) + tail_ms`。tail 塞进母带就会两处各加一次 ⇒ 成片长出一个 tail（陷阱 #110 / 裁定 217） |
| 5 | ① 句子拼接用 `concat` filter | 每句先 `aformat=s16:48k:mono,asetpts=PTS-STARTPTS,apad=pad_dur=<停顿>`，再 `concat=n=N:v=0:a=1` | 停顿必须**真的进音频**（只记在时间轴上 ⇒ 第 N 句比字幕早到 `pause_after_ms`）。`apad` 一个参数顶掉"每个停顿塞一个 `anullsrc` 输入"；单句脚本不走 `concat`（`n=1` 无意义） |
| 6 | 回写 `start_ms/end_ms`（事务内批量更新） | `SentenceRepo.set_timeline(spans)`：一个事务、**逐行带 `version` 守卫**，返回**真正写进去的行数** | 回写成立的前提是"这一句的音频没换过"。跳过被改的那一行会留下**自相矛盾**的时间轴（第 3 句新时长、第 4 句起按旧时长）⇒ 调用方拿"行数差"判"整条作废"（裁定 220） |
| 7 | 写 `timeline.json` + `artifacts(kind='timeline')` | 相对 `data/` 的 posix 路径（`core.paths.data_relative`）+ `.partial` 原子替换；顺手登记 `kind='voice_master'` | 清单要能跟着 `data/` 搬家；母带是这条片子的另一件产物，`kind` 词表里本来就有 |
| 8 | （规格示例里有）`scenes[]` / `loudness` | **不写** | 场景是二期；响度要到混音（T3.6）之后才有实测值。凭空写一个空数组只会让下游以为"场景算出来是空的" |

- `sentences[].text` 取 `subtitle or text`：字幕那一层要的是"可含手工断行"的文本（§03.3.7）。
- 母带实测时长与 `total_ms − tail_ms` 的偏差 **≤30ms**（§04.2.7 验收线）；超线只 `warn` 不阻断 —— 逐句 ffprobe 各自四舍五入，60 句最坏叠 30ms（与 C12"诊断不阻断"同一条口径）。
- **任一句重合成 ⇒ 时间轴全量重算**（陷阱 #26）：验收由"改一句 ⇒ 后面每句 `start_ms` 都跟着挪"钉住。
---

### 4.2.8【一期 P0 · 核心】单遍合成契约（口述 D5 · 裁决 C13）

> **这就是口述里的"剪辑只需要音画合成 + 固定水印"。** 一次 ffmpeg 调用直出 `final.mp4`，无中间产物。

#### 4.2.8.1 输入 / 输出

```
输入（全部本地文件）：
  ① 跑酷素材：data/assets/mc_parkour/parkour_*.mp4   ← 用户提供（D3）
  ② 人声：    data/media/<date>/<task_id>/tts/voice_master.wav   （句子拼接后的整条人声）
  ③ BGM：     data/assets/bgm/bgm_*.mp3              （可选，Q12 默认开启）
  ④ 水印：    templates/<tid>/assets/images/watermark.png        （固定水印，D5 必做）
  ⑤ 字幕：    data/media/<date>/<task_id>/final/subtitle.ass     （可选，Q11 默认开启）

输出：
  data/output/videos/<ts>_<task_id>_final.mp4     （1080×1920 / 30fps / H.264 + AAC）
```

#### 4.2.8.2 `CompositePlan`（一期的"IR"）

```python
class WatermarkSpec(BaseModel):
    """★ 固定水印（口述 D5 必做）。'固定' = 位置与尺寸恒定，不随机、不随时间变化。"""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    image_path: Path  # templates/<tid>/assets/images/watermark.png
    position: Literal["top_left", "top_right", "bottom_left", "bottom_right", "center"] = "bottom_right"
    margin_px: tuple[int, int] = (40, 40)  # (x, y) 距边距（偶数，yuv420p 对齐要求）
    width_px: int = 220  # 目标宽（保持宽高比；禁止超过画布 1/4）
    opacity: float = Field(0.85, ge=0.0, le=1.0)
    apply_to: Literal["full", "tail"] = "full"


class CompositePlan(BaseModel):
    """一期渲染的全部决策快照。写入 manifest.json，永久留痕（P5）。"""

    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    mode: Literal["composite"] = "composite"  # ★ 与二期的 'scene_orchestration' 区分

    # 画布与时长
    canvas: CanvasSpec  # w=1080 h=1920 fps=30 sar=1:1 colorspace=bt709
    total_ms: int  # ★ = voice_master 实测时长（ffprobe，唯一基准）
    tail_ms: int = 500  # 尾部留白（避免最后一字被切）

    # 背景（跑酷）
    bg_clips: list[BrollPick]  # 随机抽取的片段序列（含 in/out 点）
    bg_fill: Literal["loop", "stretch", "black"] = "loop"
    bg_fit: FitMode = FitMode.COVER  # 缩放铺满（不变形）

    # 音频
    voice_path: Path
    bgm_path: Path | None = None
    bgm_gain_db: float = -21.0
    ducking: DuckingSpec | None = None  # BGM 侧链避让（有 BGM 时启用）
    loudness: LoudnessSpec  # I=-16.0 TP=-1.5 LRA=11 两遍法

    # 叠加层
    watermark: WatermarkSpec  # ★ 固定水印
    subtitle_ass: Path | None = None  # 可选（Q11）

    # 随机化留痕（P3，一期仍生效）
    randomization: RandomizationPlan
    seed: int
    output_profile: str = "default"
```

#### 4.2.8.3 编译产物：单条 filter_complex

```
# 输入分配（index 由编译器分配，禁止硬编码）
#   0..k-1  : 跑酷片段（各自 -ss/-t 或 trim）
#   k       : voice_master.wav
#   k+1     : bgm.mp3（可选）
#   k+2     : watermark.png（-loop 1 -i，或用 -i 配合 overlay 的 shortest=0）
#   k+3     : subtitle.ass（可选，由 ass 滤镜直接读文件，不需要 input）

# ① 背景：逐段 trim(随机入点) → 缩放铺满 → 微变换 → concat → 循环补齐到 total_ms
[0:v]trim=start=12.480:end=27.930,setpts=PTS-STARTPTS,fps=30,
     scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,
     eq=brightness=0.012:contrast=1.021,setsar=1[v0];
[1:v]trim=start=5.120:end=22.400,setpts=PTS-STARTPTS,fps=30,
     scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,
     eq=brightness=-0.008:contrast=1.014,setsar=1[v1];
[v0][v1]concat=n=2:v=1:a=0[vbg_raw];
[vbg_raw]loop=loop=-1:size=32767:start=0:time=0,trim=duration=180.500,
         setpts=PTS-STARTPTS[vbg];                       # 循环补齐到 total_ms

# ② 字幕（可选，Q11）
#    落盘位是 data/work/<task_id>/final/subtitle.ass（见 §4.2.6 的落盘一行）
#    路径必须整体加单引号 + 盘符冒号转义：滤镜参数解析器会把 D: 的冒号当选项分隔符，
#    报出来的错是 "Option not found"，指不回"路径写错了"。
[vbg]ass='D\:/repo/data/work/<task_id>/final/subtitle.ass':fontsdir='D\:/repo/templates/douyin_9x16_default/assets/fonts'[vsub];
# 无字幕时：这一层整个不拼，视频标签直接沿用 [vbg]

# ③ 固定水印（D5 必做）—— overlay x/y 必须为偶数
[k+2:v]scale=220:-2,format=rgba,colorchannelmixer=aa=0.85[wm];
[vsub][wm]overlay=x=W-w-40:y=H-h-40:shortest=0:eval=init[vout];

# ④ 音频：人声（+BGM 侧链避让）→ 混音 → 两遍 loudnorm → 限幅
#    ★ 实测修正（T3.6）：原模板有五处照抄会出问题，逐条见下方"音频链的五个坑"
[k:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,
     volume=<voice_gain_db>dB[a_voice];
[a_voice]asplit=2[a_voice_sc][a_voice_mix];   # ★ 人声必须分两路（见坑 2）
#    BGM 的循环放在**输入侧**（-stream_loop -1 -i bgm），不用 aloop（见坑 3）
[k+1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,
       volume=-21dB[a_bgm];
[a_bgm][a_voice_sc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=420:makeup=1[a_bgm_duck];
[a_voice_mix][a_bgm_duck]amix=inputs=2:duration=first:normalize=0[a_mix];
[a_mix]loudnorm=I=-16:TP=-1:LRA=11:linear=true:
       measured_I=…:measured_TP=…:measured_LRA=…:measured_thresh=…:offset=…[a_norm];
#    limit 由 true_peak_dbtp − 0.3dB 推出（见坑 4），level=0 关掉自动电平（见坑 1）
[a_norm]aresample=48000,alimiter=limit=0.8610:level=0[aout]

# 无 BGM 时（可选输入缺失 ⇒ 单轨人声，**不报错**）：
# [k:a]aformat=…,volume=…dB,anull[a_mix];  （后面照常 loudnorm + alimiter）

# 输出参数（§01.5.5 profile）
-map "[vout]" -map "[aout_final]"
-c:v libx264 -crf 21 -preset medium -pix_fmt yuv420p -profile:v high -level 4.1
-r 30 -g 60 -colorspace bt709 -color_primaries bt709 -color_trc bt709
-c:a aac -b:a 192k -ar 48000 -ac 2
-t 180.500                    # ★ 显式总时长（禁 -shortest，见军规）
-movflags +faststart
```

**音频链的五个坑**（T3.6 实测；每一条都是"写错了 ffmpeg 也不报错，只有耳朵听得出来"）

| # | 症状 | 根因 | 正确写法 |
| --- | --- | --- | --- |
| 1 | 响度比目标高 ~0.4 LU，发布门禁（`[-16.5,-15.5]`）顶出去 | `alimiter` 的 `level` **默认开启**，语义是"把输出抬到 0dBFS"，等于在限幅器后又加了一级补偿增益，把 loudnorm 的归一化抵消掉 | `alimiter=limit=…:level=0` |
| 2 | `Stream specifier 'a_voice' in filtergraph ... matches no streams` | 人声要同时喂给侧链与 `amix`，而 ffmpeg 的滤镜图里**一个标签只能被消费一次** | 先 `[a_voice]asplit=2[a_voice_sc][a_voice_mix]`，两处各取一支 |
| 3 | 短 BGM 配长口播时 ffmpeg 申请几 GB 内存甚至 OOM | `aloop` 的 `size` 是**采样缓冲的样本数**，模板里的 `2000000000` 就是几 GB | 循环放在输入侧：`-stream_loop -1 -i bgm`，由 demuxer 做，零缓冲 |
| 4 | 解码后的真峰值超 `true_peak_max_dbtp`（实测 −0.85 dBTP > −1.0） | 限幅器压的是**编码前**的样本峰值，而门禁量的是**解码后**的真峰值 —— AAC 重建会过冲零点几 dB；模板里那个 `0.95`（−0.45 dBFS）本身也比门禁宽半 dB | `limit = 10 ** ((true_peak_dbtp − 0.3) / 20)`，即默认 `0.8610` |
| 5 | **BGM 整条消失、人声被叠了一份**（混音真峰值实测 **+1.91 dBTP**，成品响度掉到 −16.48，离门禁下界 `−16.5` 只剩 0.02 dB） | `sidechaincompress` 的输入焊盘是 **`#0: main` / `#1: sidechain`**（`ffmpeg -h filter=sidechaincompress`），而模板写的是 `[a_voice_sc][a_bgm]` —— 被压的是**人声**，`a_bgm_duck` 这个标签名是假的。BGM 没进 `amix`，人声进了两次 | `[a_bgm][a_voice_sc]sidechaincompress=…`（**主路在前**）。ffmpeg 对此**一个字都不说**，只有量电平才看得出来 |

第 5 条值得单独记一笔，因为**本文档的两处模板互相矛盾**：§4.2.8.3 上半段的简图（② 那一段）
写的是正确的 `[a_bgm][a_voice]`，下半段的完整模板却写成了 `[a_voice_sc][a_bgm]`，而实现抄的是
后者。教训不是"抄错了"，是**同一份契约里同一个滤镜出现两种写法时，必须先确定哪一份是真相**
—— 判据是 `ffmpeg -h filter=<名字>` 的输入焊盘顺序，不是哪一段写得更详细。

#### 4.2.8.4 与二期共用的规则（一期同样适用，不得省略）

| 规则 | 说明 |
| --- | --- |
| `ff_path()` 转义 | 所有滤镜内路径统一转义（§4.2.5.2），Windows 冒号陷阱 |
| `-filter_complex_script` 文件 | 不用命令行传滤镜图（Windows 32767 上限 + 转义灾难） |
| **显式 `-t`，禁用 `-shortest`** | 视频流短于音频会导致提前截断/冻结；`-t` 是唯一可靠的总时长控制 |
| `fps=30` 在 `scale` 之前 | 先统一时基再缩放，避免插值错位 |
| `overlay` x/y 为偶数 | yuv420p 色度对齐要求 |
| `amix` 必须 `normalize=0` | 否则人声被衰减 6dB（陷阱 #4） |
| `.partial` → 原子改名 | 崩溃不留"假完成"产物（陷阱 #9） |
| 语法预检 | `ffmpeg -filter_complex_script … -f null -` 先跑一遍（陷阱 #7） |
| 节点守卫 | `estimated_nodes > 60` ⇒ 拒绝并改用分块合成（见 §4.2.8.6） |

#### 4.2.8.5 时长契约（一期核心，替代 §4.2.7 的句子级时间轴）

```
total_ms = ffprobe(voice_master.wav).duration_ms + tail_ms        ← 唯一基准
video_duration = total_ms                                          ← 由 loop + trim 补齐
audio_duration = total_ms                                          ← 由 -t 强制
```

**四条不变量**

1. **人声决定成片时长**：`total_ms` 只来自 `ffprobe` 实测，**不信任** TTS 引擎返回的估算。
2. **视频必须 ≥ 音频**：跑酷素材总长不足 ⇒ `loop` 补齐；素材为空 ⇒ `bg_fill='black'`（纯黑底 + 水印，仍出片）。
3. **尾部留白**：`tail_ms`（默认 500ms）保证最后一字不被硬切。
4. **不做同步审计**：`av_sync` 仅作**诊断指标**写入 `quality_json`，**默认不阻断**出片（C12）。

#### 4.2.8.6 降级与分块（当单遍不可行时）

| 触发 | 降级动作 | 留痕 |
| --- | --- | --- |
| 跑酷素材为空 / 全部 `enabled=0` | `bg_fill='black'`（纯黑底）+ 水印照常 | `quality.degrade_reason='no_broll_assets'` |
| BGM 为空 | 静音 BGM 轨（不报错，直接单轨人声） | `warn`（不标记 degraded） |
| 水印 PNG 缺失 / 不可用 / 放不下 | **跳过水印层，照常出片**（装饰缺失不阻塞成片） | `watermark.skipped_reason` 写进 `manifest.json` |
| 素材片段过多（`estimated_nodes > 60`）或单遍超时 | **分块合成**：每块 ≤90s 出一段 `part_00N.mp4`，再用 `concat` demuxer 拼接 | `warn` + `quality.degrade_reason='composite_chunked'` |
| 编码失败（NVENC 不支持） | 回退 `libx264` | `warn` |
| 分块后仍失败 | 720P 保底档（`fallback_720p` profile） | `quality.degrade_reason='render_720p'` |
| 仍失败 | 任务 `failed` → 重试 ≤3 → `manual_pool` | 标准重试链 |

#### 4.2.8.7 缓存哈希（一期）

```python
def composite_hash(*, plan: CompositePlan, encoder_args: list[str], input_digests: dict[str, str]) -> str:
    """sha256(canonical_plan_json|encoder_args|input_sha256…) → 32hex
    - canonical_plan_json：字段排序 + 去掉 created_at 等易变字段
    - input_digests：voice_master / 跑酷片段 / 水印 / BGM 的 sha256
    - 人声或素材任一变化 ⇒ 缓存失效 ⇒ 重渲
    一期无场景级缓存，仅整片级：命中则直接复用 final.mp4（重跑任务时省时）
    """
```

**实施状态（2026-09-17 · `render/cache.py` + `services/render_service.py`）**

命中判据比上面这段更严：**上一轮 `manifest.json` 记的 `composite_hash` 与这一次算出来的完全一致**，
且那支 `final.mp4` 还在盘上、**字节数与 manifest 记的一致**，且重建 `CompositeResult` 要用的字段
一个不少（缺一个就重渲，不猜默认值）。manifest 是**最后**才写的（成片原子改名 → 量响度 → 写
manifest），所以「manifest 在」本身就意味着那一轮跑到底了 —— 这正是不做「文件在就跳过」那种
短路（`pools/render_worker.py` 纪律 2）的原因。

复用是**任务级**的：直接用盘上那一支（不建 `cache/render/<hash>.mp4`，否则同一支片子存两遍、
还要再写一套 GC）。命中时 `manifest.json` 记 `reused=true` + `rendered_at`（比 `created_at` 早），
`ProduceResult.reused` 与进度文案一起告诉面板「这次没渲」。**命中面**：底片是随机挑的，所以不带
`--seed` 的重跑通常挑到另一条底片、哈希不同、照常重渲（设计如此，不是缓存失效）；真正会命中的
是带 `seed` 的重跑 / 复现，以及队列把同一条 `render/final` 重投。

---
## 4.3 CosyVoice 适配层契约（`tts/base.py`）

### 4.3.1 音色档案（原文 §3.1）

```python
class VoiceProfileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice_id: Literal["bigbear", "littlebear"] | str  # ★ 原文 §3.1：bigbear / littlebear
    display_name: str  # '熊大' / '熊二'（**仅展示**，与 ID 解耦 ⇒ R2 合规）
    ref_wavs: list[Path] = Field(min_length=2, max_length=3)  # 原文：2–3 段
    ref_texts: list[str]  # 每段参考音频的逐字文本（必须完全一致）
    language: Literal["zh"] = "zh"
    notes: str | None = None


class VoiceProfile(VoiceProfileSpec):
    created_at: str
    ref_duration_ms_each: list[int]  # 每段时长，校验 10–30s（原文 §3.1）
    ref_quality: RefQualityReport  # 质量校验报告
    engine: str
    engine_revision: str


class RefQualityReport(BaseModel):
    segments_ok: bool
    duration_ok: bool
    no_clip_ok: bool
    speech_ratio_each: list[float]  # 有效语音占比
    has_bgm_suspect: bool  # 疑似有背景音乐（启发式）
    single_speaker_ok: bool | None  # 说话人一致性（引擎能提供时）
    warnings: list[str] = Field(default_factory=list)
```

**参考音频入库校验（`scripts/ingest_voice_src.py` · 原文"干净"要求的机器化）**

| 校验项 | 阈值 | 不通过 |
| --- | --- | --- |
| 段数 | 2–3 段 | **拒绝入库** |
| 单段时长 | 10–30 秒 | **拒绝入库** |
| 无削波 | 峰值 ≤ −1.0 dBFS | **拒绝入库** |
| 采样率 | ≥ 16 kHz | **拒绝入库** |
| 无背景音乐 | 谱平坦度 + 静音段占比（启发式） | `warn`，需人工确认 |
| 单发言人 | 说话人嵌入一致性（引擎提供时）或人工标记 | `warn` |
| 有效语音占比 | ≥ 70%（非静音） | `warn` |

**目录约定（原文 §3.1）**

```
data/voice_src/bigbear/{ref_01.wav, ref_02.wav, ref_03.wav, ref.txt, profile.json}
data/voice_src/littlebear/{ref_01.wav, ref_02.wav, ref_03.wav, ref.txt, profile.json}
    ref.txt：与 wav 一一对应的逐字文本（每行一段，顺序与文件名一致）
    profile.json：来源登记（URL/录制日期/授权说明）⇒ R2 合规留档
```

### 4.3.2 零样本复刻调用（原文 §3.2）

```python
# 引擎内部调用形态（原文 §3.2 明确写出）：
#   inference_zero_shot(目标文本, 参考文本, 参考wav) → 用原声音色说任意新台词
#
# VoiceEngine.synthesize(TTSRequest) 的实现要点：
#   - 参考音：从已注册 VoiceProfile 取 ref_wavs（多段时按 seed 随机选一段 ⇒ 可复现）
#   - 目标文本：TTSRequest.text（已归一化，≤28 字）
#   - 全程本地推理（P1），数据不出机器
```

```python
class TTSRequest(BaseModel):
    """★ 按句合成：一次调用 = 一句（断点续传的最小原子）。"""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=28)  # 超长必须先经 segmenter（R7）
    voice_id: str
    emotion: str = "neutral"  # neutral/开心/疑惑/强调/无奈…
    speed: float = Field(1.0, ge=0.5, le=2.0)
    seed: int | None = None
    sample_rate: int = 48_000
    output_format: Literal["wav", "flac"] = "wav"
    normalize_text: bool = True
    cache_key: str | None = None  # tts_hash；命中则直接复用
    out_path: Path  # 由调用方指定（决定断点续传落点）
    timeout_sec: float = 60.0


class TTSResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    audio_path: Path | None = None
    duration_ms: int | None = None  # ★ 由 ffprobe 实测（时间轴唯一来源）
    sample_rate: int | None = None
    channels: int | None = None
    cache_hit: bool = False
    engine: str
    engine_revision: str
    seed_used: int | None = None
    rtf: float | None = None  # 实时率（性能回归指标）
    peak_dbfs: float | None = None  # 爆音检测（> -0.5 dBFS 触发复核）
    rms_dbfs: float | None = None  # 静音检测（< -50 dBFS 触发复核）
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class VoiceEngine(ABC):
    """实现可替换：CosyVoice 服务 / 备用引擎 / Mock（测试用）。"""

    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def warmup(self) -> None: ...  # 预跑一段，避免首句抖动
    @abstractmethod
    async def unload(self) -> None: ...  # 释放显存（空闲策略，R4）
    @abstractmethod
    async def health(self) -> EngineHealth: ...

    # —— 音色（零样本复刻）——
    @abstractmethod
    async def register_voice(self, spec: VoiceProfileSpec) -> VoiceProfile: ...
    @abstractmethod
    async def list_voices(self) -> list[VoiceProfile]: ...
    async def unregister_voice(self, voice_id: str) -> None: ...  # 可选实现

    # —— 合成（核心原语：一次一句）——
    @abstractmethod
    async def synthesize(self, req: TTSRequest) -> TTSResult: ...

    async def synthesize_batch(self, reqs: Sequence[TTSRequest]) -> list[TTSResult]:
        """默认串行实现；引擎可用批处理优化，但必须保证逐条结果可独立失败。"""
        return [await self.synthesize(r) for r in reqs]
```

### 4.3.3 按句保存与降级策略（原文 §3.3 / §3.4）

```python
class DegradeAction(StrEnum):
    RETRY_SAME = "retry_same"  # 原样重试（瞬时故障）
    REWARM_ENGINE = "rewarm_engine"  # 释放显存/重载模型后重试
    RETRY_SIMPLIFIED = "retry_simplified"  # 去 emotion、speed=1.0、换 seed
    SPLIT_AND_MERGE = "split_and_merge"  # 再切分后分段合成并无缝拼接
    SWITCH_ENGINE = "switch_engine"  # 切备用引擎（本地 CPU / 备用模型）
    PLACEHOLDER = "placeholder"  # 等长静音占位 + 保留字幕（"字幕模式"）
    FAIL_TASK = "fail_task"  # 放弃并置任务失败（携带 retry_from）


class FallbackPolicy(Protocol):
    def decide(
        self, *, req: TTSRequest, err: TTSError, attempt: int, health: EngineHealth, consecutive_failures: int
    ) -> DegradeAction: ...
```

| 失败类型 | `error_code` | attempt | 决策 | 说明 |
| --- | --- | --- | --- | --- |
| 显存不足 | `TTS_OOM` | 1 | `REWARM_ENGINE` | 卸载后以 fp16/更短文本重载（R4） |
| 显存不足 | `TTS_OOM` | ≥2 | `SWITCH_ENGINE` | 切 CPU 备用引擎（仅保证成片可用） |
| 超时 | `TTS_TIMEOUT` | 任意 | `SPLIT_AND_MERGE` | 长句是超时主因 |
| 输出静音/爆音 | `TTS_SILENT` / `TTS_CLIP` | 任意 | `RETRY_SIMPLIFIED` | 判据：`RMS < -50 dBFS` 或 `peak > -0.5 dBFS` |
| 文本非法/超长 | `TTS_TEXT_INVALID` | 任意 | `SPLIT_AND_MERGE` → `PLACEHOLDER` | 二次失败即占位 |
| 引擎进程崩溃 | `TTS_ENGINE_DOWN` | ≥1 | `REWARM_ENGINE` | supervisor 重启服务后重试 |
| 连续失败 | — | ≥3 句 | **熔断** | 池暂停 5min + `system.alert(TTS_CIRCUIT_OPEN)` |
| 音色缺失 | `TTS_VOICE_MISSING` | 任意 | `FAIL_TASK` | 配置错误（修配置不算人工介入流程） |

> **熔断后行为**：任务**不失败**，改为"字幕模式"继续产出（P4 无人值守优先），`tasks.quality_json` 打 `degraded: true`、`degrade_reason: 'tts_unavailable'`，WebUI 显著提示（原文 §3.4）。

**按句保存的三条不变量**（与 §03.3.7 续传语义一致）

1. 产物路径 `data/output/voice/<task_id>/s<seq:03d>.wav` —— **文件名由 seq 决定，内容由 `tts_hash` 决定**。
2. `tts_status ∈ {pending, failed}` 是唯一"待办"判据；`done` 且 `tts_hash` 命中缓存 ⇒ **不调用引擎**。
3. 单句 `tts_attempts ≥ 3` ⇒ 触发降级链，最终可落 `skipped`（等长静音 + 字幕保留）。

### 4.3.4 "边配音边渲染"的处理（冲突 C7 裁决）

| 项 | 结论 |
| --- | --- |
| 默认 | **关闭**（`pipeline.streaming_render=false`）—— 配音全部完成 ⇒ ffprobe 实测 `voice_master.wav` 总时长 ⇒ 才允许渲染 |
| 理由 | 边配音边渲染会使**成片时长基准**（人声实测总长）随未完成句子漂移 —— C12 后不再要求句子级同步，但**时长基准必须唯一且稳定** |
| 保留能力 | 作为**可选开关**：开启时按"已完成句子"切分，尾部场景进入"待定区"，最后一句完成后**重渲封口** |
| 前置条件 | 开启该开关必须同时满足：①`pipeline.finalize_after_voice=true` ②`av_sync_audit` 诊断数值不恶化 ③WebUI 明示"可能产生重渲开销" |
| 原文兼容 | 原文 §3.3 要求"渲染可逐句消费" ⇒ 该能力**保留但非默认**，不视为需求缺失 |

**落地说明（T2.8 · 2026-09-16）**

| 项 | 落地 | 备注 |
| --- | --- | --- |
| `pipeline.streaming_render` | `config/app.yaml` 里**真有这个键**（默认 `false`，`core/config.py::PipelineConfig`）；`true` ⇒ `studio pipeline run` **立刻抛 `CONFIG_INVALID`**（fail fast，任务状态一步不动） | 上面那条"保留能力"在一期**没有实现**：与其静默按关闭处理（用户以为开了），不如响亮地拒绝。"按已完成句子切分 + 最后一句完成后重渲封口"要等三个前置条件一起落地 |
| 配音阶段的状态边 | `queued_voice --①投递--> voicing --②排空+收口--> queued_render`（§03.4.5）。`voicing` 是**真的停得住的落点**（作业派出去了、等池干完），`rendering` 不是 | 拆分理由见 `services/pipeline_service.py` 的模块说明：一步做完的话"作业派出去了"就无处可停，`--until voicing` 只能是摆设 |
| §4.3.3 熔断后行为 | 全句降级 ⇒ `quality_json.degrade_reason='tts_unavailable'`；**部分**降级**不写**这个字段（实情留在逐句 `tts_status` 与 `jobs.result_json` 里） | `degrade_reason` 只有一个槽位：部分降级塞进去会把"整条片子进了字幕模式"这句更重的话挤掉 |
| 降级演练开关 | 环境变量 **`STUDIO_FAULT`**（本契约原文没有这个键，T2.8 新增）：`tts_down=1` / `tts_fail_sentence=<seq>` / `tts_fail_times=<n>`，分隔符 `;` 与 `,` 等价；**认不出的键一律抛 `CONFIG_INVALID`** | 只在**引擎缝**上注入 ⇒ 池那侧走的是"失败 ⇒ 记账 ⇒ 退避重试 ⇒ 到线降级"那条**真**路径。包装后的引擎名**每场演练都不同**：固定后缀会让第二场命中第一场的缓存、引擎一次都不被调到（陷阱 #111） |
| 排空认领的范围 | 就地借的 worker 按**池**认领（`JobStore.claim` 没有任务过滤）⇒ 会顺手把别的卡在 `voicing` 的任务也念了 | 已知取舍（`todolist.md` 裁定 233）：与常驻池语义一致（谁抢到谁干）。按任务隔离要给 `JobStore.claim` 加过滤，那是四个池共用的契约 |

### 4.3.5 常驻推理服务 HTTP 契约（`tts/server.py`）

| 方法 | 路径 | 请求 | 响应 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/health` | — | `EngineHealth` | 供 supervisor 与 `doctor` 探活 |
| POST | `/warmup` | `{}` | `{ready: true, load_ms: int}` | 启动后预热，避免首句 20s+ 延迟 |
| POST | `/unload` | `{}` | `{unloaded: true, freed_mb: int}` | 空闲策略调用（R4 显存释放） |
| GET | `/voices` | — | `list[VoiceProfile]` | 音色档案列表 |
| POST | `/voices` | `VoiceProfileSpec`（multipart：`ref_wav` + 字段） | `VoiceProfile` | 零样本复刻注册 |
| DELETE | `/voices/{voice_id}` | — | `204` | 注销 |
| POST | `/synth` | `TTSRequest`（`out_path` 由服务端在 `data/media/.../tts/` 内解析，**拒绝任意路径**） | `TTSResult` | 按句合成 |

**服务端约束**

- **单并发**（信号量 = 1，GPU 串行）；请求队列长度 > 8 ⇒ 返回 `429`（由 voice worker 退避重试）。
- `out_path` 必须落在 `STUDIO_DATA_DIR` 下（路径穿越防护，`core/paths.py` 统一校验）。
- fp16 常驻、**禁 bf16**（Turing 架构）、空闲 20min 自动 `unload`。
- 并发值由 T2.2 **实测标定**后回写 `docs/runbook/tts_concurrency.md` 与 `pool_settings.concurrency`（C8 裁决）。

### 4.3.6 文本归一化、切分与缓存（`tts/text_normalize.py` / `segmenter.py`）

```python
def normalize(text: str, *, glossary: Glossary, mode: NormalizeMode = "tts") -> str:
    """幂等：normalize(normalize(x)) == normalize(x)。处理顺序（固定）：
      1) 去 Markdown/emoji/特殊符号（保留中文标点、书名号、引号）
      2) 数字/百分比/年份/单位 → 中文读法（'2026年' → '二零二六年'）
      3) 英文缩写与专名 → glossary 词典（'AI' → '诶哎'，'FFmpeg' → 'F F m peg'）
      4) 多音字/易错词 → glossary 强制替换
      5) 标点 → 停顿标记（由 prosody 层消费，不改变文本）
    禁止：依赖 pynini / WeTextProcessing（R5：Windows 不可用）
    """


def split_for_tts(text: str, *, max_chars: int = 28) -> list[str]:
    """按标点优先、语义次之切分；每片 ≤ max_chars 且 ≥ 4 字。
    切分结果回写 script_sentences（1 句 → 多行时，用 seq 小数编号：2, 2.1, 2.2）。"""


PAUSE_MAP: dict[str, int] = {
    "，": 180,
    "、": 120,
    "；": 260,
    "：": 240,
    "。": 420,
    "？": 480,
    "！": 460,
    "……": 520,
    "——": 300,
}


def tts_cache_key(
    *,
    engine: str,
    engine_revision: str,
    voice_id: str,
    normalized_text: str,
    speed: float,
    emotion: str,
    seed: int | None,
    sample_rate: int,
) -> str:
    """sha256(engine|engine_revision|voice_id|text|speed|emotion|seed|sr) → 32hex"""
```

- 命中路径：`data/cache/tts/<key>.wav`；命中即 `TTSResult(cache_hit=True)`，**不调用引擎**（P2 的性能前提）。
- 失效：引擎版本、音色、文本、语速、情感、seed、采样率任一变化即 miss。
- LRU 上限 5 GB；`use_count ≥ 2` 的条目在淘汰时降权（营销号文案复用率高，缓存是主要提速手段）。

**落地说明（T2.6 · 2026-09-16）**

| # | 规格写法 | 落地写法 | 为什么 |
| --- | --- | --- | --- |
| 1 | `sha256(engine\|engine_revision\|voice_id\|text\|speed\|emotion\|seed\|sr)` | 字段之间用 **`\x1f`**（Unit Separator）而不是 `\|` | 口播文本里出现竖线完全可能（"3\|5 的比例"）：分隔符与内容撞车会让两组不同字段拼出同一个字符串 ⇒ **假命中**（复用了别人的台词）。控制字符进不了归一化后的文本（T2.5 步骤 1 专门删这类字符） |
| 2 | `text` | **归一化后**的文本（`script_sentences.tts_text`） | 合成读的就是它。用原文做键会得到"键变了、念出来的字没变"的假 miss（改个 emoji 就重念一遍），以及反过来的假命中（词表热改后该念法变了、键却没变） |
| 3 | `speed` | `f"{speed:g}"` | `1` 与 `1.0` 必须是同一个键 —— 否则"调用方怎么写这个数"会决定缓存命不命中 |
| 4 | `seed` | `None` 编成 `"none"` | `None` = "引擎自己挑"、`0` = "就用 0"，两者编出来的音频不同，键必须区分 |
| 5 | 元数据（`use_count`） | **旁挂 `<key>.json`**，不进 DB | 缓存是**可丢弃**的东西（删掉只损失速度、不损失正确性），而 DB 里每一行都要有迁移 / 备份 / GC 的账。读坏写坏一律当"hits=0 的新条目" |
| 6 | "命中即跳过" | 命中后**必须把音频拷回交付路径**（`data/output/voice/<task_id>/sNNN.wav`） | §03.7.5 会在任务完成后 24 小时删句子 WAV：命中只说明"缓存里有"，交付路径上那份得补回来，否则渲染拿到的是不存在的文件 |
| 7 | `TTSResult(cache_hit=True)` | `jobs.result_json.cache_hit` + `tts.sentence_cache_hit` 日志 | 面板与报告读的是库里的那一份；"这一轮重念了几句"必须**有账可查**（验收硬断言"引擎调用为 0"） |

- 0 字节的缓存文件**不算命中**（只可能来自"写到一半断电"，当成命中会让下游拿到 0 秒音频）。
- 交付产物同理：`voice_worker._has_audio()` 判的是"非空"，不是"存在"。

---

### 4.3.7 配音操作面 REST 契约（T2.9 · 已落地）

配音面板（T4.5）要能在网页上做四件事：看逐句进度、试听某一句、重配某一句、换音色。原文 §7.2 只说"面板要能重配与试听"，这一节把**服务端形状**钉下来。

| 方法 | 路径 | 作用 | 关键语义 |
| --- | --- | --- | --- |
| GET | `/api/v1/sentences?task_id=` | 逐句状态 + 进度 | `audio_url` **只在盘上真有那一份**时才给（否则 `null`）；`can_resynth` 由服务端判（`synthesizing` 不给点）；`timeline_stale` = "盘上那份时间轴不再描述这条片子" |
| GET | `/api/v1/voices?task_id=` | 音色下拉框 | `voice_profiles`（已启用）∪ SAPI 已装；`source` 区分 `profile` / `sapi`；一个都没有 ⇒ `note` 说清怎么装 |
| POST | `/api/v1/sentences/{id}/resynth` | 单句重配 | **立刻返回**（只投递）；`done` / `skipped` / `pending` / `failed` ⇒ `pending` + 作业排回池；`synthesizing` ⇒ 409 |
| GET | `/api/v1/media/{path}` | 单句试听 | `{path}` 必须**整串**匹配 `voice/<task_id>/s00N.wav`（任务号那一段是 `[0-9A-Za-z_-]{1,64}`：**人起的任务号带连字符**，卡成 `[0-9A-Za-z]` 会让面板自己创建的任务号整屏 422，陷阱 #122）；**只发盘上已有的那一份，不触发合成**；`FileResponse` 自带 Range |
| PATCH | `/api/v1/tasks/{id}/voice_map` | 任务级换音色 | **增量**（只改给出的角色）；会重配 N 句且缺 `confirm` ⇒ 409；音色不在本机 ⇒ 422 |

**三条不变式**

1. **重配 = 业务表 + 作业表一起改**（`SentenceRepo.invalidate` + `JobStore.requeue_unit`）。队列的幂等键 `(task_id, pool, unit_type, unit_ref)` 让一条单元**一辈子只有一条作业**，`succeeded` 之后只改业务表 ⇒ **没有任何 worker 会再看它一眼**，而 `settle_voice` 的守卫是"全部句定局" ⇒ 任务卡在 `voicing` 且不报错（陷阱 #115）。`invalidate` 同时把 `tts_attempts` 归零（陷阱 #116）。
2. **换音色 = 三处一起改**：`tasks.payload_json.voice_map` / `script_sentences.tts_status` / `jobs.payload_json`（音色是**运行期选择**，worker 读的是作业 payload 或进程装配值）。少第三处 ⇒ 重配出来还是旧嗓子，而库里显示新音色（陷阱 #117）。
3. **试听不触发合成**（§05 T2.9 的硬要求）：三处候选（规范路径 / 库里的 `tts_audio_path` / TTS 缓存）只认"盘上已有"，一处都没有 ⇒ 404 `PATH_MISSING`。试听与合成抢同一台机器，而"看一眼"不该变成"重跑一遍"（还会覆盖交付产物）。库里的 `tts_audio_path` 另需判"落在 `data_dir` 里"（陷阱 #118）。

**音色校验的两条线**（别把它们合并）

| 场景 | 行为 | 为什么 |
| --- | --- | --- |
| 人**刚提交**的音色不在本机 | `TTS_VOICE_MISSING`（422）+ `context.available` | 写进去之后配音会连失败 3 次 ⇒ 全句降级成静音 ⇒ 用户看到"换音色成功"、拿到一支**没人声**的成片 |
| `voice_map` 里**存量**的音色不在本机（默认值就是逻辑角色名 `bigbear`） | 不报错；`resolve_voice` 退回进程音色并标 `fallback` | 那是"库里早就存着的默认值"，报错等于"每个新建任务都配不出音" |

**"刚提交的" = 请求体 `voice_map` 里出现的那几个键**，不是合并后的整张表。判整张表的话，新建任务第一次换音色必然 422：用户改的是熊大，被拒的理由却是他根本没碰过的熊二（默认值是逻辑角色名占位，陷阱 #123）。面板因此也**只提交真正改过的角色**（陷阱 #124）。

**状态与时间轴**

- `resynth` 之后该句回到 `pending`，**时间轴立刻过期**（它描述的是重配前那份音频）⇒ 响应里 `timeline_stale=true`。
- **不在这里重算**：任务在 `completed` 时没有 `completed → voicing` 这条边（§03.4.5），为了一个端点顺手重算去改松状态机不值得。下一轮收口（`settle_voice`）**全量重算**（陷阱 #26）。
- ⚠️ **别拿"总时长变了"当"重算过了"的证据**：真 SAPI 对同一句同一音色是**确定性的**，重念出来逐毫秒一致（真机演练实测 `total_ms` 14352 → 14352）。客观证据是 `timeline.json` / `voice_master.wav` 被重写（陷阱 #121）。

**留痕（`audit_ops`）**

| action | target_type | before | after |
| --- | --- | --- | --- |
| `sentence.resynth` | `sentence` | `tts_status` / `tts_attempts` / `tts_voice_id` / `tts_duration_ms` | `tts_status=pending` / `tts_attempts=0` / `job_id` / `job_created` / `voice` / `timeline_stale` |
| `task.voice_map` | `task` | `voice_map` | `voice_map` / `affected` / `requeued` / `created_jobs` / `busy` / `timeline_stale` |

`actor="user"`、`source="webui"`。`busy` 是"想失效但没动成"的句子（正被某个 worker 念着）—— **必须报出来**：静默跳过就是"换音色成功、成片里那一句还是旧嗓子"。

---

## 4.4 WebSocket 实时推送协议（`ws/protocol.py`）

### 4.4.1 连接与订阅

```
WS  ws://127.0.0.1:8787/ws/ui
    ?token=<一次性 token>                 # 本机回环免密；局域网强制（§01.2.5）
    &channels=tasks,logs,pools,metrics    # 可选，缺省=全部
    &task_ids=<id1,id2>                   # 可选，只看指定任务
    &min_level=info                       # 可选，日志级别下限
    &since_id=<system_logs.id>            # 可选，断线重连补发游标

握手后服务端立即下发：
  {"v":1,"type":"snapshot","channel":"tasks","seq":1024,"ts":"…","data":{"tasks":[…20 条…]}}
  {"v":1,"type":"snapshot","channel":"pools","seq":1024,"ts":"…","data":{"pools":{…}}}
```

**快照只发"已注册 provider"的通道**（`SnapshotRegistry.has()`）：`control` / `topics` / `publish`
在本阶段还没有 provider ⇒ 握手时**不发空快照**（否则前端会先收到一堆空壳帧）；
订阅了没有 provider 的通道**不报错**，只是先收不到东西。

**八个通道（原文 §7.2 的 8 个面板 + 工程新增）**

```python
Channel = Literal[
    "tasks",  # 任务状态与进度
    "logs",  # 日志
    "pools",  # 池状态
    "metrics",  # 资源指标
    "system",  # 告警与自检
    "control",  # 上行命令回执
    "topics",  # ★ 选题池（方向/选题候选的增删改）
    "publish",  # ★ 发布状态与数据回流
]
```

### 4.4.2 报文信封（统一）

```python
class Envelope(BaseModel):
    v: Literal[1] = 1
    type: Literal["snapshot", "event", "ack", "error", "pong"]
    channel: Channel
    seq: int  # **每连接**单调递增（从 1 开始；`snapshot` 与上行回执也占号），用于去重与缺口检测
    ts: str  # ISO8601 UTC 毫秒
    data: dict[str, Any]
```

**`seq` 是"每连接单调"，不是服务端全局计数器**：连接一旦按 `channels` / `task_ids` 过滤、并对同类事件做合并，
全局计数**必然**出现假缺口（前端会误判丢包并无限 `resync`）。每连接从 1 自增 ⇒ "缺口 ⇔ 真丢包"才成立。

### 4.4.3 事件表（下行，服务端 → 前端）

| channel | `data.kind` | 载荷字段 | 触发时机 | 频率上限 |
| --- | --- | --- | --- | --- |
| tasks | `task.created` | `id,title,status,priority,created_at` | 任务创建 | 事件驱动 |
| tasks | `task.updated` | `id,status,progress,stage_detail,version` | 字段变更 | **2 Hz/任务**（合并） |
| tasks | `task.transition` | `id,from,to,actor,reason,at` | 状态机迁移 | 事件驱动 |
| tasks | `task.progress_detail` | `id,pool,unit_ref,done,total,eta_ms` | 句/场景进度 | 2 Hz/任务 |
| tasks | `task.completed` | `id,final_path,duration_ms,quality` | 出片并可播放 | 事件驱动 |
| tasks | `task.failed` | `id,error_code,error_message,retry_from` | 进入 failed | 事件驱动 |
| tasks | `sentence.updated` | `id,task_id,seq,tts_status,start_ms,end_ms,duration_ms,version` | 单句完成/编辑 | 5 Hz/任务 |
| tasks | `review.scored` | `task_id,rule_total,llm_total,total,grade,issues_count` | 审稿完成 | 事件驱动 |
| tasks | `script.editing` | `task_id,round_no,max_rounds,issues` | 改稿中 | 事件驱动 |
| tasks | `degraded` | `task_id,reason,detail` | 任一降级触发 | 事件驱动 |
| tasks | `approval.requested` | `task_id,script_preview,grade,score_total,revision_round,sentence_count,est_duration_ms` | 进入确认闸 | 事件驱动 |
| tasks | `approval.decided` | `task_id,status,decided_by,comment` | 审批完成 | 事件驱动 |
| topics | `direction.batch_ready` | `batch_id,directions:[{id,seq,title,rationale,priority,risk_flags}]` | Planner 完成 | 事件驱动 |
| topics | `topic.batch_ready` | `direction_id,topics:[{id,title,hook_type,score,reason}]` | Ideator 完成 | 事件驱动 |
| topics | `topic.selected` | `topic_id,task_id,selected_by` | 勾选入队 | 事件驱动 |
| topics | `topic.dedup_warn` | `topic_id,similar_to:[{task_id,similarity}]` | 去重命中（R15） | 事件驱动 |
| publish | `publish.queued` | `task_id,platform,account_id` | 入队发布 | 事件驱动 |
| publish | `publish.progress` | `publication_id,stage,percent` | 上传中 | 1 Hz |
| publish | `publish.done` | `publication_id,url,published_at` | 发布成功 | 事件驱动 |
| publish | `publish.failed` | `publication_id,error_code,message,evidence_path` | 发布失败 | 事件驱动 |
| publish | `publish.manual_required` | `publication_id,reason` | 转"待人工发布" | 事件驱动 |
| publish | `metrics.updated` | `publication_id,views,likes,comments,shares` | 数据回收 | 事件驱动 |
| logs | `log.appended` | `log_id,level,source,message,task_id,payload` | 任意日志写入（**先落库再广播**） | **2 Hz** + `debug` 不落库 |
| pools | `pool.stats` | `pool,pending,blocked,claimed,succeeded,failed,dead,concurrency,running,paused,workers[]` | 5s 周期 + 变化触发 | 1 Hz |
| pools | `pool.worker_status` | `worker_id,pool,status,current_job_id,gpu_mem_mb` | 心跳变化 | 1 Hz |
| metrics | `metrics.tick` | `cpu_pct,ram_mb,gpu_util,gpu_mem_mb,disk_free_gb,tts_rtf_avg,render_fps` | 周期采样 | 0.2 Hz |
| system | `system.alert` | `log_id,code,severity,message,hint` | 磁盘/TTS/熔断/死信 | 事件驱动（**最高优先级，不合并**） |
| system | `system.doctor` | `ok,checks[]` | 手动/启动自检 | 事件驱动 |
| system | `system.persona_changed` | `persona_id,name,version,sha256,source,error,failed,reason,previous_persona_id,previous_version,changed` | 人物热重载 / 切换 / 文件损坏 | 事件驱动 |
| control | `command.result` | `request_id,ok,error` | 上行命令回执 | 事件驱动 |

**关键字段约定**

- `task.updated.progress ∈ [0,1]`；`eta_ms` 基于最近 N 个单元耗时中位数推算（无样本则 `null`）。
- `seq` 由服务端维护、**每连接**单调（从 1 开始；**非** DB 主键），前端据此检测丢包并触发 `snapshot` 重同步。
- `log_id` 是 `system_logs.id`（**不要复用 `id`**）：业务事件（`task.updated` 等）的 `id` 是实体主键，
  混用会让前端的去重/合并逻辑误伤正常事件。
- 所有时间戳为 UTC ISO8601（`2026-09-13T04:12:33.412Z`），前端负责本地时区展示。
- `system.alert.code` 枚举：`DISK_LOW` / `DISK_CRITICAL` / `TTS_CIRCUIT_OPEN` / `JOB_DEAD` / `POOL_AUTODEGRADED` / `PUBLISH_LOGIN_EXPIRED` / `DUP_AUDIT_WARN` / `BROLL_EMPTY`。

### 4.4.4 确认闸契约（原文 §2.2⑦ / §7.3③ · 全流程唯一人工节点）

**REST 上行**

| 方法 | 路径 | 请求 | 行为 |
| --- | --- | --- | --- |
| GET | `/api/v1/approvals?status=pending` | — | 待审列表 |
| POST | `/api/v1/tasks/{id}/approve` | `{comment?}` | `awaiting_approval → queued_voice`；写 `audit_ops` |
| POST | `/api/v1/tasks/{id}/reject` | `{comment}` **必填** | `awaiting_approval → editing`；`revision_round += 1` |
| POST | `/api/v1/tasks/{id}/discard` | `{reason?}` | `awaiting_approval → discarded` |
| POST | `/api/v1/approvals/approve_batch` | `{task_ids:[...]}` | 批量通过（原文 §7.2"批量操作"）—— **逐条**决断、部分失败不回滚（T4.4 裁定 124） |
| POST | `/api/v1/tasks/{id}/rescue` | `{reason?}` | 捞回：`discarded` / `canceled → pending`（误点放弃的唯一补救） |

**WS 下行**：`approval.requested` 必须携带 `grade` / `score_total` / `revision_round` —— 满足原文 §2.2⑦"呈现稿件+评分+修改次数"。

**分级放行（原文 §9.3"可设为全自动放行 A 级稿件"）**

```python
class AutoApprovePolicy(StrEnum):
    OFF = "off"  # A/B 都等人工
    GRADE_A = "grade_a"  # ★ 默认：A 级自动放行，B 级等人工
    GRADE_AB = "grade_ab"  # 全自动：A+B 都放行（"一键全自动"模式）


# 配置：config/app.yaml → approval.auto_approve_policy
# WebUI：总览台「一键全自动」开关 ⇒ 切 GRADE_AB（并写 audit_ops）
```

**四条不变量**

1. 确认闸是**唯一**允许要求人工的节点（P4）；其他任何环节不得设计人工步骤。
2. A 级自动放行时**必须**写 `audit_ops(action='task.approve', actor='auto', actor_ref='auto_approve_A')`。
3. 退回（reject）**必须**带 `comment`，并作为下一轮 Editor 的 `issues` 输入来源之一。
4. 确认闸**不阻塞**其他任务（闸只影响当前任务的状态迁移）。

**T1.11 施工修正：确认闸的业务规则住在服务层（裁定 94/99）**

- **落点**：`ReviewService.decide_approval(task_id, decision, comment=None)`。
  **REST 端点留给 T4.4** —— 上面四条不变量是**业务规则**，写在控制器里每个入口都得抄一遍，抄漏一处就是一条静默的合规漏洞。
- **失败语义**：**抛 `StudioError`**（人在按按钮，失败必须说清理由，不能吞成 `ok=False`）；成功返回 `ApprovalOutcome`（**没有 `ok` 字段** —— 能返回就是成功）。
- **不变量 2 的落库口径**：`approvals.decided_by='auto_approve_A'` **且** `audit_ops(action='task.approve', actor='auto', actor_ref='auto_approve_A')`。
  自动放行用 `record_auto_approval()` **一次事务**写入：`request()` + `decide()` 两个事务之间会留下永远 `pending` 的幽灵行（裁定 94）。
- **不变量 3 的落点**：退回时 `comment` 同时进 `approvals.comment` / `task_events.reason` / `audit_ops.reason`，供下一轮 Editor 取用。

```python
class ApprovalDecision(StrEnum):
    """人工决断的三值（= `approvals.status` 的已决值，裁定 99）"""

    APPROVE = "approved"  # ⇒ queued_voice
    REJECT = "rejected"  # ⇒ editing（revision_round+1，comment **必填**）
    DISCARD = "discarded"  # ⇒ discarded
```

### 4.4.5 面板 ↔ 通道 ↔ 接口 映射表（原文 §7.2 的 8 个面板）

| # | 面板（原文用语） | 订阅通道 | 主要 REST | 可执行操作 | 任务 |
| --- | --- | --- | --- | --- | --- |
| 1 | **总览台** | `pools` `metrics` `system` `tasks` | `GET /api/v1/overview`、`POST /api/v1/overview/{pools,auto,start}` | 启动 / 暂停 / **一键全自动**（切 `GRADE_AB`） | T4.2 |
| 2 | **选题面板** | `topics` | `GET /topics`、`GET /topics/directions`、`POST /hot/{import,submit}`、`POST /topics/{analyze,ideate,select,manual}` | 导入热点（扫盘 / 粘贴）/ 触发分析 / 生成选题 / 人工加选题 / 勾选入队 | T4.3 |
| 3 | **稿件面板** | `tasks` | `GET /scripts/{task_id}`、`GET /scripts/{task_id}/versions`、`GET /scripts/{task_id}/diff`、`POST /tasks/{id}/approve\|reject\|discard\|rescue`、`POST /approvals/approve_batch` | 确认 / 退回 / 放弃（**确认闸**）/ 批量通过 / 捞回 | T4.4 |
| 4 | **配音面板** | `tasks`（`sentence.updated`） | `GET /sentences?task_id=`、`POST /sentences/{id}/resynth`、`PATCH /tasks/{id}/voice_map` | 换音色 / 重配某句 / 试听单句 | T4.5 |
| 5 | **渲染面板** ✅ | **不订阅**（进度走 REST 轮询，1s · 有活才轮） | `GET /api/v1/render/console`、`POST /api/v1/render/jobs`、`GET /api/v1/render/jobs/{id}`、`POST /api/v1/render/jobs/{id}/cancel`、`GET /api/v1/render/videos/{name}` | 填表单触发出片 / 看进度与日志尾巴 / **协作式取消** / 成片**在线播放**与下载 | T4.6 |
| 6 | **模板面板**（一期落地为「合成配置」） | `logs`（`source=outputs`） | `GET /api/v1/outputs`、`POST /api/v1/outputs`；`GET/POST /templates`、`POST /templates/{id}/{validate,assets}`（**二期 · C13**） | 表单编辑合成 profile / 水印 / 字幕（**保存前强校验** + 并发指纹）；拖拽定位与三层模板树**延后二期**（R17） | T4.7 |
| 7 | **素材库** | — | `POST /assets/ingest`、`GET /assets/stats`、`PATCH /assets/{id}` | 上传 / 预览 / 标记（跑酷·原声·BGM） | T4.8 |
| 8 | **实时日志** | `logs` | `GET /logs?level=&task_id=&cursor=` | 过滤级别 / 搜索 / 导出 | T4.9 |
| 9 | **发布面板**（工程必需，原文未列） | `publish` | `GET /publish/queue`、`POST /publish/{id}/retry`、`POST /publish/{id}/cancel`（**端点在 T5.3 已落地**，见 §4.6.7） | 待发布 / 已发布 / 数据回流 / 待人工 | T5.5 |
| 10 | **四池调度**（工程必需，原文未列） | `pools` | `GET /api/v1/pools`、`POST /api/v1/pools/{concurrency,requeue}` | 调并发（无需重启）/ 暂停恢复（复用面板 1 的入口）/ 死信重投 / 看自动降级 | T4.10 |
| 11 | **人物库**（工程必需，原文未列） | `system`（`system.persona_changed`） | `GET /api/v1/persona`、`POST /api/v1/persona`、`POST /api/v1/persona/{activate,save-as,rollback}` | 改人设 / 口吻 / 口癖 / 禁区（**保存前强校验**）/ 一键切换（旧版自动备份）/ 另存为 / 回滚 | T4.13 |

### 4.4.6 背压、合并与重连

| 机制 | 参数 | 说明 |
| --- | --- | --- |
| 合并窗口（coalescer） | 100 ms | 同 channel + 同实体的事件在窗口内合并为最后一条（保留最大 progress） |
| 单任务限流 | 2 Hz（进度）/ 5 Hz（句更新） | 超限合并；`system.alert` **不限流** |
| 环形缓冲 | 200 条/连接 | 满则丢弃 `debug` → `info` → 合并同类；**永不清空 `alert`** |
| 慢客户端 | **环形缓冲满（200 条/连接）且持续 10s** | 断开（1008 + `WS_CLIENT_SLOW`）并让前端 `resync`（避免拖慢整个 hub） |
| 上行回执 | `pong` / `command.result` / `error` **直发** | 绕过订阅过滤与合并：否则只订阅 `channels=logs` 的连接发 `ping` 永远收不到 `pong` |
| 唤醒（`wake`） | 跨线程安全 | 写日志的可能在事件循环之外（线程池 / worker 线程）⇒ `call_soon_threadsafe` 叫醒 tail 循环 |
| 日志补发 | `since_id` 上限 2000 条 | 超出则先发 `snapshot`（最近 500 条）+ 提示"更早日志请走 REST 分页" |

```
前端断线 → 指数退避重连（1s,2s,4s…上限 15s）
        → 带上最后收到的 since_id 与 seq
        → 服务端：logs 按 id > since_id 补发；tasks/pools 直接重发 snapshot
        → 前端以 seq 校验：发现缺口(seq != last+1) ⇒ 发 resync
```

**顺序铁律**：**先落库，再广播**（陷阱 #13）。禁止先广播后写 `system_logs`，否则断线重连会永久丢日志。

**面板侧的两条落地（T4.9 裁定 119 / 122）**

- **过滤在客户端做**：前端订阅一律带 `min_level=debug`（=「把库里有的都给我」；库里本来就不落 `debug`，
  所以不额外传数据），级别 / 来源 / 任务 / 搜索全在本地过滤。这样切过滤**不重连**，
  「刚滚过去的那几行」也不会在重连窗口里消失。
- **告警必须单独订阅 `system` 通道**：告警**不在** `logs` 通道上（§04.4.3 的分流），
  只订阅 `logs` 会把告警的 id 当成缺口，于是每次告警都触发一次无谓的补洞。

---

## 4.5 Worker 心跳与日志契约

### 4.5.1 心跳（`pools/heartbeat.py`，每 5s upsert）

| 字段 | 说明 |
| --- | --- |
| `worker_id` | `<pool>#<slot>@<pid>`（如 `voice#1@20344`） |
| `status` | `idle` / `busy` / `draining`（优雅退出中）/ `dead` |
| `current_job_id` | 正在执行的 job（idle 时为 `null`） |
| `gpu_mem_mb` | voice worker 上报 TTS 服务侧显存（由 `/health` 转发） |
| `cpu_percent` / `rss_mb` | 资源画像（用于识别泄漏：RSS 持续增长报警） |

- 超时判定：`now - last_seen_at > 15s` ⇒ 标记 `dead` ⇒ supervisor 重启该 worker ⇒ 其租约由 sweeper 回收（**双保险**）。
- WebUI 池监控直接消费 `pools` 通道的心跳数据。
- **不变式：行存在 ⇔ 该进程应当在跑**（T1.6 施工裁定 33）。优雅退出（`draining` 跑完当前单元）时 worker **删掉自己那行**（`HeartbeatStore.forget`）⇒ "行还在但 15s 没动静"就一定是猝死，不误报。
- 写心跳与续 job 租约是**同一个后台脉冲线程**（T1.6 施工裁定 34）：心跳 5s、续租 `lease/3`、判超时共用一条 1s tick；**tick 异常一律吞掉继续** —— 脉冲线程死了就"看着还活着但不再续租"，比直接崩更危险。
- `WORKER_DEAD` **不是** `system.alert.code`（§4.5.2 把告警码锁死为 8 值）：判死落 `system_logs` 的 `error` 行 + `payload_json.code='WORKER_DEAD'`，WS 层按"不在 8 值内 ⇒ 走 `log.append`"处理（T1.6 施工裁定 39）。

### 4.5.2 日志事件规范（`source` 命名空间）

| source | 写入方 | 典型 message | 级别 |
| --- | --- | --- | --- |
| `http` | 中间件 | `POST /api/v1/tasks 201 12ms` | info / error(5xx) |
| `pipeline` | orchestrator | `task → voicing（gate approved by user）` | info |
| `agent.planner` / `agent.ideator` / `agent.director` / `agent.writer` / `agent.reviewer` / `agent.editor` / `agent.cover` | agent 层 | `draft ok: 682 字 / 预估 58.4s / tokens 1832` | info |
| `tts.cosyvoice` | TTS 服务 | `synth bigbear 27 字 1820ms RTF=0.31 cache=miss` | info |
| `tts.fallback` | 降级链 | `TTS_OOM → rewarm_engine (attempt 2/3)` | warn |
| `render.compile` | 编译器 | `scene_001 nodes=41 inputs=5 hash=ab12…` | info |
| `render.ffmpeg` | runner | `scene_001 done 6.92s in 8.4s (1.21x) cq=23` | info |
| `render.audit` | 审计 | `av_sync offset=+38ms lufs=-16.1 tp=-1.8` | info / warn(超阈) |
| `publish.browser` | Playwright 适配器 | `upload douyin 62% (title filled)` | info |
| `publish.audit` | 发布前校验 | `precheck ok: dup_audit pass / banned 0` | info / warn |
| `pool.<name>` | worker | `claimed job … attempts=2/3` | info |
| `persona` | 人物库服务 | `persona.update tone → v3 sha=ab12…` | info |
| `outputs` | 合成配置服务 | `outputs.update subtitle.font_size → v2 sha=ab12…` | info |
| `system` | 任意 | `disk_free=D:14.2GB 触发 DISK_LOW` | warn / error / fatal |

**约束**：`message` 必须人类可读（WebUI 直接展示），结构化细节一律进 `payload_json`；**禁止**把 stderr 原文直接塞进 `message`（放 `payload.stderr_tail`，截断 8KB）。

### 4.5.3 日志 REST 面契约（T4.9 · 过滤 / 搜索 / 导出）

> WS 负责"看得快"，REST 负责"看得全"。这一节把 REST 面的**过滤语义**钉死 ——
> 面板上的过滤与导出的过滤必须是同一套，否则"屏幕上看到的"与"导出的"会是两份数据。

| 端点 | 用途 | 关键参数 |
| --- | --- | --- |
| `GET /api/v1/logs` | 分页 / 增量 / 翻历史 / **补洞** | `since_id`（增量）、`until_id`（窗口上界，含）、`limit` ≤ 1000、`level`（**下限**语义）、`task_id`、`source`、`search` |
| `GET /api/v1/logs/export` | 导出 NDJSON（**流式**） | 同上 + `limit` ≤ 200000（默认 20000） |

三条不变量：

1. **`level` 是下限不是等值**（与 WS 的 `min_level` 同义）：`level=warn` ⇒ warn + error + fatal。
2. **`search` 是字面子串**，同时匹配 `message` 与 `source`（搜 `render` 要能捞到 `render.ffmpeg`）；
   `%` / `_` / `\` 一律**转义**后进 `LIKE ... ESCAPE '\'` —— 否则用户搜 `50%` 会把整张表捞回来（陷阱 #61）。
3. **导出不做 meta 行**：文件必须是干净的 NDJSON（一行一条，可直接喂给 `jq` / pandas）。
   行数**恰等于 `limit`** ⇒ 前端提示"可能被截断"，而不是服务端悄悄截断。

**WS 有损、REST 补齐**（T4.9 施工裁定 120）：

`log.appended` 的合并键落在 `task_id` 上（`KIND_POLICY` 里 `merge_field=None` ⇒ 退化成任务级），
因此**同一任务在 100ms 内的多条日志只会留最后一条**。这不是缺陷，是 §04.4.6 的既定取舍
（"高频日志拖慢前端 ⇒ 合并"）。所以日志面板的"不丢"由三段拼成：

```
① 连接期内：WS 实时流（可能被合并掉几条）
② 断线重连：带 since_id 补发（§04.4.6）
③ 发现跳号：GET /api/v1/logs?since_id=<连续游标> 把洞补回来（冷却 1s + 单飞）
```

第 ③ 段要求前端同时订阅 `logs` 与 `system` 两个通道：**告警不在 `logs` 通道上**
（Hub 按 `is_alert_code` 分流到 `system`，§04.4.3），只订阅 `logs` 会把告警的 id 当成缺口。
补不回来的（已被 GC 回收）⇒ 如实记 `lostIds` 并跳过游标，**不能卡住**（裁定 121）。

### 4.5.4 稿件 / 确认闸 REST 面契约（T4.4 · 已落地）

> 确认闸是全流程**唯一**的人工节点（§04.4.4 不变量 1）。它必须能回答「为什么退回」
> 「改了几轮」「这一版动了哪几句」—— 只给一个总分，人在几十秒内没法决断（原文 §2.2⑦）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/approvals?status=&limit=` | 待审 / 已决列表 + `counts`（`status` 只接受 `pending\|approved\|rejected\|discarded\|expired`，非法 ⇒ 422） |
| GET | `/api/v1/scripts/{task_id}` | **一次请求拿全**：正文 + 逐句表 + **全部轮次**的双通道评分 + 当前待审记录 |
| GET | `/api/v1/scripts/{task_id}/versions` | 版本列表（`is_active` 标出当前生效版，含 `sentence_count`） |
| GET | `/api/v1/scripts/{task_id}/diff?from_version=&to_version=` | 两版**逐句**对照（两个版本号都**必填**，没有默认值） |
| POST | `/api/v1/tasks/{id}/approve\|reject\|discard` | 三条决断；规则住 `ReviewService.decide_approval`（§04.4.4 T1.11 修正） |
| POST | `/api/v1/approvals/approve_batch` | 批量通过（**逐条**留痕，部分失败不回滚） |
| POST | `/api/v1/tasks/{id}/rescue` | 捞回（`discarded` / `canceled → pending`，连带恢复 `topic_candidates`） |

**四条落地口径**

1. **错误信封只有一种形状**（裁定 127）：业务错误（`StudioError`）与请求校验失败
   （`RequestValidationError`）都翻成 `{code, message, remediation, context}`，映射表住
   `app/errors.py`（`TASK_NOT_FOUND` ⇒ 404、`APPROVAL_NOT_PENDING` ⇒ 409、
   `APPROVAL_COMMENT_REQUIRED` ⇒ 422 …）。前端因此只需要解析**一种**形状。
2. **"一次请求拿全"而不是四个端点**：改稿期间（`editing → reviewing`）各端点本来就可能读到
   不同版本，拼出来的页面会自相矛盾（正文是 v2、分数是 v1 的）。
3. **批量是"尽力而为"**：逐条决断、逐条写 `audit_ops`，`approved` / `failed` **分别**如实返回，
   一条失败不影响其余。报成"整体失败"会让人重按一次，而重按只会撞 `APPROVAL_NOT_PENDING` 的噪音。
4. **`diff` 走块级匹配而不是逐 `seq` 对齐**（裁定 128）：插入一句不能把后面全标成"改了"；
   `replace` 块内按位置配对，多余一侧降级为 `insert` / `delete`。

**事件搭日志的车（跨进程推送 · 裁定 126）**

worker 进程没有 IPC 能把事件推给 API 进程的 Hub，而「表 → 推送」天然跨进程：
`Hub.envelopes_for(row)` 把一行日志翻成**常规帧**（`log.appended` / `system.alert`）
＋**可选第二条结构化事件**（按 `KIND_POLICY[kind].channel` 走对应通道）。事件名写在
`payload["event_kind"]`（`core/proto.py` 的 `EVENT_PAYLOAD_KEY`）—— **不能**叫 `event`：
structlog 的第一个位置参数就叫 `event`，`logger.info(msg, **payload)` 会 `TypeError`（陷阱 #63）。
`EventKind` 因此从 `ws/protocol.py` **下沉**到 `core/proto.py`（`ws` 侧原样 re-export，导入路径不变），
因为分层方向 `app → ws → services → db` 不允许 `services/` 反向 import `ws/`（陷阱 #64）。
认不出的 `event_kind` ⇒ 记 `ws.unknown_event_kind` warn 后**照常发日志帧**：少一条事件不该丢一条日志。
### 4.5.5 选题面板 REST 面契约（T4.3 · 已落地）

> 选题面板是流水线的**入口**：它决定"这一批做什么"。三个动作会花 LLM 预算
> （`analyze` / `ideate` / `select{draft_now}`），所以每一个都要能回答"我点了几次、
> 到底跑没跑、为什么没产出"（原文 §2.2①② / §7.2 面板 2）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/topics?status=&limit=` | 选题池瀑布流（`ORDER BY score DESC, seq`）+ 按状态计数；`status` 只接受 `candidate\|selected\|queued\|rejected\|expired`，非法 ⇒ 422 |
| GET | `/api/v1/topics/directions?batch_id=` | 方向卡片 + **全部**历史批次（新到旧，上限 20）+ 每方向 `topic_count` / `selected_count` |
| POST | `/api/v1/topics/analyze` | 扫盘导入 ⇒ Planner ⇒ 5–8 个方向（**长任务**） |
| POST | `/api/v1/topics/ideate` | 逐方向产出 3–5 条选题（**长任务**；一个方向失败不影响其他） |
| POST | `/api/v1/topics/select` | 勾选入队：逐条建任务（幂等键 `topic:<id>`），`draft_now` 才顺手写稿 |
| POST | `/api/v1/topics/manual` | 人工加选题（直接入库 + `audit_ops`） |
| POST | `/api/v1/hot/import` | 扫盘导入 `data/hot/*.md` + `data/feedback/*.md`（幂等） |
| POST | `/api/v1/hot/submit` | 网页端粘贴一批输入源 ⇒ 落 `data/hot/webui-<ulid>.md` ⇒ 立刻导入 |

**五条落地口径**

1. **长任务单飞**（裁定 129）：`analyze` / `ideate` 入口一把**进程级** `threading.Lock`
   非阻塞地拿一下，拿不到 ⇒ 409 `TOPIC_BATCH_RUNNING`（**不排队**：排队会让前端挂住，
   用户不知道自己排在第几，而重试按钮本来就在他手边）。用 `threading.Lock` 而不是
   `asyncio.Lock`：同步路由跑在 Starlette 线程池里（每次可能不同线程），
   `asyncio.Lock` 与事件循环绑定；跨"线程池 + 事件循环"两种执行环境共用一把锁，
   `threading.Lock` 是唯一不会在换线程/换循环时炸掉的选法。
2. **长任务失败返回 200 + 体内 `ok:false`**（裁定 134）：与确认闸"能返回就是成功"
   （§04.5.4）不同 —— 这里 HTTP 层确实成功了（请求合法、流水线跑完），失败的是**产出**
   （`LLM_SCHEMA_INVALID` / `HOT_TEXT_EMPTY`）。用 5xx 会让"重试三次"看起来像后端崩了，
   而真正要看的是 `error_message` 与逐方向 `outcomes`。
3. **`select` 逐条**：与批量通过同一取舍（§04.5.4 第 3 条）—— 一条失败不影响其余，
   `selected` / `failed` **分别**如实返回，失败带 `code` + `remediation`。
4. **人工加选题去重只提示不拦**（裁定 131）：模型产出的重复是噪音（白烧 token），
   人加的重复是**明确意图**。命中相似度写 `similar_to` + 回一条 `warnings`，人自己判断；
   `exec_feasible` 恒为真（那是模型对"3 分钟能不能做完"的估计，人加了它没有可估的输入）。
   挂靠固定批次 `manual`（懒建一次，裁定 130）—— `direction_id` 是 NOT NULL + 外键，
   现造一次性批次会让统计散落。
5. **`hot/submit` 的文件名由服务端生成**（`webui-<ulid>.md`）：客户端给名字就意味着要校验
   路径穿越、非法字符、覆盖已有文件，而这三件事没有一件对用户有价值（他关心的是
   "这段热点进去了没有"）。

**响应模型的集合字段一律必填**（T4.3 施工中发现 · 裁定 135）

`Field(default_factory=list)` 在 Pydantic 的 JSON Schema 里既不进 `required`、也不带
`default`，于是 `openapi-typescript` 把它渲染成 `T[] | undefined` —— 一个"后端一定给、
前端却要处处 `?? []`"的假可选（本面共 17 个字段）。响应模型因此统一写成 `x: list[T]`
（与 §04.5.4 的 `approvals: list[ApprovalItem]` 同写法）；请求体保留默认值。
反过来，**带 `default` 的属性会被渲染成必填**：`AnalyzeBody.import_sources` /
`IdeateBody.per_direction` 在前端必须显式传（顺带让"这一跑要不要先扫盘"在调用点看得见）。

**事件扇出（走 §04.4.3 的四条 `topics` 事件）**

| 事件 | 触发 | 合并键 | 载荷要点 |
| --- | --- | --- | --- |
| `direction.batch_ready` | Planner 落库后 | `batch_id` | `batch_id` + `directions:[{id,seq,title,rationale,priority,risk_flags}]` |
| `topic.batch_ready` | 每个方向产完选题 | `direction_id` | `direction_id` + `topics:[{id,title,hook_type,score,reason}]` |
| `topic.selected` | 勾选入队（含幂等复用） | `topic_id` | `topic_id` + `task_id` + `created_task` |
| `topic.dedup_warn` | 去重命中 | `topic_id` | `topic_id` + `similar_to` |

一个方向一条（`merge_field='direction_id'`）是刻意的：8 个方向的批量生成若合成一条，
"第 3 个方向失败"就没法只刷新它对应的那块面板。

---

### 4.5.6 总览台 REST 面契约（T4.2 · 已落地）

> 总览台是操作台的**首屏**：它要在一屏里回答"四个池什么情况 / 今天出了多少片 /
> 机器还撑得住吗 / 我能按哪些按钮"（原文 §2.2 / §7.2 面板 1）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/overview` | **一次拿全**：四池卡片（含 worker 心跳）+ 今日产量 + 资源快照 + 五进程就绪 + 当前策略 |
| POST | `/api/v1/overview/pools` | 暂停 / 恢复一个池（`paused` 是**目标状态**，不是"切换"） |
| POST | `/api/v1/overview/auto` | 切自动放行策略（`grade_ab` = 一键全自动；`grade_a` / `off` = 回退） |
| POST | `/api/v1/overview/start` | 拉起五进程（**同步等就绪**，上限 60s ⇒ 前端超时给 90s） |

**三条不变量**

1. **"今日"是本地日，不是 UTC 日**（裁定 143）。`substr(created_at,1,10)` 是 UTC 日：
   在 UTC+8 的 08:00 之前，今天新建的任务会被算进"昨天"。窗口由
   `stats_repo.local_day_window()` 算成一对**左闭右开**的 UTC 时间戳再进 SQL。
2. **"暂停"只写一行 DB，不重启 worker**（裁定 138）。`worker_base` 每轮认领前读一次
   `pool_settings`，而 `JobStore.claim()` 开头就是 `if runtime.paused: return None`
   ⇒ 新单元不再被认领，**在途单元照常跑完**。生效延迟 ≤ 一个空转退避周期
   （200ms–2s），所以面板写的是"已暂停（在途 N 个跑完后停）"，不是"已停止"。
   `paused_at` / `paused_by` 恢复时**清空**（留一个"并不在暂停"的时间戳只会让人误判），
   "谁在什么时候暂停过"本来就在 `audit_ops` 里。
3. **`DISK_LOW` 的去重是状态机，不是节流**（裁定 141 / 143）。`free_D < 15GB` 会**持续**
   成立，而 `system.alert` 永不合并、永不限流（§04.4.6）⇒ 每拍写一条等于一小时 720 条。
   只在 `ok → low` 写告警；`low → ok` 写 `info` 日志（`payload.code='DISK_RECOVERED'`，
   **不在** 8 个 `AlertCode` 里 ⇒ 走 `logs` 通道，不占"永不合并"的那条）。
   初值 `None` 表示"**还没采过**"，不是"低水位"：首拍**低位照常告警**（API 起来时磁盘
   已经满了同样要留痕），首拍**健康则一个字都不写** —— 否则每次启动都会凭空写一条
   "磁盘水位恢复"，而它排在 tail 游标之后，会顶掉日志通道的第一帧。

**两条"只暴露一半"的口径**

- **只暴露"启动"，不做"停止"**（裁定 137）：`ServiceManager.stop()` 的第一个目标就是
  `api` 自己 —— 在 API 进程里停服务等于自杀。停止的正门是 `停止.bat`。
  启动必须 `open_browser=False`（点按钮的人已经在浏览器里）+ `doctor_gate=True`；
  入口一把 `threading.Lock` **非阻塞**地拿（与 §04.5.5 第 1 条同手法），拿不到 ⇒
  409 `SERVICE_START_BUSY`（不排队）。
- **"一键全自动"改的是 `config/app.yaml` 的那一行，不是另开一张表**（裁定 139）：
  `approval.auto_approve_policy` 的真相源在 YAML，消费方是写稿池里的 `ReviewService`；
  再建一张表就有**两处真相**，分叉那天没人说得清哪份算数。落点四件事：**逐行替换**
  （保留全部注释，不用 `yaml.safe_dump` 整份重写）→ 回读校验 → 同步内存
  `RuntimeSettings` → `audit_ops` 留痕。顺序不能反：先写盘再改内存，写盘失败时
  内存不会与盘上分叉。

**采样方在 `app/` 而不是 `services/`**（裁定 140）

"怎么采、阈值怎么判"留在 `services/metrics_service.py`；"什么时候推、推给谁"在
`app/metrics.py` 的 `MetricsPump`。分层的收益很实际：`services/` import `ws/` 会成环
（陷阱 #64），而 CLI 排障入口与 WebUI 用**同一份**数字，不会出现"命令行说 12 GB、
网页说 15 GB"这种查不完的悬案。

5s 一拍（0.2 Hz，§04.4.3 的上限），同一拍里扇出 `metrics.tick` + 四池 `pool.stats`，
心跳 `pool.worker_status` **只在指纹变化时发**。三者挤在一拍是因为它们的真相是同一份：
拆成三条定时器，面板上迟早出现"池说 3 个在跑、心跳说 2 个活着"的一帧 —— 而那一帧
恰好是用户盯着看的那一帧。整拍丢进 `asyncio.to_thread`：`nvidia-smi` 一次 80–150ms，
在事件循环里直接跑会每 5s 卡一次 Hub 的 tail 与推送。

**前端的两个取舍**

- `metrics.tick` 的载荷与 `ResourcesModel` **同源同形**（都出自
  `ResourceSnapshot.to_dict()`）⇒ **直接合并**，不重拉。
- `pool.stats` / `pool.worker_status` 是 `PoolStatus` 的**真子集**（没有 `paused_at` /
  `paused_by` / `backlog` / `alive_workers` / `error`，`workers` 还被拆成单独的事件）
  ⇒ 走 500ms 合并窗口**重拉**：在前端合并等于维护第二份卡片形状，迟早与后端分叉，
  而它们本来就只有 1 Hz。
- **不做兜底轮询**：WS 断了的时候顶栏的 WS 灯就是红的，用户一眼知道"这屏不动了"；
  15s 一次的兜底轮询会喂出"看起来在动、其实是旧数据"的假象。重连成功时 store
  主动重拉一次补缺口。

### 4.5.7 四池调度控制台 REST 面契约（T4.10 · 已落地）

> 四池面板是**总览台那一屏的下钻**：总览台回答「现在什么情况」，这里回答「**要动哪个旋钮**」
> （原文 §2.2 / §7.2 面板 1；§03.4.4 的池控制）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/pools` | **一次拿全**：四个池的配置值 + 运行值 + 状态 + 死信（每池最近 20 条）+ 自动降级开关与阈值 |
| POST | `/api/v1/pools/concurrency` | 调一个池的并发（`concurrency` 是**目标值**；写 `pool_settings` + `audit_ops`） |
| POST | `/api/v1/pools/requeue` | 批量重投死信（`dead → pending` + `attempts` 归零 + `audit_ops`），**逐条独立** |

**四条不变量**

1. **暂停 / 恢复不在本面**（裁定 144）。它已经在 `POST /api/v1/overview/pools`（§04.5.6）：
   同一件事两条写路径，审计 / 事件 / 幂等判据迟早分叉。四池面板是它的**第二个视图**，
   复用同一个写入口（前端的暂停按钮调的仍是那个端点）。
2. **配置值与运行值并排**（裁定 150）。`config_concurrency`（`pools.yaml` 的出厂值）与
   `concurrency`（`pool_settings` 的当前值）**必须一起给**。只显示一个，就会出现
   「我明明把 YAML 改成 3 了，面板还是 1」这种查不完的悬案 —— 双真相的唯一体面解法是两个
   都摆出来。配置读失败 ⇒ 200 + `config_error` + 每池 `error`，**不是** 500：
   面板的作用就是「告诉我现在什么情况」，而配置坏了的时候这条信息最值钱。
   读的**只有** `config/pools.yaml` 这一份（`load_pools_config`，裁定 150）——
   否则 `llm.yaml` 里少一个 key 会让四池面板打不开。
3. **并发上限硬编码，且分两层**。请求体 `le=8`（与 DDL `CHECK (concurrency BETWEEN 0 AND 8)`
   同一条线，越界 ⇒ 422 `VALIDATION_FAILED`）；服务层按池取 `concurrency_bounds()`
   （`voice ≤ 3` 是 8GB 显存下的工程值，越界 ⇒ 422 `POOL_CONCURRENCY_LIMIT` + `remediation`
   「8GB 显存跑 4 路 TTS 必然 OOM」）。下限是 **1**：DDL 允许 0，但 0 看着像并发数、
   行为却是「沉默的暂停」，与 `paused=1` 在面板上无法区分（裁定 145）。
4. **下调不杀在途**（§03.4.4 不变量 2）。新值只影响 `claim()` 的守卫
   （`running_count(pool) >= concurrency` ⇒ 不认领），在跑的几个自然跑完、池**收敛**到新值。
   响应的 `note` 直接写人话（「并发 2 → 1：在途 2 个会跑完，之后收敛到 1」），面板原样显示 ——
   用户真正要知道的是「我这一下调，会不会打断正在跑的活」。

**自动降并发的可见面**（判据见 §03.3.17）

| 字段 | 含义 |
| --- | --- |
| `consecutive_oom` | 该池连续 OOM 计数（`succeed()` 归零；**人工重投死信不清零**，裁定 153） |
| `oom_threshold` | 阈值（`config/pools.yaml → auto_concurrency.oom_threshold`） |
| `auto_degrade_enabled` | 开关（关掉 ⇒ 只计数、不降并发） |

降级后**不会自动回升**（`recover_after_min` 留给 T4.11）：面板必须把这句话写出来，
否则用户会一直等它自己涨回去 —— 等到发现不会涨，故障已经多烧了一晚上。降级动作本身留痕在
`audit_ops(action='pool.autodegrade', actor='auto', source='worker')`，告警走
`system.alert(POOL_AUTODEGRADED)`（`warn`，§04.5.2 的 8 值枚举之内）。

**前端两个取舍**（与 §04.5.6 同源）

- `pools` 通道上的 `pool.stats` / `pool.worker_status` 是 `PoolConsole` 的**真子集**
  （没有 `priority` / `consecutive_oom` / `dead_letters` / `config_error`）⇒ 走 500ms
  合并窗口**重拉**，不在前端拼第二份卡片形状。
- 池名与中文标签、`describeError` / `isPoolEvent` / `createCoalescer` 三个纯函数从
  `stores/overview.ts` **复用**（裁定 156）：它们是同一套语义，抄第三份只是把同一处 bug 修三遍。

### 4.5.8 人物库 REST 面契约（T4.13 · 已落地）

> 人物库面板是**唯一一处「改完立刻影响下一稿」的地方**：`config/persona.yaml` 是唯一人工必填项（E7），
> 也是唯一「改错了会一路带进所有稿子」的文件（§02.4 / §04.1.6）。所以这一面的重点不是「能改」，
> 而是**改错了退得回来**。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/persona` | **一次拿全**：激活人物（含 `stale` / `last_error`）+ 人物库 + 最近 20 份备份 + 表单上下限 |
| POST | `/api/v1/persona` | 保存表单（**先校验、再备份、后落盘**）；`changes` 只交**显式给了**的字段 |
| POST | `/api/v1/persona/activate` | 一键切换（旧版自动备份 ⇒ 换错人永远退得回来） |
| POST | `/api/v1/persona/save-as` | 把当前激活人物存进人物库（`overwrite` 显式覆盖） |
| POST | `/api/v1/persona/rollback` | 回滚到某份备份（**先备份当前** ⇒ 回滚本身也能再回滚） |

**六条不变量**

1. **文件是唯一真相，表是投影**（裁定 165）。四个写端点只改 `config/persona.yaml`
   与 `config/personas/*.yaml`；`personas` 表（§03.3.1）是它们的**只读投影**。面板不提供「直接编辑人物库某一条」
   的入口 —— 改库里那条得先切过去再改（或 `save-as --overwrite`）。不这么做的话，「激活的」和「库里被改的」
   会变成两份互不知情的真相。
2. **没有 `/validate` 端点**。保存本身就**先校验后落盘**：再开一个「只校验」的端点，等于同一套
   判定跑在两条路径上 —— 两条路径的差异就是「面板说没问题、保存却 422」的来源。表单要即时反馈，
   前端拿 `limits` 自己判长度；**真判定只有一次**，在保存那一下。
3. **上下限跟着响应下发**（裁定 161）。`limits` 从 `PersonaConfig.model_fields` **现取**，
   前端不抄第二份：某天把 `target_chars_max` 从 8000 调到 12000，面板跟着变，
   不会出现「前端拦着、后端放行」。
4. **激活文件坏了也返回 200**。`active_error` / `active.stale` 带原因，下面那些字段是
   **上一份好的**。这个面板存在的意义就是「人物坏了的时候把它修回来」，一个 500
   恰好把工具关在门外。
5. **通用配置码在服务层翻译**（裁定 163）：`CONFIG_MISSING → PERSONA_NOT_FOUND(404)`、
   `CONFIG_INVALID → PERSONA_INVALID(422)`、`save-as` 撞名 → `PERSONA_EXISTS(409)`。不翻译的话
   `activate('ghost')` 会带着 `CONFIG_MISSING` 冒到应用级 handler —— 那张映射表里没有它 ⇒ **500**，
   用户看到「服务器挂了」，实际只是 id 打错了。**store 的既有语义一个字不改**
   （T1.2 的单测逐条断言它）。
6. **`changed=false` 是如实回答，不是失败**。「我已经是他了」不该假装做了一次操作（不写盘、
   不备份、不留痕），但也不能报错 —— 面板据此说「当前就是「X」，没有做任何改动」。

**审计与日志**

| 动作 | `audit_ops.action` | `target_id` | 其余 |
| --- | --- | --- | --- |
| 保存表单 | `persona.update` | 激活人物 id | `target_type="persona"`、`actor="user"`、`source="webui"` |
| 一键切换 | `persona.activate` | 切换后的 id | 同上 |
| 另存为 | `persona.save_as` | **新存的那个 id** | 同上 |
| 回滚 | `persona.rollback` | 回滚后的 id | 同上 |

`before` / `after` 只放 `{persona_id, name, version, sha256}` 指纹，**不放全文**（审计表不是媒资库）。
日志走 `system_logs.source='persona'`（§04.5.2），payload 里**不带 `kind`** —— 带的话 Hub tail
会再广播一遍，一条改动两条事件。

**备份命名**（`data/backups/persona/`）：`<yyyymmdd-HHMMSS>[-<n>]_<persona_id>.yaml`。
同秒撞名退到 `-2` / `-3`（裁定 164）—— 只到秒的话，「改一次 + 立刻回滚一次」会互相覆盖，
回滚变成空转（版本 +1、`sha256` 不变，从面板上完全看不出来）。

**前端两个取舍**（与 §04.5.7 同源）

- WS 事件 / 刷新**不覆盖脏草稿**，只立 `draftStale` 旗子（裁定 162）：否则一条
  `system.persona_changed` 会把正在写的半屏字抹掉。面板提示「人物已变，你手上这份是旧的」，
  由人来决定丢弃还是继续。
- 事件广播挂在 `PersonaStore.subscribe()` 上，**不在写入口顺手声明**（裁定 159）：
  改人物有三条路径（面板 / CLI `studio persona use` / 手改 YAML），只有第一条走我们的写入口。
  挂在订阅上，三条路径都经过同一个 `_load_locked`，一条都漏不掉。

### 4.5.9 合成配置 REST 面契约（T4.7 · 已落地）

> 合成配置面板回答的是「这一档片子**长什么样、按什么参数出**」：分辨率 / 帧率 / 质量（CRF 还是 CQ）、
> 固定水印、字幕样式。它改的是 `config/outputs.yaml` —— 渲染编译器（T3.x）与发布前校验（T5.1）
> **读的都是这一份文件**。所以这一面的重点不是「能改」，而是**改不出坏配置**（§04.2.8 / R17）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/outputs` | **一次拿全**：profile 列表 + 水印（含 `exists` / `width_px`）+ 字幕 + 表单上下限 |
| POST | `/api/v1/outputs` | 保存表单（**先校验、后落盘、再回读**）；`changes` 只交**显式给了**的字段 |

**八条不变量**

1. **文件是唯一真相，面板不写表**（与裁定 165 同源）。消费方读的是 `config/outputs.yaml` 这个**文件**；
   面板写 DB 会让「表说 A、文件说 B」，而真正出片的是文件。
2. **没有 `/validate` 端点**。保存本身就**先校验后落盘**：再开一个「只校验」的端点，等于同一套判定跑在
   两条路径上 —— 两条路径的差异就是「面板说没问题、保存却 422」的来源。**真判定只有一次**，在保存那一下。
3. **上下限与枚举跟着响应下发**（裁定 161）。`limits` 从 `EncodingProfileConfig` / `WatermarkConfig` /
   `SubtitleConfig` 的 `model_fields` **现取**（含 `exclusive_min` —— `width_ratio` 是 `gt=0.0`，画成
   「最小 0」会放行一个必然 422 的 0）；水印位置的**枚举**同样现取，前端不抄第二份。
4. **配置坏了也返回 200**。`stale` / `error` 带原因、`profiles` 为空，但 `limits` **照常下发**
   （它来自模型，不是文件）—— 面板仍能画出表单骨架，用户才修得回来（与 §04.5.7 裁定 150 同一条）。
5. **写盘走行级替换，不用 `yaml.safe_dump`**（裁定 166）。这份文件每一行都带理由
   （`# ★ 水印是必做项（D5）`、`# 上限 0.25（T3.2 夹取）`），整份重写等于「点一次面板就永久毁掉可读性」。
   逐行只换冒号右边的标量：缩进 / 键名 / 行尾注释 / 行尾换行符**一个字节都不动**（`core/yaml_lines.py`）。
   代价如实说明：**改不了块与序列**（增删一行不是行级替换能干的事），面板一期也不编辑它们。
6. **质量参数是抽象字段**（裁定 167）。`quality` 按 `vcodec` 落到 `crf`（libx264）或 `cq`（NVENC）；
   翻译函数 `quality_field_of()` **公开**给服务层复用，响应里带 `quality_field` 让面板把标签写成
   「CRF」或「CQ」。抄第二份就会出现「面板说 CQ、写进 crf」。
7. **并发编辑用 `source_sha256`**（裁定 169）：面板提交时带上「我打开时那一份」的指纹，与盘上现状
   不符 ⇒ **409 `OUTPUTS_STALE`**（状态问题，不是入参问题），**一个字节都不写**。手改 YAML 也算一条
   路径 —— 判据在**文件**上，不在进程缓存里。
8. **`changed=false` 是如实回答**：值与盘上完全一致 ⇒ 不写盘、**版本号不 +1**、不留痕
   （否则审计表会被一串「什么都没改」的记录刷满）。

**审计与日志**

| 动作 | `audit_ops.action` | `target_id` | 其余 |
| --- | --- | --- | --- |
| 保存表单 | `outputs.update` | **配置文件的绝对路径** | `target_type="outputs"`、`actor="user"`、`source="webui"` |

`before` / `after` 放 `{sha256, version, values}`，其中 `values` 是**被改字段**的旧值 / 新值
（`{"subtitle.font_size": 64}`）。这份配置**没有备份目录**（与人物库不同）⇒ `before` 就是唯一能查到的
「改之前是多少」。日志走 `system_logs.source='outputs'`（§04.5.2），payload 里**不带 `kind`**
（带的话 Hub tail 会再广播一遍）。

**前端三个取舍**

- **WS 不新增事件**：面板靠 `logs` 通道里 `source === "outputs"` 的那条帧自己刷新。§04.4.3 的事件表是
  **被契约测试解析的契约**，为一次配置保存新增一种事件不划算；而保存本来就会记一行 `system_logs`，
  「谁改了这份配置」这条信息一条都不少。CLI / 另一个标签页改了配置，走的是同一条路。
  `web/src/stores/outputs.ts` 的 `OUTPUTS_LOG_SOURCE` 由 `tests/contract/test_web_contracts.py` 锁死与后端同源。
- **刷新不覆盖脏草稿**（与裁定 162 同源）：只立 `draftStale` 旗子。而「脏」必须拿**旧**快照判 ——
  赋值之后再算，任何「底稿 == 旧服务端状态」都会被误判成脏草稿，用户一个键都没敲却被扣一顶帽子。
- **保存成功后强制回填**：`sha256` 变了，草稿的基线必须跟着换，否则下一次保存必然 409。

**一期不做**（R17）：三层模板树（Video → Scene → Component）的结构编辑、水印拖拽定位、
`/templates/*` 三个端点。一期这一屏只编辑**标量**；模板结构仍按 `templates/<id>/` 下的 YAML 走。

---

### 4.5.10 无人值守守护 REST 面契约（T4.11 · 已落地）

> 守护是**没有页面的那个子系统**：平时什么都不说，出事时才动手。所以这一屏要回答的不是
> 「怎么操作」，而是「**它还在干活吗**」—— 原文 §1.1⑦ 的无人值守（§01.2.1 / §03.7.5）落到
> 面板上就是这两个端点。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/watchdog` | 守护当前状态：守谁（逐进程）/ 窗口内自愈几次 / 谁已停手 / 门禁水位与人工覆盖 / **人工池里有哪几条** |
| POST | `/api/v1/watchdog/tick` | **人工触发一轮**（跑的是**同一个** `tick()`），写 `audit_ops`；守护停用也照跑照留痕 |

**六条不变量**

1. **触发一轮是 POST，不是 `GET /tick`**（裁定 170）。它会**改状态**：拉起进程、暂停池、把任务推进
   人工池。用 GET 的话，浏览器预取、链接预览、甚至一次误粘贴的地址栏都会真的重启一个 worker ——
   把「能改状态的动作」放进 GET，是这类事故最常见的入口。
2. **路由是 `def` 而不是 `async def`**（裁定 171）。`tick()` 是**阻塞**的（等进程就绪、读进程表、
   写库）。`def` 路由跑在 Starlette 的线程池里，不占事件循环 —— 否则一次重启探测会把整个
   `/ws/ui` 推送卡住。
3. **守护守不了自己**（裁定 172）。跑在 API 进程里的守护**恒不重启 `api`**，报告里如实标
   `guarded=false` + `detail` 指向 `ops/start_all.ps1`。画一个「守护中」的绿灯等于撒谎（P4）：
   `guarded=false` 的两种含义（守护者自己 / 不在守护名单里）靠 `detail` 分开。
4. **没数据 ≠ 正常**（裁定 173）。`disk_gate.low = null` 表示**还没采过资源**，与 `false`（水位正常）
   是两回事；`services=[]` + `last_tick_at=null` 表示「一轮都没跑过」，不是「全都健康」。
   把两者画成同一格，用户会在磁盘爆掉那一刻才发现门禁从没工作过。
5. **`runnable` 与 `enabled` 分开报**（裁定 174）。`enabled` 是配置（`pools.yaml → watchdog.enabled`），
   `runnable = enabled && workers/ 在`。临时家目录（集成测试）里 `workers/` 不在 ⇒ 周期泵**不起跳**，
   守护不会在后台悄悄改 `tasks` / `jobs` —— 这条既是省事也是安全（守护类 flaky 的来源就这一条）。
6. **停用也照跑照留痕**（裁定 175）。`config/pools.yaml` 读不到 ⇒ `enabled=false` + `detail`，
   但 `POST /tick` 仍回 **200** + `note`（「守护已停用…本轮没有做任何事」）并写 `audit_ops`：
   审计要回答的是「谁在什么时候动过这台机器」，不是「这个动作有没有效果」。

**`GET /api/v1/watchdog` 的字段**

| 字段 | 含义 |
| --- | --- |
| `enabled` / `runnable` | 配置开关 / 现在起一轮有没有意义（不变量 5） |
| `self_name` | 守护者自己所在的进程名（`api`） |
| `tick_sec` | 周期（`watchdog.tick_sec`，缺配置时为内置默认） |
| `manual_pool_after` | 进人工池的阈值 —— **与人工池在同一个响应里**，面板不必去翻 YAML（停用时为 0） |
| `services[]` | 逐进程：`guarded` / `running` / `pid` / `restarts` / `halted` / `next_restart_at` / `detail` |
| `halted` | 因**重启风暴上限**停手的进程（超限停手 + 告警，不无限重试） |
| `restarted_total` | **窗口内自愈总次数**（各进程 `restarts` 之和），不是上一拍重启了几个 |
| `disk_gate` | `low`（`null` = 没数据）/ `enabled` / `applied` / `released` / `skipped` / `overridden` / `detail` |
| `manual_pool[]` | 人工池任务：`task_id` / `title` / `status` / `attempt_count` / `retry_from` / `error_code` / `stage_detail` / `updated_at`（最多 20 条） |
| `detail` | 守护整体停用的原因（`pools.yaml` 读不到） |

`restarted_total` 取**窗口内总次数**而不是「上一拍重启了几个」：后者在时间轴上几乎恒为 0
（重启是稀疏事件），用户点开面板看到的永远是 0，于是「这台机器今天自愈了几次」这个问题永远
得不到回答。

**人工池为什么必须可见**（P4）

`attempt_count ≥ manual_pool_after` 的失败任务由守护清扫成 `manual_pool`（§03.5.1）。它**不再
自动重试**，所以这是全系统唯一一处「机器停下、等人」的状态：看不见它，任务就是静默死亡。
面板必须给出三样 —— **卡在哪一步**（`stage_detail`）、**为什么**（`error_code`）、**从哪继续**
（`retry_from`）。进池时错误码**原样带过去**（裁定 176）：`manual_pool` 本身就是错误态
（`_ERROR_STATUSES`），「离开失败态就把错误码清零」那条规矩管的是「重新跑」，不是「停在这里等人」。
`error_message` 全文**不下发**（它在 `/api/v1/logs` 与任务详情里各有一份）：这一屏是常驻页面，
把长文本塞进响应里只会让 5s 一拍的载荷白白变大。

**审计与日志**

| 动作 | `audit_ops.action` | `target_id` | 其余 |
| --- | --- | --- | --- |
| 人工触发一轮 | `watchdog.tick` | —（`target_type="watchdog"`） | `actor="user"`、`source="webui"`、`reason` 来自请求体（`clean_reason`）、`before={enabled}`、`after={restarted,halted,recovered,disk_gate,sweep}` |
| 自动重启一个进程 | **无** | — | 只走日志：`source="watchdog"`、`code=SERVICE_RESTARTED`，payload 带 `service` / `restarts` / `backoff_ms` / `landed` |
| 磁盘门禁暂停 / 放开 | `pool.pause` / `pool.resume` | 池名 | `actor="auto"`、`source="auto"`（走 §04.5.6 那个写入口，不另开一条路径） |
| 人工覆盖门禁 | **无（推导）** | 池名 | 判据是留痕里**相邻**的两条 `[pool.pause(auto), pool.resume(非 auto)]`；水位恢复时写 `pool.override_cleared` **自清**（否则覆盖成了永久豁免） |
| 并发自动回升 | `pool.autorecover` | 池名 | `actor="auto"`、`source="auto"`；它是**唯一**的冷却锚点（`_concurrency_anchor` 读它决定还能不能再涨） |

自动重启**不写** `audit_ops`（裁定 177）：它是每 5s 一拍的高频动作，写进永久留痕表只会把它刷满；
证据链在 `system_logs`（有保留期）+ 进程表。审计表留给**人的决定**（人工触发、人工覆盖）与
**稀疏的状态变更**（暂停 / 放开 / 回升）。

`audit_ops.at` 与 `pool_settings.updated_at` **取同一个时刻**（裁定 178）：`set_concurrency(now=)`
给的就是这一次变更的时刻，两处各写一个「现在」会让读留痕的人（并发回升的冷却锚点）按**另一个
时钟**算冷却。缺省仍交给 DDL 的 `strftime('now')`（`COALESCE(?, strftime(...))`），行为不变。

**前端三个取舍**

- **WS 不新增事件**：守护的状态变化本来就会写 `system_logs`（`source='watchdog'`），面板靠 `logs`
  通道里那一帧重拉 `GET /api/v1/watchdog`。§04.4.3 的事件表是**被契约测试解析的契约**，
  为一次自检新增事件不划算。
- **总览台那一段与独立面板同一来源**（同一个 `WatchdogStatusModel`）：总览台要「一眼扫过」、
  这块要「逐进程核对」，两处口径分叉是这类面板最常见的坏味道。
- **人工池非空 ⇒ 顶部常驻告警条**（不是列表里的一行小字）：「机器停下等人」这件事如果不显眼，
  人永远看不到它。

**一期不做**（R17）：守护阈值热重载（改 `pools.yaml` 仍需重启 API）、重启风暴的**自动解封**
（超限停手后由人重启守护）、进程级资源画像（RSS / 句柄趋势）。

### 4.5.11 观测 / 备份 / GC / 审计 REST 面契约（T4.12 · 已落地）

> 这一屏回答的不是「现在怎么样」（那是总览台），而是**「出事了还有得救吗」**：备份新不新鲜、
> 删掉的旧行有没有把盘还回来、谁在什么时候动过这台机器。原文 §1.1⑦ 的无人值守落到运维面上，
> 就是这三个端点加一套 CLI。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/metrics` | 一次拿全：资源 / 四池 / 今日产量 / 五进程 / **备份新鲜度** / **存储体检** |
| GET | `/api/v1/audit` | 按 `task_id` / `actor` / `action` / `target_type` / `result` / `since` 筛选留痕（新→旧） |
| GET | `/api/v1/audit/facets` | 筛选下拉的取值（`actors` / `actions` / `target_types` / `results`） |

**八条不变量**

1. **观测面只读**。GC / 备份 / VACUUM 都是 CLI 或计划任务的事（`studio gc run` /
   `studio db backup` / `studio db vacuum`）。在面板上给按钮，等于给「删数据」开一个只隔一次
   点击的入口 —— 这一屏一个写动作都没有（`POST /api/v1/metrics` ⇒ **405**）。
2. **备份新鲜度按文件写入时刻算**，不是文件名里那个日期的零点。两者最多差 24 小时：日备
   03:00 产出，按零点算的话上午打开面板会看到「11 小时前」—— 一个刚跑完的任务被报成快半天
   没动。`BackupFile.written_at` 取 `stat().st_mtime`，`day` 只回答「这是哪天的」（轮转用）。
3. **一份都没有也是「旧」**。`count=0` ⇒ `stale=true`、`age_hours=null`：把「没有备份」画成
   「0 小时前」是这块面板最容易犯的谎。
4. **`DELETE` 删了行 ≠ 文件变小**。`db_freelist_bytes = freelist_count × page_size` 由
   `db/engine.py::footprint()` **一处算**（GC 报告 / `db vacuum` / 这一屏是同一个数，三份实现
   迟早让「面板说 0、命令说 120 MB」）。
5. **存储体检按请求跑，不进 5s 轮询**。它是**递归统计文件**（TTS 缓存 / 热点归档 / tmp），
   不是一次 `stat`。代价如实说：这一屏是「要查的时候查一下」，不是实时监控（前端超时给 30s，
   见 `METRICS_TIMEOUT_MS`）。
6. **审计页只读**。写入口散在确认闸 / 池启停 / 策略切换 / 人物库各处（§04.4.4 不变量 2），
   在这里再开一个写入口 = 给「伪造留痕」开条路。
7. **筛选值走仓储白名单 + 占位符**，路由层一条合法性都不校验（`AuditRepo.FILTER_COLUMNS` /
   `FACET_COLUMNS`）；`limit > 500` ⇒ **422**（审计行带 `before` / `after`，比日志行重得多）。
8. **`total` 是同一组筛选下的总数**，不是全表总数 —— 否则「筛出 3 条」会被读成「库里只有 3 条」。

**`GET /api/v1/metrics` 的字段**

| 块 | 字段 |
| --- | --- |
| 四池 | `pools[]`：`pool` / `pending` / `running` / `backlog` / `dead` / `failed` / `paused` / `alive_workers` / `oldest_pending_age_sec`（**不含** worker 明细：那一屏在四池控制台） |
| 今日产量 | `today`：`day`（**本地日**）/ `created` / `completed` / `failed` / `published` / `by_grade` / `by_status` / 窗口两端 |
| 资源 | `resources`（与 `metrics.tick` 同源同形）/ `resources_age_sec` / `disk_low` |
| 进程 | `worker_total` / `worker_alive` / `services[]`（`name` / `ready` / `readiness` / `detail` / `remediation`） |
| 备份 | `backups`：`dir` / `count` / `newest` / `age_hours` / `total_bytes` / `stale`（`stale` 阈值 **48h**） |
| 存储 | `storage`：`db_bytes` / `db_freelist_bytes` / `tts_cache_bytes` / `tts_cache_limit_bytes` / `hot_archive_bytes` / `tmp_bytes` |

前四块**直接复用** `OverviewService.read()` 的同一份数字（同一口径、同一时刻）：抄一遍那四块
的 SQL，就是在给「命令行说 12 GB、网页说 15 GB」这类查不完的悬案交学费。

**运维 CLI 与保留策略**（§03.7.4 / §03.7.5）

| 命令 | 作用 |
| --- | --- |
| `studio db backup` / `db backups` / `db restore --from --to` | 热备（`VACUUM INTO`）/ 列出与新鲜度 / **恢复演练**（还原到临时库 ⇒ `db check` ⇒ 抽查 3 张表行数；**拒绝还原到活库**） |
| `studio db vacuum [--yes]` | 先钉一份 `prevacuum_*.db` 检查点，失败即中止；报告还回去多少字节 |
| `studio gc run [--dry-run\|--rows-only\|--media-only\|--json]` | 行级保留 + 媒资清理（详见下） |
| `scripts/backup_db.ps1` / `restore_db.ps1` / `gc_media.ps1` | 计划任务入口（薄壳，实现全在 Python） |

- **行级保留**：`debug` 7d → `logs` 30d → `llm_calls` 90d → `task_events` 90d；分批 ≤ 5000 行。
  **`audit_ops` 没有规则**（永久）：它是「谁动过这台机器」的唯一账本。
- **媒资**：句子音频 / 母带 24h（**成片不在 ⇒ `skip`**，`SKIP_FINAL_MISSING` / `SKIP_FINAL_UNKNOWN`）、
  `scenes/` 7d 按 mtime、失败任务的 `.partial` 立即清、TTS 缓存 LRU（`use_count ≥ 2` 降权、
  旁车 `.meta.json`）、热点**移动**进 `hot/archive/<YYYYMM>/`（撞名退 `-2`）。
- **唯一删除闸口是 `gc/policy.py::guard_path()`**：越界 ⇒ `GC_PATH_OUT_OF_BOUNDS`；命中白名单
  （`final/` / `output/` / `covers/`）⇒ `GC_REFUSED_PROTECTED`；动作本身失败 ⇒ `GC_ACTION_FAILED`。
  被守卫拒绝时 CLI 以 **exit 1** 收场（计划任务里这就是告警）。
- **恢复演练的 `schema_lag`**：刚上过迁移时，盘上最新备份必然落后一个版本 ⇒ 多出来的失败项若
  全落在 `db.indexes` / `db.tables` / `db.triggers` / `db.migrations`，**且**还原库的迁移集是
  活库的**真前缀**，则记为「备份版本落后」而**不是**红。判据是**双条件**：真被删掉索引的坏库
  照旧报红（「备份只是比活库旧」与「备份坏了」必须分得开）。

**schema 窗口（T4.9 转来的债）**

`0008_observability.sql`：`idx_logs_source`（日志按 `source` 过滤）+ `idx_events_created`
（事件按时间扫）。对象数冻结 **58 → 60 索引**，15 处断言与文档同步。

**前端三个取舍**

- **观测面板不轮询**：备份新鲜度与存储体检是「要查的时候查一下」，自动跳动的数字只会让人以为
  备份任务正被实时盯着（与总览台 10s 健康探针的分工不同）。
- **审计页的筛选不自动查**：文本框改值只改状态，回车 / 点「筛选」才发请求 —— 每一次都是一次
  数据库查询（下拉是选择，选完即查）。翻页**取消在途请求**：连点两下「下一页」时，慢的那个
  后到会把列表按旧 `offset` 重画一遍（表现为「点了下一页，看到的还是上一页」）。
- **`denied` 不是失败**：它是「有人想动、被规则拦下了」，同样是有效留痕，标黄不标红。

**一期不做**：备份的异地副本、GC 的可视化（面板只报数字，动作全在 CLI）、审计的导出。

---

### 4.5.12 素材库 REST 面契约（T4.8 · 已落地）

> 素材库面板回答的是「本机有哪些能用的素材、够不够用、哪一条能不能上」：**跑酷片**
> （背景）、**BGM**、**零样本音色**。它改的是 `broll_clips` / `bgm_tracks` /
> `voice_profiles` 三张表 **+** `data/assets/` 与 `data/voice_src/` 两个目录 ——
> **两处一起才算数**（文件在、行也在）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/assets` | **一次拿全**：三类分节（条目 + 家底 + 缺口）+ 降级判定 |
| GET | `/api/v1/assets/stats` | **只要数字**：三类家底 + 判据线 + 授权枚举（总览台的小卡片用它，不拖整库） |
| POST | `/api/v1/assets/ingest` | 扫盘 / 入库；`dry_run=true` ⇒ **只读预览，一个字节都不写库** |
| PATCH | `/api/v1/assets/{id}` | 改一条（启停 / 授权 / 标签 / 可用区间 / 情绪…） |
| GET | `/api/v1/assets/{id}/thumb` | 缩略图（跑酷抽帧图；没有 ⇒ 404，**不临时现抽**） |
| GET | `/api/v1/assets/{id}/media` | 原文件（BGM 试听 / 跑酷预览；音色 ⇒ 422） |

**十条不变量**

1. **文件 + 表两处一起才算数**。`broll_clips.path` 指向的文件被挪走 ⇒
   `GET /media` **404 说清路径**，而**库里那行照旧** —— 我们不会替你删记录。
   反过来说，「面板上有这条、播放器打不开」是**如实反映**，不是 bug。
2. **扫盘与入库是同一条路径的两个模式**（`dry_run` 开关）。分成两个端点会让两条路径
   各写一遍「怎么判定一条素材合格」—— 而它们的差异，恰恰就是「预览说能进、真入库
   却被拒」的来源。判定只有一份（`assets/validate.py`），跑两次而已。
3. **`license` 只对本次新入库的条目生效**。已入库的按**行里存的那份**走 —— 否则一次
   「顺手全扫」会把每条素材的授权都改成本次请求里填的那个（那是伪造 R2 留痕）。
4. **缺授权 ⇒ 拒绝入库**，且逐条说清是哪一条缺（`check.problems[].code =
   license_missing`）。**不替用户填一个 `self_recorded`** —— R2 合规留痕的意义就在于
   它记的是人的声明。
5. **坏文件不中断整批**：0 字节 / 截断 / 扩展名骗人的那一条标红、写清原因，其余照常入库。
   让整批扫描停在第一个坏文件上，等于 59 条好素材白扫。
6. **只允许停用，不物理删除**（§T4.8 硬约束）。面板上没有「删除」按钮，这不是「还没做」：
   误删一柜子素材不可逆，而停用随时能点回来。真要腾空间，是用户在资源管理器里的决定。
7. **重扫只刷新机器事实**（陷阱 #92）：`upsert` 更新 `sha256` / 时长 / 宽高 / 帧率 /
   指纹，**绝不覆盖** `enabled` / `license` / `tags` / `usable_*` / `has_text` / `mood` / `bpm`
   —— 那些是人填的。反过来说，「重扫之后我标的可用区间没了」是一条**不该存在**的 bug。
8. **判据线与授权枚举跟着响应下发**（与裁定 161 同一条）。`thresholds`
   （`broll_min_clips=60` / `broll_min_duration_ms=30min` / `bgm_min_duration_ms=15s` /
   `voice_*`）从 `assets/validate.py` 现取，`licenses` 从 `asset_service.LICENSES` 现取；
   某天把「跑酷 ≥ 60 条」调成 80，面板的进度条跟着变，前端一行都不用改。
9. **`strays` 如实报出来**：目录里存在、但命名不合规的文件（`跑酷1.mp4`、嵌套子目录）
   进 `strays` 列表，**绝不悄悄忽略** —— 用户把文件丢进去，看到的应该是「它没被认出来，
   改名成 `parkour_xxx.mp4`」，而不是「扫了 0 条」。
10. **音色认不出 ⇒ 明确报错，绝不静默换默认音色**。`persona` 的 `tts_voice_id` 只做**格式**
    校验（存在性不查），真正解析不到时抛错并给出「这个 id 该在哪个目录、目录里该有什么」。

**审计与日志**

| 动作 | `audit_ops.action` | `target_id` | `before` / `after` |
| --- | --- | --- | --- |
| 启用 / 停用 | `asset.enable` / `asset.disable` | 素材 id | `{"kind": "broll", "enabled": true → false}` |
| 改字段 | `asset.update` | 素材 id | **只放被改的字段**（`{"license": "cc0" → "purchased"}`） |

- `target_type="asset"`、`actor="user"`、`source="webui"`（脚本 / CLI 走 `actor="system"` + `source="cli"`）。
- 留痕里带 `kind`：同一个 id 在不同类里出现时，审计页要能分清是哪一类。
- **幂等不留痕**：状态本来就对（重复点同一下停用）⇒ 不写库、不记审计。否则审计表会被
  一串「什么都没改」的记录刷满。
- 日志走 `system_logs.source='assets'`（§04.5.2），payload 里**不带 `kind`**（带的话 Hub tail
  会再广播一遍）。入库与启停各记一行；`dry_run` 的预览**不记**（它什么都没干）。

**前端三个取舍**

- **WS 不新增事件**（与 T4.11 / T4.7 同一条裁定）：面板靠 `logs` 通道里
  `source === "assets"` 的那条帧自己刷新，合并窗口 300 ms（一次全扫会连着写几行）。
  `web/src/stores/assets.ts` 的 `ASSETS_LOG_SOURCE` 由 `tests/contract/test_web_contracts.py`
  锁死与后端同源。
- **预览 / 试听交给浏览器**：`<img src>` / `<audio src>` 直接取 URL（浏览器自己做范围请求、
  缓存与并发控制），不走 `fetch` + objectURL —— 那会把这些全丢掉，还要自己管生命周期，
  并且「刷新时音频被掐断」。但**路径只从库里取**（`asset_id` 是白名单正则
  `^[a-z0-9][a-z0-9_\-]{0,63}$`），前端不拼任何文件路径。音色是**目录**，不给播放器。
- **占位素材标黄**：`tags` 里的 `placeholder`（由 `scripts/seed_placeholder_assets.py` 写入）
  ⇒ 面板上挂一个黄色的「占位」角标。**绝不假装成正式素材** —— 否则「这条片子为什么这么
  难看」会变成一条查不完的悬案。音色没有 `tags` 列，它的占位标记写在目录里的
  `profile.json`（`origin: generated`），面板**如实不显示**，而不是拿别的信号去猜。

**一期不做**

- **真正的 multipart 上传**：一期是「把文件丢进目录 + 点扫盘入库」。上传要做分片、
  断点续传、去重与授权表单，而它换来的只是「不用开资源管理器」—— 优先级低于把链路跑通。
- **素材裁剪 / 转码 / 自动切片**：那是 T3（渲染编译器）与素材准备的活，不是素材库的。
- **帧哈希 / 感知哈希防搬运**：T3 的随机化策略负责「不重复用同一段」，素材库只负责
  「这条能不能用」。列已经有了（`broll_clips.phash` / `frame_hashes`），填充留到 T3。

---

### 4.5.13 配音面板 REST 面契约（T4.5 · 已落地）

> 配音面板回答的是「这条任务配到哪一步了、哪一句出了岔子、想换一个人的嗓子怎么办」。
> 它**不新增**任何后端能力 —— 五个端点全部来自 §4.3.7；这一节写的是**面板怎么用它们**，
> 以及前端必须守住的四条（每一条都对应一个已经踩过的坑）。

| 面板上的动作 | 端点 | 什么时候发 |
| --- | --- | --- |
| 看逐句状态 | `GET /api/v1/sentences?task_id=` | 首屏 + 轮询 1s（**只在有活时**） |
| 看有哪些音色 | `GET /api/v1/voices?task_id=` | 首屏 + 手动刷新（音色清单不会因为某一句念完而变） |
| 试听某一句 | `<audio src=audio_url>` ⇒ `GET /api/v1/media/{path}` | 用户点播放键，浏览器自己发 Range 请求 |
| 重配某一句 | `POST /api/v1/sentences/{id}/resynth` | 动作一次 + 随后轮询看它什么时候念完 |
| 换音色 | `PATCH /api/v1/tasks/{id}/voice_map`（**同一个端点调两次**：先不带 `confirm` 探代价，再带 `confirm=true` 真写） | 用户点「提交」+ 在确认框上点头 |

**前端必须守住的四条**

1. **`audio_url` 由服务端给，没有音频时是 `null`**（裁定 244）。`null` ⇒ 那一行**不画播放键**。
   面板**不自己拼** url：它指的是盘上那份文件，而"文件落在哪"是后端的事（§2.3 一改目录就是
   "点了播放没反应"，且不报错）；发一个注定 404 的 url 同样如此。
2. **换音色先问代价再动手**（裁定 245）。不带 `confirm` 的那一次是**探测**：服务端要么直接
   写完（`affected=0`，没有句子要重配），要么抛 409 `VOICE_MAP_CONFIRM_REQUIRED`。面板拿
   `context.affected / total / sentences / speakers` 弹确认框，用户点头才带 `confirm=true` 重发。
   **不要在本地自己算"会重配几句"** —— 那是服务端的规则（`REINVALIDATABLE_STATUSES`）。
3. **只提交真正改过的角色**（裁定 249）。服务端校验的是"提交里出现的每一个音色"（§4.3.7 的
   两条线），把整张表发上去等于顺手替用户断言了他没碰过的那些行 —— 而其中可能正躺着一条
   本机找不到的存量值（陷阱 #124）。
4. **`busy` 与 `timeline_stale` 都要显示**（裁定 246）。`busy` 是"想失效但没动成"的那几句
   （正被某个 worker 念着），不显示就是"换音色成功、成片里那几句还是旧嗓子"；
   `timeline_stale=true` 说的是"盘上那份时间轴不是现在这条片子"（重配不重算，裁定 241）。

**为什么是轮询而不是 WS**（裁定 247 / 248）

§04.4.3 有 `sentence.updated` 这条事件，但后端**目前没有生产者**（只有 `metrics` 与
`persona` 两路会 `publish`）。面板用 1s 轮询（与渲染面板同口径，`VOICE_POLL_MS`），并且
**只在 `pending + synthesizing > 0` 时开**：失败与跳过都是**定局**，不会自己变，不该让定时器
空转（陷阱 #126）。真要做推送，落点是"给配音收口加一路 publish"，而不是把轮询周期调小。

**`skipped` 句要整行染色 + 原因写在行上**（陷阱 #125 的同族）。跳过是**降级**：这一句没声音，
片子照样出 —— 只给一个计数，用户永远不知道是哪几句、为什么，而原因（`tts_error`）就躺在库里。

### 4.5.14 四屏端到端串联契约（T4.14 · 已落地）

> 「选题 → 稿件 → 配音 → 渲染」四屏各自都能单独用，而**串起来的那一步**（把任务号带过去）
> 既不属于任何一屏，也不该由后端出接口 —— 它是一次**纯前端跳转**。这一节写的就是它的语义，
> 以及它**刻意不做**的两件事。

| 从哪一屏 | 按钮 | 带到哪一屏 | 任务号从哪来 |
| --- | --- | --- | --- |
| 选题 | 「去稿件 →」（只在入队过的卡片上） | 稿件 | `TopicItem.task_id`（**入队时后端生成**，前端不猜） |
| 稿件 | 「去配音 →」/ 「去渲染 →」（详情行尾） | 配音 / 渲染 | `ScriptDetail.task_id` |
| 配音 | 「去渲染 →」（任务卡） | 渲染 | 面板上填的那个（空 ⇒ 按钮是**灰的**） |
| 渲染 | 「去配音 →」（表单旁 + 最近任务每行） | 配音 | `RenderJob.task_id`（**反向跳转**） |

**跳转只携带两个字段**：`panel`（去哪一屏）+ `taskId`（带哪个号）。落在 `stores/ui.ts`：

- `goTo(panel, taskId)`：规范化任务号（空 / 全空白 ⇒ **不跳**，返回 `false`）⇒ 记一笔 ⇒ 切面板。
- `takeHandoff(panel)`：**目标面板挂载时**认领属于自己的一笔；认领即清空。
- 人自己点侧边栏（`selectPanel`）⇒ 顺手清掉待认领的那一笔。

**契约要求（三条，每一条都对应一个坑）**

1. **外壳不许知道业务**（裁定 253）。跳转不带任何业务参数、也不预判目标面板的状态。
   面板是 `v-if` 挂的，切过去必然重新挂载，所以“用上这个任务号”写在**目标面板的 `onMounted`**。
   外壳一旦开始知道“配音面板要拉逐句”，四屏就绑死了。
2. **先拉列表、再认领跳转**（裁定 254 / 陷阱 #128）。稿件面板的 `refresh()` 会把“不在当前
   状态列表里”的选中项清掉，配音面板的 `setTaskId()` 会清掉上一条任务的快照 —— 顺序反了，
   跳转会被自己的首屏覆盖掉，**而且不报错**。
3. **认领即清空**（陷阱 #129）。留着的话，用户从配音点回选题、再点回配音，会被同一个
   任务号再跳一次 —— 那时他多半是想看别的。

**刻意不做的两件事**

- **不自动出片**：跳到渲染面板只**填框**。“出片”是花钱花时间的那一步，必须由人按下去。
- **不改 URL**：一期是本地单页控制台，跳转不写路由（`?task_id=`）—— 多标签页 / 刷新恢复
  属于后续版本的事，现在写进去只会多一个“URL 与面板状态谁是权威”的问题。

### 4.5.15 发布前准备契约（T5.1 · 已落地）

> 这一节写的是**发布之前**那两步：把封面画出来、判"能不能发"。
> 它**不发布** —— `publish.enabled=false` 是出厂状态（R14 不可逆防护）。

#### 命令面

```text
studio publish cover    --task <id> [--no-agent] [--json]
studio publish precheck --task <id> [--json]
```

| 命令 | 退出码 | 做什么 |
| --- | --- | --- |
| `cover` | `0` = 封面出来了；`1` = 没出来 | 读 `timeline.json` 算抽帧点 ⇒ Cover Agent 写文案（可 `--no-agent` 跳过）⇒ 合成 1080×1920 JPEG ⇒ 落 `data/output/covers/{yyyymmdd-HHMMSS}_{task_id}_cover.jpg` |
| `precheck` | `0` = 可以发；`1` = **拒绝发布** | 三道门禁 + 禁区扫描 ⇒ 不通过时打出 `manual_required` 与 `error_code`（`PRECHECK_*`） |

⚠️ **`cover` 失败不阻塞发布**（§06.3：无封面就用平台首帧）—— 退出码 1 只表示"这张封面没出成"。

#### 封面（§06.3）

| 项 | 值 |
| --- | --- |
| 画布 | 1080×1920，JPEG `q=3` |
| 抽帧点 | `timeline.json` 第一句 `start_ms + 500`；**模型可覆盖**，但一律钳在 `[0, duration_ms-1]` |
| 排版 | 主文案 ≤2 行 × ≤10 字（字号 96，超安全宽按实测宽度线性收缩，**下限 60**）；次文案 1 行 × ≤12 字（52）；文字块底部对齐、强制落在安全区 `[230, 1632]` 内 |
| 高亮 | `drawtext` 没有富文本 ⇒ 整行先画一遍，高亮段**重叠**在同一个 x 上（`0xFFD400`） |
| 可读性 | 文字区背后一条 `drawbox=black@0.45` 压暗带（按文字块高度，不铺满全屏） |
| 降级 | 抽帧失败 ⇒ 纯色底 + 文字（`warn`）；连纯色底都出不来 ⇒ **无封面发布**（`path=None`，**不抛**） |
| 留痕 | `tasks.context_json.cover_path`（失败写 `null`）+ `context_json.cover_plan` + `artifacts(kind='cover')` |

#### 发布前二次校验（§06.4）

| # | 门禁 | 判据 | 不过 |
| --- | --- | --- | --- |
| 1 | 水印 | `quality_json.watermark_applied == true`（`None` ⇒ 读 `manifest.json.watermark.enabled`；两边都没有 ⇒ **不过**） | **拒发** ⇒ `PRECHECK_WATERMARK` |
| 2 | 响度 | **重量盘上那个成片**：`lufs ∈ [-16.5,-15.5]` 且 `true_peak ≤ -1.0`；量不出来 ⇒ **不过** | **拒发** ⇒ `PRECHECK_LOUDNESS` |
| 3 | 相似度 | `quality_json.dup_audit_pass` | 只 `warn`（`precheck.block_on_similarity=false`）⇒ `PRECHECK_SIMILARITY` |
| 4 | 禁区 | `persona.forbidden` + `prompts/shared/banned_words.yaml` | `title`/`caption` 命中 ⇒ **拒发**（`PRECHECK_BANNED`）；`tags` 命中 ⇒ **剔掉该 tag + warn** |

- `av_sync_offset_ms` **只进 `diagnostics`**（C12 取消了硬门禁）。
- `GateResult` 的两个字段分工固定：`passed` 报**审计结论本身**，`blocking` 报**要不要拦**。
- 门禁顺序固定（水印 → 响度 → 相似度 → 禁区）；`error_code` 取**第一道**没过的阻断门禁。

#### 成片路径的取法

`manifest.json` 的 `final` 字段**优先**（同一个任务可以有多个 `*_{task_id}_final*.mp4`，重合成一次多一个）；
manifest 缺失或它指的那条不在盘上 ⇒ 退回目录里按名字找**最新**的一条。两个来源都没有 ⇒ 没有成片。
## 4.6 发布与数据回流契约（第六部分重建 · 原文 §1.1⑤ / §8 / §9.3）

> 本节定义**接口与签名**；子系统的行为、平台矩阵、风控与合规策略见 **§06 成片与发布**。

### 4.6.1 `Publisher` 抽象（平台实现可替换 · A1 多平台）

```python
class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    platform: Literal[
        "douyin",
        "kuaishou",
        "shipinhao",  # 一线（一期必做）
        "xiaohongshu",
        "bilibili",
        "xigua",
        "weibo",  # 二线（一期仅接口）
        "other",
    ]
    account_id: str
    video_path: Path
    cover_path: Path | None = None
    title: str = Field(max_length=55)  # 平台标题长度上限（profile 可收紧）
    caption: str = Field(max_length=2000)  # 发布文案（含话题标签）
    tags: list[str] = Field(default_factory=list)
    scheduled_at: str | None = None
    dry_run: bool = False  # ★ 演练模式：走完流程但不真正发布


class PublishEvidence(BaseModel):
    screenshot_path: Path | None = None  # 失败/成功截图（取证，R13）
    dom_snapshot_path: Path | None = None  # 页面 DOM 快照（选择器失效排障）
    stderr_tail: str | None = None  # 截断 8KB
    selector_version: str | None = None  # selectors/*.yaml 的版本（页面改版后定位）
    platform_text: str | None = None  # 平台返回的提示文案原文


class PublishResult(BaseModel):
    ok: bool
    status: PublishStatus
    url: str | None = None
    platform_post_id: str | None = None
    published_at: str | None = None
    error_code: str | None = None
    # 'PUBLISH_LOGIN_EXPIRED' | 'PUBLISH_RATELIMIT' | 'PUBLISH_SELECTOR_MISS'
    # | 'PUBLISH_UPLOAD_FAILED' | 'PUBLISH_REVIEW_REJECTED' | 'PUBLISH_TIMEOUT' | 'PUBLISH_UNKNOWN'
    error_message: str | None = None
    evidence: PublishEvidence | None = None
    elapsed_ms: int


class PublishMetrics(BaseModel):
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    collected_at: str


class PublishHealth(BaseModel):
    ready: bool
    logged_in: bool
    account_name: str | None = None
    last_check_at: str
    hint: str | None = None  # '需人工扫码登录' | '登录态已过期'


class Publisher(ABC):
    """平台差异**只**允许出现在三个地方：① selectors/*.yaml ② profile ③ 本类的子类实现。"""

    platform: ClassVar[str]
    selectors_version: ClassVar[str]

    @abstractmethod
    async def health(self) -> PublishHealth: ...  # 登录态探测（**不自动登录**）
    @abstractmethod
    async def publish(self, req: PublishRequest) -> PublishResult: ...
    @abstractmethod
    async def fetch_metrics(self, platform_post_id: str) -> PublishMetrics: ...
```

**平台实现注册表**

```python
PUBLISHERS: dict[str, type[Publisher]] = {
    "douyin": DouyinPublisher,  # 一期
    "kuaishou": KuaishouPublisher,  # 一期
    "shipinhao": ShipinhaoPublisher,  # 一期
    "xiaohongshu": XiaohongshuPublisher,  # 二期（接口已定，实现可空）
    "bilibili": BilibiliPublisher,  # 二期
    "xigua": XiguaPublisher,  # 二期
    "weibo": WeiboPublisher,  # 二期
}
```

### 4.6.2 发布流水线契约（原文 §8：快速封面 → 成片审核 → 自动发布 → 数据回收 → 记忆沉淀）

```
rendering → completed
   ↓
① 快速封面（publish/cover.py）
   ffmpeg 抽帧（CoverOutput.frame_at_ms）+ drawtext 叠加标题
   → data/output/covers/{时间戳}_{task_id}_cover.jpg（1080×1920）
   ↓
② 成片审核（复用 §4.2.5 审计 + 发布前二次校验 · R14）
   ├─ 三道门禁：音画同步 / 响度 / 相似度（§4.2.4.5）
   ├─ 标题/文案禁区词扫描（persona.forbidden + banned_words.yaml）
   └─ publish.require_confirm=true ⇒ 进"发布前确认闸"（复用 §4.4.4 机制）
   ↓
③ 自动发布（publish/playwright_publisher.py）
   ├─ 限频检查（≤3 条/天/账号，间隔 ≥30min · R13）
   ├─ 登录态检查（失效 ⇒ manual_required，**不自动登录**）
   ├─ 上传 + 填标题/文案/话题 + 选封面 + 发布
   └─ 成功 ⇒ publications(status='published', url=...)
      失败 ⇒ 重试 ≤3（指数退避）⇒ 仍失败 ⇒ manual_required + 告警
   ↓
④ 数据回收（publish/metrics.py，定时）
   T+1h / T+6h / T+24h / T+72h 采集 views/likes/comments/shares
   → publications.metrics_json + metrics_history_json
   ↓
⑤ 记忆沉淀（publish/memory.py）
   ├─ 高互动评论 → feedback_items(is_auto=1, sentiment=…)
   ├─ 低互动选题 → topic_candidates 同类降权（≤20%）
   └─ 汇总进 data/feedback/auto_YYYYMM.md ⇒ 下一轮 Planner 消费（闭环）
```

**接口签名**

```python
async def publish_task(task_id: str, platforms: list[str], *, dry_run: bool) -> list[PublishResult]: ...
async def collect_metrics(publication_id: str) -> PublishMetrics: ...
async def sink_memory(publication_id: str) -> MemorySinkResult: ...


class MemorySinkResult(BaseModel):
    feedback_items_created: int
    topics_demoted: int
    digest_path: Path  # data/feedback/auto_YYYYMM.md
    planner_consumable: bool  # ★ 必须为 True（T5.4 闭环验收）
```

### 4.6.3 `HandoffAdapter`（对接外部"制片台既有设计" · A2）

```python
class HandoffPayload(BaseModel):
    """交给外部系统的自包含交付包（不依赖本系统在线）。"""

    task_id: str
    title: str
    final_video: Path
    cover: Path | None
    subtitle_ass: Path | None
    script_md: Path
    timeline_json: Path
    manifest_json: Path
    quality: QualityReport
    license_notes: str | None = None


class HandoffAdapter(ABC):
    """默认实现 = LocalHandoffAdapter（复制到 data/handoff/ 并写 audit_ops）。
    外部对接 = 实现本 ABC 并在 config/app.yaml 的 handoff.adapter 指定。"""

    @abstractmethod
    async def push(self, payload: HandoffPayload) -> HandoffResult: ...
    @abstractmethod
    async def health(self) -> bool: ...
```

> **默认行为**：`handoff.enabled=false`。开启后，任务 `completed` 时自动 `push`；失败**不影响**主流程（记 `warn` + `audit_ops`）。

### 4.6.4 发布硬约束（契约层强制，实现不得绕过）

| 约束 | 值 | 理由 |
| --- | --- | --- |
| 默认是否发布 | `publish.enabled=false` | 保守起步：先跑通"出片"，确认质量后再开（R14） |
| 发布前确认 | `publish.require_confirm=true` | 发布不可逆（R14） |
| 限频 | ≤3 条/天/账号，间隔 ≥30min | 平台风控（R13） |
| 登录态 | 仅复用持久化 profile；**不自动登录、不绕过验证码** | 合规底线（R13） |
| 幂等 | `sha256(task_id\|platform\|account_id)` 唯一 | 防重复发布（已真机验证唯一键生效） |
| 失败取证 | 截图 + DOM 快照落 `artifacts` | 人工可排查 |
| 数据回收频率 | T+1h/6h/24h/72h | 平衡信息量与请求量 |
| 发布失败不回退任务 | 任务停 `completed`，发布单独重试 | 成片依然可用 |
| 二线平台 | 一期仅保留接口与 profile | R18 控制范围 |

---

### 4.6.5 定时发布与报告契约（口述 D7/D9 + Q14/Q15 · 新增）

#### 4.6.5.1 定时发布调度（`services/scheduler_service.py`）

```python
class PublishScheduleSpec(BaseModel):
    """定时发布计划。到点才创建 publish job —— 调度器不占 worker，job 仍走 §03.4 原子认领。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str | None = None  # None ⇒ 按平台/账号从"待发布池"取
    platforms: list[str] = Field(min_length=1)
    account_ids: list[str] = Field(min_length=1)  # D1：结构支持多账号，默认 1 个
    mode: Literal["at_time", "daily_window", "interval"]
    at_time: str | None = None  # '2026-09-14T19:30:00+08:00'
    window: tuple[str, str] | None = None  # ('18:00','21:30')
    interval_hours: int | None = None
    jitter_min: int = Field(15, ge=0, le=120)  # 时刻抖动，避免"每天准点"的机器特征（R13）
    enabled: bool = True
    created_by: str = "user"


class ScheduleRuntime(BaseModel):
    schedule_id: str
    next_run_at: str  # 持久化，重启后不丢
    last_run_at: str | None
    run_count: int
    last_result: str | None  # 'ok' | 'skipped_ratelimit' | 'error:…'
```

| 项 | 规则 |
| --- | --- |
| 调度器形态 | 单进程、**30s tick**、`next_run_at` 落库（重启不丢） |
| 与队列关系 | 调度器**只负责到点创建 job**；job 的认领/租约/重试全部走 §03.4 |
| 与限频关系 | **叠加**：定时 ≠ 免限频（§03.4.4 的 `≤3 条/天/账号` 仍生效）。被限频 ⇒ `last_result='skipped_ratelimit'` 并顺延 |
| 窗口模式（默认） | `daily_window` 在窗口内随机取时刻 + `jitter_min` ⇒ 比固定时刻更自然（Q14 默认 `18:00–21:30` + 15min） |
| 幂等 | 同一 `(task_id, platform, account_id)` 的发布幂等键仍生效（§03.3.15），**调度不会重复发布** |
| 失败 | 创建 job 失败 ⇒ 记 `warn` + `next_run_at` 顺延 1 个 tick；连续失败 ≥5 次 ⇒ `system.alert` |
| **策略可编辑（Q14）** | 模式 / 窗口 / `jitter_min` / 平台 / 账号 / 启停 **均可在 WebUI 编辑**；`PATCH` ⇒ **同事务重算 `next_run_at`** + 写 `audit_ops`（陷阱 #33：只改参数不重算 ⇒ 不生效或立刻触发） |
| 参数校验 | 窗口必须 `HH:MM` 且 `start < end`；`jitter_min ∈ [0,120]`；`at_time` 必须带时区；非法参数 ⇒ **400 且不落库** |
| 与 `publish.enabled` | `publish.enabled=false` 时调度器**空转**（不创建 job），仅记录 `skipped_disabled` |

**REST 契约**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/schedules` | 列表（含 `next_run_at` / `last_result`） |
| POST | `/api/v1/schedules` | 创建（body = `PublishScheduleSpec`） |
| PATCH | `/api/v1/schedules/{id}` | 修改（时间/平台/启停） |
| DELETE | `/api/v1/schedules/{id}` | 删除（写 `audit_ops`） |
| POST | `/api/v1/schedules/{id}/run_now` | 立即执行一次（调试用，仍走限频） |

**WS**：`publish.scheduled`（`schedule_id, next_run_at, task_id, platforms`）、`publish.schedule_fired`（`schedule_id, job_ids[]`）。

#### 4.6.5.2 报告生成（`services/report_service.py`）

```python
class ReportSpec(BaseModel):
    period: Literal["daily", "weekly", "monthly"] = "weekly"  # Q15 默认周报
    start: str
    end: str
    include: list[Literal["publish", "metrics", "topics", "quality", "cost", "errors"]] = [
        "publish",
        "metrics",
        "topics",
        "quality",
        "cost",
    ]
    format: Literal["md", "md+charts"] = "md+charts"


class ReportScheduleSpec(BaseModel):
    """报告周期（Q15"周期可编辑"）。与 PublishScheduleSpec 同构，共用调度器 tick。"""

    model_config = ConfigDict(extra="forbid")
    period: Literal["daily", "weekly", "monthly"]
    weekday: int | None = Field(None, ge=0, le=6)  # weekly：0=周一（Python weekday）
    day_of_month: int | None = Field(None, ge=1, le=28)  # monthly：≤28 避免月末缺日
    at_time: str = Field("09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    tz: str = "Asia/Shanghai"
    lookback_days: int = Field(7, ge=1, le=366)
    include: list[Literal["publish", "metrics", "topics", "quality", "cost", "errors"]] = [
        "publish",
        "metrics",
        "topics",
        "quality",
        "cost",
    ]
    enabled: bool = True
    is_builtin: bool = False  # 内置计划不可删，只能停用 / 改参数

    @model_validator(mode="after")
    def _check_period_fields(self) -> "ReportScheduleSpec":
        """weekly 必须有 weekday；monthly 必须有 day_of_month（与 DDL 的 CHECK 双保险）。"""
        ...


class Insight(BaseModel):
    """★ 决策建议 —— 报告的价值就在这一层（口述 D9"以便后续决策"）。"""

    kind: Literal["topic", "timing", "platform", "quality", "cost"]
    statement: str  # '数字型钩子在抖音的完播率高于悬念型 23%'
    evidence: dict  # 支撑数据（可追溯到 publications.metrics_json 的聚合值）
    confidence: Literal["low", "medium", "high"]  # 样本量决定（n<10 ⇒ low）
    suggested_action: str  # '下轮选题中数字型占比提升至 40%'


class Report(BaseModel):
    id: str
    period: str
    start: str
    end: str
    generated_at: str
    summary_md: str  # ★ 人类可读 Markdown（WebUI 直接渲染 / 可导出 PDF）
    data_json: str  # 结构化聚合数据（供图表）
    insights: list[Insight]
    artifacts: list[str]  # 图表 PNG / CSV 路径
    task_count: int
    publish_count: int
    total_views: int | None
```

| 项 | 规则 |
| --- | --- |
| 生成方式 | **纯 SQL 聚合 + 规则归因**（**不调 LLM**，避免成本与不确定性）；需要自然语言总结时走 §04.1 Agent 通道（可选开关） |
| 归因维度 | ①选题类型（`hook_type`）②发布时段（`published_at` 的小时）③平台 ④时长 ⑤稿件评分（`grade`/`score_total`）⑥TTS 质量（`skipped` 率）⑦成本（`llm_calls` 汇总） |
| 置信度 | 样本量 `n < 10` ⇒ `low`；`10 ≤ n < 30` ⇒ `medium`；`n ≥ 30` ⇒ `high`。**`low` 的建议必须显式标注"样本不足"** |
| 报告**不调 LLM** | 见上；`statement` 由模板渲染（如 `f"{a} 的 {metric} 高于 {b} {pct}%"`） |
| 决策闭环 | `Insight` 经**人工确认**后写入 `config/persona.yaml` 偏好项 或 `content_directions` 权重 ⇒ 影响下轮 Planner |
| 调度（**周期可编辑 · Q15**） | `report_schedules` 表驱动，与 `publish_schedules` 共用同一个调度器 tick。默认 seed：`weekly`（周一 09:00，回看 7 天）+ `monthly`（每月 1 日 09:00）；`daily` **默认停用**。同周期**仅允许 1 个启用**（部分唯一索引） |
| 留痕 | `reports` 表 + `audit_ops(report.generate)`；报告文件永久保留 |

**REST 契约**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/reports?period=&limit=` | 报告列表 |
| GET | `/api/v1/reports/{id}` | 详情（`summary_md` + `data_json` + `insights`） |
| POST | `/api/v1/reports/generate` | 手动生成（body = `ReportSpec`） |
| POST | `/api/v1/reports/{id}/insights/{idx}/apply` | ★ 采纳某条建议（写入 persona/方向权重 + `audit_ops`） |
| GET | `/api/v1/reports/{id}/export?format=md\|csv` | 导出 |
| GET | `/api/v1/report-schedules` | 周期列表（含 `next_run_at` / `last_result`） |
| POST | `/api/v1/report-schedules` | 新建周期（body = `ReportScheduleSpec`） |
| PATCH | `/api/v1/report-schedules/{id}` | ★ **编辑周期**（同事务重算 `next_run_at` + `audit_ops`） |
| DELETE | `/api/v1/report-schedules/{id}` | 删除（`is_builtin=1` 的仅可停用，不可删） |
| POST | `/api/v1/report-schedules/{id}/run_now` | 立即生成一次（`trigger='manual'`） |

**WS**：`report.generated`（`report_id, period, start, end, insights_count`）、`report.schedule_updated`（`schedule_id, next_run_at`）。

> **报告与记忆沉淀的分工**：§6.7 记忆沉淀 = **自动**、无人工、粒度到"单条反馈"（回流 `feedback_items`）；报告 = **定期**、**有决策建议**、粒度到"聚合结论"。两者都读 `publications.metrics_json`，但用途不同。

---

### 4.6.6 发布适配层落地契约（T5.2 · 已落地）

§4.6.1 定的是**抽象**（三个方法、七个平台、七个错误码）；这一节定的是**落地件**：
八步流程怎么切、选择器住哪、``dry_run`` 到底停在哪一步、证据落哪。

**① 八步与代码的对应（§06.5.3）**

| 步 | 动作 | 实现 |
| --- | --- | --- |
| ① | 打开创作页 | ``PlaywrightPublisher._step_open``（读 ``selectors/<p>.yaml`` 的 ``urls.upload``） |
| ② | 上传成片 | ``_step_upload``（``set_input_files`` + 等上传完成） |
| ③ | 填标题 / 文案 / 话题 | ``_step_fill``（``domain/publish.py`` 的 ``fit_text`` / ``build_caption`` / ``render_tags``） |
| ④ | 选封面 | ``_step_cover``（``cover_path`` 为空则整步跳过） |
| ⑤ | **回读逐字比对** | ``_step_readback``（``compare_readback``；不一致重填 ≤2 次） |
| ⑥ | **点发布** | ``_step_publish`` —— **``dry_run=True`` 时流程到此为止** |
| ⑦ | 留证（截图 + DOM + 平台提示原文） | ``_step_result`` / ``PublishEvidence`` |
| ⑧ | 落库 + 回填 | ``publish_worker._publish_row`` → ``PublicationRepo``（T5.3 已落地，见 §4.6.7） |

**② 选择器：一个平台一份 yaml，装配期就校验**

```yaml
platform: douyin
version: "2026-09-16.1"      # ★ 写进 evidence_json；页面改版后靠它定位
urls:
  upload: https://creator.douyin.com/creator-micro/content/upload
  manage: https://creator.douyin.com/creator-micro/content/manage
selectors:
  video_input: 'input[type="file"]'
  title_input: "..."
  caption_editor: "..."
  cover_trigger: "..."
  publish_button: "..."
markers:
  login_expired_text: ["登录已过期", "请重新登录"]
  review_rejected_text: ["审核不通过"]
readback:
  title: value
  caption: text
```

- **装配期**（``load_selector_pack``）就校验：文件名与 ``platform`` 一致 / ``version`` 非空 /
  ``urls.upload`` 在 / 5 个必需选择器齐 / 2 个必需 marker 齐 / ``readback`` 取值合法。
  任何一条不满足 ⇒ 当场 ``PUBLISH_SELECTOR_MISS``，不推迟到真机（裁定 263）。
- ``selector_version`` 是**实例属性**（读 yaml），不是 ``ClassVar``：写成类属性之后，
  "改了 yaml 但忘了改类属性"会得到一个**永远不变的版本号**，而它存在的全部意义就是
  事后能回答"这条是哪个版本的选择器发的"。

**③ ``dry_run`` 的语义（R14 的落地）**

- ``dry_run=True`` ⇒ 走完 ①–⑤ 与 ⑦，**第 ⑥ 步一次都不执行**，返回
  ``PublishResult.stopped_before_publish()``（``ok=true`` / ``status='queued'``）。
  写成 ``ok=false`` 会让"演练成功"在面板上与"演练失败"长得一样。
- ``dry_run`` **不看** ``publish.enabled``：演练的全部意义就是"在开关还关着的时候验证链路是通的"；
  要求先打开才能演练，等于让人拿**真发布**当验证手段（裁定 264）。
- ``--target fixture`` 那个发布器**自己就拒绝** ``dry_run=False``（纵深防御）。

**④ 证据落哪**

``data/work/<task_id>/publish/<platform>/<stage>_<时刻>.png``
（``StudioPaths.publish_evidence_dir``）。``stage`` 取 ``01-open`` … ``99-failure``；
失败时另存 DOM 快照与平台提示原文。

**⑤ 发布失败不回退任务状态（§06.5.4）**

``publish/`` 里**没有任何一处**把 ``PublishStatus`` 映射成 ``TaskStatus``。
``tests/contract/test_publisher_abc.py::test_publish_failure_does_not_touch_the_task_status``
把这个"缺席"钉住 —— 一旦有人加了映射，``publish/`` 就再也不能独立重试了。

**⑥ ``health()`` 的四态**

| 页面现象 | ``ready`` | ``hint`` |
| --- | --- | --- |
| 创作页正常 | ``true`` | — |
| 出现"尚未登录" | ``false`` | ``尚未登录，需人工扫码登录`` |
| 出现"登录态已过期" | ``false`` | ``登录态已过期，需人工重新扫码登录`` |
| 探测本身抛异常 | ``false`` | ``探测失败：…`` |

**探测失败一律 ``ready=false``**（``PublishHealth.unknown``）：把"没探到"当成"是好的"，
会让一个问题从"登录态过期"（一眼可修）推迟成"上传失败"（要去翻截图）。

**⑦ 演练命令**

```
studio publish dry-run --task <id> --platform <p> [--account <a>] [--target fixture]
                       [--probe "?logged_out=1"] [--show-browser]
                       [--title T] [--caption C] [--tag TAG]... [--json]
```

``--target fixture`` 打 ``publish/fixtures/upload_form.html``（真浏览器、真导航、真选文件、
真填字、真回读、真截图）；不带 ``--target`` 则打平台真实创作页 —— 需要**已登录**的账号，
R13 不自动登录，探测不过就如实报"需人工扫码登录"。

### 4.6.7 发布池落地契约（T5.3 · 已落地）

§06.5.4 定的是**六态状态机**，§06.10 定的是**六个错误码怎么处置**；这一节定的是
"一个 ``publish/publish`` 单元从被认领到有结论"这段路上，**每一条判据住在哪**。

**① 单元生命周期（``publish/publish`` · ``unit_ref`` = 平台代号）**

```text
claim(publish/publish, unit_ref = douyin)
   ├─ 解析账号（payload.account_id ⇒ 该平台唯一启用的账号）
   ├─ 幂等短路（只读）：已有 published / canceled 的记录 ⇒ 直接收工
   ├─ 限频守卫：≤3 条/天/账号（池级为准）+ 间隔 ≥30min
   │     └─ 不过 ⇒ UnitDeferred（回 pending + not_before，**不计 attempts**）
   ├─ 开关守卫：enabled=false 且非演练 ⇒ PUBLISH_DISABLED（死信）
   ├─ 幂等登记 publications（sha256(task_id|platform|account_id) 上有 UNIQUE）
   ├─ 登录态探测：不 ready ⇒ manual_required（**不自动登录**，R13）
   ├─ 发布器 ①–⑦（§4.6.6）⇒ 按结果落 published / failed / manual_required
   └─ 失败**不回退任务状态**：任务仍是 completed，成片可下载后人工发
```

**② 两条"非失败"的出口（这是本节最要紧的一件事）**

| 出口 | 触发 | 队列里发生什么 | 为什么不是失败 |
| --- | --- | --- | --- |
| **顺延** | 限频触顶（日额度 / 最小间隔） | ``JobStore.defer``：回 ``pending`` + ``not_before``，并把认领时加的 ``attempts`` **退回去** | 走 ``fail`` 会消耗一次尝试 ⇒ 三天后一条**从没真正试过**的作业自己进死信并告警（半夜被叫起来看一条没跑过的发布） |
| **转人工** | 登录态失效 / 选择器失效 / 审核不通过 / 重试到 ``max_attempts`` | ``publications.status='manual_required'`` + 写一条 ``audit_ops``，**job 正常成功** | ``manual_required`` 的语义是"自动这条路走完了，接下来等人"。以失败收场会把同一条记录**既送进待人工队列、又送进死信告警**，而两者的排障动作完全不同 |

判"重试还是转人工"用的是**错误码 + 剩余次数**（``RETRYABLE_CODES`` + ``ctx.attempt``），
**不照抄** ``PublishResult.status`` —— 发布器不知道还剩几次机会，那是队列的事。

**③ 限频：计数与策略分居两层**

- **计数**在 :meth:`studio.db.queue.JobStore.rate_limit_state`（要读 ``publications``，
  按**本地日**统计，``status IN ('uploading','published')``）；判据读 ``pool_settings.rate_limit_json``，
  而那张表由 ``pools.yaml`` 的 ``daily_limit_per_account`` / ``min_gap_min`` 落种。
- **策略**在 ``publish/ratelimit.py``：把结论翻成"**到几点再来**"。次日顺延加
  ``blake2s(account_id + 日期)`` 派生的**确定性**抖动（0–30 分钟）——
  随机会让"每次被限频都把 ``not_before`` 往后推一点"，那条作业永远等不到自己。
  ``min_gap`` 那条**不加**抖动（锚点本身已经是散的）。
- **不做"认领前先查限频"**：认领是单条 SQL 的原子操作，认领前查会开出"查完到认领之间"的
  窗口 —— 窗口里另一个 worker 恰好发了一条，这条就**超发**了，而超发**不可逆**（R14）。
  代价是浪费一次认领，下次轮到它是 ``not_before`` 之后。

**④ 幂等键**

``publications.idempotency_key = sha256(task_id|platform|account_id)``（§03.3.15），
实现住在 ``core/ids.py`` 的 ``publication_idempotency_key``（``domain.publish.idempotency_key``
是它的别名）：``publications`` 的唯一写入者是 ``db/repositories/``，而分层是
``core → db → domain``，``db`` 反向 import ``domain`` 会被
``tests/contract/test_no_direct_job_write.py::test_db_layer_does_not_import_domain`` 当场拦下。
``|`` 分隔符**逐字照抄规格书**：换一个分隔符等于让每条已有记录换个键，重复发布防护当场失效。

**⑤ 操作面（六个端点 / 五个 CLI 命令）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | ``/api/v1/publish/publications`` | 看板：六状态计数 + 各若干条（``task_id`` 过滤时计数**只算这条任务**） |
| GET | ``/api/v1/publish/queue`` | **待人工**队列（§06.10）：带 ``error_code`` / ``error_message`` / ``evidence`` |
| POST | ``/api/v1/publish/tasks/{task_id}/enqueue`` | 投递（幂等）。**不看** ``publish.enabled`` |
| POST | ``/api/v1/publish/{publication_id}/retry`` | 人工重试：记录回 ``queued`` + 作业重排（两处都改） |
| POST | ``/api/v1/publish/{publication_id}/cancel`` | 人工取消：``canceled`` + 作废还没被认领的作业 |
| POST | ``/api/v1/publish/{publication_id}/manual-done`` | 标记已人工处理（写 ``finished_at``） |

``studio publish {enqueue,queue,retry,cancel,manual-done}`` 与上表一一对应。
``PublicationView`` 的 ``can_retry`` / ``can_cancel`` / ``can_mark_done`` 由**服务端**算：
判据是服务端的状态机规则，发给面板三个布尔比让前端记住"哪些状态能点"可靠 ——
规则改一次就漏一处，而漏的那一处会变成"点了按钮报 400"。

**⑥ 投递期**不看**开关**

``enqueue`` 在 ``publish.enabled=false`` 时**照样成功**：作业会在 worker 那一侧带
``PUBLISH_DISABLED`` 转人工。投递期直接拒绝的话，面板上什么都不会出现 ——
而"点了没反应"比"有一条带原因的待人工"难查得多。

**⑦ 为什么发布进程不在 ``SERVICE_NAMES`` 里**

``workers/run_publish.py`` 已落地（四个池的 handler 都齐了），但 ``publish`` **不进**
``service_manager.SERVICE_NAMES``（仍是"五进程"）：发布进程随 **T5.5 发布面板**一起接进
supervisor。出厂 ``publish.enabled=false``，一个常驻发布 worker 在开关关着时唯一会做的事，
是把投递进来的作业标成 ``PUBLISH_DISABLED``。

---

### 4.6.8 一键出片面板契约（T4.14 延伸 · 已落地）

T4.14 的四屏接力解决的是"不手抄任务号"，但仍然要人在四个地方各点一次、每次都等上一步
真的结束。而 ``services/pipeline_service.run_task`` 早就能一口气跑完整条链路（CLI 的
``studio pipeline run --until completed`` 就是它），只是**没有 REST 面**。这一节定的是
"把整条链路变成面板上可以点、可以看进度的一件事"。

**① 为什么是进程内登记表，不是第五个池**

``render_jobs``（§04.2.8）已经是进程内的了，而整条链路的最后一步就是它；把整条链路接进
队列要先有一个 ``pipeline`` 池（现在只有四个）。所以沿用同一条路，**代价写在明处**：

- 进程重启 ⇒ 在跑的任务信息丢失（成片与句子音频都在盘上，重跑一条命令就能接上）；
- 只在本进程内可见（多开一个 API 进程，两边各看各的）。

将来接进队列时，替换的是 ``PipelineJobService._run`` 里的执行方式，对外形状不变。

**② 三个硬规矩照旧（面板只是把它们**提前**说出来）**

``run_task`` 的三条：**不写稿**（``pending`` / ``drafting`` 只做状态迁移，需先有生效稿件）、
**不代按确认闸**（``awaiting_approval`` 报错停下）、**不接 stuck 状态**（``editing`` /
``failed`` / ``manual_pool`` / ``discarded`` / ``canceled``）。

把这三条做成"点了之后返回 409"，面板就只能等错误回来才说话 —— 而任务号是用户手输的，
敲错一个字符也要等一轮往返。所以有 ``GET /pipeline/tasks/{id}``：它返回
``plan_task(...)`` 的结论（能不能跑 / 为什么不能 / 会从哪儿推到哪儿）。**判据与
``run_task`` 共用** ``_STUCK`` / ``_FORWARD`` / ``_check_until``，不是又写一遍 ——
两处判断迟早会分叉，而分叉的表现是"面板说能跑、点了报错"。

**③ 落点是**字符串**，真相只有一处**

请求体收 ``until: str``，服务端按 ``supported_until()`` 复核：不合法 ⇒ **422**
（``VALIDATION_FAILED``，``context.supported`` 给出能用的那些）。**不用**
``STATE_TRANSITION_ILLEGAL`` —— 那个码会让人以为是任务状态不对，跑去查任务号。

下拉框的文案（``_UNTIL_TEXT``）键必须覆盖全部落点，由
``tests/integration/test_pipeline_api.py`` 的契约用例盯着：少一个 ⇒ 那个落点从界面上
**消失**，而没有任何地方会报错。

**④ 同一个任务不会同时跑两条**

``submit`` 见到该任务已有在跑的 job ⇒ **原样返回那一条**，响应里的 ``deduped=true``。
连点两次"开始出片"是很自然的动作（面板卡顿时尤其如此），而两条链路同时改一个任务的状态，
轻则互相踩状态迁移（乐观锁把一条踢成 ``CONCURRENT_MODIFICATION``），重则两条都渲染一遍。

**⑤ 取消是协作式的，检查点**比渲染少

进度回调只在**配音的每一句**与**渲染的每一段**被调到。任务停在"投递配音作业"或"拼母带"
那几步时按取消，要等它进到下一个回调点才会真的停 —— 这一点由服务层写进 ``note``，
面板照它显示（"已请求取消"而不是"已取消"）。

**⑥ 进度条在跑完时是 100%**

``PipelineJob.percent`` 在 ``status == "succeeded"`` 时直接返回 100，**不看最后一次回调
落在哪个分母上**：这条链路的回调不是连续的（配音按句、渲染按段、投递与拼母带那几步根本
不回调），最后停在哪取决于它是从哪一步收尾的。进度条停在 37% 而旁边写着"已完成"，
是最容易被当成"卡住了"的一种显示 —— 而用户此刻正要去点播放。（``RenderJobService``
的同一处一并修掉，见陷阱 146。）

**⑦ 操作面（五个端点）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | ``/api/v1/pipeline/console`` | 首屏：可选落点（带人话）/ 可选音色 / 在跑的那条 / 最近 10 条 |
| GET | ``/api/v1/pipeline/tasks/{task_id}`` | 这条任务点了会怎样（``until`` 可给可不给；任务不存在 ⇒ 404） |
| POST | ``/api/v1/pipeline/jobs`` | 开一条（幂等，见 ④）。**不读库**：任务号写错会变成一条带原因的失败 job |
| GET | ``/api/v1/pipeline/jobs/{job_id}`` | 轮询进度（``^p[0-9]{4,}$``，前缀 ``p`` 与渲染的 ``r`` 分开） |
| POST | ``/api/v1/pipeline/jobs/{job_id}/cancel`` | 叫停（协作式，见 ⑤） |

进度用**轮询不用 WS**（与渲染面板同一条理由）。成片**不在这里播**：它就是
``data/output/videos/`` 里那一支，走渲染面板的 ``/render/videos/{name}``（带 Range）。

**⑧ 配音不依赖常驻池**

``run_task`` 在 ``voicing`` 那一步**就地借一条 worker** 把这条任务的配音干完（T2.8 的裁定，
§04.3.4）。所以这个面板在"没起任何池进程"的机器上也能从稿子一路做到成片 —— 这正是它作为
"第一支 MP4 的入口"的意义。常驻池同时在跑也不会出错：作业认领靠租约，谁先拿到算谁的。

**⑨ 这一屏**不重复**渲染层的参数**

画布档 / 字幕 / 线程数归「合成配置」与「渲染」两块面板管。在这一屏再放一遍，要么得多问
后端一次"配置里现在开着吗"（多一处会过时的显示），要么就变成一个"看着是关的、实际是跟随
配置"的假开关 —— 后者比没有更糟。这一屏只管一件事：**从这条任务当前的状态推到哪一步**。

---

## 4.7 端到端时序（一次完整任务）

```
WebUI ──POST /topics/select──▶ API ──INSERT tasks(pending) + jobs(draft/task)──▶ DB
                                 └─WS: task.created / pool.stats

draft worker ──claim(draft/task)──▶ DB            (lease 180s)
   ├─ agent.planner  → content_directions（若选题池已有方向则跳过）
   ├─ agent.director → scripts.outline_json
   ├─ agent.writer   → scripts + script_sentences（逐句入库，同一事务）
   ├─ agent.reviewer → 规则通道 + LLM 六维度 → review_scores
   ├─ 0.3×rule + 0.7×llm ⇒ grade
   │     A ⇒ 自动放行（approved_by='auto_approve_A'）→ queued_voice
   │     B ⇒ agent.editor（≤2 轮）→ 重审 → 仍 B ⇒ awaiting_approval
   │     C ⇒ discarded
   └─ WS: review.scored / script.editing / approval.requested

【确认闸 · 唯一人工节点】WebUI ──POST /tasks/{id}/approve──▶ API
   └─ transition: awaiting_approval → queued_voice + audit_ops
        └─ 批量 enqueue jobs(voice/sentence × N)

voice worker ──claim(sentence)──▶ DB              (lease 90s)
   ├─ normalize/segment → tts_hash 命中缓存? ── 是 ─▶ 复用 wav（0 引擎调用）
   ├─ HTTP /synth ──▶ TTS 服务（GPU 串行，fp16 常驻）
   ├─ ffprobe 实测时长 → 回填 script_sentences.tts_duration_ms
   └─ 全部句 done/skipped ⇒ 时间轴全量重算 ⇒ timeline.json ⇒ queued_render
        └─ enqueue jobs(render/scene × M) → jobs(render/final, depends_on=[scenes])

render worker ──claim(final)──▶ DB                (lease 600s)   ← ★ 一期：单遍合成，无 scene 单元
   ├─ total_ms = ffprobe(voice_master).duration + tail_ms        （人声决定时长 · C12）
   ├─ RandomizationPlan(seed) → 抽跑酷片段 + 随机入点 → broll_usage 落账
   ├─ CompositePlan → filter_complex.txt（语法预检）
   ├─ ffmpeg 单次调用：跑酷循环裁长 → 缩放铺满 → 叠加固定水印 → 混音 → 编码
   │     libx264 CRF21 + faststart + bt709 → final/final.mp4（.partial → 原子改名）
   ├─ 质检：lufs −16±0.5 / true_peak ≤ −1.0 / dup_audit；av_sync **仅诊断不阻断**（C12）
   └─ transition: rendering → completed + artifacts + manifest.json + composite_hash
        └─WS: task.completed──▶ WebUI（在线播放 / 一键打开目录）

【发布（默认关闭）】scheduler ──到点──▶ enqueue jobs(publish/publish × 平台)   ← ★ D7 定时任务
   └─ publish worker ──claim(publish)──▶ DB   (lease 600s)
        ├─ 封面 → 发布前二次校验 → 限频/登录态 → 上传 → 发布
        ├─ 成功 ⇒ publications(published) ──WS: publish.done──▶ WebUI
        └─ 失败 ⇒ 重试 ≤3 ⇒ manual_required + 取证 + 告警
             └─ 定时：metrics 回收（T+1h/6h/24h/72h）→ memory 沉淀 → feedback 回流
                  └─ 定期：report 生成（周/月）→ insights 决策建议 → 人工采纳 ⇒ 下轮选题  ← ★ D9 报告
```

**跨阶段守卫（每次迁移前必须通过，否则拒绝迁移并落 `error_code`）**

| 迁移 | 守卫 |
| --- | --- |
| `drafting → reviewing` | `scripts` 存在 + `script_sentences` 句数 ≥ 15 + 字数 600–800 |
| `reviewing → queued_voice` | `grade ∈ {A,B}` 且 A 级自动放行策略允许 或 已人工确认 |
| `queued_voice → voicing` | 至少 1 个 `voice/sentence` job 处于 `pending/claimed` |
| `voicing → queued_render` | 全部句 `tts_status ∈ {done, skipped}` + `voice_master.wav` 存在且 `total_ms` 已实测 |
| `queued_render → rendering` | 至少 1 个 `render/final` job 可认领（**一期只有 final 单元**，C13） |
| `rendering → completed` | `final.mp4` 存在 + **水印已叠加**（D5）+ 响度/相似度通过；`av_sync` 仅诊断（C12）；`quality.degraded=true` 时允许带 `warn` 通过 |
| `completed → publishing` | `publish.enabled=true` + 发布前二次校验通过 + 限频未触顶 |


---

## 4.8 五进程编排契约（T1.12 · 原文附2 / §7.4 · ADR-002）

**唯一真相**：`src/studio/services/service_manager.py` 的 `SERVICE_NAMES` + `build_specs()`。
Python 与 PowerShell（`ops/*.ps1`）都按这一份规格找进程 —— 关停时序**只**写在 Python 里（裁定 104：
能测才敢改；`ops/*.ps1` 退化为薄壳，T4.12 的总览台启停 import 同一份实现）。

### 4.8.1 进程规格表

| 进程 | `kind` | 命令行（`<python>` = 当前解释器） | 端口 | 就绪判据 | 未就绪的后果 | 关停主通道 |
| --- | --- | --- | --- | --- | --- | --- |
| `api` | HTTP | `<python> workers/run_api.py` | 8787 | `GET /api/v1/health` 回 200 | **硬失败**（`required=True`：WebUI 是唯一入口） | 信号 |
| `tts` | HTTP | `<python> workers/run_tts.py` | 8788 | `GET /health` 回 200 | 降级 `server_missing`（§1.7） | 信号 |
| `draft` | POOL | `<python> workers/run_draft.py` | — | handler 已注册 **且**活过观察窗 | 降级 `handler_missing` ⇒ **不拉起**（裁定 103） | 标志文件 |
| `voice` | POOL | `<python> workers/run_voice.py` | — | 同上 | 同上 | 标志文件 |
| `render` | POOL | `<python> workers/run_render.py` | — | 同上 | 同上 | 标志文件 |

- 池观察窗 `POOL_GRACE_SEC = 3.0s`：池进程没有 HTTP 面 ⇒ 用"活过观察窗"代替握手（真探活属 T4.11）。
- 就绪总预算 `DEFAULT_READY_TIMEOUT_SEC = 60.0s`；关停宽限 `DEFAULT_STOP_TIMEOUT_SEC = 10.0s`。
- `Readiness` 四值：`ready` / `entry_missing`（入口脚本不在）/ `handler_missing`（池 handler 未注册）/
  `server_missing`（`studio.tts.server` 未落地）；每个非 ready 结论都带**可直接展示的** `remediation`。

### 4.8.2 启动契约（`StartReport`）

`ServiceManager.start(*, open_browser, only=None, doctor_gate=True, ready_timeout_sec=60.0)` 顺序**固定**：

```
① doctor 门禁（不过 ⇒ 抛 ENV_CONTRACT_VIOLATION，一个进程都不拉）
② clean_stale()：清陈旧 PID 台账 + 残留关停标志（**先清再拉**，否则新 worker 起来就自己关掉）
③ 端口预探测（占用且不是自己的 ⇒ 该进程记 port_busy，其余照常）
④ 逐进程拉起（台账里有活进程 ⇒ already_running，不重复拉）
⑤ 等就绪（HTTP 走 /health；池走观察窗）
⑥ 开浏览器（**只在 api 真就绪时**）
```

| 字段 | 含义 |
| --- | --- |
| `ok` | **没有 `failed` 就是真** —— 降级不算失败（P4 无人值守优先，§1.7） |
| `started` / `ready` / `degraded` / `failed` | 本次拉起 / 已就绪 / 降级（未拉起或未就绪）/ 硬失败 |
| `already_running` | 台账里有活进程 ⇒ 不重复拉起 |
| `port_busy` | 端口被**别人**占用（"端口冲突" ≠ "已在运行"，两者必须分开报） |
| `readiness` | 5 条就绪结论（`name` / `readiness` / `ready` / `detail` / `remediation`） |
| `url` / `browser_opened` / `elapsed_ms` | `http://127.0.0.1:8787` / 是否真开了浏览器 / 总耗时 |

- **退出码**：`0` = `ok`；`1` = 有 `failed`（`studio service start` 与 `run_entry()` 同语义）。
- **调用姿势**：`studio service start` 必须在**环境变量已重定向**的会话里跑（`ops/*.ps1`、`启动.bat` 已 dot-source `scripts\env.ps1`）。
  裸 shell 直接跑会被 doctor 门禁拦下（`env.contract` 报 7 项缺失）—— 这是**设计如此**（R1 防 C 盘爆盘），不是 bug。
- `only=[...]` 只拉指定进程（排障用）；出现**未知名字** ⇒ 拉起任何进程之前就抛错。

### 4.8.3 关停契约（`StopReport`）与三级时序

主通道是**标志文件** `data/logs/<name>.stop`（裁定 102）：`GenerateConsoleCtrlEvent` 只作用于**同控制台**的
进程组，而 `停止.bat` 是另起一个控制台 ⇒ 从那里发的 Ctrl-Break 会**静默失效**（API 返回 TRUE 但目标收不到），
最后只能硬杀、丢进度。标志文件与"命令从哪个控制台发出"无关；信号与强杀只作**升级手段**。

```
① 对 polls_stop_flag=True 的进程：写标志 ⇒ 等 budget×0.6（池 worker 在 1s 脉冲 tick 里看到就 draining）
② 仍未退 ⇒ request_break()（Ctrl-Break，同控制台才有效）⇒ 等 max(0.5, budget×0.3)
③ 仍未退 ⇒ terminate ⇒ 等 max(1.0, budget×0.2) ⇒ 仍不退 ⇒ kill ⇒ 等 1.0s
```

| 字段 | 含义 |
| --- | --- |
| `ok` | **没有 `forced` 就是真** |
| `stopped` | 优雅退出（标志或信号） |
| `forced` | **强杀** ⇒ 可能丢了当前单元，如实记录，不假装优雅 |
| `missing` | 台账有、进程已不在（陈旧台账，顺手清理） |
| `skipped` | 没有台账（从没起过 / 已关过） |
| `elapsed_ms` | 总耗时 |

**不变量**

- 关停**只动 PID 台账里的进程**：台账外的进程一律不碰（"停止"不许变成"杀掉一切 python"）。
- 结束后**一定清掉**自己的 PID 台账与关停标志（残留标志 ⇒ 下次启动"什么都不干"，最难查）。
- 幂等：对没在跑的服务 `stop` ⇒ `skipped`，**不是错误**（退出码 0）。
- `polls_stop_flag=False` 的进程（uvicorn 无 tick）**直接进 ②** —— 不为它白等一个"永远不会发生"的优雅退出（裁定 107）。

### 4.8.4 环境变量与日志

| 变量 | 方向 | 说明 |
| --- | --- | --- |
| `STUDIO_STOP_FLAG` | 父 → 子 | 关停标志文件的**绝对路径**；`pools.runner.stop_flag_from_env()` 读它；空串 ⇒ 不启用（`Path("")` 会变成 `.`，"看起来配了"其实永远不生效） |
| `PYTHONUNBUFFERED` | 父 → 子 | `1` ⇒ 子进程日志**逐行**落盘，不等到缓冲区满 |

- 子进程 `stdout/stderr` **追加**到 `data/logs/<name>.log`（`stdin=DEVNULL` + `CREATE_NEW_PROCESS_GROUP`）
  ⇒ "起来就死"时第一眼看日志就有原因（裁定 106）。
- 路径规则只有一处真相：`StudioPaths.pid_file()` / `stop_flag_file()` / `service_log_file()`（裁定 105）。

### 4.8.5 一期降级现状（2026-09-13 实测）

| 进程 | `readiness` | 落地任务 |
| --- | --- | --- |
| `api` | `ready` | ✅ 实测就绪（`/api/v1/health` 回 `ok=true`）：经 `ops\start_all.ps1` ⇒ `elapsed_ms=4827`（含 `uv run` 开销），进程内直调 `ServiceManager` ⇒ `≈1.4s` |
| `tts` | `server_missing` | T2.2（`studio.tts.server`） |
| `draft` | `handler_missing` | T4.11（`draft/task` 单元接线） |
| `voice` | `handler_missing` | T2.6 |
| `render` | `handler_missing` | T3.x |

> **裁定 108**：M1 的"5 进程全部 ready"**在 M1 阶段不可能达成**（三个池的 handler 分属 T2.6 / T3.x / T4.11）
> ⇒ M1 的验收口径改为"`api` ready + 其余**如实报降级**"，"5 进程全 ready"顺延到 M4（四池并行落地后）。
> **为了让验收数字好看而放行一个永远不消化队列的空转 worker 是错误做法**（裁定 103 的推论）。

---

## 4.9 Web 契约生成契约（T4.1 · 前端类型的**唯一来源**）

> 一句话：**Python 是真相源，TypeScript 是生成物**。"禁止手写接口类型"不是靠自觉，
> 而是靠"生成物入库 + 契约测试逐字比对 + 静态扫描 `fetch(`"三条同时生效。

### 4.9.1 为什么要生成，而不是手抄

REST 与 WS 是**跨语言**接口：服务端 Python、客户端 TypeScript。手抄类型不会报错，只会在
运行时表现为"某个字段永远是 `undefined`"或者"面板少一块数据"——这类漂移没有任何本地信号，
只有真机联调时才暴露。所以这里把"两端一致"变成一条可断言的性质。

### 4.9.2 真相源与生成物

| 真相源 | 生成物 | 渲染函数 |
| --- | --- | --- |
| FastAPI 的 `app.openapi()`（Pydantic 模型是模型层） | `web/openapi.json` | `webcontracts.render_openapi_json()` |
| `ws/protocol.py`（`CHANNELS` / `FrameType` / `EventKind` / `KIND_POLICY` / `SEVERITY_ORDER` / 背压阈值 / `WS_PATH` / `WS_PROTOCOL_VERSION`） | `web/src/ws/events.ts` | `webcontracts.render_events_ts()` |
| `services/log_service.SystemLog`（`dataclasses.fields` + `get_type_hints`） | `events.ts` 的 `SystemLogRow` | 同上 |
| `web/openapi.json` | `web/src/api/types.gen.ts` | `openapi-typescript`（node 侧） |

渲染函数是**纯函数**（同输入同输出，不碰文件系统），所以契约测试可以直接调它们比对，
不需要"先跑一遍生成命令"这种副作用。

### 4.9.3 生成与校验命令

```powershell
.\tasks.ps1 web:gen      # ① dump_web_contracts.py（Python 侧）→ ② npm run gen:api（node 侧）
.\tasks.ps1 web:check    # 只比对不写盘；漂移 ⇒ 退出码 1
.\tasks.ps1 check        # 门禁已含 web:check（漂移在提交前就红）
```

> `web:gen` 必须是**两步**：前者是 Python 的活，后者是 node 的活。合成一条会在"Python 环境没装 node"
> 或反之的情况下失败得很难懂。

### 4.9.4 三条落地手段

1. **生成物入库**（`.gitignore` 用显式 `!` 白名单放行）：漂移会在 diff 里看得见。
2. **契约测试逐字比对**（`tests/contract/test_web_contracts.py`）：后端一改、生成物没跟着改 ⇒ 门禁红。
3. **`fetch(` 收敛到一处**（同文件静态扫描 `web/src/**`）：整个前端只有 `api/http.ts` 允许直接
   `fetch(`。这样"统一超时 / 统一错误信封 / 统一请求头"才有唯一落点。

### 4.9.5 两侧的日志行形状（易错点）

REST 的 `LogRow` 与 WS 的 `SystemLog.to_dict()` **必须同字段集**，由契约测试断言。注意两路载荷
**并不逐字相同**，前端需归一：

| 路径 | 主键字段 | 缺失字段 |
| --- | --- | --- |
| `logs` 快照行 / REST 分页 | `id` | — |
| `log.appended` 事件 | **`log_id`** | `trace_id` / `seq_in_task` |

> 只按一种形状写解析，另一路会**静默**变成 `undefined`（日志面板少一截且不报错）—— 陷阱 #59。

### 4.9.6 前端类型的硬约束

- `types.gen.ts` / `openapi.json` / `events.ts` **禁止手改**（文件头有醒目提示）。
- 端点封装一律 `OkJson<"/api/v1/xxx", "get">`：**改了后端路径但没改前端**会在 `npm run typecheck`
  阶段就炸，而不是等到运行时 404。
- `web/src/env.d.ts` **故意不写** `declare module "*.vue"`：`vue-tsc` 原生认识 `.vue` 并校验
  props/emits，补一个返回 `any` 的模块声明等于把类型门禁关掉。
