// 全局问答终态引用核对的呈现(PR-D,Q3「部分失败而非整份作废」)。
//
// 钉住的契约:
//   · 回答照常整份上屏,答案下方一句按原因与条数拼的说明(只在 failed > 0 时出现);
//   · 带标记的引用卡留在列表里、保留摘录、显示原因行,且**不渲染**任何打开原文/
//     图谱/Knowhow/图片/笔记本/外链的入口(不是渲染成禁用);
//   · 行内标记换弱化样式、原因进可访问名称,点开是引用卡的原因;
//   · 带标记引用名下的内联图片跳过;
//   · 没有任何核对字段的回答与今天逐字节相同;
//   · 公开页用过去时。
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeAll, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetchPublicConversation: vi.fn(), assetBlob: vi.fn() }));

vi.mock("next/navigation", () => ({ useParams: () => ({ token: "ctok-test" }) }));
vi.mock("../../app/public-conversation.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/public-conversation.ts")>()),
  fetchPublicConversation: mocks.fetchPublicConversation,
}));
vi.mock("../../app/source-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/source-api")>()),
  fetchInternalAssetBlob: mocks.assetBlob,
}));

import { AnswerView } from "../../app/answer-panel";
import { SelectedReferenceDetail } from "../../app/citation-card";
import { ActivityDetail } from "../../app/dev/logs/activity/ActivityDetail";
import type { ActivityAsk } from "../../app/dev/logs/activity/types";
import PublicConversationPage from "../../app/c/[token]/page";
import type { AnswerAnchor, AskResponse, Citation } from "../../app/workspace-model";

beforeAll(() => {
  if (typeof URL.createObjectURL !== "function") URL.createObjectURL = vi.fn(() => "blob:mock-url");
  if (typeof URL.revokeObjectURL !== "function") URL.revokeObjectURL = vi.fn();
  // jsdom 不实现 scrollIntoView；公开页点引用编号会滚到清单里那一条。
  Element.prototype.scrollIntoView = vi.fn();
});

beforeEach(() => {
  mocks.assetBlob.mockResolvedValue(new Blob(["fake-image-bytes"], { type: "image/png" }));
});

// `knowhow` 在线上 wire(answer-formatting.ts AnswerAnchorLike)里有,workspace-model 的
// AnswerAnchor 至今没声明它——与 answer-panel-readonly 用例同一个已知差异。
type AnchorOverrides = Partial<AnswerAnchor> & { knowhow?: { table_id: string; row_id: string } | null };
const anchor = (overrides: AnchorOverrides = {}): AnswerAnchor => ({
  key: "k1", object_id: "obj-1", object_type: "concept", label: "低温容量",
  name: "低温容量", snippet: "低温环境下，容量下降。", source_title: "测试记录",
  location_label: "第 2 页", source_id: "source-1", element_id: "element-1",
  notebook_id: "nb-0", tier: "personal",
  images: [{ element_id: "img-el-1", asset_id: "asset-1", caption: "图 1：容量曲线" }],
  knowhow: { table_id: "table-1", row_id: "row-1" },
  ...overrides,
} as AnswerAnchor);

function answerWith(anchors: AnswerAnchor[], extra: Partial<AskResponse> = {}): AskResponse {
  return {
    answer_id: "answer-1", conversation_id: "conv-1",
    conclusion: "结论。", answer: "低温下容量下降 [k1]。第二个事实 [k2]。", grounded: true,
    anchors, related_knowledge: [], citations: [], llm_mode: "reasoning",
    ...extra,
  };
}

// 每一个出口都给承接方:证明「不渲染」来自核对结果,而不是来自没传回调。
function renderAnswer(answer: AskResponse) {
  return render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      notebookId={null}
      notebookNames={{ "nb-0": "材料研究" }}
      notebookHref={(notebookId, sourceId) => `/#notebook=${notebookId}&source=${sourceId}`}
      onOpenNotebook={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      onOpenSource={() => undefined}
      onPreviewImage={() => undefined}
      buildingScaleIndex={false}
      memorySaved={false}
    />,
  );
}

const flaggedAnswer = () => answerWith(
  [
    anchor({ verification: "changed" }),
    anchor({
      key: "k2", object_id: "obj-2", label: "第二个事实", name: "第二个事实",
      snippet: "第二段摘录。", element_id: "element-2",
      images: [{ element_id: "img-el-2", asset_id: "asset-2", caption: "图 2：通过核对的图" }],
      knowhow: null,
    }),
  ],
  {
    grounded: false, evidence_level: "overview",
    citation_check: { outcome: "partial", checked: 2, failed: 1, changed: 1, source_gone: 0, unverifiable: 0 },
  },
);

