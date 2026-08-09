import { test } from "node:test";
import assert from "node:assert/strict";
import { dayLabel, TODAY_VALUE } from "../../app/dev/logs/date.ts";

test("dayLabel maps sentinels", () => {
  assert.equal(dayLabel(TODAY_VALUE), "今天");
  assert.equal(dayLabel("legacy"), "历史(未分天)");
  assert.equal(dayLabel("2026-07-08"), "2026-07-08");
});
