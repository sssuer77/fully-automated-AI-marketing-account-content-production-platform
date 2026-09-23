//
// ⑬ 账号配置（T6.4 · §06.2.4 / §06.12）。
//
// 这一块要回答三个问题
// --------------------
// ① 我配了哪些号、它们现在什么状态？—— 清单（**含停用的**）+ 每行一句 `note`；
// ② 加一个号 / 改一个号 / 删一个号 —— 一个表单 + 两个动作；
// ③ 加完就能发了吗？—— **不能**：登录态要人工扫码一次（R13：不自动登录、不绕验证码），
//    所以每一行都带着"下一步做什么"，而不是让人去翻文档。
//
// 为什么这一块**不轮询**
// ----------------------
// 账号是配置文件里的东西，只有在这个面板上动手才会变（别的进程不会替你加号）。
// 与「定时计划」不同：那一个轮询的是**时间**，这一块轮询的是**我们自己的写入** ——
// 而写入就在本屏，写完当场刷新即可。
//
// 为什么表单的上下限与平台清单都来自后端
// --------------------------------------
// `limits` 是后端从 `AccountConfig` 的字段约束上现取的；平台清单与投递区块**同一份**
// （含"这个平台现在能不能投"）。前端各写一份的结果分别是"面板让填、后端拒收"和
// "给一个不会生效的平台配了账号，而面板显示一切正常"。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  deleteAccount,
  fetchAccounts,
  loginAccount,
  probeAccount,
  saveAccount,
  type PublishAccount,
  type PublishAccountBody,
  type PublishAccountHealthOutcome,
  type PublishAccountsView,
  type PublishPlatformOption,
} from "@/api/endpoints/publish";
import { describeError } from "@/stores/overview";
import { calibrationBadge, platformLabel } from "@/stores/publish";
import type { StatusTone } from "@/components/tone";

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface PublishAccountsApi {
  fetchAccounts: typeof fetchAccounts;
  saveAccount: typeof saveAccount;
  deleteAccount: typeof deleteAccount;
  probeAccount: typeof probeAccount;
  loginAccount: typeof loginAccount;
}

