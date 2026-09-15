# §03 核心数据结构与持久化 Schema

> 唯一真相源 = `data/studio.db`（SQLite，WAL，**30 表**）。全部 DDL 采用**纯 SQL 迁移文件**（`src/studio/db/migrations/000N_*.sql`），ORM 只映射、不建表。
> 依赖前提：SQLite ≥3.35（`UPDATE ... RETURNING`）、JSON1 扩展（3.38+ 默认启用）。本机 3.51.1 ✅
> **本节的 DDL 已用本机 `sqlite3 3.51.1` 实际执行验证**：**29 表 / 60 索引 / 6 触发器 / `integrity_check=ok` / `foreign_key_check` 为空**。
> 另已实测：`report_schedules` 的**部分唯一索引**（同周期仅 1 个启用）、`at_time` CHECK、`weekly 必须给 weekday` CHECK、到期查询只取启用项 —— 全部符合预期。

---

## 3.1 实体关系（ER 总览）

```
                    ┌──────────────┐         ┌──────────────────────┐
                    │   personas   │         │  template_definitions│
                    └──────┬───────┘         └───────────┬──────────┘
                           │ persona 注入                ▼
  ┌──────────┐      ┌──────▼────────────┐      ┌──────────────────┐
  │ hot_items│──┐   │content_directions │      │ template_scenes  │
  └──────────┘  ├──▶│  (方向 5–8)       │      └────────┬─────────┘
  ┌──────────┐  │   └──────┬────────────┘               ▼
  │feedback_ │──┘          ▼                  ┌────────────────────┐
  │  items   │      ┌──────────────┐          │template_components │
  └──────────┘      │topic_candidates│         └────────────────────┘
                    │  (选题池 20+) │
                    └──────┬───────┘
                           │ 勾选 → 建任务
                           ▼
   ┌───────────┐    ┌──────────────┐    ┌──────────────┐   ┌────────────┐
   │  scripts  │◀───│    tasks     │───▶│     jobs     │   │task_events │
   └─────┬─────┘    │  (16 态状态机)│    │(四池+租约)   │   └────────────┘
         ▼          └──┬───┬───┬───┘    └──────┬───────┘
 ┌──────────────────┐  │   │   │               ▼
 │ script_sentences │  │   │   │      ┌──────────────────┐
 │ (句级续传核心)    │  │   │   │      │worker_heartbeats │
 └────────┬─────────┘  │   │   │      └──────────────────┘
          ▼            │   │   │
   ┌────────────┐      │   │   └────────────▶ ┌──────────────┐
   │ artifacts  │◀─────┘   └────────────────▶ │review_scores │
   └────────────┘                             └──────────────┘
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │ system_logs  │   │  llm_calls   │   │  audit_ops   │
   └──────────────┘   └──────────────┘   └──────────────┘
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │ broll_clips  │──▶│ broll_usage  │   │ approvals    │   │ publications │
   └──────────────┘   └──────────────┘   └──────────────┘   └──────┬───────┘
   ┌──────────────┐   ┌──────────────┐
   │  bgm_tracks  │   │voice_profiles│  （BGM · Q12 / 音色 · T4.8）
   └──────────────┘   └──────────────┘
                                                                   │ 数据回流
   ┌──────────────┐   ┌──────────────────┐   ┌──────────────┐      ▼
   │pool_settings │   │publish_schedules │──▶│   reports    │  data/feedback/auto_*.md
   │  (池并发/暂停) │   │  (定时发布 · D7)  │   │ (报告 · D9)   │  （→ 下轮 Planner）
   └──────────────┘   └──────────────────┘   └──────▲───────┘
   ┌──────────────────┐                             │ last_report_id
   │report_schedules  │─────────────────────────────┘
   │ (报告周期 · Q15)  │
   └──────────────────┘
```

**表清单（30）**：`schema_migrations`、`personas`、`hot_items`、`feedback_items`、`content_directions`、`topic_candidates`、`tasks`、`scripts`、`script_sentences`、`review_scores`、`jobs`、`template_definitions`、`template_scenes`、`template_components`、`artifacts`、`system_logs`、`approvals`、`task_events`、`llm_calls`、`worker_heartbeats`、`broll_clips`、`broll_usage`、`publications`、`audit_ops`、`pool_settings`、`publish_schedules`、`reports`、`report_schedules`、`bgm_tracks`、`voice_profiles`。

---

## 3.2 迁移与连接前置

```sql
-- 0000_pragmas.sql（每个连接建立后执行）
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA temp_store   = MEMORY;
PRAGMA cache_size   = -32000;
PRAGMA mmap_size    = 268435456;
PRAGMA wal_autocheckpoint = 1000;
```

```sql
-- 0001_init.sql（节选：迁移登记表）
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,                      -- '0001'
  name        TEXT NOT NULL,
  checksum    TEXT NOT NULL,                         -- 文件 sha256，防偷改历史迁移
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms INTEGER
);
```

---

## 3.3 表 DDL

### 3.3.1 `personas`（频道定位 · 原文 §2.1 · 唯一人工必填）

```sql
CREATE TABLE personas (
  id            TEXT PRIMARY KEY,                            -- 'persona_default'
  name          TEXT NOT NULL,
  is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  role_desc     TEXT NOT NULL,                               -- 人设描述（"熊大熊二对话体"）
  tone          TEXT NOT NULL,                               -- 口吻（"东北话、兄弟互怼、接地气"）
  audience      TEXT NOT NULL,                               -- 受众（"18-35 男性、游戏/动漫"）
  catchphrases_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(catchphrases_json)),  -- 口癖
  forbidden_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(forbidden_json)),     -- 禁区
  style_hint    TEXT,
  target_chars_min INTEGER NOT NULL DEFAULT 600,             -- 原文 §2.2④
  target_chars_max INTEGER NOT NULL DEFAULT 800,
  max_duration_ms  INTEGER NOT NULL DEFAULT 180000,          -- 原文"3分钟以内"
  source_path   TEXT,                                        -- config/persona.yaml
  source_sha256 TEXT,
  version       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE UNIQUE INDEX ux_persona_active ON personas(is_active) WHERE is_active = 1;
```

> **文件是唯一真相，表是投影**（T4.13 裁定 165）：`config/persona.yaml` 与 `config/personas/*.yaml` 才是人设的真相，
> 本表是它们的**只读投影**（供 Agent 注入与报告归因 join）。人物库面板的四个写端点（§04.5.8）
> 只改文件、不改本表 —— 「激活的」和「库里被改的」因此不会变成两份互不知情的真相。

### 3.3.2 `hot_items`（热点 · 原文 §2.1，格式 `标题|热度|平台`）

```sql
CREATE TABLE hot_items (
  id          TEXT PRIMARY KEY,
  source_file TEXT NOT NULL,                                 -- data/hot/xxx.md
  line_no     INTEGER,
  title       TEXT NOT NULL,                                 -- 第 1 列
  heat        TEXT,                                          -- 第 2 列（原样保留：'9821' | '爆' | '高'）
  platform    TEXT,                                          -- 第 3 列（'抖音' | '微博' | 'B站'）
  raw_line    TEXT NOT NULL,                                 -- 原始行（解析失败可回溯）
  parse_ok    INTEGER NOT NULL DEFAULT 1 CHECK (parse_ok IN (0,1)),
  used_by_direction_id TEXT,                                 -- 被哪个方向消费
  consumed_at TEXT,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_hot_unused   ON hot_items(created_at DESC) WHERE consumed_at IS NULL;
CREATE INDEX idx_hot_platform ON hot_items(platform, created_at DESC);
```

### 3.3.3 `feedback_items`（历史反馈 · 原文 §2.1）

```sql
CREATE TABLE feedback_items (
  id           TEXT PRIMARY KEY,
  source_file  TEXT NOT NULL,                                -- data/feedback/xxx.md
  platform     TEXT,
  occurred_on  TEXT,                                         -- 可选：结构化头里的日期
  content      TEXT NOT NULL,                                -- 原文
  sentiment    TEXT CHECK (sentiment IN ('positive','neutral','negative','unknown')),
  wants_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(wants_json)),      -- 用户"想要"
  complaints_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(complaints_json)), -- 用户"吐槽"
  is_auto      INTEGER NOT NULL DEFAULT 0 CHECK (is_auto IN (0,1)),            -- 发布回流自动写入
  source_publication_id TEXT,                                -- 回流来源
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_fb_sentiment ON feedback_items(sentiment, created_at DESC);
CREATE INDEX idx_fb_auto      ON feedback_items(is_auto, created_at DESC);
```

> **导入幂等键 = `(source_file, content)`**（T1.9 落地裁定）：`hot_items` 有 `line_no`，本表**没有**
> —— 结构化头解析失败时整行即 `content`，行号会随文件重排而漂移，用它做键会把"同一句话"判成两条。
> `content` 是这行的事实，`source_file` 划定范围；自动回流文件带 `auto_` 前缀天然隔离（§06.6）。
>
> **解析留痕**：坏行不阻塞整批 —— 单行 `warn` 后按原样落库（`sentiment` 可为 `NULL`），
> 解析警告进 `system_logs`（`source='services.input_service'`），不写回本表。
> **情感口径**：领域层 `pos/neu/neg/unknown`，落库一律 `positive/neutral/negative/unknown`，
> 换算只在 `domain.SENTIMENT_TO_DB` / `DB_TO_SENTIMENT`（裁定 66）。

### 3.3.4 `content_directions`（方向 · 原文 §2.2① Planner 产出 5–8 个）

```sql
CREATE TABLE content_directions (
  id            TEXT PRIMARY KEY,
  batch_id      TEXT NOT NULL,                               -- 一次 Planner 运行的批次
  seq           INTEGER NOT NULL,
  title         TEXT NOT NULL,
  rationale     TEXT NOT NULL,                               -- 理由（WebUI 展示）
  grounded_on_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(grounded_on_json)),
      -- 依据：{"type":"hot"|"feedback"|"persona","id":...}
  priority      INTEGER NOT NULL DEFAULT 100,                -- 越小越优先（原文"优先用户想要的"）
  risk_flags_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(risk_flags_json)),
  status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','selected','dropped')),
  llm_model     TEXT,
  prompt_version TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_dir_batch ON content_directions(batch_id, seq);
```

### 3.3.5 `topic_candidates`（选题池 · 原文 §2.2② 每方向 4 个）

```sql
CREATE TABLE topic_candidates (
  id            TEXT PRIMARY KEY,
  direction_id  TEXT NOT NULL REFERENCES content_directions(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  title         TEXT NOT NULL,                               -- 选题标题（有网感/钩子）
  hook_type     TEXT,                                        -- 冲突 | 悬念 | 反差 | 数字
  angle         TEXT NOT NULL,                               -- 角度差异化说明
  exec_feasible INTEGER NOT NULL DEFAULT 1 CHECK (exec_feasible IN (0,1)),  -- 3 分钟内可执行
  score         REAL,                                        -- 选题评分（瀑布流排序）
  reason        TEXT,                                        -- 评分理由
  dedup_hash    TEXT,                                        -- ★ R15：标题归一化哈希
  similar_to_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(similar_to_json)),
  status        TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN (
                  'candidate','selected','queued','rejected','expired')),
  task_id       TEXT,                                        -- 勾选后派生的任务
  selected_at   TEXT,
  selected_by   TEXT,                                        -- 'user' | 'auto_A'
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_topic_pool  ON topic_candidates(status, score DESC);
CREATE INDEX idx_topic_dir   ON topic_candidates(direction_id, seq);
CREATE INDEX idx_topic_dedup ON topic_candidates(dedup_hash);
```

> **`set_status` 自动盖 `selected_at`**（T1.9 裁定 75）：状态转入 `selected` 时由仓储写时间戳，
> 与 `hot_items.mark_consumed` 同一口径 —— "什么时候被选中的"是这一行的事实，
> 漏传就是一条静默的空列。
>
> **`dedup_hash` 两级去重（R15）**：归一化哈希完全命中 ⇒ **丢弃不入库**（池子里看不到）；
> 相似度 ≥ `0.85` ⇒ `score` 降 `2.0` 并写 `similar_to_json`（最多 3 条）。
> 历史比对取**全部状态**的最近 500 条（`queued`/`selected` 同样参与），与 `idx_topic_pool` 对齐。
> 领域实现：`domain/topics.py::normalize_title / hash_title / similarity / dedup_topic`。
### 3.3.6 `tasks`（主任务表 · 16 态状态机）

