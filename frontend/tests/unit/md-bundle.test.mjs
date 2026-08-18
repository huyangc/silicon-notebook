import assert from "node:assert/strict";
import test from "node:test";
import { deflateRawSync } from "node:zlib";

import {
  MD_BUNDLE_MAX_DECLARED_ENTRIES,
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

/** entries: [{
 *    name, data, method = 0, flags = 0, sentinelSize = false, sentinelOffset = false,
 *    blob,                 // 覆盖压缩载荷（构造损坏/截断的 deflate 流用）
 *    compressedSizeDelta,    // 声明的 compressed size 相对真实载荷的偏移（负数＝声明得更短）
 *    uncompressedSizeDelta,  // 声明的 uncompressed size 相对 data.length 的偏移，独立于
 *                            // 实际内容/CRC——用来单独钉住「长度检查」而不牵连 CRC 检查
 *                            // （两者正常情况下总是同时触发，需要这个旋钮才能拆开验证）
 *    localExtra, centralExtra,  // local / central 两侧**各自独立**的 extra 字段
 *  }]
 */
function makeZip(entries, options = {}) {
  const w = new ByteWriter();
  const prepared = [];
  for (const entry of entries) {
    const name = bytesOf(entry.name);
    const data = bytesOf(entry.data ?? "");
    const method = entry.method ?? 0;
    const blob = entry.blob
      ?? (method === 8 ? new Uint8Array(deflateRawSync(Buffer.from(data))) : data);
    const declaredCompressed = blob.length + (entry.compressedSizeDelta ?? 0);
    const localExtra = bytesOf(entry.localExtra ?? new Uint8Array(0));
    const centralExtra = bytesOf(entry.centralExtra ?? new Uint8Array(0));
    const offset = w.length;
    const declaredUncompressed = entry.sentinelSize
      ? 0xffffffff
      : data.length + (entry.uncompressedSizeDelta ?? 0);
    w.u32(0x04034b50).u16(20).u16(entry.flags ?? 0).u16(method)
      .u16(0).u16(0)
      .u32(crc32(data)).u32(declaredCompressed).u32(data.length)
      .u16(name.length).u16(localExtra.length)
      .raw(name).raw(localExtra).raw(blob);
    prepared.push({
      name, data, method, offset, declaredCompressed, declaredUncompressed, centralExtra, entry,
    });
  }
  const centralStart = w.length;
  for (const item of prepared) {
    w.u32(0x02014b50).u16(20).u16(20).u16(item.entry.flags ?? 0).u16(item.method)
      .u16(0).u16(0)
      .u32(crc32(item.data)).u32(item.declaredCompressed).u32(item.declaredUncompressed)
      .u16(item.name.length).u16(item.centralExtra.length).u16(0)
      .u16(0).u16(0).u32(0)
      .u32(item.entry.sentinelOffset ? 0xffffffff : item.offset)
      .raw(item.name).raw(item.centralExtra);
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


test("a fake EOCD signature planted inside the comment does not win the backward scan", async () => {
  // 尾部回扫是从后往前的，所以注释里的假签名会**先于**真 EOCD 被看到。挡住它的只有
  // 「注释长度必须正好补齐到文件末尾」这一条。少了那条判据：假签名处读出的
  // entries/centralSize/centralOffset 全是零 → 解析「成功」但一个文件都没有,
  // 静默返回空包,没有任何错误码。所以这里必须断言文件真的在。
  const comment = new Uint8Array(40);
  comment.set(bytesOf("PK\x05\x06"), 0);
  const zip = makeZip([{ name: "a.md", data: "hi" }], { comment });
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["a.md"]);
});


test("the data offset follows the local header's own extra length, not the central one", async () => {
  // local 与 central 的 extra 字段长度经常不同（时间戳扩展在两侧写法不一样）。
  // 拿 central 的长度去算数据起点会整体错位 5 字节,而错位后的字节仍是「一段数据」
  // ——不断言内容就完全看不出来,所以这里逐字节比对 PNG。
  const zip = makeZip([{
    name: "images/shot.png",
    data: PNG,
    localExtra: bytesOf("UT\x05\x00\x03aaaa"),
    centralExtra: bytesOf("UT\x01\x00"),
  }]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual([...result.files[0].bytes], [...PNG]);
});


test("both extra fields also line up when the deflated branch is taken", async () => {
  const prose = "# 标题\n\n".repeat(50);
  const zip = makeZip([{
    name: "notes.md",
    data: prose,
    method: 8,
    localExtra: bytesOf("UT\x09\x00\x03abcdefghi"),
    centralExtra: bytesOf("UT\x00\x00"),
  }]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.equal(decodeMarkdownText(result.files[0].bytes), prose);
});


test("a duplicate entry path keeps the first copy and is not counted twice", async () => {
  const zip = makeZip([
    { name: "a.png", data: PNG },
    { name: "a.png", data: JPEG },
  ]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["a.png"]);
  assert.deepEqual([...result.files[0].bytes], [...PNG], "first writer wins");
  // totalBytes 必须与 files 的字节和一致——重复条目既没进文件集,就不能进总量。
  assert.equal(result.totalBytes, PNG.length);
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


/** 非法 deflate：首字节的 BTYPE 位是保留值 11，解压器立刻拒绝。 */
const GARBAGE_DEFLATE = new Uint8Array([0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff]);


test("a deflate payload that is not a valid stream is reported as corrupt, not as too large", async () => {
  // 尺寸自洽（声明的 compressed size 就是这堆垃圾的真实长度），所以除了真去解一次
  // 之外没有别的办法发现它坏了。不接住解压器抛的错就是一条 unhandled rejection：
  // 调用方拿不到任何错误码,浏览器里还会把整页的错误处理拖下水。
  const zip = makeZip([{ name: "broken.md", data: "whatever", method: 8, blob: GARBAGE_DEFLATE }]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "corrupt");
  assert.equal(result.error.path, "broken.md");
});


test("a compressed size shorter than the real stream is reported as corrupt", async () => {
  // central/local 声明的长度比真实 deflate 流短 → 我们只喂进去一个被砍头的流。
  // 它同样只有在真解一次的时候才暴露。
  const zip = makeZip([{
    name: "short.md",
    data: "a".repeat(4000),
    method: 8,
    compressedSizeDelta: -3,
  }]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "corrupt");
  assert.equal(result.error.path, "short.md");
});


test("a bit-flipped stored entry fails the CRC-32 check even though sizes line up", async () => {
  // 位翻转不改变长度,只改变字节内容——单靠 uncompressedSize 比对完全看不出来,
  // 必须真的比 CRC。central directory 记的 CRC 来自「本该是」的原文,磁盘上的
  // 字节被翻了一位,两者就对不上。
  const original = bytesOf("A".repeat(64));
  const flipped = new Uint8Array(original);
  flipped[10] ^= 0xff;
  const zip = makeZip([{ name: "shot.png", data: original, blob: flipped }]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "corrupt");
  assert.equal(result.error.path, "shot.png");
});


test("a declared uncompressed size that disagrees with the real (CRC-valid) content is reported as corrupt", async () => {
  // 内容与 CRC 都合法(content 就是 data,crc32(data) 逐字写进两处 header)——只有
  // 声明的 uncompressed size 被单独改错了 1 字节。CRC 检查在这份夹具上必然通过,
  // 唯一能拦下它的只有长度比对,借此把两条检查彼此独立地钉住。
  const zip = makeZip([{ name: "off-by-one.md", data: "x".repeat(40), uncompressedSizeDelta: -1 }]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "corrupt");
  assert.equal(result.error.path, "off-by-one.md");
});


test("a deflated entry whose decoded length disagrees with the declared size is reported as corrupt", async () => {
  // central/local 两处的 uncompressedSize 与 CRC 都是照「声明的」原文算出来的,
  // 而磁盘上真正的 deflate 流解出来是另一段更短的内容——长度一比对就露馅,
  // 不需要走到 CRC 那一步。
  const declared = bytesOf("a".repeat(100));
  const actualBlob = new Uint8Array(deflateRawSync(Buffer.from("a".repeat(50))));
  const zip = makeZip([{ name: "notes.md", data: declared, method: 8, blob: actualBlob }]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "corrupt");
  assert.equal(result.error.path, "notes.md");
});


test("an intact bundle with matching CRCs parses normally (no false positives)", async () => {
  const zip = makeZip([
    { name: "notes.md", data: "# 标题\n\n正文。".repeat(30), method: 8 },
    { name: "images/shot.png", data: PNG, method: 0 },
  ]);
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, true);
  assert.deepEqual(result.files.map((f) => f.path), ["notes.md", "images/shot.png"]);
});


test("more entries than the cap is refused before anything is decompressed", async () => {
  // 第一个条目的压缩流是坏的：只要解压发生在条目闸之前,结局就会是 `corrupt` 而不是
  // `too_many_entries`。这是「先拦再解」的字节级证明——只数条目数证明不了顺序。
  const entries = Array.from(
    { length: MD_BUNDLE_MAX_ENTRIES + 1 },
    (_v, i) => (i === 0
      ? { name: "f0.txt", data: "x", method: 8, blob: GARBAGE_DEFLATE }
      : { name: `f${i}.txt`, data: "x" }),
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


test("macOS resource forks do not consume the entry cap", async () => {
  // 「压缩」给每个文件配一条 `__MACOSX/._x` 伴随条目,声明数因此正好翻倍。按声明数
  // 预拦会让一个刚好合规的包被判成超限,而用户在 Finder 里看到的文件数只有一半。
  const entries = [];
  for (let i = 0; i < MD_BUNDLE_MAX_ENTRIES; i += 1) {
    entries.push({ name: `f${i}.txt`, data: "x" });
    entries.push({ name: `__MACOSX/._f${i}.txt`, data: "junk" });
  }
  const result = await parseZipBundle(makeZip(entries), CAPS);
  assert.equal(result.ok, true);
  assert.equal(result.files.length, MD_BUNDLE_MAX_ENTRIES);
});


test("real files over the cap are still refused even when resource forks pad the count", async () => {
  const entries = [];
  for (let i = 0; i < MD_BUNDLE_MAX_ENTRIES + 1; i += 1) {
    entries.push({ name: `f${i}.txt`, data: "x" });
    if (i % 2 === 0) entries.push({ name: `__MACOSX/._f${i}.txt`, data: "junk" });
  }
  const result = await parseZipBundle(makeZip(entries), CAPS);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "too_many_entries");
  // 报出去的是**保留**条目数,不是被资源叉撑大的声明数。
  assert.equal(result.error.actual, MD_BUNDLE_MAX_ENTRIES + 1);
  assert.equal(result.error.limit, MD_BUNDLE_MAX_ENTRIES);
});


test("an absurd declared entry count is refused without scanning the directory", async () => {
  // 保留条目数要扫完才知道,所以扫描本身需要一个先验上界,否则一个声明了六万条的
  // 畸形包会先让我们白扫一遍。这不是产品上限,只是扫描的封顶。
  const zip = makeZip(
    [{ name: "a.md", data: "x" }],
    { declaredEntries: MD_BUNDLE_MAX_DECLARED_ENTRIES + 1 },
  );
  const result = await parseZipBundle(zip, CAPS);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "too_many_entries");
  assert.equal(result.error.limit, MD_BUNDLE_MAX_DECLARED_ENTRIES);
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
  // 高度可压缩:压缩后只有两百来字节,解出来远超预算。**尾部被截掉 3 字节**,所以这个
  // 流最终是解不开的——这就是「中途切断」的字节级判据:
  //   边解边计预算 → 第一个 16KiB 分片就超预算 → cancel,坏尾根本没读到 → too_large
  //   先整个解完再看总量 → 读到坏尾先抛 → corrupt
  // 两种结局都不会报错退出测试,只有断言分得开它们。
  const real = new Uint8Array(deflateRawSync(Buffer.from("a".repeat(budget * 50))));
  const zip = makeZip([{
    name: "bomb.bin",
    data: "",
    method: 8,
    blob: real.subarray(0, real.length - 3),
  }]);
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


test("an unusable upload cap disables the byte budget instead of rejecting everything", async () => {
  // `0` 曾在这里算成「预算 0」→ 任何包都拒；而在 inlineMdImages 那边算成「不检查」。
  // 同一个缺省值一边全拒一边全放不是保守而是矛盾:全拒那侧没有任何合法操作能绕开。
  const zip = makeZip([
    { name: "notes.md", data: "# 标题\n", method: 8 },
    { name: "shot.png", data: PNG },
  ]);
  for (const uploadMaxBytes of [0, -1, Number.NaN, 1.5]) {
    const result = await parseZipBundle(zip, { uploadMaxBytes });
    assert.equal(result.ok, true, `uploadMaxBytes=${uploadMaxBytes}`);
    assert.deepEqual(result.files.map((f) => f.path), ["notes.md", "shot.png"]);
  }
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


test("a byte-order mark never survives into the decoded markdown", () => {
  // 显式拼 EF BB BF,不靠源文件里一个看不见的字符。TextDecoder 默认就吞 BOM,所以
  // 这里钉的是「结果里没有 U+FEFF」——真源换成 `{ ignoreBOM: true }`（名字骗人,实际
  // 是「原样留着」）会让首个标题带上一个不可见字符,而且不会报任何错。
  const withBom = new Uint8Array([0xef, 0xbb, 0xbf, ...bytesOf("# title")]);
  assert.equal(decodeMarkdownText(withBom), "# title");
  assert.equal(decodeMarkdownText(withBom).charCodeAt(0), "#".charCodeAt(0));
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
  // 缓存戳与锚点是 URL 语法,不是文件名的一部分——不剥就永远配不上磁盘上那个文件。
  ["docs/note.md", "img.png?v=2", "docs/img.png"],
  ["docs/note.md", "img.png#fig-1", "docs/img.png"],
  ["docs/note.md", "img.png?v=2#fig-1", "docs/img.png"],
  ["docs/note.md", "img.png#a?b", "docs/img.png"],
  ["docs/note.md", "../assets/img.png?w=100", "assets/img.png"],
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


test("when both spellings exist in the bundle the decoded one wins", () => {
  // 两个候选同时命中时的优先级必须是确定的:`%20` 在链接里的**规范**含义就是空格,
  // 所以按规范解读取 `my shot.png`。反过来（原样优先）会让 Notion 这类导出——目录里
  // 恰好同时有两个名字——配到那个几乎不可能是作者本意的文件上。
  const files = [
    { path: "docs/my%20shot.png", bytes: JPEG },
    { path: "docs/my shot.png", bytes: PNG },
  ];
  const out = inlineOf("docs/n.md", "![图注](my%20shot.png)\n", files);
  assert.equal(out.ok, true);
  assert.equal(out.receipt.inlined[0].path, "docs/my shot.png");
  assert.equal(out.receipt.inlined[0].mime, "image/png");
});


test("a link with a cache-busting query still pairs with the plain file", () => {
  const files = [{ path: "docs/shot.png", bytes: PNG }];
  const out = inlineOf("docs/n.md", "![图注](shot.png?v=2)\n", files);
  assert.equal(out.ok, true);
  assert.equal(out.receipt.inlined[0].path, "docs/shot.png");
  // 目标整体被替换:查询串跟着原链接一起消失,不能残留成 `data:...;base64,AAA?v=2`。
  assert.equal(out.rewritten, `![图注](data:image/png;base64,${bytesToBase64(PNG)})\n`);
});


test("a protocol-relative link is remote, not a missing bundle file", () => {
  // 它没有 scheme,逃得过「绝对 URL」判据,却绝不是包内相对路径。当成相对路径去配对
  // 会报一条「未找到 cdn.example.com/a.png」——把产品决定说成用户的包少了个文件。
  const out = inlineOf("n.md", "![a](//cdn.example.com/a.png)\n", []);
  assert.equal(out.ok, true);
  assert.deepEqual(out.receipt.remote.map((r) => r.src), ["//cdn.example.com/a.png"]);
  assert.deepEqual(out.receipt.missing, []);
  assert.deepEqual(out.receipt.unsupported, []);
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

// 每个 `true` 都对着服务端 `structural_markdown.parse_blocks` 实测过（该文档确实产出
// 一个 `image` 块）；每个 `false` 要么服务端不产出 image 块，要么本模块刻意保守。
const STANDALONE_CASES = [
  ["a paragraph of its own", "lead\n\n![a](x.png)\n\ntrail\n", true],
  ["the very first line", "![a](x.png)\n\ntrail\n", true],
  ["the last line with no trailing newline", "lead\n\n![a](x.png)", true],
  ["a link title after the destination", '\n![a](x.png "标题")\n', true],
  ["an angle-bracketed destination", "\n![a](<my file.png>)\n", true],
  ["balanced parens in the destination", "\n![a](img(1).png)\n", true],
  // 相邻行是块边界（不只是空行）：标题下面直接贴图是导出工具的常见写法，此前整片
  // 判成不可改写。
  ["an ATX heading on the line directly above", "# 标题\n![a](x.png)\n\ntrail\n", true],
  ["an ATX heading on the line directly below", "lead\n\n![a](x.png)\n## 下节\n", true],
  ["headings on both sides", "# 上\n![a](x.png)\n## 下\n", true],
  ["a thematic break directly above", "lead\n\n***\n![a](x.png)\n\ntrail\n", true],
  ["a --- thematic break directly above", "---\n![a](x.png)\n\ntrail\n", true],
  ["an underscore thematic break directly below", "lead\n\n![a](x.png)\n___\n", true],
  ["a spaced-out dash break directly below", "lead\n\n![a](x.png)\n- - -\n", true],
  // ⚠ `---`/`----`/`===` 跟在**下面**是 setext 下划线而不是分隔线：服务端把图片行
  // 整个吸成一个标题（实测 blocks=[paragraph, heading]，零个 image 块）。上下不对称。
  ["a --- setext underline directly below", "lead\n\n![a](x.png)\n---\n", false],
  ["a ==== setext underline directly below", "lead\n\n![a](x.png)\n===\n", false],
  ["a four-dash setext underline directly below", "lead\n\n![a](x.png)\n----\n", false],
  // 缩进 1–3 空格是**松散列表项的续段**最常见的形态,服务端会把它并进 list_item、
  // 只留 alt 文本。内联进去 = 把整张图的 base64 写进一个注定只保留文字的位置。
  ["a loose ordered list item's continuation", "1. step\n\n   ![a](x.png)\n\n2. b\n", false],
  ["a loose bullet list item's continuation", "- step\n\n  ![a](x.png)\n\n- b\n", false],
  ["indented by three spaces", "\n   ![a](x.png)\n", false],
  ["indented by one space", "lead\n\n ![a](x.png)\n\ntrail\n", false],
  ["mid-sentence", "\nsee ![a](x.png) here\n", false],
  ["a list item", "\n- ![a](x.png)\n", false],
  ["a numbered list item", "\n1. ![a](x.png)\n", false],
  ["a table cell", "\n| ![a](x.png) |\n", false],
  ["a blockquote", "\n> ![a](x.png)\n", false],
  ["two images on one line", "\n![a](x.png) ![b](y.png)\n", false],
  ["prose on the line directly above", "\nlead-in\n![a](x.png)\n\n", false],
  ["prose on the line directly below", "\n![a](x.png)\ntrailer\n", false],
  ["a bare # that is not a heading below", "lead\n\n![a](x.png)\n#下节\n", false],
  ["an indented code block", "\n    ![a](x.png)\n", false],
];

test("only a markdown image that owns its whole paragraph is rewritable", () => {
  for (const [label, doc, expected] of STANDALONE_CASES) {
    const refs = findMarkdownImages(doc);
    assert.ok(refs.length >= 1, label);
    assert.equal(refs[0].standalone, expected, label);
  }
});


test("image syntax inside an inline code span is not an image reference at all", () => {
  // 被展示的字面文本,不是引用——既不改写,也不该在回执里编出一条「这张图没内联」。
  assert.deepEqual(findMarkdownImages("\n`![a](x.png)`\n"), []);
  assert.deepEqual(findMarkdownImages("see ``![a](x.png)`` here\n"), []);
  // 同一行上代码跨度之外的图片仍然要认出来。
  assert.deepEqual(
    findMarkdownImages("`![a](x.png)` and ![b](y.png)\n").map((r) => r.src),
    ["y.png"],
  );
  // 落单的反引号不成跨度,后面的图片照常认。
  assert.deepEqual(findMarkdownImages("a ` b ![c](z.png)\n").map((r) => r.src), ["z.png"]);
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
  // 行尾留白仍要保留（行首缩进现在会让它不可改写，见 STANDALONE_CASES）。
  const doc = 'prologue\n\n![图 *注*](x.png "悬停标题")   \n\nepilogue\n';
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


test("reference-style images are reported instead of silently dropped", () => {
  // 目标写在文末的链接定义里,本模块不解析定义表。此前它们连回执都不进——用户看到
  // 「0 张未找到」就以为都配上了,上传后才发现图全没了。
  const files = [{ path: "x.png", bytes: PNG }];
  const doc = [
    "![完整引用式][shot]",
    "![折叠引用式][]",
    "[shot]: x.png",
  ].join("\n\n") + "\n";
  const out = inlineOf("n.md", doc, files);
  assert.equal(out.ok, true);
  assert.equal(out.rewritten, doc, "正文逐字不变");
  assert.deepEqual(
    out.receipt.unsupported.map((u) => [u.src, u.reason]),
    [["shot", "reference_syntax"], ["", "reference_syntax"]],
  );
  assert.deepEqual(out.receipt.inlined, []);
  assert.deepEqual(out.receipt.missing, []);
});


test("html img tags are reported instead of silently dropped", () => {
  const files = [{ path: "x.png", bytes: PNG }];
  const doc = [
    '<img src="x.png" alt="双引号">',
    "<img src='x.png'>",
    "<img src=x.png width=200>",
    "<IMG SRC=\"x.png\">",
    "<img alt='没有 src'>",
  ].join("\n\n") + "\n";
  const out = inlineOf("n.md", doc, files);
  assert.equal(out.ok, true);
  assert.equal(out.rewritten, doc, "正文逐字不变");
  assert.deepEqual(
    out.receipt.unsupported.map((u) => [u.src, u.reason]),
    [
      ["x.png", "html_syntax"],
      ["x.png", "html_syntax"],
      ["x.png", "html_syntax"],
      ["x.png", "html_syntax"],
      ["", "html_syntax"],
    ],
  );
  assert.deepEqual(out.receipt.inlined, []);
});


test("an html img tag reports its own syntax rather than being called remote", () => {
  // 「这种写法不支持」可操作（改成 markdown 语法就能用）；「远程图片不拉取」暗示
  // 改不了。语法判据因此排在 scheme 判据之前。
  const out = inlineOf("n.md", '<img src="https://example.com/a.png">\n', []);
  assert.deepEqual(
    out.receipt.unsupported.map((u) => u.reason),
    ["html_syntax"],
  );
  assert.deepEqual(out.receipt.remote, []);
});


test("unsupported image syntax inside code is still not reported", () => {
  const doc = "```html\n<img src=\"x.png\">\n```\n\n行内 `![a][ref]` 与 `<img src=y.png>`。\n";
  const out = inlineOf("n.md", doc, [{ path: "x.png", bytes: PNG }]);
  assert.equal(out.rewritten, doc);
  assert.deepEqual(out.receipt.unsupported, []);
});


test("the three syntaxes keep document order in the receipt", () => {
  const files = [{ path: "x.png", bytes: PNG }];
  const doc = "![a](x.png)\n\n![b][ref]\n\n<img src=\"c.png\">\n\n![d](x.png)\n";
  const out = inlineOf("n.md", doc, files);
  assert.deepEqual(
    out.receipt.unsupported.map((u) => u.reason),
    ["reference_syntax", "html_syntax"],
  );
  assert.deepEqual(out.receipt.inlined.map((i) => i.line), [0, 6]);
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


test("unusable cap values disable the pre-checks the same way omitting them does", () => {
  // 与 parseZipBundle 同一个方向：`0` 不是「上限为零」而是「没有可用上限」。
  const files = [{ path: "a.png", bytes: PNG }, { path: "b.png", bytes: PNG }];
  for (const bad of [0, -1, Number.NaN, 2.5]) {
    const out = inlineMdImages("n.md", "![1](a.png)\n\n![2](b.png)\n", files, {
      uploadMaxBytes: bad,
      imageMaxBytes: bad,
      maxImagesPerSource: bad,
    });
    assert.equal(out.ok, true, `cap=${bad}`);
    assert.equal(out.receipt.inlined.length, 2, `cap=${bad}`);
    assert.deepEqual(out.receipt.unsupported, [], `cap=${bad}`);
  }
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


test("the same image reached through different spellings encodes once per bundle path", () => {
  // memo 的键是包内路径而不是 src——`./x.png`、`x.png`、`x.png?v=1` 是同一个文件。
  // 三处都必须内联,且都必须是同一段 base64。
  const doc = "![一](x.png)\n\n![二](./x.png)\n\n![三](x.png?v=1)\n";
  const out = inlineOf("n.md", doc, [{ path: "x.png", bytes: PNG }]);
  assert.equal(out.ok, true);
  assert.deepEqual(out.receipt.inlined.map((i) => i.path), ["x.png", "x.png", "x.png"]);
  assert.equal(out.rewritten.split(bytesToBase64(PNG)).length - 1, 3);
});


test("a markdown file with no images passes through byte for byte", () => {
  const doc = "# 标题\n\n正文 with `![not](an.png)` inline code.\n";
  const out = inlineOf("n.md", doc, [{ path: "an.png", bytes: PNG }]);
  assert.equal(out.ok, true);
  assert.equal(out.rewritten, doc);
  assert.deepEqual(out.receipt.inlined, []);
  // 行内代码里的图片语法连回执都不该有：它是被展示的文本，不是一张没配上的图。
  assert.deepEqual(out.receipt.unsupported, []);
});
