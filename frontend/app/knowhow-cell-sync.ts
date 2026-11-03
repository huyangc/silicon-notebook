/**
 * knowhow-cell-sync.ts
 *
 * Knowhow 单元格乐观校验的前端一半（分享编辑 P1，决策 D7；规格
 * docs/superpowers/specs/2026-10-28-share-edit-collaboration-design_zh.md §5「D7 的硬规定」）：
 * 手动保存（普通格、合并共享格、「优化整行」接受）带上 expected_before，服务端在写事务里
 * 逐字节比对，不等就 409。本文件负责「带上去的基线是什么」，以及同一浏览器多个标签页之间
 * 共享单元格值——两件事都是为了一条：**同一个人在同一个浏览器里不被自己的旧快照拒绝**。
 *
 * 基线（pins）：
 *  - 打开编辑器（或「优化整行」浮层）时，把要写的格子钉住：这一格在所属概念组里的每一行
 *    （合并共享格会扇写整组）的内容，以及这些行的锚定列值（合并共享格的锚定守卫）。钉住的
 *    值取自表数据（detail）——也就是服务端存储值，不是输入框里的文本。
 *  - 保存成功后，用响应回传的写后存储值前移基线（服务端可能规范化过内容，拿本地文本当基线
 *    会让下一次保存必然失配）；收到别的标签页广播的存储值也同样前移。
 *  - 编辑器开着期间表数据被整表重拉（例如另一个人改过、规整判陈旧后刷新）**不**前移基线：
 *    那份改动不是这个浏览器写的，保存时按冲突处理才对。
 *  - 没钉住的格子（例如钉住之后才加入组的行）保存时退回表数据里的当前值。
 *
 * 跨标签页（BroadcastChannel("knowhow-cell:<table_id>")）：任一标签页保存成功后广播
 * `[[row_id, column_id, stored_content_md, mutation_seq], …]`（批量写一次广播整组）。逐格按持久
 * 提交版本排序，迟到的旧响应/广播不能回滚新值；同版本不同值和不能排序的旧版值也拒绝。收到的标签页不论
 * 开没开编辑器都更新本地表数据里对应的格子（下一次打开编辑器就钉到新值），正开着编辑器的格
 * 只前移基线、不动草稿（草稿是编辑器自己的 state，表数据变了它不跟着变）。BroadcastChannel
 * 不是 Secure Context 限定的 API，生产的 http://<IP> 形态可用；不支持或构造抛错时安全降级为
 * 不广播（同浏览器两个标签页交替编辑才会自撞 409，按冲突处理，不丢数据）。
 *
 * 纯逻辑在上半部分（无 React），hook 在最后。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, type Dispatch, type SetStateAction } from "react";

import type { KnowhowCellConflict, KnowhowCellValue, KnowhowTableDetail } from "./knowhow-model.ts";
import { validMutationSeq } from "./knowhow-model.ts";
import { groupCellWriteTargets, groupRowsByAnchor, isSharedColumn } from "./knowhow-grouping-logic.ts";

// ---------------------------------------------------------------------------
// 写目标与基线（纯逻辑）
// ---------------------------------------------------------------------------

export const knowhowCellChannelName = (tableId: string): string => `knowhow-cell:${tableId}`;

// 行 id / 列 id 都是服务端生成的不透明 id，JSON 编码成二元组当键，不靠分隔符。
const cellKey = (rowId: string, columnId: string): string => JSON.stringify([rowId, columnId]);

function liveCell(detail: KnowhowTableDetail, rowId: string, columnId: string): string {
  return detail.rows.find((row) => row.id === rowId)?.cells[columnId] ?? "";
}

/** 保存这一格实际要写的行：落在合并共享格（锚定分组内该列全分支同值、组内多于一行）时是
 *  整组，否则只有这一行。与 knowhow-panel.tsx handleCellSave 的缺省分组、编辑器头部的
 *  「改动将同步到全部 N 个分支」同一套判定，按**当前**表数据算。 */
