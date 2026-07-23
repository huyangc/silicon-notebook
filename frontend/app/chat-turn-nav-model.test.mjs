import test from "node:test";
import assert from "node:assert/strict";

import {
  activeTurnFromTops,
  chatTurnDomId,
  magnifyScales,
  nearestIndex,
} from "./chat-turn-nav-model.ts";

test("DOM id 稳定且按下标生成", () => {
  assert.equal(chatTurnDomId(0), "chat-turn-0");
  assert.equal(chatTurnDomId(7), "chat-turn-7");
});

test("高亮命中触发线以上最靠下的一轮", () => {
  // 触发线默认 140：top - 140 <= 0 视为已越线。
  assert.equal(activeTurnFromTops([-300, -80, 400, 900]), 1);
  assert.equal(activeTurnFromTops([-300, -80, 120, 900]), 2); // 120 <= 140 → 已越线
  assert.equal(activeTurnFromTops([50, 400, 900]), 0); // 仅第一条越线
});

test("尚无一轮越线时兜底为首轮", () => {
  assert.equal(activeTurnFromTops([200, 500]), 0);
});

test("触发线可自定义", () => {
  assert.equal(activeTurnFromTops([100, 130, 200], 140), 1);
  assert.equal(activeTurnFromTops([100, 130, 200], 90), 0);
});

test("缺失元素（Infinity）会中止扫描", () => {
  assert.equal(activeTurnFromTops([-10, Number.POSITIVE_INFINITY, -5]), 0);
});

test("空输入无害", () => {
  assert.equal(activeTurnFromTops([]), 0);
});

test("放大镜：指针离开时全部归 1", () => {
  assert.deepEqual(magnifyScales([0, 50, 100], null), [1, 1, 1]);
  assert.deepEqual(magnifyScales([0, 50, 100], Number.NaN), [1, 1, 1]);
});

test("放大镜：指针处峰值恰为 max，且向两侧对称衰减", () => {
  const s = magnifyScales([0, 50, 100], 50, { max: 2, sigma: 25 });
  assert.equal(Math.round(s[1] * 1000) / 1000, 2); // 正对指针 → 峰值 max
  assert.ok(s[0] < s[1] && s[2] < s[1]); // 两侧更小
  assert.ok(Math.abs(s[0] - s[2]) < 1e-9); // 等距 → 对称
  assert.ok(s[0] > 1); // 但仍被放大一些
});

test("放大镜：越远越接近 1", () => {
  const [near, far] = magnifyScales([10, 400], 10, { max: 3, sigma: 20 });
  assert.equal(near, 3);
  assert.ok(far < 1.001);
});

test("最近刻度：取距指针最近者", () => {
  assert.equal(nearestIndex([0, 50, 100], 60), 1);
  assert.equal(nearestIndex([0, 50, 100], 80), 2);
  assert.equal(nearestIndex([0, 50, 100], -20), 0);
});

test("最近刻度：并列取靠前，空数组取 -1", () => {
  assert.equal(nearestIndex([0, 100], 50), 0);
  assert.equal(nearestIndex([], 10), -1);
});
