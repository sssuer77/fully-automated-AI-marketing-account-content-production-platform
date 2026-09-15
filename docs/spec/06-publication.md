# §06 成片与发布（原文第六部分 · 重建）

> **本节是原文缺失第六部分的重建结论**（重建依据见 §README.0.2 的三条独立证据）。原文 §1.1 把「⑤ 成片与发布」列为五大子系统之一，§8 要求"快速封面→成片审核→自动发布→数据回收→记忆沉淀"，§9.3 把"发布/数据回收"列为**自动**项。
> **与 §04.6 的分工**：§04.6 = **接口与签名**（`Publisher` ABC、`PublishRequest`、`HandoffAdapter`）；**本节 = 子系统行为、平台矩阵、风控与合规、运维流程**。
> 工程假设 A1/A2（见 §README.0.3）：发布范围为**国内主流短视频平台**，分两档实施；因未提供"制片台既有设计"的接口，**由本项目内置实现**完整链路，同时预留 `HandoffAdapter`。

---

## 6.1 子系统定位与在流水线中的位置

```
渲染完成(completed) ─▶ ①封面 ─▶ ②发布前审核 ─▶ ③发布 ─▶ ④数据回收 ─▶ ⑤记忆沉淀
                        §6.3      §6.4          §6.5      §6.6          §6.8
                                    │          ↑ 定时调度 §6.5.5            │
                                    │          │                            │
                                    └─ 不通过 ⇒ manual_required（不阻塞主流程）
                                                                             ▼
                                                      data/feedback/auto_*.md
                                                                             │
                                      ⑥ 数据报告（周/月）§6.7 ◀──────────────┘
                                        └─ insights 决策建议 ─▶ 人工采纳
                                                                             │
                                                       ┌─────────────────────┘
                                                       ▼
                                          下一轮 Planner 的 grounded_on（闭环）
```

**四条设计原则（与 §README.1 的 P1–P5 对齐）**

| 原则 | 在本节的具体化 |
| --- | --- |
| **本地优先** | 发布由本机 Playwright 驱动**真实浏览器**完成，不使用任何第三方发布服务/API 代管；登录态只落本机 profile 目录 |
| **断点续传** | 一个任务 × 一个平台 = 一个 `publish` 池 job；失败重试**不重做**已成功的平台（幂等键保证） |
| **无人工介入** | 全流程自动；`manual_required` 是**降级出口**而非常规步骤（仅登录态失效 / 3 次失败 / 审核不通过时进入） |
| **确定可审计** | 每次发布留截图 + DOM 快照 + 选择器版本 + 平台返回文案；发布决策写 `audit_ops` |

**关键决策：默认关闭。** `publish.enabled=false`（R14）。先跑通 T1–T4 验证出片质量，人工确认后打开——**发布不可逆**，这是全系统唯一不可撤销的操作。

---

## 6.2 平台矩阵与输出 profile（A1）

### 6.2.1 分档实施

| 档 | 平台 | 一期 | 说明 |
| --- | --- | --- | --- |
| **一线** | 抖音 `douyin`、快手 `kuaishou`、微信视频号 `shipinhao` | ✅ 必做 | 营销号主战场，竖屏 9:16 |
| **二线** | 小红书 `xiaohongshu`、B站 `bilibili`、西瓜视频 `xigua`、微博 `weibo` | 🔶 仅接口 | `Publisher` 子类与 profile 已定义，实现可空（R18 控制范围） |

> Q9 默认：一期只做一线；二线保留接口与 profile，二期按实际投放需求逐个补齐。

### 6.2.2 平台约束矩阵（**以 profile 为准，T5.2 实测校准**）

> ⚠️ 下表的数值是**工程默认值**，用于驱动校验与告警；平台规则会变，**必须在 T5.2 阶段用真实账号实测校准**并回写 `config/publish.yaml`。表中标注"实测"的项表示需现场确认。

