import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLayoutEffect, useRef, useState } from "react";
import { beforeAll, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/source-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/source-api")>();
  return {
    ...actual,
    fetchInternalAssetBlob: vi.fn(async () => new Blob(["fake-image-bytes"], { type: "image/png" })),
  };
});

import { AnswerMarkdown } from "../../app/answer-markdown";
import { AnswerView } from "../../app/answer-panel";
import { ImagePreviewModal } from "../../app/image-preview-modal";
import type { AnswerImagePreviewRequest } from "../../app/image-preview";
import type { CitationImageOrder } from "../../app/rehype-citation-images";
import { fetchInternalAssetBlob } from "../../app/source-api";
import type { AskResponse } from "../../app/workspace-model";

beforeAll(() => {
  if (typeof URL.createObjectURL !== "function") URL.createObjectURL = vi.fn(() => "blob:mock-url");
  if (typeof URL.revokeObjectURL !== "function") URL.revokeObjectURL = vi.fn();
});

beforeEach(() => vi.mocked(fetchInternalAssetBlob).mockClear());

function renderAnswer(answer: AskResponse, extraProps: Record<string, unknown> = {}) {
  return render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={() => undefined}
      memorySaved={false}
      {...extraProps}
    />,
  );
}

function anchorAnswerWithImages(answer = "第一段结论 [k1]。\n\n第二段继续说明。"): AskResponse {
  return {
    answer_id: "answer-anchor-with-image",
    conversation_id: "conversation-1",
    conclusion: answer,
    answer,
    grounded: true,
    anchors: [{
      key: "k1", object_id: "el-1", object_type: "element",
      label: "结论", name: "结论", source_title: "来源论文",
      location_label: "p. 3", source_id: "source-1", element_id: "el-1",
      tier: "personal",
      images: [{ element_id: "img-el-1", asset_id: "asset-1", caption: "图 1：示意图" }],
    }],
    related_knowledge: [], citations: [], llm_mode: "reasoning",
  };
}

test("附图无需打开引用浮层，直接插在命中引用的段落之后", async () => {
  renderAnswer(anchorAnswerWithImages());

  const imageRegion = await screen.findByRole("complementary", { name: "引用图片 [1]" });
  const firstParagraph = screen.getByText(/第一段结论/).closest("p");
  const secondParagraph = screen.getByText("第二段继续说明。");
  expect(firstParagraph?.nextElementSibling).toBe(imageRegion);
  expect(imageRegion.nextElementSibling).toBe(secondParagraph);
  expect(within(imageRegion).getByText("模型未直接读取图片")).toBeInTheDocument();
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);
});

test("回答外的输入状态更新时已加载附图保持挂载且不重复取图", async () => {
  const answer = anchorAnswerWithImages();
  const view = renderAnswer(answer);
  const image = await screen.findByRole("img", { name: "图 1：示意图" });
  const textBelowCard = screen.getByText("第二段继续说明。");
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);

  // Ask 输入框每敲一个字都会让页面壳重新渲染 AnswerView。这里用与回答内容无关的
  // prop 变化模拟那次父级更新：图片节点不能被 react-markdown 的自定义组件换型，
  // 否则 AuthedImage 会先 revoke object URL、再请求一次，用户看到的就是闪动。
  view.rerender(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex
      onSaveMemory={() => undefined}
      memorySaved={false}
    />,
  );

  await waitFor(() => expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1));
  expect(screen.getByRole("img", { name: "图 1：示意图" })).toBe(image);
  // 真机症状是「图片卡片下方的文字打字时频闪」：卡片后的正文节点同样必须原地保留，
  // 不允许被重建（重建=一帧删除+一帧插入，看起来就是文字在闪）。
  expect(screen.getByText("第二段继续说明。")).toBe(textBelowCard);
});

