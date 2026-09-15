<script setup lang="ts">
import { computed } from "vue";

type ButtonVariant = "primary" | "ghost" | "danger";
type ButtonSize = "sm" | "md";

const props = withDefaults(
  defineProps<{
    variant?: ButtonVariant;
    size?: ButtonSize;
    disabled?: boolean;
    loading?: boolean;
    nativeType?: "button" | "submit";
  }>(),
  { variant: "ghost", size: "md", disabled: false, loading: false, nativeType: "button" },
);

const emit = defineEmits<{ click: [event: MouseEvent] }>();

const classes = computed(() => ["btn", `btn--${props.variant}`, `btn--${props.size}`]);
const blocked = computed(() => props.disabled || props.loading);

function onClick(event: MouseEvent): void {
  if (blocked.value) return;
  emit("click", event);
}
</script>

<template>
  <button :type="nativeType" :class="classes" :disabled="blocked" @click="onClick">
    <span v-if="loading" class="btn__spinner" aria-hidden="true" />
    <slot />
  </button>
</template>

<style scoped>
.btn {
  display: inline-flex;
  gap: var(--space-2);
  align-items: center;
  justify-content: center;
  height: 28px;
  padding: 0 var(--space-3);
  color: var(--text-primary);
  font-size: var(--text-sm);
  white-space: nowrap;
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}

.btn:hover:not(:disabled) {
  background: var(--bg-hover);
  border-color: var(--border-strong);
}

.btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.btn--sm {
  height: 22px;
  font-size: var(--text-xs);
}

.btn--primary {
  background: var(--accent-soft);
  border-color: var(--accent);
}

.btn--primary:hover:not(:disabled) {
  color: var(--text-inverse);
  background: var(--accent);
}

.btn--danger {
  color: var(--error);
  border-color: var(--error);
}

.btn--danger:hover:not(:disabled) {
  color: var(--text-inverse);
  background: var(--error);
}

.btn__spinner {
  width: 10px;
  height: 10px;
  border: 2px solid currentcolor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: btn-spin 0.7s linear infinite;
}

@keyframes btn-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
