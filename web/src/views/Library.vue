<script setup lang="ts">
// 成片库面板（T5.11 · §06.11）。
//
// 这一屏只回答三个问题，多一个都不放：
// ① 我手里有哪些成片？—— 盘上 `data/output/videos/` 的片子，**一条任务一行**；
// ② 它们各自发到哪儿了？—— 每行的发布记录（与发布面板**同一份**模型）；
// ③ 我要把哪几条一次发出去？—— 多选 + 选平台 + 批量投递 + 逐条结果。
//
// 三件必须写在面板上的事
// ----------------------
// 1. **一行是一条任务，不是一个文件**：发布池认任务号，它自己去找成片。一条任务重出过
//    三版就是三支片子，但发出去的是**同一支**（发布池挑的那一支）。面板按文件列的话，
//    勾第二行与勾第三行的结果一模一样 —— 那是一句谎话，所以这里把 `versions` 当信息摆着，
//    不当选择。不写这一句，用户会以为"我选了新那版"。
// 2. **投递 ≠ 已发布**：这一屏点下去只是把作业排进池子，真正发是 publish 池干的。
//    结果行说的是"已排入 N 条作业"，并把人指向发布面板。
// 3. **发布开关关着的时候点了会怎样**：真平台的投递**照样成功**，作业会在 worker 那一侧
//    带 `PUBLISH_DISABLED` 进死信（在「四池调度」里看）。这一句必须说出来 ——
//    否则"点了没反应"会被当成 bug。平台那一列自带这句话（判据来自服务端）。
//
// 为什么预览要留在这屏
// --------------------
// "这条片子的标题对不对、画面是不是那支"是投出去**之前**唯一能查的事。
// 让人为了看一眼跑去渲染面板翻列表，等于把一次核对拆成两屏。

import { computed, onMounted, ref } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import type { LibraryItem } from "@/api/endpoints/library";
import { renderVideoUrl } from "@/api/endpoints/render";
import { formatBytes } from "@/stores/metrics";
import {
  BATCH_MAX,
  blockedReason,
  canPublish,
  publicationTone,
  publishedCount,
  publishedText,
  resultText,
  useLibraryStore,
} from "@/stores/library";
import { formatDurationMs } from "@/stores/render";
import { useUiStore } from "@/stores/ui";

const library = useLibraryStore();
const ui = useUiStore();

/** 正在预览的那一条（`null` = 没在预览）。 */
const previewing = ref<LibraryItem | null>(null);

onMounted(() => {
  void library.load();
});

const subtitle = computed(() => {
  if (library.snapshot === null) return "读取中…";
  const total = library.snapshot.total;
  const shown = library.visible.length;
  const filtered = library.onlyUnpublished ? `（筛掉已发布，全库 ${total} 条）` : "";
  return `${shown} 条成片${filtered} · 一行 = 一条任务`;
});

/** 版本那一列：只有重出过多版的才值得写出来。 */
function versionsText(item: LibraryItem): string {
  return item.versions > 1 ? `${item.versions} 版（发出去的是最新那版）` : "1 版";
}

function durationText(item: LibraryItem): string {
  return item.duration_ms === null ? "-" : formatDurationMs(item.duration_ms);
}

function onAll(event: Event): void {
  library.toggleAll((event.target as HTMLInputElement).checked);
}

function onOne(taskId: string, event: Event): void {
  library.toggle(taskId, (event.target as HTMLInputElement).checked);
}

function onOnlyUnpublished(event: Event): void {
  library.onlyUnpublished = (event.target as HTMLInputElement).checked;
}

function onPlatform(code: string, event: Event): void {
  library.togglePlatform(code, (event.target as HTMLInputElement).checked);
}

function onPreview(item: LibraryItem): void {
  previewing.value = previewing.value?.task_id === item.task_id ? null : item;
}
</script>

