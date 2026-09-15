// 审计留痕面板的状态（T4.12 · §04.5.11 / §03.3.8）。
//
// 这一屏要回答三个问题
// --------------------
// ① "谁在什么时候动了什么？" —— 列表（含 `before` / `after` 两块留痕）；
// ② "还有多少条没看到？" —— 分页（总数来自后端，不是靠"这页满没满"猜）；
// ③ "按什么筛？" —— 下拉取值来自 facets，不是让人手打 id。
//
// 为什么翻页要**取消在途请求**
// --------------------------
// 连点两下"下一页"会发出两个请求，先发的未必先回。没有取消的话，慢的那个后到、
// 按旧 offset 把列表重画一遍 —— 表现为"点了下一页，看到的还是上一页"。所以每次
// refresh 都先 abort 上一次，并且只认最后一次的响应（`requestId` 兜底）。
//
// 为什么筛选项**不在这里**校验
// --------------------------
// 后端 `AuditRepo` 走列名白名单 + 占位符（`FILTER_COLUMNS` / `FACET_COLUMNS`），
// 前端再抄一份白名单只会抄出第二份真相。这里只负责"空的一律不发"。
//
// 为什么**没有** WS 通道
// ---------------------
// 留痕是"事后查账"，不是"实时盯屏"：Hub 的 8 条通道里没有 audit（§04.4.3），
// 而每来一条留痕就重拉一页，会在批量操作时把面板刷成频闪。要看实时动静去日志面板。
//
// 为什么筛选**不自动查**
// ---------------------
// 文本输入框每敲一个字就发一次请求，既是白跑也是闪烁。改值只改状态，由面板上的
// 「筛选」/ 回车 / 下拉选择显式触发一次查询 —— 与 `stores/logs.ts` 的"过滤在本地"
// 不同：那边数据已经在手里，这边每一组条件都是一次数据库查询。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  AUDIT_PAGE_SIZE,
  fetchAudit,
  fetchAuditFacets,
  type AuditFacets,
  type AuditOp,
  type AuditPage,
  type AuditQuery,
} from "@/api/endpoints/audit";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface AuditApi {
  fetchAudit: typeof fetchAudit;
  fetchAuditFacets: typeof fetchAuditFacets;
}

