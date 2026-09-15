-- ==========================================================================
-- 0006_seed.sql — 初始数据（幂等：INSERT ... ON CONFLICT DO NOTHING）
-- 来源：docs/spec/03-data-model.md §3.7.1 / §3.3.17 / §3.3.20
-- ==========================================================================
-- 规则（§03.7.2 规则 3）：全部 INSERT 必须幂等 ⇒ 连续执行两次结果一致。
--
-- ⚠️ 取值口径（T1.3 施工裁定，与 §3.3.17 代码块内的旧 INSERT 不一致）：
--    §3.3.17 代码块给的 INSERT 是「最小示例」（backoff 全走 DDL 默认 2000、
--    render lease=300、max_attempts=3），与 §1.4.4 的四池参数表 + 已冻结的
--    config/pools.yaml **互相矛盾**。二者必须只有一个真相，否则 DB 与 YAML
--    会各自为政（违反"无静默失败"）。
--
--    裁决：**以 config/pools.yaml（§1.4.4）为准**，理由：
--      1. §1.4.4 是四池参数的**权威表**，pools.yaml 是它的落地产物（T1.2 冻结）；
--      2. render 租约 300s 是**二期 scene** 的值（§1.4.4「300s / 600s」），
--         一期（C13 单遍合成）只产 final ⇒ 取 600s；
--      3. 若两处不一致，T4.10 的并发旋钮会覆盖 YAML 而 YAML 又覆盖不回 DB，
--         形成"改了没生效"的经典故障。
--
--    防漂移：tests/integration/test_migrations.py 断言本文件取值与
--    config/pools.yaml **逐字段相等**；改 YAML 而忘了改这里 ⇒ 测试立刻红。
--
-- ⚠️ report_schedules.next_run_at 初始为 NULL：
--    调度器（T5.7）必须把 NULL 视为"未排期 ⇒ 立即计算并落库"，
--    不能用 `next_run_at <= now` 一把梭（NULL 与该比较永远为假 ⇒ 永不触发）。
-- --------------------------------------------------------------------------

-- ── 四池运行参数（§3.3.17「唯一持久化的并发真相」）────────────────────────
-- 首次迁移写入初值；此后由 WebUI（T4.10）改写，DB 值优先于 pools.yaml。
INSERT INTO pool_settings
  (pool, concurrency, paused, lease_sec, max_attempts, backoff_base_ms, backoff_max_ms, rate_limit_json, updated_by)
VALUES
  ('draft',   2, 0, 180, 3,  5000,  30000, '{}',                                                    'seed:0006'),
  ('voice',   1, 0,  90, 3,  3000,  20000, '{}',                                                    'seed:0006'),
  ('render',  1, 0, 600, 2, 10000,  60000, '{}',                                                    'seed:0006'),
  ('publish', 1, 0, 600, 3, 60000, 600000, '{"daily_limit":3,"min_gap_min":30,"window":"local_day"}', 'seed:0006')
ON CONFLICT(pool) DO NOTHING;

-- ── 报告生成周期（口述 D9 / Q15「周期可编辑」）──────────────────────────
-- weekly：周一 09:00（weekday=0 ⇒ Python date.weekday() 口径）✅ 启用
-- monthly：每月 1 日 09:00（day_of_month ≤ 28，避免月末缺日）✅ 启用
-- daily：停用（日粒度样本太少，§3.3.20 建议；需要时 WebUI 一键启用）
-- is_builtin=1 ⇒ 不可删，只能停用 / 改参数（§3.3.20）
INSERT INTO report_schedules
  (id, period, weekday, day_of_month, at_time, tz, lookback_days, include_json, enabled, is_builtin, created_by)
VALUES
  ('rsched_weekly',  'weekly',  0,    NULL, '09:00', 'Asia/Shanghai',  7,
   '["publish","metrics","topics","quality","cost"]', 1, 1, 'seed:0006'),
  ('rsched_monthly', 'monthly', NULL, 1,    '09:00', 'Asia/Shanghai', 30,
   '["publish","metrics","topics","quality","cost"]', 1, 1, 'seed:0006'),
  ('rsched_daily',   'daily',   NULL, NULL, '09:00', 'Asia/Shanghai',  1,
   '["publish","metrics","topics","quality","cost"]', 0, 1, 'seed:0006')
ON CONFLICT(id) DO NOTHING;

-- ── 不在此处 seed 的东西（避免与唯一真相源重复）────────────────────────
-- personas      ← config/persona.yaml 是唯一真相（人工可编辑 + 热重载），
--                 由 persona 仓储在启动时 upsert 进表（T1.9）。SQL 里再写一份
--                 必然与热重载打架。
-- template_definitions ← templates/<tid>/template.yaml 是唯一真相（§3.6），
--                 由模板注册器在启动时同步（T3.1 / 二期）。
