// 渲染面板状态（T4.6 · §04.2.8）。
//
// 这一屏要回答四个问题
// --------------------
// ① 拿什么参数出片？② 我这条跑到哪一步了？③ 跑出来的是什么、能不能马上看？
// ④ 中途不想跑了怎么办？
//
// 为什么是**轮询**而不是 WS
// ------------------------
// 出片任务的进度是**进程内状态**，它一行都不落 `system_logs` —— 而 `logs` 通道能看见的
// 只有落库的行。把它塞进 WS 要动 Hub 的协议与快照注册表，而这条链路的进度是**粗粒度**的
// （配音第 N 句 / 渲染中 / 完成），1 秒一次轮询完全够，且断线重连天然正确
// （重新 GET 一次就是最新状态）。真要改成推送，落点是"给 Hub 加一路 render.progress"，
// 而不是把轮询周期调小。
//
// 为什么**只在有活的时候**轮询
// --------------------------
// 空闲时一直拉，就是每秒钟一次"什么新东西都没有"的请求。代价不只是带宽：面板上那些
// 数字会一直跳，而它们跳动的唯一原因是"我们在定时问"，不是"真的有事发生"。
// 代价也说清楚：**别人在命令行跑的那一条不会自己冒出来**（那是另一个进程的登记表）。
// 所以空闲时留一颗「刷新」，而不是留一个假装看得见的轮询。
//
// 为什么"取消"要单独写一段话
// --------------------------
// 后端是**协作式**取消：排队中的立刻作废，已经在跑的会在下一次进度回调处停下，而
// ffmpeg 一旦跑起来要等它自己结束。不写这句话，用户按了取消看见进度条还在动，
// 会以为按钮坏了，然后反复按 —— 而反复按不会让它更快。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  cancelRenderJob,
  createRenderJob,
  fetchRenderConsole,
  type RenderConsole,
  type RenderJob,
  type RenderJobRequest,
} from "@/api/endpoints/render";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";

/** 有活在跑时的轮询周期。1 秒：够快（配音一句要几百毫秒）也不至于把后端问爆。 */
export const RENDER_POLL_MS = 1_000;

/** 文案上限的后备值（首屏拿不到 `max_speech_chars` 时用它；真值以服务端为准）。 */
export const MAX_SPEECH_CHARS_FALLBACK = 5_000;

/** 任务号长度上限（与后端 `RenderJobRequest.task_id` 的 `max_length` 同源）。 */
export const MAX_TASK_ID_CHARS = 64;

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface RenderApi {
  fetchRenderConsole: typeof fetchRenderConsole;
  createRenderJob: typeof createRenderJob;
  cancelRenderJob: typeof cancelRenderJob;
}

let api: RenderApi = { fetchRenderConsole, createRenderJob, cancelRenderJob };

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureRenderApi(overrides: Partial<RenderApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "渲染中",
  succeeded: "完成",
  failed: "失败",
  canceled: "已取消",
};

/** 状态 → 中文（面板上不出现裸英文状态码）。 */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

