// KnowhowPanel characterization (debt-fix S4, C0): adding rows, concepts and
// branches; deleting branches and concepts from the concept matrix.
//
// Request sequences asserted here are the backend contract and stay after the
// split. See knowhow-panel-lifecycle.component.test.tsx for the list of recorded
// pre-existing quirks the split must carry over unchanged.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import {
  anchorDetail,
  recordDetail,
  renderPanel,
  routeTable,
  settle,
  useKnowhowPanelSuite,
  wireDetail,
  wireRow,
} from "../../test-support/knowhow-panel-harness.tsx";
import type { KnowhowRow, KnowhowTableDetail } from "../../app/knowhow-model.ts";

const suite = useKnowhowPanelSuite();

/** Serves `current` for the table detail, so a test can change server state. */
function liveDetail(notebookId: string, initial: KnowhowTableDetail) {
  const state = { current: initial };
  const server = suite.server();
  routeTable(server, notebookId, initial);
  server.on("GET", `/notebooks/${notebookId}/knowhow/${initial.id}`, () => ({ json: wireDetail(state.current) }));
  return state;
}

function withRow(detail: KnowhowTableDetail, row: KnowhowRow): KnowhowTableDetail {
  return { ...detail, rows: [...detail.rows, row] };
}

function withoutRows(detail: KnowhowTableDetail, ids: string[]): KnowhowTableDetail {
  return { ...detail, rows: detail.rows.filter((row) => !ids.includes(row.id)) };
}

const TABLE_READS = [
  { method: "GET", path: "/notebooks/nb-1/knowhow" },
] as const;

// ---------------------------------------------------------------------------
// 7. Add row / concept / branch
// ---------------------------------------------------------------------------

test("add row: shows 添加中…, appends optimistically, opens the first column's editor, then reloads", async () => {
  const detail = recordDetail();
  const state = liveDetail("nb-1", detail);
  const server = suite.server();
  const newRow: KnowhowRow = { id: "r3", position: 2, projectionStatus: "pending", cells: {} };
  const release = server.hold("POST", "/notebooks/nb-1/knowhow/t-rec/rows");
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const addButton = await screen.findByRole("button", { name: "添加行" });
  await settle();
  // Hold the post-add reload: the editor must appear from the optimistic append
  // alone, before the server's copy of the new row arrives.
  const releaseReload = server.hold("GET", "/notebooks/nb-1/knowhow/t-rec");
  await user.click(addButton);
  expect(addButton).toHaveTextContent("添加中…");
  expect(addButton).toBeDisabled();
  state.current = withRow(detail, newRow);
  release({ json: wireRow(newRow) });

  const editor = await screen.findByRole("dialog", { name: "行 3 › 问题" });
  expect(within(editor).getByRole("textbox")).toHaveValue("");
  // The background reload is still in flight (the grid shows its loading state);
  // the editor found the new row in the optimistically appended detail.
  expect(screen.getByText("加载中…")).toBeInTheDocument();
  expect(server.lines().at(-2)).toBe("GET /notebooks/nb-1/knowhow/t-rec");
  releaseReload({ json: wireDetail(state.current) });
  await settle();
  expect(addButton).toHaveTextContent("添加行");
  expect(screen.getByRole("dialog", { name: "行 3 › 问题" })).toBe(editor);
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/rows", body: { cells: {} } },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
  ]);
});

test("add row failure shows the grid banner and restores the button", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/rows", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const addButton = await screen.findByRole("button", { name: "添加行" });
  await user.click(addButton);
  const banner = await waitFor(() => {
    const node = document.querySelector(".knowhow-action-error");
    expect(node).not.toBeNull();
    return node as HTMLElement;
  });
  expect(banner.textContent?.trim()).not.toBe("");
  expect(addButton).toBeEnabled();
  await user.click(within(banner).getByRole("button", { name: "关闭" }));
  expect(document.querySelector(".knowhow-action-error")).toBeNull();
  await settle();
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/rows", body: { cells: {} } },
  ]);
});

