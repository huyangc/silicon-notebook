import { test } from "node:test";
import assert from "node:assert";
import { requestBlob } from "../../app/api-client.ts";
import { assetNotebookId, sourceImageAssetUrl } from "../../app/source-image.ts";

test("builds notebook-scoped asset url", () => {
  assert.equal(
    sourceImageAssetUrl("http://api", "nb-1", "asset-9"),
    "http://api/notebooks/nb-1/assets/asset-9",
  );
});

test("a relative API base yields a base-relative path, not a doubled prefix", () => {
  // 同源反代部署把 API_BASE 配成 "/api"。取图地址最终交给 requestBlob,它会把以 "/"
  // 开头的入参当作相对 API_BASE 的路径再解析一次——这里若把 "/api" 拼在前面,
  // 最后发出去的就是 /api/api/notebooks/…,每一张图都 404。
  assert.equal(
    sourceImageAssetUrl("/api", "nb-1", "asset-9"),
    "/notebooks/nb-1/assets/asset-9",
  );
  assert.equal(
    sourceImageAssetUrl("https://host.example/api", "nb-1", "asset-9"),
    "https://host.example/api/notebooks/nb-1/assets/asset-9",
  );
});

test("returns empty when asset id missing", () => {
  assert.equal(sourceImageAssetUrl("http://api", "nb-1", ""), "");
});

// 取图归属只有这一条规则。三种情形逐条钉死——第二条(没有 active 才用条目自己的库)
// 是全局问答能出图的全部依据,第一条(有 active 恒用 active)是笔记本内问答**不得**
// 拿条目库 id 去直连另一个库的那道闸,两条都不许被「简化」成同一个表达式。
test("asset notebook: active wins, item notebook only fills in for the global surface", () => {
  // ① 有 active:即使条目声明了另一个库,也恒用 active(经它的参与集代理)。
  assert.equal(assetNotebookId("nb-active", "nb-other"), "nb-active");
  assert.equal(assetNotebookId("nb-active", ""), "nb-active");
  assert.equal(assetNotebookId("nb-active", null), "nb-active");
  assert.equal(assetNotebookId("nb-active", undefined), "nb-active");
  // ② 没有 active(全局问答):用条目自己的所属库,服务端仍逐次复核读权。
  assert.equal(assetNotebookId(null, "nb-own"), "nb-own");
  assert.equal(assetNotebookId("", "nb-own"), "nb-own");
  assert.equal(assetNotebookId(undefined, "nb-own"), "nb-own");
  // ③ 两者皆空:空串,调用方据此整块不渲染。
  assert.equal(assetNotebookId(null, null), "");
  assert.equal(assetNotebookId("", ""), "");
  assert.equal(assetNotebookId(undefined, undefined), "");
});

test("asset url built from the resolved owner is the notebook-scoped endpoint", () => {
  assert.equal(
    sourceImageAssetUrl("http://api", assetNotebookId(null, "nb-own"), "asset-9"),
    "http://api/notebooks/nb-own/assets/asset-9",
  );
  assert.equal(sourceImageAssetUrl("http://api", assetNotebookId(null, null), "asset-9"), "");
});

test("authenticated image blobs stay under the API boundary and never send bearer auth externally", async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const calls = [];
  globalThis.window = {
    localStorage: { getItem: () => "token-1" },
  };
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response("image-bytes", { status: 200 });
  };
  try {
    const blob = await requestBlob("/notebooks/nb-1/assets/asset-9", { tag: "source-image" });
    assert.equal(await blob.text(), "image-bytes");
    assert.equal(calls[0].init.headers.get("Authorization"), "Bearer token-1");
    await assert.rejects(
      requestBlob("https://images.example/asset.png", { tag: "source-image" }),
      /must stay under API_BASE/,
    );
    assert.equal(calls.length, 1);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
  }
});
