import assert from "node:assert/strict";
import test from "node:test";

import {
  ALREADY_STAGED_REASON,
  BUNDLE_DIR_MAX_FILES,
  BUNDLE_IMAGES_DISABLED_NOTE,
  BUNDLE_READ_FAILED_REASON,
  BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH,
  BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES,
  DIRECTORY_READ_FAILED_REASON,
  INLINE_TOO_LARGE_IMAGE_LINES,
  NO_MARKDOWN_IN_BUNDLE_REASON,
  PAIRING_SKIPPED_SUMMARY,
  approxByteSizeLabel,
  bundleBatchFullReason,
  bundleCapsFrom,
  bundleErrorMessage,
  bundleFileNamesFor,
  bundleImagesEffectivelyEnabled,
  bundleTotalBytesLimit,
  bundleZipInputLimit,
  classifyBundleContents,
  collectDirectoryFiles,
  directoryHasMarkdown,
  directoryTooLargeMessage,
  directoryTruncatedMessage,
  inlineTooLargeImageLines,
  inlineTooLargeMessage,
  missingImageLine,
  noAltImageLine,
  notStagedNote,
  processBundleCandidates,
  processMarkdownCandidate,
  readDirectoryAsBundleFiles,
  receiptSummaryLine,
  remoteImageLine,
  unpackZipFile,
  unsupportedImageLine,
} from "../../app/bundle-intake.ts";
import { MD_BUNDLE_MAX_ENTRIES, MD_BUNDLE_TOTAL_BYTES_FACTOR } from "../../app/md-bundle.ts";

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

test("inlineTooLargeMessage: 有实际内联张数时，同时报出体积与上限，措辞含「精简图片」", () => {
  const msg = inlineTooLargeMessage(3 * 1024 * 1024, 1024 * 1024, 1);
  assert.match(msg, /约 3\.0 MB/);
  assert.match(msg, /1 MB/);
  assert.match(msg, /内联图片后体积/);
  assert.match(msg, /精简图片或拆分文档/);
});

// F3：零张实际内联时（正文本身已超限、图片配对被跳过、或正文里根本没有本地图片），
// 「请精简图片」是对不上症状的建议——不得出现在措辞里，改说「拆分文档」。
test("inlineTooLargeMessage: 零张实际内联时不提图片，只说文档体积超限、请拆分文档", () => {
  const msg = inlineTooLargeMessage(3 * 1024 * 1024, 1024 * 1024, 0);
  assert.match(msg, /约 3\.0 MB/);
  assert.match(msg, /1 MB/);
  assert.match(msg, /文档体积.*超过单文件上限.*请拆分文档后重试/);
  assert.ok(!msg.includes("图片"), `零张内联时不该提图片：${msg}`);
  assert.ok(!msg.includes("内联"), `零张内联时不该说「内联图片后体积」：${msg}`);
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
  // 逐图明细必须透传出来：只报总量，用户唯一能做的就是把整份文档拆开重试
  assert.equal(result.images.length, 1);
  assert.equal(result.images[0].path, "pic.png");
  assert.ok(result.images[0].encodedBytes > 0);
});

test("processMarkdownCandidate: fileNameOverride 覆盖 basename（同批同名 md 的消歧命名）", () => {
  const mdFile = { path: "a/README.md", bytes: new TextEncoder().encode("# hi") };
  const plain = processMarkdownCandidate(mdFile, [mdFile], { uploadMaxBytes: 10_000 });
  assert.equal(plain.fileName, "README.md");
  const renamed = processMarkdownCandidate(mdFile, [mdFile], { uploadMaxBytes: 10_000 }, "a-README.md");
  assert.equal(renamed.fileName, "a-README.md");
});

