# 施工 Todolist · AI 全自动营销号制片台

> 依据：`docs/spec/`（**v3.2 终版 · 需求已冻结**）｜ 生成日期：2026-09-13 ｜ 最近更新：2026-09-15（**T1.1–T1.12 全部 ✅** + **T4 已完成 11/13**：T4.1 前端脚手架 ✅ · T4.2 总览台 ✅ · T4.3 选题面板 ✅ · T4.4 稿件面板 + 确认闸 ✅ · T4.7 合成配置面板 ✅ · **T4.8 素材库 ✅** · T4.9 实时日志 ✅ · T4.10 四池调度控制台 ✅ · T4.11 无人值守 ✅ · T4.12 观测/备份/交付 ✅ · T4.13 人物库面板 ✅** + **T2.5 文本归一化与切分 ✅（2026-09-15）**）
> 本文件是**唯一施工执行入口**：把规格书里 49 个原子任务拆成可勾选的子项。
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

这些不在 49 个任务里，但**不准备就会在某个时刻卡住**。按"最晚需要日"排序。

| # | 项 | 放置位置 | 最晚需要 | 状态 |
| --- | --- | --- | --- | --- |
| E1 | 🔴 **MC 跑酷素材包**（≥60 条 / ≥30 分钟） | `data/assets/mc_parkour/` | **T3.1 开工前** | 缺失（`D:\MC` 空） |
| E2 | 🔴 **水印 PNG**（1080×1920 适配，带透明通道） | `templates/<tid>/assets/images/watermark.png` | **T3.2 开工前** | 缺失 |
| E3 | 🔴 **BGM 音乐库**（≥20 首授权曲） | `data/assets/bgm/` 或 WebUI 上传 | T3.6 开工前 | 缺失（`D:\MUSIC` 空） |
| E4 | 🔴 **熊大熊二原声**（各 2–3 段，10–30s，**无 BGM**） | `data/voice_src/{bigbear,littlebear}/` | **T2.4 开工前** | 缺失 |
| E5 | 🔴 **CosyVoice 权重**（2–4 GB） | `models/` 或 `D:\ai_models` | **T2.1 开工前** | 缺失（版本基线见 Q7） |
| E6 | 🟡 **LLM API Key**（[OI] 兼容） | 环境变量 `STUDIO_LLM_API_KEY` | **T1.9 真机联调前** | 未提供（T1.8 代码已用脚本化传输全覆盖，**不阻塞**；`studio llm probe` 会报 `no_key`） |
| E7 | 🟡 **`config/persona.yaml`**（人设/口吻/受众/口癖/禁区） | `config/persona.yaml` | **随时**（不阻塞） | 缺失（**唯一人工必填**）· T1.9 / T1.10 均已用 `personas/persona_default.yaml` 现值开工；改人物文件即生效（阈值经 `ScriptRules.from_persona`），历史批次可按 `prompt_version` 追溯 |
| E8 | 🟡 字体文件（中文字幕用） | `templates/<tid>/assets/fonts/` | T3.5 开工前 | 待确认 |

> **不阻塞开发**：E1–E5 缺失时链路仍可跑通（黑屏降级 §04.2.8.6 / 字幕模式降级 §04.3.3），素材到位后自动提升画质。
> **E1/E2 是硬阻塞**：水印缺失 ⇒ **拒绝渲染**（D5 必做，不降级）；跑酷素材缺失 ⇒ 出纯黑底片。

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
- ⚠️ **一期降级现状（实测）**：`api` `ready`；`tts` `server_missing`；`draft`/`voice`/`render` `handler_missing` ⇒ 真拉起 5 进程要等 T2.2 / T2.6 / T3.x / T4.11
- ⚠️ 原文 §7.4 要求 WebUI 与 Worker 完全独立（ADR-002）⇒ 已由「五进程各自 `Popen` + 独立日志 + 只动自己台账」落实

> **>>> M1 门禁**：网页端输入定位 + 热点 ⇒ 产出合格稿件（含评分）⇒ 确认闸可见；`启动.bat` 一键拉起全部服务。
>
> **M1 口径（T1.12 裁定 108）**：一键启动**已可用**（`api` ready + 其余如实报 `degraded`），但「5 进程全部 ready」
> **在 M1 阶段不可能达成** —— `tts` 属 T2.2、`voice` 属 T2.6、`render` 属 T3.x、`draft` 属 T4.11。
> 验收以「未就绪进程被**如实报告**且不阻塞其余进程」为准（P4：宁要真话，不要好看的假绿灯）；「5 进程全 ready」顺延到 M4。

---
## 2. 阶段 T2 · CosyVoice 配音（9 任务 → 门禁 M2）

### T2.1 tts venv + 模型权重就位 · **P0** 🔴 需 E5
- 依赖：T1.1 ｜ 里程碑：M2 ｜ 契约：§01.4 / §04.3.1
- [ ] tts venv = **Python 3.11** + torch **2.4.0+cu121**（复用 `D:\Torch` 预置 wheel，**不重装 CUDA Toolkit**）
- [ ] CosyVoice 源码就位 + **revision 锁定并留痕**
- [ ] 权重落 `models/` 或 `D:\ai_models`（**不落 C 盘**）
- [ ] 版本基线：**CosyVoice2-0.5B**（Q7 裁定）；若 CosyVoice3 可下载且接口兼容 ⇒ 只改 `tts.yaml` 的 `model_dir`/`revision` 即可切换
- [ ] `HF_HOME` / `MODELSCOPE_CACHE` 生效验证（下载过程不写 C 盘）
- ✅ `uv run --project tts python -c "import torch,cosyvoice;print(torch.__version__,torch.cuda.is_available())"` = `2.4.0+cu121 True`；权重目录可加载
- ⚠️ **Q7 版本不确定** ⇒ 用适配层隔离，不阻塞 T2.3 开发
- ⚠️ 禁用 bf16（Turing sm_75 不支持）

