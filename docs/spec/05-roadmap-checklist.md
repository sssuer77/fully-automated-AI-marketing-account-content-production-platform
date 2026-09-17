# §05 落地任务拆解清单（五阶段 · 49 个原子任务）

> 验收命令中 `studio` = `uv run studio`（即 `src/studio/cli.py`）。测试标记：`gpu` / `slow` / `e2e` / `contract` / `net`（需网络）。
> 每个任务都满足 §README.8 的通用 DoD（契约先行 / 幂等 / 可观测 / 可降级 / 无静默失败 / 可冷启动 / 超时硬约束 / 零人工）。
> 阶段划分依据原文 §9.2 的四阶段，**T5 为第六部分（原文缺失）重建后新增**（原文 §8/§9.3 明确要求发布与数据回收，但 §9.2 未列阶段）。

---

## 5.0 阶段总览与关键路径

| 阶段 | 主题 | 任务数 | 原文对应 | 里程碑门禁 |
| --- | --- | --- | --- | --- |
| **T1** | 基座 + 智能体脚手架 + 选题池 + WebUI 骨架 | 12 | 阶段一 | **M1** |
| **T2** | CosyVoice3 配音（熊大熊二 + 按句续传） | 9 | 阶段二 | **M2** |
| **T3** | 渲染引擎（**一期：单遍合成 + 固定水印**） | 7 | 阶段三 | **M3** |
| **T4** | 四池并行 + 网页实时操作台 + 无人值守 | 14 | 阶段四 | **M4** |
| **T5** | 发布 + **定时任务** + **数据报告** + 数据回流（第六部分重建 + 口述 D7/D9） | 8 | §8 / §9.3 | **M5** |
| | **合计** | **50** | | |

**关键路径**

```
     → T3.1 → T3.2 → T3.3 → T3.4 → T3.7 ─(M3)          ← ★ 一期只到单遍合成；三层模板属二期
     → T4.11 → T4.12 ─(M4) → T5.1 → T5.2 → T5.3 → T5.6 → T5.4 → T5.7 → T5.5 → T5.8 ─(M5)
```

**必须先启动的外部依赖（有 lead time，越早越好）**

| 项 | 阻塞 | 要求 | 当前状态 |
| --- | --- | --- | --- |
| 🔴 **MC 跑酷素材（★由你提供，D3）** | T3.1 | 放入 `data/assets/mc_parkour/`，建议 ≥60 条 / ≥30 分钟 | **`D:\MC` 为空** |
| 🔴 **BGM 音乐库** | T3.1 | ≥20 首授权曲（可选，缺失则静音降级） | **`D:\MUSIC` 为空** |
| 🔴 **熊大熊二原声录制** | T2.4 | 各 2–3 段，10–30s/段，**无背景音乐** | 占位已就位（2026-09-17 · 开箱即用）· **正式原声仍缺** |
| 🔴 **水印 PNG** | T3.2 | `templates/<tid>/assets/images/watermark.png`（**必做**，D5） | 缺失 |
| ✅ ~~CosyVoice 权重下载~~ | T2.1 | 2–4 GB，落 `models/` 或 `D:\ai_models` | **已就位（2026-09-17）**：21 文件 / 5.23 GB · revision `074ca6dc` 锁定（Q7 已核验）· 见 `docs/runbook/tts_models.md` |
| 🟡 **LLM API Key** | T1.9 | [OI] 兼容接口 | **面板可配（2026-09-17 · T6.1）**：WebUI「设置」面板填入即生效（`config/secrets.yaml`，env 优先）；未填时 `studio llm probe` 报 `no_key`，**不阻塞** |
| 🟡 **`config/persona.yaml` 填写** | T1.9 | 人设/口吻/受众/口癖/禁区 | 缺失（**唯一人工必填**） |
| ✅ 前端脚手架与设计系统 | T4.1 | 可与 T2/T3 并行 | **已交付（2026-09-14）**：无外部依赖，先于 T2/T3 完成 |

> **素材/权重缺失不阻塞开发**：T3.1/T3.7 提供黑屏降级（§04.2.8.6），T2.8 提供"字幕模式"降级（§04.3.3）——链路始终可跑通，素材到位后自动提升画质。

**第一周建议顺序**：`T1.1 → T1.3 → T1.5`（立骨架）→ `T1.2 → T1.4`（配置与 16 态状态机）→ `T1.6 → T1.7`（Worker 与日志/WS）→ `T1.8 ✅`（LLM 网关）→ `T1.9 → T1.10 → T1.11`（Agent 链条与确认闸）→ `T1.12 ✅`（一键启动）。

---

## 5.1 阶段 T1 · 基座 + 智能体脚手架 + 选题池（12 任务）

| ID | 模块与目标 | 依赖 | 验收标准 / 验证命令 | 风险点与降级预案 |
| --- | --- | --- | --- | --- |
| **T1.1** | **仓库骨架 + 双 venv + `doctor`**：目录树（§02.2）、uv 建 app(3.12)/tts(3.11)、**环境变量重定向**（§README.6） | — | `studio doctor --json` → `ok=true`（FFmpeg/NVENC/字体/磁盘/DB/环境变量逐项）；`uv run python -V` = 3.12.x；`uv run --project tts python -c "import torch;print(torch.__version__,torch.cuda.is_available())"` = `2.4.0+cu121 True` | **R1 磁盘**：C 盘仅 1.80 GB ⇒ `TEMP`/`HF_HOME`/`UV_CACHE_DIR`/`PIP_CACHE_DIR`/`PLAYWRIGHT_BROWSERS_PATH` 全量重定向 D 盘；`doctor` 断言 `free_C ≥ 1GB`、`free_D ≥ 15GB`，否则**拒绝启动** |
| **T1.2** | **配置系统**：`persona.yaml`/`llm.yaml`/`app.yaml`/`pools.yaml`/`outputs.yaml`/`randomization.yaml`/`publish.yaml` + 环境变量覆盖 + 启动校验 | T1.1 | `studio config dump --json` 与 `config/*.yaml` 一致；`pytest tests/unit/core/test_config.py -q`（缺字段 / 非法值 / 未知键 / 越界路径 / **未设密码但开启局域网** 五类用例） | 密钥泄漏 ⇒ `llm.yaml` 只存 `api_key_env` 名，密钥走环境变量且入 `.gitignore`；persona 缺字段 ⇒ **启动即报错**并打印模板路径 |
| **T1.3** | **数据库与迁移框架**：**29 表** DDL（§03.3）、迁移器、checksum 校验、`db check` | T1.2 | `studio db migrate` → `sqlite3 data/studio.db "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"` = **29**；`studio db check` 断言 `journal_mode=wal` / `foreign_keys=1` / `integrity_check=ok` / `foreign_key_check` 为空；**连续执行两次结果一致**（幂等）；`pytest tests/integration/test_migrations.py -q` | **R10 锁竞争** ⇒ WAL + `busy_timeout=5000` + 短事务（<20ms，禁事务内 I/O）；历史迁移 checksum 变化 ⇒ 拒绝启动；**改 CHECK 需走四步重建**（§03.7.3，已验证） |
| **T1.4** | **领域模型与 16 态状态机**：`TaskStatus`(16)/`Grade`/`PoolName`(4)/`PublishStatus` + `ALLOWED_TRANSITIONS` + `TaskService.transition()` 单入口 + `version` 乐观锁 + `task_events` 审计 | T1.3 | `pytest tests/unit/domain/test_state_machine.py -q`（**16×16 全矩阵**参数化：合法迁移通过、非法抛 `IllegalTransition`）；`pytest tests/contract/test_no_direct_status_write.py`（静态校验：仓储层外无 `UPDATE tasks SET status`） | 状态旁路 ⇒ 单入口 + 静态检查 + 触发器兜底；并发写 ⇒ `version` 乐观锁 + `ConcurrentModification` 重试 ≤3 |
| **T1.5** | **队列内核（四池）**：`JobStore`（§03.4）——单语句原子认领、租约、续租、sweeper 回收 + 退避、依赖解锁、死信、幂等入队、`publish` 限频守卫 | T1.3 | `pytest tests/integration/test_queue_lease.py -q`：①优先级认领顺序正确；②租约未过期不回收 / 过期回收 + 退避；③依赖 2 级解锁；④`attempts ≥ max` ⇒ `dead`；⑤幂等入队不重复；⑥`CHECK` 拒绝非法状态；`pytest tests/contract/test_no_direct_job_write.py` | 重复执行 ⇒ 单语句认领 + 租约（陷阱 #2）；空转吃 CPU ⇒ 指数退避休眠（200ms→2s），**禁 busy-loop**；死信无告警 ⇒ 强制 `system.alert(JOB_DEAD)` |
| **T1.6** | **Worker 框架（四池）**：认领循环、心跳上报（5s）、优雅退出（`draining`）、信号处理（Ctrl+C）、崩溃恢复 | T1.5 | `pytest tests/integration/test_worker_lifecycle.py -q`（**37 例**）：①`SIGINT` ⇒ 跑完当前单元后退出（不丢进度）；②心跳超 15s ⇒ 标记 `dead` ⇒ supervisor 重启；③重启后租约被 sweeper 回收并重新认领；加 `pytest tests/unit/pools/test_heartbeat.py -q`（**36 例**）+ `pytest tests/contract/test_no_direct_heartbeat_write.py -q`（**5 例**） | 硬杀进程留"假完成" ⇒ 先写 `.partial` 再原子改名（陷阱 #9）；worker 泄漏 ⇒ RSS 持续增长告警；心跳表被越权写 ⇒ 监测误报/漏报（契约测试静态拦截）；测试里漏 `request_stop()` ⇒ 脉冲线程泄漏（陷阱 #34） |
| **T1.7** | **日志与 WS 骨架**：`system_logs` 写入契约（先落库后广播）、`/ws/ui`、**8 通道**、100ms 合并、2Hz/任务限流、环形缓冲、`since_id` 补发、`seq` 缺口检测 | T1.6 | `pytest tests/integration/test_ws_replay.py -q`（**24 例**）：①断线重连按 `since_id` 补发且不丢不重；②高频进度被合并到 ≤2Hz；③慢客户端被断开（1008 + `WS_CLIENT_SLOW`）；加 `pytest tests/unit/ws -q`（**38 例**）+ `pytest tests/contract/test_ws_protocol.py -q`（**19 例**，直接解析 §04.4 比对通道/信封/事件表/告警码/阈值） | 前端被刷爆 ⇒ 合并窗口 + 限流 + 环形缓冲（陷阱 #12）；日志断线丢失 ⇒ **先落库再广播**（陷阱 #13）；`system.alert` 永不合并/永不丢弃；mypy「同一文件两个模块名」**顺带掩盖**真实类型错误 ⇒ 修完必须重跑（陷阱 #35） |
| **T1.8** ✅ | **LLM 双通道网关**：云端 [OI] 兼容 + 本地兜底、JSON Schema 强校验、修复重试 ≤3、熔断、`llm_calls` 记账、**token 预算闸门**（R16）、提示词注册表 | T1.2 | `pytest tests/unit/agents -q`（**31 例**）：①schema 不合法 ⇒ 每通道 1+3 次后返回 `LLM_SCHEMA_INVALID`；②云端 5xx/超时 ⇒ 自动切本地兜底；③超预算 ⇒ 按 `on_exceed` 三策略处理；④每次调用写 `llm_calls`（token/成本/耗时/model/prompt_version）；加 `pytest tests/contract/test_llm_gateway.py -q`（**10 例**：`llm_calls` 唯一写入方 / `agents` 分层 / 密钥不入库 / `CallStatus`↔DDL / manifest sha256）；`studio llm probe` + `studio prompts verify` | **R6** 输出非结构化/限流/超预算 ⇒ Schema 强校验 + 修复重试 + 本地兜底 + 成本记账；**R16** 成本失控 ⇒ 任务级 token 上限 + 成本面板 + 超限切本地；单次硬超时 120s；**失败也记账**（陷阱 #37）；提示词**禁控制流**（陷阱 #38） |
| **T1.9** | **输入源解析 + Planner + Ideator + 选题池**：`hot_items`/`feedback_items` 解析（§04.1.7）、5–8 方向、每方向 4 选题、**两级去重**（R15）、选题入池 | T1.8 | `studio topics analyze` 产出 5–8 方向；`studio topics ideate` 产出 20–32 选题；`pytest tests/integration/test_topic_pool.py -q`：①热点行解析（缺字段跳过 + warn）；②反馈自由文本容错（Q4）；③归一化哈希去重；④相似度 ≥0.85 降分并标 `similar_to`；⑤4 条 Planner 规则校验（含"被吐槽"降权 +500） | **R15 选题同质化** ⇒ 两级去重 + 历史库比对；**grounding 不足**（无热点无反馈）⇒ 仅凭 persona 产出 + 打 `low_grounding` 标记；单方向失败不影响其他方向（每方向独立 job） |
| **T1.10** ✅ | **Director + Writer**：大纲（钩子 / 3–5 段含要点+画面建议+情绪 / CTA / 时长预估）→ 600–800 字口播稿 → **逐句落库**（同一事务） | T1.9 | `pytest tests/unit/agents/test_director.py -q`（段数 3–5、每段 80–350 字、Σ 600–800、时长 60–180s）；`pytest tests/unit/agents/test_writer.py -q`（字数、口癖命中 ≥2、句长 ≤28、禁区命中即 block、单人占比 ≤70%）；`pytest tests/contract/test_director_schema.py test_script_schema.py`；`pytest tests/integration/test_script_pipeline.py -q`（①700 字/20 句两表同事务落库 ②seq 连续且句长 ≤28 ③超长句切分**不重写** ④禁区 block 且不留半成品 ⑤越界重写 ≤2 取最接近 ⑥时长用**算出来的**值 ⑦重跑复用任务且只留一版 `is_active=1` ⑧LLM 超时 ⇒ `failed` + `last_healthy_status` 断点） | 字数越界 ⇒ 重写 ≤2 次，仍越界取最接近版本 + `warn`；口癖缺失 ⇒ 重写；**句长 >28 字** ⇒ 强制切分（不重写）；"有稿无句"半成品 ⇒ 同事务写入 `scripts` + `script_sentences` |
| **T1.11** ✅ | **双通道评分 + Editor + 确认闸**：规则 4 项 + LLM 六维度、`0.3×规则+0.7×LLM`、A/B/C 分级、改稿 ≤2 轮、**分级放行**、`approvals` + `audit_ops` | T1.10 | `pytest tests/integration/test_scoring.py -q`（`8.0/9.0→8.7→A`、`7.0/6.0→6.3→B`、`4.0/4.5→4.35→C` 三条基线）；`pytest tests/e2e/test_approval_flow.py -q`：①A 级自动放行并写 `approved_by='auto_approve_A'`；②B 级进 `awaiting_approval`；③reject 必填 comment 且 `revision_round+1`；④改稿 2 轮后仍 B ⇒ 进闸（**不无限循环**）；⑤人工退回后重审**不再烧改稿轮次**（`round_no` 只保下界，裁定 100）；⑥规则通道四项 + 六维度各自可复算（`pytest tests/unit/domain/test_scoring.py -q` 97 passed） | **R16** 改稿烧 token ⇒ 轮次硬上限 2；C 级误废弃 ⇒ 选题候选置 `rejected`（**可人工捞回**）；Editor 乱改 ⇒ diff 断言"只改 issues 涉及段落" |
| **T1.12** ✅ | **一键启动与关停**：`启动.bat` → doctor 门禁 → 拉起 5 进程（API/TTS/draft/voice/render）→ 健康等待 → **浏览器自动打开** → `停止.bat` 三级优雅关停（契约 §04.8） | T1.11 | `studio service start --no-browser` ⇒ `ok=true`、`ready=['api']`、`degraded=['tts','draft','voice','render']`、`elapsed_ms≈4.8s`（含 `uv run` 开销；进程内直调 ≈1.4s）、`/api/v1/health` 回 `ok=true`；`ops\status.ps1` ⇒ `api` 的 pid / 8787 / 端口占用=是；`ops\stop_all.ps1` ⇒ `stopped=['api']`、`forced=[]`、155ms；`pytest tests/unit/services/test_service_manager.py -q` ⇒ 54 passed；`tasks.ps1 check` ⇒ 1791 passed / 32 skipped | 端口占用 ⇒ 启动前探测，且与「已在运行」**分开报**；**WebUI 挂掉不影响后台任务**（原文 §7.4）⇒ 五进程各自 `Popen` + 独立日志；未就绪 ⇒ 报 `degraded` 且**不拉起空转 worker**（裁定 103 / **108：M1 的「5 进程全 ready」顺延到 M4**） |
---

