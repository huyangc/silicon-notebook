import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { NotebookTransferJob } from "../../app/notebook-transfer-api.ts";

// 两个状态机:导出(接回 → 轮询 → 可下载 → 下载 / 取消)与导入(本地预检 → 上传结局 →
// 作业轮询 → 发布即报告;取消 / 移除的结果;轮询与取消抢跑)。网络层整个换成假的。

const api = vi.hoisted(() => ({
  startNotebookExport: vi.fn(),
  listNotebookTransfers: vi.fn(),
  getNotebookTransfer: vi.fn(),
  deleteNotebookTransfer: vi.fn(),
  downloadNotebookExport: vi.fn(),
  uploadNotebookImport: vi.fn(),
}));
vi.mock("../../app/notebook-transfer-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/notebook-transfer-api.ts")>()),
  ...api,
}));
const system = vi.hoisted(() => ({ fetchSystemConfiguration: vi.fn() }));
vi.mock("../../app/system-api.ts", () => system);
const memories = vi.hoisted(() => ({ exportDeletedNotebookMemories: vi.fn() }));
vi.mock("../../app/notebook-deletion-api.ts", () => memories);

const {
  useNotebookExport, useNotebookImport, importFileProblem, TRANSFER_POLL_MS,
} = await import("../../app/use-notebook-transfer.ts");
const { forgetTransferLimit } = await import("../../app/notebook-transfer-api.ts");
const { importUpload } = await import("../../app/notebook-import-upload.ts");

const LIMIT = 8 * 1024 * 1024;

function job(over: Partial<NotebookTransferJob> = {}): NotebookTransferJob {
  return {
    id: "xfer-1", kind: "export", state: "running", phase: "writing", notebook_id: "nb1",
    notebook_name: "封装工艺库", created_at: "", finished_at: null, expires_at: null,
    progress: { bytes: 1024 }, archive_bytes: 0, downloadable: false, downloaded: false,
    report: null, reembed: null, index: null, failure_code: "", client_upload_id: "", ...over,
  };
}

function imp(id: string, over: Partial<NotebookTransferJob> = {}): NotebookTransferJob {
  return job({ id, kind: "import", phase: "rows", notebook_id: "", notebook_name: "", ...over });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  forgetTransferLimit();
  system.fetchSystemConfiguration.mockResolvedValue({ notebook_transfer_max_bytes: LIMIT });
  api.listNotebookTransfers.mockResolvedValue([]);
});
afterEach(() => {
  importUpload.reset();
  vi.useRealTimers();
});

async function tick(ms = TRANSFER_POLL_MS + 10) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

test("导出:接回这本笔记本进行中的作业,轮询到完成后可以下载,下载结果落在按钮上并按时还原", async () => {
  api.listNotebookTransfers.mockResolvedValue([
    job({ id: "other", notebook_id: "nb2" }),
    job({ id: "xfer-1" }),
  ]);
  api.getNotebookTransfer.mockResolvedValueOnce(job({ progress: { bytes: 4096 } }))
    .mockResolvedValueOnce(job({ state: "succeeded", phase: "done", downloadable: true, archive_bytes: 9000 }));
  api.downloadNotebookExport.mockResolvedValue(undefined);
  const { result } = renderHook(() => useNotebookExport("nb1"));
  await waitFor(() => expect(result.current.view.loading).toBe(false));
  expect(result.current.view.job?.id).toBe("xfer-1");
  expect(result.current.view.limitBytes).toBe(LIMIT);
  await tick();
  expect(result.current.view.job?.progress.bytes).toBe(4096);
  await tick();
  expect(result.current.view.job?.state).toBe("succeeded");
  await tick();
  expect(api.getNotebookTransfer).toHaveBeenCalledTimes(2);

  await act(async () => { await result.current.download(); });
  expect(api.downloadNotebookExport).toHaveBeenCalledWith("xfer-1");
  expect(result.current.view.downloadResult).toBe("started");
  await tick(4100);
  expect(result.current.view.downloadResult).toBe("idle");
});

