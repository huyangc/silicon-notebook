// 编辑态「历史」按钮的草稿落盘守卫（codex 第 6 轮 P2）。
//
// 修复前：编辑态 History 按钮直调 onHistory、卸载编辑器；卸载兜底的 flushDraft
// **忽略返回值**，localStorage 满/不可用时草稿没写成、文字静默丢失。修复把它经
// performLeave({kind:"history"}) → commitLeave 走与 Esc/关闭同一套守卫：落盘成功
// 即打开历史，失败首次拦下+警告、二次强制放行。
//
// 本文件直接挂载导出的 KnowhowCellEditor（无需整个 panel），驱动真实点击，断言：
//   A. 落盘正常：点历史 → onHistory 被调用一次，且 onClose **不**被调用（若
//      commitLeave 把 "history" 意图误落到 onClose 分支，本断言转红）。
//   B. 落盘失败（localStorage.setItem 抛错）：首次点历史 → onHistory **不**被调用、
//      屏幕出现 DRAFT_FLUSH_FAILED 警告；二次点 → 强制放行、onHistory 被调用。
//      （把 History 按钮改回直调 onHistory 绕开守卫时，A 的 onClose 断言仍绿但 B
//      的「首次不调用」转红——正是被修的静默丢字 bug。）
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/knowhow-model.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/knowhow-model.ts")>();
  return {
    ...actual,
    optimizeKnowhowCell: vi.fn(),
    reformatKnowhowCell: vi.fn(),
  };
});

import { KnowhowCellEditor } from "../../app/knowhow-cell-editor.tsx";
import { DRAFT_FLUSH_FAILED_MESSAGE } from "../../app/knowhow-cell-editor-logic.ts";
import {
  optimizeKnowhowCell,
  reformatKnowhowCell,
  type KnowhowTableDetail,
} from "../../app/knowhow-model.ts";

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
    window.localStorage.clear();
  }
});

function makeTable(): KnowhowTableDetail {
  return {
    id: "t1",
    title: "T",
    description: "",
    anchorColumnId: "c-concept",
    columns: [
      { id: "c-concept", name: "概念", role: "anchor", position: 0 },
      { id: "c-note", name: "备注", role: "attribute", position: 1 },
    ],
    rows: [
      { id: "r1", position: 0, projectionStatus: "synced", cells: { "c-concept": "示波器", "c-note": "原始值" } },
    ],
  };
}

function renderEditor(onHistory: () => void, onClose: () => void) {
  render(
    <KnowhowCellEditor
      notebookId="nb-1"
      apiBase="http://api.test"
      table={makeTable()}
      rowId="r1"
      columnId="c-note"
      rowTitle="示波器"
      onSave={vi.fn().mockResolvedValue(undefined)}
      onNavigate={vi.fn()}
      onClose={onClose}
      onHistory={onHistory}
    />,
  );
}

test("A. 落盘正常：点历史打开历史（onHistory 调用一次），不误触 onClose", async () => {
  const user = userEvent.setup();
  const onHistory = vi.fn();
  const onClose = vi.fn();
  renderEditor(onHistory, onClose);

  await user.type(screen.getByRole("textbox"), "改");  // 制造未保存改动
  await user.click(screen.getByRole("button", { name: "历史" }));

  expect(onHistory).toHaveBeenCalledTimes(1);
  expect(onClose).not.toHaveBeenCalled();
});

test("B. 落盘失败：首次点历史拦下+警告，二次强制放行", async () => {
  const user = userEvent.setup();
  const onHistory = vi.fn();
  const onClose = vi.fn();
  renderEditor(onHistory, onClose);

  await user.type(screen.getByRole("textbox"), "改");
  // 断掉 localStorage 写：草稿落不了盘（配额/隐私模式）。
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota exceeded");
  });

  await user.click(screen.getByRole("button", { name: "历史" }));
  expect(onHistory).not.toHaveBeenCalled();  // 首次拦下
  await screen.findByText(DRAFT_FLUSH_FAILED_MESSAGE);  // 警告出现

  await user.click(screen.getByRole("button", { name: "历史" }));
  expect(onHistory).toHaveBeenCalledTimes(1);  // 二次强制放行
});

test("关闭单格编辑器会取消仍在等待的优化请求", async () => {
  const user = userEvent.setup();
  vi.mocked(optimizeKnowhowCell).mockReturnValue(new Promise(() => undefined));
  const onClose = vi.fn();
  renderEditor(vi.fn(), onClose);

  await user.click(screen.getByRole("button", { name: "优化表达" }));
  const signal = vi.mocked(optimizeKnowhowCell).mock.calls[0][4];
  expect(signal?.aborted).toBe(false);
  await user.click(screen.getByTitle("关闭"));

  expect(onClose).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(true);
});

test("关闭单格编辑器会取消仍在等待的规整格式请求", async () => {
  const user = userEvent.setup();
  vi.mocked(reformatKnowhowCell).mockReturnValue(new Promise(() => undefined));
  const onClose = vi.fn();
  renderEditor(vi.fn(), onClose);

  await user.click(screen.getByRole("button", { name: "规整格式" }));
  const signal = vi.mocked(reformatKnowhowCell).mock.calls[0][4];
  expect(signal?.aborted).toBe(false);
  await user.click(screen.getByTitle("关闭"));

  expect(onClose).toHaveBeenCalledTimes(1);
  expect(signal?.aborted).toBe(true);
});
