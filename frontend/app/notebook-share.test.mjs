import test from "node:test";
import assert from "node:assert/strict";

import { parseShareToken, buildShareLink, shareModeLabel } from "./notebook-share.ts";

test("parseShareToken: 单参数 ?share=shr-abc → shr-abc", () => {
  assert.strictEqual(parseShareToken("?share=shr-abc"), "shr-abc");
});

test("parseShareToken: 多参数 ?foo=1&share=shr-x → shr-x", () => {
  assert.strictEqual(parseShareToken("?foo=1&share=shr-x"), "shr-x");
});

test("parseShareToken: 无前导 ? 也能取(share=shr-y)", () => {
  assert.strictEqual(parseShareToken("share=shr-y"), "shr-y");
});

test("parseShareToken: 空串 → null", () => {
  assert.strictEqual(parseShareToken(""), null);
});

test("parseShareToken: 无 share 参数 → null", () => {
  assert.strictEqual(parseShareToken("?foo=1"), null);
});

test("buildShareLink: origin + token → ${origin}/?share=${token}", () => {
  assert.strictEqual(buildShareLink("shr-x", "https://h"), "https://h/?share=shr-x");
});

test("shareModeLabel: readonly → 只读共享 / copy → 可拷贝", () => {
  assert.strictEqual(shareModeLabel("readonly"), "只读共享");
  assert.strictEqual(shareModeLabel("copy"), "可拷贝");
});
