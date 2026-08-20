// 「我的回答偏好」弹窗纯逻辑单测(Agentic Memory P3, T9)。
//
// 钉的是三件不看渲染就能证明、而渲染测试又证明不牢的事:
//   ① 枚举→界面词映射全覆盖——每个封闭取值都必须有真实翻译，不能落回 label()
//      的中性兜底词(那意味着有个值漏译了)；
//   ② PATCH 请求体的三态构造——"没传"(unset)与"传 null"(clear)必须在
//      JSON.stringify 之后仍然可区分，这正是后端 model_fields_set 判据要求的
//      语义；
//   ③ 服务端 search_profile 文档的防御性解析对畸形输入 fail-open。
import test from "node:test";
import assert from "node:assert/strict";

import {
  ANSWER_DETAIL_OPTIONS,
  ANSWER_LANGUAGE_OPTIONS,
  ANSWER_SHAPE_OPTIONS,
  DOMAIN_TERM_MAX_CHARS,
  DOMAIN_TERMS_MAX,
  answerDetailLabel,
  answerLanguageLabel,
  answerShapeLabel,
  buildSearchProfilePatch,
  domainTermRejectionMessage,
  emptyDrafts,
  hasPendingChanges,
  parseSearchProfile,
  searchProfileOriginLabel,
  validateNewDomainTerm,
} from "../../app/search-profile-model.ts";

// --------------------------------------------------------------------------
// ① 映射全覆盖
// --------------------------------------------------------------------------

test("回答语言/组织方式/详略三张表覆盖各自的全部封闭取值", () => {
  const FALLBACK = "__no_such_value__";
  for (const value of ANSWER_LANGUAGE_OPTIONS) {
    assert.notEqual(answerLanguageLabel(value), FALLBACK, `answer_language 漏译:${value}`);
  }
  for (const value of ANSWER_SHAPE_OPTIONS) {
    assert.notEqual(answerShapeLabel(value), FALLBACK, `answer_shape 漏译:${value}`);
  }
  for (const value of ANSWER_DETAIL_OPTIONS) {
    assert.notEqual(answerDetailLabel(value), FALLBACK, `answer_detail 漏译:${value}`);
  }
});

test("来源标记覆盖 user/job 两值", () => {
  assert.equal(searchProfileOriginLabel("user"), "你设置的");
  assert.equal(searchProfileOriginLabel("job"), "自动判断");
});

test("未知枚举值退到中性兜底,不是原值本身", () => {
  assert.notEqual(answerLanguageLabel("unknown"), "unknown");
  assert.notEqual(answerShapeLabel("unknown"), "unknown");
  assert.notEqual(answerDetailLabel("unknown"), "unknown");
});

// --------------------------------------------------------------------------
// ② 没传 vs 传 null
// --------------------------------------------------------------------------

test("空草稿不产生任何待处理改动", () => {
  assert.equal(hasPendingChanges(emptyDrafts()), false);
  assert.deepEqual(buildSearchProfilePatch(emptyDrafts()), {});
});

test("unset 字段整体不出现在请求体里,clear 字段显式为 null,set 字段带具体值", () => {
  const drafts = {
    answer_language: { kind: "unset" },
    answer_shape: { kind: "clear" },
    answer_detail: { kind: "set", value: "concise" },
    domain_terms: { kind: "unset" },
  };
  assert.equal(hasPendingChanges(drafts), true);
  const body = buildSearchProfilePatch(drafts);

  assert.equal("answer_language" in body, false, "unset 字段不该出现在请求体对象里");
  assert.equal("domain_terms" in body, false);
  assert.equal(body.answer_shape, null);
  assert.equal(body.answer_detail, "concise");

  // 真正的契约是「JSON.stringify 之后」——JS 对象字面量的 undefined 属性值与
  // 缺失属性肉眼看着像，但只有序列化后的 key 集合才是打到网络上的真值。
  const wire = JSON.parse(JSON.stringify(body));
  assert.deepEqual(Object.keys(wire).sort(), ["answer_detail", "answer_shape"]);
  assert.equal(wire.answer_shape, null);
  assert.equal(wire.answer_detail, "concise");
});

