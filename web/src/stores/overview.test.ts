import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HealthResponse } from "@/api/endpoints/health";
import type {
  ManualPoolTask,
  OverviewResponse,
  PoolPauseResult,
  PoolStatus,
  Resources,
  ServiceStatus,
  StartResult,
  Watchdog,
  WorkerStatus,
} from "@/api/endpoints/overview";
import type { WatchdogTickResult } from "@/api/endpoints/watchdog";
import { ApiError } from "@/api/http";
import {
  configureOverviewApi,
  createCoalescer,
  describeError,
  isMetricsEvent,
  isPoolEvent,
  mergeMetrics,
  policyLabel,
  poolLabel,
  summarizeStart,
  summarizeTick,
  useOverviewStore,
} from "./overview";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function health(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return {
    ok: true,
    db: "data/studio.db",
    spec_version: "3.2",
    latest_log_id: 12,
    ws: {
      clients: [],
      connections: 1,
      cursor: 12,
      emitted: 3,
      merged: 1,
      pending: 0,
      published: 4,
    },
    ...overrides,
  };
}

function worker(overrides: Partial<WorkerStatus> = {}): WorkerStatus {
  return {
    worker_id: "render#1@4242",
    pool: "render",
    status: "idle",
    current_job_id: null,
    gpu_mem_mb: null,
    rss_mb: null,
    last_seen_at: "2026-09-14T00:00:00Z",
    silent_sec: 1,
    stale: false,
    ...overrides,
  };
}

function pool(overrides: Partial<PoolStatus> = {}): PoolStatus {
  return {
    pool: "draft",
    pending: 0,
    blocked: 0,
    claimed: 0,
    succeeded: 0,
    failed: 0,
    dead: 0,
    canceled: 0,
    concurrency: 2,
    running: 0,
    paused: false,
    paused_at: null,
    paused_by: null,
    oldest_pending_age_sec: null,
    backlog: 0,
    alive_workers: 0,
    workers: [],
    error: null,
    ...overrides,
  };
}

function resources(overrides: Partial<Resources> = {}): Resources {
  return {
    sampled_at: "2026-09-14T00:00:05Z",
    cpu_pct: 12.5,
    ram_mb: 4096,
    ram_total_mb: 32768,
    ram_pct: 12.5,
    gpu_util: 3,
    gpu_mem_mb: 1024,
    gpu_mem_total_mb: 8192,
    gpu_name: "NVIDIA GeForce RTX 2070",
    disk_free_gb: 100,
    disk_free_c_gb: 50,
    disk_free_d_min_gb: 15,
    disk_drive: "D:",
    disk_low: false,
    process_rss_mb: 256,
    tts_rtf_avg: null,
    render_fps: null,
    ...overrides,
  };
}

function service(overrides: Partial<ServiceStatus> = {}): ServiceStatus {
  return {
    name: "api",
    readiness: "ready",
    ready: true,
    detail: "GET /health ok",
    remediation: null,
    ...overrides,
  };
}

function manualTask(overrides: Partial<ManualPoolTask> = {}): ManualPoolTask {
  return {
    task_id: "01J9TASKMANUAL0000000000000",
    title: "MC 跑酷：三分钟看懂红石电梯",
    status: "manual_pool",
    attempt_count: 3,
    retry_from: "rendering",
    error_code: "RENDER_FAILED",
    stage_detail: "render: 2/5 场景",
    updated_at: "2026-09-14T00:05:00Z",
    ...overrides,
  };
}

function diskGate(overrides: Partial<Watchdog["disk_gate"]> = {}): Watchdog["disk_gate"] {
  return {
    low: false,
    enabled: true,
    applied: [],
    released: [],
    skipped: [],
    overridden: [],
    detail: null,
    ...overrides,
  };
}