### T2.2 常驻推理服务 + 并发实测标定 · **P0**
- 依赖：T2.1 ｜ 里程碑：M2 ｜ 契约：§04.3.5 / 裁决 C8
- [ ] HTTP 服务接口：`/health`、`/warmup`、`/unload`、`/voices`、`/synth`
- [ ] GPU 串行信号量（**默认并发 1**，可调 1–3）
- [ ] **fp16 常驻** + 空闲 20min 卸载
- [ ] 429 背压（超并发请求不排队雪崩）
- [ ] **实测标定**：`scripts/bench_tts.py` 跑并发 1/2/3，把实测结论**回写** `pools.yaml`
- [ ] OOM 自动降并发（连续 3 次 `TTS_OOM` ⇒ `concurrency-1` + `audit_ops`，恢复需人工确认）
- ✅ `curl 127.0.0.1:8811/health` → `{ready:true,device:"cuda",model_state:"ready"}`；`python scripts/bench_tts.py --concurrency 1` 产出延迟基线；**首句延迟 <5s**（常驻 + 预热）
- ⚠️ **R4 显存紧**（8GB，桌面占 1.49GB）⇒ fp16 + 单并发 + 禁 bf16
- ⚠️ 陷阱 #10 首句延迟 20s+ ⇒ 常驻服务 + `/warmup` + 空闲卸载

### T2.3 引擎适配层与路由 · **P0**
- 依赖：T2.2 ｜ 里程碑：M2 ｜ 契约：§04.3.2 / §04.3.3
- [ ] `VoiceEngine` ABC：**零样本复刻**（参考音频 + 参考文本 + 目标文本）、按句合成、降级策略签名
- [ ] 多引擎路由（Mock / 常驻服务 / CosyVoice 三实现可互换）
- [ ] 熔断：连续失败 ⇒ 打开熔断，N 秒后半开
- [ ] §04.3.3 降级决策表**逐条落地**：OOM / 超时 / 静音 / 爆音 / 引擎宕 / 连续失败熔断
- ✅ `pytest tests/unit/tts/test_router.py -q`（决策表**逐条**覆盖）；`pytest tests/contract/test_voice_engine_abc.py`（三实现同一契约）
- ⚠️ 引擎替换成本 ⇒ ABC 隔离，换引擎不动业务代码

### T2.4 原声入库与音色注册 · **P0** 🔴 需 E4
- 依赖：T2.2 ｜ 里程碑：M2 ｜ 契约：§04.3.1 / R2
- [ ] 目录契约：`data/voice_src/{bigbear,littlebear}/` + `profile.json`
- [ ] 质量校验：段数 2–3 / 时长 10–30s / **无 BGM** / 无削波 / 有效语音占比
- [ ] 零样本复刻注册 + 试听样本生成
- [ ] **R2 合规**：音色 ID 与展现名**解耦**（默认按原文用 `bigbear`/`littlebear`，但可一键替换为自录音色）
- [ ] 来源登记留档（`proof_path`）
- ✅ `python scripts/ingest_voice_src.py --voice bigbear` 校验通过并注册；`studio tts list` 可见两个音色；`pytest tests/integration/test_voice_profile.py -q`
- ⚠️ **R2 法律风险**（熊大熊二是《熊出没》IP）⇒ 解耦 + 可替换 + WebUI 显著提示 + 留档
- ⚠️ 原声缺失 ⇒ T2.3 熔断走降级，链路不断

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

### T2.6 按句合成流水线 + 句级缓存 · **P0**
- 依赖：T2.3–T2.5, T1.5 ｜ 里程碑：M2 ｜ 契约：§03.3.7
- [ ] sentence 单元 job（**续传粒度 = 句子**）
- [ ] `tts_hash` 缓存（内容哈希决定产物路径 ⇒ 命中即跳过）
- [ ] 单句重试 / 降级（`tts_attempts ≥ 3` ⇒ `skipped`，等长静音 + 字幕保留）
- [ ] `version` 竞态保护（合成中稿件被编辑 ⇒ 丢弃结果）
- [ ] 产物落 `data/media/<date>/<task_id>/tts/s001.wav`
- ✅ `pytest tests/integration/test_sentence_resume.py -q`：①杀进程重启后已完成句**引擎调用次数为 0**（Mock 计数断言）②第 3 句注入失败 ⇒ 仅该句重试 ③编辑某句 ⇒ 仅该句失效重合成 ④缓存命中率 ≥30%
- ⚠️ **P2 按句可续传的核心验收点**：引擎调用为 0 是硬断言
- ⚠️ 陷阱 #26 单句编辑后其余句音频复用错位 ⇒ 编辑 ⇒ 时间轴**全量重算**

### T2.7 时长时间轴 · **P0**
- 依赖：T2.6 ｜ 里程碑：M2 ｜ 契约：§04.2.7
- [ ] `ffprobe` 逐句**实测**时长（**不信任引擎返回值**）
- [ ] 句间停顿 = `base + jitter(seed)`（±80ms，种子派生可复现）
- [ ] 累加得 `start_ms`/`end_ms`，末尾追加 `tail_ms`
- [ ] 写 `timeline.json` + `artifacts(kind='timeline')`
- [ ] 批量回写 `script_sentences.start_ms/end_ms`（事务内）
- [ ] **任一句重合成 ⇒ 时间轴全量重算**（**禁增量拼接**）
- ✅ `pytest tests/integration/test_timeline.py -q`（单调不重叠、总时长 = Σ句时长 + Σ停顿 + tail）；`voice_master.wav` 时长与 `timeline.total_ms` 偏差 ≤30ms
- ⚠️ **C12 修订**：`av_sync_audit` 降为**可选诊断**，不阻断发布
- ⚠️ 陷阱 #26 增量拼接 ⇒ 全量重算