export function cellWriteTargets(detail: KnowhowTableDetail, rowId: string, columnId: string): string[] {
  if (!detail.anchorColumnId) return [rowId];
  const group = groupRowsByAnchor(detail.rows, detail.anchorColumnId).find((g) => g.rows.some((r) => r.id === rowId));
  return group && isSharedColumn(group, columnId) ? groupCellWriteTargets(group, columnId) : [rowId];
}

/** 打开编辑这些列时要钉住的格子：每列的写目标行上的这一列，加上这些行的锚定列。 */
export function cellsToPin(
  detail: KnowhowTableDetail,
  rowId: string,
  columnIds: readonly string[],
): Array<[string, string]> {
  const cells = new Map<string, [string, string]>();
  for (const columnId of columnIds) {
    for (const targetRowId of cellWriteTargets(detail, rowId, columnId)) {
      cells.set(cellKey(targetRowId, columnId), [targetRowId, columnId]);
      if (detail.anchorColumnId) {
        cells.set(cellKey(targetRowId, detail.anchorColumnId), [targetRowId, detail.anchorColumnId]);
      }
    }
  }
  return [...cells.values()];
}

/** 钉住的格子：值 + 持有者计数（同一格可能被两处同时钉住；StrictMode 的双次 effect）。 */
export type PinnedCells = Map<string, { value: string; holders: number }>;

export function pinCells(pins: PinnedCells, detail: KnowhowTableDetail, cells: ReadonlyArray<[string, string]>): void {
  for (const [rowId, columnId] of cells) {
    const key = cellKey(rowId, columnId);
    const held = pins.get(key);
    if (held) held.holders += 1;
    else pins.set(key, { value: liveCell(detail, rowId, columnId), holders: 1 });
  }
}

export function unpinCells(pins: PinnedCells, cells: ReadonlyArray<[string, string]>): void {
  for (const [rowId, columnId] of cells) {
    const key = cellKey(rowId, columnId);
    const held = pins.get(key);
    if (!held) continue;
    held.holders -= 1;
    if (held.holders <= 0) pins.delete(key);
  }
}

/** 这个浏览器写成（或从别的标签页听到）的存储值：只前移已经钉住的格子。 */
export function advancePinnedCells(pins: PinnedCells, values: readonly KnowhowCellValue[]): void {
  for (const value of values) {
    const held = pins.get(cellKey(value.rowId, value.columnId));
    if (held) held.value = value.contentMd;
  }
}

/** 整表刷新之后（用户在冲突提示里点了「刷新」）：把所有钉住的格子重钉到新表数据。 */
export function repinCells(pins: PinnedCells, detail: KnowhowTableDetail): void {
  for (const [key, held] of pins) {
    const [rowId, columnId] = JSON.parse(key) as [string, string];
    held.value = liveCell(detail, rowId, columnId);
  }
}

export type CellSaveBaseline = {
  targetRowIds: string[];
  expectedByRowId: Map<string, string>;
  anchorGuard?: { anchorColumnId: string; expectedAnchorByRowId: Map<string, string> };
};

/**
 * 一次手动保存要带的守卫：写目标按当前表数据算；每个写目标的 expected_before 取钉住的值
 * （没钉住才取表数据）；扇写整组（写目标多于一行）时再带锚定守卫，各行锚定值同样先取钉住
 * 的值。`overwrite` 是「用我的覆盖」：以 409 时服务端回传的当前值为这些行的新基线重发。
 */
