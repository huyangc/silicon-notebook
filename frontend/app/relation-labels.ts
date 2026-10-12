// 图谱关系的界面名：边类型与关系审核状态 → 中文。
// 知识图谱视图（kg-graph-view.tsx）与关系审核队列弹窗（edge-review-modal.tsx）共用这一份，
// 两处画的是同一批关系，名字不能各说各的。

import type { EdgeReviewStatus } from "./edge-review-queue.ts";
import { label } from "./vocabulary.ts";

// 图谱边类型 → 中文。取值真源:prompts.py 列出的 edge_type 词表(supports /
// depends_on / contrasts_with / about / defines / used_in / composed_of / mixed,
// 外加可传递的 derived_from / kind_of / prerequisite_of / precedes / part_of)。
// 此前有 8 个值只是把英文 id 抄了一遍(about: "about"),另有 5 个值压根没进表、
// 靠 `?? edge_type` 直接把英文渲染给用户——两条路都是英文外泄,一并补齐。
const RELATION_LABELS: Record<string, string> = {
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
  mixed: "多种关联"
};

/**
 * 边类型的界面名。未映射时退到中性的「关联」,**绝不回落成 edge_type 原值**——
 * 后端每加一个边类型,`RELATION_LABELS[t] ?? t` 那种写法都会把英文 id 直接画到
 * 图上(used_in / mixed 等 5 个值就是这么泄出去的)。label() 强制传兜底词,并在
 * 开发期把未映射的值 console.error 出来,让新值被发现而不是被静默渲染。
 */
export function relationLabel(edgeType: string): string {
  return label(RELATION_LABELS, edgeType, "关联");
}

// 关系审核状态 → 中文。取值真源:edge-review-queue.ts 的 EdgeReviewStatus
// (库默认 pending)。按联合类型键入,后端加了取值而这里没补时 tsc 会红。
const EDGE_REVIEW_STATUS_LABELS: Record<EdgeReviewStatus, string> = {
  pending: "待审核",
  verified: "已确认",
  rejected: "已拒绝",
};

/**
 * 关系审核状态的界面名。未映射时退到中性的「未知状态」——不用「待审核」:
 * 把认不出的状态说成待审,等于对用户谎报这条关系还没人看过。
 */
export function edgeReviewStatusLabel(status: string): string {
  return label(EDGE_REVIEW_STATUS_LABELS, status, "未知状态");
}
