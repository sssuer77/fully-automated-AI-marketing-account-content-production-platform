// 总览台状态（T4.1 建骨架 · T4.2 补齐四池 / 今日产量 / 资源 / 启动 / 暂停 / 全自动）。
//
// 一屏要回答六个问题
// ------------------
// ① 四个池现在什么情况？② 今天出了多少片？③ 机器还撑得住吗？
// ④ 我能按哪些按钮？⑤ 后端还在不在？⑥ **这台机器还在自己照顾自己吗**（T4.11）？
//
// 为什么健康状态是**轮询**而不是订阅
// ---------------------------------
// `GET /api/v1/health` 是**就绪判据**（§04.8.1），它的真相在进程存活与依赖探针上，
// 本来就不是"事件"；而 WS 自己断了的时候，恰恰最需要有一个独立信道告诉我们后端
// 还在不在 —— 用 WS 推健康状态会在这个场景下自欺欺人。
//
// WS 事件为什么一个**直接合并**、一个**重拉**
// -----------------------------------------
// - `metrics.tick` 的载荷与 `ResourcesModel` **同源同形**（都出自
//   `ResourceSnapshot.to_dict()`）⇒ 直接合并，不重拉。
// - `pool.stats` / `pool.worker_status` 是 `PoolStatus` 的**真子集**：没有
//   `paused_at` / `paused_by` / `backlog` / `alive_workers` / `error`，`workers`
//   还被拆成单独的事件 ⇒ 在前端合并等于维护第二份卡片形状，迟早与后端分叉。
//   它们本来就只有 1 Hz，合并窗口内重拉一次远比抄一份契约便宜。
//
// 三个错误位分开放（裁定 71）
// --------------------------
// `healthError`（探针）/ `loadError`（拉取）/ `error`（动作）。混成一个的后果是：
// 一次拉取失败会把"我刚点的暂停到底成没成"这句话吞掉。
//
// 为什么**没有**兜底轮询
// ---------------------
// 总览由 WS 驱动：断了的时候顶栏的 WS 灯就是红的，用户一眼知道"这屏不动了"；
// 而 15s 一次的兜底轮询会喂出一个"看起来在动、其实是旧数据"的假象 —— 那比
// 明说"不动了"更糟。重连成功时 store 会主动重拉一次，把缺口补上。
//
// 无人值守那一段（T4.11）为什么搭在同一个响应里
// -------------------------------------------
// `manual_pool` 是**验收明文要求"可见"**的东西，而它的可见性依赖"用户会打开这一屏"。
// 让它走另一个 REST、再由总览台聚合，等于把"有没有掉进人工池"拆成两次请求 + 两处
// 缓存；搭在 `/overview` 里，一次拉取就把"池子/产量/资源/守护"对齐到同一时刻。

import { defineStore } from "pinia";
import { computed, ref, watch } from "vue";

import { fetchHealth, type HealthResponse } from "@/api/endpoints/health";
import {
  fetchOverview,
  pausePool as pausePoolRequest,
  setAutoPolicy as setAutoPolicyRequest,
  startServices as startServicesRequest,
  type OverviewResponse,
  type PoolStatus,
  type Resources,
  type ServiceStatus,
  type Settings,
  type StartResult,
  type TodayOutput,
  type Watchdog,
} from "@/api/endpoints/overview";
import {
  runWatchdogTick as runWatchdogTickRequest,
  type WatchdogTickResult,
} from "@/api/endpoints/watchdog";
import { ApiError } from "@/api/http";
import type { StatusTone } from "@/components/tone";
import { useChannelStream } from "@/composables/useTaskStream";
import { useWsConnection } from "@/composables/useWsConnection";
import type { Envelope } from "@/ws/events";

/** 轮询间隔：健康探针很便宜（一次 SELECT），10s 足够让"进程刚挂"在视野内。 */
export const HEALTH_POLL_MS = 10_000;

/** `pools` 通道的合并窗口：`pool.stats` 有 1 Hz，一拍还会扇出四条（四个池）。 */
export const POOL_EVENT_COALESCE_MS = 500;

/** 池名（与后端 `PoolName` / `pool_settings.pool` 的 CHECK 同源）。 */
export type PoolName = "draft" | "voice" | "render" | "publish";

/** 放行策略（与 `config/app.yaml → approval.auto_approve_policy` 同源）。 */
export type PolicyName = "off" | "grade_a" | "grade_ab";

/** 池 → 中文（卡片标题与提示共用一份）。 */
export const POOL_LABELS: Record<PoolName, string> = {
  draft: "写稿",
  voice: "配音",
  render: "渲染",
  publish: "发布",
};

