// 网页导入的上传:一个模块级的小仓库,不住在 React 里。
//
// 为什么不放在 `useNotebookImport` 的 state 里:
//   * 进度一秒能来几十次。放在 Home 持有的 hook 里,每一次都重渲整个 Home(弹窗关着也是)。
//     这里只在**整数百分比或阶段变了**时通知,订阅它的只有导入弹窗(`useImportUpload`)。
//   * 上传是浏览器自己的一次请求,不该跟着某个组件的生命周期走:Home 因为客户端导航卸载
//     (去独立页再回来)时,它照样传完,回来的弹窗重新订阅就看得见进度,结果也不丢——
//     没人接的结果先存着(`takeOutcomes`),下一个挂载的状态机拿走。所以不会悄悄丢掉一次
//     上传,也不会让人在不知情时再传一份。
//   * 关掉或刷新标签页会真的中断上传:上传进行中挂一个 `beforeunload` 提示。
//
// 上传之前先读一次自己的作业:进行中的导出与导入已经有两个(后端每人上限),就不上传——
// 服务器会在读请求体之前回 409 并断开,浏览器那边只看得到一次说不清原因的网络中断。
// 每一串上传尝试带一个上传标识(`X-Upload-Id`,见 `chain`):上传被打断后只按它认领这一次
// 建出的作业(别的标签页的导入不算),重传同一个文件也用它,服务端据此不会建第二本。
// 确定没被接收时才说失败或「没有导入任何内容」;说不准时照实说说不准。
//
// 票据、文件内容都不经过这里;日志只有 api-client 那一行(路径与 query 的键)。

import { useSyncExternalStore } from "react";

import { newClientRequestId } from "./client-request-id.ts";
import { httpErrorStatus } from "./errors.ts";
import { pageReloading } from "./page-reload.ts";
import {
  IMPORT_UPLOAD_CUT_TEXT,
  IMPORT_UPLOAD_ERRORS,
  IMPORT_UPLOAD_FAILED_TEXT,
  MAX_ACTIVE_TRANSFER_JOBS,
  TRANSFER_BUSY_TEXT,
  activeTransferCount,
  forgetTransferLimit,
  listNotebookTransfers,
  transferRequestErrorText,
  uploadNotebookImport,
  uploadRefusedForSure,
  uploadStillInProgress,
  type NotebookTransferJob,
} from "./notebook-transfer-api.ts";

/** 上传此刻在哪一步:先核对名额 → 上传 → 传完了、等服务器检查文件。 */
export type UploadStage = "precheck" | "uploading" | "checking";

export type UploadSnapshot = Readonly<{
  fileName: string;
  total: number;
  /** 0–100 的整数。 */
  percent: number;
  stage: UploadStage;
}> | null;

/** 一次上传的结局,交给(当时或之后挂载的)导入状态机。 */
export type UploadOutcome =
  | { kind: "uploaded"; job: NotebookTransferJob; fileName: string }
  | { kind: "stopped" }
  | { kind: "failed"; text: string }
  /** 连接断了,而服务器有没有接收这次上传说不准:不能当失败,也不能叫人马上重传。 */
  | { kind: "unsure"; text: string }
  /** 说不准之后对账到时限,服务器始终没有建出这次的导入:可以重传(仍用同一个标识,无害)。 */
  | { kind: "not_created"; text: string };

type Listener = () => void;

let snapshot: UploadSnapshot = null;
let controller: AbortController | null = null;
let generation = 0;
const listeners = new Set<Listener>();
const pendingOutcomes: UploadOutcome[] = [];
const outcomeConsumers = new Set<(outcome: UploadOutcome) => void>();

function publish(next: UploadSnapshot): void {
  const same = snapshot === next || (
    snapshot !== null && next !== null
    && snapshot.percent === next.percent && snapshot.stage === next.stage
    && snapshot.fileName === next.fileName && snapshot.total === next.total
  );
  if (same) return;
  snapshot = next;
  syncUnloadGuard();
  for (const listener of [...listeners]) listener();
}

