import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ApprovalItem,
  ApprovalList,
  ApprovalResult,
  BatchResult,
  RescueResult,
} from "@/api/endpoints/approvals";
import type { ScriptDetail, ScriptDiff, ScriptVersionList } from "@/api/endpoints/scripts";
import { ApiError } from "@/api/http";
import {
  batchPreview,
  canReject,
  configureScriptsApi,
  describeError,
  diffOpLabel,
  gradeTone,
  isGateEvent,
  summarizeBatch,
  useScriptsStore,
  type ScriptsApi,
} from "./scripts";

// ══════════════════════════════════════════════════════════════════════
// 假件（字段全部从生成的契约类型里取 ⇒ 后端一改字段，这里跟着炸）
// ══════════════════════════════════════════════════════════════════════

function item(taskId: string, overrides: Partial<ApprovalItem> = {}): ApprovalItem {
  return {
    id: `ap-${taskId}`,
    task_id: taskId,
    script_id: `sc-${taskId}`,
    status: "pending",
    grade: "B",
    score_total: 6.4,
    revision_round: 0,
    requested_at: "2026-09-14T00:00:00Z",
    decided_at: null,
    decided_by: null,
    comment: null,
    auto_expire_at: null,
    ...overrides,
  };
}

function page(items: ApprovalItem[]): ApprovalList {
  return { approvals: items, counts: { pending: items.length }, limit: 200 };
}

function result(taskId: string, decision: string): ApprovalResult {
  return {
    task_id: taskId,
    approval_id: `ap-${taskId}`,
    decision,
    status: decision,
    grade: "B",
    score_total: 6.4,
    revision_round: 0,
    comment: null,
  };
}

function batch(ids: readonly string[]): BatchResult {
  return { requested: ids.length, approved: ids.map((id) => result(id, "approved")), failed: [] };
}

function rescue(taskId: string): RescueResult {
  return {
    task_id: taskId,
    status: "pending",
    previous_status: "discarded",
    revision_round: 0,
    topic_id: `tp-${taskId}`,
    topic_restored: true,
  };
}

function detail(taskId: string, overrides: Partial<ScriptDetail> = {}): ScriptDetail {
  return {
    task_id: taskId,
    task_status: "awaiting_approval",
    revision_round: 1,
    script: {
      id: `sc-${taskId}`,
      version: 2,
      is_active: true,
      title: "标题",
      hook: "钩子",
      body_md: "正文",
      cta: "CTA",
      word_count: 700,
      est_duration_ms: 180000,
      target_chars: 700,
      grade: "B",
      score_total: 6.4,
      score_rule: 7,
      score_llm: 6.1,
      revision_round: 1,
      editor_notes: [],
      outline: {},
      speaker_ratio: {},
      llm_model: "qwen",
      prompt_version: "p1",
      created_at: null,
    },
    sentences: [{ seq: 1, text: "一句", speaker: "xiongda", emotion: "neutral", pause_after_ms: 200 }],
    reviews: [],
    approval: {
      id: `ap-${taskId}`,
      task_id: taskId,
      script_id: `sc-${taskId}`,
      status: "pending",
      grade: "B",
      score_total: 6.4,
      revision_round: 1,
      requested_at: null,
    },
    ...overrides,
  };
}

function versions(taskId: string): ScriptVersionList {
  return {
    task_id: taskId,
    versions: [
      {
        version: 1,
        is_active: false,
        word_count: 640,
        grade: "C",
        score_total: 4.6,
        revision_round: 0,
        sentence_count: 12,
        created_at: null,
      },
      {
        version: 2,
        is_active: true,
        word_count: 700,
        grade: "B",
        score_total: 6.4,
        revision_round: 1,
        sentence_count: 14,
        created_at: null,
      },
    ],
  };
}

function diff(taskId: string, from: number, to: number): ScriptDiff {
  return {
    task_id: taskId,
    from_version: from,
    to_version: to,
    changes: [{ op: "replace", old_seq: 3, new_seq: 3, old_text: "旧", new_text: "新" }],
    summary: { unchanged: 11, changed: 1, added: 1, removed: 0, total: 13 },
  };
}