```sql
CREATE TABLE tasks (
  id                  TEXT PRIMARY KEY,                       -- ULID，字典序 = 时间序
  kind                TEXT NOT NULL DEFAULT 'video' CHECK (kind IN ('video','audio_only','draft_only')),
  title               TEXT NOT NULL,
  topic               TEXT,

  status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                        'pending','drafting','reviewing','editing','awaiting_approval',
                        'queued_voice','voicing','queued_render','rendering',
                        'completed','publishing','published',
                        'failed','manual_pool','discarded','canceled')),
  last_healthy_status TEXT,                                   -- 失败前状态 ⇒ 断点重试依据
  pool                TEXT NOT NULL DEFAULT 'draft'
                        CHECK (pool IN ('draft','voice','render','publish','none')),
  priority            INTEGER NOT NULL DEFAULT 100,           -- 数值越小越优先
  version             INTEGER NOT NULL DEFAULT 1,             -- 乐观锁

  -- 选题来源与评分（原文 §2.2⑤/⑦）
  source_direction_id TEXT,
  source_topic_id     TEXT,
  grade               TEXT CHECK (grade IN ('A','B','C')),
  score_total         REAL,                                   -- 0.3×规则 + 0.7×LLM
  score_rule          REAL,
  score_llm           REAL,
  revision_round      INTEGER NOT NULL DEFAULT 0,             -- 改稿轮次（≤2，原文 §2.2⑥）

  -- 租约（任务级认领）
  lease_owner         TEXT,
  lease_expires_at    TEXT,

  -- 进度与上下文
  progress            REAL NOT NULL DEFAULT 0.0 CHECK (progress BETWEEN 0 AND 1),
  stage_detail        TEXT,                                   -- 'voice: 12/27 句'
  payload_json        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
      -- 输入：{audience, angle, hook_type, persona_id, target_duration_ms, style, template_id,
      --        voice_map, seed}
      --   ★ hook_type / persona_id 为 T1.10 裁定 87 增补：人物可热改 ⇒ 必须记住"这一稿是
      --     哪个 persona 写的"；hook_type 取选题归一化后的值，供 §06 归因免 join
  context_json        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(context_json)),
      -- 产物引用：{script_id, timeline_path, scenes:[...], final_path, cover_path}
  quality_json        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(quality_json)),
      -- QC：{av_sync_offset_ms, lufs, true_peak, phash_distance, degraded, degrade_reason}

  -- 重试与错误
  attempt_count       INTEGER NOT NULL DEFAULT 0,
  max_attempts        INTEGER NOT NULL DEFAULT 3,
  retry_from          TEXT,
  error_code          TEXT,
  error_message       TEXT,
  error_trace         TEXT,

  template_id         TEXT REFERENCES template_definitions(id) ON DELETE SET NULL,
  idempotency_key     TEXT UNIQUE,                            -- 防重复建任务

  created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  started_at          TEXT,
  finished_at         TEXT,
  approved_at         TEXT,
  approved_by         TEXT                                    -- 'user'|'auto_approve_A'|'auto_approve_AB'
);

CREATE INDEX idx_tasks_board       ON tasks(status, priority, created_at DESC);
CREATE INDEX idx_tasks_claim       ON tasks(pool, status, priority, created_at) WHERE status='pending';
CREATE INDEX idx_tasks_lease       ON tasks(status, lease_expires_at) WHERE lease_expires_at IS NOT NULL;
CREATE INDEX idx_tasks_created_day ON tasks(substr(created_at,1,10));
CREATE INDEX idx_tasks_grade       ON tasks(grade, status);
CREATE INDEX idx_tasks_topic       ON tasks(source_topic_id);
CREATE INDEX idx_tasks_manual      ON tasks(status, finished_at) WHERE status='manual_pool';

CREATE TRIGGER trg_tasks_touch AFTER UPDATE ON tasks FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE tasks SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
```

### 3.3.7 `scripts` + `script_sentences`（稿件与逐句续传）

```sql
CREATE TABLE scripts (
  id              TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  version         INTEGER NOT NULL DEFAULT 1,                 -- 修订版本（打回即 +1）
  is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  title           TEXT,
  hook            TEXT,                                       -- 前 3 秒钩子
  body_md         TEXT NOT NULL,                              -- 全文（600–800 字）
  cta             TEXT,
  word_count      INTEGER NOT NULL DEFAULT 0,
  est_duration_ms INTEGER,
  speaker_ratio_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(speaker_ratio_json)),
  outline_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(outline_json)),
      -- 原文 §2.2③ Director 产出：{hook_3s, segments:[{point,visual,mood,est_chars}], cta, est_duration_ms}
  grade           TEXT CHECK (grade IN ('A','B','C')),
  score_total     REAL,
  score_rule      REAL,
  score_llm       REAL,
  revision_round  INTEGER NOT NULL DEFAULT 0,
  editor_notes_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(editor_notes_json)),
      -- 原文 §2.2⑥ 每轮改稿记录「被指出的问题 + 改了什么」
  target_chars    INTEGER NOT NULL DEFAULT 700,               -- 原文 §2.2④ 600–800
  llm_model       TEXT,
  prompt_version  TEXT,
  review_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(review_json)),
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (task_id, version)
);
CREATE UNIQUE INDEX ux_scripts_active ON scripts(task_id) WHERE is_active = 1;
```

```sql
CREATE TABLE script_sentences (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  script_id     TEXT NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,                             -- 1-based 句序（与 s001.wav 对应）

  -- 文本层（编辑与合成解耦）
  text_raw      TEXT NOT NULL,                                -- LLM 原始输出
  text          TEXT NOT NULL,                                -- 生效文本
  tts_text      TEXT,                                         -- 归一化后送 TTS 的最终文本
  subtitle      TEXT,                                         -- 字幕文本（可含手工断行）

  -- 表演层
  speaker       TEXT NOT NULL CHECK (speaker IN ('bigbear','littlebear','narrator')),
  emotion       TEXT NOT NULL DEFAULT 'neutral',
  speed         REAL NOT NULL DEFAULT 1.0 CHECK (speed BETWEEN 0.5 AND 2.0),
  pause_after_ms INTEGER NOT NULL DEFAULT 200,
  emphasis_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(emphasis_json)),

  -- 合成层（★ 断点续传与单句重试核心）
  tts_status    TEXT NOT NULL DEFAULT 'pending' CHECK (tts_status IN (
                  'pending','synthesizing','done','failed','skipped')),
  tts_engine    TEXT,
  tts_voice_id  TEXT,                                         -- 'bigbear' | 'littlebear'
  tts_audio_path TEXT,                                        -- data/output/voice/<task_id>/s007.wav
  tts_duration_ms INTEGER,                                    -- ffprobe 实测（时间轴唯一来源）
  tts_sample_rate INTEGER,
  tts_hash      TEXT,                                         -- ★ 缓存键
  tts_attempts  INTEGER NOT NULL DEFAULT 0,
  tts_error     TEXT,

  -- 时间轴（合成完成后回填）
  start_ms      INTEGER,
  end_ms        INTEGER,

  version       INTEGER NOT NULL DEFAULT 1,                   -- 句级乐观锁
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (script_id, seq)
);

CREATE INDEX idx_sent_task_seq ON script_sentences(task_id, seq);
CREATE INDEX idx_sent_pending  ON script_sentences(task_id, tts_status)
  WHERE tts_status IN ('pending','failed');
CREATE INDEX idx_sent_hash     ON script_sentences(tts_hash);

CREATE TRIGGER trg_sent_touch AFTER UPDATE ON script_sentences FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE script_sentences SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
```

**续传语义（必须由代码遵守）**

1. `tts_status ∈ {pending, failed}` 是唯一"待办"判据。
2. `done` 的句子在**文本未改且 `tts_hash` 命中缓存**时直接复用音频，不调用引擎。
3. 编辑句子 ⇒ `text` 变更 ⇒ `version+1`、`tts_status='pending'`、`tts_hash=NULL`（仅该句失效）。
4. 合成完成后回填前校验 `version` 未变，否则丢弃音频（说明用户又改了）。
5. 单句 `tts_attempts ≥ 3` ⇒ 触发降级链（§04.3.3），最终可落 `skipped`（占位静音 + 字幕保留）。

### 3.3.8 `review_scores`（双通道评分 · 原文 §2.2⑤）

```sql
CREATE TABLE review_scores (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  script_id     TEXT NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
  round_no      INTEGER NOT NULL DEFAULT 1,                  -- 第几轮审稿

  -- 通道一：规则（权重 0.3）—— 原文四项
  rule_total    REAL NOT NULL,
  rule_detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(rule_detail_json)),
      -- {length:{ok,score}, banned_hits:[...], opening_ok, ending_ok,
      --  paragraph_dup_ratio, chars, est_duration_ms}

  -- 通道二：LLM 六维度（权重 0.7）—— 原文六项
  llm_total     REAL NOT NULL,
  llm_detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(llm_detail_json)),
      -- {hook_opening, positioning_fit, oral_style, emotion_rhythm, ending_cta, forbidden}
      -- 每项 {score: 0-10, comment: str}

  total         REAL NOT NULL,                               -- 0.3×rule + 0.7×llm
  grade         TEXT NOT NULL CHECK (grade IN ('A','B','C')),
  decision      TEXT NOT NULL CHECK (decision IN (
                  'pass_auto','need_edit','discard','human_approved','human_rejected')),
  issues_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(issues_json)),   -- 传给 editor
  llm_model     TEXT,
  prompt_version TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (script_id, round_no)
);
CREATE INDEX idx_review_task ON review_scores(task_id, round_no);
```
### 3.3.9 `jobs`（队列与租约 · 四池共用）

```sql
CREATE TABLE jobs (
  id              TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  pool            TEXT NOT NULL CHECK (pool IN ('draft','voice','render','publish')),
  unit_type       TEXT NOT NULL CHECK (unit_type IN (
                    'task','sentence','scene','final','topic_batch','publish')),
  unit_ref        TEXT NOT NULL,                              -- task_id|sentence_id|scene_seq|'final'|platform
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                    'pending','blocked','claimed','succeeded','failed','dead','canceled')),
  priority        INTEGER NOT NULL DEFAULT 100,               -- 数值越小越优先
  depends_on_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(depends_on_json)),
  lease_owner     TEXT,
  lease_expires_at TEXT,
  heartbeat_at    TEXT,
  attempts        INTEGER NOT NULL DEFAULT 0,
  max_attempts    INTEGER NOT NULL DEFAULT 3,
  not_before      TEXT,                                       -- 退避/限频：早于此时间不认领
  payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
  result_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_json)),
  error_code      TEXT,
  error_message   TEXT,
  error_trace     TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  finished_at     TEXT,
  -- ★ 幂等键必须含 task_id：不同任务可有各自的 scene/1、final
  UNIQUE (task_id, pool, unit_type, unit_ref)
);

CREATE INDEX idx_jobs_claim ON jobs(pool, status, priority, not_before, created_at) WHERE status='pending';
CREATE INDEX idx_jobs_lease ON jobs(status, lease_expires_at) WHERE status='claimed';
CREATE INDEX idx_jobs_task  ON jobs(task_id, pool, status);
CREATE INDEX idx_jobs_dead  ON jobs(pool, finished_at) WHERE status='dead';

CREATE TRIGGER trg_jobs_touch AFTER UPDATE ON jobs FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
```

**四池的单元映射**

| 池 | `unit_type` | `unit_ref` | 单元数（单任务） | 幂等键效果 |
| --- | --- | --- | --- | --- |
| draft | `task` | `task_id` | 1 | 一任务一次写稿 |
| draft | `topic_batch` | `batch_id` | 1 | 一批方向/选题只生成一次 |
| voice | `sentence` | `sentence_id` | N（≈20–40 句） | **句子级续传/重试** |
| render | `scene` | `scene_seq` | M（5–9 段） | 场景级缓存与重试 |
| render | `final` | `'final'` | 1 | 合流只做一次 |
| publish | `publish` | `platform`（如 `douyin`） | 每平台 1 | **一任务一平台只发一次** |

### 3.3.10 `template_definitions` / `template_scenes` / `template_components`（三层模板）

```sql
CREATE TABLE template_definitions (
  id            TEXT PRIMARY KEY,                             -- 'tpl_douyin_9x16_default'
  name          TEXT NOT NULL,
  version       INTEGER NOT NULL DEFAULT 1,                   -- 模板变更即 +1（P5 留痕）
  schema_version TEXT NOT NULL DEFAULT '1.0',
  canvas_json   TEXT NOT NULL CHECK (json_valid(canvas_json)),
      -- {width,height,fps,sar,safe_area:{top,bottom,left,right},background}
  duration_policy_json TEXT NOT NULL CHECK (json_valid(duration_policy_json)),
  audio_bus_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(audio_bus_json)),
  randomization_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(randomization_json)),
  output_profile TEXT NOT NULL DEFAULT 'douyin_1080x1920_30fps_v1',
  source_path   TEXT,                                         -- templates/.../template.yaml
  source_sha256 TEXT,
  is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (id, version)
);

CREATE TABLE template_scenes (
  id            TEXT PRIMARY KEY,                             -- 'tpl_x@3:scene_hook'
  template_id   TEXT NOT NULL,
  template_version INTEGER NOT NULL,
  seq           INTEGER NOT NULL,                             -- 1-based 播放顺序
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN
                  ('opening','main','ending','hook','setup','conflict','payload','cta','transition','outro')),
  duration_policy_json TEXT NOT NULL CHECK (json_valid(duration_policy_json)),
      -- {mode:'audio_driven'|'fixed'|'range', fixed_ms, min_ms, max_ms, tail_ms}
  transition_in_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(transition_in_json)),
  transition_out_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(transition_out_json)),
  layer_stack_json TEXT NOT NULL CHECK (json_valid(layer_stack_json)),
      -- 有序层引用：[{component_id, z, bind:{...}, overrides:{...}}]
  repeatable    INTEGER NOT NULL DEFAULT 0 CHECK (repeatable IN (0,1)),
      -- ★ 原文 §4.3：主画面场景可被克隆扩展（repeat_last 策略）
  source_path   TEXT,
  FOREIGN KEY (template_id, template_version)
    REFERENCES template_definitions(id, version) ON DELETE CASCADE,
  UNIQUE (template_id, template_version, seq)
);

CREATE TABLE template_components (
  id            TEXT PRIMARY KEY,                             -- 'tpl_x@3:subtitle_main'
  template_id   TEXT NOT NULL,
  template_version INTEGER NOT NULL,
  name          TEXT NOT NULL,
  type          TEXT NOT NULL CHECK (type IN (
                  'title','subtitle','sticker','bg_video','music','voice',
                  'image','shape','progress','watermark','sticker_pool','group')),
  input_slot    TEXT,                                         -- 逻辑槽位；真实索引由编译器分配
  props_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(props_json)),
  bindings_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(bindings_json)),
  source_path   TEXT,
  FOREIGN KEY (template_id, template_version)
    REFERENCES template_definitions(id, version) ON DELETE CASCADE
);

CREATE INDEX idx_tscene ON template_scenes(template_id, template_version, seq);
CREATE INDEX idx_tcomp  ON template_components(template_id, template_version, type);
```