// 打字时的父级重渲染不止不能换型重挂载，连 remark+KaTeX+rehype 解析都不该重跑
// （O(全会话内容)/键的开销）。观测点用 citationImageOrder 账本：解析每重跑一次都
// 会清空重记（items 换成新数组），所以「数组身份未变」就是「管线没重跑」的直接
// 证据；反过来，回答内容真变了必须重跑并重新记账。
test("与回答无关的更新复用已解析的 markdown 子树，回答变化时才重新解析", () => {
  const order: CitationImageOrder = { items: [] };
  const { anchors } = anchorAnswerWithImages();
  const markdownProps = {
    anchors,
    onReferenceClick: () => undefined,
    renderCitationImages: () => null,
    citationImageOrder: order,
  };
  const view = render(
    <AnswerMarkdown answer="第一段结论 [k1]。" selectedReferenceId={null} {...markdownProps} />,
  );
  const ledger = order.items;
  expect(ledger).toEqual([{ citationKey: "k1", imageId: "asset-1" }]);

  view.rerender(
    <AnswerMarkdown answer="第一段结论 [k1]。" selectedReferenceId="ref-1" {...markdownProps} />,
  );
  expect(order.items).toBe(ledger);

  view.rerender(
    <AnswerMarkdown answer="改写后的结论 [k1]。" selectedReferenceId="ref-1" {...markdownProps} />,
  );
  expect(order.items).not.toBe(ledger);
  expect(order.items).toEqual([{ citationKey: "k1", imageId: "asset-1" }]);
});

test("caption 只作为图片 alt，不在正文或引用浮层重复显示", async () => {
  const user = userEvent.setup();
  const { container } = renderAnswer(anchorAnswerWithImages());

  const imageRegion = await screen.findByRole("complementary", { name: "引用图片 [1]" });
  expect(within(imageRegion).getByRole("img", { name: "图 1：示意图" })).toBeInTheDocument();
  expect(imageRegion.textContent).not.toContain("图 1：示意图");

  await user.click(screen.getByRole("button", { name: "[1]" }));
  const popover = screen.getByRole("dialog");
  await within(popover).findByRole("img", { name: "图 1：示意图" });
  expect(popover.textContent).not.toContain("图 1：示意图");
  expect(container.querySelector(".cite-detail-image-caption")).toBeNull();
});

test("引用本身是图片元素时隐藏其解析描述，文字证据附近带图时仍保留真实摘录", async () => {
  const user = userEvent.setup();
  const direct = anchorAnswerWithImages("图片结论 [k1]。");
  direct.anchors[0].element_id = "img-el-1";
  direct.anchors[0].snippet = "图注与 AI 图片描述";
  const directView = renderAnswer(direct);

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.queryByText("图注与 AI 图片描述")).toBeNull();
  directView.unmount();

  const nearby = anchorAnswerWithImages("文字结论 [k1]。");
  nearby.anchors[0].snippet = "真正被引用的文字摘录";
  renderAnswer(nearby);
  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("真正被引用的文字摘录")).toBeInTheDocument();
});

test("点击正文图片请求页面内放大，并保留 caption 作为预览 alt", async () => {
  const user = userEvent.setup();
  const onPreviewImage = vi.fn();
  renderAnswer(anchorAnswerWithImages(), { onPreviewImage });

  await screen.findByRole("img", { name: "图 1：示意图" });
  await user.click(screen.getByRole("button", { name: "放大查看[1]的附图" }));
  expect(onPreviewImage).toHaveBeenCalledWith({
    items: [{ assetId: "asset-1", alt: "图 1：示意图", referenceLabel: "[1]" }],
    index: 0,
  });
});

function twoImageAnswer(): AskResponse {
  const answer = "先说第一点 [k1]。\n\n再说第二点 [k2]。";
  return {
    answer_id: "answer-two-images",
    conversation_id: "conversation-1",
    conclusion: answer,
    answer,
    grounded: true,
    anchors: [
      {
        key: "k1", object_id: "el-1", object_type: "element",
        label: "一", name: "一", source_title: "来源甲", location_label: "p. 1",
        source_id: "source-1", element_id: "el-1", tier: "personal",
        images: [{ element_id: "img-el-1", asset_id: "asset-1", caption: "图 1" }],
      },
      {
        key: "k2", object_id: "el-2", object_type: "element",
        label: "二", name: "二", source_title: "来源乙", location_label: "p. 2",
        source_id: "source-2", element_id: "el-2", tier: "personal",
        images: [{ element_id: "img-el-2", asset_id: "asset-2", caption: "图 2" }],
      },
    ],
    related_knowledge: [], citations: [], llm_mode: "reasoning",
  };
}

