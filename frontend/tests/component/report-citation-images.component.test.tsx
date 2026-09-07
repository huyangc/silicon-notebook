// 深度报告的引用图片。两期各占一半:
//
// * T6(一期):ReportMarkdown 的引用详情区渲染 reference.images(后端
//   `EvidenceContextService.attach_reference_images` 装配)——「本段附图」区块;
// * 二期:正文接上 Ask 的同一条内联图片管线(`rehypeCitationImages` 块级落位 +
//   跨引用按资产去重 + `InlineCitationImages` 渲染 + 页内放大预览)。
//
// 二期这一半的判据逐条对齐 `answer-citation-images.component.test.tsx`:两个面接的
// 是**同一个**插件与**同一个**组件,块级落位、全篇去重、caption 只作 alt、未可见不
// 发图片请求、画册顺序=正文顺序这几条契约必须一模一样。分叉了就说明有人在报告侧
// 另写了一份。
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/source-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/source-api")>();
  return {
    ...actual,
    fetchInternalAssetBlob: vi.fn(async () => new Blob(["fake-image-bytes"], { type: "image/png" })),
  };
});

import { ReportMarkdown, ReportsPanel, type ReportDetailT } from "../../app/report-view";
import { fetchInternalAssetBlob } from "../../app/source-api";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

beforeAll(() => {
  // jsdom 不实现 blob object URL;AuthedImage 依赖它们创建/回收 <img src>。
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = vi.fn(() => "blob:mock-url");
  }
  if (typeof URL.revokeObjectURL !== "function") {
    URL.revokeObjectURL = vi.fn();
  }
});

beforeEach(() => vi.mocked(fetchInternalAssetBlob).mockClear());
afterEach(cleanup);

type ReportReference = ReportDetailT["references"][number];

function reference(overrides: Partial<ReportReference> = {}): ReportReference {
  return {
    key: "k1",
    label: "时序手册",
    source_title: "时序手册",
    location_label: "§1",
    snippet: "被引用的原文片段",
    images: [{ element_id: "el-fig-1", asset_id: "asset-1", caption: "图 1：时钟树收敛示意" }],
    ...overrides,
  };
}

function referenceWithImage(overrides: Partial<ReportReference> = {}): ReportReference[] {
  return [reference(overrides)];
}

function renderBody(
  markdown: string,
  references: ReportReference[],
  extra: Partial<Parameters<typeof ReportMarkdown>[0]> = {},
) {
  return render(
    <ReportMarkdown markdown={markdown} references={references} notebookId="nb-1" {...extra} />,
  );
}

const detailCard = () => screen.getByRole("complementary", { name: "引用原文" });

// ---------------------------------------------------------------------------
// T6:引用详情区的「本段附图」
// ---------------------------------------------------------------------------

test("带 images 的引用详情渲染「本段附图」区与图注（notebookId 已提供）", async () => {
  const user = userEvent.setup();
  renderBody("结论 [k1]。", referenceWithImage());

  await user.click(screen.getByRole("button", { name: "[1]" }));
  const detail = detailCard();
  expect(within(detail).getByText("本段附图")).toBeInTheDocument();
  expect(await within(detail).findByRole("img")).toBeInTheDocument();
  // 图注在详情区是可见文字(与正文内联区只作 alt 的口径不同,见下方二期用例)。
  expect(within(detail).getByText("图 1：时钟树收敛示意")).toBeInTheDocument();
});

test("无图注的附图仍渲染缩略图，但不渲染图注文字", async () => {
  const user = userEvent.setup();
  renderBody("结论 [k1]。", referenceWithImage({
    images: [{ element_id: "el-fig-1", asset_id: "asset-1", caption: "" }],
  }));

  await user.click(screen.getByRole("button", { name: "[1]" }));
  const detail = detailCard();
  expect(within(detail).getByText("本段附图")).toBeInTheDocument();
  await within(detail).findByRole("img");
  expect(document.querySelector(".cite-detail-image-caption")).toBeNull();
});

