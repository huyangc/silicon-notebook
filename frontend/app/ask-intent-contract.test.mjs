import assert from "node:assert/strict";
import test from "node:test";

import {
  callsIn,
  findFunction,
  jsxTextValues,
  parseModule,
} from "./test/semantic-source.mjs";


const page = await parseModule("page.tsx");
const review = await parseModule("ask-intent-review.tsx");


test("reasoning Ask previews intent before starting its durable stream", () => {
  const previewCalls = callsIn(findFunction(page, "runAsk"));
  const executeCalls = callsIn(findFunction(page, "executeAsk"));

  assert.ok(previewCalls.includes("previewAskIntent"));
  assert.ok(previewCalls.includes("buildAskIntentConfirmation"));
  assert.ok(executeCalls.includes("runAskStream"));
});


test("blocking reasoning ambiguity is visibly confirmed before retrieval", () => {
  const copy = jsxTextValues(review).join(" ");
  assert.match(copy, /只理解你的问题，不读取资料/);
  assert.match(copy, /先补充问题信息/);
  assert.match(copy, /确认后的问题/);
});
