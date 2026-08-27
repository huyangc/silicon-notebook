import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { ActivityStream } from "../../app/dev/logs/activity/ActivityStream";
import type { ActivityAsk, ActivityReport, ActivitySource } from "../../app/dev/logs/activity/types";

const NOW = new Date(2026, 7, 4, 12, 0); // 2026-08-04 12:00 本地

function ask(overrides: Partial<ActivityAsk> = {}): ActivityAsk {
  return {
    type: "ask",
    id: "ask-1",
    notebook_id: "nb-1",
    created_at: "2026-08-04T10:30:00",
    asked_at: "2026-08-04T10:29:00",
    conversation_id: "conv-1",
    question: "这份合同的付款条款是什么？",
    mode: "reasoning",
    status: "done",
    answer_id: "ans-1",
    error: "",
    ...overrides,
  };
}

function source(overrides: Partial<ActivitySource> = {}): ActivitySource {
  return {
    type: "source",
    id: "src-1",
    notebook_id: "nb-1",
    created_at: "2026-08-04T09:00:00",
    display_title: "季度报告.pdf",
    file_name: "q3-report.pdf",
    source_type: "pdf",
    parse_status: "extracted",
    status: "extracted",
    parse_failed: false,
    extraction_warning: "",
    parse_quality_warning: false,
    paper_meta_status: "",
    ...overrides,
  };
}

function report(overrides: Partial<ActivityReport> = {}): ActivityReport {
  return {
    type: "report",
    id: "rep-1",
    notebook_id: "nb-1",
    created_at: "2026-08-04T10:00:00",
    updated_at: "2026-08-04T10:20:00",
    question: "整理本季度的风险点",
    depth: 4,
    status: "done",
    generation_started_at: "2026-08-04T10:05:00",
    ...overrides,
  };
}

function stream(items: (ActivityAsk | ActivitySource | ActivityReport)[]) {
  return render(
    <ActivityStream
      hasMore={false}
      items={items}
      loading={false}
      now={NOW}
      onLoadMore={() => undefined}
      onSelect={() => undefined}
      selectedKey=""
    />,
  );
}


test("三类活动条目各自渲染类型、状态与标题", () => {
  const { container } = stream([ask(), source(), report()]);

  const rows = container.querySelectorAll(".activity-row");
  expect(rows).toHaveLength(3);

  // 提问:类型「提问」+ 状态「完成」+ 模式名 + 提问时间(浏览器本地格式)。
  expect(rows[0].textContent).toContain("提问");
  expect(rows[0].textContent).toContain("完成");
  expect(rows[0].textContent).toContain("逐步推理");
  expect(rows[0].textContent).toContain("这份合同的付款条款是什么？");
  expect(rows[0].textContent).toContain("10:29"); // asked_at 优先于 created_at

  // 来源:类型「来源」+ 解析状态界面词 + 显示名。
  expect(rows[1].textContent).toContain("来源");
  expect(rows[1].textContent).toContain("已就绪");
  expect(rows[1].textContent).toContain("季度报告.pdf");

  // 报告:类型「报告」+ 状态 + 研究深度档名 + 标题。
  expect(rows[2].textContent).toContain("报告");
  expect(rows[2].textContent).toContain("深入"); // depth=4
  expect(rows[2].textContent).toContain("整理本季度的风险点");
});


// `docs/product-and-api.md` 契约:来源异常小字唯一渲染路径是 AnomalyBadge + sourceAnomalies()。
// 断言按 class 判定而不是按文字——手搓一个写着同样文字的 <span> 不会带这些 class。
test("来源行的异常小字经 AnomalyBadge 渲染，并保持 integrity → retrieval → info 的顺序", () => {
  const { container } = stream([
    source({
      parse_status: "failed",
      status: "failed",
      parse_quality_warning: true,
      paper_meta_status: "missing",
    }),
  ]);

  const badges = container.querySelectorAll(".anomaly-badge");
  expect(badges).toHaveLength(3);
  expect(badges[0]).toHaveClass("anomaly-badge--integrity");
  expect(badges[0].textContent).toContain("解析失败");
  expect(badges[1]).toHaveClass("anomaly-badge--retrieval");
  expect(badges[1].textContent).toContain("降级解析");
  expect(badges[2]).toHaveClass("anomaly-badge--info");
  expect(badges[2].textContent).toContain("待补全");
});


test("无异常的来源行不挂任何异常小字", () => {
  const { container } = stream([source()]);
  expect(container.querySelectorAll(".anomaly-badge")).toHaveLength(0);
});


// ⚠ `docs/product-and-api.md` 契约:报告耗时只能是 generation_started_at → updated_at。旧报告缺
// 开始戳时**不编造耗时**——绝不能退回 updated_at - created_at,那会把用户确认大纲
// 的等待算进去。把实现改成 created_at→updated_at 时,下面这条会因为读到
// 「总耗时 20 分钟」而报红。
test("报告耗时缺 generation_started_at 时不显示耗时（不拿 created_at 顶替）", () => {
  const { container } = stream([
    report({ id: "rep-with", generation_started_at: "2026-08-04T10:05:00" }),
    report({ id: "rep-without", generation_started_at: "" }),
  ]);

  const rows = container.querySelectorAll(".activity-row");
  // 红线本身放在最前:没有开始戳时只报生成时刻,一个字的耗时都不能出现
  // (created_at→updated_at 是 20 分钟,退回那条算法这里就会读到它)。
  expect(rows[1].textContent).toContain("生成于");
  expect(rows[1].textContent).not.toContain("总耗时");
  expect(rows[1].textContent).not.toContain("20 分钟");
  // 正向对照:有开始戳时 10:05 → 10:20,15 分钟。缺了它,「干脆永远不显示耗时」
  // 这种把红线做过头的实现会从这里报红。
  expect(rows[0].textContent).toContain("总耗时 15 分钟");
});


test("未完成的报告只显示创建时间，不冒充已有最终耗时", () => {
  const { container } = stream([
    report({ status: "generating", generation_started_at: "2026-08-04T10:05:00" }),
  ]);
  const row = container.querySelector(".activity-row");
  expect(row?.textContent).toContain("创建于");
  expect(row?.textContent).not.toContain("总耗时");
});


test("认不出的问答模式不把内部代号上屏", () => {
  const { container } = stream([ask({ mode: "internal_only_mode" })]);
  expect(container.textContent).not.toContain("internal_only_mode");
});


test("空标题渲染占位符，不回落到文件名", () => {
  stream([source({ display_title: "   ", file_name: "q3-report.pdf" })]);
  expect(screen.getByText("（无标题）")).toBeInTheDocument();
  expect(screen.queryByText("q3-report.pdf")).not.toBeInTheDocument();
});
