import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetchPublicConversation: vi.fn() }));

vi.mock("next/navigation", () => ({ useParams: () => ({ token: "ctok-test" }) }));
// 只挡取数；编号与标记映射、图片 URL 拼接保持真实实现——本文件钉的正是「正文编号 ⇔
// 清单编号」「附图走别名 URL」这两条契约，把它们 mock 掉就只剩渲染壳子了。
vi.mock("../../app/public-conversation.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/public-conversation.ts")>()),
  fetchPublicConversation: mocks.fetchPublicConversation,
}));

import PublicConversationPage from "../../app/c/[token]/page";

const CONVERSATION = {
  title: "关于 LLM 架构的一次问答",
  created_at: "2026-08-18T09:00:00Z",
  shared_at: "2026-08-18T09:30:00Z",
  truncated_turns: false,
  turns: [
    {
      question: "LLM 有哪些架构？",
      answer_md: [
        "Transformer 是主流[k1]，SSM 是另一条路线[k7]。",
        "",
        "| 模型 | 架构族 |",
        "| --- | --- |",
        "| LLaMA-3 | Transformer |",
        "",
        "```python",
        "print('x')",
        "```",
      ].join("\n"),
      asked_at: "2026-08-18T09:00:00Z",
      answered_at: "2026-08-18T09:01:00Z",
      evidence_level: "grounded",
      references: [
        { key: "k1", title: "甲文", file_name: "jia.pdf", location: "p. 2", snippet: "甲摘录" },
        { key: "k7", title: "乙文", file_name: "乙文", location: "", snippet: "乙摘录" },
      ],
      reference_count: 2,
      truncated_references: false,
      omitted_result_sets: 0,
      images: [{ alias: "alias-1", caption: "架构示意", reference_keys: ["k1"] }],
    },
    {
      question: "它们的区别是什么？",
      answer_md: "主要在于注意力机制[k1]。",
      asked_at: "2026-08-18T09:20:00Z",
      answered_at: "2026-08-18T09:21:00Z",
      evidence_level: "inferred",
      references: [
        { key: "k1", title: "丙文", file_name: "bing.pdf", location: "p. 5", snippet: "丙摘录" },
      ],
      reference_count: 1,
      truncated_references: false,
      // C-1：清单卡不进 v1，但绝不静默丢弃。
      omitted_result_sets: 2,
      images: [],
    },
    {
      question: "有示意图吗？",
      answer_md: "见下图。",
      asked_at: "2026-08-18T09:25:00Z",
      answered_at: "2026-08-18T09:26:00Z",
      evidence_level: "overview",
      references: [],
      reference_count: 0,
      truncated_references: false,
      omitted_result_sets: 0,
      images: [],
    },
  ],
};

beforeEach(() => {
  mocks.fetchPublicConversation.mockResolvedValue(CONVERSATION);
  // jsdom 不实现 scrollIntoView；不补上的话点引用编号会抛未捕获异常。
  Element.prototype.scrollIntoView = vi.fn();
});

test("多轮问答竖排渲染，每轮的问题与答案都在", async () => {
  render(<PublicConversationPage />);

  expect(await screen.findByText("LLM 有哪些架构？")).toBeInTheDocument();
  expect(screen.getByText("它们的区别是什么？")).toBeInTheDocument();
  expect(screen.getByText("有示意图吗？")).toBeInTheDocument();
  // 只读快照说明与轮数。
  expect(screen.getByText("这是只读快照")).toBeInTheDocument();
  expect(screen.getByText("3 轮问答")).toBeInTheDocument();
});

test("正文 [k] 标记编号取自 key 的序号，且每轮号段互相隔离", async () => {
  const { container } = render(<PublicConversationPage />);

  // 编号取自 key（k7 → [7]），不是清单里的位置序号（那会是 [2]）。
  const chip = await screen.findByRole("button", { name: "[7]" });
  expect(chip).toHaveClass("cite-chip");
  expect(screen.queryByText("[k7]")).toBeNull();

  // 第一轮的 k7 → ref-t0-k7，编号 7。
  const t0k7 = container.querySelector("#ref-t0-k7");
  expect(t0k7).not.toBeNull();
  expect(t0k7!.querySelector(".public-report-refnum")!.textContent).toBe("7");

  // 第二轮也有 k1 → ref-t1-k1，与第一轮的 ref-t0-k1 是两个不同 DOM id，不撞车。
  expect(container.querySelector("#ref-t0-k1")).not.toBeNull();
  const t1k1 = container.querySelector("#ref-t1-k1");
  expect(t1k1).not.toBeNull();
  expect(t1k1!.querySelector(".public-report-refnum")!.textContent).toBe("1");
});

test("宽表格与代码块走共用包装，才能在自己的内容块里横向滚动", async () => {
  const { container } = render(<PublicConversationPage />);

  await screen.findByRole("table");
  const wrap = container.querySelector(".answer-table-wrap");
  expect(wrap).not.toBeNull();
  expect(wrap!.querySelector("table")).toHaveClass("answer-table");
  expect(container.querySelector("pre.answer-code")).not.toBeNull();
});

