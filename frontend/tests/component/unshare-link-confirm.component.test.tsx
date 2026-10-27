// 「取消链接分享」的确认弹窗:分享弹窗与「已分享」总览两个入口共用 unshareLinkConfirmation,
// 由通用提示弹窗的动作区 InfoModalActions 渲染(与 page.tsx 里同一个组件)。
// page.tsx 整体不可渲染;两个入口确实接到这里,由 tests/guards/unshare-link-confirm-guard 钉。
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { InfoModalActions } from "../../app/info-modal-actions";
import { unshareLinkConfirmation } from "../../app/unshare-link-confirm";

function renderConfirmation(mentionGroups: boolean) {
  const onConfirm = vi.fn();
  const requestClose = vi.fn(() => true);
  const modal = unshareLinkConfirmation({ notebookName: "器件手册", mentionGroups, onConfirm });
  render(
    <div>
      <h2>{modal.title}</h2>
      <p>{modal.message}</p>
      <InfoModalActions actions={modal.actions} requestClose={requestClose} reportDetachedError={vi.fn()} />
    </div>,
  );
  return { modal, onConfirm, requestClose };
}

test("「保留链接」只关掉确认,不取消分享", async () => {
  const user = userEvent.setup();
  const { onConfirm, requestClose } = renderConfirmation(true);
  await user.click(screen.getByRole("button", { name: "保留链接" }));
  expect(requestClose).toHaveBeenCalledTimes(1);
  expect(onConfirm).not.toHaveBeenCalled();
});

test("确认之后才取消分享,只执行一次", async () => {
  const user = userEvent.setup();
  const { onConfirm, requestClose } = renderConfirmation(true);
  expect(onConfirm).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "取消链接分享" }));
  expect(requestClose).toHaveBeenCalledTimes(1);
  expect(onConfirm).toHaveBeenCalledTimes(1);
});

test("后果写清:链接失效、成员移除、检索心得与 Agent 记录清空、个人记忆不删、旧链接不恢复", () => {
  const { modal } = renderConfirmation(false);
  expect(screen.getByRole("heading", { name: "取消「器件手册」的链接分享" })).toBeInTheDocument();
  expect(modal.message).toBe(
    "链接立即失效，拿到旧链接的人不能再打开；已通过这条链接加入的只读成员会被一并移除。"
      + "他们在这本笔记本里的检索心得和 Agent 记录会被清空，个人记忆不会删除。"
      + "重新开启会生成另一条链接，旧链接不会恢复。",
  );
});

test("入口在群组共享旁时补一句群组不受影响", () => {
  const { modal } = renderConfirmation(true);
  expect(modal.message.endsWith("共享给群组的部分不受影响。")).toBe(true);
});
