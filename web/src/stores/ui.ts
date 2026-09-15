// 控制台外壳状态（T4.1）：当前面板 + 面板清单。

import { defineStore } from "pinia";
import { ref } from "vue";

export type PanelId =
  | "overview"
  | "topics"
  | "scripts"
  | "voices"
  | "renders"
  | "templates"
  | "assets"
  | "pools"
  | "metrics"
  | "audit"
  | "personas"
  | "publish"
  | "logs";

export interface PanelDef {
  id: PanelId;
  label: string;
  /** 该面板的施工任务号（未落地的面板点进去显示"待 T4.x"）。 */
  task: string;
  /** T4.1 是否已交付。false ⇒ 只画占位，避免"看起来能用其实没接线"。 */
  ready: boolean;
}

/** 导航清单 = 规格书 §02.2 的 8+1 面板 + 四池 / 观测 / 审计 / 人物库 / 发布。 */
export const PANELS: readonly PanelDef[] = [
  { id: "overview", label: "总览台", task: "T4.1/T4.2", ready: true },
  { id: "topics", label: "选题", task: "T4.3", ready: true },
  { id: "scripts", label: "稿件", task: "T4.4", ready: true },
  { id: "voices", label: "配音", task: "T4.5", ready: false },
  { id: "renders", label: "渲染", task: "T4.6", ready: false },
  { id: "templates", label: "合成配置", task: "T4.7", ready: true },
  { id: "assets", label: "素材库", task: "T4.8", ready: true },
  { id: "logs", label: "实时日志", task: "T4.1/T4.9", ready: true },
  { id: "pools", label: "四池调度", task: "T4.10", ready: true },
  { id: "metrics", label: "观测面板", task: "T4.12", ready: true },
  { id: "audit", label: "审计留痕", task: "T4.12", ready: true },
  { id: "personas", label: "人物库", task: "T4.13", ready: true },
  { id: "publish", label: "发布", task: "T5", ready: false },
];

export const useUiStore = defineStore("ui", () => {
  const activePanel = ref<PanelId>("overview");

  function selectPanel(id: PanelId): void {
    activePanel.value = id;
  }

  return { activePanel, selectPanel };
});
