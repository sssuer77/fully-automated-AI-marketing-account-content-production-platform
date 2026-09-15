// 选题面板状态（T4.3 · §04.1.3 / §04.4.5 第 2 行）。
//
// 三条纪律
// --------
// 1. **不重排后端给的序**：选题池是"人一条条挑"的东西，后端已经
//    `ORDER BY score DESC, seq`；前端只在"人工加选题"那条**不走重拉**的路径上
//    用同一个比较器把它插到对的位置。
// 2. **勾选入队两步确认**：入队 = 建任务 = 会花 LLM 预算。先摊开"将入队哪 N 条"，
//    确认后才发请求（与确认闸的批量通过同一手法 · T4.3 降级预案"勾选疲劳"）。
// 3. **逐条结果如实显示**：`select` 是逐条建任务，失败的那几条要连 `code` 一起摆出来
//    —— 只说"部分失败"等于让人去猜哪一条（与确认闸同一取舍）。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  PAGE_SIZE,
  addManualTopic,
  analyzeTopics,
  fetchDirections,
  fetchTopics,
  ideateTopics,
  importHot,
  selectTopics,
  submitHot,
  type AnalyzeResult,
  type DirectionItem,
  type IdeateResult,
  type ImportResult,
  type ManualTopicBody,
  type ManualTopicResult,
  type SelectFailure,
  type TopicItem,
} from "@/api/endpoints/topics";
import { ApiError } from "@/api/http";
import type { StatusTone } from "@/components/tone";

/** 选题池的状态过滤（与后端 `topic_candidates.status` 的 CHECK 同源）。 */
export type TopicStatus = "candidate" | "selected" | "queued" | "rejected" | "expired";

/** 状态 → 中文标签（下拉与空态文案共用同一份）。 */
export const STATUS_LABELS: Record<TopicStatus, string> = {
  candidate: "候选",
  selected: "已选",
  queued: "已入队",
  rejected: "已淘汰",
  expired: "已过期",
};

/** 钩子类型 → 中文（与后端 `hook_type` 的取值逐字一致）。 */
export const HOOK_LABELS: Record<string, string> = {
  conflict: "冲突",
  suspense: "悬念",
  contrast: "反差",
  number: "数字",
  other: "其他",
};

/** 热点输入的两种去向（与后端 `HotSubmitBody.kind` 同源）。 */
export type HotKind = "hot" | "feedback";

/** 注入点（单测用假件替换，生产用真实现）。 */
export interface TopicsApi {
  fetchTopics: typeof fetchTopics;
  fetchDirections: typeof fetchDirections;
  analyzeTopics: typeof analyzeTopics;
  ideateTopics: typeof ideateTopics;
  selectTopics: typeof selectTopics;
  addManualTopic: typeof addManualTopic;
  importHot: typeof importHot;
  submitHot: typeof submitHot;
}

