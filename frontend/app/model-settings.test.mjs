import test from "node:test";
import assert from "node:assert/strict";
import { buildPutPayload, MODEL_ROLES } from "./model-settings.ts";

test("buildPutPayload omits api_key when not dirty", () => {
  const forms = Object.fromEntries(MODEL_ROLES.map((r) => [r,
    { base_url: " https://u/v1 ", model: " m ", api_key: "x", keyDirty: false }]));
  const p = buildPutPayload(forms);
  assert.equal(p.llm.base_url, "https://u/v1");
  assert.equal(p.llm.model, "m");
  assert.equal("api_key" in p.llm, false);
});

test("buildPutPayload includes api_key (even empty) when dirty", () => {
  const forms = Object.fromEntries(MODEL_ROLES.map((r) => [r,
    { base_url: "", model: "", api_key: "", keyDirty: true }]));
  assert.equal(buildPutPayload(forms).llm.api_key, "");
});
