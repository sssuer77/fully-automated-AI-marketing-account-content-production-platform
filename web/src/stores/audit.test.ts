import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AuditFacets, AuditOp, AuditPage } from "@/api/endpoints/audit";
import { ApiError } from "@/api/http";
import {
  activeFilterCount,
  changedKeys,
  configureAuditApi,
  describeFilters,
  diffSummary,
  emptyFilters,
  pageWindow,
  resultTone,
  toQuery,
  useAuditStore,
} from "./audit";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function op(overrides: Partial<AuditOp> = {}): AuditOp {
  return {
    id: 1,
    at: "2026-09-14T03:00:00.000Z",
    actor: "user",
    actor_ref: null,
    action: "task.approve",
    target_type: "task",
    target_id: "01J9TASK000000000000000000",
    task_id: "01J9TASK000000000000000000",
    before: { status: "awaiting_approval" },
    after: { status: "voicing" },
    result: "ok",
    reason: null,
    source: "http",
    ip: null,
    request_id: null,
    ...overrides,
  };
}

function page(overrides: Partial<AuditPage> = {}): AuditPage {
  return { total: 1, limit: 50, offset: 0, items: [op()], ...overrides };
}

function facets(overrides: Partial<AuditFacets> = {}): AuditFacets {
  return {
    actors: ["user", "system"],
    actions: ["task.approve", "pool.pause"],
    target_types: ["task", "pool"],
    results: ["ok", "denied"],
    ...overrides,
  };
}

