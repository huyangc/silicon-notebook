// KnowhowPanel characterization (debt-fix S4, C0): the three reset sets, state by
// state.
//
// KnowhowPanel resets its cross-cluster state in three places that do NOT clear
// the same things: openTable(), backToList() and the notebook-reset effect (E6).
// S4-d moves state into slices (list / grid / row detail / concept matrix) and is
// the place where someone is most likely to pull these into a shared reset helper
// and "fix" the asymmetry in passing. That would be a behaviour change, so this
// table records, for every user-visible state, what each reset path does to it
// today, by setting the state to a non-default value, running the reset, and
// looking at the screen.
//
// Reset paths and how they are driven:
//  * openTable — a re-targeted citation jump (new initialTableId): E7 clears the
//    jump notice and calls openTable(). The twin table has the same row ids, so
//    a row-bound overlay that survived would visibly come back on the twin.
//  * backToList — the toolbar's 返回 (clicked on the DOM node, because most of the
//    overlays cover the toolbar for a pointer), observed on the list and again
//    after opening the twin table from its card.
//  * E6 — switching notebookId, observed on the new notebook's list and again
//    after opening the twin table there.
//
// Only cells where a difference is observable are exercised. For every other
// state, backToList and E6 are "masked": the only way back to a table is
// openTable(), which clears the state anyway, so a change to those two resets
// cannot be seen (completeRowId's missing backToList reset is one of them). The
// request sequences of these flows are pinned by the scenario suites
// (lifecycle / editing / rows / table-actions); this table asserts visible state
// only, plus the shared "no unrouted request" check.
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
  type FakeKnowhowServer,
  type RenderedPanel,
} from "../../test-support/knowhow-panel-harness.tsx";

const suite = useKnowhowPanelSuite();

type User = ReturnType<typeof userEvent.setup>;
type Fixture = "record" | "anchor";

const R1_TITLE = "供电异常 · 检查电源 · 万用表";
const TABLES = {
  record: { id: "t-rec", title: "排障记录", twinId: "t-two", twinTitle: "第二张表" },
  anchor: { id: "t-anc", title: "概念表", twinId: "t-anc2", twinTitle: "概念表二" },
} as const;

function routeEverything(server: FakeKnowhowServer): void {
  const record = recordDetail();
  const recordTwin = recordDetail({ id: "t-two", title: "第二张表" });
  const anchor = anchorDetail();
  const anchorTwin = anchorDetail({ id: "t-anc2", title: "概念表二" });
  const summaries = [record, recordTwin, anchor, anchorTwin].map((detail) =>
    wireSummary({ id: detail.id, title: detail.title, rowCount: detail.rows.length }));
  for (const notebookId of ["nb-1", "nb-2"]) {
    routeTable(server, notebookId, record, summaries);
    for (const detail of [recordTwin, anchor, anchorTwin]) {
      server.on("GET", `/notebooks/${notebookId}/knowhow/${detail.id}`, { json: wireDetail(detail) });
    }
  }
  server.on("GET", "/agent/knowhow/rows/r-gone", { json: { code: [] } });
  server.on("GET", "/notebooks", {
    json: [{ id: "nb-1", name: "源笔记本", access: "owner" }, { id: "nb-2", name: "目标笔记本", access: "owner" }],
  });
  server.on("POST", "/notebooks/nb-1/knowhow/t-rec/transfer", { json: { new_table_id: "t-copy" } });
  for (const tableId of ["t-rec", "t-two"]) {
    server.on("GET", `/notebooks/nb-1/knowhow/${tableId}/history?limit=100`, { json: { head_seq: 0, changes: [], milestones: [] } });
    server.on("POST", `/notebooks/nb-1/knowhow/${tableId}/rows/r1/cells/c-title/optimize/stream`, {
      stream: { suggestion_md: "供电电压异常" },
    });
    server.on("POST", `/notebooks/nb-1/knowhow/${tableId}/rows/r2/complete/stream`, {
      stream: {
        retrieval_mode: "reasoning", retrieval_scope: "active_and_mounted", retrieval_status: "succeeded",
        reasoning_trace: [], evidence: [], suggestions: [],
      },
    });
  }
}

