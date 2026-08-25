import assert from "node:assert/strict";
import test from "node:test";

import {
  buildImageGallery,
  currentPreviewImage,
  imagePreviewRequest,
} from "../../app/image-preview.ts";

const image = (assetId, caption = "") => ({ element_id: `el-${assetId}`, asset_id: assetId, caption });

test("画册按引用出现顺序排列，同一张图只保留第一次出现的位置", () => {
  const gallery = buildImageGallery([
    { displayLabel: "[1]", images: [image("a", "图 1"), image("b")] },
    { displayLabel: "[2]", images: [image("b"), image("c", "图 3")] },
  ]);

  assert.deepEqual(gallery.map((item) => item.assetId), ["a", "b", "c"]);
  // 重复的 b 留在 [1] 名下——它在正文里就是跟着 [1] 显示的那一次。
  assert.deepEqual(gallery.map((item) => item.referenceLabel), ["[1]", "[1]", "[2]"]);
  // caption 只作 alt；没有 caption 时回落到「引用 的附图」的既有措辞。
  assert.deepEqual(gallery.map((item) => item.alt), ["图 1", "[1] 的附图", "图 3"]);
});

test("没有 asset_id 的条目不进画册", () => {
  const gallery = buildImageGallery([{ displayLabel: "[1]", images: [image(""), image("a")] }]);
  assert.deepEqual(gallery.map((item) => item.assetId), ["a"]);
});

test("点开某一张会定位到它在画册里的位置", () => {
  const gallery = buildImageGallery([
    { displayLabel: "[1]", images: [image("a"), image("b"), image("c")] },
  ]);
  const request = imagePreviewRequest(gallery, gallery[2]);

  assert.equal(request.index, 2);
  assert.equal(request.items, gallery);
  assert.equal(currentPreviewImage(request).assetId, "c");
});

test("画册里找不到这张图时退化成单张，绝不改开另一张", () => {
  const gallery = buildImageGallery([{ displayLabel: "[1]", images: [image("a")] }]);
  const orphan = { assetId: "zz", alt: "孤图", referenceLabel: "[9]" };
  const request = imagePreviewRequest(gallery, orphan);

  assert.equal(request.index, 0);
  assert.deepEqual(request.items, [orphan]);
  assert.equal(currentPreviewImage(request).assetId, "zz");
});

test("越界下标不返回图片，由调用方决定不渲染", () => {
  assert.equal(currentPreviewImage(null), null);
  assert.equal(currentPreviewImage({ items: [], index: 0 }), null);
  assert.equal(
    currentPreviewImage({ items: [{ assetId: "a", alt: "", referenceLabel: "" }], index: 3 }),
    null,
  );
});
