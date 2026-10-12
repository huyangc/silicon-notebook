// KnowhowPanel characterization (debt-fix S4, C0): read-only view, the cell
// preview/edit/save state machine, row code status, errors shown next to the
// control that caused them, Escape layering and modal stacking order.
//
// Request sequences asserted here are the backend contract and stay after the
// split. See knowhow-panel-lifecycle.component.test.tsx for the list of recorded
// pre-existing quirks the split must carry over unchanged.
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import {
  anchorDetail,
  recordDetail,
  renderPanel,
  routeTable,
  settle,
  useKnowhowPanelSuite,
  wirePatchResult,
} from "../../test-support/knowhow-panel-harness.tsx";

const suite = useKnowhowPanelSuite();

const R1_TITLE = "供电异常 · 检查电源 · 万用表";

const LIST = { method: "GET", path: "/notebooks/nb-1/knowhow" };
const REC = { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" };
const ANC = { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" };
const rowCode = (rowId: string) => ({ method: "GET", path: `/agent/knowhow/rows/${rowId}` });

function toolbarButtonLabels(): string[] {
  const toolbar = screen.getByRole("button", { name: "返回" }).closest(".knowhow-grid-toolbar") as HTMLElement;
  return within(toolbar).getAllByRole("button").map((button) => button.textContent?.trim() ?? "");
}

function siblingCell(dialog: HTMLElement, columnName: string): HTMLElement {
  const match = within(dialog)
    .getAllByRole("button")
    .find((element) => element.querySelector("strong")?.textContent === columnName);
  expect(match).toBeDefined();
  return match!;
}

const COMPLETION = {
  retrieval_mode: "reasoning",
  retrieval_scope: "active_and_mounted",
  retrieval_status: "succeeded",
  reasoning_trace: [],
  evidence: [],
};

// ---------------------------------------------------------------------------
// 3. Read-only members (canEdit=false)
// ---------------------------------------------------------------------------

test("read-only grid: toolbar keeps only back, copy/move and history; empty cells are inert; no retry", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  renderPanel({ canEdit: false, initialTableId: "t-rec" });

  expect(await screen.findByRole("heading", { name: "排障记录" })).toBeInTheDocument();
  expect(screen.getByText("结构化经验表：按行浏览与核对（只读共享，不可修改）。")).toBeInTheDocument();
  expect(toolbarButtonLabels()).toEqual(["返回", "复制/移动到…", "历史"]);
  expect(screen.getAllByText("—")).toHaveLength(2);
  expect(screen.queryByRole("button", { name: "+" })).not.toBeInTheDocument();
  expect(screen.getByText("同步失败·可重试")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /概念/ })).not.toBeInTheDocument();
  // Column names are plain text for readers (no double-click rename affordance).
  expect(screen.getByText("处理方法")).not.toHaveAttribute("title", "处理方法（双击改名）");
  await settle();
  expect(server.log).toEqual([LIST, REC]);
});

test("read-only row drawer and cell preview offer no write entry", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const user = userEvent.setup();
  renderPanel({ canEdit: false, initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: R1_TITLE });
  for (const name of ["编辑", "优化整行", "智能补全空列", "一键规整整行"]) {
    expect(within(drawer).queryByRole("button", { name: new RegExp(name) })).not.toBeInTheDocument();
  }
  await user.click(within(drawer).getByRole("button", { name: "关闭" }));

  await user.click(screen.getByRole("button", { name: "检查电源" }));
  const preview = await screen.findByRole("dialog", { name: `${R1_TITLE} › 处理方法` });
  expect(within(preview).queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
  expect(within(preview).getByRole("button", { name: "历史" })).toBeInTheDocument();
  // Switching to a sibling cell keeps a reader in preview.
  await user.click(siblingCell(preview, "工具"));
  const next = await screen.findByRole("dialog", { name: `${R1_TITLE} › 工具` });
  expect(within(next).queryByRole("textbox")).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
  ]);
});

