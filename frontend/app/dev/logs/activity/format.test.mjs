import test from "node:test";
import assert from "node:assert/strict";

import {
  activityKindLabel,
  activityStatusLabel,
  activityTitle,
  activityTone,
  mergeActivityPages,
} from "./format.ts";

function ask(overrides = {}) {
  return {
    type: "ask",
    id: "ask-1",
    notebook_id: "nb-1",
    created_at: "2026-08-01T00:00:00Z",
    asked_at: "2026-08-01T00:00:00Z",
    conversation_id: "conv-1",
    question: "这份合同的付款条款是什么？",
    mode: "reasoning",
    status: "done",
    answer_id: "ans-1",
    error: "",
    ...overrides,
  };
}

function source(overrides = {}) {
  return {
    type: "source",
    id: "src-1",
    notebook_id: "nb-1",
    created_at: "2026-08-01T00:00:00Z",
    display_title: "季度报告.pdf",
    file_name: "q3-report.pdf",
    source_type: "pdf",
    parse_status: "extracted",
    status: "extracted",
    error_message: "",
    extraction_warning: "",
    parse_quality_warning: false,
    paper_meta_status: "",
    ...overrides,
  };
}

function report(overrides = {}) {
  return {
    type: "report",
    id: "rep-1",
    notebook_id: "nb-1",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:10:00Z",
    question: "整理本季度的风险点",
    depth: 4,
    status: "done",
    generation_started_at: "2026-08-01T00:00:05Z",
    ...overrides,
  };
}

test("activityKindLabel maps the three types to 界面词", () => {
  assert.equal(activityKindLabel("ask"), "提问");
  assert.equal(activityKindLabel("source"), "来源");
  assert.equal(activityKindLabel("report"), "报告");
});

test("activityTitle: ask/report use question, source uses display_title", () => {
  assert.equal(activityTitle(ask()), "这份合同的付款条款是什么？");
  assert.equal(activityTitle(report()), "整理本季度的风险点");
  assert.equal(activityTitle(source()), "季度报告.pdf");
});

// B1:source_display_title()(backend/app/services/source_display.py)刻意让
// 空白 title 遮蔽 file_name、返回空串——这里必须原样反映那条规则,不能重造一条
// "空标题就换文件名"的相反回退,否则同一篇来源在引用卡上"无名"、在活动流里
// 却叫出文件名,变成两个名字。
test("activityTitle: source never falls back to file_name (mirrors source_display_title)", () => {
  assert.equal(
    activityTitle(source({ display_title: "", file_name: "q3-report.pdf" })),
    "（无标题）",
  );
  // 空白 title 同样"没有显示名"——不是"看起来没填、拿文件名顶上"。
  assert.equal(
    activityTitle(source({ display_title: "   ", file_name: "q3-report.pdf" })),
    "（无标题）",
  );
});

test("activityTitle: falls back to a placeholder when everything is empty", () => {
  assert.equal(activityTitle(ask({ question: "" })), "（无标题）");
  assert.equal(activityTitle(source({ display_title: "", file_name: "" })), "（无标题）");
  assert.equal(activityTitle(report({ question: "  " })), "（无标题）");
});

// A4:取消是用户主动行为不是失败,ask/report 两处都归 muted(灰),不显示成
// error(红)——否则混合流里两行都写"已取消"却一红一灰。
test("activityTone: ask — done=ok, failed=error, running/cancelled=muted", () => {
  assert.equal(activityTone(ask({ status: "done" })), "ok");
  assert.equal(activityTone(ask({ status: "failed" })), "error");
  assert.equal(activityTone(ask({ status: "cancelled" })), "muted");
  assert.equal(activityTone(ask({ status: "running" })), "muted");
});

// B2:真源 globals.css 的 `.status-*` 选择器组——绿色只属于 extracted,
// queued/parsing/parsed/extracting 这批"已经开始但还没完工"的中间态显示成
// warn(橙),uploaded/metadata-only 这批"还没真正开始处理"的初始态显示成
// muted(灰),不能把中间态判成绿色(那会与来源页签的橙色"处理中"矛盾)。
test("activityTone: source — extracted=ok, failed=error, mid-states=warn, initial-states=muted", () => {
  assert.equal(activityTone(source({ parse_status: "extracted" })), "ok");
  assert.equal(activityTone(source({ parse_status: "failed" })), "error");
  for (const status of ["queued", "parsing", "parsed", "extracting"]) {
    assert.equal(activityTone(source({ parse_status: status })), "warn", `${status} 应为 warn`);
  }
  for (const status of ["uploaded", "metadata-only"]) {
    assert.equal(activityTone(source({ parse_status: status })), "muted", `${status} 应为 muted`);
  }
});