async function openRowDrawer(user: User, rowTitle: "供电异常" | "时钟漂移"): Promise<HTMLElement> {
  await user.click(screen.getByRole("button", { name: rowTitle }));
  return screen.findByRole("dialog", { name: rowTitle === "供电异常" ? R1_TITLE : "时钟漂移" });
}

const dialogShown = (name: string | RegExp) => () => screen.queryByRole("dialog", { name }) !== null;
const textShown = (text: string) => () => screen.queryByText(text) !== null;

type Outcome = "cleared" | "kept";

type StateCase = {
  state: string;
  fixture: Fixture;
  /** Extra mount props (a stale cited row, or staying on the list). */
  mount?: { initialRowId?: string; onList?: boolean };
  setup: (user: User) => Promise<void>;
  shown: () => boolean;
  openTable?: Outcome;
  backToList?: { list?: Outcome; reentry: Outcome };
  e6?: { list?: Outcome; reentry?: Outcome };
};

const JUMP_NOTICE = "引用指向的行未找到，可能已被删除或调整，已为你展开整表内容。";

const CASES: StateCase[] = [
  {
    state: "grid filter query",
    fixture: "record",
    setup: async () => {
      fireEvent.change(screen.getByPlaceholderText("按行标题/全文过滤行…"), { target: { value: "供电" } });
    },
    shown: () => (screen.queryByPlaceholderText("按行标题/全文过滤行…") as HTMLInputElement | null)?.value === "供电",
    openTable: "cleared",
  },
  {
    state: "copy notice (transferNotice)",
    fixture: "record",
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: "复制/移动到…" }));
      const picker = await screen.findByRole("dialog", { name: "复制/移动表：排障记录" });
      await within(picker).findByRole("option", { name: "目标笔记本" });
      await user.selectOptions(within(picker).getByRole("combobox"), "nb-2");
      await user.click(within(picker).getByRole("button", { name: "确认" }));
      await screen.findByText("已复制到目标笔记本");
    },
    shown: textShown("已复制到目标笔记本"),
    openTable: "cleared",
    backToList: { list: "cleared", reentry: "cleared" },
    e6: { list: "kept", reentry: "cleared" },
  },
  {
    state: "stale-citation notice (jumpNotice)",
    fixture: "record",
    mount: { initialRowId: "r-gone" },
    setup: async () => {
      await screen.findByText(JUMP_NOTICE);
    },
    shown: textShown(JUMP_NOTICE),
    // Cleared by the jump effect E7, not by openTable itself: opening another
    // table from its card keeps it (backToList re-entry below).
    openTable: "cleared",
    backToList: { list: "kept", reentry: "kept" },
    e6: { list: "kept", reentry: "kept" },
  },
  {
    state: "row drawer (openRowId)",
    fixture: "record",
    setup: async (user) => {
      await openRowDrawer(user, "供电异常");
    },
    shown: dialogShown(R1_TITLE),
    openTable: "cleared",
  },
  {
    state: "cell modal",
    fixture: "record",
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: "检查电源" }));
      await screen.findByRole("dialog", { name: `${R1_TITLE} › 处理方法` });
    },
    shown: dialogShown(/› 处理方法$/),
    openTable: "cleared",
  },
  {
    state: "table delete confirmation",
    fixture: "record",
    setup: async (user) => {
      await user.click(screen.getByTitle("删除表"));
    },
    shown: textShown("删除这张表？行、格与同步生成的内容将一并删除"),
    openTable: "cleared",
  },
  {
    state: "manage modal",
    fixture: "record",
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: "管理" }));
      await screen.findByRole("dialog", { name: "表管理：排障记录" });
    },
    shown: dialogShown(/^表管理：/),
    openTable: "cleared",
  },
  {
    state: "copy/move picker",
    fixture: "record",
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: "复制/移动到…" }));
      await screen.findByRole("dialog", { name: "复制/移动表：排障记录" });
    },
    shown: dialogShown(/^复制\/移动表：/),
    openTable: "cleared",
  },
  {
    state: "append wizard",
    fixture: "record",
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: "追加导入" }));
      await screen.findByRole("dialog", { name: "追加导入 Knowhow 表" });
    },
    shown: dialogShown("追加导入 Knowhow 表"),
    openTable: "cleared",
  },
  {
    state: "history drawer",
    fixture: "record",
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: "历史" }));
      await screen.findByRole("dialog", { name: "历史：排障记录" });
    },
    shown: dialogShown(/^历史：/),
    openTable: "cleared",
  },
  {
    state: "row optimisation modal",
    fixture: "record",
    setup: async (user) => {
      const drawer = await openRowDrawer(user, "供电异常");
      await user.click(within(drawer).getByRole("button", { name: /优化整行/ }));
      await screen.findByRole("dialog", { name: `优化整行 · ${R1_TITLE}` });
    },
    shown: dialogShown(/^优化整行 · /),
    openTable: "cleared",
  },
  {
    state: "row completion modal",
    fixture: "record",
    setup: async (user) => {
      const drawer = await openRowDrawer(user, "时钟漂移");
      await user.click(within(drawer).getByRole("button", { name: "智能补全空列" }));
      await screen.findByRole("dialog", { name: "智能补全空列 · 时钟漂移" });
    },
    shown: dialogShown(/^智能补全空列 · /),
    openTable: "cleared",
  },
  {
    state: "code modal",
    fixture: "record",
    setup: async (user) => {
      const drawer = await openRowDrawer(user, "供电异常");
      await user.click((await within(drawer).findAllByRole("button", { name: /添加代码/ }))[0]);
      await screen.findByRole("dialog", { name: `${R1_TITLE} › 问题 › 代码` });
    },
    shown: dialogShown(/› 代码$/),
    openTable: "cleared",
  },
  {
    state: "row reformat modal (reformatRowId)",
    fixture: "record",
    setup: async (user) => {
      const drawer = await openRowDrawer(user, "供电异常");
      await user.click(within(drawer).getByRole("button", { name: /一键规整整行/ }));
      await screen.findByRole("dialog", { name: `一键规整整行 · ${R1_TITLE}` });
    },
    shown: dialogShown(/^一键规整整行 · /),
    openTable: "kept",
    backToList: { reentry: "kept" },
    e6: { reentry: "cleared" },
  },
  {
    state: "table reformat modal (reformatTableOpen)",
    fixture: "record",
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: "一键规整整表" }));
      await screen.findByRole("dialog", { name: "一键规整整表 · 排障记录" });
    },
    shown: dialogShown(/^一键规整整表 · /),
    openTable: "kept",
    backToList: { reentry: "kept" },
    e6: { reentry: "cleared" },
  },
  {
    state: "concept matrix (openConceptValue)",
    fixture: "anchor",
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: "时钟" }));
      await screen.findByRole("dialog", { name: "概念 时钟" });
    },
    shown: dialogShown("概念 时钟"),
    openTable: "cleared",
  },
  {
    state: "import wizard (importOpen)",
    fixture: "record",
    mount: { onList: true },
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: /导入表格/ }));
      await screen.findByRole("dialog", { name: "导入 Knowhow 表" });
    },
    shown: dialogShown("导入 Knowhow 表"),
    e6: { list: "cleared" },
  },
  {
    state: "create wizard (createOpen)",
    fixture: "record",
    mount: { onList: true },
    setup: async (user) => {
      await user.click(screen.getByRole("button", { name: /新建表/ }));
      await screen.findByRole("dialog", { name: "新建 Knowhow 表" });
    },
    shown: dialogShown("新建 Knowhow 表"),
    e6: { list: "cleared" },
  },
];

