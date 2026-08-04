import test from "node:test";
import assert from "node:assert/strict";

import {
  parseShareToken,
  buildShareLink,
  shareModeLabel,
  shareLinkCopyToast,
} from "./notebook-share.ts";
import { callsIn, findFunctionIn, parseModule } from "./test/semantic-source.mjs";

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

test("shareLinkCopyToast: 成功确认，失败引导手动选择可见链接", () => {
  assert.equal(shareLinkCopyToast(true), "分享链接已复制");
  assert.equal(shareLinkCopyToast(false), "复制失败，请手动选择上方链接");
});

test("分享链接复制统一走带降级路径的安全复制 helper", async () => {
  const page = await parseModule("page.tsx");
  const handlerCalls = callsIn(findFunctionIn(page, "Home", "handleShareLinkCopy"));
  const modalCalls = callsIn(findFunctionIn(page, "Home", "copyShareLink"));
  const pageCalls = callsIn(page);

  assert.ok(handlerCalls.includes("copyTextSafely"));
  assert.ok(handlerCalls.includes("shareLinkCopyToast"));
  assert.ok(handlerCalls.includes("setToast"));
  assert.ok(!handlerCalls.includes("setStatusText"));
  assert.ok(modalCalls.includes("handleShareLinkCopy"));
  assert.ok(pageCalls.filter((call) => call === "handleShareLinkCopy").length >= 2);
  assert.ok(!pageCalls.some((call) => call.includes("navigator.clipboard")));
});