test("processMarkdownCandidate: imageMaxBytes/maxImagesPerSource 传入后对内联生效（上限传入生效）", () => {
  const mdText = "# Title\n\n![one](a.png)\n\n![two](b.png)\n";
  const mdFile = { path: "note.md", bytes: new TextEncoder().encode(mdText) };
  const files = [
    mdFile,
    { path: "a.png", bytes: PNG },
    { path: "b.png", bytes: PNG },
  ];
  // 单图上限比 PNG 的字节数还小：两张图都该被判 image_too_large，而不是内联。
  const byBytes = processMarkdownCandidate(mdFile, files, {
    uploadMaxBytes: 10_000_000,
    imageMaxBytes: 1,
  });
  assert.equal(byBytes.ok, true);
  assert.equal(byBytes.receipt.inlined.length, 0);
  assert.equal(byBytes.receipt.unsupported.length, 2);
  assert.ok(byBytes.receipt.unsupported.every((item) => item.reason === "image_too_large"));

  // 张数上限为 1：第一张内联成功，第二张被 too_many_images 挡下。
  const byCount = processMarkdownCandidate(mdFile, files, {
    uploadMaxBytes: 10_000_000,
    maxImagesPerSource: 1,
  });
  assert.equal(byCount.ok, true);
  assert.equal(byCount.receipt.inlined.length, 1);
  assert.equal(byCount.receipt.unsupported.length, 1);
  assert.equal(byCount.receipt.unsupported[0].reason, "too_many_images");
});

// ---------------------------------------------------------------------------
// processMarkdownCandidate：imagesEnabled === false（部署未开启图片存储）
// ---------------------------------------------------------------------------

test("processMarkdownCandidate: imagesEnabled=false 时跳过整个图片配对/内联，正文原样返回，回执全空", () => {
  const mdText = "# Title\n\n![a picture](pic.png)\n";
  const mdFile = { path: "docs/note.md", bytes: new TextEncoder().encode(mdText) };
  const picFile = { path: "docs/pic.png", bytes: PNG };
  const result = processMarkdownCandidate(
    mdFile,
    [mdFile, picFile],
    { uploadMaxBytes: 10_000_000, imagesEnabled: false },
  );
  assert.equal(result.ok, true);
  assert.equal(result.fileName, "note.md");
  // 正文原样保留：既没有被改写成 data URI，也没有做任何图片相关处理。
  assert.equal(result.rewritten, mdText);
  assert.deepEqual(result.receipt, { inlined: [], missing: [], unsupported: [], remote: [], noAlt: [] });
  // F2：pairingSkipped 必须为真——这份回执是"根本没做配对"而不是"配对了、零命中"，
  // 两者对回执面板是不同的措辞。
  assert.equal(result.pairingSkipped, true);
});

test("processMarkdownCandidate: imagesEnabled=false 时仍然遵守单文件上传上限，且标注 pairingSkipped", () => {
  const mdText = "# Title\n\n" + "x".repeat(100);
  const mdFile = { path: "note.md", bytes: new TextEncoder().encode(mdText) };
  const result = processMarkdownCandidate(
    mdFile,
    [mdFile],
    { uploadMaxBytes: 10, imagesEnabled: false },
  );
  assert.equal(result.ok, false);
  assert.equal(result.limit, 10);
  assert.ok(result.bytes > 10);
  assert.equal(result.images.length, 0);
  assert.deepEqual(result.receipt, { inlined: [], missing: [], unsupported: [], remote: [], noAlt: [] });
  assert.equal(result.pairingSkipped, true);
});

test("processMarkdownCandidate: imagesEnabled 省略时保持既有内联行为（向后兼容），pairingSkipped 为假", () => {
  const mdText = "# Title\n\n![a picture](pic.png)\n";
  const mdFile = { path: "note.md", bytes: new TextEncoder().encode(mdText) };
  const picFile = { path: "pic.png", bytes: PNG };
  const result = processMarkdownCandidate(mdFile, [mdFile, picFile], { uploadMaxBytes: 10_000_000 });
  assert.equal(result.ok, true);
  assert.match(result.rewritten, /data:image\/png;base64,/);
  assert.equal(result.receipt.inlined.length, 1);
  assert.equal(result.pairingSkipped, false);
});