test("上限只读一次:第二次打开不再拉 /system/config", async () => {
  const first = renderHook(() => useNotebookExport("nb1"));
  await waitFor(() => expect(first.result.current.view.limitBytes).toBe(LIMIT));
  first.unmount();
  const second = renderHook(() => useNotebookExport("nb1"));
  await waitFor(() => expect(second.result.current.view.limitBytes).toBe(LIMIT));
  expect(system.fetchSystemConfiguration).toHaveBeenCalledTimes(1);
});

test("导出:下载在途时再点不会再取一张票据;失败的原因留在按钮旁", async () => {
  api.listNotebookTransfers.mockResolvedValue([job({ state: "succeeded", downloadable: true })]);
  let release: () => void = () => undefined;
  api.downloadNotebookExport.mockImplementationOnce(() => new Promise<void>((resolve) => { release = resolve; }))
    .mockRejectedValueOnce(new Error("boom"));
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { result } = renderHook(() => useNotebookExport("nb1"));
  await waitFor(() => expect(result.current.view.loading).toBe(false));
  let first: Promise<void> = Promise.resolve();
  act(() => { first = result.current.download(); });
  expect(result.current.view.downloading).toBe(true);
  await act(async () => { await result.current.download(); });
  expect(api.downloadNotebookExport).toHaveBeenCalledTimes(1);
  await act(async () => { release(); await first; });
  await act(async () => { await result.current.download(); });
  expect(result.current.view.downloadResult).toBe("failed");
  expect(result.current.view.downloadError).toBe("下载没有开始，请重试。");
});

test("导出:取消——cancelled 清掉作业并就地说明;deleted(刚好结束)说清文件已删", async () => {
  api.listNotebookTransfers.mockResolvedValue([job()]);
  api.getNotebookTransfer.mockImplementation(() => new Promise(() => undefined));
  const { result } = renderHook(() => useNotebookExport("nb1"));
  await waitFor(() => expect(result.current.view.loading).toBe(false));
  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "cancelled", kept_notebook_id: null });
  await act(async () => { await result.current.cancel(); });
  expect(result.current.view.job).toBeNull();
  expect(result.current.view.notice).toBe("已取消导出。");

  api.startNotebookExport.mockResolvedValueOnce(job({ id: "xfer-2" }));
  await act(async () => { await result.current.start(); });
  expect(result.current.view.job?.id).toBe("xfer-2");
  expect(result.current.view.notice).toBe("");
  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "deleted", kept_notebook_id: null });
  await act(async () => { await result.current.cancel(); });
  expect(result.current.view.job).toBeNull();
  expect(result.current.view.notice).toContain("导出文件已删除");
});

test("导出:取消之后迟到的「进行中」读结果不会把作业复活", async () => {
  api.listNotebookTransfers.mockResolvedValue([job()]);
  let lateRead: (value: NotebookTransferJob) => void = () => undefined;
  api.getNotebookTransfer.mockImplementationOnce(() => new Promise((resolve) => { lateRead = resolve; }));
  const { result } = renderHook(() => useNotebookExport("nb1"));
  await waitFor(() => expect(result.current.view.loading).toBe(false));
  await tick(); // 一次读已经发出,还没回来
  expect(api.getNotebookTransfer).toHaveBeenCalledTimes(1);
  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "cancelled", kept_notebook_id: null });
  await act(async () => { await result.current.cancel(); });
  expect(result.current.view.job).toBeNull();
  await act(async () => { lateRead(job()); await Promise.resolve(); });
  expect(result.current.view.job).toBeNull();
  expect(result.current.view.notice).toBe("已取消导出。");
});

