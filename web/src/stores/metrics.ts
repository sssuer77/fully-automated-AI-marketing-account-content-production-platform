// 观测面板的状态（T4.12 · §04.5.11）。
//
// 这一屏与总览台的分工
// --------------------
// 总览台回答"现在怎么样"（10s 一轮自己刷），观测面板回答**"出事了还有得救吗"**：
// 备份新不新鲜、删掉的旧行有没有把盘还回来。所以它**不轮询** —— 这两件事是"要查的
// 时候查一下"，自动跳动的数字只会让人以为备份任务正被实时盯着。
//
// 为什么只读
// ----------
// GC / 备份 / VACUUM 都在 CLI 或计划任务里（`studio gc run` / `studio db backup` /
// `studio db vacuum`）。面板上放按钮，等于给"删数据"开一个只隔一次点击的入口
// （与 `routers/metrics.py` 同一条理由）。
//
// 为什么数字**不在这里重算**
// -------------------------
// 四池 / 今日产量 / 资源 / 进程这四块直接来自后端 `OverviewService.read()` 的同一份
// 数字（同一口径、同一时刻）。在前端再算一遍，就是给"命令行说 12 GB、网页说 15 GB"
// 这类查不完的悬案交学费。这里只做**格式化**与**判灯**。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  fetchMetrics,
  type BackupHealth,
  type MetricsResponse,
  type PoolMetric,
  type StorageHealth,
} from "@/api/endpoints/metrics";
import { describeError, poolLabel } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface MetricsApi {
  fetchMetrics: typeof fetchMetrics;
}

