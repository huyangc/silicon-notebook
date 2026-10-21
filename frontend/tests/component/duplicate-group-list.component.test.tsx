import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import {
  DuplicateGroupList,
  DuplicateResults,
  MERGE_RESULT_HOLD_MS,
  OWN_DUPLICATES_HINT,
  OWN_DUPLICATES_TITLE,
} from "../../app/duplicate-group-list.tsx";
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

// ---- DuplicateResults: the shared groups and 「我的记忆里的重复」 --------------

function group(prefix: string, size = 2): DuplicateGroup {
  return {
    object_type: "concept",
    similarity: 1,
    members: Array.from({ length: size }, (_, i) => ({
      id: `${prefix}-${i + 1}`, object_type: "concept", headline: `${prefix}-${i + 1}`, status: "approved",
    })),
  };
}

function ownSection() {
  return screen.queryByRole("region", { name: OWN_DUPLICATES_TITLE });
}

test("本人分区单独成段，放在共享组下方，带此刻的说明", () => {
  render(
    <DuplicateResults shared={[group("s")]} own={[group("m")]} canMergeShared canMergeOwn onMerge={vi.fn()} />,
  );
  const section = ownSection();
  expect(section).not.toBeNull();
  expect(within(section!).getByText(OWN_DUPLICATES_HINT)).toBeInTheDocument();
  const titles = Array.from(document.querySelectorAll(".section-title")).map((n) => n.textContent);
  expect(titles).toEqual(["重复组（规范名归一化后相同）", OWN_DUPLICATES_TITLE]);
  expect(within(section!).getByText("m-1")).toBeInTheDocument();
});

test("没有本人组时不显示本人分区", () => {
  render(<DuplicateResults shared={[group("s")]} own={[]} canMergeShared canMergeOwn onMerge={vi.fn()} />);
  expect(ownSection()).toBeNull();
});

test("只读成员只看到本人分区，没有共享组那一段", () => {
  render(<DuplicateResults shared={null} own={[group("m")]} canMergeShared={false} canMergeOwn onMerge={vi.fn()} />);
  expect(screen.queryByText("重复组（规范名归一化后相同）")).not.toBeInTheDocument();
  expect(within(ownSection()!).getByRole("button", { name: "合并到第 1 条" })).toBeEnabled();
});

test("只读成员本人也没有重复时，给出未发现重复", () => {
  render(<DuplicateResults shared={null} own={[]} canMergeShared={false} canMergeOwn onMerge={vi.fn()} />);
  expect(screen.getByText("未发现重复。")).toBeInTheDocument();
});

test("合并在途时按钮改写为合并中并禁用两段的按钮，结果显示在那一组里并按时收起", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    let finish: (value: { tone: "done"; text: string }) => void = () => {};
    const onMerge = vi.fn(() => new Promise<{ tone: "done"; text: string }>((resolve) => { finish = resolve; }));
    render(
      <DuplicateResults shared={[group("s", 3)]} own={[group("m")]} canMergeShared canMergeOwn onMerge={onMerge} />,
    );
    await user.click(screen.getAllByRole("button", { name: "合并到第 1 条" })[0]);
    expect(onMerge).toHaveBeenCalledWith("s-2", "s-1", "shared");
    expect(screen.getByRole("button", { name: "合并中…" })).toBeDisabled();
    for (const button of screen.getAllByRole("button", { name: "合并到第 1 条" })) {
      expect(button).toBeDisabled();
    }
    await act(async () => { finish({ tone: "done", text: "已合并，原条目已弃用" }); });
    const result = screen.getByRole("status");
    expect(result).toHaveTextContent("已合并，原条目已弃用");
    // in the group that was merged into, not at the top of the page
    const article = result.closest(".item") as HTMLElement | null;
    expect(article).not.toBeNull();
    expect(within(article!).getByText("s-1")).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(MERGE_RESULT_HOLD_MS + 10); });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

test("合并后那一组消失时，结果显示在该分区列表最上方；本人分区留到结果收起", async () => {
  const user = userEvent.setup();
  const onMerge = vi.fn().mockResolvedValue({ tone: "done", text: "已合并，原条目已弃用" });
  const { rerender } = render(
    <DuplicateResults shared={[]} own={[group("m")]} canMergeShared canMergeOwn onMerge={onMerge} />,
  );
  await user.click(within(ownSection()!).getByRole("button", { name: "合并到第 1 条" }));
  expect(onMerge).toHaveBeenCalledWith("m-2", "m-1", "own");
  rerender(<DuplicateResults shared={[]} own={[]} canMergeShared canMergeOwn onMerge={onMerge} />);
  const section = ownSection();
  expect(section).not.toBeNull();
  expect(within(section!).getByRole("status")).toHaveTextContent("已合并，原条目已弃用");
});

test("失败的结果(409/404 的文案)同样就地显示，用失败样式", async () => {
  const user = userEvent.setup();
  const onMerge = vi.fn().mockResolvedValue({ tone: "failed", text: "这条已不在了" });
  render(<DuplicateResults shared={[group("s", 3)]} own={null} canMergeShared canMergeOwn onMerge={onMerge} />);
  await user.click(screen.getAllByRole("button", { name: "合并到第 1 条" })[0]);
  const result = await screen.findByRole("status");
  expect(result).toHaveTextContent("这条已不在了");
  expect(result).toHaveClass("dup-merge-result-failed");
});
