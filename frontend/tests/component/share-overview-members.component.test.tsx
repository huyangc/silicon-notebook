import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";

import { ShareOverviewMembers } from "../../app/share-overview-members.tsx";

afterEach(cleanup);

// 「已分享」弹窗里一本笔记本的只读成员名单。接口整份返回(契约上不分页),界面
// 每页 20 人 —— 见 share-overview-members.tsx 的 SHARE_OVERVIEW_MEMBERS_PAGE_SIZE。
function makeMembers(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    username: `user${String(i + 1).padStart(2, "0")}`,
    added_at: new Date(2026, 0, 1).toISOString(),
  }));
}

function chipNames(): string[] {
  return Array.from(document.querySelectorAll(".share-member-chip")).map((node) => node.textContent ?? "");
}

test("只读成员超过一页时分页展示，翻页后显示下一批成员", async () => {
  const user = userEvent.setup();
  render(<ShareOverviewMembers members={makeMembers(25)} notebookName="先进封装工艺" />);

  expect(chipNames()).toHaveLength(20);
  expect(chipNames()[0]).toBe("user01");
  const pager = screen.getByRole("navigation", { name: "《先进封装工艺》的成员分页" });
  expect(within(pager).getByText("1–20 / 25")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(chipNames()).toEqual(["user21", "user22", "user23", "user24", "user25"]);
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("一页放得下时不显示分页控件", () => {
  render(<ShareOverviewMembers members={makeMembers(20)} notebookName="先进封装工艺" />);
  expect(chipNames()).toHaveLength(20);
  expect(screen.queryByRole("navigation", { name: "《先进封装工艺》的成员分页" })).not.toBeInTheDocument();
});

test("分页标签带上笔记本名字，区分同时展开的多本笔记本", () => {
  render(<ShareOverviewMembers members={makeMembers(25)} notebookName="材料数据库" />);
  expect(screen.getByRole("navigation", { name: "《材料数据库》的成员分页" })).toBeInTheDocument();
});
