// 发布面板状态（T5.5 · §06.11 / §06.12）。
//
// 这一屏要回答五个问题
// --------------------
// ① 现在有什么在等着发 / 正在发 / 已经发出去了？—— 六状态看板 + 顶部计数；
// ② 有没有**需要我做决定**的？—— 待人工队列（重试 / 取消 / 标记已处理三连）；
// ③ 发出去的片子数据回来了吗？—— 回流时刻表与已回流的数字，以及三个动作
//    （跑一轮 / 采这一条 / 沉淀这一条）；
// ④ 我要发的东西，来源登记齐了吗？—— R2 合规留档（常驻提示 + 缺口逐条）；
// ⑤ 怎么把一支成片连同它的证据交给别人？—— 交付包预览 + 导出。
//
// 为什么 `can_retry` / `can_cancel` / `can_mark_done` 原样转发
// ------------------------------------------------------------
// 判据是**服务端**的状态机规则。前端再写一份的代价是"规则改一次漏一处"，
// 而漏的那一处表现为"点了按钮报 400" —— 用户看到的是"这个按钮坏了"。
// 所以这里一个布尔都不自己算，只把服务端给的那三个摆上去。
//
// 为什么**只在有活的时候**轮询
// --------------------------
// 与渲染 / 一键出片同一条：空闲时一直拉，就是每秒钟一次"什么新东西都没有"的请求，
// 面板上那些数字还会一直跳，而它们跳动的唯一原因是"我们在定时问"。
// 「待人工」不算活：那一条正等人做决定，人不动它就不会变 —— 所以它靠刷新按钮，
// 不靠轮询（代价说清楚：**别的进程刚转人工的那一条不会自己冒出来**）。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  cancelPublication,
  collectMetrics,
  fetchCompliance,
  fetchHandoff,
  fetchManualQueue,
  fetchPublications,
  markManualDone,
  pushHandoff,
  retryPublication,
  runMetricsTick,
  sinkMemory,
  type ComplianceView,
  type HandoffPreview,
  type HandoffResult,
  type MemorySinkResult,
  type MetricsTickResult,
  type Publication,
  type PublicationList,
  type PublishActionBody,
} from "@/api/endpoints/publish";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";

/** 有活在跑时的轮询周期。2 秒：发布是"上传 + 等平台确认"，比配音慢一个量级。 */
export const PUBLISH_POLL_MS = 2_000;

/** 任务号长度上限（与后端 path pattern 同源）。 */
export const MAX_TASK_ID_CHARS = 64;

/** 看板上按状态排出来的三块（顺序就是屏上的顺序）。 */
export const BOARD_STATUSES: readonly string[] = ["queued", "uploading", "published"];

/** 「失败 / 已取消」那一块 —— 它们不在规格的七区块里，但**藏起来更糟**：
 *  失败中的记录还在自动重试，看不到它就会以为"这条根本没投出去过"。 */
export const ENDED_STATUSES: readonly string[] = ["failed", "canceled"];

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface PublishApi {
  fetchPublications: typeof fetchPublications;
  fetchManualQueue: typeof fetchManualQueue;
  retryPublication: typeof retryPublication;
  cancelPublication: typeof cancelPublication;
  markManualDone: typeof markManualDone;
  fetchHandoff: typeof fetchHandoff;
  pushHandoff: typeof pushHandoff;
  fetchCompliance: typeof fetchCompliance;
  runMetricsTick: typeof runMetricsTick;
  collectMetrics: typeof collectMetrics;
  sinkMemory: typeof sinkMemory;
}

