import { test } from "node:test";
import assert from "node:assert";
import { sourceImageAssetUrl } from "./source-image.ts";

test("builds notebook-scoped asset url", () => {
  assert.equal(
    sourceImageAssetUrl("http://api", "nb-1", "asset-9"),
    "http://api/notebooks/nb-1/assets/asset-9",
  );
});

test("returns empty when asset id missing", () => {
  assert.equal(sourceImageAssetUrl("http://api", "nb-1", ""), "");
});
