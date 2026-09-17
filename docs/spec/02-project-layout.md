# §02 完整工程目录骨架

> 目录即架构：**分层靠目录表达，跨层调用靠契约约束**。任何模块只能依赖同层或更底层目录，禁止反向 import（由 `ruff` 的 `flake8-tidy-imports` 规则 + CI 强制）。

---

## 2.1 依赖方向（单向）

```
app ──▶ services ──▶ pipeline ──▶ {agents, tts, render, publish, pools} ──▶ domain ──▶ core
                                                                              │
                                                                              ▼
                                                              db（经 repositories / JobStore）
ws ──▶ services（只读查询）        pools ──▶ db.queue（唯一队列入口）
```

**附加约束**

- `domain/` 零外部依赖（纯数据结构 + 状态机 + 时间轴 + 评分算法）；
- `render/`、`tts/`、`agents/`、`publish/` 不 import FastAPI；
- `publish/` 不得 import `render/`（通过产物路径 + `artifacts` 表解耦）；
  **唯一例外**：`publish/precheck.py` 量成片响度时，在**函数内**取
  `render.mixdown` 的 `MixSettings` / `measure_file`。发布门禁与 `quality_json`
  必须量**同一个东西** —— 另写一份 ffmpeg argv 会让「当初写的」与「发布前量的」
  变成两套口径；而把 `MixSettings` 下沉到 `core/` 又会把渲染策略（ducking / 限幅）
  搬进平台层。两边都不划算，故保留函数内导入 + `noqa: PLC0415`；
  **新增 `publish → render` 依赖一律按违规处理**（T5.1）；
- `agents/` 不得 import `tts/`、`render/`、`services/`、`app/`、`ws/`；
  日志出口用 `LogSink` 回调注入（`services.log_service.append`），否则 Agent 会在 Worker 里
  反向拉起整条 API 依赖链（T1.8 裁定 54，契约测试静态拦截）；
- `db/repositories/` 是唯一允许出现 SQL 的地方（队列认领 SQL 在 `db/queue.py`，属例外，需注明 `§3.4`）。

---

## 2.2 目录树（文件级）

