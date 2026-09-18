// 定时发布计划状态（T5.6 · §04.6.5.1）。
//
// 这一块要回答三个问题
// --------------------
// ① 我排了哪些计划、下一次各是什么时候？—— 列表 + `next_run_at`；
// ② 上一个计划跑成什么样了？—— `last_result` / `fail_streak` / `run_count`；
// ③ 现在就要它跑一次 —— `run_now`（**仍走限频、仍看总开关**）。
//
// 为什么这一块**可以**慢轮询，而其余面板只在有活时轮询
// ----------------------------------------------------
// 别的面板轮询的是"某个进程正在做的事"，空闲时问它只是白问。这一块轮询的是
// **时间本身** —— 一个计划到点没到点，只有到点了才会变；而"它刚才自己跑了"这件事
// 恰恰是用户打开这一屏想看到的。15s 一拍、一屏四五个请求，代价可以忽略。
//
// 为什么平台清单来自 `/publish/platforms`
// --------------------------------------
// 与投递面板**同一份**：能选的平台、每个平台下的账号、以及"点了会怎样"的说明，
// 全是后端按 `config/publish.yaml` 现算的。前端列一份等于把那边的配置抄第二遍，
// 漏改的那一处表现为"这个平台在定时计划里选不到"—— 没有人会去报这个 bug。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { fetchPlatforms, type PublishPlatformOption } from "@/api/endpoints/publish";
import {
  createSchedule,
  deleteSchedule,
  fetchSchedules,
  patchSchedule,
  runSchedule,
  type Schedule,
  type ScheduleList,
  type ScheduleRunResult,
} from "@/api/endpoints/schedules";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";

/** 慢轮询周期（见模块注释：这一块轮询的是"时间"，不是"某个进程在忙"）。 */
export const SCHEDULE_POLL_MS = 15_000;

/** 三种模式（顺序 = 下拉框里的顺序：默认那个排第一）。 */
export const SCHEDULE_MODES: readonly string[] = ["daily_window", "at_time", "interval"];

/** 抖动上限（与后端 `MAX_JITTER_MIN` 同源；面板据此把输入框卡住）。 */
export const MAX_JITTER_MIN = 120;

const MODE_LABELS: Record<string, string> = {
  daily_window: "每天窗口内随机",
  at_time: "指定时刻（一次性）",
  interval: "固定间隔",
};

/** 模式 → 中文（不认识的**原样显示**，别把未知吞掉）。 */
export function modeLabel(mode: string): string {
  return MODE_LABELS[mode] ?? mode;
}

/** `last_result` → 中文。`error:…` 原样保留后半句 —— 那句话就是排查线索。 */
export function resultLabel(result: string | null | undefined): string {
  if (result === null || result === undefined || result === "") return "还没跑过";
  if (result.startsWith("error:")) return `失败：${result.slice("error:".length)}`;
  if (result === "ok") return "成功";
  if (result === "skipped_ratelimit") return "被限频，已顺延";
  if (result === "skipped_disabled") return "发布开关关着，空转";
  if (result === "skipped_duplicate") return "早就投过，没重复发";
  return result;
}

/** `last_result` → 灯色。 */
export function resultTone(result: string | null | undefined): StatusTone {
  if (result === null || result === undefined || result === "") return "idle";
  if (result === "ok") return "ok";
  if (result.startsWith("error:")) return "error";
  return "warn";
}

/**
 * ISO → 本地 `MM-DD HH:MM`（计划跨天，日期不能省）。
 *
 * **必须换算，不能截字符串**：库里存的是 UTC（``…Z``），截出来是 UTC 的钟点 ——
 * 一个排在 18:00-21:30 窗口里的计划会显示成"下一次 10:30"，
 * 与它自己那行窗口参数**当场自相矛盾**。时区取浏览器真实偏移（同 `localToIso`）。
 */