let api: MetricsApi = { fetchMetrics };

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureMetricsApi(overrides: Partial<MetricsApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 备份"多久算旧"（与后端 `observability_service.BACKUP_STALE_HOURS` 同源）。 */
export const BACKUP_STALE_HOURS = 48;

/** 空洞占库文件的比重超过这个数，就该跑一次 `db vacuum` 了（只影响颜色）。 */
export const FREELIST_WARN_SHARE = 0.2;

/** TTS 缓存用到容量的这个比例，就该看看 GC 了（只影响颜色）。 */
export const CACHE_WARN_SHARE = 0.8;

/** 字节 → 人话。这一屏全是 GB 级数字，一位小数足够。 */
export function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Math.max(0, bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${units[unit]}`;
}

/** 备份年龄 → 人话。`null` = 一份都没有，**不是**"0 小时前"。 */
export function ageText(hours: number | null): string {
  if (hours === null) return "没有备份";
  if (hours < 1) return `${Math.round(hours * 60)} 分钟前`;
  if (hours < BACKUP_STALE_HOURS) return `${hours.toFixed(1)} 小时前`;
  return `${(hours / 24).toFixed(1)} 天前`;
}

/** 备份灯：**一份都没有也是红的**（后端的 `stale` 就是这口径，前端不许把它画绿）。 */
export function backupTone(backups: BackupHealth): StatusTone {
  if (backups.count === 0) return "error";
  return backups.stale ? "warn" : "ok";
}

/** 空洞占库文件的比例（`db vacuum` 大约能还回来这么多）。 */
export function freelistShare(storage: StorageHealth): number {
  return storage.db_bytes <= 0 ? 0 : storage.db_freelist_bytes / storage.db_bytes;
}

/** TTS 缓存用掉了容量的多少。 */
export function cacheShare(storage: StorageHealth): number {
  return storage.tts_cache_limit_bytes <= 0
    ? 0
    : storage.tts_cache_bytes / storage.tts_cache_limit_bytes;
}

/** 存储灯：空洞或缓存任一超线就变黄（数字照报，颜色只是提示）。 */
export function storageTone(storage: StorageHealth): StatusTone {
  if (freelistShare(storage) >= FREELIST_WARN_SHARE) return "warn";
  if (cacheShare(storage) >= CACHE_WARN_SHARE) return "warn";
  return "ok";
}

/** 磁盘灯：没采过就是没采过（`idle`），不把"未知"画成"健康"（与总览台同源）。 */
export function diskTone(diskLow: boolean | null | undefined): StatusTone {
  if (diskLow === null || diskLow === undefined) return "idle";
  return diskLow ? "error" : "ok";
}

/** 这一屏最该被看见的一句话（有则报，没有就说"没事"）。 */
export function storageAdvice(storage: StorageHealth): string {
  const share = freelistShare(storage);
  if (share >= FREELIST_WARN_SHARE) {
    return `库里有 ${formatBytes(storage.db_freelist_bytes)} 空洞（占 ${(share * 100).toFixed(0)}%）：删掉的行不会让文件变小，跑 `+"`studio db vacuum`"+` 能还回去。`;
  }
  if (cacheShare(storage) >= CACHE_WARN_SHARE) {
    return `TTS 缓存已用到 ${(cacheShare(storage) * 100).toFixed(0)}% 上限：跑 `+"`studio gc run`"+` 清一轮。`;
  }
  return "没有需要立刻处理的事。";
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const useMetricsStore = defineStore("metrics", () => {
  const snapshot = ref<MetricsResponse | null>(null);
  const loading = ref(false);
  const loadError = ref<string | null>(null);

  let inflight: AbortController | null = null;

  // ── 派生 ────────────────────────────────────────────────────────────
  const pools = computed<PoolMetric[]>(() => snapshot.value?.pools ?? []);
  const services = computed(() => snapshot.value?.services ?? []);
  const resources = computed(() => snapshot.value?.resources ?? null);
  const today = computed(() => snapshot.value?.today ?? null);
  const generatedAt = computed<string | null>(() => snapshot.value?.generated_at ?? null);
  const workerAlive = computed<number>(() => snapshot.value?.worker_alive ?? 0);
  const workerTotal = computed<number>(() => snapshot.value?.worker_total ?? 0);
  const backups = computed<BackupHealth | null>(() => snapshot.value?.backups ?? null);
  const storage = computed<StorageHealth | null>(() => snapshot.value?.storage ?? null);

  const backupToneValue = computed<StatusTone>(() =>
    backups.value === null ? "idle" : backupTone(backups.value),
  );
  const storageToneValue = computed<StatusTone>(() =>
    storage.value === null ? "idle" : storageTone(storage.value),
  );
  const diskToneValue = computed<StatusTone>(() => diskTone(snapshot.value?.disk_low));
  const backupAgeText = computed<string>(() =>
    backups.value === null ? "-" : ageText(backups.value.age_hours ?? null),
  );
  const backupHint = computed<string>(() => {
    if (backups.value === null) return "还没读过";
    if (backups.value.count === 0) return "备份目录里一份都没有 —— 先跑 `studio db backup`";
    if (backups.value.stale) {
      return `最新一份是 ${backupAgeText.value}（超过 ${BACKUP_STALE_HOURS} 小时）—— 先查计划任务，别急着演练`;
    }
    return `最新一份 ${backupAgeText.value} · 共 ${backups.value.count} 份`;
  });
  const storageHint = computed<string>(() =>
    storage.value === null ? "还没读过" : storageAdvice(storage.value),
  );
  /** 三个"会自己长大的目录"加起来（含库文件）—— 面板上给一个总账。 */
  const footprintBytes = computed<number>(() => {
    const current = storage.value;
    if (current === null) return 0;
    return (
      current.db_bytes +
      current.tts_cache_bytes +
      current.hot_archive_bytes +
      current.tmp_bytes
    );
  });

  /** 池卡片标题（复用总览台那份中文词表，不另抄一份）。 */
  function label(pool: string): string {
    return poolLabel(pool);
  }

  // ── 读 ──────────────────────────────────────────────────────────────

  /** 拉一次全量。失败**不清空**旧快照：把一屏数字换成空白更难判断。 */
  async function refresh(): Promise<void> {
    inflight?.abort();
    const controller = new AbortController();
    inflight = controller;
    loading.value = true;
    try {
      snapshot.value = await api.fetchMetrics(controller.signal);
      loadError.value = null;
    } catch (failure) {
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
      inflight = null;
    }
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：这一屏**不轮询**（见模块头），`start()` 就是"进来拉一次"。 */
  function start(): void {
    void refresh();
  }

  function stop(): void {
    inflight?.abort();
    inflight = null;
  }

  return {
    snapshot,
    loading,
    loadError,
    // 派生
    pools,
    services,
    resources,
    today,
    generatedAt,
    workerAlive,
    workerTotal,
    backups,
    storage,
    backupTone: backupToneValue,
    storageTone: storageToneValue,
    diskTone: diskToneValue,
    backupAgeText,
    backupHint,
    storageHint,
    footprintBytes,
    label,
    // 动作
    refresh,
    start,
    stop,
  };
});