test("read-only concept matrix has no delete, add-branch or completion entry", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", anchorDetail());
  const user = userEvent.setup();
  renderPanel({ canEdit: false, initialTableId: "t-anc" });

  expect(await screen.findByRole("heading", { name: "概念表" })).toBeInTheDocument();
  expect(screen.getAllByText("—")).toHaveLength(1);
  expect(screen.queryByRole("button", { name: /概念$/ })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "时钟" }));
  const matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  expect(within(matrix).queryByTitle("删除整个概念")).not.toBeInTheDocument();
  expect(within(matrix).queryByTitle("删除该分支")).not.toBeInTheDocument();
  expect(within(matrix).queryByRole("button", { name: /分支$/ })).not.toBeInTheDocument();
  expect(within(matrix).queryByRole("button", { name: /智能补全/ })).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([LIST, ANC]);
});

// ---------------------------------------------------------------------------
// 4. Edit and save
// ---------------------------------------------------------------------------

test("preview → edit → save patches one cell and updates the grid summary without a reload", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix", {
    json: wirePatchResult("r1", "c-fix", "检查电源并复位"),
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByRole("button", { name: "检查电源" }));
  const preview = await screen.findByRole("dialog", { name: `${R1_TITLE} › 处理方法` });
  expect(within(preview).queryByRole("textbox")).not.toBeInTheDocument();
  await user.click(within(preview).getByRole("button", { name: "编辑" }));
  const editor = await screen.findByRole("dialog", { name: `${R1_TITLE} › 处理方法` });
  const textbox = within(editor).getByRole("textbox");
  await user.clear(textbox);
  await user.type(textbox, "检查电源并复位");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));

  expect(await screen.findByRole("button", { name: "检查电源并复位" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: /› 处理方法/ })).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    {
      method: "PATCH",
      path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix",
      body: { content_md: "检查电源并复位" },
    },
  ]);
});

test("saving a merged shared cell fans out to the whole concept group in one batch request", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", anchorDetail());
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-anc/cells", {
    json: [wirePatchResult("a1", "c-shared", "换晶振"), wirePatchResult("a2", "c-shared", "换晶振")],
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "检查时钟源" }));
  const preview = await screen.findByRole("dialog", { name: "时钟 › 共同方法" });
  await user.click(within(preview).getByRole("button", { name: "编辑" }));
  const editor = await screen.findByRole("dialog", { name: "时钟 › 共同方法" });
  expect(within(editor).getByText(/2 个分支/)).toBeInTheDocument();
  const textbox = within(editor).getByRole("textbox");
  await user.clear(textbox);
  await user.type(textbox, "换晶振");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));

  expect(await screen.findByRole("button", { name: "换晶振" })).toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-anc" },
    {
      method: "PATCH",
      path: "/notebooks/nb-1/knowhow/t-anc/cells",
      body: { column_id: "c-shared", row_ids: ["a1", "a2"], content_md: "换晶振" },
    },
  ]);
});

test("a failed save keeps the editor open with the error inside it", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r2/cells/c-tool", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const empty = await screen.findAllByRole("button", { name: "+" });
  await user.click(empty[1]);
  const editor = await screen.findByRole("dialog", { name: "时钟漂移 › 工具" });
  await user.type(within(editor).getByRole("textbox"), "示波器");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));

  const error = await waitFor(() => {
    const node = editor.querySelector(".kh-inline-error");
    expect(node).not.toBeNull();
    return node as HTMLElement;
  });
  expect(error.textContent?.trim()).not.toBe("");
  expect(screen.getByRole("dialog", { name: "时钟漂移 › 工具" })).toBe(editor);
  expect(within(editor).getByRole("textbox")).toHaveValue("示波器");
  expect(within(editor).getByRole("button", { name: /^保存$/ })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "示波器" })).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([
    LIST,
    REC,
    { method: "PATCH", path: "/notebooks/nb-1/knowhow/t-rec/rows/r2/cells/c-tool", body: { content_md: "示波器" } },
  ]);
});