/** 手动挡的 promise（用来验证"后发先至"的那种响应不会把列表画回去）。 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configureAuditApi({
    fetchAudit: vi.fn(async () => page()),
    fetchAuditFacets: vi.fn(async () => facets()),
  });
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("空筛选值一律**不发**（发 `actor=` 会被后端当成筛空操作人）", () => {
    const query = toQuery({ ...emptyFilters(), actor: "  ", action: " pool.pause " }, 50, 50);

    expect(query).toEqual({ limit: 50, offset: 50, action: "pool.pause" });
  });

  it("筛选条数与小结：没筛就说「未筛选」，别让人以为库是空的", () => {
    expect(activeFilterCount(emptyFilters())).toBe(0);
    expect(describeFilters(emptyFilters())).toBe("全部留痕（未筛选）");

    const filters = { ...emptyFilters(), actor: "user", result: "denied" };
    expect(activeFilterCount(filters)).toBe(2);
    expect(describeFilters(filters)).toBe("操作人=user · 结果=denied");
  });

  it("翻页区间：空结果、中间页、末页各一种说法", () => {
    expect(pageWindow(0, 50, 0)).toBe("0 / 0");
    expect(pageWindow(0, 50, 1200)).toBe("1–50 / 1200");
    expect(pageWindow(1150, 50, 1200)).toBe("1151–1200 / 1200");
    // offset 越界（换了筛选条件但 offset 没跟上）时不许算出 "1201–1200"
    expect(pageWindow(1200, 50, 1200)).toBe("1200–1200 / 1200");
  });

  it("`denied` 是**有效留痕**（有人想动、被拦下了）⇒ 标黄不标红", () => {
    expect(resultTone("ok")).toBe("ok");
    expect(resultTone("denied")).toBe("warn");
    expect(resultTone("error")).toBe("error");
    expect(resultTone("weird")).toBe("idle");
  });

  it("变更摘要只列真变了的字段（两份 JSON 逐字段比）", () => {
    const row = op({
      before: { status: "awaiting_approval", grade: "A", note: "同" },
      after: { status: "voicing", grade: "A", note: "同" },
    });

    expect(changedKeys(row)).toEqual(["status"]);
    expect(diffSummary(row)).toBe("status");
    expect(diffSummary(op({ before: { a: 1 }, after: { a: 1 } }))).toBe("（无字段变化）");
  });

  it("字段名按字典序（同一条留痕两次渲染不许换顺序）", () => {
    const row = op({ before: {}, after: { zeta: 1, alpha: 2, mid: 3 } });

    expect(diffSummary(row)).toBe("alpha、mid、zeta");
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("store", () => {
  it("start() 拉一次列表 + 一次 facets", async () => {
    const store = useAuditStore();
    store.start();
    await vi.waitFor(() => expect(store.rows.length).toBe(1));

    expect(store.total).toBe(1);
    expect(store.actors).toEqual(["user", "system"]);
    expect(store.summary).toBe("全部留痕（未筛选）");
    expect(store.windowLabel).toBe("1–1 / 1");
  });

  it("改筛选值**不发请求**，apply() 才发，且从第一页重来", async () => {
    const fetchAudit = vi.fn(async () => page({ offset: 50, total: 200 }));
    configureAuditApi({ fetchAudit });
    const store = useAuditStore();
    await store.refresh();
    store.goTo(50);
    await vi.waitFor(() => expect(store.offset).toBe(50));
    fetchAudit.mockClear();

    store.setFilter("actor", "system");
    expect(fetchAudit).not.toHaveBeenCalled(); // 逐字触发等于白跑几十次

    store.apply();
    await vi.waitFor(() => expect(fetchAudit).toHaveBeenCalledTimes(1));
    expect(store.offset).toBe(0);
    expect(fetchAudit).toHaveBeenCalledWith(
      expect.objectContaining({ actor: "system", offset: 0 }),
      expect.anything(),
    );
  });

  it("翻页：下一页带上 offset，到底了就不发", async () => {
    const fetchAudit = vi.fn(async () => page({ total: 60, offset: 0 }));
    configureAuditApi({ fetchAudit });
    const store = useAuditStore();
    await store.refresh();

    expect(store.hasNext).toBe(true);
    store.goNext();
    await vi.waitFor(() => expect(fetchAudit).toHaveBeenCalledTimes(2));
    expect(fetchAudit).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 50 }),
      expect.anything(),
    );
  });

  it("末页上点「下一页」不发请求（总数说了没有更多）", async () => {
    const fetchAudit = vi.fn(async () => page({ total: 1, offset: 0 }));
    configureAuditApi({ fetchAudit });
    const store = useAuditStore();
    await store.refresh();
    fetchAudit.mockClear();

    expect(store.hasNext).toBe(false);
    store.goNext();

    expect(fetchAudit).not.toHaveBeenCalled();
  });

  it("★ 后发先至的旧响应不许把列表画回去（连点两下「下一页」）", async () => {
    const first = deferred<AuditPage>();
    const second = deferred<AuditPage>();
    let call = 0;
    configureAuditApi({
      fetchAudit: vi.fn(() => (call++ === 0 ? first.promise : second.promise)),
    });
    const store = useAuditStore();

    const inFlight = store.refresh();
    store.goTo(50);
    second.resolve(page({ offset: 50, total: 200, items: [op({ id: 99 })] }));
    await vi.waitFor(() => expect(store.page?.offset).toBe(50));

    // 慢的那个这才回来：它带的是旧 offset 的那一页
    first.resolve(page({ offset: 0, total: 200, items: [op({ id: 1 })] }));
    await inFlight;

    expect(store.page?.offset).toBe(50);
    expect(store.rows.map((row) => row.id)).toEqual([99]);
  });

  it("拉取失败写 loadError，但**保留**上一次那一页", async () => {
    const store = useAuditStore();
    await store.refresh();
    expect(store.rows.length).toBe(1);

    configureAuditApi({
      fetchAudit: vi.fn(async () => {
        throw new ApiError("库锁住了", 503, null);
      }),
    });
    await store.refresh();

    expect(store.loadError).toBe("库锁住了（HTTP 503）");
    expect(store.rows.length).toBe(1);
  });

  it("facets 拉不到只写 facetsError：下拉空着不该让列表也打不开", async () => {
    configureAuditApi({
      fetchAuditFacets: vi.fn(async () => {
        throw new ApiError("连接被拒", 0, null);
      }),
    });
    const store = useAuditStore();
    store.start();
    await vi.waitFor(() => expect(store.facetsError).not.toBeNull());

    expect(store.facetsError).toBe("连接被拒");
    expect(store.rows.length).toBe(1);
    expect(store.loadError).toBeNull();
  });

  it("清空筛选：回到第一页 + 一次重查", async () => {
    const fetchAudit = vi.fn(async () => page());
    configureAuditApi({ fetchAudit });
    const store = useAuditStore();
    store.setFilter("result", "denied");
    store.apply();
    await vi.waitFor(() => expect(fetchAudit).toHaveBeenCalledTimes(1));
    fetchAudit.mockClear();

    store.clearFilters();
    await vi.waitFor(() => expect(fetchAudit).toHaveBeenCalledTimes(1));

    expect(store.filters).toEqual(emptyFilters());
    expect(store.filterCount).toBe(0);
  });

  it("空列表的两种原因分开说（「没筛到」 ≠ 「什么都没发生过」）", async () => {
    configureAuditApi({ fetchAudit: vi.fn(async () => page({ total: 0, items: [] })) });
    const store = useAuditStore();
    await store.refresh();

    expect(store.emptyHint).toBe("库里还没有任何留痕");

    store.setFilter("actor", "user");
    store.apply();
    await vi.waitFor(() => expect(store.filterCount).toBe(1));

    expect(store.emptyHint).toBe("这组筛选下没有留痕（放宽条件再试）");
  });
});
