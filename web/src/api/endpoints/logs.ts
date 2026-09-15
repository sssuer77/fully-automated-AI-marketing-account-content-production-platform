// `GET /api/v1/logs`（T1.7 分页 · T4.9 过滤/搜索/导出）。

import { apiDownload, apiGet, type DownloadResult, type OkJson } from "../http";

export type LogPage = OkJson<"/api/v1/logs", "get">;

/** 列表与导出**共用**同一套过滤参数 —— 两条通道的语义必须一致（§04.5.2）。 */
export interface LogQuery {
  since_id?: number | null;
  until_id?: number | null;
  limit?: number;
  level?: string | null;
  task_id?: string | null;
  source?: string | null;
  search?: string | null;
}

export function fetchLogs(query: LogQuery = {}, signal?: AbortSignal): Promise<LogPage> {
  return apiGet<LogPage>("/api/v1/logs", { query: { ...query }, signal });
}

/** 导出 NDJSON（流式响应；`rows === limit` ⇒ 大概率被截断）。 */
export function exportLogs(query: LogQuery = {}, signal?: AbortSignal): Promise<DownloadResult> {
  return apiDownload("/api/v1/logs/export", { query: { ...query }, signal });
}