test("processMarkdownCandidate: imagesEnabled=true 且内联后超限时 pairingSkipped 为假", () => {
  const mdText = "# Title\n\n![a picture](pic.png)\n";
  const mdFile = { path: "note.md", bytes: new TextEncoder().encode(mdText) };
  const picFile = { path: "pic.png", bytes: PNG };
  const result = processMarkdownCandidate(mdFile, [mdFile, picFile], { uploadMaxBytes: 5 });
  assert.equal(result.ok, false);
  assert.equal(result.pairingSkipped, false);
});

// ---------------------------------------------------------------------------
// bundleImagesEffectivelyEnabled：零值护栏（codex #518 R1 P2）
//
// MINERU_MAX_IMAGE_BYTES=0 / MINERU_MAX_IMAGES_PER_SOURCE=0 是合法部署值（后端有
// 用例钉住），语义是「一张都不存」。把 0 当成「拿不到上限、不做预检」会让浏览器照常
// base64 内联并报「N 张已内联」，而服务端把资产全部丢弃——既白付体积又对用户撒谎。
// ---------------------------------------------------------------------------

test("bundleImagesEffectivelyEnabled: 任一上限为 0 即视为图片存储关闭", () => {
  assert.equal(bundleImagesEffectivelyEnabled({ imageMaxBytes: 0 }), false);
  assert.equal(bundleImagesEffectivelyEnabled({ maxImagesPerSource: 0 }), false);
  assert.equal(bundleImagesEffectivelyEnabled({ imagesEnabled: true, imageMaxBytes: 0 }), false);
  assert.equal(
    bundleImagesEffectivelyEnabled({ imagesEnabled: true, maxImagesPerSource: 0 }),
    false,
  );
});

test("bundleImagesEffectivelyEnabled: 总开关 false 关闭；正数上限与 null/缺失保持开启", () => {
  assert.equal(bundleImagesEffectivelyEnabled({ imagesEnabled: false }), false);
  // 反向对照：上面几条单独看，把实现写成恒 false 也能通过。
  assert.equal(bundleImagesEffectivelyEnabled({}), true);
  assert.equal(
    bundleImagesEffectivelyEnabled({ imagesEnabled: true, imageMaxBytes: null, maxImagesPerSource: null }),
    true,
  );
  assert.equal(
    bundleImagesEffectivelyEnabled({ imagesEnabled: true, imageMaxBytes: 1024, maxImagesPerSource: 20 }),
    true,
  );
});

test("processMarkdownCandidate: imageMaxBytes=0 时按「图片存储关闭」短路，绝不内联", () => {
  const mdText = "# Title\n\n![a picture](pic.png)\n";
  const mdFile = { path: "docs/note.md", bytes: new TextEncoder().encode(mdText) };
  const picFile = { path: "docs/pic.png", bytes: PNG };
  const result = processMarkdownCandidate(
    mdFile,
    [mdFile, picFile],
    { uploadMaxBytes: 10_000_000, imagesEnabled: true, imageMaxBytes: 0, maxImagesPerSource: 200 },
  );
  assert.equal(result.ok, true);
  // 关键断言：0 不能被 resolveLimit 反过来解释成「无上限」而照常内联。
  assert.equal(result.rewritten, mdText);
  assert.ok(!result.rewritten.includes("data:image"), "零值部署不得产出 base64 内联");
  assert.deepEqual(result.receipt, { inlined: [], missing: [], unsupported: [], remote: [], noAlt: [] });
  assert.equal(result.pairingSkipped, true);
});

test("processMarkdownCandidate: maxImagesPerSource=0 时同样短路（张数上限那一维）", () => {
  const mdText = "# Title\n\n![a picture](pic.png)\n";
  const mdFile = { path: "note.md", bytes: new TextEncoder().encode(mdText) };
  const picFile = { path: "pic.png", bytes: PNG };
  const result = processMarkdownCandidate(
    mdFile,
    [mdFile, picFile],
    { uploadMaxBytes: 10_000_000, imagesEnabled: true, imageMaxBytes: 5_242_880, maxImagesPerSource: 0 },
  );
  assert.equal(result.ok, true);
  assert.equal(result.rewritten, mdText);
  assert.equal(result.pairingSkipped, true);
});

