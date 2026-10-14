// KnowhowPanel: the table-delete flow releases its in-flight lock exactly when the
// DELETE lands (debt-fix S5).
//
// `deleting` disables every table-level write button and turns the confirmation
// into a disabled 删除中…. Before S5 only the failure path reset it, so after a
// successful delete every table opened in the same panel session had 添加行 /
// 追加导入 / 管理 / 复制/移动到… / 历史 / 重新同步 greyed out and the delete
// confirmation stuck on 删除中….
//
// The reset sits in the success branch only, next to backToList() in one
// synchronous block, so:
//  * the grid leaves and the lock lifts in the same commit: there is never a
//    frame with a clickable 确认删除 between the DELETE landing and the list;
//  * backToList()/openTable do not reset it: going 返回 while the DELETE is in
//    flight and reopening the same table keeps the buttons disabled until the
//    request lands, so the same table cannot be deleted twice.
//
// The grid is wrapped (not replaced) to record the delete props of every render.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import {
  recordDetail,
  renderPanel,
  routeTable,
  settle,
  useKnowhowPanelSuite,
  wireDetail,
  wireSummary,
} from "../../test-support/knowhow-panel-harness.tsx";

type GridRender = { tableId: string | null; confirmDelete: boolean; deleting: boolean };

const gridRenders = vi.hoisted(() => [] as GridRender[]);

vi.mock("../../app/knowhow-table-grid.tsx", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-table-grid.tsx")>();
  const RealGrid = actual.KnowhowTableGrid;
  return {
    ...actual,
    KnowhowTableGrid: (props: Parameters<typeof RealGrid>[0]) => {
      gridRenders.push({ tableId: props.detail?.id ?? null, confirmDelete: props.confirmDelete, deleting: props.deleting });
      return <RealGrid {...props} />;
    },
  };
});

const suite = useKnowhowPanelSuite();

const LIST = { method: "GET", path: "/notebooks/nb-1/knowhow" } as const;
const DETAIL = { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" } as const;
const DELETE = { method: "DELETE", path: "/notebooks/nb-1/knowhow/t-rec" } as const;
const TWIN_DETAIL = { method: "GET", path: "/notebooks/nb-1/knowhow/t-two" } as const;

// Every button `deleting` disables (canEdit=true), by accessible name.
const LOCKED_BUTTONS = ["添加行", "追加导入", "一键规整整表", "管理", "复制/移动到…", "历史", "重新同步"];

function routeTwoTables(): ReturnType<typeof suite.server> {
  const server = suite.server();
  const record = recordDetail();
  const twin = recordDetail({ id: "t-two", title: "第二张表" });
  const both = [record, twin].map((detail) =>
    wireSummary({ id: detail.id, title: detail.title, rowCount: detail.rows.length }));
  // The mount-time list still has both tables; every later list (after the
  // delete) only has the twin.
  routeTable(server, "nb-1", record, [both[1]]);
  server.once("GET", "/notebooks/nb-1/knowhow", { json: both });
  server.on("GET", "/notebooks/nb-1/knowhow/t-two", { json: wireDetail(twin) });
  return server;
}

function expectToolbarUnlocked(): void {
  for (const name of LOCKED_BUTTONS) expect(screen.getByRole("button", { name }), name).toBeEnabled();
}

function expectToolbarLocked(): void {
  for (const name of LOCKED_BUTTONS) expect(screen.getByRole("button", { name }), name).toBeDisabled();
}

test("after a successful delete the next table opens with every write button usable", async () => {
  const server = routeTwoTables();
  server.on("DELETE", "/notebooks/nb-1/knowhow/t-rec", { status: 204 });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByTitle("删除表"));
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  await user.click(await screen.findByRole("button", { name: "打开表格：第二张表" }));
  await screen.findByRole("heading", { name: "第二张表" });
  await settle();

  expectToolbarUnlocked();
  await user.click(screen.getByTitle("删除表"));
  expect(screen.getByRole("button", { name: "确认删除" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "删除中…" })).not.toBeInTheDocument();
  expect(server.log).toEqual([LIST, DETAIL, DELETE, LIST, TWIN_DETAIL]);
});

test("from the DELETE landing to the list there is no frame with a clickable 确认删除", async () => {
  const server = routeTwoTables();
  const release = server.hold("DELETE", "/notebooks/nb-1/knowhow/t-rec");
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByTitle("删除表"));
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  expect(await screen.findByRole("button", { name: "删除中…" })).toBeDisabled();
  expectToolbarLocked();
  await settle();

  gridRenders.length = 0;
  release({ status: 204 });
  await screen.findByRole("button", { name: "打开表格：第二张表" });
  await settle();

  // The grid may re-render while the request settles, but only still locked; the
  // commit that lifts the lock is the one that takes the grid away.
  expect(gridRenders.every((render) => render.deleting)).toBe(true);
  expect(screen.queryByRole("button", { name: "确认删除" })).not.toBeInTheDocument();
  expect(document.querySelector(".knowhow-confirm")).toBeNull();
  expect(server.log).toEqual([LIST, DETAIL, DELETE, LIST]);
});

test("返回 while the DELETE is in flight and reopening the same table keeps it locked until the request lands", async () => {
  const server = routeTwoTables();
  const release = server.hold("DELETE", "/notebooks/nb-1/knowhow/t-rec");
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByTitle("删除表"));
  await user.click(screen.getByRole("button", { name: "确认删除" }));
  expect(await screen.findByRole("button", { name: "删除中…" })).toBeDisabled();

  await user.click(screen.getByRole("button", { name: "返回" }));
  await user.click(await screen.findByRole("button", { name: "打开表格：排障记录" }));
  await screen.findByRole("heading", { name: "排障记录" });
  await settle();

  // Same panel session, DELETE still pending: nothing can start a second delete.
  expectToolbarLocked();
  await user.click(screen.getByTitle("删除表"));
  const confirm = within(document.querySelector(".knowhow-confirm") as HTMLElement)
    .getByRole("button", { name: "删除中…" });
  expect(confirm).toBeDisabled();
  await user.click(confirm);
  await settle();
  expect(server.log.filter((entry) => entry.method === "DELETE")).toHaveLength(1);

  // The DELETE lands: as before S5 the success path goes back to the list (the
  // table is gone) and now also lifts the lock.
  release({ status: 204 });
  await user.click(await screen.findByRole("button", { name: "打开表格：第二张表" }));
  await screen.findByRole("heading", { name: "第二张表" });
  await settle();
  expectToolbarUnlocked();
  await waitFor(() => expect(server.log).toEqual([LIST, DETAIL, DELETE, DETAIL, LIST, TWIN_DETAIL]));
});
