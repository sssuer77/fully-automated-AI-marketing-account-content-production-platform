<script setup lang="ts">
// ② 选题面板（T4.3 · §04.1.3 / §04.4.5 第 2 行）。
//
// 这一屏只回答四个问题，多一个都不放：
// ① 模型给了哪些方向？—— 左侧方向卡片（含历史批次下拉与每方向选题数）；
// ② 哪些选题值得做？—— 右侧瀑布流（分数 / 钩子 / 理由 / 相似提示）；
// ③ 怎么把热点喂进来？—— 扫盘导入 或 直接粘一段（两种入口都在这里）；
// ④ 我勾的这几条进队了吗？—— 两步确认 + 逐条结果（失败带 code）。
//
// 为什么方向卡片要有"历史批次"
// ----------------------------
// 只看最近一批，就没人能回答"上周那批方向为什么一条都没选"—— 而那正是复盘选题
// 质量的入口（§04.1.2 的批次是留痕单位，不是缓存）。
//
// 为什么 WS 事件要合并
// --------------------
// `ideate` 逐方向扇出 `topic.batch_ready`（8 个方向就是 8 条），每条都重拉一次
// 选题池纯属自找抖动；这里 400ms 内的多条事件只重拉一次。

import { computed, onMounted, reactive, ref } from "vue";

import {
  DEFAULT_DIRECTION_PRIORITY,
  type DirectionItem,
  type OutlineItem,
  type TopicItem,
  type TopicPatchBody,
} from "@/api/endpoints/topics";
import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import { useChannelStream } from "@/composables/useTaskStream";
import {
  STATUS_LABELS,
  hookLabel,
  isTopicEvent,
  scoreTone,
  topicStatusTone,
  useTopicsStore,
  type HotKind,
  type TopicStatus,
} from "@/stores/topics";
import { useUiStore } from "@/stores/ui";
import type { Envelope } from "@/ws/events";

const topics = useTopicsStore();
const ui = useUiStore();

const STATUSES: TopicStatus[] = ["candidate", "selected", "queued", "rejected", "expired"];

/** WS 合并窗口：一批事件只重拉一次（见文件头注释）。 */
const COALESCE_MS = 400;

const hotText = ref("");
const hotKind = ref<HotKind>("hot");

const manualTitle = ref("");
const manualAngle = ref("");
const manualHook = ref("");
const manualScore = ref("");
const manualReason = ref("");

const subtitle = computed(() => {
  const parts = [
    `候选 ${topics.candidateCount} 条`,
    `已入队 ${topics.queuedCount} 条`,
    `方向 ${topics.directions.length} 个`,
    `勾选 ${topics.checked.length} 条`,
  ];
  if (topics.busy) parts.push("正在跑…");
  return parts.join(" · ");
});

const manualReady = computed(() => manualTitle.value.trim().length > 0);

/** 分数展示（没打分就是没打分，不显示 0.0）。 */
function formatScore(score: number | null | undefined): string {
  return score === null || score === undefined ? "-" : score.toFixed(1);
}

/** 状态的中文标签（后端的 `status` 是自由字符串，认不出就照原样显示）。 */
function statusLabel(status: string): string {
  return STATUS_LABELS[status as TopicStatus] ?? status;
}

/** 相似提示的文案（`similar_to` 是自由 JSON，取 `title` 兜底）。 */
function similarText(item: Record<string, unknown>): string {
  const title = item.title ?? item.id ?? "-";
  const score = typeof item.score === "number" ? ` ${item.score.toFixed(1)}` : "";
  return `${String(title)}${score}`;
}

/**
 * 从一条已入队的选题跳到它的稿件（T4.14）。
 *
 * 任务号是**入队时后端生成的**（`item.task_id`），所以选题面板这一端
 * 不用猜也不用拼 —— 没入队的选题就没有这颗按钮。
 */
function goScripts(taskId: string | null): void {
  if (taskId !== null) ui.goTo("scripts", taskId);
}

async function onStatusChange(event: Event): Promise<void> {
  await topics.setStatus((event.target as HTMLSelectElement).value as TopicStatus);
}

