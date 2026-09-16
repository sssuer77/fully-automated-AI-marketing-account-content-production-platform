<script setup lang="ts">
import { computed, onMounted, type Component } from "vue";

import EmptyState from "@/components/EmptyState.vue";
import StatusDot from "@/components/StatusDot.vue";
import { useLogsStore } from "@/stores/logs";
import { useOverviewStore } from "@/stores/overview";
import { PANELS, useUiStore, type PanelId } from "@/stores/ui";
import Assets from "@/views/Assets.vue";
import Audit from "@/views/Audit.vue";
import Logs from "@/views/Logs.vue";
import Metrics from "@/views/Metrics.vue";
import Outputs from "@/views/Outputs.vue";
import Overview from "@/views/Overview.vue";
import Personas from "@/views/Personas.vue";
import Pools from "@/views/Pools.vue";
import Renders from "@/views/Renders.vue";
import Scripts from "@/views/Scripts.vue";
import Topics from "@/views/Topics.vue";
import Voices from "@/views/Voices.vue";

const ui = useUiStore();
const logs = useLogsStore();
const overview = useOverviewStore();

/** 每交付一块面板就加一行；其余面板点进去仍是"待 T4.x"的占位。 */
const VIEWS: Partial<Record<PanelId, Component>> = {
  overview: Overview,
  topics: Topics,
  scripts: Scripts,
  voices: Voices,
  logs: Logs,
  templates: Outputs,
  renders: Renders,
  assets: Assets,
  pools: Pools,
  metrics: Metrics,
  audit: Audit,
  personas: Personas,
};

const activeView = computed(() => VIEWS[ui.activePanel] ?? null);
const activeDef = computed(() => PANELS.find((panel) => panel.id === ui.activePanel) ?? PANELS[0]);

// 挂载即接线：日志缓冲与健康轮询**与当前面板无关**地一直在攒，
// 否则"切到日志面板才开始收日志"会让断线补齐这件事没法验证。
onMounted(() => {
  overview.start();
  logs.start();
});
</script>

<template>
  <div class="shell">
    <aside class="rail">
      <div class="rail__brand">
        <span class="rail__title">制片台</span>
        <span class="rail__sub">本地部署 · 全自动</span>
      </div>
      <nav class="rail__nav">
        <button
          v-for="panel in PANELS"
          :key="panel.id"
          type="button"
          class="rail__item"
          :class="{ 'rail__item--active': panel.id === ui.activePanel }"
          @click="ui.selectPanel(panel.id)"
        >
          <span class="rail__label">{{ panel.label }}</span>
          <span v-if="!panel.ready" class="rail__task mono">{{ panel.task }}</span>
        </button>
      </nav>
    </aside>

    <header class="topbar">
      <h1 class="topbar__title">{{ activeDef.label }}</h1>
      <span class="topbar__task mono">{{ activeDef.task }}</span>
    </header>

    <main class="content">
      <component :is="activeView" v-if="activeView" />
      <EmptyState
        v-else
        :title="`${activeDef.label} 面板尚未施工`"
        :hint="`计划在 ${activeDef.task} 交付；当前进度见 todolist.md。`"
      />
    </main>

    <footer class="statusbar">
      <StatusDot
        :tone="overview.healthTone"
        :label="
          overview.healthError ? 'API 异常' : overview.health ? 'API 就绪' : 'API 未知'
        "
      />
      <StatusDot
        :tone="overview.wsTone"
        :label="`WS ${overview.wsStatus}`"
        :pulse="overview.wsStatus === 'connecting' || overview.wsStatus === 'reconnecting'"
      />
      <span class="statusbar__item mono">seq {{ overview.wsCursor.seq }}</span>
      <span class="statusbar__item mono">since_id {{ overview.wsCursor.sinceId ?? "-" }}</span>
      <span class="statusbar__item mono">日志 {{ logs.rows.length }}</span>
      <span class="statusbar__spacer" />
      <span class="statusbar__item mono">spec {{ overview.health?.spec_version ?? "-" }}</span>
    </footer>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template:
    "rail topbar" 44px
    "rail content" 1fr
    "rail statusbar" var(--statusbar-height) / var(--rail-width) 1fr;
  height: 100%;
  min-height: 0;
}

.rail {
  display: flex;
  flex-direction: column;
  grid-area: rail;
  gap: var(--space-3);
  padding: var(--space-3);
  background: var(--bg-panel);
  border-right: 1px solid var(--border-subtle);
}

.rail__brand {
  display: flex;
  flex-direction: column;
  padding: var(--space-2);
}

.rail__title {
  font-size: var(--text-lg);
  font-weight: 600;
}

.rail__sub {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.rail__nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
}

.rail__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-2) var(--space-3);
  color: var(--text-secondary);
  font-size: var(--text-md);
  text-align: left;
  border-radius: var(--radius-sm);
}

.rail__item:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.rail__item--active {
  color: var(--text-primary);
  background: var(--accent-soft);
  box-shadow: inset 2px 0 0 var(--accent);
}

.rail__task {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.topbar {
  display: flex;
  grid-area: topbar;
  gap: var(--space-3);
  align-items: center;
  padding: 0 var(--space-4);
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border-subtle);
}

.topbar__title {
  font-size: var(--text-xl);
  font-weight: 600;
}

.topbar__task {
  color: var(--text-muted);
}

.content {
  grid-area: content;
  min-height: 0;
  padding: var(--space-4);
  overflow: auto;
}

.statusbar {
  display: flex;
  grid-area: statusbar;
  gap: var(--space-4);
  align-items: center;
  padding: 0 var(--space-4);
  color: var(--text-secondary);
  background: var(--bg-panel);
  border-top: 1px solid var(--border-subtle);
}

.statusbar__item {
  color: var(--text-muted);
}

.statusbar__spacer {
  flex: 1;
}
</style>
