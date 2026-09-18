import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ComplianceView,
  HandoffPreview,
  HandoffResult,
  MemorySinkResult,
  MetricsTickResult,
  Publication,
  PublicationList,
} from "@/api/endpoints/publish";
import { ApiError } from "@/api/http";
import {
  PUBLISH_POLL_MS,
  actionBody,
  activeWork,
  configurePublishApi,
  formatStamp,
  manualCount,
  metricsRows,
  metricsText,
  missingText,
  plainText,
  reasonProblem,
  rowsOf,
  sinkText,
  statusLabel,
  statusTone,
  tickText,
  usePublishStore,
} from "./publish";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

const TASK = "01M2M9THZ1M83CT5EJH789ZVZA";

function publication(overrides: Partial<Publication> = {}): Publication {
  return {
    id: "pub-1",
    task_id: TASK,
    platform: "douyin",
    account_id: "acc_main",
    status: "queued",
    title: "离谱跑酷地图",
    caption: "",
    tags: [],
    video_path: "D:/repo/data/output/videos/20260917-120000_x_final.mp4",
    cover_path: null,
    profile_key: null,
    dry_run: false,
    url: null,
    platform_post_id: null,
    published_at: null,
    scheduled_at: null,
    next_metric_at: null,
    attempt_count: 0,
    max_attempts: 3,
    error_code: null,
    error_message: null,
    evidence: {},
    metrics: {},
    metrics_history: [],
    metric_attempts: 0,
    created_at: "2026-09-17T12:00:00.000Z",
    updated_at: "2026-09-17T12:00:00.000Z",
    finished_at: null,
    can_retry: true,
    can_cancel: true,
    can_mark_done: false,
    ...overrides,
  };
}

const ZERO_COUNTS = {
  queued: 0,
  uploading: 0,
  published: 0,
  failed: 0,
  manual_required: 0,
  canceled: 0,
};

function board(overrides: Partial<PublicationList> = {}): PublicationList {
  return { counts: { ...ZERO_COUNTS }, by_status: {}, manual_required: [], hint: null, ...overrides };
}

function compliance(overrides: Partial<ComplianceView> = {}): ComplianceView {
  return { notice: "R2 合规提示：**声音权**与著作权。是否可商用请自行确认", ok: true, items: [], gaps: [], ...overrides };
}

function preview(overrides: Partial<HandoffPreview> = {}): HandoffPreview {
  return {
    task_id: TASK,
    ready: true,
    items: [
      { kind: "video", label: "成片", required: true, present: true, source: "D:/x.mp4", bytes: 10, note: null },
      { kind: "cover", label: "封面", required: false, present: false, source: null, bytes: null, note: "还没有封面" },
    ],
    missing: ["cover"],
    adapter: "local",
    output_dir: "D:/repo/data/handoff",
    auto_on_publish: false,
    note: null,
    ...overrides,
  };
}

function handoffResult(overrides: Partial<HandoffResult> = {}): HandoffResult {
  return {
    task_id: TASK,
    adapter: "local",
    root: "D:/repo/data/handoff/T1/20260917-120000.100",
    manifest: "D:/repo/data/handoff/T1/20260917-120000.100/handoff.json",
    copied: [],
    missing: ["cover"],
    bytes: 1024,
    ...overrides,
  };
}

function tickReport(overrides: Partial<MetricsTickResult> = {}): MetricsTickResult {
  return {
    collected: [],
    deferred: [],
    stopped: [],
    yielded: false,
    schedule_hours: [1, 6, 24, 72],
    pending: 0,
    ...overrides,
  };
}

