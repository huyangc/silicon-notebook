import test from "node:test";
import assert from "node:assert/strict";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

import { remarkCitations } from "./answer-citations.ts";

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
      answer,
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