| 平台 | 画幅 | 时长上限 | 标题/文案长度 | 封面 | 话题 | 需要人工介入的项 |
| --- | --- | --- | --- | --- | --- | --- |
| `douyin` | 9:16 1080×1920 | 长视频权限外 ≤ 15min（实测） | 标题 ≤ 55 字 | 自定义封面（可选） | `#话题` | 首次扫码登录 |
| `kuaishou` | 9:16 | ≤ 10min（实测） | 标题 ≤ 60 字 | 自定义封面 | `#话题` | 首次扫码登录 |
| `shipinhao` | 9:16 | ≤ 30min（实测） | 描述 ≤ 1000 字 | 自定义封面 | `#话题` | 首次扫码登录 |
| `xiaohongshu` | 3:4 或 9:16 | ≤ 5min（实测） | **标题 ≤ 20 字**；正文 ≤ 1000 字 | 首帧/自定义 | `#话题` | 首次扫码登录 |
| `bilibili` | **16:9 横屏**为主 | 长视频需权限 | 标题 ≤ 80 字；**必选分区** | 16:9 封面必填 | `#话题` | 分区选择、封面尺寸 |
| `xigua` | 16:9 / 9:16 | 长视频（实测） | 标题 ≤ 30 字 | 自定义封面 | `#话题` | 首次扫码登录 |
| `weibo` | 9:16 / 16:9 | ≤ 30min（实测） | 正文 ≤ 2000 字 | 首帧 | `#话题#` | 首次扫码登录 |

**由该矩阵导出的三条工程后果**

1. **输出 profile 必须参数化**（`config/outputs.yaml`）：竖屏模板 `douyin_9x16_default` 产出 1080×1920；若投 B站/西瓜横屏，需**另一个模板**（`bilibili_16x9_default`）—— 不做"自动裁切变形"（会毁掉字幕安全区）。
2. **标题长度取全平台最小值做校验**（小红书 20 字）：`CoverOutput.title_text ≤ 20` 已是硬约束（§04.1.8）；发布文案按平台 profile 二次裁剪，超长 ⇒ `warn` 而非静默截断。
3. **`tags` 必须按平台语法生成**：抖音/快手/视频号用 `#话题`，微博用 `#话题#`，由 `selectors/<platform>.yaml` 的 `tag_syntax` 决定。

### 6.2.3 profile 契约（`config/publish.yaml` 节选）

```yaml
enabled: false                     # ★ 默认关闭（R14）
require_confirm: true              # 发布前确认闸（R14）
handoff:
  enabled: false
  adapter: local                   # local | <外部实现>

accounts:
  - account_id: acc_main
    platform: douyin
    profile_dir: data/browser_profiles/acc_main    # 持久化登录态（含 Cookie，.gitignore）
    enabled: true

platforms:
  douyin:
    publisher: douyin
    profile: douyin_9x16_default
    title_max: 55
    caption_max: 1000
    tag_syntax: "#{tag}"
    daily_limit: 3
    min_gap_min: 30
    selectors_version: "2026-09-13"
  kuaishou:  {publisher: kuaishou,  profile: douyin_9x16_default, title_max: 60,  daily_limit: 3, min_gap_min: 30}
  shipinhao: {publisher: shipinhao, profile: douyin_9x16_default, title_max: 1000, daily_limit: 3, min_gap_min: 30}
  xiaohongshu: {publisher: xiaohongshu, enabled: false, title_max: 20, caption_max: 1000}
  bilibili:    {publisher: bilibili,    enabled: false, profile: bilibili_16x9_default, title_max: 80}
  xigua:       {publisher: xigua,       enabled: false, title_max: 30}
  weibo:       {publisher: weibo,       enabled: false, tag_syntax: "#{tag}#"}

metrics_schedule_hours: [1, 6, 24, 72]     # 数据回收时点
```

---

### 6.2.4 多账号支持（口述 D1：**结构支持多账号，暂时单账号**）

| 项 | 规则 |
| --- | --- |
| 数据结构 | **完整支持多账号**：`publications.account_id`、`publish_schedules.account_ids_json[]`、`config/publish.yaml: accounts[]`、`data/browser_profiles/<account_id>/` |
| 默认配置 | **只启用 1 个账号**（`acc_main`）；其余账号不配置即不参与发布 |
| 加账号成本 | **零迁移**：新增一条 `accounts[]` 配置 + 首次扫码登录即可 |
| 限频粒度 | **按账号独立计数**（`≤3 条/天/账号`）；多账号不会互相占用额度 |
| 幂等键 | `sha256(task_id\|platform\|account_id)` —— 含 `account_id` ⇒ **同任务可安全分发到多账号**（不同账号算不同发布） |
| 登录态隔离 | 每账号独立 `profile_dir`，互不干扰；某账号失效**不影响**其他账号 |
| 报告维度 | `reports.data_json` 支持按 `account_id` 拆分对比（多账号启用后自动生效） |

> **设计意图**：现在单账号跑通闭环；将来做矩阵时，**只改配置不改代码**。

---
## 6.3 快速封面（原文 §8 第一步）

