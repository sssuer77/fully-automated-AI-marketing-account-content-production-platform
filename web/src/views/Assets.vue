<script setup lang="ts">
// 素材库面板（T4.8 · §3.3.14 / §4.3.1 / §04.5.12）—— **一个菜单只看一类素材**。
//
// 三个菜单（跑酷素材 / 音色库 / BGM 库）是**同一个组件**带不同的 `kind`：三类的字段
// 本来就不一样（跑酷有可用区间与 `has_text`，BGM 有 `bpm` / `mood` / `loopable`，
// 音色有 `ref_count`），摊在一屏里只能是一张"大半格子是空的"大表。分菜单之后，
// 每一屏的表头、筛选、编辑器都只画这一类真正有的东西。
//
// 这一屏要回答六个问题，缺一个都会让人不再信任它：
// ① "**出片到底会不会用到它**" —— 标题右边一行三组数字 + 每行一个状态（`usable` 由后端算）；
// ② "盘上还有没入库的" —— 未入库那批显式列出 + 一个入库入口（它们**照样会被出片挑到**）；
// ③ "够不够用、缺多少" —— 缺口那句话（后端算的判据线，前端不抄第二份）；
// ④ "这条启用还是停用、要不要删掉" —— 开关 + 删除（删除要点两下：误删不可逆）；
// ⑤ "素材长什么样、听着对不对" —— 缩略图 + 试听（URL 直接交给 img / audio）；
// ⑥ "那几十个字段在哪改" —— 逐行编辑器（按 `EDIT_FIELDS[kind]` 画，不手写三份模板）；
// ⑦ "这个音色到底由哪几段拼出来的、哪一段不对" —— 音色的逐段表（裁定 381）。
//
// 为什么音色要额外有第 ⑦ 条
// ----------------------------
// 音色是三类里**唯一一个"一条素材 = 一组文件"**的：它的可用性不取决于库里那一行，
// 而取决于目录里躺着哪几段、每段多长、`ref.txt` 有没有与它们一一对应。只看"3 段"
// 这个数，用户既看不出哪一段是当年复制凑数的，也没法把不要的那一段去掉 —— 只能整条
// 删掉重传，而重传要把所有段再选一遍。逐段表把这三件事摊开，并且让"删一段"成为
// 一个可以说清、可以撤销思路（重传）的动作。
//
// 三态必须分开画（这是"清晰操作"的全部意思）
// ------------------------------------------
// ① **出片会挑到**（绿）—— 盘上有文件、库里启用着；
// ② **已停用**（黄）—— 人自己点的，随时能点回来，出片不再挑它；
// ③ **文件不在了**（红）—— 行还在、开关还亮着，而文件被人挪走了。
// 第 ③ 种是最阴的：面板上只画开关的话，用户会看着一个绿点，纳闷片子为什么一直用别的底片。
//
// 翻页 / 筛选都是**服务端**在做
// ----------------------------
// 一柜子素材迟早到几百条，一次全拖过来既慢又没人看得完。页码以服务端回的为准
// （`page` 是钳过的：最后一页被筛空了，它给的是最后一页，不是一页空白）——
// 所以这里翻页之后什么都不自己算，直接照 `store.pageIndex` 画。
//
// 为什么编辑器只有一份草稿
// ------------------------
// 同时只有一行在编辑（`store.editingId`）。草稿留在**本组件**里而不是 store 里：
// 它是一次编辑的中间态，切菜单就该没了 —— 放进 store 只会多一份要记得清的状态。

import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import type { AssetItem, AssetKind, ScannedAsset } from "@/api/endpoints/assets";
import type { StatusTone } from "@/components/tone";
import {
  ACTION_LABELS,
  EDIT_FIELDS,
  ENABLED_FILTERS,
  KIND_HINTS,
  KIND_LABELS,
  LICENSE_LABELS,
  PAGE_SIZE_OPTIONS,
  UPLOAD_LABELS,
  acceptFor,
  coverageText,
  fieldDraft,
  formatDuration,
  isPlaceholder,
  itemStateText,
  itemTone,
  mediaUrlOf,
  pageRange,
  pendingNote,
  pendingText,
  scannedTone,
  shortfallText,
  thumbUrlOf,
  totalText,
  uploadOf,
  uploadTone,
  usableRangeText,
  useAssetsStore,
  type EnabledFilter,
  type FieldDraft,
  type FieldSpec,
} from "@/stores/assets";
import { plainText } from "@/stores/publish";

const props = defineProps<{ kind: AssetKind }>();

const assets = useAssetsStore();

/** 搜索框里的字：**立刻跟着手指走**，而 store 里那份是防抖之后真发出去的那一次。 */
const queryInput = ref("");

/** 编辑器的草稿（同时只开一行）。 */
const draft = ref<FieldDraft>({});

/** 正在等二次确认删除的那一行（`null` = 没有）。删除不可逆，所以要点两下。 */
const confirmId = ref<string | null>(null);

/**
 * 正在等二次确认的那一段参考音（`null` = 没有）。
 *
 * 删一段同样不可逆（音频不在盘上了），而且它**会牵动别的段**：服务端删完会重编号，
 * 后面的 `ref_03.wav` 会变成 `ref_02.wav`。所以这里也要点两下，并且把"会连带动什么"
 * 写在按钮旁边。
 */
const confirmSegment = ref<string | null>(null);

/**
 * 正在等二次确认的**孤儿清理**（`false` = 没有）。
 *
 * 与上面两个确认分开一个 ref：它们确认的对象完全不同（一行素材 / 一段参考音 /
 * 一整批盘上的东西），共用一个 ref 会让"点了一行的删除、另一处的确认条跟着亮"。
 */
const confirmPrune = ref(false);

/**
 * 这一次上传**落盘了、但没入库**的那些。
 *
 * 为什么要在回执正下方单独画一条红带：上面那张表说的是"字节写没写进盘"，
 * **不是**"这条素材能不能用"。两者混在一起时，面板会对一条永远入不了库的音色说
 * "已落盘 ref_02.wav"（绿点），而真正的原因（"第 1 段只有 2.98 秒"）埋在整页最
 * 下面的扫盘报告里 —— 用户会以为传成功了，直到配音那天才发现它根本挑不到。
 *
 * 只认**这一次回执里点到过的 id**：报告是按类别扫的，不筛一下会把别人早先传坏的
 * 条目也算到这一次头上。
 */