// 顺序对账：左右切换走的画册（buildImageGallery，按引用首次出现排序）必须与
// rehypeCitationImages 实际插进正文的图片区块顺序逐条一致。两处是各自独立推导出
// 来的顺序，这条用例是它们唯一的对账点——分叉了，按「→」翻到的就不是正文里的
// 下一张。
test("正文多图时交出整本画册，顺序与正文里的图片区块一致", async () => {
  const user = userEvent.setup();
  const onPreviewImage = vi.fn();
  const { container } = renderAnswer(twoImageAnswer(), { onPreviewImage });

  await screen.findByRole("img", { name: "图 1" });
  const openButtons = [...container.querySelectorAll(".answer-inline-image-open")];
  expect(openButtons.map((button) => button.getAttribute("aria-label"))).toEqual([
    "放大查看[1]的附图",
    "放大查看[2]的附图",
  ]);

  const expectedItems = [
    { assetId: "asset-1", alt: "图 1", referenceLabel: "[1]" },
    { assetId: "asset-2", alt: "图 2", referenceLabel: "[2]" },
  ];
  await user.click(openButtons[1]);
  expect(onPreviewImage).toHaveBeenCalledWith({ items: expectedItems, index: 1 });

  await user.click(openButtons[0]);
  expect(onPreviewImage).toHaveBeenLastCalledWith({ items: expectedItems, index: 0 });
});

// codex #599 R1 P2：没有 anchor 时 references 回退成 `citations` 数组顺序,而正文完全
// 可以先写 [2] 再写 [1]。画册顺序过去是按 references 数组自己推导的,于是「→」翻出来的
// 顺序与眼睛看到的相反;从未在正文出现过的引用的图片也会被算进画册。现在顺序由渲染管线
// 记账(citationImageOrder)提供,这条用例按 DOM 顺序对账它。
test("回退引用路径下,画册跟随正文顺序而不是 citations 数组顺序", async () => {
  const user = userEvent.setup();
  const onPreviewImage = vi.fn();
  const body = "先引用后面那条 [2]。\n\n再引用前面那条 [1]。\n\n这条引用没在正文出现过。";
  const citation = (n: number, assetId: string) => ({
    label: `来源 ${n}`,
    source_id: `source-${n}`,
    element_id: `el-${n}`,
    location_label: `p. ${n}`,
    quoted_span: "",
    tier: "personal",
    images: [{ element_id: `img-el-${n}`, asset_id: assetId, caption: `图 ${n}` }],
  });
  const { container } = renderAnswer({
    answer_id: "answer-fallback-order",
    conversation_id: "conversation-1",
    conclusion: body,
    answer: body,
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [citation(1, "asset-1"), citation(2, "asset-2"), citation(3, "asset-3")],
    llm_mode: "chunk",
  } as unknown as AskResponse, { onPreviewImage });

  await screen.findByRole("img", { name: "图 2" });
  const openButtons = [...container.querySelectorAll(".answer-inline-image-open")];
  expect(openButtons.map((button) => button.getAttribute("aria-label"))).toEqual([
    "放大查看[2]的附图",
    "放大查看[1]的附图",
  ]);

  // 画册 = 正文顺序（[2] 在前），且不含从未在正文出现过的 [3] 的图片。
  const expectedItems = [
    { assetId: "asset-2", alt: "图 2", referenceLabel: "[2]" },
    { assetId: "asset-1", alt: "图 1", referenceLabel: "[1]" },
  ];
  await user.click(openButtons[0]);
  expect(onPreviewImage).toHaveBeenCalledWith({ items: expectedItems, index: 0 });
  await user.click(openButtons[1]);
  expect(onPreviewImage).toHaveBeenLastCalledWith({ items: expectedItems, index: 1 });
});