function watchdogStatus(overrides: Partial<Watchdog> = {}): Watchdog {
  return {
    enabled: true,
    runnable: true,
    self_name: "api",
    tick_sec: 5,
    manual_pool_after: 3,
    last_tick_at: "2026-09-14T00:00:05Z",
    services: [
      {
        name: "api",
        guarded: false,
        running: true,
        pid: 4242,
        restarts: 0,
        halted: false,
        next_restart_at: null,
        detail: "守护者守不了自己所在的进程；它的守护者是 ops/start_all.ps1",
      },
    ],
    halted: [],
    restarted_total: 0,
    disk_gate: diskGate(),
    manual_pool: [],
    detail: null,
    ...overrides,
  };
}

function tickResult(overrides: Partial<WatchdogTickResult> = {}): WatchdogTickResult {
  return {
    at: "2026-09-14T00:00:10Z",
    enabled: true,
    services: [],
    restarted: [],
    halted: [],
    disk_gate: diskGate(),
    recovered: [],
    sweep: { failed: [], manual_pool: [], skipped: [] },
    errors: [],
    audit_id: 91,
    note: "守护自检完成：重启 0 个进程，停手 0 个，恢复并发 0 个池",
    ...overrides,
  };
}

function snapshot(overrides: Partial<OverviewResponse> = {}): OverviewResponse {
  return {
    generated_at: "2026-09-14T00:00:06Z",
    pools: [
      pool({ pool: "draft" }),
      pool({ pool: "voice" }),
      pool({ pool: "render", workers: [worker()], alive_workers: 1 }),
      pool({ pool: "publish" }),
    ],
    today: {
      day: "2026-09-14",
      window_start: "2026-09-13T16:00:00Z",
      window_end: "2026-09-14T16:00:00Z",
      created: 3,
      completed: 1,
      published: 0,
      failed: 0,
      by_status: { completed: 1 },
      by_grade: { A: 1 },
    },
    resources: resources(),
    resources_age_sec: 5,
    settings: {
      auto_approve_policy: "grade_a",
      free_c_min_gb: 1,
      free_d_min_gb: 15,
      pause_pools_on_low: true,
    },
    services: [service()],
    worker_total: 1,
    worker_alive: 1,
    disk_low: false,
    watchdog: watchdogStatus(),
    ...overrides,
  };
}

function pauseResult(overrides: Partial<PoolPauseResult> = {}): PoolPauseResult {
  return {
    changed: true,
    note: "已暂停（在途 1 个跑完后停）",
    paused: true,
    paused_at: "2026-09-14T00:00:07Z",
    paused_by: "user",
    pending: 2,
    running: 1,
    pool: "render",
    ...overrides,
  };
}

