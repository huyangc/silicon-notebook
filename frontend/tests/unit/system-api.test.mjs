import test from "node:test";
import assert from "node:assert/strict";

import { fetchSystemConfiguration } from "../../app/system-api.ts";

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
      supported_source_extensions: ["pdf", "md"],
      parser_engines: [{
        id: "builtin",
        priority: 3,
        execution: "local",
        file_extensions: ["pdf", "md"],
        capabilities: ["structured_text", "headings"],
        supports_url: false,
        fallback: true,
        available: true,
        unavailable_reason: null,
      }],
      report_max_sections: 7,
      report_max_subqueries_per_section: 6,
      user_activity_view_enabled: false,
      source_image_max_bytes: 5 * 1024 * 1024,
      source_image_max_per_source: 200,
      source_images_enabled: false,
    }), {
      status: 200,
    });
  };

  try {
    assert.deepEqual(await fetchSystemConfiguration(), {
      source_upload_max_bytes: 50 * 1024 * 1024,
      source_upload_max_files_per_batch: 20,
      supported_source_extensions: ["pdf", "md"],
      parser_engines: [{
        id: "builtin",
        priority: 3,
        execution: "local",
        file_extensions: ["pdf", "md"],
        capabilities: ["structured_text", "headings"],
        supports_url: false,
        fallback: true,
        available: true,
        unavailable_reason: null,
      }],
      report_max_sections: 7,
      report_max_subqueries_per_section: 6,
      user_activity_view_enabled: false,
      source_image_max_bytes: 5 * 1024 * 1024,
      source_image_max_per_source: 200,
      source_images_enabled: false,
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
    assert.equal(config.report_max_sections, 6);
    assert.equal(config.report_max_subqueries_per_section, 4);
    assert.deepEqual(config.supported_source_extensions, [
      "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm", "xls",
    ]);
    assert.deepEqual(config.parser_engines, []);
    // 旧后端同样不下发图片护栏三兄弟:上限缺失 = 不做本地预检(`null`),
    // 开关缺失 = 视为开启(不能凭空对旧部署弹出「图片不会被保存」的警告)。
    assert.equal(config.source_image_max_bytes, null);
    assert.equal(config.source_image_max_per_source, null);
    assert.equal(config.source_images_enabled, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchSystemConfiguration parses the image guard trio when the backend sends them", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    source_image_max_bytes: 2 * 1024 * 1024,
    source_image_max_per_source: 12,
    source_images_enabled: false,
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    assert.equal(config.source_image_max_bytes, 2 * 1024 * 1024);
    assert.equal(config.source_image_max_per_source, 12);
    assert.equal(config.source_images_enabled, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchSystemConfiguration treats malformed image guard values as absent, not a hard failure", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    source_image_max_bytes: 0,
    source_image_max_per_source: -3,
    source_images_enabled: "false",
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    assert.equal(config.source_image_max_bytes, null);
    assert.equal(config.source_image_max_per_source, null);
    // 非布尔的 "false" 字符串不是显式 `false`:只有真值 `false` 才关闭本地预检/内联,
    // 其余一律按开启处理,保守方向不变。
    assert.equal(config.source_images_enabled, true);
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
