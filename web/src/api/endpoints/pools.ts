// 四池调度控制台 REST 面（T4.10 · §04.4.5 / §03.4.4）。
//
// 三个端点对应面板上的三件事：看（GET /pools）、调并发、重投死信。
//
// 为什么暂停 / 恢复**不在这里**
// ----------------------------
// 它已经在 `endpoints/overview.ts`（`POST /api/v1/overview/pools`，T4.2 落地）。
// 同一件事两条写路径，审计 / 事件 / 幂等判据迟早分叉（T4.10 裁定 144）。
// 四池面板是它的**第二个视图**，不是第二个入口。
//
// 请求体类型不手写
// ----------------
// 与响应一样从 `types.gen.ts` 里取（`BodyJson`）—— 手写一份 `{ pool: string }`
// 就等于在"后端改了字段名"这件事上自愿放弃了编译期保护。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（同样从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type PoolsResponse = OkJson<"/api/v1/pools", "get">;
export type PoolConsole = PoolsResponse["pools"][number];
export type DeadLetter = PoolConsole["dead_letters"][number];
export type ConcurrencyResult = OkJson<"/api/v1/pools/concurrency", "post">;
export type RequeueResult = OkJson<"/api/v1/pools/requeue", "post">;
export type ConcurrencyBody = BodyJson<"/api/v1/pools/concurrency", "post">;
export type RequeueBody = BodyJson<"/api/v1/pools/requeue", "post">;

/** 四池一次拿全：配置值 / 运行值 / 状态 / 死信。 */
export function fetchPools(signal?: AbortSignal): Promise<PoolsResponse> {
  return apiGet<PoolsResponse>("/api/v1/pools", { signal });
}

/** 调一个池的并发（`concurrency` 是**目标值**；写 DB，生效无需重启）。 */
export function setConcurrency(
  body: ConcurrencyBody,
  signal?: AbortSignal,
): Promise<ConcurrencyResult> {
  return apiPost<ConcurrencyResult>("/api/v1/pools/concurrency", body, { signal });
}

/** 批量重投死信（逐条独立，部分失败不回滚）。 */
export function requeueDead(body: RequeueBody, signal?: AbortSignal): Promise<RequeueResult> {
  return apiPost<RequeueResult>("/api/v1/pools/requeue", body, { signal });
}
