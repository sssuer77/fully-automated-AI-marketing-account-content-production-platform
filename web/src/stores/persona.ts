// 人物库面板状态（T4.13 · §02.4 / §04.1.6 / §04.5.8）。
//
// 这一屏要回答四个问题
// --------------------
// ① 现在用的是谁、哪来的、坏没坏？② 库里还有哪些人、哪个不能用？
// ③ 改完怎么存、改错了怎么退？④ 换个人上。
//
// 为什么"草稿"是 store 的一等公民
// ------------------------------
// 表单里的东西**不是**服务端状态：它随时可能是"用户改了一半"。所以它单独放
// `draft`，并且：
//   - 只有**改了**的字段才发出去（`dirtyChanges`）—— 两个标签页同时开着，整份表单
//     回写会把对方刚保存的值覆盖成"我打开时的那一份"；
//   - WS 事件 / 刷新**不覆盖**脏草稿（`draftStale` 只如实提示"底稿变了"）。
//     少了这一条，一条 `system.persona_changed` 就能把正在写的半屏字抹掉 ——
//     而它恰好会在你按下"切换"的时候到达。
//
// 为什么本地也要判一次长度
// ------------------------
// 真判定只有一次（服务端 `PersonaConfig.model_validate`），但等一个来回才告诉用户
// "名字不能为空"太慢。所以前端拿响应里的 `limits`（**从模型现取的那一份**）做即时
// 提示，提交时服务端仍然会再判一遍 —— 两者用的是同一组数字，不会打架。
//
// 为什么没有兜底轮询
// ----------------
// 与总览台 / 四池一致：顶栏的 WS 灯就是"这屏还动不动"的答案。重连成功时 store
// 主动重拉一次补缺口（`watch(wsStatus)`）。

import { defineStore } from "pinia";
import { computed, ref, watch } from "vue";

import { ApiError } from "@/api/http";
import {
  activatePersona as activatePersonaRequest,
  fetchPersona,
  rollbackPersona as rollbackPersonaRequest,
  saveAsPersona as saveAsPersonaRequest,
  updatePersona as updatePersonaRequest,
  type PersonaBackup,
  type PersonaConfig,
  type PersonaLibraryItem,
  type PersonaLimits,
  type PersonaOutcome,
  type PersonaResponse,
  type PersonaUpdateBody,
} from "@/api/endpoints/persona";
import { useChannelStream } from "@/composables/useTaskStream";
import { useWsConnection } from "@/composables/useWsConnection";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";
import type { Envelope } from "@/ws/events";

/** 面板上的表单草稿（= 可编辑字段的子集，与 `EDITABLE_FIELDS` 一一对应）。 */
export interface PersonaDraft {
  name: string;
  role_desc: string;
  tone: string;
  audience: string;
  catchphrases: string[];
  forbidden: string[];
  speaker_names: string[];
  style_hint: string;
  target_chars_min: number;
  target_chars_max: number;
  max_duration_ms: number;
}

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface PersonaApi {
  fetchPersona: typeof fetchPersona;
  updatePersona: typeof updatePersonaRequest;
  activatePersona: typeof activatePersonaRequest;
  saveAsPersona: typeof saveAsPersonaRequest;
  rollbackPersona: typeof rollbackPersonaRequest;
}