### T2.8 配音阶段编排与降级演练 · **P0**
- 依赖：T2.7, T1.11 ｜ 里程碑：M2 ｜ 契约：§04.3.4
- [ ] voice stage 接入 orchestrator
- [ ] 守卫：`voicing → queued_render` 仅在**全部句 `done/skipped`** + timeline 校验通过后放行
- [ ] 故障注入开关 `STUDIO_FAULT`（供演练与测试）
- [ ] 降级演练：TTS 全挂 ⇒ **字幕模式**仍推进（不卡死）
- [ ] `pipeline.streaming_render` 默认 **false**（C7 裁决：配音全部完成才渲染）
- ✅ `studio pipeline run <task_id> --until voicing` 跑通；`STUDIO_FAULT=tts_fail_sentence=3` ⇒ 单句重试成功；`STUDIO_FAULT=tts_down=1` ⇒ 字幕模式仍推进到 `queued_render`
- ⚠️ **C7**：边配音边渲染会让时长基准漂移 ⇒ 默认关闭
- ⚠️ 原文 §3.3"渲染可逐句消费" ⇒ 保留为可选开关，非默认

### T2.9 配音服务化操作接口 · **P1**
- 依赖：T2.6, T1.7 ｜ 里程碑：M2 ｜ 契约：§04.3 / T4.5
- [ ] `POST /api/v1/sentences/{id}/resynth` 单句重配
- [ ] `GET /api/v1/media/{path}` 单句试听（HTTP Range）
- [ ] `PATCH /api/v1/tasks/{id}/voice_map` 任务级换音色（受影响句全部失效）
- [ ] 三者均写 `audit_ops`
- ✅ `POST /sentences/{id}/resynth` ⇒ 该句 `done`→`pending`→重合成 ⇒ **时间轴重算**（总时长变化可见）；单句 wav 可播放；换音色 ⇒ 受影响句全部失效
- ⚠️ 试听与合成抢 GPU ⇒ 试听**走缓存文件，不触发新合成**

> **>>> M2 门禁**：一句话用熊大音色读出；杀进程重启后已完成句**引擎调用为 0**；网页可见逐句进度。

---

## 3. 阶段 T3 · 渲染引擎（一期：单遍合成 + 固定水印 · 7 任务 → 门禁 M3）

> **v3.2 范围**：一期 = **音画合成 + 固定水印**，一次 ffmpeg 调用直出 `final.mp4`。
> **不做**句子↔镜头对齐（D2），**不做**场景中间产物与三层模板编排（C13 ⇒ 二期 P1）。

### T3.1 素材入库（跑酷 + BGM）· **P0** 🔴 需 E1/E3
- 依赖：T1.3 ｜ 里程碑：M3 ｜ 契约：§03.3.14 / §04.2.4
- [ ] 跑酷素材入库：通配 `parkour_*.mp4`、缩略图、**指纹（sha256 + pHash + 帧哈希）**、可用区间（`usable_from_ms/to_ms`）、`has_text` 标记
- [ ] **BGM 入库**（Q12）：`bgm_tracks` 表、`loudness_lufs`（ffmpeg 实测）、`mood`、`loopable` 标记
- [ ] **授权登记**：`license` 枚举强制（`self_recorded`/`authorized`/`cc0`/`purchased`）+ `proof_path`；**缺 license 直接拒绝入库**
- [ ] 重复素材按 sha256 / pHash 拒绝
- [ ] 黑帧段落自动排除
- [ ] `studio assets ingest --kind bgm --dir <源>` / `--kind parkour`
- ✅ `studio assets stats` 显示 clips 数/总时长/bgm 数；`pytest tests/integration/test_broll_ingest.py -q`（重复拒绝、黑帧排除、可用区间正确）；**缺 `license` 直接拒绝**
- ⚠️ **R3 版权** ⇒ 只收自录/授权 + 强制留档
- ⚠️ 🔴 **E1 素材未到位** ⇒ 走**黑屏降级**（§04.2.8.6）保证链路不断
- ⚠️ 陷阱 #14 素材被判搬运 ⇒ 随机化两档 + 相似度审计

### T3.2 水印资产与合成 profile · **P0** 🔴 需 E2
- 依赖：T1.2, T1.3 ｜ 里程碑：M3 ｜ 契约：§04.2.8.2
- [ ] `watermark.png` 入库与校验（尺寸/透明通道/路径存在）
- [ ] 水印参数：位置枚举 / 边距（**偶数**）/ 宽度（≤ 画布 1/4）/ 透明度
- [ ] `config/outputs.yaml` 合成 profile：**1080×1920 / 30fps / libx264 CRF21 / faststart / bt709**
- [ ] **720P 保底档**（渲染失败降级用）
- [ ] `studio render profile --show`
- ✅ `studio render profile --show` 打印合成 profile；水印 PNG 缺失 ⇒ **渲染直接报错 `RENDER_WATERMARK_MISSING`**（**不静默跳过**）；`pytest tests/unit/render/test_watermark.py -q`（位置枚举/边距偶数/宽度上限/透明度范围）
- ⚠️ **D5 水印是必做项** ⇒ 缺失**拒绝渲染**而非降级
- ⚠️ 位置越界 ⇒ **编译期**报错（不要留到 ffmpeg 运行时报）

