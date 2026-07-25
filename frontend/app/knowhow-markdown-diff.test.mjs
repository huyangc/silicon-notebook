import test from "node:test";
import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";

import {
  KNOWHOW_DIFF_MAX_CHARS,
  KNOWHOW_DIFF_MAX_LINES,
  KNOWHOW_DIFF_COARSE_BLOCK_LINES,
  KNOWHOW_DIFF_COARSE_LINE_CHARS,
  diffKnowhowMarkdown,
  tokenizeKnowhowDiffLine,
} from "./knowhow-markdown-diff.ts";

function textOf(lines, kind) {
  return lines.filter((line) => line.kind === kind).map((line) => line.text);
}

test("Markdown diff: 相同输入只有中性上下文", () => {
  const result = diffKnowhowMarkdown("标题\n正文", "标题\n正文");
  assert.equal(result.degraded, false);
  assert.deepEqual(result.lines, [
    { kind: "context", text: "标题" },
    { kind: "context", text: "正文" },
  ]);
});

test("Markdown diff: 行级增删保留中英文、emoji 与全角标点", () => {
  const result = diffKnowhowMarkdown("标题\n旧行🙂。\n尾部", "标题\n新行🚀！\n尾部");
  assert.deepEqual(textOf(result.lines, "delete"), ["旧行🙂。"]);
  assert.deepEqual(textOf(result.lines, "add"), ["新行🚀！"]);
  assert.deepEqual(textOf(result.lines, "context"), ["标题", "尾部"]);
});

test("Markdown diff: 行内 token 标出 ASCII、中文、空白、tab、标点和 Markdown 控制符", () => {
  const result = diffKnowhowMarkdown("**foo bar**\t中文。", "**foo  baz**\t中英！");
  const deleted = result.lines.find((line) => line.kind === "delete");
  const added = result.lines.find((line) => line.kind === "add");
  assert.ok(deleted?.tokens?.some((token) => token.text === "bar" && token.changed));
  assert.ok(added?.tokens?.some((token) => token.text === "baz" && token.changed));
  assert.ok(deleted?.tokens?.some((token) => token.whitespace));
  assert.deepEqual(tokenizeKnowhowDiffLine("``` foo\t中文🙂。"), ["```", " ", "foo", "\t", "中", "文", "🙂", "。​".slice(0, 1)]);
});

test("Markdown diff tokenizer: 20k 连续 ASCII/空白按线性 run 聚合且守住宽松预算", () => {
  const ascii = "a".repeat(KNOWHOW_DIFF_MAX_CHARS);
  const whitespace = " ".repeat(KNOWHOW_DIFF_MAX_CHARS);
  const started = performance.now();
  const asciiTokens = tokenizeKnowhowDiffLine(ascii);
  const whitespaceTokens = tokenizeKnowhowDiffLine(whitespace);
  const elapsedMs = performance.now() - started;

  assert.deepEqual(asciiTokens, [ascii]);
  assert.deepEqual(whitespaceTokens, [whitespace]);
  // 这是防 O(n²) 的宽松保险丝，不是微基准：旧实现单条 20k ASCII 会在
  // 每个 code point 重建此前 token，通常需要数秒；线性实现应有两个数量级余量。
  assert.ok(elapsedMs < 3_000, `20k run tokenization took ${elapsedMs.toFixed(1)}ms`);
});

test("Markdown diff: CRLF 与 LF 视为相同行结构", () => {
  const result = diffKnowhowMarkdown("a\r\nb", "a\nb");
  assert.equal(result.degraded, false);
  assert.ok(result.lines.every((line) => line.kind === "context"));
});

test("Markdown diff: 超过字符预算线性降级且不丢变化中段", () => {
  const before = `共同\n${"a".repeat(KNOWHOW_DIFF_MAX_CHARS + 1)}\n结尾`;
  const after = `共同\n${"b".repeat(KNOWHOW_DIFF_MAX_CHARS + 1)}\n结尾`;
  const result = diffKnowhowMarkdown(before, after);
  assert.equal(result.degraded, true);
  assert.equal(result.degradationReason, "characters");
  assert.equal(textOf(result.lines, "delete")[0].length, KNOWHOW_DIFF_COARSE_LINE_CHARS);
  assert.equal(textOf(result.lines, "add")[0].length, KNOWHOW_DIFF_COARSE_LINE_CHARS);
  assert.equal(result.lines.find((line) => line.kind === "delete")?.truncated, true);
});

test("Markdown diff: 超过行数预算线性降级", () => {
  const before = Array.from({ length: KNOWHOW_DIFF_MAX_LINES + 1 }, (_, index) => `旧${index}`).join("\n");
  const after = Array.from({ length: KNOWHOW_DIFF_MAX_LINES + 1 }, (_, index) => `新${index}`).join("\n");
  const result = diffKnowhowMarkdown(before, after);
  assert.equal(result.degraded, true);
  assert.equal(result.degradationReason, "lines");
  assert.equal(textOf(result.lines, "delete").length, KNOWHOW_DIFF_COARSE_BLOCK_LINES + 1);
  assert.equal(textOf(result.lines, "add").length, KNOWHOW_DIFF_COARSE_BLOCK_LINES + 1);
  assert.ok(result.lines.some((line) => line.omittedLineCount > 0));
});

test("Markdown diff: 400 行对抗输入有硬预算并返回完整粗粒度内容", () => {
  const beforeLines = Array.from({ length: KNOWHOW_DIFF_MAX_LINES }, (_, index) => `a-${index}`);
  const afterLines = Array.from({ length: KNOWHOW_DIFF_MAX_LINES }, (_, index) => `b-${index}`);
  const result = diffKnowhowMarkdown(beforeLines.join("\n"), afterLines.join("\n"));
  assert.equal(result.degraded, true);
  assert.equal(result.degradationReason, "line_steps");
  assert.equal(textOf(result.lines, "delete").length, KNOWHOW_DIFF_COARSE_BLOCK_LINES + 1);
  assert.equal(textOf(result.lines, "add").length, KNOWHOW_DIFF_COARSE_BLOCK_LINES + 1);
  assert.equal(textOf(result.lines, "delete")[0], beforeLines[0]);
  assert.equal(textOf(result.lines, "delete").at(-1), beforeLines.at(-1));
});
