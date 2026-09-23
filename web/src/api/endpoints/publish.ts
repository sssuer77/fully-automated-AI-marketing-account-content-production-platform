// 发布面板 REST 面（T5.5 · §06.11 / §06.12）。
//
// 十三个端点对应面板上的十三件事：看板、待人工队列、投递、人工处置四连（重试 / 取消 /
// 标记已处理 / **人工过验证**）、交付包预览 / 导出、R2 合规留档、数据回收三连
// （跑一轮 / 采这一条 / 沉淀这一条）。
//
// 为什么「数据回收」是三个端点而不是一个
// --------------------------------------
// §4.6.2 把它们分成两个入口，加上"整轮"就是三件不同的事：**轮询**（谁到点了）、
// **采一条**（现在就要这个数）、**沉淀一条**（让下一轮选题吃得到）。合并成一个的代价是
// "我想再沉淀一次"必须连带再采一次数，而那次采集可能正好撞上平台限流。
//
// 为什么「待人工」单独一个端点
// --------------------------
// 它是唯一一个**要求人做决定**的列表（§06.10），其余区块是"看"。分开之后面板可以只轮询
// 它、只给它做顶部计数，而不必每次都拉全量记录 —— 全量那一条在几百条记录时会明显变慢。
//
// 为什么 `can_retry` / `can_cancel` / `can_mark_done` 不在这里算
// -------------------------------------------------------------
// 判据是**服务端**的状态机规则（`published` 不能取消、只有 `manual_required` 能标记已处理）。
// 前端再写一份的代价是"规则改一次漏一处"，而漏的那一处表现为"点了按钮报 400"——
// 用户看到的是"这个按钮坏了"。所以这里只转发服务端给的那三个布尔。
//
// 请求体类型不手写
// ----------------
// 与其余端点文件同一条：从 `types.gen.ts` 里取。手写一份形状，等于在「后端改了字段名」
// 这件事上自愿放弃了编译期保护。

import { apiDelete, apiGet, apiPost, apiPut, type OkJson } from "../http";
import type { paths } from "../types.gen";

/** 某个操作的**请求体**类型（从生成的契约里取，不手写形状）。 */
type BodyJson<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type PublicationList = OkJson<"/api/v1/publish/publications", "get">;
export type Publication = NonNullable<PublicationList["by_status"]>[string][number];
/** 人工处置的请求体（重试 / 取消 / 标记已处理共用 —— 后端也是同一个模型）。 */
export type PublishActionBody = BodyJson<"/api/v1/publish/{publication_id}/retry", "post">;
export type PublishActionResponse = OkJson<"/api/v1/publish/{publication_id}/retry", "post">;
/** 「人工过验证」的结论：与人工处置同一个形状 + `waited_sec`（它可能等上十几分钟）。 */
export type PublishAssistOutcome = OkJson<"/api/v1/publish/{publication_id}/assist", "post">;
export type PublishEnqueueBody = BodyJson<"/api/v1/publish/tasks/{task_id}/enqueue", "post">;
export type PublishEnqueueResponse = OkJson<"/api/v1/publish/tasks/{task_id}/enqueue", "post">;
/** 出封面的请求体（T5.1 追加）。 */
export type PublishCoverBody = BodyJson<"/api/v1/publish/tasks/{task_id}/cover", "post">;
export type PublishCoverOutcome = OkJson<"/api/v1/publish/tasks/{task_id}/cover", "post">;
export type PublishPlatformsView = OkJson<"/api/v1/publish/platforms", "get">;
export type PublishPlatformOption = NonNullable<PublishPlatformsView["items"]>[number];
export type PublishAccountsView = OkJson<"/api/v1/publish/accounts", "get">;
export type PublishAccount = NonNullable<PublishAccountsView["accounts"]>[number];
/** 加号 / 改号的请求体（``account_id`` 在路径里，不在体里 —— 身份只有一个来源）。 */
export type PublishAccountBody = BodyJson<
  "/api/v1/publish/accounts/{account_id}",
  "put"
