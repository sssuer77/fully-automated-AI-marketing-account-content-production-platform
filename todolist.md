# 施工 Todolist · AI 全自动营销号制片台

> 依据：`docs/spec/`（**v3.2 终版 · 需求已冻结**）｜ 生成日期：2026-09-13 ｜ 最近更新：2026-09-18（**T5.7 数据报告与决策闭环 ✅ —— M5 只剩多账号**）（**T1.1–T1.12 全部 ✅** + **T4 已完成 14/14（T4 齐了）**：T4.1 前端脚手架 ✅ · T4.2 总览台 ✅ · T4.3 选题面板 ✅ · T4.4 稿件面板 + 确认闸 ✅ · **T4.6 渲染面板 ✅** · T4.7 合成配置面板 ✅ · **T4.8 素材库 ✅** · T4.9 实时日志 ✅ · T4.10 四池调度控制台 ✅ · T4.11 无人值守 ✅ · T4.12 观测/备份/交付 ✅ · T4.13 人物库面板 ✅ · **T4.5 配音面板 ✅** · **T4.14 四屏端到端串联 ✅**（T4 14/14 齐）** + **T2.5 文本归一化与切分 ✅（2026-09-15）** + **T2.6 按句合成流水线 + 句级缓存 ✅（2026-09-16）** + **T2.7 时长时间轴 ✅（2026-09-16）** + **T2.8 配音阶段编排与降级演练 ✅（2026-09-16）** + **T2.9 配音服务化操作接口 ✅（2026-09-16）** + **T4.5 配音面板 ✅（2026-09-16）** + **T4.14 四屏端到端串联 ✅（2026-09-16）** + **T5.2 发布适配层与 profile ✅（2026-09-16）**）
> 本文件是**唯一施工执行入口**：把规格书里 50 个原子任务拆成可勾选的子项。
> 规格书回答"**为什么这样做**"，本文件回答"**现在做什么、怎么算做完**"。

---

## 图例与用法

**优先级分级**

| 级别 | 含义 | 处理原则 |
| --- | --- | --- |
| **P0** | 阻塞主链路，不做则下游全部卡住 | 串行优先，先做完再开新任务 |
| **P1** | 必要，但不阻塞主链路 | 可与 P0 并行 |
| **P2** | 可延后 / 二期 | 一期不做，不占工期 |

**依赖标记**

- `依赖：—` 可立即开工
- `依赖：T1.5` 必须等该任务**验收通过**后才开工（不是"写完"，是"验收通过"）
- `🔴 外部阻塞` 需要你提供素材/账号/密钥，有 lead time

**每个任务的字段**

```
### T编号 名称 · 优先级
- 依赖 / 里程碑 / 契约条款
- [ ] 子项（可独立勾选）
- ✅ 验收命令
- ⚠️ 降级预案
```

**勾选规则**：子项全勾 **且** ✅ 验收命令跑通 ⇒ 该任务才算完成。只写完代码不算。

---

## 0. 前置阻塞项（🔴 有 lead time，今天就启动）

这些不在 50 个任务里，但**不准备就会在某个时刻卡住**。按"最晚需要日"排序。

| # | 项 | 放置位置 | 最晚需要 | 状态 |
| --- | --- | --- | --- | --- |
| E1 | 🔴 **MC 跑酷素材包**（≥60 条 / ≥30 分钟） | `data/assets/mc_parkour/` | **T3.1 开工前** | 缺失（`D:\MC` 空） |
| E2 | 🔴 **水印 PNG**（1080×1920 适配，带透明通道） | `templates/<tid>/assets/images/watermark.png` | **T3.2 开工前** | 缺失 |
| E3 | 🔴 **BGM 音乐库**（≥20 首授权曲） | `data/assets/bgm/` 或 WebUI 上传 | T3.6 开工前 | 缺失（`D:\MUSIC` 空） |
| E4 | 🔴 **熊大熊二原声**（各 2–3 段，10–30s，**无 BGM**） | `data/voice_src/{bigbear,littlebear}/` | **T2.4 开工前** | 占位已就位（2026-09-17 · `scripts/seed_placeholder_assets.py`，**开箱即用**）· **正式原声仍缺** |
| E5 | ✅ **CosyVoice 权重**（2–4 GB） | `models/` 或 `D:\ai_models` | **T2.1 开工前** | **已就位（2026-09-17）** —— `D:\ai_models\modelscope_cache\models\iic--CosyVoice2-0.5B\snapshots\master`，**21 文件 / 5.23 GB**（与远端清单逐条一致）；真机加载 **10.4s / 显存 2.38 GB** |
| E6 | ✅ **LLM API Key**（[OI] 兼容） | `config/secrets.yaml` **或**环境变量 `STUDIO_LLM_API_KEY`（**env 优先**） | **T1.9 真机联调前** | **已可由面板配置（2026-09-17 · T6.1）** —— 打开「设置」面板填 key 即生效，**不改代码、不重启**；真 key 仍未填 ⇒ `studio llm probe` 报 `no_key`，其余施工照常 |
| E7 | 🟡 **`config/persona.yaml`**（人设/口吻/受众/口癖/禁区） | `config/persona.yaml` | **随时**（不阻塞） | 缺失（**唯一人工必填**）· T1.9 / T1.10 均已用 `personas/persona_default.yaml` 现值开工；改人物文件即生效（阈值经 `ScriptRules.from_persona`），历史批次可按 `prompt_version` 追溯 |
| E8 | 🟡 字体文件（中文字幕用） | `templates/<tid>/assets/fonts/` | T3.5 开工前 | 待确认 |

> **不阻塞开发**：E1–E5 缺失时链路仍可跑通（黑屏降级 §04.2.8.6 / 字幕模式降级 §04.3.3），素材到位后自动提升画质。
> **E5 已解除（2026-09-17）** —— 至此**没有任何外部项是硬阻塞**：权重到位 ⇒ 配音可走 CosyVoice（Windows SAPI 保留为降级档）。水印缺失 ⇒ **跳过水印照常出片**（可选装饰）；跑酷素材缺失 ⇒ 出纯黑底片；E6 未填 ⇒ 面板与 CLI **如实报 `no_key`**，不挡其余链路。

---

## 1. 阶段 T1 · 基座 + 智能体脚手架 + 选题池（12 任务 → 门禁 M1）

### T1.1 仓库骨架 + 双 venv + `doctor` · **P0** ✅ **已完成（2026-09-13）**
- 依赖：— ｜ 里程碑：M1 ｜ 契约：§02.2 / §README.6
- [x] 建目录树：`src/studio/`、`workers/`、`web/`、`config/`、`prompts/`、`templates/`、`data/`、`schemas/`、`tests/`、`scripts/`、`ops/`、`docs/`
- [x] `uv` 建双 venv：app = **Python 3.12.6**，tts = **Python 3.11.15**（锁 3.11 才能用 `D:\Torch` 的 cp311 wheel）
- [x] 环境变量重定向 **11 项**：`TEMP`/`TMP`、`HF_HOME`、`MODELSCOPE_CACHE`、`TORCH_HOME`、`UV_CACHE_DIR`、`PIP_CACHE_DIR`、`PLAYWRIGHT_BROWSERS_PATH`、`NPM_CONFIG_CACHE`、`STUDIO_HOME`、`STUDIO_DATA_DIR`（全部离开系统盘；`NPM_CONFIG_CACHE` 由 T4.1 追加，见裁定 111）
- [x] `studio doctor` 逐项断言 **16 项**：FFmpeg 版本 + 9 个必需滤镜在位、NVENC **实编码探测**、字体、`free_C`/`free_D`、DB 可写 + `journal_mode=wal`、环境变量契约、RAM、GPU、Node、uv 缓存落盘
- [x] 磁盘门禁硬约束：`free_C < 1GB` 或 `free_D < 15GB` ⇒ **拒绝启动**（`DoctorReport.raise_if_blocked`）
- [x] `.gitignore`：`data/`、`models/`、`*.db`、`.venv*`、`web/node_modules/`、密钥
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  uv run studio doctor --json   # ok=true · 16 项检查 · 0 阻塞 · 2 warn（项目字体缺失 / RAM 15.82 GB）
  uv run python -V              # Python 3.12.6
  uv run --project tts python -c "import torch;print(torch.__version__,torch.cuda.is_available())"
                                # 2.4.0+cu121 True
  powershell -File tasks.ps1 check   # ruff format/check + mypy strict + pytest 全绿
  ```
- 📦 交付物
  - 源码：`src/studio/__init__.py`、`src/studio/cli.py`、`src/studio/core/{config,settings,paths,logging,ids,clock,errors,proto,doctor}.py`
  - 脚本：`scripts/{env,doctor}.ps1`、`ops/{start_all,stop_all,status,healthcheck}.ps1`、`启动.bat`、`停止.bat`、`tasks.ps1`、`Makefile`
  - 依赖：`tts/pyproject.toml`（独立 3.11 子项目）、`requirements-tts.lock.txt`、根 `uv.lock`、`tts/uv.lock`
  - 测试：`tests/conftest.py`、`tests/unit/core/{test_settings,test_paths,test_clock,test_ids,test_errors,test_doctor}.py` —— **55 用例**
- 🔧 施工裁定（本任务期间新增，需回灌规格书）
  1. **`tts/` 建独立 `pyproject.toml`**（`studio-tts`、`package=false`、`find-links=["D:/Torch"]`）—— §02.2 树只写了 `tts\.venv` + `tts\uv.lock`，但验收命令 `uv run --project tts` 要求子项目存在。
  2. **`core/doctor.py`** 承载自检引擎（§02.2 的 `core\` 清单未列），CLI 与 `/api/v1/doctor` 共用同一实现。
  3. **盘符门禁语义泛化**：断言"不在系统盘"（`is_on_system_drive`）而非字面 `D:`，避免换机 / CI 失效。
  4. **`TEMP`/`TMP` 指向 `data\tmp`**（随仓库走，便于 GC），其余缓存指向 `D:\ai_models\*`。
  5. **新增 R1 加固**：`doctor` 断言 `uv cache dir` 不在系统盘（uv 会静默写 GB 级缓存）。
- ⚠️ **R1 C 盘仅 ~2 GB** ⇒ 任一路径漏重定向就会写爆系统盘；`doctor` 必须逐项断言而非抽查
- ⚠️ 禁用 `C:\msys64\ucrt64\python.exe` 装 ML 包（会污染 torch 依赖）
- ℹ️ 遗留 warn（非阻塞，已有主责任务）：①项目内置字体缺失 ⇒ E8 / T3.5；②RAM 15.82 GB 略低于推荐 16 GB ⇒ T2.x 须控 TTS 并发
### T1.2 配置系统 · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.1 ｜ 里程碑：M1 ｜ 契约：§01.2 / §README.6
- [x] 八份 YAML：`persona.yaml`、`llm.yaml`、`app.yaml`、`pools.yaml`、`outputs.yaml`、`randomization.yaml`、`publish.yaml`、`logging.yaml`（+ `persona.example.yaml`、`secrets.example.yaml` 模板）
- [x] Pydantic 强校验 + `extra="forbid"`（未知键直接报错，防拼写错误静默失效）
- [x] 环境变量覆盖层（env > yaml）+ `studio config dump --json` / `studio config validate`
- [x] **密钥不入库**：`llm.yaml` 只存 `api_key_env` 名；写进密钥本体（`sk-*`/`Bearer`/`eyJ*`）⇒ 加载期直接拒绝
- [x] 启动校验：persona 缺字段 ⇒ 启动即报错并打印模板路径（`config/persona.example.yaml`）
- [x] 局域网鉴权守卫：未设密码但开启局域网 ⇒ 拒绝启动（`AUTH_LAN_WITHOUT_PASSWORD`）
- [x] 越界路径守卫：`app.paths.*` 必须落在仓库内且不在系统盘；`outputs.watermark.path` 必须落在 `templates/` 内
- [x] 接入 `doctor`：新增 `config.valid` 阻塞项（8 份配置 + 局域网鉴权 + 越界路径）
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  uv run studio config validate             # 配置校验通过（8 份文件）
  uv run studio config dump --json          # 与 config/*.yaml 一致，密钥已脱敏
  uv run studio doctor --json               # ok=true（18 项，含 config.valid / persona.library）
  uv run studio persona validate            # 激活人物 OK + 库 2 条 / 无效 0 条
  pytest tests/unit/core/test_config.py -q  # 67 用例（五类必需用例全覆盖）
  powershell -File tasks.ps1 check          # ruff + mypy strict + 175 用例全绿（slow 另计 1）
  ```
- 📦 交付物
  - 代码：`src/studio/core/config.py`（~53 KB：9 个模型 + 加载器 + 4 类守卫 + 脱敏）、`cli.py` 新增 `config dump|validate`
  - 配置：`config/{persona,llm,app,pools,outputs,randomization,publish,logging}.yaml` + 2 份 example
  - 测试：`tests/unit/core/test_config.py`（67 用例）
- 🔧 施工裁定（本任务期间新增，需回灌规格书）
  1. **`config/llm.yaml` 入库**（§2.5 原写"入 `.gitignore`"）—— 该文件按设计**零密钥**（只存 `api_key_env` 名），入库才能保证"零配置可跑"；个性化覆盖走 `config/*.local.yaml`（已 gitignore）。
  2. **新增 `config/<name>.local.yaml` 覆盖层**（深度合并，优先级：env > local > 主文件），给用户一个安全的个性化落点。
  3. **env 覆盖命名 `STUDIO_CFG__<文件>__<路径>__<键>`**（双下划线表嵌套，值按 JSON 解析）；指向未知文件名的前缀 ⇒ 直接报错。
  4. **`logging.yaml` 的 `json` 键改名 `json_output`**（`json` 与 pydantic `BaseModel.json` 冲突）。
  5. **`StudioPaths` 增加 4 个可选覆盖字段**（`work_dir/output_dir/cache_dir/logs_dir`），让 `app.paths` 真正生效而非摆设。
  6. **`config.valid` 加入 `doctor`**（T1.1 的 16 项 → 17 项），把"persona 缺字段 / 局域网无密码 / 越界路径"提升为**启动阻塞项**。
- ⚠️ 密钥泄漏 ⇒ 只存环境变量名 + 加载期模式识别 + dump 脱敏（`api_key_env` 不脱敏，因为它是名字不是密钥）
- ⚠️ persona 是**唯一人工必填**，当前已填一版"熊大熊二·MC跑酷"默认值可跑通；**上线前需你确认口吻/受众/禁区**（E7）
#### T1.2+ persona 可编辑改造 ✅ **已完成（2026-09-13）**
> 用户追加需求："**persona 做成可编辑，随时可能改人物**"。
- [x] 人物库 `config/personas/*.yaml`：多套人物并存，`studio persona list` 一览（无效条目带原因）
- [x] 一键切换 `studio persona use <id>`：**先校验后落盘**，旧版自动备份到 `data/backups/persona/`
- [x] 改完存一份 `studio persona save-as <id>`（`--overwrite` 可覆盖），便于随时切回
- [x] **热重载**：只比对 `mtime_ns + size`，改文件即刻生效，无需重启任何进程
- [x] **坏文件不打断在跑任务**：热重载失败保留上一份可用快照并记 `last_error`；只有显式 `reload()` 才抛
- [x] 校验标准不分叉：`PersonaStore` 与 `load_config` 共用同一套 `PersonaConfig` 校验器
- [x] `doctor` 新增 `persona.library` 非阻塞项：报告激活人物 + 库条目数 + 无效条目
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  uv run studio persona show                 # 当前人物全字段（来源 / 版本 / sha256 / 加载时刻）
  uv run studio persona use solo_commentary  # 切人 ⇒ 备份 <stamp>_persona_default.yaml
  uv run studio persona use persona_default  # 切回 ⇒ 备份 <stamp>_solo_commentary.yaml
  pytest tests/unit/core/test_persona_store.py -q   # 48 用例
  ```
- 📦 交付物
  - 代码：`src/studio/core/persona_store.py`（`PersonaStore` + 进程级单例 + 订阅回调）、`cli.py` 新增 `persona show|list|use|save-as|validate`
  - 配置：`config/personas/{README.md, persona_default.yaml, solo_commentary.yaml}`、`StudioPaths.persona_library_dir`
  - 数据：`data/backups/persona/`（回滚点，已 gitignore）
  - 测试：`tests/unit/core/test_persona_store.py`（48 用例）
- 🔧 施工裁定（需回灌 §02.2 目录树 / §02.4 命名约定）
  7. **`core/persona_store.py`** 承载人物热重载（§02.2 的 `core\` 清单未列）。
  8. **`config/personas/` 人物库 + `studio persona` CLI 子命令组**（新增能力）。
  9. **`doctor` 新增 `persona.library` 非阻塞项**（17 → 18 项）：激活人物不可读时**只 warn**，避免与 `config.valid` 重复阻塞。
  10. **`solo_commentary` 人物**顺带作为 R2（IP 风险）规避方案：一键切到无 IP 形象的原创人设。
- ⚠️ **多进程无 IPC**：各进程各持 `PersonaStore` 实例，靠文件 `mtime` 独立发现变更 ⇒ 切换后**最迟下一次 `current()` 生效**，不可假设全局立即一致
- ⚠️ 备份名必须是**切换前**的人物 id：冷进程（CLI）没有内存快照 ⇒ 回退读盘解析；文件坏到无法解析才退 `unknown`


### T1.3 数据库与迁移框架 · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.2 ｜ 里程碑：M1 ｜ 契约：§03.2 / §03.7
- [x] 6 个**版本化**迁移 + 1 个**连接级** PRAGMA 文件：`0000_pragmas.sql`、`0001_init.sql`(17 表)、`0002_topics.sql`(3)、`0003_templates.sql`(3)、`0004_publish.sql`(3)、`0005_schedule_report.sql`(3)、`0006_seed.sql`
- [x] **合计 29 表 / 58 索引 / 6 触发器**（DDL 照抄 §03.3，仅补 `IF NOT EXISTS` 以幂等）
- [x] 连接级 PRAGMA：WAL / `busy_timeout=5000` / `foreign_keys=ON` / `temp_store=MEMORY` / `cache_size` / `mmap_size` / `wal_autocheckpoint`
- [x] 迁移器：单文件单事务（`BEGIN IMMEDIATE`）、`schema_migrations` 登记、**checksum 校验**（历史迁移被改 ⇒ 拒绝启动）
- [x] `studio db check`：表数/索引数/触发器数 + `integrity_check` + `foreign_key_check`（对象清单**双向比对**：漏建与多建都能发现）
- [x] seed：`pool_settings` 初始值（draft 2 / voice 1 / render 1 / publish 1）+ **报告周期默认值**（weekly 周一 09:00、monthly 每月 1 日、daily 停用）
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  uv run studio db migrate    # executed=6 · 29 表 · 73 ms（再跑一次 executed=0 ⇒ 幂等）
  uv run studio db check      # 8 项全 ok：wal / fk=1 / integrity ok / 外键 0 / 29 表 / 58 索引 / 6 触发器 / 6 迁移
  uv run studio db status     # 6 行版本 + sha256 全一致
  uv run studio doctor --json # ok=true · 19 项 · 0 阻塞 · 2 warn（字体 / RAM）
  pytest tests/unit/db tests/integration -q   # 96 用例
  powershell -File tasks.ps1 check            # ruff format/check + mypy strict + 273 用例全绿
  ```
- 📦 交付物
  - 代码：`src/studio/db/{__init__,engine,migrate}.py`（engine 语句工具 + 连接工厂；migrate 发现/计划/执行/自检）
  - 迁移：`src/studio/db/migrations/0000..0006`（7 文件 · 29 表 · 58 索引 · 6 触发器）
  - 测试：`tests/unit/db/{test_engine,test_migrate}.py` + `tests/integration/test_migrations.py`
  - 新增错误码：`DB_MIGRATION_FAILED` / `DB_MIGRATION_MISSING_FILE` / `DB_MIGRATION_OUT_OF_ORDER` / `DB_SCHEMA_DRIFT`
- 🔧 施工裁定（需回灌 §02.2 / §03.3.17 / §03.7.1）
  11. **`0000_pragmas.sql` 是连接级、不登记版本**：不进 `schema_migrations`，由 `db/engine.py` 在每次 `connect()` 后执行。磁盘文件是 PRAGMA 的**唯一真相**，代码里不另抄一份。
  12. **`0006_seed.sql` 的 `pool_settings` 以 `config/pools.yaml` 为准**（§03.3.17 代码块里的旧 INSERT 作废）：render 租约取 **600s**（非 300s）、backoff 取 YAML 值 ⇒ 已加"YAML ↔ DB 逐字段相等"防漂移测试。
  13. **`report_schedules.next_run_at` 初始为 NULL** ⇒ T5.7 调度器必须把 NULL 视为"未排期 ⇒ 立即计算"，**不能**写 `next_run_at <= now`（NULL 比较永假 ⇒ 永不触发）。
  14. **迁移器自建 `schema_migrations`**（`SCHEMA_MIGRATIONS_DDL`）且与 `0001_init.sql` 内定义**逐字一致**（有防漂移测试）—— 顺序上必须"先能登记版本，才能应用 0001"。
  15. **`doctor` 18 项 → 19 项**：新增 `db.migrations`。库不存在 ⇒ ok；有 pending ⇒ warn 非阻塞；乱序 / 改历史 / 缺文件 ⇒ **fail + 阻塞**。
  16. **语句切分用 `sqlite3.complete_statement`**（自研正则会切错 `CREATE TRIGGER ... BEGIN ... END;`）；且**前导注释必须显式剥离** —— `complete_statement` 只在见到分号时返回 True，注释横幅会被粘进下一条语句，导致 `PRAGMA` 名字解析失手、只读连接误改 WAL。
  17. **`migrate()` 用带 PRAGMA 的连接建库**：WAL 是**库级持久设置**，不在建库时落盘 ⇒ 新库会停在 `journal_mode=delete`，首次 `db check` 即红。
- ⚠️ **R10 锁竞争** ⇒ WAL + 短事务（<20ms，**禁事务内 I/O**）
- ⚠️ 改 `CHECK` 约束需走四步重建（SQLite 不支持 `ALTER ... CHECK`），见 §03.7.3
- ⚠️ 迁移**只增不改**；改历史文件 = 全站拒绝启动（`DB_MIGRATION_CHECKSUM_MISMATCH`）
- ⚠️ **乱序同样拒绝启动**：库已到 0006 却冒出 0005 待应用 ⇒ `DB_MIGRATION_OUT_OF_ORDER`（比"照常执行"安全）

### T1.4 领域模型与 16 态状态机 · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.3 ｜ 里程碑：M1 ｜ 契约：§03.5.1
- [x] 枚举：`TaskStatus`(16) / `Grade`(A/B/C) / `PoolName`(4) / `PublishStatus` / `UnitType`（顺带 `TaskKind` / `TaskPool` / `JobStatus` / `AutoApprovePolicy`）
- [x] `ALLOWED_TRANSITIONS` 16×16 迁移矩阵（含 `failed` / `manual_pool` / `discarded` / `canceled` 的进出）
- [x] `TaskService.transition()` **唯一写入口**（静态检查：其他模块不得写 `tasks.status`）
- [x] `version` 乐观锁（并发迁移冲突 ⇒ 重试而非覆盖）
- [x] 每次迁移写 `task_events`（`from`/`to`/`reason`/`actor`）+ 同事务写 `system_logs`
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  pytest tests/unit/domain/test_state_machine.py -q        # 540 用例（16×16=256 格逐格 + 动态边 + 可达性 + 白名单）
  pytest tests/contract/test_no_direct_task_write.py -q    # 4 用例（静态扫描 tasks 表越权写入）
  pytest tests/unit/domain tests/integration/test_task_transition.py -q   # 581 用例
  powershell -File tasks.ps1 check                         # ruff + mypy strict + 858 用例全绿
  ```
- 📦 交付物
  - 代码：`src/studio/domain/{__init__,enums,errors,state_machine,models,task_service}.py`
  - 测试：`tests/unit/domain/{test_enums,test_state_machine}.py`、`tests/integration/test_task_transition.py`、`tests/contract/test_no_direct_task_write.py`
  - 新增错误码：`TASK_NOT_FOUND`（`STATE_TRANSITION_ILLEGAL` / `STATE_VERSION_CONFLICT` 沿用既有登记）
- 🔧 施工裁定（需回灌 §02.2 / §03.5.1）
  18. **`PoolName`(4) 与 `TaskPool`(5) 拆成两个枚举**：DDL 的 `tasks.pool` CHECK 允许 `none`，而 §03.5.1 的 `PoolName` 只有 4 值（那是**作业池**身份）。混成一个会让"作业池"类型凭空多出 `none` 这种脏值。
  19. **`IllegalTransition` / `ConcurrentModification` 挂在 `core.errors.StateTransitionError` 下**（规格书写 `DomainError`，本项目没有该基类）；异常名按 §03.5.1 原文保留，故 `domain/errors.py` 顶部带 `# ruff: noqa: N818`。
  20. **`retry_from` 是动态边**：`ALLOWED_TRANSITIONS` 只存静态半边，`failed`/`manual_pool` → `retry_from` 由 `is_allowed(..., retry_from=)` 判定；白名单外的 `retry_from` 一律拒绝（防脏数据开后门）。
  21. **`tasks.pool` 由 `TaskService` 维护**（状态 → 池的映射表）：`tasks` 只有这一个写入方，不能让调用方顺手改。
  22. **`update_progress()` 不 bump `version`**：`version` 是状态迁移的乐观锁，进度是高频覆盖写，若也 bump 会让所有持 `version` 的调用方白白重试。
  23. **契约测试文件名用 `test_no_direct_task_write.py`**（§02.2 原列 `test_no_direct_status_write.py`）—— 与 todolist 对齐，语义也更准。
  24. **mypy 约定**：`Field(...)` 必须用关键字 `default=`（位置参数会让 mypy 的 `dataclass_transform` 认不出默认值）；pydantic 模型字段可直接给 BaseModel 实例作默认值（v2 逐实例深拷贝，已实测）。
- ⚠️ 状态机被绕过 ⇒ 后续所有守卫失效；用静态检查 + 契约测试双保险
- ⚠️ **`published` 是唯一真终态**（连人工都没有出边）；`discarded` / `canceled` 只能由人工"捞回"到 `pending`

### T1.5 队列内核（四池 · `JobStore`）· **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.3 ｜ 里程碑：M1 ｜ 契约：§03.4
- [x] **单语句原子认领**：`UPDATE ... WHERE id=(SELECT ... ORDER BY priority ASC, created_at LIMIT 1) RETURNING ...`（无 SELECT-then-UPDATE 窗口）
- [x] 租约：`lease_sec` 按池配置（draft 180 / voice 90 / render **600** / publish 600；render 取 YAML 值，见 T1.3 裁定 12）
- [x] 续租 + sweeper 回收 + 指数退避（`base × 2^(n-1)` 封顶 `backoff_max_ms` + 20% 确定性抖动，**禁 busy-loop**）
- [x] 依赖解锁（上游 job 完成 ⇒ 下游 `blocked` → `pending`，逐级收敛，2 级）
- [x] 死信：`attempts ≥ max_attempts` ⇒ `dead` + **强制 `system.alert(JOB_DEAD)`**（与状态变更同事务）
- [x] 幂等入队：唯一键 `(task_id, pool, unit_type, unit_ref)` ⇒ 重复入队不产生第二条
- [x] `publish` 池限频守卫：`≤3 条/天/账号` + 间隔 ≥30min（按**本地日** UTC+8 统计）
- [x] 池控制：`paused` / `concurrency` 守卫内置在 `claim()` 内（worker 侧 2s 缓存只是优化）
- [x] 死信一键重投：`requeue_dead()` 重置 `attempts` 并写 `audit_ops` 留痕
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  pytest tests/unit/db/test_lease.py -q                  # 21 用例（抖动确定性 / 退避封顶 / 心跳间隔 / 轮询退避）
  pytest tests/integration/test_queue_lease.py -q        # 61 用例（六条验收 + 池控制 + 限频 + 死信重投 + 统计 + 租约守卫）
  pytest tests/contract/test_no_direct_job_write.py -q   # 4 用例（静态扫描 jobs 表越权写入 + db↛domain 分层）
  powershell -File tasks.ps1 check                       # ruff + mypy strict + 947 passed / 32 skipped
  ```
- 📦 交付物
  - 代码：`src/studio/db/lease.py`（纯函数：确定性抖动 / 指数退避 / 租约时刻 / 心跳间隔 / 空池轮询退避）、`src/studio/db/queue.py`（`JobStore` 四池门面 + `Job` / `PoolRuntime` / `PoolStats` / `JobOutcome` / `ReclaimResult` / `RateLimitState`）
  - 测试：`tests/unit/db/test_lease.py`、`tests/integration/test_queue_lease.py`、`tests/contract/test_no_direct_job_write.py`
  - 新增错误码：`JOB_NOT_FOUND`（`JOB_DEAD` / `JOB_LEASE_LOST` / `POOL_RATE_LIMITED` 沿用既有登记）
- 🔧 施工裁定（需回灌 §01.4.4 / §02.2 / §03.4）
  25. **`clock.format_iso` 毫秒化**：DDL 默认值是 `strftime('%Y-%m-%dT%H:%M:%fZ','now')`（毫秒），Python 侧写微秒会让 `...22.123456Z` 与 `...22.123Z` **字典序倒挂**（`'Z' > '2'`）⇒ SQL 时间比较不可信。新增 `ISO_LENGTH = 24`，**截断不四舍五入**。同时修正 `core.proto.Severity` 为 `debug|info|warn|error|fatal`（原写成 `warning|critical`，与 `system_logs.level` 的 CHECK 冲突），`AlertCode` 8 值进 `core/proto.py`（§1.4.4 写的 `POOL_CONCURRENCY_REDUCED` 以 §4.5.2 的 `POOL_AUTODEGRADED` 为准）。
  26. **队列时间戳一律 Python 侧算好传参**（不用 SQL 的 `strftime('now')`）：① 单测可冻结时间（`now=` 注入，不必 monkeypatch 全局时钟）；② 与 DDL 默认值同为毫秒格式，字典序即时间序；③ 同一方法内多处取时间必然一致。
  27. **`JobStore` 全同步 API**（与 `TaskService` 一致）：单次调用 <5ms，远低于 R10 的 20ms 事务预算；T1.6 的 worker 在自己的事件循环里直接调用，需要并发时用 `asyncio.to_thread` + **每线程独立连接**。
  28. **`claim()` 承担 `paused` / `concurrency` 守卫**（§03.4.4）：放在这里是为了"只有一个地方能忘"；worker 侧那 2s 缓存只是省一次读，正确性由本方法保证。**暂停只停"开新工"**，sweeper 照常回收过期租约（否则暂停会永久卡住在途单元）。
  29. **`reclaim_expired()` 用 SELECT-then-UPDATE**（整轮一个 `BEGIN IMMEDIATE`）：退避时长按池配置算，一条 SQL 取不到逐行不同的 base/cap；§3.4.3 的 SQL 注释本身就写"`backoff_ms` 由应用层算"，§03.4.6 规则 2 禁的是**认领**路径的 SELECT-then-UPDATE。
  30. **`rate_limit_state()` 按本地日（UTC+8）统计**：DB 存 UTC，先 `datetime(ts, '+480 minutes')` 偏移再取日期部分 —— 直接用 `substr(utc,1,10)` 会把北京时间 08:00 前发布的算进前一天，限频形同虚设（有专门用例锁死）。
  31. **死信告警以 `system_logs` 行 + `payload_json.code` 表达**，与 job 状态变更**同事务**落库（不会出现"状态变了但没告警"或反之）；T1.7 的 WS 层负责广播为 `system.alert`。
  32. **`db/queue.py` 不 import `domain`**：池名 / 单元类型一律 `str` 透传，合法性由 DDL 的 `CHECK` 兜底（"DDL 是唯一真相"，不在 Python 里再抄一份枚举）；契约测试锁死该分层。
- ⚠️ 重复执行 ⇒ 单语句认领 + 租约（陷阱 #2）
- ⚠️ 空转吃 CPU ⇒ 指数退避休眠（`lease.poll_delay_ms()`，200ms→2s），禁 busy-loop
- ⚠️ **`jobs` 表只允许 `JobStore` 写**，其他模块绕过 = 架构回退（`test_no_direct_job_write.py` 静态拦截）
- ⚠️ 未知池名 ⇒ `DB_SCHEMA_DRIFT` 立刻报错，**不允许**"悄悄丢进某个池"

### T1.6 Worker 框架（四池）· **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.5 ｜ 里程碑：M1 ｜ 契约：§04.5.1
- [x] 认领循环（`claim → run → ack`）+ 空池退避（`lease.poll_delay_ms()`，禁 busy-loop）
- [x] 心跳上报：每 5s upsert `worker_heartbeats`（`_Pulse` **一个**后台线程同时干三件事：报活 / 续租 `lease/3` / 判超时）
- [x] 优雅退出 `draining`：`SIGINT` ⇒ **跑完当前单元**再退出（不丢进度）⇒ **删掉自己那行心跳**（行存在 ⇔ 进程应当在跑）
- [x] 崩溃恢复：心跳超 15s ⇒ 标记 `dead` + 落 `system_logs(error)` ⇒ supervisor **先 terminate 僵进程**再指数退避重启（超 `restart_limit` ⇒ `halted`）
- [x] 硬杀保护：产物先写 `.partial` 再 `Path.replace` 原子改名（`commit_partial`）
- [x] RSS 持续增长告警（`RssLeakWatch`：相对基线 +50% **且** +256MB，连续 3 拍）
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  pytest tests/integration/test_worker_lifecycle.py -q        # 37 用例（三条验收 + draining 语义 / 优雅退出摘行 / 脉冲不死 / 租约丢失丢产物 / 协作式超时 / 不可重试直死信 / .partial 原子提交 / 空池退避 / 池暂停不认领 / 依赖解锁 / supervisor 重启上限 + stop_all）
  pytest tests/unit/pools/test_heartbeat.py -q                # 36 用例（worker_id 格式与解析 / 进程画像 / RSS 判定 / 心跳读写 / 判死幂等 / 优雅退出摘行 / 死行 GC）
  pytest tests/contract/test_no_direct_heartbeat_write.py -q  # 5 用例（心跳表越权写入扫描 + WORKER_STATUSES↔DDL CHECK 漂移 + WORKER_DEAD 非告警码）
  powershell -File tasks.ps1 check                            # ruff + mypy strict + 1025 passed / 32 skipped
  ```
- 📦 交付物
  - 代码：`src/studio/pools/{__init__,heartbeat,worker_base,supervisor,runner}.py`；`studio pool run|status|reap`（`src/studio/cli.py`）
  - 测试：`tests/unit/pools/test_heartbeat.py`、`tests/integration/test_worker_lifecycle.py`、`tests/contract/test_no_direct_heartbeat_write.py`
  - 新增错误码：`UNIT_TIMEOUT` / `WORKER_DEAD`（+ `WorkerError`）
- 🔧 施工裁定（需回灌 §04.5.1 / §01.4.2 / §02.2）
  33. **心跳行存在 ⇔ 进程应当在跑**：优雅退出**删行**（`HeartbeatStore.forget`），只有猝死才靠 15s 超时判死 ⇒ 不误报。
  34. **`_Pulse` 用一个后台线程同时干三件事**（心跳 5s / 续租 `lease/3` / 判超时），tick 默认 1s。不用 asyncio：`db` 层全同步（T1.5 裁定 27），单元处理器也是同步的（跑 ffmpeg / 阻塞 HTTP），为续租引入事件循环只会把最简单的路径复杂化。
  35. **租约丢失 ⇒ 丢弃产物**：`renew()` 返回 `False` ⇒ **不写** `succeed`（否则会用过期结果覆盖别人的成果）；handler 可 `ctx.check_alive()` 主动退出。
  36. **`NON_RETRYABLE_CODES` 首次失败即死信**（配置错 / 缺字体 / 缺水印 / 缺素材 / FFmpeg 缺滤镜 / DB schema 漂移 / 发布关闭）—— 重试不会变好，早一分钟报警早一分钟能修，不必耗完 `max_attempts`。
  37. **单元超时是协作式的**：脉冲线程置 `timed_out`，handler 可 `ctx.check_alive()`；跑完才发现超时 ⇒ 按 `UNIT_TIMEOUT` 重排。**"硬杀进程"是 supervisor 的事**（T4.11），不是本层职责。
  38. **supervisor 判死 ⇒ 先 terminate 僵进程 ⇒ 指数退避重启**；重启次数超 `restart_limit` ⇒ `halted=True` + 落 `system_logs` error。
  39. **`WORKER_DEAD` / `WORKER_RESTART_HALTED` 不是 `system.alert.code`**（§4.5.2 锁死 8 值）⇒ 落 `system_logs` 的 `error` 行 + `payload_json.code`；T1.7 的 WS 层按"不在 8 值内 ⇒ 走 `log.append`"处理。
  40. **`build_supervisor()` 用 `python -m studio.cli pool run --pool X --slot N` 拉起子进程**（`.venv` 有 `_editable_impl_studio.pth`，`-m` 可用）。
  41. **测试要点**：① worker 必须**自己开连接**（`sqlite3` 线程亲和，`connection=None`），生产同语义；② `PoolWorker.run()` 用**真实** `utc_now()`（生产语义不注入），故测试把 `T0` 锚在"半小时前"。
  42. **`PoolWorker` 留注入点** `pulse_tick_sec` / `unit_timeout_sec`（生产取 `pools.yaml` 与默认 tick，只有测试与一次性排障才覆盖）。
  43. **测试里的 worker 必须"兜底停"**：`run()` 收不到 `request_stop()` 就**永不返回**（生产语义，不为测试让步）。`test_worker_lifecycle.py` 用 `_RUNNING` 登记表 + autouse `_leak_guard` 兜底停并 `join`。漏停会留下**永不停歇**的脉冲线程，一直往已删除的临时库里写心跳（`no such table`），把 stderr 刷成噪音并掩盖真问题 —— 本次 `test_graceful_exit_forgets_heartbeat` 就是这样被坑的（表现为"优雅退出了但心跳行还在"，实际是 worker 根本没退出）。
  44. **mypy `warn_unreachable` 坑**：`assert x.prop is False` 会把 `x.prop` 收窄成 `Literal[False]`，紧跟其后的 `assert x.prop is True` 会被判成"永远不成立 ⇒ 后续不可达"。**读进局部变量再断言**。
- ⚠️ 陷阱 #9：直接写目标文件 ⇒ 崩溃留残缺产物（`.partial` + 原子改名）
- ⚠️ **`worker_heartbeats` 只允许 `pools/heartbeat.py` 写**（`test_no_direct_heartbeat_write.py` 静态拦截）
- ⚠️ 脉冲线程**绝不能死**：死了就"看着还活着但不再续租"，比直接崩更危险（tick 异常一律吞掉继续）

### T1.7 日志与 WebSocket 骨架 · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.6 ｜ 里程碑：M1 ｜ 契约：§04.4 / §04.5.2
- [x] `system_logs` 写入契约：**先落库再广播**（`LogService.append()` 落库 → `Hub.wake()`；顺序不可颠倒）
- [x] `/ws/ui` 长连接 + **8 通道**订阅（`channels` / `task_ids` / `min_level` / `since_id`）
- [x] 合并窗口 100ms + **2Hz/任务限流**（`sentence.updated` 5Hz；时间由调用方注入 ⇒ 测试确定性，不靠 `sleep`）
- [x] 环形缓冲 200 条/连接（满则丢 `debug` → `info` → 合并同类；**告警永不丢**，允许溢出 2× 容量）
- [x] `since_id` 补发（断线重连**不丢不重**；超 2000 条 ⇒ 改发最近 500 条快照 + 提示走 REST 分页）
- [x] `seq` 缺口检测（**每连接**单调；`snapshot` 与上行回执也占号）
- [x] `system.alert` **永不合并、永不丢弃**（`never_merge` / `never_drop` / `rate_hz=None`）
- [x] 慢客户端主动断开（环形缓冲满 **且持续 10s** ⇒ 1008 + `WS_CLIENT_SLOW`）
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  pytest tests/unit/ws -q                                     # 38 用例（8 通道/信封/30 事件策略表 + 合并窗口与限流 + 环形缓冲与慢客户端判定）
  pytest tests/integration/test_ws_replay.py -q               # 24 用例（三条验收 + 快照/三重过滤/告警通道/ping-resync/慢客户端/REST 分页/健康检查）
  pytest tests/contract/test_ws_protocol.py -q                # 19 用例（直接解析 §04.4 比对通道/信封/事件表/告警码/阈值 + 分层静态扫描）
  powershell -File tasks.ps1 check                            # ruff + mypy strict + 1106 passed / 32 skipped
  ```
- 📦 交付物
  - 代码：`src/studio/ws/{protocol,coalescer,backpressure,hub,snapshots,__init__}.py`、`src/studio/services/log_service.py`、`src/studio/db/session.py`、`src/studio/app/{main,lifespan,deps}.py`、`src/studio/app/routers/{ws,health,logs}.py`、`studio serve`（`src/studio/cli.py`）
  - 测试：`tests/unit/ws/{helpers,test_protocol,test_coalescer,test_backpressure}.py`、`tests/integration/test_ws_replay.py`、`tests/contract/test_ws_protocol.py`
  - 新增错误码：`WS_SUBSCRIPTION_INVALID` / `WS_CLIENT_SLOW` / `WS_PROTOCOL_VIOLATION`
- 🔧 施工裁定（需回灌 §04.4 / §02.2 / §02.4）
  45. **`seq` 是"每连接单调"，不是服务端全局计数器**：连接一旦按通道过滤 + 对同类事件合并，全局计数**必然**出现假缺口（前端会误判丢包并无限 `resync`）。每连接从 1 自增 ⇒ "缺口 ⇔ 真丢包"才成立；`snapshot` 与上行回执（`pong`/`ack`/`error`）**也占号**。
  46. **慢客户端判定 = 环形缓冲满（200 条/连接）且持续 10s**（原 §04.4.6 写"队列 > 500 条"，与 200 条容量自相矛盾，以容量为准）；断开码 1008 + `WS_CLIENT_SLOW`。
  47. **`log.appended` / `system.alert` 的日志主键字段名是 `log_id`**（= `system_logs.id`），**不是** `id`：业务事件的 `id` 是实体主键，混用会让"按 `log_id` 去重"误伤正常事件（实测踩到：tail 与 `since_id` 补发撞同一行时，去重把 `task.updated` 也吞了）。
  48. **上行回执（`pong` / `ack` / `error`）必须 `send_direct` 绕过订阅过滤**：否则 `channels=logs` 的连接发 `ping` 永远收不到 `pong`（实测踩到：客户端死等，服务端以为已经回了）。
  49. **`Hub.wake()` 必须跨线程安全**：`asyncio.Event.set()` 只能在拥有该循环的线程里调，而写日志的可能来自线程池 / 主线程 ⇒ 统一走 `call_soon_threadsafe`（循环未起或已关则静默返回，tail 轮询兜底）。`Hub.start()` 记录 `self._loop`。
  50. **连接按线程取**（`db/session.py: ThreadLocalConnections`）：应用进程里事件循环线程与 Starlette 线程池都会碰库，而 `sqlite3` 连接线程亲和 ⇒ 每线程一条连接；`LogService` 因此接受"连接**或**工厂"。
  51. **只对已注册 provider 的通道发快照**（`SnapshotRegistry.has()`）：否则前端握手会收到一堆空 `snapshot`（`control` / `topics` / `publish` 本阶段无 provider）。
  52. **`debug` 不落库**（`config/logging.yaml: database.min_level=INFO`）⇒ 任何 `min_level` 都看不到它；别写"`min_level=debug` 能看到 debug"的用例（`test_debug_is_not_persisted_so_no_min_level_can_show_it` 就是为此存在）。
  53. **测试包树必须有 `__init__.py`**：需要被其它测试模块 import 的目录（本次 `tests/` → `tests/unit/` → `tests/unit/ws/`，因 `helpers.py` 被 unit 与 integration 共用）若缺 `__init__.py`，mypy 会把同一文件按"目录短名"与"`tests.*` 全名"各看一遍 ⇒ 报 `Source file found twice under different module names`，**并跳过该文件、掩盖它下面所有真实类型错误**（本次修完重复名后立刻暴露出 12 个被掩盖的错误）。
- ⚠️ 陷阱 #12 前端被刷爆 ⇒ 合并 + 限流 + 环形缓冲
- ⚠️ 陷阱 #13 日志断线丢失 ⇒ 先落库再广播
- ⚠️ 陷阱 #35 mypy 重复模块名**会掩盖真实类型错误** ⇒ 修完 `__init__.py` 后必须重跑 `mypy`（不能只看"那个报错消失了"）

### T1.8 LLM 双通道网关 · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.2 ｜ 里程碑：M1 ｜ 契约：§01.2.4 / §04.1.1
- [x] 云端 [OI] 兼容通道 + 本地兜底通道（`oi_compatible` / `llama_cpp` 走 `POST {base}/chat/completions`；`ollama` 走 `POST {base}/api/chat`）
- [x] JSON Schema 强校验（Draft 2020-12；错误串**按 JSON 路径排序**返回，**禁正则补救**）
- [x] 修复重试 ≤3（schema 不合法 ⇒ **追加"上次输出 + 校验错误"两条消息再问**，不是重发同一请求）
- [x] 熔断：连续失败 ≥3 ⇒ OPEN（60s）⇒ 半开放一个探针；打开期间该通道直接跳过
- [x] `llm_calls` 记账：token / 成本 / 耗时 / model / prompt_version / is_local（**失败也记**）
- [x] **token 预算闸门**（任务级 token + 当日成本 + `on_exceed` 三策略）
- [x] 单次硬超时 120s（与 `profile.timeout_sec` **取小**）
- [x] 密钥只从环境变量读（`api_key_env` 存的是变量**名**；写密钥本体被 `config.validate` 拒绝）
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  pytest tests/unit/agents -q                       # 31 用例（①修复重试 ②通道兜底 ③预算 ④记账 + 熔断/硬超时/密钥来源/路由缺失/日志出口）
  pytest tests/contract/test_llm_gateway.py -q      # 10 用例（llm_calls 唯一写入方 / agents 分层 / 密钥不入库 / CallStatus↔DDL / manifest sha256）
  studio prompts verify                             # 漂移清单为空（2 条公共注入块）
  studio llm budget                                 # 按 llm_calls 聚合打印预算判定
  studio llm probe                                  # 只读探测（本地 /api/tags + 云端 /models），不烧钱；不可用 ⇒ 退出码 1
  powershell -File tasks.ps1 check                  # ruff format + ruff check + mypy strict + 1147 passed / 32 skipped
  ```
- 📦 交付物
  - 代码：`src/studio/agents/{llm_client,json_guard,cost,budget,circuit,prompts,gateway,base,__init__}.py`、`studio llm {probe,budget}` / `studio prompts {verify,list}`（`src/studio/cli.py`）
  - 提示词：`prompts/manifest.yaml`、`prompts/shared/{persona_block.md,json_contract.md}`
  - 测试：`tests/unit/agents/{fakes,test_gateway}.py`、`tests/contract/test_llm_gateway.py`
  - 新增错误码：`LLM_SCHEMA_INVALID` / `LLM_TIMEOUT` / `LLM_BUDGET_EXCEEDED` / `LLM_RATE_LIMIT` / `LLM_UPSTREAM` / `LLM_CIRCUIT_OPEN` / `LLM_ROUTE_MISSING` / `LLM_PROMPT_MISSING` / `LLM_PROMPT_DRIFT`
- 🔧 施工裁定（需回灌 §01.2.4 / §02.1 / §04.1.1）
  54. **`agents/` 层不得 import `services`**（§02.1 依赖方向）⇒ 网关的日志出口用 `LogSink` Protocol 注入 `LogService.append`，不直接依赖；WS 推送同理（网关只负责"让人知道"，广播归 T1.7 的 Hub）。
  55. **"修复重试 ≤3"按"重试次数"计**：1 次原始 + ≤3 次修复 = **每通道 ≤4 次调用**（原文"≤3"有歧义，按"重试"二字字面执行）。
  56. **`AgentResult` 增补 `profile` / `error_message`**（只增不改）：排障必须能回答"最后停在哪条通道、为什么失败"，否则只能翻 `llm_calls` 反推。
  57. **提示词模板不引入 Jinja2**（§1.6.2 依赖矩阵没有它）⇒ 自研极小子集，只支持 `{{变量}}`；出现 `{%` / `{#` 或缺变量**直接报错**（静默渲染成空串会让提示词悄悄退化）。`prompt_version = "<version>+<sha12>"`，sha256 由"路径 + 内容"算出 ⇒ **改一个字版本就变**，不靠人工记得 bump。
  58. **`attempts` 跨通道累计**：一次 `complete()` 的次数 = 所有通道之和（云端 4 + 本地 4 = 8）。单通道口径由 `warnings` 的 `repair:<channel>:<n>` 表达。
  59. **mypy 的 ini 不支持多行 `module = [...]`**（写多行直接报 `Source contains parsing errors`）⇒ overrides 必须**单行**；缺 stubs 的库（`jsonschema`）加 dev 依赖 `types-jsonschema`，与 `types-psutil` / `types-PyYAML` 同规格。
  60. **`llm_calls` 失败也记账**（否则"反复重试"能绕开预算闸门）；`LlmCallStore.record` 记账失败**不得**中断主链路（宁可少一行账，不可丢一次产出）。
  61. **schema 修复耗尽后同样走兜底通道**（与传输失败同等待遇）：云端 JSON 能力失效时本地仍可能出活，而本地不烧钱 ⇒ 这份保险是免费的。
  62. **`llm_calls` 的定序必须用 `rowid` 兜底**（`ORDER BY created_at DESC, rowid DESC`）：`created_at` 只到**毫秒**，而 `new_ulid()` 同毫秒内是**随机**后缀 ⇒ 用 `id` 定序会把"第几次尝试"随机打乱（实测踩到：3 行的状态序列在整仓跑时随机失败，单跑必过）。
  63. **预算 `remaining_hint` 的语义是"还剩多少可花"**：`limit = min(配置上限, 已花 + 剩余)` ⇒ 提示只可能**收紧**闸门（`remaining=0` ⇒ 立刻拦），不可能放宽。
  64. **`studio llm probe` 的失败判定 = `unreachable` 或 `http_error`**（`no_key` 不算：本地优先模式下云端未配密钥是正常态）。实测踩到：本地端口被别的进程占着回 502，旧规则却给退出码 0 —— 启动自检的假绿灯。
- ⚠️ **R6** 输出非结构化 ⇒ Schema 强校验 + 修复重试
- ⚠️ **R16** 成本失控 ⇒ 预算上限 + 成本面板 + 超限切本地
- ⚠️ 陷阱 #19 成本失控 / #36 `llm_calls` 同毫秒乱序 / #37 失败不记账绕过预算 / #38 提示词模板静默退化

### T1.9 输入源解析 + Planner + Ideator + 选题池 · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.8 ｜ 里程碑：M1 ｜ 契约：§04.1.2 / §04.1.3 / §04.1.7
- [x] 热点解析：`data/hot/*.md`，格式 `标题|热度|平台`（**坏行照入库留痕** `parse_ok=0`：不参与选题、不阻塞归档 —— 裁定 65/67/74）
- [x] 反馈解析：`data/feedback/*.md`，**双形态兼容**（纯自由文本 / 结构化头 `平台|日期|情感|内容`），坏行 `warn` 不中断（**Q4 裁定**）
- [x] Planner：5–8 方向 + 4 条规则（契合度 <6 丢弃 / 缺 persona 依据违规 / 优先"想要"违规 / 紧贴热点违规 / "被吐槽" `priority += 500`）
- [x] Ideator：每方向 4 个 ⇒ 20–32 选题（**一个方向失败不影响其他方向**）
- [x] **两级去重**：归一化哈希命中 ⇒ **丢弃不入库**；相似度 ≥0.85 ⇒ 降分 2.0 + 写 `similar_to_json`
- [x] 历史选题库比对（R15）：`list_for_dedup` 取**全部状态**的最近 500 条（`queued`/`selected` 同样参与）
- [x] `low_grounding` 标记（无热点且无反馈 ⇒ 每个方向打 `low_grounding`）
- [x] 反馈分类（`FeedbackClassifierAgent`，`ref` 回抄对齐）**失败或缺失 ⇒ 关键词兜底**，并回填 `feedback_items`
- [x] CLI：`studio topics analyze` / `studio topics ideate` / `studio topics list`
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  uv run studio topics analyze --json   # 5–8 方向 + rule_report（含 dropped/demoted/low_grounding）
  uv run studio topics ideate           # 20–32 选题（去重后），逐方向结果表
  uv run studio topics list             # 选题池瀑布流（按分数倒序）
  uv run pytest tests/integration/test_topic_pool.py -q   # ①热点行解析 ②反馈容错 ③哈希去重 ④相似度降分 ⑤4 条规则
  uv run pytest tests/unit/domain/test_topics.py tests/unit/db/test_repositories.py tests/unit/services -q
  powershell -File tasks.ps1 check      # ruff format + ruff check + mypy strict + 1229 passed / 32 skipped
  ```
- 📦 交付物
  - 领域：`src/studio/domain/topics.py`（归一化 / 哈希 / 相似度 / 词频 / 四规则 / 两级去重 / 分类契约）
  - 仓储：`src/studio/db/models.py`、`src/studio/db/repositories/{hot,feedback,direction,topic}_repo.py`
  - 服务：`src/studio/services/{input_service,topic_service}.py`
  - 智能体：`src/studio/agents/{planner,ideator,feedback_classifier,gateway_factory}.py` + `schemas/{planner_result,ideator_result,feedback_classification}.schema.json` + `prompts/{planner,ideator,feedback_classifier}/`
  - 测试：`tests/unit/domain/test_topics.py`（35）/ `tests/unit/db/test_repositories.py`（30）/ `tests/unit/services/test_topic_service.py`（12）/ `tests/integration/test_topic_pool.py`（5）
  - 新增错误码：`INPUT_SOURCE_EMPTY` / `INPUT_PARSE_FAILED` / `TOPIC_BATCH_NOT_FOUND` / `TOPIC_DIRECTION_EMPTY` / `TOPIC_RULE_VIOLATED`
- 🔧 施工裁定（**已回灌 §02.1 / §03.3 / §04.1.2 / §04.1.3 / §04.1.7**）
  65. **坏行照入库**：`hot_items.parse_ok=0` + `raw_line` 两个专列的存在意义就是"前端能说清第 7 行错在哪"；不入库＝静默丢数据（且 `warn` 也没法指向原文）。
  66. **情感双口径**：领域用 `pos/neu/neg`（§04.1.7），落库一律 DDL 口径 `positive/neutral/negative`（§03.3.3），换算只在 `domain.SENTIMENT_TO_DB` / `DB_TO_SENTIMENT` 一处，否则运行期才被 `CHECK` 拒绝。
  67. 同 65（陷阱表条目：坏行丢弃 ⇒ 数据静默丢失）。
  68. **`PlannerOutput.directions` 放宽到 `min_length=1`**：5–8 由 `schemas/planner_result.schema.json` 保证；模型层不放宽则"过滤跑偏后不足 5 个"这条降级路径无法表达（宁可 4 个方向带告警，也不要整批丢掉）。
  69. **`BaseAgent` 自动注入 persona 变量 + 公共提示词块**，且 `prompt_version` 必须覆盖公共块（`combined_version`）—— 否则改 `persona.yaml` 不影响版本号 ⇒ P5（提示词可复现）失效。
  70. **`build_gateway` 工厂放 `agents/gateway_factory.py`**（composition root），不放 `cli.py`：网关要凑 8 样东西，放 CLI 等于"只有 CLI 能用"。
  71. **`topic_batch` 队列单元推迟到四池接线（T4.11）**（T1.12 复核后改指；原计划"每批次建一条 `tasks` 行"作废）：此刻仍没有能认领它的 worker，提前入队只会留下永远 `pending` 的作业 —— 比"没有作业"更难排查。`run_planner` / `run_ideator` 已是**批次键控**，届时接线是纯增量。
  72. **`HotItemRepo.mark_consumed(direction_id=None)`**：热点被读过但没被任何方向引用 ⇒ 只写 `consumed_at`，`used_by_direction_id` 留 `NULL`。编一个方向 id 会让"这个热点支撑了哪个方向"在复盘时变成错的。
  73. **`direction_spec_from_row` 的 `fit_score` 取默认 7**：DDL 没有这一列；落库的方向都是**已通过**契合度过滤的，给 7 分（放行）与事实一致。
  74. **坏行不得阻塞归档**（施工期发现的真实缺陷）：`list_unconsumed_sources` 必须带 `parse_ok = 1`，否则文件里只要有一个坏行就**永远归档不了**，每轮重复解析、重复报同一条；归档判据同时改为 `list_consumed_sources()`（看**库里的事实**，不看"本轮走没走导入"）。
  75. **`TopicRepo.set_status` 自动盖 `selected_at`**：与 `mark_consumed` 同一口径 —— "什么时候被选中的"是这一行的事实，漏传时间戳就是一条静默的空列。
  76. **网关日志出口加一层瘦适配**（`cli._gateway_log_sink`）：`LogService.append` 返回落库的行，而 `LogSink` 声明返回 `None`，直接把方法当回调传 mypy 会拒绝。
  77. **`DIRECTION_COUNT_MAX` 进 `domain/topics.py`**（与 `_MIN` 并列并导出）：5–8 的上下限必须是同一个真相，散在服务层与 schema 两处迟早对不上。
- ⚠️ **R15 选题同质化** ⇒ 两级去重 + 全状态历史比对；撞车在**写入前**拦下（池子随方向推进共享，A 方向刚产出的选题 B 方向立刻看得见）
- ⚠️ 单方向失败**不影响**其他方向 ⇒ 每方向独立 try/except + 独立事务；失败只记 `DirectionOutcome` + 一条 `warn`（不静默）
- ⚠️ E7（persona 口吻 / 受众 / 禁区）尚未拍板 ⇒ 本轮用 `config/persona.yaml` 现值开工，**随时可改**（`studio persona use` 切换后 `prompt_version` 会跟着变，历史批次可追溯）；T1.10 已按同一口径开工，**仍待确认**

### T1.10 Director + Writer · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.9 ｜ 里程碑：M1 ｜ 契约：§04.1.4 / §04.1.5
- [x] Director 大纲：钩子 / 3–5 段（含要点 + 画面建议 + 情绪）/ CTA / 时长预估（规则闸不过 ⇒ 带提示重试 ≤2，仍不过 ⇒ **降级返回** + `outline:*` warn）
- [x] Writer 成稿：**600–800 字** / 口播风格 / 口癖命中 ≥2 / 句长 ≤28 字 / 单人占比 ≤70%
- [x] **逐句落库**：`scripts` + `script_sentences` **同一事务**（防"有稿无句"半成品）
- [x] 禁区命中 ⇒ 直接 block（不进入评分，**也不重写**：合规问题必须让人看见）
- [x] 字数越界 ⇒ 重写 ≤2 次，仍越界取最接近版本 + `warn`
- [x] 阈值随 persona 走（`ScriptRules.from_persona`）⇒ 改 `personas/*.yaml` **不用改代码**
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  uv run pytest tests/unit/agents/test_director.py tests/unit/agents/test_writer.py -q      # 21 passed
  uv run pytest tests/contract/test_director_schema.py tests/contract/test_script_schema.py -q   # 42 passed
  uv run pytest tests/unit/domain/test_text.py tests/unit/domain/test_script.py -q          # 62 passed
  uv run pytest tests/unit/db/test_script_repo.py tests/unit/services/test_script_service.py -q  # 24 passed
  uv run pytest tests/integration/test_script_pipeline.py -q                                # 10 passed
  # CLI（临时 STUDIO_HOME 实测：无 E6 密钥时**干净降级、不崩**）
  uv run studio script draft <topic_id>     # 选题 ⇒ 大纲 ⇒ 成稿 ⇒ 逐句落库；无密钥 ⇒ ok:false + LLM_UPSTREAM
  uv run studio script draft <bad_topic>    # [TOPIC_NOT_FOUND] + 修复提示（退出码 1）
  uv run studio script show <bad_task>      # [TASK_NOT_FOUND]（与"还没有稿件"区分开）
  uv run studio script show <task_id>       # 生效稿件 + 逐句表；--json 输出机读结构
  powershell -File tasks.ps1 check          # ruff + mypy + 1388 passed / 32 skipped / 1 deselected
  ```
- 📦 交付物
  - 领域：`src/studio/domain/text.py`（`count_chars` / `split_sentences` / `split_long_sentence`）、`src/studio/domain/script.py`（`ScriptRules` / 大纲与成稿契约 / `check_outline` / `check_script` / `build_draft` / `rewrite_hint` / `estimate_duration_ms`）
  - 仓储：`src/studio/db/repositories/script_repo.py`（`save_draft` 一个事务写两表 + `ux_scripts_active` 版本切换）
  - 服务：`src/studio/services/script_service.py`（`draft()` 编排 + 模块级 `read_active_script()`）
  - 智能体：`src/studio/agents/{director,writer}.py` + `schemas/{director_result,script_result}.schema.json` + `prompts/{director,writer}/`
  - CLI：`studio script draft` / `studio script show`
  - 测试：`tests/unit/domain/test_text.py`（23）/ `test_script.py`（39）/ `tests/unit/db/test_script_repo.py`（9）/ `tests/unit/agents/test_director.py`（8）/ `test_writer.py`（13）/ `tests/contract/test_director_schema.py`（20）/ `test_script_schema.py`（22）/ `tests/unit/services/test_script_service.py`（15）/ `tests/integration/test_script_pipeline.py`（10）
  - 新增错误码：`TOPIC_NOT_FOUND` / `SCRIPT_FORBIDDEN` / `SCRIPT_WORD_COUNT` / `SCRIPT_DRAFT_FAILED`
- 🔧 施工裁定（**已回灌 §02 / §03.5.3 / §04.1.4 / §04.1.5 / §05**）
  78. **`SentenceSpec.text` 契约层留 200 字，28 字由服务端强制切分**：把 28 写进 JSON Schema 会让"模型写了长句"变成网关的**修复重试**，而规格书要的是"切分、不重写"（切分比重写省 token）—— 降级路径不能长在 schema 上。
  79. **`script_sentences` 的 INSERT 归 `ScriptRepo`**（T2.6 才拆出 `SentenceRepo` 管句级状态更新）："一个事务写两张表"是"不留有稿无句"的**实现手段**，拆开就没人守了。
  80. **`topic_spec_from_row` 对 `hook_type` 非法值兜底 `"other"`、`score` 缺失兜底 10.0**：库里是**可空 TEXT**（人工加选题可以不填），领域契约是枚举字面量 —— 对不上就落 `other`，让"没填钩子类型"变成一条可用选题，而不是让链路炸在这里。
  81. **新增 `TaskService.create()`**：建任务也是"状态写入"，必须走唯一写入口（同事务写 `created` 事件 + `system_logs`）；幂等键命中 ⇒ **原样返回旧行、不写任何东西**（不重复记账）。
  82. **Agent 返回原始 `WriterOutput`，切分在服务层再做一次**：Agent 侧算一遍只为"决定要不要重写"，落库前的切分/字数重算归 `build_draft` —— 否则同一份稿子会有两个"生效版本"。
  83. **Writer 提示词里的 `{{catchphrases}}` / `{{forbidden}}` 由 `persona_variables` 自动注入**，Agent **不得**再传同名 kwarg（`TypeError: got multiple values`），也不该有两处真相。
  84. **`est_duration_ms` 落库值一律 `estimate_duration_ms(word_count)`**（≈5 字/秒）：模型自报值只留在 `WriterOutput`，进了 `scripts` 表就变成一个**没法复算**的数。
  85. **`read_active_script()` 做成模块级函数而非 `ScriptService` 方法**：读路径不需要 Agent，为读一份稿子去凑 Director/Writer 只会让 CLI / 只读面板多背一堆依赖。
  86. **重写用尽后按 `_distance`（到字数区间边界的距离）挑最接近的一版**，**相同距离保留先出现的那一版**：并列规则必须可解释、可复盘，随机或"后者优先"都不行。
  88. **`studio script show` 必须区分"任务不存在"与"任务还没有稿"**：混为一谈会让排查的人去找一份**永远不会出现**的稿子（验收时实测踩到：打错 task_id 也回"还没有稿件"）；`--json` 下同样输出机读结构（`{"ok":false,"reason":"no_script",...}`），不落回人读文案。
  87. **`TaskPayload` 增补 `hook_type` / `persona_id`**：人物**随时可改**，任务必须记住"这一稿是哪个 persona 写的"；`hook_type` 取**归一化后**的值（见裁定 80），否则 `Literal` 会在建任务时炸。同时补 `TaskService.find_by_idempotency_key()` —— `create()` 命中幂等键时原样返回旧行，**光看返回值分不出新旧**，`DraftReport.created_task` 会说谎（实测踩到：第二次跑同一选题报 `created_task=True`）。
- ⚠️ **R6** 输出非结构化 ⇒ JSON Schema 强校验 + 修复重试（网关侧）；大纲/成稿的**语义**校验在 `domain/script.py`，两层分工不重叠
- ⚠️ 句长 >28 字 ⇒ **强制切分**（不重写）；字数越界 ⇒ 重写 ≤2 次后取最接近版本 + `warn`（**不整批失败**）
- ⚠️ E7（persona 口吻 / 受众 / 禁区）**仍未拍板** ⇒ 本轮用 `config/persona.yaml` 现值开工；阈值全部经 `ScriptRules.from_persona` 取，改人物文件即生效，历史批次可按 `prompt_version` 追溯

### T1.11 双通道评分 + Editor + 确认闸 · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.10 ｜ 里程碑：M1 ｜ 契约：§04.1.6 / §04.4.4
- [x] 规则通道 4 项 + LLM 通道 6 维度
- [x] 加权公式：`total = 0.3 × rule_total + 0.7 × llm_total`
- [x] 分级：**A ≥ 8.0 / B 5.0–7.9 / C < 5.0**
- [x] 分级放行：A 级**自动放行**（写 `approved_by='auto_approve_A'`）/ B 级进确认闸 / C 级废弃（可人工捞回）
- [x] Editor 改稿 **≤2 轮**（硬上限，防无限循环）
- [x] Editor diff 断言：**只改 issues 涉及段落**（**句子级**机检，裁定 95）
- [x] `approvals` + `audit_ops` 留痕；`revision_round` 可见
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  uv run pytest tests/unit/domain/test_scoring.py -q                                          # 97 passed
  uv run pytest tests/unit/db/test_review_repo.py tests/unit/db/test_approval_repo.py -q      # 47 passed
  uv run pytest tests/unit/db/test_audit_repo.py -q                                           # 26 passed
  uv run pytest tests/unit/agents/test_reviewer.py tests/unit/agents/test_editor.py -q        # 39 passed
  uv run pytest tests/contract/test_review_schema.py tests/contract/test_editor_schema.py -q  # 64 passed
  uv run pytest tests/unit/services/test_review_service.py -q                                 # 35 passed
  uv run pytest tests/unit/domain/test_enums.py -q                                            # 16 passed（ApprovalDecision 三值）
  uv run pytest tests/integration/test_scoring.py -q                                          # 16 passed（8.7→A / 6.3→B / 4.35→C 三条基线）
  uv run pytest tests/e2e/test_approval_flow.py -q                                            # 24 passed
  powershell -File tasks.ps1 check                                                            # ruff + mypy 全绿；pytest 1737 passed / 32 skipped
  ```
- ⚠️ **C 盘水位贴着门禁（本机环境风险，非代码问题）**：门禁已全绿，但 C 盘只剩 **1.37 GB**（`disk.gate` 门禁 ≥1 GB）。
  上一次跑门禁时它只有 0.10 GB ⇒ `disk.gate` 正确报 blocking，`tests/unit/core/test_doctor.py::test_doctor_run_covers_all_checks_on_fake_home` 变红。
  **再逼近就会复现**：跑门禁前先看 C 盘（陷阱 15 / T1.1），别把环境红灯误读成回归。
- 📦 交付物
  - 领域：`src/studio/domain/scoring.py`（权重/阈值常量 + `Decision`/`GateAction` 两个枚举 + 11 个契约模型 + 纯函数 `compute_score` / `evaluate_rule_channel` / `paragraph_dup_ratio` / `gate_action` / `decision_for` / `check_edit` / `allowed_sentences`）；`domain/enums.py` 新增 `ApprovalDecision`；`domain/task_service.py` 新增 `approved_by` 与 `bump_revision`
  - 仓储：`db/repositories/{review_repo,approval_repo,audit_repo}.py`（`record_auto_approval()` **一次事务**写自动放行）
  - 服务：`src/studio/services/review_service.py`（`review()` 主循环 / `_edit()` / `_apply_action()` 四路去向 / **`decide_approval()`**）+ 模块级 `assemble()` / `read_latest_review()`
  - 智能体：`src/studio/agents/{reviewer,editor}.py` + `schemas/{review_result,editor_result}.schema.json` + `prompts/{reviewer,editor}/`
  - 新增错误码：`REVIEW_FAILED` / `REVIEW_SCRIPT_MISSING` / `EDIT_ROUNDS_EXHAUSTED` / `EDIT_FAILED` / `APPROVAL_NOT_PENDING` / `APPROVAL_COMMENT_REQUIRED`
  - 测试：`tests/unit/domain/test_scoring.py`（97）/ `tests/unit/db/test_review_repo.py`（19）/ `test_approval_repo.py`（28）/ `test_audit_repo.py`（26）/ `tests/unit/agents/test_reviewer.py`（20）/ `test_editor.py`（19）/ `tests/contract/test_review_schema.py`（32）/ `test_editor_schema.py`（32）/ `tests/unit/services/test_review_service.py`（35）/ `tests/integration/test_scoring.py`（16）/ `tests/e2e/test_approval_flow.py`（24）
- 🐞 施工期修掉的真缺陷（**防回归**）
  1. `_edit` 把**空句子列表**传给 Editor ⇒ 越界检查永远"无越界"（改稿可以随便改）：改为透传 `sentence_rows`。
  2. `review()` 现在把 `TaskStatus.EDITING` **归一为 `REVIEWING`**：人工退回后改稿工人交回、或工人漏推状态时，`editing → queued_voice` 这条**不存在的边**会把任务炸成 `failed`。
  3. 循环内 `warnings` 从"覆盖上一轮"改为**累积**：审稿与改稿的告警不该互相抹掉。
  4. e2e 断言"`to_status='editing'` 只有一条 reason"被自动改稿的事件顶掉 ⇒ 改为断言**最后一条**（`ORDER BY id`）。
- 🔧 施工裁定（**已回灌 §02 / §04.1.6 / §04.4.4 / §05**）
  89. **LLM 只给六维度 + `issues`，`total` / `grade` / `verdict` / `decision` 全由服务端算**：让模型报总分 = 让考生自己判卷；而且总分一旦由模型给，"0.3×规则+0.7×LLM"这条硬契约就失去唯一实现（`schemas/review_result.schema.json` 同步收窄）。
  90. **`Decision.NEED_EDIT` 的语义是"未自动放行"，不是"一定要改稿"**：DDL 把 `review_scores.decision` 锁死 5 值、没有"等人工"这一项 ⇒ 把 `GateAction`（动作）与 `Decision`（落库事实）拆成两个枚举；`GateAction.HUMAN_GATE → Decision.NEED_EDIT`。
  91. **`bump_revision=True` 用 SQL 增量（`revision_round = revision_round + 1`）而非调用方传目标值**：并发下"读到的旧值 + 1"会丢掉另一方的改动。
  92. **`rule_total` = 四项等权平均（长度/禁区/开场/结尾各 0–10）− 重复度扣分**（`paragraph_dup_ratio > 0.3 ⇒ −2.0`）；长度项以**区间宽度做分母**线性扣分 ⇒ 改 persona 阈值时曲线跟着缩放，不用改代码。
  93. **`llm_total` = 六维度算术平均**：与 `rule_total` 的"等权"口径一致 —— 要加权必须有依据，不能凭手感。
  94. **自动放行也必须写一条 `approvals` 记录**（`status='approved'`、`decided_by='auto_approve_A'`），且用 `record_auto_approval()` **一次事务**写入：`request()` + `decide()` 两个事务之间会留下永远 `pending` 的幽灵行（"待审列表里有一条永远点不掉的"）。
  95. **`check_edit` 用句子级越界判定**（不是 diff 行数）：`segment:N` 按 `ceil(句数/段数)` 映射到句区间，`hook` → 第 1 句、`cta` → 最后一句、`global` → 全篇；段到句的映射**刻意取宽**（宁松不误杀）。
  96. **改稿越界 = 重试 1 次后照收 + `warnings`**，不判失败："改稿整条失败"比"稿件稍越界"贵得多，而且越界判定本身是启发式的。
  97. **每轮审稿都落 `review_scores`**（`UNIQUE (script_id, round_no)`）：重跑同一版撞唯一键是**故意的** —— "这一版被审过几次"必须能从库里看出来。
  98. **`EditorAgent` 整份回写稿件、不返回 diff**：diff 由服务端比对算（`check_edit`）；让模型输出 diff 等于把"越界判定"交给被判定的人。
  99. **确认闸的规则住在服务层**（`ReviewService.decide_approval`），**REST 留给 T4.4**：§04.4.4 的四条不变量是业务规则，写在控制器里每个入口都得抄一遍；`decide_approval` **抛 `StudioError`**（人在按按钮，失败必须说清理由，不能吞成 `ok=False`），成功返回**没有 `ok` 字段**的 `ApprovalOutcome`。
  100. **`ReviewerInput.round_no` 只保下界、不设上界**：人工在确认闸退回后 `revision_round` 继续加 ⇒ 审稿轮次自然超过 `REVISION_LIMIT+1`；轮次闸门由 `gate_action` 把守，输入模型不该当第二道闸（原 `le=REVISION_LIMIT+1` 会把"人工退回后重审"这条正常路径直接炸成 `ValidationError`，e2e 实测踩到）。
  101. **同名测试文件必须带 `__init__.py`**：`tests/unit/domain/test_scoring.py` 与 `tests/integration/test_scoring.py` 重名 ⇒ pytest 按顶层模块 `test_scoring` 各收集一次，报 `import file mismatch` **中断整轮收集**（门禁实测踩到）；补 `tests/unit/domain/__init__.py`（与 mypy "同一文件两个模块名"同一条理由）。
- ⚠️ **R16** 改稿烧 token ⇒ 轮次硬上限 2
- ⚠️ C 级误废弃 ⇒ 候选置 `rejected` 而非删除（**可人工捞回**）
- ⚠️ E7（persona 口吻 / 受众 / 禁区）**仍未拍板** ⇒ 本轮用 `config/persona.yaml` 现值开工；评分阈值经 `ScriptRules.from_persona` 取，改人物文件即生效

- 📌 **遗留**：`studio review run|show` 两个 CLI 未做 ⇒ 并入 T4.4 的 REST 薄壳（规则已在 `ReviewService.decide_approval`，CLI 只是第二个入口，**不阻塞 M1**）
### T1.12 一键启动与关停 · **P0** ✅ **已完成（2026-09-13）**
- 依赖：T1.11 ｜ 里程碑：M1 ｜ 契约：T1.12 / 原文附2 / **§04.8**
- [x] `启动.bat`：先跑 `studio doctor`（失败则中止）⇒ 拉起 5 进程（API / TTS / draft / voice / render）⇒ 健康等待 ⇒ **自动打开浏览器**
- [x] `停止.bat`：优雅关停，**不丢进度**（标志 ⇒ 信号 ⇒ 强杀三级时序；强杀**如实记账**，不假装优雅）
- [x] 端口占用预探测 + 提示（**与「已在运行」分开报**：`port_busy` vs `already_running`）
- [x] **WebUI 挂掉不影响后台任务**（五进程各自 `Popen` + 独立日志 + 只认自己的 PID 台账）
- [x] 未就绪的进程 ⇒ 报 `degraded` 且**不拉起空转 worker**（裁定 103）；TTS 未就绪 ⇒ 其余照常启动
- [x] `studio service start|stop|status`（人读表 + `--json` 机读）+ `ops/*.ps1` 薄壳 + 两个 `.bat`
- ✅ 验收命令（2026-09-13 实测全过）
  ```powershell
  uv run pytest tests/unit/services/test_service_manager.py -q   # 54 用例（规格表 / 就绪 / 端口 / 启停时序 / run_entry）
  powershell -File ops\start_all.ps1 -NoBrowser                  # ok=true；ready=['api']；degraded=[tts,draft,voice,render]；elapsed_ms=4827
                                                                #   （含 uv run 开销；进程内直调 ServiceManager 时 ≈1.4s）
  powershell -File ops\status.ps1                                # api：pid / 存活=是 / 8787 / 端口占用=是 / data\logs\api.log
  powershell -File ops\stop_all.ps1                              # stopped=['api']；forced=[]；155ms
  powershell -File tasks.ps1 check                               # ruff + mypy 全绿；pytest 1791 passed / 32 skipped
  ```
- 📦 交付物
  - 编排：`src/studio/services/service_manager.py`（`ServiceSpec` / `ServiceReadiness` / `PortProbe` / `StartReport` / `StopReport` / `ServiceStatus` + `ProcessTable` 注入点 + `probe_port` + `build_specs` + `ServiceManager` + `run_entry` + `default_manager`）
  - 入口：`workers/run_{api,tts,draft,voice,render}.py`（薄壳，一律走 `run_entry`）
  - CLI：`studio service start|stop|status`；`app/main.py:run_server()`（与 `workers/run_api.py` **共用一份**）
  - 路径：`StudioPaths.{workers_dir,pid_file,stop_flag_file,service_log_file}`
  - 脚本：`启动.bat` / `停止.bat` / `ops/{start_all,stop_all,status}.ps1`（薄壳 · CRLF）
  - 新增错误码：`SERVICE_PORT_BUSY` / `SERVICE_ALREADY_RUNNING` / `SERVICE_START_FAILED` / `SERVICE_START_TIMEOUT` / `SERVICE_STOP_FAILED` / `SERVICE_NOT_READY`
  - 测试：`tests/unit/services/test_service_manager.py`（54 例；全假进程表 ⇒ 关停时序**可测**）
- 🔧 施工裁定（**已回灌 §02.2 / §04.8 / §05.7**）
  102. **关停主通道是标志文件 `data/logs/<name>.stop`，不是信号**：`GenerateConsoleCtrlEvent` 只能作用于**同控制台**的进程组，而 `停止.bat` 是另起一个控制台 ⇒ 从那里发的 Ctrl-Break **静默失效**（API 返回 TRUE 但目标收不到），最后只能硬杀 ⇒ 丢进度。池 worker 在 1s 脉冲 tick 里看到标志就 `request_stop(reason="stop_flag")`；信号与强杀只作**升级手段**。
  103. **未就绪的进程不拉起**：`pools.runner.handler_for()` 故意在 handler 未注册时报错 ⇒ 管理器的职责是「先问再拉」：报 `degraded` 并照常启动其余进程，而不是放行一个永远不消化队列的空转 worker。
  104. **编排核心是 Python，`ops/*.ps1` 退化为薄壳**：关停时序是最需要被验证的东西，写在 `.ps1` 里既测不了也改不动；T4.12 的总览台启停直接 import 同一份实现，不必写第三遍。
  105. **PID 台账 / 关停标志 / 服务日志的路径规则只有一处真相**（`StudioPaths.pid_file()` / `stop_flag_file()` / `service_log_file()`）：Python 与 PowerShell 都按它找进程。
  106. **子进程 stdout/stderr 追加到 `data/logs/<name>.log`**（`PYTHONUNBUFFERED=1` + `stdin=DEVNULL` + `CREATE_NEW_PROCESS_GROUP`）：起来就死时第一眼就有原因。
  107. **`ServiceSpec.polls_stop_flag` 区分「会不会自己轮询标志」**：池 worker（有 1s tick）为 `True`，HTTP 面的进程（uvicorn 无 tick）为 `False` ⇒ 对它直接走信号，免得白等一个「永远不会发生」的优雅退出。关停时序 = ① pollers 等标志（`budget×0.6`）→ ② Ctrl-Break（`max(0.5, budget×0.3)`）→ ③ `terminate`（`max(1.0, budget×0.2)`）⇒ `kill`（1.0s）；强杀记入 `StopReport.forced`（`ok=False`）。
  108. **M1 的「5 进程全部 ready」不可能在 M1 达成**：`tts` 属 T2.2、`voice` 属 T2.6、`render` 属 T3.x、`draft` 属 T4.11 ⇒ M1 验收口径改为「`api` ready + 其余**如实报降级**」，「5 进程全 ready」顺延 M4。**为了让验收数字好看而放行空转 worker 是错误做法**（103 的推论）。**（← 这条口径变更需要你确认）**
  109. **测试假件必须与「被测对象看的那张表」接在一起**（`Recorder(table)`）：`make_manager` 只在「自造 Recorder」时接线，调用方传进来的 Recorder 保留自己的表 —— 这是**故意**留的口子（用来构造「起完立刻死」），代价是其余用例必须显式接线；本轮 4 个用例就踩在这里。
  110. **lint 债必须由 `tasks.ps1 check` 兜住**，不能只跑 `ruff check src`：本轮 14 条错误（失效的 `noqa`（RUF100）/ `TRY400` / `T201` / `PLC0415` / `PLW0108`）全在「只查 src、不查 format」的盲区里。`run_entry()` 是进程入口（诊断必须进日志）⇒ `ruff.toml` 给它开 `T20` 白名单（与 `cli.py` 同一条理由）。
- ⚠️ 新增陷阱 5 条已并入 §05.7（编号 34–38）：跨控制台 Ctrl-Break 静默失效 / 残留 `.stop` 标志导致「启动后什么都不干」/ 把「端口被占用」报成「已在运行」/ 对 uvicorn 白等优雅退出 / 子进程日志丢失
- ⚠️ **一期降级现状（T1.12 时实测）**：`api` `ready`；`tts` `server_missing`；`draft`/`voice`/`render` `handler_missing`
  ⇒ 真拉起 5 进程要等 T2.2 / T2.6 / T3.x / T4.11。**2026-09-16 更新**：后三项都已落地（T4.11 / T2.6 / T3.7）
  ⇒ 只剩 `tts` 报 `server_missing`（等 `T2.2` 常驻服务；**E5 权重已于 2026-09-17 就位**）
- ⚠️ 原文 §7.4 要求 WebUI 与 Worker 完全独立（ADR-002）⇒ 已由「五进程各自 `Popen` + 独立日志 + 只动自己台账」落实

> **>>> M1 门禁**：网页端输入定位 + 热点 ⇒ 产出合格稿件（含评分）⇒ 确认闸可见；`启动.bat` 一键拉起全部服务。
>
> **M1 口径（T1.12 裁定 108）**：一键启动**已可用**（`api` ready + 其余如实报 `degraded`），但「5 进程全部 ready」
> **在 M1 阶段不可能达成** —— `tts` 属 T2.2、`voice` 属 T2.6、`render` 属 T3.x、`draft` 属 T4.11。
> 验收以「未就绪进程被**如实报告**且不阻塞其余进程」为准（P4：宁要真话，不要好看的假绿灯）；「5 进程全 ready」顺延到 M4。

---
## 2. 阶段 T2 · CosyVoice 配音（9 任务 → 门禁 M2）

### T2.1 tts venv + 模型权重就位 · **P0** ✅ **已完成（2026-09-17）**
- 依赖：T1.1 ｜ 里程碑：M2 ｜ 契约：§01.4 / §04.3.1
- [x] tts venv = **Python 3.11.15** + torch **2.4.0+cu121**（复用 `D:\Torch` 预置 wheel，**未重装 CUDA Toolkit**）
- [x] CosyVoice 源码就位 + **revision 锁定并留痕**：`D:\ai_models\CosyVoice` @ `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`（2026-05-26）·
      子模块 Matcha-TTS @ `dd9105b34bf2be2230f4aa1e4769fb586a3c824e`（浅克隆 + 浅子模块）
- [x] 权重落 `D:\ai_models\modelscope_cache\models\iic--CosyVoice2-0.5B\snapshots\master`（**不落 C 盘**）：
      **21 文件 / 5.23 GB**，**73 秒**下完，与远端清单逐条一致
- [x] 版本基线：**CosyVoice2-0.5B**（Q7 裁定）；若 CosyVoice3 可下载且接口兼容 ⇒ 只改 `tts.yaml` 的 `model_dir`/`revision` 即可切换
- [x] `MODELSCOPE_CACHE` 生效验证（下载全程**未写 C 盘**）
- [x] 推理依赖清单 **`tts/requirements-cosyvoice.txt`**（**故意不写 torch / torchaudio** —— 裁定 304 / 陷阱 159）
- ✅ **真机读数**：模型加载 **10.4s** · 显存 **2.38 GB** · 合成一句 13 字中文 **8.4s → 7.72s 音频（RTF 1.09）**
      · 采样率 24000 · 输出 RMS 0.0071 / peak 0.0757（**非静音**）；样音留档 `data/output/voice/_cosyvoice2_smoke_bigbear.wav`
- ✅ 环境自检：`.\tts\.venv\Scripts\python.exe -c "import torch;print(torch.__version__, torch.cuda.is_available())"` ⇒ `2.4.0+cu121 True`；权重目录**可加载**（真机 smoke 见上）
      · `cosyvoice` **未装进 venv**，走 `PYTHONPATH` 指向源码目录（陷阱 160 的双路径要求）
- ⚠️ 已知**非故障**：`onnxruntime` 跑 CPU（未装 `onnxruntime-gpu`）；无 `ttsfrd` 前端（走内置 normalize，符合 T2.5 取舍）
- ⚠️ 禁用 bf16（Turing sm_75 不支持）
- ⚠️ 陷阱 159（上游 requirements 写死 torch 2.3.1）· 160（`openai-whisper` 要 `pkg_resources`）· 161（`inference_zero_shot` 第三参是路径）
- **施工裁定（本轮新增 304）**：
  - **304** **不照抄上游 `requirements.txt`**，单独维护 `tts/requirements-cosyvoice.txt`：上游锁 `torch==2.3.1`，与本项目已验证的 `2.4.0+cu121` 冲突 —— 照单全收等于把一个能跑的环境换成另一个要重新验证的环境

### T2.2 常驻推理服务 + 并发实测标定 · **P0** ✅ **已完成（2026-09-17）**
- 依赖：T2.1 ｜ 里程碑：M2 ｜ 契约：§04.3.5 / 裁决 C8
- [x] HTTP 服务接口：`/health`、`/warmup`、`/unload`、`/voices`、`/synth`（`src/studio/tts/server.py`）
- [x] GPU 串行信号量（**实测裁定 1**，可调 1–3；`config/tts.yaml: server.concurrency`）
- [x] **fp16 常驻** + 空闲 20min 卸载（看门狗 30s 一跳；`POST /unload` 手动版）
- [x] 429 背压（`queue_max=8`；超了回 `TTS_BUSY`，**排队不雪崩**）
- [x] **实测标定**：`scripts/bench_tts.py` 跑并发 1/2/3 ⇒ 结论 **1**，已回写 `pools.yaml` 与 `tts.yaml`
- [x] OOM 自动降并发（T4.10 已落地：`db/queue.py` 连续 `oom_threshold` 次 `TTS_OOM` ⇒ 池并发 −1 + `audit_ops`，恢复需人工确认）
- ✅ **真机标定读数**（RTX 2080 SUPER 8 GB · `docs/runbook/tts_concurrency.md` · 原始读数 `tts_concurrency.json`）：
  模型加载 16890 ms · 常驻显存 2442 MB
  | 并发 | ok | 中位 RTF | 峰值显存 | 墙钟 |
  | --- | --- | --- | --- | --- |
  | 1 | 3 | **0.819** | 3643 MB | 41.38s |
  | 2 | 3 | 1.344 | 4060 MB | 36.67s |
  | 3 | 3 | 2.021 | 4090 MB | 35.61s |
  ⇒ **2/3 路都不会 OOM，但吞吐只快 11% / 14%（理想 +100%），中位 RTF 却涨 1.6–2.5 倍** ⇒ 裁定 **306**：并发取 **1**
- ✅ **真机 HTTP 冒烟**（跑在 8799，**没碰你自己的 8787/8788**）：`/health` ⇒ `ready:true · device:cuda · fp16:true · sample_rate:24000`；
  `/warmup` 真念一句；`/voices` 列出 `bigbear` / `littlebear` 均 usable；`/synth` ⇒ `duration_ms=12000 · synth_ms=11515 · rtf=0.96`
  （ffprobe 复核 12000ms / 24000Hz / 有音轨）；`../evil.wav` 与 `C:/evil.wav` ⇒ **400 `CONFIG_PATH_OUT_OF_BOUNDS`**；
  未知音色 ⇒ **422 `TTS_VOICE_MISSING`**（带 `available` 清单）；`/unload` ⇒ `freed_mb=2402`
- ✅ **验收命令**：`pytest tests/unit/tts/test_server.py tests/unit/tts/test_cosyvoice_backend.py -q` ⇒ **38 passed**（FakeBackend 注入，无 GPU 也能跑）；
  `pytest tests/unit/services/test_service_manager.py -q` ⇒ **60 passed**（含「argv 指向子环境解释器」「PYTHONPATH 前置」「配了 python 但文件不在 ⇒ 不静默换」）
- ✅ **门禁（2026-09-18）**：`.\tasks.ps1 check` ⇒ **3715 passed / 32 skipped / 27 deselected**（162s）；
  `web: npm run verify` ⇒ **428 passed** + `vite build` + dist 体积门禁 OK（本轮**没动前端**）
- ⚠️ **R4 显存紧**（8GB，桌面占 1.49GB）⇒ fp16 + 单并发 + 禁 bf16
- ⚠️ 陷阱 #10 首句延迟 20s+ ⇒ 常驻服务 + `/warmup` + 空闲卸载
- ⚠️ 陷阱 **162**（`tts/.venv` 缺 `tzdata` ⇒ `import studio.core.clock` 抛 `ZoneInfoNotFoundError`）· **163**（子环境没装 `studio` 包 ⇒ 启动器必须前置 `PYTHONPATH`）
- **施工裁定（本轮新增 305–308）**：
  - **305** 音色的**注册 / 注销不在推理服务上**（`POST|DELETE /voices` ⇒ **501**）：入库判定（四条硬拒 + R2 留痕）只在 T2.4 的 `AssetService.ingest` 有一份，服务上再实现一遍就是两份判定迟早对不上（与陷阱 #150 同族）
  - **306** 常驻服务的并发**不按显存判，按吞吐增益判**：8 GB 卡上 2/3 路不 OOM，但吞吐只 +13%、RTF 涨 1.6–2.5 倍，且 render 池要共用这块卡 ⇒ 取 **1**。门槛写死在 `bench_tts.py: MIN_GAIN_RATIO = 0.4`（升一档至少拿到理想增益的 40%）
  - **307** `tts` 的「模块在不在」（`server_missing`）与「子环境在不在」（`env_missing`）**分开报**：模块不在 ⇒ 写代码，环境不在 ⇒ 建 venv；最糟的第三种结果是拉起来一个「活着、`/health` 回 200、每句都报 `no module named torch`」的进程（**真机复现过**：T2.2 落地前起的那个 `tts` 进程就是这个状态）
  - **308** 服务自己带 `concurrency` 时，标定脚本必须在**进程内**直驱引擎：打 HTTP 测到的是**排队**，不是显卡容量。`--from-json` 支持拿上次读数重出报告 —— 改措辞不必重跑 40 秒 GPU
  - **309** **`mypy.ini` 里不能写 `[[mypy.overrides]]`**（那是 `pyproject.toml` 的写法）：写在 INI 里会被 mypy **静默忽略**，不报配置错。原文件里那段一直没生效，只因那几个包恰好都装着，所以没人发现；直到 `torch` / `soundfile` / `cosyvoice` 只在子环境里，才炸出来。改成逐段 `[mypy-<模块>]`（见陷阱 164）

### T2.3 引擎适配层与路由 · **P0** ✅ **管子已真机跑通（2026-09-18）· 熔断与决策表仍缺**
- 依赖：T2.2 ｜ 里程碑：M2 ｜ 契约：§04.3.2 / §04.3.3
- [x] **薄片：配音池 → 常驻服务这根管子**（`src/studio/tts/service_engine.py`）
  - [x] `ResidentEngine`：`SentenceEngine` 的服务实现（POST `/synth`，`out_path` 转成相对 `data/`）
  - [x] 每一次取用时选引擎（`voice_worker._EnginePicker`）：服务可用 ⇒ 用它；否则 **SAPI 兜底**（§1.7，成片照样有人声）。只升不降 —— 裁定 314
  - [x] 「哪个音色念得出来」跟着换裁判（`voice_service.speakable_voices(paths=...)`）⇒ 面板与投递同时生效
  - [x] 远端错误码**原样保住**（`TTS_OOM` 仍是 `TTS_OOM` ⇒ T4.10 的自动降并发才认得出）
- [ ] `VoiceEngine` ABC：**零样本复刻**（参考音频 + 参考文本 + 目标文本）的正式契约 —— 现在用的是 T2.6 那个最小缝（`SentenceEngine`）
- [ ] 多引擎路由（Mock / 常驻服务 / CosyVoice 三实现可互换）—— 现在只有"常驻服务 or SAPI"两条
- [ ] 熔断：连续失败 ⇒ 打开熔断，N 秒后半开
- [ ] §04.3.3 降级决策表**逐条落地**：OOM / 超时 / 静音 / 爆音 / 引擎宕 / 连续失败熔断（现在只有"引擎宕 ⇒ 重试 ⇒ 到线降级"那条既有路径）
- ✅ **真机验证（2026-09-18 · 隔离家目录 · 真服务在 8799 · 没碰你的 8787/8788）**：
  模型加载 31922 ms / 显存 2442 MB ⇒ `active_resident` 认出 `bigbear`+`littlebear` ⇒
  `build_voice_handler` 选中 **ResidentEngine（cosyvoice2）**、兜底音色 `bigbear` ⇒
  真念一句走 **HTTP**（不是进程内直驱）⇒ `s001.wav` **16800 ms / 24000 Hz / 806 KB**，
  引擎 `cosyvoice2`、音色 `bigbear`；电平 mean −47.4 dB / max −23.7 dB
  （与 T2.1 的 known-good 样音 −43.0 / −22.4 同一档，**不是静音**，交给 T3.6 的 `loudnorm` 抬）。
  样音留档 `data/output/voice/_t23_resident_bigbear.wav`
- ✅ **全链路真机出片（2026-09-18 · 你的宿主环境 · 8787/8788）**：五进程全就绪（tts 模型加载 **22640 ms**）⇒
  `GET /pipeline/console` 与 `GET /render/console` 都报 `engine=cosyvoice2 / engine_ready=true`（不再是写死的 `sapi`）⇒
  对任务 `01M2M9THZ1M83CT5EJH789ZVZA` 的 4 句走 `POST /sentences/{id}/resynth`（起点是 **SAPI 静音占位**）⇒
  4 句全 `done`、`tts_engine=cosyvoice2`、`tts_voice_id=bigbear`、**`tts_attempts=0`**、`tts_sample_rate=24000`（一次成功）⇒
  `POST /render/jobs` ⇒ **`data/output/videos/20260918-011841_01M2M9THZ1M83CT5EJH789ZVZA_final.mp4`**
  （28.2 MB · 1080×1920@30 · h264 + aac 48k · **38.0s**），音轨 `mean -18.4 dB / max -1.1 dB`（**不是静音**，
  静音会是 ≈−91 dB）、字幕烧进画面、水印按裁定跳过。
  同一任务上一版静音成片是 11.8 MB —— **差距就是人声**
- ✅ **空闲卸载复验（2026-09-18 · 宿主 8787/8788）**：`POST /unload`（释放 2402 MB）制造"睡着的实例" ⇒
  `/health` 报 `ready:false / model_state:unloaded` ⇒ **`GET /pipeline/console` 与 `/render/console` 仍报
  `engine=cosyvoice2 / ready=true`、无降级横幅**（修前这两处会显示 `sapi` + 一段"查 tts.log"的误导文案）⇒
  `studio service start --only tts` 报「**已在运行**」+ `service.ready detail='自报未就绪但可唤醒：…'`（109 ms，不重启）⇒
  台账缺失时（人为删 `data/logs/tts.pid`）只记 `service.port_busy_wakeable`、**不杀那个睡着的实例**
  （修前会 terminate→kill 一个健康实例）⇒ `POST /sentences/{id}/resynth` ⇒ 池子 `tts.resident_active wakeable=True`、
  选中 ResidentEngine、`POST /synth` **冷加载 8625 ms** 后念出（整单元 21640 ms）⇒ 4 句全 `done` /
  `cosyvoice2` / `bigbear` / **`tts_attempts=0`** ⇒ `POST /render/jobs` ⇒
  **`data/output/videos/20260918-113754_01M2M9THZ1M83CT5EJH789ZVZA_final.mp4`**（35.2 MB · 1080×1920@30 · **48.1s**）
- ✅ **门禁（2026-09-18）**：`.\tasks.ps1 check` ⇒ **3770 passed / 32 skipped / 27 deselected**（171s）；
  `.\tasks.ps1 web:verify` ⇒ **428 passed / 20 文件** + `vite build` + dist **0.35 MB / 3.00 MB** OK
  （本轮动了前端：两块面板的引擎横幅 + 配音面板加「引擎」列；契约已用 `web:gen` 重生成，**无漂移**）
- ✅ **验收命令**：`pytest tests/unit/tts/test_service_engine.py -q` ⇒ **27 passed**（无 GPU）；
  `pytest tests/unit/services/test_voice_service.py tests/unit/services/test_service_manager.py -q` ⇒ **79 passed**；
  `pytest tests/unit/pools/test_voice_worker.py -q` ⇒ **29 passed**（本轮 +5：升级时序 / 只升不降 / 音色兜底 / 只解析一次 / 注入不探测）
- **施工裁定（310–315）**：
  - **310** **引擎判据只此一份**（`service_engine.active_resident`），配音池装配与 `speakable_voices` 共用：
    两处各判一次，就会出现「面板说 bigbear 能念、池子把它交给 SAPI」—— `SelectVoice` 直接抛 ⇒
    每句失败 3 次 ⇒ 成片没人声，而库里写着换音色成功（与陷阱 #154 同族）
  - **311** ~~引擎选择在**装配期**定一次~~，且**注入引擎时不探测**：探测会把注入的假件顶掉。
    前半句（"装配期定一次"）**已被裁定 314 推翻** —— 它把整条链路钉在了 SAPI 上；
    后半句仍然成立（注入引擎 = "就用这一个"，不探测）
  - **312** **进程活着 ≠ 服务能用**：就绪判据必须吃服务**自报的 `ready`**，
    不能只看 HTTP 状态码。真机实例：端口上是一个 9/17 23:26 起的旧 tts（那时 `config/tts.yaml`
    还没有 `python` 键 ⇒ 用主 venv 起 ⇒ 没 torch）：`/health` 回 200、`ready:false`，
    守护进程因为只看状态码而认定"已就绪" ⇒ **永远不重启它** ⇒ 配音一直悄悄退回系统语音包。
    服务不自报 `ready`（如 api）不受影响
  - **313** 端口上占着的是**我们自己、没就绪**的实例 ⇒ 由编排器**接管重启**，而不是报 `port_busy` 让人去手动查。
    三条判据同时成立才接管：`ready is False` + 有 `engine` 字段 + `pid` 是 int 且进程活着。
    认不出是谁（旧版本不自报 `pid`）就**不动手**，只记 `service.port_busy_unready` 并给 hint ——
    误杀别人的进程比多跑一次 `start` 严重得多
  - **314** 引擎在**每一次取用时**判定（`_EnginePicker`），且**只升不降**。
    真机实测：启动器**同时**拉五个进程，而 tts 要先把模型读进显存（真机 17–22s）
    ⇒ 配音池的装配期必然落在那几秒里 ⇒ 判据永远是"服务不可用"。
    只升不降是因为反过来的"中途降回 SAPI"会让同一支片子一半 CosyVoice、一半系统音色 ——
    听起来只是"有几句话怪"，比如实失败难查得多。同时 payload 里的音色若**当前引擎念不出来** ⇒
    改用当前引擎的兜底音色并写一行 warning（不硬交给它 ⇒ 每句失败 3 次 ⇒ 成片没人声）
  - **315** `ready: false` 有**两种**语义，必须分开：①**坏了**（子环境里没有 torch，该接管重启）
    ②**只是睡着了**（`model_state: unloaded`，空闲 20 分钟卸显存）。判据只此一份
    （`service_engine.wakeable_health`）：睡着 ⇒ **算可用**（配音池照用常驻引擎、编排器照记就绪，
    代价只是第一句多等一次冷加载）。混成一种会同时犯两个错 —— 编排器去杀一个睡着的健康实例、
    配音池悄悄退回系统语音包（成片里换了个人念，而日志/面板/库里全写着 `cosyvoice2`）
- ⚠️ 引擎替换成本 ⇒ ABC 隔离，换引擎不动业务代码
- ⚠️ 陷阱 **165**（引擎名与版本**必须**从服务自述来，客户端写死会让旧缓存被当成新引擎的产物复用）
- ⚠️ 陷阱 **166**（`/health` 回 200 却是坏实例；守护进程只看状态码 ⇒ 永远不重启它）
- ⚠️ 陷阱 **167**（子环境进程入口借 `services` 的骨架函数 ⇒ 拖进整层依赖）
- ⚠️ 陷阱 **168**（空闲卸载被当成故障 ⇒ 要么误杀健康实例，要么静默退回系统语音包）

### T2.4 原声入库与音色注册 · **P0** 🔶 **部分完成（2026-09-17）** · 正式音色仍需 E4
- 依赖：T2.2 ｜ 里程碑：M2 ｜ 契约：§04.3.1 / R2
- [x] 目录契约：`data/voice_src/{bigbear,littlebear}/` + `profile.json`
- [x] 质量校验的**四条硬拒**：段数 2–3 / 单段 10–30s / 无削波（峰值 ≤ −1.0 dBFS）/ 采样率 ≥ 16 kHz
- [ ] 质量校验的三条 `warn`：无 BGM / 有效语音占比 ≥ 70% / 单发言人 —— **本轮刻意不做**（裁定 287）
- [x] 入库命令 `scripts/ingest_voice_src.py`：扫目录 → 体检 → 写 `voice_profiles`（判定不重写，见裁定 289）
- [ ] 零样本复刻注册 + **试听样本生成** —— 注册已通（库里有行 ⇒ `usable_voices` 选得到）；试听样本待 `T2.3`（**常驻引擎已于 T2.2 就位**，差的是引擎适配层与路由）
- [x] **R2 合规**：音色 ID 与展现名**解耦**（`voice_map` 可换，配音面板可操作，**不用改代码**）
- [x] 来源登记留档（`proof_path` ← 目录里的 `profile.json`）
- ✅ **验收命令**（规格里的 `studio tts list` **不存在**，口径改如下 —— 裁定 288）：
  - `.venv\Scripts\python.exe -m pytest tests/integration/test_voice_profile.py -q` ⇒ **13 passed**（四类拒绝逐条 + 旁车只 `warn` + 坏的不拖垮好的 + 默认映射真的接得上）
  - `.venv\Scripts\python.exe scripts\ingest_voice_src.py --dry-run` ⇒ 逐条体检、**一个字节不写库**；全通过退出码 **0**、有任何一个被拒 ⇒ **1**（可挂批处理）
- **真机（2026-09-17）**：`--dry-run` 4 个目录全过；`--voice bigbear --voice littlebear` ⇒ `新增 2 / 未入库 0`；
  `usable_voices` ⇒ `('bigbear','littlebear','Microsoft David Desktop',…)`
- **端到端真机出片（2026-09-17 · 隔离家目录 · 真 SAPI / 真 ffmpeg）**：文案 4 句 → 配音 → 渲染
  ⇒ **`data/output/videos/20260917-172940_<task>_final.mp4`（16.3 MB · 1080×1920@30 · h264 + aac 48k 立体声 · 23.63s）**；
  4 句全 `done`（`tts_attempts=0`、**0 降级**）、时间轴单调不重叠（`0→5083 / 5394→11381 / 11595→17043 / 17292→22785`）；
  成片音轨 `mean -20.4 dB / max -1.3 dB`（**不是静音**）、字幕烧进画面、`lufs=-16.21 / true_peak=-1.26`；
  水印 `watermark_applied=false`（E2 未到位 ⇒ 按裁定跳过，不影响出片）
- ⚠️ **修前这条链路是"出得来片、但没有声音"**：音色解析把 `bigbear` 判成"本机有" ⇒ 传给 SAPI ⇒
  `SelectVoice` 抛 ⇒ 每句连失败 3 次降级成静音。见裁定 290 / 陷阱 #154
- ⚠️ **R2 法律风险**（熊大熊二是《熊出没》IP）⇒ 解耦 + 可替换 + WebUI 显著提示 + 留档
- ⚠️ 原声缺失 ⇒ T2.3 熔断走降级，链路不断
- **施工裁定（本轮新增 286–292）**：
  - **286** 占位音色的目录名**必须**与 `TaskPayload.voice_map` 的默认值一致（`bigbear` / `littlebear`）：不一致的后果**不是报错**，而是每个角色都悄悄退回 `fallback`（进程音色）—— 盘上明明躺着两个能用的，系统一个都没用，而这件事**只有翻 manifest 才看得见**。占位脚本原先叫 `bear_da` / `bear_xiong`，就是这么坏的。**开箱即用是占位件的唯一价值**：要用户先去面板改一次映射才用得上，它就一件事都没省下。连带两条：`ref.txt` 要**一段一行**（写 1 行、2 段 ⇒ 一条**恒真**的 `ref_text_mismatch`，等哪天真的对不上就没人当回事了）；音高按**位次**分而不是按名字里的字（名字是自由起的，拿它当判据，改个名字两个音色就一模一样）
  - **287** 「无 BGM / 有效语音占比 / 单发言人」三条**留在 `warn` 且本轮不做**：它们要谱平坦度 + 静音占比 + 说话人嵌入，是**三个新的音频分析子系统**，而一期它们**一条都不拦**（§4.3.1 表里就是 `warn`）。本轮用户的优先级是"先把 MP4 打通"（明确要求不写复杂资产校验）。**不假装**：`check_voice` 里没有这三条，规格里的 `-k quality`（含 BGM 被标 `warn`）**因此跑不出来** —— 缺就写缺，比填一个恒 `pass` 的假检查诚实
  - **288** 规格的验收命令 `studio tts list` **不存在**（`cli.py` 里没有 `tts_app` / `voice_app`），**口径改为**脚本退出码 + `tests/integration/test_voice_profile.py`。**不为一句验收命令补一个 CLI**：它能说的（"这台机器现在有哪些音色"）配音面板已经说了，而且是**同一条** `usable_voices` —— 再开一个面只会多一处会过时的显示（与裁定 281 同一条理由）
  - **290** 「本机有什么音色」（`usable_voices`：已启用参考音 ∪ 系统音色）与「**当前这台引擎念得出来吗**」（`speakable_voices`）**必须分开**。前者是**下拉框的候选**，它必须全（否则用户以为刚入库的音色丢了）；后者是**能不能真出声**，唯一的裁判是当前引擎。把它们合成一个并集喂给校验，后果是**整片静音**：`bigbear` 通过校验 ⇒ 写进作业 payload ⇒ SAPI `SelectVoice` 抛 ⇒ 这一句连失败 3 次、降级成静音，**每一句都是** ⇒ 成片没人声，而库里写着"换音色成功"。真机实测：`synthesize(voice="bigbear")` ⇒ `rc=1` + `SelectVoice` 异常；同一段文本传 `voice=None` ⇒ 正常出 162 KB 的 wav。**这不是理论风险**：本轮把占位音色改名对齐默认映射之后，并集立刻把它变成了一条真会走到的路径
  - **291** 「本机没有」与「本机有、但这台引擎念不出来」在 422 里要**分开说**（`context.absent` / `context.unspeakable`）：两者要用户做的事完全不同 —— 前者去入库一个音色，后者**入库没用**，要等 CosyVoice（T2.1 / E5）。混成一句"本机没有"，用户会去重新入库一个已经入好的音色，然后发现还是不行
  - **292** 这件事**必须写在面板上**（`VoiceOptions.voices[].speakable` + 每行标签「当前引擎念不出来」+ `note`），不能只写在文档里：`source="profile"` 只说了"它是参考音"，听起来只是"另一种音色"。选它的代价（整片没人声）要到**播放成片**那一刻才暴露，而那时人已经在看别的了。**代价要在按下按钮之前显示**
  - **289** `scripts/ingest_voice_src.py` **只打印、不判定**：阈值与旁车检查全在 `studio.assets.validate.check_voice`，入库走 `AssetService.ingest` —— 与素材库面板点「扫描并入库」是**同一条代码路径**。理由：「命令行说能进、面板说不能」这种**两个真相源**的 bug 极难查（陷阱 #150 / #151 是同一族）。脚本每多写一遍阈值，就多一个漂移点

### T2.5 文本归一化与切分 · **P0** ✅ **已完成（2026-09-15）**
- 依赖：T1.10 ｜ 里程碑：M2 ｜ 契约：§04.3.6
- [x] 归一化：数字 / 百分比 / 日期 / 英文缩写 / 多音字 / emoji
- [x] **幂等性**：`normalize(normalize(x)) == normalize(x)`
- [x] 标点 → 停顿映射
- [x] 切分：单句 **4–28 字**，不破坏语义边界（不在数字/英文单词中间切）
- [x] glossary 热更新（不改代码即可纠正读音）
- [x] **不引入 `pynini`/`WeTextProcessing`**（Windows 装不上）
- ✅ 黄金用例 **59 条**（线是 ≥40）；幂等性在**全部黄金输入 + 一段"什么都掺了"的段落**上两轮收敛；切分长度约束与拉丁词边界逐条断言
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/unit/tts -q` ⇒ **165 例**（`test_normalize.py` 147 + `test_segmenter.py` 18）
  - `.\tasks.ps1 check` ⇒ **2633 passed / 32 skipped / 1 deselected**；`ruff` + `mypy`（244 文件）全绿
- **交付物**：`prompts/shared/glossary.yaml`（**数据，可热改**）；`src/studio/tts/{__init__,text_normalize,segmenter}.py`；`src/studio/core/paths.py::glossary_file`；`tests/unit/tts/{test_normalize,test_segmenter}.py`
- **施工裁定（本轮新增 197–205）**：
  - **197** 词表是**数据不是代码**：`prompts/shared/glossary.yaml` 按 mtime 热重载（`GlossaryStore`），改一行 YAML 立刻生效 —— 听到"银行"念错就要能当场改。要求重启 TTS 常驻服务（模型加载几十秒）会把这件事变成"攒一批再改"，代价是这一批片子全带着错音发出去
  - **198** 幂等性**由加载期校验结构性保证**：任一 value 不得含任何 key，否则 `A→AB` 且 `B→C` 会让 `A` 第一遍变 `AB`、第二遍变 `AC` ⇒ 同一句话两次合成出两个音，句级缓存（T2.6）永远命不中。另加"单遍扫描"兜底：换出来的字不再参与本轮匹配
  - **199** **启动失败要抛、运行中失败只记**（与 `PersonaStore` 同一条）：启动时文件不存在 = 装错了（`CONFIG_MISSING`，doctor 提前拦）；运行中改坏 = 保住上一份好的 + 一条 `warning` —— 正在合成的任务不该因为别人在编辑词表而中断。坏版本连同 `(mtime, size)` 一起记住 ⇒ 一直坏着只告警一次
  - **200** **步骤 1 不能吃掉步骤 2 要用的字符**：`2026-09-15` 里的 `-` 一旦当"特殊符号"删掉，日期再也认不出来。所以 `_UNSPEAKABLE` **特意不含** `.` `%` `-` `/` `:`，留给步骤 2 消费；没消费完的由步骤 5 清掉
  - **201** **日期必须排在区间之前**：若 `(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})` 晚于 `X-Y` 区间规则，`2026-09-15` 会被当成"2026 到 09"的区间，后面剩一个孤零零的 `-15`
  - **202** 半角句点**只在句末位置**才算句号（后面是空白 / 结尾 / 中文）：`a.b.com` 里的点夹在 ASCII 词中间，当句号会在词中间插一个 420ms 停顿，切分器还会就此多切一句、多花一次引擎调用
  - **203** 数字标记**原样保留**：`3号` ⇒ `三号`（**不是**"三日"）。第一版把 `[日号]` 统一成 `日`，日期对了、序号全错
  - **204** 停顿是**元数据**，不进正文（`PAUSE_MAP` + `pause_after_ms()` 单独导出）：塞进正文会污染句级缓存键。"最长优先"匹配：`……` 不能被判成两个句号（420×2 = 840ms 的沉默）
  - **205** 切分**复用** `domain/text.py::split_long_sentence`（口径只此一处）：写稿说合格、配音这边不该报超长。数字不会被切一半 —— 因为**归一化之后根本没有 ASCII 数字**（`3.14` ⇒ `三点一四`），这条由 `test_normalize_removes_ascii_digits_so_numbers_cannot_be_split` 守住
- ⚠️ **R5** `pynini` 不可用 ⇒ 自研 `text_normalize`，**不 import `tn`**（本模块零第三方依赖，只吃 `yaml` / `pydantic`）
- ⚠️ **R7** 长文本漂移/吞字 ⇒ 单句字数上限 + 强制切分
- 📌 **`tts_cache_key`（§04.3.6 同节）留给 T2.6**：它是句级缓存的一部分，与本任务的"文本变换"是两件事
- 📌 **人物（口吻 / 受众 / 禁区）与本模块解耦**：人物管"说什么"，词表管"怎么念" ⇒ 改人物不需要动这里

### T2.6 按句合成流水线 + 句级缓存 · **P0** ✅ **已完成（2026-09-16）**
- 依赖：T2.3–T2.5, T1.5 ｜ 里程碑：M2 ｜ 契约：§03.3.7
- [x] sentence 单元 job（**续传粒度 = 句子**）
- [x] `tts_hash` 缓存（内容哈希决定产物路径 ⇒ 命中即跳过）
- [x] 单句重试 / 降级（`tts_attempts ≥ 3` ⇒ `skipped`，等长静音 + 字幕保留）
- [x] `version` 竞态保护（合成中稿件被编辑 ⇒ 丢弃结果）
- [x] 产物落 `data/media/<date>/<task_id>/tts/s001.wav` ⇒ 实落 **`data/output/voice/<task_id>/s001.wav`**（`StudioPaths.sentence_wav`，3 位补零保证字典序；`data/media/<date>/` 是二期分层，见裁定 215）
- ✅ `pytest tests/integration/test_sentence_resume.py -q` ⇒ **5 passed**：①杀进程重启后已完成句**引擎调用次数为 0**（Mock 计数断言）②第 3 句注入失败 ⇒ 仅该句重试 ③编辑某句 ⇒ 仅该句失效重合成 ④缓存命中率 **33%**（线是 ≥30%）
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/integration/test_sentence_resume.py -q` ⇒ **5 passed in 4.8s**（真 `PoolWorker`：真入队 / 真认领 / 真续租 / 真收尾 + 真 SQLite + 真 ffprobe 实测时长；只把**引擎**换成数得清调用次数的假件）
  - `.venv\Scripts\python.exe -m pytest tests/unit/tts tests/unit/db/test_sentence_repo.py tests/unit/pools/test_voice_worker.py -q` ⇒ **246 passed**（`test_cache` 16 + `test_sentence` 17 + `test_sentence_repo` 27 + `test_voice_worker` 21，外加 T2.5 的 165）
  - `.\tasks.ps1 check` ⇒ **2985 passed / 32 skipped / 12 deselected**；`ruff` + `mypy`（289 文件）全绿
- **交付物**：`src/studio/tts/{cache,sentence}.py`；`src/studio/db/repositories/sentence_repo.py`；`src/studio/pools/voice_worker.py`；`workers/run_voice.py`（注册真实 handler）；`tests/unit/tts/{test_cache,test_sentence}.py`、`tests/unit/db/test_sentence_repo.py`、`tests/unit/pools/test_voice_worker.py`、`tests/integration/test_sentence_resume.py`
- **施工裁定（本轮新增 206–216）**：
  - **206** 缓存键吃**归一化后**的文本，不吃原文：合成读的就是归一化后的文本（T2.5），拿原文做键会得到"键变了、念出来的字没变"的**假 miss**（改个 emoji 就重念一遍），以及反过来的**假命中**（词表热改后该念法变了、键却没变 ⇒ 复用了旧读音）。**键与合成必须吃同一份字符串**
  - **207** 键字段之间用 `\x1f`（Unit Separator）而不是 `|`：口播文本里出现竖线完全可能（"3|5 的比例"），分隔符与内容撞车会让两组不同字段拼出同一个字符串 ⇒ **假命中**（复用了别人的台词）。控制字符进不了归一化后的文本（T2.5 步骤 1 专门删这类字符），所以它不会与内容撞车
  - **208** 键里的 `speed` 用 `%g` 格式化：`1` 与 `1.0` 必须是同一个键 —— 否则"调用方怎么写这个数"会决定缓存命不命中，而这种"看心情"的失效最难查。`seed=None` 编成 `"none"` 而不是 `0`：前者是"引擎自己挑"，后者是"就用 0"
  - **209** 缓存元数据走**旁挂 `.json`**，不写进 DB：缓存是**可丢弃**的东西（删掉只损失速度、不损失正确性），而 DB 里每一行都要有迁移 / 备份 / GC 的账。元数据读坏写坏一律当"hits=0 的新条目"处理 —— 最坏结果只是这条早点被淘汰
  - **210** 淘汰**不是纯 LRU**：`hits ≥ 2` 的条目**降权**（最后才碰）。用过两次以上的正是那些高频套话（开场白 / CTA），淘汰它们等于淘汰最值钱的那部分；只被用过一次的大多是"这一版稿子里的独有句子"，下次改稿就再也用不上。顺序 = "先 `hits < 2`、组内先旧后新"
  - **211** `version` 是竞态**唯一**闸门，而它守的是"文本有没有被改过"：合成期间用户改了稿 ⇒ `finish` / `fail` 命中 0 行 ⇒ **丢弃这次结果**（不是失败：它没失败，只是过期了）。过期结果**不记账** —— 否则改一句就让这一句离"3 次失败降级"近一步，而它一次都没被念过
  - **212** 降级句（`skipped`）**保留** `tts_audio_path` 与 `tts_duration_ms`：产物是一段**等长静音**，时间轴（T2.7）照样能算、字幕照样在 —— 只是这一句没声音。面板据此说得出"这条片子有 2 句没念出来"
  - **213** 占位静音的时长**估算进、实测出**：`estimate_duration_ms` 只用来告诉 ffmpeg 生成多长，写库的是 `ffprobe` **实测**值（§03.3.7：`tts_duration_ms` 是时间轴唯一来源，估算值进去就是"字幕整体偏移"）
  - **214** 音色在**装配期**解析一次（`build_voice_handler`）：列音色要起一个 PowerShell（1–2 秒），放进单元里就是"每念一句先卡两秒"，而音色是进程生命周期内不变的环境事实。解析不到 ⇒ **启动就报** `TTS_ENGINE_UNAVAILABLE`，而不是"起来了，然后每句都失败"
  - **215** `done` 是**可撤销**的（`SentenceRepo.reopen`）：产物（句子 WAV）与缓存副本两处都被清理之后，"已完成"这个结论不再成立 —— 此时"相信库里的 done"只会交出一个指向空气的 `tts_audio_path`。处理器把状态**如实**退回 `pending` 重念，而**不动 `version`**：那道闸门守的是文本，这里一个字没改（跟着 +1 会把在飞的合成结果一并作废，那是编辑该做的事，不是清理该做的事）。落地时这条真的踩到了：见陷阱 #104
  - **216** T2.6 **自带一个最小 `SentenceEngine` Protocol**，不等 T2.3 的 `VoiceEngine` 路由：T2.3 依赖 T2.2 的 GPU 常驻服务（E5 不可用），而验收（"引擎调用次数为 0"）**必须有账可查**。T2.3 落地时换实现即可，调用方一行不动 —— 与 T3.x 的 ffmpeg 路径同一条取舍：**能出片 > 音质完美**
- ⚠️ **P2 按句可续传的核心验收点**：引擎调用为 0 是硬断言
- ⚠️ 陷阱 #26 单句编辑后其余句音频复用错位 ⇒ 编辑 ⇒ 时间轴**全量重算**
- ⚠️ 新增陷阱 5 条已并入 §05.7（编号 103–107）
- 📌 **`render/final` 的入队不在这里**（§03.4.5：全部句 `done/skipped` + `timeline.json` 落盘 ⇒ 入队渲染）⇒ 归 T2.8。T2.6 只负责"把这一句念出来"
- 📌 **`SENTENCE_COLUMNS` 提升为公开**：`SentenceRepo` 与 `ScriptRepo` 读同一张表，列清单只能有一份 —— 写两遍必然在加列时漏一处

### T2.7 时长时间轴 · **P0** ✅ **已完成（2026-09-16）**
- 依赖：T2.6 ｜ 里程碑：M2 ｜ 契约：§04.2.7
- [x] `ffprobe` 逐句**实测**时长（**不信任引擎返回值**）
- [x] 句间停顿 = `base + jitter(seed)`（±80ms，种子派生可复现）
- [x] 累加得 `start_ms`/`end_ms`，末尾追加 `tail_ms`
- [x] 写 `timeline.json` + `artifacts(kind='timeline')`（顺手把 `voice_master` 也登记了：两样都是这条片子的产物）
- [x] 批量回写 `script_sentences.start_ms/end_ms`（事务内）
- [x] **任一句重合成 ⇒ 时间轴全量重算**（**禁增量拼接**）
- ✅ `pytest tests/integration/test_timeline.py -q`（单调不重叠、`total_ms = Σ句实测 + Σ停顿 + tail`）；`voice_master.wav` 与 `total_ms − tail_ms` 的偏差 **≤30ms**（母带**不含** tail，见裁定 217）
- ✅ **验收 ①**：母带与时间轴对得上 —— 时间轴自己单调不重叠、三部分之和成立；`ffprobe(voice_master)` 与 `total_ms − tail_ms` 偏差在 30ms 内；母带是 48k 单声道 PCM；**停顿真的进了音频**（母带比"Σ句时长"长出来的那一截正是停顿之和）
- ✅ **验收 ②**：改一句 ⇒ **全量重算** —— 第 1 句窗口一个字不动、抖动不变，第 3 句的 `start_ms` 跟着挪**正好等于**第 2 句变长的那一截
- ✅ **验收 ③**：未定局 / 产物缺失 ⇒ 报错停下（不拼半条母带），错误里说得出**是哪一句**
- ✅ **验收 ④**：降级句（`skipped` 的等长静音）照样进时间轴
- ✅ **验收 ⑤**：重跑一次得到**逐毫秒一致**的时间轴（含"任务没给种子"的兜底）
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/integration/test_timeline.py -q` ⇒ **7 passed in 6.0s**（真 `PoolWorker`：真入队 / 真认领 / 真收尾 + 真 SQLite + 真 ffmpeg 拼母带 + 真 ffprobe 实测；只把**引擎**换成写得出固定时长 WAV 的假件 —— 时长必须可控，否则"改一句 ⇒ 后面全体挪"没法钉死）
  - `.venv\Scripts\python.exe -m pytest tests/unit/tts/test_timeline.py tests/unit/db/test_sentence_repo.py tests/unit/db/test_artifact_repo.py -q` ⇒ **50 passed**（`test_timeline` 15 + `test_sentence_repo` 31 + `test_artifact_repo` 5）
  - `.\tasks.ps1 check` ⇒ **3016 passed / 32 skipped / 12 deselected**；`ruff` + `mypy` 全绿
- **交付物**：`src/studio/tts/timeline.py`（纯计算 + 滤镜图 + JSON 落盘）；`src/studio/services/voice_service.py`（`settle_voice`：收口顺序 / 回写 / 产物登记）；`src/studio/db/repositories/artifact_repo.py`（`artifacts` 的写与读）；`SentenceRepo.list_for_task` + `set_timeline`；`core/paths.py::data_relative`；`tests/unit/tts/test_timeline.py`、`tests/unit/db/test_artifact_repo.py`、`tests/integration/test_timeline.py`
- **施工裁定（本轮新增 217–223）**：
  - **217** `total_ms` 含 `tail_ms`，**母带不含**：`total_ms` 是**成片**时长基准（§04.2.7 / §04.2.4.2：场景时长 = Σ句实测 + Σ停顿 + tail），而 `voice_master.wav` 只装"句子 + 句间停顿"—— 尾部那一段由合成那一步用 `-t = ffprobe(voice_master) + tail_ms` 补（§04.2.8.5）。**两处各加一次** ⇒ 成片比预期长出一个 `tail_ms`。所以验收口径写的是"`ffprobe(master)` vs `total_ms − tail_ms` ≤30ms"；把 tail 也塞进母带能让"母带 vs `total_ms`"那条断言成立，但那是**改错了地方**（见陷阱 #110）
  - **218** 停顿必须**真的进音频**（`apad=pad_dur`），不能只记在时间轴上：字幕与画面都按时间轴对齐，观众听到的却是母带 —— 母带里没有那段静音，第 N 句就会比字幕早到 `pause_after_ms`。这条**不会让任何东西报错**，只会让口型与字幕"看起来有点怪"。验收里那条"母带比 Σ句时长长出来的正是停顿之和"就是钉它的
  - **219** 母带与时间轴对账**超线只 `warn`**（`MASTER_DRIFT_TOLERANCE_MS = 30`）：逐句 `ffprobe` 各自四舍五入到毫秒，N 句最坏叠 0.5×N 毫秒（60 句 = 30ms）—— 为一个四舍五入的账把整条片子拦下来，与 C12"诊断不阻断"同一条口径。**真正该拦的**（句子音频缺失 / 长度为 0）在别处拦，那里是硬错误
  - **220** 时间轴回写**整批要么全成、要么整条作废**（`set_timeline` 返回**真正写进去的行数**）：跳过被改过的那一行、其余照写，会留下一份**自相矛盾**的时间轴 —— 第 3 句是新时长，第 4 句起的 `start_ms` 还是按旧时长算出来的（陷阱 #26）。调用方拿"写进去的行数 < 传进去的条数"判"这一版作废"，重跑即全量重算
  - **221** 基础停顿为 0 时**不许抖**：抖动只负责让"**设了的**停顿"听起来不像节拍器。写成 `max(0, base + offset)` 在 `base = 0` 时会退化成"只加不减"（0…80ms）—— 凭空造出静音，而用户要的是"紧接下一句"。0 要单独短路（`test_a_zero_base_pause_never_invents_silence` 钉住）
  - **222** `has_audio`（**非空**才算数）**只定义一次**（`tts/timeline.py`），池 worker 的短路 / 逐句实测 / 收口挑路径三处共用：三处各写一遍，早晚会有一处退化成"存在就算"—— 而那会让整段片子交给一份半截音频（陷阱 #105 的同一条）
  - **223** `timeline.json` 里的 `voice_master` 与 `audio` 写**相对 `data/`** 的 posix 路径（`core.paths.data_relative`）：清单要能跟着 `data/` 搬家；绝对路径会在换机器 / 换盘符 / 从备份还原之后指着一堆不存在的文件。落在 `data/` 之外（调用方给错了）退回绝对路径而**不抛** —— 收尾阶段为一个路径格式把片子拦下来不值当
- ⚠️ **C12 修订**：`av_sync_audit` 降为**可选诊断**，不阻断发布
- ⚠️ 陷阱 #26 增量拼接 ⇒ 全量重算（本轮由"改一句 ⇒ 后面每句的 `start_ms` 都跟着挪"这条验收钉住）
- ⚠️ 新增陷阱 3 条已并入 §05.7（编号 108–110）
- 📌 **交接给 T2.8**：`render_service.reuse_or_synthesize_voice`（T1.12 的一次性配音）仍会往**同一个** `timeline.json` 写一份**旧形状**的内容（无 `seed`/`tail_ms`、`pause_after_ms` 恒 0、绝对路径）⇒ 现在有两个写入者（陷阱 #108）。T2.8 把编排改到"池 + `settle_voice`"之后，写入者只剩一个；在那之前别按字段名硬读 `seed` / `tail_ms`
- 📌 **`settle_voice` 不改任务状态**：`voicing → queued_render` 归 T2.8 的编排（§03.4.5）。把迁移塞进来，"只想重算一遍时间轴"就会变成一个会改任务状态的副作用

### T2.8 配音阶段编排与降级演练 · **P0** ✅ **已完成（2026-09-16）**
- 依赖：T2.7, T1.11 ｜ 里程碑：M2 ｜ 契约：§04.3.4
- [x] voice stage 接入 orchestrator
- [x] 守卫：`voicing → queued_render` 仅在**全部句 `done/skipped`** + timeline 校验通过后放行
- [x] 故障注入开关 `STUDIO_FAULT`（供演练与测试）
- [x] 降级演练：TTS 全挂 ⇒ **字幕模式**仍推进（不卡死）
- [x] `pipeline.streaming_render` 默认 **false**（C7 裁决：配音全部完成才渲染）
- ✅ `studio pipeline run <task_id> --until voicing` 跑通；`STUDIO_FAULT=tts_fail_sentence=3` ⇒ 单句重试成功；`STUDIO_FAULT=tts_down=1` ⇒ 字幕模式仍推进到 `queued_render`
- ⚠️ **C7**：边配音边渲染会让时长基准漂移 ⇒ 默认关闭
- ⚠️ 原文 §3.3"渲染可逐句消费" ⇒ 保留为可选开关，非默认
- **真机演练（2026-09-16，真库 / 真 SAPI / 真 ffmpeg，4 句 × `pause_after_ms=250`）**：
  - `--until voicing` ⇒ 停在 `voicing`：4 句全 `pending`、作业全 `pending`、**母带与 `timeline.json` 都不在盘上** —— "投递完就停"这句话是真的（裁定 224）
  - `STUDIO_FAULT="tts_fail_sentence=3;tts_fail_times=2"` + `--until queued_render` ⇒ 第 3 句 `tts_attempts=2`（作业 `attempts=3`）、其余 **0**、4 句全 `done`、母带 17013ms、**无降级**（不是"一失败就降级"）
  - `STUDIO_FAULT="tts_down=1"` + `--until queued_render` ⇒ 4 句全 `skipped`（各挂满 3 次）+ 等长静音 + 母带 16077ms；`quality_json.degrade_reason = "tts_unavailable"`；任务**照常推进**（字幕模式，不卡死）
  - 续跑同一条任务 `--until completed` ⇒ `voicing → queued_render → rendering → completed`，成片 `data/output/videos/20260916-130927_01M2M9THZ1M83CT5EJH789ZVZA_final.mp4`（1080×1920 h264 + aac 48k，17.47s，11.8MB）
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/integration/test_voice_stage.py -q` ⇒ **7 passed in 8.1s**（真 `PoolWorker` 真认领 / 真退避 / 真收尾 + 真 SQLite + 真 ffmpeg 拼母带 + 真编排；只把**引擎**换成写得出固定时长 WAV 的假件）
  - `.venv\Scripts\python.exe -m pytest tests/unit/core/test_faults.py tests/unit/tts/test_faults.py tests/unit/services/test_pipeline_service.py -q` ⇒ **56 passed**（26 + 14 + 16）
  - `.\tasks.ps1 check` ⇒ **3071 passed / 32 skipped / 12 deselected**（121s）；`ruff format` + `ruff check` + Web 契约 + `mypy`（300 files）全绿
- **交付物**：`src/studio/core/faults.py`（`STUDIO_FAULT` 的解析与词表）；`src/studio/tts/faults.py`（引擎缝上的故障注入）；`src/studio/services/pipeline_service.py`（`run_task` 的 `QUEUED_VOICE` / `VOICING` 两个分支 + `_drain_voice_pool` + `_note_voice_degrade` / `_final_quality`）；`services/voice_service.py::enqueue_sentences`；`services/render_service.py`（`write_timeline` 收口到 §04.2.7 的形状，陷阱 #108 结案）；`core/config.py::PipelineConfig`；`config/app.yaml → pipeline.streaming_render`；`tests/integration/test_voice_stage.py`、`tests/unit/core/test_faults.py`、`tests/unit/tts/test_faults.py`
- **施工裁定（本轮新增 224–233）**：
  - **224** 配音阶段拆成 **投递 / 排空** 两步（`queued_voice --①投递--> voicing --②排空+收口--> queued_render`）：`voicing` 因此是**真的停得住的落点**（作业派出去了、等池干完），而 `rendering` 是一口气跑完的瞬时态 —— 后者收进 `SUPPORTED_UNTIL` 只会得到"有时停得住、有时停不住"
  - **225** 排空**就地借一条 worker**（`worker_id = voice#1@<pid>`，与常驻池不撞名）：`studio pipeline run` 是一条一次性命令，不该要求"你先把池起起来"；就地跑与常驻池不冲突 —— 认领是单语句原子操作，谁抢到谁干
  - **226** 排空的收工判据是 `SentenceProgress.is_settled`（**唯一**），不能拿 `worker.run()` 的返回当"干完了"：退避中的 `claim` 返回 `None`，和"池空了"长得一模一样（陷阱 #107 的同一条）
  - **227** 守卫写在 `settle_voice` **里面**（不是全部 `done/skipped` 就抛），编排层不重复判据：两处判据迟早会不一致，而 `voicing → queued_render` 这条边**只有在时间轴真的算出来了**之后才可能被跨过
  - **228** **降级是成功**（§04.3.3）：`tts_attempts >= 3` ⇒ `skipped` + 等长静音 + 字幕保留，任务**不失败**、继续往下走。把降级写成"抛出去"⇒ 产线卡死在 `voicing`，而单看某一句它只是"失败了一次"，看不出整体已经不动了
  - **229** 配音的降级结论**只在全句都降级时**写进 `quality_json`（`tts_unavailable` = "这条片子进了字幕模式"）；部分降级留在逐句状态里 —— `degrade_reason` 只有一个槽位，塞进去会把更重的那句话挤掉。出片那一步的 `_final_quality` **不带掉**配音的结论；两边都降级时以**出片**的原因为准
  - **230** 兜底 `timeline.json` **不覆盖**已有那份：只在 `cues` 非空且文件不存在时才写。覆盖的后果是"停顿 350ms 被改回 0ms、字幕整体错位"，而且**不报错**（陷阱 #108 的收口方式）
  - **231** 故障引擎的名字必须**每场演练都不同**（进程号 + 随机段），**固定后缀不够**：第二场演练会命中第一场写下的缓存 ⇒ 引擎一次都没被调到，而 `voice.fault_injected` 照打（真机踩到，见陷阱 #111）
  - **232** 排空空转时必须**等一拍**（池的 `poll_ms`，下限 `DRAIN_POLL_FLOOR_MS = 50`）：`max_empty_rounds=1` 的 `run()` 在空池时立刻返回，外层不等就是忙等 —— 退避最长 20 秒，那是几万次认领与几万对 `worker.started/stopped` 日志（真机踩到，见陷阱 #112）
  - **233** 排空认领**不按任务过滤**：它借的是 voice 池的一条 worker，池里有什么活就干什么活 —— 与常驻池语义一致（谁抢到谁干）。代价是"顺手把另一条卡在 `voicing` 的任务也念了"；真要按任务隔离得给 `JobStore.claim` 加任务过滤，那是四个池共用的契约，而这里没有谁吃亏：另一条任务的句子念完了，它的下一次 `pipeline run` 照样从 `settle_voice` 接着走（陷阱 #113）
- ⚠️ 新增陷阱 4 条已并入 §10（编号 111–114）
- 📌 **陷阱 #108 结案**：`render_service.write_timeline` 改成与配音阶段**同一个形状**（走 `tts.timeline.build_timeline` + `write_timeline_json`，`total_ms` ⇒ `tail_ms`），并且**不覆盖**配音阶段那份更准的 ⇒ `timeline.json` 的写入者从两个变回一个

### T2.9 配音服务化操作接口 · **P1** ✅ **已完成（2026-09-16）**
- 依赖：T2.6, T1.7 ｜ 里程碑：M2 ｜ 契约：§04.3.7 / T4.5
- [x] `POST /api/v1/sentences/{id}/resynth` 单句重配
- [x] `GET /api/v1/media/{path}` 单句试听（HTTP Range）
- [x] `PATCH /api/v1/tasks/{id}/voice_map` 任务级换音色（受影响句全部失效）
- [x] 三者均写 `audit_ops`
- [x] 另外两个面板首屏要用的读端点：`GET /api/v1/sentences?task_id=`（逐句状态 + 可播 url）、`GET /api/v1/voices`（音色下拉框）
- ✅ `POST /sentences/{id}/resynth` ⇒ 该句 `done`→`pending`→重合成 ⇒ **时间轴重算**（总时长变化可见）；单句 wav 可播放；换音色 ⇒ 受影响句全部失效
- ⚠️ 试听与合成抢 GPU ⇒ 试听**走缓存文件，不触发新合成**
- ⚠️ 换音色触发全量重配 ⇒ 明确提示"将重配 N 句"并二次确认
- **真机演练（2026-09-16 · 真 SAPI `Microsoft Huihui Desktop` / 真 ffmpeg / 真 REST / 隔离家目录，3 句 × `pause_after_ms=250`）**：
  - `GET /sentences` ⇒ 3 句 `done`（**3978 / 4823 / 4148 ms** · 真 SAPI 实测）、`total_ms=14352`、`stale=false`、每句带 `audio_url`
  - `GET /media/voice/<task>/s001.wav` ⇒ **200**（175482 字节 · `audio/wav`）；`Range: bytes=0-31` ⇒ **206** `content-range=bytes 0-31/175482`
  - `POST /sentences/<id>/resynth` ⇒ 200 `status=pending`、`job_created=false`（复用原作业，**没另开一条**）、`timeline_stale=true`；库里那一句 `pending`、作业 `pending/attempts=0`；真 SAPI 重念 1 个单元 ⇒ 3 句重新全 `done`
  - 重算时间轴 ⇒ `total_ms` 与重念前**逐毫秒一致**（SAPI 对同一句是确定性的）⇒ **"时长变了"不能当"重算过了"的证据**；客观证据是 `timeline.json` 与 `voice_master.wav` 的 mtime 都变了（陷阱 #121）
  - `PATCH /tasks/<id>/voice_map` 不带 confirm ⇒ **409** `VOICE_MAP_CONFIRM_REQUIRED`（`affected=3 / total=3 / sentences=[1,2,3]`，**一个字节都没写**）；带 `confirm=true` ⇒ 200、`affected=3`、`requeued=3`、`created_jobs=0`、`busy=[]`，3 句全 `pending` + 3 条作业带着**新音色**的 payload；`littlebear` 那条映射**原样保留**（PATCH 是增量）
  - 换一个本机没有的音色 ⇒ **422** `TTS_VOICE_MISSING`（`context.available` 列出本机 3 个真音色）
  - `GET /api/v1/audit?task_id=` ⇒ `['task.voice_map', 'sentence.resynth']`（两条留痕都在）
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/integration/test_voice_api.py -q` ⇒ **15 passed in 4.0s**（真 `TestClient` + 真库 + 真盘 + 真 ffmpeg 重算时间轴；只把**音色清单**换成固定两个名字 —— 真列一次要起 PowerShell）
  - `.venv\Scripts\python.exe -m pytest tests/unit/services/test_voice_service.py -q` ⇒ **13 passed in 1.0s**
  - `.\tasks.ps1 check` ⇒ **3099 passed / 32 skipped / 12 deselected**（137s）；`ruff format` + `ruff check` + Web 契约 + `mypy`（304 source files）全绿
- **交付物**：`services/voice_service.py`（`resolve_voice` / `usable_voices` / `voice_payloads` / `preview_audio` / `resynth_sentence` / `set_voice_map` / `read_timeline_total_ms` + 4 个报告 dataclass）；`app/routers/voice.py`（5 个端点）+ `app/schemas/voice.py` + `app/main.py` 注册；`db/repositories/sentence_repo.py`（`get_by_seq` / `invalidate`）；`db/queue.py`（`requeue_unit`）；`domain/task_service.py`（`set_voice_map`）；`core/errors.py`（`TTS_VOICE_MISSING` / `VOICE_MAP_CONFIRM_REQUIRED`）+ `app/errors.py` 映射（422 / 409）；`tests/integration/test_voice_api.py`、`tests/unit/services/test_voice_service.py`；`web/openapi.json` + `web/src/api/types.gen.ts` 重生成
- **施工裁定（本轮新增 234–243）**：
  - **234** 试听走 `GET /api/v1/media/{path}`，路径**只从规范路径 / 缓存键拼出来**，请求里那个 `{path}` 只用来**找到句子**：正则整串匹配 `voice/<task_id>/s00N.wav` ⇒ `../` 与绝对路径在路由层就是 422（陷阱 #118）
  - **235** 试听**绝不触发合成**：三处候选（规范路径 / 库里的 `tts_audio_path` / TTS 缓存）都只看"盘上有没有"，一处都没有 ⇒ 404 `PATH_MISSING`。点一下试听就排一次合成，等于把"看一眼"变成"重跑一遍"，而重跑还会覆盖交付产物
  - **236** 重配 = 业务表 + 作业表**一起**改（`SentenceRepo.invalidate` + `JobStore.requeue_unit`）。只改前者：REST 200、库里显示 `pending`，而幂等键让那条作业**一辈子只有一条**、`succeeded` 之后没人再看它 ⇒ 任务卡在 `voicing` 且不报错（陷阱 #115）
  - **237** `invalidate` **必须**把 `tts_attempts` 归零：不归零的话，上一轮已经撞过 3 次的降级句重配时**再失败一次**就直接降级 —— 用户点"重配"的本意是"再给一次机会"，结果只给了一次（陷阱 #116）
  - **238** 换音色 = **三处**一起改：`tasks.payload_json.voice_map`（`TaskService.set_voice_map`）+ `script_sentences.tts_status` + `jobs.payload_json`（`requeue_unit(payload={"voice": ...})`）。少第三处 ⇒ 重配出来还是旧嗓子，而库里显示新音色（陷阱 #117）
  - **239** `PATCH /voice_map` 是**增量**（只改给出的角色，其余原样保留）。整体替换看起来更"干净"，但它的失败方式是静默的：面板只提交被改的那个角色 ⇒ 另一个角色的映射被悄悄抹掉（陷阱 #120）
  - **240** 音色合法性在**写入之前**校验（`TTS_VOICE_MISSING` 422 + `context.available`）：写进去之后配音会连失败 3 次 ⇒ 全句降级成静音 ⇒ 用户看到"换音色成功"、拿到的却是一支**没人声**的成片。而 `voice_map` 里**存量**的、本机没装的音色（默认值就是逻辑角色名）**不报错**：那是"库里早就存着的默认值"，只在 `resolve_voice` 里退回进程音色并标 `fallback` —— 两者面对的是不同的问题
  - **241** 重配 / 换音色**不重算时间轴**：任务在 `completed` 时没有 `completed → voicing` 这条边（§03.4.5），为了一个端点顺手重算去改松状态机不值得。下一轮收口（`settle_voice`）全量重算（陷阱 #26），响应里用 `timeline_stale` + `hint` 说清"成片时长以那一版为准"
  - **242** 换音色**先算代价再抛** 409（`context` 带 `affected/total/sentences`）：一次点击要重配几十句，这个代价得人点头。`synthesizing` 的句子**不动**但进 `busy`（静默跳过就是"换音色成功、成片还是旧嗓子"）
  - **243** `audio_url` 由**服务端**拼，没有音频时给 `null`：让面板自己拼 url 等于把"文件落在哪"抄进前端，目录一改就是"点了播放没反应"；给一个注定 404 的 url 同样如此
- ⚠️ 新增陷阱 7 条已并入 §10（编号 115–121）


> **>>> M2 门禁**：一句话用熊大音色读出；杀进程重启后已完成句**引擎调用为 0**；网页可见逐句进度。

---

## 3. 阶段 T3 · 渲染引擎（一期：单遍合成 + 固定水印 · 7 任务 → 门禁 M3）

> **v3.2 范围**：一期 = **音画合成 + 固定水印**，一次 ffmpeg 调用直出 `final.mp4`。
> **不做**句子↔镜头对齐（D2），**不做**场景中间产物与三层模板编排（C13 ⇒ 二期 P1）。

### T3.1 素材入库（跑酷 + BGM）· **P0** 🔴 需 E1/E3 ｜ 🔶 **部分完成（2026-09-17 核对）**
- 依赖：T1.3 ｜ 里程碑：M3 ｜ 契约：§03.3.14 / §04.2.4
- [x] 跑酷素材入库：通配 `parkour_*.mp4`、缩略图、可用区间（`usable_from_ms/to_ms`）、`has_text` 标记；**指纹只做了 sha256**（pHash / 帧哈希见下条）
- [x] **BGM 入库**（Q12）：`bgm_tracks` 表、`loudness_lufs`（ffmpeg 实测）、`mood`、`loopable` 标记
- [x] **授权登记**：`license` 枚举强制（`self_recorded`/`authorized`/`cc0`/`purchased`）+ `proof_path`；**缺 license 直接拒绝入库**（`services/asset_service.py` 的 `LICENSES`，非法 ⇒ `ASSET_INVALID`）
- [x] 重复素材按 **sha256** 拒绝（`IngestAction.DUPLICATE`）
- [ ] 重复素材按 **pHash** 拒绝 + **帧哈希** —— **用户已裁定为非核心（2026-09-17），一期不做**。列 `broll_clips.phash` / `frame_hashes_json`（`db/models.py:562-563`）与 DDL 都在，但**全仓没有任何代码写它们**
- [ ] 黑帧段落自动排除 —— **用户已裁定为非核心（2026-09-17），一期不做**
- [ ] `studio assets ingest --kind bgm --dir <源>` / `--kind parkour` —— **未做**：`cli.py` 里**没有 `assets_app`**；入库主干走 T4.8 的 REST 面板（`studio assets stats` 同理，该 CLI 也不存在）
- ✅ `studio assets stats` 显示 clips 数/总时长/bgm 数；`pytest tests/integration/test_broll_ingest.py -q`（重复拒绝、黑帧排除、可用区间正确）；**缺 `license` 直接拒绝**
- 📌 **实测（2026-09-17 核对）**：`tests/unit/services/test_asset_service.py` ⇒ **40 passed**（含 `test_duplicate_content_is_not_ingested_twice` / `test_new_asset_without_a_license_stays_out` / `test_illegal_license_is_refused_before_any_write`）；`tests/unit/render/test_assets.py` ⇒ **13 passed**；`tests/integration/test_broll_ingest.py` **不存在** ⇒ 上面那条集成验收**未落地**
- ⚠️ **R3 版权** ⇒ 只收自录/授权 + 强制留档
- ⚠️ 🔴 **E1 素材未到位** ⇒ 走**黑屏降级**（§04.2.8.6）保证链路不断
- ⚠️ 陷阱 #14 素材被判搬运 ⇒ 随机化两档 + 相似度审计

### T3.2 水印资产与合成 profile · **P0** 🔴 需 E2 ✅ **已完成（核对确认 2026-09-17）**
- 依赖：T1.2, T1.3 ｜ 里程碑：M3 ｜ 契约：§04.2.8.2
- [x] `watermark.png` 入库与校验（尺寸/透明通道/路径存在）
- [x] 水印参数：位置枚举 / 边距（**偶数**）/ 宽度（≤ 画布 1/4）/ 透明度
- [x] `config/outputs.yaml` 合成 profile：**1080×1920 / 30fps / libx264 CRF21 / faststart / bt709**（`douyin_1080x1920_30fps_v1`；另带 `xhs_1080x1440_30fps_v1` / `bili_1920x1080_30fps_v1`）
- [x] **720P 保底档**（`fallback_720x1280_v1`，渲染失败降级用）
- [x] `studio render profile --show`（`cli.py:1670`）
- ✅ `studio render profile --show` 打印合成 profile 并说明这次贴不贴水印；`pytest tests/unit/render/test_watermark.py -q`（位置枚举/边距偶数/宽度上限/透明度范围）
- 📌 **实测（2026-09-17 核对）**：`tests/unit/render/test_watermark.py` ⇒ **95 passed**；`tests/unit/render/test_profiles.py` ⇒ **21 passed**。实现落在 `src/studio/render/watermark.py`（`WatermarkSpec` / `WatermarkAsset` / `WatermarkPlacement` / `WatermarkPlan`）与 `src/studio/render/profiles.py`
- ⚠️ **水印是可选装饰** ⇒ 缺失 / 不可用 / 放不下都只**跳过**（原因写进 `manifest.json`），不阻塞出片
- ⚠️ 位置越界 ⇒ **编译期**报错（不要留到 ffmpeg 运行时报）

### T3.3 `CompositePlan` 与单遍编译器 · **P0** 🔶 **部分完成（2026-09-17 核对）**
- 依赖：T3.1, T3.2, T2.7 ｜ 里程碑：M3 ｜ 契约：**§04.2.8.2 / §04.2.8.3 / §04.2.8.5**
- [x] 数据结构 —— **实际名字是 `CompositeRequest`**（`src/studio/render/composite.py:69`）+ `CompositeResult`；`CompositePlan` 这个名字只出现在 `watermark.py:26` 的注释里。**不为对名字去改名**：会牵动 `render_service` / `render_worker` / `hashing` / 一大片测试
- [x] `total_ms = ffprobe(voice_master.wav).duration + tail_ms`（**音频为时长基准**）
- [x] `filter_complex` 生成链：跑酷循环裁长 → 缩放铺满 → 水印 overlay → 混音
- [x] **共用规则**（§04.2.8.4，一期同样适用）：`fps=30` 在 `scale` 前、`setpts=PTS-STARTPTS`、`setsar=1`、显式 `-t`、**禁用 `-shortest`**、`amix normalize=0`
- [x] 路径统一转义（Windows 驱动器冒号转 `\:`）—— 实际函数名 `filter_path_arg()`（`composite.py:137`）
- [ ] **语法预检**：`ffmpeg -filter_complex_script … -f null -`（快速失败）—— **一期未做**（全仓无此实现）。它只是「快速失败」的优化，不影响出片
- [ ] 节点守卫：`estimated_nodes > 60` ⇒ 触发分块降级 —— **刻意不做**：一期是**单底片**合成，整张滤镜图实测十来二十个节点，离 60 差得远；现在写分块就是写一段**永远不会被执行、因而永远不会被验证**的代码，留到二期三层模板。理由写在 `src/studio/render/degrade.py` 的模块注释里；`COMPOSITE_CHUNKED` 常量（`degrade.py:56`）只作占位，**没有代码读它**
- [ ] `studio render plan --task <id> --out plan.json` —— **没有**：`render_app` 只有 `profile` 与 `make` 两个命令
- ✅ `pytest tests/unit/render/test_composite.py -q`：①`total_ms = ffprobe + tail_ms` ②`fps=30` 在 `scale` 前 ③`overlay` x/y 为偶数 ④`amix` 含 `normalize=0` ⑤`estimated_nodes > 60` 触发分块；`pytest tests/golden/test_filtergraph.py -q`（含中文/空格/冒号路径）；语法预检退出码 0
- 📌 **实测（2026-09-17 核对）**：`tests/unit/render/test_composite.py` ⇒ **23 passed**（①–④ 都在）；`tests/unit/render/test_hashing.py` ⇒ **15 passed**；**⑤ 不适用**（分块不做）；`tests/golden/test_filtergraph.py` **不存在** —— 中文/空格/盘符冒号的转义由 `test_composite.py` 的用例直接覆盖（对 `filter_path_arg()` 断言含 `\:`），golden 文件不必再建；语法预检未做 ⇒ 无退出码可验
- ⚠️ **R8 复杂度爆炸** ⇒ ~~节点守卫 + 分块降级~~ **一期不做**（理由见上）
- ⚠️ 陷阱 #6 路径报错 ⇒ 统一 `filter_path_arg()`
- ⚠️ 陷阱 #7 命令行超长 ⇒ ~~`-filter_complex_script` 文件~~ **一期未做**：`build_composite_argv()` 用的是内联 `-filter_complex`（`composite.py:283`）。单底片滤镜图只有十来二十个节点，离命令行长度上限很远；滤镜图仍会落盘到 `graphs/` 供手工重跑（`_record_graph()`）
- ⚠️ 陷阱 #28 素材比人声短 ⇒ `loop` + `trim=duration=total_ms` 补齐；素材为空 ⇒ 纯黑底仍出片

### T3.4 单遍合成执行器 · **P0** 🔶 **部分完成（2026-09-17 核对；`composite_hash` 缓存当日接上）**
- 依赖：T3.3 ｜ 里程碑：M3 ｜ 契约：§01.5.2
- [x] **argv 数组**调用（**不用 shell 拼接**，防注入与转义地狱）—— `build_composite_argv()` 返回 `list[str]`，经 `run_command()` 走 `subprocess.run(argv, …)`
- [ ] `-progress pipe:1 -stats_period 0.5` 进度解析（`out_time_us` → 百分比）—— **一期未做**：argv 里写的是 `-nostats`（`composite.py:281`）。渲染面板的进度是**粗粒度**的（`render 0/1` 那种），不是 ffmpeg 百分比
- [ ] 进度推送限流 2Hz —— **一期未做**（没有 ffmpeg 进度流，也就没有可限流的东西）
- [x] 超时 / 取消 ⇒ **杀进程树**（`taskkill /PID <pid> /T /F`）—— `core/media.py` 的 `run_command()` 从 `subprocess.run(timeout=)` 换成 `Popen` + `_kill_tree()`：Windows 走 `taskkill /PID <pid> /T /F`（§01.5.2 的「取消/超时」约定），POSIX 走 `killpg`（子进程 `start_new_session` 自成一组）；树杀失败时退回只杀直接子进程，**并在报错里如实说明**（`已终止进程` vs `已终止进程树`）。半成品的清理（`.partial` 被删）本来就有
- [x] `.partial` → 原子改名（`partial.replace(req.output)`，`composite.py:358`）
- [x] stderr 尾部截断留痕 + 错误码映射（`CommandResult.tail()`；超时 ⇒ `RENDER_TIMEOUT`，其余 ⇒ `RENDER_FAILED`）
- [x] **`composite_hash` 整片缓存**（同哈希二次运行**不调用 ffmpeg**）—— `src/studio/render/cache.py` + `produce_video` 合成前先比一次指纹：命中 ⇒ 复用盘上那一支（连响度都从上一轮 manifest 里读回来，**一次 ffmpeg 都不起**）。判据是「**这个文件是这批输入渲出来的**」，不是「文件在不在」——后者正是 `pools/render_worker.py` 纪律 2 否掉的短路；manifest 只在成片原子改名 + 量完响度**之后**才写，所以「manifest 在」本身就等于「那一轮跑到底了」。命中时 manifest 记 `reused=true` + `rendered_at`（比 `created_at` 早），`ProduceResult.reused` 与进度文案一起告诉面板；`--force-render` 可强制重渲（`ProduceRequest.force_render`）
- [ ] Windows 用 `BELOW_NORMAL_PRIORITY_CLASS` 启动 —— **未做**：`core/media.py:126` 用的是 `CREATE_NO_WINDOW`（消黑框），没设优先级类
- ✅ `pytest tests/integration/test_composite_runner.py -q`：①进度可解析 ②超时杀进程树且不留子进程 ③中断 ⇒ 目标文件不存在但 `.partial` 被清理 ④argv 不含 shell 拼接（静态断言）⑤同 `composite_hash` 二次运行**不调用 ffmpeg**（Mock 计数）⑥人声或素材改动 ⇒ 哈希变化 ⇒ 重渲
- 📌 **`tests/integration/test_composite_runner.py` 不存在**；上面那条集成验收**未落地**（它的 ⑤「同哈希二次运行不调用 ffmpeg」已由 `tests/unit/services/test_render_cache.py` 以 Mock 计数覆盖；②「超时杀进程树且不留子进程」已由 `tests/unit/core/test_media.py::test_a_killed_tree_leaves_no_grandchild` 用真两级进程树覆盖）。其余已落地部分由 `tests/unit/render/test_degrade.py`（**8 passed**）与 `tests/unit/render/test_composite.py`（**23 passed**，含 argv 静态断言）覆盖
- ⚠️ 陷阱 #9 崩溃留「假完成」⇒ `.partial` + 原子改名
- ⚠️ **陷阱 #149 超时后调用方永远不返回**（2026-09-17 实测，已修）：`subprocess.run(timeout=)` 超时后只杀直接子进程，**然后（Windows 上）又调了一次不带超时的 `communicate()`** 去读管道 —— 只要有一个继承了我们管道的孙进程还活着，它就永远等不到 EOF。实测：`timeout=2` 的命令 **12 秒后仍挂着**，日志里只有一句超时，渲染 worker 就是这么卡死的。⇒ 超时改走 `_kill_tree()`，收尸那一步自己也带超时（`REAP_TIMEOUT_SEC`），收不干净就关掉我们这一端的管道
- ⚠️ 陷阱 #8 缓存复用旧产物 ⇒ 哈希含 canonical plan + 输入 sha256 + 水印/字幕参数（**已接**：见上条）
- 📌 **命中面如实说**：底片是**随机**挑的（陷阱 #14 反搬运），所以不带 `--seed` 的重跑通常挑到另一条底片 ⇒ 哈希不同 ⇒ **照常重渲**（设计如此，不是缓存失效）。会命中的是带 `seed` 的重跑 / 复现、以及队列把同一条 `render/final` 重投（payload 里带着同一个 seed）
- 📌 **实测（2026-09-17 真机 · 同一条命令连跑）**：`studio render make --task-id t34cache-smoke --text … --seed 7` ⇒ 第一次 **6.8s**、第二次 **1.96s**（省下的正是 ffmpeg 那一段 + 响度测量），manifest 记 `reused=true`、`rendered_at=06:36:48Z` 早于 `created_at=06:36:59Z`、`final` 仍是第一次那一支（**没有**另存一个新时间戳的文件）；`--force-render` ⇒ 5.9s、`reused=false`、**哈希不变**（`916ef67a…`）；换文案 ⇒ 哈希变（`b0d4b163…`）、6.0s、`reused=false`。**注意**：这两次都没带 `--reuse-voice`，人声重新合成后母带 sha256 仍然一致 ⇒ 命中（SAPI 的母语带是确定性的）
- 📌 **单测**：`tests/unit/render/test_cache.py`（**35 passed**：哈希不同 / 产物被清理 / 被截断 / 变长 / manifest 缺字段 / 字段类型不对 / `bool` 冒充整数 / 换家目录，逐条判「不命中」）+ `tests/unit/services/test_render_cache.py`（**8 passed**：第二次不调 `deliver`/`measure_file`、QC 读数读得回来、manifest 记 `reused`、进度文案、人声变了失效、成片被清理/截断失效、`--force-render`）
- ⚠️ NVENC 失败 ⇒ 回退 `libx264`

### T3.5 字幕生成（Q11：**开启**）· **P0** ✅ **已完成（2026-09-15）**
- 依赖：T3.3 ｜ 里程碑：M3 ｜ 契约：§04.2.6
- [x] ASS 生成（**不用 `drawtext`**）：自动换行 / 每行 ≤13 字 / ≤2 行 / 描边 / 居中
- [x] 说话人样式：`SpeakerA`/`SpeakerB` 换强调色（按**首次出现顺序**分配）
- [x] 中文断行：**不在数字/英文单词中间断行**；标点**留在上一行**（不会跑到行首）；无标点长句 ⇒ 容量抬到 `ceil(总字数/max_lines)`，宁可某行多几个字，也不丢台词
- [x] 安全区：`MarginV = max(margin_bottom, safe_area.bottom)`（`subtitle.safe_area` 新增进 `config/outputs.yaml`）
- [x] 字体校验：**优先** `templates/<模板>/assets/fonts/`，退到系统字体目录并记 `note`；两边都没有 ⇒ 抛 `FONT_MISSING` ⇒ `plan_subtitle` 转成**跳过字幕**（不产生豆腐块，也**不阻塞出片**）
- [x] 时间**只**取自 `voice_master` 实测的句级时长（`synthesize_script` 逐句 ffprobe ⇒ `VoiceResult.sentence_durations_ms` ⇒ `timeline.json`）
- [x] 编码 UTF-8 无 BOM + LF（便于 golden 比对）
- [x] 落盘 `data/work/<task_id>/final/subtitle.ass`（永久保留，可二次剪辑复用；`gc/policy.py` 白名单已加 `final`）
- [x] 开关：`outputs.yaml → subtitle.enabled`（默认 **true**）+ `ProduceRequest.subtitle`（**三态**：`None` 听配置）/ CLI `--subtitle/--no-subtitle` / 面板复选框
- ✅ `pytest tests/golden/test_ass.py -q`（golden 比对整份 ASS 文本）；`pytest tests/unit/render/test_subtitle.py -q`：①每行 ≤13 字、≤2 行 ②不在数字/英文单词中间断行 ③`MarginV ≥ safe_area.bottom` ④字体缺失 ⇒ 报错而非豆腐块 ⑤**关掉开关仍能出片**（可选性验证）；`tests/integration/test_render_pipeline.py::test_subtitle_is_burned_and_optional`
- ⚠️ 陷阱 #5 字幕豆腐块 ⇒ `fontsdir` + `resolve_font_dir` 存在性校验 + **找不到就跳过字幕**
- ⚠️ 陷阱 #95 盘符冒号被滤镜参数解析器当选项分隔符 ⇒ 路径整体加单引号 + 冒号转义
- ⚠️ **与原设计的三处出入**（都是真机上被证明行不通才改的，理由见 §04.2.6 的实施状态注）：默认字体改成 `Microsoft YaHei`、字体缺失改为**跳过**而非拒绝出片、落盘改到 `data/work/<task_id>/final/`

### T3.6 混音与响度 · **P0** ✅ **已完成（2026-09-15）**
- 依赖：T3.3 ｜ 里程碑：M3 ｜ 契约：§04.2.8.3 / §01.5.3
- [x] 人声直通 + BGM **侧链 ducking**（threshold 0.05 / ratio 8 / attack 20 / release 420，全部取自 `outputs.yaml → audio`）
- [x] **两遍 `loudnorm`**（第一遍 `-f null -` 只量、`print_format=json`；第二遍 `linear=true` + `measured_*` + `offset`）
- [x] `amix` 显式 `normalize=0`（默认 `normalize=1` 会衰减人声）
- [x] 限幅兜底 `alimiter`：**`level=0`**（自动电平默认开启，会把 loudnorm 的归一化抵消掉）+ `limit = 10**((true_peak_dbtp − 0.3)/20)`（实测 −1.21 dBTP，落在 `≤ -1.0` 门禁内）
- [x] BGM 缺失 ⇒ **单轨人声静音降级**（不报错）
- [x] BGM 循环放在**输入侧** `-stream_loop -1`（规格里的 `aloop=size=2000000000` 是几 GB 的采样缓冲）
- [ ] BGM 预对齐：按 `bgm_tracks.loudness_lufs` 先归一 —— **本轮不做**：两遍 loudnorm 已经把成品响度归到目标，再按素材响度预对齐是**二次补偿**（调两次只会让"为什么响度是这个数"更难查）。要做的话落点是"混音前按库里的 LUFS 调 `bgm_gain_db`"，不是改这条链
- ✅ `pytest tests/integration/test_mixdown.py -q`：①`amix` 含 `normalize=0` ②`loudnorm` 两遍 ③`alimiter` 的 `level=0` 与阈值由 `true_peak_dbtp` 推出 ④**BGM 缺失 ⇒ 单轨人声且不报错**；其中两条标 `slow` 的用例**真跑 ffmpeg 并量**，断言 `lufs ∈ [-16.5,-15.5]`、`true_peak ≤ -1.0`（端到端实测 **−16.48 LUFS / −1.12 dBTP**，GUI 一键出片复核同一支）
- ⚠️ 陷阱 #4 人声偏小 ⇒ `normalize=0` + 两遍 loudnorm
- ⚠️ 陷阱 #96 **滤镜图里一个标签只能被消费一次** ⇒ 人声要先 `asplit=2`（规格原模板直接用了两遍 `[a_voice]`，报的是 `matches no streams`）
- ⚠️ 陷阱 #97 `alimiter` 自动电平 + `limit` 比门禁宽 ⇒ 成品响度/真峰值双超标
- ⚠️ 陷阱 #98 **`sidechaincompress` 接反**（规格下半段模板就是反的）⇒ BGM 整条消失、人声被叠一份，混音真峰值 +1.91 dBTP，成品响度掉到门禁边缘。判据是 `ffmpeg -h filter=sidechaincompress` 的 `#0: main` / `#1: sidechain`
- 📌 **离下界只剩 0.02 dB 不是缺陷**：口播的峰值因数约 19 dB，而 `I=−16` 配 `TP=−1.3` 只允许 14.7 dB，`loudnorm` 为保证不越真峰值门禁主动少抬了 ~0.5 LU。实测去掉限幅器后 `loudnorm` 自己就落在 −16.3 / −1.0 —— 这是素材动态决定的物理上限，再往上顶就得放宽真峰值门禁
- 📌 `scripts/audio_qc.py` **本轮不做**：它要读 `publish.yaml` 的发布门禁并回填 `quality_json`，那属于 T3.7（成片交付与降级链）。当前的响度验证在 `tests/integration/test_mixdown.py` 里真跑真量

### T3.7 成片交付、manifest 与降级链 · **P0** ✅ **已完成（2026-09-16）**
- 依赖：T3.4–T3.6 ｜ 里程碑：M3 ｜ 契约：§04.2.8.6 / §04.2.8.7 / §03.3.11
- [x] `final/final.mp4` 落盘 + `manifest.json`（含 `CompositePlan` + `composite_hash` + 随机化留痕 + 水印标记）
- [x] `quality_json` 回填 —— 口径按 §03.5.3 的 `QualityReport`：`lufs` / `true_peak` / `degraded` / `degrade_reason`；`av_sync_offset_ms` **恒为 `None`**（C12：写 0 会被读成"误差是 0"，那是谎报）
- [x] **降级链**：正常 → **720P 保底** → **黑屏降级**（三轴独立，不是一条线，见 `render/degrade.py` 的表）
- [x] `pipeline.run --until completed` 端到端跑通 —— `studio pipeline run <task_id> --until completed`（`services/pipeline_service.py`）
- [x] 响度 QC `scripts/audio_qc.py` 落地（判据直接读 `config/publish.yaml → precheck`，不另写一份阈值）
- ✅ `pytest tests/integration/test_render_pipeline.py -m "e2e and slow" -q`：**7 例全绿**（含新增的**注入素材为空 ⇒ 黑屏出片**、**注入编码失败 ⇒ 720P 保底出片**两条）；成片实测 **−16.48 LUFS / −1.12 dBTP**，两侧门禁都在内
- ✅ `pytest tests/unit/services/test_pipeline_service.py tests/unit/services/test_render_job_service.py -q`：编排纪律 13 例（幂等 / 无稿件不动状态 / 不代按确认闸 / 卡住状态拒绝 / QC 回填不 bump version / 量不出来留空）
- [x] **`render/final` 接进渲染池队列**（收口 · 2026-09-16）：`pools/render_worker.py` 的单元处理器 + `workers/run_render.py` 注册 + `runner.HANDLER_MODULES` 声明 ⇒ **渲染池从此拉得起来**（不再报 `handler_missing`）；进度写 `jobs.result_json`（`JobStore.report_progress`，带租约守卫）、取消写 `audit_ops`（`JobStore.cancel`）
- ✅ `pytest tests/unit/pools/test_render_worker.py -q`：**14 例**（payload 逐字段收编 / 进度整份覆写 / 失败现场含 `remediation` / 处理器**不自己收尾**）
- ✅ `pytest tests/integration/test_render_pool.py -m "e2e and slow" -q`：**2 例**（真 worker 出片，且进度**对另一条连接可见**；真失败 ⇒ 死信 + 告警）
- ✅ `pytest tests/integration/test_queue_lease.py -q`：**70 例**（新增 `report_progress` / `cancel` 共 9 例）
- 📌 **面板那条出片仍走进程内**（不是漏做）：面板默认的任务号 `ui-YYYYMMDD-HHMMSS` 在 `tasks` 里没有对应行，而 `jobs.task_id` 有外键 ⇒ **挂不上队列**（陷阱 #101）；且队列的幂等键 `(task_id, 'render', 'final', 'final')` 决定"一条任务只渲一次"，与面板"同任务重渲"的用法冲突
- 📌 **分块降级本轮不做**（与"三级降级链"的字面写法有出入，理由写在这里）：分块判据是 `estimated_nodes > 60`，而一期是**单底片**合成，整张滤镜图实测十来二十个节点，离 60 差得远。现在写分块就是写一段**永远不会被执行、因而永远不会被验证**的代码 —— 等二期三层模板（多场景 / 转场 / 多段镜头）节点数真的爆了再写，那时才知道该按什么切
- 📌 **相似度审计 `scripts/dup_audit.py` 本轮不做**：规格 §04.2.4.5 要求成片抽 8 帧 pHash + 关键帧 SSIM + chromaprint 音频指纹，三样都是**新增的重型子系统**，而它在一期是 `warn` 级（§06.4 门禁 3 `block_on_similarity=false`，不阻塞发布）。当前 `QualityReport.dup_audit_pass` 留 `None` —— **`None`（没审）与 `False`（审了没过）是两件事**，留空比填一个假值诚实
- 📌 **渲染反复失败 ⇒ `manual_pool` 本轮不做**：那属于重试链（`attempt_count` / 退避 / 死信 / 池自动降级），T4.x 的地盘。T3.7 的 `deliver()` 只换一次保底档，仍失败就以**原来的错误**抛出去 —— 让重试链接手，而不是在这里再实现一套重试
- ⚠️ 陷阱 #99 **`pipeline run` 对 `failed` / `editing` 的任务静默空转**（判"越过 `--until`"用正向链位置，而它们不在链上 ⇒ 被算成"排在终点之后"）⇒ 先判"卡住 / 人工闸"，再判"越过"
- ⚠️ 陷阱 #100 **`quality_json.lufs` 填了 loudnorm 的输入读数**（归一化**之前**那个，约 −22）⇒ 发布门禁把好片子拦下。QC 必须重量一遍落盘的成片
- ⚠️ **C12**：`av_sync` 仅诊断（不阻断发布）；保留 CFR + 显式 `-t` + 禁 `-shortest`（陷阱 #3）
- ⚠️ **水印缺失 ⇒ 跳过水印层**（不是失败）
- ⚠️ **已知偏差**：保底档 `fallback_720x1280_v1` 编的是 `h264_nvenc`，而正常档是 `libx264` —— 保底档依赖一个**可能不存在的硬件编码器**，方向反了（保底档应当比正常档**更容易**成功）。本机实测 NVENC 可用所以链路是通的；换机器前先 `uv run studio doctor` 看 `ffmpeg.nvenc` 那一行
- ⚠️ **已知偏差**：`QualityReport` 里**没有** `watermark_applied`，而 §06.4 门禁 1 写的是读 `quality_json.watermark_applied`。一期水印已改为**可选装饰**（缺失不阻塞），门禁 1 的判据取自 `manifest.json → watermark.enabled`；真要恢复硬门禁时两处一起改

### 二期（P1）预留 · 三层模板场景编排 · **P2**（不阻塞 M3）
- [ ] **T3-P1** 三层模板契约与加载器（YAML → Pydantic → DB；组件六类；八类校验）— 依赖 T3.7，契约 §03.6 / §04.2.0
- [ ] **T3-P2** IR 与构建器（`VideoIR` + bind pass + `$` 变量绑定）— 依赖 T3-P1，契约 §04.2.1
- [ ] **T3-P3** 自动填充 + `repeat_last` 场景扩展（克隆场景强制重抽素材、**禁绝对时间**）— 依赖 T3-P2，契约 §04.2.2 / §04.2.3
- [ ] **T3-P4** 场景级 filtergraph 编译 + 场景中间产物缓存 + 场景级重试 + 合流 — 依赖 T3-P3，契约 §04.2.5 / ADR-004
- **二期启动条件（三者同时满足）**：①一期稳定出片 ≥100 条 ②确有"片头/片尾/多段镜头编排"需求 ③磁盘与时间预算允许

> **>>> M3 门禁**：换稿不重剪（**同素材 + 同水印，换稿件直接出片**）；网页一键出新片并在线预览；水印/响度/相似度门禁通过。

---
## 4. 阶段 T4 · 网页实时操作台 + 四池并行 + 无人值守（14 任务 → 门禁 M4）

### T4.1 前端脚手架与设计系统 · **P1**（可与 T2/T3 并行）✅ **已完成（2026-09-14）**
- 依赖：T1.7 ｜ 里程碑：M4 ｜ 契约：§02.2 / §04.4 / **§04.9**
- [x] Vite + Vue3 + TS + Pinia（Vite 6 / Vue 3.5 / TS 5.6 / Pinia 2.3 / vitest 3）
- [x] 深色控制台布局 + 设计系统（`styles/tokens.css` 是**唯一**颜色/间距/字号来源 + 5 个基础组件）
- [x] **API 类型由 OpenAPI 自动生成**（**禁止手写接口类型**）⇒ `webcontracts.py` 纯函数渲染 + 生成物入库 + 契约测试逐字比对
- [x] WS 客户端：重连（指数退避 1s→2s→4s→8s→**上限 15s**）+ `since_id` 补发 + `seq` 缺口检测 + 心跳（`ping`/`pong` 半开检测）
- [x] `npm run typecheck` 零错误
- ✅ 验收命令（2026-09-14 实测全过）
  ```powershell
  .\tasks.ps1 web:verify
  #   npm run typecheck  ⇒ 零错误（vue-tsc --noEmit）
  #   npm run test       ⇒ 29 passed（4 文件：reconnect / client / http / logs store）
  #   npm run build      ⇒ 60 modules；dist/index.html 0.48 kB + index.css 9.62 kB + index.js 88.14 kB
  #   npm run size       ⇒ dist 共 0.09 MB（门禁 3.00 MB）OK
  .\tasks.ps1 check
  #   ruff format/check + mypy strict 全绿；pytest 1798 passed / 32 skipped / 1 deselected
  #   （含 tests/contract/test_web_contracts.py 7 例 + 门禁内的 web:check 漂移拦截）
  ```
- 📦 交付物
  - 契约生成：`src/studio/app/webcontracts.py`（`render_openapi_json` / `render_events_ts` / `write_web_contracts`）+ `scripts/dump_web_contracts.py`（`--check`）
  - 生成物（**入库 · 禁止手改**）：`web/openapi.json`、`web/src/ws/events.ts`、`web/src/api/types.gen.ts`
  - REST 模型（补上后生成的 TS 才不是 `unknown`）：`app/schemas/{health,logs}.py` + 两个 router 的 `response_model`
  - 前端骨架：`web/{package.json,vite.config.ts,tsconfig.json,index.html,.npmrc}` + `web/scripts/check-dist-size.mjs`
  - 前端源码：`api/{http,endpoints/{health,logs}}.ts`、`ws/{client,reconnect,events}.ts`、`stores/{ui,overview,logs}.ts`、`composables/{useWsConnection,useTaskStream}.ts`、`components/{AppButton,StatusDot,PanelCard,LogStream,EmptyState}.vue`、`styles/{tokens,base}.css`、`views/{Overview,Logs}.vue`、`App.vue` / `main.ts` / `env.d.ts`
  - 任务入口：`tasks.ps1 web` / `web:gen` / `web:check` / `web:verify`（`check` 已并入 `web:check`）
  - 测试：`tests/contract/test_web_contracts.py`（7 例）+ 前端 4 个测试文件（29 例）
- 🔧 施工裁定（**已回灌 §README.6 / §02.2 / §04.9 / §05.7**）
  111. **npm 缓存纳入 D 盘重定向**（`NPM_CONFIG_CACHE=D:\ai_models\npm_cache`）：`npm i` 默认把几百 MB 写进 `%LOCALAPPDATA%`，而 C 盘只剩 **1.4 GB**（门禁 ≥1 GB）⇒ 环境变量契约 **10 → 11 项**；`web/.npmrc` 另写一份 `cache=` 兜底（忘 source `scripts/env.ps1` 也不炸盘）。实测：装前 C 1.39 GB → 装后 **1.41 GB**（未恶化）。
  112. **前端类型的唯一真相是 Python**：`webcontracts.py` 是**纯函数**（同输入同输出、不碰文件系统），生成物入库，契约测试直接调它逐字比对 ⇒ "禁止手写接口类型"从纪律变成结构。`web:gen` 必须是两步（Python 渲染 → node 生成 TS），合成一条会在单侧环境缺失时失败得很难懂。
  113. **`fetch(` 全前端收敛到 `api/http.ts`**，由契约测试静态扫描 `web/src/**` 拦截（含反向断言：白名单文件里必须真的还有 `fetch(`，否则这条用例会变成永远通过）。
  114. **WS 客户端是"单连接多路复用"**（不在面板粒度建连）：服务端的合并/限流是 Hub 侧**全局**算的，连接数与面板数脱钩才能让"慢客户端"判定（环形缓冲 200 / 持续 10s）保持可解释；前端也就只需要维护**一份** `seq` 与 `since_id` 游标。
  115. **`seq` 是每连接单调 ⇒ `onopen` 必须重置基线**（`lastSeq = 0`）：新连接 `seq` 从 1 重新开始，沿用旧基线会把第一帧判成缺口 ⇒ 每重连一次白跑一轮 `resync`（陷阱 #60）。`since_id` 游标则**只进不退**：回退会让补发重复或漏，而两者都只在真断线时才暴露。
  116. **日志缓冲暂停时只丢显示、不丢游标**：`paused` 期间仍推进 `lastId`，否则恢复后会补发一屏重复行；丢弃条数如实显示（`droppedWhilePaused`），不假装没发生。
  117. **未施工的面板必须显式标 `ready:false` + 任务号**（导航里显示"待 T4.x"）：`PANELS` 表是"哪块能用"的唯一真相，避免"看起来能用其实没接线"。
  118. **`defineConfig` 从 `vitest/config` 取，且 vitest 与 vite 必须同大版本**：`vitest@2` + `vite@6` 会在 node_modules 里嵌套第二份 vite ⇒ `vite.config.ts` 的 `test` 段在 `vue-tsc` 下报 TS2769（陷阱 #58）。判据：`node_modules/vitest/node_modules/vite` 不该存在。
- ⚠️ 新增陷阱 3 条已并入 §10（编号 **58–60**）：`vite.config.ts` 类型来自嵌套的旧 vite / WS 事件 `log_id` vs 快照 `id` 静默 `undefined` / 重连未重置 `seq` 基线白跑 `resync`
- ⚠️ **面板占位说明**：T4.1 只交付**总览台**与**实时日志**两块真面板；其余 9 块（选题/稿件/配音/渲染/合成配置/素材库/四池调度/人物库/发布）在导航中显示为 `待 T4.x` 占位，**不假装可用**（裁定 117）
- ⚠️ **REST 面仍很窄**（仅 `/api/v1/health` + `/api/v1/logs`）：其余面板的接口随各自任务补齐，届时只需重跑 `web:gen` 即可获得类型

### T4.2 ① 总览台 · **P1** ✅ **已完成（2026-09-14）**
- 依赖：T4.1, T1.5 ｜ 里程碑：M4 ｜ 契约：§04.4.5 / **§04.5.6**（新增）
- [x] 四池状态卡片（`pending`/`claimed`/`failed`/`dead`）+ worker 心跳（`stale` 显式标"疑似猝死"）
- [x] 队列长度、今日产量（**本地日**口径，窗口一并显示）
- [x] 资源占用（CPU / 内存 / 显存 / 磁盘），采样 0.2Hz（5s）
- [x] `free_D < 15GB` ⇒ 显示 `DISK_LOW` 告警（**状态迁移才写**，不是每拍一条）
- [x] 启动 / 暂停 / **一键全自动**（+ 回退 `grade_a` / `off`）
- ✅ 验收命令（2026-09-14 实测全过）
  ```powershell
  .\tasks.ps1 check
  #   ruff format/check + mypy strict 全绿；pytest 1924 passed / 32 skipped / 1 deselected
  #   （含 tests/integration/test_overview_api.py 34 例）
  .\tasks.ps1 web:verify
  #   npm run typecheck ⇒ 零错误
  #   npm run test      ⇒ 153 passed（8 文件：overview store 20 / topics 39 / scripts 37
  #                       / logs 31 / reconnect 8 / client 7 / http 6 / highlight 5）
  #   npm run build     ⇒ 81 modules；dist/index.html 0.48 kB + index.css 24.54 kB + index.js 148.08 kB
  #   npm run size      ⇒ dist 共 0.17 MB（门禁 3.00 MB）OK
  ```
- 📦 交付物
  - 服务端：`src/studio/services/{metrics_service,overview_service}.py`、`src/studio/app/metrics.py`（`MetricsPump` 采样泵）、`src/studio/app/routers/overview.py`、`src/studio/app/schemas/overview.py`、`src/studio/db/repositories/stats_repo.py`（`today_output` + `local_day_window`）、`src/studio/core/config.py`（`RuntimeSettings` / `load_runtime_settings` / `set_auto_approve_policy`）、`src/studio/app/deps.py`（`metrics` 快照 provider + `overview_service_for`）、`src/studio/app/lifespan.py`（泵的起停**顺序**）
  - 前端：`web/src/views/Overview.vue`（重写）、`web/src/stores/overview.ts`、`web/src/api/endpoints/overview.ts`、`web/src/App.vue`（`healthError`）
  - 测试：`tests/integration/test_overview_api.py`（34 例）、`web/src/stores/overview.test.ts`（20 例）
- 🔧 施工裁定（**已回灌 §04.5.6 / §05.7**）
  136. **总览台 REST 面 = `GET /api/v1/overview` 一次拿全**（四池 + 今日产量 + 资源 + 服务就绪 + 策略）：与 T4.4「一次请求拿全」同一取舍 —— 首屏要的是"同一时刻的一张照片"，四个请求拼出来的是四张。
  137. **只暴露"启动"，不做"停止"**：`ServiceManager.stop()` 的第一个目标就是 `api` 自己 —— 在 API 进程里停服务等于**自杀**。停止的正门是 `停止.bat`。启动必须 `open_browser=False`（点按钮的人已经在浏览器里）+ `doctor_gate=True`；入口一把 `threading.Lock` 非阻塞地拿，拿不到 ⇒ 409 `SERVICE_START_BUSY`。
  138. **暂停/恢复池直接写 `pool_settings`，不重启 worker**：`JobStore.claim()` 开头就是 `if runtime.paused: return None` ⇒ 新单元不认领、**在途跑完**。恢复时 `paused_at` / `paused_by` **清空**（历史在 `audit_ops` 里）；重复点同状态 ⇒ `changed=false` 不重复留痕。
  139. **一键全自动改 `config/app.yaml` 的 `approval.auto_approve_policy` 一行**：**逐行替换、保留注释**（不用 `yaml.safe_dump` 整份重写）+ 回读校验 + 同步内存 `RuntimeSettings` + `audit_ops`。顺序必须"先写盘（含回读）→ 再改内存 → 再留痕"。
  140. **`metrics.tick` 的采样器 = `app/metrics.py` 的 `MetricsPump`**（`app` 层才能碰 Hub，`services/` 碰它会成环 · 陷阱 #64）：5s 一拍，同拍扇出 `metrics.tick` + 四池 `pool.stats`（1Hz 上限内），心跳 `pool.worker_status` **只在指纹变化时发**；整拍丢 `asyncio.to_thread`。
  141. **`DISK_LOW` 的去重是状态机不是节流**：`free_D < 15GB` 会持续成立，而 `system.alert` 永不合并 ⇒ 每拍写一条 = 一小时 720 条。只在 `ok → low` 写告警；`low → ok` 写 `info`（`payload.code='DISK_RECOVERED'`，**不在** 8 个 `AlertCode` 里 ⇒ 走 `logs` 通道）；**首拍照常判**（API 起来时磁盘已经满了同样要留痕）。
  142. **`metrics` 通道补 `deps.py` 的快照 provider**（与 `pools` / `topics` 同手法）：没采过 ⇒ `metrics: null`，**不发全 0 的假快照**。
  143. **首拍健康 ≠ "恢复"**：磁盘状态机的初值 `None` 是"**还没采过**"，不是"低水位"；少了这一条，每次启动都会凭空写一条 `DISK_RECOVERED`，而它排在 tail 游标之后 ⇒ **顶掉日志通道的第一帧**（T1.7 的两条补发用例就是这么挂的）。
- ⚠️ 误触全自动 ⇒ 二次确认（**只对它一个**：暂停可逆且局部，代价不对称）+ `audit_ops` + **可一键回退** `grade_a` / `off`
- ⚠️ 采样开销 ⇒ 0.2 Hz（5s）+ 整拍丢线程池（`nvidia-smi` 一次 80–150ms）
- ⚠️ WS 事件策略：`metrics.tick` **直接合并**（载荷与 `ResourcesModel` 同源同形）；`pool.stats` / `pool.worker_status` 是 `PoolStatus` 的**真子集** ⇒ 走 500ms 合并窗口**重拉**（不抄第二份卡片形状）
- ⚠️ 断线期间的事件是真丢了 ⇒ 重连成功必须以一次全量重拉收尾；**不做兜底轮询**（"看起来在动其实是旧数据"比明说"不动了"更糟）
- ⚠️ 陷阱 **#75** "今日"用 UTC 日 ⇒ UTC+8 的 08:00 前算错一天
- ⚠️ 陷阱 **#76** 在 API 进程里停自己 ⇒ 只暴露"启动"，停止走 `停止.bat`
- ⚠️ 陷阱 **#77** 采样泵在工作线程里 `publish()` ⇒ `RuntimeError: Non-thread-safe operation`（`pool.*` / `metrics.*` 三个事件此前**从未真正发出去过**）
- ⚠️ 陷阱 **#78** 首拍健康被当成"磁盘水位恢复" ⇒ 顶掉日志通道第一帧

### T4.3 ② 选题面板 · **P1** ✅ **已完成（2026-09-14）**
- 依赖：T1.9, T4.1 ｜ 里程碑：M4 ｜ 契约：§04.1.3 / **§04.5.5**（新增）
- [x] 方向卡片（5–8 个）+ **历史批次下拉**（新到旧，上限 20）+ 每方向选题计数
- [x] 选题瀑布流 + 评分理由（`score` / `reason` / `hook_type` / `similar_to`）
- [x] 勾选入队（创建任务；**两步确认** + 幂等键 `topic:<id>`）
- [x] 导入热点（扫盘 `data/hot/*.md` + 网页粘贴 ⇒ `webui-<ulid>.md`）
- [x] 触发分析（Planner 5–8 方向）/ 生成选题（Ideator 逐方向 3–5 条）/ 人工加选题
- [x] 按 `score` 降序 + 方向分组 + 默认折叠
- [x] 长任务**单飞**（已在跑 ⇒ 409 `TOPIC_BATCH_RUNNING`，**不排队**）
- ✅ 验收命令（2026-09-14 实测全过）
  ```powershell
  .\tasks.ps1 check
  #   ruff format/check + mypy strict 全绿；pytest 1890 passed / 32 skipped / 1 deselected
  #   （含 tests/integration/test_topics_api.py 15 例）
  .\tasks.ps1 web:verify
  #   npm run typecheck ⇒ 零错误
  #   npm run test      ⇒ 133 passed（7 文件：topics store 39 / scripts store 37 / logs store 31
  #                       / highlight 5 / reconnect 8 / client 7 / http 6）
  #   npm run build     ⇒ 80 modules；dist/index.html 0.48 kB + index.css 22.85 kB + index.js 135.75 kB
  #   npm run size      ⇒ dist 共 0.15 MB（门禁 3.00 MB）OK
  ```
- 📦 交付物
  - 服务端：`src/studio/app/routers/topics.py`、`src/studio/app/schemas/topics.py`、`src/studio/services/topic_service.py`（`add_manual_topic` + 事件扇出）、`src/studio/services/script_service.py`（`enqueue`）、`src/studio/app/deps.py`（`topic_service_for` / `script_service_for` / `active_persona` / `topics` 快照 provider）、`src/studio/db/repositories/{direction_repo,topic_repo}.py`、`src/studio/core/errors.py` + `src/studio/app/errors.py`（3 个新错误码 + 状态码映射）
  - 前端：`web/src/views/Topics.vue`、`web/src/stores/topics.ts`、`web/src/api/endpoints/topics.ts`、`web/src/stores/ui.ts`（`topics` 面板 `ready: true`）、`web/src/App.vue`
  - 测试：`tests/integration/test_topics_api.py`（15 例）、`web/src/stores/topics.test.ts`（39 例）
- 🔧 施工裁定（**已回灌 §04.5.5 / §05.7**）
  129. **长任务单飞用进程级 `threading.Lock` 非阻塞**（不是 `asyncio.Lock`）：同步路由跑在 Starlette 线程池（每次可能不同线程），跨"线程池 + 事件循环"只有 `threading.Lock` 不会炸；已在跑 ⇒ **409 而不是静默排队**（排队会让前端挂住，用户也不知道自己排第几）。
  130. **人工加选题挂靠固定批次 `manual`**（懒建一次）：`direction_id` 是 NOT NULL + 外键，现造一次性批次会让统计散落。
  131. **人工加选题去重只提示不拦**：模型产出的重复是噪音（白烧 token），人加的重复是**明确意图**；`exec_feasible` 恒为真（那是模型对"3 分钟能不能做完"的估计，人加了它没有可估的输入）。
  132. **`select` 的 `draft_now` 默认 `false`**：入队是**意图**，跑写稿是**执行**；勾上它等价于 CLI 的 `studio script draft`。
  133. **`script_service_for(with_agents=False)`**：勾选入队是纯业务规则，不该因为"LLM Key 没配"就点不动（与裁定 125 同手法）。
  134. **`analyze` / `ideate` 失败返回 200 + 体内 `ok:false`**：与确认闸"能返回就是成功"不同 —— 这里 HTTP 层确实成功了，失败的是**产出**（`LLM_SCHEMA_INVALID` / `HOT_TEXT_EMPTY`）。用 5xx 会让"重试三次"看起来像后端崩了，而真正要看的是 `error_message` 与逐方向 `outcomes`。
  135. **响应模型的集合字段一律必填**（`x: list[T]` 而非 `Field(default_factory=list)`）：后者在 JSON Schema 里既不进 `required` 也不带 `default` ⇒ 生成类型是 `T[] | undefined`，前端被迫到处 `?? []`（本面 17 个字段）。
- ⚠️ 勾选疲劳 ⇒ 降序 + 方向分组 + 默认折叠
- ⚠️ 重复勾选 ⇒ 幂等键 `topic:<id>` 防重复建任务（第二次 `created_task=false`）
- ⚠️ 长任务被连点 / 双标签页重放 ⇒ 单飞守卫 409（**不排队**，不重复烧 token）
- ⚠️ 陷阱 **#68** 响应集合字段带 `default_factory` ⇒ 生成类型假可选（`T[] | undefined`）
- ⚠️ 陷阱 **#69** 带 `default` 的请求字段被渲染成必填 ⇒ 前端显式传 `import_sources` / `per_direction`
- ⚠️ 陷阱 **#70** `asyncio.Lock` 跨"线程池 + 事件循环"会炸 ⇒ `threading.Lock`
- ⚠️ 陷阱 **#71** 动作刚报的错被紧随的成功刷新抹掉 ⇒ 动作级 / 拉取级两条错误信道
- ⚠️ 陷阱 **#72** 一个用例里换第二次假件不生效 ⇒ 原函数取未被 patch 的那份
- ⚠️ 陷阱 **#73** 假件方向数不够 ⇒ `LLM_SCHEMA_INVALID`（schema 要 5–8 个方向 / 每方向 3–5 条）
- ⚠️ 陷阱 **#74** `system_logs` 的列名是 `payload_json`（不是 `payload`）

### T4.4 ③ 稿件面板 + 确认闸 · **P0**（唯一人工节点）✅ **已完成（2026-09-14）**
- 依赖：T1.11, T4.1 ｜ 里程碑：M4 ｜ 契约：§04.4.4 / **§04.5.4**（新增）
- [x] 稿件全文 + 审稿评分（**双通道明细**：`rule_detail` + `llm_detail` 六维度 + `issues`）
- [x] 版本对比（`v1→v2` diff；两个版本号**都必须显式选**）
- [x] 确认 / 退回（**必填意见**）/ 放弃（**二次确认**）
- [x] 批量操作（**逐条写 `audit_ops`**，部分失败不回滚）
- [x] `revision_round` 可见（原文 §2.2⑦"呈现稿件 + 评分 + 修改次数"）
- [x] 队列状态过滤（待审 / 已通过 / 已退回 / **已放弃**）+ 捞回（`discarded → pending`，连带恢复 `topic_candidates`）
- ✅ 验收命令（2026-09-14 实测全过）
  ```powershell
  .\tasks.ps1 check
  #   ruff format/check + mypy strict 全绿；pytest 1875 passed / 32 skipped / 1 deselected
  #   （含 tests/integration/test_approvals_api.py 22 例 + test_scripts_api.py 13 例
  #     + tests/unit/domain/test_diff.py 11 例 + tests/unit/ws/test_hub_events.py 11 例）
  .\tasks.ps1 web:verify
  #   npm run typecheck ⇒ 零错误
  #   npm run test      ⇒ 94 passed（6 文件：scripts store 37 / logs store 31 / highlight 5 / reconnect 8 / client 7 / http 6）
  #   npm run build     ⇒ 75 modules；dist/index.html 0.48 kB + index.css 17.57 kB + index.js 118.60 kB
  #   npm run size      ⇒ dist 共 0.13 MB（门禁 3.00 MB）OK
  ```
- 📦 交付物
  - 服务端：`src/studio/app/routers/{approvals,scripts}.py`、`src/studio/app/schemas/{approvals,scripts}.py`、`src/studio/app/errors.py`（统一错误信封 + 状态码映射）、`src/studio/domain/diff.py`（块级逐句对照）、`src/studio/services/review_service.py`（`rescue_task` + `RescueOutcome`）、`src/studio/ws/hub.py`（`envelopes_for()`）、`src/studio/core/proto.py`（`EventKind` 下沉 + `EVENT_PAYLOAD_KEY`）
  - 前端：`web/src/views/Scripts.vue`（队列 + 详情 + 决断条 + 版本对照）、`web/src/components/{GradeBadge,SentenceDiffList}.vue`、`web/src/stores/scripts.ts`、`web/src/api/endpoints/{approvals,scripts}.ts`、`web/src/api/http.ts`（`apiPost`）
  - 测试：`tests/integration/test_approvals_api.py`（22 例）、`tests/integration/test_scripts_api.py`（13 例）、`tests/unit/domain/test_diff.py`（11 例）、`tests/unit/ws/test_hub_events.py`（11 例）、`web/src/stores/scripts.test.ts`（37 例）
- 🔧 施工裁定（**已回灌 §04.4.4 / §04.5.4 / §05.7**）
  124. **批量通过路径定为 `POST /api/v1/approvals/approve_batch`**：§04.4.4 表里的 `/tasks/{id}/approve_batch` 的 `{id}` 在请求体是 `task_ids` 时**无处可用**（没有"哪一个任务"这回事）。规格书已同步。
  125. **`ReviewService` 的 Agent 依赖可选**（`reviewer` / `editor` 默认 `None` + `_require_agents()`）：确认闸是**纯业务规则**，REST 面不该为了"点一个按钮"去装配 LLM 网关。读路径同理（`GET /scripts/{id}` 不碰 Agent）。
  126. **事件搭日志的车**（`EVENT_PAYLOAD_KEY`）：worker 没有 IPC 能把事件推给 API 进程的 Hub，而"表 → 推送"天然跨进程 ⇒ `Hub.envelopes_for(row)` 一行日志发**两条**（日志帧 + 结构化事件）。`EventKind` 因此下沉到 `core/proto.py`（`services/` 不能反向 import `ws/`）。
  127. **错误信封只有一种形状**：业务错误与 `RequestValidationError` 都翻成 `StudioError.to_dict()`；批量**部分失败不回滚**，逐条如实报 `code` / `message` / `remediation`。
  128. **`diff_sentences` 走 `difflib` 块级匹配而非逐 `seq` 对齐**：插入一句不能把后面全标成"改了"；`replace` 块内按位置配对，多余一侧降级 `insert` / `delete`。
- ⚠️ 误点放弃 ⇒ 二次确认 + **可"捞回"**（`discarded → pending`，选题候选一并恢复）
- ⚠️ 批量误伤 ⇒ **先摊开清单再确认**（`requestBatch()` 不发请求，`confirmBatch()` 才发）
- ⚠️ 陷阱 **#63** 事件名写成 `event` ⇒ `logger.info(msg, **payload)` 抛 `TypeError` ⇒ 键固定 `event_kind`
- ⚠️ 陷阱 **#64** `services/` 反向 import `ws/` 成环 ⇒ 事件枚举下沉 `core/proto.py`，`ws` 侧 re-export
- ⚠️ 陷阱 **#65** 逐 `seq` 对齐 ⇒ 插入一句把后面全标成"改了" ⇒ 块级匹配
- ⚠️ 陷阱 **#66** 两种错误形状 ⇒ 前端两套解析 ⇒ 应用级 handler 统一信封
- ⚠️ 陷阱 **#67** 部分失败报成整体失败 ⇒ 人重按 ⇒ 逐条如实返回、不回滚

### T4.5 ④ 配音面板 · **P1** ✅ **已完成（2026-09-16）**
- 依赖：T2.9, T4.1 ｜ 里程碑：M4 ｜ 契约：§04.3.7 / §04.5.13
- [x] 逐句进度条（已完成 / 进行中 / 失败 / 跳过）+ 一条 `settled / total` 的比例条
- [x] 换音色（提示"将重配 N 句" + 二次确认）
- [x] 重配某句（`synthesizing` 的行**按钮是灰的**，不是"点了没反应"）
- [x] 试听单句（`<audio>` 直接取后端给的 `audio_url`；没有音频 ⇒ 不给播放键）
- [x] `skipped` 句高亮 + 原因可见（整行染色 + `tts_error` 写在行上）
- ✅ 逐句状态实时刷新（**轮询 1s**，见裁定 247/248）；重配 ⇒ 该句回 `pending` 并重合成；换音色 ⇒ 二次确认；试听可播放；`skipped` 高亮
- ⚠️ 重配期间渲染抢跑 ⇒ 守卫：`voicing` 态不允许 render 认领
- ⚠️ 试听抢 GPU ⇒ **走缓存文件，不触发新合成**（后端 `preview_audio` 保证，面板只管放）
- **真机演练（2026-09-16 · 真 API 进程 `studio serve --port 8791` · 真库 `data/studio.db` · 真 wav）**：
  - `GET /voices?task_id=` ⇒ 200，**5 个音色**（3 个 SAPI + 2 个参考音 `bear_da` / `bear_xiong`），带这条任务现在的映射
  - `GET /sentences?task_id=` ⇒ 200，4 句全 `done`（4533 / 4478 / 3693 / 3273 ms）、每句带 `audio_url`、`timeline_total_ms=null`（这条还没出过时间轴）
  - `GET /media/voice/<task>/s001.wav` ⇒ **200**（199940 字节 · `audio/wav`）；`Range: bytes=0-31` ⇒ **206** `content-range=bytes 0-31/199940`
  - `GET /media/.../s099.wav` ⇒ 404 `SCRIPT_NOT_FOUND`；`/api/v1/media/voice/../../config/app.yaml` ⇒ **422**（形状外的东西进不来）
  - `POST /sentences/<id>/resynth` ⇒ 200 `status=pending`、`timeline_stale=true`、`job_created=false`；**真 worker 认领并念完**：作业 `01M2M9B6M0RRT2PF2H458D3BF9` ⇒ `succeeded`、`tts_attempts=1`、句子回 `done`、`version` 归 1（**1.5s 内**，面板轮询就能看到）
  - `PATCH /tasks/<id>/voice_map` **只带改过的那个角色**、不带 confirm ⇒ **409** `VOICE_MAP_CONFIRM_REQUIRED`（`affected=4 / total=4 / speakers=["bigbear"] / sentences=[1,2,3,4]`，**一个字节都没写**）
  - 提交里**显式**写本机没有的音色 ⇒ 422 `TTS_VOICE_MISSING`（`context.missing` + `available`）；字段名写错（`voiceMap`）⇒ 422（`extra=forbid`）
  - 带连字符的任务号（`ui-20260916-120000`）⇒ 形状过了 ⇒ **404**（不再是 422，见裁定 251）
  - `GET /` ⇒ 200（SPA 由 `studio serve` 托管，`dist` 里能查到"音色映射"）
- **验收命令**：
  - `cd web; npm run verify` ⇒ `vue-tsc` + **vitest 341 passed（16 文件；新增 `stores/voice.test.ts` 31 例）** + `vite build` + 体积门禁 **0.30 MB / 3.00 MB**
  - `.venv\Scripts\python.exe -m pytest tests/integration/test_voice_api.py -q` ⇒ **17 passed**（新增 2 例：带连字符的任务号 / 不动没碰过的占位映射）
  - `.venv\Scripts\python.exe -m pytest tests/unit/services/test_voice_service.py -q` ⇒ **13 passed**
  - `.\tasks.ps1 check` ⇒ **3100 passed / 32 skipped / 12 deselected**（126s）；`ruff format` + `ruff check` + Web 契约 + `mypy`（304 source files）全绿
- **交付物**：`web/src/api/endpoints/voice.ts`（4 个端点 + 从 `types.gen.ts` 取的 9 个类型别名）；`web/src/stores/voice.ts`（`configureVoiceApi` 注入点 + 17 个纯函数 + 轮询 + 二次确认流）；`web/src/views/Voices.vue`（任务卡 / 逐句表 / 音色映射 + 确认框）；`web/src/stores/voice.test.ts`（31 例）；接线：`stores/ui.ts` 的 `voices` ⇒ `ready: true`、`App.vue` 的 `VIEWS`；`styles/tokens.css` 新增 3 个淡底色令牌；后端两处**按契约修**：`app/routers/voice.py` 的路径字符集、`services/voice_service.py` 的 `TTS_VOICE_MISSING` 判据
- **施工裁定（本轮新增 244–252）**：
  - **244** 面板**不自己拼** `audio_url`：url 由服务端给，没有音频时是 `null` ⇒ 那一行干脆不画播放键。自己拼 = 把"文件落在哪"抄进前端（目录一改就是"点了播放没反应"，且不报错），发一个注定 404 的 url 同样如此（与裁定 243 同一条）
  - **245** 换音色**先问代价再动手**：`requestVoiceChange` 不带 `confirm` ⇒ 拿到 409 就把 `context.affected` 摆进确认框；用户点头才 `confirmVoiceChange` 带 `confirm=true` 重发。少了这一步，一次误点会重配几十句，而用户没有后悔的机会
  - **246** `busy`（想失效但没动成的那几句）单独走**黄色**横幅，不拼进绿色成功消息：绿底横幅上写一句警告，人不会当回事 —— 而它的后果是"成片里那几句还是旧嗓子"
  - **247** 配音面板用**轮询**（1s，与渲染面板同口径）而不是 WS `sentence.updated`：契约里有这条事件，但后端**目前没有生产者**；而"这一句念完了没有"1 秒一次 GET 就够，断线重连天然正确。真要做推送，落点是给配音收口加一路 `publish`，不是把轮询周期调小
  - **248** 轮询**只在有活时开**，判据是 `pending + synthesizing > 0`（失败与跳过都是**定局**，不会自己变）。空闲时一直拉，面板上那些数字跳动的唯一原因会变成"我们在定时问"
  - **249** 面板提交的 `voice_map` **只带真正改过的角色**（不是整张表）：后端校验的是"提交里出现的每一个音色"，把整张表发上去等于顺手替用户断言了他没碰过的那些行 —— 而其中可能正躺着一条本机找不到的存量值（真机数据就是 `{"bigbear": "bigbear", "littlebear": "littlebear"}`）
  - **250** `set_voice_map` 的 `TTS_VOICE_MISSING` **只判这次提交的那几条**，不再判合并后的整张表。契约 §04.3.7「音色校验的两条线」早就写明"**存量**的音色不在本机不报错、由 `resolve_voice` 退回进程音色并标 `fallback`"，而实现判的是整张表 ⇒ **新建任务第一次换音色必然 422**：用户改的是熊大，被拒的理由却是他没碰过的熊二。这是实现与已交付契约不符，按契约修（陷阱 #123）
  - **251** 路径契约接受**带连字符的任务号**（`[0-9A-Za-z_-]{1,64}`）：任务号是**调用方起的名**（`RenderJobRequest.task_id` 只限长度、不限字符集），渲染面板默认给的就是 `ui-YYYYMMDD-HHMMSS` ⇒ 按 `[0-9A-Za-z]` 卡的话，面板自己创建的任务号一进配音面板就整屏 422，而 422 报的是"路径不合法"（陷阱 #122）。放开的是**任务号**那一段，媒资键的整串形状一个字都没松
  - **252** 音色下拉框对"库里记着、本机找不到"的值**要说出**来（`· 本机找不到（换一个）` + 红标），不能让 `<select>` 空着：空白会被读成"这个角色没有音色"，而真实原因可能是"参考音没入库 / 系统语音包没装"（陷阱 #125）
- ⚠️ 新增陷阱 6 条已并入 §10（编号 122–127）

### T4.14 四屏端到端串联（选题 → 稿件 → 配音 → 渲染）· **P1** ✅ **已完成（2026-09-16）**
- 依赖：T4.3, T4.4, T4.5, T4.6 ｜ 里程碑：M4 ｜ 契约：§04.5.14
- [x] 选题卡片：入队过的选题上多一颗「去稿件 →」（任务号由后端生成，前端不猜）
- [x] 稿件详情：行尾「去配音 →」「去渲染 →」两颗按钮
- [x] 配音面板：「去渲染 →」；任务号空着时按钮是**灰的**（不发一个注定 422 的跳转）
- [x] 渲染面板：「去配音 →」（表单旁一颗 + 最近任务每行一颗 —— **反向跳转**）
- [x] 跳转语义落在 `stores/ui.ts`：`goTo(panel, taskId)` 记一笔 + 切面板；目标面板**挂载时** `takeHandoff()` 认领
- ✅ 端到端路径：一条选题 → 稿件 → 配音 → 渲染，全程**不手抄任务号**
- ⚠️ 认领即清空 ⇒ 切回来自动跳第二次这种事不会发生；人自己点侧边栏走则**顺手清掉**待认领的那一笔
- ⚠️ 渲染面板**只填框、不自动出片**：出片是花钱花时间的那一步，必须由人按下去
- **验收命令**：
  - `cd web; npm run verify` ⇒ `vue-tsc` + **vitest 355 passed（17 文件；新增 `stores/ui.test.ts` 14 例）** + `vite build` + 体积门禁 **0.30 MB / 3.00 MB**
  - `.\tasks.ps1 check` ⇒ **3101 passed / 32 skipped / 12 deselected**；`ruff format` + `ruff check` + Web 契约 + `mypy`（304 source files）全绿
- **交付物**：`web/src/stores/ui.ts`（`FlowPanelId` / `handoffTaskId` / `claimHandoff` + store 的 `goTo` / `takeHandoff` / `pendingHandoff`）；`web/src/stores/ui.test.ts`（14 例）；四屏接线：`Topics.vue` / `Scripts.vue` / `Voices.vue` / `Renders.vue`
- **施工裁定（本轮新增 253–254）**：
  - **253** 跳转**不带任何业务参数**（只有"去哪一屏 + 哪个任务号"），也不预判目标面板的状态：面板是 `v-if` 挂的，切过去必然重新挂载，所以"用上这个任务号"放在**目标面板的 `onMounted`**，而不是让外壳去调别人的 store。外壳一旦开始知道"配音面板要拉逐句"，四屏就绑死了
  - **254** 认领顺序是**先拉列表、再认领跳转**：稿件面板的 `refresh()` 会把"不在当前状态列表里"的选中项清掉，配音面板的 `setTaskId()` 会清掉上一条任务的快照 —— 反过来的话，跳转会被自己的首屏覆盖掉，而且**不报错**（陷阱 #128）
- ⚠️ 新增陷阱 2 条已并入 §10（编号 128–129）

### T4.14+ 一键出片面板（整条链路图形化：文案 → 配音 → 渲染出片）· **P0** ✅ **已完成（2026-09-17）**
- 依赖：T4.14, T4.6, T2.9 ｜ 里程碑：M4 ｜ 契约：**§4.6.8（新增）**
- [x] 服务层 `services/pipeline_job_service.py`：进程内登记表 + 后台线程 + 协作式取消 + **同任务幂等**
- [x] `pipeline_service.plan_task`：把"这条任务点了会怎样"提前算出来（与 `run_task` **共用** `_STUCK`/`_FORWARD`/`_check_until`）
- [x] REST **五个端点** `app/routers/pipeline.py` + `app/schemas/pipeline.py`（首屏 / 预览 / 开一条 / 轮询 / 叫停）
- [x] 接线：`AppState.pipeline_jobs`（`build_state`）+ `lifespan` 起停 + `main` 挂路由
- [x] 前端 `web/src/views/Pipeline.vue` + `stores/pipeline.ts`（+ `pipeline.test.ts` **23 例**）+ `api/endpoints/pipeline.ts`
- [x] 侧边栏第 14 块面板「一键出片」；`stores/ui.ts` 的 `FlowPanelId` 加上 `pipeline`，稿件详情多一颗「一键出片 →」
- ✅ `pytest tests/integration/test_pipeline_api.py -q` ⇒ **11 passed**，四条验收逐条对上：
  - ① 首屏的落点**一个不多一个不少**（拿 `supported_until()` 逐字比对），且每个都有名字与说明
  - ② 预览：审稿中的任务说"从 reviewing 一路推到 completed"；落点就是它自己时说"再点一次不会重跑（幂等）"；
    确认闸 / `failed` ⇒ `runnable=false` + 原因；没有生效稿件 ⇒ `has_script=false` 提前说；落点写错 ⇒ **422** + `context.supported`
  - ③ 主路径：填任务号 → 开始出片 → 轮询到 `succeeded`（`percent=100`、`steps=[voice, render]`、日志尾巴有 `[voice]`/`[done]`）
    → 成片落在 `data/output/videos/` 且能播；首屏 `active` 清空、`jobs` 里有它
  - ④ 幂等与取消：连点两次只开一条（`deduped=true`，预览里带 `active_job`）；跑着时 `percent=50`；
    按取消**立刻返回但状态还是 running**，放行后在下一个检查点变 `canceled`
- ✅ `cd web && npm run verify` ⇒ **378 passed**（18 文件）+ `vue-tsc` + `vite build` + 体积门禁 **0.32 MB / 3.00 MB**
- ✅ `.\tasks.ps1 check` ⇒ **3529 passed / 32 skipped / 27 deselected**（比 T5.3 多 **11 例**）；
  `ruff format` + `ruff check` + Web 契约（`web/openapi.json` 重生成，新 5 个端点）+ `mypy` 全绿
- **交付物**：
  - 服务层 `src/studio/services/pipeline_job_service.py`（`PipelineRequest` / `PipelineJob` / `PipelineJobService`
    + `supported_until()`；`MAX_JOBS_KEPT=50` / `MAX_LOG_LINES=200`；job id 前缀 `p`，与渲染的 `r` 分开）
  - 编排侧 `src/studio/services/pipeline_service.py`：新增 `PipelinePlan` + `plan_task`，并把落点校验抽成 `_check_until`
  - REST `src/studio/app/routers/pipeline.py` + 契约 `src/studio/app/schemas/pipeline.py`
  - 接线 `src/studio/app/deps.py` · `src/studio/app/lifespan.py` · `src/studio/app/main.py`
  - 前端 `web/src/views/Pipeline.vue` · `web/src/stores/pipeline.ts` · `web/src/stores/pipeline.test.ts` · `web/src/api/endpoints/pipeline.ts`
  - 测试 `tests/integration/test_pipeline_api.py`（11 例，进 `check`）
- **施工裁定（本轮新增 276–281）**：
  - **276** 一键出片**不新起一个池**，而是进程内登记表 + 后台线程（与 `render_jobs` 同形）：整条链路的最后一步本来就是进程内的渲染，而把它接进队列要先有一个 `pipeline` 池。代价写在明处（重启即丢、只在本进程可见），替换点是 `PipelineJobService._run`
  - **277** `submit` **同任务幂等**（该任务已有在跑的 job ⇒ 原样返回那一条 + `deduped=true`）：连点两次是很自然的动作，而两条链路同时改一个任务的状态轻则踩乐观锁、重则**渲染两遍**（花的是真金白银的编码时间）
  - **278** 落点收**字符串**、由服务端按 `supported_until()` 复核（422 + `context.supported`），**不在这里再抄一份枚举** —— 抄了之后加落点时总有一处会忘，而忘了的那一处只会让新落点从界面上消失。下拉框文案的键由契约用例盯着覆盖全部落点
  - **279** "点了会怎样"由**服务端**算（`plan_task`，与 `run_task` 共用判据），前端只负责显示：判据写两份 ⇒ 面板说能跑、点了报错（或更糟：面板说不能跑、其实能跑）
  - **280** `PipelineJob.percent` 在 `succeeded` 时**恒为 100**：这条链路的进度回调不是连续的，最后停在哪取决于它是从哪一步收尾的（陷阱 146）。`RenderJobService` 的同一处一并修掉
  - **281** 这一屏**不重复渲染层参数**（画布档 / 字幕 / 线程数）：要么得多问后端一次"配置里现在开着吗"（多一处会过时的显示），要么变成"看着是关的、实际跟随配置"的假开关 —— 后者比没有更糟
- ⚠️ 新增陷阱 3 条已并入 §10（编号 146–148）

### T4.6 ⑤ 渲染面板 · **P1** ✅ **已完成（2026-09-15）**
- 依赖：T3.4, T3.7, T4.1 ｜ 里程碑：M4 ｜ 契约：§04.2.8
- [x] **合成进度**（阶段 + 已完成 / 总数 + 日志尾巴；**轮询** 1s，不是 WS）
- [x] **面板上的出片表单**：任务号 / 口播文案（留空 ⇒ 读库里生效稿件）/ 画布档 / 音色 / 复用配音 / 种子 / 线程数
- [x] 成片列表 + **在线播放**（HTTP Range，`<video>` 可拖进度条）+ 下载
- [x] 触发渲染（**进程内**任务登记表 + 单工作线程；取舍写在 `src/studio/app/routers/render.py` 的模块说明里）
- [x] 取消（**协作式**：排队中的立刻作废，在跑的走到下一个检查点；ffmpeg 起来后等它自己结束）
- [x] 水印**可选**：不贴就在面板上写出原因（不再是 D5 硬门禁 —— 见 2026-09-15 的口径变更）
- [x] 图形界面**由 API 直接托管**（`web/dist` 挂在 `/`，不另起静态服务器）
- ✅ 进度推进；成片可在线播放；触发渲染真的出片；水印处置可见
- ⚠️ 进度是**粗粒度**的（配音第 N 句 / 渲染中 / 完成），不追 `-progress` 的逐帧 —— 那要动 Hub 的协议与快照注册表
- ⚠️ 任务登记表在**进程内存**里：API 重启即丢（成片在盘上，不受影响）
- ⚠️ 并发上限 1（与 `pools.yaml` 的 render 池同口径：同时开两条 1080×1920 只会都变慢）
- ✅ **接进 render 池队列**（T3.7 收口 · 2026-09-16）：`render/final` 的 handler 落地（`pools/render_worker.py`）+ `workers/run_render.py` 注册 + `runner.HANDLER_MODULES` 声明 ⇒ 启动器不再把它判成 `handler_missing`；进度落 `jobs.result_json`、取消落 `audit_ops`
- 📌 **面板那条出片**：面板默认的任务号（`ui-…`）在 `tasks` 里没有行、而 `jobs.task_id` 有外键 ⇒ 挂不上队列（陷阱 #101），**故意留在进程内**（取舍写在 `services/render_job_service.py` 的模块说明里）；队列那条路的入队方是 §03.4.5 的「`voice/sentence` 全部完成 ⇒ `render/final`」（T2.6 之后）
- 📌 **"场景进度/换模板"随 C13 延后二期**；**`composite_hash` 整片缓存**已随 T3.4 落地，队列侧只做幂等键去重（一条任务只渲一次）

### T4.7 ⑥ 合成配置面板 · **P1** ✅ **已完成（2026-09-14）**
- 依赖：T3.2, T4.1 ｜ 里程碑：M4 ｜ 契约：§04.2.8 / **§04.5.9（新增）** / R17
- [x] **一期表单编辑**：合成 profile（分辨率/帧率/CRF/720P 保底档）
- [x] 水印（位置 / 边距 / 宽度 / 透明度）+ **PNG 存在性**显著提示（D5）
- [x] 字幕（字号 / 描边 / 每行字数）
- [x] 保存前强制 `validate`（不通过**拒绝保存**，**一个字节都不写**）
- [x] 改动写 `audit_ops` 且 `version +1`
- [x] 并发编辑 ⇒ `source_sha256` 比对（不符 ⇒ 409 `OUTPUTS_STALE`）
- ✅ 表单可改并保存；`validate` 不通过拒绝保存；改动留痕且版本递增
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/unit/core/test_yaml_lines.py tests/unit/core/test_outputs_store.py tests/integration/test_outputs_api.py -q` ⇒ **65 例**（17 + 24 + 24）
  - `cmd /c "cd /d %CD%\web && npm run test"` ⇒ **230 例**（11 文件，含 `outputs.test.ts` **32 例**）
  - `.\tasks.ps1 check` ⇒ **2080 passed / 32 skipped / 1 deselected**；`.\tasks.ps1 web:verify` 全绿 · dist **0.22 MB**
- **交付物**：`src/studio/core/yaml_lines.py`、`src/studio/core/files.py`、`src/studio/core/outputs_store.py`、`src/studio/services/outputs_service.py`、`src/studio/app/routers/outputs.py`、`src/studio/app/schemas/outputs.py`；`web/src/stores/outputs.ts`、`web/src/api/endpoints/outputs.ts`、`web/src/views/Outputs.vue`
- **施工裁定（本轮新增 166–169）**：
  - **166** `outputs.yaml` 写盘走**行级替换**（`core/yaml_lines.py`），**不用 `yaml.safe_dump` 整份重写** —— 这份文件每行都带理由（`# ★ 水印是必做项（D5）`），整份重写等于「点一次面板就永久毁掉可读性」（与裁定 139 同一取舍）
  - **167** 质量参数是**抽象字段** `quality`：按 `vcodec` 落到 `crf`（libx264）或 `cq`（NVENC）；翻译函数 `quality_field_of()` **公开**给服务层复用（抄第二份就会出现「面板说 CQ、写进 crf」）
  - **168** `load_outputs_config(path: Path)` 参数是**文件路径**（与 `load_persona_file` 一致、与 `load_pools_config` 不同）—— 测试与生产走**同一条**校验路径
  - **169** 并发编辑用 `source_sha256`，不符 ⇒ **409 `OUTPUTS_STALE`**（状态问题，不是入参问题），**一个字节都不写**
- ⚠️ **R17** 一期**只做表单编辑**；拖拽定位与三层模板树**随 C13 延后二期**
- ⚠️ 水印 PNG 不在盘上 ⇒ **跳过水印层继续出片**（D5 的硬门禁已被推翻，见 T3.2）⇒ 面板顶部一条黄条如实说明（不是红条：它没坏，只是少了层装饰）
- 📌 面板**不写 DB**：`config/outputs.yaml` 是唯一真相，消费方（T3.x 编译器 / T5.1 发布前校验）读的都是**文件**
- 📌 WS **不新增事件**：面板靠 `logs` 通道里 `source === "outputs"` 的帧刷新（§04.4.3 事件表是被契约测试解析的契约）；前端那个常量由 `test_web_contracts.py` 锁死与后端同源
- 📌 二期：层结构树 + 组件增删 + 拖拽定位 + 实时预览

### T4.8 ⑦ 素材库 · **P1** ✅ **已完成（2026-09-15）**
- 依赖：T3.1, T4.1 ｜ 里程碑：M4 ｜ 契约：§03.3.14 / **§03.3.21（新增）** / §4.3.1 / **§04.5.12（新增）**
- [x] 列表：跑酷素材 / 原声 / **BGM**（`license` / `use_count` / `last_used_at` / `has_text`）
- [x] **导入**（跑酷 / 原声 / **BGM**）⇒ 扫盘入库（`sha256` 指纹 / 缩略图 / 可用区间 / 时长 / 响度）
- [x] **BGM 入库后立即可被 T3.6 混音随机选中**（**Q12：无需改配置**）
- [x] 预览 / 试听（跑酷缩略图 + 原文件；BGM 试听；音色是**目录** ⇒ 不给播放器）
- [x] 标记启用/禁用（禁用 ⇒ 随机化不再选中；状态本来就对 ⇒ **幂等不留痕**）
  - ⚠️ **2026-09-17 补正**：这句验收此前**没有落地** —— `render/assets.py` 全文没有 `enabled` 这个词，渲染器只列目录。补的**不是校验模块**，是**接线**：调用方查库 ⇒ `disabled_assets()` 翻成**文件名集合** ⇒ `pick_*(exclude=)` 做一次集合减法（渲染器照旧不碰 DB、不查时长、不算 pHash）。真机验证：停用 1 条后 3000 次挑样 **0 次命中**（不排除时命中 50 次）
- [x] 统计：`clips ≥ 60 且 ≥ 30min`（判据线**现取**下发，前端不抄第二份）
  - ⚠️ **2026-09-17 补正**：判据此前只认**库里的家底**，而盘上 58 条一条没入库时它说的是"0 条"、`degraded=true`、横幅写"当前为黑屏降级模式" —— 而片子里正放着跑酷（**两个真相源**，陷阱 #150）。现在每节报**三组数字**（出片能挑到 / 盘上 / 已入库），`degraded` 收窄成"**一条都挑不到**"（"不够多"归 `shortfall`），未入库那批显式列出并给入库入口（陷阱 #151 / #152）
- [x] **R2 合规提示常驻**
- [x] 误删保护：**只允许禁用，不物理删除** + `audit_ops`
- ✅ 列表显示完整字段；三类素材均可扫盘入库；BGM 入库即被混音选中；禁用生效；统计达标；R2 提示常驻
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/unit/assets tests/unit/core/test_media.py tests/unit/core/test_files.py tests/unit/db/test_asset_repo.py tests/unit/services/test_asset_service.py tests/integration/test_assets_api.py -q` ⇒ **222 例**（76 + 51 + 8 + 21 + 40 + 26）
  - **2026-09-17 补正后**：`pytest tests/unit/assets tests/unit/render/test_assets.py tests/unit/services/test_asset_service.py tests/integration/test_assets_api.py -q` ⇒ **172 例**（38 + 38 + 19 + 51 + 26）；`pytest tests/integration/test_render_api.py -q` ⇒ **11 例**（新增 1 例：面板那条路真的把停用名单查出来传下去了）
  - `cmd /c "cd /d %CD%\web && npm run test"` ⇒ **284 例**（14 文件，含 `assets.test.ts` **18 例**）
  - `.\tasks.ps1 check` ⇒ **2468 passed / 32 skipped / 1 deselected**；`.\tasks.ps1 web:verify` 全绿 · dist **0.26 MB**（门禁 3 MB）
  - **2026-09-17 补正后**：`.\tasks.ps1 check` ⇒ **3591 passed / 32 skipped / 27 deselected**；`.\tasks.ps1 web:verify` ⇒ **380 passed**（18 文件，含 `assets.test.ts` **20 例**）· dist **0.32 MB**
- **交付物**：`src/studio/assets/{__init__,layout,validate}.py`、`src/studio/core/media.py`、`src/studio/core/files.py`、`src/studio/core/paths.py`、`src/studio/core/errors.py`、`src/studio/db/migrations/0009_assets.sql`、`src/studio/db/models.py`、`src/studio/db/repositories/asset_repo.py`、`src/studio/services/asset_service.py`、`src/studio/app/schemas/assets.py`、`src/studio/app/routers/assets.py`、`scripts/seed_placeholder_assets.py`；`web/src/api/endpoints/assets.ts`、`web/src/stores/assets.ts`、`web/src/views/Assets.vue`
- **施工裁定（本轮新增 187–196）**：
  - **187** `BROLL_MIN_USABLE_MS = 4500`：usable 区间只判「长度 > 0」是不够的 —— §04.2.4 的入点规则要「头 1.5s + 尾 1.5s + 一个 1.5s 候选窗口」，低于 4500 ms 的片段**入库时就拒收**，而不是等到渲染那一刻才报错（陷阱 #93）
  - **188** 授权**扫盘读不出来**，只能问人：已入库的行用**它自己那份** `license`（重扫不覆盖人的决定）；新条目用**本次请求**带的；没带就**不入库**并逐条报 `license_missing`。替用户填一个默认值 = 伪造 R2 留痕。音色是唯一例外（前置是 `profile.json` 来源登记，授权可 NULL）
  - **189** 扫盘与入库是**同一条代码路径的两个模式**（`dry_run` 开关）—— 分成两套实现，「预览说 60 条都能进、真入库只进 12 条」是迟早的事
  - **190** 发现与判据**分两层**：`assets/layout.py` 只回答「盘上有什么、它叫什么」（**不碰 DB**；命名不合规的进 `strays`，**不静默忽略、也不猜**），`assets/validate.py` 只回答「够不够格」（**不抛异常**，`problems`（拒收）与 `warnings`（能用但你该知道）分开）
  - **191** 条目是**按 kind 判别的联合**（`Field(discriminator="kind")`）：跑酷有 `has_text` / 可用区间，BGM 有 `loudness_lufs` / `bpm`，音色有 `ref_count` / `peak_db` —— 摊平成「全字段可空」的单一模型，前端就再也分不清「这个字段对这条素材没意义」与「它恰好是空的」。请求体一律 `extra="forbid"`
  - **192** `upsert` 返回 **`IngestAction` 四值**（`created` / `refreshed` / `unchanged` / `duplicate`）而不是 `bool`：一个布尔会让「扫了 60 条、0 条新增」与「扫了 60 条、60 条都是重复」在面板上长得一模一样。「内容已经在库里」的判定落在**仓储层**（`broll_clips.sha256` **刻意不加唯一索引** —— 同一段跑酷允许以不同 usable 区间再入库，那是产品决定）
  - **193** **重扫只刷机器事实**（陷阱 #92）：`upsert` 只更新 `sha256` / 时长 / 宽高 / 帧率 / 指纹，**绝不覆盖** `enabled` / `license` / `tags` / `usable_*` / `has_text` / `mood` / `bpm` —— 那些是人填的
  - **194** 判据线与授权枚举**跟着响应下发**（`thresholds` 从 `assets/validate.py`、`licenses` 从 `asset_service.LICENSES` **现取**）：某天把「跑酷 ≥ 60 条」调成 80，面板进度条跟着变，**前端一行都不用改**（与裁定 161 同一条）
  - **195** 扫描 / 启停**不新增 WS 事件**（与 T4.7 / T4.11 同一条）：入库与启停各写一行 `system_logs.source='assets'`，面板靠 `logs` 通道自己刷新（合并 300 ms）；`dry_run` 的预览**不记**（它什么都没干）
  - **196** 音色探测器结果**只写库、不回写资产目录**：`data/voice_src/<id>/` 是「人往里丢东西」的地方，机器算出来的状态混进去，下一次扫描就分不清「这是人写的还是我写的」。`voice_profiles.id` 就是目录名，且**不加外键**到 `tasks.tts_voice_id`（配音是异步的，目录被改名后历史任务仍要查得到它当时用的是谁）
  - **282** **面板与出片必须看到同一个真相**：出片挑素材走 `render/assets.py`（只列目录、不读库），而面板读库 —— 这两个真相**不会自己对上**，面板必须把盘上事实合进来并给出**一个与出片同口径的数**（`usable`）。三组数字分开报（出片能挑到 / 盘上 / 已入库），因为"它们对不上"本身就是用户要知道的事（陷阱 #150）
  - **283** `degraded` **只回答"出片会不会真的黑屏"**（`usable == 0`），不回答"素材够不够多"（那是 `shortfall`）。两者混在一起的后果是横幅写着"当前为黑屏降级模式"而片子里正放着跑酷 —— 同一句谎话换了个说法。"不够多"是**建议**，"一条都挑不到"才是**降级**
  - **284** **停用名单过线只传一样东西**：库 → 渲染器只流一串**文件名**（`DisabledAssets`），渲染器照旧不 import `db`、不认素材 id、不查时长 —— 补的是**接线**不是校验。用**文件名**而不是 id 或整条路径：库里存的是绝对路径（换台机器前缀就不同），能稳定对上的只有文件名。`produce_video(disabled=None)` ⇒ 退回"能进目录就算数"，CLI 与旧调用方不受影响（陷阱 #151）
  - **285** 同一条 `usable`，**按类别说不同的话**：跑酷/BGM 是"出片能挑到几条"，音色是"配音能用几个" —— 两条链路真的不同（出片挑素材只列目录；配音只认 `voice_profiles` 表），写成一句通用的话就会在音色那一节说一个反过来的谎（陷阱 #152）
- ⚠️ **E1 / E3 / E4 仍缺**：跑酷 / BGM / 原声都没到位 ⇒ 现用 `scripts/seed_placeholder_assets.py` 造的**占位素材**撑门禁（60 跑酷 × 32s = 32 分钟 + 3 BGM + 2 音色，实测 **74s** 跑完）；它们**一律带 `tags: ["placeholder"]` 并在面板标黄**，绝不假装成正式素材
- ⚠️ **E2 水印 PNG 缺失 ⇒ 跳过水印层**（不再是硬门禁）—— 与素材库无关，但属于同一批外部依赖
- 📌 一期**不做真 multipart 上传**（分片 / 断点续传 / 授权表单）：走「把文件丢进目录 + 点扫盘入库」；上传的收益只是「不用开资源管理器」，优先级低于把链路跑通
- 📌 一期**不做帧哈希 / 感知哈希**：`broll_clips.phash` / `frame_hashes` 列已留，填充留到 T3；素材库只负责「这条能不能用」，「不重复用同一段」是 T3 的随机化策略
- 📌 **音色目录契约**：`data/voice_src/<voice_id>/{ref_NN.<ext>, ref.txt, profile.json}`；参考音 **2–3 段**、每段 **10–30s**、采样率 **≥ 16 kHz**、峰值 **≤ −1.0 dBFS**；**0 段或 > 3 段 ⇒ 无效**；解析失败**明确报错、不回退默认音色**
- 📌 缩略图落 `data/assets/.thumbs`（`paths.thumb_file()`）；**没有就 404，不临时现抽**（面板刷新不该触发 ffmpeg）

### T4.9 ⑧ 实时日志 · **P1** ✅ **已完成（2026-09-14）**
- 依赖：T1.7, T4.1 ｜ 里程碑：M4 ｜ 契约：§04.5.2 / **§04.5.3** / §04.4.6
- [x] 分级 / 任务 / 来源过滤（**全在客户端做** ⇒ 切过滤不重连、已收行不消失）
- [x] 搜索（`message` + `source` 字面子串；`%` / `_` / `\` 转义后进 `LIKE ... ESCAPE '\'`）
- [x] `system.alert` 高亮且**不被限流丢弃**（独立订阅 `system` 通道，**不受级别过滤**）
- [x] 导出 NDJSON（服务端**流式**响应，每批 500 行，`limit` ≤ 200000）
- [x] 断线重连后**不丢不重**（`since_id` 补发 + 跳号 REST 补洞 + `lostIds` 如实记账）
- [x] `debug` 默认丢弃（**不落库**，而非"落库后不显示"）
- [x] 翻历史（`until_id` 窗口上界）+ 暂停（**丢显示不丢游标**）
- ✅ 验收命令（2026-09-14 实测全过）
  ```powershell
  .\tasks.ps1 check
  #   ruff format/check + mypy strict 全绿；pytest 1818 passed / 32 skipped / 1 deselected
  #   （含 tests/integration/test_logs_api.py 19 例 + test_web_contracts.py 告警码用例）
  .\tasks.ps1 web:verify
  #   npm run typecheck ⇒ 零错误
  #   npm run test      ⇒ 57 passed（5 文件：logs store 31 / highlight 5 / reconnect 8 / client 7 / http 6）
  #   npm run build     ⇒ 63 modules；dist/index.html 0.48 kB + index.css 10.49 kB + index.js 96.54 kB
  #   npm run size      ⇒ dist 共 0.10 MB（门禁 3.00 MB）OK
  ```
- 📦 交付物
  - 服务端：`src/studio/services/log_service.py`（`like_pattern()` + `source` / `search` / `recent(until_id=…)`）、`src/studio/app/routers/logs.py`（`GET /api/v1/logs` + **`GET /api/v1/logs/export`** 流式 NDJSON）、`src/studio/app/schemas/logs.py`、`src/studio/app/webcontracts.py`（`events.ts` 增 `ALERT_CODES` 8 值）
  - 前端：`web/src/stores/logs.ts`（客户端过滤 + 告警 + 补洞 + 翻历史 + 导出）、`web/src/views/Logs.vue`、`web/src/components/LogStream.vue`（告警高亮 + 搜索高亮，**逐段渲染不用 `v-html`**）、`web/src/utils/{download,highlight}.ts`、`web/src/api/{http,endpoints/logs}.ts`
  - 测试：`tests/integration/test_logs_api.py`（19 例）、`web/src/stores/logs.test.ts`（31 例）、`web/src/utils/highlight.test.ts`（5 例）
- 🔧 施工裁定（**已回灌 §04.5.3 / §05.7**）
  119. **过滤全在客户端做**（WS 订阅固定带 `minLevel: "debug"`）：过滤是**视图**不是**订阅**。放服务端会让"切一次过滤 = 重连一次"，已收到的行凭空消失，也没法在不改订阅的前提下叠加"只看告警"。
  120. **WS 有损、REST 补齐**：`log.appended` 的合并键退化成**任务级**（§04.4.6 既定取舍），同一任务 100ms 内多条只留最后一条。所以"不丢"由三段拼成 —— ① 连接期 WS → ② 重连带 `since_id` → ③ 跳号用 REST 补洞（冷却 1s + 单飞）。**不要**试图把 WS 改成无损，那等于取消合并。
  121. **补不回来的必须如实记账**：已被 GC 回收的 id 记进 `lostIds` 并**跳过游标**继续，不能因为一个洞永远卡在同一个 `since_id` 上（否则整个面板停摆）。
  122. **告警必须单独订阅 `system` 通道**：告警不在 `logs` 通道上（Hub 按 `is_alert_code` 分流，§04.4.3），只订 `logs` 会把每条告警的 id 当成缺口，补洞循环跑不停。
  123. **暂停时仍收告警**：`paused` 的语义是"别滚屏"，不是"别让我知道出事了"；`gapCount` 按**事件**计数（只在洞 0→>0 时 +1），不按行数。
- ⚠️ 陷阱 #12 高频日志拖慢前端 ⇒ 合并窗口 100ms + 环形缓冲 2000 条
- ⚠️ 陷阱 **#61** 搜 `50%` 把整张日志表捞回来 ⇒ `like_pattern()` 转义（**先转义 `\` 再转义 `%` `_`**）+ SQL 显式 `ESCAPE '\'`
- ⚠️ 陷阱 **#62** "导出到这条为止"被静默忽略 ⇒ `until_id` **必须进 SQL**，不能靠调用方事后截断
- 📌 **schema 变更不在本任务范围**：曾试加 `idx_logs_source`，牵动 15 处"对象数冻结"断言（`EXPECTED_INDEXES=58`）⇒ 已撤回，索引优化记入 **T4.12**

### T4.10 四池调度控制台 · **P1** ✅ **已完成（2026-09-14）**
- 依赖：T1.5, T4.2 ｜ 里程碑：M4 ｜ 契约：§03.4.4 / **§04.5.7**（新增）
- [x] 池状态 / 积压 / 优先级
- [x] 并发旋钮（写 `pool_settings` + `audit_ops`，**生效无需重启**）
- [x] 暂停 / 恢复（**在途跑完，不丢进度**）
- [x] 死信重投（`dead → pending` + `audit_ops`）
- [x] 并发上限硬编码（≤8；voice 池 ≤3）
- [x] **自动降级**：连续 `TTS_OOM` 达 `oom_threshold`（`config/pools.yaml`，默认 **2**）⇒ 自动降并发 + 告警
- ✅ 验收命令（2026-09-14 实测全过）
  ```powershell
  pytest tests/integration/test_pools_api.py -q
  #   29 passed（配置值 vs 运行值并排 / voice 上限 3 / 幂等不重复留痕 / 下调不杀在途
  #              / 死信逐条重投 / 连续 OOM 降级 + 告警 + 留痕 / 成功归零 / 下限仍告警 / YAML 开关）
  .\tasks.ps1 check
  #   ruff format/check + mypy strict 全绿；pytest 1953 passed / 32 skipped / 1 deselected
  .\tasks.ps1 web:verify
  #   npm run typecheck ⇒ 零错误
  #   npm run test      ⇒ 172 passed（9 文件：pools store 19 + overview 20 + topics 39 + scripts 37
  #                       / logs 31 / reconnect 8 / client 7 / http 6 / highlight 5）
  #   npm run size      ⇒ dist 共 0.18 MB（门禁 3.00 MB）OK
  ```
- 📦 交付物
  - 服务端：`src/studio/services/pool_service.py`（`read()` / `set_concurrency()` / `requeue_dead()`）、`src/studio/app/routers/pools.py`（`GET /api/v1/pools` + `POST /api/v1/pools/concurrency` + `POST /api/v1/pools/requeue`）、`src/studio/app/schemas/pools.py`、`src/studio/app/schemas/common.py`（`MAX_REASON` / `clean_reason` 两面板共用）、`src/studio/db/queue.py`（`AUTO_ACTOR` / `AUTODEGRADE_CODES` / `PoolRuntime.consecutive_oom` / `set_concurrency()` / `_maybe_autodegrade()`）、`src/studio/core/config.py`（`POOL_CONCURRENCY_MIN` / `POOL_CONCURRENCY_MAX` / `concurrency_bounds()` / `load_pools_config()`）、`src/studio/core/errors.py`（`POOL_CONCURRENCY_LIMIT`）
  - 迁移：`src/studio/db/migrations/0007_pool_autodegrade.sql`（`pool_settings.consecutive_oom`）
  - 前端：`web/src/views/Pools.vue`、`web/src/stores/pools.ts`（**复用** overview 的 `poolLabel` / `describeError` / `isPoolEvent` / `createCoalescer`）、`web/src/api/endpoints/pools.ts`、`web/src/App.vue`（`pools` 视图）、`web/src/stores/ui.ts`（面板 `ready`）
  - 测试：`tests/integration/test_pools_api.py`（29 例）、`web/src/stores/pools.test.ts`（19 例）
- 🔧 施工裁定（**已回灌 §04.5.7 / §05.7**）
  144. **暂停/恢复不在四池控制台另开写入口**：复用 `POST /api/v1/overview/pools`（T4.2 裁定 138）。同一个动作两个写入口 ⇒ 必然两套校验、两套留痕；四池面板是它的**第二个视图**，不是第二个 API。
  145. **并发下限 = 1**：`0` 是「沉默的暂停」，与 `paused=1` 在面板上分不出来，而且绕过了暂停那条路的留痕与「在途跑完」语义（陷阱 #79）。
  146. **自动降级必须与 job 状态变更同事务**：写进 `fail()` / `succeed()` 的**同一个 `BEGIN IMMEDIATE`**（陷阱 #81）。分成两个事务时，重排窗口里恰好发生的 OOM 会被丢掉，计数器漏加。
  147. **判据用 `== threshold` 而不是 `>=`**（陷阱 #80）：`>=` 会让「阈值 2、连来 5 次 OOM」连降 4 次；`==` 配合 `succeed()` 归零，语义变成「池又成功过一次之后才可能再次越线」。
  148. **`consecutive_oom` 落 `pool_settings`**（迁移 `0007`）：`jobs` 分不出「连续 2 次 OOM」与「一条作业 OOM 两次」；`system_logs` 有保留期，拿它当业务状态等于把规则建立在「日志还没被 GC」上。
  149. **`JobStore.__init__` 注入 `AutoConcurrencyConfig`**：事务内禁止 I/O（§03.4.6 规则 3），阈值必须在进事务之前拿到。
  150. **`load_pools_config()` 只读 `pools.yaml`**，不走 `load_config()`：worker 与 API 两个进程都要读它，路径与失败语义必须显式；读失败 ⇒ 配置按 `None` 处理（面板降级显示），**不 500**。
  151. **已在并发下限时「降不动」也要告警**：静默失败比降级本身更糟 —— 人以为系统处理了，实际并发一点没变。
  152. **`pool_settings` 加一列 `consecutive_oom`**（迁移 `0007_pool_autodegrade.sql`）；`EXPECTED_TABLES=29` / `EXPECTED_INDEXES=58` / `EXPECTED_TRIGGERS=6` **均未变**。
  153. **人工重投死信不清零** `consecutive_oom`：计数器量的是「显存装不装得下」，与某条作业的进度无关。
  154. **T4.10 REST 面 = 三个端点**（读 / 并发 / 重投）；暂停恢复复用 T4.2 那一个 ⇒ 合计仍是 3 个新端点。
  155. **`MAX_REASON` / `clean_reason` 抽到 `schemas/common.py`**：总览台与四池面板共用一份上限，避免两处各写一份然后慢慢漂移。
  156. **池名 / 中文标签 / `describeError` / `isPoolEvent` / `createCoalescer` 从 `stores/overview.ts` 复用**，不抄第三份（第四份在 `logs.ts`）。
  157. **`config_concurrency` 与 `concurrency` 并排给**：`pools.yaml` 是出厂值、`pool_settings` 是运行值，两个真相都摆在卡片上，比「显示一个、心里想另一个」体面。当前取值 draft 2 / voice 1 / render 1 / publish 1（与 DB seed 同值）。
- ⚠️ 并发调太高 ⇒ 上限硬编码（`POOL_CONCURRENCY_MAX`：draft/render/publish 8、**voice 3**）+ 面板影响提示
- ⚠️ 自动降级抖动 ⇒ `== threshold` 判据 + **降级后需人工确认恢复**（`recover_after_min` / `gpu_mem_high_ratio` 留在 `pools.yaml`，T4.11 接自动恢复）
- ⚠️ 陷阱 **#79** 并发降到 0 = 沉默的暂停 ⇒ 下限硬编码 1
- ⚠️ 陷阱 **#80** `>=` 判据连降 ⇒ `== threshold` + 成功归零
- ⚠️ 陷阱 **#81** 降级与 job 状态分两个事务 ⇒ 同事务写

### T4.11 无人值守编排与故障自愈 · **P0** ✅ **已完成（2026-09-14）**
- 依赖：T4.10, T2.8 ｜ 里程碑：M4 ｜ 契约：§01.2.1 / **§04.5.10（新增）** / §03.7.5
- [x] supervisor 守护 5 进程
- [x] 崩溃重启（指数退避 + 重启次数上限，超限停止并告警）
- [x] 磁盘/显存水位门禁：`free_D < 15GB` ⇒ 暂停 render/publish 认领 + `DISK_LOW`
- [x] 自动重试与死信告警
- [x] **人工池**：`attempt_count ≥ 3` ⇒ `manual_pool` 且总览台可见
- [x] 显存回落 ⇒ 并发自动回升（`pool.autorecover`，把 T4.10 留的那半句补上）
- ✅ 判死 → 重启 → 指数退避 → 上限停手 → **未就绪不硬拉**（裁定 103）；磁盘水位触发暂停 / 放开；人工池在总览台可见（假编排器全覆盖，用例**不碰真进程**）
- ⏳ **真机「杀一个 worker ⇒ 15s 内被重启」与「连续运行 24h 无人工干预」：长跑验收未做** —— 随 T4.12 的观测面（指标面板 + 备份 + 运维剧本）就位后单独进行；本轮可交付的证据是 34 例后端 + 235 例前端用例与 `check` / `web:verify` 全绿
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/unit/services/test_watchdog_service.py tests/integration/test_watchdog_api.py -q` ⇒ **34 例**（27 + 7）
  - `cmd /c "cd /d %CD%\web && npm run test"` ⇒ **235 例**（11 文件，含 `overview.test.ts` **25 例**）
  - `.\tasks.ps1 check` ⇒ **2115 passed / 32 skipped / 1 deselected**；`.\tasks.ps1 web:verify` 全绿 · dist **0.22 MB**
- **交付物**：`src/studio/services/watchdog_service.py`、`src/studio/app/watchdog.py`、`src/studio/app/routers/watchdog.py`、`src/studio/app/schemas/watchdog.py`；`web/src/api/endpoints/watchdog.ts`、`web/src/stores/overview.ts`、`web/src/views/Overview.vue`
- **施工裁定（本轮新增 170–178）**：
  - **170** 人工触发一轮是 `POST /api/v1/watchdog/tick`，**不是 `GET /tick`** —— 它会真的拉起进程、暂停池、把任务推进人工池；GET 会被浏览器预取 / 链接预览 / 误粘贴的地址栏触发
  - **171** 路由是 `def` 而不是 `async def`：`tick()` 阻塞（等就绪 / 读进程表 / 写库），跑在 Starlette 线程池里不占事件循环，否则一次重启探测会把 `/ws/ui` 推送卡住
  - **172** 守护**守不了自己**：跑在 API 进程里的守护恒不重启 `api`，如实标 `guarded=false` + `detail` 指向 `ops/start_all.ps1`（给它画一个"守护中"的绿灯等于撒谎）
  - **173** **没数据 ≠ 正常**：`disk_gate.low=null`（还没采过）与 `false`（水位正常）必须分开画；`services=[]` + `last_tick_at=null` 表示"一轮都没跑过"，不是"全都健康"
  - **174** `enabled`（配置）与 `runnable`（`enabled && workers/ 在`）**分开报**：临时家目录里周期泵不起跳 ⇒ 守护不会在后台悄悄改 `tasks` / `jobs`（守护类 flaky 的来源就这一条）
  - **175** 守护**停用也照跑照留痕**：`pools.yaml` 读不到 ⇒ `enabled=false` + `detail`，`POST /tick` 仍回 200 + `note` 并写 `audit_ops`（审计要答的是"谁在什么时候动过这台机器"）
  - **176** 进人工池时**错误码原样带过去**：`manual_pool` 本身是错误态（`_ERROR_STATUSES`），"离开失败态就清零"那条规矩管的是"重新跑"，不是"停在这里等人"；面板必须同时给出 `error_code` / `stage_detail` / `retry_from`
  - **177** **自动重启不写 `audit_ops`**（每 5s 一拍的高频动作会把永久留痕表刷满）：证据链在 `system_logs`（`SERVICE_RESTARTED`，有保留期）+ 进程表；审计表留给**人的决定**与**稀疏的状态变更**
  - **178** `audit_ops.at` 与 `pool_settings.updated_at` **取同一个时刻**（`COALESCE(?, strftime('now'))`）：`set_concurrency(now=)` 给的时刻要一路带进留痕，否则并发回升的冷却锚点按**另一个时钟**算冷却（这条是**修 bug**：锚点读到的是"现在"，不是"降级那一刻"）
- ⚠️ 重启风暴 ⇒ 指数退避（5s → … → 300s）+ 窗口内上限 20 次，超限**停手 + 告警**（不无限重试）
- ⚠️ 水位误判 ⇒ 阈值可配（`pools.yaml → watchdog`）+ **手动覆盖**（判据落在留痕上：相邻两条 `[pool.pause(auto), pool.resume(人工)]`；水位恢复时 `pool.override_cleared` **自清**，否则覆盖成了永久豁免）
- ⚠️ **人工池必须可见**，否则违背 P4
- 📌 未就绪的进程**不硬拉**（裁定 103）：`readiness` 不过 ⇒ 记 `degraded`，不静默起一个空转 worker
- 📌 守护**不做**的事：阈值热重载（改 `pools.yaml` 仍需重启 API）、重启风暴的自动解封（超限停手后由人重启守护）

### T4.12 观测、备份与交付 · **P1** ✅ **已完成（2026-09-14）**
- 依赖：T4.11 ｜ 里程碑：M4 ｜ 契约：§03.7.4 / §03.7.5 / **§04.5.11（新增）**
- [x] 指标面板（观测面板：备份新鲜度 + 存储体检 + 四池 / 产量 / 资源 / 进程）
- [x] 每日 DB 备份 `scripts/backup_db.ps1`（热备 `VACUUM INTO` + 7 日备 / 4 周备轮转）
- [x] **恢复演练** `scripts/restore_db.ps1`（还原到临时库 ⇒ `db check` ⇒ 抽查 3 张表行数）
- [x] 媒资 GC（`studio gc run` + `scripts/gc_media.ps1`，按 TTL）+ **GC 白名单**（`final/`/`output/`/`covers/` **永不清理**）
- [x] 运维手册 **6 个应急剧本 + 导航**（`docs/runbook/` 共 7 篇：`README` 速查 + 恢复演练记录表 / 库损坏 / 盘满 / TTS OOM / 重启风暴 / 流水线卡住）
- [x] **操作审计页**（按任务 / 操作人 / 动作 / 对象 / 结果筛选 + 分页 + `before`/`after`）
- [x] **T4.9 转来的索引债**：`0008_observability`（`idx_logs_source` + `idx_events_created`，对象数冻结 **58 → 60**）
- ✅ 日备产出；恢复演练通过（**真机 `exit 0`**）；GC 按 TTL 清理且**不误删成片**；剧本齐备；审计页可筛选
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/unit/db/test_backup.py tests/integration/test_backup_cli.py tests/unit/gc tests/integration/test_gc_cli.py tests/integration/test_audit_api.py tests/integration/test_metrics_api.py -q` ⇒ **130 例**（32 + 10 + 57 + 11 + 12 + 8）
  - `.\scripts\restore_db.ps1 -Force` ⇒ **exit 0**（面板：活库基线失败项 **0** / 备份版本落后 `db.indexes` / 备份自己的问题 **0**）；`studio db check` ⇒ 8 项全 ok（29 表 / 60 索引 / 6 触发器 / 8 迁移）
  - `cmd /c "cd /d %CD%\web && npm run test"` ⇒ **263 例**（13 文件，含 `audit.test.ts` **15 例** + `metrics.test.ts` **13 例**）
  - `.\tasks.ps1 check` ⇒ **2245 passed / 32 skipped / 1 deselected**；`.\tasks.ps1 web:verify` 全绿 · dist **0.24 MB**
- **交付物**：`src/studio/db/backup.py`、`src/studio/db/migrations/0008_observability.sql`、`src/studio/gc/{policy,rows,media,runner}.py`、`src/studio/services/observability_service.py`、`src/studio/app/routers/{audit,metrics}.py`、`src/studio/app/schemas/{audit,metrics}.py`、`docs/runbook/`（7 篇）；`scripts/{backup_db,restore_db,gc_media}.ps1`；前端 `web/src/api/endpoints/{audit,metrics}.ts` + `web/src/stores/{audit,metrics}.ts` + `web/src/views/{Audit,Metrics}.vue`
- **施工裁定（本轮新增 179–186）**：
  - **179** 备份新鲜度按**文件写入时刻**算（`BackupFile.written_at` = `stat().st_mtime`），不按文件名里那个日期的零点 —— 日备 03:00 产出，按零点算上午会显示「11 小时前」，一个刚跑完的任务被报成快半天没动，这面板第一行就没人再信
  - **180** **一份都没有也是 stale**（`age_hours=null` + `stale=true`）：把「没有备份」画成「0 小时前」是这块面板最容易犯的谎
  - **181** 恢复演练的 `schema_lag` 判据**双条件**（多出的失败项全落在 `db.indexes`/`db.tables`/`db.triggers`/`db.migrations` **且**还原库迁移集是活库的真前缀）：刚上过迁移的库不该把好备份判死，真删了索引的坏库**照旧报红**
  - **182** 删除动作**只有一个闸口**（`gc/policy.py::guard_path()`）：越界 ⇒ `GC_PATH_OUT_OF_BOUNDS`、白名单 ⇒ `GC_REFUSED_PROTECTED`（CLI `exit 1`）；成片不在 ⇒ `skip`，**绝不因为「找不到母带」去删别的目录**
  - **183** `audit_ops` **无保留规则**（永久）：它是「谁动过这台机器」的唯一账本；GC 的行级规则只覆盖 `system_logs` / `llm_calls` / `task_events`
  - **184** 观测面**只读**（`POST /api/v1/metrics` ⇒ **405**）：GC / 备份 / VACUUM 都在 CLI 与计划任务里，面板上给按钮等于给「删数据」开一个只隔一次点击的入口
  - **185** 观测面板**不轮询**、审计筛选**不自动查**：前者是「要查的时候查一下」（存储体检要递归数文件），后者每敲一个字就是一次数据库查询；翻页**取消在途请求**（后发先至的旧响应不许把列表画回去）
  - **186** `db_freelist_bytes` 由 `db/engine.py::footprint()` **一处算**（GC 报告 / `db vacuum` / 观测面板同一个数）：三份实现迟早让「面板说 0、命令说 120 MB」
- ⚠️ **R11** 媒资无限增长 ⇒ 保留策略 + GC + 水位门禁
- ⚠️ 备份占盘 ⇒ 7 日备 + 4 周备，**计入水位预算**
- ⚠️ **VACUUM 要 2 倍空间 + 独占写** ⇒ `studio db vacuum` 先钉 `prevacuum_*.db` 检查点，失败即中止
- 📌 T4.11 留下的 **24h 长跑 / 真机杀进程**验收：观测面（指标面板 + 备份 + 剧本）本轮已就位 ⇒ 可单独进行
- 📌 本轮顺手修好的真机状态：`data/studio.db` 只迁到 `0006` ⇒ 已 `db migrate` 到 **0008**（自动钉了 `data/backups/checkpoints/premigrate_*.db`）

### T4.13 ⑨ 人物库面板（persona 编辑 / 切换 / 回滚）· **P1** · ✅ **已完成（2026-09-14）**
- 依赖：T1.2+ ｜ 里程碑：M4 ｜ 契约：§02.4 / §04.1.6 / §04.5.2 / **§04.5.8（新增）**
- [x] 展示当前激活人物（来源 / 版本 / `sha256` / 加载时刻 / `last_error` / **`stale`**）
- [x] **人物库列表**：`id` / 名称 / 口吻 / 受众 / 有效性（无效条目直接显示原因）
- [x] 表单编辑（人设 / 口吻 / 受众 / 口癖 / 禁区 / 篇幅 / 时长上限）⇒ 保存前强制 `validate`（不通过**拒绝保存**）
- [x] **一键切换** `use <id>`（旧版自动备份到 `data/backups/persona/`）+ **另存为** `save-as <id>`
- [x] 切换 / 编辑 ⇒ 广播 `system.persona_changed`，**无需重启任何进程**（热重载）
- [x] 影响提示：**只影响后续稿件**，已入队任务不中断、不重写
- [x] **备份回滚**（§04.5.8）：`data/backups/persona/` 最近 20 份列在面板上，一键退回
- ✅ 面板可改 / 可切 / 可回滚；`validate` 不通过拒绝保存；改完后续任务立即生效且在跑任务不中断
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/integration/test_persona_api.py tests/unit/core/test_persona_store.py -q` ⇒ **109 例**（35 + 74）
  - `cmd /c "cd /d %CD%\web && npm run test"` ⇒ **198 例**（10 文件，含 `persona.test.ts` **26 例**）
  - `.\tasks.ps1 check` ⇒ **2014 passed / 32 skipped / 1 deselected**；`.\tasks.ps1 web:verify` 全绿 · dist **0.20 MB**
- **交付物**：`src/studio/services/persona_service.py`、`src/studio/app/routers/persona.py`、`src/studio/app/schemas/persona.py`、`src/studio/app/persona_events.py`；`web/src/stores/persona.ts`、`web/src/api/endpoints/persona.ts`、`web/src/views/Personas.vue`
- **施工裁定（本轮新增 158–165）**：
  - **158** `AppState.persona` 由 `paths` **现造**，不用 `get_persona_store()` 进程单例 —— `build_state(paths=tmp)` 必须自洽，否则测试里改人物会改到真仓库的 `config/persona.yaml`
  - **159** 事件广播挂在 `PersonaStore.subscribe()` 上，**不在写入口顺手声明** —— 改人物有三条路径（面板 / CLI / 手改 YAML），只有第一条走我们的写入口；`initial` 不广播（那一刻什么都没变）
  - **160** `PERSONA_ID_PATTERN` 公开给 REST 复用；备份文件名走**白名单正则**（`..` / 斜杠一个都进不来）
  - **161** 表单上下限从 `PersonaConfig.model_fields` **现取**（`persona_limits()`），前端不抄第二份
  - **162** WS 事件 / 刷新**不覆盖脏草稿**，只立 `draftStale` 旗子 —— 否则一条 `system.persona_changed` 会把正在写的半屏字抹掉
  - **163** `PersonaService` 把 store 的通用配置码翻译成人物码（`CONFIG_MISSING`→404 / `CONFIG_INVALID`→422 / `save_as` 冲突→409），**不改 store 的既有语义**
  - **164** 备份同秒撞名退到 `-<n>`，且 `rollback()` **先把内容读进内存再备份当前**
  - **165** `update` / `rollback` 只改**激活文件**；改人物库条目得用 `save-as --overwrite`
- ⚠️ **E7 唯一人工必填** ⇒ 面板须常驻提示"禁区词会经规则通道全量扫描"
- ⚠️ 多进程无 IPC ⇒ 面板显示的是**本进程**已发现的状态；切换后以"下次 `current()` 生效"为准
- 📌 复用 `PersonaStore.subscribe()`（T1.2+ 已实现）；WS 事件表已补 `system.persona_changed`（§04.5.2）
- 📌 T5.7 报告"采纳建议写回 persona"**必须走 `PersonaStore`**（校验 + 备份），禁止直接改文件

> **>>> M4 门禁**：≥3 篇同时推进；中断后恢复；连续 24h 无人干预；全程网页操作。

---
## 5. 阶段 T5 · 发布 + 定时 + 报告 + 数据回流（8 任务 → 门禁 M5）

> **默认关闭**（`publish.enabled=false`）：先跑通 T1–T4 验证出片质量，人工确认后再开启（R14）。

### T5.1 快速封面 + 发布前二次校验 · **P1** ✅ **已完成（2026-09-16）**
- 依赖：T3.7 ｜ 里程碑：M5 ｜ 契约：§04.1.8 / §06.3 / §06.4
- [x] Cover Agent 封面文案 + 1080×1920 封面图生成
- [x] 发布前**重跑三道门禁**：水印校验 / 响度（`audio_qc`）/ 相似度（`dup_audit`）
- [x] 封面与文案**禁区扫描**
- [x] 审计不通过或命中禁区 ⇒ **拒绝发布**并转 `manual_required`（**不静默放行**）
- [x] `av_sync` **仅诊断**（C12：不阻断发布）
- ✅ `studio publish cover --task <id>` 产出 1080×1920 封面；审计不通过 ⇒ 拒绝发布并转人工；水印缺失 ⇒ 拒绝发布
- ⚠️ **R14 发布不可逆** ⇒ 默认关闭 + `require_confirm=true` + 三道门禁
- ⚠️ 陷阱 #27 成片没水印 ⇒ 发布前**再校验一次**（不是读当初的结论：成片可能在盘上被动过）
- ⚠️ **本任务只做"能不能发"，一个字节都不往平台送**：`publish.enabled=false` 是出厂状态，真正点下"发布"是 T5.2/T5.3
- **验收命令**：
  - `studio publish cover --task 01M2M9THZ1M83CT5EJH789ZVZA --no-agent` ⇒ 封面 `data/output/covers/20260916-193022_…_cover.jpg`（1080×1920，中文主文案 + 描边 + 压暗带；抽帧点 500ms）
  - `studio publish precheck --task 01M2M9THZ1M83CT5EJH789ZVZA` ⇒ 退出码 **1**，`error_code=PRECHECK_WATERMARK`（**真机拦住了**：本机没水印 PNG + 成片实测 -16.69 LUFS 超 `[-16.5,-15.5]` 窗口）
  - `.\tasks.ps1 check` ⇒ **3276 passed / 32 skipped / 12 deselected**（比 T4.14 多 **175 例**）；`ruff format` + `ruff check` + Web 契约 + `mypy`（**308 source files**）全绿
- **交付物**：
  - 纯函数层 `src/studio/domain/cover.py`（换行 / 高亮分段 / 字号收缩 / 安全区 / 规则兜底文案）
  - 合成层 `src/studio/publish/cover.py`（抽帧 + `bbox` 量宽 + 单条滤镜链 + 原子落盘 + 两级降级）
  - 平台事实 `src/studio/core/fonts.py`（`system_font_dirs` 从 `render/subtitle.py` 下沉，顺带修掉 `publish → render` 的分层违规；裁定 261）
  - 校验层 `src/studio/publish/precheck.py`（三道门禁 + 禁区扫描 + 诊断）
  - Agent `src/studio/agents/cover.py`；服务 `src/studio/services/publish_service.py`；CLI `studio publish cover|precheck`
  - 契约 `prompts/cover/{system.md,user.jinja}` + `prompts/manifest.yaml` 的 `cover` 条目 + `schemas/cover_result.schema.json` + `prompts/shared/banned_words.yaml`（30 词）
  - 测试 **175 例**：`tests/unit/domain/test_cover.py`（37）· `tests/unit/publish/test_cover.py`（36）· `tests/unit/publish/test_precheck.py`（38）· `tests/unit/agents/test_cover.py`（16）· `tests/unit/services/test_publish_service.py`（25）· `tests/contract/test_cover_schema.py`（23）
- **施工裁定（本轮新增 255–260）**：
  - **255** 封面**分三层**：`domain/cover.py`（写什么字、怎么摆 —— 纯函数，可单测）⇒ `publish/cover.py`（把字画上去 —— ffmpeg 子进程）⇒ `agents/cover.py`（只产文案）。揉在一起的话，"改一次排版要重跑一次 LLM"，而"文案命中禁区"也没法在**不画图**的前提下判
  - **256** 抽帧点由**模型挑**（§06.3 第 4 条），`resolve_frame_at_ms` 算出来的那个只是**提示词里的默认建议**；但无论谁给的都要**钳在 `[0, duration_ms-1]`**（`-ss` 落在片尾会解不出帧 ⇒ 整张封面降级成纯色底）
  - **257** Cover Agent 判出禁区命中 ⇒ **不重试**，把 `ok` 置假交回调用方**退规则兜底**（与 Writer 的禁区处理同一条：改一处、犯另一处是重试的常见结局）；`payload.forbidden` 为空时**退回 `ctx.persona.forbidden`** —— 提示词里注入的就是那一份，事后扫的若与它不是同一份，就会出现"提示词说了不许写、事后却没判"的错位，而那种错位没人看得出来
  - **258** `tasks.context_json` 的封面回填是**合并**写，与 `quality_json` 的整体覆盖**相反**：`context_json` 里躺着互不相干的几件事（成片路径 `final_path`、这次封面），整体覆盖会把成片路径抹掉 —— 那是静默的数据损坏。封面**失败也写**（`cover_path=null`）："没试过"与"试了没成"在面板上是两件事
  - **259** `GateResult` 的两个字段**不许揉**：`passed` 报**审计结论本身**，`blocking` 报**要不要拦**。写成 `dup_audit_pass or not block_on_similarity` 会让"审了没过"变成 `passed=True` ⇒ `run_precheck` 那条"没过就 warn"的循环**一句提示都不出**（真机踩过，陷阱 #133）。响度那一项同理**重量盘上那个文件**，量不出来判**不过**（不能给"根本没检查"发合格证）
  - **261** 分层违规**在模块级修，不在函数里躲**：`publish/cover.py` 原先在函数内 `from studio.render.subtitle import system_font_dirs`（配 `noqa: PLC0415`），看着「没依赖」，实则 §02.1 那条「`publish/` 不得 import `render/`」已经破了。修法是把 `system_font_dirs` 下沉成 `core/fonts.py`（它只依赖 `sys`/`os`/`pathlib`，是**平台事实**而不是渲染决策），字幕与封面各自从 `core/` 取，`render/subtitle.py` 保留 re-export 不破坏既有调用方。`publish/precheck.py` 那处**量成片响度**是唯一真例外（门禁与 `quality_json` 必须同源），已写进 §02.1 明列
  - **260** 封面文字区背后加一条**压暗带**（`drawbox=black@0.45`，按文字块高度而不是铺满全屏）：底片是别人的画面，可能是雪地、白墙、或者本身已经烧了字幕 —— 白描边字压在这三种底上都读不清，而用户看到的只是"这张封面好糊"
- ⚠️ 新增陷阱 5 条已并入 §10（编号 130–134）
### T5.2 发布适配层与 profile · **P1** ✅ **已完成（2026-09-16）**
- 依赖：T5.1 ｜ 里程碑：M5 ｜ 契约：§04.6.1 / §06.2 / §06.5
- [x] `Publisher` ABC（平台实现可替换）
- [x] 一线三平台实现：**抖音 / 快手 / 视频号**（Q9：二线留接口）
- [x] 选择器集中到 `publish/selectors/*.yaml` + `selectors_version` 留痕
- [x] 失败截图 + DOM 快照留档
- [x] 登录态 `health()` 探测：失效 ⇒ 转 `manual_required` + 告警（**不自动登录、不绕过验证码**）
- [x] 发布后**回读标题与文案逐字比对**，不一致 ⇒ 重填 ≤2 次
- [x] `studio publish dry-run --task <id> --platform douyin`
- ✅ `pytest tests/contract/test_publisher_abc.py`；`dry-run` 走完流程到"确认发布"前一步并截图；`health()` 能正确报告"未登录"与"登录态已过期"
- ⚠️ 陷阱 #23 平台改版 ⇒ 选择器集中配置便于热修
- ⚠️ 陷阱 #21 登录态失效被静默跳过 ⇒ 探测 + 转人工
- ⚠️ 陷阱 #24 文案被编辑器吞掉 ⇒ 回读比对
- ⚠️ **R18** 多平台工作量 ⇒ 分层实施，一期只做一线
- ⚠️ **本任务仍不发布**：`publish.enabled=false` 是出厂状态；`--target fixture` 那个发布器**自己就拒绝** `dry_run=False`（纵深防御）
- **验收命令**（两条都真机跑过）：
  - `studio publish dry-run --task 01M2M9THZ1M83CT5EJH789ZVZA --platform douyin --target fixture` ⇒ 退出码 **0**，截图 `data/work/<task>/publish/fixture/…_06-before-publish.png`（**肉眼确认**：文件选中 / 标题与文案填对 / 封面选上 / 没有结果块）；靶页服务器**一次都没收到 POST /__published**
  - 同一条加 `--probe "?logged_out=1"` ⇒ 退出码 **1**、`尚未登录，需人工扫码登录`；再加 `&expired=1` ⇒ `登录态已过期，需人工重新扫码登录`
  - `--platform douyin`（live，不带 `--target`）⇒ **真打开抖音创作页**，如实报 `尚未登录，需人工扫码登录`（R13：不自动登录）
  - `.\tasks.ps1 check` ⇒ **3471 passed / 32 skipped / 27 deselected**（比 T5.1 多 **195 例**）；`ruff format` + `ruff check` + Web 契约 + `mypy`（**334 source files**）全绿
  - `pytest tests/integration/test_publish_dryrun.py` ⇒ **15 passed**（带 `e2e`+`slow`，不进 `check`；**真 Playwright 打本地 http 靶页**）
- **交付物**：
  - 抽象层 `src/studio/publish/base.py`（`Publisher` ABC + `PublisherContext` / `PublishRequest` / `PublishResult` / `PublishEvidence` / `PublishHealth` / `PublishMetrics` / `PublishStatus` / `PUBLISHERS` 注册表）
  - 通用实现 `src/studio/publish/playwright_publisher.py`（§06.5.3 八步）+ `browser.py`（`PageLike` Protocol + 持久化 profile 会话）
  - 选择器 `src/studio/publish/selectors.py` + `selectors/{douyin,kuaishou,shipinhao,fixture}.yaml`（**装配期**校验）
  - 平台 `platforms/{douyin,kuaishou,shipinhao}.py`（一线真实现）· `platforms/{xiaohongshu,bilibili,xigua,weibo}.py`（二线空实现）· `platforms/fixture.py`
  - 演练靶 `publish/fixtures/upload_form.html`（`?logged_out=1` / `?expired=1` / `?eat=emoji` / `?eat=newlines` / `?reject=1`）
  - 纯函数 `src/studio/domain/publish.py`（幂等键 / 文案裁剪 / 话题拼装 / 回读比对）
  - 服务与 CLI `services/publish_service.py` 的 `dry_run` + `studio publish dry-run`
  - 测试 **195 例**（进 `check`）：`tests/unit/domain/test_publish.py`（46）· `tests/unit/publish/test_selectors.py`（40）· `tests/unit/publish/test_playwright_publisher.py`（43）· `tests/unit/publish/test_base.py`（34）· `tests/contract/test_publisher_abc.py`（32）；另 **15 例**集成（`tests/integration/test_publish_dryrun.py`，`e2e`+`slow`）
- **施工裁定（本轮新增 262–267）**：
  - **262** `Publisher` 的**构造函数进基类**：§4.6.1 规定的是三个**抽象方法**，但 `PUBLISHERS` 里存的是**类**，服务层要 `get_publisher(code)(ctx)` 直接造实例。签名不写在基类上，"每个实现都收同一个上下文"就只是口头约定（mypy 只能靠 `cast` 蒙混），而漏掉一个参数要等真机才发现（陷阱 #138）。`_ctx` 由基类持有，子类 `super().__init__(ctx)`
  - **263** 选择器校验放在**装配期**（`load_selector_pack`）而不是点击时：缺项 / 版本 / readback 取值错 ⇒ 当场 `PUBLISH_SELECTOR_MISS`。放在点击时的话，"yaml 写错了"与"平台改版了"在日志里长得一样，而处置完全不同（改配置 vs 重新校准）
  - **264** dry-run **不看** `publish.enabled`：它的全部意义就是"在开关还关着的时候验证链路是通的"。要求先打开才能演练 = 让人拿**真发布**当验证手段（正是 R14 要防的）。真正的保护是 `PublishRequest(dry_run=True)` + 靶页服务器那条"一次都没收到 POST"
  - **265** `ReadbackDiff.at` 对外是 **1 基**、`empty` 不给偏移：给操作员看的行号要与编辑器一致；`empty` 是"整段都没了"，硬凑一个 0 会让人去找第 1 个字符
  - **266** `TagPlan.dropped` 统一成**带语法**的形态：混着原始名与带 `#` 的形态时，"哪几个话题被丢了"在不同平台上写法不同，面板与日志各显示一半
  - **267** `_dry_run_account` 里 `fixture` 目标**现造**账号（`_fixture`，profile 落 `data/browser_profile/_fixture/`）：为了演练在配置里加一个假账号，会让配置里出现一条**永远发不出去**的东西；而真账号的 profile 必须与演练**物理隔离**（不然演练会把真 cookie 弄脏）
- ⚠️ 新增陷阱 5 条已并入 §10（编号 135–139）

### T5.3 发布池 + 限频 + 失败转人工 · **P0** ✅ **已完成（2026-09-17）**
- 依赖：T5.2, T1.5 ｜ 里程碑：M5 ｜ 契约：**§4.6.7（新增）** / §03.4.4 / §06.5.4 / §06.10 / §06.12
- [x] `publish` 池 worker（`pools/publish_worker.py` + `workers/run_publish.py`）
- [x] 限频：**≤3 条/天/账号** + 间隔 ≥30min ⇒ 不过**顺延**（不是失败）
- [x] 重试 ≤3 次指数退避（复用 `JobStore.fail` 那一份退避算术，不另写一条曲线）
- [x] 失败转"待人工发布"（`manual_required`）+ 写 `audit_ops`（§06.10 不变量 3）
- [x] **幂等键含 `account_id`**（`sha256(task_id|platform|account_id)`，落在 `publications.idempotency_key` 的 UNIQUE 上）
- [x] 发布失败**不回退任务状态**（任务仍为 `completed`，成片可下载后人工发）
- ✅ `pytest tests/integration/test_publish_pool.py -q` ⇒ **4 passed**，四条验收逐条对上：
  - ① 第 4 条当天被限频 ⇒ **自动顺延**：`units_deferred=1` / `units_failed=0`，作业回 `pending`、`attempts=0`、
    `error_code=PUBLISH_RATELIMIT`、`not_before` 在**未来**，且**一条记录都不落**（它还没开始发）；任务仍 `completed`
  - ② 连续失败 3 次 ⇒ `publications.status='manual_required'`、`attempt_count=3`、`error_message` 是发布器原话，
    且**任务仍为 `completed`**；`audit_ops` 恰好一条 `publish.manual_required`
  - ③ 幂等键防重复发布：同一条任务投两次（第二次被幂等挡下且**说得出为什么**）、池跑两轮（第二轮 `units_done=0`）
    ⇒ 发布器**只被叫过一次**，`idempotency_key` 逐字等于 `sha256(f"{task_id}|douyin|acc_main")`
  - ④ `GET /api/v1/publish/queue` 查得到待人工那一条（带 `error_code` / 三个 `can_*`），看板 `counts` 与它一致
- ✅ `pytest tests/unit/pools/test_publish_worker.py tests/unit/db/test_publication_repo.py tests/unit/publish/test_ratelimit.py -q`
  ⇒ **39 passed**（19 + 12 + 8）；`tests/integration/test_queue_lease.py` 另加 **4 例** `defer` 用例（含"连顺延五轮仍不进死信"）
- ✅ `.\tasks.ps1 check` ⇒ **3518 passed / 32 skipped / 27 deselected**（比 T5.2 多 **47 例**）；
  `ruff format` + `ruff check` + Web 契约（`web/openapi.json` 重生成，新 6 个端点）+ `mypy`（**344 source files**）全绿
- ⚠️ **R13 风控** ⇒ 限频 + 登录态探测 + 转人工 + **不实现验证码绕过 / 不自动登录**
- ⚠️ **R14 不可逆** ⇒ 出厂 `publish.enabled=false`：真发布一律 `PUBLISH_DISABLED` 死信，**演练照样放行**（裁定 269）
- **交付物**：
  - 队列原语 `src/studio/db/queue.py` 的 `JobStore.defer`（回 `pending` + `not_before` + **把刚加上的那次尝试退回去**）
  - 池层顺延语义 `src/studio/pools/worker_base.py` 的 `UnitDeferred` + `PoolWorker._defer` + `WorkerRunReport.units_deferred`
  - 限频策略 `src/studio/publish/ratelimit.py`（次日零点 + `blake2s` **确定性**抖动；`min_gap` 那条不加抖动）
  - 记录仓储 `src/studio/db/repositories/publication_repo.py`（`publications` 的**唯一写入者**：幂等 `create` / `mark_failed` /
    `mark_manual_required` / `mark_dry_run` / `reset_for_retry` / `manual_queue` / `counts`）
  - 单元处理器 `src/studio/pools/publish_worker.py`（八步 + 转人工 + **只读幂等短路**）+ `workers/run_publish.py`
  - 登记 `src/studio/pools/runner.py` 的 `HANDLER_MODULES["publish"]`（**四个池的 handler 都齐了**）
  - 服务面 `services/publish_service.py`（`enqueue_publications` / `build_board` / `retry_publication` /
    `cancel_publication` / `mark_manual_done` + `resolve_platform` / `resolve_account`）
  - REST **六个端点** `app/routers/publish.py`（看板 / **待人工队列** / 投递 / 重试 / 取消 / 标记已人工处理）+ `app/schemas/publish.py`
  - CLI 五个命令 `studio publish {enqueue,queue,retry,cancel,manual-done}`
  - 测试 **47 例**（进 `check`）：`test_publish_worker.py`（19）· `test_publication_repo.py`（12）·
    `test_ratelimit.py`（8）· `test_publish_pool.py`（4，集成）· `test_queue_lease.py`（+4）
- **施工裁定（本轮新增 268–275）**：
  - **268** 限频触顶 ⇒ **顺延**（`JobStore.defer` + `UnitDeferred`），**不消耗 `attempts`**：走 `fail` 会把"今天额度用完了"算成三次失败之一 ⇒ 三天后一条**从没真正试过**的作业自己进死信并告警。`defer` **不做成"认领前先查限频"**：认领是单条 SQL 的原子操作，认领前查会开出超发窗口，而超发**不可逆**（R14）
  - **269** `publish.enabled=false` 时**演练照样放行**，真发布一律 `PUBLISH_DISABLED` 死信；而**投递期不看开关**（`POST /publish/tasks/{id}/enqueue` 照样成功，作业在 worker 那一侧带原因转人工）—— "点了没反应"比"有一条带原因的待人工"难查得多
  - **270** "转人工"让 **job 成功**（`manual_required` 的语义是"自动这条路走完了，接下来等人"）：让它以失败收场会把同一条记录**既送进待人工队列、又送进死信告警**，而两者的排障动作完全不同。判"重试还是转人工"用**错误码 + 剩余次数**，不照抄 `PublishResult.status` —— 发布器**不知道还剩几次机会**
  - **271** 幂等短路排在限频**之前**（只读、不改库）：已经 `published`/`canceled` 的记录再去问"今天额度够不够"没有意义，而额度用满时它会先被顺延 30 分钟 —— 一次重投变成一次白等
  - **272** 限频**计数在 `JobStore`**（要读 `publications` 表），**策略在 `publish/ratelimit.py`**。次日顺延的抖动由 `blake2s(account_id + 日期)` 派生 ⇒ **同一天问多少次都是同一个时刻**（随机的话每次被限频都把 `not_before` 往后推一点，那条作业永远等不到自己）；`min_gap` 那条**不加**抖动（锚点本身已经是散的）
  - **273** 幂等键的实现下沉 `core/ids.py`（`db` 不许 import `domain`，§02.4 —— 契约测试当场会红），`domain.publish.idempotency_key` 保留为**别名**。§03.3.15 的 `|` 分隔符与真机已落库的键**一字不改**（换分隔符等于让每条已有记录换个键，重复发布防护当场失效）
  - **274** `publish` **不进** `SERVICE_NAMES`（仍是"五进程"）：发布进程随 T5.5 发布面板一起接进 supervisor。理由是出厂 `publish.enabled=false` —— 一个常驻发布 worker 在开关关着时唯一会做的事，是把投递进来的作业标成 `PUBLISH_DISABLED`
  - **275** `can_retry` / `can_cancel` / `can_mark_done` 由**服务端**算：判据（`published` 不能取消、`manual_required` 才能标记已处理）是服务端的状态机规则，发给面板三个布尔比让前端记住"哪些状态能点"可靠 —— 规则改一次就漏一处，而漏的那一处会变成"点了按钮报 400"
- ⚠️ 新增陷阱 6 条已并入 §10（编号 140–145）

### T5.4 数据回收 + 记忆沉淀闭环 · **P1**（**已交付 2026-09-18**）
- 依赖：T5.3 ｜ 里程碑：M5 ｜ 契约：§06.6 / §06.8
- [x] 采集时点：**T+1h / 6h / 24h / 72h**（相对 `published_at`，走 `metrics_schedule_hours`）
- [x] `metrics_json` + `metrics_history_json` 落库（`record_metrics`：最新 + 时间序列 + 计数器归零）
- [x] `next_metric_at` 由 `metrics_service` 轮询（**定时器，非队列**）——
      `services/publish_metrics_service.py` 拍一拍，`app/recycle.py` 每 60s 起跳
- [x] `data/feedback/auto_YYYYMM.md` 生成（`publish/memory.py`：幂等追加 + 结构化头）
- [x] **闭环验证**：`auto_*.md` 可被 §04.1.7 解析器消费（`_consumable` 真的跑一遍解析器），
      且 `grounded_on` 里出现 `source="auto"`（`_mark_auto_refs` 确定性回填）
- ✅ T+1h 能在发布面板看到数据；`auto_YYYYMM.md` 生成且**能被解析器消费**
- ⚠️ 采集失败 ⇒ 顺延 1h 重试（≤3 次），用尽即停止采集**这一条**，不阻断其他任务
- ⚠️ 发布池正忙 ⇒ 整拍让路（两个浏览器抢同一个 profile 会让**发布**失败）
- ⏸ **评论抓取：用户裁定暂时搁置（2026-09-18）**。用户口径：「暂时只关心播放量 / 点赞量 /
      完播率这些数据，暂时不要考虑门禁校验等线上问题，抓紧跑通流程」⇒ 本轮**不做**评论抓取。
      `sink_memory` 的缝留着（`comments=[…]` 或发布器的 `fetch_comments`），各平台评论页的
      选择器还没写 —— 在那之前 ① 恒为 0，②③ 照常工作。**连带后果要说清**：
      `feedback_items_created` 恒为 0 是**预期**（§06.8 ① 的输入就是评论），不是坏了；
      而 §06.8 ③ 的汇总文件因此**只有两行表头、没有数据行**，`planner_consumable=true`
      只表示"格式能被解析器吃"（`_consumable` 真跑了一遍解析器）。
- [x] 发布面板「数据回流」区块的**按钮**（跑一轮 / 采这一条 / 沉淀这一条）已接线：
      `api/endpoints/publish.ts` 三函数 + `stores/publish.ts` 三动作 + `views/Publish.vue` 区块头
      「跑一轮」与逐条「采数 / 沉淀」，并显示「上一轮：采到 N 条 · 顺延 M 条 · …」与
      「下一次 <时刻> · 失败 K 次」（前端 **435 passed**）
- **施工裁定（本轮新增 319–321）**：
  - **319** 采集时点**相对 `published_at`** 算，不是"上次 +1h"：后者每失败顺延一次就把整条趋势线
    往后推，72h 那个点会漂到发布后第 5 天（陷阱 172）
  - **320** 发布池**忙 ⇒ 整拍让路**（`PublishMetricsService.publish_busy`）：两个浏览器抢同一个
    `browser_profile` 的代价不是"采不到数"，是**发布失败** —— 采数是"读"，读不到可以下一拍再读（陷阱 173）
  - **321** `grounded_on[].source` 由**服务层文本比对**回填（`_mark_auto_refs`），**不让模型自报**：
    让 LLM 说"这条依据来自自动回流"等于请它猜，而它一定会猜"是"（陷阱 174）
- **施工裁定（2026-09-18 增补）**：
  - **322** 播放量 / 点赞量 / 完播率这些**数字**不进 `feedback_items`（T5.4 落库时已定，
    本轮复核确认）：§06.8 ① 的输入是**评论**，数字走的是 ② 的降权与 §6.7 的报告。
    ⇒ 面板文案必须跟着这个事实走（原来写"只回流了数字"是错的，见陷阱 176）
  - **323** 完播率进契约时存 **0–1 的比值**（`PublishMetrics.completion_rate`），
    百分数只在**显示那一处**乘 100：百分数被当成比值是 100 倍的错，而 42 与 0.42
    都是"看着正常"的数（陷阱 175）
- ⚠️ 陷阱 **172**（时点相对谁算）· **173**（采数与发布抢 profile）· **174**（让模型自报来源）
  · **175**（比率与百分数同形）· **176**（面板说"回流了"而库里什么都没有）

### T5.5 发布面板 + 合规留档 + 外部对接 · **P1**（**已交付 2026-09-17**）
- 依赖：T5.4 ｜ 里程碑：M5 ｜ 契约：§06.11 / §06.12
- [x] 面板**七区块**：待发布 / 发布中 / 已发布 / 数据回流 / **待人工** / **定时计划** / **报告**
  - 前五块**后端齐备、面板可用**；「**定时计划**」**2026-09-18 补齐**（T5.6：真列表 + 建计划表单
    + 立即执行 / 启停 / 删除）；只剩「报告」一块后端没有端点（`tests/integration/test_reports.py`
    不存在）⇒ 面板如实写「尚未施工 + 卡在哪」，**一个数字都不画**（裁定 299）
  - 另加**第八块「失败 / 已取消」**：`failed` 的记录还在自动重试，藏起来会被读成"这条根本没投出去过"（裁定 298）
  - **数据回流**那块原本只有时刻表（T5.4 未落地）；**2026-09-18 补上三个按钮**：
    区块头「跑一轮」+ 逐条「采数 / 沉淀」+ 「上一轮：…」结论行（见 T5.4 区块）
- [x] `manual_required` 可重试 / 可标记已人工处理 / 可取消（**三者均写 `audit_ops`**）
  - 三个按钮的可用性由**服务端**给的 `can_retry` / `can_cancel` / `can_mark_done` 决定（裁定 275），前端一个布尔都不自己算
  - 「标记已处理」**理由必填**（后端 422；面板先拦一道）
- [x] `HandoffAdapter`（默认 `LocalHandoffAdapter`，可 push 自包含交付包：成片/封面/ASS/稿件/时间轴/manifest）
- [x] 来源登记留档（R2）：`publish/compliance.py` 逐条列缺口（音色 `proof_path` / BGM `proof_path` / 跑酷 `source_url`）
- [x] 应急剧本：`docs/runbook/publish_selector.md`（**T5.2 已交付**）、
  `docs/runbook/publish_account.md`（**本任务交付**：登录态失效重新扫码 + 多账号切换 + 新增第 2 个账号要动什么）
- [x] **R2 合规提示常驻**（发布面板 + 素材库，**同一份服务端文案** `R2_NOTICE`）
- ✅ 七区块可用；`manual_required` 三种操作均留痕；交付包可导出；两个剧本齐备
- ⚠️ `manual_required` 堆积 ⇒ 面板顶部计数 + 告警
- ⚠️ 剧本过时 ⇒ 每次页面改版热修后更新剧本版本号

- **落地清单**：
  - 后端：`publish/handoff.py`（`build_package` / `DeliveryItem` / `DeliveryPackage` / `HandoffAdapter` /
    `LocalHandoffAdapter` / `handoff_adapter` / `resolve_cover`）+ `publish/compliance.py`
    （`R2_NOTICE` / `Registration` / `ComplianceSnapshot` / `compliance_snapshot`）
  - REST **三个新端点** `app/routers/publish.py`：`GET /publish/handoff/{task_id}`（预览，**一个字节都不写**）·
    `POST /publish/handoff/{task_id}`（导出 + 写 `audit_ops`）· `GET /publish/compliance`（只读）；
    契约再生成（`web/openapi.json` + `types.gen.ts`）
  - WebUI：`web/src/views/Publish.vue`（八区块）+ `web/src/stores/publish.ts` + `web/src/api/endpoints/publish.ts`；
    `stores/ui.ts` 的 `publish` 置 `ready: true`、`App.vue` 接线；素材库加 R2 常驻提示（`stores/assets.ts` 的 `compliance`）
  - 测试 **50 例**：`tests/unit/publish/test_handoff.py`（12）· `test_compliance.py`（7）·
    `tests/integration/test_publish_handoff_api.py`（8）· `web/src/stores/publish.test.ts`（23）
  - `.\tasks.ps1 check` ⇒ **3636 passed / 32 skipped / 27 deselected**（+27）；
    `.\tasks.ps1 web:verify` ⇒ **406 passed · dist 0.34 MB**（+23 例）
- **施工裁定（本轮新增 293–299）**：
  - **293** 交付包**预览与打包走同一个 `build_package`**：分开写会让"面板说六件齐"与"真打出来的包少两件"
    各自成立 —— 那种不一致最难查（与陷阱 #150 / #151 同族）
  - **294** 预览是 **GET 且一个字节都不写**，导出才落盘 + 写 `audit_ops(action='publish.handoff')`：
    交付包是**离开我们掌控**的东西，没有那一行就答不上"这份片子什么时候被谁导出去过"
  - **295** 手动导出**不看** `handoff.enabled`（沿用裁定 269）：那个开关管的是"发布时自动顺手带一份"，
    而人按下的这一下就是意图本身 —— 拦它只会得到"按钮是坏的"
  - **296** 适配器名字不认识 ⇒ **抛 `CONFIG_INVALID`**，不静默退回 `local`：静默退回的后果是
    "配置写了对接、包却一直落在本地盘上"，而两边都不报错
  - **297** 打包目录名的时间戳取到**毫秒**（`[:19]`）：秒级精度下"连按两下导出"会算出同一个目录名，
    而覆盖正是这一层要避免的（留两份的代价是几百 MB，留一份错的代价是"发出去的片子对不上归档"）
  - **298** 面板排**八块**（规格七区块 + 「失败 / 已取消」）：`failed` 的记录**还在自动重试**
  - **299** 「定时计划」与「报告」**不画假数据**：后端没有对应端点，面板如实写"尚未施工 + 卡在哪" ——
    画一个看着像真的时刻表，比空着更糟（用户会按它去安排发布）
- ⚠️ 新增陷阱 3 条已并入 §10（编号 155–157）

### T5.6 定时发布调度 · **P0**（★D7 + Q14 · **已交付 2026-09-18**）
- 依赖：T5.3 ｜ 里程碑：M5 ｜ 契约：**§03.3.18 / §04.6.5.1 / §06.5.5**
- **为什么做**：这是 M5 门禁里「**定时**自动发布」那半句 —— T5.3 的发布池能发，但只能人按；
  而「每天在 18:00–21:30 之间挑个时刻自己发一条」是这套系统的主要用法（★D7 + Q14）。
- [x] `services/scheduler_service.py`：**30s tick**（`app/recycle.py` 的 `SchedulePump`），`next_run_at` **落库**
- [x] 三种模式：`at_time` / `daily_window` / `interval`
- [x] 窗口内**随机取时刻**：`HMAC(schedule_id, 'base|日期')` ⇒ 同一天多次计算得**同一时刻**（幂等，重启不漂移）
- [x] 抖动 `jitter_min`（默认 15，≤120），**不得越出窗口**（越界夹取到边界）
- [x] **到点才建 job**（不预占，**不占 worker 空转**）
- [x] **策略可编辑（Q14）**：模式 / 窗口 / 抖动 / 平台 / 账号 / 启停全部 WebUI 可编辑
- [x] 编辑 ⇒ **同事务重算 `next_run_at`** + `audit_ops`（陷阱 #33）
- [x] 参数校验：`HH:MM` 且 `start < end`；`jitter_min ∈ [0,120]`；`at_time` 带时区；非法 ⇒ **400 且不落库**
- [x] 被限频 ⇒ `last_result='skipped_ratelimit'` + 顺延（**不算失败**）
- [x] `publish.enabled=false` ⇒ 空转 + `skipped_disabled`
- [x] 连续失败 ≥5 次 ⇒ `system.alert`（`AlertCode.SCHEDULE_FAILING`）
- [x] **幂等命中不算失败**：这条任务在该平台上早就投过 ⇒ `last_result='skipped_duplicate'`（`fail_streak` 不动）
- [x] REST：`GET/POST /api/v1/schedules`、`PATCH/DELETE /api/v1/schedules/{id}`、`POST .../run_now`
- [x] WS：`publish.scheduled` / `publish.schedule_fired`
- ✅ `pytest tests/integration/test_scheduler.py -q` ⇒ **23 passed**：①窗口模式在 `[18:00,21:30]` 内取时刻且叠加 ≤15min 抖动 ②**同 schedule + 同一天多次计算得到同一时刻** ③未到点/已停用的计划**不**被取到 ④到点 ⇒ 创建 `publish` job 且不占 worker ⑤被限频 ⇒ `skipped_ratelimit` + 顺延 ⑥`enabled=false` ⇒ `skipped_disabled` ⑦连续失败 ≥5 ⇒ 告警 ⑧增删改启停均写 `audit_ops` ⑨**非法参数被拒绝**且不落库 ⑩编辑 ⇒ **同事务重算 `next_run_at`**
- **落地清单**：
  - 后端：`domain/schedule.py`（时刻算术纯函数）+ `db/repositories/schedule_repo.py` +
    `services/scheduler_service.py` + `app/schemas/schedule.py` + `app/routers/schedules.py`（5 端点）+
    `app/recycle.py` 的 `SchedulePump`（30s）；`AlertCode.SCHEDULE_FAILING`（枚举加一个成员）+ 两条 WS 事件
  - 前端：`api/endpoints/schedules.ts` + `stores/schedules.ts`（15s 慢轮询）+ `views/Publish.vue` 第 ⑪ 块
    「定时计划」换成**真 UI**（列表 + 立即执行 / 启停 / 删除 + 建计划表单）
  - **表早就存在**（`0005_schedule_report.sql` 的 `publish_schedules`，含三个索引与 touch 触发器）⇒ **本轮零迁移**
- ✅ **真机**（服务重启后打真库）：`GET /api/v1/schedules` ⇒ 空清单；`POST` 建一条
  （`other` + `_rehearsal` + 18:00–21:30）⇒ `next_run_at=2026-09-18T10:09:00Z`（= 本地 18:09，**落在窗口内**）；
  总开关关着时 `run_now` ⇒ `skipped_disabled`；临时打开开关 ⇒ 撞幂等 ⇒ **`skipped_duplicate` + `fail_streak=0`**
  （修之前是 `error:PUBLISH_FAILED` + `fail_streak=1`，见陷阱 182）；`DELETE` ⇒ `deleted=true` + 留痕，清单回到空。
  **验完已把 `config/publish.yaml` 改回 `enabled: false`**
- ✅ **门禁**：`.\tasks.ps1 check` ⇒ **3892 passed / 32 skipped / 27 deselected**（+25 例）；
  `.\tasks.ps1 web:verify` ⇒ **482 passed · dist 0.37 MB**（+31 例）
- ⚠️ 陷阱 #29 变成「每天准点」的机器特征 ⇒ 窗口随机 + 抖动
- ⚠️ 陷阱 #30 重启丢计划 ⇒ `next_run_at` **落库**
- ⚠️ 陷阱 #33 只改参数不重算 ⇒ 不生效或立刻触发
- ⚠️ 调度器本身崩溃 ⇒ supervisor 守护（T4.11）
- ⚠️ 新增陷阱 **179**（把 UTC 时刻当本地时间显示）· **180**（枚举加值 ⇒ 硬编码的 `len(...)` 与「N 值」文案过时）
  · **181**（前端门禁只报一句「未通过」，真错要绕开外层跑 `npm run typecheck`）
- **施工裁定（325–332）**：
  - **325** 窗口内取时刻用 **HMAC(schedule_id, 'base|日期')** 而不是 `random`：这是"重启不漂移"的**全部实现**（库里
    `next_run_at` 丢了重算一次还是同一时刻）。抖动用**另一个 salt**（`'jitter|日期'`）而不是复用同一个数 ——
    复用会让"基准时刻"与"抖动"完全相关，抖动就白加了
  - **326** 抖动**夹在窗口边界内**（`min(max(base+offset, start), end)`），且 `jitter_min=0` 时**不掷第二次骰子**：
    "±0 分钟"也去取一个随机数，会让"关掉抖动"反而挪位
  - **327** `jitter_min` 的 pydantic 约束**不写在 schema 上**（写了是 **422**），校验统一在服务层 ⇒ **400 且不落库**：
    规格要的是 400，两个错码混用会让用户看着 422 去改一个根本没写错的字段（与裁定 301 同族）
  - **328** `ScheduleRepo.update()` 是**整行 UPDATE 含 `next_run_at`**（陷阱 #33）：拆成两条语句时，中间那一瞬的
    `next_run_at` 是按**旧**窗口算的
  - **329** 调度器**池级限频为准、账号级兜底**（与 `publish_worker._daily_limit` 同口径）：两边各写一份判据
    ⇒ 会出现"调度器说能发、worker 说超限"这种两边都自洽的假矛盾
  - **330** 面板的「立刻执行一次」走**与到点触发同一条 `fire()`**（照样看总开关、照样过限频）：加一条"调试就跳过检查"
    的旁路，面板行为与后台行为就分叉了 —— 而分叉只会在真出事那天被发现
  - **331** 展示层的时间戳**必须换算**（`stampText` 用 `new Date()` + 浏览器真实偏移），不许截 UTC 字符串；
    时区取**浏览器偏移**而不是写死 `+08:00`（写死后任何一台非东八区的机器排出来的计划都会差几小时，
    而面板上显示的时刻还是对的 —— 最难查的一类）
  - **333** **幂等命中（早就投过）不是失败**：`EnqueueReport.duplicates` 单独列出来，命中 ⇒ `skipped_duplicate`
    且 `failed=False`。「作业没建出来」有两种原因，处置动作相反 —— 平台/账号不可用要人改配置，早就投过什么都
    不用做；合成一个 `error:` 的代价是钉着已发任务的计划每天"失败"一次、五天后拉一条真告警（陷阱 182，真机踩到）
  - **332** **枚举的数量不进断言、不进文案**：加一个 `AlertCode` 就要改两处 `len(...) == 8` 与三处「8 值」文案，
    而文案不会自己红，只会安静地骗人（陷阱 180）

### T5.7 数据报告与决策闭环 · **P0**（★D9 + Q15）· **已交付 2026-09-18**
- 依赖：T5.4 ｜ 里程碑：M5 ｜ 契约：**§03.3.19 / §03.3.20 / §04.6.5.2 / §06.7**
- [x] `services/report_service.py`：**纯 SQL 聚合 + 规则归因**（**不调 LLM**）
- [x] 五维归因 + 两项：①选题类型 ②发布时段 ③平台 ④时长 ⑤稿件评分 ⑥TTS 质量 ⑦成本
- [x] 产物：`summary_md`（模板渲染）+ `data_json` + `insights_json` + `artifacts_json` + `coverage_json`
- [x] `insights` 置信度：`n<10` ⇒ `low`（**必须**标注"样本不足"）/ `10–29` ⇒ `medium` / `≥30` ⇒ `high`
- [x] **周期可编辑（Q15）**：`report_schedules` CRUD；`period`/`weekday`/`day_of_month`/`at_time`/`tz`/`lookback_days`/`include`/`enabled`
- [x] **同周期仅 1 个启用**（部分唯一索引）；`is_builtin=1` **不可删、只能停用**
- [x] 编辑 ⇒ 重算 `next_run_at` + `audit_ops`
- [x] **决策闭环**：`POST /api/v1/reports/{id}/insights/{idx}/apply` ⇒ 写入 persona 偏好项 ⇒ **影响下轮 Planner**，写 `audit_ops(action='report.apply_insight')`
- [x] `GET /api/v1/reports/{id}/export?format=md|csv`
- [x] WS：`report.generated` / `report.schedule_updated`
- ✅ `pytest tests/integration/test_reports.py -q` ⇒ **29 passed**：①周报含 `summary_md` + `data_json` + `insights` ②**同周期重复生成不重复插入** ③`n<10` ⇒ `confidence='low'` 且面板强制显示"样本不足" ④`apply` ⇒ 写入 persona + `audit_ops` ⑤**闭环**：采纳后 `persona_variables(...)['style_hint']` 含该结论 ⑥非法 `period` 被 CHECK 拒绝 ⑦**周期编辑** ⇒ `next_run_at` 重算 + `audit_ops` ⑧**同周期仅 1 个启用** ⑨停用 ⇒ 不再被到期查询取到；外加：幂等再点、结论只留 5 条、越界 index、内置不可删、到点生成、**补排期**、无数据空转、连败告警、`run_now`、导出 md+csv、WS 事件、REST roundtrip、400/422 边界
- ⚠️ 陷阱 #31 报告被当"结论"直接改生产策略 ⇒ `insights` **必须人工采纳**（**不自动改配置**）
- ⚠️ 陷阱 #32 报告烧钱 ⇒ **不调 LLM**（纯 SQL）
- ⚠️ 采纳不可追溯 ⇒ `audit_ops` 记 `before/after`
- ⚠️ 陷阱 #183 内置周期 `next_run_at` 初值为 NULL ⇒ 调度器每拍先补排期（否则出厂自带的周报**一次都不会跑**）
- ⚠️ 陷阱 #184 枚举数量进了断言与文案 ⇒ 加 `REPORT_FAILING` 让两处契约测试与两处文档当场过时（同陷阱 180）

**落地清单**

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 领域 | `domain/report.py` | `period_bounds`（右端 = 触发日**前一天**）/ `next_run_at`（逐日扫 400 天）/ `confidence_for` / 四个校验器 |
| 仓储 | `db/repositories/report_repo.py` | `ReportRepo`（`get`/`require`/`find_period`/`list_recent`/`create`/`mark_applied`）+ `ReportScheduleRepo`（含 `delete()` 里写死 `AND is_builtin = 0`） |
| 服务 | `services/report_service.py` | `build_report`（**一条 SQL**，中位数等在 Python 算）+ `_build_insights`（只出「有对比且相对差 ≥10%」的）+ `report_markdown` + `generate_report`（幂等 / 落盘 / 留痕 / 发事件）+ `apply_insight`（写 `persona.style_hint`）+ `export_report` + `ReportSchedulerService`（`tick` 含 `_schedule_pending`）+ 周期 CRUD |
| 接口 | `app/routers/reports.py` | **9 端点**：`GET|POST /api/v1/reports`、`GET /reports/{id}`、`POST .../insights/{idx}/apply`、`GET .../export`、`GET|POST /report-schedules`、`PATCH|DELETE /report-schedules/{id}`、`POST .../run_now` |
| 契约 | `core/proto.py` · `ws/protocol.py` | `AlertCode.REPORT_FAILING`（`warn`）+ `report.generated` / `report.schedule_updated`（均进 `Channel.PUBLISH`） |
| 编排 | `app/recycle.py` | `SchedulePump.report_tick()` —— **第二个独立 try**（发布失败影响产出，报告失败只影响洞察，一边炸不能带走另一边） |
| 前端 | `api/endpoints/reports.ts` · `stores/reports.ts` · `views/Publish.vue` ⑫ | 10 个 API 函数 + 60s 慢轮询 store + 报告列表 / 详情 / 建议卡片（含采纳与「样本不足」警示）/ 导出 / 周期编辑表单 |

**真机读数（2026-09-18 · 六进程全 ready）**

- `POST /api/v1/reports/generate {period:"daily", start:"2026-09-18", end:"2026-09-18"}` ⇒
  `01M2T9MDBZF9XKCDCZF2AF94GH`，`publish_count=1` / `median_views=128524` / 落盘
  `data/output/reports/daily_2026-09-18_2026-09-18.md`；**再发一次 ⇒ 同一个 id**（幂等，`counts.total=1`）；
- `POST /api/v1/report-schedules/rsched_weekly/run_now` ⇒ `ok` + `01M2T9MZ8TJF5YZ9C0YKG6P567`（`weekly 2026-09-11~2026-09-17`）；
- `GET /api/v1/reports/{id}/export?format=csv` ⇒ 200 + `Content-Disposition: attachment; filename="daily_2026-09-18_2026-09-18.csv"` + `text/csv`；
- `GET /api/v1/report-schedules` ⇒ seed 三条（周报/月报启用、日报停用），`next_run_at` 起初为 `null`，
  **api 起来跑一拍后自动补上**（`2026-09-21T01:00:00Z` / `2026-10-01T01:00:00Z`）；
- `PATCH {enabled:true}` ⇒ 当场算出 `2026-09-19T01:00:00Z`；`{enabled:false}` ⇒ 置回 `null`；验完已复原；
- `DELETE /api/v1/report-schedules/rsched_weekly` ⇒ **400 `REPORT_INVALID`**「『weekly』是内置周期，不能删，只能停用」+ `remediation`；
- 面板第 ⑫ 块目视：三条建议卡片（置信度低/高/中三色灯、`n=4` 那条强制显示「样本不足，仅供参考」、已采纳的按钮禁用）、
  周期三条（`已生成` 绿灯 / `窗口里没数据` 黄灯 / `失败：数据库锁住了` 红灯 + 连续失败 3 次），内置行的「删除」按钮置灰。

**施工裁定（334–340）**

- **334** **采纳写 `persona.style_hint`，不写 `content_directions.priority`**：后者是**历史批次**
  （"当时为什么这么做"的证据），下一轮 Planner 会重新生成一个批次 ⇒ 改上一批的 priority 对下一轮
  **没有任何影响**。表现出来就是"我点了采纳，但选题一点没变"—— 那是最坏的一类假闭环。
  `style_hint` 是**所有 Agent（含 Planner）每次都会读到**的公共输入（`agents/base.py` 的 `persona_block`）。
- **335** **只统计已发布记录**：把未发布的算进中位数，那个数字就是自己编的。面板空态把这句话写出来。
- **336** **窗口右端 = 触发日的**前一天**，左端由 `lookback_days` 倒推**：今天还没过完，算进去每份报告都偏低。
- **337** **`is_builtin=1` 只能停用、不能删**（`delete()` 的 SQL 里写死 `AND is_builtin = 0`）：删掉之后
  没人知道它们本来是什么，而"停用"**会留痕**。
- **338** **导出走 `apiDownload` 而不是 `apiGet`**：导出要的是一个**文件**（带 `Content-Disposition`），
  前端自己拼文件名的话，那份"拼出来的名字"与服务器实际写的那一份迟早分家 —— 而没有任何地方会报错。
- **339** **`skipped_no_data` 不是失败**：窗口里没数据是常态（新账号 / 淡季），照记 `last_result` 但**不涨**
  `fail_streak`。涨了的话，一个刚上线的账号会在三周后拉出一条"报告连续失败"的告警，而它淹掉的正是真故障（同陷阱 182）。
- **340** **`REPORT_FAILING` 的严重度是 `warn`**（比 `SCHEDULE_FAILING` 的 `error` 低一档）：报告失败只影响
  洞察，发布失败影响产出 —— 两件事的处置等级本来就不同（§3.3.20「不阻断生产」）。

**新增陷阱**

| # | 现象 | 根因 | 正确做法 | 关联 |
| --- | --- | --- | --- | --- |
| 183 | **出厂自带的周报 / 月报一次都不会跑，而且不报错** | seed 里三条内置周期的 `next_run_at` 初值全是 NULL，而到期查询写的是 `next_run_at <= now` —— SQL 里 `NULL <= x` **恒为假** | 把 `NULL` 读成「未排期」而不是「永不触发」：调度器每拍先 `_schedule_pending()`（`enabled=1 AND next_run_at IS NULL` ⇒ 当场算好落库）。它同时覆盖「新建时没排期」与「停用后被清空」两种情况 | T5.7 |
| 184 | **加一个枚举值，两处契约测试与两处文档当场过时** | 陷阱 180 的同一个坑再咬一次：T5.6 记下"枚举数量不进断言、不进文案"之后，仓库里仍有 `len(AlertCode) == 9` 与两处「9 值」文案 | 契约测试改成 `>= 9` 下限（**数量不是它要守的东西** —— 它守的是"猝死不升格为告警"）；文档只写「枚举」。加值前先 `rg "== 9|9 值"` | T5.7 |

### T5.8 多账号支持与合规留档 · **P1**（★D1）
- 依赖：T5.3, T5.5 ｜ 里程碑：M5 ｜ 契约：§06.2.4 / §06.9
- [ ] `accounts[]` 配置；**默认只配 1 个**（`acc_main`）
- [ ] 按账号隔离 `profile_dir`
- [ ] **限频按账号独立计数**
- [ ] 幂等键含 `account_id`
- [ ] 账号 A 登录态失效**不影响**账号 B
- [ ] `reports.data_json` 可按 `account_id` 拆分
- [ ] 来源登记留档（R2）：`data/voice_src/*/profile.json` + 素材 `proof_path`
- [ ] **新增第 2 个账号 ⇒ 零迁移**（仅改 `config/publish.yaml` + 首次扫码）
- ✅ `pytest tests/integration/test_multi_account.py -q`：①两账号限频互不影响 ②同一任务分发到两账号 ⇒ 生成 2 条 `publications`（**不互相阻塞**）③账号 A 失效不影响 B ④报告可按账号拆分
- ⚠️ 多账号误配 ⇒ **未配置即不参与发布**（安全默认）
- ⚠️ 账号风控关联 ⇒ 独立限频 + 独立 profile + 错峰发布（T5.6）
- ⚠️ **范围蔓延** ⇒ 一期只跑通单账号，多账号仅"结构可用 + 测试覆盖"

### T5.9 本地演练台（无真账号也能跑完整条发布链路）· **P1**（**已交付 2026-09-18**）
- 依赖：T5.3, T5.4 ｜ 里程碑：M5 ｜ 契约：§06.5.4 / §04.6.1
- **为什么做**：用户口径是"抓紧跑通流程"，而**没有真账号**时后半条链路（发布 → 数据回收 →
  记忆沉淀）一步都验不了。演练台把"发布"这一步换成打**本机靶页**，其余一字不改 ——
  于是"投放 → 采数 → 沉淀"能在真库里真跑一遍。
- [x] `config/publish.yaml` 新增 `platforms.other`（`publisher: fixture`）+ 账号 `_rehearsal`
      （`profile_dir` 与真账号**物理隔离**：`data/browser_profile/_rehearsal`）
- [x] `core/config.py` 的 `PlatformCode` 加 `other`（§03.3.15 的 CHECK 里本来就有这一档）
- [x] **完播率**（用户点名要的三个数之一）：`PublishMetrics.completion_rate`（**0–1 比值**）+
      `parse_metric_ratio()`（`42.3%` / `42.3` / `0.423` ⇒ 0.423；`101%` / 负数 ⇒ `None`）+
      `RATIO_METRIC_KEYS` 与计数分开走各自的解析器
- [x] 靶页 `fixtures/upload_form.html`：数字**由作品号确定性算出来**（FNV-1a：播放 8k–260k /
      赞 200–30k / 评论 30–4k / 转发 10–900 / 完播 22.0–68.9%），带查询串就原样用
- ✅ **真机**：`POST /publish/tasks/{id}/enqueue {"platforms":["other"]}` ⇒ publish 池**真发布**
      （Playwright 打本地靶页）⇒ `publication_id=01M2SNT04PV558PT76QF74CFFK`、`status=published`、
      `platform=other`、`platform_post_id=rehearsal-1789715614909`、`dry_run=false`；
      `POST …/collect` ⇒ `views=128524 / likes=28488 / comments=478 / shares=256 /
      completion_rate=0.426`（**完播率端到端打通**）；`POST …/sink` ⇒ `planner_consumable=true`
- ⚠️ 演练台**不在投递默认目标里**（`_default_platforms` 跳过它）：算进去的话，每条任务投递完
      都会顺手多一条"演练发布"，而它在面板上与真发布只差一个平台代号（裁定 324）
- ⚠️ 开关守卫的判据是"**会不会发到真实平台**"而不是"是不是 dry-run"：`FixturePublisher`
      发不出任何东西 ⇒ 出厂 `enabled=false` 时它照样放行（否则"验证链路"要先打开真发布开关）
- ⚠️ 陷阱 **177**（演练台进默认目标 ⇒ 凭空多一条假发布）

### T5.10 发布面板「投递」区块（图形化操作 · 补 T5.5 的缺口）· **P1**（**已交付 2026-09-18**）
- 依赖：T5.5, T5.9 ｜ 里程碑：M5 ｜ 契约：§04.6.2 / §06.5.4
- **为什么做**：用户要求"在 UI 工作台界面能清晰操作"。T5.5 的面板**只能看不能投** ——
  投递只有 `POST /publish/tasks/{id}/enqueue` 与 CLI 两条路，面板上连个输入框都没有。
- [x] `GET /api/v1/publish/platforms`：能投到哪儿、**投了会怎样**（`selectable` / `note`）
      —— 清单**来自配置**，不是面板自己列的（面板列一份 = 把 `publish.yaml` 抄第二遍）
- [x] `PlatformOption` / `platform_options()`：`selectable` 的判据与**投递期跳过它的那两条**
      是同一套（平台未启用 / 这个平台没有启用的账号）⇒ 不会出现"面板显示点得动、投出去被跳过"
- [x] 面板第 ② 块「投递」：任务号 + 平台勾选框（`optionText` 把**账号**也写出来）+
      结论行（`投出 N 条 · 目标 … · 跳过：…`）
- [x] 出厂口径写进 subtitle：**一个都不勾 = 投 `douyin`**（不是"都不发"）；一个都不勾时
      前端**不发** `platforms` 字段（发空数组在服务端是"一个都不投"）
- ✅ 真机（应用内浏览器实测）：勾 douyin → 投递 ⇒ 面板报 `投出 1 条 · 目标 douyin`；
      切到「四池调度」能看到那条作业 `status=dead / error_code=PUBLISH_DISABLED`
- ⚠️ **修掉三处文案假话**：原文写"开关关着时投递进来的作业会**转人工**"，而 worker 的
      开关守卫跑在**建 `publications` 那一行之前** ⇒ 真实落点是**死信**、发布面板上什么都不出现
      （§04-contracts ① 自己写的是"死信"，两处自相矛盾）。现在：面板在真平台选项旁**明说**
      "投了会直接死信、发布面板上不会出现记录（去「四池调度」看）"，文档同步更正（陷阱 178）
- ⚠️ 陷阱 **178**（文案说"转人工"、代码做"死信"）

> **>>> M5 门禁**：成片**定时/即时**自动发布（≥1 平台）+ 数据回流 + **报告生成与决策采纳** + 记忆沉淀闭环（`auto_*.md` 可被解析器消费）。

---

## T6. 追加任务（不在原文 50 个任务内 · **不占一期工期**）

### T6.1 设置面板（LLM 通道与密钥）· **P1**（**已交付 2026-09-17**）
- 依赖：T1.8 ｜ 里程碑：—（服务 M1 真机联调）｜ 契约：§01.4 / §04.2
- [x] `core/secret_store.py`：`SecretStore`（mtime **热重载** · 原子落盘 · **env > 文件** · 写坏则沿用上一份可用值）
      + `mask_secret` / `validate_api_key`（min 8 / max 512）
- [x] 落盘位置 **`config/secrets.yaml`（不入库）**，`config/secrets.example.yaml` 给模板（裁定 300）
- [x] `services/llm_settings_service.py`：探测判定收口（`probe_profiles` / `probe_one` / `profile_cards`）
- [x] `services/settings_service.py`：读 / 写 / 探测 + `audit_ops` + 日志（明文密钥不出这一层，审计只记掩码）
- [x] REST：`GET|PUT /api/v1/settings/llm` · `POST /api/v1/settings/llm/probe`（`app/routers/settings.py` + `app/schemas/settings.py`）；写密钥走 **PUT**（幂等替换），清除走同一端点的 `clear=true`
- [x] WebUI：`web/src/views/Settings.vue` + `stores/settings.ts` + `api/endpoints/settings.ts`；`stores/ui.ts` 的 `settings` 置 `ready: true`；`api/http.ts` 新增 `apiPut`；契约再生成
- [x] 顺手修一个**真 bug**：`app/errors.py` 原样回显 pydantic `errors()` ⇒ 请求体写错变 **500**，且把刚提交的密钥抄回 422 响应（裁定 302 / 陷阱 158）
- ✅ `pytest tests/unit/core/test_secret_store.py tests/integration/test_settings_api.py -q` 全绿；面板改 key ⇒ **立即生效**（热重载，与网关**同一个 `SecretStore` 实例**），`POST .../probe` 回卡片状态
- ⚠️ **不硬编码密钥**、**不在面板保存时回写环境变量**（裁定 300）：env 只是**优先级更高**的来源，不是保存目标
- ⚠️ 表单校验失败 ⇒ `VALIDATION_FAILED`（422）；`CONFIG_INVALID`（400）留给「启动时读到坏配置」（裁定 301）
- ⚠️ 陷阱 158
- **施工裁定（300–303）**：
  - **300** 密钥存 `config/secrets.yaml`（**不入库**），环境变量**优先**；面板保存**不**回写环境变量 —— 回写会让「我以为改了、其实只是这个进程改了」变成下一个人踩的坑
  - **301** 表单校验失败 ⇒ `VALIDATION_FAILED`（422）；`CONFIG_INVALID`（400）**专指**「启动时读到一份坏配置」 —— 两者混用会让用户看着 400 去改一个根本没写错的字段
  - **302** 请求体校验错误**不回显 `input`**，`ctx` 里的异常对象**转字符串**：原样回显既会 500，又等于把密钥抄进响应体（本文件上面那条分支早就写着「请求内容不进响应体」）
  - **303** 探测判定收进 `llm_settings_service`，**CLI 与 REST 共用一份** —— 否则会出现「命令行说能进、面板说不能」这种两边都自洽的假矛盾（与陷阱 #150 / #151 同族）

---

## 6. 横切任务（贯穿全程，每个任务都要满足）

- [ ] **契约先行**：每个任务有引用 `§` 条款编号的契约测试
- [ ] **幂等**：同一 job 重复执行结果一致（产物路径由内容哈希决定）
- [ ] **可观测**：关键路径写 `system_logs`；状态变更写 `task_events`；人工/自动决策写 `audit_ops`
- [ ] **可降级**：每个外部依赖（LLM / TTS / FFmpeg / 磁盘 / GPU / 平台）都有显式失败分支**且有测试覆盖**
- [ ] **无静默失败**：禁止裸 `except: pass`；异常落 `error_code` + 日志 + 状态迁移（**静态检查**）
- [ ] **可冷启动复现**：`studio doctor` 全绿 + `pytest -m "not gpu and not slow and not net"` 通过
- [ ] **超时硬约束**：LLM 120s / 单句 TTS 60s / **一期整片合成 600s**（二期单场景 300s）/ 整任务 30min
- [ ] **零人工**：除确认闸外不得要求人工干预；失败必须自动重试或降级
- [ ] **确定可审计**：同输入 + 同 seed ⇒ 同产物；模板/提示词/引擎版本/种子/滤镜图/人工操作全部留痕

**全局验收命令（每个里程碑都要跑一遍）**

```powershell
uv run studio doctor --json                      # 环境与磁盘门禁
uv run studio db check                           # 30 表 / WAL / 完整性 / 外键
uv run pytest -m "not gpu and not slow and not net" -q    # 快速回归（CI 门槛）
uv run pytest -m contract -q                     # 全部契约测试
uv run pytest -m "e2e and slow" -q               # 端到端（里程碑前跑）
python scripts/audio_qc.py --task <id>           # 响度/峰值（发布门禁 2）· ✅ 已落地
python scripts/dup_audit.py --task <id>          # 相似度（发布门禁 3）· ⏸ 一期不做（见 T3.7）
python scripts/av_sync_audit.py --task <id>      # 仅诊断（C12：不阻断发布）
python scripts/ingest_voice_src.py --dry-run      # 参考音入库体检（T2.4；有被拒 ⇒ 退出码 1）
uv run pytest tests/integration/test_scheduler.py tests/integration/test_reports.py -q   # 定时调度 / 报告（M5 门槛）
```

---

## 7. 里程碑门禁速查

| 里程碑 | 门禁（可执行） | 关联任务 | 状态 |
| --- | --- | --- | --- |
| **M1** | 网页端输入定位 + 热点 ⇒ 产出合格稿件（含评分）⇒ 确认闸可见；`启动.bat` 一键拉起全部服务 | T1.1–T1.12 | [ ] **口径见 §1 T1.12 裁定 108**（「5 进程全 ready」顺延 M4） |
| **M2** | 一句话用熊大音色读出；杀进程重启后已完成句**引擎调用为 0**；网页可见逐句进度 | T2.1–T2.9 | [ ] |
| **M3** | 换稿不重剪（**同素材 + 同水印，换稿件直接出片**）；网页一键出新片并在线预览；水印/响度/相似度门禁通过 | T3.1–T3.7 | [ ] |
| **M4** | ≥3 篇同时推进；中断后恢复；连续 24h 无人干预；全程网页操作 | T4.1–T4.12 | [ ] |
| **M5** | 成片**定时/即时**自动发布（≥1 平台）+ 数据回流 + **报告生成与决策采纳** + 记忆沉淀闭环 | T5.1–T5.10 | [ ] **定时发布已通（T5.6）+ 报告与决策闭环已通（T5.7）**，剩 T5.8 多账号 |

---

## 8. 关键路径（不可并行段）

```
T1.1 → T1.3 → T1.5 → T1.6 → T1.7 ─┬─ T1.8 → T1.9 → T1.10 → T1.11 → T1.12 ─(M1)
                                   └─ T2.1 → T2.2 → T2.3 → T2.5 → T2.6 → T2.7 → T2.8 ─(M2)
                                                                                    ↓
                                            T3.1 → T3.2 → T3.3 → T3.4 → T3.7 ─(M3)  ← ★一期只到单遍合成
                                                                                    ↓
                                                              T4.11 → T4.12 ─(M4)  ↓
                                                                                    ↓
                          T5.1 → T5.2 → T5.3 → T5.6 → T5.4 → T5.7 → T5.5 → T5.8 ─(M5)
```

**可并行支线**（不阻塞主链路）

- `T4.1` 前端脚手架 可与 T2/T3 并行开工
- `T2.4`（原声入库）可与 `T2.5` 并行
- `T3.1`（素材入库）可与 T2 阶段并行（**建议提前启动，有采购 lead time**）
- `T4.3`–`T4.9` 各面板之间无依赖，可分头推进
- `T3-P1`–`T3-P4`（二期三层模板）**不占一期工期**

**第一周建议顺序**

```
T1.1 → T1.3 → T1.5   （立骨架：目录 / 双 venv / DB / 队列）
T1.2 → T1.4          （配置 + 16 态状态机）
T1.6 → T1.7          （Worker + 日志/WS）
T1.8 ✅              （LLM 网关）
T1.9 → T1.10 → T1.11 （Agent 链条 + 确认闸）
T1.12 ✅             （一键启动）
```

---

## 9. 进度总表

| 阶段 | 任务数 | 已完成 | 里程碑 | 门禁状态 |
| --- | --- | --- | --- | --- |
| **T1** 基座 + 脚手架 + 选题池 | 12 | **12**（T1.1–T1.12 全部 ✅） | M1 | [ ] |
| **T2** CosyVoice 配音 | 9 | **7**（**T2.1 ✅ T2.2 ✅** T2.5 ✅ T2.6 ✅ T2.7 ✅ T2.8 ✅ T2.9 ✅）+ **2 部分**（T2.3 🔶 T2.4 🔶） | M2 | [ ] |
| **T3** 渲染（一期单遍合成） | 7 | **4**（T3.2 ✅ T3.5 ✅ T3.6 ✅ T3.7 ✅）+ **3 部分**（T3.1 🔶 T3.3 🔶 T3.4 🔶） | M3 | [ ] |
| **T4** 操作台 + 四池 + 无人值守 | 14 | **14**（T4.1 ✅ T4.2 ✅ T4.3 ✅ T4.4 ✅ **T4.5 ✅** T4.6 ✅ T4.7 ✅ T4.8 ✅ T4.9 ✅ T4.10 ✅ T4.11 ✅ T4.12 ✅ T4.13 ✅ **T4.14 ✅** —— **T4 齐了**） | M4 | [ ] |
| **T5** 发布 + 定时 + 报告 | 8 | **5**（T5.1 ✅ T5.2 ✅ T5.3 ✅ **T5.5 ✅** **T5.6 ✅**） | M5 | [ ] |
| **T6** 追加任务（设置面板） | 1 | **1**（T6.1 ✅） | — | *不占一期工期* |
| **合计（一期）** | **50** | **42**（另有 **5** 项部分完成） | — | — |
| *T3-P1…T3-P4* | *4（二期）* | *0* | — | *不占一期工期* |

**外部阻塞项**：~~E1 跑酷素材~~ ✅ **已入库 2026-09-18（58 条）** / ~~E2 水印 PNG~~ ✅ **已做 2026-09-18（640×150 rgba）** / ~~E3 BGM~~ ✅ **已有占位** / E4 原声 🟡（占位素材无音轨，不影响出片） / ~~E5 CosyVoice 权重~~ ✅ **已就位 2026-09-17** / ~~E6 LLM Key~~ ✅ **面板可配 2026-09-17** / E7 persona 🟡 / **E8 字体 🟡（唯一还需要你出手的一项）**
> ⇒ **当前无任何硬阻塞**。E8 的现状：字幕**已经能烧**，只是走的是"退到 `C:/WINDOWS/Fonts`"那条降级路 ——
> 渲染结束会打一句降级说明（§04.2.6 要求字体放 `templates/<模板>/assets/fonts/`）。
> 需要你放一个**可自由分发**的中文字体（思源黑体 / Noto Sans SC 之类）进去：微软雅黑是商业字体，
> 我**不替你提交**（会污染仓库授权）。目录已经建好了：`templates/douyin_9x16_default/assets/fonts/`。

> **「部分完成」口径（2026-09-17 核对）**：总表只把**整块做完**的任务计进「已完成」；主干已落地、但仍有明确缺口
> 的记作 **🔶 部分完成**（未做的子项在任务块里逐条标 `[ ]` 并写了理由与落点）—— **不计入**「已完成」，也**不算未开工**。
> 当前 T3 有 3 项部分完成：
> ① `T3.1` 素材入库 —— 缺 pHash + 帧哈希、黑帧段落排除、`studio assets ingest` / `stats` CLI、`tests/integration/test_broll_ingest.py`（**前两项用户已裁定为非核心**）；
> ② `T3.3` 单遍编译器 —— 缺语法预检、节点守卫 / 分块降级（**刻意不做**，见 `src/studio/render/degrade.py`）、`studio render plan`；规格里的 `CompositePlan` 实际名为 `CompositeRequest`；
> ③ `T3.4` 单遍合成执行器 —— `composite_hash` 整片缓存与**超时杀进程树**均已接上（2026-09-17），仍缺 ffmpeg 进度解析与 2Hz 限流、`BELOW_NORMAL_PRIORITY_CLASS`。

> 另 `T2.3` 引擎路由为 **🔶 部分完成（2026-09-18）**：**配音池 → 常驻服务**这根管子已打通并真机验证
> （成片里念的是 CosyVoice 的 `bigbear`，不再是系统语音包），未做的是正式 `VoiceEngine` ABC、
> 多引擎路由、熔断、以及 §04.3.3 决策表的逐条落地。
> `T2.4` 音色注册亦为 **🔶 部分完成**：目录契约 / 四条硬拒 / 入库命令 / 解耦 / 来源登记已就位（真机跑通），未做的是三条 `warn` 级检查（裁定 287）与试听样本生成（卡 E5）。
> 注 1：`T1.2` 含 **T1.2+ persona 可编辑改造**（人物库 / 热重载 / 一键切换 / 自动备份）。E7 现有 2 套可跑人物（`persona_default` 熊大熊二 · `solo_commentary` 快嘴单人），**口吻 / 受众 / 禁区仍待你定稿内容**（**可编辑性已就位**，见注 4）。
>
> 注 2：**M1 仍未算过**（T1.12 裁定 108）：一键启动已可用，但「5 进程全 ready」只差 `tts`（T2.2）——
> `draft`（T4.11）· `voice`（T2.6）· `render`（T3.7）都已注册真实 handler ⇒ 2026-09-16 实测 `spawned = api/draft/voice/render`。
> ⇒ M1 的验收口径 = 「`api` ready + 其余**如实报降级**且不阻塞」。**E5（CosyVoice 权重）已于 2026-09-17 就位** ⇒ `T2.1` ✅，剩下的只是 `T2.2` 常驻服务；当初为绕开它先做了 T4.1（前端脚手架，P1，2026-09-14 ✅）。
> ⇒ **2026-09-17 `T2.2` 落地后**：`tts` 的 `server_missing` / `env_missing` 两条判据都已消解，五个进程的入口、解释器、`PYTHONPATH` 都齐了。
> **但「5 进程全 ready」仍未真机复核** —— 你机器上 8787/8788 跑着自己起的服务，其中 `tts` 是 T2.2 落地**之前**起的
> （系统 python ⇒ `/health` 回 200 但每句报 `no module named torch`，正是裁定 307 说的第三种结果），**没动它**。
> 复核方式：`停止.bat` → `启动.bat`，`tts` 会改用 `tts/.venv` 解释器重启。
>
> 注 3：**下一批可开工任务**（依赖已满足）——
> ① ~~`T2.5` 文本归一化与切分~~ ⇒ **已完成（2026-09-15）**，见上方任务块。
> ② **`T3.1` 素材入库补全**（P0 · 依赖 T1.3 ✅）：T4.8 已建好扫盘 / sha256 / 时长 / 响度 / 缩略图 / 入库主干，**缺口** = pHash + 帧哈希、黑帧段落排除、`studio assets ingest --kind parkour|bgm` CLI、`tests/integration/test_broll_ingest.py`；E1/E3 用 `scripts/seed_placeholder_assets.py` 的占位素材撑门禁。
> ③ ~~`T3.2` 水印资产与合成 profile~~ ⇒ **已完成**（水印参数模型 / PNG 实测 / 编译期摆放 / `studio render profile --show` / `tests/unit/render/test_watermark.py`）。**硬门禁被推翻**：水印缺失改为**跳过水印层**，成片优先（理由见 `render/watermark.py` 的模块注释）。
> ④ ~~`T2.6` 按句合成流水线 + 句级缓存~~ ⇒ **已完成（2026-09-16）**，见上方任务块。
> ⑤ ~~`T2.7` 时长时间轴~~ ⇒ **已完成（2026-09-16）**，见上方任务块。
> ⑥ ~~`T2.8` 配音阶段编排与降级演练~~ ⇒ **已完成（2026-09-16）**，见上方任务块。`render_service` 那条一次性配音路径
> 已换成"池 + `settle_voice`"，`timeline.json` 的写入者从两个变回一个（**陷阱 #108 结案**）。
> ⑦ ~~`T2.9` 配音服务化操作接口~~ ⇒ **已完成（2026-09-16）**，见上方任务块。五个端点（`GET /sentences` · `GET /voices` ·
> `POST /sentences/{id}/resynth` · `GET /media/{path}` · `PATCH /tasks/{id}/voice_map`）已就位，`T4.5` 的依赖满足了。
> ⑧ ~~`T4.5` ④ 配音面板~~ ⇒ **已完成（2026-09-16）**，见上方任务块。逐句进度条 + 换音色（二次确认）+ 重配某句 + 试听单句
> 全部落地，**T4 齐了（含随后的 T4.14）**。真机演练（真 API / 真库 / 真 wav）把"重配 ⇒ worker 认领 ⇒ 念完 ⇒ 回 `done`"与"换音色 ⇒ 409 先算代价"
> 两条链路都走通了；顺带按契约修掉两处后端问题（裁定 250/251）。
> ⑨ ~~四屏串联~~ ⇒ **已完成（2026-09-16）**，见 `T4.14`。四屏各带「去下一屏 →」，跳转语义落在 `stores/ui.ts`
> （`goTo` / `takeHandoff`）；一条选题走完 选题 → 稿件 → 配音 → 渲染，**不手抄任务号**。
> ⑩ ~~`T5.1` 快速封面 + 发布前二次校验~~ ⇒ **已完成（2026-09-16）**，见上方任务块。封面三层拆分（纯函数 / ffmpeg 合成 / Agent 文案）、
> 三道门禁 + 禁区扫描全部落地；真机把「封面出图」与「门禁真的拦住」两条都验了（裁定 255–261，陷阱 130–134）。
> ⑪ ~~`T5.2` 发布适配层与 profile~~ ⇒ **已完成（2026-09-16）**，见上方任务块。`Publisher` ABC + 通用八步
> `PlaywrightPublisher` + 一线三平台子类 + 二线四个空实现 + 选择器集中化（yaml 可热修）+ 本地靶页 +
> `studio publish dry-run`。真机两条都验了（裁定 262–267，陷阱 135–139）。
> ⑫ ~~`T5.3` 发布池 + 限频 + 失败转人工~~ ⇒ **已完成（2026-09-17）**，见上方任务块。`JobStore.defer`（顺延原语，
> **不消耗 attempts**）+ `UnitDeferred` + `publish/ratelimit.py` + `publication_repo.py` + `publish_worker.py`
> + `workers/run_publish.py` + REST 六端点 + CLI 五命令全部落地；**四个池的 handler 都齐了**（裁定 268–275，陷阱 140–145）。
> 出厂仍是 `publish.enabled=false`，真发布一律 `PUBLISH_DISABLED` 转人工（R14）。
> ⑬ ~~`T5.5` 发布面板~~ ⇒ **已完成（2026-09-17）**，见上方任务块。八区块（规格七区块 + 失败/已取消）+
> 交付包（预览 / 导出，**预览不写盘、导出留痕**）+ R2 来源登记留档（发布面板与素材库常驻同一份）+
> `docs/runbook/publish_account.md`；`manual_required` 三连按钮的可用性由服务端 `can_*` 决定
> （裁定 293–299，陷阱 155–157）。~~**发布进程接进 supervisor**~~ ⇒ **已完成（2026-09-18）**：
> `publish` 已是**第六个**受管进程（`SERVICE_NAMES` / `build_specs` / `WATCHDOG_SERVICES` / `pools.yaml:guard_services`
> 四处同步），`service start` 后六进程全 ready（裁定 274 的前提已经落地，见下方 ㉒）。
> ⑭ ~~**一键出片面板**（把整条链路图形化）~~ ⇒ **已完成（2026-09-17）**，见上方 `T4.14+` 任务块。
> `services/pipeline_service.run_task` 一直只有 CLI、没有 REST 面 —— 这一屏就是补上它：五个端点 + 一块面板
> （填任务号 / 选落点 → 看进度 → 就地播成片），**同一个任务连点两次不会开两条**。它也是"第一支 MP4 的入口"：
> 配音那一步就地借 worker，**不需要先把常驻池起起来**（裁定 276–281，陷阱 146–148）。
> ⑮ ~~**第一支 MP4 端到端打通**~~ ⇒ **已完成（2026-09-17）**：文案 → 配音 → 渲染出片整条链路真机跑通
> （隔离家目录 · 真 SAPI · 真 ffmpeg · 1080×1920 h264 + aac · 16.3 MB · 23.63s · 4 句 0 降级），
> 见 `T2.4` 任务块的「端到端真机出片」一段。顺带修掉一个**会让成片没人声**的回归（裁定 290–292 / 陷阱 #154）。
> ⑰ ~~**设置面板（LLM 通道与密钥）**~~ ⇒ **已完成（2026-09-17 · T6.1）**：密钥不再需要改代码或配环境变量，打开「设置」面板填即生效（`config/secrets.yaml`，**不入库**，env 优先）；
> 探测判定 CLI 与面板**共用一份**（裁定 300–303）。
> ⑱ ~~**E5 CosyVoice 权重**~~ ⇒ **已就位（2026-09-17）**：源码 revision 锁定 + 权重 21 文件 / 5.23 GB + 真机合成读数（加载 10.4s / 显存 2.38 GB / 13 字 8.4s → 7.72s 音频），见 `T2.1` 任务块。
> ⑲ ~~**T2.2 常驻推理服务 + 并发标定**~~ ⇒ **已完成（2026-09-17）**，见上方任务块。`src/studio/tts/server.py`
> （五端点 + 429 背压 + 空闲卸载 + 看门狗）+ `src/studio/tts/cosyvoice.py`（fp16 常驻后端）+ `scripts/bench_tts.py`
> （真机标定 ⇒ 并发 **1**，已回写 `pools.yaml` / `tts.yaml`）；`tts` 进程改用 `tts/.venv` 解释器 + 前置 `PYTHONPATH`
> （裁定 305–308，陷阱 162–163）。
> ⑳ ~~**T2.3 薄片：配音池 → 常驻服务**~~ ⇒ **已完成（2026-09-18）**：成片里念的是 CosyVoice 的 `bigbear`，不再是系统语音包（真机验过，见 T2.3 任务块）。熔断 / 决策表 / 正式 `VoiceEngine` ABC 仍缺。
> ⑴ ~~**这根管子真的在生产路径上通了**~~ ⇒ **已完成（2026-09-18）**：全链路真机出片，4 句全 `cosyvoice2` / `bigbear` / `tts_attempts=0` / 24kHz，
> 成片 28.2 MB、38.0s、音轨 `mean -18.4 dB`（**不是静音**）。上一版静音成片是 11.8 MB —— **差距就是人声**。
> 过程中挖出三个**只会在真机上现形的时序坑**：旧 tts 实例占着 8788 而 `ready:false`（守护进程只看状态码 ⇒ **永远不重启它**）、
> 子环境入口借服务层的骨架函数⇒拖进整层依赖、以及引擎在**装配期**定一次会把整条链路钉在 SAPI 上
> （模型要加载 22s，而启动器**同时**拉五个进程）。裁定 312–314，陷阱 166–167。
> ㉑ **下一件待你定**：①`T2.3` 剩下的部分（熔断 + §04.3.3 决策表逐条 —— 现在只有「引擎宕 ⇒ 重试 ⇒ 到线降级」那一条）；
> ②~~把 `publish` 进程接进 supervisor~~ ⇒ **已完成（2026-09-18）**，见 ㉒；
> ③~~`T5.4` 数据回收~~ ⇒ **已完成（2026-09-18）**，见 ㉓；
> ④~~补 `T3.1` 素材入库缺口~~ ⇒ **你已明确降级为非核心**（本轮再次确认：不做 pHash / 黑帧 / 严格水印门禁）。
>
> ㉒ ~~**核心链路四段真机打通**~~ ⇒ **已完成（2026-09-18）**：素材导入 → AI 文稿 → 字幕水印自动剪辑 → 成片发布管理，
> 一条命令都不用改代码就能走完。真机读数（同一个任务 `01M2SC0XKKXYKJ5C19N33Z849N`）：
> - **素材**：`POST /assets/ingest` ⇒ 58 created / 0 rejected；`/assets/stats` ⇒ `usable=58`、`degraded=false`；
> - **文稿**：`topics analyze` ⇒ **8 个方向**（本地 qwen2.5:7b 兜底）⇒ `topics ideate` ⇒ **32 个选题** ⇒ `script draft` ⇒ 414 字 / 29 句 / ~83s；
> - **剪辑**：`pipeline run` ⇒ 成片 **238 MB / 384.6s / 1080×1920@30 / h264+aac**，字幕 29 句已烧、水印已贴、BGM 已混、响度实测 **−16.30 LUFS / −1.08 dBTP**；
> - **发布**：`publish cover` 出封面 ⇒ `publish precheck` **四道门禁全绿**（watermark / loudness / similarity / forbidden）
>   ⇒ `publish dry-run --target fixture` 走完前七步、停在第 ⑥ 步之前（截图存 `data/work/<task>/publish/fixture/`）。
> 顺带挖出并修掉**三个真问题**（裁定 316–318 / 陷阱 169–171）：本地通道的语法约束解码、
> 「迟到的失败」夺权、限幅器余量刚好卡在门禁线上。**发布进程接进 supervisor** 也在这一轮落地。
>
> ㉓ ~~**`T5.4` 数据回收 + 记忆沉淀闭环收口**~~ ⇒ **已完成（2026-09-18）**：采数时点 / 落库 /
> 面板按钮三件都落地，**发布面板「数据回流」现在能按**。
> - **后端**：`publish/metrics.py`（读数解析 `1.2万`/`1,234`/`12.5k`、时点推进、失败顺延 ≤3 次）+
>   `publish/memory.py`（回流 `data/feedback/auto_YYYYMM.md`、低互动降权、`grounded_on.source='auto'` 回填）+
>   `services/publish_metrics_service.py`（一拍）+ `app/recycle.py`（60s 起跳）+ 三个 REST；
> - **前端**：`api/endpoints/publish.ts` 三函数 ⇒ `stores/publish.ts` 三动作 ⇒ `views/Publish.vue`
>   区块头「跑一轮」+ 逐条「采数 / 沉淀」+ 「上一轮：…」结论行；
> - **真机**：`db migrate` 应用 **0010_metrics**（`publications.metric_attempts`，迁移前自动检查点
>   `data/backups/checkpoints/premigrate_20260918-135900.db`）⇒ 六进程重启全 ready（`http://127.0.0.1:8787`）；
>   `POST /api/v1/publish/metrics/tick` ⇒ 200 `{collected:[], pending:0}`；面板已加载新 bundle（`index-BRLYS1FR.js`）；
> - **门禁**：`.\tasks.ps1 check` ⇒ **3832 passed / 32 skipped**；`.\tasks.ps1 web:verify` ⇒ **435 passed · dist 0.36 MB**；
> - **裁定 319–321 · 陷阱 172–174**；遗留 ⏳ 只剩**评论抓取**（各平台评论页选择器未写，§06.8 ① 目前恒为 0，②③ 照常）。
>
> ㉔ **`T5.9` 本地演练台 + `T5.10` 面板投递区块** ⇒ **已完成（2026-09-18）**：**没有真账号也能把后半条链路真跑一遍**。
> - **演练台**：`platforms.other`（发布器 = 本地靶页）+ 账号 `_rehearsal`（profile 物理隔离）；
>   **完播率**进契约（`completion_rate`，0–1 比值）与靶页（22.0–68.9%，由作品号确定性算出）；
> - **真机**：面板勾「本地演练台」投递 ⇒ publish 池**真发布**（Playwright 打本机靶页）⇒
>   `published` / `dry_run=false` / `rehearsal-1789715614909` ⇒ 采数 ⇒ `播放 128524 · 赞 28488 ·
>   评论 478 · 转发 256 · 完播 42.6%` ⇒ 沉淀 ⇒ `planner_consumable=true`；
> - **面板**：新增第 ② 块「投递」（任务号 + 平台勾选 + 结论行），清单来自 `GET /publish/platforms`
>   —— **T5.5 的面板此前只能看不能投**，投递只有 REST 与 CLI 两条路；
> - **门禁**：`.	asks.ps1 check` ⇒ **3867 passed / 32 skipped**；`.	asks.ps1 web:verify` ⇒ **451 passed · dist 0.36 MB**；
> - **裁定 322–324 · 陷阱 175–178**。本轮顺带**修掉三处文案假话**（"开关关着会转人工" ⇒ 真实落点是**死信**）。
>
> ㉕ **`T5.6` 定时发布调度** ⇒ **已完成（2026-09-18）**：M5 门禁里「**定时**自动发布」那半句落地 —— 发布池从此不只等人按。
> - **后端**：`domain/schedule.py`（时刻算术纯函数：窗口内取时刻 = `HMAC(schedule_id, 'base|日期')`，抖动另取一个 salt 并**夹在窗口边界内**）+
>   `db/repositories/schedule_repo.py` + `services/scheduler_service.py`（30s tick · 到点才建 job · 限频 ⇒ `skipped_ratelimit` 顺延 · 开关关着 ⇒ `skipped_disabled` · 连败 5 次 ⇒ `SCHEDULE_FAILING` 告警）+
>   五个 REST（列表 / 新建 / 改（含启停）/ 删 / 立刻跑一次）+ 两条 WS 事件（`publish.scheduled` / `publish.schedule_fired`）；
> - **表早就有了**（`0005_schedule_report.sql` 的 `publish_schedules`）⇒ **本轮零迁移**；
> - **前端**：面板第 ⑪ 块「定时计划」从「尚未施工」换成**真 UI**（列表 + 立即执行 / 启停 / 删除 + 建计划表单：平台 / 模式 / 窗口 / 时刻 / 间隔 / 抖动 / 任务号 / 启用），
>   15s 慢轮询（这一块轮询的是**时间本身**，空闲也要问）；
> - **真机**：建计划 ⇒ `next_run_at` 落在 18:00–21:30 窗口内（本地 18:09）；总开关关着 ⇒ `skipped_disabled`；
>   打开开关 ⇒ 撞幂等 ⇒ **`skipped_duplicate` + `fail_streak=0`**（修之前是 `error:PUBLISH_FAILED` + `fail_streak=1`）；`DELETE` 带留痕；验完已把开关改回出厂值；
> - **门禁**：`.\tasks.ps1 check` ⇒ **3892 passed / 32 skipped**（+25）；`.\tasks.ps1 web:verify` ⇒ **482 passed · dist 0.37 MB**（+31）；
> - **裁定 325–333 · 陷阱 179–182**。本轮**修掉一个真 bug**：面板把 UTC 时刻当本地时间显示（陷阱 179）。
>
> ㉖ **`T5.7` 数据报告与决策闭环** ⇒ **已完成（2026-09-18）**：M5 的最后一块 —— 数据回流上来的原始指标从此会变成**结论与建议**。
> - **后端**：`domain/report.py`（周期算术纯函数：`period_bounds` 右端 = 触发日**前一天**、`next_run_at` 逐日扫 400 天、
>   `confidence_for` 分档）+ `db/repositories/report_repo.py` + `services/report_service.py`（**一条 SQL** 取
>   `publications ⋈ tasks ⋈ topic_candidates`，中位数/归因全在 Python 里算 —— **一个模型都不调**）+
>   `app/schemas/report.py` + `app/routers/reports.py`（**9 端点**）+ `AlertCode.REPORT_FAILING`（`warn`）+
>   两条 WS 事件；`app/recycle.py` 的 `SchedulePump.report_tick()` 是**第二个独立 try**（一边炸不能带走另一边）；
> - **表早就有了**（`0005_schedule_report.sql` 的 `reports` / `report_schedules`）⇒ **本轮零迁移**；
> - **决策闭环落在 `persona.style_hint`**（裁定 334）：`content_directions` 是**历史批次**，改它的 priority
>   对下一轮 Planner **没有任何影响** ⇒ 那会是"点了采纳但选题一点没变"的假闭环。`style_hint` 才是每个 Agent
>   每次都读到的公共输入；写入仍走 `PersonaStore`（校验 + 备份 + 留痕），只保留最新 5 条结论、用户自己写的风格一行不丢；
> - **前端**：面板第 ⑫ 块「报告」从「尚未施工」换成**真 UI**（报告列表 + 详情 + 建议卡片（采纳 / 已采纳禁用 / `low` 强制警示）
>   + 导出 md·csv + 周期编辑表单（period / weekday / day_of_month / at_time / lookback_days / include / enabled）），
>   60s 慢轮询（一份报告只可能在**到点那一刻**多出来，问得再勤也不会早一秒）；
> - **真机**：`generate` 幂等（同窗口再发一次 ⇒ **同一个 id**，`counts.total=1`）；`run_now` ⇒ `ok` + 新报告；
>   导出 csv ⇒ 200 + `Content-Disposition` + `text/csv`；seed 三条周期 `next_run_at` 起初为 `null`，
>   **api 起来跑一拍后自动补上**；启停 ⇒ 当场算 / 置回 `null`；删内置的 ⇒ 400 说清"只能停用"；面板目视通过；
> - **门禁**：`.\tasks.ps1 check` ⇒ **3921 passed / 32 skipped**（+29）；`.\tasks.ps1 web:verify` ⇒ **544 passed · dist 0.39 MB**（+62）；
> - **裁定 334–340 · 陷阱 183–184**。本轮**修掉两个真问题**：出厂自带的周报/月报一次都不会跑（陷阱 183）、
>   枚举数量再次进断言与文案（陷阱 184，同 180）；
> - 遗留 ⏳：`T5.8` 多账号（结构已就绪）· `publish.ts` 的 `formatStamp` 同款 UTC 问题（T5.5 遗留）· **E8 字体**（`templates/douyin_9x16_default/assets/fonts/` 仍空，字幕走系统字体降级）。

> **当前关键路径**：`T2.1 ✅` ⇒ `T2.2 ✅` ⇒ `T2.3 🔶`（**管子已通**，剩熔断与决策表）⇒ **`T2.4` 正式音色** ⇒ `T3.3`（时间轴 ✅，可直接开工）⇒ T3.4–T3.7 ⇒ `T4.6` 与 `T5.1` 起全部。
> ⇒ **没有任何硬阻塞**；E1/E2/E3/E4 只影响各自任务的真机验收，**不影响开发推进**。
>
> 注 4：**口吻 / 受众 / 禁区是数据，不是代码**（本轮的明确要求，现状核对如下）——
> - **落在哪**：`config/personas/<id>.yaml` 的 `tone` / `audience` / `forbidden`（外加 `catchphrases` / `role_desc` / `style_hint` / 篇幅阈值）；
> - **怎么改**：WebUI **人物库面板**（`web/src/views/Personas.vue`：`tone`/`audience` 文本框、`forbidden`/`catchphrases` 逐行文本域）或直接改 YAML（**热重载**，见 `PersonaStore`）；
> - **生效链路**：`agents/base.py` 把 `tone`/`audience`/`forbidden`/`catchphrases` 注进 `prompts/shared/persona_block.md` 的 `{{...}}` 占位符 ⇒ 每个 Agent 的提示词；`writer.py` 另按 `persona.forbidden` 走**规则通道**逐词扫描；`ScriptRules.from_persona` 让字数 / 时长 / 句长阈值也跟着人物走；
> - **校验与回滚**：`validate` 不通过 ⇒ **拒绝保存**（一个字节都不写）；切换人物自动备份旧版到 `data/backups/persona/`，可回滚；
> - **结论**：改口吻 / 受众 / 禁区**不需要改代码、不需要重启**；系统里没有任何一处把它们写死。

---

## 10. 高频陷阱速查（施工时对照）

| # | 现象 | 根因 | 正确做法 | 关联 |
| --- | --- | --- | --- | --- |
| 1 | `database is locked` | 长事务 / 事务内做 I/O | 短事务（<20ms）+ WAL + `busy_timeout=5000` | T1.3 |
| 2 | 任务被重复执行 | 无租约或租约过长 | 单语句原子认领 + 租约 + sweeper | T1.5 |
| 3 | 画面冻结/提前截断 | 未显式指定总时长 / VFR / 用了 `-shortest` | **音频为时长基准** + CFR + 显式 `-t` + **禁 `-shortest`** | T3.3 |
| 4 | 人声偏小 | `amix` 默认 `normalize=1` 衰减 | `amix` 显式 `normalize=0` + 两遍 `loudnorm` | T3.6 |
| 5 | 字幕豆腐块 | libass 找不到字体 | 内置字体 + `fontsdir` + 启动校验 | T3.5 |
| 6 | `ass` 滤镜路径报错 | Windows 冒号/反斜杠未转义 | 统一 `ff_path()`，驱动器冒号转 `\:` | T3.3 |
| 7 | 滤镜图 `Invalid argument` | 命令行超长 / 引号错配 | `-filter_complex_script` 文件 + 语法预检 | T3.3 |
| 8 | 渲染缓存复用旧产物 | 哈希未包含输入/配置 | `composite_hash` 含 canonical plan + 输入 sha256 | T3.4 |
| 9 | 崩溃留"假完成"产物 | 直接写目标文件 | 先写 `.partial` 再 `os.replace` | T3.4 |
| 10 | TTS 首句延迟 20s+ | 模型未预热 / 每句重载 | 常驻服务 + `/warmup` + 空闲卸载 | T2.2 |
| 11 | 显存 OOM（8GB 卡） | fp32 / 并发过高 | fp16 + **默认单并发** + 空闲卸载 + **禁 bf16** | T2.2 |
| 12 | 前端被进度刷爆 | 高频逐帧推送 | 合并窗口 100ms + 2Hz 限流 + 环形缓冲 | T1.7 |
| 13 | 日志断线后丢失 | 先广播后落库 | **先落库再广播** + `since_id` 补发 | T1.7 |
| 14 | 素材被判搬运 | 固定素材/固定入点/固定编码 | 随机化两档 + 相似度审计门禁 | T3.1 / T3.3 |
| 15 | C 盘爆满 | 临时文件/模型缓存落在系统盘 | 环境变量重定向 + 磁盘门禁 | T1.1 / T4.11 |
| 19 | LLM 成本失控 | 批量选题 × 6 Agent × 2 轮改稿 | token 预算上限 + 成本面板 + 超限切本地 | T1.8 |
| 20 | 发布不可逆事故 | 自动发布无二次校验 | `publish.enabled=false` + `require_confirm=true` + 三道门禁 | T5.1 |
| 21 | 登录态失效被静默跳过 | 未探测登录态直接上传 | 发布前 `health()` 探测 ⇒ 转 `manual_required`（**不自动登录**） | T5.2 |
| 23 | 平台改版选择器全失效 | 选择器散落在代码里 | 集中 `selectors/*.yaml` + 版本留痕 + 失败截图 | T5.2 |
| 24 | 发布文案被编辑器吞掉 | 富文本编辑器截断/丢 emoji | 发布后**回读逐字比对**，不一致重填 ≤2 次 | T5.2 |
| 26 | 单句编辑后音频错位 | 增量拼接时间轴 | 任一句重合成 ⇒ **时间轴全量重算**（禁增量） | T2.7 |
| 27 | 成片没水印 | 水印缺失被当成"可选"静默跳过 | **已接受**（口径改为可选装饰）：缺失即跳过 + `skipped_reason` 留痕；要"必须带水印"就放发布前校验（T5.1） | T3.2 / T5.1 |
| 28 | 素材比人声短 ⇒ 黑屏/冻结 | 未做循环补齐 | `loop` + `trim=duration=total_ms` 补齐；空素材 ⇒ 纯黑底仍出片 | T3.3 |
| 29 | 定时发布变"每天准点" | 固定时刻发布 | `daily_window` 窗口内随机 + `jitter_min` | T5.6 |
| 30 | 定时任务重启丢失/重复触发 | `next_run_at` 只在内存 | `next_run_at` **落库** + HMAC(seed=id+日期) 幂等 | T5.6 |
| 31 | 报告被当"结论"改生产策略 | 数据噪声 / 样本不足 | `insights` **必须人工采纳**；`n<10` 强制警示 | T5.7 |
| 32 | 报告生成烧钱 | 用 LLM 写总结 | 纯 SQL 聚合 + 规则归因（**不调 LLM**） | T5.7 |
| 33 | 改了定时/报告周期却不生效 | 编辑计划只改了参数、没重算 `next_run_at` | 任何策略编辑 ⇒ **同事务重算 `next_run_at`** + `audit_ops` | T5.6 / T5.7 |
| 34 | **从另一个控制台发 Ctrl-Break：API 返回 TRUE，目标却收不到**（进程照旧在跑，最后被硬杀 ⇒ 丢进度） | `GenerateConsoleCtrlEvent` 只作用于**同控制台**的进程组；`停止.bat` 是另起控制台 | 关停主通道 = **标志文件** `data/logs/<name>.stop`（池 worker 在 1s 脉冲 tick 里看到就 draining）；信号只作升级手段 | T1.12 |
| 35 | **启动后 worker「什么都不干」** | 上一轮关停留下的 `.stop` 标志没清，worker 起来第一拍就自己关掉 | `start` 的第 ② 步 `clean_stale()`：**先清标志与陈旧台账再拉进程**；`stop` 结束后也必须清 | T1.12 |
| 36 | **把「端口被占用」报成「已在运行」**（或反过来：重复拉起第二个实例） | 只探测端口、不看 PID 台账 | 两个判据分开：`already_running`（台账里有活进程）vs `port_busy`（端口被**别人**占） | T1.12 |
| 37 | **对 uvicorn 等「优雅退出」白等 6 秒** | HTTP 面的进程没有 tick 循环，**永远**不会去读标志文件 | `ServiceSpec.polls_stop_flag` 区分：池 worker `True`（等标志），HTTP 进程 `False`（直接走信号） | T1.12 |
| 38 | **子进程起来就死，却看不到原因**（日志全丢） | 没有重定向子进程 stdout/stderr，`Popen` 的管道没人读 | 追加写 `data/logs/<name>.log` + `PYTHONUNBUFFERED=1` + `stdin=DEVNULL` | T1.12 |
| 39 | **`DraftReport.created_task` 在复用任务时说谎** | 幂等键命中时 `create()` 原样返回旧行 ⇒ 光看返回值分不出新旧 | 建任务前先 `find_by_idempotency_key()` 问一次；测试必须断言**第二次 `created_task=False`** | T1.10 |
| 40 | LLM 自报总分 ⇒ 定级不可信 | 把 `total`/`grade` 放进模型输出 schema | 模型**只给六维度 + `issues`**；定级一律服务端算 | T1.11 |
| 41 | `need_edit` 被当成"一定要改稿" | DDL 的 `decision` 只有 5 值、没有"等人工" | `Decision`（事实）与 `GateAction`（动作）拆两个枚举 | T1.11 |
| 42 | 并发退回丢更新 | `revision_round` 用"读到的旧值 + 1" | SQL 增量 `revision_round = revision_round + 1` | T1.11 |
| 43 | 待审列表里有一条永远点不掉的记录 | 自动放行走 `request()` + `decide()` **两个事务** | `record_auto_approval()` **一次事务**写 `approvals` + `audit_ops` | T1.11 |
| 44 | 改稿越界判失败 ⇒ 比"稍越界"更贵 | 越界即 `EDIT_FAILED` | 重试 1 次后**照收 + `warnings`** | T1.11 |
| 45 | 人工退回后重审被 `ValidationError` 炸掉 | `ReviewerInput.round_no` 设了上界 | 输入模型**只保下界**；轮次闸门由 `gate_action` 把守 | T1.11 |
| 46 | `import file mismatch` 中断整轮测试收集 | 同名测试文件且都不带 `__init__.py` | 同名测试目录补 `__init__.py` | T1.11 |
| 47 | 改稿越界检查永远"无越界" | `_edit` 把**空句子列表**传给 Editor | 透传 `sentence_rows` | T1.11 |
| 53 | 测试里漏 `request_stop()` ⇒ worker 永不返回 + 脉冲线程泄漏 | 测试只 `join()` 不请求停止（`run()` 没收到停止就永不返回，这是生产语义） | 登记表 + autouse 守卫兜底停；断言 `not thread.is_alive()` | T1.6 |
| 54 | **mypy「同一文件两个模块名」顺带掩盖真实类型错误** | 测试目录缺 `__init__.py` 却写 `from tests.x.y import z`（mypy 直接跳过该文件） | 补 `__init__.py` 链；修完重复名后**必须重跑** `mypy` | T1.7 |
| 55 | **`llm_calls` 同毫秒内乱序**（「第几次尝试」随机错位） | `created_at` 只到**毫秒**，`new_ulid()` 同毫秒内是随机后缀 | 定序一律 `ORDER BY created_at DESC, rowid DESC`；测试断言正序 = 翻转 `recent()` | T1.8 |
| 56 | **反复重试绕开预算闸门** | 只记成功调用 ⇒ 失败重试不烧账、预算永远不超 | **失败也写 `llm_calls`**（`schema_invalid`/`http_error`/`timeout` 都记 token 与成本）；记账失败不得中断主链路 | T1.8 |
| 57 | **提示词模板静默退化** | 模板里写控制流 / 缺变量，渲染成空串继续跑 | 只支持 `{{变量}}`；遇 `{%`/`{#`/缺变量**直接报错**；`prompt_version` 由内容 sha256 决定 | T1.8 |
| 58 | **`vite.config.ts` 报 TS2769（`test` 段不是已知属性）** | `vitest@2` 与 `vite@6` 并存 ⇒ 嵌套了第二份 vite，`defineConfig` 类型来自旧那份 | `defineConfig` 取自 `vitest/config`；vitest 与 vite **同大版本**（升 3.x）。判据：`node_modules/vitest/node_modules/vite` 不该存在 | T4.1 |
| 59 | **前端某路日志字段静默 `undefined`**（面板少一截且不报错） | WS 事件用 `log_id`、快照行/REST 用 `id`；事件还缺 `trace_id`/`seq_in_task` | 两侧统一走**一个归一函数**进缓冲；契约测试断言 `LogRow` 字段集 == `SystemLog.to_dict()`；`fetch(` 收敛到 `api/http.ts` | T4.1 |
| 60 | **每重连一次白跑一轮 `resync`** | `seq` 是**每连接**单调（新连接从 1 重新开始），客户端却沿用旧基线 ⇒ 第一帧被判缺口 | `onopen` 重置 `lastSeq = 0`（只建基线不判缺口）；`since_id` 游标**只进不退** | T4.1 |
| 61 | **搜 `50%` 把整张日志表捞回来** | 用户输入直接进 `LIKE`，`%` / `_` 被当通配符 | 统一 `like_pattern()` 转义（**先转义 `\` 再转义 `%` `_`**）+ SQL 里显式 `ESCAPE '\'`；`search` 同时匹配 `message` 与 `source` | T4.9 |
| 62 | **「导出到这条为止」被静默忽略**（`until_id` 不生效，导出的比要的多） | `since_id` 为空时走「最近 N 条」分支，而那个分支**没有**窗口上界 | 窗口上界必须进 SQL（`recent(until_id=…)`），不能靠调用方事后截断 —— 截断与查询在分页边界上不等价 | T4.9 |
| 63 | **`logger.info(msg, **payload)` 抛 `TypeError`**（日志发不出去） | 事件名用了 `event` 这个键，而 structlog 的第一个位置参数就叫 `event` | 载荷键固定 `event_kind`（`core/proto.py` 的 `EVENT_PAYLOAD_KEY`）；兜底分支再过滤 `_LOG_RESERVED` | T4.4 |
| 64 | **`services/` 要声明事件名却 import 不到 `ws/`** | 分层方向是 `app → ws → services → db`，反向 import 会成环 | 事件枚举**下沉**到 `core/proto.py`；`ws/protocol.py` 原样 re-export（导入路径不变） | T4.4 |
| 65 | **改稿只加了一句，diff 却把后面全标成「改了」** | 逐 `seq` 对齐（插入一句 ⇒ 之后每一句的 `seq` 都变了） | 走 `difflib.SequenceMatcher` 的**块级**匹配；`replace` 块内按位置配对，多余一侧降级成 `insert` / `delete` | T4.4 |
| 66 | **前端要写两套错误解析** | 业务错误与 `RequestValidationError` 各返回一种形状 | 应用级 handler 把两者都翻成 `StudioError.to_dict()`（映射表住 `app/errors.py`） | T4.4 |
| 67 | **批量放行 10 条失败 1 条 ⇒ 人重按一次** | 把"部分失败"报成"整体失败" | 逐条如实返回 `approved` / `failed`（含 `code` / `remediation`），**不回滚** | T4.4 |
| 68 | **响应集合字段在前端变成 `T[] \| undefined`** | `Field(default_factory=list)` 既不进 `required`、也不带 `default` | 响应模型写 `x: list[T]`（必填）；请求体才保留默认值 | T4.3 |
| 69 | **请求体的"可选"字段在前端变成必填** | `openapi-typescript` 把带 `default` 的属性渲染成必填 | 前端显式传（`import_sources: true` / `per_direction: 4`） | T4.3 |
| 70 | **长任务单飞用 `asyncio.Lock` 跨线程炸** | 同步路由跑在 Starlette 线程池，`asyncio.Lock` 与事件循环绑定 | 进程级 `threading.Lock` 非阻塞地拿；拿不到 ⇒ 409（**不排队**） | T4.3 |
| 71 | **动作刚报的错被紧随的成功刷新抹掉** | 拉取与动作共用一条 `error` | `error`（动作级）vs `loadError`（拉取级）两条信道 | T4.3 |
| 72 | **一个用例里换第二次假件不生效** | `arm()` 的"原函数"是上一次已 patch 的假件 | 原函数取未被 patch 的那份（`gateway_factory.build_gateway`） | T4.3 |
| 73 | **假件方向数不够 ⇒ `LLM_SCHEMA_INVALID`** | schema 要 5–8 个方向、每方向 3–5 条；`ScriptedTransport` 用尽后复用最后一条 | 逐方向各给一条脚本化回应 | T4.3 |
| 74 | **查事件报 `no such column: payload`** | `system_logs` 的列名是 `payload_json` | 直接查库用 `payload_json`；读侧走 `SystemLog` / `LogRow` | T4.3 |
| 75 | **"今日"在 UTC+8 的 08:00 前算错一天** | `substr(created_at,1,10)` 是 **UTC 日**，而"今天"是本地日 | 窗口由 `local_day_window()` 算成**左闭右开**的一对 UTC 时间戳再进 SQL | T4.2 |
| 76 | **在 API 进程里"停服务"等于自杀** | `ServiceManager.stop()` 的第一个目标就是 `api` 自己 | 只暴露"启动"；停止的正门是 `停止.bat` | T4.2 |
| 77 | **采样泵在工作线程里 `publish()` 抛 `RuntimeError`** | `Hub.publish()` 末尾直接 `self._wake.set()`，而 `asyncio.Event.set()` 不是线程安全的 | 走 `Hub.wake()`（内部 `call_soon_threadsafe`） | T4.2 |
| 78 | **每次启动凭空多一条"磁盘水位恢复"**（顶掉日志第一帧） | 磁盘状态机初值 `None` 被当成"低水位"，`None → ok` 也走恢复分支 | `None` = "还没采过"：首拍低位照常告警，首拍**健康一个字都不写** | T4.2 |
| 79 | **并发降到 0 = 沉默的暂停**（面板上与 `paused=1` 分不出来） | DDL 的 `CHECK (concurrency BETWEEN 0 AND 8)` 允许 0，而 0 看着像个并发数 | 旋钮下限硬编码 **1**（`POOL_CONCURRENCY_MIN`）：要停就点「暂停」—— 那条路有留痕、有语义、有「在途跑完」的说明 | T4.10 |
| 80 | **自动降并发连降 4 次，一路掉到下限**（阈值 2，连来 5 次 OOM） | 判据写成 `consecutive_oom >= threshold` ⇒ 之后每一次 OOM 都算「又越线了」 | 判据 `== threshold`；计数器由 `succeed()` 归零 ⇒ 下一次越线必然发生在「池又成功过一次」之后 | T4.10 |
| 81 | **作业重排了，OOM 计数器却没加**（窗口期恰好是连续 OOM 正在发生的时候） | 自动降级与 job 状态变更分成两个事务写 | 降级逻辑放进 `fail()` / `succeed()` 的**同一个 `BEGIN IMMEDIATE`**；配置对象在 `JobStore.__init__` 注入（事务内禁止 I/O，§03.4.6 规则 3） | T4.10 |
| 82 | **改一次 + 立刻回滚一次，回滚变成空转**（版本 +1、`sha256` 不变，从面板上看不出来） | 备份文件名只到秒（`<yyyymmdd-HHMMSS>_<id>.yaml`），同秒同名 ⇒ 后写覆盖先写 | 撞名退 `-2` / `-3`；`rollback()` **先把内容读进内存再备份当前** | T4.13 |
| 83 | **id 打错了，面板报「服务器挂了」**（HTTP 500） | `activate('ghost')` 带着 `CONFIG_MISSING` 冒到应用级 handler，而映射表里没有这个码 | 服务层翻译成 `PERSONA_NOT_FOUND`(404) / `PERSONA_INVALID`(422) / `PERSONA_EXISTS`(409)；**store 的既有语义一个字不改**（T1.2 单测逐条断言它） | T4.13 |
| 84 | **手改 `config/persona.yaml` 后面板没反应**；同一人连改两次只收到一条事件 | 广播若挂在 REST 写入口，CLI / 手改两条路径不经过它；且 `merge_field='persona_id'` 会把同人事件合并 | 广播挂在 `PersonaStore.subscribe()`（三条路径都经过 `_load_locked`）；面板收到事件只立 `draftStale` 旗子，**不覆盖脏草稿** | T4.13 |
| 85 | **给「将来会消失的错误」加了 `# type: ignore`，错误消失那天门禁反而挂了**（T2.2 落地即触发） | mypy strict 含 `warn_unused_ignores`：`workers/run_tts.py` 的占位导入在 T2.2 落地后不再报 `import-untyped`，那条 ignore 就成了多余的 | 并列写 `# type: ignore[import-untyped, unused-ignore]`；变量声明要写全 `Callable[[], None] \| None`（否则 `if x is None` 会被 `warn_unreachable` 判成永远为假）；**`mypy.ini` 的 `ignore_missing_imports` 管不了 `import-untyped`**（它只管 `import-not-found`） | T2.2 |
| 86 | **`workers/` 与 `scripts/` 从来没被类型检查过**（ruff 扫 4 个目录，mypy 只扫 2 个） | 两个工具的目标列表各写一份：`ruff.toml` 的 `src` 是 4 个，`tasks.ps1` / `Makefile` 的 `mypy` 是 2 个 | 对齐成 `src tests workers scripts`（`tasks.ps1` 两处 + `Makefile` 一处）；扩大范围后**立刻暴露出 1 个被漏掉的真实错误** | T1.1 |
| 87 | **抽公共原语时局部变量遮蔽了导入名** ⇒ `UnboundLocalError`，80 个用例一夜全红 | `_stat_key()` 方法抽成 `core/files.py` 的函数后，`_load_locked` 里写成 `stat_key = stat_key(path)` —— 赋值语句让 `stat_key` 在**整个函数作用域**里变成局部名，导入进来的那个函数被遮蔽 | 局部名不要与被导入的名字同名（改叫 `key`）；`ruff` 的 `F823` 是唯一当场喊出来的工具 —— **改了导入就立刻跑 `ruff check`**，别等 pytest | T4.7 |
| 88 | **`vue-tsc --noEmit` 全绿，`vite build` 却挂**（`Element is missing end tag`） | Vue 模板里的**裸文本** `templates/<id>/` 被当成 HTML 标签 | 模板文本里的尖括号写 `&lt;id&gt;`；验收必须跑完整 `web:verify`（typecheck 过 ≠ 能 build） | T4.7 |
| 89 | **刚上过迁移 ⇒ 恢复演练必然报红**（活库 8 个迁移，盘上最新备份 6 个） | 演练拿「活库的 schema 基线」去比「备份还原出来的库」，两边本来就该差一个版本；而真被删掉索引的坏库是**同样的红** | 判据**双条件**：① 多出的失败项全落在 `db.indexes` / `db.tables` / `db.triggers` / `db.migrations`；② 还原库的迁移集是活库的**真前缀** ⇒ 记为「备份版本落后」而不是红（`RestoreReport.schema_lag`） | T4.12 |
| 90 | **日备 03:00 刚跑完，面板上写着「11 小时前」** | 新鲜度按**文件名里那个日期的零点**算 ⇒ 一个刚成功的任务被报成快半天没动，人去查一个根本没坏的计划任务 | 按**文件写入时刻**算（`BackupFile.written_at` = `stat().st_mtime`）；`day` 只用于轮转（7 日 + 4 周）与「这是哪天的」 | T4.12 |
| 91 | **「我清过垃圾了」但盘一点没还回来**；或 **VACUUM 跑到一半失败、库只剩半条命** | `DELETE` 只把页挂进 freelist，文件不会变小；`VACUUM` 要 2 倍空间 + 独占写，直接对活库跑是拿数据冒险 | 面板报 `db_freelist_bytes`（`footprint()` **一处算**，GC 报告 / `db vacuum` / 观测面板同一个数）；`studio db vacuum` 先钉一份 `prevacuum_*.db` 检查点，**失败即中止** | T4.12 |
| 92 | **重扫一遍，人工标的启用/授权/可用区间全没了** | `upsert` 把模型里的字段全量回写，分不清「机器算出来的」与「人填的」 | 重扫**只刷机器事实**（`sha256` / 时长 / 宽高 / 帧率 / 指纹），`enabled` / `license` / `tags` / `usable_*` / `has_text` / `mood` / `bpm` 一律不动（裁定 182） | T4.8 |
| 93 | **入库了一批“合格”跑酷，到渲染那一刻才报错抽不出入点** | usable 区间只判「长度 > 0」，而 §04.2.4 的入点规则要「头 1.5s + 尾 1.5s + 一个 1.5s 候选窗口」 | `BROLL_MIN_USABLE_MS = 4500`：低于它根本抽不出合法入点，入库时就拒收并说清原因（裁定 187） | T4.8 |
| 94 | **前端还没 `npm run build`，后端的 404 / 405 全变成 500** | `StaticFiles` 挂在 `/` 上兜住了所有没匹配上路由的请求，而它在**目录不存在**时抛 `RuntimeError` —— 于是`POST /api/v1/metrics`（本该 405）、打错的路径（本该 404）统统 500 | 产物不在就**干脆不挂载**（只留一条 `app.web_dist_missing` 日志指路），路由表与从前一模一样；另外**必须挂在所有 API 路由之后**，否则 `/api` 与 `/ws` 会被它一起吃下去 | T4.6 |
| 95 | **`ass` 滤镜报 `Option not found`，或路径被截成两半** | `ass` / `subtitles` / `movie` 的参数**以 `:` 分列**，`D:\...` 的盘符冒号被当成了选项分隔符 | 路径整体加单引号 + 冒号转义：`ass='D\:/path/x.ass':fontsdir='C\:/WINDOWS/Fonts'` | T3.5 |
| 96 | **同一个输出标签被用两遍 ⇒ `Stream specifier 'a_voice' ... matches no streams`** | 滤镜图里一个输出标签**只能被消费一次**；要分叉必须显式分叉 | 人声先分叉：`[a_voice]asplit=2[a_voice_sc][a_voice_mix]`（一路进侧链、一路进混音）。**规格原模板直接写了两遍 `[a_voice]`** | T3.6 |
| 97 | **成品响度与真峰值双双越界**（实测 −15.49 LUFS / −0.85 dBTP，两边都在门禁外） | ① `alimiter` 的自动电平**默认开启**，把 `loudnorm` 刚归一好的响度又抬上去；② `limit=0.95` 比 `≤ −1.0 dBTP` 的门禁**宽**，等于没兜住 | ① 显式 `alimiter=...:level=0` 关掉自动电平；② `limit` 由门禁反推 `10**((true_peak_dbtp − 0.3)/20)` = `0.8610` ⇒ 实测 −1.21 dBTP | T3.6 |
| 98 | **BGM 整条消失、人声被叠了一份**；混音真峰值实测 **+1.91 dBTP**，成品响度掉到 −16.48（离门禁下界 `−16.5` 只剩 0.02 dB） | `sidechaincompress` 的输入焊盘是 **`#0: main` / `#1: sidechain`**，而规格 §04.2.8.3 下半段的完整模板写的是 `[a_voice_sc][a_bgm]` —— 被压的是**人声**，`a_bgm_duck` 这个标签名是假的：BGM 没进 `amix`，人声进了两次 | 写成 `[a_bgm][a_voice_sc]sidechaincompress=…`（**主路在前**）。**同一份契约里 §04.2.8.3 上半段的简图写的是对的、下半段的模板写的是错的** —— 判据取 `ffmpeg -h filter=sidechaincompress`，不取哪一段写得更详细 | T3.6 |
| 99 | **`studio pipeline run <id> --until completed` 打印成功、任务一步没动** | 判「已经越过 `--until`」用的是「状态在正向链上的位置」，而 `failed` / `editing` **不在正向链上** ⇒ 被算成「排在终点之后」⇒ 直接收工。**静默的空转比报错难查得多** | 先判「卡住 / 人工闸」（`failed` / `manual_pool` / `editing` / `discarded` / `canceled` / `awaiting_approval`），**再**判「越过 `--until`」 | T3.7 |
| 100 | **一条听起来完全正常的片子，`quality_json.lufs` 显示 −22 LUFS**，发布门禁（§06.4 门禁 2）把它拦下 | `loudnorm` 报的 `input_i` / `input_tp` 是**归一化之前**的输入读数 —— 那是「打算抬到 −16」的**起点**，不是终点；而它偏偏是链路上唯一一份现成的响度数据，最容易被顺手拿来填 | QC 必须**重量一遍落盘的成片**（`mixdown.measure_file`，纯解码 `-f null`，不编码不落盘）。manifest 里两个响度**都要留**且名字要能分清：`loudness`（loudnorm 的输入读数）/ `output_loudness`（成片实测） | T3.7 |
| 101 | **渲染面板上那条出片挂不进 `jobs` 队列** | `jobs.task_id` 有外键到 `tasks(id)`（`PRAGMA foreign_keys = ON`），而面板默认的任务号是 `ui-YYYYMMDD-HHMMSS` —— 库里根本没有那一行。这不是"顺手加个字段"能绕过去的：任务号是这条片子与稿件 / 审计留痕的挂钩 | 面板的"试片"入口**故意留在进程内**（`services/render_job_service.py` 的模块说明写了取舍）；`render/final` 单元的入队方是**有真实任务行**的那条链（§03.4.5：`voice/sentence` 全部完成 ⇒ `render/final`）。**别为了让面板走队列去自动建任务行** —— 那是产品决策，不是重构 | T3.7 |
| 102 | **把 `PoolConfig.max_attempts` 改成 1，作业还是重试了两次** | 生效值取自 **`pool_settings`**（`JobStore.enqueue` 读 `self._runtime(pool).max_attempts` 写进 `jobs.max_attempts`）；`PoolConfig` 只喂 worker 自己的循环（退避 / 单元超时） | 改"重试几次"要改库（`UPDATE pool_settings SET max_attempts = …`，或改 `config/pools.yaml` + `0006_seed.sql` 的种子值）。测试里同理：只传 `PoolConfig` 不改库，断言会与实现对不上 | T3.7 |
| 103 | **缓存"越跑命中率越低"**（反复跑同一批稿子，本该命中的套话全被淘汰） | `prune()` 把 `entries()`（已按"最该走的排最前"排好）又 `reversed()` 了一遍 ⇒ 先淘汰 `hits ≥ 2` 的高频条目，正好把降权的意义抹掉 | 顺着 `entries()` 的顺序删；单测直接断言"淘汰顺序 = `hits < 2` 在前、组内先旧后新" | T2.6 |
| 104 | **`done` 的句子在成片里没声音，而且没有任何地方报错** | 短路只看库里的 `tts_status`：产物被 §03.7.5 的 24 小时 GC 删掉、缓存条目又被 LRU 淘汰之后，"已完成"这个结论已经不成立，交出去的 `tts_audio_path` 指向空气 | `done` 要顺着**两处盘上的东西**验：产物在 ⇒ 直接用；只在缓存里 ⇒ 拷回来；两处都没了 ⇒ `SentenceRepo.reopen` 退回 `pending` 重念（`begin` 会拒 `done`，所以必须显式退） | T2.6 |
| 105 | **0 字节的音频被当成"已完成"** | 只 `exists()` 不 `stat().st_size`；空文件只可能来自"写到一半断电"，而它在下游的表现是"这一句没声音" | 空文件一律当**未命中**：`TtsCache.get` 与 `voice_worker._has_audio` 两处都要判（缓存与交付产物是两条独立的路径，一条判了不算） | T2.6 |
| 106 | **故障注入"成功"了，用例却什么都没验**（测试全绿，引擎一次都没失败过） | 注入与断言按**原文**匹配，而送进引擎的是**归一化后**的文本（`第3句` ⇒ `第三句`） | 故障注入与调用计数一律按归一化后的文本（或它的子串）匹配；写死 ASCII 数字的标记永远认不到中文数字 | T2.6 |
| 107 | **测试挂在 `worker.run()` 上永不返回** | `PoolWorker.run()` 默认**永不退出**（生产语义：常驻 worker 空池就等），空池 + 无可认领单元 ⇒ 无限退避 | 测试一律传 `max_empty_rounds`，并断言 `report.stop_reason == "max_units"` —— 让"池里没活"**响亮地失败**，而不是挂住整轮 | T2.6 |
| 108 | **`timeline.json` 里读不到 `seed` / `tail_ms`，或它的形状隔一次运行就变一次** | 这个文件现在有**两个写入者**：T1.12 的 `render_service.write_timeline`（无 `seed`/`tail_ms`、`pause_after_ms` 恒 0、绝对路径）与 T2.7 的 `tts.timeline.write_timeline`（§04.2.7 的形状）。`studio pipeline run` 走的是前一条 ⇒ 它会把池刚写好的那份**盖成旧形状** | 读它的地方（`render_service._read_timeline`）只认两边都有的字段（`duration_ms` / `text`）；**T2.8 把编排改到"池 + `settle_voice`"之后写入者只剩一个**，那时才允许按 `seed` / `tail_ms` 硬读 | T2.7 |
| 109 | **每一句后面都多出一小段静音**（时间轴上写的是"紧接下一句"） | 停顿抖动写成 `max(0, base + offset)`：`base = 0` 时它退化成"只加不减"（0…80ms）—— 用户要的 0 被抖成了正值，而**没有任何地方报错** | `base_ms <= 0` 直接短路成 0：抖动只负责让"**设了的**停顿"听起来不像节拍器，不许凭空造静音。夹取位置写错的表现是"节奏反而更整齐"，最难怀疑到抖动头上 | T2.7 |
| 110 | **"母带与时间轴差 600ms"**，于是去改拼母带的代码 | 口径不同：`total_ms` 是**成片**时长基准（含 `tail_ms`，§04.2.7），而 `voice_master.wav` 只装"句子 + 句间停顿"—— 尾部那 `tail_ms` 由合成那一步补（§04.2.8.5）。把 tail 也拼进母带 ⇒ 成片比预期长出一个 tail（两处各加一次） | 验收拿 `total_ms − tail_ms` 与母带比；**别为了让"母带 vs `total_ms`"这条断言成立去动母带**。两个数都要在 `timeline.json` 里（`total_ms` + `tail_ms`），读的人自己减 | T2.7 |
| 111 | **故障演练"全绿"，而注入的故障一次都没生效** | 缓存键吃的是**引擎名**，而故障引擎的后缀是**常量**：第一场演练（`tts_fail_sentence=3`）把念成功的音频按 `sapi+fault` 收进了缓存，第二场演练（`tts_down=1`）算出**同一个键** ⇒ 全句命中缓存 ⇒ 引擎一次都没被调到，而 `voice.fault_injected tts_down=True` 照打 | 演练用的引擎名必须**每场都不同**（进程号 + 随机段）。**别只看"命令返回成功"**：判据要落在句子状态上（`tts_down` ⇒ 全句 `skipped`），而"引擎被调了几次"在两条路径上都是 0，分辨不出来 | T2.8 |
| 112 | **一次排空刷出几万对 `worker.started/stopped`，CPU 空转** | `PoolWorker.run(max_empty_rounds=1)` 在空池时**立刻返回**（认领的语义就是"没有就返回"），外层 `while` 不等 ⇒ 忙等；而句子退避最长要等 `backoff_max_ms`（voice 池 20s） | 空转那一支必须**等一拍**（池的 `poll_ms`）。`max_empty_rounds` 只用来"把一次认领切出来"，它**不提供**节奏；而 `_empty_rounds` 跨 `run()` 累计 ⇒ 想靠它退避也不行（第二次进来就已经到线了） | T2.8 |
| 113 | **`--until voicing` 的任务，句子却被念完了** | 排空借的 worker 按**池**认领（`claim(pool=...)` 没有任务过滤），另一条卡在 `voicing` 的任务的作业一起被干掉了 | 这是**已知取舍**（裁定 233）：与常驻池语义一致（谁抢到谁干）。真要按任务隔离得给 `JobStore.claim` 加过滤 —— 那是四个池共用的契约。读日志时别把"别的任务的 `worker.unit_done`"当成"这条任务跑起来了" | T2.8 |
| 114 | **`STUDIO_FAULT` 拼错一个键 ⇒ 演练"成功"了** | 解析器若静默忽略认不出的键，`STUDIO_FAULT=tts_downn=1` 会得到一份**空计划**（`enabled=False`）⇒ 一行故障都没注入，而命令照样跑完、门禁照样绿 | 认不出的键一律抛 `CONFIG_INVALID` 并列出合法键：演练的价值全在"它真的失败了"，静默降级成"没配故障"等于把演练变成走过场 | T2.8 |

| 115 | **点了「重配」没反应、不报错，任务卡在 `voicing`** | 队列的幂等键是 `(task_id, pool, unit_type, unit_ref)` ⇒ 一条单元**一辈子只有一条作业**；`succeeded` 之后业务侧把 `tts_status` 改成 `pending`，**没有任何 worker 会再看它一眼**，而 `settle_voice` 的守卫是"全部句定局" | 重配必须**同时**改两处：业务表（`SentenceRepo.invalidate`）+ 调度令牌（`JobStore.requeue_unit`）。**别只看 REST 返回 200** —— 判据要落在"那条作业回到了 `pending` 且真的能被 `claim` 到" | T2.9 |
| 116 | **点一次「重配」，降级句只给了一次机会就又降级**（看不见的错） | `invalidate` 没把 `tts_attempts` 归零，而上一轮已经撞过 `DEGRADE_AFTER_ATTEMPTS`（3 次） | 失效时 `tts_attempts = 0`：人工重配的意思是"给它一次**完整**的机会"，不是"接着上一轮的第 3 次算" | T2.9 |
| 117 | **换完音色，重配出来还是旧嗓子，而库里显示新音色** | 只改了 `payload_json.voice_map` 与 `tts_status`，没同步 `jobs.payload_json` —— 音色是**运行期选择**，worker 读的是作业 payload（或进程装配值） | 三处一起改：`tasks.payload_json.voice_map` + `script_sentences.tts_status` + `jobs.payload_json`（`requeue_unit(payload=...)` 是**整体替换**） | T2.9 |
| 118 | **试听端点变成任意文件读取** | 路径若由请求拼出来（或直接信任库里的 `tts_audio_path`），`../../` 与绝对路径都能播出去 | 正则**整串**匹配 `voice/<task_id>/s00N.wav`（非法键在路由层就 422）；真正的文件路径只由 `StudioPaths.sentence_wav` / 缓存键拼出；库里的 `tts_audio_path` 还要再判"落在 `data_dir` 里" | T2.9 |
| 119 | **点一下试听就把配音重跑了一遍**（还覆盖了交付产物） | 试听顺手触发合成 —— 而它与 voice 池抢同一台机器（§05 明确要求"走缓存文件，不触发新合成"） | 试听只认**盘上已有**的三处候选（规范路径 / 库里的路径 / TTS 缓存），一处都没有 ⇒ 404 `PATH_MISSING`。面板上"播放"与"重配"是两颗按钮，不是一件事 | T2.9 |
| 120 | **只改一个角色的音色，另一个角色的映射被悄悄抹掉** | `PATCH` 写成**整体替换** `voice_map`：面板只提交被改的那个角色 ⇒ 另一个角色退回进程音色，而用户以为自己只动了一个人 | `PATCH` 走**增量合并**（`{**before, **提交}`）；要一次改两个就一次给两个键。角色名写错（`bigBear`）同样静默无操作 ⇒ 未知角色一律 422 并列出已知的 | T2.9 |
| 121 | **拿"总时长变了"当"时间轴重算了"的证据 ⇒ 假绿** | 真 SAPI 对同一句同一音色是**确定性的**：重念出来的时长逐毫秒一致，`total_ms` 一个字都不变（真机演练实测 14352 → 14352） | 客观证据是**文件被重写**（`timeline.json` / `voice_master.wav` 的 mtime 变了）；"时长变化可见"要用**可控引擎**的用例来钉（集成测试里 700ms → 1400ms）。两件事分开验 | T2.9 |
| 122 | **面板默认任务号（`ui-20260916-120000`）一进配音面板就整屏 422**，而报的是「路径不合法」 | 配音路由的 `PathParam(pattern=...)` 只认 `[0-9A-Za-z]`，而任务号是**调用方起的名**（`RenderJobRequest.task_id` 只限长度、不限字符集）—— 渲染面板默认给的就是带连字符的那一种 | 放开成 `[0-9A-Za-z_-]{1,64}`（媒资键的整串形状一个字都没松）。**校验形状时先问「这个值是谁生成的」**：服务端生成的（句子 id / ULID）可以卡死，人填的只能卡「拼进 URL 会出事」的那些字符 | T4.5 |
| 123 | **新建任务第一次换音色必然 422**：改的是熊大，被拒的理由是没碰过的熊二 | `set_voice_map` 校验的是**合并后的整张表**，而 `TaskPayload.voice_map` 的默认值是逻辑角色名占位（`{'bigbear': 'bigbear', ...}`）—— 本机当然没有这个名字的音色 | 只判**这次提交的那几条**（§04.3.7「音色校验的两条线」本来就是这么写的：存量值交给 `resolve_voice` 退回进程音色并标 `fallback`）。**「人刚填的」与「库里早存着的」是两件事**，别用一个判据兜 | T4.5 |
| 124 | **「我只想换熊大」，被一句「熊二的音色不存在」挡回来** | 面板把**整张映射**发上去 ⇒ 顺手替用户断言了他没碰过的那些行 | 只发改过的角色（`changedSpeakers`）；PATCH 的语义本来就是「只改我点到的这几个」 | T4.5 |
| 125 | **音色下拉框空着**，而库里明明记着这个角色的音色 | `<select>` 的 `:value` 不在任何 `<option>` 里 ⇒ 显示成空白；空白被读成「这个角色没有音色」，真实原因是「参考音没入库 / 系统语音包没装」（真机数据：映射是 `bigbear`，本机只有 `bear_da` / `bear_xiong`） | 把那个值**也渲染成一个 option**（`· 本机找不到（换一个）`）+ 行上挂红标。**凡是「值可能不在选项里」的下拉框都要留这一手** | T4.5 |
| 126 | **空闲时面板上的数字一直在跳**，而它跳动的唯一原因是「我们在定时问」 | 轮询开着不放（1s 一次，什么新东西都没有），还顺手把音色清单也塞进轮询里重画下拉框 | 只在 `pending + synthesizing > 0` 时轮询（失败与跳过都是**定局**）；音色清单只在首屏与手动刷新时拉。**与渲染面板同一条口径**（`RENDER_POLL_MS`） | T4.5 |
| 127 | **新端点整屏 404，而 `/api/v1/health` 是 200**（排查会去查前端） | 上一轮的 API 进程还占着端口（健康检查是**旧进程**在答），新路由没加载 | 改完后端**必须重启 API 进程**再验；看到「新端点 404 而健康检查正常」先查端口占用（`Get-NetTCPConnection -State Listen -LocalPort 8787`），别去翻前端 | T4.5 |
| 128 | **跳过去那一屏还是上一个任务**（或者干脆空着），而**不报错** | 认领跳转放在首屏拉取**之前**：稿件面板的 `refresh()` 会把"不在当前状态列表里"的选中项清掉，配音面板的 `setTaskId()` 会把上一条任务的快照清掉 —— 自己的首屏把刚接过来的任务号覆盖了 | **先拉列表、再认领跳转**（`await refresh()` ⇒ `takeHandoff()` ⇒ `select()`；配音面板是 `setTaskId()` ⇒ `start()`）。**顺序错不会报错，只会静默显示别的任务** | T4.14 |
| 129 | **人自己点侧边栏走，却被一个过期的任务号又跳一次** | 待认领的跳转**留着不清**（或只在"目标面板认领"时清），于是那一笔在内存里躺着等下一次 | 认领即清空 + `selectPanel()`（人自己点走）也清掉。**"面板把人送过去的"与"人自己走过去的"是两件事** | T4.14 |
| 130 | **封面只有最后一行字**（前面那几行不报错地消失） | 一条命令里写了多个 `-vf`，而 ffmpeg **只认最后一个** —— 前面几条滤镜链被静默丢弃 | 把全部滤镜**逗号连成一条** `-vf`（`scale,crop,drawbox,drawtext,…`）；`drawtext` 每段一条、顺序即绘制顺序。真机踩过 | T5.1 |
| 131 | **`drawtext` 报 `No option name near '/Windows/Fonts/…'`**（封面整条降级成纯色底） | `fontfile=` 的值要过**两层**解析（filtergraph 一层、drawtext 选项一层），而 `C:/…` 的驱动器冒号在第一层就被当成参数分隔符 | 写成 **`fontfile='C\:/Windows/Fonts/msyh.ttc'`**（转义 + 单引号）。⚠️ **在 shell 里手敲单反斜杠会成功** —— shell 先吃掉一层，ffmpeg 收到的还是 `C:/…`；别拿"我在命令行里试过"当 argv 的证据 | T5.1 |
| 132 | **长标题溢出到屏幕外，而且不报错** | 量宽那条命令漏挂 `bbox` 滤镜 ⇒ `stderr` 里没有 `w:` 行 ⇒ 量出来**恒为 0** ⇒ 既不缩字号、又按"实测宽 0"居中 | 量宽命令末尾必须挂 `bbox=min_val=…`（且量的是**黑底白字**：透明底上 `bbox` 会量出整幅画布）；量不出来时**按字数估一个**，绝不返回 0 | T5.1 |
| 133 | **相似度审计没过，却安安静静地过审了**（detail 里还写着"建议复核"） | `GateResult.passed` 与 `blocking` 揉在一起（`passed = dup_audit_pass or not block_on_similarity`）⇒ "审了没过"变成 `passed=True` ⇒ `run_precheck` 那条"没过就 warn"的循环一句提示都不出 | `passed` 报**审计结论本身**，`blocking` 只报**要不要拦**；非阻断门禁没过时进 `warnings`（真机踩过） | T5.1 |
| 134 | **`publish/` 里冒出 `from studio.render…` 却没人发现**（§02.1 明令禁止） | 函数内导入 + `# noqa: PLC0415` 让违规**绕过了 ruff 的可见性**：写在模块顶会被人一眼看见，塞进函数体就只剩一行 noqa —— 而 noqa 的语义是「我知道这条规则」，不是「这条规则不适用」 | 分层依赖必须在**模块级**成立；函数内导入只允许在文档里**明列**（§02.1 现只列了 `precheck` 量响度一处）。`system_font_dirs` 这类**平台事实**要下沉到 `core/`（裁定 261） | T5.1 |
| 135 | **dry-run 的「按钮没被点」靠截图自证 ⇒ 假绿** | “截图里没有结果块”是**看不见**的证据：按钮点不动（选择器写错）与没点，在像素上长得一样 | 靶页点下发布时 `POST /__published` 到**本地服务器**，断言改成“服务器一次都没收到”；反面对照（点得动）必须走**同一条 URL 组装路径**（真机踩过） | T5.2 |
| 136 | **靶页的“未登录”提示读不到**（`health()` 只能报一句谁都看不懂的 hint） | 提示元素带着 `hidden` 属性时 `inner_text` 读到**空串** ⇒ 判不出“未登录 / 已过期” | 演练靶页在 `?logged_out=1` 时**显式** `hidden=false`；真机踩过 | T5.2 |
| 137 | **回读把「吞 emoji」误判成 `mismatch`**，给操作员的建议方向全错 | 判据没抹平空白差异：真机上「吞 emoji + 尾部多一个换行」退化成 `mismatch`（该去查编辑器吃字，却叫人去查选择器） | 判据先 `_squash(_drop_emoji(x))` 再比；真机踩过 | T5.2 |
| 138 | **注册表里的类“不接受参数”**（`Too many arguments for "Publisher"`） | `Publisher` ABC 只声明了三个抽象方法，没有 `__init__` —— 而 `PUBLISHERS` 里存的是**类**，服务层要 `get_publisher(code)(ctx)` | 构造签名写进基类（`_ctx` 由基类持有，子类 `super().__init__(ctx)`）。靠 `cast` 蒙混会让“漏传参数”拖到真机（裁定 262） | T5.2 |
| 139 | **`health()` 的两条失败分支从来没人走过**（直到第一次真机 dry-run 报 `AttributeError`） | 假页面测试只能验“读得到”那一条；“未登录”与“已过期”需要**真的页面**才能造出来 | 靶页要能用 `?logged_out=1` / `?expired=1` **制造**这两种状态，否则它们是死代码 | T5.2 |
| 140 | **pytest 静默挂死两分钟、一行输出都没有**（发布池的集成测试） | 给 `PoolWorker` 传了 `connection=`，而它的**心跳线程**用的是自己开的连接 ⇒ 跨线程复用一条 sqlite 连接，两个线程互相等 | 测试里给 `PoolWorker` **别传** `connection`（让它自己开），并把 `max_empty_rounds=1` 当保险；排查靠"按启动时间杀 python 进程" | T5.3 |
| 141 | **重投一条「已经发过」的内容，要白等 30 分钟** | 幂等短路排在**限频判定之后**：已经 `published` 的记录先撞上"距上次发布不足最小间隔" ⇒ 顺延 | 幂等短路要排在限频**之前**（只读、不改库）；`run()` 里原有那处终态分支保留作双保险 | T5.3 |
| 142 | **面板说"试了 2 次"，备注说"重试 3 次仍失败"**（同一件事两个数字） | `mark_failed` 记 `attempt_count += 1`，而 `mark_manual_required` 不记 —— 可它俩是**替代**关系不是补充关系（最后一次失败只走后者） | 转人工也 `attempt_count + 1`：漏掉它，面板上那个数字永远比实际少一次 | T5.3 |
| 143 | **`db/` 里冒出 `from studio.domain.publish import …`**（契约测试 `test_db_layer_does_not_import_domain` 当场红） | 幂等键的实现写在 `domain/publish.py`，而 `publications` 的唯一写入者是 `db/repositories/`，分层是 `core → db → domain` | 纯函数下沉 `core/ids.py`（`publication_idempotency_key`），`domain.publish.idempotency_key` 留作**别名**（契约名不变） | T5.3 |
| 144 | **测试里 `dataclasses.replace(PoolConfig(...))` 直接 `TypeError`**，而"把退避压到毫秒"也没生效 | ① `PoolConfig` 是 **pydantic 模型**不是 dataclass；②"等多久"有**两个真相源** —— `pools.yaml`（传进处理器的 `PoolConfig`）与 `pool_settings` 表（`fail` 之后算退避用的） | 用 `PoolConfig(**{**cfg.model_dump(), **overrides})`（还能顺带过一遍校验）；压测试时间要**两处一起压**，否则第二次尝试要等 60 秒 | T5.3 |
| 145 | **手工改完 import，`ruff format` 绿、`check` 却红（I001）** | `tasks.ps1 fmt` 只跑 `ruff format`（格式化），**不管 import 排序** | 改完 import 补一条 `uv run ruff check`（或 `--fix`）；`fmt` 绿 ≠ `check` 绿 | T5.3 |
| 146 | **面板上那条进度条跑完停在 0%，旁边却写着「完成」** | 这条链路的进度回调**不是连续的**（配音按句、渲染按段、投递与拼母带那几步根本不回调），而 `percent` 直接按 `done/total` 算 ⇒ 最后停在哪取决于它是从哪一步收尾的。真机上 `produce_video` 最后那次回调是 `render 0/1` | `percent` 在 `status == "succeeded"` 时直接返回 100。`RenderJobService` 有同一处（已一并修） | T4.14+ |
| 147 | **新端点的响应里少了一个字段，只有 `vue-tsc` 会告诉你** | 用 `**plan.to_dict()` 拼 payload，而响应模型里没声明那个字段 ⇒ FastAPI 按 `response_model` **静默过滤**（多出来的键不报错、少声明的字段也不报错） | `**dict` 拼 payload 时，字段清单是**响应模型**说了算：加字段要同时改模型；别指望运行时告诉你 | T4.14+ |
| 148 | **模板里写的 `**加粗**` 在界面上是字面星号** | 前端没有 markdown 渲染器（`PanelCard` 直接 `{{ }}`），而提示语沿用了写文档的习惯 | 模板里用 `<b>` 或拆句，别写 markdown 语法 | T4.14+ |
| 149 | **命令「超时」之后调用方永远不返回**（渲染 worker 卡死，日志里只有一句超时） | `subprocess.run(timeout=)` 超时后只杀**直接子进程**，随后（Windows 上）又调了一次**不带超时**的 `communicate()` 去读管道 —— 只要有一个继承了我们管道的孙进程还活着，它就永远等不到 EOF。实测：`timeout=2` 的命令 12s 后仍挂着 | 超时改走 `_kill_tree()`（Windows `taskkill /PID <pid> /T /F`、POSIX `killpg`），收尸那一步自己也带超时（`REAP_TIMEOUT_SEC`），收不干净就关掉我们这一端的管道 | T3.4 |
| 155 | **交付包缺件那一行只有「缺」两个字，没有补救说明** | `build_package` 的兜底条件写成 `source is not None and not source.is_file()` —— "路径根本没给"（还没渲染 / 还没生成封面）走不到兜底，`note` 留在 `None` | 判据改成"**这一件不在盘上**就兜底"；"路径没给"与"路径给了但文件没了"都算缺（后者用更具体的那句话，**不覆盖**它）。这条是**写测试时才发现的** | T5.5 |
| 156 | **夹具里第二条素材静默不入库，报错长得像查询写错了**（`KeyError`） | 跑酷 / BGM 的 `upsert` 按 **sha256 去重**（同一条素材重复入库 ⇒ 跳过），而夹具里拿一个常量当哈希 | 每条素材派生一个**各不相同**的 sha256（`sha256(clip_id)`）。"入库没报错但查不到"先怀疑去重，别先怀疑 SQL | T5.5 |
| 157 | **连按两下「导出」得到同一个目录，第一份被悄悄换掉** | 打包目录名的时间戳只取到**秒**（`now_iso()` 变换后 `[:15]`），而复制一个小文件远不到一秒 | 时间戳取到毫秒（`[:19]`）。"两个动作落进同一个名字"在**所有**以时间戳命名产物的地方都成立 | T5.5 |
| 158 | **请求体写错返回 500**，而且**响应里带着刚提交的密钥原文** | `RequestValidationError` handler 把 pydantic `errors()` 原样回显：`ctx` 里的 `ValueError` 对象让 `json.dumps` 抛 `TypeError`（⇒ 500），而 `input` 字段把用户输入抄回响应 | 只保留 `type` / `loc` / `msg`（`ctx` 转字符串），**绝不回显 `input`** | T6.1 |
| 159 | **照抄上游 `requirements.txt` 会把能跑的环境降级** | CosyVoice 上游锁 `torch==2.3.1`，而本项目是 `torch 2.4.0+cu121`（`D:\Torch` 里的 cp311 wheel） | 单独维护 `tts/requirements-cosyvoice.txt`，**只列推理真正用到的**，**不写 torch / torchaudio**（由 `tts/pyproject.toml` 负责，依赖想升级会被 uv 拦住） | T2.1 |
| 160 | **`pip install openai-whisper` 报 `No module named 'pkg_resources'`**；装上了又 `No module named 'matcha'` | ① whisper 的 `setup.py` 用 `pkg_resources`，而 `setuptools>=81` 已删掉它；② `cosyvoice` 依赖 `third_party/Matcha-TTS` | ① `setuptools<81` **且** `--no-build-isolation`；② `PYTHONPATH` **必须同时含** `D:\ai_models\CosyVoice` **和** `...\third_party\Matcha-TTS`（缺后者报 `matcha`） | T2.1 |
| 161 | **`inference_zero_shot` 传张量报错；`torchaudio.save` 报 `Invalid file`** | 该 revision 的第三参是**参考音路径**不是张量；且此环境的 `torchaudio.save` 不可用 | 第三参传**路径**；落盘用 `soundfile.write` | T2.1 |
| 162 | **`tts/.venv` 里 `import studio.core.clock` 抛 `ZoneInfoNotFoundError`**（`No time zone found with key Asia/Shanghai`） | Windows 没有系统 tz 数据库，CPython 要靠 `tzdata` 包；pip **不装也不报缺** | 把 `tzdata` 写进 `tts/requirements-cosyvoice.txt`（已装）。报错点离「少装一个包」隔了**四层 import**（`studio.tts.server` → … → `studio.core.clock`），别顺着调用栈查 | T2.2 |
| 163 | **tts 进程活着、`/health` 回 200，可每个请求都报 `No module named 'studio'`** | `tts/pyproject.toml` 是 `package = false` ⇒ 子环境里**没有**本项目；启动器不前置 `PYTHONPATH` 就 import 不到 | `ServiceSpec.env_prepend` 前置 `PYTHONPATH=<仓库>/src`（**前置不覆盖**已有值）；手工起进程要自己加 | T2.2 |
| 164 | **`mypy.ini` 里的 `[[mypy.overrides]]` 段一个字都没生效，却也不报错** | `[[...]]` 是 **TOML** 的数组表写法（`pyproject.toml` 用）；INI 里 mypy **静默忽略**整段 —— 连「未知键」都不提醒 | INI 一律写 `[mypy-<模块>]`（`[mypy-yaml.*]` 这种）。判据：写完后**故意**把某个 `[mypy-<模块>]` 删掉，看那个 import-not-found 会不会**回来** —— 回来了才说明这一段真的在生效 | T2.2 |
| 165 | **换了引擎 / 换了权重版本，却复用了旧引擎的缓存音频**（成片里是**别人的嗓子**，而且一切正常） | 缓存键吃 `engine` + `engine_revision`，而客户端**自己写死**了这两个字面量 —— 服务换了它自己不知道 | 引擎名与版本**从服务自述来**（`/health` 的 `engine` / `revision`），不写死在客户端 | T2.3 |
| 166 | **`/health` 回 200，服务却是个坏实例**（`ready:false`、每次请求都报“推理环境坏了”），而守护进程**永远不修它** | 就绪判据只看 HTTP 状态码 ⇒ “端口上有人答话”被当成“服务能用”；坏实例占着端口 ⇒ 新的起不来、旧的没人管 | 就绪判据**必须吃服务自述的 `ready`**（`_default_health`）；端口上“我们自己、没就绪”的实例由编排器**接管重启**（`_takeover_unhealthy`：`ready is False` + 有 `engine` 字段 + 自报 `pid` 且活着 ⇒ terminate→kill→等端口释放）；服务不自报 `pid` 就认不出是谁，只能记 `service.port_busy_unready` 并给 hint | T2.3 |
| 168 | **配音忽然换成系统语音包念了，而每一处日志都写着 `cosyvoice2`**（空闲 20 分钟之后开始） | `ready: false` 被当成**一种**东西（坏了），而它其实是两种：①子环境没 torch ②`model_state: unloaded`（空闲卸载，叫得醒）。编排器把它当坏的 ⇒ 去杀一个睡着的健康实例；配音池把它当不可用 ⇒ 退回 SAPI | 两种语义分开：`service_engine.wakeable_health()` 判"叫得醒"（`ready is False` 且 `model_state in WAKEABLE_MODEL_STATES`）；`ResidentStatus.usable` 把睡着算可用；`_default_health` 记就绪、`_takeover_unhealthy` 不碰它。另外**配置类失败也要落到 `EngineState.ERROR`**（权重目录不在时停在 `UNLOADED` 会让坏实例伪装成睡着的健康实例） | T2.3 |
| 167 | **子环境进程起不来，日志看着像“推理环境坏了”**（`ModuleNotFoundError: No module named '''ulid'''`） | `workers/run_tts.py` 从 `services.service_manager` import `run_entry` ⇒ 把一个**叶子骨架函数**放进了服务层 ⇒ 拖进整个服务层依赖（`ulid` 之类不在子环境里） | 叶子进程入口放**叶子层**（`studio.core.entry.run_entry`）；`service_manager` 原样 re-export，老调用点不动 | T2.3 |
| 169 | **本地兜底通道每次 HTTP 200，网关的修复重试也照跑 3 轮，却永远报 `$.directions ... is too short`** | 请求体只给了 `format: "json"`（"是 JSON 就行"），**没给 `format: <schema>`**（"必须长成这个样子"）⇒ `minItems` 这类**数量约束**对模型完全不可见，7B 级模型稳定只吐 1 条 | 把该 Agent 的 JSON Schema 当 `format` 发给 Ollama（语法约束解码）。schema 是**契约事实**、与 engine 无关 ⇒ 由网关挂到 `LlmCall.response_schema`，传输层决定怎么编码（云端仍走 `json_object`）。实测 qwen2.5:7b：只给 `json` ⇒ 1 条；给 schema ⇒ 5–8 条 | T1.9 |
| 170 | **任务已经进配音了，忽然被拽回 `failed`，紧接着流水线撞上 `failed → queued_render` 非法迁移** | CLI 的 `script draft` 与常驻写稿池会跑**同一个**任务：先跑完的那条把任务推过写稿段，另一条稍后失败时照常置 `failed` ⇒ 一次好端端的生产被打断 | **迟到的失败没有话语权**：`ScriptService._fail_task` 先看状态，已越过写稿段就只留一条 warn（不写 `failed`）；写稿池那条单元同样改成空操作成功。反过来「断点不在写稿段」也必须**空操作**（原代码会接着去审稿，拿 `failed` 去撞不存在的边 ⇒ 重投死信只是再生产一次死信） | T1.9 / T4.11 |
| 171 | **成片响度实测 −0.99 dBTP，被发布门禁（≤ −1.0）拦死**（渲染自测也是 −0.99，不是发布才量的） | 限幅器的余量只留了"AAC 编码过冲量"本身 ⇒ 落点**正好在门禁线上**，比 ebur128 的整数读数细一点就出界 | 过冲余量之外再让**一格安全间隙**（`PEAK_SAFETY_MARGIN_DB`）：落点从 −1.0 变 −1.3 dBTP，过冲随内容浮动也顶不穿门禁 | T3.6 |
| 172 | **回流趋势线自己往后漂**：写着 T+72h，实际第 5 天才去采 | 下一个时点算成"上次 +1h"（失败顺延的写法）而不是"`published_at` + 计划点" ⇒ 每失败一次就把整条时刻表推后一次 | 时点一律**相对 `published_at`** 推进（`next_metric_at(published_at, schedule, now=…)`，取第一个**严格大于** now 的计划点）；失败顺延是**另一个**函数（`defer_after_failure`，+1h，≤3 次） | T5.4 |
| 173 | **采数把发布搞挂了**：两个浏览器抢同一个 `browser_profile` | 采数与发布**共用账号登录态目录**，各起一个 Playwright ⇒ 后起的那个把前一个的 profile 锁走/顶掉 | 采数**不排队、不抢占**：发布池 `running > 0` ⇒ 这一拍整拍让路（`yielded=true`，不是失败）。采数是"读"，晚一拍读到的数字只会更新 | T5.4 |
| 174 | **人工反馈被标成"自动回流"**（`grounded_on.source='auto'`），而那条其实是 0913.md 里人写的 | 让模型自报"我这条依据来自自动回流数据"：模型看不到数据来源，只会顺着问法猜"是" | 来源由**服务层文本比对**回填（`_mark_auto_refs`：引文与自动回流文本比对，短引文用 `MIN_QUOTE_MAT` 挡掉）；模型只说"我引用了哪句话"，**不说这句话从哪来** | T5.4 |
| 175 | **完播率 42.3% 被当成 0.423 存 / 读，或反过来** —— 两边都「看着正常」，100 倍的错一路到面板 | 比率与百分数**同形**：`42` 与 `0.42` 都是合法的小数，类型系统看不出来 | 契约层钉死**比值**（0–1）：`PublishMetrics.completion_rate` 存比值，`parse_metric_ratio()` 把 `42.3%` / `42.3` / `0.423` 三种写法都归一到比值（>1 或负数 ⇒ `None`），**百分数只在显示那一处乘 100**（`rateText`） | T5.9 |
| 176 | **面板说「只回流了数字」，而库里一条反馈都没有** | 播放量 / 点赞量并**不**进 `feedback_items`（§06.8 ① 的输入是**评论**，数字走 ② 的降权与 §6.7 的报告）⇒ 没有评论时 `feedback_items_created=0` 是**预期** | 面板文案跟着事实走：说清「§06.8① 的输入就是评论 ⇒ 新增反馈 0 条是预期」。「回流了」这种话不许写 —— 面板说回流了而库里什么都没有，是最难查的一类不一致 | T5.4 / T5.10 |
| 177 | **没让它发，它自己发了一条**（面板上多出一条与真发布只差平台代号的记录） | 演练台（`platforms.other`）被算进**投递默认目标**：只要任务完成，投递侧就会顺手给它排一条作业 | `_default_platforms` **跳过**演练台（判据是「这个平台的发布器是不是 `REHEARSAL_PUBLISHER`」）；要演练就**显式点名** `platforms=("other",)` | T5.9 |
| 179 | **定时面板把「下一次」显示得比它自己的窗口还早**（排 18:00–21:30 的计划写着「下一次 10:30」，两行当场自相矛盾） | 库里 / API 一律 **UTC** ISO（`…Z`，§02.4），而展示层**直接截字符串**（`ts.slice(11,16)`）⇒ 显示的是 UTC 钟点，比北京时间早 8 小时 | 展示层**必须换算**：`stampText` 用 `new Date()` + **浏览器真实偏移**（不写死 `+08:00` —— 写死后任何一台非东八区的机器排出来的计划都会差几小时，而面板上显示的时刻还是对的）。**遗留**：`stores/publish.ts` 的 `formatStamp` 仍是老写法（`next_metric_at` 会偏 8 小时） | T5.6 |
| 180 | **给枚举加一个值，仓库里两处硬编码的断言与三处文档当场过时**（`len(AlertCode) == 8` / 「8 值」文案） | 枚举的**数量**被写进了契约测试与文档：加一个 `SCHEDULE_FAILING` ⇒ 契约红，而文案不会自己红，只会安静地骗人 | **枚举数量不进断言、不进文案**：契约测试断言具体成员（或 `>=` 下限），文档写「枚举」而不写数字。加值时全仓库搜一遍 `== 8` 与「N 值」 | T5.6 |
| 182 | **钉着已发任务的计划每天"失败"一次，五天后拉一条真告警** —— 而那条告警淹掉的正是真故障 | 「作业没建出来」只有一种落点（`error:PUBLISH_FAILED`），可它有两种原因：**平台没启用**（要人改配置）与**这条早投过**（什么都不用做）。幂等命中被算进失败 ⇒ `fail_streak` 每天 +1 | 两种原因**分开报**：`EnqueueReport.duplicates` 单独列出来（别让调用方去猜那句中文），幂等命中 ⇒ `last_result='skipped_duplicate'` 且 `failed=False`；只有"平台/账号不可用"才落 `error:`。真机踩到 | T5.6 |
| 181 | **`web:verify` 只回一句「npm run verify 未通过」，看不到真错**（`tasks.ps1` 抛的是自己的中文提示，npm 的输出被吞掉） | 门禁脚本为了给出人话提示，把子进程输出压掉了 ⇒ 真错（`vue-tsc` 的 5 处 TS2554）一个字都看不见 | 前端门禁红了先**绕过外层**直接跑：`cd web; cmd /c "npm run typecheck"`（再 `test` / `build`）。类型错永远排第一 —— 单测与打包都排在它后面 | T5.6 |
| 183 | **出厂自带的周报 / 月报一次都不会跑，而且不报错** | seed 里三条内置周期的 `next_run_at` 初值全是 NULL，而到期查询写的是 `next_run_at <= now` —— SQL 里 `NULL <= x` **恒为假** | 把 `NULL` 读成「未排期」而不是「永不触发」：调度器每拍先 `_schedule_pending()`（`enabled=1 AND next_run_at IS NULL` ⇒ 当场算好落库）。它同时覆盖「新建时没排期」与「停用后被清空」两种情况 | T5.7 |
| 184 | **加一个枚举值，两处契约测试与两处文档当场过时** | 陷阱 180 的同一个坑再咬一次：T5.6 记下"枚举数量不进断言、不进文案"之后，仓库里仍有 `len(AlertCode) == 9` 与两处「9 值」文案 | 契约测试改成 `>= 9` 下限（**数量不是它要守的东西** —— 它守的是"猝死不升格为告警"）；文档只写「枚举」。加值前先 `rg "== 9\|9 值"` | T5.7 |
| 178 | **面板 / 文档说「开关关着时投递进来的作业会转人工」，实际是死信** —— 操作员去「待人工」里找一个永远不出现的记录 | 开关守卫（`_guard_switch`）跑在**建 `publications` 那一行之前** ⇒ 既没有待人工记录、发布面板上也什么都不出现；而三处文案（路由 docstring / CLI / runbook）写的是「转人工」，§04-contracts ① 自己写的是「死信」 | 文案与**代码的真实落点**对齐：死信（去「四池调度」看）。投递面板把这句话写在**真平台选项旁边**（出厂就是这一档，不说的话按一次投递会得到「什么都没发生」） | T5.10 |
> 本节是常用子集，**编号与 `docs/spec/05-roadmap-checklist.md` §5.7 完全一致**（完整 184 条见该处；跨文档引用按编号即可）。

---

## 11. 交付物清单（每阶段结束时必须齐备）

| 类别 | 内容 |
| --- | --- |
| 代码 | `src/studio/**`、`workers/**`、`web/**`、`scripts/**` |
| 契约 | `schemas/*.schema.json`、`tests/contract/**`、`tests/golden/**` |
| 配置 | `config/*.yaml`（含 `persona.yaml` 实例）、`prompts/**`、`templates/**` |
| 文档 | `docs/spec/**`（规格书）、`docs/adr/**`、`docs/runbook/**`（7 个剧本）、`docs/qc/**` |
| 运维 | `启动.bat` / `停止.bat` / `ops/*.ps1` / `scripts/backup_db.ps1` / `scripts/restore_db.ps1` |
