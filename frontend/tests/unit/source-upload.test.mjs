import test from "node:test";
import assert from "node:assert/strict";

import {
  classifyStagedFiles,
  compactStagedFileName,
  summarizeUpload,
  uploadDocTypeFields,
  fillAutoDetectedTypes,
  markTouched,
  markAllTouched,
  applyTouchedUpdate,
  mergeLiveStagedFileWarnings,
  sourceUploadSizeLabel,
  splitFilesByUploadSize,
  scanStandaloneMarkdownImageWarnings,
  standaloneMarkdownImageWarnings,
} from "../../app/source-upload.ts";

const src = (id, reused) => ({ id, title: `${id}.pdf`, ...(reused === undefined ? {} : { reused }) });

test("compactStagedFileName: 短文件名保持原样，长文件名中间压缩且保留末尾扩展名", () => {
  assert.equal(compactStagedFileName("short-name.pdf"), "short-name.pdf");

  const original = `silicon-notebook-KG抽取与${"a".repeat(60)}检索方法说明.pdf`;
  const compacted = compactStagedFileName(original);
  assert.equal(Array.from(compacted).length, 48);
  assert.match(compacted, /^silicon-notebook-KG抽取与/);
  assert.match(compacted, /…/);
  assert.match(compacted, /检索方法说明\.pdf$/);
});

test("compactStagedFileName: 不会截断 emoji 等代理对字符", () => {
  const compacted = compactStagedFileName(`测试${"😀".repeat(20)}文件.pdf`, 12);
  assert.equal(Array.from(compacted).length, 12);
  assert.doesNotMatch(compacted, /\uFFFD/);
  assert.match(compacted, /\.pdf$/);
});

test("splitFilesByUploadSize: 精确采用后端下发的字节上限，等于上限可上传", () => {
  const files = [
    { name: "fits.pdf", size: 1024 },
    { name: "too-large.pdf", size: 1025 },
  ];
  assert.deepEqual(splitFilesByUploadSize(files, 1024), {
    accepted: [files[0]],
    rejected: [files[1]],
  });
  assert.equal(sourceUploadSizeLabel(50 * 1024 * 1024), "50 MB");
  assert.equal(sourceUploadSizeLabel(1024), "1 KB");
});

test("splitFilesByUploadSize: 配置尚未到达时不猜测旧上限，交给后端 413", () => {
  const files = [{ name: "pending.pdf", size: 99 * 1024 * 1024 }];
  assert.deepEqual(splitFilesByUploadSize(files, null), {
    accepted: files,
    rejected: [],
  });
});

test("standaloneMarkdownImageWarnings: 相对/本地图片提示改用 ZIP 或完整文件夹", async () => {
  const warnings = await standaloneMarkdownImageWarnings({
    name: "mineru.filled.md",
    size: 120,
    async text() {
      return "![流程图](质量和流程/images/图片12.jpg)\n![另一张](./images/a.png)";
    },
  });
  assert.equal(warnings.length, 1);
  assert.match(warnings[0].reason, /2 个本地图片引用/);
  assert.match(warnings[0].reason, /ZIP 或拖入完整文件夹/);
  assert.match(warnings[0].reason, /问答中将无法展示/);
});

test("standaloneMarkdownImageWarnings: 远程图片不自动下载，data URI 与普通文件不误报", async () => {
  const remote = await standaloneMarkdownImageWarnings({
    name: "note.markdown",
    size: 140,
    async text() {
      return "![远程](https://example.test/a.png)\n![内嵌](data:image/png;base64,AAAA)";
    },
  });
  assert.equal(remote.length, 1);
  assert.match(remote[0].reason, /1 个远程图片引用/);
  assert.match(remote[0].reason, /不会自动下载/);

  assert.deepEqual(await standaloneMarkdownImageWarnings({
    name: "note.txt",
    size: 10,
    async text() { throw new Error("非 Markdown 不应读取内容"); },
  }), []);
  assert.deepEqual(await standaloneMarkdownImageWarnings({
    name: "unreadable.md",
    size: 10,
    async text() { throw new Error("advisory scan fails open"); },
  }), []);
});

