// Knowhow cell optimistic concurrency, frontend half (share-edit P1, decision D7;
// spec docs/superpowers/specs/2026-10-28-share-edit-collaboration-design_zh.md §5
// "D7 的硬规定" and its test list).
//
// The fake server here is stateful and enforces the backend contract the way
// `knowhow_routes.py` / `update_knowhow_cells_guarded_atomic` do: a write that
// carries `expected_before` is compared byte-for-byte with the stored value and
// refused with the structured 409 (`content_changed` + every target's current
// value, or `group_changed` when the anchor guard's group moved); a successful
// write answers with the value it STORED, which here is deliberately not the
// typed text (it trims), so a client that used its own input as the next
// baseline would conflict with itself on the second save.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import {
  anchorDetail,
  recordDetail,
  renderPanel,
  registerKnowhowPanelSuite,
  settle,
  wireDetail,
  wirePatchResult,
  wireSummary,
  type FakeKnowhowServer,
  type FakeReply,
  BASE_PROPS,
} from "../../test-support/knowhow-panel-harness.tsx";
import { KnowhowPanel } from "../../app/knowhow-panel.tsx";
import type { KnowhowTableDetail } from "../../app/knowhow-model.ts";

const suite = registerKnowhowPanelSuite();

type Scope = Pick<typeof screen, "findByRole" | "getByRole" | "queryByRole" | "getAllByRole" | "findByText" | "getByText">;

type WireCellsBatch = {
  column_id: string;
  row_ids: string[];
  content_md: string;
  expected_before?: string[];
  anchor_column_id?: string;
  expected_anchor?: string[];
};

function conflictReply(
  reason: "content_changed" | "group_changed",
  current: Array<{ row_id: string; column_id: string; content_md: string }>,
): FakeReply {
  return {
    status: 409,
    json: {
      detail: {
        code: "knowhow_cell_conflict",
        reason,
        message: reason === "content_changed" ? "内容已被其他人修改，请刷新后重试" : "这一组已被重新分组，请刷新后再编辑",
        current,
      },
    },
  };
}

/** A table the fake server owns: reads return its live state, guarded writes are checked against it. */
function statefulTable(server: FakeKnowhowServer, initial: KnowhowTableDetail, legacyResponses = false) {
  const table: KnowhowTableDetail = structuredClone(initial);
  const stats = { conflicts: 0 };
  const store = (md: string) => md.trim(); // the server's normalisation
  const read = (rowId: string, columnId: string) => table.rows.find((row) => row.id === rowId)?.cells[columnId] ?? "";
  const current = (rowId: string, columnId: string) => ({ row_id: rowId, column_id: columnId,
    content_md: read(rowId, columnId), ...(table.mutationSeq !== undefined ? { mutation_seq: table.mutationSeq } : {}) });
  const write = (rowId: string, columnId: string, contentMd: string, bump = true) => {
    const row = table.rows.find((item) => item.id === rowId)!;
    row.cells = { ...row.cells, [columnId]: contentMd };
    row.projectionStatus = "pending";
    if (bump && table.mutationSeq !== undefined) table.mutationSeq += 1;
  };
  const prefix = `/notebooks/nb-1/knowhow/${table.id}`;
  server.on("GET", "/notebooks/nb-1/knowhow", { json: [wireSummary({ id: table.id, title: table.title, rowCount: table.rows.length })] });
  server.on("GET", prefix, () => ({ json: wireDetail(table) }));
  for (const row of table.rows) {
    server.on("GET", `/agent/knowhow/rows/${row.id}`, { json: { code: [] } });
    for (const column of table.columns) {
      server.on("PATCH", `${prefix}/rows/${row.id}/cells/${column.id}`, (request) => {
        const body = request.body as { content_md: string; expected_before?: string };
        if (body.expected_before !== undefined && body.expected_before !== read(row.id, column.id)) {
          stats.conflicts += 1;
          return conflictReply("content_changed", [current(row.id, column.id)]);
        }
        const stored = store(body.content_md);
        write(row.id, column.id, stored);
        return { json: wirePatchResult(row.id, column.id, stored, legacyResponses ? undefined : table.mutationSeq) };
      });
    }
  }
  server.on("PATCH", `${prefix}/cells`, (request) => {
    const body = request.body as WireCellsBatch;
    if (body.anchor_column_id !== undefined) {
      const anchor = body.anchor_column_id;
      const expectedAnchor = body.expected_anchor ?? [];
      const moved = body.row_ids.some((rowId, index) => read(rowId, anchor) !== expectedAnchor[index]);
      const values = new Set(expectedAnchor.map((value) => value.trim()));
      const members = table.rows.filter((row) => values.has((row.cells[anchor] ?? "").trim())).map((row) => row.id).sort();
      if (moved || table.anchorColumnId !== anchor || JSON.stringify(members) !== JSON.stringify([...body.row_ids].sort())) {
        stats.conflicts += 1;
        return conflictReply("group_changed", []);
      }
    }
    const expected = body.expected_before;
    if (expected && body.row_ids.some((rowId, index) => read(rowId, body.column_id) !== expected[index])) {
      stats.conflicts += 1;
      return conflictReply(
        "content_changed",
        body.row_ids.map((rowId) => current(rowId, body.column_id)),
      );
    }
    const stored = store(body.content_md);
    if (table.mutationSeq !== undefined) table.mutationSeq += 1;
    for (const rowId of body.row_ids) write(rowId, body.column_id, stored, false);
    return { json: body.row_ids.map((rowId) => wirePatchResult(rowId, body.column_id, stored, legacyResponses ? undefined : table.mutationSeq)) };
  });
  return { stats, read, write, snapshot: () => structuredClone(table) };
}

