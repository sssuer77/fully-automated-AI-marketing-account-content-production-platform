<script setup lang="ts">
// 渲染面板（T4.6 · §04.2.8）。
//
// 这一屏只回答四个问题，多一个都不放：
// ① 拿什么参数出片？—— 任务号 / 口播文案 / 画布档 / 音色 / 复用配音 / 种子 / 线程数；
// ② 我这条跑到哪一步了？—— 阶段 + 已完成 / 总数 + 日志尾巴 + 一条进度条；
// ③ 跑出来的是什么？—— 成片列表 + 就地播放（`<video>` 直接取后端给的 url）；
// ④ 中途不想跑了怎么办？—— 一颗取消按钮，**并且写清楚它是协作式的**。
//
// 三条必须写在面板上的话
// ----------------------
// 1. **取消是协作式的**：排队中的立刻作废，已经在跑的会走到下一个检查点才停，ffmpeg
//    一旦跑起来要等它自己结束。不写这一句，用户按了取消看见进度条还在动，会以为按钮坏了。
// 2. **水印与字幕都是可选层**：盘上有就贴、没有就跳过（跳过原因就写在横幅里）。这与 D5 的旧
//    口径相反，所以必须说出来 —— 否则"我的片子怎么没水印 / 没字幕"会被当成 bug。
// 3. **配音引擎现在是 SAPI**（Windows 内置语音）：音色列表就是本机装的那几个。CosyVoice
//    还没接上，面板上不许出现一个点不动的"高级音色"下拉框。
//
// 为什么文案留空是**合法**的
// --------------------------
// 后端规则只有一条：`text` 非空就用它，否则按 `task_id` 读库里那一版生效稿件。所以
// "我在稿件面板刚出了一版稿，直接拿它出片"是一条正经路径，不是漏填。提示里写清楚，
// 免得用户为了出片去把稿子再粘一遍（然后粘出一版和库里不一样的稿）。

import { computed, onMounted, onUnmounted, ref, watch } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import { renderVideoUrl, type RenderJob, type RenderProfileOption } from "@/api/endpoints/render";
import { formatBytes } from "@/stores/metrics";
import {
  RENDER_POLL_MS,
  formatDurationMs,
  qualityNotice,
  qualityReading,
  resultVideoName,
  statusLabel,
  statusTone,
  subtitleNotice,
  useRenderStore,
  watermarkNotice,
} from "@/stores/render";
import { handoffTaskId, useUiStore } from "@/stores/ui";

const renders = useRenderStore();
const ui = useUiStore();

// 进面板即接线：一次首屏拉取；有活在跑时 store 自己会按 `RENDER_POLL_MS` 轮询，
// 离开面板必须停 —— 否则切走之后还在每秒问一次后端。
onMounted(() => {
  // 从别的面板跳过来（T4.14）：**只填框，不自动出片** ——
  // 出片是花钱花时间的那一步，必须由人按下去。
  const jump = ui.takeHandoff("renders");
  if (jump !== null) renders.draft.taskId = jump;
  renders.start();
});
onUnmounted(() => renders.stop());

const job = computed(() => renders.focusedJob);
const running = computed(() => renders.running !== null);
const cancelable = computed(
  () => job.value !== null && (job.value.status === "running" || job.value.status === "queued"),
);

/** 任务号还空着就跳不过去（配音面板也认不出空任务号）。 */
const canJump = computed(() => handoffTaskId(renders.draft.taskId) !== null);

/** 正在播放 / 选中的那一支。默认取最新的一支（列表已按时间倒序）。 */
const selectedName = ref<string | null>(null);
const playing = computed(() => {
  const list = renders.videos;
  return list.find((video) => video.name === selectedName.value) ?? list[0] ?? null;
});

// 刚跑完那条的成片自动选上：按下出片之后，用户最想看的就是它。
watch(
  () => (job.value === null ? null : resultVideoName(job.value)),
  (name) => {
    if (name !== null) selectedName.value = name;
  },
);

// ── 表单 ────────────────────────────────────────────────────────────────
// 用 `:value` + `@input` 而不是 `v-model`：`draft` 是一个 store 上的对象，
// 直接改字段比给每个框建一份双向绑定更少一层"这份值和那份值哪个是真的"。

function onTaskId(event: Event): void {
  renders.draft.taskId = (event.target as HTMLInputElement).value;
}

function onText(event: Event): void {
  renders.draft.text = (event.target as HTMLTextAreaElement).value;
}

function onProfile(event: Event): void {
  renders.draft.profile = (event.target as HTMLSelectElement).value;
}

function onVoice(event: Event): void {
  renders.draft.voice = (event.target as HTMLSelectElement).value;
}