let api: AuditApi = { fetchAudit, fetchAuditFacets };

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureAuditApi(overrides: Partial<AuditApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 可筛字段（`limit` / `offset` 是分页，不算筛选）。 */
export const AUDIT_FILTERS = [
  "task_id",
  "actor",
  "action",
  "target_type",
  "result",
  "since",
] as const;

export type AuditFilterName = (typeof AUDIT_FILTERS)[number];

/** 面板上的筛选值（一律字符串；空串 = 不筛）。 */
export type AuditFilterState = Record<AuditFilterName, string>;

/** 筛选字段 → 中文标签（下拉与小结共用一份词表）。 */
export const FILTER_LABELS: Record<AuditFilterName, string> = {
  task_id: "任务",
  actor: "操作人",
  action: "动作",
  target_type: "对象",
  result: "结果",
  since: "起始时间",
};

export function emptyFilters(): AuditFilterState {
  return { task_id: "", actor: "", action: "", target_type: "", result: "", since: "" };
}

/** 筛选值 → 查询参数：**空的一律不发**（发 `actor=` 会被后端当成"筛空操作人"）。 */
export function toQuery(
  filters: AuditFilterState,
  offset: number,
  limit: number = AUDIT_PAGE_SIZE,
): AuditQuery {
  const query: AuditQuery = { limit, offset };
  for (const name of AUDIT_FILTERS) {
    const value = filters[name].trim();
    if (value.length > 0) query[name] = value;
  }
  return query;
}

/** 生效中的筛选条数（0 ⇒ 面板要说清"这是全量，不是筛出来的空"）。 */
export function activeFilterCount(filters: AuditFilterState): number {
  return AUDIT_FILTERS.filter((name) => filters[name].trim().length > 0).length;
}

/** 一句话说清当前在看什么。 */
export function describeFilters(filters: AuditFilterState): string {
  const parts = AUDIT_FILTERS.filter((name) => filters[name].trim().length > 0).map(
    (name) => `${FILTER_LABELS[name]}=${filters[name].trim()}`,
  );
  return parts.length === 0 ? "全部留痕（未筛选）" : parts.join(" · ");
}

/** 当前页覆盖到第几条（`0 / 0` 与 `1–50 / 1200` 是两种完全不同的处境）。 */
export function pageWindow(offset: number, limit: number, total: number): string {
  if (total === 0) return "0 / 0";
  const first = Math.min(offset + 1, total);
  const last = Math.min(offset + limit, total);
  return `${first}–${last} / ${total}`;
}

/** 结果 → 色调。`denied` 是**有效**的一次留痕（有人想动、被拦下了），不是失败。 */
export function resultTone(result: string): StatusTone {
  switch (result) {
    case "ok":
      return "ok";
    case "denied":
      return "warn";
    case "error":
      return "error";
    default:
      return "idle";
  }
}

/** 这条留痕到底改了什么（留痕的全部意义就是这两块，只给一句"改过了"等于没留）。 */
export function changedKeys(op: AuditOp): string[] {
  const keys = new Set([...Object.keys(op.before), ...Object.keys(op.after)]);
  return [...keys].filter(
    (key) => JSON.stringify(op.before[key]) !== JSON.stringify(op.after[key]),
  );
}

/** 变更字段压成一行（字段名按字典序，免得同一条留痕两次渲染顺序不同）。 */
export function diffSummary(op: AuditOp): string {
  const keys = changedKeys(op).sort();
  return keys.length === 0 ? "（无字段变化）" : keys.join("、");
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const useAuditStore = defineStore("audit", () => {
  const filters = ref<AuditFilterState>(emptyFilters());
  const offset = ref(0);
  const page = ref<AuditPage | null>(null);
  const facets = ref<AuditFacets | null>(null);
  const loading = ref(false);
  const loadError = ref<string | null>(null);
  const facetsError = ref<string | null>(null);

  /** 在途请求（翻页连点时的取消把手）。 */
  let inflight: AbortController | null = null;
  /** 只认最后一次请求的响应（abort 是尽力而为，兜底靠这个）。 */
  let requestId = 0;

  // ── 派生 ────────────────────────────────────────────────────────────
  const rows = computed<AuditOp[]>(() => page.value?.items ?? []);
  const total = computed<number>(() => page.value?.total ?? 0);
  const limit = computed<number>(() => page.value?.limit ?? AUDIT_PAGE_SIZE);
  const pageCount = computed<number>(() => Math.max(1, Math.ceil(total.value / limit.value)));
  const pageIndex = computed<number>(() => Math.floor(offset.value / limit.value) + 1);
  const hasPrev = computed<boolean>(() => offset.value > 0);
  const hasNext = computed<boolean>(() => offset.value + limit.value < total.value);
  const windowLabel = computed<string>(() => pageWindow(offset.value, limit.value, total.value));
  const filterCount = computed<number>(() => activeFilterCount(filters.value));
  const summary = computed<string>(() => describeFilters(filters.value));
  /** 空列表的两种原因必须分开说：**"没筛到"与"什么都没发生过"是两件事**。 */
  const emptyHint = computed<string>(() =>
    filterCount.value > 0 ? "这组筛选下没有留痕（放宽条件再试）" : "库里还没有任何留痕",
  );
  const actors = computed<string[]>(() => facets.value?.actors ?? []);
  const actions = computed<string[]>(() => facets.value?.actions ?? []);
  const targetTypes = computed<string[]>(() => facets.value?.target_types ?? []);
  const results = computed<string[]>(() => facets.value?.results ?? []);

  // ── 读 ──────────────────────────────────────────────────────────────

  /** 拉当前 offset 下的那一页。失败**不清空**旧页：空表格比旧表格更难判断。 */
  async function refresh(): Promise<void> {
    inflight?.abort();
    const controller = new AbortController();
    inflight = controller;
    requestId += 1;
    const mine = requestId;
    loading.value = true;
    try {
      const result = await api.fetchAudit(toQuery(filters.value, offset.value), controller.signal);
      if (mine !== requestId) return;
      page.value = result;
      loadError.value = null;
    } catch (failure) {
      if (mine !== requestId) return;
      loadError.value = describeError(failure);
    } finally {
      if (mine === requestId) {
        loading.value = false;
        inflight = null;
      }
    }
  }

  /** 拉筛选下拉的取值（失败只写 `facetsError`：下拉空着不该让列表也打不开）。 */
  async function loadFacets(): Promise<void> {
    try {
      facets.value = await api.fetchAuditFacets();
      facetsError.value = null;
    } catch (failure) {
      facetsError.value = describeError(failure);
    }
  }

  // ── 筛选 / 翻页 ─────────────────────────────────────────────────────

  /** 改一个筛选值（**只改状态**：什么时候查由 `apply()` 说了算）。 */
  function setFilter(name: AuditFilterName, value: string): void {
    filters.value = { ...filters.value, [name]: value };
  }

  /** 用当前筛选条件从第一页重查。 */
  function apply(): void {
    offset.value = 0;
    void refresh();
  }

  function clearFilters(): void {
    filters.value = emptyFilters();
    offset.value = 0;
    void refresh();
  }

  /** 翻到指定 offset（越界直接忽略：负 offset 会被后端 422）。 */
  function goTo(next: number): void {
    if (next < 0) return;
    if (next === offset.value) return;
    offset.value = next;
    void refresh();
  }

  function goPrev(): void {
    goTo(Math.max(0, offset.value - limit.value));
  }

  function goNext(): void {
    if (!hasNext.value) return;
    goTo(offset.value + limit.value);
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：重复调用只是再拉一次（这一屏没有轮询、没有订阅）。 */
  function start(): void {
    void loadFacets();
    void refresh();
  }

  function stop(): void {
    inflight?.abort();
    inflight = null;
  }

  return {
    // 读
    filters,
    offset,
    page,
    facets,
    rows,
    total,
    limit,
    pageCount,
    pageIndex,
    windowLabel,
    hasPrev,
    hasNext,
    filterCount,
    summary,
    emptyHint,
    actors,
    actions,
    targetTypes,
    results,
    loading,
    loadError,
    facetsError,
    // 动作
    setFilter,
    apply,
    clearFilters,
    goTo,
    goPrev,
    goNext,
    refresh,
    loadFacets,
    start,
    stop,
  };
});
