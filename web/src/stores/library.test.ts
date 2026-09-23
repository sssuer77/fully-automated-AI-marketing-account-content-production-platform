import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  LibraryBatchItem,
  LibraryItem,
  LibraryPublication,
  LibraryPublishResponse,
  LibraryResponse,
} from "@/api/endpoints/library";
import type { PublishPlatformsView } from "@/api/endpoints/publish";
import { ApiError } from "@/api/http";
import {
  BATCH_MAX,
  blockedReason,
  canPublish,
  configureLibraryApi,
  nextSelection,
  publicationLabel,
  publicationTone,
  publishedCount,
  publishedText,
  resultText,
  useLibraryStore,
} from "./library";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

const TASK = "01M2Z9BP1CR70TBW0CJ05FQ12Z";
const OTHER = "01M2Z9GCZ00H0Q31VF8EYD6ENS";

function publication(overrides: Partial<LibraryPublication> = {}): LibraryPublication {
  return {
    id: "pub-1",
    task_id: TASK,
    platform: "other",
    account_id: "_rehearsal",
    status: "published",
    title: "离谱跑酷地图",
    caption: "",
    tags: [],
    video_path: `D:/repo/data/output/videos/20260922-152331_${TASK}_final.mp4`,
    cover_path: null,
    profile_key: null,
    dry_run: true,
    url: "/rehearsal/rehearsal-1790063537248",
    platform_post_id: null,
    published_at: "2026-09-22T15:30:00.000Z",
    scheduled_at: null,
    next_metric_at: null,
    attempt_count: 1,
    max_attempts: 3,
    error_code: null,
    error_message: null,
    evidence: {},
    metrics: {},
    metrics_history: [],
    metric_attempts: 0,
    created_at: "2026-09-22T15:30:00.000Z",
    updated_at: "2026-09-22T15:30:04.000Z",
    finished_at: "2026-09-22T15:30:04.000Z",
    can_retry: false,
    can_cancel: false,
    can_mark_done: false,
    ...overrides,
  };
}

function item(overrides: Partial<LibraryItem> = {}): LibraryItem {
  return {
    task_id: TASK,
    video_name: `20260922-152331_${TASK}_final.mp4`,
    video_url: `/api/v1/render/videos/20260922-152331_${TASK}_final.mp4`,
    video_path: `D:/repo/data/output/videos/20260922-152331_${TASK}_final.mp4`,
    size_bytes: 190_723_456,
    modified_at: 1_790_063_531,
    versions: 3,
    task_found: true,
    title: "离谱跑酷地图",
    task_status: "completed",
    duration_ms: 205_760,
    publications: [],
    ...overrides,
  };
}

function snapshot(overrides: Partial<LibraryResponse> = {}): LibraryResponse {
  return { items: [item()], total: 1, limit: 200, hint: null, ...overrides };
}

function platforms(overrides: Partial<PublishPlatformsView> = {}): PublishPlatformsView {
  return {
    items: [
      {
        code: "douyin",
        publisher: "douyin",
        enabled: true,
        rehearsal: false,
        accounts: ["acc_main"],
        selectable: true,
        note: "真平台：要登录态，发出去不可撤销",
        calibration: "calibrated",
        calibration_note: "已真机校准（2026-09-23）",
      },
      {
        code: "other",
        publisher: "fixture",
        enabled: true,
        rehearsal: true,
        accounts: ["_rehearsal"],
        selectable: true,
        note: "本地演练台：发到本机靶页，不是真平台",
        calibration: "n/a",
        calibration_note: "",
      },
      {
        code: "kuaishou",
        publisher: "kuaishou",
        enabled: false,
        rehearsal: false,
        accounts: [],
        selectable: false,
        note: "没有启用账号",
        calibration: "uncalibrated",
        calibration_note: "",
      },
    ],
    default_platforms: ["douyin"],
    dry_run: false,
    publish_enabled: false,
    ...overrides,
  };
}

