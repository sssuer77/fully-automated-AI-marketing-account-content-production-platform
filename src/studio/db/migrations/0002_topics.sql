-- ==========================================================================
-- 0002_topics.sql — 选题池（3 表）
-- 来源：docs/spec/03-data-model.md（§03.3 逐字照抄，仅补 IF NOT EXISTS 以幂等）
-- ==========================================================================
-- §3.7.1：content_directions / topic_candidates / review_scores。
-- --------------------------------------------------------------------------

-- ──────────────────────────────────────────────────────────────────────────
-- content_directions — 方向（Planner 产出 5–8 个，§3.3.4）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS content_directions (
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
CREATE INDEX IF NOT EXISTS idx_dir_batch ON content_directions(batch_id, seq);

-- ──────────────────────────────────────────────────────────────────────────
-- topic_candidates — 选题池（每方向 4 个，§3.3.5）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS topic_candidates (
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
CREATE INDEX IF NOT EXISTS idx_topic_pool  ON topic_candidates(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_topic_dir   ON topic_candidates(direction_id, seq);
CREATE INDEX IF NOT EXISTS idx_topic_dedup ON topic_candidates(dedup_hash);

-- ──────────────────────────────────────────────────────────────────────────
-- review_scores — 双通道评分 0.3/0.7（§3.3.8）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS review_scores (
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
CREATE INDEX IF NOT EXISTS idx_review_task ON review_scores(task_id, round_no);
