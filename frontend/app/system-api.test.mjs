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

test("fetchSystemConfiguration defaults user_activity_view_enabled to true when the field is missing (old backend)", async () => {
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
