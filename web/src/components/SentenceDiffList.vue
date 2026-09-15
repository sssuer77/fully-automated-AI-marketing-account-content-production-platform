<script setup lang="ts">
// 逐句对照（T4.4 · 版本对比）—— 只画 `domain/diff.py` 给出的四类操作。
//
// 为什么逐段渲染而不是 `v-html`：稿件正文是**模型产出**，直接塞进 DOM 等于把
// "模型说什么就是什么"变成一条 XSS 通道（`LogStream.vue` 同一手法）。

import type { SentenceDiffRow } from "@/api/endpoints/scripts";
import { diffOpLabel } from "@/stores/scripts";

defineProps<{ changes: SentenceDiffRow[] }>();

function seqText(seq: number | null | undefined): string {
  return seq === null || seq === undefined ? "-" : `#${seq}`;
}
</script>

<template>
  <ul class="diff">
    <li v-for="(row, index) in changes" :key="index" class="diff__row" :class="`diff__row--${row.op}`">
      <span class="diff__op">{{ diffOpLabel(row.op) }}</span>
      <span class="diff__seq mono">{{ seqText(row.old_seq) }}</span>
      <span class="diff__text diff__text--old">{{ row.old_text ?? "" }}</span>
      <span class="diff__arrow">→</span>
      <span class="diff__seq mono">{{ seqText(row.new_seq) }}</span>
      <span class="diff__text diff__text--new">{{ row.new_text ?? "" }}</span>
    </li>
  </ul>
</template>

<style scoped>
.diff {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.diff__row {
  display: grid;
  grid-template-columns: 20px 34px 1fr 16px 34px 1fr;
  gap: var(--space-2);
  align-items: baseline;
  padding: var(--space-1) var(--space-2);
  background: var(--bg-raised);
  border-radius: var(--radius-sm);
}

.diff__op {
  color: var(--text-muted);
  font-size: var(--text-xs);
  text-align: center;
}

.diff__seq {
  color: var(--text-muted);
  font-size: var(--text-xs);
  text-align: right;
}

.diff__text {
  word-break: break-word;
}

.diff__text--old {
  color: var(--text-muted);
}

.diff__text--new {
  color: var(--text-primary);
}

.diff__arrow {
  color: var(--text-muted);
  text-align: center;
}

.diff__row--replace .diff__op {
  color: var(--warn);
}

.diff__row--insert .diff__op {
  color: var(--ok);
}

.diff__row--delete .diff__op {
  color: var(--error);
}

.diff__row--delete .diff__text--new {
  color: var(--text-muted);
}
</style>