test("scanStandaloneMarkdownImageWarnings: 大批文件严格串行读取，warning 保留 name+size 身份", async () => {
  let active = 0;
  let peak = 0;
  const readOrder = [];
  const files = [
    { name: "same.md", size: 10, body: "![a](a.png)" },
    { name: "same.md", size: 20, body: "![b](b.png)" },
    { name: "remote.md", size: 30, body: "![c](https://example.test/c.png)" },
  ].map(({ name, size, body }) => ({
    name,
    size,
    async text() {
      active += 1;
      peak = Math.max(peak, active);
      readOrder.push(`start:${size}`);
      await Promise.resolve();
      readOrder.push(`end:${size}`);
      active -= 1;
      return body;
    },
  }));

  const warnings = await scanStandaloneMarkdownImageWarnings(files);
  assert.equal(peak, 1);
  assert.deepEqual(readOrder, [
    "start:10", "end:10", "start:20", "end:20", "start:30", "end:30",
  ]);
  assert.deepEqual(warnings.map(({ name, size }) => ({ name, size })), [
    { name: "same.md", size: 10 },
    { name: "same.md", size: 20 },
    { name: "remote.md", size: 30 },
  ]);
});

test("mergeLiveStagedFileWarnings: 删除中的迟到结果被拒绝，同名不同大小保持独立", () => {
  const reason = "本地图片缺失";
  const previous = [{ name: "same.md", size: 10, reason }];
  const incoming = [
    { name: "removed.md", size: 5, reason },
    { name: "same.md", size: 10, reason },
    { name: "same.md", size: 20, reason },
  ];

  assert.deepEqual(mergeLiveStagedFileWarnings(previous, incoming, [
    { name: "same.md", size: 10 },
    { name: "same.md", size: 20 },
  ]), [
    { name: "same.md", size: 10, reason },
    { name: "same.md", size: 20, reason },
  ]);
});

// ---------------- 追踪「用户是否动过类型下拉框」→ 上传发 per-file doc_type_explicit

test("uploadDocTypeFields: touched 的文件发 explicit=1，未 touched 发 0（据交互不据值）", () => {
  // file0：用户手动选回「自动检测」（值空但 touched）→ explicit=1（显式重置回自动）。
  // file1：auto-detect 自动填了 textbook、用户没动（有值但未 touched）→ explicit=0。
  const fields = uploadDocTypeFields(["", "textbook"], [true, false]);
  assert.deepEqual(fields, [
    { docType: "", explicit: "1" },
    { docType: "textbook", explicit: "0" },
  ]);
});

test("uploadDocTypeFields: 显式选具体类型发 explicit=1，doc_types 原样透传（空串表自动检测）", () => {
  const fields = uploadDocTypeFields(["academic_paper", ""], [true, false]);
  assert.deepEqual(fields.map((f) => f.docType), ["academic_paper", ""]);
  assert.deepEqual(fields.map((f) => f.explicit), ["1", "0"]);
});

test("fillAutoDetectedTypes: 自动检测回填只填空项、不覆盖已选值，且**不置 touched**", () => {
  // 模拟 page.tsx 的状态机：两个文件入列（types 全空、touched 全 false）。
  let types = ["", ""];
  const touched = [false, false];
  // auto-detect 把 file0 填成 textbook（file1 没检测出）。
  types = fillAutoDetectedTypes(types, ["textbook", undefined]);
  assert.deepEqual(types, ["textbook", ""], "只填空项、无建议的保持空");
  assert.deepEqual(touched, [false, false], "auto-detect 是系统建议、不算用户表态");
  // 于是重传没动下拉框时，两个文件都发 explicit=0 → 后端保留既有类型（消除静默重置回退）。
  assert.deepEqual(
    uploadDocTypeFields(types, touched).map((f) => f.explicit),
    ["0", "0"],
  );
});

