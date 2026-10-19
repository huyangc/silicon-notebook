// 全局问答：某个参与笔记本被删除之后（产品裁决 2026-10-19），这一轮在留存期内对所有者
// 照常可读——正文保留，来自已删笔记本的引用显示「来源已删除」且点不开，名下的附图一张
// 都不去取；其余引用照常可点。这一轮下方说明它保留到什么时候。
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import GlobalAskPage from "../../app/ask/page.tsx";
import {
  normalizeGlobalJob,
  type GlobalConversation, type GlobalConversationDetail, type GlobalJob,
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
  id: "conv-a", title: "对话", created_at: "2026-10-19T01:00:00Z", updated_at: "2026-10-19T01:00:00Z",
  notebook_scope: { mode: "all" }, submitted_via: "web",
};
const answer: AskResponse = {
  answer_id: "job-a", conversation_id: "conv-a", conclusion: "结论。",
  answer: "容量下降 [k1]。内阻上升 [k2]。", grounded: true, evidence_level: "grounded",
  anchors: [
    {
      key: "k1", object_id: "", object_type: "element", label: "甲", name: "甲",
      snippet: "甲摘录。", source_title: "甲文", location_label: "p. 2",
      source_id: "source-1", element_id: "element-1", notebook_id: "nb-0",
    },
    {
      key: "k2", object_id: "", object_type: "element", label: "乙", name: "乙",
      snippet: "乙摘录。", source_title: "乙文", location_label: "p. 5",
      source_id: "source-2", element_id: "element-2", notebook_id: "nb-gone",
      images: [{ element_id: "img-2", asset_id: "asset-2", caption: "乙图" }],
    },
  ],
  related_knowledge: [], citations: [], llm_mode: "reasoning",
};
const retainedTurn: GlobalJob = {
  job_id: "job-a", conversation_id: "conv-a", status: "done", question: "低温会怎样？",
  created_at: "2026-10-19T01:00:00Z", notebook_scope: { mode: "all" },
  resolved_notebook_ids: ["nb-0", "nb-gone"], searched_notebook_ids: ["nb-0", "nb-gone"],
  cited_notebook_ids: ["nb-0", "nb-gone"], skipped_notebooks: [], error: null, mode: "reasoning",
  response: null, answer, retained_until: "2027-04-17T01:00:00+00:00", deleted_notebook_ids: ["nb-gone"],
};
const detail = (turns: GlobalJob[]): GlobalConversationDetail => ({
  ...conversation, turns: turns.map(normalizeGlobalJob), has_more: false, next_offset: null,
});

const fetchSpy = vi.fn();

beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState(null, "", "/ask?conversation_id=conv-a");
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  api.me.mockResolvedValue({ id: "user-1", ui_mode: "advanced" });
  api.notebooks.mockResolvedValue(notebooks);
  api.list.mockResolvedValue([conversation]);
  api.stream.mockRejectedValue(new Error("stream unavailable"));
  fetchSpy.mockImplementation(async () => new Response(new Blob(["x"]), { status: 200 }));
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("a retained turn keeps its body and degrades only the deleted library's citation", async () => {
  api.detail.mockResolvedValue(detail([retainedTurn]));
  const { container } = render(<GlobalAskPage />);

  expect(await screen.findByText(/容量下降/)).toBeInTheDocument();
  // The deleted library's marker is not a button: it says so, and it cannot open.
  const deleted = container.querySelector(".cite-chip-deleted")!;
  expect(deleted).toHaveTextContent("[2]来源已删除");
  expect(deleted.tagName).toBe("SPAN");
  expect(screen.queryByRole("button", { name: /\[2\]/ })).toBeNull();
  fireEvent.click(deleted);
  expect(screen.queryByRole("dialog")).toBeNull();
  // The live library's marker still opens its card.
  fireEvent.click(screen.getByRole("button", { name: "[1]" }));
  const card = await screen.findByRole("dialog");
  expect(card).toHaveTextContent("甲摘录。");
  expect(within(card).queryByText("来源已删除")).toBeNull();
  // Nothing was ever requested from the deleted library (its inline image included).
  for (const [url] of fetchSpy.mock.calls) {
    expect(String(url)).not.toContain("nb-gone");
  }
  expect(container.querySelector('[alt="乙图"]')).toBeNull();
  // The turn says how long it stays.
  expect(container.querySelector(".global-retention-notice"))
    .toHaveTextContent(/^本轮用到的笔记本已被删除，这一轮问答保留到 .+，之后自动清除。$/);
});

test("a turn none of whose libraries was deleted shows no retention notice", async () => {
  const plain: GlobalJob = {
    ...retainedTurn, resolved_notebook_ids: ["nb-0"], searched_notebook_ids: ["nb-0"],
    cited_notebook_ids: ["nb-0"], retained_until: "", deleted_notebook_ids: [],
    answer: { ...answer, answer: "容量下降 [k1]。", anchors: [answer.anchors[0]] },
  };
  api.detail.mockResolvedValue(detail([plain]));
  const { container } = render(<GlobalAskPage />);
  expect(await screen.findByRole("button", { name: "[1]" })).toBeInTheDocument();
  expect(container.querySelector(".global-retention-notice")).toBeNull();
  expect(container.querySelector(".cite-chip-deleted")).toBeNull();
});