test("附图在正文引用段落后渲染、只显示图片，并可页内放大", async () => {
  const user = userEvent.setup();
  const { container } = render(<PublicConversationPage />);

  const img = await screen.findByRole("img", { name: "架构示意" });
  // 别名地址，不是 asset_id：/public/conversations/<token>/assets/<alias>。
  expect(img.getAttribute("src")).toContain("/public/conversations/ctok-test/assets/alias-1");
  const region = img.closest(".answer-inline-images");
  expect(region).not.toBeNull();
  expect(region?.previousElementSibling?.tagName).toBe("P");
  expect(region?.textContent).toContain("本段附图");
  expect(region?.textContent).toContain("模型未直接读取图片");
  expect(region?.textContent).not.toContain("架构示意");

  const openButton = screen.getByRole("button", { name: "放大查看本段附图" });
  await user.click(openButton);
  const dialog = screen.getByRole("dialog", { name: "[1]附图预览" });
  expect(dialog).toBeInTheDocument();
  expect(within(dialog).getByRole("img", { name: "架构示意" })).toBeInTheDocument();
  expect(dialog.textContent).not.toContain("架构示意");
  await user.click(within(dialog).getByRole("button", { name: "关闭图片预览" }));
  expect(screen.queryByRole("dialog", { name: "[1]附图预览" })).toBeNull();
  expect(screen.getByRole("button", { name: "放大查看本段附图" })).toHaveFocus();
});

test("清单卡未公开时留一句可见说明，绝不静默丢弃", async () => {
  render(<PublicConversationPage />);

  expect(await screen.findByText(/未公开的清单内容（2 项）/)).toBeInTheDocument();
});

test("引用卡因标题和摘录都为空而被过滤时，reference_keys 仍能把图放到正文标记处", async () => {
  const user = userEvent.setup();
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    turns: [{
      ...CONVERSATION.turns[0],
      answer_md: "只保留图片位置 [k4]。",
      references: [],
      reference_count: 1,
      images: [{ alias: "alias-image-only", caption: "版图", reference_keys: ["k4"] }],
    }],
  });
  const { container } = render(<PublicConversationPage />);

  const img = await screen.findByRole("img", { name: "版图" });
  expect(img.closest(".answer-inline-images")?.previousElementSibling?.textContent).toContain("[4]");
  expect(img.closest(".answer-inline-images")?.textContent).toContain("[4]");
  expect(container.querySelector("#ref-t0-k4")).toBeNull();
  expect(screen.queryByRole("button", { name: "[4]" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "放大查看本段附图" }));
  expect(screen.getByRole("dialog", { name: "[4]附图预览" })).toBeInTheDocument();
});

test("旧公开载荷缺 reference_keys 时图片不消失，以明确未定位的 image-only 区块降级", async () => {
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    turns: [{
      ...CONVERSATION.turns[0],
      images: [{ alias: "legacy-alias", caption: "旧版架构图" }],
    }],
  });
  render(<PublicConversationPage />);

  const region = await screen.findByRole("complementary", { name: "本段附图（旧分享）" });
  expect(within(region).getByRole("img", { name: "旧版架构图" })).toBeInTheDocument();
  expect(region.textContent).toContain("旧分享未保留引用位置");
  expect(region.textContent).toContain("模型未直接读取图片");
  expect(region.textContent).not.toContain("旧版架构图");
});

test("公开引用本身是图片元素时不重复显示解析描述，普通文字引用仍显示摘录", async () => {
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    turns: [{
      ...CONVERSATION.turns[0],
      references: [
        { ...CONVERSATION.turns[0].references[0], snippet: "图片描述 blob", is_image_reference: true },
        { ...CONVERSATION.turns[0].references[1], snippet: "文字证据摘录", is_image_reference: false },
      ],
    }],
  });
  render(<PublicConversationPage />);

  await screen.findByText("文字证据摘录");
  expect(screen.queryByText("图片描述 blob")).toBeNull();
});

test("撤销或不存在的链接给出可读的空态", async () => {
  mocks.fetchPublicConversation.mockResolvedValue(null);
  render(<PublicConversationPage />);

  expect(await screen.findByText("链接不可用")).toBeInTheDocument();
});

test("标题/原始文件名/摘录被截断时逐条显式披露，不静默丢尾", async () => {
  // 与报告公开页同一条红线、同一套文案（`public-report-page.component.test.tsx`
  // 有逐字对应的一对）。这里补上是因为渲染这三条提示的分支此前没有守卫。
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    turns: [
      {
        ...CONVERSATION.turns[0],
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
      },
    ],
  });
  render(<PublicConversationPage />);

  expect(await screen.findByText("（标题过长，已截断）")).toBeInTheDocument();
  expect(screen.getByText("（原始文件名过长，已截断）")).toBeInTheDocument();
  expect(screen.getByText("（摘录过长，已截断）")).toBeInTheDocument();
});

test("没被截断的引用不挂假提示", async () => {
  // 空转保护：上一条可以被一个「恒渲染提示」的实现骗过去。
  render(<PublicConversationPage />);

  await screen.findByText("LLM 有哪些架构？");
  expect(screen.queryByText("（标题过长，已截断）")).toBeNull();
  expect(screen.queryByText("（原始文件名过长，已截断）")).toBeNull();
  expect(screen.queryByText("（摘录过长，已截断）")).toBeNull();
});
