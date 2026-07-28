import test from "node:test";
import assert from "node:assert/strict";

import { formatQuestionTime } from "./chat-question-time.ts";

test("question time grows from time to weekday, date, and year in local time", () => {
  const now = new Date(2026, 6, 29, 12, 0); // Wednesday, local time.

  assert.equal(formatQuestionTime(new Date(2026, 6, 29, 9, 5), now), "09:05");
  assert.equal(formatQuestionTime(new Date(2026, 6, 27, 9, 5), now), "星期一 09:05");
  assert.equal(formatQuestionTime(new Date(2026, 6, 20, 9, 5), now), "7月20日 09:05");
  assert.equal(formatQuestionTime(new Date(2025, 6, 20, 9, 5), now), "2025年7月20日 09:05");
});

test("question time treats Monday as the start of the browser-local week", () => {
  const sunday = new Date(2026, 7, 2, 12, 0);
  assert.equal(formatQuestionTime(new Date(2026, 6, 27, 8, 30), sunday), "星期一 08:30");
  assert.equal(formatQuestionTime(new Date(2026, 6, 26, 8, 30), sunday), "7月26日 08:30");
});

test("question time fails closed for a malformed timestamp", () => {
  assert.equal(formatQuestionTime("not-a-time", new Date(2026, 6, 29)), "");
});
