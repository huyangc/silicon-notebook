import test from "node:test";
import assert from "node:assert/strict";

import { canSeeAdminUsage, formatLastActive, logsDrillHref, parseUploadLimit } from "./admin/usage/format.ts";

test("canSeeAdminUsage 仅 admin 为真", () => {
  assert.equal(canSeeAdminUsage("admin"), true);
  assert.equal(canSeeAdminUsage("user"), false);
  assert.equal(canSeeAdminUsage(undefined), false);
});

test("formatLastActive 处理空值与格式", () => {
  assert.equal(formatLastActive(null), "—");
  assert.equal(formatLastActive(undefined), "—");
  assert.equal(formatLastActive("2026-07-06T12:34:56"), "2026-07-06 12:34");
});

test("logsDrillHref 编码 owner", () => {
  assert.equal(logsDrillHref("user-abc123"), "/dev/logs?owner=user-abc123");
});

test("parseUploadLimit 接受区间内整数、拒绝越界与非整数", () => {
  assert.equal(parseUploadLimit("20"), 20);
  assert.equal(parseUploadLimit(" 1 "), 1);
  assert.equal(parseUploadLimit("100000"), 100000);
  // 越界
  assert.equal(parseUploadLimit("0"), null);
  assert.equal(parseUploadLimit("100001"), null);
  // 非整数 / 非法形态
  assert.equal(parseUploadLimit(""), null);
  assert.equal(parseUploadLimit("12.5"), null);
  assert.equal(parseUploadLimit("-5"), null);
  assert.equal(parseUploadLimit("1e3"), null);
  assert.equal(parseUploadLimit("abc"), null);
});
