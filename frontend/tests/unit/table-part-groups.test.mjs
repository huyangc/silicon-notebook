import test from "node:test";
import assert from "node:assert/strict";

import { composeTablePartsHtml, groupTableParts, sanitizeTableHtml } from "../../app/table-part-groups.ts";

const el = (element_type, metadata = {}, id = element_type) => ({ id, element_type, metadata });
const sectioned = (element, divider = null) => ({ element, divider });
// 分组测试默认都带 table_html——真实数据里 table 元素必有它,单独测 table_html 缺失
// 的回落见下面专门的用例。
const tableEl = (metadata, id) => el("table", { table_html: "<table><tr><td>x</td></tr></table>", ...metadata }, id);

test("groupTableParts merges consecutive table_part elements sharing a table_group", () => {
  const items = [
    sectioned(tableEl({ table_group: "t1", table_part: 1, table_parts: 3 }, "a")),
    sectioned(tableEl({ table_group: "t1", table_part: 2, table_parts: 3 }, "b")),
    sectioned(tableEl({ table_group: "t1", table_part: 3, table_parts: 3 }, "c")),
  ];
  const groups = groupTableParts(items);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].kind, "table_group");
  assert.deepEqual(groups[0].elements.map((e) => e.id), ["a", "b", "c"]);
});

test("groupTableParts breaks the run when an unrelated element sits in between", () => {
  const items = [
    sectioned(tableEl({ table_group: "t1", table_part: 1, table_parts: 2 }, "a")),
    sectioned(el("paragraph", {}, "mid")),
    sectioned(tableEl({ table_group: "t1", table_part: 2, table_parts: 2 }, "b")),
  ];
  const groups = groupTableParts(items);
  assert.deepEqual(
    groups.map((g) => (g.kind === "single" ? g.element.id : g.elements.map((e) => e.id))),
    ["a", "mid", "b"],
  );
  assert.equal(groups[0].kind, "single");
  assert.equal(groups[2].kind, "single");
});

test("groupTableParts keeps different table_group values apart even when adjacent", () => {
  const items = [
    sectioned(tableEl({ table_group: "t1", table_part: 1, table_parts: 2 }, "a")),
    sectioned(tableEl({ table_group: "t1", table_part: 2, table_parts: 2 }, "b")),
    sectioned(tableEl({ table_group: "t2", table_part: 1, table_parts: 2 }, "c")),
    sectioned(tableEl({ table_group: "t2", table_part: 2, table_parts: 2 }, "d")),
  ];
  const groups = groupTableParts(items);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0].elements.map((e) => e.id), ["a", "b"]);
  assert.deepEqual(groups[1].elements.map((e) => e.id), ["c", "d"]);
});

test("groupTableParts stops a run when table_part is not the next consecutive integer", () => {
  const items = [
    sectioned(tableEl({ table_group: "t1", table_part: 1, table_parts: 3 }, "a")),
    sectioned(tableEl({ table_group: "t1", table_part: 3, table_parts: 3 }, "b")), // 跳过 2
  ];
  const groups = groupTableParts(items);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].kind, "single");
  assert.equal(groups[1].kind, "single");
});

test("groupTableParts leaves non-table elements and tables without a group as singles", () => {
  const items = [
    sectioned(el("paragraph", {}, "p1")),
    sectioned(tableEl({}, "t-no-group")),
    sectioned(el("heading", { heading_level: 1 }, "h1")),
  ];
  const groups = groupTableParts(items);
  assert.deepEqual(groups.map((g) => g.element.id), ["p1", "t-no-group", "h1"]);
  assert.ok(groups.every((g) => g.kind === "single"));
});

test("groupTableParts collapses a single-member run back to a single", () => {
  const items = [sectioned(tableEl({ table_group: "t1", table_part: 1, table_parts: 1 }, "only"))];
  const groups = groupTableParts(items);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].kind, "single");
  assert.equal(groups[0].element.id, "only");
});

test("groupTableParts merges a window that starts mid-table (part 3 first)", () => {
  const items = [
    sectioned(tableEl({ table_group: "t1", table_part: 3, table_parts: 5 }, "c")),
    sectioned(tableEl({ table_group: "t1", table_part: 4, table_parts: 5 }, "d")),
    sectioned(tableEl({ table_group: "t1", table_part: 5, table_parts: 5 }, "e")),
  ];
  const groups = groupTableParts(items);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].kind, "table_group");
  assert.deepEqual(groups[0].elements.map((e) => e.id), ["c", "d", "e"]);
});

