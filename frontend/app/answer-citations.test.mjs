import test from "node:test";
import assert from "node:assert/strict";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

import { remarkCitations } from "./answer-citations.ts";
import { normalizeMathMarkdown } from "./math-markdown.ts";

// 与 AnswerMarkdown 完全一致的渲染管线（含 [remarkCitations, refsByKey] 元组形态
// —— 正是这种形态暴露了「多一层 ()=>」的 bug）。
const urlTransform = (url) => (url.startsWith("cite:") ? url : defaultUrlTransform(url));

function render(answer, refsByKey) {
  return renderToStaticMarkup(
    React.createElement(
      ReactMarkdown,
      {
        remarkPlugins: [remarkGfm, remarkMath, [remarkCitations, refsByKey]],
        rehypePlugins: [rehypeKatex],
        urlTransform,
      },
      normalizeMathMarkdown(answer),
    ),
  );
}

test("回归:带 [k] 标记的答案正文必须渲染出来(修复前整段空白 <div></div>)", () => {
  const refs = { k1: { id: "r1", displayLabel: "[1]" }, k2: { id: "r2", displayLabel: "[2]" } };
  // 相邻标记 [k1][k2] 复刻真实失败答案的结构
  const answer = "DeepSeek 系列对比：V2 引入 MLA [k1][k2]，V3 延续并优化 [k1]。";
  const html = render(answer, refs);
  assert.match(html, /DeepSeek 系列对比/);
  assert.match(html, /V3 延续并优化/);
});

test("引用标记渲染为 cite: 链接(urlTransform 保留 cite: 协议)", () => {
  const refs = { k1: { id: "r1", displayLabel: "[1]" } };
  const html = render("结论 [k1] 完", refs);
  assert.match(html, /href="cite:k1"/);
  assert.match(html, />\[1\]</);
});

test("数字复合引用渲染为多个 cite 链接", () => {
  const refs = {
    1: { id: "r1", displayLabel: "[1]" },
    2: { id: "r2", displayLabel: "[2]" },
    3: { id: "r3", displayLabel: "[3]" },
  };
  const html = render("结论来自多个来源 [1, 2, 3]。", refs);
  assert.match(html, /href="cite:1"/);
  assert.match(html, /href="cite:2"/);
  assert.match(html, /href="cite:3"/);
});

test("k 前缀复合引用 [k6, k10] 渲染为多个 cite 链接(报告 LLM 常吐逗号形式)", () => {
  const refs = {
    k6: { id: "r6", displayLabel: "[6]" },
    k10: { id: "r10", displayLabel: "[10]" },
  };
  const html = render("超越先前开源模型并与闭源持平 [k6, k10]。", refs);
  assert.match(html, /href="cite:k6"/);
  assert.match(html, /href="cite:k10"/);
  assert.match(html, />\[6\]</);
  assert.match(html, />\[10\]</);
});

test("k 前缀复合引用有未命中时保持原文", () => {
  const refs = { k6: { id: "r6", displayLabel: "[6]" } };
  const html = render("混入幻觉 [k6, k99] 不应半转换。", refs);
  assert.match(html, /\[k6, k99\]/);
  assert.doesNotMatch(html, /href="cite:k6"/);
});

test("数字复合引用中有未命中编号时保持原文", () => {
  const refs = { 1: { id: "r1", displayLabel: "[1]" } };
  const html = render("普通数组或缺失引用 [1, 99] 不应半转换。", refs);
  assert.match(html, /\[1, 99\]/);
  assert.doesNotMatch(html, /href="cite:1"/);
});

test("未命中的 key 原样保留为文本", () => {
  const html = render("见 [k9] 处", {});
  assert.match(html, /\[k9\]/);
  assert.doesNotMatch(html, /href="cite:k9"/);
});

test("普通外链经默认 urlTransform 仍被正常处理(不放行不安全协议)", () => {
  const html = render("[link](javascript:alert(1))", {});
  // 默认 urlTransform 会清掉 javascript: 协议
  assert.doesNotMatch(html, /javascript:/);
});

test("回归:紧邻正文的单行 $$ 功耗公式渲染为可滚动的 display KaTeX", () => {
  const refs = { k20: { id: "r20", displayLabel: "[1]" } };
  const answer = [
    "**第二层：分模块功耗计算**",
    "将预测得到的模块级利用率代入标准动态功耗方程：[k20]",
    "$$P^{\\mathrm{dyn}} = \\alpha_{\\mathrm{DRAM}} \\cdot C_{\\mathrm{D}} V_{\\mathrm{D}}^2 f_{\\mathrm{D}} + \\sum_{\\mathrm{modules}} \\alpha_{\\mathrm{module}} \\cdot C_{\\mathrm{m}} V^2 f$$",
    "其中 $\\alpha$ 为利用率，$V$ 和 $f$ 为工作电压和频率（作为DVFS配置输入）",
  ].join("\n");

  const html = render(answer, refs);
  assert.match(html, /class="katex-display"/);
  assert.match(html, /<mi>α<\/mi>/);
  assert.match(html, /href="cite:k20"/);
  assert.doesNotMatch(html, /katex-error/);
});

test("引用与列表容器中的单行 $$ 公式仍渲染为 display KaTeX", () => {
  for (const answer of ["> $$E=mc^2$$", "- $$P=VI$$", "1. $$f=ma$$"]) {
    const html = render(answer, {});
    assert.match(html, /class="katex-display"/);
    assert.doesNotMatch(html, /katex-error/);
  }
});

test("额外转义的 aligned 公式保留 LaTeX 换行并正常渲染", () => {
  const answer = String.raw`Intro\n$$\\begin{aligned}a&=b\\\\c&=d\\end{aligned}$$\nWhere the rows differ`;
  const html = render(answer, {});
  assert.match(html, /class="katex-display"/);
  assert.match(html, /Where the rows differ/);
  assert.doesNotMatch(html, /katex-error/);
});

test("纯公式及末尾容器公式的额外转义仍恢复为 display KaTeX", () => {
  for (const answer of [
    String.raw`$$\n\\begin{aligned}a&=b\\\\c&=d\\end{aligned}\n$$`,
    String.raw`Intro\n> $$E=mc^2$$`,
    String.raw`Intro\n- $$E=mc^2$$`,
  ]) {
    const html = render(answer, {});
    assert.match(html, /class="katex-display"/);
    assert.doesNotMatch(html, /katex-error/);
    assert.doesNotMatch(html, /\\n/);
  }
});

test("容器内未闭合 fence 不吞掉容器结束后的 display 公式", () => {
  for (const answer of [
    ["> ```md", "> code", "Text", "$$E=mc^2$$", "After"].join("\n"),
    ["- ```md", "  code", "Text", "$$E=mc^2$$", "After"].join("\n"),
  ]) {
    const html = render(answer, {});
    assert.match(html, /class="katex-display"/);
    assert.doesNotMatch(html, /katex-error/);
  }
});
