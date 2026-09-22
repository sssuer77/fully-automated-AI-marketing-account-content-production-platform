// 设置面板状态（T6.1）—— LLM 通道与密钥。
//
// 这一屏要回答四个问题
// --------------------
// ① 现在配了没有、哪来的？—— 密钥状态（掩码 + 来源 + 文件路径）；
// ② 有哪几条通道、哪条能用、缺什么？—— 通道卡片；
// ③ 每个 Agent 走哪条？—— 路由表（只读，改它属于 `llm.yaml`）；
// ④ 填完真的通吗？—— 按需探测（只发只读 GET）。
//
// 为什么前端**也**做一次长度校验
// ----------------------------
// 唯一裁判是服务端（`validate_api_key`）。前端这一份只做**长度与空白**这两件
// 能立刻回答的事，为的是让红字在输入框下面即时出现，而不是等一个来回。
// 判据来自服务端下发的 `limits` —— 前端不抄任何阈值，所以改后端这里跟着变。
//
// 为什么「环境变量优先」要单独给一个 `envOverridden`
// ------------------------------------------------
// 环境变量占着位置时，用户在面板里存的那份**不生效**。这件事不说出来，他会以为
// 面板坏了 —— 而其实只需要去改环境变量。面板据此把那一行标黄并说明。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  clearLlmKey,
  fetchLlmSettings,
  probeLlm,
  saveLlmKey,
  saveLlmProfile,
  type LlmKeyOutcome,
  type LlmKeyStatus,
  type LlmProbe,
  type LlmProbeRow,
  type LlmProfile,
  type LlmProfileBody,
  type LlmProfileOutcome,
  type LlmSettings,
} from "@/api/endpoints/settings";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";

/** 密钥长度上下限的**兜底**（服务端没答上来时用；正常一律以 `data.limits` 为准）。 */
export const FALLBACK_KEY_LIMITS = { min_len: 8, max_len: 512 } as const;

