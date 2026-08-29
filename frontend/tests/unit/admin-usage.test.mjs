import test from "node:test";
import assert from "node:assert/strict";

import {
  canSeeAdminUsage,
  formatLastActive,
  logsDrillHref,
  parseUploadLimit,
  sortAdminUsers,
} from "../../app/admin/usage/format.ts";

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
  assert.equal(
    logsDrillHref("user-abc123"),
    "/dev/logs?owner=user-abc123&view=activity&activity_type=ask",
  );
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

test("sortAdminUsers 对完整集合排序且最近活跃空值始终置底", () => {
  const rows = [
    { id: "u3", username: "user10", role: "user", created_at: "2026-03-01", notebooks: 3, sources: 0, conversations: 3, questions: 8, reports: 0, last_active: null, upload_limit: 20 },
    { id: "u1", username: "user2", role: "user", created_at: "2026-01-01", notebooks: 1, sources: 0, conversations: 1, questions: 2, reports: 0, last_active: "2026-07-01", upload_limit: 30 },
    { id: "u2", username: "admin", role: "admin", created_at: "2026-02-01", notebooks: 2, sources: 0, conversations: 2, questions: 5, reports: 0, last_active: "2026-06-01", upload_limit: 20 },
  ];

  assert.deepEqual(sortAdminUsers(rows, "username", "asc").map((row) => row.id), ["u2", "u1", "u3"]);
  assert.deepEqual(sortAdminUsers(rows, "notebooks", "desc").map((row) => row.id), ["u3", "u2", "u1"]);
  assert.deepEqual(sortAdminUsers(rows, "questions", "desc").map((row) => row.id), ["u3", "u2", "u1"]);
  assert.deepEqual(sortAdminUsers(rows, "last_active", "desc").map((row) => row.id), ["u1", "u2", "u3"]);
  assert.deepEqual(sortAdminUsers(rows, "upload_limit", "asc").map((row) => row.id), ["u3", "u1", "u2"]);
});

test("sortAdminUsers 按绝对时间跨 UTC offset 排序", () => {
  const rows = [
    { id: "later", username: "later", role: "user", created_at: "2026-01-01T12:00:00+08:00", notebooks: 0, sources: 0, conversations: 0, questions: 0, reports: 0, last_active: "2026-01-01T12:00:00+08:00", upload_limit: 20 },
    { id: "earlier", username: "earlier", role: "user", created_at: "2026-01-01T03:00:00+00:00", notebooks: 0, sources: 0, conversations: 0, questions: 0, reports: 0, last_active: "2026-01-01T03:00:00+00:00", upload_limit: 20 },
  ];

  assert.deepEqual(sortAdminUsers(rows, "created_at", "asc").map((row) => row.id), ["earlier", "later"]);
  assert.deepEqual(sortAdminUsers(rows, "last_active", "desc").map((row) => row.id), ["later", "earlier"]);
});

test("sortAdminUsers 对相同值稳定排序且多个管理员上限比较不返回 NaN", () => {
  const rows = [
    { id: "admin-z", username: "z-admin", role: "admin", created_at: "2026-01-01", notebooks: 1, sources: 0, conversations: 1, questions: 0, reports: 0, last_active: null, upload_limit: 20 },
    { id: "admin-a", username: "a-admin", role: "admin", created_at: "2026-01-02", notebooks: 1, sources: 0, conversations: 1, questions: 0, reports: 0, last_active: null, upload_limit: 20 },
    { id: "user", username: "user", role: "user", created_at: "2026-01-03", notebooks: 1, sources: 0, conversations: 1, questions: 0, reports: 0, last_active: null, upload_limit: 20 },
  ];

  assert.deepEqual(sortAdminUsers(rows, "notebooks", "asc").map((row) => row.id), ["admin-z", "admin-a", "user"]);
  assert.deepEqual(sortAdminUsers(rows, "upload_limit", "asc").map((row) => row.id), ["user", "admin-z", "admin-a"]);
  assert.deepEqual(sortAdminUsers(rows, "upload_limit", "desc").map((row) => row.id), ["admin-z", "admin-a", "user"]);
});