function startResult(overrides: Partial<StartResult> = {}): StartResult {
  return {
    ok: true,
    started: ["tts"],
    already_running: ["api"],
    ready: ["api", "tts"],
    failed: [],
    port_busy: [],
    degraded: [],
    readiness: [service()],
    elapsed_ms: 3200,
    browser_opened: false,
    url: "http://127.0.0.1:8787",
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configureOverviewApi({
    fetchHealth: vi.fn(async () => health()),
    fetchOverview: vi.fn(async () => snapshot()),
    pausePool: vi.fn(async () => pauseResult()),
    setAutoPolicy: vi.fn(async () => ({
      changed: true,
      config_path: "config/app.yaml",
      policy: "grade_ab",
      previous: "grade_a",
    })),
    startServices: vi.fn(async () => startResult()),
    runWatchdogTick: vi.fn(async () => tickResult()),
  });
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("池名与策略都翻成中文，认不出的照原样（别把未知吞掉）", () => {
    expect(poolLabel("draft")).toBe("写稿");
    expect(poolLabel("publish")).toBe("发布");
    expect(poolLabel("weird")).toBe("weird");
    expect(policyLabel("grade_ab")).toBe("A/B 全自动（唯一的人工节点被绕过）");
    expect(policyLabel("???")).toBe("???");
  });

  it("describeError 把统一信封翻成人话（带状态码）", () => {
    expect(describeError(new ApiError("已经有一次启动在进行中", 409, null))).toBe(
      "已经有一次启动在进行中（HTTP 409）",
    );
    expect(describeError(new ApiError("超时", 0, null))).toBe("超时");
    expect(describeError("boom")).toBe("boom");
  });

  it("通道判据只认自己那两个 kind（这两条通道上还有别的 kind）", () => {
    expect(isPoolEvent("pool.stats")).toBe(true);
    expect(isPoolEvent("pool.worker_status")).toBe(true);
    expect(isPoolEvent("log.appended")).toBe(false);
    expect(isMetricsEvent("metrics.tick")).toBe(true);
    expect(isMetricsEvent("pool.stats")).toBe(false);
  });

  it("mergeMetrics 直接替换资源那一段，并把采样年龄归零", () => {
    const before = snapshot();
    const after = mergeMetrics(before, {
      ...resources({ cpu_pct: 88.5, disk_low: true }),
      kind: "metrics.tick",
    });
    expect(after.resources?.cpu_pct).toBe(88.5);
    expect(after.resources_age_sec).toBe(0);
    expect(after.disk_low).toBe(true);
    // 别的段落**一个都没动**：`metrics.tick` 只带资源那一段。
    expect(after.pools).toBe(before.pools);
    expect(after.today).toBe(before.today);
  });

  it("mergeMetrics 载荷里没有 disk_low 时保留旧值（不把未知写成健康）", () => {
    const after = mergeMetrics(snapshot({ disk_low: true }), { cpu_pct: 1 });
    expect(after.disk_low).toBe(true);
  });

  it("summarizeTick 把「没做事」与「没留痕」都如实说出来", () => {
    expect(summarizeTick(tickResult())).toContain("留痕 #91");
    expect(summarizeTick(tickResult({ enabled: false, note: "随便什么" }))).toContain(
      "守护已停用",
    );
    expect(summarizeTick(tickResult({ audit_id: null }))).toContain("留痕未写入");
  });

  it("summarizeStart 成功与失败都要出现", () => {
    expect(summarizeStart(startResult())).toBe("五进程就绪（3.2s）：已在跑 api · 新拉起 tts");
    const bad = summarizeStart(
      startResult({ ok: false, failed: ["render"], port_busy: ["voice"], degraded: ["tts"] }),
    );
    expect(bad).toContain("没全部就绪");
    expect(bad).toContain("失败 render");
    expect(bad).toContain("端口被占 voice");
    expect(bad).toContain("降级 tts");
  });
});

// ══════════════════════════════════════════════════════════════════════
// 合并节流（WS 的 `pools` 一拍扇出 4 条 ⇒ 只重拉一次）
// ══════════════════════════════════════════════════════════════════════