## 5.2 阶段 T2 · CosyVoice3 配音（9 任务 · 原文第三部分）

| ID | 模块与目标 | 依赖 | 验收标准 / 验证命令 | 风险点与降级预案 |
| --- | --- | --- | --- | --- |
| **T2.1** | **tts venv + 模型权重就位**：Python 3.11 + torch 2.4.0+cu121（复用 `D:\Torch` 预置 wheel）+ CosyVoice 源码（**revision 锁定**）+ 权重落 `models/` 或 `D:\ai_models` | T1.1 | `uv run --project tts python -c "import torch,cosyvoice;print(torch.__version__,torch.cuda.is_available())"` = `2.4.0+cu121 True`；`python scripts/smoke_cosyvoice.py --self-test` 打印模型路径/设备/权重 revision 并成功合成 1 句；权重目录与体积记入 `docs/runbook/tts_models.md` | **Q7 版本核验**：原文写 `CosyVoice3-0.5B`，FunAudioLLM 已知发布 CosyVoice / CosyVoice2-0.5B ⇒ **以实际能下载跑通的版本为准**，revision 写入留痕；**R5**：`pynini`/`WeTextProcessing` 在 Windows 装不上 ⇒ 不 import `tn`；下载中断 ⇒ ModelScope 缓存续传 |
| **T2.2** ✅ | **常驻推理服务 + 并发实测标定**（★裁决 C8 · **已完成 2026-09-17**）：`/health` `/warmup` `/unload` `/voices` `/synth`、GPU 串行信号量、fp16 常驻、空闲 20min 卸载、429 背压 | T2.1 | `curl 127.0.0.1:8811/health` → `{ready:true,device:"cuda",model_state:"ready"}`；`python scripts/bench_tts.py --concurrency 1,2,3` 输出**各并发下的峰值显存与 RTF**，写出建议值到 `docs/runbook/tts_concurrency.md`；fp16 常驻 < 4 GB | **R4/C8 显存**：8 GB 卡且桌面占 1.49 GB ⇒ **默认并发 1**；若实测 3 并发 OOM ⇒ **正式否决原文的 3 并**并记录依据；显存不足 ⇒ 自动 `unload` 重载；服务崩溃 ⇒ supervisor 重启（T4.11）；**禁 bf16**（Turing 无原生支持） |
| **T2.3** | **引擎适配层与路由**：§04.3.2 的 `VoiceEngine` ABC 实现、多引擎路由、熔断、§04.3.3 降级决策表落地 | T2.2 | `pytest tests/unit/tts/test_router.py -q`（§04.3.3 决策表**逐条**：OOM / 超时 / 静音 / 爆音 / 引擎宕 / 连续失败熔断）；`pytest tests/contract/test_voice_engine_abc.py`（Mock / 服务 / CosyVoice 三实现均满足 ABC） | 引擎"假成功"（返回静音）⇒ `RMS < -50 dBFS` 判 `TTS_SILENT`；熔断阈值可配（默认连续 3 句）；熔断后任务**不失败**，转"字幕模式" |
| **T2.4** 🔶 | **原声入库与音色注册（`bigbear`/`littlebear`）**：目录契约、质量校验（段数/时长/无 BGM/无削波/有效语音占比）、零样本复刻注册、试听样本 | T2.2 | `python scripts/ingest_voice_src.py --voice bigbear` 校验通过并注册 ⇒ **真机已跑通（`新增 2 / 未入库 0`）**；`studio tts list` 可见 `bigbear`/`littlebear`（**该 CLI 不存在**，裁定 288）；`pytest tests/integration/test_voice_profile.py -q`（段数<2 / 时长越界 / 削波 / 采样率不足 **四类拒绝**）⇒ **13 passed（2026-09-17）**；`-k quality`（含 BGM 被标 `warn`）**不做**（三条 `warn` 级检查见 `todolist.md` T2.4 裁定 287）；规格里的 `studio tts list` **该 CLI 不存在** ⇒ 口径改为脚本退出码（裁定 288） | **R2 版权**：《熊出没》IP 音色复刻存在声音权/著作权风险 ⇒ ①音色 ID 与展现名**可配置解耦**；②支持一键替换为自录音色；③WebUI 显著合规提示；④`profile.json` 来源登记留档；参考音质量差 ⇒ 入库校验 + 试听确认 + 可重录替换 |
| **T2.5** ✅ | **文本归一化与切分**：数字/英文/多音字归一化（**幂等**）、标点→停顿映射、单句 ≤28 字切分、glossary 热更新 | T1.10 | `pytest tests/unit/tts/test_normalize.py -q`（≥40 条黄金用例：日期/百分比/英文缩写/多音字/emoji/超长句）；`pytest tests/unit/tts/test_segmenter.py -q`（每片 4–28 字且不破坏语义边界）；幂等性属性测试 `normalize(normalize(x)) == normalize(x)` | **R7 长句漂移** ⇒ 单句硬上限 + 自动切分并回写 DB；误读 ⇒ `glossary.yaml` 热更新且变更即回归；**不引入 `pynini`**（R5） |
| **T2.6** ✅ | **按句合成流水线 + 句级缓存**：sentence 单元 job、`tts_hash` 缓存命中、单句重试/降级、`version` 竞态保护 | T2.3–T2.5, T1.5 | `pytest tests/integration/test_sentence_resume.py -q`：①杀进程重启后已完成句**引擎调用次数为 0**（Mock 计数断言）；②第 3 句注入失败 ⇒ 仅该句重试成功；③编辑某句后仅该句失效重合成；④缓存命中率 ≥30%；⑤产物落 `data/output/voice/<task_id>/s001.wav` | 缓存污染 ⇒ 哈希含引擎版本/音色/文本/参数；**R4** 缓存占盘 ⇒ LRU 5 GB；句级写冲突 ⇒ `version` 校验失败即**丢弃音频重合成** |
| **T2.7** ✅ | **时长时间轴**（**已完成 2026-09-16**）：`ffprobe` 实测时长、句间停顿 + 抖动（种子派生）、`timeline.json`（§04.2.7）、批量回写 `start_ms/end_ms`、拼 `voice_master.wav` | T2.6 | `pytest tests/integration/test_timeline.py -q` ⇒ **7 passed**（单调不重叠、`total_ms = Σ句实测 + Σ停顿 + tail`；`ffprobe(voice_master)` 与 **`total_ms − tail_ms`** 偏差 ≤30ms；改一句 ⇒ 后面每句 `start_ms` **全量重算**；降级句照样占时间）。`scripts/av_sync_audit.py` 按 C12 降为**可选诊断**（不阻断发布） | **R9 音画不同步**：统一 48kHz mono s16 + `concat` filter + 显式总时长；句子重合成后**必须全量重算时间轴**（禁止增量拼接）；**不信任引擎返回的时长**，一律 ffprobe 实测 |
| **T2.8** ✅ | **配音阶段编排与降级演练**（**已完成 2026-09-16**）：voice stage 接入 orchestrator、`voicing → queued_render` 守卫（全部句 `done/skipped` + timeline 校验）、故障注入开关 | T2.7, T1.11 | **真机演练（真库 / 真 SAPI / 真 ffmpeg）**：`studio pipeline run <id> --until voicing` ⇒ 停在 `voicing`（4 句全 `pending`、作业全 `pending`、母带与 `timeline.json` **都不在盘上**）；`STUDIO_FAULT="tts_fail_sentence=3;tts_fail_times=2"` ⇒ 第 3 句 `tts_attempts=2`、其余 **0**、全 `done`、**无降级**；`STUDIO_FAULT=tts_down=1` ⇒ **全句 `skipped`（各挂满 3 次）+ 等长静音 + 任务仍推进到 `queued_render`**，`quality.degrade_reason='tts_unavailable'`（原文 §3.4）；续跑同一条 `--until completed` ⇒ `voicing → rendering → completed`，成片 1080×1920 h264 + aac 48k / 17.47s。`pytest tests/integration/test_voice_stage.py -q` ⇒ **7 passed**；`pytest tests/unit/core/test_faults.py tests/unit/tts/test_faults.py tests/unit/services/test_pipeline_service.py -q` ⇒ **56 passed**；`.\tasks.ps1 check` ⇒ **3071 passed / 32 skipped / 12 deselected** | 全池失败 ⇒ 熔断 + 任务失败但保留 `retry_from`；`skipped` 句 >20% ⇒ `quality.warn` + WebUI 高亮；**不允许因 TTS 故障阻塞产线**（原文 §1.2 原则4）。**已知取舍**：排空借的 worker 按**池**认领（不按任务过滤，裁定 233）⇒ 会顺手把别的卡在 `voicing` 的任务也念了（陷阱 #113） |
| **T2.9** ✅ | **配音服务化操作接口**（**已完成 2026-09-16** · 契约 §4.3.7）：单句重配、单句试听（HTTP Range）、任务级换音色（受影响句全部失效）+ 面板首屏两个读端点 | T2.6, T1.7 | **真机演练（真 SAPI / 真 ffmpeg / 真 REST / 隔离家目录，3 句）**：`GET /sentences` ⇒ 3 句 `done`（3978 / 4823 / 4148 ms，真 SAPI 实测）+ `total_ms=14352` + 每句带 `audio_url`；`GET /media/voice/<task>/s001.wav` ⇒ **200**（175482 字节 · `audio/wav`）、`Range: bytes=0-31` ⇒ **206**；`POST /sentences/{id}/resynth` ⇒ 200 `pending`、`job_created=false`、作业回到 `pending/attempts=0`、真 SAPI 重念 1 句后 3 句重新 `done`；`PATCH /tasks/{id}/voice_map` 不带 confirm ⇒ **409** `VOICE_MAP_CONFIRM_REQUIRED`（`affected=3`，**一个字节都没写**）、带 `confirm=true` ⇒ 200 `affected=3 / requeued=3` 且 `littlebear` 映射原样保留（PATCH 是增量）、不存在的音色 ⇒ **422** `TTS_VOICE_MISSING`；`audit_ops` 两条留痕都在。`pytest tests/integration/test_voice_api.py -q` ⇒ **15 passed**；`pytest tests/unit/services/test_voice_service.py -q` ⇒ **13 passed**；`tasks.ps1 check` ⇒ **3099 passed / 32 skipped / 12 deselected** | 换音色触发全量重配 ⇒ 明确提示「将重配 N 句」并二次确认（409 + `context.affected`）；试听与合成抢 GPU ⇒ 试听**走缓存文件，不触发新合成**；重配期间禁止渲染（守卫：`voicing` 态不允许 render 认领）。**已知取舍**：重配 / 换音色**不**重算时间轴（`completed` 没有回 `voicing` 的边，裁定 241）—— 下一轮收口全量重算，响应里 `timeline_stale` 说清（陷阱 #121） |
---

## 5.3 阶段 T3 · 渲染引擎（**一期：单遍合成 + 固定水印** · 7 任务）

> ⚠️ **v3.1 范围收敛（口述 D2/D5 · 裁决 C12/C13）**：一期渲染 = **音画合成 + 固定水印**，一次 ffmpeg 调用直出 `final.mp4`。
> **不做**句子↔镜头对齐（D2），**不做**场景中间产物、三层模板编排（C13 ⇒ 降为二期 P1）。
> 契约见 §04.2.8；二期任务见本节末「二期（P1）预留」。