test("导出:没变化的轮询不换作业对象(不重渲)", async () => {
  api.listNotebookTransfers.mockResolvedValue([job()]);
  api.getNotebookTransfer.mockResolvedValue(job());
  let renders = 0;
  const { result } = renderHook(() => { renders += 1; return useNotebookExport("nb1"); });
  await waitFor(() => expect(result.current.view.loading).toBe(false));
  const before = renders;
  await tick();
  await tick();
  await tick();
  expect(api.getNotebookTransfer).toHaveBeenCalledTimes(3);
  expect(renders).toBe(before);
});

test("导出:开始失败的原因就地给出;在途时连点只发一次", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  let reject: (error: unknown) => void = () => undefined;
  api.startNotebookExport.mockImplementationOnce(() => new Promise((_, no) => { reject = no; }));
  const { result } = renderHook(() => useNotebookExport("nb1"));
  await waitFor(() => expect(result.current.view.loading).toBe(false));
  let first: Promise<void> = Promise.resolve();
  act(() => { first = result.current.start(); });
  expect(result.current.view.starting).toBe(true);
  await act(async () => { await result.current.start(); });
  expect(api.startNotebookExport).toHaveBeenCalledTimes(1);
  await act(async () => { reject(new Error("x")); await first; });
  expect(result.current.view.starting).toBe(false);
  expect(result.current.view.startError).toBe("导出没有开始，请重试。");
});

test("导入:本地预检——扩展名、空文件、超过上限;拿不到上限时不预检", () => {
  const big = new File([new Uint8Array(10)], "a.snbk");
  Object.defineProperty(big, "size", { value: LIMIT + 1 });
  expect(importFileProblem(new File(["x"], "a.zip"), LIMIT)).toBe("请选择笔记本导出文件（.snbk）。");
  expect(importFileProblem(new File([], "a.snbk"), LIMIT)).toBe("这个文件是空的。");
  expect(importFileProblem(big, LIMIT)).toContain("超过网页导入上限 8 MB");
  expect(importFileProblem(big, LIMIT)).toContain("请联系管理员用命令行导入");
  expect(importFileProblem(big, null)).toBe("");
  expect(importFileProblem(new File(["x"], "A.SNBK"), LIMIT)).toBe("");
});

function renderImport(open = true) {
  const onNotebookReady = vi.fn();
  const hook = renderHook(
    ({ isOpen }) => useNotebookImport({ open: isOpen, onNotebookReady }),
    { initialProps: { isOpen: open } },
  );
  return { ...hook, onNotebookReady };
}

test("导入:超过上限的文件不能开始(不发请求);合格的文件上传后成为一行作业,发布时报告一次新笔记本", async () => {
  const { result, onNotebookReady } = renderImport();
  await waitFor(() => expect(result.current.view.limitBytes).toBe(LIMIT));
  const big = new File([new Uint8Array(10)], "big.snbk");
  Object.defineProperty(big, "size", { value: LIMIT + 1 });
  act(() => result.current.chooseFile(big));
  expect(result.current.view.fileError).toContain("超过网页导入上限");
  act(() => result.current.start());
  expect(api.uploadNotebookImport).not.toHaveBeenCalled();

  const file = new File([new Uint8Array(10)], "库.snbk");
  act(() => result.current.chooseFile(file));
  act(() => result.current.setName("新库"));
  api.uploadNotebookImport.mockResolvedValueOnce(imp("imp-1", { state: "queued", phase: "queued" }));
  act(() => result.current.start());
  await waitFor(() => expect(result.current.view.jobs.map((item) => item.id)).toEqual(["imp-1"]));
  expect(api.uploadNotebookImport.mock.calls[0][1].name).toBe("新库");
  expect(result.current.view.fileNames["imp-1"]).toBe("库.snbk");
  expect(result.current.view.file).toBeNull();
  expect(result.current.view.name).toBe("");

  api.getNotebookTransfer.mockResolvedValueOnce(imp("imp-1", { notebook_id: "nb-new" }))
    .mockResolvedValueOnce(imp("imp-1", { phase: "reembed", notebook_id: "nb-new" }))
    .mockResolvedValue(imp("imp-1", { phase: "reembed", notebook_id: "nb-new" }));
  await tick();
  expect(onNotebookReady).not.toHaveBeenCalled();
  await tick();
  expect(onNotebookReady).toHaveBeenCalledExactlyOnceWith("nb-new");
  await tick();
  expect(onNotebookReady).toHaveBeenCalledTimes(1);
});