export function cellSaveBaseline(
  detail: KnowhowTableDetail,
  pins: PinnedCells,
  rowId: string,
  columnId: string,
  overwrite?: KnowhowCellConflict,
): CellSaveBaseline {
  const targetRowIds = cellWriteTargets(detail, rowId, columnId);
  const pinned = (targetRowId: string, targetColumnId: string): string =>
    pins.get(cellKey(targetRowId, targetColumnId))?.value ?? liveCell(detail, targetRowId, targetColumnId);
  const theirs = new Map(
    (overwrite?.current ?? [])
      .filter((value) => value.columnId === columnId)
      .map((value) => [value.rowId, value.contentMd]),
  );
  const expectedByRowId = new Map(
    targetRowIds.map((targetRowId) => [targetRowId, theirs.get(targetRowId) ?? pinned(targetRowId, columnId)]),
  );
  const anchorColumnId = detail.anchorColumnId;
  if (targetRowIds.length < 2 || !anchorColumnId) return { targetRowIds, expectedByRowId };
  return {
    targetRowIds,
    expectedByRowId,
    anchorGuard: {
      anchorColumnId,
      expectedAnchorByRowId: new Map(targetRowIds.map((targetRowId) => [targetRowId, pinned(targetRowId, anchorColumnId)])),
    },
  };
}

/** A table snapshot covers every cell at its revision. Deltas have separate
 * per-cell revisions: a newer write to A says nothing about an earlier write
 * to B whose response is still on the wire. Unversioned legacy values may be
 * used only until this cell has a known revision. */
function knownCellValues(
  detail: KnowhowTableDetail | null,
  value: KnowhowCellValue,
  observed?: KnowhowCellValue,
): Array<KnowhowCellValue & { mutationSeq: number }> {
  const detailRevisions = [detail?.mutationSeq, detail?.cellMutationSeqs?.[value.rowId]?.[value.columnId]].filter(validMutationSeq);
  const known: Array<KnowhowCellValue & { mutationSeq: number }> = [];
  if (detail && detailRevisions.length > 0) known.push({ rowId: value.rowId, columnId: value.columnId,
    mutationSeq: Math.max(...detailRevisions), contentMd: liveCell(detail, value.rowId, value.columnId) });
  if (observed && validMutationSeq(observed.mutationSeq)) known.push({ ...observed, mutationSeq: observed.mutationSeq });
  return known;
}

function cellValueOrder(
  detail: KnowhowTableDetail | null,
  value: KnowhowCellValue,
  observed?: KnowhowCellValue,
): "current" | "matching_older" | "stale" {
  const known = knownCellValues(detail, value, observed);
  if (known.length === 0) return "current";
  if (!validMutationSeq(value.mutationSeq)) return "stale";
  const latest = Math.max(...known.map((entry) => entry.mutationSeq));
  if (value.mutationSeq > latest) return "current";
  if (known.some((entry) => entry.mutationSeq === latest && entry.contentMd !== value.contentMd)) return "stale";
  return value.mutationSeq === latest ? "current" : "matching_older";
}

/** Merge committed values in commit order, even if responses/messages arrived
 * in reverse order. Keep the snapshot watermark separate from cell deltas. */
