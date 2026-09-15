// Knowhow 表卡片清单分页：接口一次性整份返回（GET /notebooks/{id}/knowhow 不分页），
// 界面每页 24 张。行/矩阵网格本身不在这个改动范围内，见 knowhow-panel.tsx
// KnowhowTableList 头注释。
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/knowhow-model.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-model.ts")>();
  return {
    ...actual,
    fetchKnowhowTables: vi.fn(),
    fetchKnowhowTable: vi.fn(),
  };
});

import { fetchKnowhowTables, type KnowhowTableSummary } from "../../app/knowhow-model.ts";
import { KnowhowPanel } from "../../app/knowhow-panel.tsx";

// 前 26 张表待同步(projectionPending>0)，后 4 张健康——用来验证切换「状态」筛选后
// 分页真的按新筛选结果的第一页展示，而不是停在切筛选前翻到的那一页。
function tables(count: number): KnowhowTableSummary[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `t-${String(index + 1).padStart(2, "0")}`,
    title: `表${String(index + 1).padStart(2, "0")}`,
    description: "",
    rowCount: 1,
    projectionPending: index < 26 ? 1 : 0,
    projectionFailed: 0,
    staleCodeCount: 0,
    lastActivityAt: "2026-08-01T00:00:00Z",
  }));
}

function openCardNames(): string[] {
  return screen.getAllByRole("button", { name: /^打开表格：/ }).map((button) => button.getAttribute("aria-label") ?? "");
}

beforeEach(() => {
  vi.mocked(fetchKnowhowTables).mockResolvedValue(tables(30));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("Knowhow 表超过一页时分页展示，翻页后显示下一批表", async () => {
  render(
    <KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit={false} onClose={() => undefined} />,
  );

  await screen.findByRole("button", { name: "打开表格：表01" });
  expect(openCardNames()).toHaveLength(24);
  const pager = screen.getByRole("navigation", { name: "Knowhow 表分页" });
  expect(within(pager).getByText("1–24 / 30")).toBeInTheDocument();

  const user = userEvent.setup();
  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(openCardNames()).toHaveLength(6);
  expect(screen.getByRole("button", { name: "打开表格：表25" })).toBeInTheDocument();
  expect(within(pager).getByRole("button", { name: "下一页" })).toBeDisabled();
});

test("一页放得下时不显示 Knowhow 表分页控件", async () => {
  vi.mocked(fetchKnowhowTables).mockResolvedValue(tables(10));
  render(
    <KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit={false} onClose={() => undefined} />,
  );

  await screen.findByRole("button", { name: "打开表格：表01" });
  expect(openCardNames()).toHaveLength(10);
  expect(screen.queryByRole("navigation", { name: "Knowhow 表分页" })).not.toBeInTheDocument();
});

test("切换状态筛选后分页回到第一页，而不是停在切筛选前翻到的那一页", async () => {
  const user = userEvent.setup();
  render(
    <KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit={false} onClose={() => undefined} />,
  );

  await screen.findByRole("button", { name: "打开表格：表01" });
  const pager = screen.getByRole("navigation", { name: "Knowhow 表分页" });
  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(screen.getByRole("button", { name: "打开表格：表25" })).toBeInTheDocument();

  // 切到「待同步」：筛选后还剩 26 张（表01–表26），仍然超过一页；如果分页状态没有
  // 随筛选重置，这里会保持在第 2 页，第一眼看到的是筛选后清单的尾巴而不是开头。
  await user.selectOptions(screen.getByLabelText("状态"), "projection_pending");
  await waitFor(() => expect(screen.getByRole("button", { name: "打开表格：表01" })).toBeInTheDocument());
  expect(openCardNames()).toHaveLength(24);
  const refreshedPager = screen.getByRole("navigation", { name: "Knowhow 表分页" });
  expect(within(refreshedPager).getByText("1–24 / 26")).toBeInTheDocument();
});