test("导入:没变化的轮询不换数组、不重渲 Home", async () => {
  api.listNotebookTransfers.mockResolvedValue([imp("a")]);
  api.getNotebookTransfer.mockResolvedValue(imp("a"));
  let renders = 0;
  const onNotebookReady = vi.fn();
  const { result } = renderHook(() => { renders += 1; return useNotebookImport({ open: true, onNotebookReady }); });
  await waitFor(() => expect(result.current.view.jobs).toHaveLength(1));
  const jobs = result.current.view.jobs;
  const before = renders;
  await tick();
  await tick();
  expect(api.getNotebookTransfer).toHaveBeenCalledTimes(2);
  expect(renders).toBe(before);
  expect(result.current.view.jobs).toBe(jobs);
});

test("导入:上传失败的原因留在按钮旁、文件仍选着;主动停止说清没有导入任何内容", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { result } = renderImport();
  await waitFor(() => expect(result.current.view.limitBytes).toBe(LIMIT));
  const file = new File([new Uint8Array(10)], "a.snbk");
  act(() => result.current.chooseFile(file));
  api.uploadNotebookImport.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  act(() => result.current.start());
  await waitFor(() => expect(result.current.view.uploadError).toBe("上传没有完成，请检查网络后重试。"));
  expect(result.current.view.file).toBe(file);

  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: { signal: AbortSignal }) => new Promise((_, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  act(() => result.current.start());
  await waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  act(() => result.current.stopUpload());
  await waitFor(() => expect(result.current.view.uploadNotice).toBe("已停止上传，没有导入任何内容。"));
  expect(importUpload.getSnapshot()).toBeNull();
});

test("导入:Home 卸载时上传不断,结局交给重新挂载的状态机", async () => {
  const first = renderImport();
  await waitFor(() => expect(first.result.current.view.limitBytes).toBe(LIMIT));
  const file = new File([new Uint8Array(10)], "a.snbk");
  act(() => first.result.current.chooseFile(file));
  let finish: (value: NotebookTransferJob) => void = () => undefined;
  api.uploadNotebookImport.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  act(() => first.result.current.start());
  await waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  first.unmount();
  expect(importUpload.busy()).toBe(true);
  await act(async () => { finish(imp("imp-9")); await Promise.resolve(); });
  await waitFor(() => expect(importUpload.busy()).toBe(false));
  const second = renderImport();
  await waitFor(() => expect(second.result.current.view.jobs.map((item) => item.id)).toEqual(["imp-9"]));
  expect(second.result.current.view.fileNames["imp-9"]).toBe("a.snbk");
  expect(second.result.current.view.uploadNotice).toContain("文件已上传");
});

test("导入:关掉弹窗不再轮询;重新打开读回记录并保留这一页上传出来、列表里还没有的作业", async () => {
  const { result, rerender } = renderImport();
  await waitFor(() => expect(result.current.view.loadingJobs).toBe(false));
  api.uploadNotebookImport.mockResolvedValueOnce(imp("imp-9"));
  act(() => result.current.chooseFile(new File([new Uint8Array(10)], "a.snbk")));
  rerender({ isOpen: false });
  act(() => result.current.start());
  await waitFor(() => expect(result.current.view.jobs[0]?.id).toBe("imp-9"));
  await tick();
  expect(api.getNotebookTransfer).not.toHaveBeenCalled();
  // 列表请求早于那次上传落定:列表里没有 imp-9,它不能被换掉。
  api.listNotebookTransfers.mockResolvedValue([imp("old"), job({ id: "exp-1" })]);
  rerender({ isOpen: true });
  await waitFor(() => expect(result.current.view.jobs.map((item) => item.id)).toEqual(["imp-9", "old"]));
});

