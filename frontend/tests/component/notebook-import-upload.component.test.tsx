import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { NotebookTransferJob } from "../../app/notebook-transfer-api.ts";

// 模块级的上传仓库:先核对名额、只在整数百分比/阶段变化时通知、传完进入「检查文件」、
// 中途断开时说清原因、上传中挂 beforeunload、结局没人接就先存着。

const api = vi.hoisted(() => ({
  listNotebookTransfers: vi.fn(),
  uploadNotebookImport: vi.fn(),
  forgetTransferLimit: vi.fn(),
  uploadStillInProgress: vi.fn(() => false),
}));
vi.mock("../../app/notebook-transfer-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/notebook-transfer-api.ts")>()),
  ...api,
}));

const reload = vi.hoisted(() => ({ reloading: false }));
vi.mock("../../app/page-reload.ts", () => ({
  pageReloading: () => reload.reloading,
  reloadPage: () => { reload.reloading = true; },
}));

const {
  importUpload, useImportUpload, useImportUploadBusy, IMPORT_UPLOAD_UNSURE_TEXT, IMPORT_STOP_UNSURE_TEXT,
  IMPORT_PRECHECK_STOP_UNSURE_TEXT,
} = await import("../../app/notebook-import-upload.ts");
const { IMPORT_UPLOAD_CUT_TEXT, IMPORT_UPLOAD_FAILED_TEXT, TRANSFER_BUSY_TEXT } = await import(
  "../../app/notebook-transfer-api.ts"
);

function active(id: string, kind: "export" | "import" = "import", uploadId = ""): NotebookTransferJob {
  return {
    id, kind, state: "running", phase: "rows", notebook_id: "", notebook_name: "", created_at: "",
    finished_at: null, expires_at: null, progress: {}, archive_bytes: 0, downloadable: false,
    downloaded: false, report: null, reembed: null, index: null, failure_code: "",
    client_upload_id: uploadId,
  };
}

/** 假上传:记下这次带的上传标识、交出进度与「请求体已传完」两个回调,由用例决定怎么收场。 */
function controllableUpload() {
  const handle = {
    uploadId: "",
    progress: (_sent: number, _total: number) => undefined as void,
    bodySent: () => undefined as void,
    resolve: (_job: NotebookTransferJob) => undefined as void,
    reject: (_error: unknown) => undefined as void,
  };
  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: {
    uploadId: string; onProgress: typeof handle.progress; onBodySent: () => void; signal: AbortSignal;
  }) => new Promise((resolve, reject) => {
    handle.uploadId = options.uploadId;
    handle.progress = options.onProgress;
    handle.bodySent = options.onBodySent;
    handle.resolve = resolve;
    handle.reject = reject;
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  return handle;
}

const file = () => new File([new Uint8Array(100)], "库.snbk");

beforeEach(() => {
  reload.reloading = false;
  api.listNotebookTransfers.mockResolvedValue([]);
});
afterEach(() => {
  importUpload.reset();
});

function collect() {
  const outcomes: unknown[] = [];
  const stop = importUpload.consumeOutcomes((outcome) => outcomes.push(outcome));
  return { outcomes, stop };
}

test("名额已满(进行中的导出与导入一起算)就不上传,原因说清楚", async () => {
  api.listNotebookTransfers.mockResolvedValue([active("a"), active("b", "export")]);
  const { outcomes, stop } = collect();
  await importUpload.start(file(), "", null);
  expect(api.uploadNotebookImport).not.toHaveBeenCalled();
  expect(outcomes).toEqual([{ kind: "failed", text: TRANSFER_BUSY_TEXT }]);
  expect(importUpload.getSnapshot()).toBeNull();
  stop();
});

test("进度只在整数百分比变化时通知;传完进入「正在检查文件」直到服务器回话", async () => {
  let progress: (sent: number, total: number) => void = () => undefined;
  let finish: (job: NotebookTransferJob) => void = () => undefined;
  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: { onProgress: typeof progress }) => {
    progress = options.onProgress;
    return new Promise((resolve) => { finish = resolve; });
  });
  const listener = vi.fn();
  const unsubscribe = importUpload.subscribe(listener);
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "名字", null);
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  const base = listener.mock.calls.length;
  for (let sent = 0; sent <= 1000; sent += 1) progress(sent, 100_000);
  // 0..1000 / 100000 只跨过了 0% → 1%:一千次进度,一次通知。
  expect(listener.mock.calls.length - base).toBe(1);
  expect(importUpload.getSnapshot()?.percent).toBe(1);
  progress(100_000, 100_000);
  expect(importUpload.getSnapshot()).toMatchObject({ percent: 100, stage: "checking" });
  finish(active("imp-1"));
  await running;
  expect(outcomes).toEqual([{ kind: "uploaded", job: active("imp-1"), fileName: "库.snbk" }]);
  expect(importUpload.getSnapshot()).toBeNull();
  unsubscribe();
  stop();
});

