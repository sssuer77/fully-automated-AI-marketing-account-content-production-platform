// 一键出片面板状态（T4.14 延伸 · §04.6.8）。
//
// 这一屏要回答四个问题
// --------------------
// ① 这条任务现在在哪儿、点了会怎样？② 拿什么参数跑？③ 跑到哪一步了、成片出来没有？
// ④ 中途不想跑了怎么办？
//
// 与渲染面板的关系
// ----------------
// 渲染面板管的是**渲染这一步**（给它文案或任务号，它出片）；这一屏管的是**整条链路**
// （从任务当前状态出发，该投配音就投、该拼母带就拼、该渲染就渲染）。两块并存不是重复：
// "我手上只有一版稿子"与"我手上是一个已经配好音的任务"是两个真实的起点。
//
// 为什么先把"会怎样"问一次
// ------------------------
// 这条链路有三条硬规矩：**不写稿**、**不代按确认闸**、**不接 stuck 状态**。把这三条做成
// "点了之后返回 409"，面板就只能等错误回来才说话；而任务号是用户手输的，敲错一个字符
// 也要等一轮往返。所以任务号一确定就先问一次预览 —— 能不能跑、不能跑是为什么、
// 会从哪儿推到哪儿。**判据在后端**（`plan_task` 与 `run_task` 用同一批常量），
// 这里只负责把它显示出来。
//
// 为什么**只在有活的时候**轮询
// --------------------------
// 与渲染面板同一条：空闲时一直拉，就是每秒钟一次"什么新东西都没有"的请求。代价不只是
// 带宽 —— 面板上那些数字会一直跳，而它们跳动的唯一原因是"我们在定时问"。
// 代价也说清楚：**别人在命令行跑的那一条不会自己冒出来**（那是另一个进程的登记表）。
// 所以空闲时留一颗「刷新」，而不是留一个假装看得见的轮询。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  cancelPipelineJob,
  createPipelineJob,
  fetchPipelineConsole,
  fetchPipelineJob,
  fetchPipelineTask,
  type PipelineConsole,
  type PipelineJob,
  type PipelineJobRequest,
  type PipelineTask,
} from "@/api/endpoints/pipeline";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";

/** 有活在跑时的轮询周期。1 秒：够快（配音一句要几百毫秒）也不至于把后端问爆。 */
export const PIPELINE_POLL_MS = 1_000;

/** 任务号长度上限（与后端 `PipelineJobRequest.task_id` 的 `max_length` 同源）。 */
export const MAX_TASK_ID_CHARS = 64;

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface PipelineApi {
  fetchPipelineConsole: typeof fetchPipelineConsole;
  fetchPipelineTask: typeof fetchPipelineTask;
  createPipelineJob: typeof createPipelineJob;
  fetchPipelineJob: typeof fetchPipelineJob;
  cancelPipelineJob: typeof cancelPipelineJob;
}

let api: PipelineApi = {
  fetchPipelineConsole,
  fetchPipelineTask,
  createPipelineJob,
  fetchPipelineJob,
  cancelPipelineJob,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configurePipelineApi(overrides: Partial<PipelineApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "跑着",
  succeeded: "完成",
  failed: "失败",
  canceled: "已取消",
};

/** job 状态 → 中文（面板上不出现裸英文状态码）。 */
export function jobStatusLabel(status: string): string {
  return JOB_STATUS_LABELS[status] ?? status;
}

/** job 状态 → 灯色。 */
export function jobStatusTone(status: string): StatusTone {
  switch (status) {
    case "succeeded":
      return "ok";
    case "failed":
      return "error";
    case "canceled":
      return "warn";
    case "running":
      return "busy";
    default:
      return "idle";
  }
}

/**
 * **任务**状态 → 中文（§README.3 的 16 态）。
 *
 * 不认识的**原样显示**而不是吞掉：后端加了新状态而前端还没跟上时，面板上出现一个
 * `some_new_state` 也好过什么都不说 —— 后者会让人以为任务号填错了。
 */
const TASK_STATUS_LABELS: Record<string, string> = {
  pending: "待写稿",
  drafting: "写稿中",
  reviewing: "审稿中",
  editing: "改稿中",
  awaiting_approval: "待人工确认",
  queued_voice: "待配音",
  voicing: "配音中",
  queued_render: "待渲染",
  rendering: "渲染中",
  completed: "已成片",
  publishing: "发布中",
  published: "已发布",
  failed: "失败",
  manual_pool: "人工池",
  discarded: "已废弃",
  canceled: "已取消",
};

export function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] ?? status;
}