test("groupTableParts carries the divider from the group's first element", () => {
  const items = [
    sectioned(tableEl({ table_group: "t1", table_part: 1, table_parts: 2 }, "a"), "工作表 Sheet1"),
    sectioned(tableEl({ table_group: "t1", table_part: 2, table_parts: 2 }, "b"), null),
  ];
  const groups = groupTableParts(items);
  assert.equal(groups[0].divider, "工作表 Sheet1");
});

test("groupTableParts falls back to single when a part is missing table_html, even with matching group/part metadata", () => {
  const items = [
    sectioned(tableEl({ table_group: "t1", table_part: 1, table_parts: 3 }, "a")),
    // b 具备 table_group/table_part,但没有 table_html(或是空字符串)——不能合并,
    // 单元素渲染路径本来就会因为 `if (html)` 为假而落到默认分支,分组不能制造出
    // 一个「合并卡片里缺一段内容」的空洞。
    sectioned(el("table", { table_group: "t1", table_part: 2, table_parts: 3 }, "b")),
    sectioned(el("table", { table_group: "t1", table_part: 2, table_parts: 3, table_html: "" }, "b2")),
    sectioned(tableEl({ table_group: "t1", table_part: 1, table_parts: 3 }, "c")),
  ];
  const groups = groupTableParts(items);
  assert.deepEqual(groups.map((g) => g.kind), ["single", "single", "single", "single"]);
  assert.deepEqual(groups.map((g) => g.element.id), ["a", "b", "b2", "c"]);
});

test("composeTablePartsHtml produces one <table> with one <tbody> per part carrying its domId", () => {
  const parts = [
    { domId: "source-element-a", html: "<table><tr><th>H1</th><th>H2</th></tr><tr><td>1</td><td>2</td></tr></table>" },
    { domId: "source-element-b", html: "<table><tr><td>3</td><td>4</td></tr></table>" },
  ];
  const html = composeTablePartsHtml(parts, (s) => s);
  assert.equal((html.match(/<table>/g) || []).length, 1);
  assert.equal((html.match(/<\/table>/g) || []).length, 1);
  assert.equal((html.match(/<tbody/g) || []).length, 2);
  assert.ok(html.includes('id="source-element-a"'));
  assert.ok(html.includes('id="source-element-b"'));
  assert.ok(html.includes('class="table-part"'));
  assert.ok(html.includes("<th>H1</th>"));
  assert.ok(html.includes("<td>3</td>"));
  // 高亮不写进这段 HTML 字符串(见 table-part-groups.ts 顶部注释):composeTablePartsHtml
  // 不接收、也不产出 table-part--highlighted 类,由调用方事后按 domId 切 class。
  assert.ok(!html.includes("--highlighted"));
});

test("composeTablePartsHtml strips inner thead/tbody/tfoot wrapper tags and caption content", () => {
  const parts = [
    {
      domId: "source-element-a",
      html: "<table><caption>My table</caption><thead><tr><th>H</th></tr></thead><tbody><tr><td>1</td></tr></tbody><tfoot><tr><td>F</td></tr></tfoot></table>",
    },
  ];
  const html = composeTablePartsHtml(parts, (s) => s);
  assert.ok(!html.includes("My table"), "caption content is dropped");
  assert.ok(!html.includes("<thead"), "no nested thead");
  assert.ok(!html.includes("<tfoot"), "no nested tfoot");
  // 只保留最外层由 composeTablePartsHtml 自己生成的那一个 <tbody>。
  assert.equal((html.match(/<tbody/g) || []).length, 1);
  assert.ok(html.includes("<th>H</th>"));
  assert.ok(html.includes("<td>1</td>"));
  assert.ok(html.includes("<td>F</td>"));
});

test("composeTablePartsHtml runs each part's html through the given sanitize function, after unwrapping", () => {
  const seen = [];
  const sanitize = (raw) => {
    seen.push(raw);
    return raw;
  };
  const parts = [{ domId: "source-element-a", html: "<table><tr><td>ok</td></tr></table>" }];
  composeTablePartsHtml(parts, sanitize);
  assert.equal(seen.length, 1);
  // sanitize 看到的是已经剥掉外层 <table> 的行内容,不是原始整段 <table>...</table>——
  // 这就是「sanitize 是净化流水线里最后一步」的可观察证据:它拿到的输入已经是
  // unwrap 之后、即将直接拼进 innerHTML 的样子。
  assert.equal(seen[0], "<tr><td>ok</td></tr>");
});

