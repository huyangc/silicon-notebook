import assert from "node:assert/strict";
import test from "node:test";
import { deflateRawSync } from "node:zlib";

import {
  MD_BUNDLE_MAX_ENTRIES,
  MD_BUNDLE_MAX_SUGGESTIONS,
  MD_BUNDLE_TOTAL_BYTES_FACTOR,
  bytesToBase64,
  decodeMarkdownText,
  findMarkdownImages,
  inlineMdImages,
  markdownFiles,
  parseZipBundle,
  resolveBundleRef,
  sniffImageMime,
  utf8ByteLength,
} from "../../app/md-bundle.ts";


// ---------------------------------------------------------------------------
// zip 夹具：手工拼字节。method 0 直接放原文，method 8 用 node:zlib 生成真实的
// raw deflate 流，这样 DecompressionStream 那一支是真的被解出来的而不是被绕过。
// ---------------------------------------------------------------------------

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function bytesOf(value) {
  return typeof value === "string" ? new TextEncoder().encode(value) : value;
}

class ByteWriter {
  constructor() {
    this.parts = [];
    this.length = 0;
  }

  u16(value) {
    const b = new Uint8Array(2);
    new DataView(b.buffer).setUint16(0, value, true);
    return this.raw(b);
  }

  u32(value) {
    const b = new Uint8Array(4);
    new DataView(b.buffer).setUint32(0, value >>> 0, true);
    return this.raw(b);
  }

  raw(bytes) {
    this.parts.push(bytes);
    this.length += bytes.length;
    return this;
  }

  done() {
    const out = new Uint8Array(this.length);
    let at = 0;
    for (const part of this.parts) {
      out.set(part, at);
      at += part.length;
    }
    return out;
  }
}

/** entries: [{ name, data, method = 0, flags = 0, sentinelSize = false }] */
function makeZip(entries, options = {}) {
  const w = new ByteWriter();
  const prepared = [];
  for (const entry of entries) {
    const name = bytesOf(entry.name);
    const data = bytesOf(entry.data ?? "");
    const method = entry.method ?? 0;
    const blob = method === 8 ? new Uint8Array(deflateRawSync(Buffer.from(data))) : data;
    const offset = w.length;
    const declaredUncompressed = entry.sentinelSize ? 0xffffffff : data.length;
    w.u32(0x04034b50).u16(20).u16(entry.flags ?? 0).u16(method)
      .u16(0).u16(0)
      .u32(crc32(data)).u32(blob.length).u32(data.length)
      .u16(name.length).u16(0)
      .raw(name).raw(blob);
    prepared.push({ name, data, blob, method, offset, declaredUncompressed, entry });
  }
  const centralStart = w.length;
  for (const item of prepared) {
    w.u32(0x02014b50).u16(20).u16(20).u16(item.entry.flags ?? 0).u16(item.method)
      .u16(0).u16(0)
      .u32(crc32(item.data)).u32(item.blob.length).u32(item.declaredUncompressed)
      .u16(item.name.length).u16(0).u16(0)
      .u16(0).u16(0).u32(0)
      .u32(item.entry.sentinelOffset ? 0xffffffff : item.offset)
      .raw(item.name);
  }
  const centralSize = w.length - centralStart;
  if (options.zip64Locator) {
    w.u32(0x07064b50).u32(0).u32(0).u32(0).u32(1);
  }
  const comment = bytesOf(options.comment ?? "");
  w.u32(0x06054b50).u16(0).u16(0)
    .u16(options.declaredEntries ?? prepared.length)
    .u16(options.declaredEntries ?? prepared.length)
    .u32(centralSize).u32(centralStart)
    .u16(comment.length).raw(comment);
  return w.done();
}

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4]);
const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 9, 9, 9]);
const GIF87 = new Uint8Array([...bytesOf("GIF87a"), 7, 7]);
const GIF89 = new Uint8Array([...bytesOf("GIF89a"), 7, 7]);
const WEBP = new Uint8Array([...bytesOf("RIFF"), 0, 0, 0, 0, ...bytesOf("WEBP"), 1, 2]);
const SVG = bytesOf('<svg xmlns="http://www.w3.org/2000/svg"></svg>');

const CAPS = { uploadMaxBytes: 50 * 1024 * 1024 };


