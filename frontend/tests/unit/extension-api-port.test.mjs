import test from "node:test";
import assert from "node:assert/strict";

import { requestBlob, requestJson, requestVoid } from "../../app/api-client.ts";
import { toUserMessage } from "../../app/errors.ts";
import {
  EXTENSION_API_TRANSPORT,
  createWorkspaceExtensionApi,
  extensionApiPath,
  withExtensionApi,
} from "../../features/extension-sdk/api.ts";


function recordingTransport() {
  const calls = [];
  return {
    calls,
    transport: {
      requestJson: (path, options) => {
        calls.push({ method: "requestJson", path, options });
        return Promise.resolve({ ok: true });
      },
      requestVoid: (path, options) => {
        calls.push({ method: "requestVoid", path, options });
        return Promise.resolve();
      },
      requestBlob: (path, options) => {
        calls.push({ method: "requestBlob", path, options });
        return Promise.resolve("blob");
      },
      toUserMessage: (error, fallback) => {
        calls.push({ method: "toUserMessage", error, fallback });
        return "translated";
      },
    },
  };
}


test("a legal relative path is prefixed with the calling plugin's own namespace", () => {
  assert.equal(extensionApiPath("ieee_xplore", "/search"), "/extensions/ieee_xplore/search");
  assert.equal(
    extensionApiPath("ieee_xplore", "/notebooks/abc-123/items"),
    "/extensions/ieee_xplore/notebooks/abc-123/items",
  );
  // 结尾 `/` 合法（`/collection/` 与 `/collection` 是两个资源，端口不替插件裁剪）。
  assert.equal(extensionApiPath("ieee.xplore", "/search/"), "/extensions/ieee.xplore/search/");
  assert.equal(extensionApiPath("a", "/x~y.z-0"), "/extensions/a/x~y.z-0");
  // 下划线合法，包括段首（RESTful 资源名常见蛇形命名，如 `import_selected`）。
  assert.equal(extensionApiPath("a", "/import_selected"), "/extensions/a/import_selected");
  assert.equal(extensionApiPath("a", "/a_b/c_d/"), "/extensions/a/a_b/c_d/");
  // 段首本身就是下划线：这一条才真正只被「段首字符类含 `_`」放行——
  // `/import_selected` 与 `/a_b/c_d/` 两段都以字母开头，段首字符类本来就一直
  // 含字母数字，光靠它们证明不了段首字符类被扩过（变异验证钉住了这一点）。
  assert.equal(extensionApiPath("a", "/_selected"), "/extensions/a/_selected");
});


test("query lives only in the query field and is encoded by URLSearchParams", () => {
  assert.equal(
    extensionApiPath("ieee", "/search", { q: "a b&c=d", page: 2, deep: true }),
    "/extensions/ieee/search?q=a+b%26c%3Dd&page=2&deep=true",
  );
  // 空 query 不该凭空长出一个 `?`——它会进 URL、进日志、也可能进后端的路由匹配。
  assert.equal(extensionApiPath("ieee", "/search", {}), "/extensions/ieee/search");
  assert.equal(extensionApiPath("ieee", "/search", undefined), "/extensions/ieee/search");
});


test("every escape shape out of the plugin's own prefix is refused", () => {
  for (const path of [
    // 绝对 URL / 协议相对：整条离开本站。
    "https://evil.example/x",
    "//evil/x",
    "http://127.0.0.1:8000/api/notebooks/x",
    // 不以 `/` 开头：会拼成**兄弟前缀**（`/extensions/ieeesearch`），不是本插件的子路径。
    "search",
    "",
    // 只有一个 `/`：既没有段，拼出来就是插件前缀本身。
    "/",
    // 路径穿越，含它的百分号编码形态。
    "/../notebooks/x",
    "/./x",
    "/%2e%2e/notebooks",
    // 空段：`//` 在部分服务端与代理上会被折叠或重解析。
    "/a//b",
    // 反斜杠、空白、查询串与 fragment——查询串只许走 query 字段。
    "/a\\b",
    "/x?y=1",
    "/x#y",
    "/ext ra",
  ]) {
    assert.throws(
      () => extensionApiPath("ieee", path),
      TypeError,
      `must refuse ${JSON.stringify(path)}`,
    );
  }
});


test("a plugin id that is not a registered stable id is refused", () => {
  for (const pluginId of ["Ieee", "-x", "ieee..x", "../other", "", "ieee/x", "ieee ", "1ieee"]) {
    assert.throws(
      () => extensionApiPath(pluginId, "/x"),
      TypeError,
      `must refuse plugin id ${JSON.stringify(pluginId)}`,
    );
  }
});


