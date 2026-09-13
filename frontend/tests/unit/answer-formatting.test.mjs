import test from "node:test";
import assert from "node:assert/strict";

import {
  buildAnswerReferences,
  computeSourceTierCounts,
  parseMarkdownBlocks,
  referenceByCitationKey,
  renderTextWithReferenceNumbers,
  splitInlineLatex,
} from "../../app/answer-formatting.ts";

const anchors = [
  {
    key: "k2",
    object_id: "ko-2",
    object_type: "claim",
    label: "Claim",
    name: "Second claim",
    source_title: "source.md",
    location_label: "L2",
    tier: "base",
  },
  {
    key: "k1",
    object_id: "ko-1",
    object_type: "concept",
    label: "Concept",
    name: "First concept",
    source_title: "source.md",
    location_label: "L1",
  },
];

test("numbers cited anchors by first appearance and reuses repeated markers", () => {
  const text = "先看 [k1]，再看 [k2]，最后回到 [k1]。";
  const references = buildAnswerReferences(text, anchors, []);

  assert.deepEqual(
    references.map((reference) => [reference.anchor?.key, reference.displayLabel]),
    [
      ["k1", "[1]"],
      ["k2", "[2]"],
    ],
  );
  assert.equal(renderTextWithReferenceNumbers(text, references), "先看 [1]，再看 [2]，最后回到 [1]。");
});

test("numbers every anchor in a grouped k marker by first appearance", () => {
  const groupedAnchors = [
    { key: "k2002", object_id: "rel-2", object_type: "relation", label: "Second hop" },
    { key: "k2001", object_id: "rel-1", object_type: "relation", label: "First hop" },
    { key: "k2003", object_id: "rel-3", object_type: "relation", label: "Third hop" },
  ];
  const text = "推导依据 [k2001, k2002]，补充 [k2003, k2001]。";
  const references = buildAnswerReferences(text, groupedAnchors, []);

  assert.deepEqual(
    references.map((reference) => [reference.anchor?.key, reference.displayLabel]),
    [
      ["k2001", "[1]"],
      ["k2002", "[2]"],
      ["k2003", "[3]"],
    ],
  );
  assert.equal(renderTextWithReferenceNumbers(text, references), "推导依据 [1, 2]，补充 [3, 1]。");
});

test("binds Chinese bracket markers and Chinese-comma groups", () => {
  const text = "先看【k1】，再看【k2，k1】。";
  const references = buildAnswerReferences(text, anchors, []);

  assert.deepEqual(
    references.map((reference) => [reference.anchor?.key, reference.displayLabel]),
    [["k1", "[1]"], ["k2", "[2]"]],
  );
  assert.equal(renderTextWithReferenceNumbers(text, references), "先看[1]，再看[2, 1]。");
});

test("fails a mixed known and unknown grouped k marker closed without partial binding", () => {
  const text = "已知 [k2001]，不可部分绑定 [k2001, k2999]。";
  const references = buildAnswerReferences(text, [
    { key: "k2001", object_id: "rel-1", object_type: "relation", label: "Known hop" },
  ], []);

  assert.deepEqual(references.map((reference) => reference.anchor?.key), ["k2001"]);
  assert.equal(
    renderTextWithReferenceNumbers(text, references),
    "已知 [1]，不可部分绑定 [k2001, k2999]。",
  );
});

test("does not add the known subset when the only grouped k marker contains an unknown key", () => {
  const references = buildAnswerReferences("推导 [k2001, k2999]。", [
    { key: "k2001", object_id: "rel-1", object_type: "relation", label: "Known hop" },
  ], []);

  assert.deepEqual(references, []);
});

test("falls back to sequential citation numbers when no anchors are cited", () => {
  const references = buildAnswerReferences("没有 anchor。", [], [
    { label: "A", source_id: "s", element_id: "e1", location_label: "p.1", quoted_span: "quote 1" },
    { label: "B", source_id: "s", element_id: "e2", location_label: "p.2", quoted_span: "quote 2" },
  ]);

  assert.deepEqual(references.map((reference) => reference.displayLabel), ["[1]", "[2]"]);
});

test("parses code fences, display formulas, and markdown tables", () => {
  const blocks = parseMarkdownBlocks([
    "说明",
    "",
    "```ts",
    "const x = 1;",
    "```",
    "",
    "$$",
    "E = mc^2",
    "$$",
    "",
    "| A | B |",
    "| --- | --- |",
    "| 1 | 2 |",
  ].join("\n"));

  assert.deepEqual(blocks.map((block) => block.type), ["paragraph", "code", "formula", "table"]);
  assert.equal(blocks[1].type === "code" ? blocks[1].language : "", "ts");
  assert.equal(blocks[2].type === "formula" ? blocks[2].latex : "", "E = mc^2");
  assert.deepEqual(blocks[3].type === "table" ? blocks[3].headers : [], ["A", "B"]);
});