| 项 | 规则 |
| --- | --- |
| 抽帧点 | `CoverOutput.frame_at_ms`（默认 `hook` 句 `start_ms + 500`）—— 取**开口瞬间**，表情最生动 |
| 合成 | ffmpeg 单帧抽取 → `drawtext` 叠加 `title_text`（主）+ `sub_text`（次）+ `highlight_words` 换色 |
| 输出 | `data/output/covers/{时间戳}_{task_id}_cover.jpg`（1080×1920，JPEG q=3） |
| 排版 | 自动换行（≤2 行 × ≤10 字）、描边保证任意背景下可读、**强制落在安全区内** |
| 降级 | 抽帧失败（视频损坏）⇒ 用纯色底 + 文字生成封面（`warn`）；仍失败 ⇒ 无封面发布（平台用首帧） |
| 校验 | 文字溢出检测（渲染后测包围盒）⇒ 自动缩放字号（最小 60pt），仍溢出 ⇒ 截断 + `warn` |
| 留痕 | 封面路径写入 `tasks.context_json.cover_path` + `artifacts(kind='cover')` |

**与 §04.1.8 的分工**：`CoverOutput`（Agent 产出的**文案与抽帧点**）与封面**合成**（ffmpeg 动作）解耦——文案可被审稿与禁区扫描，合成可被单测。

---

## 6.4 发布前审核（原文 §8 第二步 · R14 不可逆防护）

**三道门禁（缺一不可，全部来自已有子系统，不新增逻辑）**

| # | 门禁 | 判据 | 来源 | 不通过处理 |
| --- | --- | --- | --- | --- |
| 1 | **水印已叠加**（D5 必做） | `quality_json.watermark_applied == true` | 渲染阶段产物 | **拒绝发布** ⇒ `manual_required`（缺水印是硬缺陷） |
| 2 | 响度 | `lufs ∈ [-16.5, -15.5]` 且 `true_peak ≤ -1.0 dBTP` | `scripts/audio_qc.py` | **拒绝发布** ⇒ `manual_required` |
| 3 | 相似度（防搬运） | `dup_audit` 通过（§04.2.4.5） | `scripts/dup_audit.py` | `warn` ⇒ 允许发布但标记"建议复核"（**不阻塞**，P4） |
| — | ~~音画同步~~ | ~~`|av_sync_offset_ms| ≤ 80ms`~~ | `scripts/av_sync_audit.py` | ⚠️ **已按 C12 取消硬门禁**（口述 D2"不需要音画同步"）⇒ 降为**诊断指标**，仅写入 `quality_json` 并在 WebUI 显示，**不阻断发布** |

**文案与标题禁区扫描（复用规则通道，不额外调 LLM）**

| 扫描对象 | 词表 | 命中处理 |
| --- | --- | --- |
| `title`（封面文案 / 平台标题） | `persona.forbidden` + `banned_words.yaml` | **拒绝发布** ⇒ `manual_required` |
| `caption`（发布文案） | 同上 | **拒绝发布** ⇒ `manual_required` |
| `tags` | 同上 | 剔除该 tag + `warn` |

**发布前确认闸（`publish.require_confirm=true` 时）**

- 复用 §04.4.4 的确认闸机制：任务进入 `completed`，`publications.status='queued'`，WS 推 `publish.manual_required`（reason=`await_confirm`）。
- WebUI 发布面板展示：成片预览 + 封面 + 标题 + 文案 + 三道审计结论 + 目标平台列表。
- 人工确认后 ⇒ 批量 `enqueue jobs(publish/publish × 平台)`；放弃 ⇒ `publications.status='canceled'`（任务保持 `completed`）。
- 若开启"一键全自动"（`auto_approve_policy=GRADE_AB`）**且** `publish.require_confirm=false`，则跳过本闸——**两个开关必须同时满足**才允许无人确认发布。

---

## 6.5 发布执行（原文 §8 第三步 · Playwright 浏览器自动化）

### 6.5.1 选型结论（详见 §01.8）

| 方案 | 结论 | 理由 |
| --- | --- | --- |
| **Playwright（采纳）** | ✅ | 持久化 `user_data_dir` 复用登录态；原生等待/自动重试；失败可截图取证；可 headless |
| Selenium | ❌ | 驱动与浏览器版本耦合、等待机制弱、取证繁琐 |
| 平台开放 API | ❌ | 国内主流短视频平台**不向个人开放上传 API**；申请门槛与审核周期不可控 |
| 第三方代发布服务 | ❌ | 违反 P1 本地优先；账号凭据外泄风险 |

