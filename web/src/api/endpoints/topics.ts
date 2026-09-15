// 选题面板 REST 面（T4.3 · §04.4.5 第 2 行）。
//
// 八条路径对应面板的八块动作：选题池（topics）、方向卡片 + 历史批次（directions）、
// 两次长任务（analyze / ideate）、勾选入队（select）、人工加选题（manual）、
// 输入源（hot/import 扫盘 · hot/submit 网页粘贴）。
//
// 长任务为什么单独给超时
// ----------------------
// `analyze` / `ideate` 是几十秒的 LLM 流水线（5–8 个方向 × 每方向 3–5 条选题）。
// 默认 10s 超时会在后端还在跑的时候先把前端判死：用户看到"超时"，其实那边马上就出
// 结果了，于是他会再点一次 —— 正好撞上后端的单飞守卫（409 `TOPIC_BATCH_RUNNING`）。
// 给足 5 分钟比"假装很快"更诚实。
//
// 请求体类型也不手写
// ------------------
// 与响应一样从 `types.gen.ts` 里取（`BodyJson`）—— 手写一份 `{ topic_ids: string[] }`
// 就等于在"后端改了字段名"这件事上自愿放弃了编译期保护。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 长任务（analyze / ideate）的超时：默认 10s 是"问一句状态"的尺度，不够用。 */
export const LONG_TASK_TIMEOUT_MS = 300_000;

/** 选题池单页条数（瀑布流是"人一条条看"的东西，给太多反而看不完）。 */
export const PAGE_SIZE = 200;

/** 某个操作的**请求体**类型（同样从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type TopicList = OkJson<"/api/v1/topics", "get">;
export type TopicItem = TopicList["topics"][number];
export type DirectionList = OkJson<"/api/v1/topics/directions", "get">;
export type DirectionItem = DirectionList["directions"][number];
export type AnalyzeResult = OkJson<"/api/v1/topics/analyze", "post">;
export type IdeateResult = OkJson<"/api/v1/topics/ideate", "post">;
export type SelectResult = OkJson<"/api/v1/topics/select", "post">;
export type SelectItem = SelectResult["selected"][number];
export type SelectFailure = SelectResult["failed"][number];
export type ManualTopicResult = OkJson<"/api/v1/topics/manual", "post">;
export type HotImportResult = OkJson<"/api/v1/hot/import", "post">;
export type ImportResult = NonNullable<HotImportResult["hot"]>;

export type AnalyzeBody = BodyJson<"/api/v1/topics/analyze", "post">;
export type IdeateBody = BodyJson<"/api/v1/topics/ideate", "post">;
export type SelectBody = BodyJson<"/api/v1/topics/select", "post">;
export type ManualTopicBody = BodyJson<"/api/v1/topics/manual", "post">;
export type HotSubmitBody = BodyJson<"/api/v1/hot/submit", "post">;

/** 瀑布流（默认只看候选：已入队 / 已淘汰的另开状态看）。 */
export function fetchTopics(
  status = "candidate",
  limit = PAGE_SIZE,
  signal?: AbortSignal,
): Promise<TopicList> {
  return apiGet<TopicList>("/api/v1/topics", { query: { status, limit }, signal });
}

/** 方向卡片；`batchId` 省略 ⇒ 后端给最近一批（并把它回填进 `batch_id`）。 */
export function fetchDirections(
  batchId?: string | null,
  signal?: AbortSignal,
): Promise<DirectionList> {
  return apiGet<DirectionList>("/api/v1/topics/directions", {
    query: { batch_id: batchId ?? null },
    signal,
  });
}

/**
 * 触发方向分析（长任务；已在跑 ⇒ 409，**不排队**）。
 *
 * `import_sources` 显式给 `true`：后端虽然给了默认值，但 OpenAPI 把"有默认值的字段"
 * 渲染成**必填**（openapi-typescript 的既定行为），所以这里明写一次 —— 顺带也让
 * "这一跑要不要先扫盘"在调用点看得见。
 */
export function analyzeTopics(
  body: AnalyzeBody = { import_sources: true },
  signal?: AbortSignal,
): Promise<AnalyzeResult> {
  return apiPost<AnalyzeResult>("/api/v1/topics/analyze", body, {
    timeoutMs: LONG_TASK_TIMEOUT_MS,
    signal,
  });
}

/** 逐方向产出选题（长任务；方向之间互不影响）。 */
export function ideateTopics(
  body: IdeateBody = { per_direction: 4 },
  signal?: AbortSignal,
): Promise<IdeateResult> {
  return apiPost<IdeateResult>("/api/v1/topics/ideate", body, {
    timeoutMs: LONG_TASK_TIMEOUT_MS,
    signal,
  });
}

/**
 * 勾选入队（逐条建任务，幂等键 `topic:<id>`）。
 *
 * `draftNow` 为真 ⇒ 后端顺手跑一次写稿，那是一条**长任务**，超时跟着放宽；
 * 为假（默认）时它只是一串建任务，10s 足够（T4.3 裁定 132）。
 */
export function selectTopics(
  topicIds: readonly string[],
  draftNow = false,
  signal?: AbortSignal,
): Promise<SelectResult> {
  const body: SelectBody = { topic_ids: [...topicIds], draft_now: draftNow };
  return apiPost<SelectResult>("/api/v1/topics/select", body, {
    timeoutMs: draftNow ? LONG_TASK_TIMEOUT_MS : undefined,
    signal,
  });
}

/** 人工加选题（相似只提示、不拦）。 */
export function addManualTopic(
  body: ManualTopicBody,
  signal?: AbortSignal,
): Promise<ManualTopicResult> {
  return apiPost<ManualTopicResult>("/api/v1/topics/manual", body, { signal });
}

/** 扫盘导入 `data/hot/*.md` 与 `data/feedback/*.md`（幂等）。 */
export function importHot(signal?: AbortSignal): Promise<HotImportResult> {
  return apiPost<HotImportResult>("/api/v1/hot/import", { hot: true, feedback: true }, { signal });
}

/** 网页端直接粘一批输入源（落盘文件名由服务端生成，客户端不参与）。 */
export function submitHot(body: HotSubmitBody, signal?: AbortSignal): Promise<HotImportResult> {
  return apiPost<HotImportResult>("/api/v1/hot/submit", body, { signal });
}
