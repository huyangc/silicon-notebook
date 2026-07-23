// 批量规整保存的 origin 归因测试（codex 第 6 轮 P2）。
//
// 背景：KnowhowReformatBatchModal 是**自动保存** LLM 规整结果的循环——点「确认
// 保存」后它逐格把候选写回，不经人手一格复核。这些变更流水必须归因为
// "llm_reformat"，否则落后端默认 "user"、历史时间线上「格式规整」徽章对这条
// 主流程永不出现（后端 patch_knowhow_cells_batch 的 origin 白名单校验正是为此
// 保留）。修复前 runBatch 的 onSaveCell 调用只传 6 个参数、漏了 origin。
//
// 本文件挂载整个 KnowhowPanel（onSaveCell=panel 内部闭包 handleCellSave，不是
// 能单拎出来测的纯函数，同 knowhow-cell-restore.component.test.tsx 的取舍），驱动
// 真实的「规整整表 → 开始规整 → 确认保存」UI 流程，断言最终落到批量端点的请求体
// 带 origin==="llm_reformat"。
//
// 变异验证目标：把 knowhow-panel.tsx runBatch 里 onSaveCell(...) 末尾的
// "llm_reformat" 删掉（退回 6 参），本测试必须转红（origin 变 undefined）。
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("./knowhow-model.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./knowhow-model.ts")>();
  return {
    ...actual,
    fetchKnowhowTables: vi.fn(),
    fetchKnowhowTable: vi.fn(),
    reformatKnowhowCell: vi.fn(),
    patchKnowhowCell: vi.fn(),
    batchPatchKnowhowCells: vi.fn(),
  };
});

import {
  batchPatchKnowhowCells,
  fetchKnowhowTable,
  fetchKnowhowTables,
  patchKnowhowCell,
  reformatKnowhowCell,
  type KnowhowTableDetail,
} from "./knowhow-model.ts";
import { KnowhowPanel } from "./knowhow-panel.tsx";

// 同 knowhow-cell-restore.component.test.tsx：白名单掉 styled-jsx 在裸 jsdom 下
// 的已知属性告警，其余 console.error 仍视为真实问题。
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

// r1/r2 同一概念组（anchor "概念"="示波器"），"备注" 两行同值 "当前值"——合并成
// 一个共享格。整表规整排除 anchor 列，只规整 "备注"；两行同 trim 值经去重只发一次
// reformat，保存时按合并组扇写整组（batchPatchKnowhowCells）。
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
    ],
  };
}

test("批量规整保存：请求体 origin==='llm_reformat'（不落后端默认 user）", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchKnowhowTables).mockResolvedValue([]);
  vi.mocked(fetchKnowhowTable).mockResolvedValue(makeDetail());
  // reformat 回带 sourceMd="当前值" = 建批次那刻的 originalMd 快照（非陈旧），
  // changed=true + 一个不同的候选，令该格进入 "changed" 保存集。
  vi.mocked(reformatKnowhowCell).mockResolvedValue({
    candidateMd: "规整后的值",
    source: "rule",
    changed: true,
    sourceMd: "当前值",
  });
  vi.mocked(batchPatchKnowhowCells).mockResolvedValue([
    { rowId: "r1", columnId: "c-note", contentMd: "规整后的值", projectionStatus: "pending" },
    { rowId: "r2", columnId: "c-note", contentMd: "规整后的值", projectionStatus: "pending" },
  ]);

  render(
    <KnowhowPanel notebookId="nb-1" apiBase="http://api.test" canEdit onClose={() => undefined} initialTableId="t1" />,
  );

  // 规整整表 → 开始规整 → 等候选就位 → 确认保存
  await user.click(await screen.findByRole("button", { name: "一键规整整表" }));
  await user.click(await screen.findByRole("button", { name: "开始规整" }));
  await waitFor(() => expect(reformatKnowhowCell).toHaveBeenCalled());
  const saveButton = await screen.findByRole("button", { name: "确认保存" });
  await waitFor(() => expect(saveButton).toBeEnabled());
  await user.click(saveButton);

  await waitFor(() => expect(batchPatchKnowhowCells).toHaveBeenCalledTimes(1));
  expect(patchKnowhowCell).not.toHaveBeenCalled();
  const [, , input] = vi.mocked(batchPatchKnowhowCells).mock.calls[0];
  expect(input.columnId).toBe("c-note");
  expect([...input.rowIds].sort()).toEqual(["r1", "r2"]);
  expect(input.contentMd).toBe("规整后的值");
  // 本文件的核心断言：批量规整的变更流水归因为 llm_reformat，不是默认 user。
  expect(input.origin).toBe("llm_reformat");
});
