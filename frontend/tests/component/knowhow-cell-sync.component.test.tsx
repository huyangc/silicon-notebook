import { act, cleanup, renderHook } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { applyCellValues, useKnowhowCellSync } from "../../app/knowhow-cell-sync.ts";
import type { KnowhowCellValue, KnowhowTableDetail } from "../../app/knowhow-model.ts";
import { anchorDetail, recordDetail } from "../../test-support/knowhow-panel-harness.tsx";

class CellChannel {
  static current: CellChannel;
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor() { CellChannel.current = this; }
  postMessage = vi.fn();
  close = vi.fn();
}

beforeEach(() => vi.stubGlobal("BroadcastChannel", CellChannel));
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderCellSync(initial: KnowhowTableDetail, rowId: string, columnId: string) {
  const inFlight = { current: true };
  const hook = renderHook(({ tableId }) => {
    const [detail, setDetail] = useState<KnowhowTableDetail | null>(initial);
    const [editing, setEditing] = useState(true);
    const [editingCell, setEditingCell] = useState({ rowId, columnId });
    const [optimizeRowId, setOptimizeRowId] = useState<string | null>(null);
    const sync = useKnowhowCellSync({
      tableId, detail, setDetail,
      editingRowId: editing ? editingCell.rowId : null,
      editingColumnId: editing ? editingCell.columnId : null,
      optimizeRowId,
      detailInFlight: () => inFlight.current,
    });
    return { detail, setDetail, setEditing, setEditingCell, setOptimizeRowId, sync };
  }, { initialProps: { tableId: initial.id } });
  return { ...hook, inFlight };
}

function recordLocalSave(
  current: ReturnType<typeof renderCellSync>["result"]["current"],
  values: KnowhowCellValue[],
) {
  const accepted = current.sync.recordStored(values);
  // The panel merges the server's write response immediately, before its
  // older background detail request has returned.
  if (accepted) current.setDetail((previous) => previous && applyCellValues(previous, accepted));
}

test.each(["editor", "row optimisation"])("a %s opened after a queued browser write pins that committed value before the older read settles", (entry) => {
  const detail = recordDetail({ mutationSeq: 1 });
  const { result, inFlight } = renderCellSync(detail, "r1", "c-title");
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "saved in another tab", 2]] } as MessageEvent));
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("检查电源"); // still queued
  act(() => {
    if (entry === "editor") result.current.setEditingCell({ rowId: "r1", columnId: "c-fix" });
    else result.current.setOptimizeRowId("r1");
  });
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("saved in another tab");
  act(() => {
    result.current.setDetail(structuredClone(detail));
    inFlight.current = false;
    result.current.sync.settleDetailRequest();
  });
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("saved in another tab");
  // The next save names the actual committed value, so it cannot self-conflict.
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("saved in another tab");
});

test("new row-optimisation pins ignore an older different browser value and preserve an already open draft baseline", () => {
  const detail = recordDetail({ mutationSeq: 1 });
  const { result } = renderCellSync(detail, "r1", "c-title");
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "older browser save", 2]] } as MessageEvent));
  act(() => result.current.setDetail({ ...detail, mutationSeq: 3,
    rows: detail.rows.map((row) => ({ ...row, cells: { ...row.cells, "c-title": "a third party changed X", "c-fix": "newer Y" } })) }));
  act(() => result.current.setOptimizeRowId("r1"));
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("newer Y");
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-title").expectedByRowId.get("r1")).toBe("供电异常");
});

test("a delayed save response cannot roll back a newer committed value received from another tab", () => {
  const detail = { ...recordDetail(), mutationSeq: 1 };
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  inFlight.current = false;
  // Tab A's v1 committed at 2, but its HTTP response is still delayed. Tab B
  // read that commit, saved v2 at 3, and its broadcast reaches A first.
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "v2", 3]] } as MessageEvent));
  act(() => recordLocalSave(result.current, [{ rowId: "r1", columnId: "c-fix", contentMd: "v1", mutationSeq: 2 }]));
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("v2");
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("v2");
  expect(CellChannel.current.postMessage).not.toHaveBeenCalled();
});

