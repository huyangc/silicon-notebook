import test from "node:test";
import assert from "node:assert/strict";

import {
  formatReportDuration,
  formatReportTime,
  formatReportTiming,
} from "../../app/report-time.ts";

test("report time includes an exact browser-local timestamp and relative age", () => {
  const now = new Date(2026, 7, 1, 16, 30);

  assert.equal(
    formatReportTime(new Date(2026, 7, 1, 14, 5), now),
    "2026年8月1日 14:05 · 2 小时前",
  );
  assert.equal(
    formatReportTime(new Date(2026, 6, 1, 16, 30), now),
    "2026年7月1日 16:30 · 1 个月前",
  );
});

test("report time fails closed for malformed timestamps", () => {
  assert.equal(formatReportTime("not-a-time", new Date(2026, 7, 1)), "");
});

test("completed report timing uses completion time and includes total duration", () => {
  const now = new Date(2026, 7, 1, 16, 30);

  assert.equal(
    formatReportTiming(
      "done",
      new Date(2026, 7, 1, 12, 0).toISOString(),
      new Date(2026, 7, 1, 14, 5).toISOString(),
      new Date(2026, 7, 1, 12, 0).toISOString(),
      now,
    ),
    "生成于 2026年8月1日 14:05 · 2 小时前 · 总耗时 2 小时 5 分钟",
  );
});

test("unfinished reports show creation time without claiming a total duration", () => {
  const now = new Date(2026, 7, 1, 16, 30);
  assert.equal(
    formatReportTiming(
      "generating",
      new Date(2026, 7, 1, 14, 5).toISOString(),
      new Date(2026, 7, 1, 16, 0).toISOString(),
      new Date(2026, 7, 1, 14, 6).toISOString(),
      now,
    ),
    "创建于 2026年8月1日 14:05 · 2 小时前",
  );
});

test("report duration is compact across minute, hour, and day ranges", () => {
  const start = new Date(2026, 7, 1, 10, 0);
  assert.equal(formatReportDuration(start, new Date(2026, 7, 1, 10, 0, 20)), "少于 1 分钟");
  assert.equal(formatReportDuration(start, new Date(2026, 7, 1, 12, 0)), "2 小时");
  assert.equal(formatReportDuration(start, new Date(2026, 7, 2, 13, 15)), "1 天 3 小时 15 分钟");
  assert.equal(formatReportDuration("bad", new Date()), "");
});