```
Fully_Automated_AI_Marketing_Account_Content_Production_Platform\   # = STUDIO_HOME
│
├─ full-execution-plan.md                # 需求权威文档（用户提供，只读）
├─ README.md                             # 项目入口（规格书索引）
├─ pyproject.toml  uv.lock  requirements-tts.lock.txt
├─ ruff.toml  mypy.ini  pytest.ini  .editorconfig  .gitattributes  .env.example  .gitignore
├─ Makefile  tasks.ps1
├─ 启动.bat                              # ✅ T1.12 一键启动（doctor 门禁 ⇒ 5 进程 ⇒ 开浏览器）
├─ 停止.bat                              # ✅ T1.12 优雅关停（标志 ⇒ 信号 ⇒ 强杀）；两者都是**薄壳**
│
├─ docs\
│  ├─ spec\                              # ★ 本规格书（唯一有效版本）
│  ├─ adr\                               # ADR-001..011 全文
│  ├─ runbook\                           # 应急剧本（7 个，含 publish_selector.md）+ tts_concurrency.md（实测标定）
│  └─ qc\                                # 质检阈值（音画同步/响度/相似度）
│
├─ config\
│  ├─ persona.yaml                       # ★ 频道定位（人设/口吻/受众/口癖/禁区）——唯一人工必填；= 人物库的激活副本
│  ├─ personas\                          # ★ 人物库（多套人物并存，`studio persona use <id>` 一键切换）
│  │  ├─ README.md                       #   用法说明
│  │  ├─ persona_default.yaml            #   出厂基线（熊大熊二·MC跑酷）
│  │  └─ solo_commentary.yaml            #   快嘴单人解说（兼作 R2 IP 风险规避方案）
│  ├─ llm.yaml                           # ★ LLM 双通道（§01.2.4）——零密钥，可入库
│  ├─ app.yaml                           # 路径/保留期/超时/auto_approve_policy/一键全自动
│  ├─ pools.yaml                         # 四池并发·租约·退避（§01.4.4）
│  ├─ outputs.yaml                       # 编码 profile（含多平台与 720P 降级档）
│  ├─ randomization.yaml                 # 防搬运随机化：standard / aggressive
│  ├─ publish.yaml                       # ★ 平台/账号/限频/是否需确认/启用开关
│  ├─ logging.yaml
│  ├─ persona.example.yaml               # 人设模板（persona 缺字段时报错打印的就是这个路径）
│  ├─ <name>.local.yaml                  # 个性化覆盖层（深度合并，优先级 env > local > 主文件；已 gitignore）
│  └─ secrets.example.yaml               # LLM Key 占位、WebUI 局域网密码
│
├─ prompts\
│  ├─ manifest.yaml                      # ✅ 提示词注册表（name → file + sha256 + version）
│  │                                     #    已入库；sha256 = sha256_hex(路径+内容) ⇒ 改一字版本就变
│  ├─ shared\{persona_block.md, json_contract.md}   # ✅ T1.8：所有 Agent 的公共注入块
│  ├─ planner\{system.md, user.jinja}              # ✅ T1.9 方向分析（5–8 方向 + 4 条规则）
│  ├─ ideator\{system.md, user.jinja}              # ✅ T1.9 批量选题（每方向 4 个）
│  ├─ feedback_classifier\{system.md, user.jinja}  # ✅ T1.9 反馈分类（情感/想要/吐槽，ref 回抄）
│  ├─ director\{system.md, user.jinja}             # ✅ T1.10 内容导演（钩子/3–5段/CTA/时长预估）
│  ├─ writer\{system.md, user.jinja}               # ✅ T1.10 写稿（600–800 字 + 逐句表）
│  │                                     #   ★ style_* 变体推迟：`style` 目前只进 TaskPayload，
│  │                                     #     模板分块等 T1.11 改稿链路一起接
│  ├─ reviewer\{system.md, user.jinja}             # ✅ T1.11 双通道评分（**只给六维度 + issues**，不给总分）
│  ├─ editor\{system.md, user.jinja}               # ✅ T1.11 改稿（≤2 轮 · 整份回写 · 只改 issues 涉及段落）
│  ├─ cover\{system.md}                            # 封面文案
│  └─ shared\{glossary.yaml, banned_words.yaml, style_guide.md}
│
├─ schemas\                              # JSON Schema（LLM 结构化输出 + IR + manifest）
│  ├─ planner_result.schema.json  ideator_result.schema.json   # ✅ T1.9（planner 方向 5–8 的唯一保证）
│  ├─ feedback_classification.schema.json                      # ✅ T1.9（ref 回抄）
│  ├─ director_result.schema.json                              # ✅ T1.10（段数/字数/时长的唯一保证）
│  ├─ script_result.schema.json                                # ✅ T1.10（body_md **不带字数区间**：
│  │                                     #   越界要走"重写 ≤2 次"的降级路径，不能被 schema 拦掉）
│  ├─ review_result.schema.json                                # ✅ T1.11（**不含** total/grade/verdict：定级是服务端的事）
│  ├─ editor_result.schema.json   cover_result.schema.json     # ✅ T1.11 改稿（内联完整 script 结构 + changes）
│  └─ video_ir.schema.json        render_manifest.schema.json
│
├─ templates\                            # 三层模板（磁盘为源，启动注册进 DB）
│  └─ douyin_9x16_default\
│     ├─ template.yaml                   # Layer 0：画布/片头-主画面-片尾序列/音频总线/输出 profile
│     ├─ scenes\
│     │  ├─ 01_opening.yaml              # 片头（标题+熊大贴图+开场BGM，固定 3s）
│     │  ├─ 02_main.yaml                 # 主画面（字幕+背景视频+配音+贴图，跟随内容）
│     │  └─ 03_ending.yaml               # 片尾（关注引导+收尾BGM）
│     ├─ components\                     # Layer 2：六类组件
│     │  ├─ title.yaml                   # 标题
│     │  ├─ subtitle_main.yaml           # 字幕（自动换行/描边/居中）
│     │  ├─ sticker_bear.yaml            # 贴图（熊大/熊二）
│     │  ├─ bg_mc_parkour.yaml           # 背景视频（通配 parkour_*.mp4，随机抽取）
│     │  ├─ bgm_slot.yaml                # 音乐
│     │  └─ voice_slot.yaml              # 配音
│     └─ assets\{fonts,images,shapes}\
│
├─ models\                               # ★ TTS 模型权重（.gitignore）
│  └─ CosyVoice3-0.5B\                   # 版本待核验（见 §README.13 Q7）
│
├─ data\                                 # 运行数据（.gitignore 大文件）
│  ├─ studio.db (+ -wal/-shm)
│  ├─ backups\                           # 备份根（DB 快照 / 人物回滚点）
│  │  └─ persona\                        #   每次 `persona use` 自动备份切换前的人物
│  ├─ hot\                               # ★ 热点：*.md，每行 `标题|热度|平台`
│  │  └─ archive\                        #   已消费热点归档（90 天）
│  ├─ feedback\                          # ★ 历史反馈：*.md（含 auto_*.md 自动回流）
│  ├─ voice_src\                         # ★ 原声
│  │  ├─ bigbear\{ref_01.wav, ref_02.wav, ref_03.wav, ref.txt, profile.json}
│  │  └─ littlebear\{...}
│  ├─ assets\
│  │  └─ mc_parkour\                     # ★ 我的世界跑酷素材包
│  │     ├─ parkour_001.mp4 ...          #   通配命名，渲染随机抽取
│  │     ├─ index.json                   #   时长/分辨率/可用区间/pHash/授权信息
│  │     └─ .thumbnails\
│  ├─ browser_profile\                   # ★ Playwright 登录态（敏感，禁止备份外传）
│  ├─ output\                            # ★ 交付物
│  │  ├─ topics\<task_id>.json           #   选题卡（方向+大纲+评分+理由）
│  │  ├─ voice\<task_id>\s001.wav ...    #   按句配音
│  │  ├─ videos\{时间戳}_{task_id}_final.mp4
│  │  ├─ videos\{时间戳}_{task_id}_final_720p.mp4     # 降级档
│  │  └─ covers\{时间戳}_{task_id}_cover.jpg
│  ├─ work\<task_id>\                    # 过程物（可 GC）
│  │  ├─ script.json  timeline.json  manifest.json  ir.json
│  │  ├─ tts\voice_master.wav            #   拼接人声（24h TTL）
│  │  ├─ graphs\scene_001.txt ...        #   filter_complex（永久保留）
│  │  ├─ scenes\scene_001.mp4 ...        #   场景中间产物（7d TTL）
│  │  └─ publish\<platform>\             #  发布证据截图（T5.2：<stage>_时刻.png）
│  ├─ cache\{tts\<hash>.wav, phash\, broll_index\}
│  └─ tmp\{graphs\, parts\, ffmpeg_*}    #   指向 D 盘（§README.6）
│
├─ src\studio\
│  ├─ __init__.py  cli.py
│  ├─ core\{config.py, persona_store.py, settings.py, paths.py, logging.py, ids.py, clock.py, errors.py, proto.py, doctor.py, ✅ faults.py, ✅ fonts.py}
│  ├─ db\                                   # ✅ T1.3 / T1.5 已落地（engine + migrate + lease + queue）
│  │  ├─ engine.py  migrate.py               #   ✅ 连接工厂/PRAGMA 唯一真相 · 迁移器/库自检
│  │  ├─ session.py                           #   ✅ T1.7 每线程一条连接（sqlite3 线程亲和 vs 线程池）
│  │  ├─ write_queue.py
│  │  ├─ models.py                           #   ✅ T1.9 行映射 dataclass（JSON 列就地解析；坏 JSON 返回 [] 不抛）
│  │  ├─ queue.py  lease.py                  #   ✅ T1.5 四池队列内核（DB-as-Queue + 租约 + 死信 + 限频）
│  │  ├─ migrations\0000_pragmas.sql  0001_init.sql ... 0006_seed.sql
│  │  │                                      #   30 表 / 61 索引 / 6 触发器（§03.7.1）
│  │  └─ repositories\{✅hot, ✅feedback, ✅direction, ✅topic, ✅script, ✅review, ✅approval,
│  │                   ✅audit, task, job, ✅sentence, ✅artifact, template, log, broll,
│  │                   persona, ✅publication, event, voice}_repo.py
│  │                                     #   ✅ T5.3 `publication_repo.py`：`publications` 的**唯一写入者**
│  │                                     #      （幂等 `create` 返回 `(row, created)`；`mark_failed` /
│  │                                     #      `mark_manual_required` 各自记一次尝试；`mark_dry_run`
│  │                                     #      回 `queued` 且不占额度 —— 见 §06.5.4）
│  │                                     #   ★ 唯一允许出现 SQL 的地方；id 由仓储自派 ULID
│  ├─ domain\                            # ✅ T1.4 已落地（枚举 / 状态机 / 契约 / 唯一写入口）
│  │  ├─ enums.py                        # ✅ 16 态 TaskStatus + Grade + PoolName(4) + TaskPool(5) + ...
│  │  ├─ state_machine.py                # ✅ ALLOWED_TRANSITIONS 16×16（纯函数，无 I/O）
│  │  ├─ errors.py  models.py            # ✅ IllegalTransition / ConcurrentModification · TaskRead
│  │  ├─ task_service.py                 # ✅ ★ tasks 表的唯一写入口（乐观锁 + task_events）
│  │  ├─ events.py  policies.py
│  │  ├─ scoring.py                      # ✅ T1.11 双通道评分 + A/B/C 分级 + GateAction/Decision（唯一实现）
│  │  ├─ timeline.py                     # 音频时间轴（唯一真相）—— **一期落在 `tts/timeline.py`**（理由见该处）
│  │  ├─ topics.py                       # ✅ T1.9 选题领域：归一化/哈希/相似度/词频/四规则/两级去重/分类契约
│  │  ├─ script.py                       # ✅ T1.10 写稿领域：ScriptRules（persona 可覆盖）/大纲与成稿契约/
│  │  │                                  #   check_outline·check_script·build_draft·rewrite_hint·时长估算
│  │  ├─ text.py                         # ✅ T1.10 分句/切分/字数（CJK 逐字计 1、拉丁词计 1、标点不计）
│  │  ├─ ✅ cover.py                      # ✅ T5.1 封面领域（§06.3）：换行 / 高亮分段 / 字号收放 /
│  │                                     #      描边与压暗带参数 / 落位 —— 全是纯函数；
│  │                                     #      真正碰 ffmpeg 的那半边在 `publish/cover.py`
│  │  └─ ✅ publish.py                      # ✅ T5.2 发布领域（§06.2.2 · §06.5.3）—— 纯函数，零 I/O：
│  │                                        #      幂等键（task|platform|account 的 sha256）/ 文案裁剪
│  │                                        #      **只报不改** / 话题按语法拼 / 回读比对**说清吞法**
│  ├─ agents\                            # ✅ T1.8 / T1.9 已落地（网关 + 记账 + 预算 + 熔断 + 提示词库 + 3 Agent）
│  │  ├─ base.py  llm_client.py  json_guard.py  prompts.py  cost.py  budget.py
│  │  ├─ circuit.py                      # ✅ 熔断（连续失败 ≥3 ⇒ OPEN 60s；半开放一个探针）
│  │  ├─ gateway.py                      # ✅ 决策链：预算 → 通道 → 传输重试 → 修复重试 → 记账 → 熔断
│  │  ├─ gateway_factory.py              # ✅ T1.9 composition root：build_gateway(connection/llm/paths/transport/settings/log/env)
│  │  ├─ planner.py  ideator.py  feedback_classifier.py   # ✅ T1.9（persona 变量 + 公共块自动注入）
│  │  ├─ director.py  writer.py          # ✅ T1.10（规则闸 + 重写 ≤2 + 降级不抛裸异常）
│  │  ├─ reviewer.py  editor.py  cover.py   # ✅ T1.11 / ✅ T1.11 / ✅ T5.1
│  │  └─ tools\                          # 计划中的 dedup/trend_source/feedback_digest/topic_history
│  │                                     #   ★ T1.9 落地时收敛为 domain/topics.py + db/repositories/（不再单列）
│  ├─ tts\
│  │  ├─ base.py  client.py  cosyvoice_engine.py  engine_router.py  fallback.py
│  │  │                                 #   T2.2/T2.3/T2.4 待落地（E5 权重）
│  │  ├─ ✅ text_normalize.py  ✅ segmenter.py  ✅ cache.py
│  │  │                                 #   T2.5 归一化与切分 / T2.6 句级缓存（内容寻址 + 旁挂元数据 + 降权淘汰）
│  │  ├─ ✅ sapi.py  ✅ sentence.py  ✅ synth.py
│  │  │                                 #   T2.6 `SentenceEngine` 最小缝（Windows SAPI 引擎 / 一句合成 /
│  │  │                                 #       缓存键 / 占位静音）；`synth.py` 是 T1.12 那条一次性配音
│  │  │                                 #       （逐句合成 → concat 母带），T2.8 起由池 + `timeline.py` 接手
│  │  ├─ ✅ faults.py                    #   ★ T2.8 把 `STUDIO_FAULT` 的计划套在**引擎缝**上（降级演练）：
│  │  │                                 #      注入点必须落在引擎层 —— 池那侧一行不改，走的才是
│  │  │                                 #      "失败 ⇒ 记账 ⇒ 退避重试 ⇒ 到线降级"那条真路径；包装后的
│  │  │                                 #      引擎名**每场演练都不同**（否则第二场命中第一场的缓存，
│  │  │                                 #      引擎一次都不被调到 ⇒ 演练全绿但什么都没发生，陷阱 #111）
│  │  ├─ ✅ timeline.py                  #   ★ T2.7 时长时间轴（§04.2.7）：逐句实测 → 停顿抖动 → start/end →
│  │  │                                 #      `timeline.json`；并负责把逐句 WAV 拼成 `voice_master.wav`
│  │  │                                 #      （**规格树里写的是 `domain/timeline.py`**：那份"真相"有一半是
│  │  │                                 #       I/O（ffmpeg + 落盘），而 `domain/` 是纯函数；纯累加那部分与
│  │  │                                 #       拼接永远一起用，拆开只会多一层没人受益的间接。T3.3 若需要
│  │  │                                 #       强类型模型，再把 dataclass 搬去 `domain/timeline.py`）
│  │  └─ server.py                      #   T2.2 常驻推理服务（待落地）
│  ├─ render\                          # ✅ 一期已落地（单遍合成；ir/randomizer/builder/compiler
│  │  │                                 #    /graph/filters/scene_builder/final_builder 是二期三层模板的，
│  │  │                                 #    见 T3-P1..T3-P4 —— 一期不建空壳）
│  │  ├─ profiles.py                     #   ✅ T3.2 profile → 编码 argv（抽象 quality 落 crf/cq）
│  │  ├─ watermark.py                    #   ✅ T3.2 水印规划（**可选装饰**：缺失跳过，不阻塞出片）
│  │  ├─ assets.py                       #   ✅ T3.3 挑底片 / BGM（挑不到 ⇒ None ⇒ 黑屏降级）
│  │  ├─ subtitle.py                     #   ✅ T3.5 句级时长 → ASS（字体缺失就跳过）
│  │  ├─ mixdown.py                      #   ✅ T3.6 侧链 ducking + 两遍 loudnorm + alimiter
│  │  │                                  #      + `measure_file`（T3.7：量**落盘的成片**，QC 用）
│  │  ├─ composite.py                    #   ✅ T3.4 单遍合成：argv + 滤镜图 + .partial 原子改名
│  │  ├─ hashing.py                      #   ✅ T3.7 `composite_hash`（§04.2.8.7，排除输出路径/线程数）
│  │  └─ degrade.py                      #   ✅ T3.7 降级链：正常档 → 720P 保底档（只换一次）
│  ├─ publish\                           # ★ 第六部分（✅ T5.1 封面/预检 · ✅ T5.2 发布适配层）
│  │  ├─ ✅ base.py                       #   ✅ T5.2 `Publisher` ABC（§4.6.1）：三个抽象方法
│  │  │                                  #      `health`/`publish`/`fetch_metrics` + `PublisherContext`
│  │  │                                  #      / `PublishRequest` / `PublishResult` / `PublishEvidence`
│  │  │                                  #      / `PublishHealth` + `PUBLISHERS` 注册表（存**类**，
│  │  │                                  #      实例化按账号做 —— 两个账号两份登录态，§06.2.4）
│  │  ├─ ✅ playwright_publisher.py       #   ✅ T5.2 §06.5.3 八步的通用实现（`dry_run=True`
│  │  │                                  #      停在第 ⑥ 步之前）；平台子类只给选择器与代号
│  │  ├─ ✅ browser.py                    #   ✅ T5.2 `PageLike` Protocol（发布器对浏览器的全部要求）
│  │  │                                  #      + 持久化 profile 会话；playwright 只在函数内 import
│  │  ├─ ✅ selectors.py                  #   ✅ T5.2 选择器集中化：**装配期**就校验（缺项/版本/readback
│  │  │                                  #      取值错 ⇒ 当场 `PUBLISH_SELECTOR_MISS`，不推迟到真机）
│  │  ├─ ✅ selectors\{douyin,kuaishou,shipinhao,fixture}.yaml   # 页面选择器（可热修，R13）
│  │  ├─ ✅ platforms\{douyin,kuaishou,shipinhao}.py    # 一线真实现（只声明 `platform`）
│  │  ├─ ✅ platforms\{xiaohongshu,bilibili,xigua,weibo}.py     # 二线：接口在、实现空（Q9）
│  │  ├─ ✅ platforms\fixture.py          #   本地靶页发布器（**自己就拒绝 `dry_run=False`**）
│  │  ├─ ✅ fixtures\upload_form.html     #   靶页本体：真浏览器演练的靶子，点发布会回打本地服务器
│  │  ├─ manual_queue.py                 #   待人工发布兜底
│  │  ├─ ✅ cover.py                      #   ✅ T5.1 封面生成（§06.3）：抽帧 → 缩放 → 量字 → drawtext
│  │  │                                  #      三层拆分：`domain/cover.py` 算怎么摆（纯函数）、
│  │  │                                  #      本文件碰 ffmpeg、`agents/cover.py` 出文案
│  │  ├─ ✅ precheck.py                   #   ✅ T5.1 发布前二次校验（§06.4）：字幕/水印/响度/禁区四道门禁
│  │  │                                  #      —— **只判定不发布**；判据读 `config/publish.yaml`
│  │  ├─ ✅ ratelimit.py                  #   ✅ T5.3 发布限频（§03.4.4 ⑥）：把 `JobStore.rate_limit_state`
│  │  │                                  #      的结论翻成"到几点再来"（次日零点 + `blake2s` 确定性抖动，
│  │  │                                  #      见 §4.6.7）。**只算策略，不数数**（计数要读表 ⇒ 在 db 层）
│  │  ├─ metrics.py                      #   数据回收（播放/点赞/评论/分享）
│  │  ├─ memory.py                       #   记忆沉淀（回流 feedback + 选题降权）
│  │  └─ handoff.py                      #   ★ HandoffAdapter（对接外部制片台，A2）
│  ├─ pipeline\                          # ⏸ **一期为空**（只有 stages\.gitkeep）：
│  │  │                                  #   按 §02.1 的箭头 services → pipeline → {…}，
│  │  │                                  #   pipeline **不得 import services**，而一期那条主线的编排
│  │  │                                  #   主体就是调服务 ⇒ 落在 `services/pipeline_service.py`。
│  │  │                                  #   二期三层模板的场景级编排（编排的是 render 内部的东西）
│  │  │                                  #   才该住在这里 —— 现在建空壳只会长出一层没人受益的间接
│  │  ├─ orchestrator.py                 #   T3-P4（二期）
│  │  ├─ stages\{plan, topic, draft, review, gate, voice, render, finalize,
│  │  │          publish, recycle}.py
│  │  └─ gates.py  retry.py  hooks.py
│  ├─ pools\
│  │  ├─ heartbeat.py  worker_base.py  supervisor.py  runner.py   # ✅ T1.6 已落地
│  │  ├─ ✅ draft_worker.py  ✅ render_worker.py  ✅ voice_worker.py
│  │  │                                       # 单元处理器：T4.11 写稿 / T3.7 `render/final` / T2.6 `voice/sentence`
│  │  │                                       #   （文案 ⇒ 逐句配音 ⇒ 合成 ⇒ QC 回填；进度落 `jobs.result_json`）
│  │  │                                       #   `voice_worker` 的三条纪律与渲染池逐条对应：不自己收尾 /
│  │  │                                       #   可重入 / 不在单元里开池（缓存·引擎·音色装配期一次装好）
│  │  └─ ✅ publish_worker.py             # ✅ T5.3 `publish/publish`：限频（不过 ⇒ `UnitDeferred`，
│  │                                      #   回 `pending` 且**不计 attempts**）⇒ 幂等登记 ⇒ 登录态探测
│  │                                      #   ⇒ §06.5.3 八步 ⇒ 落 published/failed/manual_required。
│  │                                      #   判"重试还是转人工"用的是**错误码 + 剩余次数**，不是照抄
│  │                                      #   发布器给的状态（发布器不知道还剩几次机会）—— 见 §4.6.7
│  ├─ services\
│  │  ├─ service_manager.py                   #   ✅ T1.12 五进程编排：readiness / start / stop / status
│  │  │                                       #      规格表 + PID 台账 + 三级关停时序（裁定 102/104/107）
│  │  ├─ input_service.py                     #   ✅ T1.9 热点/反馈解析（纯函数）+ 导入/归档（**解析阶段禁调 LLM**）
│  │  ├─ topic_service.py                     #   ✅ T1.9 选题编排：run_planner / run_ideator / 分类 / 共享池去重 / 批次归档
│  │  ├─ log_service.py                       #   ✅ T1.7 `system_logs` 唯一应用层写入口（先落库再唤醒 Hub）
│  │  ├─ script_service.py                    #   ✅ T1.10 写稿编排：选题 ⇒ 建任务 ⇒ Director ⇒ Writer ⇒ 逐句落库
│  │  ├─ review_service.py                    #   ✅ T1.11 审稿编排 + Editor 循环 + **确认闸决断 decide_approval**
│  │  ├─ ✅ voice_service.py              #   T2.7 配音收口 `settle_voice`：全部句定局 → 逐句实测 →
│  │  │                                  #      拼母带 → 写 `timeline.json` → 回写 start/end → 登记 artifacts
│  │  │                                  #      （**不改任务状态**：`voicing → queued_render` 归 T2.8 的编排）
│  │  │                                  #   ✅ T2.8 `enqueue_sentences`：待办 = `pending`/`failed` 的句子，
│  │  │                                  #      走 `JobStore.enqueue` 幂等投递（`synthesizing` 不算待办 —— 那是
│  │  │                                  #      sweeper 的活）
│  │  │                                  #   ✅ T2.9 操作面：`resynth_sentence`（业务表 + 作业表一起改）、
│  │  │                                  #      `set_voice_map`（映射 + 失效 + 作业 payload 三处一起改，
│  │  │                                  #      缺 confirm 先抛"将重配 N 句"）、`preview_audio`（试听**不触发合成**）、
│  │  │                                  #      `voice_payloads` / `resolve_voice` / `usable_voices`（音色解析）
│  │  ├─ task_service.py  render_service.py
│  │  │                                  #   ✅ T3.x `render_service`：文案 → 配音 → 合成 → manifest
│  │  │                                  #      + `quality_report`（T3.7，成片实测 → quality_json）
│  │  ├─ render_job_service.py           #   ✅ T4.6 出片任务登记表（进程内串行；T3.7 起回填 quality_json）
│  │  ├─ pipeline_service.py             #   ✅ T3.7 编排：把**一个任务**从当前状态推到 `--until`
│  │  │                                  #      （**为什么不在 `pipeline/` 里**：§02.1 的箭头是
│  │  │                                  #       `services → pipeline`，pipeline 不得 import services，
│  │  │                                  #       而这段编排的主体就是调服务）
│  │  │                                  #   ✅ T2.8 配音阶段拆两步：`queued_voice --投递--> voicing
│  │  │                                  #      --排空+收口--> queued_render`。`voicing` 因此是真的停得住的
│  │  │                                  #      落点（`rendering` 不收进 `SUPPORTED_UNTIL`）。排空**就地借一条
│  │  │                                  #      worker**（不要求先把常驻池起起来），空转等一拍、收工判据是
│  │  │                                  #      `SentenceProgress.is_settled`
│  │  ├─ publish_service.py  asset_service.py  template_service.py
│  │  │                                       #   ✅ T5.1 `publish_service`：封面（`make_cover`）与
│  │  │                                       #      发布前校验（`precheck`）两条只读/半只读链路
│  │  │                                       #   ✅ T5.2 `dry_run`：真 Playwright 走完 §06.5.3 前七步、
│  │  │                                       #      停在第 ⑥ 步之前（`--target fixture` 打本地靶页）
│  │  ├─ approval_service.py                  #   T4.4 REST 薄壳（规则已在 review_service.decide_approval）
│  │  └─ metrics_service.py  gc_service.py  audit_service.py
│  ├─ ws\                                    # ✅ T1.7 已落地（只依赖 core / services，不碰 db）
│  │  ├─ protocol.py  coalescer.py  backpressure.py
│  │  ├─ hub.py  snapshots.py
│  │  └─ __init__.py
│  └─ app\                                   # ✅ T1.7 骨架已落地（create_app + lifespan + AppState）
│     ├─ main.py  lifespan.py  deps.py       #   ✅ 建应用 / 起停 Hub / 组装连接池 + LogService + 快照 provider
│     │                                       #   ✅ T1.12：`run_server()` 被 `studio serve` 与 `workers/run_api.py` **共用一份**
│     ├─ routers\{✅ health, ✅ logs, ✅ ws, ✅ voice, overview, topics, hot, feedback, tasks,
│     │            scripts, sentences, voices, templates, assets, renders, ✅publish, metrics,
│     │            pools, audit}.py
│     │                                       #   ✅ T2.9 `voice.py`：配音操作面五个端点。**名字与计划里的
│     │                                       #      `sentences.py` / `voices.py` 合并成一个** —— 它们服务的是
│     │                                       #      同一块面板（逐句状态 / 试听 / 重配 / 换音色），拆开会让
│     │                                       #      "这一句现在能不能重配"这条判据出现两处
│     ├─ schemas\*.py                          #   ✅ T2.9 `schemas/voice.py`（逐句视图 / 音色选项 / 换音色请求与报告）
│     │                                        #   ✅ T5.3 `schemas/publish.py`（发布记录视图 / 待人工队列 /
│     │                                        #      投递与处置请求；`can_retry`/`can_cancel`/`can_mark_done`
│     │                                        #      由**服务端**算 —— 状态机规则只该有一处）
│     └─ middleware\{request_id.py, access_log.py, errors.py, auth.py, audit.py}
│
├─ tts\                                  # ★ TTS 运行时子项目（独立 3.11 venv，uv 托管）
│  ├─ pyproject.toml                     #   studio-tts（package=false，find-links=D:/Torch）
│  ├─ uv.lock                            #   （.gitignore）
│  └─ .venv\                             #   Python 3.11（torch 2.4.0+cu121 / CUDA 12.1）
│
├─ web\                                  # 前端（8+1 面板）· ✅ T4.1 骨架已落地
│  ├─ ✅ package.json                    #   scripts：dev/build/preview/typecheck/test/gen:api/size/verify
│  ├─ ✅ vite.config.ts                  #   /api + /ws 代理到 8787；`defineConfig` 取自 `vitest/config`（陷阱 #58）
│  ├─ ✅ tsconfig.json                   #   strict + noUnusedLocals + verbatimModuleSyntax + `@/*` 别名
│  ├─ ✅ index.html  ✅ .npmrc           #   `.npmrc` 兜底 npm 缓存路径（裁定 111）
│  ├─ ✅ openapi.json                    #   ★ 生成物（后端 OpenAPI 快照）—— 禁止手改
│  ├─ ✅ scripts\check-dist-size.mjs    #   产物体积门禁（< 3 MB）
│  └─ src\
│     ├─ ✅ api\{http.ts, types.gen.ts, http.test.ts}
│     │                                 #   http.ts 是**全前端唯一允许出现 `fetch(` 的地方**（陷阱 #59 / 契约测试拦截）
│     ├─ ✅ api\endpoints\{health, logs, overview, topics, scripts, approvals, outputs, assets,
│     │                    pools, persona, metrics, audit, watchdog, render, voice}.ts
│     │                                 #   一个面板一份出口；请求/响应类型一律从 types.gen.ts 取，不手写形状
│     │                                 #   voice.ts（T4.5）：sentences / voices / resynth / voice_map 四个端点
│     ├─ ✅ ws\{client.ts, reconnect.ts, events.ts}
│     │                                 #   events.ts 为生成物；reconnect.ts 放纯策略（退避/缺口/URL）供单测
│     ├─ ✅ stores\{ui, overview, logs, topics, scripts, outputs, assets, pools, persona,
│     │             metrics, audit, render, voice}.ts
│     │                                 #   logs.ts（T4.9）：过滤/搜索/告警/补洞/导出
│     │                                 #   render.ts（T4.6）/ voice.ts（T4.5）：注入点 + 纯函数 + 轮询
│     │                                 #   voice.ts 另有二次确认流（409 ⇒ 确认框 ⇒ 带 confirm 重发）
│     │                                 #   ui.ts（T4.14）：四屏跳转（goTo / takeHandoff）+ 纯函数（配套 ui.test.ts 14 例）
│     ├─ ✅ composables\{useWsConnection, useTaskStream}.ts
│     ├─ ✅ components\{AppButton, StatusDot, PanelCard, LogStream, EmptyState, GradeBadge,
│     │                  SentenceDiffList}.vue  ✅ components\tone.ts
│     ├─ ✅ utils\{download, highlight}.ts
│     │                                 #   隔离 DOM（下载）与不可信文本渲染（高亮）
│     ├─ ✅ styles\{tokens.css, base.css}   #   tokens.css 是**唯一**颜色/间距/字号来源
│     ├─ ✅ views\{Overview, Topics, Scripts, Voices, Renders, Outputs, Assets, Logs, Pools,
│     │              Metrics, Audit, Personas}.vue
│     │                                 #   Voices.vue（T4.5）：逐句进度条 + 逐句表（试听/重配）+ 音色映射 + 确认框
│     │                                 #   T4.14：Topics / Scripts / Voices / Renders 四屏各带「去下一屏 →」，跳转语义在 stores/ui.ts
│     ├─ ✅ App.vue  ✅ main.ts  ✅ env.d.ts
│     └─ （待 T5.x）views\Publish.vue  stores\publish.ts
│
├─ workers\{✅ run_api.py, ✅ run_tts.py, ✅ run_draft.py, ✅ run_voice.py, ✅ run_render.py,
│           ✅ run_publish.py, supervisor.py}
│                                        #   ✅ T1.12：五个入口全部在位（`run_*` = `run_entry()` 薄壳）
│                                        #      `run_draft.py`（T4.11）、`run_voice.py`（T2.6）、`run_render.py`（T3.7）
│                                        #      与 `run_publish.py`（T5.3）已注册真实 handler ⇒ **四个池都齐了**；
│                                        #      未注册的池 ⇒ **报错退出**，不静默起空转 worker（裁定 103）
│                                        #      ⚠️ `publish` **不在** `service_manager.SERVICE_NAMES` 里
│                                        #      （六个进程是另一件事）：发布进程随 T5.5 发布面板一起接进
│                                        #      supervisor —— 出厂 `publish.enabled=false`，常驻发布 worker
│                                        #      在开关关着时唯一会做的事是把投递进来的作业标成 `PUBLISH_DISABLED`
│                                        #      `run_voice.py` 在装配期解析音色（列音色要起 PowerShell，1–2 秒）：
│                                        #      解析不到 ⇒ 启动即报 `TTS_ENGINE_UNAVAILABLE`（裁定 214）
│                                        #      `run_tts.py` 在 `studio.tts.server` 落地（T2.2）前抛
│                                        #      `TTS_ENGINE_UNAVAILABLE` ⇒ 管理器报 degraded、**不拉起**（裁定 103）
│
├─ scripts\
│  ├─ env.ps1  doctor.ps1  smoke_cosyvoice.py  bench_tts.py  bench_render.py
│  ├─ ✅ audio_qc.py                     #   ✅ T3.7 响度/真峰值 QC（判据读 publish.yaml → precheck）
│  ├─ av_sync_audit.py  dup_audit.py     #   ⏸ 一期不做（C12 诊断 / §04.2.4.5 相似度，见 T3.7）
│  ├─ ingest_mc_parkour.py  ingest_voice_src.py
│  ├─ parse_hot.py  parse_feedback.py
│  ├─ gen_fixture_media.py  gc_media.py
│  └─ backup_db.ps1  restore_db.ps1
│
├─ ops\
│  ├─ ✅ start_all.ps1  ✅ stop_all.ps1  ✅ status.ps1  healthcheck.ps1
│  │                                     #   ✅ T1.12：前三个是**薄壳** —— dot-source `scripts\env.ps1`
│  │                                     #      后调 `studio service {start,stop,status}`；
│  │                                     #      关停时序住在 Python（裁定 104：能测才敢改）
│  └─ install_services.ps1              # nssm 注册 6 个进程（可选）
│
└─ tests\
   ├─ __init__.py                            # ★ 测试包树（跨模块复用假件 ⇒ 见 §2.4）
   ├─ conftest.py
   ├─ unit\{core,domain,db,pools,✅ ws,tts,render,agents,✅ services,pipeline,✅ publish}\
   │                                          # unit/ws · unit/domain · unit/services 带 __init__.py
   │                                          # ✅ T1.12：unit/services/test_service_manager.py（54 例，全假进程表）
   ├─ integration\{✅ test_queue_lease, ✅ test_worker_lifecycle, ✅ test_ws_replay,
   │                test_sentence_resume, test_timeline, test_runner, test_mixdown,
   │                test_pool_control, test_voice_profile, test_broll_ingest,
   │                test_topic_pool, ✅ test_script_pipeline, ✅ test_scoring, ✅ test_publish_dryrun,
   │                test_publish_pool, test_metrics_recycle}.py
   ├─ contract\{✅ test_planner_schema, ✅ test_ideator_schema, ✅ test_director_schema,
   │             ✅ test_script_schema, ✅ test_review_schema, ✅ test_editor_schema, test_ir_schema,
   │             test_manifest_schema, ✅ test_ws_protocol, ✅ test_publisher_abc,
   │             ✅ test_no_direct_job_write, ✅ test_no_direct_task_write,
   │             ✅ test_no_direct_heartbeat_write}.py
   ├─ e2e\{test_gate_to_voicing, ✅ test_approval_flow, test_sentence_edit,
   │        test_render_e2e, test_unattended, test_topic_flow, test_full_chain}.py
   ├─ fixtures\{scripts\, audio\, clips\, hot\, feedback\}
   └─ golden\{filtergraph\*.txt, ir\*.json, ass\*.ass, manifest\*.json, scoring\*.json}
