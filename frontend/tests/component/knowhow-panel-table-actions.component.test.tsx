// KnowhowPanel characterization (debt-fix S4, C0): table-level commands (delete,
// re-sync, column rename, template download, copy/move), version revert, the
// batch-reformat concurrency paths, and whole-row optimisation.
//
// Request sequences asserted here are the backend contract and stay after the
// split. See knowhow-panel-lifecycle.component.test.tsx for the list of recorded
// pre-existing quirks the split must carry over unchanged.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import {
  BASE_PROPS,
  recordDetail,
  renderPanel,
  routeTable,
  settle,
  useKnowhowPanelSuite,
  wirePatchResult,
} from "../../test-support/knowhow-panel-harness.tsx";
import type { KnowhowTableDetail } from "../../app/knowhow-model.ts";

const suite = useKnowhowPanelSuite();

const LIST = { method: "GET", path: "/notebooks/nb-1/knowhow" } as const;
const DETAIL = { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" } as const;

function actionBanner(): HTMLElement | null {
  return document.querySelector(".knowhow-action-error");
}

// ---------------------------------------------------------------------------
// 8. Table-level commands
// ---------------------------------------------------------------------------

test("delete table: confirm, DELETE, back to the list and reload it", async () => {
  // Only the success path's navigation is asserted here. What the `deleting` lock
  // does to the grid's buttons around a delete (released on success, kept while
  // the request is in flight) is pinned in knowhow-panel-delete-table.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("DELETE", "/notebooks/nb-1/knowhow/t-rec", { status: 204 });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByTitle("删除表"));
  expect(screen.getByText("删除这张表？行、格与同步生成的内容将一并删除")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByText("删除这张表？行、格与同步生成的内容将一并删除")).not.toBeInTheDocument();
  await user.click(screen.getByTitle("删除表"));
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  expect(await screen.findByRole("button", { name: "打开表格：排障记录" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([LIST, DETAIL, { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-rec" }, LIST]);
});

test("delete table failure shows the banner and resets the confirmation", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("DELETE", "/notebooks/nb-1/knowhow/t-rec", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByTitle("删除表"));
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(actionBanner()).toHaveTextContent("删除失败，请重试"));
  expect(screen.getByTitle("删除表")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "添加行" })).toBeEnabled();
  await user.click(within(actionBanner()!).getByRole("button", { name: "关闭" }));
  expect(actionBanner()).toBeNull();
  await settle();
  expect(server.log).toEqual([LIST, DETAIL, { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-rec" }]);
});

test("re-sync shows 重建中… while in flight, then reloads the detail; failure shows its copy", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const release = server.hold("POST", "/notebooks/nb-1/knowhow/t-rec/reproject");
  server.once("POST", "/notebooks/nb-1/knowhow/t-rec/reproject", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const resync = await screen.findByRole("button", { name: "重新同步" });
  await user.click(resync);
  expect(resync).toHaveTextContent("重建中…");
  expect(resync).toBeDisabled();
  expect(screen.getByText("重新同步中…")).toBeInTheDocument();
  release({ status: 204 });
  await waitFor(() => expect(resync).toHaveTextContent("重新同步"));
  await settle();
  // Second attempt hits the queued failure.
  await user.click(resync);
  await waitFor(() => expect(actionBanner()).toHaveTextContent("重新同步失败，请重试"));
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/reproject" },
    DETAIL,
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/reproject" },
  ]);
});

test("double-click rename: Enter saves and reloads only the list; Esc and an unchanged name send nothing", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/columns/c-fix", {
    json: { id: "c-fix", name: "解决办法", position: 1, kind: "procedure" },
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.dblClick(await screen.findByTitle("处理方法（双击改名）"));
  let input = screen.getByDisplayValue("处理方法");
  await user.clear(input);
  await user.type(input, "临时名{Escape}");
  expect(await screen.findByTitle("处理方法（双击改名）")).toBeInTheDocument();

  await user.dblClick(screen.getByTitle("处理方法（双击改名）"));
  await user.keyboard("{Enter}");
  expect(await screen.findByTitle("处理方法（双击改名）")).toBeInTheDocument();

  await user.dblClick(screen.getByTitle("处理方法（双击改名）"));
  input = screen.getByDisplayValue("处理方法");
  await user.clear(input);
  await user.type(input, "解决办法{Enter}");
  expect(await screen.findByTitle("解决办法（双击改名）")).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "PATCH", path: "/notebooks/nb-1/knowhow/t-rec/columns/c-fix", body: { name: "解决办法" } },
    LIST,
  ]);
});