### T3.3 `CompositePlan` 与单遍编译器 · **P0**
- 依赖：T3.1, T3.2, T2.7 ｜ 里程碑：M3 ｜ 契约：**§04.2.8.2 / §04.2.8.3 / §04.2.8.5**
- [ ] `CompositePlan` + `WatermarkSpec` 数据结构
- [ ] `total_ms = ffprobe(voice_master.wav).duration + tail_ms`（**音频为时长基准**）
- [ ] `filter_complex` 生成链：跑酷循环裁长 → 缩放铺满 → 水印 overlay → 混音
- [ ] **共用规则**（§04.2.8.4，一期同样适用）：`fps=30` 在 `scale` 前、`setpts=PTS-STARTPTS`、`setsar=1`、显式 `-t`、**禁用 `-shortest`**、`amix normalize=0`
- [ ] `ff_path()` 统一转义（Windows 驱动器冒号转 `\:`）
- [ ] **语法预检**：`ffmpeg -filter_complex_script … -f null -`（快速失败）
- [ ] 节点守卫：`estimated_nodes > 60` ⇒ 触发分块降级
- [ ] `studio render plan --task <id> --out plan.json`
- ✅ `pytest tests/unit/render/test_composite.py -q`：①`total_ms = ffprobe + tail_ms` ②`fps=30` 在 `scale` 前 ③`overlay` x/y 为偶数 ④`amix` 含 `normalize=0` ⑤`estimated_nodes > 60` 触发分块；`pytest tests/golden/test_filtergraph.py -q`（含中文/空格/冒号路径）；语法预检退出码 0
- ⚠️ **R8 复杂度爆炸** ⇒ 节点守卫 + 分块降级
- ⚠️ 陷阱 #6 路径报错 ⇒ 统一 `ff_path()`
- ⚠️ 陷阱 #7 命令行超长 ⇒ `-filter_complex_script` 文件
- ⚠️ 陷阱 #28 素材比人声短 ⇒ `loop` + `trim=duration=total_ms` 补齐；素材为空 ⇒ 纯黑底仍出片

### T3.4 单遍合成执行器 · **P0**
- 依赖：T3.3 ｜ 里程碑：M3 ｜ 契约：§01.5.2
- [ ] **argv 数组**调用（**不用 shell 拼接**，防注入与转义地狱）
- [ ] `-progress pipe:1 -stats_period 0.5` 进度解析（`out_time_us` → 百分比）
- [ ] 进度推送限流 2Hz
- [ ] 超时 / 取消 ⇒ **杀进程树**（`taskkill /PID <pid> /T /F`），清理半成品
- [ ] `.partial` → `os.replace` **原子改名**
- [ ] stderr 尾部 64KB 截断留痕 + 错误码映射
- [ ] `composite_hash` 整片缓存（同哈希二次运行**不调用 ffmpeg**）
- [ ] Windows 用 `BELOW_NORMAL_PRIORITY_CLASS` 启动
- ✅ `pytest tests/integration/test_composite_runner.py -q`：①进度可解析 ②超时杀进程树且不留子进程 ③中断 ⇒ 目标文件不存在但 `.partial` 被清理 ④argv 不含 shell 拼接（静态断言）⑤同 `composite_hash` 二次运行**不调用 ffmpeg**（Mock 计数）⑥人声或素材改动 ⇒ 哈希变化 ⇒ 重渲
- ⚠️ 陷阱 #9 崩溃留"假完成" ⇒ `.partial` + `os.replace`
- ⚠️ 陷阱 #8 缓存复用旧产物 ⇒ 哈希含 canonical plan + 输入 sha256 + 水印/字幕参数
- ⚠️ NVENC 失败 ⇒ 回退 `libx264`

### T3.5 字幕生成（Q11：**开启**）· **P0**
- 依赖：T3.3 ｜ 里程碑：M3 ｜ 契约：§04.2.6
- [ ] ASS 生成（**不用 `drawtext`**）：自动换行 / 每行 ≤13 字 / ≤2 行 / 描边 / 居中
- [ ] 说话人样式：`SpeakerA`/`SpeakerB` 换强调色
- [ ] 中文断行：**不在数字/英文单词中间断行**
- [ ] 安全区：`MarginV ≥ safe_area.bottom`
- [ ] 字体校验：引用 `assets/fonts/` 内字体，缺失**直接报错**（不产生豆腐块）
- [ ] 时间**只**取自 `voice_master` 实测的句级时长
- [ ] 编码 UTF-8 无 BOM + LF（便于 golden 比对）
- [ ] 落盘 `final/subtitle.ass`（永久保留，可二次剪辑复用）
- [ ] 开关 `render.burn_subtitle`（默认 **true**）
- ✅ `pytest tests/golden/test_ass.py -q`（golden 比对 ASS 文本）；`pytest tests/unit/render/test_subtitle.py -q`：①每行 ≤13 字、≤2 行 ②不在数字/英文单词中间断行 ③`MarginV ≥ safe_area.bottom` ④字体缺失 ⇒ 报错而非豆腐块 ⑤**关掉开关仍能出片**（可选性验证）
- ⚠️ 陷阱 #5 字幕豆腐块 ⇒ 内置字体 + `fontsdir` + 启动校验

### T3.6 混音与响度 · **P0**
- 依赖：T3.3 ｜ 里程碑：M3 ｜ 契约：§04.2.8.3 / §01.5.3
- [ ] 人声直通 + BGM **侧链 ducking**（threshold 0.05 / ratio 8 / attack 20 / release 420）
- [ ] **两遍 `loudnorm`**（第二遍带 `measured_*`）
- [ ] `amix` 显式 `normalize=0`（默认 `normalize=1` 会衰减人声）
- [ ] 限幅兜底 `alimiter=limit=0.95`
- [ ] BGM 缺失 ⇒ **单轨人声静音降级**（不报错）
- [ ] BGM 预对齐：按 `bgm_tracks.loudness_lufs` 先归一
- ✅ `python scripts/audio_qc.py --in voice_master.wav --mix final.mp4` → `lufs ∈ [-16.5,-15.5]`、`true_peak ≤ -1.0`；`pytest tests/integration/test_mixdown.py -q`：①`amix` 含 `normalize=0` ②`loudnorm` 两遍 ③`alimiter` 存在 ④**BGM 缺失 ⇒ 单轨人声且不报错**
- ⚠️ 陷阱 #4 人声偏小 ⇒ `normalize=0` + 两遍 loudnorm
- ⚠️ 削波 ⇒ `alimiter=limit=0.95`

