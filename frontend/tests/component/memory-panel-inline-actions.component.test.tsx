// 记忆页两句「说到动作」的提示：动作是可点的字，当前用户做不了的不说。
//  - 移动时「正在公共知识库审批中」：提案只有管理员能在「内容审核」里处理；
//  - 「贡献到公共知识库」因没挂公共知识库而禁用：按钮旁说由谁来挂。
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { MemoryPanel, PromotionBlockedNotice } from "../../app/memory-panel";
import type { MemoryRecord, PaginatedMemories } from "../../app/workspace-model";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("管理员：「内容审核」是可点的字", async () => {
  const onOpenPromotionQueue = vi.fn();
  render(<PromotionBlockedNotice count={2} onOpenPromotionQueue={onOpenPromotionQueue} />);
  const notice = screen.getByRole("status");
  expect(notice).toHaveTextContent("2 条正在公共知识库审批中，暂不能移动；请先在内容审核里处理这些提案。");
  const action = within(notice).getByRole("button", { name: "内容审核" });
  expect(action).toHaveClass("link-button", "inline-action");
  await userEvent.setup().click(action);
  expect(onOpenPromotionQueue).toHaveBeenCalledOnce();
});

test("非管理员处理不了提案：只说等审核完成，不指去一个看不到它的地方", () => {
  render(<PromotionBlockedNotice count={1} />);
  const notice = screen.getByRole("status");
  expect(notice).toHaveTextContent("1 条正在公共知识库审批中，暂不能移动；等管理员审核完成后再移动。");
  expect(notice).not.toHaveTextContent("待确认中心");
  expect(within(notice).queryByRole("button")).toBeNull();
});

const memory: MemoryRecord = {
  id: "memory-1",
  notebook_id: "notebook-1",
  created_by: "user-1",
  origin: "ask_answer",
  status: "confirmed",
  promotion_state: "none",
  title: "一条记忆",
  content_md: "内容",
  tags: [],
  embedding_status: "ready",
  embedding_error: "",
  created_at: "2026-07-20T00:00:00Z",
  updated_at: "2026-07-20T00:00:00Z",
  provenance: {},
};

const page: PaginatedMemories = {
  items: [memory],
  total_count: 1,
  offset: 0,
  limit: 20,
  owner_total_count: 1,
  owner_pending_count: 0,
  notebook_options: [{ notebook_id: "notebook-1", name: "笔记本", memory_count: 1, pending_count: 0 }],
};

function renderPanel(props: Partial<Parameters<typeof MemoryPanel>[0]>) {
  vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(new Response(
    JSON.stringify(url.includes("/memories?") ? page : []),
    { status: 200 },
  ))));
  return render(
    <MemoryPanel
      scope="notebook"
      notebookId="notebook-1"
      bases={[]}
      sessionSignal={new AbortController().signal}
      {...props}
    />,
  );
}

test("没挂公共知识库：库主在禁用的按钮旁看到可点的「挂载公共知识库」", async () => {
  const onMountBases = vi.fn();
  renderPanel({ mountBasesAccess: () => "self", onMountBases });
  const promote = await screen.findByRole("button", { name: "贡献到公共知识库" });
  expect(promote).toBeDisabled();
  await userEvent.setup().click(screen.getByRole("button", { name: "挂载公共知识库" }));
  expect(onMountBases).toHaveBeenCalledWith("notebook-1");
});

test("没挂公共知识库：非库主只被告知要库主先挂", async () => {
  renderPanel({ mountBasesAccess: () => "owner", onMountBases: vi.fn() });
  await screen.findByRole("button", { name: "贡献到公共知识库" });
  expect(screen.getByText("需库主先挂载一个公共知识库")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "挂载公共知识库" })).toBeNull();
});

test("挂了公共知识库：按钮可点，旁边不多一句", async () => {
  renderPanel({
    bases: [{ id: "base-1", name: "公共库", tier: "base", active: true, inactive_reason: "" }],
    mountBasesAccess: () => "self",
    onMountBases: vi.fn(),
  });
  expect(await screen.findByRole("button", { name: "贡献到公共知识库" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "挂载公共知识库" })).toBeNull();
});