function patches(server: FakeKnowhowServer) {
  return server.log.filter((request) => request.method === "PATCH");
}

/** Click a filled grid cell, switch the preview to the editor, replace the text. */
async function openAndType(
  user: ReturnType<typeof userEvent.setup>,
  scope: Scope,
  cellButton: string,
  columnName: string,
  text: string,
): Promise<HTMLElement> {
  const name = new RegExp(`› ${columnName}$`);
  await user.click(await scope.findByRole("button", { name: cellButton }));
  const preview = await scope.findByRole("dialog", { name });
  await user.click(within(preview).getByRole("button", { name: "编辑" }));
  const textbox = await waitFor(() => within(scope.getByRole("dialog", { name })).getByRole("textbox"));
  await user.clear(textbox);
  await user.type(textbox, text);
  return scope.getByRole("dialog", { name });
}

async function save(user: ReturnType<typeof userEvent.setup>, scope: Scope, editor: HTMLElement, columnName: string) {
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  await waitFor(() => expect(scope.queryByRole("dialog", { name: new RegExp(`› ${columnName}$`) })).not.toBeInTheDocument());
  await settle();
}

function renderTab(): Scope {
  const { container } = render(<KnowhowPanel {...BASE_PROPS} initialTableId="t-rec" />);
  return within(container);
}

// ---------------------------------------------------------------------------
// One user, one tab: never conflicts with itself
// ---------------------------------------------------------------------------

test("one tab saving a plain cell N times never conflicts: each baseline is the stored value the last save returned", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  let shown = "检查电源";
  for (const round of [1, 2, 3]) {
    // Trailing spaces: the server stores the trimmed text, so the typed text is
    // never what the next save must name as expected_before.
    const editor = await openAndType(user, screen, shown, "处理方法", `复位第${round}次  `);
    await save(user, screen, editor, "处理方法");
    shown = `复位第${round}次`;
    expect(screen.getByRole("button", { name: shown })).toBeInTheDocument();
  }

  expect(state.stats.conflicts).toBe(0);
  expect(patches(server).map((request) => request.body)).toEqual([
    { content_md: "复位第1次  ", expected_before: "检查电源" },
    { content_md: "复位第2次  ", expected_before: "复位第1次" },
    { content_md: "复位第3次  ", expected_before: "复位第2次" },
  ]);
  expect(state.read("r1", "c-fix")).toBe("复位第3次");
});

test("one tab saving a merged shared cell N times never conflicts: per-row stored baselines plus the anchor guard", async () => {
  const server = suite.server();
  const state = statefulTable(server, anchorDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  let shown = "检查时钟源";
  for (const round of [1, 2, 3]) {
    const editor = await openAndType(user, screen, shown, "共同方法", `换晶振${round}  `);
    expect(within(editor).getByText(/2 个分支/)).toBeInTheDocument();
    await save(user, screen, editor, "共同方法");
    shown = `换晶振${round}`;
  }

  expect(state.stats.conflicts).toBe(0);
  const previous = ["检查时钟源", "换晶振1", "换晶振2"];
  expect(patches(server).map((request) => request.body)).toEqual(previous.map((before, index) => ({
    column_id: "c-shared",
    row_ids: ["a1", "a2"],
    content_md: `换晶振${index + 1}  `,
    expected_before: [before, before],
    anchor_column_id: "c-anchor",
    expected_anchor: ["时钟", "时钟"],
  })));
  expect([state.read("a1", "c-shared"), state.read("a2", "c-shared")]).toEqual(["换晶振3", "换晶振3"]);
});

test("a delayed save from a previous table visit cannot overwrite a reopened table or close its new editor", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  statefulTable(server, recordDetail({
    id: "t-other", title: "另一张表",
    rows: recordDetail().rows.map((row) => ({ ...row, id: `other-${row.id}` })),
  }));
  const user = userEvent.setup();
  const panel = renderPanel({ initialTableId: "t-rec" });
  const oldEditor = await openAndType(user, screen, "检查电源", "处理方法", "第一次保存");
  const releaseOldSave = server.hold("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix");
  await user.click(within(oldEditor).getByRole("button", { name: /^保存$/ }));
  await waitFor(() => expect(patches(server)).toHaveLength(1));
  // The first transaction committed, but its response is delayed. A later
  // visit can therefore read that value and successfully save a newer one.
  state.write("r1", "c-fix", "第一次保存");
  panel.update({ ...BASE_PROPS, initialTableId: "t-other" });
  await screen.findByRole("heading", { name: "另一张表" });
  panel.update({ ...BASE_PROPS, initialTableId: "t-rec" });
  const newEditor = await openAndType(user, screen, "第一次保存", "处理方法", "重开后的保存");
  await save(user, screen, newEditor, "处理方法");
  const currentEditor = await openAndType(user, screen, "重开后的保存", "处理方法", "当前未保存草稿");

  releaseOldSave({ json: wirePatchResult("r1", "c-fix", "第一次保存") });
  await settle();
  expect(screen.getByRole("button", { name: "重开后的保存" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "第一次保存" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: /› 处理方法$/ })).toBe(currentEditor);
  expect(within(currentEditor).getByRole("textbox")).toHaveValue("当前未保存草稿");
  expect(state.stats.conflicts).toBe(0);
});

