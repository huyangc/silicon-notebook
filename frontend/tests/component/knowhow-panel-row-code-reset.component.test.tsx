// KnowhowPanel: the row-code owner clears the previous row's code state whenever
// the open row changes (debt-fix S4-d review P2-1, moved to render time in S6).
//
// The row-code fetch for a newly opened row only starts in an effect, so the new
// drawer's first render shows whatever state `use-knowhow-row-code.ts` holds at
// that moment. Without a reset that is the previous row's `codeLoaded=true` and
// code map, painted as the new row's chips until the fetch lands. The Profiler
// commit counts that used to pin the reset were retired in S4-d, so the first
// test pins the behaviour instead: it wraps the real drawer and records the code
// props of every render.
//
// Until S6 the reset ran in an effect whenever no row was open, which cost one
// extra commit after the drawer closed. It now runs at render time when the open
// row changes, in the same commit as the close; the second test pins that by
// counting the grid's renders around the close (the drawer is gone by then).
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
} from "../../test-support/knowhow-panel-harness.tsx";

type DrawerRender = { rowId: string; codeLoaded: boolean; codeColumns: string[] };

const drawerRenders = vi.hoisted(() => [] as DrawerRender[]);
const gridRenders = vi.hoisted(() => ({ count: 0 }));

vi.mock("../../app/knowhow-row-drawer.tsx", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-row-drawer.tsx")>();
  const RealDrawer = actual.KnowhowRowDrawer;
  return {
    ...actual,
    KnowhowRowDrawer: (props: Parameters<typeof RealDrawer>[0]) => {
      drawerRenders.push({
        rowId: props.row.id,
        codeLoaded: props.codeLoaded,
        codeColumns: Object.keys(props.codeByColumn).sort(),
      });
      return <RealDrawer {...props} />;
    },
  };
});

vi.mock("../../app/knowhow-table-grid.tsx", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-table-grid.tsx")>();
  const RealGrid = actual.KnowhowTableGrid;
  return {
    ...actual,
    KnowhowTableGrid: (props: Parameters<typeof RealGrid>[0]) => {
      gridRenders.count += 1;
      return <RealGrid {...props} />;
    },
  };
});

const suite = useKnowhowPanelSuite();

const R1_TITLE = "供电异常 · 检查电源 · 万用表";

test("reopening the drawer on another row never renders the previous row's code state", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/agent/knowhow/rows/r1", {
    json: {
      code: [{
        column_id: "c-fix", language: "python", code_text: "print(1)", status: "implemented",
        updated_at: "2026-10-01T00:00:00Z", updated_by: "alice",
      }],
    },
  });
  const releaseR2 = server.hold("GET", "/agent/knowhow/rows/r2");
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const first = await screen.findByRole("dialog", { name: R1_TITLE });
  expect(await within(first).findByRole("button", { name: /已实现/ })).toBeInTheDocument();
  await user.click(within(first).getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: R1_TITLE })).not.toBeInTheDocument());
  await settle();

  drawerRenders.length = 0;
  await user.click(screen.getByRole("button", { name: "时钟漂移" }));
  const second = await screen.findByRole("dialog", { name: "时钟漂移" });
  await settle();

  // While r2's code status is still in flight, nothing of r1's state shows.
  expect(drawerRenders.length).toBeGreaterThan(0);
  expect(drawerRenders.every((render) => render.rowId === "r2")).toBe(true);
  expect(drawerRenders[0]).toEqual({ rowId: "r2", codeLoaded: false, codeColumns: [] });
  expect(drawerRenders.some((render) => render.codeLoaded || render.codeColumns.length > 0)).toBe(false);
  expect(within(second).queryByRole("button", { name: /已实现/ })).not.toBeInTheDocument();
  expect(within(second).queryByRole("button", { name: /添加代码/ })).not.toBeInTheDocument();

  releaseR2({ json: { code: [] } });
  expect(await within(second).findAllByRole("button", { name: /添加代码/ })).toHaveLength(3);
  expect(drawerRenders.at(-1)).toEqual({ rowId: "r2", codeLoaded: true, codeColumns: [] });
  expect(within(second).queryByRole("button", { name: /已实现/ })).not.toBeInTheDocument();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "GET", path: "/agent/knowhow/rows/r2" },
  ]);
});

test("closing the drawer re-renders the panel once: the code-state reset rides the close commit", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });
  const drawer = await screen.findByRole("dialog", { name: R1_TITLE });
  expect(await within(drawer).findAllByRole("button", { name: /添加代码/ })).toHaveLength(3);
  await settle();

  gridRenders.count = 0;
  await user.click(within(drawer).getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: R1_TITLE })).not.toBeInTheDocument());
  await settle();
  // One render for the close itself. Before S6 the effect-time reset added a
  // second render (and commit) of the whole panel right after it.
  expect(gridRenders.count).toBe(1);
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
  ]);
});

test("a citation re-targeted from the open row to another row never renders the previous row's code state", async () => {
  // A same-table re-targeted jump switches openRowId straight from r1 to r2 in
  // one commit (no closed drawer in between). The render-time reset still clears
  // r1's code state before r2's drawer is first committed.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/agent/knowhow/rows/r1", {
    json: {
      code: [{
        column_id: "c-fix", language: "python", code_text: "print(1)", status: "implemented",
        updated_at: "2026-10-01T00:00:00Z", updated_by: "alice",
      }],
    },
  });
  const releaseR2 = server.hold("GET", "/agent/knowhow/rows/r2");
  const panel = renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });
  const first = await screen.findByRole("dialog", { name: R1_TITLE });
  expect(await within(first).findByRole("button", { name: /已实现/ })).toBeInTheDocument();
  await settle();

  drawerRenders.length = 0;
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec", initialRowId: "r2" });
  const second = await screen.findByRole("dialog", { name: "时钟漂移" });
  await settle();
  const r2Renders = drawerRenders.filter((render) => render.rowId === "r2");
  expect(r2Renders.length).toBeGreaterThan(0);
  expect(r2Renders.some((render) => render.codeLoaded || render.codeColumns.length > 0)).toBe(false);
  expect(within(second).queryByRole("button", { name: /已实现/ })).not.toBeInTheDocument();

  releaseR2({ json: { code: [] } });
  expect(await within(second).findAllByRole("button", { name: /添加代码/ })).toHaveLength(3);
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "GET", path: "/agent/knowhow/rows/r2" },
  ]);
});
