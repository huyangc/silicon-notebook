import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetchPublicConversation: vi.fn() }));

vi.mock("next/navigation", () => ({ useParams: () => ({ token: "ctok-test" }) }));
// 只挡取数；编号与标记映射、图片 URL 拼接保持真实实现——本文件钉的正是「正文编号 ⇔
// 清单编号」「附图走别名 URL」这两条契约，把它们 mock 掉就只剩渲染壳子了。
vi.mock("../../app/public-conversation.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/public-conversation.ts")>()),
  // 旧用例直接给会话对象/null；这里统一包成页面现在消费的结果形状。
  fetchPublicConversation: async (...args: unknown[]) => {
    const value = await (mocks.fetchPublicConversation as (...a: unknown[]) => Promise<unknown>)(...args);
    if (value && typeof value === "object" && "kind" in value) return value;
    return value ? { kind: "ready", conversation: value } : { kind: "missing" };
  },
}));
// 行为不变的透传 spy：normalizeMathMarkdown 只在 markdown 解析真正重跑时被调用，
// 调用次数就是「remark+KaTeX+rehype 管线跑了几次」的账本。
vi.mock("../../app/math-markdown.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/math-markdown.ts")>();
  return { ...actual, normalizeMathMarkdown: vi.fn(actual.normalizeMathMarkdown) };
});

import PublicConversationPage from "../../app/c/[token]/page";
import { claimEscapePress } from "../../app/use-root-modal-coordinator";
import { normalizeMathMarkdown } from "../../app/math-markdown";

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

test("公开分享中完整性提示不会被未闭合的代码围栏吞掉", async () => {
  const notice = "本次回答未验证完整性，不能视为全部结果。";
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    turns: [{
      ...CONVERSATION.turns[0],
      answer_md: `示例\n\n\`\`\`text\n未闭合\n\n> ${notice}`,
      completeness_notice: notice,
      references: [],
      images: [],
    }],
  });
  const { container } = render(<PublicConversationPage />);

  const visibleNotice = await screen.findByText(notice);
  const code = container.querySelector(".public-turn-answer pre code");
  expect(code).toHaveTextContent("未闭合");
  expect(code).not.toHaveTextContent(notice);
  expect(visibleNotice).toHaveClass("answer-completeness-notice");
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

test("点正文引用在标记旁弹出与站内同一张引用卡，清单默认折叠", async () => {
  const user = userEvent.setup();
  const { container } = render(<PublicConversationPage />);

  const chip = await screen.findByRole("button", { name: "[7]" });
  // 每轮的引用清单折叠在回答下方，不再铺满整屏。
  const lists = container.querySelectorAll("details.public-references-fold");
  expect(lists).toHaveLength(2);
  lists.forEach((list) => expect(list).not.toHaveAttribute("open"));
  expect(lists[0].querySelector("summary")).toHaveTextContent("引用出处 · 2 条");

  await user.click(chip);
  const card = await screen.findByRole("dialog");
  expect(card).toHaveClass("cite-popover");
  expect(card).toHaveTextContent("[7]");
  expect(card).toHaveTextContent("乙文");
  expect(card).toHaveTextContent("乙摘录");
  expect(within(card).queryAllByRole("link")).toHaveLength(0);
  expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();

  // 第二轮自己的号段：它的 [1] 是丙文，不是第一轮的甲文。
  await user.click(screen.getAllByRole("button", { name: "[1]" })[1]);
  await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("丙文"));
  expect(screen.getAllByRole("dialog")).toHaveLength(1);

  fireEvent.keyDown(window, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.keyUp(window, { key: "Escape" });
});

test("整页只有一张引用卡：键盘从一轮的标记换到另一轮的标记，前一张收起", async () => {
  const user = userEvent.setup();
  render(<PublicConversationPage />);

  const first = await screen.findByRole("button", { name: "[7]" });
  first.focus();
  await user.keyboard("{Enter}");
  expect(await screen.findByRole("dialog")).toHaveTextContent("乙文");

  // Tab/聚焦到第二轮的 [1] 再按 Enter：没有 pointerdown，也没有滚动。
  screen.getAllByRole("button", { name: "[1]" })[1].focus();
  await user.keyboard("{Enter}");
  await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("丙文"));
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(first).not.toHaveClass("active");
});

