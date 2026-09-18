import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ReportApplyResult,
  ReportDetail,
  ReportInsight,
  ReportList,
  ReportSchedule,
  ReportScheduleList,
  ReportScheduleRunResult,
  ReportSummary,
} from "@/api/endpoints/reports";
import type { DownloadResult } from "@/api/http";
import { ApiError } from "@/api/http";
import {
  DEFAULT_INCLUDE,
  DEFAULT_LOOKBACK,
  INCLUDE_KINDS,
  PERIODS,
  REPORT_POLL_MS,
  WEEKDAYS,
  type ReportsApi,
  applyText,
  confidenceLabel,
  confidenceTone,
  configureReportsApi,
  countText,
  draftFromRow,
  draftToBody,
  draftToPatch,
  emptyDraft,
  includeLabel,
  insightEvidenceText,
  insightIsShaky,
  insightKindLabel,
  lookbackFor,
  nextRunText,
  periodLabel,
  resultLabel,
  resultTone,
  runText,
  scheduleSummary,
  stampText,
  summaryText,
  triggerLabel,
  useReportsStore,
  weekdayLabel,
  windowText,
} from "./reports";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function insight(overrides: Partial<ReportInsight> = {}): ReportInsight {
  return {
    kind: "topic",
    statement: "「反差开场」的播放中位数比「提问开场」高 32.0%",
    suggested_action: "下轮选题把这个钩子类型的占比提上来",
    confidence: "high",
    evidence: {
      dimension: "by_hook_type",
      metric: "views_median",
      n: 12,
      a: "反差",
      a_value: 12000,
      b: "提问",
      b_value: 9000,
      delta_pct: 32,
    },
    applied: false,
    applied_at: null,
    applied_by: null,
    ...overrides,
  };
}

function summary(overrides: Partial<ReportSummary> = {}): ReportSummary {
  return {
    id: "rep-1",
    period: "weekly",
    start_date: "2026-09-10",
    end_date: "2026-09-16",
    generated_at: "2026-09-17T01:00:00.000Z",
    trigger: "manual",
    task_count: 3,
    publish_count: 2,
    total_views: 24000,
    median_views: 12000,
    llm_cost_usd: 0.0123,
    insights_count: 1,
    applied_count: 0,
    ...overrides,
  };
}

function detail(overrides: Partial<ReportDetail> = {}): ReportDetail {
  return {
    ...summary(),
    summary_md: "# 周报\n\n正文",
    data: { by_hook_type: {} },
    insights: [insight()],
    artifacts: ["data/output/reports/weekly_2026-09-10_2026-09-16.md"],
    coverage: {},
    avg_views: 12000,
    total_likes: 10,
    total_comments: 1,
    total_shares: 0,
    tts_skip_ratio: null,
    ...overrides,
  };
}

function board(overrides: Partial<ReportList> = {}): ReportList {
  return {
    items: [summary()],
    counts: { total: 1, with_insights: 1, applied: 0 },
    periods: ["daily", "weekly", "monthly"],
    tick_sec: 30,
    ...overrides,
  };
}

function schedule(overrides: Partial<ReportSchedule> = {}): ReportSchedule {
  return {
    id: "rsched_weekly",
    period: "weekly",
    weekday: 0,
    day_of_month: null,
    at_time: "09:00",
    tz: "Asia/Shanghai",
    lookback_days: 7,
    include: ["publish", "metrics", "topics", "quality", "cost"],
    enabled: true,
    is_builtin: true,
    next_run_at: "2026-09-21T01:00:00.000Z",
    last_run_at: null,
    last_report_id: null,
    last_result: null,
    fail_streak: 0,
    created_by: "seed",
    created_at: "2026-09-01T00:00:00.000Z",
    updated_at: "2026-09-01T00:00:00.000Z",
    ...overrides,
  };
}

function scheduleList(overrides: Partial<ReportScheduleList> = {}): ReportScheduleList {
  return {
    items: [schedule()],
    counts: { total: 1, enabled: 1, disabled: 0, builtin: 1, failing: 0 },
    tick_sec: 30,
    ...overrides,
  };
}

