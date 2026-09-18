// 报告与决策闭环状态（T5.7 · §04.6.5.2 / §06.7）。
//
// 这一块要回答四个问题
// --------------------
// ① 我手上有哪些报告、窗口各是哪一段？—— 列表；
// ② 这份报告到底说了什么？—— 详情（正文 + 聚合数据 + 建议 + 产物路径）；
// ③ 哪条建议我认？—— 采纳（写进人物偏好 + 留痕，**已采纳再点什么都不做**）；
// ④ 报告什么时候自己生成？—— 周期（能建、能改、能停、能立刻生成一次）。
//
// 为什么这一块**可以**慢轮询
// --------------------------
// 别的面板轮询的是「某个进程正在做的事」，空闲时问它只是白问。报告不像发布那样
// 每分钟都在变：它只可能在「到点」那一刻多出一份。60s 一拍足够了，而「它刚才
// 自己生成了」这件事恰恰是用户打开这一屏想看到的。
//
// 为什么采纳只传 `report_id + index`，不把那句话传回去
// ---------------------------------------------------
// 采纳的语义是「我看了第 N 条，同意它」。把句子传回后端的话，留痕里记的东西与
// 报告里存的那一句就有了两个来源 —— 而它们迟早会分家，且没有任何地方会报错。
// 后端按 index 取原文，前端只负责说清「哪一份的第几条」。
//
// 为什么「样本不足」要在面板上强制出现
// ----------------------------------
// `confidence='low'`（n<10）的建议**看着和别的一样**，照它去改风格等于拿三次
// 播放量当规律。后端已经把这句写进 `statement`，面板再标一次，是因为用户很可能
// 只看按钮那一行，不看正文。
//
// 为什么周期表单里的 `include` 是勾选框而不是一个数字
// --------------------------------------------------
// 六个区块各自对应报告正文里的一节。让用户勾，等于让他自己决定「这份报告要不要
// 谈钱」；给个 `all` 之类的预设，等于替他决定，而他事后无从知道漏了什么。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  applyInsight,
  createReportSchedule,
  deleteReportSchedule,
  exportReport,
  fetchReport,
  fetchReportSchedules,
  fetchReports,
  generateReport,
  patchReportSchedule,
  runReportSchedule,
  type ReportApplyResult,
  type ReportDetail,
  type ReportGenerateBody,
  type ReportInsight,
  type ReportList,
  type ReportSchedule,
  type ReportScheduleBody,
  type ReportScheduleList,
  type ReportSchedulePatchBody,
  type ReportScheduleRunResult,
  type ReportSummary,
} from "@/api/endpoints/reports";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";
import { saveTextFile } from "@/utils/download";

/** 慢轮询周期（见模块注释：这一块轮询的是「到点」，不是「某个进程在忙」）。 */
export const REPORT_POLL_MS = 60_000;

/** 三种周期（顺序 = 下拉框里的顺序，与后端 `domain/report.py` 的 `PERIODS` 同源）。 */
export const PERIODS: readonly string[] = ["daily", "weekly", "monthly"];

const PERIOD_LABELS: Record<string, string> = { daily: "日报", weekly: "周报", monthly: "月报" };

/** 周期 → 中文（不认识的**原样显示**，别把未知吞掉）。 */
export function periodLabel(period: string): string {
  return PERIOD_LABELS[period] ?? period;
}

/** 0=周一（与后端 `validate_period_fields` 的约定一致）。 */
export const WEEKDAYS: readonly string[] = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export function weekdayLabel(day: number | null | undefined): string {
  if (day === null || day === undefined) return "";
  return WEEKDAYS[day] ?? `第 ${day} 天`;
}

const CONFIDENCE_LABELS: Record<string, string> = { low: "低", medium: "中", high: "高" };

