-- ==========================================================================
-- 0009_assets.sql — 音色档案表（T4.8 · §3.3.14 / §4.3.1）
-- 来源：docs/spec/03-data-model.md §3.3.14 + docs/spec/04-contracts.md §4.3.1
-- ==========================================================================
-- 为什么跑酷 / BGM 不加表，音色要加
-- --------------------------------
-- `broll_clips` 与 `bgm_tracks` 在 0001_init.sql 里就有了（§3.3.14 原始设计），
-- 所以 T4.8 对它们是"把已有列填满"；而音色在 0001 里**只有** `tasks.tts_voice_id`
-- 这个自由文本字段 —— 它记的是"这条任务用了谁"，不是"本机有哪些音色"。
--
-- §4.3.1 定的是目录约定（`data/voice_src/<voice_id>/{ref_01.wav, ref.txt, profile.json}`），
-- 于是 T4.8 的音色面板要回答的问题变成三个，而它们**都落不了地**：
--   ① 本机有哪些音色（目录扫描，能算）；
--   ② 哪些被停用了（**这是人的决定，不能靠"目录还在不在"表达** —— §T4.8 明写
--      "只允许禁用、不物理删除"，删掉目录等于连人写的参考音一起删）；
--   ③ 每个音色用了几次、最后一次什么时候用（T2.6 配音时回填）。
-- ②③ 必须有地方存，且必须**与文件系统解耦**：素材目录是"人往里丢东西"的地方，
-- 机器算出来的状态混进去，下一次扫描就分不清"这是人写的还是我写的"。
--
-- 为什么 id 就是目录名（不加外键到 tasks.tts_voice_id）
-- ---------------------------------------------------
-- `tasks.tts_voice_id` 是 TEXT 且**刻意不加外键**：配音是异步的，一个音色目录被
-- 手工改名之后，历史任务仍要查得到"它当时用的是谁"。加了外键就会变成
-- "删一个目录 ⇒ 历史任务报错"（与 §03.3.8 `audit_ops` 不加外键同一条理由）。
-- 音色解析失败时的口径也不是"回退默认"，而是**明确报错**（T4.8 裁定）。
--
-- 为什么不加 phash / 帧哈希一类的列
-- ---------------------------------
-- 音色的"像不像"判据是说话人嵌入（§4.3.1 的"单发言人一致性"），一期没做；
-- 先加列会得到一堆永远是 NULL 的字段，而面板会以为自己有这项数据。
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS voice_profiles (
  id            TEXT PRIMARY KEY,              -- 目录名，即 voice_id（进 tasks.tts_voice_id）
  path          TEXT NOT NULL UNIQUE,          -- data/voice_src/<id>
  ref_count     INTEGER NOT NULL,              -- 参考音段数（§4.3.1：2–3 段）
  total_duration_ms INTEGER NOT NULL,          -- 各段之和（面板按它排序 / 显示）
  sample_rate   INTEGER,                       -- 各段里**最低**的采样率（§4.3.1：≥ 16 kHz）
  peak_db       REAL,                          -- 各段里**最高**的峰值（§4.3.1：≤ −1.0 dBFS）
  text_path     TEXT,                          -- ref.txt（逐字文本；缺 ⇒ NULL，面板提示复刻质量打折）
  proof_path    TEXT,                          -- profile.json（来源登记 · R2 合规留档）
  license       TEXT CHECK (license IS NULL OR license IN
                  ('self_recorded','authorized','cc0','purchased')),
      -- 可空：跑酷 / BGM 的 license 是**入库前置**（§3.3.14 明写"license 必填"），
      -- 音色在 §4.3.1 里的前置是"profile.json（来源登记）"，授权类型可能写在里面
      -- 也可能没写 —— 这里如实允许 NULL，而不是替用户填一个 self_recorded。
  source_url    TEXT,
  licensed_to   TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  use_count     INTEGER NOT NULL DEFAULT 0,
  last_used_at  TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 与 idx_clip_enabled / idx_bgm_enabled 同形：随机化选音色时只吃 enabled = 1 的行。
CREATE INDEX IF NOT EXISTS idx_voice_enabled ON voice_profiles(enabled);
