// `GET /admin/extensions` 前端解析器的防御性契约。
//
// 与 `system-extensions-api.test.mjs` 钉的是**相反的方向**,这不是疏漏:那一份喂
// 渲染管线,读不懂就整份 fail closed;这一份喂管理员的只读运维清单,管理员打开它
// 往往正是因为部署侧刚出了状况——此时因为一个未知字段把整页换成「格式无效」,
// 恰好在最需要可见性的时刻把它拿走。所以这里逐行降级,绝不抛。
import test from "node:test";
import assert from "node:assert/strict";

import { parseLoadedExtensions, setExtensionRuntimeEnabled } from "../../app/admin/extensions/api.ts";
import { httpErrorStatus } from "../../app/errors.ts";


const wire = {
  api_version: "1",
  extensions: [{
    id: "builtin.ask_agent_profile",
    version: "1.0.0",
    trust: "builtin",
    display_name: "Ask agent-profile completion",
    contributions: [{
      id: "builtin.ask_agent_profile",
      point: "ask.completed_observer",
      kind: "observer",
    }],
    ui_contributions: [{
      id: "builtin.ask_agent_profile.workspace_panel",
      slot: "workspace.side_panel",
      capability: "ui.agent_profile.available",
    }],
  }],
};


test("解析器把后端形状原样翻成页面形状", () => {
  assert.deepEqual(parseLoadedExtensions(wire), {
    apiVersion: "1",
    versionRecognized: true,
    extensions: [{
      id: "builtin.ask_agent_profile",
      displayName: "Ask agent-profile completion",
      version: "1.0.0",
      trust: "builtin",
      contributions: [{
        id: "builtin.ask_agent_profile",
        point: "ask.completed_observer",
        kind: "observer",
      }],
      uiContributions: [{
        id: "builtin.ask_agent_profile.workspace_panel",
        slot: "workspace.side_panel",
        capability: "ui.agent_profile.available",
      }],
      // builtin 行:三个运行时字段恒为 null(见 api.ts 的字段注释——builtin 只读,
      // 后端也确实一律回 None)。
      runtimeEnabled: null,
      runtimeUpdatedBy: null,
      runtimeUpdatedAt: null,
    }],
  });
});


test("deployment 行的运行时三字段原样解析:合法值透传,builtin 恒 null", () => {
  const parsed = parseLoadedExtensions({
    api_version: "1",
    extensions: [{
      id: "corp.sample",
      trust: "deployment",
      runtime_enabled: false,
      runtime_updated_by: "user-admin-1",
      runtime_updated_at: "2026-08-29T10:00:00",
    }],
  });
  assert.deepEqual(parsed.extensions, [{
    id: "corp.sample",
    displayName: "",
    version: "",
    trust: "deployment",
    contributions: [],
    uiContributions: [],
    runtimeEnabled: false,
    runtimeUpdatedBy: "user-admin-1",
    runtimeUpdatedAt: "2026-08-29T10:00:00",
  }]);
});


test("PG 带偏移的 updated_at 一样原样透传,不做任何字符串改写", () => {
  const [row] = parseLoadedExtensions({
    api_version: "1",
    extensions: [{
      id: "corp.sample",
      trust: "deployment",
      runtime_enabled: true,
      runtime_updated_by: "user-admin-1",
      runtime_updated_at: "2026-08-29T10:00:00+00:00",
    }],
  }).extensions;
  assert.equal(row.runtimeUpdatedAt, "2026-08-29T10:00:00+00:00");
});


test("运行时三字段缺失/类型不对一律退回 null,不把无效值透出去", () => {
  const [row] = parseLoadedExtensions({
    api_version: "1",
    extensions: [{
      id: "corp.sample",
      trust: "deployment",
      runtime_enabled: "yes",       // 非布尔
      runtime_updated_by: 42,       // 非字符串
      // runtime_updated_at 缺失
    }],
  }).extensions;
  assert.equal(row.runtimeEnabled, null);
  assert.equal(row.runtimeUpdatedBy, null);
  assert.equal(row.runtimeUpdatedAt, null);
});


test("未知字段忽略,缺字段给安全默认", () => {
  const parsed = parseLoadedExtensions({
    api_version: "1",
    reason_code: "leaked", // 未知顶层字段:忽略,不是致命错误
    extensions: [{
      id: "corp.sample",
      module_path: "corp_sample.plugin", // 未知行内字段:忽略
      // version / display_name / trust / contributions / ui_contributions 全缺
    }],
  });
  assert.equal(parsed.versionRecognized, true);
  assert.deepEqual(parsed.extensions, [{
    id: "corp.sample",
    displayName: "",
    version: "",
    trust: "unknown",
    contributions: [],
    uiContributions: [],
    runtimeEnabled: null,
    runtimeUpdatedBy: null,
    runtimeUpdatedAt: null,
  }]);
});


