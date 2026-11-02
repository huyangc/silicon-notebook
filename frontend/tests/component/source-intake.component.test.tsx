import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { useSourceIntake, type SourceIntakeConfiguration } from "../../app/use-source-intake";
import { SourceIntakePanel } from "../../app/source-intake-panel";
import { useSourceLibrary } from "../../app/use-source-library";
import { useRootModalCoordinator } from "../../app/use-root-modal-coordinator";
import { resolveDocumentCapacity } from "../../app/document-limit";
import type { SourceSummary } from "../../app/workspace-model";

const api = vi.hoisted(() => ({
  uploadSources: vi.fn(),
  importUrlSources: vi.fn(),
  detectSourceTypes: vi.fn(),
  collectDirectoryFiles: vi.fn(),
  readDirectoryAsBundleFiles: vi.fn(),
}));
vi.mock("../../app/source-api.ts", async (original) => ({
  ...(await original<typeof import("../../app/source-api.ts")>()),
  uploadSources: api.uploadSources,
  importUrlSources: api.importUrlSources,
  detectSourceTypes: api.detectSourceTypes,
}));
vi.mock("../../app/bundle-intake.ts", async (original) => ({
  ...(await original<typeof import("../../app/bundle-intake.ts")>()),
  collectDirectoryFiles: api.collectDirectoryFiles,
  readDirectoryAsBundleFiles: api.readDirectoryAsBundleFiles,
}));

type Intake = ReturnType<typeof useSourceIntake>;
let intake: Intake;
let library: ReturnType<typeof useSourceLibrary>;
let modals: ReturnType<typeof useRootModalCoordinator>;
const events: string[] = [];
const effects = {
  reportError: vi.fn(),
  notify: vi.fn(() => { events.push("notify"); }),
  refreshCollection: vi.fn(async (_guard: () => boolean) => { events.push("collection"); }),
  revalidateAskAvailability: vi.fn(() => { events.push("ask"); }),
  reloadCheckup: vi.fn((_id: string) => { events.push("checkup"); }),
};
const libraryEffects = {
  setStatusText: vi.fn(), reportError: vi.fn(), setToast: vi.fn(),
  invalidateKnowledge: vi.fn(), refreshCollection: vi.fn(async () => undefined),
  refreshNotebook: vi.fn(async () => undefined), refreshCheckup: vi.fn(),
};
const config: SourceIntakeConfiguration = {
  sourceUploadMaxBytes: 100000, sourceUploadMaxFilesPerBatch: 10,
  sourceImageMaxBytes: 10000, sourceImageMaxPerSource: 5,
  sourceImagesEnabled: true, supportedSourceExtensions: ["pdf", "md", "zip"],
};
function Harness({ actorId = "actor", notebookId = "a", limit = 10, configuration = config }: {
  actorId?: string; notebookId?: string; limit?: number;
  configuration?: SourceIntakeConfiguration;
}) {
  library = useSourceLibrary({ actorId, canWriteSources: true, effects: libraryEffects });
  modals = useRootModalCoordinator({
    actorId, sourceId: null,
    onClosed: (slot) => {
      if (slot === "source-add") {
        events.push("close");
        intake.commands.dismiss();
      }
    },
  });
  intake = useSourceIntake({
    notebookId, configuration,
    capacity: resolveDocumentCapacity({
      isAdmin: false, documentLimit: limit, documentCount: library.notebookDocumentCount,
    }),
    library, modals, effects,
  });
  return modals.view("source-add").open
    ? <SourceIntakePanel intake={intake} modal={modals.view("source-add")}
      docTypeOptions={[{ id: "", label: "自动检测" }, { id: "paper", label: "论文" }]}
      onManageSources={vi.fn()} />
    : null;
}
function source(id: string, notebookId = "a"): SourceSummary {
  return {
    id, notebook_id: notebookId, title: id, display_title: id, type: "file", status: "extracted",
    parse_status: "extracted", summary: "", element_count: 0, file_name: id + ".pdf",
    file_size: 1, created_at: "", created_label: "",
  };
}
function enter(notebookId = "a", epoch = 1, actorId = "actor") {
  act(() => {
    const transition = modals.beginWorkspaceTransition();
    library.beginTransition();
    intake.commands.detachRequests();
    library.commitNotebookSnapshot({
      actorId, notebookId, workspaceEpoch: epoch,
      page: { items: [], total_count: 0, offset: 0, limit: 50 },
    });
    modals.finishWorkspaceTransition(transition, { actorId, notebookId, workspaceEpoch: epoch });
  });
}
function open() { act(() => { intake.commands.openFiles(); }); }
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}
function file(name: string, body = "document") {
  const result = new File([body], name);
  Object.defineProperty(result, "text", { value: async () => body });
  Object.defineProperty(result, "slice", { value: () => ({ text: async () => body }) });
  return result;
}
async function stage(...files: File[]) {
  await act(async () => {
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files },
    });
  });
}
function dropDirectory() {
  const entry = { name: "folder", isDirectory: true, isFile: false };
  fireEvent.drop(document.querySelector(".drop-zone")!, {
    dataTransfer: { files: [], items: [{ webkitGetAsEntry: () => entry }] },
  });
}

