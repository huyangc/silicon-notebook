import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

// codex #522 R6 P1 的回归门:分享状态加载**非 404 失败**时,当前是否已分享/水位**未知**,
// 必须**禁用**分享并给可操作错误——绝不能让 CTA 可点而发一个空 expected,那会让服务端按
// 当前最新兜底发布(可能推进隐藏分享、或在未显示任何披露的情况下公开个人记忆)。同时钉住
// 「分享状态与会话详情各自成败」:详情失败(countsError)仍可分享,与本条正交。
//
// ask-api 被 mock(四个端点),errors.ts/披露纯逻辑/FloatingModalCard 全部保持真实实现——
// 本文件钉的正是「非 404 → 禁用」这条接线,把 httpErrorStatus / 状态位判定 mock 掉就只剩壳子。

const mocks = vi.hoisted(() => ({
  getConversation: vi.fn(),
  getConversationShare: vi.fn(),
  shareConversation: vi.fn(),
  unshareConversation: vi.fn(),
}));

vi.mock("../../app/ask-api.ts", () => ({
  getConversation: mocks.getConversation,
  getConversationShare: mocks.getConversationShare,
  shareConversation: mocks.shareConversation,
  unshareConversation: mocks.unshareConversation,
}));

import { ConversationShareModal } from "../../app/conversation-share-modal.tsx";
import { humanizedError } from "../../app/errors.ts";
import { SHARE_DISCLOSURE_COUNTS_ERROR } from "../../app/conversation-share-disclosure.ts";

// 两轮问答,末轮引用一条个人记忆——分享披露算得出 memoryCount=1;末条 answer_id 是
// 「分享」将钉死的 expected_through_id。
const DETAIL = {
  id: "conv-1",
  notebook_id: "nb-1",
  title: "t",
  updated_at: "2026-01-01T00:00:02",
  turn_count: 2,
  turns: [
    {
      answer_id: "a1",
      question: "q1",
      response: { anchors: [], citations: [] },
      asked_at: "2026-01-01T00:00:00",
      created_at: "2026-01-01T00:00:00",
    },
    {
      answer_id: "a2",
      question: "q2",
      response: { anchors: [], citations: [{ memory_id: "m1", images: [] }] },
      asked_at: "2026-01-01T00:00:01",
      created_at: "2026-01-01T00:00:01",
    },
  ],
};

function renderModal() {
  return render(
    <ConversationShareModal
      notebookId="nb-1"
      conversationId="conv-1"
      title="一次问答"
      onClose={vi.fn()}
    />,
  );
}

beforeEach(() => {
  mocks.getConversation.mockReset();
  mocks.getConversationShare.mockReset();
  mocks.shareConversation.mockReset();
  mocks.unshareConversation.mockReset();
});

test("分享状态非 404 加载失败 → 禁用分享 CTA、显示错误、绝不发 POST", async () => {
  // 网络失败:无状态码 → httpErrorStatus 返回 undefined → 走非 404 分支。
  mocks.getConversationShare.mockRejectedValue(new Error("网络中断"));
  mocks.getConversation.mockResolvedValue(DETAIL); // 详情成功,但分享状态未知

  renderModal();

  const cta = await screen.findByRole("button", { name: /生成分享链接/ });
  // 未知分享状态下必须禁用——这是本条的核心。
  expect(cta).toBeDisabled();
  // 可操作错误上屏(toUserMessage 对无状态错误退回 fallback 文案)。
  expect(screen.getByText("分享状态加载失败，请重试")).toBeInTheDocument();

  // 点击被禁用的按钮不发请求;防御性 handler 也复查 shareStateError。
  fireEvent.click(cta);
  await Promise.resolve();
  expect(mocks.shareConversation).not.toHaveBeenCalled();
});

test("分享状态 404(未分享)+ 详情成功 → 正常可分享,expected 钉在末条", async () => {
  mocks.getConversationShare.mockRejectedValue(humanizedError("not shared", 404));
  mocks.getConversation.mockResolvedValue(DETAIL);
  mocks.shareConversation.mockResolvedValue({
    share_token: "tok-1",
    shared_through_at: "2026-01-01T00:00:01",
    shared_through_id: "a2",
  });

  renderModal();

  const cta = await screen.findByRole("button", { name: /生成分享链接/ });
  expect(cta).not.toBeDisabled();
  // 披露算得出(不是 countsError 兜底)——末轮的个人记忆被数出来。
  expect(
    screen.getByText(/公开页会包含 1 条你引用到的个人记忆摘录/),
  ).toBeInTheDocument();

  fireEvent.click(cta);
  // 水位钉死在弹窗据以算披露的那批 turns 的末条 answer_id(a2)。
  await waitFor(() =>
    expect(mocks.shareConversation).toHaveBeenCalledWith("nb-1", "conv-1", "a2"),
  );
});

test("详情失败 + 分享状态成功 → 仍可分享,披露退化成不带数字的兜底(countsError)", async () => {
  mocks.getConversationShare.mockRejectedValue(humanizedError("not shared", 404));
  mocks.getConversation.mockRejectedValue(new Error("详情加载失败"));

  renderModal();

  const cta = await screen.findByRole("button", { name: /生成分享链接/ });
  // 详情失败只影响披露计数,绝不拦住分享本身(与 shareStateError 正交)。
  expect(cta).not.toBeDisabled();
  // 附图与个人记忆两面缺一不可的兜底文案。
  expect(screen.getByText(SHARE_DISCLOSURE_COUNTS_ERROR)).toBeInTheDocument();
});
