<script setup lang="ts">
// 评分徽章（T4.4）—— 确认闸上"这稿子值不值得放行"的第一眼判据。
//
// 色调来自 `gradeTone`（A 绿 / B 黄 / C 红 / 未知灰），**不在组件里再抄一遍映射**：
// 评级的语义只有一处真相，否则"列表显示绿色、详情显示黄色"这种事迟早发生。

import { computed } from "vue";

import { gradeTone } from "@/stores/scripts";

const props = withDefaults(
  defineProps<{ grade?: string | null; score?: number | null; round?: number }>(),
  { grade: null, score: null, round: 0 },
);

const tone = computed(() => gradeTone(props.grade));
const grade = computed(() => props.grade ?? "-");
const score = computed(() => (props.score === null || props.score === undefined ? "-" : props.score.toFixed(1)));
</script>

<template>
  <span
    class="badge"
    :class="`badge--${tone}`"
    :title="`评级 ${grade} · 总分 ${score} · 第 ${round} 轮`"
  >
    <span class="badge__grade">{{ grade }}</span>
    <span class="badge__score mono">{{ score }}</span>
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  gap: var(--space-1);
  align-items: baseline;
  padding: 0 var(--space-2);
  color: var(--text-muted);
  font-size: var(--text-xs);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.badge__grade {
  font-weight: 600;
}

.badge__score {
  color: var(--text-secondary);
}

.badge--ok {
  color: var(--ok);
  border-color: var(--ok);
}

.badge--warn {
  color: var(--warn);
  border-color: var(--warn);
}

.badge--error {
  color: var(--error);
  border-color: var(--error);
}
</style>