let api: TopicsApi = {
  fetchTopics,
  fetchDirections,
  analyzeTopics,
  ideateTopics,
  selectTopics,
  addManualTopic,
  importHot,
  submitHot,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureTopicsApi(overrides: Partial<TopicsApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 分组后的一个方向（`direction === null` ⇒ 不属于当前批次，见 `groupByDirection`）。 */
export interface DirectionGroup {
  direction: DirectionItem | null;
  topics: TopicItem[];
}

/** 瀑布流排序：分高的在前，同分按 `seq`（**与后端同一条比较器**）。 */
export function sortByScore(topics: readonly TopicItem[]): TopicItem[] {
  return [...topics].sort((a, b) => {
    const left = a.score ?? Number.NEGATIVE_INFINITY;
    const right = b.score ?? Number.NEGATIVE_INFINITY;
    if (left !== right) return right - left;
    return a.seq - b.seq;
  });
}

/**
 * 按方向分组（**保持组内原序**）。
 *
 * 三个刻意的选择：
 * - 没有选题的方向也保留 —— "这个方向一条都没出"本身就是信息；
 * - 归不进当前批次的选题**不丢**，收进末尾 `direction: null` 那组 —— 人工加选题
 *   挂在固定批次 `manual` 上，而面板默认看的是最近一批模型方向，不兜住就会"加了
 *   却看不见"；
 * - 只有真的存在这类选题时才多出那一组（空列表不占位）。
 */
export function groupByDirection(
  directions: readonly DirectionItem[],
  topics: readonly TopicItem[],
): DirectionGroup[] {
  const known = new Set(directions.map((item) => item.id));
  const buckets = new Map<string, TopicItem[]>();
  const orphans: TopicItem[] = [];
  for (const topic of topics) {
    if (!known.has(topic.direction_id)) {
      orphans.push(topic);
      continue;
    }
    const bucket = buckets.get(topic.direction_id);
    if (bucket) bucket.push(topic);
    else buckets.set(topic.direction_id, [topic]);
  }
  const groups: DirectionGroup[] = directions.map((direction) => ({
    direction,
    topics: buckets.get(direction.id) ?? [],
  }));
  if (orphans.length > 0) groups.push({ direction: null, topics: orphans });
  return groups;
}

/** 钩子类型的中文（没标注就是没标注，不编一个）。 */
export function hookLabel(hook: string | null | undefined): string {
  if (hook === null || hook === undefined || hook === "") return "未标注";
  return HOOK_LABELS[hook] ?? hook;
}

/** 评分色调：8 分以上绿 / 6 分以上黄 / 其余红 / 没打分灰。 */
export function scoreTone(score: number | null | undefined): StatusTone {
  if (score === null || score === undefined) return "idle";
  if (score >= 8) return "ok";
  if (score >= 6) return "warn";
  return "error";
}

/** 选题状态色调。 */
export function topicStatusTone(status: string): StatusTone {
  switch (status) {
    case "queued":
      return "ok";
    case "selected":
      return "busy";
    case "rejected":
      return "error";
    case "expired":
      return "warn";
    default:
      return "idle";
  }
}

/**
 * 这条 WS 事件是否意味着「选题池变了」（面板据此决定要不要重拉）。
 *
 * 只认 `topics` 通道自己的四类。`task.transition` **不**算 —— 选题入队后任务怎么
 * 流转是稿件面板的事，为它重拉一次选题池纯属噪音。
 */
export function isTopicEvent(kind: unknown): boolean {
  return (
    kind === "direction.batch_ready" ||
    kind === "topic.batch_ready" ||
    kind === "topic.selected" ||
    kind === "topic.dedup_warn"
  );
}

/** 一次导入的一句话小结（`hot` / `feedback` 各一段，没跑的为 null）。 */
export function summarizeImport(
  hot: ImportResult | null,
  feedback: ImportResult | null,
): string {
  const parts: string[] = [];
  if (hot) parts.push(`热点 +${hot.inserted}（坏行 ${hot.bad}）`);
  if (feedback) parts.push(`反馈 +${feedback.inserted}（坏行 ${feedback.bad}）`);
  return parts.length > 0 ? parts.join(" · ") : "没有可导入的输入源";
}

/** 入队结果的一句话小结（**成功与失败都要出现**，不能只说"部分失败"）。 */
export function summarizeSelect(selected: number, failed: number, drafted: number): string {
  const parts = [`已入队 ${selected} 条`];
  if (drafted > 0) parts.push(`其中 ${drafted} 条已顺手写稿`);
  if (failed > 0) parts.push(`失败 ${failed} 条（逐条原因见下）`);
  return parts.join(" · ");
}

/** 把一次请求失败翻成人话（与 `stores/scripts.ts` 同源；两个 store 之间不留依赖）。 */
export function describeError(reason: unknown): string {
  if (reason instanceof ApiError) {
    return reason.status === 0 ? reason.message : `${reason.message}（HTTP ${reason.status}）`;
  }
  return String(reason);
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const useTopicsStore = defineStore("topics", () => {
  const status = ref<TopicStatus>("candidate");
  const topics = ref<TopicItem[]>([]);
  const counts = ref<Record<string, number>>({});
  const directions = ref<DirectionItem[]>([]);
  const batches = ref<string[]>([]);
  const batchId = ref<string | null>(null);

  const checked = ref<string[]>([]);
  const pendingSelect = ref<TopicItem[] | null>(null);
  const draftNow = ref(false);
  const failures = ref<SelectFailure[]>([]);

  const lastAnalyze = ref<AnalyzeResult | null>(null);
  const lastIdeate = ref<IdeateResult | null>(null);
  const lastImport = ref<ImportResult | null>(null);
  const manual = ref<ManualTopicResult | null>(null);

  const loading = ref(false);
  const busy = ref(false);
  // 两条错误信道，刻意分开：
  // - `error` 是**动作级**结论（分析没产出、入队被守卫拦下、热点格式全坏）；
  // - `loadError` 是**拉取级**失败（选题池没拉到）。
  // 合成一条的代价是"动作刚说完原因，紧接着一次成功的刷新就把它抹了" —— 而那
  // 正是用户唯一能看到的解释（T4.3 施工中踩到过）。
  const error = ref<string | null>(null);
  const loadError = ref<string | null>(null);
  const notice = ref<string | null>(null);

  const groups = computed(() => groupByDirection(directions.value, topics.value));
  const hasSelection = computed(() => checked.value.length > 0);
  const candidateCount = computed(() => counts.value.candidate ?? 0);
  const queuedCount = computed(() => counts.value.queued ?? 0);

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
  }

  /** 重拉选题池 + 方向卡片（两个请求并发，任一失败都只报一次错）。 */
  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      const [pool, dirs] = await Promise.all([
        api.fetchTopics(status.value, PAGE_SIZE),
        api.fetchDirections(batchId.value),
      ]);
      topics.value = sortByScore(pool.topics);
      counts.value = pool.counts;
      directions.value = dirs.directions;
      batches.value = dirs.batches;
      batchId.value = dirs.batch_id ?? null;
      loadError.value = null;
    } catch (reason) {
      loadError.value = describeError(reason);
    } finally {
      loading.value = false;
    }
  }

  /** 换状态过滤：**清空勾选** —— 跨状态勾选会让人以为自己选了看不见的东西。 */
  async function setStatus(next: TopicStatus): Promise<void> {
    status.value = next;
    clearChecked();
    await refresh();
  }

  /** 切历史批次（同上：勾选只对当前视野负责）。 */
  async function setBatch(next: string | null): Promise<void> {
    batchId.value = next;
    clearChecked();
    await refresh();
  }

  function toggleChecked(topicId: string): void {
    checked.value = checked.value.includes(topicId)
      ? checked.value.filter((id) => id !== topicId)
      : [...checked.value, topicId];
    pendingSelect.value = null;
  }

  function clearChecked(): void {
    checked.value = [];
    pendingSelect.value = null;
  }

  function setDraftNow(value: boolean): void {
    draftNow.value = value;
  }

  /** 第一步：摊开清单（**不发请求**）。按瀑布流顺序，而不是勾选顺序。 */
  function requestSelect(): void {
    clearMessages();
    const wanted = new Set(checked.value);
    const picked = topics.value.filter((item) => wanted.has(item.id));
    if (picked.length === 0) {
      error.value = "先勾选要入队的选题";
      return;
    }
    pendingSelect.value = picked;
  }

  function cancelSelect(): void {
    pendingSelect.value = null;
  }

  /** 第二步：确认后才发请求（逐条结果都带回来）。 */
  async function confirmSelect(): Promise<void> {
    const picked = pendingSelect.value;
    if (picked === null) return;
    busy.value = true;
    clearMessages();
    try {
      const result = await api.selectTopics(
        picked.map((item) => item.id),
        draftNow.value,
      );
      failures.value = result.failed;
      notice.value = summarizeSelect(result.selected.length, result.failed.length, result.drafted);
      pendingSelect.value = null;
      checked.value = [];
      await refresh();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      busy.value = false;
    }
  }

  /** 触发方向分析（长任务；已在跑 ⇒ 409）。`ok=false` 也是 200，所以要看体内。 */
  async function analyze(): Promise<void> {
    busy.value = true;
    clearMessages();
    try {
      const result = await api.analyzeTopics({ import_sources: true });
      lastAnalyze.value = result;
      if (result.ok) {
        notice.value = `出了 ${result.direction_count} 个方向（热点 +${result.hot_imported} 条）`;
      } else {
        error.value = result.error_message ?? "方向分析没产出（原因见「实时日志」面板）";
      }
      await refresh();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      busy.value = false;
    }
  }

  /** 逐方向产出选题（长任务）。 */
  async function ideate(): Promise<void> {
    busy.value = true;
    clearMessages();
    try {
      const result = await api.ideateTopics({ per_direction: 4 });
      lastIdeate.value = result;
      if (result.ok) {
        notice.value = `新增 ${result.inserted} 条选题（去重丢 ${result.dropped} 条）`;
      } else {
        error.value = "选题生成没产出（逐方向原因见下）";
      }
      await refresh();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      busy.value = false;
    }
  }

  /** 人工加选题（相似只提示、不拦）。 */
  async function addManual(body: ManualTopicBody): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const result = await api.addManualTopic(body);
      manual.value = result;
      notice.value =
        result.similar_to.length > 0
          ? `已入库；库里有 ${result.similar_to.length} 条很像的（只提示，不拦）`
          : "已入库";
      await refresh();
      return true;
    } catch (reason) {
      error.value = describeError(reason);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 扫盘导入 `data/hot/*.md` 与 `data/feedback/*.md`。 */
  async function scanHot(): Promise<void> {
    busy.value = true;
    clearMessages();
    try {
      const result = await api.importHot();
      lastImport.value = result.hot ?? result.feedback ?? null;
      notice.value = summarizeImport(result.hot ?? null, result.feedback ?? null);
      await refresh();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      busy.value = false;
    }
  }

  /** 网页端粘一批输入源（落盘文件名由服务端生成）。 */
  async function submitHotText(text: string, kind: HotKind = "hot"): Promise<boolean> {
    if (text.trim().length === 0) {
      error.value = "先粘几行再提交";
      return false;
    }
    busy.value = true;
    clearMessages();
    try {
      const result = await api.submitHot({ text, kind });
      const report = (kind === "hot" ? result.hot : result.feedback) ?? null;
      lastImport.value = report;
      notice.value = summarizeImport(result.hot ?? null, result.feedback ?? null);
      await refresh();
      return true;
    } catch (reason) {
      error.value = describeError(reason);
      return false;
    } finally {
      busy.value = false;
    }
  }

  return {
    status,
    topics,
    counts,
    directions,
    batches,
    batchId,
    checked,
    pendingSelect,
    draftNow,
    failures,
    lastAnalyze,
    lastIdeate,
    lastImport,
    manual,
    loading,
    busy,
    error,
    loadError,
    notice,
    groups,
    hasSelection,
    candidateCount,
    queuedCount,
    refresh,
    setStatus,
    setBatch,
    toggleChecked,
    clearChecked,
    setDraftNow,
    requestSelect,
    cancelSelect,
    confirmSelect,
    analyze,
    ideate,
    addManual,
    scanHot,
    submitHotText,
  };
});
