// 配音面板状态（T4.5 · §04.3.7）。
//
// 这一屏要回答四个问题
// --------------------
// ① 这条任务配到哪一步了？② 哪一句出了岔子、为什么？③ 想换一个人的嗓子怎么办？
// ④ 某一句念错了，怎么单独重念？
//
// 为什么"换音色"要**先问代价再动手**
// ----------------------------------
// 后端把它拆成两次请求：第一次不带 `confirm`，服务端**先算**"会让 N 句重新配音"然后抛
// 409（`VOICE_MAP_CONFIRM_REQUIRED`），第二次带 `confirm: true` 才真写。面板照抄这个
// 形状：`requestVoiceChange()` 拿到 409 就把 `context.affected` 摆进确认框，用户点头才
// `confirmVoiceChange()`。少了这一步，一次误点会重配几十句，而用户没有后悔的机会 ——
// 重配不是免费的（每句一次 TTS，收口时还要全量重算一次时间轴）。
//
// 为什么 `busy` 必须显示出来
// --------------------------
// 换音色时**正被某个 worker 攥着**的那几句动不了（后端不去和它抢），它们会出现在
// `report.busy` 里。不显示的话，用户看到"换音色成功"，而成片里那几句还是旧嗓子 ——
// 这是最难查的一类（陷阱 #117）。
//
// 为什么是**轮询**而不是 WS
// -------------------------
// 契约 §04.4.3 有 `sentence.updated` 这条事件，但后端**目前没有生产者**（只有 `metrics`
// 与 `persona` 两路会 publish）。面板要的是"这一句念完了没有"，1 秒一次 GET 就能拿到，
// 且断线重连天然正确（重新 GET 一次就是最新状态）。真要做推送，落点是"给配音收口加一路
// publish"，而不是把轮询周期调小 —— 现在先按与渲染面板同一口径走。
//
// 只在**有活**的时候轮询
// ----------------------
// 空闲时一直拉，就是每秒钟一次"什么新东西都没有"的请求，面板上那些数字跳动的唯一原因
// 会变成"我们在定时问"。判据是 `pending + synthesizing > 0`：失败与跳过都是**定局**，
// 不会自己变，不该让定时器空转。
//
// 音色下拉框只在**首屏与手动刷新**时拉
// ------------------------------------
// 轮询只拉逐句列表：音色清单是"本机装了什么"，它不会因为某一句念完而变。把它塞进每秒
// 一次的轮询里，等于每秒重画一次下拉框（而重画会把用户正在选的框弹回原位）。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  createVoicePreview,
  fetchSentences,
  fetchVoiceOptions,
  fetchVoicePreview,
  patchVoiceMap,
  resynthSentence,
  type SentenceProgress,
  type SentenceVoice,
  type SentenceVoiceList,
  type VoiceMapRequest,
  type VoiceMapResponse,
  type VoiceOption,
  type VoiceOptions,
  type VoicePreview,
} from "@/api/endpoints/voice";
import { ApiError } from "@/api/http";
import { describeError } from "@/stores/overview";
import { formatDurationMs } from "@/stores/render";
import type { StatusTone } from "@/components/tone";

/** 有活在跑时的轮询周期。1 秒：够快（一句几百毫秒）也不至于把后端问爆。 */
export const VOICE_POLL_MS = 1_000;

/** 等试听样本生成时的轮询周期。1 秒：真机上一次十几秒，够快也不至于把后端问爆。 */
export const PREVIEW_POLL_MS = 1_000;

/**
 * 等试听样本的上限。**两分钟**：真机实测冷加载 20s + 推理十几秒，正常远到不了；
 * 到点就如实说"还在生成"，而不是永远转圈（转圈的按钮看起来像坏了）。
 */
export const PREVIEW_TIMEOUT_MS = 120_000;

/** 任务号长度上限（与后端 `RenderJobRequest.task_id` 同源）。 */
export const MAX_TASK_ID_CHARS = 64;

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface VoiceApi {
  fetchSentences: typeof fetchSentences;
  fetchVoiceOptions: typeof fetchVoiceOptions;
  resynthSentence: typeof resynthSentence;
  patchVoiceMap: typeof patchVoiceMap;
  fetchVoicePreview: typeof fetchVoicePreview;
  createVoicePreview: typeof createVoicePreview;
}

