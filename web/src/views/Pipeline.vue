<script setup lang="ts">
// 一键出片面板（T4.14 延伸 · §04.6.8）。
//
// 这一屏只回答四个问题，多一个都不放：
// ① 这条任务现在在哪儿、点了会怎样？—— 状态 + 一句话结论 + 不能按的原因；
// ② 拿什么参数跑？—— 任务号 / 落点 / 音色 / 种子；
// ③ 跑到哪一步了、成片出来没有？—— 阶段 + 已完成 / 总数 + 日志尾巴 + 就地播放；
// ④ 中途不想跑了怎么办？—— 一颗取消按钮，并且写清楚它是协作式的。
//
// 三条必须写在面板上的话
// ----------------------
// 1. 这条链路**不写稿**：`pending` / `drafting` 的任务要先去「稿件」面板出一版稿。
//    流水线只接手"已经有生效稿件"的任务 —— 它不会偷偷调 LLM（那会花钱，而且不在
//    用户按的这一下里）。
// 2. 它也**不代按确认闸**：停在 `awaiting_approval` 的任务要先在确认闸决断。
//    那是全流程唯一的人工节点，顺手按掉等于那个节点不存在。
// 3. **取消是协作式的，而且检查点比渲染少**：只在配音的每一句与渲染的每一段。
//    停在"投递配音作业"或"拼母带"那几步之间时按取消，要等它进到下一个回调点。
//
// 为什么这一屏**不重复**渲染面板的参数
// ------------------------------------
// 画布档 / 字幕 / 线程数是**渲染层**的东西，归「合成配置」与「渲染」两块面板管。
// 在这一屏再放一遍，要么得多问后端一次"配置里现在开着吗"（多一处会过时的显示），
// 要么就变成一个"看着是关的、实际是跟随配置"的假开关。这一屏只管一件事：
// **从这条任务当前的状态推到哪一步**。
//
// 为什么不用起任何池进程
// ----------------------
// 后端在配音那一步会**就地借一条 worker** 把这条任务的配音念完（T2.8 的裁定），
// 所以这一屏在"没起任何池进程"的机器上也能从稿子一路做到成片 —— 这正是它作为
// "第一支 MP4 的入口"的意义。常驻池同时在跑也不会出错：作业认领靠租约。

import { computed, onMounted, onUnmounted } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import { renderVideoUrl } from "@/api/endpoints/render";
import {
  PIPELINE_POLL_MS,
  finalVideoName,
  formatClock,
  jobStatusLabel,
  jobStatusTone,
  previewReason,
  stageText,
  taskStatusLabel,
  taskStatusTone,
  usePipelineStore,
} from "@/stores/pipeline";
import { handoffTaskId, useUiStore } from "@/stores/ui";

const pipeline = usePipelineStore();
const ui = useUiStore();

// 进面板即接线：一次首屏拉取；有活在跑时 store 自己会按 `PIPELINE_POLL_MS` 轮询，
// 离开面板必须停 —— 否则切走之后还在每秒问一次后端。
onMounted(() => {
  // 从别的面板跳过来（T4.14）：**只填框，不自动出片** ——
  // 出片是花钱花时间的那一步，必须由人按下去。
  const jump = ui.takeHandoff("pipeline");
  if (jump !== null) pipeline.draft.taskId = jump;
  pipeline.start();
  if (pipeline.draft.taskId.trim() !== "") void pipeline.ask();
});
onUnmounted(() => pipeline.stop());

const job = computed(() => pipeline.focusedJob);
const running = computed(() => pipeline.running !== null);
const cancelable = computed(
  () => job.value !== null && (job.value.status === "running" || job.value.status === "queued"),
);
/** 刚跑完那条的成片：按下开始之后，用户最想看的就是它。 */
const finalName = computed(() => (job.value === null ? null : finalVideoName(job.value)));
/** 不能按的原因（预览给的）。`null` = 没拦。 */
const reason = computed(() => previewReason(pipeline.preview));
/** 任务号还空着就跳不过去（稿件面板也认不出空任务号）。 */
const jumpTaskId = computed(() => handoffTaskId(pipeline.draft.taskId));

/** 选中的落点是什么意思（下拉框那一行的说明；选之前也能看见）。 */
const untilHint = computed(() => {
  const wanted =
    pipeline.draft.until === "" ? pipeline.snapshot?.default_until ?? "" : pipeline.draft.until;
  return pipeline.untilOptions.find((option) => option.value === wanted)?.description ?? "";
});

