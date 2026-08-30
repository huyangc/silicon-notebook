import test from "node:test";
import assert from "node:assert/strict";

import { canContinueKgBuild } from "../../app/kg-build-status.ts";
import { withoutDecidedMerge } from "../../app/kg-merge-model.ts";
import {
  declarations,
  jsxElements,
  parseModule,
} from "../../test-support/semantic-source.mjs";


test("KG sidebar composes structured evidence cards", async () => {
  // KgEvidenceCard (and its progressive-disclosure wrapper KgEvidenceList)
  // moved out of page.tsx into their own module (codex PR #639 R1 P2) so the
  // "show more evidence" reveal/reset behaviour is directly unit-testable —
  // see kg-evidence-list.component.test.tsx. KgOccurrenceCard stays in
  // page.tsx (it serves the non-concept-detail occurrence fallback path
  // only, no progressive disclosure).
  const page = await parseModule("page.tsx");
  const evidenceList = await parseModule("kg-evidence-list.tsx");
  const pageFunctionNames = new Set(
    declarations(page)
      .filter((finding) => finding.kind === "function")
      .map((finding) => finding.name),
  );
  const evidenceListFunctionNames = new Set(
    declarations(evidenceList)
      .filter((finding) => finding.kind === "function")
      .map((finding) => finding.name),
  );
  const evidenceCards = [
    ...jsxElements(page, "article"),
    ...jsxElements(evidenceList, "article"),
  ].filter(({ attributes }) => (
    typeof attributes.className === "string"
    && attributes.className.includes("kg-evidence-card")
  ));

  assert.equal(evidenceListFunctionNames.has("KgEvidenceCard"), true);
  assert.equal(evidenceListFunctionNames.has("KgEvidenceList"), true);
  assert.equal(pageFunctionNames.has("KgOccurrenceCard"), true);
  assert.ok(evidenceCards.length >= 2);
});


test("KG merge decisions remove id and duplicate-pair rows", () => {
  const decided = {
    id: "merge-1",
    canonical_a: "a",
    canonical_b: "b",
  };
  const remaining = withoutDecidedMerge(
    [
      decided,
      { id: "merge-2", canonical_a: "b", canonical_b: "a" },
      { id: "merge-3", canonical_a: "a", canonical_b: "c" },
    ],
    decided,
  );

  assert.deepEqual(remaining.map((item) => item.id), ["merge-3"]);
});

test("interrupted KG continuation stays hidden for read-only notebook members", () => {
  assert.equal(canContinueKgBuild("继续分析未完成内容", false, false), true);
  assert.equal(canContinueKgBuild("继续分析未完成内容", false, true), false);
  assert.equal(canContinueKgBuild("继续分析未完成内容", true, false), false);
  assert.equal(canContinueKgBuild(null, false, false), false);
});