test("显式选择 auto 是 set 而不是 unset——两者对后端语义不同", () => {
  const drafts = {
    ...emptyDrafts(),
    answer_language: { kind: "set", value: "auto" },
  };
  const body = buildSearchProfilePatch(drafts);
  assert.equal("answer_language" in body, true);
  assert.equal(body.answer_language, "auto");
  // 与「用户点了恢复自动」(clear → null)必须能区分开
  const clearedBody = buildSearchProfilePatch({
    ...emptyDrafts(),
    answer_language: { kind: "clear" },
  });
  assert.equal(clearedBody.answer_language, null);
  assert.notEqual(body.answer_language, clearedBody.answer_language);
});

test("domain_terms 的 set/clear 同样在请求体里可区分", () => {
  const setBody = buildSearchProfilePatch({
    ...emptyDrafts(),
    domain_terms: { kind: "set", value: ["PPA", "IRR"] },
  });
  assert.deepEqual(setBody.domain_terms, ["PPA", "IRR"]);

  const clearBody = buildSearchProfilePatch({
    ...emptyDrafts(),
    domain_terms: { kind: "clear" },
  });
  assert.equal(clearBody.domain_terms, null);
});

// --------------------------------------------------------------------------
// ③ 服务端文档防御性解析
// --------------------------------------------------------------------------

test("null/undefined/非对象一律 fail-open 到全部未设置", () => {
  for (const raw of [null, undefined, "oops", 42, [], true]) {
    const parsed = parseSearchProfile(raw);
    assert.deepEqual(parsed, {
      answer_language: null,
      answer_shape: null,
      answer_detail: null,
      domain_terms: null,
    });
  }
});

test("正常文档逐字段解析出 value + origin", () => {
  const parsed = parseSearchProfile({
    version: 1,
    fields: {
      answer_language: { value: "zh", origin: "user", updated_at: "2026-08-01T00:00:00+00:00" },
      answer_shape: { value: "table_first", origin: "job", updated_at: "2026-08-01T00:00:00+00:00" },
      domain_terms: { value: ["PPA"], origin: "user", updated_at: "2026-08-01T00:00:00+00:00" },
    },
  });
  assert.deepEqual(parsed.answer_language, { value: "zh", origin: "user" });
  assert.deepEqual(parsed.answer_shape, { value: "table_first", origin: "job" });
  assert.equal(parsed.answer_detail, null);
  assert.deepEqual(parsed.domain_terms, { value: ["PPA"], origin: "user" });
});

test("未知字段名、非法 origin、非法值域逐个字段丢弃,不拖垮其余字段", () => {
  const parsed = parseSearchProfile({
    version: 1,
    fields: {
      answer_language: { value: "fr", origin: "user", updated_at: "x" }, // 非法值域
      answer_shape: { value: "bullets", origin: "robot", updated_at: "x" }, // 非法 origin
      answer_detail: { value: "concise", origin: "user", updated_at: "x" }, // 合法
      unknown_field: { value: "whatever", origin: "user", updated_at: "x" }, // 未知字段
      domain_terms: { value: "not-an-array", origin: "user", updated_at: "x" }, // 非法类型
    },
  });
  assert.equal(parsed.answer_language, null);
  assert.equal(parsed.answer_shape, null);
  assert.deepEqual(parsed.answer_detail, { value: "concise", origin: "user" });
  assert.equal(parsed.domain_terms, null);
});

// --------------------------------------------------------------------------
// 术语标签输入校验
// --------------------------------------------------------------------------

test("术语校验:去空白、长度上限、去重、条数上限", () => {
  assert.deepEqual(validateNewDomainTerm([], "  "), { ok: false, reason: "empty" });
  assert.deepEqual(
    validateNewDomainTerm([], "x".repeat(DOMAIN_TERM_MAX_CHARS + 1)),
    { ok: false, reason: "too_long" },
  );
  assert.deepEqual(validateNewDomainTerm(["PPA"], "PPA"), { ok: false, reason: "duplicate" });
  assert.deepEqual(
    validateNewDomainTerm(Array.from({ length: DOMAIN_TERMS_MAX }, (_, i) => `t${i}`), "one-more"),
    { ok: false, reason: "limit_reached" },
  );
  assert.deepEqual(validateNewDomainTerm(["PPA"], "  IRR  "), { ok: true, term: "IRR" });
});

test("每种拒绝原因都有非空的中文提示,且不是兜底文案本身", () => {
  for (const reason of ["empty", "too_long", "duplicate", "limit_reached"]) {
    const message = domainTermRejectionMessage(reason);
    assert.ok(message.length > 0, `reason=${reason} 提示为空`);
    assert.notEqual(message, "无法添加这条术语", `reason=${reason} 落到了兜底文案`);
  }
});
