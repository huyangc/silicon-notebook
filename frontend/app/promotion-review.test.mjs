import test from "node:test";
import assert from "node:assert/strict";

import { promotionReviewSections } from "./promotion-review.ts";

test("memory promotion review exposes every typed candidate and pinned evidence", () => {
  const sections = promotionReviewSections({
    source_kind: "memory",
    source_revision: 7,
    payload: {
      candidates: [
        { object_type: "concept", payload: { name: "PLL", definition: "A loop" } },
        { object_type: "claim", payload: { statement: "PLL locks" } },
        { object_type: "formula", payload: { expression: "f=1/T", variables: ["f", "T"] } },
        { object_type: "procedure", payload: { goal: "Lock", steps: ["Tune", "Verify"] } },
      ],
    },
    evidence: [
      { source_title: "Paper", location_label: "p. 3", quoted_span: "Validated quote" },
      { source_title: "Lab", location_label: "§2", quoted_span: "Second quote" },
    ],
  });

  assert.equal(sections.sourceRevision, 7);
  assert.deepEqual(sections.candidates, [
    { objectType: "concept", fields: [["名称", "PLL"], ["定义", "A loop"]] },
    { objectType: "claim", fields: [["陈述", "PLL locks"]] },
    { objectType: "formula", fields: [["表达式", "f=1/T"], ["变量", "f、T"]] },
    { objectType: "procedure", fields: [["目标", "Lock"], ["步骤", "1. Tune\n2. Verify"]] },
  ]);
  assert.equal(sections.evidence.length, 2);
  assert.equal(sections.evidence[1].quotedSpan, "Second quote");
});

test("review sections omit unknown fields and raw private context", () => {
  const sections = promotionReviewSections({
    source_kind: "memory",
    source_revision: 2,
    payload: {
      candidates: [{
        object_type: "claim",
        payload: { statement: "Safe", secret_task: "customer alpha", token: "hidden" },
      }],
      task_context: { customer: "alpha" },
    },
    evidence: [],
  });

  assert.equal(JSON.stringify(sections).includes("customer alpha"), false);
  assert.equal(JSON.stringify(sections).includes("hidden"), false);
  assert.equal(JSON.stringify(sections).includes("task_context"), false);
});

test("review sections keep the complete type-specific field contract", () => {
  const sections = promotionReviewSections({
    source_kind: "memory",
    source_revision: 3,
    payload: {
      candidates: [
        { object_type: "concept", payload: { name: "PLL" } },
        { object_type: "formula", payload: { expression: "f=1/T" } },
        { object_type: "procedure", payload: { steps: ["Tune"] } },
      ],
    },
    evidence: [],
  });

  assert.deepEqual(sections.candidates.map((item) => item.fields.map(([label]) => label)), [
    ["名称", "定义"],
    ["表达式", "变量"],
    ["目标", "步骤"],
  ]);
});
