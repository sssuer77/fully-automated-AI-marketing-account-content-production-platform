// 选题面板 REST 面（T4.3 · §04.4.5 第 2 行）。
//
// 路径对应面板的动作：选题池（topics）、方向卡片 + 历史批次（directions）、
// 人工写 / 改 / 删方向（POST/PATCH/DELETE directions）、两次长任务（analyze / ideate）、
// 勾选入队（select）、人工加选题（manual）、改选题（PATCH topics/{id}）、
// 删选题（DELETE topics/{id}）、生成文案并送审（POST topics/{id}/draft-review）、
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

import { apiDelete, apiGet, apiPatch, apiPost, apiPut, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 长任务（analyze / ideate）的超时：默认 10s 是"问一句状态"的尺度，不够用。 */
export const LONG_TASK_TIMEOUT_MS = 300_000;

/** 选题池单页条数（瀑布流是"人一条条看"的东西，给太多反而看不完）。 */
export const PAGE_SIZE = 200;

/**
 * 手写方向的默认优先级。
 *
 * 为什么要在前端写死这一个数：OpenAPI 会把"有默认值的字段"渲染成**必填**
 * （openapi-typescript 的既定行为），所以 `ManualDirectionBody` 的 `priority`
 * 在类型上是必给的。值与后端 `add_manual_direction` 的默认值同源（100 = 普通）。
 */
export const DEFAULT_DIRECTION_PRIORITY = 100;

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
export type ManualDirectionBody = BodyJson<"/api/v1/topics/directions", "post">;
export type ManualDirectionResult = OkJson<"/api/v1/topics/directions", "post">;
export type DirectionPatchBody = BodyJson<"/api/v1/topics/directions/{direction_id}", "patch">;
export type DirectionEditResult = OkJson<"/api/v1/topics/directions/{direction_id}", "patch">;
export type DirectionDeleteResult = OkJson<"/api/v1/topics/directions/{direction_id}", "delete">;
export type DraftReviewResult = OkJson<"/api/v1/topics/{topic_id}/draft-review", "post">;
export type TopicEditResult = OkJson<"/api/v1/topics/{topic_id}", "patch">;
export type OutlineView = OkJson<"/api/v1/topics/{topic_id}/outline", "get">;
export type OutlineItem = NonNullable<OutlineView["outline"]>;
export type OutlineResult = OkJson<"/api/v1/topics/{topic_id}/outline", "post">;
export type TopicDeleteResult = OkJson<"/api/v1/topics/{topic_id}", "delete">;
export type HotImportResult = OkJson<"/api/v1/hot/import", "post">;
export type ImportResult = NonNullable<HotImportResult["hot"]>;

export type AnalyzeBody = BodyJson<"/api/v1/topics/analyze", "post">;
export type IdeateBody = BodyJson<"/api/v1/topics/ideate", "post">;
export type SelectBody = BodyJson<"/api/v1/topics/select", "post">;
export type ManualTopicBody = BodyJson<"/api/v1/topics/manual", "post">;
export type TopicPatchBody = BodyJson<"/api/v1/topics/{topic_id}", "patch">;
export type OutlineSaveBody = BodyJson<"/api/v1/topics/{topic_id}/outline", "put">;
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
  body: IdeateBody = { per_direction: 5 },
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

/**
 * 改一条选题（**只传你要改的字段**；字段给 `null` = 把那一列清空）。
 *
 * 改标题 ⇒ 后端顺手重算去重指纹与相似清单，所以返回的 `topic` 里那两列也可能变。
 */
export function updateTopic(
  topicId: string,
  body: TopicPatchBody,
  signal?: AbortSignal,
): Promise<TopicEditResult> {
  return apiPatch<TopicEditResult>(`/api/v1/topics/${encodeURIComponent(topicId)}`, body, {
    signal,
  });
}

/**
 * 删一条选题（**不可撤销**）。
 *
 * 已经派生过任务的选题会被后端拦下（422 `TOPIC_SELECT_INVALID` + 任务号）：删掉它
 * 那条任务就再也写不出稿 —— 前端不需要自己判，把后端给的原因照原样摆出来即可。
 */
export function deleteTopic(topicId: string, signal?: AbortSignal): Promise<TopicDeleteResult> {
  return apiDelete<TopicDeleteResult>(`/api/v1/topics/${encodeURIComponent(topicId)}`, { signal });
}

/**
 * 读一个选题的二级产物（视频标题 + 核心论点）。
 *
 * 没有 ⇒ `outline=null`，**不是 404**：「这一级还没定」是正常状态（三级会照旧自由发挥），
 * 用一次注定失败的往返表达它，等于把「还没做」伪装成「出错了」。
 */
export function fetchTopicOutline(topicId: string, signal?: AbortSignal): Promise<OutlineView> {
  return apiGet<OutlineView>(`/api/v1/topics/${encodeURIComponent(topicId)}/outline`, { signal });
}

/** 让模型定标题与核心论点（**长任务**：一次 LLM）。 */
export function generateTopicOutline(
  topicId: string,
  signal?: AbortSignal,
): Promise<OutlineResult> {
  return apiPost<OutlineResult>(
    `/api/v1/topics/${encodeURIComponent(topicId)}/outline`,
    undefined,
    { timeoutMs: LONG_TASK_TIMEOUT_MS, signal },
  );
}

/** 手工定稿二级产物（**一次 LLM 都不调**：没配 Key 也能用）。 */
export function saveTopicOutline(
  topicId: string,
  body: OutlineSaveBody,
  signal?: AbortSignal,
): Promise<OutlineResult> {
  return apiPut<OutlineResult>(`/api/v1/topics/${encodeURIComponent(topicId)}/outline`, body, {
    signal,
  });
}

/** 清空二级产物（**幂等**）：清掉之后三级退回「按选题自由发挥」。 */
export function clearTopicOutline(topicId: string, signal?: AbortSignal): Promise<OutlineResult> {
  return apiDelete<OutlineResult>(`/api/v1/topics/${encodeURIComponent(topicId)}/outline`, {
    signal,
  });
}

/** 扫盘导入 `data/hot/*.md` 与 `data/feedback/*.md`（幂等）。 */
export function importHot(signal?: AbortSignal): Promise<HotImportResult> {
  return apiPost<HotImportResult>("/api/v1/hot/import", { hot: true, feedback: true }, { signal });
}

/** 网页端直接粘一批输入源（落盘文件名由服务端生成，客户端不参与）。 */
export function submitHot(body: HotSubmitBody, signal?: AbortSignal): Promise<HotImportResult> {
  return apiPost<HotImportResult>("/api/v1/hot/submit", body, { signal });
}


// ── 方向：人工写 / 改 / 删（左列那一栏）──────────────────────────────
//
// 这三个动作**一次 LLM 都不调**：方向这一列是"这一批要做什么"的草稿纸，
// 手写的与模型产的混在一起才看得见全貌。所以它们既没有长任务超时，也不需要
// 单飞守卫 —— 点下去就该立刻有结果。

/**
 * 人工写一个方向（**不经模型**）。
 *
 * `batch_id` 缺省 ⇒ 后端落进**当前正在看的那个批次**（而不是另起一个"手工批次"
 * 把模型那批盖掉）。面板只在切换历史批次时才显式传它。
 */
export function createDirection(
  body: ManualDirectionBody,
  signal?: AbortSignal,
): Promise<ManualDirectionResult> {
  return apiPost<ManualDirectionResult>("/api/v1/topics/directions", body, { signal });
}

/**
 * 改一个方向（**只传你要改的字段**；字段给 `null` = 把那一列清空）。
 *
 * `batch_id` / `seq` / `status` 不可改：前两个是"这一批怎么排的"，
 * 后者有自己的语义（后端会 422，前端不需要自己判）。
 */
export function updateDirection(
  directionId: string,
  body: DirectionPatchBody,
  signal?: AbortSignal,
): Promise<DirectionEditResult> {
  return apiPatch<DirectionEditResult>(
    `/api/v1/topics/directions/${encodeURIComponent(directionId)}`,
    body,
    { signal },
  );
}

/**
 * 删一个方向，**它下面的候选一起走**（级联）。
 *
 * 响应里的 `cascaded_topics` 就是被一起删掉的候选条数 —— 面板拿它做二次确认，
 * 也拿它在事后如实报一句"删了 7 条候选"。已经有候选派生了任务的方向会被后端拦下
 * （422 + 任务号）：那不是门禁，是"删掉之后立刻断链"。
 */
export function deleteDirection(
  directionId: string,
  signal?: AbortSignal,
): Promise<DirectionDeleteResult> {
  return apiDelete<DirectionDeleteResult>(
    `/api/v1/topics/directions/${encodeURIComponent(directionId)}`,
    { signal },
  );
}

/**
 * 选中一条候选 ⇒ 生成完整文案 ⇒ 移交审核（**长任务**：Director + Writer 两次 LLM）。
 *
 * 与 `selectTopics([id], true)` 的差别：那一个是"批量勾选，顺手写稿"（跑完停在
 * `drafting`，交给写稿池接着审）；这一个是**单条候选的下一步**（写完之后推到
 * `reviewing`）。响应里的 `task_status` 会告诉你它落在哪一格，`reused` 告诉你
 * 这一次有没有真的花钱 —— 库里已经有生效稿件时它是 `true`。
 */
export function draftTopicForReview(
  topicId: string,
  signal?: AbortSignal,
): Promise<DraftReviewResult> {
  return apiPost<DraftReviewResult>(
    `/api/v1/topics/${encodeURIComponent(topicId)}/draft-review`,
    undefined,
    { timeoutMs: LONG_TASK_TIMEOUT_MS, signal },
  );
}
