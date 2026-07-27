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

test("does not rewrite display-looking text inside fenced code", () => {
  const markdown = ["```md", "$$E = mc^2$$", "```"].join("\n");
  assert.equal(normalizeMathMarkdown(markdown), markdown);
});

test("does not mistake the LaTeX command nabla for an escaped newline", () => {
  const markdown = String.raw`向量 $\nabla f$ 保持原样`;
  assert.equal(normalizeMathMarkdown(markdown), markdown);
});

test("unwraps Markdown math delimiters before direct KaTeX rendering", () => {
  assert.equal(unwrapStandaloneLatex("$$ \\alpha + \\beta $$"), "\\alpha + \\beta");
  assert.equal(unwrapStandaloneLatex("\\[ E = mc^2 \\]"), "E = mc^2");
  assert.equal(unwrapStandaloneLatex("\\(x+y\\)"), "x+y");
  assert.equal(unwrapStandaloneLatex("$V_{DD}$"), "V_{DD}");
  assert.equal(unwrapStandaloneLatex("V_{DD}"), "V_{DD}");
});