test("fillAutoDetectedTypes: 已被用户选过的值绝不被自动检测覆盖", () => {
  assert.deepEqual(fillAutoDetectedTypes(["academic_paper", ""], ["textbook", "textbook"]), [
    "academic_paper", // 用户已选，检测结果不得覆盖
    "textbook",       // 仍为空，用检测结果回填
  ]);
});

test("fillAutoDetectedTypes: 检测在飞时用户改回「自动检测」的项（空但 touched）不被回填", () => {
  // 场景：检测异步进行时，用户先选了具体类型、又改回「自动检测」——值空但 touched=true。
  // 回填必须跳过它（只看「值空不空」会把这个显式的自动检测当没表态、填成检测结果，上传
  // 就发成 explicit=1 + 检测类型，把复用源改成与用户显式选择相反的类型）。
  const types = fillAutoDetectedTypes(["", ""], ["textbook", "academic_paper"], [true, false]);
  assert.deepEqual(types, [
    "",               // file0：touched 的空项 = 用户显式选回自动检测 → 绝不回填
    "academic_paper", // file1：未 touched 的空项 → 正常回填检测结果
  ]);
  // 上传时 file0 发 explicit=1 + 空值（显式重置回自动），不是检测到的类型。
  const fields = uploadDocTypeFields(types, [true, false]);
  assert.deepEqual(fields[0], { docType: "", explicit: "1" });
  assert.deepEqual(fields[1], { docType: "academic_paper", explicit: "0" });
});

test("markTouched: 用户改某一项下拉框只置该项 touched；markAllTouched 置全部", () => {
  assert.deepEqual(markTouched([false, false, false], 1), [false, true, false]);
  assert.deepEqual(markAllTouched([false, false]), [true, true]);
});

// ------- applyTouchedUpdate：同步镜像 ref，检测同 tick resolve 时回填读到最新值

test("applyTouchedUpdate: 同步更新 ref 与 state（函数式与直接传值两种形态）", () => {
  const ref = { current: [false, false] };
  const seen = [];
  const setState = (v) => seen.push(v);
  // 函数式更新：从 ref.current 的最新值算起。
  const out = applyTouchedUpdate(ref, setState, (prev) => markTouched(prev, 0));
  assert.deepEqual(out, [true, false]);
  assert.deepEqual(ref.current, [true, false], "ref 立刻反映最新值，不等 useEffect");
  assert.deepEqual(seen.at(-1), [true, false], "state 被推进同一个值");
  // 直接传数组（重置 / 加文件路径）：ref 同样立刻同步。
  applyTouchedUpdate(ref, setState, []);
  assert.deepEqual(ref.current, []);
  assert.deepEqual(seen.at(-1), []);
});

test("检测在飞时用户改回自动检测 → 检测同 tick resolve → 回填读最新 touched，不覆盖", () => {
  // 复现竞态：ref 只靠 useEffect 异步更新时，检测恰在「置 touched」与「useEffect 写 ref」
  // 之间 resolve，会读到旧 ref(touched=false)、把用户显式选的空自动检测覆盖成检测结果。
  // 同步入 ref 后，回填读到的是最新 touched=true，跳过该项。
  const ref = { current: [false, false] };
  const seen = [];
  const setState = (v) => seen.push(v);

  // 用户把 file0 改回「自动检测」（值空 + touched），经 handler 同步入 ref。
  applyTouchedUpdate(ref, setState, (prev) => markTouched(prev, 0));

  // 此刻 detectStagedTypes 异步 resolve（useEffect 尚未提交）。回填读 ref.current 的最新值：
  const types = fillAutoDetectedTypes(["", ""], ["textbook", "academic_paper"], ref.current);
  assert.deepEqual(
    types,
    ["", "academic_paper"],
    "file0 的显式自动检测不被检测结果覆盖；file1 未表态照常回填",
  );
  // 上传时 file0 发 explicit=1 + 空值（显式重置回自动），不是检测到的 textbook。
  const fields = uploadDocTypeFields(types, ref.current);
  assert.deepEqual(fields[0], { docType: "", explicit: "1" });
});

