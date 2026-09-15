<script setup lang="ts">
import { nextTick, ref, watch } from "vue";

import { splitHighlight } from "@/utils/highlight";
import type { SystemLogRow } from "@/ws/events";

const props = withDefaults(
  defineProps<{
    rows: readonly SystemLogRow[];
    alertIds?: ReadonlySet<number>;
    search?: string;
    autoscroll?: boolean;
    height?: string;
  }>(),
  { alertIds: () => new Set<number>(), search: "", autoscroll: true, height: "100%" },
);

const viewport = ref<HTMLElement | null>(null);

// 只在**行数变化**时贴底：用 `rows` 整体做依赖会在同一批里触发多次滚动，
// 长日志流下表现为"滚动条抖动"。
watch(
  () => props.rows.length,
  async () => {
    if (!props.autoscroll) return;
    await nextTick();
    const element = viewport.value;
    if (element) element.scrollTop = element.scrollHeight;
  },
);

function clock(ts: string): string {
  return ts.length >= 19 ? ts.slice(11, 19) : ts;
}

function alertCode(row: SystemLogRow): string | null {
  const code = row.payload["code"];
  return typeof code === "string" ? code : null;
}
</script>

<template>
  <div ref="viewport" class="stream" :style="{ height }">
    <p v-if="rows.length === 0" class="stream__empty">暂无日志</p>
    <div
      v-for="row in rows"
      :key="row.id"
      class="line"
      :class="[`line--${row.level}`, { 'line--alert': alertIds.has(row.id) }]"
    >
      <span class="line__ts mono">{{ clock(row.ts) }}</span>
      <span class="line__level mono">{{ row.level }}</span>
      <span class="line__source mono">{{ row.source }}</span>
      <span class="line__message">
        <span v-if="alertIds.has(row.id)" class="line__badge mono">{{ alertCode(row) ?? 'ALERT' }}</span>
        <!-- 逐段渲染而不是拼 HTML：`message` 是不可信文本（用户输入 / 模型输出 / stderr） -->
        <template v-for="(part, index) in splitHighlight(row.message, search)" :key="index">
          <mark v-if="part.hit" class="line__hit">{{ part.text }}</mark>
          <template v-else>{{ part.text }}</template>
        </template>
      </span>
      <span v-if="row.duration_ms !== null" class="line__dur mono">{{ row.duration_ms }}ms</span>
    </div>
  </div>
</template>

<style scoped>
.stream {
  overflow-y: auto;
  font-size: var(--text-sm);
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.stream__empty {
  padding: var(--space-4);
  color: var(--text-muted);
  text-align: center;
}

.line {
  display: grid;
  grid-template-columns: 62px 46px 108px 1fr auto;
  gap: var(--space-3);
  align-items: baseline;
  padding: 2px var(--space-3);
  border-bottom: 1px solid transparent;
}

.line:hover {
  background: var(--bg-hover);
}

.line--alert {
  background: rgba(242, 99, 122, 0.1);
  box-shadow: inset 3px 0 0 var(--error);
}

.line__ts {
  color: var(--text-muted);
}

.line__level {
  color: var(--text-secondary);
  text-transform: uppercase;
}

.line--warn .line__level {
  color: var(--warn);
}

.line--error .line__level {
  color: var(--error);
}

.line--fatal .line__level {
  color: var(--fatal);
}

.line--warn {
  background: rgba(229, 181, 103, 0.06);
}

.line--error,
.line--fatal {
  background: rgba(242, 99, 122, 0.08);
}

.line__source {
  overflow: hidden;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.line__message {
  min-width: 0;
  color: var(--text-primary);
  word-break: break-word;
}

.line__badge {
  margin-right: var(--space-2);
  padding: 0 var(--space-1);
  color: var(--text-inverse);
  font-size: var(--text-xs);
  background: var(--error);
  border-radius: var(--radius-sm);
}

.line__hit {
  color: var(--text-inverse);
  background: var(--accent);
}

.line__dur {
  color: var(--text-muted);
}
</style>
