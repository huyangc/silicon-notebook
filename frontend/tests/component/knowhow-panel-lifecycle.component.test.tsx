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
//  * Asymmetric reset sets: fixed in S6. openTable() and backToList() now both
//    clear completeRowId / reformatRowId / reformatTableOpen; the notebook-reset
//    effect (E6) also clears importOpen, transferNotice and jumpNotice. The tests
//    below that used to be named "quirk:" now pin the fixed behaviour with the
//    same request sequences; knowhow-panel-resets holds the full state ×
//    reset-path table. Still deliberate: backToList keeps jumpNotice (a cited
//    table that fails to load writes it and then goes back), and no reset path
//    touches `deleting` (S5: only the DELETE's outcome releases it).
//  * The row drawer re-registered its Escape listener on every panel render:
//    fixed in S6 (pinned in knowhow-panel-golden).
// Found while characterising and fixed in S6 (see the tests): a stray detail
// request on a notebook switch, a re-targeted jump routing against the previous
// table's detail, and a same-table re-targeted jump leaving the grid blank.
//  * The row-code effect (E5) reset its state from an effect whenever no row was
//    open, costing one extra commit after the drawer closed: fixed in S6 (the
//    reset now runs at render time; pinned in knowhow-panel-row-code-reset).
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

test("a notebook switch that also cites a table with the open table's id loads the new notebook's table", async () => {
  // notebookId and initialTableId change in one update, and the cited table's id
  // equals the one already open. The reset effect (E6) clears the selection and
  // the jump effect (E7) selects the same id again in the same batch, so
  // selectedTableId is unchanged across the two commits and the detail effect
  // (E4, which follows selectedTableId only since S6) does not rerun. openTable
  // sees the reset through selectedTableIdRef, treats it as a different table and
  // loads it itself: exactly one detail request, against the new notebook.
  // (Found in the S6 review: without it the grid stayed blank with no request.)
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const other = recordDetail({ title: "另一本的表" });
  server.on("GET", "/notebooks/nb-2/knowhow", { json: [wireSummary({ id: "t-rec", title: other.title, rowCount: other.rows.length })] });
  server.on("GET", "/notebooks/nb-2/knowhow/t-rec", { json: wireDetail(other) });
  const user = userEvent.setup();
  const panel = renderPanel();
  await user.click(await screen.findByRole("button", { name: "打开表格：排障记录" }));
  await screen.findByRole("heading", { name: "排障记录" });
  await settle();

  panel.update({ ...BASE_PROPS, notebookId: "nb-2", initialTableId: "t-rec" });
  expect(await screen.findByRole("heading", { name: "另一本的表" })).toBeInTheDocument();
  await settle();
  expect(screen.getAllByRole("button", { name: "供电异常" })).toHaveLength(1);
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/notebooks/nb-2/knowhow" },
    { method: "GET", path: "/notebooks/nb-2/knowhow/t-rec" },
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

test("re-targeting a jump from an open record table to an anchor table opens the concept matrix", async () => {
  // Until S6 the routing effect (E8) ran in the same commit as the jump effect
  // (E7) and still saw the record table's detail (no anchor column): it opened
  // the row drawer for a3 instead of the concept matrix, marked the target as
  // routed, and fetched a3's row code. E8 now waits for the cited table's own
  // detail, so the outcome matches a jump made from the list.
  const server = suite.server();
  const record = recordDetail();
  const anchor = anchorDetail();
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: record.id, title: record.title })] });
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec", { json: wireDetail(record) });
  server.on("GET", "/notebooks/nb-1/knowhow/t-anc", { json: wireDetail(anchor) });
  server.on("GET", "/agent/knowhow/rows/r1", { json: { code: [] } });
  const panel = renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });
  await screen.findByRole("dialog", { name: /^供电异常/ });

  panel.update({ ...BASE_PROPS, initialTableId: "t-anc", initialRowId: "a3" });
  const matrix = await screen.findByRole("dialog", { name: "概念 电源" });
  expect(screen.getByRole("heading", { name: "概念表" })).toBeInTheDocument();
  const highlighted = within(matrix).getAllByRole("columnheader")
    .filter((header) => header.classList.contains("kh-matrix-branch--hi"));
  expect(highlighted).toHaveLength(1);
  await settle();
  expect(screen.queryByRole("dialog", { name: "电源" })).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: /^供电异常/ })).not.toBeInTheDocument();
  // a3's row code is no longer fetched (no row drawer, as in a jump from the list).
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
  ]);
});

// ---------------------------------------------------------------------------
// Reset sets: openTable / backToList / E6 (asymmetries fixed in S6)
// ---------------------------------------------------------------------------

