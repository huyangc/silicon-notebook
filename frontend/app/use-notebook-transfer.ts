"use client";

// 网页导出 / 导入单本笔记本的两个状态机(计划 §7、§9)。界面在 notebook-transfer-panel.tsx。
//
// 作业在服务端跑,关掉弹窗不影响它:状态机只在面板可见时轮询,关掉就停,下次打开先从
// `GET /notebook-transfers` 接回;不可见期间由待确认中心(铃铛)呈现进行中的作业。
//
// * `useNotebookExport(notebookId)`:一本笔记本的导出。住在面板组件里,面板一卸载就停;
//   重新打开时按「这本笔记本最新的那个导出作业」接回。
// * `useNotebookImport({ open, ... })`:导入。住在 page 的 Home 里(只调用一次)。上传本身
//   在模块级的 `importUpload`(notebook-import-upload.ts):进度只重渲订阅它的弹窗,Home
//   卸载也不丢;这里只接它的结局,并管导入记录。登出(`leaveActor`)时一并中止、清空。
//
// 每个会持续的动作在途期间都有自己的忙碌位(按钮据此禁用,防止连点重复提交),每个结果都
// 落在发起它的那颗按钮旁边。轮询与取消/移除抢跑时,以取消/移除的结果为准:正在取消或
// 移除的那一行不轮询,已经取消、移除或不在了的作业,迟到的读结果一律丢掉。

import { useCallback, useEffect, useRef, useState } from "react";

import { exportDeletedNotebookMemories } from "./notebook-deletion-api.ts";
import { EXPORT_TOO_LARGE_TEXT } from "./notebook-exit-copy.ts";
import { saveBlobAsFile } from "./download-file.ts";
import { importUpload, type UploadOutcome } from "./notebook-import-upload.ts";
import {
  DOWNLOAD_FAILED_TEXT,
  EXPORT_START_ERRORS,
  deleteNotebookTransfer,
  downloadNotebookExport,
  formatTransferBytes,
  getNotebookTransfer,
  importedNotebookReady,
  listNotebookTransfers,
  loadTransferLimit,
  startNotebookExport,
  transferActive,
  transferLimitLabel,
  transferRequestErrorText,
  type NotebookTransferJob,
} from "./notebook-transfer-api.ts";

/** 进行中作业的轮询间隔(只在面板可见时)。 */
export const TRANSFER_POLL_MS = 1500;
/** 「已开始下载」在按钮旁停留多久,之后按钮回到「下载」。 */
export const DOWNLOAD_STARTED_HOLD_MS = 4000;

/** 两次读到的同一个作业有没有变(作业很小,逐字比)。 */
function sameJob(left: NotebookTransferJob, right: NotebookTransferJob): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

// ======================================================================== 导出

export type DownloadResult = "idle" | "started" | "failed";
export type MemoryExportResult = "idle" | "exported" | "failed";

export type NotebookExportView = {
  /** 正在读取这本笔记本已有的导出作业。 */
  loading: boolean;
  /** 这本笔记本最新的导出作业(进行中的优先);没有为 `null`。 */
  job: NotebookTransferJob | null;
  limitBytes: number | null;
  /** 「导出笔记本」的请求在途。 */
  starting: boolean;
  startError: string;
  /** 取消之后的一句结果(落在开始按钮旁)。 */
  notice: string;
  downloading: boolean;
  downloadResult: DownloadResult;
  downloadError: string;
  cancelling: boolean;
  cancelError: string;
  memoryExporting: boolean;
  memoryResult: MemoryExportResult;
  /** 导出记忆没有成功、或没有可导出的记忆时的那句话。 */
  memoryText: string;
};

const EXPORT_DOWNLOAD_ERRORS = {
  404: "这次导出已经过期或被删除，请重新导出。",
  409: "导出还没有完成，暂时不能下载。",
};

function pickExportJob(jobs: NotebookTransferJob[], notebookId: string): NotebookTransferJob | null {
  const mine = jobs.filter((job) => job.kind === "export" && job.notebook_id === notebookId);
  return mine.find(transferActive) ?? mine[0] ?? null;
}