// 钉住"绿色只属于 extracted"这条不变量本身,与上面按值枚举的用例互补:任何
// 已知非 extracted 状态都不应该是 ok。把 parsed(或其余任一个)误判回 ok
// 会让这条断言报红。
test("activityTone: source — ok is exclusive to extracted", () => {
  const nonExtracted = ["failed", "queued", "parsing", "parsed", "extracting", "uploaded", "metadata-only"];
  for (const status of nonExtracted) {
    assert.notEqual(activityTone(source({ parse_status: status })), "ok", `${status} 不应为 ok`);
  }
});

test("activityTone: source falls back to status when parse_status is empty", () => {
  assert.equal(activityTone(source({ parse_status: "", status: "failed" })), "error");
});

test("activityTone: report — done=ok, failed=error, rest=muted", () => {
  assert.equal(activityTone(report({ status: "done" })), "ok");
  assert.equal(activityTone(report({ status: "failed" })), "error");
  assert.equal(activityTone(report({ status: "cancelled" })), "muted");
  assert.equal(activityTone(report({ status: "generating" })), "muted");
  assert.equal(activityTone(report({ status: "outline_ready" })), "muted");
});

test("activityStatusLabel: reuses existing 中文 status wording per type (vocabulary.ts single source)", () => {
  assert.equal(activityStatusLabel(ask({ status: "done" })), "完成");
  assert.equal(activityStatusLabel(ask({ status: "running" })), "生成中");
  assert.equal(activityStatusLabel(ask({ status: "cancelled" })), "已取消");
  assert.equal(activityStatusLabel(source({ parse_status: "extracted" })), "已就绪");
  assert.equal(activityStatusLabel(source({ parse_status: "failed" })), "解析失败");
  assert.equal(activityStatusLabel(report({ status: "outline_ready" })), "待确认");
  assert.equal(activityStatusLabel(report({ status: "failed" })), "失败");
});

test("activityStatusLabel: unknown values fall back, never leak the raw value", () => {
  assert.equal(activityStatusLabel(ask({ status: "weird" })), "未知状态");
  assert.equal(activityStatusLabel(source({ parse_status: "weird" })), "未知状态");
  assert.equal(activityStatusLabel(report({ status: "weird" })), "未知状态");
});

test("mergeActivityPages: appends without reordering", () => {
  const prev = [ask({ id: "a", created_at: "2026-08-01T00:00:03Z" })];
  const next = [
    ask({ id: "b", created_at: "2026-08-01T00:00:02Z" }),
    ask({ id: "c", created_at: "2026-08-01T00:00:01Z" }),
  ];
  const merged = mergeActivityPages(prev, next);
  assert.deepEqual(merged.map((i) => i.id), ["a", "b", "c"]);
});

test("mergeActivityPages: dedupes by (created_at, id, type)", () => {
  const shared = ask({ id: "dup", created_at: "2026-08-01T00:00:00Z" });
  const prev = [shared, ask({ id: "keep-1", created_at: "2026-08-01T00:00:01Z" })];
  // 下一页首行与上一页末行重叠(典型的 keyset 分页重叠场景)。
  const next = [
    { ...shared },
    ask({ id: "keep-2", created_at: "2026-08-01T00:00:02Z" }),
  ];
  const merged = mergeActivityPages(prev, next);
  assert.deepEqual(merged.map((i) => i.id), ["dup", "keep-1", "keep-2"]);
});

test("mergeActivityPages: same id but different created_at is not deduped", () => {
  // (created_at, id, type) 是复合键——同 id 不同时间戳理论上不该同时出现,但键
  // 的定义必须是复合的,不能只按 id 去重(否则一旦服务端时间戳有误,会静默吞掉
  // 真实的行)。
  const prev = [ask({ id: "x", created_at: "2026-08-01T00:00:00Z" })];
  const next = [ask({ id: "x", created_at: "2026-08-01T00:00:05Z" })];
  const merged = mergeActivityPages(prev, next);
  assert.equal(merged.length, 2);
});

test("mergeActivityPages: same (created_at, id) but different type is not deduped", () => {
  // type 是复合键的第三维:ask_jobs/sources/reports 各自独立生成主键,不同表完全
  // 可能撞出相同的 (created_at, id)。
  const prev = [ask({ id: "shared-id", created_at: "2026-08-01T00:00:00Z" })];
  const next = [source({ id: "shared-id", created_at: "2026-08-01T00:00:00Z" })];
  const merged = mergeActivityPages(prev, next);
  assert.equal(merged.length, 2);
});

test("mergeActivityPages: works across mixed item types", () => {
  const prev = [ask({ id: "a1" })];
  const next = [source({ id: "s1" }), report({ id: "r1" })];
  const merged = mergeActivityPages(prev, next);
  assert.deepEqual(merged.map((i) => i.type), ["ask", "source", "report"]);
});