| ID | 模块与目标 | 依赖 | 验收标准 / 验证命令 | 风险点与降级预案 |
| --- | --- | --- | --- | --- |
| **T3.1** 🔶 | **素材入库（跑酷 + BGM）**：`data/assets/mc_parkour/parkour_*.mp4` 通配命名、缩略图、指纹（sha256 + pHash）、可用区间、**授权登记**；**BGM 库**（支持经 WebUI 素材库导入 · Q12）；`studio assets ingest` | T1.3（**素材由用户提供**，D3） | ✅ **已落地**：扫盘 / sha256 / 时长 / 缩略图 / 响度 / 入库主干（`services/asset_service.py`）、`license` 枚举强制（缺 ⇒ `ASSET_INVALID`，**在任何写入之前**拒绝）、sha256 重复拒绝（`IngestAction.DUPLICATE`）、BGM 入库（`bgm_tracks` + `loudness_lufs`/`mood`/`loopable`）—— `pytest tests/unit/services/test_asset_service.py -q` ⇒ **51 passed**、`pytest tests/unit/render/test_assets.py -q` ⇒ **19 passed**（2026-09-17 补正后的计数；新增的 17 例钉的是"停用要生效"与"盘/库两个真相源"）。❌ **一期未做**（2026-09-17 核对 · **前两项用户已裁定为非核心**）：pHash + 帧哈希（列与 DDL 在，**无代码写**）、黑帧段落排除、`studio assets ingest` / `studio assets stats` CLI（`cli.py` 里没有 `assets_app`，入库走 T4.8 的 REST 面板）、`pytest tests/integration/test_broll_ingest.py -q`（**文件不存在**） | **R3 版权** ⇒ 只收自录/授权（`license` 枚举强制 + `proof_path`）；🔴 **素材未到位** ⇒ 走**黑屏降级**（§04.2.8.6）保证链路不断；建议 T1 期间即把素材放入目录 |
| **T3.2** ✅ | **水印资产与合成 profile**：`watermark.png` 入库与校验、位置/边距/宽度/透明度参数、`config/outputs.yaml` 合成 profile（1080×1920/30fps/libx264 CRF21/faststart/bt709）+ **720P 保底档** | T1.2, T1.3 | `studio render profile --show` 打印合成 profile 并说明**这次贴不贴水印**；`pytest tests/unit/render/test_watermark.py -q`（位置枚举/边距偶数校验/宽度上限 1/4 画布/透明度范围） · ✅ **2026-09-17 核对完成**：`test_watermark.py` ⇒ **95 passed**、`test_profiles.py` ⇒ **21 passed**；`config/outputs.yaml` 四档齐（`douyin_1080x1920_30fps_v1` / `xhs_1080x1440_30fps_v1` / `bili_1920x1080_30fps_v1` / 保底 `fallback_720x1280_v1`） | **水印是可选装饰** ⇒ 缺失 / 不可用 / 放不下都只是**跳过**（`skipped_reason` 留痕），不阻塞出片；宽度过大 ⇒ 夹取到画布 1/4 并 `warn` |
| **T3.3** 🔶 | **`CompositePlan` 与单遍编译器**：§04.2.8.2 数据结构、`total_ms` 由 ffprobe 实测、`filter_complex` 生成（跑酷循环裁长 → 缩放铺满 → 水印 overlay → 混音）、路径转义、语法预检、节点守卫 | T3.1, T3.2, T2.7 | ✅ **已落地**：`src/studio/render/composite.py`（数据结构**实际名为 `CompositeRequest`** —— 规格里的 `CompositePlan` 只出现在 `watermark.py` 注释里，**不为对名字改名**）+ `hashing.py`；`pytest tests/unit/render/test_composite.py -q` ⇒ **23 passed**（①–④ 全在）、`pytest tests/unit/render/test_hashing.py -q` ⇒ **15 passed**。❌ **一期未做**（2026-09-17 核对）：`studio render plan --task <id> --out plan.json`（`render_app` 只有 `profile` / `make`）；**语法预检**（`ffmpeg -filter_complex_script … -f null -`，全仓无实现）；**节点守卫 / 分块降级**（**刻意不做**：一期单底片滤镜图实测十来二十个节点、离 60 差得远，理由见 `src/studio/render/degrade.py` 模块注释；`COMPOSITE_CHUNKED` 只是占位常量）；`pytest tests/golden/test_filtergraph.py -q`（**文件不存在** —— 中文/空格/盘符冒号转义由 `test_composite.py` 直接断言 `filter_path_arg()` 含 `\:` 覆盖）；`-filter_complex_script`（实际用内联 `-filter_complex`，单底片离命令行长度上限很远） | **R8 复杂度爆炸** ⇒ ~~节点守卫 + 分块降级~~ **一期不做**（见左）；路径报错 ⇒ 统一 `filter_path_arg()`（陷阱 #6）；命令行超长 ⇒ 一期不需要（陷阱 #7） |
| **T3.4** 🔶 | **单遍合成执行器**：argv 数组（**不用 shell**）、`-progress pipe:1` 进度解析、超时/取消（杀进程树）、`.partial` 原子改名、stderr 截断留痕、`composite_hash` 整片缓存 | T3.3 | ✅ **已落地**：argv 数组（`build_composite_argv()` → `run_command(argv)`）、`.partial` + 原子改名（`composite.py:358`）、stderr 尾部截断（`CommandResult.tail()`）+ 错误码映射、`composite_hash` 计算（写入 `manifest.json` / `quality_json.plan_hash`）、**超时杀进程树**（`run_command()` 改 `Popen` + `_kill_tree()`：Windows `taskkill /PID <pid> /T /F`、POSIX `killpg`；树杀失败退回只杀直接子进程并**如实说明**；见陷阱 #149）、**整片级缓存**（`render/cache.py` + `produce_video` 合成前比指纹：同哈希 ⇒ 复用盘上那一支，一次 ffmpeg 都不起；`--force-render` 可强制重渲）—— `pytest tests/unit/render/test_degrade.py -q` ⇒ **8 passed**、`tests/unit/render/test_cache.py` ⇒ **35 passed**、`tests/unit/services/test_render_cache.py` ⇒ **8 passed**、`tests/unit/core/test_media.py` ⇒ **53 passed**（含真两级进程树那条）；**真机**：同命令连跑两次 **6.8s → 1.96s**、manifest `reused=true` 且 `rendered_at` 早于 `created_at`；`--force-render` ⇒ `reused=false` 而哈希不变；换文案 ⇒ 哈希变 ⇒ 重渲。❌ **一期未做**（2026-09-17 核对）：`-progress pipe:1` 进度解析与 2Hz 限流（argv 用 `-nostats`，面板进度是粗粒度的）、`BELOW_NORMAL_PRIORITY_CLASS`（`media.py:126` 用 `CREATE_NO_WINDOW`）；`pytest tests/integration/test_composite_runner.py -q`（**文件不存在**，其 ⑤ 由 `test_render_cache.py` 以 Mock 计数覆盖） | 崩溃留「假完成」⇒ `.partial` + 原子改名（陷阱 #9）；僵尸 ffmpeg ⇒ **杀进程树已做**（陷阱 #149）；**缓存复用旧产物** ⇒ 哈希含 canonical plan + 输入 sha256（陷阱 #8，**已接**）；**命中面**：底片随机挑 ⇒ 不带 `seed` 的重跑通常不命中（设计如此）；NVENC 失败 ⇒ 回退 `libx264` |
| **T3.5** ✅ | **字幕生成（可选，默认开启 · Q11）**：ASS 生成（自动换行 / 每行限字 / 描边 / 居中）、说话人样式、中文断行、字体校验、安全区；开关 `render.burn_subtitle` | T3.3 | `pytest tests/golden/test_ass.py -q`（golden 比对 ASS 文本）；`pytest tests/unit/render/test_subtitle.py -q`：①每行 ≤13 字、≤2 行；②**不在数字/英文单词中间断行**；③`MarginV ≥ safe_area.bottom`；④字体缺失 ⇒ 报错而非豆腐块；⑤UTF-8 无 BOM + LF；开关关闭 ⇒ 产物无字幕层（断言） | 字幕豆腐块 ⇒ 内置字体 + `fontsdir` + 启动校验（陷阱 #5）；时间错位 ⇒ 时间**只**取自 `voice_master` 实测的句级时长；**关掉字幕必须仍能出片**（可选性验证） · ✅ **2026-09-15 实测**：`tests/golden/test_ass.py` + `tests/unit/render/test_subtitle.py`（23 例）全绿；端到端 12.0s / 1080×1920 真出片，字幕烧进画面（2 行、落在安全区内）；**字体缺失 ⇒ 跳过字幕层但仍出片** |
| **T3.6** ✅ | **混音与响度**：人声直通 + BGM 侧链 ducking + 两遍 `loudnorm` + 限幅兜底；BGM 缺失 ⇒ 单轨静音降级 | T3.3 | `python scripts/audio_qc.py --in voice_master.wav --mix final.mp4` 输出 `lufs ∈ [-16.5,-15.5]`、`true_peak ≤ -1.0`；`pytest tests/integration/test_mixdown.py -q`：①`amix` 含 `normalize=0`（静态断言）；②`loudnorm` 两遍（第二遍带 `measured_*`）；③`alimiter` 存在；④BGM 缺失 ⇒ 单轨人声且不报错 | 人声偏小 ⇒ `normalize=0` + 两遍 loudnorm（陷阱 #4）；削波 ⇒ `alimiter`（`level=0` + `limit` 由 `true_peak_dbtp` 反推 —— 规格原值 `0.95` 比门禁还宽，等于没兜住，陷阱 #97）；BGM 压住人声 ⇒ ducking 参数（threshold 0.05 / ratio 8 / attack 20 / release 420） · ✅ **2026-09-15 实测**：`tests/integration/test_mixdown.py`（10 例）全绿；真跑 ffmpeg 量得 **−16.48 LUFS / −1.12 dBTP**（GUI 一键出片实测），**两侧门禁都在内**；离下界 `−16.5` 只剩 0.02 dB 是**素材动态决定**的：口播的峰值因数约 19 dB，而 `I=−16` 配 `TP=−1.3` 只允许 14.7 dB，`loudnorm` 为保证不越真峰值门禁主动少抬了 ~0.5 LU（实测：去掉限幅器后 `loudnorm` 自己就落在 −16.3 / −1.0） |
| **T3.7** ✅ | **成片交付、manifest 与降级链**：`final.mp4` 落盘 + `manifest.json`（含 `CompositePlan` + `composite_hash`）+ `quality_json` 回填；**720P 保底** + 黑屏降级 | T3.4–T3.6 | `studio pipeline run <task_id> --until completed` 产出 `final/final.mp4`；`pytest tests/integration/test_render_pipeline.py -m "e2e and slow" -q`（7 例）；`python scripts/audio_qc.py --task <id>` 通过；**注入素材为空 ⇒ 黑屏出片**；**注入编码失败 ⇒ 720P 保底出片**；`manifest.json` 含水印标记与随机化留痕 · ✅ **2026-09-16 实测**：7 例 e2e 全绿（含新增的黑屏与 720P 注入两条），成片实测 **−16.48 LUFS / −1.12 dBTP**（两侧门禁都在内） | **R9 已按 C12 降级**：仅保留 CFR + 显式 `-t` + 禁 `-shortest`（陷阱 #3）；**水印缺失 ⇒ 跳过水印层**（不是拒绝出片，T3.7 改）；渲染反复失败 ⇒ 720P → 仍失败 ⇒ 原错误抛出，接重试链 / `manual_pool` |

**一期交付判定（M3 门禁）**：换稿不重剪 —— **同素材、同水印、换稿件** ⇒ 直接出新片；网页一键触发并在线预览。

### 5.3.1 二期（P1）预留任务 · 三层模板场景编排（**不阻塞 M3**）

> 保留原文 §4 的三层模板设计（Video→Scene→Component）。**一期不实现**，二期按需启动。契约见 §04.2.1–§04.2.7、§03.6。

| ID | 模块与目标 | 依赖 | 说明 |
| --- | --- | --- | --- |
| **T3-P1** | 三层模板契约与加载器（YAML → Pydantic → DB；组件六类；八类校验） | T3.7 | §03.6 / §04.2.0 |
| **T3-P2** | IR 与构建器（`VideoIR` + bind pass + `$` 变量绑定） | T3-P1 | §04.2.1 |
| **T3-P3** | 自动填充 + `repeat_last` 场景扩展（克隆场景强制重抽素材、禁绝对时间） | T3-P2 | §04.2.2 / §04.2.3 |
| **T3-P4** | 场景级 filtergraph 编译 + 场景中间产物缓存 + 场景级重试 + 合流 | T3-P3 | §04.2.5；ADR-004 |

> **二期启动条件**（三者同时满足）：①一期稳定出片 ≥100 条；②确有"片头/片尾/多段镜头编排"需求；③磁盘与时间预算允许（§README.7 二期占用约翻倍）。

## 5.4 阶段 T4 · 网页实时操作台 + 四池并行 + 无人值守（14 任务 · 原文第七部分）

