// 稿件面板 + 确认闸状态（T4.4 · §04.4.4）。
//
// 四条纪律
// --------
// 1. **放弃要二次确认**：它是唯一把人推向终局态的动作，而补救（捞回）要多走一步。
//    所以 `requestDiscard` 只记意图，`confirmDiscard` 才发请求。
// 2. **批量先看清单**：批量通过不是"按一下全放行"，而是先把"将放行哪 N 条"摊开，
//    确认后才发请求（todolist T4.4 的降级预案：批量误伤）。
// 3. **退回必填意见**：前端先拦一道（`canReject`），后端才是权威 —— 但让用户白跑
//    一次往返不值得。
// 4. **部分失败要如实显示**：批量里失败的那几条要连 `code` 一起摆出来，不能只说
//    "部分失败"（那样用户不知道该去修哪一条）。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  approveBatch,
  approveTask,
  discardTask,
  fetchApprovals,
  rejectTask,
  rescueTask,
  type ApprovalItem,
  type BatchFailure,
} from "@/api/endpoints/approvals";
import {
  fetchScript,
  fetchScriptDiff,
  fetchScriptVersions,
  type ScriptDetail,
  type ScriptDiff,
  type ScriptVersionList,
} from "@/api/endpoints/scripts";
import { ApiError } from "@/api/http";
import type { StatusTone } from "@/components/tone";

/** 一次决断的三种动作（与后端 `ApprovalDecision` 的值逐字一致）。 */
export type GateAction = "approve" | "reject" | "discard";

/** 队列的状态过滤（与后端 `approvals.status` 的 CHECK 同源）。 */
export type ApprovalStatus = "pending" | "approved" | "rejected" | "discarded";

/** 状态 → 中文标签（下拉与空态文案共用同一份）。 */
export const STATUS_LABELS: Record<ApprovalStatus, string> = {
  pending: "待审",
  approved: "已通过",
  rejected: "已退回",
  discarded: "已放弃",
};

/** 注入点（单测用假件替换，生产用真实现）。 */
export interface ScriptsApi {
  fetchApprovals: typeof fetchApprovals;
  fetchScript: typeof fetchScript;
  fetchScriptVersions: typeof fetchScriptVersions;
  fetchScriptDiff: typeof fetchScriptDiff;
  approveTask: typeof approveTask;
  rejectTask: typeof rejectTask;
  discardTask: typeof discardTask;
  approveBatch: typeof approveBatch;
  rescueTask: typeof rescueTask;
}

let api: ScriptsApi = {
  fetchApprovals,
  fetchScript,
  fetchScriptVersions,
  fetchScriptDiff,
  approveTask,
  rejectTask,
  discardTask,
  approveBatch,
  rescueTask,
};

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureScriptsApi(overrides: Partial<ScriptsApi>): void {
  api = { ...api, ...overrides };
}

// ── 纯函数（不碰响应式状态，单测直接调）────────────────────────────

/** 退回意见是否满足"必填"（只有空白等于没写）。 */
export function canReject(comment: string): boolean {
  return comment.trim().length > 0;
}

/** 勾选出来的待放行清单（保持队列顺序，而不是勾选顺序）。 */
export function batchPreview(
  queue: readonly ApprovalItem[],
  checked: readonly string[],
): ApprovalItem[] {
  const wanted = new Set(checked);
  return queue.filter((item) => wanted.has(item.task_id));
}

/** 评分色调：A 绿 / B 黄 / C 红 / 未知灰。 */
export function gradeTone(grade: string | null | undefined): StatusTone {
  switch (grade) {
    case "A":
      return "ok";
    case "B":
      return "warn";
    case "C":
      return "error";
    default:
      return "idle";
  }
}

/** diff 操作的中文标签（`op` 是后端的 `equal|replace|insert|delete`）。 */
export function diffOpLabel(op: string): string {
  switch (op) {
    case "replace":
      return "改";
    case "insert":
      return "增";
    case "delete":
      return "删";
    default:
      return "同";
  }
}

