// KnowhowPanel request-sequence goldens and computed styles (debt-fix S4).
//
// `knowhow-panel.tsx` was split in S4 into a styles module, three modal modules,
// list / grid / row-drawer views and three owner hooks, with zero behaviour
// change. This file holds the behavioural contracts that outlive the split:
//
//  * The request sequence of each scripted scenario (method, URL, body, order),
//    in a plain render and under <StrictMode>. That is what the backend sees, so
//    it is a behavioural contract, not a snapshot of how the code is organised.
//  * Computed styles (`getComputedStyle` on the rendered panel): layering, panel
//    shell and grid layout, i.e. what the browser applies, not which module
//    declares it or which `jsx-<hash>` class SWC stamps on the elements.
//  * The panel's global stylesheet renders once, as the section's last child.
//
// During the split this file also pinned three refactor-only tripwires: React
// commit counts per step (a <Profiler> around the panel), the number of window
// keydown re-registrations while the row drawer is open, and the SHA-256 of the
// stylesheet text. AGENTS.md "Tests" does not keep refactor-only goldens, so the
// last S4 PR (S4-d) retired them after showing them green on the final split.
// What they guarded and still matters is pinned elsewhere:
//  * effect order (notebook reset before the citation jump, the concept-matrix
//    hook last) and callback identity: knowhow-panel-boundary-guard;
//  * how the owner hooks are used: owner-view-no-reflatten-guard;
//  * the E6-before-E7 order and the other quirks, behaviourally: the lifecycle,
//    resets, rows, editing and table-actions suites.
//
// The numbers are deterministic and platform-independent: every fake response
// resolves on microtasks (test-support/knowhow-panel-harness.tsx), each step ends
// with `settle()`, no scripted flow sets state from a wall-clock timer, and no
// baseline contains a time, random value, float, path, font metric or
// `navigator.*` value. They were checked identical under Node 22 and Node 26.
// There is no re-record mode: every baseline is a literal edited by hand, so CI
// can never rewrite one.
//
// Recorded pre-existing quirks (plan §5, fixed only after S4):
//  * The row drawer re-registered its window Escape listener on every panel
//    render (its effect depended on an inline `onClose` arrow). Fixed in S6: the
//    test below checks re-renders no longer re-register it and Escape still
//    closes the drawer.
//  * The row-code hook resets its state from an effect whenever no row is open,
//    which costs one extra commit when the drawer closes. The reset is also what
//    clears the previous row's code state, so a drawer reopened on another row
//    never renders the old `codeLoaded=true` and code map; that behaviour is
//    pinned by knowhow-panel-row-code-reset.component.test.tsx.
//  * Asymmetric reset sets of openTable / backToList / E6 — fixed in S6; see the
//    lifecycle suite and the state × reset-path table in knowhow-panel-resets.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import {
  BASE_PROPS,
  anchorDetail,
  panelStyleElement,
  recordDetail,
  renderPanel,
  routeTable,
  settle,
  useKnowhowPanelSuite,
  wireDetail,
  wirePatchResult,
  type RecordedRequest,
} from "../../test-support/knowhow-panel-harness.tsx";

const suite = useKnowhowPanelSuite();

const MODES = [
  { name: "plain render", strict: false },
  { name: "StrictMode", strict: true },
] as const;
type Mode = (typeof MODES)[number]["name"];

/** Runs one scripted step and lets it settle. */
async function step(action: () => Promise<void>) {
  await action();
  await settle();
}

const LIST: RecordedRequest = { method: "GET", path: "/notebooks/nb-1/knowhow" };

// ---------------------------------------------------------------------------
// G1/G2 — citation jump on mount
// ---------------------------------------------------------------------------

