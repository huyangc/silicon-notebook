// T6:深度报告引用卡附图——ReportMarkdown 的「参考文献」详情区渲染
// reference.images（后端 EvidenceContextService.attach_reference_images 装配）。
//
// 与 answer-citation-images.component.test.tsx（T2）共用同一套视觉语义/CSS
// class（.cite-detail-images 等），本文件只钉报告侧特有的三条:
//   1. 带 images 的引用详情渲染附图区与图注,缺字段(旧报告)/空数组不渲染;
//   2. notebookId 缺席(无资产代理端点可用)时整体不渲染附图区,即使有 images;
//   3. AuthedImage 保持懒挂载——弹层/详情未打开时不发鉴权图片请求。
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

import { ReportMarkdown, type ReportDetailT } from "../../app/report-view";
import { fetchInternalAssetBlob } from "../../app/source-api";

beforeAll(() => {
  // jsdom 不实现 blob object URL;AuthedImage 依赖它们创建/回收 <img src>。
  if (typeof URL.createObjectURL !== "function") {
    URL.createObjectURL = vi.fn(() => "blob:mock-url");
  }
  if (typeof URL.revokeObjectURL !== "function") {
    URL.revokeObjectURL = vi.fn();
  }
});

function referenceWithImage(
  overrides: Partial<ReportDetailT["references"][number]> = {},
): ReportDetailT["references"] {
  return [{
    key: "k1",
    label: "时序手册",
    source_title: "时序手册",
    location_label: "§1",
    snippet: "被引用的原文片段",
    images: [{ element_id: "el-fig-1", asset_id: "asset-1", caption: "图 1：时钟树收敛示意" }],
    ...overrides,
  }];
}

test("带 images 的引用详情渲染「本段附图」区与图注（notebookId 已提供）", async () => {
  const user = userEvent.setup();
  render(
    <ReportMarkdown markdown="结论 [k1]。" references={referenceWithImage()} notebookId="nb-1" />,
  );

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("本段附图")).toBeInTheDocument();
  const img = await screen.findByRole("img");
  expect(img).toBeInTheDocument();
  expect(screen.getByText("图 1：时钟树收敛示意")).toBeInTheDocument();
});

test("无图注的附图仍渲染缩略图，但不渲染图注文字", async () => {
  const user = userEvent.setup();
  render(
    <ReportMarkdown
      markdown="结论 [k1]。"
      references={referenceWithImage({
        images: [{ element_id: "el-fig-1", asset_id: "asset-1", caption: "" }],
      })}
      notebookId="nb-1"
    />,
  );

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("本段附图")).toBeInTheDocument();
  await screen.findByRole("img");
  expect(document.querySelector(".cite-detail-image-caption")).toBeNull();
});

test("旧报告缺 images 字段：不渲染「本段附图」区，其余渲染逐字节不变", async () => {
  const user = userEvent.setup();
  render(
    <ReportMarkdown
      markdown="结论 [k1]。"
      references={referenceWithImage({ images: undefined })}
      notebookId="nb-1"
    />,
  );

  await user.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("时序手册")).toBeInTheDocument();
  expect(screen.getByText("被引用的原文片段")).toBeInTheDocument();
  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
});

test("空 images 数组（exclude_if 语义的另一面）：同样不渲染附图区", async () => {
  const user = userEvent.setup();
  render(
    <ReportMarkdown
      markdown="结论 [k1]。"
      references={referenceWithImage({ images: [] })}
      notebookId="nb-1"
    />,
  );

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
});

test("引用详情未打开时不挂载附图，不发鉴权图片请求", () => {
  vi.mocked(fetchInternalAssetBlob).mockClear();
  render(
    <ReportMarkdown markdown="结论 [k1]。" references={referenceWithImage()} notebookId="nb-1" />,
  );

  expect(screen.queryByText("本段附图")).not.toBeInTheDocument();
  expect(screen.queryByRole("img")).not.toBeInTheDocument();
  expect(fetchInternalAssetBlob).not.toHaveBeenCalled();
});