test("sanitizeTableHtml keeps table markup and drops script tags/event handlers (re-exported unchanged)", () => {
  const html = sanitizeTableHtml('<table><tr><td onclick="x()">a</td></tr></table><script>bad()</script>');
  assert.ok(!html.includes("onclick"));
  assert.ok(!html.includes("<script"));
  assert.ok(html.includes("<table>"));
  assert.ok(html.includes("<td>a</td>") || html.includes("<td >a</td>"));
});

// 回归:sanitize 必须在 unwrap 之后跑(composeTablePartsHtml 内部顺序),否则剥掉
// <tbody>/<caption> 标签会把两侧本来无害的碎片重新拼成一个全新的、从未被净化过的
// 标签。这里用**真实**的 sanitizeTableHtml(不是测试桩),复现评审报告里的两条输入。
test("composeTablePartsHtml does not let unwrapping fabricate a new unsanitized tag (XSS regression)", () => {
  const attackViaTbody = "<table><tr><td><<tbody>img/src=x/onerror=alert(1)></td></tr></table>";
  const attackViaCaption = "<table><tr><td><<caption>x</caption>svg/onload=alert(1)></td></tr></table>";

  for (const attack of [attackViaTbody, attackViaCaption]) {
    const html = composeTablePartsHtml([{ domId: "source-element-a", html: attack }], sanitizeTableHtml);
    assert.ok(!/<img/i.test(html), `must not fabricate <img> from: ${attack}`);
    assert.ok(!/<svg/i.test(html), `must not fabricate <svg> from: ${attack}`);
    assert.ok(!/onerror/i.test(html), `must not leak onerror from: ${attack}`);
    assert.ok(!/onload/i.test(html), `must not leak onload from: ${attack}`);
  }
});

// sanitizeTableHtml 的属性是按白名单**重建**的:只可能输出 colspan/rowspan(纯数字)
// 与 align(固定取值)。旧实现只删 `\son…=`,`/` 分隔的事件属性会原样放行。
test("sanitizeTableHtml drops slash-separated event handlers that the old whitespace rule missed", () => {
  for (const tag of [
    "<td x/onclick=alert(1)>a</td>",
    "<td/onclick=alert(1)>a</td>",
    '<td/onmouseover="alert(1)">a</td>',
    "<th colspan=2/onclick='alert(1)'>a</th>",
  ]) {
    const html = sanitizeTableHtml(`<table><tr>${tag}</tr></table>`);
    assert.ok(!/on(click|mouseover)/i.test(html), `handler leaked from: ${tag} -> ${html}`);
    assert.ok(/<t[dh][ >]/.test(html), `table cell tag should survive: ${html}`);
  }
});

test("sanitizeTableHtml drops quoted and unquoted handlers, style, and any href/src attributes", () => {
  const html = sanitizeTableHtml(
    '<table style="background:url(javascript:alert(1))" onload=x>' +
      "<tr onclick='a()'><td ONCLICK=\"b()\" style=\"color:red\" href=\"javascript:alert(1)\" data-x=\"1\">a</td></tr>" +
      '<tr><td><a href="javascript:alert(1)">link</a></td></tr></table>',
  );
  assert.equal(
    html,
    "<table><tr><td>a</td></tr><tr><td> link </td></tr></table>",
  );
});

test("sanitizeTableHtml keeps numeric colspan/rowspan and a fixed-vocabulary align, nothing else", () => {
  const html = sanitizeTableHtml(
    '<table><tr><td colspan="2" rowspan=3 align="CENTER" class="x">a</td>' +
      '<th colspan="2 onclick=x" rowspan="abc" align="javascript:x">b</th></tr></table>',
  );
  assert.equal(
    html,
    '<table><tr><td colspan="2" rowspan="3" align="center">a</td><th colspan="2">b</th></tr></table>',
  );
});

test("sanitizeTableHtml escapes stray angle brackets and removes comments and script/style bodies", () => {
  const html = sanitizeTableHtml(
    "<table><tr><td>a < b > c<<tbody>img/src=x/onerror=alert(1)></td></tr></table>" +
      "<!-- <img src=x onerror=alert(1)> --><script>bad()</script><style>td{}</style>",
  );
  assert.ok(!/<img|<script|<style|<!--/i.test(html), html);
  assert.ok(html.includes("a &lt; b &gt; c&lt;<tbody>img/src=x/onerror=alert(1)&gt;"), html);
  assert.ok(!html.includes("bad()"));
});

test("sanitizeTableHtml still turns non-table tags into a space so mammoth <p> paragraphs don't glue", () => {
  assert.equal(
    sanitizeTableHtml("<table><tr><td><p>a</p><p>b</p></td></tr></table>"),
    "<table><tr><td> a  b </td></tr></table>",
  );
});
