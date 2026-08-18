import assert from "node:assert/strict";
import test from "node:test";

import {
  batchFullReason,
  emptyStagedList,
  mergeStagedFiles,
  normalizeStagedList,
  stagedFileKey,
} from "../../app/staged-files.ts";

/** 合并只读 name/size，纯函数因此不需要真的造 File。 */
function item(name, size = 1) {
  return { name, size };
}

function merge(list, incoming, cap = null) {
  return mergeStagedFiles(list, incoming, { maxFilesPerBatch: cap });
}

test("emptyStagedList: 每次都是新对象，三条数组都空（共享同一个常量会被误改）", () => {
  const first = emptyStagedList();
  const second = emptyStagedList();
  assert.deepEqual(first, { files: [], docTypes: [], touched: [] });
  assert.notEqual(first, second);
  assert.notEqual(first.files, second.files);
});

test("mergeStagedFiles: 追加入列并让三条数组同步增长（新项默认自动检测、未表态）", () => {
  const result = merge(emptyStagedList(), [item("a.pdf"), item("b.md")]);
  assert.deepEqual(result.next.files.map((f) => f.name), ["a.pdf", "b.md"]);
  assert.deepEqual(result.next.docTypes, ["", ""]);
  assert.deepEqual(result.next.touched, [false, false]);
  assert.deepEqual(result.added.map((f) => f.name), ["a.pdf", "b.md"]);
  assert.deepEqual(result.skipped, []);
  assert.deepEqual(result.duplicates, []);
});

test("mergeStagedFiles: 追加不覆盖已有项，且保留每项已选的类型/表态位", () => {
  const current = {
    files: [item("a.pdf")],
    docTypes: ["paper"],
    touched: [true],
  };
  const result = merge(current, [item("b.md")]);
  assert.deepEqual(result.next.files.map((f) => f.name), ["a.pdf", "b.md"]);
  assert.deepEqual(result.next.docTypes, ["paper", ""]);
  assert.deepEqual(result.next.touched, [true, false]);
});

// ⚠ 这条钉的就是「一次选中 pdf + zip，zip 解完后 pdf 消失」那个静默丢失：zip 解包
// 跨了 await 才回来入列，它必须从**上一次合并的结果**继续，而不是从发起那一刻的
// 旧快照。纯函数层面就是「第二次合并要以 result.next 为基」——page.tsx 侧由
// stagedRef 保证喂进来的就是这个最新值（staged-files-guard 钉住那一半）。
test("mergeStagedFiles: 异步链接力入列时两批都在（pdf 先入列、zip 解出的 md 后入列）", () => {
  const first = merge(emptyStagedList(), [item("paper.pdf")]);
  const second = merge(first.next, [item("note.md")]);
  assert.deepEqual(second.next.files.map((f) => f.name), ["paper.pdf", "note.md"]);
  assert.equal(second.next.docTypes.length, 2);
  assert.equal(second.next.touched.length, 2);
  // 反向对照：若第二批仍从**空列表**起算（旧闭包形态），pdf 就没了。
  const stale = merge(emptyStagedList(), [item("note.md")]);
  assert.deepEqual(stale.next.files.map((f) => f.name), ["note.md"]);
});

test("mergeStagedFiles: 同名同大小去重，重复项进 duplicates 而不是被静默丢掉", () => {
  const current = merge(emptyStagedList(), [item("a.pdf", 10)]).next;
  const result = merge(current, [item("a.pdf", 10), item("a.pdf", 11)]);
  assert.deepEqual(result.added.map((f) => f.size), [11]);
  assert.deepEqual(result.duplicates.map((f) => f.size), [10]);
  assert.equal(result.next.files.length, 2);
  assert.equal(result.next.docTypes.length, 2);
});

test("mergeStagedFiles: 同一批里的重复项也只入列一次", () => {
  const result = merge(emptyStagedList(), [item("a.pdf", 5), item("a.pdf", 5)]);
  assert.equal(result.added.length, 1);
  assert.equal(result.duplicates.length, 1);
  assert.equal(result.next.files.length, 1);
});

test("mergeStagedFiles: 超过单次上传数量上限的逐条给出可读原因，不静默丢弃", () => {
  const result = merge(emptyStagedList(), [item("a.pdf"), item("b.pdf"), item("c.pdf")], 2);
  assert.deepEqual(result.added.map((f) => f.name), ["a.pdf", "b.pdf"]);
  assert.deepEqual(result.skipped, [{ name: "c.pdf", reason: batchFullReason(2) }]);
  assert.equal(result.next.files.length, 2);
  assert.match(batchFullReason(2), /2 个/);
});

test("mergeStagedFiles: 上限为 null（配置未到达）时不做数量预判", () => {
  const result = merge(emptyStagedList(), [item("a"), item("b"), item("c")], null);
  assert.equal(result.added.length, 3);
  assert.deepEqual(result.skipped, []);
});

test("mergeStagedFiles: 零新增时原样返回入参（调用方可按引用相等跳过一次 setState）", () => {
  const current = merge(emptyStagedList(), [item("a.pdf", 3)]).next;
  const result = merge(current, [item("a.pdf", 3)]);
  assert.equal(result.next, current);
  assert.deepEqual(result.added, []);
});

test("normalizeStagedList: 长度不齐的输入按 files 补齐/截断，等长不变量恒成立", () => {
  const fixed = normalizeStagedList({
    files: [item("a"), item("b"), item("c")],
    docTypes: ["paper"],
    touched: [true, false, true, true],
  });
  assert.deepEqual(fixed.docTypes, ["paper", "", ""]);
  assert.deepEqual(fixed.touched, [true, false, true]);
  // 已经等长时原样返回，不做多余复制
  const ok = { files: [item("a")], docTypes: [""], touched: [false] };
  assert.equal(normalizeStagedList(ok), ok);
});

test("mergeStagedFiles: 入参长度不齐也会被补齐，合并结果仍三条等长", () => {
  const result = merge({ files: [item("a")], docTypes: [], touched: [] }, [item("b")]);
  assert.equal(result.next.files.length, 2);
  assert.deepEqual(result.next.docTypes, ["", ""]);
  assert.deepEqual(result.next.touched, [false, false]);
});

test("stagedFileKey: 名字与大小共同构成身份（同名不同大小不算同一个文件）", () => {
  assert.equal(stagedFileKey(item("a.pdf", 1)), stagedFileKey(item("a.pdf", 1)));
  assert.notEqual(stagedFileKey(item("a.pdf", 1)), stagedFileKey(item("a.pdf", 2)));
  assert.notEqual(stagedFileKey(item("a.pdf", 1)), stagedFileKey(item("b.pdf", 1)));
});