**组件类型对齐（原文 §4.1 六类 + 工程扩展）**

| 类型 | 原文对应 | 说明 |
| --- | --- | --- |
| `title` | ✅ 标题 | 大字标题层（片头"熊大熊大有话说"） |
| `subtitle` | ✅ 字幕 | 逐句字幕层（与配音句对齐） |
| `sticker` | ✅ 贴图 | 熊大/熊二形象贴图 |
| `bg_video` | ✅ 背景视频 | 我的世界跑酷素材（通配 `parkour_*.mp4`） |
| `music` | ✅ 音乐 | BGM（模板自带） |
| `voice` | ✅ 配音 | 熊大/熊二朗读的句子 |
| `image`/`shape` | ➕ 扩展 | 通用图片与矢量形状（封面/装饰） |
| `progress`/`watermark` | ➕ 扩展 | 进度条 / 账号角标 |
| `sticker_pool`/`group` | ➕ 扩展 | 贴图随机池 / 组件分组 |
### 3.3.11 `artifacts`（产物清单 · 缓存与审计依据）

```sql
CREATE TABLE artifacts (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  sentence_id   TEXT REFERENCES script_sentences(id) ON DELETE SET NULL,
  kind          TEXT NOT NULL CHECK (kind IN (
                  'sentence_audio','voice_master','timeline','scene_video','final_video',
                  'subtitle_ass','filtergraph','manifest','thumbnail','cover','script_json',
                  'publish_evidence','ir_json')),
  path          TEXT NOT NULL,                                -- 相对 STUDIO_DATA_DIR
  bytes         INTEGER,
  sha256        TEXT,
  render_hash   TEXT,                                         -- ★ 缓存键（scene/final）
  meta_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta_json)),
      -- {duration_ms,width,height,fps,codec,encoder,crf,lufs,true_peak,...}
  ttl_hint      TEXT,                                         -- '24h' | '7d' | 'forever'
  expires_at    TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (task_id, kind, path)
);
CREATE INDEX idx_art_task ON artifacts(task_id, kind);
CREATE INDEX idx_art_hash ON artifacts(render_hash) WHERE render_hash IS NOT NULL;
CREATE INDEX idx_art_gc   ON artifacts(expires_at)   WHERE expires_at IS NOT NULL;
```

### 3.3.12 `system_logs`（实时推送与留痕）

```sql
CREATE TABLE system_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,              -- ★ 单调递增 = WS 增量游标 since_id
  ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  level       TEXT NOT NULL CHECK (level IN ('debug','info','warn','error','fatal')),
  source      TEXT NOT NULL,                                  -- 'http'|'pipeline'|'agent.writer'
                                                              -- |'tts.cosyvoice'|'render.ffmpeg'
                                                              -- |'publish.douyin'|'pool.voice'
  task_id     TEXT,
  job_id      TEXT,
  stage       TEXT,                                           -- 'drafting'|'voicing'|'rendering'|'publishing'
  unit_ref    TEXT,                                           -- sentence_id / scene_seq / platform
  message     TEXT NOT NULL,
  payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)),
  worker_id   TEXT,
  trace_id    TEXT,
  duration_ms INTEGER,
  seq_in_task INTEGER,                                        -- 任务内序号（前端按任务回放）
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_logs_task  ON system_logs(task_id, id);
CREATE INDEX idx_logs_ts    ON system_logs(ts);
CREATE INDEX idx_logs_warn  ON system_logs(level, id) WHERE level IN ('warn','error','fatal');
```

**写入契约（`log_service.emit()`）**

```python
async def emit(level, source, message, *, task_id=None, job_id=None, stage=None,
               unit_ref=None, payload=None, worker_id=None, trace_id=None,
               duration_ms=None) -> int:      # 返回 system_logs.id（即 WS 游标）
```

- 必须**先落库再广播**：保证前端断线重连时可按 `since_id` 补齐。
- 高频日志折叠：同类连续日志在 5s 窗口内合并为一条并附 `payload.count`。
- 保留：`debug|info` 90 天后归档；`warn|error|fatal` 永久保留。

### 3.3.13 `approvals` / `task_events` / `llm_calls` / `worker_heartbeats`

```sql
-- 确认闸（P4：全流程唯一人工节点，仅 B 级进入）
CREATE TABLE approvals (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  script_id     TEXT REFERENCES scripts(id) ON DELETE SET NULL,
  status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected','discarded','expired')),
  grade         TEXT,                                         -- 呈现给人工的等级
  score_total   REAL,                                         -- 原文 §2.2⑦ 要求呈现"评分"
  revision_round INTEGER NOT NULL DEFAULT 0,                  -- 原文 §2.2⑦ 要求呈现"修改次数"
  requested_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  decided_at    TEXT,
  decided_by    TEXT,                                         -- 'user' | 'auto_approve_AB'
  comment       TEXT,                                         -- 打回意见（回注给 editor）
  auto_expire_at TEXT                                         -- 默认关闭
);
CREATE INDEX idx_appr_pending ON approvals(status, requested_at) WHERE status='pending';

-- 状态机审计
CREATE TABLE task_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  from_status TEXT,
  to_status   TEXT NOT NULL,
  actor       TEXT NOT NULL,                                  -- 'system'|'worker:voice#1'|'user'
  reason      TEXT,
  detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json)),
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_events_task ON task_events(task_id, id);

-- LLM 调用记账（成本/延迟/失败率）
CREATE TABLE llm_calls (
  id            TEXT PRIMARY KEY,
  task_id       TEXT,
  job_id        TEXT,
  agent         TEXT NOT NULL,     -- 'planner'|'ideator'|'director'|'writer'|'reviewer'|'editor'|'cover'
  engine        TEXT NOT NULL,     -- 'oi_compatible' | 'ollama' | ...
  model         TEXT NOT NULL,
  is_local      INTEGER NOT NULL DEFAULT 0 CHECK (is_local IN (0,1)),
  prompt_version TEXT,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL,
  latency_ms    INTEGER,
  status        TEXT NOT NULL CHECK (status IN ('ok','schema_invalid','http_error','timeout','budget_exceeded')),
  error_message TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_llm_task ON llm_calls(task_id, created_at);
CREATE INDEX idx_llm_cost ON llm_calls(created_at, cost_usd);

-- Worker 心跳与池健康
CREATE TABLE worker_heartbeats (
  worker_id      TEXT PRIMARY KEY,                            -- 'voice#1@<pid>'
  pool           TEXT NOT NULL,
  pid            INTEGER,
  hostname       TEXT,
  status         TEXT NOT NULL CHECK (status IN ('idle','busy','draining','dead')),
  current_job_id TEXT,
  last_seen_at   TEXT NOT NULL,
  gpu_mem_mb     INTEGER,
  cpu_percent    REAL,
  rss_mb         INTEGER,
  started_at     TEXT,
  version        TEXT
);
CREATE INDEX idx_hb_pool ON worker_heartbeats(pool, last_seen_at);
```

### 3.3.14 `broll_clips` / `broll_usage` / `bgm_tracks`（素材库 · 跑酷 + BGM）

```sql
CREATE TABLE broll_clips (
  id            TEXT PRIMARY KEY,
  path          TEXT NOT NULL UNIQUE,                         -- data/assets/mc_parkour/parkour_017.mp4
  sha256        TEXT NOT NULL,
  duration_ms   INTEGER NOT NULL,
  width INTEGER, height INTEGER, fps REAL,
  usable_from_ms INTEGER NOT NULL DEFAULT 0,                  -- 跳过片头/黑帧
  usable_to_ms  INTEGER,
  tags_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),
  has_text      INTEGER NOT NULL DEFAULT 0 CHECK (has_text IN (0,1)),
      -- ★ 画面内是否有文字（有文字时禁止镜像，见 §04.2.4）
  thumb_path    TEXT,
  phash         TEXT,
  frame_hashes_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(frame_hashes_json)),
  license       TEXT NOT NULL CHECK (license IN ('self_recorded','authorized','cc0','purchased')),
  source_url    TEXT,
  proof_path    TEXT,
  licensed_to   TEXT,
  use_count     INTEGER NOT NULL DEFAULT 0,
  last_used_at  TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_clip_enabled ON broll_clips(enabled, duration_ms);

-- 素材使用记录（随机化"近 K 任务不重复"约束的来源）
CREATE TABLE broll_usage (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id   TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  clip_id   TEXT NOT NULL REFERENCES broll_clips(id) ON DELETE CASCADE,
  scene_seq INTEGER,
  in_ms     INTEGER,
  out_ms    INTEGER,
  seed      TEXT,
  used_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (task_id, clip_id)
);
CREATE INDEX idx_usage_recent ON broll_usage(used_at DESC);
CREATE INDEX idx_usage_clip   ON broll_usage(clip_id, used_at DESC);
```

**BGM 素材表**（口述 Q12：BGM **开启**，且**必须可导入素材库**）

> 跑酷素材是**视频**（需要 pHash / 帧哈希 / 镜像禁用），BGM 是**音频**（需要响度 / 可循环标记）—— 字段集不同，故分表，而不是给 `broll_clips` 加可空列。

```sql
CREATE TABLE IF NOT EXISTS bgm_tracks (
  id            TEXT PRIMARY KEY,
  path          TEXT NOT NULL UNIQUE,        -- data/assets/bgm/bgm_003.mp3
  sha256        TEXT NOT NULL,
  duration_ms   INTEGER NOT NULL,
  sample_rate   INTEGER, channels INTEGER, bitrate_kbps INTEGER,
  loudness_lufs REAL,                        -- ★ 入库时 ffmpeg loudnorm 实测；混音前据此预对齐（T3.6）
  bpm           REAL,
  mood          TEXT,                        -- 'upbeat' | 'calm' | 'suspense' | NULL（供 T3.6 选曲）
  tags_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),
  loopable      INTEGER NOT NULL DEFAULT 0 CHECK (loopable IN (0,1)),
  license       TEXT NOT NULL CHECK (license IN ('self_recorded','authorized','cc0','purchased')),
  source_url    TEXT, proof_path TEXT, licensed_to TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  use_count     INTEGER NOT NULL DEFAULT 0,
  last_used_at  TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_bgm_enabled ON bgm_tracks(enabled, duration_ms);
```

| 项 | 规则 |
| --- | --- |
| 入库 | `studio assets ingest --kind bgm --dir <源>`；或 WebUI 素材库直接上传（T4.8） |
| 校验 | 时长 ≥ 15s、可解码、`license` 必填；重复按 `sha256` 拒绝（同跑酷素材） |
| 缺失降级 | **无 BGM 素材 ⇒ 单轨人声静音降级**（§04.2.8.6），**不报错、不阻塞出片**（Q12） |
| 选中 | T3.6 按 `enabled=1` + `mood`/`duration` 过滤后随机抽；「近 K 任务不重复」与跑酷素材同策略 |
---

### 3.3.15 `publications`（发布记录 · 第六部分重建 · §06）

> 一个任务 × 一个平台 × 一个账号 = 一条记录。**幂等键 `sha256(task_id|platform|account_id)` 唯一**（已真机验证：重复插入被拒绝）。

```sql
CREATE TABLE publications (
  id                TEXT PRIMARY KEY,
  task_id           TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  platform          TEXT NOT NULL CHECK (platform IN (
                      'douyin','kuaishou','shipinhao',          -- 一线（一期必做）
                      'xiaohongshu','bilibili','xigua','weibo', -- 二线（一期仅接口）
                      'other')),
  account_id        TEXT NOT NULL,
  profile_key       TEXT,                                       -- 平台输出 profile（如 douyin_9x16_default）

  -- 内容快照（发布时的最终版本，事后可追责）
  video_path        TEXT NOT NULL,
  video_sha256      TEXT NOT NULL,
  cover_path        TEXT,
  title             TEXT NOT NULL,
  caption           TEXT NOT NULL DEFAULT '',
  tags_json         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),

  status            TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
                      'queued','uploading','published','failed','manual_required','canceled')),
  dry_run           INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),

  -- 结果
  url               TEXT,
  platform_post_id  TEXT,
  published_at      TEXT,
  scheduled_at      TEXT,
  next_metric_at    TEXT,                                       -- 数据回收调度点（T+1h/6h/24h/72h）

  -- 重试与错误
  attempt_count     INTEGER NOT NULL DEFAULT 0,
  max_attempts      INTEGER NOT NULL DEFAULT 3,
  error_code        TEXT,   -- PUBLISH_LOGIN_EXPIRED|PUBLISH_RATELIMIT|PUBLISH_SELECTOR_MISS|...
  error_message     TEXT,

  -- 取证与指标（R13/R14：失败可排查、效果可回收）
  evidence_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
      -- {screenshot_path, dom_snapshot_path, stderr_tail, selector_version, platform_text}
  metrics_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metrics_json)),
      -- {views, likes, comments, shares, collected_at, source}
  metrics_history_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(metrics_history_json)),
      -- 时间序列 [{at, views, likes, comments, shares}]（供趋势图）

  idempotency_key   TEXT NOT NULL UNIQUE,                       -- sha256(task_id|platform|account_id)
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  finished_at       TEXT
);

CREATE INDEX idx_pub_task     ON publications(task_id, platform);
CREATE INDEX idx_pub_board    ON publications(status, created_at DESC);
CREATE INDEX idx_pub_account  ON publications(account_id, platform, published_at DESC);
CREATE INDEX idx_pub_metrics  ON publications(next_metric_at) WHERE status='published';
CREATE INDEX idx_pub_manual   ON publications(status, updated_at DESC) WHERE status='manual_required';

CREATE TRIGGER trg_pub_touch AFTER UPDATE ON publications FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE publications SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
```

