import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PoolPauseResult } from "@/api/endpoints/overview";
import type {
  ConcurrencyResult,
  DeadLetter,
  PoolConsole,
  PoolsResponse,
  RequeueResult,
} from "@/api/endpoints/pools";
import { ApiError } from "@/api/http";
import {
  canLower,
  canRaise,
  configurePoolsApi,
  needsAttention,
  poolState,
  poolTone,
  summarizeRequeue,
  usePoolsStore,
} from "./pools";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function letter(overrides: Partial<DeadLetter> = {}): DeadLetter {
  return {
    job_id: "job_1",
    task_id: "t1",
    unit_type: "sentence",
    unit_ref: "s1",
    attempts: 1,
    max_attempts: 1,
    error_code: "TTS_OOM",
    error_message: "CUDA out of memory",
    finished_at: "2026-09-14T00:00:00Z",
    ...overrides,
  };
}

function card(overrides: Partial<PoolConsole> = {}): PoolConsole {
  return {
    pool: "draft",
    unit_type: "task",
    priority: 100,
    poll_ms: 1000,
    unit_timeout_sec: 120,
    config_concurrency: 2,
    concurrency: 2,
    concurrency_min: 1,
    concurrency_max: 8,
    at_concurrency_ceiling: false,
    paused: false,
    paused_at: null,
    paused_by: null,
    lease_sec: 180,
    max_attempts: 3,
    pending: 0,
    blocked: 0,
    claimed: 0,
    succeeded: 0,
    failed: 0,
    dead: 0,
    canceled: 0,
    running: 0,
    oldest_pending_age_sec: null,
    backlog: 0,
    consecutive_oom: 0,
    oom_threshold: 2,
    auto_degrade_enabled: true,
    dead_letters: [],
    error: null,
    ...overrides,
  };
}

function snapshot(overrides: Partial<PoolsResponse> = {}): PoolsResponse {
  return {
    generated_at: "2026-09-14T00:00:06Z",
    pools: [
      card({ pool: "draft" }),
      card({ pool: "voice", unit_type: "sentence", priority: 200, config_concurrency: 1, concurrency: 1, concurrency_max: 3, at_concurrency_ceiling: true }),
      card({ pool: "render", unit_type: "final", priority: 300, config_concurrency: 1, concurrency: 1, lease_sec: 600 }),
      card({ pool: "publish", unit_type: "publish", priority: 400, config_concurrency: 1, concurrency: 1 }),
    ],
    auto_degrade_enabled: true,
    oom_threshold: 2,
    config_path: "D:/studio/config/pools.yaml",
    config_error: null,
    ...overrides,
  };
}

function concurrencyResult(overrides: Partial<ConcurrencyResult> = {}): ConcurrencyResult {
  return {
    pool: "draft",
    concurrency: 3,
    previous: 2,
    changed: true,
    running: 0,
    pending: 4,
    paused: false,
    note: "并发 2 → 3（下一轮认领即生效）",
    ...overrides,
  };
}

function requeueResult(overrides: Partial<RequeueResult> = {}): RequeueResult {
  return { requested: 1, requeued: ["job_1"], failed: [], ...overrides };
}

