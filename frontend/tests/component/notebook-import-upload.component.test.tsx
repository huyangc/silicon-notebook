import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { NotebookTransferJob } from "../../app/notebook-transfer-api.ts";

// 模块级的上传仓库:先核对名额、只在整数百分比/阶段变化时通知、传完进入「检查文件」、
// 中途断开时说清原因、上传中挂 beforeunload、结局没人接就先存着。

const api = vi.hoisted(() => ({
  listNotebookTransfers: vi.fn(),
  uploadNotebookImport: vi.fn(),
  forgetTransferLimit: vi.fn(),
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

const { importUpload, useImportUpload, useImportUploadBusy } = await import("../../app/notebook-import-upload.ts");
const { IMPORT_UPLOAD_CUT_TEXT, IMPORT_UPLOAD_FAILED_TEXT, TRANSFER_BUSY_TEXT } = await import(
  "../../app/notebook-transfer-api.ts"
);

function active(id: string, kind: "export" | "import" = "import"): NotebookTransferJob {
  return {
    id, kind, state: "running", phase: "rows", notebook_id: "", notebook_name: "", created_at: "",
    finished_at: null, expires_at: null, progress: {}, archive_bytes: 0, downloadable: false,
    downloaded: false, report: null, reembed: null, index: null, failure_code: "",
  };
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

test("中途网络断开:再读一次作业——名额满了就照实说;读不清时说可能的几种原因;确定没超上限就只是网络", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { outcomes, stop } = collect();
  api.uploadNotebookImport.mockRejectedValue(new TypeError("Failed to fetch"));
  api.listNotebookTransfers.mockResolvedValueOnce([]).mockResolvedValueOnce([active("a"), active("b")]);
  await importUpload.start(file(), "", null);
  api.listNotebookTransfers.mockResolvedValueOnce([]).mockRejectedValueOnce(new TypeError("offline"));
  await importUpload.start(file(), "", null);
  api.listNotebookTransfers.mockResolvedValueOnce([]).mockResolvedValueOnce([]);
  await importUpload.start(file(), "", 1024);
  api.listNotebookTransfers.mockResolvedValueOnce([]).mockResolvedValueOnce([]);
  await importUpload.start(file(), "", null);
  expect(outcomes).toEqual([
    { kind: "failed", text: TRANSFER_BUSY_TEXT },
    { kind: "failed", text: IMPORT_UPLOAD_CUT_TEXT },
    { kind: "failed", text: IMPORT_UPLOAD_FAILED_TEXT },
    { kind: "failed", text: IMPORT_UPLOAD_CUT_TEXT },
  ]);
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

test("停下的那一刻请求体其实已经传完:再读一次作业,多出来的导入照实交出,不说「没有导入任何内容」", async () => {
  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: { signal: AbortSignal }) => new Promise((_, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  api.listNotebookTransfers.mockResolvedValueOnce([active("old")])
    .mockResolvedValueOnce([active("imp-8"), active("old")]);
  const { outcomes, stop } = collect();
  const running = importUpload.start(file(), "", null);
  await vi.waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  importUpload.stop();
  await running;
  expect(outcomes).toEqual([{ kind: "uploaded", job: active("imp-8"), fileName: "库.snbk" }]);
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