function onSeed(event: Event): void {
  renders.draft.seed = (event.target as HTMLInputElement).value;
}

function onThreads(event: Event): void {
  renders.draft.threads = (event.target as HTMLInputElement).value;
}

function onReuseVoice(event: Event): void {
  renders.draft.reuseVoice = (event.target as HTMLInputElement).checked;
}

/** 勾/放复选框 = **显式**开关（`null` 才是"跟随配置"，那只有"清空表单"能回到）。 */
function onSubtitle(event: Event): void {
  renders.draft.subtitle = (event.target as HTMLInputElement).checked;
}

function profileLabel(option: RenderProfileOption): string {
  const quality = `${option.quality_field.toUpperCase()} ${option.quality}`;
  return `${option.name} · ${option.width}×${option.height} @${option.fps}fps · ${quality}`;
}

// ── 展示 ────────────────────────────────────────────────────────────────

/** ISO 时间戳 → `HH:MM:SS`（与实时日志面板同一手法；日期在这一屏没有意义）。 */
function clock(ts: string | null): string {
  return ts === null || ts.length < 19 ? "-" : ts.slice(11, 19);
}

function stageText(item: RenderJob): string {
  if (item.total > 0) return `${item.stage || "-"} ${item.done}/${item.total}`;
  return item.stage || "-";
}

function jobVideo(item: RenderJob): string | null {
  return resultVideoName(item);
}

/** 成片时长（结果里有就显示；没跑完 / 失败时是 `null`，不是 0 秒）。 */
function jobDuration(item: RenderJob): string | null {
  const ms = item.result?.["duration_ms"];
  return typeof ms === "number" ? formatDurationMs(ms) : null;
}

const watermark = computed(() =>
  renders.snapshot === null ? null : watermarkNotice(renders.snapshot),
);

/** 刚跑完那一条的质检结论（降级 / 响度）。跑完才出现 —— 跑之前没有结论可言。 */
const quality = computed(() => (job.value === null ? null : qualityNotice(job.value)));
const reading = computed(() => (job.value === null ? null : qualityReading(job.value)));

const subtitle = computed(() =>
  renders.snapshot === null ? null : subtitleNotice(renders.snapshot),
);
</script>

