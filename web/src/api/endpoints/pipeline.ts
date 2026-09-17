// 一键出片面板 REST 面（T4.14 延伸 · §04.6.8）。
//
// 五个端点对应面板上的五件事：看首屏（`/pipeline/console`）、看"这条任务点了会怎样"
// （`GET /pipeline/tasks/{id}`）、开一条（`POST /pipeline/jobs`）、看进度
// （`GET /pipeline/jobs/{id}`）、叫停（`POST /pipeline/jobs/{id}/cancel`）。
//
// 为什么**没有**播放成片的端点
// ----------------------------
// 成片就落在 `data/output/videos/` 里，而渲染面板已经有一条取它的路径
// （`/render/videos/{name}`，带 Range 支持）。再开一条一模一样的，只会多一处
// "到底该用哪个 url" 的分叉 —— 所以这里返回的是**路径**，播放走渲染面板那个入口
// （`renderVideoUrl`）。
//
// 请求体类型不手写
// ----------------
// 与渲染 / 合成配置同一条：从 `types.gen.ts` 里取。手写一份形状，等于在
// 「后端改了字段名」这件事上自愿放弃编译期保护。

import { apiGet, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type PipelineConsole = OkJson<"/api/v1/pipeline/console", "get">;
export type PipelineUntilOption = PipelineConsole["until_options"][number];
export type PipelineVoice = PipelineConsole["voices"][number];
export type PipelineJob = PipelineConsole["jobs"][number];
export type PipelineTask = OkJson<"/api/v1/pipeline/tasks/{task_id}", "get">;
export type PipelineJobRequest = BodyJson<"/api/v1/pipeline/jobs", "post">;
/** 提交的结果：那条 job + 这次是不是**复用**了已经在跑的那一条。 */
export type PipelineSubmit = OkJson<"/api/v1/pipeline/jobs", "post">;

const PIPELINE_PATH = "/api/v1/pipeline";

/** 面板首屏：可选落点 / 可选音色 / 在跑的那条 / 最近几条。 */
export function fetchPipelineConsole(signal?: AbortSignal): Promise<PipelineConsole> {
  return apiGet<PipelineConsole>(`${PIPELINE_PATH}/console`, { signal });
}

/**
 * 这条任务现在在哪一步、点了会怎样。
 *
 * `until` 可给可不给：面板换落点时重调一次，那一行字就跟着变（"从 voicing 推到
 * completed"和"已经在 completed 上了"是两句话）。任务号不存在 ⇒ 404。
 */
export function fetchPipelineTask(
  taskId: string,
  until?: string | null,
  signal?: AbortSignal,
): Promise<PipelineTask> {
  return apiGet<PipelineTask>(`${PIPELINE_PATH}/tasks/${encodeURIComponent(taskId)}`, {
    query: { until },
    signal,
  });
}

/** 开一条「一路做到出片」（**立刻返回**：真正的活在服务端的后台线程里跑）。 */
export function createPipelineJob(
  body: PipelineJobRequest,
  signal?: AbortSignal,
): Promise<PipelineSubmit> {
  return apiPost<PipelineSubmit>(`${PIPELINE_PATH}/jobs`, body, { signal });
}

/** 查一条的进度 / 结果（面板轮询的就是它）。 */
export function fetchPipelineJob(jobId: string, signal?: AbortSignal): Promise<PipelineJob> {
  return apiGet<PipelineJob>(`${PIPELINE_PATH}/jobs/${encodeURIComponent(jobId)}`, { signal });
}

/**
 * 叫停一条。
 *
 * 后端是**协作式**取消，而且检查点**比渲染少**：只在配音的每一句与渲染的每一段。
 * 任务停在"投递配音作业"或"拼母带"那几步时按取消，要等它进到下一个回调点才会真的停
 * —— 面板上必须把这句话写出来，否则用户按了看见进度条还在动，会以为按钮坏了。
 */
export function cancelPipelineJob(jobId: string, signal?: AbortSignal): Promise<PipelineJob> {
  return apiPost<PipelineJob>(
    `${PIPELINE_PATH}/jobs/${encodeURIComponent(jobId)}/cancel`,
    {},
    { signal },
  );
}