// ---------------------------------------------------------------------------
// 1. zip 解析
// ---------------------------------------------------------------------------

test("stored and deflated entries both round-trip through the central directory", async () => {
  const prose = "# 标题\n\n".repeat(200);
  const zip = makeZip([
    { name: "notes.md", data: prose, method: 8 },
    { name: "images/shot.png", data: PNG, method: 0 },
  ]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["notes.md", "images/shot.png"]);
  assert.equal(decodeMarkdownText(result.files[0].bytes), prose);
  assert.deepEqual([...result.files[1].bytes], [...PNG]);
  assert.equal(result.totalBytes, utf8ByteLength(prose) + PNG.length);
});


test("a zip comment does not hide the end-of-central-directory record", async () => {
  const zip = makeZip([{ name: "a.md", data: "hi" }], { comment: "x".repeat(400) });
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["a.md"]);
});


test("utf-8 entry names survive whether or not the name flag is set", async () => {
  const zip = makeZip([
    { name: "文档/图 一.png", data: PNG, flags: 0x0800 },
    { name: "文档/说明.md", data: "x" },
  ]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["文档/图 一.png", "文档/说明.md"]);
});


test("directory entries and macOS resource forks never enter the file set", async () => {
  const zip = makeZip([
    { name: "docs/", data: "" },
    { name: "__MACOSX/docs/._note.md", data: "junk" },
    { name: "docs/note.md", data: "real" },
  ]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["docs/note.md"]);
});


test("a nested zip entry stays an ordinary file instead of being recursed into", async () => {
  const inner = makeZip([{ name: "inner.md", data: "inner" }]);
  const zip = makeZip([{ name: "outer.md", data: "outer" }, { name: "bundle.zip", data: inner }]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["outer.md", "bundle.zip"]);
});


const ZIP_REJECTIONS = [
  ["not a zip at all", () => bytesOf("just some bytes, no EOCD anywhere"), "not_a_zip"],
  ["truncated below the EOCD size", () => new Uint8Array(4), "not_a_zip"],
  [
    "an encrypted entry",
    () => makeZip([{ name: "secret.md", data: "x", flags: 0x0001 }]),
    "encrypted",
  ],
  [
    "a strongly encrypted entry",
    () => makeZip([{ name: "secret.md", data: "x", flags: 0x0040 }]),
    "encrypted",
  ],
  [
    "a zip64 size sentinel",
    () => makeZip([{ name: "big.md", data: "x", sentinelSize: true }]),
    "zip64",
  ],
  [
    "a zip64 local-header-offset sentinel",
    () => makeZip([{ name: "big.md", data: "x", sentinelOffset: true }]),
    "zip64",
  ],
  [
    "a zip64 end-of-central-directory locator",
    () => makeZip([{ name: "a.md", data: "x" }], { zip64Locator: true }),
    "zip64",
  ],
  [
    "an unsupported compression method",
    () => makeZip([{ name: "a.md", data: "x", method: 12 }]),
    "unsupported_compression",
  ],
  [
    "an entry path escaping the bundle root",
    () => makeZip([{ name: "../outside.md", data: "x" }]),
    "unsafe_entry_path",
  ],
];

for (const [label, build, code] of ZIP_REJECTIONS) {
  test(`${label} is refused with a named reason`, async () => {
    const result = await parseZipBundle(build(), CAPS);
    assert.equal(result.ok, false);
    assert.equal(result.error.code, code);
  });
}


test("more entries than the cap is refused before anything is decompressed", async () => {
  const entries = Array.from(
    { length: MD_BUNDLE_MAX_ENTRIES + 1 },
    (_v, i) => ({ name: `f${i}.txt`, data: "x" }),
  );
  const result = await parseZipBundle(makeZip(entries), CAPS);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "too_many_entries");
  assert.equal(result.error.actual, MD_BUNDLE_MAX_ENTRIES + 1);
  assert.equal(result.error.limit, MD_BUNDLE_MAX_ENTRIES);
});


test("exactly at the entry cap the bundle still parses", async () => {
  const entries = Array.from(
    { length: MD_BUNDLE_MAX_ENTRIES },
    (_v, i) => ({ name: `f${i}.txt`, data: "x" }),
  );
  const result = await parseZipBundle(makeZip(entries), CAPS);
  assert.equal(result.ok, true);
  assert.equal(result.files.length, MD_BUNDLE_MAX_ENTRIES);
});


