import test from "node:test";
import assert from "node:assert/strict";

import {
  ADMIN_QUESTIONS_DEFAULT_LIMIT,
  ADMIN_QUESTIONS_MAX_LIMIT,
  fetchAdminQuestions,
} from "../../app/admin/questions/api.ts";


test("管理员提问分页 rail 与后端协议同名且 API 层拒绝越界值", async () => {
  assert.equal(ADMIN_QUESTIONS_DEFAULT_LIMIT, 50);
  assert.equal(ADMIN_QUESTIONS_MAX_LIMIT, 200);
  await assert.rejects(
    fetchAdminQuestions({ limit: ADMIN_QUESTIONS_MAX_LIMIT + 1 }),
    { name: "RangeError", message: "每页数量必须是 1 到 200 之间的整数" },
  );
  await assert.rejects(fetchAdminQuestions({ limit: 0 }), { name: "RangeError" });
  await assert.rejects(fetchAdminQuestions({ limit: 1.5 }), { name: "RangeError" });
});


test("调用方式筛选以 submitted_via 查询参数发出，未选时不带该参数", async () => {
  const originalFetch = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(new URL(String(url), "http://localhost"));
    return new Response(JSON.stringify({
      items: [], stats: { total: 0, asks: 0, reports: 0, active_users: 0, global_asks: 0 },
      total: 0, offset: 0, limit: 50,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    await fetchAdminQuestions({ submittedVia: "mcp", kind: "ask" });
    await fetchAdminQuestions({});
    assert.equal(urls.length, 2);
    assert.ok(urls[0].pathname.endsWith("/admin/questions"), urls[0].pathname);
    assert.equal(urls[0].searchParams.get("submitted_via"), "mcp");
    assert.equal(urls[0].searchParams.get("kind"), "ask");
    assert.equal(urls[1].searchParams.has("submitted_via"), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("范围筛选以 scope 查询参数发出，未选时不带该参数", async () => {
  const originalFetch = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(new URL(String(url), "http://localhost"));
    return new Response(JSON.stringify({
      items: [], stats: { total: 0, asks: 0, reports: 0, active_users: 0, global_asks: 0 },
      total: 0, offset: 0, limit: 50,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    await fetchAdminQuestions({ scope: "global" });
    await fetchAdminQuestions({});
    assert.equal(urls.length, 2);
    assert.equal(urls[0].searchParams.get("scope"), "global");
    assert.equal(urls[1].searchParams.has("scope"), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