### 6.5.2 工程约束（R13）

| 约束 | 值 | 说明 |
| --- | --- | --- |
| 浏览器 | 本机 Chromium（`PLAYWRIGHT_BROWSERS_PATH` 重定向 D 盘，§README.6） | 不落 C 盘 |
| 登录态 | `data/browser_profiles/<account_id>/` 持久化；**不自动登录、不绕过验证码** | 合规底线 |
| headless | 默认 `true`；排障/首次登录用 `false`（WebUI 可临时开启） | 首次扫码必须可见 |
| 选择器 | **集中**在 `publish/selectors/<platform>.yaml` + `selectors_version` | 页面改版时热修，无需改代码 |
| 超时 | 上传 300s / 整体 600s（= job 租约） | 超时 ⇒ `PUBLISH_TIMEOUT` ⇒ 重试 |
| 截图 | 每步关键节点截图（上传完成、文案填入、发布前、结果页） | 失败取证 |
| 并发 | 1（`publish` 池 `concurrency=1`） | 同浏览器多标签易触发风控 |
| 限频 | ≤3 条/天/账号，间隔 ≥30min | 由 `pool_settings.rate_limit_json` 守卫（§03.4.4） |

### 6.5.3 发布流程（单平台 · 八步）

```
① 打开平台创作页（selectors 定位"发布"入口）
② 上传视频文件（等待上传完成事件，轮询进度百分比 → WS: publish.progress）
③ 填写标题 / 文案 / 话题（tag_syntax 按平台）
④ 选择封面（若平台支持自定义）
⑤ 二次校验：页面回读标题与文案，与请求逐字比对（防输入丢失）★ 关键
⑥ 点击发布（dry_run=true 时在此停止 + 截图 + 返回 ok=true, status='queued'）
⑦ 等待结果页 / 作品列表出现新作品（提取 url / platform_post_id）
⑧ 落库：publications(status='published', url, published_at, evidence_json)
```

> **第 ⑤ 步是必须的**：平台富文本编辑器常吞掉 emoji / 换行 / 超长文本。回读比对失败 ⇒ 重填 ≤2 次 ⇒ 仍失败 ⇒ `PUBLISH_UPLOAD_FAILED`。

### 6.5.4 发布状态机（与任务状态机解耦）

```
queued ─▶ uploading ─▶ published
   │          │
   │          ├─▶ failed ──(重试<3, 指数退避)──▶ queued
   │          │      └──(重试≥3)──▶ manual_required
   │          └─▶ manual_required（登录态失效 / 审核不通过 / 选择器失效）
   └─▶ canceled（人工取消）
```

**关键约定**：发布失败**不回退任务状态**（任务停在 `completed`）—— 成片仍然有效，可人工下载或换平台重发。

---

### 6.5.5 定时发布调度（口述 D7 · 新增 · **策略可编辑 · Q14**）

> **定位**：把"发出去"从"立刻"变成"到点"。适配营销号作息（晚间流量高峰）与多账号错峰。

| 项 | 规则 |
| --- | --- |
| 三种模式 | ① `at_time` 指定时刻 ② **`daily_window` 每日窗口内随机**（默认）③ `interval` 固定间隔 |
| 默认策略 | `daily_window` = **18:00–21:30 内随机取时刻** + `jitter_min=15`（Q14 默认值，**非硬编码**） |
| 为什么要随机 | 固定准点发布是明显的机器特征（R13）；窗口随机更像真人运营 |
| 调度器形态 | `services/scheduler_service.py`，**30s tick**，`next_run_at` 落库（重启不丢） |
| 与队列关系 | 调度器**只负责到点创建 `publish` job**；job 的认领/租约/重试全走 §03.4 —— **不占 worker 空转** |
| 与限频关系 | **叠加**：定时 ≠ 免限频。`≤3 条/天/账号` 仍生效；被限频 ⇒ `last_result='skipped_ratelimit'` + 顺延（**不算失败**） |
| 与开关关系 | `publish.enabled=false` 时调度器**空转**（不建 job），记 `last_result='skipped_disabled'`（便于验证调度器在工作） |
| 幂等 | 发布幂等键（§03.3.15）仍生效 ⇒ 定时**不会**造成重复发布 |
| 失败处理 | 建 job 失败 ⇒ `warn` + 顺延 1 tick；连续失败 ≥5 次 ⇒ `system.alert` |
| **策略可编辑（Q14）** | 模式 / 窗口起止 / `jitter_min` / 平台 / 账号 / 启停 **全部可在 WebUI 编辑**，无需改配置文件；`PATCH` ⇒ **同事务重算 `next_run_at`** + 写 `audit_ops`（陷阱 #33：只改参数不重算 ⇒ 新策略不生效或立刻触发） |
| 参数校验 | 窗口必须 `HH:MM` 且 `start < end`；`jitter_min ∈ [0,120]`；`at_time` 必须带时区；非法参数 ⇒ **400 且不落库** |
| 留痕 | 计划增删改启停**全部**写 udit_ops（schedule.create / schedule.update / schedule.disable） |
| WebUI | 发布面板「定时计划」区块：列表 + `next_run_at` 倒计时 + 启停开关 + 「立即执行一次」+ **编辑表单**（模式/窗口/抖动/平台/账号） |

