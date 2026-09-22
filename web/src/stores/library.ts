// 成片库面板状态（T5.11 · §06.11）。
//
// 这一屏要回答三个问题
// --------------------
// ① 我手里有哪些成片？② 它们各自发到哪儿了？③ 我要把哪几条一次发出去？
//
// 为什么"一行 = 一条任务"这件事在面板上要说出来
// ---------------------------------------------
// 发布池认的是**任务号**：它自己拿 `resolve_final_video(task_id)` 去找成片
// （manifest 优先、目录兜底），不认面板上选的是哪个文件。所以一条任务重出过三版时，
// 面板按文件列三行、勾哪一行结果都一样 —— 那是最难查的一种谎话（"我明明选了新那版"）。
// 一行一条任务，把 `versions` 当**信息**摆出来，不当**选择**。
//
// 为什么发布成功后不自动跳去发布面板
// ----------------------------------
// 投递只把作业排进池子（**立刻返回**），真正发是 publish 池干的，要几秒到几十秒。
// 自动跳走会让人以为"已经发完了"，而那时它多半还在 `queued`。所以留在这屏，
// 把那一条的结果写在行上，并给一句"去发布面板看进度"。
//
// 为什么**不轮询**
// ----------------
// 这一屏看的是"盘上有什么"—— 它只在人出片 / 删片时变。空闲时每两秒拉一次全库，
// 拉回来的是同一份东西。所以靠"进来拉一次 + 一颗刷新"，代价说清楚：
// **别人在命令行刚出的那一条不会自己冒出来**。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import {
  fetchLibrary,
  publishSelected,
  type LibraryBatchItem,
  type LibraryItem,
  type LibraryPublishBody,
  type LibraryResponse,
} from "@/api/endpoints/library";
import { fetchPlatforms, type PublishPlatformsView } from "@/api/endpoints/publish";
import { describeError } from "@/stores/overview";
import type { StatusTone } from "@/components/tone";

/** 一次最多勾几条（与后端 `BATCH_MAX` 同源；面板据此禁用而不是让人点了报 422）。 */
export const BATCH_MAX = 50;

// ══════════════════════════════════════════════════════════════════════
// 注入点（单测用假件替换，生产用真实现）
// ══════════════════════════════════════════════════════════════════════

export interface LibraryApi {
  fetchLibrary: typeof fetchLibrary;
  fetchPlatforms: typeof fetchPlatforms;
  publishSelected: typeof publishSelected;
}

let api: LibraryApi = { fetchLibrary, fetchPlatforms, publishSelected };

/** 换掉部分实现（**只用于测试**：生产代码不调用它）。 */
export function configureLibraryApi(overrides: Partial<LibraryApi>): void {
  api = { ...api, ...overrides };
}

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态，单测直接调）
// ══════════════════════════════════════════════════════════════════════

/** 发布状态 → 中文（面板上不出现裸英文状态码）。 */
const PUBLICATION_LABELS: Record<string, string> = {
  queued: "排队中",
  uploading: "发布中",
  published: "已发布",
  failed: "失败",
  manual_required: "待人工",
  canceled: "已取消",
};

export function publicationLabel(status: string): string {
  return PUBLICATION_LABELS[status] ?? status;
}

export function publicationTone(status: string): StatusTone {
  switch (status) {
    case "published":
      return "ok";
    case "failed":
      return "error";
    case "manual_required":
      return "warn";
    case "queued":
    case "uploading":
      return "busy";
    default:
      return "idle";
  }
}

/**
 * 这一行"发到哪儿了"的一句话。
 *
 * **按平台数，不按记录数**：一条任务发到两个平台就是两条记录，而人关心的是
 * "发出去几个地方"。一条都没发过 ⇒ `未发布`（不是空字符串 —— 空白列会被读成"没加载完"）。
 */
export function publishedText(item: LibraryItem): string {
  if (item.publications.length === 0) return "未发布";
  const parts = item.publications.map(
    (row) => `${row.platform}/${row.account_id}·${publicationLabel(row.status)}`,
  );
  return parts.join("，");
}

/** 这一行发出去过几个平台（"只看未发布"这个筛选用它）。 */
export function publishedCount(item: LibraryItem): number {
  return item.publications.filter((row) => row.status === "published").length;
}

