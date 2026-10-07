// 通用提示弹窗(「分析」面板)的动作按钮区。会被服务端拒绝的写动作(`run`,例如
// 「设为公共知识库」)按下后弹窗不关、按钮置灰;被拒时原因落在这个按钮旁边,
// 成功才关弹窗。原有的 `action` 形态保持「先关弹窗再执行」。
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { humanizedError } from "../../app/errors";
import { InfoModalActions } from "../../app/info-modal-actions";

const PUBLISH_REFUSED =
  "这本笔记本里还有成员的个人记忆，不能发布为公共知识库；请先让成员转移或删除自己的记忆";

test("发布被拒：原因显示在「设为公共知识库」按钮旁边，弹窗不关，按钮恢复可点", async () => {
  const user = userEvent.setup();
  const requestClose = vi.fn(() => true);
  let reject: (error: unknown) => void = () => {};
  const run = vi.fn(() => new Promise<void>((_resolve, rej) => { reject = rej; }));
  render(
    <InfoModalActions
      actions={[
        { label: "内容审核", desc: "审核待收录的内容", action: vi.fn() },
        { label: "设为公共知识库", desc: "把当前笔记本设为公共知识库", run },
      ]}
      requestClose={requestClose}
      reportDetachedError={vi.fn()}
    />,
  );

  await user.click(screen.getByRole("button", { name: "设为公共知识库" }));
  // Pressed: visibly busy, every action held until the result arrives.
  const busy = screen.getByRole("button", { name: "设为公共知识库…" });
  expect(busy).toBeDisabled();
  expect(busy).toHaveAttribute("aria-busy", "true");
  // Every other action is held too: pressing one would close the panel and
  // lose this action's result.
  expect(screen.getByRole("button", { name: "内容审核" })).toBeDisabled();
  // A browser moves focus off a button that becomes disabled; jsdom does not,
  // so move it away here the way the browser would.
  const elsewhere = document.createElement("input");
  document.body.appendChild(elsewhere);
  elsewhere.focus();
  elsewhere.remove();
  expect(screen.getByRole("button", { name: "设为公共知识库…" })).not.toHaveFocus();

  reject(humanizedError(PUBLISH_REFUSED, 409));
  const status = await screen.findByRole("status");
  expect(status).toHaveTextContent(PUBLISH_REFUSED);
  // The reason sits in the same row as the button that was pressed.
  expect(status.closest(".info-action-desc-cell")?.previousElementSibling).toBe(
    screen.getByRole("button", { name: "设为公共知识库" }),
  );
  expect(requestClose).not.toHaveBeenCalled();
  const button = screen.getByRole("button", { name: "设为公共知识库" });
  expect(button).toBeEnabled();
  // Focus comes back to the button that was pressed (it was disabled while
  // pending, which drops focus to the page).
  await waitFor(() => expect(button).toHaveFocus());
  expect(screen.getByRole("button", { name: "内容审核" })).toBeEnabled();
});

test("等待中弹窗被关掉：之后到达的拒绝原因交给页面级错误呈现，不静默丢掉", async () => {
  const user = userEvent.setup();
  const reportDetachedError = vi.fn();
  let reject: (error: unknown) => void = () => {};
  const run = vi.fn(() => new Promise<void>((_resolve, rej) => { reject = rej; }));
  const { unmount } = render(
    <InfoModalActions
      actions={[{ label: "设为公共知识库", desc: "发布", run }]}
      requestClose={() => true}
      reportDetachedError={reportDetachedError}
    />,
  );
  await user.click(screen.getByRole("button", { name: "设为公共知识库" }));
  unmount();
  const refusal = humanizedError(PUBLISH_REFUSED, 409);
  reject(refusal);
  await waitFor(() => expect(reportDetachedError).toHaveBeenCalledWith(refusal));
});

test("发布成功：关弹窗；再按时旧的失败原因先清掉", async () => {
  const user = userEvent.setup();
  const requestClose = vi.fn(() => true);
  const run = vi.fn()
    .mockRejectedValueOnce(humanizedError(PUBLISH_REFUSED, 409))
    .mockResolvedValueOnce(undefined);
  render(
    <InfoModalActions
      actions={[{ label: "设为公共知识库", desc: "发布", run }]}
      requestClose={requestClose}
      reportDetachedError={vi.fn()}
    />,
  );

  await user.click(screen.getByRole("button", { name: "设为公共知识库" }));
  expect(await screen.findByRole("status")).toHaveTextContent(PUBLISH_REFUSED);
  await user.click(screen.getByRole("button", { name: "设为公共知识库" }));
  await waitFor(() => expect(requestClose).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("status")).toBeNull();
});

test("原有 action 形态：先关弹窗再执行，协调器拒绝关闭时不执行", async () => {
  const user = userEvent.setup();
  const action = vi.fn();
  const requestClose = vi.fn(() => true);
  const { rerender } = render(
    <InfoModalActions actions={[{ label: "关系审核队列", action }]} requestClose={requestClose} reportDetachedError={vi.fn()} />,
  );
  await user.click(screen.getByRole("button", { name: "关系审核队列" }));
  expect(requestClose).toHaveBeenCalledTimes(1);
  expect(action).toHaveBeenCalledTimes(1);

  const refuse = vi.fn(() => false);
  rerender(<InfoModalActions actions={[{ label: "关系审核队列", action }]} requestClose={refuse} reportDetachedError={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "关系审核队列" }));
  expect(action).toHaveBeenCalledTimes(1);
});
