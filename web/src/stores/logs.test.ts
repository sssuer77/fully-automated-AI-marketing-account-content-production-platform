import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  GAP_COOLDOWN_MS,
  LOG_BUFFER_CAPACITY,
  isAlertPayload,
  matchesSearch,
  nextGap,
  toAlertRow,
  toLogRow,
  toSnapshotRow,
  useLogsStore,
} from "./logs";
import type { LogPage } from "@/api/endpoints/logs";
import type { Envelope, SystemLogRow } from "@/ws/events";

function row(id: number, level = "info", overrides: Partial<SystemLogRow> = {}): SystemLogRow {
  return {
    id,
    ts: "2026-09-14T00:00:00Z",
    level,
    source: "test",
    message: `m${id}`,
    task_id: null,
    job_id: null,
    stage: null,
    unit_ref: null,
    worker_id: null,
    trace_id: null,
    duration_ms: null,
    seq_in_task: null,
    payload: {},
    ...overrides,
  };
}

function event(id: number, data: Record<string, unknown> = {}): Envelope {
  return {
    v: 1,
    type: "event",
    channel: "logs",
    seq: id,
    ts: "2026-09-14T00:00:00Z",
    data: { kind: "log.appended", log_id: id, level: "info", message: `m${id}`, ...data },
  };
}

function alertFrame(id: number, data: Record<string, unknown> = {}): Envelope {
  return {
    v: 1,
    type: "event",
    channel: "system",
    seq: id,
    ts: "2026-09-14T00:00:00Z",
    data: {
      kind: "system.alert",
      log_id: id,
      code: "DISK_LOW",
      severity: "warn",
      message: "disk_free=D:9.1GB",
      hint: "清理 data/tmp",
      source: "system",
      ...data,
    },
  };
}

/** 造一条 REST 分页行（字段从生成的 `LogPage` 里取，保证与后端契约同源）。 */
type PageRow = LogPage["logs"][number];

function pageRow(id: number, message: string): PageRow {
  return { id, ts: "2026-09-14T00:00:00Z", level: "info", source: "test", message, payload: {} };
}

/** 用快照播种缓冲（建立连续基线，不触发补洞）。 */
function seedSnapshot(
  store: ReturnType<typeof useLogsStore>,
  logs: PageRow[],
  truncated = false,
): void {
  store.ingest({
    v: 1,
    type: "snapshot",
    channel: "logs",
    seq: 1,
    ts: "2026-09-14T00:00:00Z",
    data: { logs, truncated },
  });
}

/** 让 `void maybeBackfill()` 里的微任务跑完。 */
async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  setActivePinia(createPinia());
});

// ══════════════════════════════════════════════════════════════════════
// 归一（陷阱 #59：事件用 log_id、快照用 id）
// ══════════════════════════════════════════════════════════════════════

describe("归一函数", () => {
  it("toLogRow 认事件里的 log_id 并补齐事件没有的字段", () => {
    const mapped = toLogRow({ log_id: 9, level: "warn", source: "pool", message: "慢" });
    expect(mapped?.id).toBe(9);
    expect(mapped?.level).toBe("warn");
    expect(mapped?.trace_id).toBeNull();
    expect(mapped?.seq_in_task).toBeNull();
  });

  it("toAlertRow 把顶层 code/hint 折回 payload", () => {
    const mapped = toAlertRow({ log_id: 3, code: "JOB_DEAD", hint: "看 data/logs", severity: "error" });
    expect(mapped?.payload).toEqual({ code: "JOB_DEAD", hint: "看 data/logs" });
    expect(mapped?.level).toBe("error");
  });

  it("缺 id ⇒ 拒绝（宁可丢一行，也不让游标推进到 undefined）", () => {
    expect(toLogRow({ id: 9 })).toBeNull();
    expect(toAlertRow({ id: 9 })).toBeNull();
    expect(toSnapshotRow({ log_id: 9 })).toBeNull();
  });

  it("isAlertPayload 只认 8 个告警码（WORKER_DEAD 不算，裁定 39）", () => {
    expect(isAlertPayload({ code: "DISK_LOW" })).toBe(true);
    expect(isAlertPayload({ code: "WORKER_DEAD" })).toBe(false);
    expect(isAlertPayload({})).toBe(false);
  });

  it("matchesSearch 同时搜 message 与 source", () => {
    const item = row(1, "info", { message: "scene_001 done", source: "render.ffmpeg" });
    expect(matchesSearch(item, "scene")).toBe(true);
    expect(matchesSearch(item, "render")).toBe(true);
    expect(matchesSearch(item, "nope")).toBe(false);
    expect(matchesSearch(item, "  ")).toBe(true);
  });
});

