// 观测面板 REST 面（T4.12 · §04.5.11）。
//
// 一个端点，一次拿全（六块：资源 / 队列 / 产量 / 进程 / 备份 / 存储）。
//
// 为什么给 30s 超时
// ----------------
// 存储体检是**递归统计文件**（TTS 缓存 / 热点归档 / tmp），不是一次 `stat`：
// 缓存里攒了十万个小文件时，默认的 10s 会在后端还在数的时候先把前端判死 ——
// 用户看到的是"面板打不开"，而不是"文件有点多"。这一屏不是首屏，慢一点没关系。
//
// 为什么**没有**写动作
// -------------------
// GC / 备份 / VACUUM 都是 CLI 或计划任务的事（`studio gc run` / `studio db backup`）。
// 做成按钮，等于给"删数据"这个动作开一个只隔一次点击的入口。

import { apiGet, type OkJson } from "../http";

export type MetricsResponse = OkJson<"/api/v1/metrics", "get">;
export type PoolMetric = MetricsResponse["pools"][number];
export type BackupHealth = MetricsResponse["backups"];
export type StorageHealth = MetricsResponse["storage"];

/** 存储体检要递归数文件 ⇒ 给它比"问一句状态"更宽的预算。 */
export const METRICS_TIMEOUT_MS = 30_000;

/** 观测面板一次拿全（只读：这一屏一个写动作都没有）。 */
export function fetchMetrics(signal?: AbortSignal): Promise<MetricsResponse> {
  return apiGet<MetricsResponse>("/api/v1/metrics", { timeoutMs: METRICS_TIMEOUT_MS, signal });
}
