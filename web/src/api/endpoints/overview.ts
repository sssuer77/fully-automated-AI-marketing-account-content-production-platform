// 总览台 REST 面（T4.2 · §04.4.5 第 1 行）。
//
// 四个端点对应面板上的四件事：看（GET /overview）、暂停/恢复一个池、
// 切自动放行策略（一键全自动）、拉起五进程。
//
// 为什么"启动"要单独给 90s 超时
// ----------------------------
// `POST /overview/start` 是**同步等就绪**的（`ServiceManager.start()` 逐进程等到
// ready，上限 60s）。默认 10s 超时会在后端还在等的时候先把前端判死：用户看到
// "超时"，于是再点一次 —— 正好撞上后端的单飞守卫（409 `SERVICE_START_BUSY`）。
// 给足 90s 比"假装很快"更诚实（与选题面板的长任务同一取舍）。
//
// 请求体类型不手写
// ----------------
// 与响应一样从 `types.gen.ts` 里取（`BodyJson`）—— 手写一份 `{ pool: string }`
// 就等于在"后端改了字段名"这件事上自愿放弃了编译期保护。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** "启动五进程"的超时：后端逐进程等就绪（上限 60s），给足余量。 */
export const START_TIMEOUT_MS = 90_000;

/** 某个操作的**请求体**类型（同样从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type OverviewResponse = OkJson<"/api/v1/overview", "get">;
export type PoolStatus = OverviewResponse["pools"][number];
export type WorkerStatus = PoolStatus["workers"][number];
export type TodayOutput = OverviewResponse["today"];
export type Resources = NonNullable<OverviewResponse["resources"]>;
export type Settings = OverviewResponse["settings"];
export type ServiceStatus = OverviewResponse["services"][number];
/** 无人值守那一段（T4.11）。`null` ⇒ 这套依赖没接守护（极简家目录）。 */
export type Watchdog = NonNullable<OverviewResponse["watchdog"]>;
export type ManualPoolTask = Watchdog["manual_pool"][number];
export type PoolPauseResult = OkJson<"/api/v1/overview/pools", "post">;
export type PolicyResult = OkJson<"/api/v1/overview/auto", "post">;
export type StartResult = OkJson<"/api/v1/overview/start", "post">;
export type PoolPauseBody = BodyJson<"/api/v1/overview/pools", "post">;
export type PolicyBody = BodyJson<"/api/v1/overview/auto", "post">;

/** 总览台一次拿全：四池 + 今日产量 + 资源 + 服务就绪 + 当前策略。 */
export function fetchOverview(signal?: AbortSignal): Promise<OverviewResponse> {
  return apiGet<OverviewResponse>("/api/v1/overview", { signal });
}

/** 暂停 / 恢复一个池（`paused` 是**目标状态**，不是"切换"）。 */
export function pausePool(body: PoolPauseBody, signal?: AbortSignal): Promise<PoolPauseResult> {
  return apiPost<PoolPauseResult>("/api/v1/overview/pools", body, { signal });
}

/** 切自动放行策略（`grade_ab` = 一键全自动；`grade_a` / `off` = 回退）。 */
export function setAutoPolicy(body: PolicyBody, signal?: AbortSignal): Promise<PolicyResult> {
  return apiPost<PolicyResult>("/api/v1/overview/auto", body, { signal });
}

/** 拉起五进程（后端**不**开浏览器、**要**过 doctor 门禁 · 裁定 137）。 */
export function startServices(signal?: AbortSignal): Promise<StartResult> {
  return apiPost<StartResult>("/api/v1/overview/start", {}, { timeoutMs: START_TIMEOUT_MS, signal });
}
