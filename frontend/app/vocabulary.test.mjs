import test from "node:test";
import assert from "node:assert/strict";
import { label, TIER, PARSE_STATUS, ELEMENT_TYPE, EVIDENCE_LEVEL, PROMOTION_STATUS, KNOWLEDGE_STATUS, SEVERITY, MODEL_SERVICE_STATUS_ERROR } from "./vocabulary.ts";
import { KNOWLEDGE_STATUS_OPTIONS } from "./workspace-model.ts";

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

// 注意:不要写成 `for (const v of ["uploaded","queued",...])` 去循环断言「都有中文」——
// 那个字面量数组就是从 PARSE_STATUS 自己的 key 抄来的,等于在断言「表里有表里已有的
// 东西」,恒真,验证不了真实完整性。前端没有 parse_status / element_type 的独立锚点
// (对比:KNOWLEDGE_STATUS 有 workspace-model.ts 的 KNOWLEDGE_STATUS_OPTIONS 可锚),
// 所以这里只断言「行为安全」:已知值译对、未知值退到中性兜底而非泄漏原值。
// 真正的「映射表覆盖后端全部取值」需要一个跨栈守卫(照 check_ask_modes_contract.py),
// 归 PR B。

// PARSE_STATUS 已由本文件(Task 1)覆盖——这里不再重复。
// ELEMENT_TYPE 是 Task 1 没测过的，属真新覆盖，加这一条：
test("内容块类型:已知值译对，未知值退中性兜底", () => {
  assert.equal(label(ELEMENT_TYPE, "table", "内容"), "表格");
  assert.equal(label(ELEMENT_TYPE, "some_future_block", "内容"), "内容");
});

// KNOWLEDGE_STATUS 是本 PR 唯一一处有独立锚点的枚举：workspace-model.ts 的
// KNOWLEDGE_STATUS_OPTIONS 是后端知识条目状态的真实取值来源(与 KNOWLEDGE_STATUS
// 映射表是两份独立维护的列表)。从这个锚点取值断言，才能真的发现「映射表漏了
// 后端某个取值」，而不是像上面 PARSE_STATUS/ELEMENT_TYPE 那样只能断言行为安全。
test("KNOWLEDGE_STATUS 覆盖 workspace-model 里的每一个取值", () => {
  // 必须 Object.hasOwn 直接查表——不能写 assert.notEqual(label(MAP,v,"其他"), v):
  // label 未命中返回「其他」(≠v)恒真,漏了 key 也发现不了(Task 5 评审揪出的空转)。
  for (const v of KNOWLEDGE_STATUS_OPTIONS) {
    assert.ok(Object.hasOwn(KNOWLEDGE_STATUS, v), `${v} 未映射,会退到兜底词`);
  }
});

test("SEVERITY 三个取值都有中文(真源 extraction_profiles.py:28)", () => {
  assert.equal(label(SEVERITY, "high", "—"), "高");
  assert.equal(label(SEVERITY, "medium", "—"), "中");
  assert.equal(label(SEVERITY, "low", "—"), "低");
  assert.notEqual(label(SEVERITY, "high", "—"), "high");
});


test("模型服务状态 code 使用稳定中文，不展示原始诊断", () => {
  assert.equal(label(MODEL_SERVICE_STATUS_ERROR, "upstream_error", "连接未通过"), "连接未通过");
  assert.equal(label(MODEL_SERVICE_STATUS_ERROR, "model_queue_timeout", "连接未通过"), "排队等待超时");
  assert.equal(label(MODEL_SERVICE_STATUS_ERROR, "provider_rate_limited", "连接未通过"), "上游服务限流");
  for (const value of Object.values(MODEL_SERVICE_STATUS_ERROR)) {
    assert.ok(!/base_url|api_key|provider_|model_/i.test(value), `文案泄漏协议字段名：${value}`);
  }
  assert.equal(label(MODEL_SERVICE_STATUS_ERROR, "future_code", "连接未通过"), "连接未通过");
  assert.notEqual(label(MODEL_SERVICE_STATUS_ERROR, "future_code", "连接未通过"), "future_code");
});
