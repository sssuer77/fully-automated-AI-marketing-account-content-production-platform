# §01 技术栈选型与依赖矩阵

> 所有选型以 **单机本地部署 · 零外部服务 · 可崩溃自愈** 为第一优先级。条款编号 `§01.x`。

---

## 1.1 选型总览

| 层 | 选型 | 版本锚点 | 核心理由 | 否决的方案 |
| --- | --- | --- | --- | --- |
| 后端框架 | **FastAPI** | ≥0.115 | 原生 async、自动 OpenAPI、Pydantic2 强校验、WS 一等公民 | Flask（WS/async 弱）、Django（重） |
| ASGI 服务器 | **Uvicorn**（h11 + asyncio） | ≥0.34 | Windows 不支持 uvloop，用标准 asyncio 循环最稳 | Hypercorn、uvloop（Windows 不可用） |
| 实时通道 | **FastAPI WebSocket** | 内置 | 与 REST 同进程、复用 Pydantic 序列化 | Socket.IO、SSE、轮询 |
| 数据库 | **SQLite**（WAL） | ≥3.35（本机 3.51.1） | 零运维、单文件备份、`UPDATE...RETURNING` 支持原子认领 | PostgreSQL（过度）、Redis（外部服务） |
| 异步驱动 | **aiosqlite** | ≥0.20 | 轻量、无 C 扩展 | asyncpg（不适用） |
| ORM | **SQLAlchemy 2.0 async**（CRUD）+ **原生 SQL**（队列认领） | ≥2.0.30 | ORM 管常规 CRUD；认领必须单语句原子 | 纯 ORM 做队列（语义不达） |
| 任务池 | **DB-as-Queue + 租约**（§1.4） | — | 见论证 | Celery / rq / asyncio.Queue |
| 音视频引擎 | **FFmpeg 子进程 + `-filter_complex_script`**（§1.5） | gyan.dev full ≥6.1 | 见论证 | ffmpeg-python / PyAV / MoviePy |
| 本地 TTS | **CosyVoice（零样本复刻）+ 常驻 HTTP 服务** | 权重版本锁定留痕 | 本地离线、零样本音色复刻、指令控制 | 云端 TTS（违反 P1）、EdgeTTS（不可控） |
| 推理运行时 | **PyTorch cu121 + fp16** | 2.4.0+cu121（本机已备 wheel） | 与 Turing sm_75 兼容且已预下载 | bf16（Turing 不支持）、CPU 推理（RTF 不可接受） |
| LLM 网关 | **双通道**：云端 [OI] 兼容 API ＋ 本地（Ollama / DeepSeek 本地版 / llama.cpp） | 按 `config/llm.yaml` | 原文 §7.1 要求可选本地或云端 | 单一云端（断网不可用） |
| 发布执行器 | **Playwright（Chromium）浏览器自动化** | ≥1.48 | 见 §1.8 论证 | 开放 API（个人号不可得）、第三方 SaaS |
| 封面生成 | FFmpeg（抽帧 + drawtext 叠加标题） | 已有 FFmpeg | 不引入额外图像栈 | Pillow / Canvas（多余依赖） |
| 前端 | **Vue3 + Vite + TS + Pinia** | Vue ≥3.4 / Vite ≥5 | 状态复杂（看板/日志流/逐句编辑） | Jinja2+HTMX（降级方案，§1.7） |
| 定时任务 | **APScheduler** | ≥3.10 | GC / 备份 / 心跳巡检 / **数据回收** | Celery Beat |
| 进程守护 | **自研 supervisor 进程**（或 nssm） | — | 5 个常驻进程自动重启 | PM2 |

---

## 1.2 后端与网关

### 1.2.1 进程与端口拓扑

| 进程 | 入口命令 | 端口 | 并发 | 重启策略 | 说明 |
| --- | --- | --- | --- | --- | --- |
| API / WebUI 网关 | `studio serve` | `127.0.0.1:8787` | 1（**禁用 `--workers >1`**） | 常驻，崩溃重启 | 进程内持有 WS hub；多 worker 会导致状态分裂 |
| TTS 推理服务 | `studio tts serve` | `127.0.0.1:8811` | 1（GPU 串行） | 崩溃自动重启 + 冷加载 | 独立 venv，模型常驻显存 |
| draft worker | `studio worker run --pool draft` | — | **2** | 常驻 | LLM 调用为主（纯网络 I/O） |
| voice worker | `studio worker run --pool voice` | — | **1**（可调 1–3） | 常驻 | HTTP 调用 TTS，占位不占 GPU |
| render worker | `studio worker run --pool render` | — | **1**（可调 2） | 常驻 | 派生 ffmpeg 子进程 |
| **publish worker** | `studio worker run --pool publish` | — | **1** | 常驻 | 串行发布（R13 限频 + 账号风控） |

> **绑定安全**：默认只绑 `127.0.0.1`，不做公网暴露。

### 1.2.2 接口分层