let api: PublishApi = {
  fetchPublications,
  fetchManualQueue,
  retryPublication,
  cancelPublication,
  markManualDone,
  fetchHandoff,
  pushHandoff,
  fetchCompliance,
  runMetricsTick,
  collectMetrics,
  sinkMemory,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configurePublishApi(overrides: Partial<PublishApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

const STATUS_LABELS: Record<string, string> = {
  queued: "待发布",
  uploading: "发布中",
  published: "已发布",
  failed: "失败",
  manual_required: "待人工",
  canceled: "已取消",
};

/** 状态 → 中文。不认识的**原样显示**：后端加了新状态而前端没跟上时，
 *  屏上出现一个 `some_new_state` 也好过什么都不说（后者会被读成"这条没了"）。 */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

/** 状态 → 灯色。 */
export function statusTone(status: string): StatusTone {
  switch (status) {
    case "published":
      return "ok";
    case "failed":
      return "error";
    case "manual_required":
      return "warn";
    case "uploading":
      return "busy";
    default:
      return "idle";
  }
}

/** ISO 时间戳 → `MM-DD HH:MM`（发布这一屏跨天，日期不能省；与其余面板只显示时刻不同）。 */
export function formatStamp(ts: string | null | undefined): string {
  if (ts === null || ts === undefined || ts.length < 16) return "-";
  return `${ts.slice(5, 10)} ${ts.slice(11, 16)}`;
}

/** 某个状态的若干条（快照没拿到 ⇒ 空数组，不抛）。 */
export function rowsOf(list: PublicationList | null, status: string): Publication[] {
  return list?.by_status?.[status] ?? [];
}

/** 有活在跑（排队中 + 上传中）—— 轮询的唯一判据。 */
export function activeWork(counts: Record<string, number> | null | undefined): boolean {
  if (counts === null || counts === undefined) return false;
  return (counts.queued ?? 0) + (counts.uploading ?? 0) > 0;
}

/** 待人工有多少条（面板顶部那个红点计数）。 */
export function manualCount(list: PublicationList | null): number {
  return list?.counts?.manual_required ?? 0;
}

/** 已发布那几条里**数据回流**相关的（有回流时刻表或有数字）。 */
export function metricsRows(list: PublicationList | null): Publication[] {
  return rowsOf(list, "published").filter(
    (row) => row.next_metric_at != null || Object.keys(row.metrics ?? {}).length > 0,
  );
}

/** 一条记录的数字 → 一行人话（键名不固定 ⇒ 只列认得出的那几个，其余原样给出）。 */
export function metricsText(row: Publication): string {
  const parts: string[] = [];
  const known: readonly [string, string][] = [
    ["views", "播放"],
    ["likes", "赞"],
    ["comments", "评论"],
    ["shares", "转发"],
    ["favorites", "收藏"],
  ];
  for (const [key, label] of known) {
    const value = (row.metrics ?? {})[key];
    if (typeof value === "number") parts.push(`${label} ${value}`);
  }
  if (parts.length === 0) {
    return row.next_metric_at == null
      ? "还没有回流时刻表"
      : `下一次回收：${formatStamp(row.next_metric_at)}`;
  }
  return parts.join(" · ");
}

/** 交付包缺件 → 一行人话（缺的是必需件还是可选件，语气不一样）。 */
export function missingText(preview: HandoffPreview | null): string {
  if (preview === null) return "";
  const missing = preview.missing ?? [];
  if (missing.length === 0) return "六件齐";
  return `缺 ${missing.join(" / ")}`;
}

/**
 * 「标记已人工处理」的理由自检。返回**人话**，`null` = 可以提交。
 *
 * 后端对这一条是 422：标记已处理的意思是"这件事我处理完了"，而**为什么算处理完**
 * 只有人能回答 —— 没有那句话，三个月后回看只看到一条"已处理"，等于没记。
 */
/**
 * 后端那句常驻提示里带 `**`（它是写给 markdown 看的）—— 这一屏没有渲染器，
 * 原样显示会让人看见一堆星号。这里**只去掉标记，一个字都不改**。
 */
export function plainText(value: string): string {
  return value.split("**").join("");
}

export function reasonProblem(reason: string): string | null {
  if (reason.trim() === "") {
    return "标记已处理必须写清理由（例：已在平台上手工发布 / 素材本身不合规，放弃这条）。";
  }
  return null;
}

/** 人工处置的请求体（三个动作共用；`reason` 只在该给的时候发）。 */
export function actionBody(reason: string | null): PublishActionBody {
  const body: PublishActionBody = { actor: "user" };
  if (reason !== null && reason.trim() !== "") body.reason = reason.trim();
  return body;
}

/** 一轮数据回收的结论 → 一行人话。`yielded` 是**让路**，不是失败（见 `recycle.py`）。 */
export function tickText(report: MetricsTickResult | null): string {
  if (report === null) return "";
  if (report.yielded) {
    return "发布池正忙，这一拍整拍让路（两个浏览器抢同一个 profile 会让发布失败）—— 下一拍照跑。";
  }
  const parts = [
    `采到 ${report.collected?.length ?? 0} 条`,
    `顺延 ${report.deferred?.length ?? 0} 条`,
    `停止 ${report.stopped?.length ?? 0} 条`,
    `还有 ${report.pending ?? 0} 条到点未采`,
  ];
  return parts.join(" · ");
}

/**
 * 沉淀的结论 → 一行人话。
 *
 * 评论采样那一条**明说没接线**（各平台评论页的选择器还没写）：不说的话，
 * "评论 0 条"会被读成"这条作品没人评论"，而真实情况是"我们还没去读"。
 */
export function sinkText(result: MemorySinkResult): string {
  const parts = [
    `新增反馈 ${result.feedback_items_created} 条`,
    `降权方向 ${result.topics_demoted} 个`,
    `汇总 ${result.digest_path.split(/[\\/]/).pop() ?? result.digest_path}`,
  ];
  if (result.planner_consumable) parts.push("下一轮选题可直接吃");
  if (result.comments_seen === 0) parts.push("评论采样还没接线（只回流了数字）");
  return parts.join(" · ");
}

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

export const usePublishStore = defineStore("publish", () => {
  const board = ref<PublicationList | null>(null);
  const queue = ref<PublicationList | null>(null);
  const compliance = ref<ComplianceView | null>(null);

  const loading = ref(false);
  const loadError = ref<string | null>(null);
  const busy = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);

  // ── 数据回收三连（§06.6 采数 / §06.8 沉淀）───────────────────────────

  /**
   * 跑一轮数据回收（**给人按的**：后台本来每 60s 自己拍一次）。
   *
   * 一条都没采到**不算错**：可能只是还没到点（`pending` 会说清有几条到点未采），
   * 也可能发布池正忙让路了。所以结论走 `notice` 而不是 `error`。
   */
  async function tickMetrics(): Promise<boolean> {
    metricsBusy.value = true;
    clearMessages();
    try {
      const report = await api.runMetricsTick();
      metricsTick.value = report;
      notice.value = tickText(report);
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      metricsBusy.value = false;
    }
  }

  /** 采这一条（不看 `next_metric_at`：人按的就是"现在采"）。 */
  async function collectOne(publication: Publication): Promise<boolean> {
    metricsBusy.value = true;
    clearMessages();
    try {
      const fresh = await api.collectMetrics(publication.id);
      notice.value = `「${fresh.title}」${metricsText(fresh)}`;
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      metricsBusy.value = false;
    }
  }

  /** 沉淀这一条（写 feedback + 汇总文件 + 低互动降权）。 */
  async function sinkOne(publication: Publication): Promise<boolean> {
    metricsBusy.value = true;
    clearMessages();
    try {
      const result = await api.sinkMemory(publication.id);
      notice.value = sinkText(result);
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      metricsBusy.value = false;
    }
  }

  // ── 交付包 ──────────────────────────────────────────────────────────
  const handoffTaskId = ref("");
  const handoffPreview = ref<HandoffPreview | null>(null);
  const handoffResult = ref<HandoffResult | null>(null);
  const handoffError = ref<string | null>(null);
  const handoffBusy = ref(false);

  // ── 数据回收（T5.4 · §06.6）──────────────────────────────────────────
  /** 上一轮回收的结论（`null` = 本次会话还没跑过）。 */
  const metricsTick = ref<MetricsTickResult | null>(null);
  /** 有没有一条采数 / 沉淀在途（三个按钮共用一个忙碌位）。 */
  const metricsBusy = ref(false);

  // ── 中间态（一次编辑的东西，不进持久状态）─────────────────────────────
  /** 「标记已处理」的理由草稿（按 publication id 存）。 */
  const reasonDraft = ref<Record<string, string>>({});
  /** 哪一条展开了理由输入框（同时只开一个 —— 屏上同时开五个输入框没人看得懂）。 */
  const reasonFor = ref<string | null>(null);

  // ── 读 ──────────────────────────────────────────────────────────────

  const counts = computed(() => board.value?.counts ?? {});
  const manual = computed(() => manualCount(queue.value));
  const active = computed(() => activeWork(board.value?.counts));
  const manualRows = computed(() => rowsOf(queue.value, "manual_required"));
  const metrics = computed(() => metricsRows(board.value));
  const endedRows = computed(() =>
    ENDED_STATUSES.flatMap((status) => rowsOf(board.value, status)),
  );
  const complianceOk = computed(() => compliance.value?.ok ?? true);
  const canPreviewHandoff = computed(() => {
    const wanted = handoffTaskId.value.trim();
    return wanted !== "" && wanted.length <= MAX_TASK_ID_CHARS;
  });

  let polling = false;
  let timer: ReturnType<typeof setInterval> | null = null;

  function clearTimer(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  /** 轮询开关与"现在有没有活"绑定（每轮 refresh 之后再判一次）。 */
  function syncPolling(): void {
    if (!polling || !active.value) {
      clearTimer();
      return;
    }
    if (timer === null) timer = setInterval(() => void refresh(), PUBLISH_POLL_MS);
  }

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      board.value = await api.fetchPublications({ limit: 50 });
      loadError.value = null;
    } catch (failure) {
      // 旧快照**留着**：一屏数字不因一次抖动变空白（与其余面板同一条）。
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
    syncPolling();
  }

  /** 待人工队列（**单独一个端点**：它是唯一一个要求人做决定的列表）。 */
  async function refreshQueue(): Promise<void> {
    try {
      queue.value = await api.fetchManualQueue({ limit: 100 });
    } catch (failure) {
      loadError.value = describeError(failure);
    }
  }

  async function refreshCompliance(): Promise<void> {
    try {
      compliance.value = await api.fetchCompliance();
    } catch (failure) {
      loadError.value = describeError(failure);
    }
  }

  /** 一次把三份都拉齐（首屏与「刷新」按钮走这条）。 */
  async function refreshAll(): Promise<void> {
    await Promise.all([refresh(), refreshQueue(), refreshCompliance()]);
  }

  // ── 人工处置三连（§06.10 不变量 3：三者均写 audit_ops）─────────────────

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
  }

  /** 三个动作共用一条壳：报结论 → 重拉（重拉失败不该把结论吞掉）。 */
  async function act(
    action: (body: PublishActionBody) => Promise<{ message: string }>,
    body: PublishActionBody,
  ): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const result = await action(body);
      notice.value = result.message;
      await refresh();
      await refreshQueue();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  function retry(publication: Publication): Promise<boolean> {
    return act(
      (body) => api.retryPublication(publication.id, body),
      actionBody("人工重试"),
    );
  }

  function cancel(publication: Publication, reason: string): Promise<boolean> {
    return act((body) => api.cancelPublication(publication.id, body), actionBody(reason));
  }

  /** 标记已人工处理（理由必填 —— 由 :func:`reasonProblem` 先拦一道）。 */
  async function markDone(publication: Publication): Promise<boolean> {
    const reason = reasonDraft.value[publication.id] ?? "";
    const problem = reasonProblem(reason);
    if (problem !== null) {
      error.value = problem;
      notice.value = null;
      return false;
    }
    const ok = await act(
      (body) => api.markManualDone(publication.id, body),
      actionBody(reason),
    );
    if (ok) {
      reasonFor.value = null;
      const next = { ...reasonDraft.value };
      delete next[publication.id];
      reasonDraft.value = next;
    }
    return ok;
  }

  function openReason(publicationId: string): void {
    reasonFor.value = publicationId;
    clearMessages();
  }

  function closeReason(): void {
    reasonFor.value = null;
  }

  function setReason(publicationId: string, value: string): void {
    reasonDraft.value = { ...reasonDraft.value, [publicationId]: value };
  }

  // ── 交付包 ──────────────────────────────────────────────────────────

  function setHandoffTaskId(value: string): void {
    handoffTaskId.value = value;
    // 换了任务号 ⇒ 上一份预览说的是**另一个任务号**的事，留着比没有更糟。
    handoffPreview.value = null;
    handoffResult.value = null;
    handoffError.value = null;
  }

  /** 预览：**一个字节都不写**（后端保证）。 */
  async function previewHandoff(): Promise<boolean> {
    const wanted = handoffTaskId.value.trim();
    if (wanted === "") {
      handoffError.value = "先填一个任务号。";
      return false;
    }
    handoffBusy.value = true;
    handoffError.value = null;
    handoffResult.value = null;
    try {
      handoffPreview.value = await api.fetchHandoff(wanted);
      return true;
    } catch (failure) {
      handoffPreview.value = null;
      handoffError.value = describeError(failure);
      return false;
    } finally {
      handoffBusy.value = false;
    }
  }

  /**
   * 导出。**先预览再导出**：按钮在没预览过时不亮 —— 不看一眼包里缺什么就交出去，
   * 收到的是一份自己都不知道少了什么的包。
   */
  async function exportHandoff(): Promise<boolean> {
    const wanted = handoffTaskId.value.trim();
    if (wanted === "") {
      handoffError.value = "先填一个任务号。";
      return false;
    }
    if (handoffPreview.value === null) {
      handoffError.value = "先点「预览」看一眼包里有什么、缺哪件，再导出。";
      return false;
    }
    handoffBusy.value = true;
    handoffError.value = null;
    try {
      handoffResult.value = await api.pushHandoff(wanted, actionBody("导出交付包"));
      return true;
    } catch (failure) {
      handoffError.value = describeError(failure);
      return false;
    } finally {
      handoffBusy.value = false;
    }
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：重复调用只会有一次在途请求、一份轮询。 */
  function start(): void {
    polling = true;
    void refreshAll();
  }

  function stop(): void {
    polling = false;
    clearTimer();
  }

  return {
    // 读
    board,
    queue,
    compliance,
    loading,
    loadError,
    counts,
    manual,
    active,
    manualRows,
    metrics,
    endedRows,
    complianceOk,
    // 数据回收
    metricsTick,
    metricsBusy,
    tickMetrics,
    collectOne,
    sinkOne,
    // 动作
    busy,
    error,
    notice,
    retry,
    cancel,
    markDone,
    refresh,
    refreshQueue,
    refreshCompliance,
    refreshAll,
    // 中间态
    reasonDraft,
    reasonFor,
    openReason,
    closeReason,
    setReason,
    // 交付包
    handoffTaskId,
    handoffPreview,
    handoffResult,
    handoffError,
    handoffBusy,
    canPreviewHandoff,
    setHandoffTaskId,
    previewHandoff,
    exportHandoff,
    // 生命周期
    start,
    stop,
  };
});