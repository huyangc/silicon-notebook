import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { SourceDetailWindow } from "../../app/source-detail-window";


class TestPointerEvent extends MouseEvent {
  readonly pointerId: number;
  readonly pointerType: string;

  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 0;
    this.pointerType = init.pointerType ?? "";
  }
}


afterEach(() => {
  vi.unstubAllGlobals();
});


test("uses a conventional accessible close control", async () => {
  const onClose = vi.fn();
  const user = userEvent.setup();

  render(
    <SourceDetailWindow onClose={onClose}>
      <p>详情正文</p>
    </SourceDetailWindow>,
  );

  const dialog = screen.getByRole("dialog", { name: "来源" });
  expect(dialog).toHaveTextContent("详情正文");
  const close = screen.getByRole("button", { name: "关闭来源详情" });
  expect(close).toHaveAttribute("title", "关闭");
  await user.click(close);
  expect(onClose).toHaveBeenCalledTimes(1);
});


test("removes a covered source dialog from the interactive accessibility tree", () => {
  const { container, rerender } = render(
    <SourceDetailWindow onClose={() => undefined} interactive={false} zIndex={61}>
      <p>详情正文</p>
    </SourceDetailWindow>,
  );

  const covered = container.querySelector<HTMLElement>('[role="dialog"]');
  expect(covered).not.toBeNull();
  expect(covered).toHaveAttribute("aria-hidden", "true");
  expect(covered).toHaveAttribute("inert");
  expect(covered).toHaveAttribute("aria-modal", "false");
  expect(covered).toHaveStyle({ zIndex: "61" });

  rerender(
    <SourceDetailWindow onClose={() => undefined} interactive>
      <p>详情正文</p>
    </SourceDetailWindow>,
  );
  const active = screen.getByRole("dialog", { name: "来源" });
  expect(active).not.toHaveAttribute("aria-hidden", "true");
  expect(active).not.toHaveAttribute("inert");
  expect(active).toHaveAttribute("aria-modal", "true");
});


test("drags the dialog card from its header", () => {
  vi.stubGlobal("PointerEvent", TestPointerEvent);
  let pendingFrame: FrameRequestCallback | null = null;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    pendingFrame = callback;
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 142,
    y: 84,
    width: 740,
    height: 600,
    top: 84,
    right: 882,
    bottom: 684,
    left: 142,
    toJSON: () => ({}),
  });

  render(
    <SourceDetailWindow onClose={() => undefined}>
      <p>详情正文</p>
    </SourceDetailWindow>,
  );

  const dialog = screen.getByRole("dialog", { name: "来源" });
  const card = dialog.querySelector<HTMLElement>(".source-detail-card");
  const header = dialog.querySelector<HTMLElement>(".source-detail-shell-header");
  expect(card).not.toBeNull();
  expect(header).not.toBeNull();
  expect(header).toHaveStyle({ cursor: "grab" });
  expect(card).toHaveStyle({ transform: "translate3d(0px, 0px, 0)" });

  fireEvent.pointerDown(header!, {
    pointerId: 7,
    pointerType: "mouse",
    button: 0,
    clientX: 100,
    clientY: 100,
  });
  fireEvent.pointerMove(window, {
    pointerId: 7,
    pointerType: "mouse",
    clientX: 140,
    clientY: 125,
  });
  act(() => pendingFrame?.(0));

  expect(card).toHaveStyle({ transform: "translate3d(40px, 25px, 0)" });
  fireEvent.pointerUp(window, { pointerId: 7, pointerType: "mouse" });
});
