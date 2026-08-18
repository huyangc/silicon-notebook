import assert from "node:assert/strict";
import test from "node:test";

import {
  BUNDLE_DIR_MAX_FILES,
  NO_MARKDOWN_IN_BUNDLE_REASON,
  approxByteSizeLabel,
  bundleCapsFrom,
  bundleErrorMessage,
  classifyBundleContents,
  collectDirectoryFiles,
  directoryHasMarkdown,
  directoryTruncatedMessage,
  inlineTooLargeMessage,
  missingImageLine,
  noAltImageLine,
  processMarkdownCandidate,
  readDirectoryAsBundleFiles,
  receiptSummaryLine,
  remoteImageLine,
  unpackZipFile,
  unsupportedImageLine,
} from "../../app/bundle-intake.ts";
import { MD_BUNDLE_MAX_ENTRIES } from "../../app/md-bundle.ts";

const EMPTY = new Uint8Array(0);
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4]);

// ---------------------------------------------------------------------------
// bundleCapsFrom / approxByteSizeLabel / inlineTooLargeMessage
// ---------------------------------------------------------------------------

test("bundleCapsFrom: null（配置未到达）归一为 0，与 resolveLimit 的“非正数=不做预检”口径一致", () => {
  assert.deepEqual(bundleCapsFrom(null), { uploadMaxBytes: 0 });
  assert.deepEqual(bundleCapsFrom(50 * 1024 * 1024), { uploadMaxBytes: 50 * 1024 * 1024 });
});

test("approxByteSizeLabel: 按量级选择 MB/KB/字节，非整数场景不退化成裸字节数", () => {
  assert.equal(approxByteSizeLabel(2 * 1024 * 1024 + 512 * 1024), "约 2.5 MB");
  assert.equal(approxByteSizeLabel(1536), "约 1.5 KB");
  assert.equal(approxByteSizeLabel(500), "500 字节");
});

test("inlineTooLargeMessage: 同时报出实际体积与上限，且措辞给出可操作建议", () => {
  const msg = inlineTooLargeMessage(3 * 1024 * 1024, 1024 * 1024);
  assert.match(msg, /约 3\.0 MB/);
  assert.match(msg, /1 MB/);
  assert.match(msg, /精简图片或拆分文档/);
});

// ---------------------------------------------------------------------------
// bundleErrorMessage：zip 解析失败码 → 中文文案
// ---------------------------------------------------------------------------

test("bundleErrorMessage: 每个 BundleErrorCode 都有非空中文文案", () => {
  const codes = [
    "not_a_zip", "corrupt", "encrypted", "zip64", "unsupported_compression",
    "unsafe_entry_path", "too_many_entries", "too_large",
  ];
  for (const code of codes) {
    const msg = bundleErrorMessage({ code });
    assert.equal(typeof msg, "string");
    assert.ok(msg.length > 0, `${code} 缺少文案`);
  }
});

test("bundleErrorMessage: too_many_entries 带上具体上限数值", () => {
  assert.match(bundleErrorMessage({ code: "too_many_entries", limit: 2000 }), /2000/);
  // 未带 limit 时回退到 MD_BUNDLE_MAX_ENTRIES，而不是留空
  assert.match(bundleErrorMessage({ code: "too_many_entries" }), new RegExp(String(MD_BUNDLE_MAX_ENTRIES)));
});

// ---------------------------------------------------------------------------
// classifyBundleContents：零个／恰好一个／多个 markdown
// ---------------------------------------------------------------------------

test("classifyBundleContents: 零个 markdown → empty", () => {
  assert.deepEqual(classifyBundleContents([]), { kind: "empty" });
  assert.deepEqual(
    classifyBundleContents([{ path: "readme.txt", bytes: EMPTY }, { path: "pic.png", bytes: EMPTY }]),
    { kind: "empty" },
  );
});

test("classifyBundleContents: 恰好一个 markdown（含大小写扩展名）→ single", () => {
  const result = classifyBundleContents([
    { path: "pic.png", bytes: EMPTY },
    { path: "docs/NOTE.MARKDOWN", bytes: EMPTY },
  ]);
  assert.equal(result.kind, "single");
  assert.equal(result.file.path, "docs/NOTE.MARKDOWN");
});

