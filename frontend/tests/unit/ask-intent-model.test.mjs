import test from "node:test";
import assert from "node:assert/strict";

import {
  buildAskIntentConfirmation,
  missingRequiredIntentAnswers,
} from "../../app/ask-intent-model.ts";


const contract = {
  objective: "分析一下这个问题",
  resolved_question: "分析电荷泵 PLL",
  intent_type: "diagnose",
  entities: ["电荷泵 PLL"],
  mandatory_topics: [],
  comparison_axes: [],
  constraints: [],
  excluded_topics: [],
  expected_output: "",
  assumptions: [],
  ambiguities: [{
    id: "ambiguity-input",
    question: "重点关注什么？",
    required: true,
    options: ["锁定时间", "抖动"],
  }],
  confidence: 0.5,
  needs_clarification: true,
  confirmed: false,
};


test("reasoning intent confirmation blocks missing required answers and preserves reviewed wording", () => {
  assert.equal(missingRequiredIntentAnswers(contract, {}), true);
  assert.equal(missingRequiredIntentAnswers(contract, { "ambiguity-input": "锁定时间" }), false);

  assert.deepEqual(buildAskIntentConfirmation(
    contract,
    "  分析电荷泵 PLL 的锁定时间  ",
    { "ambiguity-input": "  锁定时间  " },
  ), {
    contract,
    resolved_question: "分析电荷泵 PLL 的锁定时间",
    answers: [{ id: "ambiguity-input", answer: "锁定时间" }],
  });
});