| ID | 模块与目标 | 依赖 | 验收标准 / 验证命令 | 风险点与降级预案 |
| --- | --- | --- | --- | --- |
| **T4.1** ✅ | **前端脚手架与设计系统**：Vite + Vue3 + TS + Pinia、深色控制台布局、API 类型自动生成（OpenAPI）、WS 客户端（重连/补发/`seq` 缺口检测） | T1.7 | ✅ **2026-09-14 实测**：`npm run typecheck` 零错误；`npm run test` **29 passed**（4 文件）；`npm run build` 成功且 dist **0.09 MB < 3 MB**；`npm run size` OK；`pytest tests/contract/test_web_contracts.py` **7 passed**；`tasks.ps1 check` **1798 passed / 32 skipped** | 类型漂移 ⇒ 生成物入库 + 契约测试逐字比对 + 静态扫描 `fetch(`（陷阱 #58/#59）；WS 重连风暴 ⇒ 指数退避（1s→2s→4s→8s→**上限 15s**，永不放弃）；重连后 `seq` 基线未重置 ⇒ 每重连白跑一轮 `resync`（陷阱 #60） |
| **T4.2** ✅ | **① 总览台**：四池状态、队列长度、今日产量、资源占用（CPU/内存/显存/磁盘）+ **启动/暂停/一键全自动** | T4.1, T1.5 | 页面显示四池 `pending/claimed/failed/dead` 与 worker 心跳；`free_D < 15GB` 时显示 `DISK_LOW` 告警；点"一键全自动"⇒ `auto_approve_policy=GRADE_AB` 且写 `audit_ops`；暂停某池 ⇒ 该池停止认领但**在途跑完** · ✅ **2026-09-14 实测**：`pytest tests/integration/test_overview_api.py` **34 例**（四池/心跳 stale/本地日窗口/资源快照/暂停语义/一键全自动/`DISK_LOW` 去重/三个 WS 事件/启动单飞）；前端 `vitest` **153 例**（新增 overview store **20**）；`tasks.ps1 check` **1924 passed**；`web:verify` 全绿 · dist **0.17 MB** | 误触全自动 ⇒ 二次确认 + `audit_ops` 留痕 + 可一键回退 `GRADE_A`；指标采样开销 ⇒ 0.2 Hz（5s）+ 整拍丢线程池；"今日"用 UTC 日会算错一天（陷阱 #75）；在 API 进程里停自己（陷阱 #76）；采样泵跨线程 `publish()`（陷阱 #77）；首拍健康被当成"恢复"（陷阱 #78） |
| **T4.3** ✅ | **② 选题面板**（★新增）：方向卡片（含**历史批次**）、选题瀑布流、评分理由、勾选入队（**两步确认**）、导入热点（扫盘 / 网页粘贴）、触发分析 / 生成选题、人工加选题 | T1.9, T4.1 | 导入 `data/hot/*.md` ⇒ 方向卡片出现（WS `direction.batch_ready`）；触发分析 ⇒ 5–8 方向；瀑布流显示 `score` 与 `reason`；勾选 ⇒ 创建任务（`topic.selected`）；人工加选题 ⇒ 直接入库并写 `audit_ops` · ✅ **2026-09-14 实测**：`pytest tests/integration/test_topics_api.py` **15 例**（8 条路径 / 单飞 409 / 逐条部分失败 / 幂等复用 / `draft_now` 出稿 / 人工加选题留痕 / 两种热点入口 / WS 扇出）；前端 `vitest` **133 例**（新增 topics store **39**）；`tasks.ps1 check` **1890 passed**；`web:verify` 全绿 · dist **0.15 MB** | 选题过多导致勾选疲劳 ⇒ 按 `score` 降序 + 方向分组 + 默认折叠；重复勾选 ⇒ `idempotency_key` 防重复建任务；长任务被连点 / 双标签页重放 ⇒ **单飞守卫 409（不排队）**；动作刚报的错被紧随的刷新抹掉 ⇒ 动作级 / 拉取级**两条错误信道**（陷阱 #71） |
| **T4.4** ✅ | **③ 稿件面板 + 确认闸**：稿件全文、审稿评分（**双通道明细**）、修改版本对比、确认/退回/放弃、批量操作 | T1.11, T4.1 | 展示 `rule_detail` + `llm_detail` 六维度 + `issues`；版本对比可看 `v1→v2` diff；**退回必填意见**；批量通过 ⇒ 逐条写 `audit_ops`；`revision_round` 可见（原文 §2.2⑦"呈现稿件+评分+修改次数"）· ✅ **2026-09-14 实测**：`pytest tests/integration/test_{approvals,scripts}_api.py` **35 例**（六条路径 / 批量部分失败 / 捞回恢复选题 / 版本 diff）；`pytest tests/unit/domain/test_diff.py` **11 例** + `tests/unit/ws/test_hub_events.py` **11 例**；前端 `vitest` **94 例**（新增 scripts store **37**）；`tasks.ps1 check` **1875 passed**；`web:verify` 全绿 · dist **0.13 MB** | 误点放弃 ⇒ 二次确认 + 可"捞回"（`discarded → pending`）；批量操作误伤 ⇒ 显示待处理清单再确认 |
| **T4.5** ✅ | **④ 配音面板**（**已完成 2026-09-16**）：逐句进度条（已完成/进行中/失败/跳过）、换音色、重配某句、试听单句 | T2.9, T4.1 | 逐句状态实时刷新（**轮询 1s**，裁定 247/248）；重配某句 ⇒ 该句回 `pending` 并重合成；换音色 ⇒ 提示「将重配 N 句」并二次确认；试听 ⇒ 可播放单句 wav；`skipped` 句高亮 + 原因可见 · **真机演练（真 API 进程 / 真库 / 真 wav）**：`GET /sentences` 4 句全 `done` 且每句带 `audio_url`；`GET /media/.../s001.wav` ⇒ **200**（199940 字节 · `audio/wav`）、`Range` ⇒ **206**；`POST /resynth` ⇒ 200 `pending`，**真 worker 认领并念完**（作业 `succeeded` / `tts_attempts=1` / 句子回 `done`，1.5s 内）；`PATCH /voice_map` 只带改过的角色 ⇒ **409** `affected=4 / total=4`（一个字节都没写）；带连字符的任务号 ⇒ 形状过了 ⇒ 404（不再是 422）。`pytest tests/integration/test_voice_api.py -q` ⇒ **17 passed**；`web` 侧 `npm run verify` ⇒ **341 passed / 16 文件** + dist **0.30 MB**；`tasks.ps1 check` ⇒ **3100 passed / 32 skipped / 12 deselected** | 重配期间渲染抢跑 ⇒ 守卫：`voicing` 态不允许 render 认领；试听与合成抢 GPU ⇒ 试听走缓存文件，**不触发新合成**；换音色 ⇒ **先算代价再抛 409**（面板据此弹确认框）；「本机找不到」的音色要在下拉框里说出来（陷阱 #125）；提交只带改过的角色（陷阱 #123/#124） |
| **T4.6** ✅ | **⑤ 渲染面板**：**合成进度**、**出片表单**、成片列表、触发渲染、**在线播放**、协作式取消（"场景进度/换模板"随 C13 延后二期） | T3.4, T3.7, T4.1 | 进度按**轮询**推进（1s，有活才轮）—— 进度是进程内状态，塞进 WS 要动 Hub 的协议与快照注册表；成片列表可在线播放（HTTP Range）；触发渲染 ⇒ **进程内登记表 + 单工作线程**（并发 1，与 render 池同口径）—— 面板默认的任务号（`ui-…`）在 `tasks` 里没有行、而 `jobs.task_id` 有外键 ⇒ **挂不上队列**（陷阱 #101）；**水印可选**：不贴就在面板上写出原因 · ✅ **2026-09-15/16 实测**：`pytest tests/integration/test_render_api.py` 全绿；GUI 一键出片实测 **−16.48 LUFS / −1.12 dBTP**；**`render/final` 的池处理器已随 T3.7 收口落地**（池不再报 `handler_missing`） | 轮询频率 ⇒ 只在有活时轮，空闲一次都不发；在线播放占用带宽 ⇒ 本机回环，无风险；重复触发 ⇒ 面板给「再来一条」（**同任务重渲走进程内**：队列幂等键 `(task_id,'render','final','final')` 决定"一条任务只渲一次"，见陷阱 #101） |
| **T4.7** ✅ | **⑥ 合成配置面板**（**一期**：合成 profile / 水印 / 字幕样式表单编辑；**二期**：三层模板可视化编辑） | T3.2, T4.1 | **一期（必做）**：表单编辑合成 profile（分辨率/帧率/CRF/720P 保底档）、水印（位置/边距/宽度/透明度）、字幕（字号/描边/每行字数），保存前强制 `validate`（**不写 DB**：`config/outputs.yaml` 是唯一真相 · 裁定 165/166）；改动写 `audit_ops` 且 `version +1`；**二期**再启用层结构树 + 组件增删 + 实时预览 · ✅ **2026-09-14 实测**：后端 `test_yaml_lines.py` **17 例** + `test_outputs_store.py` **24 例** + `test_outputs_api.py` **24 例**（合计 **65 例**）；前端 `outputs.test.ts` **32 例**（前端合计 **230 例**）；`tasks.ps1 check` **2080 passed / 32 skipped / 1 deselected**；`web:verify` 全绿 · dist **0.22 MB** | **R17 工作量被低估** ⇒ 一期只做表单编辑，拖拽定位与三层模板树**随 C13 延后二期**；编辑破坏配置 ⇒ 保存前强制 `validate`，不通过拒绝保存；并发编辑 ⇒ `source_sha256` 比对 |
| **T4.8** ✅ | **⑦ 素材库**：跑酷素材 / 原声 / **BGM** 的列表、**导入（Q12：BGM 必须可导入）**、预览、试听、标记（启用/禁用）、授权信息、统计 | T3.1, T4.1 | 列表显示 `license`/`use_count`/`last_used_at`/`has_text`；导入（跑酷 / 原声 / **BGM**）⇒ 自动入库（指纹/缩略图/可用区间/时长）；**BGM 入库后立即可被混音随机选中**（T3.6 无需改配置 · Q12）；禁用 ⇒ 随机化不再选中；统计满足 `clips ≥ 60 且 ≥ 30min`；**R2 合规提示常驻**；**面板与出片必须看到同一个真相**（见下 2026-09-17 补正） · ✅ **2026-09-15 实测**：`pytest tests/integration/test_assets_api.py` **26 例**（六端点 / `dry_run` 预览一个字节都不写库 / 逐条部分失败 / 坏文件不中断整批 / 缺授权拒绝入库 / 重扫不覆盖人工字段 / 幂等不留痕 / `strays` 如实报）；素材库相关单测 **222 例**（`assets` 76 + `test_asset_service` 40 + `test_asset_repo` 21 + `test_media` 51 + `test_files` 8 + API 26）；迁移 `0009_assets`（`voice_profiles` + `idx_voice_enabled` ⇒ **30 表 / 61 索引 / 9 迁移**）；`scripts/seed_placeholder_assets.py` 真机跑通（60 跑酷 × 32s = **32 分钟** + 3 BGM + 2 音色，耗时 **74s**，占位标黄）；前端 `vitest` **284 例**（新增 assets store **18** + `http` PATCH **3**）；`tasks.ps1 check` **2468 passed / 32 skipped / 1 deselected**；`web:verify` 全绿 · dist **0.26 MB** · ✅ **2026-09-17 补正（接线，不是加功能）**：验收里那句「禁用 ⇒ 随机化不再选中」此前**没有落地**（`render/assets.py` 全文没有 `enabled` 这个词），且面板读库、出片读目录 ⇒ **两个真相源**（盘上 58 条一条没入库时，面板说"跑酷素材 0 条 / 当前为黑屏降级模式"，而片子里正放着跑酷）。补正三处：① `render/assets.py` 的 `pick_*` 增加 `exclude=`（**只做一次集合减法**，模块照旧不碰数据库、不做校验 —— 口径仍是"能进目录就算数"）；② `services/asset_service.disabled_assets(connection)` 把库里**被停用**的行翻成**文件名集合**，由 `render_job_service` / `pipeline_service` / `render_worker` / CLI 各自注入（`produce_video(disabled=...)` 缺省 `None` ⇒ 退回老口径，旧调用方不受影响）；③ `AssetService.library()` 合上**盘上事实**（`disk_total` / `pending` / `strays` / `root_missing` / `usable`），`degraded` 收窄成"**一条都挑不到**"（"不够多"归 `shortfall`）。前端：每节一行**三组数字**（出片能挑到 / 盘上 / 已入库）、未入库那批显式列出 + 入库入口、每行**三态**（出片会挑到 / 已停用 / 文件不在了，后者红）。**真机验证**：58 条跑酷 + 2 BGM 全部未入库时面板显示「出片能挑到 58 条」且 `degraded=false`；入库 3 条后停用 1 条 ⇒ `usable` 58→57、3000 次挑样**0 次命中**停用那条（不排除时命中 50 次）；面板提交一条真任务 ⇒ **10s 出片成功**，manifest 记的是 `parkour_050.mp4`（不是停用的 `parkour_004`），响度 −16.25 LUFS / −1.22 dBTP 在门禁内。`tasks.ps1 check` **3591 passed / 32 skipped / 27 deselected**；`web:verify` **380 passed** · dist **0.32 MB** | 素材不足 ⇒ 显著提示“当前为黑屏降级模式”；误删 ⇒ 只允许禁用（不物理删除）+ `audit_ops`；**重扫把人工标记抹掉** ⇒ 只刷机器事实（陷阱 #92）；**短片段当合格素材** ⇒ `BROLL_MIN_USABLE_MS=4500` 硬判据（陷阱 #93）；占位素材被当真 ⇒ `tags:["placeholder"]` 标黄 |
| **T4.9** ✅ | **⑧ 实时日志**：分级/任务/来源过滤、搜索、告警高亮、导出 NDJSON、断线重连不丢 | T1.7, T4.1 | ✅ **2026-09-14 实测**：`pytest tests/integration/test_logs_api.py` **19 例**（过滤组合 / LIKE 转义 / NDJSON 导出 / `until_id` 窗口 / 上限 422）；前端 `vitest` **57 例**（含过滤、告警、补洞、导出）；`tasks.ps1 check` **1818 passed**；`web:verify` 全绿 · dist **0.10 MB** | 高频日志拖慢前端 ⇒ 合并 + 环形缓冲（陷阱 #12）；`debug` 默认丢弃（不落库）；导出超大 ⇒ 服务端**流式** + 每页 500 行；**WS 有损** ⇒ 跳号时用 REST `since_id` 补洞（陷阱 #61/#62） |
| **T4.10** ✅ | **四池调度控制台**：池状态/积压/并发旋钮/暂停恢复/优先级/死信重投 + **并发自动降级** | T1.5, T4.2 | 并发旋钮 ⇒ 写 `pool_settings` + `audit_ops`，生效无需重启；暂停/恢复**不丢进度**（在途跑完）；死信重投 ⇒ `dead → pending` 且 `audit_ops`；连续 `TTS_OOM` 达 `oom_threshold`（默认 2）⇒ **自动降并发**并告警 · ✅ **2026-09-14 实测**：`pytest tests/integration/test_pools_api.py` **29 例**（配置值 vs 运行值并排 / voice 上限 3 / 幂等不重复留痕 / 下调不杀在途 / 死信逐条重投 / 连续 OOM 降级 + 告警 + 留痕 / 成功归零 / 下限仍告警 / YAML 开关生效）；迁移 `0007_pool_autodegrade`（`consecutive_oom`）；前端 `vitest` **172 例**（新增 pools store **19**）；`tasks.ps1 check` **1953 passed**；`web:verify` 全绿 · dist **0.18 MB** | 并发调太高 ⇒ 上限硬编码（≤8，voice ≤3）+ 影响提示；自动降级抖动 ⇒ 判据 `== threshold`（不是 `>=`）+ 降级后需人工确认恢复；静默降不动 ⇒ 已在下限仍告警（陷阱 #79/#80/#81） |
| **T4.11** ✅ | **无人值守编排与故障自愈**：supervisor 守护 5 进程、崩溃重启（指数退避）、磁盘/显存水位门禁、自动重试与死信告警、**人工池** | T4.10, T2.8 | 杀任一 worker ⇒ 15s 内被重启并恢复认领；`free_D < 15GB` ⇒ 暂停 render/publish 认领 + `DISK_LOW`；任务 `attempt_count ≥ 3` ⇒ `manual_pool` 并在总览台可见；**连续运行 24h 无人工干预** · ✅ **2026-09-14 实测**：后端 `test_watchdog_service.py` **27 例** + `test_watchdog_api.py` **7 例**（合计 **34 例**：判死 / 指数退避 / 上限停手 / 未就绪不硬拉 / 水位门禁 / 人工覆盖 / 并发回升 / 人工池可见 / 停用照留痕）；前端 `overview.test.ts` **25 例**（前端合计 **235 例**）；`tasks.ps1 check` **2115 passed / 32 skipped / 1 deselected**；`web:verify` 全绿 · dist **0.22 MB**；契约 §04.5.10 · ⏳ **24h 长跑与真机杀进程演练未做**（守护侧能力已就位，随 T4.12 观测面单独验收） | 重启风暴 ⇒ 指数退避 + 重启次数上限（超限停止并告警）；水位门禁误判 ⇒ 阈值可配 + 手动覆盖（写 `audit_ops`）；**人工池必须可见**，否则违背 P4 |
| **T4.12** ✅ | **观测、备份与交付**：指标面板、每日 DB 备份与**恢复演练**、媒资 GC（§03.7.5）、运维手册（6 个应急剧本 + 导航）、**操作审计页** | T4.11 | `scripts/backup_db.ps1` 产出日备；**恢复演练**：还原到临时库 → `db check` 通过 → 抽查 3 张表行数；`studio gc run` 按 TTL 清理且**不误删成片**；`docs/runbook/` 剧本齐备；审计页可按任务/操作人筛选 · ✅ **2026-09-14 实测**：`pytest tests/unit/db/test_backup.py tests/integration/test_backup_cli.py tests/unit/gc tests/integration/test_gc_cli.py tests/integration/test_audit_api.py tests/integration/test_metrics_api.py -q` ⇒ **130 例**（32+10+57+11+12+8）；`scripts/restore_db.ps1 -Force` ⇒ **exit 0**（活库基线失败项 0 / 备份版本落后 `db.indexes` / 备份自身问题 0）；`studio db check` ⇒ 8 项全 ok（29 表 / **60 索引** / 6 触发器 / 8 迁移）；前端 `vitest` **263 例**（新增 audit **15** + metrics **13**）；`tasks.ps1 check` ⇒ **2245 passed / 32 skipped / 1 deselected**；`web:verify` 全绿 · dist **0.24 MB** | **R11 媒资无限增长** ⇒ 保留策略 + GC + 水位门禁（陷阱 #15）；误删成片 ⇒ GC 白名单（`final/`、`output/`、`covers/` 永不清理）+ **唯一删除闸口** `guard_path()`；备份占盘 ⇒ 7 日备 + 4 周备，计入水位；**刚上过迁移 ⇒ 盘上最新备份必然落后**（陷阱 #89）；**新鲜度按日期零点算**会把刚跑完的日备报成「11 小时前」（陷阱 #90）；**VACUUM 要 2 倍空间 + 独占写** ⇒ 先钉检查点、失败即中止（陷阱 #91） |
| **T4.13** ✅ | **人物库面板**（persona 编辑 / 切换 / 回滚）：展示激活人物（来源 / 版本 / `sha256` / `last_error`）、人物库列表（含无效条目原因）、表单编辑（人设 / 口吻 / 受众 / 口癖 / 禁区 / 篇幅）、**一键切换**（旧版自动备份到 `data/backups/persona/`）与**另存为**、切换即广播 `system.persona_changed` | T1.2+ | 面板可改 / 可切 / 可回滚；`validate` 不通过 ⇒ **拒绝保存**；改完后续任务立即生效且**在跑任务不中断**；`pytest tests/unit/core/test_persona_store.py -q` 全绿 · ✅ **2026-09-14 实测**：后端 `test_persona_store.py` **74 例** + `test_persona_api.py` **35 例**；前端 `persona.test.ts` **26 例**（前端合计 **198 例**）；`tasks.ps1 check` **2014 passed / 32 skipped**；`web:verify` 全绿 · dist **0.20 MB** | **E7 唯一人工必填** ⇒ 常驻提示"禁区词经规则通道全量扫描"；多进程无 IPC ⇒ 以"下次 `current()` 生效"为准；T5.7 报告写回 persona **必须走 `PersonaStore`**（禁止直接改文件） |
| **T4.14** ✅ | **四屏端到端串联**（选题 → 稿件 → 配音 → 渲染）（**已完成 2026-09-16**）：选题卡「去稿件 →」、稿件详情「去配音 / 去渲染 →」、配音「去渲染 →」、渲染「去配音 →」（含最近任务每行的反向跳转） | T4.3, T4.4, T4.5, T4.6 | 一条选题走完四屏**不手抄任务号**；跳转语义在 `stores/ui.ts`（`goTo` / `takeHandoff`），目标面板**挂载时**认领；认领即清空，人自己点侧边栏走则顺手清掉 · **2026-09-16 实测**：`cd web; npm run verify` ⇒ `vue-tsc` + **355 passed / 17 文件**（新增 `ui.test.ts` **14 例**）+ 体积门禁 **0.30 MB / 3.00 MB**；`tasks.ps1 check` ⇒ **3101 passed / 32 skipped / 12 deselected** | 跳转**不带业务参数**、也不预判目标面板状态（否则四屏绑死，裁定 253）；**先拉列表、再认领跳转**（反了会被自己的首屏覆盖掉且不报错，陷阱 #128）；认领即清空（陷阱 #129）；跳到渲染面板**只填框、不自动出片** |
---

## 5.5 阶段 T5 · 发布、定时、报告与数据回流（8 任务 · 第六部分重建 + 口述 D7/D9）