/** 置信度 → 中文（分档规则在后端 `confidence_for`：<10 低 / 10-29 中 / ≥30 高）。 */
export function confidenceLabel(confidence: string): string {
  return CONFIDENCE_LABELS[confidence] ?? confidence;
}

/**
 * 置信度 → 灯色。
 *
 * `low` 给 `warn` 而不是 `error`：它**不是故障**，是「这条结论的证据只有三次播放量」。
 * 用红色会让人以为出了错，而真正该做的是「知道它不牢，再等等看」。
 */
export function confidenceTone(confidence: string): StatusTone {
  if (confidence === "high") return "ok";
  if (confidence === "medium") return "warn";
  return "idle";
}

/** n<10 ⇒ 面板必须把那句警告摆出来（后端也写进了 `statement`，这里是第二道）。 */
export function insightIsShaky(insight: ReportInsight): boolean {
  return insight.confidence === "low";
}

const KIND_LABELS: Record<string, string> = {
  topic: "选题",
  timing: "时段",
  platform: "平台",
  quality: "质量",
  cost: "成本",
};

export function insightKindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/** 报告正文的六个区块（与后端 `INCLUDE_KINDS` 同源，顺序 = 屏上顺序）。 */
export const INCLUDE_KINDS: readonly string[] = [
  "publish",
  "metrics",
  "topics",
  "quality",
  "cost",
  "errors",
];

const INCLUDE_LABELS: Record<string, string> = {
  publish: "发布量",
  metrics: "播放数据",
  topics: "选题分布",
  quality: "质量分档",
  cost: "成本",
  errors: "错误",
};

export function includeLabel(kind: string): string {
  return INCLUDE_LABELS[kind] ?? kind;
}

/** 默认勾选（与后端 `DEFAULT_INCLUDE` 同源：错误区块默认不勾，它多数时候是空的）。 */
export const DEFAULT_INCLUDE: readonly string[] = ["publish", "metrics", "topics", "quality", "cost"];

const TRIGGER_LABELS: Record<string, string> = {
  manual: "手动生成",
  scheduled: "到点生成",
  panel: "面板生成",
};

export function triggerLabel(trigger: string): string {
  return TRIGGER_LABELS[trigger] ?? trigger;
}

/** 周期的 `last_result` → 中文。`error:…` 原样保留后半句 —— 那句话就是排查线索。 */
export function resultLabel(result: string | null | undefined): string {
  if (result === null || result === undefined || result === "") return "还没跑过";
  if (result.startsWith("error:")) return `失败：${result.slice("error:".length)}`;
  if (result === "ok") return "已生成";
  if (result === "skipped_disabled") return "停用中，空转";
  if (result === "skipped_no_data") return "窗口里没数据";
  return result;
}

