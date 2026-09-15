<script setup lang="ts">
// ③ 稿件面板 + 确认闸（T4.4 · §04.4.4 / §04.4.5 第 3 行）—— 全流程**唯一**的人工节点。
//
// 这一屏只回答四个问题，多一个都不放：
// ① 现在有几条在等我？—— 左侧队列（勾选 + 计数 + 状态过滤）；
// ② 这条稿子为什么是这个分？—— 双通道明细（规则通道 + LLM 六维度）与 `issues`；
// ③ 改了几轮、这一轮动了哪几句？—— `revision_round` + 版本逐句对照；
// ④ 按下按钮会发生什么？—— 放行 / 退回（必填意见）/ 放弃（二次确认，可捞回）。
//
// 为什么"退回必填"在前端也拦一道
// ------------------------------
// 后端 `APPROVAL_COMMENT_REQUIRED` 才是权威（§04.4.4 不变量 3）。前端拦的是
// **白跑一次往返**：按钮点下去、转一圈、回来说"意见必填" —— 用户只会觉得卡。
//
// 为什么放弃要二次确认、捞回只留一个按钮
// --------------------------------------
// 放弃是唯一把人推向终局态的动作（`awaiting_approval → discarded`），而补救要多走
// 一步。把"代价大的那个"做难、把"补救"做易，是这个面板唯一的交互取舍。

import { computed, onMounted, ref, watch } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import GradeBadge from "@/components/GradeBadge.vue";
import PanelCard from "@/components/PanelCard.vue";
import SentenceDiffList from "@/components/SentenceDiffList.vue";
import { useTaskStream } from "@/composables/useTaskStream";
import {
  STATUS_LABELS,
  canReject,
  isGateEvent,
  useScriptsStore,
  type ApprovalStatus,
} from "@/stores/scripts";
import type { Envelope } from "@/ws/events";

const scripts = useScriptsStore();

const STATUSES: ApprovalStatus[] = ["pending", "approved", "rejected", "discarded"];

/** LLM 六维度（顺序与 `domain/scoring.py` 的 `LLM_DIMENSIONS` 逐字一致）。 */
const LLM_DIMENSIONS: ReadonlyArray<readonly [string, string]> = [
  ["hook_opening", "开场钩子"],
  ["positioning_fit", "定位契合"],
  ["oral_style", "口语化"],
  ["emotion_rhythm", "情绪节奏"],
  ["ending_cta", "结尾引导"],
  ["forbidden", "禁区"],
];

/** 版本对照的两个下拉：**都必须显式选**（默认值会让人对着错的对照下结论）。 */
const diffFrom = ref<number | null>(null);
const diffTo = ref<number | null>(null);

const detail = computed(() => scripts.detail);
const approval = computed(() => detail.value?.approval ?? null);
const versions = computed(() => scripts.versions?.versions ?? []);
const diffReady = computed(() => diffFrom.value !== null && diffTo.value !== null);

const subtitle = computed(() => {
  const parts = [
    `待审 ${scripts.pendingCount} 条`,
    `本页 ${scripts.queue.length} 条`,
    `勾选 ${scripts.checked.length} 条`,
  ];
  if (scripts.busy) parts.push("正在提交…");
  return parts.join(" · ");
});

const durationText = computed(() => {
  const ms = detail.value?.script.est_duration_ms ?? null;
  return ms === null ? "-" : `${(ms / 1000).toFixed(1)} 秒`;
});

/** 对象形状的未知值 → 可安全下标的 Record（`rule_detail` / `issues` 都是自由 JSON）。 */
function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function formatDetail(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length === 0 ? "-" : value.map((item) => String(item)).join("、");
  }
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

function dimensionScore(detailValue: Record<string, unknown>, key: string): string {
  const score = asRecord(detailValue[key]).score;
  return typeof score === "number" ? score.toFixed(1) : "-";
}

function dimensionComment(detailValue: Record<string, unknown>, key: string): string {
  const comment = asRecord(detailValue[key]).comment;
  return typeof comment === "string" ? comment : "";
}