function batchItem(overrides: Partial<LibraryBatchItem> = {}): LibraryBatchItem {
  return { task_id: TASK, queued: 1, skipped: [], duplicates: [], missing: false, ...overrides };
}

function report(overrides: Partial<LibraryPublishResponse> = {}): LibraryPublishResponse {
  return { items: [batchItem()], platforms: ["douyin"], queued_total: 1, task_total: 1, ...overrides };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configureLibraryApi({
    fetchLibrary: vi.fn(async () => snapshot()),
    fetchPlatforms: vi.fn(async () => platforms()),
    publishSelected: vi.fn(async () => report()),
  });
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("状态词：六个都翻成中文，认不出的照原样（别把未知吞掉）", () => {
    expect(publicationLabel("queued")).toBe("排队中");
    expect(publicationLabel("uploading")).toBe("发布中");
    expect(publicationLabel("published")).toBe("已发布");
    expect(publicationLabel("failed")).toBe("失败");
    expect(publicationLabel("manual_required")).toBe("待人工");
    expect(publicationLabel("canceled")).toBe("已取消");
    expect(publicationLabel("weird")).toBe("weird");
  });

  it("状态灯：待人工是黄不是红（要人去处置，但那条记录没坏）", () => {
    expect(publicationTone("published")).toBe("ok");
    expect(publicationTone("failed")).toBe("error");
    expect(publicationTone("manual_required")).toBe("warn");
    expect(publicationTone("queued")).toBe("busy");
    expect(publicationTone("uploading")).toBe("busy");
    expect(publicationTone("canceled")).toBe("idle");
  });

  it("发到哪儿了：没记录说未发布，有记录按平台逐条列出来", () => {
    expect(publishedText(item())).toBe("未发布");

    const two = item({
      publications: [
        publication(),
        publication({ id: "pub-2", platform: "douyin", account_id: "acc_main", status: "queued" }),
      ],
    });
    expect(publishedText(two)).toBe("other/_rehearsal·已发布，douyin/acc_main·排队中");
  });

  it("发出去几个：只数已发布的（排队中的不算，否则筛选会说谎）", () => {
    expect(publishedCount(item())).toBe(0);

    const two = item({
      publications: [publication(), publication({ id: "pub-2", status: "queued" })],
    });
    expect(publishedCount(two)).toBe(1);
  });

  it("能不能投：任务还在就行；任务没了要说出为什么", () => {
    expect(canPublish(item())).toBe(true);
    expect(blockedReason(item())).toBeNull();

    const orphan = item({ task_found: false, title: "", task_status: null });
    expect(canPublish(orphan)).toBe(false);
    expect(blockedReason(orphan)).toContain("发不了");
  });

  it("一条批量结论：早投过的说「无需处理」，与其余跳过分开", () => {
    expect(resultText(batchItem())).toBe("已排入 1 条作业");
    expect(resultText(batchItem({ queued: 0, missing: true }))).toBe("任务不存在");
    expect(resultText(batchItem({ queued: 0, duplicates: ["other"] }))).toBe(
      "早就投过：other（无需处理）",
    );
    expect(resultText(batchItem({ queued: 0, skipped: ["douyin：平台未启用"] }))).toBe(
      "跳过：douyin：平台未启用",
    );
    expect(resultText(batchItem({ queued: 0 }))).toBe("没有变化");
  });

  it("一条批量结论：重复报同一件事时只说一遍（后端 skipped 里也带着它）", () => {
    const both = batchItem({
      queued: 0,
      duplicates: ["other"],
      skipped: ["other：早就投过"],
    });
    expect(resultText(both)).toBe("早就投过：other（无需处理）");
  });

  it("勾选：重复勾同一条不产生第二份，取消勾不碰别人", () => {
    expect(nextSelection([], TASK, true)).toEqual([TASK]);
    expect(nextSelection([TASK], TASK, true)).toEqual([TASK]);
    expect(nextSelection([TASK, OTHER], TASK, false)).toEqual([OTHER]);
    expect(nextSelection([TASK], OTHER, false)).toEqual([TASK]);
  });
});

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