**数据结构**：`publish_schedules`（§03.3.18）；**契约**：§04.6.5.1；**接口**：`/api/v1/schedules`（含 `PATCH` 编辑）。

**取时刻算法（幂等 + 可复现）**

```python
def next_run_in_window(win: tuple[str, str], *, jitter_min: int, seed: str) -> datetime:
    """在 [window_start, window_end] 内取随机时刻 + ±jitter_min 抖动。
    - 随机源 = HMAC(seed = schedule_id + 日期) ⇒ 同一天多次计算得到**同一时刻**（幂等，重启不漂移）
    - 抖动不得越出窗口（越界则夹取到边界）
    - 目的：避免"每天准点"的机器特征（R13）
    """
```

**与"即时发布"的关系**：不配置任何定时计划时，任务 `completed` 后**立即**创建 `publish` job（原行为）。定时是**可选增强**，不是必经路径。

---
## 6.6 数据回收（原文 §8 第四步 · 原文 §9.3"数据回收"列为自动项）

| 项 | 规则 |
| --- | --- |
| 调度 | 由 `metrics_service` 轮询 `publications.next_metric_at`（**不用队列**——它不是任务，失败无需重试链） |
| 时点 | T+1h / T+6h / T+24h / T+72h（可配 `metrics_schedule_hours`） |
| 采集 | `Publisher.fetch_metrics(platform_post_id)` → `views / likes / comments / shares` |
| 落库 | `publications.metrics_json`（最新）+ `metrics_history_json`（时间序列，供趋势图） |
| 失败 | 记 `warn` + 顺延 1 小时重试（≤3 次）；仍失败 ⇒ 停止采集该条（**不阻塞**其他发布） |
| 平台限制 | 部分平台数据有延迟/不公开 ⇒ 字段允许 `None`（**禁止**用 0 冒充"无数据"） |
| 汇总 | 每日生成 `data/feedback/auto_YYYYMM.md`（§6.8） |

---

## 6.7 数据报告与决策闭环（口述 D9 · 新增）

> **定位**：数据回收（§6.6）产出的是**原始指标**；报告产出的是**结论与建议**。口述"整合生成报告，以便后续决策"正是指这一层。

### 6.7.1 报告 vs 记忆沉淀（职责边界，不可混淆）

| 维度 | §6.7 报告 | §6.8 记忆沉淀 |
| --- | --- | --- |
| 触发 | **定期**（周报/月报） | **实时**（每条发布回收后） |
| 粒度 | 聚合结论（跨任务/跨平台/跨时段） | 单条反馈（单条评论/单个选题） |
| 人工 | **有**（`insights` 需人工采纳才生效） | **无**（自动回流 `feedback_items`） |
| 产物 | `reports` 表 + `summary_md` + 图表 | `data/feedback/auto_*.md` |
| 用途 | **决策**（调整选题偏好、发布时段、平台权重） | **素材**（喂给下一轮 Planner） |

> 两者都读 `publications.metrics_json`，但**一条是"结论"，一条是"原料"**。

### 6.7.2 报告内容

| 区块 | 内容 |
| --- | --- |
| 产量概览 | 任务数 / 成片数 / 发布数 / 各平台分布 |
| 效果指标 | 播放 / 点赞 / 评论 / 分享（总量 + 均值 + **中位数**，避免长尾拉偏） |
| **选题归因** | 按 `hook_type`（冲突/悬念/反差/数字）、方向、评分等级（A/B/C）分组对比效果 |
| **时段归因** | 按 `published_at` 的小时分组对比（回答"几点发最好"） |
| **平台对比** | 同一内容在不同平台的播放/互动差异（多账号启用后可按账号拆） |
| 质量归因 | `grade` 与效果的相关性；`skipped` 句比例与完播的关系；降级任务的效果 |
| 成本 | `llm_calls` 汇总（按 Agent / 模型 / 任务），单位成本（元/条成片） |
| 异常 | 失败任务、死信、`manual_required` 堆积、发布失败原因 Top-N |