// ── 表单 ────────────────────────────────────────────────────────────────
// 用 `:value` + `@change` 而不是 `v-model`：`draft` 是一个 store 上的对象，
// 直接改字段比给每个框建一份双向绑定更少一层"这份值和那份值哪个是真的"。
// `@change`（而不是 `@input`）还顺带解决了另一件事：一次输入只换来**一次**预览请求，
// 而不是每敲一个字符问一次后端。

function onTaskId(event: Event): void {
  pipeline.draft.taskId = (event.target as HTMLInputElement).value;
  void pipeline.ask();
}

function onUntil(event: Event): void {
  pipeline.draft.until = (event.target as HTMLSelectElement).value;
  void pipeline.ask();
}

function onVoice(event: Event): void {
  pipeline.draft.voice = (event.target as HTMLSelectElement).value;
}

function onSeed(event: Event): void {
  pipeline.draft.seed = (event.target as HTMLInputElement).value;
}
</script>

<template>
  <div class="pipeline">
    <p v-if="pipeline.loadError" class="alert alert--error">
      首屏拉取失败：{{ pipeline.loadError }}（下面显示的是上一次拿到的内容；点「刷新」重试）
    </p>

    <p v-if="pipeline.error" class="alert alert--error">动作失败：{{ pipeline.error }}</p>
    <p v-else-if="pipeline.notice" class="alert alert--ok">{{ pipeline.notice }}</p>

    <p v-if="pipeline.snapshot && !pipeline.engineReady" class="alert alert--error">
      配音引擎没有可用音色，这一条链路会停在配音那一步。{{ pipeline.snapshot.engine_hint }}
    </p>

    <PanelCard
      title="一键出片"
      :subtitle="
        pipeline.snapshot === null
          ? '还没有读到后端'
          : `${pipeline.untilOptions.length} 个落点 · ${pipeline.voices.length} 个音色 · 登记表里最近 ${pipeline.jobs.length} 条`
      "
    >
      <template #actions>
        <AppButton size="sm" :loading="pipeline.loading" @click="pipeline.refresh()">刷新</AppButton>
        <AppButton size="sm" @click="pipeline.resetDraft()">清空表单</AppButton>
        <AppButton
          size="sm"
          :disabled="jumpTaskId === null"
          :title="jumpTaskId ? '去稿件面板看这条任务的稿子' : '先填一个任务号'"
          @click="ui.goTo('scripts', pipeline.draft.taskId)"
        >
          去稿件 →
        </AppButton>
        <AppButton
          size="sm"
          variant="primary"
          :disabled="!pipeline.canSubmit"
          :loading="pipeline.busy"
          :title="pipeline.problem ?? reason ?? '从这条任务当前的状态一路推到落点'"
          @click="pipeline.submit()"
        >
          开始出片
        </AppButton>
      </template>

      <div class="form">
        <label class="f f--task">
          <span class="f__key">任务号</span>
          <input
            class="field mono"
            type="text"
            placeholder="ULID 或你自己起的名字"
            :value="pipeline.draft.taskId"
            @change="onTaskId($event)"
          />
        </label>

        <label class="f f--until">
          <span class="f__key">落点（推到哪一步为止）</span>
          <select class="field mono" :value="pipeline.draft.until" @change="onUntil($event)">
            <option
              v-for="option in pipeline.untilOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }} · {{ option.value }}
            </option>
          </select>
        </label>

        <label class="f f--voice">
          <span class="f__key">音色（本机 SAPI）</span>
          <select class="field mono" :value="pipeline.draft.voice" @change="onVoice($event)">
            <option v-for="voice in pipeline.voices" :key="voice.name" :value="voice.name">
              {{ voice.name }}
            </option>
          </select>
        </label>

        <label class="f f--num">
          <span class="f__key">随机种子（留空 = 随机挑素材）</span>
          <input
            class="field mono"
            type="number"
            :value="pipeline.draft.seed"
            @input="onSeed($event)"
          />
        </label>
      </div>

      <p v-if="pipeline.problem" class="hint hint--err">{{ pipeline.problem }}</p>

      <p v-if="untilHint" class="hint">
        落点「{{ pipeline.draft.until || pipeline.snapshot?.default_until }}」：{{ untilHint }}
      </p>

      <!-- 预览：按按钮**之前**就把结论摆出来 -->
      <div v-if="pipeline.preview" class="preview">
        <div class="preview__head">
          <StatusDot
            :tone="taskStatusTone(pipeline.preview.status)"
            :label="taskStatusLabel(pipeline.preview.status)"
          />
          <span class="preview__title">{{ pipeline.preview.title }}</span>
          <span v-if="pipeline.stale" class="preview__stale">结论可能已过时，改完再看一次</span>
        </div>
        <p class="preview__note">{{ pipeline.preview.note }}</p>
        <p v-if="reason" class="alert alert--error">{{ reason }}</p>
        <p v-if="pipeline.preview.active_job" class="alert alert--warn">
          这个任务已经有一条在跑了（{{ pipeline.preview.active_job.id }}）—— 再按一次不会开出第二条，
          进度会接到那一条上。
          <AppButton size="sm" @click="pipeline.focus(pipeline.preview.active_job.id)">
            看进度
          </AppButton>
        </p>
      </div>
      <p v-else-if="pipeline.previewError" class="alert alert--error">{{ pipeline.previewError }}</p>
      <p v-else class="hint">填一个任务号，这里会先告诉你它现在在哪一步、点下去会发生什么。</p>

      <p class="hint">
        点「开始出片」只是登记一条任务并立刻返回，真正的活在服务端的后台线程里跑（一次一条）。
        这条链路不写稿：没有生效稿件会直接报错，先去「稿件」面板出一版。它也不代按确认闸 ——
        停在 awaiting_approval 的任务要自己在确认闸上放行或退回。
      </p>
      <p class="hint">
        配音那一步会就地借一条 worker 把这条任务念完，所以没起任何池进程也能从稿子一路做到成片；
        常驻池同时在跑也不会出错（作业认领靠租约）。成片落在 data/output/videos/，
        文件名是 &lt;时间戳&gt;_&lt;任务号&gt;_final.mp4；画布档 / 字幕 / 线程数在「合成配置」与「渲染」
        两块面板上调，这一屏不重复它们。
      </p>
    </PanelCard>

    <PanelCard
      title="进度"
      :subtitle="
        job === null
          ? '还没有跑过'
          : `${job.id} · ${job.task_id} · ${jobStatusLabel(job.status)} · 登记于 ${formatClock(job.created_at)}`
      "
    >
      <template #actions>
        <StatusDot
          v-if="job"
          :tone="jobStatusTone(job.status)"
          :label="jobStatusLabel(job.status)"
        />
        <AppButton
          v-if="job"
          size="sm"
          variant="danger"
          :disabled="!cancelable"
          :loading="pipeline.busy"
          title="协作式取消：下一次进度回调处才停（配音的每一句 / 渲染的每一段）"
          @click="pipeline.cancel(job.id)"
        >
          取消
        </AppButton>
      </template>

      <EmptyState
        v-if="job === null"
        title="还没有跑过"
        hint="在上面填一个任务号、选一个落点，点「开始出片」。"
      />

      <template v-else>
        <div class="bar">
          <div class="bar__fill" :style="{ width: `${job.percent}%` }" />
        </div>

        <div class="rows">
          <div class="row">
            <span class="row__key">阶段</span>
            <span class="row__val mono">{{ stageText(job) }}</span>
          </div>
          <div class="row">
            <span class="row__key">说明</span>
            <span class="row__val">{{ job.note || "-" }}</span>
          </div>
          <div class="row">
            <span class="row__key">落点</span>
            <span class="row__val mono">{{ job.until }}</span>
          </div>
          <div class="row">
            <span class="row__key">起止</span>
            <span class="row__val mono">
              {{ formatClock(job.started_at) }} → {{ formatClock(job.finished_at) }}
            </span>
          </div>
          <div v-if="finalName" class="row">
            <span class="row__key">成片</span>
            <span class="row__val mono">{{ finalName }}</span>
          </div>
        </div>

        <p v-if="job.error_message" class="alert alert--error">
          {{ job.error_code }} · {{ job.error_message }}
          <span v-if="job.remediation"><br />怎么办：{{ job.remediation }}</span>
        </p>

        <p v-if="running" class="hint">
          进度每 {{ PIPELINE_POLL_MS / 1000 }} 秒刷新一次（这条链路是粗粒度的：配音第 N 句 /
          渲染中 / 完成，不是每帧都在动）。「取消」是协作式的，而且检查点只在配音的每一句与渲染的
          每一段 —— 停在投递或拼母带那几步之间时，要等它进到下一个回调点。
        </p>

        <div v-if="finalName" class="player">
          <video
            :key="finalName"
            class="player__video"
            :src="renderVideoUrl(finalName)"
            controls
            preload="metadata"
          />
          <p class="hint">
            {{ finalName }}
            <a class="player__link" :href="renderVideoUrl(finalName)" download>下载</a>
          </p>
        </div>

        <ul v-if="job.steps.length > 0" class="steps">
          <li v-for="(step, index) in job.steps" :key="index" class="steps__item mono">
            {{ String(step["stage"] ?? "-") }}：{{ String(step["status_before"] ?? "-") }} →
            {{ String(step["status"] ?? "-") }} · {{ String(step["note"] ?? "") }}
          </li>
        </ul>

        <pre v-if="job.logs.length > 0" class="logs mono">{{ job.logs.join("\n") }}</pre>
      </template>
    </PanelCard>

    <PanelCard
      title="最近任务"
      :subtitle="`最近 ${pipeline.jobs.length} 条（登记表在内存里，最多留 50 条）`"
    >
      <EmptyState
        v-if="pipeline.jobs.length === 0"
        title="还没有任务"
        hint="跑过一条就会出现在这里。"
      />

      <table v-else class="table">
        <thead>
          <tr>
            <th>任务</th>
            <th>任务号</th>
            <th>落点</th>
            <th>状态</th>
            <th>阶段</th>
            <th>进度</th>
            <th>成片</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in pipeline.jobs" :key="item.id">
            <td class="mono">{{ item.id }}</td>
            <td class="mono">{{ item.task_id }}</td>
            <td class="mono">{{ item.until }}</td>
            <td>
              <StatusDot :tone="jobStatusTone(item.status)" :label="jobStatusLabel(item.status)" />
            </td>
            <td class="mono">{{ stageText(item) }}</td>
            <td class="mono">{{ item.percent }}%</td>
            <td class="mono">{{ finalVideoName(item) ?? "-" }}</td>
            <td class="cell--actions">
              <AppButton
                size="sm"
                title="把这一条的进度放到上面那块"
                @click="pipeline.focus(item.id)"
              >
                看进度
              </AppButton>
              <AppButton size="sm" title="把这条的参数填回表单" @click="pipeline.reuseJob(item)">
                再来一条
              </AppButton>
            </td>
          </tr>
        </tbody>
      </table>
    </PanelCard>
  </div>