test("导入:弹窗关着时发布的导入,重新打开时刷新一次笔记本列表(合并成一次)", async () => {
  const { result, rerender, onNotebookReady } = renderImport(false);
  api.listNotebookTransfers.mockResolvedValue([
    imp("a", { phase: "reembed", notebook_id: "nb-a" }),
    imp("b", { state: "succeeded", phase: "done", notebook_id: "nb-b" }),
    imp("c"),
  ]);
  api.getNotebookTransfer.mockImplementation(() => new Promise(() => undefined));
  rerender({ isOpen: true });
  await waitFor(() => expect(result.current.view.jobs).toHaveLength(3));
  expect(onNotebookReady).toHaveBeenCalledTimes(1);
  rerender({ isOpen: false });
  rerender({ isOpen: true });
  await waitFor(() => expect(api.listNotebookTransfers).toHaveBeenCalledTimes(2));
  await tick(10);
  expect(onNotebookReady).toHaveBeenCalledTimes(1);
});

test("导入:取消 / 移除的结果——笔记本留没留下只认服务端回的 kept_notebook_id", async () => {
  const running = imp("a", { notebook_id: "nb-a" });
  const publishing = imp("b", { phase: "publish", notebook_id: "nb-b" });
  const failed = imp("c", { state: "failed", failure_code: "invalid_archive" });
  const done = imp("d", { state: "succeeded", phase: "done", notebook_id: "nb-d" });
  const slow = imp("e");
  // 点「取消」时手里的快照还在 rows,服务端却说它已经发布并结束了。
  const raced = imp("f", { notebook_id: "nb-f" });
  api.listNotebookTransfers.mockResolvedValue([running, publishing, failed, done, slow, raced]);
  api.getNotebookTransfer.mockImplementation(() => new Promise(() => undefined));
  const { result, onNotebookReady } = renderImport();
  await waitFor(() => expect(result.current.view.jobs).toHaveLength(6));
  // 打开时已经发布的 d 已经触发过一次刷新。
  onNotebookReady.mockClear();

  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "cancelled", kept_notebook_id: null });
  await act(async () => { await result.current.cancelJob(running); });
  expect(result.current.view.notes.a).toEqual({ tone: "done", text: "已取消导入，没有留下任何内容。" });

  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "kept", kept_notebook_id: "nb-b" });
  await act(async () => { await result.current.cancelJob(publishing); });
  expect(result.current.view.notes.b).toEqual({
    tone: "kept", text: "取消时笔记本已经导入完成，它已保留在你的笔记本列表里。", notebookId: "nb-b",
  });
  expect(onNotebookReady).toHaveBeenCalledWith("nb-b");

  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "deleted", kept_notebook_id: null });
  await act(async () => { await result.current.removeJob(failed); });
  expect(result.current.view.notes.c).toEqual({ tone: "done", text: "记录已移除。" });

  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "deleted", kept_notebook_id: "nb-d" });
  await act(async () => { await result.current.removeJob(done); });
  expect(result.current.view.notes.d).toEqual({ tone: "kept", text: "记录已移除，笔记本仍在你的笔记本列表里。", notebookId: "nb-d" });

  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "deleted", kept_notebook_id: "nb-f" });
  await act(async () => { await result.current.cancelJob(raced); });
  expect(result.current.view.notes.f).toEqual({ tone: "kept", text: "记录已移除，笔记本仍在你的笔记本列表里。", notebookId: "nb-f" });
  expect(onNotebookReady).toHaveBeenCalledWith("nb-f");

  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "pending", kept_notebook_id: null });
  await act(async () => { await result.current.cancelJob(slow); });
  expect(result.current.view.notes.e).toBeUndefined();
  expect(result.current.view.rows.e).toEqual({ stopping: true });
  api.getNotebookTransfer.mockResolvedValue({ ...slow, state: "cancelled" });
  await tick();
  expect(result.current.view.rows.e.stopping).toBe(false);
  expect(result.current.view.jobs.find((item) => item.id === "e")?.state).toBe("cancelled");
});

