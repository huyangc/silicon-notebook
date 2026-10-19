import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { AnalyticsAnswerQuality } from "../../app/analytics-answer-quality";
import type { NotebookAnalytics } from "../../app/workspace-model";

const base: NotebookAnalytics = {
  answers_total: 5,
  feedback_useful: 3,
  feedback_not_useful: 2,
  feedback_useful_global: 0,
  feedback_not_useful_global: 0,
  usefulness_rate: 0.6,
  low_rated_questions: ["放大器为什么振荡？"],
  candidate_counts: {},
  knowledge_counts: {},
  source_status_counts: {},
};

test("没有全局评分时只显示笔记本内的数字，不加全局标注", () => {
  render(<AnalyticsAnswerQuality analytics={base} />);
  expect(screen.getByText("笔记本内提问 5")).toBeInTheDocument();
  expect(screen.getByText("👍 3")).toBeInTheDocument();
  expect(screen.getByText("👎 2")).toBeInTheDocument();
  expect(screen.getByText("放大器为什么振荡？")).toBeInTheDocument();
  expect(screen.queryByText(/全局问答/)).toBeNull();
});

test("全局评分计入 👍 / 👎 并标注其中全局问答的条数；低分列表只计数不列原文", () => {
  render(
    <AnalyticsAnswerQuality
      analytics={{ ...base, feedback_useful: 4, feedback_not_useful: 5, feedback_useful_global: 1, feedback_not_useful_global: 3 }}
    />,
  );
  expect(screen.getByText("👍 4（其中全局问答 1 条）")).toBeInTheDocument();
  expect(screen.getByText("👎 5（其中全局问答 3 条）")).toBeInTheDocument();
  expect(screen.getByText("放大器为什么振荡？")).toBeInTheDocument();
  expect(screen.getByText("另有 3 条来自全局问答")).toBeInTheDocument();
});

test("只有全局 👎 时仍显示低分提问一节，只有计数", () => {
  render(
    <AnalyticsAnswerQuality
      analytics={{ ...base, feedback_not_useful: 1, feedback_not_useful_global: 1, low_rated_questions: [] }}
    />,
  );
  expect(screen.getByText("低分提问（知识缺口）")).toBeInTheDocument();
  expect(screen.getByText("另有 1 条来自全局问答")).toBeInTheDocument();
});
