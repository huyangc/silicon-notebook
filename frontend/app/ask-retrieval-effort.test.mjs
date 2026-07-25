import assert from "node:assert/strict";
import test from "node:test";

import {
  ASK_RETRIEVAL_EFFORTS,
  DEFAULT_ASK_RETRIEVAL_EFFORT,
  STRUCTURED_ENUMERATION_LIMITS,
  retrievalEffortFromTurn,
  retrievalEffortThresholdText,
} from "./ask-retrieval-effort.ts";
import { callsIn, findFunction, parseModule } from "./test/semantic-source.mjs";


test("retrieval effort registry pins every ranked and complete-set threshold", () => {
  assert.deepEqual(ASK_RETRIEVAL_EFFORTS.map((item) => item.id), [
    "overview", "standard", "deep", "thorough", "exhaustive",
  ]);
  assert.equal(DEFAULT_ASK_RETRIEVAL_EFFORT, "standard");
  assert.deepEqual(
    ASK_RETRIEVAL_EFFORTS.map((item) => [
      item.ranked.perQuery,
      item.ranked.finalFloor,
      item.ranked.finalAspect,
      item.ranked.finalCap,
      item.ranked.maxSteps,
      item.ranked.maxSubqueries,
      item.ranked.kgContextChars,
      item.ranked.chunkContextChars,
    ]),
    [
      [4, 8, 2, 12, 4, 2, 4_000, 12_000],
      [8, 20, 3, 36, 8, 5, 6_000, 30_000],
      [8, 24, 4, 48, 16, 6, 8_000, 50_000],
      [12, 32, 5, 64, 32, 8, 12_000, 80_000],
      [16, 40, 6, 96, 50, 10, 16_000, 120_000],
    ],
  );
  assert.deepEqual(STRUCTURED_ENUMERATION_LIMITS, {
    pageRows: 25, maxPages: 50, maxRows: 1_250, maxTables: 8, maxColumns: 8,
    cellExcerptChars: 1_000, payloadChars: 256_000, inlineAnswerRows: 100,
    initialVisibleRows: 20, overflowReason: "explicit_partial",
  });
});


test("historical effort restores safely and the visible threshold copy includes enforced limits", () => {
  assert.equal(retrievalEffortFromTurn({ response: { retrieval_effort: "exhaustive" } }), "exhaustive");
  assert.equal(retrievalEffortFromTurn({ response: { retrieval_effort: "bad" } }), "standard");
  assert.equal(retrievalEffortFromTurn(undefined), "standard");
  const copy = retrievalEffortThresholdText("standard");
  for (const token of ["至少 20", "增加 3", "最多 36", "最多 8 个推理步骤", "5 条首轮子查询", "6000/30000", "25", "50", "1250", "8", "1000", "256000", "100", "20", "explicit_partial"]) {
    assert.ok(copy.includes(token), `missing threshold ${token}`);
  }
});


test("reasoning submit carries effort and opening a conversation restores it", async () => {
  const page = await parseModule("page.tsx");
  const execute = callsIn(findFunction(page, "executeAsk"));
  const applyDetail = callsIn(findFunction(page, "applySessionDetail"));
  assert.ok(execute.includes("runAskStream"));
  assert.ok(applyDetail.includes("retrievalEffortFromTurn"));
});