test("旧报告缺 images 字段：不渲染「本段附图」区，其余渲染逐字节不变", async () => {
  const user = userEvent.setup();
  renderBody("结论 [k1]。", referenceWithImage({ images: undefined }));

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("时序手册")).toBeInTheDocument();
  expect(screen.getByText("被引用的原文片段")).toBeInTheDocument();
  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("空 images 数组（exclude_if 语义的另一面）：同样不渲染附图区", async () => {
  const user = userEvent.setup();
  renderBody("结论 [k1]。", referenceWithImage({ images: [] }));

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("notebookId 未传时：即使 images 非空，附图区整体不渲染（无资产代理端点）", async () => {
  const user = userEvent.setup();
  render(<ReportMarkdown markdown="结论 [k1]。" references={referenceWithImage()} />);

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("时序手册")).toBeInTheDocument();
  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
});

// ---------------------------------------------------------------------------
// 二期:正文内联引用图片
// ---------------------------------------------------------------------------

test("附图无需点开引用，直接插在命中引用的段落之后", async () => {
  renderBody("第一段结论 [k1]。\n\n第二段继续说明。", referenceWithImage());

  const imageRegion = await screen.findByRole("complementary", { name: "引用图片 [1]" });
  const firstParagraph = screen.getByText(/第一段结论/).closest("p");
  const secondParagraph = screen.getByText("第二段继续说明。");
  expect(firstParagraph?.nextElementSibling).toBe(imageRegion);
  expect(imageRegion.nextElementSibling).toBe(secondParagraph);
  expect(within(imageRegion).getByText("模型未直接读取图片")).toBeInTheDocument();
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);
});

test("正文图片的 caption 只作为 alt，不在正文重复显示", async () => {
  renderBody("结论 [k1]。", referenceWithImage());

  const imageRegion = await screen.findByRole("complementary", { name: "引用图片 [1]" });
  expect(within(imageRegion).getByRole("img", { name: "图 1：时钟树收敛示意" })).toBeInTheDocument();
  expect(imageRegion.textContent).not.toContain("图 1：时钟树收敛示意");
});

test("缺 images 的报告正文不插入图片区，也不发图片请求", () => {
  const { container } = renderBody("结论 [k1]。", referenceWithImage({ images: undefined }));

  expect(container.querySelector(".answer-inline-images")).toBeNull();
  expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
});

test("没有 notebookId（无资产代理端点）时正文同样不渲染图片", () => {
  const { container } = render(
    <ReportMarkdown markdown="结论 [k1]。" references={referenceWithImage()} />,
  );

  expect(container.querySelector(".answer-inline-images")).toBeNull();
  expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
});

// 懒加载判据。AuthedImage 只在环境**有** IntersectionObserver 时把 fetch 推迟到元素
// 进入视口;jsdom 默认没有这个 API(于是退化成立即加载,上面几条用例走的正是那条
// 路径),所以这里显式装一个「永不通知可见」的 observer,钉住「未可见 → 一个图片
// 请求都不发」,与 Ask 侧同一条纪律。
test("正文图片在进入可视区域之前不发起任何图片请求", async () => {
  class NeverVisibleObserver {
    observe() { /* 永不回调 */ }
    unobserve() { /* noop */ }
    disconnect() { /* noop */ }
  }
  const original = Reflect.get(globalThis, "IntersectionObserver");
  Object.defineProperty(globalThis, "IntersectionObserver", {
    configurable: true, writable: true, value: NeverVisibleObserver,
  });
  try {
    const { container } = renderBody("结论 [k1]。", referenceWithImage());
    expect(container.querySelector(".answer-inline-images")).not.toBeNull();
    await waitFor(() => expect(screen.getByText("图片加载中…")).toBeInTheDocument());
    expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
  } finally {
    if (original) {
      Object.defineProperty(globalThis, "IntersectionObserver", {
        configurable: true, writable: true, value: original,
      });
    } else {
      Reflect.deleteProperty(globalThis, "IntersectionObserver");
    }
  }
});

test("同一张图被正文多次引用时只在第一次出现处展示一次", async () => {
  const { container } = renderBody("第一处 [k1]。\n\n第二处 [k1]。", referenceWithImage());

  await screen.findByRole("img");
  expect(container.querySelectorAll(".answer-inline-images")).toHaveLength(1);
  expect(container.querySelectorAll(".answer-inline-image-item")).toHaveLength(1);
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);
});

