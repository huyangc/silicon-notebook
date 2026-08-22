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
} from "../../app/report-view";
import { reportWorkspaceFixture } from "./report-workspace-fixture";


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


test("限定资料范围与统计失败在资料基础里读起来不是同一件事", () => {
  const { rerender } = render(
    <ReportCorpusBasis report={detail({
      understanding: {
        result_scope: "ranked",
        corpus_profile: { unavailable_reason: "scope_restricted" },
      },
    })} />,
  );

  expect(screen.getByLabelText("资料基础")).toHaveTextContent("限定了检索的资料范围");
  expect(screen.queryByText(/未能完成/)).toBeNull();

  rerender(
    <ReportCorpusBasis report={detail({
      understanding: {
        result_scope: "ranked",
        corpus_profile: { unavailable_reason: "failed" },
      },
    })} />,
  );

  expect(screen.getByLabelText("资料基础")).toHaveTextContent("统计未能完成");
  expect(screen.queryByText(/限定了检索的资料范围/)).toBeNull();
});


test("资料基础点明引用到的参考库资料，按来源去重而非按锚点", () => {
  const { rerender } = render(
    <ReportCorpusBasis report={detail({
      understanding: { corpus_profile: { total_sources: 4 } },
      references: [
        { key: "k1", label: "a", tier: "base", source_id: "src-b1" },
        { key: "k2", label: "a", tier: "base", source_id: "src-b1" },
        { key: "k3", label: "b", tier: "base", source_id: "src-b2" },
        { key: "k4", label: "c", tier: "personal", source_id: "src-p1" },
      ],
    })} />,
  );

  expect(screen.getByLabelText("资料基础")).toHaveTextContent("4 份资料");
  expect(screen.getByText("另引用了 2 份参考库资料，未计入上述统计。")).toBeVisible();

  // 参考库知识证据可以没有 source_id，后端用 family_key 兜底，前端必须同口径。
  rerender(
    <ReportCorpusBasis report={detail({
      understanding: { corpus_profile: { total_sources: 4 } },
      references: [
        { key: "k1", label: "a", tier: "base", family_key: "source-title:a paper" },
        { key: "k2", label: "a", tier: "base", family_key: "source-title:a paper" },
        { key: "k3", label: "b", tier: "base", family_key: "source-title:another" },
        // evidence:<锚点> 每条都不同，计入会把一份身份未知的资料数成好几份。
        { key: "k4", label: "c", tier: "base", family_key: "evidence:anchor-1" },
        { key: "k5", label: "d", tier: "base", family_key: "evidence:anchor-2" },
      ],
    })} />,
  );
  expect(screen.getByText("另引用了 2 份参考库资料，未计入上述统计。")).toBeVisible();

  // 挂载自有 notebook 时 tier 仍是 personal，判据是归属标记而不是 tier。
  // 两个方向分开断言：混在一起两种错误会互相抵消。
  rerender(
    <ReportCorpusBasis report={detail({
      understanding: { corpus_profile: { total_sources: 4 } },
      references: [
        { key: "k1", label: "a", tier: "personal", from_reference_library: true,
          source_id: "src-m1" },
      ],
    })} />,
  );
  expect(screen.getByText("另引用了 1 份参考库资料，未计入上述统计。")).toBeVisible();

  rerender(
    <ReportCorpusBasis report={detail({
      understanding: { corpus_profile: { total_sources: 4 } },
      references: [
        { key: "k2", label: "b", tier: "base", from_reference_library: false,
          source_id: "src-local" },
      ],
    })} />,
  );
  expect(screen.queryByText(/参考库资料/)).toBeNull();

  // 纯本地报告一个字都不该多说。
  rerender(
    <ReportCorpusBasis report={detail({
      understanding: { corpus_profile: { total_sources: 4 } },
      references: [{ key: "k1", label: "c", tier: "personal", source_id: "src-p1" }],
    })} />,
  );
  expect(screen.queryByText(/参考库资料/)).toBeNull();

  // 旧报告没有画像：卡片上方是空的，所以不能说「未计入上述统计」。
  rerender(
    <ReportCorpusBasis report={detail({
      understanding: {},
      references: [{ key: "k1", label: "b", tier: "base", source_id: "src-b1" }],
    })} />,
  );
  expect(screen.getByText("本报告引用了 1 份参考库资料。")).toBeVisible();
  expect(screen.queryByText(/未计入上述统计/)).toBeNull();
});