>;
export type PublishAccountOutcome = OkJson<
  "/api/v1/publish/accounts/{account_id}",
  "put"
>;
export type PublishAccountHealthOutcome = OkJson<
  "/api/v1/publish/accounts/{account_id}/login",
  "post"
>;
export type PublishAccountHealth = NonNullable<PublishAccountHealthOutcome["health"]>;
export type HandoffPreview = OkJson<"/api/v1/publish/handoff/{task_id}", "get">;
export type HandoffItem = NonNullable<HandoffPreview["items"]>[number];
export type HandoffResult = OkJson<"/api/v1/publish/handoff/{task_id}", "post">;
export type ComplianceView = OkJson<"/api/v1/publish/compliance", "get">;
export type ComplianceItem = NonNullable<ComplianceView["items"]>[number];
export type MetricsTickResult = OkJson<"/api/v1/publish/metrics/tick", "post">;
export type MemorySinkResult = OkJson<
  "/api/v1/publish/publications/{publication_id}/sink",
  "post"
>;

const PUBLISH_PATH = "/api/v1/publish";

/**
 * 发布看板（六状态各若干条 + 全量计数）。
 *
 * 带 `taskId` ⇒ 只回这条任务的记录，`counts` 也只算这条任务的。
 */
export function fetchPublications(
  options: { limit?: number; taskId?: string; signal?: AbortSignal } = {},
): Promise<PublicationList> {
  return apiGet<PublicationList>(`${PUBLISH_PATH}/publications`, {
    query: { limit: options.limit ?? 50, task_id: options.taskId ?? null },
    signal: options.signal,
  });
}

