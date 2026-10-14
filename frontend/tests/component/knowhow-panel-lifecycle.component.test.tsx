// KnowhowPanel characterization (debt-fix S4, C0): list lifecycle, citation
// jumps, and the reset sets of openTable / backToList / the notebook-reset effect.
//
// These suites pin what the panel does today, before it is split into modules
// (S4-b styles, S4-c modals, S4-d list/grid/row/concept slices). Every request
// sequence asserted here is the backend contract and stays forever; see
// knowhow-panel-golden.component.test.tsx for the split-period-only baselines.
//
// Recorded pre-existing quirks (plan §5 "仍只登记"; fixing any of them would change
// the request or commit baselines, so the split must carry them over verbatim and
// they are revisited only after S4):
//  * Asymmetric reset sets. backToList() does not clear completeRowId,
//    reformatRowId or reformatTableOpen (nor importOpen/createOpen); openTable()
//    does not clear reformatRowId/reformatTableOpen; the notebook-reset effect
//    (E6) does not clear deleting, transferNotice or jumpNotice (importOpen: S6). The
//    tests named "quirk:" below observe the visible consequences with their full
//    request sequences: reformatTableOpen survives back-and-reopen and a
//    re-targeted jump, reformatRowId survives back-and-reopen, and the jump
//    notice survives a notebook switch (the transfer notice in
//    knowhow-panel-table-actions). knowhow-panel-resets holds the full
//    state × reset-path table. The completeRowId leftover is masked (every way
//    back into a table goes through openTable, which clears it), and `deleting`
//    is S5's bug, left unasserted.
//  * The row drawer re-registers its Escape listener on every panel render
//    (pinned in knowhow-panel-golden).
// Found while characterising (also recorded as-is, see the tests): a re-targeted
// jump routing against the stale detail; a same-table re-targeted jump leaving
// the grid blank. (A stray detail request on a notebook switch was fixed in S6;
// see the notebook-switch test.)
//  * The row-code effect (E5) resets its state from an effect whenever no row is
//    open: on mount those writes coalesce with the list effect's (E3) loading
//    render; after closing the drawer they cost one extra commit (pinned in
//    knowhow-panel-golden).
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import {
  BASE_PROPS,
  anchorDetail,
  recordDetail,
  renderPanel,
  routeTable,
  settle,
  useKnowhowPanelSuite,
  wireDetail,
  wireSummary,
} from "../../test-support/knowhow-panel-harness.tsx";

const suite = useKnowhowPanelSuite();

// ---------------------------------------------------------------------------
// 1. Lifecycle
// ---------------------------------------------------------------------------

test("list load failure shows an inline retry that reloads the list", async () => {
  const server = suite.server();
  server.once("GET", "/notebooks/nb-1/knowhow", { status: 500, json: { detail: "boom" } });
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: "t-rec", title: "排障记录", rowCount: 2 })] });
  const user = userEvent.setup();
  renderPanel();

  expect(await screen.findByText("加载 knowhow 表失败，请重试")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByRole("button", { name: "打开表格：排障记录" })).toBeInTheDocument();
  expect(screen.queryByText("加载 knowhow 表失败，请重试")).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
  ]);
});

test("empty list copy distinguishes editors, read-only members and an empty filter", async () => {
  const server = suite.server();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [] });
  const editor = renderPanel();
  expect(await screen.findByText("还没有 knowhow 表")).toBeInTheDocument();
  expect(screen.getByText("可从 Excel/CSV/Markdown 导入，或新建空表现场定表头")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /新建表/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /导入表格/ })).toBeInTheDocument();
  editor.unmount();

  const reader = renderPanel({ canEdit: false });
  expect(await screen.findByText("还没有 knowhow 表")).toBeInTheDocument();
  expect(screen.getByText("当前为只读访问")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /新建表/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /导入表格/ })).not.toBeInTheDocument();
  reader.unmount();

  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: "t-ok", title: "健康表", rowCount: 3 })] });
  renderPanel({ initialHealthFilter: "projection_failed" });
  expect(await screen.findByText("当前筛选下没有表")).toBeInTheDocument();
  expect(screen.queryByText("可从 Excel/CSV/Markdown 导入，或新建空表现场定表头")).not.toBeInTheDocument();
  expect(screen.queryByText("当前为只读访问")).not.toBeInTheDocument();
  await settle();
  // One list request per mount.
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
  ]);
});

