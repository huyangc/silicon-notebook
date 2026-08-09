import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/knowhow-model.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-model.ts")>();
  return {
    ...actual,
    fetchKnowhowTables: vi.fn(),
    fetchKnowhowTable: vi.fn(),
  };
});

import {
  fetchKnowhowTable,
  fetchKnowhowTables,
  type KnowhowTableDetail,
} from "../../app/knowhow-model.ts";
import { KnowhowPanel } from "../../app/knowhow-panel.tsx";

const detail: KnowhowTableDetail = {
  id: "t-widths",
  title: "列宽测试",
  description: "",
  anchorColumnId: "c-anchor",
  columns: [
    { id: "c-anchor", name: "题", role: "anchor", position: 0 },
    { id: "c-procedure", name: "处理步骤", role: "procedure", position: 1 },
    { id: "c-attribute", name: "备注", role: "attribute", position: 2 },
  ],
  rows: [{
    id: "r1",
    position: 0,
    projectionStatus: "synced",
    cells: {
      "c-anchor": "短",
      "c-procedure": "a".repeat(120),
      "c-attribute": "内容",
    },
  }],
};

beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1024 });
  vi.mocked(fetchKnowhowTables).mockResolvedValue([]);
  vi.mocked(fetchKnowhowTable).mockResolvedValue(detail);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("Knowhow 网格用 colgroup 固定内容感知宽度，窄屏切换紧 clamp 且保留滚动/sticky 结构", async () => {
  render(
    <KnowhowPanel
      notebookId="nb-1"
      apiBase="http://api.test"
      canEdit={false}
      onClose={() => undefined}
      initialTableId="t-widths"
    />,
  );

  const table = await screen.findByRole("table");
  expect(table.closest(".knowhow-grid-scroll")).not.toBeNull();
  expect(table).toHaveClass("knowhow-grid-table");
  expect(table.querySelector("thead th:first-child")).toHaveTextContent("题");

  const desktopCols = table.querySelectorAll("colgroup col");
  expect(desktopCols).toHaveLength(detail.columns.length + 1);
  expect(Array.from(desktopCols, (column) => (column as HTMLElement).style.width)).toEqual([
    "180px", "520px", "160px", "112px",
  ]);
  expect(table.style.width).toBe("972px");
  expect(table.style.minWidth).toBe("100%");

  act(() => {
    window.innerWidth = 600;
    window.dispatchEvent(new Event("resize"));
  });
  await waitFor(() => {
    const compactCols = table.querySelectorAll("colgroup col");
    expect(Array.from(compactCols, (column) => (column as HTMLElement).style.width)).toEqual([
      "160px", "380px", "144px", "104px",
    ]);
    expect(table.style.width).toBe("788px");
  });
});