/** 状态 → 灯色。`running` 是 busy（还在动），`canceled` 是 warn（不是错，但也没成）。 */
export function statusTone(status: string): StatusTone {
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

/** 毫秒 → 人话（配音时长 / 成片时长都按秒看，小数点后两位没有意义）。 */
export function formatDurationMs(ms: number): string {
  if (ms <= 0) return "0 秒";
  const total = Math.round(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`;
}

/**
 * 一个**默认任务号**（`ui-YYYYMMDD-HHMMSS`）。
 *
 * 为什么给默认值而不是留空：任务号是这条片子与稿件 / 审计留痕的挂钩，空着提交必然 422。
 * 让用户为了"先跑一支看看"去自己想一个名字，是把一次纯粹的机械劳动推给人。
 * 时间戳保证同一秒内不会撞，且一眼看得出是哪一次。
 */
export function defaultTaskId(now: Date): string {
  const pad = (value: number): string => String(value).padStart(2, "0");
  const date = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
  const clock = `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `ui-${date}-${clock}`;
}

/** 表单草稿。数字项用**字符串**存：空串表示"不传这个参数"，而不是 NaN。 */
export interface RenderDraft {
  taskId: string;
  text: string;
  profile: string;
  voice: string;
  reuseVoice: boolean;
  seed: string;
  threads: string;
  /**
   * 烧不烧字幕。**三态**：`null` = 跟随 `outputs.yaml → subtitle.enabled`。
   *
   * 为什么不用 `boolean`：面板一提交就把它顶成 `false`，会把配置里开着的字幕关掉 ——
   * 而用户根本没碰这个框。复选框显示的是"生效值"（`null` 时取配置值），一勾一放
   * 才变成显式覆盖。
   */
  subtitle: boolean | null;
}

export function emptyDraft(now: Date): RenderDraft {
  return {
    taskId: defaultTaskId(now),
    text: "",
    profile: "",
    voice: "",
    reuseVoice: false,
    seed: "",
    threads: "",
    subtitle: null,
  };
}

/** 提交前自检。返回**人话**，`null` = 可以提交。 */
export function validate(draft: RenderDraft, maxChars: number): string | null {
  const taskId = draft.taskId.trim();
  if (taskId === "") return "任务号不能空：它是这条片子和稿件、审计留痕的挂钩。";
  if (taskId.length > MAX_TASK_ID_CHARS) return `任务号最长 ${MAX_TASK_ID_CHARS} 个字符。`;
  if (draft.text.length > maxChars) {
    return `口播文案最长 ${maxChars} 字，现在有 ${draft.text.length} 字。`;
  }
  if (draft.seed.trim() !== "" && !Number.isInteger(Number(draft.seed.trim()))) {
    return "随机种子要是一个整数（留空 = 每次随机挑素材）。";
  }
  const threads = draft.threads.trim();
  if (threads !== "" && (!Number.isInteger(Number(threads)) || Number(threads) < 1 || Number(threads) > 64)) {
    return "线程数要落在 1–64（留空 = 交给 ffmpeg 自己定）。";
  }
  return null;
}

function intOrNull(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const parsed = Number.parseInt(trimmed, 10);
  return Number.isNaN(parsed) ? null : parsed;
}

/**
 * 草稿 → 请求体。
 *
 * 空的可选项**一个都不发**（而不是发 `null`）：后端把"没传"和"传了 null"当同一件事，
 * 但发出去的空字段会让请求日志里全是 `"profile": null`，排查时看不出"用户到底选没选"。
 */
export function toRequest(draft: RenderDraft): RenderJobRequest {
  const request: RenderJobRequest = {
    task_id: draft.taskId.trim(),
    text: draft.text,
    reuse_voice: draft.reuseVoice,
  };
  if (draft.profile !== "") request.profile = draft.profile;
  if (draft.voice !== "") request.voice = draft.voice;
  const seed = intOrNull(draft.seed);
  if (seed !== null) request.seed = seed;
  const threads = intOrNull(draft.threads);
  if (threads !== null) request.threads = threads;
  // 三态：null 就**一个字段都不发**，让后端按配置决定
  if (draft.subtitle !== null) request.subtitle = draft.subtitle;
  return request;
}

/** 成片文件名（从结果里的绝对路径取末段）—— `<video>` 只认名字，不认整条路径。 */
export function resultVideoName(job: RenderJob): string | null {
  const final = job.result?.["final"];
  if (typeof final !== "string") return null;
  const name = final.split(/[\\/]/).pop() ?? "";
  return name === "" ? null : name;
}

/**
 * 降级原因 → 人话（T3.7 · §04.2.8.6）。
 *
 * 后端给的是**稳定标识符**（会进库、会被报表统计），面板负责把它翻成人话。
 * 不认识的标识符**原样显示**而不是吞掉 —— 后端加了新原因而前端还没跟上时，
 * 面板上出现一个 `composite_chunked` 也好过什么都不说。
 */
const DEGRADE_LABELS: Record<string, string> = {
  no_broll_assets: "没有跑酷底片 ⇒ 这次是纯黑底",
  render_720p: "正常档编码失败 ⇒ 换了 720P 保底档",
  composite_chunked: "滤镜图过大 ⇒ 分块合成",
};

/**
 * 成片的质检读数（T3.7）：`−16.48 LUFS / −1.12 dBTP`。没有读数 ⇒ `null`。
 *
 * 这个数取的是**成片自己**（后端 `mixdown.measure_file` 量落盘那一支），不是
 * `loudnorm` 的输入读数 —— 后者是"归一化之前有多响"，写出来会把人吓一跳。
 */
export function qualityReading(job: RenderJob): string | null {
  const loudness = job.result?.["output_loudness"];
  if (typeof loudness !== "object" || loudness === null) return null;
  const lufs = (loudness as Record<string, unknown>)["input_i"];
  const peak = (loudness as Record<string, unknown>)["input_tp"];
  if (typeof lufs !== "number" || typeof peak !== "number") return null;
  return `${lufs.toFixed(2)} LUFS / ${peak.toFixed(2)} dBTP`;
}

/**
 * 跑完之后**必须**让人看见的那一句：这次是不是降级出片。`null` = 正常档出片，没什么要说的。
 *
 * 为什么非要说：降级出片**不会失败**（这正是它的设计目的），所以除了这一句话，用户
 * 没有任何别的线索能知道"我这条片子画质其实打了折"。不说 = 悄悄降级。
 */
export function qualityNotice(job: RenderJob): string | null {
  if (job.status !== "succeeded" || job.result === null) return null;
  const reading = qualityReading(job);
  const parts: string[] = [];
  if (job.result["degraded"] === true) {
    const reason = job.result["degrade_reason"];
    const label =
      typeof reason === "string" ? (DEGRADE_LABELS[reason] ?? reason) : "原因未记录";
    parts.push(`这条是**降级出片**：${label}。片子能发，但画质与正常档不一样。`);
  }
  if (reading !== null) parts.push(`成片实测 ${reading}`);
  return parts.length === 0 ? null : parts.join(" · ");
}

/** 水印这次的处置（贴 / 跳过 + 为什么）。`null` = 会贴，没什么要说的。 */
export function watermarkNotice(console_: RenderConsole): string | null {
  if (console_.watermark_enabled) return null;
  return console_.watermark_hint ?? "这次出片不带水印（水印是可选装饰，不阻塞出片）。";
}

/**
 * 字幕这次的处置。`null` = 会烧，也没什么要补充的。
 *
 * 与 `watermarkNotice` 分开写，是因为字幕**多了一种状态**：字体缺失时"跳过"是必然的，
 * 而"用了系统字体"这类合规说明即使字幕会烧也要说出来。
 */
export function subtitleNotice(console_: RenderConsole): string | null {
  if (console_.subtitle_enabled) return console_.subtitle_hint ?? null;
  return console_.subtitle_hint ?? "这次出片不带字幕（字幕是可选层，不阻塞出片）。";
}

/** 复选框该显示成什么：用户显式选过就用他选的，否则用配置里的生效值。 */
export function subtitleEffective(draft: RenderDraft, console_: RenderConsole | null): boolean {
  if (draft.subtitle !== null) return draft.subtitle;
  return console_?.subtitle_enabled ?? true;
}

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

export const useRenderStore = defineStore("render", () => {
  const snapshot = ref<RenderConsole | null>(null);
  const draft = ref<RenderDraft>(emptyDraft(new Date()));
  /** 进度卡盯着的任务（跑完之后它仍然是"刚才那条"，而不是立刻跳走） */
  const focusId = ref<string | null>(null);

  const loading = ref(false);
  const loadError = ref<string | null>(null);
  const busy = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);

  // ── 读 ──────────────────────────────────────────────────────────────

  const running = computed<RenderJob | null>(() => snapshot.value?.running ?? null);
  const jobs = computed<readonly RenderJob[]>(() => snapshot.value?.jobs ?? []);
  const videos = computed(() => snapshot.value?.videos ?? []);
  const profiles = computed(() => snapshot.value?.profiles ?? []);
  const voices = computed(() => snapshot.value?.voices ?? []);
  const maxChars = computed(() => snapshot.value?.max_speech_chars ?? MAX_SPEECH_CHARS_FALLBACK);

  /** 进度卡显示哪一条：在跑的优先，其次是刚跑完那条，最后是最近一条。 */
  const focusedJob = computed<RenderJob | null>(() => {
    if (running.value !== null) return running.value;
    const list = jobs.value;
    const focused = list.find((job) => job.id === focusId.value);
    return focused ?? list[0] ?? null;
  });

  const problem = computed(() => validate(draft.value, maxChars.value));

  const engineReady = computed(() => snapshot.value?.engine_ready ?? false);

  /** 复选框的**生效值**：用户显式选过就用他选的，否则用配置里的值。 */
  const subtitleEnabled = computed(() => subtitleEffective(draft.value, snapshot.value));

  /** 现在能不能按下「出片」；不能按的原因就是 `problem` / 引擎未就绪。 */
  const canSubmit = computed(() => {
    if (busy.value || snapshot.value === null) return false;
    if (!engineReady.value) return false;
    return problem.value === null;
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
    }, RENDER_POLL_MS);
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
    if (draft.value.profile === "") draft.value.profile = current.default_profile;
    if (draft.value.voice === "") draft.value.voice = current.voices[0]?.name ?? "";
  }

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      snapshot.value = await api.fetchRenderConsole();
      loadError.value = null;
      applyDefaults();
    } catch (failure) {
      // 旧快照**留着**：一屏数字不因一次抖动变空白（与总览台 / 四池同一条）。
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
      const job = await api.createRenderJob(toRequest(draft.value));
      focusId.value = job.id;
      notice.value = `已登记 ${job.id}（任务 ${job.task_id}）—— 下面是它的实时进度。`;
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
   * 叫停。**协作式**：排队中的立刻作废，在跑的等它走到下一个检查点。
   * 所以这里不说"已取消"，只说"已请求取消" —— 说满了就是在骗人。
   */
  async function cancel(jobId: string): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const job = await api.cancelRenderJob(jobId);
      focusId.value = job.id;
      notice.value =
        job.status === "canceled"
          ? `${job.id} 已取消。`
          : `${job.id} 已收到取消请求 —— 当前这一句配音或这一次编码会先跑完，进度条停在这里是正常的。`;
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 把某条任务的参数填回表单（"照这样再来一条"）。文案不填 —— 请求摘要里只有前 60 字。 */
  function reuseJob(job: RenderJob): void {
    const request = job.request;
    draft.value.taskId = defaultTaskId(new Date());
    const profile = request["profile"];
    if (typeof profile === "string") draft.value.profile = profile;
    const voice = request["voice"];
    if (typeof voice === "string") draft.value.voice = voice;
    const reuseVoice = request["reuse_voice"];
    draft.value.reuseVoice = reuseVoice === true;
    const seed = request["seed"];
    draft.value.seed = typeof seed === "number" ? String(seed) : "";
    const threads = request["threads"];
    draft.value.threads = typeof threads === "number" ? String(threads) : "";
    const subtitle = request["subtitle"];
    draft.value.subtitle = typeof subtitle === "boolean" ? subtitle : null;
    clearMessages();
    notice.value = `已把 ${job.id} 的参数填回表单（文案要自己补：请求摘要里只有前 60 字）。`;
  }

  /** 换一份空白草稿（任务号重新取时间戳）。 */
  function resetDraft(): void {
    draft.value = emptyDraft(new Date());
    clearMessages();
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
    videos,
    profiles,
    voices,
    maxChars,
    subtitleEnabled,
    focusedJob,
    engineReady,
    // 表单
    draft,
    problem,
    canSubmit,
    // 动作
    busy,
    error,
    notice,
    submit,
    cancel,
    reuseJob,
    resetDraft,
    refresh,
    start,
    stop,
  };
});