const JUMP_RECORD: Record<Mode, { requests: RecordedRequest[] }> = {
  "plain render": {
    requests: [LIST, { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" }, { method: "GET", path: "/agent/knowhow/rows/r1" }],
  },
  StrictMode: {
    // StrictMode replays mount effects: the list effect (E3) fires twice.
    requests: [LIST, LIST, { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" }, { method: "GET", path: "/agent/knowhow/rows/r1" }],
  },
};

const JUMP_ANCHOR: Record<Mode, { requests: RecordedRequest[] }> = {
  "plain render": {
    requests: [LIST, { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" }],
  },
  StrictMode: {
    requests: [LIST, LIST, { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" }],
  },
};

for (const mode of MODES) {
  test(`golden [${mode.name}]: citation jump into a record table`, async () => {
    const server = suite.server();
    routeTable(server, "nb-1", recordDetail());
    renderPanel({ initialTableId: "t-rec", initialRowId: "r1" }, { strict: mode.strict });
    await screen.findByRole("dialog", { name: /^供电异常/ });
    await settle();
    expect(server.log).toEqual(JUMP_RECORD[mode.name].requests);
  });

  test(`golden [${mode.name}]: citation jump into an anchor table`, async () => {
    const server = suite.server();
    routeTable(server, "nb-1", anchorDetail());
    renderPanel({ initialTableId: "t-anc", initialRowId: "a2" }, { strict: mode.strict });
    await screen.findByRole("dialog", { name: "概念 时钟" });
    await settle();
    expect(server.log).toEqual(JUMP_ANCHOR[mode.name].requests);
  });
}

// ---------------------------------------------------------------------------
// G3 — list → table → row drawer → edit → save → close → filter
// ---------------------------------------------------------------------------

const EDIT_FLOW_REQUESTS: Record<Mode, RecordedRequest[]> = {
  "plain render": [
    LIST,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "PATCH", path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix", body: { content_md: "检查电源并复位" } },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-two" },
  ],
  StrictMode: [
    LIST,
    LIST,
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "PATCH", path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix", body: { content_md: "检查电源并复位" } },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-two" },
  ],
};

for (const mode of MODES) {
  test(`golden [${mode.name}]: open a row, edit a cell from the drawer, save, close, filter, switch table`, async () => {
    const server = suite.server();
    routeTable(server, "nb-1", recordDetail());
    server.on("GET", "/notebooks/nb-1/knowhow/t-two", { json: wireDetail(recordDetail({ id: "t-two", title: "第二张表" })) });
    server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix", {
      json: wirePatchResult("r1", "c-fix", "检查电源并复位"),
    });
    const user = userEvent.setup();
    const panel = renderPanel({}, { strict: mode.strict });

    await step(async () => {
      await screen.findByRole("button", { name: "打开表格：排障记录" });
    });
    await step(async () => {
      await user.click(screen.getByRole("button", { name: "打开表格：排障记录" }));
      await screen.findByRole("button", { name: "供电异常" });
    });
    await step(async () => {
      await user.click(screen.getByRole("button", { name: "供电异常" }));
      await screen.findByRole("dialog", { name: /^供电异常/ });
    });
    await step(async () => {
      const drawer = screen.getByRole("dialog", { name: /^供电异常/ });
      await user.click(within(drawer).getAllByRole("button", { name: "编辑" })[1]);
      await screen.findByRole("dialog", { name: /› 处理方法/ });
    });
    await step(async () => {
      const editor = screen.getByRole("dialog", { name: /› 处理方法/ });
      const textbox = within(editor).getByRole("textbox");
      await user.clear(textbox);
      await user.type(textbox, "检查电源并复位");
    });
    await step(async () => {
      const editor = screen.getByRole("dialog", { name: /› 处理方法/ });
      await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
      await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 处理方法/ })).not.toBeInTheDocument());
    });
    await step(async () => {
      const drawer = screen.getByRole("dialog", { name: /^供电异常/ });
      await user.click(within(drawer).getByRole("button", { name: "关闭" }));
    });
    await step(async () => {
      const input = screen.getByPlaceholderText("按行标题/全文过滤行…");
      fireEvent.change(input, { target: { value: "供" } });
      fireEvent.change(input, { target: { value: "供电" } });
    });
    // The filter really filters (rows are permanent behaviour, not a count).
    expect(screen.getByRole("button", { name: "供电异常" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查电源并复位" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "时钟漂移" })).not.toBeInTheDocument();
    // Switch tables with the filter still set: the new table opens unfiltered.
    await step(async () => {
      panel.update({ ...BASE_PROPS, initialTableId: "t-two" });
      await screen.findByRole("heading", { name: "第二张表" });
      await screen.findByRole("button", { name: "时钟漂移" });
    });
    expect(screen.getByPlaceholderText("按行标题/全文过滤行…")).toHaveValue("");
    expect(screen.getByRole("button", { name: "供电异常" })).toBeInTheDocument();

    expect(server.log).toEqual(EDIT_FLOW_REQUESTS[mode.name]);
  });
}

// ---------------------------------------------------------------------------
// G4 — batch reformat the whole table: run → review → save → close
// ---------------------------------------------------------------------------

const REFORMAT_REQUESTS: RecordedRequest[] = [
  { method: "GET", path: "/model-services/status" },
  { method: "POST", path: "/notebooks/nb-1/knowhow/t-anc/rows/a1/cells/c-shared/reformat/stream" },
  { method: "POST", path: "/notebooks/nb-1/knowhow/t-anc/rows/a1/cells/c-own/reformat/stream" },
  { method: "POST", path: "/notebooks/nb-1/knowhow/t-anc/rows/a3/cells/c-shared/reformat/stream" },
  { method: "POST", path: "/notebooks/nb-1/knowhow/t-anc/rows/a3/cells/c-own/reformat/stream" },
  { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
  {
    method: "PATCH",
    path: "/notebooks/nb-1/knowhow/t-anc/cells",
    body: {
      column_id: "c-shared",
      row_ids: ["a1", "a2"],
      content_md: "检查时钟源。",
      expected_before: ["检查时钟源", "检查时钟源"],
      anchor_column_id: "c-anchor",
      expected_anchor: ["时钟", "时钟"],
      origin: "llm_reformat",
    },
  },
  {
    method: "PATCH",
    path: "/notebooks/nb-1/knowhow/t-anc/rows/a1/cells/c-own",
    body: { content_md: "主板。", expected_before: "主板", origin: "llm_reformat" },
  },
  {
    method: "PATCH",
    path: "/notebooks/nb-1/knowhow/t-anc/cells",
    body: {
      column_id: "c-shared",
      row_ids: ["a3"],
      content_md: "量电压。",
      expected_before: ["量电压"],
      anchor_column_id: "c-anchor",
      expected_anchor: ["电源"],
      origin: "llm_reformat",
    },
  },
  {
    method: "PATCH",
    path: "/notebooks/nb-1/knowhow/t-anc/cells",
    body: {
      column_id: "c-own",
      row_ids: ["a3"],
      content_md: "单板。",
      expected_before: ["单板"],
      anchor_column_id: "c-anchor",
      expected_anchor: ["电源"],
      origin: "llm_reformat",
    },
  },
];

for (const mode of MODES) {
  test(`golden [${mode.name}]: batch reformat the whole table`, async () => {
    const server = suite.server();
    const detail = anchorDetail();
    routeTable(server, "nb-1", detail);
    server.on("GET", "/model-services/status", { json: { services: [] } });
    const reformatPath = (rowId: string, columnId: string) =>
      `/notebooks/nb-1/knowhow/t-anc/rows/${rowId}/cells/${columnId}/reformat/stream`;
    for (const row of detail.rows) {
      for (const columnId of ["c-shared", "c-own"]) {
        const source = row.cells[columnId];
        if (!source) continue;
        server.on("POST", reformatPath(row.id, columnId), {
          stream: { candidate_md: `${source}。`, source: "llm", changed: true, source_md: source },
        });
      }
    }
    server.on("PATCH", "/notebooks/nb-1/knowhow/t-anc/cells", (request) => {
      const body = request.body as { column_id: string; row_ids: string[]; content_md: string };
      return { json: body.row_ids.map((rowId) => wirePatchResult(rowId, body.column_id, body.content_md)) };
    });
    server.on("PATCH", "/notebooks/nb-1/knowhow/t-anc/rows/a1/cells/c-own", { json: wirePatchResult("a1", "c-own", "主板。") });
    const user = userEvent.setup();
    renderPanel({ initialTableId: "t-anc" }, { strict: mode.strict });
    await screen.findByRole("button", { name: "一键规整整表" });
    await settle();
    // The mount + jump prefix is pinned by G2's shape; this golden starts at the
    // batch modal so its sequence is the same in both render modes.
    server.clearLog();

    await step(async () => {
      await user.click(screen.getByRole("button", { name: "一键规整整表" }));
      await screen.findByRole("dialog", { name: "一键规整整表 · 概念表" });
    });
    const dialog = screen.getByRole("dialog", { name: "一键规整整表 · 概念表" });
    await step(async () => {
      await user.click(within(dialog).getByRole("button", { name: "开始规整" }));
      await waitFor(() => expect(within(dialog).getByRole("button", { name: /确认保存/ })).toBeEnabled());
    });
    await step(async () => {
      await user.click(within(dialog).getByRole("button", { name: /确认保存/ }));
      await within(dialog).findByText(/已完成保存。/);
    });
    await step(async () => {
      await user.click(within(dialog).getAllByRole("button", { name: "关闭" }).at(-1)!);
      await waitFor(() => expect(screen.queryByRole("dialog", { name: /一键规整整表/ })).not.toBeInTheDocument());
    });

    expect(server.log).toEqual(REFORMAT_REQUESTS);
  });
}

// ---------------------------------------------------------------------------
// The row drawer's Escape listener is registered once, not on every render
// ---------------------------------------------------------------------------

test("panel re-renders with the drawer open do not re-register its Escape listener; Escape still closes it", async () => {
  // Until S6 the panel passed the drawer an inline onClose arrow, and the drawer's
  // listener effect depends on it, so every panel render removed and re-added the
  // window keydown listener (three filter edits: three of each). The callback is
  // now stable; the listener only moves when an overlay opens or closes over the
  // drawer (cellModalOpen), which the Escape layering tests in the editing suite
  // cover.
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });
  await screen.findByRole("dialog", { name: /^供电异常/ });
  await settle();

  const keydown = (calls: unknown[][]) => calls.filter(([type]) => type === "keydown").length;
  const added = vi.spyOn(window, "addEventListener");
  const removed = vi.spyOn(window, "removeEventListener");
  const input = screen.getByPlaceholderText("按行标题/全文过滤行…");
  fireEvent.change(input, { target: { value: "供" } });
  fireEvent.change(input, { target: { value: "供电" } });
  fireEvent.change(input, { target: { value: "" } });
  await settle();
  expect(keydown(added.mock.calls)).toBe(0);
  expect(keydown(removed.mock.calls)).toBe(0);
  fireEvent.keyDown(window, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /^供电异常/ })).not.toBeInTheDocument());
});

