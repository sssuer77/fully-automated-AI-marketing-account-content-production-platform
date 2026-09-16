<script setup lang="ts">
// 合成配置面板（T4.7 · §04.2.8 / §04.5.9）。
//
// 这一屏只回答三个问题，多一个都不放：
// ① 现在这一档是什么参数？—— 分辨率 / 帧率 / 质量（CRF 还是 CQ）/ 平台 / 谁是默认档；
// ② 水印与字幕长什么样、会不会出问题？—— 水印位置与边距、字幕字号与描边，**PNG 在不在盘上**；
// ③ 改完存哪、会不会把配置写坏？—— 保存前强制校验（不过一个字节都不写）+ 并发指纹。
//
// 三条必须写在面板上的话
// ----------------------
// 1. **只影响后续渲染**：已入队 / 在跑的任务不中断、不重跑。不写这一句，用户会以为改完
//    分辨率"当前那条片子也跟着变了"，然后盯着一部永远不会变的片子等。
// 2. **水印是可选装饰**：PNG 不在盘上 ⇒ 这次出片**不贴水印、照样出片**（BGM 空着同理）。
//    所以它是一条横幅，把"少了什么"说在明处 —— 但**不能**说成"出不了片"。
// 3. **一期只有表单**：拖拽定位水印与三层模板树（Video -> Scene -> Component）随 C13 延后二期
//    （R17）。说清楚，用户才不会在面板上找一个不存在的拖拽框。
//
// 为什么表单是 `:value` + `@input` 而不是 `v-model`
// ------------------------------------------------
// `draft` 可能是 `null`（配置读不出来）。`v-model` 在可空嵌套对象上要么报类型错、要么被迫
// 写一堆 `!`；显式的事件处理把"没有草稿就什么都不做"写成一行判断。

import { computed, onMounted } from "vue";

import AppButton from "@/components/AppButton.vue";
import EmptyState from "@/components/EmptyState.vue";
import PanelCard from "@/components/PanelCard.vue";
import StatusDot from "@/components/StatusDot.vue";
import {
  describeBound,
  describeWatermark,
  qualityLabel,
  useOutputsStore,
  watermarkTone,
} from "@/stores/outputs";

const outputs = useOutputsStore();

// 进面板即接线：订阅 WS 的 `logs` 通道（只认 source === "outputs"）+ 一次全量拉取。
onMounted(() => {
  outputs.start();
});

const form = computed(() => outputs.draft);
const errors = computed(() => outputs.visibleFieldErrors);

/** 默认档的画布宽 × 水印占比 ⇒ 像素宽（与后端 `round(width_ratio * width)` 同一算法）。 */
const watermarkPx = computed<number | null>(() => {
  const draft = form.value;
  if (draft === null || draft.watermark === null) return null;
  const profile = draft.profiles[draft.default_profile];
  if (profile === undefined) return null;
  return Math.round(profile.width * draft.watermark.width_ratio);
});

function parseInt0(input: Event): number | null {
  const raw = (input.target as HTMLInputElement).value.trim();
  if (raw === "") return null;
  const parsed = Number.parseInt(raw, 10);
  return Number.isNaN(parsed) ? null : parsed;
}

function parseFloat0(input: Event): number | null {
  const raw = (input.target as HTMLInputElement).value.trim();
  if (raw === "") return null;
  const parsed = Number.parseFloat(raw);
  return Number.isNaN(parsed) ? null : parsed;
}

// 输入框清空时**保留原值**：让"空"变成 NaN 存进草稿，只会在提交时给出一条看不懂的 422，
// 而用户真正想做的往往是"全选重打"。

function onProfileInt(name: string, key: "width" | "height" | "fps" | "quality", event: Event): void {
  const draft = form.value;
  if (draft === null) return;
  const profile = draft.profiles[name];
  if (profile === undefined) return;
  const parsed = parseInt0(event);
  if (parsed === null) return;
  profile[key] = parsed;
}

function onDefaultProfile(event: Event): void {
  const draft = form.value;
  if (draft === null) return;
  draft.default_profile = (event.target as HTMLSelectElement).value;
}

function onPosition(event: Event): void {
  const draft = form.value;
  if (draft === null || draft.watermark === null) return;
  draft.watermark.position = (event.target as HTMLSelectElement).value;
}

function onWatermarkInt(key: "margin_x" | "margin_y", event: Event): void {
  const draft = form.value;
  if (draft === null || draft.watermark === null) return;
  const parsed = parseInt0(event);
  if (parsed === null) return;
  draft.watermark[key] = parsed;
}

function onWatermarkFloat(key: "width_ratio" | "opacity", event: Event): void {
  const draft = form.value;
  if (draft === null || draft.watermark === null) return;
  const parsed = parseFloat0(event);
  if (parsed === null) return;
  draft.watermark[key] = parsed;
}