test("上传中挂 beforeunload 提示,结束就摘掉", async () => {
  const add = vi.spyOn(window, "addEventListener");
  const remove = vi.spyOn(window, "removeEventListener");
  let finish: (job: NotebookTransferJob) => void = () => undefined;
  api.uploadNotebookImport.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const { stop } = collect();
  const running = importUpload.start(file(), "", null);
  expect(add.mock.calls.some(([type]) => type === "beforeunload")).toBe(true);
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
  finish(active("imp-1"));
  await running;
  expect(remove.mock.calls.some(([type]) => type === "beforeunload")).toBe(true);
  const after = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(after);
  expect(after.defaultPrevented).toBe(false);
  stop();
});

test("中途网络断开、请求体没传完(确定没被接收):名额满了就照实说;确定没超上限就只是网络;否则说出可能的几种原因", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { outcomes, stop } = collect();
  for (const [after, limit] of [
    [[active("a"), active("b", "export")], null],
    [[], 1024],
    [[], null],
  ] as const) {
    const upload = controllableUpload();
    api.listNotebookTransfers.mockResolvedValueOnce([]).mockResolvedValueOnce([...after]).mockResolvedValueOnce([...after]);
    const running = importUpload.start(file(), "", limit);
    await vi.waitFor(() => expect(upload.uploadId).not.toBe(""));
    upload.progress(10, 100);
    upload.reject(new TypeError("Failed to fetch"));
    await running;
  }
  expect(outcomes).toEqual([
    { kind: "failed", text: TRANSFER_BUSY_TEXT },
    { kind: "failed", text: IMPORT_UPLOAD_FAILED_TEXT },
    { kind: "failed", text: IMPORT_UPLOAD_CUT_TEXT },
  ]);
  stop();
});

test("服务器已经接收、连接才断:按上传标识认领到的那个作业照实交出(不报失败)", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const upload = controllableUpload();
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "", 1024);
  await vi.waitFor(() => expect(upload.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockResolvedValueOnce([active("accepted", "import", upload.uploadId), active("old")]);
  upload.bodySent();
  upload.reject(new TypeError("Failed to fetch"));
  await running;
  expect(outcomes).toEqual([{ kind: "uploaded", job: active("accepted", "import", upload.uploadId), fileName: "库.snbk" }]);
  stop();
});