const uploadBlocked = computed<readonly ScannedAsset[]>(() => {
  const receipt = assets.upload;
  if (receipt === null || receipt.report === null) return [];
  const touched = new Set(
    receipt.files.map((row) => row.asset_id).filter((id): id is string => id !== null),
  );
  if (touched.size === 0) return [];
  return receipt.report.sections
    .flatMap((section) => section.assets)
    .filter((row) => touched.has(row.id) && !row.check.ok);
});

onMounted(() => {
  // 顺序要紧：先认领这一类的菜单，再接线 —— `start()` 里的第一次拉取用的是 `kind`。
  assets.open(props.kind);
  assets.start();
});

onBeforeUnmount(() => {
  assets.stop();
});

// ── 派生 ────────────────────────────────────────────────────────────────

const title = computed(() => KIND_LABELS[props.kind]);
const hint = computed(() => KIND_HINTS[props.kind]);
const fields = computed<readonly FieldSpec[]>(() => EDIT_FIELDS[props.kind]);
const isBroll = computed(() => props.kind === "broll");

/** 标题右边那颗灯：目录不在 / 出片挑不到 / 还差一些 ⇒ 黄；否则绿。 */
const tone = computed<StatusTone>(() => {
  const page = assets.page;
  if (page === null) return "warn";
  if (page.root_missing) return "warn";
  if (page.usable === 0) return "warn";
  if (page.shortfall !== null) return "warn";
  return "ok";
});

/** 这一趟扫盘里属于**这一类**的条目（别的类别的结果不该出现在这个菜单里）。 */
const scannedRows = computed<ScannedAsset[]>(
  () => assets.report?.sections.find((section) => section.kind === props.kind)?.assets ?? [],
);

/** 有筛选吗（空列表的提示要按这个分叉："没有匹配的"不是"还没有素材"）。 */
const filtered = computed(() => assets.query.trim() !== "" || assets.enabledFilter !== "all");

/** 翻页条上的一个按钮（`page === null` 是中间的省略号）。 */
interface PageButton {
  key: string;
  label: string;
  page: number | null;
}

/**
 * 页码按钮。
 *
 * 省略号是**算出来的**而不是画死的：`pageRange` 只给"该画哪几个数字"，中间断没断
 * 由这里比一下相邻两数 —— 页数少的时候 `pageRange` 会把全部页给出来，于是这里一个
 * 省略号都不会冒出来（3 页的列表上画两个省略号是最没道理的一种）。
 */
const pageButtons = computed<PageButton[]>(() => {
  const range = pageRange(assets.pageIndex, assets.pages);
  const out: PageButton[] = [];
  range.forEach((value, index) => {
    if (index > 0 && value - range[index - 1] > 1) {
      out.push({ key: `gap-${value}`, label: "…", page: null });
    }
    out.push({ key: `page-${value}`, label: String(value), page: value });
  });
  return out;
});

// ── 顶部开关 ────────────────────────────────────────────────────────────

function onLicenseChange(event: Event): void {
  assets.license = (event.target as HTMLSelectElement).value;
}

function onDryRunChange(event: Event): void {
  assets.dryRun = (event.target as HTMLInputElement).checked;
}

function onOverwriteChange(event: Event): void {
  assets.overwrite = (event.target as HTMLInputElement).checked;
}

// ── 翻页 / 筛选 ─────────────────────────────────────────────────────────

function onQuery(event: Event): void {
  const value = (event.target as HTMLInputElement).value;
  queryInput.value = value;
  assets.setQuery(value);
}

function onEnabledFilter(event: Event): void {
  assets.setEnabledFilter((event.target as HTMLSelectElement).value as EnabledFilter);
}

function onPageSize(event: Event): void {
  assets.setPageSize(Number((event.target as HTMLSelectElement).value));
}

function onPage(entry: PageButton): void {
  if (entry.page !== null) assets.goPage(entry.page);
}

// ── 入库 ────────────────────────────────────────────────────────────────

async function onScan(): Promise<void> {
  await assets.scan(props.kind);
}

/**
 * 清掉这一类的孤儿（**点两下，第一下是预览**）。
 *
 * 与删除素材同一条规矩（不可逆的动作要点两下），但这里多给了一步预览 —— 因为这一下
 * 可能一次动好几个目录，而"到底会清掉哪几个"在点之前没人知道。预览走 `dry_run`，
 * 后端一个字节都不动。
 */
async function onPrune(): Promise<void> {
  if (!confirmPrune.value) {
    const preview = await assets.prune(true);
    // 一个都不清就别进确认态：否则按钮会变成"确认清掉 0 个"，点下去什么也没发生。
    if (preview !== null && preview.removed.length > 0) confirmPrune.value = true;
    return;
  }
  if (await assets.prune(false)) confirmPrune.value = false;
}

function onCancelPrune(): void {
  confirmPrune.value = false;
}

/** 清孤儿那颗按钮上的字（确认态说清"会清掉几个"）。 */
function pruneLabel(): string {
  if (assets.pruneBusy) return "清理中…";
  if (confirmPrune.value) return `确认清掉 ${assets.pruneReport?.removed.length ?? 0} 个`;
  return "清掉不合格的孤儿";
}

/**
 * 清孤儿的回执（**三段分开说**，与后端 `PruneReport` 的三个字段一一对应）。
 *
 * 为什么不能合成一句"清掉 3 个"：三种"盘上有、库里没有"该做的动作完全不同 ——
 * 清掉的（不合格）、**该入库的**（本身合格）、以及名字不合规、压根没被认出来的。
 * 尤其第二种：用户看到"清了一遍"却还剩着，会以为功能坏了，而真相是**那几条该入库**。
 */
