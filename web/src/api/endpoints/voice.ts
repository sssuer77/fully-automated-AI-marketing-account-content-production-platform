// 配音面板 REST 面（T4.5 · §04.3.7）。
//
// 四个端点对应面板上的四件事：看逐句状态（`GET /sentences`）、看有哪些音色
// （`GET /voices`）、重配一句（`POST /sentences/{id}/resynth`）、换音色
// （`PATCH /tasks/{id}/voice_map`）。
//
// 试听**不走这里**
// ----------------
// 后端在逐句列表里直接给了 `audio_url`（没有音频时是 `null`），面板把它交给
// `<audio src>`，浏览器自己发范围请求 —— 与渲染面板的 `<video>` 同一条。
// 面板**不自己拼**这个 url：它指的是盘上那份文件，而"文件落在哪"是后端的事
// （§2.3 一改目录，自己拼的 url 就变成"点了播放没反应"，且不报错）。
//
// 为什么"重配"与"换音色"是**两个**端点
// ------------------------------------
// 代价不一样：重配一句是几秒，换音色是"重配 N 句"（几十秒到几分钟）。合成一个端点，
// 面板就没法把"点了立刻有反应"与"先弹确认框"分开 —— 而二次确认恰恰是换音色最该有的
// 那一步。两次调用的是**同一个** `patchVoiceMap`，差别只在请求体里那一个布尔值。
//
// 请求体类型不手写
// ----------------
// 与渲染面板 / 素材库同一条：从 `types.gen.ts` 里取。手写一份形状，等于在
// 「后端改了字段名」这件事上自愿放弃编译期保护。

import { apiGet, apiPatch, apiPost, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type SentenceVoiceList = OkJson<"/api/v1/sentences", "get">;
export type SentenceVoice = SentenceVoiceList["sentences"][number];
export type SentenceProgress = SentenceVoiceList["progress"];
export type VoiceOptions = OkJson<"/api/v1/voices", "get">;
export type VoiceOption = VoiceOptions["voices"][number];
export type ResynthResponse = OkJson<"/api/v1/sentences/{sentence_id}/resynth", "post">;
export type VoiceMapRequest = BodyJson<"/api/v1/tasks/{task_id}/voice_map", "patch">;
export type VoiceMapResponse = OkJson<"/api/v1/tasks/{task_id}/voice_map", "patch">;
export type VoiceChange = VoiceMapResponse["changes"][number];

const SENTENCES_PATH = "/api/v1/sentences";
const VOICES_PATH = "/api/v1/voices";
const TASKS_PATH = "/api/v1/tasks";

/** 一条任务的逐句配音状态（面板的主列表；`task_id` 是必填的查询参数）。 */
export function fetchSentences(taskId: string, signal?: AbortSignal): Promise<SentenceVoiceList> {
  return apiGet<SentenceVoiceList>(SENTENCES_PATH, { query: { task_id: taskId }, signal });
}

/**
 * 音色下拉框；带上 `taskId` 就顺带拿这条任务现在的映射。
 *
 * 一次请求画两件事（有哪些音色 + 这条任务用哪个）是有意的：分两次请求，第一帧会
 * 显示成"没选音色"，而那个空白会被读成"这条任务没配音色"。
 */
export function fetchVoiceOptions(taskId: string, signal?: AbortSignal): Promise<VoiceOptions> {
  return apiGet<VoiceOptions>(VOICES_PATH, { query: { task_id: taskId }, signal });
}

/** 单句重配：这一句退回待办 + 它的作业排回 voice 池（**立刻返回**，念的是 voice 池）。 */
export function resynthSentence(sentenceId: string, signal?: AbortSignal): Promise<ResynthResponse> {
  return apiPost<ResynthResponse>(
    `${SENTENCES_PATH}/${encodeURIComponent(sentenceId)}/resynth`,
    {},
    { signal },
  );
}

/**
 * 任务级换音色。
 *
 * 不带 `confirm` 时后端**先算代价**再抛 409（`VOICE_MAP_CONFIRM_REQUIRED`，
 * `context.affected` / `context.sentences` 在错误信封里），面板据此弹二次确认框；
 * 用户点头后带 `confirm: true` 重发。
 */
export function patchVoiceMap(
  taskId: string,
  body: VoiceMapRequest,
  signal?: AbortSignal,
): Promise<VoiceMapResponse> {
  return apiPatch<VoiceMapResponse>(`${TASKS_PATH}/${encodeURIComponent(taskId)}/voice_map`, body, {
    signal,
  });
}
