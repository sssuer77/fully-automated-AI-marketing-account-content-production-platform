// 定时发布计划 REST 面（T5.6 · §04.6.5.1）。
//
// 五个端点对应面板上的五件事：列出计划、新建、改（含启停）、删、立刻跑一次。
//
// 为什么「立刻跑一次」不走一个新函数
// --------------------------------
// 后端那一下与到点触发走的是**同一条** `fire()` —— 所以它会照样看发布总开关、
// 照样过限频。前端这边也一样：`runSchedule` 只是把同一个端点包一层，
// 不在本地做任何"调试模式就跳过检查"的判断（那种判断会让面板上的行为和
// 后台的行为分叉，而分叉只会在真出事那天被发现）。
//
// 请求体类型不手写
// ----------------
// 与其余端点文件同一条：从 `types.gen.ts` 里取。手写一份形状，等于在
// 「后端改了字段名」这件事上自愿放弃了编译期保护。

import { apiDelete, apiGet, apiPatch, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type ScheduleList = OkJson<"/api/v1/schedules", "get">;
export type Schedule = NonNullable<ScheduleList["items"]>[number];
export type ScheduleBody = BodyJson<"/api/v1/schedules", "post">;
export type SchedulePatchBody = BodyJson<"/api/v1/schedules/{schedule_id}", "patch">;
export type ScheduleDeleteResult = OkJson<"/api/v1/schedules/{schedule_id}", "delete">;
export type ScheduleRunResult = OkJson<"/api/v1/schedules/{schedule_id}/run_now", "post">;

const SCHEDULES_PATH = "/api/v1/schedules";

/** 全部计划 + 计数 + 能选的平台（**平台清单来自配置，不是面板自己列的**）。 */
export function fetchSchedules(signal?: AbortSignal): Promise<ScheduleList> {
  return apiGet<ScheduleList>(SCHEDULES_PATH, { signal });
}

/** 新建一条计划（后端会当场算好 `next_run_at` 落库）。 */
export function createSchedule(
  body: ScheduleBody,
  signal?: AbortSignal,
): Promise<Schedule> {
  return apiPost<Schedule>(SCHEDULES_PATH, body, { signal });
}

/** 改一条计划（**部分字段**：只给 `enabled` 就是启停）。 */
export function patchSchedule(
  scheduleId: string,
  body: SchedulePatchBody,
  signal?: AbortSignal,
): Promise<Schedule> {
  return apiPatch<Schedule>(`${SCHEDULES_PATH}/${encodeURIComponent(scheduleId)}`, body, {
    signal,
  });
}

/** 删一条计划（`deleted=false` ⇒ 它本来就不在，不是错误）。 */
export function deleteSchedule(
  scheduleId: string,
  signal?: AbortSignal,
): Promise<ScheduleDeleteResult> {
  return apiDelete<ScheduleDeleteResult>(`${SCHEDULES_PATH}/${encodeURIComponent(scheduleId)}`, {
    signal,
  });
}

/** 立刻执行一次（**仍走限频、仍看发布总开关** —— 与到点触发同一条路径）。 */
export function runSchedule(
  scheduleId: string,
  reason?: string,
  signal?: AbortSignal,
): Promise<ScheduleRunResult> {
  return apiPost<ScheduleRunResult>(
    `${SCHEDULES_PATH}/${encodeURIComponent(scheduleId)}/run_now`,
    { reason: reason ?? null },
    { signal },
  );
}