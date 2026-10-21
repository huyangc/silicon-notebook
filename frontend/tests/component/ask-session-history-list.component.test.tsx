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

// 收起来源栏后的左栏:一行一个标题(时间/轮数收进 title 提示),翻页用窄栏的 compact 控件。
test("侧栏形态一行一个标题，当前会话标出 aria-current，分页用紧凑控件", async () => {
  const user = userEvent.setup();
  const onDelete = vi.fn();
  render(
    <AskSessionHistoryList
      sessions={makeSessions(25)}
      {...baseProps()}
      conversationId="s2"
      onDelete={onDelete}
      variant="sidebar"
    />,
  );

  const rows = document.querySelectorAll(".chat-session-row");
  expect(rows).toHaveLength(20);
  expect(document.querySelector(".chat-session-card")).toBeNull();
  expect(rows[0].querySelector("small")).toBeNull();
  expect(rows[1]).toHaveClass("active");
  const current = screen.getByRole("button", { name: "会话02" });
  expect(current).toHaveAttribute("aria-current", "true");
  expect(current.getAttribute("title")).toContain("1 轮");
  expect(screen.getByRole("button", { name: "会话01" })).not.toHaveAttribute("aria-current");

  const pager = screen.getByRole("navigation", { name: "历史会话分页" });
  expect(pager).toHaveClass("compact");

  await user.click(within(rows[2] as HTMLElement).getByTitle("删除"));
  expect(onDelete).toHaveBeenCalledWith(expect.objectContaining({ id: "s3" }));
});

// 会话最后一轮挂着关联报告时,清单行带一个标记:卡片里是「关联报告」徽标(题目在悬停提示),
// 左栏一行只放得下标题,标记缩成标题前的小图标、题目并进整行的悬停提示。
test("挂着关联报告的会话在清单行上带标记，没挂的不带", () => {
  const sessions: ConversationSummary[] = [
    { ...makeSessions(1)[0], id: "linked", title: "带报告的会话", linked_report: { report_id: "r-1", title: "电池安全综述" } },
    { ...makeSessions(1)[0], id: "plain", title: "普通会话" },
  ];
  const { rerender } = render(<AskSessionHistoryList sessions={sessions} {...baseProps()} />);
  const badge = screen.getByText("关联报告");
  expect(badge).toHaveAttribute("title", "关联报告：电池安全综述");
  expect(badge.closest("button")).toHaveTextContent("带报告的会话");
  expect(screen.getByText("普通会话").closest("button")).not.toHaveTextContent("关联报告");

  rerender(<AskSessionHistoryList sessions={sessions} {...baseProps()} variant="sidebar" />);
  const linkedRow = screen.getByRole("button", { name: /带报告的会话/ });
  expect(linkedRow.getAttribute("title")).toContain("关联报告：电池安全综述");
  expect(within(linkedRow).getByLabelText("关联报告")).toBeInTheDocument();
  const plainRow = screen.getByRole("button", { name: "普通会话" });
  expect(plainRow.getAttribute("title")).not.toContain("关联报告");
  expect(within(plainRow).queryByLabelText("关联报告")).toBeNull();
});