// 去重口径是**资产**而不是引用:两条不同引用挂同一张图(同一张图在两处被检索到)
// 时,正文里也只在第一条出现处渲染一次。
test("两条引用挂同一张图时，全篇只在第一条出现处渲染一次", async () => {
  const { container } = renderBody("先说第一点 [k1]。\n\n再说第二点 [k2]。", [
    reference(),
    reference({ key: "k2", source_title: "另一份手册" }),
  ]);

  await screen.findByRole("img");
  expect(container.querySelectorAll(".answer-inline-image-item")).toHaveLength(1);
  expect(container.querySelector(".answer-inline-images"))
    .toHaveAttribute("aria-label", "引用图片 [1]");
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);
});

test("列表项的图片留在列表项内，表格的图片等整表结束后出现", async () => {
  const { container } = renderBody(
    [
      "- 列表结论 [k1]",
      "",
      "| 指标 | 结论 |",
      "| --- | --- |",
      "| A | 表格结论 [k2] |",
    ].join("\n"),
    [
      reference(),
      reference({
        key: "k2",
        images: [{ element_id: "el-fig-2", asset_id: "asset-2", caption: "表格图" }],
      }),
    ],
  );

  await screen.findByRole("img", { name: "表格图" });
  const listItem = screen.getByText(/列表结论/).closest("li");
  expect(within(listItem as HTMLElement).getByRole("complementary", { name: "引用图片 [1]" }))
    .toHaveClass("answer-inline-images");
  const tableWrap = container.querySelector(".answer-table-wrap");
  expect(tableWrap?.nextElementSibling).toHaveClass("answer-inline-images");
  expect(tableWrap?.querySelector(".answer-inline-images")).toBeNull();
});

// 点引用徽章会改 selectedRefKey → 整个 ReportMarkdown 重渲染。react-markdown 的
// components 若每次重建,React 会换型重挂载整棵子树,已加载的 AuthedImage 就会
// revoke objectURL 再取一次(真机症状:正文图片与其下方文字一起频闪)。
test("点开引用详情不重挂载正文图片，也不重复取图", async () => {
  const user = userEvent.setup();
  renderBody("结论 [k1]。\n\n下一段。", referenceWithImage());

  const image = await screen.findByRole("img", { name: "图 1：时钟树收敛示意" });
  const textBelow = screen.getByText("下一段。");
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(detailCard()).toBeInTheDocument();

  // 详情区自己那张缩略图是新挂载的一张(第 2 次请求);正文那张必须原地不动。
  await waitFor(() => expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(2));
  const bodyRegion = screen.getByRole("complementary", { name: "引用图片 [1]" });
  expect(within(bodyRegion).getByRole("img", { name: "图 1：时钟树收敛示意" })).toBe(image);
  expect(screen.getByText("下一段。")).toBe(textBelow);
});

test("点击正文图片交出整本画册，顺序与正文里的图片区块一致", async () => {
  const user = userEvent.setup();
  const onPreviewImage = vi.fn();
  const { container } = renderBody(
    "先说第二点 [k2]。\n\n再说第一点 [k1]。",
    [
      reference(),
      reference({
        key: "k2",
        images: [{ element_id: "el-fig-2", asset_id: "asset-2", caption: "图 2" }],
      }),
    ],
    { onPreviewImage },
  );

  await screen.findByRole("img", { name: "图 2" });
  const openButtons = [...container.querySelectorAll(".answer-inline-image-open")];
  expect(openButtons.map((button) => button.getAttribute("aria-label"))).toEqual([
    "放大查看[2]的附图",
    "放大查看[1]的附图",
  ]);

  const expectedItems = [
    { assetId: "asset-2", alt: "图 2", referenceLabel: "[2]" },
    { assetId: "asset-1", alt: "图 1：时钟树收敛示意", referenceLabel: "[1]" },
  ];
  await user.click(openButtons[1]);
  expect(onPreviewImage).toHaveBeenCalledWith({ items: expectedItems, index: 1 });
  await user.click(openButtons[0]);
  expect(onPreviewImage).toHaveBeenLastCalledWith({ items: expectedItems, index: 0 });
});