// ---------------------------------------------------------------------------
// One user, two tabs of the same browser: never conflicts with itself
// ---------------------------------------------------------------------------

test("two tabs alternating on one cell never conflict, including tab B opening its editor only after A saved", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  const bus = suite.bus();
  const user = userEvent.setup();
  const tabA = renderTab();
  const tabB = renderTab();
  await tabA.findByRole("heading", { name: "排障记录" });
  await tabB.findByRole("heading", { name: "排障记录" });

  // A saves first. B never opened an editor, yet its table data follows A's save.
  let editor = await openAndType(user, tabA, "检查电源", "处理方法", "A 改的  ");
  await save(user, tabA, editor, "处理方法");
  expect(bus.posted).toEqual([{ name: "knowhow-cell:t-rec", data: [["r1", "c-fix", "A 改的"]] }]);
  expect(tabB.getByRole("button", { name: "A 改的" })).toBeInTheDocument();

  // B opens its editor only now: its baseline is A's stored value.
  editor = await openAndType(user, tabB, "A 改的", "处理方法", "B 改的  ");
  await save(user, tabB, editor, "处理方法");
  expect(tabA.getByRole("button", { name: "B 改的" })).toBeInTheDocument();

  // And back to A.
  editor = await openAndType(user, tabA, "B 改的", "处理方法", "A 再改  ");
  await save(user, tabA, editor, "处理方法");

  expect(state.stats.conflicts).toBe(0);
  expect(patches(server).map((request) => request.body)).toEqual([
    { content_md: "A 改的  ", expected_before: "检查电源" },
    { content_md: "B 改的  ", expected_before: "A 改的" },
    { content_md: "A 再改  ", expected_before: "B 改的" },
  ]);
});

test("a delayed response for A's earlier commit cannot replace B's later save in either tab", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail({ mutationSeq: 1 }));
  const bus = suite.bus();
  const user = userEvent.setup();
  const tabA = renderTab();
  const editorA = await openAndType(user, tabA, "检查电源", "处理方法", "A 的 v1");
  const releaseA = server.hold("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix");
  await user.click(within(editorA).getByRole("button", { name: /^保存$/ }));
  await waitFor(() => expect(patches(server)).toHaveLength(1));
  state.write("r1", "c-fix", "A 的 v1"); // committed revision 2; HTTP response delayed

  const tabB = renderTab(); // reads A's committed revision 2
  const editorB = await openAndType(user, tabB, "A 的 v1", "处理方法", "B 的 v2");
  await save(user, tabB, editorB, "处理方法"); // revision 3 reaches A before its response
  expect(within(editorA).getByRole("textbox")).toHaveValue("A 的 v1");
  releaseA({ json: wirePatchResult("r1", "c-fix", "A 的 v1", 2) });
  await waitFor(() => expect(tabA.queryByRole("dialog", { name: /› 处理方法$/ })).not.toBeInTheDocument());
  expect(tabA.getByRole("button", { name: "B 的 v2" })).toBeInTheDocument();
  expect(tabB.getByRole("button", { name: "B 的 v2" })).toBeInTheDocument();
  expect(bus.posted).toEqual([{ name: "knowhow-cell:t-rec", data: [["r1", "c-fix", "B 的 v2", 3]] }]);

  const next = await openAndType(user, tabA, "B 的 v2", "处理方法", "A 的 v3");
  await save(user, tabA, next, "处理方法");
  expect(patches(server).at(-1)?.body).toEqual({ content_md: "A 的 v3", expected_before: "B 的 v2" });
  expect(state.stats.conflicts).toBe(0);
  expect(tabB.getByRole("button", { name: "A 的 v3" })).toBeInTheDocument();
  expect(server.log.filter((request) => request.method === "GET" && request.path === "/notebooks/nb-1/knowhow/t-rec")).toHaveLength(2);
});