test("switching to a sibling cell keeps the current mode (preview stays preview, edit stays edit)", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByRole("button", { name: "检查电源" }));
  const preview = await screen.findByRole("dialog", { name: `${R1_TITLE} › 处理方法` });
  await user.click(siblingCell(preview, "工具"));
  const toolPreview = await screen.findByRole("dialog", { name: `${R1_TITLE} › 工具` });
  expect(within(toolPreview).queryByRole("textbox")).not.toBeInTheDocument();
  expect(within(toolPreview).getByRole("button", { name: "编辑" })).toBeInTheDocument();

  await user.click(within(toolPreview).getByRole("button", { name: "编辑" }));
  const toolEditor = await screen.findByRole("dialog", { name: `${R1_TITLE} › 工具` });
  expect(within(toolEditor).getByRole("textbox")).toHaveValue("万用表");
  await user.click(siblingCell(toolEditor, "问题"));
  const titleEditor = await screen.findByRole("dialog", { name: `${R1_TITLE} › 问题` });
  expect(within(titleEditor).getByRole("textbox")).toHaveValue("供电异常");
  await settle();
  expect(server.log).toEqual([LIST, REC]);
});

// ---------------------------------------------------------------------------
// 9. Errors are shown next to the control that caused them
// ---------------------------------------------------------------------------

test("a failed completion accept shows role=alert inside that suggestion and re-enables its button", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/rows/r2/complete/stream", {
    stream: {
      ...COMPLETION,
      suggestions: [{
        column_id: "c-fix", suggestion_md: "校准晶振", confidence: "high",
        based_on_row_ids: [], evidence_keys: [], basis: "", abstain_reason: "",
      }],
    },
  });
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r2/cells/c-fix", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r2" });

  const drawer = await screen.findByRole("dialog", { name: "时钟漂移" });
  await user.click(within(drawer).getByRole("button", { name: "智能补全空列" }));
  const dialog = await screen.findByRole("dialog", { name: "智能补全空列 · 时钟漂移" });
  const accept = await within(dialog).findByRole("button", { name: "接受 处理方法 建议" });
  await user.click(accept);

  const item = accept.closest("section") as HTMLElement;
  expect(await within(item).findByRole("alert")).toBeInTheDocument();
  await waitFor(() => expect(accept).toBeEnabled());
  await settle();
  expect(server.log).toEqual([
    LIST,
    REC,
    rowCode("r2"),
    {
      method: "POST",
      path: "/notebooks/nb-1/knowhow/t-rec/rows/r2/complete/stream",
      body: { target_column_ids: ["c-fix", "c-tool"] },
    },
    {
      method: "PATCH",
      path: "/notebooks/nb-1/knowhow/t-rec/rows/r2/cells/c-fix",
      body: { content_md: "校准晶振", expected_before: "", origin: "llm_complete" },
    },
  ]);
});

// ---------------------------------------------------------------------------
// 10. Row code status
// ---------------------------------------------------------------------------

function codeEntry(columnId: string, status: "implemented" | "stale") {
  return {
    column_id: columnId, language: "python", code_text: "print(1)", status,
    updated_at: "2026-10-01T00:00:00Z", updated_by: "alice",
  };
}

test("the drawer fetches its row's code once and renders a chip per column", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/agent/knowhow/rows/r1", { json: { code: [codeEntry("c-fix", "implemented")] } });
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: R1_TITLE });
  expect(await within(drawer).findByRole("button", { name: /已实现/ })).toBeInTheDocument();
  expect(within(drawer).getAllByRole("button", { name: /添加代码/ })).toHaveLength(2);
  await settle();
  expect(server.log).toEqual([LIST, REC, rowCode("r1")]);
});

test("a failed code-status load shows an inline retry that refetches", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.once("GET", "/agent/knowhow/rows/r1", { status: 500, json: { detail: "boom" } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: R1_TITLE });
  expect(await within(drawer).findByText("代码状态加载失败")).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: /添加代码/ })).not.toBeInTheDocument();
  await user.click(within(drawer).getByRole("button", { name: "重试" }));
  expect(await within(drawer).findAllByRole("button", { name: /添加代码/ })).toHaveLength(3);
  expect(within(drawer).queryByText("代码状态加载失败")).not.toBeInTheDocument();
  await settle();
  expect(server.log).toEqual([LIST, REC, rowCode("r1"), rowCode("r1")]);
});

