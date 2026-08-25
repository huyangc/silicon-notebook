import assert from "node:assert/strict";
import test from "node:test";

import {
  buildImageGallery,
  currentPreviewImage,
  imagePreviewRequest,
} from "../../app/image-preview.ts";

const image = (assetId, caption = "") => ({ element_id: `el-${assetId}`, asset_id: assetId, caption });

// 画册的**顺序**不归它管：slots 是渲染管线记下的落位账目（已按正文顺序、已按资产
// 去重）。这里只钉「把账目翻成预览条目」这一半。顺序那一半由
// tests/component/answer-citation-images.component.test.tsx 在真渲染上按 DOM 对账。
const refs = {
  k1: { displayLabel: "[1]", images: [image("a", "图 1"), image("b")] },
  k2: { displayLabel: "[2]", images: [image("c", "图 3")] },
};
const resolve = (key) => refs[key];

test("按账目逐条翻成预览条目，标签与 alt 取自该条引用", () => {
  const gallery = buildImageGallery(
    [
      { citationKey: "k1", imageId: "a" },
      { citationKey: "k1", imageId: "b" },
      { citationKey: "k2", imageId: "c" },
    ],
    resolve,
  );

  assert.deepEqual(gallery.map((item) => item.assetId), ["a", "b", "c"]);
  assert.deepEqual(gallery.map((item) => item.referenceLabel), ["[1]", "[1]", "[2]"]);
  // caption 只作 alt；没有 caption 时回落到「引用 的附图」的既有措辞。
  assert.deepEqual(gallery.map((item) => item.alt), ["图 1", "[1] 的附图", "图 3"]);
});

test("账目逐字决定顺序，绝不在这里重排", () => {
  const gallery = buildImageGallery(
    [{ citationKey: "k2", imageId: "c" }, { citationKey: "k1", imageId: "a" }],
    resolve,
  );
  assert.deepEqual(gallery.map((item) => item.assetId), ["c", "a"]);
});

test("解析不出引用、或该引用没有这张图的账目条目直接跳过", () => {
  const gallery = buildImageGallery(
    [
      { citationKey: "k9", imageId: "a" },
      { citationKey: "k1", imageId: "zz" },
      { citationKey: "k1", imageId: "a" },
    ],
    resolve,
  );
  assert.deepEqual(gallery.map((item) => item.assetId), ["a"]);
});

test("点开某一张会定位到它在画册里的位置", () => {
  const gallery = buildImageGallery(
    [
      { citationKey: "k1", imageId: "a" },
      { citationKey: "k1", imageId: "b" },
      { citationKey: "k2", imageId: "c" },
    ],
    resolve,
  );
  const request = imagePreviewRequest(gallery, gallery[2]);

  assert.equal(request.index, 2);
  assert.equal(request.items, gallery);
  assert.equal(currentPreviewImage(request).assetId, "c");
});

test("画册里找不到这张图时退化成单张，绝不改开另一张", () => {
  const gallery = buildImageGallery([{ citationKey: "k1", imageId: "a" }], resolve);
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