test("the health filter follows initialHealthFilter changes without refetching the list", async () => {
  const server = suite.server();
  server.on("GET", "/notebooks/nb-1/knowhow", {
    json: [
      wireSummary({ id: "t-pending", title: "待同步表", projectionPending: 2 }),
      wireSummary({ id: "t-ok", title: "健康表" }),
    ],
  });
  const panel = renderPanel({ initialHealthFilter: "all" });
  expect(await screen.findByRole("button", { name: "打开表格：健康表" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打开表格：待同步表" })).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, initialHealthFilter: "projection_pending" });
  await waitFor(() => expect(screen.queryByRole("button", { name: "打开表格：健康表" })).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "打开表格：待同步表" })).toBeInTheDocument();
  expect(screen.getByRole("combobox")).toHaveValue("projection_pending");

  panel.update({ ...BASE_PROPS, initialHealthFilter: undefined });
  expect(await screen.findByRole("button", { name: "打开表格：健康表" })).toBeInTheDocument();
  expect(screen.getByRole("combobox")).toHaveValue("all");
  await settle();
  expect(server.log).toEqual([{ method: "GET", path: "/notebooks/nb-1/knowhow" }]);
});

test("opening a table loads its detail once; going back shows the cached list without a reload", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const user = userEvent.setup();
  renderPanel();

  await user.click(await screen.findByRole("button", { name: "打开表格：排障记录" }));
  expect(await screen.findByRole("heading", { name: "排障记录" })).toBeInTheDocument();
  expect(screen.getByText("电源相关")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "返回" }));
  expect(await screen.findByRole("button", { name: "打开表格：排障记录" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
  ]);
});

test("a late detail response for a table the user already left is discarded", async () => {
  const server = suite.server();
  const first = recordDetail();
  const second = recordDetail({
    id: "t-two",
    title: "第二张表",
    description: "",
    rows: [{ id: "s1", position: 0, projectionStatus: "synced", cells: { "c-title": "风扇异响", "c-fix": "", "c-tool": "" } }],
  });
  server.on("GET", "/notebooks/nb-1/knowhow", {
    json: [wireSummary({ id: first.id, title: first.title }), wireSummary({ id: second.id, title: second.title })],
  });
  const releaseFirst = server.hold("GET", "/notebooks/nb-1/knowhow/t-rec");
  server.on("GET", "/notebooks/nb-1/knowhow/t-two", { json: wireDetail(second) });
  const user = userEvent.setup();
  renderPanel();

  await user.click(await screen.findByRole("button", { name: "打开表格：排障记录" }));
  expect(await screen.findByText("加载中…")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "返回" }));
  await user.click(await screen.findByRole("button", { name: "打开表格：第二张表" }));
  expect(await screen.findByRole("heading", { name: "第二张表" })).toBeInTheDocument();
  releaseFirst({ json: wireDetail(first) });
  await settle();
  expect(screen.getByRole("heading", { name: "第二张表" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "风扇异响" })).toBeInTheDocument();
  expect(screen.queryByText("供电异常")).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-two" },
  ]);
});

test("switching notebookId resets the open table and reloads the new notebook's list", async () => {
  // Only the new notebook's list is requested. Until S6 the detail effect (E4)
  // also depended on loadDetail, whose identity changes with notebookId, and ran
  // before the reset effect (E6) in the same commit with the old selectedTableId:
  // one stray `GET /notebooks/nb-2/knowhow/t-rec` whose response was discarded.
  // E4 now follows selectedTableId only. nb-2/t-rec is deliberately unrouted, so
  // a regression also fails the suite's no-unrouted-request check.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/notebooks/nb-2/knowhow", { json: [wireSummary({ id: "t-other", title: "别的表" })] });
  const panel = renderPanel({ initialTableId: "t-rec" });
  expect(await screen.findByRole("heading", { name: "排障记录" })).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, notebookId: "nb-2", initialTableId: "t-rec" });
  expect(await screen.findByRole("button", { name: "打开表格：别的表" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "排障记录" })).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/notebooks/nb-2/knowhow" },
  ]);
});

test("switching notebookId closes an open import wizard", async () => {
  // The wizard uploads into the panel's current notebookId. Until S6 the reset
  // effect (E6) left importOpen alone, so an import started in nb-1 stayed open
  // over nb-2's list and would have been submitted to nb-2.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/notebooks/nb-2/knowhow", { json: [wireSummary({ id: "t-other", title: "别的表" })] });
  const user = userEvent.setup();
  const panel = renderPanel();
  await user.click(await screen.findByRole("button", { name: /导入表格/ }));
  expect(await screen.findByRole("dialog", { name: "导入 Knowhow 表" })).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, notebookId: "nb-2" });
  expect(await screen.findByRole("button", { name: "打开表格：别的表" })).toBeInTheDocument();
  await settle();
  expect(screen.queryByRole("dialog", { name: "导入 Knowhow 表" })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-2/knowhow" },
  ]);
});

