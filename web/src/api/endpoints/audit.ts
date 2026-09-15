// 审计页 REST 面（T4.12 · §04.5.11）。
//
// 两个端点对应面板上的两件事：看（按筛选翻页）、拿筛选项的可选值。
//
// 为什么这一屏**没有**写动作
// ------------------------
// 审计是"别人写、这里读"：写入口散在确认闸 / 池启停 / 策略切换 / 人物库各处
// （§04.4.4 不变量 2：改动了别人能看到的东西就要留痕）。这里开一个写入口，等于
// 给"伪造留痕"开条路 —— 后端也没给（`routers/audit.py` 只有两个 GET）。
//
// 为什么筛选项要单独问一次
// ----------------------
// "按操作人筛"得先知道有哪几个操作人，让人手打 id 是另一种形式的"查不到"；
// 而把 facets 塞进列表响应里，等于每翻一页重算四次 `SELECT DISTINCT`。
//
// 响应类型不手写
// --------------
// 一律从 `types.gen.ts` 里取（`OkJson`）—— 手写一份 `{ actor: string }` 就等于在
// "后端改了字段名"这件事上自愿放弃编译期保护。

import { apiGet, type OkJson } from "../http";

export type AuditPage = OkJson<"/api/v1/audit", "get">;
export type AuditOp = AuditPage["items"][number];
export type AuditFacets = OkJson<"/api/v1/audit/facets", "get">;

/** 单页上限（与后端 `routers/audit.py::MAX_LIMIT` 同源；超了后端直接 422）。 */
export const AUDIT_MAX_LIMIT = 500;

/** 默认页大小：审计行带 `before` / `after`，比日志行重得多。 */
export const AUDIT_PAGE_SIZE = 50;

/** 列表筛选参数（与后端那几个 `Query(...)` 逐字对应）。 */
export interface AuditQuery {
  task_id?: string | null;
  actor?: string | null;
  action?: string | null;
  target_type?: string | null;
  result?: string | null;
  since?: string | null;
  limit?: number;
  offset?: number;
}

/** 翻一页留痕（新→旧）。`total` 是**同一组筛选下**的总数，不是全表总数。 */
export function fetchAudit(query: AuditQuery = {}, signal?: AbortSignal): Promise<AuditPage> {
  return apiGet<AuditPage>("/api/v1/audit", { query: { ...query }, signal });
}

/** 筛选下拉的取值（动作 / 对象 / 操作人 / 结果）。 */
export function fetchAuditFacets(signal?: AbortSignal): Promise<AuditFacets> {
  return apiGet<AuditFacets>("/api/v1/audit/facets", { signal });
}