test("导入:取消在途的那一行不轮询", async () => {
  const a = imp("a");
  api.listNotebookTransfers.mockResolvedValue([a]);
  api.getNotebookTransfer.mockResolvedValue(a);
  const { result } = renderImport();
  await waitFor(() => expect(result.current.view.jobs).toHaveLength(1));
  await tick();
  expect(api.getNotebookTransfer).toHaveBeenCalledTimes(1);
  let releaseDelete: (value: unknown) => void = () => undefined;
  api.deleteNotebookTransfer.mockImplementationOnce(() => new Promise((resolve) => { releaseDelete = resolve; }));
  let cancelling: Promise<void> = Promise.resolve();
  act(() => { cancelling = result.current.cancelJob(a); });
  await tick();
  await tick();
  expect(api.getNotebookTransfer).toHaveBeenCalledTimes(1);
  await act(async () => { releaseDelete({ outcome: "cancelled", kept_notebook_id: null }); await cancelling; });
  await tick();
  expect(api.getNotebookTransfer).toHaveBeenCalledTimes(1);
});

test("导入:取消之前发出、之后才回来的读——404 不覆盖「已保留」,「进行中」不复活", async () => {
  const a = imp("a");
  const b = imp("b");
  api.listNotebookTransfers.mockResolvedValue([a, b]);
  const late: Record<string, (value: NotebookTransferJob | null) => void> = {};
  api.getNotebookTransfer.mockImplementation((id: string) => new Promise((resolve) => { late[id] = resolve; }));
  const { result } = renderImport();
  await waitFor(() => expect(result.current.view.jobs).toHaveLength(2));
  await tick();
  expect(Object.keys(late).sort()).toEqual(["a", "b"]);
  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "kept", kept_notebook_id: "nb-a" })
    .mockResolvedValueOnce({ outcome: "cancelled", kept_notebook_id: null });
  await act(async () => { await result.current.cancelJob(a); });
  await act(async () => { await result.current.cancelJob(b); });
  expect(result.current.view.notes.a.notebookId).toBe("nb-a");
  await act(async () => {
    late.a(null);
    late.b({ ...b, phase: "files" });
    await Promise.resolve();
  });
  expect(result.current.view.notes.a).toEqual({
    tone: "kept", text: "取消时笔记本已经导入完成，它已保留在你的笔记本列表里。", notebookId: "nb-a",
  });
  expect(result.current.view.notes.b.text).toBe("已取消导入，没有留下任何内容。");
  expect(result.current.view.jobs.find((item) => item.id === "b")?.phase).toBe("rows");
});

test("导入:取消失败的原因落在那一行;登出中止上传并清空", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const running = imp("a");
  api.listNotebookTransfers.mockResolvedValue([running]);
  api.getNotebookTransfer.mockResolvedValue(running);
  const { result } = renderImport();
  await waitFor(() => expect(result.current.view.jobs).toHaveLength(1));
  api.deleteNotebookTransfer.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  await act(async () => { await result.current.cancelJob(running); });
  expect(result.current.view.rows.a).toEqual({ failure: "没能取消，请重试。" });

  act(() => result.current.chooseFile(new File([new Uint8Array(10)], "a.snbk")));
  let signal: AbortSignal | null = null;
  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: { signal: AbortSignal }) => {
    signal = options.signal;
    return new Promise(() => undefined);
  });
  act(() => result.current.start());
  await waitFor(() => expect(signal).not.toBeNull());
  act(() => result.current.leaveActor());
  expect(signal!.aborted).toBe(true);
  expect(importUpload.getSnapshot()).toBeNull();
  expect(result.current.view.jobs).toEqual([]);
  expect(result.current.view.file).toBeNull();
});


