-- ==========================================================================
-- 0005_schedule_report.sql — 定时发布 + 数据报告（3 表）
-- 来源：docs/spec/03-data-model.md（§03.3 逐字照抄，仅补 IF NOT EXISTS 以幂等）
-- ==========================================================================
-- §3.7.1：publish_schedules / reports / report_schedules。
-- 口述 D7（定时任务）/ D9（整合报告）/ Q15（周期可编辑）。
-- --------------------------------------------------------------------------

-- ──────────────────────────────────────────────────────────────────────────
-- publish_schedules — 定时发布计划（§3.3.18）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS publish_schedules (
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

CREATE INDEX IF NOT EXISTS idx_sched_due     ON publish_schedules(next_run_at) WHERE enabled=1;
CREATE INDEX IF NOT EXISTS idx_sched_task    ON publish_schedules(task_id);
CREATE INDEX IF NOT EXISTS idx_sched_enabled ON publish_schedules(enabled, next_run_at);

CREATE TRIGGER IF NOT EXISTS trg_sched_touch AFTER UPDATE ON publish_schedules FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE publish_schedules SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- ──────────────────────────────────────────────────────────────────────────
-- reports — 数据报告（§3.3.19）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reports (
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

CREATE INDEX IF NOT EXISTS idx_report_period ON reports(period, start_date DESC);
CREATE INDEX IF NOT EXISTS idx_report_recent ON reports(generated_at DESC);

-- ──────────────────────────────────────────────────────────────────────────
-- report_schedules — 报告生成周期（§3.3.20）
-- ──────────────────────────────────────────────────────────────────────────
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_rsched_period ON report_schedules(period) WHERE enabled=1;
CREATE INDEX IF NOT EXISTS idx_rsched_due           ON report_schedules(next_run_at) WHERE enabled=1;

CREATE TRIGGER IF NOT EXISTS trg_rsched_touch AFTER UPDATE ON report_schedules FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE report_schedules SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