/** 待人工队列（只回 `manual_required` 那几条，带 error_code 与取证路径）。 */
export function fetchManualQueue(
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<PublicationList> {
  return apiGet<PublicationList>(`${PUBLISH_PATH}/queue`, {
    query: { limit: options.limit ?? 100 },
    signal: options.signal,
  });
}

/** 人工重试（`attempt_count` 归零 —— 人工这一下是"给它一次完整的机会"）。 */
export function retryPublication(
  publicationId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<PublishActionResponse> {
  return apiPost<PublishActionResponse>(
    `${PUBLISH_PATH}/${encodeURIComponent(publicationId)}/retry`,
    body,
    { signal },
  );
}

/** 人工取消（**已发布的不给取消**：平台上已经有那条作品了，删记录只会让账更乱）。 */
export function cancelPublication(
  publicationId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<PublishActionResponse> {
  return apiPost<PublishActionResponse>(
    `${PUBLISH_PATH}/${encodeURIComponent(publicationId)}/cancel`,
    body,
    { signal },
  );
}

/** 标记已人工处理（**必须写 reason**，后端会 422）。 */
export function markManualDone(
  publicationId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<PublishActionResponse> {
  return apiPost<PublishActionResponse>(
    `${PUBLISH_PATH}/${encodeURIComponent(publicationId)}/manual-done`,
    body,
    { signal },
  );
}

/**
 * 服务端等**人**过验证的上限（秒）—— 与后端 `MANUAL_VERIFY_WAIT_SEC` 同一个数。
 */
export const MANUAL_VERIFY_WAIT_SEC = 900;

/**
 * 服务端**上传成片**的上限（秒）—— 与后端 `UPLOAD_TIMEOUT_SEC` 同一个数。
 *
 * 这条路会把整条片子**重新传一遍**（上一次那个浏览器会话已经没了，能复用的只有
 * 盘上那份登录态），所以这一次请求的耗时里，上传与"等人"是**两段**，都要算进去。
 */
export const ASSIST_UPLOAD_SEC = 300;

/** 「人工过验证」这一次请求的上限：上传 + 等人 + 收尾（秒）。 */
export const ASSIST_TIMEOUT_SEC = MANUAL_VERIFY_WAIT_SEC + ASSIST_UPLOAD_SEC + 60;

/**
 * **人工过验证**：开一个**可见**窗口，把这条重新发一遍，途中停下来等人输验证码。
 *
 * 什么时候按它
 * ------------
 * 那条记录写着 `PUBLISH_FAILED：平台要求短信验证` 时。平台在点下发布之后弹一个
 * 「接收短信验证码」，要人在**浏览器窗口**里点「获取验证码」、再输手机上收到的
 * 六位数。这时「重试」没用：平台问的是"这台机器 / 这个出口 IP 是不是你本人"，
 * 那是个**一次性质询** —— 重试一百次只会弹一百次（真机 2026-09-23 实测）。
 *
 * 为什么超时给到二十多分钟
 * ----------------------
 * 它在等**人**（掏手机、等短信、输码），不是在等网络。窗口开在**跑着 studio 服务
 * 的那台电脑**上；人输完码，服务端自己把流程走完并落库。`timeoutMs` 比服务端那个
 * 上限更长，是为了让"谁先超时"永远由服务端说了算（它才知道窗口关没关）。
 *
 * 它做什么 / 不做什么
 * ------------------
 * 做：开窗口 ⇒ 上传 ⇒ 填标题文案 ⇒ 点发布 ⇒ **停在那儿等人**。
 * 不做：**不点**「获取验证码」、**不读**短信、**不填**码、不存任何凭据（R13）。
 */
export function assistPublication(
  publicationId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<PublishAssistOutcome> {
  return apiPost<PublishAssistOutcome>(
    `${PUBLISH_PATH}/${encodeURIComponent(publicationId)}/assist`,
    body,
    { timeoutMs: ASSIST_TIMEOUT_SEC * 1000, signal },
  );
}

/** 把这条任务排进发布池（**不看 `publish.enabled`**：投递照样成功，作业会在 worker 那侧转人工）。 */
export function enqueueTask(
  taskId: string,
  body: PublishEnqueueBody,
  signal?: AbortSignal,
): Promise<PublishEnqueueResponse> {
  return apiPost<PublishEnqueueResponse>(
    `${PUBLISH_PATH}/tasks/${encodeURIComponent(taskId)}/enqueue`,
    body,
    { signal },
  );
}

/**
 * 出封面这一次请求的上限（秒）—— 与后端那三段耗时加起来同一个量级。
 *
 * 它要跑：Cover Agent（一次 LLM，几秒到几十秒）⇒ 量两串文字的宽度（两次 ffmpeg）
 * ⇒ 合成（再一次 ffmpeg，最多 120 秒）。默认的 10 秒连量字都不够。
 */
export const COVER_TIMEOUT_SEC = 300;

/**
 * **给这条任务出一张封面**（T5.1 追加 · §06.3）。
 *
 * 封面 = 成片里抽的一帧 + 合成配置里的贴图（居中）+ 标题（黄字黑边）。
 * 落 `data/output/covers/`，并写进 `tasks.context_json.cover_path` ——
 * **发布器读的就是后者**，所以出完封面直接投递，那条片子就会带着它发出去。
 *
 * `ok=false` **不是错误**（HTTP 200）：封面是可选装饰，抽帧失败会退成纯色底，
 * 连纯色底都出不来就走"无封面发布"（平台用首帧）—— 那是契约里写明的合法结局。
 * 所以调用方看的是 `ok`，不是 try/catch。
 */
export function makeCover(
  taskId: string,
  body: PublishCoverBody,
  signal?: AbortSignal,
): Promise<PublishCoverOutcome> {
  return apiPost<PublishCoverOutcome>(
    `${PUBLISH_PATH}/tasks/${encodeURIComponent(taskId)}/cover`,
    body,
    { timeoutMs: COVER_TIMEOUT_SEC * 1000, signal },
  );
}

/** 封面图 / 成片封面的可显示 url（`<img>` 直接取它）。 */
export function coverUrl(name: string): string {
  return `${PUBLISH_PATH}/covers/${encodeURIComponent(name)}`;
}

/**
 * 投递面板的选项清单（**来自配置，不是面板自己列的**）。
 *
 * 面板列一份平台清单 = 把 `config/publish.yaml` 抄第二遍：加一个平台要改两处，而漏改
 * 的那一处表现为"这个平台在面板上不存在" —— 没人会去报这个 bug。连"点了会怎样"
 * （`selectable` / `note`）也一起给，判据与投递期是同一套。
 */
export function fetchPlatforms(signal?: AbortSignal): Promise<PublishPlatformsView> {
  return apiGet<PublishPlatformsView>(`${PUBLISH_PATH}/platforms`, { signal });
}

/**
 * 账号区块首屏（T6.4 · **含停用的账号**）。
 *
 * 与 `/publish/platforms` 分开：那一个是"投哪几个平台"（投递用），这里是"我有哪些号、
 * 它们现在什么状态"（配置用）。两块各自刷新 —— 改完账号立刻重画这一块，
 * 不必等 2 秒一次的看板轮询。
 */
export function fetchAccounts(signal?: AbortSignal): Promise<PublishAccountsView> {
  return apiGet<PublishAccountsView>(`${PUBLISH_PATH}/accounts`, { signal });
}

/**
 * 加一个号 / 改一个号（幂等替换：同一个账号存两次，第二次连盘都不碰）。
 *
 * 表单不合法 ⇒ 422 + `context.errors`（面板把红字标到对应输入框上）；
 * 与别的账号冲突（`profile_dir` 重复 / 平台没定义）⇒ 400 —— 两档判据都在后端，
 * 前端一个都不自己算。
 */
export function saveAccount(
  accountId: string,
  body: PublishAccountBody,
  signal?: AbortSignal,
): Promise<PublishAccountOutcome> {
  return apiPut<PublishAccountOutcome>(
    `${PUBLISH_PATH}/accounts/${encodeURIComponent(accountId)}`,
    body,
    { signal },
  );
}

/**
 * 删掉一个号（**登录态目录不碰**：`data/browser_profile/<id>/` 是凭据，§02.5）。
 *
 * `reason` 走查询串而不是请求体：DELETE 带 body 在代理链路上会被静默丢掉，
 * 而"这条为什么删了"是三个月后唯一能回答问题的东西。
 */
export function deleteAccount(
  accountId: string,
  reason: string | null,
  signal?: AbortSignal,
): Promise<PublishAccountOutcome> {
  return apiDelete<PublishAccountOutcome>(
    `${PUBLISH_PATH}/accounts/${encodeURIComponent(accountId)}`,
    { query: { reason }, signal },
  );
}

/**
 * 扫码登录的等待上限（秒）—— 与后端 `LOGIN_TIMEOUT_SEC` 同一个数。
 *
 * 前端**必须**知道这个数：下面那个 fetch 超时要跟着它走，否则会出现
 * "浏览器窗口还开着、面板已经报超时"。
 */
export const LOGIN_TIMEOUT_SEC = 180;

/**
 * 等一次「检测登录态」的上限（秒）。
 *
 * **不能**用 `http.ts` 里那个 10s 的默认值。探测要冷启动一个 Chromium（几秒到
 * 十几秒），再打开平台页面（服务端 `NAV_TIMEOUT_MS` 30s），最后还要等 SPA 把登录
 * 标志渲染出来（服务端 `MARKER_WAIT_SEC` 8s）—— 真机上十几秒是常态。
 *
 * 用默认值的后果**不是**"探测失败"，而是"面板报超时、服务端其实探成功了"：用户
 * 看到一行红字（`/api/v1/publish/accounts/x/probe 超时（10000 ms）`），而真相只
 * 写在日志里。120s 是服务端最坏耗时（≈ 启动 10 + 导航 30 + 等标志 8 + 收尾）的三倍。
 */
export const PROBE_TIMEOUT_SEC = 120;

/**
 * 探一眼这个账号的登录态（**无头、不动任何东西**；十几秒是正常的，不是卡住）。
 *
 * 与 `loginAccount` 分开：这一个回答"现在能不能发"，那一个回答"帮我登上"。
 * 合成一个的代价是"我就想看一眼"必须开一个可见窗口 —— 而那会占住这个账号的
 * profile（同一个账号同时只能开一个浏览器）。
 */
export function probeAccount(
  accountId: string,
  signal?: AbortSignal,
): Promise<PublishAccountHealthOutcome> {
  return apiPost<PublishAccountHealthOutcome>(
    `${PUBLISH_PATH}/accounts/${encodeURIComponent(accountId)}/probe`,
    undefined,
    { timeoutMs: PROBE_TIMEOUT_SEC * 1000, signal },
  );
}

/**
 * 开一个**可见的浏览器窗口**等人扫码（**长动作**：最长等 `timeoutSec` 秒）。
 *
 * 窗口出现在**跑着 studio 服务的那台电脑**上 —— 浏览器是它起的。人用手机上对应
 * 的 App 扫窗口里那个码，扫完窗口自己关掉。
 *
 * 为什么 `timeoutMs` 要**比**服务端那个等待更长：短于它的话，人还在扫，前端已经
 * 把请求 abort 掉了 —— 面板报"超时"，而窗口还开着、码还能扫。留 30 秒余量，
 * 让"谁先超时"这件事永远是服务端说了算（它才知道窗口关没关）。
 */
export function loginAccount(
  accountId: string,
  timeoutSec: number = LOGIN_TIMEOUT_SEC,
  signal?: AbortSignal,
): Promise<PublishAccountHealthOutcome> {
  return apiPost<PublishAccountHealthOutcome>(
    `${PUBLISH_PATH}/accounts/${encodeURIComponent(accountId)}/login`,
    undefined,
    { query: { timeout_sec: timeoutSec }, timeoutMs: (timeoutSec + 30) * 1000, signal },
  );
}

/** 交付包**预览**：会打进去哪几件、缺哪件（后端保证一个字节都不写）。 */
export function fetchHandoff(taskId: string, signal?: AbortSignal): Promise<HandoffPreview> {
  return apiGet<HandoffPreview>(`${PUBLISH_PATH}/handoff/${encodeURIComponent(taskId)}`, {
    signal,
  });
}

/** 打一个交付包出去（复制到 `app.yaml → handoff.output_dir` 下，并写 audit_ops）。 */
export function pushHandoff(
  taskId: string,
  body: PublishActionBody,
  signal?: AbortSignal,
): Promise<HandoffResult> {
  return apiPost<HandoffResult>(`${PUBLISH_PATH}/handoff/${encodeURIComponent(taskId)}`, body, {
    signal,
  });
}

/** R2 来源登记留档（**发布面板与素材库共用这一份**）。 */
export function fetchCompliance(signal?: AbortSignal): Promise<ComplianceView> {
  return apiGet<ComplianceView>(`${PUBLISH_PATH}/compliance`, { signal });
}

/**
 * 立刻跑一轮数据回收（T5.4 · §06.6）。
 *
 * 后台本来每 60s 自己拍一次；这个端点是**给人按的** —— "我刚发完想现在看一眼数据"，
 * 或者"上一轮看着没动静，手动催一下"。发布池正忙时它会整拍让路（``yielded``），
 * 那不是失败：两个浏览器抢同一个 profile 会让**发布**失败。
 */
export function runMetricsTick(signal?: AbortSignal): Promise<MetricsTickResult> {
  return apiPost<MetricsTickResult>(`${PUBLISH_PATH}/metrics/tick`, undefined, { signal });
}

/** 采**这一条**（不看 ``next_metric_at``：人按的就是"现在采"）。 */
export function collectMetrics(publicationId: string, signal?: AbortSignal): Promise<Publication> {
  return apiPost<Publication>(
    `${PUBLISH_PATH}/publications/${encodeURIComponent(publicationId)}/collect`,
    undefined,
    { signal },
  );
}

/** 把这一条的数据**沉淀成记忆**（§06.8：回流 feedback + 汇总文件 + 低互动降权）。 */
export function sinkMemory(publicationId: string, signal?: AbortSignal): Promise<MemorySinkResult> {
  return apiPost<MemorySinkResult>(
    `${PUBLISH_PATH}/publications/${encodeURIComponent(publicationId)}/sink`,
    undefined,
    { signal },
  );
}
