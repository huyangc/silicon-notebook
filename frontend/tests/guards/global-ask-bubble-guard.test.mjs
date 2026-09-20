// 全局问答气泡与笔记本问答输入框的相处方式守卫。
//
// 背景：气泡停在视口右下角，恰好压在笔记本问答输入框的右端。#757 的修法是给输入框
// 加 `margin-right: 66px` 让出一格——输入框左右留白从此不对称（左 18px、右 66px）。
// 用户裁决：让位的应该是气泡（在笔记本里缩小、退进右下空白角），不是输入框。
//
// 断言（只读样式表——样式表没有可消费的 AST）：
//   1. 气泡的样式表不碰 `.chat-input-bar`；
//   2. 笔记本工作区里（`:has(.chat-panel)`）气泡不超过 40px、离角不超过 8px——
//      空白角横竖各 42px（页面边距 24 + 面板内边距 18），再大就压到输入框的内容。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");
const CSS = await readFile(path.join(APP_DIR, "ask/global-ask-launcher.css"), "utf8");
const RULES = CSS.replace(/\/\*[\s\S]*?\*\//g, "");

test("the bubble stylesheet never moves the notebook composer", () => {
  assert.doesNotMatch(RULES, /\.chat-input-bar/);
});

test("inside a notebook the bubble fits the empty corner", () => {
  const blocks = [...RULES.matchAll(/body:has\(\.chat-panel\) \.global-ask-bubble\s*\{([^}]*)\}/g)].map((match) => match[1]);
  assert.ok(blocks.length >= 1, "the in-notebook bubble rule is missing");
  for (const block of blocks) {
    const px = (property) => Number(block.match(new RegExp(`(?:^|[;\\s])${property}:\\s*(?:max\\()?(\\d+)px`))?.[1]);
    assert.ok(px("width") <= 40 && px("height") <= 40, `bubble too large: ${block}`);
    assert.ok(px("right") <= 8 && px("bottom") <= 8, `bubble too far from the corner: ${block}`);
  }
});
