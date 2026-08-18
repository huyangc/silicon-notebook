// T2:检索结果带图——Ask 引用/锚点弹层的「本段附图」渲染。
//
// 覆盖设计文档 §2.1/§2.3 与任务要求的三条:
//   1. 带 images 的锚点弹层渲染附图区与图注;
//   2. 无 images 字段的旧答案逐字节回退(不渲染附图区);
//   3. 弹层未打开时不挂载附图,不发鉴权图片请求(对齐
//      answer-collection-results.component.test.tsx 的清单卡懒挂载测试形态)。
// 额外覆盖:点击附图跳转来源详情定位到该图片元素、citation 回退列表(无 [k] 锚点
// 命中时)同样渲染这套区块——referenceImages() 是 anchor 优先/citation 兜底,
// 与本文件其余 reference* helper 同一惯例。
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, expect, test, vi } from "vitest";

vi.mock("../../app/source-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/source-api")>();
  return {
    ...actual,
    fetchInternalAssetBlob: vi.fn(async () => new Blob(["fake-image-bytes"], { type: "image/png" })),
  };
});

import { AnswerView } from "../../app/answer-panel";
import { fetchInternalAssetBlob } from "../../app/source-api";
import type { AskResponse } from "../../app/workspace-model";

beforeAll(() => {
  // jsdom 不实现 blob object URL;AuthedImage 依赖它们创建/回收 <img src>,
  // 测试只关心"渲染到了 img 节点"而不关心真实 URL 的可解析性。
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = vi.fn(() => "blob:mock-url");
  }
  if (typeof URL.revokeObjectURL !== "function") {
    URL.revokeObjectURL = vi.fn();
  }
});

function renderAnswer(answer: AskResponse, extraProps: Record<string, unknown> = {}) {
  render(
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

function anchorAnswerWithImages(): AskResponse {
  return {
    answer_id: "answer-anchor-with-image",
    conversation_id: "conversation-1",
    conclusion: "结论 [k1]。",
    answer: "结论 [k1]。",
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

test("带 images 的锚点弹层渲染「本段附图」区与图注", async () => {
  const user = userEvent.setup();
  renderAnswer(anchorAnswerWithImages());

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("本段附图")).toBeInTheDocument();
  const img = await screen.findByRole("img");
  expect(img).toBeInTheDocument();
  expect(screen.getByText("图 1：示意图")).toBeInTheDocument();
});

test("无图注的附图仍渲染缩略图,但不渲染图注文字", async () => {
  const user = userEvent.setup();
  const answer = anchorAnswerWithImages();
  answer.anchors[0].images = [{ element_id: "img-el-1", asset_id: "asset-1", caption: "" }];
  renderAnswer(answer);

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("本段附图")).toBeInTheDocument();
  await screen.findByRole("img");
  // 图注区块本身没有渲染任何 <small> 文字节点(附图区只剩标题 + 图),不会留下空文案。
  expect(document.querySelector(".cite-detail-image-caption")).toBeNull();
});

test("旧答案缺 images 字段:不渲染「本段附图」区,其余渲染逐字节不变", async () => {
  const user = userEvent.setup();
  const answer: AskResponse = {
    answer_id: "answer-no-images-field",
    conversation_id: "conversation-1",
    conclusion: "结论 [k1]。",
    answer: "结论 [k1]。",
    grounded: true,
    anchors: [{
      key: "k1", object_id: "el-1", object_type: "element",
      label: "结论", name: "结论", source_title: "来源论文",
      location_label: "p. 3", source_id: "source-1", element_id: "el-1",
      tier: "personal",
      // 旧持久化答案:没有 images 键(不是空数组,是这个字段压根不存在)。
    }],
    related_knowledge: [], citations: [], llm_mode: "reasoning",
  };
  renderAnswer(answer);

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("结论")).toBeInTheDocument();
  expect(screen.getByText(/来源论文/)).toBeInTheDocument();
  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("空 images 数组(exclude_if 语义的另一面):同样不渲染附图区", async () => {
  const user = userEvent.setup();
  const answer = anchorAnswerWithImages();
  answer.anchors[0].images = [];
  renderAnswer(answer);

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("弹层未打开时不挂载附图,不发鉴权图片请求", () => {
  vi.mocked(fetchInternalAssetBlob).mockClear();
  renderAnswer(anchorAnswerWithImages());

  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
});

test("关闭弹层后不再显示附图区,重新打开的懒挂载纪律仍然成立(对齐枚举卡既有测试形态)", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchInternalAssetBlob).mockClear();
  renderAnswer(anchorAnswerWithImages());

  const badge = screen.getByRole("button", { name: "[1]" });
  await user.click(badge);
  await screen.findByRole("img");
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);

  // 既有交互:Esc 收起浮层(点徽章本身会重新打开而不是 toggle 关闭,见
  // answer-panel-readonly.component.test.tsx 的既有惯例)。
  await user.keyboard("{Escape}");
  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("点击附图跳转来源详情,定位到该图片元素(复用「查看原文」的 onOpenSource 通道)", async () => {
  const user = userEvent.setup();
  const onOpenSource = vi.fn();
  renderAnswer(anchorAnswerWithImages(), { onOpenSource });

  await user.click(screen.getByRole("button", { name: "[1]" }));
  await screen.findByRole("img");
  await user.click(screen.getByTitle("在来源详情中查看这张附图"));
  expect(onOpenSource).toHaveBeenCalledWith("source-1", "img-el-1");
});

test("未传 onOpenSource 时附图仍展示,只是不可点击跳转", async () => {
  const user = userEvent.setup();
  renderAnswer(anchorAnswerWithImages());

  await user.click(screen.getByRole("button", { name: "[1]" }));
  await screen.findByRole("img");
  expect(screen.queryByTitle("在来源详情中查看这张附图")).not.toBeInTheDocument();
});

test("citation 回退列表(无 [k] 锚点命中)携带 images 时渲染同一套附图区", async () => {
  const user = userEvent.setup();
  const answer: AskResponse = {
    answer_id: "answer-citation-fallback-image",
    conversation_id: "conversation-1",
    // 正文里的 "[1]" 是回退列表自己合成的显示编号(buildAnswerReferences 在零
    // 有效锚点命中时才会启用这条路径),不是 [k\d+] 锚点标记。
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

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("本段附图")).toBeInTheDocument();
  await screen.findByRole("img");
  expect(screen.getByText("图 2")).toBeInTheDocument();
});
