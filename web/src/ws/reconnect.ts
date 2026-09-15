// WS 重连与补发策略（T4.1 · §04.4.6）—— **纯函数**，无副作用、可单测。
//
// 三条契约（规格书原文，别改）：
// 1. 退避：1s → 2s → 4s → 8s，**上限 15s**，之后恒为 15s（永不放弃重连）。
// 2. 重连必须带游标 `since_id`（最后一条日志 id）：服务端据此**补发**，而不是重发全量。
// 3. `seq` 是**每连接**单调（§04.4.2）：`seq !== last + 1` ⇒ 发 `{"type":"resync"}`。
//
// 为什么单独一个文件：这些是唯一值得单测的部分（`npm run test`）；
// `client.ts` 只做"把 socket 事件接到这些纯函数上"的薄胶水。

import { WS_PATH, type Channel, type Envelope } from "./events";

/** 退避阶梯（毫秒）：第 1 次重连等 1s，第 5 次及以后恒等 15s。 */
export const BACKOFF_STEPS_MS = [1000, 2000, 4000, 8000, 15000] as const;

/** 退避上限（§04.4.6）：**不是**放弃阈值 —— 永远重连，只是不再变慢。 */
export const MAX_BACKOFF_MS = 15000;

/** 心跳间隔；一个周期内没等到 `pong` ⇒ 判定连接已死，主动断开触发重连。 */
export const PING_INTERVAL_MS = 20000;

/** 客户端主动收工。 */
export const WS_CLOSE_NORMAL = 1000;
/** 客户端主动关闭：pong 超时（TCP 半开时 `onclose` 不会自己来）。 */
export const WS_CLOSE_PONG_TIMEOUT = 4000;
/** 服务端背压断开码（§04.4.6）：语义是"你太慢"，重连即可，不算故障。 */
export const WS_CLOSE_SLOW = 1008;

/** 连接状态（状态栏据此上色）。 */
export type WsStatus = "idle" | "connecting" | "open" | "reconnecting" | "stopped";

/** 第 `attempt` 次重连的等待毫秒数（`attempt` 从 1 开始）。 */
export function backoffDelay(attempt: number): number {
  if (!Number.isFinite(attempt) || attempt < 1) return BACKOFF_STEPS_MS[0];
  const index = Math.min(Math.floor(attempt), BACKOFF_STEPS_MS.length) - 1;
  return BACKOFF_STEPS_MS[index];
}

/**
 * `seq` 缺口判定（§04.4.2）。
 *
 * `lastSeq <= 0` 表示"本连接还没收到过帧"（新连接的 `seq` 从 1 重新开始）⇒
 * 用第一帧建立基线，**不算**缺口。否则必须严格 `+1`。
 */
export function detectGap(lastSeq: number, seq: number): boolean {
  if (lastSeq <= 0) return false;
  return seq !== lastSeq + 1;
}

export interface WsUrlOptions {
  /** 页面 origin（`http(s)://host:port`）；生产传 `window.location.origin`。 */
  origin: string;
  channels?: readonly Channel[];
  taskIds?: readonly string[];
  minLevel?: string;
  /** 最后一条日志 id；`null`/`undefined` ⇒ 不带该参数（服务端发最近快照）。 */
  sinceId?: number | null;
  token?: string | null;
}

/** 拼握手 URL：`ws(s)://host/ws/ui?channels=…&task_ids=…&min_level=…&since_id=…`。 */
export function buildWsUrl(options: WsUrlOptions): string {
  const base = options.origin.replace(/^http/, "ws").replace(/\/+$/, "");
  const params = new URLSearchParams();
  if (options.channels && options.channels.length > 0) {
    params.set("channels", options.channels.join(","));
  }
  if (options.taskIds && options.taskIds.length > 0) {
    params.set("task_ids", options.taskIds.join(","));
  }
  if (options.minLevel) params.set("min_level", options.minLevel);
  // 只跳过 null/undefined：`since_id=0` 在服务端是"从头补发"这条**独立分支**，
  // 顺手把它当空值过滤掉会静默改变语义。
  if (options.sinceId !== null && options.sinceId !== undefined) {
    params.set("since_id", String(options.sinceId));
  }
  if (options.token) params.set("token", options.token);
  const query = params.toString();
  return `${base}${WS_PATH}${query ? `?${query}` : ""}`;
}

/** 取一个可能是日志 id 的值（脏值一律当"没有"，绝不写入游标）。 */
export function asLogId(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 信封形状校验：服务端可信，但坏帧不该让前端状态机跑飞。 */
export function isEnvelope(value: unknown): value is Envelope {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate["type"] === "string" &&
    typeof candidate["channel"] === "string" &&
    typeof candidate["seq"] === "number" &&
    typeof candidate["data"] === "object" &&
    candidate["data"] !== null
  );
}