beforeEach(() => {
  events.length = 0;
  vi.clearAllMocks();
  for (const mock of Object.values(api)) mock.mockReset();
  api.detectSourceTypes.mockResolvedValue([]);
});

test("picker and drop append accepted files, retain rejection messages and upload ZIP unchanged", async () => {
  render(<Harness />);
  enter(); open();
  await stage(file("first.pdf"), file("unsupported.exe"));
  const zip = file("images.zip");
  await act(async () => {
    fireEvent.drop(document.querySelector(".drop-zone")!, {
      dataTransfer: { files: [zip], items: [{ webkitGetAsEntry: () => null }] },
    });
  });
  expect(intake.view.stagedFiles.map((item) => item.name)).toEqual(["first.pdf", "images.zip"]);
  expect(screen.getByText("unsupported.exe")).toBeInTheDocument();
  expect(api.collectDirectoryFiles).not.toHaveBeenCalled();
  api.uploadSources.mockResolvedValue([source("new")]);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "上传 2 个文件" })); });
  const payload = api.uploadSources.mock.calls[0][1] as FormData;
  expect(payload.getAll("files")).toEqual(expect.arrayContaining([zip]));
  expect(payload.getAll("doc_types")).toEqual(["", ""]);
  expect(payload.getAll("doc_type_explicit")).toEqual(["0", "0"]);
  expect(events).toEqual(["collection", "ask", "checkup", "close", "notify"]);
  expect(library.sources.map((item) => item.id)).toEqual(["new"]);
  expect(intake.view.stagedFiles).toEqual([]);
});

test("manual auto-detection choice wins over a pending suggestion and remains explicit on upload", async () => {
  const detection = deferred<{ name: string; doc_type_id: string }[]>();
  api.detectSourceTypes.mockReturnValueOnce(detection.promise);
  render(<Harness />);
  enter(); open();
  await stage(file("notes.md"));
  act(() => { intake.commands.setStagedDocType(0, ""); });
  await act(async () => { detection.resolve([{ name: "notes.md", doc_type_id: "paper" }]); });
  expect(intake.view.stagedDocTypes).toEqual([""]);
  api.uploadSources.mockResolvedValue([]);
  await act(async () => { await intake.commands.confirmUpload(); });
  const payload = api.uploadSources.mock.calls[0][1] as FormData;
  expect(payload.getAll("doc_type_explicit")).toEqual(["1"]);
});