test("template download shows 下载中… and saves <title>-template.xlsx", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const release = server.hold("GET", "/notebooks/nb-1/knowhow/t-rec/template");
  const createObjectURL = vi.fn(() => "blob:template");
  const revokeObjectURL = vi.fn();
  // jsdom has no object URLs; a subclass keeps every other URL behaviour intact
  // and unstubAllGlobals() restores the real constructor after the test.
  class BlobAwareURL extends URL {
    static createObjectURL = createObjectURL;
    static revokeObjectURL = revokeObjectURL;
  }
  vi.stubGlobal("URL", BlobAwareURL);
  const downloads: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    downloads.push(this.download);
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const button = await screen.findByRole("button", { name: "下载模板" });
  await user.click(button);
  expect(button).toHaveTextContent("下载中…");
  expect(button).toBeDisabled();
  release({ text: "xlsx-bytes" });
  await waitFor(() => expect(button).toHaveTextContent("下载模板"));
  expect(downloads).toEqual(["排障记录-template.xlsx"]);
  expect(createObjectURL).toHaveBeenCalledTimes(1);
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:template");
  expect(server.log).toEqual([LIST, DETAIL, { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec/template" }]);
});

const NOTEBOOKS = [
  { id: "nb-1", name: "源笔记本", access: "owner" },
  { id: "nb-2", name: "目标笔记本", access: "owner" },
];

async function openTransfer(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "复制/移动到…" }));
  const picker = await screen.findByRole("dialog", { name: "复制/移动表：排障记录" });
  await within(picker).findByRole("option", { name: "目标笔记本" });
  await user.selectOptions(within(picker).getByRole("combobox"), "nb-2");
  return picker;
}

test("copy shows a notice and keeps the table open", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/notebooks", { json: NOTEBOOKS });
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/transfer", { json: { new_table_id: "t-copy" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const picker = await openTransfer(user);
  await user.click(within(picker).getByRole("button", { name: "确认" }));
  expect(await screen.findByText("已复制到目标笔记本")).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "复制/移动表：排障记录" })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "排障记录" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "GET", path: "/notebooks" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/transfer", body: { target_notebook_id: "nb-2", mode: "copy" } },
  ]);
});

test("move returns to the list and reloads it", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/notebooks", { json: NOTEBOOKS });
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/transfer", { json: { new_table_id: "t-moved" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const picker = await openTransfer(user);
  await user.click(within(picker).getByRole("radio", { name: /移动/ }));
  await user.click(within(picker).getByRole("button", { name: "确认" }));
  expect(await screen.findByRole("button", { name: "打开表格：排障记录" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "GET", path: "/notebooks" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/transfer", body: { target_notebook_id: "nb-2", mode: "move" } },
    LIST,
  ]);
});

test("a source-cleanup failure closes the picker, shows the server's message and reloads detail and list", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/notebooks", { json: NOTEBOOKS });
  const message = "副本已在目标笔记本落地，但源表没能删除。";
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/transfer", {
    status: 409,
    json: { detail: { code: "source_cleanup_failed", new_table_id: "t-moved", message } },
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const picker = await openTransfer(user);
  await user.click(within(picker).getByRole("radio", { name: /移动/ }));
  await user.click(within(picker).getByRole("button", { name: "确认" }));
  await waitFor(() => expect(actionBanner()).toHaveTextContent(message));
  expect(screen.queryByRole("dialog", { name: "复制/移动表：排障记录" })).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "GET", path: "/notebooks" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/transfer", body: { target_notebook_id: "nb-2", mode: "move" } },
    DETAIL,
    LIST,
  ]);
});

test("quirk: the notebook-reset effect keeps the copy notice across a notebook switch", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/notebooks", { json: NOTEBOOKS });
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/transfer", { json: { new_table_id: "t-copy" } });
  server.on("GET", "/notebooks/nb-2/knowhow", { json: [] });
  const user = userEvent.setup();
  const panel = renderPanel({ initialTableId: "t-rec" });

  const picker = await openTransfer(user);
  await user.click(within(picker).getByRole("button", { name: "确认" }));
  expect(await screen.findByText("已复制到目标笔记本")).toBeInTheDocument();
  panel.update({ ...BASE_PROPS, notebookId: "nb-2", initialTableId: "t-rec" });
  expect(await screen.findByText("还没有 knowhow 表")).toBeInTheDocument();
  expect(screen.getByText("已复制到目标笔记本")).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "GET", path: "/notebooks" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/transfer", body: { target_notebook_id: "nb-2", mode: "copy" } },
    { method: "GET", path: "/notebooks/nb-2/knowhow" },
  ]);
});

