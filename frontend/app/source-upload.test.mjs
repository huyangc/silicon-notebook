import test from "node:test";
import assert from "node:assert/strict";

import { summarizeUpload } from "./source-upload.ts";

const src = (id, reused) => ({ id, title: `${id}.pdf`, ...(reused === undefined ? {} : { reused }) });

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
    "1 个文件的内容已经在本笔记本里，沿用原有来源并改用了新的文档类型，正在按新类型重新抽取知识",
  );
});

test("summarizeUpload: 类型改了但后端没开抽 → 不谎报「正在重新抽取」", () => {
  const outcome = summarizeUpload(
    [retyped("a", "textbook", "extracted")],
    new Map([["a", ""]]),
  );
  assert.deepEqual(outcome.retyped.map((s) => s.id), ["a"]);
  assert.doesNotMatch(outcome.toast, /重新抽取/);
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
      "1 个文件的内容已经在本笔记本里，沿用原有来源并改用了新的文档类型，正在按新类型重新抽取知识",
  );
});
