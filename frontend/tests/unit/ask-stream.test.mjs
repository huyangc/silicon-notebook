import test from "node:test";
import assert from "node:assert/strict";

import { takeNdjsonLines } from "../../app/ask-stream.ts";

test("takes complete NDJSON lines and keeps an incomplete remainder", () => {
  const parsed = takeNdjsonLines('{"event":"progress"}\n{"event":"final"');

  assert.deepEqual(parsed.lines, ['{"event":"progress"}']);
  assert.equal(parsed.remainder, '{"event":"final"');
});

test("ignores blank NDJSON lines", () => {
  const parsed = takeNdjsonLines('\n{"event":"progress"}\n\n');

  assert.deepEqual(parsed.lines, ['{"event":"progress"}']);
  assert.equal(parsed.remainder, "");
});

test("takeNdjsonLines 拆多行 + 保留残段", () => {
  const r = takeNdjsonLines('{"event":"started","job_id":"j1","conversation_id":"c1"}\n{"event":"progress"');
  assert.deepEqual(r.lines, ['{"event":"started","job_id":"j1","conversation_id":"c1"}']);
  assert.equal(r.remainder, '{"event":"progress"');
});

test("started/cancelled 事件可被 JSON.parse 出正确 tag", () => {
  const started = JSON.parse('{"event":"started","job_id":"j1","conversation_id":"c1"}');
  assert.equal(started.event, "started");
  assert.equal(started.job_id, "j1");
  assert.equal(started.conversation_id, "c1");
  assert.equal(JSON.parse('{"event":"cancelled"}').event, "cancelled");
});