### 6.7.3 决策建议（`insights`）—— 报告的核心价值

```python
class Insight(BaseModel):
    kind: Literal["topic", "timing", "platform", "quality", "cost"]
    statement: str  # '数字型钩子在抖音的完播率高于悬念型 23%'
    evidence: dict  # 支撑数据（可追溯到 publications.metrics_json 的聚合值）
    confidence: Literal["low", "medium", "high"]  # 由样本量决定
    suggested_action: str  # '下轮选题中数字型占比提升至 40%'
```

| 置信度规则 | `n < 10` ⇒ `low`（**必须**标注"样本不足，仅供参考"）；`10 ≤ n < 30` ⇒ `medium`；`n ≥ 30` ⇒ `high` |
| --- | --- |

**归因维度（七项，全部由 SQL 聚合得出，不调 LLM）**：①选题类型 ②发布时段 ③平台 ④时长 ⑤稿件评分 ⑥TTS 质量（`skipped` 率）⑦成本。

**为什么报告不调 LLM**：聚合与归因是确定性问题，用 SQL + 规则更准、更快、零成本、可复现；需要自然语言总结时，走 §04.1 的 Agent 通道（**可选开关**，默认关闭）。

### 6.7.4 决策闭环（报告 → 下一轮选题）

```
report.insights[0] = '数字型钩子完播率高于悬念型 23%'（confidence=high, n=34）
        │
        ▼ 人工在报告面板点「采纳」（POST /reports/{id}/insights/{idx}/apply）
写入 config/persona.yaml 偏好项  或  content_directions 权重
        │
        ▼
下一轮 Planner 的 grounded_on 含该偏好 ⇒ 选题结构自动调整
        │
        ▼ 新一轮数据回收 ⇒ 下一份报告验证"调整是否有效"
```

**三条安全阀**

1. **必须人工采纳**：`insights` 只是建议，**不自动**改配置（避免"数据噪声直接改生产策略"）。
2. **样本不足必须标注**：`confidence='low'` 的建议在 WebUI 强制显示警示文案。
3. **采纳留痕**：`audit_ops(action='report.apply_insight')` 记录 `before/after`（谁在何时依据哪条建议改了什么）。

### 6.7.5 调度与呈现

| 项 | 规则 |
| --- | --- |
| 周报 | **周一 09:00** 生成上周（默认值，**可编辑**） |
| 月报 | 每月 1 日 09:00 生成上月 |
| 日报 | 可选（**默认关闭**，数据量小时意义不大） |
| **周期可编辑（Q15）** | 周期由 `report_schedules` 表驱动：`period` / `weekday` / `day_of_month` / `at_time` / `tz` / `lookback_days` / `include` / `enabled` **均可编辑**；**同周期仅允许 1 个启用**（部分唯一索引）；编辑 ⇒ 重算 `next_run_at` + `audit_ops`；`is_builtin=1` 的默认计划**不可删、只能停用** |
| 手动 | `POST /api/v1/reports/generate` |
| 导出 | `GET /api/v1/reports/{id}/export?format=md\|csv` |
| WebUI | 发布面板「报告」区块：报告列表 + 详情页渲染 `summary_md` + 图表 + `insights` 卡片（可采纳/忽略）+ **周期编辑表单** |
| 落盘 | `data/output/reports/{period}_{start}_{end}.md` + 图表 PNG（**永久保留**，纳入备份） |
| 幂等 | `UNIQUE (period, start_date, end_date)` ⇒ 同周期重复生成不重复插入 |

**数据结构**：`reports` + `report_schedules`（§03.3.19 / §03.3.20）；**契约**：§04.6.5.2。

---
## 6.8 记忆沉淀与回流闭环（原文 §8 第五步）

```
① 高互动评论（点赞 ≥ 阈值 或 回复数 ≥ 阈值）
      ⇒ feedback_items(is_auto=1, sentiment=LLM/规则判定, kind='want'|'complaint')
② 低互动选题（views 低于同账号近期中位数 50%）
      ⇒ topic_candidates 同类**降权**（幅度上限 20%，防选题收敛）
③ 汇总进 data/feedback/auto_YYYYMM.md
      ⇒ 下一轮 Planner 的 grounded_on 含 {"type":"feedback","source":"auto"}
```