> ⚠️ **本阶段为第六部分（原文缺失）重建后的新增阶段**：原文 §1.1 把"⑤ 成片与发布"列为五大子系统之一，§8 明确"自动发布→数据回收→记忆沉淀"，§9.3 把"发布/数据回收"列为**自动**项，但 §9.2 的四阶段未包含它 ⇒ 独立为 T5。
> **v3.1 扩展**：按口述 D7（定时任务）与 D9（整合报告）新增 T5.6 / T5.7；按 D1（多账号结构支持）新增 T5.8。
> **默认关闭**（`publish.enabled=false`）：先跑通 T1–T4 验证出片质量，人工确认后再开启（R14）。
> 子系统行为见 **§06**；接口签名见 **§04.6**；数据表见 **§03.3.15 / §03.3.18 / §03.3.19**。

| ID | 模块与目标 | 依赖 | 验收标准 / 验证命令 | 风险点与降级预案 |
| --- | --- | --- | --- | --- |
| **T5.1** ✅ | **快速封面 + 发布前二次校验**（§06.3 / §06.4）：ffmpeg 抽帧 + `drawtext` 标题 → 封面；发布前重跑**门禁**（水印 / 响度 / 相似度）+ 标题/文案禁区词扫描 | T3.7 | `studio publish cover --task <id>` → `data/output/covers/{时间戳}_{task_id}_cover.jpg`（1080×1920，标题清晰可读）；`pytest tests/unit/publish/test_cover.py -q`（文字溢出自动缩放、抽帧失败降级纯色底）；**发布前校验**：**水印缺失 / 响度越界 / 命中禁区** ⇒ **拒绝发布并转 `manual_required`**（不静默放行） · ✅ **2026-09-16 实测**：封面真机产出（1080×1920，中文主/次文案 + 描边 + 压暗带，抽帧点 500ms）；`publish precheck` 退出码 **1**、`error_code=PRECHECK_WATERMARK`（本机无水印 PNG + 成片 −16.69 LUFS 超窗，**门禁真的拦住了**）；`pytest tests/unit/{domain,publish,agents,services} tests/contract -q` ⇒ **175 例新增**；`.\tasks.ps1 check` ⇒ **3276 passed / 32 skipped / 12 deselected**；分层收尾：`system_font_dirs` 下沉 `src/studio/core/fonts.py`（修掉 `publish → render` 违规，裁定 261） | **R14 不可逆** ⇒ 发布前二次校验 + `publish.require_confirm=true`；封面文字溢出 ⇒ 自动缩放（最小 60pt）+ 换行 + 边界校验；标题超长（小红书 ≤20 字）⇒ 按 profile 裁剪 + `warn`；**C12**：`av_sync` 仅诊断，**不**作为门禁。**本任务不发布**（`publish.enabled=false`） |
| **T5.2** ✅ | **发布平台适配层与 profile**（§06.2 / §06.5 · **A1 多平台** · **已完成 2026-09-16**）：`Publisher` ABC 实现（**一线**：抖音/快手/视频号；**二线**仅保留接口）、持久化登录态、登录态探测、上传+填标题/文案/话题+选封面、**选择器集中化** | T5.1 | `pytest tests/contract/test_publisher_abc.py -q`（Mock 实现可替换）；**`studio publish dry-run --task <id> --platform douyin`** 走完流程到"确认发布"前一步并截图，**不真正发布**；`health()` 正确报告"未登录"/"登录态已过期"；`pytest tests/integration/test_publish_dryrun.py -m "e2e and slow" -q`；**标题回读比对**（§06.5.3 第⑤步）通过。**真机读数**：`--target fixture` 退出码 0 且靶页服务器**一次都没收到 POST /__published**；`--probe "?logged_out=1"` / `&expired=1` 分别报「尚未登录」/「登录态已过期」；`--platform douyin`（live）真打开创作页并如实报未登录。`pytest tests/integration/test_publish_dryrun.py -q` ⇒ **15 passed**；`.\tasks.ps1 check` ⇒ **3471 passed** | **R13 风控** ⇒ 限频 + 登录态失效即转人工 + **不实现验证码绕过/不自动登录**（合规底线）；页面改版导致选择器失效 ⇒ `publish/selectors/<platform>.yaml` 集中配置便于热修 + 失败截图取证 + `selectors_version` 留痕；**R18 范围膨胀** ⇒ 一期只做一线三平台，二线仅留接口与 profile（Q9 默认） |
| **T5.3** ✅ | **发布池 worker + 限频 + 失败转人工**（§06.5.4 / §06.10 · **已完成 2026-09-17**）：`publish` 池、≤3 条/天/账号、间隔 ≥30min、重试 ≤3 指数退避、失败转"待人工发布" | T5.2, T1.5 | `pytest tests/integration/test_publish_pool.py -q` ⇒ **4 passed**：①第 4 条当天被限频**自动顺延**（`units_deferred=1` / `units_failed=0`，作业回 `pending`、`attempts=0`、`not_before` 在未来，**一条记录都不落**）；②连续失败 3 次 ⇒ `publications.status='manual_required'` + `attempt_count=3` 且**任务仍为 `completed`**（不回退）+ `audit_ops` 恰好一条 `publish.manual_required`；③幂等键 `sha256(task_id\|platform\|account_id)` 防重复发布（投两次 + 跑两轮 ⇒ 发布器**只被叫过一次**；**已真机验证唯一键生效**）；④`GET /api/v1/publish/queue` 可查待人工列表（带 `error_code` 与三个 `can_*`）。另有 `pytest tests/unit/{pools/test_publish_worker,db/test_publication_repo,publish/test_ratelimit}.py -q` ⇒ **39 passed** 与 `test_queue_lease.py` 的 **4 例** `defer` 用例；`.\tasks.ps1 check` ⇒ **3518 passed / 32 skipped / 27 deselected**（+47 例） | 发布失败不应让成片不可用 ⇒ 任务停 `completed`，发布单独重试；**账号风险** ⇒ 限频 + 告警 + 可一键关闭发布（`publish.enabled=false` 立即生效，真发布一律 `PUBLISH_DISABLED` 转人工）；多账号 ⇒ 限频**按账号独立计数**；**顺延不是失败** ⇒ `JobStore.defer` 把认领时加的那次 `attempts` 退回去（否则三天后一条没跑过的作业自己进死信） |
| **T5.4** | **数据回收 + 记忆沉淀闭环**（§06.6 / §06.8）：T+1h/6h/24h/72h 采集播放/点赞/评论/分享；高互动评论入 `feedback_items`；低互动选题降权（≤20%）；汇总进 `data/feedback/auto_*.md` 供下轮 Planner 消费 | T5.3 | `pytest tests/integration/test_metrics_recycle.py -q`（定时触发、`metrics_json` 与 `metrics_history_json` 正确落库）；手工：发布后 T+1h 能在发布面板看到数据；`data/feedback/auto_YYYYMM.md` 生成且**能被 T1.9 的解析器直接消费**（闭环验证：下一轮 Planner 的 `grounded_on` 含 `{"type":"feedback","source":"auto"}`） | 平台数据接口变动 ⇒ 抓取失败记 `warn` **不阻塞**；字段缺失 ⇒ 允许 `None`（**禁止用 0 冒充**）；回流数据污染历史反馈 ⇒ `is_auto=1` 区分，Planner 可配置是否采纳；低互动降权导致选题收敛 ⇒ 降权幅度**上限 20%** |
| **T5.5** ✅ | **发布面板 + 合规留档 + 外部对接**（§06.11 / §06.12 · **已完成 2026-09-17**）：面板**七区块**（待发布/发布中/已发布/数据回流/待人工/**定时计划**/**报告**）、`HandoffAdapter`、来源登记留档、发布应急剧本 | T5.4 | 发布面板七区块可用；`manual_required` 可重试/可标记已人工处理/可取消，且**三者均写 `audit_ops`**；`HandoffAdapter` 默认 `LocalHandoffAdapter` 可 push 自包含交付包（视频/封面/ASS/稿件/manifest/质检）；`docs/runbook/publish_selector.md` 与 `publish_account.md` 剧本齐备；**R2 合规提示常驻**于发布面板与素材库。**已交付**：`publish/handoff.py`（`build_package` / `HandoffAdapter` / `LocalHandoffAdapter`）+ `publish/compliance.py`（`R2_NOTICE` / `compliance_snapshot`）+ 三个新端点（`GET|POST /api/v1/publish/handoff/{task_id}`、`GET /api/v1/publish/compliance`）+ `web/src/views/Publish.vue`（八区块）+ `stores/publish.ts` + `stores/assets.ts` 的 R2 常驻提示 + `docs/runbook/publish_account.md`。测试 **50 例**：`tests/unit/publish/{test_handoff,test_compliance}.py` · `tests/integration/test_publish_handoff_api.py` · `web/src/stores/publish.test.ts`；`.\.tasks.ps1 check` ⇒ **3636 passed / 32 skipped / 27 deselected**；`.\.tasks.ps1 web:verify` ⇒ **406 passed · dist 0.34 MB**。**定时计划（T5.6）与报告（T5.7）后端无端点 ⇒ 面板如实显示「尚未施工 + 卡在哪」，不画假数据**（裁定 299） | 外部对接未定义 ⇒ A2：**内置实现完整链路** + 预留 `HandoffAdapter`（外部实现 ABC 即可接入，不影响主流程）；`manual_required` 堆积 ⇒ 面板顶部计数 + 告警；剧本过时 ⇒ 每次页面改版热修后更新剧本版本号 |
| **T5.6** | **定时发布调度（★口述 D7 新增）**（§06.5.5 / §04.6.5.1）：`scheduler_service`（30s tick）、三种模式（`at_time`/`daily_window`/`interval`）、窗口内**随机取时刻 + 抖动**、`next_run_at` 持久化、到点**才**创建 job、**策略可编辑**（WebUI CRUD：模式 / 窗口 / 抖动 / 启停 · Q14） | T5.3 | `pytest tests/integration/test_scheduler.py -q`：①窗口模式在 `[18:00,21:30]` 内取时刻且叠加 ≤15min 抖动；②**同一 schedule + 同一天多次计算得到同一时刻**（幂等，重启不漂移）；③未到点/已停用的计划**不**被取到（真机已验证该查询）；④到点 ⇒ 创建 `publish` job 且**不占用 worker 空转**；⑤被限频 ⇒ `last_result='skipped_ratelimit'` + 顺延（不算失败）；⑥`publish.enabled=false` ⇒ 空转且记 `skipped_disabled`；⑦连续失败 ≥5 次 ⇒ `system.alert`；⑧计划增删改启停均写 `audit_ops`；⑨**非法窗口参数被拒绝**（非 `HH:MM` / `start ≥ end` / `jitter_min > 120`）且不落库；⑩编辑策略 ⇒ **同事务重算 `next_run_at`**（陷阱 #33） | **定时 ≠ 免限频** ⇒ 限频守卫叠加生效（§03.4.4）；**机器特征** ⇒ 窗口随机 + 抖动（R13）；**重启丢计划** ⇒ `next_run_at` 落库；**重复发布** ⇒ 发布幂等键兜底；调度器本身崩溃 ⇒ supervisor 守护（T4.11） |
| **T5.7** | **数据报告与决策闭环（★口述 D9 新增）**（§06.7 / §04.6.5.2）：日/周/月报告生成（**纯 SQL 聚合 + 规则归因，不调 LLM**）、七维归因、`insights` 决策建议 + 置信度、**人工采纳写回** persona/方向权重、**周期可编辑**（`report_schedules` CRUD · Q15） | T5.4 | `pytest tests/integration/test_reports.py -q`：①周报生成含 `summary_md` + `data_json` + `insights`；②**同周期重复生成不重复插入**（真机已验证 UNIQUE 生效）；③`n<10` ⇒ `confidence='low'` 且 WebUI 强制显示"样本不足"；④`POST /reports/{id}/insights/{idx}/apply` ⇒ 写入 persona/方向权重 + `audit_ops`；⑤**闭环**：采纳后下一轮 Planner 的 `grounded_on` 可见该偏好；⑥非法 `period` 被 CHECK 拒绝（真机已验证）；`GET /reports/{id}/export?format=md` 可导出；⑦**周期编辑**（daily/weekly/monthly 的 `weekday`/`day_of_month`/`at_time`/`lookback_days`/`include`）⇒ `next_run_at` 重算 + `audit_ops`；⑧**同周期仅 1 个启用**（真机已验证部分唯一索引）；⑨停用 ⇒ 不再被到期查询取到 | **数据噪声直接改生产策略** ⇒ `insights` **必须人工采纳**（不自动改配置）；**样本不足误导** ⇒ 置信度分级 + 强制警示；**报告烧钱** ⇒ 不调 LLM（纯 SQL），需要自然语言总结时才走 Agent 通道（可选开关）；**采纳不可追溯** ⇒ `audit_ops` 记 `before/after` |
| **T5.8** | **多账号支持与合规留档（★口述 D1）**（§06.2.4 / §06.9）：`accounts[]` 配置、按账号隔离 `profile_dir`、**限频按账号独立计数**、幂等键含 `account_id`、来源登记留档（R2）、发布/账号应急剧本 | T5.3, T5.5 | **默认配置仅 1 个账号**（`acc_main`）；新增第 2 个账号 ⇒ **零迁移**（仅改 `config/publish.yaml` + 首次扫码）；`pytest tests/integration/test_multi_account.py -q`：①两账号限频互不影响；②同一任务分发到两账号 ⇒ 生成 2 条 `publications`（幂等键含 `account_id`，**不**互相阻塞）；③账号 A 登录态失效**不影响**账号 B 发布；④`reports.data_json` 可按 `account_id` 拆分；`data/voice_src/*/profile.json` 与素材 `proof_path` 合规留档齐备 | **多账号误配** ⇒ 未配置即不参与发布（安全默认）；**账号风控关联** ⇒ 每账号独立限频 + 独立 profile + 错峰发布（T5.6 定时）；**R2 IP 音色合规** ⇒ 音色 ID 与展现名解耦 + 一键替换自录音色 + WebUI 显著提示；**范围蔓延** ⇒ 一期只跑通单账号，多账号仅"结构可用 + 测试覆盖" |

