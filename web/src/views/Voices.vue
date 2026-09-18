<script setup lang="ts">
// 配音面板（T4.5 · §04.3.7）。
//
// 这一屏只回答四个问题，多一个都不放：
// ① 这条任务配到哪一步了？—— 一条进度条 + 四态计数 + 逐句表；
// ② 哪一句出了岔子、为什么？—— `skipped` / `failed` 的行高亮，`tts_error` 直接写出来；
// ③ 某一句念错了怎么办？—— 那一行的「重配」（`synthesizing` 的行**没有**这颗按钮）；
// ④ 想换一个人的嗓子怎么办？—— 每个角色一个下拉框 + **二次确认框**。
//
// 三条必须写在面板上的话
// ----------------------
// 1. **换音色不是免费的**：一次点击会重配 N 句（确认框里写着 N 是多少）。后端先算代价
//    再抛 409，面板据此弹框 —— 不写这句话，用户会以为"换个音色"跟"改个标签"一样轻。
// 2. **重配不重算时间轴**：改完那几句只是回到待办，成片时长要等下一轮配音收口
//    （`studio pipeline run <任务号> --until queued_render`）才会全量重算。
// 3. **试听放的是盘上那一份，不触发合成**：没有音频的句子干脆不给播放键（后端给的就是
//    `null`）—— 发一个注定 404 的 url，面板上就是一个点了没反应的播放键。
//
// 为什么进度条要配**四态计数**而不是一个百分比
// --------------------------------------------
// 配音的"完成"有三种：配好、跳过（降级：这一句没声音，片子照样出）、失败。一个百分比会
// 把这三者压成一个数，而它们的**后果**完全不同 —— 跳过不用管，失败要人管。
//
// 为什么空着任务号**不发请求**
// ----------------------------
// 面板刚打开时任务号是空的。这时候拉一次 `GET /sentences?task_id=` 只会换来一个 422，
// 然后面板上挂一条红字 —— 而那根本不是错误，是"还没填"。空着就画空状态。

import { computed, onMounted, onUnmounted } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import type { SentenceVoice } from "@/api/endpoints/voice";
import { formatDurationMs } from "@/stores/render";
import { handoffTaskId, useUiStore } from "@/stores/ui";
import {
  VOICE_POLL_MS,
  durationText,
  outstanding,
  progressText,
  spanText,
  statusLabel,
  statusTone,
  useVoiceStore,
  voiceMapRows,
  voiceOptionLabel,
} from "@/stores/voice";

const voice = useVoiceStore();
const ui = useUiStore();

// 进面板即接线：一次首屏（逐句 + 音色下拉框）；还有句子在念时 store 自己会按
// `VOICE_POLL_MS` 轮询，离开面板必须停 —— 否则切走之后还在每秒问一次后端。
onMounted(() => {
  // 从别的面板跳过来（T4.14）：先把任务号接下来再首屏 ——
  // `setTaskId` 会清掉上一条任务的快照，顺序反了会拉出一屏上个任务的句子。
  const jump = ui.takeHandoff("voices");
  if (jump !== null) voice.setTaskId(jump);
  voice.start();
});
onUnmounted(() => voice.stop());

const percent = computed(() => Math.round((voice.progress?.ratio ?? 0) * 100));
const polling = computed(() => outstanding(voice.progress) > 0);

/** 任务号还空着就跳不过去（跳过去也是一屏空白）。 */
const canJump = computed(() => handoffTaskId(voice.taskId) !== null);

function onTaskId(event: Event): void {
  voice.setTaskId((event.target as HTMLInputElement).value);
}

function onLoad(): void {
  voice.start();
}

function onVoice(speaker: string, event: Event): void {
  voice.draft[speaker] = (event.target as HTMLSelectElement).value;
}

/** 文本列只给一行；完整的那句在 `title` 里（表格里塞整段稿子会把行高撑到看不清）。 */
function preview(text: string): string {
  return text.length > 42 ? `${text.slice(0, 42)}…` : text;
}

/** 这一句现在能不能点「重配」：判据来自服务端（`synthesizing` 不能动），这里只叠加"有活在途"。 */
function canResynth(row: SentenceVoice): boolean {
  return row.can_resynth && voice.busyId !== row.id && !voice.busy;
}

function resynthTitle(row: SentenceVoice): string {
  if (!row.can_resynth) return "这一句正被念着（synthesizing），等它跑完再点 —— 现在点必被拒";
  return "把这一句退回待办并重新念一遍（只重念这一句）";
}