```
/api/v1/overview                          总览台聚合
/api/v1/hot            POST(import)       热点导入
/api/v1/feedback       POST(import)       反馈导入
/api/v1/topics         POST(analyze|ideate|select)   选题池
/api/v1/tasks          POST/GET/PATCH     任务创建·查询·取消·重试
/api/v1/tasks/{id}/approve | reject | discard | approve_batch    确认闸
/api/v1/scripts        GET/PUT            稿件与逐句编辑
/api/v1/sentences/{id}/resynth            单句重合成
/api/v1/voices         GET/POST/DELETE    音色档案与试听
/api/v1/templates      GET/POST/validate  三层模板
/api/v1/assets         POST(ingest)/GET/stats   跑酷·BGM·字体素材库
/api/v1/renders/{id}   GET                渲染进度与产物
/api/v1/publish        POST(queue|retry)/GET(queue)   发布队列
/api/v1/metrics        GET                数据回收
/api/v1/pools          GET/PATCH          四池状态与并发/暂停
/api/v1/logs           GET(分页/过滤)      日志回溯
/api/v1/audit          GET(actor/action)  操作审计
/api/v1/media/{path}   GET(支持 Range)    成片/音频播放
/api/v1/health | /doctor                  健康与自检
/ws/ui                                    WebSocket 下行广播（§4.4）
/static/*, /                              构建后的 WebUI 静态资源
```

> **上下行分离原则**：WS **只做下行广播**；所有写操作走 REST。理由：幂等键、鉴权、审计、错误码语义在 HTTP 上更清晰，且避免 WS 半开连接下命令丢失。

### 1.2.3 中间件与横切关注点

| 关注点 | 实现 | 契约 |
| --- | --- | --- |
| 请求追踪 | `X-Request-ID` 生成/透传 → 写入 `system_logs.trace_id` | 所有 REST 响应头回显 |
| 访问日志 | 中间件记录到 `system_logs`（`source='http'`，5xx 为 `error`） | 不记录敏感 body |
| 异常映射 | 领域异常 → HTTP 状态码（`core/errors.py`） | 响应体恒为 `{code, message, detail, trace_id}` |
| 参数校验 | Pydantic v2 model | 生成 OpenAPI 供前端 `types.ts` 自动生成 |
| **操作审计** | `audit_service` 中间件：改变状态的写操作自动落 `audit_ops` | 记录 `actor/action/target/before/after`（原文 §7.4） |
| 静态托管 | 生产：`StaticFiles` 托管 `web/dist`；开发：Vite `5173` 代理 `/api` | 单端口交付 |

### 1.2.4 LLM 双通道网关

```
                    ┌─────────────────────────────────────────┐
   Agent 调用 ────▶ │  llm_client（统一接口 + 结构化输出守卫）  │
                    └───────────────┬─────────────────────────┘
                                    │ 按 config/llm.yaml 的 routing 决策
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
        ┌───────────────────────┐       ┌───────────────────────────┐
        │ 云端通道（首选，快）   │       │ 本地通道（兜底/离线）      │
        │ [OI] 兼容 /chat/...   │       │ Ollama / DeepSeek 本地版   │
        │ httpx + 重试 + 记账   │       │ http://127.0.0.1:11434    │
        └───────────────────────┘       └───────────────────────────┘
                    │                               │
                    └───────────┬───────────────────┘
                                ▼
                  llm_calls 表（成本/延迟/token/是否本地/成败）
```

```yaml
# config/llm.yaml 契约
schema_version: "1.0"
default_profile: cloud
profiles:
  cloud:
    engine: oi_compatible
    base_url: https://.../v1
    api_key_env: STUDIO_LLM_API_KEY    # ★ 密钥只从环境变量读，绝不入 yaml/git
    model: <模型名>
    timeout_sec: 120
    max_retries: 3
    temperature: 0.8
    json_mode: true
    cost_per_1k_in: 0.0                # 用于 llm_calls.cost_usd 估算
    cost_per_1k_out: 0.0
  local:
    engine: ollama                     # ollama | oi_compatible | llama_cpp
    base_url: http://127.0.0.1:11434
    model: <本地模型名>
    timeout_sec: 300
    json_mode: true
routing:                               # 按 Agent 分派
  planner:  {profile: cloud, fallback: local}
  ideator:  {profile: cloud, fallback: local}
  director: {profile: cloud, fallback: local}
  writer:   {profile: cloud, fallback: local}
  reviewer: {profile: cloud, fallback: local}
  editor:   {profile: cloud, fallback: local}
  cover:    {profile: local}           # 轻任务走本地省成本
budget:                                # R16：成本闸门
  per_task_token_limit: 60000
  per_day_cost_usd_limit: 5.0
  on_exceed: switch_to_local           # switch_to_local | fail_task | alert_only
```

| 硬约束 | 说明 |
| --- | --- |
| 密钥管理 | 只从环境变量读（`STUDIO_LLM_API_KEY`）；`config/llm.yaml` 只允许出现变量**名**（`api_key_env`），写密钥本体被 `config.validate` 直接拒绝 |
| 结构化输出 | 所有 Agent 输出必须过 JSON Schema（Draft 2020-12）校验；**修复重试 ≤3 按"重试次数"计 ⇒ 每通道 ≤4 次调用**（裁定 55），耗尽后切 `fallback` 通道（裁定 61）；解析失败**禁正则补救** |
| 成本记账 | **每次尝试**写 `llm_calls`（含 `is_local`）—— 失败也写（裁定 60），否则"反复重试"能绕开预算闸门；总览台展示"今日花费"（原文"看钱"） |
| 预算闸门（R16） | 两条独立判定：任务级 token（`llm_calls` 按 `task_id` 求和）+ 当日成本；上限取 `min(配置上限, 已花 + token_budget_remaining)` ⇒ 上游提示只可能**收紧**闸门（裁定 63） |
| 熔断 | 同一通道连续失败 ≥3 ⇒ OPEN 60s，期间**直接跳过**该通道（`LLM_CIRCUIT_OPEN`）；到点转 HALF_OPEN 放**一个**探针 |
| 记账定序 | `llm_calls` 一律 `ORDER BY created_at DESC, rowid DESC` —— `created_at` 只到毫秒而 ULID 同毫秒内随机，用 `id` 定序会把"第几次尝试"随机打乱（裁定 62） |
| 分层 | `agents/` 不得 import `services/`：日志出口用 `LogSink` 回调注入（裁定 54） |
| 本地可用性 | `studio llm probe` 探测本地通道（`GET /api/tags`）与云端（`GET /models`，只读不烧钱）；`unreachable` **或** `http_error` ⇒ 退出码 1（裁定 64） |

