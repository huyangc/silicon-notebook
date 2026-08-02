import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import {
  OutlineEditor,
  ReportCitationDistribution,
  ReportCredibilitySummary,
  ReportCorpusBasis,
  ReportsPanel,
  type ReportDetailT,
} from "./report-view";


afterEach(cleanup);


function detail(overrides: Partial<ReportDetailT> = {}): ReportDetailT {
  return {
    id: "rep-credibility",
    question: "比较两类模型",
    status: "outline_ready",
    progress: "",
    section_count: 1,
    created_at: "2026-08-01T00:00:00Z",
    created_by: "user-1",
    outline: [{ title: "分类与比较", scope: "统一分类口径", sub_queries: ["model architecture"] }],
    sections: [],
    section_status: [],
    gaps: [],
    content_md: "",
    references: [],
    error: "",
    understanding: {},
    ...overrides,
  };
}


test("资料基础披露完整性限制，并兼容缺失 profile 的旧报告", () => {
  const { rerender } = render(<ReportCorpusBasis report={detail()} />);
  expect(screen.queryByLabelText("资料基础")).toBeNull();

  rerender(
    <ReportCorpusBasis report={detail({
      understanding: {
        result_scope: "complete",
        corpus_profile: {
          total_sources: 84,
          representative_count: 12,
          identified_duplicate_lower_bound: 7,
          identity_uncertain_sources: 4,
          type_distribution: [{ type: "论文", count: 60 }, { type: "报告", count: 24 }],
          year_distribution: [{ year: 2025, count: 50 }],
          unknown_year: 34,
          metadata_coverage: 0.6,
        },
      },
    })} />,
  );

  expect(screen.getByLabelText("资料基础")).toHaveTextContent("84 份资料");
  expect(screen.queryByText(/可区分资料/)).toBeNull();
  expect(screen.getByText("保守识别重复至少 7 份")).toBeVisible();
  expect(screen.getByText("论文 60 · 报告 24")).toBeVisible();
  expect(screen.getByText("2025 50 · 年份未知 34")).toBeVisible();
  expect(screen.getByText("本报告按相关性检索生成，未做完整枚举。")).toBeVisible();
  expect(screen.getByText(/资料识别信息完整度 60%/)).toBeVisible();
});


test("大纲确认将用户编辑的分析框架与章节一起提交", async () => {
  const user = userEvent.setup();
  const updateReportOutline = vi.fn().mockResolvedValue({ status: "ok", sections: 1 });
  const generateReport = vi.fn().mockResolvedValue({ status: "generating" });
  render(
    <OutlineEditor
      report={detail({
        understanding: {
          report_frame: {
            subject_kind: "模型实例",
            facets: [{ id: "mixer", name: "序列建模", values: ["注意力"], exclusive: true }],
            axes: [{ id: "cost", name: "比较条件", condition_fields: ["相同规模"] }],
            instance_policy: "模型作为组合实例",
          },
        },
      })}
      notebookId="nb-1"
      updateReportOutline={updateReportOutline}
      generateReport={generateReport}
      onGenerating={vi.fn()}
      setToast={vi.fn()}
    />,
  );

  expect(screen.getByLabelText("分析框架")).toBeVisible();
  const subjectKind = screen.getByLabelText("对象类型");
  await user.clear(subjectKind);
  await user.type(subjectKind, "架构实例");
  await user.click(screen.getByRole("button", { name: "生成完整报告" }));

  expect(updateReportOutline).toHaveBeenCalledWith("nb-1", "rep-credibility", {
    sections: expect.any(Array),
    frame: expect.objectContaining({ subject_kind: "架构实例" }),
  });
  expect(generateReport).toHaveBeenCalledWith("nb-1", "rep-credibility");
});


test("可信度回执明确显示全篇综合失败或跳过，并展示可用主张账本", () => {
  const { rerender } = render(
    <ReportCredibilitySummary report={detail({
      understanding: {
        credibility: {
          synthesis_status: "failed_validation",
          claim_ledgers_available: 3,
          claim_ledgers_total: 5,
        },
      },
    })} />,
  );

  expect(screen.getByLabelText("报告可信度回执")).toHaveTextContent("返回结果未通过校验");
  expect(screen.getByText("主张账本：3/5 节可用")).toBeVisible();

  rerender(
    <ReportCredibilitySummary report={detail({
      understanding: { credibility: { synthesis_status: "skipped_no_evidence" } },
    })} />,
  );
  expect(screen.getByText(/已跳过全篇综合：可用资料不足/)).toBeVisible();
});