function deliver(outcome: UploadOutcome): void {
  if (outcomeConsumers.size === 0) {
    pendingOutcomes.push(outcome);
    return;
  }
  for (const consumer of [...outcomeConsumers]) consumer(outcome);
}

const onBeforeUnload = (event: BeforeUnloadEvent) => {
  // 页面自己发起的重载(401 清掉会话、别的标签页换了登录)不问:那时上传本来就进行不下去。
  if (pageReloading()) return;
  event.preventDefault();
  // 旧浏览器要求设一个字符串才弹框;内容浏览器不展示。
  event.returnValue = "";
};
let unloadGuarded = false;
function syncUnloadGuard(): void {
  if (typeof window === "undefined") return;
  const want = snapshot !== null;
  if (want === unloadGuarded) return;
  unloadGuarded = want;
  if (want) window.addEventListener("beforeunload", onBeforeUnload);
  else window.removeEventListener("beforeunload", onBeforeUnload);
}

function percentOf(sent: number, total: number): number {
  return total > 0 ? Math.min(100, Math.floor((sent / total) * 100)) : 0;
}

const isAbort = (error: unknown): boolean =>
  error instanceof DOMException && error.name === "AbortError";

/** 连接断了、又判断不出服务器有没有接收时那句话:不叫人马上重传(会多出一本)。 */
export const IMPORT_UPLOAD_UNSURE_TEXT = "连接中断，导入可能已被接收；请先看下方的导入记录，再决定是否重传。";
/** 停止了、而请求体可能已经整个到了服务器:同一个意思。 */
export const IMPORT_STOP_UNSURE_TEXT = "已停止上传，但文件可能已经传完、导入可能已被接收；请先看下方的导入记录，再决定是否重传。";

/** 说不准之后对账到时限、还是没看到作业时那句话。 */
export const IMPORT_NOT_CREATED_TEXT = "服务器没有建出这次的导入，可以重传。";
/** 说不准之后,每隔多久按上传标识对一次账。 */
export const RECONCILE_INTERVAL_MS = 4000;
/**
 * 对账的时限:服务器收完请求体之后检查文件、数文档、建作业,最多也就这么久(网页导入上限
 * 之内的文件);过了它还没看到作业,就是没建出来。
 */
export const RECONCILE_WINDOW_MS = 3 * 60_000;

/** 服务器回了话、却不是认得出的确定拒绝(网关超时、5xx……)时那句话。 */
export const IMPORT_RESPONSE_UNSURE_TEXT = "服务器没有给出明确结果，导入可能已被接收；请先看下方的导入记录，再决定是否重传。";
/** 已经停在预检那一步、而上一次带这个标识的上传结果未定时那句话。 */
export const IMPORT_PRECHECK_STOP_UNSURE_TEXT = "已停止。上一次上传可能已被接收；请先看下方的导入记录，再决定是否重传。";

/**
 * 一串上传尝试共用的上传标识(`X-Upload-Id`)。它绑在**选中的那个文件**上,一直沿用到这一
 * 串有了确定的结局为止:说不准有没有被接收(`unsure`)时,再传这同一个文件仍用同一个标识
 * ——服务端认出它,回已经建出的那个作业,不读请求体,不会多出一本。
 *
 * 只有确定的结局才换标识(`endChain`):已接收(含按标识认领到)、确定没被接收、服务端明确
 * 拒绝且这个标识下确实没有作业,或者换了文件。预检里的拒绝(名额满了、作业读不到)与预检
 * 时的停止都**不是**确定的结局——那时请求还没发出,而上一次带这个标识的尝试(`unresolved`)
 * 可能已经建出作业、正占着名额;换了标识,下一次重传就会多出一本。
 */
let chain: { file: File; uploadId: string; unresolved: boolean } | null = null;

function chainFor(file: File): { file: File; uploadId: string; unresolved: boolean } {
  if (chain?.file !== file) chain = { file, uploadId: newClientRequestId(), unresolved: false };
  return chain;
}

/** 这一串尝试有了确定的结局:下一次上传用新的标识。 */
function endChain(file: File): void {
  if (chain?.file === file) chain = null;
}

