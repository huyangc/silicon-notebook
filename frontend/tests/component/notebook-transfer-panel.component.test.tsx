import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import type { NotebookTransferJob } from "../../app/notebook-transfer-api.ts";
import type { NotebookImportView } from "../../app/use-notebook-transfer.ts";
import { CHECKUP_FIX } from "../../app/vocabulary.ts";

// 导出一节、导入弹窗、铃铛行动作:按钮在途禁用、结果落在按钮旁、关闭入口永不禁用、
// 记忆说明是可点的动作、补全检索数据期间的那句提示。

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
const saving = vi.hoisted(() => ({ saveBlobAsFile: vi.fn() }));
vi.mock("../../app/download-file.ts", () => saving);
// 上传进度来自模块级仓库;这里直接给弹窗一个快照。
const upload = vi.hoisted(() => ({ snapshot: null as null | Record<string, unknown> }));
vi.mock("../../app/notebook-import-upload.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/notebook-import-upload.ts")>()),
  useImportUpload: () => upload.snapshot,
  useImportUploadBusy: () => upload.snapshot !== null,
}));

const {
  MEMORY_EXPORT_LABEL,
  NotebookExportModal,
  NotebookExportSection,
  NotebookImportModal,
  ImportToolbarButton,
  TransferBellActions,
} = await import("../../app/notebook-transfer-panel.tsx");

function job(over: Partial<NotebookTransferJob> = {}): NotebookTransferJob {
  return {
    id: "xfer-1", kind: "export", state: "running", phase: "writing", notebook_id: "nb1",
    notebook_name: "封装工艺库", created_at: "", finished_at: null, expires_at: null,
    progress: { bytes: 2 * 1024 * 1024 }, archive_bytes: 0, downloadable: false, downloaded: false,
    report: null, reembed: null, index: null, failure_code: "", ...over,
  };
}

beforeEach(() => {
  upload.snapshot = null;
  system.fetchSystemConfiguration.mockResolvedValue({ notebook_transfer_max_bytes: 512 * 1024 * 1024 });
  api.listNotebookTransfers.mockResolvedValue([]);
  api.getNotebookTransfer.mockImplementation(() => new Promise(() => undefined));
});

test("导出一节:说清上限与记忆不在文件里;「导出我的记忆（Markdown）」是可点的字,结果就在它旁边", async () => {
  const user = userEvent.setup();
  memories.exportDeletedNotebookMemories.mockResolvedValueOnce({ kind: "nothing" })
    .mockResolvedValueOnce({ kind: "exported", blob: new Blob(["# m"]), filename: "m.md" });
  render(<NotebookExportSection notebookId="nb1" />);
  expect(await screen.findByText(/网页导出上限 512 MB/)).toBeInTheDocument();
  expect(screen.getByText(/导出文件不包含记忆/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: MEMORY_EXPORT_LABEL }));
  expect(await screen.findByRole("alert")).toHaveTextContent("这本笔记本里没有你的记忆");
  await user.click(screen.getByRole("button", { name: MEMORY_EXPORT_LABEL }));
  expect(await screen.findByText(/已开始下载记忆/)).toBeInTheDocument();
  expect(saving.saveBlobAsFile).toHaveBeenCalledWith(expect.any(Blob), "m.md");
  expect(memories.exportDeletedNotebookMemories).toHaveBeenCalledWith("nb1");
});

test("导出一节:开始 → 在途禁用 → 进度与「取消导出」;开始失败的原因就在按钮旁", async () => {
  const user = userEvent.setup();
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  api.startNotebookExport.mockRejectedValueOnce(new Error("x"));
  render(<NotebookExportSection notebookId="nb1" />);
  const start = await screen.findByRole("button", { name: "导出笔记本" });
  await user.click(start);
  const actions = start.closest(".transfer-actions") as HTMLElement;
  expect(await within(actions).findByRole("alert")).toHaveTextContent("导出没有开始，请重试。");

  let resolve: (value: NotebookTransferJob) => void = () => undefined;
  api.startNotebookExport.mockImplementationOnce(() => new Promise((ok) => { resolve = ok; }));
  await user.click(screen.getByRole("button", { name: "导出笔记本" }));
  expect(screen.getByRole("button", { name: "正在提交…" })).toBeDisabled();
  resolve(job());
  // 播报区里只有阶段(阶段变了才读);字节数在它旁边,不在播报区里。
  const phase = await screen.findByText("正在导出：正在写入导出文件");
  expect(phase).toHaveAttribute("aria-live", "polite");
  expect(phase).not.toHaveTextContent("MB");
  expect(screen.getByText("（已写入 2.0 MB）")).not.toHaveAttribute("role");
  expect(screen.getByText(/关掉这个窗口不影响导出/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "取消导出" })).toBeEnabled();
});

