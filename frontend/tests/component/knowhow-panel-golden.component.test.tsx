// KnowhowPanel split baselines (debt-fix S4, C0).
//
// `knowhow-panel.tsx` is split in S4 into a styles module, three modal modules
// and list / grid / row-detail / concept-matrix slices, with zero behaviour
// change. This file holds the evidence each split PR must reproduce exactly.
//
// Two kinds of baseline live here, with different lifetimes:
//
//  * PERMANENT — the request sequence of each scripted scenario (method, URL,
//    body, order), in a plain render and under <StrictMode>. That is what the
//    backend sees, so it is a behavioural contract, not a snapshot of how the
//    code is organised, and it stays after the split. The same goes for the
//    computed-style checks (`getComputedStyle` on the rendered panel), which are
//    the acceptance criterion for moving the stylesheet: they read what the
//    browser would apply, not which module declares it or which `jsx-<hash>`
//    class SWC stamps on the elements (that class disappears when the style block
//    moves out of the panel, harmlessly — nothing depends on it).
//
//  * SPLIT-PERIOD ONLY — React commit counts per step (a <Profiler> around the
//    panel), the window keydown re-registration count, and the SHA-256 of the
//    panel's global stylesheet text. They exist because "zero behaviour change"
//    for a refactor of this size needs a sensitive tripwire: a reordered effect,
//    a state reset moved into an effect, or a dropped/added/unbatched state
//    update moves these numbers before it shows up anywhere a user could see.
//    They are refactor-only goldens in the sense of AGENTS.md "Tests", so they
//    do not stay: the last S4 PR removes them, or converts each into the
//    behaviour it guards (e.g. the E6-before-E7 order is already pinned
//    behaviourally in the lifecycle suite). Until then a change to any of them
//    must be explained in the PR, never just re-recorded.
//
//    The stylesheet hash proves the CSS literal is byte-identical, comments and
//    indentation included, so S4-b must move the template literal untouched. It
//    cannot tell `<style jsx global>` from `<style jsx>` (vitest applies no
//    styled-jsx transform); S4-b's hard gate for that is the `next build`
//    artefact: the `_JSXStyle` id `ef36e44ab474f044` and the SHA-256 of its
//    compiled CSS literal recorded in S4-a must be unchanged.
//
// What these baselines do NOT guard: callback identity in general. A Profiler
// counts commits, so a callback that gains or loses identity is only visible
// where that identity feeds an effect dependency (e.g. the row drawer's inline
// `onClose`, pinned by the keydown count). Elsewhere it is unobservable by
// design — e.g. dropping useCallback from `reloadTableDetail` only re-runs the
// cell editor's ref-sync effect, and wrapping `handleCodeSaved` in useCallback
// changes nothing a test can see. Those rules are enforced by review: every S4
// PR's checklist includes `git diff -M --color-moved=zebra
// --color-moved-ws=allow-indentation-change` showing the moved code as moved,
// plus plan line 184 — the C3/C5 hooks must NOT add useCallback to
// `handleCodeSaved` / `handleCodeDeleted`, and no other callback changes its
// useCallback status or dependency array either.
//
// The numbers are deterministic and platform-independent: every fake response
// resolves on microtasks (test-support/knowhow-panel-harness.tsx), each step ends
// with `settle()`, no scripted flow sets state from a wall-clock timer, and no
// baseline contains a time, random value, float, path, font metric or
// `navigator.*` value. They were checked identical under Node 22 and Node 26.
// There is no re-record mode: every baseline is a literal edited by hand, so CI
// can never rewrite one.
//
// Recorded pre-existing quirks the baselines include on purpose (plan §5, kept
// until after S4 because fixing any of them moves these numbers):
//  * The row drawer re-registers its window Escape listener on every panel
//    render (its effect depends on the inline `onClose` arrow) — pinned by the
//    keydown registration count below.
//  * The row-code effect (E5) resets the row-code state from an effect whenever
//    no row is open (rowCodeByColumn to a fresh `{}`, loaded/error flags), which
//    costs a render of its own. After closing the drawer it is the second of the
//    two commits of the "closeDrawer" step (verified: deleting that reset branch
//    turns 2 into 1). On mount the same writes, including the always-new `{}`,
//    coalesce into the list effect's loading render, so they add no separate
//    commit there; the split must still keep the branch where it is.
//  * Asymmetric reset sets of openTable / backToList / E6 — see the lifecycle
//    suite and the state × reset-path table in knowhow-panel-resets.
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
  sha256Hex,
  useKnowhowPanelSuite,
  wireDetail,
  wirePatchResult,
  type RecordedRequest,
  type RenderedPanel,
} from "../../test-support/knowhow-panel-harness.tsx";

const suite = useKnowhowPanelSuite();

const MODES = [
  { name: "plain render", strict: false },
  { name: "StrictMode", strict: true },
] as const;
type Mode = (typeof MODES)[number]["name"];

