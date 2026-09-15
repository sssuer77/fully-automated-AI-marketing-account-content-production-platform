import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  BackupHealth,
  MetricsResponse,
  PoolMetric,
  StorageHealth,
} from "@/api/endpoints/metrics";
import { ApiError } from "@/api/http";
import {
  BACKUP_STALE_HOURS,
  ageText,
  backupTone,
  cacheShare,
  configureMetricsApi,
  diskTone,
  formatBytes,
  freelistShare,
  storageAdvice,
  storageTone,
  useMetricsStore,
} from "./metrics";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function poolMetric(overrides: Partial<PoolMetric> = {}): PoolMetric {
  return {
    pool: "voice",
    pending: 3,
    running: 1,
    backlog: 2,
    dead: 0,
    failed: 1,
    paused: false,
    alive_workers: 1,
    oldest_pending_age_sec: 12,
    ...overrides,
  };
}

function backups(overrides: Partial<BackupHealth> = {}): BackupHealth {
  return {
    dir: "D:/studio/data/backups",
    count: 3,
    newest: "20260914",
    age_hours: 1.5,
    total_bytes: 3 * 1024 * 1024,
    stale: false,
    ...overrides,
  };
}

function storage(overrides: Partial<StorageHealth> = {}): StorageHealth {
  return {
    db_bytes: 100 * 1024 * 1024,
    db_freelist_bytes: 0,
    tts_cache_bytes: 1024 * 1024 * 1024,
    tts_cache_limit_bytes: 5 * 1024 ** 3,
    hot_archive_bytes: 2048,
    tmp_bytes: 4096,
    ...overrides,
  };
}

function snapshot(overrides: Partial<MetricsResponse> = {}): MetricsResponse {
  return {
    generated_at: "2026-09-14T11:00:00.000Z",
    pools: [poolMetric()],
    today: {
      day: "2026-09-14",
      created: 4,
      completed: 3,
      failed: 1,
      published: 2,
      by_grade: { A: 2, B: 1 },
      by_status: { completed: 3 },
      window_start: "2026-09-13T16:00:00.000Z",
      window_end: "2026-09-14T16:00:00.000Z",
    },
    services: [
      { name: "api", ready: true, readiness: "ready", detail: "pid 123", remediation: null },
    ],
    resources: null,
    resources_age_sec: null,
    disk_low: false,
    worker_total: 5,
    worker_alive: 4,
    backups: backups(),
    storage: storage(),
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  configureMetricsApi({ fetchMetrics: vi.fn(async () => snapshot()) });
});