function audioTitle(row: SentenceVoice): string {
  return `来源：${row.audio_source ?? "?"}（放的是盘上已经有的那一份，不触发合成）`;
}

/** 句号列表 → `1、2、3`；超过 12 个就折叠（确认框里不需要一屏数字）。 */
function seqList(seqs: readonly number[]): string {
  if (seqs.length === 0) return "（没有）";
  const head = seqs.slice(0, 12).join("、");
  return seqs.length > 12 ? `${head} … 共 ${seqs.length} 句` : head;
}

/** 每个角色一行（"库里记着什么 / 选着什么 / 本机找不找得到"由 store 的纯函数算）。 */
const mapRows = computed(() =>
  voiceMapRows(voice.speakers, voice.draft, voice.voiceMap, voice.voices),
);

function timelineText(): string {
  if (voice.timelineTotalMs === null) return "盘上还没有时间轴";
  return `盘上那一版是 ${formatDurationMs(voice.timelineTotalMs)}`;
}

function taskSubtitle(): string {
  if (voice.snapshot === null) return "还没有读到后端";
  return `状态 ${voice.taskStatus} · ${voice.total} 句 · 时间轴 ${timelineText()}`;
}

function resynthSubtitle(): string {
  if (voice.sentences.length === 0) return "这条任务还没有逐句稿件";
  return `共 ${voice.sentences.length} 句 · 试听放的是盘上那一份，不触发合成`;
}

function mapSubtitle(): string {
  return `${voice.speakers.length} 个角色 · 本机 ${voice.voices.length} 个可用音色`;
}
</script>