### 1.2.5 鉴权策略（原文 §7.4）

| 场景 | 策略 |
| --- | --- |
| 仅本机（`127.0.0.1`） | **免密**；仅绑定回环地址 |
| 局域网（`STUDIO_ALLOW_LAN=1`） | **强制密码**（首次启动生成随机密码写入 `config/secrets.yaml` 并打印到控制台） |
| 发布相关接口 | 额外**二次确认**（`publish.require_confirm=true`，R14） |

**安全底线**：`doctor` 检查"未设密码但已开启局域网"为**致命错误**。

---

## 1.3 数据库与状态机

### 1.3.1 PRAGMA 调优（连接建立即执行，`db/engine.py`）

```sql
PRAGMA journal_mode = WAL;         -- 读写并发；崩溃安全
PRAGMA synchronous  = NORMAL;      -- WAL 下的性能/安全平衡点
PRAGMA busy_timeout = 5000;        -- 竞争时等待而非立即报错
PRAGMA foreign_keys = ON;          -- 引用完整性
PRAGMA temp_store   = MEMORY;
PRAGMA cache_size   = -32000;      -- 32MB 页缓存
PRAGMA mmap_size    = 268435456;   -- 256MB 内存映射
PRAGMA wal_autocheckpoint = 1000;  -- 控制 WAL 膨胀
```

**硬性禁忌**：数据库文件禁止放在网络盘 / OneDrive / SMB / WSL 跨文件系统路径（WAL 需本地文件系统与共享内存）。`doctor` 必须断言 `journal_mode == 'wal'`，否则拒绝启动。

### 1.3.2 三层并发控制模型（无锁优先）

| 层级 | 机制 | 解决的问题 | 实现要点 |
| --- | --- | --- | --- |
| **任务级** | `tasks.version` 乐观锁 | WebUI 编辑与 worker 推进的状态竞态 | `UPDATE tasks SET ..., version=version+1 WHERE id=? AND version=?`，影响行数 0 ⇒ HTTP 409 |
| **Job 级** | **租约**（`lease_owner` + `lease_expires_at`） | 重复执行 / 崩溃残留 / 多进程争抢 | 单语句 `UPDATE ... WHERE id=(SELECT ...) RETURNING ...`（§3.4） |
| **句级** | `script_sentences.version` + `tts_status` | 逐句编辑与合成竞态 | 编辑即 `version+1`；合成完成后 `WHERE version=?` 才落盘音频，否则丢弃 |

**单写者原则**：Worker 与 API 的写操作全部走**短事务**（目标 <20ms，禁止在事务内做 I/O、HTTP、ffmpeg 等待）；必要时经 `db/write_queue.py` 串行化。长事务是 `database is locked` 的唯一根因。

### 1.3.3 为什么不是 PostgreSQL / 为什么不是 ORM 队列

- 单机、单用户、<1000 任务/天 ⇒ PostgreSQL 带来额外服务进程与迁移运维，收益为零；
- 但**队列语义必须自行实现**（`FOR UPDATE SKIP LOCKED` 的 SQLite 等价物 = `UPDATE ... WHERE id=(SELECT ... LIMIT 1) RETURNING`），这部分**不用 ORM**，以免生成 `SELECT` + `UPDATE` 两次往返（产生竞态窗口）。

### 1.3.4 备份与恢复

| 手段 | 命令 | 频率 | 验收 |
| --- | --- | --- | --- |
| 在线备份 | `sqlite3 studio.db "VACUUM INTO 'backups/studio_YYYYMMDD.db'"` | 每日 + 批量任务前 | 可 `PRAGMA integrity_check` 通过并成功挂载 |
| 完整性检查 | `PRAGMA integrity_check` / `foreign_key_check` | 每周 | 返回 `ok` |
| 恢复演练 | 恢复脚本 + 只读启动校验 | 每月（T4.12 验收项） | 恢复后任务列表与产物引用一致 |
---

## 1.4 任务池实现方案论证（核心决策）

### 1.4.1 候选方案 × 维度对比

| 方案 | 持久化 | 跨进程共享 | 优先级 | 租约/超时回收 | 任务依赖 | 崩溃恢复 | Windows 兼容 | 运维成本 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `asyncio.Queue` | ❌ 内存 | ❌ 进程内 | ❌ 需自建 | ❌ | ❌ | ❌ 重启即丢 | ✅ | 0 | 仅作**进程内唤醒信号** |
| **Celery + Redis** | ✅ | ✅ | ✅ | ✅ | ⚠️ 弱 | ✅ | ⚠️ 需 Redis for Windows；无 prefork，需 `--pool=solo/threads` | **高** | ❌ 否决 |
| **rq** | ✅ | ✅ | ✅ | ❌ 无自动超时回收 | ❌ | ⚠️ | ⚠️ 需 Redis | 中 | ❌ 否决 |
| **Dramatiq** | ✅ | ✅ | ❌ | ✅ | ❌ | ✅ | ⚠️ 需 broker | 中 | ❌ 否决 |
| APScheduler | ✅ | ⚠️ | ⚠️ | ❌ | ❌ | ⚠️ | ✅ | 低 | ✅ 仅用于定时任务 |
| **SQLite DB-as-Queue + 租约** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 原生 | **极低** | ✅ **采纳** |

