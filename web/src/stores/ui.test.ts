import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  claimHandoff,
  DEFAULT_PANEL_ORDER,
  handoffTaskId,
  movePanel,
  normalizePanelOrder,
  PANEL_ORDER_STORAGE_KEY,
  readPanelOrder,
  useUiStore,
  writePanelOrder,
  type FlowHandoff,
  type PanelId,
} from "./ui";

// ══════════════════════════════════════════════════════════════════════
// T4.14 四屏串联：一次跳转的三个纯函数 / 状态迁移
// ══════════════════════════════════════════════════════════════════════

const TASK = "ui-20260916-120000";
const ULID = "01M2M9THZ1M83CT5EJH789ZVZA";

beforeEach(() => {
  setActivePinia(createPinia());
});

// ══════════════════════════════════════════════════════════════════════
// 侧边栏拖动排序：顺序是**显示偏好**，存这个浏览器（`localStorage`），不落库
// ══════════════════════════════════════════════════════════════════════

/** 一个最小可用的 `localStorage` 假件（node 环境里没有真的那个，正好也不会串味）。 */
class FakeStorage {
  readonly map = new Map<string, string>();

  getItem(key: string): string | null {
    return this.map.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.map.set(key, value);
  }
}

/** 读 / 写都抛的存储：浏览器把 `localStorage` 整个禁用时就是它。 */
const hostileStorage = {
  getItem(): string | null {
    throw new Error("blocked");
  },
  setItem(): void {
    throw new Error("blocked");
  },
};

function stubLocalStorage(storage: unknown): void {
  Object.defineProperty(globalThis, "localStorage", { value: storage, configurable: true });
}

afterEach(() => {
  Reflect.deleteProperty(globalThis, "localStorage");
});

describe("normalizePanelOrder", () => {
  const defaults: readonly PanelId[] = ["overview", "topics", "scripts"];

  it("存档里的顺序照原样保留（它就不是出厂顺序）", () => {
    expect(normalizePanelOrder(["scripts", "overview", "topics"], defaults)).toEqual([
      "scripts",
      "overview",
      "topics",
    ]);
  });

  it("存档里没有的那一屏补在最后 —— 新加的菜单不会因为存档旧了就消失", () => {
    expect(normalizePanelOrder(["scripts", "overview"], defaults)).toEqual([
      "scripts",
      "overview",
      "topics",
    ]);
  });

  it("认不得的 id 丢掉 —— 菜单删了，存档里那一格也得跟着走", () => {
    expect(normalizePanelOrder(["topics", "gone_panel", "overview"], defaults)).toEqual([
      "topics",
      "overview",
      "scripts",
    ]);
  });

  it("重复项只留第一次 —— 不然菜单里会出现两个一模一样的按钮", () => {
    expect(normalizePanelOrder(["topics", "topics", "overview"], defaults)).toEqual([
      "topics",
      "overview",
      "scripts",
    ]);
  });

  it("不是数组 / 空 / null ⇒ 出厂顺序（读坏的东西不该让菜单少几屏）", () => {
    expect(normalizePanelOrder(null, defaults)).toEqual([...defaults]);
    expect(normalizePanelOrder("overview", defaults)).toEqual([...defaults]);
    expect(normalizePanelOrder([1, 2, 3], defaults)).toEqual([...defaults]);
  });
});

describe("movePanel", () => {
  const order: readonly PanelId[] = ["overview", "topics", "scripts", "voices"];

  it("往后拖：A 拖到 C 之后 ⇒ [B, C, A, D]", () => {
    expect(movePanel(order, "overview", "scripts", "after")).toEqual([
      "topics",
      "scripts",
      "overview",
      "voices",
    ]);
  });

  it("往前拖：C 拖到 A 之前 ⇒ [C, A, B, D]", () => {
    expect(movePanel(order, "scripts", "overview", "before")).toEqual([
      "scripts",
      "overview",
      "topics",
      "voices",
    ]);
  });

  it("落点是相对位置：拖到相邻项的后面与前面，结果不同", () => {
    expect(movePanel(order, "overview", "topics", "after")).toEqual([
      "topics",
      "overview",
      "scripts",
      "voices",
    ]);
    expect(movePanel(order, "overview", "topics", "before")).toEqual([...order]);
  });

  it("拖到自己身上 / 有一头不在清单里 ⇒ 原样，而且是**新数组**（调用方不必判空）", () => {
    const ghost = "gone_panel" as PanelId;
    expect(movePanel(order, "topics", "topics", "after")).toEqual([...order]);
    expect(movePanel(order, ghost, "topics", "after")).toEqual([...order]);
    expect(movePanel(order, "topics", ghost, "before")).toEqual([...order]);
    expect(movePanel(order, "topics", "topics", "before")).not.toBe(order);
  });

  it("不改传进来的那一份（纯函数：拖到一半失败也不该把菜单改了）", () => {
    const before = [...order];
    movePanel(order, "voices", "overview", "before");
    expect(order).toEqual(before);
  });
});