async function onBatchChange(event: Event): Promise<void> {
  const value = (event.target as HTMLSelectElement).value;
  await topics.setBatch(value === "" ? null : value);
}

async function onAddManual(): Promise<void> {
  const score = manualScore.value.trim();
  const ok = await topics.addManual({
    title: manualTitle.value.trim(),
    angle: manualAngle.value.trim(),
    hook_type: manualHook.value === "" ? null : (manualHook.value as ManualTopicBodyHook),
    score: score === "" ? null : Number(score),
    reason: manualReason.value.trim() === "" ? null : manualReason.value.trim(),
  });
  if (ok) {
    manualTitle.value = "";
    manualAngle.value = "";
    manualHook.value = "";
    manualScore.value = "";
    manualReason.value = "";
  }
}

async function onSubmitHot(): Promise<void> {
  const ok = await topics.submitHotText(hotText.value, hotKind.value);
  if (ok) hotText.value = "";
}

/** 正在行内编辑的那条（`null` = 没有）。 */
const editingId = ref<string | null>(null);
/** 正在等二次确认删除的那条（`null` = 没有）。删除不可撤销，所以要点两下。 */
const confirmingId = ref<string | null>(null);

const editDraft = reactive({ title: "", angle: "", hook: "", score: "", reason: "" });

/** 空标题 / 分数不是数字 ⇒ 保存按钮是灰的（不让用户提交一个后端一定会拒的东西）。 */
const editReady = computed(() => {
  if (editDraft.title.trim().length === 0) return false;
  const score = editDraft.score.trim();
  return score === "" || !Number.isNaN(Number(score));
});

function startEdit(item: TopicItem): void {
  confirmingId.value = null;
  editingId.value = item.id;
  editDraft.title = item.title;
  editDraft.angle = item.angle;
  editDraft.hook = item.hook_type ?? "";
  editDraft.score = item.score === null || item.score === undefined ? "" : String(item.score);
  editDraft.reason = item.reason ?? "";
}

/**
 * 保存行内编辑。
 *
 * 只把**真的变了**的字段发出去：后端按「有没有给这个字段」判要不要改，把没动的字段
 * 一起塞进去只会让返回的 `changed` 说一堆没发生的事（面板上就是一句假话）。
 * 一个字段都没动 ⇒ 直接收起来，不发请求（后端会回 422「至少要给一个要改的字段」）。
 */
async function saveEdit(item: TopicItem): Promise<void> {
  const body: TopicPatchBody = {};
  const title = editDraft.title.trim();
  const angle = editDraft.angle.trim();
  const hook = editDraft.hook === "" ? null : (editDraft.hook as ManualTopicBodyHook);
  const raw = editDraft.score.trim();
  const score = raw === "" ? null : Number(raw);
  const reason = editDraft.reason.trim() === "" ? null : editDraft.reason.trim();

  if (title !== item.title) body.title = title;
  if (angle !== item.angle) body.angle = angle;
  if (hook !== (item.hook_type ?? null)) body.hook_type = hook;
  if (score !== (item.score ?? null)) body.score = score;
  if (reason !== (item.reason ?? null)) body.reason = reason;

  if (Object.keys(body).length === 0) {
    editingId.value = null;
    return;
  }
  if (await topics.editTopic(item.id, body)) editingId.value = null;
}

async function removeTopic(item: TopicItem): Promise<void> {
  if (await topics.removeTopic(item.id)) confirmingId.value = null;
}

/** 展开着二级产物编辑器的那条（`null` = 没有）。 */
const outlineId = ref<string | null>(null);

const outlineDraft = reactive({ title: "", core: "" });

/** 标题与论点都是必填（后端也这么判：这一级只有这两样东西）。 */
const outlineReady = computed(
  () => outlineDraft.title.trim().length > 0 && outlineDraft.core.trim().length > 0,
);

/** 这条选题的二级产物（`undefined` = 还没拉过；`null` = 拉过了、确实还没有）。 */
function outlineOf(topicId: string): OutlineItem | null | undefined {
  return topics.outlines[topicId];
}