export function stampText(ts: string | null | undefined): string {
  if (ts === null || ts === undefined || ts === "") return "-";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "-";
  const pad = (value: number): string => String(value).padStart(2, "0");
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return `${day} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 一条计划的参数摘要（模式 + 该模式真正生效的那几个字段）。 */
export function scheduleSummary(row: Schedule): string {
  if (row.mode === "daily_window") {
    const window = row.window ?? [];
    return `每天 ${window[0] ?? "?"} - ${window[1] ?? "?"}，抖动 ±${row.jitter_min} 分钟`;
  }
  if (row.mode === "at_time") return `指定时刻 ${stampText(row.at_time)}`;
  if (row.mode === "interval") return `每 ${row.interval_hours ?? "?"} 小时`;
  return modeLabel(row.mode);
}

/** 下一次触发的说明（停用 / 一次性跑完 ⇒ 说清楚"不会再触发"，而不是显示一个 `-`）。 */
export function nextRunText(row: Schedule): string {
  if (!row.enabled) return "已停用";
  if (row.next_run_at === null) return "不会再触发";
  return stampText(row.next_run_at);
}

/**
 * `datetime-local` 的值（本地时间、不带时区）→ 后端要的 ISO（**必须带时区**）。
 *
 * 时区取**浏览器**的真实偏移，不写死 `+08:00`：写死之后，任何一台不在东八区的机器
 * 上排出来的计划都会差几个小时，而面板上显示的时刻还是对的 —— 那是最难查的一类。
 */
export function localToIso(value: string): string {
  if (value === "") return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const pad = (value: number): string => String(value).padStart(2, "0");
  const hours = pad(Math.floor(Math.abs(offsetMinutes) / 60));
  const minutes = pad(Math.abs(offsetMinutes) % 60);
  return `${value}:00${sign}${hours}:${minutes}`;
}

/** 一个计划的运行结论 → 一行人话（面板上那条提示）。 */
export function runText(outcome: ScheduleRunResult): string {
  if (outcome.result === "ok") return `已建 ${outcome.queued} 个发布作业，去「待发布」看它`;
  if (outcome.result === "skipped_ratelimit") return "被限频挡住了：额度恢复前不会再试（这不是失败）";
  if (outcome.result === "skipped_disabled") return "发布总开关是关的 ⇒ 空转，没建作业";
  if (outcome.result === "skipped_duplicate") return "这条任务早就投过 ⇒ 没重复发（也不用改什么）";
  if (outcome.result.startsWith("error:")) return outcome.result.slice("error:".length);
  return outcome.result;
}

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface SchedulesApi {
  fetchSchedules: typeof fetchSchedules;
  fetchPlatforms: typeof fetchPlatforms;
  createSchedule: typeof createSchedule;
  patchSchedule: typeof patchSchedule;
  deleteSchedule: typeof deleteSchedule;
  runSchedule: typeof runSchedule;
}

let api: SchedulesApi = {
  fetchSchedules,
  fetchPlatforms,
  createSchedule,
  patchSchedule,
  deleteSchedule,
  runSchedule,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureSchedulesApi(overrides: Partial<SchedulesApi>): void {
  api = { ...api, ...overrides };
}

/** 新建表单的初值（Q14 的默认策略：18:00-21:30 + 15 分钟抖动）。 */
export interface ScheduleDraft {
  platform: string;
  mode: string;
  windowStart: string;
  windowEnd: string;
  atTime: string;
  intervalHours: number;
  jitterMin: number;
  taskId: string;
  enabled: boolean;
}

export function emptyDraft(platform = ""): ScheduleDraft {
  return {
    platform,
    mode: "daily_window",
    windowStart: "18:00",
    windowEnd: "21:30",
    atTime: "",
    intervalHours: 6,
    jitterMin: 15,
    taskId: "",
    enabled: true,
  };
}

/** 表单 → 请求体（**校验留给后端**：非法参数必须是 400，而不是前端自己拼一个 422）。 */
export function draftToBody(draft: ScheduleDraft, accountId: string | null): Record<string, unknown> {
  const body: Record<string, unknown> = {
    platforms: draft.platform === "" ? [] : [draft.platform],
    account_ids: accountId === null ? [] : [accountId],
    mode: draft.mode,
    jitter_min: draft.jitterMin,
    enabled: draft.enabled,
    task_id: draft.taskId.trim() === "" ? null : draft.taskId.trim(),
  };
  if (draft.mode === "daily_window") {
    body.window = [draft.windowStart, draft.windowEnd];
  } else if (draft.mode === "at_time") {
    body.at_time = localToIso(draft.atTime);
  } else {
    body.interval_hours = draft.intervalHours;
  }
  return body;
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const useSchedulesStore = defineStore("schedules", () => {
  const list = ref<ScheduleList | null>(null);
  const platforms = ref<PublishPlatformOption[]>([]);
  const loading = ref(false);
  const saving = ref(false);
  const busyId = ref<string | null>(null);
  const error = ref("");
  const notice = ref("");
  const draft = ref<ScheduleDraft>(emptyDraft());

  let timer: ReturnType<typeof setInterval> | null = null;

  const items = computed<Schedule[]>(() => list.value?.items ?? []);
  const counts = computed(() => list.value?.counts ?? {});
  const tickSec = computed(() => list.value?.tick_sec ?? 0);

  /** 能建的平台（后端算好的 `selectable`：平台启用 **且** 有启用的账号）。 */
  const selectable = computed(() => platforms.value.filter((item) => item.selectable));
  /** 当前选中平台下的账号（T5.8 之前一个平台只有一个）。 */
  const draftAccounts = computed(() => {
    const found = platforms.value.find((item) => item.code === draft.value.platform);
    return found?.accounts ?? [];
  });
  const draftAccountId = computed<string | null>(() => draftAccounts.value[0] ?? null);

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      const [rows, options] = await Promise.all([api.fetchSchedules(), api.fetchPlatforms()]);
      list.value = rows;
      platforms.value = options.items ?? [];
      error.value = "";
      if (draft.value.platform === "" && selectable.value.length > 0) {
        draft.value = { ...draft.value, platform: selectable.value[0]!.code };
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
      timer = setInterval(() => void refresh(), SCHEDULE_POLL_MS);
    }
  }

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  async function create(): Promise<void> {
    saving.value = true;
    notice.value = "";
    try {
      const row = await api.createSchedule(
        draftToBody(draft.value, draftAccountId.value) as never,
      );
      notice.value = `已建：${scheduleSummary(row)}，下一次 ${nextRunText(row)}`;
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      saving.value = false;
    }
  }

  async function toggle(row: Schedule): Promise<void> {
    busyId.value = row.id;
    try {
      const fresh = await api.patchSchedule(row.id, { enabled: !row.enabled } as never);
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

  async function run(row: Schedule): Promise<void> {
    busyId.value = row.id;
    try {
      const outcome = await api.runSchedule(row.id, "面板手动执行");
      notice.value = runText(outcome);
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  async function remove(row: Schedule): Promise<void> {
    busyId.value = row.id;
    try {
      const outcome = await api.deleteSchedule(row.id);
      notice.value = outcome.message;
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  function setDraft(patch: Partial<ScheduleDraft>): void {
    draft.value = { ...draft.value, ...patch };
  }

  return {
    list,
    platforms,
    loading,
    saving,
    busyId,
    error,
    notice,
    draft,
    items,
    counts,
    tickSec,
    selectable,
    draftAccounts,
    draftAccountId,
    refresh,
    start,
    stop,
    create,
    toggle,
    run,
    remove,
    setDraft,
  };
});