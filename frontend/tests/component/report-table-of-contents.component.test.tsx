import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { ReportDocument } from "../../app/report-document";
import { ReportMarkdown, ReportsPanel, type ReportDetailT } from "../../app/report-view";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

afterEach(cleanup);

function documentView(markdown: string) {
  return <ReportDocument markdown={markdown}><ReportMarkdown markdown={markdown} /></ReportDocument>;
}

test("目录使用最终正文的标题与顺序，不使用生成前的大纲", () => {
  const active: ReportDetailT = {
    id: "report-toc", question: "研究问题", status: "done", progress: "",
    section_count: 2, created_at: "2026-10-26T00:00:00Z", created_by: "user-1",
    outline: [{ title: "原计划章节", scope: "", sub_queries: [] }],
    sections: [], section_status: [], gaps: [], references: [], error: "", understanding: {},
    content_md: "# 综合报告\n\n## 执行摘要\n结论\n\n## **重新组织的论证**\n\n### 条件与限制\n\n## 参考文献",
  };
  render(<ReportsPanel notebookId="nb-1" workspace={reportWorkspaceFixture({ active })} setToast={vi.fn()} />);

  const contents = screen.getByRole("navigation", { name: "报告目录" });
  expect(within(contents).getAllByRole("button").map((button) => button.textContent)).toEqual([
    "综合报告", "执行摘要", "重新组织的论证", "条件与限制", "参考文献",
  ]);
  expect(within(contents).queryByText("原计划章节")).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "参考文献" })).toHaveAttribute("id", "report-references");
});

test("正文末尾的「参考文献」一节默认收起、可展开，目录跳过去时自动展开", async () => {
  const user = userEvent.setup();
  const markdown = "## 结论\n\n主流是 Transformer。\n\n## 参考文献\n\n- 甲文 · p. 2\n- 乙文 · p. 5\n\n> 引证分布：2 处\n\n## 局限\n\n证据有限。";
  const { container } = render(documentView(markdown));

  const heading = screen.getByRole("heading", { name: "参考文献" });
  heading.scrollIntoView = vi.fn();
  const fold = heading.closest("details");
  expect(fold).toHaveClass("report-references-fold");
  expect(fold).not.toHaveAttribute("open");
  // 标题在 summary 里：收起时仍可见；清单与引证分布都收进去，后面的「局限」不收。
  expect(heading.closest("summary")?.parentElement).toBe(fold);
  expect(fold).toHaveTextContent("甲文 · p. 2");
  expect(fold).toHaveTextContent("引证分布");
  expect(fold).not.toHaveTextContent("证据有限");
  expect(container.querySelectorAll("details")).toHaveLength(1);

  await user.click(within(screen.getByRole("navigation", { name: "报告目录" })).getByRole("button", { name: "参考文献" }));
  expect(fold).toHaveAttribute("open");
  expect(heading).toHaveFocus();
});

test("只折叠末尾那个标题恰为「参考文献」的一节，标题里含这几个字的正文章节不折叠", () => {
  const markdown = [
    "## 参考文献中的方法比较", "", "正文章节。", "",
    "## 参考文献", "", "- 模型在章节里写的同名小节", "",
    "## 结论", "", "收尾。", "",
    "## 参考文献", "", "- 甲文 · p. 2", "",
    "> 引证分布：1 处",
  ].join("\n");
  const { container } = render(documentView(markdown));

  const folds = container.querySelectorAll("details.report-references-fold");
  expect(folds).toHaveLength(1);
  expect(folds[0]).toHaveTextContent("甲文 · p. 2");
  expect(folds[0]).not.toHaveTextContent("同名小节");
  expect(screen.getByText("正文章节。").closest("details")).toBeNull();
});

test("同名标题分别跳转到对应的正文位置，并移动键盘焦点", async () => {
  const user = userEvent.setup();
  render(documentView("## 相同标题\n\n第一节\n\n### 子节\n\n## 相同标题\n\n第二节"));
  const headings = screen.getAllByRole("heading", { name: "相同标题" });
  const firstScroll = vi.fn();
  const secondScroll = vi.fn();
  headings[0].scrollIntoView = firstScroll;
  headings[1].scrollIntoView = secondScroll;
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  const links = within(contents).getAllByRole("button", { name: "相同标题" });

  await user.click(links[1]);
  expect(secondScroll).toHaveBeenCalledOnce();
  expect(firstScroll).not.toHaveBeenCalled();
  expect(headings[1]).toHaveFocus();
  expect(links[1]).toHaveAttribute("aria-current", "location");

  await user.click(links[0]);
  expect(firstScroll).toHaveBeenCalledOnce();
  expect(headings[0]).toHaveFocus();
  expect(links[1]).not.toHaveAttribute("aria-current");
});

test("代码块里的井号不是目录项，一级至六级真实标题都可见", () => {
  render(documentView("# 总览\n\n```md\n## 代码示例标题\n```\n\n#### 四级\n\n##### 五级\n\n###### 六级"));
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  expect(within(contents).getAllByRole("button").map((button) => button.textContent)).toEqual([
    "总览", "四级", "五级", "六级",
  ]);
});

test("正文更新后重建目录，正文没有标题时不显示目录", () => {
  const view = render(documentView("## 初稿标题\n\n初稿"));
  expect(screen.getByRole("button", { name: "初稿标题" })).toBeVisible();

  view.rerender(documentView("## 综合后的标题\n\n正文\n\n### 新的小节"));
  expect(screen.queryByRole("button", { name: "初稿标题" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "综合后的标题" })).toBeVisible();
  expect(screen.getByRole("button", { name: "新的小节" })).toBeVisible();

  view.rerender(documentView("只有正文，没有标题。"));
  expect(screen.queryByRole("navigation", { name: "报告目录" })).not.toBeInTheDocument();
  expect(screen.getByText("只有正文，没有标题。")).toBeVisible();
});

test("公式标题在目录中只展示一次公式内容", () => {
  render(documentView("## 关于 $x^2$ 的讨论"));
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  expect(within(contents).getByRole("button").textContent?.match(/x/g)).toHaveLength(1);
  expect(within(contents).getAllByRole("button")).toHaveLength(1);
});
