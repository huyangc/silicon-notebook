import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { CitationPopover } from "../../app/citation-card";
import { PublicCitationPopover } from "../../app/public-citation-popover";

afterEach(() => vi.unstubAllGlobals());

function resizeObserver() {
  let notify: ResizeObserverCallback | undefined;
  let observer: ResizeObserver;
  const observe = vi.fn();
  const disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: ResizeObserverCallback) { notify = callback; observer = this as unknown as ResizeObserver; }
    observe = observe;
    disconnect = disconnect;
  });
  return { observe, disconnect, notify: () => act(() => notify?.([], observer)) };
}

function measureCard(height: () => number) {
  const original = HTMLElement.prototype.getBoundingClientRect;
  return vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.getAttribute("role") !== "dialog") return original.call(this);
    return new DOMRect(0, Number.parseFloat(this.style.top) || 0, 240, height());
  });
}

function expectInsideViewport(card: HTMLElement, height: number) {
  expect(Number.parseFloat(card.style.top)).toBeGreaterThanOrEqual(0);
  expect(Number.parseFloat(card.style.top) + height).toBeLessThanOrEqual(window.innerHeight);
}

test("引用原文展开后按新的卡片尺寸重新放入视口", async () => {
  const user = userEvent.setup();
  const resize = resizeObserver();
  let height = 96;
  measureCard(() => height);
  render(<PublicCitationPopover reference={{ key: "k1", title: "来源", file_name: "", location: "", snippet: "摘录",
    evidence: { kind: "original", text: "完整的原文上下文。", quote: "原文", location: "", truncated: false },
  }} displayLabel="[1]" anchorRect={new DOMRect(200, window.innerHeight - 180, 12, 12)} onClose={() => {}} />);
  const card = screen.getByRole("dialog");
  expectInsideViewport(card, height);
  await user.click(screen.getByRole("button", { name: "查看原文" }));
  height = window.innerHeight / 2;
  resize.notify();
  expectInsideViewport(card, height);
  expect(resize.observe).toHaveBeenCalledWith(card);
});

test("视口调整时重新定位，卸载后停止观察和监听", () => {
  const resize = resizeObserver();
  const height = 220;
  const measure = measureCard(() => height);
  vi.stubGlobal("innerHeight", 700);
  const { unmount } = render(<CitationPopover reference={{ id: "ref-1", displayLabel: "[1]",
    citation: { label: "来源", source_id: "", element_id: "", location_label: "", quoted_span: "摘录" },
  }} notebookId={null} notebookNames={{}} anchorRect={new DOMRect(200, 500, 12, 12)} onClose={() => {}} />);
  const card = screen.getByRole("dialog");
  expectInsideViewport(card, height);
  vi.stubGlobal("innerHeight", 360);
  fireEvent(window, new Event("resize"));
  expectInsideViewport(card, height);
  unmount();
  expect(resize.disconnect).toHaveBeenCalledOnce();
  const measuredBeforeResize = measure.mock.calls.length;
  fireEvent(window, new Event("resize"));
  expect(measure.mock.calls.length).toBe(measuredBeforeResize);
});