```text
# ── 阶段 T1 · 基座 + 智能体脚手架 + 选题池（12）── 门禁 M1
[x] T1.1  仓库骨架 + 双 venv + doctor 全绿（环境变量全量重定向 D 盘）
[x] T1.2  配置系统（persona/llm/app/pools/outputs/randomization/publish + env 覆盖 + 防越界）
[x] T1.3  数据库与迁移（29 表 + 幂等 + checksum + db check）
[x] T1.4  领域模型与 16 态状态机（16×16 矩阵 + 单入口 + 乐观锁）
[x] T1.5  队列内核（四池 + 单语句原子认领 + 租约 + 退避 + 死信 + 限频守卫）
[x] T1.6  Worker 框架与心跳（四池 + draining + 崩溃恢复）
[x] T1.7  日志与 WS 骨架（8 通道 + 先落库后广播 + since_id 补发 + 背压 + 慢客户端判死）
[x] T1.8  LLM 双通道网关（云端+本地 + Schema 守卫 + 熔断 + 成本闸门 + 提示词注册表）
[x] T1.9  输入源解析 + Planner + Ideator + 选题池（★两级去重）· ✅ 已完成（2026-09-13）
[x] T1.10 Director + Writer（★Director 新增，600–800 字 / ≤180s / 逐句落库）· ✅ 已完成（2026-09-13）
[x] T1.11 双通道评分 + Editor + 确认闸（0.3/0.7 + A/B/C 分级放行 + ≤2 轮）· ✅ 已完成（2026-09-13）
[x] T1.12 一键启动/停止（启动.bat → 5 进程 + 自动开浏览器）· ✅ 已完成（2026-09-13）
       >>> M1：网页端输入定位+热点 → 产出合格稿件（含评分）→ 确认闸可见
       >>> **M1 口径（T1.12 裁定 108）**：一键启动**已可用**（`api` ready + 其余如实报 `degraded`）；
           「5 进程全 ready」要等 T2.2 / T2.6 / T3.x / T4.11 ⇒ 顺延 M4
           **2026-09-17 更新**：T2.2 落地 ⇒ `tts` 的 `server_missing` / `env_missing` 都已消解，五进程入口齐备；
           仍未真机复核（8787/8788 上跑着用户自己起的服务，未动）—— `停止.bat` → `启动.bat` 即可复核

# ── 阶段 T2 · CosyVoice3 配音（9）── 门禁 M2
[x] T2.1  tts venv + 权重就位（py3.11 + torch2.4cu121 + revision 留痕 · Q7 核验）· ✅ 已完成（2026-09-17：权重 21 文件 / 5.23 GB · 真机加载 10.4s / 显存 2.38 GB）
[x] T2.2  常驻推理服务 + 并发实测标定（★裁决 C8：**实测并发 1** · 五端点 + 429 背压 + 空闲卸载 + 看门狗）· ✅ 已完成（2026-09-17）
[ ] T2.3  引擎适配层与路由（决策表逐条 + 熔断 + 字幕模式降级）
[ ] T2.4  原声入库与音色注册（bigbear/littlebear + 质量校验 + R2 合规留档）
[x] T2.5  文本归一化与切分（幂等 + ≥40 用例 + glossary + 不引入 pynini）· ✅ 已完成（2026-09-15）
[x] T2.6  按句合成 + 句级缓存（引擎调用次数为 0 的续传断言）· ✅ 已完成（2026-09-16）
[x] T2.7  时长时间轴（ffprobe 实测 + timeline.json + 全量重算）· ✅ 已完成（2026-09-16）
[x] T2.8  配音编排与降级演练（TTS 全挂 ⇒ 字幕模式仍推进）· ✅ 已完成（2026-09-16）
[x] T2.9  配音服务化接口（单句重配/试听/换音色 + audit_ops）· ✅ 已完成（2026-09-16）
       >>> M2：一句话用熊大音色读出；能断点续传；网页看逐句进度

# ── 阶段 T3 · 渲染引擎（一期单遍合成 + 固定水印 · 7）── 门禁 M3
[ ] T3.1  素材入库（跑酷通配 + 指纹 + 可用区间 + license 强制 + BGM 库）
[x] T3.2  水印资产与合成 profile（水印可选：有就贴没有就跳过；1080×1920 + 720P 保底档）
[ ] T3.3  CompositePlan + 单遍编译器（total_ms=ffprobe + 循环裁长 + 水印 overlay + ff_path + 节点守卫）
[ ] T3.4  单遍合成执行器（argv 数组 + 进度 + 超时杀树 + .partial 原子改名 + composite_hash 缓存）
[x] T3.5  字幕生成（可选默认开启：ASS + 自动换行/限字/描边/居中 + 字体校验）· ✅ 已完成（2026-09-15）
[x] T3.6  混音与响度（normalize=0 + 侧链 ducking + 两遍 loudnorm + 限幅；BGM 缺失静音降级）· ✅ 已完成（2026-09-15）
[x] T3.7  成片交付与降级链（manifest + composite_hash + quality_json 回填 + 黑屏/720P 降级）· ✅ 已完成（2026-09-16）
       >>> M3：换稿不重剪（同素材同水印换稿件直接出片）；网页一键出新片并在线预览
       （二期 P1 预留：T3-P1..T3-P4 三层模板场景编排 —— 不阻塞 M3）

# ── 阶段 T4 · 网页操作台 + 四池并行 + 无人值守（13）── 门禁 M4
[x] T4.1  前端脚手架与设计系统（OpenAPI 生成类型 + WS 重连）· ✅ 已完成（2026-09-14）
[x] T4.2  ① 总览台（四池 + 资源 + 一键全自动 + 暂停）· ✅ 已完成（2026-09-14）
[x] T4.3  ② 选题面板（方向卡片 + 瀑布流 + 勾选入队）· ✅ 已完成（2026-09-14）
[x] T4.4  ③ 稿件面板 + 确认闸（双通道明细 + 版本对比 + 批量）· ✅ 已完成（2026-09-14）
[x] T4.5  ④ 配音面板（逐句进度 + 换音色 + 重配 + 试听）· ✅ 已完成（2026-09-16）
[x] T4.6  ⑤ 渲染面板（合成进度 + 出片表单 + 成片在线播放 + 协作式取消 + UI 由 API 托管）· ✅ 已完成（2026-09-15）
[x] T4.7  ⑥ 合成配置面板（profile/水印/字幕表单编辑；三层模板树延后二期 · R17）· ✅ 已完成（2026-09-14）
[x] T4.8  ⑦ 素材库（导入/预览/标记/授权/统计 + R2 提示）· ✅ 已完成（2026-09-15）
[x] T4.9  ⑧ 实时日志（过滤/搜索/告警高亮/导出/断线不丢）· ✅ 已完成（2026-09-14）
[x] T4.10 四池调度控制台（并发旋钮 + 暂停恢复 + 死信重投 + 自动降级）· ✅ 已完成（2026-09-14）
[x] T4.11 无人值守编排与自愈（supervisor + 水位门禁 + 人工池 + 24h 无人干预）· ✅ 已完成（2026-09-14，24h 长跑待 T4.12）
[x] T4.12 观测/备份/GC/审计页/应急剧本（含恢复演练）· ✅ 已完成（2026-09-14）
[x] T4.13 人物库面板（persona 编辑 / 切换 / 回滚 + `system.persona_changed`）· ✅ 已完成（2026-09-14）
[x] T4.14 四屏端到端串联（选题 → 稿件 → 配音 → 渲染，四屏不手抄任务号）· ✅ 已完成（2026-09-16）
       >>> M4：≥3 篇同时推进；中断后恢复；全程网页操作

# ── 阶段 T5 · 发布 + 定时 + 报告 + 数据回流（8）── 门禁 M5（★第六部分重建 + 口述 D7/D9）
[x] T5.1  快速封面 + 发布前二次校验（水印/响度/相似度门禁 + 禁区扫描；av_sync 仅诊断）
[x] T5.2  发布适配层与 profile（一线三平台 + dry-run + 选择器集中化 + 标题回读比对）
[x] T5.3  发布池 + 限频 + 失败转人工（幂等键含 account_id + 不回退任务）· ✅ 已完成（2026-09-17）
[ ] T5.4  数据回收 + 记忆沉淀闭环（T+1h/6h/24h/72h + auto 回流可消费）
[x] T5.5  发布面板（七区块）+ 合规留档 + HandoffAdapter + 发布应急剧本 · ✅ 已完成（2026-09-17）
[ ] T5.6  定时发布调度（★D7：三模式 + 窗口随机 + next_run_at 持久化 + 到点才建 job）
[ ] T5.7  数据报告与决策闭环（★D9：七维归因 + insights 置信度 + 人工采纳写回）
[ ] T5.8  多账号支持与合规留档（★D1：结构支持/默认单账号 + 限频按账号独立）
       >>> M5：定时/即时自动发布（≥1 平台）+ 数据回流 + 报告与决策采纳 + 记忆沉淀闭环

# ── 阶段 T6 · 追加任务（不在原文 50 个任务内 · 不占一期工期）── 无独立门禁
[x] T6.1  设置面板（LLM 通道与密钥：`config/secrets.yaml` 热重载 + env 优先 + CLI/面板共用探测）· ✅ 已完成（2026-09-17）

# ── 全程（横切）──
[ ] 每个任务有引用条款编号的契约测试
[ ] 每个外部依赖有显式失败分支 + 测试覆盖
[ ] 无裸 except / 无静默失败（静态检查）
[ ] pytest -m "not gpu and not slow and not net" 全绿
```

---

## 5.7 高频陷阱对照表（164 条 · 实现期直接查阅）