### 1.4.2 采纳方案机制（SQL 见 §3.4）

1. **认领**：单语句原子 `UPDATE ... WHERE id=(SELECT ... ORDER BY priority ASC, created_at LIMIT 1) RETURNING *` —— 无竞态窗口。
2. **续租/心跳**：运行期每 `lease/3` 续租一次；worker 崩溃 ⇒ 租约自然过期。
3. **回收（sweeper）**：每 5s 执行「过期租约 → 回 `pending`（`not_before=now+backoff`）」与「`attempts >= max_attempts` → `dead`（死信）」。
4. **依赖解锁**：job 完成后，将依赖它的 job 的 `depends_on_json` 中该 job 移除，全部清空 ⇒ `pending`。
5. **退避**：`not_before = now + min(base * 2^(attempts-1), cap) + jitter(0..20%)`。
6. **唤醒通道**：主循环 `asyncio.sleep(poll_interval)` 轮询（默认 500ms，可靠）；可选优化：API 向 worker 的 loopback UDP 端口发 1 字节唤醒包，把平均延迟从 250ms 降到 ~1ms。**可靠性不依赖唤醒包**。
   - ✅ **T1.6 已落地**：走的是**轮询**分支 —— 每池 `pools.yaml: poll_ms` + 指数退避（封顶 2s，`lease.poll_delay_ms()`），用 `threading.Event.wait()` 而非 `asyncio.sleep`（四池 worker 是**同步进程**，T1.5 裁定 27）；**UDP 唤醒包未实现**，也不打算现在做 —— 单机空池时多等 ≤2s 不值得引入一个端口与丢包路径；真要提速先调 `poll_ms`（那是唯一真相）。
   - ✅ **心跳与续租共用一个后台线程**（T1.6 裁定 34）：`pools/worker_base.py` 的 `_Pulse`，tick 默认 1s。
7. **死信处理**：`dead` job 保留现场（`error_json` + 已产出 artifact），WebUI 可见、可一键重投。

### 1.4.3 为什么不用纯 `asyncio.Queue`（关键反驳）

| 需求 | asyncio.Queue 的问题 |
| --- | --- |
| 断点续传（P2） | 队列态在内存，进程重启后**无法知道**哪些句子已完成 |
| 可视化与审计 | 无 SQL 可查，WebUI 无法呈现"排队中/执行中/失败" |
| 崩溃自愈 | 无租约概念，卡死任务无法自动回收，必须人工介入（违反 P4） |
| 跨进程 | draft/voice/render/publish 是四个进程，内存队列不可共享 |
| 优先级与依赖 | 需自行实现全部语义，等价于重新发明 DB 队列 |

**结论**：`asyncio.Queue` 仅保留一个用途——进程内"有新活干了"的唤醒信号；**真相源永远是 `jobs` 表**。

### 1.4.4 四池默认参数（`config/pools.yaml`）

| 池 | 认领单元 `unit_type` | 默认并发 | 原文值 | 轮询 | 租约 | 退避基数 | 单单元超时 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| draft | `task` | **2** | 2 ✅ | 1s | 180s | 5s | 120s（单次 LLM） |
| voice | `sentence` | **1** ⚠️ | 3 | 0.5s | 90s | 3s | 60s |
| render | `scene` / `final` | **1** ⚠️ | 2 | 0.5s | 300s / 600s | 10s | 300s / 900s |
| **publish** | `publish` | **1** | —（新增） | 5s | 600s | 60s | 600s |

**关于 voice/render 并发（冲突 C8/C9 的显存推算）**

```
可用显存       = 8.00 GB（总） − 1.49 GB（桌面/浏览器） ≈ 6.5 GB
CosyVoice 类模型 fp16 常驻 ≈ 1.5–2.5 GB（含声学+声码器+说话人编码器）
单次推理激活峰值            ≈ 0.5–1.0 GB（随文本长度变化）
────────────────────────────────────────────────────────
安全余量充足（1 并发）：6.5 − 2.5 − 1.0 ≈ 3.0 GB  ✅
风险区（3 并发）：     6.5 − 2.5 − 3.0 ≈ 1.0 GB    ⚠️ 长文本时可能 OOM
```

> **结论**：`voice.concurrency=1` 为默认。T2.2 完成后用 `scripts/bench_tts.py --concurrency 1,2,3` 实测峰值显存，把**实测安全值**写回 `pools.yaml` 并记录到 `docs/runbook/`。

**自动降并发保护**

| 触发 | 动作 |
| --- | --- |
| 单池连续 2 次 `TTS_OOM` | 该池并发 −1（下限 1）并写 `warn` |
| 连续 5 分钟无 OOM 且 `gpu_mem_used < 60%` | 逐级恢复到配置值（每 5 分钟 +1） |
| 触发降并发 | 写 `system_logs` + WS `system.alert(code='POOL_AUTODEGRADED')`（**以 §4.5.2 的 8 值枚举为准**，本表原写的 `POOL_CONCURRENCY_REDUCED` 作废） |

**其它约束**

