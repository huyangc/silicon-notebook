import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { ContentOverviewCards } from "./content-overview-cards";
import type { NotebookContentOverview } from "./workspace-model";

const overview: NotebookContentOverview = {
  memory: {
    total: 4,
    confirmed: 2,
    candidate: 2,
    recent: [
      { id: "m1", title: "Stable memory", status: "confirmed", updated_at: "2026-07-21T08:00:00Z" },
      { id: "m2", title: "Candidate memory", status: "candidate", updated_at: "2026-07-20T08:00:00Z" },
      { id: "m3", title: "Third memory", status: "confirmed", updated_at: "2026-07-19T08:00:00Z" },
      { id: "m4", title: "Not shown memory", status: "candidate", updated_at: "2026-07-18T08:00:00Z" },
    ],
  },
  knowhow: {
    table_count: 3,
    row_count: 12,
    projection_pending: 1,
    projection_failed: 2,
    stale_code_count: 3,
    recent_tables: [
      { id: "t1", title: "Bring-up", row_count: 4, last_activity_at: "2026-07-21T08:00:00Z" },
      { id: "t2", title: "Validation", row_count: 3, last_activity_at: "2026-07-20T08:00:00Z" },
      { id: "t3", title: "Release", row_count: 5, last_activity_at: "2026-07-19T08:00:00Z" },
      { id: "t4", title: "Not shown table", row_count: 1, last_activity_at: "2026-07-18T08:00:00Z" },
    ],
  },
};

const emptyOverview: NotebookContentOverview = {
  memory: { total: 0, confirmed: 0, candidate: 0, recent: [] },
  knowhow: {
    table_count: 0,
    row_count: 0,
    projection_pending: 0,
    projection_failed: 0,
    stale_code_count: 0,
    recent_tables: [],
  },
};

function renderCards(overrides: Partial<React.ComponentProps<typeof ContentOverviewCards>> = {}) {
  const onOpenMemory = vi.fn();
  const onOpenKnowhow = vi.fn();
  const { container } = render(
    <ContentOverviewCards
      overview={overview}
      loading={false}
      error=""
      readOnly={false}
      onOpenMemory={onOpenMemory}
      onOpenKnowhow={onOpenKnowhow}
      {...overrides}
    />,
  );
  return { onOpenMemory, onOpenKnowhow, container };
}

test("renders content asset metrics, recent items, and navigation callbacks", () => {
  const { onOpenMemory, onOpenKnowhow, container } = renderCards();

  expect(screen.getByRole("heading", { name: "内容资产" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Memory" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Knowhow" })).toBeInTheDocument();

  const totals = container.querySelectorAll(".content-overview-total");
  expect(totals[0].textContent).toBe("4 条");
  expect(totals[1].textContent).toContain("3 张表");
  expect(totals[1].textContent).toContain("12 行");

  expect(screen.getByText("已同步 0 张")).toBeInTheDocument();
  expect(screen.getByText("同步失败 2 张")).toBeInTheDocument();
  expect(screen.getByText("代码过期 3 格")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "打开记忆 Not shown memory" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "打开 Knowhow 表 Not shown table" })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "查看全部记忆" }));
  expect(onOpenMemory).toHaveBeenCalledWith(null, null);
  fireEvent.click(screen.getByRole("button", { name: "查看 2 条已确认记忆" }));
  expect(onOpenMemory).toHaveBeenCalledWith("confirmed", null);
  fireEvent.click(screen.getByRole("button", { name: "查看 2 条待确认记忆" }));
  expect(onOpenMemory).toHaveBeenCalledWith("candidate", null);
  fireEvent.click(screen.getByRole("button", { name: "打开记忆 Stable memory" }));
  expect(onOpenMemory).toHaveBeenCalledWith(null, "m1");
  fireEvent.click(screen.getByRole("button", { name: "查看全部 Knowhow 表" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("all", null);
  fireEvent.click(screen.getByRole("button", { name: "查看 1 张待同步表" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("projection_pending", null);
  fireEvent.click(screen.getByRole("button", { name: "查看 2 张同步失败表" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("projection_failed", null);
  fireEvent.click(screen.getByRole("button", { name: "查看含 3 个过期代码单元格的表" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("stale_code", null);
  fireEvent.click(screen.getByRole("button", { name: "打开 Knowhow 表 Bring-up" }));
  expect(onOpenKnowhow).toHaveBeenCalledWith("all", "t1");
});

test("renders proportion bars with accessible labels and normalized widths", () => {
  renderCards();

  const memoryBar = screen.getByRole("img", { name: "已确认 2 条，待确认 2 条" });
  const memorySegments = memoryBar.querySelectorAll("span");
  expect(memorySegments).toHaveLength(2);
  expect(parseFloat(memorySegments[0].style.width)).toBeCloseTo(50, 1);
  expect(parseFloat(memorySegments[1].style.width)).toBeCloseTo(50, 1);

  const knowhowBar = screen.getByRole("img", {
    name: "已同步 0 张，待同步 1 张，同步失败 2 张",
  });
  const knowhowSegments = knowhowBar.querySelectorAll("span");
  expect(knowhowSegments).toHaveLength(3);
  expect(parseFloat(knowhowSegments[0].style.width)).toBeCloseTo(0, 1);
  expect(parseFloat(knowhowSegments[1].style.width)).toBeCloseTo(33.3, 1);
  expect(parseFloat(knowhowSegments[2].style.width)).toBeCloseTo(66.7, 1);
});

test("normalizes segment widths when parts exceed the total", () => {
  renderCards({
    overview: {
      ...overview,
      memory: { total: 1, confirmed: 2, candidate: 2, recent: [] },
      knowhow: {
        ...overview.knowhow,
        table_count: 3,
        projection_pending: 2,
        projection_failed: 2,
      },
    },
  });

  const memoryBar = screen.getByRole("img", { name: "已确认 2 条，待确认 2 条" });
  const memoryWidths = [...memoryBar.querySelectorAll("span")].map((seg) =>
    parseFloat(seg.style.width),
  );
  expect(memoryWidths.reduce((acc, w) => acc + w, 0)).toBeCloseTo(100, 1);

  const knowhowBar = screen.getByRole("img", {
    name: "已同步 0 张，待同步 2 张，同步失败 2 张",
  });
  const knowhowWidths = [...knowhowBar.querySelectorAll("span")].map((seg) =>
    parseFloat(seg.style.width),
  );
  expect(knowhowWidths.reduce((acc, w) => acc + w, 0)).toBeCloseTo(100, 1);
});

test("hides proportion bars when totals are zero", () => {
  renderCards({ overview: emptyOverview });

  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(screen.getByText("还没有已保存的记忆")).toBeInTheDocument();
  expect(screen.getByText("还没有 Knowhow 表")).toBeInTheDocument();
});

test("renders a section-only loading state", () => {
  renderCards({ overview: null, loading: true });

  expect(screen.getByText("正在加载内容资产…")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Memory" })).not.toBeInTheDocument();
});

test("renders a section-only failure state", () => {
  renderCards({ overview: null, error: "network unavailable" });

  expect(screen.getByText("内容资产暂时不可用")).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Knowhow" })).not.toBeInTheDocument();
});

test("keeps navigation enabled in read-only mode without edit controls", () => {
  renderCards({ readOnly: true });

  expect(screen.getByText("只读")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看 2 条已确认记忆" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "查看 1 张待同步表" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: /新建|编辑|删除/ })).not.toBeInTheDocument();
});