test("classifyBundleContents: 多个 markdown → choose，候选保持原顺序", () => {
  const result = classifyBundleContents([
    { path: "a.md", bytes: EMPTY },
    { path: "pic.png", bytes: EMPTY },
    { path: "sub/b.markdown", bytes: EMPTY },
  ]);
  assert.equal(result.kind, "choose");
  assert.deepEqual(result.candidates.map((f) => f.path), ["a.md", "sub/b.markdown"]);
});

// ---------------------------------------------------------------------------
// processMarkdownCandidate：内联 + 命名 + 太大时拒绝
// ---------------------------------------------------------------------------

test("processMarkdownCandidate: 成功内联，文件名取 basename，回执带一张已内联", () => {
  const mdText = "# Title\n\n![a picture](pic.png)\n";
  const mdFile = { path: "docs/note.md", bytes: new TextEncoder().encode(mdText) };
  const picFile = { path: "docs/pic.png", bytes: PNG };
  const result = processMarkdownCandidate(mdFile, [mdFile, picFile], { uploadMaxBytes: 10_000_000 });
  assert.equal(result.ok, true);
  assert.equal(result.fileName, "note.md");
  assert.match(result.rewritten, /data:image\/png;base64,/);
  assert.equal(result.receipt.inlined.length, 1);
  assert.equal(result.receipt.missing.length, 0);
});

test("processMarkdownCandidate: 内联后超过单文件上限 → ok:false 且带体积明细，不返回可入列正文", () => {
  const mdText = "# Title\n\n![a picture](pic.png)\n";
  const mdFile = { path: "note.md", bytes: new TextEncoder().encode(mdText) };
  const picFile = { path: "pic.png", bytes: PNG };
  const result = processMarkdownCandidate(mdFile, [mdFile, picFile], { uploadMaxBytes: 5 });
  assert.equal(result.ok, false);
  assert.equal(result.fileName, "note.md");
  assert.equal(result.limit, 5);
  assert.ok(result.bytes > 5);
  // 回执仍描述了配对本身（这张图确实被配上、只是整体超限）
  assert.equal(result.receipt.inlined.length, 1);
});

// ---------------------------------------------------------------------------
// 回执文案函数
// ---------------------------------------------------------------------------

test("receiptSummaryLine: 逐类计数拼接，空回执给出「未发现」而不是空字符串", () => {
  assert.equal(
    receiptSummaryLine({ inlined: [1, 2], missing: [1], unsupported: [], remote: [], noAlt: [] }),
    "2 张已内联 / 1 张未找到",
  );
  assert.equal(
    receiptSummaryLine({ inlined: [], missing: [], unsupported: [], remote: [], noAlt: [] }),
    "未在正文中发现本地图片",
  );
});

test("missingImageLine: 有候选时列出候选，没有候选时只报未找到", () => {
  assert.match(
    missingImageLine({ src: "a.png", resolved: "a.png", suggestions: ["b/a.png", "c/a.png"], line: 0 }),
    /未找到「a\.png」，近似候选：b\/a\.png、c\/a\.png/,
  );
  assert.equal(
    missingImageLine({ src: "a.png", resolved: null, suggestions: [], line: 0 }),
    "未找到「a.png」",
  );
});

test("unsupportedImageLine: 每种 reason 都有专属措辞，且逐字对齐设计文档（末尾附上定位用的 src/path）", () => {
  assert.equal(
    unsupportedImageLine({ src: "x", reason: "inline_position", line: 0 }),
    "不在独立段落中，本次未内联、保留原始链接（x）",
  );
  assert.equal(
    unsupportedImageLine({ src: "x", reason: "reference_syntax", line: 0 }),
    "引用式图片语法暂不支持内联（x）",
  );
  assert.equal(
    unsupportedImageLine({ src: "x", reason: "html_syntax", line: 0 }),
    "HTML 图片标签暂不支持内联（x）",
  );
  assert.match(
    unsupportedImageLine({ src: "x.svg", reason: "unsupported_image_type", path: "assets/x.svg", line: 0 }),
    /不支持的图片格式.*png\/jpeg\/gif\/webp.*assets\/x\.svg/,
  );
  // path 缺失时退回 src 作为定位，而不是留一个说不清是哪张图的裸原因
  assert.equal(
    unsupportedImageLine({ src: "y.svg", reason: "unsupported_image_type", line: 0 }),
    "不支持的图片格式（仅支持 png/jpeg/gif/webp）（y.svg）",
  );
});

