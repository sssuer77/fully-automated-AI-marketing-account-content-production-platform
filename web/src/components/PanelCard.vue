<script setup lang="ts">
withDefaults(
  defineProps<{ title: string; subtitle?: string; fill?: boolean; dense?: boolean }>(),
  { subtitle: "", fill: false, dense: false },
);
</script>

<template>
  <section class="panel" :class="{ 'panel--fill': fill, 'panel--dense': dense }">
    <header class="panel__head">
      <div class="panel__titles">
        <h2 class="panel__title">{{ title }}</h2>
        <p v-if="subtitle" class="panel__sub">{{ subtitle }}</p>
      </div>
      <div class="panel__actions">
        <slot name="actions" />
      </div>
    </header>
    <div class="panel__body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-panel);
}

.panel--fill {
  height: 100%;
  min-height: 0;
}

.panel__head {
  display: flex;
  gap: var(--space-3);
  align-items: flex-start;
  justify-content: space-between;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--border-subtle);
}

.panel--dense .panel__head {
  padding: var(--space-2) var(--space-3);
}

.panel__title {
  font-size: var(--text-lg);
  font-weight: 600;
}

.panel__sub {
  margin-top: var(--space-1);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.panel__actions {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  flex-shrink: 0;
}

.panel__body {
  flex: 1;
  min-height: 0;
  padding: var(--space-4);
}

.panel--dense .panel__body {
  padding: var(--space-3);
}
</style>
