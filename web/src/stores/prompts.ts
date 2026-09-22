// 提示词面板状态（T6.2）—— 提示词的界面化编辑。
//
// 四条纪律
// --------
// 1. **校验交给后端**：引用了"没人会填的变量"由后端拦（422 + 变量名 + 允许清单）。
//    那份清单（`allowed_variables` = 自己模板 ∪ 全部 `shared.*` 注入块）在后端，
//    抄一份到前端就是两份真相，而且会先烂掉的是前端那份。
// 2. **草稿与生效分开存**：编辑到一半时切条目、或者一次保存失败，草稿不该把生效
//    内容盖掉。所以 `drafts` 是按 `<条目>/<段>` 存的另一份，`saved` 只由后端更新。
// 3. **只发真的改了的段**：后端把 `system: null` 当"这一段不动"（不是清空），
//    把没改的那段一起塞进去只会让返回的 `changed` 说一堆没发生的事。
// 4. **改没改看得见**：`overridden` 与 `prompt_version` 都来自后端 —— 版本跟着
//    覆盖走（`digest` 走生效那一份），所以面板显示的就是下一次生成会落库的那个号。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  fetchPrompts,
  restorePrompt,
  savePrompt,
  type PromptCatalog,
  type PromptEntry,
  type PromptOutcome,
  type PromptRole,
} from "@/api/endpoints/prompts";
import { ApiError } from "@/api/http";

/** 注入点（单测用假件替换，生产用真实现）。 */
export interface PromptsApi {
  fetchPrompts: typeof fetchPrompts;
  savePrompt: typeof savePrompt;
  restorePrompt: typeof restorePrompt;
}

let api: PromptsApi = { fetchPrompts, savePrompt, restorePrompt };

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configurePromptsApi(overrides: Partial<PromptsApi>): void {
  api = { ...api, ...overrides };
}

/** 一段提示词在面板上的样子（一个 textarea）。 */
export interface PromptSegment {
  role: PromptRole;
  path: string;
  /** 后端说这一段当前生效的是覆盖，不是仓库那份 */
  overridden: boolean;
  /** **生效**的正文（只由后端更新，草稿不会写进它） */
  saved: string;
  /** 面板上的草稿（用户正在编辑的那一份） */
  draft: string;
  /** 草稿与生效不一致 */
  dirty: boolean;
}

/** 草稿的键：一个条目最多两段，`<条目>/<段>` 唯一。 */
export function draftKey(name: string, role: PromptRole): string {
  return `${name}/${role}`;
}

/** 一个条目里被覆盖的段（面板上的「已覆盖」徽标就靠它）。 */
export function overriddenRoles(entry: PromptEntry): PromptRole[] {
  return entry.files.filter((file) => file.overridden).map((file) => file.role);
}

function describeError(reason: unknown): string {
  if (reason instanceof ApiError) {
    return reason.status === 0 ? reason.message : `${reason.message}（HTTP ${reason.status}）`;
  }
  return String(reason);
}