/** 这一行现在能不能投（任务还在、成片还在盘上）。 */
export function canPublish(item: LibraryItem): boolean {
  return item.task_found;
}

/** 不能投时的那句话（能投 ⇒ `null`）。 */
export function blockedReason(item: LibraryItem): string | null {
  if (!item.task_found) return "这条任务在库里已经没有了（只剩片子）⇒ 发布池读不到它，发不了";
  return null;
}

/**
 * 一条批量投递结论 → 人话。
 *
 * 三种"没投出去"分开说（与后端 `EnqueueReport` 同一条）：**早就投过**的处置动作是
 * "什么都不用做"，而其余跳过要人去改配置。合成一句"跳过 2 条"之后，操作员只能靠猜。
 */
export function resultText(item: LibraryBatchItem): string {
  const parts: string[] = [];
  if (item.missing) parts.push("任务不存在");
  if (item.queued > 0) parts.push(`已排入 ${item.queued} 条作业`);
  if (item.duplicates.length > 0) parts.push(`早就投过：${item.duplicates.join("、")}（无需处理）`);
  const rest = item.skipped.filter((line) => !item.duplicates.some((dup) => line.includes(dup)));
  if (rest.length > 0) parts.push(`跳过：${rest.join("；")}`);
  return parts.length > 0 ? parts.join("；") : "没有变化";
}

/** 勾选 / 全选的判据（纯函数：面板与单测共用一份）。 */
export function nextSelection(current: readonly string[], taskId: string, checked: boolean): string[] {
  if (checked) return current.includes(taskId) ? [...current] : [...current, taskId];
  return current.filter((id) => id !== taskId);
}

// ══════════════════════════════════════════════════════════════════════
// store
// ══════════════════════════════════════════════════════════════════════