| # | 现象 | 根因 | 正确做法 | 任务 |
| --- | --- | --- | --- | --- |
| 1 | `database is locked` | 长事务 / 事务内做 I/O | 短事务（<20ms）+ WAL + `busy_timeout=5000` | T1.3 |
| 2 | 任务被重复执行 | 无租约或租约过长 | 单语句原子认领 + 租约 + sweeper | T1.5 |
| 3 | **视频流先结束 ⇒ 画面冻结 / 提前截断**（v3.1 按 C12 已**不再要求**句子级音画同步） | 未显式指定总时长 / VFR / 用了 `-shortest` | **音频为时长基准** + CFR + 显式 `-t` + **禁用 `-shortest`** + 循环补齐 | T2.7 / T3.3 |
| 4 | 人声偏小 | `amix` 默认 `normalize=1` 衰减 | 显式 `normalize=0` + 两遍 `loudnorm` | T3.6 |
| 5 | 字幕豆腐块 | libass 找不到字体 | 项目内置字体 + `fontsdir` + 启动校验 | T3.5 |
| 6 | `ass` 滤镜路径报错 | Windows 冒号/反斜杠未转义 | 统一 `ff_path()`，驱动器冒号转 `\:` | T3.3 |
| 7 | 滤镜图报 `Invalid argument` | 命令行超长 / 引号错配 | `-filter_complex_script` 文件 + 语法预检 | T3.3 |
| 8 | 渲染缓存复用了旧产物 | 哈希未包含输入/配置 | `composite_hash` 含 canonical plan + 输入 sha256 + 水印/字幕参数 | T3.4 |
| 9 | 崩溃后留下"假完成"产物 | 直接写目标文件 | 先写 `.partial` 再 `os.replace` | T3.4 |
| 10 | TTS 首句延迟 20s+ | 模型未预热 / 每句重载 | 常驻服务 + `/warmup` + 空闲卸载 | T2.2 |
| 11 | 显存 OOM（8 GB 卡） | fp32 / 并发过高 | fp16 + **默认单并发** + 空闲卸载 + **禁 bf16** | T2.2 |
| 12 | 前端被进度刷爆 | 高频逐帧推送 | 合并窗口 100ms + 2Hz/任务限流 + 环形缓冲 | T1.7 / T4.9 |
| 13 | 日志断线后丢失 | 先广播后落库 | **先落库再广播** + `since_id` 补发 | T1.7 |
| 14 | 素材被平台判搬运 | 固定素材/固定入点/固定编码 | 随机化两档 + 相似度审计门禁 | T3.1 / T3.3 |
| 15 | C 盘爆满 | 临时文件/模型缓存落在系统盘 | 环境变量重定向 + 磁盘门禁 | T1.1 / T4.11 |
| 16 | **克隆场景导致画面重复** | `repeat_last` 扩展时复用了源场景的素材与入点 | 克隆场景**强制重新抽素材 + 变换差异化** | T3-P3（二期） |
| 17 | **字幕与配音错位** | 克隆场景后用了绝对时间绑定 | 被克隆场景**不得含绝对时间**，一律用 `$sentence.start_ms/end_ms` | T3-P3（二期） |
| 18 | **选题同质化（20+ 选题雷同）** | 批量生成无去重 | 归一化哈希 + 相似度 ≥0.85 降分 + 历史库比对 | T1.9 |
| 19 | **LLM 成本失控** | 批量选题 × 6 Agent × 2 轮改稿 | token 预算上限 + 成本面板 + 超限切本地模型 | T1.8 / T4.2 |
| 20 | **发布不可逆事故** | 自动发布无二次校验 | `publish.enabled=false` 默认关闭 + `require_confirm=true` + 发布前重跑三道门禁 | T5.1 |
| 21 | **发布登录态失效被静默跳过** | 未探测登录态直接上传 | 发布前 `health()` 探测 ⇒ 失效即转 `manual_required` + 告警（**不自动登录**） | T5.2 |
| 22 | **操作无法追责** | 人工操作只进了全量日志 | 人工/自动决策写 `audit_ops`（含 `before/after`），WebUI 可查 | T4.12 |
| 23 | **平台页面改版导致选择器全失效** | 选择器散落在代码里 | 集中在 `publish/selectors/*.yaml` + `selectors_version` 留痕 + 失败截图 | T5.2 |
| 24 | **发布文案被平台编辑器吞掉** | 富文本编辑器截断/丢 emoji | 发布后**回读标题与文案逐字比对**，不一致则重填 ≤2 次 | T5.2 |
| 25 | **`repeat_last` 后总时长与时间轴不一致** | 场景时长求和未与 `total_ms` 校验 | 编译期断言 `Σ scene_duration = total_ms ± 1ms`，不一致直接报错 | T3.3 / T3-P3（二期） |
| 26 | **单句编辑后其余句子音频被复用错位** | 增量拼接时间轴 | 任一句重合成 ⇒ **时间轴全量重算**（禁增量） | T2.7 |
| 27 | **成片没水印** | 水印缺失被当成"可选"静默跳过 | **已接受**：水印改为可选装饰 ⇒ 缺失即跳过并把 `skipped_reason` 写进 manifest（成片优先）。若某天要求"必须带水印"，那是发布前校验（T5.1）的事，不是渲染的阻塞 | T3.2 / T5.1 |
| 28 | **跑酷素材比人声短 ⇒ 画面提前黑屏/冻结** | 未做循环补齐 | `loop` + `trim=duration=total_ms` 补齐；素材为空 ⇒ 纯黑底仍出片 | T3.3 |
| 29 | **定时发布变成"每天准点"的机器特征** | 固定时刻发布 | `daily_window` 窗口内随机取时刻 + `jitter_min`（默认 15min） | T5.6 |
| 30 | **定时任务重启后丢失 / 重复触发** | `next_run_at` 只在内存 | `next_run_at` **落库** + 取时刻用 HMAC(seed=id+日期) ⇒ 幂等；发布幂等键兜底防重复 | T5.6 |
| 31 | **报告被当成"结论"直接改生产策略** | 数据噪声 / 样本不足 | `insights` **必须人工采纳**；`n<10` 强制标注"样本不足"；采纳写 `audit_ops` | T5.7 |
| 32 | **报告生成烧钱** | 用 LLM 写总结 | 报告 = 纯 SQL 聚合 + 规则归因（**不调 LLM**）；需要自然语言时才走 Agent 通道（可选） | T5.7 |
| 33 | **改了定时/报告周期却不生效（或立刻触发）** | 编辑计划只改了参数、没重算 `next_run_at` | 任何策略编辑 ⇒ **同事务重算 `next_run_at`** + 写 `audit_ops`；停用 ⇒ 置 NULL | T5.6 / T5.7 |
| 34 | **从另一个控制台发 Ctrl-Break：API 返回 TRUE，目标却收不到**（进程照旧在跑，最后被硬杀 ⇒ 丢进度） | `GenerateConsoleCtrlEvent` 只作用于**同控制台**的进程组；`停止.bat` 是另起控制台 | 关停主通道 = **标志文件** `data/logs/<name>.stop`（池 worker 在 1s 脉冲 tick 里看到就 draining）；信号只作升级手段 | T1.12 |
| 35 | **启动后 worker「什么都不干」** | 上一轮关停留下的 `.stop` 标志没清，worker 起来第一拍就自己关掉 | `start` 的第 ② 步 `clean_stale()`：**先清标志与陈旧台账再拉进程**；`stop` 结束后也必须清 | T1.12 |
| 36 | **把「端口被占用」报成「已在运行」**（或反过来：重复拉起第二个实例） | 只探测端口、不看 PID 台账 | 两个判据分开：`already_running`（台账里有活进程）vs `port_busy`（端口被**别人**占） | T1.12 |
| 37 | **对 uvicorn 等「优雅退出」白等 6 秒** | HTTP 面的进程没有 tick 循环，**永远**不会去读标志文件 | `ServiceSpec.polls_stop_flag` 区分：池 worker `True`（等标志），HTTP 进程 `False`（直接走信号） | T1.12 |
| 38 | **子进程起来就死，却看不到原因**（日志全丢） | 没有重定向子进程 stdout/stderr，`Popen` 的管道没人读 | 追加写 `data/logs/<name>.log` + `PYTHONUNBUFFERED=1` + `stdin=DEVNULL` | T1.12 |
| 39 | **`DraftReport.created_task` 在复用任务时说谎** | 幂等键命中时 `create()` 原样返回旧行 ⇒ 光看返回值分不出新旧 | 建任务前先 `find_by_idempotency_key()` 问一次；测试必须断言**第二次 `created_task=False`** | T1.10 |
| 40 | **LLM 自报总分 ⇒ 定级不可信** | 把 `total` / `grade` 放进模型输出 schema | 模型**只给六维度 + `issues`**；`total` / `grade` / `decision` 一律服务端算 | T1.11 |
| 41 | **`need_edit` 被当成"一定要改稿"** | DDL 的 `decision` 只有 5 值、没有"等人工"这一项 | `Decision`（落库事实）与 `GateAction`（动作）**拆成两个枚举**；`human_gate → need_edit` | T1.11 |
| 42 | **并发退回丢更新** | `revision_round` 用"读到的旧值 + 1" | 用 SQL 增量 `revision_round = revision_round + 1`（`bump_revision=True`） | T1.11 |
| 43 | **待审列表里有一条永远点不掉的记录** | 自动放行走 `request()` + `decide()` **两个事务** | `record_auto_approval()` **一次事务**写 `approvals(status='approved')` + `audit_ops` | T1.11 |
| 44 | **改稿越界判失败 ⇒ 比"稿件稍越界"更贵** | 越界即 `EDIT_FAILED` | 重试 1 次后**照收 + `warnings`**；越界判定本身是启发式的 | T1.11 |
| 45 | **重跑同一版撞 `UNIQUE (script_id, round_no)`** | 以为是 bug | **故意的**："这一版被审过几次"必须能从库里看出来 | T1.11 |
| 46 | **确认闸规则写在 REST 控制器里** | 四条不变量每个入口抄一遍 | 规则住 `ReviewService.decide_approval`（**抛 `StudioError`**）；REST 只是薄壳（T4.4） | T1.11 |
| 47 | **人工退回后重审被 `ValidationError` 炸掉** | `ReviewerInput.round_no` 设了 `le=REVISION_LIMIT+1` | 输入模型**只保下界**；轮次闸门由 `gate_action` 把守 | T1.11 |
| 48 | **`import file mismatch` 中断整轮测试收集** | `tests/unit/domain/test_scoring.py` 与 `tests/integration/test_scoring.py` 重名且都不带 `__init__.py` | 同名测试目录补 `__init__.py`（与 mypy 的"两个模块名"同一条理由） | T1.11 |
| 49 | **改稿越界检查永远"无越界"** | `_edit` 把**空句子列表**传给 Editor | 透传 `sentence_rows`；越界检查必须有非空输入 | T1.11 |
| 50 | **人工退回后的任务被炸成 `failed`** | `review()` 不把 `EDITING` 归一为 `REVIEWING` ⇒ 走 `editing → queued_voice` 这条**不存在的边** | 审稿入口先归一状态；工人漏推状态不该让任务死 | T1.11 |
| 51 | **告警互相抹掉** | 循环内 `warnings = [...]` **覆盖**上一轮 | **累积**：审稿与改稿的告警都该留下 | T1.11 |
| 52 | **断言"唯一一条 reason"被自动改稿顶掉** | `task_events` 里同一 `to_status` 有多条（自动改稿 + 人工退回） | 断言**最后一条**（`ORDER BY id`）而不是"唯一一条" | T1.11 |
| 53 | **测试里漏 `request_stop()` ⇒ worker 永不返回 + 脉冲线程泄漏** | 测试只 `join()` 不请求停止（`run()` 没收到停止就永不返回，这是生产语义） | 登记表 + autouse 守卫兜底停；断言 `not thread.is_alive()` | T1.6 |
| 54 | **mypy「同一文件两个模块名」顺带掩盖真实类型错误** | 测试目录缺 `__init__.py` 却写 `from tests.x.y import z`（mypy 报一次重复模块名后**跳过该文件**，本次掩盖了 12 个错误） | 需要跨模块复用假件的测试链路补 `__init__.py`；修完重复名后**必须重跑** `mypy` | T1.7 |
| 55 | **`llm_calls` 同毫秒内乱序**（「第几次尝试」随机错位，整仓跑才复现） | `created_at` 只到**毫秒**（`format_iso`），而 `new_ulid()` 同毫秒内是**随机**后缀 ⇒ `ORDER BY created_at DESC, id DESC` 不是全序 | 定序一律 `ORDER BY created_at DESC, rowid DESC`；测试断言正序 = 翻转 `recent()`（单跑必过、整仓随机失败最难查） | T1.8 |
| 56 | **反复重试绕开预算闸门** | 只记成功调用 ⇒ 失败重试不烧账、`tokens_for_task` 永远不超 | **失败也写 `llm_calls`**（`schema_invalid`/`http_error`/`timeout` 都记 token 与成本）；`record()` 记账失败不得中断主链路 | T1.8 |
| 57 | **提示词模板静默退化**（改了模板但输出没变 / 变量名写错却照跑） | 模板里写控制流、或变量缺失时渲染成空串继续跑 | 只支持 `{{变量}}`；遇 `{%`/`{#`/缺变量**直接报错**；`prompt_version` 由内容 sha256 决定（`manifest.yaml` 漂移即红灯） | T1.8 |
| 58 | **`vite.config.ts` 报 TS2769（`test` 段不是已知属性）** | `vitest@2` 与 `vite@6` 并存 ⇒ node_modules 里**嵌套了第二份 vite**，`defineConfig` 的类型来自旧那份 | `defineConfig` 从 `vitest/config` 取；vitest 与 vite **必须同大版本**（升 vitest 3.x 消掉嵌套 vite）。判据：`node_modules/vitest/node_modules/vite` 不该存在 | T4.1 |
| 59 | **前端某路日志字段静默 `undefined`**（面板少一截且不报错） | WS 事件用 `log_id`、快照行/REST 用 `id`；事件还缺 `trace_id`/`seq_in_task` | 两侧统一走**一个归一函数**进缓冲；契约测试断言 `LogRow` 字段集 == `SystemLog.to_dict()`；`fetch(` 全前端收敛到 `api/http.ts` | T4.1 |
| 60 | **每重连一次白跑一轮 `resync`** | `seq` 是**每连接**单调（新连接从 1 重新开始），客户端却沿用旧基线 ⇒ 第一帧被判缺口 | `onopen` 重置 `lastSeq = 0`（只建基线不判缺口）；`since_id` 游标**只进不退** | T4.1 |
| 61 | **搜 `50%` 把整张日志表捞回来** | 用户输入直接进 `LIKE`，`%` / `_` 被当通配符 | 统一 `like_pattern()` 转义（**先转义 `\` 再转义 `%` `_`**）+ SQL 里显式 `ESCAPE '\'`；`search` 同时匹配 `message` 与 `source` | T4.9 |
| 62 | **「导出到这条为止」被静默忽略**（`until_id` 不生效，导出的比要的多） | `since_id` 为空时走「最近 N 条」分支，而那个分支**没有**窗口上界 | 窗口上界必须进 SQL（`recent(until_id=…)`），不能靠调用方事后截断 —— 截断与查询在分页边界上不等价 | T4.9 |
| 63 | **`logger.info(msg, **payload)` 抛 `TypeError`**（日志发不出去） | 事件名用了 `event` 这个键，而 structlog 的第一个位置参数就叫 `event` | 载荷键固定 `event_kind`（`core/proto.py` 的 `EVENT_PAYLOAD_KEY`）；兜底分支再过滤 `_LOG_RESERVED` | T4.4 |
| 64 | **`services/` 要声明事件名却 import 不到 `ws/`** | 分层方向是 `app → ws → services → db`，反向 import 会成环 | 事件枚举**下沉**到 `core/proto.py`；`ws/protocol.py` 原样 re-export（导入路径不变，调用方零改动） | T4.4 |
| 65 | **改稿只加了一句，diff 却把后面全标成「改了」** | 逐 `seq` 对齐（插入一句 ⇒ 之后每一句的 `seq` 都变了） | 走 `difflib.SequenceMatcher` 的**块级**匹配；`replace` 块内按位置配对，多余一侧降级成 `insert` / `delete` | T4.4 |
| 66 | **前端要写两套错误解析** | 业务错误（`StudioError`）与 `RequestValidationError` 各返回一种形状 | 应用级 handler 把两者都翻成 `StudioError.to_dict()`（映射表住 `app/errors.py`），**信封只有一种形状** | T4.4 |
| 67 | **批量放行 10 条失败 1 条 ⇒ 人重按一次** | 把"部分失败"报成"整体失败" | 逐条如实返回 `approved` / `failed`（含 `code` / `remediation`），**不回滚**；重按只会撞 `APPROVAL_NOT_PENDING` 的噪音 | T4.4 |
| 68 | **响应模型的集合字段在前端变成 `T[] \| undefined`**（处处 `?? []`） | `Field(default_factory=list)` 在 JSON Schema 里既不进 `required`、也不带 `default` | 响应模型写 `x: list[T]`（**必填**）；请求体才保留默认值 | T4.3 |
| 69 | **请求体的"可选"字段在前端变成必填** | `openapi-typescript` 把**带 `default` 的属性**渲染成必填 | 前端显式传（`import_sources: true` / `per_direction: 4`）—— 顺带让"这一跑要不要先扫盘"在调用点看得见 | T4.3 |
| 70 | **长任务单飞用 `asyncio.Lock` 会跨线程炸** | 同步路由跑在 Starlette 线程池（每次可能不同线程），`asyncio.Lock` 与事件循环绑定 | 入口一把**进程级 `threading.Lock`** 非阻塞地拿；拿不到 ⇒ 409（**不排队**：排队会让前端挂住，用户还不知道自己排第几） | T4.3 |
| 71 | **动作刚报的错被紧随的成功刷新抹掉**（用户唯一能看到的解释没了） | 拉取与动作共用一条 `error`，刷新成功时顺手清空 | **两条信道**：`error`（动作级结论）vs `loadError`（拉取级失败）；视图 `error ?? loadError` | T4.3 |
| 72 | **一个用例里换第二次假件却不生效**（仍在用旧传输） | `arm()` 把"原函数"取成 `deps.build_gateway` —— 它第一次之后已经是我们自己换上去的假件 | 原函数取**未被 patch** 的那份（`gateway_factory.build_gateway`） | T4.3 |
| 73 | **假件方向数不够 ⇒ `LLM_SCHEMA_INVALID`（"is too short"）** | `planner_result.schema.json` 要 5–8 个方向、`ideator_result.schema.json` 每方向 3–5 条 | 脚本化回应按契约给足；`ScriptedTransport` 用尽后会**复用最后一条** ⇒ 逐方向各给一条，否则 5 个方向拿到同一批标题会被去重丢掉 4 份 | T4.3 |
| 74 | **查事件查不到：`no such column: payload`** | `system_logs` 的列名是 `payload_json`（DDL），而 API / 文档口径叫 `payload` | 直接查库用 `payload_json`；读侧统一走 `SystemLog` / `LogRow` | T4.3 |
| 75 | **"今日产量"在 UTC+8 的 08:00 前把今天新建的算进昨天** | `substr(created_at,1,10)` 是 **UTC 日**，而"今天"是本地日 | 窗口由 `local_day_window()` 算成**左闭右开**的一对 UTC 时间戳再进 SQL | T4.2 |
| 76 | **在 API 进程里"停服务"等于自杀** | `ServiceManager.stop()` 的第一个目标就是 `api` 自己 | 只暴露"启动"（`open_browser=False` + `doctor_gate=True`）；停止的正门是 `停止.bat` | T4.2 |
| 77 | **采样泵在工作线程里 `publish()` 抛 `RuntimeError: Non-thread-safe operation`** | `Hub.publish()` 末尾直接 `self._wake.set()`，而 `asyncio.Event.set()` 不是线程安全的 | 走 `Hub.wake()`（内部 `call_soon_threadsafe`）；`pool.*` / `metrics.*` 三个事件此前**从未真正发出去过** | T4.2 |
| 78 | **每次启动凭空多一条"磁盘水位恢复"**（顶掉日志通道第一帧） | 磁盘状态机的初值 `None` 被当成"低水位"，`None → ok` 也走恢复分支 | `None` = "还没采过"：首拍**低位照常告警**，首拍**健康一个字都不写** | T4.2 |
| 79 | **并发降到 0 = 沉默的暂停**（与 `paused=1` 在面板上根本分不出来） | DDL 的 `CHECK (concurrency BETWEEN 0 AND 8)` 允许 0，而 0 看着像个并发数 | 旋钮下限硬编码 **1**（`POOL_CONCURRENCY_MIN`）：要停就点「暂停」—— 那条路有留痕、有语义、有「在途跑完」的说明 | T4.10 |
| 80 | **自动降并发连降 4 次，并发一路掉到下限**（阈值 2，连来 5 次 OOM） | 判据写成 `consecutive_oom >= threshold` ⇒ 之后每一次 OOM 都算「又越线了」 | 判据 `== threshold`；计数器由 `succeed()` 归零 ⇒ 下一次越线必然发生在「池又成功过一次」之后 | T4.10 |
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
| 115 | **点了「重配」没反应、不报错，任务卡在 `voicing`** | 队列的幂等键是 `(task_id, pool, unit_type, unit_ref)` ⇒ 一条单元**一辈子只有一条作业**；`succeeded` 之后业务侧把 `tts_status` 改成 `pending`，**没有任何 worker 会再看它一眼**，而 `settle_voice` 的守卫是「全部句定局」 | 重配必须**同时**改两处：业务表（`SentenceRepo.invalidate`）+ 调度令牌（`JobStore.requeue_unit`）。**别只看 REST 返回 200** —— 判据要落在「那条作业回到了 `pending` 且真的能被 `claim` 到」 | T2.9 |
| 116 | **点一次「重配」，降级句只给了一次机会就又降级**（看不见的错） | `invalidate` 没把 `tts_attempts` 归零，而上一轮已经撞过 `DEGRADE_AFTER_ATTEMPTS`（3 次） | 失效时 `tts_attempts = 0`：人工重配的意思是「给它一次**完整**的机会」，不是「接着上一轮的第 3 次算」 | T2.9 |
| 117 | **换完音色，重配出来还是旧嗓子，而库里显示新音色** | 只改了 `payload_json.voice_map` 与 `tts_status`，没同步 `jobs.payload_json` —— 音色是**运行期选择**，worker 读的是作业 payload（或进程装配值） | 三处一起改：`tasks.payload_json.voice_map` + `script_sentences.tts_status` + `jobs.payload_json`（`requeue_unit(payload=...)` 是**整体替换**） | T2.9 |
| 118 | **试听端点变成任意文件读取** | 路径若由请求拼出来（或直接信任库里的 `tts_audio_path`），`../../` 与绝对路径都能播出去 | 正则**整串**匹配 `voice/<task_id>/s00N.wav`（非法键在路由层就 422）；真正的文件路径只由 `StudioPaths.sentence_wav` / 缓存键拼出；库里的 `tts_audio_path` 还要再判「落在 `data_dir` 里」 | T2.9 |
| 119 | **点一下试听就把配音重跑了一遍**（还覆盖了交付产物） | 试听顺手触发合成 —— 而它与 voice 池抢同一台机器（§05 明确要求「走缓存文件，不触发新合成」） | 试听只认**盘上已有**的三处候选（规范路径 / 库里的路径 / TTS 缓存），一处都没有 ⇒ 404 `PATH_MISSING`。面板上「播放」与「重配」是两颗按钮，不是一件事 | T2.9 |
| 120 | **只改一个角色的音色，另一个角色的映射被悄悄抹掉** | `PATCH` 写成**整体替换** `voice_map`：面板只提交被改的那个角色 ⇒ 另一个角色退回进程音色，而用户以为自己只动了一个人 | `PATCH` 走**增量合并**（`{**before, **提交}`）；要一次改两个就一次给两个键。角色名写错（`bigBear`）同样静默无操作 ⇒ 未知角色一律 422 并列出已知的 | T2.9 |
| 121 | **拿「总时长变了」当「时间轴重算了」的证据 ⇒ 假绿** | 真 SAPI 对同一句同一音色是**确定性的**：重念出来的时长逐毫秒一致，`total_ms` 一个字都不变（真机演练实测 14352 → 14352） | 客观证据是**文件被重写**（`timeline.json` / `voice_master.wav` 的 mtime 变了）；「时长变化可见」要用**可控引擎**的用例来钉（集成测试里 700ms → 1400ms）。两件事分开验 | T2.9 |
| 122 | **面板默认任务号（`ui-20260916-120000`）一进配音面板就整屏 422**，而报的是「路径不合法」 | 配音路由的 `PathParam(pattern=...)` 只认 `[0-9A-Za-z]`，而任务号是**调用方起的名**（`RenderJobRequest.task_id` 只限长度、不限字符集）—— 渲染面板默认给的就是带连字符的那一种 | 放开成 `[0-9A-Za-z_-]{1,64}`（媒资键的整串形状一个字都没松）。**校验形状时先问「这个值是谁生成的」**：服务端生成的（句子 id / ULID）可以卡死，人填的只能卡「拼进 URL 会出事」的那些字符 | T4.5 |
| 123 | **新建任务第一次换音色必然 422**：改的是熊大，被拒的理由是没碰过的熊二 | `set_voice_map` 校验的是**合并后的整张表**，而 `TaskPayload.voice_map` 的默认值是逻辑角色名占位 | 只判**这次提交的那几条**（§04.3.7「音色校验的两条线」本来就是这么写的：存量值交给 `resolve_voice` 退回进程音色并标 `fallback`）。**「人刚填的」与「库里早存着的」是两件事** | T4.5 |
| 124 | **「我只想换熊大」，被一句「熊二的音色不存在」挡回来** | 面板把**整张映射**发上去 ⇒ 顺手替用户断言了他没碰过的那些行 | 只发改过的角色（`changedSpeakers`）；PATCH 的语义本来就是「只改我点到的这几个」 | T4.5 |
| 125 | **音色下拉框空着**，而库里明明记着这个角色的音色 | `<select>` 的 `:value` 不在任何 `<option>` 里 ⇒ 显示成空白；空白被读成「这个角色没有音色」，真实原因是「参考音没入库 / 系统语音包没装」 | 把那个值**也渲染成一个 option**（`· 本机找不到（换一个）`）+ 行上挂红标 | T4.5 |
| 126 | **空闲时面板上的数字一直在跳**，而它跳动的唯一原因是「我们在定时问」 | 轮询开着不放（1s 一次，什么新东西都没有），还顺手把音色清单也塞进轮询里重画下拉框 | 只在 `pending + synthesizing > 0` 时轮询（失败与跳过都是**定局**）；音色清单只在首屏与手动刷新时拉 | T4.5 |
| 127 | **新端点整屏 404，而 `/api/v1/health` 是 200**（排查会去查前端） | 上一轮的 API 进程还占着端口（健康检查是**旧进程**在答），新路由没加载 | 改完后端**必须重启 API 进程**再验；看到「新端点 404 而健康检查正常」先查端口占用，别去翻前端 | T4.5 |
| 128 | **跳过去那一屏还是上一个任务**（或者干脆空着），而**不报错** | 认领跳转放在首屏拉取**之前**：稿件面板的 `refresh()` 会把"不在当前状态列表里"的选中项清掉，配音面板的 `setTaskId()` 会把上一条任务的快照清掉 | **先拉列表、再认领跳转**（`await refresh()` ⇒ `takeHandoff()` ⇒ `select()`；配音面板是 `setTaskId()` ⇒ `start()`）。顺序错不会报错，只会静默显示别的任务 | T4.14 |
| 129 | **人自己点侧边栏走，却被一个过期的任务号又跳一次** | 待认领的跳转**留着不清**（或只在"目标面板认领"时清），于是那一笔在内存里躺着等下一次 | 认领即清空 + `selectPanel()`（人自己点走）也清掉；"面板把人送过去的"与"人自己走过去的"是两件事 | T4.14 |
| 130 | **封面只有最后一行字**（前面那几行不报错地消失） | 一条命令里写了多个 `-vf`，而 ffmpeg **只认最后一个** | 全部滤镜**逗号连成一条** `-vf`；`drawtext` 每段一条、顺序即绘制顺序 | T5.1 |
| 131 | **`drawtext` 报 `No option name near /Windows/Fonts/…`** | `fontfile=` 的值要过两层解析，`C:/…` 的驱动器冒号在第一层就被当成分隔符 | 写成 **`fontfile=C\:/…` 外加单引号**（转义 + 引号）。⚠️ **shell 里手敲单反斜杠会成功** —— 别拿「命令行里试过」当 argv 的证据 | T5.1 |
| 132 | **长标题溢出到屏幕外，而且不报错** | 量宽命令漏挂 `bbox` ⇒ 量出来恒为 0 ⇒ 既不缩字号、又按「宽 0」居中 | 量宽命令末尾挂 `bbox=min_val=…`（量**黑底白字**）；量不出来时**按字数估**，绝不返回 0 | T5.1 |
| 133 | **相似度审计没过，却安安静静地过审了** | `GateResult.passed` 与 `blocking` 揉在一起 ⇒ 「审了没过」变成 `passed=True` ⇒ warn 循环一句都不出 | `passed` 报审计结论本身，`blocking` 只报要不要拦 | T5.1 |
| 134 | **`publish/` 里冒出 `from studio.render…` 却没人发现**（§02.1 明令禁止） | 函数内导入 + `# noqa: PLC0415` 让违规**绕过了 ruff 的可见性**（写在模块顶一眼可见，塞进函数体只剩一行 noqa） | 分层依赖必须在**模块级**成立；函数内导入只允许在 §02.1 里**明列**（现仅 `precheck` 量响度一处）；`system_font_dirs` 这类平台事实下沉到 `core/fonts.py`（裁定 261） | T5.1 |
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
| 150 | **面板说"跑酷素材 0 条 / 当前为黑屏降级模式"，而片子里正放着跑酷** | 面板读**库**（`repo.list_all()`）、出片读**目录**（`render/assets.py` 只 `listdir`）—— 两个真相源，而用户只能看见一个。盘上 58 条一条没入库时，这一屏上的每个数字都与事实相反 | 面板必须把**盘上事实**合进来，并给出**一个与出片同口径**的数（`usable`）。三组数字分开报（出片能挑到 / 盘上 / 已入库），"不够多"（`shortfall`）与"会不会黑屏"（`degraded`）也分开 —— 后者只回答"一条都挑不到吗" | T4.8 |
| 151 | **面板上点了「停用」，出片照样挑到它** | 验收写着"禁用 ⇒ 随机化不再选中"，而渲染器**全文没有 `enabled` 这个词** —— 它只列目录。库到渲染器之间**根本没有这条线** | 补的是**接线**、不是校验：调用方查库 → 翻成**文件名集合** → `pick_*(exclude=)` 做一次集合减法（渲染器照旧不碰数据库、不查时长、不算 pHash）。缺省 `None` ⇒ 退回"能进目录就算数"，旧调用方不受影响 | T4.8 |
| 152 | **面板上写「出片照样会挑到它们」，可那一节是音色** | 想用一句通用的话盖住三类素材，而两条链路**真的不同**：出片挑素材只列目录（未入库照样用），配音只认 `voice_profiles` 表（未入库挑不了） | 同一件事按类别说不同的话（后端 `_usable` 与前端 `pendingNote` / `usableText` 各分一次叉）。"出片能挑到 0 个音色"还会让人以为音色是拿去当画面的 | T4.8 |
| 154 | **面板上选了个音色，成片出来没人声**（而库里写着"换音色成功"） | 把「本机有什么音色」（`voice_profiles` 已启用 ∪ 系统音色）当成「**这台引擎念得出来吗**」用了。参考音的名字 SAPI 不认识：`SelectVoice` 抛 ⇒ 这一句连失败 3 次、降级成静音，**每一句都是** ⇒ 整片没人声。真机实测 `synthesize(voice="bigbear")` ⇒ `rc=1` | 两个真相源**分开**：`usable_voices`（下拉框的候选，必须全）与 `speakable_voices`（当前引擎认不认，唯一裁判）；校验与投递走后者，面板把每行标成「当前引擎念不出来」并把 422 的 `absent` / `unspeakable` 分开说 | T2.4 |
| 155 | **交付包缺件那一行只有「缺」两个字，没有补救说明** | `build_package` 的兜底条件写成 `source is not None and not source.is_file()` —— "路径根本没给"走不到兜底，`note` 留在 `None` | 判据改成"**这一件不在盘上**就兜底"（"路径没给"与"文件没了"都算缺，后者用更具体的那句话，**不覆盖**它）；写测试时才发现 | T5.5 |
| 156 | **夹具里第二条素材静默不入库，报错长得像查询写错了**（`KeyError`） | 跑酷 / BGM 的 `upsert` 按 **sha256 去重**，而夹具里拿一个常量当哈希 | 每条素材派生一个**各不相同**的 sha256；"入库没报错但查不到"先怀疑去重，别先怀疑 SQL | T5.5 |
| 157 | **连按两下「导出」得到同一个目录，第一份被悄悄换掉** | 打包目录名的时间戳只取到**秒**（`[:15]`），而复制一个小文件远不到一秒 | 时间戳取到毫秒（`[:19]`）；这类坑在**所有**以时间戳命名产物的地方都成立 | T5.5 |
| 153 | **占位音色造好了、系统却一个都不用**（每个角色都退回进程音色，而盘上躺着两个能用的） | 占位脚本的目录名（`bear_da` / `bear_xiong`）与 `TaskPayload.voice_map` 的默认值（`bigbear` / `littlebear`）**对不上**。症状不是报错，是 `resolve_voice` 静默走 `fallback` —— **只有翻 manifest 才看得见** | 占位件的名字必须与默认映射**一致**（开箱即用是它唯一的价值）；顺带 `ref.txt` 要**一段一行**、音高按**位次**分而不是按名字里的字。`tests/integration/test_voice_profile.py` 从**默认值**出发断言 `source == voice_map`，接不上就红 | T2.4 |
| 158 | **请求体写错返回 500**，而且**响应里带着刚提交的密钥原文** | `RequestValidationError` handler 把 pydantic `errors()` 原样回显：`ctx` 里的 `ValueError` 对象让 `json.dumps` 抛 `TypeError`（⇒ 500），而 `input` 字段把用户输入抄回响应 | 只保留 `type` / `loc` / `msg`（`ctx` 转字符串），**绝不回显 `input`** | T6.1 |
| 159 | **照抄上游 `requirements.txt` 会把能跑的环境降级** | CosyVoice 上游锁 `torch==2.3.1`，而本项目是 `torch 2.4.0+cu121`（`D:\Torch` 里的 cp311 wheel） | 单独维护 `tts/requirements-cosyvoice.txt`，**只列推理真正用到的**，**不写 torch / torchaudio** | T2.1 |
| 160 | **`pip install openai-whisper` 报 `No module named 'pkg_resources'`**；装上了又 `No module named 'matcha'` | ① whisper 的 `setup.py` 用 `pkg_resources`，而 `setuptools>=81` 已删掉它；② `cosyvoice` 依赖 `third_party/Matcha-TTS` | ① `setuptools<81` **且** `--no-build-isolation`；② `PYTHONPATH` **必须同时含** `D:\ai_models\CosyVoice` **和** `...\third_party\Matcha-TTS` | T2.1 |
| 161 | **`inference_zero_shot` 传张量报错；`torchaudio.save` 报 `Invalid file`** | 该 revision 的第三参是**参考音路径**不是张量；且此环境的 `torchaudio.save` 不可用 | 第三参传**路径**；落盘用 `soundfile.write` | T2.1 |
| 162 | **`tts/.venv` 里 `import studio.core.clock` 抛 `ZoneInfoNotFoundError`**（`No time zone found with key Asia/Shanghai`） | Windows 没有系统 tz 数据库，CPython 要靠 `tzdata` 包；pip **不装也不报缺** | 把 `tzdata` 写进 `tts/requirements-cosyvoice.txt`（已装）。报错点离「少装一个包」隔了**四层 import**，别顺着调用栈查 | T2.2 |
| 163 | **tts 进程活着、`/health` 回 200，可每个请求都报 `No module named 'studio'`** | `tts/pyproject.toml` 是 `package = false` ⇒ 子环境里**没有**本项目；启动器不前置 `PYTHONPATH` 就 import 不到 | `ServiceSpec.env_prepend` 前置 `PYTHONPATH=<仓库>/src`（**前置不覆盖**已有值）；手工起进程要自己加 | T2.2 |
| 164 | **`mypy.ini` 里的 `[[mypy.overrides]]` 段一个字都没生效，却也不报错** | `[[...]]` 是 **TOML** 的数组表写法（`pyproject.toml` 用）；INI 里 mypy **静默忽略**整段 —— 连「未知键」都不提醒 | INI 一律写 `[mypy-<模块>]`（`[mypy-yaml.*]` 这种） | T2.2 |

