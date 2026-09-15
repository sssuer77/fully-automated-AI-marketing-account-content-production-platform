// 确认闸 REST 面（T4.4 · §04.4.4）—— 全流程唯一的人工节点。
//
// 六条动作一一对应后端的六条路径；参数名刻意与后端**逐字一致**（`comment` /
// `reason` / `task_ids`），改后端时 `types.gen.ts` 会先炸，而不是等到运行时 422。

import { apiGet, apiPost, type OkJson } from "../http";

export type ApprovalList = OkJson<"/api/v1/approvals", "get">;
export type ApprovalItem = ApprovalList["approvals"][number];
export type ApprovalResult = OkJson<"/api/v1/tasks/{task_id}/approve", "post">;
export type BatchResult = OkJson<"/api/v1/approvals/approve_batch", "post">;
export type BatchFailure = BatchResult["failed"][number];
export type RescueResult = OkJson<"/api/v1/tasks/{task_id}/rescue", "post">;

export function fetchApprovals(
  status: string = "pending",
  signal?: AbortSignal,
): Promise<ApprovalList> {
  return apiGet<ApprovalList>("/api/v1/approvals", { query: { status }, signal });
}

/** 确认放行（`awaiting_approval → queued_voice`）。 */
export function approveTask(
  taskId: string,
  comment?: string,
  signal?: AbortSignal,
): Promise<ApprovalResult> {
  return apiPost<ApprovalResult>(`/api/v1/tasks/${taskId}/approve`, { comment }, { signal });
}

/** 退回改稿（`awaiting_approval → editing`）。`comment` **必填**。 */
export function rejectTask(
  taskId: string,
  comment: string,
  signal?: AbortSignal,
): Promise<ApprovalResult> {
  return apiPost<ApprovalResult>(`/api/v1/tasks/${taskId}/reject`, { comment }, { signal });
}

/** 放弃（`awaiting_approval → discarded`）。误点后可经 `rescueTask` 捞回。 */
export function discardTask(
  taskId: string,
  reason?: string,
  signal?: AbortSignal,
): Promise<ApprovalResult> {
  return apiPost<ApprovalResult>(`/api/v1/tasks/${taskId}/discard`, { reason }, { signal });
}

/** 批量通过：**逐条**决断，部分失败不回滚。 */
export function approveBatch(
  taskIds: readonly string[],
  comment?: string,
  signal?: AbortSignal,
): Promise<BatchResult> {
  return apiPost<BatchResult>(
    "/api/v1/approvals/approve_batch",
    { task_ids: [...taskIds], comment },
    { signal },
  );
}

/** 捞回（`discarded` / `canceled` → `pending`）。 */
export function rescueTask(
  taskId: string,
  reason?: string,
  signal?: AbortSignal,
): Promise<RescueResult> {
  return apiPost<RescueResult>(`/api/v1/tasks/${taskId}/rescue`, { reason }, { signal });
}