function onSubtitleInt(key: "font_size" | "outline" | "max_chars_per_line", event: Event): void {
  const draft = form.value;
  if (draft === null || draft.subtitle === null) return;
  const parsed = parseInt0(event);
  if (parsed === null) return;
  draft.subtitle[key] = parsed;
}
</script>

<template>
  <div class="outputs">
    <p v-if="outputs.configError" class="alert alert--error">
      `config/outputs.yaml` 读不出来：{{ outputs.configError }}
      （下面显示的是**上一份可用**的配置。改好文件或在这里保存一次即可 —— 这份面板存在的意义
      就是把它修回来，所以读取失败**不**返回错误页）
    </p>

    <p v-if="outputs.watermarkMissing" class="alert alert--warn">
      **水印 PNG 不在盘上**：{{ outputs.watermark?.path }} —— 出片**照常**，只是这一版不带水印
      （水印是可选装饰，不阻塞渲染）。想要水印就把图放到这个路径，或改成盘上已有的那张。
    </p>

    <p v-if="outputs.conflict" class="alert alert--warn">
      盘上那份在你编辑期间被改过（或另一个标签页先保存了）⇒ **本次一个字节都没写**。
      点「重新载入」拿最新的那一份，再决定要不要重新改。
    </p>

    <p v-else-if="outputs.draftStale" class="alert alert--warn">
      底稿在你编辑期间变过（别处保存了一次，或文件被直接改了）。你手里的改动**没有被丢掉**，
      保存时会覆盖对应字段；想放弃改动就点「放弃编辑」。
    </p>

    <p v-if="outputs.loadError" class="alert alert--error">
      配置拉取失败：{{ outputs.loadError }}（下面显示的是上一次拿到的内容）
    </p>

    <p v-if="outputs.error" class="alert alert--error">动作失败：{{ outputs.error }}</p>
    <p v-else-if="outputs.notice" class="alert alert--ok">{{ outputs.notice }}</p>

    <PanelCard
      title="合成配置"
      :subtitle="
        outputs.snapshot === null
          ? '还没有读到配置'
          : `v${outputs.version} · sha256 ${outputs.sha256.slice(0, 12)} · 加载于 ${outputs.loadedAt}`
      "
    >
      <template #actions>
        <AppButton
          size="sm"
          variant="primary"
          :disabled="!outputs.canSave"
          :loading="outputs.busy"
          :title="outputs.dirty ? '校验通过后写盘（写坏不了）' : '还没有改动'"
          @click="outputs.save()"
        >
          保存
        </AppButton>
        <AppButton size="sm" :disabled="!outputs.dirty" @click="outputs.resetDraft()">
          放弃编辑
        </AppButton>
        <AppButton size="sm" :loading="outputs.loading" @click="outputs.refresh({ resetDraft: true })">
          重新载入
        </AppButton>
      </template>

      <div class="rows">
        <div class="row">
          <span class="row__key">文件</span>
          <span class="row__val mono">{{ outputs.configPath || "-" }}</span>
        </div>
        <div class="row">
          <span class="row__key">默认档</span>
          <span class="row__val mono">{{ outputs.defaultProfile || "-" }}</span>
        </div>
        <div class="row">
          <span class="row__key">状态</span>
          <span class="row__val">
            <StatusDot
              :tone="outputs.stale ? 'error' : 'ok'"
              :label="outputs.stale ? '磁盘上那份已损坏（在用上一份好的）' : '正常'"
            />
          </span>
        </div>
        <div class="row">
          <span class="row__key">表单</span>
          <span class="row__val">
            {{ outputs.dirty ? "有未保存的改动" : "与服务端一致" }}
            <span v-if="errors['<root>']" class="row__err">{{ errors["<root>"] }}</span>
          </span>
        </div>
      </div>

      <p class="hint">
        这份 YAML 是**唯一真相**：渲染编译器与发布前校验读的都是它，面板不写数据库。
        保存会**先校验、后落盘、再回读**（校验不过一个字节都不写），改动同时进 `audit_ops` 留痕。
        **只影响后续渲染** —— 已入队、正在跑的任务不中断、不重跑。
      </p>
    </PanelCard>

    <PanelCard title="输出 profile" :subtitle="`共 ${outputs.profiles.length} 档（一档对应一类平台画布）`">
      <EmptyState
        v-if="form === null || outputs.profiles.length === 0"
        title="没有可编辑的 profile"
        hint="`config/outputs.yaml` 的 `profiles:` 段至少要有一档；把文件改好再回到这一屏。"
      />

      <ul v-else class="list">
        <li v-for="profile in outputs.profiles" :key="profile.name" class="item">
          <div class="item__main">
            <label class="check">
              <input
                type="radio"
                name="default_profile"
                :checked="form.default_profile === profile.name"
                @change="onDefaultProfile($event)"
              />
              <span class="item__title mono">{{ profile.name }}</span>
            </label>
            <span class="item__sub">
              {{ profile.vcodec }} · {{ profile.platforms.join(" / ") || "未标平台" }}
            </span>
          </div>

          <div class="grid">
            <label class="f f--num">
              <span class="f__key">宽</span>
              <input
                class="field mono"
                type="number"
                :value="form.profiles[profile.name]?.width ?? profile.width"
                @input="onProfileInt(profile.name, 'width', $event)"
              />
            </label>
            <label class="f f--num">
              <span class="f__key">高</span>
              <input
                class="field mono"
                type="number"
                :value="form.profiles[profile.name]?.height ?? profile.height"
                @input="onProfileInt(profile.name, 'height', $event)"
              />
            </label>
            <label class="f f--num">
              <span class="f__key">帧率</span>
              <input
                class="field mono"
                type="number"
                :value="form.profiles[profile.name]?.fps ?? profile.fps"
                @input="onProfileInt(profile.name, 'fps', $event)"
              />
            </label>
            <label class="f f--num">
              <span class="f__key">{{ qualityLabel(profile) }}</span>
              <input
                class="field mono"
                type="number"
                :value="form.profiles[profile.name]?.quality ?? profile.quality"
                @input="onProfileInt(profile.name, 'quality', $event)"
              />
            </label>
          </div>

          <div class="errs">
            <span v-if="errors[`profiles.${profile.name}.width`]" class="f__err">
              {{ errors[`profiles.${profile.name}.width`] }}
            </span>
            <span v-if="errors[`profiles.${profile.name}.height`]" class="f__err">
              {{ errors[`profiles.${profile.name}.height`] }}
            </span>
            <span v-if="errors[`profiles.${profile.name}.fps`]" class="f__err">
              {{ errors[`profiles.${profile.name}.fps`] }}
            </span>
            <span v-if="errors[`profiles.${profile.name}.quality`]" class="f__err">
              {{ errors[`profiles.${profile.name}.quality`] }}
            </span>
          </div>
        </li>
      </ul>

      <p v-if="outputs.limits" class="hint">
        宽高必须是**偶数**（yuv420p 色度对齐）：{{ describeBound(outputs.limits.profile.width) }}；
        帧率 {{ describeBound(outputs.limits.profile.fps) }}；
        质量 {{ describeBound(outputs.limits.profile.crf) }}。
        质量框的标签按这一档的编码器来 —— `libx264` 是 CRF、`h264_nvenc` 是 CQ，填的是同一个数字。
      </p>

      <p class="hint">
        三层模板（Video -> Scene -> Component）的**结构编辑**与**拖拽定位**随 C13 延后二期（R17）：
        一期这一屏只做上面这些**标量**参数的编辑。模板结构现在仍然按 `templates/&lt;id&gt;/` 下的
        YAML 走，改结构请直接改文件。
      </p>
    </PanelCard>

    <PanelCard
      title="固定水印"
      :subtitle="outputs.watermark === null ? '配置里没有 watermark 段' : outputs.watermark.path"
    >
      <EmptyState
        v-if="form === null || form.watermark === null"
        title="没有可编辑的水印"
        hint="水印是**可选装饰**：`config/outputs.yaml` 的 `watermark:` 段不在也能出片（只是不贴水印）。"
      />

      <div v-else class="form">
        <div class="f f--full f--row">
          <label class="f f--num">
            <span class="f__key">位置</span>
            <select class="field" :value="form.watermark.position" @change="onPosition($event)">
              <option v-for="option in outputs.limits?.watermark.positions ?? []" :key="option" :value="option">
                {{ option }}
              </option>
            </select>
            <span v-if="errors['watermark.position']" class="f__err">
              {{ errors["watermark.position"] }}
            </span>
          </label>

          <label class="f f--num">
            <span class="f__key">水平边距（偶数）</span>
            <input
              class="field mono"
              type="number"
              :value="form.watermark.margin_x"
              @input="onWatermarkInt('margin_x', $event)"
            />
            <span v-if="errors['watermark.margin_x']" class="f__err">
              {{ errors["watermark.margin_x"] }}
            </span>
          </label>

          <label class="f f--num">
            <span class="f__key">垂直边距（偶数）</span>
            <input
              class="field mono"
              type="number"
              :value="form.watermark.margin_y"
              @input="onWatermarkInt('margin_y', $event)"
            />
            <span v-if="errors['watermark.margin_y']" class="f__err">
              {{ errors["watermark.margin_y"] }}
            </span>
          </label>
        </div>

        <div class="f f--full f--row">
          <label class="f f--num">
            <span class="f__key">宽度占比（0-0.25）</span>
            <input
              class="field mono"
              type="number"
              step="0.01"
              :value="form.watermark.width_ratio"
              @input="onWatermarkFloat('width_ratio', $event)"
            />
            <span v-if="errors['watermark.width_ratio']" class="f__err">
              {{ errors["watermark.width_ratio"] }}
            </span>
          </label>

          <label class="f f--num">
            <span class="f__key">不透明度（0-1）</span>
            <input
              class="field mono"
              type="number"
              step="0.05"
              :value="form.watermark.opacity"
              @input="onWatermarkFloat('opacity', $event)"
            />
            <span v-if="errors['watermark.opacity']" class="f__err">
              {{ errors["watermark.opacity"] }}
            </span>
          </label>
        </div>
      </div>

      <div v-if="outputs.watermark !== null" class="rows">
        <div class="row">
          <span class="row__key">文件</span>
          <span class="row__val">
            <StatusDot
              :tone="watermarkTone(outputs.watermark.exists)"
              :label="outputs.watermark.exists ? '在盘上' : '找不到这张图'"
            />
          </span>
        </div>
        <div class="row">
          <span class="row__key">尺寸</span>
          <span class="row__val mono">
            {{ describeWatermark(outputs.watermark) }}
            <span v-if="watermarkPx !== null && watermarkPx !== outputs.watermark.width_px">
              ⇒ 改成 {{ watermarkPx }} px
            </span>
          </span>
        </div>
      </div>

      <p class="hint">
        水印**固定**叠加（位置 / 边距 / 宽度 / 透明度四项）。边距必须是偶数 —— 奇数边距会让
        `overlay` 落在一个半像素上，出片边缘会有一道 1px 的偏移。宽度占比按**默认档的画布宽**
        换算，上面那行会实时跟着变。
      </p>
    </PanelCard>

    <PanelCard
      title="字幕"
      :subtitle="outputs.subtitle === null ? '配置里没有 subtitle 段' : `字体 ${outputs.subtitle.font_name}`"
    >
      <EmptyState
        v-if="form === null || form.subtitle === null"
        title="没有可编辑的字幕样式"
        hint="字幕是 Q11 的开启项：`config/outputs.yaml` 的 `subtitle:` 段不能少。"
      />

      <div v-else class="form">
        <label class="f f--num">
          <span class="f__key">字号</span>
          <input
            class="field mono"
            type="number"
            :value="form.subtitle.font_size"
            @input="onSubtitleInt('font_size', $event)"
          />
          <span v-if="errors['subtitle.font_size']" class="f__err">
            {{ errors["subtitle.font_size"] }}
          </span>
        </label>

        <label class="f f--num">
          <span class="f__key">描边</span>
          <input
            class="field mono"
            type="number"
            :value="form.subtitle.outline"
            @input="onSubtitleInt('outline', $event)"
          />
          <span v-if="errors['subtitle.outline']" class="f__err">
            {{ errors["subtitle.outline"] }}
          </span>
        </label>

        <label class="f f--num">
          <span class="f__key">每行字数</span>
          <input
            class="field mono"
            type="number"
            :value="form.subtitle.max_chars_per_line"
            @input="onSubtitleInt('max_chars_per_line', $event)"
          />
          <span v-if="errors['subtitle.max_chars_per_line']" class="f__err">
            {{ errors["subtitle.max_chars_per_line"] }}
          </span>
        </label>
      </div>

      <p v-if="outputs.subtitle !== null" class="hint">
        每行 {{ outputs.subtitle.max_lines }} 行封顶、距底 {{ outputs.subtitle.margin_bottom }} px、
        阴影 {{ outputs.subtitle.shadow }} —— 这三项一期**不可编辑**（改它们要动版式，属于二期）。
        断句按「每行字数」重排，改小了会让长句多占一行。
      </p>
    </PanelCard>
  </div>
</template>

<style scoped>
.outputs {
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
  flex: 0 0 64px;
  color: var(--text-muted);
}

.row__val {
  min-width: 0;
  color: var(--text-secondary);
  word-break: break-all;
}

.row__err {
  color: var(--error);
  font-size: var(--text-xs);
}

.form {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
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

.f--row {
  flex-direction: row;
  gap: var(--space-4);
}

.f--num {
  flex: 0 0 150px;
}

.f__key {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.f__err {
  color: var(--error);
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

.list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: 0;
  margin: 0;
  list-style: none;
}

.item {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

.item__main {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: baseline;
}

.item__title {
  color: var(--text-primary);
  font-size: var(--text-md);
}

.item__sub {
  color: var(--text-muted);
  font-size: var(--text-xs);
}

.grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.errs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.check {
  display: inline-flex;
  gap: var(--space-1);
  align-items: center;
  color: var(--text-secondary);
  font-size: var(--text-xs);
}
</style>