export const usePromptsStore = defineStore("prompts", () => {
  const entries = ref<PromptEntry[]>([]);
  const overrideDir = ref<string | null>(null);
  const selectedName = ref<string | null>(null);
  //: 草稿：`<条目>/<段>` → 用户正在敲的正文
  const drafts = ref<Record<string, string>>({});

  const loading = ref(false);
  const saving = ref<string | null>(null);
  const restoring = ref<string | null>(null);
  const error = ref<string | null>(null);
  const loadError = ref<string | null>(null);
  const notice = ref<string | null>(null);
  const lastOutcome = ref<PromptOutcome | null>(null);

  const selected = computed<PromptEntry | null>(
    () => entries.value.find((item) => item.name === selectedName.value) ?? null,
  );

  const segments = computed<PromptSegment[]>(() => {
    const entry = selected.value;
    if (entry === null) return [];
    return entry.files.map((file) => {
      const draft = drafts.value[draftKey(entry.name, file.role)];
      return {
        role: file.role,
        path: file.path,
        overridden: file.overridden,
        saved: file.text,
        draft: draft ?? file.text,
        dirty: draft !== undefined && draft !== file.text,
      };
    });
  });

  const dirtyCount = computed(() => segments.value.filter((item) => item.dirty).length);

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
  }

  /** 拉全部条目。选中项保持不动（刷新不该把用户正在看的那条换掉）。 */
  async function load(): Promise<void> {
    loading.value = true;
    loadError.value = null;
    try {
      const catalog: PromptCatalog = await api.fetchPrompts();
      entries.value = catalog.prompts;
      overrideDir.value = catalog.override_dir ?? null;
      const names = catalog.prompts.map((item) => item.name);
      if (selectedName.value === null || !names.includes(selectedName.value)) {
        selectedName.value = names[0] ?? null;
      }
    } catch (reason) {
      loadError.value = describeError(reason);
    } finally {
      loading.value = false;
    }
  }

  /** 换一个条目看。**草稿留着**：切回去还在，不必重打。 */
  function select(name: string): void {
    selectedName.value = name;
    clearMessages();
  }

  function setDraft(name: string, role: PromptRole, text: string): void {
    drafts.value = { ...drafts.value, [draftKey(name, role)]: text };
  }

  /** 放弃这一段的草稿（退回生效那一份）。 */
  function dropDraft(name: string, role: PromptRole): void {
    const next = { ...drafts.value };
    delete next[draftKey(name, role)];
    drafts.value = next;
  }

  function dropAllDrafts(name: string): void {
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(drafts.value)) {
      if (!key.startsWith(`${name}/`)) next[key] = value;
    }
    drafts.value = next;
  }

  /**
   * 保存这个条目里**真的改了的**段。
   *
   * 一段都没改 ⇒ 直接返回 `true`（不发请求）：后端会把"什么都没改"当成一次成功但
   * 不留痕的操作，多发一次只是噪音。
   */
  async function save(name: string): Promise<boolean> {
    const entry = entries.value.find((item) => item.name === name);
    if (entry === undefined) return false;

    const body: { system?: string; user?: string; reason?: string } = {};
    for (const file of entry.files) {
      const draft = drafts.value[draftKey(name, file.role)];
      if (draft !== undefined && draft !== file.text) body[file.role] = draft;
    }
    if (Object.keys(body).length === 0) return true;

    saving.value = name;
    clearMessages();
    try {
      const outcome = await api.savePrompt(name, body);
      lastOutcome.value = outcome;
      entries.value = entries.value.map((item) => (item.name === name ? outcome.entry : item));
      dropAllDrafts(name);
      notice.value =
        outcome.changed.length === 0
          ? "提交的内容与生效那份逐字相同，什么都没写"
          : `已保存 ${outcome.changed.join(" / ")} ⇒ 版本 ${outcome.entry.prompt_version}`;
      return true;
    } catch (reason) {
      error.value = describeError(reason);
      return false;
    } finally {
      saving.value = null;
    }
  }

  /** 还原一段（删掉覆盖文件 ⇒ 退回仓库那一份）。**幂等**。 */
  async function restore(name: string, role: PromptRole): Promise<boolean> {
    restoring.value = draftKey(name, role);
    clearMessages();
    try {
      const outcome = await api.restorePrompt(name, role);
      lastOutcome.value = outcome;
      entries.value = entries.value.map((item) => (item.name === name ? outcome.entry : item));
      dropDraft(name, role);
      notice.value =
        outcome.changed.length === 0
          ? "这一段本来就没被覆盖"
          : `已还原 ${role} ⇒ 版本 ${outcome.entry.prompt_version}`;
      return true;
    } catch (reason) {
      error.value = describeError(reason);
      return false;
    } finally {
      restoring.value = null;
    }
  }

  return {
    entries,
    overrideDir,
    selectedName,
    drafts,
    selected,
    segments,
    dirtyCount,
    loading,
    saving,
    restoring,
    error,
    loadError,
    notice,
    lastOutcome,
    load,
    select,
    setDraft,
    dropDraft,
    save,
    restore,
  };
});