test("opening another cell after its browser update was queued uses that saved value as the next save baseline", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail({ mutationSeq: 1 }));
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });
  await user.click(await screen.findByRole("button", { name: "检查电源" }));
  const preview = await screen.findByRole("dialog", { name: /› 处理方法$/ });
  await user.click(within(preview).getByRole("button", { name: "编辑" }));
  const editorX = screen.getByRole("dialog", { name: /› 处理方法$/ });
  state.write("r1", "c-fix", "new server fix");
  const snapshotBeforeY = wireDetail(state.snapshot());
  const releaseRead = server.hold("GET", "/notebooks/nb-1/knowhow/t-rec");
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix/reformat/stream", {
    stream: { candidate_md: "reformatted fix", source: "llm", changed: true, source_md: "new server fix" },
  });
  await user.click(within(editorX).getByRole("button", { name: "规整格式" }));
  await waitFor(() => expect(server.log.filter((request) => request.method === "GET" && request.path === "/notebooks/nb-1/knowhow/t-rec")).toHaveLength(2));
  // The background read is pending. Y has no pin yet when its peer's save arrives.
  state.write("r1", "c-tool", "peer's saved Y");
  const peer = new BroadcastChannel("knowhow-cell:t-rec");
  peer.postMessage([["r1", "c-tool", "peer's saved Y", 3]]);
  await settle();
  await user.click(within(editorX).getByRole("button", { name: /工具/ }));
  const editorY = await screen.findByRole("dialog", { name: /› 工具$/ });
  await user.clear(within(editorY).getByRole("textbox"));
  await user.type(within(editorY).getByRole("textbox"), "my next Y");
  releaseRead({ json: snapshotBeforeY });
  await screen.findByRole("button", { name: "peer's saved Y" });
  await user.click(within(editorY).getByRole("button", { name: /^保存$/ }));
  await waitFor(() => expect(patches(server)).toHaveLength(1));
  expect(patches(server)[0].body).toEqual({ content_md: "my next Y", expected_before: "peer's saved Y" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 工具$/ })).not.toBeInTheDocument());
  expect(state.stats.conflicts).toBe(0);
  peer.close();
});

test("a legacy write without a revision is confirmed by a fresh versioned table read", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail({ mutationSeq: 1 }), true);
  const bus = suite.bus();
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });
  const editor = await openAndType(user, screen, "检查电源", "处理方法", "legacy save");
  await save(user, screen, editor, "处理方法");
  expect(screen.getByRole("button", { name: "legacy save" })).toBeInTheDocument();
  expect(server.log.filter((request) => request.method === "GET" && request.path === "/notebooks/nb-1/knowhow/t-rec")).toHaveLength(2);
  expect(bus.posted).toEqual([]); // a read revision must never masquerade as the original commit
  const next = await openAndType(user, screen, "legacy save", "处理方法", "legacy save again");
  await save(user, screen, next, "处理方法");
  expect(state.stats.conflicts).toBe(0);
  expect(patches(server).at(-1)?.body).toEqual({ content_md: "legacy save again", expected_before: "legacy save" });
});

test("a failed legacy confirmation read preserves the draft and reports the saved-but-unconfirmed outcome beside Save", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail({ mutationSeq: 1 }), true);
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });
  const editor = await openAndType(user, screen, "检查电源", "处理方法", "already committed draft");
  server.once("GET", "/notebooks/nb-1/knowhow/t-rec", { status: 500, json: { detail: "internal" } });
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  expect(await within(editor).findByText("保存已完成，但最新内容暂时无法确认，当前编辑内容仍保留")).toBeInTheDocument();
  expect(within(editor).getByRole("textbox")).toHaveValue("already committed draft");
  expect(within(editor).getByRole("button", { name: /^保存$/ })).toBeEnabled();
  expect(state.read("r1", "c-fix")).toBe("already committed draft");
});