test("re-targeting a jump to another row of the same record table keeps the grid and opens that row", async () => {
  // Until S6 a new initialRowId with the same initialTableId ran E7 →
  // openTable(sameId), which nulled the detail; selectedTableId did not change,
  // so the detail effect (E4) never refetched and the grid stayed blank until
  // the user went back, while the routing effect (E8) still pointed openRowId at
  // r2 and fetched its code for a drawer that could not render. openTable now
  // keeps the detail of the table it reopens, so E8 routes against it. Same
  // request sequence; the row-code request is now used by the drawer.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const panel = renderPanel({ initialTableId: "t-rec" });
  expect(await screen.findByRole("button", { name: "供电异常" })).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", initialRowId: "r2" });
  const drawer = await screen.findByRole("dialog", { name: "时钟漂移" });
  expect(await within(drawer).findAllByRole("button", { name: /添加代码/ })).toHaveLength(3);
  await settle();
  expect(screen.getByRole("heading", { name: "排障记录" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "供电异常" })).toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r2" },
  ]);
});

test("a jump to the table already open but failed to load falls back to the list with a notice", async () => {
  // Keeping the reopened table's detail (S6) must not turn a failed load into a
  // dead end: the stale-citation effect sees the failure in the same commit and
  // goes back to the list, as for a cited table that fails to load. No refetch.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.once("GET", "/notebooks/nb-1/knowhow/t-rec", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  const panel = renderPanel();
  await user.click(await screen.findByRole("button", { name: "打开表格：排障记录" }));
  expect(await screen.findByText("加载表格详情失败，请重试")).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", initialRowId: "r2" });
  expect(await screen.findByText("未能定位到引用指向的表格，它可能已被删除。")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "打开表格：排障记录" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
  ]);
});

test("re-targeting a jump to another concept of the same anchor table switches the matrix without a reload", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", anchorDetail());
  const panel = renderPanel({ initialTableId: "t-anc", initialRowId: "a2" });
  expect(await screen.findByRole("dialog", { name: "概念 时钟" })).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, initialTableId: "t-anc", initialRowId: "a3" });
  expect(await screen.findByRole("dialog", { name: "概念 电源" })).toBeInTheDocument();
  await settle();
  expect(screen.queryByRole("dialog", { name: "概念 时钟" })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "概念表" })).toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
  ]);
});

test("backToList closes the table batch-reformat modal, so reopening the table does not bring it back", async () => {
  // Until S6 backToList (and openTable) left reformatTableOpen set: reopening the
  // table reopened the batch modal on a fresh snapshot. Request sequence unchanged.
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
  expect(await screen.findByRole("heading", { name: "排障记录" })).toBeInTheDocument();
  await settle();
  expect(screen.queryByRole("dialog", { name: "一键规整整表 · 排障记录" })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
  ]);
});

test("backToList closes the row batch-reformat modal, so reopening the table does not bring it back", async () => {
  // Until S6 backToList (and openTable) left reformatRowId set and the row batch
  // modal came back on reopening. Request sequence unchanged.
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
  expect(await screen.findByRole("heading", { name: "排障记录" })).toBeInTheDocument();
  await settle();
  expect(screen.queryByRole("dialog", { name })).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "供电异常 · 检查电源 · 万用表" })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
  ]);
});

test("a new jump target does not inherit the previous table's batch-reformat modal", async () => {
  // Until S6 openTable left reformatTableOpen set, so the batch modal moved over to
  // the newly cited table. Request sequence unchanged.
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
  expect(await screen.findByRole("heading", { name: "第二张表" })).toBeInTheDocument();
  await settle();
  expect(screen.queryByRole("dialog", { name: /^一键规整整表 · / })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-two" },
  ]);
});

test("the notebook-reset effect clears the stale-citation notice", async () => {
  // Until S6 E6 left jumpNotice set, so the notice about nb-1's table stayed over
  // nb-2's list. Request sequence unchanged.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/agent/knowhow/rows/r-gone", { json: { code: [] } });
  server.on("GET", "/notebooks/nb-2/knowhow", { json: [] });
  const panel = renderPanel({ initialTableId: "t-rec", initialRowId: "r-gone" });
  const notice = "引用指向的行未找到，可能已被删除或调整，已为你展开整表内容。";
  expect(await screen.findByText(notice)).toBeInTheDocument();

  panel.update({ ...BASE_PROPS, notebookId: "nb-2", initialTableId: "t-rec", initialRowId: "r-gone" });
  expect(await screen.findByText("还没有 knowhow 表")).toBeInTheDocument();
  await settle();
  expect(screen.queryByText(notice)).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r-gone" },
    { method: "GET", path: "/notebooks/nb-2/knowhow" },
  ]);
});
