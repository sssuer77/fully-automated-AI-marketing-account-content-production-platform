import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PublishPlatformOption, PublishPlatformsView } from "@/api/endpoints/publish";
import type {
  Schedule,
  ScheduleDeleteResult,
  ScheduleList,
  ScheduleRunResult,
} from "@/api/endpoints/schedules";
import { ApiError } from "@/api/http";
import {
  MAX_JITTER_MIN,
  SCHEDULE_MODES,
  SCHEDULE_POLL_MS,
  configureSchedulesApi,
  draftToBody,
  emptyDraft,
  localToIso,
  modeLabel,
  nextRunText,
  resultLabel,
  resultTone,
  runText,
  scheduleSummary,
  stampText,
  useSchedulesStore,
} from "./schedules";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

const TASK = "01M2SC0XKKXYKJ5C19N33Z849N";

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "sched-1",
    task_id: null,
    platforms: ["douyin"],
    account_ids: ["acc_main"],
    mode: "daily_window",
    at_time: null,
    window: ["18:00", "21:30"],
    interval_hours: null,
    jitter_min: 15,
    enabled: true,
    next_run_at: "2026-09-18T10:30:00.000Z",
    last_run_at: null,
    run_count: 0,
    last_result: null,
    fail_streak: 0,
    created_by: "panel",
    created_at: "2026-09-18T02:00:00.000Z",
    updated_at: "2026-09-18T02:00:00.000Z",
    ...overrides,
  };
}

function board(overrides: Partial<ScheduleList> = {}): ScheduleList {
  return {
    items: [schedule()],
    counts: { total: 1, enabled: 1, disabled: 0, failing: 0 },
    enabled_platforms: ["douyin"],
    tick_sec: 30,
    ...overrides,
  };
}

function option(overrides: Partial<PublishPlatformOption> = {}): PublishPlatformOption {
  return {
    code: "douyin",
    enabled: true,
    publisher: "browser",
    rehearsal: false,
    selectable: true,
    accounts: ["acc_main"],
    note: "",
    ...overrides,
  };
}

function platforms(overrides: Partial<PublishPlatformsView> = {}): PublishPlatformsView {
  return {
    items: [option()],
    default_platforms: ["douyin"],
    dry_run: false,
    publish_enabled: false,
    ...overrides,
  };
}

function fire(overrides: Partial<ScheduleRunResult> = {}): ScheduleRunResult {
  return {
    schedule_id: "sched-1",
    task_id: TASK,
    result: "ok",
    queued: 1,
    skipped: [],
    job_ids: ["job-1"],
    next_run_at: "2026-09-19T10:30:00.000Z",
    note: null,
    ...overrides,
  };
}

