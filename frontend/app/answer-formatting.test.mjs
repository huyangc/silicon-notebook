import test from "node:test";
import assert from "node:assert/strict";

import {
  buildAnswerReferences,
  parseMarkdownBlocks,
  referenceByCitationKey,
  renderTextWithReferenceNumbers,
  splitInlineLatex,
} from "./answer-formatting.ts";

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