test("a stored entry past the decompressed-byte budget stops the parse", async () => {
  const caps = { uploadMaxBytes: 1000 };
  const budget = caps.uploadMaxBytes * MD_BUNDLE_TOTAL_BYTES_FACTOR;
  const zip = makeZip([{ name: "big.bin", data: "a".repeat(budget + 1) }]);
  const result = await parseZipBundle(zip, caps);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "too_large");
  assert.equal(result.error.limit, budget);
  assert.equal(result.error.path, "big.bin");
});


test("a deflate bomb is cut off mid-stream rather than fully inflated", async () => {
  const caps = { uploadMaxBytes: 1000 };
  const budget = caps.uploadMaxBytes * MD_BUNDLE_TOTAL_BYTES_FACTOR;
  // 高度可压缩:压缩后只有几十字节,解出来远超预算——只有边解边计预算才拦得住。
  const zip = makeZip([{ name: "bomb.bin", data: "a".repeat(budget * 50), method: 8 }]);
  const result = await parseZipBundle(zip, caps);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "too_large");
  assert.equal(result.error.path, "bomb.bin");
});


test("the budget is cumulative across entries, not per entry", async () => {
  const caps = { uploadMaxBytes: 100 };
  const budget = caps.uploadMaxBytes * MD_BUNDLE_TOTAL_BYTES_FACTOR;
  const half = "a".repeat(Math.floor(budget * 0.6));
  const result = await parseZipBundle(
    makeZip([{ name: "a.bin", data: half }, { name: "b.bin", data: half }]),
    caps,
  );
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "too_large");
  assert.equal(result.error.path, "b.bin");
});


// ---------------------------------------------------------------------------
// 2. md 发现
// ---------------------------------------------------------------------------

test("markdown discovery accepts both extensions and ignores case", () => {
  const files = [
    { path: "a.md", bytes: bytesOf("a") },
    { path: "nested/B.MARKDOWN", bytes: bytesOf("b") },
    { path: "c.mdx", bytes: bytesOf("c") },
    { path: "d.txt", bytes: bytesOf("d") },
    { path: "md", bytes: bytesOf("e") },
  ];
  assert.deepEqual(markdownFiles(files).map((f) => f.path), ["a.md", "nested/B.MARKDOWN"]);
});


test("a byte-order mark is stripped from decoded markdown", () => {
  assert.equal(decodeMarkdownText(bytesOf("﻿# title")), "# title");
});


// ---------------------------------------------------------------------------
// 3. 路径解析与配对
// ---------------------------------------------------------------------------

const PATH_CASES = [
  ["docs/note.md", "img.png", "docs/img.png"],
  ["docs/note.md", "./img.png", "docs/img.png"],
  ["docs/note.md", "../assets/img.png", "assets/img.png"],
  ["docs/a/b/note.md", "../../img.png", "docs/img.png"],
  ["note.md", "assets/sub/img.png", "assets/sub/img.png"],
  ["docs/note.md", "/root.png", "root.png"],
  ["docs/note.md", "sub\\win.png", "docs/sub/win.png"],
  ["docs/note.md", "./a/./b/../img.png", "docs/a/img.png"],
  ["note.md", "../escape.png", null],
];

test("relative image links resolve against the markdown file's own directory", () => {
  for (const [mdPath, ref, expected] of PATH_CASES) {
    assert.equal(resolveBundleRef(mdPath, ref), expected, `${mdPath} + ${ref}`);
  }
});


function inlineOf(mdPath, mdText, files, opts = {}) {
  return inlineMdImages(mdPath, mdText, files, { uploadMaxBytes: 5_000_000, ...opts });
}


test("percent-encoded links match the decoded file name", () => {
  const files = [{ path: "docs/my shot.png", bytes: PNG }];
  const out = inlineOf("docs/n.md", "![图注](my%20shot.png)\n", files);
  assert.equal(out.ok, true);
  assert.equal(out.receipt.inlined.length, 1);
  assert.equal(out.receipt.inlined[0].path, "docs/my shot.png");
});


