// 无人值守守护 REST 面（T4.11 · §04.5.10）。
//
// 两个端点对应面板上的两件事：看（守护还活着吗 / 谁停手了 / 人工池有几条）、
// 立刻验证一轮（"立即自检"）。
//
// 为什么"立即自检"要单独给超时
// ---------------------------
// 一轮守护会**等进程就绪**（`ServiceManager.start()` 的 60s 预算就在这条路上）。
// 默认 10s 会在后端还在等的时候先把前端判死，用户看到"超时"于是再点一次 ——
// 正好撞上服务层的串行锁（第二次要排队）。给 30s 是"重启一个 worker 通常几秒"
// 与"别把用户晾在那里"之间的折中：真超时了，日志面板里那几行已经写下了。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type WatchdogStatus = OkJson<"/api/v1/watchdog", "get">;
export type WatchdogServiceGuard = WatchdogStatus["services"][number];
export type WatchdogDiskGate = WatchdogStatus["disk_gate"];
export type ManualPoolTask = WatchdogStatus["manual_pool"][number];
export type WatchdogTickResult = OkJson<"/api/v1/watchdog/tick", "post">;
export type WatchdogTickBody = BodyJson<"/api/v1/watchdog/tick", "post">;

/** "立即自检"的超时：一轮守护可能要等一个 worker 就绪。 */
export const TICK_TIMEOUT_MS = 30_000;

/** 守护当前状态（`enabled` / 自愈次数 / 停手进程 / 门禁 / 人工池）。 */
export function fetchWatchdog(signal?: AbortSignal): Promise<WatchdogStatus> {
  return apiGet<WatchdogStatus>("/api/v1/watchdog", { signal });
}

/** 人工触发一轮守护（**会改状态**，且留 `audit_ops`）。 */
export function runWatchdogTick(
  body: WatchdogTickBody = {},
  signal?: AbortSignal,
): Promise<WatchdogTickResult> {
  return apiPost<WatchdogTickResult>("/api/v1/watchdog/tick", body, {
    timeoutMs: TICK_TIMEOUT_MS,
    signal,
  });
}