**发布状态机（独立于任务状态机）**

```
queued ─▶ uploading ─▶ published ─┬─▶ (数据回收循环，不回退状态)
   │          │                   └─▶ metrics_json 持续更新
   │          └─▶ failed ──重试<3──▶ queued（not_before 指数退避）
   │                 └──重试≥3─────▶ manual_required（转"待人工发布"，任务仍 completed）
   └─▶ canceled（人工取消）
```

**限频守卫（`≤3 条/天/账号`，R13）**：认领前必须通过

```sql
-- 返回 1 = 允许发布，0 = 今日额度已用尽（写入 not_before = 次日 00:00 + jitter）
SELECT CASE WHEN (
  SELECT count(*) FROM publications
   WHERE account_id = :account_id
     AND status IN ('uploading','published')
     AND substr(COALESCE(published_at, created_at),1,10) = :today
) < :daily_limit THEN 1 ELSE 0 END;
```

### 3.3.16 `audit_ops`（操作留痕 · 原文 §7.4"一切操作留痕"）

> 与 `system_logs`（技术日志、量大、有 TTL）**职责分离**：`audit_ops` 只记**决策与变更**，量小、永久保留、WebUI 有独立页面。

```sql
CREATE TABLE audit_ops (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  actor         TEXT NOT NULL CHECK (actor IN ('user','system','auto','worker')),
  actor_ref     TEXT,                                           -- 用户名 | 'auto_approve_A' | 'voice#1@20344'
  action        TEXT NOT NULL,                                  -- 'task.approve' | 'pool.pause' | 'template.update' ...
  target_type   TEXT NOT NULL,                                  -- 'task'|'script'|'template'|'pool'|'publication'|'asset'
  target_id     TEXT,
  task_id       TEXT,                                           -- 冗余便于按任务聚合（不加外键，留痕不得被级联删除）
  before_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(before_json)),
  after_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(after_json)),
  result        TEXT NOT NULL DEFAULT 'ok' CHECK (result IN ('ok','denied','error')),
  reason        TEXT,                                           -- 人工填写的意见/理由（确认闸必填项落此）
  request_id    TEXT,                                           -- 与 http 日志关联
  ip            TEXT,
  source        TEXT NOT NULL DEFAULT 'webui' CHECK (source IN ('webui','api','cli','worker','auto'))
);

CREATE INDEX idx_audit_at      ON audit_ops(at DESC);
CREATE INDEX idx_audit_task    ON audit_ops(task_id, at DESC);
CREATE INDEX idx_audit_target  ON audit_ops(target_type, target_id, at DESC);
CREATE INDEX idx_audit_actor   ON audit_ops(actor, at DESC);
```

**必写清单（缺一条即视为 DoD 不达标）**：任务确认/退回/放弃、批量放行、池暂停/恢复、并发调整、模板增改、素材启用/禁用、音色注册/替换、单句重配、换音色、重投死信、发布/取消发布/转人工、GC 与备份执行、`auto_approve_policy` 切换、`publish.enabled` 切换。

### 3.3.17 `pool_settings`（池运行时可调参数 · 唯一持久化的并发真相）

> 并发/暂停**不写配置文件**（配置文件只给默认值）：运行时以本表为准，WebUI 调旋钮即写本表 + `audit_ops`。worker 每轮认领前读取（带 2s 内存缓存）。

```sql
CREATE TABLE pool_settings (
  pool          TEXT PRIMARY KEY CHECK (pool IN ('draft','voice','render','publish')),
  concurrency   INTEGER NOT NULL CHECK (concurrency BETWEEN 0 AND 8),
  paused        INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  paused_at     TEXT,
  paused_by     TEXT,
  lease_sec     INTEGER NOT NULL,                               -- draft 180 / voice 90 / render 600 / publish 600
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  backoff_base_ms INTEGER NOT NULL DEFAULT 2000,
  backoff_max_ms  INTEGER NOT NULL DEFAULT 300000,
  rate_limit_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(rate_limit_json)),
      -- publish: {"daily_limit":3,"min_gap_min":30,"window":"local_day"}
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_by    TEXT,
  consecutive_oom INTEGER NOT NULL DEFAULT 0   -- T4.10：连续 OOM 计数（迁移 0007 追加）
);

-- 初始值（**已移至 `0006_seed.sql`**，本处仅作语义说明）
-- ⚠️ 取值以 `config/pools.yaml`（§01.4.4）为唯一权威，逐字段照抄；两者必须相等
--    （`tests/integration/test_migrations.py::test_pool_settings_matches_pools_yaml` 防漂移）
-- 一期 render 是"单遍合成"（只产 final）⇒ 租约取 600s，**不是** 300s
INSERT INTO pool_settings(pool,concurrency,lease_sec,max_attempts,backoff_base_ms,backoff_max_ms,rate_limit_json) VALUES
  ('draft',   2, 180, 3,  5000,  30000, '{}'),
  ('voice',   1,  90, 3,  3000,  20000, '{}'),
  ('render',  1, 600, 2, 10000,  60000, '{}'),
  ('publish', 1, 600, 3, 60000, 600000, '{"daily_limit":3,"min_gap_min":30,"window":"local_day"}');
```

**并发自动降级（R4 落地 · T4.10）**

| 项 | 取值 | 说明 |
| --- | --- | --- |
| 计数列 | `pool_settings.consecutive_oom` | 池级滚动状态，与 `concurrency` **同表同事务**更新（迁移 `0007_pool_autodegrade.sql`） |
| 计数时机 | `fail()` 且 `error_code ∈ AUTODEGRADE_CODES`（当前只有 `TTS_OOM`） | 与 job 状态变更**同一事务**：分开写会出现「作业重排了但计数器没加」的窗口，而窗口期恰好是连续 OOM 正在发生的时候 |
| 归零时机 | `succeed()`（同事务） | 一条成功 ⇒ 池重新证明自己装得下当前并发 |
| 阈值 | `config/pools.yaml → auto_concurrency.oom_threshold`（默认 2） | 判据是 `== threshold` 而**不是** `>=`：`>=` 会在每一次后续 OOM 上重复告警、重复 −1 |
| 动作 | `concurrency = max(1, concurrency-1)` + `audit_ops(action='pool.autodegrade', actor='auto', source='worker')` + `system.alert(POOL_AUTODEGRADED)` | 已在并发下限时**仍要告警**（文案明说「已在下限」）：静默会把故障伪装成「任务本身有问题」 |
| 开关 | `auto_concurrency.enabled=false` ⇒ 只计数、不降并发 | 面板（T4.10）与 worker 读同一份配置（`load_pools_config`） |
| 人工重投 | **不清零** `consecutive_oom` | 计数器量的是「显存能不能装下」，与某条作业的进度无关 |

> **恢复需人工确认**：自动回升（`recover_after_min`）留给 T4.11 无人值守编排。在那之前，
> 降下去的并发**不会自己涨回来** —— 四池控制台（§04.5.7）上必须写明这一点。
---

### 3.3.18 `publish_schedules`（定时发布计划 · 口述 D7）

> 定时**不是**"占着 worker 空等"，而是"到点才创建 `publish` job"。job 的认领/租约/重试全部走 §3.4。

```sql
CREATE TABLE publish_schedules (
  id            TEXT PRIMARY KEY,
  task_id       TEXT REFERENCES tasks(id) ON DELETE CASCADE,   -- NULL ⇒ 按平台/账号从待发布池取
  platforms_json TEXT NOT NULL CHECK (json_valid(platforms_json)),      -- ["douyin","kuaishou"]
  account_ids_json TEXT NOT NULL CHECK (json_valid(account_ids_json)),  -- D1：支持多账号，默认 1 个

  mode          TEXT NOT NULL CHECK (mode IN ('at_time','daily_window','interval')),
  at_time       TEXT,                                          -- ISO8601（mode=at_time）
  window_start  TEXT,                                          -- '18:00'（mode=daily_window）
  window_end    TEXT,                                          -- '21:30'
  interval_hours INTEGER,                                      -- mode=interval
  jitter_min    INTEGER NOT NULL DEFAULT 15 CHECK (jitter_min BETWEEN 0 AND 120),

  enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  next_run_at   TEXT,                                          -- ★ 持久化，重启不丢
  last_run_at   TEXT,
  run_count     INTEGER NOT NULL DEFAULT 0,
  last_result   TEXT,   -- 'ok' | 'skipped_ratelimit' | 'skipped_disabled' | 'error:…'
  fail_streak   INTEGER NOT NULL DEFAULT 0,                    -- ≥5 ⇒ system.alert

  created_by    TEXT NOT NULL DEFAULT 'user',
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX idx_sched_due     ON publish_schedules(next_run_at) WHERE enabled=1;
CREATE INDEX idx_sched_task    ON publish_schedules(task_id);
CREATE INDEX idx_sched_enabled ON publish_schedules(enabled, next_run_at);

CREATE TRIGGER trg_sched_touch AFTER UPDATE ON publish_schedules FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE publish_schedules SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
```

**调度语义（三条不变量）**

1. **到点才建 job**：调度器（30s tick）只做 `enqueue(publish/publish)`；不预先占坑，**不阻塞** worker。
2. **定时 ≠ 免限频**：§3.4.4 的 `≤3 条/天/账号` 仍然生效；被限频 ⇒ `last_result='skipped_ratelimit'` + 顺延到下一个可用时刻（**不算失败**）。
3. **`publish.enabled=false` 时空转**：不创建 job，记 `last_result='skipped_disabled'`（便于验证调度器本身在工作）。

**窗口模式取时刻算法（默认 `daily_window`，Q14）**

```python
def next_run_in_window(win: tuple[str, str], *, jitter_min: int, seed: str) -> datetime:
    """在 [window_start, window_end] 内取一个**随机**时刻，再叠加 ±jitter_min 抖动。
    - 随机源 = HMAC(seed=schedule_id+日期)，保证同一天多次计算得到同一时刻（幂等）
    - 抖动上限 ≤ jitter_min，且不得越出窗口（越界则夹取到窗口边界）
    - 目的：避免"每天准点 19:00 发布"的机器特征（R13）
    """
```

### 3.3.19 `reports`（数据报告 · 口述 D9）

> 报告 = **聚合结论 + 决策建议**。生成**不调 LLM**（纯 SQL 聚合 + 规则归因），`summary_md` 由模板渲染。

```sql
CREATE TABLE reports (
  id            TEXT PRIMARY KEY,
  period        TEXT NOT NULL CHECK (period IN ('daily','weekly','monthly')),
  start_date    TEXT NOT NULL,                                 -- '2026-09-07'
  end_date      TEXT NOT NULL,                                 -- '2026-09-13'
  generated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  trigger       TEXT NOT NULL DEFAULT 'scheduled' CHECK (trigger IN ('scheduled','manual')),

  -- 聚合结果
  task_count      INTEGER NOT NULL DEFAULT 0,
  publish_count   INTEGER NOT NULL DEFAULT 0,
  total_views     INTEGER,
  total_likes     INTEGER,
  total_comments  INTEGER,
  total_shares    INTEGER,
  avg_views       REAL,
  median_views    REAL,
  llm_cost_usd    REAL,
  tts_skip_ratio  REAL,

  -- 产物与结论
  summary_md    TEXT NOT NULL,                                 -- ★ 人类可读（WebUI 渲染 / 导出）
  data_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data_json)),   -- 供图表
  insights_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(insights_json)), -- ★ 决策建议
  artifacts_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(artifacts_json)), -- 图表/CSV 路径
  applied_count INTEGER NOT NULL DEFAULT 0,                    -- 已被采纳的建议数
  coverage_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(coverage_json)),
      -- 样本覆盖度：{published:12, with_metrics:9, metric_age_hours:[1,6,24]} ⇒ 决定 insights 置信度

  UNIQUE (period, start_date, end_date)                        -- 幂等：同周期重复生成不重复插入
);

CREATE INDEX idx_report_period ON reports(period, start_date DESC);
CREATE INDEX idx_report_recent ON reports(generated_at DESC);
```

**`insights_json` 元素结构（与 §04.6.5.2 的 `Insight` 一致）**

```json
[{"kind":"topic","statement":"数字型钩子在抖音的完播率高于悬念型 23%",
  "evidence":{"n":34,"metric":"play_rate","a":0.41,"b":0.33},
  "confidence":"high","suggested_action":"下轮选题中数字型占比提升至 40%"}]
```

**置信度与样本量（硬规则）**

| 样本量 `n` | `confidence` | WebUI 呈现 |
| --- | --- | --- |
| < 10 | `low` | **必须**显示"样本不足，仅供参考" |
| 10–29 | `medium` | 正常显示 |
| ≥ 30 | `high` | 高亮显示 |

**决策闭环**：`POST /api/v1/reports/{id}/insights/{idx}/apply` ⇒ 写入 `config/persona.yaml` 偏好项或 `content_directions` 权重 ⇒ **影响下轮 Planner**，并写 `audit_ops(action='report.apply_insight')`。

### 3.3.20 `report_schedules`（报告生成周期 · 口述 Q15"周期可编辑"）

> **为何单独建表**：报告周期必须**可编辑**（Q15）且**重启不丢**（陷阱 #30），与 `publish_schedules` 同构 —— 同一个调度器 tick 处理两类计划，只多一个 `kind` 分支。
> 默认 seed：`weekly`（周一 09:00，回看 7 天）+ `monthly`（每月 1 日 09:00，回看上月）；`daily` **默认不启用**。