// ══════════════════════════════════════════════════════════════════════
// ① 过滤组合（全在本地做 ⇒ 切换不重连、已收的行不会消失）
// ══════════════════════════════════════════════════════════════════════

describe("过滤与搜索", () => {
  it("级别是下限语义，且只影响展示不影响缓冲", () => {
    const store = useLogsStore();
    store.append([row(1, "info"), row(2, "warn"), row(3, "error"), row(4, "fatal")]);
    store.setMinLevel("error");
    expect(store.visible.map((item) => item.id)).toEqual([3, 4]);
    expect(store.rows).toHaveLength(4);
    expect(store.errorCount).toBe(2);
  });

  it("来源 / 任务 / 搜索可叠加", () => {
    const store = useLogsStore();
    store.append([
      row(1, "info", { source: "pool.voice", task_id: "t1", message: "claimed job" }),
      row(2, "info", { source: "pool.voice", task_id: "t2", message: "claimed job" }),
      row(3, "info", { source: "render.ffmpeg", task_id: "t1", message: "scene done" }),
    ]);
    store.setSource("pool.voice");
    expect(store.visible.map((item) => item.id)).toEqual([1, 2]);
    store.setTaskId("t1");
    expect(store.visible.map((item) => item.id)).toEqual([1]);
    store.setTaskId("");
    store.setSearch("scene");
    expect(store.visible).toEqual([]);
  });

  it("sources 是缓冲里出现过的来源（去重排序）", () => {
    const store = useLogsStore();
    store.append([row(1, "info", { source: "b" }), row(2, "info", { source: "a" }), row(3, "info", { source: "b" })]);
    expect(store.sources).toEqual(["a", "b"]);
  });

  it("超过容量裁掉最旧的（内存有界）", () => {
    const store = useLogsStore();
    store.append(Array.from({ length: LOG_BUFFER_CAPACITY + 50 }, (_, index) => row(index + 1)));
    expect(store.rows).toHaveLength(LOG_BUFFER_CAPACITY);
    expect(store.rows[0].id).toBe(51);
    expect(store.lastId).toBe(LOG_BUFFER_CAPACITY + 50);
  });

  it("ingest 认识快照帧与 log.appended 帧，且快照是替换", () => {
    const store = useLogsStore();
    store.append([row(1), row(2)]);
    store.ingest({
      v: 1,
      type: "snapshot",
      channel: "logs",
      seq: 1,
      ts: "t",
      data: { logs: [{ id: 7, level: "info", message: "a" }], truncated: true },
    });
    expect(store.rows.map((item) => item.id)).toEqual([7]);
    expect(store.truncated).toBe(true);
    store.ingest(event(8, { level: "error", message: "b" }));
    expect(store.rows.map((item) => item.id)).toEqual([7, 8]);
  });

  it("同一 id 重复到达只留一份（不重）", () => {
    const store = useLogsStore();
    store.ingest(event(1));
    store.ingest(event(1));
    store.append([row(1)]);
    expect(store.rows).toHaveLength(1);
  });
});

// ══════════════════════════════════════════════════════════════════════
// ② 告警：走 system 通道、不受级别过滤、暂停也照收
// ══════════════════════════════════════════════════════════════════════