- **GPU 互斥**：若启用 GPU 滤镜（`overlay_cuda`/`scale_cuda`）或 Whisper 对轴，必须持有命名互斥体 `Global\studio_gpu_lock`；默认渲染走 CPU 滤镜，不与 TTS 抢显存。
- **CPU 保护**：渲染 job 显式 `-threads 5`（12 核机器），为 TTS 前后处理与 API 留核。
- **升级触发条件**：>2000 jobs/天、需多机、或需跨机 GPU 调度 ⇒ 迁移到 Redis + Arq/Redis-Streams。为降低迁移成本，队列访问统一经 `db/queue.py` 暴露的 `JobStore` 协议，业务代码不得直写 `jobs` 表。

---

## 1.5 音视频合成引擎选型与工程约束

### 1.5.1 候选方案对比

| 方案 | 滤镜图表达力 | 错误诊断 | 进度获取 | 跟随 FFmpeg 新版 | 依赖重量 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| `ffmpeg-python` | 等价于手拼字符串，无实质抽象 | 差 | 弱 | ❌ 停更于 0.2.0（2019） | 轻 | ❌ 否决 |
| PyAV（帧级 API） | 适合逐帧处理；复杂 `filter_complex` 与硬件编码参数表达笨拙 | 中 | 中 | ⚠️ 受 libav 绑定限制 | 重 | ❌ 否决（仅保留抽帧小工具） |
| MoviePy | 上层拼接便利，底层仍是 ffmpeg；性能与可控性差 | 差 | 弱 | ❌ | 中 | ❌ 否决 |
| **自研：argv + `-filter_complex_script` + `-progress pipe:1`** | **完全** | **精确**（stderr 原文入库） | 结构化（`out_time_us`/`fps`/`speed`） | ✅ 任意版本 | 无 | ✅ **采纳** |

### 1.5.2 采纳方案的工程细节

```powershell
# 调用形态（示意）：argv 数组，永不经 shell
ffmpeg -hide_banner -nostdin -loglevel error -progress pipe:1 -stats_period 0.5 `
  -i "D:\data\work\<task>\broll\clip_07.mp4" `
  -i "D:\data\work\<task>\tts\s003.wav" `
  -filter_complex_script "D:\data\tmp\graphs\scene_001.txt" `
  -map "[vout]" -map "[aout]" -r 30 -fps_mode cfr -t 7.42 `
  -c:v libx264 -preset medium -crf 21 -pix_fmt yuv420p `
  "D:\data\work\<task>\scenes\scene_001.partial.mp4"
```

| 项 | 约定 |
| --- | --- |
| 滤镜图传递 | **写文件** `-filter_complex_script`（规避 Windows 32767 字符命令行上限与转义地狱），文件路径写入 `manifest.json` 供复现 |
| 参数形式 | `list[str]` argv，禁止 `shell=True` |
| 进度 | `-progress pipe:1 -stats_period 0.5` ⇒ 解析 `out_time_us` 换算百分比；**每任务限流 2Hz** 推送 WS |
| 错误 | 保留 stderr 尾部 64KB 入 `artifacts`/日志；提取 `Invalid argument`、`Conversion failed`、`No such filter` 映射为 `error_code` |
| 取消/超时 | 超时后**杀进程树**（`taskkill /PID <pid> /T /F`），清理半成品 · **实施（2026-09-17）**：`core/media.run_command` 用阻塞 `Popen` + `timeout` 收口（一期渲染 worker 是同步的，没有 `asyncio.wait_for`），超时走 `_kill_tree()`，树杀失败退回只杀直接子进程并如实说明。**为什么不能只杀直接子进程**：`subprocess.run(timeout=)` 在超时分支里还会调一次**不带超时**的 `communicate()`，孙进程攥着管道时调用方**永远不返回**（实测 `timeout=2` 挂了 12s+，陷阱 #149） |
| 产物原子性 | 一律输出 `*.partial.mp4`，成功后 `os.replace()` 原子改名 ⇒ 崩溃不留"看起来完成"的残缺文件 |
| 并发 | 池并发 × 每进程 `-threads 5`；Windows 下用 `BELOW_NORMAL_PRIORITY_CLASS` 启动 |

### 1.5.3 音画正确性七条军规（P0 · 按 C12 修订：不做句子级同步，但下列底线不可省）

1. **音频先行**：先合成全部句子 ⇒ `ffprobe` 实测**总时长** ⇒ 成片时长 = `voice_master.wav` 实测时长 + `tail_ms`；视频侧用 `-stream_loop` / `loop` 滤镜**循环补齐**（一期单遍合成无"视频段"概念，见 C12/C13）。禁止"先定视频时长再塞音频"。
2. **恒定帧率**：所有视频输入 `-fps_mode cfr -r 30`，杜绝 VFR 漂移。
3. **统一采样参数**：音频统一 `48000 Hz`；合成期 `mono s16`，输出期 `stereo`；混流前显式 `aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=mono`。
4. **时间基复位**：每个视频输入链首必须 `trim=...` + `setpts=PTS-STARTPTS`；音频同理 `atrim` + `asetpts=PTS-STARTPTS`。**缺失此步是 90% 不同步问题的根因**。
5. **显式总时长**：收尾 `-t <total_ms/1000>`；**禁用 `-shortest`**（它会掩盖缺陷而非修复）。
6. **concat 前置规范化**（**二期场景合流适用**）：送入 concat 的 scene 必须同分辨率/同像素格式/同时基/同 SAR（`setsar=1`），否则 concat 会静默串帧。
7. **兜底与诊断**：`aresample=async=1:first_pts=0` 仅作最后保险，触发即写 `warn`；`scripts/av_sync_audit.py`（人声起点 vs 字幕起点偏差）**降为可选诊断指标**（C12：**不阻断发布**），数值仅记入 `quality_json` 供观察。