function fillOutlineDraft(topicId: string): void {
  const current = topics.outlines[topicId] ?? null;
  outlineDraft.title = current?.title ?? "";
  outlineDraft.core = current?.core_argument ?? "";
}

/** 展开 / 收起二级产物（**展开时才去拉**：人一次只看一条的标题与论点）。 */
async function toggleOutline(item: TopicItem): Promise<void> {
  if (outlineId.value === item.id) {
    outlineId.value = null;
    return;
  }
  editingId.value = null;
  confirmingId.value = null;
  outlineId.value = item.id;
  await topics.loadOutline(item.id);
  fillOutlineDraft(item.id);
}

async function onGenerateOutline(item: TopicItem): Promise<void> {
  if (await topics.generateOutline(item.id)) fillOutlineDraft(item.id);
}

async function onSaveOutline(item: TopicItem): Promise<void> {
  await topics.saveOutline(item.id, {
    title: outlineDraft.title.trim(),
    core_argument: outlineDraft.core.trim(),
  });
}

async function onClearOutline(item: TopicItem): Promise<void> {
  if (await topics.clearOutline(item.id)) fillOutlineDraft(item.id);
}

// ── 方向：手写 / 编辑 / 删除 / 只跑这一个 ──────────────────────
//
// 这一栏的三件事**一次 LLM 都不调**（手写、改名、删除），所以它们不走 `busy`，
// 各走各的按行 loading —— 点一下就该有结果，不该把整块面板冻住。

/** 每方向生成几条候选的可选档位（后端上限 20）。 */
const PER_DIRECTION_CHOICES = [3, 4, 5, 6, 8, 10];

const dirTitle = ref("");
const dirWhy = ref("");
const dirReady = computed(() => dirTitle.value.trim().length > 0);

const dirEditId = ref<string | null>(null);
const dirEditDraft = reactive({ title: "", rationale: "" });
const dirEditReady = computed(() => dirEditDraft.title.trim().length > 0);
const dirConfirmId = ref<string | null>(null);

async function onAddDirection(): Promise<void> {
  const ok = await topics.addDirection({
    title: dirTitle.value.trim(),
    rationale: dirWhy.value.trim(),
    // 契约把"有默认值的字段"渲染成必填 ⇒ 显式给一次（后端默认也是 100）
    priority: DEFAULT_DIRECTION_PRIORITY,
  });
  if (ok) {
    dirTitle.value = "";
    dirWhy.value = "";
  }
}

function startDirEdit(item: DirectionItem): void {
  dirEditId.value = item.id;
  dirConfirmId.value = null;
  dirEditDraft.title = item.title;
  dirEditDraft.rationale = item.rationale;
}

/**
 * 保存方向的行内编辑。
 *
 * 与选题那一条同一个取舍：只把**真的变了**的字段发出去。一个字段都没动就直接收起来
 * —— 后端会把"什么都没改"当成一次成功但不留痕的操作，而面板上多发一次请求只是噪音。
 */
async function saveDirEdit(item: DirectionItem): Promise<void> {
  const title = dirEditDraft.title.trim();
  const rationale = dirEditDraft.rationale.trim();
  const body: { title?: string; rationale?: string } = {};
  if (title !== item.title) body.title = title;
  if (rationale !== item.rationale) body.rationale = rationale;

  if (Object.keys(body).length === 0) {
    dirEditId.value = null;
    return;
  }
  if (await topics.editDirection(item.id, body)) dirEditId.value = null;
}

async function confirmRemoveDirection(item: DirectionItem): Promise<void> {
  if (await topics.removeDirection(item.id)) dirConfirmId.value = null;
}

async function onIdeateOne(item: DirectionItem): Promise<void> {
  await topics.ideateOne(item.id, item.title);
}

async function onPerDirectionChange(event: Event): Promise<void> {
  topics.setPerDirection(Number((event.target as HTMLSelectElement).value));
}

/**
 * 生成完整文案并送审（长任务）。
 *
 * 成功后**自动跳到稿件面板**并带上任务号 —— "移交审核"这件事的下一站在那里，
 * 而这一步会把选题推出候选视图（`candidate → queued`），留在原地的话它当场就消失了。
 */