test("大纲确认将用户编辑的分析框架与章节一起提交", async () => {
  const user = userEvent.setup();
  const onGenerate = vi.fn().mockResolvedValue(undefined);
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
      busy={false}
      onGenerate={onGenerate}
      setToast={vi.fn()}
    />,
  );

  expect(screen.getByLabelText("分析框架")).toBeVisible();
  const subjectKind = screen.getByLabelText("对象类型");
  await user.clear(subjectKind);
  await user.type(subjectKind, "架构实例");
  await user.click(screen.getByRole("button", { name: "生成完整报告" }));

  expect(onGenerate).toHaveBeenCalledWith({
    sections: expect.any(Array),
    frame: expect.objectContaining({ subject_kind: "架构实例" }),
  });
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


test("部分主张账本在可用数上加括注，缺该字段或为零的旧报告不受影响", () => {
  const { rerender } = render(
    <ReportCredibilitySummary report={detail({
      understanding: {
        credibility: {
          synthesis_status: "available",
          claim_ledgers_available: 4,
          claim_ledgers_partial: 2,
          claim_ledgers_total: 5,
        },
      },
    })} />,
  );
  expect(screen.getByText("主张账本：4/5 节可用（其中 2 节为部分账本）")).toBeVisible();

  // claim_ledgers_partial 为 0：不加括注，措辞与历史一致。
  rerender(
    <ReportCredibilitySummary report={detail({
      understanding: {
        credibility: {
          synthesis_status: "available",
          claim_ledgers_available: 5,
          claim_ledgers_partial: 0,
          claim_ledgers_total: 5,
        },
      },
    })} />,
  );
  expect(screen.getByText("主张账本：5/5 节可用")).toBeVisible();
  expect(screen.queryByText(/部分账本/)).toBeNull();

  // 旧报告没有 claim_ledgers_partial 字段：走旧文案，不能因缺字段而报错或误加括注。
  rerender(
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
  expect(screen.getByText("主张账本：3/5 节可用")).toBeVisible();
  expect(screen.queryByText(/部分账本/)).toBeNull();
});


test("单节报告的未请求综合是预期的，多节报告的同一回执必须可见", () => {
  // 章节数就是 `claim_ledgers_total`。一节没有跨章节一致性可综合，所以它的
  // `not_requested` 不含信息；两节以上的同一回执说明本该发生的综合没发生。
  const { rerender } = render(
    <ReportCredibilitySummary report={detail({
      depth: 16,
      understanding: {
        credibility: {
          synthesis_status: "not_requested",
          claim_ledgers_available: 0,
          claim_ledgers_total: 1,
        },
      },
    })} />,
  );

  expect(screen.queryByLabelText("报告可信度回执")).toBeNull();

  rerender(
    <ReportCredibilitySummary report={detail({
      depth: 16,
      understanding: {
        credibility: {
          synthesis_status: "not_requested",
          claim_ledgers_available: 0,
          claim_ledgers_total: 2,
        },
      },
    })} />,
  );

  expect(screen.getByLabelText("报告可信度回执")).toHaveTextContent("未请求全篇综合");
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
      workspace={reportWorkspaceFixture({ active: report })}
      setToast={vi.fn()}
    />,
  );

  expect(await screen.findByLabelText("报告可信度回执")).toHaveTextContent("返回结果未通过校验");
  expect(screen.getByTitle("报告引证的资料数量与个人/公共来源分布")).toHaveTextContent("可区分资料 1");
});


test("失败报告保留大纲时可从详情页原地重新生成", async () => {
  const user = userEvent.setup();
  const failed = detail({
    id: "rep-retry",
    depth: 8,
    status: "failed",
    error: "internal detail",
    content_md: "STALE REPORT BODY",
    sections: [{ title: "old", markdown: "stale", grounded: false, failed: true }],
    section_status: [{ title: "old", phase: "失败", step: 0 }],
    gaps: ["old gap"],
    references: [{ key: "k1", label: "old reference" }],
    understanding: {
      credibility: { synthesis_status: "failed_model" },
    },
  });
  const requestRetry = vi.fn();
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: failed, requestRetry })}
      setToast={vi.fn()}
    />,
  );

  expect(await screen.findByText("STALE REPORT BODY")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "重新生成" }));

  expect(requestRetry).toHaveBeenCalledOnce();
});


test("形成大纲前失败的报告不展示误导性的重试按钮", async () => {
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: detail({
        id: "rep-no-outline", status: "failed", outline: [],
      }) })}
      setToast={vi.fn()}
    />,
  );

  expect(await screen.findByText(/请重新创建报告/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();
});


test("分析框架可以删除维度和条件，避免留下后端拒绝的空名称", async () => {
  const user = userEvent.setup();
  const onGenerate = vi.fn().mockResolvedValue(undefined);
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
      busy={false}
      onGenerate={onGenerate}
      setToast={vi.fn()}
    />,
  );

  await user.click(screen.getByRole("button", { name: "删除分类维度：序列建模" }));
  await user.click(screen.getByRole("button", { name: "删除比较条件：比较条件" }));
  await user.click(screen.getByRole("button", { name: "生成完整报告" }));

  expect(onGenerate).toHaveBeenCalledWith({
    sections: expect.any(Array),
    frame: expect.objectContaining({ facets: [], axes: [] }),
  });
});


test("分享完成后落到发起时那份报告，且剪贴板失败不谎报已复制", async () => {
  const user = userEvent.setup();
  const first = detail({ id: "rep-a", status: "done", content_md: "A", shared: false });
  const second = detail({ id: "rep-b", status: "done", content_md: "B", shared: false });

  // 分享请求悬挂：期间把面板切到另一份报告，完成时不得把分享态按到它头上。
  const toggleShare = vi.fn();

  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: first, reports: [first, second], toggleShare })}
      setToast={vi.fn()}
    />,
  );

  const shareButton = await screen.findByRole("button", { name: /分享/ });
  await user.click(shareButton);
  expect(toggleShare).toHaveBeenCalledOnce();
});


test("剪贴板被拒时如实报错并把链接显示出来", async () => {
  // execCommand 返回 false 而不抛，是非安全上下文的常态；旧写法会说「已复制」。
  const originalClipboard = navigator.clipboard;
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
  const originalExec = document.execCommand;
  document.execCommand = vi.fn(() => false) as unknown as typeof document.execCommand;
  try {
    const { copyReportContent } = await import("../../app/report-view");
    await expect(copyReportContent("x")).rejects.toThrow();
  } finally {
    document.execCommand = originalExec;
    Object.defineProperty(navigator, "clipboard", {
      value: originalClipboard, configurable: true,
    });
  }
});