describe("store", () => {
  it("load：一次拉列表 + 平台清单，默认勾上「出厂默认」里能选的那几个", async () => {
    const store = useLibraryStore();
    await store.load();

    expect(store.items.map((row) => row.task_id)).toEqual([TASK]);
    expect(store.platformOptions.map((option) => option.code)).toEqual(["douyin", "other"]);
    expect(store.chosenPlatforms).toEqual(["douyin"]);
    expect(store.error).toBeNull();
    expect(store.loading).toBe(false);
  });

  it("load：默认平台里不能选的那几个不会被勾上（勾了也是白勾）", async () => {
    configureLibraryApi({
      fetchPlatforms: vi.fn(async () => platforms({ default_platforms: ["douyin", "kuaishou"] })),
    });

    const store = useLibraryStore();
    await store.load();

    expect(store.chosenPlatforms).toEqual(["douyin"]);
  });

  it("load：库里已经没有了的那几条，勾选跟着掉（免得投了 3 条、报错 4 条）", async () => {
    const store = useLibraryStore();
    await store.load();
    store.toggle(TASK, true);
    expect(store.selected).toEqual([TASK]);

    configureLibraryApi({ fetchLibrary: vi.fn(async () => snapshot({ items: [], total: 0 })) });
    await store.load();

    expect(store.selected).toEqual([]);
  });

  it("load 失败：把话说出来，旧列表留着（一次抖动不把屏清空）", async () => {
    const store = useLibraryStore();
    await store.load();

    configureLibraryApi({
      fetchLibrary: vi.fn(async () => {
        throw new ApiError("成片库读取失败", 500, null);
      }),
    });
    await store.load();

    expect(store.error).toBe("成片库读取失败（HTTP 500）");
    expect(store.items).toHaveLength(1);
    expect(store.loading).toBe(false);
  });

  it("「只看未发布」只筛显示，不动已经勾上的那些", async () => {
    configureLibraryApi({
      fetchLibrary: vi.fn(async () =>
        snapshot({
          items: [item(), item({ task_id: OTHER, publications: [publication()] })],
          total: 2,
        }),
      ),
    });

    const store = useLibraryStore();
    await store.load();
    store.toggleAll(true);
    expect(store.selected).toEqual([TASK, OTHER]);

    store.onlyUnpublished = true;
    expect(store.visible.map((row) => row.task_id)).toEqual([TASK]);
    expect(store.selected).toEqual([TASK, OTHER]);
  });

  it("任务没了的那一条勾不上：它不进「全选」", async () => {
    configureLibraryApi({
      fetchLibrary: vi.fn(async () =>
        snapshot({ items: [item(), item({ task_id: OTHER, task_found: false })], total: 2 }),
      ),
    });

    const store = useLibraryStore();
    await store.load();

    expect(store.selectableIds).toEqual([TASK]);
    store.toggleAll(true);
    expect(store.selected).toEqual([TASK]);
  });

  it("提交前的那句话：没勾说没勾，没平台说平台，都齐了说投几条去哪", async () => {
    const store = useLibraryStore();
    await store.load();
    expect(store.submitHint).toBe("先在左边勾一条以上");

    store.toggle(TASK, true);
    expect(store.submitHint).toBe("将把 1 条任务投到 douyin");

    store.togglePlatform("douyin", false);
    expect(store.submitHint).toBe("选一个目标平台");
  });

  it("勾了真平台而发布开关是关的：必须说一句「投了会直接死信」", async () => {
    const store = useLibraryStore();
    await store.load();

    expect(store.chosenPlatforms).toEqual(["douyin"]);
    expect(store.publishWarning).toContain("直接死信");

    store.togglePlatform("douyin", false);
    store.togglePlatform("other", true);
    expect(store.publishWarning).toBeNull();
  });

  it("发布开关开着：这句话就不该出现（判据来自服务端，不靠前端猜）", async () => {
    configureLibraryApi({
      fetchPlatforms: vi.fn(async () => platforms({ publish_enabled: true })),
    });

    const store = useLibraryStore();
    await store.load();

    expect(store.publishWarning).toBeNull();
  });

  it("勾一条就把那条的旧结论擦掉（旧结论说的是上一次提交的事）", async () => {
    const store = useLibraryStore();
    await store.load();
    store.toggle(TASK, true);
    store.results = { [TASK]: batchItem() };

    store.toggle(TASK, false);

    expect(store.results).toEqual({});
  });

  it("提交：一个请求带上全部任务号，逐条结论落回行上，然后重拉一次", async () => {
    const fetchLibrary = vi.fn(async () => snapshot());
    const publishSelected = vi.fn(async () => report());
    configureLibraryApi({ fetchLibrary, publishSelected });

    const store = useLibraryStore();
    await store.load();
    store.toggle(TASK, true);
    await store.submit();

    expect(publishSelected).toHaveBeenCalledTimes(1);
    expect(publishSelected).toHaveBeenCalledWith({ task_ids: [TASK], platforms: ["douyin"] });
    expect(store.results[TASK]).toEqual(batchItem());
    expect(store.notice).toContain("已排入 1 条发布作业");
    expect(fetchLibrary).toHaveBeenCalledTimes(2);
    expect(store.publishing).toBe(false);
  });

  it("一条都没排进去：说清楚为什么，不假装成功", async () => {
    const publishSelected = vi.fn(async () =>
      report({
        items: [batchItem({ queued: 0, duplicates: ["douyin"] })],
        queued_total: 0,
      }),
    );
    configureLibraryApi({ publishSelected });

    const store = useLibraryStore();
    await store.load();
    store.toggle(TASK, true);
    await store.submit();

    expect(store.notice).toContain("一条都没排进去");
    expect(resultText(store.results[TASK])).toBe("早就投过：douyin（无需处理）");
  });

  it("提交失败：把话说出来，不留半份结论", async () => {
    const publishSelected = vi.fn(async () => {
      throw new ApiError("批量投递失败", 500, null);
    });
    configureLibraryApi({ publishSelected });

    const store = useLibraryStore();
    await store.load();
    store.toggle(TASK, true);
    await store.submit();

    expect(store.error).toBe("批量投递失败（HTTP 500）");
    expect(store.results).toEqual({});
    expect(store.publishing).toBe(false);
  });

  it("没勾任务 / 没勾平台：按钮是禁用的，一个请求都不发", async () => {
    const publishSelected = vi.fn(async () => report());
    configureLibraryApi({ publishSelected });

    const store = useLibraryStore();
    await store.load();

    expect(store.canSubmit).toBe(false);
    await store.submit();
    expect(publishSelected).not.toHaveBeenCalled();

    store.toggle(TASK, true);
    expect(store.canSubmit).toBe(true);

    store.togglePlatform("douyin", false);
    expect(store.canSubmit).toBe(false);
    await store.submit();
    expect(publishSelected).not.toHaveBeenCalled();
  });

  it("勾过上限：提交按钮直接禁用（不让人点了收 422）", async () => {
    const many = Array.from({ length: BATCH_MAX + 1 }, (_, index) =>
      item({ task_id: `t${index}` }),
    );
    configureLibraryApi({
      fetchLibrary: vi.fn(async () => snapshot({ items: many, total: many.length })),
    });

    const store = useLibraryStore();
    await store.load();
    store.toggleAll(true);

    expect(store.selected).toHaveLength(BATCH_MAX + 1);
    expect(store.overLimit).toBe(true);
    expect(store.canSubmit).toBe(false);
  });
});
