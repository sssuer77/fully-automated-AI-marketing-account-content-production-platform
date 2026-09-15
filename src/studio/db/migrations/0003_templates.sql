-- ==========================================================================
-- 0003_templates.sql — 三层模板（3 表）
-- 来源：docs/spec/03-data-model.md（§03.3 逐字照抄，仅补 IF NOT EXISTS 以幂等）
-- ==========================================================================
-- §3.7.1：template_definitions / template_scenes / template_components。
-- ⚠️ 一期（C13 单遍合成）不读这三张表；保留 DDL 供二期 P1 使用，不阻塞 M3。
-- --------------------------------------------------------------------------

-- ──────────────────────────────────────────────────────────────────────────
-- 三层模板定义（§3.3.10）
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS template_definitions (
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

CREATE TABLE IF NOT EXISTS template_scenes (
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

CREATE TABLE IF NOT EXISTS template_components (
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

CREATE INDEX IF NOT EXISTS idx_tscene ON template_scenes(template_id, template_version, seq);
CREATE INDEX IF NOT EXISTS idx_tcomp  ON template_components(template_id, template_version, type);