### 1.5.4 滤镜复杂度治理六条

1. **分层编译**：Layer A(scene 产物) → Layer B(scene 内组合) → Layer C(concat + 混音 + 烧字幕)。单次编译滤镜图节点数 ≤ 60。
2. **中间产物缓存**：`render_hash` 命中 ⇒ 直接复用 `scene_00N.mp4`，不重编译。
3. **标签命名规范**：`[v{scene}_s{slot}]`（源）/ `[v{scene}_l{layer}]`（合成后）/ `[a{scene}]`；禁止匿名输出导致 FFmpeg 自动编号错位。
4. **链长上限**：单条滤镜链 ≤12 个滤镜，超出拆成两个 pass。
5. **同源复用**：同一文件被多处使用必须 `split=N`（或 `asplit=N`）扇出，禁止重复 `-i`。
6. **语法预检 + golden**：编译后先 `ffmpeg -filter_complex_script x.txt -f null -` 快速失败，并有 `tests/golden/filtergraph/*.txt` 快照测试。

### 1.5.5 编码策略

| 阶段 | 编码器 | 参数 | 理由 |
| --- | --- | --- | --- |
| scene 中间层 | `h264_nvenc` | `-preset p5 -tune hq -rc vbr -cq 23 -b:v 0 -g 60 -pix_fmt yuv420p` | 速度优先；NVENC 使用独立编码单元，**不抢 CUDA 计算单元**（不干扰 TTS） |
| 成片 final | `libx264` | `-preset medium -crf 21 -profile:v high -level 4.1 -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p -colorspace bt709 -color_primaries bt709 -color_trc bt709 -movflags +faststart` | 质量与平台兼容优先；bt709 显式标记避免平台色彩偏移 |
| **720P 降级档** | `h264_nvenc` | 720×1280 + `-preset p4 -cq 26`（原文 §5.5"降为 720P 出片保底"） | final 失败时的兜底 |
| 音频 | `aac` | `-c:a aac -b:a 192k -ar 48000 -ac 2` | 平台通用 |

> **本机实测**：RTX 2080 SUPER 为 Turing，**NVENC 支持 H.264/HEVC，不支持 AV1**（`av1_nvenc` 虽在 encoder 列表中出现但硬件不可用）。编码器可用性由 `studio doctor` 实测探测（跑 1 秒 testsrc）。
---

## 1.6 本地依赖矩阵

### 1.6.1 系统与运行时

| 组件 | 目标版本 | 本机实测 | 动作 |
| --- | --- | --- | --- |
| Windows | 10/11 x64 | 11 (10.0.26200) | ✅ |
| CPU | ≥8 逻辑核 | 12 | ✅ `-threads 5` |
| RAM | ≥16 GB（推荐 32） | 未采到 | ⚠️ `doctor` 补采（`psutil`） |
| GPU | ≥8 GB, sm_70+ | RTX 2080 SUPER 8GB, **sm_75** | ✅ 但需 fp16；禁 bf16/tf32 |
| NVIDIA 驱动 | ≥535 | 610.74 | ✅ |
| CUDA Runtime | 12.1（wheel 内置） | 无需装 Toolkit | ✅ |
| FFmpeg / ffprobe | full build ≥6.1 | gyan.dev 2025-10-21 | ✅ 构建指纹写入健康接口 |
| SQLite | ≥3.35 | 3.51.1 | ✅ `RETURNING` 可用 |
| Node.js | ≥20 | v22.12.0 | ✅ |
| Python（app） | 3.12.x | 3.12.12（msys）+ uv 可管理 | ⚠️ 用 `uv venv --python 3.12` 重建，**不用 msys python** |
| Python（tts） | **3.11.x** | uv 托管 3.11.15 | ✅ 与 torch cp311 wheel 匹配 |
| uv | ≥0.5 | 0.11.17 | ✅ 设 `UV_CACHE_DIR=D:\ai_models\uv_cache` |
| 字体 | 思源黑体等 | 系统字体 584 个；项目内置为准 | ⚠️ 必须随项目内置 `assets/fonts/`（libass 依赖） |
| VC++ Runtime | 2015-2022 x64 | 通常已具备 | `doctor` 检测 ffmpeg 能否启动 |

### 1.6.2 Python 依赖矩阵