### T3.7 成片交付、manifest 与降级链 · **P0**
- 依赖：T3.4–T3.6 ｜ 里程碑：M3 ｜ 契约：§04.2.8.6 / §03.3.11
- [ ] `final/final.mp4` 落盘 + `manifest.json`（含 `CompositePlan` + `composite_hash` + 随机化留痕 + 水印标记）
- [ ] `quality_json` 回填（响度/峰值/时长/水印命中）
- [ ] **三级降级链**：正常 → **720P 保底** → **黑屏降级** → **分块降级**
- [ ] 渲染反复失败 ⇒ `manual_pool`
- [ ] 相似度审计 `scripts/dup_audit.py` 接入
- [ ] `pipeline.run --until completed` 端到端跑通
- ✅ `studio pipeline run <task_id> --until completed` 产出 `final/final.mp4`；`pytest tests/e2e/test_render_e2e.py -m "e2e and slow" -q`；`audio_qc.py` 与 `dup_audit.py` 通过；**注入素材为空 ⇒ 黑屏出片**；**注入编码失败 ⇒ 720P 保底出片**
- ⚠️ **C12**：`av_sync` 仅诊断（不阻断发布）；保留 CFR + 显式 `-t` + 禁 `-shortest`（陷阱 #3）
- ⚠️ **水印缺失 ⇒ 拒绝出片**（不是降级）

### 二期（P1）预留 · 三层模板场景编排 · **P2**（不阻塞 M3）
- [ ] **T3-P1** 三层模板契约与加载器（YAML → Pydantic → DB；组件六类；八类校验）— 依赖 T3.7，契约 §03.6 / §04.2.0
- [ ] **T3-P2** IR 与构建器（`VideoIR` + bind pass + `$` 变量绑定）— 依赖 T3-P1，契约 §04.2.1
- [ ] **T3-P3** 自动填充 + `repeat_last` 场景扩展（克隆场景强制重抽素材、**禁绝对时间**）— 依赖 T3-P2，契约 §04.2.2 / §04.2.3
- [ ] **T3-P4** 场景级 filtergraph 编译 + 场景中间产物缓存 + 场景级重试 + 合流 — 依赖 T3-P3，契约 §04.2.5 / ADR-004
- **二期启动条件（三者同时满足）**：①一期稳定出片 ≥100 条 ②确有"片头/片尾/多段镜头编排"需求 ③磁盘与时间预算允许

> **>>> M3 门禁**：换稿不重剪（**同素材 + 同水印，换稿件直接出片**）；网页一键出新片并在线预览；水印/响度/相似度门禁通过。

---
## 4. 阶段 T4 · 网页实时操作台 + 四池并行 + 无人值守（13 任务 → 门禁 M4）

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

### T4.5 ④ 配音面板 · **P1**
- 依赖：T2.9, T4.1 ｜ 里程碑：M4 ｜ 契约：§04.3
- [ ] 逐句进度条（已完成 / 进行中 / 失败 / 跳过）
- [ ] 换音色（提示"将重配 N 句" + 二次确认）
- [ ] 重配某句
- [ ] 试听单句
- [ ] `skipped` 句高亮 + 原因可见
- ✅ 逐句状态实时刷新（WS `sentence.updated`）；重配 ⇒ 该句回 `pending` 并重合成；换音色 ⇒ 二次确认；试听可播放；`skipped` 高亮
- ⚠️ 重配期间渲染抢跑 ⇒ 守卫：`voicing` 态不允许 render 认领
- ⚠️ 试听抢 GPU ⇒ **走缓存文件，不触发新合成**

### T4.6 ⑤ 渲染面板 · **P1**
- 依赖：T3.4, T3.7, T4.1 ｜ 里程碑：M4 ｜ 契约：§04.2.8
- [ ] **单遍合成进度**（按 `-progress` 推进）
- [ ] ffmpeg 日志（按 `source=render.*` 过滤 + 高亮 `warn`/`error`）
- [ ] 成片列表 + **在线播放**（HTTP Range）
- [ ] 触发渲染（入队 render 池）
- [ ] 水印缺失 ⇒ 面板显示 `RENDER_WATERMARK_MISSING` 且不出片
- ✅ 合成进度推进（WS `render.progress`）；日志过滤生效；成片可在线播放；触发渲染入队；水印缺失可见报错
- ⚠️ 日志量大 ⇒ 只订阅 `render.*` + 环形缓冲 200 条
- ⚠️ 重复触发 ⇒ 幂等键 + `composite_hash` 缓存直接命中
- 📌 **"场景进度/换模板"随 C13 延后二期**

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
- ⚠️ 水印 PNG 不在盘上 ⇒ 渲染**拒绝出片**（D5）⇒ 面板顶部常驻红条（不是一行小字）
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
- [x] 统计：`clips ≥ 60 且 ≥ 30min`（判据线**现取**下发，前端不抄第二份）
- [x] **R2 合规提示常驻**
- [x] 误删保护：**只允许禁用，不物理删除** + `audit_ops`
- ✅ 列表显示完整字段；三类素材均可扫盘入库；BGM 入库即被混音选中；禁用生效；统计达标；R2 提示常驻
- **验收命令**：
  - `.venv\Scripts\python.exe -m pytest tests/unit/assets tests/unit/core/test_media.py tests/unit/core/test_files.py tests/unit/db/test_asset_repo.py tests/unit/services/test_asset_service.py tests/integration/test_assets_api.py -q` ⇒ **222 例**（76 + 51 + 8 + 21 + 40 + 26）
  - `cmd /c "cd /d %CD%\web && npm run test"` ⇒ **284 例**（14 文件，含 `assets.test.ts` **18 例**）
  - `.\tasks.ps1 check` ⇒ **2468 passed / 32 skipped / 1 deselected**；`.\tasks.ps1 web:verify` 全绿 · dist **0.26 MB**（门禁 3 MB）
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
- ⚠️ **E1 / E3 / E4 仍缺**：跑酷 / BGM / 原声都没到位 ⇒ 现用 `scripts/seed_placeholder_assets.py` 造的**占位素材**撑门禁（60 跑酷 × 32s = 32 分钟 + 3 BGM + 2 音色，实测 **74s** 跑完）；它们**一律带 `tags: ["placeholder"]` 并在面板标黄**，绝不假装成正式素材
- ⚠️ **E2 水印 PNG 缺失 ⇒ 渲染拒绝出片**（D5）—— 与素材库无关，但属于同一批外部依赖
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