**闭环验收（T5.4）**：`data/feedback/auto_YYYYMM.md` 必须**能被 §04.1.7 的解析器直接消费**（无需人工编辑），且下一轮 Planner 输出中 `grounded_on` 出现 `source="auto"` 的引用。

**三条安全阀**

1. **`is_auto=1` 区分来源**：Planner 可配置是否采纳自动回流数据（`config/llm.yaml: planner.include_auto_feedback`）。
2. **降权幅度上限 20%**：避免"一次低播放 ⇒ 永久放弃某方向"的过拟合。
3. **评论采样的合规**：只采集公开可见的评论与计数，**不采集任何用户身份信息**（不留昵称/头像/ID，只留文本与情感标签）。

---

## 6.9 风控与合规（R2 / R3 / R13 / R14）

| 风险 | 措施 | 落点 |
| --- | --- | --- |
| **R2 熊大熊二 IP 音色**（声音权/著作权） | ①音色 ID 与展现名**可配置解耦**；②一键替换为自录音色；③WebUI 显著合规提示；④`data/voice_src/*/profile.json` 来源登记留档 | §04.3.1 / T2.4 / T4.8 |
| **R3 MC 跑酷素材版权与搬运判定** | ①仅入库自录/授权素材（`license` 强制字段 + `proof_path`）；②随机化 + 相似度审计双保险 | §04.2.4 / T3.2 / T3.5 |
| **R13 多平台风控** | ①按平台限频（≤3 条/天/账号，间隔 ≥30min）；②登录态探测与告警；③失败转"待人工发布"；④选择器集中配置便于热修；⑤**不实现验证码绕过、不自动登录** | §6.5 / T5.3 |
| **R14 自动发布不可逆** | ①`publish.enabled=false` 默认关闭；②`require_confirm=true`；③发布前重跑三道门禁 + 文案禁区扫描；④封面/标题长度校验 | §6.4 / T5.1 |
| **内容合规**（虚假宣传/敏感词/限流） | 规则通道禁区词 + LLM 六维度含禁区项 + 确认闸 + `audit_ops` 留痕 | §04.1.6 / T1.11 |
| **账号安全** | 登录态仅存本机 profile 目录（`.gitignore`）；不记录密码；不使用任何第三方登录服务 | §6.5.2 |

---

## 6.10 降级与人工兜底（`manual_required` 处置）

| 触发场景 | `error_code` / `reason` | 处置 |
| --- | --- | --- |
| 登录态失效 | `PUBLISH_LOGIN_EXPIRED` | 转 `manual_required`；WebUI 提示"需人工扫码登录"（**不自动登录**） |
| 选择器失效（页面改版） | `PUBLISH_SELECTOR_MISS` | 转 `manual_required` + 截图 + DOM 快照；运维按 `docs/runbook/publish_selector.md` 热修 `selectors/*.yaml` |
| 重试 ≥3 仍失败 | `PUBLISH_UPLOAD_FAILED` | 转 `manual_required`；成片可从渲染面板直接下载人工发布 |
| 发布前审核不通过 | `PRECHECK_AV_SYNC` / `PRECHECK_LOUDNESS` / `PRECHECK_BANNED` | 转 `manual_required`（**不静默放行**）；可回到渲染阶段重渲 |
| 限频触顶 | `PUBLISH_RATELIMIT` | **不是失败**：`not_before = 次日 00:00 + jitter`，job 回 `pending` 自动顺延 |
| 平台审核不通过 | `PUBLISH_REVIEW_REJECTED` | 转 `manual_required` + 记录平台文案；回流水线触发一次改稿（可选） |

**人工兜底的三条不变量**

1. `manual_required` **不改变任务状态**（任务保持 `completed`），成片始终可用。
2. 所有 `manual_required` 必须在发布面板可见、可一键重试、可一键取消。
3. 处置动作**必须**写 `audit_ops`（`publish.retry` / `publish.cancel` / `publish.manual_done`）。

---

## 6.11 与外部"制片台既有设计"的对接（A2）

```
本系统（制片台内置）                          外部系统（若存在）
  rendering → completed
        │
        ├─ HandoffAdapter.push(HandoffPayload) ──▶ 复制到 data/handoff/<task_id>/
        │                                          （或实现外部 ABC 推送）
        └─ 同时走内置发布链路（§6.5）
```

