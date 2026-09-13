import test from "node:test";
import assert from "node:assert/strict";

import {
  descriptionBlocks,
  elementHeadingLevel,
  elementLocationNote,
  elementSection,
  elementTypeTag,
  withSectionDividers,
} from "../../app/source-element-display.ts";
import { ELEMENT_TYPE } from "../../app/vocabulary.ts";
import { jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const el = (element_type, metadata = {}, id = element_type) => ({ id, element_type, metadata });

test("elementSection reads page / slide / sheet from parser metadata, in that priority", () => {
  assert.deepEqual(elementSection(el("paragraph", { parser: "mineru", page_number: 3 })), { key: "page:3", label: "第 3 页" });
  assert.deepEqual(elementSection(el("paragraph", { parser: "pdf", page_number: "12" })), { key: "page:12", label: "第 12 页" });
  assert.deepEqual(elementSection(el("slide_text", { slide_number: 2 })), { key: "slide:2", label: "幻灯片 2" });
  assert.deepEqual(elementSection(el("table_row", { sheet: "Q3 " , row_index: 4 })), { key: "sheet:Q3", label: "工作表 Q3" });
  // markdown / docx / txt 元素没有定位元数据:不分节。
  assert.equal(elementSection(el("paragraph", { parser: "markdown", heading_level: 1 })), null);
  assert.equal(elementSection(el("paragraph", {})), null);
  assert.equal(elementSection({ element_type: "paragraph", metadata: null }), null);
  // 0 / 负数 / 非数字不是合法页码。
  assert.equal(elementSection(el("paragraph", { page_number: 0 })), null);
  assert.equal(elementSection(el("paragraph", { page_number: "abc" })), null);
  assert.equal(elementSection(el("table_row", { sheet: "   " })), null);
});

test("withSectionDividers marks only the first element of each run, including the window head", () => {
  const rows = withSectionDividers([
    el("heading", { page_number: 1 }, "a"),
    el("paragraph", { page_number: 1 }, "b"),
    el("paragraph", { page_number: 2 }, "c"),
    el("paragraph", { page_number: 2 }, "d"),
    el("paragraph", { page_number: 1 }, "e"), // 回到第 1 页(解析顺序不保证单调)也要再标
  ]);
  assert.deepEqual(rows.map((row) => [row.element.id, row.divider]), [
    ["a", "第 1 页"],
    ["b", null],
    ["c", "第 2 页"],
    ["d", null],
    ["e", "第 1 页"],
  ]);
});

test("withSectionDividers never invents a divider for elements without a section", () => {
  const rows = withSectionDividers([
    el("heading", { heading_level: 1 }, "a"),
    el("paragraph", {}, "b"),
    el("paragraph", { page_number: 4 }, "c"),
    el("paragraph", {}, "d"),
    el("paragraph", { page_number: 4 }, "e"), // 中间隔了无节元素:前一个 key 是 null,重新标
  ]);
  assert.deepEqual(rows.map((row) => row.divider), [null, null, "第 4 页", null, "第 4 页"]);
  assert.deepEqual(withSectionDividers([]), []);
});

test("elementTypeTag hides self-evident types and keeps the rest on the vocabulary label", () => {
  for (const type of ["heading", "paragraph", "page_text", "table", "table_row", "formula", "code_block", "list_item", "image", "slide_text"]) {
    assert.equal(elementTypeTag(type), null, type);
  }
  assert.equal(elementTypeTag("speaker_notes"), ELEMENT_TYPE.speaker_notes);
  assert.equal(elementTypeTag("image_caption"), ELEMENT_TYPE.image_caption);
  assert.equal(elementTypeTag("knowhow_cell"), ELEMENT_TYPE.knowhow_cell);
  // 词汇表未收录的类型:不贴一个没意义的兜底标签。
  assert.equal(elementTypeTag("mystery"), null);
  assert.equal(elementTypeTag(""), null);
});

test("every ELEMENT_TYPE value is either hidden on purpose or shown verbatim", () => {
  // 词汇表新增类型时这条会逼着作者决定:它的形状是否一眼可辨。
  for (const [type, text] of Object.entries(ELEMENT_TYPE)) {
    const tag = elementTypeTag(type);
    assert.ok(tag === null || tag === text, `${type} -> ${tag}`);
  }
});

test("elementHeadingLevel maps parser levels onto h3..h5 with h3 as the default", () => {
  assert.equal(elementHeadingLevel(el("heading", { heading_level: 1 })), 3);
  assert.equal(elementHeadingLevel(el("heading", { text_level: 1 })), 3);
  assert.equal(elementHeadingLevel(el("heading", { heading_level: 2 })), 4);
  assert.equal(elementHeadingLevel(el("heading", { text_level: 3 })), 5);
  assert.equal(elementHeadingLevel(el("heading", { heading_level: 6 })), 5);
  assert.equal(elementHeadingLevel(el("heading", {})), 3);
  assert.equal(elementHeadingLevel(el("heading", { heading_level: 0 })), 3);
});

test("elementLocationNote only annotates table rows, with the docx table index when present", () => {
  assert.equal(elementLocationNote(el("table_row", { parser: "csv", row_index: 7 })), "第 7 行");
  assert.equal(elementLocationNote(el("table_row", { parser: "xlsx", sheet: "S", row_index: 2 })), "第 2 行");
  assert.equal(elementLocationNote(el("table_row", { parser: "docx", table_index: 3, row_index: 2 })), "表 3 第 2 行");
  assert.equal(elementLocationNote(el("table_row", {})), null);
  assert.equal(elementLocationNote(el("paragraph", { row_index: 7 })), null);
});

test("descriptionBlocks turns unfenced multi-line text into one paragraph per non-empty line", () => {
  assert.deepEqual(descriptionBlocks("line one\n\nline two\n  line three  "), [
    { kind: "paragraph", text: "line one" },
    { kind: "paragraph", text: "line two" },
    { kind: "paragraph", text: "line three" },
  ]);
});

test("descriptionBlocks reads a single fenced block with a language marker", () => {
  assert.deepEqual(descriptionBlocks("```spice\nR1 1 0 1k\nC1 1 0 1u\n```"), [
    { kind: "code", text: "R1 1 0 1k\nC1 1 0 1u", lang: "spice" },
  ]);
});

test("descriptionBlocks preserves blank lines and indentation inside a fence", () => {
  assert.deepEqual(descriptionBlocks("```\n  foo\n\n  bar\n```"), [
    { kind: "code", text: "  foo\n\n  bar", lang: "" },
  ]);
});

test("descriptionBlocks extends an unclosed fence to the end of the text", () => {
  assert.deepEqual(descriptionBlocks("```netlist\nR1 1 0 1k\nC1 1 0 1u"), [
    { kind: "code", text: "R1 1 0 1k\nC1 1 0 1u", lang: "netlist" },
  ]);
});

test("descriptionBlocks mixes paragraphs before and after a fence", () => {
  assert.deepEqual(descriptionBlocks("intro\n```\ncode\n```\noutro"), [
    { kind: "paragraph", text: "intro" },
    { kind: "code", text: "code", lang: "" },
    { kind: "paragraph", text: "outro" },
  ]);
});

test("descriptionBlocks returns an empty list for empty input", () => {
  assert.deepEqual(descriptionBlocks(""), []);
});

test("descriptionBlocks splits CRLF input and keeps \\r out of the code text", () => {
  assert.deepEqual(
    descriptionBlocks("intro\r\n```spice\r\nR1 1 0 1k\r\nC1 1 0 1u\r\n```\r\noutro"),
    [
      { kind: "paragraph", text: "intro" },
      { kind: "code", text: "R1 1 0 1k\nC1 1 0 1u", lang: "spice" },
      { kind: "paragraph", text: "outro" },
    ],
  );
});

test("descriptionBlocks opens a fence with leading indentation before the backticks", () => {
  assert.deepEqual(descriptionBlocks("  ```spice\ncode1\n```"), [
    { kind: "code", text: "code1", lang: "spice" },
  ]);
});

test("descriptionBlocks closes on a fence line with trailing spaces and keeps reading after it", () => {
  assert.deepEqual(descriptionBlocks("```\ncode\n```  \nafter"), [
    { kind: "code", text: "code", lang: "" },
    { kind: "paragraph", text: "after" },
  ]);
});

test("descriptionBlocks drops an empty code block instead of emitting an empty <pre>", () => {
  assert.deepEqual(descriptionBlocks("```\n```"), []);
  assert.deepEqual(descriptionBlocks("```"), []);
  assert.deepEqual(descriptionBlocks("before\n```\n```\nafter"), [
    { kind: "paragraph", text: "before" },
    { kind: "paragraph", text: "after" },
  ]);
});

test("descriptionBlocks splits on bare CR line endings", () => {
  assert.deepEqual(descriptionBlocks("line one\rline two\r```\rcode\r```"), [
    { kind: "paragraph", text: "line one" },
    { kind: "paragraph", text: "line two" },
    { kind: "code", text: "code", lang: "" },
  ]);
});

test("source detail renders elements through the display module, not location_label", async () => {
  const page = await parseModule("page.tsx");
  const cards = jsxElements(page, "SourceElementCard");
  assert.ok(cards.length >= 1, "SourceElementCard is mounted in the element stack");
  const source = page.getFullText();
  const stackStart = source.indexOf('className="source-element-stack"');
  const stackEnd = source.indexOf("</SourceDetailWindow>", stackStart);
  assert.ok(stackStart > 0 && stackEnd > stackStart);
  const stack = source.slice(stackStart, stackEnd);
  assert.ok(stack.includes("withSectionDividers("), "dividers come from withSectionDividers");
  assert.ok(!stack.includes("location_label"), "the parser location label is not rendered in the reading view");
});

test("the image element branch renders a fenced description as a <pre class=\"element-image-code\">", async () => {
  // ElementBody/SourceElementCard 都不对外导出(page.tsx 是巨型客户端组件,导出
  // 内部渲染函数会扩大它们的公开面),这里改用源码断言。锚点定在
  // `element.element_type === "image"` 分支的起止之间(下一个顶层函数
  // `EvidenceLine` 之前),不用无界通配符,不会误吃到其它分支的文本。
  const page = await parseModule("page.tsx");
  const source = page.getFullText();
  const imageBranchStart = source.indexOf('element.element_type === "image"');
  assert.ok(imageBranchStart > 0, "ElementBody still has an image branch");
  const imageBranchEnd = source.indexOf("function EvidenceLine", imageBranchStart);
  assert.ok(imageBranchEnd > imageBranchStart, "image branch is bounded before the next top-level function");
  const imageBranch = source.slice(imageBranchStart, imageBranchEnd);
  assert.ok(imageBranch.includes("descriptionBlocks("), "description is parsed through descriptionBlocks");
  assert.ok(imageBranch.includes('className="element-image-code"'), "fenced code renders through .element-image-code");
  assert.ok(imageBranch.includes("data-language={block.lang"), "the fence language is exposed as data-language");
  assert.ok(!imageBranch.includes("data-lang="), "the old data-lang attribute name is gone");
});