test("a partially failed check keeps the whole answer and adds one truthful notice under it", async () => {
  const { container } = renderAnswer(flaggedAnswer());
  // 正文一个字不少。
  expect(container.querySelector(".answer-markdown")).toHaveTextContent("低温下容量下降 [1]。第二个事实 [2]。");
  const notice = container.querySelector(".answer-citation-check-notice")!;
  expect(notice).toHaveAttribute("role", "note");
  expect(notice).toHaveTextContent(
    "本次回答有部分引用未通过核对：1 条原文已改动。回答内容照常保留，带标记的引用可点开查看原因。",
  );
  // 说明落在正文之后。
  const body = container.querySelector(".answer-markdown")!;
  expect(body.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test.each([
  [{ failed: 3, changed: 2, source_gone: 1, unverifiable: 0 }, "2 条原文已改动、1 条资料已删除"],
  [{ failed: 1, changed: 0, source_gone: 1, unverifiable: 0 }, "1 条资料已删除"],
  [{ failed: 2, changed: 0, source_gone: 0, unverifiable: 2 }, "2 条无法核对"],
  [{ failed: 3, changed: 1, source_gone: 1, unverifiable: 1 }, "1 条原文已改动、1 条资料已删除、1 条无法核对"],
])("notice text for reason counts %o", (counts, reasons) => {
  const { container } = renderAnswer(answerWith([anchor()], {
    citation_check: { outcome: "partial", checked: 5, ...counts },
  }));
  expect(container.querySelector(".answer-citation-check-notice")).toHaveTextContent(
    `本次回答有部分引用未通过核对：${reasons}。回答内容照常保留，带标记的引用可点开查看原因。`,
  );
});

test("a check summary with failed = 0 renders no notice", () => {
  const { container } = renderAnswer(answerWith([anchor()], {
    citation_check: { outcome: "passed", checked: 2, failed: 0, changed: 0, source_gone: 0, unverifiable: 0 },
  }));
  expect(container.querySelector(".answer-citation-check-notice")).toBeNull();
});

test("the flagged marker is muted, names its reason, and opens a card that keeps the excerpt but no way to the source", async () => {
  const { container } = renderAnswer(flaggedAnswer());
  const flaggedMarker = screen.getByRole("button", { name: "[1] 未通过核对：原文已改动" });
  expect(flaggedMarker).toHaveClass("cite-chip", "cite-chip-unverified");
  expect(flaggedMarker).toHaveAttribute("title", "[1] 未通过核对：原文已改动");
  // 通过核对的那一枚不受影响。
  const cleanMarker = screen.getByRole("button", { name: "[2]" });
  expect(cleanMarker.className).toBe("cite-chip");
  expect(cleanMarker).not.toHaveAttribute("title");

  fireEvent.click(flaggedMarker);
  const popover = await screen.findByRole("dialog");
  const card = popover.querySelector(".cite-detail-card")!;
  expect(card).toHaveClass("is-unverified");
  const reason = within(popover).getByRole("note");
  expect(reason).toHaveTextContent("未通过核对：原文已改动");
  // 摘录与出处照常留着。
  expect(popover).toHaveTextContent("低温环境下，容量下降。");
  expect(popover).toHaveTextContent("测试记录 · 第 2 页");
  // 所有「去原处看」的入口一律不渲染（不是禁用）。
  expect(within(popover).queryByRole("button", { name: /知识图谱|查看原文|在表格中查看|放大查看/ })).toBeNull();
  expect(within(popover).queryByRole("link")).toBeNull();
  expect(within(popover).queryByRole("img")).toBeNull();
  expect(popover.querySelector(".cite-detail-images")).toBeNull();
  expect(popover.querySelectorAll("button")).toHaveLength(0);
  expect(flaggedMarker).toHaveAttribute("aria-expanded", "true");
  // 卡片没被删：清单里仍然是两条引用，编号不跳。
  expect(container.querySelectorAll(".cite-chip")).toHaveLength(2);
});

test("the clean card in the same answer still offers every exit", async () => {
  renderAnswer(flaggedAnswer());
  fireEvent.click(screen.getByRole("button", { name: "[2]" }));
  const popover = await screen.findByRole("dialog");
  expect(within(popover).queryByRole("note")).toBeNull();
  expect(within(popover).getByRole("button", { name: /知识图谱/ })).toBeInTheDocument();
  expect(within(popover).getByRole("button", { name: /查看原文/ })).toBeInTheDocument();
  expect(within(popover).getByRole("link", { name: /打开笔记本/ })).toBeInTheDocument();
});

test("each reason label reaches the card", async () => {
  for (const [verification, text] of [
    ["source_gone", "资料已删除"],
    ["unverifiable", "无法核对"],
  ] as const) {
    const view = renderAnswer(answerWith([anchor({ verification })]));
    fireEvent.click(screen.getByRole("button", { name: `[1] 未通过核对：${text}` }));
    const popover = await screen.findByRole("dialog");
    expect(within(popover).getByRole("note")).toHaveTextContent(`未通过核对：${text}`);
    view.unmount();
  }
});

test("a flagged citation on the no-anchor fallback path keeps its quoted span and loses every exit", () => {
  const citation: Citation = {
    label: "测试记录", source_id: "source-1", element_id: "element-1",
    location_label: "第 2 页", quoted_span: "回答时的摘录。", notebook_id: "nb-0",
    knowhow: { table_id: "table-1", row_id: "row-1" },
    images: [{ element_id: "img-el-1", asset_id: "asset-1", caption: "图 1" }],
    verification: "source_gone",
  } as Citation;
  const { container } = render(
    <SelectedReferenceDetail
      reference={{ id: "citation:source-1:element-1:0", displayLabel: "[1]", citation }}
      notebookId={null}
      notebookNames={{}}
      notebookHref={() => "/#notebook=nb-0"}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      onOpenSource={() => undefined}
      onPreviewImage={() => undefined}
    />,
  );
  expect(screen.getByRole("note")).toHaveTextContent("未通过核对：资料已删除");
  expect(container).toHaveTextContent("回答时的摘录。");
  expect(container.querySelectorAll("button, a, img")).toHaveLength(0);
});

test("inline images of a flagged citation are skipped; a clean citation keeps its images", async () => {
  renderAnswer(flaggedAnswer());
  const region = await screen.findByRole("complementary", { name: "引用图片 [2]" });
  expect(await within(region).findByRole("img", { name: "图 2：通过核对的图" })).toBeInTheDocument();
  expect(screen.queryByRole("complementary", { name: /引用图片 \[1\]/ })).toBeNull();
  expect(screen.queryByRole("img", { name: "图 1：容量曲线" })).toBeNull();
  await waitFor(() => expect(mocks.assetBlob).toHaveBeenCalledTimes(1));
});

test("an answer with no verification fields renders exactly as before", () => {
  const plain = answerWith([
    anchor(),
    anchor({ key: "k2", object_id: "obj-2", element_id: "element-2", images: [], knowhow: null }),
  ]);
  const { container } = renderAnswer(plain);
  expect(container.querySelector(".answer-citation-check-notice")).toBeNull();
  expect(container.querySelector(".cite-chip-unverified")).toBeNull();
  const marker = screen.getByRole("button", { name: "[1]" });
  expect(marker.outerHTML).toBe('<button type="button" aria-expanded="false" class="cite-chip">[1]</button>');
});

test("the admin activity / question detail (AnswerView reuse) shows the notice and the flagged card", async () => {
  const item: ActivityAsk = {
    type: "ask", id: "job-1", notebook_id: "", created_at: "2026-09-29T10:30:00",
    asked_at: "2026-09-29T10:29:00", conversation_id: "conv-1", question: "低温会怎样？",
    mode: "reasoning", status: "done", answer_id: "job-1", error: "", submitted_via: "",
  };
  render(
    <ActivityDetail
      askDetail={{
        job_id: "job-1", scope: "global", notebook_id: "", conversation_id: "conv-1",
        question: item.question, mode: "reasoning", status: "done",
        asked_at: "2026-09-29T10:29:00", answered_at: "2026-09-29T10:31:00",
        error: "", trace: [], answer: flaggedAnswer(), notebook_ids: ["nb-0"],
      }}
      askDetailError=""
      askDetailLoading={false}
      item={item}
      notebookNames={{}}
      now={new Date(2026, 8, 29, 12, 0)}
      reportDetail={null}
      reportDetailError=""
      reportDetailLoading={false}
    />,
  );
  expect(screen.getByText(
    "本次回答有部分引用未通过核对：1 条原文已改动。回答内容照常保留，带标记的引用可点开查看原因。",
  )).toHaveClass("answer-citation-check-notice");
  fireEvent.click(screen.getByRole("button", { name: "[1] 未通过核对：原文已改动" }));
  const popover = await screen.findByRole("dialog");
  expect(within(popover).getByRole("note")).toHaveTextContent("未通过核对：原文已改动");
  expect(popover).toHaveTextContent("低温环境下，容量下降。");
});

// --- 公开页 /c/{token} --------------------------------------------------------

const PUBLIC_CONVERSATION = {
  title: "一次全局问答",
  created_at: "2026-09-29T09:00:00Z",
  shared_at: "2026-09-29T09:30:00Z",
  truncated_turns: false,
  turns: [{
    question: "低温会怎样？",
    answer_md: "容量下降[k1]，内阻上升[k2]。",
    asked_at: "2026-09-29T09:00:00Z",
    answered_at: "2026-09-29T09:01:00Z",
    evidence_level: "overview",
    references: [
      { key: "k1", title: "甲文", file_name: "jia.pdf", location: "p. 2", snippet: "甲摘录", verification: "source_gone" },
      { key: "k2", title: "乙文", file_name: "yi.pdf", location: "p. 5", snippet: "乙摘录" },
    ],
    reference_count: 2,
    truncated_references: false,
    omitted_result_sets: 0,
    images: [
      { alias: "alias-1", caption: "甲图", reference_keys: ["k1"] },
      { alias: "alias-2", caption: "乙图", reference_keys: ["k2"] },
    ],
    citation_check: { outcome: "partial", checked: 2, failed: 1, changed: 0, source_gone: 1, unverifiable: 0 },
  }],
};

test("the public page states the check in the past tense and marks the flagged reference", async () => {
  mocks.fetchPublicConversation.mockResolvedValue(PUBLIC_CONVERSATION);
  const { container } = render(<PublicConversationPage />);
  const notice = await screen.findByText(
    "回答生成时，有部分引用未通过核对：1 条资料已删除。回答内容照常保留，带标记的引用可点开查看原因。",
  );
  expect(notice).toHaveClass("answer-citation-check-notice");
  // 正文完整。
  expect(container.querySelector(".public-turn-answer")).toHaveTextContent("容量下降[1]，内阻上升[2]。");

  const flagged = screen.getByRole("button", { name: "[1] 未通过核对：资料已删除" });
  expect(flagged).toHaveClass("cite-chip-unverified");
  expect(screen.getByRole("button", { name: "[2]" }).className).toBe("cite-chip");

  const references = screen.getByRole("region", { name: "引用出处" });
  const items = within(references).getAllByRole("listitem");
  expect(items).toHaveLength(2);
  expect(items[0]).toHaveTextContent("未通过核对：资料已删除");
  expect(items[0]).toHaveTextContent("甲摘录");
  expect(items[1]).not.toHaveTextContent("未通过核对");

  // 带标记引用名下的附图不插入;通过核对的那张照常。
  expect(await screen.findByRole("img", { name: "乙图" })).toBeInTheDocument();
  expect(screen.queryByRole("img", { name: "甲图" })).toBeNull();

  // 点带标记的标记:滚到清单里那一条(原因在那里),不是打开什么资料。
  fireEvent.click(flagged);
  await waitFor(() => expect(items[0]).toHaveClass("active"));
});

test("a public turn without a check summary has no notice and no marked references", async () => {
  const [turn] = PUBLIC_CONVERSATION.turns;
  mocks.fetchPublicConversation.mockResolvedValue({
    ...PUBLIC_CONVERSATION,
    turns: [{
      ...turn,
      citation_check: undefined,
      references: turn.references.map(({ verification: _unused, ...rest }) => rest),
    }],
  });
  const { container } = render(<PublicConversationPage />);
  await screen.findByRole("button", { name: "[1]" });
  expect(container.querySelector(".answer-citation-check-notice")).toBeNull();
  expect(container.querySelector(".cite-chip-unverified")).toBeNull();
  expect(container.querySelector(".public-report-verification")).toBeNull();
  expect(await screen.findByRole("img", { name: "甲图" })).toBeInTheDocument();
});
