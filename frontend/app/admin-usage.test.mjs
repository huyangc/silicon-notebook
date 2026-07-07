import test from "node:test";
import assert from "node:assert/strict";

import { canSeeAdminUsage, formatLastActive, logsDrillHref } from "./admin/usage/format.ts";

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