test("summarizeUpload: 全是新建 → 全部计入新增，文案就是老的那句", () => {
  const outcome = summarizeUpload([src("a", false), src("b", false)]);
  assert.deepEqual(outcome.added.map((s) => s.id), ["a", "b"]);
  assert.deepEqual(outcome.reused, []);
  assert.equal(outcome.toast, "已上传 2 个来源");
});

test("summarizeUpload: 沿用的既有来源不计入新增（来源总数不再虚高）", () => {
  const outcome = summarizeUpload([src("a", true)]);
  assert.deepEqual(outcome.added, []);
  assert.deepEqual(outcome.reused.map((s) => s.id), ["a"]);
  assert.match(outcome.toast, /已经在本笔记本里/);
  assert.doesNotMatch(outcome.toast, /已上传/);
});

test("summarizeUpload: 新增与沿用混合 → 两个数字分别如实报出", () => {
  const outcome = summarizeUpload([src("a", false), src("b", true), src("c", true)]);
  assert.deepEqual(outcome.added.map((s) => s.id), ["a"]);
  assert.deepEqual(outcome.reused.map((s) => s.id), ["b", "c"]);
  assert.equal(
    outcome.toast,
    "已上传 1 个来源；另有 2 个文件的内容已经在本笔记本里，沿用原有来源（名称保持原样），没有重复添加",
  );
});

test("summarizeUpload: 文案交代沿用条目保留原名（同内容改名再传不会换名字）", () => {
  assert.match(summarizeUpload([src("a", true)]).toast, /名称保持原样/);
});

test("summarizeUpload: 老后端不返回 reused 时按新建处理，行为与改动前一致", () => {
  const outcome = summarizeUpload([src("a"), src("b")]);
  assert.equal(outcome.added.length, 2);
  assert.equal(outcome.reused.length, 0);
  assert.equal(outcome.toast, "已上传 2 个来源");
});

// --------------------------------------------- 改了文档类型的沿用条目要如实交代

const retyped = (id, docType, parseStatus) => ({
  id,
  title: `${id}.pdf`,
  reused: true,
  doc_type: docType,
  parse_status: parseStatus,
});

test("summarizeUpload: 沿用但改了文档类型 → 单独归类，并说清在按新类型重抽", () => {
  const outcome = summarizeUpload(
    [retyped("a", "textbook", "extracting")],
    new Map([["a", ""]]),
  );
  assert.deepEqual(outcome.retyped.map((s) => s.id), ["a"]);
  assert.deepEqual(outcome.added, []);
  assert.equal(
    outcome.toast,
    "1 个文件的内容已经在本笔记本里，沿用原有来源并改用了新的文档类型，正在按新类型重新分析",
  );
});

test("summarizeUpload: 类型改了但后端没开抽 → 不谎报「正在重新分析」", () => {
  const outcome = summarizeUpload(
    [retyped("a", "textbook", "extracted")],
    new Map([["a", ""]]),
  );
  assert.deepEqual(outcome.retyped.map((s) => s.id), ["a"]);
  assert.doesNotMatch(outcome.toast, /重新分析/);
  assert.match(outcome.toast, /改用了新的文档类型/);
});

test("summarizeUpload: 类型没变的沿用条目仍走老文案，不算改类型", () => {
  const outcome = summarizeUpload(
    [retyped("a", "textbook", "extracted")],
    new Map([["a", "textbook"]]),
  );
  assert.deepEqual(outcome.retyped, []);
  assert.equal(
    outcome.toast,
    "1 个文件的内容已经在本笔记本里，沿用原有来源（名称保持原样），没有重复添加",
  );
});

