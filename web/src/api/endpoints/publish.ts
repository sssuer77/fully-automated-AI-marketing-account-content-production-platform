// 发布面板 REST 面（T5.5 · §06.11 / §06.12）。
//
// 十二个端点对应面板上的十二件事：看板、待人工队列、投递、人工处置三连、交付包预览 / 导出、
// R2 合规留档、数据回收三连（跑一轮 / 采这一条 / 沉淀这一条）。
//
// 为什么「数据回收」是三个端点而不是一个
// --------------------------------------
// §4.6.2 把它们分成两个入口，加上"整轮"就是三件不同的事：**轮询**（谁到点了）、
// **采一条**（现在就要这个数）、**沉淀一条**（让下一轮选题吃得到）。合并成一个的代价是
// "我想再沉淀一次"必须连带再采一次数，而那次采集可能正好撞上平台限流。
//
// 为什么「待人工」单独一个端点
// --------------------------
// 它是唯一一个**要求人做决定**的列表（§06.10），其余区块是"看"。分开之后面板可以只轮询
// 它、只给它做顶部计数，而不必每次都拉全量记录 —— 全量那一条在几百条记录时会明显变慢。
//
// 为什么 `can_retry` / `can_cancel` / `can_mark_done` 不在这里算
// -------------------------------------------------------------
// 判据是**服务端**的状态机规则（`published` 不能取消、只有 `manual_required` 能标记已处理）。
// 前端再写一份的代价是"规则改一次漏一处"，而漏的那一处表现为"点了按钮报 400"——
// 用户看到的是"这个按钮坏了"。所以这里只转发服务端给的那三个布尔。
//
// 请求体类型不手写
// ----------------
// 与其余端点文件同一条：从 `types.gen.ts` 里取。手写一份形状，等于在「后端改了字段名」
// 这件事上自愿放弃了编译期保护。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type PublicationList = OkJson<"/api/v1/publish/publications", "get">;
export type Publication = NonNullable<PublicationList["by_status"]>[string][number];
/** 人工处置的请求体（重试 / 取消 / 标记已处理共用 —— 后端也是同一个模型）。 */
export type PublishActionBody = BodyJson<"/api/v1/publish/{publication_id}/retry", "post">;
export type PublishActionResponse = OkJson<"/api/v1/publish/{publication_id}/retry", "post">;
export type PublishEnqueueBody = BodyJson<"/api/v1/publish/tasks/{task_id}/enqueue", "post">;
export type PublishEnqueueResponse = OkJson<"/api/v1/publish/tasks/{task_id}/enqueue", "post">;
export type PublishPlatformsView = OkJson<"/api/v1/publish/platforms", "get">;
export type PublishPlatformOption = NonNullable<PublishPlatformsView["items"]>[number];
export type HandoffPreview = OkJson<"/api/v1/publish/handoff/{task_id}", "get">;
export type HandoffItem = NonNullable<HandoffPreview["items"]>[number];
export type HandoffResult = OkJson<"/api/v1/publish/handoff/{task_id}", "post">;
export type ComplianceView = OkJson<"/api/v1/publish/compliance", "get">;
export type ComplianceItem = NonNullable<ComplianceView["items"]>[number];
export type MetricsTickResult = OkJson<"/api/v1/publish/metrics/tick", "post">;
export type MemorySinkResult = OkJson<
  "/api/v1/publish/publications/{publication_id}/sink",
  "post"
>;

const PUBLISH_PATH = "/api/v1/publish";

/**
 * 发布看板（六状态各若干条 + 全量计数）。
 *
 * 带 `taskId` ⇒ 只回这条任务的记录，`counts` 也只算这条任务的。
 */
export function fetchPublications(
  options: { limit?: number; taskId?: string; signal?: AbortSignal } = {},
): Promise<PublicationList> {
  return apiGet<PublicationList>(`${PUBLISH_PATH}/publications`, {
    query: { limit: options.limit ?? 50, task_id: options.taskId ?? null },
    signal: options.signal,
  });
}