test("+概念 adds an empty row and opens the editor on the anchor column", async () => {
  const detail = anchorDetail();
  const state = liveDetail("nb-1", detail);
  const server = suite.server();
  const newRow: KnowhowRow = { id: "a4", position: 3, projectionStatus: "pending", cells: {} };
  server.on("POST", "/notebooks/nb-1/knowhow/t-anc/rows", () => {
    state.current = withRow(detail, newRow);
    return { json: wireRow(newRow) };
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  const conceptButton = await screen.findByRole("button", { name: "概念" });
  await settle();
  // Hold the post-add reload, as for 添加行: the optimistic append alone must
  // bring up the anchor-column editor.
  const releaseReload = server.hold("GET", "/notebooks/nb-1/knowhow/t-anc");
  await user.click(conceptButton);
  const editor = await screen.findByRole("dialog", { name: "行 4 › 概念" });
  expect(within(editor).getByRole("textbox")).toHaveValue("");
  expect(server.lines().at(-2)).toBe("GET /notebooks/nb-1/knowhow/t-anc");
  releaseReload({ json: wireDetail(state.current) });
  await settle();
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-anc/rows", body: { cells: {} } },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
  ]);
});

test("+分支 posts the concept value as the anchor cell and opens no editor", async () => {
  const detail = anchorDetail();
  const state = liveDetail("nb-1", detail);
  const server = suite.server();
  const newRow: KnowhowRow = { id: "a4", position: 3, projectionStatus: "pending", cells: { "c-anchor": "时钟" } };
  server.on("POST", "/notebooks/nb-1/knowhow/t-anc/rows", () => {
    state.current = withRow(detail, newRow);
    return { json: wireRow(newRow) };
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "时钟" }));
  const matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  expect(within(matrix).getByText("2 个分支")).toBeInTheDocument();
  await user.click(within(matrix).getByRole("button", { name: "分支" }));
  expect(await within(matrix).findByText("3 个分支")).toBeInTheDocument();
  await settle();
  expect(screen.getAllByRole("dialog").map((dialog) => dialog.getAttribute("aria-label"))).toEqual([null, "概念 时钟"]);
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-anc/rows", body: { cells: { "c-anchor": "时钟" } } },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
  ]);
});

test("+分支 failure shows the error inside the drawer, and the grid banner after closing it", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", anchorDetail());
  server.on("POST", "/notebooks/nb-1/knowhow/t-anc/rows", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "时钟" }));
  const matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  await user.click(within(matrix).getByRole("button", { name: "分支" }));
  const drawerError = await waitFor(() => {
    const node = matrix.querySelector(".kh-inline-error");
    expect(node).not.toBeNull();
    return node as HTMLElement;
  });
  const message = drawerError.textContent?.trim() ?? "";
  expect(message).not.toBe("");
  expect(within(matrix).getByRole("button", { name: "分支" })).toBeEnabled();
  await user.click(within(matrix).getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "概念 时钟" })).not.toBeInTheDocument());
  expect(document.querySelector(".knowhow-action-error")?.textContent?.trim()).toBe(message);
  await settle();
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-anc/rows", body: { cells: { "c-anchor": "时钟" } } },
  ]);
});

// ---------------------------------------------------------------------------
// 7. Delete branch / concept
// ---------------------------------------------------------------------------

test("deleting the last branch of a concept closes the drawer; a double confirm sends one DELETE", async () => {
  const detail = anchorDetail();
  const state = liveDetail("nb-1", detail);
  const server = suite.server();
  const release = server.hold("DELETE", "/notebooks/nb-1/knowhow/t-anc/rows/a3");
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "电源" }));
  const matrix = await screen.findByRole("dialog", { name: "概念 电源" });
  await user.click(within(matrix).getByTitle("删除该分支"));
  const confirm = within(matrix).getByRole("button", { name: "确认删除" });
  await user.click(confirm);
  expect(confirm).toHaveTextContent("删除中…");
  expect(confirm).toBeDisabled();
  await user.click(confirm);
  state.current = withoutRows(detail, ["a3"]);
  release({ status: 204 });

  await waitFor(() => expect(screen.queryByRole("dialog", { name: "概念 电源" })).not.toBeInTheDocument());
  await settle();
  expect(screen.queryByRole("button", { name: "电源" })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-anc/rows/a3" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
  ]);
});

