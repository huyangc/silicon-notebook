import assert from "node:assert/strict";
import test from "node:test";

import { REPORT_INPUT_LIMITS, clampToCodePoints } from "../../app/report-api.ts";

// 这条守卫的存在理由：`<textarea maxLength>` 数的是 UTF-16 code unit，而后端
// Pydantic 的 `max_length` 数的是 Unicode 码点。含 emoji 等非 BMP 字符时两者差一倍，
// 「同一护栏」就名不副实（codex #525 R2 P2）。clampToCodePoints 让前端改用后端那把尺。

test("按码点夹，而不是按 UTF-16 code unit", () => {
  // 每个 emoji 占 2 个 code unit、1 个码点。maxLength={4} 只会放进 2 个。
  const four = "😀😀😀😀";
  assert.equal(four.length, 8, "前提：这段文本的 code unit 数是码点数的两倍");
  assert.equal(Array.from(four).length, 4);

  assert.equal(clampToCodePoints(four, 4), four, "恰好等于上限：一个码点都不该丢");
  assert.equal(clampToCodePoints("😀😀😀😀😀", 4), four, "超限：按码点截，不劈开代理对");
});

test("没超限时原样返回（空转保护：一个恒截断的实现过不了）", () => {
  assert.equal(clampToCodePoints("时序收敛", 10), "时序收敛");
  assert.equal(clampToCodePoints("", 10), "");
});

test("截出来的结果里没有落单的代理项", () => {
  const clamped = clampToCodePoints("ab😀cd", 3);
  assert.equal(clamped, "ab😀");
  for (const unit of clamped) assert.ok(unit.codePointAt(0) !== undefined);
  // 劈开代理对会产生 U+D800..U+DFFF 的孤儿；逐码点遍历后不该出现。
  assert.ok(!Array.from(clamped).some((c) => c.charCodeAt(0) >= 0xd800 && c.charCodeAt(0) <= 0xdfff && c.length === 1));
});

test("上限与后端 REPORT_QUESTION_MAX_CHARS 同值（改一侧就要改另一侧）", () => {
  assert.equal(REPORT_INPUT_LIMITS.questionMaxChars, 4000);
});