describe("createCoalescer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("窗口内的多次 schedule 只跑一次", () => {
    const run = vi.fn();
    const coalescer = createCoalescer(run, 500);
    coalescer.schedule();
    coalescer.schedule();
    coalescer.schedule();
    expect(run).not.toHaveBeenCalled();
    vi.advanceTimersByTime(499);
    expect(run).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("窗口过去之后再 schedule 会再跑一次", () => {
    const run = vi.fn();
    const coalescer = createCoalescer(run, 500);
    coalescer.schedule();
    vi.advanceTimersByTime(500);
    coalescer.schedule();
    vi.advanceTimersByTime(500);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("cancel 掉就不再跑（`stop()` 走这条路）", () => {
    const run = vi.fn();
    const coalescer = createCoalescer(run, 500);
    coalescer.schedule();
    coalescer.cancel();
    vi.advanceTimersByTime(1000);
    expect(run).not.toHaveBeenCalled();
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("useOverviewStore", () => {
  it("refresh 成功 ⇒ 快照就位，派生量跟着走", async () => {
    const store = useOverviewStore();
    await store.refresh();
    expect(store.loadError).toBeNull();
    expect(store.pools.map((card) => card.pool)).toEqual(["draft", "voice", "render", "publish"]);
    expect(store.today?.completed).toBe(1);
    expect(store.resourcesAgeSec).toBe(5);
    expect(store.diskLow).toBe(false);
    expect(store.autoPolicy).toBe("grade_a");
    expect(store.autoApproveOn).toBe(false);
    expect(store.workerAlive).toBe(1);
    expect(store.workerTotal).toBe(1);
  });

  it("refresh 失败 ⇒ loadError 就位，但**旧快照留着**（一屏数字不因一次抖动变空白）", async () => {
    const store = useOverviewStore();
    await store.refresh();
    configureOverviewApi({
      fetchOverview: vi.fn(async () => {
        throw new ApiError("库锁住了", 503, null);
      }),
    });
    await store.refresh();
    expect(store.loadError).toBe("库锁住了（HTTP 503）");
    expect(store.snapshot).not.toBeNull();
  });

  it("暂停一个池：notice 用后端的 note，并重拉一次", async () => {
    const fetchOverview = vi.fn(async () => snapshot());
    const pausePool = vi.fn(async () => pauseResult());
    configureOverviewApi({ fetchOverview, pausePool });
    const store = useOverviewStore();

    expect(await store.pausePool("render", true, "磁盘紧张")).toBe(true);
    expect(pausePool).toHaveBeenCalledWith({ pool: "render", paused: true, reason: "磁盘紧张" });
    expect(store.notice).toBe("已暂停（在途 1 个跑完后停）");
    expect(store.error).toBeNull();
    expect(fetchOverview).toHaveBeenCalledTimes(1);
  });

  it("重复点同一个状态：后端说没改，面板也如实说没改", async () => {
    configureOverviewApi({ pausePool: vi.fn(async () => pauseResult({ changed: false })) });
    const store = useOverviewStore();
    await store.pausePool("voice", true);
    expect(store.notice).toBe("「配音」本来就在暂停状态（没有改动）");
  });

  it("动作失败只写 error，不污染 loadError（裁定 71）", async () => {
    configureOverviewApi({
      pausePool: vi.fn(async () => {
        throw new ApiError("池名不认识", 422, null);
      }),
    });
    const store = useOverviewStore();
    expect(await store.pausePool("render", true)).toBe(false);
    expect(store.error).toBe("池名不认识（HTTP 422）");
    expect(store.loadError).toBeNull();
  });

  it("动作前清掉上一次的提示（不同时挂着「已暂停」和错误）", async () => {
    const store = useOverviewStore();
    await store.pausePool("render", true);
    expect(store.notice).toContain("已暂停");

    configureOverviewApi({
      pausePool: vi.fn(async () => {
        throw new ApiError("炸了", 500, null);
      }),
    });
    await store.pausePool("render", false);
    expect(store.notice).toBeNull();
    expect(store.error).toBe("炸了（HTTP 500）");
  });

  it("一键全自动：notice 写明 previous → policy 与落盘路径", async () => {
    const store = useOverviewStore();
    await store.refresh();
    expect(await store.setAutoPolicy("grade_ab")).toBe(true);
    expect(store.notice).toBe("放行策略：grade_a → grade_ab（已写盘 config/app.yaml）");
  });

  it("启动五进程：成功与失败都如实报", async () => {
    const store = useOverviewStore();
    expect(await store.startServices()).toBe(true);
    expect(store.notice).toContain("五进程就绪");

    configureOverviewApi({
      startServices: vi.fn(async () => startResult({ ok: false, failed: ["render"] })),
    });
    expect(await store.startServices()).toBe(false);
    expect(store.notice).toContain("失败 render");
  });

  it("启动撞上单飞守卫（409）⇒ error 里带状态码", async () => {
    configureOverviewApi({
      startServices: vi.fn(async () => {
        throw new ApiError("已经有一次启动在进行中，请等它结束", 409, null);
      }),
    });
    const store = useOverviewStore();
    expect(await store.startServices()).toBe(false);
    expect(store.error).toContain("HTTP 409");
  });

  it("磁盘灯：**没采过是 idle**，不把「未知」画成「健康」", async () => {
    const store = useOverviewStore();
    expect(store.diskTone).toBe("idle");
    await store.refresh();
    expect(store.diskTone).toBe("ok");

    configureOverviewApi({
      fetchOverview: vi.fn(async () =>
        snapshot({ disk_low: true, resources: resources({ disk_low: true }) }),
      ),
    });
    await store.refresh();
    expect(store.diskTone).toBe("error");
  });

  it("守护那一段：自愈次数 / 停手 / 人工池都跟着快照走", async () => {
    configureOverviewApi({
      fetchOverview: vi.fn(async () =>
        snapshot({
          watchdog: watchdogStatus({
            restarted_total: 4,
            halted: ["voice"],
            manual_pool: [manualTask()],
          }),
        }),
      ),
    });
    const store = useOverviewStore();
    await store.refresh();
    expect(store.watchdog?.restarted_total).toBe(4);
    expect(store.watchdogHalted).toEqual(["voice"]);
    expect(store.manualPool.map((item) => item.task_id)).toEqual([
      "01J9TASKMANUAL0000000000000",
    ]);
    // 停手是**事故**，比"人工池有待处置"更该抢到红色。
    expect(store.watchdogTone).toBe("error");
  });

  it("守护灯：没接线 ⇒ idle；人工池非空 ⇒ warn；一切正常 ⇒ ok", async () => {
    const store = useOverviewStore();
    expect(store.watchdogTone).toBe("idle"); // 还没拉过

    await store.refresh();
    expect(store.watchdogTone).toBe("ok");

    configureOverviewApi({
      fetchOverview: vi.fn(async () =>
        snapshot({ watchdog: watchdogStatus({ manual_pool: [manualTask()] }) }),
      ),
    });
    await store.refresh();
    expect(store.watchdogTone).toBe("warn");

    configureOverviewApi({
      fetchOverview: vi.fn(async () => snapshot({ watchdog: null })),
    });
    await store.refresh();
    expect(store.watchdog).toBeNull();
    expect(store.watchdogTone).toBe("idle");
  });

  it("立即自检：notice 用后端那一句话，并重拉一次", async () => {
    const fetchOverview = vi.fn(async () => snapshot());
    const runWatchdogTick = vi.fn(async () => tickResult({ audit_id: 7 }));
    configureOverviewApi({ fetchOverview, runWatchdogTick });
    const store = useOverviewStore();

    expect(await store.runWatchdogTick("手动验证")).toBe(true);
    expect(runWatchdogTick).toHaveBeenCalledWith({ reason: "手动验证" });
    expect(store.notice).toContain("留痕 #7");
    expect(store.error).toBeNull();
    expect(fetchOverview).toHaveBeenCalledTimes(1);
  });

  it("自检失败只写 error（守护没起来不该把 loadError 也点着）", async () => {
    configureOverviewApi({
      runWatchdogTick: vi.fn(async () => {
        throw new ApiError("守护正忙", 409, null);
      }),
    });
    const store = useOverviewStore();
    expect(await store.runWatchdogTick()).toBe(false);
    expect(store.error).toBe("守护正忙（HTTP 409）");
    expect(store.loadError).toBeNull();
  });

  it("健康探针失败写 healthError，与 loadError 互不干扰", async () => {
    configureOverviewApi({
      fetchHealth: vi.fn(async () => {
        throw new ApiError("连接被拒", 0, null);
      }),
    });
    const store = useOverviewStore();
    await store.refreshHealth();
    expect(store.healthError).toBe("连接被拒");
    expect(store.healthTone).toBe("error");
    expect(store.loadError).toBeNull();
  });
});
