import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { DuplicateGroupList } from "../../app/duplicate-group-list.tsx";
import type { DuplicateGroup } from "../../app/workspace-model.ts";

afterEach(cleanup);

// 知识浏览器「查重」结果里的重复组清单。接口整份返回(契约上不分页),界面每页
// 20 组 —— 见 duplicate-group-list.tsx 的 DUPLICATE_GROUP_PAGE_SIZE。
function makeDuplicates(count: number): DuplicateGroup[] {
  return Array.from({ length: count }, (_, i) => ({
    object_type: "concept",
    similarity: 0.9,
    members: [
      { id: `g${i + 1}-a`, object_type: "concept", headline: `重复组${String(i + 1).padStart(2, "0")}-主条`, status: "approved" },
      { id: `g${i + 1}-b`, object_type: "concept", headline: `重复组${String(i + 1).padStart(2, "0")}-副条`, status: "approved" },
    ],
  }));
}

function groupHeadlines(): string[] {
  // 每组只取第一条(主条)的文案来断言组的身份与顺序。
  return Array.from(document.querySelectorAll(".item")).map(
    (node) => node.querySelector(".dup-member span")?.textContent ?? "",
  );
}

test("重复组超过一页时分页展示，翻页后显示下一批重复组", async () => {
  const user = userEvent.setup();
  render(<DuplicateGroupList duplicates={makeDuplicates(25)} mergingId={null} onMerge={vi.fn()} />);

  expect(document.querySelectorAll(".item")).toHaveLength(20);
  expect(groupHeadlines()[0]).toContain("重复组01-主条");
  const pager = screen.getByRole("navigation", { name: "重复组分页" });
  expect(within(pager).getByText("1–20 / 25")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(document.querySelectorAll(".item")).toHaveLength(5);
  expect(groupHeadlines()[0]).toContain("重复组21-主条");
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("一页放得下时不显示分页控件", () => {
  render(<DuplicateGroupList duplicates={makeDuplicates(20)} mergingId={null} onMerge={vi.fn()} />);
  expect(document.querySelectorAll(".item")).toHaveLength(20);
  expect(screen.queryByRole("navigation", { name: "重复组分页" })).not.toBeInTheDocument();
});

test("resetKey 变化时翻页状态回到第一页", async () => {
  const user = userEvent.setup();
  const { rerender } = render(
    <DuplicateGroupList duplicates={makeDuplicates(25)} mergingId={null} onMerge={vi.fn()} resetKey="concept" />,
  );
  await user.click(within(screen.getByRole("navigation", { name: "重复组分页" })).getByRole("button", { name: "下一页" }));
  expect(groupHeadlines()[0]).toContain("重复组21-主条");

  // resetKey 换成另一个知识类型(kind):即便这份 duplicates 数组恰好没换引用,
  // 也该回到第一页,不能停在上一个类型翻到的页码上。
  rerender(
    <DuplicateGroupList duplicates={makeDuplicates(25)} mergingId={null} onMerge={vi.fn()} resetKey="claim" />,
  );
  expect(groupHeadlines()[0]).toContain("重复组01-主条");
});

test("合并按钮不出现在每组第一条上，点击第二条调用 onMerge(第二条 id, 第一条 id)", async () => {
  const user = userEvent.setup();
  const onMerge = vi.fn();
  render(<DuplicateGroupList duplicates={makeDuplicates(1)} mergingId={null} onMerge={onMerge} />);

  const buttons = screen.getAllByRole("button", { name: "合并到第 1 条" });
  expect(buttons).toHaveLength(1);
  await user.click(buttons[0]);
  expect(onMerge).toHaveBeenCalledWith("g1-b", "g1-a");
});

test("只读模式不渲染合并按钮", () => {
  render(<DuplicateGroupList duplicates={makeDuplicates(1)} readOnly mergingId={null} onMerge={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "合并到第 1 条" })).not.toBeInTheDocument();
});
