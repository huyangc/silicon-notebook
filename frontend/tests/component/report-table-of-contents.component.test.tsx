import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { ReportDocument } from "../../app/report-document";
import { ReportMarkdown, ReportsPanel, type ReportDetailT } from "../../app/report-view";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

afterEach(cleanup);

function documentView(markdown: string) {
  return <ReportDocument markdown={markdown}><ReportMarkdown markdown={markdown} /></ReportDocument>;
}

function entryTitles(contents: HTMLElement) {
  return within(contents).getAllByRole("button")
    .filter((button) => !button.hasAttribute("aria-expanded") && !button.textContent?.startsWith("全部"))
    .map((button) => button.textContent);
}

test("目录使用最终正文的标题与顺序，不使用生成前的大纲", async () => {
  const user = userEvent.setup();
  const active: ReportDetailT = {
    id: "report-toc", question: "研究问题", status: "done", progress: "",
    section_count: 2, created_at: "2026-10-26T00:00:00Z", created_by: "user-1",
    outline: [{ title: "原计划章节", scope: "", sub_queries: [] }],
    sections: [], section_status: [], gaps: [], references: [], error: "", understanding: {},
    content_md: "# 综合报告\n\n## 执行摘要\n结论\n\n## **重新组织的论证**\n\n### 条件与限制\n\n## 参考文献",
  };
  render(<ReportsPanel notebookId="nb-1" workspace={reportWorkspaceFixture({ active })} setToast={vi.fn()} />);

  const contents = screen.getByRole("navigation", { name: "报告目录" });
  expect(within(contents).queryByRole("button", { name: "条件与限制" })).not.toBeInTheDocument();
  await user.click(within(contents).getByRole("button", { name: "全部展开" }));
  expect(entryTitles(contents)).toEqual([
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

test("代码块里的井号不是目录项，跳级标题按真实层级展开", async () => {
  const user = userEvent.setup();
  render(documentView("# 总览\n\n```md\n## 代码示例标题\n```\n\n#### 四级\n\n##### 五级\n\n###### 六级"));
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  await user.click(within(contents).getByRole("button", { name: "全部展开" }));
  expect(entryTitles(contents)).toEqual([
    "总览", "四级", "五级", "六级",
  ]);
});

test("正文更新后重建目录并重置折叠，正文没有标题时不显示目录", async () => {
  const user = userEvent.setup();
  const view = render(documentView("## 初稿标题\n\n初稿"));
  expect(screen.getByRole("button", { name: "初稿标题" })).toBeVisible();

  view.rerender(documentView("## 综合后的标题\n\n正文\n\n### 新的小节"));
  expect(screen.queryByRole("button", { name: "初稿标题" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "综合后的标题" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "新的小节" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "全部展开" }));
  expect(screen.getByRole("button", { name: "新的小节" })).toBeVisible();

  view.rerender(documentView("只有正文，没有标题。"));
  expect(screen.queryByRole("navigation", { name: "报告目录" })).not.toBeInTheDocument();
  expect(screen.getByText("只有正文，没有标题。")).toBeVisible();
});

test("公式标题在目录中只展示一次公式内容", () => {
  render(documentView("## 关于 $x^2$ 的讨论"));
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  expect(entryTitles(contents)[0]?.match(/x/g)).toHaveLength(1);
  expect(entryTitles(contents)).toHaveLength(1);
});

test("各分支可独立收起展开，并支持一键操作所有层级", async () => {
  const user = userEvent.setup();
  render(documentView("# 报告\n\n## 甲章\n\n### 甲节\n\n#### 甲小节\n\n## 乙章\n\n### 乙节"));
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  expect(entryTitles(contents)).toEqual(["报告", "甲章", "乙章"]);
  await user.click(within(contents).getByRole("button", { name: "展开「甲章」子目录" }));
  expect(entryTitles(contents)).toEqual(["报告", "甲章", "甲节", "乙章"]);
  await user.click(within(contents).getByRole("button", { name: "展开「甲节」子目录" }));
  expect(within(contents).getByRole("button", { name: "甲小节" })).toBeVisible();
  await user.click(within(contents).getByRole("button", { name: "收起「甲章」子目录" }));
  expect(entryTitles(contents)).toEqual(["报告", "甲章", "乙章"]);
  await user.click(within(contents).getByRole("button", { name: "全部展开" }));
  expect(entryTitles(contents)).toEqual(["报告", "甲章", "甲节", "甲小节", "乙章", "乙节"]);
  await user.click(within(contents).getByRole("button", { name: "全部收起" }));
  expect(entryTitles(contents)).toEqual(["报告", "甲章", "乙章"]);
});

test("跟踪滚动容器可见中心，展开当前路径且手动收起持续到读到下一节", async () => {
  const user = userEvent.setup();
  let offset = 0;
  const positions = new Map([
    ["甲章", 150], ["甲节", 350], ["甲小节", 430], ["乙章", 800], ["乙节", 1000],
  ]);
  const rect = (top: number, bottom: number) => ({ top, bottom, height: bottom - top, left: 0, right: 600, width: 600, x: 0, y: top, toJSON() {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const position = this.matches("h2, h3, h4") ? positions.get(this.textContent || "") : undefined;
    if (position !== undefined) return rect(position - offset, position - offset + 25);
    if (this.getAttribute("aria-label") === "阅读区域") return rect(100, 500);
    return rect(-offset, 1800 - offset);
  });
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([rect(0, 25)] as unknown as DOMRectList);
  render(<section aria-label="阅读区域" style={{ overflowY: "auto" }}>
    {documentView("## 甲章\n\n### 甲节\n\n#### 甲小节\n\n## 乙章\n\n### 乙节")}
  </section>);
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  const panel = screen.getByRole("region", { name: "阅读区域" });
  // Panel center is 300, not the browser window's center: 甲节 is still ahead.
  expect(within(contents).getByRole("button", { name: "甲章" })).toHaveAttribute("aria-current", "location");
  expect(within(contents).queryByRole("button", { name: "甲小节" })).not.toBeInTheDocument();

  offset = 140;
  fireEvent.scroll(panel);
  await waitFor(() => expect(within(contents).getByRole("button", { name: "甲小节" })).toHaveAttribute("aria-current", "location"));
  expect(contents).toHaveTextContent("正在阅读：甲小节");
  await user.click(within(contents).getByRole("button", { name: "全部收起" }));
  expect(within(contents).getByRole("button", { name: "甲章" })).toHaveAttribute("aria-current", "location");
  fireEvent.scroll(panel);
  await new Promise((resolve) => window.requestAnimationFrame(resolve));
  expect(within(contents).queryByRole("button", { name: "甲节" })).not.toBeInTheDocument();

  offset = 720;
  fireEvent.scroll(panel);
  await waitFor(() => expect(within(contents).getByRole("button", { name: "乙节" })).toHaveAttribute("aria-current", "location"));
  expect(within(contents).queryByRole("button", { name: "甲节" })).not.toBeInTheDocument();

  offset = 70;
  fireEvent.scroll(panel);
  await waitFor(() => expect(within(contents).getByRole("button", { name: "甲节" })).toHaveAttribute("aria-current", "location"));
  expect(within(contents).queryByRole("button", { name: "乙节" })).not.toBeInTheDocument();
});

test("目录只在面板裁剪后的可见区域内滚动当前项，缩小面板后也保持可见", async () => {
  const user = userEvent.setup();
  let panelBottom = 500;
  const rect = (top: number, bottom: number) => ({ top, bottom, height: bottom - top, left: 0, right: 600, width: 600, x: 0, y: top, toJSON() {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.getAttribute("aria-label") === "阅读区域") return rect(100, panelBottom);
    if (this.getAttribute("aria-label") === "报告目录") return rect(100, 900);
    if (this.tagName === "BUTTON" && this.textContent === "乙章") {
      const offset = this.closest("nav")?.scrollTop || 0;
      return rect(550 - offset, 580 - offset);
    }
    if (this.tagName === "H2") {
      const top = this.textContent === "丙章" ? 1000 : this.textContent === "乙章" ? 250 : 150;
      return rect(top, top + 25);
    }
    if (this.tagName === "H3") return rect(1200, 1225);
    return rect(0, 1500);
  });
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([rect(0, 25)] as unknown as DOMRectList);
  render(<section aria-label="阅读区域" style={{ overflowY: "auto" }}>
    {documentView("## 甲章\n\n## 乙章\n\n## 丙章\n\n### 丙节")}
  </section>);
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  expect(within(contents).getByRole("button", { name: "乙章" })).toHaveAttribute("aria-current", "location");
  expect(contents.scrollTop).toBe(80);
  // Scrolling the contents by hand must not immediately snap back to the current item.
  contents.scrollTop = 20;
  fireEvent.scroll(contents);
  await new Promise((resolve) => window.requestAnimationFrame(resolve));
  expect(contents.scrollTop).toBe(20);
  await user.click(within(contents).getByRole("button", { name: "展开「丙章」子目录" }));
  expect(within(contents).getByRole("button", { name: "丙节" })).toBeVisible();
  expect(contents.scrollTop).toBe(20);

  panelBottom = 400;
  fireEvent(window, new Event("resize"));
  await waitFor(() => expect(contents.scrollTop).toBe(180));
  expect(screen.getByRole("region", { name: "阅读区域" }).scrollTop).toBe(0);
});

test("展开正文上方的资料基础后，无需滚动也会重新定位当前阅读章节", async () => {
  const user = userEvent.setup();
  let shift = 0;
  const rect = (top: number, bottom: number) => ({ top, bottom, height: bottom - top, left: 0, right: 600, width: 600, x: 0, y: top, toJSON() {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.getAttribute("aria-label") === "阅读区域") return rect(100, 500);
    if (this.tagName === "H2") {
      const top = (this.textContent === "乙章" ? 250 : 150) + shift;
      return rect(top, top + 25);
    }
    return rect(shift, 1500 + shift);
  });
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([rect(0, 25)] as unknown as DOMRectList);
  render(<section aria-label="阅读区域" style={{ overflowY: "auto" }}>
    <details><summary>资料基础</summary><p>资料统计</p></details>
    {documentView("## 甲章\n\n## 乙章")}
  </section>);
  const contents = screen.getByRole("navigation", { name: "报告目录" });
  expect(within(contents).getByRole("button", { name: "乙章" })).toHaveAttribute("aria-current", "location");
  shift = 200;
  await user.click(screen.getByText("资料基础"));
  await waitFor(() => expect(within(contents).getByRole("button", { name: "甲章" })).toHaveAttribute("aria-current", "location"));
  shift = 0;
  await user.click(screen.getByText("资料基础"));
  await waitFor(() => expect(within(contents).getByRole("button", { name: "乙章" })).toHaveAttribute("aria-current", "location"));
});