// ---------------------------------------------------------------------------
// 2. Citation jumps
// ---------------------------------------------------------------------------

test("mount-time jump into a record table opens the cited row's drawer (pins E6 before E7)", async () => {
  // The notebook-reset effect (E6) clears selectedTableId on mount and the jump
  // effect (E7) then selects the cited table; the later write wins inside one
  // batch. Swapping the two effects would make every mount-time jump a no-op.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: /^供电异常/ });
  expect(within(drawer).getByRole("heading", { name: /^供电异常/ })).toBeInTheDocument();
  expect(screen.queryByText(/引用指向的行未找到/)).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
  ]);
});

test("jump into an anchor table opens the concept matrix and highlights the cited branch", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", anchorDetail());
  renderPanel({ initialTableId: "t-anc", initialRowId: "a2" });

  const matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  const headers = within(matrix).getAllByRole("columnheader");
  const highlighted = headers.filter((header) => header.classList.contains("kh-matrix-branch--hi"));
  expect(highlighted.map((header) => within(header).getByText(/^分支/).textContent)).toEqual(["分支 2"]);
  expect(screen.queryByRole("dialog", { name: /^时钟/ })).not.toBeInTheDocument();
  await settle();
  // No row drawer, so no row-code request.
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
  ]);
});

test("a cited row that no longer exists shows a notice and leaves the whole table open", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/agent/knowhow/rows/r-gone", { json: { code: [] } });
  renderPanel({ initialTableId: "t-rec", initialRowId: "r-gone" });

  expect(await screen.findByText("引用指向的行未找到，可能已被删除或调整，已为你展开整表内容。")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "排障记录" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: /供电异常/ })).not.toBeInTheDocument();
  await settle();
  // The jump still points openRowId at the missing row, so its code is fetched.
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r-gone" },
  ]);
});

test("a cited table that fails to load falls back to the list with a notice", async () => {
  const server = suite.server();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: "t-x", title: "别的表" })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-gone", { status: 404, json: { detail: "not found" } });
  renderPanel({ initialTableId: "t-gone", initialRowId: "r1" });

  expect(await screen.findByText("未能定位到引用指向的表格，它可能已被删除。")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "打开表格：别的表" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-gone" },
  ]);
});

test("closing the jumped-to drawer and then editing a cell does not reopen the drawer", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r2/cells/c-fix", {
    json: { row_id: "r2", column_id: "c-fix", content_md: "校准晶振", projection_status: "pending" },
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: /^供电异常/ });
  await user.click(within(drawer).getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /^供电异常/ })).not.toBeInTheDocument());

  const emptyCells = screen.getAllByRole("button", { name: "+" });
  await user.click(emptyCells[0]);
  const editor = await screen.findByRole("dialog", { name: /时钟漂移.*› 处理方法/ });
  await user.type(within(editor).getByRole("textbox"), "校准晶振");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 处理方法/ })).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "校准晶振" })).toBeInTheDocument();
  await settle();
  expect(screen.queryByRole("dialog", { name: /^供电异常/ })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "PATCH", path: "/notebooks/nb-1/knowhow/t-rec/rows/r2/cells/c-fix", body: { content_md: "校准晶振" } },
  ]);
});

test("quirk: re-targeting a jump while a record table is loaded routes the new row against the stale detail", async () => {
  // Recorded as-is (found while characterising, not in the plan's list): when
  // initialTableId/initialRowId change while a record table's detail is loaded,
  // the routing effect (E8) runs in the same commit as the jump effect (E7) and
  // still sees the old detail (no anchor column), so it opens the row drawer for
  // the cited row instead of the concept matrix, and marks the target as routed.
  // A jump from the list (no detail loaded) opens the matrix as designed — see the
  // anchor-jump test above. Do not change during the split.
  const server = suite.server();
  const record = recordDetail();
  const anchor = anchorDetail();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: record.id, title: record.title })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { json: wireDetail(record) });
  server.on("GET", "/notebooks/nb-1/knowhow/t-anc", { json: wireDetail(anchor) });
  server.on("GET", "/agent/knowhow/rows/r1", { json: { code: [] } });
  server.on("GET", "/agent/knowhow/rows/a3", { json: { code: [] } });
  const panel = renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });
  await screen.findByRole("dialog", { name: /^供电异常/ });

  panel.update({ ...BASE_PROPS, initialTableId: "t-anc", initialRowId: "a3" });
  expect(await screen.findByRole("dialog", { name: "电源" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "概念表" })).toBeInTheDocument();
  await settle();
  expect(screen.queryByRole("dialog", { name: "概念 电源" })).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: /^供电异常/ })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    { method: "GET", path: "/agent/knowhow/rows/a3" },
  ]);
});