// 这条是 T8 的承重断言:缺 `contributions` 必须落成空数组。给不出安全默认,
// 页面就会对 `undefined` 取 `.length` 而整页白屏——恰好在部署侧出状况时。
test("缺 contributions / ui_contributions 落成空数组而不是 undefined", () => {
  const [row] = parseLoadedExtensions({
    api_version: "1",
    extensions: [{ id: "corp.sample" }],
  }).extensions;
  assert.deepEqual(row.contributions, []);
  assert.deepEqual(row.uiContributions, []);
  assert.equal(row.contributions.length, 0);
  assert.equal(row.uiContributions.length, 0);
});


test("类型不对的字段一律退回安全默认,不把原值透出去", () => {
  const [row] = parseLoadedExtensions({
    api_version: "1",
    extensions: [{
      id: "corp.sample",
      version: 3,                 // 非字符串
      display_name: null,
      trust: "self_signed",       // 认不出的信任档位
      contributions: "not-a-list",
      ui_contributions: [
        { id: "ok", slot: 7, capability: undefined },
        { slot: "workspace.side_panel" }, // 无身份:整行丢掉
        null,
      ],
    }],
  }).extensions;
  assert.equal(row.version, "");
  assert.equal(row.displayName, "");
  assert.equal(row.trust, "unknown");
  assert.deepEqual(row.contributions, []);
  assert.deepEqual(row.uiContributions, [{ id: "ok", slot: "", capability: "" }]);
});


test("认不出身份的扩展行丢掉,其余照常返回", () => {
  const parsed = parseLoadedExtensions({
    api_version: "1",
    extensions: [{ version: "1.0.0" }, null, 42, wire.extensions[0]],
  });
  assert.deepEqual(parsed.extensions.map((row) => row.id), ["builtin.ask_agent_profile"]);
});


// api_version 不一致只是一条提示:后端比页面新时已知字段大概率仍读得懂,把它当
// 致命错误等于让一次灰度发布把整页变成白屏。
test("api_version 不一致不是致命错误,只翻掉 versionRecognized", () => {
  const parsed = parseLoadedExtensions({ ...wire, api_version: "2" });
  assert.equal(parsed.apiVersion, "2");
  assert.equal(parsed.versionRecognized, false);
  assert.equal(parsed.extensions.length, 1);
});


test("整份响应畸形时返回空拓扑而不抛", () => {
  for (const malformed of [null, undefined, 42, "text", [], { extensions: {} }]) {
    const parsed = parseLoadedExtensions(malformed);
    assert.deepEqual(parsed.extensions, []);
    assert.equal(parsed.versionRecognized, false);
  }
});


// --------------------------------------------------------------------------
// setExtensionRuntimeEnabled —— PATCH /admin/extensions/{plugin_id}
// --------------------------------------------------------------------------

test("setExtensionRuntimeEnabled 发 PATCH,body 带 enabled,响应体解析成驼峰形状", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({
      plugin_id: "corp.sample",
      runtime_enabled: false,
      runtime_updated_by: "user-admin-1",
      runtime_updated_at: "2026-08-29T10:00:00",
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    const result = await setExtensionRuntimeEnabled("corp.sample", false);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init.method, "PATCH");
    assert.deepEqual(JSON.parse(calls[0].init.body), { enabled: false });
    assert.ok(calls[0].url.endsWith("/admin/extensions/corp.sample"), calls[0].url);
    assert.deepEqual(result, {
      pluginId: "corp.sample",
      runtimeEnabled: false,
      runtimeUpdatedBy: "user-admin-1",
      runtimeUpdatedAt: "2026-08-29T10:00:00",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("setExtensionRuntimeEnabled 对 plugin_id 做 URL 编码", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return new Response(JSON.stringify({
      plugin_id: "corp/weird id",
      runtime_enabled: true,
      runtime_updated_by: "user-admin-1",
      runtime_updated_at: "2026-08-29T10:00:00",
    }), { status: 200 });
  };
  try {
    await setExtensionRuntimeEnabled("corp/weird id", true);
    assert.ok(calls[0].endsWith(encodeURIComponent("corp/weird id")), calls[0]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("setExtensionRuntimeEnabled 失败时把 HTTP 错误原样抛出,不吞、不自造成功", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("该扩展不存在或不支持运行时开关", {
    status: 404,
    headers: { "X-User-Message": "1" },
  });
  try {
    await assert.rejects(
      () => setExtensionRuntimeEnabled("missing.plugin", true),
      (error) => {
        assert.equal(httpErrorStatus(error), 404);
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