test("in-flight file upload disables repeat submission; success commits before the refresh effects", async () => {
  const upload = deferred<SourceSummary[]>();
  api.uploadSources.mockReturnValueOnce(upload.promise);
  render(<Harness />);
  enter(); open();
  await stage(file("paper.pdf"));
  let running!: Promise<void>;
  act(() => { running = intake.commands.confirmUpload(); });
  expect(screen.getByRole("button", { name: "上传中…" })).toBeDisabled();
  await act(async () => { await intake.commands.confirmUpload(); });
  expect(api.uploadSources).toHaveBeenCalledTimes(1);
  await act(async () => { upload.resolve([source("new")]); await running; });
  expect(library.notebookDocumentCount).toBe(1);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("late upload after A→B→A cannot commit, refresh or close the new intake", async () => {
  const upload = deferred<SourceSummary[]>();
  api.uploadSources.mockReturnValueOnce(upload.promise);
  const rendered = render(<Harness />);
  enter(); open();
  await stage(file("old.pdf"));
  let running!: Promise<void>;
  act(() => { running = intake.commands.confirmUpload(); });
  rendered.rerender(<Harness notebookId="b" />);
  enter("b", 2);
  rendered.rerender(<Harness />);
  enter("a", 3); open();
  await stage(file("current.pdf"));
  events.length = 0;
  await act(async () => { upload.resolve([source("old")]); await running; });
  expect(library.sources).toEqual([]);
  expect(intake.view.stagedFiles.map((item) => item.name)).toEqual(["current.pdf"]);
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(events).toEqual([]);
});

test("bundle choice reset settles the pending chain and drops all late files and receipts", async () => {
  api.collectDirectoryFiles.mockResolvedValue({
    entries: [{ path: "one.md", file: file("one.md") }], truncated: false, overBudget: false,
  });
  api.readDirectoryAsBundleFiles.mockResolvedValue([
    { path: "one.md", bytes: new TextEncoder().encode("# One") },
    { path: "two.md", bytes: new TextEncoder().encode("# Two") },
  ]);
  render(<Harness />);
  enter(); open();
  await act(async () => { dropDirectory(); });
  expect(intake.view.bundleChoice?.selected.size).toBe(2);
  expect(document.querySelector('input[type="file"]')).toBeDisabled();
  await act(async () => { intake.commands.resetStagedIntake(); });
  expect(intake.view.bundleChoice).toBeNull();
  expect(intake.view.bundleBusyLabel).toBeNull();
  expect(intake.view.stagedFiles).toEqual([]);
  expect(intake.view.bundleReceipts).toEqual([]);
  expect(document.querySelector('input[type="file"]')).not.toBeDisabled();
});

test("directory results resolving after close cannot reopen or populate a fresh draft", async () => {
  const reading = deferred<{ entries: { path: string; file: File }[]; truncated: boolean; overBudget: boolean }>();
  api.collectDirectoryFiles.mockReturnValueOnce(reading.promise);
  render(<Harness />);
  enter(); open();
  act(() => { dropDirectory(); });
  act(() => { intake.commands.closeSourceModal(); });
  await act(async () => { reading.resolve({
    entries: [{ path: "late.pdf", file: file("late.pdf") }], truncated: false, overBudget: false,
  }); });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  open();
  expect(intake.view.stagedFiles).toEqual([]);
  expect(intake.view.stagedSkipped).toEqual([]);
});

test("capacity and missing upload limits block file entry while URL import keeps its existing path", async () => {
  const rendered = render(<Harness configuration={{ ...config, sourceUploadMaxBytes: null }} />);
  enter(); open();
  expect(document.querySelector('input[type="file"]')).toBeDisabled();
  expect(screen.getByRole("button", { name: "添加链接" })).not.toBeDisabled();
  rendered.rerender(<Harness limit={1} />);
  await stage(file("one.pdf"), file("two.pdf"));
  expect(screen.getByRole("button", { name: "上传 2 个文件" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("当前仅可再上传 1 个文档");
  expect(api.uploadSources).not.toHaveBeenCalled();
});

test("URL import retains rejected rows and resets draft only after an entirely accepted response", async () => {
  render(<Harness />);
  enter(); open();
  fireEvent.click(screen.getByRole("button", { name: "添加链接" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "https://example.test/one.pdf" } });
  api.importUrlSources.mockResolvedValueOnce({
    created: [source("url")], rejected: [{ url: "https://example.test/bad", reason: "无法添加" }],
  });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "添加并解析" })); });
  expect(api.importUrlSources).toHaveBeenCalledWith("a", ["https://example.test/one.pdf"]);
  expect(screen.getByText("无法添加")).toBeInTheDocument();
  expect(intake.view.urlText).toBe("https://example.test/one.pdf");
  expect(events).toEqual(["collection", "ask", "checkup", "notify"]);
  api.importUrlSources.mockResolvedValueOnce({ created: [], rejected: [] });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "添加并解析" })); });
  expect(intake.view.linkSectionOpen).toBe(false);
  expect(intake.view.urlText).toBe("");
});

