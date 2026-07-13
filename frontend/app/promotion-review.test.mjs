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
        { object_type: "claim", payload: { name: "Lock claim", statement: "PLL locks" } },
        { object_type: "formula", payload: { name: "Period formula", expression: "f=1/T" } },
        { object_type: "procedure", payload: { name: "Lock procedure", steps: ["Tune", "Verify"] } },
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
    { objectType: "claim", fields: [["名称", "Lock claim"], ["陈述", "PLL locks"]] },
    { objectType: "formula", fields: [["名称", "Period formula"], ["表达式", "f=1/T"]] },
    { objectType: "procedure", fields: [["名称", "Lock procedure"], ["步骤", "1. Tune\n2. Verify"]] },
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

test("every production candidate payload key is present in the review projection", () => {
  const productionCandidates = [
    { object_type: "concept", payload: { name: "PLL", definition: "A loop" } },
    { object_type: "claim", payload: { name: "Claim", statement: "It locks" } },
    { object_type: "formula", payload: { name: "Formula", expression: "f=1/T" } },
    { object_type: "procedure", payload: { name: "Procedure", steps: ["Tune"] } },
  ];
  const sections = promotionReviewSections({
    source_kind: "memory",
    source_revision: 3,
    payload: { candidates: productionCandidates },
    evidence: [],
  });

  const labelForKey = {
    name: "名称",
    definition: "定义",
    statement: "陈述",
    expression: "表达式",
    steps: "步骤",
  };
  productionCandidates.forEach((candidate, index) => {
    const projectedLabels = sections.candidates[index].fields.map(([label]) => label);
    assert.deepEqual(
      projectedLabels,
      Object.keys(candidate.payload).map((key) => labelForKey[key]),
      `${candidate.object_type} omitted a payload field that will enter Base KG`,
    );
  });
});
