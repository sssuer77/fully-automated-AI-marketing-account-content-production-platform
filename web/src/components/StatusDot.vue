<script setup lang="ts">
import type { StatusTone } from "./tone";

withDefaults(defineProps<{ tone: StatusTone; label?: string; pulse?: boolean }>(), {
  label: "",
  pulse: false,
});
</script>

<template>
  <span class="dot-wrap">
    <span class="dot" :class="[`dot--${tone}`, { 'dot--pulse': pulse }]" aria-hidden="true" />
    <span v-if="label" class="dot__label">{{ label }}</span>
  </span>
</template>

<style scoped>
.dot-wrap {
  display: inline-flex;
  gap: var(--space-2);
  align-items: center;
  font-size: var(--text-sm);
}

.dot {
  width: 8px;
  height: 8px;
  background: var(--debug);
  border-radius: 50%;
}

.dot--ok {
  background: var(--ok);
}

.dot--warn {
  background: var(--warn);
}

.dot--error {
  background: var(--error);
}

.dot--busy {
  background: var(--info);
}

.dot--idle {
  background: var(--debug);
}

.dot--pulse {
  animation: dot-pulse 1.4s ease-in-out infinite;
}

@keyframes dot-pulse {
  50% {
    opacity: 0.35;
  }
}
</style>