test("没有预览承接方时正文图片照常显示，只是不可点击放大", async () => {
  const { container } = renderBody("结论 [k1]。", referenceWithImage());

  await screen.findByRole("img", { name: "图 1：时钟树收敛示意" });
  expect(container.querySelector(".answer-inline-image-open")).toBeNull();
});

function reportDetail(): ReportDetailT {
  return {
    id: "report-1",
    question: "研究问题",
    status: "done",
    progress: "",
    section_count: 1,
    created_at: "2026-09-01T00:00:00Z",
    created_by: "user-1",
    outline: [],
    sections: [],
    gaps: [],
    content_md: "第一节结论 [k1]。",
    references: referenceWithImage(),
    understanding: {},
    error: "",
  };
}

// ReportsPanel 在两份报告之间切换时不会被卸载重挂载(workspace.active 只是换了
// 个对象),ReportMarkdown 的 selectedRefKey 是它内部的 state,不会跟着自动清零。
// 两份报告若恰好复用同一个引用 key(报告内部各自独立编号,完全可能撞上),
// 报告 A 里点开的引用详情卡就会带着 A 的 selectedRefKey 在切到 B 后继续渲染,
// 连带对 B 发一次用户没点过的附图请求。
test("切换到另一份报告时不保留上一份报告选中的引用详情，也不多发额外的图片请求", async () => {
  const user = userEvent.setup();
  const reportA = reportDetail();
  const reportB: ReportDetailT = {
    ...reportDetail(),
    id: "report-2",
    content_md: "第二份报告的结论 [k1]。",
    references: referenceWithImage({
      images: [{ element_id: "el-fig-2", asset_id: "asset-2", caption: "图 2：另一张附图" }],
    }),
  };
  const { rerender } = render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: reportA })}
      setToast={vi.fn()}
    />,
  );

  await screen.findByRole("img", { name: "图 1：时钟树收敛示意" });
  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByRole("complementary", { name: "引用原文" })).toBeInTheDocument();
  await waitFor(() => expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(2));

  vi.mocked(fetchInternalAssetBlob).mockClear();
  rerender(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: reportB })}
      setToast={vi.fn()}
    />,
  );

  await screen.findByRole("img", { name: "图 2：另一张附图" });
  expect(screen.queryByRole("complementary", { name: "引用原文" })).not.toBeInTheDocument();
  expect(fetchInternalAssetBlob).toHaveBeenCalledTimes(1);
});

// 面板层的接线:报告详情视图必须把 notebookId 与预览承接方一路传到正文渲染,
// 否则上面那些用例全绿而真机上一张图都不出现。
test("报告详情面板把正文图片与预览承接方接到正文渲染", async () => {
  const user = userEvent.setup();
  const onPreviewImage = vi.fn();
  render(
    <ReportsPanel
      notebookId="nb-1"
      workspace={reportWorkspaceFixture({ active: reportDetail() })}
      setToast={vi.fn()}
      onPreviewImage={onPreviewImage}
    />,
  );

  await screen.findByRole("img", { name: "图 1：时钟树收敛示意" });
  await user.click(screen.getByRole("button", { name: "放大查看[1]的附图" }));
  expect(onPreviewImage).toHaveBeenCalledWith({
    items: [{ assetId: "asset-1", alt: "图 1：时钟树收敛示意", referenceLabel: "[1]" }],
    index: 0,
  });
});