test("标准档不显示预期的未请求综合和 0/N 账本噪音，高档异常仍可见", () => {
  const { rerender } = render(
    <ReportCredibilitySummary report={detail({
      depth: 2,
      understanding: {
        credibility: {
          synthesis_status: "not_requested",
          claim_ledgers_available: 0,
          claim_ledgers_total: 3,
        },
      },
    })} />,
  );

  expect(screen.queryByLabelText("报告可信度回执")).toBeNull();

  rerender(
    <ReportCredibilitySummary report={detail({
      depth: 8,
      understanding: {
        credibility: {
          synthesis_status: "not_requested",
          claim_ledgers_available: 0,
          claim_ledgers_total: 3,
        },
      },
    })} />,
  );

  expect(screen.getByLabelText("报告可信度回执")).toHaveTextContent("未请求全篇综合");
});


test("报告来源层级徽章复用可见引用的个人/公共口径", () => {
  render(
    <ReportCitationDistribution report={detail({
      references: [
        { key: "k1", label: "个人资料", tier: "personal" },
        { key: "k2", label: "公共资料", tier: "base" },
        { key: "k3", label: "未知资料" },
      ],
    })} />,
  );

  expect(screen.getByText(/来源 · 个人 2/)).toBeVisible();
  expect(screen.getByText("公共 1")).toBeVisible();
  expect(screen.queryByText(/可区分资料/)).toBeNull();
  expect(screen.queryByText(/最集中资料占/)).toBeNull();
});


test("旧报告不从未知身份引用推断可区分资料或 Top-1", () => {
  render(
    <ReportCitationDistribution report={detail({
      references: [
        { key: "k1", label: "旧资料一", source_id: "legacy-one", family_key: "legacy-one" },
        { key: "k2", label: "旧资料二", source_id: "legacy-two", family_key: "legacy-two" },
      ],
    })} />,
  );

  expect(screen.getByText(/来源 · 个人 2/)).toBeVisible();
  expect(screen.queryByText(/可区分资料 2/)).toBeNull();
  expect(screen.queryByText(/最集中资料占 50%/)).toBeNull();
});


test("报告详情实际挂载可信度回执与引证分布，而非只测试独立组件", async () => {
  const report = detail({
    id: "rep-mounted-credibility",
    depth: 8,
    status: "done",
    content_md: "# 完整报告\n\n结论 [k1]。",
    understanding: {
      credibility: {
        synthesis_status: "failed_validation",
        claim_ledgers_available: 1,
        claim_ledgers_total: 1,
        independent_documents: 1,
        top1_share: 1,
      },
    },
    references: [{
      key: "k1", label: "资料一", tier: "personal", family_key: "family:one",
    }],
  });
  render(
    <ReportsPanel
      notebookId="nb-1"
      listReports={vi.fn().mockResolvedValue([])}
      getReport={vi.fn().mockResolvedValue(report)}
      createReport={vi.fn()}
      confirmReportIntent={vi.fn()}
      updateReportOutline={vi.fn()}
      generateReport={vi.fn()}
      cancelReport={vi.fn()}
      deleteReport={vi.fn()}
      downloadReportsZip={vi.fn()}
      setToast={vi.fn()}
      focusReportId="rep-mounted-credibility"
      onFocusConsumed={vi.fn()}
    />,
  );

  expect(await screen.findByLabelText("报告可信度回执")).toHaveTextContent("返回结果未通过校验");
  expect(screen.getByTitle("按可区分资料统计，不以引用标记数量代替资料数量")).toHaveTextContent("可区分资料 1");
});


test("分析框架可以删除维度和条件，避免留下后端拒绝的空名称", async () => {
  const user = userEvent.setup();
  const updateReportOutline = vi.fn().mockResolvedValue({ status: "ok", sections: 1 });
  const generateReport = vi.fn().mockResolvedValue({ status: "generating" });
  render(
    <OutlineEditor
      report={detail({
        understanding: {
          report_frame: {
            facets: [{ id: "mixer", name: "序列建模", values: ["注意力"] }],
            axes: [{ id: "cost", name: "比较条件", condition_fields: ["相同规模"] }],
          },
        },
      })}
      notebookId="nb-1"
      updateReportOutline={updateReportOutline}
      generateReport={generateReport}
      onGenerating={vi.fn()}
      setToast={vi.fn()}
    />,
  );

  await user.click(screen.getByRole("button", { name: "删除分类维度：序列建模" }));
  await user.click(screen.getByRole("button", { name: "删除比较条件：比较条件" }));
  await user.click(screen.getByRole("button", { name: "生成完整报告" }));

  expect(updateReportOutline).toHaveBeenCalledWith("nb-1", "rep-credibility", {
    sections: expect.any(Array),
    frame: expect.objectContaining({ facets: [], axes: [] }),
  });
});