<template>
  <div class="voices">
    <p v-if="voice.loadError" class="alert alert--error">
      拉取失败：{{ voice.loadError }}（下面显示的是上一次拿到的内容；点「刷新」重试）
    </p>

    <p v-if="voice.error" class="alert alert--error">动作失败：{{ voice.error }}</p>
    <p v-if="voice.warn" class="alert alert--warn">{{ voice.warn }}</p>
    <p v-if="voice.notice" class="alert alert--ok">{{ voice.notice }}</p>

    <p v-if="voice.timelineStale" class="alert alert--warn">
      逐句时间轴**已经过期**：还有句子没定局，{{ timelineText() }} 是上一轮收口的结论。
      <template v-if="voice.hint">{{ voice.hint }}</template>
    </p>

    <PanelCard title="任务" :subtitle="taskSubtitle()">
      <template #actions>
        <StatusDot
          v-if="voice.progress"
          :tone="voice.progress.settled === voice.progress.total ? 'ok' : 'busy'"
          :label="progressText(voice.progress)"
          :pulse="polling"
        />
        <AppButton
          size="sm"
          :loading="voice.loading"
          :disabled="!voice.canLoad"
          :title="voice.problem ?? '重新拉一次逐句状态与音色清单'"
          @click="onLoad()"
        >
          刷新
        </AppButton>
        <AppButton
          size="sm"
          :disabled="!canJump"
          :title="canJump ? '去渲染面板拿这个任务号出片' : '先填一个任务号'"
          @click="ui.goTo('renders', voice.taskId)"
        >
          去渲染 →
        </AppButton>
      </template>

      <div class="form">
        <label class="f f--task">
          <span class="f__key">任务号</span>
          <input
            class="field mono"
            type="text"
            placeholder="ui-20260915-120000 或 ULID"
            :value="voice.taskId"
            @input="onTaskId($event)"
            @keyup.enter="onLoad()"
          />
        </label>
      </div>

      <p v-if="voice.problem" class="hint">{{ voice.problem }}</p>
      <p v-else-if="voice.snapshot === null" class="hint">
        填一个任务号再点「刷新」—— 任务号是稿件、配音与成片的挂钩，也是审计留痕的键。
      </p>

      <template v-if="voice.progress">
        <div class="bar" :title="`已定局 ${percent}%`">
          <div class="bar__fill" :style="{ width: `${percent}%` }" />
        </div>

        <ul class="counts">
          <li class="count"><b class="count__n">{{ voice.progress.done }}</b> 已完成</li>
          <li class="count"><b class="count__n">{{ voice.progress.synthesizing }}</b> 进行中</li>
          <li class="count count--warn"><b class="count__n">{{ voice.progress.skipped }}</b> 跳过</li>
          <li class="count count--err"><b class="count__n">{{ voice.progress.failed }}</b> 失败</li>
          <li class="count count--idle"><b class="count__n">{{ voice.progress.pending }}</b> 待配音</li>
        </ul>

        <p class="hint">
          “引擎”列写的是**念这一句时用的那台**：`cosyvoice2` = 常驻推理服务（参考音复刻），`sapi` = 本机系统语音包（常驻服务没起时的降级档）。两者都能出片，但听起来是两个人。
          跳过是降级：这一句没声音，
          片子照样出 —— 原因见下面每一行的红字。失败要人管，否则它会卡在那一句上。
          <template v-if="polling">
            还有句子在念，这一屏每 {{ VOICE_POLL_MS / 1000 }} 秒自己刷一次（念完就停）。
          </template>
        </p>
      </template>
    </PanelCard>

    <PanelCard title="逐句" :subtitle="resynthSubtitle()">
      <EmptyState
        v-if="voice.sentences.length === 0"
        title="还没有逐句配音"
        hint="先在「稿件」面板出一版稿并落库，再用这个任务号拉一次；也可以从命令行跑 `studio pipeline run <任务号> --until queued_render`。"
      />

      <div v-else class="scroll">
        <table class="table">
          <thead>
            <tr>
              <th>#</th>
              <th>角色</th>
              <th>引擎</th>
              <th>状态</th>
              <th>文本</th>
              <th>时长</th>
              <th>时间轴</th>
              <th>试听</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in voice.sentences"
              :key="row.id"
              :class="{ 'row--warn': row.degraded, 'row--err': row.tts_status === 'failed' }"
            >
              <td class="mono">{{ row.seq }}</td>
              <td class="mono">{{ row.speaker }}</td>
              <td class="mono">{{ row.tts_engine ?? "—" }}</td>
              <td>
                <StatusDot
                  :tone="statusTone(row.tts_status)"
                  :label="statusLabel(row.tts_status)"
                  :pulse="row.tts_status === 'synthesizing'"
                />
              </td>
              <td class="text" :title="row.text">
                {{ preview(row.text) }}
                <span v-if="row.degraded || row.tts_status === 'failed'" class="why">
                  原因：{{ row.tts_error ?? "库里没有记原因" }}
                </span>
              </td>
              <td class="mono">{{ durationText(row.tts_duration_ms) }}</td>
              <td class="mono">{{ spanText(row) }}</td>
              <td>
                <audio
                  v-if="row.audio_url"
                  :key="row.audio_url"
                  class="player"
                  :src="row.audio_url"
                  :title="audioTitle(row)"
                  controls
                  preload="none"
                />
                <span v-else class="hint" :title="`这一句盘上没有音频（tts_status=${row.tts_status}）`">
                  无音频
                </span>
              </td>
              <td>
                <AppButton
                  size="sm"
                  :disabled="!canResynth(row)"
                  :loading="voice.busyId === row.id"
                  :title="resynthTitle(row)"
                  @click="voice.resynth(row)"
                >
                  重配
                </AppButton>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p class="hint">
        「重配」只重念这一句：它退回待办、作业排回 voice 池，念完自己出现在这一列里。
        正被念着的句子（`synthesizing`）那颗按钮是灰的 —— 点了必被拒，而"点了没反应"
        比"按钮是灰的"难懂得多。
      </p>
    </PanelCard>

    <PanelCard title="音色映射" :subtitle="mapSubtitle()">
      <template #actions>
        <AppButton
          size="sm"
          variant="primary"
          :disabled="voice.changes.length === 0 || voice.anyBusy"
          :loading="voice.busy"
          :title="
            voice.changes.length === 0
              ? '还没有改动'
              : `改动了 ${voice.changes.join('、')} —— 会先弹一次确认框（后端先算代价）`
          "
          @click="voice.requestVoiceChange()"
        >
          提交换音色
        </AppButton>
        <AppButton
          size="sm"
          :disabled="voice.changes.length === 0 || voice.anyBusy"
          title="把下拉框退回库里现在用的映射"
          @click="voice.resetDraft()"
        >
          还原
        </AppButton>
      </template>

      <p v-if="voice.voiceNote" class="alert alert--warn">{{ voice.voiceNote }}</p>

      <EmptyState
        v-if="voice.speakers.length === 0"
        title="这条任务还没有角色"
        hint="逐句列表里有角色之后，这里才会出现每个角色的音色下拉框。"
      />

      <div v-else class="map">
        <div v-for="row in mapRows" :key="row.speaker" class="map__row">
          <span class="map__key mono">{{ row.speaker }}</span>
          <select class="field mono" :value="row.current" @change="onVoice(row.speaker, $event)">
            <option v-if="row.unknown" :value="row.current">
              {{ row.current }} · 本机找不到（换一个）
            </option>
            <option v-for="option in voice.voices" :key="option.id" :value="option.id">
              {{ voiceOptionLabel(option) }}
            </option>
          </select>
          <span class="map__was mono">
            现在用的是
            {{ row.was === "" ? "（还没有映射，配音时会落到第一个可用音色）" : row.was }}
          </span>
          <span v-if="row.unknown" class="map__diff map__diff--err">本机找不到</span>
          <span v-else-if="row.current !== row.was" class="map__diff">改</span>
        </div>
      </div>

      <div v-if="voice.confirm" class="confirm">
        <p class="confirm__title">确认换音色？</p>
        <p class="confirm__body">
          {{ voice.confirm.message }}：**将重配 {{ voice.confirm.affected }} / {{ voice.confirm.total }} 句**
          （第 {{ seqList(voice.confirm.sentences) }} 句；角色 {{ voice.confirm.speakers.join("、") }}）。
          每句都要重新合成一次，收口时还要全量重算一次时间轴。
        </p>
        <div class="confirm__actions">
          <AppButton
            size="sm"
            variant="danger"
            :loading="voice.busy"
            @click="voice.confirmVoiceChange()"
          >
            确认重配 {{ voice.confirm.affected }} 句
          </AppButton>
          <AppButton size="sm" :disabled="voice.busy" @click="voice.cancelVoiceChange()">
            算了
          </AppButton>
        </div>
      </div>

      <p class="hint">
        换音色**只**动那个角色名下的句子（别的角色的音频一个字节都不改）。正被念着的那几句
        动不了 —— 后端不去和 worker 抢同一个文件，它们会在上面的黄色横幅里列出来。改完
        时间轴是**过期**的：成片时长以下一轮配音收口的全量重算为准。
      </p>
    </PanelCard>
  </div>