### T5.1 快速封面 + 发布前二次校验 · **P1**
- 依赖：T3.7 ｜ 里程碑：M5 ｜ 契约：§04.1.8 / §06.3 / §06.4
- [ ] Cover Agent 封面文案 + 1080×1920 封面图生成
- [ ] 发布前**重跑三道门禁**：水印校验 / 响度（`audio_qc`）/ 相似度（`dup_audit`）
- [ ] 封面与文案**禁区扫描**
- [ ] 审计不通过或命中禁区 ⇒ **拒绝发布**并转 `manual_required`（**不静默放行**）
- [ ] `av_sync` **仅诊断**（C12：不阻断发布）
- ✅ `studio publish cover --task <id>` 产出 1080×1920 封面；审计不通过 ⇒ 拒绝发布并转人工；水印缺失 ⇒ 拒绝发布
- ⚠️ **R14 发布不可逆** ⇒ 默认关闭 + `require_confirm=true` + 三道门禁
- ⚠️ 陷阱 #27 成片没水印 ⇒ 发布前**再校验一次**

### T5.2 发布适配层与 profile · **P1**
- 依赖：T5.1 ｜ 里程碑：M5 ｜ 契约：§04.6.1 / §06.2 / §06.5
- [ ] `Publisher` ABC（平台实现可替换）
- [ ] 一线三平台实现：**抖音 / 快手 / 视频号**（Q9：二线留接口）
- [ ] 选择器集中到 `publish/selectors/*.yaml` + `selectors_version` 留痕
- [ ] 失败截图 + DOM 快照留档
- [ ] 登录态 `health()` 探测：失效 ⇒ 转 `manual_required` + 告警（**不自动登录、不绕过验证码**）
- [ ] 发布后**回读标题与文案逐字比对**，不一致 ⇒ 重填 ≤2 次
- [ ] `studio publish dry-run --task <id> --platform douyin`
- ✅ `pytest tests/contract/test_publisher_abc.py`；`dry-run` 走完流程到"确认发布"前一步并截图；`health()` 能正确报告"未登录"与"登录态已过期"
- ⚠️ 陷阱 #23 平台改版 ⇒ 选择器集中配置便于热修
- ⚠️ 陷阱 #21 登录态失效被静默跳过 ⇒ 探测 + 转人工
- ⚠️ 陷阱 #24 文案被编辑器吞掉 ⇒ 回读比对
- ⚠️ **R18** 多平台工作量 ⇒ 分层实施，一期只做一线

### T5.3 发布池 + 限频 + 失败转人工 · **P0**
- 依赖：T5.2, T1.5 ｜ 里程碑：M5 ｜ 契约：§03.4.4 / §06.5.4
- [ ] `publish` 池 worker
- [ ] 限频：**≤3 条/天/账号** + 间隔 ≥30min
- [ ] 重试 ≤3 次指数退避
- [ ] 失败转"待人工发布"（`manual_required`）
- [ ] **幂等键含 `account_id`**（`task_id + platform + account_id`）
- [ ] 发布失败**不回退任务状态**（任务仍为 `completed`）
- ✅ `pytest tests/integration/test_publish_pool.py -q`：①第 4 条当天发布被限频拒绝（**自动顺延，不报失败**）②连续失败 3 次 ⇒ `manual_required` 且任务仍 `completed` ③幂等键防重复发布
- ⚠️ **R13 风控** ⇒ 限频 + 登录态探测 + 转人工 + **不实现验证码绕过**

### T5.4 数据回收 + 记忆沉淀闭环 · **P1**
- 依赖：T5.3 ｜ 里程碑：M5 ｜ 契约：§06.6 / §06.8
- [ ] 采集时点：**T+1h / 6h / 24h / 72h**
- [ ] `metrics_json` + `metrics_history_json` 落库
- [ ] `next_metric_at` 由 `metrics_service` 轮询（**定时器，非队列**）
- [ ] `data/feedback/auto_YYYYMM.md` 生成
- [ ] **闭环验证**：`auto_*.md` 可被 §04.1.7 解析器消费（回流下轮 Planner）
- ✅ T+1h 能在发布面板看到数据；`auto_YYYYMM.md` 生成且**能被解析器消费**
- ⚠️ 采集失败 ⇒ 顺延重试，不阻断其他任务

### T5.5 发布面板 + 合规留档 + 外部对接 · **P1**
- 依赖：T5.4 ｜ 里程碑：M5 ｜ 契约：§06.11 / §06.12
- [ ] 面板**七区块**：待发布 / 发布中 / 已发布 / 数据回流 / **待人工** / **定时计划** / **报告**
- [ ] `manual_required` 可重试 / 可标记已人工处理 / 可取消（**三者均写 `audit_ops`**）
- [ ] `HandoffAdapter`（默认 `LocalHandoffAdapter`，可 push 自包含交付包：视频/封面/ASS/稿件/manifest/质检）
- [ ] 来源登记留档（R2）
- [ ] 应急剧本：`docs/runbook/publish_selector.md`、`publish_account.md`
- [ ] **R2 合规提示常驻**（发布面板 + 素材库）
- ✅ 七区块可用；`manual_required` 三种操作均留痕；交付包可导出；两个剧本齐备
- ⚠️ `manual_required` 堆积 ⇒ 面板顶部计数 + 告警
- ⚠️ 剧本过时 ⇒ 每次页面改版热修后更新剧本版本号