describe("告警", () => {
  it("system.alert 进缓冲并标记（告警不走 logs 通道）", () => {
    const store = useLogsStore();
    store.ingestAlert(alertFrame(1));
    expect(store.rows).toHaveLength(1);
    expect(store.alertRows.map((item) => item.id)).toEqual([1]);
    expect(store.rows[0].payload["code"]).toBe("DISK_LOW");
  });

  it("告警不受级别过滤影响（§04.4.6）", () => {
    const store = useLogsStore();
    store.ingestAlert(alertFrame(1, { severity: "warn" }));
    store.append([row(2, "info")]);
    store.setMinLevel("fatal");
    expect(store.visible.map((item) => item.id)).toEqual([1]);
  });

  it("只看告警", () => {
    const store = useLogsStore();
    store.append([row(1, "info"), row(2, "error")]);
    store.ingestAlert(alertFrame(3));
    store.toggleOnlyAlerts();
    expect(store.visible.map((item) => item.id)).toEqual([3]);
  });

  it("快照里的告警行也认得出来（靠 payload.code）", () => {
    const store = useLogsStore();
    store.ingest({
      v: 1,
      type: "snapshot",
      channel: "logs",
      seq: 1,
      ts: "t",
      data: {
        logs: [{ id: 1, level: "warn", message: "x", payload: { code: "DUP_AUDIT_WARN" } }],
        truncated: false,
      },
    });
    expect(store.alertRows).toHaveLength(1);
  });

  it("暂停时普通行只记账不显示，但告警照进（否则暂停期间出事正好看不到）", () => {
    const store = useLogsStore();
    store.togglePause();
    store.ingest(event(1));
    store.ingest(event(2));
    expect(store.rows).toHaveLength(0);
    expect(store.droppedWhilePaused).toBe(2);
    expect(store.lastId).toBe(2);

    store.ingestAlert(alertFrame(3));
    expect(store.rows.map((item) => item.id)).toEqual([3]);

    store.togglePause();
    expect(store.droppedWhilePaused).toBe(0);
    expect(store.lastId).toBe(3);
  });

  it("被裁掉的告警不再出现在 alertIds 里", () => {
    const store = useLogsStore();
    store.ingestAlert(alertFrame(1));
    store.append(Array.from({ length: LOG_BUFFER_CAPACITY }, (_, index) => row(index + 2)));
    expect(store.alertRows).toEqual([]);
  });
});

// ══════════════════════════════════════════════════════════════════════
// ③ 补洞（WS 有损，REST 补齐）
// ══════════════════════════════════════════════════════════════════════

describe("nextGap", () => {
  it("没有悬空 id ⇒ 不补", () => {
    expect(nextGap(5, new Set())).toBeNull();
  });

  it("悬空 id 紧邻游标 ⇒ 不补（只是还没 absorb）", () => {
    expect(nextGap(5, new Set([6]))).toBeNull();
  });

  it("悬空 id 跳号 ⇒ 返回要补的开区间", () => {
    expect(nextGap(5, new Set([9, 12]))).toEqual({ from: 5, to: 9 });
  });
});

