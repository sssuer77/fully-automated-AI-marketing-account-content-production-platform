// 报告与报告周期 REST 面（T5.7 · §04.6.5.2）。
//
// 十个端点对应面板上的十件事：列报告、看详情、手动生成、采纳建议、导出，
// 以及周期的列表 / 新建 / 改 / 删 / 立刻生成一次。
//
// 为什么导出走 `apiDownload` 而不是 `apiGet`
// ------------------------------------------
// 导出要的是**一个文件**（带 `Content-Disposition`），不是一段 JSON。走 `apiGet`
// 的话文件名要靠前端自己拼，而"拼出来的名字"和服务器实际写的那一份迟早分家 ——
// 用户拿到的是两个不同名字的同一天报告，而没有任何地方会报错。
//
// 为什么采纳建议只传 `report_id + index`
// --------------------------------------
// 采纳的语义是"我看了第 N 条，同意它"。传那句话本身的话，报告一旦重算
// （同周期不会重算，但换一个窗口就会），留痕里记的东西就对不上了 ——
// 而留痕的全部意义就是事后能对上。

import { apiDelete, apiDownload, apiGet, apiPatch, apiPost, type DownloadResult, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type ReportList = OkJson<"/api/v1/reports", "get">;
export type ReportSummary = NonNullable<ReportList["items"]>[number];
export type ReportDetail = OkJson<"/api/v1/reports/{report_id}", "get">;
export type ReportInsight = NonNullable<ReportDetail["insights"]>[number];
export type ReportGenerateBody = BodyJson<"/api/v1/reports/generate", "post">;
export type ReportApplyResult = OkJson<"/api/v1/reports/{report_id}/insights/{index}/apply", "post">;

export type ReportScheduleList = OkJson<"/api/v1/report-schedules", "get">;
export type ReportSchedule = NonNullable<ReportScheduleList["items"]>[number];
export type ReportScheduleBody = BodyJson<"/api/v1/report-schedules", "post">;
export type ReportSchedulePatchBody = BodyJson<"/api/v1/report-schedules/{schedule_id}", "patch">;
export type ReportScheduleDeleteResult = OkJson<"/api/v1/report-schedules/{schedule_id}", "delete">;
export type ReportScheduleRunResult = OkJson<
  "/api/v1/report-schedules/{schedule_id}/run_now",
  "post"
>;

const REPORTS_PATH = "/api/v1/reports";
const SCHEDULES_PATH = "/api/v1/report-schedules";

/** 报告列表（新到旧；`period` 可选过滤）。 */
export function fetchReports(
  params: { period?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<ReportList> {
  return apiGet<ReportList>(REPORTS_PATH, {
    query: { period: params.period ?? null, limit: params.limit ?? null },
    signal,
  });
}

/** 报告详情（正文 + 聚合数据 + 建议）。 */
export function fetchReport(reportId: string, signal?: AbortSignal): Promise<ReportDetail> {
  return apiGet<ReportDetail>(`${REPORTS_PATH}/${encodeURIComponent(reportId)}`, { signal });
}

/** 手动生成一份（同周期已存在 ⇒ 返回那一份，**不重复插入**）。 */
export function generateReport(
  body: ReportGenerateBody,
  signal?: AbortSignal,
): Promise<ReportDetail> {
  return apiPost<ReportDetail>(`${REPORTS_PATH}/generate`, body, { signal });
}

/** 采纳第 `index` 条建议（写进人物偏好 + 留痕；已采纳过的再点一次什么都不做）。 */
export function applyInsight(
  reportId: string,
  index: number,
  signal?: AbortSignal,
): Promise<ReportApplyResult> {
  return apiPost<ReportApplyResult>(
    `${REPORTS_PATH}/${encodeURIComponent(reportId)}/insights/${String(index)}/apply`,
    {},
    { signal },
  );
}

/** 导出（`md` 或 `csv`）—— 直接回文件，文件名由服务器给。 */
export function exportReport(
  reportId: string,
  format: "md" | "csv",
  signal?: AbortSignal,
): Promise<DownloadResult> {
  return apiDownload(`${REPORTS_PATH}/${encodeURIComponent(reportId)}/export`, {
    query: { format },
    signal,
  });
}

/** 全部报告周期（含 `next_run_at` / `last_result`）。 */
export function fetchReportSchedules(signal?: AbortSignal): Promise<ReportScheduleList> {
  return apiGet<ReportScheduleList>(SCHEDULES_PATH, { signal });
}

/** 新建一条周期（后端当场算好 `next_run_at` 落库）。 */
export function createReportSchedule(
  body: ReportScheduleBody,
  signal?: AbortSignal,
): Promise<ReportSchedule> {
  return apiPost<ReportSchedule>(SCHEDULES_PATH, body, { signal });
}

/** 改一条周期（**部分字段**：只给 `enabled` 就是启停）。 */
export function patchReportSchedule(
  scheduleId: string,
  body: ReportSchedulePatchBody,
  signal?: AbortSignal,
): Promise<ReportSchedule> {
  return apiPatch<ReportSchedule>(`${SCHEDULES_PATH}/${encodeURIComponent(scheduleId)}`, body, {
    signal,
  });
}

/** 删一条周期（内置的删不掉 ⇒ 400 说清"只能停用"）。 */
export function deleteReportSchedule(
  scheduleId: string,
  signal?: AbortSignal,
): Promise<ReportScheduleDeleteResult> {
  return apiDelete<ReportScheduleDeleteResult>(
    `${SCHEDULES_PATH}/${encodeURIComponent(scheduleId)}`,
    { signal },
  );
}

/** 立刻生成一次（`trigger='manual'`）。 */
export function runReportSchedule(
  scheduleId: string,
  reason?: string,
  signal?: AbortSignal,
): Promise<ReportScheduleRunResult> {
  return apiPost<ReportScheduleRunResult>(
    `${SCHEDULES_PATH}/${encodeURIComponent(scheduleId)}/run_now`,
    { reason: reason ?? null },
    { signal },
  );
}