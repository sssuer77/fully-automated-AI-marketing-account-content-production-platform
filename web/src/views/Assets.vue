<script setup lang="ts">
// ⑦ 素材库面板（T4.8 · §3.3.14 / §4.3.1 / §04.5.12）。
//
// 这一屏要回答五个问题，缺一个都会让人不再信任它：
// ① "**出片到底会不会用到它**" —— 每节一行三组数字 + 每行一个状态（`usable` 由后端算）；
// ② "盘上还有没入库的" —— 未入库那批显式列出 + 一个入库入口（它们**照样会被出片挑到**）；
// ③ "够不够用、缺多少" —— 缺口那句话（后端算的判据线，前端不抄第二份）；
// ④ "这条启用还是停用" —— 标记（**只有启用/停用，没有删除**：误删不可逆）；
// ⑤ "素材长什么样、听着对不对" —— 缩略图 + 试听（URL 直接交给 img/audio）。
//
// 为什么要有 ①：出片挑素材走的是 `render/assets.py`，它**只列目录、不读库**。
// 所以"库里有什么"和"出片会挑到什么"是**两个真相**，而面板只能说一个。曾经它说的是
// 库里那个 —— 于是盘上 58 条一条没入库时，面板写着"跑酷素材 0 条"，而片子里正放着跑酷。
// 用户看到这个之后，这一屏上的任何数字他都不会再信。
//
// 三态必须分开画（这是"清晰操作"的全部意思）
// ------------------------------------------
// ① **出片会挑到**（绿）—— 盘上有文件、库里启用着；
// ② **已停用**（黄）—— 人自己点的，随时能点回来，出片不再挑它；
// ③ **文件不在了**（红）—— 行还在、开关还亮着，而文件被人挪走了。
// 第 ③ 种是最阴的：面板上只画开关的话，用户会看着一个绿点，纳闷片子为什么一直用别的底片。
//
// 另外三个容易踩的点
// ------------------
// ① **目录路径常驻**：素材是"人往目录里丢文件"的，面板不写清往哪放，用户就只能去翻文档；
// ② **占位素材标黄**：`origin=generated` 的占位件能播、但一眼要看出它只是占位；
// ③ **空态说清往哪放**：空列表有三种原因（还没放 / 命名不合规进了 strays / 盘上有但没入库），
//    三种提示必须分开说，否则"命名写错了"会被读成"素材没放进去"。

import { onBeforeUnmount, onMounted, ref } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import type { AssetItem, AssetKind, IngestSection, ScannedAsset } from "@/api/endpoints/assets";
import type { StatusTone } from "@/components/tone";
import {
  ACTION_LABELS,
  ASSET_KINDS,
  KIND_HINTS,
  KIND_LABELS,
  LICENSE_LABELS,
  coverageText,
  formatDuration,
  isPlaceholder,
  itemStateText,
  itemTone,
  mediaUrlOf,
  pendingNote,
  pendingText,
  scannedTone,
  thumbUrlOf,
  usableText,
  useAssetsStore,
} from "@/stores/assets";

const assets = useAssetsStore();

/** 可用区间的草稿（按 id 存；不放进 store —— 它只是一次编辑的中间态）。 */
const usableDraft = ref<Record<string, { from: number; to: number | null }>>({});

onMounted(() => {
  assets.start();
});

onBeforeUnmount(() => {
  assets.stop();
});

function onLicenseChange(event: Event): void {
  assets.license = (event.target as HTMLSelectElement).value;
}

function onDryRunChange(event: Event): void {
  assets.dryRun = (event.target as HTMLInputElement).checked;
}

async function onScan(kind?: AssetKind): Promise<void> {
  await assets.scan(kind);
}

async function onToggle(item: AssetItem, event: Event): Promise<void> {
  await assets.setEnabled(item, (event.target as HTMLInputElement).checked);
}

async function onItemLicense(item: AssetItem, event: Event): Promise<void> {
  await assets.applyPatch(item, { license: (event.target as HTMLSelectElement).value });
}

function draftOf(item: AssetItem): { from: number; to: number | null } {
  if (item.kind !== "broll") return { from: 0, to: null };
  const existing = usableDraft.value[item.id];
  if (existing !== undefined) return existing;
  return { from: item.usable_from_ms, to: item.usable_to_ms };
}

function onUsableInput(item: AssetItem, edge: "from" | "to", event: Event): void {
  const raw = (event.target as HTMLInputElement).value.trim();
  const parsed = raw === "" ? null : Number(raw);
  const current = draftOf(item);
  usableDraft.value = {
    ...usableDraft.value,
    [item.id]:
      edge === "from"
        ? { from: parsed ?? 0, to: current.to }
        : { from: current.from, to: parsed === null ? null : parsed },
  };
}