export function useNotebookExport(notebookId: string) {
  const [loading, setLoading] = useState(true);
  const [job, setJob] = useState<NotebookTransferJob | null>(null);
  const [limitBytes, setLimitBytes] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState("");
  const [notice, setNotice] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [downloadResult, setDownloadResult] = useState<DownloadResult>("idle");
  const [downloadError, setDownloadError] = useState("");
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const [memoryExporting, setMemoryExporting] = useState(false);
  const [memoryResult, setMemoryResult] = useState<MemoryExportResult>("idle");
  const [memoryText, setMemoryText] = useState("");
  // 同步闸:state 更新是异步的,同一渲染周期里的连点要在它生效前挡住。
  const busyRef = useRef({ start: false, download: false, cancel: false, memory: false });
  // 换了笔记本 / 卸载之后,迟到的响应一律不落屏。
  const generationRef = useRef(0);
  // 已经取消或删掉的作业:迟到的轮询结果不能把它复活成「进行中」。
  const goneRef = useRef(new Set<string>());
  // 轮询读回来的和手里的一样,就连 setState 都不调(一次也不重渲)。
  const jobRef = useRef(job);
  jobRef.current = job;

  useEffect(() => {
    const generation = ++generationRef.current;
    const controller = new AbortController();
    setLoading(true);
    setJob(null);
    setStartError("");
    setNotice("");
    setDownloadResult("idle");
    setDownloadError("");
    setCancelError("");
    setMemoryResult("idle");
    setMemoryText("");
    void (async () => {
      const [jobs, limit] = await Promise.all([
        listNotebookTransfers(controller.signal).catch(() => [] as NotebookTransferJob[]),
        loadTransferLimit(),
      ]);
      if (generation !== generationRef.current) return;
      setJob(pickExportJob(jobs.filter((item) => !goneRef.current.has(item.id)), notebookId));
      setLimitBytes(limit);
      setLoading(false);
    })();
    return () => {
      generationRef.current += 1;
      controller.abort();
    };
  }, [notebookId]);

  // 进行中就轮询,结束(或作业不在了)就停;取消在途时不轮询。
  const activeJobId = job && transferActive(job) && !cancelling ? job.id : "";
  useEffect(() => {
    if (!activeJobId) return;
    const generation = generationRef.current;
    const controller = new AbortController();
    let stopped = false;
    let timer = 0;
    const round = async () => {
      try {
        const next = await getNotebookTransfer(activeJobId, controller.signal);
        if (stopped || generation !== generationRef.current || goneRef.current.has(activeJobId)) return;
        if (next === null) {
          goneRef.current.add(activeJobId);
          setJob((current) => (current?.id === activeJobId ? null : current));
          setNotice("这次导出已经不在了，可以重新导出。");
          return;
        }
        const current = jobRef.current;
        if (current?.id === next.id && !sameJob(current, next)) setJob(next);
      } catch {
        // 一次读失败:下一轮再读。
      }
      if (!stopped) timer = window.setTimeout(() => { void round(); }, TRANSFER_POLL_MS);
    };
    timer = window.setTimeout(() => { void round(); }, TRANSFER_POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [activeJobId]);

  useEffect(() => {
    if (downloadResult !== "started") return;
    const timer = window.setTimeout(() => setDownloadResult("idle"), DOWNLOAD_STARTED_HOLD_MS);
    return () => window.clearTimeout(timer);
  }, [downloadResult]);

  useEffect(() => {
    if (memoryResult !== "exported") return;
    const timer = window.setTimeout(() => setMemoryResult("idle"), DOWNLOAD_STARTED_HOLD_MS);
    return () => window.clearTimeout(timer);
  }, [memoryResult]);

  const start = useCallback(async () => {
    if (busyRef.current.start) return;
    busyRef.current.start = true;
    const generation = generationRef.current;
    setStarting(true);
    setStartError("");
    setNotice("");
    setDownloadResult("idle");
    setDownloadError("");
    try {
      const started = await startNotebookExport(notebookId);
      if (generation === generationRef.current) setJob(started);
    } catch (error) {
      if (generation === generationRef.current) {
        setStartError(transferRequestErrorText(error, EXPORT_START_ERRORS, "导出没有开始，请重试。"));
      }
    } finally {
      busyRef.current.start = false;
      if (generation === generationRef.current) setStarting(false);
    }
  }, [notebookId]);

  const download = useCallback(async () => {
    if (!job || busyRef.current.download) return;
    busyRef.current.download = true;
    const generation = generationRef.current;
    setDownloading(true);
    setDownloadResult("idle");
    setDownloadError("");
    try {
      await downloadNotebookExport(job.id);
      if (generation === generationRef.current) setDownloadResult("started");
    } catch (error) {
      if (generation === generationRef.current) {
        setDownloadResult("failed");
        setDownloadError(transferRequestErrorText(error, EXPORT_DOWNLOAD_ERRORS, DOWNLOAD_FAILED_TEXT));
      }
    } finally {
      busyRef.current.download = false;
      if (generation === generationRef.current) setDownloading(false);
    }
  }, [job]);

  // 后端对导出的 DELETE 只会回 `cancelled`(在跑的被停下)或 `deleted`(已经结束、连文件
  // 一起删掉);`pending` 只属于导入(等它决定笔记本留不留)。
  const cancel = useCallback(async () => {
    if (!job || busyRef.current.cancel) return;
    busyRef.current.cancel = true;
    const generation = generationRef.current;
    const target = job.id;
    setCancelling(true);
    setCancelError("");
    try {
      const { outcome } = await deleteNotebookTransfer(target);
      if (generation !== generationRef.current) return;
      goneRef.current.add(target);
      setJob((current) => (current?.id === target ? null : current));
      setNotice(outcome === "cancelled"
        ? "已取消导出。"
        : "这次导出刚好已经结束，导出文件已删除；需要的话请重新导出。");
    } catch (error) {
      if (generation === generationRef.current) {
        setCancelError(transferRequestErrorText(error, {}, "没能取消，请重试。"));
      }
    } finally {
      busyRef.current.cancel = false;
      if (generation === generationRef.current) setCancelling(false);
    }
  }, [job]);

  const exportMemories = useCallback(async () => {
    if (busyRef.current.memory) return;
    busyRef.current.memory = true;
    const generation = generationRef.current;
    setMemoryExporting(true);
    setMemoryResult("idle");
    setMemoryText("");
    const failed = () => {
      setMemoryResult("failed");
      setMemoryText("记忆没有导出，请重试。");
    };
    try {
      const outcome = await exportDeletedNotebookMemories(notebookId);
      if (generation !== generationRef.current) return;
      if (outcome.kind === "exported") {
        saveBlobAsFile(outcome.blob, outcome.filename);
        setMemoryResult("exported");
      } else {
        setMemoryResult("failed");
        setMemoryText(
          outcome.kind === "nothing" ? "这本笔记本里没有你的记忆，不需要另外导出。"
            : outcome.kind === "too_large" ? EXPORT_TOO_LARGE_TEXT
              : "记忆没有导出，请重试。",
        );
      }
    } catch {
      // 读文件内容时断了、或下载没能开始:结果同样落在那几个字旁边,不悄悄回到原样。
      if (generation === generationRef.current) failed();
    } finally {
      busyRef.current.memory = false;
      if (generation === generationRef.current) setMemoryExporting(false);
    }
  }, [notebookId]);

  const view: NotebookExportView = {
    loading, job, limitBytes, starting, startError, notice, downloading, downloadResult,
    downloadError, cancelling, cancelError, memoryExporting, memoryResult, memoryText,
  };
  return { view, start, download, cancel, exportMemories };
}

export type NotebookExportController = ReturnType<typeof useNotebookExport>;

// ======================================================================== 导入

/** 一行导入记录被取消 / 移除之后,留在原位置上的那句结果。 */
export type ImportRowNote = {
  tone: "done" | "kept";
  text: string;
  /** `kept`:留下来的笔记本,可以直接打开。 */
  notebookId?: string;
};

/** 一行导入记录自己的在途动作与结果。 */
export type ImportRowState = {
  busy?: "cancel" | "remove";
  /** 取消已经提交、服务端还在收尾(`pending`)。 */
  stopping?: boolean;
  /** 这一行上一次动作没成功的那句话(就落在按钮旁)。 */
  failure?: string;
};

export type NotebookImportView = {
  limitBytes: number | null;
  file: File | null;
  /** 文件选择器的身份:上传成功后换一个,清掉已选的文件。 */
  fileInputKey: number;
  name: string;
  /** 选中的文件本地就能判定不行(扩展名、超过上限)时的那句话;此时不能开始。 */
  fileError: string;
  uploadError: string;
  /** 上传结束(成功 / 主动停止)后的一句话。「正在导入」那一句只在那个作业还在导入时说。 */
  uploadNotice: string;
  /** 上一次上传失败、而弹窗当时关着:工具栏的「导入」按钮说「导入失败」,直到弹窗打开。 */
  uploadFailedUnseen: boolean;
  /** 我的导入作业(新的在前);只在弹窗打开过之后才有。 */
  jobs: NotebookTransferJob[];
  /** 正在读取导入记录。 */
  loadingJobs: boolean;
  rows: Readonly<Record<string, ImportRowState>>;
  notes: Readonly<Record<string, ImportRowNote>>;
  /** 本页上传过的文件名(作业发布前还没有笔记本名时,行里就说它)。 */
  fileNames: Readonly<Record<string, string>>;
};

const SNBK = /\.snbk$/i;

/** 选中的文件在本地就能判出来的问题(不预检拿不到的上限)。 */
export function importFileProblem(file: File | null, limitBytes: number | null): string {
  if (!file) return "";
  if (!SNBK.test(file.name)) return "请选择笔记本导出文件（.snbk）。";
  if (file.size === 0) return "这个文件是空的。";
  if (limitBytes !== null && file.size > limitBytes) {
    return `这个文件有 ${formatTransferBytes(file.size)}，超过网页导入上限 ${transferLimitLabel(limitBytes)}；`
      + "请联系管理员用命令行导入。";
  }
  return "";
}

export function useNotebookImport(options: {
  /** 导入弹窗此刻是否打开:只在打开时读记录、轮询进行中的作业。 */
  open: boolean;
  /** 一本新笔记本出现了(导入发布,或取消时它已经留下):刷新笔记本列表。 */
  onNotebookReady: (notebookId: string) => void;
}) {
  const { open } = options;
  const onReadyRef = useRef(options.onNotebookReady);
  onReadyRef.current = options.onNotebookReady;
  const [limitBytes, setLimitBytes] = useState<number | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [name, setName] = useState("");
  const [uploadError, setUploadError] = useState("");
  // 上传之后那句话。说「正在导入」时记着是哪个作业:它结束(或已经发布)、或那一行被取消/
  // 移除了,这句话就不再成立,不显示。
  const [notice, setNotice] = useState<{ text: string; jobId?: string }>({ text: "" });
  const [uploadFailedUnseen, setUploadFailedUnseen] = useState(false);
  const openRef = useRef(open);
  openRef.current = open;
  const [jobs, setJobs] = useState<NotebookTransferJob[]>([]);
  const [loadingJobs, setLoadingJobs] = useState(false);
  const [rows, setRows] = useState<Record<string, ImportRowState>>({});
  const [notes, setNotes] = useState<Record<string, ImportRowNote>>({});
  const [fileNames, setFileNames] = useState<Record<string, string>>({});
  // 正在取消/移除的行:不轮询它,读回来的结果也不要。
  const rowBusyRef = useRef(new Set<string>());
  // 已经取消、移除或不在了的作业:迟到的读结果一律丢掉。
  const goneRef = useRef(new Set<string>());
  // 这一页上传出来的作业:打开弹窗时读回的列表里若还没有它(列表请求早于上传落定),不丢。
  const uploadedRef = useRef(new Set<string>());
  // 已经报告过「新笔记本出现了」的作业:每个作业只报一次。
  const announcedRef = useRef(new Set<string>());
  // 登出之后,迟到的响应一律不落屏。
  const actorGenerationRef = useRef(0);
  const notesRef = useRef(notes);
  notesRef.current = notes;
  // 轮询读回来的和手里的一样,就连 setState 都不调:Home 一次也不重渲。
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  const announce = useCallback((jobId: string, notebookId: string) => {
    if (announcedRef.current.has(jobId)) return;
    announcedRef.current.add(jobId);
    onReadyRef.current(notebookId);
  }, []);

  /** 把读到的作业并进列表(没有变化就不换数组,Home 不重渲);能打开的那一刻报告一次。 */
  const mergeJob = useCallback((next: NotebookTransferJob) => {
    if (importedNotebookReady(next)) announce(next.id, next.notebook_id);
    const known = jobsRef.current.find((job) => job.id === next.id);
    if (known && !sameJob(known, next)) {
      setJobs((current) => current.map((job) => (job.id === next.id ? next : job)));
    }
    // 结束了,或已经发布(取消没赶上,笔记本留下、作业接着跑):「正在停止」不再成立。
    if ((!transferActive(next) || importedNotebookReady(next)) && rowsRef.current[next.id]?.stopping) {
      setRows((current) => ({ ...current, [next.id]: { ...current[next.id], stopping: false } }));
    }
  }, [announce]);

  // 上传的结局:当时挂着的这个状态机接,或者(Home 刚重新挂载)接住之前落定的那些。
  useEffect(() => importUpload.consumeOutcomes((outcome: UploadOutcome) => {
    if (outcome.kind === "uploaded") {
      const { job } = outcome;
      uploadedRef.current.add(job.id);
      // 小文件的导入可能在上传的响应回来之前就已经做完:那时它是终态,不会再被轮询,
      // 「新笔记本出现了」只能在这里报(与打开弹窗时那条同一个去重入口)。
      if (importedNotebookReady(job)) announce(job.id, job.notebook_id);
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setFileNames((current) => ({ ...current, [job.id]: outcome.fileName }));
      setFile(null);
      setName("");
      setFileInputKey((key) => key + 1);
      setUploadError("");
      setNotice({ text: "文件已上传，正在导入；进度见下方。关掉这个窗口不影响导入。", jobId: job.id });
    } else if (outcome.kind === "stopped") {
      setUploadError("");
      setNotice({ text: "已停止上传，没有导入任何内容。" });
    } else {
      setNotice({ text: "" });
      setUploadError(outcome.text);
      // 弹窗关着时失败的:失败要落在发起它的那颗按钮(工具栏「导入」)上。
      if (!openRef.current) setUploadFailedUnseen(true);
    }
  }), [announce]);

  // 弹窗一打开,失败原因就在「开始导入」旁边了:工具栏上的「导入失败」撤掉。
  useEffect(() => {
    if (open) setUploadFailedUnseen(false);
  }, [open]);

  // 弹窗打开:读上限与导入记录。
  useEffect(() => {
    if (!open) return;
    const generation = actorGenerationRef.current;
    const controller = new AbortController();
    setLoadingJobs(true);
    void (async () => {
      const [listed, limit] = await Promise.all([
        listNotebookTransfers(controller.signal).catch(() => null),
        loadTransferLimit(),
      ]);
      if (generation !== actorGenerationRef.current || controller.signal.aborted) return;
      setLimitBytes(limit);
      setLoadingJobs(false);
      if (listed === null) return;
      const imports = listed.filter((job) => job.kind === "import" && !goneRef.current.has(job.id));
      // 弹窗关着的时候发布的(或页面刚打开时就已经发布的):这一页还没为它刷新过笔记本列表,
      // 而界面要说「已在列表里」——合并成一次刷新。
      const unannounced = imports.filter((job) => importedNotebookReady(job) && !announcedRef.current.has(job.id));
      for (const job of unannounced) announcedRef.current.add(job.id);
      if (unannounced.length > 0) onReadyRef.current(unannounced[0].notebook_id);
      const listedIds = new Set(imports.map((job) => job.id));
      setJobs((current) => {
        // 列表请求可能早于一次上传落定:这一页上传出来的、列表里还没有的,放在最前;
        // 已经取消/移除、留着结果那几行,作业不会再出现在列表里:原位保留。
        const fresh = current.filter((job) => !listedIds.has(job.id) && uploadedRef.current.has(job.id) && !notesRef.current[job.id]);
        const settled = current.filter((job) => !listedIds.has(job.id) && notesRef.current[job.id]);
        return [...fresh, ...imports, ...settled];
      });
    })();
    return () => controller.abort();
  }, [open]);

  // 弹窗打开时,轮询进行中的作业(取消/移除在途、或已经有结果的行除外)。
  const activeIds = open
    ? jobs.filter((job) => transferActive(job) && !notes[job.id] && !rows[job.id]?.busy).map((job) => job.id).join(",")
    : "";
  useEffect(() => {
    if (!activeIds) return;
    const generation = actorGenerationRef.current;
    const controller = new AbortController();
    let stopped = false;
    let timer = 0;
    const skip = (id: string) => rowBusyRef.current.has(id) || goneRef.current.has(id);
    const round = async () => {
      await Promise.all(activeIds.split(",").map(async (id) => {
        if (skip(id)) return;
        try {
          const next = await getNotebookTransfer(id, controller.signal);
          if (stopped || generation !== actorGenerationRef.current || skip(id)) return;
          if (next === null) {
            goneRef.current.add(id);
            setNotes((current) => (current[id] ? current : { ...current, [id]: { tone: "done", text: "这次导入的记录已经不在了。" } }));
            return;
          }
          mergeJob(next);
        } catch {
          // 一次读失败:下一轮再读。
        }
      }));
      if (!stopped) timer = window.setTimeout(() => { void round(); }, TRANSFER_POLL_MS);
    };
    timer = window.setTimeout(() => { void round(); }, TRANSFER_POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [activeIds, mergeJob]);

  const fileError = importFileProblem(file, limitBytes);

  const chooseFile = useCallback((next: File | null) => {
    setFile(next);
    setUploadError("");
    setNotice({ text: "" });
  }, []);

  const start = useCallback(() => {
    if (!file || importUpload.busy() || importFileProblem(file, limitBytes)) return;
    setUploadError("");
    setNotice({ text: "" });
    void importUpload.start(file, name, limitBytes);
  }, [file, limitBytes, name]);

  const stopUpload = useCallback(() => {
    importUpload.stop();
  }, []);

  const runRowAction = useCallback(async (job: NotebookTransferJob, action: "cancel" | "remove") => {
    if (rowBusyRef.current.has(job.id)) return;
    rowBusyRef.current.add(job.id);
    const generation = actorGenerationRef.current;
    setRows((current) => ({ ...current, [job.id]: { busy: action } }));
    try {
      const { outcome, kept_notebook_id: kept } = await deleteNotebookTransfer(job.id);
      if (generation !== actorGenerationRef.current) return;
      if (outcome === "pending") {
        setRows((current) => ({ ...current, [job.id]: { stopping: true } }));
        return;
      }
      goneRef.current.add(job.id);
      let note: ImportRowNote;
      // 笔记本留没留下只认服务端这一次的回答(`kept_notebook_id`),不认点击时手里的快照:
      // 点下去的那一刻它可能刚好发布。
      if (kept) {
        announce(job.id, kept);
        note = outcome === "kept"
          ? { tone: "kept", text: "取消时笔记本已经导入完成，它已保留在你的笔记本列表里。", notebookId: kept }
          : { tone: "kept", text: "记录已移除，笔记本仍在你的笔记本列表里。", notebookId: kept };
      } else if (outcome === "cancelled") {
        note = { tone: "done", text: "已取消导入，没有留下任何内容。" };
      } else {
        note = { tone: "done", text: "记录已移除。" };
      }
      setNotes((current) => ({ ...current, [job.id]: note }));
      setRows((current) => {
        const next = { ...current };
        delete next[job.id];
        return next;
      });
    } catch (error) {
      if (generation === actorGenerationRef.current) {
        setRows((current) => ({
          ...current,
          [job.id]: { failure: transferRequestErrorText(error, {}, action === "cancel" ? "没能取消，请重试。" : "没能移除，请重试。") },
        }));
      }
    } finally {
      rowBusyRef.current.delete(job.id);
    }
  }, [announce]);

  const cancelJob = useCallback((job: NotebookTransferJob) => runRowAction(job, "cancel"), [runRowAction]);
  const removeJob = useCallback((job: NotebookTransferJob) => runRowAction(job, "remove"), [runRowAction]);

  /** 登出:中止上传,清空一切(下一个人登录后从服务端重新读)。 */
  const leaveActor = useCallback(() => {
    actorGenerationRef.current += 1;
    importUpload.reset();
    rowBusyRef.current.clear();
    goneRef.current.clear();
    uploadedRef.current.clear();
    announcedRef.current.clear();
    setFile(null);
    setFileInputKey((key) => key + 1);
    setName("");
    setUploadError("");
    setNotice({ text: "" });
    setUploadFailedUnseen(false);
    setJobs([]);
    setRows({});
    setNotes({});
    setFileNames({});
    setLimitBytes(null);
  }, []);

  const noticeJob = notice.jobId ? jobs.find((job) => job.id === notice.jobId) : undefined;
  const uploadNotice = notice.jobId
    && (!noticeJob || notes[notice.jobId] || !transferActive(noticeJob) || importedNotebookReady(noticeJob))
    ? ""
    : notice.text;
  const view: NotebookImportView = {
    limitBytes, file, fileInputKey, name, fileError, uploadError, uploadNotice, uploadFailedUnseen,
    jobs, loadingJobs, rows, notes, fileNames,
  };
  return { view, chooseFile, setName, start, stopUpload, cancelJob, removeJob, leaveActor };
}

export type NotebookImportController = ReturnType<typeof useNotebookImport>;
