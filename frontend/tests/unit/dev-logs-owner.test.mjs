import test from "node:test";
import assert from "node:assert/strict";

import { fetchRecord } from "../../app/dev/logs/api.ts";
import { usernameForOwner } from "../../app/dev/logs/owner.ts";

const users = [
  { id: "user-aaa", username: "a00000001" },
  { id: "user-bbb", username: "b00000002" },
];

test("命中 owner 返回其用户名", () => {
  assert.equal(usernameForOwner(users, "user-bbb", "self"), "b00000002");
});

test("owner 为空返回自身名", () => {
  assert.equal(usernameForOwner(users, "", "myself"), "myself");
});

test("未知 owner 回退 owner 原值", () => {
  assert.equal(usernameForOwner(users, "user-zzz", "self"), "user-zzz");
});

test("管理员查看其他用户的日志详情时透传 owner", async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = "";
  globalThis.fetch = async (url) => {
    requestedUrl = String(url);
    return new Response(JSON.stringify({ id: "llm-1" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    await fetchRecord("llm", "llm-1", "2026-07-27", 42, "user-abc123");
  } finally {
    globalThis.fetch = originalFetch;
  }

  const url = new URL(requestedUrl);
  assert.equal(url.pathname, "/api/debug/logs/llm/llm-1");
  assert.equal(url.searchParams.get("date"), "2026-07-27");
  assert.equal(url.searchParams.get("seq"), "42");
  assert.equal(url.searchParams.get("owner"), "user-abc123");
});
