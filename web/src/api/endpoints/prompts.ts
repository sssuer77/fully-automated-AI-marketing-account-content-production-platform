// 提示词面板 REST 面（T6.2）—— 提示词的界面化编辑。
//
// 三个端点对应面板上的三件事：看全部条目、存一段覆盖、还原一段。
//
// 为什么没有"新建提示词"
// ---------------------
// 条目是**注册表**里的东西（`prompts/manifest.yaml` + 每个文件的 sha256），
// 而注册表是入库的。面板造一个新条目就意味着往仓库文件里写 —— 那是 git 的事。
//
// 为什么"还原"要带 file 参数
// -------------------------
// 一个条目最多两段（system / user），是两份独立文件。不带参数的话，"还原 ideator"
// 到底还原哪一份就没有答案了；默认成"两段都还原"会让一次误点把两处改动一起丢掉。

import { apiDelete, apiGet, apiPut, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type PromptCatalog = OkJson<"/api/v1/prompts", "get">;
export type PromptEntry = PromptCatalog["prompts"][number];
export type PromptFile = PromptEntry["files"][number];
export type PromptRole = PromptFile["role"];
export type PromptSaveBody = BodyJson<"/api/v1/prompts/{name}", "put">;
export type PromptOutcome = OkJson<"/api/v1/prompts/{name}", "put">;

const PROMPTS_PATH = "/api/v1/prompts";

/** 全部条目（含**生效**正文与 `prompt_version`）。 */
export function fetchPrompts(options: { signal?: AbortSignal } = {}): Promise<PromptCatalog> {
  return apiGet<PromptCatalog>(PROMPTS_PATH, { signal: options.signal });
}

/**
 * 存一段覆盖（`system` / `user` 只给要改的那一段，另一段留空 = 不动）。
 *
 * 后端**先校验、后落盘**：引用了没人会填的变量 ⇒ 422，一个字节都不写。
 * 提交的内容与生效那份逐字相同 ⇒ `changed` 是空数组（不是错误）。
 */
export function savePrompt(name: string, body: PromptSaveBody): Promise<PromptOutcome> {
  return apiPut<PromptOutcome>(`${PROMPTS_PATH}/${encodeURIComponent(name)}`, body);
}

/** 还原一段（删掉覆盖文件 ⇒ 退回仓库那一份）。**幂等**：本来就没覆盖也回 200。 */
export function restorePrompt(
  name: string,
  file: PromptRole,
  options: { signal?: AbortSignal } = {},
): Promise<PromptOutcome> {
  return apiDelete<PromptOutcome>(
    `${PROMPTS_PATH}/${encodeURIComponent(name)}/override?file=${encodeURIComponent(file)}`,
    { signal: options.signal },
  );
}