<template>
  <div class="library">
    <PanelCard title="成片库" :subtitle="subtitle">
      <template #actions>
        <AppButton size="sm" :loading="library.loading" @click="library.load()">刷新</AppButton>
        <AppButton size="sm" title="去发布面板看这些作业发到哪一步了" @click="ui.selectPanel('publish')">
          去发布 →
        </AppButton>
      </template>

      <p v-if="library.error" class="alert alert--error">{{ library.error }}</p>

      <EmptyState
        v-if="library.items.length === 0"
        title="还没有成片"
        :hint="library.snapshot?.hint ?? '先去「一键出片」或「渲染」那一屏出一条，成片会落到 data/output/videos/。'"
      />

      <template v-else>
        <div class="toolbar">
          <label class="check">
            <input type="checkbox" :checked="library.allSelected" @change="onAll" />
            <span>全选（{{ library.selectableIds.length }} 条可发）</span>
          </label>
          <label class="check">
            <input type="checkbox" :checked="library.onlyUnpublished" @change="onOnlyUnpublished" />
            <span>只看未发布</span>
          </label>
          <span class="toolbar__count">已勾 {{ library.selected.length }} / {{ BATCH_MAX }}</span>
        </div>

        <p v-if="library.overLimit" class="alert alert--warn">
          一次最多投 {{ BATCH_MAX }} 条（现在勾了 {{ library.selected.length }} 条）——
          分成几批，或者用「发布」面板里的定时计划那一路。
        </p>

        <table class="table">
          <thead>
            <tr>
              <th class="cell--pick"></th>
              <th>任务号</th>
              <th>标题</th>
              <th>成片</th>
              <th>时长 / 体积</th>
              <th>版本</th>
              <th>发到哪儿了</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in library.visible" :key="item.task_id">
              <td class="cell--pick">
                <input
                  type="checkbox"
                  :checked="library.selected.includes(item.task_id)"
                  :disabled="!canPublish(item)"
                  :title="blockedReason(item) ?? `勾中 ${item.task_id}`"
                  @change="onOne(item.task_id, $event)"
                />
              </td>
              <td class="mono cell--task">{{ item.task_id }}</td>
              <td class="cell--title">{{ item.title || "（库里没有这条任务）" }}</td>
              <td class="mono cell--video" :title="item.video_path">{{ item.video_name }}</td>
              <td class="mono">
                {{ durationText(item) }} · {{ formatBytes(item.size_bytes) }}
              </td>
              <td class="cell--versions">{{ versionsText(item) }}</td>
              <td>
                <StatusDot
                  v-if="item.publications.length === 0"
                  tone="idle"
                  label="未发布"
                />
                <span v-else class="pubs">
                  <StatusDot
                    v-for="row in item.publications"
                    :key="row.id"
                    :tone="publicationTone(row.status)"
                    :label="`${row.platform}/${row.account_id}·${row.status}`"
                  />
                </span>
                <span v-if="item.publications.length > 0" class="pubs__text">
                  已发 {{ publishedCount(item) }} / {{ item.publications.length }}
                </span>
              </td>
              <td class="cell--actions">
                <AppButton size="sm" @click="onPreview(item)">
                  {{ previewing?.task_id === item.task_id ? "收起" : "预览" }}
                </AppButton>
              </td>
            </tr>
          </tbody>
        </table>

        <p v-if="!library.onlyUnpublished && publishedText(library.items[0])" class="hint">
          「发到哪儿了」那一列与发布面板是**同一份**记录：发布中 / 失败 / 待人工的处置
          都在发布面板上做（这一屏只管投递）。
        </p>

        <div v-if="previewing" class="player">
          <div class="player__head">
            <span class="player__title">{{ previewing.title || previewing.task_id }}</span>
            <a class="player__link mono" :href="renderVideoUrl(previewing.video_name)" download>
              下载这一支
            </a>
          </div>
          <video
            class="player__video"
            controls
            preload="metadata"
            :src="renderVideoUrl(previewing.video_name)"
          />
          <p class="hint">
            预览的是这条任务**现在**的那支成片（{{ previewing.video_name }}）。
            发布时用的就是它 —— 发布池按任务号自己找成片，与你在这一屏点开的是同一支。
          </p>
        </div>
      </template>
    </PanelCard>

    <PanelCard title="批量发布" :subtitle="library.submitHint">
      <template #actions>
        <AppButton
          variant="primary"
          :disabled="!library.canSubmit"
          :loading="library.publishing"
          @click="library.submit()"
        >
          批量发布（{{ library.selected.length }} 条）
        </AppButton>
      </template>

      <p v-if="library.error" class="alert alert--error">{{ library.error }}</p>
      <p v-if="library.notice" class="alert alert--ok">{{ library.notice }}</p>
      <p v-if="library.publishWarning" class="alert alert--warn">{{ library.publishWarning }}</p>

      <div v-if="library.platformOptions.length === 0" class="hint">
        现在一个**能选的**平台都没有 —— 去「发布」面板或 `config/publish.yaml` 里
        给某个平台配一个启用账号。
      </div>

      <div v-else class="platforms">
        <label v-for="option in library.platformOptions" :key="option.code" class="platform">
          <input
            type="checkbox"
            :checked="library.chosenPlatforms.includes(option.code)"
            @change="onPlatform(option.code, $event)"
          />
          <span class="platform__name">
            {{ option.code }}<span v-if="option.rehearsal" class="platform__tag">演练台</span>
          </span>
          <span class="platform__note">{{ option.note }}</span>
        </label>
      </div>

      <p class="hint">
        点「批量发布」只是把这些任务**排进发布池**（立刻返回）—— 真正发出去的是 publish
        池的 worker。它发完 / 发失败了，在「发布」那一屏上看。
        **发布开关关着**（`config/publish.yaml → enabled: false`）时，真平台的投递照样成功，
        但作业会带 `PUBLISH_DISABLED` 进死信（在「四池调度」里看）——
        想先把链路走通，就选**演练台**（它发到本机靶页，不碰任何真平台）。
      </p>

      <div v-if="library.selected.length > 0" class="results">
        <div v-for="item in library.selectedItems" :key="item.task_id" class="result">
          <span class="mono result__task">{{ item.task_id }}</span>
          <span v-if="library.results[item.task_id]" class="result__text">
            {{ resultText(library.results[item.task_id]) }}
          </span>
          <span v-else class="result__text result__text--idle">还没投</span>
        </div>
      </div>
    </PanelCard>
  </div>
