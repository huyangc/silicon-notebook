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
      agent_profile_enabled: true,
      user_search_profile_enabled: true,
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
      agent_profile_enabled: true,
      user_search_profile_enabled: true,
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
      "pdf", "md", "markdown", "zip", "docx", "pptx", "csv", "xlsx", "xlsm", "xls",
    ]);
    assert.deepEqual(config.parser_engines, []);
    // 旧后端同样不下发图片护栏三兄弟:上限缺失 = 不做本地预检(`null`),
    // 开关缺失 = 视为开启(不能凭空对旧部署弹出「图片不会被保存」的警告)。
    assert.equal(config.source_image_max_bytes, null);
    assert.equal(config.source_image_max_per_source, null);
    assert.equal(config.source_images_enabled, true);
    // user_search_profile_enabled 刻意不下发,模拟旧后端——缺失按 false
    // (codex #535 R1 P2:字段与 PATCH /me/search-profile 端点同批新增,缺字段
    // = 旧后端没有那个路由,按 true 渲染入口只会让保存打出裸 404)。
    assert.equal(config.user_search_profile_enabled, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchSystemConfiguration honors an explicit false for user_search_profile_enabled", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    user_search_profile_enabled: false,
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    assert.equal(config.user_search_profile_enabled, false);
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
    source_image_max_bytes: -1,
    source_image_max_per_source: 3.5,
    source_images_enabled: "false",
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    // 负数/非整数是坏值,不是可执行的配置 → `null`(不做本地预检)。
    assert.equal(config.source_image_max_bytes, null);
    assert.equal(config.source_image_max_per_source, null);
    // 非布尔的 "false" 字符串不是显式 `false`:只有真值 `false` 才关闭本地预检/内联,
    // 其余一律按开启处理,保守方向不变。
    assert.equal(config.source_images_enabled, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchSystemConfiguration preserves an explicit zero image guard (a legal deployment value)", async () => {
  // codex #518 R1 P2:`MINERU_MAX_IMAGE_BYTES=0` / `MINERU_MAX_IMAGES_PER_SOURCE=0`
  // 是合法部署值(后端转发这两个字段时刻意没有正数约束,见
  // backend/tests/test_source_upload_size_limit.py 的零值用例),语义是「一张都不
  // 持久化」——与 `null`(拿不到上限,不做预检)恰恰相反。折成 `null` 会让打包上传
  // 按「无上限」照常 base64 内联并报「N 张已内联」,而服务端把资产全部丢弃。
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    source_image_max_bytes: 0,
    source_image_max_per_source: 0,
    source_images_enabled: true,
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    assert.equal(config.source_image_max_bytes, 0);
    assert.equal(config.source_image_max_per_source, 0);
    // 总开关本身仍是 true:有效关闭态由 bundle-intake 的
    // `bundleImagesEffectivelyEnabled` 从这三个值推导,解析层不替它做判断。
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

test("fetchSystemConfiguration treats a missing agent_profile_enabled as unavailable (old backend)", async () => {
  // Agentic Memory P1(T6)。这个字段与四个理解端点是同一批新增的,缺失可靠地
  // 说明该后端根本没有这个特性——兜底成 true 会让入口按钮在打不开任何端点的
  // 旧后端上出现。
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    // agent_profile_enabled 刻意不下发,模拟旧后端。
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    assert.equal(config.agent_profile_enabled, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetchSystemConfiguration keeps an explicit agent_profile_enabled true", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    agent_profile_enabled: true,
  }), {
    status: 200,
  });
  try {
    const config = await fetchSystemConfiguration();
    assert.equal(config.agent_profile_enabled, true);
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