/**
 * 这条 WS 事件是否意味着「待审队列变了」（面板据此决定要不要重拉）。
 *
 * 只认这三类：`approval.requested` / `approval.decided` 是闸自己的事件，
 * `task.transition` 是任务进闸 / 出闸。`review.scored` **不**算 —— 打分更新不改
 * 队列内容，每打一次分就重拉一次列表纯属噪音（改稿期间一次任务能打两三轮）。
 */
export function isGateEvent(kind: unknown): boolean {
  return kind === "approval.requested" || kind === "approval.decided" || kind === "task.transition";
}

/** 把一次请求失败翻成人话（后端统一信封里的 `message` 已经在 `ApiError` 上）。 */
export function describeError(reason: unknown): string {
  if (reason instanceof ApiError) {
    return reason.status === 0 ? reason.message : `${reason.message}（HTTP ${reason.status}）`;
  }
  return String(reason);
}

/** 批量结果的一句话小结（面板顶部提示用）。 */
export function summarizeBatch(approved: number, failed: number): string {
  if (failed === 0) return `已放行 ${approved} 条`;
  return `放行 ${approved} 条，失败 ${failed} 条（见下方清单）`;
}

// ── store ──────────────────────────────────────────────────────────

export const useScriptsStore = defineStore("scripts", () => {
  const queue = ref<ApprovalItem[]>([]);
  const counts = ref<Record<string, number>>({});
  const status = ref<ApprovalStatus>("pending");
  const selectedTaskId = ref<string | null>(null);
  const detail = ref<ScriptDetail | null>(null);
  const versions = ref<ScriptVersionList | null>(null);
  const diff = ref<ScriptDiff | null>(null);
  const checked = ref<string[]>([]);
  /** 待确认的批量清单（非空 ⇒ 面板显示确认条，而不是直接发请求）。 */
  const pendingBatch = ref<ApprovalItem[] | null>(null);
  /** 待二次确认的放弃任务（非空 ⇒ 面板显示"再点一次"）。 */
  const pendingDiscard = ref<string | null>(null);
  const rejectComment = ref("");
  const loading = ref(false);
  const busy = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);
  const failures = ref<BatchFailure[]>([]);

  const pendingCount = computed(() => counts.value.pending ?? 0);
  const hasSelection = computed(() => checked.value.length > 0);

  function clearMessages(): void {
    error.value = null;
    notice.value = null;
    failures.value = [];
  }

  /** 拉待审列表；被选中的任务若已不在列表里，连详情一起清掉（避免看幽灵）。 */
  async function refresh(): Promise<void> {
    loading.value = true;
    try {
      const page = await api.fetchApprovals(status.value);
      queue.value = page.approvals;
      counts.value = page.counts;
      checked.value = checked.value.filter((id) => page.approvals.some((it) => it.task_id === id));
      if (selectedTaskId.value && !page.approvals.some((it) => it.task_id === selectedTaskId.value)) {
        selectedTaskId.value = null;
        detail.value = null;
        versions.value = null;
        diff.value = null;
      }
      error.value = null;
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      loading.value = false;
    }
  }

  /**
   * 换一个状态过滤（待审 ↔ 已放弃…）。
   *
   * 勾选与提示一并清掉：带着上一屏的勾选看新列表，"批量通过"按下去放行的是
   * 谁全靠记忆 —— 而"已放弃"那一屏里正好混着刚刚误点的那几条。
   */
  async function setStatus(next: ApprovalStatus): Promise<void> {
    if (next === status.value) return;
    status.value = next;
    clearChecked();
    clearMessages();
    await refresh();
  }

  /** 选中一个任务：一次把正文 / 评分 / 版本拉齐（避免拼出互相矛盾的视图）。 */
  async function select(taskId: string): Promise<void> {
    selectedTaskId.value = taskId;
    diff.value = null;
    rejectComment.value = "";
    pendingDiscard.value = null;
    clearMessages();
    try {
      const [script, list] = await Promise.all([
        api.fetchScript(taskId),
        api.fetchScriptVersions(taskId),
      ]);
      detail.value = script;
      versions.value = list;
    } catch (reason) {
      detail.value = null;
      versions.value = null;
      error.value = describeError(reason);
    }
  }

  function toggleChecked(taskId: string): void {
    checked.value = checked.value.includes(taskId)
      ? checked.value.filter((id) => id !== taskId)
      : [...checked.value, taskId];
    pendingBatch.value = null;
  }

  function clearChecked(): void {
    checked.value = [];
    pendingBatch.value = null;
  }

  /** 第一步：摊开清单（**不发请求**）。 */
  function requestBatch(): void {
    clearMessages();
    const picked = batchPreview(queue.value, checked.value);
    if (picked.length === 0) {
      error.value = "先勾选要放行的稿件";
      return;
    }
    pendingBatch.value = picked;
  }

  function cancelBatch(): void {
    pendingBatch.value = null;
  }

  /** 第二步：确认后才发请求（逐条结果都带回来）。 */
  async function confirmBatch(): Promise<void> {
    const picked = pendingBatch.value;
    if (picked === null) return;
    busy.value = true;
    try {
      const result = await api.approveBatch(picked.map((item) => item.task_id));
      notice.value = summarizeBatch(result.approved.length, result.failed.length);
      failures.value = result.failed;
      pendingBatch.value = null;
      checked.value = [];
      await refresh();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      busy.value = false;
    }
  }

  async function approve(taskId: string, comment?: string): Promise<boolean> {
    busy.value = true;
    clearMessages();
    try {
      await api.approveTask(taskId, comment);
      notice.value = "已放行，任务进入配音队列";
      await refresh();
      return true;
    } catch (reason) {
      error.value = describeError(reason);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 退回：意见为空**不发请求**（省一次注定 422 的往返）。 */
  async function reject(taskId: string): Promise<boolean> {
    if (!canReject(rejectComment.value)) {
      error.value = "退回必须写清理由：下一轮改稿只认这条意见";
      return false;
    }
    busy.value = true;
    clearMessages();
    try {
      await api.rejectTask(taskId, rejectComment.value.trim());
      notice.value = "已退回改稿";
      rejectComment.value = "";
      await refresh();
      return true;
    } catch (reason) {
      error.value = describeError(reason);
      return false;
    } finally {
      busy.value = false;
    }
  }

  /** 第一步：记下放弃意图（面板显示"再点一次"）。 */
  function requestDiscard(taskId: string): void {
    clearMessages();
    pendingDiscard.value = taskId;
  }

  function cancelDiscard(): void {
    pendingDiscard.value = null;
  }

  /** 第二步：确认放弃。 */
  async function confirmDiscard(): Promise<void> {
    const taskId = pendingDiscard.value;
    if (taskId === null) return;
    busy.value = true;
    try {
      await api.discardTask(taskId);
      notice.value = "已放弃；点错了可以捞回";
      pendingDiscard.value = null;
      await refresh();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      busy.value = false;
    }
  }

  /** 捞回（`discarded → pending`）—— 误点放弃的唯一补救。 */
  async function rescue(taskId: string, reason?: string): Promise<void> {
    busy.value = true;
    clearMessages();
    try {
      const outcome = await api.rescueTask(taskId, reason);
      notice.value = outcome.topic_restored ? "已捞回，选题候选一并恢复" : "已捞回";
      await refresh();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      busy.value = false;
    }
  }

  /** 拉两版逐句对照（两个版本号都必须显式给）。 */
  async function loadDiff(fromVersion: number, toVersion: number): Promise<void> {
    const taskId = selectedTaskId.value;
    if (taskId === null) return;
    try {
      diff.value = await api.fetchScriptDiff(taskId, fromVersion, toVersion);
      error.value = null;
    } catch (reason) {
      diff.value = null;
      error.value = describeError(reason);
    }
  }

  function clearDiff(): void {
    diff.value = null;
  }

  return {
    queue,
    counts,
    status,
    selectedTaskId,
    detail,
    versions,
    diff,
    checked,
    pendingBatch,
    pendingDiscard,
    rejectComment,
    loading,
    busy,
    error,
    notice,
    failures,
    pendingCount,
    hasSelection,
    refresh,
    setStatus,
    select,
    toggleChecked,
    clearChecked,
    requestBatch,
    cancelBatch,
    confirmBatch,
    approve,
    reject,
    requestDiscard,
    cancelDiscard,
    confirmDiscard,
    rescue,
    loadDiff,
    clearDiff,
  };
});