describe("readPanelOrder / writePanelOrder", () => {
  it("写进去再读出来 ⇒ 同一份顺序", () => {
    const storage = new FakeStorage();
    const order = normalizePanelOrder(["voices", "overview"], DEFAULT_PANEL_ORDER);
    writePanelOrder(storage, order);
    expect(readPanelOrder(storage, DEFAULT_PANEL_ORDER)).toEqual(order);
  });

  it("存档是坏 JSON ⇒ 出厂顺序，不抛", () => {
    const storage = new FakeStorage();
    storage.setItem(PANEL_ORDER_STORAGE_KEY, "{ 这不是 JSON");
    expect(readPanelOrder(storage, DEFAULT_PANEL_ORDER)).toEqual([...DEFAULT_PANEL_ORDER]);
  });

  it("没有存档 / 存储整个不可用（读就抛）⇒ 出厂顺序", () => {
    expect(readPanelOrder(new FakeStorage(), DEFAULT_PANEL_ORDER)).toEqual([...DEFAULT_PANEL_ORDER]);
    expect(readPanelOrder(null, DEFAULT_PANEL_ORDER)).toEqual([...DEFAULT_PANEL_ORDER]);
    expect(readPanelOrder(hostileStorage, DEFAULT_PANEL_ORDER)).toEqual([...DEFAULT_PANEL_ORDER]);
  });

  it("存不下（写就抛）⇒ 不抛 —— 顺序只在内存里生效，下次打开回出厂", () => {
    expect(() => writePanelOrder(hostileStorage, DEFAULT_PANEL_ORDER)).not.toThrow();
  });
});

describe("useUiStore 的侧边栏顺序", () => {
  it("拖动一次 ⇒ 顺序变了、也落盘了（下次打开还是这个样）", () => {
    const storage = new FakeStorage();
    stubLocalStorage(storage);
    const ui = useUiStore();
    ui.movePanelTo("logs", "overview", "before");
    expect(ui.panelOrder.slice(0, 2)).toEqual(["logs", "overview"]);
    expect(JSON.parse(storage.getItem(PANEL_ORDER_STORAGE_KEY) ?? "null")).toEqual(ui.panelOrder);
  });

  it("拖动**不切面板**：排位置不是点进去（也不吞掉待认领的跳转）", () => {
    stubLocalStorage(new FakeStorage());
    const ui = useUiStore();
    ui.goTo("voices", TASK);
    ui.movePanelTo("logs", "overview", "before");
    expect(ui.activePanel).toBe("voices");
    expect(ui.pendingHandoff).toEqual({ panel: "voices", taskId: TASK });
  });

  it("重建 store（刷新页面）⇒ 读到刚才那份顺序，缺的补在后面一个都不少", () => {
    const storage = new FakeStorage();
    storage.setItem(PANEL_ORDER_STORAGE_KEY, JSON.stringify(["settings", "overview"]));
    stubLocalStorage(storage);
    const ui = useUiStore();
    expect(ui.panelOrder.slice(0, 2)).toEqual(["settings", "overview"]);
    expect(ui.panelOrder).toHaveLength(DEFAULT_PANEL_ORDER.length);
    expect(new Set(ui.panelOrder)).toEqual(new Set(DEFAULT_PANEL_ORDER));
  });

  it("orderedPanels 与顺序一一对应，内容仍是 PANELS 那份（标签 / 任务号不变）", () => {
    stubLocalStorage(new FakeStorage());
    const ui = useUiStore();
    ui.movePanelTo("settings", "overview", "before");
    expect(ui.orderedPanels.map((panel) => panel.id)).toEqual(ui.panelOrder);
    expect(ui.orderedPanels[0]).toEqual({ id: "settings", label: "设置", task: "T6.1", ready: true });
  });

  it("恢复默认 ⇒ 回出厂顺序并落盘；没改过时 `isDefaultOrder` 为真（那个按钮不出现）", () => {
    const storage = new FakeStorage();
    stubLocalStorage(storage);
    const ui = useUiStore();
    expect(ui.isDefaultOrder).toBe(true);
    ui.movePanelTo("settings", "overview", "before");
    expect(ui.isDefaultOrder).toBe(false);
    ui.resetPanelOrder();
    expect(ui.panelOrder).toEqual([...DEFAULT_PANEL_ORDER]);
    expect(ui.isDefaultOrder).toBe(true);
    expect(JSON.parse(storage.getItem(PANEL_ORDER_STORAGE_KEY) ?? "null")).toEqual([
      ...DEFAULT_PANEL_ORDER,
    ]);
  });
});