// 引用浮层里的缩略图与正文图片是同一批资产,点开后左右切换必须能走遍整条回答,
// 而不是被关在「这条引用的附图」里。
test("引用浮层里的缩略图打开的也是整本画册", async () => {
  const user = userEvent.setup();
  const onPreviewImage = vi.fn();
  renderAnswer(twoImageAnswer(), { onPreviewImage });

  await user.click(screen.getByRole("button", { name: "[2]" }));
  const popover = screen.getByRole("dialog");
  await user.click(await within(popover).findByTitle("放大查看这张附图"));
  expect(onPreviewImage).toHaveBeenCalledWith({
    items: [
      { assetId: "asset-1", alt: "图 1", referenceLabel: "[1]" },
      { assetId: "asset-2", alt: "图 2", referenceLabel: "[2]" },
    ],
    index: 1,
  });
});

test("引用浮层里的缩略图也只打开页面内预览，不跳来源详情", async () => {
  const user = userEvent.setup();
  const onPreviewImage = vi.fn();
  const onOpenSource = vi.fn();
  renderAnswer(anchorAnswerWithImages(), { onPreviewImage, onOpenSource });

  await user.click(screen.getByRole("button", { name: "[1]" }));
  const popover = screen.getByRole("dialog");
  await user.click(await within(popover).findByTitle("放大查看这张附图"));
  expect(onPreviewImage).toHaveBeenCalledWith({
    items: [{ assetId: "asset-1", alt: "图 1：示意图", referenceLabel: "[1]" }],
    index: 0,
  });
  expect(onOpenSource).not.toHaveBeenCalled();
});

test("引用浮层打开预览后暂停外部关闭，Escape 关闭预览并把焦点还给仍存活的缩略图", async () => {
  function Harness() {
    const [preview, setPreview] = useState<AnswerImagePreviewRequest | null>(null);
    const returnFocusRef = useRef<HTMLElement | null>(null);
    const restorePendingRef = useRef(false);
    useLayoutEffect(() => {
      if (preview || !restorePendingRef.current) return;
      restorePendingRef.current = false;
      if (returnFocusRef.current?.isConnected) returnFocusRef.current.focus();
    }, [preview]);
    return (
      <>
        <AnswerView
          answer={anchorAnswerWithImages()}
          feedbackSent=""
          onFeedback={() => undefined}
          onOpenKnowledgeGraph={() => undefined}
          onOpenKnowhowRow={() => undefined}
          notebookId="nb-1"
          notebookNames={{}}
          onBuildScaleIndex={() => undefined}
          buildingScaleIndex={false}
          onSaveMemory={() => undefined}
          memorySaved={false}
          imagePreviewOpen={Boolean(preview)}
          onPreviewImage={(request) => {
            returnFocusRef.current = document.activeElement as HTMLElement;
            setPreview(request);
          }}
        />
        {preview && (
          <ImagePreviewModal
            referenceLabel={preview.items[preview.index].referenceLabel}
            imageIndex={preview.index}
            imageCount={preview.items.length}
            onSelectImage={(index) => setPreview((prev) => (prev ? { ...prev, index } : prev))}
            onClose={() => {
              restorePendingRef.current = true;
              setPreview(null);
            }}
          >
            <img src="blob:preview" alt={preview.items[preview.index].alt} />
          </ImagePreviewModal>
        )}
      </>
    );
  }

  const user = userEvent.setup();
  const { container } = render(<Harness />);
  await user.click(screen.getByRole("button", { name: "[1]" }));
  const thumbnailButton = await screen.findByTitle("放大查看这张附图");
  await user.click(thumbnailButton);
  expect(screen.getByRole("dialog", { name: "引用 [1] 图片预览" })).toBeInTheDocument();

  document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
  expect(container.querySelector(".cite-popover")).not.toBeNull();

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "引用 [1] 图片预览" })).toBeNull();
  expect(container.querySelector(".cite-popover")).not.toBeNull();
  expect(screen.getByTitle("放大查看这张附图")).toHaveFocus();
});

test("旧答案缺 images 字段时不插入附图区，其余引用仍可打开", async () => {
  const user = userEvent.setup();
  const answer = anchorAnswerWithImages("结论 [k1]。");
  delete answer.anchors[0].images;
  renderAnswer(answer);

  expect(screen.queryByRole("complementary", { name: /本段附图/ })).toBeNull();
  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText(/来源论文/)).toBeInTheDocument();
  expect(screen.queryByRole("img")).toBeNull();
});