let api: VoiceApi = {
  fetchSentences,
  fetchVoiceOptions,
  resynthSentence,
  patchVoiceMap,
  fetchVoicePreview,
  createVoicePreview,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureVoiceApi(overrides: Partial<VoiceApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

const STATUS_LABELS: Record<string, string> = {
  pending: "待配音",
  synthesizing: "正在配",
  done: "已配好",
  skipped: "已跳过",
  failed: "失败",
};

/** 逐句状态 → 中文（面板上不出现裸英文状态码）。 */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

/** 逐句状态 → 灯色。跳过是 warn（不是错，但那句没声音）、正在配是 busy。 */
export function statusTone(status: string): StatusTone {
  switch (status) {
    case "done":
      return "ok";
    case "failed":
      return "error";
    case "skipped":
      return "warn";
    case "synthesizing":
      return "busy";
    default:
      return "idle";
  }
}

const SOURCE_LABELS: Record<string, string> = {
  profile: "参考音",
  sapi: "系统音色",
};

/** 音色来源 → 人话（`sapi` / `profile` 是后端的词，不是用户能看懂的东西）。 */
export function voiceSourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

const PREVIEW_LABELS: Record<string, string> = {
  missing: "生成试听",
  running: "生成中…",
  ready: "试听",
  failed: "重新生成",
};

/**
 * 试听按钮上的字（四态各一句）。
 *
 * `missing` 与 `failed` **必须分开**：前者点一下就行，后者再点一下大概率还是失败
 * —— 得先看那句话。都写成"试听"，用户会反复点一个注定失败的按钮。
 */
export function previewLabel(state: string): string {
  return PREVIEW_LABELS[state] ?? "试听";
}

/**
 * 试听样本现在是什么状况（按钮的 `title`）。
 *
 * `engine` 一定要说出来：参考音是**零样本复刻**，同一段文本换个引擎念出来是**两个人的
 * 嗓子**。听出来"不像"的时候，第一件要确认的就是"这一份是谁念的"。
 */
export function previewHint(state: string, engine: string | null): string {
  switch (state) {
    case "ready":
      return engine === null ? "播这个音色的试听样本" : `试听样本（由 ${engine} 念的）`;
    case "running":
      return "正在生成试听样本（真机上一次十几秒）—— 好了会自动播";
    case "failed":
      return "上一次生成失败了，点一下重试；失败原因在下面那行红字里";
    default:
      return "还没有试听样本 —— 点一下让当前引擎念一句（十几秒），好了会自动播";
  }
}

/**
 * 下拉框里那一行：`Microsoft Huihui Desktop · 系统音色`。
 *
 * `speakable === false` 的照旧列出来（用户要知道自己入库的参考音还在），但**必须
 * 写明它现在发不出声** —— 选它的后果是"每一句都降级成静音"，而成片没人声这件事
 * 要到播放时才发现。`source` 里的"参考音"三个字不够：那听起来只是"另一种音色"。
 */
export function voiceOptionLabel(option: VoiceOption): string {
  const base = `${option.id} · ${voiceSourceLabel(option.source)}`;
  return option.speakable ? base : `${base} · 当前引擎念不出来`;
}

/**
 * 逐句进度 → 一行字（`已定局 3/5 · 待配 2`）。
 *
 * 计数为 0 的那几项**不出现**：写一串 `0` 只是噪音，而"跳过 0"尤其容易被读成
 * "跳过这一项没统计"。
 */
export function progressText(progress: SentenceProgress): string {
  const parts = [`已定局 ${progress.settled}/${progress.total}`];
  if (progress.synthesizing > 0) parts.push(`正在配 ${progress.synthesizing}`);
  if (progress.pending > 0) parts.push(`待配 ${progress.pending}`);
  if (progress.skipped > 0) parts.push(`跳过 ${progress.skipped}`);
  if (progress.failed > 0) parts.push(`失败 ${progress.failed}`);
  return parts.join(" · ");
}

/** 还有没有"会自己变"的句子（决定轮询开不开）。失败与跳过都是**定局**。 */
export function outstanding(progress: SentenceProgress | null): number {
  return progress === null ? 0 : progress.pending + progress.synthesizing;
}

/** 一句"没配成"的记录（面板要**逐条**说清，不能只显示一个计数）。 */
export interface SkipNote {
  seq: number;
  speaker: string;
  reason: string;
}

/**
 * 跳过 / 失败的句子 + 原因。
 *
 * 为什么要单独列出来：`skipped` 是**降级**（这一句没声音，片子照样出），它不会让任务
 * 失败。只给一个"跳过 2"的计数，用户永远不知道是哪两句、为什么 —— 而原因
 * （`tts_error`）就躺在库里，不发出来等于没有。
 */
export function skipNotes(sentences: readonly SentenceVoice[]): SkipNote[] {
  return sentences
    .filter((row) => row.tts_status === "skipped" || row.tts_status === "failed")
    .map((row) => ({
      seq: row.seq,
      speaker: row.speaker,
      reason: row.tts_error ?? "库里没有记原因",
    }));
}

/**
 * 时长 → 人话；没念过 ⇒ `-`（不是"0 秒"：0 秒会被读成"念了个空的"）。
 *
 * 形参收 `undefined`：契约里带默认值的字段在生成的类型里是可选的（`tts_duration_ms?`），
 * 而"没给"与"给了 null"在这里是同一件事 —— 都是"这一句还没念过"。
 */
export function durationText(ms: number | null | undefined): string {
  return ms === null || ms === undefined || ms <= 0 ? "-" : formatDurationMs(ms);
}

/** 时间轴位置 → `1234 → 5678 ms`；还没定局 ⇒ `-`。 */
export function spanText(row: SentenceVoice): string {
  if (row.start_ms === null || row.end_ms === null) return "-";
  return `${row.start_ms} → ${row.end_ms} ms`;
}

/** 逐句列表里出现过的角色（**按首次出现顺序**去重：面板的顺序要跟稿子一致）。 */
export function speakersOf(sentences: readonly SentenceVoice[]): string[] {
  const seen: string[] = [];
  for (const row of sentences) {
    if (!seen.includes(row.speaker)) seen.push(row.speaker);
  }
  return seen;
}

/** 音色草稿：每个角色一个下拉框；库里没有映射的角色落到**第一个可用音色**。 */
export function voiceDraft(
  voiceMap: Readonly<Record<string, string>>,
  speakers: readonly string[],
  fallback: string,
): Record<string, string> {
  const draft: Record<string, string> = {};
  for (const speaker of speakers) draft[speaker] = voiceMap[speaker] ?? fallback;
  return draft;
}

/** 音色映射表上的一行：库里记着什么 / 下拉框里选着什么 / 本机找不找得到。 */
export interface VoiceMapRow {
  speaker: string;
  current: string;
  was: string;
  unknown: boolean;
}

/**
 * 每个角色一行。
 *
 * 为什么要单算"本机找不到"：库里的映射是一串**字符串**，它指向的音色可能在别的机器上
 * （参考音没入库、系统语音包没装）。这时候 `<select>` 会因为"没有匹配的 option"显示成
 * 空白 —— 而空白会被读成"这个角色没有音色"，用户就找不到北了。
 * 真机演练里就撞上了：库里的映射是 `bigbear`，本机只有 `bear_da` / `bear_xiong`
 * 与三个 SAPI 音色。
 */
export function voiceMapRows(
  speakers: readonly string[],
  draft: Readonly<Record<string, string>>,
  voiceMap: Readonly<Record<string, string>>,
  voices: readonly VoiceOption[],
): VoiceMapRow[] {
  const known = new Set(voices.map((option) => option.id));
  return speakers.map((speaker) => {
    const current = draft[speaker] ?? "";
    return {
      speaker,
      current,
      was: voiceMap[speaker] ?? "",
      unknown: current !== "" && !known.has(current),
    };
  });
}

/** 草稿与库里的映射**不一样**的那些角色（换音色只提交它们）。 */
export function changedSpeakers(
  voiceMap: Readonly<Record<string, string>>,
  draft: Readonly<Record<string, string>>,
): string[] {
  return Object.keys(draft).filter((speaker) => draft[speaker] !== voiceMap[speaker]);
}

/**
 * 草稿 → 请求体：**只发真正改过的角色**。
 *
 * 为什么不是把整张表发上去：后端的 PATCH 语义是"只改我点到的这几个"，而它会校验**提交里
 * 出现的**每一个音色（§04.3.7 的两条线：人刚填的那个字符串必须存在）。把整张表发上去，
 * 等于顺手替用户断言了他根本没碰过的那些行 —— 而其中可能正躺着一条本机找不到的存量值
 * （`TaskPayload.voice_map` 的默认值就是逻辑角色名 `bigbear`），于是"我只想换熊大"会被
 * 一句"熊二的音色不存在"挡回来。
 *
 * 空值也**一个都不发**：后端要 `min_length=1`，发空串只会换来 422。
 */
export function toVoiceMapRequest(
  draft: Readonly<Record<string, string>>,
  voiceMap: Readonly<Record<string, string>>,
  confirm: boolean,
): VoiceMapRequest {
  const changed: Record<string, string> = {};
  for (const speaker of changedSpeakers(voiceMap, draft)) {
    const voice = draft[speaker] ?? "";
    if (voice !== "") changed[speaker] = voice;
  }
  return { voice_map: changed, confirm };
}

/** 二次确认框要显示的东西（从 409 的错误信封里取）。 */
export interface ConfirmPrompt {
  message: string;
  affected: number;
  total: number;
  speakers: string[];
  sentences: number[];
}

/**
 * 从 409 的错误信封里取"这次要重配多少句"。取不到 ⇒ `null`（说明是别的 409）。
 *
 * 判据是**错误码**而不是状态码：409 这条路上还有别的码（比如"池暂停了"），
 * 只看状态码会把它们也弹成"换音色确认框"。
 */
export function confirmPrompt(reason: unknown): ConfirmPrompt | null {
  if (!(reason instanceof ApiError) || reason.status !== 409) return null;
  const envelope = reason.detail;
  if (envelope === null || typeof envelope !== "object") return null;
  const bag = envelope as Record<string, unknown>;
  if (bag["code"] !== "VOICE_MAP_CONFIRM_REQUIRED") return null;
  const context = bag["context"];
  if (context === null || typeof context !== "object") return null;
  const fields = context as Record<string, unknown>;
  const affected = fields["affected"];
  if (typeof affected !== "number") return null;
  return {
    message: typeof bag["message"] === "string" ? bag["message"] : "换音色会让若干句重新配音",
    affected,
    total: typeof fields["total"] === "number" ? fields["total"] : 0,
    speakers: Array.isArray(fields["speakers"]) ? fields["speakers"].map(String) : [],
    sentences: Array.isArray(fields["sentences"]) ? fields["sentences"].map(Number) : [],
  };
}

/** `busy`（句子 id）⇒ 第几号句（面板上给人看的不是 id）。 */
export function busySeqs(sentences: readonly SentenceVoice[], ids: readonly string[]): number[] {
  const wanted = new Set(ids);
  return sentences.filter((row) => wanted.has(row.id)).map((row) => row.seq);
}

/** 换音色之后要说的那一句话（"动了多少句"）。 */
export function changeNotice(report: VoiceMapResponse): string {
  if (report.affected === 0) {
    return "映射写好了，但**没有句子需要重配**（这些角色名下还没有能失效的句子）。";
  }
  return `已重排 ${report.requeued} 句（影响 ${report.affected} 句，新建 ${report.created_jobs} 条作业）。`;
}

/**
 * "哪几句没动成" —— 没有 ⇒ `null`。
 *
 * 单独一个函数而不是拼进 `changeNotice`：它是**警告**，而绿底横幅上写一句警告，
 * 人是不会当回事的。两种消息在面板上的颜色必须不一样。
 */
export function busyNotice(
  report: VoiceMapResponse,
  sentences: readonly SentenceVoice[],
): string | null {
  const busy = busySeqs(sentences, report.busy ?? []);
  if (busy.length === 0) return null;
  return `第 ${busy.join("、")} 句正被念着，这次**没有动它们** —— 等它跑完再单独重配那几句。`;
}

const TASK_ID_SHAPE = /^[0-9A-Za-z_-]+$/;

/** 提交前自检。返回**人话**，`null` = 可以用。 */
export function validateTaskId(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === "") return "任务号不能空：它是这条片子、稿件与逐句配音的挂钩。";
  if (trimmed.length > MAX_TASK_ID_CHARS) return `任务号最长 ${MAX_TASK_ID_CHARS} 个字符。`;
  if (!TASK_ID_SHAPE.test(trimmed)) {
    return "任务号只接受字母、数字、连字符与下划线 —— 换音色与试听要把任务号拼进 URL 路径（§04.3.7）。";
  }
  return null;
}

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

/** 睡一会儿（试听轮询用）。抽出来是为了让 `previewVoice` 的主线读起来还是一件事。 */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}


