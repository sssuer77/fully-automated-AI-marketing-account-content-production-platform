<script setup lang="ts">
// 审计留痕（T4.12 · §04.5.11 / §03.3.8）。
//
// 这一屏是**查账**：谁在什么时候动了什么、改动前后各是什么、有没有被拦下。
//
// 它只有读。写入口散在确认闸 / 池启停 / 策略切换 / 人物库各处（§04.4.4 不变量 2：
// 改动了别人能看到的东西就要留痕）—— 在这里再开一个写入口，等于给"伪造留痕"开条路。
//
// 三个容易踩的点，面板上直接写清楚
// -------------------------------
// ① **筛选不自动查**：文本框改值只改状态，回车或点「筛选」才发请求 —— 每一次都是一次
//    数据库查询，逐字触发等于白跑几十次（下拉是选择，选完即查）；
// ② **`denied` 不是失败**：它是"有人想动、被规则拦下了"，同样是有效留痕，标黄不标红；
// ③ **空列表有两种原因**：没筛到 / 什么都没发生过 —— 两种提示必须分开说，
//    否则"筛错了"会被读成"没人动过"。

import { onMounted } from "vue";

import type { AuditOp } from "@/api/endpoints/audit";
import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import {
  AUDIT_FILTERS,
  FILTER_LABELS,
  diffSummary,
  resultTone,
  useAuditStore,
  type AuditFilterName,
} from "@/stores/audit";

const audit = useAuditStore();

onMounted(() => {
  audit.start();
});

/** 下拉改完立刻查一次（选择本身就是一次明确的意图）。 */
function onSelect(name: AuditFilterName, event: Event): void {
  audit.setFilter(name, (event.target as HTMLSelectElement).value);
  audit.apply();
}

function onInput(name: AuditFilterName, event: Event): void {
  audit.setFilter(name, (event.target as HTMLInputElement).value);
}

/** `before` / `after` 原样摆出来：留痕的意义就是这两块，折叠起来省地方但不省内容。 */
function pretty(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2);
}

function rowKey(row: AuditOp): string {
  return String(row.id);
}
</script>