| 组 | 包 | 版本 | 用途 |
| --- | --- | --- | --- |
| **app** | fastapi | ≥0.115 | REST + WS |
| app | uvicorn[standard] | ≥0.34 | ASGI（Windows 用 asyncio） |
| app | pydantic / pydantic-settings | ≥2.8 / ≥2.6 | 契约与配置 |
| app | SQLAlchemy[asyncio] | ≥2.0.30 | ORM（CRUD） |
| app | aiosqlite | ≥0.20 | 异步驱动 |
| app | httpx | ≥0.27 | LLM / TTS 客户端 |
| app | tenacity | ≥9.0 | 重试与退避 |
| app | structlog（或 loguru） | ≥24 | 结构化日志（落 DB + 控制台） |
| app | typer + rich | ≥0.15 / ≥13 | CLI 与表格输出 |
| app | PyYAML | ≥6.0 | 配置与模板 |
| app | jsonschema | ≥4.23 | LLM 结构化输出校验 |
| app | python-ulid | ≥2.7 | 有序 ID |
| app | psutil | ≥6.0 | 资源采集（doctor / 池监控） |
| app | APScheduler | ≥3.10 | GC / 备份 / **数据回收** |
| app | **playwright** | ≥1.48 | 发布执行器（Chromium 约 150 MB，**必须装到 D 盘**） |
| **tts** | torch | **2.4.0+cu121** | 推理（本机 `D:\Torch` 已预置 cp311 wheel） |
| tts | torchaudio | **2.4.0+cu121** | 音频 IO |
| tts | torchvision | 0.19.0+cu121 | 间接依赖 |
| tts | numpy | **锁定 1.26.x** | 规避 numpy 2.x 生态不兼容 |
| tts | CosyVoice（源码） | commit 锁定并在健康接口留痕 | 零样本复刻与推理 |
| tts | modelscope | ≥1.20 | 权重下载（缓存至 `D:\ai_models\modelscope_cache`） |
| tts | onnxruntime(-gpu) | 与 CosyVoice 要求一致 | 说话人/声学辅助模型 |
| tts | soundfile / librosa | 最新兼容 | 音频读写与 RMS 检测 |
| **render** | *（无 Python 依赖）* | — | 通过 `asyncio.create_subprocess_exec` 调用 FFmpeg/ffprobe；ASS 字幕自研生成 |
| **dev** | pytest / pytest-asyncio / pytest-xdist | ≥8 / ≥0.24 / ≥3 | 测试 |
| dev | respx | ≥0.21 | httpx mock（LLM/TTS 契约测试） |
| dev | ruff / mypy / pre-commit | ≥0.7 / ≥1.11 / ≥3.8 | 静态检查 |
| dev | **types-jsonschema**（+ types-PyYAML / types-psutil） | 与运行时同版本 | 第三方库 stubs（mypy strict 需要；overrides 的 `module` 必须**单行**，ini 不支持多行取值） |
| dev | freezegun | ≥1.5 | 时间相关单测 |

### 1.6.3 显式"不安装"清单（架构约束，防技术栈回退）

| 不安装 | 原因 |
| --- | --- |
| Redis / RabbitMQ / Memurai | 违反 P1（零外部服务）；队列已由 SQLite 承担 |
| **Jinja2 / Mako** | 提示词只做 `{{变量}}` 替换（`agents/prompts.py` 自研极小子集）；控制流交给 Python 组装 —— 能写单测、能类型检查，比模板里的循环可靠（裁定 57） |
| Celery / rq / Dramatiq | 同上（§1.4） |
| Docker / WSL2（默认路径） | 增加运维面；媒资跨文件系统 I/O 性能下降。WSL2 仅作为 R5 的降级逃生舱 |
| PostgreSQL / MongoDB | 单机无必要 |
| Conda 环境 | 与 uv 双 venv 方案冲突；避免 PYTHONPATH 污染 |
| `ffmpeg-python` / MoviePy | §1.5.1 已否决 |
| `pynini` / `WeTextProcessing` | Windows 无法编译安装 ⇒ 自研 `text_normalize`（T2.5） |
| 云端 TTS 客户端（Edge-TTS/Azure） | 违反 P1；仅可作为**显式可选**兜底引擎（关闭态出厂） |
| **任何验证码识别/绕过类库** | 合规底线（R13） |

### 1.6.4 权重与资源体积预算（D 盘 79.6 GB 可用）

| 资源 | 预估体积 | 落盘位置 |
| --- | --- | --- |
| CosyVoice 权重（0.5B 级 + 辅助模型） | 2–4 GB | `models/CosyVoice3-0.5B/`（或 `D:\ai_models`） |
| torch cu121 与已安装包 | ~6–8 GB | `venv-tts` |
| MC 跑酷素材库（≥60 clip / ≥30 min，1080p） | 3–8 GB | `data/assets/mc_parkour/` |
| BGM / 原声 / 字体 | 0.5–1 GB | `data/assets/`、`data/voice_src/` |
| Playwright Chromium | ~150 MB | `D:\ai_models\playwright` |
| 每任务媒资（含 GC 前） | 180–280 MB | `data/work/<task_id>/` |
| TTS 缓存上限 | 5 GB（LRU） | `data/cache/tts/` |

---

## 1.7 降级路线（Pre-decided Fallbacks）

