import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useSourceSidebar } from "../../app/use-source-sidebar";
import { SourceSidebarHint, sourceSidebarHintKey } from "../../app/source-sidebar-hint";
import { useSessionSidebarMorph } from "../../app/use-session-sidebar-morph";

beforeEach(() => {
  window.localStorage.clear();
  vi.stubGlobal("PointerEvent", MouseEvent);
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.hasPointerCapture = vi.fn(() => false);
});
afterEach(() => vi.unstubAllGlobals());

function Harness({ active = true, owner = "one" }: { active?: boolean; owner?: string }) {
  const [collapsed, setCollapsed] = useState(false);
  function collapse() { sidebar.captureCollapse(); setCollapsed(true); }
  const sidebar = useSourceSidebar({ active, collapsed, ownerKey: owner, onCollapse: collapse });
  return <section data-testid="grid" style={sidebar.style}>
    {!collapsed ? <aside ref={(element) => {
      sidebar.panelRef.current = element;
      if (element) element.getBoundingClientRect = () => ({ left: 24, top: 90, width: Number.parseFloat(sidebar.style?.["--sources-width" as keyof typeof sidebar.style] as string) || 300, height: 700 }) as DOMRect;
    }}>
      <div {...sidebar.resizeProps} />
      <button onClick={collapse}>收起</button>
    </aside> : <button className="sources-restore-button" onClick={() => setCollapsed(false)}>还原</button>}
    {sidebar.ghost && <div data-testid="source-ghost" ref={sidebar.ghostRef} />}
  </section>;
}

function dragTo(x: number) {
  const edge = screen.getByRole("separator");
  fireEvent.pointerDown(edge, { clientX: 324, button: 0 });
  fireEvent.pointerMove(edge, { clientX: x });
  return edge;
}

test("右边缘只能向左缩小，向右拖不增加宽度", () => {
  render(<Harness />);
  const edge = dragTo(400);
  expect(screen.getByTestId("grid")).toHaveStyle("--sources-width: 300px");
  fireEvent.pointerMove(edge, { clientX: 264 });
  expect(screen.getByTestId("grid")).toHaveStyle("--sources-width: 240px");
  fireEvent.pointerMove(edge, { clientX: 300 });
  expect(screen.getByTestId("grid")).toHaveStyle("--sources-width: 240px");
  fireEvent.pointerUp(edge);
  fireEvent.pointerDown(edge, { clientX: 264, button: 0 });
  fireEvent.pointerMove(edge, { clientX: 400 });
  expect(screen.getByTestId("grid")).toHaveStyle("--sources-width: 240px");
});

test("拖到左边缘收起，焦点落在还原按钮，展开恢复默认宽度", () => {
  render(<Harness />);
  dragTo(80);
  expect(screen.queryByRole("separator")).toBeNull();
  expect(screen.getByRole("button", { name: "还原" })).toHaveFocus();
  fireEvent.click(screen.getByRole("button", { name: "还原" }));
  expect(screen.getByRole("separator")).toBeInTheDocument();
  expect(screen.getByTestId("grid").style.getPropertyValue("--sources-width")).toBe("");
});

test("取消拖动撤回本次宽度调整，丢失捕获也能恢复", () => {
  render(<Harness />);
  const edge = dragTo(264);
  fireEvent.pointerUp(edge);
  fireEvent.pointerDown(edge, { clientX: 264, button: 0 });
  fireEvent.pointerMove(edge, { clientX: 210 });
  fireEvent.pointerCancel(edge);
  expect(screen.getByTestId("grid")).toHaveStyle("--sources-width: 240px");
  fireEvent.pointerDown(edge, { clientX: 264, button: 0 });
  fireEvent.pointerMove(edge, { clientX: 210 });
  fireEvent.lostPointerCapture(edge);
  expect(screen.getByTestId("grid")).toHaveStyle("--sources-width: 240px");
});

test("键盘向左缩小、向右不放大，Home 收起", () => {
  render(<Harness />);
  const edge = screen.getByRole("separator");
  fireEvent.keyDown(edge, { key: "ArrowLeft" });
  expect(screen.getByTestId("grid")).toHaveStyle("--sources-width: 276px");
  fireEvent.keyDown(edge, { key: "ArrowRight" });
  expect(screen.getByTestId("grid")).toHaveStyle("--sources-width: 276px");
  fireEvent.keyDown(edge, { key: "Home" });
  expect(screen.getByRole("button", { name: "还原" })).toBeInTheDocument();
});

test("切换笔记本或窄屏会终止拖动并清除临时宽度", () => {
  const view = render(<Harness />);
  const edge = dragTo(264);
  view.rerender(<Harness owner="two" />);
  fireEvent.pointerMove(edge, { clientX: 80 });
  expect(screen.queryByRole("button", { name: "还原" })).toBeNull();
  expect(screen.getByTestId("grid").style.getPropertyValue("--sources-width")).toBe("");
  view.rerender(<Harness owner="two" active={false} />);
  dragTo(80);
  expect(screen.getByTestId("grid").style.getPropertyValue("--sources-width")).toBe("");
});

