// 四池调度控制台状态（T4.10 · §04.4.5 / §03.4.4）。
//
// 这一屏要回答四个问题
// --------------------
// ① 四个池各自什么配置、现在什么状态？② 把并发调一下？③ 暂停 / 恢复？
// ④ 死信重投 + "机器是不是在偷偷降我的并发"。
//
// 为什么"配置值"和"运行值"都要留着
// --------------------------------
// `pools.yaml` 的 `concurrency` 与 `pool_settings.concurrency` 是**两个数**：
// 前者是出厂值，后者是当前值（DB 值优先）。只显示一个，就会出现"我明明把 YAML
// 改成 3 了，面板还是 1"这种查不完的悬案 —— 两个并排摆出来，差异一眼可见。
//
// 为什么 WS 到达是**重拉**而不是合并
// --------------------------------
// 与总览台同一条理由：`pool.stats` 是 `PoolConsole` 的真子集（没有 `priority` /
// `consecutive_oom` / `dead_letters` / `config_error`），在前端拼等于维护第二份
// 卡片形状，迟早与后端分叉。它本来就只有 1 Hz，合并窗口内重拉一次远比抄契约便宜。
//
// 为什么**没有**兜底轮询
// ---------------------
// 与总览台一致：顶栏的 WS 灯就是"这屏还动不动"的答案，15s 一次的兜底轮询会喂出
// 一个"看起来在动、其实是旧数据"的假象。重连成功时 store 主动重拉一次补缺口。
//
// 三个错误位分开放（裁定 71）
// --------------------------
// `loadError`（拉取）/ `error`（动作）/ `configError`（后端读配置失败）。
// 混成一个的后果是：一次拉取失败会把"我刚调的并发到底成没成"这句话吞掉。
//
// 为什么池名与中文标签从 `stores/overview.ts` 借
// ---------------------------------------------
// 那是**共享词表**（四个池的名字与中文），抄一份出来两边就会分叉：总览台叫"配音"、
// 四池面板叫"语音"。同理复用它的 `describeError` / `isPoolEvent` / `createCoalescer`
// 三个纯函数 —— 它们是同一套语义，复制第三份只是把同一处 bug 修三遍（裁定 156）。

import { defineStore } from "pinia";
import { computed, ref, watch } from "vue";

import { pausePool as pausePoolRequest } from "@/api/endpoints/overview";
import {
  fetchPools,
  requeueDead as requeueDeadRequest,
  setConcurrency as setConcurrencyRequest,
  type ConcurrencyResult,
  type DeadLetter,
  type PoolConsole,
  type PoolsResponse,
  type RequeueResult,
} from "@/api/endpoints/pools";
import { useChannelStream } from "@/composables/useTaskStream";
import { useWsConnection } from "@/composables/useWsConnection";
import {
  createCoalescer,
  describeError,
  isPoolEvent,
  poolLabel,
  type PoolName,
} from "@/stores/overview";
import type { StatusTone } from "@/components/tone";
import type { Envelope } from "@/ws/events";

/** `pools` 通道的合并窗口：`pool.stats` 有 1 Hz，一拍还会扇出四条（四个池）。 */
export const POOL_EVENT_COALESCE_MS = 500;

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface PoolsApi {
  fetchPools: typeof fetchPools;
  setConcurrency: typeof setConcurrencyRequest;
  requeueDead: typeof requeueDeadRequest;
  pausePool: typeof pausePoolRequest;
}

