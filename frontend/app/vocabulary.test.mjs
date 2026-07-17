import test from "node:test";
import assert from "node:assert/strict";
import { label, TIER, PARSE_STATUS, EVIDENCE_LEVEL, PROMOTION_STATUS } from "./vocabulary.ts";

test("label 命中时返回映射值", () => {
  assert.equal(label(TIER, "base", "未知"), "公共知识库");
  assert.equal(label(TIER, "personal", "未知"), "个人知识库");
});

test("label 未命中时返回 fallback，绝不返回原值", () => {
  assert.equal(label(TIER, "shadow_tier", "未知来源"), "未知来源");
  assert.notEqual(label(TIER, "shadow_tier", "未知来源"), "shadow_tier");
});

test("label 对空字符串与未定义值同样不泄漏原值", () => {
  assert.equal(label(PARSE_STATUS, "", "处理中"), "处理中");
  assert.equal(label(PARSE_STATUS, "totally_new_status", "处理中"), "处理中");
});

test("证据等级三个取值都有中文", () => {
  assert.equal(label(EVIDENCE_LEVEL, "grounded", "—"), "有据");
  assert.equal(label(EVIDENCE_LEVEL, "inferred", "—"), "推断");
  assert.equal(label(EVIDENCE_LEVEL, "overview", "—"), "概述");
});

test("label 不被原型链上的键名骗到", () => {
  // map[value] + 真值判断会让这些键命中 Object.prototype 并返回函数/对象。
  // TS 推成 string 但运行时不是,渲染进 JSX 就是 React 白屏。
  for (const key of ["constructor", "toString", "__proto__", "hasOwnProperty", "valueOf"]) {
    const out = label(TIER, key, "未知来源");
    assert.equal(out, "未知来源", `${key} 命中了原型链`);
    assert.equal(typeof out, "string", `${key} 返回了非字符串`);
  }
});

test("label 不把合法的空串翻译误判为未命中", () => {
  assert.equal(label({ silent: "" }, "silent", "兜底"), "");
});

test("晋升候选状态覆盖后端四个真实取值", () => {
  assert.equal(label(PROMOTION_STATUS, "proposed", "处理中"), "待审核");
  assert.equal(label(PROMOTION_STATUS, "under_review", "处理中"), "审核中");
  assert.equal(label(PROMOTION_STATUS, "approved", "处理中"), "已收录");
  assert.equal(label(PROMOTION_STATUS, "rejected", "处理中"), "未采纳");
});

test("解析状态含 metadata-only", () => {
  assert.equal(label(PARSE_STATUS, "metadata-only", "处理中"), "仅元数据");
});