test("saving a cell of the open row refetches that row's code status", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-tool", {
    json: wirePatchResult("r1", "c-tool", "示波器"),
  });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: R1_TITLE });
  const editButtons = within(drawer).getAllByRole("button", { name: "编辑" });
  await user.click(editButtons[2]);
  const editor = await screen.findByRole("dialog", { name: `${R1_TITLE} › 工具` });
  const textbox = within(editor).getByRole("textbox");
  await user.clear(textbox);
  await user.type(textbox, "示波器");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 工具/ })).not.toBeInTheDocument());
  await settle();
  expect(server.log).toEqual([
    { method: "GET", path: "/notebooks/nb-1/knowhow" },
    { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec" },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
    { method: "PATCH", path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-tool", body: { content_md: "示波器" } },
    { method: "GET", path: "/agent/knowhow/rows/r1" },
  ]);
});

test("a late code-status response for a row the user already left is discarded", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  const releaseR1 = server.hold("GET", "/agent/knowhow/rows/r1");
  server.on("GET", "/agent/knowhow/rows/r2", { json: { code: [codeEntry("c-fix", "stale")] } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const first = await screen.findByRole("dialog", { name: R1_TITLE });
  await user.click(within(first).getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "时钟漂移" }));
  const second = await screen.findByRole("dialog", { name: "时钟漂移" });
  expect(await within(second).findByRole("button", { name: /知识已更新/ })).toBeInTheDocument();
  releaseR1({ json: { code: [codeEntry("c-fix", "implemented")] } });
  await settle();
  expect(within(second).queryByRole("button", { name: /已实现/ })).not.toBeInTheDocument();
  expect(within(second).getByRole("button", { name: /知识已更新/ })).toBeInTheDocument();
  expect(server.log).toEqual([LIST, REC, rowCode("r1"), rowCode("r2")]);
});

// ---------------------------------------------------------------------------
// 11. Escape layering and DOM stacking order
// ---------------------------------------------------------------------------

const follows = (earlier: Element, later: Element) =>
  Boolean(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING);

// Every overlay the row drawer's `cellModalOpen` flag accounts for: with the
// overlay on top of the drawer, the first Escape closes only the overlay and the
// second one the drawer. The flag is a five-way disjunction that S4-d moves into
// the row-detail slice, so each term gets its own case.
type EscOverlay = {
  name: string;
  rowId: "r1" | "r2";
  open: (user: ReturnType<typeof userEvent.setup>, drawer: HTMLElement) => Promise<void>;
  overlay: string;
  extraRequests: Array<{ method: string; path: string; body?: unknown }>;
};

const R2_TITLE = "时钟漂移";

const ESC_OVERLAYS: EscOverlay[] = [
  {
    name: "cell editor",
    rowId: "r1",
    open: async (user, drawer) => user.click(within(drawer).getAllByRole("button", { name: "编辑" })[0]),
    overlay: `${R1_TITLE} › 问题`,
    extraRequests: [],
  },
  {
    name: "code modal",
    rowId: "r1",
    open: async (user, drawer) => user.click((await within(drawer).findAllByRole("button", { name: /添加代码/ }))[0]),
    overlay: `${R1_TITLE} › 问题 › 代码`,
    extraRequests: [],
  },
  {
    name: "row optimisation",
    rowId: "r1",
    open: async (user, drawer) => user.click(within(drawer).getByRole("button", { name: /优化整行/ })),
    overlay: `优化整行 · ${R1_TITLE}`,
    extraRequests: [{ method: "POST", path: "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title/optimize/stream" }],
  },
  {
    name: "row completion",
    rowId: "r2",
    open: async (user, drawer) => user.click(within(drawer).getByRole("button", { name: "智能补全空列" })),
    overlay: `智能补全空列 · ${R2_TITLE}`,
    extraRequests: [{
      method: "POST",
      path: "/notebooks/nb-1/knowhow/t-rec/rows/r2/complete/stream",
      body: { target_column_ids: ["c-fix", "c-tool"] },
    }],
  },
  {
    name: "row reformat",
    rowId: "r1",
    open: async (user, drawer) => user.click(within(drawer).getByRole("button", { name: /一键规整整行/ })),
    overlay: `一键规整整行 · ${R1_TITLE}`,
    extraRequests: [],
  },
];