test("an unexplained change after a legacy save shows the existing conflict controls and retains the saved draft", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail({ mutationSeq: 1 }), true);
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });
  const editor = await openAndType(user, screen, "检查电源", "处理方法", "my saved draft");
  server.once("GET", "/notebooks/nb-1/knowhow/t-rec", () => {
    state.write("r1", "c-fix", "a later writer");
    return { json: wireDetail(state.snapshot()) };
  });
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  const conflict = await within(editor).findByRole("group", { name: "保存冲突" });
  expect(within(conflict).getByText("a later writer")).toBeInTheDocument();
  expect(within(editor).getByRole("textbox")).toHaveValue("my saved draft");
  await user.click(within(conflict).getByRole("button", { name: "用我的覆盖" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 处理方法$/ })).not.toBeInTheDocument());
  expect(patches(server).at(-1)?.body).toEqual({ content_md: "my saved draft", expected_before: "a later writer" });
  expect(state.stats.conflicts).toBe(0);
  expect(state.read("r1", "c-fix")).toBe("my saved draft");
});

test("a tab whose editor is open when the other tab saves moves its baseline forward but keeps its draft", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  const user = userEvent.setup();
  const tabA = renderTab();
  const tabB = renderTab();
  await tabA.findByRole("heading", { name: "排障记录" });
  await tabB.findByRole("heading", { name: "排障记录" });

  const editorA = await openAndType(user, tabA, "检查电源", "处理方法", "A 的草稿");
  const editorB = await openAndType(user, tabB, "检查电源", "处理方法", "B 先存");
  await save(user, tabB, editorB, "处理方法");

  // A's draft is untouched; only the table data (and the baseline) moved.
  expect(within(editorA).getByRole("textbox")).toHaveValue("A 的草稿");
  expect(tabA.getByRole("button", { name: "B 先存" })).toBeInTheDocument();

  await save(user, tabA, editorA, "处理方法");
  expect(state.stats.conflicts).toBe(0);
  expect(patches(server).map((request) => request.body)).toEqual([
    { content_md: "B 先存", expected_before: "检查电源" },
    { content_md: "A 的草稿", expected_before: "B 先存" },
  ]);
  expect(state.read("r1", "c-fix")).toBe("A 的草稿");
});

test("a broadcast that arrives while the table detail is loading waits for it, so the older snapshot cannot undo it", async () => {
  // Main-session ruling on T14: tab B's table request is in flight when tab A saves.
  // The reply B is waiting for is a snapshot from BEFORE A's save; applied after the
  // broadcast it would put the old value back, B would pin it and then be refused
  // (409) by its own browser's write. The broadcast is queued and laid over the reply.
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  const bus = suite.bus();
  const user = userEvent.setup();
  const tabA = renderTab();
  await tabA.findByRole("heading", { name: "排障记录" });

  const before = wireDetail(recordDetail()); // what the server held when B asked
  const releaseB = server.hold("GET", "/notebooks/nb-1/knowhow/t-rec");
  const tabB = renderTab();
  await waitFor(() => expect(bus.open("knowhow-cell:t-rec")).toBe(2));

  const editor = await openAndType(user, tabA, "检查电源", "处理方法", "A 改的  ");
  await save(user, tabA, editor, "处理方法");
  expect(bus.posted).toEqual([{ name: "knowhow-cell:t-rec", data: [["r1", "c-fix", "A 改的"]] }]);
  await settle();

  releaseB({ json: before });
  expect(await tabB.findByRole("button", { name: "A 改的" })).toBeInTheDocument();
  expect(tabB.queryByRole("button", { name: "检查电源" })).not.toBeInTheDocument();

  const editorB = await openAndType(user, tabB, "A 改的", "处理方法", "B 改的");
  await save(user, tabB, editorB, "处理方法");
  expect(state.stats.conflicts).toBe(0);
  expect(patches(server).map((request) => request.body)).toEqual([
    { content_md: "A 改的  ", expected_before: "检查电源" },
    { content_md: "B 改的", expected_before: "A 改的" },
  ]);
});

test("a merged shared cell saved in one tab is broadcast as one message carrying the whole group", async () => {
  const server = suite.server();
  statefulTable(server, anchorDetail());
  const bus = suite.bus();
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  const editor = await openAndType(user, screen, "检查时钟源", "共同方法", "换晶振");
  await save(user, screen, editor, "共同方法");
  expect(bus.posted).toEqual([
    { name: "knowhow-cell:t-anc", data: [["a1", "c-shared", "换晶振"], ["a2", "c-shared", "换晶振"]] },
  ]);
});