test("上传后那句「正在导入」只在那个作业还在导入时说:结束、发布或被取消后撤掉", async () => {
  const { result } = renderImport();
  await waitFor(() => expect(result.current.view.limitBytes).toBe(LIMIT));
  api.uploadNotebookImport.mockResolvedValueOnce(imp("imp-1"));
  act(() => result.current.chooseFile(new File([new Uint8Array(10)], "a.snbk")));
  act(() => result.current.start());
  await waitFor(() => expect(result.current.view.uploadNotice).toContain("正在导入"));
  api.getNotebookTransfer.mockResolvedValue(imp("imp-1", { state: "succeeded", phase: "done", notebook_id: "nb-1" }));
  await tick();
  expect(result.current.view.jobs[0].state).toBe("succeeded");
  expect(result.current.view.uploadNotice).toBe("");

  // 第二次:发布(还在补全检索数据)也算不再「正在导入」。
  api.uploadNotebookImport.mockResolvedValueOnce(imp("imp-2"));
  act(() => result.current.chooseFile(new File([new Uint8Array(10)], "b.snbk")));
  act(() => result.current.start());
  await waitFor(() => expect(result.current.view.uploadNotice).toContain("正在导入"));
  api.getNotebookTransfer.mockResolvedValue(imp("imp-2", { phase: "reembed", notebook_id: "nb-2" }));
  await tick();
  expect(result.current.view.uploadNotice).toBe("");

  // 第三次:那一行被取消了。
  api.uploadNotebookImport.mockResolvedValueOnce(imp("imp-3"));
  api.getNotebookTransfer.mockImplementation(() => new Promise(() => undefined));
  act(() => result.current.chooseFile(new File([new Uint8Array(10)], "c.snbk")));
  act(() => result.current.start());
  await waitFor(() => expect(result.current.view.uploadNotice).toContain("正在导入"));
  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "cancelled", kept_notebook_id: null });
  await act(async () => { await result.current.cancelJob(imp("imp-3")); });
  expect(result.current.view.uploadNotice).toBe("");
});

test("弹窗关着时上传失败:工具栏上的「导入失败」一直挂着,打开弹窗(原因就在那里)才撤掉", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { result, rerender } = renderImport();
  await waitFor(() => expect(result.current.view.limitBytes).toBe(LIMIT));
  act(() => result.current.chooseFile(new File([new Uint8Array(10)], "a.snbk")));
  let fail: (error: unknown) => void = () => undefined;
  api.uploadNotebookImport.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }));
  act(() => result.current.start());
  await waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  rerender({ isOpen: false });
  await act(async () => { fail(new Error("x")); await Promise.resolve(); });
  await waitFor(() => expect(result.current.view.uploadError).not.toBe(""));
  expect(result.current.view.uploadAttention).toBe("failed");
  rerender({ isOpen: true });
  await waitFor(() => expect(result.current.view.uploadAttention).toBe(""));
  expect(result.current.view.uploadError).not.toBe("");
  // 弹窗开着时失败的,原因已经在眼前:不挂工具栏上的那一句。
  api.uploadNotebookImport.mockRejectedValueOnce(new Error("y"));
  act(() => result.current.start());
  await waitFor(() => expect(importUpload.busy()).toBe(false));
  expect(result.current.view.uploadAttention).toBe("");
});


test("上传的响应回来时导入已经做完(小文件):照样报告一次新笔记本,列表跟着刷新", async () => {
  const { result, onNotebookReady } = renderImport();
  await waitFor(() => expect(result.current.view.limitBytes).toBe(LIMIT));
  api.uploadNotebookImport.mockResolvedValueOnce(imp("tiny", { state: "succeeded", phase: "done", notebook_id: "nb-tiny" }));
  act(() => result.current.chooseFile(new File([new Uint8Array(10)], "tiny.snbk")));
  act(() => result.current.start());
  await waitFor(() => expect(result.current.view.jobs[0]?.id).toBe("tiny"));
  expect(onNotebookReady).toHaveBeenCalledExactlyOnceWith("nb-tiny");
  // 终态不轮询;再打开弹窗读到它,也不再报第二次。
  api.listNotebookTransfers.mockResolvedValue([imp("tiny", { state: "succeeded", phase: "done", notebook_id: "nb-tiny" })]);
  await tick();
  expect(onNotebookReady).toHaveBeenCalledTimes(1);
});

