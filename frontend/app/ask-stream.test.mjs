import test from "node:test";
import assert from "node:assert/strict";

import { takeNdjsonLines } from "./ask-stream.ts";

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
  const r = takeNdjsonLines('{"event":"started","job_id":"j1"}\n{"event":"progress"');
  assert.deepEqual(r.lines, ['{"event":"started","job_id":"j1"}']);
  assert.equal(r.remainder, '{"event":"progress"');
});

test("started/cancelled 事件可被 JSON.parse 出正确 tag", () => {
  assert.equal(JSON.parse('{"event":"started","job_id":"j1"}').event, "started");
  assert.equal(JSON.parse('{"event":"started","job_id":"j1"}').job_id, "j1");
  assert.equal(JSON.parse('{"event":"cancelled"}').event, "cancelled");
});