test("a file whose name literally contains %20 still matches the raw link", () => {
  const files = [{ path: "docs/my%20shot.png", bytes: PNG }];
  const out = inlineOf("docs/n.md", "![图注](my%20shot.png)\n", files);
  assert.equal(out.ok, true);
  assert.equal(out.receipt.inlined[0].path, "docs/my%20shot.png");
});


test("a miss reports the resolved path plus case and basename candidates", () => {
  const files = [
    { path: "docs/Shot.PNG", bytes: PNG },
    { path: "elsewhere/shot.png", bytes: PNG },
  ];
  const out = inlineOf("docs/n.md", "![图注](shot.png)\n", files);
  assert.equal(out.ok, true);
  assert.deepEqual(out.receipt.inlined, []);
  assert.equal(out.receipt.missing.length, 1);
  assert.equal(out.receipt.missing[0].resolved, "docs/shot.png");
  assert.deepEqual(out.receipt.missing[0].suggestions, ["docs/Shot.PNG", "elsewhere/shot.png"]);
});


test("suggestions are capped instead of dumping the whole bundle", () => {
  const files = Array.from(
    { length: MD_BUNDLE_MAX_SUGGESTIONS + 4 },
    (_v, i) => ({ path: `d${i}/shot.png`, bytes: PNG }),
  );
  const out = inlineOf("n.md", "![图注](shot.png)\n", files);
  assert.equal(out.receipt.missing[0].suggestions.length, MD_BUNDLE_MAX_SUGGESTIONS);
});


test("a link escaping the bundle root is a miss with no resolved path", () => {
  const out = inlineOf("n.md", "![图注](../outside.png)\n", [{ path: "outside.png", bytes: PNG }]);
  assert.equal(out.receipt.missing.length, 1);
  assert.equal(out.receipt.missing[0].resolved, null);
  assert.deepEqual(out.receipt.missing[0].suggestions, []);
});


// ---------------------------------------------------------------------------
// 4. 独占段判定
// ---------------------------------------------------------------------------

const STANDALONE_CASES = [
  ["a paragraph of its own", "lead\n\n![a](x.png)\n\ntrail\n", true],
  ["the very first line", "![a](x.png)\n\ntrail\n", true],
  ["the last line with no trailing newline", "lead\n\n![a](x.png)", true],
  ["a link title after the destination", '\n![a](x.png "标题")\n', true],
  ["an angle-bracketed destination", "\n![a](<my file.png>)\n", true],
  ["balanced parens in the destination", "\n![a](img(1).png)\n", true],
  ["indented by up to three spaces", "\n   ![a](x.png)\n", true],
  ["mid-sentence", "\nsee ![a](x.png) here\n", false],
  ["a list item", "\n- ![a](x.png)\n", false],
  ["a numbered list item", "\n1. ![a](x.png)\n", false],
  ["a table cell", "\n| ![a](x.png) |\n", false],
  ["a blockquote", "\n> ![a](x.png)\n", false],
  ["two images on one line", "\n![a](x.png) ![b](y.png)\n", false],
  ["prose on the line directly above", "\nlead-in\n![a](x.png)\n\n", false],
  ["prose on the line directly below", "\n![a](x.png)\ntrailer\n", false],
  ["an indented code block", "\n    ![a](x.png)\n", false],
  ["wrapped in inline code", "\n`![a](x.png)`\n", false],
];

test("only a markdown image that owns its whole paragraph is rewritable", () => {
  for (const [label, doc, expected] of STANDALONE_CASES) {
    const refs = findMarkdownImages(doc);
    assert.ok(refs.length >= 1, label);
    assert.equal(refs[0].standalone, expected, label);
  }
});


test("image syntax inside a fenced code block is not an image reference at all", () => {
  const doc = "intro\n\n```md\n![a](x.png)\n```\n\n![b](y.png)\n";
  assert.deepEqual(findMarkdownImages(doc).map((r) => r.src), ["y.png"]);
});


test("a tilde fence hides image syntax the same way", () => {
  assert.deepEqual(findMarkdownImages("~~~\n![a](x.png)\n~~~\n"), []);
});


test("inline-position local images are reported instead of silently left behind", () => {
  const files = [{ path: "x.png", bytes: PNG }];
  const out = inlineOf("n.md", "see ![a](x.png) here\n", files);
  assert.equal(out.ok, true);
  assert.equal(out.rewritten, "see ![a](x.png) here\n");
  assert.deepEqual(
    out.receipt.unsupported.map((u) => [u.src, u.reason]),
    [["x.png", "inline_position"]],
  );
});