```sql
CREATE TABLE IF NOT EXISTS report_schedules (
  id            TEXT PRIMARY KEY,
  period        TEXT NOT NULL CHECK (period IN ('daily','weekly','monthly')),
  weekday       INTEGER CHECK (weekday IS NULL OR weekday BETWEEN 0 AND 6),            -- weekly：0=周一（Python weekday）
  day_of_month  INTEGER CHECK (day_of_month IS NULL OR day_of_month BETWEEN 1 AND 28),  -- monthly：≤28 避免月末缺日
  at_time       TEXT NOT NULL DEFAULT '09:00'
                CHECK (length(at_time)=5 AND substr(at_time,3,1)=':'
                       AND CAST(substr(at_time,1,2) AS INTEGER) BETWEEN 0 AND 23
                       AND CAST(substr(at_time,4,2) AS INTEGER) BETWEEN 0 AND 59),
  tz            TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  lookback_days INTEGER NOT NULL DEFAULT 7 CHECK (lookback_days BETWEEN 1 AND 366),

  include_json  TEXT NOT NULL DEFAULT '["publish","metrics","topics","quality","cost"]'
                CHECK (json_valid(include_json)),
  enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  is_builtin    INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0,1)),   -- 内置计划不可删，只能停用/改参数

  next_run_at    TEXT,                                   -- ★ 持久化，重启不丢
  last_run_at    TEXT,
  last_report_id TEXT REFERENCES reports(id) ON DELETE SET NULL,
  last_result    TEXT,   -- 'ok' | 'skipped_disabled' | 'skipped_no_data' | 'error:…'
  fail_streak    INTEGER NOT NULL DEFAULT 0,             -- ≥3 ⇒ system.alert（不阻断生产）

  created_by    TEXT NOT NULL DEFAULT 'user',
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),

  CHECK (period <> 'weekly'  OR weekday      IS NOT NULL),
  CHECK (period <> 'monthly' OR day_of_month IS NOT NULL)
);

-- 每个周期最多 1 个启用中的计划（daily/weekly/monthly 各一条）
CREATE UNIQUE INDEX idx_rsched_period ON report_schedules(period) WHERE enabled=1;
CREATE INDEX idx_rsched_due           ON report_schedules(next_run_at) WHERE enabled=1;

CREATE TRIGGER trg_rsched_touch AFTER UPDATE ON report_schedules FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE report_schedules SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
```

**"可编辑"的边界（Q15）**

| 可编辑 | 不可编辑（保护可追溯性） |
| --- | --- |
| `period` / `weekday` / `day_of_month` / `at_time` / `tz` / `lookback_days` / `include_json` / `enabled` | 已生成报告的内容 —— `reports` 行**只增不改**，重算生成新行 |

- 编辑 ⇒ 立即重算 `next_run_at` + 写 `audit_ops(action='report_schedule.update')`（含 `before/after`）。
- 停用某周期 ⇒ `next_run_at` 置 NULL，调度器不再取到（与 `publish_schedules` 同一套到期查询）。
- **报告失败不阻断生产**：`fail_streak ≥ 3` 仅告警；处置等级低于 `publish` 计划（发布失败影响产出，报告失败只影响洞察）。
- 幂等兜底：`reports` 的 `UNIQUE (period, start_date, end_date)` ⇒ 同日重复触发不会产生重复报告（真机已验证）。

---

### 3.3.21 `voice_profiles`（零样本音色库 · T4.8 新增 · 迁移 `0009`）

> 与 §3.3.14 的 `broll_clips` / `bgm_tracks` 是**同一件事的另一半**：跑酷与 BGM 在
> `0001_init.sql` 里就有表（T4.8 只是把列填满），而音色必须**新建**一张表。

**为什么音色必须建表，跑酷 / BGM 不用**

`0001` 里与音色有关的只有 `tasks.tts_voice_id` 这个自由文本字段 —— 它记的是
「这条任务用了谁」，不是「本机有哪些音色」。§4.3.1 定的是**目录约定**
（`data/voice_src/<voice_id>/{ref_NN.<ext>, ref.txt, profile.json}`），于是音色面板要
回答的三个问题里有两个**落不了地**：

| 问题 | 能不能靠扫目录回答 |
| --- | --- |
| 本机有哪些音色 | ✅ 能（`layout.discover`） |
| 哪些被停用了 | ❌ **不能**。停用是**人的决定**，而 §T4.8 明写「只允许禁用、不物理删除」—— 删掉目录等于连人录的参考音一起删 |
| 每个音色用了几次 / 最后一次什么时候用 | ❌ 不能（T2.6 配音时回填，属于运行期状态） |

后两个必须有地方存，且必须**与文件系统解耦**：素材目录是「人往里丢东西」的地方，
机器算出来的状态混进去，下一次扫描就分不清「这是人写的还是我写的」。

```sql
-- 0009_assets.sql（节选）
CREATE TABLE IF NOT EXISTS voice_profiles (
  id            TEXT PRIMARY KEY,              -- 目录名，即 voice_id（进 tasks.tts_voice_id）
  path          TEXT NOT NULL UNIQUE,          -- data/voice_src/<id>
  ref_count     INTEGER NOT NULL,              -- 参考音段数（§4.3.1：2–3 段）
  total_duration_ms INTEGER NOT NULL,          -- 各段之和（面板按它排序 / 显示）
  sample_rate   INTEGER,                       -- 各段里**最低**的采样率（§4.3.1：≥ 16 kHz）
  peak_db       REAL,                          -- 各段里**最高**的峰值（§4.3.1：≤ −1.0 dBFS）
  text_path     TEXT,                          -- ref.txt（逐字文本；缺 ⇒ NULL，复刻质量打折）
  proof_path    TEXT,                          -- profile.json（来源登记 · R2 合规留档）
  license       TEXT CHECK (license IS NULL OR license IN
                  ('self_recorded','authorized','cc0','purchased')),
      -- 可空：跑酷 / BGM 的 license 是**入库前置**（§3.3.14 明写「license 必填」），
      -- 音色在 §4.3.1 里的前置是「profile.json（来源登记）」，授权类型可能写在里面
      -- 也可能没写 —— 如实允许 NULL，而不是替用户填一个 self_recorded。
  source_url    TEXT,
  licensed_to   TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  use_count     INTEGER NOT NULL DEFAULT 0,
  last_used_at  TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 与 idx_clip_enabled / idx_bgm_enabled 同形：选音色时只吃 enabled = 1 的行。
CREATE INDEX IF NOT EXISTS idx_voice_enabled ON voice_profiles(enabled);
```

**三条设计取舍**

1. **`id` 就是目录名，且不加外键到 `tasks.tts_voice_id`**：配音是异步的，一个音色目录被
   手工改名之后，历史任务仍要查得到「它当时用的是谁」。加了外键就会变成「删一个目录 ⇒
   历史任务报错」（与 §3.3.8 `audit_ops` 不加外键同一条理由）。音色解析失败时的口径也
   不是「回退默认」，而是**明确报错**（§04.5.12 不变量 10）。
2. **不加 `phash` / 帧哈希一类列**：音色的「像不像」判据是说话人嵌入（§4.3.1 的
   「单发言人一致性」），一期**没做**。先加列会得到一堆永远是 `NULL` 的字段，而面板会
   以为自己有这项数据（P4：宁可空着，不做假绿勾）。
3. **`peak_db` / `sample_rate` 存的是「最差的那一段」**：校验判据是「每一段都达标」，
   所以入库时取**最低采样率**与**最高峰值** —— 存平均值会让一条削波的参考音看着像正常。

**与素材目录的边界（重要）**

探测器算出来的结果（段数 / 时长 / 采样率 / 峰值）**只写库，绝不回写 `data/voice_src/`**。
素材目录是用户的地盘：机器往里写一个 `profile.json`，下一次扫描就分不清这份来源登记
是用户填的还是脚本生成的。占位素材的 `profile.json` 由**生成脚本**自己写（`origin: generated`），
那是「造文件」而不是「改资产」。

---

## 3.4 队列契约（DB-as-Queue · 四池共用）

> **一句话**：队列就是 `jobs` 表，没有内存队列、没有 broker。全部并发正确性由 **单条 SQL 的原子性** + **租约** + **`(task_id,pool,unit_type,unit_ref)` 唯一键** 三条机制保证。
> 本节的 SQL 已用本机 `sqlite3 3.45.3`（app venv，`RETURNING` 可用）实测：优先级认领、租约过期回收 + 退避、依赖二级解锁、死信判定、CHECK 拒绝非法状态、`publish` 池入队、发布幂等 —— 全部符合预期。
> **落地状态（T1.5 · 2026-09-13）**：已实现于 `src/studio/db/{queue,lease}.py`，61 条集成用例 + 21 条单测 + 4 条契约测试全绿。实现细节与本节伪码的差异见 **§3.4.7**。

### 3.4.1 `JobStore` 协议（落地：`src/studio/db/queue.py`，唯一允许写 `jobs` 的模块）

```python
class JobStore(Protocol):
    """队列的**唯一**访问面。任何业务代码直接 `UPDATE jobs` 一律由静态检查拒绝
    （tests/contract/test_no_direct_job_write.py）。"""

    async def enqueue(
        self,
        *,
        task_id: str,
        pool: PoolName,
        unit_type: UnitType,
        unit_ref: str,
        priority: int = 100,
        depends_on: list[str] | None = None,
        payload: dict | None = None,
    ) -> str | None:
        """幂等入队：命中 (task_id,pool,unit_type,unit_ref) 则**返回 None 且不重复插入**。
        ⇒ 重复调用安全（"自动触发下游入队"可被多处触发，见原文 §5.2）。"""

    async def claim(self, *, pool: PoolName, worker_id: str, lease_sec: int) -> Job | None:
        """单语句原子认领；返回 None 表示当前无可认领单元（空转，不是错误）。"""

    async def renew(self, *, job_id: str, worker_id: str, lease_sec: int) -> bool:
        """续租。返回 False ⇒ 租约已丢失，worker **必须立即中止**当前单元（丢弃产物）。"""

    async def succeed(self, *, job_id: str, worker_id: str, result: dict) -> bool: ...
    async def fail(
        self, *, job_id: str, worker_id: str, error_code: str, error_message: str, retryable: bool = True
    ) -> JobOutcome:
        """retryable 且 attempts < max_attempts ⇒ 置 pending + not_before 退避；
        否则 ⇒ dead（死信）。"""

    async def unlock_dependents(self, *, pool: PoolName) -> list[str]: ...
    async def reclaim_expired(self, *, pool: PoolName) -> list[str]:
        """sweeper 周期调用（每 15s）。"""

    async def stats(self, *, pool: PoolName) -> PoolStats: ...
    async def dead_letters(self, *, pool: PoolName, limit: int = 100) -> list[Job]: ...
    async def requeue_dead(self, *, job_id: str, actor: str) -> None: ...  # 写 audit_ops
```

> **落地差异（T1.5 施工裁定 27）**：实际签名是**全同步**（与 `TaskService` 一致），每个方法多一个
> 关键字参数 `now: datetime | None = None`（测试注入冻结时间）。协议里的 `async def` 只是「调用点可以
> 在事件循环里 await」的语义占位 —— worker 直接同步调用（单次 <5ms），需要真并发时用
> `asyncio.to_thread` + 每线程独立连接。`lease_sec` 也是可选参数（缺省取 `pool_settings.lease_sec`）。

### 3.4.2 原子认领（核心语句 · 单事务、无读改写窗口）

```sql
-- ① 认领：UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING *
UPDATE jobs
   SET status            = 'claimed',
       lease_owner       = :worker_id,
       lease_expires_at  = strftime('%Y-%m-%dT%H:%M:%fZ','now', '+' || :lease_sec || ' seconds'),
       heartbeat_at      = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
       attempts          = attempts + 1,
       updated_at        = updated_at            -- 保持不变 ⇒ 由触发器统一刷新
 WHERE id = (
   SELECT id FROM jobs
    WHERE pool = :pool
      AND status = 'pending'
      AND (not_before IS NULL OR not_before <= strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    ORDER BY priority ASC, created_at ASC
    LIMIT 1)
RETURNING *;
```

- **为什么不会重复认领**：`UPDATE` 在 SQLite 中是**写事务**，同一时刻只有一个写者（WAL 下读者不阻塞，写者串行）；子查询与更新在**同一语句同一事务**内求值，不存在"两个 worker 都读到同一行再都去改"的窗口。
- **为什么不用 `SELECT` 再 `UPDATE`**：那会产生"读到同一行"的竞态，必须靠 `busy_timeout` 之外的额外锁，是 R10 的根因。
- **空转策略**：返回 0 行 ⇒ 指数退避休眠（`200ms → 2s`，上限 2s），**禁止 busy-loop**（否则 4 个 worker 会吃满 CPU 并加剧写锁竞争）。
- **优先级语义**：`priority` 数值越小越优先。任务创建用 `100`；重试/人工插队用 `50`；批量预热用 `200`。

### 3.4.3 续租 / 回收 / 退避 / 死信