test("同一图片被正文多次引用时只在第一次出现处展示一次", async () => {
  const { container } = renderAnswer(anchorAnswerWithImages("第一处 [k1]。\n\n第二处 [k1]。"));

  await screen.findByRole("img");
  expect(container.querySelectorAll(".answer-inline-images")).toHaveLength(1);
  expect(container.querySelectorAll(".answer-inline-image-item")).toHaveLength(1);
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);
});

test("跨库附图仍经当前 active notebook 的代理资产端点读取", async () => {
  const answer = anchorAnswerWithImages();
  answer.anchors[0].notebook_id = "base-1";
  answer.anchors[0].tier = "base";
  renderAnswer(answer);

  await screen.findByRole("img");
  const url = vi.mocked(fetchInternalAssetBlob).mock.calls[0][0];
  expect(url).toContain("/notebooks/nb-1/assets/asset-1");
  expect(url).not.toContain("base-1");
});

test("citation 回退编号携带 images 时也在正文引用位置插图", async () => {
  const answer: AskResponse = {
    answer_id: "answer-citation-fallback-image",
    conversation_id: "conversation-1",
    conclusion: "结论 [1]。",
    answer: "结论 [1]。",
    grounded: true,
    anchors: [],
    related_knowledge: [],
    citations: [{
      label: "来源论文 · p.3", source_id: "source-1", element_id: "el-1",
      location_label: "p. 3", quoted_span: "原文片段",
      images: [{ element_id: "img-el-1", asset_id: "asset-1", caption: "图 2" }],
    }],
    llm_mode: "reasoning",
  };
  renderAnswer(answer);

  const region = await screen.findByRole("complementary", { name: "引用图片 [1]" });
  expect(within(region).getByRole("img", { name: "图 2" })).toBeInTheDocument();
  expect(region.textContent).not.toContain("图 2");
});

test("列表和引用中的图片跟随最小内部段落，表格图片等整表结束后出现", async () => {
  const answer = anchorAnswerWithImages([
    "- 列表结论 [k1]",
    "",
    "> 引用结论 [k2]",
    "",
    "| 指标 | 结论 |",
    "| --- | --- |",
    "| A | 表格结论 [k3] |",
  ].join("\n"));
  answer.anchors = [
    answer.anchors[0],
    {
      ...answer.anchors[0], key: "k2", object_id: "el-2", element_id: "el-2",
      images: [{ element_id: "img-el-2", asset_id: "asset-2", caption: "引用图" }],
    },
    {
      ...answer.anchors[0], key: "k3", object_id: "el-3", element_id: "el-3",
      images: [{ element_id: "img-el-3", asset_id: "asset-3", caption: "表格图" }],
    },
  ];
  const { container } = renderAnswer(answer);

  await screen.findByRole("img", { name: "表格图" });
  const listItem = screen.getByText(/列表结论/).closest("li");
  expect(listItem).not.toBeNull();
  expect(within(listItem as HTMLElement).getByRole("complementary", { name: "引用图片 [1]" }))
    .toHaveClass("answer-inline-images");

  const quoteParagraph = screen.getByText(/引用结论/).closest("p");
  const quoteRegion = quoteParagraph?.nextElementSibling;
  expect(quoteRegion).toHaveClass("answer-inline-images");
  expect(quoteRegion?.closest("blockquote")).not.toBeNull();

  const tableWrap = container.querySelector(".answer-table-wrap");
  expect(tableWrap?.nextElementSibling).toHaveClass("answer-inline-images");
  expect(tableWrap?.querySelector(".answer-inline-images")).toBeNull();
});

test("同一完整块的复合引用按正文引用顺序合并附图", async () => {
  const answer = anchorAnswerWithImages("联合结论 [k2]、[k1]。");
  answer.anchors = [
    answer.anchors[0],
    {
      ...answer.anchors[0], key: "k2", object_id: "el-2", element_id: "el-2",
      images: [{ element_id: "img-el-2", asset_id: "asset-2", caption: "第二张图" }],
    },
  ];
  const { container } = renderAnswer(answer);

  await screen.findByRole("img", { name: "第二张图" });
  const images = [...container.querySelectorAll<HTMLImageElement>(".answer-inline-images img")];
  expect(images.map((image) => image.alt)).toEqual(["第二张图", "图 1：示意图"]);
});