test("导出一节:完成后「下载」;按下取票据,在途禁用,之后按钮上写「已开始下载」;取消有结果就地说明", async () => {
  const user = userEvent.setup();
  api.listNotebookTransfers.mockResolvedValue([job({
    state: "succeeded", phase: "done", downloadable: true, archive_bytes: 5 * 1024 * 1024,
    expires_at: "2026-10-29T10:30:00Z",
  })]);
  let release: () => void = () => undefined;
  api.downloadNotebookExport.mockImplementationOnce(() => new Promise<void>((ok) => { release = ok; }));
  render(<NotebookExportSection notebookId="nb1" />);
  expect(await screen.findByText(/导出完成：5.0 MB/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "下载" }));
  expect(api.downloadNotebookExport).toHaveBeenCalledWith("xfer-1");
  expect(screen.getByRole("button", { name: "准备下载…" })).toBeDisabled();
  release();
  expect(await screen.findByRole("button", { name: "已开始下载" })).toHaveClass("transfer-button-done");
  expect(screen.getByRole("button", { name: "重新导出" })).toBeEnabled();
});

test("导出一节:进行中取消 → cancelled,作业消失,「已取消导出。」落在开始按钮旁", async () => {
  const user = userEvent.setup();
  api.listNotebookTransfers.mockResolvedValue([job()]);
  api.deleteNotebookTransfer.mockResolvedValueOnce({ outcome: "cancelled", kept_notebook_id: null });
  render(<NotebookExportSection notebookId="nb1" />);
  await user.click(await screen.findByRole("button", { name: "取消导出" }));
  const start = await screen.findByRole("button", { name: "导出笔记本" });
  expect(within(start.closest(".transfer-actions") as HTMLElement).getByRole("status")).toHaveTextContent("已取消导出。");
});

test("导出一节:失败的作业说原因(按稳定码查表),按钮变成「重新导出」", async () => {
  api.listNotebookTransfers.mockResolvedValue([job({ state: "failed", failure_code: "too_large" })]);
  render(<NotebookExportSection notebookId="nb1" />);
  expect(await screen.findByText("导出文件超过网页导出上限，请联系管理员用命令行导出这本笔记本。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新导出" })).toBeEnabled();
});

test("导出弹窗(卡片菜单入口):被盖住时退出交互树,「×」不禁用", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  api.listNotebookTransfers.mockResolvedValue([job()]);
  const { rerender } = render(<NotebookExportModal notebookId="nb1" notebookName="镜像库" onClose={onClose} />);
  expect(screen.getByRole("heading", { name: "导出「镜像库」" })).toBeInTheDocument();
  await screen.findByRole("button", { name: "取消导出" });
  await user.click(screen.getByTitle("关闭"));
  expect(onClose).toHaveBeenCalledOnce();
  rerender(<NotebookExportModal notebookId="nb1" notebookName="镜像库" onClose={onClose} interactive={false} />);
  const dialog = document.querySelector("section.utility-modal") as HTMLElement;
  expect(dialog).toHaveAttribute("aria-hidden", "true");
  expect(dialog).toHaveAttribute("inert");
});

function importView(over: Partial<NotebookImportView> = {}): NotebookImportView {
  return {
    limitBytes: 512 * 1024 * 1024, file: null, fileInputKey: 0, name: "", fileError: "",
    uploadError: "", uploadNotice: "", uploadFailedUnseen: false, jobs: [], loadingJobs: false,
    rows: {}, notes: {}, fileNames: {}, ...over,
  };
}

function renderImport(view: NotebookImportView) {
  const handlers = {
    onChooseFile: vi.fn(), onNameChange: vi.fn(), onStart: vi.fn(), onStopUpload: vi.fn(),
    onCancelJob: vi.fn(), onRemoveJob: vi.fn(), onOpenNotebook: vi.fn(), onClose: vi.fn(),
  };
  const utils = render(<NotebookImportModal view={view} {...handlers} />);
  return { ...utils, handlers };
}

test("导入弹窗被盖住时退出交互树(aria-hidden + inert),最上层时可交互", () => {
  const { handlers, rerender } = renderImport(importView());
  const dialog = () => document.querySelector("section.utility-modal") as HTMLElement;
  expect(dialog()).not.toHaveAttribute("inert");
  expect(dialog()).toHaveAttribute("aria-modal", "true");
  rerender(<NotebookImportModal view={importView()} {...handlers} interactive={false} />);
  expect(dialog()).toHaveAttribute("aria-hidden", "true");
  expect(dialog()).toHaveAttribute("inert");
});