test("别的标签页同时开始的导入不会被当成这一次的", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { outcomes, stop } = collect();
  // 请求体传完后断开:列表里多了一个导入,可它带的是别的上传标识。
  const sent = controllableUpload();
  const first = importUpload.start(file(), "", 1024);
  await vi.waitFor(() => expect(sent.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockResolvedValueOnce([active("other-tab", "import", "up-other-tab-1")]);
  sent.bodySent();
  sent.reject(new TypeError("Failed to fetch"));
  await first;
  // 请求体没传完就断开:同样不认,说清没被接收的原因。
  const partial = controllableUpload();
  const second = importUpload.start(file(), "", 1024);
  await vi.waitFor(() => expect(partial.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockResolvedValueOnce([active("other-tab", "import", "up-other-tab-1")])
    .mockResolvedValueOnce([active("other-tab", "import", "up-other-tab-1")]);
  partial.reject(new TypeError("Failed to fetch"));
  await second;
  expect(outcomes).toEqual([
    { kind: "unsure", text: IMPORT_UPLOAD_UNSURE_TEXT },
    { kind: "failed", text: IMPORT_UPLOAD_FAILED_TEXT },
  ]);
  stop();
});

test("判断不了有没有被接收:说「可能已被接收」,不叫人马上重传", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { outcomes, stop } = collect();
  // 一、请求体已经传完,作业却还没出现(服务器可能还在检查文件)。
  const sent = controllableUpload();
  const first = importUpload.start(file(), "", 1024);
  await vi.waitFor(() => expect(sent.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockResolvedValueOnce([]);
  sent.bodySent();
  sent.reject(new TypeError("Failed to fetch"));
  await first;
  // 二、断开之后作业读不到(哪怕请求体没传完,也说不准)。
  const unread = controllableUpload();
  const second = importUpload.start(file(), "", 1024);
  await vi.waitFor(() => expect(unread.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockRejectedValueOnce(new TypeError("offline"));
  unread.reject(new TypeError("Failed to fetch"));
  await second;
  expect(outcomes).toEqual([
    { kind: "unsure", text: IMPORT_UPLOAD_UNSURE_TEXT },
    { kind: "unsure", text: IMPORT_UPLOAD_UNSURE_TEXT },
  ]);
  expect(IMPORT_UPLOAD_UNSURE_TEXT).toContain("可能已被接收");
  expect(IMPORT_UPLOAD_UNSURE_TEXT).toContain("再决定是否重传");
  stop();
});

test("说不准之后再传同一个文件,用的是同一个上传标识(服务端回已有作业,不会多出一本);换文件或有了确定结局才换新的", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { outcomes, stop } = collect();
  const chosen = file();
  const first = controllableUpload();
  const attempt = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(first.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockResolvedValueOnce([]);
  first.bodySent();
  first.reject(new TypeError("Failed to fetch"));
  await attempt;
  expect(outcomes.at(-1)).toMatchObject({ kind: "unsure" });
  // 重传同一个文件:同一个标识;服务端认出它,回已经建出的那个作业(不读请求体)。
  const retry = controllableUpload();
  const again = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(retry.uploadId).not.toBe(""));
  expect(retry.uploadId).toBe(first.uploadId);
  retry.resolve(active("existing", "import", first.uploadId));
  await again;
  expect(outcomes.at(-1)).toEqual({ kind: "uploaded", job: active("existing", "import", first.uploadId), fileName: "库.snbk" });
  // 有了确定结局:同一个文件再传是新的一串,换新标识。
  const fresh = controllableUpload();
  const third = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(fresh.uploadId).not.toBe(""));
  expect(fresh.uploadId).not.toBe(first.uploadId);
  fresh.resolve(active("second", "import", fresh.uploadId));
  await third;
  // 换了文件:新标识。
  const other = controllableUpload();
  const fourth = importUpload.start(file(), "", 1024);
  await vi.waitFor(() => expect(other.uploadId).not.toBe(""));
  expect(other.uploadId).not.toBe(fresh.uploadId);
  expect(other.uploadId).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
  other.resolve(active("third", "import", other.uploadId));
  await fourth;
  stop();
});

test("同一个标识的上一次上传还在服务端处理(409):说不准,标识留着给重传", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { humanizedError } = await import("../../app/errors.ts");
  const { outcomes, stop } = collect();
  const chosen = file();
  const first = controllableUpload();
  const attempt = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(first.uploadId).not.toBe(""));
  api.uploadStillInProgress.mockReturnValueOnce(true);
  first.reject(humanizedError("上一次上传的这个文件还在处理中，请稍后查看导入记录。", 409));
  await attempt;
  expect(outcomes).toEqual([{ kind: "unsure", text: "上一次上传的这个文件还在处理中，请稍后查看导入记录。" }]);
  const retry = controllableUpload();
  const again = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(retry.uploadId).not.toBe(""));
  expect(retry.uploadId).toBe(first.uploadId);
  retry.resolve(active("x", "import", first.uploadId));
  await again;
  stop();
});

test("没人接的结局先存着,下一个接收者一挂上就拿到(且只拿一次)", async () => {
  api.uploadNotebookImport.mockResolvedValueOnce(active("imp-2"));
  await importUpload.start(file(), "", null);
  const first = collect();
  expect(first.outcomes).toEqual([{ kind: "uploaded", job: active("imp-2"), fileName: "库.snbk" }]);
  first.stop();
  const second = collect();
  expect(second.outcomes).toEqual([]);
  second.stop();
});

test("同一时刻只有一次上传;停止得到 stopped;登出丢掉一切", async () => {
  api.uploadNotebookImport.mockImplementation((_file: File, options: { signal: AbortSignal }) => new Promise((_, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  await importUpload.start(file(), "", null);
  expect(api.uploadNotebookImport).toHaveBeenCalledTimes(1);
  importUpload.stop();
  await running;
  expect(outcomes).toEqual([{ kind: "stopped" }]);
  stop();
  const again = importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  importUpload.reset();
  await again;
  expect(importUpload.getSnapshot()).toBeNull();
  const late = collect();
  expect(late.outcomes).toEqual([]);
  late.stop();
});

test("useImportUpload 只在快照变化时重渲订阅它的组件", async () => {
  let progress: (sent: number, total: number) => void = () => undefined;
  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: { onProgress: typeof progress }) => {
    progress = options.onProgress;
    return new Promise(() => undefined);
  });
  let renders = 0;
  const { result } = renderHook(() => { renders += 1; return useImportUpload(); });
  const { stop } = collect();
  act(() => { void importUpload.start(file(), "", null); });
  await vi.waitFor(() => expect(result.current?.stage).toBe("uploading"));
  const before = renders;
  act(() => { for (let sent = 0; sent < 900; sent += 1) progress(sent, 100_000); });
  expect(renders).toBe(before);
  act(() => progress(50_000, 100_000));
  expect(result.current?.percent).toBe(50);
  expect(renders).toBe(before + 1);
  stop();
});



test("传完之后(正在检查文件)停不下来:stop 什么都不做,结局照实是那次导入", async () => {
  let progress: (sent: number, total: number) => void = () => undefined;
  let finish: (job: NotebookTransferJob) => void = () => undefined;
  let signal: AbortSignal | null = null;
  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: { onProgress: typeof progress; signal: AbortSignal }) => {
    progress = options.onProgress;
    signal = options.signal;
    return new Promise((resolve) => { finish = resolve; });
  });
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  progress(100, 100);
  expect(importUpload.getSnapshot()?.stage).toBe("checking");
  importUpload.stop();
  expect(signal!.aborted).toBe(false);
  finish(active("imp-7"));
  await running;
  expect(outcomes).toEqual([{ kind: "uploaded", job: active("imp-7"), fileName: "库.snbk" }]);
  stop();
});

