import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const css = await readFile(new URL("./globals.css", import.meta.url), "utf8");

test("ConversationSummary 类型带 used_reasoning", () => {
  assert.match(page, /type ConversationSummary = \{[^}]*used_reasoning\?: boolean/);
});

test("import 了纯函数 lastTurnUsedReasoning", () => {
  assert.match(page, /from "\.\/session-reasoning"/);
});

test("openSession 用最后一轮还原推理按钮", () => {
  assert.match(page, /setReasoningMode\(lastTurnUsedReasoning\(detail\.turns\)\)/);
});

test("历史卡片渲染推理标记", () => {
  assert.match(page, /session\.used_reasoning/);
  assert.match(page, /chat-session-reasoning-badge/);
});

test("标记样式已定义", () => {
  assert.match(css, /\.chat-session-reasoning-badge\s*\{/);
});
