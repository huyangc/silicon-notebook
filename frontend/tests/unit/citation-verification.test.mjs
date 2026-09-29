import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  citationCheckNotice,
  citationCheckReasonText,
  hasFailedCitationCheck,
  referenceVerification,
  verificationExplanation,
  verificationLabel,
  verificationMarkerName,
} from "../../app/citation-verification.ts";

const check = (counts) => ({
  outcome: "partial", checked: 6, failed: 0, changed: 0, source_gone: 0, unverifiable: 0, ...counts,
});

test("the notice lists only the reasons with a positive count, in a fixed order", () => {
  assert.equal(
    citationCheckNotice(check({ failed: 3, changed: 2, source_gone: 1 })),
    "本次回答有部分引用未通过核对：2 条原文已改动、1 条资料已删除。回答内容照常保留，带标记的引用可点开查看原因。",
  );
  assert.equal(citationCheckReasonText(check({ failed: 1, source_gone: 1 })), "1 条资料已删除");
  assert.equal(citationCheckReasonText(check({ failed: 2, unverifiable: 2 })), "2 条无法核对");
  assert.equal(
    citationCheckReasonText(check({ failed: 6, unverifiable: 1, changed: 4, source_gone: 1 })),
    "4 条原文已改动、1 条资料已删除、1 条无法核对",
  );
});

test("a summary without per-reason counts falls back to the total, never an empty colon", () => {
  assert.equal(citationCheckReasonText(check({ failed: 3 })), "共 3 条");
});

test("the public snapshot notice is in the past tense", () => {
  assert.equal(
    citationCheckNotice(check({ failed: 1, changed: 1 }), "snapshot"),
    "回答生成时，有部分引用未通过核对：1 条原文已改动。回答内容照常保留，带标记的引用可点开查看原因。",
  );
});

test("the notice appears only when failed > 0", () => {
  assert.equal(hasFailedCitationCheck(undefined), false);
  assert.equal(hasFailedCitationCheck(null), false);
  assert.equal(hasFailedCitationCheck(check({ failed: 0 })), false);
  assert.equal(hasFailedCitationCheck(check({ failed: 1, changed: 1 })), true);
});

test("absent verification means passed; the anchor wins over the citation", () => {
  assert.equal(referenceVerification({ anchor: {} }), "");
  assert.equal(referenceVerification({ citation: { verification: "" } }), "");
  assert.equal(referenceVerification({ anchor: { verification: "changed" } }), "changed");
  assert.equal(referenceVerification({ citation: { verification: "source_gone" } }), "source_gone");
  assert.equal(
    referenceVerification({ anchor: { verification: "unverifiable" }, citation: { verification: "changed" } }),
    "unverifiable",
  );
});

test("labels and the marker's accessible name", () => {
  assert.equal(verificationLabel("changed"), "原文已改动");
  assert.equal(verificationLabel("source_gone"), "资料已删除");
  assert.equal(verificationLabel("unverifiable"), "无法核对");
  assert.equal(verificationMarkerName("[2]", "source_gone"), "[2] 未通过核对：资料已删除");
});

test("the source_gone explanation also covers a surviving document whose cited passage is gone", () => {
  // 资料还在、被引的那段在运行中重新解析时没了,同样判 source_gone;那时文档仍能打开,
  // 所以解释句不能只说「资料已被删除」。标签是跨侧契约,保持原样。
  assert.equal(verificationExplanation("source_gone"), "这份资料或这段原文已被删除，下面是回答时引用的摘录。");
  assert.equal(verificationLabel("source_gone"), "资料已删除");
});

// One table shared with the backend twin (global_citation_check.citation_check_notice),
// which scripts/check_citation_verification_contract.py asserts against the same file.
const SHARED_CASES = JSON.parse(
  readFileSync(new URL("../../../backend/tests/fixtures/citation_check_notice_cases.json", import.meta.url), "utf8"),
).cases;

test("the notice matches the shared backend/frontend case table verbatim", () => {
  assert.ok(SHARED_CASES.length >= 8);
  for (const { name, check: summary, tense, notice } of SHARED_CASES) {
    const actual = hasFailedCitationCheck(summary) ? citationCheckNotice(summary, tense) : "";
    assert.equal(actual, notice, name);
  }
});