test("BUNDLE_IMAGES_DISABLED_NOTE：非空中文提示，说明图片不会被保存", () => {
  assert.equal(typeof BUNDLE_IMAGES_DISABLED_NOTE, "string");
  assert.match(BUNDLE_IMAGES_DISABLED_NOTE, /图片/);
  assert.match(BUNDLE_IMAGES_DISABLED_NOTE, /不会被保存/);
});

test("PAIRING_SKIPPED_SUMMARY：非空中文提示，说明本次未做配对（区别于「没发现图片」）", () => {
  assert.equal(typeof PAIRING_SKIPPED_SUMMARY, "string");
  assert.match(PAIRING_SKIPPED_SUMMARY, /配对/);
  assert.match(PAIRING_SKIPPED_SUMMARY, /关闭/);
});

// ---------------------------------------------------------------------------
// bundleFileNamesFor：同批同名 md 的消歧命名
// ---------------------------------------------------------------------------

test("bundleFileNamesFor: basename 唯一时原样使用，不无谓改名", () => {
  const names = bundleFileNamesFor(["a/note.md", "b/guide.md"]);
  assert.equal(names.get("a/note.md"), "note.md");
  assert.equal(names.get("b/guide.md"), "guide.md");
});

test("bundleFileNamesFor: basename 撞车时带上包内父目录前缀，且产出不含路径分隔符", () => {
  const names = bundleFileNamesFor(["a/README.md", "b/README.md", "solo.md"]);
  assert.equal(names.get("a/README.md"), "a-README.md");
  assert.equal(names.get("b/README.md"), "b-README.md");
  // 没撞车的那份不受影响
  assert.equal(names.get("solo.md"), "solo.md");
  for (const value of names.values()) {
    assert.ok(!value.includes("/"), `消歧后的文件名不得含路径分隔符：${value}`);
    assert.ok(value.endsWith(".md"), `消歧后必须保留扩展名（后端按扩展名分派解析器）：${value}`);
  }
});

test("bundleFileNamesFor: 深层路径逐段拼接，仍两两不同", () => {
  const names = bundleFileNamesFor(["docs/en/README.md", "docs/zh/README.md"]);
  assert.equal(names.get("docs/en/README.md"), "docs-en-README.md");
  assert.equal(names.get("docs/zh/README.md"), "docs-zh-README.md");
  assert.equal(new Set(names.values()).size, 2);
});

test("bundleFileNamesFor: 消歧后仍撞车时追加序号，绝不产出两个相同名字", () => {
  // 「a/b/c.md」与「a-b/c.md」拍平后都是 a-b-c.md
  const names = bundleFileNamesFor(["a/b/c.md", "a-b/c.md"]);
  assert.equal(new Set(names.values()).size, 2);
  assert.ok([...names.values()].some((value) => /-2\.md$/.test(value)));
});

// ---------------------------------------------------------------------------
// inlineTooLargeImageLines / 未入列标注
// ---------------------------------------------------------------------------

test("inlineTooLargeImageLines: 按体积降序点名最大的几张，超出上限只报剩余计数", () => {
  const images = [
    { src: "s.png", path: "imgs/small.png", encodedBytes: 1024 },
    { src: "b.png", path: "imgs/big.png", encodedBytes: 5 * 1024 * 1024 },
    { src: "m.png", path: "imgs/mid.png", encodedBytes: 2 * 1024 * 1024 },
    { src: "t.png", path: "imgs/tiny.png", encodedBytes: 10 },
  ];
  const [line] = inlineTooLargeImageLines(images, 2);
  assert.match(line, /imgs\/big\.png（约 5\.0 MB）/);
  assert.match(line, /imgs\/mid\.png（约 2\.0 MB）/);
  assert.ok(!line.includes("tiny"), `只该点名最大的两张：${line}`);
  assert.match(line, /另有 2 张/);
});