test("without BroadcastChannel the panel still saves (no cross-tab sharing, no error)", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  vi.stubGlobal("BroadcastChannel", undefined);
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const editor = await openAndType(user, screen, "检查电源", "处理方法", "复位");
  await save(user, screen, editor, "处理方法");
  expect(state.read("r1", "c-fix")).toBe("复位");
  expect(screen.getByRole("button", { name: "复位" })).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Two users: the later save gets the side-by-side versions, in place
// ---------------------------------------------------------------------------

test("a concurrent edit by someone else: 409 shows both versions beside the cell; overwrite resends with the current value", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const editor = await openAndType(user, screen, "检查电源", "处理方法", "我的改法");
  state.write("r1", "c-fix", "别人的改法"); // another user, another browser
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));

  const notice = await within(editor).findByRole("group", { name: "保存冲突" });
  expect(within(notice).getByText("这一格在你编辑期间被其他人改过，请选择保留哪个版本。")).toBeInTheDocument();
  expect(within(within(notice).getByText("对方的版本").closest("section")!).getByText("别人的改法")).toBeInTheDocument();
  expect(within(within(notice).getByText("我的版本").closest("section")!).getByText("我的改法")).toBeInTheDocument();
  // The editor stayed open with the draft; saving again with the same baseline is locked.
  expect(within(editor).getByRole("textbox")).toHaveValue("我的改法");
  expect(within(editor).getByRole("button", { name: /^保存$/ })).toBeDisabled();

  const release = server.hold("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix");
  await user.click(within(notice).getByRole("button", { name: "用我的覆盖" }));
  // In flight: both buttons locked, the pressed one says what it is doing.
  expect(within(notice).getByRole("button", { name: "覆盖中…" })).toBeDisabled();
  expect(within(notice).getByRole("button", { name: "放弃我的" })).toBeDisabled();
  state.write("r1", "c-fix", "我的改法");
  release({ json: wirePatchResult("r1", "c-fix", "我的改法") });

  // Overwriting is a save: like a plain save, a successful overwrite closes the editor
  // (main-session ruling), and the grid shows the stored value.
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 处理方法$/ })).not.toBeInTheDocument());
  await settle();
  expect(screen.getByRole("button", { name: "我的改法" })).toBeInTheDocument();

  expect(patches(server).map((request) => request.body)).toEqual([
    { content_md: "我的改法", expected_before: "检查电源" },
    { content_md: "我的改法", expected_before: "别人的改法" },
  ]);
});

test.each(["ctrlKey", "metaKey"])("%s shortcut cannot save a conflicted cell or resubmit a pending overwrite", async (modifier) => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });
  const editor = await openAndType(user, screen, "检查电源", "处理方法", "我的改法");
  state.write("r1", "c-fix", "别人的改法");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  const notice = await within(editor).findByRole("group", { name: "保存冲突" });

  fireEvent.keyDown(window, { key: "Enter", [modifier]: true });
  fireEvent.keyDown(window, { key: "Enter", [modifier]: true, shiftKey: true });
  await settle();
  expect(patches(server)).toHaveLength(1);
  expect(within(editor).getByRole("group", { name: "保存冲突" })).toBe(notice);

  const release = server.hold("PATCH", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-fix");
  await user.click(within(notice).getByRole("button", { name: "用我的覆盖" }));
  await waitFor(() => expect(patches(server)).toHaveLength(2));
  fireEvent.keyDown(window, { key: "Enter", [modifier]: true });
  fireEvent.keyDown(window, { key: "Enter", [modifier]: true, shiftKey: true });
  await settle();
  expect(patches(server)).toHaveLength(2);
  expect(within(editor).getByRole("group", { name: "保存冲突" })).toBe(notice);
  expect(within(notice).getByRole("button", { name: "覆盖中…" })).toBeDisabled();
  expect(within(editor).getByRole("textbox")).toHaveValue("我的改法");
  state.write("r1", "c-fix", "我的改法");
  release({ json: wirePatchResult("r1", "c-fix", "我的改法") });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 处理方法$/ })).not.toBeInTheDocument());
});

test("discarding mine takes their version into the editor and the table, and the next save does not conflict", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });

  const editor = await openAndType(user, screen, "检查电源", "处理方法", "我的改法");
  state.write("r1", "c-fix", "别人的改法");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  const notice = await within(editor).findByRole("group", { name: "保存冲突" });

  await user.click(within(notice).getByRole("button", { name: "放弃我的" }));
  expect(within(notice).getByRole("status")).toHaveTextContent("已换成对方的版本");
  expect(within(editor).getByRole("textbox")).toHaveValue("别人的改法");
  expect(screen.getByRole("button", { name: "别人的改法" })).toBeInTheDocument();
  await waitFor(() => expect(within(editor).queryByRole("group", { name: "保存冲突" })).not.toBeInTheDocument(), { timeout: 4000 });

  const textbox = within(editor).getByRole("textbox");
  await user.type(textbox, "，再补一句");
  await save(user, screen, editor, "处理方法");
  expect(patches(server).map((request) => request.body)).toEqual([
    { content_md: "我的改法", expected_before: "检查电源" },
    { content_md: "别人的改法，再补一句", expected_before: "别人的改法" },
  ]);
  expect(state.read("r1", "c-fix")).toBe("别人的改法，再补一句");
});

