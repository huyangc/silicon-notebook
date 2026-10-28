// 网页导出 / 导入单本笔记本(`.snbk`)—— 网络客户端与纯文案(不依赖 React)。
//
// 计划 docs/superpowers/plans/2026-10-22-notebook-export-import.md §7、§9。端点(后端
// `backend/app/api/notebook_transfer_routes.py`):
//
//   * POST   /notebooks/{id}/export                 202 + 作业;同一本在进行中的导出再要一次返回同一个作业;
//   * POST   /notebook-imports?name=                原始 `application/zip` 请求体,202 + 作业;
//   * GET    /notebook-transfers[/{job}]            我的作业(新的在前)/ 一个作业;
//   * POST   /notebook-transfers/{job}/download-ticket   一次性下载票据 {url, expires_at};
//   * DELETE /notebook-transfers/{job}              取消或删除,回 {outcome, kept_notebook_id}。
//
// 下载走票据(§9.4):票据 60 秒、只能用一次,浏览器拿它**原生**流式下载——隐藏的
// `<a download>` 导航,不读成 Blob(导出文件可达几百 MB)。票据与它的 URL 只活在
// `downloadNotebookExport` 这一次调用里:不进 state、不进 storage、不进日志
// (`performApiRequest` 只记路径,而票据在响应体里,路径上没有它)。
//
// 生产前端是 http://<IP>:3000,不是 Secure Context:这里不碰 crypto.randomUUID、
// navigator.clipboard 之类的受限 API。

import { performApiRequest, performApiUpload, requestJson } from "./api-client.ts";
import { API_BASE } from "./api-config.ts";
import {
  GENERIC_USER_ERROR,
  httpErrorStatus,
  humanizeHttpError,
  humanizedError,
  throwHumanizedHttpError,
  toUserMessage,
} from "./errors.ts";
import { importPublished } from "./notebook-transfer-phases.ts";
import { fetchSystemConfiguration } from "./system-api.ts";
import { CHECKUP_FIX, label } from "./vocabulary.ts";

const TAG = "notebook-transfer";

export type TransferKind = "export" | "import";
export type TransferState = "queued" | "running" | "succeeded" | "failed" | "cancelled";

/** 一个导出或导入作业(`NotebookTransferJob`)。 */
export type NotebookTransferJob = {
  id: string;
  kind: TransferKind;
  state: TransferState;
  phase: string;
  /** 导出:被导出的笔记本;导入:新笔记本(从占位行起就有,发布前它还不能打开)。 */
  notebook_id: string;
  notebook_name: string;
  created_at: string;
  finished_at: string | null;
  /** 作业(与导出文件)保留到这一刻。 */
  expires_at: string | null;
  progress: { bytes?: number; rows?: number; rows_total?: number };
  archive_bytes: number;
  downloadable: boolean;
  downloaded: boolean;
  report: Record<string, unknown> | null;
  reembed: Record<string, unknown> | null;
  index: Record<string, unknown> | null;
  /** 失败原因的稳定码(`error.code`);没有失败为 ""。原因的文案见 `transferFailureText`。 */
  failure_code: string;
  /** 导入:建出它的那次上传的上传标识(`X-Upload-Id`);没有为 ""。 */
  client_upload_id: string;
};

export type TransferDeleteOutcome = "deleted" | "cancelled" | "kept" | "pending";

export type TransferDeleted = {
  outcome: TransferDeleteOutcome;
  /** `kept`:已经建好、留下来的那本笔记本。 */
  kept_notebook_id: string | null;
};

const STATES: ReadonlySet<string> = new Set(["queued", "running", "succeeded", "failed", "cancelled"]);
const OUTCOMES: ReadonlySet<string> = new Set(["deleted", "cancelled", "kept", "pending"]);

/** 进行中的两个状态:后端在这两态里还会推进它。 */
export function transferActive(job: Pick<NotebookTransferJob, "state">): boolean {
  return job.state === "queued" || job.state === "running";
}

/** 这个导入作业的新笔记本已经能打开(判据与铃铛共用,见 notebook-transfer-phases.ts)。 */
export function importedNotebookReady(job: NotebookTransferJob): boolean {
  return importPublished(job);
}

