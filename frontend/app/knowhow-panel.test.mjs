// knowhow-panel.tsx 的纯逻辑单测。knowhow-panel.tsx 本身含 JSX，Node 原生
// TS 类型剥离不支持 .tsx（仅 .ts/.mts/.cts），因此可测纯逻辑一律抽到
// knowhow-panel-logic.ts（无 JSX）导出，本文件直接 import 该文件测试。
import test from "node:test";
import assert from "node:assert/strict";

import {
  filterRows,
  sortColumnsByPosition,
  orderColumnsForGrid,
  PROJECTION_STATUS_LABELS,
  PROJECTION_STATUS_TONE,
  isRetryableProjectionStatus,
  resolveRowTitleText,
  isInternalAssetUrl,
} from "./knowhow-panel-logic.ts";

// --- fixtures ------------------------------------------------------------------

const columns = [
  { id: "c-tool", name: "依赖工具", role: "tool", position: 3 },
  { id: "c-concept", name: "概念", role: "concept", position: 0 },
  { id: "c-fix", name: "修复方法", role: "fix", position: 2 },
  { id: "c-identify", name: "现象识别", role: "identify", position: 1 },
];

function row(id, cells) {
  return { id, position: 0, projectionStatus: "synced", cells };
}

// --- filterRows ------------------------------------------------------------------

test("filterRows: 空查询返回全部行（保持原序）", () => {
  const rows = [row("r1", { a: "foo" }), row("r2", { a: "bar" })];
  assert.deepStrictEqual(filterRows(rows, ""), rows);
});

test("filterRows: 空白查询（仅空格）等同空查询", () => {
  const rows = [row("r1", { a: "foo" })];
  assert.deepStrictEqual(filterRows(rows, "   "), rows);
});

test("filterRows: 按概念列内容匹配", () => {
  const rows = [
    row("r1", { "c-concept": "时序违例", "c-fix": "调整约束" }),
    row("r2", { "c-concept": "IR Drop", "c-fix": "加电源环" }),
  ];
  const out = filterRows(rows, "时序");
  assert.deepStrictEqual(out.map((r) => r.id), ["r1"]);
});

test("filterRows: 按非概念列(全文)内容匹配", () => {
  const rows = [
    row("r1", { "c-concept": "时序违例", "c-fix": "调整 clk_out_en 约束" }),
    row("r2", { "c-concept": "IR Drop", "c-fix": "加电源环" }),
  ];
  const out = filterRows(rows, "clk_out_en");
  assert.deepStrictEqual(out.map((r) => r.id), ["r1"]);
});

test("filterRows: 大小写不敏感", () => {
  const rows = [row("r1", { a: "Place_Opt_Design" })];
  assert.deepStrictEqual(filterRows(rows, "place_opt_design"), rows);
});

test("filterRows: 无匹配返回空数组", () => {
  const rows = [row("r1", { a: "foo" })];
  assert.deepStrictEqual(filterRows(rows, "不存在的字符串"), []);
});

test("filterRows: 多行部分匹配，保留原相对顺序", () => {
  const rows = [
    row("r1", { a: "alpha" }),
    row("r2", { a: "beta" }),
    row("r3", { a: "alpha beta" }),
  ];
  assert.deepStrictEqual(filterRows(rows, "beta").map((r) => r.id), ["r2", "r3"]);
});

test("filterRows: 单元格为空串时不报错、不匹配", () => {
  const rows = [row("r1", { a: "" })];
  assert.deepStrictEqual(filterRows(rows, "x"), []);
});

// --- sortColumnsByPosition ------------------------------------------------------

test("sortColumnsByPosition: 按 position 升序排序", () => {
  const out = sortColumnsByPosition(columns);
  assert.deepStrictEqual(out.map((c) => c.id), ["c-concept", "c-identify", "c-fix", "c-tool"]);
});

test("sortColumnsByPosition: 不修改原数组", () => {
  const copy = [...columns];
  sortColumnsByPosition(columns);
  assert.deepStrictEqual(columns, copy);
});

test("sortColumnsByPosition: 空数组返回空数组", () => {
  assert.deepStrictEqual(sortColumnsByPosition([]), []);
});

// --- orderColumnsForGrid ---------------------------------------------------------

test("orderColumnsForGrid: 概念列钉首列，其余按 position 跟随", () => {
  const out = orderColumnsForGrid(columns);
  assert.deepStrictEqual(out.map((c) => c.id), ["c-concept", "c-identify", "c-fix", "c-tool"]);
});