function pruneNote(): string {
  const report = assets.pruneReport;
  if (report === null) return "";
  const parts: string[] = [];
  if (report.removed.length > 0) {
    const ids = report.removed.map((item) => item.id).join("、");
    parts.push(`${report.dry_run ? "会清掉" : "已清掉"} ${report.removed.length} 个：${ids}`);
  }
  if (report.kept.length > 0) {
    const ids = report.kept.map((item) => item.id).join("、");
    parts.push(`留着 ${report.kept.length} 个（${ids}）—— 它们本身合格，该入库、不是该删`);
  }
  if (report.strays.length > 0) {
    parts.push(`另有 ${report.strays.length} 个名字不合规的**没动**（它们不在这次清理的范围里）`);
  }
  if (parts.length === 0) {
    return "这一类没有不合格的孤儿 —— 盘上那些要么已经入库，要么该入库。";
  }
  return `${parts.join("；")}。`;
}

/**
 * 选完文件**立刻**上传（没有第二个按钮）。
 *
 * 这一栏的语义就是"把文件放进素材库"：再让用户点一次「上传」，只是多一次
 * "我以为点了"的机会。上传中会禁用文件框，所以也不会选重。
 */
async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  // 清空 value：同一个文件再选一次也要能触发 change（否则第二次点它毫无反应）
  input.value = "";
  await sendFiles(files);
}

/** 拖进来的文件走**同一条**路（拖拽只是另一个"选文件"的入口，不是另一套逻辑）。 */
async function onDrop(event: DragEvent): Promise<void> {
  await sendFiles(Array.from(event.dataTransfer?.files ?? []));
}

async function sendFiles(files: File[]): Promise<void> {
  if (files.length === 0) return;
  if (props.kind === "voice") await assets.uploadVoiceFiles(files);
  else await assets.uploadFiles(props.kind, files);
}

// ── 逐行：启停 / 授权 / 编辑器 ──────────────────────────────────────────

async function onToggle(item: AssetItem, event: Event): Promise<void> {
  await assets.setEnabled(item, (event.target as HTMLInputElement).checked);
}

async function onItemLicense(item: AssetItem, event: Event): Promise<void> {
  await assets.applyPatch(item, { license: (event.target as HTMLSelectElement).value });
}

/**
 * 点「改」：**先把当前值抄进草稿**，再开这一行。
 *
 * 抄草稿必须发生在"开"的那一刻，而不是每次渲染都抄 —— 后者会把用户刚敲进去的字
 * 在下一帧抹掉。同一行再点一次是"关"（`toggleEditor` 的语义）。
 */
function onEdit(item: AssetItem): void {
  confirmId.value = null;
  if (assets.editingId !== item.id) draft.value = fieldDraft(item);
  assets.toggleEditor(item);
}

/**
 * 删这一行要不要**连盘上那份一起删**。
 *
 * 音色要：配音只认库里的行，而参考音目录留在盘上，下次扫盘又会变成一条"盘上有、
 * 库里没有" —— 用户刚删掉的东西自己回来了。
 * 跑酷 / BGM 不要：文件留在盘上、出片照样挑得到，删掉的只是留痕（授权、时长、
 * 缩略图），而重扫一次就能补回来。
 */
function purgeFor(kind: AssetKind): boolean {
  return kind === "voice";
}

/** 二次确认那一行里说清**这次会删掉什么**（两种结局差得很远，不能共用一句话）。 */
function deleteNote(item: AssetItem): string {
  return purgeFor(item.kind)
    ? "会从库里移除，并删掉盘上的参考音目录（不可撤销）"
    : "会从库里移除，盘上的文件保留（重扫一次就回来）";
}

function onDelete(item: AssetItem): void {
  confirmId.value = item.id;
}

function onCancelDelete(): void {
  confirmId.value = null;
}

async function onConfirmDelete(item: AssetItem): Promise<void> {
  // 失败时**不关**确认条：错误就在旁边，用户能看着它再点一次。
  if (await assets.remove(item, purgeFor(item.kind))) {
    confirmId.value = null;
    if (assets.editingId === item.id) assets.editingId = null;
  }
}

function draftText(key: string): string {
  const value = draft.value[key];
  return typeof value === "string" ? value : "";
}

function draftBool(key: string): boolean {
  return draft.value[key] === true;
}

function onFieldInput(key: string, event: Event): void {
  draft.value = { ...draft.value, [key]: (event.target as HTMLInputElement).value };
}

function onFieldCheck(key: string, event: Event): void {
  draft.value = { ...draft.value, [key]: (event.target as HTMLInputElement).checked };
}

async function onSave(item: AssetItem): Promise<void> {
  const ok = await assets.saveFields(item, draft.value);
  if (ok) draft.value = {};
}

// ── 音色：逐段管理（裁定 381）──────────────────────────────────────────

/** 展开 / 收起逐段表（同时收掉别的行：两行的段列表摊在一起会看串）。 */
async function onSegments(item: AssetItem): Promise<void> {
  confirmId.value = null;
  confirmSegment.value = null;
  await assets.toggleSegments(item);
}

async function onRemoveSegment(voiceId: string, name: string): Promise<void> {
  // 失败时**不关**确认条：错误就在旁边，用户能看着它再点一次。
  if (await assets.removeSegment(voiceId, name)) confirmSegment.value = null;
}

/**
 * 这一栏那句提示：**三类各说各的**。
 *
 * 音色的「覆盖同名」比跑酷 / BGM 重一档：它是**镜像**（这次没传到的旧段会被清掉），
 * 而跑酷 / BGM 只是"同名那个文件换掉"。两者用同一句话，用户会按轻的那一档去理解，
 * 然后在音色上被清掉一段而毫无预期。
 */
function overwriteHint(): string {
  if (assets.overwrite) {
    return props.kind === "voice"
      ? "同名会被替换，**这次没传到的旧段会被清掉**（镜像）"
      : "同名会被替换";
  }
  return props.kind === "voice"
    ? "撞名会跳过；要换掉旧段请勾上「覆盖同名」（那是镜像：传进去的就是全部）"
    : "撞名会跳过，不会盖掉已有素材";
}


/** 逐段表顶上那句"N 段 / M 行文本"—— 两个数不等就是**文本与音频对不上**。 */
function segmentsSummary(voiceId: string): string {
  const view = assets.segmentsFor(voiceId);
  if (view === null) return "读取中…";
  return `${view.ref_count} 段 · ref.txt ${view.text_lines} 行`;
}