<template>
  <div class="renders">
    <p v-if="renders.loadError" class="alert alert--error">
      首屏拉取失败：{{ renders.loadError }}（下面显示的是上一次拿到的内容；点「刷新」重试）
    </p>

    <p v-if="renders.error" class="alert alert--error">动作失败：{{ renders.error }}</p>
    <p v-else-if="renders.notice" class="alert alert--ok">{{ renders.notice }}</p>

    <p v-if="renders.snapshot && !renders.engineReady" class="alert alert--error">
      配音引擎没有可用音色，出片这一步会直接失败。{{ renders.snapshot.engine_hint }}
    </p>
    <p v-else-if="renders.snapshot?.engine_hint" class="alert alert--warn">
      {{ renders.snapshot.engine_hint }}
    </p>

    <p v-if="watermark" class="alert alert--warn">
      **这次出片不带水印**（水印是可选装饰，不阻塞出片）：{{ watermark }}
    </p>

    <p v-if="subtitle" class="alert alert--warn">字幕：{{ subtitle }}</p>

    <PanelCard
      title="出片"
      :subtitle="
        renders.snapshot === null
          ? '还没有读到后端'
          : `引擎 ${renders.snapshot.engine} · ${renders.voices.length} 个音色 · ${renders.profiles.length} 档画布`
      "
    >
      <template #actions>
        <AppButton size="sm" :loading="renders.loading" @click="renders.refresh()">刷新</AppButton>
        <AppButton size="sm" @click="renders.resetDraft()">清空表单</AppButton>
        <AppButton
          size="sm"
          :disabled="!canJump"
          :title="canJump ? '去配音面板看这条任务的逐句状态' : '先填一个任务号'"
          @click="ui.goTo('voices', renders.draft.taskId)"
        >
          去配音 →
        </AppButton>
        <AppButton
          size="sm"
          variant="primary"
          :disabled="!renders.canSubmit"
          :loading="renders.busy"
          :title="renders.problem ?? '登记一条出片任务'"
          @click="renders.submit()"
        >
          出片
        </AppButton>
      </template>

      <div class="form">
        <label class="f f--task">
          <span class="f__key">任务号</span>
          <input
            class="field mono"
            type="text"
            :value="renders.draft.taskId"
            @input="onTaskId($event)"
          />
        </label>

        <label class="f f--profile">
          <span class="f__key">画布档</span>
          <select class="field mono" :value="renders.draft.profile" @change="onProfile($event)">
            <option v-for="option in renders.profiles" :key="option.name" :value="option.name">
              {{ profileLabel(option) }}
            </option>
          </select>
        </label>

        <label class="f f--voice">
          <span class="f__key">音色（本机 SAPI）</span>
          <select class="field mono" :value="renders.draft.voice" @change="onVoice($event)">
            <option v-for="voice in renders.voices" :key="voice.name" :value="voice.name">
              {{ voice.name }}
            </option>
          </select>
        </label>

        <label class="f f--num">
          <span class="f__key">随机种子（留空 = 随机挑素材）</span>
          <input
            class="field mono"
            type="number"
            :value="renders.draft.seed"
            @input="onSeed($event)"
          />
        </label>

        <label class="f f--num">
          <span class="f__key">线程数（留空 = ffmpeg 自定）</span>
          <input
            class="field mono"
            type="number"
            :value="renders.draft.threads"
            @input="onThreads($event)"
          />
        </label>

        <label class="check">
          <input
            type="checkbox"
            :checked="renders.draft.reuseVoice"
            @change="onReuseVoice($event)"
          />
          <span>复用已有配音（同一任务号下已配过音时，跳过配音直接合成）</span>
        </label>

        <label class="check">
          <input
            type="checkbox"
            :checked="renders.subtitleEnabled"
            @change="onSubtitle($event)"
          />
          <span>
            烧字幕（时间取自**逐句实测**的配音时长；字体缺失时自动跳过，不阻塞出片）
          </span>
        </label>

        <label class="f f--full">
          <span class="f__key">
            口播文案（{{ renders.draft.text.length }} / {{ renders.maxChars }} 字）
          </span>
          <textarea
            class="field field--area"
            rows="6"
            placeholder="留空 ⇒ 按上面的任务号读库里那一版**生效稿件**（在「稿件」面板出的那版）"
            :value="renders.draft.text"
            @input="onText($event)"
          />
        </label>
      </div>

      <p v-if="renders.problem" class="hint hint--err">{{ renders.problem }}</p>

      <p class="hint">
        点「出片」只是**登记一条任务**并立刻返回，真正的活在服务端的后台线程里跑（一次一条，
        与 `pools.yaml` 的 render 池同口径）。素材从 `data/assets/mc_parkour/` 里**随机挑一条**；
        水印有就贴、没有就跳过；BGM 目录空着就只留人声；字幕的字体找不到就跳过。
        **成片落在** `data/output/videos/`，文件名是 `<时间戳>_<任务号>_final.mp4`；
        字幕源文件与句级时间轴留在 `data/work/<任务号>/`（二次剪辑可直接复用）。
      </p>
      <p class="hint">
        任务登记表在**进程内存**里：API 重启过，或者在跑的这条滚出了最近 50 条，就查不到了 ——
        成片本身在盘上，不受影响。
      </p>
    </PanelCard>

    <PanelCard
      title="进度"
      :subtitle="
        job === null
          ? '还没有跑过'
          : `${job.id} · ${job.task_id} · ${statusLabel(job.status)} · 登记于 ${clock(job.created_at)}`
      "
    >
      <template #actions>
        <StatusDot v-if="job" :tone="statusTone(job.status)" :label="statusLabel(job.status)" />
        <AppButton
          v-if="job"
          size="sm"
          variant="danger"
          :disabled="!cancelable"
          :loading="renders.busy"
          title="协作式取消：排队中的立刻作废，在跑的会走到下一个检查点才停"
          @click="renders.cancel(job.id)"
        >
          取消
        </AppButton>
      </template>

      <EmptyState
        v-if="job === null"
        title="还没有出片任务"
        hint="在上面填一个任务号（已经给好了）和一段口播文案，点「出片」。"
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
            <span class="row__key">起止</span>
            <span class="row__val mono">
              {{ clock(job.started_at) }} → {{ clock(job.finished_at) }}
            </span>
          </div>
          <div v-if="jobVideo(job)" class="row">
            <span class="row__key">成片</span>
            <span class="row__val mono">
              {{ jobVideo(job) }}
              <template v-if="jobDuration(job)"> · {{ jobDuration(job) }}</template>
            </span>
          </div>
          <div v-if="reading" class="row">
            <span class="row__key">质检</span>
            <span class="row__val mono">成片实测 {{ reading }}</span>
          </div>
        </div>

        <p v-if="quality" class="alert alert--warn">{{ quality }}</p>

        <p v-if="job.error_message" class="alert alert--error">
          {{ job.error_code }} · {{ job.error_message }}
          <span v-if="job.remediation"><br />怎么办：{{ job.remediation }}</span>
        </p>

        <p v-if="running" class="hint">
          进度每 {{ RENDER_POLL_MS / 1000 }} 秒刷新一次（这条链路是**粗粒度**的：配音第 N 句 /
          渲染中 / 完成，不是每帧都在动）。「取消」是**协作式**的 —— 已经在跑的这一句配音或
          这一次编码会先跑完；ffmpeg 一旦跑起来要等它自己结束。
        </p>

        <pre v-if="job.logs.length > 0" class="logs mono">{{ job.logs.join("\n") }}</pre>
      </template>
    </PanelCard>

    <PanelCard title="成片" :subtitle="`共 ${renders.videos.length} 支 · data/output/videos/`">
      <EmptyState
        v-if="renders.videos.length === 0"
        title="盘上还没有成片"
        hint="上面跑完一条就会出现在这里；也可以从命令行跑 `studio render make --task-id <任务号> --text <文案>`。"
      />

      <div v-else class="split">
        <div class="player">
          <video
            v-if="playing"
            :key="playing.name"
            class="player__video"
            :src="playing.url || renderVideoUrl(playing.name)"
            controls
            preload="metadata"
          />
          <p class="hint">
            {{ playing?.name }} · {{ playing ? formatBytes(playing.size_bytes) : "-" }}
            <a class="player__link" :href="playing?.url" download>下载</a>
          </p>
        </div>

        <ul class="list">
          <li
            v-for="video in renders.videos"
            :key="video.name"
            class="item"
            :class="{ 'item--active': video.name === playing?.name }"
            @click="selectedName = video.name"
          >
            <span class="item__name mono">{{ video.name }}</span>
            <span class="item__sub mono">{{ formatBytes(video.size_bytes) }}</span>
          </li>
        </ul>
      </div>
    </PanelCard>

    <PanelCard title="最近任务" :subtitle="`最近 ${renders.jobs.length} 条（登记表在内存里，最多留 50 条）`">
      <EmptyState v-if="renders.jobs.length === 0" title="还没有任务" hint="跑过一条就会出现在这里。" />

      <table v-else class="table">
        <thead>
          <tr>
            <th>任务</th>
            <th>任务号</th>
            <th>状态</th>
            <th>阶段</th>
            <th>进度</th>
            <th>成片</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in renders.jobs" :key="item.id">
            <td class="mono">{{ item.id }}</td>
            <td class="mono">{{ item.task_id }}</td>
            <td>
              <StatusDot :tone="statusTone(item.status)" :label="statusLabel(item.status)" />
            </td>
            <td class="mono">{{ stageText(item) }}</td>
            <td class="mono">{{ item.percent }}%</td>
            <td class="mono">{{ jobVideo(item) ?? "-" }}</td>
            <td class="cell--actions">
              <AppButton size="sm" title="把这条的参数填回表单" @click="renders.reuseJob(item)">
                再来一条
              </AppButton>
              <AppButton
                size="sm"
                :title="`去配音面板看 ${item.task_id} 的逐句状态`"
                @click="ui.goTo('voices', item.task_id)"
              >
                去配音 →
              </AppButton>
            </td>
          </tr>
        </tbody>
      </table>
    </PanelCard>
  </div>