test("summarizeUpload: 上传前不知道那条来源的类型时不猜，按纯沿用报", () => {
  const outcome = summarizeUpload([retyped("a", "textbook", "extracted")]);
  assert.deepEqual(outcome.retyped, []);
  assert.match(outcome.toast, /名称保持原样/);
});

test("summarizeUpload: 新建 + 纯沿用 + 改类型三者并存时分别如实报出", () => {
  const outcome = summarizeUpload(
    [src("a", false), retyped("b", "", "extracted"), retyped("c", "textbook", "extracting")],
    new Map([["b", ""], ["c", ""]]),
  );
  assert.deepEqual(outcome.added.map((s) => s.id), ["a"]);
  assert.deepEqual(outcome.reused.map((s) => s.id), ["b", "c"]);
  assert.deepEqual(outcome.retyped.map((s) => s.id), ["c"]);
  assert.equal(
    outcome.toast,
    "已上传 1 个来源；另有 1 个文件的内容已经在本笔记本里，沿用原有来源（名称保持原样），没有重复添加；" +
      "1 个文件的内容已经在本笔记本里，沿用原有来源并改用了新的文档类型，正在按新类型重新分析",
  );
});

// ------------------------------------------- 批内内容重复：同一 id 只留一张卡

test("summarizeUpload: 折叠去重后一个 id 只留一张卡（sources 可直接并进 state）", () => {
  // 一次上传里两个内容相同的文件：第 1 个新建（reused=false），第 2 个命中它刚建的
  // 行（同 id、reused=true）。直接铺进 state 会画出两张同 id 的卡片。
  const outcome = summarizeUpload([src("a", false), src("a", true), src("b", false)]);
  assert.deepEqual(outcome.sources.map((s) => s.id), ["a", "b"], "同 id 只留一条");
});

test("summarizeUpload: 批内自我重复只计一次新增，且不误报为「沿用既有」", () => {
  const outcome = summarizeUpload([src("a", false), src("a", true)]);
  assert.equal(outcome.added.length, 1, "新建一次就计一次，回声不该再 +1");
  assert.deepEqual(outcome.reused, [], "批内自我重复的回声不是「沿用本笔记本已有来源」");
  assert.equal(outcome.toast, "已上传 1 个来源");
});

test("summarizeUpload: 折叠取后出现的同 id 快照（第 2 个文件在刚建行上改的类型）", () => {
  // 同批两个内容相同、类型不同的文件：第 2 个在第 1 个刚建的行上把类型改成 textbook，
  // 折叠后 state 里的那张卡应反映最新（改后）的快照。
  const outcome = summarizeUpload([
    { id: "a", title: "a.pdf", reused: false, doc_type: "" },
    { id: "a", title: "a.pdf", reused: true, doc_type: "textbook" },
  ]);
  assert.deepEqual(outcome.sources.map((s) => s.doc_type), ["textbook"], "留最新快照");
  assert.equal(outcome.added.length, 1, "仍是本次真正新建的一条");
});

// ------------------------------------------- classifyStagedFiles：入列前逐文件分类

const CLASSIFY_OPTS = {
  supportedExtensions: ["pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm", "xls"],
  legacyOfficeExtensions: ["doc", "ppt"],
  maxBytes: 1024,
  supportedHint: "PDF / Word(.docx) / PPT(.pptx) / Excel(.xlsx,.xlsm,.xls) / Markdown / CSV",
};

test("classifyStagedFiles: 不支持的类型逐条给出原因，可上传的保序进 accepted", () => {
  const files = [
    { name: "a.pdf", size: 10 },
    { name: "b.txt", size: 10 },
    { name: "c.md", size: 10 },
    { name: "d", size: 10 }, // 无扩展名（拖入文件夹时的典型形态）
  ];
  const { accepted, skipped } = classifyStagedFiles(files, CLASSIFY_OPTS);
  assert.deepEqual(accepted.map((f) => f.name), ["a.pdf", "c.md"]);
  assert.deepEqual(skipped.map((f) => f.name), ["b.txt", "d"]);
  for (const item of skipped) {
    assert.match(item.reason, /不支持的文件类型/);
    assert.match(item.reason, /PDF \/ Word/, "原因里要有支持列表，用户才知道该给什么");
  }
});

