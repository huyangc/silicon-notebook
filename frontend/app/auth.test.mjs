import { test } from "node:test";
import assert from "node:assert/strict";
import { isValidUsername } from "./auth.ts";

test("username accepts a single letter + 00 + 6 digits", () => {
  assert.ok(isValidUsername("a00123456"));
  assert.ok(isValidUsername("Z00000042"));
  assert.ok(isValidUsername("b00999999"));
});

test("username rejects bad shapes", () => {
  assert.ok(!isValidUsername("00123456"));
  assert.ok(!isValidUsername("ab00123456"));   // 多个字母
  assert.ok(!isValidUsername("a0123456"));
  assert.ok(!isValidUsername("a0012345"));
  assert.ok(!isValidUsername("a001234567"));
});