/**
 * **任务**状态 → 灯色。
 *
 * 与 job 的灯色不是一回事：`running`（这条流水线在跑）与 `voicing`（这个任务在配音）
 * 是两个层次的"动着"，但它们在面板上要长得一样 —— 都是 `busy`。分两套写法只会让
 * 同一个视觉语义有两处定义。
 */
export function taskStatusTone(status: string): StatusTone {
  switch (status) {
    case "completed":
    case "published":
      return "ok";
    case "failed":
    case "manual_pool":
      return "error";
    case "awaiting_approval":
    case "discarded":
    case "canceled":
      return "warn";
    case "drafting":
    case "reviewing":
    case "voicing":
    case "rendering":
    case "publishing":
      return "busy";
    default:
      return "idle";
  }
}

/** 流水线阶段 → 中文（后端给的是 `voice` / `render` 这类稳定标识符）。 */
const STAGE_LABELS: Record<string, string> = {
  queued: "排队",
  starting: "准备",
  script: "接手稿件",
  review: "过审稿",
  gate: "放行",
  voice: "配音",
  render: "渲染",
  done: "完成",
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

/** 阶段 + 分数（`渲染 3/12`）。`total<=0` 时只显示阶段 —— 不编一个分母。 */
export function stageText(job: PipelineJob): string {
  const label = stageLabel(job.stage || "-");
  return job.total > 0 ? `${label} ${job.done}/${job.total}` : label;
}

/** ISO 时间戳 → `HH:MM:SS`（日期在这一屏没有意义，与渲染面板同一手法）。 */
export function formatClock(ts: string | null): string {
  return ts === null || ts.length < 19 ? "-" : ts.slice(11, 19);
}

/** 成片文件名（从结果里的绝对路径取末段）—— `<video>` 只认名字，不认整条路径。 */
export function finalVideoName(job: PipelineJob): string | null {
  if (job.final === null) return null;
  const name = job.final.split(/[\\/]/).pop() ?? "";
  return name === "" ? null : name;
}

/**
 * 表单草稿。种子用**字符串**存：空串表示"不传这个参数"，而不是 NaN。
 *
 * 为什么这里**没有**字幕 / 画布档 / 线程数
 * --------------------------------------
 * 那些是**渲染层**的参数，归「合成配置」与「渲染」两块面板管。在这一屏再放一遍，
 * 要么得先问后端"配置里现在开着吗"（多一次往返、多一处会过时的显示），要么就变成
 * 一个"看着是关的、实际是跟随配置"的假开关 —— 后者比没有更糟。这一屏只管一件事：
 * **从这条任务当前的状态推到哪一步**。
 */
export interface PipelineDraft {
  taskId: string;
  /** 落点。空串 = 用后端的默认落点（一路出成片）。 */
  until: string;
  voice: string;
  seed: string;
}

export function emptyDraft(): PipelineDraft {
  return { taskId: "", until: "", voice: "", seed: "" };
}

/** 提交前自检。返回**人话**，`null` = 可以提交。 */
export function validate(draft: PipelineDraft): string | null {
  const taskId = draft.taskId.trim();
  if (taskId === "") return "任务号不能空：整条链路是围着它转的（稿子、句子、成片都挂在它下面）。";
  if (taskId.length > MAX_TASK_ID_CHARS) return `任务号最长 ${MAX_TASK_ID_CHARS} 个字符。`;
  if (!/^[0-9A-Za-z_-]+$/.test(taskId)) {
    return "任务号只能用字母、数字、连字符与下划线（它要进 URL 路径）。";
  }
  if (draft.seed.trim() !== "" && !Number.isInteger(Number(draft.seed.trim()))) {
    return "随机种子要是一个整数（留空 = 每次随机挑素材）。";
  }
  return null;
}

/**
 * 草稿 → 请求体。
 *
 * 空的可选项**一个都不发**（而不是发 `null`）：后端把"没传"和"传了 null"当同一件事，
 * 但发出去的空字段会让请求日志里全是 `"voice": null`，排查时看不出"用户到底选没选"。
 */
export function toRequest(draft: PipelineDraft): PipelineJobRequest {
  const request: PipelineJobRequest = { task_id: draft.taskId.trim() };
  if (draft.until !== "") request.until = draft.until;
  if (draft.voice !== "") request.voice = draft.voice;
  const seed = draft.seed.trim();
  if (seed !== "") request.seed = Number.parseInt(seed, 10);
  // `subtitle` **不发**：留空 = 后端按 `outputs.yaml` 决定（三态里的"没传"）。
  // 面板上刻意没有这个控件，理由写在 `PipelineDraft` 的注释里。
  return request;
}

/**
 * 预览那一行要说的话（`null` = 没什么要说的，因为**默认落点**下的正常情况就写在
 * 旁边那两行里了）。
 *
 * 分开写是因为它们回答的是不同的问题：`reason` 是"**为什么不能按**"（红），
 * `note` 是"按下去会做什么"（灰）。
 */
export function previewReason(preview: PipelineTask | null): string | null {
  if (preview === null) return null;
  if (preview.reason !== null) return preview.reason;
  if (!preview.has_script) {
    return "这个任务还没有生效稿件 —— 流水线**不写稿**，先到「稿件」面板出一版稿再来。";
  }
  return null;
}

/** 预览说能不能按。拿不到预览 ⇒ 不拦（真判据在后端，这里只是提前说一声）。 */
export function previewRunnable(preview: PipelineTask | null): boolean {
  if (preview === null) return true;
  return preview.runnable;
}

/** 预览是不是已经过时了（任务号 / 落点改过而没重新问）。 */
export function previewStale(preview: PipelineTask | null, draft: PipelineDraft): boolean {
  if (preview === null) return false;
  if (preview.task_id !== draft.taskId.trim()) return true;
  return draft.until !== "" && preview.until !== draft.until;
}

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

export const usePipelineStore = defineStore("pipeline", () => {
  const snapshot = ref<PipelineConsole | null>(null);
  const draft = ref<PipelineDraft>(emptyDraft());
  const preview = ref<PipelineTask | null>(null);
  const previewError = ref<string | null>(null);
  /** 进度卡盯着的任务（跑完之后它仍然是"刚才那条"，而不是立刻跳走） */
  const focusId = ref<string | null>(null);

  const loading = ref(false);
  const loadError = ref<string | null>(null);
  const busy = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);

  // ── 读 ──────────────────────────────────────────────────────────────

  const running = computed<PipelineJob | null>(() => snapshot.value?.active ?? null);
  const jobs = computed<readonly PipelineJob[]>(() => snapshot.value?.jobs ?? []);
  const untilOptions = computed(() => snapshot.value?.until_options ?? []);
  const voices = computed(() => snapshot.value?.voices ?? []);
  const engineReady = computed(() => snapshot.value?.engine_ready ?? false);

  /** 进度卡显示哪一条：在跑的优先，其次是刚跑完那条，最后是最近一条。 */
  const focusedJob = computed<PipelineJob | null>(() => {
    if (running.value !== null) return running.value;
    const list = jobs.value;
    return list.find((job) => job.id === focusId.value) ?? list[0] ?? null;
  });

  const problem = computed(() => validate(draft.value));
  const stale = computed(() => previewStale(preview.value, draft.value));

  /** 现在能不能按下「开始出片」；不能按的原因就是 `problem` / 预览说不行 / 引擎未就绪。 */
  const canSubmit = computed(() => {
    if (busy.value || snapshot.value === null) return false;
    if (!engineReady.value) return false;
    if (problem.value !== null) return false;
    return previewRunnable(preview.value);
  });

  // ── 轮询 ────────────────────────────────────────────────────────────

  let timer: ReturnType<typeof setTimeout> | null = null;
  let polling = false;

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function schedule(): void {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      void tick();
    }, PIPELINE_POLL_MS);
  }

  /** 只在"有活"与"没活"之间切换时动定时器；两者相同时**什么都不做**（否则每次刷新都重排一次）。 */
  function syncPolling(): void {
    const should = running.value !== null;
    if (should === polling) return;
    polling = should;
    if (should) schedule();
    else clearTimer();
  }

  async function tick(): Promise<void> {
    timer = null;
    if (!polling) return;
    await refresh();
    if (polling && timer === null) schedule();
  }

  /** 首屏拿不到就填一次下拉框默认值；**用户已经选过的不覆盖**。 */
  function applyDefaults(): void {
    const current = snapshot.value;
    if (current === null) return;
    if (draft.value.until === "") draft.value.until = current.default_until;
    if (draft.value.voice === "") {
      draft.value.voice = current.default_voice ?? current.voices[0]?.name ?? "";
    }
  }

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      snapshot.value = await api.fetchPipelineConsole();
      loadError.value = null;
      applyDefaults();
    } catch (failure) {
      // 旧快照**留着**：一屏数字不因一次抖动变空白（与渲染 / 四池同一条）。
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
    syncPolling();
  }

  // ── 动作 ────────────────────────────────────────────────────────────

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
  }

  /**
   * 问一次"这条任务点了会怎样"。
   *
   * 任务号空着就**不问**（那会换来一个 422），直接清掉上一条预览 —— 留着上一条的结论
   * 比没有更糟：它说的是**另一个任务号**的事。
   */
  async function ask(): Promise<void> {
    const taskId = draft.value.taskId.trim();
    if (taskId === "") {
      preview.value = null;
      previewError.value = null;
      return;
    }
    try {
      preview.value = await api.fetchPipelineTask(taskId, draft.value.until || null);
      previewError.value = null;
    } catch (failure) {
      preview.value = null;
      previewError.value = describeError(failure);
    }
  }

  /** 开一条（服务端立刻返回，活在工作线程里跑）。 */
  async function submit(): Promise<boolean> {
    const reason = problem.value;
    if (reason !== null) {
      error.value = reason;
      notice.value = null;
      return false;
    }
    busy.value = true;
    clearMessages();
    try {
      const result = await api.createPipelineJob(toRequest(draft.value));
      focusId.value = result.job.id;
      notice.value = result.deduped
        ? `任务 ${result.job.task_id} 已经有一条在跑了（${result.job.id}）—— 下面是它的实时进度，没有重复开第二条。`
        : `已登记 ${result.job.id}（任务 ${result.job.task_id}）—— 下面是它的实时进度。`;
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * 叫停。**协作式**，而且检查点比渲染少：只在配音的每一句与渲染的每一段。
   * 所以这里不说"已取消"，只说"已请求取消" —— 说满了就是在骗人。
   */
  async function cancel(jobId: string): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const job = await api.cancelPipelineJob(jobId);
      focusId.value = job.id;
      notice.value =
        job.status === "canceled"
          ? `${job.id} 已取消。`
          : `${job.id} 已收到取消请求 —— 它会在下一次进度回调处停下（配音的每一句 / 渲染的每一段）。停在"投递配音"或"拼母带"那几步之间时，要等它进到下一个回调点。`;
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 盯住某一条（最近任务表里点「看进度」）。 */
  async function focus(jobId: string): Promise<void> {
    focusId.value = jobId;
    clearMessages();
    try {
      const job = await api.fetchPipelineJob(jobId);
      focusId.value = job.id;
    } catch (failure) {
      error.value = describeError(failure);
    }
  }

  /** 把某条任务号填回表单（"照这条再来一次"）。 */
  function reuseJob(job: PipelineJob): void {
    draft.value.taskId = job.task_id;
    draft.value.until = job.until;
    const request = job.request;
    const voice = request["voice"];
    if (typeof voice === "string") draft.value.voice = voice;
    const seed = request["seed"];
    draft.value.seed = typeof seed === "number" ? String(seed) : "";
    clearMessages();
    notice.value = `已把 ${job.id} 的参数填回表单。`;
    void ask();
  }

  /** 换一份空白草稿。 */
  function resetDraft(): void {
    draft.value = emptyDraft();
    preview.value = null;
    previewError.value = null;
    clearMessages();
    applyDefaults();
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：重复调用只会有一次在途请求。 */
  function start(): void {
    void refresh();
  }

  function stop(): void {
    polling = false;
    clearTimer();
  }

  return {
    // 读
    snapshot,
    loading,
    loadError,
    running,
    jobs,
    untilOptions,
    voices,
    engineReady,
    focusedJob,
    // 表单
    draft,
    problem,
    stale,
    canSubmit,
    // 预览
    preview,
    previewError,
    ask,
    // 动作
    busy,
    error,
    notice,
    submit,
    cancel,
    focus,
    reuseJob,
    resetDraft,
    refresh,
    start,
    stop,
  };
});