</template>

<style scoped>
.library {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.alert {
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-2);
  font-size: var(--text-sm);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.alert--error {
  color: var(--error);
  border-color: var(--error);
}

.alert--warn {
  color: var(--warn);
  border-color: var(--warn);
}

.alert--ok {
  color: var(--ok);
  border-color: var(--ok);
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: center;
  margin-bottom: var(--space-2);
}

.toolbar__count {
  margin-left: auto;
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.check {
  display: inline-flex;
  gap: var(--space-2);
  align-items: center;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.table {
  width: 100%;
  font-size: var(--text-sm);
  border-collapse: collapse;
}

.table th {
  padding: var(--space-1) var(--space-2);
  color: var(--text-muted);
  font-weight: 400;
  font-size: var(--text-xs);
  text-align: left;
  border-bottom: 1px solid var(--border-subtle);
}

.table td {
  padding: var(--space-1) var(--space-2);
  color: var(--text-secondary);
  vertical-align: top;
  border-bottom: 1px solid var(--border-subtle);
}

.cell--pick {
  width: 28px;
}

.cell--task {
  white-space: nowrap;
}

.cell--title {
  min-width: 180px;
  color: var(--text-primary);
}

.cell--video {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell--versions {
  white-space: nowrap;
}

.cell--actions {
  white-space: nowrap;
}

.pubs {
  display: inline-flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.pubs__text {
  margin-left: var(--space-2);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.hint {
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.player {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-3);
}

.player__head {
  display: flex;
  gap: var(--space-3);
  align-items: baseline;
  justify-content: space-between;
}

.player__title {
  color: var(--text-primary);
  font-size: var(--text-sm);
}

.player__link {
  font-size: var(--text-xs);
}

.player__video {
  width: 100%;
  max-height: 420px;
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.platforms {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.platform {
  display: grid;
  grid-template-columns: auto 160px 1fr;
  gap: var(--space-2);
  align-items: baseline;
  font-size: var(--text-xs);
}

.platform__name {
  color: var(--text-primary);
}

.platform__tag {
  padding: 0 var(--space-1);
  margin-left: var(--space-1);
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.platform__note {
  color: var(--text-muted);
}

.results {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-3);
}

.result {
  display: flex;
  gap: var(--space-3);
  align-items: baseline;
  font-size: var(--text-xs);
}

.result__task {
  flex: 0 0 220px;
  color: var(--text-muted);
}

.result__text {
  min-width: 0;
  color: var(--text-secondary);
}

.result__text--idle {
  color: var(--text-muted);
}
</style>
