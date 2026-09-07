import test from "node:test";
import assert from "node:assert/strict";

import {
  canSeeAdminUsage,
  formatBytes,
  formatLastActive,
  logsDrillHref,
  parseUploadLimit,
  questionsDrillHref,
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

// 规格 §3 B6:进位判断放在四舍五入之后(1048570/1048575 不出现「1024.0 KB」假进位);
// TB 是最高单位,顶到 1024.0 TB 也不再进位;负数/NaN/非有限值统一 "0 B"。
test("formatBytes 按 1024 进制格式化,进位在四舍五入之后判断,TB 顶格不再进位,异常输入归零", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(1023), "1023 B");
  assert.equal(formatBytes(1024), "1.0 KB");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(1048570), "1.0 MB"); // 舍入前 1023.99...KB,不得停留在 KB 显示「1024.0 KB」
  assert.equal(formatBytes(1048575), "1.0 MB");
  assert.equal(formatBytes(1048576), "1.0 MB");
  assert.equal(formatBytes(1932735283), "1.8 GB");
  assert.equal(formatBytes(1099511627776), "1.0 TB");
  assert.equal(formatBytes(1125899906842624), "1024.0 TB"); // TB 已是最高单位,不再进位
  assert.equal(formatBytes(-1), "0 B");
  assert.equal(formatBytes(NaN), "0 B");
});

test("用户分析下钻链接编码 owner 并分别进入提问与 LLM 日志", () => {
  assert.equal(
    logsDrillHref("user-abc123"),
    "/dev/logs?owner=user-abc123&view=llm",
  );
  assert.equal(
    questionsDrillHref("user-abc123"),
    "/admin/usage?sheet=questions&owner=user-abc123",
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