test("deleting one of several branches keeps the drawer open on the smaller matrix", async () => {
  const detail = anchorDetail();
  const state = liveDetail("nb-1", detail);
  const server = suite.server();
  server.on("DELETE", "/notebooks/nb-1/knowhow/t-anc/rows/a2", () => {
    state.current = withoutRows(detail, ["a2"]);
    return { status: 204 };
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "时钟" }));
  const matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  await user.click(within(matrix).getAllByTitle("删除该分支")[1]);
  await user.click(within(matrix).getByRole("button", { name: "确认删除" }));
  expect(await within(matrix).findByText("1 个分支")).toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "概念 时钟" })).toBe(matrix);
  await settle();
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-anc/rows/a2" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
  ]);
});

test("deleting a concept deletes every branch row in parallel and closes the drawer", async () => {
  const detail = anchorDetail();
  const state = liveDetail("nb-1", detail);
  const server = suite.server();
  server.on("DELETE", "/notebooks/nb-1/knowhow/t-anc/rows/a1", { status: 204 });
  server.on("DELETE", "/notebooks/nb-1/knowhow/t-anc/rows/a2", () => {
    state.current = withoutRows(detail, ["a1", "a2"]);
    return { status: 204 };
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "时钟" }));
  const matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  await user.click(within(matrix).getByTitle("删除整个概念"));
  expect(within(matrix).getByText("删除整个概念？其下 2 个分支将一并删除")).toBeInTheDocument();
  await user.click(within(matrix).getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "概念 时钟" })).not.toBeInTheDocument());
  await settle();
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-anc/rows/a1" },
    { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-anc/rows/a2" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
  ]);
});

test("a failed concept delete keeps the drawer open, shows the error inside and resets the confirm", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", anchorDetail());
  server.on("DELETE", "/notebooks/nb-1/knowhow/t-anc/rows/a1", { status: 204 });
  server.on("DELETE", "/notebooks/nb-1/knowhow/t-anc/rows/a2", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "时钟" }));
  const matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  await user.click(within(matrix).getByTitle("删除整个概念"));
  await user.click(within(matrix).getByRole("button", { name: "确认删除" }));
  expect(await within(matrix).findByText("删除失败，请重试")).toBeInTheDocument();
  expect(within(matrix).getByTitle("删除整个概念")).toBeInTheDocument();
  expect(within(matrix).queryByRole("button", { name: "确认删除" })).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    ...TABLE_READS,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-anc/rows/a1" },
    { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-anc/rows/a2" },
  ]);
});

test("switching or closing the concept drawer resets a pending delete confirmation", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", anchorDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "时钟" }));
  let matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  await user.click(within(matrix).getByTitle("删除整个概念"));
  expect(within(matrix).getByRole("button", { name: "确认删除" })).toBeInTheDocument();
  await user.click(within(matrix).getByRole("button", { name: "关闭" }));

  await user.click(screen.getByRole("button", { name: "电源" }));
  matrix = await screen.findByRole("dialog", { name: "概念 电源" });
  expect(within(matrix).queryByRole("button", { name: "确认删除" })).not.toBeInTheDocument();
  await user.click(within(matrix).getByRole("button", { name: "关闭" }));

  await user.click(screen.getByRole("button", { name: "时钟" }));
  matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  expect(within(matrix).queryByRole("button", { name: "确认删除" })).not.toBeInTheDocument();
  expect(within(matrix).getByTitle("删除整个概念")).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([...TABLE_READS, { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" }]);
});