test("inlineTooLargeImageLines: 张数不超上限时不写「另有」，零张时不产出任何行", () => {
  const [line] = inlineTooLargeImageLines([
    { src: "a.png", path: "a.png", encodedBytes: 2048 },
  ]);
  assert.match(line, /a\.png（约 2\.0 KB）/);
  assert.ok(!line.includes("另有"));
  assert.deepEqual(inlineTooLargeImageLines([]), []);
  assert.ok(INLINE_TOO_LARGE_IMAGE_LINES >= 1);
});

test("notStagedNote: 标注明确说出「未加入待上传列表」并带上原因，不伪装成功", () => {
  const note = notStagedNote(ALREADY_STAGED_REASON);
  assert.match(note, /未加入待上传列表/);
  assert.match(note, new RegExp(ALREADY_STAGED_REASON));
});

test("读取失败的两条原因文案非空且互相区分（文件 / 文件夹）", () => {
  assert.ok(BUNDLE_READ_FAILED_REASON.length > 0);
  assert.ok(DIRECTORY_READ_FAILED_REASON.length > 0);
  assert.notEqual(BUNDLE_READ_FAILED_REASON, DIRECTORY_READ_FAILED_REASON);
  assert.match(DIRECTORY_READ_FAILED_REASON, /文件夹/);
});

// ---------------------------------------------------------------------------
// bundleTotalBytesLimit：与 zip 解压护栏共用同一个系数
// ---------------------------------------------------------------------------

test("bundleTotalBytesLimit: 复用 md-bundle 的系数，不另抄一份数字", () => {
  assert.equal(bundleTotalBytesLimit(1024), 1024 * MD_BUNDLE_TOTAL_BYTES_FACTOR);
});

test("bundleTotalBytesLimit: 缺失/非正数 = 没有可用上限，不做本地预检", () => {
  assert.equal(bundleTotalBytesLimit(null), null);
  assert.equal(bundleTotalBytesLimit(0), null);
  assert.equal(bundleTotalBytesLimit(-1), null);
});