// ---------------------------------------------------------------------------
// 6. Version revert
// ---------------------------------------------------------------------------

function historyPage(headSeq: number) {
  const change = (seq: number, kind: string, note = "") => ({
    id: `ch-${seq}`, table_id: "t-rec", seq, kind, actor: "alice", origin: "user",
    fingerprint: `fp-${seq}`, note, created_at: "2026-10-01T08:00:00Z", payload: {},
  });
  return {
    head_seq: headSeq,
    changes: [change(2, "table_meta"), change(1, "table_create", "建表")],
    milestones: [],
  };
}

test("a successful revert in the history drawer reloads the timeline, the detail and the list", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec/history?limit=100", { json: historyPage(2) });
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/revert", { json: { seq: 3, target_seq: 1 } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByRole("button", { name: "历史" }));
  const drawer = await screen.findByRole("dialog", { name: /历史/ });
  await user.click(await within(drawer).findByRole("button", { name: "回到这里" }));
  await user.click(within(drawer).getByRole("button", { name: "确认回退" }));
  await waitFor(() => expect(server.log.filter((entry) => entry.path === "/notebooks/nb-1/knowhow").length).toBe(2));
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec/history?limit=100" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/revert", body: { target_seq: 1, expected_head_seq: 2 } },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec/history?limit=100" },
    DETAIL,
    LIST,
  ]);
});

// ---------------------------------------------------------------------------
// 5. Batch reformat concurrency
// ---------------------------------------------------------------------------

function oneRowDetail(): KnowhowTableDetail {
  return recordDetail({
    rows: [{ id: "r1", position: 0, projectionStatus: "synced", cells: { "c-title": "供电异常", "c-fix": "检查电源", "c-tool": "" } }],
  });
}

function routeReformat(detail: KnowhowTableDetail) {
  const server = suite.server();
  routeTable(server, "nb-1", detail);
  server.on("GET", "/model-services/status", { json: { services: [] } });
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title/reformat/stream", {
    stream: { candidate_md: "供电异常（已规整）", source: "llm", changed: true, source_md: "供电异常" },
  });
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix/reformat/stream", {
    stream: { candidate_md: "检查电源（已规整）", source: "llm", changed: true, source_md: "检查电源" },
  });
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title", {
    json: wirePatchResult("r1", "c-title", "供电异常（已规整）"),
  });
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix", { status: 409, json: { detail: "stale" } });
  return server;
}

const REFORMAT_RUN = [
  { method: "GET", path: "/model-services/status" },
  { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title/reformat/stream" },
  { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix/reformat/stream" },
];

const REFORMAT_SAVE = [
  DETAIL,
  {
    method: "PATCH",
    path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title",
    body: { content_md: "供电异常（已规整）", expected_before: "供电异常", origin: "llm_reformat" },
  },
  {
    method: "PATCH",
    path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix",
    body: { content_md: "检查电源（已规整）", expected_before: "检查电源", origin: "llm_reformat" },
  },
];

async function runAndSave(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "一键规整整表" }));
  const dialog = await screen.findByRole("dialog", { name: "一键规整整表 · 排障记录" });
  await user.click(within(dialog).getByRole("button", { name: "开始规整" }));
  const save = await within(dialog).findByRole("button", { name: "确认保存" });
  await waitFor(() => expect(save).toBeEnabled());
  await user.click(save);
  expect(await within(dialog).findByText(/已完成保存。/)).toBeInTheDocument();
  return dialog;
}

test("a 409 while saving marks the unit stale-skipped; closing the modal reloads the detail exactly once", async () => {
  const server = routeReformat(oneRowDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const dialog = await runAndSave(user);
  expect(within(dialog).getByText(/已完成保存。/)).toHaveTextContent("已完成保存。 1 个已保存，1 个已跳过");
  expect(within(dialog).getByText(/内容已被其他人修改，已跳过——请重新运行规整/)).toBeInTheDocument();
  // Footer close (the header icon carries the same accessible name).
  await user.click(within(dialog).getAllByRole("button", { name: "关闭" }).at(-1)!);
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /一键规整整表/ })).not.toBeInTheDocument());
  await settle();
  expect(server.log).toEqual([LIST, DETAIL, ...REFORMAT_RUN, ...REFORMAT_SAVE, DETAIL]);
});