function fired(overrides: Partial<ReportScheduleRunResult> = {}): ReportScheduleRunResult {
  return {
    schedule_id: "rsched_weekly",
    result: "ok",
    report_id: "rep-1",
    next_run_at: "2026-09-28T01:00:00.000Z",
    note: "weekly 2026-09-10~2026-09-16",
    ...overrides,
  };
}

function applied(overrides: Partial<ReportApplyResult> = {}): ReportApplyResult {
  return {
    report_id: "rep-1",
    index: 0,
    applied: true,
    message: "已采纳 ⇒ 写进人物偏好，下一轮 Planner 就会读到",
    style_hint: "【数据结论 2026-09-18】……",
    report: detail({ insights: [insight({ applied: true })], applied_count: 1 }),
    ...overrides,
  };
}

function exported(overrides: Partial<DownloadResult> = {}): DownloadResult {
  return {
    text: "# 周报\n",
    filename: "weekly_2026-09-10_2026-09-16.md",
    rows: 12,
    ...overrides,
  };
}

const STAMP = new Intl.DateTimeFormat("en-GB", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** 独立实现的本地 `MM-DD HH:MM`（用 Intl，与 store 里的 getHours 不是同一套代码）。 */
function localStamp(iso: string): string {
  const parts = STAMP.formatToParts(new Date(iso));
  const pick = (type: string): string => parts.find((part) => part.type === type)?.value ?? "";
  return `${pick("month")}-${pick("day")} ${pick("hour")}:${pick("minute")}`;
}

/** 每个用例一套新的假件；`api` 让用例能断言「谁被调了、调了几次」。 */
let api: { [K in keyof ReportsApi]: ReturnType<typeof vi.fn> };

beforeEach(() => {
  setActivePinia(createPinia());
  api = {
    fetchReports: vi.fn(async () => board()),
    fetchReport: vi.fn(async () => detail()),
    generateReport: vi.fn(async () => detail()),
    applyInsight: vi.fn(async () => applied()),
    exportReport: vi.fn(async () => exported()),
    fetchReportSchedules: vi.fn(async () => scheduleList()),
    createReportSchedule: vi.fn(async () => schedule()),
    patchReportSchedule: vi.fn(async () => schedule()),
    deleteReportSchedule: vi.fn(async () => ({
      schedule_id: "rsched_weekly",
      deleted: true,
      message: "已删除这条周期",
    })),
    runReportSchedule: vi.fn(async () => fired()),
  };
  configureReportsApi(api);
});

afterEach(() => {
  vi.useRealTimers();
});
// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("周期词：三种都翻成中文，认不出的照原样（别把未知吞掉）", () => {
    expect(periodLabel("daily")).toBe("日报");
    expect(periodLabel("weekly")).toBe("周报");
    expect(periodLabel("monthly")).toBe("月报");
    expect(periodLabel("quarterly")).toBe("quarterly");
  });

  it("下拉框里的三种周期：默认那个排第一（排错了会让人以为只有一种）", () => {
    expect(PERIODS).toEqual(["daily", "weekly", "monthly"]);
  });

  it("星期：0=周一（与后端 `validate_period_fields` 的约定一致）", () => {
    expect(WEEKDAYS[0]).toBe("周一");
    expect(weekdayLabel(0)).toBe("周一");
    expect(weekdayLabel(6)).toBe("周日");
    expect(weekdayLabel(null)).toBe("");
    expect(weekdayLabel(undefined)).toBe("");
    expect(weekdayLabel(9)).toBe("第 9 天");
  });

  it("置信度词 + 灯色：低=灰（不是故障）、中=黄、高=绿", () => {
    expect(confidenceLabel("low")).toBe("低");
    expect(confidenceLabel("medium")).toBe("中");
    expect(confidenceLabel("high")).toBe("高");
    expect(confidenceLabel("weird")).toBe("weird");
    expect(confidenceTone("high")).toBe("ok");
    expect(confidenceTone("medium")).toBe("warn");
    expect(confidenceTone("low")).toBe("idle");
    expect(confidenceTone("weird")).toBe("idle");
  });

  it("建议种类词：五种都翻，认不出的照原样", () => {
    expect(insightKindLabel("topic")).toBe("选题");
    expect(insightKindLabel("timing")).toBe("时段");
    expect(insightKindLabel("platform")).toBe("平台");
    expect(insightKindLabel("quality")).toBe("质量");
    expect(insightKindLabel("cost")).toBe("成本");
    expect(insightKindLabel("vibes")).toBe("vibes");
  });

  it("正文六个区块：与后端 `INCLUDE_KINDS` 同源；默认勾选里没有 errors", () => {
    expect(INCLUDE_KINDS).toEqual(["publish", "metrics", "topics", "quality", "cost", "errors"]);
    expect(DEFAULT_INCLUDE).toEqual(["publish", "metrics", "topics", "quality", "cost"]);
    expect(includeLabel("publish")).toBe("发布量");
    expect(includeLabel("errors")).toBe("错误");
    expect(includeLabel("weird")).toBe("weird");
  });

  it("触发方式：手动 / 到点，认不出的照原样", () => {
    expect(triggerLabel("manual")).toBe("手动生成");
    expect(triggerLabel("scheduled")).toBe("到点生成");
    expect(triggerLabel("weird")).toBe("weird");
  });

  it("周期的上一次结论：四种结果 + 没跑过 + 认不出的照原样", () => {
    expect(resultLabel(null)).toBe("还没跑过");
    expect(resultLabel("")).toBe("还没跑过");
    expect(resultLabel("ok")).toBe("已生成");
    expect(resultLabel("skipped_disabled")).toBe("停用中，空转");
    expect(resultLabel("skipped_no_data")).toBe("窗口里没数据");
    expect(resultLabel("error:数据库锁住了")).toBe("失败：数据库锁住了");
    expect(resultLabel("whatever")).toBe("whatever");
  });

  it("周期灯色：生成绿、失败红、空转黄、没跑过灰", () => {
    expect(resultTone(null)).toBe("idle");
    expect(resultTone("ok")).toBe("ok");
    expect(resultTone("error:boom")).toBe("error");
    expect(resultTone("skipped_no_data")).toBe("warn");
  });

  it("数量：过万折成万，空值给破折号（**不给 0** —— 0 与「没测到」是两件事）", () => {
    expect(countText(null)).toBe("—");
    expect(countText(undefined)).toBe("—");
    expect(countText(0)).toBe("0");
    expect(countText(999)).toBe("999");
    expect(countText(10000)).toBe("1.0万");
    expect(countText(12345)).toBe("1.2万");
    expect(countText(-15000)).toBe("-1.5万");
  });

  it("时刻：UTC 换算成本地 MM-DD HH:MM（截字符串会把 01:00 显示成 01:00 之外的东西）", () => {
    expect(stampText("2026-09-17T01:00:00.000Z")).toBe(localStamp("2026-09-17T01:00:00.000Z"));
    expect(stampText("2026-09-17T01:00:00.000Z")).toMatch(/^\d{2}-\d{2} \d{2}:\d{2}$/);
  });

  it("时刻：没有 / 空的 / 认不出的给 -（不显示半截时间，也不显示 Invalid Date）", () => {
    expect(stampText(null)).toBe("-");
    expect(stampText(undefined)).toBe("-");
    expect(stampText("")).toBe("-");
    expect(stampText("不是时间")).toBe("-");
  });

  it("窗口：同年只写月日（每行都顶着重复的年份最没用）", () => {
    expect(windowText("2026-09-10", "2026-09-16")).toBe("09-10 ~ 09-16");
    expect(windowText("2025-12-30", "2026-01-05")).toBe("2025-12-30 ~ 2026-01-05");
    expect(windowText("nope", "also")).toBe("nope ~ also");
  });

  it("窗口：**按字符串切**而不是 `new Date`（后者按 UTC 解析，西半球会退一天）", () => {
    expect(windowText("2026-09-10", "2026-09-10")).toBe("09-10 ~ 09-10");
  });

  it("列表摘要：发布数 + 中位播放 + 建议数，采纳过才提采纳", () => {
    expect(summaryText(summary({ applied_count: 0 }))).toBe(
      "发布 2 条 · 中位播放 1.2万 · 建议 1 条 · 成本 $0.0123",
    );
    expect(summaryText(summary({ applied_count: 2 }))).toContain("已采纳 2 条");
    expect(summaryText(summary({ median_views: null, llm_cost_usd: null }))).toBe(
      "发布 2 条 · 中位播放 — · 建议 1 条",
    );
  });

  it("证据行：对比型给 n + A/B + 相对差", () => {
    expect(insightEvidenceText(insight())).toBe("n=12 · 反差（1.2万） · 提问（9000） · 相对差 32.0%");
  });

  it("证据行：成本型给 n + 单条花费；已采纳也写出来", () => {
    const cost = insight({
      kind: "cost",
      applied: true,
      evidence: { metric: "llm_cost_usd", n: 5, total_usd: 0.06, per_video_usd: 0.012, calls: 5 },
    });
    expect(insightEvidenceText(cost)).toBe("n=5 · 单条 $0.0120 · 已采纳");
  });

  it("证据行：evidence 是空对象也不崩（老报告可能没有证据）", () => {
    expect(insightEvidenceText(insight({ evidence: {} }))).toBe("");
  });

  it("样本不足：只有 low 才算不牢（面板据此强制把警告摆出来）", () => {
    expect(insightIsShaky(insight({ confidence: "low" }))).toBe(true);
    expect(insightIsShaky(insight({ confidence: "medium" }))).toBe(false);
    expect(insightIsShaky(insight({ confidence: "high" }))).toBe(false);
  });

  it("周期摘要：三种周期各显示真正生效的那几项 + 回看", () => {
    expect(scheduleSummary(schedule())).toBe("每周一 09:00（回看 7 天）");
    expect(scheduleSummary(schedule({ weekday: 6 }))).toBe("每周日 09:00（回看 7 天）");
    expect(
      scheduleSummary(
        schedule({ period: "monthly", weekday: null, day_of_month: 1, lookback_days: 31 }),
      ),
    ).toBe("每月 1 日 09:00（回看 31 天）");
    expect(
      scheduleSummary(
        schedule({ period: "daily", weekday: null, day_of_month: null, lookback_days: 1 }),
      ),
    ).toBe("每天 09:00（回看 1 天）");
  });

  it("周期摘要：monthly 缺 day_of_month 时显示 ?（不是 undefined）", () => {
    expect(scheduleSummary(schedule({ period: "monthly", day_of_month: null }))).toBe(
      "每月 ? 日 09:00（回看 7 天）",
    );
  });

  it("下一次触发：停用 / 还没排期都要说清楚，而不是显示一个 -", () => {
    expect(nextRunText(schedule({ enabled: false }))).toBe("已停用");
    expect(nextRunText(schedule({ next_run_at: null }))).toBe("待排期（下一拍补上）");
    expect(nextRunText(schedule())).toBe(localStamp("2026-09-21T01:00:00.000Z"));
  });

  it("立刻生成的结论 → 一行人话：四种都覆盖，认不出的照原样", () => {
    expect(runText(fired())).toBe("已生成 rep-1，去上面看它");
    expect(runText(fired({ result: "skipped_disabled", report_id: null }))).toBe(
      "这条周期是停用的 ⇒ 没生成（先启用它）",
    );
    expect(runText(fired({ result: "skipped_no_data", report_id: null, note: "没有数据" }))).toBe(
      "没有数据",
    );
    expect(
      runText(fired({ result: "skipped_no_data", report_id: null, note: null })),
    ).toContain("不是故障");
    expect(runText(fired({ result: "error:锁住了", report_id: null }))).toBe("锁住了");
    expect(runText(fired({ result: "surprise", report_id: null }))).toBe("surprise");
  });

  it("采纳的结论：真采纳用那句话；幂等命中用后端给的那句（它更准）", () => {
    expect(applyText(applied())).toBe("已采纳 ⇒ 写进人物偏好，下一轮 Planner 就会读到");
    expect(applyText(applied({ applied: false, message: "这条建议本来就采纳过，没有重复写" }))).toBe(
      "这条建议本来就采纳过，没有重复写",
    );
  });

  it("回看天数默认值：与后端 `DEFAULT_LOOKBACK_DAYS` 同源，认不出的周期退回 7", () => {
    expect(DEFAULT_LOOKBACK).toEqual({ daily: 1, weekly: 7, monthly: 31 });
    expect(lookbackFor("daily")).toBe(1);
    expect(lookbackFor("monthly")).toBe(31);
    expect(lookbackFor("quarterly")).toBe(7);
  });

  it("表单初值：周报 + 周一 09:00 + 回看 7 天 + 默认五个区块 + 启用", () => {
    expect(emptyDraft()).toMatchObject({
      period: "weekly",
      weekday: 0,
      dayOfMonth: 1,
      atTime: "09:00",
      lookbackDays: 7,
      enabled: true,
    });
    expect(emptyDraft().include).toEqual([...DEFAULT_INCLUDE]);
  });

  it("表单初值：include 空的周期（老数据）⇒ 回到默认勾选，而不是一个都不勾", () => {
    expect(draftFromRow(schedule({ include: [] })).include).toEqual([...DEFAULT_INCLUDE]);
    expect(draftFromRow(schedule({ include: ["publish"] })).include).toEqual(["publish"]);
  });

  it("表单初值：从一行周期读出来（weekly 缺 weekday ⇒ 退回周一）", () => {
    const draft = draftFromRow(
      schedule({ period: "monthly", weekday: null, day_of_month: 15, lookback_days: 31 }),
    );
    expect(draft).toMatchObject({
      period: "monthly",
      weekday: 0,
      dayOfMonth: 15,
      lookbackDays: 31,
      enabled: true,
    });
  });

  it("表单 → 请求体：不生效的字段给 null（给 0 会被当成「你确实填了」）", () => {
    const weekly = draftToBody(emptyDraft());
    expect(weekly).toMatchObject({
      period: "weekly",
      at_time: "09:00",
      tz: "Asia/Shanghai",
      weekday: 0,
      day_of_month: null,
      lookback_days: 7,
      enabled: true,
    });
    expect(weekly.include).toEqual([...DEFAULT_INCLUDE]);

    const daily = draftToBody({ ...emptyDraft(), period: "daily" });
    expect(daily.weekday).toBeNull();
    expect(daily.day_of_month).toBeNull();

    const monthly = draftToBody({ ...emptyDraft(), period: "monthly", dayOfMonth: 15 });
    expect(monthly.weekday).toBeNull();
    expect(monthly.day_of_month).toBe(15);
  });

  it("表单 → 改一条：理由留空给 null（不是空串），有就给去掉空白的那句", () => {
    expect(draftToPatch(emptyDraft(), "   ").reason).toBeNull();
    expect(draftToPatch(emptyDraft(), "  面板编辑  ").reason).toBe("面板编辑");
    expect(draftToPatch(emptyDraft(), "").period).toBe("weekly");
  });

  it("轮询周期是慢轮询：一份报告只可能在到点那一刻多出来", () => {
    expect(REPORT_POLL_MS).toBe(60_000);
  });
});
// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("store：读取", () => {
  it("refresh：列表与周期一起拉（同一屏的两块，分两次拉只会让它们不同步）", async () => {
    const store = useReportsStore();
    await store.refresh();
    expect(api.fetchReports).toHaveBeenCalledTimes(1);
    expect(api.fetchReportSchedules).toHaveBeenCalledTimes(1);
    expect(store.items).toHaveLength(1);
    expect(store.scheduleItems).toHaveLength(1);
    expect(store.error).toBe("");
  });

  it("refresh：没选过就自动落到最新那一份（空着详情框会让人以为没数据）", async () => {
    const store = useReportsStore();
    await store.refresh();
    expect(api.fetchReport).toHaveBeenCalledWith("rep-1");
    expect(store.selectedId).toBe("rep-1");
    expect(store.detail?.id).toBe("rep-1");
  });

  it("refresh：列表为空 ⇒ 不发详情请求（问了也是白问）", async () => {
    api.fetchReports.mockResolvedValue(board({ items: [] }));
    const store = useReportsStore();
    await store.refresh();
    expect(api.fetchReport).not.toHaveBeenCalled();
    expect(store.selectedId).toBeNull();
    expect(store.detail).toBeNull();
  });

  it("refresh：拉不到 ⇒ error 有人话，loading 回落（不留一个转不完的圈）", async () => {
    api.fetchReports.mockRejectedValue(new ApiError("炸了", 500, null));
    const store = useReportsStore();
    await store.refresh();
    expect(store.error).toBe("炸了（HTTP 500）");
    expect(store.loading).toBe(false);
  });

  it("select：点同一份且详情已在手上 ⇒ 不重复请求", async () => {
    const store = useReportsStore();
    await store.refresh();
    await store.select("rep-1");
    expect(api.fetchReport).toHaveBeenCalledTimes(1);
  });

  it("select：换一份就拉那一份", async () => {
    api.fetchReports.mockResolvedValue(
      board({ items: [summary(), summary({ id: "rep-2", period: "daily" })] }),
    );
    const store = useReportsStore();
    await store.refresh();
    await store.select("rep-2");
    expect(api.fetchReport).toHaveBeenLastCalledWith("rep-2");
    expect(store.selectedId).toBe("rep-2");
  });

  it("select：详情还没拉到（比如上一份拉失败）⇒ 还是要去拉，不能凭 selectedId 空转", async () => {
    api.fetchReport.mockRejectedValueOnce(new ApiError("炸了", 500, null));
    const store = useReportsStore();
    await store.refresh();
    expect(store.detail).toBeNull();
    await store.select("rep-1");
    expect(api.fetchReport).toHaveBeenCalledTimes(2);
    expect(store.detail?.id).toBe("rep-1");
  });

  it("start / stop：进面板拉一次并开始慢轮询；重复 start 不会多起一个定时器", async () => {
    vi.useFakeTimers();
    const store = useReportsStore();
    store.start();
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(api.fetchReports).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(REPORT_POLL_MS);
    expect(api.fetchReports).toHaveBeenCalledTimes(3);
    store.stop();
    await vi.advanceTimersByTimeAsync(REPORT_POLL_MS * 3);
    expect(api.fetchReports).toHaveBeenCalledTimes(3);
  });
});

