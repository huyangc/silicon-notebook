// 活动详情写出提问 / 报告所在的笔记本：详情到达后以详情的库名为准；删除后的留存由
// 留存说明写出原库名、不再重复一行；全局问答只列「参与笔记本」。
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { ActivityDetail } from "../../app/dev/logs/activity/ActivityDetail";
import type {
  ActivityAsk,
  ActivityItem,
  ActivityReport,
  AskDetail,
  ReportDetail,
} from "../../app/dev/logs/activity/types";

const NOW = new Date(2026, 9, 21, 12, 0);

function askItem(overrides: Partial<ActivityAsk> = {}): ActivityAsk {
  return {
    type: "ask",
    id: "ask-1",
    notebook_id: "nb-1",
    created_at: "2026-10-21T10:30:00+00:00",
    asked_at: "2026-10-21T10:29:00+00:00",
    conversation_id: "conv-1",
    question: "这颗芯片的功耗是多少？",
    mode: "chunk",
    status: "done",
    answer_id: "",
    error: "",
    submitted_via: "",
    notebook_name: "列表里的库名",
    ...overrides,
  };
}

function askDetail(overrides: Partial<AskDetail> = {}): AskDetail {
  return {
    job_id: "ask-1",
    notebook_id: "nb-1",
    conversation_id: "conv-1",
    question: "这颗芯片的功耗是多少？",
    mode: "chunk",
    status: "done",
    asked_at: "2026-10-21T10:29:00+00:00",
    answered_at: "",
    error: "",
    trace: [],
    answer: null,
    notebook_name: "详情里的库名",
    ...overrides,
  };
}

function reportItem(overrides: Partial<ActivityReport> = {}): ActivityReport {
  return {
    type: "report",
    id: "rep-1",
    notebook_id: "nb-1",
    created_at: "2026-10-21T10:00:00+00:00",
    updated_at: "2026-10-21T10:20:00+00:00",
    question: "整理选型风险",
    depth: 2,
    status: "done",
    generation_started_at: "",
    submitted_via: "",
    notebook_name: "列表里的库名",
    ...overrides,
  };
}

function reportDetail(overrides: Partial<ReportDetail> = {}): ReportDetail {
  return {
    report_id: "rep-1",
    notebook_id: "nb-1",
    question: "整理选型风险",
    depth: 2,
    status: "done",
    created_at: "2026-10-21T10:00:00+00:00",
    updated_at: "2026-10-21T10:20:00+00:00",
    generation_started_at: "",
    error: "",
    content_md: "",
    references: [],
    notebook_name: "详情里的库名",
    ...overrides,
  };
}

function detailPane(
  item: ActivityItem,
  { ask = null, report = null }: { ask?: AskDetail | null; report?: ReportDetail | null } = {},
) {
  return render(
    <ActivityDetail
      askDetail={ask}
      askDetailError=""
      askDetailLoading={false}
      item={item}
      notebookNames={{}}
      now={NOW}
      reportDetail={report}
      reportDetailError=""
      reportDetailLoading={false}
    />,
  );
}

function notebookFact(): string | null {
  const term = screen.queryByText("笔记本", { selector: "dt" });
  return term ? term.nextElementSibling?.textContent ?? "" : null;
}

test("提问详情写出所在笔记本，详情到达后以详情的库名为准", () => {
  detailPane(askItem(), { ask: askDetail() });
  expect(notebookFact()).toBe("详情里的库名");
});

test("提问详情还没到达时先用列表条目的库名", () => {
  detailPane(askItem());
  expect(notebookFact()).toBe("列表里的库名");
});

test("报告详情写出所在笔记本，详情到达后以详情的库名为准", () => {
  detailPane(reportItem(), { report: reportDetail() });
  expect(notebookFact()).toBe("详情里的库名");
});

test("删除后的留存只由留存说明写出原库名，不再重复一行", () => {
  const deleted = {
    notebook_name: "旧项目",
    notebook_deleted_at: "2026-10-20T11:00:00+00:00",
    retained_until: "2027-04-18T11:00:00+00:00",
  };
  const { container } = detailPane(askItem(deleted), { ask: askDetail(deleted) });
  expect(notebookFact()).toBeNull();
  expect(container.querySelector(".activity-retained-notice")).toHaveTextContent("原笔记本《旧项目》已删除");
});

test("删除后留存的报告同样只由留存说明写出原库名", () => {
  const deleted = {
    notebook_name: "旧项目",
    notebook_deleted_at: "2026-10-20T11:00:00+00:00",
    retained_until: "2027-04-18T11:00:00+00:00",
  };
  const { container } = detailPane(reportItem(deleted), { report: reportDetail(deleted) });
  expect(notebookFact()).toBeNull();
  expect(container.querySelector(".activity-retained-notice")).toHaveTextContent("原笔记本《旧项目》已删除");
});

test("全局问答只列参与笔记本，不写「笔记本」", () => {
  detailPane(askItem({ scope: "global", notebook_id: "", notebook_name: "" }), {
    ask: askDetail({
      scope: "global",
      notebook_id: "",
      notebook_name: "",
      notebook_ids: ["nb-0"],
      notebook_names: { "nb-0": "材料研究" },
    }),
  });
  expect(notebookFact()).toBeNull();
  expect(screen.getByText("参与笔记本")).toBeInTheDocument();
  expect(screen.getByText("材料研究")).toBeInTheDocument();
});