test("discarding an older conflict uses the latest browser value in both the input and its next save baseline", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail({ mutationSeq: 1 }));
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });
  const editor = await openAndType(user, screen, "检查电源", "处理方法", "my draft");
  state.write("r1", "c-fix", "conflict at v2");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  const notice = await within(editor).findByRole("group", { name: "保存冲突" });
  state.write("r1", "c-fix", "later browser v3");
  const peer = new BroadcastChannel("knowhow-cell:t-rec");
  peer.postMessage([["r1", "c-fix", "later browser v3", 3]]);
  await settle();
  await user.click(within(notice).getByRole("button", { name: "放弃我的" }));
  expect(within(editor).getByRole("textbox")).toHaveValue("later browser v3");
  expect(screen.getByRole("button", { name: "later browser v3" })).toBeInTheDocument();
  await waitFor(() => expect(within(editor).queryByRole("group", { name: "保存冲突" })).not.toBeInTheDocument(), { timeout: 4000 });
  await user.type(within(editor).getByRole("textbox"), " amended");
  await save(user, screen, editor, "处理方法");
  expect(patches(server).at(-1)?.body).toEqual({ content_md: "later browser v3 amended", expected_before: "later browser v3" });
  expect(state.stats.conflicts).toBe(1); // only the original real conflict
  peer.close();
});

test("discarding an older conflict after a background snapshot aligns the next save baseline with the adopted input", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail({ mutationSeq: 1 }));
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec" });
  await user.click(await screen.findByRole("button", { name: "万用表" }));
  const preview = await screen.findByRole("dialog", { name: /› 工具$/ });
  await user.click(within(preview).getByRole("button", { name: "编辑" }));
  const sourceEditor = screen.getByRole("dialog", { name: /› 工具$/ });
  state.write("r1", "c-tool", "new server tool");
  const releaseRead = server.hold("GET", "/notebooks/nb-1/knowhow/t-rec");
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/c-tool/reformat/stream", {
    stream: { candidate_md: "reformatted tool", source: "llm", changed: true, source_md: "new server tool" },
  });
  await user.click(within(sourceEditor).getByRole("button", { name: "规整格式" }));
  await waitFor(() => expect(server.log.filter((request) => request.method === "GET" && request.path === "/notebooks/nb-1/knowhow/t-rec")).toHaveLength(2));
  await user.click(within(sourceEditor).getByRole("button", { name: /处理方法/ }));
  const editor = await screen.findByRole("dialog", { name: /› 处理方法$/ });
  state.write("r1", "c-fix", "v1 conflict");
  await user.clear(within(editor).getByRole("textbox"));
  await user.type(within(editor).getByRole("textbox"), "my draft");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  const notice = await within(editor).findByRole("group", { name: "保存冲突" });
  expect(within(notice).getByText("v1 conflict")).toBeInTheDocument();

  state.write("r1", "c-fix", "v2 snapshot");
  releaseRead({ json: wireDetail(state.snapshot()) });
  await screen.findByRole("button", { name: "v2 snapshot" });
  expect(within(editor).getByRole("textbox")).toHaveValue("my draft");
  await user.click(within(notice).getByRole("button", { name: "放弃我的" }));
  expect(within(editor).getByRole("textbox")).toHaveValue("v2 snapshot");
  await waitFor(() => expect(within(editor).queryByRole("group", { name: "保存冲突" })).not.toBeInTheDocument(), { timeout: 4000 });
  await user.type(within(editor).getByRole("textbox"), " amended");
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));
  await waitFor(() => expect(patches(server)).toHaveLength(2));
  expect(patches(server).at(-1)?.body).toEqual({ content_md: "v2 snapshot amended", expected_before: "v2 snapshot" });
  await waitFor(() => expect(screen.queryByRole("dialog", { name: /› 处理方法$/ })).not.toBeInTheDocument());
  expect(state.stats.conflicts).toBe(1);
});

test("a regrouped shared cell: 409 group_changed offers no overwrite, only an inline refresh; after it the save goes through", async () => {
  const server = suite.server();
  const state = statefulTable(server, anchorDetail());
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc" });

  const editor = await openAndType(user, screen, "检查时钟源", "共同方法", "换晶振");
  state.write("a2", "c-anchor", "时钟（旧）"); // someone moved branch a2 out of the group
  await user.click(within(editor).getByRole("button", { name: /^保存$/ }));

  const notice = await within(editor).findByRole("group", { name: "保存冲突" });
  expect(notice).toHaveTextContent("这一组已被重新分组，请刷新后再编辑");
  expect(within(notice).queryByRole("button", { name: "用我的覆盖" })).not.toBeInTheDocument();
  expect(within(notice).queryByRole("button", { name: "放弃我的" })).not.toBeInTheDocument();

  server.clearLog();
  await user.click(within(notice).getByRole("button", { name: "刷新" }));
  expect(await within(notice).findByRole("status")).toHaveTextContent("已刷新，可以继续编辑");
  expect(server.lines()).toEqual(["GET /notebooks/nb-1/knowhow/t-anc"]);
  await waitFor(() => expect(within(editor).queryByRole("group", { name: "保存冲突" })).not.toBeInTheDocument(), { timeout: 4000 });

  // a1 is now alone in its concept: the cell is no longer shared, the draft survived.
  expect(within(editor).getByRole("textbox")).toHaveValue("换晶振");
  expect(within(editor).queryByText(/2 个分支/)).not.toBeInTheDocument();
  await save(user, screen, editor, "共同方法");
  expect(server.log.filter((request) => request.method === "PATCH")).toEqual([
    {
      method: "PATCH",
      path: "/notebooks/nb-1/knowhow/t-anc/rows/a1/cells/c-shared",
      body: { content_md: "换晶振", expected_before: "检查时钟源" },
    },
  ]);
  expect(state.read("a1", "c-shared")).toBe("换晶振");
  expect(state.read("a2", "c-shared")).toBe("检查时钟源");
});