/** 逐段表顶上的灯：段数与文本行数对得上、且没有不合格的段 ⇒ 绿。 */
function segmentsTone(voiceId: string): StatusTone {
  const view = assets.segmentsFor(voiceId);
  if (view === null) return "warn";
  if (view.problems.length > 0) return "warn";
  if (view.ref_count !== view.text_lines) return "warn";
  return "ok";
}

// ── 空态 ────────────────────────────────────────────────────────────────

function emptyTitle(): string {
  if (assets.page === null) return "正在读这一类素材…";
  if (filtered.value) return "这一页没有匹配的素材";
  const pending = assets.page.pending.length;
  if (pending > 0) return `盘上那 ${pending} 条还没进库`;
  return "这一类还没有素材";
}

/**
 * 空列表时该说往哪放 —— **分几种原因**，说混了会把"命名写错了"读成"素材没放进去"。
 *
 * 筛选排在最前面：搜出 0 条时说"把文件放进目录"，会让人真的去翻目录找那批素材。
 */
function emptyHint(): string {
  const page = assets.page;
  if (page === null) return "稍等，第一次拉取还要读一遍目录。";
  if (filtered.value) {
    return `这一类里本来有 ${page.stats.total} 条，是当前这个筛选一条都没匹配上 —— 清掉搜索框、或把筛选改回「全部」。`;
  }
  if (page.pending.length > 0) {
    return `盘上那 ${page.pending.length} 条已经在等着了 —— 点上面的「把这一类入库」登记它们（授权、时长、缩略图）。`;
  }
  if (page.strays.length > 0) {
    return `目录里有文件，但命名不合规（上面列出来了）。改成 ${hint.value} 这个形状再扫一次。`;
  }
  return `把文件放进 ${hint.value} 再点「扫描并入库」，或者直接用上面的「选择文件上传」（传上去就入库了）。`;
}
</script>