test("导入弹窗:没选文件不能开始;选了文件交给状态机;上限写在选择器下面", async () => {
  const user = userEvent.setup();
  const { handlers, rerender } = renderImport(importView());
  expect(screen.getByRole("button", { name: "开始导入" })).toBeDisabled();
  expect(screen.getByText(/网页导入上限 512 MB/)).toBeInTheDocument();
  const file = new File(["x"], "库.snbk");
  await user.upload(screen.getByLabelText("导出文件"), file);
  expect(handlers.onChooseFile).toHaveBeenCalledWith(file);
  rerender(<NotebookImportModal view={importView({ file, fileError: "这个文件有 600.0 MB，超过网页导入上限 512 MB；请联系管理员用命令行导入。" })} {...handlers} />);
  expect(screen.getByRole("button", { name: "开始导入" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("请联系管理员用命令行导入");
  rerender(<NotebookImportModal view={importView({ file })} {...handlers} />);
  await user.click(screen.getByRole("button", { name: "开始导入" }));
  expect(handlers.onStart).toHaveBeenCalledOnce();
});

test("导入弹窗:上传中按钮显示百分比并禁用,「停止上传」可点,「×」不因上传而禁用;错误落在按钮旁", async () => {
  const user = userEvent.setup();
  const file = new File(["x"], "库.snbk");
  upload.snapshot = { fileName: "库.snbk", total: 100, percent: 0, stage: "precheck" };
  const { handlers, rerender } = renderImport(importView({ file }));
  expect(screen.getByRole("button", { name: "正在准备…" })).toBeDisabled();
  expect(screen.queryByRole("progressbar")).toBeNull();
  upload.snapshot = { fileName: "库.snbk", total: 100, percent: 45, stage: "uploading" };
  rerender(<NotebookImportModal view={importView({ file })} {...handlers} />);
  expect(screen.getByRole("button", { name: "上传中 45%" })).toBeDisabled();
  const bar = screen.getByRole("progressbar", { name: "上传进度" });
  expect(bar).toHaveAttribute("aria-valuenow", "45");
  expect(bar).toHaveAttribute("aria-valuemin", "0");
  expect(bar).toHaveAttribute("aria-valuemax", "100");
  expect(screen.getByLabelText("导出文件")).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "停止上传" }));
  expect(handlers.onStopUpload).toHaveBeenCalledOnce();
  const close = screen.getByTitle("关闭");
  expect(close).toBeEnabled();
  await user.click(close);
  expect(handlers.onClose).toHaveBeenCalledOnce();
  // 传完了、服务器还在检查文件(解压目录、数文档):不是 100% 停住不动。
  upload.snapshot = { fileName: "库.snbk", total: 100, percent: 100, stage: "checking" };
  rerender(<NotebookImportModal view={importView({ file })} {...handlers} />);
  expect(screen.getByRole("button", { name: "正在检查文件…" })).toBeDisabled();
  // 传完了就停不下来(服务器会建出作业):不给「停止上传」,只说文件已经上传。
  expect(screen.queryByRole("button", { name: "停止上传" })).toBeNull();
  expect(screen.getByText("文件已上传，正在检查…")).toBeInTheDocument();
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "已上传，正在检查文件");
  upload.snapshot = null;
  rerender(<NotebookImportModal view={importView({ file, uploadError: "这个文件的文档超过每本笔记本最多 50 篇的上限；请联系管理员用命令行导入。" })} {...handlers} />);
  const start = screen.getByRole("button", { name: "开始导入" });
  expect(within(start.closest(".transfer-actions") as HTMLElement).getByRole("alert"))
    .toHaveTextContent("请联系管理员用命令行导入");
});

