import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { ReasoningTracePanel } from "../../app/answer-panel";

afterEach(() => vi.unstubAllGlobals());

// jsdom has no layout engine: supply the measured preview dimensions, while
// exercising the real panel, disclosure control, resize and stream updates.
function mockLayout(initialHeight: number) {
  let height = initialHeight;
  const observers = new Set<() => void>();
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.tagName === "SMALL" ? height : 0;
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(30);
  vi.stubGlobal("ResizeObserver", class {
    constructor(private callback: () => void) {}
    observe() { observers.add(this.callback); }
    disconnect() { observers.delete(this.callback); }
  });
  return {
    resize(nextHeight: number) {
      height = nextHeight;
      act(() => { [...observers].forEach((callback) => callback()); });
    },
    observers,
  };
}

function intent(text: string) {
  return { step_type: "intent", summary: "已按确认后的问题理解开始检索", detail: { resolved_question: text }, duration_ms: 51300 };
}

test("长详情通过按钮展开完整原文，可键盘收起，且不折叠整个轨迹", async () => {
  mockLayout(180);
  const user = userEvent.setup();
  const text = "逐篇说明当前笔记本中所有文章分别讨论了什么主题或内容。".repeat(20);
  render(<ReasoningTracePanel steps={[intent(text)]} />);
  await user.click(screen.getByRole("button", { expanded: false }));
  const more = screen.getByRole("button", { name: "查看完整内容" });
  expect(more).toHaveAttribute("aria-expanded", "false");
  const detail = document.getElementById(more.getAttribute("aria-controls")!);
  expect(detail?.textContent).toBe(text);
  await user.click(more);
  expect(screen.getByRole("button", { name: "收起" })).toHaveAttribute("aria-expanded", "true");
  expect(detail).toHaveAttribute("tabindex", "0");
  expect(detail?.textContent).toBe(text);
  expect(screen.getByText("51.3s")).toBeVisible();
  await user.keyboard("{Enter}");
  expect(more).toHaveTextContent("查看完整内容");
  expect(detail).not.toHaveAttribute("tabindex");
  expect(screen.getByRole("list")).toBeVisible();
});

test("不超过两行的详情不出现按钮，容器变窄或变宽后重新判断并清理监听", async () => {
  const layout = mockLayout(30);
  const user = userEvent.setup();
  const { unmount } = render(<ReasoningTracePanel steps={[intent("短问题")]} />);
  await user.click(screen.getByRole("button", { expanded: false }));
  expect(screen.queryByRole("button", { name: "查看完整内容" })).toBeNull();
  layout.resize(60);
  expect(screen.getByRole("button", { name: "查看完整内容" })).toBeVisible();
  layout.resize(30);
  expect(screen.queryByRole("button", { name: "查看完整内容" })).toBeNull();
  unmount();
  expect(layout.observers.size).toBe(0);
});

test("流式替换详情时收起旧全文，新增步骤不影响当前展开状态", async () => {
  mockLayout(120);
  const user = userEvent.setup();
  const { rerender } = render(<ReasoningTracePanel steps={[intent("原问题")]} live />);
  await user.click(screen.getByRole("button", { expanded: false }));
  await user.click(screen.getByRole("button", { name: "查看完整内容" }));
  const next = { step_type: "skip", summary: "未找到相关记忆", detail: {} };
  rerender(<ReasoningTracePanel steps={[intent("原问题"), next]} live />);
  expect(screen.getByRole("button", { name: "收起" })).toHaveAttribute("aria-expanded", "true");
  rerender(<ReasoningTracePanel steps={[intent("确认后的新问题"), next]} live />);
  expect(screen.queryByRole("button", { name: "收起" })).toBeNull();
  const more = screen.getByRole("button", { name: "查看完整内容" });
  expect(document.getElementById(more.getAttribute("aria-controls")!)?.textContent).toBe("确认后的新问题");
});