// ---------------------------------------------------------------------------
// 「优化整行」 accept is part of D7
// ---------------------------------------------------------------------------

test("row optimisation accept carries the stored value from when the modal opened; a 409 shows both versions in the modal", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail());
  const optimizePath = (column: string) => `/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/${column}/optimize/stream`;
  server.on("POST", optimizePath("c-title"), { stream: { suggestion_md: "供电电压异常" } });
  server.hold("POST", optimizePath("c-fix"));
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });

  const drawer = await screen.findByRole("dialog", { name: "供电异常 · 检查电源 · 万用表" });
  await user.click(within(drawer).getByRole("button", { name: /优化整行/ }));
  const modal = await screen.findByRole("dialog", { name: "优化整行 · 供电异常 · 检查电源 · 万用表" });
  expect(await within(modal).findByText("供电电压异常")).toBeInTheDocument();

  state.write("r1", "c-title", "别人改的标题");
  await user.click(within(modal).getByRole("button", { name: /接受/ }));
  const notice = await within(modal).findByRole("group", { name: "保存冲突" });
  expect(within(within(notice).getByText("对方的版本").closest("section")!).getByText("别人改的标题")).toBeInTheDocument();
  expect(within(within(notice).getByText("我的版本").closest("section")!).getByText("供电电压异常")).toBeInTheDocument();
  // The footer's accept/skip step aside while the versions are on screen.
  expect(within(modal).queryByRole("button", { name: /接受/ })).not.toBeInTheDocument();

  await user.click(within(notice).getByRole("button", { name: "用我的覆盖" }));
  expect(await within(notice).findByRole("status")).toHaveTextContent("已用我的版本覆盖");
  expect(within(modal).getByText("已接受")).toBeInTheDocument();

  expect(patches(server).map((request) => request.body)).toEqual([
    { content_md: "供电电压异常", expected_before: "供电异常" },
    { content_md: "供电电压异常", expected_before: "别人改的标题" },
  ]);
  expect(state.read("r1", "c-title")).toBe("供电电压异常");
});

test("discarding an older row-optimisation conflict keeps a later browser save in the table", async () => {
  const server = suite.server();
  const state = statefulTable(server, recordDetail({ mutationSeq: 1 }));
  const optimizePath = (column: string) => `/notebooks/nb-1/knowhow/t-rec/rows/r1/cells/${column}/optimize/stream`;
  server.on("POST", optimizePath("c-title"), { stream: { suggestion_md: "candidate title" } });
  server.hold("POST", optimizePath("c-fix"));
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-rec", initialRowId: "r1" });
  const drawer = await screen.findByRole("dialog", { name: "供电异常 · 检查电源 · 万用表" });
  await user.click(within(drawer).getByRole("button", { name: /优化整行/ }));
  const modal = await screen.findByRole("dialog", { name: "优化整行 · 供电异常 · 检查电源 · 万用表" });
  expect(await within(modal).findByText("candidate title")).toBeInTheDocument();
  state.write("r1", "c-title", "conflict title v2");
  await user.click(within(modal).getByRole("button", { name: /接受/ }));
  const notice = await within(modal).findByRole("group", { name: "保存冲突" });
  state.write("r1", "c-title", "later browser title v3");
  const peer = new BroadcastChannel("knowhow-cell:t-rec");
  peer.postMessage([["r1", "c-title", "later browser title v3", 3]]);
  await settle();
  await user.click(within(notice).getByRole("button", { name: "放弃我的" }));
  expect(screen.getAllByRole("button", { name: "later browser title v3" }).length).toBeGreaterThan(0);
  expect(screen.queryByRole("button", { name: "conflict title v2" })).not.toBeInTheDocument();
  expect(state.read("r1", "c-title")).toBe("later browser title v3");
  peer.close();
});