/** 进行中的作业数(导出与导入一起算:后端每人最多同时 `MAX_ACTIVE_TRANSFER_JOBS` 个)。 */
export const MAX_ACTIVE_TRANSFER_JOBS = 2;
export function activeTransferCount(jobs: readonly NotebookTransferJob[]): number {
  return jobs.filter(transferActive).length;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const count = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
const text = (value: unknown): string => (typeof value === "string" ? value : "");

/** 失败原因只取稳定码——它是后端闭集里的一个短标识,经 `transferFailureText` 查表成中文;
 *  后端同时给的那句 `message` 不在这里读,界面上的话只出自本模块的词表。 */
function failureCodeOf(job: Record<string, unknown>): string {
  const failure = job.error;
  return isRecord(failure) && typeof failure.code === "string" ? failure.code : "";
}

/** 形状不对的作业一律丢掉:读不懂的数据不能变成一个能点的按钮。 */
export function parseTransferJob(value: unknown): NotebookTransferJob | null {
  if (!isRecord(value)) return null;
  if (typeof value.id !== "string" || !value.id) return null;
  if (value.kind !== "export" && value.kind !== "import") return null;
  if (typeof value.state !== "string" || !STATES.has(value.state)) return null;
  const progress = isRecord(value.progress) ? value.progress : {};
  return {
    id: value.id,
    kind: value.kind,
    state: value.state as TransferState,
    phase: text(value.phase),
    notebook_id: text(value.notebook_id),
    notebook_name: text(value.notebook_name),
    created_at: text(value.created_at),
    finished_at: typeof value.finished_at === "string" ? value.finished_at : null,
    expires_at: typeof value.expires_at === "string" ? value.expires_at : null,
    progress: {
      bytes: count(progress.bytes),
      rows: count(progress.rows),
      rows_total: count(progress.rows_total),
    },
    archive_bytes: count(value.archive_bytes) ?? 0,
    downloadable: value.downloadable === true,
    downloaded: value.downloaded === true,
    report: isRecord(value.report) ? value.report : null,
    reembed: isRecord(value.reembed) ? value.reembed : null,
    index: isRecord(value.index) ? value.index : null,
    failure_code: failureCodeOf(value),
    client_upload_id: text(value.client_upload_id),
  };
}

function requireJob(value: unknown): NotebookTransferJob {
  const job = parseTransferJob(value);
  if (!job) throw humanizedError("服务返回的作业信息无法识别，请刷新后重试。");
  return job;
}

const jobPath = (jobId: string) => `/notebook-transfers/${encodeURIComponent(jobId)}`;

/** 开始导出这本笔记本(这本有进行中的导出时,拿回的就是那一个)。 */
export async function startNotebookExport(notebookId: string): Promise<NotebookTransferJob> {
  return requireJob(await requestJson<unknown>(
    `/notebooks/${encodeURIComponent(notebookId)}/export`,
    { method: "POST", tag: TAG },
  ));
}

/** 我的导出与导入作业,新的在前。 */
export async function listNotebookTransfers(signal?: AbortSignal): Promise<NotebookTransferJob[]> {
  const body = await requestJson<{ jobs?: unknown }>("/notebook-transfers", { tag: TAG, signal });
  if (!Array.isArray(body.jobs)) return [];
  return body.jobs.map(parseTransferJob).filter((job): job is NotebookTransferJob => job !== null);
}

/** 读一个作业;它已经不在了(过期、被删、不是我的)时返回 `null`。 */
export async function getNotebookTransfer(
  jobId: string,
  signal?: AbortSignal,
): Promise<NotebookTransferJob | null> {
  const response = await performApiRequest(jobPath(jobId), { tag: TAG, signal });
  if (response.status === 404) return null;
  if (!response.ok) await throwHumanizedHttpError(response, TAG);
  return requireJob(await response.json());
}

/** 取消进行中的作业,或删除已经结束的作业(连同它的文件);它已经不在了也当作删掉了。 */
export async function deleteNotebookTransfer(jobId: string): Promise<TransferDeleted> {
  const response = await performApiRequest(jobPath(jobId), { method: "DELETE", tag: TAG });
  if (response.status === 404) return { outcome: "deleted", kept_notebook_id: null };
  if (!response.ok) await throwHumanizedHttpError(response, TAG);
  const body: unknown = await response.json();
  if (!isRecord(body) || typeof body.outcome !== "string" || !OUTCOMES.has(body.outcome)) {
    throw humanizedError("暂时无法确认结果，请刷新后查看。");
  }
  return {
    outcome: body.outcome as TransferDeleteOutcome,
    kept_notebook_id: typeof body.kept_notebook_id === "string" && body.kept_notebook_id
      ? body.kept_notebook_id
      : null,
  };
}

export type ImportUploadOptions = {
  /** 新笔记本的名字;空串沿用导出时的名字。 */
  name: string;
  /**
   * 这一串上传尝试的标识(`X-Upload-Id`):同一个标识再传一次,服务端回已经建出的那个作业、
   * 不读请求体,所以重传不会多出一本。它也是事后认领「这次上传建出的是哪个作业」的唯一凭据。
   */
  uploadId: string;
  /** 请求体已经整个交给网络。 */
  onBodySent?: () => void;
  signal?: AbortSignal;
  onProgress?: (sent: number, total: number) => void;
};

/** 上传一个导出文件,开始导入为我的一本新笔记本。 */
export async function uploadNotebookImport(
  file: Blob,
  options: ImportUploadOptions,
): Promise<NotebookTransferJob> {
  const query = new URLSearchParams({ name: options.name.trim() }).toString();
  const response = await performApiUpload(`/notebook-imports?${query}`, file, {
    tag: TAG,
    contentType: "application/zip",
    signal: options.signal,
    onProgress: options.onProgress,
    onBodySent: options.onBodySent,
    headers: { [UPLOAD_ID_HEADER]: options.uploadId },
  });
  if (!response.ok) {
    const origin = await refusalOrigin(response.clone());
    const definitive = isDefinitiveUploadRefusal(response.status, origin);
    let error: unknown;
    if (response.status === 400 && origin.code === "archive_too_new") {
      // 一个更新版本的部署写的文件:后端用结构化 detail 的稳定码说(不带出处头),补救是
      // 升级本部署,不是换一个文件——按码给我们自己的那句话。
      error = humanizedError(ARCHIVE_TOO_NEW_TEXT, 400);
    } else if (response.status === 409 && origin.code === "upload_in_progress") {
      // 同一个标识的上一次上传还在服务端接收/检查:这一次没有被接收,上一次结果未定。
      error = humanizedError(UPLOAD_IN_PROGRESS_TEXT, 409);
      stillInProgress.add(error as Error);
    } else {
      error = await throwHumanizedHttpError(response, TAG).catch((thrown: unknown) => thrown);
    }
    if (definitive && error instanceof Error) definitiveRefusals.add(error);
    throw error;
  }
  return requireJob(await response.json());
}

/** 一次失败响应的出处:后端 `user_error()` 的出处头、结构化 detail 的稳定码、是不是后端
 *  形状的 JSON(`{"detail": …}`)。 */
export type RefusalOrigin = { userMessage: boolean; code: string; backendJson: boolean };

async function refusalOrigin(response: Response): Promise<RefusalOrigin> {
  const userMessage = response.headers.get("X-User-Message") === "1";
  try {
    const body: unknown = await response.json();
    const detail = isRecord(body) ? body.detail : undefined;
    return {
      userMessage,
      code: isRecord(detail) && typeof detail.code === "string" ? detail.code : "",
      backendJson: isRecord(body) && detail !== undefined,
    };
  } catch {
    return { userMessage, code: "", backendJson: false };
  }
}

/**
 * 上传被拒、而且**确定没有被接收**的那几种回答:我们自己的后端认得出来地给出、又发生在
 * 任何作业可能存在之前(白名单,按状态码逐条列出)。
 *
 * 默认是**说不准**,不是「被拒」:请求体整个到了服务器之后,它还要检查文件、数文档、建作业,
 * 这期间反向代理可能先回一个 504(或 502/503/500、408、429……),而后端照样把作业建出来。
 * 把这种回答当成确定拒绝、换掉上传标识,下一次重传就会多出一本。所以只有下面这些——后端的
 * 出处头(`user_error()` 的 `X-User-Message: 1`)或结构化 detail 的稳定码证明是后端说的,
 * 且后端在建作业之前就这么说——才算确定没被接收;其余一律说不准,留着标识按它对账。
 */
const DEFINITIVE_UPLOAD_REFUSALS: Readonly<Record<number, (origin: RefusalOrigin) => boolean>> = {
  // 文件是空的 / 不是导出文件或已损坏 / 上传标识格式不对(user_error),更新版本的文件(结构化码)。
  400: (origin) => origin.userMessage || origin.code === "archive_too_new",
  // 名额已满 / 文档超过每本笔记本的上限(user_error)。`upload_in_progress` 不在这里:说不准。
  409: (origin) => origin.userMessage,
  // 超过网页导入上限(user_error;Next 反代自己回的 413 没有出处头,不算)。
  413: (origin) => origin.userMessage,
  // 不是 application/zip(user_error)。
  415: (origin) => origin.userMessage,
  // 会话失效 / 没有权限:后端的 `{"detail": …}`,在读请求体之前。
  401: (origin) => origin.backendJson,
  403: (origin) => origin.backendJson,
};

/** 这个失败响应是不是白名单里的确定拒绝(其余一律说不准)。 */
export function isDefinitiveUploadRefusal(status: number, origin: RefusalOrigin): boolean {
  const rule = DEFINITIVE_UPLOAD_REFUSALS[status];
  return Boolean(rule && rule(origin));
}

const definitiveRefusals = new WeakSet<Error>();

/** 这个上传错误是不是后端在建作业之前给出的确定拒绝(`DEFINITIVE_UPLOAD_REFUSALS`)。 */
export function uploadRefusedForSure(error: unknown): boolean {
  return error instanceof Error && definitiveRefusals.has(error);
}

export const ARCHIVE_TOO_NEW_TEXT = "这个文件来自更新版本的部署，请先升级本部署再导入。";

/** 上传标识的请求头(后端 `notebook_transfer_routes.UPLOAD_ID_HEADER`)。 */
export const UPLOAD_ID_HEADER = "X-Upload-Id";
export const UPLOAD_IN_PROGRESS_TEXT = "上一次上传的这个文件还在处理中，请稍后查看导入记录。";
const stillInProgress = new WeakSet<Error>();

/** 这个错误是不是「同一个上传标识的上一次上传还在处理」(409 upload_in_progress)。 */
export function uploadStillInProgress(error: unknown): boolean {
  return error instanceof Error && stillInProgress.has(error);
}

// ------------------------------------------------------------------ 下载

/** 票据 URL 的唯一合法形状:服务端只发这一条根相对路径(`DOWNLOAD_PATH`)。 */
const DOWNLOAD_PATH = "/api/notebook-transfers/download?";

/**
 * 服务端给的根相对 URL(`/api/notebook-transfers/download?ticket=…`)按本部署的
 * `API_BASE` 改写,与别处拼 API 地址同一做法:同源反代(`/api`)时就是它自己,
 * 前后端分开部署(`http://host:8000/api`)时指向后端。形状不对就拒绝——绝不导航到
 * 一个服务端以外的地址。
 */
export function transferDownloadHref(url: string, base: string = API_BASE): string {
  if (!url.startsWith(DOWNLOAD_PATH)) throw humanizedError(DOWNLOAD_FAILED_TEXT);
  return `${base.replace(/\/+$/, "")}${url.slice("/api".length)}`;
}

/** 下载用的隐藏 iframe 的标记。 */
const DOWNLOAD_FRAME_ATTR = "data-transfer-download";
/**
 * iframe 留多久再拿掉(约 5 分钟)。往长了取:拿掉一个 iframe 会不会中断一个响应头来得
 * 很慢的附件下载,各浏览器的行为我们没能逐一核实。每一次点击最多留下一个,数量有界。
 */
export const DOWNLOAD_FRAME_HOLD_MS = 5 * 60_000;

/**
 * 让浏览器自己去下载这个地址,而且**绝不离开当前页**:导航发生在一个隐藏 iframe 里。
 *
 * 不用 `<a download>`:跨源部署(前端 :3000、后端 :8000)时浏览器无视 `download` 属性,
 * 那次导航作用在整个标签页上——票据被拒(过期、用过、作业已删)时后端回的是一页 404
 * JSON,用户的页面就被它替换掉了。iframe 里的附件响应照样交给下载管理器;被拒的那页
 * JSON 只落在看不见的 iframe 里。每个 iframe 只拿掉它自己:加载完(被拒的那一页)或到
 * 点——新的一次下载绝不拿掉上一次的,那可能正在下载。它存在的这段时间里,地址上的票据
 * 已经被兑换(一次性),再拿去用也是 404。
 */
export function startBrowserDownload(href: string): void {
  const frame = document.createElement("iframe");
  frame.setAttribute(DOWNLOAD_FRAME_ATTR, "");
  frame.setAttribute("aria-hidden", "true");
  frame.tabIndex = -1;
  frame.style.display = "none";
  frame.addEventListener("load", () => frame.remove(), { once: true });
  frame.src = href;
  document.body.appendChild(frame);
  window.setTimeout(() => frame.remove(), DOWNLOAD_FRAME_HOLD_MS);
}

export const DOWNLOAD_FAILED_TEXT = "下载没有开始，请重试。";

/** 取一张一次性票据并立刻用它开始下载。票据只活在这一次调用里。 */
export async function downloadNotebookExport(jobId: string): Promise<void> {
  const ticket = await requestJson<unknown>(`${jobPath(jobId)}/download-ticket`, {
    method: "POST",
    tag: TAG,
  });
  const url = isRecord(ticket) && typeof ticket.url === "string" ? ticket.url : "";
  startBrowserDownload(transferDownloadHref(url));
}

// ------------------------------------------------------------------ 文案

const MIB = 1024 * 1024;

/** 字节数的展示。 */
export function formatTransferBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < MIB) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * MIB) return `${(bytes / MIB).toFixed(1)} MB`;
  return `${(bytes / 1024 / MIB).toFixed(2)} GB`;
}

