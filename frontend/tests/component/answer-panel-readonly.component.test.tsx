// AnswerView 的交互回调全是可选 prop：**没有承接方就不渲染那颗按钮**。
//
// 这条不是风格偏好。只读的排障视图（dev/logs 的「活动」）此前传的是空实现，再由
// logs.css 隐藏 `.answer-feedback` 与索引横幅上的按钮——而引用浮层里的「知识图谱」/
// 「在表格中查看」和清单卡每行的「在表格中查看」根本不在那两条规则的覆盖面里，于是
// 以**启用态**留在界面上，`title` 还写着「在知识图谱中定位」，点下去什么都不发生。
// 最常见的路径就会踩到：选中一条有据提问 → 点 [1] → 浮层 → 点「知识图谱」。
//
// 所以判据收敛成一条：缺回调即缺控件，不靠样式表补救。下面第一条用例钉只读态、
// 第二条是正向对照（回调齐全时那几颗按钮必须在），免得第一条因为「浮层压根没打开」
// 或「按钮名字对不上」而假绿。

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import { ActivityDetail } from "../../app/dev/logs/activity/ActivityDetail";
import type { ActivityAsk, AskDetail } from "../../app/dev/logs/activity/types";
import type { AskResponse } from "../../app/workspace-model";

const NOW = new Date(2026, 7, 4, 12, 0);

const ITEM: ActivityAsk = {
  type: "ask",
  id: "job-1",
  notebook_id: "nb-1",
  created_at: "2026-08-04T10:30:00",
  asked_at: "2026-08-04T10:29:00",
  conversation_id: "conv-1",
  question: "这份合同的付款条款是什么？",
  mode: "reasoning",
  status: "done",
  answer_id: "ans-1",
  error: "",
};

// ⚠ 刻意用 `as unknown as AskResponse`：线上 wire 里 anchor/citation 命中 knowhow
// 单行格子时会多带一个 `knowhow` 字段（真源是 answer-formatting.ts 的
// AnswerAnchorLike/CitationLike，也是 answer-panel 实际消费的形状），而
// workspace-model.ts 的 AnswerAnchor 至今没有声明它。这里要覆盖的正是那条分支。
function answerFixture(): AskResponse {
  return {
    answer_id: "ans-1",
    conversation_id: "conv-1",
    conclusion: "付款分三期 [k1]，明细见表 [k2]。",
    answer: "付款分三期 [k1]，明细见表 [k2]。",
    grounded: true,
    // 索引降级横幅：**说明文字**是管理员要看的诊断，必须留下；那颗按钮没有承接方。
    index_required: true,
    anchors: [
      {
        key: "k1",
        object_id: "obj-1",
        object_type: "claim",
        label: "付款条款",
        name: "付款条款",
        source_title: "合同",
        location_label: "第 3 条",
        source_id: "src-1",
        element_id: "el-1",
        tier: "personal",
      },
      {
        key: "k2",
        object_id: "obj-2",
        object_type: "concept",
        label: "付款明细",
        name: "付款明细",
        source_title: "明细表",
        location_label: "第 1 行",
        knowhow: { table_id: "table-1", row_id: "row-0" },
      },
    ],
    related_knowledge: [],
    citations: [],
    llm_mode: "reasoning",
    // 站外来源建议（ask.gap_consult，X9 PR-A T3）：区块本身没有承接方门槛
    // （不像保存到记忆/反馈那样整块隐藏），只有里面的「导入」按钮是可选交互——
    // 只读排障视图仍应显示这份披露，只是没有导入入口。
    gap_suggestions: [{
      title: "站外相关文档",
      url: "https://example.com/doc.pdf",
      summary: "一句摘要",
      source_label: "示例来源",
    }],
    result_sets: [{
      kind: "knowhow",
      table_id: "table-1",
      title: "付款明细",
      columns: [{ id: "stage", name: "期次" }],
      rows: [{ row_id: "row-0", position: 0, cells: { stage: "首期" } }],
      coverage: {
        total_rows: 1,
        scanned_rows: 1,
        returned_rows: 1,
        complete: true,
        truncated_reason: null,
        overflow_semantics: "",
      },
    }],
  } as unknown as AskResponse;
}

function detailFixture(): AskDetail {
  return {
    job_id: "job-1",
    notebook_id: "nb-1",
    conversation_id: "conv-1",
    question: ITEM.question,
    mode: "reasoning",
    status: "done",
    asked_at: "2026-08-04T10:29:00",
    answered_at: "2026-08-04T10:31:00",
    error: "",
    trace: [],
    answer: answerFixture(),
  };
}


