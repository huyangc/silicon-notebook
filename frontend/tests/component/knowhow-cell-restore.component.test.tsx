// 格子历史「恢复此版本」的扇写路由测试（评审 Important 修复，problem 1）。
//
// 背景：knowhow-cell-history.tsx 的 handleRestore 曾经直接调用
// patchKnowhowCell（单格端点），完全绕过了 knowhow-panel.tsx handleCellSave
// 里的 groupRowsByAnchor + isSharedColumn + batchPatchKnowhowCells 扇写判定
// （anchor 分组 spec §4.4/§6，设计文档 §6.4 明文要求走既有 update_knowhow_cell
// 「含...合并格扇写判定」）。后果：合并组内 N 行同列同值时，对其中一行做历史
// 恢复只会写回该行，组内其余行不变——下次渲染 G2 因 isSharedColumn 判假而
// 自动把合并格散开，静默产生组内数据发散。
//
// 修复把恢复路径改为委托给 handleCellSave 本身（多传 origin="revert"），与
// 手动编辑走同一条判定。本文件挂载整个 KnowhowPanel（这条判定活在 panel 内部
// 的闭包里，不是一个能单独拎出来测的纯函数），驱动真实的"点格子→切历史→
// 确认恢复"UI 流程，断言：
//   A. 合并共享格（anchor 分组内该列同值、组内 >1 行）：恢复发出的是**批量**
//      请求（batchPatchKnowhowCells），且覆盖组内全部行——不只是当前打开的
//      这一行。
//   B. 非合并格（概念组只有一行）：恢复仍走**单格**请求（patchKnowhowCell）。
//   C. 两种情况下请求体都不带 expected_before（恢复走 last-write-wins 语义，
//      不受并发防护 P1-b 的基线比对约束——若未来误加会悄悄改变恢复语义）。
//
// A 项是本文件的核心变异验证目标：把 knowhow-cell-history.tsx 的 handleRestore
// 改回直调 patchKnowhowCell 时，这条测试必须变红（batchPatchKnowhowCells 不再
// 被调用、且 patchKnowhowCell 只写了当前这一行）。
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/knowhow-model.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-model.ts")>();
  return {
    ...actual,
    fetchKnowhowTables: vi.fn(),
    fetchKnowhowTable: vi.fn(),
    fetchKnowhowCellHistory: vi.fn(),
    patchKnowhowCell: vi.fn(),
    batchPatchKnowhowCells: vi.fn(),
  };
});

import {
  batchPatchKnowhowCells,
  fetchKnowhowCellHistory,
  fetchKnowhowTable,
  fetchKnowhowTables,
  patchKnowhowCell,
  type KnowhowTableDetail,
} from "../../app/knowhow-model.ts";
import { KnowhowPanel } from "../../app/knowhow-panel.tsx";

// KnowhowPanel 顶层的 <style jsx global> 在裸 vitest/jsdom 环境下（没有 Next.js
// 的 babel 变换）会触发 React 一条已知的 DOM 属性警告——同
// content-navigation.component.test.tsx 既有的处理方式：白名单掉这一条已知
// 噪音，但仍然对其余任何意外的 console.error 保持敏感（不静默吞掉真实问题）。
let unexpectedConsoleErrors: unknown[][] = [];

function isKnownStyledJsxAttributeWarning(args: unknown[]): boolean {
  const [format, value, attribute, domAttribute, domValue, propName] = args;
  return (
    format ===
      "Received `%s` for a non-boolean attribute `%s`.\n\n" +
        "If you want to write it to the DOM, pass a string instead: %s=\"%s\" or %s={value.toString()}."
    && value === true
    && (attribute === "jsx" || attribute === "global")
    && domAttribute === attribute
    && domValue === true
    && propName === attribute
  );
}

beforeEach(() => {
  unexpectedConsoleErrors = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    if (!isKnownStyledJsxAttributeWarning(args)) unexpectedConsoleErrors.push(args);
  });
});

afterEach(() => {
  try {
    expect(unexpectedConsoleErrors).toEqual([]);
  } finally {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  }
});

// r1/r2：同一个概念组（anchor 列 "概念" 同值 "示波器"），"备注" 列两行也同值
// "当前值"——G2 合并成一个 rowSpan=2 的共享格（isSharedColumn 判真）。
// r3：独立概念 "万用表"，组内只有它自己一行——"备注" 列不构成"合并共享格"
// （isSharedColumn 对单行组恒 false），即便技术上也是"全组同值"。
function makeDetail(): KnowhowTableDetail {
  return {
    id: "t1",
    title: "测试表",
    description: "",
    anchorColumnId: "c-concept",
    columns: [
      { id: "c-concept", name: "概念", role: "anchor", position: 0 },
      { id: "c-note", name: "备注", role: "attribute", position: 1 },
    ],
    rows: [
      { id: "r1", position: 0, projectionStatus: "synced", cells: { "c-concept": "示波器", "c-note": "当前值" } },
      { id: "r2", position: 1, projectionStatus: "synced", cells: { "c-concept": "示波器", "c-note": "当前值" } },
      { id: "r3", position: 2, projectionStatus: "synced", cells: { "c-concept": "万用表", "c-note": "独立值" } },
    ],
  };
}

