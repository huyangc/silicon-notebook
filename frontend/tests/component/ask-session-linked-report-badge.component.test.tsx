// 历史会话清单上的「关联报告」徽标，接上真实的 useAskSession：打开一个会话（读详情、把
// 详情投影回清单那一行）之后，徽标仍按「最后一轮挂没挂报告」显示。曾经的缺陷：投影只用
// 详情重建那一行、漏了 linked_report，刚打开的会话的徽标当场消失。
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { AskResponse, ConversationDetail, ConversationSummary } from "../../app/workspace-model";

const askApi = vi.hoisted(() => ({
  checkReportLink: vi.fn(),
  fetchAskModes: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(),
}));
vi.mock("../../app/ask-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/ask-api.ts")>()),
  ...askApi,
}));

import { AskSessionHistoryList } from "../../app/ask-session-history-list";
import { useAskSession } from "../../app/use-ask-session";

afterEach(cleanup);

const REPORT = { report_id: "r-1", title: "电池安全综述" };

function answer(conversationId: string, linked: boolean): AskResponse {
  return {
    answer_id: `answer-${conversationId}`,
    conversation_id: conversationId,
    conclusion: "done",
    answer: "done",
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [],
    llm_mode: "test",
    mode: "chunk",
    ...(linked ? { linked_report: { ...REPORT, status: "read" as const } } : {}),
  };
}

function detail(id: string, title: string, linked: boolean): ConversationDetail {
  return {
    id,
    notebook_id: "notebook-a",
    title,
    updated_at: "2026-10-21T00:00:00Z",
    turn_count: 1,
    used_reasoning: false,
    ...(linked ? { linked_report: REPORT } : {}),
    turns: [{
      answer_id: `answer-${id}`,
      question: "q",
      response: answer(id, linked),
      asked_at: "2026-10-21T00:00:00Z",
      created_at: "2026-10-21T00:00:00Z",
    }],
  };
}

const SESSIONS: ConversationSummary[] = [
  { id: "linked", title: "带报告的会话", updated_at: "2026-10-21T00:00:00Z", turn_count: 1, used_reasoning: false, linked_report: REPORT },
  { id: "plain", title: "普通会话", updated_at: "2026-10-20T00:00:00Z", turn_count: 1, used_reasoning: false },
];

let session: ReturnType<typeof useAskSession> | null = null;

function Harness() {
  session = useAskSession({
    actorId: "user-a",
    notebookId: "notebook-a",
    policy: {
      advanced: true,
      askUnavailable: false,
      scopeBlocked: false,
      kgAvailable: true,
      sourceScope: { mode: "exclude", source_ids: [] },
      baseScope: { mode: "exclude", notebook_ids: [] },
    },
    effects: { notify: vi.fn(), reportError: vi.fn(), ensureAskVisible: vi.fn() },
  });
  return (
    <AskSessionHistoryList
      sessions={session.sessions}
      conversationId={session.conversationId}
      renamingSessionId={null}
      sessionTitleDraft=""
      sessionTitleOverLimit={null}
      strictLabel="逐步推理"
      onUpdateTitleDraft={vi.fn()}
      onCommitRename={vi.fn()}
      onCancelRename={vi.fn()}
      onOpenSession={vi.fn()}
      onBeginRename={vi.fn()}
      onShare={vi.fn()}
      onDelete={vi.fn()}
    />
  );
}

function badgeOn(title: string): Element | null {
  const row = screen.getByText(title).closest("button")!;
  return row.querySelector(".chat-session-report-badge");
}

beforeEach(() => {
  for (const mock of Object.values(askApi)) mock.mockReset();
  askApi.fetchAskModes.mockResolvedValue([]);
  askApi.listConversations.mockResolvedValue(SESSIONS);
  askApi.checkReportLink.mockImplementation(async (_nb: string, body: { report_id: string }) => ({
    report_id: body.report_id,
    title: "",
    readable: true,
  }));
  askApi.getConversation.mockImplementation(async (id: string) => (
    id === "linked" ? detail("linked", "带报告的会话", true) : detail("plain", "普通会话", false)
  ));
});

async function openNotebook() {
  let owner: ReturnType<NonNullable<typeof session>["beginNotebookTransition"]> = null;
  act(() => {
    owner = session!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 1 });
  });
  await act(async () => {
    // Restoring the notebook reads the list, then opens the latest conversation's detail.
    await session!.restoreNotebook(owner!);
  });
  act(() => session!.finishNotebookTransition(owner!));
}

test("打开一个最后一轮挂着报告的会话之后，它的清单行仍带「关联报告」徽标", async () => {
  render(<Harness />);
  await openNotebook();
  expect(askApi.getConversation).toHaveBeenCalledWith("linked");
  expect(session!.conversationId).toBe("linked");
  expect(badgeOn("带报告的会话")).toHaveTextContent("关联报告");
  expect(badgeOn("带报告的会话")).toHaveAttribute("title", "关联报告：电池安全综述");

  await act(async () => {
    await session!.openSession("linked", 2);
  });
  expect(badgeOn("带报告的会话")).toHaveTextContent("关联报告");
});

test("打开一个最后一轮没挂报告的会话之后，它的清单行没有徽标", async () => {
  // 清单读得早、还带着旧徽标；打开时详情说最后一轮没挂报告，以详情为准。
  askApi.listConversations.mockResolvedValue([
    SESSIONS[0],
    { ...SESSIONS[1], linked_report: { report_id: "r-old", title: "旧报告" } },
  ]);
  render(<Harness />);
  await openNotebook();
  await act(async () => {
    await session!.openSession("plain", 2);
  });
  expect(session!.conversationId).toBe("plain");
  expect(badgeOn("普通会话")).toBeNull();
  // 别的行不受影响。
  expect(badgeOn("带报告的会话")).toHaveTextContent("关联报告");
});
