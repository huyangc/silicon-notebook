// Knowhow 单元格乐观校验（分享编辑 P1，决策 D7）的纯逻辑：409 结构化冲突的解析与挂载、
// 同浏览器标签页广播的消息校验、基线（钉住的存储值）怎样决定 expected_before。
// 面板上的端到端行为（连续保存不自撞、两个标签页交替编辑、双版本 UI）在
// tests/component/knowhow-panel-conflict.component.test.tsx。
import test from "node:test";
import assert from "node:assert/strict";

import { httpErrorStatus, toUserMessage } from "../../app/errors.ts";
import {
  batchPatchKnowhowCells,
  knowhowCellConflictOf,
  parseKnowhowCellConflict,
  patchKnowhowCell,
} from "../../app/knowhow-model.ts";
import {
  advancePinnedCells,
  applyCellValues,
  cellSaveBaseline,
  cellsToPin,
  decodeCellValues,
  encodeCellValues,
  knowhowCellChannelName,
  openKnowhowCellChannel,
  pinCells,
  queueCellValues,
  repinCells,
  unpinCells,
} from "../../app/knowhow-cell-sync.ts";

test("整表请求在途时排队的广播:同一张表依次累加,换了表就只留新表的", () => {
  const a = { rowId: "r1", columnId: "c1", contentMd: "A" };
  const b = { rowId: "r2", columnId: "c1", contentMd: "B" };
  const c = { rowId: "r9", columnId: "c9", contentMd: "C" };
  const first = queueCellValues(null, "t-1", [a]);
  assert.deepEqual(first, { tableId: "t-1", values: [a] });
  assert.deepEqual(queueCellValues(first, "t-1", [b]), { tableId: "t-1", values: [a, b] });
  assert.deepEqual(queueCellValues(first, "t-2", [c]), { tableId: "t-2", values: [c] });
});

const CONFLICT_BODY = {
  detail: {
    code: "knowhow_cell_conflict",
    reason: "content_changed",
    message: "内容已被其他人修改，请刷新后重试",
    current: [{ row_id: "r1", column_id: "c1", content_md: "别人改的" }],
  },
};

function anchorTable() {
  return {
    id: "t1",
    title: "概念表",
    description: "",
    anchorColumnId: "ca",
    columns: [
      { id: "ca", name: "概念", role: "anchor", position: 0 },
      { id: "cs", name: "共同", role: "procedure", position: 1 },
      { id: "co", name: "备注", role: "attribute", position: 2 },
    ],
    rows: [
      { id: "a1", position: 0, projectionStatus: "synced", cells: { ca: "时钟", cs: "查时钟 ", co: "主板" } },
      { id: "a2", position: 1, projectionStatus: "synced", cells: { ca: "时钟 ", cs: "查时钟", co: "" } },
      { id: "a3", position: 2, projectionStatus: "synced", cells: { ca: "电源", cs: "量电压", co: "单板" } },
    ],
  };
}

async function withFetch(status, body, run) {
  const original = globalThis.fetch;
  const originalError = console.error;
  const originalDebug = console.debug;
  console.error = () => undefined;
  console.debug = () => undefined;
  globalThis.fetch = async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  try {
    await run();
  } finally {
    globalThis.fetch = original;
    console.error = originalError;
    console.debug = originalDebug;
  }
}

// --- 409 解析 ------------------------------------------------------------------

test("parseKnowhowCellConflict: 409 + 闭集 code/reason + 合形 current 才命中", () => {
  assert.deepStrictEqual(parseKnowhowCellConflict(409, CONFLICT_BODY), {
    reason: "content_changed",
    current: [{ rowId: "r1", columnId: "c1", contentMd: "别人改的" }],
  });
  assert.deepStrictEqual(
    parseKnowhowCellConflict(409, { detail: { ...CONFLICT_BODY.detail, reason: "group_changed", current: [] } }),
    { reason: "group_changed", current: [] },
  );
});

test("parseKnowhowCellConflict: 状态码、code、reason、current 任一不合形一律不认", () => {
  assert.equal(parseKnowhowCellConflict(400, CONFLICT_BODY), null);
  assert.equal(parseKnowhowCellConflict(409, { detail: "内容已被其他人修改" }), null);
  assert.equal(parseKnowhowCellConflict(409, { detail: { ...CONFLICT_BODY.detail, code: "notebook_mirrored" } }), null);
  assert.equal(parseKnowhowCellConflict(409, { detail: { ...CONFLICT_BODY.detail, reason: "whatever" } }), null);
  assert.equal(parseKnowhowCellConflict(409, { detail: { ...CONFLICT_BODY.detail, current: null } }), null);
  assert.equal(
    parseKnowhowCellConflict(409, { detail: { ...CONFLICT_BODY.detail, current: [{ row_id: "r1", column_id: "c1" }] } }),
    null,
  );
  assert.equal(parseKnowhowCellConflict(409, null), null);
});