function pauseResult(overrides: Partial<PoolPauseResult> = {}): PoolPauseResult {
  return {
    pool: "render",
    paused: true,
    changed: true,
    running: 1,
    pending: 2,
    paused_at: "2026-09-14T00:00:07Z",
    paused_by: "user",
    note: "已暂停（在途 1 个跑完后停）",
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configurePoolsApi({
    fetchPools: vi.fn(async () => snapshot()),
    setConcurrency: vi.fn(async () => concurrencyResult()),
    requeueDead: vi.fn(async () => requeueResult()),
    pausePool: vi.fn(async () => pauseResult()),
  });
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("上下限判据看的是**本池**的线（voice 3，draft 8）", () => {
    const voice = card({ pool: "voice", concurrency: 1, concurrency_min: 1, concurrency_max: 3 });
    expect(canLower(voice)).toBe(false);
    expect(canRaise(voice)).toBe(true);
    expect(canRaise(card({ concurrency: 8, concurrency_max: 8 }))).toBe(false);
    expect(canLower(card({ concurrency: 1, concurrency_min: 1 }))).toBe(false);
  });

  it("计过 OOM 数就该有人看一眼（0 不算）", () => {
    expect(needsAttention(card({ consecutive_oom: 0 }))).toBe(false);
    expect(needsAttention(card({ consecutive_oom: 1 }))).toBe(true);
  });

  it("状态灯与文案：error > paused > running > 待认领 > 空闲", () => {
    expect(poolTone(card({ error: "池参数缺失" }))).toBe("error");
    expect(poolTone(card({ paused: true }))).toBe("warn");
    expect(poolTone(card({ running: 2 }))).toBe("busy");
    expect(poolTone(card({ pending: 3 }))).toBe("warn");
    expect(poolTone(card())).toBe("idle");

    expect(poolState(card({ error: "x" }))).toBe("参数缺失");
    expect(poolState(card({ paused: true, paused_by: "user" }))).toBe("已暂停（user）");
    expect(poolState(card({ running: 2 }))).toBe("在跑 2");
    expect(poolState(card({ pending: 3 }))).toBe("待认领");
    expect(poolState(card())).toBe("空闲");
  });

  it("重投小结：成功与失败都要出现，逐条写清为什么没进去", () => {
    expect(summarizeRequeue(requeueResult())).toBe("重投 1/1 条");
    const partial = summarizeRequeue(
      requeueResult({
        requested: 2,
        requeued: ["job_1"],
        failed: [
          {
            job_id: "job_2",
            code: "JOB_DEAD",
            message: "只有死信可以重投：job_2 当前是 pending",
            remediation: null,
          },
        ],
      }),
    );
    expect(partial).toContain("重投 1/2 条");
    expect(partial).toContain("job_2（只有死信可以重投：job_2 当前是 pending）");
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("usePoolsStore", () => {
  it("refresh 成功 ⇒ 四池就位，派生量跟着走", async () => {
    const store = usePoolsStore();
    await store.refresh();
    expect(store.loadError).toBeNull();
    expect(store.pools.map((item) => item.pool)).toEqual(["draft", "voice", "render", "publish"]);
    expect(store.autoDegradeEnabled).toBe(true);
    expect(store.oomThreshold).toBe(2);
    expect(store.configError).toBeNull();
    expect(store.backlogTotal).toBe(0);
    expect(store.degraded).toEqual([]);
  });

  it("配置读不到：configError 上桌，但运行值照样显示（两件事不互相拖累）", async () => {
    configurePoolsApi({
      fetchPools: vi.fn(async () =>
        snapshot({
          config_error: "读不到 config/pools.yaml：优先级 / 单元类型 / OOM 阈值都不可信",
          pools: [card({ error: "读不到 pools.yaml", unit_type: null, priority: null, config_concurrency: null })],
        }),
      ),
    });
    const store = usePoolsStore();
    await store.refresh();
    expect(store.configError).toContain("读不到");
    expect(store.pools[0]?.concurrency).toBe(2);
    expect(store.pools[0]?.unit_type).toBeNull();
  });

  it("refresh 失败 ⇒ loadError 就位，但**旧快照留着**（一屏数字不因一次抖动变空白）", async () => {
    const store = usePoolsStore();
    await store.refresh();
    configurePoolsApi({
      fetchPools: vi.fn(async () => {
        throw new ApiError("库锁住了", 503, null);
      }),
    });
    await store.refresh();
    expect(store.loadError).toBe("库锁住了（HTTP 503）");
    expect(store.snapshot).not.toBeNull();
  });

  it("死信总数与降级池从四张卡片里汇出来", async () => {
    configurePoolsApi({
      fetchPools: vi.fn(async () =>
        snapshot({
          pools: [
            card({ pool: "draft", dead: 2, dead_letters: [letter(), letter({ job_id: "job_2" })] }),
            card({ pool: "voice", consecutive_oom: 2 }),
            card({ pool: "render" }),
            card({ pool: "publish" }),
          ],
        }),
      ),
    });
    const store = usePoolsStore();
    await store.refresh();
    expect(store.deadTotal).toBe(2);
    expect(store.degraded.map((item) => item.pool)).toEqual(["voice"]);
  });

  it("调并发：请求体就是 pool / concurrency / reason，notice 用后端的 note，并重拉一次", async () => {
    const fetchPools = vi.fn(async () => snapshot());
    const setConcurrency = vi.fn(async () => concurrencyResult());
    configurePoolsApi({ fetchPools, setConcurrency });
    const store = usePoolsStore();

    expect(await store.setConcurrency("draft", 3, "素材多了")).toBe(true);
    expect(setConcurrency).toHaveBeenCalledWith({ pool: "draft", concurrency: 3, reason: "素材多了" });
    expect(store.notice).toBe("「写稿」并发 2 → 3（下一轮认领即生效）");
    expect(store.error).toBeNull();
    expect(fetchPools).toHaveBeenCalledTimes(1);
  });

  it("下调不杀在途：note 里那句「在途 N 个跑完」原样上桌", async () => {
    configurePoolsApi({
      setConcurrency: vi.fn(async () =>
        concurrencyResult({
          concurrency: 1,
          previous: 2,
          running: 2,
          note: "并发 2 → 1：在途 2 个会跑完，之后收敛到 1（**不是**立刻掐掉）",
        }),
      ),
    });
    const store = usePoolsStore();
    await store.setConcurrency("draft", 1);
    expect(store.notice).toContain("在途 2 个会跑完");
  });

  it("并发越界（voice 4 ⇒ 422）：错误进 error，**不**污染 loadError（裁定 71）", async () => {
    configurePoolsApi({
      setConcurrency: vi.fn(async () => {
        throw new ApiError("voice 池并发只能是 1–3，收到 4", 422, null);
      }),
    });
    const store = usePoolsStore();
    expect(await store.setConcurrency("voice", 4)).toBe(false);
    expect(store.error).toBe("voice 池并发只能是 1–3，收到 4（HTTP 422）");
    expect(store.loadError).toBeNull();
  });

  it("动作前清掉上一次的提示（不同时挂着「调好了」和错误）", async () => {
    const store = usePoolsStore();
    await store.setConcurrency("draft", 3);
    expect(store.notice).toContain("并发 2 → 3");

    configurePoolsApi({
      setConcurrency: vi.fn(async () => {
        throw new ApiError("炸了", 500, null);
      }),
    });
    await store.setConcurrency("draft", 2);
    expect(store.notice).toBeNull();
    expect(store.error).toBe("炸了（HTTP 500）");
  });

  it("重投死信：成功如实报，并重拉一次", async () => {
    const fetchPools = vi.fn(async () => snapshot());
    const requeueDead = vi.fn(async () => requeueResult({ requested: 2, requeued: ["job_1", "job_2"] }));
    configurePoolsApi({ fetchPools, requeueDead });
    const store = usePoolsStore();

    expect(await store.requeueDead(["job_1", "job_2"], "换小模型")).toBe(true);
    expect(requeueDead).toHaveBeenCalledWith({ job_ids: ["job_1", "job_2"], reason: "换小模型" });
    expect(store.notice).toBe("重投 2/2 条");
    expect(fetchPools).toHaveBeenCalledTimes(1);
  });

  it("部分失败：notice 逐条写清，返回值是 false（不假装全成了）", async () => {
    configurePoolsApi({
      requeueDead: vi.fn(async () =>
        requeueResult({
          requested: 2,
          requeued: ["job_1"],
          failed: [{ job_id: "job_2", code: "JOB_DEAD", message: "只有死信可以重投", remediation: null }],
        }),
      ),
    });
    const store = usePoolsStore();
    expect(await store.requeueDead(["job_1", "job_2"])).toBe(false);
    expect(store.notice).toContain("job_2（只有死信可以重投）");
  });

  it("空列表**不发请求**（后端 min_length=1，发过去只会换回一个 422）", async () => {
    const requeueDead = vi.fn(async () => requeueResult());
    configurePoolsApi({ requeueDead });
    const store = usePoolsStore();
    expect(await store.requeueDead([])).toBe(false);
    expect(requeueDead).not.toHaveBeenCalled();
  });

  it("单条重投走同一个入口（UI 上那颗按钮）", async () => {
    const requeueDead = vi.fn(async () => requeueResult());
    configurePoolsApi({ requeueDead });
    const store = usePoolsStore();
    expect(await store.requeueOne(letter({ job_id: "job_9" }))).toBe(true);
    expect(requeueDead).toHaveBeenCalledWith({ job_ids: ["job_9"], reason: null });
  });

  it("暂停 / 恢复复用总览台那个写入口（同一件事只有一个权威落点 · 裁定 144）", async () => {
    const pausePool = vi.fn(async () => pauseResult());
    const fetchPools = vi.fn(async () => snapshot());
    configurePoolsApi({ pausePool, fetchPools });
    const store = usePoolsStore();

    expect(await store.pausePool("render", true, "换素材")).toBe(true);
    expect(pausePool).toHaveBeenCalledWith({ pool: "render", paused: true, reason: "换素材" });
    expect(store.notice).toBe("已暂停（在途 1 个跑完后停）");
    expect(fetchPools).toHaveBeenCalledTimes(1);
  });

  it("重复点同一个状态：后端说没改，面板也如实说没改", async () => {
    configurePoolsApi({ pausePool: vi.fn(async () => pauseResult({ changed: false })) });
    const store = usePoolsStore();
    await store.pausePool("voice", true);
    expect(store.notice).toBe("「配音」本来就在暂停状态（没有改动）");
  });

  it("暂停失败只写 error，不污染 loadError", async () => {
    configurePoolsApi({
      pausePool: vi.fn(async () => {
        throw new ApiError("池名不认识", 422, null);
      }),
    });
    const store = usePoolsStore();
    expect(await store.pausePool("render", true)).toBe(false);
    expect(store.error).toBe("池名不认识（HTTP 422）");
    expect(store.loadError).toBeNull();
  });
});
