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
// 上传中途真断了,再读一次作业把原因说清楚(名额满了),说不清就照实说可能的几种原因。
//
// 票据、文件内容都不经过这里;日志只有 api-client 那一行(路径与 query 的键)。

import { useSyncExternalStore } from "react";

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
  | { kind: "failed"; text: string };

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

/** 预检之后新出现的那个导入作业(读不到为 `null`)。 */
async function createdSince(known: Set<string>): Promise<NotebookTransferJob | null> {
  try {
    const jobs = await listNotebookTransfers();
    return jobs.find((job) => job.kind === "import" && !known.has(job.id)) ?? null;
  } catch {
    return null;
  }
}

/** 上传中途网络断了:再读一次作业,能说清原因就说清楚。 */
async function explainCut(fileSize: number, limitBytes: number | null): Promise<string> {
  try {
    const jobs = await listNotebookTransfers();
    if (activeTransferCount(jobs) >= MAX_ACTIVE_TRANSFER_JOBS) return TRANSFER_BUSY_TEXT;
    // 名额没满、文件也确定没超上限:那就只是网络。
    if (limitBytes !== null && fileSize <= limitBytes) return IMPORT_UPLOAD_FAILED_TEXT;
  } catch {
    // 读不到:原因说不清。
  }
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
    const mine = ++generation;
    const own = new AbortController();
    controller = own;
    const base = { fileName: file.name, total: file.size };
    publish({ ...base, percent: 0, stage: "precheck" });
    const current = () => mine === generation;
    // 预检时读到的作业 id:停止之后再读一次,多出来的那个导入就是这一次上传建出来的。
    let known: Set<string> | null = null;
    try {
      let jobs: NotebookTransferJob[] | null = null;
      try {
        jobs = await listNotebookTransfers(own.signal);
        known = new Set(jobs.map((job) => job.id));
      } catch (error) {
        if (isAbort(error)) throw error;
        // 读不到就不预检:由服务端兜底。
      }
      if (!current()) return;
      if (jobs && activeTransferCount(jobs) >= MAX_ACTIVE_TRANSFER_JOBS) {
        deliver({ kind: "failed", text: TRANSFER_BUSY_TEXT });
        return;
      }
      publish({ ...base, percent: 0, stage: "uploading" });
      const job = await uploadNotebookImport(file, {
        name,
        signal: own.signal,
        onProgress: (sent, total) => {
          if (!current()) return;
          const percent = percentOf(sent, total);
          publish({ ...base, percent, stage: percent >= 100 ? "checking" : "uploading" });
        },
      });
      if (current()) deliver({ kind: "uploaded", job, fileName: file.name });
    } catch (error) {
      if (!current()) return;
      if (isAbort(error)) {
        // 请求体也许在停下的那一刻已经传完:服务器那时会接着检查、建作业。再读一次,
        // 多出来的导入作业就是这一次的——照实交出去,不说「没有导入任何内容」。
        const created = known ? await createdSince(known) : null;
        if (!current()) return;
        deliver(created ? { kind: "uploaded", job: created, fileName: file.name } : { kind: "stopped" });
      } else if (error instanceof TypeError) {
        const text = await explainCut(file.size, limitBytes);
        if (!current()) return;
        deliver({ kind: "failed", text });
      } else {
        // 超过上限被拒:上限可能刚被改小,下次重新读。
        if (httpErrorStatus(error) === 413) forgetTransferLimit();
        deliver({ kind: "failed", text: transferRequestErrorText(error, IMPORT_UPLOAD_ERRORS, IMPORT_UPLOAD_FAILED_TEXT) });
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