describe("store：生成与采纳", () => {
  it("generate：手动生成一份 ⇒ 选中它、提示带上窗口、顺手刷新列表", async () => {
    const store = useReportsStore();
    await store.refresh();
    api.generateReport.mockResolvedValue(
      detail({ id: "rep-9", period: "daily", start_date: "2026-09-17", end_date: "2026-09-17" }),
    );
    store.period = "daily";
    await store.generate();
    expect(api.generateReport).toHaveBeenCalledWith({ period: "daily" });
    expect(store.selectedId).toBe("rep-9");
    expect(store.notice).toContain("日报");
    expect(store.notice).toContain("09-17");
    expect(api.fetchReports).toHaveBeenCalledTimes(2);
    expect(store.generating).toBe(false);
  });

  it("generate：同一个窗口已经有一份 ⇒ 说清「没重复生成」（后端回的是老那一份）", async () => {
    const store = useReportsStore();
    await store.refresh();
    api.generateReport.mockResolvedValue(detail({ trigger: "scheduled" }));
    await store.generate();
    expect(store.notice).toContain("没重复生成");
  });

  it("generate：失败 ⇒ error 有人话（非法周期是 400，不是静默失败）", async () => {
    const store = useReportsStore();
    api.generateReport.mockRejectedValue(new ApiError("不认识的报告周期", 400, null));
    await store.generate();
    expect(store.error).toBe("不认识的报告周期（HTTP 400）");
    expect(store.generating).toBe(false);
  });

  it("apply：采纳第 0 条 ⇒ 详情换成后端回的那一份（已采纳标记在里面）", async () => {
    const store = useReportsStore();
    await store.refresh();
    await store.apply(0);
    expect(api.applyInsight).toHaveBeenCalledWith("rep-1", 0);
    expect(store.detail?.applied_count).toBe(1);
    expect(store.insights[0]?.applied).toBe(true);
    expect(store.notice).toContain("写进人物偏好");
    expect(store.applyingIndex).toBeNull();
  });

  it("apply：没有选中任何一份 ⇒ 一个请求都不发", async () => {
    const store = useReportsStore();
    await store.apply(0);
    expect(api.applyInsight).not.toHaveBeenCalled();
  });

  it("apply：越界 index ⇒ error 有人话，applyingIndex 回落", async () => {
    const store = useReportsStore();
    await store.refresh();
    api.applyInsight.mockRejectedValue(new ApiError("这份报告里没有第 7 条建议", 400, null));
    await store.apply(7);
    expect(store.error).toBe("这份报告里没有第 7 条建议（HTTP 400）");
    expect(store.applyingIndex).toBeNull();
  });
});