| 失效项 | 原文要求 | 实现 | 任务状态影响 |
| --- | --- | --- | --- |
| **CosyVoice 未安装/模型缺失** | 跳过配音，其余照跑；台账记录（§3.4） | 全部句子 `tts_status='skipped'`，生成**等长静音轨**（保证时间轴与字幕可用），`quality.degraded=true`、`degrade_reason='tts_unavailable'` | 任务**继续**推进到 `queued_render` |
| **单句合成失败** | 跳过该句继续（§5.5） | 该句 `skipped` + 占位静音（时长按字数估算） | 任务继续，`quality` 打 warn |
| **无 MC 跑酷素材** | **黑屏 + 纯字幕/标题出片**（§4.2） | 背景层替换为 `color=black`，字幕/标题/贴图层照常 | 任务继续，`degraded=true` |
| **BGM 缺失** | 模板自带（§4.3） | 跳过 BGM 层，仅人声 | 任务继续 |
| **渲染失败** | **降为 720P 出片保底**（§5.5） | 720×1280 + NVENC p4 + cq26；仍失败 ⇒ 540×960 纯字幕版 | 任务继续，`degraded=true` |
| **NVENC 不可用** | — | 全部走 `libx264`（CPU +40%） | 任务继续 |
| **GPU 完全不可用** | — | TTS 走 CPU（RTF 高，仅单条验证）；渲染不受影响 | 任务继续但极慢，自动降并发 |
| **LLM 云端不可用/超预算** | — | 切本地模型；仍失败 ⇒ `failed` + `retry_from` | `llm_calls.is_local=1` 留痕 |
| **发布失败** | — | 重试 ≤3（指数退避）⇒ 仍失败转"**待人工发布**"并告警 | 任务停在 `completed`（不回退） |
| **发布登录态失效** | — | `health()` 探测 ⇒ 转 `manual_required` + 告警（**不自动登录**） | 同上 |
| **反复失败** | **进入人工池**（§5.3/§5.5） | `attempt_count ≥ 3` ⇒ `manual_pool`，不影响其他任务 | 人工池可见、可一键重投 |
| **磁盘水位低** | — | `free_D < 15 GB` ⇒ 暂停 render/publish 认领 + 触发 GC | 队列积压，不断线 |
| **前端构建不可用** | — | 零构建降级：Jinja2 + HTMX 精简控制台（任务列表/审批/日志） | 保证 P4 确认闸可用 |
| **SQLite 锁持续失败** | — | 启用单写者串行队列 + 缩短事务 + 增 `busy_timeout` | 吞吐略降 |

**降级总原则（原文 §1.2 原则4）**：**任何单点失效都不得让整条链路停下来**；降级必须留痕（`quality_json` + `system_logs`）并在 WebUI 显著提示。

---

## 1.8 发布子系统选型论证（第六部分重建）

### 1.8.1 候选方案对比

| 方案 | 可行性 | 前置条件 | 结论 |
| --- | --- | --- | --- |
| **平台开放 API** | ❌ 个人号基本不可得 | 需企业开发者资质 + 应用审核 + 权限申请 | 否决 |
| **Playwright 浏览器自动化** | ✅ 通用、无需平台授权 | 首次人工扫码建立登录态；之后复用持久化 profile | **采纳** |
| 半自动（导出成片+文案，人工上传） | ✅ 100% 可行 | 无 | **兜底**（发布失败即转此队列） |
| 第三方发布 SaaS | ⚠️ 引入外部依赖与费用 | 违反 P1 | 否决 |
| 逆向签名接口 | ⚠️ 违反平台协议、账号风险 | — | 否决（合规底线） |

### 1.8.2 多平台适配矩阵（原文未限定平台 ⇒ 按"国内主流短视频平台"分两档）

**一线平台（必做）**

| 平台 | 代号 | 比例 | 时长上限 | 标题长度 | 封面 | 话题标签 | 频次建议 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 抖音 | `douyin` | 9:16 | 15 min（本规格自限 180s） | 55 | 必填 | ✅ | ≤3/天 |
| 快手 | `kuaishou` | 9:16 | 10 min | 60 | 可选 | ✅ | ≤3/天 |
| 视频号 | `shipinhao` | 9:16 | 30 min | 短标题 | 可选 | 话题弱 | ≤3/天 |

**二线平台（保留接口与 profile，一期可延后）**

| 平台 | 代号 | 差异要点 |
| --- | --- | --- |
| 小红书 | `xiaohongshu` | 3:4 或 9:16；标题 ≤20；强话题标签；笔记形态 |
| B站 | `bilibili` | 支持横竖屏；标题 ≤80；需分区与标签；有审核期 |
| 西瓜视频 | `xigua` | 9:16 或 16:9；时长宽松；标题 ≤30 |
| 微博 | `weibo` | 9:16；强话题；可带正文 |

**输出 profile 差异化（`config/outputs.yaml`）**

| profile | 画布 | 编码 | 适用 |
| --- | --- | --- | --- |
| `douyin_1080x1920_30fps_v1` | 1080×1920 | libx264 CRF21 + faststart + bt709 | 抖音/快手/视频号（默认） |
| `xhs_1080x1440_30fps_v1` | 1080×1440 | 同上 | 小红书 |
| `bili_1920x1080_30fps_v1` | 1920×1080 | 同上 | B站（横屏版） |
| `fallback_720x1280_v1` | 720×1280 | NVENC p4 cq26 | 降级保底 |

### 1.8.3 Playwright 方案工程约束

| 约束 | 说明 |
| --- | --- |
| 登录态 | 持久化 `user_data_dir`（`data/browser_profile/`）；首次需**人工扫码一次**（一次性运维动作，不违反 P4） |
| 登录态失效检测 | 发布前 `health()` 探测"是否已登录"；失效则**告警并转人工**，不尝试自动登录 |
| 限频 | 默认 **≤3 条/天/账号**，两条间隔 ≥30 分钟（R13） |
| **不实现** | 验证码识别/绕过、批量养号、模拟真人行为对抗风控 |
| 选择器维护 | 集中在 `src/studio/publish/selectors/<platform>.yaml`，页面改版可热修（不改代码） |
| 失败留痕 | 截图 + DOM 快照 + 平台返回文案落 `artifacts` |
| 发布产物 | `publications` 表记录：平台、账号、URL、发布时间、状态、封面路径 |
| 幂等 | `idempotency_key = sha256(task_id\|platform\|account_id)`，防重复发布 |
| 演练模式 | `studio publish dry-run`：走完流程到"确认发布"前一步并截图，**不真正发布**（T5.2 验收项） |