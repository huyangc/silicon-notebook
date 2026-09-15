import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { NotebookCollectionSection } from "../../app/notebook-collection-section.tsx";
import type { NotebookSummary } from "../../app/workspace-model.ts";

afterEach(cleanup);

// 笔记本集合页一个分区(卡片/列表 + 分页)。接口整份返回(契约上不分页),界面
// 每页 24 个笔记本 —— 见 notebook-collection-section.tsx 的 NOTEBOOK_COLLECTION_PAGE_SIZE。
function makeEntries(count: number): Array<{ notebook: NotebookSummary; index: number; hits: never[] }> {
  return Array.from({ length: count }, (_, i) => ({
    notebook: {
      id: `nb-${i + 1}`,
      name: `笔记本${String(i + 1).padStart(2, "0")}`,
      purpose: "",
      primary_domain: "",
      status: "ready",
      counts: {},
      created_label: "",
    },
    index: i,
    hits: [],
  }));
}

function cardNames(): string[] {
  return Array.from(document.querySelectorAll(".notebook-card h2")).map((node) => node.textContent ?? "");
}

function listRowNames(): string[] {
  return Array.from(document.querySelectorAll(".notebook-list-row .notebook-list-title strong"))
    .map((node) => node.textContent ?? "");
}

function baseProps() {
  return {
    openingNotebookId: null,
    openNotebook: vi.fn(),
    openMemory: vi.fn(),
    openMenu: vi.fn(),
  };
}

test("网格视图超过一页时分页展示，翻页后显示下一批笔记本", async () => {
  const user = userEvent.setup();
  render(
    <NotebookCollectionSection
      entries={makeEntries(30)}
      viewMode="grid"
      label="我的笔记本分页"
      {...baseProps()}
    />,
  );

  expect(cardNames()).toHaveLength(24);
  expect(cardNames()[0]).toBe("笔记本01");
  const pager = screen.getByRole("navigation", { name: "我的笔记本分页" });
  expect(within(pager).getByText("1–24 / 30")).toBeInTheDocument();

  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(cardNames()).toEqual(["笔记本25", "笔记本26", "笔记本27", "笔记本28", "笔记本29", "笔记本30"]);
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("列表视图同样分页，且页大小与网格视图一致", async () => {
  const user = userEvent.setup();
  render(
    <NotebookCollectionSection
      entries={makeEntries(30)}
      viewMode="list"
      label="群组笔记本分页"
      roleText="群组成员"
      {...baseProps()}
    />,
  );

  expect(listRowNames()).toHaveLength(24);
  expect(listRowNames()[0]).toBe("笔记本01");
  const pager = screen.getByRole("navigation", { name: "群组笔记本分页" });
  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(listRowNames()[0]).toBe("笔记本25");
  expect(listRowNames()).toHaveLength(6);
});

test("一页放得下时不显示分页控件", () => {
  render(
    <NotebookCollectionSection
      entries={makeEntries(24)}
      viewMode="grid"
      label="我的笔记本分页"
      {...baseProps()}
    />,
  );
  expect(cardNames()).toHaveLength(24);
  expect(screen.queryByRole("navigation", { name: "我的笔记本分页" })).not.toBeInTheDocument();
});

test("resetKey 变化时翻页状态回到第一页", async () => {
  const user = userEvent.setup();
  const { rerender } = render(
    <NotebookCollectionSection
      entries={makeEntries(30)}
      viewMode="grid"
      label="我的笔记本分页"
      resetKey="mine"
      {...baseProps()}
    />,
  );

  await user.click(within(screen.getByRole("navigation", { name: "我的笔记本分页" })).getByRole("button", { name: "下一页" }));
  expect(cardNames()[0]).toBe("笔记本25");

  // 换一个 resetKey(等价于搜索词/筛选 tab/排序/视图模式变了):即便条数不变,
  // 也该回到第一页,不能停在上一次筛选结果翻到的页码上。
  rerender(
    <NotebookCollectionSection
      entries={makeEntries(30)}
      viewMode="grid"
      label="我的笔记本分页"
      resetKey="mine|q"
      {...baseProps()}
    />,
  );
  expect(cardNames()[0]).toBe("笔记本01");
});