/**
 * 策略 → 一句话说明（口径来自 `domain/scoring.py::gate_action` 的那张表）。
 *
 * 不写"低/中/高"这种模糊说法：这三档的差别是**哪一级稿子还需要人点一下**，
 * 而"一键全自动"正是把最后那个人点一下也去掉 —— 面板上必须写清楚。
 */
export const POLICY_LABELS: Record<PolicyName, string> = {
  off: "全人工（A 级也要过确认闸）",
  grade_a: "A 级自动放行（B 级改稿后过闸）",
  grade_ab: "A/B 全自动（唯一的人工节点被绕过）",
};

/** 池名 → 中文（认不出就照原样显示，别把未知池名吞掉）。 */
export function poolLabel(pool: string): string {
  return POOL_LABELS[pool as PoolName] ?? pool;
}

/**
 * 后端的 `pool` 是自由字符串（`PoolStatus.pool`），而入参要的是字面量联合。
 *
 * 之所以敢直接收窄：取值由 `pool_settings.pool` 的 CHECK 钉死成那四个，
 * 而 `PoolCard` 是按 `POOL_NAMES` 逐个造出来的。认不出的池名不会出现在这里。
 */
export function asPoolName(pool: string): PoolName {
  return pool as PoolName;
}

/** 策略 → 中文（同上）。 */
export function policyLabel(policy: string): string {
  return POLICY_LABELS[policy as PolicyName] ?? policy;
}

/** 注入点（单测用假件替换，生产用真实现）。 */
export interface OverviewApi {
  fetchHealth: typeof fetchHealth;
  fetchOverview: typeof fetchOverview;
  pausePool: typeof pausePoolRequest;
  setAutoPolicy: typeof setAutoPolicyRequest;
  startServices: typeof startServicesRequest;
  runWatchdogTick: typeof runWatchdogTickRequest;
}