test("patchKnowhowCell 的 409 冲突：异常仍是共享错误层的那一个（状态码与可信文案），另挂 reason/current", async () => {
  await withFetch(409, CONFLICT_BODY, async () => {
    await assert.rejects(patchKnowhowCell("nb-1", "t1", "r1", "c1", "我的", "旧的"), (err) => {
      assert.equal(httpErrorStatus(err), 409);
      assert.equal(toUserMessage(err), "内容已被其他人修改，请刷新后重试");
      assert.deepStrictEqual(knowhowCellConflictOf(err), {
        reason: "content_changed",
        current: [{ rowId: "r1", columnId: "c1", contentMd: "别人改的" }],
      });
      return true;
    });
  });
});

test("batchPatchKnowhowCells 的 group_changed 同样挂冲突；其他失败不挂", async () => {
  const groupChanged = { detail: { ...CONFLICT_BODY.detail, reason: "group_changed", message: "这一组已被重新分组，请刷新后再编辑", current: [] } };
  await withFetch(409, groupChanged, async () => {
    await assert.rejects(
      batchPatchKnowhowCells("nb-1", "t1", { columnId: "c1", rowIds: ["r1", "r2"], contentMd: "x", expectedBefore: ["a", "a"] }),
      (err) => knowhowCellConflictOf(err)?.reason === "group_changed",
    );
  });
  await withFetch(500, { detail: "boom" }, async () => {
    await assert.rejects(patchKnowhowCell("nb-1", "t1", "r1", "c1", "x", ""), (err) => knowhowCellConflictOf(err) === null);
  });
  assert.equal(knowhowCellConflictOf(new Error("x")), null);
  assert.equal(knowhowCellConflictOf("x"), null);
});

// --- 广播消息 --------------------------------------------------------------------

test("频道名按表；消息是三元组数组，任何一项不合形整条丢弃", () => {
  assert.equal(knowhowCellChannelName("t1"), "knowhow-cell:t1");
  const values = [{ rowId: "r1", columnId: "c1", contentMd: "x" }];
  assert.deepStrictEqual(encodeCellValues(values), [["r1", "c1", "x"]]);
  assert.deepStrictEqual(decodeCellValues([["r1", "c1", "x"]]), values);
  assert.equal(decodeCellValues({ cells: [] }), null);
  assert.equal(decodeCellValues([["r1", "c1"]]), null);
  assert.equal(decodeCellValues([["r1", "c1", 3]]), null);
});

test("versioned broadcasts retain the commit revision and reject malformed revisions atomically", () => {
  const values = [{ rowId: "r1", columnId: "c1", contentMd: "x", mutationSeq: 9 }];
  assert.deepEqual(encodeCellValues(values), [["r1", "c1", "x", 9]]);
  assert.deepEqual(decodeCellValues(encodeCellValues(values)), values);
  for (const revision of [-1, 1.5, "9", Number.MAX_SAFE_INTEGER + 1, null]) {
    assert.equal(decodeCellValues([["r1", "c1", "x", 9], ["r2", "c1", "y", revision]]), null);
  }
});

test("HTTP saved values and conflicts retain the same committed revision", async () => {
  await withFetch(200, { row_id: "r1", column_id: "c1", content_md: "saved", projection_status: "pending", mutation_seq: 9 }, async () => {
    assert.equal((await patchKnowhowCell("nb-1", "t1", "r1", "c1", "saved", "old")).mutationSeq, 9);
  });
  const body = { detail: { ...CONFLICT_BODY.detail, current: [{ row_id: "r1", column_id: "c1", content_md: "theirs", mutation_seq: 10 }] } };
  assert.deepEqual(parseKnowhowCellConflict(409, body)?.current, [{ rowId: "r1", columnId: "c1", contentMd: "theirs", mutationSeq: 10 }]);
  body.detail.current[0].mutation_seq = -1;
  assert.equal(parseKnowhowCellConflict(409, body), null);
});

test("cell delta revisions do not become a false whole-table watermark", () => {
  const initial = { ...anchorTable(), mutationSeq: 1 };
  const newer = { rowId: "a1", columnId: "co", contentMd: "v3", mutationSeq: 3 };
  const other = { rowId: "a2", columnId: "co", contentMd: "v2 other cell", mutationSeq: 2 };
  const stale = { ...newer, contentMd: "v2", mutationSeq: 2 };
  const merged = applyCellValues(initial, [newer, other, stale]);
  assert.equal(merged.rows[0].cells.co, "v3");
  assert.equal(merged.rows[1].cells.co, "v2 other cell");
  assert.equal(merged.mutationSeq, 1);
  assert.equal(applyCellValues(merged, [stale]), merged);
  assert.equal(applyCellValues(merged, [{ ...stale, mutationSeq: undefined }]), merged);
  const freshSnapshot = { ...initial, mutationSeq: 4 };
  assert.equal(applyCellValues(freshSnapshot, [newer, other]), freshSnapshot);
});

test("a delayed save at the snapshot revision cannot undo observed projection completion", () => {
  const snapshot = { ...anchorTable(), mutationSeq: 3 };
  const saved = { rowId: "a1", columnId: "co", contentMd: "主板", mutationSeq: 3, projectionStatus: "pending" };
  const merged = applyCellValues(snapshot, [saved]);
  assert.equal(merged, snapshot);
  assert.equal(merged.rows[0].projectionStatus, "synced");
  assert.equal(merged.rows[0].cells.co, "主板");
  const later = applyCellValues(merged, [{ ...saved, mutationSeq: 4 }]);
  assert.equal(later.rows[0].projectionStatus, "pending");
});

