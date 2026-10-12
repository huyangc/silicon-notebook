// relation-labels.ts:知识图谱视图与关系审核弹窗共用的关系类型 / 审核状态界面名。
// 逐个钉住每个已知取值的中文——删掉或改坏任一条(例如 mixed)都会红;未知值退中性词。
import test from "node:test";
import assert from "node:assert/strict";

import { edgeReviewStatusLabel, relationLabel } from "../../app/relation-labels.ts";

test("每个已知边类型都译成中文", () => {
  const expected = {
    related_concepts: "关联概念",
    related_claims: "关联论断",
    related_formulas: "关联公式",
    related_procedures: "关联过程",
    about: "关于",
    defines: "定义",
    supports: "支持",
    depends_on: "依赖",
    composed_of: "包含",
    part_of: "属于",
    precedes: "先于",
    contrasts_with: "对比",
    used_in: "用于",
    derived_from: "推导自",
    kind_of: "是一种",
    prerequisite_of: "前置于",
    mixed: "多种关联",
  };
  for (const [edgeType, chinese] of Object.entries(expected)) {
    assert.equal(relationLabel(edgeType), chinese, edgeType);
  }
});

test("审核状态三个取值都译成中文", () => {
  assert.equal(edgeReviewStatusLabel("pending"), "待审核");
  assert.equal(edgeReviewStatusLabel("verified"), "已确认");
  assert.equal(edgeReviewStatusLabel("rejected"), "已拒绝");
});

test("未知值退到中性词，不回落成原值，也不把未知状态说成待审核", (t) => {
  t.mock.method(console, "error", () => undefined);
  assert.equal(relationLabel("some_future_edge"), "关联");
  assert.equal(edgeReviewStatusLabel("escalated"), "未知状态");
  assert.equal(relationLabel("toString"), "关联");
});
