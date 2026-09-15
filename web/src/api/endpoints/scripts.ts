// 稿件面板 REST 面（T4.4 · §04.4.5 第 3 行）。
//
// 三个端点对应面板的三块：正文 + 评分（detail）、版本下拉（versions）、
// "这一版到底改了什么"（diff）。

import { apiGet, type OkJson } from "../http";

export type ScriptDetail = OkJson<"/api/v1/scripts/{task_id}", "get">;
export type ScriptBody = ScriptDetail["script"];
export type ScriptSentence = ScriptDetail["sentences"][number];
export type ScriptReview = ScriptDetail["reviews"][number];
export type ScriptApproval = NonNullable<ScriptDetail["approval"]>;
export type ScriptVersionList = OkJson<"/api/v1/scripts/{task_id}/versions", "get">;
export type ScriptVersion = ScriptVersionList["versions"][number];
export type ScriptDiff = OkJson<"/api/v1/scripts/{task_id}/diff", "get">;
export type SentenceDiffRow = ScriptDiff["changes"][number];

export function fetchScript(taskId: string, signal?: AbortSignal): Promise<ScriptDetail> {
  return apiGet<ScriptDetail>(`/api/v1/scripts/${taskId}`, { signal });
}

export function fetchScriptVersions(
  taskId: string,
  signal?: AbortSignal,
): Promise<ScriptVersionList> {
  return apiGet<ScriptVersionList>(`/api/v1/scripts/${taskId}/versions`, { signal });
}

/** 两个版本号都**必须**给：默认值会让人对着错误的对照下结论。 */
export function fetchScriptDiff(
  taskId: string,
  fromVersion: number,
  toVersion: number,
  signal?: AbortSignal,
): Promise<ScriptDiff> {
  return apiGet<ScriptDiff>(`/api/v1/scripts/${taskId}/diff`, {
    query: { from_version: fromVersion, to_version: toVersion },
    signal,
  });
}