test("多轮问答右缘有提问导航圆点，点圆点跳到对应那一轮", async () => {
  const user = userEvent.setup();
  render(<PublicConversationPage />);

  const nav = await screen.findByRole("navigation", { name: "本次会话的提问" });
  const dots = within(nav).getAllByRole("button");
  expect(dots).toHaveLength(3);
  expect(dots[1]).toHaveAccessibleName("它们的区别是什么？");

  await user.click(dots[1]);
  const scroll = vi.mocked(Element.prototype.scrollIntoView);
  expect(scroll).toHaveBeenCalled();
  expect(scroll.mock.contexts.at(-1)).toBe(document.getElementById("chat-turn-1"));
  expect(document.getElementById("chat-turn-1")).toHaveTextContent("它们的区别是什么？");
  expect(dots[1]).toHaveAttribute("aria-current", "true");
});

test("只有一轮的分享没有提问导航", async () => {
  mocks.fetchPublicConversation.mockResolvedValue({ ...CONVERSATION, turns: [CONVERSATION.turns[0]] });
  render(<PublicConversationPage />);

  await screen.findByText("LLM 有哪些架构？");
  expect(screen.queryByRole("navigation", { name: "本次会话的提问" })).toBeNull();
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
  render(<PublicConversationPage />);

  const img = await screen.findByRole("img", { name: "架构示意" });
  // 别名地址，不是 asset_id：/public/conversations/<token>/assets/<alias>。
  expect(img.getAttribute("src")).toContain("/public/conversations/ctok-test/assets/alias-1");
  const region = img.closest(".answer-inline-images");
  expect(region).not.toBeNull();
  expect(screen.getByRole("complementary", { name: "引用图片 [1]" })).toBe(region);
  expect(region?.previousElementSibling?.tagName).toBe("P");
  expect(region?.textContent).toContain("引用 [1]");
  expect(region?.textContent).toContain("模型未直接读取图片");
  expect(region?.textContent).not.toContain("架构示意");

  const openButton = screen.getByRole("button", { name: "放大查看本段附图" });
  await user.click(openButton);
  const dialog = screen.getByRole("dialog", { name: "引用 [1] 图片预览" });
  expect(dialog).toBeInTheDocument();
  expect(within(dialog).getByRole("img", { name: "架构示意" })).toBeInTheDocument();
  expect(dialog.textContent).not.toContain("架构示意");
  await user.click(within(dialog).getByRole("button", { name: "关闭图片预览" }));
  expect(screen.queryByRole("dialog", { name: "引用 [1] 图片预览" })).toBeNull();
  expect(screen.getByRole("button", { name: "放大查看本段附图" })).toHaveFocus();
});

// Esc 关闭预览认领这一次按键（与站内同一本账）：按住 Esc 也只关这一层。
test("Esc 关闭预览时认领这一次按键，同一次按住里别的层拿不到名额", async () => {
  const user = userEvent.setup();
  render(<PublicConversationPage />);
  await screen.findByRole("img", { name: "架构示意" });
  await user.click(screen.getByRole("button", { name: "放大查看本段附图" }));
  const dialog = screen.getByRole("dialog", { name: "引用 [1] 图片预览" });

  fireEvent.keyDown(within(dialog).getByRole("button", { name: "关闭图片预览" }), { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "引用 [1] 图片预览" })).toBeNull();
  // 这一次按键已被预览认领：同一次按住里别的层再认领拿不到名额。
  expect(claimEscapePress()).toBe(false);
  fireEvent.keyUp(window, { key: "Escape" });
});