test("停止与服务端接收抢跑:请求体已经传完、又没认领到作业——说不准,不说「没有导入任何内容」", async () => {
  const upload = controllableUpload();
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(upload.uploadId).not.toBe(""));
  // 「请求体已传完」先到,进度事件还没来得及到 100%:这时按钮仍在(阶段已是 checking 则不在)。
  upload.progress(99, 100);
  upload.bodySent();
  api.listNotebookTransfers.mockResolvedValueOnce([active("other-tab", "import", "up-other-tab-1")]);
  upload.reject(new DOMException("aborted", "AbortError"));
  await running;
  expect(outcomes).toEqual([{ kind: "unsure", text: IMPORT_STOP_UNSURE_TEXT }]);
  stop();
});

test("停止与服务端接收抢跑:认领到了这一次的作业就照实交出", async () => {
  const upload = controllableUpload();
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(upload.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockResolvedValueOnce([active("mine", "import", upload.uploadId)]);
  importUpload.stop();
  await running;
  expect(outcomes).toEqual([{ kind: "uploaded", job: active("mine", "import", upload.uploadId), fileName: "库.snbk" }]);
  stop();
});

test("请求体没传完就停止、也没认领到作业:确定没导入", async () => {
  const upload = controllableUpload();
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(upload.uploadId).not.toBe(""));
  upload.progress(30, 100);
  api.listNotebookTransfers.mockResolvedValueOnce([]);
  importUpload.stop();
  await running;
  expect(outcomes).toEqual([{ kind: "stopped" }]);
  stop();
});

