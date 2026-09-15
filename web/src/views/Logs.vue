<script setup lang="ts">
// ⑧ 实时日志面板（T4.9 · §04.4.6 / §04.5.2）。
//
// 这一屏要回答三个问题，缺一个都会让人不再信任它：
// ① "现在在发生什么" —— 实时流（WS）；
// ② "刚才那段我没看见" —— 断线按 `since_id` 补齐 + 跳号时 REST 补洞；
// ③ "给我一份拿去分析" —— 导出 NDJSON。

import { ref } from "vue";

import AppButton from "@/components/AppButton.vue";
import LogStream from "@/components/LogStream.vue";
import PanelCard from "@/components/PanelCard.vue";
import { EXPORT_LIMIT, useLogsStore } from "@/stores/logs";
import { useOverviewStore } from "@/stores/overview";
import { DEFAULT_MIN_LEVEL, SEVERITY_ORDER } from "@/ws/events";

const logs = useLogsStore();
const overview = useOverviewStore();

const LEVELS = Object.keys(SEVERITY_ORDER);
const exportNote = ref<string | null>(null);

function onLevelChange(event: Event): void {
  logs.setMinLevel((event.target as HTMLSelectElement).value);
}

function onSourceChange(event: Event): void {
  logs.setSource((event.target as HTMLSelectElement).value);
}

function onTaskChange(event: Event): void {
  logs.setTaskId((event.target as HTMLInputElement).value.trim());
}

function onSearchChange(event: Event): void {
  logs.setSearch((event.target as HTMLInputElement).value);
}

async function onExport(): Promise<void> {
  exportNote.value = null;
  const result = await logs.exportToFile();
  if (!result) return;
  const truncatedHint =
    result.rows >= EXPORT_LIMIT ? "（已达上限，可能被截断：收紧过滤条件再导一次）" : "";
  exportNote.value = `${result.filename} · ${result.rows} 行${truncatedHint}`;
}

async function onLoadEarlier(): Promise<void> {
  const count = await logs.loadEarlier();
  exportNote.value = count > 0 ? `已向前加载 ${count} 行` : "没有更早的日志了";
}
</script>

<template>
  <PanelCard
    fill
    dense
    title="实时日志"
    :subtitle="`WS ${overview.wsStatus} · 缓冲 ${logs.rows.length} · 展示 ${logs.visible.length} · 告警 ${logs.alertRows.length} · 补洞 ${logs.backfillCount} 次 · 缺口 ${logs.gapCount}${logs.lostIds > 0 ? ` · 已丢失 ${logs.lostIds} 条` : ''}`"
  >
    <template #actions>
      <select class="field mono" :value="logs.minLevel" title="级别下限" @change="onLevelChange">
        <option v-for="level in LEVELS" :key="level" :value="level">{{ level }}</option>
      </select>
      <select class="field mono" :value="logs.source" title="来源" @change="onSourceChange">
        <option value="">全部来源</option>
        <option v-for="item in logs.sources" :key="item" :value="item">{{ item }}</option>
      </select>
      <input
        class="field mono"
        :value="logs.taskId"
        placeholder="task_id"
        title="只看某个任务"
        @input="onTaskChange"
      />
      <input
        class="field field--wide"
        :value="logs.search"
        placeholder="搜索 message / source"
        title="字面子串（% 与 _ 不是通配符）"
        @input="onSearchChange"
      />
      <AppButton
        size="sm"
        :variant="logs.onlyAlerts ? 'primary' : 'ghost'"
        @click="logs.toggleOnlyAlerts()"
      >
        只看告警
      </AppButton>
      <AppButton size="sm" :loading="logs.loadingEarlier" @click="onLoadEarlier()">更早</AppButton>
      <AppButton
        size="sm"
        :variant="logs.paused ? 'primary' : 'ghost'"
        @click="logs.togglePause()"
      >
        {{ logs.paused ? `继续（丢了 ${logs.droppedWhilePaused}）` : "暂停" }}
      </AppButton>
      <AppButton size="sm" :loading="logs.exporting" @click="onExport()">导出 NDJSON</AppButton>
      <AppButton size="sm" @click="logs.clear()">清空</AppButton>
    </template>

    <div class="wrap">
      <p v-if="logs.exportError" class="notice notice--error">导出失败：{{ logs.exportError }}</p>
      <p v-else-if="exportNote" class="notice">{{ exportNote }}</p>
      <p v-else-if="logs.truncated" class="notice">
        握手快照被截断过（服务端一次只发最近 500 条）—— 更早的用「更早」或直接导出。
      </p>
      <p v-else-if="logs.backfillInFlight" class="notice">正在用 REST 补齐缺口…</p>
      <p v-else-if="logs.minLevel === DEFAULT_MIN_LEVEL" class="notice notice--muted">
        `debug` 不落库（§04.5.2）⇒ 任何级别过滤都看不到它；过滤全在本地做，切换不重连。
      </p>
      <LogStream :rows="logs.visible" :alert-ids="logs.alertIds" :search="logs.search" />
    </div>
  </PanelCard>
</template>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  height: 100%;
  min-height: 0;
}

.field {
  height: 22px;
  padding: 0 var(--space-2);
  color: var(--text-primary);
  font-size: var(--text-xs);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.field--wide {
  width: 200px;
}

.notice {
  padding: var(--space-1) var(--space-3);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.notice--muted {
  color: var(--text-muted);
}

.notice--error {
  color: var(--error);
  border-color: var(--error);
}
</style>