test("导入记录:进行中可取消;发布后说补全检索数据期间提问可能找不全并给「打开笔记本」;失败可移除", async () => {
  const user = userEvent.setup();
  const running = job({ id: "a", kind: "import", phase: "rows", notebook_id: "", notebook_name: "", progress: { rows: 3, rows_total: 10 } });
  const reembed = job({ id: "b", kind: "import", phase: "reembed", notebook_id: "nb-b", notebook_name: "新库" });
  const failed = job({ id: "c", kind: "import", state: "failed", phase: "preflight", notebook_name: "", failure_code: "invalid_archive" });
  const { handlers } = renderImport(importView({ jobs: [running, reembed, failed], fileNames: { a: "上传的.snbk" } }));
  const rows = screen.getAllByTestId("import-job-row");
  expect(within(rows[0]).getByText("上传的.snbk")).toBeInTheDocument();
  expect(within(rows[0]).getByText("正在写入内容（3 / 10）")).toBeInTheDocument();
  await user.click(within(rows[0]).getByRole("button", { name: "取消导入" }));
  expect(handlers.onCancelJob).toHaveBeenCalledWith(running);

  expect(within(rows[1]).getByText(/后台正在补全检索数据，完成前提问可能找不全相关内容/)).toBeInTheDocument();
  expect(within(rows[1]).queryByRole("button", { name: "取消导入" })).toBeNull();
  await user.click(within(rows[1]).getByRole("button", { name: "打开笔记本" }));
  expect(handlers.onOpenNotebook).toHaveBeenCalledWith("nb-b");

  expect(within(rows[2]).getByText(/这个文件无法导入/)).toBeInTheDocument();
  await user.click(within(rows[2]).getByRole("button", { name: "移除记录" }));
  expect(handlers.onRemoveJob).toHaveBeenCalledWith(failed);
});

test("导入记录:补不全时的提示旁边都有「打开笔记本」;只有段/内容块没补完才点「补齐向量」的名", async () => {
  const user = userEvent.setup();
  const done = { state: "done", embedded: 1, remaining: 0, detail: "" };
  const broken = { state: "failed", embedded: 0, remaining: 3, detail: "x" };
  const followup = job({ id: "f", kind: "import", state: "failed", phase: "reembed", notebook_id: "nb-f", notebook_name: "导入的库", failure_code: "import_followup_failed" });
  const chunks = job({ id: "g", kind: "import", state: "succeeded", phase: "done", notebook_id: "nb-g", notebook_name: "另一本",
    reembed: { state: "failed", legs: ["chunks", "kg_objects"], progress: { chunks: broken, kg_objects: done } } });
  const kg = job({ id: "h", kind: "import", state: "succeeded", phase: "done", notebook_id: "nb-h", notebook_name: "第三本",
    reembed: { state: "failed", legs: ["chunks", "kg_relations"], progress: { chunks: done, kg_relations: broken } } });
  const { handlers } = renderImport(importView({ jobs: [followup, chunks, kg] }));
  const rows = screen.getAllByTestId("import-job-row");
  expect(within(rows[1]).getByText(new RegExp(`「看板」的「来源状态」里点「${CHECKUP_FIX.backfill_vectors}」`))).toBeInTheDocument();
  for (const row of [rows[0], rows[2]]) {
    expect(within(row).getByText(/可以删除这本笔记本后重新导入/)).toBeInTheDocument();
    expect(within(row).queryByText(new RegExp(CHECKUP_FIX.backfill_vectors))).toBeNull();
  }
  for (const [row, id] of [[rows[0], "nb-f"], [rows[1], "nb-g"], [rows[2], "nb-h"]] as const) {
    await user.click(within(row).getByRole("button", { name: "打开笔记本" }));
    expect(handlers.onOpenNotebook).toHaveBeenLastCalledWith(id);
  }
});

test("导入记录:在途的行按钮禁用;收尾中说「正在停止」;结果原位替换那一行,留下的笔记本可以直接打开", async () => {
  const user = userEvent.setup();
  const a = job({ id: "a", kind: "import", phase: "rows" });
  const b = job({ id: "b", kind: "import", phase: "rows" });
  const c = job({ id: "c", kind: "import", phase: "publish" });
  const { handlers } = renderImport(importView({
    jobs: [a, b, c],
    rows: { a: { busy: "cancel" }, b: { stopping: true } },
    notes: { c: { tone: "kept", text: "取消时笔记本已经导入完成，它已保留在你的笔记本列表里。", notebookId: "nb-c" } },
  }));
  const rows = screen.getAllByTestId("import-job-row");
  expect(within(rows[0]).getByRole("button", { name: "正在取消…" })).toBeDisabled();
  expect(within(rows[1]).getByText("正在停止，结果确定后这里会更新。")).toBeInTheDocument();
  expect(within(rows[1]).getByRole("button", { name: "正在停止…" })).toBeDisabled();
  expect(within(rows[2]).getByText(/已保留在你的笔记本列表里/)).toBeInTheDocument();
  await user.click(within(rows[2]).getByRole("button", { name: "打开笔记本" }));
  expect(handlers.onOpenNotebook).toHaveBeenCalledWith("nb-c");
});