// ---------------------------------------------------------------------------
// 5. 魔数嗅探
// ---------------------------------------------------------------------------

const SNIFF_CASES = [
  ["png", PNG, "image/png"],
  ["jpeg", JPEG, "image/jpeg"],
  ["gif87a", GIF87, "image/gif"],
  ["gif89a", GIF89, "image/gif"],
  ["webp", WEBP, "image/webp"],
  ["svg", SVG, null],
  ["plain prose", bytesOf("not an image"), null],
  ["empty", new Uint8Array(0), null],
  ["riff that is not webp", new Uint8Array([...bytesOf("RIFF"), 0, 0, 0, 0, ...bytesOf("WAVE")]), null],
  ["a truncated png signature", PNG.slice(0, 4), null],
];

test("mime comes from the magic bytes", () => {
  for (const [label, bytes, expected] of SNIFF_CASES) {
    assert.equal(sniffImageMime(bytes), expected, label);
  }
});


test("the extension never overrides the magic bytes in either direction", () => {
  const files = [
    { path: "actually-jpeg.png", bytes: JPEG },
    { path: "actually-svg.png", bytes: SVG },
  ];
  const out = inlineOf("n.md", "![a](actually-jpeg.png)\n\n![b](actually-svg.png)\n", files);
  assert.equal(out.receipt.inlined.length, 1);
  assert.equal(out.receipt.inlined[0].mime, "image/jpeg");
  assert.deepEqual(
    out.receipt.unsupported.map((u) => [u.path, u.reason]),
    [["actually-svg.png", "unsupported_image_type"]],
  );
});


// ---------------------------------------------------------------------------
// 6. base64 内联
// ---------------------------------------------------------------------------

function decodeBase64(doc) {
  const binary = atob(doc);
  return Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
}

test("chunked base64 matches the platform encoder across chunk boundaries", () => {
  // 覆盖多个块以及三种余数长度:块长是 3 的倍数,只有拼接正确时才等于整体编码。
  for (const size of [0, 1, 2, 3, 24_575, 24_576, 24_577, 100_003]) {
    const bytes = new Uint8Array(size);
    for (let i = 0; i < size; i += 1) bytes[i] = (i * 37 + 11) & 0xff;
    assert.equal(bytesToBase64(bytes), Buffer.from(bytes).toString("base64"), `size ${size}`);
  }
});


test("a large image round-trips through the rewritten data URI", () => {
  const size = 100_000;
  const blob = new Uint8Array(size);
  blob.set(PNG.slice(0, 8));
  for (let i = 8; i < size; i += 1) blob[i] = (i * 131) & 0xff;
  const out = inlineOf("n.md", "![图注](big.png)\n", [{ path: "big.png", bytes: blob }]);
  assert.equal(out.ok, true);
  const match = /!\[图注\]\(data:image\/png;base64,([A-Za-z0-9+/=]+)\)/.exec(out.rewritten);
  assert.ok(match, "rewritten markdown keeps the standalone image shape");
  assert.deepEqual([...decodeBase64(match[1])], [...blob]);
  assert.equal(out.receipt.inlined[0].bytes, size);
  assert.equal(out.receipt.inlined[0].encodedBytes, match[1].length + "data:image/png;base64,".length);
});


test("only the destination is replaced — alt, title and surrounding bytes are untouched", () => {
  const files = [{ path: "x.png", bytes: PNG }];
  const doc = 'prologue\n\n   ![图 *注*](x.png "悬停标题")   \n\nepilogue\n';
  const out = inlineOf("n.md", doc, files);
  assert.equal(out.ok, true);
  const expected = doc.replace("x.png", `data:image/png;base64,${bytesToBase64(PNG)}`);
  assert.equal(out.rewritten, expected);
});


test("an angle-bracketed destination keeps its brackets while the target is swapped", () => {
  const out = inlineOf("n.md", "\n![a](<my file.png>)\n", [{ path: "my file.png", bytes: PNG }]);
  assert.equal(out.ok, true);
  assert.equal(out.rewritten, `\n![a](<data:image/png;base64,${bytesToBase64(PNG)}>)\n`);
});


