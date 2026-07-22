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
