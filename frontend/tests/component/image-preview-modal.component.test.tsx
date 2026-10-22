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

// 一次 Esc 只收一层：预览收下的 Esc 要掐掉默认动作（浏览器的 close request），否则
// 下面那一层模态 <dialog>（全屏的全局问答窗口）会在同一次按键里也收到关闭请求。
test("预览收下的 Escape 掐掉默认动作，其他按键不掐", () => {
  const onClose = vi.fn();
  render(
    <ImagePreviewModal referenceLabel="[2]" onClose={onClose}>
      <img src="/preview.png" alt="流程图" />
    </ImagePreviewModal>,
  );
  const dialog = screen.getByRole("dialog", { name: "引用 [2] 图片预览" });
  expect(fireEvent.keyDown(dialog, { key: "Escape" })).toBe(false);
  expect(onClose).toHaveBeenLastCalledWith("escape");
  expect(fireEvent.keyDown(dialog, { key: "Enter" })).toBe(true);
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("打开时关闭按钮的 autoFocus 没落地，挂载后补聚焦；落地了就不再动焦点", () => {
  const nativeFocus = HTMLElement.prototype.focus;
  // 打开那一刻焦点进不去（下面那层还是模态的）：第一次 focus 调用落空。
  let blocked = true;
  const gate = vi.spyOn(HTMLElement.prototype, "focus").mockImplementation(function focus(this: HTMLElement, options?: FocusOptions) {
    if (blocked && this.getAttribute("aria-label") === "关闭图片预览") { blocked = false; return; }
    nativeFocus.call(this, options);
  });
  try {
    const first = render(
      <ImagePreviewModal referenceLabel="[2]" onClose={vi.fn()}><img src="/a.png" alt="a" /></ImagePreviewModal>,
    );
    expect(blocked).toBe(false);
    expect(screen.getByRole("button", { name: "关闭图片预览" })).toHaveFocus();
    first.unmount();
  } finally {
    gate.mockRestore();
  }
  // autoFocus 正常落地时只有它那一次聚焦。
  const focus = vi.spyOn(HTMLElement.prototype, "focus");
  render(<ImagePreviewModal referenceLabel="[2]" onClose={vi.fn()}><img src="/a.png" alt="a" /></ImagePreviewModal>);
  expect(screen.getByRole("button", { name: "关闭图片预览" })).toHaveFocus();
  expect(focus).toHaveBeenCalledTimes(1);
  focus.mockRestore();
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

test("单张图片时不出现任何切换控件，方向键也不做任何事", async () => {
  const onSelectImage = vi.fn();
  render(
    <ImagePreviewModal referenceLabel="[1]" onClose={() => undefined} onSelectImage={onSelectImage}>
      <img src="/preview.png" alt="唯一一张" />
    </ImagePreviewModal>,
  );

  const dialog = screen.getByRole("dialog", { name: "引用 [1] 图片预览" });
  expect(screen.queryByRole("button", { name: "上一张图片" })).toBeNull();
  expect(screen.queryByRole("button", { name: "下一张图片" })).toBeNull();
  expect(screen.queryByText("1 / 1")).toBeNull();
  fireEvent.keyDown(dialog, { key: "ArrowRight" });
  fireEvent.keyDown(dialog, { key: "ArrowLeft" });
  expect(onSelectImage).not.toHaveBeenCalled();
});

test("多张图片时左右按键与切换按钮都翻页，计数如实显示当前位置", async () => {
  const user = userEvent.setup();
  const onSelectImage = vi.fn();
  const { rerender } = render(
    <ImagePreviewModal
      referenceLabel="[2]"
      onClose={() => undefined}
      imageIndex={1}
      imageCount={3}
      onSelectImage={onSelectImage}
    >
      <img src="/preview-2.png" alt="第二张" />
    </ImagePreviewModal>,
  );

  const dialog = screen.getByRole("dialog", { name: "引用 [2] 图片预览" });
  expect(screen.getByText("2 / 3")).toBeInTheDocument();

  fireEvent.keyDown(dialog, { key: "ArrowRight" });
  expect(onSelectImage).toHaveBeenLastCalledWith(2);
  fireEvent.keyDown(dialog, { key: "ArrowLeft" });
  expect(onSelectImage).toHaveBeenLastCalledWith(0);

  await user.click(screen.getByRole("button", { name: "下一张图片" }));
  expect(onSelectImage).toHaveBeenLastCalledWith(2);
  await user.click(screen.getByRole("button", { name: "上一张图片" }));
  expect(onSelectImage).toHaveBeenLastCalledWith(0);
  expect(onSelectImage).toHaveBeenCalledTimes(4);

  rerender(
    <ImagePreviewModal
      referenceLabel="[3]"
      onClose={() => undefined}
      imageIndex={2}
      imageCount={3}
      onSelectImage={onSelectImage}
    >
      <img src="/preview-3.png" alt="第三张" />
    </ImagePreviewModal>,
  );
  expect(screen.getByText("3 / 3")).toBeInTheDocument();
});

test("到头/到尾按钮禁用，方向键原地不动，切换按钮不会被当成点遮罩关闭", async () => {
  const user = userEvent.setup();
  const onClose = vi.fn();
  const onSelectImage = vi.fn();
  const { rerender } = render(
    <ImagePreviewModal
      referenceLabel="[1]"
      onClose={onClose}
      imageIndex={0}
      imageCount={2}
      onSelectImage={onSelectImage}
    >
      <img src="/preview.png" alt="第一张" />
    </ImagePreviewModal>,
  );

  const dialog = screen.getByRole("dialog", { name: "引用 [1] 图片预览" });
  expect(screen.getByRole("button", { name: "上一张图片" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "下一张图片" })).toBeEnabled();
  fireEvent.keyDown(dialog, { key: "ArrowLeft" });
  expect(onSelectImage).not.toHaveBeenCalled();

  // 切换按钮落在舞台里,点它绝不能被 pointerup 的「点遮罩关闭」判据吃掉。
  await user.click(screen.getByRole("button", { name: "下一张图片" }));
  expect(onSelectImage).toHaveBeenLastCalledWith(1);
  expect(onClose).not.toHaveBeenCalled();

  rerender(
    <ImagePreviewModal
      referenceLabel="[2]"
      onClose={onClose}
      imageIndex={1}
      imageCount={2}
      onSelectImage={onSelectImage}
    >
      <img src="/preview-2.png" alt="第二张" />
    </ImagePreviewModal>,
  );
  expect(screen.getByRole("button", { name: "下一张图片" })).toBeDisabled();
  fireEvent.keyDown(dialog, { key: "ArrowRight" });
  expect(onSelectImage).toHaveBeenCalledTimes(1);
});

test("换图后缩放回到 100%，上一张的放大倍数不会带到下一张", async () => {
  const user = userEvent.setup();
  const { rerender } = render(
    <ImagePreviewModal
      referenceLabel="[1]"
      onClose={() => undefined}
      imageIndex={0}
      imageCount={2}
      onSelectImage={() => undefined}
    >
      <img src="/preview.png" alt="第一张" />
    </ImagePreviewModal>,
  );

  await user.click(screen.getByRole("button", { name: "放大图片" }));
  await waitFor(() => expect(screen.queryByText("100%")).toBeNull());

  rerender(
    <ImagePreviewModal
      referenceLabel="[2]"
      onClose={() => undefined}
      imageIndex={1}
      imageCount={2}
      onSelectImage={() => undefined}
    >
      <img src="/preview-2.png" alt="第二张" />
    </ImagePreviewModal>,
  );
  await waitFor(() => expect(screen.getByText("100%")).toBeInTheDocument());
});

test("被更高层弹窗覆盖时不接方向键，正在输入的地方也不抢方向键", async () => {
  const onSelectImage = vi.fn();
  const covered = render(
    <ImagePreviewModal
      referenceLabel="[1]"
      interactive={false}
      onClose={() => undefined}
      imageIndex={0}
      imageCount={3}
      onSelectImage={onSelectImage}
    >
      <img src="/preview.png" alt="被盖住的" />
    </ImagePreviewModal>,
  );
  fireEvent.keyDown(window, { key: "ArrowRight" });
  expect(onSelectImage).not.toHaveBeenCalled();
  covered.unmount();

  // 预览没有焦点陷阱：Tab 出去落到页面里的输入框时，方向键属于光标移动。
  render(
    <>
      <textarea aria-label="提问" />
      <ImagePreviewModal
        referenceLabel="[1]"
        onClose={() => undefined}
        imageIndex={0}
        imageCount={3}
        onSelectImage={onSelectImage}
      >
        <img src="/preview.png" alt="第一张" />
      </ImagePreviewModal>
    </>,
  );
  fireEvent.keyDown(screen.getByLabelText("提问"), { key: "ArrowRight" });
  expect(onSelectImage).not.toHaveBeenCalled();

  // 同一次挂载下,预览自己身上的方向键照常翻页——上面两条不是「整个功能没接上」。
  fireEvent.keyDown(screen.getByRole("dialog", { name: "引用 [1] 图片预览" }), { key: "ArrowRight" });
  expect(onSelectImage).toHaveBeenLastCalledWith(1);
});