test("打开格子 after a stale skip reloads first, opens the cell, and the close does not reload again", async () => {
  const server = routeReformat(oneRowDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const dialog = await runAndSave(user);
  await user.click(within(dialog).getByRole("button", { name: "打开格子" }));
  expect(await screen.findByRole("dialog", { name: "供电异常 · 检查电源 › 问题" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: /一键规整整表/ })).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([LIST, DETAIL, ...REFORMAT_RUN, ...REFORMAT_SAVE, DETAIL]);
});

// ---------------------------------------------------------------------------
// 12. Whole-row optimisation
// ---------------------------------------------------------------------------

test("row optimisation: accept saves and advances; error → retry; skip; abort; close is disabled while saving", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const optimizePath = (column: string) => `/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/${column}/optimize/stream`;
  server.on("POST", optimizePath("c-title"), { stream: { suggestion_md: "供电电压异常" } });
  server.once("POST", optimizePath("c-fix"), { status: 500, json: { detail: "boom" } });
  server.once("POST", optimizePath("c-fix"), { stream: { suggestion_md: "先测电压" } });
  server.hold("POST", optimizePath("c-tool"));
  const releaseSave = server.hold("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title");
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: "供电异常 · 检查电源 · 万用表" });
  await user.click(within(drawer).getByRole("button", { name: /优化整行/ }));
  const modal = await screen.findByRole("dialog", { name: "优化整行 · 供电异常 · 检查电源 · 万用表" });
  expect(await within(modal).findByText("供电电压异常")).toBeInTheDocument();
  expect(within(modal).getByText("0/3")).toBeInTheDocument();

  await user.click(within(modal).getByRole("button", { name: /接受/ }));
  // While the accepted suggestion is being saved both close controls are locked.
  const closes = within(modal).getAllByRole("button", { name: "关闭" });
  expect(closes).toHaveLength(2);
  for (const close of closes) expect(close).toBeDisabled();
  releaseSave({ json: wirePatchResult("r1", "c-title", "供电电压异常") });

  // c-fix fails first, then succeeds on retry; skip it, then abort c-tool.
  await user.click(await within(modal).findByRole("button", { name: "重试" }));
  expect(await within(modal).findByText("先测电压")).toBeInTheDocument();
  await user.click(within(modal).getByRole("button", { name: "跳过" }));
  await user.click(await within(modal).findByRole("button", { name: "中止" }));
  await waitFor(() => expect(within(modal).queryByRole("button", { name: "中止" })).not.toBeInTheDocument());
  for (const close of within(modal).getAllByRole("button", { name: "关闭" })) expect(close).toBeEnabled();
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "POST", path: optimizePath("c-title") },
    { method: "PATCH", path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title", body: { content_md: "供电电压异常" } },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "POST", path: optimizePath("c-fix") },
    { method: "POST", path: optimizePath("c-fix") },
    { method: "POST", path: optimizePath("c-tool") },
  ]);
});

test("Escape cannot close the row-optimisation modal while an accepted suggestion is saving", async () => {
  // The modal's window keydown listener is re-registered whenever acceptBusy
  // changes, so the Escape handler sees the in-flight save. A listener frozen at
  // mount would unmount the modal mid-save (found by an S4-c mutation).
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const optimizePath = (column: string) => `/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/${column}/optimize/stream`;
  server.on("POST", optimizePath("c-title"), { stream: { suggestion_md: "供电电压异常" } });
  server.hold("POST", optimizePath("c-fix"));
  const releaseSave = server.hold("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title");
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: "供电异常 · 检查电源 · 万用表" });
  await user.click(within(drawer).getByRole("button", { name: /优化整行/ }));
  const modalName = "优化整行 · 供电异常 · 检查电源 · 万用表";
  const modal = await screen.findByRole("dialog", { name: modalName });
  expect(await within(modal).findByText("供电电压异常")).toBeInTheDocument();
  await user.click(within(modal).getByRole("button", { name: /接受/ }));
  await user.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: modalName })).toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "供电异常 · 检查电源 · 万用表" })).toBeInTheDocument();

  releaseSave({ json: wirePatchResult("r1", "c-title", "供电电压异常") });
  await waitFor(() => {
    for (const close of within(modal).getAllByRole("button", { name: "关闭" })) expect(close).toBeEnabled();
  });
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog", { name: modalName })).not.toBeInTheDocument());
  // The drawer stays; its title now reads the accepted title-column text.
  expect(screen.getByRole("dialog", { name: "供电电压异常 · 检查电源 · 万用表" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    LIST,
    DETAIL,
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "POST", path: optimizePath("c-title") },
    { method: "PATCH", path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title", body: { content_md: "供电电压异常" } },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "POST", path: optimizePath("c-fix") },
  ]);
});
