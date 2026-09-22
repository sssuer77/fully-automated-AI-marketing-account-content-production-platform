// 控制台外壳状态（T4.1 / T4.14）：当前面板 + 面板清单 + 四屏之间的一次跳转。

import { defineStore } from "pinia";
import { ref } from "vue";

import type { AssetKind } from "@/api/endpoints/assets";

/**
 * 素材库的**三个菜单**（跑酷 / 音色 / BGM）。
 *
 * 为什么不是一屏看三类：三类素材可管理的字段**本来就不一样**（跑酷有可用区间与
 * `has_text`，BGM 有 `bpm` / `mood` / `loopable`，音色有 `ref_count`），摊在一屏里
 * 只能是一张"大半格子是空的"大表，而每一列的表头都得加一句"这一类才有"。
 * 分成三个菜单之后，每一屏的表头、筛选、编辑器都只画这一类真正有的东西。
 */
export type AssetPanelId = "assets_broll" | "assets_voice" | "assets_bgm";

export type PanelId =
  | "overview"
  | "topics"
  | "scripts"
  | "voices"
  | "renders"
  | "pipeline"
  | "templates"
  | AssetPanelId
  | "pools"
  | "metrics"
  | "audit"
  | "personas"
  | "library"
  | "publish"
  | "settings"
  | "prompts"
  | "logs";

/** 菜单 → 素材类别（`Assets.vue` 就是按它知道自己该画哪一类）。 */
export const ASSET_PANEL_KINDS: Readonly<Record<AssetPanelId, AssetKind>> = {
  assets_broll: "broll",
  assets_voice: "voice",
  assets_bgm: "bgm",
};

/** 这个面板是素材库的某一个菜单吗？是就给出它管的那一类。 */
export function assetKindOf(panel: PanelId): AssetKind | null {
  return panel in ASSET_PANEL_KINDS ? ASSET_PANEL_KINDS[panel as AssetPanelId] : null;
}

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
  { id: "voices", label: "配音", task: "T4.5", ready: true },
  { id: "renders", label: "渲染", task: "T4.6", ready: true },
  { id: "pipeline", label: "一键出片", task: "T4.14+", ready: true },
  { id: "templates", label: "合成配置", task: "T4.7", ready: true },
  // 素材库按类别分三个菜单（跑酷 / 音色 / BGM）：一屏看一类，各自翻页、各自编辑。
  { id: "assets_broll", label: "跑酷素材", task: "T4.8", ready: true },
  { id: "assets_voice", label: "音色库", task: "T4.8", ready: true },
  { id: "assets_bgm", label: "BGM 库", task: "T4.8", ready: true },
  { id: "logs", label: "实时日志", task: "T4.1/T4.9", ready: true },
  { id: "pools", label: "四池调度", task: "T4.10", ready: true },
  { id: "metrics", label: "观测面板", task: "T4.12", ready: true },
  { id: "audit", label: "审计留痕", task: "T4.12", ready: true },
  { id: "personas", label: "人物库", task: "T4.13", ready: true },
  // 成片库紧挨着发布：它是"选片 → 投递"那一步，而发布面板是"投出去之后怎么样"。
  // 两屏挨着，人从渲染面板出来顺着往下点就是一条路。
  { id: "library", label: "成片库", task: "T5.11", ready: true },
  { id: "publish", label: "发布", task: "T5.5", ready: true },
  { id: "settings", label: "设置", task: "T6.1", ready: true },
  { id: "prompts", label: "提示词", task: "T6.2", ready: true },
];

/**
 * 端到端流程里能互相跳过去的几屏（T4.14：选题 → 稿件 → 配音 → 渲染）。
 *
 * `pipeline` 是后加的第五个：它不在那条链上，而是**把整条链一次跑完**的那一屏。
 * 加进来是为了让"我在稿件面板刚出了一版稿，直接拿它出成片"不用手抄任务号 ——
 * 与四屏接力解决的是同一件事。
 */
export type FlowPanelId = "topics" | "scripts" | "voices" | "renders" | "pipeline";

/** 一次待认领的跳转：去哪一屏 + 带上哪个任务号。 */
export interface FlowHandoff {
  panel: FlowPanelId;
  taskId: string;
}

/**
 * 跳转前的规范化：空 / 只有空白 ⇒ `null`。
 *
 * 任务号是**调用方起的名**（后端只限长度），而"没填"与"填了个空串"在 URL 和后端的
 * path pattern 眼里是两件事 —— 后者换来一个 422。所以按钮该是禁用态，
 * 而不是把一个空任务号跳过去。
 */
export function handoffTaskId(raw: string): string | null {
  const trimmed = raw.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * 认领规则（纯函数）：这一跳是给我的就取走，不是就原样留着。
 *
 * **取走即清空**：留着的话，用户从配音点回选题、再点回配音，会被同一个任务号再跳一次
 * —— 那时他多半是想看别的。
 */
export function claimHandoff(
  pending: FlowHandoff | null,
  panel: FlowPanelId,
): { taskId: string | null; rest: FlowHandoff | null } {
  if (pending === null || pending.panel !== panel) return { taskId: null, rest: pending };
  return { taskId: pending.taskId, rest: null };
}

export const useUiStore = defineStore("ui", () => {
  const activePanel = ref<PanelId>("overview");
  const pendingHandoff = ref<FlowHandoff | null>(null);

  /**
   * 侧边栏点过去。
   *
   * 顺手清掉待认领的跳转："人自己点走的"与"面板把人送过去的"是两件事
   * —— 否则下一次点进去时，会被一个早就过期的任务号再跳一次。
   */
  function selectPanel(id: PanelId): void {
    pendingHandoff.value = null;
    activePanel.value = id;
  }

  /**
   * 跳到另一屏并带上任务号（T4.14）。任务号空着 ⇒ **不跳**（返回 `false`）。
   *
   * 这里只做两件事：记下"给谁的、带什么"，再把面板切过去。真正的"用上这个任务号"
   * 由目标面板**挂载时**认领 —— 面板是 `v-if` 挂的，切过去必然重新挂载，所以外壳
   * 不需要知道对方怎么用（稿件要拉详情、配音要拉逐句、渲染只是填个框）。
   */
  function goTo(panel: FlowPanelId, taskId: string): boolean {
    const wanted = handoffTaskId(taskId);
    if (wanted === null) return false;
    pendingHandoff.value = { panel, taskId: wanted };
    activePanel.value = panel;
    return true;
  }

  /** 目标面板挂载时认领属于自己的一跳；没有就返回 `null`。 */
  function takeHandoff(panel: FlowPanelId): string | null {
    const { taskId, rest } = claimHandoff(pendingHandoff.value, panel);
    pendingHandoff.value = rest;
    return taskId;
  }

  return { activePanel, selectPanel, pendingHandoff, goTo, takeHandoff };
});