for (const overlay of ESC_OVERLAYS) {
  test(`with the drawer and the ${overlay.name} open, the first Escape closes only the ${overlay.name}`, async () => {
    const server = suite.server();
    routeTable(server, "nb-1", recordDetail());
    server.on("POST", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-title/optimize/stream", {
      stream: { suggestion_md: "供电电压异常" },
    });
    server.on("POST", "/notebooks/nb-1/knowhow/t-rec/rows/r2/complete/stream", { stream: { ...COMPLETION, suggestions: [] } });
    const user = userEvent.setup();
    renderPanel({ initialTableId: "t-rec", initialRowId: overlay.rowId });
    const drawerName = overlay.rowId === "r1" ? R1_TITLE : R2_TITLE;

    const drawer = await screen.findByRole("dialog", { name: drawerName });
    await overlay.open(user, drawer);
    expect(await screen.findByRole("dialog", { name: overlay.overlay })).toBeInTheDocument();
    await settle();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: overlay.overlay })).not.toBeInTheDocument());
    await settle();
    expect(screen.getByRole("dialog", { name: drawerName })).toBe(drawer);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: drawerName })).not.toBeInTheDocument());
    await settle();
    expect(server.log).toEqual([LIST, REC, rowCode(overlay.rowId), ...overlay.extraRequests]);
  });
}

test("stacked kh-modals render in DOM order matrix → completion → cell modal", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", anchorDetail());
  server.on("POST", "/notebooks/nb-1/knowhow/t-anc/rows/a2/complete/stream", { stream: { ...COMPLETION, suggestions: [] } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  await user.click(await screen.findByRole("button", { name: "时钟" }));
  const matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  await user.click(within(matrix).getByRole("button", { name: "智能补全分支 2 的空列" }));
  const completion = await screen.findByRole("dialog", { name: "智能补全空列 · 时钟" });
  // The completion overlay covers the matrix for a pointer; the stacking order is
  // what is being pinned, so the matrix cell is activated on the DOM node.
  fireEvent.click(within(matrix).getByRole("button", { name: "主板" }));
  const cell = await screen.findByRole("dialog", { name: "时钟 › 分支备注" });

  expect(follows(matrix, completion)).toBe(true);
  expect(follows(completion, cell)).toBe(true);
  expect(follows(matrix, cell)).toBe(true);
  await settle();
  expect(server.log).toEqual([
    LIST,
    ANC,
    { method: "POST", path: "/notebooks/nb-1/knowhow/t-anc/rows/a2/complete/stream", body: { target_column_ids: ["c-own"] } },
  ]);
});

test("the history drawer renders before the cell modal, so a cell modal opened over it stacks on top", async () => {
  const server = suite.server();
  routeTable(server, "nb-1", recordDetail());
  server.on("GET", "/notebooks/nb-1/knowhow/t-rec/history?limit=100", { json: { head_seq: 0, changes: [], milestones: [] } });
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  await user.click(await screen.findByRole("button", { name: "历史" }));
  const history = await screen.findByRole("dialog", { name: "历史：排障记录" });
  // Same reason as above: the grid cell sits under the history overlay.
  fireEvent.click(screen.getByRole("button", { name: "检查电源" }));
  const cell = await screen.findByRole("dialog", { name: `${R1_TITLE} › 处理方法` });
  expect(follows(history, cell)).toBe(true);
  await settle();
  expect(server.log).toEqual([LIST, REC, { method: "GET", path: "/notebooks/nb-1/knowhow/t-rec/history?limit=100" }]);
});
