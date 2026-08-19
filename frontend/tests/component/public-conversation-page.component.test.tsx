import { render, screen } from "@testing-library/react";
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
      images: [],
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
      // 附图可能没有对应引用卡（被丢弃的空引用卡若带图仍发别名）：「本段附图」是
      // turn 级展示，不依赖引用卡存在。
      question: "有示意图吗？",
      answer_md: "见下图。",
      asked_at: "2026-08-18T09:25:00Z",
      answered_at: "2026-08-18T09:26:00Z",
      evidence_level: "overview",
      references: [],
      reference_count: 0,
      truncated_references: false,
      omitted_result_sets: 0,
      images: [{ alias: "alias-1", caption: "架构示意" }],
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

test("附图渲染成图片、走别名地址、并标注「本段附图」，即使该轮没有引用卡", async () => {
  const { container } = render(<PublicConversationPage />);

  const img = await screen.findByRole("img", { name: "架构示意" });
  // 别名地址，不是 asset_id：/public/conversations/<token>/assets/<alias>。
  expect(img.getAttribute("src")).toContain("/public/conversations/ctok-test/assets/alias-1");
  // 必须与引证内容视觉区分、标注它不是模型引用过的证据。
  expect(screen.getByText(/本段附图/)).toBeInTheDocument();
  // 该轮没有引用卡，但附图照常渲染（turn 级，不依赖引用卡）。
  const figure = img.closest(".public-turn-images");
  expect(figure).not.toBeNull();
});

test("清单卡未公开时留一句可见说明，绝不静默丢弃", async () => {
  render(<PublicConversationPage />);

  expect(await screen.findByText(/未公开的清单内容（2 项）/)).toBeInTheDocument();
});

test("撤销或不存在的链接给出可读的空态", async () => {
  mocks.fetchPublicConversation.mockResolvedValue(null);
  render(<PublicConversationPage />);

  expect(await screen.findByText("链接不可用")).toBeInTheDocument();
});
