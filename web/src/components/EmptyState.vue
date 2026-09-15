<script setup lang="ts">
withDefaults(defineProps<{ title: string; hint?: string }>(), { hint: "" });
</script>

<template>
  <div class="empty">
    <p class="empty__title">{{ title }}</p>
    <p v-if="hint" class="empty__hint">{{ hint }}</p>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  align-items: center;
  justify-content: center;
  height: 100%;
  min-height: 160px;
  padding: var(--space-6);
  text-align: center;
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-md);
}

.empty__title {
  color: var(--text-secondary);
  font-size: var(--text-lg);
}

.empty__hint {
  color: var(--text-muted);
  font-size: var(--text-sm);
}
</style>
