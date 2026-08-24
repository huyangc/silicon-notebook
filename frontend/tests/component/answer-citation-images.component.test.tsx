import { render, screen, within } from "@testing-library/react";
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

import { AnswerView } from "../../app/answer-panel";
import { ImagePreviewModal } from "../../app/image-preview-modal";
import type { AnswerImagePreviewRequest } from "../../app/image-preview";
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

  const imageRegion = await screen.findByRole("complementary", { name: "本段附图 [1]" });
  const firstParagraph = screen.getByText(/第一段结论/).closest("p");
  const secondParagraph = screen.getByText("第二段继续说明。");
  expect(firstParagraph?.nextElementSibling).toBe(imageRegion);
  expect(imageRegion.nextElementSibling).toBe(secondParagraph);
  expect(within(imageRegion).getByText("模型未直接读取图片")).toBeInTheDocument();
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);
});

test("caption 只作为图片 alt，不在正文或引用浮层重复显示", async () => {
  const user = userEvent.setup();
  const { container } = renderAnswer(anchorAnswerWithImages());

  const imageRegion = await screen.findByRole("complementary", { name: "本段附图 [1]" });
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
    assetId: "asset-1",
    alt: "图 1：示意图",
    referenceLabel: "[1]",
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
    assetId: "asset-1",
    alt: "图 1：示意图",
    referenceLabel: "[1]",
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
            referenceLabel={preview.referenceLabel}
            onClose={() => {
              restorePendingRef.current = true;
              setPreview(null);
            }}
          >
            <img src="blob:preview" alt={preview.alt} />
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
  expect(screen.getByRole("dialog", { name: "[1]附图预览" })).toBeInTheDocument();

  document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
  expect(container.querySelector(".cite-popover")).not.toBeNull();

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "[1]附图预览" })).toBeNull();
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

  const region = await screen.findByRole("complementary", { name: "本段附图 [1]" });
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
  expect(within(listItem as HTMLElement).getByRole("complementary", { name: "本段附图 [1]" }))
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