// ---------------------------------------------------------------------------
// 15. Stylesheet: mount position and computed styles
// ---------------------------------------------------------------------------

// vitest applies no styled-jsx transform, so the panel's global stylesheet
// renders as a plain <style> element; it must be the section's last child (the
// position SWC's _JSXStyle injection order follows in production).
test("the panel's global stylesheet renders once, as the section's last child", async () => {
  suite.server().on("GET", "/notebooks/nb-1/knowhow", { json: [] });
  const panel = renderPanel();
  await screen.findByText("还没有 knowhow 表");
  const style = panelStyleElement(panel.container);
  const section = panel.container.querySelector("section.knowhow-view")!;
  expect(section.lastElementChild).toBe(style);
  expect(style.textContent).toContain(".knowhow-view");
});

test("computed styles: panel shell, layers and grid read the panel stylesheet", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const user = userEvent.setup();
  const panel = renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });
  await screen.findByRole("dialog", { name: /^供电异常/ });
  await user.click(within(screen.getByRole("dialog", { name: /^供电异常/ })).getAllByRole("button", { name: "编辑" })[0]);
  await screen.findByRole("dialog", { name: /› 问题/ });

  const css = (selector: string) => {
    const element = panel.container.querySelector(selector);
    expect(element, selector).not.toBeNull();
    return getComputedStyle(element!);
  };
  const shell = css("section.knowhow-view");
  expect([shell.position, shell.zIndex, shell.display, shell.flexDirection]).toEqual(["fixed", "50", "flex", "column"]);
  expect(css(".knowhow-view-header").display).toBe("flex");
  expect(css(".knowhow-grid-table").tableLayout).toBe("fixed");
  // Layering: panel 50 < drawer backdrop 55 < drawer 56 < kh-modal overlay 65.
  expect(css(".knowhow-drawer-backdrop").zIndex).toBe("55");
  const drawer = css("section.knowhow-drawer");
  expect([drawer.position, drawer.zIndex, drawer.display]).toEqual(["fixed", "56", "flex"]);
  const overlay = css(".kh-modal-overlay");
  expect([overlay.position, overlay.zIndex, overlay.display]).toEqual(["fixed", "65", "grid"]);
  const card = css(".kh-modal-card");
  expect([card.display, card.flexDirection, card.overflow]).toEqual(["flex", "column", "hidden"]);
});
