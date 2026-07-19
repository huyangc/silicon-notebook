import test from "node:test";
import assert from "node:assert/strict";

import {
  defaultNotebookPayload,
  namedNotebookPayload,
  normalizedNotebookName,
} from "./notebook-creation.ts";
import { DEFAULT_NOTEBOOK_NAME } from "./workspace-model.ts";


test("default notebook name remains the persisted product contract", () => {
  assert.equal(DEFAULT_NOTEBOOK_NAME, "Untitled notebook");
  assert.deepEqual(defaultNotebookPayload(), {
    name: DEFAULT_NOTEBOOK_NAME,
    purpose: "",
  });
});


test("create payloads normalize blank names through the same contract", () => {
  assert.deepEqual(namedNotebookPayload("  ", " purpose "), {
    name: DEFAULT_NOTEBOOK_NAME,
    purpose: "purpose",
  });
  assert.deepEqual(namedNotebookPayload("  Analog  ", "  filters "), {
    name: "Analog",
    purpose: "filters",
  });
});


test("inline rename uses the same blank-name fallback", () => {
  assert.equal(normalizedNotebookName("  "), DEFAULT_NOTEBOOK_NAME);
  assert.equal(normalizedNotebookName("  Notebook A "), "Notebook A");
});