<template>
  <PanelCard fill dense :title="title" :subtitle="coverageText(assets.page)">
    <template #actions>
      <select
        class="field mono"
        :value="assets.license"
        title="新入库素材的授权类型"
        @change="onLicenseChange"
      >
        <option v-for="value in assets.licenses" :key="value" :value="value">
          {{ LICENSE_LABELS[value] ?? value }}
        </option>
      </select>
      <label class="toggle" title="上传时允许替换同名文件（默认拒绝：绝不静默盖掉已有素材）">
        <input type="checkbox" :checked="assets.overwrite" @change="onOverwriteChange" />
        <span>覆盖同名</span>
      </label>
      <label class="toggle" title="只扫盘、不写库（先看看会怎样）。上传不受它影响：上传要么进去、要么没进去">
        <input type="checkbox" :checked="assets.dryRun" @change="onDryRunChange" />
        <span>预览模式</span>
      </label>
      <AppButton :loading="assets.busy" @click="onScan">
        {{ assets.dryRun ? "扫一遍（不写库）" : "扫描并入库" }}
      </AppButton>
    </template>

    <div class="assets">
      <!-- R2 来源登记提示：**常驻**（§06.11 要求发布面板与素材库两处都有）。
           文案与缺口都来自服务端那一份，这一屏一个字都不自己写。 -->
      <p v-if="assets.compliance" class="r2">
        {{ plainText(assets.compliance.notice) }}
        <span v-if="!assets.compliance.ok" class="r2__gap">
          现在缺 {{ assets.compliance.gaps?.length ?? 0 }} 份登记 —— 在下面每一行的「改」里补授权类型 / 来源地址即可，缺了不阻塞出片。
        </span>
      </p>

      <p v-if="assets.loadError" class="alert alert--error">读素材库失败：{{ assets.loadError }}</p>
      <p v-if="assets.error" class="alert alert--error">{{ assets.error }}</p>
      <p v-if="isBroll && assets.degradedNote" class="alert alert--warn">
        降级模式：{{ assets.degradedNote }}
      </p>

      <section class="block">
        <header class="block__head">
          <h3 class="block__title">
            <StatusDot :tone="tone" :label="title" />
          </h3>
          <span class="mono muted">{{ coverageText(assets.page) }}</span>
        </header>

        <p class="hint mono">目录：{{ assets.page?.root ?? hint }}</p>
        <p class="hint">放这里：{{ hint }}</p>

        <!-- 上传（T4.8 的图形化入库入口）：**落盘 + 入库一次做完**，不让人传完再去点一次扫描。
             它**不受「预览模式」影响** —— 那一栏是"扫盘先看看会怎样"的开关，而"把这个文件
             放进素材库"没有预览这一说：要么进去了，要么没有（逐条报出来）。 -->
        <div
          class="upload"
          :class="{ 'upload--busy': assets.uploadBusy }"
          @dragover.prevent
          @drop.prevent="onDrop"
        >
          <template v-if="props.kind === 'voice'">
            <input
              v-model="assets.voiceId"
              class="field"
              placeholder="音色 id（小写字母 / 数字 / _ -，如 bear_da）"
            />
            <textarea
              v-model="assets.voiceText"
              class="field field--text"
              rows="2"
              placeholder="参考音文字稿（可选）：一行对应一段，第 1 行 ↔ ref_01"
            />
          </template>
          <label class="pick">
            <input
              type="file"
              multiple
              :accept="acceptFor(props.kind)"
              :disabled="assets.uploadBusy"
              @change="onPick"
            />
            <span>{{ assets.uploadBusy ? "上传中…" : "选择文件上传" }}</span>
          </label>
          <span class="muted">
            或把文件拖到这一节里（{{ overwriteHint() }}）
          </span>
        </div>

        <table v-if="uploadOf(assets.upload, props.kind)" class="table">
          <thead>
            <tr>
              <th>上传的文件</th>
              <th>素材 id</th>
              <th>结局</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(row, index) in uploadOf(assets.upload, props.kind)?.files ?? []"
              :key="`up-${index}`"
            >
              <td class="mono">{{ row.filename }}</td>
              <td class="mono">{{ row.asset_id ?? "—" }}</td>
              <td>
                <StatusDot
                  :tone="uploadTone(row)"
                  :label="UPLOAD_LABELS[row.status] ?? row.status"
                />
              </td>
              <td class="muted">{{ row.message }}</td>
            </tr>
          </tbody>
        </table>

        <!-- 落盘成功 ≠ 入库成功。上面那三行说的是**前者**，这条红带说的是后者 ——
             放在回执正下方，因为用户看完"已落盘"就会往下走：把原因埋在页尾的
             扫盘报告里，等于没提示。 -->
        <div v-if="uploadBlocked.length > 0" class="alert alert--error">
          <p>
            {{ uploadBlocked.length }} 条已经落盘，但<strong>没入库</strong> —— 它们现在还用不了：
          </p>
          <ul class="blocked">
            <li v-for="row in uploadBlocked" :key="row.id">
              <span class="mono">{{ row.id }}</span>
              <span class="bad">{{ row.check.problems.map((p) => p.message).join("；") }}</span>
              <span v-if="row.check.warnings.length > 0" class="muted">
                （提醒：{{ row.check.warnings.map((p) => p.message).join("；") }}）
              </span>
            </li>
          </ul>
        </div>

        <!-- 覆盖的账（裁定 381）：勾了「覆盖同名」之后，这次没传到的旧段会被清掉。
             清掉了什么、以及"什么没动但你应该知道"，都要在这里说 —— 不说的话，
             用户看到的是"我覆盖了，可它还是 3 段"。 -->
        <div v-if="(assets.upload?.removed.length ?? 0) > 0" class="alert alert--warn">
          <p>
            覆盖同名 ⇒ 这次没传到的旧段已经清掉 {{ assets.upload?.removed.length }} 个：
            <span class="mono">{{ assets.upload?.removed.join("、") }}</span>
          </p>
          <p class="muted">
            「覆盖同名」的语义是<strong>镜像</strong>：这次传进去的就是全部。只管同名的那几个，
            会留下一份"两边都不是"的目录 —— 新传 2 段、旧的第 3 段还在，库里照样报 3 段。
          </p>
        </div>

        <div v-if="(assets.upload?.notes.length ?? 0) > 0" class="alert alert--info">
          <ul class="blocked">
            <li v-for="note in assets.upload?.notes ?? []" :key="note">{{ note }}</li>
          </ul>
        </div>

        <p v-if="assets.page?.root_missing" class="alert alert--warn">
          目录还没建 —— 点「扫描并入库」会把它建出来。
        </p>
        <p v-if="shortfallText(assets.page)" class="alert alert--warn">
          {{ shortfallText(assets.page) }}
        </p>

        <div v-if="(assets.page?.pending.length ?? 0) > 0" class="alert alert--info">
          <p>盘上有 {{ assets.page?.pending.length }} 条还没入库 —— {{ pendingNote(props.kind) }}。</p>
          <p class="mono pending">{{ pendingText(assets.page) }}</p>
          <div class="row-actions">
            <AppButton size="sm" :disabled="assets.busy" @click="onScan">
              {{ assets.dryRun ? "扫一遍（不写库）" : "把这一类入库" }}
            </AppButton>
            <!-- 孤儿 = 盘上认得出、库里没有的那些。**它们以前删不掉**：`DELETE /assets/{id}`
                 的对象是库里那一行，而孤儿没有行。所以这里给的是唯一一条出路 —— 而且
                 只清**本身不合格**的（合格的该入库），判据在后端。 -->
            <AppButton
              size="sm"
              :variant="confirmPrune ? 'danger' : 'ghost'"
              :disabled="assets.pruneBusy"
              @click="onPrune"
            >
              {{ pruneLabel() }}
            </AppButton>
            <AppButton v-if="confirmPrune" size="sm" :disabled="assets.pruneBusy" @click="onCancelPrune">
              取消
            </AppButton>
          </div>
        </div>

        <!-- 回执画在**外面**：清完 pending 就空了，那个块自己会消失，回执跟着一起没影。 -->
        <p v-if="pruneNote()" class="alert alert--info">{{ pruneNote() }}</p>

        <p v-if="(assets.page?.strays.length ?? 0) > 0" class="alert alert--warn">
          目录里有 {{ assets.page?.strays.length }} 个文件没被认出来（命名不合规，<strong>没有</strong>入库）：
          <span class="mono">{{ assets.page?.strays.join("、") }}</span>
        </p>

        <!-- 工具条：搜索 + 三态筛选 + 每页条数。三个都在**服务端**生效，换一个就回第 1 页。 -->
        <div class="toolbar">
          <input
            class="field field--search"
            type="search"
            :value="queryInput"
            placeholder="搜 id 或标签…"
            @input="onQuery"
          />
          <select class="field" :value="assets.enabledFilter" @change="onEnabledFilter">
            <option v-for="option in ENABLED_FILTERS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
          <select
            class="field"
            :value="assets.pageSize"
            title="每页几条"
            @change="onPageSize"
          >
            <option v-for="value in PAGE_SIZE_OPTIONS" :key="value" :value="value">
              每页 {{ value }}
            </option>
          </select>
          <span class="mono muted">{{ totalText(assets.page) }}</span>
          <span class="toolbar__spacer" />
          <span v-if="assets.loading" class="muted">读取中…</span>
        </div>

        <EmptyState v-if="assets.items.length === 0" :title="emptyTitle()" :hint="emptyHint()" />

        <table v-else class="table">
          <thead>
            <tr>
              <th>预览</th>
              <th>id</th>
              <th>时长</th>
              <th>授权</th>
              <th v-if="isBroll">可用区间</th>
              <th>用量</th>
              <th>状态</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <template v-for="item in assets.items" :key="item.id">
              <tr>
                <td class="thumb-cell">
                  <img
                    v-if="thumbUrlOf(item)"
                    class="thumb"
                    :src="thumbUrlOf(item) ?? ''"
                    :alt="item.id"
                  />
                  <span v-else class="muted">无预览</span>
                </td>
                <td class="mono">
                  {{ item.id }}
                  <span v-if="isPlaceholder(item)" class="chip chip--warn">占位</span>
                  <span v-if="item.kind === 'broll' && item.has_text" class="chip chip--warn">
                    自带文字
                  </span>
                </td>
                <td class="mono">
                  {{ formatDuration(item.kind === "voice" ? item.total_duration_ms : item.duration_ms) }}
                  <span v-if="item.kind === 'voice'" class="muted">· {{ item.ref_count }} 段</span>
                </td>
                <td>
                  <select
                    class="field mono"
                    :value="item.license ?? ''"
                    :disabled="assets.pendingId === item.id"
                    @change="onItemLicense(item, $event)"
                  >
                    <option v-for="value in assets.licenses" :key="value" :value="value">
                      {{ LICENSE_LABELS[value] ?? value }}
                    </option>
                  </select>
                </td>
                <td v-if="isBroll" class="mono muted">{{ usableRangeText(item) }}</td>
                <td class="mono muted">{{ item.use_count }} 次</td>
                <td>
                  <StatusDot :tone="itemTone(item)" :label="itemStateText(item)" />
                  <label class="toggle">
                    <input
                      type="checkbox"
                      :checked="item.enabled"
                      :disabled="assets.pendingId === item.id"
                      @change="onToggle(item, $event)"
                    />
                    <span>{{ item.enabled ? "启用" : "停用" }}</span>
                  </label>
                  <audio
                    v-if="mediaUrlOf(item)"
                    class="player"
                    controls
                    preload="none"
                    :src="mediaUrlOf(item) ?? ''"
                  />
                </td>
                <td>
                  <div class="row-actions">
                  <!-- 「段落」只在音色上出现：跑酷 / BGM 是**一个文件**，没有"由哪几段拼的"
                       这回事。给它画一个永远展开不出东西的按钮，比不画更糟。 -->
                  <AppButton
                    v-if="item.kind === 'voice'"
                    size="sm"
                    :disabled="assets.segmentsBusy && assets.segmentsId === item.id"
                    @click="onSegments(item)"
                  >
                    {{ assets.segmentsId === item.id ? "收起" : "段落" }}
                  </AppButton>
                  <AppButton size="sm" :disabled="assets.pendingId === item.id" @click="onEdit(item)">
                    {{ assets.editingId === item.id ? "收起" : "改" }}
                  </AppButton>
                  <template v-if="confirmId === item.id">
                    <AppButton
                      size="sm"
                      variant="danger"
                      :disabled="assets.pendingId === item.id"
                      @click="onConfirmDelete(item)"
                    >
                      确认删除
                    </AppButton>
                    <AppButton size="sm" :disabled="assets.pendingId === item.id" @click="onCancelDelete">
                      取消
                    </AppButton>
                    <span class="muted row-actions__note">{{ deleteNote(item) }}</span>
                  </template>
                  <AppButton
                    v-else
                    size="sm"
                    :disabled="assets.pendingId === item.id"
                    @click="onDelete(item)"
                  >
                    删除
                  </AppButton>
                  </div>
                </td>
              </tr>

              <!-- 音色的逐段表（裁定 381）：段号 / 文件 / 时长 / 采样率 / 峰值 / **同一位置
                   的那行文本** / 那一段自己的问题 / 删。位置即对应 —— 所以「第 2 行文本」配的就是
                   「第 2 段音频」，删掉一段之后服务端会把后面的重编号，回执里的 renamed 会说。 -->
              <tr
                v-if="item.kind === 'voice' && assets.segmentsId === item.id"
                class="segments-row"
              >
                <td :colspan="isBroll ? 8 : 7">
                  <div class="segments">
                    <div class="segments__head">
                      <StatusDot :tone="segmentsTone(item.id)" :label="segmentsSummary(item.id)" />
                      <span class="muted">
                        合成时这几段会**按顺序拼成一段**一起喂给引擎 —— 越多越像，但每一段都要与
                        下面那行文字逐字对得上。
                      </span>
                      <AppButton
                        size="sm"
                        :disabled="assets.segmentsBusy"
                        @click="assets.loadSegments(item.id)"
                      >
                        重新读取
                      </AppButton>
                    </div>

                    <p v-if="!assets.segmentsFor(item.id)" class="muted">读取中…</p>

                    <template v-else>
                      <p
                        v-if="assets.segmentsFor(item.id)?.ref_count !== assets.segmentsFor(item.id)?.text_lines"
                        class="alert alert--warn"
                      >
                        参考音 {{ assets.segmentsFor(item.id)?.ref_count }} 段，而 ref.txt 有
                        {{ assets.segmentsFor(item.id)?.text_lines }} 行 ——
                        <strong>两者对不上</strong>。文本与音频必须逐段一一对应（第 N 行 ↔ 第 N 段），
                        对不上的那几段克隆质量会打折。
                      </p>

                      <p
                        v-if="assets.segmentsFor(item.id)?.in_library === false"
                        class="alert alert--info"
                      >
                        盘上有这个目录，但它<strong>还没入库</strong> —— 点上面的「把这一类入库」登记它。
                      </p>

                      <table class="table table--inner">
                        <thead>
                          <tr>
                            <th>段</th>
                            <th>文件</th>
                            <th>时长</th>
                            <th>采样率</th>
                            <th>峰值</th>
                            <th>对应文本（ref.txt 同一行）</th>
                            <th />
                          </tr>
                        </thead>
                        <tbody>
                          <tr v-for="row in assets.segmentsFor(item.id)?.segments ?? []" :key="row.name">
                            <td class="mono">第 {{ row.index }} 段</td>
                            <td class="mono">{{ row.name }}</td>
                            <td class="mono">{{ formatDuration(row.duration_ms) }}</td>
                            <td class="mono muted">
                              {{ row.sample_rate === null ? "—" : `${row.sample_rate} Hz` }}
                            </td>
                            <td class="mono muted">
                              {{ row.peak_db === null ? "—" : `${row.peak_db.toFixed(1)} dBFS` }}
                            </td>
                            <td>
                              <span v-if="row.text !== null">{{ row.text }}</span>
                              <span v-else class="bad">没有对应文本（这一段克隆时会没有参考文本）</span>
                              <span v-if="row.problems.length > 0" class="bad">
                                {{ row.problems.map((p) => p.message).join("；") }}
                              </span>
                            </td>
                            <td>
                              <div class="row-actions">
                                <template v-if="confirmSegment === row.name">
                                  <AppButton
                                    size="sm"
                                    variant="danger"
                                    :loading="assets.segmentPending === row.name"
                                    @click="onRemoveSegment(item.id, row.name)"
                                  >
                                    确认删这一段
                                  </AppButton>
                                  <AppButton size="sm" @click="confirmSegment = null">取消</AppButton>
                                  <span class="muted row-actions__note">
                                    从盘上删掉 {{ row.name }}，后面的段会<strong>重编号</strong>
                                    （ref.txt 同一行也会一起删）
                                  </span>
                                </template>
                                <AppButton
                                  v-else
                                  size="sm"
                                  :disabled="(assets.segmentsFor(item.id)?.segments.length ?? 0) <= 1"
                                  :title="
                                    (assets.segmentsFor(item.id)?.segments.length ?? 0) <= 1
                                      ? '这是最后一段 —— 删了音色就念不出来了（整条不要请用「删除」）'
                                      : '删掉这一段'
                                  "
                                  @click="confirmSegment = row.name"
                                >
                                  删
                                </AppButton>
                              </div>
                            </td>
                          </tr>
                        </tbody>
                      </table>

                      <!-- 删段的回执：**重编号那本账要显示出来** —— 用户手上的文件名变了，
                           而静默改名是这一屏最不能有的一种行为。 -->
                      <div v-if="assets.segmentResult" class="alert alert--info">
                        <p>
                          已删掉 <span class="mono">{{ assets.segmentResult.removed }}</span>
                          <span v-if="assets.segmentResult.removed_text">
                            （它那一行文本是「{{ assets.segmentResult.removed_text }}」，也一起删了）
                          </span>
                          <span v-else>（它本来就没有对应文本）</span>
                        </p>
                        <p v-if="assets.segmentResult.renamed.length > 0" class="mono">
                          重编号：
                          {{
                            assets.segmentResult.renamed
                              .map((entry) => `${entry.from} ⇒ ${entry.to}`)
                              .join("、")
                          }}
                        </p>
                        <p v-if="!assets.segmentResult.text_rewritten" class="muted">
                          ref.txt 这次**没动** —— 见下面那几句说明。
                        </p>
                        <ul v-if="assets.segmentResult.notes.length > 0" class="blocked">
                          <li v-for="note in assets.segmentResult.notes" :key="note">
                            {{ note }}
                          </li>
                        </ul>
                      </div>
                    </template>
                  </div>
                </td>
              </tr>

              <!-- 逐行编辑器：字段清单按类别取（`EDIT_FIELDS`），三份模板共用这一段。
                   只交改过的字段 —— 没动过的不会被这一行用"打开编辑器那一刻的旧值"覆盖回去。 -->
              <tr v-if="assets.editingId === item.id" class="editor-row">
                <td :colspan="isBroll ? 8 : 7">
                  <div class="editor">
                    <label v-for="field in fields" :key="field.key" class="editor__field">
                      <span class="editor__label">{{ field.label }}</span>
                      <input
                        v-if="field.type === 'bool'"
                        type="checkbox"
                        :checked="draftBool(field.key)"
                        @change="onFieldCheck(field.key, $event)"
                      />
                      <input
                        v-else-if="field.type === 'number'"
                        class="field field--num"
                        type="number"
                        :value="draftText(field.key)"
                        :placeholder="field.placeholder ?? ''"
                        @input="onFieldInput(field.key, $event)"
                      />
                      <input
                        v-else
                        class="field"
                        type="text"
                        :value="draftText(field.key)"
                        :placeholder="field.placeholder ?? ''"
                        @input="onFieldInput(field.key, $event)"
                      />
                      <span v-if="field.hint" class="editor__hint">{{ field.hint }}</span>
                    </label>
                  </div>
                  <div class="editor__actions">
                    <AppButton
                      size="sm"
                      variant="primary"
                      :loading="assets.pendingId === item.id"
                      @click="onSave(item)"
                    >
                      保存
                    </AppButton>
                    <AppButton
                      size="sm"
                      :disabled="assets.pendingId === item.id"
                      @click="onEdit(item)"
                    >
                      取消
                    </AppButton>
                    <span class="muted">保存时只交改过的字段，没动过的不会被覆盖回去。</span>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>

        <footer v-if="assets.items.length > 0" class="pager">
          <AppButton
            size="sm"
            :disabled="assets.pageIndex <= 1 || assets.loading"
            @click="assets.goPage(assets.pageIndex - 1)"
          >
            上一页
          </AppButton>
          <button
            v-for="entry in pageButtons"
            :key="entry.key"
            type="button"
            class="page"
            :class="{ 'page--active': entry.page === assets.pageIndex, 'page--gap': entry.page === null }"
            :disabled="entry.page === null || assets.loading"
            @click="onPage(entry)"
          >
            {{ entry.label }}
          </button>
          <AppButton
            size="sm"
            :disabled="assets.pageIndex >= assets.pages || assets.loading"
            @click="assets.goPage(assets.pageIndex + 1)"
          >
            下一页
          </AppButton>
          <span class="mono muted">第 {{ assets.pageIndex }} / {{ assets.pages }} 页</span>
        </footer>
      </section>

      <!-- 扫盘报告：只画**这一类**。别的类别的结果出现在这个菜单里，只会让人以为
           "我在音色库点了一下，怎么把跑酷又扫了一遍"。 -->
      <section v-if="assets.report" class="block">
        <header class="block__head">
          <h3 class="block__title">扫盘报告</h3>
          <span class="mono muted">
            {{ assets.report.dry_run ? "预览（未写库）" : "已入库" }} · 新增
            {{ assets.report.totals.created }} / 刷新 {{ assets.report.totals.refreshed }} / 未变
            {{ assets.report.totals.unchanged }} / 重复 {{ assets.report.totals.duplicate }} / 未入库
            {{ assets.report.totals.rejected }}
          </span>
        </header>
        <table v-if="scannedRows.length > 0" class="table">
          <thead>
            <tr>
              <th>id</th>
              <th>动作</th>
              <th>体检</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in scannedRows" :key="row.id">
              <td class="mono">{{ row.id }}</td>
              <td class="mono">
                <StatusDot
                  :tone="scannedTone(row)"
                  :label="row.action ? (ACTION_LABELS[row.action] ?? row.action) : '未写库'"
                />
              </td>
              <td>
                <span v-if="row.check.ok" class="muted">通过</span>
                <span v-else class="bad">
                  {{ row.check.problems.map((p) => p.message).join("；") }}
                </span>
              </td>
              <td class="muted">{{ row.note ?? "—" }}</td>
            </tr>
          </tbody>
        </table>
        <EmptyState
          v-else
          title="这一趟没扫到这一类素材"
          :hint="`确认文件放进了对应目录、命名符合约定（${hint}）。`"
        />
      </section>
    </div>
  </PanelCard>