test("openKnowhowCellChannel：不支持 BroadcastChannel 或构造抛错时降级为空操作", () => {
  const original = globalThis.BroadcastChannel;
  try {
    globalThis.BroadcastChannel = undefined;
    const silent = openKnowhowCellChannel("t1", () => assert.fail("不该收到"));
    silent.post([{ rowId: "r1", columnId: "c1", contentMd: "x" }]);
    silent.close();
    globalThis.BroadcastChannel = function Throwing() {
      throw new Error("blocked");
    };
    const broken = openKnowhowCellChannel("t1", () => assert.fail("不该收到"));
    broken.post([{ rowId: "r1", columnId: "c1", contentMd: "x" }]);
    broken.close();
  } finally {
    globalThis.BroadcastChannel = original;
  }
});

test("applyCellValues：只换命中的格子；一格都没变时原样返回同一个对象", () => {
  const table = anchorTable();
  assert.equal(applyCellValues(table, [{ rowId: "a3", columnId: "cs", contentMd: "量电压" }]), table);
  assert.equal(applyCellValues(table, [{ rowId: "gone", columnId: "cs", contentMd: "x" }]), table);
  const next = applyCellValues(table, [{ rowId: "a1", columnId: "co", contentMd: "新" }]);
  assert.notEqual(next, table);
  assert.equal(next.rows[0].cells.co, "新");
  assert.equal(next.rows[1], table.rows[1]);
  assert.equal(table.rows[0].cells.co, "主板");
});

// --- 基线 ------------------------------------------------------------------------

test("合并共享格：钉住整组的这一列与各行锚定列；保存带逐行基线（逐字节，不 trim）与锚定守卫", () => {
  const table = anchorTable();
  const pins = new Map();
  const cells = cellsToPin(table, "a1", ["cs"]);
  assert.deepStrictEqual(cells, [["a1", "cs"], ["a1", "ca"], ["a2", "cs"], ["a2", "ca"]]);
  pinCells(pins, table, cells);

  const baseline = cellSaveBaseline(table, pins, "a1", "cs");
  assert.deepStrictEqual(baseline.targetRowIds, ["a1", "a2"]);
  assert.deepStrictEqual([...baseline.expectedByRowId], [["a1", "查时钟 "], ["a2", "查时钟"]]);
  assert.deepStrictEqual(baseline.anchorGuard, {
    anchorColumnId: "ca",
    expectedAnchorByRowId: new Map([["a1", "时钟"], ["a2", "时钟 "]]),
  });

  // 非共享列 / 单行组：只有这一行，不带锚定守卫。
  const own = cellSaveBaseline(table, pins, "a1", "co");
  assert.deepStrictEqual(own.targetRowIds, ["a1"]);
  assert.equal(own.anchorGuard, undefined);
});

test("钉住的值不随表数据整表刷新变，只随这个浏览器写成/听到的存储值前移；覆盖时用冲突的当前值", () => {
  const table = anchorTable();
  const pins = new Map();
  const cells = cellsToPin(table, "a1", ["co"]);
  pinCells(pins, table, cells);

  // 别人改过、整表重拉进来的值：不前移基线（保存时按冲突处理）。
  const refetched = applyCellValues(table, [{ rowId: "a1", columnId: "co", contentMd: "别人改的" }]);
  assert.equal(cellSaveBaseline(refetched, pins, "a1", "co").expectedByRowId.get("a1"), "主板");

  // 这个浏览器的保存 / 另一个标签页的广播：前移。
  advancePinnedCells(pins, [{ rowId: "a1", columnId: "co", contentMd: "我存的" }]);
  assert.equal(cellSaveBaseline(refetched, pins, "a1", "co").expectedByRowId.get("a1"), "我存的");

  // 「用我的覆盖」：409 回传的当前值当基线，不改钉住的值本身。
  const overwrite = { reason: "content_changed", current: [{ rowId: "a1", columnId: "co", contentMd: "别人改的" }] };
  assert.equal(cellSaveBaseline(refetched, pins, "a1", "co", overwrite).expectedByRowId.get("a1"), "别人改的");
  assert.equal(cellSaveBaseline(refetched, pins, "a1", "co").expectedByRowId.get("a1"), "我存的");

  // 冲突后点「刷新」：改钉到刷新后的值。
  repinCells(pins, refetched);
  assert.equal(cellSaveBaseline(refetched, pins, "a1", "co").expectedByRowId.get("a1"), "别人改的");

  // 持有者计数：两处钉住，放掉一处仍在。
  pinCells(pins, table, cells);
  unpinCells(pins, cells);
  assert.equal(cellSaveBaseline(table, pins, "a1", "co").expectedByRowId.get("a1"), "别人改的");
  unpinCells(pins, cells);
  assert.equal(pins.size, 0);
  assert.equal(cellSaveBaseline(table, pins, "a1", "co").expectedByRowId.get("a1"), "主板");
});