test("页面自己发起的重载(401、换了登录)不被「离开此页」挡住", async () => {
  api.uploadNotebookImport.mockImplementationOnce(() => new Promise(() => undefined));
  const { stop } = collect();
  void importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  const asked = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(asked);
  expect(asked.defaultPrevented).toBe(true);
  reload.reloading = true;
  const reloadingEvent = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(reloadingEvent);
  expect(reloadingEvent.defaultPrevented).toBe(false);
  stop();
});

test("等着解释断线原因的时候登出了:迟到的结局不再交出去", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  api.uploadNotebookImport.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  let explain: (jobs: NotebookTransferJob[]) => void = () => undefined;
  // 预检 → 断开后按标识认领(等着) → 登出。
  api.listNotebookTransfers.mockResolvedValueOnce([])
    .mockImplementationOnce(() => new Promise((resolve) => { explain = resolve; }));
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(api.listNotebookTransfers).toHaveBeenCalledTimes(2));
  importUpload.reset();
  explain([active("a"), active("b")]);
  await running;
  expect(outcomes).toEqual([]);
  stop();
});

test("useImportUploadBusy 只在「有没有上传」翻转时重渲", async () => {
  let progress: (sent: number, total: number) => void = () => undefined;
  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: { onProgress: typeof progress }) => {
    progress = options.onProgress;
    return new Promise(() => undefined);
  });
  let renders = 0;
  const { result } = renderHook(() => { renders += 1; return useImportUploadBusy(); });
  const { stop } = collect();
  act(() => { void importUpload.start(file(), "", null); });
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  expect(result.current).toBe(true);
  const before = renders;
  act(() => { for (let sent = 0; sent <= 100; sent += 5) progress(sent, 100); });
  expect(renders).toBe(before);
  act(() => importUpload.reset());
  expect(result.current).toBe(false);
  stop();
});


test("上传被 413 拒绝:忘掉记住的上限,下次重新读", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { humanizedError } = await import("../../app/errors.ts");
  api.uploadNotebookImport.mockRejectedValueOnce(humanizedError("文件超过网页导入上限", 413));
  const { outcomes, stop } = collect();
  await importUpload.start(file(), "", null);
  expect(api.forgetTransferLimit).toHaveBeenCalledOnce();
  expect(outcomes).toHaveLength(1);
  stop();
});