test("unsupportedImageLine: image_too_large / too_many_images 有上限时带上具体数值，没有时不编造", () => {
  assert.match(
    unsupportedImageLine({ src: "x", reason: "image_too_large", limit: 1024, line: 0 }),
    /超过单张图片上限.*1 KB/,
  );
  assert.equal(
    unsupportedImageLine({ src: "x", reason: "image_too_large", line: 0 }),
    "超过单张图片上限（x）",
  );
  assert.match(
    unsupportedImageLine({ src: "x", reason: "too_many_images", limit: 20, line: 0 }),
    /超过单来源图片张数上限（20 张）/,
  );
});

test("remoteImageLine / noAltImageLine: 措辞逐字对齐设计文档", () => {
  assert.equal(
    remoteImageLine({ src: "https://example.com/a.png", line: 0 }),
    "云端图片链接未拉取，保留原文字（https://example.com/a.png）",
  );
  assert.equal(
    noAltImageLine({ src: "a.png", path: "imgs/a.png", line: 0 }),
    "「imgs/a.png」无图注，上传后无法被检索",
  );
});

test("NO_MARKDOWN_IN_BUNDLE_REASON: 零 markdown 的整包回执措辞", () => {
  assert.equal(NO_MARKDOWN_IN_BUNDLE_REASON, "压缩包里没有 markdown 文件");
});

test("directoryTruncatedMessage: 措辞里带上具体的文件数/层级上限，不是空泛的“太多了”", () => {
  const msg = directoryTruncatedMessage();
  assert.match(msg, new RegExp(String(BUNDLE_DIR_MAX_FILES)));
  assert.match(msg, /层/);
});

// ---------------------------------------------------------------------------
// 文件夹遍历（duck-typed File and Directory Entries API 打桩，不依赖真实 DOM）
// ---------------------------------------------------------------------------

function fakeFileEntry(name, contentText) {
  return {
    isFile: true,
    isDirectory: false,
    name,
    file(success) {
      success(new File([contentText], name));
    },
  };
}

function fakeDirEntry(name, children) {
  let delivered = false;
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader() {
      return {
        readEntries(success) {
          if (delivered) { success([]); return; }
          delivered = true;
          success(children);
        },
      };
    },
  };
}

test("collectDirectoryFiles: 递归收集路径（不含根名），跳过 .DS_Store", async () => {
  const root = fakeDirEntry("dropped-folder", [
    fakeFileEntry("note.md", "# hi"),
    fakeDirEntry("imgs", [fakeFileEntry("pic.png", "binary")]),
    fakeFileEntry(".DS_Store", "junk"),
  ]);
  const { entries, truncated } = await collectDirectoryFiles(root);
  assert.equal(truncated, false);
  assert.deepEqual(entries.map((e) => e.path).sort(), ["imgs/pic.png", "note.md"]);
});

test("collectDirectoryFiles: 命中文件数护栏 → truncated=true，不吃满全部文件", async () => {
  const root = fakeDirEntry("f", [
    fakeFileEntry("a.txt", "a"),
    fakeFileEntry("b.txt", "b"),
    fakeFileEntry("c.txt", "c"),
  ]);
  const { entries, truncated } = await collectDirectoryFiles(root, { maxFiles: 2 });
  assert.equal(truncated, true);
  assert.equal(entries.length, 2);
});

test("collectDirectoryFiles: 命中深度护栏时只截停更深的子目录，同级文件仍收集完整", async () => {
  // root -> a(dir, depth1) -> [x.txt(file), b(dir, depth2)] -> b 里的 y.txt 因深度被截停
  const dirB = fakeDirEntry("b", [fakeFileEntry("y.txt", "y")]);
  const dirA = fakeDirEntry("a", [fakeFileEntry("x.txt", "x"), dirB]);
  const root = fakeDirEntry("root", [dirA]);
  const { entries, truncated } = await collectDirectoryFiles(root, { maxDepth: 1 });
  assert.equal(truncated, true);
  assert.deepEqual(entries.map((e) => e.path), ["a/x.txt"]);
});

test("directoryHasMarkdown: 零 I/O 只看路径判断有没有 markdown（大小写不敏感）", () => {
  assert.equal(directoryHasMarkdown([{ path: "a.png", file: null }]), false);
  assert.equal(directoryHasMarkdown([{ path: "docs/NOTE.MD", file: null }]), true);
  assert.equal(directoryHasMarkdown([]), false);
});