function issueCode(issue: unknown): string {
  const code = asRecord(issue).code;
  return typeof code === "string" ? code : "ISSUE";
}

function issueDetail(issue: unknown): string {
  const record = asRecord(issue);
  return [record.severity, record.target, record.detail, record.suggestion]
    .filter((part) => typeof part === "string" && part.length > 0)
    .join(" · ");
}

async function onStatusChange(event: Event): Promise<void> {
  await scripts.setStatus((event.target as HTMLSelectElement).value as ApprovalStatus);
}

async function onLoadDiff(): Promise<void> {
  const from = diffFrom.value;
  const to = diffTo.value;
  if (from === null || to === null) return;
  await scripts.loadDiff(from, to);
}

/** 队列变了就重拉（事件很稀：一条待审 / 一次决断 / 一次状态迁移，人-paced）。 */
function onTaskEvent(envelope: Envelope): void {
  if (!isGateEvent(envelope.data.kind)) return;
  void scripts.refresh();
}

watch(
  () => scripts.selectedTaskId,
  () => {
    diffFrom.value = null;
    diffTo.value = null;
  },
);

onMounted(async () => {
  await scripts.refresh();
  const first = scripts.queue[0];
  if (first !== undefined && scripts.selectedTaskId === null) await scripts.select(first.task_id);
});

useTaskStream(onTaskEvent);
</script>