</template>

<style scoped>
.renders {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

/* 行尾的两颗"去下一步"按钮：一条任务的两个去向。 */
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

.f--full {
  flex: 1 1 100%;
}

.f--task {
  flex: 0 0 200px;
}

.f--profile {
  flex: 1 1 320px;
}

.f--voice {
  flex: 1 1 220px;
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

.field--area {
  height: auto;
  padding: var(--space-2);
  font-family: var(--font-sans);
  line-height: 1.6;
  resize: vertical;
}

.check {
  display: inline-flex;
  flex: 1 1 100%;
  gap: var(--space-2);
  align-items: center;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.hint {
  margin-top: var(--space-3);
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.hint--err {
  color: var(--error);
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

.split {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(260px, 1fr);
  gap: var(--space-4);
}

.player {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
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

.list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 420px;
  padding: 0;
  margin: 0;
  overflow: auto;
  list-style: none;
}

.item {
  display: flex;
  gap: var(--space-3);
  align-items: baseline;
  justify-content: space-between;
  padding: var(--space-1) var(--space-2);
  cursor: pointer;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
}

.item:hover {
  background: var(--bg-hover);
}

.item--active {
  background: var(--accent-soft);
  border-color: var(--accent);
}

.item__name {
  min-width: 0;
  overflow: hidden;
  color: var(--text-primary);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item__sub {
  color: var(--text-muted);
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