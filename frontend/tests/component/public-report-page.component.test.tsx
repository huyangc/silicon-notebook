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