describe("补洞", () => {
  it("跳号 ⇒ 用 REST since_id 把中间那段捞回来", async () => {
    const store = useLogsStore();
    const fetchPage = vi.fn(async () => ({
      logs: [pageRow(2, "补回来 2"), pageRow(3, "补回来 3")],
      next_since_id: 3,
      limit: 500,
    }));
    store.configure({ fetchPage, now: () => 0 });

    store.ingest(event(1));
    store.ingest(event(4)); // 跳号：2、3 缺
    await flush();

    expect(fetchPage).toHaveBeenCalledWith({ since_id: 1, limit: 500 });
    expect(store.rows.map((item) => item.id)).toEqual([1, 2, 3, 4]);
    expect(store.lastId).toBe(4);
    expect(store.gapCount).toBe(1);
    expect(store.backfillCount).toBe(1);
  });

  it("告警经 system 通道到达也能补上游标（不会被当成洞）", async () => {
    const store = useLogsStore();
    store.configure({ fetchPage: vi.fn(async () => ({ logs: [], next_since_id: 1, limit: 500 })), now: () => 0 });
    store.ingest(event(1));
    store.ingestAlert(alertFrame(2));
    store.ingest(event(3));
    await flush();
    expect(store.lastId).toBe(3);
    expect(store.gapCount).toBe(0);
  });

  it("冷却期内不重复打 REST（合并窗口每 100ms 就会造一个洞）", async () => {
    const store = useLogsStore();
    const fetchPage = vi.fn(async () => ({ logs: [], next_since_id: 0, limit: 500 }));
    store.configure({ fetchPage, now: () => 0 });
    store.ingest(event(5));
    await flush();
    store.ingest(event(9));
    await flush();
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("补不回来（已被 GC）⇒ 如实记账并跳过，不让游标永远卡住", async () => {
    const store = useLogsStore();
    store.configure({ fetchPage: vi.fn(async () => ({ logs: [], next_since_id: 0, limit: 500 })), now: () => 0 });
    store.ingest(event(5));
    await flush();
    expect(store.lastId).toBe(5);
    expect(store.lostIds).toBe(4);
    expect(store.rows.map((item) => item.id)).toEqual([5]);
  });

  it("REST 挂了不影响实时流", async () => {
    const store = useLogsStore();
    store.configure({
      fetchPage: vi.fn(async () => {
        throw new Error("boom");
      }),
      now: () => 0,
    });
    store.ingest(event(1));
    store.ingest(event(5));
    await flush();
    expect(store.rows.map((item) => item.id)).toEqual([1, 5]);
    expect(store.lastId).toBe(1);
  });

  it("冷却窗口按注入的时钟走（GAP_COOLDOWN_MS 之后可以再补）", async () => {
    let now = 0;
    const store = useLogsStore();
    const fetchPage = vi.fn(async () => ({ logs: [], next_since_id: 0, limit: 500 }));
    store.configure({ fetchPage, now: () => now });
    store.ingest(event(5));
    await flush();
    now += GAP_COOLDOWN_MS;
    store.ingest(event(9));
    await flush();
    expect(fetchPage).toHaveBeenCalledTimes(2);
  });
});

// ══════════════════════════════════════════════════════════════════════
// ④ 翻历史 / 导出
// ══════════════════════════════════════════════════════════════════════

describe("更早", () => {
  it("按 until_id 往前翻一页并接到前面", async () => {
    const store = useLogsStore();
    const fetchPage = vi.fn(async () => ({
      logs: [pageRow(1, "更早 1"), pageRow(2, "更早 2")],
      next_since_id: 2,
      limit: 200,
    }));
    store.configure({ fetchPage, now: () => 0 });
    // 用**快照**播种：它建立基线且不产生缺口，于是这一条只测"翻历史"这一件事
    // （`append` 播种会顺带触发补洞，把两件事混在一起）。
    seedSnapshot(store, [pageRow(10, "m10"), pageRow(11, "m11")]);

    const count = await store.loadEarlier();
    expect(fetchPage).toHaveBeenCalledWith({ until_id: 9, limit: 200 });
    expect(count).toBe(2);
    expect(store.rows.map((item) => item.id)).toEqual([1, 2, 10, 11]);
    expect(store.atOldest).toBe(true);
    expect(store.lastId).toBe(11);
  });

  it("到底之后不再请求", async () => {
    const store = useLogsStore();
    const fetchPage = vi.fn(async () => ({ logs: [], next_since_id: 0, limit: 200 }));
    store.configure({ fetchPage, now: () => 0 });
    seedSnapshot(store, [pageRow(10, "m10")]);
    await store.loadEarlier();
    await store.loadEarlier();
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });
});

describe("导出", () => {
  it("带上当前过滤条件，并交给保存函数", async () => {
    const store = useLogsStore();
    const fetchExport = vi.fn(async () => ({
      text: '{"id":1}\n',
      filename: "studio-logs-x.ndjson",
      rows: 1,
    }));
    const save = vi.fn();
    store.configure({ fetchExport, save, now: () => 0 });

    store.setMinLevel("warn");
    store.setSource("pool.voice");
    store.setTaskId("t1");
    store.setSearch("boom");

    const result = await store.exportToFile();
    expect(fetchExport).toHaveBeenCalledWith({
      level: "warn",
      task_id: "t1",
      source: "pool.voice",
      search: "boom",
      limit: 20_000,
    });
    expect(save).toHaveBeenCalledWith('{"id":1}\n', "studio-logs-x.ndjson");
    expect(result).toEqual({ filename: "studio-logs-x.ndjson", rows: 1 });
    expect(store.exporting).toBe(false);
  });

  it("空过滤项传 null（不是空串）—— 空串会被后端当成「搜空」从而匹配全部", async () => {
    const store = useLogsStore();
    const fetchExport = vi.fn(async () => ({ text: "", filename: "f.ndjson", rows: 0 }));
    store.configure({ fetchExport, save: vi.fn(), now: () => 0 });
    await store.exportToFile();
    expect(fetchExport).toHaveBeenCalledWith({
      level: "info",
      task_id: null,
      source: null,
      search: null,
      limit: 20_000,
    });
  });

  it("导出失败 ⇒ 记错误，不抛出（面板不该因为导出失败而崩）", async () => {
    const store = useLogsStore();
    store.configure({
      fetchExport: vi.fn(async () => {
        throw new Error("network down");
      }),
      save: vi.fn(),
      now: () => 0,
    });
    expect(await store.exportToFile()).toBeNull();
    expect(store.exportError).toContain("network down");
  });
});