/** 这一串尝试的结局说不准:标识留着,下一次(哪怕被打断)也不能当作确定没导入。 */
function leaveUnresolved(file: File): void {
  if (chain?.file === file) chain.unresolved = true;
}

/**
 * 说不准之后的后台对账:每隔 `RECONCILE_INTERVAL_MS` 按上传标识读一次作业,直到认领到
 * (交出 `uploaded`)、到了 `RECONCILE_WINDOW_MS`(交出 `not_created`,标识留着)、这一串
 * 又开始了一次新的尝试、换了文件,或登出。弹窗开不开都在跑:它不住在 React 里。
 */
let reconciler: { timer: ReturnType<typeof setTimeout> | null } | null = null;

function stopReconcile(): void {
  if (reconciler?.timer) clearTimeout(reconciler.timer);
  reconciler = null;
}

function startReconcile(file: File): void {
  stopReconcile();
  const own = chain;
  if (own === null || own.file !== file) return;
  const handle: { timer: ReturnType<typeof setTimeout> | null } = { timer: null };
  reconciler = handle;
  const deadline = Date.now() + RECONCILE_WINDOW_MS;
  const alive = () => reconciler === handle && chain === own;
  const tick = async () => {
    handle.timer = null;
    if (!alive()) return;
    const job = await jobOfUpload(own.uploadId);
    if (!alive()) return;
    if (job) {
      reconciler = null;
      endChain(file);
      deliver({ kind: "uploaded", job, fileName: file.name });
      return;
    }
    if (Date.now() >= deadline) {
      // 没建出来:标识照旧留着——再传一次用的还是它,服务端那边即便晚到也只会认成同一个。
      reconciler = null;
      deliver({ kind: "not_created", text: IMPORT_NOT_CREATED_TEXT });
      return;
    }
    handle.timer = setTimeout(() => { void tick(); }, RECONCILE_INTERVAL_MS);
  };
  handle.timer = setTimeout(() => { void tick(); }, RECONCILE_INTERVAL_MS);
}

/** 交出一个「说不准」:标识留着,并在后台接着按标识对账。 */
function deliverUnsure(file: File, text: string): void {
  leaveUnresolved(file);
  deliver({ kind: "unsure", text });
  startReconcile(file);
}

/**
 * 按上传标识找这一次上传建出的作业。只认 `client_upload_id` 相等的那一个:别的标签页
 * 同时开始的导入不是这一次的。读不到作业为 `undefined`(说不准),没有为 `null`。
 */
async function jobOfUpload(uploadId: string): Promise<NotebookTransferJob | null | undefined> {
  try {
    const jobs = await listNotebookTransfers();
    return jobs.find((job) => job.kind === "import" && job.client_upload_id === uploadId) ?? null;
  } catch {
    return undefined;
  }
}

/**
 * 上传被打断(连接断了,或用户停止)之后:先按上传标识认领这一次的作业。
 *
 * * 找到了:服务器已经接收,照实交出(`uploaded`)——不能说失败或「没有导入任何内容」。
 * * 说不准(`unsure`):作业读不到;或者请求体已经整个交给了网络(服务器可能还在检查文件、
 *   作业还没登记)却还没看到它。不叫人马上重传;真要重传,用的仍是同一个标识。
 * * 确定没被接收:请求体没有传完——服务器只在读完整个请求体之后才建作业,半截的请求体
 *   一断开它就放掉名额。停止的就说停止了;断开的再说清原因(名额满了 / 只是网络 / 可能的
 *   几种原因)。
 */
async function settleInterrupted(
  uploadId: string,
  stopped: boolean,
  bodySent: boolean,
  file: File,
  limitBytes: number | null,
): Promise<UploadOutcome> {
  const job = await jobOfUpload(uploadId);
  if (job) return { kind: "uploaded", job, fileName: file.name };
  const unsureText = stopped ? IMPORT_STOP_UNSURE_TEXT : IMPORT_UPLOAD_UNSURE_TEXT;
  if (job === undefined || bodySent) return { kind: "unsure", text: unsureText };
  if (stopped) return { kind: "stopped" };
  return { kind: "failed", text: await cutReason(file, limitBytes) };
}