async function onSaveUsable(item: AssetItem): Promise<void> {
  const draft = draftOf(item);
  const ok = await assets.applyPatch(item, {
    usable_from_ms: draft.from,
    usable_to_ms: draft.to,
  });
  if (ok) {
    const next = { ...usableDraft.value };
    delete next[item.id];
    usableDraft.value = next;
  }
}

function sectionFor(kind: AssetKind) {
  return assets.sections.find((section) => section.kind === kind) ?? null;
}

function reportSection(kind: AssetKind): IngestSection | null {
  return assets.report?.sections.find((section) => section.kind === kind) ?? null;
}

function scannedOf(kind: AssetKind): ScannedAsset[] {
  return reportSection(kind)?.assets ?? [];
}

function straysOf(kind: AssetKind): string[] {
  return sectionFor(kind)?.strays ?? [];
}

/**
 * 空列表时该说往哪放 —— **分三种原因**，说混了会把"命名写错了"读成"素材没放进去"。
 *
 * ① 盘上已经有了、只是没入库；② 目录里放的东西命名不合规（上面会列出来）；
 * ③ 确实什么都没放。第一种最容易被误读，所以它单独一句话。
 */
function emptyHint(kind: AssetKind): string {
  const section = sectionFor(kind);
  if ((section?.pending.length ?? 0) > 0) {
    return `盘上那 ${section?.pending.length} 条已经在等着了 —— 点上面的「把这一类入库」登记它们（授权、时长、缩略图）。`;
  }
  if ((section?.strays.length ?? 0) > 0) {
    return `目录里有文件，但命名不合规（上面列出来了）。改成 ${KIND_HINTS[kind]} 这个形状再扫一次。`;
  }
  return `把文件放进 ${KIND_HINTS[kind]}，再点「扫描并入库」。`;
}

/** 这一节的灯：目录不在 / 出片挑不到 / 还差一些 ⇒ 黄；否则绿。 */
function sectionTone(kind: AssetKind): StatusTone {
  const section = sectionFor(kind);
  if (section === null) return "warn";
  if (section.root_missing) return "warn";
  if (section.usable === 0) return "warn";
  if (section.shortfall !== null) return "warn";
  return "ok";
}
</script>