async function onDraftForReview(item: TopicItem): Promise<void> {
  const taskId = await topics.draftForReview(item.id);
  if (taskId !== null) ui.goTo("scripts", taskId);
}

/** 钩子下拉的取值（与后端 `hook_type` 的 Literal 逐字一致）。 */
type ManualTopicBodyHook = "conflict" | "suspense" | "contrast" | "number" | "other";

let pending: ReturnType<typeof setTimeout> | null = null;

function onTopicEvent(envelope: Envelope): void {
  if (!isTopicEvent(envelope.data.kind)) return;
  if (pending !== null) return;
  pending = setTimeout(() => {
    pending = null;
    void topics.refresh();
  }, COALESCE_MS);
}

onMounted(async () => {
  await topics.refresh();
});

useChannelStream("topics", onTopicEvent);
</script>

<template>
  <PanelCard fill dense title="选题 · 方向与候选" :subtitle="subtitle">
    <template #actions>
      <select class="field mono" :value="topics.status" title="选题池状态" @change="onStatusChange">
        <option v-for="item in STATUSES" :key="item" :value="item">
          {{ STATUS_LABELS[item] }}（{{ topics.counts[item] ?? 0 }}）
        </option>
      </select>
      <select
        class="field mono"
        :value="topics.batchId ?? ''"
        title="历史批次（新到旧）"
        @change="onBatchChange"
      >
        <option value="">最近一批</option>
        <option v-for="item in topics.batches" :key="item" :value="item">{{ item }}</option>
      </select>
      <AppButton size="sm" :loading="topics.loading" @click="topics.refresh()">刷新</AppButton>
      <select
        class="field mono"
        :value="topics.perDirection"
        title="每方向生成几条候选"
        @change="onPerDirectionChange"
      >
        <option v-for="n in PER_DIRECTION_CHOICES" :key="n" :value="n">每方向 {{ n }} 条</option>
      </select>
      <AppButton size="sm" :disabled="topics.busy" @click="topics.analyze()">触发分析</AppButton>
      <AppButton variant="primary" size="sm" :disabled="topics.busy" @click="topics.ideate()">
        生成选题
      </AppButton>
    </template>

    <div class="wrap">
      <p v-if="topics.error ?? topics.loadError" class="notice notice--error">
        {{ topics.error ?? topics.loadError }}
      </p>
      <p v-else-if="topics.notice" class="notice">{{ topics.notice }}</p>
      <p v-else class="notice notice--muted">
        流水线：触发分析（5–8 个方向）⇒ 生成选题（每方向 3–5 条）⇒ 勾选入队。长任务同一时刻只跑一个。
      </p>

      <div class="split">
        <section class="dirs">
          <div class="dirnew">
            <div class="tool__row">
              <input
                v-model="dirTitle"
                class="field field--wide"
                placeholder="手写一个方向（必填，≤120 字）"
              />
            </div>
            <div class="tool__row">
              <input
                v-model="dirWhy"
                class="field field--wide"
                placeholder="为什么做这个方向（选填）"
              />
              <AppButton
                size="sm"
                :disabled="!dirReady || topics.directionBusy !== null"
                :loading="topics.directionBusy === 'new'"
                @click="onAddDirection()"
              >
                加进这一批
              </AppButton>
            </div>
            <span class="tool__meta">
              手写方向**不经模型**：直接落在当前批次，与模型产的那批排在一起（可改可删）。
            </span>
          </div>

          <EmptyState
            v-if="topics.directions.length === 0"
            title="还没有内容方向"
            hint="上面可以直接手写一个；也可以点右上角「触发分析」，它会先扫 data/hot 与 data/feedback 再问 Planner。"
          />
          <ul v-else class="dirs__list">
            <li v-for="group in topics.groups" :key="group.direction?.id ?? 'orphan'" class="dir">
              <header class="dir__head">
                <span class="dir__seq mono">
                  {{ group.direction === null ? "—" : `#${group.direction.seq}` }}
                </span>
                <span class="dir__title">
                  {{ group.direction === null ? "未归入当前批次" : group.direction.title }}
                </span>
              </header>
              <p v-if="group.direction" class="dir__why">{{ group.direction.rationale }}</p>
              <p class="dir__meta mono">
                选题 {{ group.topics.length }} 条 ·
                已入队 {{ group.direction?.selected_count ?? 0 }} 条 ·
                优先级 {{ group.direction?.priority ?? "-" }}
              </p>
              <p v-if="(group.direction?.risk_flags.length ?? 0) > 0" class="dir__risk mono">
                风险：{{ group.direction?.risk_flags.join("、") }}
              </p>

              <template v-if="group.direction">
                <div v-if="dirEditId === group.direction.id" class="dir__edit">
                  <input
                    v-model="dirEditDraft.title"
                    class="field field--wide"
                    placeholder="方向标题"
                  />
                  <input
                    v-model="dirEditDraft.rationale"
                    class="field field--wide"
                    placeholder="为什么做这个方向"
                  />
                  <div class="tool__row">
                    <AppButton
                      size="sm"
                      :disabled="!dirEditReady || topics.directionBusy !== null"
                      @click="saveDirEdit(group.direction)"
                    >
                      保存
                    </AppButton>
                    <AppButton size="sm" @click="dirEditId = null">取消</AppButton>
                  </div>
                </div>

                <div v-else-if="dirConfirmId === group.direction.id" class="dir__edit">
                  <p class="dir__warn">
                    删掉《{{ group.direction.title }}》，它下面的 {{ group.topics.length }}
                    条候选一起走（不可撤销）。
                  </p>
                  <div class="tool__row">
                    <AppButton
                      size="sm"
                      :disabled="topics.directionBusy !== null"
                      @click="confirmRemoveDirection(group.direction)"
                    >
                      确认删除方向
                    </AppButton>
                    <AppButton size="sm" @click="dirConfirmId = null">取消</AppButton>
                  </div>
                </div>

                <div v-else class="dir__acts">
                  <AppButton
                    size="sm"
                    :disabled="topics.busy"
                    :loading="topics.directionBusy === group.direction.id"
                    :title="`只跑《${group.direction.title}》，产出 ${topics.perDirection} 条候选`"
                    @click="onIdeateOne(group.direction)"
                  >
                    生成 {{ topics.perDirection }} 条候选
                  </AppButton>
                  <AppButton size="sm" @click="startDirEdit(group.direction)">编辑</AppButton>
                  <AppButton size="sm" @click="dirConfirmId = group.direction.id">删除</AppButton>
                </div>
              </template>
            </li>
          </ul>
        </section>

        <section class="pool">
          <EmptyState
            v-if="topics.topics.length === 0"
            :title="`没有${STATUS_LABELS[topics.status]}的选题`"
            hint="点「生成选题」按方向产出；也可以自己加一条（右下角「人工加选题」）。"
          />
          <template v-else>
            <article v-for="group in topics.groups" :key="group.direction?.id ?? 'orphan'" class="grp">
              <h3 class="grp__title">
                {{ group.direction === null ? "未归入当前批次" : group.direction.title }}
                <span class="grp__count mono">{{ group.topics.length }} 条</span>
              </h3>
              <ul class="cards">
                <li
                  v-for="item in group.topics"
                  :key="item.id"
                  class="card"
                  :class="{ 'card--checked': topics.checked.includes(item.id) }"
                >
                  <input
                    type="checkbox"
                    :checked="topics.checked.includes(item.id)"
                    :title="`勾选 ${item.title}`"
                    @change="topics.toggleChecked(item.id)"
                  />
                  <div class="card__body">
                    <div class="card__head">
                      <span class="card__title">{{ item.title }}</span>
                      <span class="card__score mono" :class="`tone--${scoreTone(item.score)}`">
                        {{ formatScore(item.score) }}
                      </span>
                      <StatusDot
                        :tone="topicStatusTone(item.status)"
                        :label="statusLabel(item.status)"
                      />
                      <span class="card__hook mono">{{ hookLabel(item.hook_type) }}</span>
                    </div>
                    <p v-if="item.angle" class="card__angle">{{ item.angle }}</p>
                    <p v-if="item.reason" class="card__reason">{{ item.reason }}</p>
                    <p v-if="item.similar_to.length > 0" class="card__similar">
                      库里已有很像的：{{ item.similar_to.map(similarText).join("；") }}
                    </p>
                    <p v-if="outlineOf(item.id)" class="card__outline">
                      二级：《{{ outlineOf(item.id)?.title }}》 ·
                      论点：{{ outlineOf(item.id)?.core_argument }}
                    </p>
                    <div v-if="outlineId === item.id" class="outline">
                      <input
                        v-model="outlineDraft.title"
                        class="field field--wide"
                        placeholder="视频标题（≤60 字）"
                      />
                      <input
                        v-model="outlineDraft.core"
                        class="field field--wide"
                        placeholder="核心论点（一句话，≤200 字）"
                      />
                      <div class="tool__row">
                        <AppButton
                          size="sm"
                          :disabled="!outlineReady || topics.outlineBusy === item.id"
                          @click="onSaveOutline(item)"
                        >
                          保存
                        </AppButton>
                        <AppButton
                          size="sm"
                          :disabled="topics.outlineBusy === item.id"
                          @click="onGenerateOutline(item)"
                        >
                          让模型定
                        </AppButton>
                        <AppButton
                          size="sm"
                          :disabled="topics.outlineBusy === item.id"
                          @click="onClearOutline(item)"
                        >
                          清空
                        </AppButton>
                        <span class="tool__meta">三级会锁定这个标题，并按论点展开。</span>
                      </div>
                    </div>
                    <div v-if="editingId === item.id" class="edit">
                      <div class="tool__row">
                        <input v-model="editDraft.title" class="field field--wide" placeholder="标题（≤50 字）" />
                        <input v-model="editDraft.angle" class="field field--wide" placeholder="角度" />
                      </div>
                      <div class="tool__row">
                        <select v-model="editDraft.hook" class="field mono">
                          <option value="">钩子未标注</option>
                          <option value="conflict">冲突</option>
                          <option value="suspense">悬念</option>
                          <option value="contrast">反差</option>
                          <option value="number">数字</option>
                          <option value="other">其他</option>
                        </select>
                        <input v-model="editDraft.score" class="field field--num mono" placeholder="分数" />
                        <input v-model="editDraft.reason" class="field field--wide" placeholder="理由" />
                      </div>
                      <div class="tool__row">
                        <AppButton
                          size="sm"
                          :disabled="!editReady || topics.saving"
                          @click="saveEdit(item)"
                        >
                          保存
                        </AppButton>
                        <AppButton size="sm" @click="editingId = null">取消</AppButton>
                        <span class="tool__meta">改标题会重算去重指纹与相似提示。</span>
                      </div>
                    </div>
                    <div v-else class="card__acts">
                      <AppButton
                        variant="primary"
                        size="sm"
                        :disabled="topics.draftBusy !== null || topics.busy"
                        :loading="topics.draftBusy === item.id"
                        :title="`给《${item.title}》写一版完整文案并移交审核（花两次 LLM）`"
                        @click="onDraftForReview(item)"
                      >
                        生成文案并送审
                      </AppButton>
                      <AppButton
                        size="sm"
                        :title="`定《${item.title}》的标题与核心论点`"
                        @click="toggleOutline(item)"
                      >
                        标题/论点
                      </AppButton>
                      <AppButton size="sm" :title="`改《${item.title}》`" @click="startEdit(item)">
                        编辑
                      </AppButton>
                      <template v-if="confirmingId === item.id">
                        <span class="tool__meta">删掉就没了（不可撤销）</span>
                        <AppButton size="sm" :disabled="topics.saving" @click="removeTopic(item)">
                          确认删除
                        </AppButton>
                        <AppButton size="sm" @click="confirmingId = null">取消</AppButton>
                      </template>
                      <AppButton
                        v-else
                        size="sm"
                        :title="`删掉《${item.title}》`"
                        @click="confirmingId = item.id"
                      >
                        删除
                      </AppButton>
                    </div>
                    <div v-if="item.task_id" class="card__task">
                      <span class="mono">→ {{ item.task_id }}</span>
                      <AppButton
                        size="sm"
                        :title="`去稿件面板看 ${item.task_id} 这一版稿`"
                        @click="goScripts(item.task_id)"
                      >
                        去稿件 →
                      </AppButton>
                    </div>
                  </div>
                </li>
              </ul>
            </article>
          </template>
        </section>
      </div>

      <footer class="bar">
        <AppButton
          size="sm"
          :disabled="!topics.hasSelection"
          @click="topics.requestSelect()"
        >
          勾选入队（{{ topics.checked.length }}）
        </AppButton>
        <AppButton v-if="topics.hasSelection" size="sm" @click="topics.clearChecked()">
          清空勾选
        </AppButton>
        <label class="bar__check mono" title="入队后立刻跑一次写稿（等价 CLI 的 studio script draft）">
          <input
            type="checkbox"
            :checked="topics.draftNow"
            @change="topics.setDraftNow(($event.target as HTMLInputElement).checked)"
          />
          顺手写稿
        </label>
        <span class="bar__spacer" />
        <span class="bar__hint mono">入队 = 建任务（幂等键 topic:&lt;id&gt;），可重复点</span>
      </footer>

      <div v-if="topics.pendingSelect" class="confirm">
        <p class="confirm__title">
          将入队以下 {{ topics.pendingSelect.length }} 条（逐条建任务，一条失败不影响其余）：
        </p>
        <ul class="confirm__list">
          <li v-for="item in topics.pendingSelect" :key="item.id" class="mono">
            {{ item.title }} · {{ formatScore(item.score) }} · {{ hookLabel(item.hook_type) }}
          </li>
        </ul>
        <div class="confirm__actions">
          <AppButton
            variant="primary"
            size="sm"
            :loading="topics.busy"
            @click="topics.confirmSelect()"
          >
            确认入队
          </AppButton>
          <AppButton size="sm" @click="topics.cancelSelect()">取消</AppButton>
        </div>
      </div>

      <ul v-if="topics.failures.length > 0" class="failures">
        <li v-for="item in topics.failures" :key="item.topic_id" class="failures__item">
          <span class="mono">{{ item.topic_id }}</span>
          <span class="failures__code mono">{{ item.code }}</span>
          <span>{{ item.message }}</span>
          <span v-if="item.remediation" class="failures__fix">{{ item.remediation }}</span>
        </li>
      </ul>

      <ul v-if="topics.lastIdeate && !topics.lastIdeate.ok" class="failures">
        <li v-for="item in topics.lastIdeate.outcomes" :key="item.direction_id" class="failures__item">
          <span class="mono">{{ item.direction_id }}</span>
          <span class="failures__code mono">{{ item.error_code ?? "IDEATE_FAILED" }}</span>
          <span>{{ item.title }}</span>
          <span v-if="item.error_message" class="failures__fix">{{ item.error_message }}</span>
        </li>
      </ul>

      <div class="tools">
        <details class="tool">
          <summary>输入源 · 扫盘 / 粘贴</summary>
          <div class="tool__body">
            <div class="tool__row">
              <AppButton size="sm" :disabled="topics.busy" @click="topics.scanHot()">
                扫盘导入 data/hot + data/feedback
              </AppButton>
              <span v-if="topics.lastImport" class="tool__meta mono">
                {{ topics.lastImport.files.length }} 个文件 · 解析 {{ topics.lastImport.parsed }} ·
                入库 {{ topics.lastImport.inserted }} · 坏行 {{ topics.lastImport.bad }}
              </span>
            </div>
            <ul v-if="topics.lastImport && topics.lastImport.issues.length > 0" class="issues">
              <li v-for="(issue, index) in topics.lastImport.issues" :key="index">{{ issue }}</li>
            </ul>
            <select v-model="hotKind" class="field mono">
              <option value="hot">热点（标题|热度|平台）</option>
              <option value="feedback">反馈（自由文本也能兜住）</option>
            </select>
            <textarea
              v-model="hotText"
              class="area"
              rows="4"
              placeholder="每行一条，例如：某人某件事后续|9800|抖音"
            />
            <div class="tool__row">
              <AppButton size="sm" :disabled="topics.busy" @click="onSubmitHot()">提交并导入</AppButton>
              <span class="tool__meta">文件名由服务端生成（webui-&lt;ulid&gt;.md），客户端不参与命名。</span>
            </div>
          </div>
        </details>

        <details class="tool">
          <summary>人工加选题</summary>
          <div class="tool__body">
            <div class="tool__row">
              <input v-model="manualTitle" class="field field--wide" placeholder="标题（必填，≤50 字）" />
              <select v-model="manualHook" class="field mono">
                <option value="">钩子未标注</option>
                <option value="conflict">冲突</option>
                <option value="suspense">悬念</option>
                <option value="contrast">反差</option>
                <option value="number">数字</option>
                <option value="other">其他</option>
              </select>
              <input v-model="manualScore" class="field field--num mono" placeholder="自评分" />
            </div>
            <input v-model="manualAngle" class="field field--wide" placeholder="角度差异化说明（选填）" />
            <input v-model="manualReason" class="field field--wide" placeholder="评分理由（选填）" />
            <div class="tool__row">
              <AppButton size="sm" :disabled="!manualReady || topics.busy" @click="onAddManual()">
                入库
              </AppButton>
              <span class="tool__meta">
                与库里很像的只提示、不拦 —— 模型产出的重复是噪音，人加的重复是明确意图。
              </span>
            </div>
            <p v-if="topics.manual && topics.manual.similar_to.length > 0" class="tool__meta">
              相似：{{ topics.manual.similar_to.map(similarText).join("；") }}
            </p>
          </div>
        </details>
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
  grid-template-columns: 320px 1fr;
  gap: var(--space-3);
  min-height: 0;
}