// 渲染器组件表若在每次渲染时重建，React 会把整棵 markdown 子树（含附图区）卸载重挂：
// 一帧删除 + 一帧插入，用户看到的就是附图闪烁与文字重排（主问答页 c5649a88/02ceda29
// 修掉的同型缺陷）。该页无输入框，触发面是点选引用与开关图片预览这两类状态变化。
// DOM 节点身份钉「不重挂」；normalizeMathMarkdown 调用数钉「解析管线没重跑」——与
// answer-citation-images 用 citationImageOrder 账本身份断言是同一套证据思路，此处
// 账本在组件内部拿不到，改用行为等价的解析入口计数。
test("点选引用与开关图片预览复用已解析的 markdown 子树，附图与正文不重挂", async () => {
  const user = userEvent.setup();
  render(<PublicConversationPage />);

  const img = await screen.findByRole("img", { name: "架构示意" });
  const paragraph = screen.getByText(/Transformer 是主流/).closest("p");
  const parseRuns = vi.mocked(normalizeMathMarkdown).mock.calls.length;

  // 点选正文引用：高亮态必须经 Context 穿透被复用的子树到达徽章（同一个 DOM 按钮
  // 原地变 active），而不是靠重建子树刷出来。
  const chip = screen.getByRole("button", { name: "[7]" });
  await user.click(chip);
  expect(screen.getByRole("button", { name: "[7]" })).toBe(chip);
  expect(chip).toHaveClass("active");
  expect(screen.getByRole("img", { name: "架构示意" })).toBe(img);
  expect(screen.getByText(/Transformer 是主流/).closest("p")).toBe(paragraph);

  // 开关图片预览同理：预览是 markdown 子树之外的弹层，正文与附图必须原地保留。
  await user.click(screen.getByRole("button", { name: "放大查看本段附图" }));
  await user.click(screen.getByRole("button", { name: "关闭图片预览" }));
  expect(screen.getByRole("img", { name: "架构示意" })).toBe(img);
  expect(screen.getByText(/Transformer 是主流/).closest("p")).toBe(paragraph);

  // 上面四次状态变化没有一次触发重新解析。
  expect(vi.mocked(normalizeMathMarkdown).mock.calls.length).toBe(parseRuns);
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
  expect(screen.getByRole("dialog", { name: "引用 [4] 图片预览" })).toBeInTheDocument();
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

  const region = await screen.findByRole("complementary", { name: "引用图片（旧分享）" });
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

// 外部证据（`ask.reflect_action`，设计文档 §七 / §九 不变量 4）：库外引用在公开页
// 必须一眼分得出来，但**只有标记、没有链接**——公开投影刻意不下发 url（匿名读者
// 能不能跟随部署外链是另一件事，见 §十一 第 7 条）。
test("is_external 的引用带「外部」标记，且公开页不给它渲染任何链接", async () => {
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    turns: [{
      ...CONVERSATION.turns[0],
      references: [
        { key: "k1", title: "库内甲文", file_name: "jia.pdf", location: "p. 2", snippet: "甲摘录" },
        {
          key: "k7",
          title: "库外乙文",
          file_name: "",
          location: "§3.2",
          snippet: "库外摘录",
          is_external: true,
        },
      ],
    }],
  });
  render(<PublicConversationPage />);

  const external = (await screen.findByText("库外乙文")).closest("li");
  expect(external).not.toBeNull();
  expect(within(external!).getByText("外部")).toBeInTheDocument();
  // 标记归标记，摘录照常显示（它不是 is_image_reference 那种要压掉正文的情形）。
  expect(within(external!).getByText("库外摘录")).toBeInTheDocument();

  // 不渲染链接：整条引用清单里一个 <a> 都不该有（后端根本没下发 url，
  // 别让哪天有人「顺手」补一个）。
  const list = external!.closest("ol");
  expect(within(list!).queryAllByRole("link")).toHaveLength(0);
});

test("库内引用不挂「外部」标记（空转保护）", async () => {
  render(<PublicConversationPage />);

  await screen.findByText("甲文");
  expect(screen.queryByText("外部")).toBeNull();
});