### T5.6 定时发布调度 · **P0**（★D7 + Q14）
- 依赖：T5.3 ｜ 里程碑：M5 ｜ 契约：**§03.3.18 / §04.6.5.1 / §06.5.5**
- [ ] `services/scheduler_service.py`：**30s tick**，`next_run_at` **落库**
- [ ] 三种模式：`at_time` / `daily_window` / `interval`
- [ ] 窗口内**随机取时刻**：`HMAC(seed = schedule_id + 日期)` ⇒ 同一天多次计算得**同一时刻**（幂等，重启不漂移）
- [ ] 抖动 `jitter_min`（默认 15，≤120），**不得越出窗口**（越界夹取到边界）
- [ ] **到点才建 job**（不预占，**不占 worker 空转**）
- [ ] **策略可编辑（Q14）**：模式 / 窗口 / 抖动 / 平台 / 账号 / 启停全部 WebUI 可编辑
- [ ] 编辑 ⇒ **同事务重算 `next_run_at`** + `audit_ops`（陷阱 #33）
- [ ] 参数校验：`HH:MM` 且 `start < end`；`jitter_min ∈ [0,120]`；`at_time` 带时区；非法 ⇒ **400 且不落库**
- [ ] 被限频 ⇒ `last_result='skipped_ratelimit'` + 顺延（**不算失败**）
- [ ] `publish.enabled=false` ⇒ 空转 + `skipped_disabled`
- [ ] 连续失败 ≥5 次 ⇒ `system.alert`
- [ ] REST：`GET/POST /api/v1/schedules`、`PATCH/DELETE /api/v1/schedules/{id}`、`POST .../run_now`
- [ ] WS：`publish.scheduled` / `publish.schedule_fired`
- ✅ `pytest tests/integration/test_scheduler.py -q`：①窗口模式在 `[18:00,21:30]` 内取时刻且叠加 ≤15min 抖动 ②**同 schedule + 同一天多次计算得到同一时刻** ③未到点/已停用的计划**不**被取到 ④到点 ⇒ 创建 `publish` job 且不占 worker ⑤被限频 ⇒ `skipped_ratelimit` + 顺延 ⑥`enabled=false` ⇒ `skipped_disabled` ⑦连续失败 ≥5 ⇒ 告警 ⑧增删改启停均写 `audit_ops` ⑨**非法参数被拒绝**且不落库 ⑩编辑 ⇒ **同事务重算 `next_run_at`**
- ⚠️ 陷阱 #29 变成"每天准点"的机器特征 ⇒ 窗口随机 + 抖动
- ⚠️ 陷阱 #30 重启丢计划 ⇒ `next_run_at` **落库**
- ⚠️ 陷阱 #33 只改参数不重算 ⇒ 不生效或立刻触发
- ⚠️ 调度器本身崩溃 ⇒ supervisor 守护（T4.11）

### T5.7 数据报告与决策闭环 · **P0**（★D9 + Q15）
- 依赖：T5.4 ｜ 里程碑：M5 ｜ 契约：**§03.3.19 / §03.3.20 / §04.6.5.2 / §06.7**
- [ ] `services/report_service.py`：**纯 SQL 聚合 + 规则归因**（**不调 LLM**）
- [ ] 七维归因：①选题类型 ②发布时段 ③平台 ④时长 ⑤稿件评分 ⑥TTS 质量 ⑦成本
- [ ] 产物：`summary_md`（模板渲染）+ `data_json`（图表）+ `insights_json` + `artifacts_json` + `coverage_json`
- [ ] `insights` 置信度：`n<10` ⇒ `low`（**必须**标注"样本不足"）/ `10–29` ⇒ `medium` / `≥30` ⇒ `high`
- [ ] **周期可编辑（Q15）**：`report_schedules` CRUD；`period`/`weekday`/`day_of_month`/`at_time`/`tz`/`lookback_days`/`include`/`enabled`
- [ ] **同周期仅 1 个启用**（部分唯一索引）；`is_builtin=1` **不可删、只能停用**
- [ ] 编辑 ⇒ 重算 `next_run_at` + `audit_ops`
- [ ] **决策闭环**：`POST /api/v1/reports/{id}/insights/{idx}/apply` ⇒ 写入 persona 偏好项 / `content_directions` 权重 ⇒ **影响下轮 Planner**，写 `audit_ops(action='report.apply_insight')`
- [ ] `GET /api/v1/reports/{id}/export?format=md|csv`
- [ ] WS：`report.generated` / `report.schedule_updated`
- ✅ `pytest tests/integration/test_reports.py -q`：①周报含 `summary_md` + `data_json` + `insights` ②**同周期重复生成不重复插入** ③`n<10` ⇒ `confidence='low'` 且 WebUI 强制显示"样本不足" ④`apply` ⇒ 写入 persona/方向权重 + `audit_ops` ⑤**闭环**：采纳后下轮 Planner 的 `grounded_on` 可见该偏好 ⑥非法 `period` 被 CHECK 拒绝 ⑦**周期编辑** ⇒ `next_run_at` 重算 + `audit_ops` ⑧**同周期仅 1 个启用** ⑨停用 ⇒ 不再被到期查询取到
- ⚠️ 陷阱 #31 报告被当"结论"直接改生产策略 ⇒ `insights` **必须人工采纳**（**不自动改配置**）
- ⚠️ 陷阱 #32 报告烧钱 ⇒ **不调 LLM**（纯 SQL）
- ⚠️ 采纳不可追溯 ⇒ `audit_ops` 记 `before/after`

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