<template>
  <PanelCard fill dense title="稿件 · 确认闸" :subtitle="subtitle">
    <template #actions>
      <select class="field mono" :value="scripts.status" title="队列状态" @change="onStatusChange">
        <option v-for="item in STATUSES" :key="item" :value="item">
          {{ STATUS_LABELS[item] }}（{{ scripts.counts[item] ?? 0 }}）
        </option>
      </select>
      <AppButton size="sm" :loading="scripts.loading" @click="scripts.refresh()">刷新</AppButton>
    </template>

    <div class="wrap">
      <p v-if="scripts.error" class="notice notice--error">{{ scripts.error }}</p>
      <p v-else-if="scripts.notice" class="notice">{{ scripts.notice }}</p>
      <p v-else class="notice notice--muted">
        确认闸是**唯一**的人工节点：其余环节全自动（§04.4.4 不变量 1）。
      </p>

      <div class="split">
        <section class="queue">
          <ul v-if="scripts.queue.length > 0" class="queue__list">
            <li
              v-for="item in scripts.queue"
              :key="item.task_id"
              class="item"
              :class="{ 'item--active': item.task_id === scripts.selectedTaskId }"
            >
              <input
                type="checkbox"
                :checked="scripts.checked.includes(item.task_id)"
                :title="`勾选 ${item.task_id} 用于批量通过`"
                @change="scripts.toggleChecked(item.task_id)"
              />
              <button type="button" class="item__open" @click="scripts.select(item.task_id)">
                <span class="item__id mono">{{ item.task_id }}</span>
                <GradeBadge
                  :grade="item.grade"
                  :score="item.score_total"
                  :round="item.revision_round"
                />
                <span v-if="item.decided_by" class="item__by mono">{{ item.decided_by }}</span>
              </button>
            </li>
          </ul>
          <EmptyState
            v-else
            :title="`没有${STATUS_LABELS[scripts.status]}的记录`"
            hint="队列随 WS `tasks` 通道自动刷新；也可以点右上角「刷新」。"
          />

          <div class="queue__batch">
            <AppButton size="sm" :disabled="!scripts.hasSelection" @click="scripts.requestBatch()">
              批量通过（{{ scripts.checked.length }}）
            </AppButton>
            <AppButton v-if="scripts.hasSelection" size="sm" @click="scripts.clearChecked()">
              清空勾选
            </AppButton>
          </div>

          <div v-if="scripts.pendingBatch" class="confirm">
            <p class="confirm__title">
              将放行以下 {{ scripts.pendingBatch.length }} 条（逐条写 audit_ops，部分失败不回滚）：
            </p>
            <ul class="confirm__list">
              <li v-for="item in scripts.pendingBatch" :key="item.task_id" class="mono">
                {{ item.task_id }} · {{ item.grade ?? "-" }} · {{ item.score_total ?? "-" }}
              </li>
            </ul>
            <div class="confirm__actions">
              <AppButton
                variant="primary"
                size="sm"
                :loading="scripts.busy"
                @click="scripts.confirmBatch()"
              >
                确认放行
              </AppButton>
              <AppButton size="sm" @click="scripts.cancelBatch()">取消</AppButton>
            </div>
          </div>

          <ul v-if="scripts.failures.length > 0" class="failures">
            <li v-for="item in scripts.failures" :key="item.task_id" class="failures__item">
              <span class="mono">{{ item.task_id }}</span>
              <span class="failures__code mono">{{ item.code }}</span>
              <span>{{ item.message }}</span>
              <span v-if="item.remediation" class="failures__fix">{{ item.remediation }}</span>
            </li>
          </ul>
        </section>

        <section class="detail">
          <EmptyState
            v-if="!detail"
            title="选一条稿件"
            hint="左侧点一条记录，这里显示正文 / 双通道评分 / 修改轮次。"
          />
          <template v-else>
            <header class="head">
              <span class="head__id mono">{{ detail.task_id }}</span>
              <span class="head__status mono">{{ detail.task_status }}</span>
              <span class="head__round">第 {{ detail.revision_round }} 轮修改</span>
              <GradeBadge
                :grade="detail.script.grade"
                :score="detail.script.score_total"
                :round="detail.revision_round"
              />
              <span class="head__meta mono">
                {{ detail.script.word_count }} 字 · 约 {{ durationText }} · v{{ detail.script.version }}
              </span>
            </header>

            <div v-if="approval" class="gate">
              <textarea
                v-model="scripts.rejectComment"
                class="gate__comment"
                rows="2"
                placeholder="退回意见（必填）：下一轮改稿只认这条意见"
              />
              <div class="gate__actions">
                <AppButton
                  variant="primary"
                  size="sm"
                  :loading="scripts.busy"
                  @click="scripts.approve(detail.task_id)"
                >
                  确认放行
                </AppButton>
                <AppButton
                  size="sm"
                  :disabled="!canReject(scripts.rejectComment)"
                  :loading="scripts.busy"
                  @click="scripts.reject(detail.task_id)"
                >
                  退回改稿
                </AppButton>
                <template v-if="scripts.pendingDiscard !== detail.task_id">
                  <AppButton
                    variant="danger"
                    size="sm"
                    @click="scripts.requestDiscard(detail.task_id)"
                  >
                    放弃
                  </AppButton>
                </template>
                <template v-else>
                  <span class="gate__warn">再点一次即放弃（之后可在「已放弃」里捞回）</span>
                  <AppButton
                    variant="danger"
                    size="sm"
                    :loading="scripts.busy"
                    @click="scripts.confirmDiscard()"
                  >
                    确认放弃
                  </AppButton>
                  <AppButton size="sm" @click="scripts.cancelDiscard()">取消</AppButton>
                </template>
              </div>
              <p class="gate__hint mono">
                放行 ⇒ queued_voice ｜ 退回 ⇒ editing 且轮次 +1 ｜ 放弃 ⇒ discarded（可捞回）
              </p>
            </div>
            <div v-else-if="detail.task_status === 'discarded' || detail.task_status === 'canceled'" class="gate">
              <span class="gate__warn">这条已被放弃（{{ detail.task_status }}）。</span>
              <AppButton size="sm" :loading="scripts.busy" @click="scripts.rescue(detail.task_id)">
                捞回（回到 pending）
              </AppButton>
            </div>

            <article class="script">
              <h3 v-if="detail.script.title" class="script__title">{{ detail.script.title }}</h3>
              <p v-if="detail.script.hook" class="script__hook">钩子：{{ detail.script.hook }}</p>
              <pre class="script__body">{{ detail.script.body_md }}</pre>
              <p v-if="detail.script.cta" class="script__cta">CTA：{{ detail.script.cta }}</p>
              <p class="script__meta mono">
                {{ detail.script.llm_model ?? "-" }} · prompt {{ detail.script.prompt_version ?? "-" }} ·
                规则 {{ detail.script.score_rule ?? "-" }} / LLM {{ detail.script.score_llm ?? "-" }}
              </p>
            </article>

            <details class="block" open>
              <summary>逐句（{{ detail.sentences.length }} 句 · 按句合成、断点续传的最小单位）</summary>
              <table class="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>说话人</th>
                    <th>情绪</th>
                    <th>停顿</th>
                    <th>文本</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="sentence in detail.sentences" :key="sentence.seq">
                    <td class="mono">{{ sentence.seq }}</td>
                    <td class="mono">{{ sentence.speaker }}</td>
                    <td class="mono">{{ sentence.emotion }}</td>
                    <td class="mono">{{ sentence.pause_after_ms }}ms</td>
                    <td>{{ sentence.text }}</td>
                  </tr>
                </tbody>
              </table>
            </details>

            <details
              v-for="review in detail.reviews"
              :key="review.round_no"
              class="block"
              open
            >
              <summary>
                第 {{ review.round_no }} 轮 · {{ review.grade }} · 总分 {{ review.total.toFixed(1) }}
                （规则 {{ review.rule_total.toFixed(1) }} × 0.3 + LLM {{ review.llm_total.toFixed(1) }} × 0.7）
                · {{ review.decision }}
              </summary>
              <div class="channels">
                <table class="table">
                  <thead>
                    <tr>
                      <th colspan="2">通道一 · 规则（可复算）</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(value, key) in review.rule_detail" :key="key">
                      <td class="mono">{{ key }}</td>
                      <td>{{ formatDetail(value) }}</td>
                    </tr>
                  </tbody>
                </table>
                <table class="table">
                  <thead>
                    <tr>
                      <th colspan="3">通道二 · LLM 六维度</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="[key, label] in LLM_DIMENSIONS" :key="key">
                      <td>{{ label }}</td>
                      <td class="mono">{{ dimensionScore(review.llm_detail, key) }}</td>
                      <td>{{ dimensionComment(review.llm_detail, key) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <ul v-if="review.issues.length > 0" class="issues">
                <li v-for="(issue, index) in review.issues" :key="index" class="issues__item">
                  <span class="issues__code mono">{{ issueCode(issue) }}</span>
                  <span>{{ issueDetail(issue) }}</span>
                </li>
              </ul>
              <p v-else class="issues__none">这一轮没有指出问题（退回时才会成为下一轮 Editor 的输入）。</p>
            </details>

            <details class="block" open>
              <summary>版本对照（共 {{ versions.length }} 版）</summary>
              <div class="diffbar">
                <select v-model="diffFrom" class="field mono">
                  <option :value="null" disabled>旧版本</option>
                  <option v-for="item in versions" :key="item.version" :value="item.version">
                    v{{ item.version }}{{ item.is_active ? "（当前）" : "" }} · {{ item.grade ?? "-" }} ·
                    {{ item.sentence_count }} 句
                  </option>
                </select>
                <span class="diffbar__arrow">→</span>
                <select v-model="diffTo" class="field mono">
                  <option :value="null" disabled>新版本</option>
                  <option v-for="item in versions" :key="item.version" :value="item.version">
                    v{{ item.version }}{{ item.is_active ? "（当前）" : "" }} · {{ item.grade ?? "-" }} ·
                    {{ item.sentence_count }} 句
                  </option>
                </select>
                <AppButton size="sm" :disabled="!diffReady" @click="onLoadDiff()">对照</AppButton>
                <AppButton v-if="scripts.diff" size="sm" @click="scripts.clearDiff()">清空</AppButton>
              </div>
              <p v-if="!diffReady" class="notice notice--muted">
                两个版本都要显式选：改稿可能一次跳两版（人工退回 + 自动重跑），默认值会让人对着错的对照下结论。
              </p>
              <template v-else-if="scripts.diff">
                <p class="notice">
                  v{{ scripts.diff.from_version }} → v{{ scripts.diff.to_version }}：改
                  {{ scripts.diff.summary.changed }} 句 · 增 {{ scripts.diff.summary.added }} 句 · 删
                  {{ scripts.diff.summary.removed }} 句 · 未动
                  {{ scripts.diff.summary.unchanged }} 句
                </p>
                <SentenceDiffList :changes="scripts.diff.changes" />
              </template>
            </details>
          </template>
        </section>
      </div>
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

.split {
  display: grid;
  flex: 1;
  grid-template-columns: 300px 1fr;
  gap: var(--space-3);
  min-height: 0;
}

.queue,
.detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-height: 0;
  overflow-y: auto;
}

.queue {
  padding-right: var(--space-2);
  border-right: 1px solid var(--border-subtle);
}

.queue__list {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.item {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
}

.item:hover {
  background: var(--bg-hover);
}

.item--active {
  background: var(--accent-soft);
  box-shadow: inset 2px 0 0 var(--accent);
}

.item__open {
  display: flex;
  flex: 1;
  gap: var(--space-2);
  align-items: center;
  min-width: 0;
  text-align: left;
}

.item__id {
  overflow: hidden;
  color: var(--text-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item__by {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.queue__batch {
  display: flex;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--border-subtle);
}

.confirm {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  background: var(--bg-raised);
  border: 1px solid var(--warn);
  border-radius: var(--radius-sm);
}

.confirm__title {
  font-size: var(--text-xs);
}

.confirm__list {
  max-height: 140px;
  margin: 0;
  padding-left: var(--space-4);
  overflow-y: auto;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.confirm__actions {
  display: flex;
  gap: var(--space-2);
}

.failures {
  margin: 0;
  padding: 0;
  list-style: none;
}

.failures__item {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  border-left: 2px solid var(--error);
}

.failures__code {
  color: var(--error);
}

.failures__fix {
  color: var(--text-muted);
}

.head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: center;
}

.head__id {
  color: var(--text-primary);
}

.head__status {
  color: var(--text-muted);
}

.head__round {
  color: var(--warn);
  font-size: var(--text-xs);
}

.head__meta {
  color: var(--text-muted);
}

.gate {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2);
  background: var(--bg-raised);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
}

.gate__comment {
  width: 100%;
  padding: var(--space-1) var(--space-2);
  color: var(--text-primary);
  font: inherit;
  font-size: var(--text-sm);
  resize: vertical;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.gate__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}

.gate__warn {
  color: var(--warn);
  font-size: var(--text-xs);
}

.gate__hint {
  color: var(--text-muted);
}

.script {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.script__title {
  font-size: var(--text-lg);
}

.script__hook {
  color: var(--accent);
  font-size: var(--text-sm);
}

.script__body {
  margin: 0;
  font-family: var(--font-sans);
  font-size: var(--text-md);
  white-space: pre-wrap;
  word-break: break-word;
}

.script__cta {
  color: var(--text-secondary);
  font-size: var(--text-sm);
}

.script__meta {
  color: var(--text-muted);
}

.block {
  padding: var(--space-2);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.block > summary {
  color: var(--text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
}

.channels {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
  padding-top: var(--space-2);
}

.table {
  width: 100%;
  font-size: var(--text-xs);
  border-collapse: collapse;
}

.table th {
  color: var(--text-muted);
  font-weight: 600;
  text-align: left;
  border-bottom: 1px solid var(--border-subtle);
}

.table td {
  padding: 2px var(--space-2) 2px 0;
  vertical-align: top;
  border-bottom: 1px solid var(--bg-hover);
}

.issues {
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
}

.issues__item {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-1) 0;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.issues__code {
  color: var(--warn);
}

.issues__none {
  margin-top: var(--space-2);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.diffbar {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  padding: var(--space-2) 0;
}

.diffbar__arrow {
  color: var(--text-muted);
}

.field {
  height: 22px;
  padding: 0 var(--space-2);
  color: var(--text-primary);
  font-size: var(--text-xs);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
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