// ---------------------------------------------------------------------------
// 7. 回执分类
// ---------------------------------------------------------------------------

test("remote links are reported as not fetched, data URIs stay silent", () => {
  const doc = [
    "![a](https://example.com/a.png)",
    "![b](http://example.com/b.png)",
    "![c](data:image/png;base64,AAAA)",
    "inline badge ![d](https://img.shields.io/x.svg) here",
  ].join("\n\n") + "\n";
  const out = inlineOf("n.md", doc, []);
  assert.equal(out.ok, true);
  assert.equal(out.rewritten, doc);
  assert.deepEqual(
    out.receipt.remote.map((r) => r.src),
    [
      "https://example.com/a.png",
      "http://example.com/b.png",
      "https://img.shields.io/x.svg",
    ],
  );
  assert.deepEqual(out.receipt.unsupported, []);
});


test("an image with no caption is inlined but flagged as unsearchable", () => {
  const files = [{ path: "a.png", bytes: PNG }, { path: "b.png", bytes: PNG }];
  const out = inlineOf("n.md", "![](a.png)\n\n![有图注](b.png)\n", files);
  assert.equal(out.ok, true);
  assert.equal(out.receipt.inlined.length, 2);
  assert.deepEqual(out.receipt.noAlt.map((n) => n.path), ["a.png"]);
});


test("whitespace-only alt counts as no caption", () => {
  const out = inlineOf("n.md", "![   ](a.png)\n", [{ path: "a.png", bytes: PNG }]);
  assert.deepEqual(out.receipt.noAlt.map((n) => n.path), ["a.png"]);
});


test("an empty destination is reported rather than treated as a miss", () => {
  const out = inlineOf("n.md", "![图注]()\n", []);
  assert.deepEqual(
    out.receipt.unsupported.map((u) => u.reason),
    ["empty_src"],
  );
  assert.deepEqual(out.receipt.missing, []);
});


test("an image over the deployment's per-image cap is left as a link and reported", () => {
  const big = new Uint8Array(500);
  big.set(PNG.slice(0, 8));
  const files = [{ path: "big.png", bytes: big }, { path: "small.png", bytes: PNG }];
  const out = inlineOf("n.md", "![a](big.png)\n\n![b](small.png)\n", files, {
    imageMaxBytes: 100,
  });
  assert.equal(out.ok, true);
  assert.ok(out.rewritten.includes("![a](big.png)"), "the oversized link is left untouched");
  assert.deepEqual(
    out.receipt.unsupported.map((u) => [u.path, u.reason, u.bytes, u.limit]),
    [["big.png", "image_too_large", 500, 100]],
  );
  assert.deepEqual(out.receipt.inlined.map((i) => i.path), ["small.png"]);
});


test("the per-source image count is spent only by images that were actually inlined", () => {
  const big = new Uint8Array(500);
  big.set(PNG.slice(0, 8));
  const files = [
    { path: "a.png", bytes: PNG },
    { path: "big.png", bytes: big },
    { path: "b.png", bytes: PNG },
    { path: "c.png", bytes: PNG },
  ];
  const doc = "![1](a.png)\n\n![2](big.png)\n\n![3](b.png)\n\n![4](c.png)\n";
  const out = inlineOf("n.md", doc, files, { imageMaxBytes: 100, maxImagesPerSource: 2 });
  assert.equal(out.ok, true);
  // 超限的 big.png 没有占掉名额,所以 b.png 仍然内联;第三张 c.png 才被张数上限挡下。
  assert.deepEqual(out.receipt.inlined.map((i) => i.path), ["a.png", "b.png"]);
  assert.deepEqual(
    out.receipt.unsupported.map((u) => [u.path, u.reason]),
    [["big.png", "image_too_large"], ["c.png", "too_many_images"]],
  );
});


test("omitting the optional caps disables the client-side pre-checks", () => {
  const files = [{ path: "a.png", bytes: PNG }, { path: "b.png", bytes: PNG }];
  const out = inlineOf("n.md", "![1](a.png)\n\n![2](b.png)\n", files, {
    imageMaxBytes: null,
    maxImagesPerSource: undefined,
  });
  assert.equal(out.receipt.inlined.length, 2);
  assert.deepEqual(out.receipt.unsupported, []);
});


