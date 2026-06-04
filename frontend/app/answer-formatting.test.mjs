import test from "node:test";
import assert from "node:assert/strict";

import {
  buildAnswerReferences,
  parseMarkdownBlocks,
  renderTextWithReferenceNumbers,
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
