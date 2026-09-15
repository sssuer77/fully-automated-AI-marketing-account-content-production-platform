-- ==========================================================================
-- 0004_publish.sql — 发布 + 审计 + 池设置（3 表）
-- 来源：docs/spec/03-data-model.md（§03.3 逐字照抄，仅补 IF NOT EXISTS 以幂等）
-- ==========================================================================
-- §3.7.1：publications / audit_ops / pool_settings。
-- ⚠️ §3.3.17 代码块内的 pool_settings INSERT 已**移至 0006_seed.sql**（§3.7.1 的既定归属），
--    且取值以 config/pools.yaml（§1.4.4 权威参数表）为准，见 0006_seed.sql 顶部说明。
-- --------------------------------------------------------------------------

-- ──────────────────────────────────────────────────────────────────────────
-- publications — 发布记录（§3.3.15）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS publications (
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

CREATE INDEX IF NOT EXISTS idx_pub_task     ON publications(task_id, platform);
CREATE INDEX IF NOT EXISTS idx_pub_board    ON publications(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pub_account  ON publications(account_id, platform, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_pub_metrics  ON publications(next_metric_at) WHERE status='published';
CREATE INDEX IF NOT EXISTS idx_pub_manual   ON publications(status, updated_at DESC) WHERE status='manual_required';

CREATE TRIGGER IF NOT EXISTS trg_pub_touch AFTER UPDATE ON publications FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE publications SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- ──────────────────────────────────────────────────────────────────────────
-- audit_ops — 操作留痕（§3.3.16）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_ops (
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

CREATE INDEX IF NOT EXISTS idx_audit_at      ON audit_ops(at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_task    ON audit_ops(task_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target  ON audit_ops(target_type, target_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor   ON audit_ops(actor, at DESC);

-- ──────────────────────────────────────────────────────────────────────────
-- pool_settings — 池运行时可调参数（§3.3.17）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pool_settings (
  pool          TEXT PRIMARY KEY CHECK (pool IN ('draft','voice','render','publish')),
  concurrency   INTEGER NOT NULL CHECK (concurrency BETWEEN 0 AND 8),
  paused        INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  paused_at     TEXT,
  paused_by     TEXT,
  lease_sec     INTEGER NOT NULL,                               -- draft 180 / voice 90 / render 300 / publish 600
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  backoff_base_ms INTEGER NOT NULL DEFAULT 2000,
  backoff_max_ms  INTEGER NOT NULL DEFAULT 300000,
  rate_limit_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(rate_limit_json)),
      -- publish: {"daily_limit":3,"min_gap_min":30,"window":"local_day"}
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_by    TEXT
);