/** 待人工队列（只回 `manual_required` 那几条，带 error_code 与取证路径）。 */
export function fetchManualQueue(
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<PublicationList> {
  return apiGet<PublicationList>(`${PUBLISH_PATH}/queue`, {
    query: { limit: options.limit ?? 100 },
    signal: options.signal,
  });
}

/** 人工重试（`attempt_count` 归零 —— 人工这一下是"给它一次完整的机会"）。 */
export function retryPublication(
  publicationId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<PublishActionResponse> {
  return apiPost<PublishActionResponse>(
    `${PUBLISH_PATH}/${encodeURIComponent(publicationId)}/retry`,
    body,
    { signal },
  );
}

/** 人工取消（**已发布的不给取消**：平台上已经有那条作品了，删记录只会让账更乱）。 */
export function cancelPublication(
  publicationId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<PublishActionResponse> {
  return apiPost<PublishActionResponse>(
    `${PUBLISH_PATH}/${encodeURIComponent(publicationId)}/cancel`,
    body,
    { signal },
  );
}

/** 标记已人工处理（**必须写 reason**，后端会 422）。 */
export function markManualDone(
  publicationId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<PublishActionResponse> {
  return apiPost<PublishActionResponse>(
    `${PUBLISH_PATH}/${encodeURIComponent(publicationId)}/manual-done`,
    body,
    { signal },
  );
}

/** 把这条任务排进发布池（**不看 `publish.enabled`**：投递照样成功，作业会在 worker 那侧转人工）。 */
export function enqueueTask(
  taskId: string,
  body: PublishEnqueueBody,
  signal?: AbortSignal,
): Promise<PublishEnqueueResponse> {
  return apiPost<PublishEnqueueResponse>(
    `${PUBLISH_PATH}/tasks/${encodeURIComponent(taskId)}/enqueue`,
    body,
    { signal },
  );
}

/**
 * 投递面板的选项清单（**来自配置，不是面板自己列的**）。
 *
 * 面板列一份平台清单 = 把 `config/publish.yaml` 抄第二遍：加一个平台要改两处，而漏改
 * 的那一处表现为"这个平台在面板上不存在" —— 没人会去报这个 bug。连"点了会怎样"
 * （`selectable` / `note`）也一起给，判据与投递期是同一套。
 */
export function fetchPlatforms(signal?: AbortSignal): Promise<PublishPlatformsView> {
  return apiGet<PublishPlatformsView>(`${PUBLISH_PATH}/platforms`, { signal });
}

/** 交付包**预览**：会打进去哪几件、缺哪件（后端保证一个字节都不写）。 */
export function fetchHandoff(taskId: string, signal?: AbortSignal): Promise<HandoffPreview> {
  return apiGet<HandoffPreview>(`${PUBLISH_PATH}/handoff/${encodeURIComponent(taskId)}`, {
    signal,
  });
}

/** 打一个交付包出去（复制到 `app.yaml → handoff.output_dir` 下，并写 audit_ops）。 */
export function pushHandoff(
  taskId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<HandoffResult> {
  return apiPost<HandoffResult>(`${PUBLISH_PATH}/handoff/${encodeURIComponent(taskId)}`, body, {
    signal,
  });
}

/** R2 来源登记留档（**发布面板与素材库共用这一份**）。 */
export function fetchCompliance(signal?: AbortSignal): Promise<ComplianceView> {
  return apiGet<ComplianceView>(`${PUBLISH_PATH}/compliance`, { signal });
}

/**
 * 立刻跑一轮数据回收（T5.4 · §06.6）。
 *
 * 后台本来每 60s 自己拍一次；这个端点是**给人按的** —— "我刚发完想现在看一眼数据"，
 * 或者"上一轮看着没动静，手动催一下"。发布池正忙时它会整拍让路（``yielded``），
 * 那不是失败：两个浏览器抢同一个 profile 会让**发布**失败。
 */
export function runMetricsTick(signal?: AbortSignal): Promise<MetricsTickResult> {
  return apiPost<MetricsTickResult>(`${PUBLISH_PATH}/metrics/tick`, undefined, { signal });
}

/** 采**这一条**（不看 ``next_metric_at``：人按的就是"现在采"）。 */
export function collectMetrics(publicationId: string, signal?: AbortSignal): Promise<Publication> {
  return apiPost<Publication>(
    `${PUBLISH_PATH}/publications/${encodeURIComponent(publicationId)}/collect`,
    undefined,
    { signal },
  );
}

/** 把这一条的数据**沉淀成记忆**（§06.8：回流 feedback + 汇总文件 + 低互动降权）。 */
export function sinkMemory(publicationId: string, signal?: AbortSignal): Promise<MemorySinkResult> {
  return apiPost<MemorySinkResult>(
    `${PUBLISH_PATH}/publications/${encodeURIComponent(publicationId)}/sink`,
    undefined,
    { signal },
  );
}
