import { render, screen, waitFor, within } from "@testing-library/react";
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

test("正文 [k] 标记渲染成可点编号，点开在标记旁弹出与站内同一张引用卡", async () => {
  const user = userEvent.setup();
  const { container } = render(<PublicReportPage />);

  // 编号取自 key 的序号（k7 → [7]），不是清单里的位置序号（那会是 [2]）。
  const chip = await screen.findByRole("button", { name: "[7]" });
  expect(chip).toHaveClass("cite-chip");
  expect(screen.queryByText("[k7]")).toBeNull();

  // 页底不再另列一份引用出处（与正文参考文献重复）：引用只看这张卡。
  expect(container.querySelector(".public-report-references")).toBeNull();

  await user.click(chip);
  const card = await screen.findByRole("dialog");
  expect(card).toHaveClass("cite-popover");
  expect(card.querySelector(".cite-detail-card")).not.toBeNull();
  expect(card).toHaveTextContent("[7]");
  expect(card).toHaveTextContent("乙文");
  expect(card).toHaveTextContent("乙摘录");
  expect(chip).toHaveClass("active");
  // 不把读者拽到页底。
  expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();

  // 与站内同一套关闭：点卡片外。
  await user.click(screen.getByText("LLM 有哪些架构？"));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

test("引用卡上显式披露截断，个人记忆读作「作者的个人记忆」", async () => {
  const user = userEvent.setup();
  mocks.fetchPublicReport.mockResolvedValue({
    ...REPORT,
    references: [
      {
        key: "k1", title: "我的笔记", file_name: "", location: "", snippet: "记忆摘录前缀",
        is_memory: true, snippet_truncated: true,
      },
      REPORT.references[1],
    ],
  });
  render(<PublicReportPage />);

  await user.click(await screen.findByRole("button", { name: "[1]" }));
  const card = await screen.findByRole("dialog");
  expect(card).toHaveTextContent("我的笔记");
  expect(card).toHaveTextContent("作者的个人记忆");
  expect(card).toHaveTextContent("（摘录过长，已截断）");
  expect(card).not.toHaveTextContent("（标题过长，已截断）");
});

test("只留正文的「参考文献」：默认收起、可展开，页底不再重复列「引用出处」", async () => {
  const user = userEvent.setup();
  mocks.fetchPublicReport.mockResolvedValue({
    ...REPORT,
    content_md: `${REPORT.content_md}\n\n## 参考文献\n\n- [k1] 甲文 · p. 2\n- [k7] 乙文\n\n## 局限\n\n证据有限。`,
  });
  const { container } = render(<PublicReportPage />);

  const heading = await screen.findByRole("heading", { name: "参考文献" });
  const bodyFold = heading.closest("details")!;
  expect(bodyFold).toHaveClass("report-references-fold");
  expect(bodyFold).not.toHaveAttribute("open");
  expect(bodyFold).not.toHaveTextContent("证据有限");
  expect(container.querySelectorAll("details")).toHaveLength(1);
  expect(screen.queryByText(/^引用出处/)).toBeNull();

  await user.click(heading);
  expect(bodyFold).toHaveAttribute("open");
  // 参考文献里的编号同样弹引用卡。
  await user.click(within(bodyFold).getByRole("button", { name: "[7]" }));
  expect(await screen.findByRole("dialog")).toHaveTextContent("乙摘录");
});

test("打印时展开收起的参考文献，打印后收回", async () => {
  mocks.fetchPublicReport.mockResolvedValue({
    ...REPORT,
    content_md: `${REPORT.content_md}\n\n## 参考文献\n\n- [k1] 甲文`,
  });
  render(<PublicReportPage />);
  const bodyFold = (await screen.findByRole("heading", { name: "参考文献" })).closest("details")!;

  window.dispatchEvent(new Event("beforeprint"));
  expect(bodyFold.open).toBe(true);
  window.dispatchEvent(new Event("afterprint"));
  expect(bodyFold.open).toBe(false);
});

test("引用出处被截断时页面上写明只收录前 N 条", async () => {
  mocks.fetchPublicReport.mockResolvedValue({ ...REPORT, truncated_references: true, reference_count: 2 });
  render(<PublicReportPage />);

  expect(await screen.findByText("引用出处过多，分享副本只收录前 2 条，其余引用编号无法点开查看。"))
    .toHaveClass("public-report-note");
});

test("没被截断时不挂截断说明", async () => {
  render(<PublicReportPage />);
  await screen.findByRole("button", { name: "[7]" });
  expect(screen.queryByText(/引用出处过多/)).toBeNull();
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

test("标题/原始文件名/摘录被截断时在引用卡上逐条显式披露，不静默丢尾", async () => {
  const user = userEvent.setup();
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

  await user.click(await screen.findByRole("button", { name: "[1]" }));
  const card = await screen.findByRole("dialog");
  expect(within(card).getByText("（标题过长，已截断）")).toBeInTheDocument();
  expect(within(card).getByText("（原始文件名过长，已截断）")).toBeInTheDocument();
  expect(within(card).getByText("（摘录过长，已截断）")).toBeInTheDocument();
});

test("研究问题被截断时页头显式披露（只可能出现在护栏上线前建的报告上）", async () => {
  mocks.fetchPublicReport.mockResolvedValue({ ...REPORT, question_truncated: true });
  render(<PublicReportPage />);

  expect(await screen.findByText("（研究问题过长，已截断）")).toBeInTheDocument();
});

test("没被截断的引用不挂假提示", async () => {
  // 空转保护：上一条可以被一个「恒渲染提示」的实现骗过去。REPORT 的两条引用都
  // 不带 `*_truncated`（旧后端也是这个形状），此时一个提示都不该出现。
  const user = userEvent.setup();
  render(<PublicReportPage />);

  await user.click(await screen.findByRole("button", { name: "[1]" }));
  await screen.findByRole("dialog");
  expect(screen.queryByText("（标题过长，已截断）")).toBeNull();
  expect(screen.queryByText("（原始文件名过长，已截断）")).toBeNull();
  expect(screen.queryByText("（摘录过长，已截断）")).toBeNull();
  // REPORT 不带 `question_truncated`（旧后端也是这个形状），页头也不该挂提示。
  expect(screen.queryByText("（研究问题过长，已截断）")).toBeNull();
});

test("个人记忆引用（后端的结构化布尔 is_memory）保留标题与摘录，位置标签读作「作者的个人记忆」", async () => {
  const user = userEvent.setup();
  mocks.fetchPublicReport.mockResolvedValue({
    ...REPORT,
    content_md: "甲[k1]，乙[k2]，丙[k7]。",
    references: [
      // 后端没有位置标签可给时，标签也照样出现（不能因为 location 为空就整行消失）。
      { key: "k1", title: "我的笔记", file_name: "", location: "", snippet: "记忆摘录", is_memory: true },
      // 有位置标签的个人记忆：标签被统一换掉。
      { key: "k2", title: "另一条笔记", file_name: "", location: "第 2 页", snippet: "另一条摘录", is_memory: true },
      { key: "k7", title: "乙文", file_name: "乙文", location: "p. 3", snippet: "乙摘录" },
    ],
  });
  render(<PublicReportPage />);

  const cardFor = async (label: string) => {
    await user.click(await screen.findByRole("button", { name: label }));
    return screen.findByRole("dialog");
  };
  const memory = await cardFor("[1]");
  expect(memory.querySelector("h4")!.textContent).toBe("我的笔记");
  expect(memory).toHaveTextContent("记忆摘录");
  expect(memory).toHaveTextContent("作者的个人记忆");
  await waitFor(async () => expect(await cardFor("[2]")).toHaveTextContent("另一条笔记"));
  expect(screen.getByRole("dialog")).toHaveTextContent("作者的个人记忆");
  expect(screen.getByRole("dialog")).not.toHaveTextContent("第 2 页");
  // 不是个人记忆的引用：位置标签原样，也没有个人记忆标签。
  await waitFor(async () => expect(await cardFor("[7]")).toHaveTextContent("乙摘录"));
  expect(screen.getByRole("dialog")).toHaveTextContent("p. 3");
  expect(screen.getByRole("dialog")).not.toHaveTextContent("作者的个人记忆");
});

test("只认结构化布尔：位置标签恰好叫 Memory 的普通文档引用不会被标成个人记忆", async () => {
  const user = userEvent.setup();
  mocks.fetchPublicReport.mockResolvedValue({
    ...REPORT,
    references: [
      // 芯片手册的一级章节就叫 "Memory"——这是文档原文，不是作者的记忆。
      { key: "k1", title: "芯片手册", file_name: "chip.pdf", location: "Memory", snippet: "章节摘录" },
    ],
  });
  render(<PublicReportPage />);

  await user.click(await screen.findByRole("button", { name: "[1]" }));
  const card = await screen.findByRole("dialog");
  expect(card).toHaveTextContent("chip.pdf · Memory");
  expect(screen.queryByText(/作者的个人记忆/)).toBeNull();
});

test("分享页只显示失败章节的中性说明，不显示失败原因", async () => {
  // 公开接口本就不返回 sections;即便响应里混进了带原文的字段,页面也不读它。
  mocks.fetchPublicReport.mockResolvedValueOnce({
    ...REPORT,
    content_md: "## A\n\n（本节生成失败:本节处理过程出错）\n\n## B\n\n正文 B",
    sections: [{ title: "A", failed: true, error: "OperationalError: 10.9.8.7" }],
  });
  render(<PublicReportPage />);

  expect(await screen.findByText(/本节处理过程出错/)).toBeInTheDocument();
  expect(screen.queryByText(/10\.9\.8\.7/)).toBeNull();
  expect(screen.queryByText(/失败原因/)).toBeNull();
});

