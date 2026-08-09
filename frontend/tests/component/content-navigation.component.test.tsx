import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { KnowhowPanel } from "../../app/knowhow-panel";
import { MemoryPanel } from "../../app/memory-panel";
import type { MemoryRecord, PaginatedMemories } from "../../app/workspace-model";


const knowhowTables = [
  {
    id: "pending-table",
    title: "待投影表",
    description: null,
    row_count: 1,
    projection_pending: 1,
    projection_failed: 0,
    stale_code_count: 0,
    last_activity_at: "",
  },
  {
    id: "failed-table",
    title: "失败表",
    description: null,
    row_count: 1,
    projection_pending: 0,
    projection_failed: 1,
    stale_code_count: 0,
    last_activity_at: "",
  },
];

const oldMemory: MemoryRecord = {
  id: "memory-old",
  notebook_id: "notebook-old",
  created_by: "user-1",
  origin: "external_agent",
  status: "candidate",
  promotion_state: "none",
  title: "旧目标",
  content_md: "旧内容",
  tags: [],
  embedding_status: "ready",
  embedding_error: "",
  created_at: "2026-07-20T00:00:00Z",
  updated_at: "2026-07-20T00:00:00Z",
  provenance: {},
};

const newMemory: MemoryRecord = {
  ...oldMemory,
  id: "memory-new",
  notebook_id: "notebook-new",
  origin: "ask_answer",
  status: "confirmed",
  title: "新目标",
  content_md: "新内容",
  provenance: {
    question: "新问题",
    mode: "chunk",
    evidence_level: "grounded",
    citations: [],
  },
};

const memoryPage: PaginatedMemories = {
  items: [oldMemory, newMemory],
  total_count: 41,
  offset: 0,
  limit: 20,
  owner_total_count: 41,
  owner_pending_count: 1,
  notebook_options: [
    { notebook_id: "notebook-old", name: "旧笔记本", memory_count: 1, pending_count: 1 },
    { notebook_id: "notebook-new", name: "新笔记本", memory_count: 1, pending_count: 0 },
  ],
};

let unexpectedConsoleErrors: unknown[][] = [];

function isKnownStyledJsxAttributeWarning(args: unknown[]): boolean {
  const [format, value, attribute, domAttribute, domValue, propName] = args;
  return format === (
    "Received `%s` for a non-boolean attribute `%s`.\n\n"
    + "If you want to write it to the DOM, pass a string instead: %s=\"%s\" or %s={value.toString()}."
  )
    && value === true
    && (attribute === "jsx" || attribute === "global")
    && domAttribute === attribute
    && domValue === true
    && propName === attribute;
}

beforeEach(() => {
  unexpectedConsoleErrors = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    if (!isKnownStyledJsxAttributeWarning(args)) unexpectedConsoleErrors.push(args);
  });
});

afterEach(() => {
  try {
    expect(unexpectedConsoleErrors).toEqual([]);
  } finally {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.localStorage.clear();
  }
});


test("KnowhowPanel follows health navigation targets when they change", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
    new Response(JSON.stringify(knowhowTables), { status: 200 }),
  ));
  const { rerender } = render(
    <KnowhowPanel
      notebookId="notebook-1"
      apiBase=""
      canEdit
      onClose={() => undefined}
      initialHealthFilter="projection_pending"
    />,
  );

  await screen.findByRole("button", { name: "打开表格：待投影表" });
  expect(screen.queryByRole("button", { name: "打开表格：失败表" })).not.toBeInTheDocument();

  rerender(
    <KnowhowPanel
      notebookId="notebook-1"
      apiBase=""
      canEdit
      onClose={() => undefined}
      initialHealthFilter="projection_failed"
    />,
  );
  await waitFor(() => expect(screen.getByLabelText("状态")).toHaveValue("projection_failed"));
  expect(screen.getByRole("button", { name: "打开表格：失败表" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "打开表格：待投影表" })).not.toBeInTheDocument();

  rerender(
    <KnowhowPanel notebookId="notebook-1" apiBase="" canEdit onClose={() => undefined} />,
  );
  await waitFor(() => expect(screen.getByLabelText("状态")).toHaveValue("all"));
  expect(screen.getByRole("button", { name: "打开表格：待投影表" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打开表格：失败表" })).toBeInTheDocument();
});


test("global Memory navigation replaces stale filters and opens the new target read-only", async () => {
  const fetchMock = vi.fn((url: string) => {
    if (url.includes("/memories?")) {
      return Promise.resolve(new Response(JSON.stringify(memoryPage), { status: 200 }));
    }
    return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const session = new AbortController();
  const user = userEvent.setup();
  const { rerender } = render(
    <MemoryPanel
      scope="global"
      notebookId={null}
      sessionSignal={session.signal}
      initialNotebookId="notebook-old"
      initialStatus="candidate"
      initialMemoryId="memory-old"
    />,
  );

  const oldTitle = await screen.findByRole("button", { name: "旧目标" });
  await waitFor(() => expect(oldTitle).toHaveAttribute("aria-expanded", "true"));
  await user.selectOptions(screen.getByLabelText("来源"), "external_agent");
  await user.type(screen.getByPlaceholderText("搜索标题、内容或标签"), "陈旧查询");
  await user.click(screen.getByRole("button", { name: "搜索" }));
  await user.click(screen.getByRole("button", { name: "审核" }));
  expect(screen.getByDisplayValue("旧目标")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(fetchMock.mock.calls.map(([url]) => String(url))).toContainEqual(
    expect.stringContaining("offset=20"),
  ));

  rerender(
    <MemoryPanel
      scope="global"
      notebookId={null}
      sessionSignal={session.signal}
      initialNotebookId="notebook-new"
      initialStatus="confirmed"
      initialMemoryId="memory-new"
    />,
  );

  await waitFor(() => {
    expect(screen.getByLabelText("笔记本")).toHaveValue("notebook-new");
    expect(screen.getByLabelText("状态")).toHaveValue("confirmed");
    expect(screen.getByLabelText("来源")).toHaveValue("all");
    expect(screen.getByPlaceholderText("搜索标题、内容或标签")).toHaveValue("");
  });
  await waitFor(() => {
    const paths = fetchMock.mock.calls.map(([url]) => String(url));
    expect(paths.some((path) => (
      path.includes("/memories?")
      && path.includes("notebook_id=notebook-new")
      && path.includes("status=confirmed")
      && path.includes("offset=0")
      && !path.includes("origin=")
      && !path.includes("query=")
    ))).toBe(true);
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "新目标" })).toHaveAttribute("aria-expanded", "true"));
  expect(screen.getByRole("button", { name: "旧目标" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByRole("button", { name: "审核" })).toBeInTheDocument();
  expect(screen.queryByDisplayValue("旧目标")).not.toBeInTheDocument();
});