test("classifyStagedFiles: 旧版 Office 给「另存为」引导而非笼统的类型不支持", () => {
  const { accepted, skipped } = classifyStagedFiles(
    [{ name: "legacy.doc", size: 10 }, { name: "deck.ppt", size: 10 }],
    CLASSIFY_OPTS,
  );
  assert.deepEqual(accepted, []);
  for (const item of skipped) assert.match(item.reason, /另存为 \.docx \/ \.pptx/);
});

test("classifyStagedFiles: 超过单文件上限的给出带上限的原因，等于上限可上传", () => {
  const { accepted, skipped } = classifyStagedFiles(
    [{ name: "fits.pdf", size: 1024 }, { name: "big.pdf", size: 1025 }],
    CLASSIFY_OPTS,
  );
  assert.deepEqual(accepted.map((f) => f.name), ["fits.pdf"]);
  assert.deepEqual(skipped.map((f) => f.name), ["big.pdf"]);
  assert.match(skipped[0].reason, /超过单个文件上限（1 KB）/);
});

test("classifyStagedFiles: maxBytes 未到达（null）时不做大小预判，交给服务端权威 413", () => {
  const { accepted, skipped } = classifyStagedFiles(
    [{ name: "big.pdf", size: 10 ** 9 }],
    { ...CLASSIFY_OPTS, maxBytes: null },
  );
  assert.deepEqual(accepted.map((f) => f.name), ["big.pdf"]);
  assert.deepEqual(skipped, []);
});

test("classifyStagedFiles: 扩展名大小写不敏感", () => {
  const { accepted, skipped } = classifyStagedFiles(
    [{ name: "UPPER.PDF", size: 10 }],
    CLASSIFY_OPTS,
  );
  assert.deepEqual(accepted.map((f) => f.name), ["UPPER.PDF"]);
  assert.deepEqual(skipped, []);
});

// ------------------------------------- classifyStagedFiles：.zip 原始上传

test("classifyStagedFiles: 后端注册表声明 zip 时按普通来源入列", () => {
  const { accepted, skipped, bundles } = classifyStagedFiles(
    [{ name: "notes.pdf", size: 10 }, { name: "bundle.zip", size: 10 }],
    { ...CLASSIFY_OPTS, supportedExtensions: [...CLASSIFY_OPTS.supportedExtensions, "zip"] },
  );
  assert.deepEqual(accepted.map((f) => f.name), ["notes.pdf", "bundle.zip"]);
  assert.deepEqual(skipped, []);
  assert.deepEqual(bundles, []);
});

test("classifyStagedFiles: .zip 大小写不敏感并受原始上传字节上限约束", () => {
  const { accepted, skipped, bundles } = classifyStagedFiles(
    [{ name: "HUGE.ZIP", size: 10 ** 9 }],
    { ...CLASSIFY_OPTS, supportedExtensions: [...CLASSIFY_OPTS.supportedExtensions, "zip"] },
  );
  assert.deepEqual(accepted, []);
  assert.equal(skipped.length, 1);
  assert.match(skipped[0].reason, /超过单个文件上限/);
  assert.deepEqual(bundles, []);
});

test("classifyStagedFiles: 后端未声明 zip 时给出可见的不支持原因", () => {
  const { accepted, skipped, bundles } = classifyStagedFiles(
    [{ name: "a.zip", size: 5 }, { name: "b.zip", size: 5 }],
    CLASSIFY_OPTS,
  );
  assert.deepEqual(accepted, []);
  assert.equal(skipped.length, 2);
  assert.deepEqual(bundles, []);
});