<template>
  <div class="audit">
    <p v-if="audit.loadError" class="alert alert--error">
      留痕拉取失败：{{ audit.loadError }}（下面显示的是上一次拿到的那些）
    </p>
    <p v-if="audit.facetsError" class="alert alert--warn">
      筛选项读不到：{{ audit.facetsError }}（下拉是空的，但文本框照样能用）
    </p>

    <PanelCard
      title="审计留痕"
      :subtitle="`${audit.summary} · ${audit.windowLabel} · 第 ${audit.pageIndex}/${audit.pageCount} 页`"
    >
      <template #actions>
        <AppButton size="sm" :loading="audit.loading" @click="audit.refresh()">刷新</AppButton>
        <AppButton size="sm" :disabled="!audit.hasPrev" @click="audit.goPrev()">上一页</AppButton>
        <AppButton size="sm" :disabled="!audit.hasNext" @click="audit.goNext()">下一页</AppButton>
      </template>

      <div class="filters">
        <input
          class="field mono"
          :value="audit.filters.task_id"
          placeholder="task_id"
          title="只看某个任务的操作史"
          @input="onInput('task_id', $event)"
          @keyup.enter="audit.apply()"
        />
        <select
          class="field mono"
          :value="audit.filters.actor"
          title="操作人"
          @change="onSelect('actor', $event)"
        >
          <option value="">全部操作人</option>
          <option v-for="item in audit.actors" :key="item" :value="item">{{ item }}</option>
        </select>
        <select
          class="field mono"
          :value="audit.filters.action"
          title="动作"
          @change="onSelect('action', $event)"
        >
          <option value="">全部动作</option>
          <option v-for="item in audit.actions" :key="item" :value="item">{{ item }}</option>
        </select>
        <select
          class="field mono"
          :value="audit.filters.target_type"
          title="对象类型"
          @change="onSelect('target_type', $event)"
        >
          <option value="">全部对象</option>
          <option v-for="item in audit.targetTypes" :key="item" :value="item">{{ item }}</option>
        </select>
        <select
          class="field mono"
          :value="audit.filters.result"
          title="结果"
          @change="onSelect('result', $event)"
        >
          <option value="">全部结果</option>
          <option v-for="item in audit.results" :key="item" :value="item">{{ item }}</option>
        </select>
        <input
          class="field field--wide mono"
          :value="audit.filters.since"
          placeholder="起始时间（2026-09-01T00:00:00Z）"
          title="ISO 时间下界（含）"
          @input="onInput('since', $event)"
          @keyup.enter="audit.apply()"
        />
        <AppButton size="sm" variant="primary" :loading="audit.loading" @click="audit.apply()">
          筛选
        </AppButton>
        <AppButton size="sm" :disabled="audit.filterCount === 0" @click="audit.clearFilters()">
          清空
        </AppButton>
      </div>

      <EmptyState
        v-if="audit.rows.length === 0 && !audit.loading"
        title="没有留痕可看"
        :hint="audit.emptyHint"
      />

      <table v-else class="table">
        <thead>
          <tr>
            <th>时间</th>
            <th>操作人</th>
            <th>动作</th>
            <th>对象</th>
            <th>结果</th>
            <th>变更</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in audit.rows" :key="rowKey(row)">
            <td class="mono">{{ row.at ?? '-' }}</td>
            <td class="mono">
              {{ row.actor }}
              <span v-if="row.actor_ref" class="muted">/{{ row.actor_ref }}</span>
            </td>
            <td class="mono">{{ row.action }}</td>
            <td class="mono">
              {{ row.target_type }}
              <span v-if="row.target_id" class="muted">#{{ row.target_id }}</span>
            </td>
            <td>
              <span class="badge" :class="`badge--${resultTone(row.result)}`">{{ row.result }}</span>
            </td>
            <td class="diff">
              <span>{{ diffSummary(row) }}</span>
              <span v-if="row.reason" class="reason">{{ row.reason }}</span>
              <details>
                <summary>改动前后</summary>
                <div class="diff__body">
                  <div>
                    <span class="muted">before</span>
                    <pre class="mono">{{ pretty(row.before) }}</pre>
                  </div>
                  <div>
                    <span class="muted">after</span>
                    <pre class="mono">{{ pretty(row.after) }}</pre>
                  </div>
                </div>
              </details>
            </td>
          </tr>
        </tbody>
      </table>

      <p class="hint">
        共 {{ audit.total }} 条（同一组筛选下的总数）· 单页上限 500 · 筛选字段：
        {{ AUDIT_FILTERS.map((name) => FILTER_LABELS[name]).join(' / ') }}
      </p>
    </PanelCard>
  </div>
</template>

<style scoped>
.audit {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  align-content: start;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  margin-bottom: var(--space-3);
}

.field {
  height: 24px;
  padding: 0 var(--space-2);
  color: var(--text-primary);
  font-size: var(--text-xs);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.field--wide {
  width: 220px;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.table th {
  color: var(--text-muted);
  font-weight: 500;
  text-align: left;
}

.table th,
.table td {
  padding: var(--space-1) var(--space-2);
  vertical-align: top;
  border-bottom: 1px solid var(--border-subtle);
}

.badge {
  padding: 0 var(--space-1);
  font-size: var(--text-xs);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
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

.diff {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.diff__body {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.diff__body pre {
  max-height: 200px;
  margin: 0;
  padding: var(--space-2);
  overflow: auto;
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.reason {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.muted {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.hint {
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.alert {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  border-radius: var(--radius-sm);
}

.alert--error {
  color: var(--error);
  background: rgba(242, 99, 122, 0.08);
  border: 1px solid var(--error);
}

.alert--warn {
  color: var(--warn);
  background: rgba(242, 190, 99, 0.08);
  border: 1px solid var(--warn);
}
</style>