export function applyCellValues(detail: KnowhowTableDetail, values: readonly KnowhowCellValue[]): KnowhowTableDetail {
  if (values.length === 0) return detail;
  const byRow = new Map<string, Map<string, KnowhowCellValue>>();
  for (const value of values) {
    const cells = byRow.get(value.rowId) ?? new Map<string, KnowhowCellValue>();
    if (cellValueOrder(detail, value, cells.get(value.columnId)) !== "current") continue;
    cells.set(value.columnId, value);
    byRow.set(value.rowId, cells);
  }
  let changed = false;
  let cellMutationSeqs = detail.cellMutationSeqs;
  const rows = detail.rows.map((row) => {
    const updates = byRow.get(row.id);
    if (!updates) return row;
    let cells = row.cells;
    let projectionStatus = row.projectionStatus;
    for (const [columnId, value] of updates) {
      const coveredBySnapshot = validMutationSeq(value.mutationSeq) && validMutationSeq(detail.mutationSeq)
        && value.mutationSeq <= detail.mutationSeq;
      if (validMutationSeq(value.mutationSeq) && !coveredBySnapshot && cellMutationSeqs?.[row.id]?.[columnId] !== value.mutationSeq) {
        cellMutationSeqs = { ...cellMutationSeqs,
          [row.id]: { ...cellMutationSeqs?.[row.id], [columnId]: value.mutationSeq } };
        changed = true;
      }
      if ((cells[columnId] ?? "") !== value.contentMd) {
        cells = cells === row.cells ? { ...row.cells } : cells;
        cells[columnId] = value.contentMd;
      }
      // mutation_seq orders content writes, not projection completion. A read
      // at the same revision may already report synced/failed; the original
      // save's delayed pending flag must not roll that observation backwards.
      if (value.projectionStatus && !coveredBySnapshot) projectionStatus = value.projectionStatus;
    }
    if (cells === row.cells && projectionStatus === row.projectionStatus) return row;
    changed = true;
    return { ...row, cells, projectionStatus };
  });
  return changed ? { ...detail, rows, ...(cellMutationSeqs ? { cellMutationSeqs } : {}) } : detail;
}

// ---------------------------------------------------------------------------
// 同一浏览器标签页之间的广播
// ---------------------------------------------------------------------------

/** Old tabs understand only triples. New tabs send a fourth commit revision
 * and never allow a legacy triple to overwrite a versioned snapshot/delta. */
export function encodeCellValues(values: readonly KnowhowCellValue[]): Array<[string, string, string, number?]> {
  return values.map((value) => validMutationSeq(value.mutationSeq)
    ? [value.rowId, value.columnId, value.contentMd, value.mutationSeq]
    : [value.rowId, value.columnId, value.contentMd]);
}

/** 收到的消息逐项校验形状；任何一项不合形就整条丢弃（别的页面版本、别的用途的同名频道）。 */
export function decodeCellValues(data: unknown): KnowhowCellValue[] | null {
  if (!Array.isArray(data)) return null;
  const values: KnowhowCellValue[] = [];
  for (const item of data as unknown[]) {
    if (
      !Array.isArray(item) || (item.length !== 3 && item.length !== 4)
      || typeof item[0] !== "string" || typeof item[1] !== "string" || typeof item[2] !== "string"
      || (item.length === 4 && !validMutationSeq(item[3]))
    ) {
      return null;
    }
    values.push({ rowId: item[0], columnId: item[1], contentMd: item[2],
      ...(validMutationSeq(item[3]) ? { mutationSeq: item[3] } : {}) });
  }
  return values;
}

export type KnowhowCellChannel = {
  post: (values: readonly KnowhowCellValue[]) => void;
  close: () => void;
};

const NOOP_CHANNEL: KnowhowCellChannel = { post: () => undefined, close: () => undefined };

/**
 * 打开一张表的单元格频道。浏览器不支持 BroadcastChannel、构造抛错、发送抛错（例如频道已
 * 关）时都静默降级——广播只是省掉同浏览器自撞的 409，不是保存成功的前提。
 */
export function openKnowhowCellChannel(
  tableId: string,
  onValues: (values: KnowhowCellValue[]) => void,
): KnowhowCellChannel {
  const Channel = (globalThis as { BroadcastChannel?: typeof BroadcastChannel }).BroadcastChannel;
  if (typeof Channel !== "function") return NOOP_CHANNEL;
  let channel: BroadcastChannel;
  try {
    channel = new Channel(knowhowCellChannelName(tableId));
  } catch {
    return NOOP_CHANNEL;
  }
  let closed = false;
  channel.onmessage = (event: MessageEvent) => {
    if (closed) return;
    const values = decodeCellValues(event.data);
    if (values && values.length > 0) onValues(values);
  };
  return {
    post: (values) => {
      if (closed || values.length === 0) return;
      try {
        channel.postMessage(encodeCellValues(values));
      } catch {
        /* 频道不可用：降级为不广播 */
      }
    },
    close: () => {
      if (closed) return;
      closed = true;
      try {
        channel.close();
      } catch {
        /* ignore */
      }
    },
  };
}

