-- ==========================================================================
-- 0011_topic_outlines.sql — 二级产物：视频标题 + 核心论点
-- ==========================================================================
-- 文案生成分三级：一级「话题主体」→ 二级「视频标题 + 核心论点」→ 三级「对话文案」。
-- 一级早就在 topic_candidates 里，三级在 scripts 里；二级此前只活在 Director 的
-- 临时输出里（没有独立落点）—— 于是「只想改标题和论点、不想重写全文」这件事在库里
-- 无法表达。这张表就是那一级。
--
-- 一个选题一行（topic_id UNIQUE）：二级产物是「这条视频到底要说什么」的定论，
-- 不是一份可以并存多版的历史（要改就改这一行，留痕在 audit_ops）。
-- 选题被删 ⇒ 它跟着走（ON DELETE CASCADE）。
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS topic_outlines (
  id             TEXT PRIMARY KEY,
  topic_id       TEXT NOT NULL UNIQUE REFERENCES topic_candidates(id) ON DELETE CASCADE,
  title          TEXT NOT NULL,          -- 视频标题（三级成稿时**锁定**用它）
  core_argument  TEXT NOT NULL,          -- 核心论点（一句话；喂给 Director / Writer 当主线）
  llm_model      TEXT,                   -- 模型产出时记下是谁写的；手改的照旧保留原值
  prompt_version TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
