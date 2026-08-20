import assert from "node:assert/strict";
import test from "node:test";

import { ASK_INPUT_LIMITS, askQuestionLimitHint, conversationTitleLimitHint } from "../../app/ask-api.ts";
import { countCodePoints } from "../../app/input-limits.ts";

// 与 `report-input-limits.test.mjs` 同构——两条契约：
// 1) 尺子要与后端一致——`<textarea maxLength>` / `String.length` 数 UTF-16 code unit，
//    Pydantic `max_length` 数 Unicode 码点。含 emoji 时两者差一倍，「同一护栏」就名不
//    副实（codex #525 R2）。
// 2) 超限**一个字都不删**——护栏是拦住提交，不是替用户裁剪（codex #525 R3）。

const MAX = ASK_INPUT_LIMITS.questionMaxChars;

test("按码点数，而不是按 UTF-16 code unit", () => {
  const four = "😀😀😀😀";
  assert.equal(four.length, 8, "前提：这段文本的 code unit 数是码点数的两倍");
  assert.equal(countCodePoints(four), 4);
  assert.equal(countCodePoints("时序收敛"), 4, "BMP 内的中文两把尺一致");
  assert.equal(countCodePoints(""), 0);
});

test("恰好等于上限不算超限（空转保护：一个恒超限的实现过不了）", () => {
  assert.equal(askQuestionLimitHint("问".repeat(MAX)), null);
  assert.equal(askQuestionLimitHint("这个库里都有哪些时序收敛方法？"), null);
  assert.equal(askQuestionLimitHint(""), null);
});

test("超限给出带实际字数的可操作提示", () => {
  const hint = askQuestionLimitHint("问".repeat(MAX + 3));
  assert.ok(hint, "超限必须有提示，否则发送键变灰而用户不知道为什么");
  assert.match(hint, new RegExp(String(MAX)), "提示里要有上限");
  assert.match(hint, new RegExp(String(MAX + 3)), "提示里要有当前字数");
});

test("按码点判超限：4,000 个 emoji 合法（按 code unit 会误判成 8,000 超限）", () => {
  const emoji = "😀".repeat(MAX);
  assert.equal(emoji.length, MAX * 2);
  assert.equal(askQuestionLimitHint(emoji), null);
  assert.ok(askQuestionLimitHint("😀".repeat(MAX + 1)));
});

test("上限与后端 ASK_QUESTION_MAX_CHARS 同值（改一侧就要改另一侧）", () => {
  // backend/app/models/ask.py::ASK_QUESTION_MAX_CHARS —— 会话公开分享页把每轮
  // question 原样发给匿名访客，「不截断」只有在提交侧就挡住超长问题时才成立。
  assert.equal(MAX, 4000);
});

// --- 会话标题 ---------------------------------------------------------------
// 同一条红线的另一半：公开分享页把标题也原样发给匿名访客，所以「不截断」同样只有在
// 重命名那一刻就挡住超长标题时才是有界的。尺子与「不裁剪」两条契约逐字相同。

const TITLE_MAX = ASK_INPUT_LIMITS.conversationTitleMaxChars;

test("标题恰好等于上限不算超限（空转保护）", () => {
  assert.equal(conversationTitleLimitHint("标".repeat(TITLE_MAX)), null);
  assert.equal(conversationTitleLimitHint("时序收敛怎么做"), null);
  assert.equal(conversationTitleLimitHint(""), null);
});

test("标题超限给出带实际字数的可操作提示", () => {
  const hint = conversationTitleLimitHint("标".repeat(TITLE_MAX + 5));
  assert.ok(hint, "超限必须有提示，否则保存键变灰而用户不知道为什么");
  assert.match(hint, new RegExp(String(TITLE_MAX)), "提示里要有上限");
  assert.match(hint, new RegExp(String(TITLE_MAX + 5)), "提示里要有当前字数");
});

test("标题也按码点判超限（按 code unit 会把满额的 emoji 标题误判成超限）", () => {
  const emoji = "😀".repeat(TITLE_MAX);
  assert.equal(emoji.length, TITLE_MAX * 2);
  assert.equal(conversationTitleLimitHint(emoji), null);
  assert.ok(conversationTitleLimitHint("😀".repeat(TITLE_MAX + 1)));
});

test("标题上限与后端 CONVERSATION_TITLE_MAX_CHARS 同值（改一侧就要改另一侧）", () => {
  // backend/app/models/ask.py::CONVERSATION_TITLE_MAX_CHARS —— 200 而不是 4,000：
  // 那把尺是给问题正文定的，标题是一行标签，服务端自动取的也只有前 60 字。
  assert.equal(TITLE_MAX, 200);
});

test("两个上限是各自独立的常量，不是同一个数（标题不得跟着问题上限漂移）", () => {
  assert.notEqual(TITLE_MAX, MAX);
});