test("commit order is tracked per cell so a late different-cell save is still delivered", () => {
  const detail = { ...recordDetail(), mutationSeq: 1 };
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  inFlight.current = false;
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "newer cell", 3]] } as MessageEvent));
  act(() => recordLocalSave(result.current, [{ rowId: "r1", columnId: "c-other", contentMd: "earlier other cell", mutationSeq: 2 }]));
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("newer cell");
  expect(result.current.detail!.rows[0].cells["c-other"]).toBe("earlier other cell");
  expect(CellChannel.current.postMessage).toHaveBeenCalledWith([["r1", "c-other", "earlier other cell", 2]]);
});

test("a newer detail snapshot wins over queued commits without repinning an open draft", () => {
  const detail = { ...recordDetail(), mutationSeq: 1 };
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  act(() => recordLocalSave(result.current, [{ rowId: "r1", columnId: "c-fix", contentMd: "v1", mutationSeq: 2 }]));
  act(() => {
    result.current.setDetail({ ...detail, mutationSeq: 3, rows: detail.rows.map((row) => ({ ...row, cells: { ...row.cells, "c-fix": "third party" } })) });
    inFlight.current = false;
    result.current.sync.settleDetailRequest();
  });
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("third party");
  // A normal background read must not advance a draft's optimistic baseline.
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("v1");
  // Neither an old response nor an old three-field broadcast can undo it.
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "legacy old"]] } as MessageEvent));
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("v1");
  // Explicitly discarding the draft adopts the actual current snapshot, rather
  // than the older conflict response; the shown value and pin must agree.
  act(() => result.current.sync.adoptCurrent([{ rowId: "r1", columnId: "c-fix", contentMd: "old conflict", mutationSeq: 2 }]));
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("third party");
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("third party");
});

test("discarding v1 after a background snapshot v2 advances the original v0 pin to the value returned to the editor", () => {
  const detail = recordDetail({ mutationSeq: 1 });
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  inFlight.current = false;
  const conflict = [{ rowId: "r1", columnId: "c-fix", contentMd: "v1 conflict", mutationSeq: 2 }];
  act(() => result.current.setDetail({ ...detail, mutationSeq: 3,
    rows: detail.rows.map((row) => ({ ...row, cells: { ...row.cells, "c-fix": "v2 snapshot" } })) }));
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("检查电源");
  let adopted: KnowhowCellValue[] = [];
  act(() => { adopted = result.current.sync.adoptCurrent(conflict); });
  expect(adopted[0].contentMd).toBe("v2 snapshot");
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe(adopted[0].contentMd);
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("v2 snapshot");
  expect(CellChannel.current.postMessage).not.toHaveBeenCalled();
});

test("a late conflict cannot replace the newer browser save on discard or overwrite", () => {
  const detail = { ...recordDetail(), mutationSeq: 1 };
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  inFlight.current = false;
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "v3", 3]] } as MessageEvent));
  const conflict = { reason: "content_changed" as const, current: [{ rowId: "r1", columnId: "c-fix", contentMd: "v2", mutationSeq: 2 }] };
  act(() => result.current.sync.adoptCurrent(conflict.current));
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("v3");
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix", conflict).expectedByRowId.get("r1")).toBe("v3");
});

test("a delayed browser commit matching a newer snapshot still advances pins without replaying old data", () => {
  const detail = { ...recordDetail(), mutationSeq: 1 };
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  inFlight.current = false;
  // B saved this cell at 2, another person saved a different cell at 3, and
  // A reloaded the full table before B's delayed broadcast arrived.
  act(() => result.current.setDetail({ ...detail, mutationSeq: 3,
    rows: detail.rows.map((row) => ({ ...row, cells: { ...row.cells, "c-fix": "B saved", "c-tool": "other person's update" } })) }));
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("检查电源");
  const snapshot = result.current.detail;
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "B saved", 2]] } as MessageEvent));
  expect(result.current.detail).toBe(snapshot);
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("B saved");
  expect(CellChannel.current.postMessage).not.toHaveBeenCalled();
});

test("a same-revision different value is rejected before pins and display", () => {
  const detail = { ...recordDetail(), mutationSeq: 1 };
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  inFlight.current = false;
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "impossible conflicting value", 1]] } as MessageEvent));
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("检查电源");
  expect(result.current.sync.baselineFor(result.current.detail!, "r1", "c-fix").expectedByRowId.get("r1")).toBe("检查电源");
});

