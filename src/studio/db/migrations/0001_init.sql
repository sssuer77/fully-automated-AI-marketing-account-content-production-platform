-- ==========================================================================
-- 0001_init.sql — 迁移登记表 + 基座表（17 表）
-- 来源：docs/spec/03-data-model.md（§03.3 逐字照抄，仅补 IF NOT EXISTS 以幂等）
-- ==========================================================================
-- §3.7.1：schema_migrations + personas/hot_items/feedback_items/tasks/scripts/script_sentences/
-- jobs/artifacts/system_logs/approvals/task_events/llm_calls/worker_heartbeats/broll_clips/
-- broll_usage/bgm_tracks = 1 + 16 = 17 表。
-- ⚠️ tasks.template_id 前向引用 0003_templates.sql 的 template_definitions：
--    SQLite 允许 CREATE TABLE 引用尚不存在的表（FK 在 DML 时才校验）；
--    全部迁移跑完后 foreign_key_check 为空，已实测验证。
-- --------------------------------------------------------------------------

-- ──────────────────────────────────────────────────────────────────────────
-- 迁移登记表（版本 / 名称 / sha256 / 耗时）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,                      -- '0001'
  name        TEXT NOT NULL,
  checksum    TEXT NOT NULL,                         -- 文件 sha256，防偷改历史迁移
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms INTEGER
);

-- ──────────────────────────────────────────────────────────────────────────
-- personas — 频道定位（唯一人工必填，§3.3.1）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS personas (
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
CREATE UNIQUE INDEX IF NOT EXISTS ux_persona_active ON personas(is_active) WHERE is_active = 1;

-- ──────────────────────────────────────────────────────────────────────────
-- hot_items — 热点（§3.3.2）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hot_items (
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
CREATE INDEX IF NOT EXISTS idx_hot_unused   ON hot_items(created_at DESC) WHERE consumed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_hot_platform ON hot_items(platform, created_at DESC);

-- ──────────────────────────────────────────────────────────────────────────
-- feedback_items — 历史反馈（§3.3.3）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS feedback_items (
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
CREATE INDEX IF NOT EXISTS idx_fb_sentiment ON feedback_items(sentiment, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_fb_auto      ON feedback_items(is_auto, created_at DESC);

-- ──────────────────────────────────────────────────────────────────────────
-- tasks — 主任务表 · 16 态状态机（§3.3.6）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
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
      -- 输入：{audience, angle, target_duration_ms, style, template_id, voice_map, seed}
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

CREATE INDEX IF NOT EXISTS idx_tasks_board       ON tasks(status, priority, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_claim       ON tasks(pool, status, priority, created_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_tasks_lease       ON tasks(status, lease_expires_at) WHERE lease_expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_created_day ON tasks(substr(created_at,1,10));
CREATE INDEX IF NOT EXISTS idx_tasks_grade       ON tasks(grade, status);
CREATE INDEX IF NOT EXISTS idx_tasks_topic       ON tasks(source_topic_id);
CREATE INDEX IF NOT EXISTS idx_tasks_manual      ON tasks(status, finished_at) WHERE status='manual_pool';

CREATE TRIGGER IF NOT EXISTS trg_tasks_touch AFTER UPDATE ON tasks FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE tasks SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- ──────────────────────────────────────────────────────────────────────────
-- scripts — 稿件（§3.3.7）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scripts (
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
CREATE UNIQUE INDEX IF NOT EXISTS ux_scripts_active ON scripts(task_id) WHERE is_active = 1;

-- ──────────────────────────────────────────────────────────────────────────
-- script_sentences — 逐句配音 · 断点续传核心（§3.3.7）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS script_sentences (
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

CREATE INDEX IF NOT EXISTS idx_sent_task_seq ON script_sentences(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_sent_pending  ON script_sentences(task_id, tts_status)
  WHERE tts_status IN ('pending','failed');
CREATE INDEX IF NOT EXISTS idx_sent_hash     ON script_sentences(tts_hash);

CREATE TRIGGER IF NOT EXISTS trg_sent_touch AFTER UPDATE ON script_sentences FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE script_sentences SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- ──────────────────────────────────────────────────────────────────────────
-- jobs — 队列与租约 · 四池共用（§3.3.9）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
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

CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(pool, status, priority, not_before, created_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, lease_expires_at) WHERE status='claimed';
CREATE INDEX IF NOT EXISTS idx_jobs_task  ON jobs(task_id, pool, status);
CREATE INDEX IF NOT EXISTS idx_jobs_dead  ON jobs(pool, finished_at) WHERE status='dead';

CREATE TRIGGER IF NOT EXISTS trg_jobs_touch AFTER UPDATE ON jobs FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

-- ──────────────────────────────────────────────────────────────────────────
-- artifacts — 产物清单 · 缓存与审计依据（§3.3.11）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS artifacts (
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
CREATE INDEX IF NOT EXISTS idx_art_task ON artifacts(task_id, kind);
CREATE INDEX IF NOT EXISTS idx_art_hash ON artifacts(render_hash) WHERE render_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_art_gc   ON artifacts(expires_at)   WHERE expires_at IS NOT NULL;

-- ──────────────────────────────────────────────────────────────────────────
-- system_logs — 实时推送与留痕（§3.3.12）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_logs (
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
CREATE INDEX IF NOT EXISTS idx_logs_task  ON system_logs(task_id, id);
CREATE INDEX IF NOT EXISTS idx_logs_ts    ON system_logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_warn  ON system_logs(level, id) WHERE level IN ('warn','error','fatal');

-- ──────────────────────────────────────────────────────────────────────────
-- approvals / task_events / llm_calls / worker_heartbeats（§3.3.13）
-- ──────────────────────────────────────────────────────────────────────────
-- 确认闸（P4：全流程唯一人工节点，仅 B 级进入）
CREATE TABLE IF NOT EXISTS approvals (
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
CREATE INDEX IF NOT EXISTS idx_appr_pending ON approvals(status, requested_at) WHERE status='pending';

-- 状态机审计
CREATE TABLE IF NOT EXISTS task_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  from_status TEXT,
  to_status   TEXT NOT NULL,
  actor       TEXT NOT NULL,                                  -- 'system'|'worker:voice#1'|'user'
  reason      TEXT,
  detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json)),
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);

-- LLM 调用记账（成本/延迟/失败率）
CREATE TABLE IF NOT EXISTS llm_calls (
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
CREATE INDEX IF NOT EXISTS idx_llm_task ON llm_calls(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_cost ON llm_calls(created_at, cost_usd);

-- Worker 心跳与池健康
CREATE TABLE IF NOT EXISTS worker_heartbeats (
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
CREATE INDEX IF NOT EXISTS idx_hb_pool ON worker_heartbeats(pool, last_seen_at);

-- ──────────────────────────────────────────────────────────────────────────
-- broll_clips / broll_usage — 跑酷素材库（§3.3.14）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broll_clips (
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
CREATE INDEX IF NOT EXISTS idx_clip_enabled ON broll_clips(enabled, duration_ms);

-- 素材使用记录（随机化"近 K 任务不重复"约束的来源）
CREATE TABLE IF NOT EXISTS broll_usage (
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
CREATE INDEX IF NOT EXISTS idx_usage_recent ON broll_usage(used_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_clip   ON broll_usage(clip_id, used_at DESC);

-- ──────────────────────────────────────────────────────────────────────────
-- bgm_tracks — BGM 素材库（§3.3.14 · Q12 可导入）
-- ──────────────────────────────────────────────────────────────────────────
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
CREATE INDEX IF NOT EXISTS idx_bgm_enabled ON bgm_tracks(enabled, duration_ms);