test("orderColumnsForGrid: 概念列不在 position 0 时仍钉首列", () => {
  const shuffled = [
    { id: "c-identify", name: "现象识别", role: "identify", position: 0 },
    { id: "c-concept", name: "概念", role: "concept", position: 1 },
    { id: "c-fix", name: "修复方法", role: "fix", position: 2 },
  ];
  const out = orderColumnsForGrid(shuffled);
  assert.deepStrictEqual(out.map((c) => c.id), ["c-concept", "c-identify", "c-fix"]);
});

test("orderColumnsForGrid: 概念列已在首位时保持不变", () => {
  const alreadyFirst = [
    { id: "c-concept", name: "概念", role: "concept", position: 0 },
    { id: "c-fix", name: "修复方法", role: "fix", position: 1 },
  ];
  const out = orderColumnsForGrid(alreadyFirst);
  assert.deepStrictEqual(out.map((c) => c.id), ["c-concept", "c-fix"]);
});

test("orderColumnsForGrid: 无概念角色列时退化为纯 position 排序", () => {
  const noConcept = [
    { id: "c-fix", name: "修复方法", role: "fix", position: 1 },
    { id: "c-tool", name: "依赖工具", role: "tool", position: 0 },
  ];
  const out = orderColumnsForGrid(noConcept);
  assert.deepStrictEqual(out.map((c) => c.id), ["c-tool", "c-fix"]);
});

test("orderColumnsForGrid: 不修改原数组", () => {
  const copy = [...columns];
  orderColumnsForGrid(columns);
  assert.deepStrictEqual(columns, copy);
});

// --- 投影状态徽标映射 -----------------------------------------------------------

test("PROJECTION_STATUS_LABELS: 四态文案与规格一致", () => {
  assert.deepStrictEqual(PROJECTION_STATUS_LABELS, {
    pending: "待同步",
    syncing: "同步中",
    synced: "已同步",
    failed: "同步失败·可重试",
  });
});

test("PROJECTION_STATUS_TONE: 覆盖四态且取值合法", () => {
  const allowed = new Set(["neutral", "info", "success", "danger"]);
  for (const status of ["pending", "syncing", "synced", "failed"]) {
    assert.ok(allowed.has(PROJECTION_STATUS_TONE[status]), `status ${status} tone 非法`);
  }
});

test("isRetryableProjectionStatus: 仅 failed 可重试", () => {
  assert.strictEqual(isRetryableProjectionStatus("failed"), true);
  assert.strictEqual(isRetryableProjectionStatus("pending"), false);
  assert.strictEqual(isRetryableProjectionStatus("syncing"), false);
  assert.strictEqual(isRetryableProjectionStatus("synced"), false);
});

// --- resolveRowTitleText ---------------------------------------------------------

test("resolveRowTitleText: 取概念列原始文本", () => {
  const r = row("r1", { "c-concept": "时序违例", "c-fix": "调整约束" });
  assert.strictEqual(resolveRowTitleText(r, columns), "时序违例");
});

test("resolveRowTitleText: 无概念角色列时退化为 position 最小的列", () => {
  const noConcept = [
    { id: "c-fix", name: "修复方法", role: "fix", position: 1 },
    { id: "c-tool", name: "依赖工具", role: "tool", position: 0 },
  ];
  const r = row("r1", { "c-tool": "innovus", "c-fix": "xxx" });
  assert.strictEqual(resolveRowTitleText(r, noConcept), "innovus");
});

test("resolveRowTitleText: 无列时返回空串", () => {
  assert.strictEqual(resolveRowTitleText(row("r1", {}), []), "");
});

test("resolveRowTitleText: 概念列存在但该行缺少该单元格时返回空串", () => {
  const r = row("r1", { "c-fix": "xxx" });
  assert.strictEqual(resolveRowTitleText(r, columns), "");
});

// --- isInternalAssetUrl -----------------------------------------------------------

test("isInternalAssetUrl: 本 API 资产 URL 判定为内部（需带鉴权抓取）", () => {
  const apiBase = "http://api.test/api";
  const url = `${apiBase}/notebooks/nb-1/assets/a1`;
  assert.strictEqual(isInternalAssetUrl(url, apiBase), true);
});

test("isInternalAssetUrl: 外部图床 URL 判定为非内部（不附带鉴权头，避免令牌外泄）", () => {
  const apiBase = "http://api.test/api";
  assert.strictEqual(isInternalAssetUrl("https://example.com/pic.png", apiBase), false);
});

test("isInternalAssetUrl: 相对 apiBase（同源反代场景）同样生效", () => {
  const apiBase = "/api";
  assert.strictEqual(isInternalAssetUrl("/api/notebooks/nb-1/assets/a1", apiBase), true);
});

test("isInternalAssetUrl: apiBase 前缀相同但非 assets 路径不算内部资产", () => {
  const apiBase = "http://api.test/api";
  assert.strictEqual(isInternalAssetUrl(`${apiBase}/notebooks/nb-1/knowhow`, apiBase), false);
});