function historyFor(current: string, historical: string) {
  return [
    { seq: 2, actor: "u1", origin: "user", createdAt: "2026-07-20T00:00:00Z", before: historical, after: current },
    { seq: 1, actor: "u1", origin: "user", createdAt: "2026-07-18T00:00:00Z", before: null, after: historical },
  ];
}

// 打开某一格的浮窗（预览态）→ 点「历史」切到历史页签→ 点「恢复此版本」→
// 「确认恢复」——三态共用 .kh-modal-overlay/.kh-modal-card 外壳（见
// knowhow-cell-history.tsx 头注释），"历史"这个按钮文案在表格工具栏（整表
// 历史抽屉入口）也存在，故用弹窗自己的 overlay 容器限定查找范围，避免撞到
// 工具栏那个同名按钮。
async function openCellAndRestore(user: ReturnType<typeof userEvent.setup>, cellText: string) {
  const cellButton = await screen.findByRole("button", { name: cellText });
  await user.click(cellButton);

  const overlay = document.querySelector(".kh-modal-overlay");
  expect(overlay).not.toBeNull();
  await user.click(within(overlay as HTMLElement).getByRole("button", { name: "历史" }));

  await user.click(await screen.findByRole("button", { name: /恢复此版本/ }));
  await user.click(screen.getByRole("button", { name: "确认恢复" }));
}

test("合并共享格：历史恢复发出批量请求，覆盖组内全部行；不落单格端点", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchKnowhowTables).mockResolvedValue([]);
  vi.mocked(fetchKnowhowTable).mockResolvedValue(makeDetail());
  vi.mocked(fetchKnowhowCellHistory).mockResolvedValue(historyFor("当前值", "旧值"));
  vi.mocked(batchPatchKnowhowCells).mockResolvedValue([
    { rowId: "r1", columnId: "c-note", contentMd: "旧值", projectionStatus: "pending" },
    { rowId: "r2", columnId: "c-note", contentMd: "旧值", projectionStatus: "pending" },
  ]);

  render(
    <KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit onClose={() => undefined} initialTableId="t1" />,
  );

  await openCellAndRestore(user, "当前值");

  await waitFor(() => expect(batchPatchKnowhowCells).toHaveBeenCalledTimes(1));
  expect(patchKnowhowCell).not.toHaveBeenCalled();

  const [calledNotebookId, calledTableId, input] = vi.mocked(batchPatchKnowhowCells).mock.calls[0];
  expect(calledNotebookId).toBe("nb-1");
  expect(calledTableId).toBe("t1");
  expect(input.columnId).toBe("c-note");
  // 覆盖组内全部行——不是只有被点开的那一行。
  expect([...input.rowIds].sort()).toEqual(["r1", "r2"]);
  expect(input.contentMd).toBe("旧值");
  expect(input.origin).toBe("revert");
  // 恢复走 last-write-wins，不带并发防护 P1-b 的基线比对（省略, 不是 undefined
  // 值——若未来误加会悄悄改变恢复语义）。
  expect("expectedBefore" in input).toBe(false);
  expect("anchorColumnId" in input).toBe(false);
});

test("非合并格（独立概念，组内只有一行）：历史恢复仍落单格端点", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchKnowhowTables).mockResolvedValue([]);
  vi.mocked(fetchKnowhowTable).mockResolvedValue(makeDetail());
  vi.mocked(fetchKnowhowCellHistory).mockResolvedValue(historyFor("独立值", "旧独立值"));
  vi.mocked(patchKnowhowCell).mockResolvedValue({
    rowId: "r3",
    columnId: "c-note",
    contentMd: "旧独立值",
    projectionStatus: "pending",
  });

  render(
    <KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit onClose={() => undefined} initialTableId="t1" />,
  );

  await openCellAndRestore(user, "独立值");

  await waitFor(() => expect(patchKnowhowCell).toHaveBeenCalledTimes(1));
  expect(batchPatchKnowhowCells).not.toHaveBeenCalled();

  const [calledNotebookId, calledTableId, calledRowId, calledColumnId, calledContentMd, expectedBefore, origin] =
    vi.mocked(patchKnowhowCell).mock.calls[0];
  expect(calledNotebookId).toBe("nb-1");
  expect(calledTableId).toBe("t1");
  expect(calledRowId).toBe("r3");
  expect(calledColumnId).toBe("c-note");
  expect(calledContentMd).toBe("旧独立值");
  // 恢复不带 expectedBefore——last-write-wins，同手动格子编辑既有语义。
  expect(expectedBefore).toBeUndefined();
  expect(origin).toBe("revert");
});
