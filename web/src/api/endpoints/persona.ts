// 人物库 REST 面（T4.13 · §02.4 / §04.1.6 / §04.5.8）。
//
// 五个端点对应面板上的五件事：看（GET /persona）、改（POST /persona）、切
// （activate）、存一份（save-as）、退回去（rollback）。
//
// 为什么没有"只校验"的端点
// ------------------------
// 保存本身就**先校验后落盘**（`PersonaStore.update`）。再开一个 `/validate`，等于同一套
// 判定跑在两条路径上 —— 两条路径的差异就是"面板说没问题、保存却 422"的来源。
// 表单的即时反馈用响应里的 `limits` 自己判长度即可（后端给的就是那一份上限）。
//
// 请求体类型不手写
// ----------------
// 与响应一样从 `types.gen.ts` 里取（`BodyJson`）—— 手写一份 `{ persona_id: string }`
// 就等于在"后端改了字段名"这件事上自愿放弃了编译期保护。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（同样从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type PersonaResponse = OkJson<"/api/v1/persona", "get">;
export type ActivePersona = NonNullable<PersonaResponse["active"]>;
export type PersonaConfig = ActivePersona["config"];
export type PersonaLibraryItem = PersonaResponse["library"][number];
export type PersonaBackup = PersonaResponse["backups"][number];
export type PersonaLimits = PersonaResponse["limits"];
export type PersonaOutcome = OkJson<"/api/v1/persona", "post">;
export type PersonaUpdateBody = BodyJson<"/api/v1/persona", "post">;
export type PersonaActivateBody = BodyJson<"/api/v1/persona/activate", "post">;
export type PersonaSaveAsBody = BodyJson<"/api/v1/persona/save-as", "post">;
export type PersonaRollbackBody = BodyJson<"/api/v1/persona/rollback", "post">;

/** 一次拿全：激活人物 + 人物库 + 备份 + 表单上下限。 */
export function fetchPersona(signal?: AbortSignal): Promise<PersonaResponse> {
  return apiGet<PersonaResponse>("/api/v1/persona", { signal });
}

/** 保存表单（**校验不过一个字节都不写**）。 */
export function updatePersona(
  body: PersonaUpdateBody,
  signal?: AbortSignal,
): Promise<PersonaOutcome> {
  return apiPost<PersonaOutcome>("/api/v1/persona", body, { signal });
}

/** 一键切换（旧版自动备份）。 */
export function activatePersona(
  body: PersonaActivateBody,
  signal?: AbortSignal,
): Promise<PersonaOutcome> {
  return apiPost<PersonaOutcome>("/api/v1/persona/activate", body, { signal });
}

/** 把当前激活人物存进人物库。 */
export function saveAsPersona(
  body: PersonaSaveAsBody,
  signal?: AbortSignal,
): Promise<PersonaOutcome> {
  return apiPost<PersonaOutcome>("/api/v1/persona/save-as", body, { signal });
}

/** 回滚到某份备份（**先备份当前** ⇒ 回滚本身也可以再回滚）。 */
export function rollbackPersona(
  body: PersonaRollbackBody,
  signal?: AbortSignal,
): Promise<PersonaOutcome> {
  return apiPost<PersonaOutcome>("/api/v1/persona/rollback", body, { signal });
}