| 项 | 规则 |
| --- | --- |
| 默认 | `handoff.enabled=false`（不推送，只走内置链路） |
| 交付包 | 自包含（视频/封面/ASS/稿件/timeline/manifest/质检报告），**不依赖本系统在线** |
| 失败影响 | **不影响**主流程；失败记 `warn` + `audit_ops`，可手动重推 |
| 外部实现 | 实现 `HandoffAdapter` ABC 并在 `config/publish.yaml: handoff.adapter` 指定 |

---

## 6.12 发布面板（WebUI · 工程必需，原文未列）

| 区块 | 内容 | 操作 |
| --- | --- | --- |
| 待发布 | 任务、封面缩略图、标题、目标平台、预检结论 | 确认发布 / 取消 / 改文案 |
| 发布中 | 平台、进度百分比、当前步骤 | 取消（尽力） |
| 已发布 | 平台、发布时间、链接、封面 | 打开链接 / 复制文案 |
| 数据回流 | 各时点指标 + 趋势折线 | 手动刷新某条 |
| **待人工** | `manual_required` 列表 + 失败原因 + 截图 + DOM 快照 | 重试 / 标记已人工处理 / 取消 |
| 账号 | 各平台登录态（`health()`）、最近检查时间 | 触发登录态探测 / 提示扫码 |
| **定时计划**（D7/Q14 新增） | 计划列表、模式、窗口、`jitter_min`、`next_run_at` 倒计时、`last_result` | 新建 / **编辑策略（模式·窗口·抖动·平台·账号）** / 启停 / 立即执行一次 |
| **报告**（D9/Q15 新增） | 周报/月报列表、`summary_md`、图表、`insights` 卡片、**周期设置** | 查看 / 导出 / **采纳建议**（写入 persona 或方向权重）/ **编辑周期**（daily·weekly·monthly） |

---

## 6.13 §06 验收标准（对应 T5.1–T5.8）

```text
[ ] T5.1 封面：studio publish cover --task <id> 产出 1080×1920 封面，标题清晰可读
[ ] T5.1 发布前校验：审计不通过或命中禁区 ⇒ 拒绝发布并转 manual_required（不静默放行）
[ ] T5.2 Publisher ABC 契约测试通过（Mock 实现可替换）
[ ] T5.2 studio publish dry-run --task <id> --platform douyin 走完流程到"确认发布"前一步并截图
[ ] T5.2 health() 能正确报告"未登录"与"登录态已过期"
[ ] T5.3 第 4 条当天发布被限频拒绝（自动顺延，不报失败）
[ ] T5.3 连续失败 3 次 ⇒ publications.status='manual_required' 且任务仍为 completed
[ ] T5.3 幂等键防重复发布（同 task+platform+account 只发一次）
[ ] T5.4 T+1h 能在发布面板看到数据；metrics_json 与 metrics_history_json 正确落库
[ ] T5.4 data/feedback/auto_YYYYMM.md 生成且能被 §04.1.7 解析器消费（闭环）
[ ] T5.5 发布面板七个区块可用（含定时计划、报告）；manual_required 可重试/可取消且写 audit_ops
[ ] T5.6 定时发布：daily_window 模式在窗口内随机取时刻 + 抖动；到点才建 job（不占 worker）
[ ] T5.6 定时 ≠ 免限频：第 4 条被限频 ⇒ last_result='skipped_ratelimit' 且顺延（不算失败）
[ ] T5.6 publish.enabled=false 时调度器空转且记 skipped_disabled
[ ] T5.7 报告生成：周报含 summary_md + data_json + insights；同周期重复生成不重复插入（幂等）
[ ] T5.7 置信度：n<10 的 insight 在 WebUI 强制显示"样本不足"
[ ] T5.7 决策闭环：采纳 insight ⇒ 写入 persona/方向权重 + audit_ops；下轮 Planner grounded_on 可见
[ ] T5.8 多账号（D1）：结构支持多账号、默认单账号；加账号零迁移；限频按账号独立计数
[ ] T5.6 策略可编辑（Q14）：PATCH 改窗口/抖动 ⇒ **同事务重算 next_run_at** + audit_ops；非法参数 ⇒ 400 且不落库
[ ] T5.7 周期可编辑（Q15）：report_schedules CRUD 生效；**同周期仅 1 个启用**（部分唯一索引）；is_builtin 计划不可删、只能停用
[ ] Q12 BGM 可导入素材库：上传 BGM ⇒ 立即可被 T3.6 混音随机选中；**无 BGM ⇒ 静音降级仍出片**（不报错）
[ ] 全局：publish.enabled 默认 false；开启前后均有 audit_ops 记录
[ ] 全局：C12 已生效 —— av_sync 不阻断发布，仅作诊断
```