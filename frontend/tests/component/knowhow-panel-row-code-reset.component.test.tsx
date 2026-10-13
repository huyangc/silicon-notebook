// KnowhowPanel: the row-code owner clears the previous row's code state when the
// drawer closes (debt-fix S4-d review P2-1).
//
// `use-knowhow-row-code.ts` resets rowCodeByColumn / rowCodeLoaded /
// rowCodeError from its effect whenever no row is open. That branch costs one
// extra commit when the drawer closes (a recorded quirk), but it is also what
// keeps the next drawer honest: the row-code fetch for a newly opened row only
// starts in an effect, so the new drawer's first render shows whatever state the
// hook holds at that moment. Without the reset that is the previous row's
// `codeLoaded=true` and code map, painted as the new row's chips until the fetch
// lands. The Profiler commit counts that used to pin the branch were retired in
// S4-d, so this test pins the behaviour instead: it wraps the real drawer and
// records the code props of every render.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import {
  recordDetail,
  renderPanel,
  routeTable,
  settle,
  useKnowhowPanelSuite,
} from "../../test-support/knowhow-panel-harness.tsx";

type DrawerRender = { rowId: string; codeLoaded: boolean; codeColumns: string[] };

const drawerRenders = vi.hoisted(() => [] as DrawerRender[]);

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