test("连接断了、说不准有没有被接收:原因落在按钮旁,弹窗关着时工具栏说「待确认」", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { result, rerender } = renderImport();
  await waitFor(() => expect(result.current.view.limitBytes).toBe(LIMIT));
  act(() => result.current.chooseFile(new File([new Uint8Array(10)], "a.snbk")));
  let cut: (error: unknown) => void = () => undefined;
  api.uploadNotebookImport.mockImplementationOnce(() => new Promise((_, reject) => { cut = reject; }));
  api.listNotebookTransfers.mockResolvedValueOnce([]).mockRejectedValueOnce(new TypeError("offline"));
  act(() => result.current.start());
  await waitFor(() => expect(importUpload.getSnapshot()?.stage).toBe("uploading"));
  rerender({ isOpen: false });
  await act(async () => { cut(new TypeError("Failed to fetch")); await Promise.resolve(); });
  await waitFor(() => expect(result.current.view.uploadError).toContain("可能已被接收"));
  expect(result.current.view.uploadAttention).toBe("unsure");
});

test("导出记忆的请求本身抛错:结果落在那几个字旁边,不悄悄回到原样", async () => {
  const { result } = renderHook(() => useNotebookExport("nb1"));
  await waitFor(() => expect(result.current.view.loading).toBe(false));
  memories.exportDeletedNotebookMemories.mockRejectedValueOnce(new TypeError("body stream broke"));
  await act(async () => { await result.current.exportMemories(); });
  expect(result.current.view.memoryExporting).toBe(false);
  expect(result.current.view.memoryResult).toBe("failed");
  expect(result.current.view.memoryText).toBe("记忆没有导出，请重试。");
});


test("说不准之后重传同一个文件:带同一个上传标识,服务端回的已有作业只占一行", async () => {
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  const { result } = renderImport();
  await waitFor(() => expect(result.current.view.limitBytes).toBe(LIMIT));
  const chosen = new File([new Uint8Array(10)], "a.snbk");
  act(() => result.current.chooseFile(chosen));
  let cut: (error: unknown) => void = () => undefined;
  let sent: () => void = () => undefined;
  api.uploadNotebookImport.mockImplementationOnce((_file: File, options: { onBodySent: () => void }) => {
    sent = options.onBodySent;
    return new Promise((_, reject) => { cut = reject; });
  });
  act(() => result.current.start());
  await waitFor(() => expect(api.uploadNotebookImport).toHaveBeenCalledTimes(1));
  // 请求体传完、连接断了、作业还没出现:说不准,文件仍选着。
  api.listNotebookTransfers.mockResolvedValueOnce([]);
  await act(async () => { sent(); cut(new TypeError("Failed to fetch")); await Promise.resolve(); });
  await waitFor(() => expect(result.current.view.uploadError).toContain("可能已被接收"));
  expect(result.current.view.file).toBe(chosen);
  const firstId = api.uploadNotebookImport.mock.calls[0][1].uploadId;
  // 这时作业已经在服务端登记了(列表里看得到),用户又点了一次。
  const existing = imp("existing", { client_upload_id: firstId });
  api.uploadNotebookImport.mockResolvedValueOnce(existing);
  act(() => result.current.start());
  await waitFor(() => expect(result.current.view.jobs.map((item) => item.id)).toEqual(["existing"]));
  expect(api.uploadNotebookImport.mock.calls[1][1].uploadId).toBe(firstId);
  expect(result.current.view.uploadError).toBe("");
});