---

## 5.8 里程碑门禁与验收速查

| 里程碑 | 门禁（可执行） | 关联任务 |
| --- | --- | --- |
| **M1** | 网页端输入定位 + 热点 ⇒ 产出合格稿件（含评分）⇒ 确认闸可见；`启动.bat` 一键拉起全部服务 | T1.1–T1.12 |

> **M1 口径（裁定 108）**：一键启动**已可用**（`api` ready + 其余如实报 `degraded`），但「5 进程全部 ready」**在 M1 阶段不可能达成** —— `tts` 属 T2.2、`voice` 属 T2.6、`draft` 属 T4.11、`render` 属 T3.7（**已落地**）。验收以「未就绪进程被**如实报告**且不阻塞其余进程」为准（P4：宁要真话，不要好看的假绿灯）；「5 进程全 ready」顺延到 M4。**2026-09-17 更新**：`tts`（T2.2）已落地 —— `server_missing` / `env_missing` 两条判据都消解，五个进程的入口、解释器与 `PYTHONPATH` 都齐了，**M1 的「5 进程全 ready」在代码层面已可达成**；剩下的只是一次真机复核（8787/8788 上跑着用户自己起的服务，本轮未动它）。
| **M2** | 一句话用熊大音色读出；杀进程重启后已完成句**引擎调用为 0**；网页可见逐句进度 | T2.1–T2.9 |
| **M3** | 换稿不重剪（**同素材 + 同水印，换稿件直接出片**）；网页一键出新片并在线预览；水印/响度/相似度门禁通过 | T3.1–T3.7 |
| **M4** | ≥3 篇同时推进；中断后恢复；连续 24h 无人干预；全程网页操作 | T4.1–T4.14 |
| **M5** | 成片**定时/即时**自动发布（≥1 平台）+ 数据回流 + **报告生成与决策采纳** + 记忆沉淀闭环（`auto_*.md` 可被解析器消费） | T5.1–T5.8 |

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
uv run pytest tests/integration/test_scheduler.py tests/integration/test_reports.py -q   # 定时调度 / 报告（M5 门槛）
```

**交付物清单（每阶段结束时必须齐备）**

| 类别 | 内容 |
| --- | --- |
| 代码 | `src/studio/**`、`workers/**`、`web/**`、`scripts/**` |
| 契约 | `schemas/*.schema.json`、`tests/contract/**`、`tests/golden/**` |
| 配置 | `config/*.yaml`（含 `persona.yaml` 实例）、`prompts/**`、`templates/**` |
| 文档 | `docs/spec/**`（本规格书）、`docs/adr/**`、`docs/runbook/**`（6+2 个剧本）、`docs/qc/**` |
| 运维 | `启动.bat` / `停止.bat` / `ops/*.ps1` / `scripts/backup_db.ps1` / `scripts/restore_db.ps1` |