describe("handoffTaskId", () => {
  it("去掉首尾空白后原样给出（连字符与 ULID 都不动）", () => {
    expect(handoffTaskId(`  ${TASK}  `)).toBe(TASK);
    expect(handoffTaskId(ULID)).toBe(ULID);
  });

  it("空串 / 全空白 ⇒ null（那是没填，不是一个任务号）", () => {
    expect(handoffTaskId("")).toBeNull();
    expect(handoffTaskId("   ")).toBeNull();
  });

  it("只裁首尾：中间的字符一个都不动（形状校验是面板自己的事）", () => {
    expect(handoffTaskId("a b")).toBe("a b");
  });
});

describe("claimHandoff", () => {
  it("是给这一屏的 ⇒ 取走任务号，待认领的那一条清空", () => {
    const pending: FlowHandoff = { panel: "voices", taskId: TASK };
    expect(claimHandoff(pending, "voices")).toEqual({ taskId: TASK, rest: null });
  });

  it("不是给这一屏的 ⇒ 什么都不取，待认领的那一条原样留着", () => {
    const pending: FlowHandoff = { panel: "voices", taskId: TASK };
    expect(claimHandoff(pending, "renders")).toEqual({ taskId: null, rest: pending });
  });

  it("根本没有待认领的跳转 ⇒ 什么都不发生", () => {
    expect(claimHandoff(null, "scripts")).toEqual({ taskId: null, rest: null });
  });
});

describe("useUiStore 的跳转", () => {
  it("goTo：记下任务号并把面板切过去", () => {
    const ui = useUiStore();
    expect(ui.goTo("scripts", TASK)).toBe(true);
    expect(ui.activePanel).toBe("scripts");
    expect(ui.pendingHandoff).toEqual({ panel: "scripts", taskId: TASK });
  });

  it("goTo：任务号空着就不跳（面板不动、也不留下一条空的待认领）", () => {
    const ui = useUiStore();
    expect(ui.goTo("voices", "   ")).toBe(false);
    expect(ui.activePanel).toBe("overview");
    expect(ui.pendingHandoff).toBeNull();
  });

  it("goTo：首尾空白在跳之前就裁掉（不把空白带进去）", () => {
    const ui = useUiStore();
    ui.goTo("renders", `  ${TASK} `);
    expect(ui.pendingHandoff).toEqual({ panel: "renders", taskId: TASK });
  });

  it("takeHandoff：认领一次就没了 —— 切回来不会被同一个任务号再跳一次", () => {
    const ui = useUiStore();
    ui.goTo("voices", TASK);
    expect(ui.takeHandoff("voices")).toBe(TASK);
    expect(ui.takeHandoff("voices")).toBeNull();
    expect(ui.pendingHandoff).toBeNull();
  });

  it("takeHandoff：认领别的屏不会把属于自己的那一条吞掉", () => {
    const ui = useUiStore();
    ui.goTo("voices", TASK);
    expect(ui.takeHandoff("renders")).toBeNull();
    expect(ui.pendingHandoff).toEqual({ panel: "voices", taskId: TASK });
    expect(ui.takeHandoff("voices")).toBe(TASK);
  });

  it("人自己点侧边栏走 ⇒ 待认领的跳转清掉（不替人跳）", () => {
    const ui = useUiStore();
    ui.goTo("voices", TASK);
    ui.selectPanel("logs");
    expect(ui.activePanel).toBe("logs");
    expect(ui.pendingHandoff).toBeNull();
  });

  it("顺着流程走一圈：选题 ⇒ 稿件 ⇒ 配音 ⇒ 渲染，每一步都只带一个任务号", () => {
    const ui = useUiStore();
    for (const panel of ["scripts", "voices", "renders"] as const) {
      expect(ui.goTo(panel, TASK)).toBe(true);
      expect(ui.activePanel).toBe(panel);
      expect(ui.takeHandoff(panel)).toBe(TASK);
      expect(ui.pendingHandoff).toBeNull();
    }
  });
});