describe("store：导出", () => {
  it("download：Markdown 的文件名由服务器给（前端自己拼的名字迟早与落盘的那份分家）", async () => {
    const store = useReportsStore();
    await store.refresh();
    const save = vi.fn();
    await store.download("md", save);
    expect(api.exportReport).toHaveBeenCalledWith("rep-1", "md");
    expect(save).toHaveBeenCalledWith(
      "# 周报\n",
      "weekly_2026-09-10_2026-09-16.md",
      "text/markdown",
    );
    expect(store.notice).toContain("weekly_2026-09-10_2026-09-16.md");
    expect(store.busyId).toBeNull();
  });

  it("download：CSV 给 csv 的 mime（别让浏览器按纯文本存）", async () => {
    const store = useReportsStore();
    await store.refresh();
    api.exportReport.mockResolvedValue(exported({ filename: "weekly.csv", text: "a,b\n" }));
    const save = vi.fn();
    await store.download("csv", save);
    expect(save).toHaveBeenCalledWith("a,b\n", "weekly.csv", "text/csv");
  });

  it("download：失败 ⇒ error 有人话，busyId 回落", async () => {
    const store = useReportsStore();
    await store.refresh();
    api.exportReport.mockRejectedValue(new ApiError("这份报告不存在", 404, null));
    await store.download("md", vi.fn());
    expect(store.error).toBe("这份报告不存在（HTTP 404）");
    expect(store.busyId).toBeNull();
  });

  it("download：没选中任何一份 ⇒ 一个请求都不发", async () => {
    const store = useReportsStore();
    await store.download("md", vi.fn());
    expect(api.exportReport).not.toHaveBeenCalled();
  });
});