/** 装一整套假件（**每次都给全** ⇒ 上一个用例的覆盖不会漏到下一个）。 */
function install(overrides: Partial<ScriptsApi> = {}): ScriptsApi {
  const fakes: ScriptsApi = {
    fetchApprovals: vi.fn(async () => page([item("t1"), item("t2")])),
    fetchScript: vi.fn(async (taskId: string) => detail(taskId)),
    fetchScriptVersions: vi.fn(async (taskId: string) => versions(taskId)),
    fetchScriptDiff: vi.fn(async (taskId: string, from: number, to: number) => diff(taskId, from, to)),
    approveTask: vi.fn(async (taskId: string) => result(taskId, "approved")),
    rejectTask: vi.fn(async (taskId: string) => result(taskId, "rejected")),
    discardTask: vi.fn(async (taskId: string) => result(taskId, "discarded")),
    approveBatch: vi.fn(async (ids: readonly string[]) => batch(ids)),
    rescueTask: vi.fn(async (taskId: string) => rescue(taskId)),
    ...overrides,
  };
  configureScriptsApi(fakes);
  return fakes;
}

let fakes: ScriptsApi;

beforeEach(() => {
  setActivePinia(createPinia());
  fakes = install();
});

// ══════════════════════════════════════════════════════════════════════
// 纯函数（不碰响应式状态）
// ══════════════════════════════════════════════════════════════════════

describe("退回意见的必填判定", () => {
  it("只有空白等于没写（后端才是权威，前端拦的是白跑一次往返）", () => {
    expect(canReject("")).toBe(false);
    expect(canReject("   ")).toBe(false);
    expect(canReject("开场太平")).toBe(true);
  });
});

describe("批量清单", () => {
  it("按**队列顺序**摊开，而不是勾选顺序（人对得上号）", () => {
    const queue = [item("t1"), item("t2"), item("t3")];
    expect(batchPreview(queue, ["t3", "t1"]).map((it) => it.task_id)).toEqual(["t1", "t3"]);
  });

  it("忽略未勾选与已不在队列里的 id", () => {
    const queue = [item("t1")];
    expect(batchPreview(queue, ["t9"]).map((it) => it.task_id)).toEqual([]);
    expect(batchPreview(queue, []).map((it) => it.task_id)).toEqual([]);
  });
});

describe("评分色调与 diff 标签", () => {
  it("A 绿 / B 黄 / C 红 / 其余灰（后端评级是大写）", () => {
    expect(gradeTone("A")).toBe("ok");
    expect(gradeTone("B")).toBe("warn");
    expect(gradeTone("C")).toBe("error");
    expect(gradeTone(null)).toBe("idle");
    expect(gradeTone("D")).toBe("idle");
  });

  it("四类操作各有中文标签，未知一律当未动", () => {
    expect(diffOpLabel("replace")).toBe("改");
    expect(diffOpLabel("insert")).toBe("增");
    expect(diffOpLabel("delete")).toBe("删");
    expect(diffOpLabel("equal")).toBe("同");
    expect(diffOpLabel("whatever")).toBe("同");
  });

  it("批量小结在**有失败时**必须把人指到清单上", () => {
    expect(summarizeBatch(3, 0)).toBe("已放行 3 条");
    expect(summarizeBatch(2, 1)).toContain("失败 1 条");
  });

  it("describeError 把统一信封翻成人话（带状态码）", () => {
    expect(describeError(new ApiError("意见必填", 422, null))).toBe("意见必填（HTTP 422）");
    expect(describeError(new ApiError("超时", 0, null))).toBe("超时");
    expect(describeError("boom")).toBe("boom");
  });
});

