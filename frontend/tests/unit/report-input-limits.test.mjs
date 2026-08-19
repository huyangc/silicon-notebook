import assert from "node:assert/strict";
import test from "node:test";

import {
  REPORT_INPUT_LIMITS,
  countCodePoints,
  reportQuestionLimitHint,
} from "../../app/report-api.ts";

// 两条契约：
// 1) 尺子要与后端一致——`<textarea maxLength>` / `String.length` 数 UTF-16 code unit，
//    Pydantic `max_length` 数 Unicode 码点。含 emoji 时两者差一倍，「同一护栏」就名不
//    副实（codex #525 R2）。
// 2) 超限**一个字都不删**——护栏是拦住提交，不是替用户裁剪（codex #525 R3）。

const MAX = REPORT_INPUT_LIMITS.questionMaxChars;

test("按码点数，而不是按 UTF-16 code unit", () => {
  const four = "😀😀😀😀";
  assert.equal(four.length, 8, "前提：这段文本的 code unit 数是码点数的两倍");
  assert.equal(countCodePoints(four), 4);
  assert.equal(countCodePoints("时序收敛"), 4, "BMP 内的中文两把尺一致");
  assert.equal(countCodePoints(""), 0);
});

test("恰好等于上限不算超限（空转保护：一个恒超限的实现过不了）", () => {
  assert.equal(reportQuestionLimitHint("问".repeat(MAX)), null);
  assert.equal(reportQuestionLimitHint("短问题"), null);
  assert.equal(reportQuestionLimitHint(""), null);
});

test("超限给出带实际字数的可操作提示", () => {
  const hint = reportQuestionLimitHint("问".repeat(MAX + 3));
  assert.ok(hint, "超限必须有提示，否则按钮变灰而用户不知道为什么");
  assert.match(hint, new RegExp(String(MAX)), "提示里要有上限");
  assert.match(hint, new RegExp(String(MAX + 3)), "提示里要有当前字数");
});

test("按码点判超限：4,000 个 emoji 合法（按 code unit 会误判成 8,000 超限）", () => {
  const emoji = "😀".repeat(MAX);
  assert.equal(emoji.length, MAX * 2);
  assert.equal(reportQuestionLimitHint(emoji), null);
  assert.ok(reportQuestionLimitHint("😀".repeat(MAX + 1)));
});

test("上限与后端 REPORT_QUESTION_MAX_CHARS 同值（改一侧就要改另一侧）", () => {
  assert.equal(MAX, 4000);
});