// ---------------------------------------------------------------------------
// Recorded quirks: asymmetric reset sets (do not fix during the split)
// ---------------------------------------------------------------------------

test("quirk: re-targeting a jump to another row of the same table blanks the grid without reloading it", async () => {
  // Recorded as-is (found while characterising, not in the plan's list): a new
  // initialRowId with the same initialTableId runs E7 → openTable(sameId), which
  // sets detail to null; selectedTableId does not change, so the detail effect
  // (E4) does not run again and the grid stays empty until the user goes back.
  // The routing effect (E8) still sees the old detail and points openRowId at the
  // cited row, so that row's code is fetched although no drawer can render.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const panel = renderPanel({ initialTableId: "t-rec" });
  expect(await screen.findByRole("button", { name: "供电异常" })).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", initialRowId: "r2" });
  await waitFor(() => expect(screen.queryByRole("button", { name: "供电异常" })).not.toBeInTheDocument());
  await settle();
  expect(screen.queryByRole("heading", { name: "排障记录" })).not.toBeInTheDocument();
  expect(screen.queryByPlaceholderText("按行标题/全文过滤行…")).not.toBeInTheDocument();
  expect(screen.queryByText("加载中…")).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "时钟漂移" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "返回" })).toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r2" },
  ]);
});

test("quirk: backToList keeps reformatTableOpen, so reopening the table reopens the batch modal", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByRole("button", { name: "一键规整整表" }));
  expect(await screen.findByRole("dialog", { name: "一键规整整表 · 排障记录" })).toBeInTheDocument();
  // The batch overlay covers the toolbar for a pointer; the state machine is what
  // is being characterised, so the click goes straight to the DOM node.
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  await user.click(await screen.findByRole("button", { name: "打开表格：排障记录" }));
  expect(await screen.findByRole("dialog", { name: "一键规整整表 · 排障记录" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
  ]);
});

test("quirk: backToList keeps reformatRowId, so reopening the table reopens the row batch modal", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByRole("button", { name: "供电异常" }));
  const drawer = await screen.findByRole("dialog", { name: "供电异常 · 检查电源 · 万用表" });
  await user.click(within(drawer).getByRole("button", { name: /一键规整整行/ }));
  const name = "一键规整整行 · 供电异常 · 检查电源 · 万用表";
  expect(await screen.findByRole("dialog", { name })).toBeInTheDocument();
  // As above: the overlay covers the toolbar for a pointer, not for the DOM.
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  await user.click(await screen.findByRole("button", { name: "打开表格：排障记录" }));
  expect(await screen.findByRole("dialog", { name })).toBeInTheDocument();
  await settle();
  // The row drawer itself is gone (both resets clear openRowId); only the batch
  // modal came back.
  expect(screen.queryByRole("dialog", { name: "供电异常 · 检查电源 · 万用表" })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
  ]);
});

test("quirk: openTable keeps reformatTableOpen, so a new jump target inherits the batch modal", async () => {
  const server = suite.server();
  const record = recordDetail();
  const second = recordDetail({ id: "t-two", title: "第二张表" });
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: record.id, title: record.title })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { json: wireDetail(record) });
  server.on("GET", "/notebooks/nb-1/knowhow/t-two", { json: wireDetail(second) });
  const user = userEvent.setup();
  const panel = renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByRole("button", { name: "一键规整整表" }));
  expect(await screen.findByRole("dialog", { name: "一键规整整表 · 排障记录" })).toBeInTheDocument();
  panel.update({ ...BASE_PROPS, initialTableId: "t-two" });
  expect(await screen.findByRole("dialog", { name: "一键规整整表 · 第二张表" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-two" },
  ]);
});

test("quirk: the notebook-reset effect keeps the jump notice across a notebook switch", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/agent/knowhow/rows/r-gone", { json: { code: [] } });
  server.on("GET", "/notebooks/nb-2/knowhow", { json: [] });
  const panel = renderPanel({ initialTableId: "t-rec", initialRowId: "r-gone" });
  const notice = "引用指向的行未找到，可能已被删除或调整，已为你展开整表内容。";
  expect(await screen.findByText(notice)).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, notebookId: "nb-2", initialTableId: "t-rec", initialRowId: "r-gone" });
  expect(await screen.findByText("还没有 knowhow 表")).toBeInTheDocument();
  expect(screen.getByText(notice)).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r-gone" },
    { method: "GET", path: "/notebooks/nb-2/knowhow" },
  ]);
});
