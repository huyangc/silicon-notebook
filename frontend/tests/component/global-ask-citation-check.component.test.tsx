// 全局问答窗口里的终态引用核对(PR-D):重开会话(刷新后按会话详情恢复)与推送流
// 终态帧两条路径都必须把 `citation_check` 与逐条 `verification` 原样带到界面上;
// 轨迹里后端追加的「核对」一步按普通步渲染。
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import GlobalAskPage from "../../app/ask/page.tsx";
import type {
  GlobalConversation, GlobalConversationDetail, GlobalJob, GlobalJobStreamOutcome,
} from "../../app/global-ask-api.ts";
import type { AskResponse, NotebookSummary } from "../../app/workspace-model.ts";

const api = vi.hoisted(() => ({
  me: vi.fn(), notebooks: vi.fn(), list: vi.fn(), detail: vi.fn(), poll: vi.fn(), stream: vi.fn(),
}));
vi.mock("../../app/auth.ts", () => ({ fetchMe: api.me }));
vi.mock("../../app/notebook-api.ts", () => ({ listNotebooks: api.notebooks }));
vi.mock("../../app/global-ask-api.ts", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../app/global-ask-api.ts")>(),
  listGlobalConversations: api.list, getGlobalConversation: api.detail,
  getGlobalJob: api.poll, streamGlobalJob: api.stream,
}));

const notebooks: NotebookSummary[] = [{
  id: "nb-0", name: "材料研究", purpose: "", primary_domain: "", status: "ready",
  counts: { sources: 2 }, created_label: "今天",
}];
const conversation: GlobalConversation = {
  id: "conv-a", title: "对话", created_at: "2026-09-29T01:00:00Z", updated_at: "2026-09-29T01:00:00Z",
  notebook_scope: { mode: "all" }, submitted_via: "web",
};
const job = (status: GlobalJob["status"]): GlobalJob => ({
  job_id: "job-a", conversation_id: "conv-a", status, question: "低温会怎样？",
  created_at: "2026-09-29T01:00:00Z", notebook_scope: { mode: "all" },
  resolved_notebook_ids: ["nb-0"], searched_notebook_ids: ["nb-0"], cited_notebook_ids: ["nb-0"],
  skipped_notebooks: [], error: null, mode: "reasoning", response: null,
});
const flaggedAnswer: AskResponse = {
  answer_id: "", conversation_id: "conv-a", conclusion: "结论。",
  answer: "容量下降 [k1]。内阻上升 [k2]。", grounded: false, evidence_level: "overview",
  anchors: [
    {
      key: "k1", object_id: "", object_type: "element", label: "甲", name: "甲",
      snippet: "甲摘录。", source_title: "甲文", location_label: "p. 2",
      source_id: "source-1", element_id: "element-1", notebook_id: "nb-0", verification: "source_gone",
    },
    {
      key: "k2", object_id: "", object_type: "element", label: "乙", name: "乙",
      snippet: "乙摘录。", source_title: "乙文", location_label: "p. 5",
      source_id: "source-2", element_id: "element-2", notebook_id: "nb-0",
    },
  ],
  related_knowledge: [], citations: [], llm_mode: "reasoning",
  citation_check: { outcome: "partial", checked: 2, failed: 1, changed: 0, source_gone: 1, unverifiable: 0 },
  reasoning_trace: [
    { step_type: "synthesis", summary: "写出回答", detail: {} },
    { step_type: "citation_check", summary: "已核对 2 条引用，1 条未通过", detail: { checked: 2, failed: 1 } },
  ],
};
const detail = (turns: GlobalJob[]): GlobalConversationDetail => ({
  ...conversation, turns, has_more: false, next_offset: null,
});
const NOTICE = "本次回答有部分引用未通过核对：1 条资料已删除。回答内容照常保留，带标记的引用可点开查看原因。";

beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  api.me.mockResolvedValue({ id: "user-1", ui_mode: "advanced" });
  api.notebooks.mockResolvedValue(notebooks);
  api.list.mockResolvedValue([conversation]);
  api.stream.mockRejectedValue(new Error("stream unavailable"));
});

async function expectFlaggedTurn() {
  expect(await screen.findByText(NOTICE)).toHaveClass("answer-citation-check-notice");
  expect(screen.getByText(/容量下降/)).toBeInTheDocument();
  const flagged = await screen.findByRole("button", { name: "[1] 未通过核对：资料已删除" });
  expect(flagged).toHaveClass("cite-chip-unverified");
  expect(screen.getByRole("button", { name: "[2]" }).className).toBe("cite-chip");
  fireEvent.click(flagged);
  const card = await screen.findByRole("dialog");
  expect(within(card).getByRole("note")).toHaveTextContent("未通过核对：资料已删除");
  expect(card).toHaveTextContent("甲摘录。");
  // 全局窗口给每条引用都接了「打开笔记本」；带标记的这条不渲染它。
  expect(within(card).queryByRole("link")).toBeNull();
}

test("reopening the conversation after a reload keeps the notice and the flags", async () => {
  api.detail.mockResolvedValue(detail([{ ...job("done"), answer: flaggedAnswer }]));
  render(<GlobalAskPage />);
  await expectFlaggedTurn();
});

test("the stream's terminal frame carries the notice and the flags into the window", async () => {
  api.detail.mockResolvedValue(detail([job("running")]));
  let settle!: (outcome: GlobalJobStreamOutcome) => void;
  api.stream.mockImplementation(() => new Promise<GlobalJobStreamOutcome>((resolve) => { settle = resolve; }));
  render(<GlobalAskPage />);
  await waitFor(() => expect(api.stream).toHaveBeenCalled());
  await act(async () => { settle({ kind: "final", job: { ...job("done"), answer: flaggedAnswer } }); });
  await expectFlaggedTurn();
  expect(api.poll).not.toHaveBeenCalled();
});

test("the trace shows the check step like any other step", async () => {
  api.detail.mockResolvedValue(detail([{ ...job("done"), answer: flaggedAnswer }]));
  const { container } = render(<GlobalAskPage />);
  await screen.findByText(NOTICE);
  const summary = container.querySelector<HTMLButtonElement>(".reasoning-trace-summary")!;
  expect(summary).toHaveTextContent("核对");
  expect(summary).toHaveTextContent("已核对 2 条引用，1 条未通过");
  fireEvent.click(summary);
  const list = container.querySelector(".reasoning-trace-list")!;
  const rows = within(list as HTMLElement).getAllByRole("listitem");
  expect(rows).toHaveLength(2);
  expect(rows[1]).toHaveTextContent("核对");
  expect(rows[1]).not.toHaveTextContent("处理中");
});
