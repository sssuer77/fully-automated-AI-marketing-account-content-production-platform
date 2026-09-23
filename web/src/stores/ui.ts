// 控制台外壳状态（T4.1 / T4.14）：当前面板 + 面板清单 + 四屏之间的一次跳转。

import { defineStore } from "pinia";
import { computed, ref } from "vue";

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

/** 面板 id ⇒ 定义。拖动只改**展示顺序**，这张表（标签 / 任务号 / ready）一个字段都不动。 */
const PANEL_BY_ID = new Map<PanelId, PanelDef>(PANELS.map((panel) => [panel.id, panel]));

/** 出厂顺序 = `PANELS` 的书写顺序。 */
export const DEFAULT_PANEL_ORDER: readonly PanelId[] = PANELS.map((panel) => panel.id);

/**
 * 侧边栏顺序在**这个浏览器**里的存档键。
 *
 * 为什么不落库：菜单顺序是**显示偏好**，不是业务数据 —— 它不属于任何一屏、不影响任何一条
 * 任务，也不该进 `audit_ops`。为它加一张表 + 一个 REST + 一次迁移，换来的是"同一台机器上
 * 换个浏览器打开，菜单是别人的样子"，而这份偏好本身没有跨端意义。
 */
export const PANEL_ORDER_STORAGE_KEY = "studio.rail.order";

/** 拖动落点：插在目标项的**前面**还是**后面**（由指针落在那一行的上半 / 下半决定）。 */
export type DropPlace = "before" | "after";

/** 存储的最小形状（`localStorage` 天然满足）—— 便于用假件测"读坏了 / 存不下"这两条分支。 */
export interface OrderStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/**
 * 把存档里那份顺序**对齐到当前的菜单清单**：认得的留下、不认得的丢掉、缺的补在最后。
 *
 * 为什么不能"存了什么就用什么"：菜单会变（`prompts` 就是 2026-09-20 才加的）。照着旧存档画，
 * 新加的那一屏**永远不出现**，而屏幕上没有任何信号说明"它被藏起来了" —— 这是本仓库最不能
 * 接受的那种失败（静默）。同理，删掉一屏之后存档里那个 id 必须自己消失，否则它会在下一次
 * 拖动时被一起搬来搬去、占着一个看不见的坑位。
 *
 * 重复项只留第一次：存档被手改过 / 两个标签页同时拖动时都可能出现，留两份等于菜单里出现
 * 两个一模一样的按钮（而且它们的高亮联动，看起来像渲染 bug）。
 */
export function normalizePanelOrder(stored: unknown, defaults: readonly PanelId[]): PanelId[] {
  const known = new Set(defaults);
  const seen = new Set<PanelId>();
  const out: PanelId[] = [];
  if (Array.isArray(stored)) {
    for (const raw of stored) {
      if (typeof raw !== "string") continue;
      const id = raw as PanelId;
      if (!known.has(id) || seen.has(id)) continue;
      seen.add(id);
      out.push(id);
    }
  }
  for (const id of defaults) {
    if (!seen.has(id)) out.push(id);
  }
  return out;
}

/**
 * 一次拖动（纯函数）：把 `dragged` 挪到 `target` 的前 / 后。
 *
 * 落点写成**相对位置**而不是"第几行"：拖动过程中行号会变（被拖走的那一项让它后面的每一行
 * 都上移一格），相对位置不会变 —— 而用户看到的那条插入线正是相对位置。
 *
 * 先把被拖项摘掉再找 `target` 的落点，所以"往下拖"与"往上拖"共用一套算法：
 * `[A,B,C,D]` 把 A 拖到 C 之后 ⇒ `[B,C,A,D]`；把 C 拖到 A 之前 ⇒ `[C,A,B,D]`。
 */
export function movePanel(
  order: readonly PanelId[],
  dragged: PanelId,
  target: PanelId,
  place: DropPlace,
): PanelId[] {
  if (dragged === target) return [...order];
  const rest = order.filter((id) => id !== dragged);
  if (rest.length === order.length) return [...order];
  const at = rest.indexOf(target);
  if (at < 0) return [...order];
  rest.splice(at + (place === "after" ? 1 : 0), 0, dragged);
  return rest;
}