</template>

<style scoped>
.r2 {
  padding: var(--space-2) var(--space-3);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  line-height: 1.7;
  background: var(--bg-raised);
  border-left: 2px solid var(--warn);
  border-radius: var(--radius-sm);
}

.r2__gap {
  color: var(--warn);
}

.assets {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-4);
  overflow: auto;
}

.block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
}

.block__head {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  justify-content: space-between;
}

.block__title {
  font-size: var(--text-lg);
  font-weight: 600;
}

.hint {
  color: var(--text-muted);
  font-size: var(--text-sm);
}

.alert {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  border-radius: var(--radius-sm);
}

.alert--error {
  color: var(--error);
  background: rgba(242, 99, 122, 0.1);
}

.alert--warn {
  color: var(--warn);
  background: rgba(229, 181, 103, 0.1);
}

.alert--info {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  align-items: flex-start;
  color: var(--text-secondary);
  background: rgba(120, 170, 255, 0.1);
}

.pending {
  max-height: 72px;
  overflow: auto;
  font-size: var(--text-xs);
  line-height: 1.6;
  word-break: break-all;
}

/* 没入库那几条：一行一条，id 与原因分开列 —— 挤成一段话就看不出是哪个文件的问题。 */
.blocked {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding-left: var(--space-4);
  margin: var(--space-1) 0 0;
  list-style: disc;
}

