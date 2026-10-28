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
// 每一串上传尝试带一个上传标识(`X-Upload-Id`,见 `chains`):上传被打断后只按它认领这一次
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
  getImportUploadStatus,
  listNotebookTransfers,
  transferRequestErrorText,
  uploadNotebookImport,
  uploadRefusedForSure,
  uploadStillInProgress,
  type ImportUploadStatus,
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
  /** 说不准之后,服务端两次(隔开一段时间)都说没有这个标识的上传:可以重传(仍用同一个标识,无害)。 */
  | { kind: "not_created"; text: string };

/** 一个结局出自哪一次选择:选中的文件与它那一串的上传标识。界面只在当前选择还是它时才动表单。 */
export type UploadOrigin = Readonly<{ file: File; uploadId: string }>;

type Listener = () => void;

let snapshot: UploadSnapshot = null;
let controller: AbortController | null = null;
let generation = 0;
const listeners = new Set<Listener>();
type OutcomeConsumer = (outcome: UploadOutcome, origin: UploadOrigin) => void;
const pendingOutcomes: Array<[UploadOutcome, UploadOrigin]> = [];
const outcomeConsumers = new Set<OutcomeConsumer>();

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

function deliver(outcome: UploadOutcome, origin: UploadOrigin): void {
  if (outcomeConsumers.size === 0) {
    pendingOutcomes.push([outcome, origin]);
    return;
  }
  for (const consumer of [...outcomeConsumers]) consumer(outcome, origin);
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

/** 服务端确认没有这个标识的上传时那句话。 */
export const IMPORT_NOT_CREATED_TEXT = "服务器没有建出这次的导入，可以重传。";
/** 对账一直问不到服务端、没法确认时那句话:不说没建出来,标识留着。 */
export const IMPORT_RECONCILE_UNREADABLE_TEXT = "暂时无法确认这次导入是否已被接收；稍后打开导入窗口会再次核对。";
/** 连续问不到这么多次就先停下(等打开弹窗或下一次尝试时再接着核对)。 */
export const RECONCILE_GIVE_UP_AFTER_FAILURES = 3;
/** 说不准之后,每隔多久问一次服务端。 */
export const RECONCILE_INTERVAL_MS = 4000;
/**
 * 服务端说「没有」之后,要隔这么久再说一次「没有」才算确定没建出来:请求可能还停在前面的
 * 反向代理里(Next 会先把整个请求体缓存下来再转发),服务端那时还没见过这个标识。
 */
export const NONE_CONFIRM_MS = 15_000;

/** 服务器回了话、却不是认得出的确定拒绝(网关超时、5xx……)时那句话。 */
export const IMPORT_RESPONSE_UNSURE_TEXT = "服务器没有给出明确结果，导入可能已被接收；请先看下方的导入记录，再决定是否重传。";
/** 已经停在预检那一步、而上一次带这个标识的上传结果未定时那句话。 */
export const IMPORT_PRECHECK_STOP_UNSURE_TEXT = "已停止。上一次上传可能已被接收；请先看下方的导入记录，再决定是否重传。";

/**
 * 一串上传尝试共用的上传标识(`X-Upload-Id`)。它绑在**选中的那个文件**上,一直沿用到这一
 * 串有了确定的结局为止:说不准有没有被接收(`unsure`)时,再传这同一个文件仍用同一个标识
 * ——服务端认出它,回已经建出的那个作业,不读请求体,不会多出一本。
 *
 * 一串**只能**经 `retireChain` 结束(换标识),而它只收服务端给出的凭据(`ChainProof`):
 *   (a) 服务端拿出了带这个标识的作业(上传的 202、预检列表里的那一行、去向接口说 `job`);
 *   (b) 服务端的去向接口说这个标识 `none`——第一次尝试被白名单拒绝之后读到一次,或后台对账
 *       隔开 `NONE_CONFIRM_MS` 读到两次(中间没有 `receiving`)。
 * 除此之外——白名单拒绝(尤其是重传时)、断线、停止、预检拒绝、作业列表里「没看到」——一律
 * 不结束、不换标识:请求可能还缓存在代理里,或还占着服务端的预留,换了标识重传就会多出一本。
 * 换了文件不结束旧的一串(它脱离表单、在后台接着对账);登出(`reset`)丢掉全部。
 * 守卫 `notebook-import-chain-guard` 钉住:删除一串、生成或改写标识只发生在这几处。
 */
type Chain = {
  file: File;
  uploadId: string;
  unresolved: boolean;
  /** 这一串的后台对账(没有在跑为 `null`)。 */
  reconciler: { timer: ReturnType<typeof setTimeout> | null } | null;
};
const chains = new Map<File, Chain>();
/** 此刻正在上传的那个文件(没有为 `null`)。 */
let activeFile: File | null = null;

function chainFor(file: File): Chain {
  let found = chains.get(file);
  if (!found) {
    found = { file, uploadId: newClientRequestId(), unresolved: false, reconciler: null };
    chains.set(file, found);
  }
  return found;
}

const originOf = (file: File): UploadOrigin => ({ file, uploadId: chains.get(file)?.uploadId ?? "" });

/** 结束一串(下一次上传换新标识)的凭据:只能出自服务端的回答。 */
export type ChainProof =
  /** 服务端拿出了带这个标识的作业。 */
  | { kind: "job"; job: NotebookTransferJob }
  /** 服务端的去向接口说这个标识 `none`(见 `Chain` 上方的两种情形)。 */
  | { kind: "server_none" };

/** 唯一结束一串、换掉标识的地方。凭据只能是服务端的回答(`ChainProof`)。 */
export function retireChain(file: File, proof: ChainProof): void {
  const found = chains.get(file);
  if (!found) return;
  if (proof.kind === "job" && proof.job.client_upload_id && proof.job.client_upload_id !== found.uploadId) return;
  stopReconcile(found);
  chains.delete(file);
}

/** 这一串尝试的结局说不准:标识留着,下一次(哪怕被打断)也不能当作确定没导入。 */
function leaveUnresolved(file: File): void {
  const found = chains.get(file);
  if (found) found.unresolved = true;
}

/**
 * 说不准之后的后台对账:每隔 `RECONCILE_INTERVAL_MS` 问服务端「这个标识后来怎样了」
 * (`GET /notebook-imports/uploads/{id}`,权威回答)。
 *
 * * `job`:认领它,交出 `uploaded`。
 * * `receiving`:还在接收或检查,接着等——服务端这么说着就没有时限(它的检查没有时限)。
 * * `none`:第一次只是记下;隔 `NONE_CONFIRM_MS` 以上再连着说 `none`,才交出「没建出来」
 *   (`not_created`,标识留着)。中间说了 `receiving` 就重新数。
 * * 问不到:不算任何回答。连续 `RECONCILE_GIVE_UP_AFTER_FAILURES` 次问不到就交出「暂时无法
 *   确认」(仍是 `unsure`,标识留着)并先停下;打开导入弹窗(`resumeReconcile`)或下一次
 *   尝试的预检会接着核对。
 * * 这一串开始新的一次尝试、有了确定结局、或登出,就停。换了文件不停:旧的一串在后台接着对账。
 *
 * 弹窗开不开都在跑:它不住在 React 里。
 */
function stopReconcile(found: Chain): void {
  if (found.reconciler?.timer) clearTimeout(found.reconciler.timer);
  found.reconciler = null;
}

async function uploadStatusOf(uploadId: string): Promise<ImportUploadStatus | undefined> {
  try {
    return await getImportUploadStatus(uploadId);
  } catch {
    return undefined;
  }
}

function startReconcile(file: File, firstDelay: number = RECONCILE_INTERVAL_MS): void {
  const own = chains.get(file);
  if (!own || !own.unresolved) return;
  stopReconcile(own);
  const handle: { timer: ReturnType<typeof setTimeout> | null } = { timer: null };
  own.reconciler = handle;
  const alive = () => own.reconciler === handle && chains.get(file) === own;
  let failures = 0;
  let firstNoneAt: number | null = null;
  const tick = async () => {
    handle.timer = null;
    if (!alive()) return;
    const answer = await uploadStatusOf(own.uploadId);
    if (!alive()) return;
    const origin = { file, uploadId: own.uploadId };
    if (answer?.status === "job") {
      retireChain(file, { kind: "job", job: answer.job });
      deliver({ kind: "uploaded", job: answer.job, fileName: file.name }, origin);
      return;
    }
    if (answer === undefined) {
      failures += 1;
      if (failures >= RECONCILE_GIVE_UP_AFTER_FAILURES) {
        own.reconciler = null;
        deliver({ kind: "unsure", text: IMPORT_RECONCILE_UNREADABLE_TEXT }, origin);
        return;
      }
    } else {
      failures = 0;
      if (answer.status === "receiving") {
        firstNoneAt = null;
      } else if (firstNoneAt === null) {
        firstNoneAt = Date.now();
      } else if (Date.now() - firstNoneAt >= NONE_CONFIRM_MS) {
        // 服务端隔开两次都说没有:没建出来。标识照旧留着——再传一次用的还是它。
        own.reconciler = null;
        deliver({ kind: "not_created", text: IMPORT_NOT_CREATED_TEXT }, origin);
        return;
      }
    }
    handle.timer = setTimeout(() => { void tick(); }, RECONCILE_INTERVAL_MS);
  };
  handle.timer = setTimeout(() => { void tick(); }, firstDelay);
}

/** 交出一个「说不准」:标识留着,并在后台接着按标识对账。 */
function deliverUnsure(file: File, text: string): void {
  leaveUnresolved(file);
  deliver({ kind: "unsure", text }, originOf(file));
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
  /**
   * 接着核对结果未定、对账已经停下的那几串上传(打开导入弹窗时调用):马上问一次。对账还在
   * 跑的、或那个文件此刻正在上传的,不动。
   */
  resumeReconcile(): void {
    for (const found of chains.values()) {
      if (found.unresolved && found.reconciler === null && found.file !== activeFile) {
        startReconcile(found.file, 0);
      }
    }
  },
  /** 接收上传的结局;先把挂载前就落定、还没人拿走的那些交出来。 */
  consumeOutcomes(consumer: OutcomeConsumer): () => void {
    outcomeConsumers.add(consumer);
    for (const [outcome, origin] of pendingOutcomes.splice(0)) consumer(outcome, origin);
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
    const attempt = chainFor(file);
    // 新的一次尝试接手:这一串的后台对账让给它(这次尝试自己会按标识认领)。别的串不动。
    stopReconcile(attempt);
    activeFile = file;
    const { uploadId } = attempt;
    const origin: UploadOrigin = { file, uploadId };
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
        retireChain(file, { kind: "job", job: adopted });
        deliver({ kind: "uploaded", job: adopted, fileName: file.name }, origin);
        return;
      }
      if (jobs && activeTransferCount(jobs) >= MAX_ACTIVE_TRANSFER_JOBS) {
        // 预检拒绝不是确定的结局:标识留着。
        deliver({ kind: "failed", text: TRANSFER_BUSY_TEXT }, origin);
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
      retireChain(file, { kind: "job", job });
      deliver({ kind: "uploaded", job, fileName: file.name }, origin);
    } catch (error) {
      if (!current()) return;
      const interrupted = isAbort(error) || error instanceof TypeError;
      if (interrupted && !requestStarted) {
        // 停在预检那一步:这一次的请求根本没发出。标识留着;上一次的结果未定就照实说。
        if (carried) deliverUnsure(file, IMPORT_PRECHECK_STOP_UNSURE_TEXT);
        else deliver({ kind: "stopped" }, origin);
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
          // 认领到作业就以它为凭据结束这一串;「没被接收」出自作业列表里没看到,那不是凭据:
          // 标识留着(再传同一个文件仍用它,无害)。
          if (outcome.kind === "uploaded") retireChain(file, { kind: "job", job: outcome.job });
          deliver(outcome, origin);
        }
      } else if (uploadRefusedForSure(error)) {
        // 白名单里的确定拒绝(后端在建作业之前说的)只说明**这一次请求**没被接收。有些拒绝(格式、
        // 按声明长度的 413)早于服务端按标识去重,而上一次带这个标识的请求可能还缓存在代理里、
        // 或还占着服务端的预留——所以先问服务端这个标识的去向:
        // * `job`:认领它;
        // * 重传(上一次结果未定):拒绝照实说,标识留着,后台接着对账;
        // * 第一次尝试:去向接口说 `none` 才结束这一串;读不到或 `receiving` 就留着。
        if (httpErrorStatus(error) === 413) forgetTransferLimit();
        const text = transferRequestErrorText(error, IMPORT_UPLOAD_ERRORS, IMPORT_UPLOAD_FAILED_TEXT);
        const answer = await uploadStatusOf(uploadId);
        if (!current()) return;
        if (answer?.status === "job") {
          retireChain(file, { kind: "job", job: answer.job });
          deliver({ kind: "uploaded", job: answer.job, fileName: file.name }, origin);
        } else {
          deliver({ kind: "failed", text }, origin);
          if (carried || answer?.status === "receiving") {
            leaveUnresolved(file);
            startReconcile(file);
          } else if (answer?.status === "none") {
            retireChain(file, { kind: "server_none" });
          }
        }
      } else {
        // 其余一切回答(5xx、502/503/504、408、429、不认得的状态码或正文、没有认得的码的
        // 「像后端」的拒绝、409 upload_in_progress、响应读不懂):说不准——请求体可能已经到了
        // 服务器,它可能还会建出作业。按标识对账;没认领到就留着标识,不叫人马上重传。
        const job = await jobOfUpload(uploadId);
        if (!current()) return;
        if (job) {
          retireChain(file, { kind: "job", job });
          deliver({ kind: "uploaded", job, fileName: file.name }, origin);
        } else {
          deliverUnsure(file, uploadStillInProgress(error)
            ? transferRequestErrorText(error, {}, IMPORT_UPLOAD_UNSURE_TEXT)
            : IMPORT_RESPONSE_UNSURE_TEXT);
        }
      }
    } finally {
      if (current()) {
        controller = null;
        activeFile = null;
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
    for (const found of chains.values()) stopReconcile(found);
    chains.clear();
    activeFile = null;
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