let api: PersonaApi = {
  fetchPersona,
  updatePersona: updatePersonaRequest,
  activatePersona: activatePersonaRequest,
  saveAsPersona: saveAsPersonaRequest,
  rollbackPersona: rollbackPersonaRequest,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configurePersonaApi(overrides: Partial<PersonaApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 服务端那一份 ⇒ 草稿（打开表单 / 保存成功后回填）。 */
export function draftFrom(config: PersonaConfig): PersonaDraft {
  return {
    name: config.name,
    role_desc: config.role_desc,
    tone: config.tone,
    audience: config.audience,
    catchphrases: [...config.catchphrases],
    forbidden: [...config.forbidden],
    speaker_names: [...config.speaker_names],
    style_hint: config.style_hint,
    target_chars_min: config.target_chars_min,
    target_chars_max: config.target_chars_max,
    max_duration_ms: config.max_duration_ms,
  };
}

function sameList(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

/**
 * 草稿 → **只包含改动**的请求体（没改动 ⇒ `{}`）。
 *
 * 这是"两个标签页同时开着"这件事的全部答案：整份回写会用**我打开时的旧值**覆盖
 * 对方刚保存的字段，而只发改动时，两边的改动可以叠加。
 */
export function dirtyChanges(draft: PersonaDraft, config: PersonaConfig): PersonaUpdateBody {
  const body: PersonaUpdateBody = {};
  if (draft.name !== config.name) body.name = draft.name;
  if (draft.role_desc !== config.role_desc) body.role_desc = draft.role_desc;
  if (draft.tone !== config.tone) body.tone = draft.tone;
  if (draft.audience !== config.audience) body.audience = draft.audience;
  if (!sameList(draft.catchphrases, config.catchphrases)) body.catchphrases = [...draft.catchphrases];
  if (!sameList(draft.forbidden, config.forbidden)) body.forbidden = [...draft.forbidden];
  if (!sameList(draft.speaker_names, config.speaker_names)) {
    body.speaker_names = [...draft.speaker_names];
  }
  if (draft.style_hint !== config.style_hint) body.style_hint = draft.style_hint;
  if (draft.target_chars_min !== config.target_chars_min) {
    body.target_chars_min = draft.target_chars_min;
  }
  if (draft.target_chars_max !== config.target_chars_max) {
    body.target_chars_max = draft.target_chars_max;
  }
  if (draft.max_duration_ms !== config.max_duration_ms) body.max_duration_ms = draft.max_duration_ms;
  return body;
}

/** 有改动吗（"保存"按钮亮不亮）。 */
export function isDirty(draft: PersonaDraft, config: PersonaConfig): boolean {
  return Object.keys(dirtyChanges(draft, config)).length > 0;
}

function checkLength(
  value: string,
  label: string,
  bounds: { min: number | null; max: number | null },
): string | null {
  const length = [...value].length; // 按**字符**数，不是 UTF-16 码元（"熊大"是 2 个字）
  if (bounds.min !== null && length < bounds.min) return `${label}至少 ${bounds.min} 个字`;
  if (bounds.max !== null && length > bounds.max) return `${label}最多 ${bounds.max} 个字`;
  return null;
}

/**
 * 提交前的即时提示（**不是**权威判定：权威在服务端的 `PersonaConfig`）。
 *
 * 返回空对象 ⇒ 可以提交。字段名与后端 `context.field_errors[].field` **同名**，
 * 于是"本地提示"和"服务端打回"画在同一处红字上，不需要两套渲染逻辑。
 */
export function localErrors(draft: PersonaDraft, limits: PersonaLimits): Record<string, string> {
  const errors: Record<string, string> = {};
  const name = checkLength(draft.name, "名称", limits.name);
  if (name) errors.name = name;
  const role = checkLength(draft.role_desc, "人设", limits.role_desc);
  if (role) errors.role_desc = role;
  const tone = checkLength(draft.tone, "口吻", limits.tone);
  if (tone) errors.tone = tone;
  const audience = checkLength(draft.audience, "受众", limits.audience);
  if (audience) errors.audience = audience;

  if (draft.catchphrases.length < limits.catchphrases.min!) {
    errors.catchphrases = `口癖至少 ${limits.catchphrases.min} 条`;
  } else if (draft.catchphrases.length > limits.catchphrases.max!) {
    errors.catchphrases = `口癖最多 ${limits.catchphrases.max} 条`;
  } else if (draft.catchphrases.some((item) => item.trim() === "")) {
    errors.catchphrases = "口癖不能有空行";
  }

  if (draft.forbidden.length < limits.forbidden.min!) {
    errors.forbidden = `禁区至少 ${limits.forbidden.min} 条`;
  } else if (draft.forbidden.length > limits.forbidden.max!) {
    errors.forbidden = `禁区最多 ${limits.forbidden.max} 条`;
  } else if (draft.forbidden.some((item) => item.trim() === "")) {
    errors.forbidden = "禁区不能有空行";
  }

  // 角色名可以留空（留空 = 不查这一条），所以只查条数与空行，不查下限。
  if (draft.speaker_names.length > limits.speaker_names.max!) {
    errors.speaker_names = `角色名最多 ${limits.speaker_names.max} 个`;
  } else if (draft.speaker_names.some((item) => item.trim() === "")) {
    errors.speaker_names = "角色名不能有空行";
  } else if (draft.speaker_names.some((item) => [...item].length > 16)) {
    errors.speaker_names = "每个角色名不超过 16 个字";
  }

  const min = limits.target_chars_min;
  if (draft.target_chars_min < min.min! || draft.target_chars_min > min.max!) {
    errors.target_chars_min = `篇幅下限要在 ${min.min}–${min.max} 之间`;
  }
  const max = limits.target_chars_max;
  if (draft.target_chars_max < max.min! || draft.target_chars_max > max.max!) {
    errors.target_chars_max = `篇幅上限要在 ${max.min}–${max.max} 之间`;
  }
  if (!errors.target_chars_min && !errors.target_chars_max) {
    if (draft.target_chars_min >= draft.target_chars_max) {
      errors.target_chars_max = "篇幅上限必须大于下限";
    }
  }
  const duration = limits.max_duration_ms;
  if (draft.max_duration_ms < duration.min! || draft.max_duration_ms > duration.max!) {
    errors.max_duration_ms = `时长上限要在 ${duration.min}–${duration.max} ms 之间`;
  }
  return errors;
}

/**
 * 服务端 422 的 `context.field_errors` ⇒ `{字段: 原因}`（能标到输入框上）。
 *
 * 拿不到就返回空对象 —— 调用方会退回显示整条 `message`（至少不丢信息）。
 */
export function fieldErrors(reason: unknown): Record<string, string> {
  if (!(reason instanceof ApiError)) return {};
  const detail = reason.detail;
  if (detail === null || typeof detail !== "object" || !("context" in detail)) return {};
  const context = (detail as { context: unknown }).context;
  if (context === null || typeof context !== "object" || !("field_errors" in context)) return {};
  const raw = (context as { field_errors: unknown }).field_errors;
  if (!Array.isArray(raw)) return {};
  const result: Record<string, string> = {};
  for (const item of raw) {
    if (item !== null && typeof item === "object" && "field" in item && "error" in item) {
      const field = String((item as { field: unknown }).field);
      result[field] = String((item as { error: unknown }).error);
    }
  }
  return result;
}

/** 库里那一条的状态灯（`active` 优先：他就是现在生效的那个）。 */
export function libraryTone(item: PersonaLibraryItem): StatusTone {
  if (!item.valid) return "error";
  return item.active ? "ok" : "idle";
}

/** 库里那一条的状态文案（无效条目**直接说原因**，不从列表里悄悄消失）。 */
export function libraryState(item: PersonaLibraryItem): string {
  if (!item.valid) return "不可用";
  return item.active ? "当前激活" : "可切换";
}

/** 备份的状态灯（坏备份照样列出来，但一眼能看出它不能用来回滚）。 */
export function backupTone(backup: PersonaBackup): StatusTone {
  return backup.valid ? "idle" : "error";
}

/** 备份那一行的人话（时间 + 是哪个人 + 坏没坏）。 */
export function describeBackup(backup: PersonaBackup): string {
  const base = `${backup.created_at} · ${backup.persona_id}`;
  return backup.valid ? base : `${base} · 已损坏`;
}

/** 一次写动作的结论（`changed=false` 也要说清楚"本来就是这样"）。 */
export function describeOutcome(outcome: PersonaOutcome): string {
  return outcome.changed ? outcome.note : `${outcome.note}`;
}

/** `system` 通道上要不要重拉（这条通道还有日志与告警，不能一律重拉）。 */
export function isPersonaEvent(kind: unknown): boolean {
  return kind === "system.persona_changed";
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const usePersonaStore = defineStore("persona", () => {
  const { status: wsStatus, cursor: wsCursor, connect, resync } = useWsConnection();

  const snapshot = ref<PersonaResponse | null>(null);
  const loading = ref(false);
  const loadError = ref<string | null>(null);

  const busy = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);

  /** 表单草稿（`null` ⇒ 还没有可编辑的底稿）。 */
  const draft = ref<PersonaDraft | null>(null);
  /** 服务端打回的逐字段红字（本地提示与它合并显示）。 */
  const serverFieldErrors = ref<Record<string, string>>({});
  /** 草稿是基于**旧一版**人物改的（底稿在编辑期间变过）。 */
  const draftStale = ref(false);

  // ── 派生 ───────────────────────────────────────────────────────────

  const active = computed(() => snapshot.value?.active ?? null);
  const activeError = computed<string | null>(() => snapshot.value?.active_error ?? null);
  const library = computed<PersonaLibraryItem[]>(() => snapshot.value?.library ?? []);
  const backups = computed<PersonaBackup[]>(() => snapshot.value?.backups ?? []);
  const limits = computed<PersonaLimits | null>(() => snapshot.value?.limits ?? null);
  const stale = computed<boolean>(() => active.value?.stale ?? false);
  const activePath = computed<string>(() => snapshot.value?.active_path ?? "");
  const libraryDir = computed<string>(() => snapshot.value?.library_dir ?? "");
  const backupDir = computed<string>(() => snapshot.value?.backup_dir ?? "");

  const localFieldErrors = computed<Record<string, string>>(() => {
    if (draft.value === null || limits.value === null) return {};
    return localErrors(draft.value, limits.value);
  });

  const visibleFieldErrors = computed<Record<string, string>>(() => ({
    ...localFieldErrors.value,
    ...serverFieldErrors.value,
  }));

  const dirty = computed<boolean>(() => {
    if (draft.value === null || active.value === null) return false;
    return isDirty(draft.value, active.value.config);
  });

  /** 能不能提交（有改动 + 本地没意见 + 没有在途动作）。 */
  const canSave = computed<boolean>(
    () => dirty.value && Object.keys(localFieldErrors.value).length === 0 && !busy.value,
  );

  const wsTone = computed<StatusTone>(() => {
    switch (wsStatus.value) {
      case "open":
        return "ok";
      case "connecting":
        return "busy";
      case "reconnecting":
        return "warn";
      case "stopped":
        return "error";
      default:
        return "idle";
    }
  });

  // ── 读 ──────────────────────────────────────────────────────────────

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
    serverFieldErrors.value = {};
  }

  /**
   * 拉一次全量；`resetDraft` 决定要不要用服务端那一份覆盖草稿。
   *
   * **默认不覆盖脏草稿**（裁定 162）：一条 `system.persona_changed` 就能把用户
   * 正在写的半屏字抹掉，而它恰好会在按"切换"的时候到达。此时只立一个
   * `draftStale` 旗子如实提示"底稿变了"，把决定权留给人。
   */
  async function refresh(options: { resetDraft?: boolean } = {}): Promise<void> {
    loading.value = true;
    try {
      snapshot.value = await api.fetchPersona();
      loadError.value = null;
      const config = snapshot.value.active?.config ?? null;
      if (config === null) {
        draft.value = null;
        draftStale.value = false;
      } else if (options.resetDraft === true || draft.value === null || !dirty.value) {
        draft.value = draftFrom(config);
        draftStale.value = false;
        serverFieldErrors.value = {};
      } else {
        draftStale.value = true;
      }
    } catch (failure) {
      // 旧快照**留着**：一屏数字不因一次抖动变空白（与总览台同一条）。
      loadError.value = describeError(failure);
    } finally {
      loading.value = false;
    }
  }

  /** 放弃编辑，回到服务端那一份。 */
  function resetDraft(): void {
    const config = active.value?.config ?? null;
    draft.value = config === null ? null : draftFrom(config);
    draftStale.value = false;
    serverFieldErrors.value = {};
  }

  // ── WS ──────────────────────────────────────────────────────────────

  let coalescer: ReturnType<typeof setTimeout> | null = null;

  function onSystem(envelope: Envelope): void {
    if (!isPersonaEvent(envelope.data.kind)) return;
    if (coalescer !== null) return;
    coalescer = setTimeout(() => {
      coalescer = null;
      void refresh();
    }, 300);
  }

  let wired = false;
  function wire(): void {
    if (wired) return;
    wired = true;
    useChannelStream("system", onSystem);
    watch(wsStatus, (next, previous) => {
      // 断线期间的事件是**真的丢了** ⇒ 重连成功必须以一次全量重拉收尾。
      if (next === "open" && previous !== "open") void refresh();
    });
  }

  // ── 动作 ────────────────────────────────────────────────────────────

  /** 保存表单（只发改动；校验不过一个字节都不会写）。 */
  async function save(reason: string | null = null): Promise<boolean> {
    if (draft.value === null || active.value === null) return false;
    const changes = dirtyChanges(draft.value, active.value.config);
    if (Object.keys(changes).length === 0) {
      notice.value = "没有改动，不需要保存";
      return false;
    }
    busy.value = true;
    clearMessages();
    try {
      const outcome = await api.updatePersona({ ...changes, reason });
      notice.value = describeOutcome(outcome);
      await refresh({ resetDraft: true });
      return true;
    } catch (failure) {
      const fields = fieldErrors(failure);
      serverFieldErrors.value = fields;
      error.value =
        Object.keys(fields).length > 0
          ? `${describeError(failure)}（${Object.entries(fields)
              .map(([field, why]) => `${field}：${why}`)
              .join("；")}）`
          : describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * 一键切换（旧版自动备份）。
   *
   * `backup` 显式传 `true`：它在契约里是**必填**（默认值不会进 `required`，
   * 但生成类型按后端签名给了必填 —— 裁定 69），而面板这一侧**永远**要备份。
   * `--no-backup` 那个逃生口是给 CLI 用的（跑批量脚本时不想刷一屏备份）。
   */
  async function activate(personaId: string, reason: string | null = null): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const outcome = await api.activatePersona({ persona_id: personaId, backup: true, reason });
      notice.value = describeOutcome(outcome);
      await refresh({ resetDraft: true });
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 把当前激活人物存进人物库。 */
  async function saveAs(
    personaId: string,
    overwrite = false,
    reason: string | null = null,
  ): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const outcome = await api.saveAsPersona({
        persona_id: personaId,
        overwrite,
        reason,
      });
      notice.value = describeOutcome(outcome);
      await refresh();
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /**
   * 回滚到某份备份。
   *
   * `backup: true` 同上 —— 回滚前也备份当前这份，于是"回滚错了"本身也可以回滚。
   */
  async function rollback(name: string, reason: string | null = null): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      const outcome = await api.rollbackPersona({ name, backup: true, reason });
      notice.value = describeOutcome(outcome);
      await refresh({ resetDraft: true });
      return true;
    } catch (failure) {
      error.value = describeError(failure);
      return false;
    } finally {
      busy.value = false;
    }
  }

  // ── 生命周期 ────────────────────────────────────────────────────────

  /** 幂等：重复调用只会有一次订阅、一份合并窗口。 */
  function start(): void {
    connect();
    wire();
    void refresh();
  }

  function stop(): void {
    if (coalescer !== null) {
      clearTimeout(coalescer);
      coalescer = null;
    }
  }

  return {
    // 读
    snapshot,
    active,
    activeError,
    library,
    backups,
    limits,
    stale,
    activePath,
    libraryDir,
    backupDir,
    loading,
    loadError,
    // 表单
    draft,
    dirty,
    canSave,
    draftStale,
    localFieldErrors,
    visibleFieldErrors,
    resetDraft,
    // 动作
    busy,
    error,
    notice,
    save,
    activate,
    saveAs,
    rollback,
    // 通道
    wsStatus,
    wsCursor,
    wsTone,
    refresh,
    start,
    stop,
    resync,
  };
});