/** 周期的 `last_result` → 灯色。 */
export function resultTone(result: string | null | undefined): StatusTone {
  if (result === null || result === undefined || result === "") return "idle";
  if (result === "ok") return "ok";
  if (result.startsWith("error:")) return "error";
  return "warn";
}
/** 数量 → 人看的字（过万折成 `1.2万`；空值给破折号，不给 0）。 */
export function countText(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(1)}万`;
  return String(Math.round(value));
}

/**
 * ISO → 本地 `MM-DD HH:MM`。
 *
 * **必须换算，不能截字符串**：库里存的是 UTC（`…Z`），截出来是 UTC 的钟点 ——
 * 一份「昨天 23:00 生成」的报告会显示成「今天 07:00」，与它自己的窗口
 * 参数**当场自相矛盾**。时区取浏览器真实偏移。
 */
export function stampText(ts: string | null | undefined): string {
  if (ts === null || ts === undefined || ts === "") return "-";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "-";
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** `YYYY-MM-DD` → `[年, 月, 日]`（**直接切字符串**：`new Date("2026-09-10")` 按 UTC 解析，西半球会退一天）。 */
function dateParts(value: string): [number, number, number] | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (match === null) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

/** 报告窗口 → `09-10 ~ 09-16`（跨年才把年份带出来，省得每行都顶着一串重复数字）。 */
export function windowText(start: string, end: string): string {
  const pad = (value: number): string => String(value).padStart(2, "0");
  const a = dateParts(start);
  const b = dateParts(end);
  if (a === null || b === null) return `${start} ~ ${end}`;
  if (a[0] !== b[0]) return `${start} ~ ${end}`;
  return `${pad(a[1])}-${pad(a[2])} ~ ${pad(b[1])}-${pad(b[2])}`;
}

/** 报告列表里一条 → 一行数字摘要。 */
export function summaryText(row: ReportSummary): string {
  const parts = [
    `发布 ${row.publish_count} 条`,
    `中位播放 ${countText(row.median_views)}`,
    `建议 ${row.insights_count} 条`,
  ];
  if (row.applied_count > 0) parts.push(`已采纳 ${row.applied_count} 条`);
  if (row.llm_cost_usd !== null && row.llm_cost_usd !== undefined) {
    parts.push(`成本 $${row.llm_cost_usd.toFixed(4)}`);
  }
  return parts.join(" · ");
}

/** 一条建议的证据 → 一行（两种形状：对比型给 A/B + 相对差，成本型给单条花费）。 */
export function insightEvidenceText(insight: ReportInsight): string {
  const evidence = insight.evidence ?? {};
  const parts: string[] = [];
  const n = evidence["n"];
  if (typeof n === "number") parts.push(`n=${n}`);
  const a = evidence["a"];
  const b = evidence["b"];
  if (typeof a === "string" && typeof b === "string") {
    const aValue = evidence["a_value"];
    const bValue = evidence["b_value"];
    parts.push(`${a}（${countText(typeof aValue === "number" ? aValue : null)}）`);
    parts.push(`${b}（${countText(typeof bValue === "number" ? bValue : null)}）`);
  }
  const delta = evidence["delta_pct"];
  if (typeof delta === "number") parts.push(`相对差 ${delta.toFixed(1)}%`);
  const perVideo = evidence["per_video_usd"];
  if (typeof perVideo === "number") parts.push(`单条 $${perVideo.toFixed(4)}`);
  if (insight.applied) parts.push("已采纳");
  return parts.join(" · ");
}

/** 一条周期的参数摘要（周期 + 真正生效的那几个字段 + 回看）。 */
export function scheduleSummary(row: ReportSchedule): string {
  const lookback = `回看 ${row.lookback_days} 天`;
  if (row.period === "weekly") {
    // `weekdayLabel` 已经是「周一」⇒ 前缀只加一个「每」，否则会念成「每周周一」。
    return `每${weekdayLabel(row.weekday)} ${row.at_time}（${lookback}）`;
  }
  if (row.period === "monthly") {
    return `每月 ${row.day_of_month ?? "?"} 日 ${row.at_time}（${lookback}）`;
  }
  return `每天 ${row.at_time}（${lookback}）`;
}

/** 下一次触发的说明（停用 / 还没排期都要说清，别让人以为「它坏了」）。 */
export function nextRunText(row: ReportSchedule): string {
  if (!row.enabled) return "已停用";
  if (row.next_run_at === null || row.next_run_at === undefined) return "待排期（下一拍补上）";
  return stampText(row.next_run_at);
}

/** 立刻生成一次的结论 → 一行人话（面板上那条提示）。 */
export function runText(outcome: ReportScheduleRunResult): string {
  if (outcome.result === "ok") {
    return `已生成 ${outcome.report_id ?? "一份报告"}，去上面看它`;
  }
  if (outcome.result === "skipped_disabled") return "这条周期是停用的 ⇒ 没生成（先启用它）";
  if (outcome.result === "skipped_no_data") {
    return outcome.note ?? "这个窗口里没有数据 ⇒ 没生成（不是故障，下一期照排）";
  }
  if (outcome.result.startsWith("error:")) return outcome.result.slice("error:".length);
  return outcome.result;
}

/** 采纳的结论 → 一行人话（幂等命中时后端给的那句更准，优先用它）。 */
export function applyText(outcome: ReportApplyResult): string {
  if (outcome.applied) return "已采纳 ⇒ 写进人物偏好，下一轮 Planner 就会读到";
  return outcome.message;
}
/** 回看天数默认值（与后端 `DEFAULT_LOOKBACK_DAYS` 同源：窗口右端 = 触发日**前一天**）。 */
export const DEFAULT_LOOKBACK: Record<string, number> = { daily: 1, weekly: 7, monthly: 31 };

export function lookbackFor(period: string): number {
  return DEFAULT_LOOKBACK[period] ?? 7;
}

/** 周期编辑表单的初值。 */
export interface ScheduleDraft {
  period: string;
  weekday: number;
  dayOfMonth: number;
  atTime: string;
  lookbackDays: number;
  include: string[];
  enabled: boolean;
}

export function emptyDraft(): ScheduleDraft {
  return {
    period: "weekly",
    weekday: 0,
    dayOfMonth: 1,
    atTime: "09:00",
    lookbackDays: lookbackFor("weekly"),
    include: [...DEFAULT_INCLUDE],
    enabled: true,
  };
}

/** 一行周期 → 表单初值（点「编辑」时用；`include` 空 ⇒ 回到默认勾选）。 */
export function draftFromRow(row: ReportSchedule): ScheduleDraft {
  return {
    period: row.period,
    weekday: row.weekday ?? 0,
    dayOfMonth: row.day_of_month ?? 1,
    atTime: row.at_time,
    lookbackDays: row.lookback_days,
    include: row.include.length > 0 ? [...row.include] : [...DEFAULT_INCLUDE],
    enabled: row.enabled,
  };
}

/**
 * 表单 → 请求体（**校验留给后端**：非法参数必须是 400 说清哪一项不对，
 * 而不是前端自己拼一个 422）。不生效的那两个字段一律给 `null` ——
 * 给 0 或 "" 会被后端当成「你确实填了」，而 weekly 带着 `day_of_month` 是矛盾输入。
 */
export function draftToBody(draft: ScheduleDraft): ReportScheduleBody {
  return {
    period: draft.period as ReportScheduleBody["period"],
    at_time: draft.atTime,
    tz: "Asia/Shanghai",
    weekday: draft.period === "weekly" ? draft.weekday : null,
    day_of_month: draft.period === "monthly" ? draft.dayOfMonth : null,
    lookback_days: draft.lookbackDays,
    include: draft.include as ReportScheduleBody["include"],
    enabled: draft.enabled,
  };
}

/** 表单 → 改一条的请求体（同样的字段 + 一句理由进留痕）。 */
export function draftToPatch(draft: ScheduleDraft, reason: string): ReportSchedulePatchBody {
  return { ...draftToBody(draft), reason: reason.trim() === "" ? null : reason.trim() };
}

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface ReportsApi {
  fetchReports: typeof fetchReports;
  fetchReport: typeof fetchReport;
  generateReport: typeof generateReport;
  applyInsight: typeof applyInsight;
  exportReport: typeof exportReport;
  fetchReportSchedules: typeof fetchReportSchedules;
  createReportSchedule: typeof createReportSchedule;
  patchReportSchedule: typeof patchReportSchedule;
  deleteReportSchedule: typeof deleteReportSchedule;
  runReportSchedule: typeof runReportSchedule;
}

let api: ReportsApi = {
  fetchReports,
  fetchReport,
  generateReport,
  applyInsight,
  exportReport,
  fetchReportSchedules,
  createReportSchedule,
  patchReportSchedule,
  deleteReportSchedule,
  runReportSchedule,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureReportsApi(overrides: Partial<ReportsApi>): void {
  api = { ...api, ...overrides };
}
// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const useReportsStore = defineStore("reports", () => {
  const list = ref<ReportList | null>(null);
  const detail = ref<ReportDetail | null>(null);
  const schedules = ref<ReportScheduleList | null>(null);
  const selectedId = ref<string | null>(null);
  const loading = ref(false);
  const detailLoading = ref(false);
  const generating = ref(false);
  const saving = ref(false);
  const busyId = ref<string | null>(null);
  const applyingIndex = ref<number | null>(null);
  const error = ref("");
  const notice = ref("");
  /** 手动生成用哪个周期（与表单里那一条**互不影响**：一个是「现在生成」，一个是「以后自动生成」）。 */
  const period = ref("weekly");
  const draft = ref<ScheduleDraft>(emptyDraft());
  const editingId = ref<string | null>(null);

  let timer: ReturnType<typeof setInterval> | null = null;

  const items = computed<ReportSummary[]>(() => list.value?.items ?? []);
  const counts = computed(() => list.value?.counts ?? {});
  const tickSec = computed(() => list.value?.tick_sec ?? 0);
  const scheduleItems = computed<ReportSchedule[]>(() => schedules.value?.items ?? []);
  const scheduleCounts = computed(() => schedules.value?.counts ?? {});
  const insights = computed<ReportInsight[]>(() => detail.value?.insights ?? []);
  const editing = computed<boolean>(() => editingId.value !== null);

  /** 拉一次详情（选中一条报告时、以及采纳之后回读时用）。 */
  async function loadDetail(reportId: string): Promise<void> {
    detailLoading.value = true;
    try {
      detail.value = await api.fetchReport(reportId);
      selectedId.value = reportId;
      error.value = "";
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      detailLoading.value = false;
    }
  }

  /** 列表 + 周期一起拉（两块在同一屏上，分两次拉只会让它们不同步）。 */
  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      const [rows, plan] = await Promise.all([api.fetchReports(), api.fetchReportSchedules()]);
      list.value = rows;
      schedules.value = plan;
      error.value = "";
      const first = rows.items[0];
      // 空着详情框只会让人以为「没数据」；没选过就自动落到最新那一份。
      if (selectedId.value === null && first !== undefined) {
        await loadDetail(first.id);
      }
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      loading.value = false;
    }
  }

  function start(): void {
    void refresh();
    if (timer === null) {
      timer = setInterval(() => void refresh(), REPORT_POLL_MS);
    }
  }

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  async function select(reportId: string): Promise<void> {
    if (reportId === selectedId.value && detail.value !== null) return;
    await loadDetail(reportId);
  }

  /** 手动生成一份（同周期已存在 ⇒ 后端回那一份，**不重复插入**）。 */
  async function generate(): Promise<void> {
    generating.value = true;
    notice.value = "";
    try {
      const row = await api.generateReport({
        period: period.value as ReportGenerateBody["period"],
      });
      detail.value = row;
      selectedId.value = row.id;
      notice.value =
        row.trigger === "manual"
          ? `已生成${periodLabel(row.period)}（${windowText(row.start_date, row.end_date)}）`
          : `这个窗口已经有一份了（${triggerLabel(row.trigger)}）⇒ 直接给你那一份，没重复生成`;
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      generating.value = false;
    }
  }

  /** 采纳第 `index` 条建议（已采纳过再点一次什么都不做 —— 后端按原文幂等）。 */
  async function apply(index: number): Promise<void> {
    const reportId = selectedId.value;
    if (reportId === null) return;
    applyingIndex.value = index;
    try {
      const outcome = await api.applyInsight(reportId, index);
      detail.value = outcome.report;
      notice.value = applyText(outcome);
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      applyingIndex.value = null;
    }
  }

  /** 导出当前这一份（文件名由服务器给 —— 前端自己拼的名字迟早与落盘的那份分家）。 */
  async function download(
    format: "md" | "csv",
    save: typeof saveTextFile = saveTextFile,
  ): Promise<void> {
    const reportId = selectedId.value;
    if (reportId === null) return;
    busyId.value = reportId;
    try {
      const result = await api.exportReport(reportId, format);
      save(result.text, result.filename, format === "csv" ? "text/csv" : "text/markdown");
      notice.value = `已导出 ${result.filename}`;
      error.value = "";
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  // ── 周期（能建、能改、能停、能立刻生成一次）────────────────────

  async function toggleSchedule(row: ReportSchedule): Promise<void> {
    busyId.value = row.id;
    try {
      const fresh = await api.patchReportSchedule(row.id, { enabled: !row.enabled });
      notice.value = fresh.enabled
        ? `已启用，下一次 ${nextRunText(fresh)}`
        : "已停用（留痕里记着这一下）";
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  async function runSchedule(row: ReportSchedule): Promise<void> {
    busyId.value = row.id;
    try {
      const outcome = await api.runReportSchedule(row.id, "面板手动生成");
      notice.value = runText(outcome);
      error.value = "";
      await refresh();
      if (outcome.report_id !== null && outcome.report_id !== undefined) {
        await loadDetail(outcome.report_id);
      }
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  async function removeSchedule(row: ReportSchedule): Promise<void> {
    busyId.value = row.id;
    try {
      const outcome = await api.deleteReportSchedule(row.id);
      notice.value = outcome.message;
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  /** 表单提交：在编辑态就是改那一条，否则新建一条。 */
  async function saveDraft(): Promise<void> {
    saving.value = true;
    notice.value = "";
    try {
      if (editingId.value !== null) {
        const fresh = await api.patchReportSchedule(
          editingId.value,
          draftToPatch(draft.value, "面板编辑报告周期"),
        );
        notice.value = `已改：${scheduleSummary(fresh)}，下一次 ${nextRunText(fresh)}`;
      } else {
        const row = await api.createReportSchedule(draftToBody(draft.value));
        notice.value = `已建：${scheduleSummary(row)}，下一次 ${nextRunText(row)}`;
      }
      editingId.value = null;
      draft.value = emptyDraft();
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      saving.value = false;
    }
  }

  /** 点「编辑」⇒ 把那一行填进表单（表单此时是**改**而不是建）。 */
  function openEdit(row: ReportSchedule): void {
    editingId.value = row.id;
    draft.value = draftFromRow(row);
    notice.value = "";
  }

  function cancelEdit(): void {
    editingId.value = null;
    draft.value = emptyDraft();
  }

  function setDraft(patch: Partial<ScheduleDraft>): void {
    draft.value = { ...draft.value, ...patch };
  }

  /** 换周期时把「跟着周期走」的回看天数一起换掉（否则 weekly 改成 daily 会带着 7 天）。 */
  function setPeriod(next: string): void {
    draft.value = { ...draft.value, period: next, lookbackDays: lookbackFor(next) };
  }

  /** 勾 / 取消一个正文区块（`include` 是数组，不能整个覆盖）。 */
  function toggleInclude(kind: string): void {
    const has = draft.value.include.includes(kind);
    const next = has
      ? draft.value.include.filter((item) => item !== kind)
      : [...draft.value.include, kind];
    draft.value = { ...draft.value, include: next };
  }

  return {
    list,
    detail,
    schedules,
    selectedId,
    loading,
    detailLoading,
    generating,
    saving,
    busyId,
    applyingIndex,
    error,
    notice,
    period,
    draft,
    editingId,
    items,
    counts,
    tickSec,
    scheduleItems,
    scheduleCounts,
    insights,
    editing,
    refresh,
    start,
    stop,
    select,
    generate,
    apply,
    download,
    toggleSchedule,
    runSchedule,
    removeSchedule,
    saveDraft,
    openEdit,
    cancelEdit,
    setDraft,
    setPeriod,
    toggleInclude,
  };
});