test("preserves anchor tier on built references", () => {
  const references = buildAnswerReferences("看 [k2]。", anchors, []);
  assert.equal(references[0].anchor?.tier, "base");
});

test("computeSourceTierCounts partitions the displayed references by tier", () => {
  const references = [
    { id: "a:k1", displayLabel: "[1]", anchor: { key: "k1", object_id: "ko-1", object_type: "chunk", label: "A", tier: "base" } },
    { id: "a:k2", displayLabel: "[2]", anchor: { key: "k2", object_id: "ko-2", object_type: "chunk", label: "B", tier: "personal" } },
    { id: "c:1", displayLabel: "[3]", citation: { label: "C", source_id: "src-1", element_id: "e1", location_label: "p.1", quoted_span: "q", tier: "base" } },
  ];
  assert.deepEqual(computeSourceTierCounts(references), { personal: 1, base: 2, external: 0 });
});

test("computeSourceTierCounts never sums above the reference count (regression: 个人15+基准库7=22>15)", () => {
  // The badge must partition the SAME references the user sees. Anchors carry the [k] hits;
  // the overlapping `citations` candidate pool must NOT be added on top — doing so double-
  // counted base sources and produced a total above the visible reference count.
  const references = buildAnswerReferences(
    "结论见 [k1] 与 [k2] 与 [k3]。",
    [
      { key: "k1", object_id: "ko-1", object_type: "chunk", label: "A", tier: "personal" },
      { key: "k2", object_id: "ko-2", object_type: "chunk", label: "B", tier: "personal" },
      { key: "k3", object_id: "ko-3", object_type: "chunk", label: "C", tier: "base" },
    ],
    [
      { label: "A", source_id: "src-1", element_id: "e1", location_label: "p.1", quoted_span: "q", tier: "base" },
      { label: "B", source_id: "src-2", element_id: "e2", location_label: "p.2", quoted_span: "q", tier: "base" },
    ],
  );
  const counts = computeSourceTierCounts(references);
  assert.equal(counts.personal + counts.base + counts.external, references.length); // 3, never 3+2
  assert.deepEqual(counts, { personal: 2, base: 1, external: 0 });
});

test("computeSourceTierCounts treats missing/unknown tier as personal", () => {
  const references = [
    { id: "a:k1", displayLabel: "[1]", anchor: { key: "k1", object_id: "ko-1", object_type: "chunk", label: "A" } },
    { id: "c:1", displayLabel: "[2]", citation: { label: "C", source_id: "src-1", element_id: "e1", location_label: "p.1", quoted_span: "q" } },
  ];
  assert.deepEqual(computeSourceTierCounts(references), { personal: 2, base: 0, external: 0 });
});

test("computeSourceTierCounts returns all zeros for empty input", () => {
  assert.deepEqual(computeSourceTierCounts([]), { personal: 0, base: 0, external: 0 });
});

test("computeSourceTierCounts handles all-personal references", () => {
  const references = [
    { id: "a:k1", displayLabel: "[1]", anchor: { key: "k1", object_id: "ko-1", object_type: "chunk", label: "A", tier: "personal" } },
    { id: "a:k2", displayLabel: "[2]", anchor: { key: "k2", object_id: "ko-2", object_type: "chunk", label: "B", tier: "personal" } },
  ];
  assert.deepEqual(computeSourceTierCounts(references), { personal: 2, base: 0, external: 0 });
});

// 外部证据（ask.reflect_action，设计文档 §6.3）：第三个桶。承重点是它**从 personal
// 里减出去**——库外条目被算成「个人知识库」就是把库外内容说成笔记本内容，正是
// §九 不变量 3 要挡的那件事。
test("computeSourceTierCounts 把 external 单列一桶，不再混进 personal", () => {
  const references = [
    { id: "a:k1", displayLabel: "[1]", anchor: { key: "k1", object_id: "ko-1", object_type: "chunk", label: "A", tier: "personal" } },
    { id: "a:k2", displayLabel: "[2]", anchor: { key: "k2", object_id: "ext:demo:1", object_type: "external", label: "B", tier: "external", url: "https://example.com/a" } },
    { id: "a:k3", displayLabel: "[3]", anchor: { key: "k3", object_id: "ext:demo:2", object_type: "external", label: "C", tier: "external", url: "https://example.com/b" } },
    { id: "a:k4", displayLabel: "[4]", anchor: { key: "k4", object_id: "ko-4", object_type: "chunk", label: "D", tier: "base" } },
  ];
  assert.deepEqual(computeSourceTierCounts(references), { personal: 1, base: 1, external: 2 });
});

