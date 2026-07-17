import test from "node:test";
import assert from "node:assert/strict";
import { label, TIER, PARSE_STATUS, EVIDENCE_LEVEL } from "./vocabulary.ts";

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
