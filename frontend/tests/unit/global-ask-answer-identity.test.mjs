import { test } from "node:test";
import assert from "node:assert/strict";

process.env.NEXT_PUBLIC_API_BASE_URL = "http://api.example/api";
const { withAnswerIdentity } = await import("../../app/global-ask-api.ts");

// 全局回答不落 answers 表,较早完成的作业存下来的 answer_id 是空串。共享的 AnswerView
// 只在 answer_id 非空时才给「分享」,也靠它按回答重置引用小卡片——读入口统一补成 job_id。
test("an answer without an id takes its job's id", () => {
  const job = { job_id: "gask-1", answer: { answer_id: "", answer: "正文" } };
  const fixed = withAnswerIdentity(job);
  assert.equal(fixed.answer.answer_id, "gask-1");
  assert.equal(job.answer.answer_id, "", "the input job is not mutated");
});

test("an existing id and answer-less jobs pass through untouched", () => {
  const stamped = { job_id: "gask-2", answer: { answer_id: "gask-2", answer: "正文" } };
  assert.equal(withAnswerIdentity(stamped), stamped);
  const running = { job_id: "gask-3", status: "running" };
  assert.equal(withAnswerIdentity(running), running);
  const legacy = { job_id: "gask-4", response: { answer_id: "old", answer: "旧形状" } };
  assert.equal(withAnswerIdentity(legacy), legacy);
});