test("computeSourceTierCounts 的三桶之和恒等于可见引用数（含 external）", () => {
  const references = [
    { id: "a:k1", displayLabel: "[1]", anchor: { key: "k1", object_id: "ko-1", object_type: "chunk", label: "A", tier: "external" } },
    { id: "c:1", displayLabel: "[2]", citation: { label: "C", source_id: "", element_id: "", location_label: "", quoted_span: "q", tier: "external", url: "https://example.com/c" } },
    { id: "c:2", displayLabel: "[3]", citation: { label: "D", source_id: "s", element_id: "e", location_label: "", quoted_span: "q" } },
  ];
  const counts = computeSourceTierCounts(references);
  assert.equal(counts.personal + counts.base + counts.external, references.length);
  assert.deepEqual(counts, { personal: 1, base: 0, external: 2 });
});

// citation 回退列表（答案里一个 [k] 标记都没有）同样要认出 external —— 引用卡的
// 「外部」标记与打开链接都吊在这条上。
test("computeSourceTierCounts 在 citation 回退路径上同样认 external tier", () => {
  const references = buildAnswerReferences(
    "没有任何 [kN] 标记的答案。",
    [],
    [
      { label: "IEEE Xplore · 某篇论文", source_id: "", element_id: "", location_label: "", quoted_span: "摘录", tier: "external", url: "https://example.org/paper" },
      { label: "本地资料", source_id: "s1", element_id: "e1", location_label: "p.1", quoted_span: "摘录" },
    ],
  );
  assert.deepEqual(computeSourceTierCounts(references), { personal: 1, base: 0, external: 1 });
  assert.equal(references[0].citation?.url, "https://example.org/paper");
});

test("maps display citation numbers to references for numeric model citations", () => {
  const references = buildAnswerReferences("没有 anchor。", [], [
    { label: "A", source_id: "s", element_id: "e1", location_label: "p.1", quoted_span: "quote 1" },
    { label: "B", source_id: "s", element_id: "e2", location_label: "p.2", quoted_span: "quote 2" },
  ]);

  const byCitationKey = referenceByCitationKey(references);
  assert.equal(byCitationKey["1"]?.id, references[0].id);
  assert.equal(byCitationKey["2"]?.id, references[1].id);
});

test("parses a single-line $$...$$ as a display formula block", () => {
  const blocks = parseMarkdownBlocks(["前言", "", "$$E = mc^2$$", "", "尾声"].join("\n"));
  assert.deepEqual(blocks.map((block) => block.type), ["paragraph", "formula", "paragraph"]);
  assert.equal(blocks[1].type === "formula" ? blocks[1].latex : "", "E = mc^2");
});

test("parses a single-line \\[ ... \\] as a display formula block", () => {
  const blocks = parseMarkdownBlocks(["介绍", "", "\\[ A_v = -g_m r_o \\]", "", "结论"].join("\n"));
  assert.deepEqual(blocks.map((block) => block.type), ["paragraph", "formula", "paragraph"]);
  assert.equal(blocks[1].type === "formula" ? blocks[1].latex : "", "A_v = -g_m r_o");
});

test("parses a multi-line \\[ ... \\] block spanning several lines", () => {
  const blocks = parseMarkdownBlocks(["\\[", "x = 1", "y = 2", "\\]"].join("\n"));
  assert.deepEqual(blocks.map((block) => block.type), ["formula"]);
  assert.equal(blocks[0].type === "formula" ? blocks[0].latex : "", "x = 1\ny = 2");
});

test("keeps the existing $$-on-its-own-line block behavior", () => {
  const blocks = parseMarkdownBlocks(["$$", "E = mc^2", "$$"].join("\n"));
  assert.deepEqual(blocks.map((block) => block.type), ["formula"]);
  assert.equal(blocks[0].type === "formula" ? blocks[0].latex : "", "E = mc^2");
});

test("splitInlineLatex segments $...$ inline math out of prose", () => {
  const segments = splitInlineLatex("增益约为 $g_m r_o$ 量级。");
  assert.deepEqual(segments, [
    { type: "text", value: "增益约为 " },
    { type: "math", value: "g_m r_o" },
    { type: "text", value: " 量级。" },
  ]);
});

test("splitInlineLatex segments \\( ... \\) inline math out of prose", () => {
  const segments = splitInlineLatex("当 \\(v_i = 0\\) 时成立");
  assert.deepEqual(segments, [
    { type: "text", value: "当 " },
    { type: "math", value: "v_i = 0" },
    { type: "text", value: " 时成立" },
  ]);
});

test("splitInlineLatex leaves plain prose untouched as a single text segment", () => {
  const segments = splitInlineLatex("这里没有任何公式定界符");
  assert.deepEqual(segments, [{ type: "text", value: "这里没有任何公式定界符" }]);
});

test("splitInlineLatex treats a whole formula name (no delimiters) as plain text", () => {
  const raw = "R_o = (v_I/i_I)|_{v_i=0} = 1/((g_m1+g_m2)A) || r_o1 || r_o2";
  assert.deepEqual(splitInlineLatex(raw), [{ type: "text", value: raw }]);
});
