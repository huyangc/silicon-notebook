import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetchPublicReport: vi.fn() }));

vi.mock("next/navigation", () => ({ useParams: () => ({ token: "rshr-test" }) }));
// 只挡取数;编号与标记映射保持真实实现——本文件钉的正是「正文编号 ⇔ 清单编号」
// 那条契约,把它 mock 掉就只剩渲染壳子了。
vi.mock("../../app/public-report.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/public-report.ts")>()),
  fetchPublicReport: mocks.fetchPublicReport,
}));

import PublicReportPage from "../../app/r/[token]/page";

const REPORT = {
  question: "LLM 有哪些架构？",
  content_md: [
    "## 结论",
    "",
    "Transformer 是主流[k1]，SSM 是另一条路线[k7]。",
    "",
    "| 模型 | 架构族 | 说明 |",
    "| --- | --- | --- |",
    "| LLaMA-3 | Transformer | 仅解码器 |",
    "",
    "```python",
    "print('x')",
    "```",
  ].join("\n"),
  created_at: "2026-08-06T12:00:00Z",
  updated_at: "2026-08-06T12:11:36Z",
  references: [
    { key: "k1", title: "甲文", file_name: "jia.pdf", location: "p. 2", snippet: "甲摘录" },
    { key: "k7", title: "乙文", file_name: "乙文", location: "", snippet: "乙摘录" },
  ],
  reference_count: 2,
  truncated_references: false,
};

beforeEach(() => {
  mocks.fetchPublicReport.mockResolvedValue(REPORT);
  // jsdom 不实现 scrollIntoView(浏览器有);不补上的话点引用编号会抛未捕获异常。
  Element.prototype.scrollIntoView = vi.fn();
});

test("正文 [k] 标记渲染成可点编号，并跳到编号一致的引用出处", async () => {
  const user = userEvent.setup();
  const { container } = render(<PublicReportPage />);

  // 编号取自 key 的序号（k7 → [7]），不是清单里的位置序号（那会是 [2]）。
  const chip = await screen.findByRole("button", { name: "[7]" });
  expect(chip).toHaveClass("cite-chip");
  expect(screen.queryByText("[k7]")).toBeNull();

  const entry = container.querySelector("#ref-k7");
  expect(entry).not.toBeNull();
  expect(entry!.querySelector(".public-report-refnum")!.textContent).toBe("7");
  expect(entry).not.toHaveClass("active");

  await user.click(chip);
  await waitFor(() => expect(container.querySelector("#ref-k7")).toHaveClass("active"));
  // 只滚动不移焦时,键盘/读屏用户点完仍停在正文按钮上,摘录读不到也 Tab 不到。
  expect(document.activeElement).toBe(container.querySelector("#ref-k7"));
});

test("宽表格与代码块走共用包装，才能在自己的内容块里横向滚动", async () => {
  const { container } = render(<PublicReportPage />);

  await screen.findByRole("table");
  // 少了这层 .answer-table-wrap(overflow-x:auto),宽表格会把整张卡片连同整页顶宽。
  const wrap = container.querySelector(".answer-table-wrap");
  expect(wrap).not.toBeNull();
  expect(wrap!.querySelector("table")).toHaveClass("answer-table");
  expect(container.querySelector("pre.answer-code")).not.toBeNull();
});

test("撤销或不存在的 token 给出可读的空态", async () => {
  mocks.fetchPublicReport.mockResolvedValue(null);
  render(<PublicReportPage />);

  expect(await screen.findByText("链接不可用")).toBeInTheDocument();
});

test("标题/原始文件名/摘录被截断时逐条显式披露，不静默丢尾", async () => {
  mocks.fetchPublicReport.mockResolvedValue({
    ...REPORT,
    references: [
      {
        key: "k1",
        title: "很长的标题前缀",
        file_name: "很长的文件名前缀.pdf",
        location: "p. 2",
        snippet: "很长的摘录前缀",
        title_truncated: true,
        file_name_truncated: true,
        snippet_truncated: true,
      },
    ],
    reference_count: 1,
  });
  render(<PublicReportPage />);

  expect(await screen.findByText("（标题过长，已截断）")).toBeInTheDocument();
  expect(screen.getByText("（原始文件名过长，已截断）")).toBeInTheDocument();
  expect(screen.getByText("（摘录过长，已截断）")).toBeInTheDocument();
});

test("研究问题被截断时页头显式披露（只可能出现在护栏上线前建的报告上）", async () => {
  mocks.fetchPublicReport.mockResolvedValue({ ...REPORT, question_truncated: true });
  render(<PublicReportPage />);

  expect(await screen.findByText("（研究问题过长，已截断）")).toBeInTheDocument();
});

test("没被截断的引用不挂假提示", async () => {
  // 空转保护：上一条可以被一个「恒渲染提示」的实现骗过去。REPORT 的两条引用都
  // 不带 `*_truncated`（旧后端也是这个形状），此时一个提示都不该出现。
  render(<PublicReportPage />);

  await screen.findByText("引用出处");
  expect(screen.queryByText("（标题过长，已截断）")).toBeNull();
  expect(screen.queryByText("（原始文件名过长，已截断）")).toBeNull();
  expect(screen.queryByText("（摘录过长，已截断）")).toBeNull();
  // REPORT 不带 `question_truncated`（旧后端也是这个形状），页头也不该挂提示。
  expect(screen.queryByText("（研究问题过长，已截断）")).toBeNull();
});

test("个人记忆引用（后端的结构化布尔 memory）保留标题与摘录，位置标签读作「作者的个人记忆」", async () => {
  mocks.fetchPublicReport.mockResolvedValue({
    ...REPORT,
    references: [
      // 后端没有位置标签可给时，标签也照样出现（不能因为 location 为空就整行消失）。
      { key: "k1", title: "我的笔记", file_name: "", location: "", snippet: "记忆摘录", memory: true },
      // 有位置标签的个人记忆：标签被统一换掉。
      { key: "k2", title: "另一条笔记", file_name: "", location: "第 2 页", snippet: "另一条摘录", memory: true },
      { key: "k7", title: "乙文", file_name: "乙文", location: "p. 3", snippet: "乙摘录" },
    ],
  });
  const { container } = render(<PublicReportPage />);

  await screen.findByText("引用出处");
  const memory = container.querySelector("#ref-k1")!;
  expect(memory.querySelector("strong")!.textContent).toBe("我的笔记");
  expect(memory.querySelector("blockquote")!.textContent).toBe("记忆摘录");
  expect(memory.querySelector(".public-report-locus")!.textContent).toBe("作者的个人记忆");
  expect(container.querySelector("#ref-k2 .public-report-locus")!.textContent).toBe("作者的个人记忆");
  // 不是个人记忆的引用：位置标签原样，也没有个人记忆标签。
  expect(container.querySelector("#ref-k7 .public-report-locus")!.textContent).toBe("p. 3");
});

test("只认结构化布尔：位置标签恰好叫 Memory 的普通文档引用不会被标成个人记忆", async () => {
  mocks.fetchPublicReport.mockResolvedValue({
    ...REPORT,
    references: [
      // 芯片手册的一级章节就叫 "Memory"——这是文档原文，不是作者的记忆。
      { key: "k1", title: "芯片手册", file_name: "chip.pdf", location: "Memory", snippet: "章节摘录" },
    ],
  });
  const { container } = render(<PublicReportPage />);

  await screen.findByText("引用出处");
  expect(container.querySelector("#ref-k1 .public-report-locus")!.textContent).toBe("Memory");
  expect(screen.queryByText("作者的个人记忆")).toBeNull();
});