function sinkResult(overrides: Partial<MemorySinkResult> = {}): MemorySinkResult {
  return {
    feedback_items_created: 1,
    topics_demoted: 0,
    digest_path: "D:/repo/data/feedback/auto_202609.md",
    planner_consumable: true,
    comments_seen: 0,
    low_engagement: false,
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configurePublishApi({
    fetchPublications: vi.fn(async () => board()),
    fetchManualQueue: vi.fn(async () => board()),
    fetchCompliance: vi.fn(async () => compliance()),
    retryPublication: vi.fn(async () => ({ publication: publication(), action: "retry", message: "已重新排队", job_changed: true })),
    cancelPublication: vi.fn(async () => ({ publication: publication({ status: "canceled" }), action: "cancel", message: "已取消", job_changed: true })),
    markManualDone: vi.fn(async () => ({ publication: publication({ status: "canceled" }), action: "manual_done", message: "已标记", job_changed: false })),
    fetchHandoff: vi.fn(async () => preview()),
    pushHandoff: vi.fn(async () => handoffResult()),
    runMetricsTick: vi.fn(async () => tickReport()),
    collectMetrics: vi.fn(async () => publication({ status: "published", metrics: { views: 1 } })),
    sinkMemory: vi.fn(async () => sinkResult()),
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("状态词：六态都翻成中文，认不出的照原样（别把未知吞掉）", () => {
    expect(statusLabel("queued")).toBe("待发布");
    expect(statusLabel("uploading")).toBe("发布中");
    expect(statusLabel("published")).toBe("已发布");
    expect(statusLabel("manual_required")).toBe("待人工");
    expect(statusLabel("brand_new")).toBe("brand_new");
  });

  it("灯色：发出去了是绿的，失败是红的，等人是黄的，正在传是蓝的", () => {
    expect(statusTone("published")).toBe("ok");
    expect(statusTone("failed")).toBe("error");
    expect(statusTone("manual_required")).toBe("warn");
    expect(statusTone("uploading")).toBe("busy");
    expect(statusTone("queued")).toBe("idle");
  });

  it("时间：截到 MM-DD HH:MM；没有 / 太短给 -（不显示半截时间）", () => {
    expect(formatStamp("2026-09-17T12:34:56.000Z")).toBe("09-17 12:34");
    expect(formatStamp(null)).toBe("-");
    expect(formatStamp(undefined)).toBe("-");
    expect(formatStamp("2026-09-17")).toBe("-");
  });

  it("取某个状态的若干条：快照没拿到 ⇒ 空数组（不抛）", () => {
    expect(rowsOf(null, "queued")).toEqual([]);
    expect(rowsOf(board({ by_status: { queued: [publication()] } }), "queued")).toHaveLength(1);
    expect(rowsOf(board(), "queued")).toEqual([]);
  });

  it("有活在跑 = 排队中 + 上传中（**待人工不算**：它正等人，人不动它就不会变）", () => {
    expect(activeWork(null)).toBe(false);
    expect(activeWork(ZERO_COUNTS)).toBe(false);
    expect(activeWork({ ...ZERO_COUNTS, queued: 1 })).toBe(true);
    expect(activeWork({ ...ZERO_COUNTS, uploading: 2 })).toBe(true);
    expect(activeWork({ ...ZERO_COUNTS, manual_required: 3 })).toBe(false);
  });

  it("待人工计数：拿队列那份，不是看板那份", () => {
    expect(manualCount(null)).toBe(0);
    expect(manualCount(board({ counts: { ...ZERO_COUNTS, manual_required: 2 } }))).toBe(2);
  });

  it("回流：只挑有时刻表或有数字的那几条", () => {
    const rows = board({
      by_status: {
        published: [
          publication({ id: "a", status: "published" }),
          publication({ id: "b", status: "published", next_metric_at: "2026-09-17T13:00:00.000Z" }),
          publication({ id: "c", status: "published", metrics: { views: 12 } }),
        ],
      },
    });
    expect(metricsRows(rows).map((row) => row.id)).toEqual(["b", "c"]);
  });

  it("回流的数字：认得出的翻成中文；一个都没有时说清「下一次什么时候」", () => {
    expect(metricsText(publication({ metrics: { views: 120, likes: 8 } }))).toBe("播放 120 · 赞 8");
    expect(metricsText(publication({ next_metric_at: "2026-09-17T13:00:00.000Z" }))).toBe(
      "下一次回收：09-17 13:00",
    );
    expect(metricsText(publication())).toBe("还没有回流时刻表");
  });

  it("缺件那句话：齐了说齐，缺了点名（必需件与可选件语气一样 —— 缺就是缺）", () => {
    expect(missingText(null)).toBe("");
    expect(missingText(preview({ missing: [] }))).toBe("六件齐");
    expect(missingText(preview({ missing: ["cover", "subtitle"] }))).toBe("缺 cover / subtitle");
  });

  it("标记已处理的理由：空 / 全空白 ⇒ 拦；写了就放行", () => {
    expect(reasonProblem("")).toContain("必须写清理由");
    expect(reasonProblem("   ")).toContain("必须写清理由");
    expect(reasonProblem("已在平台上手工发布")).toBeNull();
  });

  it("请求体：actor 固定写 user；理由为空时**不发**那个字段", () => {
    expect(actionBody(null)).toEqual({ actor: "user" });
    expect(actionBody("  ")).toEqual({ actor: "user" });
    expect(actionBody(" 人工重试 ")).toEqual({ actor: "user", reason: "人工重试" });
  });

  it("常驻提示去掉 markdown 的星号（这一屏没有渲染器），一个字都不改", () => {
    expect(plainText("**声音权**与**著作权**")).toBe("声音权与著作权");
    expect(plainText("没有标记")).toBe("没有标记");
  });

  it("一轮回收的结论：让路说成让路（不是失败），没跑过不占屏", () => {
    expect(tickText(null)).toBe("");
    expect(tickText(tickReport({ yielded: true }))).toContain("让路");
    expect(tickText(tickReport({ collected: ["a", "b"], pending: 3 }))).toBe(
      "采到 2 条 · 顺延 0 条 · 停止 0 条 · 还有 3 条到点未采",
    );
  });

  it("沉淀的结论：数字 + 汇总文件名；评论 0 条时**明说没接线**", () => {
    const text = sinkText(sinkResult({ comments_seen: 0 }));
    expect(text).toContain("新增反馈 1 条");
    expect(text).toContain("auto_202609.md");
    expect(text).toContain("下一轮选题可直接吃");
    expect(text).toContain("评论采样还没接线");
    expect(sinkText(sinkResult({ comments_seen: 5 }))).not.toContain("评论采样还没接线");
  });
});

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

describe("usePublishStore", () => {
  it("首屏拉齐三份：看板 + 待人工 + 合规", async () => {
    const store = usePublishStore();
    store.start();
    await vi.waitFor(() => expect(store.compliance).not.toBeNull());

    expect(store.board).not.toBeNull();
    expect(store.queue).not.toBeNull();
    expect(store.compliance?.notice).toContain("声音权");
    store.stop();
  });

  it("有活在跑才轮询；停掉之后一次都不再问", async () => {
    vi.useFakeTimers();
    const fetchPublications = vi.fn(async () => board({ counts: { ...ZERO_COUNTS, queued: 1 } }));
    configurePublishApi({ fetchPublications });

    const store = usePublishStore();
    store.start();
    await vi.advanceTimersByTimeAsync(0);
    const afterFirst = fetchPublications.mock.calls.length;

    await vi.advanceTimersByTimeAsync(PUBLISH_POLL_MS * 3);
    expect(fetchPublications.mock.calls.length).toBeGreaterThan(afterFirst);

    store.stop();
    const afterStop = fetchPublications.mock.calls.length;
    await vi.advanceTimersByTimeAsync(PUBLISH_POLL_MS * 3);
    expect(fetchPublications.mock.calls.length).toBe(afterStop);
  });

  it("空闲（没有排队中 / 上传中）⇒ 只拉首屏那一遍，不进轮询", async () => {
    vi.useFakeTimers();
    const fetchPublications = vi.fn(async () => board());

    configurePublishApi({ fetchPublications });
    const store = usePublishStore();
    store.start();
    await vi.advanceTimersByTimeAsync(0);

    await vi.advanceTimersByTimeAsync(PUBLISH_POLL_MS * 3);
    expect(fetchPublications.mock.calls.length).toBe(1);
    store.stop();
  });

  it("读不到就留旧快照 + 一句人话（不把屏刷成空白）", async () => {
    const store = usePublishStore();
    configurePublishApi({
      fetchPublications: vi.fn(async () => {
        throw new ApiError("看板炸了", 500, null);
      }),
    });
    await store.refresh();
    expect(store.loadError).toContain("看板炸了");
  });

  it("重试：报服务端那句话，并重拉看板与队列", async () => {
    const store = usePublishStore();
    await store.refresh();

    const ok = await store.retry(publication({ status: "manual_required", can_mark_done: true }));

    expect(ok).toBe(true);
    expect(store.notice).toBe("已重新排队");
  });

  it("标记已处理：没写理由 ⇒ 不发请求，并给出那句话", async () => {
    const markManualDone = vi.fn(async () => ({
      publication: publication(),
      action: "manual_done",
      message: "已标记",
      job_changed: false,
    }));
    configurePublishApi({ markManualDone });
    const store = usePublishStore();

    const ok = await store.markDone(publication({ status: "manual_required" }));

    expect(ok).toBe(false);
    expect(markManualDone).not.toHaveBeenCalled();
    expect(store.error).toContain("必须写清理由");
  });

  it("标记已处理：写了理由 ⇒ 带上它发出去，并把草稿清掉", async () => {
    const markManualDone = vi.fn(async () => ({
      publication: publication(),
      action: "manual_done",
      message: "已标记",
      job_changed: false,
    }));
    configurePublishApi({ markManualDone });
    const store = usePublishStore();
    const row = publication({ status: "manual_required" });
    store.openReason(row.id);
    store.setReason(row.id, "已在平台上手工发布");

    const ok = await store.markDone(row);

    expect(ok).toBe(true);
    expect(markManualDone).toHaveBeenCalledWith(row.id, { actor: "user", reason: "已在平台上手工发布" });
    expect(store.reasonFor).toBeNull();
    expect(store.reasonDraft[row.id]).toBeUndefined();
  });

  it("交付包：没预览过就导出 ⇒ 拦住（先看一眼包里缺什么）", async () => {
    const pushHandoff = vi.fn(async () => handoffResult());
    configurePublishApi({ pushHandoff });
    const store = usePublishStore();
    store.setHandoffTaskId(TASK);

    const ok = await store.exportHandoff();

    expect(ok).toBe(false);
    expect(pushHandoff).not.toHaveBeenCalled();
    expect(store.handoffError).toContain("先点「预览」");
  });

  it("交付包：预览 → 导出，两步都记结果", async () => {
    const store = usePublishStore();
    store.setHandoffTaskId(`  ${TASK}  `);

    expect(await store.previewHandoff()).toBe(true);
    expect(store.handoffPreview?.adapter).toBe("local");

    expect(await store.exportHandoff()).toBe(true);
    expect(store.handoffResult?.root).toContain("handoff");
  });

  it("交付包：换任务号 ⇒ 上一份预览作废（它说的是另一个任务号的事）", async () => {
    const store = usePublishStore();
    store.setHandoffTaskId(TASK);
    await store.previewHandoff();
    await store.exportHandoff();

    store.setHandoffTaskId("another-task");

    expect(store.handoffPreview).toBeNull();
    expect(store.handoffResult).toBeNull();
    expect(store.handoffError).toBeNull();
  });

  it("交付包：任务号空着 ⇒ 预览不发请求", async () => {
    const fetchHandoff = vi.fn(async () => preview());
    configurePublishApi({ fetchHandoff });
    const store = usePublishStore();
    store.setHandoffTaskId("   ");

    expect(store.canPreviewHandoff).toBe(false);
    expect(await store.previewHandoff()).toBe(false);
    expect(fetchHandoff).not.toHaveBeenCalled();
  });

  it("跑一轮：结论走 notice（**一条没采到不是错**），并重拉看板", async () => {
    const runMetricsTick = vi.fn(async () => tickReport({ collected: ["pub-1"], pending: 2 }));
    const fetchPublications = vi.fn(async () => board());
    configurePublishApi({ runMetricsTick, fetchPublications });
    const store = usePublishStore();

    expect(await store.tickMetrics()).toBe(true);

    expect(store.error).toBeNull();
    expect(store.notice).toContain("采到 1 条");
    expect(store.metricsTick?.pending).toBe(2);
    expect(fetchPublications).toHaveBeenCalled();
  });

  it("跑一轮：发布池正忙 ⇒ notice 说清是让路（不是采不到数）", async () => {
    configurePublishApi({ runMetricsTick: vi.fn(async () => tickReport({ yielded: true })) });
    const store = usePublishStore();

    await store.tickMetrics();

    expect(store.error).toBeNull();
    expect(store.notice).toContain("让路");
  });

  it("跑一轮：请求本身失败 ⇒ error（这一条是真的出事了）", async () => {
    configurePublishApi({
      runMetricsTick: vi.fn(async () => {
        throw new ApiError("回收接口炸了", 500, null);
      }),
    });
    const store = usePublishStore();

    expect(await store.tickMetrics()).toBe(false);
    expect(store.error).toContain("回收接口炸了");
  });

  it("采这一条：带上 id，结论里报这条的新数字", async () => {
    const collectMetrics = vi.fn(async () =>
      publication({ status: "published", title: "离谱跑酷", metrics: { views: 999 } }),
    );
    configurePublishApi({ collectMetrics });
    const store = usePublishStore();

    expect(await store.collectOne(publication({ status: "published" }))).toBe(true);

    expect(collectMetrics).toHaveBeenCalledWith("pub-1");
    expect(store.notice).toContain("播放 999");
  });

  it("沉淀这一条：带上 id，结论里报新增反馈与降权", async () => {
    const sinkMemory = vi.fn(async () => sinkResult({ feedback_items_created: 2, topics_demoted: 1 }));
    configurePublishApi({ sinkMemory });
    const store = usePublishStore();

    expect(await store.sinkOne(publication({ status: "published" }))).toBe(true);

    expect(sinkMemory).toHaveBeenCalledWith("pub-1");
    expect(store.notice).toContain("新增反馈 2 条");
    expect(store.notice).toContain("降权方向 1 个");
  });
});