test("directoryTooLargeMessage: 报出具体上限，并说明内容未被读取", () => {
  const msg = directoryTooLargeMessage(200 * 1024 * 1024);
  assert.match(msg, /200 MB/);
  assert.match(msg, /未读取内容/);
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

// 边界：恰好等于上限**不是**截断。判据写成 `> maxFiles` 或先 push 再判都会让这条
// 变红——一个刚好装满的文件夹被报成「未能完整读取」，用户被要求去精简一个本来
// 完全合法的文件夹。
test("collectDirectoryFiles: 文件数恰好等于上限时不算截断", async () => {
  const root = fakeDirEntry("f", [
    fakeFileEntry("a.txt", "a"),
    fakeFileEntry("b.txt", "b"),
  ]);
  const { entries, truncated } = await collectDirectoryFiles(root, { maxFiles: 2 });
  assert.equal(truncated, false);
  assert.equal(entries.length, 2);
});

test("collectDirectoryFiles: 命中总字节预算 → overBudget=true（与 truncated 分开两个标志）", async () => {
  const root = fakeDirEntry("f", [
    fakeFileEntry("a.bin", "0123456789"),  // 10 字节
    fakeFileEntry("b.bin", "0123456789"),
    fakeFileEntry("c.bin", "0123456789"),
  ]);
  const result = await collectDirectoryFiles(root, { maxTotalBytes: 25 });
  assert.equal(result.overBudget, true);
  assert.equal(result.truncated, false, "体积超限不是「文件太多/太深」，两条文案完全不同");
  assert.equal(result.entries.length, 2, "超预算的那一份就地止损，不继续收集");
  assert.ok(result.totalBytes <= 25);
});

test("collectDirectoryFiles: 总量恰好等于预算时放行（边界不误伤）", async () => {
  const root = fakeDirEntry("f", [
    fakeFileEntry("a.bin", "0123456789"),
    fakeFileEntry("b.bin", "0123456789"),
  ]);
  const result = await collectDirectoryFiles(root, { maxTotalBytes: 20 });
  assert.equal(result.overBudget, false);
  assert.equal(result.entries.length, 2);
  assert.equal(result.totalBytes, 20);
});

test("collectDirectoryFiles: 未给预算（null）时不做总量预检，行为与改动前一致", async () => {
  const root = fakeDirEntry("f", [fakeFileEntry("a.bin", "0123456789")]);
  const result = await collectDirectoryFiles(root, { maxTotalBytes: null });
  assert.equal(result.overBudget, false);
  assert.equal(result.entries.length, 1);
  assert.equal(result.totalBytes, 10);
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

// md-bundle.ts 现在核对 central directory 里的 CRC-32（拦位翻转的 stored 条目/
// 坏 deflate 流），所以这个夹具必须写真实值，不能再全零——否则每份用它构造的包
// 都会被新护栏当成损坏拒收。算法与 md-bundle.test.mjs 里那份逐字同构。
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

/** 最小 store-only zip 构造器：只用于验证 unpackZipFile 这层"读 File → 交给
 *  parseZipBundle"的胶水代码本身，不重复 md-bundle.test.mjs 已经覆盖的 zip 格式
 *  边界（那些测试直接喂字节给 parseZipBundle，本文件不重复）。 */
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
    view.setUint32(14, crc32(entry.data), true);
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
    view.setUint32(16, crc32(entry.data), true);
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

// ---------------------------------------------------------------------------
// unpackZipFile：压缩输入体积闸（codex #518 R1 P2）
//
// `.zip` 刻意绕开普通上传的单文件大小校验，而解压后总量的闸只有在整包字节已经进了
// 内存之后才够得着——误选一个几 GB 的归档能在任何结构化拒绝之前把标签页耗死。
// 因此闸必须跑在 arrayBuffer() 之前，只看 File.size 元数据。
// ---------------------------------------------------------------------------

/** 造一个能数出 `arrayBuffer()` 被调用几次、并可伪造 `size` 的 zip 文件。
 *  `size` 是 Blob.prototype 上的 getter，`defineProperty` 在实例上遮蔽它即可，
 *  这样才能在不真的分配 1 GiB 的前提下测到回退上限那条边界。 */
function spyZipFile(bytes, { size } = {}) {
  const file = new File([bytes], "bundle.zip");
  const calls = { arrayBuffer: 0 };
  if (size !== undefined) Object.defineProperty(file, "size", { value: size });
  const real = file.arrayBuffer.bind(file);
  file.arrayBuffer = async () => {
    calls.arrayBuffer += 1;
    return real();
  };
  return { file, calls };
}

test("bundleZipInputLimit: 已知单文件上限时复用解压后总量那条线，不另抄一份数字", () => {
  assert.equal(bundleZipInputLimit(1024), 1024 * MD_BUNDLE_TOTAL_BYTES_FACTOR);
  assert.equal(bundleZipInputLimit(50 * 1024 * 1024), bundleTotalBytesLimit(50 * 1024 * 1024));
});

test("bundleZipInputLimit: 配置未到达时回退具名常量，而不是「不预检」", () => {
  // 这里**不能**沿用「拿不到上限就放行」——那正是本条要堵的洞。
  assert.equal(bundleZipInputLimit(null), BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES);
  assert.equal(bundleZipInputLimit(0), BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES);
  // 取值依据：SOURCE_UPLOAD_MAX_MB 的协议最大值 1024 MiB。
  assert.equal(BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES, 1024 * 1024 * 1024);
});

test("unpackZipFile: 超过压缩输入上限时直接拒绝，且**不调用** arrayBuffer", async () => {
  const caps = { uploadMaxBytes: 1024 };            // → 上限 4096 字节
  const { file, calls } = spyZipFile(new Uint8Array(5000));
  const result = await unpackZipFile(file, caps);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "too_large");
  assert.equal(result.error.actual, 5000);
  assert.equal(result.error.limit, 1024 * MD_BUNDLE_TOTAL_BYTES_FACTOR);
  assert.equal(calls.arrayBuffer, 0, "整包字节绝不能在体积闸之前被读进内存");
});

test("unpackZipFile: 恰好等于上限时放行（边界是 >，不是 >=）", async () => {
  const caps = { uploadMaxBytes: 1024 };            // → 上限 4096 字节
  const { file, calls } = spyZipFile(new Uint8Array(4096));
  const result = await unpackZipFile(file, caps);
  assert.equal(calls.arrayBuffer, 1, "等于上限的包必须照常读取解析");
  assert.equal(result.ok, false);
  // 走到了 parseZipBundle（这堆零字节不是 zip），而不是被体积闸拦下。
  assert.equal(result.error.code, "not_a_zip");
});

test("unpackZipFile: 上限未知时按回退常量拦截（不真的分配 1 GiB）", async () => {
  const over = spyZipFile(new Uint8Array(8), { size: BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES + 1 });
  const rejected = await unpackZipFile(over.file, { uploadMaxBytes: 0 });
  assert.equal(rejected.ok, false);
  assert.equal(rejected.error.code, "too_large");
  assert.equal(rejected.error.limit, BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES);
  assert.equal(over.calls.arrayBuffer, 0);

  const at = spyZipFile(new Uint8Array(8), { size: BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES });
  const allowed = await unpackZipFile(at.file, { uploadMaxBytes: 0 });
  assert.equal(at.calls.arrayBuffer, 1);
  assert.equal(allowed.error.code, "not_a_zip");
});

test("bundleErrorMessage: 读入内存前拦下的整包级 too_large 不说「解压后」", () => {
  const preRead = bundleErrorMessage({
    code: "too_large",
    actual: 3 * 1024 * 1024,
    limit: 200 * 1024 * 1024,
  });
  assert.match(preRead, /约 3\.0 MB/);
  assert.match(preRead, /200 MB/);
  assert.match(preRead, /未读取内容/);
  assert.ok(!preRead.includes("解压后"), `一个字节都没读，不能说「解压后」：${preRead}`);
  // 解压过程中触发的那条（parseZipBundle 必定带上触发条目 path）逐字不变。
  assert.equal(
    bundleErrorMessage({ code: "too_large", path: "big/a.bin", limit: 4096 }),
    "压缩包解压后体积过大",
  );
});

// ---------------------------------------------------------------------------
// processBundleCandidates：单次上传数量上限必须在**内联之前**生效
// （codex #518 R1 P1）
// ---------------------------------------------------------------------------

/** 一个会记下「`bytes` 被读过」的候选：`processMarkdownCandidate` 的第一件事就是
 *  `decodeMarkdownText(mdFile.bytes)`，所以这个计数就是「它被调用了几次」的证明，
 *  不需要往生产代码里塞一个测试专用的注入口。 */
function countingCandidate(path, text, seen) {
  const bytes = new TextEncoder().encode(text);
  return {
    path,
    get bytes() {
      seen.push(path);
      return bytes;
    },
  };
}

function namesFor(candidates) {
  return bundleFileNamesFor(candidates.map((item) => item.path));
}

test("processBundleCandidates: 候选 5、余额 2 → 恰处理 2、跳过 3，且零多余内联调用", () => {
  const seen = [];
  const candidates = ["a", "b", "c", "d", "e"].map(
    (name) => countingCandidate(`${name}.md`, `# ${name}\n\n![pic](pic.png)\n`, seen),
  );
  const files = [{ path: "pic.png", bytes: PNG }];
  const batch = processBundleCandidates(
    candidates,
    files,
    { uploadMaxBytes: 10_000_000 },
    { names: namesFor(candidates), remainingSlots: 2, batchCap: 20 },
  );
  assert.equal(batch.processed.length, 2);
  assert.equal(batch.skipped.length, 3);
  // 这一条才是本次修复的核心：其余 3 份**根本没被处理**，没有白付 base64 体积。
  assert.deepEqual(seen, ["a.md", "b.md"]);
  assert.deepEqual(batch.skipped.map((item) => item.name), ["c.md", "d.md", "e.md"]);
  assert.ok(batch.skipped.every((item) => item.reason === bundleBatchFullReason(20)));
  // 被处理的那两份确实走完了内联（对照：不是整批都被跳过）。
  assert.ok(batch.processed.every((item) => item.ok && item.rewritten.includes("data:image")));
});

test("processBundleCandidates: 余额为 0 时一份都不处理（列表已满）", () => {
  const seen = [];
  const candidates = [countingCandidate("a.md", "# a", seen), countingCandidate("b.md", "# b", seen)];
  const batch = processBundleCandidates(
    candidates,
    [],
    { uploadMaxBytes: 10_000_000 },
    { names: namesFor(candidates), remainingSlots: 0, batchCap: 20 },
  );
  assert.deepEqual(batch.processed, []);
  assert.equal(batch.skipped.length, 2);
  assert.deepEqual(seen, [], "余额为 0 时连一次解码都不该发生");
});

test("processBundleCandidates: 余额足够时全部处理，跳过为空（反向对照）", () => {
  const seen = [];
  const candidates = [countingCandidate("a.md", "# a", seen), countingCandidate("b.md", "# b", seen)];
  const batch = processBundleCandidates(
    candidates,
    [],
    { uploadMaxBytes: 10_000_000 },
    { names: namesFor(candidates), remainingSlots: 5, batchCap: 20 },
  );
  assert.equal(batch.processed.length, 2);
  assert.deepEqual(batch.skipped, []);
  assert.deepEqual(seen, ["a.md", "b.md"]);
});

test("processBundleCandidates: 被单文件上限拒掉的候选不占名额（与 mergeStagedFiles 同口径）", () => {
  const seen = [];
  const candidates = [
    countingCandidate("big.md", "# big\n\n" + "x".repeat(500), seen),
    countingCandidate("small.md", "# s", seen),
  ];
  const batch = processBundleCandidates(
    candidates,
    [],
    { uploadMaxBytes: 50 },
    { names: namesFor(candidates), remainingSlots: 1, batchCap: 20 },
  );
  // big.md 超限 → ok:false，不入列也就不占格，small.md 仍拿得到那唯一一格。
  assert.equal(batch.processed.length, 2);
  assert.equal(batch.processed[0].ok, false);
  assert.equal(batch.processed[1].ok, true);
  assert.deepEqual(batch.skipped, []);
  assert.deepEqual(seen, ["big.md", "small.md"]);
});

test("processBundleCandidates: 跳过项沿用同批消歧后的名字，而不是包内路径", () => {
  const seen = [];
  const candidates = [
    countingCandidate("a/README.md", "# a", seen),
    countingCandidate("b/README.md", "# b", seen),
  ];
  const batch = processBundleCandidates(
    candidates,
    [],
    { uploadMaxBytes: 10_000_000 },
    { names: namesFor(candidates), remainingSlots: 1, batchCap: 20 },
  );
  assert.deepEqual(batch.skipped.map((item) => item.name), ["b-README.md"]);
});

test("bundleBatchFullReason: 带上具体上限，并说明「未处理」（区别于内联完却装不下）", () => {
  const reason = bundleBatchFullReason(BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH);
  assert.match(reason, new RegExp(String(BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH)));
  assert.match(reason, /未处理/);
  // 回退值必须等于后端固定的 multipart 资源护栏（SOURCE_UPLOAD_MAX_FILES_PER_BATCH）。
  assert.equal(BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH, 20);
});