test("只读活动详情里不出现任何点了没反应的控件", async () => {
  const user = userEvent.setup();
  render(
    <ActivityDetail
      askDetail={detailFixture()}
      askDetailError=""
      askDetailLoading={false}
      item={ITEM}
      notebookNames={{}}
      now={NOW}
    />,
  );

  // 正向对照①：答案确实渲染出来了（否则下面的 queryBy 全是空,这条用例毫无意义）。
  expect(screen.getByRole("button", { name: "[1]" })).toBeInTheDocument();
  // 正向对照②：复制回答是自足动作，任何调用方都能承接，它必须还在。
  expect(screen.getByRole("button", { name: "复制回答" })).toBeInTheDocument();
  // 正向对照③：索引降级横幅的说明文字保留——那正是管理员要看的诊断。
  expect(screen.getByText(/尚未建立索引/)).toBeInTheDocument();

  expect(screen.queryByRole("button", { name: "保存到记忆" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "有用" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "需改进" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /构建索引|立即构建|构建中/ }))
    .not.toBeInTheDocument();
  // 清单卡每行的跳转按钮（answer-panel.tsx 的 KnowhowResultSetCard）。
  expect(screen.queryAllByRole("button", { name: "在表格中查看" })).toHaveLength(0);

  // 站外来源建议（ask.gap_consult）：区块本身没有承接方门槛——只读排障视图仍要
  // 显示这份披露（不能因为没有导入回调就把整块隐藏，那会把「有站外建议」这件
  // 事本身也藏起来）；唯一缺席的是「导入」按钮，因为没有回调可以承接它。
  const gapSummary = screen.getByText("站外来源建议 · 1 条");
  expect(gapSummary.closest("details")).not.toHaveAttribute("open"); // 默认折叠
  await user.click(gapSummary);
  expect(screen.getByText("站外相关文档")).toBeVisible();
  expect(screen.queryByRole("button", { name: "导入" })).not.toBeInTheDocument();

  // 引用浮层里的两颗：知识对象引用的「知识图谱」、命中 knowhow 格子的「在表格中查看」。
  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument(); // 浮层真的开了
  expect(screen.queryByRole("button", { name: "知识图谱" })).not.toBeInTheDocument();

  // 浮层的既有交互:点它以外的任何地方都会收起。先按 Esc 显式收起再开下一条,
  // 免得「收起」与「打开」挤在同一次点击里。
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "[2]" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(screen.queryAllByRole("button", { name: "在表格中查看" })).toHaveLength(0);
});


// 正向对照：同一份答案，回调齐全时那几颗按钮必须都在。没有这条，把渲染条件写死成
// `false` 也能让上面那条通过。
test("回调齐全时同一份答案照常渲染这些控件", async () => {
  const user = userEvent.setup();
  render(
    <AnswerView
      answer={answerFixture()}
      buildingScaleIndex={false}
      feedbackSent=""
      memorySaved={false}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={vi.fn()}
      onFeedback={vi.fn()}
      onOpenKnowhowRow={vi.fn()}
      onOpenKnowledgeGraph={vi.fn()}
      onSaveMemory={vi.fn()}
      scaleIndexStatus={null}
    />,
  );

  expect(screen.getByRole("button", { name: "保存到记忆" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "有用" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "需改进" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "构建索引" })).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "在表格中查看" })).toHaveLength(1);

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByRole("button", { name: "知识图谱" })).toBeInTheDocument();

  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "[2]" }));
  expect(screen.getAllByRole("button", { name: "在表格中查看" }))
    .toHaveLength(2); // 清单卡那颗 + 浮层那颗
});


test("删除笔记本后的提问详情说明留存边界且不冒充答案缺失", () => {
  const deletedItem: ActivityAsk = {
    ...ITEM,
    answer_id: "",
    notebook_name: "旧项目",
    notebook_deleted_at: "2026-08-04T11:00:00",
    retained_until: "2027-01-31T11:00:00",
  };
  const deletedDetail: AskDetail = {
    ...detailFixture(),
    answer: null,
    trace: [],
    notebook_name: deletedItem.notebook_name,
    notebook_deleted_at: deletedItem.notebook_deleted_at,
    retained_until: deletedItem.retained_until,
  };

  render(
    <ActivityDetail
      askDetail={deletedDetail}
      askDetailError=""
      askDetailLoading={false}
      item={deletedItem}
      notebookNames={{}}
      now={NOW}
    />,
  );

  expect(screen.getByRole("note")).toHaveTextContent("原笔记本《旧项目》已删除");
  expect(screen.getByRole("note")).toHaveTextContent("正文、答案、引用和推理过程已随笔记本删除");
  expect(screen.queryByText("这次提问没有留下答案")).not.toBeInTheDocument();
});