```sql
-- ② 续租（长单元如 final 合流：每 lease/3 秒一次）
UPDATE jobs
   SET lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', '+' || :lease_sec || ' seconds'),
       heartbeat_at     = strftime('%Y-%m-%dT%H:%M:%fZ','now')
 WHERE id = :job_id AND lease_owner = :worker_id AND status = 'claimed'
RETURNING id;   -- 0 行 ⇒ 租约已丢（被 sweeper 回收或人工重投）⇒ worker 必须中止并丢弃产物

-- ③ 回收过期租约（sweeper 每 15s；backoff_ms 由应用层算：min(base*2^(attempts-1), max)）
UPDATE jobs
   SET status      = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
       not_before  = CASE WHEN attempts >= max_attempts THEN NULL
                          ELSE strftime('%Y-%m-%dT%H:%M:%fZ','now', '+' || :backoff_sec || ' seconds') END,
       lease_owner = NULL,
       lease_expires_at = NULL,
       error_code  = 'LEASE_EXPIRED',
       finished_at = CASE WHEN attempts >= max_attempts
                          THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END,
       updated_at  = updated_at
 WHERE status = 'claimed'
   AND lease_expires_at < strftime('%Y-%m-%dT%H:%M:%fZ','now')
RETURNING id, pool, unit_type, unit_ref, attempts, status;

-- ④ 依赖解锁（下游 job 初始为 blocked；上游全部 succeeded 后置 pending）
UPDATE jobs SET status = 'pending', updated_at = updated_at
 WHERE pool = :pool AND status = 'blocked'
   AND id IN (
     SELECT j.id FROM jobs j
      WHERE NOT EXISTS (
        SELECT 1 FROM json_each(j.depends_on_json) dep
         WHERE dep.value NOT IN (SELECT id FROM jobs WHERE status = 'succeeded')))
RETURNING id;
```

| 机制 | 参数 | 说明 |
| --- | --- | --- |
| 租约时长 | draft 180s / voice 90s / render **600s** / publish 600s | 必须 **> 单元 P95 耗时**且 **< 单元硬超时**（否则崩溃后长时间空转） |
| 心跳周期 | `lease_sec / 3` | 与 `worker_heartbeats` 的 5s 心跳**职责不同**：前者续 job 租约，后者报进程存活 |
| 退避 | `min(base × 2^(attempts-1), 300s)` + `jitter(seed)` | 避免同批失败任务同时重试形成尖峰 |
| 死信 | `attempts ≥ max_attempts(3)` ⇒ `dead` | **必须告警**（`system.alert(code='JOB_DEAD')`）并在 WebUI 死信区可见、可一键重投 |
| 任务级失败 | 单元 `dead` ⇒ 任务 `failed`（记 `retry_from = last_healthy_status`）；`attempt_count ≥ 3` ⇒ `manual_pool` | 对齐原文 §5.3"失败重试 3 次 + 超限三路处置" |

### 3.4.4 池控制（暂停 / 并发 / 限频）

```sql
-- ⑤ 暂停：唯一效果是"不再认领"（在途单元照常跑完 ⇒ 不丢进度，对齐原文 §7.3⑥）
SELECT concurrency, paused FROM pool_settings WHERE pool = :pool;
-- worker 认领前读取（2s 内存缓存）；paused=1 或 在跑数 >= concurrency ⇒ 不认领

-- ⑥ 发布限频守卫（publish 池认领前；返回 0 则 not_before = 次日 00:00 + jitter）
SELECT count(*) FROM publications
 WHERE account_id = :account_id
   AND status IN ('uploading','published')
   AND substr(COALESCE(published_at, created_at),1,10) = :today;
```

**暂停语义的三条不变量**

1. `paused=1` **不**改变任何 job 状态、**不**回收租约、**不**中断在途工作 ⇒ 恢复后从断点续跑。
2. `concurrency` 下调**不**杀在途 worker ⇒ 自然收敛到新值。
3. 池暂停/恢复/调参**必须**写 `audit_ops`（`pool.pause` / `pool.resume` / `pool.set_concurrency`）。
4. **自动降并发与 job 状态变更同事务**（判据见 §3.3.17）：`fail()` 里判 `consecutive_oom == oom_threshold` ⇒
   并发 −1 + `system.alert(POOL_AUTODEGRADED)`；`succeed()` 归零。跨事务写会出现「作业重排了但计数器没加」的窗口。
5. 并发上下限**硬编码**：`1 ≤ concurrency ≤ 8`（DDL 的 CHECK 是最后一道保险），其中 `voice ≤ 3` 是
   8GB 显存下的工程上限（`core/config.py::POOL_CONCURRENCY_MAX`）。越界 ⇒ `POOL_CONCURRENCY_LIMIT`（HTTP 422）。

### 3.4.5 单元映射与入队时机（对齐原文 §5.2"完成后自动触发下游入队"）

| 触发点 | 入队内容 | 依赖设置 |
| --- | --- | --- |
| 任务创建（勾选选题） | `draft/task ×1` | — |
| `draft/task` 成功 | `voice/sentence × N` | 无（N 句彼此独立 ⇒ 天然并行） |
| 全部句子 `done/skipped` + `timeline.json` 落盘 | `render/scene × M` | — |
| 全部场景 `succeeded` | `render/final × 1` | `depends_on = [scene_1..scene_M]` |
| `render/final` 成功且审计通过 | `publish/publish × 平台数` | — |
| 发布成功 | 定时器（非队列）：`next_metric_at` 由 `metrics_service` 轮询 | — |

> **注意**：`voice/sentence` 之间**不设依赖**（否则退化成串行）；顺序由 `seq` 在时间轴阶段统一保证。

### 3.4.6 禁止事项（违反即为架构回退）

1. 禁止在 `jobs` 之外维护内存队列（`asyncio.Queue` 只允许作为**进程内唤醒信号**，不得承载状态）。
2. 禁止 `SELECT` + 应用层判断 + `UPDATE` 的认领写法。
3. 禁止在事务内做 I/O（ffmpeg/HTTP/文件读写）。
4. 禁止无 `LIMIT` 的全表扫描式轮询。
5. 禁止把 `succeeded` 的 job 重置为 `pending`（重做必须靠 `render_hash` 缓存失效或新建 job）。
6. 禁止业务层直接写 `tasks.status`（唯一入口 `TaskService.transition()`）。
7. 禁止 `db/queue.py` 反向 import `studio.domain`（依赖方向 `core → db → domain`；池名 / 单元类型一律 `str` 透传，合法性由 DDL `CHECK` 兜底）。

### 3.4.7 落地实现说明（T1.5 · 与上文伪码的差异）

| # | 伪码写法 | 落地写法 | 为什么 |
| --- | --- | --- | --- |
| 1 | SQL 里 `strftime('%Y-%m-%dT%H:%M:%fZ','now')` | **Python 侧算好、绑定参数传入** | ① 单测可冻结时间（`now=` 注入），不必 monkeypatch 全局时钟；② 与 DDL 默认值同为毫秒格式 ⇒ 字典序即时间序；③ 同一方法内多处取时间必然一致 |
| 2 | ③ 回收用一条批量 `UPDATE` | **SELECT-then-UPDATE**，整轮包在一个 `BEGIN IMMEDIATE` 内 | 退避时长按池配置算，一条 SQL 取不到逐行不同的 `base`/`cap`（③ 的注释本身就写「`backoff_ms` 由应用层算」）。规则 2 禁的是**认领**路径，sweeper 不在其列 |
| 3 | 池控制由 worker「认领前读取」 | **`claim()` 内守卫 `paused` / `concurrency`** | 「只有一个地方能忘」：worker 侧那 2s 缓存只是省一次读，正确性由 `claim()` 保证 |
| 4 | ⑥ 用 `substr(COALESCE(...),1,10) = :today` | `substr(datetime(COALESCE(...), '+480 minutes'),1,10)` | DB 存 UTC ⇒ 北京时间 08:00 前发布的会被算进**前一天**，限频形同虚设（`rate_limit_json.window='local_day'` 就是为这个设的） |
| 5 | 死信「必须告警」 | 与 job 状态变更**同事务**插 `system_logs` 行，`payload_json = {code, severity, message, hint}` | 不会出现「状态变了但没告警」或反之；T1.7 的 WS 层按 `payload_json.code` 广播为 `system.alert` |
| 6 | 空转退避 `200ms → 2s` | `lease.poll_delay_ms(empty_rounds)` + `lease.jitter_ms()`（blake2s 确定性抖动） | 多 worker 同批唤醒会形成尖峰；确定性抖动可复现、可断言 |
| 7 | 退避 `min(base × 2^(attempts-1), 300s)` | `min(base << (n-1), backoff_max_ms)` + ≤20% 抖动，`n` 上限 32（防移位溢出） | 封顶值按池配置（draft 30s / voice 20s / render 60s / publish 600s），不是全局 300s |

> **契约测试**：`tests/contract/test_no_direct_job_write.py` 扫描 `src/studio/**/*.py`，
> 只允许 `db/queue.py` 出现 `INSERT INTO jobs` / `UPDATE jobs` / `DELETE FROM jobs` / `REPLACE INTO jobs`；
> 另有反向断言（白名单文件被删 ⇒ 测试立刻红）与 `db ↛ domain` 分层断言。
---

## 3.5 Pydantic 契约模型（`domain/`，与 DDL 一一对应）

> 规则：**DDL 是唯一真相，Pydantic 只做映射与校验**；所有写路径必须经过 Pydantic（`extra="forbid"`），禁止裸 dict 落库。

### 3.5.1 枚举与状态机（`domain/enums.py` / `domain/state_machine.py`）

```python
class TaskStatus(StrEnum):
    PENDING           = "pending"            # 待写稿（已建任务）
    DRAFTING          = "drafting"           # 写稿中（Director+Writer）
    REVIEWING         = "reviewing"          # 审稿中（双通道评分）
    EDITING           = "editing"            # 改稿中（≤2 轮）
    AWAITING_APPROVAL = "awaiting_approval"  # ★ 唯一人工节点（仅 B 级）
    QUEUED_VOICE      = "queued_voice"       # 待配音（句级 job 已入队）
    VOICING           = "voicing"            # 配音中（句级续传）
    QUEUED_RENDER     = "queued_render"      # 待渲染
    RENDERING         = "rendering"          # 渲染中（场景级 + final）
    COMPLETED         = "completed"          # 成片完成（可发布）
    PUBLISHING        = "publishing"         # 发布中
    PUBLISHED         = "published"          # 已发布（终态）
    FAILED            = "failed"             # 失败（可自动重试）
    MANUAL_POOL       = "manual_pool"        # 人工池（反复失败 ≥3 次）
    DISCARDED         = "discarded"          # 废弃（C 级 / 人工放弃）
    CANCELED          = "canceled"           # 已取消

class Grade(StrEnum):     A = "A"; B = "B"; C = "C"
class PoolName(StrEnum):  DRAFT = "draft"; VOICE = "voice"; RENDER = "render"; PUBLISH = "publish"
class UnitType(StrEnum):  TASK = "task"; SENTENCE = "sentence"; SCENE = "scene"
                          FINAL = "final"; TOPIC_BATCH = "topic_batch"; PUBLISH = "publish"
class JobStatus(StrEnum): PENDING = "pending"; BLOCKED = "blocked"; CLAIMED = "claimed"
                          SUCCEEDED = "succeeded"; FAILED = "failed"; DEAD = "dead"; CANCELED = "canceled"
class PublishStatus(StrEnum):
    QUEUED = "queued"; UPLOADING = "uploading"; PUBLISHED = "published"
    FAILED = "failed"; MANUAL_REQUIRED = "manual_required"; CANCELED = "canceled"
```

> **T1.4 落地补充**
>
> 1. `PoolName`(4) 是**作业池**身份（`jobs.pool` / `pool_settings.pool` / `config/pools.yaml`）；
>    `tasks.pool` 的 CHECK 允许 `none`，故另立 `TaskPool`(5) = `PoolName` + `none`，两个类型**不混用**。
> 2. 异常类名按上文原文保留（`IllegalTransition` / `ConcurrentModification`），实现挂在
>    `core.errors.StateTransitionError` 下 —— 本项目没有单独的 `DomainError` 基类。
> 3. `retry_from` 是**动态边**：`ALLOWED_TRANSITIONS` 只存静态半边，
>    `failed` / `manual_pool` → `retry_from` 由 `is_allowed(..., retry_from=)` 判定；
>    白名单外的 `retry_from`（历史脏数据）一律拒绝。

**`ALLOWED_TRANSITIONS`（唯一合法迁移表 · 16×16 全矩阵由测试逐格覆盖）**

| From \ 允许迁移到 | 目标 |
| --- | --- |
| `pending` | `drafting`、`discarded`、`canceled` |
| `drafting` | `reviewing`、`failed`、`discarded`、`canceled` |
| `reviewing` | `editing`、`awaiting_approval`、`queued_voice`（A 级自动放行）、`discarded`（C 级）、`failed`、`canceled` |
| `editing` | `reviewing`、`discarded`、`failed`、`canceled` |
| `awaiting_approval` | `queued_voice`（确认）、`editing`（退回，`revision_round+1`）、`discarded`（放弃）、`canceled` |
| `queued_voice` | `voicing`、`failed`、`canceled` |
| `voicing` | `queued_render`、`failed`、`canceled` |
| `queued_render` | `rendering`、`failed`、`canceled` |
| `rendering` | `completed`、`failed`、`canceled` |
| `completed` | `publishing`、`canceled` |
| `publishing` | `published`、`completed`（发布失败但成片有效 ⇒ 回落，不回退产物）、`failed`、`canceled` |
| `published` | **终态**（不可迁出；发布不可逆） |
| `failed` | `retry_from`、`manual_pool`、`discarded`、`canceled` |
| `manual_pool` | `retry_from`、`discarded`、`canceled` |
| `discarded` | `pending`（人工"捞回"重新走流程） |
| `canceled` | `pending`（人工恢复） |