test("提示不抢焦点，知道了按用户记忆，另一个用户仍可看到", () => {
  const view = render(<SourceSidebarHint userId="one" active onCollapse={vi.fn()} />);
  expect(screen.getByRole("note", { name: "来源栏提示" })).not.toContainElement(document.activeElement as HTMLElement);
  fireEvent.click(screen.getByRole("button", { name: "知道了" }));
  expect(window.localStorage.getItem(sourceSidebarHintKey("one"))).toBe("1");
  view.unmount();
  const next = render(<SourceSidebarHint userId="one" active onCollapse={vi.fn()} />);
  expect(screen.queryByRole("note")).toBeNull();
  next.rerender(<SourceSidebarHint key="two" userId="two" active onCollapse={vi.fn()} />);
  expect(screen.getByRole("note")).toBeInTheDocument();
});

test("看到提示后收起或拖动就记为已读，窄屏没展示过不算", () => {
  const view = render(<SourceSidebarHint userId="one" active={false} onCollapse={vi.fn()} />);
  expect(window.localStorage.getItem(sourceSidebarHintKey("one"))).toBeNull();
  view.rerender(<SourceSidebarHint userId="one" active onCollapse={vi.fn()} />);
  expect(screen.getByRole("note")).toBeInTheDocument();
  view.rerender(<SourceSidebarHint userId="one" active={false} onCollapse={vi.fn()} />);
  expect(window.localStorage.getItem(sourceSidebarHintKey("one"))).toBe("1");
});

test("提示里的收起文字直接执行收起，本机存储不可用仍可关闭", () => {
  const collapse = vi.fn();
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("unavailable"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("unavailable"); });
  render(<SourceSidebarHint userId="one" active onCollapse={collapse} />);
  fireEvent.click(screen.getByRole("button", { name: "收起来源栏" }));
  expect(collapse).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "知道了" }));
  expect(screen.queryByRole("note")).toBeNull();
});

test("来源栏收进还原按钮后清理影子", () => {
  const animate = vi.fn(() => ({ cancel: vi.fn(), onfinish: null as (() => void) | null }));
  Element.prototype.animate = animate as unknown as Element["animate"];
  const original = HTMLElement.prototype.getBoundingClientRect;
  HTMLElement.prototype.getBoundingClientRect = () => ({ left: 24, top: 90, width: 32, height: 32 }) as DOMRect;
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "收起" }));
  expect(screen.getByTestId("source-ghost")).toBeInTheDocument();
  act(() => animate.mock.results[0].value.onfinish?.());
  expect(screen.queryByTestId("source-ghost")).toBeNull();
  HTMLElement.prototype.getBoundingClientRect = original;
  delete (Element.prototype as Partial<Element>).animate;
});

test("减少动态效果时来源栏直接收起，不调用动画或留下影子", () => {
  const animate = vi.fn();
  Element.prototype.animate = animate;
  window.matchMedia = vi.fn(() => ({ matches: true })) as unknown as typeof window.matchMedia;
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "收起" }));
  expect(screen.getByRole("button", { name: "还原" })).toBeInTheDocument();
  expect(screen.queryByTestId("source-ghost")).toBeNull();
  expect(animate).not.toHaveBeenCalled();
  delete (Element.prototype as Partial<Element>).animate;
});

test("来源栏和会话栏同时形变时，来源影子仍落在还原按钮的最终位置", () => {
  let historyAnimating = false;
  const frames: { target: Element; keyframes: Keyframe[] }[] = [];
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false })));
  Element.prototype.animate = vi.fn(function (this: Element, keyframes: Keyframe[]) {
    if (this.getAttribute("aria-label") === "历史会话") historyAnimating = true;
    frames.push({ target: this, keyframes });
    return { cancel: vi.fn(), onfinish: null } as unknown as Animation;
  }) as unknown as Element["animate"];
  const sourceRect = { left: 24, top: 90, width: 300, height: 700 };
  const restoreRect = { left: 38, top: 102, width: 32, height: 32 };
  function Integrated() {
    const [collapsed, setCollapsed] = useState(false);
    const history = useSessionSidebarMorph(collapsed);
    const source = useSourceSidebar({ active: true, collapsed, ownerKey: "one", onCollapse: collapse, restoreTargetRef: history.restoreTargetRef });
    function collapse() { history.capture(); source.captureCollapse(); setCollapsed(true); }
    return <>
      {!collapsed ? <>
        <aside ref={(element) => {
          source.panelRef.current = element;
          if (element) element.getBoundingClientRect = () => sourceRect as DOMRect;
        }}><button onClick={collapse}>收起来源</button></aside>
        <button className="chat-session-toggle" ref={(element) => {
          if (element) element.getBoundingClientRect = () => ({ left: 900, top: 90, width: 80, height: 34 }) as DOMRect;
        }}>历史</button>
      </> : <aside aria-label="历史会话" ref={(element) => {
        history.sidebarRef.current = element;
        if (element) element.getBoundingClientRect = () => ({ ...sourceRect, width: 248 }) as DOMRect;
      }}><button className="sources-restore-button" ref={(element) => {
        if (element) element.getBoundingClientRect = () => (historyAnimating
          ? { left: 905, top: 90, width: 10, height: 2 }
          : restoreRect) as DOMRect;
      }}>展开来源栏</button></aside>}
      {source.ghost && <div data-testid="source-ghost" ref={source.ghostRef} />}
    </>;
  }
  render(<Integrated />);
  fireEvent.click(screen.getByRole("button", { name: "收起来源" }));
  expect(historyAnimating).toBe(true);
  const flight = frames.find((frame) => frame.target === screen.getByTestId("source-ghost"))!;
  expect(flight.keyframes[1].transform).toBe(`translate(14px, 12px) scale(${32 / 300}, ${32 / 700})`);
  delete (Element.prototype as Partial<Element>).animate;
});