test("铃铛:完成的导出给「下载」,在途禁用,结果写在它身上;失败原因就在旁边", async () => {
  const user = userEvent.setup();
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  let release: () => void = () => undefined;
  api.downloadNotebookExport.mockImplementationOnce(() => new Promise<void>((ok) => { release = ok; }))
    .mockRejectedValueOnce(new Error("x"));
  render(<TransferBellActions kind="export" state="succeeded" phase="done" jobId="xfer-7" notebookId="nb1" onOpenNotebook={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "下载" }));
  expect(api.downloadNotebookExport).toHaveBeenCalledWith("xfer-7");
  expect(screen.getByRole("button", { name: "准备下载…" })).toBeDisabled();
  release();
  await user.click(await screen.findByRole("button", { name: "已开始下载" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("下载没有开始，请重试。");
});

test("铃铛:「下载」同一渲染周期里的双击只取一张票据", async () => {
  let release: () => void = () => undefined;
  api.downloadNotebookExport.mockImplementationOnce(() => new Promise<void>((ok) => { release = ok; }));
  render(<TransferBellActions kind="export" state="succeeded" phase="done" jobId="xfer-8" notebookId="nb1" onOpenNotebook={vi.fn()} />);
  const button = screen.getByRole("button", { name: "下载" });
  // 两次点击之间没有重渲:disabled 还没生效,只能靠同步闸。
  button.click();
  button.click();
  expect(api.downloadNotebookExport).toHaveBeenCalledTimes(1);
  release();
  expect(await screen.findByRole("button", { name: "已开始下载" })).toBeInTheDocument();
});

test("铃铛:发布之后才失败的导入(失败码 import_followup_*)也给「打开笔记本」,哪怕停在 publish 阶段", async () => {
  const open = vi.fn();
  render(<TransferBellActions kind="import" state="failed" phase="publish" failureCode="import_followup_interrupted" jobId="i2" notebookId="nb-new" onOpenNotebook={open} />);
  await userEvent.setup().click(screen.getByRole("button", { name: "打开笔记本" }));
  expect(open).toHaveBeenCalledOnce();
});

test("铃铛:已经发布的导入给「打开笔记本」;发布前、失败、进行中的导出没有动作", async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  const { rerender } = render(<TransferBellActions kind="import" state="running" phase="reembed" jobId="i1" notebookId="nb-new" onOpenNotebook={open} />);
  await user.click(screen.getByRole("button", { name: "打开笔记本" }));
  expect(open).toHaveBeenCalledOnce();
  for (const props of [
    { kind: "import", state: "running", phase: "rows", notebookId: "nb-new" },
    { kind: "import", state: "running", phase: "reembed", notebookId: "" },
    { kind: "export", state: "running", phase: "writing", notebookId: "nb1" },
    { kind: "export", state: "failed", phase: "writing", notebookId: "nb1" },
    { kind: "import", state: "failed", phase: "rows", notebookId: "nb-new", failureCode: "invalid_archive" },
  ]) {
    rerender(<TransferBellActions {...props} jobId="i1" onOpenNotebook={open} />);
    expect(screen.queryByRole("button")).toBeNull();
  }
});

test("工具栏「导入」:上传中说「导入中…」,弹窗关着时失败了说「导入失败」,点一下都打开导入弹窗", async () => {
  const user = userEvent.setup();
  const onOpen = vi.fn();
  const { rerender } = render(<ImportToolbarButton failed={false} onOpen={onOpen} />);
  expect(screen.getByRole("button", { name: "导入" })).toBeEnabled();
  upload.snapshot = { fileName: "库.snbk", total: 100, percent: 30, stage: "uploading" };
  rerender(<ImportToolbarButton failed={false} onOpen={onOpen} />);
  await user.click(screen.getByRole("button", { name: "导入中…" }));
  expect(onOpen).toHaveBeenCalledTimes(1);
  upload.snapshot = null;
  rerender(<ImportToolbarButton failed onOpen={onOpen} />);
  const failed = screen.getByRole("button", { name: "导入失败" });
  expect(failed).toHaveClass("transfer-toolbar-failed");
  await user.click(failed);
  expect(onOpen).toHaveBeenCalledTimes(2);
});

test("导出一节在读取已有作业时不给可点的开始按钮", async () => {
  api.listNotebookTransfers.mockImplementation(() => new Promise(() => undefined));
  render(<NotebookExportSection notebookId="nb1" />);
  expect(screen.getByRole("status")).toHaveTextContent("正在读取导出状态…");
  expect(screen.queryByRole("button", { name: "导出笔记本" })).toBeNull();
  await waitFor(() => expect(api.listNotebookTransfers).toHaveBeenCalled());
});
