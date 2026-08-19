import test from "node:test";
import assert from "node:assert/strict";

import { sourceKgBadge } from "../../app/source-kg-badge.ts";

test("有知识对象 → 已分析", () => {
  const badge = sourceKgBadge({ kg_extracted: true });
  assert.equal(badge.state, "analyzed");
  assert.equal(badge.label, "已分析");
  assert.ok(badge.className.includes("source-kg-badge--in"));
});

test("分析跑完但零产出 → 第三态,不是待分析", () => {
  const badge = sourceKgBadge({ kg_extracted: false, kg_analyzed_empty: true });
  assert.equal(badge.state, "analyzed_empty");
  assert.notEqual(badge.label, "待分析");
  // 现场就是这一条:这批来源此前永远显示「待分析」,看板计数因此永远降不下来。
  assert.ok(badge.label.startsWith("已分析"));
});

test("零产出态不借用「已入库」的绿色实心样式", () => {
  // 没有任何知识对象进图,用 --in 就是在说一件没发生的事。
  const badge = sourceKgBadge({ kg_analyzed_empty: true });
  assert.ok(!badge.className.includes("source-kg-badge--in"));
  assert.ok(badge.className.includes("source-kg-badge--empty"));
});

test("零产出态的解释必须真的能弹出来", () => {
  // .source-kg-badge 有 pointer-events:none(浏览器会直接跳过 title tooltip),
  // 所以这一态必须带上自己那个 modifier —— 它在 globals.css 里把 pointer-events
  // 重新打开。没有它,下面这段解释一个字也到不了用户眼前。
  const badge = sourceKgBadge({ kg_analyzed_empty: true });
  assert.ok(badge.className.includes("source-kg-badge--empty"));
  assert.match(badge.title, /没有可整理成知识图谱的内容/);
  assert.match(badge.title, /重新解析/);
});

test("两个字段都为假 → 待分析(未改变的既有行为)", () => {
  const badge = sourceKgBadge({ kg_extracted: false, kg_analyzed_empty: false });
  assert.equal(badge.state, "pending");
  assert.equal(badge.label, "待分析");
  assert.equal(badge.className, "source-kg-badge");
});

test("旧后端不发新字段时逐字回到原来的两态", () => {
  assert.equal(sourceKgBadge({}).state, "pending");
  assert.equal(sourceKgBadge({ kg_extracted: true }).state, "analyzed");
});

test("矛盾组合显示更强的那个事实", () => {
  // 两者本该互斥。真收到矛盾组合(旧行 + 新字段回填)时,「确实有知识对象」是更强
  // 的事实,不能因为另一个布尔为真就把它降级成「无知识」。
  assert.equal(
    sourceKgBadge({ kg_extracted: true, kg_analyzed_empty: true }).state,
    "analyzed",
  );
});