</template>

<style scoped>
.voices {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
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
  flex: 0 0 260px;
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

.bar {
  height: 6px;
  margin-top: var(--space-3);
  overflow: hidden;
  background: var(--bg-raised);
  border-radius: var(--radius-sm);
}

.bar__fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}

.counts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  padding: 0;
  margin: var(--space-3) 0 0;
  list-style: none;
}

.count {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.count__n {
  margin-right: var(--space-1);
  color: var(--text-primary);
  font-size: var(--text-sm);
}

.count--warn .count__n {
  color: var(--warn);
}

.count--err .count__n {
  color: var(--error);
}

.count--idle .count__n {
  color: var(--debug);
}

.scroll {
  max-height: 520px;
  overflow: auto;
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
  vertical-align: top;
  border-bottom: 1px solid var(--border-subtle);
}

/* 跳过 / 失败的行整行染色：这两个状态在几十行里必须**一眼扫到**，而不是逐行读状态灯。 */
.row--warn {
  background: var(--warn-soft);
}

.row--err {
  background: var(--error-soft);
}

.text {
  max-width: 320px;
}

.why {
  display: block;
  margin-top: 2px;
  color: var(--error);
  font-size: var(--text-xs);
}

.player {
  height: 24px;
  max-width: 200px;
  vertical-align: middle;
}

.map {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.map__row {
  display: flex;
  gap: var(--space-3);
  align-items: center;
  font-size: var(--text-xs);
}

.map__key {
  flex: 0 0 120px;
  color: var(--text-primary);
}

.map__was {
  min-width: 0;
  color: var(--text-muted);
  word-break: break-all;
}

.map__diff {
  padding: 0 var(--space-1);
  color: var(--warn);
  border: 1px solid var(--warn);
  border-radius: var(--radius-sm);
}

.map__diff--err {
  color: var(--error);
  border-color: var(--error);
}

.confirm {
  padding: var(--space-3);
  margin-top: var(--space-3);
  border: 1px solid var(--warn);
  border-radius: var(--radius-sm);
}

.confirm__title {
  color: var(--warn);
  font-size: var(--text-sm);
}

.confirm__body {
  margin-top: var(--space-2);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  line-height: 1.7;
}

.confirm__actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
</style>