```python
class IllegalTransition(DomainError):
    """非法迁移。携带 from/to/actor 供 task_events 与排障。"""


def transition(
    self,
    task_id: str,
    to: TaskStatus,
    *,
    actor: str,
    reason: str | None = None,
    expected_version: int | None = None,
) -> TaskRead:
    """★ 任务状态的**唯一写入口**（`TaskService.transition`）。
    1) 读当前 status（可带 expected_version 做乐观锁）
    2) 校验 to ∈ ALLOWED_TRANSITIONS[status]，否则抛 IllegalTransition
    3) to == FAILED  ⇒ 记录 last_healthy_status = status、attempt_count += 1
       to ∈ {FAILED, MANUAL_POOL} 的回退目标必须来自 retry_from 白名单
    4) UPDATE tasks SET status=?, version=version+1 WHERE id=? AND version=?
       ⇒ 0 行 ⇒ 抛 ConcurrentModification（重试）
    5) 同事务写 task_events + system_logs；提交后再广播 WS（先落库后广播）
    6) 到达 queued_voice/queued_render/publishing ⇒ 由 orchestrator 入队（§3.4.5）
    """
```

**`retry_from` 白名单**（防止重试把任务送回非法位置）：`drafting` / `reviewing` / `editing` / `queued_voice` / `voicing` / `queued_render` / `rendering` / `publishing`。

**`TaskService` 的另外两条职责（T1.4 落地）**

1. `tasks.pool` 是 WebUI 的过滤维度，由**状态 → 池**的映射表统一维护（`tasks` 只有这一个写入方，
   不能让调用方顺手改）；
2. `update_progress()` 写 `progress` / `stage_detail` 但**不 bump `version`** —— `version` 只服务于
   状态迁移的乐观锁，进度是高频覆盖写，若也 bump 会让所有持 `version` 的调用方白白重试。

### 3.5.2 分级放行策略

```python
class AutoApprovePolicy(StrEnum):
    OFF = "off"  # A/B 都等人工（最保守）
    GRADE_A = "grade_a"  # ★ 默认：A 级自动放行，B 级进确认闸
    GRADE_AB = "grade_ab"  # 全自动：A+B 都放行（WebUI"一键全自动"）


# 配置：config/app.yaml → approval.auto_approve_policy
# 决策落 tasks.approved_by：'user' | 'auto_approve_A' | 'auto_approve_AB'
```

### 3.5.3 核心 Pydantic 模型（节选 · 与 DDL 字段同序）

```python
class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=60)
    topic: str | None = None
    source_topic_id: str | None = None
    template_id: str | None = None
    priority: int = Field(100, ge=0, le=999)
    kind: Literal["video", "audio_only", "draft_only"] = "video"
    payload: TaskPayload = Field(
        default_factory=TaskPayload
    )  # audience/angle/hook_type/persona_id/target_duration_ms/style/voice_map/seed
    idempotency_key: str | None = None


class TaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: str | None = None
    angle: str | None = None
    hook_type: HookType | None = None  # 建任务时从选题抄一份（已归一化，裁定 80/87）
    persona_id: str | None = None  # 人物可热改 ⇒ 必须记住"这一稿是谁写的"（裁定 87）
    target_duration_ms: int = Field(180_000, ge=60_000, le=180_000)  # 原文"3 分钟以内"
    style: Literal["koubo", "duihua"] = "koubo"
    template_id: str = "douyin_9x16_default"
    voice_map: dict[str, str] = Field(
        default_factory=lambda: {"bigbear": "bigbear", "littlebear": "littlebear"}
    )  # 逻辑角色 → 音色 ID（可换）
    seed: int | None = None


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    kind: str
    title: str
    topic: str | None
    status: TaskStatus
    pool: PoolName
    priority: int
    version: int
    grade: Grade | None
    score_total: float | None
    score_rule: float | None
    score_llm: float | None
    revision_round: int
    progress: float
    stage_detail: str | None
    payload: dict
    context: dict
    quality: QualityReport
    attempt_count: int
    max_attempts: int
    retry_from: TaskStatus | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    approved_at: str | None
    approved_by: str | None


class QualityReport(BaseModel):
    """tasks.quality_json 的强类型视图（QC 结论 + 降级留痕）。"""

    model_config = ConfigDict(extra="forbid")
    av_sync_offset_ms: int | None = None  # |值| ≤ 80ms（§04.2 门禁）
    lufs: float | None = None  # -16 ± 0.5
    true_peak: float | None = None  # ≤ -1.0 dBTP
    phash_distance_avg: float | None = None
    dup_audit_pass: bool | None = None
    degraded: bool = False
    degrade_reason: str | None = None  # 'no_broll_assets'|'tts_unavailable'|'render_720p'|...
    skipped_sentence_ratio: float = 0.0


class SentenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    task_id: str
    script_id: str
    seq: int
    text: str
    tts_text: str | None
    subtitle: str | None
    speaker: Literal["bigbear", "littlebear", "narrator"]
    emotion: str
    speed: float
    pause_after_ms: int
    tts_status: Literal["pending", "synthesizing", "done", "failed", "skipped"]
    tts_audio_path: str | None
    tts_duration_ms: int | None
    tts_hash: str | None
    tts_attempts: int
    tts_error: str | None
    start_ms: int | None
    end_ms: int | None
    version: int


class SentencePatch(BaseModel):
    """编辑单句 ⇒ 仅该句失效（§3.3.7 续传语义第 3 条）。"""

    model_config = ConfigDict(extra="forbid")
    text: str | None = None
    subtitle: str | None = None
    speaker: Literal["bigbear", "littlebear", "narrator"] | None = None
    emotion: str | None = None
    speed: float | None = Field(None, ge=0.5, le=2.0)
    pause_after_ms: int | None = Field(None, ge=0, le=2000)
    expected_version: int


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject", "discard"]
    comment: str | None = None  # reject 时必填（服务层校验）
    actor: str = "user"


class PoolStats(BaseModel):
    pool: PoolName
    pending: int
    blocked: int
    claimed: int
    succeeded: int
    failed: int
    dead: int
    concurrency: int
    running: int
    paused: bool
    oldest_pending_age_sec: int | None
    workers: list[WorkerHeartbeat]


class SystemLogLine(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    at: str
    level: Literal["debug", "info", "warn", "error", "fatal"]
    source: str
    task_id: str | None
    message: str
    payload: dict


class MetricSample(BaseModel):
    at: str
    cpu_pct: float
    ram_mb: int
    gpu_util: float | None
    gpu_mem_mb: int | None
    disk_free_gb: float
    tts_rtf_avg: float | None
    render_fps: float | None


class PublishRequest(BaseModel):  # 契约细节见 §04.6
    model_config = ConfigDict(extra="forbid")
    task_id: str
    platform: str
    account_id: str
    video_path: Path
    cover_path: Path | None
    title: str
    caption: str = ""
    tags: list[str] = Field(default_factory=list)
    scheduled_at: str | None = None
    dry_run: bool = False


class AuditOp(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    at: str
    actor: str
    actor_ref: str | None
    action: str
    target_type: str
    target_id: str | None
    task_id: str | None
    before: dict
    after: dict
    result: str
    reason: str | None
    request_id: str | None
    ip: str | None
    source: str
```

**校验分层（必须在实现中保持）**

| 层 | 职责 | 失败处理 |
| --- | --- | --- |
| Pydantic | 类型/范围/必填/`extra=forbid` | `422`（API）/ `ConfigError`（启动） |
| SQLite `CHECK` | 枚举白名单、区间、JSON 合法性 | 抛错 ⇒ **不静默吞**，落 `error_code` |
| 领域服务 | 状态机合法性、权限、幂等、依赖 | `IllegalTransition` / `Conflict` / `Idempotent` |
| 运行期守卫 | 磁盘/显存/超时/水位 | 降级 + `system.alert` |
---

## 3.6 三层模板 YAML 契约（磁盘为源 → 启动注册进 DB）

> **三层 = 视频模板(Layer 0) → 场景模板(Layer 1) → 组件(Layer 2)**，对应 `template_definitions` / `template_scenes` / `template_components` 三表。
> **磁盘 YAML 是编辑面，DB 是运行面**：启动时扫描 `templates/` → 校验 → 计算 `source_sha256` → upsert 进 DB（内容未变则跳过，`version` 不变）。WebUI 编辑写回 YAML 后再触发注册。
> **禁止**在 YAML 中出现 ffmpeg 语法（`trim=`/`overlay=`/绝对像素链）——那是 IR 层的产物（§04.2）。

### 3.6.1 Layer 0：`templates/<tid>/template.yaml`

```yaml
schema_version: "1.0"                # 未知版本 ⇒ 启动报错（不做兼容猜测）
id: douyin_9x16_default
version: 1                           # 破坏性变更 +1，旧版本保留
name: 抖音竖屏默认模板
description: 片头标题 → 主画面(跑酷+字幕+配音) → 片尾引导

canvas:
  w: 1080
  h: 1920
  fps: 30                            # ★ CFR 固定帧率（音画同步军规，§01.5.3）
  sar: "1:1"
  colorspace: bt709

safe_area:                           # 文字类组件强制裁剪到安全区（§04.2）
  top: 220
  bottom: 420                        # 避开抖音底部交互区
  left: 60
  right: 60

sequence:                            # ★ 视频模板 = 场景序列（原文 §4.1 第一层）
  - scene: 01_opening
    repeat: once
    duration_policy: {mode: fixed, ms: 3000}
  - scene: 02_main
    repeat: repeat_last              # ★ 段落数 > 场景数时克隆最后一个主画面场景（§04.2.3）
    duration_policy: {mode: follow_content, min_ms: 8000, max_ms: 60000}
    sentence_window: auto            # 由实测音频推导，不写死句数
  - scene: 03_ending
    repeat: once
    duration_policy: {mode: fixed, ms: 5000}

audio_bus:                           # 全局音频总线（唯一混音点，§04.2.5）
  voice: {sample_rate: 48000, channels: 1, format: s16, target_lufs: -16.0}
  bgm:   {gain_db: -21, loop: true,
          ducking: {threshold: 0.05, ratio: 8, attack: 20, release: 420}}
  loudness: {target_i: -16.0, target_tp: -1.5, target_lra: 11, two_pass: true}
  limiter: {enabled: true, limit: 0.95}

transitions:                         # 转场池（随机化的取值来源）
  pool: [fade, slideleft, circleopen, dissolve, smoothleft, wipeup]
  duration_ms: {min: 300, max: 700}
  rule: no_consecutive_same           # 禁止连续两次同型

output_profiles:                     # 按平台/降级档取用（§01.5.5、§06.2）
  default:      {w: 1080, h: 1920, fps: 30, vcodec: libx264, crf: 21, preset: medium,
                 pix_fmt: yuv420p, faststart: true, acodec: aac, abitrate: "192k"}
  scene_intermediate: {vcodec: h264_nvenc, rc: vbr, cq: 23, preset: p5, threads: 5}
  fallback_720p: {w: 720, h: 1280, crf: 23}     # 渲染失败保底（原文 §5.5）

assets:
  fonts_dir: assets/fonts
  default_font: SourceHanSansSC-Bold.otf
  images_dir: assets/images
```

### 3.6.2 Layer 1：`templates/<tid>/scenes/02_main.yaml`（主画面 · 最复杂的一个）

```yaml
schema_version: "1.0"
id: 02_main
role: main                            # opening | main | ending
duration_policy:
  mode: follow_content                # fixed | follow_content
  min_ms: 8000
  max_ms: 60000
  tail_ms: 300                        # 场景尾部留白（避免字幕被切）
sentence_window: auto                 # auto | [start_seq, end_seq]
background:                           # 场景底色（素材缺失时的兜底）
  color: "#000000"                    # ★ 无跑酷素材 ⇒ 纯黑 + 纯字幕/标题（§04.2.4）
layers:                               # ★ z 序升序；组件引用 Layer 2
  - {component: bg_mc_parkour,  z: 10, rect: [0, 0, 1080, 1920]}
  - {component: sticker_bear,   z: 30, rect: [760, 1180, 260, 260]}
  - {component: subtitle_main,  z: 40, rect: [60, 1380, 960, 300]}
  - {component: voice_slot,     z: 0,  rect: [0, 0, 0, 0]}   # 音频槽：rect 忽略
  - {component: bgm_slot,       z: 0,  rect: [0, 0, 0, 0]}
```

### 3.6.3 Layer 2：组件六类（`templates/<tid>/components/*.yaml`）

> 组件 `type` 严格限定为原文 §4.1 第三层的**六类**：`title` / `subtitle` / `sticker` / `broll` / `bgm` / `voice`。新增类型必须走 `schema_version` 升级。