// ---------------------------------------------------------------------------
// hook：面板持有的基线与频道
// ---------------------------------------------------------------------------

export type UseKnowhowCellSyncInput = {
  /** 当前选中的表；频道按表开，切表即关。 */
  tableId: string | null;
  detail: KnowhowTableDetail | null;
  setDetail: Dispatch<SetStateAction<KnowhowTableDetail | null>>;
  /** 正开着编辑态的格子（null = 没有）。 */
  editingRowId: string | null;
  editingColumnId: string | null;
  /** 「优化整行」浮层作用的行（null = 没开）：打开时钉住这一行的每一列。 */
  optimizeRowId: string | null;
  /**
   * 整表详情请求是否在途（面板的 loadDetail 发起时置位、落定时清掉并调 `settleDetailRequest`）。
   * 在途期间收到的广播和本页成功保存的值**排队**：那次请求的快照可能早于这次保存，先应用写入、再被
   * 请求结果整体覆盖，表数据就退回旧值，下一次打开编辑器钉住旧值、保存时撞上本浏览器自己
   * 写的值（409）。缺省 = 永不在途（测试/预览）。
   */
  detailInFlight?: () => boolean;
};

/**
 * 整表详情请求在途时本页保存或收到广播的存储值先放这里，请求落定后叠在请求结果之上（见
 * `UseKnowhowCellSyncInput.detailInFlight`）。只收同一张表的值；切表时清空。
 *
 * 合并仍逐格检查提交版本：详情若更新，不能被排队的旧值盖回去。
 */
export type QueuedCellValues = { tableId: string; values: KnowhowCellValue[] };

export function queueCellValues(
  queue: QueuedCellValues | null,
  tableId: string,
  values: readonly KnowhowCellValue[],
): QueuedCellValues {
  if (!queue || queue.tableId !== tableId) return { tableId, values: [...values] };
  return { tableId, values: [...queue.values, ...values] };
}

