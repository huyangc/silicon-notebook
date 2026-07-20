import test from "node:test";
import assert from "node:assert/strict";

import { withoutDecidedMerge } from "./kg-merge-model.ts";
import {
  declarations,
  jsxElements,
  parseModule,
} from "./test/semantic-source.mjs";


test("KG sidebar composes structured evidence cards", async () => {
  const page = await parseModule("page.tsx");
  const functionNames = new Set(
    declarations(page)
      .filter((finding) => finding.kind === "function")
      .map((finding) => finding.name),
  );
  const evidenceCards = jsxElements(page, "article")
    .filter(({ attributes }) => (
      typeof attributes.className === "string"
      && attributes.className.includes("kg-evidence-card")
    ));

  assert.equal(functionNames.has("KgEvidenceCard"), true);
  assert.equal(functionNames.has("KgOccurrenceCard"), true);
  assert.ok(evidenceCards.length >= 3);
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