test("readDirectoryAsBundleFiles: 真正读出字节，路径与内容一一对应", async () => {
  const entries = [
    { path: "note.md", file: new File(["# hi"], "note.md") },
    { path: "imgs/pic.bin", file: new File([PNG], "pic.bin") },
  ];
  const files = await readDirectoryAsBundleFiles(entries);
  assert.deepEqual(files.map((f) => f.path), ["note.md", "imgs/pic.bin"]);
  assert.equal(new TextDecoder().decode(files[0].bytes), "# hi");
  assert.deepEqual(files[1].bytes, PNG);
});

// ---------------------------------------------------------------------------
// unpackZipFile：读 File 字节 → 交给 md-bundle 的 parseZipBundle
// ---------------------------------------------------------------------------

const CENTRAL_SIG = 0x02014b50;
const LOCAL_SIG = 0x04034b50;
const EOCD_SIG = 0x06054b50;

/** 最小 store-only zip 构造器：只用于验证 unpackZipFile 这层"读 File → 交给
 *  parseZipBundle"的胶水代码本身，不重复 md-bundle.test.mjs 已经覆盖的 zip 格式
 *  边界（那些测试直接喂字节给 parseZipBundle，本文件不重复）。parseZipBundle 不校验
 *  CRC，这里全部写 0。 */
function makeStoreZip(entries) {
  const enc = new TextEncoder();
  const localParts = [];
  const localOffsets = [];
  let cursor = 0;
  for (const entry of entries) {
    const nameBytes = enc.encode(entry.name);
    const local = new Uint8Array(30 + nameBytes.length);
    const view = new DataView(local.buffer);
    view.setUint32(0, LOCAL_SIG, true);
    view.setUint16(4, 20, true);
    view.setUint16(8, 0, true); // method = store
    view.setUint32(18, entry.data.length, true);
    view.setUint32(22, entry.data.length, true);
    view.setUint16(26, nameBytes.length, true);
    local.set(nameBytes, 30);
    localOffsets.push(cursor);
    localParts.push(local, entry.data);
    cursor += local.length + entry.data.length;
  }
  const centralStart = cursor;
  const centralParts = [];
  entries.forEach((entry, i) => {
    const nameBytes = enc.encode(entry.name);
    const central = new Uint8Array(46 + nameBytes.length);
    const view = new DataView(central.buffer);
    view.setUint32(0, CENTRAL_SIG, true);
    view.setUint16(4, 20, true);
    view.setUint16(6, 20, true);
    view.setUint16(10, 0, true); // method = store
    view.setUint32(20, entry.data.length, true);
    view.setUint32(24, entry.data.length, true);
    view.setUint16(28, nameBytes.length, true);
    view.setUint32(42, localOffsets[i], true);
    central.set(nameBytes, 46);
    centralParts.push(central);
    cursor += central.length;
  });
  const centralSize = cursor - centralStart;
  const eocd = new Uint8Array(22);
  const eview = new DataView(eocd.buffer);
  eview.setUint32(0, EOCD_SIG, true);
  eview.setUint16(8, entries.length, true);
  eview.setUint16(10, entries.length, true);
  eview.setUint32(12, centralSize, true);
  eview.setUint32(16, centralStart, true);
  const all = [...localParts, ...centralParts, eocd];
  const total = all.reduce((sum, p) => sum + p.length, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of all) { out.set(part, at); at += part.length; }
  return out;
}

test("unpackZipFile: 读一个真实 File 的字节并成功解出条目", async () => {
  const zipBytes = makeStoreZip([{ name: "note.md", data: new TextEncoder().encode("# hi") }]);
  const file = new File([zipBytes], "bundle.zip");
  const result = await unpackZipFile(file, { uploadMaxBytes: 50 * 1024 * 1024 });
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["note.md"]);
  assert.equal(new TextDecoder().decode(result.files[0].bytes), "# hi");
});

test("unpackZipFile: 不是 zip 的文件报 not_a_zip，而不是抛异常", async () => {
  const file = new File([new TextEncoder().encode("just some text")], "notreally.zip");
  const result = await unpackZipFile(file, { uploadMaxBytes: 50 * 1024 * 1024 });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "not_a_zip");
});