let api: OverviewApi = {
  fetchHealth,
  fetchOverview,
  pausePool: pausePoolRequest,
  setAutoPolicy: setAutoPolicyRequest,
  startServices: startServicesRequest,
  runWatchdogTick: runWatchdogTickRequest,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureOverviewApi(overrides: Partial<OverviewApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 把一次请求失败翻成人话（与 `stores/topics.ts` 同源；两个 store 之间不留依赖）。 */
export function describeError(reason: unknown): string {
  if (reason instanceof ApiError) {
    return reason.status === 0 ? reason.message : `${reason.message}（HTTP ${reason.status}）`;
  }
  return String(reason);
}

/** `pools` 通道上要不要重拉（这条通道还有别的 kind，不能一律重拉）。 */
export function isPoolEvent(kind: unknown): boolean {
  return kind === "pool.stats" || kind === "pool.worker_status";
}

/** `metrics` 通道上要不要合并（同上）。 */
export function isMetricsEvent(kind: unknown): boolean {
  return kind === "metrics.tick";
}

/** 把一条 `metrics.tick` 合并进快照（载荷与 `ResourcesModel` 同源 ⇒ 直接替换那一段）。 */
export function mergeMetrics(
  current: OverviewResponse,
  payload: Record<string, unknown>,
): OverviewResponse {
  const diskLow = typeof payload.disk_low === "boolean" ? payload.disk_low : current.disk_low;
  return {
    ...current,
    resources: payload as unknown as Resources,
    // 刚采的这一拍就在眼前 ⇒ 年龄归零；否则面板会写"5 秒前的采样"，
    // 而它其实是刚刚推过来的。
    resources_age_sec: 0,
    disk_low: diskLow ?? null,
  };
}

/**
 * 合并节流器（`pools` 一拍扇出 4 条 `pool.stats` + N 条心跳 ⇒ 只重拉一次）。
 *
 * 抽成独立件是为了**可测**：WS 订阅本身要真连一条 WebSocket，而"窗口内的多条
 * 只触发一次"这件事是纯时间逻辑，不该被连接拖累。
 */
export interface Coalescer {
  schedule(): void;
  cancel(): void;
}

export function createCoalescer(run: () => void, windowMs: number): Coalescer {
  let pending: ReturnType<typeof setTimeout> | null = null;
  return {
    schedule(): void {
      if (pending !== null) return;
      pending = setTimeout(() => {
        pending = null;
        run();
      }, windowMs);
    },
    cancel(): void {
      if (pending === null) return;
      clearTimeout(pending);
      pending = null;
    },
  };
}

/** 启动结论的一句话小结（**成功与失败都要出现**，不能只说"没全起来"）。 */
export function summarizeStart(result: StartResult): string {
  const parts: string[] = [];
  if (result.already_running.length > 0) parts.push(`已在跑 ${result.already_running.join("/")}`);
  if (result.started.length > 0) parts.push(`新拉起 ${result.started.join("/")}`);
  if (result.failed.length > 0) parts.push(`失败 ${result.failed.join("/")}`);
  if (result.port_busy.length > 0) parts.push(`端口被占 ${result.port_busy.join("/")}`);
  if (result.degraded.length > 0) parts.push(`降级 ${result.degraded.join("/")}`);
  const head = result.ok ? "五进程就绪" : "没全部就绪";
  const elapsed = `${(result.elapsed_ms / 1000).toFixed(1)}s`;
  return parts.length > 0 ? `${head}（${elapsed}）：${parts.join(" · ")}` : `${head}（${elapsed}）`;
}

/**
 * 人工自检那一条提示（**成功与"什么都没做"都要说清**）。
 *
 * `audit_id === null` ⇒ 留痕没写进去。这不是可以静默的事：用户按这个按钮的前提是
 * "动作会被记下来"，这句话必须由面板说出来，不能指望他去翻日志。
 */
export function summarizeTick(result: WatchdogTickResult): string {
  const head = result.enabled ? result.note : "守护已停用（本轮没有做任何事）";
  const audit =
    result.audit_id === null ? "⚠️ 留痕未写入（查 API 日志）" : `留痕 #${result.audit_id}`;
  return `${head} · ${audit}`;
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const useOverviewStore = defineStore("overview", () => {
  const { status: wsStatus, cursor: wsCursor, connect, resync } = useWsConnection();

  // ── 健康（T4.1 保留）────────────────────────────────────────────────
  const health = ref<HealthResponse | null>(null);
  const healthError = ref<string | null>(null);
  const healthLoading = ref(false);
  const lastFetchAt = ref<string | null>(null);

  // ── 总览快照（T4.2）─────────────────────────────────────────────────
  const snapshot = ref<OverviewResponse | null>(null);
  const loadError = ref<string | null>(null);
  const loading = ref(false);

  // ── 动作（T4.2）────────────────────────────────────────────────────
  const busy = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);

  // ── 派生 ───────────────────────────────────────────────────────────
  const pools = computed<PoolStatus[]>(() => snapshot.value?.pools ?? []);
  const today = computed<TodayOutput | null>(() => snapshot.value?.today ?? null);
  const resources = computed<Resources | null>(() => snapshot.value?.resources ?? null);
  const services = computed<ServiceStatus[]>(() => snapshot.value?.services ?? []);
  const settings = computed<Settings | null>(() => snapshot.value?.settings ?? null);
  const resourcesAgeSec = computed<number | null>(() => snapshot.value?.resources_age_sec ?? null);
  const diskLow = computed<boolean | null>(() => snapshot.value?.disk_low ?? null);
  const workerAlive = computed<number>(() => snapshot.value?.worker_alive ?? 0);
  const workerTotal = computed<number>(() => snapshot.value?.worker_total ?? 0);
  const autoPolicy = computed<string>(() => settings.value?.auto_approve_policy ?? "off");
  // ── 无人值守（T4.11）───────────────────────────────────────────────
  const watchdog = computed<Watchdog | null>(() => snapshot.value?.watchdog ?? null);
  /** 人工池：**验收要求"可见"**，单独派生一次，面板直接画。 */
  const manualPool = computed(() => watchdog.value?.manual_pool ?? []);
  /** 停手的进程：面板要把"它已经不重启了"顶到最前面，而不是埋在表格里。 */
  const watchdogHalted = computed<string[]>(() => watchdog.value?.halted ?? []);
  const autoApproveOn = computed<boolean>(() => autoPolicy.value === "grade_ab");

  const wsTone = computed<StatusTone>(() => {
    switch (wsStatus.value) {
      case "open":
        return "ok";
      case "connecting":
        return "busy";
      case "reconnecting":
        return "warn";
      case "stopped":
        return "error";
      default:
        return "idle";
    }
  });
  const healthTone = computed<StatusTone>(() => {
    if (healthError.value) return "error";
    if (!health.value) return "idle";
    return health.value.ok ? "ok" : "warn";
  });
  /** 磁盘灯：**没采过就是没采过**（`idle`），不把"未知"画成"健康"。 */
  const diskTone = computed<StatusTone>(() => {
    if (diskLow.value === null) return "idle";
    return diskLow.value ? "error" : "ok";
  });
  /**
   * 守护灯。**停手优先于一切**：它表示"守护已经放弃重启这个进程"，比"守护没开"
   * 更该被人看见（前者是事故，后者是配置）。
   */
  const watchdogTone = computed<StatusTone>(() => {
    if (watchdog.value === null) return "idle";
    if (watchdogHalted.value.length > 0) return "error";
    if (manualPool.value.length > 0) return "warn";
    return watchdog.value.runnable ? "ok" : "idle";
  });

  let timer: ReturnType<typeof setInterval> | null = null;
  let wired = false;
  /** `pools` 一拍扇出 4 条 `pool.stats` + N 条心跳 ⇒ 窗口内只重拉一次。 */
  const poolCoalescer = createCoalescer(() => {
    void refresh();
  }, POOL_EVENT_COALESCE_MS);

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
  }

  async function refreshHealth(): Promise<void> {
    healthLoading.value = true;
    try {
      health.value = await api.fetchHealth();
      healthError.value = null;
      lastFetchAt.value = new Date().toISOString();
    } catch (failure) {
      healthError.value = describeError(failure);
    } finally {
      healthLoading.value = false;
    }
  }

  /** 重拉总览。失败**不清空**旧快照：把一屏数字换成空白，比留着旧数字更难判断。 */
  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      snapshot.value = await api.fetchOverview();
      loadError.value = null;
    } catch (failure) {
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
  }

  // ── WS ──────────────────────────────────────────────────────────────

  function onMetrics(envelope: Envelope): void {
    if (!isMetricsEvent(envelope.data.kind)) return;
    // 还没拿到快照就先不拼：`metrics.tick` 只带资源那一段，半路拼出来的是
    // "有资源、没池、没今日产量"的残帧 —— 等 `refresh()` 拿全更省事也更诚实。
    if (snapshot.value === null) return;
    snapshot.value = mergeMetrics(snapshot.value, envelope.data);
  }

  function onPools(envelope: Envelope): void {
    if (!isPoolEvent(envelope.data.kind)) return;
    poolCoalescer.schedule();
  }

  function wire(): void {
    if (wired) return;
    wired = true;
    useChannelStream("metrics", onMetrics);
    useChannelStream("pools", onPools);
    watch(wsStatus, (next, previous) => {
      // 断线期间的事件是**真的丢了**（服务端的环形缓冲是按连接发的），
      // 所以"重连成功"必须以一次全量重拉收尾，而不是拿旧快照接着拼。
      if (next === "open" && previous !== "open") void refresh();
    });
  }

  // ── 动作 ────────────────────────────────────────────────────────────

  /** 暂停 / 恢复一个池（写 `pool_settings`；**在途跑完**，不重启 worker · 裁定 138）。 */
  async function pausePool(
    pool: PoolName,
    paused: boolean,
    reason: string | null = null,
  ): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const result = await api.pausePool({ pool, paused, reason });
      notice.value = result.changed
        ? result.note
        : `「${poolLabel(pool)}」本来就在${paused ? "暂停" : "运行"}状态（没有改动）`;
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 切自动放行策略（写 `config/app.yaml` + 同步内存 + 留痕 · 裁定 139）。 */
  async function setAutoPolicy(policy: PolicyName, reason: string | null = null): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const result = await api.setAutoPolicy({ policy, reason });
      notice.value = result.changed
        ? `放行策略：${result.previous} → ${result.policy}（已写盘 ${result.config_path}）`
        : `放行策略本来就是 ${result.policy}（没有改动）`;
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 拉起五进程（**同步等就绪**，所以超时给到 90s）。 */
  async function startServices(): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const result = await api.startServices();
      notice.value = summarizeStart(result);
      await refresh();
      return result.ok;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * 立即自检（人工触发一轮守护 · §04.5.10）。
   *
   * 与其余动作同一条规矩：**先报结论再刷新**。刷新失败不该把"这一轮做了什么"吞掉
   * —— 那句话正是用户按这个按钮唯一想要的东西。
   */
  async function runWatchdogTick(reason: string | null = null): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const result = await api.runWatchdogTick({ reason });
      notice.value = summarizeTick(result);
      await refresh();
      return result.enabled;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：重复调用只会有一次轮询、一份订阅。 */
  function start(intervalMs: number = HEALTH_POLL_MS): void {
    connect();
    wire();
    if (timer !== null) return;
    void refreshHealth();
    void refresh();
    timer = setInterval(() => {
      void refreshHealth();
    }, intervalMs);
  }

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
    poolCoalescer.cancel();
  }

  return {
    // 健康
    health,
    healthError,
    healthLoading,
    healthTone,
    lastFetchAt,
    // 总览
    snapshot,
    pools,
    today,
    resources,
    services,
    settings,
    resourcesAgeSec,
    diskLow,
    diskTone,
    workerAlive,
    workerTotal,
    autoPolicy,
    autoApproveOn,
    // 无人值守（T4.11）
    watchdog,
    manualPool,
    watchdogHalted,
    watchdogTone,
    loading,
    loadError,
    // 动作
    busy,
    error,
    notice,
    pausePool,
    setAutoPolicy,
    startServices,
    runWatchdogTick,
    // 通道
    wsStatus,
    wsCursor,
    wsTone,
    refresh,
    refreshHealth,
    start,
    stop,
    resync,
  };
});
