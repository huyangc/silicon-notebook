import test from "node:test";
import assert from "node:assert/strict";

import {
  ALL_TIME_VALUE,
  LEGACY_VALUE,
  TODAY_VALUE,
  activityDayLabel,
  dayLabel,
  dayRange,
} from "../../app/dev/logs/date.ts";

// 边界带浏览器 UTC 偏移，所以断言必须在**固定时区**下做，否则同一份用例在 UTC 的 CI
// 与 UTC+8 的开发机上期望值不同。Node 支持运行时切换 process.env.TZ（实测 v22 生效），
// 用它把每条用例钉在一个具体时区上，跑完还原。
function withTZ(tz, run) {
  const previous = process.env.TZ;
  process.env.TZ = tz;
  try {
    run();
  } finally {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  }
}

test("空值在两个视图里是同一个值、两套说法", () => {
  assert.equal(ALL_TIME_VALUE, TODAY_VALUE);
  assert.equal(dayLabel(TODAY_VALUE), "今天");
  assert.equal(activityDayLabel(ALL_TIME_VALUE), "全部时间");
});

test("legacy 是日志文件的分区概念，活动视图沿用它的原标签但不产出区间", () => {
  assert.equal(dayLabel(LEGACY_VALUE), "历史(未分天)");
  assert.deepEqual(dayRange(LEGACY_VALUE), {});
});

// 这一条是 F1 的核心：边界必须**带浏览器 UTC 偏移**，代表「本地日历日的午夜」这个
// 绝对时刻。曾经拼裸串，而后端契约把裸串按 UTC 读，两侧因此错开一个本机偏移。
test("dayRange 产出半开区间，边界带浏览器 UTC 偏移（不是裸串）", () => {
  withTZ("Asia/Shanghai", () => {
    assert.deepEqual(dayRange("2026-08-04"), {
      since: "2026-08-04T00:00:00+08:00",
      until: "2026-08-05T00:00:00+08:00",
    });
  });
});

test("dayRange 的偏移随浏览器时区走，包括负偏移与半小时时区", () => {
  withTZ("UTC", () => {
    assert.deepEqual(dayRange("2026-08-04"), {
      since: "2026-08-04T00:00:00+00:00",
      until: "2026-08-05T00:00:00+00:00",
    });
  });
  withTZ("America/New_York", () => {
    assert.deepEqual(dayRange("2026-08-04"), {
      since: "2026-08-04T00:00:00-04:00",
      until: "2026-08-05T00:00:00-04:00",
    });
  });
  // UTC+05:30 —— 偏移的分钟位不是 0，拼串时按 60 取模而不是只写小时。
  withTZ("Asia/Kolkata", () => {
    assert.deepEqual(dayRange("2026-08-04"), {
      since: "2026-08-04T00:00:00+05:30",
      until: "2026-08-05T00:00:00+05:30",
    });
  });
});

// since 与 until 各自算偏移（而不是共用一个）。夏令时切换日两端偏移不同，共用会让
// 那一天多算/少算一小时。2026-03-08 是美东进入夏令时的那天（当地 02:00 拨到 03:00）。
test("dayRange 在夏令时切换日给两端各自的偏移", () => {
  withTZ("America/New_York", () => {
    assert.deepEqual(dayRange("2026-03-08"), {
      since: "2026-03-08T00:00:00-05:00",
      until: "2026-03-09T00:00:00-04:00",
    });
  });
});

test("dayRange 跨月进位", () => {
  withTZ("Asia/Shanghai", () => {
    assert.deepEqual(dayRange("2026-01-31"), {
      since: "2026-01-31T00:00:00+08:00",
      until: "2026-02-01T00:00:00+08:00",
    });
  });
});

test("dayRange 跨年进位", () => {
  withTZ("Asia/Shanghai", () => {
    assert.deepEqual(dayRange("2026-12-31"), {
      since: "2026-12-31T00:00:00+08:00",
      until: "2027-01-01T00:00:00+08:00",
    });
  });
});

test("dayRange 处理闰年 2 月末", () => {
  withTZ("Asia/Shanghai", () => {
    assert.deepEqual(dayRange("2028-02-28"), {
      since: "2028-02-28T00:00:00+08:00",
      until: "2028-02-29T00:00:00+08:00",
    });
    assert.deepEqual(dayRange("2028-02-29"), {
      since: "2028-02-29T00:00:00+08:00",
      until: "2028-03-01T00:00:00+08:00",
    });
  });
});

// 非闰年的 2 月 29 日会被 Date 悄悄顺延成 3 月 1 日;不做回环校验就会拼出一个
// since 指向不存在日期的区间。宁可不过滤,也不给一个错位的区间。
test("dayRange 拒绝不存在的日期，而不是让 Date 悄悄顺延", () => {
  assert.deepEqual(dayRange("2026-02-29"), {});
  assert.deepEqual(dayRange("2026-02-30"), {});
  assert.deepEqual(dayRange("2026-13-01"), {});
  // 两位数年份会被 Date 构造器映射到 1900+N（`new Date(99, 0, 1)` = 1999 年），
  // 回环校验同样要挡住它。
  assert.deepEqual(dayRange("0099-01-01"), {});
});

test("dayRange 对空值与任何非法形状都返回空区间（= 不过滤）", () => {
  assert.deepEqual(dayRange(ALL_TIME_VALUE), {});
  assert.deepEqual(dayRange("2026-8-4"), {});
  assert.deepEqual(dayRange("今天"), {});
  assert.deepEqual(dayRange("2026-08-04T10:00:00"), {});
});