> **>>> M5 门禁**：成片**定时/即时**自动发布（≥1 平台）+ 数据回流 + **报告生成与决策采纳** + 记忆沉淀闭环（`auto_*.md` 可被解析器消费）。

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
uv run studio db check                           # 29 表 / WAL / 完整性 / 外键
uv run pytest -m "not gpu and not slow and not net" -q    # 快速回归（CI 门槛）
uv run pytest -m contract -q                     # 全部契约测试
uv run pytest -m "e2e and slow" -q               # 端到端（里程碑前跑）
python scripts/audio_qc.py --task <id>           # 响度/峰值（发布门禁）
python scripts/dup_audit.py --task <id>          # 相似度（发布门禁）
python scripts/av_sync_audit.py --task <id>      # 仅诊断（C12：不阻断发布）
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
| **M5** | 成片**定时/即时**自动发布（≥1 平台）+ 数据回流 + **报告生成与决策采纳** + 记忆沉淀闭环 | T5.1–T5.8 | [ ] |

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
| **T2** CosyVoice 配音 | 9 | **1**（T2.5 ✅） | M2 | [ ] |
| **T3** 渲染（一期单遍合成） | 7 | 0 | M3 | [ ] |
| **T4** 操作台 + 四池 + 无人值守 | 13 | **11**（T4.1 ✅ T4.2 ✅ T4.3 ✅ T4.4 ✅ T4.7 ✅ T4.8 ✅ T4.9 ✅ T4.10 ✅ T4.11 ✅ T4.12 ✅ T4.13 ✅） | M4 | [ ] |
| **T5** 发布 + 定时 + 报告 | 8 | 0 | M5 | [ ] |
| **合计（一期）** | **49** | **24** | — | — |
| *T3-P1…T3-P4* | *4（二期）* | *0* | — | *不占一期工期* |

**外部阻塞项**：E1 跑酷素材 🔴 / E2 水印 PNG 🔴 / E3 BGM 🔴 / E4 原声 🔴 / E5 CosyVoice 权重 🔴 / E6 LLM Key 🟡（T1.9 真机联调前，不阻塞编码）/ E7 persona 🟡 / E8 字体 🟡

> 注 1：`T1.2` 含 **T1.2+ persona 可编辑改造**（人物库 / 热重载 / 一键切换 / 自动备份）。E7 现有 2 套可跑人物（`persona_default` 熊大熊二 · `solo_commentary` 快嘴单人），**口吻 / 受众 / 禁区仍待你定稿内容**（**可编辑性已就位**，见注 4）。
>
> 注 2：**M1 仍未算过**（T1.12 裁定 108）：一键启动已可用，但「5 进程全 ready」要等 T2.2 / T2.6 / T3.x / T4.11
> ⇒ M1 的验收口径 = 「`api` ready + 其余**如实报降级**且不阻塞」。T2.1 起被 E5（CosyVoice 权重）硬阻塞 ⇒ **已先做 T4.1**（前端脚手架，P1，无外部依赖，2026-09-14 ✅）。
>
> 注 3：**下一批可开工任务**（依赖已满足，不碰 E1–E5）——
> ① ~~`T2.5` 文本归一化与切分~~ ⇒ **已完成（2026-09-15）**，见上方任务块。
> ② **`T3.1` 素材入库补全**（P0 · 依赖 T1.3 ✅）：T4.8 已建好扫盘 / sha256 / 时长 / 响度 / 缩略图 / 入库主干，**缺口** = pHash + 帧哈希、黑帧段落排除、`studio assets ingest --kind parkour|bgm` CLI、`tests/integration/test_broll_ingest.py`；E1/E3 用 `scripts/seed_placeholder_assets.py` 的占位素材撑门禁。
> ③ **`T3.2` 水印资产与合成 profile**（P0 · 依赖 T1.2 / T1.3 ✅）：T4.7 已做配置侧，**缺口** = `watermark.png` 入库校验（尺寸 / 透明通道 / 存在性）、水印参数模型（位置枚举 / 偶数边距 / 宽度 ≤ 画布 1/4 / 透明度）、`studio render profile --show`、**`RENDER_WATERMARK_MISSING` 硬门禁**（D5：缺失 ⇒ 拒绝渲染，不降级）、`tests/unit/render/test_watermark.py`。
> ④ **`T2.9` 配音服务化操作接口**（P1 · 依赖 T2.6 / T1.7）与 **`T2.7` 时长时间轴**（P0 · 依赖 T2.6）：都要等 T2.6；`T2.6` 又依赖 T2.3–T2.5 ⇒ 现在**只剩 `T2.3`/`T2.6` 之间被 T2.1 卡着**。
>
> **②③ 之后全线硬阻塞**：`T2.1`（E5 权重）⇒ T2.2–T2.9 ⇒ `T3.3`（还需 T2.7 时间轴）⇒ T3.4–T3.7 ⇒ `T4.6` 与 `T5.1` 起全部；`T4.5` 另需 T2.9。
> ⇒ **E5（CosyVoice 权重）与 E2（水印 PNG）仍是关键路径瓶颈**，其余外部项只影响各自任务的真机验收。
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
| 27 | 成片没水印 | 水印缺失被当成"可选"静默跳过 | **水印必做** ⇒ 缺失即**拒绝渲染**，发布前再校验 | T3.2 / T5.1 |
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

> 本节是常用子集，**编号与 `docs/spec/05-roadmap-checklist.md` §5.7 完全一致**（完整 93 条见该处；跨文档引用按编号即可）。

---

## 11. 交付物清单（每阶段结束时必须齐备）

| 类别 | 内容 |
| --- | --- |
| 代码 | `src/studio/**`、`workers/**`、`web/**`、`scripts/**` |
| 契约 | `schemas/*.schema.json`、`tests/contract/**`、`tests/golden/**` |
| 配置 | `config/*.yaml`（含 `persona.yaml` 实例）、`prompts/**`、`templates/**` |
| 文档 | `docs/spec/**`（规格书）、`docs/adr/**`、`docs/runbook/**`（6+2 个剧本）、`docs/qc/**` |
| 运维 | `启动.bat` / `停止.bat` / `ops/*.ps1` / `scripts/backup_db.ps1` / `scripts/restore_db.ps1` |