// ---------------------------------------------------------------------------
// 8. 体积上限
// ---------------------------------------------------------------------------

test("a rewritten file over the upload cap fails with a per-image breakdown", () => {
  const bigger = new Uint8Array(900);
  bigger.set(PNG.slice(0, 8));
  const smaller = new Uint8Array(300);
  smaller.set(PNG.slice(0, 8));
  const files = [{ path: "big.png", bytes: bigger }, { path: "small.png", bytes: smaller }];
  const doc = "![a](big.png)\n\n![b](small.png)\n";
  const out = inlineOf("n.md", doc, files, { uploadMaxBytes: 1000 });
  assert.equal(out.ok, false);
  assert.equal(out.error.code, "too_large");
  assert.equal(out.error.limit, 1000);
  assert.ok(out.error.bytes > 1000);
  assert.equal(out.error.textBytes, utf8ByteLength(doc));
  assert.deepEqual(out.error.images.map((i) => i.path), ["big.png", "small.png"]);
  assert.ok(out.error.images[0].encodedBytes > out.error.images[1].encodedBytes);
  // 回执仍要给得出来:失败页也得说清配对结果。
  assert.equal(out.receipt.inlined.length, 2);
});


test("a rewritten file inside the cap reports its exact byte size", () => {
  const out = inlineOf("n.md", "![a](x.png)\n", [{ path: "x.png", bytes: PNG }], {
    uploadMaxBytes: 5000,
  });
  assert.equal(out.ok, true);
  assert.equal(out.bytes, utf8ByteLength(out.rewritten));
});


test("utf-8 byte length counts multi-byte prose and astral pairs like the encoder", () => {
  for (const doc of ["", "abc", "中文标题", "é", "\u{1F600}\u{20000}", "a中\u{1F600}b"]) {
    assert.equal(utf8ByteLength(doc), new TextEncoder().encode(doc).length, JSON.stringify(doc));
  }
});


// ---------------------------------------------------------------------------
// 9. 多个 md
// ---------------------------------------------------------------------------

test("each markdown file pairs against its own directory", async () => {
  const zip = makeZip([
    { name: "a/note.md", data: "![一](pic.png)\n", method: 8 },
    { name: "a/pic.png", data: PNG },
    { name: "b/note.md", data: "![二](pic.png)\n", method: 8 },
    { name: "b/pic.png", data: JPEG },
    { name: "c/note.md", data: "![三](pic.png)\n", method: 8 },
  ]);
  const parsed = await parseZipBundle(zip, CAPS);
  assert.equal(parsed.ok, true);
  const mds = markdownFiles(parsed.files);
  assert.deepEqual(mds.map((m) => m.path), ["a/note.md", "b/note.md", "c/note.md"]);

  const outcomes = mds.map((md) =>
    inlineOf(md.path, decodeMarkdownText(md.bytes), parsed.files));
  assert.deepEqual(outcomes.map((o) => o.receipt.inlined.map((i) => i.path)), [
    ["a/pic.png"],
    ["b/pic.png"],
    [],
  ]);
  assert.deepEqual(outcomes.map((o) => o.receipt.inlined.map((i) => i.mime)), [
    ["image/png"],
    ["image/jpeg"],
    [],
  ]);
  // 第三份没有自己的图,但同名文件在别的目录下——按近似候选如实报出。
  assert.deepEqual(outcomes[2].receipt.missing[0].suggestions, ["a/pic.png", "b/pic.png"]);
});


test("the same image referenced twice is inlined at both sites", () => {
  const out = inlineOf("n.md", "![一](x.png)\n\n![二](x.png)\n", [{ path: "x.png", bytes: PNG }]);
  assert.equal(out.ok, true);
  assert.equal(out.receipt.inlined.length, 2);
  const encoded = bytesToBase64(PNG);
  assert.equal(out.rewritten.split(encoded).length - 1, 2);
});


test("a markdown file with no images passes through byte for byte", () => {
  const doc = "# 标题\n\n正文 with `![not](an.png)` inline code.\n";
  const out = inlineOf("n.md", doc, [{ path: "an.png", bytes: PNG }]);
  assert.equal(out.ok, true);
  assert.equal(out.rewritten, doc);
  assert.deepEqual(out.receipt.inlined, []);
});