/** 上限的展示:整 MB 就说整数(「512 MB」),否则按普通字节数说。 */
export function transferLimitLabel(limitBytes: number): string {
  return limitBytes % MIB === 0 ? `${limitBytes / MIB} MB` : formatTransferBytes(limitBytes);
}

/** 「10月28日 18:30」;解析不出来返回空串,调用方就整句省略时刻。 */
export function formatTransferDeadline(iso: string | null): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getMonth() + 1}月${at.getDate()}日 ${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/**
 * 段与内容块的检索数据没补全时去哪里补:笔记本「看板」的「来源状态」里那颗修复按钮。
 * 那颗按钮只补段与内容块(体检 H4/H5),所以只在作业信息说没补完的**只有**这两条腿时才
 * 点它的名(`importReportNotes`);别的情况用 `FOLLOWUP_GENERIC`,不承诺一颗可能不在的
 * 按钮。按钮的字取自词表 `CHECKUP_FIX.backfill_vectors` 本身(不抄一份)。说这句话的
 * 地方旁边都有「打开笔记本」。
 */
export const REPAIR_HINT =
  `打开笔记本后在「看板」的「来源状态」里点「${label(CHECKUP_FIX, "backfill_vectors", "补齐")}」补上检索数据。`;

/** 导入后的后台处理没完成、又说不准缺的是哪一块时(知识图谱那两条腿、经验表同步、
 *  或作业没留下各条腿的进度):不点任何一颗按钮的名。 */
export const FOLLOWUP_GENERIC = "后台处理没有完成，部分检索可能不全；可以删除这本笔记本后重新导入。";

/** 失败原因(按稳定码,`jobs.py::_MESSAGES` 与 `too_large` / `invalid_archive`)。 */
const FAILURE_TEXT: Record<string, string> = {
  cancelled: "已取消。",
  notebook_missing: "笔记本不存在或正在删除，无法导出。",
  export_failed: "导出没有完成，请稍后重试。",
  import_failed: "导入没有完成，请稍后重试。",
  // 这两句与后端 `jobs.py::_MESSAGES` 逐字相同(后端测试读本文件核对)。
  import_followup_failed: "笔记本已经导入，但后台处理没有完成，部分检索可能不全；可以删除这本笔记本后重新导入。",
  not_started: "暂时无法开始，请稍后重试。",
  export_interrupted: "服务重启，这次导出没有完成，请重新导出。",
  import_interrupted: "服务重启，这次导入没有完成，请重新导入。",
  import_followup_interrupted: "服务重启时笔记本已经导入，但后台处理没有完成，部分检索可能不全；请不要再导入一份，需要的话可以删除这本笔记本后重新导入。",
  too_large: "导出文件超过网页导出上限，请联系管理员用命令行导出这本笔记本。",
  invalid_archive: "这个文件无法导入：它不是笔记本导出文件，或者已经损坏。",
  archive_too_new: ARCHIVE_TOO_NEW_TEXT,
  // 收割(心跳超时)说的是这一次运行,不是文件:可以原样再导入一次。
  import_reaped: "导入中断了，请重新导入。",
};

export function transferFailureText(job: NotebookTransferJob): string {
  const fallback = job.kind === "import" ? FAILURE_TEXT.import_failed : FAILURE_TEXT.export_failed;
  return job.failure_code ? label(FAILURE_TEXT, job.failure_code, fallback) : fallback;
}

const EXPORT_PHASE_TEXT: Record<string, string> = {
  queued: "排队中",
  writing: "正在写入导出文件",
  done: "导出完成",
};
const IMPORT_PHASE_TEXT: Record<string, string> = {
  queued: "排队中",
  preflight: "正在检查文件",
  files: "正在写入原文件",
  rows: "正在写入内容",
  validate: "正在核对内容",
  publish: "正在生成笔记本",
  project: "正在同步经验表",
  reembed: "正在补全检索数据",
  index: "正在更新索引",
  done: "导入完成",
};

/** 进行中作业的阶段(不带数字):播报区只读它,阶段变了才读一次。 */
export function transferPhaseText(job: NotebookTransferJob): string {
  if (job.state === "queued") return "排队中";
  const table = job.kind === "export" ? EXPORT_PHASE_TEXT : IMPORT_PHASE_TEXT;
  return label(table, job.phase, "正在处理");
}

/** 阶段之后能说的数字(「（已写入 3.0 MB）」「（40 / 100）」);没有为空串。 */
export function transferProgressDetail(job: NotebookTransferJob): string {
  if (job.state === "queued") return "";
  const { bytes, rows, rows_total: total } = job.progress;
  if (job.kind === "export" && job.phase === "writing" && bytes) {
    return `（已写入 ${formatTransferBytes(bytes)}）`;
  }
  if (job.kind === "import" && job.phase === "rows" && rows !== undefined && total) {
    return `（${Math.min(rows, total)} / ${total}）`;
  }
  return "";
}

/** 进行中作业的一句进度:阶段 + 能说的数字。 */
export function transferProgressText(job: NotebookTransferJob): string {
  return `${transferPhaseText(job)}${transferProgressDetail(job)}`;
}

/** 导入发布之后、后台还在补全检索数据时那句话(计划 §9:期间提问只有关键词召回)。 */
export const REEMBED_NOTICE =
  "笔记本已经导入，可以打开使用；后台正在补全检索数据，完成前提问可能找不全相关内容。";

/** 导入完成后值得告诉用户的几件事(都来自作业报告里的计数)。 */
export function importReportNotes(job: NotebookTransferJob): string[] {
  const notes: string[] = [];
  const report = job.report ?? {};
  const reset = count(report.sources_reset) ?? 0;
  if (reset > 0) notes.push(`有 ${reset} 份来源在导出时还没处理完，需要在笔记本里重新解析。`);
  const mounts = count(report.mount_edges_dropped) ?? 0;
  if (mounts > 0) notes.push("原笔记本挂载的参考库没有一起带过来，需要的话请重新挂载。");
  if (job.state === "succeeded") {
    const reembed = job.reembed?.state;
    if (reembed === "failed") {
      notes.push(onlyRepairableLegsIncomplete(job)
        ? `后台补全检索数据没有完成，提问可能找不全相关内容；${REPAIR_HINT}`
        : FOLLOWUP_GENERIC);
    } else if (reembed === "running") {
      notes.push("后台仍在补全检索数据，完成前提问可能找不全相关内容。");
    }
  }
  return notes;
}

/** 看板「补齐向量」补得上的那两条腿(段、内容块;`notebook_reembed` 的腿名)。 */
const REPAIRABLE_LEGS: ReadonlySet<string> = new Set(["chunks", "elements"]);

/**
 * 作业信息是否说明:没补完的腿全都是看板那颗按钮补得上的(至少有一条)。各条腿的进度
 * 缺失、或有知识图谱的腿没补完,都不算——那时点那颗按钮的名,就是承诺一颗可能不在、
 * 或点了也补不上的按钮。
 */
function onlyRepairableLegsIncomplete(job: NotebookTransferJob): boolean {
  const reembed = job.reembed ?? {};
  const legs = Array.isArray(reembed.legs) ? reembed.legs.filter((leg): leg is string => typeof leg === "string") : [];
  const progress = isRecord(reembed.progress) ? reembed.progress : null;
  if (!progress || legs.length === 0) return false;
  const incomplete = legs.filter((leg) => {
    const entry = progress[leg];
    return !(isRecord(entry) && entry.state === "done");
  });
  return incomplete.length > 0 && incomplete.every((leg) => REPAIRABLE_LEGS.has(leg));
}

/** 导出完成后值得告诉用户的事:导出时已经找不到的原文件。 */
export function exportReportNotes(job: NotebookTransferJob): string[] {
  const report = job.report ?? {};
  const missing = (count(report.missing_files) ?? 0) + (count(report.missing_source_files) ?? 0);
  return missing > 0 ? [`有 ${missing} 个原文件在导出时已经找不到，导出文件里没有它们。`] : [];
}

/**
 * 一次请求失败时给用户的话。后端为这些端点写的中文(`user_error()`,带出处标记)原样
 * 透传——它比状态码更具体(上限是多少、是哪一种 409);只有拿到的是按状态码泛化的
 * 通用文案时(跨源部署没暴露出处头、或网关改写),才换成这里按状态码写好的一句。
 */
export function transferRequestErrorText(
  error: unknown,
  byStatus: Readonly<Partial<Record<number, string>>>,
  fallback: string = GENERIC_USER_ERROR,
): string {
  const shown = toUserMessage(error, fallback);
  const status = httpErrorStatus(error);
  if (status === undefined) return shown;
  const specific = byStatus[status];
  return specific && shown === humanizeHttpError(status) ? specific : shown;
}

export const EXPORT_START_ERRORS: Readonly<Partial<Record<number, string>>> = {
  404: "没找到这本笔记本，或你已经不是它的所有者。",
  409: "你已有两个导出或导入正在进行，请等其中一个完成后再试。",
  413: "这本笔记本超过网页导出上限，请联系管理员用命令行导出。",
};

export const IMPORT_UPLOAD_ERRORS: Readonly<Partial<Record<number, string>>> = {
  400: "这个文件无法导入：它不是笔记本导出文件，或者已经损坏。",
  409: "暂时不能导入：你可能已有两个导出或导入正在进行（请等其中一个完成），"
    + "或这个文件的文档超过了每本笔记本的上限（请联系管理员用命令行导入）。",
  413: "文件超过网页导入上限，请联系管理员用命令行导入。",
  415: "请选择笔记本导出文件（.snbk）。",
};

// 部署的网页导出/导入上限。读到一次就记住(它是部署配置,页面这一生一般不变):打开面板
// 不再每次重新拉 `/system/config`。拿不到(网络)不记,下次再试;旧后端不下发为 `null`。
// 上传被 413 拒绝时忘掉它(`forgetTransferLimit`),下次重新读——上限可能刚被改小。
let cachedLimit: { value: number | null } | null = null;

/** 部署的网页导出/导入上限;拿不到为 `null`,不预检、由服务端兜底。 */
export async function loadTransferLimit(): Promise<number | null> {
  if (cachedLimit) return cachedLimit.value;
  try {
    const value = (await fetchSystemConfiguration()).notebook_transfer_max_bytes;
    cachedLimit = { value };
    return value;
  } catch {
    return null;
  }
}

/** 忘掉记住的上限(上传被 413 拒绝之后;测试也用它)。 */
export function forgetTransferLimit(): void {
  cachedLimit = null;
}

export const IMPORT_UPLOAD_FAILED_TEXT = "上传没有完成，请检查网络后重试。";
/** 上传进行中连接被服务器提前关掉、而事后查不出原因时:服务器在读完请求体之前就会拒绝
 *  名额已满(409)与超过上限(413),浏览器那一侧只看得到一次网络中断。 */
export const IMPORT_UPLOAD_CUT_TEXT =
  "上传没有完成：可能是同时进行的导出或导入已满，或文件超过了上限；也可能是网络中断。请稍后重试。";
export const TRANSFER_BUSY_TEXT = "你已有两个导出或导入正在进行，请等其中一个完成后再试。";