/** 确定没被接收的一次断开:能说清原因就说清楚。 */
async function cutReason(file: File, limitBytes: number | null): Promise<string> {
  try {
    if (activeTransferCount(await listNotebookTransfers()) >= MAX_ACTIVE_TRANSFER_JOBS) return TRANSFER_BUSY_TEXT;
  } catch {
    return IMPORT_UPLOAD_CUT_TEXT;
  }
  // 名额没满、文件也确定没超上限:那就只是网络。
  if (limitBytes !== null && file.size <= limitBytes) return IMPORT_UPLOAD_FAILED_TEXT;
  return IMPORT_UPLOAD_CUT_TEXT;
}

export const importUpload = {
  subscribe(listener: Listener): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
  getSnapshot(): UploadSnapshot {
    return snapshot;
  },
  busy(): boolean {
    return snapshot !== null;
  },
  /** 接收上传的结局;先把挂载前就落定、还没人拿走的那些交出来。 */
  consumeOutcomes(consumer: (outcome: UploadOutcome) => void): () => void {
    outcomeConsumers.add(consumer);
    for (const outcome of pendingOutcomes.splice(0)) consumer(outcome);
    return () => outcomeConsumers.delete(consumer);
  },
  /**
   * 开始上传(已经有一次在进行就什么都不做)。`limitBytes` 只用来解释中途断开的原因;
   * 本地预检由调用方先做。结局经 `consumeOutcomes` 交付。
   */
  async start(file: File, name: string, limitBytes: number | null): Promise<void> {
    if (snapshot !== null) return;
    // 新的一次尝试接手:后台对账让给它(同一串的话,这次尝试自己会按标识认领)。
    stopReconcile();
    const mine = ++generation;
    const own = new AbortController();
    controller = own;
    const base = { fileName: file.name, total: file.size };
    publish({ ...base, percent: 0, stage: "precheck" });
    const current = () => mine === generation;
    const attempt = chainFor(file);
    const { uploadId } = attempt;
    // 上一次带这个标识的尝试结果未定:这一次没认领到作业时,也只能说说不准。
    const carried = attempt.unresolved;
    // 上传请求已经发出 / 请求体已经整个交给了网络(`xhr.upload` 的 load):此后服务器
    // 可能已经接收,打断之后要按上传标识认领。
    let requestStarted = false;
    let bodySent = false;
    try {
      let jobs: NotebookTransferJob[] | null = null;
      try {
        jobs = await listNotebookTransfers(own.signal);
      } catch (error) {
        if (isAbort(error)) throw error;
        // 读不到就不预检:由服务端兜底。
      }
      if (!current()) return;
      // 先按标识对账:上一次尝试其实已经被接收(它的作业可能正占着一个名额)——认领它,不再上传。
      const adopted = jobs?.find((job) => job.kind === "import" && job.client_upload_id === uploadId);
      if (adopted) {
        endChain(file);
        deliver({ kind: "uploaded", job: adopted, fileName: file.name });
        return;
      }
      if (jobs && activeTransferCount(jobs) >= MAX_ACTIVE_TRANSFER_JOBS) {
        // 预检拒绝不是确定的结局:标识留着。
        deliver({ kind: "failed", text: TRANSFER_BUSY_TEXT });
        return;
      }
      publish({ ...base, percent: 0, stage: "uploading" });
      requestStarted = true;
      const job = await uploadNotebookImport(file, {
        name,
        uploadId,
        signal: own.signal,
        onProgress: (sent, total) => {
          if (!current()) return;
          const percent = percentOf(sent, total);
          // 进度到了 100% 也按「请求体已经传完」算(宁可说不准,不说没导入)。
          if (percent >= 100) bodySent = true;
          publish({ ...base, percent, stage: percent >= 100 ? "checking" : "uploading" });
        },
        onBodySent: () => {
          bodySent = true;
          if (current()) publish({ ...base, percent: 100, stage: "checking" });
        },
      });
      if (!current()) return;
      endChain(file);
      deliver({ kind: "uploaded", job, fileName: file.name });
    } catch (error) {
      if (!current()) return;
      const interrupted = isAbort(error) || error instanceof TypeError;
      if (interrupted && !requestStarted) {
        // 停在预检那一步:这一次的请求根本没发出。标识留着;上一次的结果未定就照实说。
        if (carried) deliverUnsure(file, IMPORT_PRECHECK_STOP_UNSURE_TEXT);
        else deliver({ kind: "stopped" });
      } else if (interrupted) {
        let outcome = await settleInterrupted(uploadId, isAbort(error), bodySent, file, limitBytes);
        if (!current()) return;
        // 没认领到作业,而上一次带这个标识的尝试结果未定:这一次的「确定没被接收」不代表那一次。
        if (carried && outcome.kind !== "uploaded") {
          outcome = { kind: "unsure", text: isAbort(error) ? IMPORT_STOP_UNSURE_TEXT : IMPORT_UPLOAD_UNSURE_TEXT };
        }
        if (outcome.kind === "unsure") {
          deliverUnsure(file, outcome.text);
        } else {
          endChain(file);
          deliver(outcome);
        }
      } else if (uploadRefusedForSure(error)) {
        // 白名单里的确定拒绝(后端在建作业之前说的)。有些拒绝(格式、按声明长度的 413)发生在
        // 服务端按标识去重之前,证明不了这个标识下没有作业——换标识之前先按标识对账,第一次
        // 尝试也一样:认领到就交出它,读不到就把标识留着。
        if (httpErrorStatus(error) === 413) forgetTransferLimit();
        const text = transferRequestErrorText(error, IMPORT_UPLOAD_ERRORS, IMPORT_UPLOAD_FAILED_TEXT);
        const job = await jobOfUpload(uploadId);
        if (!current()) return;
        if (job) {
          endChain(file);
          deliver({ kind: "uploaded", job, fileName: file.name });
        } else {
          if (job === null) endChain(file);
          deliver({ kind: "failed", text });
        }
      } else {
        // 其余一切回答(5xx、502/503/504、408、429、不认得的状态码或正文、没有认得的码的
        // 「像后端」的拒绝、409 upload_in_progress、响应读不懂):说不准——请求体可能已经到了
        // 服务器,它可能还会建出作业。按标识对账;没认领到就留着标识,不叫人马上重传。
        const job = await jobOfUpload(uploadId);
        if (!current()) return;
        if (job) {
          endChain(file);
          deliver({ kind: "uploaded", job, fileName: file.name });
        } else {
          deliverUnsure(file, uploadStillInProgress(error)
            ? transferRequestErrorText(error, {}, IMPORT_UPLOAD_UNSURE_TEXT)
            : IMPORT_RESPONSE_UNSURE_TEXT);
        }
      }
    } finally {
      if (current()) {
        controller = null;
        publish(null);
      }
    }
  },
  /**
   * 停止进行中的上传。请求体传完之后(`checking`)不再能停:服务器已经在检查文件、
   * 会建出作业——那时「停止」只会让界面说一句假话,所以什么都不做(按钮也不出现)。
   */
  stop(): void {
    if (snapshot?.stage === "checking") return;
    controller?.abort();
  },
  /** 登出:中止上传,丢掉一切(包括还没人拿走的结局),下一个人从空白开始。 */
  reset(): void {
    generation += 1;
    stopReconcile();
    chain = null;
    controller?.abort();
    controller = null;
    pendingOutcomes.splice(0);
    publish(null);
  },
};

/** 导入弹窗订阅上传进度:只在整数百分比或阶段变了时重渲。 */
export function useImportUpload(): UploadSnapshot {
  return useSyncExternalStore(importUpload.subscribe, importUpload.getSnapshot, () => null);
}

const busySnapshot = () => snapshot !== null;

/** 只问「有没有上传在进行」:工具栏的「导入」按钮用它,进度变化不让它重渲。 */
export function useImportUploadBusy(): boolean {
  return useSyncExternalStore(importUpload.subscribe, busySnapshot, () => false);
}
