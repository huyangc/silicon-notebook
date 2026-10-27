import { act, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { NotebookDeletionReceipt, DELETION_RECEIPT_HOLD_MS, DELETION_RECEIPT_FOLD_MS } from "../../app/notebook-deletion-receipt";
import type { NotebookDeletions } from "../../app/notebook-deletions-block";

const notice = {
  notebook_id: "nb-own", name: "我的旧库", delete_after: "2026-10-28T08:00:00Z",
  is_owner: true, memory_count: 0, transferable_count: 0,
};
const deletions: NotebookDeletions = {
  state: { status: "ready", items: [notice] }, nowMs: Date.now(), reload: vi.fn(),
};

const originalAnimate = Object.getOwnPropertyDescriptor(Element.prototype, "animate");
afterEach(() => {
  vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();
  if (originalAnimate) Object.defineProperty(Element.prototype, "animate", originalAnimate);
  else Reflect.deleteProperty(Element.prototype, "animate");
});

test("提示可直接打开通知；动画后移除占位，刷新同一窗口不再出现，再次删除会重新提示", () => {
  vi.useFakeTimers();
  const animate = vi.fn(() => ({ cancel: vi.fn() }));
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false })));
  Object.defineProperty(Element.prototype, "animate", { configurable: true, value: animate });
  const destinationRef = createRef<HTMLButtonElement>();
  const onOpen = vi.fn();
  const props = { deletions, destinationRef, enabled: true, onOpen };
  const view = render(<><button ref={destinationRef}>通知</button><NotebookDeletionReceipt {...props} /></>);
  expect(screen.getByRole("status")).toHaveTextContent("我的旧库");
  fireEvent.click(screen.getByRole("button", { name: "待确认中心" }));
  expect(onOpen).toHaveBeenCalledOnce();
  act(() => vi.advanceTimersByTime(DELETION_RECEIPT_HOLD_MS));
  expect(animate).toHaveBeenCalled();
  expect(screen.getByRole("status")).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(DELETION_RECEIPT_FOLD_MS));
  expect(screen.queryByRole("status")).toBeNull();
  view.rerender(<><button ref={destinationRef}>通知</button><NotebookDeletionReceipt {...props} deletions={{ ...deletions, state: { status: "ready", items: [{ ...notice }] } }} /></>);
  expect(screen.queryByRole("status")).toBeNull();
  view.rerender(<><button ref={destinationRef}>通知</button><NotebookDeletionReceipt {...props} deletions={{ ...deletions, state: { status: "ready", items: [{ ...notice, delete_after: "2026-10-29T08:00:00Z" }] } }} /></>);
  expect(screen.getByRole("status")).toBeInTheDocument();
});

test("减少动态效果时短暂提示后直接收起；离开列表再返回不会重复提示", () => {
  vi.useFakeTimers();
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
  const destinationRef = createRef<HTMLButtonElement>();
  const props = { deletions, destinationRef, enabled: true, onOpen: vi.fn() };
  const view = render(<NotebookDeletionReceipt {...props} />);
  act(() => vi.advanceTimersByTime(DELETION_RECEIPT_HOLD_MS));
  expect(view.container).toBeEmptyDOMElement();
  view.rerender(<NotebookDeletionReceipt {...props} enabled={false} />);
  view.rerender(<NotebookDeletionReceipt {...props} />);
  expect(view.container).toBeEmptyDOMElement();
});

test("列表隐藏期间的新通知会在返回列表时提示", () => {
  vi.useFakeTimers();
  const props = { deletions, destinationRef: createRef<HTMLButtonElement>(), onOpen: vi.fn() };
  const view = render(<NotebookDeletionReceipt {...props} enabled={false} />);
  expect(view.container).toBeEmptyDOMElement();
  view.rerender(<NotebookDeletionReceipt {...props} enabled />);
  expect(screen.getByRole("status")).toBeInTheDocument();
});

test("列表重新挂载沿用本用户已提示的窗口，切换用户后独立提示", () => {
  const introduced = new Set<string>();
  const props = { deletions, introduced, enabled: true, destinationRef: createRef<HTMLButtonElement>(), onOpen: vi.fn() };
  const first = render(<NotebookDeletionReceipt {...props} />);
  expect(screen.getByRole("status")).toBeInTheDocument();
  first.unmount();
  const second = render(<NotebookDeletionReceipt {...props} />);
  expect(second.container).toBeEmptyDOMElement();
  second.rerender(<NotebookDeletionReceipt {...props} introduced={new Set()} />);
  expect(screen.getByRole("status")).toBeInTheDocument();
});
