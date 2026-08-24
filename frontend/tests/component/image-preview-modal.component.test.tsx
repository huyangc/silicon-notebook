import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ImagePreviewModal } from "../../app/image-preview-modal";

test("图片预览支持关闭按钮、Escape 与遮罩关闭", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  render(
    <ImagePreviewModal referenceLabel="[2]" onClose={onClose}>
      <img src="/preview.png" alt="流程图" />
    </ImagePreviewModal>,
  );

  const dialog = screen.getByRole("dialog", { name: "[2]附图预览" });
  expect(screen.getByText("模型未直接读取图片")).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "流程图" })).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "关闭图片预览" }));
  expect(onClose).toHaveBeenLastCalledWith("button");
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(onClose).toHaveBeenLastCalledWith("escape");
  await user.click(dialog);
  expect(onClose).toHaveBeenLastCalledWith("backdrop");
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