async function mountCase(stateCase: StateCase): Promise<{ panel: RenderedPanel; user: User }> {
  const server = suite.server();
  routeEverything(server);
  const user = userEvent.setup();
  const table = TABLES[stateCase.fixture];
  const panel = renderPanel(stateCase.mount?.onList
    ? {}
    : { initialTableId: table.id, initialRowId: stateCase.mount?.initialRowId });
  if (stateCase.mount?.onList) {
    await screen.findByRole("button", { name: `打开表格：${table.title}` });
  } else {
    await screen.findByRole("heading", { name: table.title });
  }
  await settle();
  await stateCase.setup(user);
  await settle();
  expect(stateCase.shown(), `${stateCase.state} is shown before the reset`).toBe(true);
  return { panel, user };
}

async function openTwinFromList(user: User, stateCase: StateCase): Promise<void> {
  const table = TABLES[stateCase.fixture];
  await user.click(await screen.findByRole("button", { name: `打开表格：${table.twinTitle}` }));
  await screen.findByRole("heading", { name: table.twinTitle });
  await settle();
}

const isShown = (outcome: Outcome) => outcome === "kept";

for (const stateCase of CASES) {
  const outcome = stateCase.openTable;
  if (outcome) {
    test(`openTable (re-targeted jump) — ${stateCase.state}: ${outcome}`, async () => {
      const { panel } = await mountCase(stateCase);
      const table = TABLES[stateCase.fixture];
      panel.update({ ...BASE_PROPS, initialTableId: table.twinId });
      await screen.findByRole("heading", { name: table.twinTitle });
      await settle();
      expect(stateCase.shown()).toBe(isShown(outcome));
    });
  }

  const back = stateCase.backToList;
  if (back) {
    test(`backToList — ${stateCase.state}: list ${back.list ?? "n/a"}, re-entry ${back.reentry}`, async () => {
      const { user } = await mountCase(stateCase);
      fireEvent.click(screen.getByRole("button", { name: "返回" }));
      await screen.findByRole("button", { name: `打开表格：${TABLES[stateCase.fixture].twinTitle}` });
      await settle();
      if (back.list) expect(stateCase.shown()).toBe(isShown(back.list));
      await openTwinFromList(user, stateCase);
      expect(stateCase.shown()).toBe(isShown(back.reentry));
    });
  }

  const e6 = stateCase.e6;
  if (e6) {
    test(`notebook reset (E6) — ${stateCase.state}: list ${e6.list ?? "n/a"}, re-entry ${e6.reentry ?? "n/a"}`, async () => {
      const { panel, user } = await mountCase(stateCase);
      const table = TABLES[stateCase.fixture];
      panel.update({
        ...BASE_PROPS,
        notebookId: "nb-2",
        ...(stateCase.mount?.onList ? {} : { initialTableId: table.id, initialRowId: stateCase.mount?.initialRowId }),
      });
      await screen.findByRole("button", { name: `打开表格：${table.twinTitle}` });
      await waitFor(() => expect(screen.queryByRole("heading", { name: table.title })).not.toBeInTheDocument());
      await settle();
      if (e6.list) expect(stateCase.shown()).toBe(isShown(e6.list));
      if (e6.reentry) {
        await openTwinFromList(user, stateCase);
        expect(stateCase.shown()).toBe(isShown(e6.reentry));
      }
    });
  }
}

// The concept matrix's own close clears the citation highlight together with the
// open concept (like openTable / backToList / E6 do): reopening the concept by
// hand afterwards shows no highlighted branch.
test("closing a jump-opened concept matrix drops the highlight; reopening the concept shows none", async () => {
  const server = suite.server();
  routeEverything(server);
  const user = userEvent.setup();
  renderPanel({ initialTableId: "t-anc", initialRowId: "a2" });

  let matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  const highlighted = (dialog: HTMLElement) =>
    within(dialog).getAllByRole("columnheader").filter((header) => header.classList.contains("kh-matrix-branch--hi"));
  expect(highlighted(matrix)).toHaveLength(1);
  await user.click(within(matrix).getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "概念 时钟" })).not.toBeInTheDocument());
  await user.click(screen.getByRole("button", { name: "时钟" }));
  matrix = await screen.findByRole("dialog", { name: "概念 时钟" });
  expect(highlighted(matrix)).toHaveLength(0);
  expect(matrix.querySelectorAll(".kh-matrix-cell--hi")).toHaveLength(0);
});