/**
 * 读存档：读不到 / 读坏了 / 存储整个不可用 ⇒ 出厂顺序。
 *
 * **不抛**：顺序不对只是菜单换个样子，而抛出去会让外壳画不出来 —— 拿一个显示偏好去换
 * 一整屏，比例完全不对。
 */
export function readPanelOrder(
  storage: OrderStorage | null | undefined,
  defaults: readonly PanelId[],
): PanelId[] {
  try {
    const raw = storage?.getItem(PANEL_ORDER_STORAGE_KEY) ?? null;
    if (raw === null) return [...defaults];
    return normalizePanelOrder(JSON.parse(raw), defaults);
  } catch {
    return [...defaults];
  }
}

/** 写存档：存不下（配额满 / 隐私模式）就只让这一次排序在内存里生效，不打扰用户。 */
export function writePanelOrder(
  storage: OrderStorage | null | undefined,
  order: readonly PanelId[],
): void {
  try {
    storage?.setItem(PANEL_ORDER_STORAGE_KEY, JSON.stringify(order));
  } catch {
    // 显示偏好存不下不是错误，只是下次打开回到出厂顺序。
  }
}

/**
 * 本浏览器的存储。
 *
 * **连读一下都会抛**的情况是存在的（禁用 Cookie / 隐私模式），所以取存储本身也要包起来 ——
 * 那两种情况下的答案是"顺序只在内存里生效"，而不是"外壳打不开"。
 */
function orderStorage(): OrderStorage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

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
  /** 侧边栏顺序（可拖动）。出厂那份是 `PANELS` 的书写顺序，存档只在这个浏览器里。 */
  const panelOrder = ref<PanelId[]>(readPanelOrder(orderStorage(), DEFAULT_PANEL_ORDER));

  /**
   * 侧边栏真正画的那一份（顺序 = `panelOrder`，内容 = `PANELS`）。
   *
   * 顺序与清单**分开存**：`PANELS` 是规格书那份清单（标签 / 任务号 / 是否已交付），
   * 它不该因为"我把日志拖到上面去了"而变。画的时候按顺序取，两边各自只有一处真相。
   */
  const orderedPanels = computed<PanelDef[]>(() =>
    panelOrder.value.flatMap((id) => {
      const panel = PANEL_BY_ID.get(id);
      return panel === undefined ? [] : [panel];
    }),
  );

  /** 顺序还是出厂那份吗？是就不显示"恢复默认"——没有可恢复的东西。 */
  const isDefaultOrder = computed(
    () =>
      panelOrder.value.length === DEFAULT_PANEL_ORDER.length &&
      panelOrder.value.every((id, index) => id === DEFAULT_PANEL_ORDER[index]),
  );

  /**
   * 拖完一次：把 `dragged` 挪到 `target` 的前 / 后，并落盘。
   *
   * 拖动**不切面板**：拖是"排位置"，不是"点进去"—— 拖到一半松手不该把人带走。
   * 所以这里不碰 `activePanel`（也不碰 `pendingHandoff`：那一条是"面板把人送过去"的，
   * 与"人自己重新排了一遍菜单"无关）。
   */
  function movePanelTo(dragged: PanelId, target: PanelId, place: DropPlace): void {
    panelOrder.value = movePanel(panelOrder.value, dragged, target, place);
    writePanelOrder(orderStorage(), panelOrder.value);
  }

  /** 恢复出厂顺序（只有顺序被改过时，外壳才露出那个按钮）。 */
  function resetPanelOrder(): void {
    panelOrder.value = [...DEFAULT_PANEL_ORDER];
    writePanelOrder(orderStorage(), panelOrder.value);
  }

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

  return {
    activePanel,
    panelOrder,
    orderedPanels,
    isDefaultOrder,
    movePanelTo,
    resetPanelOrder,
    selectPanel,
    pendingHandoff,
    goTo,
    takeHandoff,
  };
});
