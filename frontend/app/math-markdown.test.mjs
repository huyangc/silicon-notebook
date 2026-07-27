import test from "node:test";
import assert from "node:assert/strict";

import { normalizeMathMarkdown, unwrapStandaloneLatex } from "./math-markdown.ts";

test("promotes a whole-line single-line $$ formula to display fences", () => {
  const markdown = [
    "将利用率代入动态功耗方程：[k20]",
    "$$P^{\\mathrm{dyn}} = \\alpha_{\\mathrm{DRAM}} + \\sum_{\\mathrm{modules}} P_m$$",
    "其中 $\\alpha$ 为利用率。",
  ].join("\n");

  assert.equal(
    normalizeMathMarkdown(markdown),
    [
      "将利用率代入动态功耗方程：[k20]",
      "$$",
      "P^{\\mathrm{dyn}} = \\alpha_{\\mathrm{DRAM}} + \\sum_{\\mathrm{modules}} P_m",
      "$$",
      "其中 $\\alpha$ 为利用率。",
    ].join("\n"),
  );
});

test("recovers a markdown value returned one JSON-string layer too deep", () => {
  const serialized = String.raw`说明\n$$P^{\\mathrm{dyn}} = \\alpha_{\\mathrm{DRAM}}$$\n其中 $\\alpha$ 为利用率`;
  assert.equal(
    normalizeMathMarkdown(serialized),
    [
      "说明",
      "$$",
      "P^{\\mathrm{dyn}} = \\alpha_{\\mathrm{DRAM}}",
      "$$",
      "其中 $\\alpha$ 为利用率",
    ].join("\n"),
  );
});

test("decodes one escaped layer without breaking aligned rows or English text", () => {
  const serialized = String.raw`Intro\n$$\\begin{aligned}a&=b\\\\c&=d\\end{aligned}$$\nWhere the rows differ`;
  assert.equal(
    normalizeMathMarkdown(serialized),
    [
      "Intro",
      "$$",
      "\\begin{aligned}a&=b\\\\c&=d\\end{aligned}",
      "$$",
      "Where the rows differ",
    ].join("\n"),
  );
});

test("does not rewrite display-looking text inside fenced code", () => {
  const markdown = ["```md", "$$E = mc^2$$", "```"].join("\n");
  assert.equal(normalizeMathMarkdown(markdown), markdown);
});

test("does not treat a fence marker with trailing text as a closing fence", () => {
  const markdown = [
    "````md",
    "```not-a-close",
    "$$E = mc^2$$",
    "````",
  ].join("\n");
  assert.equal(normalizeMathMarkdown(markdown), markdown);
});

test("does not rewrite formulas inside blockquote or list fenced code", () => {
  for (const markdown of [
    ["> ```md", "> $$E = mc^2$$", "> ```"].join("\n"),
    ["- ```md", "  $$E = mc^2$$", "  ```"].join("\n"),
  ]) {
    assert.equal(normalizeMathMarkdown(markdown), markdown);
  }
});

test("requires a closing fence to keep the opener container and relative indent", () => {
  for (const markdown of [
    ["```md", "    ```", "$$E = mc^2$$", "```"].join("\n"),
    ["> ```md", "```", "> $$E = mc^2$$", "> ```"].join("\n"),
  ]) {
    assert.equal(normalizeMathMarkdown(markdown), markdown);
  }
});

test("implicitly closes unterminated fences when their container ends", () => {
  for (const markdown of [
    ["> ```md", "> code", "Text", "$$E = mc^2$$", "After"].join("\n"),
    ["- ```md", "  code", "Text", "$$E = mc^2$$", "After"].join("\n"),
  ]) {
    assert.equal(
      normalizeMathMarkdown(markdown),
      markdown.replace(
        "$$E = mc^2$$",
        () => ["$$", "E = mc^2", "$$"].join("\n"),
      ),
    );
  }
});

test("promotes display formulas inside blockquote and list containers", () => {
  const markdown = [
    "> $$E = mc^2$$",
    "- $$P = VI$$",
    "1. $$f = ma$$",
  ].join("\n");
  assert.equal(
    normalizeMathMarkdown(markdown),
    [
      "> $$",
      "> E = mc^2",
      "> $$",
      "- $$",
      "  P = VI",
      "  $$",
      "1. $$",
      "   f = ma",
      "   $$",
    ].join("\n"),
  );
});

test("does not mistake the LaTeX command nabla for an escaped newline", () => {
  const markdown = String.raw`向量 $\nabla f$ 保持原样`;
  assert.equal(normalizeMathMarkdown(markdown), markdown);
});

test("does not decode raw n-prefixed LaTeX commands around display math", () => {
  assert.equal(
    normalizeMathMarkdown(String.raw`$$\nabla f$$`),
    ["$$", String.raw`\nabla f`, "$$"].join("\n"),
  );
  const proseCommand = String.raw`\newcommand{\x}{y} 与 $$E=mc^2$$`;
  assert.equal(normalizeMathMarkdown(proseCommand), proseCommand);
});

test("recovers an escaped math-only block with aligned rows", () => {
  const serialized = String.raw`$$\n\\begin{aligned}a&=b\\\\c&=d\\end{aligned}\n$$`;
  assert.equal(
    normalizeMathMarkdown(serialized),
    ["$$", "\\begin{aligned}a&=b\\\\c&=d\\end{aligned}", "$$"].join("\n"),
  );
});

test("recovers escaped newlines before container-owned formulas", () => {
  for (const [serialized, expected] of [
    [String.raw`Intro\n> $$E=mc^2$$`, ["Intro", "> $$", "> E=mc^2", "> $$"]],
    [String.raw`Intro\n- $$E=mc^2$$`, ["Intro", "- $$", "  E=mc^2", "  $$"]],
  ]) {
    assert.equal(normalizeMathMarkdown(serialized), expected.join("\n"));
  }
});

test("unwraps Markdown math delimiters before direct KaTeX rendering", () => {
  assert.equal(unwrapStandaloneLatex("$$ \\alpha + \\beta $$"), "\\alpha + \\beta");
  assert.equal(unwrapStandaloneLatex("\\[ E = mc^2 \\]"), "E = mc^2");
  assert.equal(unwrapStandaloneLatex("\\(x+y\\)"), "x+y");
  assert.equal(unwrapStandaloneLatex("$V_{DD}$"), "V_{DD}");
  assert.equal(unwrapStandaloneLatex("V_{DD}"), "V_{DD}");
});
