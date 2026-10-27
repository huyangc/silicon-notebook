import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";

import { bellDeletionHint, bellDeletionLabel } from "../../app/notebook-deletion-copy.ts";
import { itemSig, pendingView } from "../../app/pending-actions.ts";
import { PendingBell, type PendingItem } from "../../app/pending-center.tsx";

// 铃铛里的「即将永久删除的笔记本」:主人与在那里有记忆的成员各一条,带绝对时刻;
// 计入未读;点击交给上层,在通知内展开详情。

const MEMBER_ITEM: PendingItem = {
  type: "notebook_pending_delete",
  notebook_id: "nb-shared",
  notebook_name: "封装工艺库",
  delete_after: "2026-10-23T08:00:00+00:00",
  is_owner: false,
  memory_count: 4,
  transferable_count: 2,
};
const OWNER_ITEM: PendingItem = {
  ...MEMBER_ITEM, notebook_id: "nb-own", notebook_name: "我的旧库", is_owner: true, memory_count: 0,
};

afterEach(() => {
  window.localStorage.clear();
});

function renderBell(items: PendingItem[], onOpenItem = vi.fn()) {
  render(
    <PendingBell
      snapshot={{ count: items.length, items }}
      doneItems={[]}
      userId="u-deletion"
      onOpenItem={onOpenItem}
      onOpenDone={vi.fn()}
      onDismissDone={vi.fn()}
    />,
  );
  return onOpenItem;
}

test("删除通知成组排在最前,说清何时永久删除;成员副行说可以导出,主人副行说可以撤销", async () => {
  const user = userEvent.setup();
  renderBell([MEMBER_ITEM, OWNER_ITEM]);
  await user.click(screen.getByLabelText("待确认中心"));
  const group = screen.getByText("即将永久删除的笔记本").closest(".pending-group") as HTMLElement;
  expect(group).not.toBeNull();
  expect(within(group).getByText(bellDeletionLabel("封装工艺库", MEMBER_ITEM.delete_after!))).toBeInTheDocument();
  expect(within(group).getByText(bellDeletionHint(false, 4))).toBeInTheDocument();
  expect(within(group).getByText(bellDeletionHint(true, 0))).toBeInTheDocument();
  expect(bellDeletionLabel("封装工艺库", MEMBER_ITEM.delete_after!)).toContain("2026年10月23日");
  const groups = document.querySelectorAll(".pending-group");
  expect(groups[0]).toBe(group);
});

test("删除通知计入未读;签名随截止变化(再删一次是一条新通知)", async () => {
  const user = userEvent.setup();
  renderBell([MEMBER_ITEM]);
  expect(screen.getByText("1")).toBeInTheDocument();
  expect(pendingView([MEMBER_ITEM], [], [], []).unread).toBe(1);
  expect(itemSig(MEMBER_ITEM)).not.toBe(itemSig({ ...MEMBER_ITEM, delete_after: "2026-10-24T08:00:00+00:00" }));
  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.queryByText("1")).toBeNull();
});

test("点击删除通知把整条交给上层", async () => {
  const user = userEvent.setup();
  const onOpenItem = renderBell([MEMBER_ITEM]);
  await user.click(screen.getByLabelText("待确认中心"));
  await user.click(screen.getByText(bellDeletionLabel("封装工艺库", MEMBER_ITEM.delete_after!)));
  expect(onOpenItem).toHaveBeenCalledWith(MEMBER_ITEM);
});

test("删除详情在铃铛内展开，可返回通知、收起再打开；清空通知后仍有操作入口", async () => {
  const user = userEvent.setup();
  function Host() {
    const [detail, setDetail] = useState(false);
    return <PendingBell
      snapshot={{ count: 1, items: [OWNER_ITEM] }} doneItems={[]} userId="u-detail"
      onOpenItem={() => setDetail(true)} onOpenDone={vi.fn()} onDismissDone={vi.fn()}
      detail={{ key: detail ? 1 : null, onBack: () => setDetail(false), render: () => <p>删除详情内容</p> }}
      deletionAccess={{ hasNotices: true, onOpen: () => setDetail(true) }}
    />;
  }
  render(<Host />);
  await user.click(screen.getByLabelText("待确认中心"));
  await user.click(screen.getByText(bellDeletionLabel(OWNER_ITEM.notebook_name!, OWNER_ITEM.delete_after!)));
  expect(await screen.findByText("删除详情内容")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "返回通知" }));
  expect(screen.getByText("删除详情内容")).not.toBeVisible();
  await user.click(screen.getByRole("button", { name: "全部清空" }));
  await user.click(screen.getByRole("button", { name: "查看即将删除的笔记本" }));
  expect(screen.getByText("删除详情内容")).toBeVisible();
  await user.keyboard("{Escape}");
  expect(screen.getByText("删除详情内容")).not.toBeVisible();
  expect(screen.getByLabelText("待确认中心")).toHaveFocus();
  await user.click(screen.getByLabelText("待确认中心"));
  expect(screen.getByRole("button", { name: "查看即将删除的笔记本" })).toBeInTheDocument();
});
