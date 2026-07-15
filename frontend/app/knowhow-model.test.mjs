import test from "node:test";
import assert from "node:assert/strict";

import { rewriteAssetUrls, cellSummary, ROLE_LABELS } from "./knowhow-model.ts";

// --- ROLE_LABELS -------------------------------------------------------------

test("ROLE_LABELS: 覆盖全部六个角色，文案与规格一致", () => {
  assert.deepStrictEqual(ROLE_LABELS, {
    concept: "概念",
    identify: "现象识别",
    root_cause: "根因分析",
    fix: "修复方法",
    tool: "依赖工具",
    plain: "普通",
  });
});

test("ROLE_LABELS: 每个角色的文案都是非空字符串", () => {
  for (const role of ["concept", "identify", "root_cause", "fix", "tool", "plain"]) {
    assert.ok(typeof ROLE_LABELS[role] === "string" && ROLE_LABELS[role].length > 0, `role ${role} 缺少文案`);
  }
});

// --- rewriteAssetUrls ---------------------------------------------------------

test("rewriteAssetUrls: 单张 asset:// 图片改写为鉴权 API URL", () => {
  const md = "![截图](asset://a1)";
  const out = rewriteAssetUrls(md, "nb-1", "http://api.test/api");
  assert.strictEqual(out, "![截图](http://api.test/api/notebooks/nb-1/assets/a1)");
});

test("rewriteAssetUrls: 多张图片全部改写，非图片文本不受影响", () => {
  const md = "步骤一：\n![图1](asset://a1)\n步骤二：\n![图2](asset://a2)\n完成。";
  const out = rewriteAssetUrls(md, "nb-1", "http://api.test/api");
  assert.strictEqual(
    out,
    "步骤一：\n![图1](http://api.test/api/notebooks/nb-1/assets/a1)\n步骤二：\n![图2](http://api.test/api/notebooks/nb-1/assets/a2)\n完成。",
  );
});

test("rewriteAssetUrls: 无图片时原样返回", () => {
  const md = "这一格只有纯文本说明，没有任何图片引用。";
  assert.strictEqual(rewriteAssetUrls(md, "nb-1", "http://api.test/api"), md);
});

test("rewriteAssetUrls: 非 asset 协议的图片/链接不动", () => {
  const md = "![外链图片](https://example.com/pic.png) 与 [普通链接](https://example.com) 都不应被改写。";
  assert.strictEqual(rewriteAssetUrls(md, "nb-1", "http://api.test/api"), md);
});

test("rewriteAssetUrls: 空字符串原样返回", () => {
  assert.strictEqual(rewriteAssetUrls("", "nb-1", "http://api.test/api"), "");
});

// --- cellSummary ---------------------------------------------------------------

test("cellSummary: 图片剥离为图示占位文案（保留 alt 线索）", () => {
  const md = "步骤如下：\n![示意图](asset://x1)\n请参考上图完成操作。";
  const out = cellSummary(md);
  assert.ok(!out.includes("!["), "不应残留 markdown 图片语法");
  assert.ok(out.includes("（图示：示意图）"), "应替换为图示占位文案");
});

test("cellSummary: 无 alt 文本的图片使用无冒号占位", () => {
  assert.strictEqual(cellSummary("![](asset://x1)"), "（图示）");
});

test("cellSummary: 多张图片各自剥离", () => {
  const out = cellSummary("![图A](asset://a)中间文字![图B](asset://b)");
  assert.strictEqual(out, "（图示：图A）中间文字（图示：图B）");
});

test("cellSummary: 去除常见 md 记号（加粗/列表项）", () => {
  const out = cellSummary("**加粗文字**\n- 列表项一\n- 列表项二");
  assert.ok(!out.includes("**"), "不应残留加粗记号");
  assert.ok(!out.includes("- "), "不应残留列表符号");
  assert.strictEqual(out, "加粗文字 列表项一 列表项二");
});

test("cellSummary: 去除标题/行内代码记号", () => {
  const out = cellSummary("### 标题\n请运行 `foo --bar` 命令");
  assert.strictEqual(out, "标题 请运行 foo --bar 命令");
});

test("cellSummary: 超过默认 maxLen(80) 时截断并加省略号", () => {
  const longText = "这是一段很长的修复方法说明，".repeat(10); // 140 字符，无空白
  const out = cellSummary(longText);
  assert.ok(out.length <= 80, `长度应 <=80，实际 ${out.length}`);
  assert.ok(out.endsWith("…"), "截断后应以省略号结尾");
  assert.notStrictEqual(out, longText);
});

test("cellSummary: 恰好等于 maxLen 时不截断、不加省略号", () => {
  const exact = "字".repeat(80);
  const out = cellSummary(exact);
  assert.strictEqual(out, exact);
  assert.ok(!out.includes("…"));
});

test("cellSummary: 自定义 maxLen 参数生效", () => {
  const out = cellSummary("一二三四五六七八九十", 5);
  assert.ok(out.length <= 5, `长度应 <=5，实际 ${out.length}`);
  assert.ok(out.endsWith("…"));
});

test("cellSummary: 空格子（空串/纯空白）返回空串", () => {
  assert.strictEqual(cellSummary(""), "");
  assert.strictEqual(cellSummary("   \n   "), "");
});