test("个人记忆引用（后端的结构化布尔 is_memory）保留标题与摘录，位置标签读作「作者的个人记忆」", async () => {
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    turns: [{
      ...CONVERSATION.turns[0],
      references: [
        // 后端没有位置标签可给时，标签也照样出现（不能因为 location 为空就整行消失）。
        { key: "k1", title: "我的笔记", file_name: "", location: "", snippet: "记忆摘录", is_memory: true },
        { key: "k2", title: "另一条笔记", file_name: "", location: "第 2 页", snippet: "另一条摘录", is_memory: true },
        { key: "k7", title: "乙文", file_name: "乙文", location: "p. 3", snippet: "乙摘录" },
      ],
    }],
  });
  const { container } = render(<PublicConversationPage />);

  await screen.findByText("LLM 有哪些架构？");
  const memory = container.querySelector("#ref-t0-k1");
  expect(memory).not.toBeNull();
  expect(memory!.querySelector("strong")!.textContent).toBe("我的笔记");
  expect(memory!.querySelector("blockquote")!.textContent).toBe("记忆摘录");
  expect(memory!.querySelector(".public-report-locus")!.textContent).toBe("作者的个人记忆");
  expect(container.querySelector("#ref-t0-k2 .public-report-locus")!.textContent).toBe("作者的个人记忆");
  expect(container.querySelector("#ref-t0-k7 .public-report-locus")!.textContent).toBe("p. 3");
});

test("只认结构化布尔：位置标签恰好叫 Memory 的普通文档引用不会被标成个人记忆", async () => {
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    turns: [{
      ...CONVERSATION.turns[0],
      references: [
        { key: "k1", title: "芯片手册", file_name: "chip.pdf", location: "Memory", snippet: "章节摘录" },
      ],
    }],
  });
  const { container } = render(<PublicConversationPage />);

  await screen.findByText("LLM 有哪些架构？");
  expect(container.querySelector("#ref-t0-k1 .public-report-locus")!.textContent).toBe("Memory");
  expect(screen.queryByText("作者的个人记忆")).toBeNull();
});

test("限时回答链接过期（410）读作「链接已过期，请重新提问」", async () => {
  mocks.fetchPublicConversation.mockResolvedValue({ kind: "expired" });
  render(<PublicConversationPage />);

  expect(await screen.findByText("链接已过期")).toBeInTheDocument();
  expect(screen.getByText("链接已过期，请重新提问。")).toBeInTheDocument();
});

test("带 expires_at 的回答链接头部写明失效时间，会话分享页不写", async () => {
  const expiresAt = "2030-01-02T03:04:05Z";
  mocks.fetchPublicConversation.mockResolvedValue({ ...CONVERSATION, expires_at: expiresAt });
  const { unmount } = render(<PublicConversationPage />);

  await screen.findByText("LLM 有哪些架构？");
  const d = new Date(expiresAt);
  const pad = (n: number) => String(n).padStart(2, "0");
  const local = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  // 时间由挂载后的 effect 填入，等它落地再断言。
  await waitFor(() => expect(screen.getByText(/此链接将于/)).toHaveTextContent(`此链接将于 ${local} 失效`));
  expect(screen.getByText("问答回答 · 只读分享")).toBeInTheDocument();
  unmount();

  mocks.fetchPublicConversation.mockResolvedValue(CONVERSATION);
  render(<PublicConversationPage />);
  await screen.findByText("LLM 有哪些架构？");
  expect(screen.queryByText(/此链接将于/)).toBeNull();
  expect(screen.getByText("问答会话 · 只读分享")).toBeInTheDocument();
});

test("公开范围比公开页长：在第一轮之前说明只展示最近的几轮、更早的几轮未包含（确数）", async () => {
  mocks.fetchPublicConversation.mockResolvedValue({
    ...CONVERSATION,
    truncated_turns: true,
    omitted_earlier_turns: 120,
  });
  const { container } = render(<PublicConversationPage />);
  const note = await screen.findByText("会话过长，此处只展示最近 3 轮，更早的 120 轮未包含。");
  // 被省掉的是最早的那些轮次，说明排在第一轮之前。
  const firstQuestion = screen.getByText("LLM 有哪些架构？");
  expect(note.compareDocumentPosition(firstQuestion) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(container.textContent).not.toContain("只展示前");
});

test("普通长度的会话没有任何截断说明", async () => {
  mocks.fetchPublicConversation.mockResolvedValue(CONVERSATION);
  render(<PublicConversationPage />);
  await screen.findByText("LLM 有哪些架构？");
  expect(screen.queryByText(/会话过长/)).toBeNull();
});