<template>
  <PanelCard
    fill
    dense
    title="素材库"
    :subtitle="`${assets.sections.length} 类 · 跑酷 ${usableText(sectionFor('broll'))} · 音色 ${usableText(sectionFor('voice'))}`"
  >
    <template #actions>
      <select class="field mono" :value="assets.license" title="新入库素材的授权类型" @change="onLicenseChange">
        <option v-for="value in assets.licenses" :key="value" :value="value">
          {{ LICENSE_LABELS[value] ?? value }}
        </option>
      </select>
      <label class="toggle" title="只扫盘、不写库（先看看会怎样）">
        <input type="checkbox" :checked="assets.dryRun" @change="onDryRunChange" />
        <span>预览模式</span>
      </label>
      <AppButton :loading="assets.busy" @click="onScan()">
        {{ assets.dryRun ? "扫一遍（不写库）" : "扫描并入库" }}
      </AppButton>
    </template>

    <div class="assets">
      <p v-if="assets.loadError" class="alert alert--error">读素材库失败：{{ assets.loadError }}</p>
      <p v-if="assets.error" class="alert alert--error">{{ assets.error }}</p>
      <p v-if="assets.degradedNote" class="alert alert--warn">
        降级模式：{{ assets.degradedNote }}
      </p>

      <section v-for="kind in ASSET_KINDS" :key="kind" class="block">
        <header class="block__head">
          <h3 class="block__title">
            <StatusDot :tone="sectionTone(kind)" :label="KIND_LABELS[kind]" />
          </h3>
          <span class="mono muted">{{ coverageText(sectionFor(kind)) }}</span>
          <AppButton size="sm" :disabled="assets.busy" @click="onScan(kind)">只扫这一类</AppButton>
        </header>

        <p class="hint mono">目录：{{ sectionFor(kind)?.root ?? KIND_HINTS[kind] }}</p>
        <p class="hint">放这里：{{ KIND_HINTS[kind] }}</p>

        <p v-if="sectionFor(kind)?.root_missing" class="alert alert--warn">
          目录还没建 —— 点「扫描并入库」会把它建出来。
        </p>
        <p v-if="sectionFor(kind)?.shortfall" class="alert alert--warn">
          {{ sectionFor(kind)?.shortfall }}
        </p>

        <div v-if="(sectionFor(kind)?.pending.length ?? 0) > 0" class="alert alert--info">
          <p>盘上有 {{ sectionFor(kind)?.pending.length }} 条还没入库 —— {{ pendingNote(kind) }}。</p>
          <p class="mono pending">{{ pendingText(sectionFor(kind)) }}</p>
          <AppButton size="sm" :disabled="assets.busy" @click="onScan(kind)">
            {{ assets.dryRun ? "扫一遍（不写库）" : "把这一类入库" }}
          </AppButton>
        </div>

        <p v-if="straysOf(kind).length > 0" class="alert alert--warn">
          目录里有 {{ straysOf(kind).length }} 个文件没被认出来（命名不合规，<strong>没有</strong>入库）：
          <span class="mono">{{ straysOf(kind).join("、") }}</span>
        </p>

        <EmptyState
          v-if="(sectionFor(kind)?.items.length ?? 0) === 0"
          :title="
            (sectionFor(kind)?.pending.length ?? 0) > 0
              ? `盘上那 ${sectionFor(kind)?.pending.length} 条还没进库`
              : '这一类还没有素材'
          "
          :hint="emptyHint(kind)"
        />

        <table v-else class="table">
          <thead>
            <tr>
              <th>预览</th>
              <th>id</th>
              <th>时长</th>
              <th>授权</th>
              <th v-if="kind === 'broll'">可用区间</th>
              <th>用量</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in assets.sections.find((s) => s.kind === kind)?.items ?? []" :key="item.id">
              <td class="thumb-cell">
                <img v-if="thumbUrlOf(item)" class="thumb" :src="thumbUrlOf(item) ?? ''" :alt="item.id" />
                <span v-else class="muted">无预览</span>
              </td>
              <td class="mono">
                {{ item.id }}
                <span v-if="isPlaceholder(item)" class="chip chip--warn">占位</span>
              </td>
              <td class="mono">
                {{ formatDuration(item.kind === "voice" ? item.total_duration_ms : item.duration_ms) }}
                <span v-if="item.kind === 'voice'" class="muted">· {{ item.ref_count }} 段</span>
              </td>
              <td>
                <select
                  class="field mono"
                  :value="item.license ?? ''"
                  @change="onItemLicense(item, $event)"
                >
                  <option v-for="value in assets.licenses" :key="value" :value="value">
                    {{ LICENSE_LABELS[value] ?? value }}
                  </option>
                </select>
              </td>
              <td v-if="kind === 'broll'" class="mono">
                <input
                  class="field field--num"
                  type="number"
                  :value="draftOf(item).from"
                  @input="onUsableInput(item, 'from', $event)"
                />
                <input
                  class="field field--num"
                  type="number"
                  :value="draftOf(item).to ?? ''"
                  placeholder="片尾"
                  @input="onUsableInput(item, 'to', $event)"
                />
                <AppButton size="sm" :disabled="assets.pendingId === item.id" @click="onSaveUsable(item)">
                  存
                </AppButton>
              </td>
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
                <audio v-if="mediaUrlOf(item)" class="player" controls preload="none" :src="mediaUrlOf(item) ?? ''" />
              </td>
            </tr>
          </tbody>
        </table>
      </section>

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
        <table v-if="scannedOf('broll').length > 0 || scannedOf('voice').length > 0 || scannedOf('bgm').length > 0" class="table">
          <thead>
            <tr>
              <th>类别</th>
              <th>id</th>
              <th>动作</th>
              <th>体检</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="kind in ASSET_KINDS" :key="`rows-${kind}`">
              <tr v-for="row in scannedOf(kind)" :key="`${kind}-${row.id}`">
                <td class="mono">{{ KIND_LABELS[kind] }}</td>
                <td class="mono">{{ row.id }}</td>
                <td class="mono">
                  <StatusDot :tone="scannedTone(row)" :label="row.action ? (ACTION_LABELS[row.action] ?? row.action) : '未写库'" />
                </td>
                <td>
                  <span v-if="row.check.ok" class="muted">通过</span>
                  <span v-else class="bad">
                    {{ row.check.problems.map((p) => p.message).join("；") }}
                  </span>
                </td>
                <td class="muted">{{ row.note ?? "—" }}</td>
              </tr>
            </template>
          </tbody>
        </table>
        <EmptyState v-else title="这一趟没扫到素材" hint="确认文件放进了对应目录、命名符合约定（见每一节上方的「放这里」）。" />
      </section>
    </div>
  </PanelCard>
</template>

<style scoped>
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
  width: 84px;
  margin-right: var(--space-1);
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