.dirs,
.pool {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-height: 0;
  overflow-y: auto;
}

.dirs {
  padding-right: var(--space-2);
  border-right: 1px solid var(--border-subtle);
}

.dirs__list,
.cards,
.failures,
.issues {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
}

.dir {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.dir__head {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
}

.dir__seq {
  color: var(--accent);
  font-size: var(--text-xs);
}

.dir__title {
  font-size: var(--text-md);
}

.dir__why {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.dir__meta,
.dir__risk {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.dir__risk {
  color: var(--warn);
}

.dir__warn {
  color: var(--warn);
  font-size: var(--text-xs);
}

.dir__edit,
.dirnew {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-sm);
}

.dir__acts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
  margin-top: 2px;
}

.grp {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.grp__title {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
  color: var(--text-secondary);
  font-size: var(--text-sm);
}

.grp__count {
  color: var(--text-muted);
}

.card__outline {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.outline {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.card__acts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}

.edit {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  background: var(--bg-panel);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
}

.card {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-2);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.card--checked {
  border-color: var(--accent);
  box-shadow: inset 2px 0 0 var(--accent);
}

.card__body {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.card__head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}

.card__title {
  font-size: var(--text-md);
}

.card__score {
  font-size: var(--text-sm);
}

.card__hook {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.card__angle {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.card__reason {
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.card__similar {
  color: var(--warn);
  font-size: var(--text-xs);
}

.card__task {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  color: var(--text-muted);
}

.tone--ok {
  color: var(--ok, var(--accent));
}

.tone--warn {
  color: var(--warn);
}

.tone--error {
  color: var(--error);
}

.tone--idle {
  color: var(--text-muted);
}

.bar {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  padding-top: var(--space-2);
  border-top: 1px solid var(--border-subtle);
}

.bar__check {
  display: flex;
  gap: var(--space-1);
  align-items: center;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}

.bar__spacer {
  flex: 1;
}

.bar__hint {
  color: var(--text-muted);
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

.tools {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-2);
}

.tool {
  padding: var(--space-2);
  background: var(--bg-raised);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.tool > summary {
  color: var(--text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
}

.tool__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-2);
}

.tool__row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}

.tool__meta {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.issues {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.area {
  width: 100%;
  padding: var(--space-2);
  color: var(--text-primary);
  font: inherit;
  font-size: var(--text-xs);
  resize: vertical;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
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

.field--wide {
  flex: 1;
  min-width: 160px;
}

.field--num {
  width: 72px;
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