.blocked li {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: baseline;
}

/* 「改 / 删除」并排；二次确认那一句**换行**跟在下面 —— 挤进同一行会把列宽撑开，
   一进确认态整张表的列就跳一下。 */
.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
  align-items: center;
}

.row-actions__note {
  flex-basis: 100%;
  font-size: var(--text-xs);
  line-height: 1.5;
}

.bad {
  color: var(--error);
}

.chip {
  margin-left: var(--space-1);
  padding: 0 var(--space-1);
  font-size: var(--text-xs);
  border-radius: var(--radius-sm);
}

.chip--warn {
  color: var(--warn);
  background: rgba(229, 181, 103, 0.14);
}

.muted {
  color: var(--text-muted);
}

/* 工具条（搜索 / 筛选 / 每页条数 / 两个数字）。它是**常驻**的：翻到第 3 页才发现
   自己是筛过的，是最容易让人以为"素材丢了"的一种。 */
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  padding: var(--space-2) 0;
  border-top: 1px solid var(--border-subtle);
  border-bottom: 1px solid var(--border-subtle);
}

.toolbar__spacer {
  flex: 1;
}

.field--search {
  flex: 1 1 200px;
  min-width: 160px;
}

.table {
  width: 100%;
  font-size: var(--text-sm);
  border-collapse: collapse;
}