export const useLibraryStore = defineStore("library", () => {
  const snapshot = ref<LibraryResponse | null>(null);
  const platforms = ref<PublishPlatformsView | null>(null);
  const selected = ref<string[]>([]);
  const chosenPlatforms = ref<string[]>([]);
  const onlyUnpublished = ref(false);
  const loading = ref(false);
  const publishing = ref(false);
  const error = ref<string | null>(null);
  const notice = ref<string | null>(null);
  /** 上一次批量投递的逐条结论（按任务号索引）。 */
  const results = ref<Record<string, LibraryBatchItem>>({});

  const items = computed<LibraryItem[]>(() => snapshot.value?.items ?? []);

  /** 面板上真正列出来的那些（"只看未发布"是一个**显示**筛选，不动选择）。 */
  const visible = computed<LibraryItem[]>(() =>
    onlyUnpublished.value ? items.value.filter((item) => publishedCount(item) === 0) : items.value,
  );

  const selectableIds = computed<string[]>(() =>
    visible.value.filter(canPublish).map((item) => item.task_id),
  );

  const selectedItems = computed<LibraryItem[]>(() =>
    items.value.filter((item) => selected.value.includes(item.task_id)),
  );

  /** 勾中的条数超过上限 ⇒ 面板禁用提交（而不是让人点了收 422）。 */
  const overLimit = computed(() => selected.value.length > BATCH_MAX);

  const canSubmit = computed(
    () =>
      !publishing.value &&
      selected.value.length > 0 &&
      !overLimit.value &&
      chosenPlatforms.value.length > 0,
  );

  /** 能选的平台（`selectable=false` 的点了会被跳过，所以不进这一列）。 */
  const platformOptions = computed(() =>
    (platforms.value?.items ?? []).filter((option) => option.selectable),
  );

  /**
   * 勾了「真平台」而发布开关是关的 ⇒ 一句必须显眼说出来的话（没有要说的话 ⇒ `null`）。
   *
   * 这一档是**出厂默认**（`config/publish.yaml → enabled: false`），而它的真实后果是
   * "作业直接死信、发布面板上什么都不出现"。不说这句，操作员按一次「批量发布」得到的是
   * "什么都没发生" —— 最容易被读成"按钮坏了"的一种。判据与发布面板同源（服务端给的
   * `publish_enabled`），文案各写一份是因为两屏的下一步动作不同。
   */
  const publishWarning = computed<string | null>(() => {
    const view = platforms.value;
    if (view === null || view.publish_enabled) return null;
    const picked = platformOptions.value.filter(
      (option) => chosenPlatforms.value.includes(option.code) && !option.rehearsal,
    );
    if (picked.length === 0) return null;
    const names = picked.map((option) => option.code).join(" / ");
    return (
      `发布开关是关的（config/publish.yaml → enabled: false）：${names} 投出去会**直接死信**，` +
      "发布面板上不会出现记录（去「四池调度」看死信）。只验证链路请改勾「本地演练台」。"
    );
  });

  /** 勾选状态是否"全选"（用于表头那个复选框的三态）。 */
  const allSelected = computed(
    () =>
      selectableIds.value.length > 0 &&
      selectableIds.value.every((id) => selected.value.includes(id)),
  );

  /** 提交前的代价提示：这次会投几条、去哪些平台。 */
  const submitHint = computed(() => {
    if (selected.value.length === 0) return "先在左边勾一条以上";
    if (chosenPlatforms.value.length === 0) return "选一个目标平台";
    const names = chosenPlatforms.value
      .map((code) => platformOptions.value.find((option) => option.code === code)?.code ?? code)
      .join("、");
    return `将把 ${selected.value.length} 条任务投到 ${names}`;
  });

  async function load(signal?: AbortSignal): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const [library, options] = await Promise.all([
        api.fetchLibrary({ signal }),
        api.fetchPlatforms(signal),
      ]);
      snapshot.value = library;
      platforms.value = options;
      // 勾选要跟着列表走：库里已经没有的那些 id 留着，提交时会被后端判 missing，
      // 而面板上那一行根本不在 —— 用户会看到"投了 3 条，报错 4 条"。
      const alive = new Set(library.items.map((item) => item.task_id));
      selected.value = selected.value.filter((id) => alive.has(id));
      if (chosenPlatforms.value.length === 0) {
        // 默认勾"出厂默认平台"里**这次真能选**的那几个（`selectable=false` 的勾了也是白勾）。
        // 这两个字段在契约里都可缺省，所以都走 `?? []`。
        const selectable = new Set(platformOptions.value.map((option) => option.code));
        const defaults = options.default_platforms ?? [];
        chosenPlatforms.value = defaults.filter((code) => selectable.has(code));
      }
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      loading.value = false;
    }
  }

  function toggle(taskId: string, checked: boolean): void {
    selected.value = nextSelection(selected.value, taskId, checked);
    // 上一次的结论跟着选择一起清：留着它，新勾的那一条会顶着一条旧结论，
    // 而那结论说的是另一条任务。
    const rest = { ...results.value };
    delete rest[taskId];
    results.value = rest;
  }

  function toggleAll(checked: boolean): void {
    selected.value = checked ? [...selectableIds.value] : [];
    results.value = {};
  }

  function togglePlatform(code: string, checked: boolean): void {
    chosenPlatforms.value = nextSelection(chosenPlatforms.value, code, checked);
  }

  async function submit(): Promise<void> {
    if (!canSubmit.value) return;
    publishing.value = true;
    error.value = null;
    notice.value = null;
    try {
      const body: LibraryPublishBody = {
        task_ids: selected.value,
        platforms: chosenPlatforms.value,
      };
      const report = await api.publishSelected(body);
      const fresh: Record<string, LibraryBatchItem> = {};
      for (const item of report.items) fresh[item.task_id] = item;
      results.value = fresh;
      notice.value =
        report.queued_total > 0
          ? `已排入 ${report.queued_total} 条发布作业 —— 去「发布」那一屏看它发出去`
          : "一条都没排进去，逐条原因见下面每一行";
      // 重新拉一次：这一次投出去的会在 publications 里留下 `queued` 那一行，
      // 列表上"发到哪儿了"那一列要跟着变。
      await load();
    } catch (reason) {
      error.value = describeError(reason);
    } finally {
      publishing.value = false;
    }
  }

  return {
    snapshot,
    platforms,
    selected,
    chosenPlatforms,
    onlyUnpublished,
    loading,
    publishing,
    error,
    notice,
    results,
    items,
    visible,
    selectableIds,
    selectedItems,
    overLimit,
    canSubmit,
    platformOptions,
    publishWarning,
    allSelected,
    submitHint,
    load,
    toggle,
    toggleAll,
    togglePlatform,
    submit,
  };
});