export const useVoiceStore = defineStore("voice", () => {
  /** 面板上那个任务号输入框（用户直接改它）。 */
  const taskId = ref("");
  const snapshot = ref<SentenceVoiceList | null>(null);
  const options = ref<VoiceOptions | null>(null);
  /** 每个角色当前选的音色（换音色提交的就是它）。 */
  const draft = ref<Record<string, string>>({});
  /** 待用户点头的二次确认（`null` = 没有确认框）。 */
  const confirm = ref<ConfirmPrompt | null>(null);
  /** 后端关于时间轴的那句话（重配 / 换音色之后才出现）。 */
  const hint = ref<string | null>(null);
  /** 最近一次试听的样本状态（`null` = 这次进面板还没试听过）。 */
  const preview = ref<VoicePreview | null>(null);
  /** 正在生成 / 试听哪个音色（`null` = 没有在途的）。 */
  const previewId = ref<string | null>(null);
  const previewBusy = ref(false);
  const previewError = ref<string | null>(null);

  const loading = ref(false);
  const loadError = ref<string | null>(null);
  const busy = ref(false);
  const busyId = ref<string | null>(null);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);
  /** 警告横幅（"有几句没动成"这类）—— 与 `notice` 分开，因为颜色不一样。 */
  const warn = ref<string | null>(null);

  // ── 读 ──────────────────────────────────────────────────────────────

  const sentences = computed<readonly SentenceVoice[]>(() => snapshot.value?.sentences ?? []);
  const progress = computed<SentenceProgress | null>(() => snapshot.value?.progress ?? null);
  const voiceMap = computed<Record<string, string>>(() => snapshot.value?.voice_map ?? {});
  const taskStatus = computed(() => snapshot.value?.task_status ?? "");
  const timelineStale = computed(() => snapshot.value?.timeline_stale ?? false);
  const timelineTotalMs = computed(() => snapshot.value?.timeline_total_ms ?? null);
  const voices = computed<readonly VoiceOption[]>(() => options.value?.voices ?? []);
  const voiceNote = computed(() => options.value?.note ?? null);
  const speakers = computed(() => speakersOf(sentences.value));
  const changes = computed(() => changedSpeakers(voiceMap.value, draft.value));
  const skips = computed(() => skipNotes(sentences.value));
  const settled = computed(() => progress.value?.settled ?? 0);
  const total = computed(() => progress.value?.total ?? 0);
  const anyBusy = computed(() => busy.value || busyId.value !== null);
  const problem = computed(() => validateTaskId(taskId.value));
  const canLoad = computed(() => problem.value === null && !loading.value);

  /** 某个音色现在有没有试听样本（下拉框那一行按它画按钮）。 */
  function previewState(voiceId: string): string {
    return voices.value.find((option) => option.id === voiceId)?.preview_state ?? "missing";
  }

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
    }, VOICE_POLL_MS);
  }

  /** 只在"有活"与"没活"之间切换时动定时器；两者相同时**什么都不做**。 */
  function syncPolling(): void {
    const should = outstanding(progress.value) > 0;
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

  // ── 草稿 ────────────────────────────────────────────────────────────

  /** 这份草稿是给哪个任务号建的 —— 换了任务号就重建，同一个任务号下**不覆盖**用户的选择。 */
  let draftTaskId: string | null = null;

  function rebuildDraft(): void {
    draft.value = voiceDraft(
      voiceMap.value,
      speakers.value,
      options.value?.voices[0]?.id ?? "",
    );
    draftTaskId = taskId.value.trim();
  }

  function syncDraft(): void {
    if (draftTaskId !== taskId.value.trim()) rebuildDraft();
  }

  // ── 拉取 ────────────────────────────────────────────────────────────

  /** 逐句列表（**轮询走的就是它**：音色清单不会因为某一句念完而变）。 */
  async function refresh(): Promise<void> {
    const reason = validateTaskId(taskId.value);
    if (reason !== null) {
      loadError.value = reason;
      syncPolling();
      return;
    }
    loading.value = true;
    try {
      snapshot.value = await api.fetchSentences(taskId.value.trim());
      loadError.value = null;
      syncDraft();
    } catch (failure) {
      // 旧快照**留着**：一屏数字不因一次抖动变空白（与总览台 / 渲染面板同一条）。
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
    syncPolling();
  }

  /** 首屏 / 手动刷新：逐句 + 音色下拉框。两个端点**并行** —— 串起来只会让第一帧慢一倍。 */
  async function reload(): Promise<void> {
    const reason = validateTaskId(taskId.value);
    if (reason !== null) {
      loadError.value = reason;
      snapshot.value = null;
      options.value = null;
      draftTaskId = null;
      syncPolling();
      return;
    }
    const wanted = taskId.value.trim();
    loading.value = true;
    try {
      const [list, available] = await Promise.all([
        api.fetchSentences(wanted),
        api.fetchVoiceOptions(wanted),
      ]);
      snapshot.value = list;
      options.value = available;
      loadError.value = null;
      // 换了任务号 ⇒ 重建草稿；同一个任务号 ⇒ 保留用户已经选好的框。
      if (draftTaskId !== wanted) rebuildDraft();
    } catch (failure) {
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
    syncPolling();
  }

  /** 换一个任务号：旧快照立刻清掉（留着会让下一屏显示上一个任务的句子）。 */
  function setTaskId(value: string): void {
    taskId.value = value;
    draftTaskId = null;
    confirm.value = null;
  }

  // ── 动作 ────────────────────────────────────────────────────────────

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
    warn.value = null;
  }

  /** 重配一句。`can_resynth` 为假的行**不发请求**：那一定是 409，白跑一趟。 */
  async function resynth(row: SentenceVoice): Promise<boolean> {
    if (!row.can_resynth) {
      error.value = `第 ${row.seq} 句正被念着（synthesizing），现在动不了它 —— 等它跑完再点。`;
      notice.value = null;
      return false;
    }
    busyId.value = row.id;
    clearMessages();
    try {
      const report = await api.resynthSentence(row.id);
      hint.value = report.hint;
      notice.value = `第 ${report.seq} 句已退回待办（${statusLabel(report.status)}），作业排回 voice 池 —— 念完会自己出现在列表里。`;
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busyId.value = null;
    }
  }

  /** 把后端给的新映射写进快照与草稿（两处一起改，面板才不会显示成"没换成"）。 */
  function applyVoiceMap(report: VoiceMapResponse): void {
    const current = snapshot.value;
    if (current !== null) {
      snapshot.value = {
        ...current,
        voice_map: report.voice_map,
        progress: report.progress,
        timeline_stale: report.timeline_stale,
        timeline_total_ms: report.timeline_total_ms,
      };
    }
    draft.value = voiceDraft(report.voice_map, speakers.value, options.value?.voices[0]?.id ?? "");
    hint.value = report.hint;
    warn.value = busyNotice(report, sentences.value);
  }

  /**
   * 换音色（第一步：**问代价**）。
   *
   * 不带 `confirm` ⇒ 后端要么直接写完（没有句子需要重配），要么抛 409 让我们弹确认框。
   * 没有改动就不发请求：一次"提交"什么都没改，只会让人以为"按了没反应"。
   */
  async function requestVoiceChange(): Promise<boolean> {
    if (changes.value.length === 0) {
      clearMessages();
      notice.value = "音色映射没有改动（每个角色都还是现在用的那一个）。";
      return false;
    }
    busy.value = true;
    clearMessages();
    try {
      const report = await api.patchVoiceMap(
        taskId.value.trim(),
        toVoiceMapRequest(draft.value, voiceMap.value, false),
      );
      confirm.value = null;
      applyVoiceMap(report);
      notice.value = changeNotice(report);
      await refresh();
      return true;
    } catch (failure) {
      const prompt = confirmPrompt(failure);
      if (prompt !== null) {
        confirm.value = prompt;
        return false;
      }
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 换音色（第二步：用户点了头，带 `confirm` 真写下去）。 */
  async function confirmVoiceChange(): Promise<boolean> {
    if (confirm.value === null) return false;
    busy.value = true;
    clearMessages();
    try {
      const report = await api.patchVoiceMap(
        taskId.value.trim(),
        toVoiceMapRequest(draft.value, voiceMap.value, true),
      );
      confirm.value = null;
      applyVoiceMap(report);
      notice.value = changeNotice(report);
      await refresh();
      return true;
    } catch (failure) {
      confirm.value = null;
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 用户在确认框上点了"算了"。 */
  function cancelVoiceChange(): void {
    confirm.value = null;
  }

  /**
   * 试听一个音色：盘上没有就先让它生成（真机上一次十几秒），好了返回可播的 url。
   *
   * 为什么要在这里轮询、而不是让面板自己轮
   * --------------------------------------
   * "生成中"这件事要跨按钮重绘（用户可能已经点去改别的框了），状态必须落在 store 里；
   * 面板只负责拿 url 去喂 `<audio>`。
   *
   * 返回 `null` = 没成（原因在 `previewError` 里）。**不抛** —— 调用方是按钮的点击
   * 处理器，抛出去只会在控制台留一行没人看的栈。
   */
  async function previewVoice(voiceId: string): Promise<string | null> {
    if (voiceId.trim() === "") {
      previewError.value = "这个角色还没有选音色 —— 先在左边那个下拉框里挑一个。";
      return null;
    }
    previewId.value = voiceId;
    previewBusy.value = true;
    previewError.value = null;
    try {
      let sample = await api.fetchVoicePreview(voiceId);
      // 只有 missing / failed 才去点生成：running 是"别人已经点过了"，再 POST 一次
      // 也没坏处（后端幂等），但会白多一个请求。
      if (sample.status !== "ready" && sample.status !== "running") {
        sample = await api.createVoicePreview(voiceId);
      }
      const deadline = Date.now() + PREVIEW_TIMEOUT_MS;
      while (sample.status === "running" && Date.now() < deadline) {
        await sleep(PREVIEW_POLL_MS);
        sample = await api.fetchVoicePreview(voiceId);
      }
      preview.value = sample;
      applyPreview(sample);
      // `url` 在契约里是可选的 ⇒ `undefined` 与 `null` 都当"没有"（不写 `!== null`：
      // 那会让 `undefined` 漏过去，返回一个 `undefined` 给 `<audio src>`）。
      if (sample.status === "ready" && sample.url) return sample.url;
      previewError.value =
        sample.error ??
        (sample.status === "running"
          ? "试听样本还在生成（超过两分钟）—— 面板上那颗按钮会继续显示「生成中」，过一会儿再点一次。"
          : "试听样本没生成出来。");
      return null;
    } catch (failure) {
      previewError.value = describeError(failure);
      return null;
    } finally {
      previewBusy.value = false;
      previewId.value = null;
    }
  }

  /** 把样本状态写回下拉框那一行（不写的话按钮会一直显示"生成试听"）。 */
  function applyPreview(sample: VoicePreview): void {
    const current = options.value;
    if (current === null) return;
    options.value = {
      ...current,
      voices: current.voices.map((option) =>
        option.id === sample.voice_id
          ? { ...option, preview_state: sample.status, preview_url: sample.url ?? null }
          : option,
      ),
    };
  }

  /** 把草稿退回库里的映射（"我改了几个框，还是别动了"）。 */
  function resetDraft(): void {
    confirm.value = null;
    rebuildDraft();
    clearMessages();
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：重复调用只会有一次在途请求。任务号还空着就**不发请求**（那不是错误，是还没填）。 */
  function start(): void {
    if (taskId.value.trim() === "") return;
    void reload();
  }

  function stop(): void {
    polling = false;
    clearTimer();
  }

  return {
    // 输入
    taskId,
    setTaskId,
    // 读
    snapshot,
    options,
    sentences,
    progress,
    voiceMap,
    taskStatus,
    timelineStale,
    timelineTotalMs,
    voices,
    voiceNote,
    speakers,
    changes,
    skips,
    settled,
    total,
    loading,
    loadError,
    problem,
    canLoad,
    // 换音色
    draft,
    confirm,
    hint,
    // 动作
    busy,
    busyId,
    anyBusy,
    error,
    notice,
    warn,
    resynth,
    requestVoiceChange,
    confirmVoiceChange,
    cancelVoiceChange,
    resetDraft,
    // 试听
    preview,
    previewId,
    previewBusy,
    previewError,
    previewState,
    previewVoice,
    refresh,
    reload,
    start,
    stop,
  };
});
