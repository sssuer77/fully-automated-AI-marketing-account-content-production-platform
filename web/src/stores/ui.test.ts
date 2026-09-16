import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";

import { claimHandoff, handoffTaskId, useUiStore, type FlowHandoff } from "./ui";

// ══════════════════════════════════════════════════════════════════════
// T4.14 四屏串联：一次跳转的三个纯函数 / 状态迁移
// ══════════════════════════════════════════════════════════════════════

const TASK = "ui-20260916-120000";
const ULID = "01M2M9THZ1M83CT5EJH789ZVZA";

beforeEach(() => {
  setActivePinia(createPinia());
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