/** 让同一个文件先落一次「说不准」(请求体传完、连接断了、还没看到作业),交回那次的上传标识。 */
async function leaveUnsure(chosen: File): Promise<string> {
  const first = controllableUpload();
  const attempt = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(first.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockResolvedValueOnce([]);
  first.bodySent();
  first.reject(new TypeError("Failed to fetch"));
  await attempt;
  return first.uploadId;
}

test("说不准之后重传:原来那次的作业已经在、正占着名额——按标识认领它,不再上传", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { outcomes, stop } = collect();
  const chosen = file();
  const uploadId = await leaveUnsure(chosen);
  expect(outcomes.at(-1)).toMatchObject({ kind: "unsure" });
  // 两个名额都占着,其中一个正是原来那次上传建出的作业。
  api.listNotebookTransfers.mockResolvedValueOnce([active("original", "import", uploadId), active("other", "export")]);
  await importUpload.start(chosen, "", 1024);
  expect(api.uploadNotebookImport).toHaveBeenCalledTimes(1);
  expect(outcomes.at(-1)).toEqual({ kind: "uploaded", job: active("original", "import", uploadId), fileName: "库.snbk" });
  stop();
});

test("说不准之后重传被名额挡住:标识留着,再下一次重传带的还是同一个", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { outcomes, stop } = collect();
  const chosen = file();
  const uploadId = await leaveUnsure(chosen);
  api.listNotebookTransfers.mockResolvedValueOnce([active("a"), active("b", "export")]);
  await importUpload.start(chosen, "", 1024);
  expect(outcomes.at(-1)).toEqual({ kind: "failed", text: TRANSFER_BUSY_TEXT });
  expect(api.uploadNotebookImport).toHaveBeenCalledTimes(1);
  const later = controllableUpload();
  const again = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(later.uploadId).not.toBe(""));
  expect(later.uploadId).toBe(uploadId);
  later.resolve(active("dedup", "import", uploadId));
  await again;
  stop();
});

test("说不准之后:预检时停止、或这一次半截就断开,都还是说不准,标识留着", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { outcomes, stop } = collect();
  const chosen = file();
  const uploadId = await leaveUnsure(chosen);
  // 预检时停止:这一次没发请求,可上一次的结果未定。
  let releaseList: (jobs: NotebookTransferJob[]) => void = () => undefined;
  api.listNotebookTransfers.mockImplementationOnce((signal?: AbortSignal) => new Promise((resolve, reject) => {
    releaseList = resolve;
    signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  const prechecking = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("precheck"));
  importUpload.stop();
  await prechecking;
  void releaseList;
  expect(outcomes.at(-1)).toEqual({ kind: "unsure", text: IMPORT_PRECHECK_STOP_UNSURE_TEXT });
  // 这一次请求体没传完就断开、也没认领到作业:不能据此说上一次没导入。
  const partial = controllableUpload();
  const cut = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(partial.uploadId).not.toBe(""));
  expect(partial.uploadId).toBe(uploadId);
  api.listNotebookTransfers.mockResolvedValueOnce([]);
  partial.progress(10, 100);
  partial.reject(new TypeError("Failed to fetch"));
  await cut;
  expect(outcomes.at(-1)).toEqual({ kind: "unsure", text: IMPORT_UPLOAD_UNSURE_TEXT });
  const next = controllableUpload();
  const third = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(next.uploadId).not.toBe(""));
  expect(next.uploadId).toBe(uploadId);
  next.resolve(active("x", "import", uploadId));
  await third;
  stop();
});

test("说不准之后被服务端明确拒绝:先按标识对账,认领到就交出;确定没有才换标识", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { humanizedError } = await import("../../app/errors.ts");
  const { outcomes, stop } = collect();
  const chosen = file();
  const uploadId = await leaveUnsure(chosen);
  const refused = controllableUpload();
  const attempt = importUpload.start(chosen, "", 1024);
  await vi.waitFor(() => expect(refused.uploadId).not.toBe(""));
  api.listNotebookTransfers.mockResolvedValueOnce([active("found", "import", uploadId)]);
  refused.reject(humanizedError("请上传笔记本导出文件（.snbk）。", 415));
  await attempt;
  expect(outcomes.at(-1)).toEqual({ kind: "uploaded", job: active("found", "import", uploadId), fileName: "库.snbk" });
  stop();
});