test.each([
  { name: "plain", detail: recordDetail(), rowIds: ["r1"], columnId: "c-fix" },
  { name: "merged", detail: anchorDetail(), rowIds: ["a1", "a2"], columnId: "c-shared" },
])("a local $name save survives an older detail reply and becomes the reopened editor's baseline", ({ detail, rowIds, columnId }) => {
  const { result, inFlight } = renderCellSync(detail, rowIds[0], columnId);
  const stored = rowIds.map((rowId) => ({ rowId, columnId, contentMd: "写后存储值" }));
  act(() => {
    recordLocalSave(result.current, stored);
    result.current.setEditing(false);
  });
  act(() => {
    result.current.setDetail(structuredClone(detail));
    inFlight.current = false;
    result.current.sync.settleDetailRequest();
  });
  for (const rowId of rowIds) {
    expect(result.current.detail!.rows.find((row) => row.id === rowId)?.cells[columnId]).toBe("写后存储值");
  }
  act(() => result.current.setEditing(true));
  const baseline = result.current.sync.baselineFor(result.current.detail!, rowIds[0], columnId);
  expect([...baseline.expectedByRowId.values()]).toEqual(rowIds.map(() => "写后存储值"));
});

test("local and received writes are replayed in their observed order over an older read", () => {
  const detail = recordDetail();
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  const value = (contentMd: string) => [{ rowId: "r1", columnId: "c-fix", contentMd }];
  act(() => recordLocalSave(result.current, value("第一次本地保存")));
  act(() => CellChannel.current.onmessage?.({ data: [["r1", "c-fix", "别的标签页保存"]] } as MessageEvent));
  act(() => recordLocalSave(result.current, value("第二次本地保存")));
  act(() => {
    result.current.setDetail(structuredClone(detail));
    inFlight.current = false;
    result.current.sync.settleDetailRequest();
  });
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("第二次本地保存");
});

test("accepting the server's current value after a conflict also survives an older read", () => {
  const detail = recordDetail();
  const { result, inFlight } = renderCellSync(detail, "r1", "c-fix");
  act(() => {
    result.current.sync.adoptCurrent([{ rowId: "r1", columnId: "c-fix", contentMd: "已接受的当前值" }]);
    result.current.setEditing(false);
  });
  act(() => {
    result.current.setDetail(structuredClone(detail));
    inFlight.current = false;
    result.current.sync.settleDetailRequest();
  });
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("已接受的当前值");
  expect(CellChannel.current.postMessage).not.toHaveBeenCalled();
});

test("late update callbacks from a previous table cannot replace the current table's queued write", () => {
  const initial = recordDetail();
  const { result, rerender, inFlight } = renderCellSync(initial, "r1", "c-fix");
  const previousRecordStored = result.current.sync.recordStored;
  const previousAdoptCurrent = result.current.sync.adoptCurrent;
  const next = recordDetail({ id: "another-table" });
  rerender({ tableId: next.id });
  act(() => result.current.setDetail(next));
  act(() => {
    recordLocalSave(result.current, [{ rowId: "r1", columnId: "c-fix", contentMd: "新表保存" }]);
    previousRecordStored([{ rowId: "r1", columnId: "c-fix", contentMd: "旧表迟到的保存" }]);
    previousAdoptCurrent([{ rowId: "r1", columnId: "c-fix", contentMd: "旧表迟到的当前值" }]);
  });
  act(() => {
    result.current.setDetail(structuredClone(next));
    inFlight.current = false;
    result.current.sync.settleDetailRequest();
  });
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("新表保存");
  expect(CellChannel.current.postMessage).toHaveBeenCalledTimes(1);
});

test("leaving and reopening the same table invalidates callbacks from its previous visit", () => {
  const initial = recordDetail();
  const { result, rerender, inFlight } = renderCellSync(initial, "r1", "c-fix");
  const previousRecordStored = result.current.sync.recordStored;
  rerender({ tableId: "another-table" });
  rerender({ tableId: initial.id });
  act(() => {
    recordLocalSave(result.current, [{ rowId: "r1", columnId: "c-fix", contentMd: "重开后的保存" }]);
    previousRecordStored([{ rowId: "r1", columnId: "c-fix", contentMd: "上次离开前的保存" }]);
  });
  act(() => {
    result.current.setDetail(structuredClone(initial));
    inFlight.current = false;
    result.current.sync.settleDetailRequest();
  });
  expect(result.current.detail!.rows[0].cells["c-fix"]).toBe("重开后的保存");
  expect(CellChannel.current.postMessage).toHaveBeenCalledTimes(1);
});