test("the port confines the path and freezes the core request identity", async () => {
  const { calls, transport } = recordingTransport();
  const api = createWorkspaceExtensionApi("ieee", transport);
  const controller = new AbortController();
  const result = await api.requestJson("/search", {
    method: "POST",
    body: "{}",
    headers: { "X-Plugin": "1" },
    signal: controller.signal,
    query: { q: "1" },
    // 插件运行时多塞的键：TypeScript 挡得住老实的调用方，挡不住这些。核心的鉴权与
    // 诊断口径不能被插件改写——`unauthorized: "preserve"` 会关掉 401 清 token 重载,
    // 伪造的 `tag` 会把插件的失败记进核心诊断。
    tag: "plugin-tag",
    auth: "none",
    unauthorized: "preserve",
    credentials: "include",
    mode: "no-cors",
  });
  assert.deepEqual(result, { ok: true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "requestJson");
  assert.equal(calls[0].path, "/extensions/ieee/search?q=1");
  assert.deepEqual(Object.keys(calls[0].options).sort(), [
    "auth", "body", "headers", "method", "signal", "tag", "unauthorized",
  ]);
  assert.equal(calls[0].options.tag, "extension");
  assert.equal(calls[0].options.auth, "required");
  assert.equal(calls[0].options.unauthorized, "clear-and-reload");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.body, "{}");
  assert.deepEqual(calls[0].options.headers, { "X-Plugin": "1" });
  assert.equal(calls[0].options.signal, controller.signal);
  // query 属于路径，不该同时出现在 options 里；credentials/mode 是插件够不着的传输语义。
  assert.equal("query" in calls[0].options, false);
  assert.equal("credentials" in calls[0].options, false);
  assert.equal("mode" in calls[0].options, false);
});


test("all three verbs and the message translator go through the same confinement", async () => {
  const { calls, transport } = recordingTransport();
  const api = createWorkspaceExtensionApi("ieee", transport);
  await api.requestVoid("/items");
  await api.requestBlob("/items/export");
  assert.equal(api.userMessage(new TypeError("boom"), "回退文案"), "translated");
  assert.deepEqual(calls.map((entry) => [entry.method, entry.path]), [
    ["requestVoid", "/extensions/ieee/items"],
    ["requestBlob", "/extensions/ieee/items/export"],
    ["toUserMessage", undefined],
  ]);
  assert.equal(calls[2].fallback, "回退文案");
  // 无 init 时也不得漏掉核心身份（`?? {}` 那条分支同样要固定三项）。
  assert.equal(calls[0].options.tag, "extension");
  assert.equal(calls[0].options.auth, "required");
  assert.equal(calls[0].options.unauthorized, "clear-and-reload");
  assert.deepEqual(Object.keys(calls[0].options).sort(), ["auth", "tag", "unauthorized"]);
  // 同步抛而不是返回一个 rejected promise：限定发生在碰 transport 之前，插件写
  // `void api.requestVoid(...)`（不接 promise）时也仍然是响亮失败。
  assert.throws(() => api.requestVoid("/../notebooks/x"), TypeError);
  assert.equal(calls.length, 3, "拒绝的请求一个字节都不许到达 transport");
  assert.equal(Object.isFrozen(api), true);
});


test("the default transport is the core api client itself, not a shadow copy", () => {
  // 引用相等而不是"存在同名函数"：一份影子实现会绕开 api-client 的 API_BASE 约束、
  // 鉴权头注入与 401 清 token 重载，而单测里那些假 transport 什么都证明不了。
  assert.equal(EXTENSION_API_TRANSPORT.requestJson, requestJson);
  assert.equal(EXTENSION_API_TRANSPORT.requestVoid, requestVoid);
  assert.equal(EXTENSION_API_TRANSPORT.requestBlob, requestBlob);
  assert.equal(EXTENSION_API_TRANSPORT.toUserMessage, toUserMessage);
  assert.equal(Object.isFrozen(EXTENSION_API_TRANSPORT), true);
});


test("withExtensionApi keeps the host's owner gate and binds the api to that contribution", () => {
  const opened = [];
  const generation = { current: 1 };
  // 宿主那份 actions 的 openUnderstanding 是一个闭包着 owner 的窄 command；
  // 注入 api 时必须原样搬过去，不得重新包一层（那会让闸失效或被调用两次）。
  const ownedActions = {
    openUnderstanding() {
      if (generation.current !== 1) return;
      opened.push("open");
    },
  };
  const stale = withExtensionApi(ownedActions, "ieee");
  assert.equal(Object.isFrozen(stale), true);
  stale.openUnderstanding();
  assert.deepEqual(opened, ["open"]);
  generation.current = 3;
  stale.openUnderstanding();
  assert.deepEqual(opened, ["open"], "过期 owner 的回调必须继续被宿主的闸拒绝");
  assert.equal(Object.isFrozen(stale.api), true);
  // 注入进来的确实是一个受限端口，不是随手挂的转发函数：越界路径在碰 transport
  // 之前就同步抛（这里刻意不调合法路径——默认 transport 是真的核心 api 客户端）。
  assert.throws(() => stale.api.requestJson("/../notebooks/x"), TypeError);
  // 端口绑的是传进去的那个 pluginId，两条 contribution 各拿各的前缀。
  assert.equal(extensionApiPath("ieee", "/x"), "/extensions/ieee/x");
  assert.equal(extensionApiPath("other", "/x"), "/extensions/other/x");
});