let api: PublishAccountsApi = {
  fetchAccounts,
  saveAccount,
  deleteAccount,
  probeAccount,
  loginAccount,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configurePublishAccountsApi(overrides: Partial<PublishAccountsApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 表单草稿（屏上的字段名与请求体**刻意不同**：前者给人看，后者给后端看）。 */
export interface AccountDraft {
  accountId: string;
  platform: string;
  displayName: string;
  profileDir: string;
  enabled: boolean;
  dailyLimit: number;
  minGapMin: number;
}

/** 新建表单的初值（默认限频与后端默认值一致：3 条/天、间隔 30 分钟）。 */
export function emptyAccountDraft(platform = ""): AccountDraft {
  return {
    accountId: "",
    platform,
    displayName: "",
    profileDir: "",
    enabled: true,
    dailyLimit: 3,
    minGapMin: 30,
  };
}

/** 盘上那一条 → 表单（改号时用；`profile_dir` 原样带出来，不替用户改成建议值）。 */
export function draftFromAccount(row: PublishAccount): AccountDraft {
  return {
    accountId: row.account_id,
    platform: row.platform,
    displayName: row.display_name ?? "",
    profileDir: row.profile_dir,
    enabled: row.enabled ?? true,
    dailyLimit: row.daily_limit ?? 3,
    minGapMin: row.min_gap_min ?? 30,
  };
}

/**
 * ``data/browser_profile/<account_id>`` —— 运行期**真正会用**的那个目录。
 *
 * 前缀来自后端（它随 `STUDIO_HOME` 走），面板不硬编码这个字符串。
 */
export function suggestProfileDir(prefix: string, accountId: string): string {
  const id = accountId.trim();
  if (prefix === "" || id === "") return "";
  return `${prefix.replace(/\/+$/, "")}/${id}`;
}

/**
 * 给新账号起一个没被占用的 id（面板把它填进输入框，人可以直接用或改掉）。
 *
 * 为什么值得替人起这个名字：**「账号 id 填什么」是这一屏最容易卡住的地方** ——
 * 它听起来像"平台的账号"，其实是"这份登录态在盘上的文件夹名"（自己起的名）。
 * 起一个 `acc_douyin` / `acc_douyin_2` 之后，"它是个代号"这件事**在动手之前**
 * 就说清楚了，而不是等用户去猜"我是不是该填手机号"。
 *
 * 只保证**不与现有 id 撞**：起名规则本身没有业务含义，撞了才是有意义的约束
 * （重复 id 会被后端拒掉，而那是一条本可以不必发生的失败）。
 */
export function suggestAccountId(existing: readonly string[], platform: string): string {
  const taken = new Set(existing);
  const slug = platform.trim() === "" ? "" : `_${platform.trim()}`;
  const base = `acc${slug}`;
  if (!taken.has(base)) return base;
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${base}_${index}`;
    if (!taken.has(candidate)) return candidate;
  }
  return `${base}_x`;
}

/** 表单 → 请求体（`profile_dir` 留空 ⇒ 发 `null`，由后端补上运行期那个路径）。 */
export function draftToBody(draft: AccountDraft): PublishAccountBody {
  const profileDir = draft.profileDir.trim();
  return {
    platform: draft.platform,
    display_name: draft.displayName.trim(),
    profile_dir: profileDir === "" ? null : profileDir,
    enabled: draft.enabled,
    daily_limit: draft.dailyLimit,
    min_gap_min: draft.minGapMin,
  };
}

/** 一个账号在屏上的名字（有显示名就用它，没有就用 id —— 不留空行）。 */
export function accountTitle(row: PublishAccount): string {
  const name = (row.display_name ?? "").trim();
  return name === "" ? row.account_id : name;
}

/** 限频那一行（两个数字都来自配置，不在这里算）。 */
export function limitText(row: PublishAccount): string {
  return `≤${row.daily_limit ?? "?"} 条/天 · 两条间隔 ≥${row.min_gap_min ?? "?"} 分钟`;
}

/**
 * 一行账号的灯色。
 *
 * 判据是**服务端算好的那三个事实**（停用 / 登录态目录在不在 / 平台启没启用），
 * 不是前端自己再猜一遍 —— 猜的那一份迟早与"投递时会不会跳过它"分叉。
 */
export function accountTone(row: PublishAccount): StatusTone {
  if (!(row.enabled ?? true)) return "idle";
  if (!(row.profile_dir_matches_runtime ?? true)) return "warn";
  if (!(row.profile_dir_exists ?? false)) return "warn";
  return "ok";
}

/** 平台下拉里的一项：`douyin · 抖音` 那一行 + "点了会怎样"。 */
export function platformOptionText(option: PublishPlatformOption): string {
  const who = option.accounts ?? [];
  const accounts = who.length > 0 ? who.join(" / ") : "没有启用的账号";
  // 校准状态也要写在这里：账号表单上的平台下拉框是**唯一**一处"我要给哪个平台配号"，
  // 而"这个平台的 CSS 是猜的"正是那一刻最该知道的事（配完号、扫完码、投出去才发现
  // 选择器全不命中，是最贵的一种顺序）。短话复用发布面板那一份。
  const badge = calibrationBadge(option);
  const tail = badge === "" ? "" : ` · ${badge}`;
  return `${platformLabel(option.code)} · ${accounts} · ${option.note}${tail}`;
}

// ══════════════════════════════════════════════════════════════════════
// Store
// ══════════════════════════════════════════════════════════════════════

export const usePublishAccountsStore = defineStore("publishAccounts", () => {
  const view = ref<PublishAccountsView | null>(null);
  const loading = ref(false);
  const saving = ref(false);
  const busyId = ref<string | null>(null);
  const error = ref("");
  const notice = ref("");
  const draft = ref<AccountDraft>(emptyAccountDraft());
  /** 正在改的账号 id；`null` ⇒ 表单处于"新建"状态。 */
  const editingId = ref<string | null>(null);
  /** 正在**等扫码**的那一行（`null` ⇒ 没有窗口开着）。与 `busyId` 分开：
   *  按钮上要写的话不一样 —— "等扫码中…"是在告诉用户"现在轮到你了"。 */
  const loginId = ref<string | null>(null);
  /** 正在填删除理由的那一行（`null` ⇒ 没有哪一行在等确认）。 */
  const reasonFor = ref<string | null>(null);
  const reasonDraft = ref<Record<string, string>>({});

  const accounts = computed<PublishAccount[]>(() => view.value?.accounts ?? []);
  const platforms = computed<PublishPlatformOption[]>(() => view.value?.platforms ?? []);
  const notes = computed<string[]>(() => view.value?.notes ?? []);
  const limits = computed<Record<string, number>>(() => view.value?.limits ?? {});
  const publishEnabled = computed<boolean>(() => view.value?.publish_enabled ?? false);
  const requireConfirm = computed<boolean>(() => view.value?.require_confirm ?? true);
  const editing = computed<boolean>(() => editingId.value !== null);
  /** 新建表单里那个 id 的建议值（一个没被占用的 `acc_<平台>`）。 */
  const suggestedAccountId = computed<string>(() =>
    suggestAccountId(
      accounts.value.map((row) => row.account_id),
      draft.value.platform,
    ),
  );
  /** 表单里那个 `profile_dir` 的建议值（留空时后端就用它）。 */
  const suggestedProfileDir = computed<string>(() =>
    suggestProfileDir(view.value?.profile_dir_prefix ?? "", draft.value.accountId),
  );

  function firstPlatform(): string {
    return platforms.value[0]?.code ?? "";
  }

  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      view.value = await api.fetchAccounts();
      error.value = "";
      if (draft.value.platform === "") {
        draft.value = { ...draft.value, platform: firstPlatform() };
      }
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      loading.value = false;
    }
  }

  /** 面板挂载时调一次（**不轮询**：见模块注释）。 */
  function start(): void {
    void refresh();
  }

  function beginCreate(): void {
    const platform = firstPlatform();
    editingId.value = null;
    // id **先替人起一个**：这一栏最容易卡住（听起来像"平台的账号"），而它其实
    // 只是这份登录态在盘上的文件夹名。人可以直接用，也可以改成自己认得的名字。
    // 建议值按**即将选中的那个平台**算（不是按表单里那个旧的）—— 否则"先点平台再点加号"
    // 会得到一个属于上一个平台的建议名。
    draft.value = {
      ...emptyAccountDraft(platform),
      accountId: suggestAccountId(
        accounts.value.map((row) => row.account_id),
        platform,
      ),
    };
    notice.value = "";
    error.value = "";
  }

  function beginEdit(row: PublishAccount): void {
    editingId.value = row.account_id;
    draft.value = draftFromAccount(row);
    notice.value = "";
    error.value = "";
  }

  function cancelEdit(): void {
    editingId.value = null;
    draft.value = emptyAccountDraft(firstPlatform());
  }

  function setDraft(patch: Partial<AccountDraft>): void {
    draft.value = { ...draft.value, ...patch };
  }

  /** 点「删除」⇒ 先在那一行下面开一个理由输入框（删号也要留一句"为什么"）。 */
  function openReason(accountId: string): void {
    reasonFor.value = accountId;
    notice.value = "";
    error.value = "";
  }

  function closeReason(): void {
    reasonFor.value = null;
  }

  function setReason(accountId: string, value: string): void {
    reasonDraft.value = { ...reasonDraft.value, [accountId]: value };
  }

  async function save(): Promise<void> {
    const accountId = draft.value.accountId.trim();
    if (accountId === "") {
      // 只挡这一条：空 id 连请求都拼不出来（`PUT /accounts/` 会落到别的路由上），
      // 而其余判据一律留给后端（它给的 422 带逐字段的错误，前端再算一遍只会分叉）。
      error.value = "先给它起个 id（1–64 个字符，例如 acc_second）";
      return;
    }
    saving.value = true;
    notice.value = "";
    try {
      const outcome = await api.saveAccount(accountId, draftToBody(draft.value));
      notice.value = outcome.created
        ? `已加：${accountId}。它现在还发不出去 —— 先人工扫码一次（下面那行命令）。`
        : outcome.changed
          ? `已改：${accountId}`
          : `没变：${accountId}（同一个值存两次不会写盘，也不会在留痕里多一行）`;
      error.value = "";
      editingId.value = null;
      draft.value = emptyAccountDraft(firstPlatform());
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      saving.value = false;
    }
  }

  async function toggleEnabled(row: PublishAccount): Promise<void> {
    busyId.value = row.account_id;
    try {
      const body = draftToBody({ ...draftFromAccount(row), enabled: !(row.enabled ?? true) });
      await api.saveAccount(row.account_id, body);
      notice.value = body.enabled ? `已启用：${row.account_id}` : `已停用：${row.account_id}`;
      error.value = "";
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  async function remove(row: PublishAccount): Promise<void> {
    busyId.value = row.account_id;
    try {
      const reason = (reasonDraft.value[row.account_id] ?? "").trim();
      await api.deleteAccount(row.account_id, reason === "" ? null : reason);
      // 说清楚"什么没被删"：登录态目录是凭据，面板不碰它（§02.5）。
      notice.value = `已从配置里删掉：${row.account_id}（登录态目录没动，要清得自己去删）`;
      error.value = "";
      reasonFor.value = null;
      if (editingId.value === row.account_id) cancelEdit();
      await refresh();
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  /** 把"整屏"接过来 —— 探测 / 扫码登录回的也是整屏（与写操作同一个形状）。 */
  function applyOutcome(outcome: PublishAccountHealthOutcome): void {
    view.value = outcome;
    error.value = "";
  }

  /**
   * 探一眼登录态（无头；**十几秒是正常的** —— 它要冷启动一个浏览器再打开平台页面）。
   *
   * 它回答的是"现在能不能发" —— 面板上那一行原本只会说"登录过（会话是否仍有效，
   * 要真发一次才知道）"，而那句话对"我现在就想知道"是不够的。探测把那个不确定性
   * 收窄成一个当场可得的答案，**不需要真发一条**（R14 不可逆）。
   */
  async function probe(row: PublishAccount): Promise<void> {
    busyId.value = row.account_id;
    try {
      const outcome = await api.probeAccount(row.account_id);
      applyOutcome(outcome);
      notice.value = `${accountTitle(row)}：${outcome.note}`;
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
    }
  }

  /**
   * 扫码登录：开一个**可见窗口**等人扫，扫完窗口自己关掉。
   *
   * 这一步会占住这一行好几分钟（人在掏手机），所以 `loginId` 单独一个状态：
   * 按钮要在这期间写成"等扫码中…"，并告诉用户**去哪扫** —— 窗口开在跑着服务的
   * 那台电脑上，而人很可能正盯着这块面板等。
   */
  async function login(row: PublishAccount): Promise<void> {
    busyId.value = row.account_id;
    loginId.value = row.account_id;
    notice.value = "";
    error.value = "";
    try {
      const outcome = await api.loginAccount(row.account_id);
      applyOutcome(outcome);
      notice.value = `${accountTitle(row)}：${outcome.note}`;
    } catch (cause) {
      error.value = describeError(cause);
    } finally {
      busyId.value = null;
      loginId.value = null;
    }
  }

  return {
    view,
    loading,
    saving,
    busyId,
    error,
    notice,
    draft,
    editingId,
    loginId,
    reasonFor,
    reasonDraft,
    accounts,
    platforms,
    notes,
    limits,
    publishEnabled,
    requireConfirm,
    editing,
    suggestedAccountId,
    suggestedProfileDir,
    refresh,
    start,
    beginCreate,
    beginEdit,
    cancelEdit,
    setDraft,
    openReason,
    closeReason,
    setReason,
    save,
    toggleEnabled,
    remove,
    probe,
    login,
  };
});