export function useKnowhowCellSync({
  tableId,
  detail,
  setDetail,
  editingRowId,
  editingColumnId,
  optimizeRowId,
  detailInFlight,
}: UseKnowhowCellSyncInput) {
  const pinsRef = useRef<PinnedCells>(new Map());
  const channelRef = useRef<KnowhowCellChannel>(NOOP_CHANNEL);
  const queueRef = useRef<QueuedCellValues | null>(null);
  // 同一张表离开后重开也是新一次访问，不能只凭 tableId 接受上一次的迟到写回。
  const tableScope = useMemo(() => ({ tableId, observedValues: new Map<string, KnowhowCellValue>() }), [tableId]);
  const observedValues = tableScope.observedValues;
  const activeTableScopeRef = useRef(tableScope);
  // 判据收进 ref:频道回调在打开那一刻注册,要读到的是**收到消息时**的在途状态。
  const detailInFlightRef = useRef(detailInFlight);
  detailInFlightRef.current = detailInFlight;
  // 钉住要读「打开那一刻」的表数据：layout effect 在同一次提交的 passive effect 之前跑，
  // 下面两个钉住 effect 读到的就是让编辑器出现的那一次渲染的表数据。
  const detailRef = useRef(detail);
  useLayoutEffect(() => {
    detailRef.current = detail;
    activeTableScopeRef.current = tableScope;
  }, [detail, tableScope]);

  const acceptStored = useCallback(<T extends KnowhowCellValue>(values: readonly T[]): T[] => {
    const current = detailRef.current?.id === tableScope.tableId ? detailRef.current : null;
    const accepted: T[] = [];
    for (const value of values) {
      const key = cellKey(value.rowId, value.columnId);
      const order = cellValueOrder(current, value, observedValues.get(key));
      if (order === "matching_older") {
        // A newer whole-table read may only have changed ANOTHER cell. A late
        // own-browser commit whose value matches the newer snapshot can still
        // advance the editor baseline, without applying/broadcasting old data.
        advancePinnedCells(pinsRef.current, [value]);
      }
      if (order !== "current") continue;
      observedValues.set(key, value);
      accepted.push(value);
    }
    return accepted;
  }, [observedValues, tableScope]);

  useEffect(() => {
    if (!tableId) return;
    const channel = openKnowhowCellChannel(tableId, (incoming) => {
      const values = acceptStored(incoming);
      if (values.length === 0) return;
      // 基线立刻前移:在途期间用户点保存,带的也该是本浏览器刚写的那份。
      advancePinnedCells(pinsRef.current, values);
      if (detailInFlightRef.current?.()) {
        queueRef.current = queueCellValues(queueRef.current, tableId, values);
        return;
      }
      setDetail((prev) => (prev && prev.id === tableId ? applyCellValues(prev, values) : prev));
    });
    channelRef.current = channel;
    return () => {
      channel.close();
      if (channelRef.current === channel) channelRef.current = NOOP_CHANNEL;
      queueRef.current = null;
    };
  }, [tableId, setDetail, acceptStored]);

  /** 整表详情请求落定（成功、失败或被作废都调）：把在途期间排队的存储值叠到表数据上。 */
  const settleDetailRequest = useCallback(() => {
    const queued = queueRef.current;
    queueRef.current = null;
    if (!queued || queued.values.length === 0) return;
    setDetail((prev) => (prev && prev.id === queued.tableId ? applyCellValues(prev, queued.values) : prev));
  }, [setDetail]);

  useEffect(() => {
    const current = detailRef.current;
    if (!editingRowId || !editingColumnId || !current) return;
    const pins = pinsRef.current;
    // A browser write may already be observed but still queued behind a detail
    // read when this editor opens. Seed NEW pins from that ordered overlay;
    // pinCells keeps any existing draft's held baseline unchanged.
    const pinDetail = applyCellValues(current, [...observedValues.values()]);
    const cells = cellsToPin(pinDetail, editingRowId, [editingColumnId]);
    pinCells(pins, pinDetail, cells);
    return () => unpinCells(pins, cells);
  }, [editingRowId, editingColumnId, observedValues]);

  useEffect(() => {
    const current = detailRef.current;
    if (!optimizeRowId || !current) return;
    const pins = pinsRef.current;
    const pinDetail = applyCellValues(current, [...observedValues.values()]);
    const cells = cellsToPin(pinDetail, optimizeRowId, pinDetail.columns.map((column) => column.id));
    pinCells(pins, pinDetail, cells);
    return () => unpinCells(pins, cells);
  }, [optimizeRowId, observedValues]);

  /** 这次手动保存要带的守卫（见 cellSaveBaseline）。 */
  const baselineFor = useCallback(
    (current: KnowhowTableDetail, rowId: string, columnId: string, overwrite?: KnowhowCellConflict) => {
      const latestConflict = overwrite && { ...overwrite, current: overwrite.current.map((value) => {
        const observed = observedValues.get(cellKey(value.rowId, value.columnId));
        // A conflict dialog may outlive another save in this browser. Its old
        // snapshot must not replace the newer baseline on "overwrite" either.
        return observed && cellValueOrder(null, value, observed) !== "current" ? observed : value;
      }) };
      return cellSaveBaseline(current, pinsRef.current, rowId, columnId, latestConflict);
    },
    [observedValues],
  );

  /** 保存成功：前移基线，并与广播共用在途队列，避免较早的读取覆盖本页写后值。 */
  const recordStored = useCallback(<T extends KnowhowCellValue>(incoming: readonly T[]): T[] | null => {
    // 保存响应可能在切表之后才落地，不能让旧闭包替换新表的队列或向新频道广播。
    if (!tableScope.tableId || activeTableScopeRef.current !== tableScope) return null;
    const values = acceptStored(incoming);
    advancePinnedCells(pinsRef.current, values);
    if (detailInFlightRef.current?.()) {
      queueRef.current = queueCellValues(queueRef.current, tableScope.tableId, values);
    }
    channelRef.current.post(values);
    return values;
  }, [tableScope, acceptStored]);

  /** 「放弃我的」：接受 409 时取回的当前存储值——前移基线、更新本地表数据。 */
  const adoptCurrent = useCallback((incoming: readonly KnowhowCellValue[]): KnowhowCellValue[] => {
    if (!tableScope.tableId || activeTableScopeRef.current !== tableScope) return [];
    const values = acceptStored(incoming);
    const current = detailRef.current?.id === tableScope.tableId ? detailRef.current : null;
    const adopted = incoming.map((value) => knownCellValues(current, value, observedValues.get(cellKey(value.rowId, value.columnId)))
      .reduce<KnowhowCellValue>((latest, candidate) => !validMutationSeq(latest.mutationSeq)
        || candidate.mutationSeq >= latest.mutationSeq ? candidate : latest, value));
    // Discard is an explicit acceptance of what the editor will show. A newer
    // read snapshot may supersede the old conflict value; advance these target
    // pins to the resolved values, without treating the snapshot as a commit
    // or broadcasting it. Ordinary background reads still leave pins alone.
    advancePinnedCells(pinsRef.current, adopted);
    if (detailInFlightRef.current?.()) {
      queueRef.current = queueCellValues(queueRef.current, tableScope.tableId, values);
    }
    setDetail((prev) => (prev && prev.id === tableScope.tableId ? applyCellValues(prev, values) : prev));
    return adopted;
  }, [setDetail, tableScope, acceptStored, observedValues]);

  /** Only the local successful legacy response enters this reconciliation.
   * The snapshot revision labels an observation, never the unversioned write:
   * confirm matching cells' baselines without broadcasting a fabricated commit.
   * A newer known browser write also proves why the saved value changed; an
   * otherwise unexplained change keeps the draft in the existing conflict UI. */
  const reconcileLegacyStored = useCallback((values: readonly KnowhowCellValue[], fresh: KnowhowTableDetail): KnowhowCellConflict | null => {
    if (activeTableScopeRef.current !== tableScope || fresh.id !== tableScope.tableId) return null;
    const current: KnowhowCellValue[] = [];
    let changed = false;
    for (const value of values) {
      const row = fresh.rows.find((item) => item.id === value.rowId);
      if (!row || !fresh.columns.some((column) => column.id === value.columnId)) return { reason: "group_changed", current: [] };
      const snapshot = { rowId: value.rowId, columnId: value.columnId,
        contentMd: row.cells[value.columnId] ?? "", mutationSeq: fresh.mutationSeq };
      const observed = observedValues.get(cellKey(value.rowId, value.columnId));
      const knownBrowserWrite = validMutationSeq(observed?.mutationSeq)
        && (observed.contentMd === snapshot.contentMd
          || (validMutationSeq(fresh.mutationSeq) && observed.mutationSeq > fresh.mutationSeq));
      if (snapshot.contentMd !== value.contentMd && !knownBrowserWrite) changed = true;
      current.push(snapshot);
    }
    if (changed) return { reason: "content_changed", current };
    adoptCurrent(current);
    return null;
  }, [tableScope, observedValues, adoptCurrent]);

  /** 冲突后整表刷新成功：钉住的格子改钉到刷新后的值。 */
  const repinFrom = useCallback((fresh: KnowhowTableDetail) => {
    repinCells(pinsRef.current, applyCellValues(fresh, [...observedValues.values()]));
  }, [observedValues]);

  return { baselineFor, recordStored, adoptCurrent, reconcileLegacyStored, repinFrom, settleDetailRequest };
}
