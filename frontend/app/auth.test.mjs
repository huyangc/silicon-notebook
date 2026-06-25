import { test } from "node:test";
import assert from "node:assert/strict";
import { isValidUsername } from "./auth.ts";

test("username accepts 1+ letters + 00 + 6 digits", () => {
  assert.ok(isValidUsername("zhang00123456"));
  assert.ok(isValidUsername("a00000042"));
  assert.ok(isValidUsername("ABc00999999"));
});

test("username rejects bad shapes", () => {
  assert.ok(!isValidUsername("00123456"));
  assert.ok(!isValidUsername("zhang0123456"));
  assert.ok(!isValidUsername("zhang0012345"));
  assert.ok(!isValidUsername("zh4ng00123456"));
});
