import { test } from "node:test";
import assert from "node:assert";
import { requestBlob } from "../../app/api-client.ts";
import { sourceImageAssetUrl } from "../../app/source-image.ts";

test("builds notebook-scoped asset url", () => {
  assert.equal(
    sourceImageAssetUrl("http://api", "nb-1", "asset-9"),
    "http://api/notebooks/nb-1/assets/asset-9",
  );
});

test("returns empty when asset id missing", () => {
  assert.equal(sourceImageAssetUrl("http://api", "nb-1", ""), "");
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