describe("闸事件判定", () => {
  it("只认 approval.requested / approval.decided / task.transition", () => {
    expect(isGateEvent("approval.requested")).toBe(true);
    expect(isGateEvent("approval.decided")).toBe(true);
    expect(isGateEvent("task.transition")).toBe(true);
  });

  it("review.scored 不算（打分不改队列，重拉纯属噪音）", () => {
    expect(isGateEvent("review.scored")).toBe(false);
    expect(isGateEvent(undefined)).toBe(false);
  });
});

// ══════════════════════════════════════════════════════════════════════
// 队列
// ══════════════════════════════════════════════════════════════════════

describe("队列刷新", () => {
  it("填充队列与计数，并清掉上一轮的报错", async () => {
    const store = useScriptsStore();
    await store.refresh();
    expect(store.queue.map((it) => it.task_id)).toEqual(["t1", "t2"]);
    expect(store.pendingCount).toBe(2);
    expect(store.error).toBeNull();
  });

  it("失败时如实报错（不假装队列是空的）", async () => {
    install({ fetchApprovals: vi.fn(async () => { throw new ApiError("库锁了", 500, null); }) });
    const store = useScriptsStore();
    await store.refresh();
    expect(store.error).toBe("库锁了（HTTP 500）");
    expect(store.loading).toBe(false);
  });

  it("选中的任务已不在列表 ⇒ 连详情一起清掉（不留幽灵）", async () => {
    const store = useScriptsStore();
    await store.refresh();
    await store.select("t1");
    expect(store.detail?.task_id).toBe("t1");

    install({ fetchApprovals: vi.fn(async () => page([item("t2")])) });
    await store.refresh();
    expect(store.selectedTaskId).toBeNull();
    expect(store.detail).toBeNull();
    expect(store.versions).toBeNull();
  });

  it("勾选里的 id 已消失 ⇒ 跟着剔除（否则批量会带上不存在的任务）", async () => {
    const store = useScriptsStore();
    await store.refresh();
    store.toggleChecked("t1");
    store.toggleChecked("t2");

    install({ fetchApprovals: vi.fn(async () => page([item("t2")])) });
    await store.refresh();
    expect(store.checked).toEqual(["t2"]);
  });
});

