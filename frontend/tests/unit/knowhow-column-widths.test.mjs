import test from "node:test";
import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";

import {
  KNOWHOW_WIDTH_CODE_UNIT_LIMIT,
  KNOWHOW_WIDTH_GRAPHEME_LIMIT,
  computeKnowhowColumnWidths,
  estimateGraphemeUnits,
  estimateMarkdownVisibleLineUnits,
  sampleVisibleKnowhowRows,
} from "../../app/knowhow-column-widths.ts";

function row(position, cells = {}) {
  return { id: `r-${position}`, position, projectionStatus: "synced", cells };
}

test("grapheme 权重区分 CJK/全角/emoji、ASCII、标点、空格与 tab", () => {
  assert.equal(estimateGraphemeUnits("中Ａ🙂"), 6);
  assert.equal(estimateGraphemeUnits("Ab_9"), 4);
  assert.equal(estimateGraphemeUnits(".,!?"), 3);
  assert.equal(estimateGraphemeUnits(" \t"), 1.5);
});

test("Markdown 摘要忽略结构 marker 与 link URL，只取最长可见行和最长表格 cell", () => {
  assert.equal(
    estimateMarkdownVisibleLineUnits("# **短标题**\r\n- [可见文字](https://example.test/a/very/long/path)"),
    8,
  );
  assert.equal(estimateMarkdownVisibleLineUnits("短\nabcdefgh\n中等"), 8);
  assert.equal(estimateMarkdownVisibleLineUnits("| 短 | abcdef | 中 |"), 6);
  assert.equal(estimateMarkdownVisibleLineUnits("```ts"), 0);
});

test("Markdown 摘要最多读取前 8 个可见行和每行前 120 个 grapheme", () => {
  const ninthLine = [...Array(8).fill("x"), "这是第九行，不应参与估算"].join("\n");
  assert.equal(estimateMarkdownVisibleLineUnits(ninthLine), 1);
  assert.equal(
    estimateMarkdownVisibleLineUnits("a".repeat(KNOWHOW_WIDTH_GRAPHEME_LIMIT + 40)),
    KNOWHOW_WIDTH_GRAPHEME_LIMIT,
  );
});

test("Markdown 摘要在 Markdown 简化前截断 code-unit，100k/1M 尾部不造成线性放大", () => {
  const prefix = "a".repeat(KNOWHOW_WIDTH_CODE_UNIT_LIMIT);
  const medium = prefix + "\rX".repeat(50_000);
  const huge = prefix + "\rX".repeat(500_000);
  assert.equal(estimateMarkdownVisibleLineUnits(medium), KNOWHOW_WIDTH_GRAPHEME_LIMIT);
  assert.equal(estimateMarkdownVisibleLineUnits(huge), KNOWHOW_WIDTH_GRAPHEME_LIMIT);

  const measure = (value) => {
    for (let index = 0; index < 5; index += 1) estimateMarkdownVisibleLineUnits(value);
    const started = performance.now();
    for (let index = 0; index < 40; index += 1) estimateMarkdownVisibleLineUnits(value);
    return performance.now() - started;
  };
  const mediumMs = measure(medium);
  const hugeMs = measure(huge);

  // 允许慢速/抖动 CI 有 50ms 固定余量；旧实现先对整个字符串 replace/split，
  // 输入扩大 10 倍时耗时也近似扩大 10 倍，会越过这个比例保险丝。
  assert.ok(
    hugeMs <= mediumMs * 4 + 50,
    `1M tail scaled ${mediumMs.toFixed(1)}ms -> ${hugeMs.toFixed(1)}ms`,
  );
});

test("可见物理行采样固定为前 48 + 后 16，短输入不重复", () => {
  const rows = Array.from({ length: 100 }, (_, index) => row(index));
  const sampled = sampleVisibleKnowhowRows(rows);
  assert.equal(sampled.length, 64);
  assert.deepEqual(sampled.map((item) => item.position), [
    ...Array.from({ length: 48 }, (_, index) => index),
    ...Array.from({ length: 16 }, (_, index) => index + 84),
  ]);

  const short = Array.from({ length: 63 }, (_, index) => row(index));
  const shortSample = sampleVisibleKnowhowRows(short);
  assert.equal(shortSample.length, 63);
  assert.equal(new Set(shortSample.map((item) => item.id)).size, 63);
  assert.notEqual(shortSample, short);
});

test("桌面列宽按 role clamp、status 固定且全部 4px 对齐", () => {
  const columns = [
    { id: "anchor", name: "题", role: "attribute", position: 0 },
    { id: "procedure", name: "步骤", role: "procedure", position: 1 },
    { id: "entity", name: "工具", role: "entity", position: 2 },
    { id: "attribute", name: "备注", role: "attribute", position: 3 },
  ];
  const rows = [row(0, {
    anchor: "a".repeat(100),
    procedure: "a".repeat(120),
    entity: "a",
    attribute: "a".repeat(120),
  })];
  const widths = computeKnowhowColumnWidths({ columns, visibleRowsSample: rows, anchorColumnId: "anchor", compact: false });
  assert.deepEqual(widths.columns.map((item) => item.widthPx), [320, 520, 160, 420]);
  assert.equal(widths.statusWidthPx, 112);
  assert.ok(widths.columns.every((item) => item.widthPx % 4 === 0));
  assert.equal(widths.tableWidthPx, 1532);
});

test("窄屏使用紧 clamp 并继续返回完整 table 宽度", () => {
  const columns = [
    { id: "anchor", name: "题", role: "anchor", position: 0 },
    { id: "procedure", name: "步骤", role: "procedure", position: 1 },
    { id: "entity", name: "工具", role: "entity", position: 2 },
    { id: "attribute", name: "备注", role: "attribute", position: 3 },
  ];
  const rows = [row(0, Object.fromEntries(columns.map((column) => [column.id, "a".repeat(120)])))];
  const widths = computeKnowhowColumnWidths({ columns, visibleRowsSample: rows, anchorColumnId: "anchor", compact: true });
  assert.deepEqual(widths.columns.map((item) => item.widthPx), [280, 380, 320, 320]);
  assert.equal(widths.statusWidthPx, 104);
  assert.equal(widths.tableWidthPx, 1404);
});
