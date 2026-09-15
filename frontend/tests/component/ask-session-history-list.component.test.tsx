import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { AskSessionHistoryList } from "../../app/ask-session-history-list.tsx";
import type { ConversationSummary } from "../../app/workspace-model.ts";

afterEach(cleanup);

// Ask 会话管理弹窗的历史会话清单。接口整份返回(契约上不分页),界面每页 20 条 ——
// 见 ask-session-history-list.tsx 的 ASK_SESSION_HISTORY_PAGE_SIZE。
function makeSessions(count: number): ConversationSummary[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `s${i + 1}`,
    title: `会话${String(i + 1).padStart(2, "0")}`,
    updated_at: new Date(2026, 0, 1).toISOString(),
    turn_count: 1,
  }));
}

function sessionTitles(): string[] {
  return Array.from(document.querySelectorAll(".chat-session-card .chat-session-card-main > span"))
    .map((node) => node.textContent ?? "");
}

function baseProps() {
  return {
    conversationId: null,
    renamingSessionId: null,
    sessionTitleDraft: "",
    sessionTitleOverLimit: null,
    strictLabel: "严谨问答",
    onUpdateTitleDraft: vi.fn(),
    onCommitRename: vi.fn(),
    onCancelRename: vi.fn(),
    onOpenSession: vi.fn(),
    onBeginRename: vi.fn(),
    onShare: vi.fn(),
    onDelete: vi.fn(),
  };
}

test("历史会话超过一页时分页展示，翻页后显示下一批会话", async () => {
  const user = userEvent.setup();
  render(<AskSessionHistoryList sessions={makeSessions(25)} {...baseProps()} />);

  expect(sessionTitles()).toHaveLength(20);
  expect(sessionTitles()[0]).toBe("会话01");
  const pager = screen.getByRole("navigation", { name: "历史会话分页" });
  expect(within(pager).getByText("1–20 / 25")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(sessionTitles()).toEqual([
    "会话21", "会话22", "会话23", "会话24", "会话25",
  ]);
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("一页放得下时不显示分页控件", () => {
  render(<AskSessionHistoryList sessions={makeSessions(20)} {...baseProps()} />);
  expect(sessionTitles()).toHaveLength(20);
  expect(screen.queryByRole("navigation", { name: "历史会话分页" })).not.toBeInTheDocument();
});

test("清单为空时显示空态，不渲染分页", () => {
  render(<AskSessionHistoryList sessions={[]} {...baseProps()} />);
  expect(screen.getByText("还没有历史会话。")).toBeInTheDocument();
  expect(screen.queryByRole("navigation", { name: "历史会话分页" })).not.toBeInTheDocument();
});

test("点击某条会话调用 onOpenSession，携带对应的会话 id", async () => {
  const user = userEvent.setup();
  const onOpenSession = vi.fn();
  render(<AskSessionHistoryList sessions={makeSessions(3)} {...baseProps()} onOpenSession={onOpenSession} />);

  await user.click(screen.getByText("会话02"));
  expect(onOpenSession).toHaveBeenCalledWith("s2");
});