type StepCommits = Record<string, number>;

/** Runs one step, lets it settle, and records how many commits it took. */
async function step(panel: RenderedPanel, commits: StepCommits, label: string, action: () => Promise<void>) {
  const before = panel.commits();
  await action();
  await settle();
  commits[label] = panel.commits() - before;
}

const LIST: RecordedRequest = { method: "GET", path: "/notebooks/nb-1/knowhow" };

// ---------------------------------------------------------------------------
// G1/G2 — citation jump on mount
// ---------------------------------------------------------------------------

const JUMP_RECORD: Record<Mode, { requests: RecordedRequest[]; commits: number }> = {
  "plain render": {
    requests: [LIST, { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" }, { method: "GET", path: "/agent/knowhow/rows/r1" }],
    commits: 6,
  },
  StrictMode: {
    // StrictMode replays mount effects: the list effect (E3) fires twice.
    requests: [LIST, LIST, { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" }, { method: "GET", path: "/agent/knowhow/rows/r1" }],
    commits: 6,
  },
};

const JUMP_ANCHOR: Record<Mode, { requests: RecordedRequest[]; commits: number }> = {
  "plain render": {
    requests: [LIST, { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" }],
    commits: 6,
  },
  StrictMode: {
    requests: [LIST, LIST, { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" }],
    commits: 6,
  },
};

for (const mode of MODES) {
  test(`golden [${mode.name}]: citation jump into a record table`, async () => {
    const server = suite.server();
    routeTable(server, "nb-1", recordDetail());
    const panel = renderPanel({ initialTableId: "t-rec", initialRowId: "r1" }, { strict: mode.strict });
    await screen.findByRole("dialog", { name: /^供电异常/ });
    await settle();
    expect(server.log).toEqual(JUMP_RECORD[mode.name].requests);
    expect(panel.commits()).toBe(JUMP_RECORD[mode.name].commits);
  });

  test(`golden [${mode.name}]: citation jump into an anchor table`, async () => {
    const server = suite.server();
    routeTable(server, "nb-1", anchorDetail());
    const panel = renderPanel({ initialTableId: "t-anc", initialRowId: "a2" }, { strict: mode.strict });
    await screen.findByRole("dialog", { name: "概念 时钟" });
    await settle();
    expect(server.log).toEqual(JUMP_ANCHOR[mode.name].requests);
    expect(panel.commits()).toBe(JUMP_ANCHOR[mode.name].commits);
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

const EDIT_FLOW_COMMITS: Record<Mode, StepCommits> = {
  "plain render": {
    render: 2, list: 1, openTable: 3, openRow: 3, openEditor: 1, type: 9, save: 2, closeDrawer: 2, filter: 2, jumpWithFilter: 4,
  },
  StrictMode: {
    render: 2, list: 1, openTable: 3, openRow: 3, openEditor: 2, type: 8, save: 2, closeDrawer: 2, filter: 2, jumpWithFilter: 4,
  },
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
    const commits: StepCommits = {};
    const panel = renderPanel({}, { strict: mode.strict });
    commits.render = panel.commits();

    await step(panel, commits, "list", async () => {
      await screen.findByRole("button", { name: "打开表格：排障记录" });
    });
    await step(panel, commits, "openTable", async () => {
      await user.click(screen.getByRole("button", { name: "打开表格：排障记录" }));
      await screen.findByRole("button", { name: "供电异常" });
    });
    await step(panel, commits, "openRow", async () => {
      await user.click(screen.getByRole("button", { name: "供电异常" }));
      await screen.findByRole("dialog", { name: /^供电异常/ });
    });
    await step(panel, commits, "openEditor", async () => {
      const drawer = screen.getByRole("dialog", { name: /^供电异常/ });
      await user.click(within(drawer).getAllByRole("button", { name: "编辑" })[1]);
      await screen.findByRole("dialog", { name: /› 处理方法/ });
    });
    await step(panel, commits, "type", async () => {
      const editor = screen.getByRole("dialog", { name: /› 处理方法/ });
      const textbox = within(editor).getByRole("textbox");
      await user.clear(textbox);
      await user.type(textbox, "检查电源并复位");
    });
    await step(panel, commits, "save", async () => {
      const editor = screen.getByRole("dialog", { name: /› 处理方法/ });
      await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
      await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 处理方法/ })).not.toBeInTheDocument());
    });
    await step(panel, commits, "closeDrawer", async () => {
      const drawer = screen.getByRole("dialog", { name: /^供电异常/ });
      await user.click(within(drawer).getByRole("button", { name: "关闭" }));
    });
    await step(panel, commits, "filter", async () => {
      const input = screen.getByPlaceholderText("按行标题/全文过滤行…");
      fireEvent.change(input, { target: { value: "供" } });
      fireEvent.change(input, { target: { value: "供电" } });
    });
    // The filter really filters (rows are permanent behaviour, not a count).
    expect(screen.getByRole("button", { name: "供电异常" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查电源并复位" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "时钟漂移" })).not.toBeInTheDocument();
    // Switch tables with the filter still set: openTable clears the query in the
    // same batch as the selection (a reset moved into an effect costs a commit).
    await step(panel, commits, "jumpWithFilter", async () => {
      panel.update({ ...BASE_PROPS, initialTableId: "t-two" });
      await screen.findByRole("heading", { name: "第二张表" });
      await screen.findByRole("button", { name: "时钟漂移" });
    });
    expect(screen.getByPlaceholderText("按行标题/全文过滤行…")).toHaveValue("");
    expect(screen.getByRole("button", { name: "供电异常" })).toBeInTheDocument();

    expect(server.log).toEqual(EDIT_FLOW_REQUESTS[mode.name]);
    expect(commits).toEqual(EDIT_FLOW_COMMITS[mode.name]);
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

const REFORMAT_COMMITS: Record<Mode, StepCommits> = {
  "plain render": { open: 2, run: 3, save: 2, close: 1 },
  StrictMode: { open: 2, run: 2, save: 2, close: 1 },
};

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
    const panel = renderPanel({ initialTableId: "t-anc" }, { strict: mode.strict });
    await screen.findByRole("button", { name: "一键规整整表" });
    await settle();
    // The mount + jump prefix is pinned by G2's shape; this golden starts at the
    // batch modal so its sequence is the same in both render modes.
    server.clearLog();
    const commits: StepCommits = {};

    await step(panel, commits, "open", async () => {
      await user.click(screen.getByRole("button", { name: "一键规整整表" }));
      await screen.findByRole("dialog", { name: "一键规整整表 · 概念表" });
    });
    const dialog = screen.getByRole("dialog", { name: "一键规整整表 · 概念表" });
    await step(panel, commits, "run", async () => {
      await user.click(within(dialog).getByRole("button", { name: "开始规整" }));
      await waitFor(() => expect(within(dialog).getByRole("button", { name: /确认保存/ })).toBeEnabled());
    });
    await step(panel, commits, "save", async () => {
      await user.click(within(dialog).getByRole("button", { name: /确认保存/ }));
      await within(dialog).findByText(/已完成保存。/);
    });
    await step(panel, commits, "close", async () => {
      await user.click(within(dialog).getAllByRole("button", { name: "关闭" }).at(-1)!);
      await waitFor(() => expect(screen.queryByRole("dialog", { name: /一键规整整表/ })).not.toBeInTheDocument());
    });

    expect(server.log).toEqual(REFORMAT_REQUESTS);
    expect(commits).toEqual(REFORMAT_COMMITS[mode.name]);
  });
}

// ---------------------------------------------------------------------------
// Quirk: the row drawer re-registers its Escape listener on every panel render
// ---------------------------------------------------------------------------

test("split baseline: each panel render with the drawer open re-registers one window keydown listener", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });
  await screen.findByRole("dialog", { name: /^供电异常/ });
  await settle();

  const added = vi.spyOn(window, "addEventListener");
  const removed = vi.spyOn(window, "removeEventListener");
  const input = screen.getByPlaceholderText("按行标题/全文过滤行…");
  fireEvent.change(input, { target: { value: "供" } });
  fireEvent.change(input, { target: { value: "供电" } });
  fireEvent.change(input, { target: { value: "" } });
  await settle();
  const keydown = (spy: typeof added) => spy.mock.calls.filter(([type]) => type === "keydown").length;
  expect(keydown(added)).toBe(3);
  expect(keydown(removed)).toBe(3);
  // The drawer is still the one listening: Escape closes it.
  fireEvent.keyDown(window, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /^供电异常/ })).not.toBeInTheDocument());
});

// ---------------------------------------------------------------------------
// 15. Stylesheet: computed styles (permanent) and text fingerprint (split-period)
// ---------------------------------------------------------------------------

// The fingerprint is taken over the stylesheet literal itself, not over CSS that a
// runtime injected or re-serialised: vitest applies no styled-jsx transform, so
// React renders the template literal's cooked string as the <style> element's
// only text node and `textContent` returns that string verbatim. The literal has
// no `${}` interpolation, and template-literal cooking normalises CR/CRLF to LF,
// so a checkout's line endings cannot move the hash either.
const PANEL_STYLESHEET_SHA256 = "413ee967b75ac12234f17ce799eea7f528fc5108285455dd0cd4579489592524";

test("split baseline: the panel's global stylesheet is its last child and its text is unchanged", async () => {
  suite.server().on("GET", "/notebooks/nb-1/knowhow", { json: [] });
  const panel = renderPanel();
  await screen.findByText("还没有 knowhow 表");
  const style = panelStyleElement(panel.container);
  const section = panel.container.querySelector("section.knowhow-view")!;
  expect(section.lastElementChild).toBe(style);
  expect(sha256Hex(style.textContent ?? "")).toBe(PANEL_STYLESHEET_SHA256);
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