let api: PoolsApi = {
  fetchPools,
  setConcurrency: setConcurrencyRequest,
  requeueDead: requeueDeadRequest,
  pausePool: pausePoolRequest,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configurePoolsApi(overrides: Partial<PoolsApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 还能往上调吗（顶到**工程上限**时面板要说清楚"再往上没有意义"）。 */
export function canRaise(card: PoolConsole): boolean {
  return card.concurrency < card.concurrency_max;
}

/** 还能往下调吗（到下限时"再降"等于沉默的暂停，见裁定 145）。 */
export function canLower(card: PoolConsole): boolean {
  return card.concurrency > card.concurrency_min;
}

/** 这个池最近是不是装不下当前并发（计过 OOM 数就该有人看一眼）。 */
export function needsAttention(card: PoolConsole): boolean {
  return card.consecutive_oom > 0;
}

/** 池卡片的状态灯（`error` 优先：参数缺失时其余数字都不可信）。 */
export function poolTone(card: PoolConsole): StatusTone {
  if (card.error) return "error";
  if (card.paused) return "warn";
  if (card.running > 0) return "busy";
  return card.pending > 0 ? "warn" : "idle";
}

/** 池卡片的状态文案（与总览台同一口径，"有活没人认领"要显式说出来）。 */
export function poolState(card: PoolConsole): string {
  if (card.error) return "参数缺失";
  if (card.paused) return card.paused_by ? `已暂停（${card.paused_by}）` : "已暂停";
  if (card.running > 0) return `在跑 ${card.running}`;
  if (card.pending > 0) return "待认领";
  return "空闲";
}

/** 重投结论的一句话小结（**成功与失败都要出现**，不能只说"没全进去"）。 */
export function summarizeRequeue(result: RequeueResult): string {
  const parts = [`重投 ${result.requeued.length}/${result.requested} 条`];
  if (result.failed.length > 0) {
    const detail = result.failed
      .map((item) => `${item.job_id}（${item.message}）`)
      .join("；");
    parts.push(`没进去的：${detail}`);
  }
  return parts.join(" · ");
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const usePoolsStore = defineStore("pools", () => {
  const { status: wsStatus, cursor: wsCursor, connect, resync } = useWsConnection();

  const snapshot = ref<PoolsResponse | null>(null);
  const loading = ref(false);
  const loadError = ref<string | null>(null);

  const busy = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);

  // ── 派生 ───────────────────────────────────────────────────────────
  const pools = computed<PoolConsole[]>(() => snapshot.value?.pools ?? []);
  const autoDegradeEnabled = computed<boolean>(() => snapshot.value?.auto_degrade_enabled ?? false);
  const oomThreshold = computed<number>(() => snapshot.value?.oom_threshold ?? 0);
  const configError = computed<string | null>(() => snapshot.value?.config_error ?? null);
  const configPath = computed<string>(() => snapshot.value?.config_path ?? "");
  const deadTotal = computed<number>(() =>
    pools.value.reduce((sum, card) => sum + card.dead, 0),
  );
  const degraded = computed<PoolConsole[]>(() => pools.value.filter(needsAttention));
  const backlogTotal = computed<number>(() =>
    pools.value.reduce((sum, card) => sum + card.backlog, 0),
  );

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

  // ── 读 ──────────────────────────────────────────────────────────────

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
  }

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      snapshot.value = await api.fetchPools();
      loadError.value = null;
    } catch (failure) {
      // 旧快照**留着**：一屏数字不因一次抖动变空白（与总览台同一条）。
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
  }

  // ── WS ──────────────────────────────────────────────────────────────

  const poolCoalescer = createCoalescer(() => {
    void refresh();
  }, POOL_EVENT_COALESCE_MS);

  function onPools(envelope: Envelope): void {
    if (!isPoolEvent(envelope.data.kind)) return;
    poolCoalescer.schedule();
  }

  let wired = false;
  function wire(): void {
    if (wired) return;
    wired = true;
    useChannelStream("pools", onPools);
    watch(wsStatus, (next, previous) => {
      // 断线期间的事件是**真的丢了**（服务端的环形缓冲是按连接发的），
      // 所以"重连成功"必须以一次全量重拉收尾。
      if (next === "open" && previous !== "open") void refresh();
    });
  }

  // ── 动作 ────────────────────────────────────────────────────────────

  /** 调并发（写 `pool_settings` + `audit_ops`，**生效无需重启** · §03.4.4）。 */
  async function setConcurrency(
    pool: PoolName,
    concurrency: number,
    reason: string | null = null,
  ): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const result: ConcurrencyResult = await api.setConcurrency({ pool, concurrency, reason });
      notice.value = `「${poolLabel(pool)}」${result.note}`;
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 暂停 / 恢复（走总览台那个写入口 —— 同一件事只有一个权威落点 · 裁定 144）。 */
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

  /** 批量重投死信（逐条独立：一条不是死信不影响其余）。 */
  async function requeueDead(jobIds: string[], reason: string | null = null): Promise<boolean> {
    if (jobIds.length === 0) return false;
    busy.value = true;
    clearMessages();
    try {
      const result: RequeueResult = await api.requeueDead({ job_ids: jobIds, reason });
      notice.value = summarizeRequeue(result);
      await refresh();
      return result.failed.length === 0;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 一条死信的重投（UI 上按单条点的那颗按钮）。 */
  function requeueOne(letter: DeadLetter, reason: string | null = null): Promise<boolean> {
    return requeueDead([letter.job_id], reason);
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：重复调用只会有一次订阅、一份合并窗口。 */
  function start(): void {
    connect();
    wire();
    void refresh();
  }

  function stop(): void {
    poolCoalescer.cancel();
  }

  return {
    // 读
    snapshot,
    pools,
    loading,
    loadError,
    configError,
    configPath,
    autoDegradeEnabled,
    oomThreshold,
    deadTotal,
    backlogTotal,
    degraded,
    // 动作
    busy,
    error,
    notice,
    setConcurrency,
    pausePool,
    requeueDead,
    requeueOne,
    // 通道
    wsStatus,
    wsCursor,
    wsTone,
    refresh,
    start,
    stop,
    resync,
  };
});
