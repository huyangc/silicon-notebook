import test from "node:test";
import assert from "node:assert/strict";

import { fetchSystemConfiguration } from "./system-api.ts";

test("fetchSystemConfiguration uses the authenticated small config endpoint and validates its byte cap", async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const calls = [];
  globalThis.window = { localStorage: { getItem: () => "source-upload-token" } };
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({
      source_upload_max_bytes: 50 * 1024 * 1024,
      source_upload_max_files_per_batch: 20,
      user_activity_view_enabled: false,
    }), {
      status: 200,
    });
  };

  try {
    assert.deepEqual(await fetchSystemConfiguration(), {
      source_upload_max_bytes: 50 * 1024 * 1024,
      source_upload_max_files_per_batch: 20,
      user_activity_view_enabled: false,
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }

  assert.match(calls[0].url, /\/api\/system\/config$/);
  assert.equal(calls[0].init.headers.get("Authorization"), "Bearer source-upload-token");
});

test("fetchSystemConfiguration treats a missing user_activity_view_enabled as unavailable (old backend)", async () => {
  // 方向是载荷性的。这个能力位与三个活动端点是同一次改动一起上线的,所以
  // 「字段缺失」恰恰证明该后端没有活动视图——不存在「字段缺失但端点存在」的组合。
  // 兜底成 true 会让新前端配旧后端时默认打开一个请求全 404 的 tab。
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    // user_activity_view_enabled 刻意不下发,模拟旧后端。
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    assert.equal(config.user_activity_view_enabled, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchSystemConfiguration keeps an explicit true (new backend, switch on)", async () => {
  // 反向对照:上面那条单独看,把解析写成恒 false 也能通过。
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    user_activity_view_enabled: true,
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    assert.equal(config.user_activity_view_enabled, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchSystemConfiguration rejects malformed or unsafe byte caps", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 0,
    source_upload_max_files_per_batch: 20,
  }), {
    status: 200,
  });
  try {
    await assert.rejects(fetchSystemConfiguration(), /系统上传配置格式无效/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