</template>

<style scoped>
.pipeline {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.cell--actions {
  display: flex;
  gap: var(--space-2);
}

.alert {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.alert--error {
  color: var(--error);
  border-color: var(--error);
}

.alert--warn {
  color: var(--warn);
  border-color: var(--warn);
}

.alert--ok {
  color: var(--ok);
  border-color: var(--ok);
}

.form {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: flex-end;
}

.f {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.f--task {
  flex: 0 0 240px;
}

.f--until {
  flex: 1 1 280px;
}

.f--voice {
  flex: 1 1 200px;
}

.f--num {
  flex: 0 0 180px;
}

.f__key {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.field {
  height: 24px;
  padding: 0 var(--space-2);
  color: var(--text-primary);
  font-size: var(--text-xs);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.hint {
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.hint--err {
  color: var(--error);
}

.preview {
  padding: var(--space-2) var(--space-3);
  margin-top: var(--space-3);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.preview__head {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
}

.preview__title {
  color: var(--text-primary);
  font-size: var(--text-sm);
}

.preview__stale {
  color: var(--warn);
  font-size: var(--text-xs);
}

.preview__note {
  margin-top: var(--space-1);
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.bar {
  height: 6px;
  overflow: hidden;
  background: var(--bg-raised);
  border-radius: var(--radius-sm);
}

.bar__fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-3);
}

.row {
  display: flex;
  gap: var(--space-3);
  align-items: baseline;
  font-size: var(--text-sm);
}

.row__key {
  flex: 0 0 48px;
  color: var(--text-muted);
}

.row__val {
  min-width: 0;
  color: var(--text-secondary);
  word-break: break-all;
}

.steps {
  padding-left: var(--space-4);
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.steps__item {
  word-break: break-all;
}

.logs {
  max-height: 220px;
  padding: var(--space-2) var(--space-3);
  margin-top: var(--space-3);
  overflow: auto;
  color: var(--text-secondary);
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
}

.player {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-3);
}

.player__video {
  width: 100%;
  max-height: 420px;
  background: var(--bg-base);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.player__link {
  margin-left: var(--space-2);
  font-size: var(--text-xs);
}

.table {
  width: 100%;
  font-size: var(--text-sm);
  border-collapse: collapse;
}

.table th {
  padding: var(--space-1) var(--space-2);
  color: var(--text-muted);
  font-weight: 400;
  font-size: var(--text-xs);
  text-align: left;
  border-bottom: 1px solid var(--border-subtle);
}

.table td {
  padding: var(--space-1) var(--space-2);
  color: var(--text-secondary);
  border-bottom: 1px solid var(--border-subtle);
}
</style>