describe("状态过滤", () => {
  it("切换后按新状态重拉，并清掉勾选与提示", async () => {
    const store = useScriptsStore();
    await store.refresh();
    store.toggleChecked("t1");
    store.requestBatch();
    expect(store.pendingBatch).not.toBeNull();

    await store.setStatus("discarded");
    expect(store.status).toBe("discarded");
    expect(store.checked).toEqual([]);
    expect(store.pendingBatch).toBeNull();
    expect(fakes.fetchApprovals).toHaveBeenLastCalledWith("discarded");
  });

  it("同一个状态不重复拉（下拉被重放不会变成一次刷新）", async () => {
    const store = useScriptsStore();
    await store.refresh();
    const calls = (fakes.fetchApprovals as ReturnType<typeof vi.fn>).mock.calls.length;
    await store.setStatus("pending");
    expect((fakes.fetchApprovals as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls);
  });
});

// ══════════════════════════════════════════════════════════════════════
// 详情
// ══════════════════════════════════════════════════════════════════════

describe("选中详情", () => {
  it("一次把正文与版本拉齐（避免拼出互相矛盾的视图）", async () => {
    const store = useScriptsStore();
    await store.select("t1");
    expect(store.selectedTaskId).toBe("t1");
    expect(store.detail?.script.body_md).toBe("正文");
    expect(store.versions?.versions.map((v) => v.version)).toEqual([1, 2]);
  });

  it("失败时清空详情并报错（宁可空着，也不显示上一条的正文）", async () => {
    const store = useScriptsStore();
    await store.select("t1");
    install({ fetchScript: vi.fn(async () => { throw new ApiError("还没有生效稿件", 404, null); }) });
    await store.select("t2");
    expect(store.detail).toBeNull();
    expect(store.versions).toBeNull();
    expect(store.error).toBe("还没有生效稿件（HTTP 404）");
  });

  it("换一条时清掉上一条的退回意见与对照", async () => {
    const store = useScriptsStore();
    await store.select("t1");
    store.rejectComment = "上一稿的意见";
    await store.loadDiff(1, 2);
    expect(store.diff).not.toBeNull();

    await store.select("t2");
    expect(store.rejectComment).toBe("");
    expect(store.diff).toBeNull();
  });
});

// ══════════════════════════════════════════════════════════════════════
// 决断
// ══════════════════════════════════════════════════════════════════════

describe("放行", () => {
  it("成功 ⇒ 提示进入配音队列并刷新", async () => {
    const store = useScriptsStore();
    expect(await store.approve("t1")).toBe(true);
    expect(store.notice).toContain("配音队列");
    expect(fakes.approveTask).toHaveBeenCalledWith("t1", undefined);
    expect(fakes.fetchApprovals).toHaveBeenCalled();
  });

  it("失败 ⇒ 报错并返回 false（不抛：调用方是按钮）", async () => {
    install({ approveTask: vi.fn(async () => { throw new ApiError("不在待审状态", 409, null); }) });
    const store = useScriptsStore();
    expect(await store.approve("t1")).toBe(false);
    expect(store.error).toBe("不在待审状态（HTTP 409）");
  });
});

describe("退回", () => {
  it("意见为空 ⇒ **不发请求**（省一次注定 422 的往返）", async () => {
    const store = useScriptsStore();
    store.rejectComment = "   ";
    expect(await store.reject("t1")).toBe(false);
    expect(fakes.rejectTask).not.toHaveBeenCalled();
    expect(store.error).toContain("退回必须写清理由");
  });

  it("成功 ⇒ 清空意见框（下一轮不该看到上一轮的意见）", async () => {
    const store = useScriptsStore();
    store.rejectComment = "开场太平";
    expect(await store.reject("t1")).toBe(true);
    expect(fakes.rejectTask).toHaveBeenCalledWith("t1", "开场太平");
    expect(store.rejectComment).toBe("");
    expect(store.notice).toBe("已退回改稿");
  });
});

describe("放弃与捞回", () => {
  it("放弃是两步：第一步只记意图，不发请求", () => {
    const store = useScriptsStore();
    store.requestDiscard("t1");
    expect(store.pendingDiscard).toBe("t1");
    expect(fakes.discardTask).not.toHaveBeenCalled();
  });

  it("取消之后没有副作用", () => {
    const store = useScriptsStore();
    store.requestDiscard("t1");
    store.cancelDiscard();
    expect(store.pendingDiscard).toBeNull();
    expect(fakes.discardTask).not.toHaveBeenCalled();
  });

  it("第二步才发请求，并提示可以捞回", async () => {
    const store = useScriptsStore();
    store.requestDiscard("t1");
    await store.confirmDiscard();
    expect(fakes.discardTask).toHaveBeenCalledWith("t1");
    expect(store.pendingDiscard).toBeNull();
    expect(store.notice).toContain("捞回");
  });

  it("捞回成功 ⇒ 提示选题候选也恢复了", async () => {
    const store = useScriptsStore();
    await store.rescue("t1");
    expect(store.notice).toContain("选题候选");
  });
});

// ══════════════════════════════════════════════════════════════════════
// 批量
// ══════════════════════════════════════════════════════════════════════

describe("批量通过", () => {
  it("没勾选 ⇒ 只提示，不发请求", () => {
    const store = useScriptsStore();
    store.requestBatch();
    expect(store.pendingBatch).toBeNull();
    expect(store.error).toContain("先勾选");
    expect(fakes.approveBatch).not.toHaveBeenCalled();
  });

  it("第一步摊开清单（**不发请求**：批量误伤要靠这一步挡）", async () => {
    const store = useScriptsStore();
    await store.refresh();
    store.toggleChecked("t2");
    store.toggleChecked("t1");
    store.requestBatch();
    expect(store.pendingBatch?.map((it) => it.task_id)).toEqual(["t1", "t2"]);
    expect(fakes.approveBatch).not.toHaveBeenCalled();
  });

  it("取消清单 ⇒ 勾选保留（可以改完再按）", async () => {
    const store = useScriptsStore();
    await store.refresh();
    store.toggleChecked("t1");
    store.requestBatch();
    store.cancelBatch();
    expect(store.pendingBatch).toBeNull();
    expect(store.checked).toEqual(["t1"]);
  });

  it("第二步才发请求，成功后清空勾选并刷新", async () => {
    const store = useScriptsStore();
    await store.refresh();
    store.toggleChecked("t1");
    store.requestBatch();
    await store.confirmBatch();
    expect(fakes.approveBatch).toHaveBeenCalledWith(["t1"]);
    expect(store.checked).toEqual([]);
    expect(store.pendingBatch).toBeNull();
    expect(store.notice).toBe("已放行 1 条");
  });

  it("部分失败 ⇒ 逐条如实报 code / message / remediation（不报成整体失败）", async () => {
    install({
      approveBatch: vi.fn(async (ids: readonly string[]) => ({
        requested: ids.length,
        approved: [result("t1", "approved")],
        failed: [
          {
            task_id: "t2",
            code: "APPROVAL_NOT_PENDING",
            message: "不在待审状态",
            remediation: "刷新列表",
          },
        ],
      })),
    });
    const store = useScriptsStore();
    await store.refresh();
    store.toggleChecked("t1");
    store.toggleChecked("t2");
    store.requestBatch();
    await store.confirmBatch();

    expect(store.notice).toContain("失败 1 条");
    expect(store.failures.map((it) => it.code)).toEqual(["APPROVAL_NOT_PENDING"]);
    expect(store.failures[0].remediation).toBe("刷新列表");
    expect(store.checked).toEqual([]);
  });

  it("整批失败（网络层）⇒ 只报错，清单留着让人重试", async () => {
    install({ approveBatch: vi.fn(async () => { throw new ApiError("网关超时", 0, null); }) });
    const store = useScriptsStore();
    await store.refresh();
    store.toggleChecked("t1");
    store.requestBatch();
    await store.confirmBatch();
    expect(store.error).toBe("网关超时");
    expect(store.pendingBatch).not.toBeNull();
  });

  it("没有待确认清单时直接返回（双保险：按钮不该发两次）", async () => {
    const store = useScriptsStore();
    await store.confirmBatch();
    expect(fakes.approveBatch).not.toHaveBeenCalled();
  });
});

// ══════════════════════════════════════════════════════════════════════
// 版本对照
// ══════════════════════════════════════════════════════════════════════

describe("版本对照", () => {
  it("两个版本号原样透传（不许悄悄换成默认值）", async () => {
    const store = useScriptsStore();
    await store.select("t1");
    await store.loadDiff(1, 2);
    expect(fakes.fetchScriptDiff).toHaveBeenCalledWith("t1", 1, 2);
    expect(store.diff?.summary.changed).toBe(1);
  });

  it("没选任务 ⇒ 不发请求（避免拉一个不属于任何任务的对照）", async () => {
    const store = useScriptsStore();
    await store.loadDiff(1, 2);
    expect(fakes.fetchScriptDiff).not.toHaveBeenCalled();
  });

  it("失败 ⇒ 清空对照并报错（宁可没有，也不要留一份对不上的）", async () => {
    install({ fetchScriptDiff: vi.fn(async () => { throw new ApiError("没有第 9 版", 404, null); }) });
    const store = useScriptsStore();
    await store.select("t1");
    await store.loadDiff(1, 9);
    expect(store.diff).toBeNull();
    expect(store.error).toBe("没有第 9 版（HTTP 404）");
  });

  it("clearDiff 只清对照，不动正文", async () => {
    const store = useScriptsStore();
    await store.select("t1");
    await store.loadDiff(1, 2);
    store.clearDiff();
    expect(store.diff).toBeNull();
    expect(store.detail).not.toBeNull();
  });
});