```yaml
# ① 字幕（原文 §4.3：自动换行 / 每行限字 / 描边 / 居中）
schema_version: "1.0"
id: subtitle_main
type: subtitle
bind:
  entries: $timeline.sentences         # ★ 时间与文本均来自 timeline.json（不估算）
style:
  font: SourceHanSansSC-Bold.otf
  size: 58
  fill: "#FFFFFF"
  stroke: {color: "#000000", width: 3}
  shadow: {color: "#000000AA", dx: 0, dy: 3, blur: 6}
  align: center
  max_chars_per_line: 13               # ★ 自动换行
  max_lines: 2
  margin_v: 420                        # ≥ safe_area.bottom
  karaoke: {enabled: true, mode: char} # 逐字强调（ASS \k）
  speaker_styles:                      # 说话人差异化（熊大/熊二）
    bigbear:    {fill: "#FFE08A"}
    littlebear: {fill: "#9BE7FF"}
    narrator:   {fill: "#FFFFFF"}

# ② 背景视频（MC 跑酷 · 通配随机 · 防搬运承载点）
---
schema_version: "1.0"
id: bg_mc_parkour
type: broll
source:
  glob: "data/assets/mc_parkour/parkour_*.mp4"   # 原文 §4.2 的通配约定
  pick: random                                   # 由 §04.2.4 RandomizationPlan 决策
  require: {min_duration_ms: 15000, license: [self_recorded, authorized, cc0, purchased]}
fit: cover
constraints:                                     # 硬约束（违反则重抽，不是 warn）
  exclude_recent_tasks: 10                       # 近 K 个任务未使用（broll_usage）
  no_reuse_in_task: true
  avoid_edges_ms: 1500                           # 入点避开首尾 1.5s
  mirror_requires_no_text: true                  # has_text=1 的素材禁止镜像
fallback:
  mode: solid_color                              # ★ 素材为空 ⇒ 纯黑
  color: "#000000"
  degrade_reason: no_broll_assets

# ③ 贴图（熊大/熊二形象）
---
schema_version: "1.0"
id: sticker_bear
type: sticker
source: {kind: image, path: assets/images/bear_a.png}
bind: {speaker: $sentence.speaker}               # 说话人决定用哪张贴图
animation:
  in:  {type: pop_in,  ms: 260}
  out: {type: fade_out, ms: 200}
idle: {type: bob, amplitude_px: 6, period_ms: 1600}   # 轻微浮动，避免"静止贴图"特征

# ④ 音乐（模板自带，全局不变）
---
schema_version: "1.0"
id: bgm_slot
type: bgm
source: {glob: "data/assets/bgm/bgm_*.mp3", pick: random}
constraints: {exclude_recent_tasks: 5, no_reuse_in_task: true}
loop: true
fade: {in_ms: 800, out_ms: 1500}

# ⑤ 配音（按句分配）
---
schema_version: "1.0"
id: voice_slot
type: voice
bind: {segments: $timeline.sentences}
mix: {gain_db: 0, target_lufs: -16.0}
on_missing: {mode: silence, keep_subtitle: true}   # ★ TTS 降级 ⇒ 等长静音 + 保留字幕

# ⑥ 标题（片头）
---
schema_version: "1.0"
id: title
type: title
bind: {text: $script.title, sub: $script.hook}
style:
  font: SourceHanSansSC-Bold.otf
  size: 92
  fill: "#FFFFFF"
  stroke: {color: "#111111", width: 4}
  align: center
  max_chars_per_line: 9
  max_lines: 2
animation:
  in:  {type: slide_up, ms: 420}
  out: {type: fade_out, ms: 300}
```

### 3.6.4 变量命名空间（IR 绑定的唯一合法来源）

| 命名空间 | 来源 | 示例 |
| --- | --- | --- |
| `$script.*` | `scripts` 表当前 active 版本 | `$script.title`、`$script.hook`、`$script.cta` |
| `$sentence.*` | `script_sentences`（当前句） | `$sentence.speaker`、`$sentence.start_ms`、`$sentence.end_ms`、`$sentence.text` |
| `$timeline.*` | `timeline.json`（实测音频时间轴） | `$timeline.total_ms`、`$timeline.sentences` |
| `$persona.*` | `config/persona.yaml` | `$persona.display_name`、`$persona.catchphrases` |
| `$scene.*` | 编译器上下文 | `$scene.seq`、`$scene.duration_ms` |
| `$plan.*` | `RandomizationPlan` | `$plan.transform_plan[3].zoom`、`$plan.bgm_pick.path` |
| `$media.*` | `artifacts` 索引 | `$media.fonts_dir` |

**两条铁律**

1. **被 `repeat_last` 克隆的场景不得含绝对时间绑定**（`$scene.start_ms` 之类一律禁止）—— 时间必须写成 `$sentence.start_ms/end_ms` 相对化，否则克隆后时间重叠（陷阱 #17）。
2. **禁止硬编码 ffmpeg 输入索引**（`[0:v]`）—— 索引由 layout pass 分配，模板只能声明 `input_slot` 语义名。

### 3.6.5 模板校验规则（T3.1 验收 · 六类必须报错）

| # | 校验 | 错误码 |
| --- | --- | --- |
| 1 | 缺必填字段（`canvas.w/h/fps`、`sequence`、`output_profiles.default`） | `TPL_MISSING_FIELD` |
| 2 | 未知组件 `type`（不在六类内） | `TPL_UNKNOWN_COMPONENT_TYPE` |
| 3 | `layers[].component` 引用的组件文件不存在 | `TPL_COMPONENT_NOT_FOUND` |
| 4 | 文字类组件 `rect` 越出 `safe_area` | `TPL_OUT_OF_SAFE_AREA` |
| 5 | 组件内出现 ffmpeg 语法或硬编码输入索引 | `TPL_FORBIDDEN_SYNTAX` |
| 6 | 未知 `schema_version` | `TPL_SCHEMA_VERSION` |
| 7 | `repeat_last` 场景含绝对时间绑定 | `TPL_ABSOLUTE_TIME_IN_REPEATABLE` |
| 8 | `broll.source.glob` 匹配 0 文件（**仅 warn**，走黑屏降级，不阻塞） | `TPL_BROLL_EMPTY`（warn） |
---

## 3.7 迁移策略、备份与数据保留

### 3.7.1 迁移文件（`src/studio/db/migrations/`，纯 SQL、只增不改）

| 文件 | 内容 | 表数 |
| --- | --- | --- |
| `0000_pragmas.sql` | 连接级 PRAGMA（§3.2，**每个连接建立后执行**，不登记版本） | — |
| `0001_init.sql` | `schema_migrations` + 基座表：`personas`、`hot_items`、`feedback_items`、`tasks`、`scripts`、`script_sentences`、`jobs`、`artifacts`、`system_logs`、`approvals`、`task_events`、`llm_calls`、`worker_heartbeats`、`broll_clips`、`broll_usage`、`bgm_tracks` | 17 |
| `0002_topics.sql` | 选题池：`content_directions`、`topic_candidates`、`review_scores` | 3 |
| `0003_templates.sql` | 三层模板：`template_definitions`、`template_scenes`、`template_components` | 3 |
| `0004_publish.sql` | 第六部分重建：`publications`、`audit_ops`、`pool_settings` | 3 |
| `0005_schedule_report.sql` | 口述 D7/D9/Q15：`publish_schedules`、`reports`、`report_schedules` | 3 |
| `0006_seed.sql` | `pool_settings` 初始值（§3.3.17）、**报告周期默认值**（`weekly` 周一 09:00 / `monthly` 每月 1 日 09:00；`daily` 停用）、内置模板注册钩子、`persona` 占位校验 | — |
| `0007_pool_autodegrade.sql` | `pool_settings` 追加 `consecutive_oom`（T4.10 自动降并发计数器，§3.3.17） | — |
| `0008_observability.sql` | 补两个观测索引（T4.12）：`idx_logs_source`（日志按 `source` 过滤 + `ORDER BY id`，§04.5.3）、`idx_events_created`（`task_events` 按保留期 GC，§03.7.5） | — |
| `0009_assets.sql` | 素材库（T4.8）：`voice_profiles` + `idx_voice_enabled`（跑酷 / BGM 的表在 `0001` 里就有，T4.8 只是把列填满 —— 音色**必须**新建表，理由见 §3.3.21） | 1 |

**合计 30 表**（17 + 3 + 3 + 3 + 3 + 1 = 30，与 §3.1 表清单一致）。

### 3.7.2 迁移规则（六条硬约束）

1. **只增不改**：已应用的迁移文件**永不修改**（`checksum` 校验，变更即拒绝启动并提示 `MIGRATION_CHECKSUM_MISMATCH`）。
2. **单文件单事务**：迁移在 `BEGIN IMMEDIATE ... COMMIT` 内执行；失败整体回滚，不留半成品。
3. **幂等**：全部 DDL 使用 `IF NOT EXISTS`；`0006_seed.sql` 使用 `INSERT ... ON CONFLICT DO NOTHING`。连续执行两次结果一致（T1.3 验收项）。
4. **可前滚不可回滚**：不写 down 迁移；回退靠**每日备份还原**（§3.7.4）。
5. **顺序执行 + 版本登记**：按 `version` 字典序执行，每条成功即写 `schema_migrations`。
6. **启动自检**：`studio db check` 断言 `journal_mode=wal`、`foreign_keys=1`、`integrity_check=ok`、`foreign_key_check` 空结果，并把**迁移声明的对象清单**与库内实际做**双向比对**（漏建 / 多建都算 fail）：30 表 / 61 索引 / 6 触发器。

### 3.7.3 改 `CHECK` 约束的标准做法（SQLite 不支持 `ALTER ... CHECK`）

> 本项目**必须**用到该流程（16 态状态机就是一次 `tasks` 重建）。已在本机验证可行。

```sql
PRAGMA foreign_keys = OFF;          -- ★ 必须在事务外（事务内设置无效）
BEGIN IMMEDIATE;
CREATE TABLE tasks_new ( ...新的 CHECK... );           -- ① 新表
INSERT INTO tasks_new SELECT <显式列清单> FROM tasks;   -- ② 拷数据（禁止 SELECT *）
DROP TABLE tasks;                                        -- ③ 删旧表
ALTER TABLE tasks_new RENAME TO tasks;                   -- ④ 改名
-- ⑤ 重建全部索引与触发器（DROP TABLE 会一并删除）
-- ⑥ 重建被引用的外键关系（子表引用的是表名，RENAME 后自动指向新表）
PRAGMA foreign_key_check;                                -- ⑦ 必须为空
COMMIT;
PRAGMA foreign_keys = ON;
```

**注意**：`ALTER TABLE ... RENAME` 后 SQLite 会**自动重写**引用该表的其他表的 FK 定义（`legacy_alter_table=OFF` 默认行为），因此**不要**手动改子表；但**索引与触发器必须手工重建**。

### 3.7.4 备份与恢复

| 项 | 策略 |
| --- | --- |
| 每日备份 | `scripts/backup_db.ps1` → `VACUUM INTO 'data/backups/studio_YYYYMMDD.db'`（**热备安全**，WAL 下不阻塞写） |
| 保留 | 7 份日备 + 4 份周备（周日）；`data/backups/` 计入磁盘水位 |
| 关键节点备份 | 迁移前、批量任务导入前、发布开关开启前（`audit_ops` 记录备份路径） |
| 恢复演练 | **每月一次**（T4.12）：还原到临时库 → `studio db check` → 抽查 3 张表行数 → 记录 `docs/runbook/restore_drill.md` |
| 媒资备份 | 不备份 `data/media/`（体积大）；只备份 DB + `data/output/` + `templates/` + `prompts/` + `config/`（可重建媒资）；**报告文件 `data/output/reports/` 一并备份**（决策依据） |

### 3.7.5 数据保留与 GC（T4.12 落地 · 与 §README.7 容量预算一致）

| 对象 | 保留期 | 清理方式 | 备注 |
| --- | --- | --- | --- |
| `system_logs` | 30 天（`debug` 7 天） | 每日批量 `DELETE ... WHERE at < ?`（分批 ≤5000 行/次，避免长事务） | 前端只查最近 N 条 |
| `llm_calls` | 90 天 | 同上 | 成本面板按自然月聚合后即可删原始行 |
| `task_events` | 90 天 | 同上 | 与 `audit_ops` 分工不同 |
| `audit_ops` | **永久** | 不清理 | 合规留痕 |
| 句子 WAV / `voice_master.wav` | 24 小时 | 任务 `completed` 后延时删除（保留 `tts_hash` 与缓存副本） | 缓存目录独立，见下 |
| 场景中间产物 `scenes/*.mp4` | 7 天 | GC 按 mtime | 失败任务**立即**清理 `.partial` |
| 成片 `final.mp4` / 封面 / 稿件 / 时间轴 / manifest / 滤镜图 | **永久** | 不清理（人工确认后手动归档） | 交付物 |
| TTS 缓存 `data/cache/tts/` | LRU，上限 5 GB | `use_count ≥ 2` 的条目淘汰降权 | 复用率高，是主要提速手段 |
| `broll_usage` | 与任务同生命周期 | 随任务删除级联 | 随机化约束的输入 |
| 热点归档 `data/hot/archive/` | 90 天 | 移动而非删除 | 保留复盘依据 |

**磁盘水位门禁**（与 §README.6/§README.7 一致）：`free_D < 15 GB` ⇒ 暂停 `render`/`publish` 池认领 + `system.alert(code='DISK_LOW')`；`free_C < 1 GB` ⇒ `doctor` 拒绝启动。

---

## 3.8 本节自检清单（实现期对照）

```text
[ ] studio db migrate 连续执行两次结果一致（幂等）
[ ] 表数 = 29；用户索引 = 60；触发器 = 6；integrity_check = ok；foreign_key_check 为空
[ ] journal_mode = wal（每个连接都设置，不只建库时）
[ ] tasks.status 只被 TaskService.transition() 写（静态检查通过）
[ ] jobs 只被 JobStore 写（静态检查通过）
[ ] 认领语句为单条 UPDATE...RETURNING（无 SELECT-then-UPDATE）
[ ] 租约过期回收 + 退避 + 死信 三条路径均有集成测试
[ ] 单句编辑 ⇒ 仅该句 tts_status 回 pending、tts_hash 置空
[ ] 句子重合成后时间轴**全量重算**（禁止增量拼接）
[ ] 发布幂等键 (task_id|platform|account_id) 生效
[ ] 池暂停不改变 job 状态、不回收租约（在途跑完）
[ ] 模板校验八类错误码均可触发
[ ] report_schedules：同周期仅 1 个启用（部分唯一索引）；weekly/monthly 必填字段缺失被 CHECK 拒绝
[ ] report_schedules 编辑后 next_run_at 重算且写 audit_ops；停用后不再被到期查询取到
[ ] 备份还原演练通过（每月）
```
---