function removed(overrides: Partial<ScheduleDeleteResult> = {}): ScheduleDeleteResult {
  return { schedule_id: "sched-1", deleted: true, message: "已删除这条计划", ...overrides };
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

beforeEach(() => {
  setActivePinia(createPinia());
  configureSchedulesApi({
    fetchSchedules: vi.fn(async () => board()),
    fetchPlatforms: vi.fn(async () => platforms()),
    createSchedule: vi.fn(async () => schedule()),
    patchSchedule: vi.fn(async () => schedule()),
    deleteSchedule: vi.fn(async () => removed()),
    runSchedule: vi.fn(async () => fire()),
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("模式词：三种都翻成中文，认不出的照原样（别把未知吞掉）", () => {
    expect(modeLabel("daily_window")).toBe("每天窗口内随机");
    expect(modeLabel("at_time")).toBe("指定时刻（一次性）");
    expect(modeLabel("interval")).toBe("固定间隔");
    expect(modeLabel("every_full_moon")).toBe("every_full_moon");
  });

  it("下拉框里的三种模式：默认那个排第一（排错了会让人以为只有一种）", () => {
    expect(SCHEDULE_MODES).toEqual(["daily_window", "at_time", "interval"]);
  });

  it("上一次的结论：四种结果 + 没跑过 + 认不出的照原样", () => {
    expect(resultLabel(null)).toBe("还没跑过");
    expect(resultLabel("")).toBe("还没跑过");
    expect(resultLabel("ok")).toBe("成功");
    expect(resultLabel("skipped_ratelimit")).toBe("被限频，已顺延");
    expect(resultLabel("skipped_disabled")).toBe("发布开关关着，空转");
    expect(resultLabel("skipped_duplicate")).toBe("早就投过，没重复发");
    expect(resultLabel("error:上传超时")).toBe("失败：上传超时");
    expect(resultLabel("whatever")).toBe("whatever");
  });

  it("灯色：成功绿、失败红、让路黄、没跑过灰", () => {
    expect(resultTone(null)).toBe("idle");
    expect(resultTone("ok")).toBe("ok");
    expect(resultTone("error:boom")).toBe("error");
    expect(resultTone("skipped_ratelimit")).toBe("warn");
  });

  it("时刻：UTC 换算成本地 MM-DD HH:MM（截字符串会把 18:30 显示成 10:30）", () => {
    expect(stampText("2026-09-18T10:30:00.000Z")).toBe(localStamp("2026-09-18T10:30:00.000Z"));
    expect(stampText("2026-09-18T10:30:00.000Z")).toMatch(/^\d{2}-\d{2} \d{2}:\d{2}$/);
  });

  it("时刻：没有 / 空的 / 认不出的给 -（不显示半截时间，也不显示 Invalid Date）", () => {
    expect(stampText(null)).toBe("-");
    expect(stampText(undefined)).toBe("-");
    expect(stampText("")).toBe("-");
    expect(stampText("不是时间")).toBe("-");
  });

  it("参数摘要：只显示这个模式真正生效的那几项", () => {
    expect(scheduleSummary(schedule())).toBe("每天 18:00 - 21:30，抖动 ±15 分钟");
    const at = "2026-09-18T10:30:00.000Z";
    expect(scheduleSummary(schedule({ mode: "at_time", at_time: at }))).toBe(
      `指定时刻 ${localStamp(at)}`,
    );
    expect(scheduleSummary(schedule({ mode: "interval", interval_hours: 6 }))).toBe("每 6 小时");
    expect(scheduleSummary(schedule({ mode: "weird" }))).toBe("weird");
  });

  it("窗口字段缺失时摘要不崩（显示 ?，而不是 undefined）", () => {
    expect(scheduleSummary(schedule({ window: null }))).toBe("每天 ? - ?，抖动 ±15 分钟");
  });

  it("下一次触发：停用 / 一次性跑完 ⇒ 说清楚，而不是显示一个 -", () => {
    expect(nextRunText(schedule({ enabled: false }))).toBe("已停用");
    expect(nextRunText(schedule({ next_run_at: null }))).toBe("不会再触发");
    expect(nextRunText(schedule())).toBe(localStamp("2026-09-18T10:30:00.000Z"));
  });

  it("时刻转换：空 / 非法的原样给空串（交给后端报 400，前端不自己编一个时间）", () => {
    expect(localToIso("")).toBe("");
    expect(localToIso("不是时间")).toBe("");
  });

  it("时刻转换：带**浏览器真实偏移**，且与原值指同一瞬间（写死 +08:00 会在别的机器上差几小时）", () => {
    const iso = localToIso("2026-09-18T18:30");
    expect(iso).toMatch(/^2026-09-18T18:30:00[+-]\d{2}:\d{2}$/);
    expect(new Date(iso).getTime()).toBe(new Date("2026-09-18T18:30").getTime());
  });

  it("运行结论 → 一行人话：四种都覆盖，认不出的照原样", () => {
    expect(runText(fire({ queued: 2 }))).toBe("已建 2 个发布作业，去「待发布」看它");
    expect(runText(fire({ result: "skipped_ratelimit", queued: 0 }))).toBe(
      "被限频挡住了：额度恢复前不会再试（这不是失败）",
    );
    expect(runText(fire({ result: "skipped_disabled", queued: 0 }))).toBe(
      "发布总开关是关的 ⇒ 空转，没建作业",
    );
    expect(runText(fire({ result: "skipped_duplicate", queued: 0 }))).toBe(
      "这条任务早就投过 ⇒ 没重复发（也不用改什么）",
    );
    expect(runText(fire({ result: "error:没找到任务", queued: 0 }))).toBe("没找到任务");
    expect(runText(fire({ result: "surprise", queued: 0 }))).toBe("surprise");
  });

  it("表单初值：Q14 的默认策略（18:00-21:30 + 15 分钟抖动 + 启用）", () => {
    const draft = emptyDraft("douyin");
    expect(draft).toMatchObject({
      platform: "douyin",
      mode: "daily_window",
      windowStart: "18:00",
      windowEnd: "21:30",
      jitterMin: 15,
      enabled: true,
      taskId: "",
    });
    expect(MAX_JITTER_MIN).toBe(120);
  });

  it("表单 → 请求体：只带这个模式用得上的字段（带了别的会被后端当成矛盾参数）", () => {
    const window = draftToBody(emptyDraft("douyin"), "acc_main");
    expect(window).toMatchObject({
      platforms: ["douyin"],
      account_ids: ["acc_main"],
      mode: "daily_window",
      window: ["18:00", "21:30"],
      jitter_min: 15,
      enabled: true,
      task_id: null,
    });
    expect(window).not.toHaveProperty("at_time");
    expect(window).not.toHaveProperty("interval_hours");

    const at = draftToBody({ ...emptyDraft("douyin"), mode: "at_time", atTime: "2026-09-18T18:30" }, null);
    expect(at).not.toHaveProperty("window");
    expect(at).not.toHaveProperty("interval_hours");
    expect(String(at.at_time)).toMatch(/^2026-09-18T18:30:00[+-]\d{2}:\d{2}$/);

    const every = draftToBody({ ...emptyDraft("douyin"), mode: "interval", intervalHours: 6 }, "acc_main");
    expect(every.interval_hours).toBe(6);
    expect(every).not.toHaveProperty("window");
    expect(every).not.toHaveProperty("at_time");
  });

  it("表单 → 请求体：没选平台 / 没选账号给空数组，任务号留空给 null（不是空串）", () => {
    const body = draftToBody(emptyDraft(""), null);
    expect(body.platforms).toEqual([]);
    expect(body.account_ids).toEqual([]);
    expect(body.task_id).toBeNull();
    expect(draftToBody({ ...emptyDraft("douyin"), taskId: "  T1  " }, "acc_main").task_id).toBe("T1");
  });

  it("轮询周期是慢轮询：这一块问的是时间，不是某个进程在忙", () => {
    expect(SCHEDULE_POLL_MS).toBe(15_000);
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("store：读取", () => {
  it("refresh 一次拉两样：计划清单 + 平台选项（平台清单来自配置，不是前端列的）", async () => {
    const fetchSchedules = vi.fn(async () => board({ counts: { total: 2, enabled: 1, disabled: 1, failing: 1 } }));
    const fetchPlatforms = vi.fn(async () => platforms());
    configureSchedulesApi({ fetchSchedules, fetchPlatforms });

    const store = useSchedulesStore();
    await store.refresh();

    expect(fetchSchedules).toHaveBeenCalledTimes(1);
    expect(fetchPlatforms).toHaveBeenCalledTimes(1);
    expect(store.items).toHaveLength(1);
    expect(store.counts.failing).toBe(1);
    expect(store.tickSec).toBe(30);
    expect(store.loading).toBe(false);
    expect(store.error).toBe("");
  });

  it("没选平台时自动选中第一个**能建的**平台（选不到的那种点了会空转）", async () => {
    configureSchedulesApi({
      fetchPlatforms: vi.fn(async () =>
        platforms({
          items: [
            option({ code: "kuaishou", enabled: false, selectable: false, note: "平台没启用" }),
            option({ code: "douyin", accounts: ["acc_main"] }),
          ],
        }),
      ),
    });

    const store = useSchedulesStore();
    await store.refresh();

    expect(store.selectable.map((item) => item.code)).toEqual(["douyin"]);
    expect(store.draft.platform).toBe("douyin");
    expect(store.draftAccounts).toEqual(["acc_main"]);
    expect(store.draftAccountId).toBe("acc_main");
  });

  it("已经选过平台就不动它（15s 一拍会把用户选的那一项改掉的话，这屏就没法用了）", async () => {
    configureSchedulesApi({
      fetchPlatforms: vi.fn(async () =>
        platforms({ items: [option({ code: "kuaishou", accounts: ["acc_k"] }), option({ code: "douyin" })] }),
      ),
    });

    const store = useSchedulesStore();
    store.setDraft({ platform: "kuaishou" });
    await store.refresh();

    expect(store.draft.platform).toBe("kuaishou");
    expect(store.draftAccountId).toBe("acc_k");
  });

  it("读失败：把后端那句话原样摆上屏，loading 照样复位（否则按钮永远转圈）", async () => {
    configureSchedulesApi({
      fetchSchedules: vi.fn(async () => {
        throw new ApiError("库读不出来", 500, null);
      }),
    });

    const store = useSchedulesStore();
    await store.refresh();

    expect(store.error).toBe("库读不出来（HTTP 500）");
    expect(store.loading).toBe(false);
  });

  it("没选中平台时账号是空的（不是 undefined —— 模板里要能直接判空）", async () => {
    configureSchedulesApi({
      fetchPlatforms: vi.fn(async () => platforms({ items: [option({ code: "douyin", accounts: [] })] })),
    });

    const store = useSchedulesStore();
    await store.refresh();

    expect(store.draftAccounts).toEqual([]);
    expect(store.draftAccountId).toBeNull();
  });
});

describe("store：轮询", () => {
  it("start 立刻拉一次，之后每 15s 一拍（这一块轮询的是时间，空闲也要问）", async () => {
    vi.useFakeTimers();
    const fetchSchedules = vi.fn(async () => board());
    configureSchedulesApi({ fetchSchedules });

    const store = useSchedulesStore();
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchSchedules).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(SCHEDULE_POLL_MS);
    expect(fetchSchedules).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(SCHEDULE_POLL_MS * 2);
    expect(fetchSchedules).toHaveBeenCalledTimes(4);
  });

  it("start 点两次只有一个定时器（切页面回来又 start 一次是常态）", async () => {
    vi.useFakeTimers();
    const fetchSchedules = vi.fn(async () => board());
    configureSchedulesApi({ fetchSchedules });

    const store = useSchedulesStore();
    store.start();
    store.start();
    await vi.advanceTimersByTimeAsync(SCHEDULE_POLL_MS);
    expect(fetchSchedules).toHaveBeenCalledTimes(3);
  });

  it("stop 之后不再问（离开这一屏就该闭嘴）", async () => {
    vi.useFakeTimers();
    const fetchSchedules = vi.fn(async () => board());
    configureSchedulesApi({ fetchSchedules });

    const store = useSchedulesStore();
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    store.stop();
    await vi.advanceTimersByTimeAsync(SCHEDULE_POLL_MS * 3);

    expect(fetchSchedules).toHaveBeenCalledTimes(1);
  });
});

describe("store：新建 / 启停 / 立刻跑 / 删除", () => {
  it("新建：请求体是表单转出来的那一份，成功后报「已建 + 下一次」并重拉", async () => {
    const createSchedule = vi.fn(async () => schedule({ next_run_at: "2026-09-18T10:30:00.000Z" }));
    const fetchSchedules = vi.fn(async () => board());
    configureSchedulesApi({ createSchedule, fetchSchedules });

    const store = useSchedulesStore();
    await store.refresh();
    store.setDraft({ platform: "douyin", taskId: TASK });
    fetchSchedules.mockClear();
    await store.create();

    expect(createSchedule).toHaveBeenCalledTimes(1);
    const [body] = createSchedule.mock.calls[0] as unknown as [Record<string, unknown>];
    expect(body).toMatchObject({ platforms: ["douyin"], mode: "daily_window", task_id: TASK });
    expect(store.notice).toContain("已建");
    expect(store.notice).toContain(localStamp("2026-09-18T10:30:00.000Z"));
    expect(fetchSchedules).toHaveBeenCalledTimes(1);
    expect(store.saving).toBe(false);
    expect(store.error).toBe("");
  });

  it("新建失败：后端那句话上屏，saving 复位", async () => {
    configureSchedulesApi({
      createSchedule: vi.fn(async () => {
        throw new ApiError("窗口结束必须晚于开始", 400, null);
      }),
    });

    const store = useSchedulesStore();
    await store.refresh();
    await store.create();

    expect(store.error).toBe("窗口结束必须晚于开始（HTTP 400）");
    expect(store.saving).toBe(false);
  });

  it("启停：**只**提交 enabled 这一项（整份表单提交会把面板那一刻的旧值写回去）", async () => {
    const patchSchedule = vi.fn(async () => schedule({ enabled: false, next_run_at: null }));
    configureSchedulesApi({ patchSchedule });

    const store = useSchedulesStore();
    await store.refresh();
    await store.toggle(schedule({ enabled: true }));

    expect(patchSchedule).toHaveBeenCalledWith("sched-1", { enabled: false });
    expect(store.notice).toContain("已停用");
    expect(store.busyId).toBeNull();
  });

  it("启用回来：提示里带上算好的下一次", async () => {
    const patchSchedule = vi.fn(async () => schedule({ enabled: true }));
    configureSchedulesApi({ patchSchedule });

    const store = useSchedulesStore();
    await store.refresh();
    await store.toggle(schedule({ enabled: false }));

    expect(patchSchedule).toHaveBeenCalledWith("sched-1", { enabled: true });
    expect(store.notice).toContain(localStamp("2026-09-18T10:30:00.000Z"));
  });

  it("立刻跑一次：走的是同一个端点（仍看总开关、仍过限频），提示照实说", async () => {
    const runSchedule = vi.fn(async () => fire({ result: "skipped_ratelimit", queued: 0, job_ids: [] }));
    configureSchedulesApi({ runSchedule });

    const store = useSchedulesStore();
    await store.refresh();
    await store.run(schedule());

    expect(runSchedule).toHaveBeenCalledWith("sched-1", "面板手动执行");
    expect(store.notice).toContain("被限频挡住了");
    expect(store.error).toBe("");
    expect(store.busyId).toBeNull();
  });

  it("删除：`deleted=false` 也是正常返回，把那句话原样显示（它不是错误）", async () => {
    configureSchedulesApi({
      deleteSchedule: vi.fn(async () => removed({ deleted: false, message: "这条计划本来就不在" })),
    });

    const store = useSchedulesStore();
    await store.refresh();
    await store.remove(schedule());

    expect(store.notice).toBe("这条计划本来就不在");
    expect(store.error).toBe("");
  });

  it("删除失败：红字上屏", async () => {
    configureSchedulesApi({
      deleteSchedule: vi.fn(async () => {
        throw new ApiError("库里写不进去", 500, null);
      }),
    });

    const store = useSchedulesStore();
    await store.refresh();
    await store.remove(schedule());

    expect(store.error).toBe("库里写不进去（HTTP 500）");
  });
});