afterEach(() => {
  vi.useRealTimers();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数
// ══════════════════════════════════════════════════════════════════════

describe("纯函数", () => {
  it("字节换算：B 不带小数，往上每级一位小数；负数当 0", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GB");
    expect(formatBytes(-1)).toBe("0 B");
  });

  it("备份年龄：没有就是「没有备份」，不是「0 小时前」", () => {
    expect(ageText(null)).toBe("没有备份");
    expect(ageText(0.2)).toBe("12 分钟前");
    expect(ageText(1.5)).toBe("1.5 小时前");
    expect(ageText(BACKUP_STALE_HOURS + 24)).toBe("3.0 天前");
  });

  it("备份灯：一份都没有也是红的（与后端 stale 口径一致）", () => {
    expect(backupTone(backups({ count: 0, newest: null, age_hours: null, stale: true }))).toBe(
      "error",
    );
    expect(backupTone(backups({ age_hours: 60, stale: true }))).toBe("warn");
    expect(backupTone(backups())).toBe("ok");
  });

  it("空洞占比与缓存占比：分母是 0 时不算出 NaN", () => {
    expect(freelistShare(storage({ db_bytes: 0, db_freelist_bytes: 10 }))).toBe(0);
    expect(freelistShare(storage({ db_bytes: 100, db_freelist_bytes: 25 }))).toBe(0.25);
    expect(cacheShare(storage({ tts_cache_limit_bytes: 0, tts_cache_bytes: 10 }))).toBe(0);
    expect(
      cacheShare(storage({ tts_cache_bytes: 4 * 1024 ** 3, tts_cache_limit_bytes: 5 * 1024 ** 3 })),
    ).toBeCloseTo(0.8, 5);
  });

  it("存储灯：空洞或缓存任一超线就变黄", () => {
    expect(storageTone(storage())).toBe("ok");
    expect(storageTone(storage({ db_freelist_bytes: 30 * 1024 * 1024 }))).toBe("warn");
    expect(
      storageTone(storage({ tts_cache_bytes: 5 * 1024 ** 3, tts_cache_limit_bytes: 5 * 1024 ** 3 })),
    ).toBe("warn");
  });

  it("磁盘灯：没采过是 idle，不把「未知」画成「健康」", () => {
    expect(diskTone(null)).toBe("idle");
    expect(diskTone(undefined)).toBe("idle");
    expect(diskTone(false)).toBe("ok");
    expect(diskTone(true)).toBe("error");
  });

  it("该说的一句话：空洞优先，其次缓存，都没有就说没事", () => {
    expect(storageAdvice(storage())).toBe("没有需要立刻处理的事。");
    expect(storageAdvice(storage({ db_freelist_bytes: 40 * 1024 * 1024 }))).toContain(
      "studio db vacuum",
    );
    expect(
      storageAdvice(storage({ tts_cache_bytes: 5 * 1024 ** 3, tts_cache_limit_bytes: 5 * 1024 ** 3 })),
    ).toContain("studio gc run");
  });
});

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

describe("store", () => {
  it("一次拿全：六块都跟着快照走", async () => {
    const store = useMetricsStore();
    await store.refresh();

    expect(store.pools.map((card) => card.pool)).toEqual(["voice"]);
    expect(store.services.map((service) => service.name)).toEqual(["api"]);
    expect(store.today?.completed).toBe(3);
    expect(store.backups?.count).toBe(3);
    expect(store.storage?.db_bytes).toBe(100 * 1024 * 1024);
    expect(store.workerAlive).toBe(4);
    expect(store.workerTotal).toBe(5);
    expect(store.generatedAt).toBe("2026-09-14T11:00:00.000Z");
  });

  it("池名走总览台那份中文词表（不另抄一份）", async () => {
    const store = useMetricsStore();
    await store.refresh();

    expect(store.label("voice")).toBe("配音");
    expect(store.label("draft")).toBe("写稿");
    expect(store.label("weird")).toBe("weird"); // 认不出就照原样，别把未知吞掉
  });

  it("总账 = 库文件 + 缓存 + 归档 + 临时", async () => {
    const store = useMetricsStore();
    await store.refresh();

    expect(store.footprintBytes).toBe(100 * 1024 * 1024 + 1024 * 1024 * 1024 + 2048 + 4096);
  });

  it("备份那一句：新鲜 / 该查计划任务 / 一份都没有，三种说法", async () => {
    const store = useMetricsStore();
    await store.refresh();
    expect(store.backupHint).toContain("最新一份 1.5 小时前");

    configureMetricsApi({
      fetchMetrics: vi.fn(async () =>
        snapshot({ backups: backups({ age_hours: 72, stale: true }) }),
      ),
    });
    await store.refresh();
    expect(store.backupTone).toBe("warn");
    expect(store.backupHint).toContain("先查计划任务");

    configureMetricsApi({
      fetchMetrics: vi.fn(async () =>
        snapshot({
          backups: backups({ count: 0, newest: null, age_hours: null, stale: true }),
        }),
      ),
    });
    await store.refresh();
    expect(store.backupTone).toBe("error");
    expect(store.backupHint).toContain("一份都没有");
  });

  it("拉取失败写 loadError，但**保留**上一次那份快照", async () => {
    const store = useMetricsStore();
    await store.refresh();

    configureMetricsApi({
      fetchMetrics: vi.fn(async () => {
        throw new ApiError("扫描超时", 0, null);
      }),
    });
    await store.refresh();

    expect(store.loadError).toBe("扫描超时");
    expect(store.backups?.count).toBe(3);
    expect(store.loading).toBe(false);
  });

  it("★ 这一屏**不轮询**：start() 只拉一次，放着不动也不会自己再发请求", async () => {
    vi.useFakeTimers();
    const fetchMetrics = vi.fn(async () => snapshot());
    configureMetricsApi({ fetchMetrics });
    const store = useMetricsStore();

    store.start();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(fetchMetrics).toHaveBeenCalledTimes(1);
  });
});