export interface KeyLimits {
  min_len: number;
  max_len: number;
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（单测直接打这些）
// ══════════════════════════════════════════════════════════════════════

/**
 * 输入框里的即时判据：返回一句人话，或 `null`（本地看不出问题）。
 *
 * **空串不算问题**：用户刚清空输入框准备粘贴时，弹一条红字只会碍事。
 * 真正提交空串由服务端拒绝（那句「不能为空（要清除请点「清除密钥」）」更准确）。
 */
export function keyInputProblem(raw: string, limits: KeyLimits): string | null {
  const text = raw.trim();
  if (text === "") return null;
  if (text.length < limits.min_len) {
    return `太短（${text.length} 个字符，至少 ${limits.min_len} 个）—— 多半是没贴全`;
  }
  if (text.length > limits.max_len) {
    return `太长（${text.length} 个字符，上限 ${limits.max_len}）`;
  }
  if (/\s/.test(text)) return "中间有空格或换行 —— 重新复制一次";
  return null;
}

/** 密钥状态 → 色调（配好了是绿的，没配是黄的，读不出来是红的）。 */
export function keyTone(status: LlmKeyStatus | null): StatusTone {
  if (status === null) return "idle";
  if (status.last_error !== null) return "error";
  return status.configured ? "ok" : "warn";
}

/** 通道卡片 → 色调。 */
export function profileTone(profile: LlmProfile): StatusTone {
  return profile.usable ? "ok" : "warn";
}

/** 探测结果 → 色调。 */
export function probeTone(row: LlmProbeRow): StatusTone {
  if (row.status === "ok") return "ok";
  if (row.status === "no_key") return "warn";
  return "error";
}

/** 一次探测的一句话结论（面板标题栏用）。 */
export function probeSummary(probe: LlmProbe | null): string {
  if (probe === null) return "尚未测试";
  const total = probe.rows.length;
  if (total === 0) return "没有可测的通道";
  if (probe.ok_count === total) return `${total} 条通道全部连通`;
  return `${probe.ok_count} / ${total} 条通道连通`;
}

/** 存完之后给用户的一句话（说清楚**做了什么**、**存到哪**、**有没有生效**）。 */
export function saveNotice(outcome: LlmKeyOutcome): string {
  if (!outcome.changed) return "和现在这份一样，没有改动";
  if (outcome.cleared) return "已清除密钥";
  const where = outcome.key.source === "env" ? "环境变量" : "config/secrets.yaml";
  if (outcome.key.source === "env") {
    return `已保存，但当前生效的是环境变量 ${outcome.key.env_var} —— 把那个变量清掉之后这份才会生效`;
  }
  return `已保存到 ${where}，立刻生效（不需要重启）`;
}

/**
 * 改完通道参数之后的一句话（说清楚**改成了什么**、**什么时候生效**）。
 *
 * 为什么必须说"立刻生效"：这一条与密钥不同（密钥靠每次现取，模型名靠 mtime 热重载），
 * 不说的话用户会去重启 worker —— 而重启解决不了任何问题，只会打断在跑的任务。
 */
export function profileNotice(outcome: LlmProfileOutcome): string {
  if (!outcome.changed) return `${outcome.profile} 与现在这一份相同，没有改动`;
  return `${outcome.profile} 已改为 ${outcome.model}，立刻生效（常驻 worker 不需要重启）`;
}

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface SettingsApi {
  fetchLlmSettings: typeof fetchLlmSettings;
  saveLlmKey: typeof saveLlmKey;
  clearLlmKey: typeof clearLlmKey;
  saveLlmProfile: typeof saveLlmProfile;
  probeLlm: typeof probeLlm;
}

let api: SettingsApi = { fetchLlmSettings, saveLlmKey, clearLlmKey, saveLlmProfile, probeLlm };

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureSettingsApi(overrides: Partial<SettingsApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

export const useSettingsStore = defineStore("settings", () => {
  const data = ref<LlmSettings | null>(null);
  const probe = ref<LlmProbe | null>(null);
  const loading = ref(false);
  const saving = ref(false);
  //: 正在保存**哪条通道**（``null`` ⇒ 没有在保存）—— 面板据此只转那一行的按钮
  const savingProfile = ref<string | null>(null);
  const probing = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);

  const limits = computed<KeyLimits>(() => data.value?.limits ?? FALLBACK_KEY_LIMITS);
  const key = computed<LlmKeyStatus | null>(() => data.value?.key ?? null);
  const envOverridden = computed(() => data.value?.key.env_overrides_file ?? false);

  function reset(): void {
    data.value = null;
    probe.value = null;
    error.value = null;
    notice.value = null;
  }

  async function load(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      data.value = await api.fetchLlmSettings();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      loading.value = false;
    }
  }

  /**
   * 保存密钥。**不做本地拦截**（本地只用来显示红字）：真正的判定在服务端，
   * 前端提前 return 会让「服务端到底怎么说」永远看不到。
   */
  async function saveKey(raw: string, reason?: string): Promise<boolean> {
    saving.value = true;
    error.value = null;
    notice.value = null;
    try {
      const outcome = await api.saveLlmKey(raw.trim(), reason);
      data.value = outcome;
      notice.value = saveNotice(outcome);
      return true;
    } catch (reason_) {
      error.value = describeError(reason_);
      return false;
    } finally {
      saving.value = false;
    }
  }

  async function clearKey(reason?: string): Promise<boolean> {
    saving.value = true;
    error.value = null;
    notice.value = null;
    try {
      const outcome = await api.clearLlmKey(reason);
      data.value = outcome;
      notice.value = saveNotice(outcome);
      return true;
    } catch (reason_) {
      error.value = describeError(reason_);
      return false;
    } finally {
      saving.value = false;
    }
  }

  /**
   * 改通道参数（模型名 / base_url）。
   *
   * 两个字段**都是选填**：只改模型名就只传模型名。传空串等于"没给"（后端 422），
   * 所以这里在提交前先摘掉空值 —— 但**不做别的本地拦截**：真正的判定在服务端。
   */
  async function saveProfile(profile: string, model: string, baseUrl: string): Promise<boolean> {
    const body: LlmProfileBody = { profile };
    const trimmedModel = model.trim();
    const trimmedUrl = baseUrl.trim();
    if (trimmedModel !== "") body.model = trimmedModel;
    if (trimmedUrl !== "") body.base_url = trimmedUrl;
    if (body.model === undefined && body.base_url === undefined) {
      error.value = "模型名与 base_url 至少填一个";
      return false;
    }

    savingProfile.value = profile;
    error.value = null;
    notice.value = null;
    try {
      const outcome = await api.saveLlmProfile(body);
      data.value = outcome;
      notice.value = profileNotice(outcome);
      return true;
    } catch (reason_) {
      error.value = describeError(reason_);
      return false;
    } finally {
      savingProfile.value = null;
    }
  }

  async function runProbe(): Promise<void> {
    probing.value = true;
    error.value = null;
    try {
      probe.value = await api.probeLlm();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      probing.value = false;
    }
  }

  return {
    data,
    probe,
    loading,
    saving,
    probing,
    error,
    notice,
    limits,
    key,
    envOverridden,
    savingProfile,
    reset,
    load,
    saveKey,
    clearKey,
    saveProfile,
    runProbe,
  };
});