describe("store：周期", () => {
  it("toggleSchedule：停用 ⇒ PATCH 只给 enabled 这一个字段", async () => {
    const store = useReportsStore();
    await store.refresh();
    api.patchReportSchedule.mockResolvedValue(schedule({ enabled: false }));
    await store.toggleSchedule(schedule({ enabled: true }));
    expect(api.patchReportSchedule).toHaveBeenCalledWith("rsched_weekly", { enabled: false });
    expect(store.notice).toContain("已停用");
    expect(store.busyId).toBeNull();
  });

  it("toggleSchedule：启用 ⇒ 提示里带上下一次（不然人不知道它到底排上了没有）", async () => {
    const store = useReportsStore();
    await store.refresh();
    api.patchReportSchedule.mockResolvedValue(schedule({ enabled: true }));
    await store.toggleSchedule(schedule({ enabled: false }));
    expect(store.notice).toContain("已启用");
    expect(store.notice).toContain(localStamp("2026-09-21T01:00:00.000Z"));
  });

  it("runSchedule：生成出来了 ⇒ 提示 + 顺带把那一份拉出来看", async () => {
    const store = useReportsStore();
    await store.refresh();
    await store.runSchedule(schedule());
    expect(api.runReportSchedule).toHaveBeenCalledWith("rsched_weekly", "面板手动生成");
    expect(store.notice).toContain("已生成 rep-1");
    expect(api.fetchReport).toHaveBeenLastCalledWith("rep-1");
    expect(store.busyId).toBeNull();
  });

  it("runSchedule：窗口里没数据 ⇒ 用后端那句 note，不去拉详情（没有详情可拉）", async () => {
    const store = useReportsStore();
    await store.refresh();
    const before = api.fetchReport.mock.calls.length;
    api.runReportSchedule.mockResolvedValue(
      fired({ result: "skipped_no_data", report_id: null, note: "这个窗口里没有数据" }),
    );
    await store.runSchedule(schedule());
    expect(store.notice).toBe("这个窗口里没有数据");
    expect(api.fetchReport.mock.calls.length).toBe(before);
  });

  it("runSchedule：失败 ⇒ error 有人话（连续失败由后端记账，面板只显示）", async () => {
    const store = useReportsStore();
    api.runReportSchedule.mockRejectedValue(new ApiError("数据库锁住了", 500, null));
    await store.runSchedule(schedule());
    expect(store.error).toBe("数据库锁住了（HTTP 500）");
    expect(store.busyId).toBeNull();
  });

  it("removeSchedule：用后端那句 message（内置的删不掉时它正好说明了原因）", async () => {
    const store = useReportsStore();
    await store.refresh();
    api.deleteReportSchedule.mockResolvedValue({
      schedule_id: "rsched_weekly",
      deleted: false,
      message: "这是出厂自带的周期，只能停用",
    });
    await store.removeSchedule(schedule());
    expect(store.notice).toBe("这是出厂自带的周期，只能停用");
    expect(store.busyId).toBeNull();
  });

  it("saveDraft：新建 ⇒ 带上表单里的字段，建完表单复位", async () => {
    const store = useReportsStore();
    await store.refresh();
    store.setDraft({ period: "monthly", dayOfMonth: 15, lookbackDays: 31 });
    await store.saveDraft();
    expect(api.createReportSchedule).toHaveBeenCalledWith(
      expect.objectContaining({
        period: "monthly",
        day_of_month: 15,
        weekday: null,
        lookback_days: 31,
        tz: "Asia/Shanghai",
      }),
    );
    expect(store.notice).toContain("已建");
    expect(store.draft.period).toBe("weekly");
    expect(store.saving).toBe(false);
  });

  it("saveDraft：编辑态 ⇒ 走 PATCH 并带上理由，改完退出编辑态", async () => {
    const store = useReportsStore();
    await store.refresh();
    store.openEdit(schedule({ period: "daily", weekday: null, day_of_month: null, lookback_days: 1 }));
    expect(store.editing).toBe(true);
    expect(store.draft.period).toBe("daily");
    api.patchReportSchedule.mockResolvedValue(schedule({ period: "daily" }));
    await store.saveDraft();
    expect(api.patchReportSchedule).toHaveBeenCalledWith(
      "rsched_weekly",
      expect.objectContaining({ period: "daily", weekday: null, reason: "面板编辑报告周期" }),
    );
    expect(api.createReportSchedule).not.toHaveBeenCalled();
    expect(store.editing).toBe(false);
    expect(store.notice).toContain("已改");
  });

  it("saveDraft：失败 ⇒ 编辑态**保留**（清了就等于让用户重填一遍）", async () => {
    const store = useReportsStore();
    store.openEdit(schedule());
    api.patchReportSchedule.mockRejectedValue(new ApiError("weekly 必须给 weekday", 400, null));
    await store.saveDraft();
    expect(store.error).toBe("weekly 必须给 weekday（HTTP 400）");
    expect(store.editing).toBe(true);
    expect(store.saving).toBe(false);
  });

  it("cancelEdit：退出编辑态并把表单复位", async () => {
    const store = useReportsStore();
    store.openEdit(schedule({ period: "monthly", day_of_month: 15 }));
    store.cancelEdit();
    expect(store.editing).toBe(false);
    expect(store.draft.period).toBe("weekly");
  });

  it("setPeriod：回看天数跟着周期走（weekly 改成 daily 还带着 7 天是错的）", () => {
    const store = useReportsStore();
    store.setDraft({ period: "weekly", lookbackDays: 7 });
    store.setPeriod("daily");
    expect(store.draft.period).toBe("daily");
    expect(store.draft.lookbackDays).toBe(1);
    store.setPeriod("monthly");
    expect(store.draft.lookbackDays).toBe(31);
  });

  it("toggleInclude：勾上 / 取消一个区块（include 是数组，不能整个覆盖）", () => {
    const store = useReportsStore();
    store.setDraft({ include: ["publish"] });
    store.toggleInclude("cost");
    expect(store.draft.include).toEqual(["publish", "cost"]);
    store.toggleInclude("publish");
    expect(store.draft.include).toEqual(["cost"]);
  });

  it("默认是周报（与出厂自带的那条周报周期对齐）", () => {
    const store = useReportsStore();
    expect(store.period).toBe("weekly");
  });
});