```

> **测试包树**：`tests/unit/ws/` 是唯一需要跨模块复用假件的目录（`helpers.py` 被 unit 与
> integration 共用）⇒ `tests/` → `tests/unit/` → `tests/unit/ws/` 这一条链路必须有 `__init__.py`。
> 缺了它，mypy 会把同一文件按「目录短名」与「`tests.*` 全名」各看一遍，报
> `Source file found twice under different module names`，**并跳过该文件、掩盖它下面所有真实类型错误**。
>
> **同名测试文件也要包标记（T1.11 裁定 101）**：`tests/unit/domain/test_scoring.py` 与
> `tests/integration/test_scoring.py` 重名，两边都不带 `__init__.py` 时 pytest 会按顶层模块
> `test_scoring` 各收集一次 ⇒ `import file mismatch` 直接中断整轮收集。
> 因此 `tests/unit/domain/__init__.py` 也是**必需**的（与 `tests/unit/services/` 同一条理由）。
---

## 2.3 目录设计裁决（原文附录 × 工程化要求 的融合）

原文附录给出的是**扁平、按类型分**的目录；工程化要求**按任务隔离**（多任务并行 + 可 GC + 可审计）。融合裁决：

| 原文附录路径 | 语义 | 采纳方式 | 理由 |
| --- | --- | --- | --- |
| `config/persona.yaml` | 频道定位（唯一人工必填） | ✅ **原样采纳** | 语义清晰 |
| （原文未定义） | 多套人物并存、随时换人 | ➕ **新增** `config/personas/<id>.yaml` 人物库 + `config/persona.yaml` 作激活副本 | "随时可能改人物" ⇒ 必须可切换、可回滚；单文件方案改坏了无从回退 |
| `config/llm.yaml` | 模型/密钥配置 | ✅ **原样采纳** | 密钥走环境变量 |
| `prompts/` | 各 Agent 提示词模板 | ✅ **采纳并按 Agent 细分子目录** | 8 个 Agent 各自独立 |
| `templates/` | 视频模板定义（YAML） | ✅ **原样采纳** | — |
| `data/feedback/` | 历史反馈 | ✅ **原样采纳** | — |
| `data/hot/` | 热点信息 | ✅ **原样采纳** | — |
| `data/voice_src/` | 熊大熊二原声 | ✅ **原样采纳** | — |
| `data/assets/mc_parkour/` | 跑酷素材包 | ✅ **原样采纳** | — |
| `models/CosyVoice3-0.5B/` | TTS 权重 | ✅ **原样采纳** | — |
| `data/output/topics/` | 选题成果 | ⚠️ **加一层 task_id**：`topics/<task_id>.json` | 平铺会互相覆盖 |
| `data/output/voice/句子序号.wav` | 按句配音 | ⚠️ **加一层 task_id**：`voice/<task_id>/s001.wav` | **必须隔离**：多任务并行时 `001.wav` 必然冲突（四池并行的前提） |
| `data/output/videos/{时间戳}_final.mp4` | 最终成片 | ✅ **采纳格式 + 加 task_id** | 成片库平铺可浏览（原文意图）；加 ID 保证唯一且可溯源 |
| （原文未定义） | 中间产物放哪 | ➕ **新增** `data/work/<task_id>/` | 场景/滤镜图/时间轴必须按任务隔离且可 GC；**不污染 `output/`**（output 是交付物，work 是过程物） |

**融合原则**

1. **交付物**（topics / voice / videos / covers）→ `data/output/`，命名**尽量贴近原文**，仅在有冲突风险处加 `task_id`。
2. **过程物**（场景、滤镜图、时间轴、manifest、IR）→ `data/work/<task_id>/`，**可按 TTL 回收**。
3. **输入源**（hot / feedback / voice_src / assets）→ 完全按原文。

---

## 2.4 命名与组织约定

| 对象 | 约定 | 示例 |
| --- | --- | --- |
| 任务目录 | `data/work/<task_id>`，`task_id` 为 **ULID**（字典序即时间序） | `01J9Z3K7...` |
| 句子音频 | `s%03d.wav`（原文"句子序号.wav"的实现，3 位补零保证排序） | `s007.wav` |
| 场景产物 | `scene_%03d.mp4` | `scene_001.mp4` |
| 滤镜图 | `graphs/<unit>_<seq>.txt` | `scene_001.txt` |
| 成片 | `{yyyymmdd-HHMMSS}_{task_id}_final.mp4`；降级档加 `_720p` | `20260913-143022_01J9Z..._final.mp4` |
| 封面 | `{yyyymmdd-HHMMSS}_{task_id}_cover.jpg` | — |
| 模板 id | `tpl_<平台>_<比例>_<风格>` | `tpl_douyin_9x16_default` |
| 组件 id | `<用途>_<变体>`（对齐原文六类） | `title`、`subtitle_main`、`sticker_bear`、`bg_mc_parkour`、`bgm_slot`、`voice_slot` |
| 音色 id | `bigbear` / `littlebear`（**按原文**） | — |
| 热点文件 | `data/hot/<任意名>.md`，每行 `标题\|热度\|平台` | `20260913.md` |
| 反馈文件 | `data/feedback/<任意名>.md`（含自动回流 `auto_YYYYMM.md`） | `douyin_comments.md` |
| 跑酷素材 | `parkour_*.mp4`（**通配约定**，渲染随机抽取） | `parkour_017.mp4` |
| 平台代号 | 小写英文，与 `publish/platforms/*.py` 一致 | `douyin`、`kuaishou`、`shipinhao` |
| 配置键 | `snake_case`；时间单位带后缀 `_ms`/`_sec` | `lease_sec: 90` |
| 错误码 | 大写下划线，前缀标明子系统 | `TTS_OOM`、`PUBLISH_LOGIN_EXPIRED`、`RENDER_FILTER_SYNTAX` |
| 环境变量 | `STUDIO_*` 前缀 | `STUDIO_DATA_DIR` |
| 迁移文件 | `000N_*.sql`（只增不改） | `0002_v2_alignment.sql` |
| 人物 id | `^[a-z0-9][a-z0-9_-]{0,63}$`，且**必须等于**人物库文件名 | `persona_default`、`solo_commentary` |
| 人物库文件 | `config/personas/<id>.yaml` | `personas/persona_default.yaml` |
| 人物备份 | `data/backups/persona/{yyyymmdd-HHMMSS}_{切换前 id}.yaml` | `20260913-165049_solo_commentary.yaml` |
| 测试包 | 需要被其它测试模块 import 的测试目录，**其链路必须有 `__init__.py`**（模块名唯一） | `tests/unit/ws/helpers.py` |

---

## 2.5 `.gitignore` 关键条目

```gitignore
data/                                 # 运行数据（DB/媒资/缓存/登录态）
data/browser_profile/                 # ★ 登录态，绝对禁止入库
models/                               # ★ 模型权重（GB 级）
.venv/  venv/  tts/.venv/  tts/uv.lock   # 双 venv（app 3.12 / tts 3.11）
config/secrets.yaml  config/secrets.*.yaml
config/*.local.yaml                   # 个性化覆盖层（llm.yaml 零密钥 ⇒ 入库；example 文件显式例外）
data/assets/mc_parkour/*.mp4          # 大素材不入库（index.json 必须入库）
data/voice_src/**/*.wav               # 原声不入库（ref.txt 与 profile.json 入库）
data/output/**
*.partial.mp4
web/node_modules/  web/dist/
logs/
```

> **可复现性权衡**：媒资不入 git，但 `data/assets/mc_parkour/index.json`、`data/voice_src/*/profile.json`、`templates/**`、`prompts/manifest.yaml` **必须入库** —— 这样换机后可判断缺失项，并保证历史产物的 `render_hash` 可校验。

---

## 2.6 各目录"谁写谁读"矩阵

| 目录 | 写入方 | 读取方 | 备注 |
| --- | --- | --- | --- |
| `config/persona.yaml` | **人工（唯一必填）** / `studio persona use <id>` | planner/ideator/director/writer/reviewer | 变更即提示"影响后续稿件风格"；**热重载**，无需重启进程 |
| `config/personas/*.yaml` | 人工 / `studio persona save-as <id>` | `studio persona list` / `activate` | 人物库；文件内 `id` 必须与文件名一致，否则标记为**无效** |
| `data/backups/persona/` | `PersonaStore.activate()` | 人工（回滚） | 每次切人自动备份**切换前**的人物，可随时切回 |
| `config/llm.yaml` | 人工 | llm_client | 密钥走环境变量 |
| `data/hot/*.md` | 人工粘贴 / `parse_hot.py` | planner / ideator | 解析失败行跳过并记 warn |
| `data/feedback/*.md` | 人工 / `parse_feedback.py` / **发布回流（T5.5）** | planner / reviewer | 自动回流文件带 `auto_` 前缀 |
| `data/voice_src/**` | 人工 / `ingest_voice_src.py` | TTS 服务（注册音色） | 质量校验：时长/信噪比/单发言人/无削波 |
| `data/assets/mc_parkour/**` | 人工 / `ingest_mc_parkour.py` | randomizer / render | `license` 缺失拒绝入库 |
| `data/output/topics/` | draft worker | WebUI 选题/稿件面板 | — |
| `data/output/voice/` | voice worker | render worker、WebUI 试听 | 24h TTL（`voice_master` 同） |
| `data/output/videos/` | render worker | WebUI 播放、publish worker | **永久保留** |
| `data/output/covers/` | publish/cover 阶段 | publish worker、WebUI | **永久保留** |
| `data/work/<task_id>/graphs/` | 编译器 | 人（排障）、复现脚本 | **永久保留**（体积小、价值高） |
| `data/work/<task_id>/scenes/` | render worker | final builder | 7 天 TTL |
| `data/work/<task_id>/manifest.json` | 各阶段 | 审计、WebUI、GC | 追加式写入，禁止覆盖历史字段 |
| `data/browser_profile/` | Playwright | Playwright | **敏感**：含登录态，禁止备份外传与 GC |
| `data/cache/tts/` | voice worker | voice worker | 跨任务共享，LRU 5 GB |
| `data/studio.db` | API + 4 worker | 全体 | 唯一真相源（WAL） |

---

## 2.7 产物保留与 GC 策略（T4.12 落地）

| 产物 | TTL | 触发 | 保护规则 |
| --- | --- | --- | --- |
| 句子 WAV / 拼接人声 | 24 h | 每小时 | 任务未 `completed` 则跳过 |
| `data/work/*/scenes/` | 7 d | 每日 | 任务未完成或属最近 20 个任务则跳过 |
| TTS 缓存 | LRU 5 GB | 水位/定时 | `use_count ≥ 2` 降权淘汰 |
| **成片 / 封面 / manifest / 滤镜图 / 字幕** | **永久** | — | 可手工归档到外部目录 |
| **`data/browser_profile/`** | 永久 | — | **不得进入任何备份/GC 流程**（含登录态） |
| 日志 | 90 d | 每日归档 | `error`/`fatal` 永久保留 |
| `data/hot/*.md` | 90 d | 每月 | 已消费热点归档到 `data/hot/archive/` |
| `system_logs` 中 `info/debug` | 90 d | 每日 | 导出 NDJSON.gz 后删除 |
