import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ImagePreviewModal } from "../../app/image-preview-modal";

test("图片预览只保留轻量控制层，并支持缩放、关闭按钮、Escape 与遮罩关闭", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(
    <ImagePreviewModal referenceLabel="[2]" onClose={onClose}>
      <img src="/preview.png" alt="流程图" />
    </ImagePreviewModal>,
  );

  const dialog = screen.getByRole("dialog", { name: "引用 [2] 图片预览" });
  expect(screen.queryByText("引用 [2]")).toBeNull();
  expect(screen.queryByText("本段附图 [2]")).toBeNull();
  expect(screen.queryByText("模型未直接读取图片")).toBeNull();
  const image = screen.getByRole("img", { name: "流程图" });
  expect(image).toBeInTheDocument();
  expect(document.querySelector(".answer-image-preview-card")).toBeNull();
  await user.click(image.closest<HTMLElement>(".answer-image-preview-content")!);
  expect(onClose).not.toHaveBeenCalled();

  expect(screen.getByText("100%")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "放大图片" }));
  await waitFor(() => expect(screen.queryByText("100%")).toBeNull());
  await user.click(screen.getByRole("button", { name: "重置图片缩放" }));
  await waitFor(() => expect(screen.getByText("100%")).toBeInTheDocument());

  const zoomCanvas = dialog.querySelector<HTMLElement>(".answer-image-preview-zoom");
  const stage = dialog.querySelector<HTMLElement>(".answer-image-preview-stage")!;
  expect(zoomCanvas).not.toBeNull();
  fireEvent.wheel(zoomCanvas!, { deltaY: -100 });
  await waitFor(() => expect(screen.queryByText("100%")).toBeNull());

  await user.click(screen.getByRole("button", { name: "关闭图片预览" }));
  expect(onClose).toHaveBeenLastCalledWith("button");
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(onClose).toHaveBeenLastCalledWith("escape");
  await user.click(stage);
  expect(onClose).toHaveBeenLastCalledWith("backdrop");
});

test("双击可放大，拖动遮罩超过阈值不会误关", async () => {
  const onClose = vi.fn();
  render(
    <ImagePreviewModal referenceLabel="[3]" onClose={onClose}>
      <img src="/preview.png" alt="结构图" />
    </ImagePreviewModal>,
  );

  const dialog = screen.getByRole("dialog", { name: "引用 [3] 图片预览" });
  const zoomCanvas = dialog.querySelector<HTMLElement>(".answer-image-preview-zoom")!;
  fireEvent.doubleClick(zoomCanvas);
  await waitFor(() => expect(screen.queryByText("100%")).toBeNull());

  const stage = dialog.querySelector<HTMLElement>(".answer-image-preview-stage")!;
  fireEvent(stage, new MouseEvent("pointerdown", { bubbles: true, clientX: 10, clientY: 10 }));
  fireEvent(stage, new MouseEvent("pointerup", { bubbles: true, clientX: 30, clientY: 30 }));
  expect(onClose).not.toHaveBeenCalled();
});

test("被更高层弹窗覆盖时退出交互树", () => {
  const { container } = render(
    <ImagePreviewModal referenceLabel="[1]" interactive={false} zIndex={60} onClose={() => undefined}>
      <img src="/preview.png" alt="架构图" />
    </ImagePreviewModal>,
  );

  const dialog = container.querySelector<HTMLElement>('[role="dialog"]');
  expect(dialog).not.toBeNull();
  expect(dialog).toHaveAttribute("aria-hidden", "true");
  expect(dialog).toHaveAttribute("inert");
  expect(dialog).toHaveStyle({ zIndex: "60" });
});