.table th {
  color: var(--text-secondary);
  font-weight: 500;
  text-align: left;
}

.table th,
.table td {
  padding: var(--space-1) var(--space-2);
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}

.thumb-cell {
  width: 96px;
}

.thumb {
  width: 88px;
  height: 50px;
  object-fit: cover;
  background: var(--bg-base);
  border-radius: var(--radius-sm);
}

.field {
  height: 26px;
  padding: 0 var(--space-2);
  color: var(--text-primary);
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.field--num {
  width: 96px;
}

.field--text {
  height: auto;
  padding: var(--space-1) var(--space-2);
  font-family: inherit;
  resize: vertical;
}

/* 音色的逐段表（裁定 381）：与编辑器同一种"一行摊开"的结构，所以背景取同一个底色。 */
.segments-row > td {
  background: var(--bg-base);
}

.segments {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) 0;
}

.segments__head {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-wrap: wrap;
}

/* 嵌在逐段表里的那张表：它比外层矮一档，别让两个表头看起来是同一个层级。 */
.table--inner {
  margin-top: var(--space-1);
}

.table--inner th {
  font-size: var(--text-xs);
}

/* 编辑器：一行摊开这一类的全部可改字段（字段清单来自 store，不手写三份模板）。 */
.editor-row > td {
  background: var(--bg-base);
}

.editor {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  padding: var(--space-2) 0;
}

.editor__field {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 160px;
}

.editor__label {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.editor__hint {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.editor__actions {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  padding-bottom: var(--space-2);
}

/* 翻页条 */
.pager {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  padding-top: var(--space-2);
}

.page {
  min-width: 26px;
  height: 26px;
  color: var(--text-secondary);
  font-size: var(--text-sm);
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.page:hover:not(:disabled) {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.page--active {
  color: var(--text-primary);
  background: var(--accent-soft);
  border-color: var(--accent);
}

.page--gap {
  background: transparent;
  border-color: transparent;
}

/* 上传区（拖拽落点 + 文件选择）。虚线框是"这里可以拖东西进来"的通用画法 */
.upload {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  padding: var(--space-2);
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-sm);
}

.upload--busy {
  opacity: 0.65;
}

.upload .field {
  flex: 1 1 220px;
  min-width: 200px;
}

.pick {
  display: inline-flex;
  gap: var(--space-1);
  align-items: center;
  font-size: var(--text-sm);
}

.pick input {
  max-width: 260px;
  font-size: var(--text-xs);
}

.toggle {
  display: inline-flex;
  gap: var(--space-1);
  align-items: center;
  font-size: var(--text-sm);
}

.player {
  height: 26px;
  margin-left: var(--space-2);
  vertical-align: middle;
}
</style>
